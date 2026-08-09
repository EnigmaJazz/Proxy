"""Regression tests for professional model residency (keep-warm).

The thermal monitor calls ``SystemdController.ensure_professional_resident``
on a cadence; these tests cover the decision logic: when professional gets
loaded, and when it must NOT be touched (heavy model active, GPU busy with
other work, feature disabled, already running, systemctl failure).
"""

from __future__ import annotations

import importlib
import sys
from typing import Any
from unittest.mock import AsyncMock

import asyncio
import pytest


def _real_systemd() -> tuple[Any, Any]:
    """Return (fresh systemd module, real SystemdController class).

    tests/conftest.py swaps ``systemd.SystemdController`` for ``_NoOpSystemd``
    at import time so proxy tests never touch systemctl.  Re-import the module
    fresh to reach the real class under test, then restore the patched module
    so the rest of the suite is unaffected.
    """
    cached = sys.modules.get("systemd")
    if cached is not None:
        del sys.modules["systemd"]
    try:
        fresh = importlib.import_module("systemd")
    finally:
        if cached is not None:
            sys.modules["systemd"] = cached
    return fresh, fresh.SystemdController


@pytest.fixture
def systemd() -> tuple[Any, Any]:
    """(fresh systemd module, real SystemdController instance, no systemctl)."""
    fresh, cls = _real_systemd()
    return fresh, cls()


def _recorder(calls: list[str]) -> Any:
    """Return an async start_service stand-in that records its domain."""
    async def _record(domain: str) -> None:
        calls.append(domain)
    return _record


