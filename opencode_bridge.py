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
import json
import os
import re
from typing import Any, AsyncIterator, Optional

import httpx

from constants import (
    OPENCODE_AGENT,
    OPENCODE_BIN,
    OPENCODE_SERVE_TIMEOUT,
    OPENCODE_SERVE_URL,
    OPENCODE_WORKSPACE_DIR,
    get_logger,
)

logger = get_logger("kinver.opencode_bridge")

# System prompt sent to every bridge session.  The gentle-orchestrator's
# persona (AGENTS.md) defaults direct replies to Rioplatense Spanish, which
# is wrong for an API-facing bridge consumed by OpenWebUI/nanobot — pin
# English unless the user's own message is in another language.
_BRIDGE_SYSTEM_PROMPT = (
    "You are an API-backed coding assistant reached through a proxy bridge. "
    "Respond in English unless the user's message is written in another "
    "language. Keep the final answer concise and in English."
)

# Quiet period after a finished step before the bridge considers the
# agentic session complete (the final summary message follows the last
# tool step within milliseconds; a step-finish alone is not the end).
_EVENT_QUIET_TIMEOUT: float = 8.0

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
        os.makedirs(OPENCODE_WORKSPACE_DIR, exist_ok=True)
        port = OPENCODE_SERVE_URL.rsplit(":", 1)[-1]
        proc = await asyncio.create_subprocess_exec(
            OPENCODE_BIN,
            "serve",
            "--port", port,
            "--hostname", "127.0.0.1",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            cwd=OPENCODE_WORKSPACE_DIR,
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
                "system": _BRIDGE_SYSTEM_PROMPT,
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


async def opencode_chat_stream(
    user_text: str,
    *,
    agent: str = OPENCODE_AGENT,
    model_id: Optional[str] = None,
    provider_id: str = "kinver",
) -> AsyncIterator[str]:
    """Stream a task through headless opencode, yielding assistant text live.

    Uses ``prompt_async`` (no-wait send) + the ``/event`` SSE bus instead of
    the blocking message POST, so the caller sees the agent's output as it is
    generated rather than one blob after minutes.  Yields incremental text
    part content for the assistant message; returns when the session goes
    idle.  On failure yields an error string (never raises).
    """
    if not await ensure_opencode_serve():
        yield "[OpenCode Bridge Failed: opencode serve not reachable.]"
        return
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
        try:
            resp = await client.post(
                f"{OPENCODE_SERVE_URL}/session", json={}, timeout=30.0,
            )
            if resp.status_code != 200:
                yield f"[OpenCode Bridge Error: session HTTP {resp.status_code}]"
                return
            session_id = resp.json().get("id")
            if not session_id:
                yield "[OpenCode Bridge Error: session created without id]"
                return
            payload: dict[str, Any] = {
                "agent": agent,
                "system": _BRIDGE_SYSTEM_PROMPT,
                "parts": [{"type": "text", "text": user_text}],
            }
            if model_id:
                payload["model"] = {
                    "modelID": model_id,
                    "providerID": provider_id,
                    "variant": "default",
                }

            user_mids: set[str] = set()
            asst_mid: Optional[str] = None
            text_lens: dict[str, int] = {}
            tool_state: dict[str, str] = {}
            pending_done = False
            # Open the event bus BEFORE sending the message: the bus is
            # fire-and-forget (no replay), so connecting after prompt_async
            # misses the early events (user message, assistant start, first
            # reasoning/tool parts) and the stream would look empty.
            async with client.stream("GET", f"{OPENCODE_SERVE_URL}/event") as ev:
                async_resp = await client.post(
                    f"{OPENCODE_SERVE_URL}/session/{session_id}/prompt_async",
                    json=payload,
                    timeout=30.0,
                )
                if async_resp.status_code != 204:
                    yield f"[OpenCode Bridge Error: prompt HTTP {async_resp.status_code}]"
                    return

                # An agentic session can produce several assistant messages
                # (reasoning/tool step, then a final summary).  Completion =
                # a finished step followed by quiet: a step-finish sets
                # pending_done, and a timeout on the (blocking) event read
                # confirms the session went idle.  The first step-finish is
                # NOT the end (the summary message follows).
                ev_iter = ev.aiter_lines()
                while True:
                    try:
                        line = await asyncio.wait_for(
                            anext(ev_iter), timeout=_EVENT_QUIET_TIMEOUT,
                        )
                    except StopAsyncIteration:
                        return
                    except asyncio.TimeoutError:
                        if pending_done:
                            return
                        continue
                    if not line.startswith("data: "):
                        continue
                    try:
                        evt = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    props = evt.get("properties") or {}
                    if props.get("sessionID") != session_id:
                        continue
                    etype = evt.get("type")
                    if etype == "message.updated":
                        info = props.get("info") or {}
                        mid = info.get("id")
                        role = info.get("role")
                        if role == "user" and mid:
                            user_mids.add(mid)
                        elif role == "assistant" and asst_mid is None:
                            asst_mid = mid
                            if info.get("error"):
                                yield f"[OpenCode Bridge Error: {info['error']}]"
                                return
                    elif etype == "message.part.updated":
                        part = props.get("part") or {}
                        pid = part.get("id")
                        # Accept parts from ANY assistant message (an agentic
                        # session emits several: reasoning/tool step, then a
                        # final summary message); only user-message parts are
                        # excluded so the echoed prompt never streams back.
                        if part.get("messageID") in user_mids:
                            continue
                        ptype = part.get("type")
                        if ptype == "step-finish":
                            pending_done = True
                        elif ptype == "text":
                            text = str(part.get("text") or "")
                            prev = text_lens.get(pid, 0)
                            if len(text) > prev:
                                text_lens[pid] = len(text)
                                yield text[prev:]
                        elif ptype == "reasoning":
                            # Live thinking feedback (sentinel-prefixed so it
                            # is visible inline but stripped from future
                            # model copies).
                            text = str(part.get("text") or "")
                            prev = text_lens.get(pid, 0)
                            if len(text) > prev:
                                text_lens[pid] = len(text)
                                yield f"\u200b{text[prev:]}"
                        elif ptype == "tool":
                            # Tool execution feedback: show each tool the
                            # agent runs (sentinel-prefixed status).
                            call = part.get("call") or {}
                            name = str(call.get("name") or part.get("state") or "")
                            state = str(part.get("state") or "")
                            if name and state != tool_state.get(name):
                                tool_state[name] = state
                                if state == "running":
                                    yield f"\u200b🔧 {name}…"
        except (httpx.HTTPError, OSError, ValueError) as exc:
            yield f"[OpenCode Bridge Network Error: {str(exc)}]"


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
