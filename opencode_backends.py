"""
opencode_backends.py — transport backends for the opencode bridge.

Step 1 of the openchamber-bridge-backend design (2026-08-16): the serve
transport is extracted behind a small backend interface so a second
transport (the openchamber CLI, later steps) can slot in behind the SAME
public bridge API (``opencode_bridge.opencode_chat`` /
``opencode_chat_stream`` / ``ensure_opencode_serve``).

This module holds the interface (``OpenCodeBackend``) and today's ONLY
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
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

from constants import OPENCODE_SERVE_URL, get_logger

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
        """POST a permission decision to the serve (best-effort)."""

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
        """POST the user's answer to the serve's question reply endpoint."""


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


#: The bridge's active backend.  Step 1 ships the serve backend only; the
#: ``OPENCODE_BACKEND`` env selection and the OpenChamberBackend land in
#: later steps.  ``ServeBackend`` is STATELESS (session/request state lives
#: in the caller's dicts and per-request httpx clients), so this module-level
#: service reference carries no mutable state — the same class of exception
#: the F5/F6 carve-outs document for ``opencode_bridge``.
BACKEND: OpenCodeBackend = ServeBackend()
