"""Tests for the coding-task decision gate (routes._apply_coding_decision_gate).

Covers:
- fresh CODE requests prompt the user (opencode vs local code pathway)
- the decision turn routes to the opencode bridge or the local model
- the choice is cached per session (one prompt per session)
- requests from opencode itself (Lane B / IDE) skip the gate entirely
- non-CODE intents never prompt
"""
from __future__ import annotations

import json
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import proxy
from routes import (
    _CODING_QUESTION_PREFIX,
    _coding_decision_state,
    _find_coding_question_index,
    _is_deterministic_noise,
    _looks_like_gibberish,
    _parse_coding_answer,
    _resolve_session_id,
)
from tests.conftest import _NoOpCooling, _NoOpDatabase, _NoOpSystemd

QUESTION = f"{_CODING_QUESTION_PREFIX} Coding task detected — route to OpenCode or the local code pathway (Professional)? Reply `opencode` or `local`."


def _classification(intent: str = "CODE", *, tools_required: bool = False, is_valid: bool = True) -> dict[str, Any]:
    return {
        "is_valid": is_valid,
        "intent": intent,
        "priority": 2,
        "complexity": "low",
        "project_name": "general",
        "is_factual": False,
        "tools_required": tools_required,
    }


class _StreamCapture:
    """Stand-in for stream_llm that records the outbound payload."""

    def __init__(self) -> None:
        self.payload: Optional[dict[str, Any]] = None

    async def __call__(
        self,
        *,
        endpoint: str,
        payload: dict[str, Any],
        port: int = 0,
        headers: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        self.payload = payload
        yield {
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
        }


@pytest_asyncio.fixture
async def gate_client() -> Any:
    """httpx client against the real app with stubbed state + fresh caches."""
    proxy.app.state.database = _NoOpDatabase()
    proxy.app.state.systemd = _NoOpSystemd()
    proxy.app.state.cooler = _NoOpCooling()
    proxy.app.state.hardware = None
    proxy.app.state.active_heavy_model = None
    proxy.app.state.active_priority = 3
    proxy.app.state.requests_served = 0
    proxy.app.state.model_profiles = None
    proxy.app.state.coding_decisions = {}
    proxy.app.state.session_state = {}
    transport = httpx.ASGITransport(app=proxy.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

class TestParsers:
    def test_parse_answer_opencode(self) -> None:
        assert _parse_coding_answer("route to opencode") == "opencode"
        assert _parse_coding_answer("/opencode") == "opencode"

    def test_parse_answer_defaults_local(self) -> None:
        assert _parse_coding_answer("local") == "professional"
        assert _parse_coding_answer("use professional") == "professional"
        assert _parse_coding_answer("yes") == "professional"
        assert _parse_coding_answer("") == "professional"

    def test_parse_answer_cancel(self) -> None:
        assert _parse_coding_answer("cancel") == "cancel"
        assert _parse_coding_answer("stop") == "cancel"
        assert _parse_coding_answer("never mind") == "cancel"
        # A longer reply containing "stop" is NOT a cancellation.
        assert _parse_coding_answer("stop and route to local") == "professional"

    def test_gibberish_guard(self) -> None:
        assert _looks_like_gibberish("asdfghjkl12345!!!@@@") is True
        assert _looks_like_gibberish("asdfghjkl") is True
        assert _looks_like_gibberish("help") is True
        assert _looks_like_gibberish("what is the capital of france") is False

    def test_deterministic_noise(self) -> None:
        assert _is_deterministic_noise("[user]: asdfghjkl12345!!!@@@") is True
        assert _is_deterministic_noise("[user]: 12345!!") is True
        assert _is_deterministic_noise("") is True
        # Pure-alpha keyboard mash is now deterministic noise too.
        assert _is_deterministic_noise("[user]: asdfghjkl") is True
        assert _is_deterministic_noise("[user]: qwertyuiop") is True
        # Pure short alpha tokens are NOT deterministic noise — the
        # frontdesk's is_valid decides those.
        assert _is_deterministic_noise("[user]: help") is False
        assert _is_deterministic_noise("[user]: what is the capital of france") is False

    def test_find_question_index(self) -> None:
        msgs = [
            {"role": "user", "content": "write a parser"},
            {"role": "assistant", "content": QUESTION},
            {"role": "user", "content": "opencode"},
        ]
        assert _find_coding_question_index(msgs) == 1

    def test_question_not_pending_after_response(self) -> None:
        """Once the decision is made and the model responds, the question
        is no longer pending — a later request must NOT re-trigger the
        decision turn (which re-routed the old task and reprompted the
        model with the same task).
        """
        msgs = [
            {"role": "user", "content": "write a parser"},
            {"role": "assistant", "content": QUESTION},
            {"role": "user", "content": "local"},
            {"role": "assistant", "content": "Here is the parser code..."},
            {"role": "user", "content": "now add error handling"},
        ]
        assert _find_coding_question_index(msgs) is None

    def test_question_not_pending_with_old_answer(self) -> None:
        """After a cancel, re-sending the same request must prompt again,
        not silently default to the old task (the question is not the last
        assistant message before the final user turn).
        """
        msgs = [
            {"role": "user", "content": "write a parser"},
            {"role": "assistant", "content": QUESTION},
            {"role": "user", "content": "cancel"},
            {"role": "user", "content": "write a parser"},
        ]
        assert _find_coding_question_index(msgs) is None

    def test_no_question_returns_none(self) -> None:
        assert _find_coding_question_index(
            [{"role": "user", "content": "hello"}]
        ) is None


# ---------------------------------------------------------------------------
# End-to-end gate behaviour
# ---------------------------------------------------------------------------

class TestCodingDecisionGate:
    @pytest.mark.asyncio
    async def test_code_request_prompts_question(self, gate_client) -> None:
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "write a parser"}],
                    "stream": True,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            text = (await response.aread()).decode()

        assert response.status_code == 200
        assert "Coding decision" in text
        assert "opencode" in text
        # The model was NOT called — the gate took over.
        assert capture.payload is None
        # Regression: every SSE data line must parse as a JSON object (a
        # double-encoded line — "data: \"data: {...}\"" — broke nanobot
        # with "'str' object has no attribute 'choices'" and blanked
        # OpenWebUI).
        for line in text.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                payload = json.loads(line[6:])
                assert isinstance(payload, dict)

    @pytest.mark.asyncio
    async def test_decision_answer_opencode_routes_to_bridge(self, gate_client) -> None:
        with patch(
            "routes.classify_with_frontdesk",
            new=AsyncMock(return_value=_classification()),
        ), patch(
            "routes.opencode_chat",
            new=AsyncMock(return_value="BRIDGE_ANSWER"),
        ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [
                        {"role": "user", "content": "write a parser"},
                        {"role": "assistant", "content": QUESTION},
                        {"role": "user", "content": "opencode"},
                    ],
                    "stream": False,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            body = response.json()

        assert response.status_code == 200
        assert body["model"] == "opencode"
        assert "BRIDGE_ANSWER" in body["choices"][0]["message"]["content"]

    @pytest.mark.asyncio
    async def test_decision_answer_local_routes_professional(self, gate_client) -> None:
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [
                        {"role": "user", "content": "write a parser"},
                        {"role": "assistant", "content": QUESTION},
                        {"role": "user", "content": "local"},
                    ],
                    "stream": True,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200
        # The model received the ORIGINAL task, not the answer — the
        # question + answer were dropped from the model copy.
        assert capture.payload is not None
        assert capture.payload["messages"][-1]["content"] == "write a parser"

    @pytest.mark.asyncio
    async def test_decision_answer_cancel_aborts_without_routing(self, gate_client) -> None:
        """Answering "cancel" to the coding question aborts the task: no
        bridge call, no model call, and no cached decision (the next coding
        task prompts again).
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ), \
             patch(
                 "routes.opencode_chat",
                 new=AsyncMock(return_value="SHOULD_NOT_RUN"),
             ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [
                        {"role": "user", "content": "write a parser"},
                        {"role": "assistant", "content": QUESTION},
                        {"role": "user", "content": "cancel"},
                    ],
                    "stream": False,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            body = response.json()

        assert "Task cancelled" in body["choices"][0]["message"]["content"]
        assert capture.payload is None  # no model call
        # The decision was NOT cached — the next coding task prompts again.
        sid = _resolve_session_id(
            [{"role": "user", "content": "write a parser"}], proxy.app
        )
        assert sid not in _coding_decision_state(proxy.app)

    @pytest.mark.asyncio
    async def test_cached_decision_skips_prompt(self, gate_client) -> None:
        messages = [{"role": "user", "content": "write a parser"}]
        session_id = _resolve_session_id(messages, proxy.app)
        _coding_decision_state(proxy.app)[session_id] = "opencode"

        with patch(
            "routes.classify_with_frontdesk",
            new=AsyncMock(return_value=_classification()),
        ), patch(
            "routes.opencode_chat",
            new=AsyncMock(return_value="CACHED_ANSWER"),
        ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={"model": "auto", "messages": messages, "stream": False},
                headers={"Authorization": "Bearer agent-key"},
            )
            body = response.json()

        assert "CACHED_ANSWER" in body["choices"][0]["message"]["content"]
        assert "Coding decision" not in body["choices"][0]["message"]["content"]

    @pytest.mark.asyncio
    async def test_opencode_caller_skips_gate(self, gate_client) -> None:
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "write a parser"}],
                    "stream": True,
                },
                headers={
                    "Authorization": "Bearer agent-key",
                    "User-Agent": "opencode/1.18.13",
                },
            )
            text = (await response.aread()).decode()

        assert response.status_code == 200
        # Lane B (opencode caller) → straight to the local model, no prompt.
        assert capture.payload is not None
        assert "Coding decision" not in text

    @pytest.mark.asyncio
    async def test_chat_intent_never_prompts(self, gate_client) -> None:
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CHAT")),
             ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "hello there"}],
                    "stream": True,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            text = (await response.aread()).decode()

        assert capture.payload is not None
        assert "Coding decision" not in text


# ---------------------------------------------------------------------------
# Code-keyword heuristic (frontdesk says CHAT, keyword forces CODE)
# ---------------------------------------------------------------------------

class TestCodeKeywordHeuristic:
    @pytest.mark.asyncio
    async def test_python_script_request_prompts_as_code(self, gate_client) -> None:
        """A coding request the 2B frontdesk labels CHAT must still reach
        the coding-decision gate (the reported OpenWebUI bug).
        """
        with patch(
            "routes.classify_with_frontdesk",
            new=AsyncMock(return_value=_classification("CHAT")),
        ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{
                        "role": "user",
                        "content": "write a python script that downloads a file",
                    }],
                    "stream": False,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            body = response.json()

        assert response.status_code == 200
        # The heuristic forced CHAT → CODE, so the gate asked the question.
        assert "Coding decision" in body["choices"][0]["message"]["content"]

    @pytest.mark.asyncio
    async def test_plain_chat_query_not_forced(self, gate_client) -> None:
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CHAT")),
             ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{
                        "role": "user",
                        "content": "what is the capital of france",
                    }],
                    "stream": True,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            text = (await response.aread()).decode()

        assert capture.payload is not None
        assert "Coding decision" not in text

    @pytest.mark.asyncio
    async def test_tool_keywords_win_over_code(self, gate_client) -> None:
        """A request with both tool and code signals stays TOOL (the tool
        heuristic runs first and the code heuristic only upgrades CHAT).
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CHAT")),
             ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{
                        "role": "user",
                        "content": "search the web for python tutorials then write a script",
                    }],
                    "stream": True,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            text = (await response.aread()).decode()

        # TOOL intent: the model was called directly — no coding-decision
        # question, no gate prompt.
        assert capture.payload is not None
        assert "Coding decision" not in text


