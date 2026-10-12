"""Tests for the opencode bridge (opencode_bridge.py) and its routes.

Covers:
- opencode_chat: session create → message post → text extraction → proxy-status strip
- error paths (HTTP failures, network errors) degrade to error strings
- opencode_escalation mirrors the cloud-escalation contract
- routes: model:"opencode" (JSON + stream) and the /opencode embedded command
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional

import httpx
import pytest

import opencode_bridge
from opencode_bridge import (
    _strip_proxy_status_text,
    opencode_chat,
    opencode_escalation,
)


class _FakeResp:
    def __init__(self, status_code: int, data: Any) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> Any:
        return self._data


class _FakeStream:
    """Async context manager that yields scripted SSE data lines."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line
            await asyncio.sleep(0)


class _FakeClient:
    """Scripted httpx client: get→config, post→session then message."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[tuple[str, str]] = []
        self.post_calls: list[tuple[str, Any]] = []
        self.session_id = "ses_0001"
        self.session_status = 200
        self.message_status = 200
        self.message_parts: list[dict[str, Any]] = []
        self.get_status = 200
        self.raise_on = ""
        self.raise_timeout_on = ""
        self.get_timeouts: list[Any] = []
        self.stream_lines: list[str] = []
        # The dial URL of every client.stream(...) call — lets a test assert
        # the /event bus targets the ACTIVE transport's event_bus_url, never
        # a hardcoded URL (T8-found regression).
        self.stream_calls: list[str] = []
        self.message_get_parts: list[dict[str, Any]] = []
        # Blocking-path scripting (opencode_chat hardening): scripted
        # GET /permission records and a message POST that hangs briefly so
        # the permission poller gets a chance to run.
        self.permission_records: list[dict[str, Any]] = []
        self.message_hang_s: float = 0.0
        # Streaming-path scripting (opencode_chat_stream exit hygiene):
        # scripted prompt_async status, a mid-request /event connection
        # failure, and an Nth-GET failure switch.  NOTE: every GET inside
        # opencode_chat_stream is swallow-guarded, so fail_get_after cannot
        # reach the outer except; it exists for resilience scripting while
        # raise_on_stream deterministically triggers the outer except.
        self.prompt_status: int = 204
        self.raise_on_stream: bool = False
        self.fail_get_after: Optional[int] = None
        self._get_count: int = 0
        # Serve-lifecycle scripting (bridge-cycle-4): a scripted
        # /session/status map (REQ-2 stale-pin tests) and a scripted
        # N-message-POST transport failure (REQ-4 blocking respawn tests).
        self.status_map: Optional[dict[str, Any]] = None
        self.fail_post_times: int = 0
        # Resume-path existence probe: GET /session/<id> returns this
        # status (200 = session live, 404 = gone).  The REAL serve only
        # lists BUSY sessions in /session/status (idle sessions are
        # deleted from the map on completion), so the default models the
        # live-session case: 200.
        self.session_get_status: int = 200

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        self.calls.append(("get", url))
        self.get_timeouts.append(kwargs.get("timeout"))
        if self.raise_on == "get":
            raise httpx.ConnectError("conn refused")
        if self.raise_timeout_on == "get":
            raise httpx.ReadTimeout("read timed out")
        self._get_count += 1
        if self.fail_get_after is not None and self._get_count > self.fail_get_after:
            raise httpx.ReadTimeout("read timed out")
        if "/message/" in url:
            # Persisted-message GET used by the question retry-race fetch.
            return _FakeResp(200, {"info": {}, "parts": self.message_get_parts})
        if "/session/status" in url:
            # Event-stream end → polling fallback → session is idle (done).
            # REQ-2 (bridge-cycle-4): a scripted status_map overrides the
            # default so tests can assert stale-pin drop (map without the
            # pinned id) and pin retention on a failed fetch.
            if self.status_map is not None:
                return _FakeResp(200, self.status_map)
            return _FakeResp(200, {self.session_id: {"type": "idle"}})
        import re as _re

        if _re.search(r"/session/[^/]+$", url):
            # GET /session/<id> — the resume-path existence probe.
            return _FakeResp(self.session_get_status, {})
        if url.endswith("/permission"):
            # Blocking-path permission poll (opencode_chat hardening).
            return _FakeResp(200, self.permission_records)
        return _FakeResp(self.get_status, {})

    async def post(self, url: str, **kwargs: Any) -> _FakeResp:
        self.calls.append(("post", url))
        self.post_calls.append((url, kwargs.get("json")))
        if self.raise_on == "post":
            raise httpx.ConnectError("conn refused")
        if self.raise_timeout_on == "post" and "/message" in url:
            # Blocking-path message-POST timeout (opencode_chat hardening);
            # pinned to "/message" URLs so the session-create POST still
            # succeeds and the abort path can be asserted.
            raise httpx.ReadTimeout("read timed out")
        if self.fail_post_times and "/message" in url:
            # REQ-4 (bridge-cycle-4): scripted transport failure on the
            # blocking message POST — decrements per call so the bounded
            # respawn retry can be exercised deterministically.
            self.fail_post_times -= 1
            raise httpx.ConnectError("conn refused")
        if self.message_hang_s and "/message" in url:
            await asyncio.sleep(self.message_hang_s)
        if url.endswith("/session"):
            return _FakeResp(self.session_status, {"id": self.session_id})
        if "prompt_async" in url:
            return _FakeResp(self.prompt_status, {})
        return _FakeResp(self.message_status, {"parts": self.message_parts})

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStream:
        # Record the dial target (client.stream("GET", url)) so a test can
        # assert the /event bus follows the ACTIVE transport's base_url.
        self.stream_calls.append(
            args[1] if len(args) > 1 else str(kwargs.get("url", "")),
        )
        if self.raise_on_stream:
            raise httpx.ConnectError("conn refused")
        return _FakeStream(self.stream_lines)


class _FakeApp:
    """Minimal FastAPI stand-in: ``app.state`` with a real pending-permissions map.

    Lets route-level tests (``_handle_opencode_command``) exercise the true
    ``_pending_permissions_state(app)`` accessor instead of a fake dict, so
    the relay state lands where the pinned-session continuation reads it.
    """

    def __init__(
        self, pending: Optional[dict[str, tuple[str, bool]]] = None,
    ) -> None:
        self.state = SimpleNamespace()
        self.state.opencode_pending_permissions = (
            pending if pending is not None else {}
        )


class _BusyThenIdleClient(_FakeClient):
    """_FakeClient variant whose /session/status reports "busy" for the
    first ``busy_polls`` status GETs, then "idle" — keeps the polling
    fallback in its busy loop for a few poll cycles so keepalives fire."""

    def __init__(self) -> None:
        super().__init__()
        self.busy_polls: int = 3

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        if "/session/status" in url:
            if self.busy_polls > 0:
                self.busy_polls -= 1
                return _FakeResp(200, {self.session_id: {"type": "busy"}})
            return _FakeResp(200, {self.session_id: {"type": "idle"}})
        return await super().get(url, **kwargs)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    client = _FakeClient()
    monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)
    return client


# Shared pending-permission map for tests that exercise the permission
# relay: the bridge no longer holds module-level mutable state (Rule 6), so
# tests pass this dict in and assert against it.
PP: dict[str, tuple[str, bool]] = {}


@pytest.fixture(autouse=True)
def _clean_pending_permissions() -> None:
    """PP survives across tests — clear it before AND after each test so a
    stored write permission never leaks into another test (keyed by session
    id; tests reuse "ses_0001")."""
    PP.clear()
    yield
    PP.clear()





TRIAGE_TEXT = "\u200b🔍 Proxy triage: classified as CODE (priority 1). Routing to Professional (35B MoE) on port 13109.DONE"


# ---------------------------------------------------------------------------
# Status stripping
# ---------------------------------------------------------------------------

class TestStatusStrip:
    def test_strips_leading_triage(self) -> None:
        assert _strip_proxy_status_text(TRIAGE_TEXT) == "DONE"

    def test_strips_client_specified_triage(self) -> None:
        text = "\u200b🔀 Client specified Professional (35B MoE). Routing on port 13109. (frontdesk suggested CHAT, client model wins.)ANSWER"
        assert _strip_proxy_status_text(text) == "ANSWER"

    def test_no_sentinel_untouched(self) -> None:
        assert _strip_proxy_status_text("plain answer") == "plain answer"


# ---------------------------------------------------------------------------
# opencode_chat
# ---------------------------------------------------------------------------

class TestOpenCodeChat:
    @pytest.mark.asyncio
    async def test_success_collects_text_parts(self, fake_client: _FakeClient) -> None:
        fake_client.message_parts = [
            {"type": "step-start", "text": None},
            {"type": "text", "text": TRIAGE_TEXT},
            {"type": "reasoning", "text": "thinking..."},
            {"type": "step-finish", "text": None},
        ]
        result = await opencode_chat("write a test")
        assert result == "DONE"
        urls = [u for _, u in fake_client.calls]
        assert any(u.endswith("/session") for u in urls)
        assert any(u.endswith("/session/ses_0001/message") for u in urls)

    @pytest.mark.asyncio
    async def test_session_failure_returns_error(self, fake_client: _FakeClient) -> None:
        fake_client.session_status = 500
        result = await opencode_chat("task")
        assert result.startswith("[OpenCode Bridge Error:")

    @pytest.mark.asyncio
    async def test_message_failure_returns_error(self, fake_client: _FakeClient) -> None:
        fake_client.message_status = 503
        result = await opencode_chat("task")
        assert result.startswith("[OpenCode Bridge Error:")

    @pytest.mark.asyncio
    async def test_network_error_returns_error(self, fake_client: _FakeClient) -> None:
        fake_client.raise_on = "post"
        result = await opencode_chat("task")
        assert result.startswith("[OpenCode Bridge Network Error:")

    @pytest.mark.asyncio
    async def test_empty_response_returns_error(self, fake_client: _FakeClient) -> None:
        fake_client.message_parts = []
        result = await opencode_chat("task")
        assert "[OpenCode Bridge Error: empty response.]" in result


class TestOpenCodeChatHardening:
    """Blocking-path hardening (bridge-cycle-5): permission polling during
    the message POST + session cleanup on every non-success exit.

    Hermetic: scripted _FakeClient, no live serve.  The poll cadence is
    monkeypatched to 0.01s and the message POST hangs briefly so the
    permission poller gets deterministic turns.
    """

    @pytest.mark.asyncio
    async def test_timeout_aborts_session(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(opencode_bridge, "_BLOCKING_PERMISSION_POLL_S", 0.01)
        fake_client.raise_timeout_on = "post"
        result = await opencode_chat("task", timeout=10.0)
        assert result.startswith("[OpenCode Bridge Network Error:")
        assert any(
            url.endswith("/abort") for url, _ in fake_client.post_calls
        )

    @pytest.mark.asyncio
    async def test_message_503_aborts_session(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(opencode_bridge, "_BLOCKING_PERMISSION_POLL_S", 0.01)
        fake_client.message_status = 503
        result = await opencode_chat("task")
        assert result == "[OpenCode Bridge Error: message HTTP 503]"
        assert any(
            url.endswith("/abort") for url, _ in fake_client.post_calls
        )

    @pytest.mark.asyncio
    async def test_success_no_permissions_unchanged(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(opencode_bridge, "_BLOCKING_PERMISSION_POLL_S", 0.01)
        fake_client.message_parts = [{"type": "text", "text": "plain success"}]
        result = await opencode_chat("task")
        assert result == "plain success"
        assert not any(
            url.endswith("/abort") for url, _ in fake_client.post_calls
        )
        assert not any(
            "/permissions/" in url for url, _ in fake_client.post_calls
        )

    # -- bridge-cycle-4 REQ-4: bounded blocking respawn ---------------------

    @pytest.mark.asyncio
    async def test_blocking_respawn_single_retry(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-4 Scenario-1: one network-class failure after a successful
        ensure → one re-ensure + one retry; the retried call returns its
        text."""
        from opencode_bridge import opencode_chat

        ensure_results = [True, True]
        ensure_calls: list[str] = []

        async def _ensure() -> bool:
            ensure_calls.append("ensure")
            return ensure_results.pop(0)

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure)

        fake_client.fail_post_times = 1
        fake_client.message_parts = [{"type": "text", "text": "recovered"}]
        result = await opencode_chat("task")

        assert result == "recovered"
        assert len(ensure_calls) == 2
        msg_posts = [
            url for url, _ in fake_client.post_calls if "/message" in url
        ]
        assert len(msg_posts) == 2

    @pytest.mark.asyncio
    async def test_blocking_respawn_second_failure_returns_error(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-4 Scenario-2: when the single retry also fails, the error
        string is returned and no third attempt occurs."""
        from opencode_bridge import opencode_chat

        ensure_results = [True, True]

        async def _ensure() -> bool:
            return ensure_results.pop(0)

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure)

        fake_client.fail_post_times = 2
        result = await opencode_chat("task")

        assert result.startswith("[OpenCode Bridge Network Error:")
        msg_posts = [
            url for url, _ in fake_client.post_calls if "/message" in url
        ]
        assert len(msg_posts) == 2

    @pytest.mark.asyncio
    async def test_blocking_no_retry_on_http_error(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-4 Scenario-3: an HTTP-status failure never triggers the
        retry — the existing HTTP error string is returned."""
        from opencode_bridge import opencode_chat

        ensure_calls: list[str] = []

        async def _ensure() -> bool:
            ensure_calls.append("ensure")
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure)

        fake_client.message_status = 503
        result = await opencode_chat("task")

        assert result == "[OpenCode Bridge Error: message HTTP 503]"
        assert len(ensure_calls) == 1
        msg_posts = [
            url for url, _ in fake_client.post_calls if "/message" in url
        ]
        assert len(msg_posts) == 1

    @pytest.mark.asyncio
    async def test_blocking_respawn_reensure_failure(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-4: a failed re-ensure after the network failure returns the
        not-reachable error — no retry attempt."""
        from opencode_bridge import opencode_chat

        ensure_results = [True, False]

        async def _ensure() -> bool:
            return ensure_results.pop(0)

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure)

        fake_client.fail_post_times = 1
        result = await opencode_chat("task")

        assert result == (
            "[OpenCode Bridge Failed: the systemd-owned secure opencode "
            "server (http://127.0.0.1:4096) is not answering; start "
            "secure-opencode.service (the proxy never starts it).]"
        )
        msg_posts = [
            url for url, _ in fake_client.post_calls if "/message" in url
        ]
        assert len(msg_posts) == 1


class TestStreamExitHygiene:
    """Streaming-path error-exit hygiene (bridge-cycle-6): every error
    exit aborts the session, drops the pin, and pops pending permissions.

    Hermetic: scripted _FakeClient + monkeypatched ensure, no live serve.
    """

    @staticmethod
    async def _running(*args: Any, **kwargs: Any) -> bool:
        return True

    @pytest.mark.asyncio
    async def test_stream_network_error_aborts_and_drops_pin(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-4: a mid-request /event connection failure yields the
        network error AND runs the cleanup trio (abort + pin drop +
        pending-pop)."""
        from opencode_bridge import opencode_chat_stream

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", self._running)
        client = _FakeClient()
        client.raise_on_stream = True  # /event connection fails mid-request
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        pending: dict[str, tuple[str, bool]] = {"ses_0001": ("perm_1", True)}
        deltas = [
            d async for d in opencode_chat_stream(
                "task", session_map=smap, session_key="conv",
                pending_permissions=pending,
            )
        ]

        assert len(deltas) == 1
        kind, text = deltas[0]
        assert kind == "status"
        assert text.startswith("[OpenCode Bridge Network Error:")
        assert any(
            url.endswith("/abort") for url, _ in client.post_calls
        )
        assert smap == {}    # pin dropped
        assert pending == {}  # pending permission popped

    @pytest.mark.asyncio
    async def test_message_updated_error_aborts_and_drops_pin(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-3: a message.updated event carrying info.error yields the
        error AND runs the cleanup trio."""
        from opencode_bridge import opencode_chat_stream

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", self._running)
        client = _FakeClient()
        client.stream_lines = [
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_a", "role": "assistant", "error": "boom"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        pending: dict[str, tuple[str, bool]] = {"ses_0001": ("perm_1", True)}
        deltas = [
            d async for d in opencode_chat_stream(
                "task", session_map=smap, session_key="conv",
                pending_permissions=pending,
            )
        ]

        assert deltas == [("status", "[OpenCode Bridge Error: boom]")]
        assert any(
            url.endswith("/abort") for url, _ in client.post_calls
        )
        assert smap == {}
        assert pending == {}

    @pytest.mark.asyncio
    async def test_prompt_non_204_drops_pin(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-2: prompt_async HTTP != 204 yields the prompt error AND runs
        the cleanup trio."""
        from opencode_bridge import opencode_chat_stream

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", self._running)
        client = _FakeClient()
        client.prompt_status = 500
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        pending: dict[str, tuple[str, bool]] = {"ses_0001": ("perm_1", True)}
        deltas = [
            d async for d in opencode_chat_stream(
                "task", session_map=smap, session_key="conv",
                pending_permissions=pending,
            )
        ]

        assert deltas == [("status", "[OpenCode Bridge Error: prompt HTTP 500]")]
        assert any(
            url.endswith("/abort") for url, _ in client.post_calls
        )
        assert smap == {}
        assert pending == {}




class TestStalePinSelfHeal:
    """Serve-lifecycle REQ-2 (bridge-cycle-4): a pinned session that a
    SUCCESSFUL /session/status fetch no longer lists is stale — drop the
    pin and start fresh instead of posting to a nonexistent session.  A
    transport-error fetch keeps the pin conservatively.

    Hermetic: scripted _FakeClient + monkeypatched ensure, no live serve.
    """

    @staticmethod
    async def _running(*args: Any, **kwargs: Any) -> bool:
        return True

    @pytest.mark.asyncio
    async def test_stale_pin_dropped_when_existence_probe_404s(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-2 Scenario-1: a successful GET /session/<id> probe returning
        404 (the session is genuinely gone — serve recycled or dropped)
        drops the pin and POSTs a fresh /session; no abort is fired (the
        session is gone)."""
        from opencode_bridge import opencode_chat_stream

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", self._running)
        client = _FakeClient()
        client.session_get_status = 404  # existence probe: session gone
        client.stream_lines = _stream_events()
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {"conv": "ses_0001"}
        deltas = [
            d async for d in opencode_chat_stream(
                "task", session_map=smap, session_key="conv",
            )
        ]

        # The stream still completes on the fresh session.
        text = [t for k, t in deltas if k == "text"]
        assert "Created file." in text
        assert "Done." in text
        # A fresh session POST happened (a kept pin would reuse the id
        # without POSTing) and no abort fired for the nonexistent session.
        session_posts = [
            url for url, _ in client.post_calls if url.endswith("/session")
        ]
        assert len(session_posts) == 1
        assert smap == {"conv": "ses_0001"}  # repinned to the fresh session
        assert not any(
            url.endswith("/abort") for url, _ in client.post_calls
        )

    @pytest.mark.asyncio
    async def test_pin_kept_on_existence_probe_transport_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-2 Scenario-2: a transport-error existence probe keeps the
        pin — no fresh /session POST; the pinned session is used and the
        stream completes."""
        from opencode_bridge import opencode_chat_stream

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", self._running)
        client = _FakeClient()
        client.raise_timeout_on = "get"  # probe raises ReadTimeout
        client.stream_lines = _stream_events()
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {"conv": "ses_0001"}
        deltas = [
            d async for d in opencode_chat_stream(
                "task", session_map=smap, session_key="conv",
            )
        ]

        # The pinned session carried the stream; the pin was kept.
        text = [t for k, t in deltas if k == "text"]
        assert "Created file." in text
        assert "Done." in text
        assert not any(
            url.endswith("/session") for url, _ in client.post_calls
        )
        assert smap == {"conv": "ses_0001"}

    @pytest.mark.asyncio
    async def test_idle_pinned_session_kept_when_status_map_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REGRESSION (2026-08-13): the serve's /session/status map only
        lists BUSY sessions — idle sessions are deleted from it on
        completion (upstream SessionStatus.set deletes on idle).  An
        empty status map therefore does NOT mean the pinned session is
        gone: the follow-up must resume the SAME session (no fresh POST,
        no abort, pin kept).  This is the production failure behind
        'runs a few commands and then stops' — every "continue" created
        a fresh session and the agent lost the task context."""
        from opencode_bridge import opencode_chat_stream

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", self._running)
        client = _FakeClient()
        # The real serve's status map: empty while the pinned session is
        # idle (it only ever holds busy sessions).
        client.status_map = {}
        client.stream_lines = _stream_events()
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {"conv": "ses_0001"}
        deltas = [
            d async for d in opencode_chat_stream(
                "answer", session_map=smap, session_key="conv",
            )
        ]

        text = [t for k, t in deltas if k == "text"]
        assert "Created file." in text
        assert "Done." in text
        # The pinned session carried the stream: no fresh /session POST,
        # no abort, and the pin survives for the next follow-up.
        assert not any(
            url.endswith("/session") for url, _ in client.post_calls
        )
        assert not any(
            url.endswith("/abort") for url, _ in client.post_calls
        )
        assert smap == {"conv": "ses_0001"}





class TestEscalation:
    @pytest.mark.asyncio
    async def test_escalation_returns_chat_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_chat(text: str) -> str:
            return f"handled:{text}"

        monkeypatch.setattr(opencode_bridge, "opencode_chat", _fake_chat)
        result = await opencode_escalation(3, "fix the bug")
        assert result == "handled:fix the bug"


# ---------------------------------------------------------------------------
# Routes: model "opencode" and the /opencode embedded command
# ---------------------------------------------------------------------------


async def _drain_stream(response: Any) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


class TestRoutesOpenCode:
    @pytest.mark.asyncio
    async def test_model_opencode_returns_json_chat_completion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from routes import _handle_opencode_request

        captured: list[str] = []

        async def _fake_stream(
            text: str,
            session_map: Optional[dict[str, str]] = None,
            session_key: Optional[str] = None,
            pending_permissions: Optional[dict[str, tuple[str, bool]]] = None,
            system_prompt: str = "",
            timeout: float = 600.0,
            directory: Optional[str] = None,
        ) -> AsyncIterator[tuple[str, str]]:
            captured.append(text)
            yield ("text", "BRIDGE_DONE")

        monkeypatch.setattr("routes.opencode_chat_stream_resilient", _fake_stream)
        resp = await _handle_opencode_request(
            [{"role": "user", "content": "write a test"}],
            client_stream=False,
        )
        assert captured == ["write a test"]
        assert resp.status_code == 200
        body = resp.body.decode()
        assert "BRIDGE_DONE" in body
        assert '"model":"opencode"' in body

    @pytest.mark.asyncio
    async def test_model_opencode_streams_sse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from routes import _handle_opencode_request

        async def _fake_stream(
            text: str,
            session_map: Optional[dict[str, str]] = None,
            session_key: Optional[str] = None,
            pending_permissions: Optional[dict[str, tuple[str, bool]]] = None,
            system_prompt: str = "",
            timeout: float = 600.0,
            directory: Optional[str] = None,
        ) -> AsyncIterator[tuple[str, str]]:
            yield ("text", "STREAMED_")
            yield ("text", "DONE")

        monkeypatch.setattr("routes.opencode_chat_stream_resilient", _fake_stream)
        resp = await _handle_opencode_request(
            [{"role": "user", "content": "task"}],
            client_stream=True,
        )
        text = await _drain_stream(resp)
        assert "Directing to OpenCode" in text
        assert "STREAMED_" in text
        assert '"content": "DONE"' in text
        assert "[DONE]" in text

    @pytest.mark.asyncio
    async def test_model_opencode_requires_user_message(self) -> None:
        from routes import _handle_opencode_request

        resp = await _handle_opencode_request([], client_stream=True)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_opencode_command_strips_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from routes import _handle_opencode_command

        captured: list[str] = []

        async def _fake_stream(
            text: str,
            session_map: Optional[dict[str, str]] = None,
            session_key: Optional[str] = None,
            pending_permissions: Optional[dict[str, tuple[str, bool]]] = None,
            system_prompt: str = "",
            timeout: float = 600.0,
            directory: Optional[str] = None,
        ) -> AsyncIterator[tuple[str, str]]:
            captured.append(text)
            yield ("text", "CMD_DONE")

        monkeypatch.setattr("routes.opencode_chat_stream_resilient", _fake_stream)
        resp = await _handle_opencode_command(
            "/opencode implement the parser", _FakeApp(),
        )
        text = await _drain_stream(resp)
        assert captured == ["implement the parser"]
        assert "CMD_DONE" in text
        assert "OpenCode (gentle-orchestrator agent)" in text


# ---------------------------------------------------------------------------
# opencode_chat_stream
# ---------------------------------------------------------------------------

def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data)}"


def _evt(etype: str, **props: Any) -> str:
    return _sse({"id": "evt_x", "type": etype, "properties": props})


def _stream_events() -> list[str]:
    """Scripted event sequence for one agentic turn: user msg, assistant
    reasoning + text + step-finish, then a quiet completion."""
    return [
        _evt("server.connected", **{}),
        _evt("message.updated", sessionID="ses_0001",
             info={"id": "msg_user", "role": "user"}),
        _evt("message.part.updated", sessionID="ses_0001",
             part={"id": "prt_u", "messageID": "msg_user", "type": "text",
                   "text": "the echoed prompt"}),
        _evt("message.updated", sessionID="ses_0001",
             info={"id": "msg_a", "role": "assistant"}),
        _evt("message.part.updated", sessionID="ses_0001",
             part={"id": "prt_r", "messageID": "msg_a", "type": "reasoning",
                   "text": "think about it"}),
        _evt("message.part.updated", sessionID="ses_0001",
             part={"id": "prt_t", "messageID": "msg_a", "type": "text",
                   "text": "Created file."}),
        _evt("message.part.updated", sessionID="ses_0001",
             part={"id": "prt_f", "messageID": "msg_a", "type": "step-finish"}),
        # A second assistant message (the final summary) must also stream.
        _evt("message.updated", sessionID="ses_0001",
             info={"id": "msg_b", "role": "assistant"}),
        _evt("message.part.updated", sessionID="ses_0001",
             part={"id": "prt_s", "messageID": "msg_b", "type": "text",
                   "text": "Done."}),
        _evt("message.part.updated", sessionID="ses_0001",
             part={"id": "prt_f2", "messageID": "msg_b", "type": "step-finish"}),
    ]


class TestChatStream:
    @pytest.mark.asyncio
    async def test_streams_reasoning_text_and_summary(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        client = _FakeClient()
        client.stream_lines = _stream_events()
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]
        kinds = [k for k, _ in deltas]
        joined = "".join(t for _, t in deltas)
        # Reasoning is announced once as a compact status chunk; the raw
        # chain-of-thought text is NOT streamed to the client.
        assert "reasoning" not in kinds
        assert ("status", "🧠 thinking…\n") in deltas
        assert "think about it" not in joined
        # User echo excluded; both assistant messages' text streamed.
        assert "the echoed prompt" not in joined
        assert "Created file." in joined
        assert "Done." in joined
        # Exactly one prompt_async POST (the reviewer-duplicate regression).
        async_posts = [u for m, u in client.calls if m == "post" and "prompt_async" in u]
        assert len(async_posts) == 1

    @pytest.mark.asyncio
    async def test_session_error_surfaces_cleanly(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        client = _FakeClient()
        client.session_status = 500
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]
        joined = "".join(t for _, t in deltas)
        assert any("session HTTP 500" in t for _, t in deltas)

    @pytest.mark.asyncio
    async def test_polling_fallback_emits_keepalives_while_busy(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression (2026-08-07): once the /event SSE bus closes, the
        polling fallback must keep emitting "still working" status chunks
        while the agent is busy but quiet (a long tool run with no output).
        Before the fix the polling path yielded ZERO chunks during real
        work, so nanobot's 90s stall detector killed the stream."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        # Keepalives after 0.05s of client silence instead of 8.0s.
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.05)
        client = _BusyThenIdleClient()
        # Empty event bus → first anext() raises StopAsyncIteration → the
        # stream drops straight into the polling fallback.
        client.stream_lines = []
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        keepalives: list[str] = []
        async for kind, text in opencode_chat_stream("task"):
            if kind == "status" and "still working" in text:
                keepalives.append(text)

        # The session stayed busy for three 1s poll cycles; each quiet
        # cycle after the first must have emitted a keepalive (poll cadence
        # 1.0s ≫ patched quiet timeout 0.05s), never a silent gap.
        assert len(keepalives) >= 2, f"polling fallback went silent: {keepalives}"
        # Keepalives carry the polling-phase progress feedback.
        assert any("(polling" in k for k in keepalives)


class _PollClient(_FakeClient):
    """_FakeClient variant whose /session/{id}/message GET (the polling
    fallback's read) returns a scripted assistant-message list."""

    def __init__(self) -> None:
        super().__init__()
        self.poll_messages: list[dict[str, Any]] = []

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        if url.endswith(f"/session/{self.session_id}/message"):
            return _FakeResp(200, self.poll_messages)
        return await super().get(url, **kwargs)


class _SeedPollClient(_PollClient):
    """_PollClient variant whose FIRST /session/{id}/message list GET serves
    the scripted seed history (the resumed-session pre-prompt seed read);
    every later list GET delegates to ``_PollClient.get`` (poll_messages).

    Faithful to reality: at seed time the new turn has not been POSTed yet,
    so seed content is history-only.  ``seed_status``/``raise_on_seed``
    script seed failures; existing ``_PollClient`` tests never set
    ``seed_messages``, so this subclass never changes their behavior."""

    def __init__(self) -> None:
        super().__init__()
        self.seed_messages: list[dict[str, Any]] = []
        self.seed_status: int = 200
        self.raise_on_seed: bool = False
        self._seed_served: bool = False

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        if (
            not self._seed_served
            and url.endswith(f"/session/{self.session_id}/message")
        ):
            self._seed_served = True
            if self.raise_on_seed:
                raise httpx.ConnectError("conn refused")
            return _FakeResp(self.seed_status, self.seed_messages)
        return await super().get(url, **kwargs)


def _assistant_msg(parts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"info": {"role": "assistant"}, "parts": parts}


class TestToolStatePartKeyed:
    """Tool-state tracking must be keyed by PART ID, not tool name.

    Regression (2026-08-07): two bash parts with different states (one
    "error", one "running") flip-flopped the name-keyed tool_state["bash"]
    on every poll, so the running part re-emitted "🔧 bash…" once per
    second — "bash 100s of times" in the client.
    """

    @pytest.mark.asyncio
    async def test_running_part_emits_once_across_polls(self) -> None:
        """A running tool part yields "🔧 bash…" exactly once, even when
        the same part is polled repeatedly."""
        from opencode_bridge import _poll_session_deltas

        client = _PollClient()
        client.poll_messages = [_assistant_msg([{
            "id": "prt_bash_run", "messageID": "msg_a", "type": "tool",
            "tool": "bash", "state": {"status": "running"},
        }])]
        tool_state: dict[str, str] = {}
        text_lens: dict[str, int] = {}

        first = [
            d async for d in _poll_session_deltas(
                client, client.session_id, set(), text_lens, tool_state,
            )
        ]
        second = [
            d async for d in _poll_session_deltas(
                client, client.session_id, set(), text_lens, tool_state,
            )
        ]
        assert (first + second).count(("status", "🔧 bash…\n")) == 1

    @pytest.mark.asyncio
    async def test_same_name_parts_do_not_reemit_running(self) -> None:
        """A second part with the SAME tool name but a different state must
        not make the running part re-emit: with an error and a running bash
        part polled together twice, "🔧 bash…" appears once and "⚠️ bash
        failed" appears once."""
        from opencode_bridge import _poll_session_deltas

        client = _PollClient()
        client.poll_messages = [_assistant_msg([
            {
                "id": "prt_bash_err", "messageID": "msg_a", "type": "tool",
                "tool": "bash", "state": {"status": "error"},
            },
            {
                "id": "prt_bash_run", "messageID": "msg_a", "type": "tool",
                "tool": "bash", "state": {"status": "running"},
            },
        ])]
        tool_state: dict[str, str] = {}
        text_lens: dict[str, int] = {}

        first = [
            d async for d in _poll_session_deltas(
                client, client.session_id, set(), text_lens, tool_state,
            )
        ]
        second = [
            d async for d in _poll_session_deltas(
                client, client.session_id, set(), text_lens, tool_state,
            )
        ]
        deltas = first + second
        assert deltas.count(("status", "🔧 bash…\n")) == 1
        assert deltas.count(("status", "⚠️ bash failed\n")) == 1

    @pytest.mark.asyncio
    async def test_error_part_yields_failed_status(self) -> None:
        """A tool part in the error state surfaces "⚠️ {name} failed" to
        the user (previously the error transition was silent)."""
        from opencode_bridge import _yield_part_deltas

        part = {
            "id": "prt_bash_err", "messageID": "msg_a", "type": "tool",
            "tool": "bash", "state": {"status": "error"},
        }
        deltas = [
            d async for d in _yield_part_deltas(
                part, {}, {}, "ses_0001", _FakeClient(),
            )
        ]
        assert deltas == [("status", "⚠️ bash failed\n")]


class TestSeedResumedSessionState:
    """_seed_resumed_session_state must seed text lengths, tool states,
    assistant message ids, and seen question pids for a RESUMED session —
    and never raise on failure (bridge-cycle-8 REQ-1)."""

    @pytest.mark.asyncio
    async def test_seeds_text_lens_tool_state_question_pids(self) -> None:
        from opencode_bridge import _seed_resumed_session_state

        client = _SeedPollClient()
        client.seed_messages = [_assistant_msg([
            {"id": "prt_old_text", "messageID": "msg_old", "type": "text",
             "text": "old answer text"},
            {"id": "prt_old_reason", "messageID": "msg_old", "type": "reasoning",
             "text": "old chain of thought"},
            {"id": "prt_old_tool", "messageID": "msg_old", "type": "tool",
             "tool": "bash", "state": {"status": "completed"}},
            {"id": "prt_q", "messageID": "msg_old", "type": "tool",
             "tool": "question",
             "state": {"status": "running", "input": {"question": "old?"}}},
        ])]
        client.seed_messages[0]["id"] = "msg_old"

        user_mids: set[str] = set()
        text_lens: dict[str, int] = {}
        tool_state: dict[str, str] = {}
        seen_question_pids: set[str] = set()

        await _seed_resumed_session_state(
            client, client.session_id, user_mids, text_lens,
            tool_state, seen_question_pids,
        )

        assert "msg_old" in user_mids
        assert text_lens["prt_old_text"] == len("old answer text")
        assert text_lens["prt_old_reason"] == len("old chain of thought")
        assert tool_state["prt_old_tool"] == "completed"
        assert seen_question_pids == {"prt_q"}

    @pytest.mark.asyncio
    async def test_seed_failure_never_raises(self) -> None:
        """A failing seed (HTTP 500, a raising GET, or a malformed dict
        body) must return silently — polling degrades to unseeded."""
        from opencode_bridge import _seed_resumed_session_state

        class _DictBodyClient(_FakeClient):
            async def get(self, url: str, **kwargs: Any) -> _FakeResp:
                if url.endswith(f"/session/{self.session_id}/message"):
                    return _FakeResp(200, {"error": "boom"})
                return await super().get(url, **kwargs)

        client500 = _SeedPollClient()
        client500.seed_status = 500
        client_raise = _SeedPollClient()
        client_raise.raise_on_seed = True

        for client in (client500, client_raise, _DictBodyClient()):
            await _seed_resumed_session_state(
                client, client.session_id, set(), {}, {}, set(),
            )


class TestResumedSessionPollingReplay:
    """REQ-1 (bridge-cycle-8): a resumed pinned session seeds its historical
    part state before the prompt, so the polling fallback never replays
    history or re-surfaces a stale question; fresh sessions never seed."""

    @pytest.mark.asyncio
    async def test_resumed_session_never_replays_history(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Scenario-1: seeded history (old text, reasoning, completed tool,
        resolved question) is NOT replayed after bus closure; only the new
        turn's deltas stream, and the stream ends normally."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)

        old_msg = _assistant_msg([
            {"id": "prt_old_text", "messageID": "msg_old", "type": "text",
             "text": "old answer text"},
            {"id": "prt_old_reason", "messageID": "msg_old", "type": "reasoning",
             "text": "old chain of thought"},
            {"id": "prt_old_tool", "messageID": "msg_old", "type": "tool",
             "tool": "bash", "state": {"status": "completed"}},
            {"id": "prt_old_q", "messageID": "msg_old", "type": "tool",
             "tool": "question",
             "state": {"status": "running", "input": {"question": "old?"}}},
        ])
        old_msg["id"] = "msg_old"
        new_msg = _assistant_msg([
            {"id": "prt_new_text", "messageID": "msg_new", "type": "text",
             "text": "new answer text"},
        ])
        new_msg["id"] = "msg_new"

        client = _SeedPollClient()
        client.seed_messages = [old_msg]
        client.poll_messages = [old_msg, new_msg]
        client.stream_lines = []
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {"conv-1": "ses_0001"}
        deltas = [d async for d in opencode_chat_stream(
            "answer", session_map=smap, session_key="conv-1",
        )]

        joined = "".join(t for _, t in deltas)
        kinds = [k for k, _ in deltas]
        # Historical content is never replayed.
        assert "old answer text" not in joined
        assert "old chain of thought" not in joined
        assert "🧠 thinking…" not in joined
        assert "✅" not in joined
        assert "⚠️" not in joined
        assert "question" not in kinds
        # The new turn's delta streams.
        assert "new answer text" in joined
        # The stream ends normally — no network-error status.
        assert "[OpenCode Bridge Network Error" not in joined

    @pytest.mark.asyncio
    async def test_new_question_part_still_stops_stream(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Scenario-4: a question part that did NOT exist at stream start
        (fresh pid, post-prompt) must still yield ("question", ...) and
        stop the stream — only SEEDED (already-resolved) question parts are
        suppressed."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)

        old_q_msg = _assistant_msg([
            {"id": "prt_old_q", "messageID": "msg_old", "type": "tool",
             "tool": "question",
             "state": {"status": "completed", "input": {"question": "old?"}}},
        ])
        old_q_msg["id"] = "msg_old"
        new_q_msg = _assistant_msg([
            {"id": "prt_new_q", "messageID": "msg_new", "type": "tool",
             "tool": "question",
             "state": {"status": "running", "input": {"questions": [
                 {"question": "Which source?", "options": [{"label": "Logs"}, {"label": "Git"}]}]}}},
        ])
        new_q_msg["id"] = "msg_new"

        client = _SeedPollClient()
        client.seed_messages = [old_q_msg]  # old resolved question seeded
        client.poll_messages = [new_q_msg]  # NEW question part (fresh pid)
        client.stream_lines = []
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {"conv-1": "ses_0001"}
        deltas = [d async for d in opencode_chat_stream(
            "answer", session_map=smap, session_key="conv-1",
        )]

        # The NEW question yields and stops the stream; the old seeded one
        # never replays.
        assert deltas == [("question", "Which source? (Options: Logs | Git)")], deltas
        assert smap == {"conv-1": "ses_0001"}

    @pytest.mark.asyncio
    async def test_seed_failure_degrades_to_current_behavior(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Scenario-2: a failed seed GET (HTTP 500 or a raising GET) must
        not raise — the stream completes and polling degrades to the
        existing unseeded behavior."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)

        for seed_status, raise_on_seed in [(500, False), (200, True)]:
            client = _SeedPollClient()
            client.seed_status = seed_status
            client.raise_on_seed = raise_on_seed
            client.poll_messages = []
            client.stream_lines = []
            monkeypatch.setattr(
                opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client,
            )

            smap: dict[str, str] = {"conv-1": "ses_0001"}
            deltas = [d async for d in opencode_chat_stream(
                "answer", session_map=smap, session_key="conv-1",
            )]
            joined = "".join(t for _, t in deltas)
            assert "[OpenCode Bridge Network Error" not in joined

    @pytest.mark.asyncio
    async def test_fresh_session_does_not_seed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Scenario-3: with no session pin, NO seed GET occurs before the
        prompt POST — the first message-list GET is the polling read."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        client = _FakeClient()
        client.stream_lines = []
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        [d async for d in opencode_chat_stream(
            "task", session_map=smap, session_key="conv-1",
        )]

        # The prompt POST fired (fresh session created + pinned).
        prompt_idx = next(
            i for i, (m, u) in enumerate(client.calls)
            if m == "post" and "prompt_async" in u
        )
        # The first message-list GET (no trailing slash) is a POLLING read:
        # it must come after the prompt POST — no pre-prompt seed GET.
        list_get_idxs = [
            i for i, (m, u) in enumerate(client.calls)
            if m == "get" and u.endswith(f"/session/{client.session_id}/message")
        ]
        assert list_get_idxs, "expected a polling message-list GET"
        assert list_get_idxs[0] > prompt_idx


class TestParsePermissionAnswer:
    """_parse_permission_answer maps the user's reply to a permission
    response value understood by POST /session/{id}/permissions/{pid}."""

    @pytest.mark.parametrize("answer", ["always", "allow always", "ALWAYS", "always allow"])
    def test_always(self, answer: str) -> None:
        from opencode_bridge import _parse_permission_answer
        assert _parse_permission_answer(answer) == "always"

    @pytest.mark.parametrize("answer", ["reject", "no", "deny", "No thanks", "reject it"])
    def test_reject(self, answer: str) -> None:
        from opencode_bridge import _parse_permission_answer
        assert _parse_permission_answer(answer) == "reject"

    @pytest.mark.parametrize("answer", ["allow", "yes", "y", "ok", "go ahead", ""])
    def test_once(self, answer: str) -> None:
        from opencode_bridge import _parse_permission_answer
        assert _parse_permission_answer(answer) == "once"

    @pytest.mark.parametrize("reply,expected", [
        ("allow", "once"),
        ("always", "always"),
        ("yes go ahead", "once"),
        ("reject", "reject"),
        ("no", "reject"),
        ("continue", "reject"),      # unrelated follow-up must NOT grant
        ("", "reject"),              # empty reply must NOT grant
        ("write the docs", "reject"),  # a new task must NOT grant a write
    ])
    def test_parse_permission_answer_strict_write(self, reply: str, expected: str) -> None:
        """WRITE-class permissions default to reject: only an explicit
        allow/always grants.  An unrelated follow-up or bare 'continue'
        must never authorize an external write (review finding 3)."""
        from opencode_bridge import _parse_permission_answer
        assert _parse_permission_answer(reply, write=True) == expected


class TestPostPermissionResponse:
    """The permission-answer POST helper survives the serve-transport
    retirement: it still posts ``{"response": ...}`` to the active
    backend's permission endpoint.  The bridge itself never reaches it now
    (the secure server's ``opencode-perm`` resolves prompts out-of-band),
    but the helper stays part of the backend contract."""

    @pytest.mark.asyncio
    async def test_post_permission_response_posts_body(
        self, fake_client: _FakeClient,
    ) -> None:
        """_post_permission_response POSTs {"response": ...} to the active
        backend's permission endpoint and reports success on 200."""
        from constants import OPENCODE_SECURE_SERVE_URL
        from opencode_bridge import _post_permission_response

        ok = await _post_permission_response("ses_0001", "perm_1", "once")
        assert ok is True
        assert (
            f"{OPENCODE_SECURE_SERVE_URL}/session/ses_0001/permissions/perm_1",
            {"response": "once"},
        ) in fake_client.post_calls

    @pytest.mark.asyncio
    async def test_post_permission_response_error_degrades(
        self, fake_client: _FakeClient,
    ) -> None:
        """A network error while answering a permission degrades to False
        (the pinned continuation still runs), never raises."""
        from opencode_bridge import _post_permission_response

        fake_client.raise_on = "post"
        ok = await _post_permission_response("ses_0001", "perm_1", "always")
        assert ok is False


# ---------------------------------------------------------------------------
# Secure-backend permission gate (T5, ct-58464)
# ---------------------------------------------------------------------------

async def _ensure_true(*args: Any, **kwargs: Any) -> bool:
    """Async True stub for ``ensure_opencode_serve``."""
    return True


async def _drain_bounded(
    stream: AsyncIterator[tuple[str, str]], *, timeout: float = 2.5,
    stop_on: Any = None,
) -> list[tuple[str, str]]:
    """Collect a bridge stream for at most ``timeout`` seconds.

    The secure-backend stream stays alive while a permission is pending
    (prompts resolve out-of-band), so a test cannot await full completion;
    it bounds the collection and then cancels the generator.  ``stop_on``
    ends the collection early once the collected deltas satisfy it."""
    out: list[tuple[str, str]] = []

    async def _collect() -> None:
        async for item in stream:
            out.append(item)
            if stop_on is not None and stop_on(out):
                return

    task = asyncio.create_task(_collect())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return out


def _saw_waiting_status(deltas: list[tuple[str, str]]) -> bool:
    """``stop_on`` predicate for ``_drain_bounded``: True once the drained
    deltas contain the secure-backend waiting-permission status.

    Matches the production ``_WAITING_PERMISSION_STATUS`` text instead of a
    hardcoded guess so the stop predicate cannot drift from what the bridge
    actually emits (the secure stream stays open while a permission is
    pending, so the drain must end on that status)."""
    return any(
        kind == "status"
        and opencode_bridge._WAITING_PERMISSION_STATUS in text
        for kind, text in deltas
    )


def _pending_write_perm() -> dict[str, Any]:
    return {
        "id": "perm_w", "sessionID": "ses_0001",
        "permission": "write",
        "patterns": ["/home/user/out.txt"],
        "tool": {"messageID": "msg_a", "callID": "call_w"},
    }


class _SecurePermissionClient(_FakeClient):
    """_FakeClient scripting for the secure-backend permission tests: a
    finite or blocking /event bus plus scripted message-list / session-status
    / permission reads for the polling paths."""

    def __init__(self, *, blocking: bool = False) -> None:
        super().__init__()
        self.blocking = blocking
        self.lines: list[str] = []
        self.poll_messages: list[dict[str, Any]] = []
        self.status_type = "idle"
        self.permission_records: list[dict[str, Any]] = []

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStream:
        if self.blocking:
            return _BlockingStream(self.lines)
        return _FakeStream(self.lines)

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        if url.endswith(f"/session/{self.session_id}/message"):
            return _FakeResp(200, self.poll_messages)
        if url.endswith("/permission"):
            return _FakeResp(200, self.permission_records)
        if "/session/status" in url:
            return _FakeResp(200, {self.session_id: {"type": self.status_type}})
        return await super().get(url, **kwargs)


class TestSecurePermissionGate:
    """T5 (ct-58464): the secure backend answers NO permission — no relay,
    no pending entry, no POST — and reports the wait on the stream.  With
    the answering transports retired, this is the only permission policy
    the bridge can exhibit."""

    @pytest.mark.asyncio
    async def test_secure_sse_event_reports_wait_no_relay_no_post(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SSE path (permission.updated): the secure backend emits the
        waiting status instead of a question and POSTs nothing."""
        from opencode_bridge import opencode_chat_stream
        from opencode_backends import SecureBackend

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure_true)
        monkeypatch.setattr(opencode_bridge, "BACKEND", SecureBackend())
        client = _SecurePermissionClient()
        client.lines = [
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_user", "role": "user"}),
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_a", "role": "assistant"}),
            _evt("permission.updated", sessionID="ses_0001",
                 id="perm_w", type="write",
                 title="Allow writing to /home/user/out.txt",
                 metadata={"filepath": "/home/user/out.txt"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = await _drain_bounded(
            opencode_chat_stream("task", pending_permissions=PP),
            stop_on=_saw_waiting_status,
        )

        assert any(
            k == "status" and "waiting on a permission" in t for k, t in deltas
        ), deltas
        assert [k for k, _ in deltas if k == "question"] == []
        assert [u for u, _ in client.post_calls if "/permissions/" in u] == []
        assert PP.get("ses_0001") is None

    @pytest.mark.asyncio
    async def test_secure_polling_path_reports_wait_once_no_post(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Polling wedge path (GET /permission): the waiting status is
        emitted ONCE (deduped) and no question/POST fires."""
        from opencode_bridge import opencode_chat_stream
        from opencode_backends import SecureBackend

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure_true)
        monkeypatch.setattr(opencode_bridge, "BACKEND", SecureBackend())
        monkeypatch.setattr(opencode_bridge, "_WEDGE_CHECK_INTERVAL_S", 0.01)
        monkeypatch.setattr(opencode_bridge, "_TOOL_WEDGE_AFTER_S", 60.0)
        client = _SecurePermissionClient()
        client.lines = []  # bus closes → polling fallback
        client.status_type = "busy"
        client.permission_records = [_pending_write_perm()]
        client.poll_messages = [_assistant_msg([
            {"id": "prt_w", "type": "tool", "tool": "write",
             "state": {"status": "running",
                       "input": {"filePath": "/home/user/out.txt"},
                       "time": {"start": 1786124255392}}},
        ])]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = await _drain_bounded(
            opencode_chat_stream("task", pending_permissions=PP),
            stop_on=_saw_waiting_status,
        )

        waiting = [
            t for k, t in deltas
            if k == "status" and "waiting on a permission" in t
        ]
        assert len(waiting) == 1, deltas  # emitted ONCE per pending permission
        assert [k for k, _ in deltas if k == "question"] == []
        assert [u for u, _ in client.post_calls if "/permissions/" in u] == []
        assert PP.get("ses_0001") is None

    @pytest.mark.asyncio
    async def test_secure_idle_completion_resolver_reports_wait(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Idle-completion resolver: a parked session (idle, no running
        tool) is NOT declared done under a non-answering backend; it reports
        the wait and keeps polling."""
        from opencode_bridge import opencode_chat_stream
        from opencode_backends import SecureBackend

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure_true)
        monkeypatch.setattr(opencode_bridge, "BACKEND", SecureBackend())
        monkeypatch.setattr(opencode_bridge, "_WEDGE_CHECK_INTERVAL_S", 60.0)
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.02)
        client = _SecurePermissionClient()
        client.lines = []
        client.status_type = "idle"
        client.poll_messages = []  # no running tool → idle resolver owns it
        client.permission_records = [_pending_write_perm()]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = await _drain_bounded(
            opencode_chat_stream("task", pending_permissions=PP),
            stop_on=_saw_waiting_status,
        )

        assert any(
            k == "status" and "waiting on a permission" in t for k, t in deltas
        ), deltas
        assert [k for k, _ in deltas if k == "question"] == []
        assert [u for u, _ in client.post_calls if "/permissions/" in u] == []
        assert PP.get("ses_0001") is None

    @pytest.mark.asyncio
    async def test_secure_bus_timeout_wedge_check_reports_wait(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Event-bus-timeout wedge check: an open bus with a pending
        permission reports the wait instead of relaying or killing."""
        from opencode_bridge import opencode_chat_stream
        from opencode_backends import SecureBackend

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure_true)
        monkeypatch.setattr(opencode_bridge, "BACKEND", SecureBackend())
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.02)
        monkeypatch.setattr(opencode_bridge, "_WEDGE_CHECK_INTERVAL_S", 0.01)
        monkeypatch.setattr(opencode_bridge, "_TOOL_WEDGE_AFTER_S", 60.0)
        client = _SecurePermissionClient(blocking=True)
        client.lines = []
        client.status_type = "busy"
        client.permission_records = [_pending_write_perm()]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = await _drain_bounded(
            opencode_chat_stream("task", pending_permissions=PP),
            stop_on=_saw_waiting_status,
        )

        assert any(
            k == "status" and "waiting on a permission" in t for k, t in deltas
        ), deltas
        assert [k for k, _ in deltas if k == "question"] == []
        assert [u for u, _ in client.post_calls if "/permissions/" in u] == []
        assert PP.get("ses_0001") is None

    @pytest.mark.asyncio
    async def test_secure_blocking_path_does_not_abort(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Blocking path (opencode_chat): a pending permission does not
        abort the session nor fail the request — the prompt resolves
        out-of-band and the request keeps waiting."""
        from opencode_bridge import opencode_chat
        from opencode_backends import SecureBackend

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure_true)
        monkeypatch.setattr(opencode_bridge, "BACKEND", SecureBackend())
        monkeypatch.setattr(opencode_bridge, "_BLOCKING_PERMISSION_POLL_S", 0.01)
        client = _SecurePermissionClient()
        client.message_hang_s = 0.2
        client.message_parts = [{"type": "text", "text": "done"}]
        client.permission_records = [_pending_write_perm()]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        result = await opencode_chat("task")

        assert result == "done"
        assert not any(u.endswith("/abort") for u, _ in client.post_calls)
        assert [u for u, _ in client.post_calls if "/permissions/" in u] == []

# ---------------------------------------------------------------------------
# Pinned sessions + clarifying questions (a701e8d)
# ---------------------------------------------------------------------------
class TestPinnedSessionAndQuestions:
    @pytest.mark.asyncio
    async def test_pinned_session_reuse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Follow-ups reuse the pinned opencode session (no second create)."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        client = _FakeClient()
        client.stream_lines = _stream_events()
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        # First call: creates + pins the session.
        [d async for d in opencode_chat_stream("task", session_map=smap, session_key="conv-1")]
        assert smap.get("conv-1") == "ses_0001"
        creates = [u for m, u in client.calls if m == "post" and u.endswith("/session")]
        assert len(creates) == 1

        client.calls.clear()
        client.stream_lines = _stream_events()
        # Second call: reuses the pinned session — no new /session POST.
        [d async for d in opencode_chat_stream("answer", session_map=smap, session_key="conv-1")]
        creates = [u for m, u in client.calls if m == "post" and u.endswith("/session")]
        assert creates == []
        assert smap["conv-1"] == "ses_0001"

    @pytest.mark.asyncio
    async def test_question_yields_text_and_stops(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A question tool part yields ('question', text) with the options
        and the stream stops — even when the event omits the input (the
        retry-race fetch pulls it from the persisted part)."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        client = _FakeClient()
        # Persisted part (returned by the retry fetch) carries the question.
        client.message_get_parts = [{
            "id": "prt_q", "messageID": "msg_a", "type": "tool", "tool": "question",
            "state": {"status": "running", "input": {"questions": [{
                "question": "Which source?",
                "options": [{"label": "Logs"}, {"label": "Git"}],
            }]}},
        }]
        client.stream_lines = [
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_user", "role": "user"}),
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_a", "role": "assistant"}),
            # Event omits the input (the race the retry handles).
            _evt("message.part.updated", sessionID="ses_0001",
                 part={"id": "prt_q", "messageID": "msg_a", "type": "tool",
                       "tool": "question", "state": {"status": "running"}}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]
        assert deltas == [("question", "Which source? (Options: Logs | Git)")]

    @pytest.mark.asyncio
    async def test_multigroup_preflight_relays_every_group(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A multi-group question (SDD Session Preflight: Pace / Artifacts /
        PRs / Review) must be relayed LOSSESSLY — every group in order with
        its header, body, and option labels.  Regression: the renderer only
        took questions[0], so the other three groups vanished and the
        bridge emitted a bare "Could you clarify?"."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        client = _FakeClient()
        client.message_get_parts = [{
            "id": "prt_q", "messageID": "msg_a", "type": "tool", "tool": "question",
            "state": {"status": "running", "input": {"questions": [
                {"header": "Pace", "question": "How should the SDD phases run?",
                 "options": [{"label": "Automatic (Recommended)"},
                             {"label": "Interactive"}]},
                {"header": "Artifacts", "question": "Where should the SDD artifacts live?",
                 "options": [{"label": "Engram"}, {"label": "OpenSpec"},
                             {"label": "Both"}]},
                {"header": "PRs", "question": "How should PRs be handled?",
                 "options": [{"label": "Ask me"}, {"label": "Single PR"},
                             {"label": "Auto"}]},
                {"header": "Review", "question": "What review budget?",
                 "options": [{"label": "400 lines"}, {"label": "800 lines"}]},
            ]}},
        }]
        client.stream_lines = [
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_user", "role": "user"}),
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_a", "role": "assistant"}),
            _evt("message.part.updated", sessionID="ses_0001",
                 part={"id": "prt_q", "messageID": "msg_a", "type": "tool",
                       "tool": "question",
                       "state": {"status": "running",
                                 "input": {"questions": client.message_get_parts[0]["state"]["input"]["questions"]}}}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]
        assert len(deltas) == 1
        kind, text = deltas[0]
        assert kind == "question"
        assert "Pace: How should the SDD phases run?" in text
        assert "Artifacts: Where should the SDD artifacts live?" in text
        assert "PRs: How should PRs be handled?" in text
        assert "Review: What review budget?" in text
        assert "Automatic (Recommended)" in text
        assert "OpenSpec" in text
        assert "800 lines" in text
        # Order preserved: Pace comes before Review.
        assert text.index("Pace:") < text.index("Review:")

    @pytest.mark.asyncio
    async def test_tool_feedback_tuples(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Running/completed tool parts yield sentinel status tuples."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        client = _FakeClient()
        client.stream_lines = [
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_user", "role": "user"}),
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_a", "role": "assistant"}),
            _evt("message.part.updated", sessionID="ses_0001",
                 part={"id": "prt_t1", "messageID": "msg_a", "type": "tool",
                       "tool": "write", "state": {"status": "running"}}),
            _evt("message.part.updated", sessionID="ses_0001",
                 part={"id": "prt_t1", "messageID": "msg_a", "type": "tool",
                       "tool": "write", "state": {"status": "completed"}}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]
        assert ("status", "🔧 write…\n") in deltas
        assert ("status", "✅ write done\n") in deltas














# ---------------------------------------------------------------------------
# Wedged-tool detection (tool part stuck in "running" with no output)
# ---------------------------------------------------------------------------
class TestDetectWedgedTool:
    @pytest.mark.asyncio
    async def test_old_running_bash_part_without_output_is_wedged(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bash part running with no output for past the wedge threshold
        is detected as wedged."""
        from opencode_bridge import _detect_wedged_tool

        part_start = int(time.time() * 1000) - 320_000
        client = _PollClient()
        client.poll_messages = [_assistant_msg([{
            "id": "prt_bash", "messageID": "msg_a", "type": "tool",
            "tool": "bash",
            "state": {
                "status": "running",
                "time": {"start": part_start},
            },
        }])]

        assert await _detect_wedged_tool(client, client.session_id) is True

    @pytest.mark.asyncio
    async def test_recent_start_not_wedged(self) -> None:
        """A running tool started just now is real work, not a wedge."""
        from opencode_bridge import _detect_wedged_tool

        client = _PollClient()
        client.poll_messages = [_assistant_msg([{
            "id": "prt_bash", "messageID": "msg_a", "type": "tool",
            "tool": "bash",
            "state": {
                "status": "running",
                "time": {"start": int(time.time() * 1000) - 5_000},
            },
        }])]

        assert await _detect_wedged_tool(client, client.session_id) is False

    @pytest.mark.asyncio
    async def test_running_with_output_not_wedged(self) -> None:
        """A running tool that IS producing output is healthy even past the
        age threshold."""
        from opencode_bridge import _detect_wedged_tool

        client = _PollClient()
        client.poll_messages = [_assistant_msg([{
            "id": "prt_bash", "messageID": "msg_a", "type": "tool",
            "tool": "bash",
            "state": {
                "status": "running",
                "output": "downloading deps…",
                "time": {"start": int(time.time() * 1000) - 180_000},
            },
        }])]

        assert await _detect_wedged_tool(client, client.session_id) is False

    @pytest.mark.asyncio
    async def test_question_tool_not_wedged(self) -> None:
        """The "question" tool sits in "running" BY DESIGN while waiting
        for user input — never a wedge."""
        from opencode_bridge import _detect_wedged_tool

        client = _PollClient()
        client.poll_messages = [_assistant_msg([{
            "id": "prt_q", "messageID": "msg_a", "type": "tool",
            "tool": "question",
            "state": {
                "status": "running",
                "time": {"start": int(time.time() * 1000) - 180_000},
            },
        }])]

        assert await _detect_wedged_tool(client, client.session_id) is False

    @pytest.mark.asyncio
    async def test_no_tool_parts_not_wedged(self) -> None:
        """A session with only text parts is never wedged."""
        from opencode_bridge import _detect_wedged_tool

        client = _PollClient()
        client.poll_messages = [_assistant_msg([{
            "id": "prt_t", "messageID": "msg_a", "type": "text", "text": "hi",
        }])]

        assert await _detect_wedged_tool(client, client.session_id) is False

    @pytest.mark.asyncio
    async def test_http_error_not_wedged(self) -> None:
        """A client error while probing degrades to False, never raises."""
        from opencode_bridge import _detect_wedged_tool

        client = _FakeClient()
        client.raise_on = "get"

        assert await _detect_wedged_tool(client, client.session_id) is False


class TestWedgedToolStream:
    @pytest.mark.asyncio
    async def test_wedged_tool_aborts_and_drops_pin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wedged tool part terminates the stream with a clear error: the
        session is aborted and the pin dropped.  The SERVE IS NEVER KILLED
        — it hosts other concurrent sessions, and recycling it for one
        wedged tool destroys them all (2026-08-08)."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        async def _always_wedged(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_detect_wedged_tool", _always_wedged)
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.05)
        monkeypatch.setattr(opencode_bridge, "_WEDGE_CHECK_INTERVAL_S", 0.05)

        client = _BusyThenIdleClient()
        client.stream_lines = []  # empty event bus → polling fallback
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        deltas = [
            d async for d in opencode_chat_stream(
                "task", session_map=smap, session_key="conv-1",
            )
        ]

        wedged = [t for k, t in deltas if k == "status" and "wedged" in t]
        assert len(wedged) == 1
        assert "session aborted" in wedged[0]
        assert "recycled" not in wedged[0]
        # Pin dropped and the session aborted.
        assert "conv-1" not in smap
        aborts = [
            url for method, url in client.calls
            if method == "post" and "/abort" in url
        ]
        assert len(aborts) == 1


class _RunningToolPermissionClient(_FakeClient):
    """Fake whose polling read shows a RUNNING tool in the newest assistant
    message plus optionally a pending write permission."""

    def __init__(self) -> None:
        super().__init__()
        self.permission_records: list[dict[str, Any]] = []
        self.status_type = "busy"
        self.write_tool: bool = False
        self.grep_mode: bool = False

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        if url.endswith(f"/session/{self.session_id}/message"):
            if self.grep_mode:
                return _FakeResp(200, [_assistant_msg([
                    {
                        "id": "prt_grep", "type": "tool", "tool": "grep",
                        "state": {
                            "status": "running",
                            "input": {"pattern": "check.*update"},
                            "time": {"start": 1786124255392},
                        },
                    },
                ])])
            if self.write_tool:
                return _FakeResp(200, [_assistant_msg([
                    {
                        "id": "prt_write", "type": "tool", "tool": "write",
                        "state": {
                            "status": "running",
                            "input": {"filePath": "/etc/kinver-test.service",
                                      "content": "x"},
                            "time": {"start": 1786124255392},
                        },
                    },
                ])])
            return _FakeResp(200, [_assistant_msg([
                {
                    "id": "prt_running", "type": "tool", "tool": "bash",
                    "state": {
                        "status": "running",
                        "input": {"command": "cat /etc/systemd/system/x.service"},
                        "time": {"start": 1786124255392},
                    },
                },
            ])])
        if url.endswith("/permission"):
            return _FakeResp(200, self.permission_records)
        if "/message/" in url:
            return _FakeResp(200, {"info": {}, "parts": []})
        if "/session/status" in url:
            return _FakeResp(200, {self.session_id: {"type": self.status_type}})
        return await super().get(url, **kwargs)


class TestRunningToolAnnounce:
    """Command-level visibility regression (2026-08-13): the serve's SSE
    bus drops tool running-state updates, so the event-bus path must
    announce the currently running tool from the persisted message list —
    the user sees WHICH command is executing instead of bare keepalives.
    """

    @pytest.mark.asyncio
    async def test_bridge_prompt_carries_only_channel_contract(self) -> None:
        """T6 (ct-58463): the bridge prompt carries ONLY the channel
        contract — the identity/language policy, the single-stream reply
        plus turn-end/question contract, and the narration requirement.
        The WORK CONTINUITY orchestration, the serve-specific tool policy
        (glob/grep/bash wedge rules), and the reasonable-assumptions
        guidance are dropped: the server's own agent config owns
        orchestration and tool policy."""
        prompt = opencode_bridge._BRIDGE_SYSTEM_PROMPT
        # Kept channel-contract lines.
        assert "API-backed coding assistant" in prompt
        assert "Respond in English unless" in prompt
        assert "one stream, not a series of continue-pings" in prompt
        assert "End your turn only when the task is done" in prompt
        assert "clarifying question" in prompt
        assert "NARRATE (MANDATORY)" in prompt
        assert "Never run a tool in silence" in prompt
        # Dropped directives: orchestration, tool policy, assumptions.
        assert "WORK CONTINUITY" not in prompt
        assert "Never end your turn after a step" not in prompt
        assert "reasonable assumptions" not in prompt
        assert "TOOL RULES" not in prompt
        assert "glob" not in prompt
        assert "grep" not in prompt
        assert "`bash`" not in prompt
        assert "retry it ONCE" not in prompt

    @pytest.mark.asyncio
    async def test_keepalive_announces_running_tool_from_message_list(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With the event bus OPEN but silent (no part events — the drops),
        the quiet-path wedge check polls the message list and surfaces the
        running bash command as a 🔧 status chunk."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_detect_wedged_tool", lambda *a: False)
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.05)
        monkeypatch.setattr(opencode_bridge, "_EVENT_FINAL_TIMEOUT", 0.05)
        monkeypatch.setattr(opencode_bridge, "_WEDGE_CHECK_INTERVAL_S", 0.05)

        client = _RunningToolPermissionClient()
        # Bus opens, user + assistant message arrive, then SILENCE — the
        # running-state part event never arrives (upstream drop).
        client.stream_lines = [
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_u", "role": "user"}),
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_a", "role": "assistant"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        # Short total timeout: the stream must end without hanging; the
        # announce must fire before the abort.
        deltas = [
            d async for d in opencode_chat_stream(
                "task", session_map=smap, session_key="conv-1", timeout=1.0,
            )
        ]

        tools = [t for k, t in deltas if k == "status" and "🔧" in t]
        assert any("bash" in t and "cat /etc/systemd/system/x.service" in t for t in tools), tools
        # Announced exactly once (deduped by part id).
        assert len(tools) == 1, tools

    @pytest.mark.asyncio
    async def test_keepalive_announces_grep_pattern_from_message_list(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-bash tools carry no ``command`` input: the announce must
        fall back to the pattern/filePath so the user still sees WHAT the
        tool is doing (e.g. the grep pattern)."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_detect_wedged_tool", lambda *a: False)
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.05)
        monkeypatch.setattr(opencode_bridge, "_EVENT_FINAL_TIMEOUT", 0.05)
        monkeypatch.setattr(opencode_bridge, "_WEDGE_CHECK_INTERVAL_S", 0.05)

        client = _RunningToolPermissionClient()
        client.grep_mode = True
        client.stream_lines = [
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_u", "role": "user"}),
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_a", "role": "assistant"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        deltas = [
            d async for d in opencode_chat_stream(
                "task", session_map=smap, session_key="conv-1", timeout=1.0,
            )
        ]

        tools = [t for k, t in deltas if k == "status" and "🔧" in t]
        assert any("grep" in t and "check.*update" in t for t in tools), tools
        assert len(tools) == 1, tools


class _ResilientRetryClient(_FakeClient):
    """Fake whose stream() pops pre-scripted line batches per call — lets
    tests script attempt 1 to fail and attempt 2 to succeed."""

    def __init__(self) -> None:
        super().__init__()
        self.stream_batches: list[list[str]] = []

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStream:
        lines = self.stream_batches.pop(0) if self.stream_batches else []
        return _FakeStream(lines)





class _DroppedTextClient(_FakeClient):
    """Bus stays open and silent; the message list carries a text part
    whose SSE event never arrived (the upstream drop)."""

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        if url.endswith(f"/session/{self.session_id}/message"):
            return _FakeResp(200, [_assistant_msg([
                {"id": "prt_txt", "type": "text",
                 "text": "I will now inspect the log."},
            ])])
        if url.endswith("/permission"):
            return _FakeResp(200, [])
        return await super().get(url, **kwargs)


class TestCatchUpPoll:
    """While the SSE bus is open, dropped part events (upstream #35066)
    must be caught up from the message list so the user receives the
    agent's narration live instead of on the next continue."""

    @pytest.mark.asyncio
    async def test_quiet_path_delivers_dropped_text_from_message_list(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_detect_wedged_tool", lambda *a: False)
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.05)
        monkeypatch.setattr(opencode_bridge, "_EVENT_FINAL_TIMEOUT", 0.05)
        monkeypatch.setattr(opencode_bridge, "_WEDGE_CHECK_INTERVAL_S", 0.05)
        monkeypatch.setattr(opencode_bridge, "_ANNOUNCE_CHECK_INTERVAL_S", 0.05)

        client = _DroppedTextClient()
        # User + assistant message events arrive, the TEXT part's event
        # never does; the bus stays open (silence).
        client.stream_lines = [
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_u", "role": "user"}),
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_a", "role": "assistant"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        deltas = [
            d async for d in opencode_chat_stream(
                "task", session_map=smap, session_key="conv-1", timeout=1.0,
            )
        ]

        assert ("text", "I will now inspect the log.") in deltas, deltas


class _QuestionClient(_FakeClient):
    """Fake with scripted pending-question responses."""

    def __init__(self, question_entry: Optional[dict[str, Any]]) -> None:
        super().__init__()
        self.question_entry = question_entry
        self.orphan_part = False  # running question part with an OLD start

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        if url.endswith("/question"):
            return _FakeResp(200, [self.question_entry] if self.question_entry else [])
        if self.orphan_part and url.endswith(f"/session/{self.session_id}/message"):
            return _FakeResp(200, [_assistant_msg([
                {
                    "id": "prt_q", "type": "tool", "tool": "question",
                    "state": {
                        "status": "running",
                        "input": {"questions": [{"question": "Which channel?"}]},
                        "time": {"start": (time.time() - 120) * 1000},
                    },
                },
            ])])
        return await super().get(url, **kwargs)


class TestPendingQuestion:
    """The bridge must ANSWER a pending question on resume (the user's
    message is the answer, posted to /question/<id>/reply) instead of
    posting it as a new prompt into the blocked step — and must abort an
    ORPHANED question (running part with no serve-side pending request)
    that can never be answered.  Both observed live 2026-08-14: a
    'notification channel' question stayed running 8+ minutes across 5
    user messages that only got keepalives."""

    @pytest.mark.asyncio
    async def test_resume_answers_pending_question(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.05)
        monkeypatch.setattr(opencode_bridge, "_EVENT_FINAL_TIMEOUT", 0.05)

        client = _QuestionClient({
            "id": "q_0001", "sessionID": "ses_0001",
            "questions": [{"question": "Which channel?"}],
        })
        client.stream_lines = []
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {"conv-1": "ses_0001"}
        _ = [
            d async for d in opencode_chat_stream(
                "ntfy", session_map=smap, session_key="conv-1", timeout=0.5,
            )
        ]

        reply_posts = [
            (u, b) for u, b in client.post_calls
            if u.endswith("/question/q_0001/reply")
        ]
        assert len(reply_posts) == 1, client.post_calls
        assert reply_posts[0][1] == {"answers": [["ntfy"]]}
        # The answer is the question reply — NO new prompt is posted into
        # the blocked session.
        assert not any("/prompt_async" in u for u, _ in client.post_calls)

    @pytest.mark.asyncio
    async def test_resume_aborts_orphaned_question_and_starts_fresh(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.05)
        monkeypatch.setattr(opencode_bridge, "_EVENT_FINAL_TIMEOUT", 0.05)

        client = _QuestionClient(None)  # no serve-side pending request
        client.orphan_part = True
        client.stream_lines = [*_stream_events()]  # fresh session runs fine
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {"conv-1": "ses_0001"}
        deltas = [
            d async for d in opencode_chat_stream(
                "redo the task", session_map=smap, session_key="conv-1", timeout=2.0,
            )
        ]

        # The orphaned session was aborted (and the pin re-set to the
        # fresh session by the create path below).
        assert any(u.endswith("/session/ses_0001/abort") for u, _ in client.post_calls)
        # The task was re-run on a fresh session with the user text.
        prompts = [b for u, b in client.post_calls if "/prompt_async" in u]
        assert len(prompts) == 1
        assert any(
            (p.get("parts") or [{}])[0].get("text") == "redo the task"
            for p in prompts
        )
        text = [t for k, t in deltas if k == "text"]
        assert "Created file." in text and "Done." in text


class _BlockingStream(_FakeStream):
    """Yields the scripted lines then blocks forever — keeps the event
    bus OPEN so the bus path's TimeoutError handler runs (instead of the
    bus closing into the polling fallback)."""

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line
            await asyncio.sleep(0)
        await asyncio.Event().wait()


class _IdleGraceClient(_FakeClient):
    """Bus open; message list carries a text part the SSE never delivered."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def stream(self, *args: Any, **kwargs: Any) -> _BlockingStream:
        return _BlockingStream(self.lines)

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        if url.endswith(f"/session/{self.session_id}/message"):
            return _FakeResp(200, [_assistant_msg([
                {"id": "prt_new", "type": "text",
                 "text": "next step narration"},
            ])])
        if url.endswith("/permission"):
            return _FakeResp(200, [])
        return await super().get(url, **kwargs)


class TestIdleGrace:
    """The bus path must NOT return on a finished step + idle session
    while the agent is still working: multi-step turns report idle
    briefly between steps (the model generates the next action), and an
    eager return cut the stream mid-turn — the feed stopped after the
    grep step while the session produced 45 more parts unseen (observed
    live 2026-08-14).  The catch-up poll resets the grace on new content.
    """

    @pytest.mark.asyncio
    async def test_bus_path_keeps_stream_open_on_idle_with_new_content(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_detect_wedged_tool", lambda *a: False)
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.05)
        monkeypatch.setattr(opencode_bridge, "_EVENT_FINAL_TIMEOUT", 0.05)
        # Long grace: the first idle observation must NOT return; the
        # catch-up poll's new content keeps the stream alive.
        monkeypatch.setattr(opencode_bridge, "_IDLE_GRACE_S", 10.0)

        client = _IdleGraceClient()
        client.lines = [
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_u", "role": "user"}),
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_a", "role": "assistant"}),
            _evt("message.part.updated", sessionID="ses_0001",
                 part={"id": "prt_sf", "messageID": "msg_a", "type": "step-finish"}),
            _evt("session.status", sessionID="ses_0001",
                 status={"type": "idle"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [
            d async for d in opencode_chat_stream(
                "task", session_map={}, session_key="k", timeout=0.5,
            )
        ]
        # The next step's narration was delivered despite the idle event.
        assert ("text", "next step narration") in deltas, deltas
        # No misleading "still working" keepalives once the grace is
        # counting (the agent finished; the stream is waiting out the
        # grace) — the tail is silent.
        assert not any(
            k == "status" and "still working" in t for k, t in deltas
        ), deltas


class TestResilientStream:
    """``opencode_chat_stream_resilient``: one auto-retry when the first
    attempt dies silently (the serve's tool runner fails at a high rate —
    upstream defect family #35066); never retries after text or a question.
    """

    @pytest.mark.asyncio
    async def test_retries_once_after_silent_failure(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attempt 1 dies with an agent error before any output; the FULL
        task is retried once, which succeeds."""
        from opencode_bridge import opencode_chat_stream_resilient

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)

        client = _ResilientRetryClient()
        client.stream_batches = [
            # Attempt 1: the first assistant message carries an error
            # (session processing died — the upstream defect).
            [
                _evt("message.updated", sessionID="ses_0001",
                     info={"id": "msg_u", "role": "user"}),
                _evt("message.updated", sessionID="ses_0001",
                     info={"id": "msg_a", "role": "assistant",
                           "error": "model crashed"}),
            ],
            # Attempt 2: normal successful turn.
            [*_stream_events()],
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        deltas = [
            d async for d in opencode_chat_stream_resilient(
                "task", session_map=smap, session_key="conv-1",
            )
        ]

        text = [t for k, t in deltas if k == "text"]
        assert "Created file." in text
        assert "Done." in text
        retry_status = [t for k, t in deltas if k == "status" and "retrying once" in t]
        assert len(retry_status) == 1
        # One session POST per attempt — the retry ran the full task again.
        posts = [u for u, _ in client.post_calls if u.endswith("/session")]
        assert len(posts) == 2

    @pytest.mark.asyncio
    async def test_retry_also_fails_surfaces_second_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the retry also fails, the second error is surfaced and no
        third attempt is made."""
        from opencode_bridge import opencode_chat_stream_resilient

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)

        client = _ResilientRetryClient()
        client.stream_batches = [
            [_evt("message.updated", sessionID="ses_0001",
                  info={"id": "msg_u", "role": "user"}),
             _evt("message.updated", sessionID="ses_0001",
                  info={"id": "msg_a", "role": "assistant", "error": "crashed"})],
            [_evt("message.updated", sessionID="ses_0001",
                  info={"id": "msg_u", "role": "user"}),
             _evt("message.updated", sessionID="ses_0001",
                  info={"id": "msg_a", "role": "assistant", "error": "crashed again"})],
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [
            d async for d in opencode_chat_stream_resilient(
                "task", session_map={}, session_key="conv-1",
            )
        ]
        errors = [t for k, t in deltas if k == "status" and "OpenCode Bridge Error" in t]
        assert len(errors) == 1 and "crashed again" in errors[0]
        posts = [u for u, _ in client.post_calls if u.endswith("/session")]
        assert len(posts) == 2  # never a third attempt

    @pytest.mark.asyncio
    async def test_no_retry_after_text_delivered(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failure AFTER assistant text is surfaced as-is — a retry would
        duplicate the partial work."""
        from opencode_bridge import opencode_chat_stream_resilient

        calls = 0

        async def _fail_after_text(*args: Any, **kwargs: Any):
            nonlocal calls
            calls += 1
            yield "text", "partial answer"
            yield "status", "[OpenCode Bridge Error: wedged — session aborted. Please retry.]"

        monkeypatch.setattr(opencode_bridge, "opencode_chat_stream", _fail_after_text)

        deltas = [d async for d in opencode_chat_stream_resilient("task")]
        assert ("text", "partial answer") in deltas
        assert any(
            k == "status" and "OpenCode Bridge Error" in t for k, t in deltas
        )
        assert calls == 1

    @pytest.mark.asyncio
    async def test_no_retry_after_question(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A clarifying question stops the stream and is never retried."""
        from opencode_bridge import opencode_chat_stream_resilient

        calls = 0

        async def _question(*args: Any, **kwargs: Any):
            nonlocal calls
            calls += 1
            yield "question", "Which source?"

        monkeypatch.setattr(opencode_bridge, "opencode_chat_stream", _question)

        deltas = [d async for d in opencode_chat_stream_resilient("task")]
        assert deltas == [("question", "Which source?")]
        assert calls == 1


class TestCompletionResolvesPermissions:
    """Root-cause regression (2026-08-07): the bridge used to pop pending
    permissions without answering them on completion, so the serve parked
    the tool forever and the next follow-up's wedge detector killed the
    session.  Completion must not fire while a running tool exists in the
    newest message."""

    @pytest.mark.asyncio
    async def test_completion_does_not_fire_with_running_tool(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A running tool in the newest message must keep the stream alive —
        NOT declare the session done (regression: stream ended 4s into a
        wedged bash run).  The stream must emit keepalives while the tool
        runs; it must not return early."""
        import asyncio as _asyncio

        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.02)
        monkeypatch.setattr(opencode_bridge, "_TOOL_WEDGE_AFTER_S", 60.0)
        client = _RunningToolPermissionClient()
        client.stream_lines = []  # force polling fallback
        client.permission_records = []  # no pending permission
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        keepalives: list[str] = []

        async def _collect() -> None:
            async for kind, text in opencode_chat_stream("task"):
                if kind == "status" and "still working" in text:
                    keepalives.append(text)

        collector = _asyncio.create_task(_collect())
        try:
            await _asyncio.wait_for(
                _asyncio.shield(collector),
                timeout=1.5,
            )
        except _asyncio.TimeoutError:
            pass
        finally:
            collector.cancel()
            try:
                await collector
            except _asyncio.CancelledError:
                pass

        # The running tool suppressed completion: the stream was still alive
        # after 1.5s and emitting keepalives.
        assert len(keepalives) >= 1, "stream ended while tool running"

class TestSddSubagentContract:
    """The SDD sub-agent contract is the single source of truth for the
    bounded delegation rule (artifact-check-first, retry-once, inline
    fallback, loud terminal failure).  The retired serve-config template
    that used to mirror it no longer exists (bridge v4 T9-U3), so the
    canonical text is asserted directly here."""

    def test_contract_has_bounded_rule_and_marker(self) -> None:
        c = opencode_bridge._SDD_SUBAGENT_CONTRACT
        assert "RETRY the " in c and "ONCE" in c
        assert "CHECK the phase artifact on disk FIRST" in c
        assert "perform that phase INLINE" in c
        assert "SDD-CYCLE-TERMINAL-FAILURE" in c
        assert "never start a retry while the original sub-agent session" in c
        # The old unbounded rule is gone from the canonical text.
        assert "DO NOT retry the sub-agent" not in c


class TestWedgeReplayCarveOut:
    """The 600s task-part wedge must not fire while the fallback log
    shows a replay in flight (a replayed turn can run 5-20 min with the
    task part silent); non-task wedges stay untouched."""

    @staticmethod
    def _parts(task_running_for_s: int) -> list[dict]:
        import time as _t
        return [{
            "type": "tool",
            "tool": "task",
            "state": {
                "status": "running",
                "time": {"start": int((_t.time() - task_running_for_s) * 1000)},
            },
        }]

    @staticmethod
    def _client_with(parts: list[dict]) -> _PollClient:
        client = _PollClient()
        client.poll_messages = [{"parts": parts}]
        return client

    @pytest.mark.asyncio
    async def test_wedged_task_with_replay_in_flight_is_paused(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text(
            json.dumps({
                "timestamp": "2099-01-01T00:00:00.000Z",
                "event": "fallback_cycle_started",
                "sessionID": "ses_child",
            })
        )
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        monkeypatch.setattr(opencode_bridge, "_TASK_WEDGE_AFTER_S", 60.0)
        client = self._client_with(self._parts(task_running_for_s=120))
        assert await opencode_bridge._detect_wedged_tool(client, "ses_0001") is False

    @pytest.mark.asyncio
    async def test_wedged_task_without_replay_still_fires(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text(
            json.dumps({
                "timestamp": "2000-01-01T00:00:00.000Z",  # stale window
                "event": "fallback_cycle_started",
                "sessionID": "ses_child",
            })
        )
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        monkeypatch.setattr(opencode_bridge, "_TASK_WEDGE_AFTER_S", 60.0)
        client = self._client_with(self._parts(task_running_for_s=120))
        assert await opencode_bridge._detect_wedged_tool(client, "ses_0001") is True

    @pytest.mark.asyncio
    async def test_wedged_bash_fires_even_with_replay(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text(
            json.dumps({
                "timestamp": "2099-01-01T00:00:00.000Z",
                "event": "fallback_cycle_started",
                "sessionID": "ses_child",
            })
        )
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        monkeypatch.setattr(opencode_bridge, "_TOOL_WEDGE_AFTER_S", 60.0)
        parts = [{
            "type": "tool",
            "tool": "bash",
            "state": {
                "status": "running",
                "time": {"start": int((time.time() - 120) * 1000)},
            },
        }]
        client = self._client_with(parts)
        assert await opencode_bridge._detect_wedged_tool(client, "ses_0001") is True

    @pytest.mark.asyncio
    async def test_unreadable_replay_log_fails_closed(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text("garbage not json\n")
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        monkeypatch.setattr(opencode_bridge, "_TASK_WEDGE_AFTER_S", 60.0)
        client = self._client_with(self._parts(task_running_for_s=120))
        # Fails closed: today's wedge behavior (fire).
        assert await opencode_bridge._detect_wedged_tool(client, "ses_0001") is True





class TestStallAwareAbort:
    """A task part whose child session has dropped out of the live status
    map means the sub-agent finished but the delivery is stuck — the
    wedge fires early (before the 600s threshold) so the orchestrator's
    artifact-check proceeds without redoing the phase."""

    @staticmethod
    def _parts(task_running_for_s: int, child_id: str | None) -> list[dict]:
        import time as _t

        meta: dict[str, object] = {}
        if child_id:
            meta = {"metadata": {"sessionId": child_id}}
        return [{
            "type": "tool",
            "tool": "task",
            "state": {
                "status": "running",
                "time": {"start": int((_t.time() - task_running_for_s) * 1000)},
                **meta,
            },
        }]

    @pytest.mark.asyncio
    async def test_stalled_child_fires_early(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text("")  # no replay window
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        monkeypatch.setattr(opencode_bridge, "_TASK_WEDGE_AFTER_S", 600.0)
        monkeypatch.setattr(opencode_bridge, "_TASK_STALL_AFTER_S", 60.0)
        client = _PollClient()
        client.session_id = "ses_parent"
        client.poll_messages = [{"parts": self._parts(120, "ses_child")}]
        client.status_map = {"ses_parent": {"type": "busy"}}  # child NOT live
        assert await opencode_bridge._detect_wedged_tool(client, "ses_parent") is True

    @pytest.mark.asyncio
    async def test_live_child_does_not_fire_early(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text("")
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        monkeypatch.setattr(opencode_bridge, "_TASK_WEDGE_AFTER_S", 600.0)
        monkeypatch.setattr(opencode_bridge, "_TASK_STALL_AFTER_S", 60.0)
        client = _PollClient()
        client.session_id = "ses_parent"
        client.poll_messages = [{"parts": self._parts(120, "ses_child")}]
        client.status_map = {"ses_parent": {"type": "busy"},
                             "ses_child": {"type": "busy"}}  # child live
        assert await opencode_bridge._detect_wedged_tool(client, "ses_parent") is False

    @pytest.mark.asyncio
    async def test_fresh_task_does_not_fire_early(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text("")
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        monkeypatch.setattr(opencode_bridge, "_TASK_WEDGE_AFTER_S", 600.0)
        monkeypatch.setattr(opencode_bridge, "_TASK_STALL_AFTER_S", 60.0)
        client = _PollClient()
        client.session_id = "ses_parent"
        client.poll_messages = [{"parts": self._parts(10, "ses_child")}]
        client.status_map = {"ses_parent": {"type": "busy"}}
        assert await opencode_bridge._detect_wedged_tool(client, "ses_parent") is False

    @pytest.mark.asyncio
    async def test_no_child_id_waits_for_threshold(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text("")
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        monkeypatch.setattr(opencode_bridge, "_TASK_WEDGE_AFTER_S", 60.0)
        monkeypatch.setattr(opencode_bridge, "_TASK_STALL_AFTER_S", 60.0)
        client = _PollClient()
        client.session_id = "ses_parent"
        client.poll_messages = [{"parts": self._parts(120, None)}]
        client.status_map = {"ses_parent": {"type": "busy"}}
        assert await opencode_bridge._detect_wedged_tool(client, "ses_parent") is True


class TestStallAwareChildWedge:
    """The stall-abort also fires when the CHILD's own tool runner is
    wedged (a running tool with no output past the threshold) — the
    child will never finish, so the delivery is stuck with it."""

    @staticmethod
    def _parent_parts(task_running_for_s: int, child_id: str) -> list[dict]:
        import time as _t
        return [{
            "type": "tool",
            "tool": "task",
            "state": {
                "status": "running",
                "metadata": {"sessionId": child_id},
                "time": {"start": int((_t.time() - task_running_for_s) * 1000)},
            },
        }]

    @pytest.mark.asyncio
    async def test_wedged_child_fires_early(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import time as _t
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text("")
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        monkeypatch.setattr(opencode_bridge, "_TASK_WEDGE_AFTER_S", 600.0)
        monkeypatch.setattr(opencode_bridge, "_TOOL_WEDGE_AFTER_S", 60.0)
        monkeypatch.setattr(opencode_bridge, "_TASK_STALL_AFTER_S", 60.0)

        class Client(_PollClient):
            def __init__(self) -> None:
                super().__init__()
                self.session_id = "ses_parent"
                self.child_messages: list[dict] = []

            async def get(self, url: str, **kw: object) -> _FakeResp:
                if url.endswith("/session/ses_parent/message"):
                    return _FakeResp(
                        200, [{"parts":
                               TestStallAwareChildWedge._parent_parts(
                                   120, "ses_child")}],
                    )
                if url.endswith("/session/ses_child/message"):
                    return _FakeResp(200, self.child_messages)
                return await _PollClient.get(self, url, **kw)

        client = Client()
        # child is LIVE in the status (so the not-live branch is off)
        client.status_map = {"ses_parent": {"type": "busy"},
                             "ses_child": {"type": "busy"}}
        # child's own tool is wedged: running read, no output, old
        client.child_messages = [{"parts": [{
            "type": "tool", "tool": "read",
            "state": {"status": "running",
                      "time": {"start": int((_t.time() - 120) * 1000)}},
        }]}]
        assert await opencode_bridge._detect_wedged_tool(client, "ses_parent") is True

    @pytest.mark.asyncio
    async def test_busy_child_with_healthy_tools_waits(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import time as _t
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text("")
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        monkeypatch.setattr(opencode_bridge, "_TASK_WEDGE_AFTER_S", 600.0)
        monkeypatch.setattr(opencode_bridge, "_TOOL_WEDGE_AFTER_S", 60.0)
        monkeypatch.setattr(opencode_bridge, "_TASK_STALL_AFTER_S", 60.0)

        class Client(_PollClient):
            def __init__(self) -> None:
                super().__init__()
                self.session_id = "ses_parent"
                self.child_messages: list[dict] = []

            async def get(self, url: str, **kw: object) -> _FakeResp:
                if url.endswith("/session/ses_parent/message"):
                    return _FakeResp(
                        200, [{"parts":
                               TestStallAwareChildWedge._parent_parts(
                                   120, "ses_child")}],
                    )
                if url.endswith("/session/ses_child/message"):
                    return _FakeResp(200, self.child_messages)
                return await _PollClient.get(self, url, **kw)

        client = Client()
        client.status_map = {"ses_parent": {"type": "busy"},
                             "ses_child": {"type": "busy"}}
        # child busy with a RECENT running tool (real work, not wedged)
        client.child_messages = [{"parts": [{
            "type": "tool", "tool": "read",
            "state": {"status": "running",
                      "time": {"start": int(_t.time() * 1000) - 5_000}},
        }]}]
        assert await opencode_bridge._detect_wedged_tool(client, "ses_parent") is False


class TestContextCapReset:
    """The bridge-level context reset: a pinned session whose seeded
    history exceeds the cap is dropped on resume and a fresh session
    starts (the agent inside cannot shed its own context)."""

    @pytest.mark.asyncio
    async def test_over_cap_session_resets_fresh(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)

        old_msg = _assistant_msg([
            {"id": "prt_big", "messageID": "msg_old", "type": "text",
             "text": "x" * 300_000},  # ~75k tokens — over the 60k cap
        ])
        old_msg["id"] = "msg_old"
        new_msg = _assistant_msg([
            {"id": "prt_new", "messageID": "msg_new", "type": "text",
             "text": "fresh start answer"},
        ])
        new_msg["id"] = "msg_new"

        client = _SeedPollClient()
        client.seed_messages = [old_msg]
        client.poll_messages = [new_msg]
        client.stream_lines = []
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {"conv-1": "ses_0001"}
        deltas = [d async for d in opencode_chat_stream(
            "continue", session_map=smap, session_key="conv-1",
        )]
        joined = "".join(t for _, t in deltas)
        # The old context is gone (no replay) and the new turn streams.
        assert "x" * 100 not in joined
        assert "fresh start answer" in joined
        # The pin was dropped and a fresh session was created.
        assert any(u.endswith("/session") for u, _ in client.post_calls)


class TestEventBusDialTargetsActiveBackend:
    """T8-found regression: the ``/event`` bus dial must follow the ACTIVE
    transport's ``BACKEND.event_bus_url``.

    The bus URL is its own transport attribute pointing at
    ``OPENCODE_SECURE_SERVE_URL`` (4096).  Because the bus opens BEFORE the
    prompt POST, a dial at the wrong port ended every attempt before
    ``send_prompt``.
    """

    @pytest.mark.asyncio
    async def test_event_bus_targets_secure_backend_base_url(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from constants import OPENCODE_SECURE_SERVE_URL
        from opencode_backends import SecureBackend
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "BACKEND", SecureBackend())
        client = _FakeClient()
        client.stream_lines = _stream_events()
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]

        assert client.stream_calls == [f"{SecureBackend.event_bus_url}/event"]
        assert client.stream_calls[0] == f"{OPENCODE_SECURE_SERVE_URL}/event"
        # The prompt WAS reached (the pre-fix bug aborted BEFORE send_prompt).
        assert any("prompt_async" in u for u, _ in client.post_calls)
        assert "Done." in "".join(t for _, t in deltas)

    def test_event_bus_url_resolves_on_the_transport(self) -> None:
        """The sole transport resolves ``event_bus_url`` (no
        ``AttributeError``) and it points at the secure server."""
        from constants import OPENCODE_SECURE_SERVE_URL
        from opencode_backends import SecureBackend

        assert SecureBackend.event_bus_url == OPENCODE_SECURE_SERVE_URL
