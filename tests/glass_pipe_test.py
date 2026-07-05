"""Regression tests for the Glass-Pipe Hardening change (PR1 + PR2).

One test class per R requirement.  PR1 covers R1, R2, R7, R8, R9, R10, R11.
PR2 will extend this file with R3, R4, R5, R6, R12, R13, R14, R15, R16.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import proxy
from routes import _PAUSE_RE, _RESUME_RE


# ---------------------------------------------------------------------------
# SSE parsing helpers
# ---------------------------------------------------------------------------
def _parse_sse_lines(raw: bytes) -> list[str]:
    """Split raw SSE response into individual lines."""
    return raw.decode("utf-8").splitlines()


def _extract_event_lines(lines: list[str], prefix: str = "event: kinver.proxy.") -> list[dict[str, Any]]:
    """Return parsed JSON payloads for every SSE event line matching ``prefix``."""
    events: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            data_line = lines[i + 1] if i + 1 < len(lines) else ""
            if data_line.startswith("data: "):
                events.append(json.loads(data_line[len("data: "):]))
    return events


# ---------------------------------------------------------------------------
# PR1: Parameter plane
# ---------------------------------------------------------------------------
class TestPassthrough:
    """R1, R7, R11 — client-sent parameters and OpenAI fields are forwarded."""

    async def test_R1_client_wins_temperature_top_p_max_tokens(self, app_client: httpx.AsyncClient) -> None:
        """Client-sent temperature/top_p/max_tokens survive intent defaults."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        classification = {
            "is_valid": True,
            "intent": "CODE",
            "priority": 1,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.7,
            "top_p": 0.95,
            "max_tokens": 2048,
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        assert captured, "stream_llm was not called"
        payload = captured[0]
        assert payload["temperature"] == 0.7
        assert payload["top_p"] == 0.95
        assert payload["max_tokens"] == 2048

    async def test_R7_client_wins_thinking_budget_tokens(self, app_client: httpx.AsyncClient) -> None:
        """Client-sent thinking_budget_tokens survives intent defaults."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        classification = {
            "is_valid": True,
            "intent": "CODE",
            "priority": 1,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "thinking_budget_tokens": 8192,
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        assert captured, "stream_llm was not called"
        assert captured[0]["thinking_budget_tokens"] == 8192

    async def test_R11_full_openai_field_set(self, app_client: httpx.AsyncClient) -> None:
        """Optional OpenAI fields are forwarded when client sends them."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        classification = {
            "is_valid": True,
            "intent": "CODE",
            "priority": 1,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "seed": 42,
            "response_format": {"type": "json_object"},
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        assert captured, "stream_llm was not called"
        payload = captured[0]
        assert payload["tool_choice"] == "auto"
        assert payload["parallel_tool_calls"] is True
        assert payload["seed"] == 42
        assert payload["response_format"] == {"type": "json_object"}
        assert "frequency_penalty" not in payload

    async def test_R1_client_omits_temperature_uses_intent_default(self, app_client: httpx.AsyncClient) -> None:
        """Client omits temperature → CODE intent default fills the gap."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        classification = {
            "is_valid": True,
            "intent": "CODE",
            "priority": 1,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        body = {"messages": [{"role": "user", "content": "hello"}]}
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        assert captured, "stream_llm was not called"
        assert captured[0]["temperature"] == 0.1

    async def test_R7_client_omits_thinking_budget_uses_intent_default(self, app_client: httpx.AsyncClient) -> None:
        """Client omits thinking_budget_tokens → CODE intent default fills the gap."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        classification = {
            "is_valid": True,
            "intent": "CODE",
            "priority": 1,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        body = {"messages": [{"role": "user", "content": "hello"}]}
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        assert captured, "stream_llm was not called"
        assert captured[0]["thinking_budget_tokens"] == 4096


class TestExceptions:
    """R2, R8, R9 — intentional Glass-Pipe exceptions are documented."""

    async def test_R2_doc_labels_present_param_overrides(self, app_client: httpx.AsyncClient) -> None:
        """Parameter intent-default block carries an intentional-exception label."""
        repo_root = Path(__file__).parent.parent
        routes_source = (repo_root / "routes.py").read_text()
        assert "Glass Pipe exception" in routes_source
        assert "intent defaults only fill gaps" in routes_source

    async def test_R8_doc_labels_present_translate_to_deepseek_r1(self, app_client: httpx.AsyncClient) -> None:
        """translate_to_deepseek_r1 carries an intentional-exception label."""
        repo_root = Path(__file__).parent.parent
        llm_source = (repo_root / "llm.py").read_text()
        assert "Glass Pipe exception" in llm_source
        assert "translate_to_deepseek_r1" in llm_source

    async def test_R9_doc_labels_present_stop_seq_filter(self, app_client: httpx.AsyncClient) -> None:
        """Stop-sequence filter carries an intentional-exception label."""
        repo_root = Path(__file__).parent.parent
        routes_source = (repo_root / "routes.py").read_text()
        assert "Glass Pipe exception" in routes_source
        assert "ReAct-era stops" in routes_source or "mid-tool-call" in routes_source


