"""
opencode_bridge.py — route requests to a headless opencode serve backend.

OpenCode exposes an HTTP API via ``opencode serve`` (session/message/part
endpoints).  This module translates a simple OpenAI-style task into that
API so frontends (OpenWebUI, nanobot) can DIRECT a request to the opencode
agent (the full agentic loop with bash/edit/read tools) instead of a local
llama model:

    POST /session                       → create a fresh session
    POST /session/:id/message           → run the agent (blocks until done)
    response.parts[].text               → collected assistant text

Used by:
    - routes.py: ``model: "opencode"`` requests and the ``/opencode``
      embedded command.
    - proxy.py: queue-worker cloud escalation when local tiers are
      exhausted (``CLOUD_ESCALATION_BACKEND = "opencode"``).

The response text is post-processed to drop proxy-status content
(sentinel-prefixed triage) that the opencode client accumulates as
assistant text.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

import httpx

from constants import (
    OPENCODE_AGENT,
    OPENCODE_BIN,
    OPENCODE_SERVE_TIMEOUT,
    OPENCODE_SERVE_URL,
    get_logger,
)

logger = get_logger("kinver.opencode_bridge")

# Sentinel-prefixed proxy status the opencode client accumulates into the
# assistant text (the proxy emits triage as the first SSE chunk).  The
# triage formats are stable (routes._build_triage_message).  Stripping the
# leading segment keeps the response clean without touching model text.
_STATUS_SEGMENT_RE = re.compile(
    r"\u200b(?:"
    r"🔍 Proxy triage:.*?on port \d+\."
    r"|🔀 Client specified.*?wins\.\)"
    r"|🔀 Client specified.*?on port \d+\."
    r")"
)


def _strip_proxy_status_text(text: str) -> str:
    """Drop proxy-status segments from a collected assistant text string."""
    if not text or "\u200b" not in text:
        return text
    return _STATUS_SEGMENT_RE.sub("", text)


async def is_opencode_serve_running() -> bool:
    """True when the headless opencode serve backend answers."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{OPENCODE_SERVE_URL}/config", timeout=5.0)
            return resp.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


async def ensure_opencode_serve() -> bool:
    """Ensure the opencode serve backend is up (spawn if missing).

    Returns True when the backend is answering.  Spawns ``opencode serve``
    as a detached child process bound to ``OPENCODE_SERVE_URL`` — the
    proxy owns its lifecycle so no systemd unit is required.  Never raises.
    """
    if await is_opencode_serve_running():
        return True
    try:
        port = OPENCODE_SERVE_URL.rsplit(":", 1)[-1]
        proc = await asyncio.create_subprocess_exec(
            OPENCODE_BIN,
            "serve",
            "--port", port,
            "--hostname", "127.0.0.1",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("Opened opencode serve (pid=%s) on %s", proc.pid, OPENCODE_SERVE_URL)
        # Wait briefly for the listener to come up.
        for _ in range(10):
            await asyncio.sleep(0.5)
            if await is_opencode_serve_running():
                return True
    except (OSError, asyncio.CancelledError) as exc:
        logger.error("Failed to spawn opencode serve: %s", exc)
    return False


async def opencode_chat(
    user_text: str,
    *,
    agent: str = OPENCODE_AGENT,
    model_id: Optional[str] = None,
    provider_id: str = "kinver",
    timeout: float = OPENCODE_SERVE_TIMEOUT,
) -> str:
    """Send a task to headless opencode and return the assistant text.

    Creates a fresh session per call, posts the message (blocking until
    the agent finishes), and concatenates the ``text`` parts.  Proxy-status
    sentinel segments are stripped from the result.  Returns an error
    string on failure (escalation-friendly, never raises).
    """
    if not await ensure_opencode_serve():
        return "[OpenCode Bridge Failed: opencode serve not reachable.]"
    async with httpx.AsyncClient() as client:
        try:
            # ---- 1. Create a fresh session --------------------------------
            resp = await client.post(
                f"{OPENCODE_SERVE_URL}/session",
                json={},
                timeout=30.0,
            )
            if resp.status_code != 200:
                return f"[OpenCode Bridge Error: session HTTP {resp.status_code}]"
            session_id = resp.json().get("id")
            if not session_id:
                return "[OpenCode Bridge Error: no session id returned.]"

            # ---- 2. Post the message (blocks until the agent finishes) ----
            payload: dict[str, Any] = {
                "agent": agent,
                "parts": [{"type": "text", "text": user_text}],
            }
            if model_id:
                payload["model"] = {
                    "modelID": model_id,
                    "providerID": provider_id,
                    "variant": "default",
                }
            resp = await client.post(
                f"{OPENCODE_SERVE_URL}/session/{session_id}/message",
                json=payload,
                timeout=timeout,
            )
            if resp.status_code != 200:
                return f"[OpenCode Bridge Error: message HTTP {resp.status_code}]"
            data = resp.json()
            parts = data.get("parts", [])
            chunks = [
                str(p.get("text") or "")
                for p in parts
                if p.get("type") == "text" and p.get("text")
            ]
            return _strip_proxy_status_text("".join(chunks)).strip() or (
                "[OpenCode Bridge Error: empty response.]"
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return f"[OpenCode Bridge Network Error: {str(exc)}]"


# ---------------------------------------------------------------------------
# Escalation (mirrors llm.openrouter_cloud_escalation)
# ---------------------------------------------------------------------------

async def opencode_escalation(stage: int, prompt: str) -> str:
    """Fallback for the queue worker when local tiers are exhausted.

    Directs the user prompt to the opencode gentle-orchestrator agent
    OpenRouter.  ``stage`` is informational (passed through to logging).
    """
    logger.info("OpenCode escalation (stage=%d): %r", stage, prompt[:200])
    return await opencode_chat(prompt, agent=OPENCODE_AGENT)