# ---------------------------------------------------------------------------
# Frontdesk is_valid interception (nonsense input)
# ---------------------------------------------------------------------------

class TestInvalidInputInterception:
    @pytest.mark.asyncio
    async def test_gibberish_returns_clarification(self, gate_client) -> None:
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification(is_valid=False)),
             ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "asdfghjkl12345!!!@@@"}],
                    "stream": False,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            body = response.json()

        assert response.status_code == 200
        assert "couldn't understand" in body["choices"][0]["message"]["content"]
        # The professional model was NOT called.
        assert capture.payload is None

    @pytest.mark.asyncio
    async def test_deterministic_noise_intercepts_even_when_frontdesk_says_valid(
        self, gate_client
    ) -> None:
        """The 2B's is_valid is inconsistent; the deterministic noise guard
        must intercept clear gibberish even when is_valid comes back True.
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CHAT", is_valid=True)),
             ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "asdfghjkl12345!!!@@@"}],
                    "stream": False,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            body = response.json()

        assert "couldn't understand" in body["choices"][0]["message"]["content"]
        assert capture.payload is None

    @pytest.mark.asyncio
    async def test_short_but_meaningful_not_intercepted(self, gate_client) -> None:
        """The gibberish guard protects short-but-meaningful input: a 2B
        false ``is_valid=False`` on "help" must NOT block the request.
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CHAT", is_valid=False)),
             ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "help me with this"}],
                    "stream": True,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        # Not intercepted — the model was called.
        assert capture.payload is not None

    @pytest.mark.asyncio
    async def test_mid_tool_flow_result_never_intercepted(self, gate_client) -> None:
        """A tool-result continuation (short numeric/JSON result) must NOT
        be intercepted as nonsense — the context dump legitimately looks
        like noise but the professional is mid tool-chain.
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("TOOL")),
             ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [
                        {"role": "user", "content": "what's 6 times 7"},
                        {"role": "assistant", "content": "", "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {
                                "name": "calculate",
                                "arguments": '{"expr": "6*7"}',
                            }},
                        ]},
                        {"role": "tool", "tool_call_id": "call_1", "content": "42"},
                    ],
                    "stream": True,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            text = (await response.aread()).decode()

        # The tool-result continuation went to the model — no nonsense
        # interception mid-tool-chain.
        assert capture.payload is not None
        assert "couldn't understand" not in text


# ---------------------------------------------------------------------------
# Factual keyword heuristic (semantic cache feeding)
# ---------------------------------------------------------------------------

class TestFactualKeywordHeuristic:
    @pytest.mark.asyncio
    async def test_factual_phrasing_routes_normally_with_cache_path(
        self, gate_client
    ) -> None:
        """A factual-phrased question the 2B marked non-factual must be
        forced factual and still reach the model (cache miss → normal flow),
        without crashing on the NoOp database's cache surface.
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CHAT")),
             ):
            response = await gate_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{
                        "role": "user",
                        "content": "what is the capital of france",
                    }],
                    "stream": True,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert capture.payload is not None
        # The factual net forced is_factual → the semantic cache lookup ran
        # (NoOp returns a miss) and the store fires on completion without
        # crashing (NoOp cache_store no-ops).  The request reached the model.
        assert capture.payload is not None


