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
from typing import Any, AsyncIterator, Optional

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
        if self.raise_on == "post":
            raise httpx.ConnectError("conn refused")
        if url.endswith("/session"):
            return _FakeResp(self.session_status, {"id": self.session_id})
        if "prompt_async" in url:
            return _FakeResp(204, {})
        return _FakeResp(self.message_status, {"parts": self.message_parts})

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStream:
        return _FakeStream(self.stream_lines)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    client = _FakeClient()
    monkeypatch.setattr(opencode_bridge.httpx, "AsyncClient", lambda *a, **k: client)
    return client


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

        async def _fake_chat(text: str, *, agent: str = "gentle-orchestrator") -> str:
            assert text == "write a test"
            return "BRIDGE_DONE"

        monkeypatch.setattr("routes.opencode_chat", _fake_chat)
        resp = await _handle_opencode_request(
            [{"role": "user", "content": "write a test"}],
            client_stream=False,
        )
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
    async def test_opencode_command_strips_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from routes import _handle_opencode_command

        captured: list[str] = []

        async def _fake_chat(text: str, *, agent: str = "gentle-orchestrator") -> str:
            captured.append(text)
            return "CMD_DONE"

        monkeypatch.setattr("routes.opencode_chat", _fake_chat)
        resp = await _handle_opencode_command("/opencode implement the parser")
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

        deltas = [d async for d in opencode_chat_stream("task")]
        kinds = [k for k, _ in deltas]
        joined = "".join(t for _, t in deltas)
        # Reasoning streamed with its own kind; user echo excluded.
        assert "reasoning" in kinds
        assert "text" in kinds
        assert "think about it" in joined
        assert "the echoed prompt" not in joined
        # Both assistant messages' text streamed.
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

        deltas = [d async for d in opencode_chat_stream("task")]
        joined = "".join(t for _, t in deltas)
        assert any("session HTTP 500" in t for _, t in deltas)


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

        deltas = [d async for d in opencode_chat_stream("task")]
        assert deltas == [("question", "Which source? (Options: Logs | Git)")]

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

        deltas = [d async for d in opencode_chat_stream("task")]
        assert ("status", "🔧 write…") in deltas
        assert ("status", "✅ write done") in deltas


# ---------------------------------------------------------------------------
# Serve health / age-based recycling
# ---------------------------------------------------------------------------
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

        def _fake_open(path: str, *a: Any, **kw: Any):
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
