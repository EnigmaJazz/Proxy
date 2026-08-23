"""
opencode_backends.py — transport backends for the opencode bridge.

Step 1 of the openchamber-bridge-backend design (2026-08-16): the serve
transport is extracted behind a small backend interface so a second
transport (the openchamber CLI, later steps) can slot in behind the SAME
public bridge API (``opencode_bridge.opencode_chat`` /
``opencode_chat_stream`` / ``ensure_opencode_serve``).

This module holds the interface (``OpenCodeBackend``) and the serve
implementation (``ServeBackend``): the exact HTTP calls the bridge made
inline — same URLs, same request shapes, same timeouts, same error
handling — relocated verbatim.  ``opencode_bridge.py`` routes its
message-path HTTP primitives through the module-level ``BACKEND``
instance.  Zero behavior change by construction: each method wraps ONE
serve HTTP call and the bridge call sites keep their existing
status/id/error checks byte-for-byte.

The caller's ``httpx.AsyncClient`` is passed IN rather than owned: the
streaming path deliberately shares ONE client per request for connection
reuse and its per-request timeout semantics (``httpx.Timeout(600.0,
connect=10.0)``), so a per-call client inside the backend would change
behavior.  Methods return the raw ``httpx.Response`` (or the shaped
return the relocated helper produced); call sites keep their try/except
wrappers unchanged, which preserves the exact error-handling semantics.

Step 3 of the design (2026-08-22): ``OpenChamberBackend`` — the
openchamber CLI transport behind the SAME interface.  Its methods run
openchamber CLI subprocesses through ``opencode_chamber``'s bridge-owned
daemon machinery (``ensure_openchamber_daemon`` / ``_run_cli``) and
TRANSLATE the CLI's JSON payloads into the serve's HTTP contract (status
code + ``.json()`` body) so the bridge call sites stay byte-identical.
The ``OPENCODE_BACKEND`` env selector resolves ``BACKEND``: the literal
``"openchamber"`` picks the CLI transport, anything else keeps the serve
transport (production default until the evidence gate flips it — design
§6).  The openchamber transport is the CLI subprocess (design §3.3), NOT
the daemon's HTTP/UI API (explicitly out of scope, design §8); the
daemon is spawned and owned by ``opencode_chamber.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

import opencode_chamber as chamber
from constants import OPENCHAMBER_SERVE_URL, OPENCODE_SERVE_URL, get_logger

# Same logger name as the bridge: the relocated transport code must keep
# producing byte-identical log lines (e.g. the question-reply warnings).
logger = get_logger("kinver.opencode_bridge")


class OpenCodeBackend(ABC):
    """Transport abstraction behind the opencode bridge's message path.

    The bridge's public API and its helpers delegate every serve HTTP
    primitive to a backend so a second transport (the openchamber CLI,
    later steps) can implement the same contract.  Session-oriented
    signatures follow the design doc's shape — ``create_session(directory)``,
    ``send_prompt(session_id, agent, system_prompt, user_text, model_id,
    provider_id)``, ``fetch_messages(session_id)``,
    ``session_exists(session_id)``, ``abort_session(session_id)`` —
    adjusted only as needed to cover the serve primitives 1:1 (the caller's
    client parameter, and the serve-specific permission/question/status/
    list/send-message primitives that have no openchamber analogue).
    """

    # -- Session lifecycle (the design's core five) ---------------------

    @abstractmethod
    async def create_session(
        self,
        client: httpx.AsyncClient,
        *,
        directory: str,
    ) -> httpx.Response:
        """Create a fresh backend session in ``directory``."""

    @abstractmethod
    async def send_prompt(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        agent: str,
        system_prompt: str,
        user_text: str,
        model_id: Optional[str],
        provider_id: str,
    ) -> httpx.Response:
        """Fire-and-forget prompt send (the streaming path's no-wait POST)."""

    @abstractmethod
    async def fetch_messages(
        self,
        client: httpx.AsyncClient,
        session_id: str,
    ) -> httpx.Response:
        """Fetch a session's persisted message list."""

    @abstractmethod
    async def session_exists(
        self,
        client: httpx.AsyncClient,
        session_id: str,
    ) -> httpx.Response:
        """Existence probe for a pinned session (200 = live, 404 = gone)."""

    @abstractmethod
    async def abort_session(
        self,
        client: httpx.AsyncClient,
        session_id: str,
    ) -> None:
        """Best-effort abort of one session.  Never raises."""

    # -- Serve-specific primitives (no openchamber analogue) ------------

    @abstractmethod
    async def send_message(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        agent: str,
        system_prompt: str,
        user_text: str,
        model_id: Optional[str],
        provider_id: str,
        timeout: float,
    ) -> httpx.Response:
        """Blocking message send (the blocking path's POST, runs to completion)."""

    @abstractmethod
    async def fetch_message(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        message_id: str,
    ) -> httpx.Response:
        """Fetch ONE persisted message's parts."""

    @abstractmethod
    async def fetch_session_status(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """Fetch the serve's live session-status map."""

    @abstractmethod
    async def list_sessions(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """List every session the serve currently holds (zombie sweep)."""

    @abstractmethod
    async def fetch_permissions(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """Fetch the serve's pending permission requests."""

    @abstractmethod
    async def reply_permission(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        permission_id: str,
        response: str,
    ) -> bool:
        """POST a permission decision to the serve (best-effort).

        Never raises — network errors return False (the caller proceeds
        with the pinned continuation either way).
        """

    @abstractmethod
    async def fetch_questions(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """Fetch the serve's pending question requests."""

    @abstractmethod
    async def reply_question(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        question_id: str,
        answer: str,
        question_count: int,
    ) -> bool:
        """POST the user's answer to the serve's question reply endpoint.

        Never raises — network/HTTP errors are logged and return False.
        """


def _build_send_payload(
    *,
    agent: str,
    system_prompt: str,
    user_text: str,
    model_id: Optional[str],
    provider_id: str,
) -> dict[str, Any]:
    """Build the serve message/prompt payload.

    The agent/system/parts shape the serve expects, with the optional
    model block (modelID/providerID/variant) when the client named a model.
    Relocated verbatim from the bridge's two inline construction sites so
    both send primitives share one copy.
    """
    payload: dict[str, Any] = {
        "agent": agent,
        "system": system_prompt,
        "parts": [{"type": "text", "text": user_text}],
    }
    if model_id:
        payload["model"] = {
            "modelID": model_id,
            "providerID": provider_id,
            "variant": "default",
        }
    return payload


class ServeBackend(OpenCodeBackend):
    """The ``opencode serve`` HTTP transport — today's bridge behavior.

    Every method wraps ONE serve HTTP call exactly as the bridge made it
    inline before the extraction: same URL, same request shape, same
    timeout, same error handling.  Stateless by design — session/request
    state lives in the caller's dicts and per-request ``httpx`` clients.
    """

    async def create_session(
        self,
        client: httpx.AsyncClient,
        *,
        directory: str,
    ) -> httpx.Response:
        """POST /session — create a fresh serve session in ``directory``.

        Returns the raw response; the caller checks the HTTP status and
        reads the ``id`` from the body (the id may legitimately be absent
        on a non-200).  Timeout 30s — same as the pre-extraction inline
        call.  Transport errors propagate to the caller's try/except.
        """
        return await client.post(
            f"{OPENCODE_SERVE_URL}/session",
            params={"directory": directory},
            timeout=30.0,
        )

    async def send_prompt(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        agent: str,
        system_prompt: str,
        user_text: str,
        model_id: Optional[str],
        provider_id: str,
    ) -> httpx.Response:
        """POST /session/<id>/prompt_async — the streaming path's send.

        The no-wait prompt dispatch: the agent picks the message up
        asynchronously and the serve streams its progress on the /event
        bus.  Returns the raw response (204 on accepted); the caller
        checks the status and owns the abort/error policy.  Timeout 30s —
        same as the session-create POST.
        """
        return await client.post(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/prompt_async",
            json=_build_send_payload(
                agent=agent, system_prompt=system_prompt, user_text=user_text,
                model_id=model_id, provider_id=provider_id,
            ),
            timeout=30.0,
        )

    async def fetch_messages(
        self,
        client: httpx.AsyncClient,
        session_id: str,
    ) -> httpx.Response:
        """GET /session/<id>/message — the session's persisted message list.

        The bus-independent source the polling fallback, wedge detection,
        context estimate, question retry, and running-tool announce all
        read.  Timeout 10s, same as the pre-extraction inline calls.
        Transport errors propagate to the caller's existing try/except —
        identical to the inline behavior.
        """
        return await client.get(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/message", timeout=10.0,
        )

    async def session_exists(
        self,
        client: httpx.AsyncClient,
        session_id: str,
    ) -> httpx.Response:
        """GET /session/<id> — the resume-path existence probe.

        200 = live; 404 = genuinely gone (serve recycled or session
        dropped).  NOTE: /session/status is NOT an existence probe — the
        serve's status map only holds BUSY sessions; idle sessions are
        deleted from it on completion (upstream SessionStatus.set deletes
        on idle, observed live 2026-08-13 on 1.18.18), so reading the map
        as an existence check dropped EVERY pin on follow-ups.  The caller
        distinguishes a transport error (keep the pin conservatively) from
        a 404 (drop the pin) via its own try/except around this call.
        Timeout 10s.
        """
        return await client.get(
            f"{OPENCODE_SERVE_URL}/session/{session_id}", timeout=10.0,
        )

    async def abort_session(
        self,
        client: httpx.AsyncClient,
        session_id: str,
    ) -> None:
        """POST /session/<id>/abort — best-effort abort of one session.

        The blocking escalation path has no SSE bus, so it cannot watch
        for wedged tools; when it gives up on a session (timeout, HTTP
        error, write-permission abort) it must not leave a busy zombie
        behind.  Never raises.
        """
        try:
            await client.post(
                f"{OPENCODE_SERVE_URL}/session/{session_id}/abort",
                timeout=10.0,
            )
        except (httpx.HTTPError, OSError):
            pass

    async def send_message(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        agent: str,
        system_prompt: str,
        user_text: str,
        model_id: Optional[str],
        provider_id: str,
        timeout: float,
    ) -> httpx.Response:
        """POST /session/<id>/message — the BLOCKING message send.

        The serve runs the agent to completion before answering, so the
        call may take the full ``timeout``.  Returns the raw response; the
        caller (``_opencode_chat_attempt``) owns the permission-polling
        concurrency (it runs this as an asyncio task) and the
        abort-on-error policy.  Transport errors raise exactly as they did
        inline — the caller's task.result()/except surfaces them.
        """
        return await client.post(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/message",
            json=_build_send_payload(
                agent=agent, system_prompt=system_prompt, user_text=user_text,
                model_id=model_id, provider_id=provider_id,
            ),
            timeout=timeout,
        )

    async def fetch_message(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        message_id: str,
    ) -> httpx.Response:
        """GET /session/<id>/message/<mid> — ONE persisted message's parts.

        Used by the permission handler (tool-name lookup from the part's
        ``state.input``) and the question retry-race fetch (the persisted
        multi-group input).  Timeout 10s, same as inline.
        """
        return await client.get(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/message/{message_id}",
            timeout=10.0,
        )

    async def fetch_session_status(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """GET /session/status — the serve's live session-status map.

        The map holds the ACTIVE (busy) sessions keyed by session id; the
        caller looks up its own session and decides busy/idle policy.  NOT
        an existence probe (idle sessions drop out of the map on
        completion — see ``session_exists``).  Timeout 10s.
        """
        return await client.get(
            f"{OPENCODE_SERVE_URL}/session/status", timeout=10.0,
        )

    async def list_sessions(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """GET /session — every session the serve currently holds.

        The zombie sweep reads this to find busy-but-stale sessions to
        abort.  Timeout 10s.
        """
        return await client.get(f"{OPENCODE_SERVE_URL}/session", timeout=10.0)

    async def fetch_permissions(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """GET /permission — the serve's pending permission requests.

        The polling fallback cannot see ``permission.updated`` SSE events
        (the bus is closed), but the serve exposes pending requests here.
        The caller filters by session id and relayed-permission type.
        Timeout 10s.
        """
        return await client.get(f"{OPENCODE_SERVE_URL}/permission", timeout=10.0)

    async def reply_permission(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        permission_id: str,
        response: str,
    ) -> bool:
        """POST a permission decision to the opencode serve (best-effort).

        Returns True when the serve accepted the response; False on network
        errors (the caller proceeds with the pinned continuation either way).
        Never raises.
        """
        try:
            resp = await client.post(
                f"{OPENCODE_SERVE_URL}/session/{session_id}/permissions/{permission_id}",
                json={"response": response},
                timeout=10.0,
            )
            return resp.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    async def fetch_questions(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """GET /question — the serve's pending question requests.

        The agent's question tool parks the session's current step until
        the user answers (POST /question/<id>/reply).  The serve keeps the
        pending requests in memory and lists them here; the entry carries
        the request id needed for the reply.  Timeout 10s.
        """
        return await client.get(f"{OPENCODE_SERVE_URL}/question", timeout=10.0)

    async def reply_question(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        question_id: str,
        answer: str,
        question_count: int,
    ) -> bool:
        """POST the user's answer to the serve's question reply endpoint.

        The question tool completes with the answer and the agent continues
        with it in context.  Returns True when the serve accepted the reply.
        """
        try:
            resp = await client.post(
                f"{OPENCODE_SERVE_URL}/question/{question_id}/reply",
                json={"answers": [[answer] for _ in range(max(1, question_count))]},
                timeout=15.0,
            )
            if 200 <= resp.status_code < 300:
                logger.info(
                    "Answered pending question %s for session %s",
                    question_id[:16], session_id[:16],
                )
                return True
            logger.warning(
                "Question reply %s -> HTTP %s (session %s)",
                question_id[:16], resp.status_code, session_id[:16],
            )
        except (httpx.HTTPError, OSError, ValueError):
            logger.warning(
                "Question reply failed for session %s", session_id[:16],
                exc_info=True,
            )
        return False


# -- OpenChamber CLI transport (design step 3, 2026-08-22) ------------------

#: Per-create-call CLI timeout: covers the CLI's own 4s control window
#: (the first call after daemon boot can hit it while the managed
#: opencode spawns — prototype-proven 2026-08-15) plus margin.
_CREATE_TIMEOUT_S = 15.0
#: Max create attempts on the first-call control timeout (prototype: 4x).
_CREATE_RETRIES = 4
#: Sleep between create retries (the managed runtime spawns in this window).
_CREATE_RETRY_SLEEP_S = 1.0
#: The send dispatch's ``--timeout`` (the CLI returns BEFORE the turn
#: completes — dispatch semantics, not completion).
_DISPATCH_TIMEOUT_S = 30.0
#: CLI process timeout for the dispatch call (covers the control window).
_DISPATCH_PROC_TIMEOUT_S = 45.0
#: CLI timeout for messages/list fetches.
_MESSAGES_TIMEOUT_S = 15.0
#: Completion-poll interval (design §3.3: poll every ~3 s).
_POLL_INTERVAL_S = 3.0


def _json_response(data: Any, status_code: int) -> httpx.Response:
    """Synthesize a serve-contract ``httpx.Response`` from CLI output.

    The bridge call sites consume ``status_code`` + ``.json()`` only; the
    synthesized body carries the serve shape the caller expects.
    """
    return httpx.Response(
        status_code,
        content=json.dumps(data).encode("utf-8"),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", OPENCHAMBER_SERVE_URL),
    )


def _parse_json_payload(out: str) -> Any:
    """Best-effort JSON parse of CLI stdout; None when the body is not JSON."""
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return None


def _session_id_from_payload(out: str) -> Optional[str]:
    """Extract the created session id from ``session create --json`` output.

    The CLI payload carries ``sessionId`` (fallbacks: ``id``,
    ``session.id`` — the prototype reads the same trio); a non-JSON body
    (older CLI / plain output) falls back to a ``ses_...`` regex.
    """
    data = _parse_json_payload(out)
    if isinstance(data, dict):
        sid = data.get("sessionId") or data.get("id")
        if not sid and isinstance(data.get("session"), dict):
            sid = data["session"].get("id")
        if isinstance(sid, str) and sid:
            return sid
    m = re.search(r"ses_[A-Za-z0-9]+", out)
    return m.group(0) if m else None


def _normalize_messages(payload: Any) -> list[dict[str, Any]]:
    """Translate the openchamber CLI message payload into the serve shape.

    ``session messages --json`` returns an OBJECT whose ``messages`` list
    holds plain-text messages (``{"id", "role", "text"}`` — prototype raw
    captures, 2026-08-15).  The bridge consumes the serve's shape
    (``{"info": {"role"}, "parts": [{"type": "text", "text"}]}``), so each
    message maps: ``role`` → ``info.role``, the plain ``text`` field →
    one text part.  Typed parts (``{"type": ...}``) pass through unchanged
    for forward compatibility with CLI versions that emit them.
    """
    if isinstance(payload, dict):
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            payload = msgs
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for m in payload:
        if not isinstance(m, dict):
            continue
        info = m.get("info") if isinstance(m.get("info"), dict) else {}
        if not info and isinstance(m.get("role"), str):
            info = {"role": m["role"]}
        parts = m.get("parts")
        if not isinstance(parts, list):
            parts = []
        norm_parts: list[dict[str, Any]] = []
        for p in parts:
            if isinstance(p, str):
                norm_parts.append({"type": "text", "text": p})
            elif isinstance(p, dict):
                if "type" not in p and "text" in p:
                    norm_parts.append({"type": "text", **p})
                else:
                    norm_parts.append(p)
        if not norm_parts and isinstance(m.get("text"), str) and m["text"]:
            norm_parts.append({"type": "text", "text": m["text"]})
        entry: dict[str, Any] = {"info": info, "parts": norm_parts}
        if isinstance(m.get("id"), str) and m["id"]:
            entry["id"] = m["id"]
        out.append(entry)
    return out


def _assistant_text_parts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The NEWEST assistant message's text parts (the blocking reply)."""
    for m in reversed(messages):
        if (m.get("info") or {}).get("role") != "assistant":
            continue
        parts = [p for p in (m.get("parts") or [])
                 if p.get("type") == "text" and p.get("text")]
        if parts:
            return parts
    return []


def _list_entries(payload: Any) -> list[dict[str, Any]]:
    """Session entries from a ``session list --json`` payload.

    Tolerates a bare list and an object with a ``sessions``/``items``
    key (the exact CLI shape is unverified — the daemon-death probe only
    needs rc and the zombie sweep only needs ids).
    """
    if isinstance(payload, dict):
        for key in ("sessions", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return []
    return [e for e in payload if isinstance(e, dict)]


class OpenChamberBackend(OpenCodeBackend):
    """The ``openchamber`` CLI transport (design 2026-08-15, step 3).

    Every transport method runs ONE openchamber CLI subprocess in the
    bridge's isolated XDG env (``opencode_chamber._run_cli``) against the
    bridge-owned daemon, after ``ensure_openchamber_daemon`` (spawn on
    demand, config-drift recycle, hollow-session gate).  CLI payloads are
    TRANSLATED into the serve's HTTP contract (``httpx.Response`` with
    status code + ``.json()`` body) so the bridge call sites work
    unchanged.  The transport is the CLI subprocess — NOT the daemon's
    HTTP/UI API (design §8: out of scope).

    Command mapping (design §3.3, prototype 2026-08-15):
      create    ``session create --dir <dir> --json``
                — retry up to 4x on the first-call control timeout
      send      ``session send --session <id> --dir <dir> --prompt <text>
                --wait --timeout N --json`` — a DISPATCH: the CLI returns
                before the turn completes (prototype: the session shows
                idle instantly), so completion is the caller's poll of
                fetch_messages
      messages  ``session messages --session <id> --dir <dir> --json
                --limit 5``
      list      ``session list --json``

    The ``agent``/``system_prompt``/``model_id``/``provider_id``
    parameters are the serve payload's; the CLI's agent + model come
    from the daemon config (patched at spawn to pin gentle-orchestrator
    to kinver/professional) and the agent's own system prompt, so they
    are intentionally NOT forwarded.

    Serve-specific primitives with no openchamber analogue (design
    §3.4/§3.5) map to best-effort defaults with logging:
      permissions — no surface (the pre-allow block handles tools):
        fetch_permissions → [], reply_permission → True.
      questions — no registry/reply endpoint; a question part is
        answered by a NEW session send (design §3.4): fetch_questions →
        [], reply_question → True.
      session-status map — every listed session reports idle (the
        chamber transport has no busy-forever defect family to detect;
        the poll loop's own timeout governs wedges).
      abort_session — the CLI has no abort command (none in the design
        or prototype): logged no-op; wedged sessions clear on the
        daemon's next recycle (config drift / death respawn).

    F8 (2026-08-22): this class holds ONE piece of mutable instance
    state — ``_session_dirs``, a session_id → directory map — because
    the CLI needs ``--dir`` on EVERY call while the interface signatures
    only carry the directory at ``create_session``.  Bounded (only
    sessions this process created), per-instance, and it dies with the
    process; ``ServeBackend`` (the default) remains stateless.
    """

    def __init__(self) -> None:
        self._session_dirs: dict[str, str] = {}

    async def _ensure_daemon(self) -> None:
        """Ensure the bridge-owned daemon is up before CLI use.

        ``ensure_openchamber_daemon`` spawns on demand, recycles on
        config drift, and gates hollow sessions; a failed ensure is a
        transport error — the bridge's try/except wrappers treat it as a
        network-class failure (resilient retry re-ensures).
        """
        ok, status = await chamber.ensure_openchamber_daemon()
        if not ok:
            raise httpx.ConnectError(
                f"openchamber daemon unavailable ({status})",
            )

    async def create_session(
        self,
        client: httpx.AsyncClient,
        *,
        directory: str,
    ) -> httpx.Response:
        """``session create --dir <dir> --json`` with the control-timeout
        retry (up to ``_CREATE_RETRIES`` attempts — prototype discipline).

        Returns 200 with the serve-shaped ``{"id": ...}`` body, or 500
        after exhausted retries (the caller surfaces the session HTTP
        error).  ``client`` is unused — the transport is a subprocess.
        """
        await self._ensure_daemon()
        last_out = ""
        for attempt in range(_CREATE_RETRIES):
            rc, out = await chamber._run_cli(
                ["session", "create", "--dir", directory, "--json"],
                timeout=_CREATE_TIMEOUT_S,
            )
            if rc == 0:
                sid = _session_id_from_payload(out)
                if sid:
                    self._session_dirs[sid] = directory
                    return _json_response({"id": sid}, 200)
            last_out = out
            if attempt < _CREATE_RETRIES - 1:
                await asyncio.sleep(_CREATE_RETRY_SLEEP_S)
        logger.warning(
            "openchamber session create failed after %d attempts: %s",
            _CREATE_RETRIES, last_out[:200],
        )
        return _json_response({"error": "session create failed"}, 500)

    async def send_prompt(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        agent: str,
        system_prompt: str,
        user_text: str,
        model_id: Optional[str],
        provider_id: str,
    ) -> httpx.Response:
        """The streaming path's no-wait dispatch: ``session send --wait``
        returns BEFORE the turn completes (dispatch semantics —
        prototype-proven), so rc 0 maps to the serve's 204-accepted.
        Completion is the caller's fetch_messages poll.  A CLI failure
        raises (the caller's try/except yields a network error and the
        resilient wrapper retries).
        """
        await self._ensure_daemon()
        directory = self._session_dirs.get(session_id)
        if not directory:
            raise httpx.ConnectError(
                f"no directory recorded for session {session_id[:16]}",
            )
        rc, _ = await chamber._run_cli(
            [
                "session", "send",
                "--session", session_id,
                "--dir", directory,
                "--prompt", user_text,
                "--wait",
                "--timeout", str(int(_DISPATCH_TIMEOUT_S)),
                "--json",
            ],
            timeout=_DISPATCH_PROC_TIMEOUT_S,
        )
        if rc != 0:
            raise httpx.ConnectError(
                f"openchamber CLI send failed (rc={rc})",
            )
        return _json_response({}, 204)

    async def fetch_messages(
        self,
        client: httpx.AsyncClient,
        session_id: str,
    ) -> httpx.Response:
        """``session messages --json --limit 5``, translated into the
        serve's message-list shape (see ``_normalize_messages``).  A CLI
        failure raises — the design's transport-error class (callers keep
        pins conservatively / the resilient wrapper retries).
        """
        await self._ensure_daemon()
        directory = self._session_dirs.get(session_id)
        if not directory:
            raise httpx.ConnectError(
                f"no directory recorded for session {session_id[:16]}",
            )
        rc, out = await chamber._run_cli(
            [
                "session", "messages",
                "--session", session_id,
                "--dir", directory,
                "--json",
                "--limit", "5",
            ],
            timeout=_MESSAGES_TIMEOUT_S,
        )
        if rc != 0:
            raise httpx.ConnectError(
                f"openchamber CLI messages failed (rc={rc})",
            )
        return _json_response(
            _normalize_messages(_parse_json_payload(out)), 200,
        )

    async def session_exists(
        self,
        client: httpx.AsyncClient,
        session_id: str,
    ) -> httpx.Response:
        """Existence probe with the serve's discipline: 200 = live, 404 =
        genuinely gone, transport error = keep the pin conservatively.

        The daemon-health gate maps daemon death to a transport error
        (keep the pin — a dead daemon says nothing about the session); a
        HEALTHY daemon whose CLI rejects the session means it is gone
        (404 — drop the pin), mirroring the serve's 404 semantics.
        """
        if not await asyncio.to_thread(chamber.is_openchamber_daemon_running):
            raise httpx.ConnectError("openchamber daemon not reachable")
        directory = self._session_dirs.get(session_id)
        if not directory:
            raise httpx.ConnectError(
                f"no directory recorded for session {session_id[:16]}",
            )
        rc, _ = await chamber._run_cli(
            [
                "session", "messages",
                "--session", session_id,
                "--dir", directory,
                "--json",
                "--limit", "5",
            ],
            timeout=_MESSAGES_TIMEOUT_S,
        )
        if rc == 0:
            return _json_response({}, 200)
        logger.info(
            "openchamber session %s no longer exists (CLI rc=%s)",
            session_id[:16], rc,
        )
        return _json_response({}, 404)

    async def abort_session(
        self,
        client: httpx.AsyncClient,
        session_id: str,
    ) -> None:
        """Best-effort abort: the CLI has NO abort command (the design
        and the 2026-08-15 prototype list none), so this is a logged
        no-op.  Wedged sessions clear on the daemon's next recycle
        (config-drift or death respawn).  Never raises.
        """
        logger.warning(
            "openchamber abort_session: no CLI abort command (design "
            "2026-08-15) — session %s left to the daemon recycle",
            session_id[:16],
        )

    async def send_message(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        agent: str,
        system_prompt: str,
        user_text: str,
        model_id: Optional[str],
        provider_id: str,
        timeout: float,
    ) -> httpx.Response:
        """The blocking path's send: dispatch ``session send --wait`` then
        POLL ``session messages`` every ~3 s until assistant text appears
        (the CLI returns before the turn completes — prototype) or the
        caller's ``timeout`` budget expires.

        Returns 200 with the serve-shaped ``{"parts": [text parts of the
        newest assistant message]}`` body; an expired budget returns empty
        parts (the caller surfaces "[OpenCode Bridge Error: empty
        response.]").  A dead daemon mid-poll raises (network-class →
        retry); a transient CLI blip with a live daemon keeps polling.
        Runs as an asyncio task in the caller — stays cancellation-clean
        (``_run_cli`` does not swallow CancelledError).
        """
        await self._ensure_daemon()
        directory = self._session_dirs.get(session_id)
        if not directory:
            raise httpx.ConnectError(
                f"no directory recorded for session {session_id[:16]}",
            )
        deadline = time.monotonic() + timeout
        rc, _ = await chamber._run_cli(
            [
                "session", "send",
                "--session", session_id,
                "--dir", directory,
                "--prompt", user_text,
                "--wait",
                "--timeout", str(int(timeout)),
                "--json",
            ],
            timeout=timeout + 30.0,
        )
        if rc != 0:
            raise httpx.ConnectError(
                f"openchamber CLI send failed (rc={rc})",
            )
        while True:
            rc, out = await chamber._run_cli(
                [
                    "session", "messages",
                    "--session", session_id,
                    "--dir", directory,
                    "--json",
                    "--limit", "5",
                ],
                timeout=_MESSAGES_TIMEOUT_S,
            )
            if rc == 0:
                parts = _assistant_text_parts(
                    _normalize_messages(_parse_json_payload(out)),
                )
                if parts:
                    return _json_response({"parts": parts}, 200)
            elif not await asyncio.to_thread(
                chamber.is_openchamber_daemon_running,
            ):
                raise httpx.ConnectError(
                    "openchamber daemon died mid-turn",
                )
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_POLL_INTERVAL_S)
        logger.warning(
            "openchamber send_message timed out after %.0fs without "
            "assistant text (session %s)",
            timeout, session_id[:16],
        )
        return _json_response({"parts": []}, 200)

    async def fetch_message(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        message_id: str,
    ) -> httpx.Response:
        """One persisted message's parts: the messages fetch filtered by
        the message id.  The serve analogue's only consumer reads
        ``.json()["parts"]`` (permission tool-name lookup — dormant
        here); 404 when the id is not in the session's messages.
        """
        await self._ensure_daemon()
        resp = await self.fetch_messages(client, session_id)
        for m in resp.json():
            if m.get("id") == message_id:
                return _json_response({"parts": m.get("parts") or []}, 200)
        return _json_response({"error": "message not found"}, 404)

    async def fetch_session_status(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """The serve's live status map has no openchamber analogue: every
        session ``session list`` reports maps to ``{"type": "idle"}``.

        The chamber transport has no busy-forever defect family to detect
        (the poll loop's own timeout governs wedges), and the map's other
        consumer (``_session_live``) only needs presence, so a truthful
        idle map keeps both the busy-stuck abort and the stall-abort
        dormant without losing liveness.
        """
        await self._ensure_daemon()
        rc, out = await chamber._run_cli(
            ["session", "list", "--json"], timeout=_MESSAGES_TIMEOUT_S,
        )
        if rc != 0:
            raise httpx.ConnectError(
                f"openchamber CLI list failed (rc={rc})",
            )
        status_map: dict[str, Any] = {}
        for entry in _list_entries(_parse_json_payload(out)):
            sid = entry.get("id")
            if sid:
                status_map[str(sid)] = {"type": "idle"}
        logger.debug(
            "openchamber session-status map: %d sessions (all idle — no analogue)",
            len(status_map),
        )
        return _json_response(status_map, 200)

    async def list_sessions(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """``session list --json`` in the serve's list shape (id + time
        when present).

        The zombie sweep's age check skips entries without timestamps
        (absent ``updated`` → falsy 0), which is the safe default for the
        chamber transport — it has no busy-forever runner defect to sweep.
        """
        await self._ensure_daemon()
        rc, out = await chamber._run_cli(
            ["session", "list", "--json"], timeout=_MESSAGES_TIMEOUT_S,
        )
        if rc != 0:
            raise httpx.ConnectError(
                f"openchamber CLI list failed (rc={rc})",
            )
        entries: list[dict[str, Any]] = []
        for entry in _list_entries(_parse_json_payload(out)):
            e: dict[str, Any] = {"id": entry.get("id")}
            if isinstance(entry.get("time"), dict):
                e["time"] = entry["time"]
            entries.append(e)
        return _json_response(entries, 200)

    async def fetch_permissions(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """No openchamber permission surface (the pre-allow block handles
        all bridge tools — verified in the prototype): the serve's
        pending-permission list is always empty."""
        logger.debug(
            "openchamber fetch_permissions: no permission surface (pre-allow block)",
        )
        return _json_response([], 200)

    async def reply_permission(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        permission_id: str,
        response: str,
    ) -> bool:
        """No openchamber permission surface: accept the decision so the
        caller's continuation proceeds (the relay machinery stays dormant —
        design §3.5)."""
        logger.info(
            "openchamber permission reply %s ignored — no permission surface",
            permission_id[:16],
        )
        return True

    async def fetch_questions(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """No openchamber question registry: a question part in the poll
        is answered by a NEW ``session send`` to the same session (design
        §3.4), so the serve's pending-question list is always empty — the
        resume path posts the answer as a normal prompt."""
        logger.debug(
            "openchamber fetch_questions: no question registry (answers are new session sends)",
        )
        return _json_response([], 200)

    async def reply_question(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        question_id: str,
        answer: str,
        question_count: int,
    ) -> bool:
        """No openchamber question-reply endpoint: answers are sent as new
        session sends (design §3.4), so the reply is a logged no-op."""
        logger.info(
            "openchamber question reply %s ignored — answers are new session sends",
            question_id[:16],
        )
        return True


#: The bridge's active backend, resolved ONCE at import.  The
#: ``OPENCODE_BACKEND`` env selects the transport: the literal
#: ``"openchamber"`` picks ``OpenChamberBackend`` (the CLI transport,
#: design step 3); ANY other value (unset, "serve", unknown) keeps the
#: production default ``ServeBackend`` until the evidence gate flips it
#: (design §6 — one env line, no redeploy).
#:
#: ``ServeBackend`` is STATELESS (session/request state lives in the
#: caller's dicts and per-request httpx clients), so this module-level
#: service reference carries no mutable state — the same class of
#: exception the F5/F6 carve-outs document for ``opencode_bridge``.
#: ``OpenChamberBackend`` holds one bounded per-instance map
#: (``_session_dirs`` — F8, see the class) because the CLI needs ``--dir``
#: on every call.
BACKEND: OpenCodeBackend = (
    OpenChamberBackend()
    if os.environ.get("OPENCODE_BACKEND") == "openchamber"
    else ServeBackend()
)