# ---------------------------------------------------------------------------
# Client-disconnect cancellation (_event_stream CancelledError handler)
# ---------------------------------------------------------------------------

class _CancelDb:
    """Fake database recording completion/cancellation calls."""

    def __init__(self) -> None:
        self.cancelled: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str]] = []

    async def complete_job(self, job_id: str, finish_reason: str = "stop", full_content: str = "") -> None:
        self.completed.append((job_id, finish_reason))

    async def cancel_job(self, job_id: str, reason: str = "cancelled") -> None:
        self.cancelled.append((job_id, reason))

    async def purge_stream_chunks(self, job_id: str) -> None:
        pass

    async def update_partial_content(self, job_id: str, content: str) -> None:
        pass

    async def record_stream_chunk(self, *args: Any) -> None:
        pass


class _CancelCooler:
    def __init__(self) -> None:
        self.baseline = 0

    async def prefill_burst(self, path: str) -> None:
        pass

    def generation_hold(self, path: str) -> None:
        pass

    def baseline_idle(self) -> None:
        self.baseline += 1


class _CancelSystemd:
    async def get_port(self, domain: str) -> int:
        return 13109


class TestClientDisconnectCancellation:
    @pytest.mark.asyncio
    async def test_cancelling_the_stream_marks_the_job_cancelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the client aborts (Stop button / disconnect), the generator
        receives CancelledError: the job is marked cancelled (never left
        'active' for the queue worker), cooling resets, and the cancellation
        re-raises so the stream actually stops.
        """
        import asyncio
        import types

        from routes import _event_stream
        from routing import RouteDecision

        async def _endless_stream(**kwargs: Any) -> Any:
            i = 0
            while True:
                yield {"choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": f"chunk{i}"},
                    "finish_reason": None,
                }]}
                i += 1
                await asyncio.sleep(0.01)

        monkeypatch.setattr("routes.stream_llm", _endless_stream)
        db = _CancelDb()
        cooler = _CancelCooler()
        state = types.SimpleNamespace(
            database=db, systemd=_CancelSystemd(), cooler=cooler,
        )
        route = RouteDecision(model_key="professional", port=13109, intent="CHAT")
        gen = _event_stream(
            state=state, app=state, route=route, payload={"messages": []},
            fwd_headers={}, job_id="job-1", project_id="general",
            processed_messages=[{"role": "user", "content": "x"}],
            requested_model="auto", hardware_path="gpu",
        )

        async def consume() -> None:
            async for _ in gen:
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert db.cancelled == [("job-1", "client_cancelled")]
        assert db.completed == []  # not marked completed, not failed
        assert cooler.baseline == 1  # cooling reset in finally

    @pytest.mark.asyncio
    async def test_closing_the_stream_early_marks_the_job_cancelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Some frameworks deliver a disconnect as GeneratorExit (the
        generator is closed) rather than task cancellation.  The finally
        fallback must catch that and mark the job cancelled.
        """
        import asyncio
        import types

        from routes import _event_stream
        from routing import RouteDecision

        async def _endless_stream(**kwargs: Any) -> Any:
            while True:
                yield {"choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": "x"},
                    "finish_reason": None,
                }]}
                await asyncio.sleep(0.01)

        monkeypatch.setattr("routes.stream_llm", _endless_stream)
        db = _CancelDb()
        cooler = _CancelCooler()
        state = types.SimpleNamespace(
            database=db, systemd=_CancelSystemd(), cooler=cooler,
        )
        route = RouteDecision(model_key="professional", port=13109, intent="CHAT")
        gen = _event_stream(
            state=state, app=state, route=route, payload={"messages": []},
            fwd_headers={}, job_id="job-2", project_id="general",
            processed_messages=[{"role": "user", "content": "x"}],
            requested_model="auto", hardware_path="gpu",
        )

        # Consume the triage chunk + one model chunk, then close the
        # generator (GeneratorExit) mid-stream inside the try block.
        await gen.__anext__()  # triage
        await gen.__anext__()  # first model chunk
        await gen.aclose()

        assert db.cancelled == [("job-2", "client_cancelled")]
        assert db.completed == []
        assert cooler.baseline == 1
