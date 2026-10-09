"""Outbound image downscaling for the local vision model (R1 carve-out).

A 3000×4000 image core-dumped llama-professional (Vulkan device lost,
2026-08-12) — no downscaling existed anywhere.  The llama-server also runs
4 parallel slots by default, so oversized images collapse decode throughput.

This module downscales any image whose long side exceeds
``IMAGE_DOWNSCALE_MAX_LONG_SIDE`` (2048px) on the OUTBOUND model-copy ONLY,
mirroring the carve-out discipline of ``context_governance`` and
``search_enrichment``:

- Only ``{"type": "image_url"}`` content parts are examined.
- Only images over the size threshold are processed (cheap fast-path).
- Base64 data URLs are decoded, downscaled (aspect preserved), re-encoded
  as JPEG (quality ~85) and re-base64'd.
- Remote http(s) URLs are fetched, downscaled, and re-encoded as base64
  data URLs; any fetch/parse failure leaves the URL untouched (fail-open).
- The client's stored conversation and the DB audit copy are never mutated.
- Per-request opt-out: ``X-Proxy-Image-Downscale: off``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import urllib.parse
from typing import Any, Optional

import httpx
from PIL import Image

from constants import (
    IMAGE_DOWNSCALE_FETCH_TIMEOUT,
    IMAGE_DOWNSCALE_JPEG_QUALITY,
    IMAGE_DOWNSCALE_MAX_FETCH_BYTES,
    IMAGE_DOWNSCALE_MAX_LONG_SIDE,
    get_logger,
)

logger = get_logger("proxy.image_downscale")


def _decode_data_url(url: str) -> Optional[tuple[bytes, str]]:
    """Return ``(image_bytes, mime)`` from a ``data:image/...`` URL.

    Returns None for anything that is not a well-formed image data URL.
    """
    if not url.startswith("data:image/"):
        return None
    try:
        header, _, b64 = url.partition(",")
        mime = header[len("data:"):].split(";", 1)[0]  # e.g. "image/png"
        return base64.b64decode(b64), mime
    except (ValueError, TypeError):
        return None


def _downscale_image_bytes(data: bytes) -> Optional[bytes]:
    """Downscale *data* in-memory when its long side exceeds the cap.

    Returns new JPEG bytes when the image was oversized, None when it was
    already within the cap or could not be decoded (fail-open — the caller
    leaves the original untouched).
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            long_side = max(width, height)
            if long_side <= IMAGE_DOWNSCALE_MAX_LONG_SIDE:
                return None
            scale = IMAGE_DOWNSCALE_MAX_LONG_SIDE / long_side
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            resized = img.convert("RGB").resize(new_size, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            resized.save(out, format="JPEG", quality=IMAGE_DOWNSCALE_JPEG_QUALITY)
            return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        logger.warning(
            "Image downscale failed (%s) — leaving original untouched", exc,
        )
        return None


def _downscale_data_url(url: str) -> Optional[str]:
    """Downscale one base64 data URL; None when within cap or unreadable."""
    decoded = _decode_data_url(url)
    if decoded is None:
        return None
    data, _mime = decoded
    downscaled = _downscale_image_bytes(data)
    if downscaled is None:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(downscaled).decode("ascii")


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when *ip* is a routable public destination.

    SSRF guard: loopback, private, link-local, multicast, unspecified, and
    reserved ranges (IPv4 + IPv6) are never fetchable by the proxy.
    """
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


async def _validate_public_http_url(url: str) -> bool:
    """True when *url* is an http(s) URL to a public destination.

    Literal IPs are checked directly.  Hostnames are resolved NOW and
    rejected when ANY resolved address is non-public (conservative — also
    covers DNS-rebinding for this resolution).  Returns False on any parse,
    DNS, or scheme failure: the caller then fails open and leaves the
    client's URL untouched.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname
    try:
        return _is_public_ip(ipaddress.ip_address(host))
    except ValueError:
        pass  # hostname, not a literal IP
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port or 443)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not _is_public_ip(ip):
            return False
    return True


async def _fetch_remote(url: str) -> Optional[bytes]:
    """Fetch a remote image, capped at ``IMAGE_DOWNSCALE_MAX_FETCH_BYTES``.

    Returns None on any failure (HTTP error, timeout, oversized, network).
    """
    if not await _validate_public_http_url(url):
        logger.warning(
            "Remote image URL rejected (non-public destination) — fail-open",
        )
        return None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(IMAGE_DOWNSCALE_FETCH_TIMEOUT),
            # Redirects are NOT followed: a 3xx could hop to a private or
            # loopback destination after the public-destination check above
            # (SSRF guard, R1/R4-001).  A redirect response is a non-200,
            # so the fetch fails open and the client's URL stays untouched.
            follow_redirects=False,
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.warning(
                        "Remote image fetch HTTP %s for %s — fail-open",
                        resp.status_code, url[:80],
                    )
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > IMAGE_DOWNSCALE_MAX_FETCH_BYTES:
                        logger.warning(
                            "Remote image exceeds fetch cap — fail-open",
                        )
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning(
            "Remote image fetch failed (%s) — leaving URL untouched", exc,
        )
        return None


async def _process_image_url(url: str) -> Optional[str]:
    """Downscale one ``image_url`` value; None when no change is needed.

    CPU-bound Pillow work (decode/downscale/re-encode) runs in a worker
    thread so the event loop is never blocked.
    """
    if url.startswith("data:image/"):
        return await asyncio.to_thread(_downscale_data_url, url)
    if url.startswith(("http://", "https://")):
        data = await _fetch_remote(url)
        if data is None:
            return None  # fail-open: keep the original URL
        downscaled = await asyncio.to_thread(_downscale_image_bytes, data)
        if downscaled is None:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(downscaled).decode("ascii")
    return None


def _has_image_url_parts(messages: list[dict[str, Any]]) -> bool:
    """Cheap fast-path scan: any ``image_url`` part at all?"""
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "image_url"
                and isinstance(part.get("image_url"), dict)
                and isinstance(part["image_url"].get("url"), str)
            ):
                return True
    return False


async def downscale_images(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a NEW messages list with oversized images downscaled.

    Operates on the OUTBOUND model-copy only: the input list is never
    mutated (R1 carve-out — the client's stored conversation and the DB
    audit copy are untouched).  When no image needs downscaling, the
    original list is returned as-is (identity preserved).
    """
    if not _has_image_url_parts(messages):
        return messages

    changed = False
    outbound: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            outbound.append(msg)
            continue
        new_parts: list[Any] = []
        msg_changed = False
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "image_url"
                and isinstance(part.get("image_url"), dict)
            ):
                url = part["image_url"].get("url")
                if isinstance(url, str):
                    replacement = await _process_image_url(url)
                    if replacement is not None:
                        msg_changed = True
                        new_parts.append({
                            **part,
                            "image_url": {**part["image_url"], "url": replacement},
                        })
                        continue
            new_parts.append(part)
        if msg_changed:
            changed = True
            outbound.append({**msg, "content": new_parts})
        else:
            outbound.append(msg)

    if not changed:
        return messages
    return outbound
