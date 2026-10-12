"""
opencode_backends.py — transport backends for the opencode bridge.

The serve transport is extracted behind a small backend interface so a
second transport can slot in behind the SAME public bridge API
(``opencode_bridge.opencode_chat`` / ``opencode_chat_stream`` /
``ensure_opencode_serve``).

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

The ``OPENCODE_BACKEND`` env selector resolves ``BACKEND``: the literal
``"secure"`` picks the systemd-owned secure-server transport, and anything
else keeps the serve transport (production default until the evidence gate
flips it — design §6; the secure transport is bridge v4, T4).
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

import httpx

from constants import (
    OPENCODE_SECURE_SERVE_URL,
    OPENCODE_SERVE_URL,
    get_logger,
)

# Same logger name as the bridge: the relocated transport code must keep
# producing byte-identical log lines (e.g. the question-reply warnings).
logger = get_logger("kinver.opencode_bridge")


class OpenCodeBackend(ABC):
    """Transport abstraction behind the opencode bridge's message path.

    The bridge's public API and its helpers delegate every serve HTTP
    primitive to a backend so a second transport can implement the same
    contract.  Session-oriented signatures follow the design doc's shape —
    ``create_session(directory)``,
    ``send_prompt(session_id, system_prompt, user_text)``,
    ``fetch_messages(session_id)``,
    ``session_exists(session_id)``, ``abort_session(session_id)`` —
    adjusted only as needed to cover the serve primitives 1:1 (the caller's
    client parameter, and the serve-specific permission/question/status/
    list/send-message primitives).
    """

    #: Whether this transport owns its server's lifecycle.  True by default
    #: (the serve transport spawns/recycles its backend); ``SecureBackend``
    #: sets it False because the secure-opencode server is systemd-owned and
    #: the proxy must never spawn, recycle, reconfigure, auth-copy or signal
    #: it.
    owns_server: bool = True

    #: Whether this transport answers/relays permission prompts to the
    #: client.  True by default: the serve transport relays write/git asks
    #: to the user as questions and POSTs the user's answer back so the
    #: parked tool resumes.  ``SecureBackend`` sets it False — the
    #: secure-opencode server's own ``opencode-perm`` process resolves
    #: prompts out-of-band, so the bridge must neither relay a question nor
    #: POST an answer; it only reports that the session is waiting.
    answers_permissions: bool = True

    #: The SSE event-bus URL the streaming path dials
    #: (``opencode_bridge.opencode_chat_stream``).  The transport decides
    #: where its event bus lives; the default preserves the historical
    #: serve behaviour (``OPENCODE_SERVE_URL``).  Deliberately separate from
    #: ``ServeBackend.base_url``: that attribute is the serve HTTP surface,
    #: so a transport without it would raise ``AttributeError`` if the bus
    #: dial read ``base_url``.
    event_bus_url: str = OPENCODE_SERVE_URL

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


class ServeBackend(OpenCodeBackend):
    """The ``opencode serve`` HTTP transport — today's bridge behavior.

    Every method wraps ONE serve HTTP call exactly as the bridge made it
    inline before the extraction: same URL, same request shape, same
    timeout, same error handling.  Stateless by design — session/request
    state lives in the caller's dicts and per-request ``httpx`` clients.
    """

    #: The serve base URL this transport talks to.  Parameterized (rather
    #: than hardcoding ``OPENCODE_SERVE_URL``) so ``SecureBackend`` can reuse
    #: this whole implementation against the systemd-owned secure server —
    #: the secure server speaks the same serve HTTP surface.
    base_url: str = OPENCODE_SERVE_URL

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


class SecureBackend(ServeBackend):
    """The systemd-owned secure-opencode server transport (bridge v4, T4).

    The secure server on ``OPENCODE_SECURE_SERVE_URL`` (127.0.0.1:4096) was
    launched by systemd with its own config, so it speaks the SAME serve
    HTTP surface as ``ServeBackend`` — this class only overrides the base
    URL and the ownership flag rather than copying the 13 method bodies.

    ``owns_server`` is False: the proxy performs a health probe only and
    never spawns, recycles, config-syncs, auth-copies or signals this
    server.  The systemd unit is the user's to manage.

    ``answers_permissions`` is also False: the server's own
    ``opencode-perm`` resolves permission prompts out-of-band, so the bridge
    never relays a question and never POSTs an answer — while a prompt is
    pending it only reports the wait on the stream.
    """

    base_url: str = OPENCODE_SECURE_SERVE_URL
    event_bus_url: str = OPENCODE_SECURE_SERVE_URL
    owns_server: bool = False
    answers_permissions: bool = False


#: The bridge's active backend, resolved ONCE at import.  The
#: ``OPENCODE_BACKEND`` env selects the transport:
#:   * the literal ``"secure"`` picks ``SecureBackend`` — the
#:     systemd-owned secure-opencode server on ``OPENCODE_SECURE_SERVE_URL``
#:     (127.0.0.1:4096).  It owns no server: ensure is a health probe only
#:     and no spawn/recycle/config-sync/auth-copy/SIGTERM path runs.
#:   * ANY other value (unset, "serve", unknown) keeps the production
#:     default ``ServeBackend`` until the evidence gate flips it (design §6
#:     — one env line, no redeploy).
#:
#: ``ServeBackend`` and ``SecureBackend`` are STATELESS (session/request
#: state lives in the caller's dicts and per-request httpx clients), so
#: this module-level service reference carries no mutable state — the same
#: class of exception the F5/F6 carve-outs document for ``opencode_bridge``.
BACKEND: OpenCodeBackend = (
    SecureBackend()
    if os.environ.get("OPENCODE_BACKEND") == "secure"
    else ServeBackend()
)
