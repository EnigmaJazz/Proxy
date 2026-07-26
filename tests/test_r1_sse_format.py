"""R1 Glass Pipe — SSE format contract for proxy-injected messages.

The proxy's behavior is a balance between two concerns:

1. **User feedback during long delays** (cold starts, hotswaps): the user
   wants to see what's happening.  Triage and loading messages are
   emitted as ``delta.content`` chunks so nanobot-ai and similar clients
   render them inline in the chat.

2. **Glass Pipe compliance** (AGENTS.md Rule 1, memory #3): the proxy
   never mutates in-flight ``tool_calls`` JSON or terminates with a
   non-standard ``finish_reason``.  The Graceful Guillotine (audit
   halt) emits a custom SSE event + a standards-compliant finish.

The original concern that ``delta.content`` proxy messages would corrupt
tool-calling was a red herring — the actual cause of the tool-call
XML-in-chat bug was Qwen 3.5's extended-thinking mode (60-100 chunks
of ``reasoning_content`` adjacent to the tool call).  The thinking
disable is the actual fix; the triage/loading messages are now back
as content for user feedback.
"""
from __future__ import annotations

import json
from typing import Any
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
        self.endpoint: str | None = None
        self.payload: dict[str, Any] | None = None

    async def __call__(self, *, endpoint, payload, port=0, headers=None, **kwargs):
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
async def r1_client() -> Any:
    """Yield an httpx async client against the real app with state stubbed."""
    from tests.conftest import (
        _NoOpAuditor,
        _NoOpCooling,
        _NoOpDatabase,
        _NoOpSystemd,
    )

    proxy.app.state.database = _NoOpDatabase()
    proxy.app.state.systemd = _NoOpSystemd()
    proxy.app.state.cooler = _NoOpCooling()
    proxy.app.state.hardware = None
    proxy.app.state.auditor = _NoOpAuditor()
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
    """Triage and loading messages are emitted as ``delta.content`` so the
    user sees feedback during the long model-loading delay.  The Glass
    Pipe rule about not injecting content applies to in-flight payloads
    (audit halts, tool_calls mutation); user-facing status messages are
    the proxy's legitimate response to the user.
    """

    @pytest.mark.asyncio
    async def test_triage_emitted_as_content_delta(self, r1_client) -> None:
        """The first SSE chunk after the model starts should be a
        ``data: {delta.content: "🔍 Proxy triage: ..."}`` chunk that
        nanobot-ai renders inline.  This gives the user feedback
        during the long routing/hotswap delay.
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
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            text = (await response.aread()).decode()

        assert response.status_code == 200, text
        events = _parse_sse_events(text)

        # The triage message must appear as a data: chunk (delta.content),
        # not as a custom event.  The triage is the first non-empty data:
        # chunk and uses ``model: "proxy-system"`` to mark its origin.
        triage_found = False
        for event_name, event_data in events:
            if event_name:  # skip custom events
                continue
            choices = event_data.get("choices", [])
            for ch in choices:
                delta = ch.get("delta", {})
                content = delta.get("content", "")
                if "🔍 Proxy triage" in content:
                    triage_found = True
                    assert event_data.get("model") == "proxy-system", (
                        f"triage should be marked as proxy-system, "
                        f"got {event_data.get('model')!r}"
                    )
                    break
            if triage_found:
                break
        assert triage_found, (
            f"expected triage to be emitted as a data: chunk with "
            f"delta.content; events: {events!r}"
        )

    @pytest.mark.asyncio
    async def test_no_audit_override_finish_reason(self) -> None:
        """The Graceful Guillotine must terminate with ``finish_reason: "stop"``,
        not the non-standard ``"audit_override"``.  OpenAI clients treat
        unknown finish reasons as errors.

        This is the R1 violation that is still in scope after the
        triage/loading revert.
        """
        from routes import _graceful_guillotine_chunk

        chunk_text = await _graceful_guillotine_chunk("test-job-id", "test reason")
        events = _parse_sse_events(chunk_text)

        # The first event is the audit_halt custom event
        assert events[0][0] == "kinver.proxy.audit_halt", (
            f"expected kinver.proxy.audit_halt event, got {events[0]!r}"
        )
        assert events[0][1].get("reason") == "test reason"

        # The final model chunk must have finish_reason: "stop"
        model_chunks = [e for e in events if not e[0]]
        assert model_chunks, "expected a final model chunk"
        final_chunk = model_chunks[-1]
        choices = final_chunk[1].get("choices", [])
        assert choices, "expected choices in final chunk"
        assert choices[0].get("finish_reason") == "stop", (
            f"Graceful Guillotine must terminate with finish_reason='stop', "
            f"got {choices[0].get('finish_reason')!r}"
        )

        # CRITICAL: no delta.content injection in the final chunk.
        # The R1 violation was injecting synthetic content; the fix is
        # to terminate cleanly with no content delta.
        for ch in choices:
            delta = ch.get("delta", {})
            assert not delta.get("content"), (
                f"Graceful Guillotine must not inject delta.content; "
                f"got {delta!r}"
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
