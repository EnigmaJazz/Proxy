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
import sys
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
        self.stream_lines: list[str] = []
        self.message_get_parts: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        self.calls.append(("get", url))
        if self.raise_on == "get":
            raise httpx.ConnectError("conn refused")
        if "/message/" in url:
            # Persisted-message GET used by the question retry-race fetch.
            return _FakeResp(200, {"info": {}, "parts": self.message_get_parts})
        if "/session/status" in url:
            # Event-stream end → polling fallback → session is idle (done).
            return _FakeResp(200, {self.session_id: {"type": "idle"}})
        return _FakeResp(self.get_status, {})

    async def post(self, url: str, **kwargs: Any) -> _FakeResp:
        self.calls.append(("post", url))
        self.post_calls.append((url, kwargs.get("json")))
        if self.raise_on == "post":
            raise httpx.ConnectError("conn refused")
        if url.endswith("/session"):
            return _FakeResp(self.session_status, {"id": self.session_id})
        if "prompt_async" in url:
            return _FakeResp(204, {})
        return _FakeResp(self.message_status, {"parts": self.message_parts})

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStream:
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
    async def test_recorder_captures_wedge_kill(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The direct wedge-recycle ``os.kill`` (opencode_bridge.py:876/918)
        is NOT routed through the nooped primitives — the recorder must
        capture it so a future serve-pid kill trips the teardown guard."""
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
        # Fake, non-matching pid — never a real process.  Stub the captured
        # real os.kill so the recorder's pass-through cannot signal anything.
        monkeypatch.setattr(opencode_bridge, "_find_serve_pid", lambda port: 424242)
        monkeypatch.setattr(
            sys.modules[__name__], "_HERMETIC_REAL_KILL", lambda *a, **k: None,
        )
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
        # The wedge-kill os.kill was recorded → the guard can detect it.
        assert 424242 in HERMETIC_KILLED_PIDS


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


class TestIsRunning:
    @pytest.mark.asyncio
    async def test_running_when_200(self, fake_client: _FakeClient) -> None:
        assert await is_opencode_serve_running() is True

    @pytest.mark.asyncio
    async def test_not_running_on_error(self, fake_client: _FakeClient) -> None:
        fake_client.raise_on = "get"
        assert await is_opencode_serve_running() is False


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


# ---------------------------------------------------------------------------
# External-directory permission relay (opencode >= 1.18 external_directory
# gate): reads auto-allowed, writes asked to the user.
# ---------------------------------------------------------------------------
class TestClassifyExternalAccess:
    """_classify_external_access must tag mutating commands WRITE (needs
    user approval) and read-only commands READ (auto-allowed)."""

    @pytest.mark.parametrize("cmd", [
        "echo x > /etc/foo",
        "echo x >> ~/log",
        "mv /etc/foo /etc/bar",
        "cp /etc/passwd ~/",
        "rm -rf ~/cache",
        "touch ~/foo",
        "mkdir ~/out",
        "rmdir ~/old",
        "ln -s /etc/hosts ~/hosts",
        "chmod 644 ~/file",
        "chown james ~/file",
        "tee /etc/foo",
        "sed -i s/x/y/ /etc/hosts",
        "install -m 755 app /usr/local/bin/app",
        "dd if=/dev/zero of=~/big",
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
                 title="Allow writing to ~/out.txt",
                 metadata={"filepath": "~/out.txt"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]

        assert len(deltas) == 1
        kind, text = deltas[0]
        assert kind == "question"
        assert "outside its workspace" in text
        assert "Target: ~/out.txt" in text
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
                 title="Allow writing to ~/out.txt",
                 metadata={"filepath": "~/out.txt"}),
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
            "patterns": ["~/out.txt"],
            "tool": {"messageID": "msg_a", "callID": "call_w"},
        }]
        client.stream_lines = []  # empty event bus → polling fallback
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        deltas = [d async for d in opencode_chat_stream("task", pending_permissions=PP)]

        questions = [(k, t) for k, t in deltas if k == "question"]
        assert len(questions) == 1
        _, text = questions[0]
        assert "Target: ~/out.txt" in text
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
                 title="Allow writing to ~/out.txt",
                 metadata={"filepath": "~/out.txt"}),
        ]
        monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)

        resp = await _handle_opencode_command(
            "/opencode write config", _FakeApp(pending=PP),
        )
        text = await _drain_stream(resp)
        # The write question is relayed as visible SSE content — never dropped.
        assert "Target: ~/out.txt" in text
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
        monkeypatch.setattr(opencode_bridge.os, "kill", lambda pid, sig: killed.append(pid))

        await _recycle_serve_if_low_memory()
        assert killed == [12345]

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
            b"~/.opencode/bin/opencode\x00serve\x00"
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
    async def test_old_running_bash_part_without_output_is_wedged(self) -> None:
        """A bash part running with no output for past the wedge threshold
        is detected as wedged."""
        from opencode_bridge import _detect_wedged_tool

        client = _PollClient()
        client.poll_messages = [_assistant_msg([{
            "id": "prt_bash", "messageID": "msg_a", "type": "tool",
            "tool": "bash",
            "state": {
                "status": "running",
                "time": {"start": int(time.time() * 1000) - 180_000},
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
    async def test_wedged_tool_aborts_drops_pin_and_recycles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wedged tool part terminates the stream with a clear error:
        the session is aborted, the pin dropped, and the serve recycled
        (never streaming keepalives indefinitely)."""
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
        assert "session aborted, serve recycled" in wedged[0]
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
