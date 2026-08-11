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
        # the observed entries are keyed by caller AND content hash so
        # every DISTINCT system prompt gets its own primable entry
        assert any(
            k.startswith("observed:openwebui:") and v == "You are OpenWebUI."
            for k, v in reg.items()
        )
        # refresh on change registers a new distinct entry
        pc.register_observed_system_prompt("openwebui", "New prompt.")
        assert any(
            k.startswith("observed:openwebui:") and v == "New prompt."
            for k, v in pc.registered_prompts().items()
        )

    def test_seek_nanobot_prompt_registers_and_refreshes(
        self, tmp_path: Any,
    ) -> None:
        import prompt_cache as pc
        pc._PROMPTS.clear()
        pc._LAST_PRIMED.clear()
        (tmp_path / "SOUL.md").write_text("I am nanobot. Soul text.", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("Be concise.", encoding="utf-8")
        assembled = pc.seek_nanobot_prompt(str(tmp_path))
        assert "I am nanobot. Soul text." in assembled
        assert "Be concise." in assembled
        assert pc.registered_prompts().get("nanobot") == assembled
        # a change (e.g. after a dream) refreshes the registration
        (tmp_path / "SOUL.md").write_text("I am nanobot v2. Updated by dream.", encoding="utf-8")
        assembled2 = pc.seek_nanobot_prompt(str(tmp_path))
        assert "Updated by dream" in assembled2
        assert pc.registered_prompts().get("nanobot") == assembled2

    def test_prime_runs_registered_prompts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import prompt_cache as pc
        pc._PROMPTS.clear()
        pc._LAST_PRIMED.clear()
        pc.register_prompt("nanobot", "You are nanobot.")
        calls: list[tuple[int, str]] = []

        async def fake_call(port: int, prompt: str, max_tokens: int = 2048) -> str:
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


class TestDeterministicNanobotSeek:
    """The nanobot system prompt is deterministic given the workspace
    files; the seek assembles the full stable head (identity template +
    bootstrap blocks + long-term memory) in the nanobot's exact order and
    refreshes on file changes (the automatic monitoring)."""

    def _workspace(self, tmp_path: Any) -> Any:
        (tmp_path / "AGENTS.md").write_text("AGENTS body", encoding="utf-8")
        (tmp_path / "SOUL.md").write_text("SOUL body", encoding="utf-8")
        (tmp_path / "USER.md").write_text("USER body", encoding="utf-8")
        (tmp_path / "TOOLS.md").write_text("TOOLS body", encoding="utf-8")
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "MEMORY.md").write_text("MEM body", encoding="utf-8")
        return tmp_path

    def test_full_deterministic_assembly(self, tmp_path: Any) -> None:
        import prompt_cache as pc
        pc._PROMPTS.clear()
        ws = self._workspace(tmp_path)
        assembled = pc.seek_nanobot_prompt(str(ws))
        # identity (rendered) first
        assert assembled.startswith("## Runtime\n")
        assert "## Platform Policy (POSIX)" in assembled
        # bootstrap blocks in the nanobot's exact order
        assert assembled.index("## AGENTS.md") < assembled.index("## SOUL.md")
        assert assembled.index("## SOUL.md") < assembled.index("## USER.md")
        assert assembled.index("## USER.md") < assembled.index("## TOOLS.md")
        # memory section after the bootstrap
        assert "## TOOLS.md" in assembled
        assert "## Long-term Memory\nMEM body" in assembled
        # registered + deterministic
        assert pc.registered_prompts().get("nanobot") == assembled
        assert pc.seek_nanobot_prompt(str(ws)) == assembled

    def test_memory_change_forces_refresh(self, tmp_path: Any) -> None:
        import prompt_cache as pc
        pc._PROMPTS.clear()
        pc._LAST_PRIMED.clear()
        ws = self._workspace(tmp_path)
        pc.seek_nanobot_prompt(str(ws))
        assert "old memory" not in pc.registered_prompts()["nanobot"]
        (ws / "memory" / "MEMORY.md").write_text("old memory", encoding="utf-8")
        pc.seek_nanobot_prompt(str(ws))
        assert "old memory" in pc.registered_prompts()["nanobot"]
        # the refresh forces a re-prime (throttle cleared)
        assert "nanobot" not in pc._LAST_PRIMED