class TestStreamIntegrity:
    """R3, R4, R5, R6, R14 — SSE stream integrity and proxy events."""

    async def test_R3_lane_b_tools_passthrough(self, app_client: httpx.AsyncClient) -> None:
        """Lane B/IDE callers forward client tools unchanged."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        tools = [{"type": "function", "function": {"name": "read_file"}}]
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": tools,
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            response = await app_client.post(
                "/v1/chat/completions",
                json=body,
                headers={"sk-ide-pass": "sk-ide-pass"},
            )
        assert response.status_code == 200
        assert captured, "stream_llm was not called"
        assert captured[0].get("tools") == tools

    async def test_R4_tool_stripped_event_emitted(self, app_client: httpx.AsyncClient) -> None:
        """Loop detection emits kinver.proxy.tool_stripped before the first chunk."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            yield {"choices": [{"delta": {"content": "ok"}}]}

        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.detect_tool_loops", new=AsyncMock(return_value=(True, "repeated_tool_call"))):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        lines = _parse_sse_lines(response.content)
        events = _extract_event_lines(lines)
        tool_stripped = [e for e in events if e.get("kind") == "tool_stripped"]
        assert tool_stripped, "tool_stripped event not found"
        assert tool_stripped[0]["data"]["reason"] == "repeated_tool_call"
        # The event must appear before any model data chunk.
        first_data_idx = next(
            (i for i, line in enumerate(lines) if line.startswith("data: {")),
            None,
        )
        event_idx = next(
            (i for i, line in enumerate(lines) if line == "event: kinver.proxy.tool_stripped"),
            None,
        )
        assert event_idx is not None and first_data_idx is not None
        assert event_idx < first_data_idx

    async def test_R5_proxy_status_events(self, app_client: httpx.AsyncClient) -> None:
        """Triage banner is emitted as kinver.proxy.status, not delta.content."""

        async def _fake_stream(*, payload: dict, **kwargs):
            yield {"choices": [{"delta": {"content": "hello"}}]}

        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "model": "professional",
        }
        classification = {
            "is_valid": True,
            "intent": "CHAT",
            "priority": 2,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        lines = _parse_sse_lines(response.content)
        events = _extract_event_lines(lines)
        status_events = [e for e in events if e.get("kind") == "status"]
        assert status_events, "kinver.proxy.status event not found"
        triage_events = [e for e in status_events if e.get("data", {}).get("kind") == "triage"]
        assert triage_events, "triage kind not found"
        # No banner text should appear inside a content delta.
        for line in lines:
            if line.startswith("data: {"):
                try:
                    chunk = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                assert "Proxy triage" not in (content or ""), "triage leaked into delta.content"

    async def test_R14_command_status_events(self, app_client: httpx.AsyncClient) -> None:
        """Pause/resume/cloud embedded commands emit kinver.proxy.status."""
        from routes import _handle_pause_command

        mock_state = MagicMock()
        mock_state.try_pause_queue = AsyncMock(return_value=(True, ""))
        response = await _handle_pause_command(5, mock_state)
        body = "".join([chunk async for chunk in response.body_iterator]).encode("utf-8")
        lines = _parse_sse_lines(body)
        events = _extract_event_lines(lines)
        pause_events = [
            e for e in events
            if e.get("kind") == "status" and e.get("data", {}).get("kind") == "pause"
        ]
        assert pause_events, "pause status event not found"
        # No synthetic content chunk should carry the pause text.
        for line in lines:
            if line.startswith("data: {"):
                try:
                    chunk = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                assert "Queue paused" not in (content or ""), "pause text leaked into delta.content"


class TestGuillotine:
    """R6 — audit halts emit kinver.proxy.audit_halt and a clean error chunk."""

    async def test_R6_audit_halt_event_and_error_chunk(self, app_client: httpx.AsyncClient) -> None:
        """FATAL audit emits audit_halt event and a standards-compliant error chunk."""

        class _FatalAuditor:
            def __init__(self):
                self._on_fatal = None

            @staticmethod
            def should_audit(*args, **kwargs):
                return True

            def start(self, *, on_fatal, **kwargs):
                self._on_fatal = on_fatal

            def feed_chunk(self, chunk: dict) -> None:
                if self._on_fatal:
                    asyncio.get_event_loop().call_soon(
                        asyncio.create_task, self._on_fatal("job-id", "unsafe output")
                    )

            def stop(self) -> None:
                pass

        async def _fake_stream(*, payload: dict, **kwargs):
            yield {"choices": [{"delta": {"content": "step 1"}}]}
            # Yield control so the fatal callback task can run.
            await asyncio.sleep(0.01)
            yield {"choices": [{"delta": {"content": "more"}}]}

        proxy.app.state.auditor = _FatalAuditor()
        body = {
            "messages": [{"role": "user", "content": "hello"}],
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.ShadowAuditor.should_audit", return_value=True):
                response = await app_client.post(
                "/v1/chat/completions",
                json=body,
                headers={"Authorization": "Bearer agent-key"},
            )
        assert response.status_code == 200
        lines = _parse_sse_lines(response.content)
        events = _extract_event_lines(lines)
        halt_events = [e for e in events if e.get("kind") == "audit_halt"]
        assert halt_events, "audit_halt event not found"
        assert halt_events[0]["data"]["reason"] == "unsafe output"

        # Find the final data: chunk (last JSON before [DONE]).
        data_chunks = [
            json.loads(line[len("data: "):])
            for line in lines
            if line.startswith("data: {")
        ]
        final_chunk = data_chunks[-1]
        assert final_chunk["choices"][0]["finish_reason"] == "stop"
        assert final_chunk.get("error", {}).get("type") == "proxy_audit_halt"
        assert "content" not in final_chunk["choices"][0].get("delta", {})


class TestMidToolFlowOptOut:
    """R13 — X-Kinver-Allow-Mid-Tool-Switch header disables the implicit lock."""

    async def test_R13_mid_tool_flow_opt_out(self, app_client: httpx.AsyncClient) -> None:
        """Header present → frontdesk classification runs despite tool history."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        classification = {
            "is_valid": True,
            "intent": "CHAT",
            "priority": 2,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        body = {
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
                {"role": "tool", "content": "result", "tool_call_id": "c1"},
                {"role": "user", "content": "now what?"},
            ],
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)) as mock_cls:
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={
                        "Authorization": "Bearer agent-key",
                        "X-Kinver-Allow-Mid-Tool-Switch": "true",
                    },
                )
        assert response.status_code == 200
        mock_cls.assert_awaited_once()

    async def test_R13_mid_tool_flow_lock_preserved_without_header(self, app_client: httpx.AsyncClient) -> None:
        """No opt-out header → frontdesk is skipped and the implicit lock applies."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        classification = {
            "is_valid": True,
            "intent": "CHAT",
            "priority": 2,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        body = {
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
                {"role": "tool", "content": "result", "tool_call_id": "c1"},
                {"role": "user", "content": "now what?"},
            ],
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)) as mock_cls:
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        mock_cls.assert_not_awaited()


