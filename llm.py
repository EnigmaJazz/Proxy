"""
llm.py - Asynchronous LLM inference client for the Kinver AI Proxy.

Handles:
- OpenAI-compatible streaming chat completions (SSE via httpx)
- Non-streaming convenience wrapper (``call_llm``)
- Token counting & estimation
- Multi-provider failover (local Systemd → OpenRouter cloud)
- Transparent provider metadata injection
- Legacy completions endpoint (``call_model``) for CPU-bound classifiers

IMPORTANT DESIGN RULE (Glass Pipe Rule):
    This module MUST NOT inject, alter, or sanitize the text content of
    system prompts or user messages sent by external frontends.
    Prompting is strictly the responsibility of the client software.

    The proxy may ONLY generate prompts for its own internal routing tasks
    (e.g. the 2B Front Desk classification or emergency Loop-Breaker
    system interventions).  Role prompts loaded via ``load_role_prompt``
    are restricted to proxy-internal call sites.

All network calls are async (httpx) and designed to run under uvloop.

Usage::

    from llm import stream_llm, call_llm

    async for chunk in stream_llm("worker", payload):
        yield chunk

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any, AsyncIterator, List

import httpx

from constants import (
    PROJECT_ROOT,
    OPENROUTER_API_KEY,
    OPENROUTER_SITE_URL,
    OPENROUTER_SITE_NAME,
    PROMPTS_DIR,
    STOP_SEQS,
    MAX_RETRIES,
    RETRY_DELAY,
    REQUEST_TIMEOUT,
    LLAMA_ENDPOINTS,
    CoolingPreset,
    get_logger,
)

logger = get_logger("proxy.llm")

# ---------------------------------------------------------------------------
# Utility: fast regex for token-count estimation
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")


def estimate_tokens(text: str) -> int:
    """
    Rough token-count heuristic (~1 token per word/punctuation).
    Used for context-window budgeting, not for billing.
    """
    return len(_TOKEN_RE.findall(text))


# ---------------------------------------------------------------------------
# Role prompt loading (PROXY-INTERNAL USE ONLY)
#
#   These prompts MUST NOT be injected into frontend-initiated requests.
#   They are reserved for proxy-internal classifiers (frontdesk,
#   loop-breaker) that the proxy controls entirely.
# ---------------------------------------------------------------------------

def load_role_prompt(role_name: str) -> str:
    """
    Load a role prompt from disk for proxy-internal use.

    Reads ``~/kinver-hub/prompts/{role_name}.txt``.
    Returns a generic fallback string if the file is not found.

    IMPORTANT: This function is restricted to proxy-internal call sites.
    Do NOT use it to modify frontend-supplied messages.
    """
    try:
        prompt_path = os.path.join(PROMPTS_DIR, f"{role_name}.txt")
        with open(prompt_path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return f"You are the {role_name} AI."


# ---------------------------------------------------------------------------
# Core async generator: streams SSE chunks from an LLM endpoint
# ---------------------------------------------------------------------------

async def stream_llm(
    endpoint: str,
    payload: Dict[str, Any],
    api_key: str = "",
    api_url: str = "",
    port: int = 0,
    site_url: str = "",
    site_name: str = "",
    model_name: str = "",
    headers: Optional[Dict[str, str]] = None,
    timeout: float = REQUEST_TIMEOUT,
    set_cooling=None,  # Optional async callback(chip, CoolingPreset)
) -> AsyncIterator[Dict[str, Any]]:
    """
    Yield parsed JSON chunks from an SSE streaming endpoint.

    Parameters
    ----------
    endpoint : str
        Logical name (e.g. ``"worker"``, ``"cloud"``).  Used only for
        the hard-coded fallback URL when neither ``api_url`` nor ``port``
        is provided.
    payload : dict
        Full JSON body for the ``/chat/completions`` request.  The
        ``"stream": true`` field is injected automatically.
    api_key : str
        OpenRouter API key override (used when ``endpoint == "cloud"``).
    api_url : str
        Full URL override.  If set, takes priority over everything else.
    port : int
        TCP port of the local llama.cpp server.  When ``> 0``, the URL is
        constructed as ``http://127.0.0.1:{port}/v1/chat/completions``
        and the hard-coded ``LLAMA_ENDPOINTS`` dict is bypassed entirely.
        This is the RECOMMENDED path — it guarantees the port matches the
        live systemd unit file.
    site_url : str
        OpenRouter HTTP-Referer override.
    site_name : str
        OpenRouter X-Title override.
    model_name : str
        Override the ``model`` field in the payload.
    headers : dict or None
        Extra HTTP headers (used for IDE passthrough auth).
    timeout : float
        Total request timeout in seconds (default 300s for long generations).
    set_cooling : callable or None
        Optional async callback ``(chip: str, preset: CoolingPreset)``
        invoked on the first valid chunk to step down cooling from
        PREFILL to GENERATION.

    Yields
    ------
    dict
        Parsed JSON chunk from the SSE stream (one per ``data:`` line).

    Raises
    ------
    httpx.HTTPStatusError
        If the endpoint returns a non-200 status.
    httpx.TimeoutException / ConnectError / RemoteProtocolError
        After all retries are exhausted.
    """
    # Port-based URL takes priority over the hard-coded dict so that the
    # live systemd-resolved port is always the source of truth.  Falls
    # back to LLAMA_ENDPOINTS only when neither api_url nor port is given.
    if api_url:
        url = api_url
    elif port > 0:
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
    else:
        url = LLAMA_ENDPOINTS.get(endpoint, LLAMA_ENDPOINTS["cloud"])

    # ---- Build request body -----------------------------------------------
    body = {**payload}
    body["stream"] = True
    if model_name:
        body["model"] = model_name

    # ---- Build headers -----------------------------------------------------
    http_headers: Dict[str, str] = {"Content-Type": "application/json"}
    if headers:
        http_headers.update(headers)
    if endpoint == "cloud":
        http_headers.update({
            "Authorization": f"Bearer {api_key or OPENROUTER_API_KEY}",
            "HTTP-Referer": site_url or OPENROUTER_SITE_URL,
            "X-Title": site_name or OPENROUTER_SITE_NAME,
        })

    logger.debug(
        "stream_llm → %s  model=%s  body_keys=%s",
        url, body.get("model"), list(body.keys()),
    )

    # ---- Retry loop --------------------------------------------------------
    last_exc: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    json=body,
                    headers=http_headers,
                ) as resp:
                    if resp.status_code != 200:
                        err_text = await resp.aread()
                        logger.warning(
                            "stream_llm HTTP %s (attempt %d): %s",
                            resp.status_code, attempt, err_text[:500],
                        )
                        raise httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}: {err_text[:200]}",
                            request=resp.request,
                            response=resp,
                        )

                    first_chunk_seen = False
                    async for raw_line in resp.aiter_lines():
                        if not raw_line or not raw_line.startswith("data:"):
                            continue

                        data_str = raw_line[5:].strip()
                        if data_str == "[DONE]":
                            return

                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            logger.debug(
                                "Unparseable SSE line: %s", data_str[:100],
                            )
                            continue

                        # ---- Cooling callback on first valid chunk -----------
                        if not first_chunk_seen and set_cooling:
                            first_chunk_seen = True
                            # Determine hardware path from endpoint
                            if endpoint == "cloud":
                                pass  # No local cooling for cloud
                            elif endpoint == "frontdesk":
                                # CPU-bound models
                                await set_cooling("cpu", CoolingPreset.GENERATION)
                            else:
                                # GPU models
                                await set_cooling("gpu", CoolingPreset.GENERATION)

                        yield chunk

            # Success — exit retry loop
            return

        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.HTTPStatusError,
        ) as exc:
            last_exc = exc
            logger.warning(
                "stream_llm attempt %d/%d failed: %s",
                attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)

        except Exception:
            logger.exception("stream_llm unexpected error on attempt %d", attempt)
            raise

    # All retries exhausted
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Non-streaming convenience wrapper
# ---------------------------------------------------------------------------

async def call_llm(
    endpoint: str,
    payload: Dict[str, Any],
    api_key: str = "",
    api_url: str = "",
    port: int = 0,
    site_url: str = "",
    site_name: str = "",
    model_name: str = "",
    headers: Optional[Dict[str, str]] = None,
    timeout: float = REQUEST_TIMEOUT,
) -> dict:
    """
    Convenience wrapper that collects the full stream into a single
    OpenAI-compatible ``choices[0].message.content`` dict.

    Uses ``stream_llm`` internally and concatenates all delta content.
    """
    full_content: list[str] = []
    finish_reason = "stop"
    model = model_name or "local"
    usage = {}

    async for chunk in stream_llm(
        endpoint=endpoint,
        payload=payload,
        api_key=api_key,
        api_url=api_url,
        port=port,
        site_url=site_url,
        site_name=site_name,
        model_name=model_name,
        headers=headers,
        timeout=timeout,
    ):
        choices = chunk.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                full_content.append(content)
            if choices[0].get("finish_reason"):
                finish_reason = choices[0]["finish_reason"]
        if "usage" in chunk:
            usage = chunk["usage"]
        model = chunk.get("model", model)

    return {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "".join(full_content),
            },
            "finish_reason": finish_reason,
        }],
        "model": model,
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# Provider metadata injection (transparent annotation of chunks)
# ---------------------------------------------------------------------------

def _inject_provider_metadata(
    chunk: Dict[str, Any],
    provider: str,
    model: str,
    fallback_used: bool = False,
) -> None:
    """
    Annotate a chunk dict with ``_kinver_provider`` metadata for
    downstream observability and client-side logging.

    This is transparent — it does not alter the OpenAI-compatible fields
    in the chunk.
    """
    chunk["_kinver_provider"] = {
        "provider": provider,
        "model": model,
        "fallback": fallback_used,
    }


# ---------------------------------------------------------------------------
# Legacy completions endpoint (CPU-bound proxy-internal classifiers)
# ---------------------------------------------------------------------------

async def call_model(
    port: int,
    prompt: str,
    profile: str = "analytical",
    max_tokens: int = 2048,
) -> str:
    """
    Legacy completions endpoint for utility AI (frontdesk).
    Strictly forces ``thinking_budget_tokens=0`` to prevent reasoning
    overhead on CPU-bound classifiers.

    Parameters
    ----------
    port : int
        TCP port of the llama.cpp server.
    prompt : str
        The raw prompt text (may include proxy-internal role prompts).
    profile : str
        One of ``"analytical"``, ``"deterministic"``, ``"json_gbnf"``.
    max_tokens : int
        Maximum tokens to generate.

    Returns
    -------
    str
        The generated text content, or ``""`` on failure.
    """
    payload: Dict[str, Any] = {
        "prompt": prompt,
        "n_predict": max_tokens,
        "cache_prompt": True,
        "stop": STOP_SEQS,
        "thinking_budget_tokens": 0,
    }

    if profile == "deterministic":
        payload.update({"temperature": 0.0})
    elif profile == "json_gbnf":
        payload.update({
            "temperature": 0.0,
            "json_schema": {
                "type": "object",
                "properties": {
                    "is_valid": {"type": "boolean"},
                    "intent": {"type": "string"},
                    "priority": {"type": "string"},
                    "complexity": {"type": "string"},
                    "project_name": {"type": "string"},
                    "is_factual": {"type": "boolean"},
                    "tools_required": {"type": "boolean"},
                },
            },
        })
    else:
        payload.update({"temperature": 0.2, "top_p": 1.0})

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"http://127.0.0.1:{port}/completion",
                json=payload,
                timeout=300,
            )
            return response.json().get("content", "")
        except Exception:
            logger.exception("call_model failed on port %d", port)
            return ""


# ---------------------------------------------------------------------------
# Cloud escalation (OpenRouter failover)
# ---------------------------------------------------------------------------

async def openrouter_cloud_escalation(
    stage: int,
    prompt: str,
    model: str = "anthropic/claude-3.5-sonnet",
) -> str:
    """
    Fallback network request for cloud escalation when local compute
    is exhausted.

    Parameters
    ----------
    stage : int
        Escalation stage (informational, passed through to logging).
    prompt : str
        The user prompt to send.
    model : str
        OpenRouter model identifier.

    Returns
    -------
    str
        The generated response text, or an error string on failure.
    """
    if not OPENROUTER_API_KEY:
        return "[Cloud Escalation Failed: OPENROUTER_API_KEY not found.]"

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "http://localhost:13000",
        "X-Title": "Local Proxy",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a cloud escalation AI."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60.0,
            )
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"]
            return f"[Cloud Escalation API Error: {response.status_code}]"
        except Exception as exc:
            return f"[Cloud Escalation Network Error: {str(exc)}]"


# ---------------------------------------------------------------------------
# Slot cache management (NVMe persistence for llama.cpp KV caches)
# ---------------------------------------------------------------------------

async def clear_model_cache(port: int) -> None:
    """
    Wipe VRAM memory slots dynamically via the llama.cpp slots API.

    Used when switching projects to ensure the new context starts fresh.
    """
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                f"http://127.0.0.1:{port}/slots/0?action=erase",
                timeout=5,
            )
        except Exception:
            logger.debug("Failed to clear model cache on port %d (non-critical)", port)


async def manage_slot_cache(
    port: int,
    action: str,
    filename: str,
) -> None:
    """
    Save or load a KV cache slot to/from NVMe disk.

    Parameters
    ----------
    port : int
        TCP port of the llama.cpp server.
    action : str
        ``"save"`` or ``"restore"``.
    filename : str
        Cache filename (project-specific, e.g. ``"myproject_coder.bin"``).
    """
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                f"http://127.0.0.1:{port}/slots/0?action={action}",
                json={"filename": filename},
                timeout=10.0,
            )
        except Exception:
            logger.debug(
                "Slot cache %s failed on port %d (non-critical)",
                action, port,
            )


# ---------------------------------------------------------------------------
# Port readiness probe (used during model startup)
# ---------------------------------------------------------------------------

async def wait_for_port_readiness(port: int, timeout: float = 120.0) -> bool:
    """
    Poll ``http://127.0.0.1:{port}/health`` until the model responds
    with HTTP 200, or the timeout expires.

    Parameters
    ----------
    port : int
        TCP port of the llama.cpp server.
    timeout : float
        Maximum seconds to wait.

    Returns
    -------
    bool
        True if the port became ready, False on timeout.
    """
    start = time.time()
    async with httpx.AsyncClient() as client:
        while time.time() - start < timeout:
            try:
                resp = await client.get(
                    f"http://127.0.0.1:{port}/health",
                    timeout=httpx.Timeout(1.0),
                )
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False