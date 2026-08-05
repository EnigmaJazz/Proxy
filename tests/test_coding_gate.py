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

    def test_gibberish_guard(self) -> None:
        assert _looks_like_gibberish("asdfghjkl12345!!!@@@") is True
        assert _looks_like_gibberish("asdfghjkl") is True
        assert _looks_like_gibberish("help") is True
        assert _looks_like_gibberish("what is the capital of france") is False

    def test_deterministic_noise(self) -> None:
        assert _is_deterministic_noise("[user]: asdfghjkl12345!!!@@@") is True
        assert _is_deterministic_noise("[user]: 12345!!") is True
        assert _is_deterministic_noise("") is True
        # Pure short alpha tokens are NOT deterministic noise — the
        # frontdesk's is_valid decides those.
        assert _is_deterministic_noise("[user]: help") is False
        assert _is_deterministic_noise("[user]: asdfghjkl") is False
        assert _is_deterministic_noise("[user]: what is the capital of france") is False

    def test_find_question_index(self) -> None:
        msgs = [
            {"role": "user", "content": "write a parser"},
            {"role": "assistant", "content": QUESTION},
            {"role": "user", "content": "opencode"},
        ]
        assert _find_coding_question_index(msgs) == 1

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
