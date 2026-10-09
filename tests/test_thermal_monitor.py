"""Regression tests for the thermal monitor's zone mapping.

Guards the spd5118 hazard: RAM usage percent was once fed as a fake
DDR5 temperature and compared against degree-C thresholds, which
triggered ``systemctl poweroff -i`` at 80% RAM usage on a busy proxy.
"""

from pathlib import Path

import pytest

import hardware
from constants import GPU_BUSY_VRAM_GB
from hardware import THERMAL_LIMITS, VRAM_DEFAULT_TOTAL_MB, _build_thermal_temps


def test_thermal_limits_has_no_fake_spd5118_zone() -> None:
    assert "spd5118" not in THERMAL_LIMITS


def test_build_thermal_temps_never_maps_ram_usage_as_temperature() -> None:
    # Even at extreme CPU/GPU values, the enforcement map contains only
    # real sensors — RAM usage percent must never enter a °C comparison.
    temps = _build_thermal_temps(
        cpu=85.0,
        gpu={"edge": 90.0, "junction": 95.0, "vram": 92.0, "vram_used_gb": 8.0},
    )
    assert "spd5118" not in temps
    assert set(temps) == {"k10temp", "amdgpu_core", "amdgpu_vram"}


# ---------------------------------------------------------------------------
# VRAM usage from sysfs (2026-10-08 freeze: the residency "GPU busy" guard
# read 0.0 GB forever because VRAM usage came only from pyrsmi, which is not
# installed, while the hwmon fallback reads temperatures only).
# ---------------------------------------------------------------------------

_GIB = 1024**3


def _fake_card(root: Path, name: str, total: int, used: int, vendor: str = "0x1002") -> None:
    device = root / name / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text(f"{vendor}\n")
    (device / "mem_info_vram_total").write_text(f"{total}\n")
    (device / "mem_info_vram_used").write_text(f"{used}\n")


def test_vram_used_reads_the_largest_amd_card(tmp_path: Path) -> None:
    # card0 is the 512 MB iGPU, card1 the 12 GB discrete card that runs the
    # models; connector nodes (card1-DP-1) carry no memory counters.
    _fake_card(tmp_path, "card0", total=512 * 1024**2, used=15 * 1024**2)
    _fake_card(tmp_path, "card1", total=12 * _GIB, used=int(11.5 * _GIB))
    (tmp_path / "card1-DP-1").mkdir()

    assert hardware._read_amdgpu_vram_used_gb(tmp_path) == pytest.approx(11.5)


def test_vram_used_ignores_non_amd_cards(tmp_path: Path) -> None:
    _fake_card(tmp_path, "card0", total=24 * _GIB, used=20 * _GIB, vendor="0x10de")
    _fake_card(tmp_path, "card1", total=12 * _GIB, used=2 * _GIB)

    assert hardware._read_amdgpu_vram_used_gb(tmp_path) == pytest.approx(2.0)


def test_vram_used_is_zero_when_unreadable(tmp_path: Path) -> None:
    assert hardware._read_amdgpu_vram_used_gb(tmp_path / "missing") == 0.0
    device = tmp_path / "card0" / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text("0x1002\n")
    (device / "mem_info_vram_total").write_text("not-a-number\n")
    assert hardware._read_amdgpu_vram_used_gb(tmp_path) == 0.0


def test_gpu_temps_fallback_reports_vram_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware, "PYRSMI_AVAILABLE", False)
    monkeypatch.setattr(
        hardware, "_read_amdgpu_hwmon",
        lambda: {"edge": 40.0, "junction": 45.0, "vram": 46.0},
    )
    monkeypatch.setattr(hardware, "_read_amdgpu_vram_used_gb", lambda: 11.5)

    assert hardware.get_gpu_temps() == {
        "edge": 40.0, "junction": 45.0, "vram": 46.0, "vram_used_gb": 11.5,
    }


def test_gpu_busy_threshold_fits_the_card() -> None:
    # 22 GB could never be reached on the 12 GB card, so the guard was dead.
    assert GPU_BUSY_VRAM_GB < VRAM_DEFAULT_TOTAL_MB / 1024
