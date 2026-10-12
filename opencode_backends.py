"""
opencode_backends.py — the opencode bridge's HTTP transport.

The bridge's message path (``opencode_bridge.opencode_chat`` /
``opencode_chat_stream`` / ``ensure_opencode_serve``) delegates every HTTP
primitive to a backend so the transport lives behind one interface.  Bridge
v4 leaves exactly ONE transport: ``SecureBackend``, the systemd-owned
secure-opencode server on ``OPENCODE_SECURE_SERVE_URL`` (127.0.0.1:4096).
The proxy owns no opencode server of its own.

This module holds the interface (``OpenCodeBackend``) and that single
implementation (``SecureBackend``): the exact HTTP calls the bridge made
inline — same URLs, same request shapes, same timeouts, same error
handling — relocated verbatim.  ``opencode_bridge.py`` routes its
message-path HTTP primitives through the module-level ``BACKEND`` instance.

The caller's ``httpx.AsyncClient`` is passed IN rather than owned: the
streaming path deliberately shares ONE client per request for connection
reuse and its per-request timeout semantics (``httpx.Timeout(600.0,
connect=10.0)``), so a per-call client inside the backend would change
behavior.  Methods return the raw ``httpx.Response`` (or the shaped
return the relocated helper produced); call sites keep their try/except
wrappers unchanged, which preserves the exact error-handling semantics.

``OPENCODE_BACKEND`` no longer selects a transport — ``SecureBackend`` is
the only one — but it is still read ONCE at import so an operator migrating
off a retired value is warned plainly instead of silently ignored.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

import httpx

from constants import (
    OPENCODE_SECURE_SERVE_URL,
    get_logger,
)

# Same logger name as the bridge: the relocated transport code must keep
# producing byte-identical log lines (e.g. the question-reply warnings).
logger = get_logger("kinver.opencode_bridge")


class OpenCodeBackend(ABC):
    """Transport abstraction behind the opencode bridge's message path.

    The bridge's public API and its helpers delegate every serve HTTP
    primitive to a backend so the transport stays behind one contract.
    Session-oriented signatures follow the design doc's shape —
    ``create_session(directory)``,
    ``send_prompt(session_id, system_prompt, user_text)``,
    ``fetch_messages(session_id)``,
    ``session_exists(session_id)``, ``abort_session(session_id)`` —
    adjusted only as needed to cover the serve primitives 1:1 (the caller's
    client parameter, and the serve-specific permission/question/status/
    list/send-message primitives).
    """

    #: Whether this transport owns its server's lifecycle.  ``SecureBackend``
    #: — the bridge's only transport — sets it False because the
    #: secure-opencode server is systemd-owned and the proxy must never
    #: spawn, recycle, reconfigure, auth-copy or signal it.
    owns_server: bool

    #: Whether this transport answers/relays permission prompts to the
    #: client.  ``SecureBackend`` sets it False — the secure-opencode
    #: server's own ``opencode-perm`` process resolves prompts out-of-band,
    #: so the bridge must neither relay a question nor POST an answer; it
    #: only reports that the session is waiting.
    answers_permissions: bool

    #: The SSE event-bus URL the streaming path dials
    #: (``opencode_bridge.opencode_chat_stream``).  The transport decides
    #: where its event bus lives.  Deliberately separate from ``base_url``:
    #: that attribute is the serve HTTP surface, so a transport without it
    #: would raise ``AttributeError`` if the bus dial read ``base_url``.
    event_bus_url: str

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
        system_prompt: str,
        user_text: str,
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

    # -- Serve-specific primitives --------------------------------------

    @abstractmethod
    async def send_message(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        system_prompt: str,
        user_text: str,
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
    system_prompt: str,
    user_text: str,
) -> dict[str, Any]:
    """Build the serve message/prompt payload.

    The system/parts shape the serve expects.  The payload carries NEITHER a
    model override NOR an agent override: model and agent selection both
    belong to the server's own config (the model override went in bridge v4
    T6; the agent override in the T8-found agent fix), so no caller can
    dictate either.  Relocated verbatim from the bridge's two inline
    construction sites so both send primitives share one copy.
    """
    return {
        "system": system_prompt,
        "parts": [{"type": "text", "text": user_text}],
    }


class SecureBackend(OpenCodeBackend):
    """The systemd-owned secure-opencode server transport (bridge v4).

    The secure server on ``OPENCODE_SECURE_SERVE_URL`` (127.0.0.1:4096) was
    launched by systemd with its own config and speaks the ``opencode serve``
    HTTP surface; this is the bridge's ONLY transport.  Every method wraps ONE
    serve HTTP call exactly as the bridge made it inline before the backend
    extraction: same URL shape, same request shape, same timeout, same error
    handling.  Stateless by design — session/request state lives in the
    caller's dicts and per-request ``httpx`` clients.

    ``owns_server`` is False: the proxy performs a health probe only and
    never spawns, recycles, config-syncs, auth-copies or signals this
    server.  The systemd unit is the user's to manage.

    ``answers_permissions`` is also False: the server's own
    ``opencode-perm`` resolves permission prompts out-of-band, so the bridge
    never relays a question and never POSTs an answer — while a prompt is
    pending it only reports the wait on the stream.
    """

    #: The secure server's HTTP base URL (the 13 primitives dial it) and the
    #: origin of its SSE event bus.
    base_url: str = OPENCODE_SECURE_SERVE_URL
    event_bus_url: str = OPENCODE_SECURE_SERVE_URL
    owns_server: bool = False
    answers_permissions: bool = False

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
            f"{self.base_url}/session",
            params={"directory": directory},
            timeout=30.0,
        )

    async def send_prompt(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        system_prompt: str,
        user_text: str,
    ) -> httpx.Response:
        """POST /session/<id>/prompt_async — the streaming path's send.

        The no-wait prompt dispatch: the agent picks the message up
        asynchronously and the serve streams its progress on the /event
        bus.  Returns the raw response (204 on accepted); the caller
        checks the status and owns the abort/error policy.  Timeout 30s —
        same as the session-create POST.
        """
        return await client.post(
            f"{self.base_url}/session/{session_id}/prompt_async",
            json=_build_send_payload(
                system_prompt=system_prompt, user_text=user_text,
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
            f"{self.base_url}/session/{session_id}/message", timeout=10.0,
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
            f"{self.base_url}/session/{session_id}", timeout=10.0,
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
                f"{self.base_url}/session/{session_id}/abort",
                timeout=10.0,
            )
        except (httpx.HTTPError, OSError):
            pass

    async def send_message(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        system_prompt: str,
        user_text: str,
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
            f"{self.base_url}/session/{session_id}/message",
            json=_build_send_payload(
                system_prompt=system_prompt, user_text=user_text,
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
            f"{self.base_url}/session/{session_id}/message/{message_id}",
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
            f"{self.base_url}/session/status", timeout=10.0,
        )

    async def list_sessions(
        self,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """GET /session — every session the serve currently holds.

        The zombie sweep reads this to find busy-but-stale sessions to
        abort.  Timeout 10s.
        """
        return await client.get(f"{self.base_url}/session", timeout=10.0)

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
        return await client.get(f"{self.base_url}/permission", timeout=10.0)

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
                f"{self.base_url}/session/{session_id}/permissions/{permission_id}",
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
        return await client.get(f"{self.base_url}/question", timeout=10.0)

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
                f"{self.base_url}/question/{question_id}/reply",
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


# The ``OPENCODE_BACKEND`` env no longer selects a transport (``SecureBackend``
# is the only one); it is read once here at import so an operator migrating
# off a retired value is told plainly instead of being silently ignored.
# Never fails on an unknown value.
if os.environ.get("OPENCODE_BACKEND") not in (None, "secure"):
    logger.warning(
        "OPENCODE_BACKEND=%r is retired or unrecognised; the opencode bridge "
        "always uses the systemd-owned secure server (%s). Remove the setting "
        "or set OPENCODE_BACKEND=secure.",
        os.environ.get("OPENCODE_BACKEND"),
        OPENCODE_SECURE_SERVE_URL,
    )


#: The bridge's active backend, resolved ONCE at import.  Bridge v4 left
#: exactly ONE transport, so this is always ``SecureBackend`` — the
#: systemd-owned secure-opencode server on ``OPENCODE_SECURE_SERVE_URL``
#: (127.0.0.1:4096).  It owns no server: ensure is a health probe only and
#: no spawn/recycle/config-sync/auth-copy/SIGTERM path runs.
#:
#: ``SecureBackend`` is STATELESS (session/request state lives in the
#: caller's dicts and per-request httpx clients), so this module-level
#: service reference carries no mutable state — the same class of exception
#: the prompt-priming registry carve-out documents for ``prompt_cache.py``.
BACKEND: OpenCodeBackend = SecureBackend()
