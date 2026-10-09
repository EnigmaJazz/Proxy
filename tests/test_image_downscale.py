"""Regression tests for outbound image downscaling (X-Proxy-Image-Downscale).

A 3000×4000 image core-dumped llama-professional (Vulkan device lost,
2026-08-12) — no downscaling existed anywhere.  This module downscales
oversized images (long side > 2048px) on the OUTBOUND model-copy ONLY,
mirroring the R1 carve-out discipline of context_governance / search_enrichment:
the client's stored conversation and the DB audit copy are never touched.
Opt out per request with ``X-Proxy-Image-Downscale: off``.
"""
from __future__ import annotations

import base64
import io
import types
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import proxy
import routes
from image_downscale import _fetch_remote, downscale_images


def _png_b64(width: int, height: int) -> str:
    """Encode a solid-color PNG of the given size as a base64 data URL."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 100, 50)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _image_msg(url: str) -> list[dict[str, Any]]:
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this image"},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }]


def _url_dimensions(url: str) -> tuple[int, int]:
    """Decode a data URL and return the image's (width, height)."""
    from PIL import Image

    assert url.startswith("data:image/"), url
    header, _, b64 = url.partition(",")
    assert "image/jpeg" in header, header
    with Image.open(io.BytesIO(base64.b64decode(b64))) as img:
        return img.size


