"""R1 Glass Pipe — SSE format contract for proxy-injected messages.

The proxy's behavior is a balance between two concerns:

1. **User feedback during long delays** (cold starts, hotswaps): the user
   wants to see what's happening.  Triage and loading messages are
   emitted as visible ``delta.content`` (sentinel-prefixed) so clients
   render them inline.

2. **Glass Pipe compliance** (AGENTS.md Rule 1, memory #3): the proxy
   never mutates in-flight ``tool_calls`` JSON or terminates with a
   non-standard ``finish_reason``.

Proxy-injected status (triage, loading, tool status, cache) is emitted
as ``delta.content`` prefixed with the zero-width sentinel
``\u200b`` (``STATUS_SENTINEL``) and marked ``model: "proxy-system"``.
The prefix is what makes the chunk identifiable: ``strip_proxy_status``
drops it from the OUTBOUND model-copy on the next request, so the user
sees the feedback inline but the model never sees its own status echoed
back (the triage-echo degeneration that produced the routing loop).
Emission as plain content (no sentinel) was tried and polluted every
assistant turn with "🔍 Proxy triage" text the model echoed at length.
Loop warnings are NOT sentinel-prefixed — they are model-directed
corrective signals and must reach the next turn.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import httpx

import proxy
from profile_loader import ModelProfileTable
from routing import RouteDecision


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_profile_table() -> ModelProfileTable:
    return ModelProfileTable([
        {
            "model": "professional",
            "intent": "chat",
            "temperature": 0.7,
            "top_p": 1.0,
            "max_tokens": 235929,
            "thinking_budget_tokens": 0,
        },
    ])


def _classification(
    intent: str = "CHAT",
    *,
    tools_required: bool = False,
) -> dict[str, Any]:
    return {
        "is_valid": True,
        "intent": intent,
        "priority": 2,
        "complexity": "low",
        "project_name": "general",
        "is_factual": False,
        "tools_required": tools_required,
    }


class _StreamCapture:
    """Async-generator stand-in for ``stream_llm`` that yields minimal chunks."""

    def __init__(self) -> None:
        self.endpoint: Optional[str] = None
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


class _ToolCallCapture:
    """Stream stand-in emitting one native structured tool call + finish."""

    def __init__(self) -> None:
        self.endpoint: Optional[str] = None
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
            "id": "cmpl-tool1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "professional",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_native_1",
                                "type": "function",
                                "function": {
                                    "name": "exec",
                                    "arguments": '{"command": "ls"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": None,
                },
            ],
        }
        yield {
            "id": "cmpl-tool1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "professional",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls",
                },
            ],
        }


@pytest_asyncio.fixture
async def r1_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Yield an httpx async client against the real app with state stubbed."""
    from tests.conftest import (
        _NoOpCooling,
        _NoOpDatabase,
        _NoOpSystemd,
    )

    # The coding-decision gate is orthogonal to this suite (SSE format +
    # thinking defaults); pass through without prompting.
    monkeypatch.setattr(
        "routes._apply_coding_decision_gate",
        AsyncMock(return_value=None),
    )

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
    """Parse an SSE response body into (event, data) tuples.

    Returns an empty list for non-event lines.  Lines like
    ``event: foo\\n`` followed by ``data: {...}`` become one entry.
    Lines with no event name are returned with an empty-string event
    name (this is the OpenAI default for ``data:`` only).
    """
    events: list[tuple[str, dict[str, Any]]] = []
    current_event: str | None = None
    current_data: list[str] = []
    for raw in response_text.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("event: "):
            if current_event is not None or current_data:
                try:
                    events.append((current_event or "", json.loads("".join(current_data))))
                except json.JSONDecodeError:
                    pass
            current_event = line[len("event: "):].strip()
            current_data = []
        elif line.startswith("data: "):
            current_data.append(line[len("data: "):])
        elif line == "":
            if current_event is not None or current_data:
                try:
                    events.append((current_event or "", json.loads("".join(current_data))))
                except json.JSONDecodeError:
                    pass
                current_event = None
                current_data = []
    if current_event is not None or current_data:
        try:
            events.append((current_event or "", json.loads("".join(current_data))))
        except json.JSONDecodeError:
            pass
    return events


# ---------------------------------------------------------------------------
# Contract: triage/loading as content (user feedback)
# ---------------------------------------------------------------------------

