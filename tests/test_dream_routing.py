"""Dream/soul fast-path routing tests.

The Nanobot dream pathway must route to professional (35B MoE) — the
same model as CHAT/TOOL — not architect.  This pins the model key,
profile resolution, and SSE params_replaced event after the switch.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import proxy
from profile_loader import ModelProfileTable
from tests.conftest import _NoOpDatabase


def _make_profile_table() -> ModelProfileTable:
    """Professional rows in both buckets so R17 resolves for each path.

    The code bucket (ARCHITECT) and chat bucket (CHAT) carry distinct
    temperatures so tests can tell which branch resolved.
    """
    return ModelProfileTable([
        {
            "model": "professional",
            "intent": "code",
            "temperature": 0.2,
            "top_p": 1.0,
            "max_tokens": 4096,
            "thinking_budget_tokens": 4096,
        },
        {
            "model": "professional",
            "intent": "chat",
            "temperature": 0.7,
            "top_p": 1.0,
            "max_tokens": 4096,
            "thinking_budget_tokens": 0,
        },
    ])


class _StreamCapture:
    """Async-generator stand-in for ``stream_llm`` that yields minimal chunks."""

    def __init__(self) -> None:
        self.endpoint: str | None = None
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
        self.endpoint = endpoint
        self.payload = payload
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                },
            ],
        }


@pytest_asyncio.fixture
async def dream_client() -> Any:
    """Yield an httpx async client against the real app with state stubbed."""
    from tests.conftest import _NoOpCooling, _NoOpDatabase, _NoOpSystemd

    proxy.app.state.database = _NoOpDatabase()
    proxy.app.state.systemd = _NoOpSystemd()
    proxy.app.state.cooler = _NoOpCooling()
    proxy.app.state.hardware = None
    proxy.app.state.active_heavy_model = None
    proxy.app.state.active_priority = 3
    proxy.app.state.requests_served = 0
    proxy.app.state.model_profiles = _make_profile_table()

    transport = httpx.ASGITransport(app=proxy.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _parse_sse_events(response_text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE response body into (event, data) tuples."""
    events: list[tuple[str, dict[str, Any]]] = []
    current_event = ""
    for line in response_text.splitlines():
        if line.startswith("event: "):
            current_event = line[len("event: "):]
        elif line.startswith("data: "):
            raw = line[len("data: "):]
            try:
                events.append((current_event, json.loads(raw)))
            except ValueError:
                pass  # terminal chunk like [DONE]
            current_event = ""
    return events


def _params_replaced(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    data = [d for event, d in events if event == "kinver.proxy.params_replaced"]
    assert data, "expected a params_replaced event"
    return data[0]["data"]


@pytest.mark.asyncio
async def test_dream_routes_to_professional_code_profile(dream_client: Any) -> None:
    """A dream request takes the dream fast-path and applies the code profile."""
    capture = _StreamCapture()
    with patch("routes.stream_llm", new=capture), \
         patch("routes.is_dream_process", new=AsyncMock(return_value=True)):
        resp = await dream_client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [
                    {"role": "user", "content": "extract new facts from conversation history"},
                ],
                "stream": True,
            },
        )

    assert resp.status_code == 200
    params = _params_replaced(_parse_sse_events(resp.text))
    assert params["model"] == "professional"
    assert params["values"]["temperature"] == 0.2

    assert capture.payload is not None
    assert capture.payload["temperature"] == 0.2


@pytest.mark.asyncio
async def test_non_dream_chat_uses_chat_profile(dream_client: Any) -> None:
    """A normal request goes through the frontdesk path, not the dream one."""
    capture = _StreamCapture()
    classification = {
        "is_valid": True,
        "intent": "CHAT",
        "priority": 2,
        "complexity": "low",
        "project_name": "general",
        "is_factual": False,
        "tools_required": False,
    }
    with patch("routes.stream_llm", new=capture), \
         patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)), \
         patch("routes.is_dream_process", new=AsyncMock(return_value=False)):
        resp = await dream_client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

    assert resp.status_code == 200
    params = _params_replaced(_parse_sse_events(resp.text))
    assert params["model"] == "professional"
    assert params["values"]["temperature"] == 0.7


class _RecordingDatabase(_NoOpDatabase):  # type: ignore[misc]
    """Capture the last enqueue_job kwargs for assertions."""

    def __init__(self) -> None:
        self.last_enqueue: Optional[dict[str, Any]] = None

    async def enqueue_job(self, **kwargs: Any) -> str:
        self.last_enqueue = kwargs
        return "job-id"


@pytest.mark.asyncio
async def test_dream_forwards_tools_to_model(dream_client: Any) -> None:
    """The dream path must forward client tools to the model and record them.

    Regression: the dream payload used to omit ``tools`` entirely, so
    professional received the memory-consolidation prompt with no tool
    definitions, emitted bare ``[read_file]`` text stubs, and stopped
    without executing anything.
    """
    capture = _StreamCapture()
    db = _RecordingDatabase()
    proxy.app.state.database = db
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with patch("routes.stream_llm", new=capture), \
         patch("routes.is_dream_process", new=AsyncMock(return_value=True)):
        resp = await dream_client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [
                    {"role": "user", "content": "extract new facts from conversation history"},
                ],
                "stream": True,
                "tools": tools,
            },
        )

    assert resp.status_code == 200
    assert capture.payload is not None
    assert capture.payload.get("tools") == tools
    assert db.last_enqueue is not None
    assert json.loads(db.last_enqueue["tools_json"]) == tools


@pytest.mark.asyncio
async def test_native_tool_calls_not_double_emitted(dream_client: Any) -> None:
    """Native structured tool_calls must be relayed once, not re-emitted at finish.

    Regression: the proxy relayed llama.cpp's structured tool_call chunks
    verbatim AND re-emitted the accumulated copy at finish_reason.  Any
    OpenAI client accumulates arguments per tool-call index, so it
    received each call doubled ('{"query":...}{"query":...}'), which
    broke JSON parsing in nanobot-ai ("parameters must be a JSON object,
    got str").  The failed tool execution made the model retry the same
    call — the tool-loop flood seen in production.
    """

    class _NativeToolStream:
        """Fake stream_llm yielding llama.cpp-style structured tool_calls."""

        async def __call__(
            self,
            *,
            endpoint: str,
            payload: dict[str, Any],
            port: int = 0,
            headers: Optional[dict[str, Any]] = None,
            **kwargs: Any,
        ) -> Any:
            yield {
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": "{\"query\":",
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }
            yield {
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "function": {"arguments": "\"weather\"}"},
                        }],
                    },
                    "finish_reason": None,
                }],
            }
            yield {
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls",
                }],
            }

    stream = _NativeToolStream()
    with patch("routes.stream_llm", new=stream), \
         patch("routes.is_dream_process", new=AsyncMock(return_value=True)):
        resp = await dream_client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "extract facts"}],
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
        )

    assert resp.status_code == 200
    fragments: list[str] = []
    for _event, data in _parse_sse_events(resp.text):
        for ch in data.get("choices", []):
            for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                args = (tc.get("function") or {}).get("arguments") or ""
                if args:
                    fragments.append(args)
    # Two fragments build ONE JSON object.  A finish-time re-emission
    # would append a third fragment and double the arguments.
    joined = "".join(fragments)
    assert joined == '{"query":"weather"}', (
        f"tool_call arguments doubled or corrupted: {joined!r}"
    )

