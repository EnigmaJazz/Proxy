"""Dream/soul fast-path routing tests.

The Nanobot dream pathway must route to professional (35B MoE) — the
same model as CHAT/TOOL — not architect.  This pins the model key,
profile resolution, and SSE params_replaced event after the switch.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import proxy
from profile_loader import ModelProfileTable


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