class TestProxyMessagesAsContent:
    """Proxy-injected status arrives as sentinel-prefixed ``delta.content``
    (visible inline to the user), with ``model: proxy-system`` so the
    stream=false collector skips it.  The sentinel lets the OUTBOUND
    filter strip it from the model-copy — the model never sees its own
    status echoed back.
    """

    SENTINEL = "\u200b"

    @pytest.mark.asyncio
    async def test_triage_emitted_as_sentinel_content(self, r1_client) -> None:
        """The routing announcement must arrive as sentinel-prefixed
        ``delta.content`` (so Telegram renders it inline), marked
        ``model: "proxy-system"``, and must never appear in plain
        (non-sentinel) content.

        Triage text was previously emitted as plain content so Telegram
        rendered it inline, but every assistant turn in a long
        conversation then started with "🔍 Proxy triage: ..." and the
        model began echoing that pattern at length — the "routing loop
        with no output" symptom.  The sentinel prefix preserves inline
        visibility while letting ``strip_proxy_status`` keep the text
        out of the model's input.
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            text = (await response.aread()).decode()

        assert response.status_code == 200, text
        events = _parse_sse_events(text)

        # The triage must appear once as sentinel-prefixed content marked
        # proxy-system, and never in plain (non-sentinel) content.
        triage_found = False
        for _event_name, event_data in events:
            for ch in event_data.get("choices", []):
                content = ch.get("delta", {}).get("content", "")
                if "🔍 Proxy triage" not in content:
                    continue
                assert content.startswith(self.SENTINEL), (
                    f"triage content must carry the status sentinel; "
                    f"events: {events!r}"
                )
                assert event_data.get("model") == "proxy-system", (
                    f"triage chunk should be marked proxy-system, "
                    f"got {event_data.get('model')!r}"
                )
                triage_found = True
        assert triage_found, (
            f"expected triage as sentinel-prefixed delta.content; "
            f"events: {events!r}"
        )

        for _event_name, event_data in events:
            for ch in event_data.get("choices", []):
                content = ch.get("delta", {}).get("content", "")
                if "Proxy triage" in content:
                    assert content.startswith(self.SENTINEL), (
                        f"triage must not leak into plain delta.content; "
                        f"events: {events!r}"
                    )

    @pytest.mark.asyncio
    async def test_triage_skipped_on_mid_tool_flow(self, r1_client) -> None:
        """Mid-tool-flow requests (last message is a tool result) must NOT
        re-emit the triage chunk.

        Every tool iteration in an agentic chain previously re-announced
        the route, polluting the visible conversation with repeated
        "Proxy triage" lines AND feeding the model's own input text that
        it started to echo back at length.  The triage is only useful
        when a NEW user turn begins.
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [
                        {"role": "assistant", "content": "",
                         "tool_calls": [
                             {"id": "call_1", "type": "function",
                              "function": {"name": "exec", "arguments": "{\"command\": \"ls\"}"}},
                         ]},
                        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
                    ],
                    "stream": True,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            text = (await response.aread()).decode()

        assert response.status_code == 200, text
        events = _parse_sse_events(text)

        for _event_name, event_data in events:
            for ch in event_data.get("choices", []):
                content = ch.get("delta", {}).get("content", "")
                assert "Proxy triage" not in content, (
                    f"mid-tool-flow request must not re-emit triage; "
                    f"events: {events!r}"
                )

    @pytest.mark.asyncio
    async def test_tool_status_emitted_as_sentinel_content(
        self,
        r1_client: Any,
    ) -> None:
        """Per-tool-call status ("🔧 exec: ...") must arrive as
        sentinel-prefixed ``delta.content`` marked proxy-system, never
        in plain content.

        Tool-status lines are emitted as content for inline Telegram
        visibility; the sentinel prefix keeps them out of the model's
        input via ``strip_proxy_status`` so the assistant history the
        model later sees stays clean.
        """
        capture = _ToolCallCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification(intent="TOOL", tools_required=True)),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "list the files"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "exec",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"command": {"type": "string"}},
                                },
                            },
                        },
                    ],
                    "stream": True,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            text = (await response.aread()).decode()

        assert response.status_code == 200, text
        events = _parse_sse_events(text)

        status_found = False
        for _event_name, event_data in events:
            for ch in event_data.get("choices", []):
                content = ch.get("delta", {}).get("content", "")
                if "🔧 exec: `ls`" not in content:
                    continue
                assert content.startswith(self.SENTINEL), (
                    f"tool status must carry the status sentinel; "
                    f"events: {events!r}"
                )
                assert event_data.get("model") == "proxy-system", (
                    f"tool status must be marked proxy-system; "
                    f"events: {events!r}"
                )
                status_found = True
        assert status_found, (
            f"expected tool status as sentinel-prefixed delta.content; "
            f"events: {events!r}"
        )

        for _event_name, event_data in events:
            for ch in event_data.get("choices", []):
                content = ch.get("delta", {}).get("content", "")
                if "🔧" in content:
                    assert content.startswith(self.SENTINEL), (
                        f"tool status must not leak into plain delta.content; "
                        f"events: {events!r}"
                    )


