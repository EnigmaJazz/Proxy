"""R1 Glass Pipe — SSE format contract for proxy-injected events.

The proxy never mutates in-flight tool_calls JSON or terminates with a
non-standard finish_reason.  Triage, loading, and audit-halt messages
are emitted as **custom SSE events** (``kinver.proxy.<name>``) so
OpenAI-compatible clients do not render them as model content deltas.

This is the regression test for the bug pattern where nanobot-ai was
showing the triage and audit-override messages inline in the chat,
corrupting the model's tool-calling flow (it would re-call the same
tool because the proxy's injection appeared as a prior tool result).
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


def _classification() -> dict[str, Any]:
    return {
        "is_valid": True,
        "intent": "CHAT",
        "priority": 2,
        "complexity": "low",
        "project_name": "general",
        "is_factual": False,
        "tools_required": False,
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
# R1 contract: proxy messages are custom events, not content deltas
# ---------------------------------------------------------------------------

class TestProxyEventsAreNotContent:
    """The proxy must not inject ``delta.content`` for in-flight proxy
    messages (triage, loading, audit-halt).  Those are emitted as custom
    SSE events so OpenAI-compatible clients do not render them inline.
    """

    @pytest.mark.asyncio
    async def test_triage_emitted_as_custom_event_not_content(self, r1_client) -> None:
        """The first SSE event after the model starts should be
        ``kinver.proxy.triage`` with the message in the event payload,
        not as a ``data: {delta.content: "..."}`` chunk.
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

        # Find the triage event
        triage_events = [e for e in events if e[0] == "kinver.proxy.triage"]
        assert triage_events, f"expected kinver.proxy.triage event in {events!r}"
        triage_name, triage_data = triage_events[0]
        assert triage_data.get("message", "").startswith("🔍")

        # CRITICAL: no data: chunk should carry the triage message as
        # delta.content.  OpenAI clients render those as model messages.
        for event_name, event_data in events:
            if event_name:  # skip the custom event itself
                continue
            choices = event_data.get("choices", [])
            for ch in choices:
                delta = ch.get("delta", {})
                content = delta.get("content") or ""
                assert "🔍 Proxy triage" not in content, (
                    f"triage must not be rendered as content delta; "
                    f"found {content!r} in {event_name!r} event"
                )

    @pytest.mark.asyncio
    async def test_no_audit_override_finish_reason(self) -> None:
        """The Graceful Guillotine must terminate with ``finish_reason: "stop"``,
        not the non-standard ``"audit_override"``.  OpenAI clients treat
        unknown finish reasons as errors.
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

    @pytest.mark.asyncio
    async def test_loading_emitted_as_custom_event(self, r1_client) -> None:
        """When a heavy model is loading, the message goes in a
        ``kinver.proxy.loading`` event, not as a content delta.

        This is tested indirectly: if the model is already loaded (the
        common case), the loading event is not emitted.  The contract
        is that whenever it IS emitted, it uses the custom event format.
        We verify the helper directly.
        """
        from routes import _make_proxy_event

        # Confirm _make_proxy_event produces the event: ... format
        out = _make_proxy_event("kinver.proxy.loading", {"message": "Loading X", "model": "x"})
        assert out.startswith("event: kinver.proxy.loading\n")
        assert "Loading X" in out
        # Must not be a data: chunk (which clients would render as content)
        assert not out.startswith("data: "), (
            "_make_proxy_event must not produce a data: chunk"
        )


# ---------------------------------------------------------------------------
# Tool-call defaults: disable thinking when tools are present
# ---------------------------------------------------------------------------

class TestToolCallThinkingDefault:
    """When the request includes tools, the proxy must disable Qwen 3.5's
    extended-thinking mode by default.  The thinking preamble was producing
    ~60-100 chunks of reasoning_content before any tool call, which (a)
    wastes tokens, (b) confuses llama.cpp's tool-call parser, and (c)
    surfaces raw reasoning to OpenAI clients that render it inline.

    The ``X-Proxy-Thinking: true`` header re-enables thinking.
    """

    def _capture_request_payload(self) -> dict[str, Any]:
        """Build a chat_completions request and capture the payload sent
        to ``stream_llm``.  Returns the captured ``payload`` dict.
        """
        capture = _StreamCapture()
        return capture

    @pytest.mark.asyncio
    async def test_tools_request_disables_thinking_by_default(
        self, r1_client,
    ) -> None:
        capture = self._capture_request_payload()
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
        assert "chat_template_kwargs" in capture.payload, (
            "tools-present request must include chat_template_kwargs"
        )
        assert capture.payload["chat_template_kwargs"] == {"enable_thinking": False}

    @pytest.mark.asyncio
    async def test_tools_request_with_opt_in_header_keeps_thinking(
        self, r1_client,
    ) -> None:
        capture = self._capture_request_payload()
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
        # With the opt-in header, the proxy must NOT inject
        # chat_template_kwargs; the model keeps its default thinking mode.
        assert "chat_template_kwargs" not in capture.payload, (
            f"opt-in header must suppress the default; got {capture.payload!r}"
        )

    @pytest.mark.asyncio
    async def test_non_tools_request_does_not_inject_template_kwargs(
        self, r1_client,
    ) -> None:
        capture = self._capture_request_payload()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ):
            response = await r1_client.post(
                "/v1/chat/completions",
                json={
                    "model": "professional",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.payload is not None
        assert "chat_template_kwargs" not in capture.payload
