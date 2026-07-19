"""Professional-as-default routing, profiles, and queue lifecycle tests.

Covers REQ-1 through REQ-8 from the Professional Default Routing spec.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import proxy
from profile_loader import ModelProfileTable
from routing import RouteDecision, resolve_route_for_lane_a


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
class _FakeSystemd:
    """Configurable fake systemd for routing tests."""

    def __init__(
        self,
        *,
        occupied: bool = False,
        active: str | None = None,
        port: int = 13001,
    ) -> None:
        self._occupied = occupied
        self.active_heavy_model = active
        self._port = port
        self.ports_requested: list[str] = []
        self.unloads: int = 0
        self.hotswaps: list[tuple[str, str]] = []

    async def get_port(self, domain: str) -> int:
        self.ports_requested.append(domain)
        return self._port

    async def is_active(self, domain: str) -> bool:
        return self.active_heavy_model == domain

    async def unload_all_heavy(self) -> None:
        self.unloads += 1
        self.active_heavy_model = None

    async def hot_swap(self, from_domain: str, to_domain: str) -> int:
        self.hotswaps.append((from_domain, to_domain))
        self.active_heavy_model = to_domain
        return self._port

    def is_gpu_occupied(self) -> bool:
        return self._occupied


def _classification(
    intent: str = "CHAT",
    *,
    complexity: str = "low",
    tools_required: bool = False,
) -> dict[str, Any]:
    return {
        "is_valid": True,
        "intent": intent,
        "priority": 2,
        "complexity": complexity,
        "project_name": "general",
        "is_factual": False,
        "tools_required": tools_required,
    }


def _make_profile_table() -> ModelProfileTable:
    """Deterministic profile table covering all scenarios."""
    return ModelProfileTable([
        {
            "model": "professional",
            "intent": "chat",
            "temperature": 0.7,
            "top_p": 1.0,
            "max_tokens": 235929,
            "thinking_budget_tokens": 0,
        },
        {
            "model": "professional",
            "intent": "code",
            "temperature": 0.2,
            "top_p": 0.95,
            "max_tokens": 235929,
            "thinking_budget_tokens": 4096,
        },
        {
            "model": "chatter",
            "intent": "chat",
            "temperature": 0.7,
            "top_p": 1.0,
            "max_tokens": 235929,
            "thinking_budget_tokens": 0,
        },
        {
            "model": "worker",
            "intent": "code",
            "temperature": 0.2,
            "top_p": 0.95,
            "max_tokens": 235929,
            "thinking_budget_tokens": 4096,
        },
    ])


# ---------------------------------------------------------------------------
# Phase 1: Routing
# ---------------------------------------------------------------------------
class TestRouting:
    """REQ-1/2/3: auto CHAT/TOOL/CODE route to Professional."""

    @pytest.mark.asyncio
    async def test_auto_chat_free_gpu_routes_to_professional(self) -> None:
        """Scenario-1: CHAT + free GPU → Professional."""
        systemd = _FakeSystemd(occupied=False)
        decision = await resolve_route_for_lane_a(
            _classification("CHAT"), systemd, has_tool_history=False,
        )
        assert decision.model_key == "professional"
        assert decision.is_cpu_fallback is False
        assert decision.hardware_path == "gpu"
        assert "professional" in decision.model_key

    @pytest.mark.asyncio
    async def test_auto_chat_occupied_gpu_lifeboat(self) -> None:
        """CHAT + GPU occupied by specialist → Lifeboat contention fallback."""
        systemd = _FakeSystemd(occupied=True, active="coder")
        decision = await resolve_route_for_lane_a(
            _classification("CHAT"), systemd, has_tool_history=False,
        )
        assert decision.model_key == "lifeboat"
        assert decision.is_cpu_fallback is True
        assert decision.hardware_path == "cpu"

    @pytest.mark.asyncio
    async def test_auto_chat_resident_professional_stays(self) -> None:
        """CHAT + GPU occupied by resident Professional → Professional."""
        systemd = _FakeSystemd(occupied=True, active="professional")
        decision = await resolve_route_for_lane_a(
            _classification("CHAT"), systemd, has_tool_history=False,
        )
        assert decision.model_key == "professional"
        assert decision.is_cpu_fallback is False

    @pytest.mark.asyncio
    async def test_auto_tool_free_gpu_routes_to_professional(self) -> None:
        """Scenario-2: TOOL + free GPU → Professional."""
        systemd = _FakeSystemd(occupied=False)
        decision = await resolve_route_for_lane_a(
            _classification("TOOL", tools_required=True), systemd, has_tool_history=False,
        )
        assert decision.model_key == "professional"
        assert decision.is_cpu_fallback is False

    @pytest.mark.asyncio
    async def test_auto_tool_mid_flow_forces_professional(self) -> None:
        """Mid-tool-flow TOOL with specialist active → Professional (Lifeboat rejects history)."""
        systemd = _FakeSystemd(occupied=True, active="coder")
        decision = await resolve_route_for_lane_a(
            _classification("TOOL", tools_required=True), systemd, has_tool_history=True,
        )
        assert decision.model_key == "professional"
        assert decision.tools_required is True

    @pytest.mark.asyncio
    async def test_auto_tool_occupied_no_history_lifeboat(self) -> None:
        """TOOL + GPU occupied by specialist, no history → Lifeboat."""
        systemd = _FakeSystemd(occupied=True, active="coder")
        decision = await resolve_route_for_lane_a(
            _classification("TOOL", tools_required=True), systemd, has_tool_history=False,
        )
        assert decision.model_key == "lifeboat"
        assert decision.is_cpu_fallback is True

    @pytest.mark.asyncio
    async def test_auto_code_routes_to_professional(self) -> None:
        """Scenario-3: CODE → Professional."""
        systemd = _FakeSystemd(occupied=False)
        decision = await resolve_route_for_lane_a(
            _classification("CODE"), systemd, has_tool_history=False,
        )
        assert decision.model_key == "professional"
        assert decision.is_cpu_fallback is False

    @pytest.mark.asyncio
    async def test_specialist_intents_unchanged(self) -> None:
        """SCHOLAR/CREATIVE/ARCHITECT keep their specialist destinations."""
        for intent, expected in (
            ("SCHOLAR", "scholar"),
            ("CREATIVE", "creative"),
            ("ARCHITECT", "architect"),
        ):
            systemd = _FakeSystemd(occupied=False)
            decision = await resolve_route_for_lane_a(
                _classification(intent), systemd, has_tool_history=False,
            )
            assert decision.model_key == expected, f"{intent} should route to {expected}"


# ---------------------------------------------------------------------------
# Phase 2: Profiles
# ---------------------------------------------------------------------------
class TestProfiles:
    """REQ-4: exact professional/chat and professional/code rows."""

    def test_professional_chat_values(self) -> None:
        table = _make_profile_table()
        entry = table.resolve("CHAT", "professional")
        assert entry is not None
        assert entry.source == "exact"
        assert entry.values["temperature"] == 0.7
        assert entry.values["top_p"] == 1.0
        assert entry.values["max_tokens"] == 235929
        assert entry.values["thinking_budget_tokens"] == 0

    def test_professional_code_values(self) -> None:
        table = _make_profile_table()
        for intent in ("CODE", "TOOL"):
            entry = table.resolve(intent, "professional")
            assert entry is not None
            assert entry.source == "exact"
            assert entry.values["temperature"] == 0.2
            assert entry.values["top_p"] == 0.95
            assert entry.values["max_tokens"] == 235929
            assert entry.values["thinking_budget_tokens"] == 4096


# ---------------------------------------------------------------------------
# Phase 4: Integration / payload tests
# ---------------------------------------------------------------------------
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


@pytest_asyncio.fixture
async def pd_client() -> Any:
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


class TestIntegration:
    """End-to-end payload and regression tests for Scenarios 1-6."""

    @pytest.mark.asyncio
    async def test_auto_chat_applies_professional_chat_profile(self, pd_client) -> None:
        """Scenario-1: auto CHAT → Professional with chat profile values."""
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CHAT")),
             ):
            response = await pd_client.post(
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
        assert capture.endpoint == "professional"
        assert capture.payload["temperature"] == 0.7
        assert capture.payload["top_p"] == 1.0
        assert capture.payload["max_tokens"] == 235929
        assert capture.payload["thinking_budget_tokens"] == 0

    @pytest.mark.asyncio
    async def test_auto_tool_applies_professional_code_profile(self, pd_client) -> None:
        """Scenario-2: auto TOOL → Professional with code profile values."""
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("TOOL", tools_required=True)),
             ):
            response = await pd_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "search the web"}],
                    "tools": [{"type": "function", "function": {"name": "web_search"}}],
                    "temperature": 0.5,
                    "top_p": 0.7,
                    "max_tokens": 1234,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.endpoint == "professional"
        assert capture.payload["temperature"] == 0.2
        assert capture.payload["top_p"] == 0.95
        assert capture.payload["max_tokens"] == 235929
        assert capture.payload["thinking_budget_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_auto_code_applies_professional_code_profile(self, pd_client) -> None:
        """Scenario-3: auto CODE → Professional with code profile values."""
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CODE")),
             ):
            response = await pd_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "write a python function"}],
                    "temperature": 0.5,
                    "top_p": 0.7,
                    "max_tokens": 1234,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.endpoint == "professional"
        assert capture.payload["temperature"] == 0.2
        assert capture.payload["top_p"] == 0.95
        assert capture.payload["max_tokens"] == 235929
        assert capture.payload["thinking_budget_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_explicit_professional_client_wins(self, pd_client) -> None:
        """Scenario-4: client names Professional and supplies sampling values."""
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CHAT")),
             ):
            response = await pd_client.post(
                "/v1/chat/completions",
                json={
                    "model": "professional",
                    "messages": [{"role": "user", "content": "hello"}],
                    "temperature": 0.11,
                    "top_p": 0.22,
                    "max_tokens": 3333,
                    "thinking_budget_tokens": 4444,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.endpoint == "professional"
        assert capture.payload["temperature"] == 0.11
        assert capture.payload["top_p"] == 0.22
        assert capture.payload["max_tokens"] == 3333
        assert capture.payload["thinking_budget_tokens"] == 4444

    @pytest.mark.asyncio
    async def test_explicit_chatter_opt_in(self, pd_client) -> None:
        """Scenario-5: client names chatter → chatter is used."""
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CHAT")),
             ):
            response = await pd_client.post(
                "/v1/chat/completions",
                json={
                    "model": "chatter",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.endpoint == "chatter"

    @pytest.mark.asyncio
    async def test_explicit_worker_opt_in(self, pd_client) -> None:
        """Scenario-5: client names worker → worker is used."""
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("TOOL", tools_required=True)),
             ):
            response = await pd_client.post(
                "/v1/chat/completions",
                json={
                    "model": "worker",
                    "messages": [{"role": "user", "content": "run a tool"}],
                    "tools": [{"type": "function", "function": {"name": "web_search"}}],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.endpoint == "worker"

    @pytest.mark.asyncio
    async def test_explicit_specialist_opt_in(self, pd_client) -> None:
        """Scenario-6: client names coder with sampling values."""
        capture = _StreamCapture()
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification("CODE")),
             ):
            response = await pd_client.post(
                "/v1/chat/completions",
                json={
                    "model": "coder",
                    "messages": [{"role": "user", "content": "refactor"}],
                    "temperature": 0.33,
                    "top_p": 0.66,
                    "max_tokens": 5555,
                    "thinking_budget_tokens": 6666,
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200, response.text
        assert capture.endpoint == "coder"
        assert capture.payload["temperature"] == 0.33
        assert capture.payload["top_p"] == 0.66
        assert capture.payload["max_tokens"] == 5555
        assert capture.payload["thinking_budget_tokens"] == 6666


# ---------------------------------------------------------------------------
# Phase 3: Queue lifecycle
# ---------------------------------------------------------------------------
class TestQueueLifecycle:
    """REQ-7/8: Professional resident preservation and specialist cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_preserves_professional(self) -> None:
        """Scenario-7: empty queue with Professional active skips unload."""
        from proxy import _cleanup_idle_heavy

        systemd = _FakeSystemd(active="professional")
        state = proxy.AppState()
        state.active_heavy_model = None
        await _cleanup_idle_heavy(systemd, state)
        assert systemd.unloads == 0
        assert state.active_heavy_model == "professional"

    @pytest.mark.asyncio
    async def test_cleanup_unloads_specialist(self) -> None:
        """Specialist active when queue empties → unload and clear state."""
        from proxy import _cleanup_idle_heavy

        systemd = _FakeSystemd(active="coder")
        state = proxy.AppState()
        state.active_heavy_model = "coder"
        await _cleanup_idle_heavy(systemd, state)
        assert systemd.unloads == 1
        assert state.active_heavy_model is None

    @pytest.mark.asyncio
    async def test_cleanup_reconciles_externally_active_professional(self) -> None:
        """Controller state stale; probe finds Professional active."""
        from proxy import _cleanup_idle_heavy

        systemd = _FakeSystemd(active=None)
        systemd.is_active = AsyncMock(return_value=True)
        state = proxy.AppState()
        state.active_heavy_model = None
        await _cleanup_idle_heavy(systemd, state)
        assert state.active_heavy_model == "professional"
        assert systemd.unloads == 0

    @pytest.mark.asyncio
    async def test_cleanup_failsafe_on_probe_error(self) -> None:
        """OSError probing Professional is logged; destructive cleanup skipped."""
        from proxy import _cleanup_idle_heavy

        systemd = _FakeSystemd(active=None)
        systemd.is_active = AsyncMock(side_effect=OSError("probe failed"))
        state = proxy.AppState()
        state.active_heavy_model = None
        await _cleanup_idle_heavy(systemd, state)
        assert state.active_heavy_model is None
        assert systemd.unloads == 0

    @pytest.mark.asyncio
    async def test_specialist_hotswap_not_blocked(self) -> None:
        """Scenario-8: hotswap from Professional to coder still works."""
        systemd = _FakeSystemd(active="professional")
        await systemd.hot_swap("professional", "coder")
        assert systemd.hotswaps == [("professional", "coder")]
        assert systemd.active_heavy_model == "coder"
