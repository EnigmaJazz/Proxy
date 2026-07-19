"""R19 tests: client-named-model override + R1/R7 client-wins contract.

These regression tests pin three contracts:

1. Client model selection is honored — ``stream_llm()`` is called with the
   client's model even when the frontdesk classifier would have picked a
   different one.
2. Client sampling parameters (temperature/top_p/max_tokens) are forwarded
   verbatim when the client picks the model — no proxy override.
3. The auto-routing path still applies profile values to the payload (R17).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import proxy
from profile_loader import ModelProfileTable
from routing import RouteDecision


# ---------------------------------------------------------------------------
# Self-contained async test client fixture
# ---------------------------------------------------------------------------
# conftest.py declares ``app_client`` as a plain ``@pytest.fixture`` (sync) on
# an async function. In pytest-asyncio strict mode that decorator is rejected.
# We declare our own equivalent here so this test file is independent of the
# (broken) conftest fixture.

@pytest_asyncio.fixture
async def r19_client() -> Any:
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

    transport = httpx.ASGITransport(app=proxy.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_profile_table() -> ModelProfileTable:
    """Build a deterministic profile table for the tests."""
    return ModelProfileTable([
        {
            "model": "chatter",
            "intent": "chat",
            "temperature": 0.7,
            "top_p": 1.0,
            "max_tokens": 235929,
            "thinking_budget_tokens": 0,
        },
        {
            "model": "coder",
            "intent": "code",
            "temperature": 0.2,
            "top_p": 0.95,
            "max_tokens": 235929,
            "thinking_budget_tokens": 4096,
        },
    ])


def _classification() -> dict[str, Any]:
    """Return a deterministic CHAT classification from the frontdesk."""
    return {
        "is_valid": True,
        "intent": "CHAT",
        "priority": 2,
        "complexity": "low",
        "project_name": "general",
        "is_factual": False,
        "tools_required": False,
    }


def _route_decision() -> RouteDecision:
    """Return a deterministic Lane-A RouteDecision (model=chatter)."""
    return RouteDecision(
        model_key="chatter",
        port=13001,
        is_cpu_fallback=False,
        hardware_path="gpu",
        priority=2,
        intent="CHAT",
        project_id="general",
        is_factual=False,
        is_lane_b=False,
    )


class _StreamCapture:
    """Async-generator stand-in for ``stream_llm`` that records its arguments."""

    def __init__(self) -> None:
        self.endpoint: str | None = None
        self.payload: dict[str, Any] | None = None
        self.port: int | None = None
        self.headers: dict[str, str] | None = None

    async def __call__(self, *, endpoint, payload, port=0, headers=None, **kwargs):
        self.endpoint = endpoint
        self.payload = payload
        self.port = port
        self.headers = headers
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "ok"},
                    "finish_reason": "stop",
                },
            ],
        }


def _install_profiles() -> None:
    """Install a deterministic profile table on ``proxy.app.state``."""
    proxy.app.state.model_profiles = _make_profile_table()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClientNamedModel:
    """R19: client-named model + R1/R7 client-wins contract."""

    @pytest.mark.asyncio
    async def test_client_named_model_overrides_route(
        self, r19_client,
    ) -> None:
        """Contract 1: ``model='coder'`` → ``stream_llm(endpoint='coder')``."""
        _install_profiles()
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ), \
             patch(
                 "routes.resolve_route_for_lane_a",
                 new=AsyncMock(return_value=_route_decision()),
             ):
            response = await r19_client.post(
                "/v1/chat/completions",
                json={
                    "model": "coder",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.endpoint == "coder", (
            f"expected endpoint='coder', got {capture.endpoint!r}"
        )

    @pytest.mark.asyncio
    async def test_client_sampling_params_honored(
        self, r19_client,
    ) -> None:
        """Contract 2: client sampling params forwarded verbatim (R1/R7)."""
        _install_profiles()
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ), \
             patch(
                 "routes.resolve_route_for_lane_a",
                 new=AsyncMock(return_value=_route_decision()),
             ):
            response = await r19_client.post(
                "/v1/chat/completions",
                json={
                    "model": "coder",
                    "messages": [{"role": "user", "content": "hello"}],
                    "temperature": 0.5,
                    "top_p": 0.7,
                    "max_tokens": 1234,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.endpoint == "coder"
        assert capture.payload is not None
        assert capture.payload["temperature"] == 0.5
        assert capture.payload["top_p"] == 0.7
        assert capture.payload["max_tokens"] == 1234

    @pytest.mark.asyncio
    async def test_auto_routed_uses_profile_values(
        self, r19_client,
    ) -> None:
        """Contract 3: ``model='auto'`` applies profile values (R17 regression)."""
        _install_profiles()
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ), \
             patch(
                 "routes.resolve_route_for_lane_a",
                 new=AsyncMock(return_value=_route_decision()),
             ):
            response = await r19_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "hello"}],
                    "temperature": 0.5,
                    "top_p": 0.7,
                    "max_tokens": 1234,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.endpoint == "chatter"
        assert capture.payload is not None
        assert capture.payload["temperature"] == 0.7
        assert capture.payload["top_p"] == 1.0
        assert capture.payload["max_tokens"] == 235929