class TestEnsureProfessionalResident:
    @pytest.mark.asyncio
    async def test_loads_professional_when_gpu_free(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, ctrl = systemd
        started: list[str] = []
        monkeypatch.setattr(ctrl, "is_active", AsyncMock(return_value=False))
        monkeypatch.setattr(ctrl, "start_service", _recorder(started))

        await ctrl.ensure_professional_resident(gpu_vram_used_gb=10.0)

        assert started == ["professional"]
        assert ctrl.active_heavy_model == "professional"

    @pytest.mark.asyncio
    async def test_does_not_load_when_heavy_model_active(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, ctrl = systemd
        ctrl.active_heavy_model = "coder"
        start_mock = AsyncMock()
        is_active_mock = AsyncMock()
        monkeypatch.setattr(ctrl, "is_active", is_active_mock)
        monkeypatch.setattr(ctrl, "start_service", start_mock)

        await ctrl.ensure_professional_resident(gpu_vram_used_gb=10.0)

        start_mock.assert_not_awaited()
        is_active_mock.assert_not_awaited()
        assert ctrl.active_heavy_model == "coder"

    @pytest.mark.asyncio
    async def test_does_not_load_when_gpu_busy(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh, ctrl = systemd
        busy_vram = fresh.GPU_BUSY_VRAM_GB
        start_mock = AsyncMock()
        is_active_mock = AsyncMock()
        monkeypatch.setattr(ctrl, "is_active", is_active_mock)
        monkeypatch.setattr(ctrl, "start_service", start_mock)

        await ctrl.ensure_professional_resident(gpu_vram_used_gb=busy_vram)
        await ctrl.ensure_professional_resident(gpu_vram_used_gb=busy_vram + 10.0)

        start_mock.assert_not_awaited()
        is_active_mock.assert_not_awaited()
        assert ctrl.active_heavy_model is None

    @pytest.mark.asyncio
    async def test_does_not_load_when_disabled(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh, ctrl = systemd
        monkeypatch.setattr(fresh, "PROFESSIONAL_RESIDENT_ENABLED", False)
        start_mock = AsyncMock()
        is_active_mock = AsyncMock()
        monkeypatch.setattr(ctrl, "is_active", is_active_mock)
        monkeypatch.setattr(ctrl, "start_service", start_mock)

        await ctrl.ensure_professional_resident(gpu_vram_used_gb=10.0)

        start_mock.assert_not_awaited()
        is_active_mock.assert_not_awaited()
        assert ctrl.active_heavy_model is None

    @pytest.mark.asyncio
    async def test_does_not_load_when_already_running(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, ctrl = systemd
        start_mock = AsyncMock()
        monkeypatch.setattr(ctrl, "is_active", AsyncMock(return_value=True))
        monkeypatch.setattr(ctrl, "start_service", start_mock)

        await ctrl.ensure_professional_resident(gpu_vram_used_gb=10.0)

        start_mock.assert_not_awaited()
        assert ctrl.active_heavy_model is None

    @pytest.mark.asyncio
    async def test_does_not_raise_on_systemctl_failure(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, ctrl = systemd

        async def _boom(domain: str) -> None:
            raise RuntimeError("systemctl start llama-professional failed")

        monkeypatch.setattr(ctrl, "is_active", AsyncMock(return_value=False))
        monkeypatch.setattr(ctrl, "start_service", _boom)

        await ctrl.ensure_professional_resident(gpu_vram_used_gb=10.0)

        assert ctrl.active_heavy_model is None

    @pytest.mark.asyncio
    async def test_does_not_raise_on_oserror(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, ctrl = systemd
        monkeypatch.setattr(
            ctrl, "is_active",
            AsyncMock(side_effect=OSError("systemctl missing")),
        )
        start_mock = AsyncMock()
        monkeypatch.setattr(ctrl, "start_service", start_mock)

        await ctrl.ensure_professional_resident(gpu_vram_used_gb=10.0)

        start_mock.assert_not_awaited()
        assert ctrl.active_heavy_model is None


class TestPromptPriming:
    """The prompt registry + priming keep the professional's KV-cache warm."""

    def test_register_and_observed(self) -> None:
        import prompt_cache as pc
        pc._PROMPTS.clear()
        pc.register_prompt("nanobot", "You are nanobot. Be helpful.")
        pc.register_observed_system_prompt("openwebui", "You are OpenWebUI.")
        reg = pc.registered_prompts()
        assert reg["nanobot"] == "You are nanobot. Be helpful."
        assert reg["observed:openwebui"] == "You are OpenWebUI."
        # refresh on change
        pc.register_observed_system_prompt("openwebui", "New prompt.")
        assert pc.registered_prompts()["observed:openwebui"] == "New prompt."

    def test_prime_runs_registered_prompts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import prompt_cache as pc
        pc._PROMPTS.clear()
        pc._LAST_PRIMED.clear()
        pc.register_prompt("nanobot", "You are nanobot.")
        calls: list[tuple[int, str]] = []

        def fake_call(port: int, prompt: str, max_tokens: int = 2048) -> str:
            calls.append((port, prompt))
            return "OK"

        monkeypatch.setattr("prompt_cache.call_model", fake_call)
        import asyncio
        results = asyncio.run(pc.prime(port=13109))
        assert results == {"nanobot": True}
        assert len(calls) == 1
        assert calls[0][0] == 13109
        assert "Say OK" in calls[0][1]
        # re-prime within the interval is skipped
        results2 = asyncio.run(pc.prime(port=13109))
        assert len(calls) == 1


class TestThermalMonitorWiring:
    @pytest.mark.asyncio
    async def test_monitor_calls_residency_check_with_vram(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import hardware

        calls: list[float] = []

        class _FakeSystemd:
            async def ensure_professional_resident(self, gpu_vram_used_gb: float) -> None:
                calls.append(gpu_vram_used_gb)

        monkeypatch.setattr(hardware, "PROFESSIONAL_RESIDENT_CHECK_S", 0.01)
        monkeypatch.setattr(hardware, "get_cpu_temp", lambda: 40.0)
        monkeypatch.setattr(hardware, "get_gpu_temps", lambda: {
            "edge": 45.0, "junction": 50.0, "vram": 42.0, "vram_used_gb": 8.0,
        })
        monkeypatch.setattr(hardware, "get_ram_usage_percent", lambda: 30.0)

        state = hardware.ThermalState()
        task = asyncio.create_task(
            hardware.thermal_monitor_task(state, interval=0.01, systemd=_FakeSystemd()),
        )
        await asyncio.sleep(0.06)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert calls, "residency check must fire on cadence"
        assert calls[0] == 8.0  # fed the current gpu_vram_used_gb from state

    @pytest.mark.asyncio
    async def test_monitor_runs_without_systemd(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import hardware

        monkeypatch.setattr(hardware, "get_cpu_temp", lambda: 40.0)
        monkeypatch.setattr(hardware, "get_gpu_temps", lambda: {
            "edge": 45.0, "junction": 50.0, "vram": 42.0, "vram_used_gb": 8.0,
        })
        monkeypatch.setattr(hardware, "get_ram_usage_percent", lambda: 30.0)

        state = hardware.ThermalState()
        task = asyncio.create_task(
            hardware.thermal_monitor_task(state, interval=0.01, systemd=None),
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert state.cpu == 40.0  # loop still ran fine without a systemd manager