class TestAuditorCoverage:
    """R12, R15, R16 — auditor and DB observe tool_calls deltas."""

    async def test_R12_auditor_sees_tool_calls(self, app_client: httpx.AsyncClient) -> None:
        """ShadowAuditor.feed_chunk accepts a chunk dict and observes tool_calls."""
        import auditing
        import importlib

        # Conftest replaced ShadowAuditor with a no-op; reload to test the real class.
        saved_class = auditing.ShadowAuditor
        importlib.reload(auditing)
        try:
            auditor = auditing.ShadowAuditor(None, None, None)
            auditor.start(job_id="j1", project_id="p1", messages=[])
            auditor.feed_chunk({"choices": [{"delta": {"tool_calls": [{"id": "c1"}]}}]})
            auditor.feed_chunk({"choices": [{"delta": {"content": "text"}}]})
            await asyncio.sleep(0.05)
            auditor.stop()
            assert "c1" in auditor._accumulated_text
            assert "text" in auditor._accumulated_text
        finally:
            auditing.ShadowAuditor = saved_class

    async def test_R15_stream_extracts_tool_calls(self, app_client: httpx.AsyncClient) -> None:
        """Stream loop extracts delta.tool_calls and passes full chunks to auditor."""

        class _RecordingAuditor:
            def __init__(self):
                self.chunks: list[dict] = []
                self._on_fatal = None

            @staticmethod
            def should_audit(*args, **kwargs):
                return True

            def start(self, *, on_fatal, **kwargs):
                self._on_fatal = on_fatal

            def feed_chunk(self, chunk: dict) -> None:
                self.chunks.append(chunk)

            def stop(self) -> None:
                pass

        async def _fake_stream(*, payload: dict, **kwargs):
            yield {"choices": [{"delta": {"tool_calls": [{"id": "call_1"}]}}]}

        proxy.app.state.auditor = _RecordingAuditor()
        body = {"messages": [{"role": "user", "content": "hello"}]}
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.ShadowAuditor.should_audit", return_value=True):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        tool_chunks = [
            c for c in proxy.app.state.auditor.chunks
            if c.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
        ]
        assert tool_chunks, "tool_calls chunk not fed to auditor"

    async def test_R16_db_accumulates_tool_calls(self, app_client: httpx.AsyncClient) -> None:
        """complete_job stores tool_calls JSON and the helper parses it back."""
        import database
        import importlib

        saved_class = database.Database
        importlib.reload(database)
        try:
            db = database.Database(Path("/tmp/test_glass_pipe_r16.db"))
            await db.initialize()
            try:
                job_id = await db.enqueue_job(
                    messages_json=json.dumps([{"role": "user", "content": "hi"}]),
                    priority=2,
                    intent="CHAT",
                )
                tool_calls = [{"id": "call_1", "function": {"name": "read_file"}}]
                await db.complete_job(
                    job_id,
                    finish_reason="stop",
                    full_content="hello",
                    tool_calls_json=json.dumps(tool_calls),
                )
                row = await db.get_job(job_id)
                assert row is not None
                assert "|||TOOL_CALLS|||" in row["partial_content"]
                text, parsed = database.Database.parse_tool_calls_from_partial(
                    row["partial_content"]
                )
                assert text == "hello"
                assert parsed == tool_calls
            finally:
                await db.close()
                Path("/tmp/test_glass_pipe_r16.db").unlink(missing_ok=True)
        finally:
            database.Database = saved_class

    async def test_R16_text_only_stream_stores_empty_tool_calls(self, app_client: httpx.AsyncClient) -> None:
        """Stream with content only calls complete_job with empty tool_calls_json."""

        async def _fake_stream(*, payload: dict, **kwargs):
            yield {"choices": [{"delta": {"content": "hello"}}]}

        proxy.app.state.database.complete_job = AsyncMock(return_value=None)
        body = {"messages": [{"role": "user", "content": "hello"}]}
        with patch("routes.stream_llm", side_effect=_fake_stream):
            response = await app_client.post(
                "/v1/chat/completions",
                json=body,
                headers={"Authorization": "Bearer agent-key"},
            )
        assert response.status_code == 200
        proxy.app.state.database.complete_job.assert_awaited_once()
        call_kwargs = proxy.app.state.database.complete_job.call_args.kwargs
        assert call_kwargs.get("tool_calls_json") == ""