# ---------------------------------------------------------------------------
# Tool-call defaults: disable thinking when tools are present
# ---------------------------------------------------------------------------

class TestToolCallThinkingDefault:
    """Architecture: the proxy is the **authoritative** source for
    ``enable_thinking`` on every request.  The service's
    ``--chat-template-kwargs '{"enable_thinking": false}'`` is a
    fallback for clients that bypass the proxy, but it has been
    observed to NOT be respected across multi-turn conversations on
    Qwen 3.5 (the chat template honors the override for the first
    turn only).  Per-request injection is the only reliable mechanism.

    Decision matrix (the proxy is explicit on every request):
      - X-Proxy-Thinking: true           → enable_thinking: True
      - X-Proxy-Thinking: false          → enable_thinking: False
      - no header, tools in request      → enable_thinking: False
      - no header, no tools, complex intent
        (CODE/SCHOLAR/CREATIVE/ARCHITECT)
        AND not tools_required            → enable_thinking: True
      - no header, no tools, simple intent → enable_thinking: False
      - no header, no tools, complex intent
        AND tools_required                → enable_thinking: False

    The header overrides the default in either direction.  Per-request
    injection is the only reliable mechanism — see the live-bug
    context in the test file's module docstring.
    """

    @pytest.mark.asyncio
    async def test_tools_request_explicitly_disables_thinking(
        self, r1_client,
    ) -> None:
        """Tools present, no header: the proxy MUST inject
        ``chat_template_kwargs: {enable_thinking: False}``.  This is
        defense in depth — the service's ``--chat-template-kwargs`` is
        not honored across multi-turn on Qwen 3.5 (only for the first
        turn), so the proxy sets ``enable_thinking`` on every request
        to prevent the model from re-enabling thinking mid-conversation
        and emitting 60-100 reasoning chunks before tool calls.
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "professional",
                    "messages": [{"role": "user", "content": "what is the cpu temperature"}],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "exec_shell",
                            "description": "Run a shell command",
                            "parameters": {"type": "object"},
                        },
                    }],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.payload is not None
        assert capture.payload.get("chat_template_kwargs") == {
            "enable_thinking": False,
            "preserve_thinking": False,
        }, (
            f"tools-request must inject enable_thinking: False + "
            f"preserve_thinking: False; got {capture.payload!r}"
        )

    @pytest.mark.asyncio
    async def test_tools_request_with_opt_in_header_enables_thinking(
        self, r1_client,
    ) -> None:
        """Tools present + ``X-Proxy-Thinking: true``: the proxy MUST
        inject ``chat_template_kwargs: {enable_thinking: True}`` to
        override the service default.
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "professional",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "exec_shell",
                            "description": "Run a shell command",
                            "parameters": {"type": "object"},
                        },
                    }],
                },
                headers={
                    "Authorization": "Bearer agent-key",
                    "X-Proxy-Thinking": "true",
                },
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.payload is not None
        assert capture.payload.get("chat_template_kwargs") == {
            "enable_thinking": True,
            "preserve_thinking": True,
        }, (
            f"opt-in header must force thinking on even for tool requests; "
            f"got {capture.payload!r}"
        )

    @pytest.mark.asyncio
    async def test_tools_request_with_opt_out_header_disables_thinking(
        self, r1_client,
    ) -> None:
        """Tools present + ``X-Proxy-Thinking: false``: explicit
        ``enable_thinking: False`` (same effect as service default
        but stated explicitly for documentation).
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "professional",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "exec_shell",
                            "description": "Run a shell command",
                            "parameters": {"type": "object"},
                        },
                    }],
                },
                headers={
                    "Authorization": "Bearer agent-key",
                    "X-Proxy-Thinking": "false",
                },
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.payload is not None
        assert capture.payload.get("chat_template_kwargs") == {
            "enable_thinking": False,
            "preserve_thinking": False,
        }

    @pytest.mark.asyncio
    async def test_non_tools_request_enables_thinking_by_default(
        self, r1_client,
    ) -> None:
        """No tools, no header, classifier says complex intent + no tools
        needed: the proxy opts into thinking because reasoning helps for
        non-tool tasks (multi-step chat, planning, code, etc.).  This
        is the proxy's intelligence layer.
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification(
                     "CODE", tools_required=False,
                 )),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "professional",
                    "messages": [{"role": "user", "content": "explain the proxy architecture"}],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.payload is not None
        assert capture.payload.get("chat_template_kwargs") == {
            "enable_thinking": True,
            "preserve_thinking": True,
        }, (
            f"non-tool request with complex intent must opt into thinking; "
            f"got {capture.payload!r}"
        )

    @pytest.mark.asyncio
    async def test_non_tools_request_with_tools_required_keeps_thinking_off(
        self, r1_client,
    ) -> None:
        """No tools in request, but classifier says tools ARE required
        (e.g. the user asked for code, scholar, or architect work and
        is likely to add tools in a follow-up).  The proxy keeps
        thinking OFF to avoid the tool-call/parser conflicts that
        originally surfaced this whole issue.
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification(
                     "CODE", tools_required=True,
                 )),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "professional",
                    "messages": [{"role": "user", "content": "refactor this code"}],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.payload is not None
        assert capture.payload.get("chat_template_kwargs") == {
            "enable_thinking": False,
            "preserve_thinking": False,
        }, (
            f"classifier set tools_required=True — proxy must inject "
            f"enable_thinking: False; got {capture.payload!r}"
        )

    @pytest.mark.asyncio
    async def test_non_tools_chat_request_keeps_thinking_off(
        self, r1_client,
    ) -> None:
        """No tools, CHAT intent, no header: simple chat skips thinking
        (the model is faster and cheaper without the preamble).
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CHAT")),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "professional",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.payload is not None
        assert capture.payload.get("chat_template_kwargs") == {
            "enable_thinking": False,
            "preserve_thinking": False,
        }

    @pytest.mark.asyncio
    async def test_non_tools_request_with_opt_out_header_disables_thinking(
        self, r1_client,
    ) -> None:
        """No tools + ``X-Proxy-Thinking: false``: client can opt out
        of thinking even for non-tool tasks.
        """
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "professional",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={
                    "Authorization": "Bearer agent-key",
                    "X-Proxy-Thinking": "false",
                },
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.payload is not None
        assert capture.payload.get("chat_template_kwargs") == {
            "enable_thinking": False,
            "preserve_thinking": False,
        }

    @pytest.mark.asyncio
    async def test_thinking_default_persists_across_multi_turn(
        self, r1_client,
    ) -> None:
        """The proxy must apply the same thinking default on every
        turn, not just the first.  Whether the model respects it is
        a model-level concern.
        """
        # Turn 1: non-tool CODE request, classifier says tools not
        # required → opt-in
        capture_1 = _StreamCapture()
        with patch("routes.stream_llm", new=capture_1), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification(
                     "CODE", tools_required=False,
                 )),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "professional",
                    "messages": [
                        {"role": "user", "content": "first question"},
                    ],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()
        assert response.status_code == 200
        assert capture_1.payload is not None
        assert capture_1.payload.get("chat_template_kwargs") == {
            "enable_thinking": True,
            "preserve_thinking": True,
        }

        # Turn 2: tool request in same conversation
        capture_2 = _StreamCapture()
        with patch("routes.stream_llm", new=capture_2), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification(
                     "TOOL", tools_required=True,
                 )),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "professional",
                    "messages": [
                        {"role": "user", "content": "first question"},
                        {"role": "assistant", "content": "first answer"},
                        {"role": "user", "content": "now run a command"},
                    ],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "exec_shell",
                            "description": "Run a shell command",
                            "parameters": {"type": "object"},
                        },
                    }],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()
        assert response.status_code == 200
        assert capture_2.payload is not None
        # Turn 2 with tools: proxy MUST inject enable_thinking: False
        # explicitly (defense in depth — the service's default isn't
        # honored across multi-turn on Qwen 3.5).
        assert capture_2.payload.get("chat_template_kwargs") == {
            "enable_thinking": False,
            "preserve_thinking": False,
        }, (
            f"tool request on turn 2 must inject enable_thinking: False; "
            f"got {capture_2.payload!r}"
        )