class TestDownscaleImages:
    """Unit behavior of the outbound transform (pure, client copy untouched)."""

    @pytest.mark.asyncio
    async def test_oversized_base64_image_is_downscaled(self) -> None:
        original = _png_b64(64, 3200)  # long side 3200 > 2048
        out = await downscale_images(_image_msg(original))

        url = out[0]["content"][1]["image_url"]["url"]
        assert url != original
        width, height = _url_dimensions(url)
        assert max(width, height) <= 2048
        # aspect ratio preserved (64:3200 == 1:50)
        assert abs(width / height - 64 / 3200) < 0.02

    @pytest.mark.asyncio
    async def test_input_list_is_never_mutated(self) -> None:
        original = _png_b64(64, 3200)
        msgs = _image_msg(original)
        await downscale_images(msgs)
        # R1 carve-out: the client copy is untouched
        assert msgs[0]["content"][1]["image_url"]["url"] == original

    @pytest.mark.asyncio
    async def test_small_image_is_untouched(self) -> None:
        small = _png_b64(800, 600)  # under the 2048px cap
        msgs = _image_msg(small)
        out = await downscale_images(msgs)
        assert out is msgs  # identity preserved — no transform ran
        assert msgs[0]["content"][1]["image_url"]["url"] == small

    @pytest.mark.asyncio
    async def test_no_image_parts_is_untouched(self) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        out = await downscale_images(msgs)
        assert out is msgs

    @pytest.mark.asyncio
    async def test_remote_url_fetch_failure_fails_open(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        url = "https://example.com/big.png"
        msgs = _image_msg(url)

        async def _fail(fetch_url: str) -> None:
            return None

        monkeypatch.setattr("image_downscale._fetch_remote", _fail)
        out = await downscale_images(msgs)
        assert out is msgs
        assert msgs[0]["content"][1]["image_url"]["url"] == url

    @pytest.mark.asyncio
    async def test_loopback_url_rejected_fail_open(self) -> None:
        """SSRF guard (R1/R4-001): 127.0.0.1 must never be fetched."""
        url = "http://127.0.0.1:13109/secret"
        assert await _fetch_remote(url) is None

    @pytest.mark.asyncio
    async def test_private_and_link_local_urls_rejected(self) -> None:
        """SSRF guard: private ranges and the cloud-metadata address are out."""
        for url in (
            "http://192.168.1.1/x",
            "http://10.0.0.1/x",
            "http://172.16.0.1/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/x",
        ):
            assert await _fetch_remote(url) is None, url

    @pytest.mark.asyncio
    async def test_hostname_resolving_to_private_rejected(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SSRF guard: DNS resolving to a private address is rejected."""

        class _PrivateLoop:
            async def getaddrinfo(self, host: str, port: int) -> list[tuple[Any, Any, Any, Any, tuple[str, int]]]:
                return [(2, 1, 6, "", ("10.0.0.5", port))]

        monkeypatch.setattr("image_downscale.asyncio.get_running_loop", lambda: _PrivateLoop())
        assert await _fetch_remote("http://evil.example/x") is None

    @pytest.mark.asyncio
    async def test_hostname_resolving_to_public_allowed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Public destinations still fetch (validator passes them)."""

        class _PublicLoop:
            async def getaddrinfo(self, host: str, port: int) -> list[tuple[Any, Any, Any, Any, tuple[str, int]]]:
                return [(2, 1, 6, "", ("93.184.216.34", port))]

        async def _public_fetch(url: str) -> Optional[bytes]:
            return b"fake-image"

        monkeypatch.setattr("image_downscale.asyncio.get_running_loop", lambda: _PublicLoop())
        monkeypatch.setattr("image_downscale._fetch_remote", _public_fetch)
        out = await downscale_images(_image_msg("https://example.com/big.png"))
        assert out is not _image_msg("https://example.com/big.png")

    @pytest.mark.asyncio
    async def test_fetch_remote_does_not_follow_redirects(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 3xx must not be followed (fail-open) — a redirect could hop
        to a private destination after the public check (SSRF guard)."""

        class _PublicLoop:
            async def getaddrinfo(self, host: str, port: int) -> list[tuple[Any, Any, Any, Any, tuple[str, int]]]:
                return [(2, 1, 6, "", ("93.184.216.34", port))]

        captured: dict[str, Any] = {}

        class _Resp:
            status_code = 302

            async def __aenter__(self) -> "_Resp":
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

            async def aiter_bytes(self) -> Any:
                yield b""

        class _StubClient:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def __aenter__(self) -> "_StubClient":
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

            def stream(self, method: str, url: str) -> Any:
                return _Resp()

        monkeypatch.setattr("image_downscale.asyncio.get_running_loop", lambda: _PublicLoop())
        monkeypatch.setattr(
            "image_downscale.httpx",
            types.SimpleNamespace(
                AsyncClient=_StubClient,
                Timeout=lambda s: s,
                HTTPError=httpx.HTTPError,
            ),
        )
        assert await _fetch_remote("https://example.com/redirect") is None
        assert captured.get("follow_redirects") is False

    @pytest.mark.asyncio
    async def test_remote_url_success_downscaled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        url = "https://example.com/big.png"
        msgs = _image_msg(url)
        oversized = _png_b64(64, 3200)

        async def _fetch(fetch_url: str) -> bytes:
            _, _, b64 = oversized.partition(",")
            return base64.b64decode(b64)

        monkeypatch.setattr("image_downscale._fetch_remote", _fetch)
        out = await downscale_images(msgs)
        assert out is not msgs
        out_url = out[0]["content"][1]["image_url"]["url"]
        assert out_url.startswith("data:image/jpeg;base64,")
        width, height = _url_dimensions(out_url)
        assert max(width, height) <= 2048


class TestGovernMessagesWiring:
    """The downscale runs from ``routes._govern_messages`` on the outbound
    copy, default-on, with ``X-Proxy-Image-Downscale: off`` as the opt-out."""

    async def _govern(
        self, msgs: list[dict[str, Any]], headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        request = types.SimpleNamespace(headers=headers)
        return await routes._govern_messages(
            request, msgs, model_key="professional", max_tokens=4096,
        )

    @pytest.mark.asyncio
    async def test_downscale_runs_by_default(self) -> None:
        original = _png_b64(64, 3200)
        msgs = _image_msg(original)
        out = await self._govern(msgs, {})
        url = out[1]["content"][1]["image_url"]["url"]  # out[0] is the date stamp
        assert url != original
        assert max(_url_dimensions(url)) <= 2048
        # client copy untouched
        assert msgs[0]["content"][1]["image_url"]["url"] == original

    @pytest.mark.asyncio
    async def test_opt_out_header_disables_downscale(self) -> None:
        original = _png_b64(64, 3200)
        msgs = _image_msg(original)
        out = await self._govern(msgs, {"x-proxy-image-downscale": "off"})
        assert out[1]["content"][1]["image_url"]["url"] == original


class _StreamCapture:
    """Async-generator stand-in for ``stream_llm`` that records its payload."""

    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    async def __call__(
        self,
        *,
        endpoint: str,
        payload: dict[str, Any] | None,
        port: int = 0,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.payload = payload
        yield {
            "choices": [
                {"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"},
            ],
        }


@pytest_asyncio.fixture
async def image_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Real app with state stubbed; the coding gate passes through."""
    from tests.conftest import _NoOpCooling, _NoOpDatabase, _NoOpSystemd

    monkeypatch.setattr(
        "routes._apply_coding_decision_gate",
        AsyncMock(return_value=None),
    )
    proxy.app.state.database = _NoOpDatabase()
    proxy.app.state.systemd = _NoOpSystemd()
    proxy.app.state.cooler = _NoOpCooling()
    proxy.app.state.hardware = None
    proxy.app.state.active_heavy_model = None
    proxy.app.state.active_priority = 3
    proxy.app.state.requests_served = 0
    proxy.app.state.model_profiles = None

    transport = httpx.ASGITransport(app=proxy.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _outbound_image_urls(messages: list[dict[str, Any]]) -> list[str]:
    return [
        part["image_url"]["url"]
        for m in messages
        if isinstance(m.get("content"), list)
        for part in m["content"]
        if isinstance(part, dict)
        and part.get("type") == "image_url"
        and isinstance(part.get("image_url"), dict)
        and isinstance(part["image_url"].get("url"), str)
    ]


class TestWire:
    """End-to-end: an image request's OUTBOUND payload is downscaled."""

    @pytest.mark.asyncio
    async def test_image_request_downscaled_outbound(self, image_client: Any) -> None:
        original = _png_b64(64, 3200)
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture):
            response = await image_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what is this image"},
                            {"type": "image_url", "image_url": {"url": original}},
                        ],
                    }],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        urls = _outbound_image_urls(capture.payload["messages"])
        assert urls, "outbound payload must carry the image"
        assert urls[0] != original
        assert max(_url_dimensions(urls[0])) <= 2048

    @pytest.mark.asyncio
    async def test_opt_out_header_preserves_original(self, image_client: Any) -> None:
        original = _png_b64(64, 3200)
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture):
            response = await image_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what is this image"},
                            {"type": "image_url", "image_url": {"url": original}},
                        ],
                    }],
                },
                headers={
                    "Authorization": "Bearer agent-key",
                    "X-Proxy-Image-Downscale": "off",
                },
            )
            await response.aread()

        assert response.status_code == 200, response.text
        urls = _outbound_image_urls(capture.payload["messages"])
        assert urls
        assert urls[0] == original
