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
from typing import Any, AsyncIterator, Iterator, Optional

import httpx
import pytest

import opencode_bridge
from opencode_bridge import (
    _strip_proxy_status_text,
    is_opencode_serve_running,
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
    id; tests reuse "ses_0001").  HERMETIC_KILLED_PIDS accumulates across
    the module and must be reset too: the wedge-path assertion
    ``HERMETIC_KILLED_PIDS == []`` is otherwise order-dependent."""
    PP.clear()
    HERMETIC_KILLED_PIDS.clear()
    yield
    PP.clear()
    HERMETIC_KILLED_PIDS.clear()


# ---------------------------------------------------------------------------
# Hermetic serve guard (test-infrastructure REQ-2)
# ---------------------------------------------------------------------------
#
# The bridge suite must NEVER kill or recycle a live ``opencode serve``.
# A real-stream test that lets ``_recycle_serve_if_low_memory`` /
# ``_force_recycle_serve`` — or the DIRECT wedge-kill ``os.kill`` at
# opencode_bridge.py:876/918, which the noop patches cannot cover — reach
# the live serve pid would take the dev's serve down mid-run.
#
# ``hermetic_serve`` (autouse, function-scoped, module-level) records the
# live serve pid ONCE per run, noops the two recycle primitives (async),
# wraps ``opencode_bridge.os.kill`` with a recorder, and asserts at every
# teardown that the recorded serve pid is still alive and was never killed.
# ``TestServeHealth`` exercises the REAL recycle primitive and opts out via
# ``@pytest.mark.real_recycle``: the fixture skips the patches but still
# runs the guard — its patched ``_find_serve_pid`` returns 12345/None,
# never the live serve pid, so the guard is vacuously satisfied.

_HERMETIC_SERVE_PID: Optional[int] = None  # recorded once, first fixture run
_HERMETIC_REAL_KILL = opencode_bridge.os.kill  # captured before any patch
HERMETIC_KILLED_PIDS: list[int] = []


def _serve_port() -> str:
    return opencode_bridge.OPENCODE_SERVE_URL.rsplit(":", 1)[-1]


def _pid_alive(pid: int, real_kill: Any) -> bool:
    """True when pid exists (real os.kill signal-0 probe)."""
    try:
        real_kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user


def _assert_serve_untouched(
    serve_pid: Optional[int], killed_pids: list[int], real_kill: Any,
) -> None:
    """REQ-2 Scenario-1/3 guard: the live serve must still be alive and its
    pid must never have been killed during the test.  Vacuous when no live
    serve exists (serve_pid is None)."""
    if serve_pid is None:
        return
    assert serve_pid not in killed_pids, (
        "hermetic_serve guard: os.kill fired against the live opencode "
        f"serve pid {serve_pid} (REQ-2) — the bridge suite must never "
        "recycle a live serve"
    )
    assert _pid_alive(serve_pid, real_kill), (
        "hermetic_serve guard: live opencode serve pid "
        f"{serve_pid} is no longer alive after the test (REQ-2 Scenario-1)"
    )


def _record_os_kill(target_pid: int, sig: int) -> None:
    """Recorder installed on ``opencode_bridge.os.kill`` while the hermetic
    fixture is active: logs every kill so the teardown guard can detect a
    serve-pid kill, and lets non-serve kills pass through untouched."""
    HERMETIC_KILLED_PIDS.append(target_pid)
    if target_pid != _HERMETIC_SERVE_PID:
        _HERMETIC_REAL_KILL(target_pid, sig)


@pytest.fixture(autouse=True)
def hermetic_serve(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Never kill or recycle a live opencode serve during the bridge suite.

    Records the live serve pid once (``_find_serve_pid``), noops the two
    recycle primitives (async noops), and wraps ``opencode_bridge.os.kill``
    with a recorder.  Teardown asserts the recorded serve pid is still
    alive and never appears in the kill recorder (REQ-2 Scenario-1/3).
    ``@pytest.mark.real_recycle`` tests (TestServeHealth) skip the patches
    but keep the guard.
    """
    global _HERMETIC_SERVE_PID
    if _HERMETIC_SERVE_PID is None:
        _HERMETIC_SERVE_PID = opencode_bridge._find_serve_pid(_serve_port())
    serve_pid = _HERMETIC_SERVE_PID
    real_kill = opencode_bridge.os.kill  # pre-patch reference for teardown

    if request.node.get_closest_marker("real_recycle") is None:
        async def _noop_recycle(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(
            opencode_bridge, "_recycle_serve_if_low_memory", _noop_recycle,
        )
        monkeypatch.setattr(
            opencode_bridge, "_force_recycle_serve", _noop_recycle,
        )
        monkeypatch.setattr(opencode_bridge.os, "kill", _record_os_kill)
    yield
    _assert_serve_untouched(serve_pid, HERMETIC_KILLED_PIDS, real_kill)


class TestHermeticServe:
    """The hermetic guard itself (test-infrastructure REQ-2 Scenario-3): a
    serve-pid kill must fail the run, and the recorder must capture the
    wedge-path kills the noop patches cannot cover."""

    def test_guard_rejects_kill_of_live_serve_pid(self) -> None:
        """A run whose os.kill recorder saw the live serve pid FAILS."""
        real_kill = opencode_bridge.os.kill

        with pytest.raises(AssertionError, match="REQ-2"):
            _assert_serve_untouched(424242, [424242], real_kill)

    def test_guard_passes_when_serve_untouched(self) -> None:
        """Guard is satisfied when the serve pid was never killed, and is
        vacuously satisfied when no live serve exists (the real_recycle
        fake-pid case)."""
        _assert_serve_untouched(424242, [], lambda *a, **k: None)
        _assert_serve_untouched(None, [1, 2, 3], opencode_bridge.os.kill)

    @pytest.mark.asyncio
    async def test_wedge_path_never_kills_serve(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The wedge path must NEVER signal the serve: the serve hosts
        other concurrent sessions, and a serve kill for one wedged tool
        destroys them all (2026-08-08 — the kill was removed).  This test
        pins that: no pid is ever recorded by the hermetic guard."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        async def _always_wedged(*args: Any, **kwargs: Any) -> bool:
            return True

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(
            opencode_bridge, "_recycle_serve_if_low_memory", _noop,
        )
        monkeypatch.setattr(opencode_bridge, "_detect_wedged_tool", _always_wedged)
        # Even with a MATCHING serve pid, the wedge path must not signal it.
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", lambda port: 424242)
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
        assert "recycled" not in wedged[0]
        # The wedge path recorded NO kill — the serve survives.
        assert 424242 not in HERMETIC_KILLED_PIDS
        assert HERMETIC_KILLED_PIDS == []


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
        result = await opencode_chat("write a test", agent="gentle-orchestrator")
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
    async def test_read_permission_auto_allowed(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(opencode_bridge, "_BLOCKING_PERMISSION_POLL_S", 0.01)
        fake_client.message_hang_s = 0.2
        fake_client.message_parts = [{"type": "text", "text": "ok"}]
        fake_client.permission_records = [{
            "id": "perm_1",
            "sessionID": "ses_0001",
            "permission": "external_directory",
            "patterns": ["cat /etc/os-release"],
            "tool": {"messageID": "m1", "callID": "c1"},
        }]
        result = await opencode_chat("task")
        assert result == "ok"
        assert any(
            url.endswith("/permissions/perm_1") and body == {"response": "always"}
            for url, body in fake_client.post_calls
        )
        assert not any(
            url.endswith("/abort") for url, _ in fake_client.post_calls
        )

    @pytest.mark.asyncio
    async def test_write_permission_aborts(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(opencode_bridge, "_BLOCKING_PERMISSION_POLL_S", 0.01)
        fake_client.message_hang_s = 0.2
        fake_client.message_parts = [{"type": "text", "text": "ok"}]
        fake_client.permission_records = [{
            "id": "perm_2",
            "sessionID": "ses_0001",
            "permission": "write",
            "patterns": ["rm -rf /x"],
            "tool": {"messageID": "m1", "callID": "c1"},
        }]
        result = await opencode_chat("task")
        assert "[OpenCode Bridge Error:" in result
        assert "write permission" in result
        assert any(
            url.endswith("/abort") for url, _ in fake_client.post_calls
        )

    @pytest.mark.asyncio
    async def test_autonomous_write_auto_allowed(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(opencode_bridge, "_BLOCKING_PERMISSION_POLL_S", 0.01)
        fake_client.message_hang_s = 0.2
        fake_client.message_parts = [{"type": "text", "text": "ok"}]
        fake_client.permission_records = [{
            "id": "perm_3",
            "sessionID": "ses_0001",
            "permission": "write",
            "patterns": ["rm -rf /x"],
            "tool": {"messageID": "m1", "callID": "c1"},
        }]
        result = await opencode_chat("task", autonomous=True)
        assert result == "ok"
        assert any(
            url.endswith("/permissions/perm_3") and body == {"response": "always"}
            for url, body in fake_client.post_calls
        )
        assert not any(
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
        ensure → exactly one recycle + re-ensure + one retry; the retried
        call returns its text."""
        from opencode_bridge import opencode_chat

        ensure_results = [True, True]
        ensure_calls: list[str] = []
        recycle_calls: list[str] = []

        async def _ensure() -> bool:
            ensure_calls.append("ensure")
            return ensure_results.pop(0)

        async def _recycle(reason: str = "long-lived call") -> None:
            recycle_calls.append(reason)

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure)
        monkeypatch.setattr(opencode_bridge, "_force_recycle_serve", _recycle)

        fake_client.fail_post_times = 1
        fake_client.message_parts = [{"type": "text", "text": "recovered"}]
        result = await opencode_chat("task")

        assert result == "recovered"
        assert len(ensure_calls) == 2
        assert recycle_calls == ["blocking-path respawn"]
        msg_posts = [
            url for url, _ in fake_client.post_calls if "/message" in url
        ]
        assert len(msg_posts) == 2

    @pytest.mark.asyncio
    async def test_blocking_respawn_second_failure_returns_error(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-4 Scenario-2: when the single retry also fails, the error
        string is returned and no second recycle/third attempt occurs."""
        from opencode_bridge import opencode_chat

        ensure_results = [True, True]
        recycle_calls: list[str] = []

        async def _ensure() -> bool:
            return ensure_results.pop(0)

        async def _recycle(reason: str = "long-lived call") -> None:
            recycle_calls.append(reason)

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure)
        monkeypatch.setattr(opencode_bridge, "_force_recycle_serve", _recycle)

        fake_client.fail_post_times = 2
        result = await opencode_chat("task")

        assert result.startswith("[OpenCode Bridge Network Error:")
        assert len(recycle_calls) == 1
        msg_posts = [
            url for url, _ in fake_client.post_calls if "/message" in url
        ]
        assert len(msg_posts) == 2

    @pytest.mark.asyncio
    async def test_blocking_no_retry_on_http_error(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-4 Scenario-3: an HTTP-status failure never triggers the
        recycle/retry — the existing HTTP error string is returned."""
        from opencode_bridge import opencode_chat

        ensure_calls: list[str] = []
        recycle_calls: list[str] = []

        async def _ensure() -> bool:
            ensure_calls.append("ensure")
            return True

        async def _recycle(reason: str = "long-lived call") -> None:
            recycle_calls.append(reason)

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure)
        monkeypatch.setattr(opencode_bridge, "_force_recycle_serve", _recycle)

        fake_client.message_status = 503
        result = await opencode_chat("task")

        assert result == "[OpenCode Bridge Error: message HTTP 503]"
        assert recycle_calls == []
        assert len(ensure_calls) == 1
        msg_posts = [
            url for url, _ in fake_client.post_calls if "/message" in url
        ]
        assert len(msg_posts) == 1

    @pytest.mark.asyncio
    async def test_blocking_respawn_reensure_failure(
        self, fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-4: a failed re-ensure after the recycle returns the
        not-reachable error — exactly one recycle, no retry attempt."""
        from opencode_bridge import opencode_chat

        ensure_results = [True, False]
        recycle_calls: list[str] = []

        async def _ensure() -> bool:
            return ensure_results.pop(0)

        async def _recycle(reason: str = "long-lived call") -> None:
            recycle_calls.append(reason)

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure)
        monkeypatch.setattr(opencode_bridge, "_force_recycle_serve", _recycle)

        fake_client.fail_post_times = 1
        result = await opencode_chat("task")

        assert result == "[OpenCode Bridge Failed: opencode serve not reachable.]"
        assert len(recycle_calls) == 1
        msg_posts = [
            url for url, _ in fake_client.post_calls if "/message" in url
        ]
        assert len(msg_posts) == 1


class TestStreamExitHygiene:
    """Streaming-path error-exit hygiene (bridge-cycle-6): every error
    exit aborts the session, drops the pin, and pops pending permissions;
    the serve-recycle check runs before the ensure/respawn.

    Hermetic: scripted _FakeClient + monkeypatched ensure/recycle, no live
    serve (the autouse hermetic_serve guard never sees a real kill).
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

    @pytest.mark.asyncio
    async def test_recycle_before_ensure_no_failure(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-6: the serve-recycle check runs BEFORE ensure/respawn; a
        stream request completes instead of failing with a spurious
        network error after the recycle kills the serve."""
        from opencode_bridge import opencode_chat_stream

        order: list[str] = []

        async def _recycle(*args: Any, **kwargs: Any) -> None:
            order.append("recycle")

        async def _ensure(*args: Any, **kwargs: Any) -> bool:
            order.append("ensure")
            return True

        monkeypatch.setattr(opencode_bridge, "_recycle_serve_if_low_memory", _recycle)
        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _ensure)
        client = _FakeClient()
        client.stream_lines = _stream_events()
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        deltas = [
            d async for d in opencode_chat_stream(
                "task", session_map=smap, session_key="conv",
            )
        ]

        assert order == ["recycle", "ensure"]
        text = [t for k, t in deltas if k == "text"]
        assert "Created file." in text
        assert "Done." in text
        assert not any(
            url.endswith("/abort") for url, _ in client.post_calls
        )
        assert smap == {"conv": "ses_0001"}  # success path leaves the pin


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
    async def test_stale_pin_dropped_when_status_lacks_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-2 Scenario-1: a successful status fetch WITHOUT the pinned id
        drops the pin and POSTs a fresh /session; no abort is fired (the
        session is gone)."""
        from opencode_bridge import opencode_chat_stream

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", self._running)
        client = _FakeClient()
        client.status_map = {}  # serve lists NO sessions
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
    async def test_pin_kept_on_status_fetch_transport_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-2 Scenario-2: a transport-error status fetch keeps the pin —
        no fresh /session POST; the pinned session is used and the stream
        completes."""
        from opencode_bridge import opencode_chat_stream

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", self._running)
        client = _FakeClient()
        client.raise_timeout_on = "get"  # status fetch raises ReadTimeout
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


class TestIsRunning:
    @pytest.mark.asyncio
    async def test_running_when_200(self, fake_client: _FakeClient) -> None:
        assert await is_opencode_serve_running() is True

    @pytest.mark.asyncio
    async def test_not_running_on_error(self, fake_client: _FakeClient) -> None:
        fake_client.raise_on = "get"
        assert await is_opencode_serve_running() is False

    @pytest.mark.parametrize("status", [200, 302, 404, 500])
    @pytest.mark.asyncio
    async def test_alive_for_any_status(
        self, fake_client: _FakeClient, status: int,
    ) -> None:
        """REQ-1 Scenario-1: ANY received HTTP response — 2xx, 3xx, 4xx,
        5xx — proves the serve transport is alive (the port is held)."""
        fake_client.get_status = status
        assert await is_opencode_serve_running() is True

    @pytest.mark.asyncio
    async def test_not_running_on_read_timeout(
        self, fake_client: _FakeClient,
    ) -> None:
        """REQ-2 Scenario-1: a connection established but no complete
        response within the window (ReadTimeout) means down — no explicit
        body read ever runs."""
        fake_client.raise_timeout_on = "get"
        assert await is_opencode_serve_running() is False

    @pytest.mark.asyncio
    async def test_probe_uses_three_second_timeout(
        self, fake_client: _FakeClient,
    ) -> None:
        """REQ-2: the probe uses a 3.0s client timeout (was 5.0s)."""
        await is_opencode_serve_running()
        assert fake_client.get_timeouts == [3.0]

    @pytest.mark.parametrize("status", [404, 500])
    @pytest.mark.asyncio
    async def test_no_spawn_when_404_500_alive(
        self, monkeypatch: pytest.MonkeyPatch,
        fake_client: _FakeClient, status: int,
    ) -> None:
        """REQ-3 Scenario-1: a live responder answering 404 or 5xx holds
        the port — ensure_opencode_serve() must NOT spawn a duplicate or
        replacement process."""
        from opencode_bridge import ensure_opencode_serve

        spawn_calls: list[Optional[float]] = []

        async def _spawn_recorder(mtime: Optional[float]) -> bool:
            spawn_calls.append(mtime)
            return True

        fake_client.get_status = status
        monkeypatch.setattr(opencode_bridge, "_config_mtime", lambda: None)
        monkeypatch.setattr(opencode_bridge, "_spawn_serve", _spawn_recorder)

        ok = await ensure_opencode_serve()
        assert ok is True
        assert spawn_calls == []

    @pytest.mark.asyncio
    async def test_drain_bounded_to_four_probes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-3 Scenario-2: the config-drift drain is capped — at most
        four drain probes and exactly four 0.25s sleeps (≈4 × (3.0s + 0.25s)
        ≈ 13s worst case), then exactly one respawn."""
        from opencode_bridge import ensure_opencode_serve

        probe_calls: list[bool] = []

        async def _always_running() -> bool:
            probe_calls.append(True)
            return True

        sleeps: list[float] = []

        async def _sleep_recorder(delay: float) -> None:
            sleeps.append(delay)

        async def _noop() -> None:
            return None

        spawn_calls: list[Optional[float]] = []

        async def _spawn_recorder(mtime: Optional[float]) -> bool:
            spawn_calls.append(mtime)
            return True

        monkeypatch.setattr(opencode_bridge, "_serve_config_mtime", 1000.0)
        monkeypatch.setattr(opencode_bridge, "_config_mtime", lambda: 2000.0)
        monkeypatch.setattr(
            opencode_bridge, "is_opencode_serve_running", _always_running,
        )
        monkeypatch.setattr(opencode_bridge.asyncio, "sleep", _sleep_recorder)
        monkeypatch.setattr(opencode_bridge, "_force_recycle_serve", _noop)
        monkeypatch.setattr(opencode_bridge, "_spawn_serve", _spawn_recorder)

        ok = await ensure_opencode_serve()
        assert ok is True
        # 1 initial spawn-gate probe + at most 4 drain probes.
        assert len(probe_calls) - 1 <= 4
        # Exactly four 0.25s drain sleeps, never more.
        assert sleeps == [0.25, 0.25, 0.25, 0.25]
        assert len(spawn_calls) == 1


class TestEscalation:
    @pytest.mark.asyncio
    async def test_escalation_returns_chat_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_chat(text: str, *, agent: str = "gentle-orchestrator") -> str:
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
            text: str, *, agent: str = "gentle-orchestrator",
            model_id: Optional[str] = None, provider_id: str = "kinver",
            session_map: Optional[dict[str, str]] = None,
            session_key: Optional[str] = None,
            pending_permissions: Optional[dict[str, tuple[str, bool]]] = None,
            just_approved_permission: bool = False,
            system_prompt: str = "",
            timeout: float = 600.0,
            autonomous: bool = False,
        ) -> AsyncIterator[tuple[str, str]]:
            captured.append(text)
            yield ("text", "BRIDGE_DONE")

        monkeypatch.setattr("routes.opencode_chat_stream", _fake_stream)
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
            text: str, *, agent: str = "gentle-orchestrator",
            model_id: Optional[str] = None, provider_id: str = "kinver",
            session_map: Optional[dict[str, str]] = None,
            session_key: Optional[str] = None,
            pending_permissions: Optional[dict[str, tuple[str, bool]]] = None,
            just_approved_permission: bool = False,
            system_prompt: str = "",
            timeout: float = 600.0,
            autonomous: bool = False,
        ) -> AsyncIterator[tuple[str, str]]:
            yield ("text", "STREAMED_")
            yield ("text", "DONE")

        monkeypatch.setattr("routes.opencode_chat_stream", _fake_stream)
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
    async def test_model_opencode_sdd_uses_autonomous_prompt_and_long_timeout(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SDD-autonomous mode: model "opencode-sdd" must dispatch through
        the bridge with the _SDD_AUTONOMOUS_SYSTEM_PROMPT and the long
        OPENCODE_SDD_TIMEOUT — the whole cycle runs in ONE long-lived turn
        with no per-phase chat."""
        from routes import _handle_opencode_request
        from opencode_bridge import _SDD_AUTONOMOUS_SYSTEM_PROMPT
        from constants import OPENCODE_SDD_TIMEOUT

        seen: dict[str, object] = {}

        async def _fake_stream(
            text: str, *, agent: str = "gentle-orchestrator",
            model_id: Optional[str] = None, provider_id: str = "kinver",
            session_map: Optional[dict[str, str]] = None,
            session_key: Optional[str] = None,
            pending_permissions: Optional[dict[str, str]] = None,
            just_approved_permission: bool = False,
            system_prompt: str = "",
            timeout: float = 600.0,
            autonomous: bool = False,
        ) -> AsyncIterator[tuple[str, str]]:
            seen["system_prompt"] = system_prompt
            seen["timeout"] = timeout
            seen["autonomous"] = autonomous
            yield ("text", "SDD_CYCLE_DONE")

        monkeypatch.setattr("routes.opencode_chat_stream", _fake_stream)
        resp = await _handle_opencode_request(
            [{"role": "user", "content": "Use SDD to add a docs file"}],
            client_stream=True,
            sdd=True,
        )
        text = await _drain_stream(resp)
        assert "SDD_CYCLE_DONE" in text
        assert seen.get("system_prompt") == _SDD_AUTONOMOUS_SYSTEM_PROMPT
        assert seen.get("timeout") == OPENCODE_SDD_TIMEOUT
        # Task 1.6: the autonomous flag must ride the SDD dispatch.
        assert seen.get("autonomous") is True

    @pytest.mark.asyncio
    async def test_model_opencode_sdd_non_streaming_carries_prompt_timeout_autonomous(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The NON-streaming SDD dispatch must pass the same autonomous
        prompt, long timeout, and autonomous flag as the streaming path —
        otherwise a ``stream: false`` opencode-sdd request (nanobot-style)
        would run with the interactive prompt and the 600s serve timeout,
        aborting a long cycle mid-run."""
        from routes import _handle_opencode_request
        from opencode_bridge import _SDD_AUTONOMOUS_SYSTEM_PROMPT
        from constants import OPENCODE_SDD_TIMEOUT

        seen: dict[str, object] = {}

        async def _fake_stream(
            text: str, *, agent: str = "gentle-orchestrator",
            model_id: Optional[str] = None, provider_id: str = "kinver",
            session_map: Optional[dict[str, str]] = None,
            session_key: Optional[str] = None,
            pending_permissions: Optional[dict[str, str]] = None,
            just_approved_permission: bool = False,
            system_prompt: str = "",
            timeout: float = 600.0,
            autonomous: bool = False,
        ) -> AsyncIterator[tuple[str, str]]:
            seen["system_prompt"] = system_prompt
            seen["timeout"] = timeout
            seen["autonomous"] = autonomous
            yield ("text", "SDD_NONSTREAM_DONE")

        monkeypatch.setattr("routes.opencode_chat_stream", _fake_stream)
        resp = await _handle_opencode_request(
            [{"role": "user", "content": "Use SDD to add a docs file"}],
            client_stream=False,
            sdd=True,
        )
        assert resp.status_code == 200
        assert "SDD_NONSTREAM_DONE" in resp.body.decode()
        assert seen.get("system_prompt") == _SDD_AUTONOMOUS_SYSTEM_PROMPT
        assert seen.get("timeout") == OPENCODE_SDD_TIMEOUT
        assert seen.get("autonomous") is True

    @pytest.mark.asyncio
    async def test_opencode_command_strips_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from routes import _handle_opencode_command

        captured: list[str] = []

        async def _fake_stream(
            text: str, *, agent: str = "gentle-orchestrator",
            model_id: Optional[str] = None, provider_id: str = "kinver",
            session_map: Optional[dict[str, str]] = None,
            session_key: Optional[str] = None,
            pending_permissions: Optional[dict[str, tuple[str, bool]]] = None,
            just_approved_permission: bool = False,
            system_prompt: str = "",
            timeout: float = 600.0,
            autonomous: bool = False,
        ) -> AsyncIterator[tuple[str, str]]:
            captured.append(text)
            yield ("text", "CMD_DONE")

        monkeypatch.setattr("routes.opencode_chat_stream", _fake_stream)
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


# ---------------------------------------------------------------------------
# External-directory permission relay (opencode >= 1.18 external_directory
# gate): reads auto-allowed, writes asked to the user.
# ---------------------------------------------------------------------------
class TestClassifyExternalAccess:
    """_classify_external_access must tag mutating commands WRITE (needs
    user approval) and read-only commands READ (auto-allowed)."""

    @pytest.mark.parametrize("cmd", [
        "echo x > /etc/foo",
        "echo x >> /home/user/log",
        "mv /etc/foo /etc/bar",
        "cp /etc/passwd /home/user/",
        "rm -rf /home/user/cache",
        "touch /home/user/foo",
        "mkdir /home/user/out",
        "rmdir /home/user/old",
        "ln -s /etc/hosts /home/user/hosts",
        "chmod 644 /home/user/file",
        "chown james /home/user/file",
        "tee /etc/foo",
        "sed -i s/x/y/ /etc/hosts",
        "install -m 755 app /usr/local/bin/app",
        "dd if=/dev/zero of=/home/user/big",
        "git commit -m bump",
        "git push",
        "git reset --hard HEAD",
        "git checkout -- src/x.py",
        "write /dev/tty1",
        "printf x > /etc/foo",
    ])
    def test_mutating_commands_are_write(self, cmd: str) -> None:
        from opencode_bridge import _classify_external_access
        assert _classify_external_access(cmd) == "write"

    @pytest.mark.parametrize("cmd", [
        "cat /etc/os-release",
        "ls /home",
        "head -5 /etc/passwd",
        "grep james /etc/passwd",
        "stat /etc/hosts",
    ])
    def test_read_only_commands_are_read(self, cmd: str) -> None:
        from opencode_bridge import _classify_external_access
        assert _classify_external_access(cmd) == "read"

    def test_empty_cmd_is_read(self) -> None:
        from opencode_bridge import _classify_external_access
        assert _classify_external_access("") == "read"


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


class TestPermissionRelay:
    """The event-bus permission.updated handler relays external_directory
    gates: READ is auto-allowed with {"response": "always"}, WRITE yields a
    question and stops the stream with the permission stored for the next
    request."""

    @pytest.mark.asyncio
    async def test_read_permission_auto_allowed_no_question(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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
            _evt("permission.updated", sessionID="ses_0001",
                 id="perm_2", type="external_directory",
                 title="Allow reading external file",
                 metadata={"command": "cat /etc/os-release"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]

        # No question surfaced; the tool proceeds silently.
        assert [k for k, _ in deltas if k == "question"] == []
        perm_posts = [(u, b) for u, b in client.post_calls if "/permissions/" in u]
        assert any(
            u.endswith("/permissions/perm_2") and b == {"response": "always"}
            for u, b in perm_posts
        )
        assert PP.get("ses_0001") is None

    @pytest.mark.asyncio
    async def test_write_permission_yields_question_and_stops(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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
            _evt("permission.updated", sessionID="ses_0001",
                 id="perm_1", type="external_directory",
                 title="Allow writing external file",
                 metadata={"command": "mv /etc/foo /etc/bar",
                           "filepath": "/etc/foo"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]

        assert len(deltas) == 1
        kind, text = deltas[0]
        assert kind == "question"
        assert "outside its workspace" in text
        assert "Target: /etc/foo" in text
        assert "Command: mv /etc/foo /etc/bar" in text
        assert "Reply" in text
        # No permission POST fired; the pending entry awaits the user's
        # answer on the next (pinned-continuation) request.
        assert [u for u, _ in client.post_calls if "/permissions/" in u] == []
        assert PP.get("ses_0001") == ("perm_1", True)

    @pytest.mark.asyncio
    async def test_other_permission_types_ignored(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A permission.updated event whose type is NOT relayed
        (external_directory or bash) is ignored: no question, no pending
        store, no permission POST."""
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
            _evt("permission.updated", sessionID="ses_0001",
                 id="perm_x", type="webfetch",
                 title="Allow web fetch",
                 metadata={"command": "curl https://x"}),
            _evt("message.part.updated", sessionID="ses_0001",
                 part={"id": "prt_t", "messageID": "msg_a", "type": "text",
                       "text": "ok"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]

        assert [k for k, _ in deltas if k == "question"] == []
        assert [u for u, _ in client.post_calls if "/permissions/" in u] == []
        assert PP.get("ses_0001") is None

    @pytest.mark.asyncio
    async def test_git_permission_yields_question_and_stops(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bash-type permission.updated (the git ask-rules: commit/push/
        reset/rebase) is relayed to the user with the git question template
        and stops the stream with the permission stored."""
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
            _evt("permission.updated", sessionID="ses_0001",
                 id="perm_g", type="bash",
                 title="Allow git commit",
                 metadata={"command": "git commit -m bump"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]

        assert len(deltas) == 1
        kind, text = deltas[0]
        assert kind == "question"
        assert "git command" in text
        assert "Command: git commit -m bump" in text
        assert "Reply" in text
        assert [u for u, _ in client.post_calls if "/permissions/" in u] == []
        assert PP.get("ses_0001") == ("perm_g", True)

    @pytest.mark.asyncio
    async def test_post_permission_response_posts_body(
        self, fake_client: _FakeClient,
    ) -> None:
        """_post_permission_response POSTs {"response": ...} to the serve's
        permission endpoint and reports success on 200."""
        from opencode_bridge import _post_permission_response

        ok = await _post_permission_response("ses_0001", "perm_1", "once")
        assert ok is True
        assert (
            f"{opencode_bridge.OPENCODE_SERVE_URL}/session/ses_0001/permissions/perm_1",
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


class TestPermissionRelayPolicy:
    """REQ-1/REQ-2: write/edit gates (opencode 1.18.15 write/edit tools may
    OMIT the permission.updated SSE event — the relay flows through the
    polling GET /permission paths too) must be RELAYED in interactive mode
    and AUTO-ALLOWED (POST "always", no client surface, no pending state)
    in SDD-autonomous mode."""

    @pytest.mark.asyncio
    async def test_permission_write_relayed_interactive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-1 Scenario-1 + F2: a write-type permission.updated event
        (perm_type "write", no bash command to inspect) is relayed as a
        question in interactive mode — never auto-allowed."""
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
            _evt("permission.updated", sessionID="ses_0001",
                 id="perm_w", type="write",
                 title="Allow writing to /home/user/out.txt",
                 metadata={"filepath": "/home/user/out.txt"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]

        assert len(deltas) == 1
        kind, text = deltas[0]
        assert kind == "question"
        assert "outside its workspace" in text
        assert "Target: /home/user/out.txt" in text
        # No auto-allow POST fired; the write awaits the user's answer.
        assert [u for u, _ in client.post_calls if "/permissions/" in u] == []
        assert PP.get("ses_0001") == ("perm_w", True)

    @pytest.mark.asyncio
    async def test_autonomous_auto_allows_write(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-2 Scenario-1: autonomous mode POSTs "always" for a write
        permission — no question surfaced, no pending state stored."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_force_recycle_serve", _noop)
        client = _FakeClient()
        client.stream_lines = [
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

        deltas = [d async for d in opencode_chat_stream(
            "task", pending_permissions=PP, autonomous=True,
        )]

        assert [k for k, _ in deltas if k == "question"] == []
        perm_posts = [(u, b) for u, b in client.post_calls if "/permissions/" in u]
        assert any(
            u.endswith("/permissions/perm_w") and b == {"response": "always"}
            for u, b in perm_posts
        )
        assert PP.get("ses_0001") is None

    @pytest.mark.asyncio
    async def test_autonomous_auto_allows_git_commit(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-2 Scenario-2: autonomous mode auto-allows a git commit ask
        (bash permission type) — the cycle must not stall on the git ask."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_force_recycle_serve", _noop)
        client = _FakeClient()
        client.stream_lines = [
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_user", "role": "user"}),
            _evt("message.updated", sessionID="ses_0001",
                 info={"id": "msg_a", "role": "assistant"}),
            _evt("permission.updated", sessionID="ses_0001",
                 id="perm_g", type="bash",
                 title="Allow git commit",
                 metadata={"command": "git commit -m bump"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream(
            "task", pending_permissions=PP, autonomous=True,
        )]

        assert [k for k, _ in deltas if k == "question"] == []
        perm_posts = [(u, b) for u, b in client.post_calls if "/permissions/" in u]
        assert any(
            u.endswith("/permissions/perm_g") and b == {"response": "always"}
            for u, b in perm_posts
        )
        assert PP.get("ses_0001") is None

    @pytest.mark.asyncio
    async def test_interactive_does_not_auto_allow_write(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-2 Scenario-3 + F2: interactive mode must NOT auto-allow a
        write gate.  Uses the POLLING path (GET /permission record with
        ``permission: "write"`` — the 1.18.15 write tools omit the SSE
        event): the record must surface a question, never a silent
        auto-allow."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.02)
        monkeypatch.setattr(opencode_bridge, "_TOOL_WEDGE_AFTER_S", 60.0)
        client = _RunningToolPermissionClient()
        client.status_type = "idle"  # completion path
        client.permission_records = [{
            "id": "perm_w", "sessionID": "ses_0001",
            "permission": "write",
            "patterns": ["/home/user/out.txt"],
            "tool": {"messageID": "msg_a", "callID": "call_w"},
        }]
        client.stream_lines = []  # empty event bus → polling fallback
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]

        questions = [(k, t) for k, t in deltas if k == "question"]
        assert len(questions) == 1
        _, text = questions[0]
        assert "Target: /home/user/out.txt" in text
        # Interactive: never auto-allowed (no "always" POST to the write id).
        perm_posts = [(u, b) for u, b in client.post_calls if "/permissions/" in u]
        assert all("perm_w" not in u for u, _ in perm_posts)
        assert PP.get("ses_0001") == ("perm_w", True)

    @pytest.mark.asyncio
    async def test_opencode_command_write_relay(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-3 Scenario-1 + threat-matrix (routing parity): the /opencode
        command must flow through ``opencode_chat_stream`` (the relay path),
        NOT the bare blocking ``opencode_chat`` (the old silent write/edit
        drop).  A write gate inside a /opencode task surfaces as an SSE
        content question in interactive mode (no auto-allow), and the SAME
        stream path auto-allows it in autonomous mode."""
        from routes import _handle_opencode_command
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)

        # ---- Interactive leg: write gate surfaces as a question ---------
        client = _FakeClient()
        client.stream_lines = [
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

        resp = await _handle_opencode_command(
            "/opencode write config", _FakeApp(pending=PP),
        )
        text = await _drain_stream(resp)
        # The write question is relayed as visible SSE content — never dropped.
        assert "Target: /home/user/out.txt" in text
        # Interactive: no auto-allow POST fired; the gate awaits the user.
        assert [u for u, _ in client.post_calls if "/permissions/" in u] == []
        assert PP.get("ses_0001") == ("perm_w", True)
        # The stream relay path is used: prompt_async (streaming send) fired
        # and NO bare blocking /message POST (the old opencode_chat drop path).
        assert any("prompt_async" in u for u, _ in client.post_calls)
        assert not any("/message" in u for u, _ in client.post_calls)

        # ---- Autonomous leg: the same stream path auto-allows -----------
        PP.clear()
        client2 = _FakeClient()
        client2.stream_lines = list(client.stream_lines)
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client2)
        deltas = [d async for d in opencode_chat_stream(
            "write config", pending_permissions=PP, autonomous=True,
        )]
        assert [k for k, _ in deltas if k == "question"] == []
        perm_posts = [(u, b) for u, b in client2.post_calls if "/permissions/" in u]
        assert any(
            u.endswith("/permissions/perm_w") and b == {"response": "always"}
            for u, b in perm_posts
        )
        assert PP.get("ses_0001") is None
        PP.clear()


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
    async def test_busy_pinned_after_permission_approval_not_aborted(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression (review finding 2): a pinned session that just resumed
        after a permission approval is legitimately BUSY (executing the
        approved tool / generating its summary).  With
        ``just_approved_permission=True`` the busy-abort must be skipped and
        the pin reused — NOT aborted and started fresh (which would kill the
        just-approved tool mid-execution)."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        client = _BusyThenIdleClient()  # reports busy for 3 polls then idle
        client.stream_lines = _stream_events()
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        smap: dict[str, str] = {}
        # First call pins the session.
        [d async for d in opencode_chat_stream(
            "task", session_map=smap, session_key="conv-perm",
        )]
        assert smap.get("conv-perm") == "ses_0001"

        client.calls.clear()
        client.stream_lines = _stream_events()
        # Second call resumes AFTER a permission approval: busy must NOT abort.
        [d async for d in opencode_chat_stream(
            "allow",
            session_map=smap,
            session_key="conv-perm",
            just_approved_permission=True,
        )]
        # No /session POST (create) and no abort fired; pin survived.
        creates = [u for m, u in client.calls if m == "post" and u.endswith("/session")]
        aborts = [u for m, u in client.calls if m == "post" and u.endswith("/abort")]
        assert creates == []
        assert aborts == []
        assert smap["conv-perm"] == "ses_0001"

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
# Serve health / age-based recycling
# ---------------------------------------------------------------------------
@pytest.mark.real_recycle
class TestServeHealth:
    @pytest.mark.asyncio
    async def test_recycles_old_serve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An old serve (no memory pressure) is killed for recycling."""
        from opencode_bridge import _recycle_serve_if_low_memory

        killed: list[int] = []

        def _health(port: str) -> tuple[bool, float]:
            return False, 3600.0  # healthy memory, but up an hour

        def _find(port: str) -> int:
            return 12345

        monkeypatch.setattr(opencode_bridge, "_serve_health", _health)
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", _find)
        # REQ-3 (bridge-cycle-4): the kill primitive now verifies the pid's
        # cmdline before signalling — patch the verifier to confirm.
        monkeypatch.setattr(
            opencode_bridge, "_pid_is_serve", lambda pid, port: True,
        )
        monkeypatch.setattr(opencode_bridge.os, "kill", lambda pid, sig: killed.append(pid))

        await _recycle_serve_if_low_memory()
        assert killed == [12345]

    @pytest.mark.asyncio
    async def test_recycle_kills_on_pid_match(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-3 Scenario-1: the low-memory recycle verifies the pid's
        cmdline and kills on a match."""
        from opencode_bridge import _recycle_serve_if_low_memory

        killed: list[int] = []

        def _health(port: str) -> tuple[bool, float]:
            return False, 3600.0  # old serve, recycle condition met

        def _find(port: str) -> int:
            return 12345

        async def _down(*args: Any, **kwargs: Any) -> bool:
            return False  # drain breaks immediately

        monkeypatch.setattr(opencode_bridge, "_serve_health", _health)
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", _find)
        monkeypatch.setattr(opencode_bridge, "_pid_is_serve", lambda pid, port: True)
        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running", _down)
        monkeypatch.setattr(opencode_bridge.os, "kill", lambda pid, sig: killed.append(pid))

        await _recycle_serve_if_low_memory()
        assert killed == [12345]

    @pytest.mark.asyncio
    async def test_recycle_skips_kill_on_pid_mismatch(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-3 Scenario-2: a pid whose cmdline no longer matches the serve
        (reused by another process) is NEVER signalled."""
        from opencode_bridge import _recycle_serve_if_low_memory

        killed: list[int] = []

        def _health(port: str) -> tuple[bool, float]:
            return False, 3600.0

        def _find(port: str) -> int:
            return 12345

        monkeypatch.setattr(opencode_bridge, "_serve_health", _health)
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", _find)
        monkeypatch.setattr(opencode_bridge, "_pid_is_serve", lambda pid, port: False)
        monkeypatch.setattr(opencode_bridge.os, "kill", lambda pid, sig: killed.append(pid))

        await _recycle_serve_if_low_memory()
        assert killed == []

    @pytest.mark.asyncio
    async def test_force_recycle_kills_on_pid_match(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-3: _force_recycle_serve verifies the pid and kills on match."""
        from opencode_bridge import _force_recycle_serve

        killed: list[int] = []

        def _find(port: str) -> int:
            return 12345

        async def _down(*args: Any, **kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", _find)
        monkeypatch.setattr(opencode_bridge, "_pid_is_serve", lambda pid, port: True)
        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running", _down)
        monkeypatch.setattr(opencode_bridge.os, "kill", lambda pid, sig: killed.append(pid))

        await _force_recycle_serve("test")
        assert killed == [12345]

    @pytest.mark.asyncio
    async def test_force_recycle_skips_kill_on_pid_mismatch(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-3: a mismatched pid is never signalled by the force recycle."""
        from opencode_bridge import _force_recycle_serve

        killed: list[int] = []

        def _find(port: str) -> int:
            return 12345

        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", _find)
        monkeypatch.setattr(opencode_bridge, "_pid_is_serve", lambda pid, port: False)
        monkeypatch.setattr(opencode_bridge.os, "kill", lambda pid, sig: killed.append(pid))

        await _force_recycle_serve("test")
        assert killed == []

    def test_pid_is_serve_matches_cmdline(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-3: the pid verifier reads /proc/<pid>/cmdline and applies the
        shared serve matcher; a missing cmdline counts as mismatch."""
        from opencode_bridge import _pid_is_serve

        _patch_proc_cmdline(
            monkeypatch,
            b"/home/user/.opencode/bin/opencode\x00serve\x00"
            b"--port\x0018999\x00--hostname\x00127.0.0.1\x00",
        )
        assert _pid_is_serve(4242, "18999") is True
        assert _pid_is_serve(4242, "1899") is False   # exact port only
        assert _pid_is_serve(4242, "189990") is False  # prefix must not match

        def _missing(path: str, *a: Any, **kw: Any) -> Any:
            raise FileNotFoundError(path)

        monkeypatch.setattr("builtins.open", _missing)
        assert _pid_is_serve(4242, "18999") is False  # unreadable → mismatch

    @pytest.mark.asyncio
    async def test_young_serve_not_recycled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from opencode_bridge import _recycle_serve_if_low_memory

        killed: list[int] = []

        def _health(port: str) -> tuple[bool, float]:
            return False, 60.0  # healthy memory, fresh serve

        def _find(port: str) -> int:
            return 12345

        monkeypatch.setattr(opencode_bridge, "_serve_health", _health)
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", _find)
        monkeypatch.setattr(opencode_bridge.os, "kill", lambda pid, sig: killed.append(pid))

        await _recycle_serve_if_low_memory()
        assert killed == []

    def test_find_serve_pid_matches_nul_separated_cmdline(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression (2026-08-07): /proc/<pid>/cmdline separates argv with
        NUL bytes, so the old space-form match ("--port 18900") never
        matched ANY process — serve recycling silently did nothing and a
        wedged tool runner lived forever.  The matcher must normalize NULs
        to spaces first."""
        from opencode_bridge import _find_serve_pid

        real_listdir = opencode_bridge.os.listdir
        real_open = open

        fake_cmdline = (
            b"/home/user/.opencode/bin/opencode\x00serve\x00"
            b"--port\x0018999\x00--hostname\x00127.0.0.1\x00"
        )

        def _fake_listdir(path: str) -> list[str]:
            if path == "/proc":
                return ["4242"]
            return real_listdir(path)

        def _fake_open(path: str, *a: Any, **kw: Any) -> Any:
            if str(path) == "/proc/4242/cmdline":
                return _FakeProc(fake_cmdline)
            return real_open(path, *a, **kw)

        class _FakeProc:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def __enter__(self) -> "_FakeProc":
                return self

            def __exit__(self, *a: Any) -> None:
                return None

            def read(self) -> bytes:
                return self._data

        monkeypatch.setattr(opencode_bridge.os, "listdir", _fake_listdir)
        monkeypatch.setattr("builtins.open", _fake_open)

        assert _find_serve_pid("18999") == 4242
        assert _find_serve_pid("18000") is None


class _FakeProcFile:
    """File-like stand-in returning a scripted /proc/<pid>/cmdline blob."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> "_FakeProcFile":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._data


def _patch_proc_cmdline(
    monkeypatch: pytest.MonkeyPatch, cmdline: bytes,
) -> None:
    """Point the /proc scan at a single fake entry (pid 4242) whose cmdline
    is ``cmdline`` (replicates the TestServeHealth._fake_listdir/_fake_open
    pattern)."""
    real_listdir = opencode_bridge.os.listdir
    real_open = open

    def _fake_listdir(path: str) -> list[str]:
        if path == "/proc":
            return ["4242"]
        return real_listdir(path)

    def _fake_open(path: str, *a: Any, **kw: Any) -> Any:
        if str(path) == "/proc/4242/cmdline":
            return _FakeProcFile(cmdline)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(opencode_bridge.os, "listdir", _fake_listdir)
    monkeypatch.setattr("builtins.open", _fake_open)


class TestFindServePidExactMatch:
    """REQ-2 (bridge-cycle-8): serve-PID discovery matches the port by EXACT
    token equality — equals-form or adjacent ``--port`` pair — never by
    substring/prefix (the ``--port 189990`` prefix-false-positive and the
    ``--port=18999`` never-match bugs)."""

    def test_equals_form_port_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from opencode_bridge import _find_serve_pid

        _patch_proc_cmdline(
            monkeypatch,
            b"/home/user/.opencode/bin/opencode\x00serve\x00"
            b"--port=18999\x00--hostname\x00127.0.0.1\x00",
        )
        assert _find_serve_pid("18999") == 4242

    def test_longer_advertised_value_rejected(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Advertises ``--port 189990``; searching "18999" must NOT
        prefix-match the longer advertised value."""
        from opencode_bridge import _find_serve_pid

        _patch_proc_cmdline(
            monkeypatch,
            b"/home/user/.opencode/bin/opencode\x00serve\x00"
            b"--port\x00189990\x00",
        )
        assert _find_serve_pid("18999") is None

    def test_shorter_search_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Advertises ``--port 18999``; searching "1899" must NOT match."""
        from opencode_bridge import _find_serve_pid

        _patch_proc_cmdline(
            monkeypatch,
            b"/home/user/.opencode/bin/opencode\x00serve\x00"
            b"--port\x0018999\x00",
        )
        assert _find_serve_pid("1899") is None


    def test_serve_health_parses_proc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_serve_health reads /proc/<pid>/stat start ticks + uptime."""
        import tempfile
        from opencode_bridge import _serve_health

        fake_pid = 4242
        # pid found in /proc listing
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", lambda port: fake_pid)
        monkeypatch.setattr(opencode_bridge, "_memory_pressure", lambda: False)

        stat = "0 (serve) S " + " 0 " * 18 + " 1000"  # start_ticks=1000 at index 21
        real_open = open

        def _fake_open(path: str, *a: Any, **kw: Any) -> Any:
            if f"/proc/{fake_pid}/stat" in str(path):
                return _FakeProc(stat.encode())
            if path == "/proc/uptime":
                return _FakeProc(b"6000.0 123.0\n")
            return real_open(path, *a, **kw)

        class _FakeProc:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def __enter__(self) -> "_FakeProc":
                return self

            def __exit__(self, *a: Any) -> None:
                return None

            def read(self) -> bytes:
                return self._data

        monkeypatch.setattr(opencode_bridge.os, "sysconf", lambda name: 100)
        monkeypatch.setattr("builtins.open", _fake_open)

        pressure, elapsed = _serve_health("18900")
        assert pressure is False
        assert elapsed is not None
        # start_ticks 1000 @ 100Hz = 10s after boot; uptime 6000 → ~5990s up
        assert 5900 < elapsed < 6000


@pytest.mark.real_recycle
class TestServeDrain:
    """REQ-3 (bridge-cycle-8): after a verified SIGTERM both recycle helpers
    boundedly drain the dying listener — break early when it reports down,
    exhaust the probe budget without raising otherwise.  ``real_recycle``
    keeps the hermetic fixture from nooping the helper bodies; the fake pid
    424242 keeps the live serve untouched."""

    def _patch_drain(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> list[int]:
        killed: list[int] = []
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", lambda port: 424242)
        # REQ-3 (bridge-cycle-4): the kill primitives verify the pid's
        # cmdline before signalling — confirm the fake pid.
        monkeypatch.setattr(opencode_bridge, "_pid_is_serve", lambda pid, port: True)
        monkeypatch.setattr(
            opencode_bridge.os, "kill", lambda pid, sig: killed.append(pid),
        )
        monkeypatch.setattr(opencode_bridge, "_DRAIN_PROBES", 4)
        monkeypatch.setattr(opencode_bridge, "_DRAIN_PROBE_S", 0.01)
        return killed

    @pytest.mark.asyncio
    async def test_force_recycle_drain_breaks_early_when_down(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Scenario-1: the listener reports down on probe 3 — 2 "up" probes
        + 1 "down" check, then an early return (3 running-fn calls)."""
        from opencode_bridge import _force_recycle_serve

        killed = self._patch_drain(monkeypatch)
        running_calls: list[bool] = []

        async def _running() -> bool:
            running_calls.append(True)
            return len(running_calls) < 3  # True, True, then False

        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running", _running)

        await _force_recycle_serve()

        assert killed == [424242]
        assert len(running_calls) == 3

    @pytest.mark.asyncio
    async def test_force_recycle_drain_exhausts_budget_without_raising(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Scenario-2: the listener stays up through every probe — exactly
        one running-fn call per probe slot (4), no raise."""
        from opencode_bridge import _force_recycle_serve

        killed = self._patch_drain(monkeypatch)
        running_calls = 0

        async def _always_up() -> bool:
            nonlocal running_calls
            running_calls += 1
            return True

        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running", _always_up)

        await _force_recycle_serve()

        assert killed == [424242]
        assert running_calls == 4

    @pytest.mark.asyncio
    async def test_low_memory_recycle_drain_breaks_early_when_down(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Scenario-1 for ``_recycle_serve_if_low_memory``: memory pressure
        truthy → kill fires, then the drain breaks early on probe 3."""
        from opencode_bridge import _recycle_serve_if_low_memory

        killed = self._patch_drain(monkeypatch)
        # _serve_health signature: (port) -> (memory_pressure, elapsed_s).
        monkeypatch.setattr(opencode_bridge, "_serve_health", lambda port: (True, 0.0))
        running_calls: list[bool] = []

        async def _running() -> bool:
            running_calls.append(True)
            return len(running_calls) < 3  # True, True, then False

        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running", _running)

        await _recycle_serve_if_low_memory()

        assert killed == [424242]
        assert len(running_calls) == 3

    @pytest.mark.asyncio
    async def test_low_memory_recycle_drain_exhausts_budget_without_raising(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Scenario-2 for ``_recycle_serve_if_low_memory``: listener stays up
        — exactly 4 running-fn calls, no raise."""
        from opencode_bridge import _recycle_serve_if_low_memory

        killed = self._patch_drain(monkeypatch)
        monkeypatch.setattr(opencode_bridge, "_serve_health", lambda port: (True, 0.0))
        running_calls = 0

        async def _always_up() -> bool:
            nonlocal running_calls
            running_calls += 1
            return True

        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running", _always_up)

        await _recycle_serve_if_low_memory()

        assert killed == [424242]
        assert running_calls == 4


# ---------------------------------------------------------------------------
# Serve stability mode + config-drift (REQ-4/5)
# ---------------------------------------------------------------------------
def _scripted_running(script: list[bool]) -> Any:
    """Factory for ``is_opencode_serve_running`` scripts: yields the given
    values in order, then True forever."""
    state = {"i": 0}

    async def _running() -> bool:
        i = state["i"]
        state["i"] += 1
        if i < len(script):
            return script[i]
        return True

    return _running


@pytest.mark.real_recycle
class TestServeStability:
    """Serve stability mode (REQ-4) + config-drift auto-recycle (REQ-5).

    The real process boundary is patched everywhere (create_subprocess_exec,
    _find_serve_pid → fake 12345, os.kill → recorder) so nothing real is
    spawned or killed.  ``real_recycle`` keeps the hermetic fixture's noop
    patches from masking the recycle path; the fake pid keeps the
    serve-untouched guard vacuously satisfied."""

    @pytest.mark.asyncio
    async def test_serve_spawn_pure_flag(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Candidate B is the default: spawn carries NO ``--pure`` and the
        serve env gets XDG_CONFIG_HOME=serve-config; ``OPENCODE_SERVE_PURE``
        toggles candidate A (``--pure`` appended)."""
        calls: list[list[str]] = []
        envs: list[dict[str, str]] = []
        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running",
                            _scripted_running([False]))
        monkeypatch.setattr(opencode_bridge, "_sync_serve_config", lambda: None)
        monkeypatch.setattr(opencode_bridge, "_open_serve_log", lambda: None)

        async def _exec(*args: Any, **kwargs: Any) -> Any:
            calls.append(list(args))
            envs.append(kwargs.get("env", {}))
            return SimpleNamespace(pid=4242)

        monkeypatch.setattr(opencode_bridge.asyncio, "create_subprocess_exec", _exec)

        ok = await opencode_bridge.ensure_opencode_serve()
        assert ok is True
        assert len(calls) == 1
        assert calls[0][0] == opencode_bridge.OPENCODE_BIN
        assert "serve" in calls[0]
        assert "--pure" not in calls[0]
        assert envs[0]["XDG_CONFIG_HOME"] == opencode_bridge.OPENCODE_SERVE_CONFIG_DIR
        # Serve isolation: data + cache are redirected into the serve dir,
        # never shared with the TUI (2026-08-09).
        assert envs[0]["XDG_DATA_HOME"] == opencode_bridge.OPENCODE_SERVE_CONFIG_DIR
        assert envs[0]["XDG_CACHE_HOME"].startswith(
            opencode_bridge.OPENCODE_SERVE_CONFIG_DIR,
        )
        # Cache updated after the successful spawn (drift gate baseline).
        assert opencode_bridge._serve_config_mtime == \
            opencode_bridge.os.path.getmtime(opencode_bridge.OPCODE_CONFIG_PATH)

        # Candidate A: OPENCODE_SERVE_PURE appends --pure.
        monkeypatch.setattr(opencode_bridge, "OPENCODE_SERVE_PURE", True)
        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running",
                            _scripted_running([False]))
        ok = await opencode_bridge.ensure_opencode_serve()
        assert ok is True
        assert "--pure" in calls[-1]

    @pytest.mark.asyncio
    async def test_config_drift_recycles_serve(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A serve running with an older cached mtime is recycled (fake pid
        12345) and respawned when OPCODE_CONFIG_PATH is newer, and the cache
        is updated (REQ-5 Scenario-1)."""
        killed: list[int] = []
        spawns: list[list[str]] = []
        monkeypatch.setattr(opencode_bridge, "_serve_config_mtime", 1000.0)
        monkeypatch.setattr(opencode_bridge, "_config_mtime", lambda: 2000.0)
        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running",
                            _scripted_running([True, False, True]))
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", lambda port: 12345)
        # REQ-3 (bridge-cycle-4): verify the pid before the drift kill.
        monkeypatch.setattr(opencode_bridge, "_pid_is_serve", lambda pid, port: True)
        monkeypatch.setattr(opencode_bridge.os, "kill",
                            lambda pid, sig: killed.append(pid))
        monkeypatch.setattr(opencode_bridge, "_sync_serve_config", lambda: None)
        monkeypatch.setattr(opencode_bridge, "_open_serve_log", lambda: None)

        async def _exec(*args: Any, **kwargs: Any) -> Any:
            spawns.append(list(args))
            return SimpleNamespace(pid=4242)

        monkeypatch.setattr(opencode_bridge.asyncio, "create_subprocess_exec", _exec)

        ok = await opencode_bridge.ensure_opencode_serve()
        assert ok is True
        assert killed == [12345]  # recycle fired exactly once
        assert len(spawns) == 1  # respawned once
        assert opencode_bridge._serve_config_mtime == 2000.0  # cache updated

    @pytest.mark.asyncio
    async def test_no_drift_no_recycle(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A running serve whose template mtime matches the cache is left
        alone: no recycle, no respawn."""
        killed: list[int] = []
        spawns: list[list[str]] = []
        monkeypatch.setattr(opencode_bridge, "_serve_config_mtime", 2000.0)
        monkeypatch.setattr(opencode_bridge, "_config_mtime", lambda: 2000.0)
        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running",
                            _scripted_running([True]))
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", lambda port: 12345)
        monkeypatch.setattr(opencode_bridge.os, "kill",
                            lambda pid, sig: killed.append(pid))

        async def _exec(*args: Any, **kwargs: Any) -> Any:
            spawns.append(list(args))
            return SimpleNamespace(pid=4242)

        monkeypatch.setattr(opencode_bridge.asyncio, "create_subprocess_exec", _exec)

        ok = await opencode_bridge.ensure_opencode_serve()
        assert ok is True
        assert killed == []
        assert spawns == []

    @pytest.mark.asyncio
    async def test_cache_adopts_baseline_after_restart(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Proxy restarted while the serve survived: the empty cache adopts
        the current template mtime as baseline without recycling."""
        killed: list[int] = []
        monkeypatch.setattr(opencode_bridge, "_serve_config_mtime", None)
        monkeypatch.setattr(opencode_bridge, "_config_mtime", lambda: 2000.0)
        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running",
                            _scripted_running([True]))
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", lambda port: 12345)
        monkeypatch.setattr(opencode_bridge.os, "kill",
                            lambda pid, sig: killed.append(pid))

        ok = await opencode_bridge.ensure_opencode_serve()
        assert ok is True
        assert killed == []
        assert opencode_bridge._serve_config_mtime == 2000.0

    @pytest.mark.asyncio
    async def test_recycle_never_touches_unmatched_pid(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The drift recycle signals ONLY the pid ``_find_serve_pid``
        matched (fake 12345) — never any other process (e.g. the user TUI);
        when no pid matches, nothing is signaled."""
        killed: list[int] = []
        spawns: list[list[str]] = []
        monkeypatch.setattr(opencode_bridge, "_serve_config_mtime", 1000.0)
        monkeypatch.setattr(opencode_bridge, "_config_mtime", lambda: 2000.0)
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", lambda port: 12345)
        # REQ-3 (bridge-cycle-4): verify the matched pid before the kill.
        monkeypatch.setattr(opencode_bridge, "_pid_is_serve", lambda pid, port: True)
        monkeypatch.setattr(opencode_bridge.os, "kill",
                            lambda pid, sig: killed.append(pid))
        monkeypatch.setattr(opencode_bridge, "_sync_serve_config", lambda: None)
        monkeypatch.setattr(opencode_bridge, "_open_serve_log", lambda: None)

        async def _exec(*args: Any, **kwargs: Any) -> Any:
            spawns.append(list(args))
            return SimpleNamespace(pid=4242)

        monkeypatch.setattr(opencode_bridge.asyncio, "create_subprocess_exec", _exec)

        # Matched pid → exactly that one is signaled, nothing else.
        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running",
                            _scripted_running([True, False, True]))
        ok = await opencode_bridge.ensure_opencode_serve()
        assert ok is True
        assert killed == [12345]
        assert len(spawns) == 1

        # No matching pid → nothing signaled; spawn still proceeds.
        monkeypatch.setattr(opencode_bridge, "_serve_config_mtime", 1000.0)
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", lambda port: None)
        monkeypatch.setattr(opencode_bridge, "is_opencode_serve_running",
                            _scripted_running([True, False, True]))
        ok = await opencode_bridge.ensure_opencode_serve()
        assert ok is True
        assert killed == [12345]  # unchanged — no new signals
        assert len(spawns) == 2


# ---------------------------------------------------------------------------
# Zombie sweep / pinned-session protection
# ---------------------------------------------------------------------------
class _ZombieSessionClient(_FakeClient):
    """_FakeClient whose GET /session returns scripted session records.

    The default _FakeClient returns {} for /session, but the sweep iterates
    the JSON list — this variant scripts the exact record list it needs."""

    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        super().__init__()
        self.sessions = sessions

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        if url == f"{opencode_bridge.OPENCODE_SERVE_URL}/session":
            return _FakeResp(200, self.sessions)
        return await super().get(url, **kwargs)


class TestZombieSweep:
    @pytest.mark.asyncio
    async def test_pinned_session_never_aborted_unpinned_stale_is(self) -> None:
        """A session pinned in the session map survives the zombie sweep
        even when idle far past the 240s threshold, while an unpinned
        session with the same stale age is still aborted.

        Regression (2026-08-07): the sweep killed pinned sessions
        legitimately waiting for user input (clarifying questions,
        multi-turn pauses), so the follow-up posted into a dead session
        and the agent lost all conversation context."""
        from opencode_bridge import _abort_zombie_sessions

        stale_ms = int(time.time() * 1000) - 1_000_000  # ~17 min idle
        pinned_id = "ses_pinned_waiting_on_user"
        unpinned_id = "ses_unpinned_stale"
        client = _ZombieSessionClient([
            {"id": pinned_id, "time": {"updated": stale_ms}},
            {"id": unpinned_id, "time": {"updated": stale_ms}},
        ])

        await _abort_zombie_sessions(client, protected_ids={pinned_id})

        aborts = [
            url for method, url in client.calls
            if method == "post" and "/abort" in url
        ]
        assert len(aborts) == 1
        assert unpinned_id in aborts[0]
        assert pinned_id not in aborts[0]

    @pytest.mark.asyncio
    async def test_no_protected_set_sweeps_all_stale(self) -> None:
        """Without a protected set (no session map at the call site) the
        sweep behaves as before: every stale session is aborted."""
        from opencode_bridge import _abort_zombie_sessions

        stale_ms = int(time.time() * 1000) - 1_000_000
        client = _ZombieSessionClient([
            {"id": "ses_a", "time": {"updated": stale_ms}},
            {"id": "ses_b", "time": {"updated": stale_ms}},
        ])

        await _abort_zombie_sessions(client)

        aborts = [
            url for method, url in client.calls
            if method == "post" and "/abort" in url
        ]
        assert len(aborts) == 2


# ---------------------------------------------------------------------------
# Wedged-tool detection (tool part stuck in "running" with no output)
# ---------------------------------------------------------------------------
class TestDetectWedgedTool:
    @pytest.mark.asyncio
    async def test_old_running_bash_part_without_output_is_wedged(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bash part running with no output for past the wedge threshold
        is detected as wedged (started AFTER the current serve)."""
        from opencode_bridge import _detect_wedged_tool

        part_start = int(time.time() * 1000) - 320_000
        monkeypatch.setattr(
            opencode_bridge, "_serve_start_epoch_ms", lambda: part_start - 60_000,
        )
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
    async def test_stale_part_from_dead_serve_not_wedged(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A running part started BEFORE the current serve process is
        debris from a dead serve — the resumed pinned session must NOT be
        aborted for it (2026-08-08: stale 'task' parts killed healthy
        resumed cycles)."""
        from opencode_bridge import _detect_wedged_tool

        part_start = int(time.time() * 1000) - 180_000
        monkeypatch.setattr(
            opencode_bridge, "_serve_start_epoch_ms", lambda: part_start + 60_000,
        )
        client = _PollClient()
        client.poll_messages = [_assistant_msg([{
            "id": "prt_bash", "messageID": "msg_a", "type": "tool",
            "tool": "bash",
            "state": {
                "status": "running",
                "time": {"start": part_start},
            },
        }])]

        assert await _detect_wedged_tool(client, client.session_id) is False

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

        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        killed: list[int] = []
        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_recycle_serve_if_low_memory", _noop)
        monkeypatch.setattr(opencode_bridge, "_detect_wedged_tool", _always_wedged)
        # Never kill a real opencode serve on this box.
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", lambda port: None)
        monkeypatch.setattr(opencode_bridge.os, "kill", lambda pid, sig: killed.append(pid))
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
        assert killed == []


class _RunningToolPermissionClient(_FakeClient):
    """Fake whose polling read shows a RUNNING tool in the newest assistant
    message plus optionally a pending write permission."""

    def __init__(self) -> None:
        super().__init__()
        self.permission_records: list[dict[str, Any]] = []
        self.status_type = "busy"
        self.write_tool: bool = False

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        if url.endswith(f"/session/{self.session_id}/message"):
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


class TestCompletionResolvesPermissions:
    """Root-cause regression (2026-08-07): the bridge used to pop pending
    permissions without answering them on completion, so the serve parked
    the tool forever and the next follow-up's wedge detector killed the
    session.  Two fixes: (1) completion must not fire while a running tool
    exists in the newest message; (2) when it does complete, it must
    RESOLVE the pending permission (auto-allow READ, question for WRITE)."""

    @pytest.mark.parametrize("tool_name,cmd,expected", [
        ("write", "/etc/*", "write"),
        ("edit", "/etc/*", "write"),
        ("patch", "/etc/*", "write"),
        ("bash", "cat /etc/os-release", "read"),
        ("bash", "mv /etc/foo /etc/bar", "write"),
    ])
    def test_permission_classification_tool_aware(
        self, tool_name: str, cmd: str, expected: str,
    ) -> None:
        """Review finding 2: the permission classifier must be TOOL-AWARE so
        a write/edit/patch tool to an external path is classified WRITE even
        though it has no bash command to inspect (the bash-only heuristic
        would read the path pattern and auto-allow the write).  All paths —
        event bus, polling fallback, completion resolver — use this one
        classifier."""
        from opencode_bridge import _classify_permission_access
        assert _classify_permission_access(
            "external_directory", tool_name, cmd,
        ) == expected

    @pytest.mark.parametrize("perm_type", ["write", "edit", "patch"])
    def test_write_edit_perm_type_classifies_write(self, perm_type: str) -> None:
        """F2: a permission whose TYPE is itself a write tool ("write"/"edit")
        must classify as WRITE with an EMPTY command.  The old code fell
        through to the cmd heuristic, read an empty cmd as "read", and
        auto-allowed the write even in interactive mode."""
        from opencode_bridge import _classify_permission_access
        assert _classify_permission_access(perm_type, "", "") == "write"

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

    @pytest.mark.asyncio
    async def test_completion_resolves_pending_read_permission(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the agent looks done (idle) but a READ permission is pending,
        the bridge auto-allows it (POST "always") instead of abandoning it."""
        from opencode_bridge import opencode_chat_stream

        async def _running(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(opencode_bridge, "ensure_opencode_serve", _running)
        monkeypatch.setattr(opencode_bridge, "_EVENT_QUIET_TIMEOUT", 0.02)
        monkeypatch.setattr(opencode_bridge, "_TOOL_WEDGE_AFTER_S", 60.0)
        client = _RunningToolPermissionClient()
        client.status_type = "idle"  # completion path
        client.permission_records = [{
            "id": "perm_read", "sessionID": "ses_0001",
            "permission": "external_directory",
            "patterns": ["/etc/systemd/system/*"],
            "tool": {"messageID": "msg_a", "callID": "call_x"},
        }]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]

        # The permission was ANSWERED (POST always), not abandoned.
        perm_posts = [(u, b) for u, b in client.post_calls if "/permissions/" in u]
        assert any(u.endswith("/permissions/perm_read") and b == {"response": "always"}
                   for u, b in perm_posts)
        # No pending entry leaked.
        assert PP.get("ses_0001") is None
