"""Regression tests for the thermal monitor's zone mapping.

Guards the spd5118 hazard: RAM usage percent was once fed as a fake
DDR5 temperature and compared against degree-C thresholds, which
triggered ``systemctl poweroff -i`` at 80% RAM usage on a busy proxy.
"""

from hardware import THERMAL_LIMITS, _build_thermal_temps


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