class TestHarness:
    """R10 — the test harness itself is sane."""

    async def test_R10_pyproject_config(self, app_client: httpx.AsyncClient) -> None:
        """pyproject.toml configures pytest-asyncio in auto mode."""
        repo_root = Path(__file__).parent.parent
        pyproject = (repo_root / "pyproject.toml").read_text()
        assert 'asyncio_mode = "auto"' in pyproject
        assert 'testpaths = ["tests"]' in pyproject

    async def test_R10_harness_imports_safely(self, app_client: httpx.AsyncClient) -> None:
        """The harness can be imported and exercised without real hardware."""
        response = await app_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"


class TestCommandRegex:
    """R14 — _PAUSE_RE and _RESUME_RE patterns handle prefixed and bare commands."""

    def test_pause_with_prefix_and_minutes(self) -> None:
        """[role]: /pause 5 captures the duration."""
        match = _PAUSE_RE.match("[user]: /pause 5")
        assert match is not None
        assert match.group(1) == "5"

    def test_pause_with_prefix_default_minutes(self) -> None:
        """[role]: /pause omits group 1, defaulting to 60 minutes."""
        match = _PAUSE_RE.match("[user]: /pause")
        assert match is not None
        assert match.group(1) is None

    def test_pause_does_not_match_resume(self) -> None:
        """_PAUSE_RE rejects the /resume command."""
        assert _PAUSE_RE.match("[user]: /resume") is None

    def test_pause_bare_command_backward_compat(self) -> None:
        """Bare /pause 5 still matches for backward compatibility."""
        match = _PAUSE_RE.match("/pause 5")
        assert match is not None
        assert match.group(1) == "5"

    def test_pause_strict_no_extra_tokens(self) -> None:
        """_PAUSE_RE rejects trailing tokens after the optional minutes."""
        assert _PAUSE_RE.match("[user]: /pause 5 extra") is None

    def test_pause_rejects_other_command(self) -> None:
        """_PAUSE_RE rejects unrelated commands."""
        assert _PAUSE_RE.match("[user]: /foo") is None

    def test_resume_with_prefix(self) -> None:
        """[role]: /resume matches the resume command."""
        assert _RESUME_RE.match("[user]: /resume") is not None

    def test_resume_partial_rejected(self) -> None:
        """A truncated /resum does not match."""
        assert _RESUME_RE.match("[user]: /resum") is None