class TestDeterministicHistoryAndSkills:
    """The full deterministic head: active skills + skills summary +
    recent history (the only variable is the session summary + the
    conversation turns, which priming cannot warm)."""

    def _workspace(self, tmp_path: Any) -> Any:
        (tmp_path / "AGENTS.md").write_text("AGENTS body", encoding="utf-8")
        (tmp_path / "SOUL.md").write_text("SOUL body", encoding="utf-8")
        (tmp_path / "USER.md").write_text("USER body", encoding="utf-8")
        (tmp_path / "TOOLS.md").write_text("TOOLS body", encoding="utf-8")
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "MEMORY.md").write_text("MEM body", encoding="utf-8")
        (mem / ".dream_cursor").write_text("0", encoding="utf-8")
        return tmp_path

    def test_history_and_skills_sections(self, tmp_path: Any) -> None:
        import json as _json
        import prompt_cache as pc
        pc._PROMPTS.clear()
        ws = self._workspace(tmp_path)
        hist = ws / "memory" / "history.jsonl"
        hist.write_text(
            "\n".join([
                _json.dumps({"cursor": i, "timestamp": f"t{i}", "content": f"c{i}"})
                for i in (1, 2, 3)
            ]),
            encoding="utf-8",
        )
        assembled = pc.seek_nanobot_prompt(str(ws))
        assert "# Recent History" in assembled
        assert "- [t3] c3" in assembled
        # the dream cursor filters older entries
        (ws / "memory" / ".dream_cursor").write_text("2", encoding="utf-8")
        assembled2 = pc.seek_nanobot_prompt(str(ws))
        assert "- [t3] c3" in assembled2
        assert "- [t1] c1" not in assembled2
        # a history change refreshes the registration (the interaction-
        # driven updates are visible to the proxy)
        hist.write_text(
            _json.dumps({"cursor": 4, "timestamp": "t4", "content": "c4"}),
            encoding="utf-8",
        )
        assembled3 = pc.seek_nanobot_prompt(str(ws))
        assert "- [t4] c4" in assembled3
        assert pc.registered_prompts().get("nanobot") == assembled3

    def test_skills_sections_present(self, tmp_path: Any) -> None:
        import prompt_cache as pc
        pc._PROMPTS.clear()
        ws = self._workspace(tmp_path)
        skills = ws / "skills" / "probe-skill"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text(
            "---\nname: probe-skill\ndescription: Probe skill\nalways: true\n"
            "requires:\n  bins: []\n---\nProbe body\n",
            encoding="utf-8",
        )
        assembled = pc.seek_nanobot_prompt(str(ws))
        assert "# Active Skills" in assembled
        assert "### Skill: probe-skill" in assembled
        assert "Probe body" in assembled
        assert "# Skills" in assembled
        assert "**probe-skill**" in assembled


class TestPerChannelPrimingVariants:
    """The wire's identity renders the format hint for the caller's
    channel, so every channel variant must be registered and primable —
    the channel-less-only prime never matched the wire (2026-08-11)."""

    def _workspace(self, tmp_path: Any) -> Any:
        (tmp_path / "AGENTS.md").write_text("AGENTS body", encoding="utf-8")
        (tmp_path / "SOUL.md").write_text("SOUL body", encoding="utf-8")
        (tmp_path / "USER.md").write_text("USER body", encoding="utf-8")
        (tmp_path / "TOOLS.md").write_text("TOOLS body", encoding="utf-8")
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "MEMORY.md").write_text("MEM body", encoding="utf-8")
        (mem / ".dream_cursor").write_text("0", encoding="utf-8")
        (mem / "history.jsonl").write_text(
            '{"cursor": 1, "timestamp": "t1", "content": "c1"}',
            encoding="utf-8",
        )
        return tmp_path

    def test_all_channel_variants_registered(self, tmp_path: Any) -> None:
        import prompt_cache as pc
        pc._PROMPTS.clear()
        ws = self._workspace(tmp_path)
        pc.seek_nanobot_prompt(str(ws))
        reg = pc.registered_prompts()
        for key in (
            "nanobot", "nanobot:telegram", "nanobot:qq", "nanobot:discord",
            "nanobot:whatsapp", "nanobot:sms", "nanobot:email",
            "nanobot:cli", "nanobot:mochat",
        ):
            assert key in reg, key
        # the messaging channels carry the hint; the base does not
        assert "## Format Hint" in reg["nanobot:telegram"]
        assert "## Format Hint" not in reg["nanobot"]
        # every variant shares the full deterministic tail
        for key in reg:
            assert "## AGENTS.md" in reg[key]
            assert "# Recent History" in reg[key]


class TestRuntimeVersionMatch:
    """The wire's identity renders the TOOL's python version (the uv
    tool env), which differs from the proxy's own python in the patch —
    a single token mismatch at the runtime line kills the KV-cache match.
    The seek must render the tool's version."""

    def test_runtime_uses_tool_python(self, tmp_path: Any) -> None:
        import prompt_cache as pc
        pc._PROMPTS.clear()
        (tmp_path / "AGENTS.md").write_text("AGENTS body", encoding="utf-8")
        (tmp_path / "SOUL.md").write_text("SOUL body", encoding="utf-8")
        (tmp_path / "USER.md").write_text("USER body", encoding="utf-8")
        (tmp_path / "TOOLS.md").write_text("TOOLS body", encoding="utf-8")
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "MEMORY.md").write_text("MEM body", encoding="utf-8")
        (mem / ".dream_cursor").write_text("0", encoding="utf-8")
        assembled = pc.seek_nanobot_prompt(str(tmp_path))
        assert "Python 3.14.4" in assembled.splitlines()[1]
        # the WIRE template's identity text (the lib64 install)
        assert "Your current project workspace is at:" in assembled
        assert "- Agent profile:" in assembled
        assert "## External Content" in assembled
        # not the proxy's own version when they differ
        import platform as _platform
        assert "Python 3.14.6" not in assembled.splitlines()[1]
