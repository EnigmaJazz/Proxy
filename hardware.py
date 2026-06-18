"""
hardware.py - Hardware governor, thermal monitor & VRAM manager.

Merges the original hardware.py thermal telemetry with warden.py GPU
workload isolation logic into a single async module.  All I/O is
non-blocking and compatible with uvloop.

Responsibilities:
- CPU / GPU / RAM temperature readout (synchronous sensors are fast)
- Async VRAM queries via rocm-smi subprocess
- Async thermal monitor background task with Prometheus metrics
- Emergency shutdown on critical thermal thresholds
- GPU workload isolation (headless detection, VRAM pinning via LACT)
- Predictive cooling IPC writes (low-level — called by cooling.py)
- Notifications: Wayland (libnotify), Telegram, Bash (ar-notify)

The old get_service_info / scan_systemd_for_models functions are
superseded by systemd.SystemdController.

Usage::

    from hardware import HardwareGovernor
    hw = HardwareGovernor()
    temps = hw.get_gpu_temps()
    await hw.verify_vram_availability(8000)
    await hw.arm_gpu_for_inference("professional", 8084)

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any

import httpx
from prometheus_client import Gauge

from constants import (
    THERMAL_LIMITS,
    CPU_TEMP_PATH,
    GPU_TEMP_PATH,
    CoolingPreset,
    PROJECT_ROOT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    SENSOR_INTERVAL,
    get_logger,
)

logger = get_logger("proxy.hardware")

# ---------------------------------------------------------------------------
# PyRSMI availability flag (set once at import time)
# ---------------------------------------------------------------------------
PYRSMI_AVAILABLE: bool = True
try:
    from pyrsmi import rocUtil  # type: ignore
    logger.info("pyrsmi loaded – AMD GPU telemetry available")
except ImportError:
    logger.warning("pyrsmi not installed – GPU temps will be zero")
    PYRSMI_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
THERMAL_COOLDOWN: float = 30.0  # seconds after critical before re-checking
VRAM_DEFAULT_TOTAL_MB: int = 12800  # fallback for 12GB card

# ---------------------------------------------------------------------------
# Prometheus metrics (registered at module level)
# ---------------------------------------------------------------------------
metric_cpu_temp = Gauge("cpu_temp_celsius", "CPU temperature (°C)")
metric_gpu_edge_temp = Gauge("gpu_edge_temp_celsius", "GPU edge temperature (°C)")
metric_gpu_junc_temp = Gauge("gpu_junc_temp_celsius", "GPU junction temperature (°C)")
metric_gpu_vram_temp = Gauge("gpu_vram_temp_celsius", "GPU VRAM temperature (°C)")
metric_gpu_used_vram_gb = Gauge("gpu_used_vram_gb", "GPU VRAM in use (GiB)")
metric_ram_used_pct = Gauge("ram_used_percent", "System RAM usage (%)")

# ---------------------------------------------------------------------------
# In-memory thermal state (updated by background monitor, read by proxy)
# ---------------------------------------------------------------------------
thermal_state: Dict[str, float] = {
    "cpu": 0.0,
    "gpu_edge": 0.0,
    "gpu_junction": 0.0,
    "gpu_vram": 0.0,
    "gpu_vram_used_gb": 0.0,
    "ram_used_percent": 0.0,
}


# ---------------------------------------------------------------------------
# Low-level sensor readout (synchronous — fast enough for non-blocking use)
# ---------------------------------------------------------------------------

def _read_hwmon(label_path: str, value_path: str) -> float:
    """
    Generic hwmon reader for CPU k10temp fallback when python-libsensors
    is not available.
    """
    try:
        for base in Path("/sys/class/hwmon").iterdir():
            name_file = base / "name"
            if name_file.exists() and name_file.read_text().strip() == label_path:
                val = (base / value_path).read_text().strip()
                return float(val) / 1000.0
    except Exception:
        pass
    return 0.0


def get_cpu_temp() -> float:
    """
    Return CPU temperature in °C via k10temp (python-libsensors), or
    0.0 on failure.  Falls back to hwmon sysfs scraping.
    """
    try:
        import sensors  # type: ignore  # python-libsensors
        for chip in sensors.ChipIterator():
            if "k10temp" in str(chip):
                for feature in chip:
                    if "Tctl" in str(feature.name):
                        return float(feature.get_value())
    except ImportError:
        pass
    # Fallback to hwmon scraping
    return _read_hwmon("k10temp", "temp1_input")


def _read_amdgpu_hwmon() -> dict:
    """
    Read AMD GPU temperatures from the ``amdgpu`` hwmon sysfs interface.

    Scans ``/sys/class/hwmon/`` for devices with ``name == "amdgpu"``
    that expose labeled temperature files (``temp_label``).  Reads
    edge, junction, and VRAM (mem) values, which are in millidegrees
    Celsius and divided by 1000.

    Returns
    -------
    dict
        ``{"edge": float, "junction": float, "vram": float}``.
        Missing or unreadable sensors default to 0.0.
    """
    result: dict = {"edge": 0.0, "junction": 0.0, "vram": 0.0}
    try:
        for base in Path("/sys/class/hwmon").iterdir():
            name_file = base / "name"
            if not name_file.exists():
                continue
            if name_file.read_text().strip() != "amdgpu":
                continue
            # Build a label→temp_index map from temp*_label files
            label_map: dict[str, int] = {}
            for label_path in sorted(base.glob("temp*_label")):
                label = label_path.read_text().strip()
                # Extract the numeric index from "tempN_label"
                idx = int(label_path.name.split("_")[0][4:])
                label_map[label] = idx
            # Map known labels to their temperature input files
            for label, key in (("edge", "edge"), ("junction", "junction"), ("mem", "vram")):
                idx = label_map.get(label)
                if idx is not None:
                    temp_path = base / f"temp{idx}_input"
                    if temp_path.exists():
                        result[key] = float(temp_path.read_text().strip()) / 1000.0
            break  # Use the first amdgpu device with labeled temps
    except Exception:
        logger.debug("amdgpu hwmon read failed", exc_info=True)
    return result


def get_gpu_temps() -> dict:
    """
    Return dict with keys: edge, junction, vram, vram_used_gb (all floats).

    Uses pyrsmi (ROCm) for direct AMD GPU telemetry when available.
    Falls back to the ``amdgpu`` hwmon sysfs interface (always present
    with the amdgpu kernel driver) when pyrsmi is unavailable.
    Returns zeros if both sources fail.
    """
    result: dict = {"edge": 0.0, "junction": 0.0, "vram": 0.0, "vram_used_gb": 0.0}

    if PYRSMI_AVAILABLE:
        try:
            device = 0  # Primary GPU
            result["edge"] = (
                rocUtil.getTempInformation(device, rocUtil.RocmTempType.EDGE) / 1000.0
            )
            result["junction"] = (
                rocUtil.getTempInformation(device, rocUtil.RocmTempType.JUNCTION) / 1000.0
            )
            result["vram"] = (
                rocUtil.getTempInformation(device, rocUtil.RocmTempType.VRAM) / 1000.0
            )
            # VRAM usage in GiB (rocm-smi returns bytes)
            vram_bytes = rocUtil.getVRAMUsage(device)
            result["vram_used_gb"] = vram_bytes / (1024**3)
            return result
        except Exception:
            logger.exception("pyrsmi readout failed — falling back to hwmon")

    # Fallback: read amdgpu hwmon sysfs (edge, junction, VRAM temps only)
    hwmon_temps = _read_amdgpu_hwmon()
    result.update(hwmon_temps)
    return result


def get_ram_usage_percent() -> float:
    """Return system RAM usage as a percentage (0-100) via psutil."""
    try:
        import psutil
        return psutil.virtual_memory().percent
    except ImportError:
        return 0.0


# ---------------------------------------------------------------------------
# Async VRAM queries (subprocess to rocm-smi)
# ---------------------------------------------------------------------------

async def get_total_vram_mb() -> int:
    """
    Query total VRAM capacity from ROCm via ``rocm-smi``.

    Returns the total VRAM in MiB, or the fallback value (12800 MB for
    a 12 GB card) on failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "rocm-smi", "--showmeminfo", "vram", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            logger.error(
                "rocm-smi failed with return code %d: %s",
                proc.returncode,
                stderr.decode() if stderr else "",
            )
            return VRAM_DEFAULT_TOTAL_MB

        data = json.loads(stdout.decode())
        for card, info in data.items():
            if "card" in card:
                total_bytes = int(info.get("VRAM Total Memory (B)", 12884901888))
                return total_bytes // (1024 * 1024)
    except FileNotFoundError:
        logger.error("rocm-smi not found — install ROCm tools to detect VRAM")
    except asyncio.TimeoutError:
        logger.error("rocm-smi command timed out")
    except Exception:
        logger.exception("Failed to get total VRAM")
    return VRAM_DEFAULT_TOTAL_MB


async def get_free_vram_mb() -> int:
    """
    Query free VRAM from ROCm via ``rocm-smi``.

    Returns free VRAM in MiB, or 0 on failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "rocm-smi", "--showmeminfo", "vram", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            logger.error(
                "rocm-smi failed with return code %d: %s",
                proc.returncode,
                stderr.decode() if stderr else "",
            )
            return 0

        data = json.loads(stdout.decode())
        for card, info in data.items():
            if "card" in card:
                total_b = int(info.get("VRAM Total Memory (B)", 12884901888))
                used_b = int(info.get("VRAM Total Used Memory (B)", 0))
                return (total_b - used_b) // (1024 * 1024)
    except FileNotFoundError:
        logger.error("rocm-smi not found")
    except asyncio.TimeoutError:
        logger.error("rocm-smi command timed out")
    except Exception:
        logger.exception("Failed to get free VRAM")
    return 0


async def verify_vram_availability(required_mb: int = 8000) -> None:
    """
    Block until at least *required_mb* of VRAM is free.
    Sends notifications if the pipeline is paused.

    If the GPU is completely free (free >= total - 100MB), return
    immediately without waiting.
    """
    alerted = False
    free_vram = await get_free_vram_mb()
    total_vram = await get_total_vram_mb()

    logger.info(
        "VRAM check: free=%dMB, required=%dMB, total=%dMB",
        free_vram, required_mb, total_vram,
    )

    # If GPU is essentially free, proceed immediately
    if free_vram >= total_vram - 100:
        logger.info("GPU appears to be free, skipping VRAM wait")
        return

    while await get_free_vram_mb() < required_mb:
        await send_wayland_notification("Pipeline Paused", "Waiting for VRAM.")

        if not alerted:
            alert_msg = (
                f"Pipeline paused. Waiting for at least {required_mb}MB "
                "of free VRAM to continue."
            )
            await asyncio.gather(
                send_telegram_alert("⚠️ AI Proxy: Waiting for VRAM", alert_msg),
                send_bash_notification("⚠️ AI Proxy: Waiting for VRAM", alert_msg),
            )
            alerted = True

        await asyncio.sleep(30)


async def calculate_dynamic_ngl(
    target_service: str,
    is_headless: Optional[bool] = None,
) -> None:
    """
    Calculate and write the FIT_TARGET environment variable used by
    llama.cpp for --fit offloading.

    - Headless: minimal buffer (256MB) since no additional GPU workload
    - Graphical: larger buffer (1024MB) to handle compositor/user actions

    The result is written to ``~/kinver-hub/.env.ngl``.
    """
    from constants import ENV_NGL_FILE

    if is_headless is None:
        is_headless = is_system_headless()

    # fit-target is the MB buffer to leave free when --fit calculates offloading
    fit_target: int = 256 if is_headless else 1024

    try:
        with open(ENV_NGL_FILE, "w") as f:
            f.write(f"FIT_TARGET={fit_target}\n")
        logger.info("FIT_TARGET set to %dMB (headless=%s)", fit_target, is_headless)
    except OSError:
        logger.exception("Failed to write FIT_TARGET to %s", ENV_NGL_FILE)


# ---------------------------------------------------------------------------
# System headless detection (merged from warden.py)
# ---------------------------------------------------------------------------

def is_system_headless() -> bool:
    """
    Detect whether the system is running a graphical session (KDE Wayland,
    GNOME, Sway, Xorg).  Returns True if NO graphical process is found
    (i.e. safe to pin VRAM aggressively).

    Checks for known graphical compositors via pgrep.
    """
    graphical_processes = [
        "gnome-shell", "kwin_wayland", "sway", "Xwayland", "Xorg",
    ]
    for proc in graphical_processes:
        try:
            result = subprocess.run(
                ["pgrep", "-x", proc],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                logger.debug("Graphical process '%s' detected — system is NOT headless", proc)
                return False
        except Exception:
            pass

    logger.debug("No graphical process detected — system is headless")
    return True


# ---------------------------------------------------------------------------
# GPU workload isolation (merged from warden.py — now async)
# ---------------------------------------------------------------------------

# Shared environment file written by arm_gpu_for_inference for llama.cpp
SHARED_ENV_FILE: str = "~/kinver-hub/gpu_state.env"


async def arm_gpu_for_inference(
    target_service: str,
    target_port: int,
    systemd=None,  # Optional SystemdController (avoids circular import)
) -> None:
    """
    Prepare the physical GPU for optimal inference by:
    1. Pinning VRAM clocks via LACT (Headless_Pinned profile)
    2. Injecting backend-specific environment variables (ROCm / Vulkan)
    3. Restarting the target model service
    4. Waiting for port readiness

    Aborts silently if a graphical session (Wayland/Xorg) is active to
    prevent display crashes.

    Parameters
    ----------
    target_service : str
        The systemd service name (e.g. ``"llama-professional"``).
    target_port : int
        The TCP port the model will listen on.
    systemd : SystemdController or None
        If provided, used for restart + health probe.  If None, a
        temporary controller is created.
    """
    # Abort if Wayland/Xorg is active — pinning VRAM would crash the display
    if not is_system_headless():
        logger.info("Graphical session active — skipping GPU arm (safe mode)")
        return

    # Detect backend (ROCm vs Vulkan) from the service unit file
    backend = await _detect_backend(target_service)
    logger.info("Arming GPU for %s via %s", target_service, backend.upper())

    try:
        # 1. Lock VRAM clocks via LACT CLI
        proc = await asyncio.create_subprocess_exec(
            "sudo", "lact", "cli", "profile", "set", "Headless_Pinned",
        )
        await proc.communicate()
        if proc.returncode != 0:
            logger.warning("LACT profile set returned non-zero: %d", proc.returncode)
        else:
            logger.info("VRAM pinned via LACT (Headless_Pinned profile)")

        # Brief delay for clocks to stabilise
        await asyncio.sleep(0.2)

        # 2. Inject optimal backend environment variables
        os.makedirs(os.path.dirname(SHARED_ENV_FILE), exist_ok=True)
        if backend == "rocm":
            env_config = "PAL_ALWAYS_RESIDENT=1\nHSA_ENABLE_SDMA=0\n"
        elif backend == "vulkan":
            env_config = (
                "VK_ICD_FILENAMES="
                "/usr/share/vulkan/icd.d/radeon_icd.x86_64.json\n"
            )
        else:
            env_config = ""

        if env_config:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "tee", SHARED_ENV_FILE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate(input=env_config.encode())
            logger.info("Backend env written to %s", SHARED_ENV_FILE)

        # 3. Restart the target service and wait for readiness
        if systemd is not None:
            await systemd.restart_service(
                target_service.replace("llama-", "")
            )
            await systemd.wait_for_ready(target_port)
        else:
            # Fallback: direct restart
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "restart", target_service,
            )
            await proc.communicate()
            # Wait for port with a simple health check
            await _wait_for_port(target_port)

    except Exception as exc:
        logger.error("Failed to arm GPU: %s", exc)


async def _detect_backend(target_service: str) -> str:
    """
    Read a systemd unit file to determine whether it uses Vulkan or ROCm.
    Defaults to ``"rocm"`` if the file cannot be read.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "cat", target_service,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            content = stdout.decode().lower()
            if "vulkan" in content:
                return "vulkan"
    except Exception:
        pass
    return "rocm"


async def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    """
    Simple TCP connect loop to wait for a port to become available.
    Used as a fallback when SystemdController is not provided.
    """
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=1.0,
            )
            writer.close()
            await writer.wait_closed()
            logger.info("Port %d is active", port)
            return
        except Exception:
            await asyncio.sleep(0.5)
    logger.warning("Port %d did not become ready within %.1fs", port, timeout)


# ---------------------------------------------------------------------------
# Low-level predictive cooling IPC write
#   Called by cooling.CoolingStateMachine — this is the filesystem side.
# ---------------------------------------------------------------------------

def write_cooling_ipc(cpu_temp: Optional[int] = None, gpu_temp: Optional[int] = None) -> None:
    """
    Write integer millidegree-Celsius values to the IPC files monitored
    by the Cooler Control daemon.

    Pass ``None`` to leave a file untouched (only the active hardware
    path receives writes).

    Parameters
    ----------
    cpu_temp : int or None
        Value for ``/tmp/cpu_temp.txt``.
    gpu_temp : int or None
        Value for ``/tmp/gpu_temp.txt``.
    """
    try:
        if cpu_temp is not None:
            CPU_TEMP_PATH.write_text(str(cpu_temp))
        if gpu_temp is not None:
            GPU_TEMP_PATH.write_text(str(gpu_temp))
    except OSError:
        logger.exception("Failed to write predictive cooling IPC files")


# ---------------------------------------------------------------------------
# Notifications (Wayland desktop, Telegram, Bash custom script)
# ---------------------------------------------------------------------------

async def send_wayland_notification(title: str, message: str) -> None:
    """
    Send a native desktop notification via libnotify (``notify-send``).
    Requires ``DBUS_SESSION_BUS_ADDRESS`` to be set for the user session.
    """
    try:
        env = os.environ.copy()
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
        proc = await asyncio.create_subprocess_exec(
            "notify-send", title, message,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
    except Exception:
        pass  # Notification failure is non-critical


async def send_telegram_alert(title: str, message: str) -> None:
    """
    Send an alert via the Telegram Bot API.  Requires
    ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` in the environment.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    full_message = f"<b>{title}</b>\n\n{message}"

    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": full_message,
                    "parse_mode": "HTML",
                },
                timeout=5.0,
            )
        except Exception as exc:
            logger.error("Failed to send Telegram alert: %s", exc)


async def send_bash_notification(title: str, message: str) -> None:
    """
    Execute the custom ``ar-notify.sh`` bash notification script.
    The script is sourced and the ``notify_phone`` function is called.
    """
    bash_file = "~/ar-notify.sh"
    async_function = "notify_phone"

    try:
        command = (
            f"bash -c \"source {bash_file} && "
            f"{async_function} 'ubuntu=:=active=:=green=:={title}:{message}'\""
        )
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
    except Exception as exc:
        logger.error("Failed to trigger bash notification: %s", exc)


# ---------------------------------------------------------------------------
# Thermal monitor background task
# ---------------------------------------------------------------------------

async def thermal_monitor_task(
    state: Dict[str, float],
    interval: float = SENSOR_INTERVAL,
) -> None:
    """
    Async background loop that:

    1. Reads all thermal sensors every *interval* seconds.
    2. Updates the shared *state* dict (read by proxy for health checks).
    3. Updates Prometheus metrics.
    4. Triggers emergency system shutdown if any zone exceeds its
       critical limit.

    Runs forever until cancelled.  Designed to be spawned as an
    ``asyncio.Task`` during proxy startup.

    Parameters
    ----------
    state : dict
        Shared thermal state dict — mutated in-place each iteration.
    interval : float
        Seconds between sensor polls (default from constants.SENSOR_INTERVAL).
    """
    cooldown_until: Dict[str, float] = {}  # per-zone cooldown after crit warning

    logger.info(
        "Thermal monitor started (interval=%.1fs, limits=%s)",
        interval,
        {k: v["crit"] for k, v in THERMAL_LIMITS.items()},
    )

    while True:
        try:
            # ---- Read sensors ------------------------------------------------
            cpu = get_cpu_temp()
            gpu = get_gpu_temps()
            ram = get_ram_usage_percent()

            # ---- Update shared state -----------------------------------------
            state["cpu"] = cpu
            state["gpu_edge"] = gpu["edge"]
            state["gpu_junction"] = gpu["junction"]
            state["gpu_vram"] = gpu["vram"]
            state["gpu_vram_used_gb"] = gpu["vram_used_gb"]
            state["ram_used_percent"] = ram

            # ---- Update Prometheus metrics -----------------------------------
            metric_cpu_temp.set(cpu)
            metric_gpu_edge_temp.set(gpu["edge"])
            metric_gpu_junc_temp.set(gpu["junction"])
            metric_gpu_vram_temp.set(gpu["vram"])
            metric_gpu_used_vram_gb.set(gpu["vram_used_gb"])
            metric_ram_used_pct.set(ram)

            # ---- Thermal threshold enforcement -------------------------------
            temps = {
                "k10temp": cpu,
                "amdgpu_core": gpu["edge"],
                "amdgpu_vram": gpu["vram"],
                "spd5118": ram,  # RAM % as approximation (no SPD temp sensors)
            }

            for zone, current in temps.items():
                limits = THERMAL_LIMITS.get(zone)
                if limits is None:
                    continue

                # Check cooldown (suppress repeated warnings for same zone)
                now = time.monotonic()
                if zone in cooldown_until and now < cooldown_until[zone]:
                    continue

                # Warning threshold
                if current >= limits["warn"]:
                    logger.warning(
                        "%s temp %.1f °C >= warn %.1f °C",
                        limits["name"], current, limits["warn"],
                    )

                # Critical threshold → EMERGENCY SHUTDOWN
                if current >= limits["crit"]:
                    logger.critical(
                        "🔥 %s reached %.1f °C (limit %.1f °C) — "
                        "initiating EMERGENCY SHUTDOWN",
                        limits["name"], current, limits["crit"],
                    )
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "systemctl", "poweroff", "-i",
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await asyncio.wait_for(proc.communicate(), timeout=10.0)
                    except Exception:
                        logger.exception("Emergency shutdown failed")
                    cooldown_until[zone] = now + THERMAL_COOLDOWN

        except Exception:
            logger.exception("Thermal monitor iteration failed")

        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# HardwareGovernor convenience class (wraps module-level functions)
# ---------------------------------------------------------------------------

class HardwareGovernor:
    """
    Convenience wrapper around the module-level hardware functions.

    Provides a single object that can be passed to routing / proxy code,
    encapsulating GPU workload decisions (headless check, VRAM pinning,
    occupancy tracking).

    Usage::

        hw = HardwareGovernor()
        if hw.is_headless():
            await hw.arm_gpu("professional", 8084)
    """

    # Instance-level state
    _active_heavy_model: Optional[str] = None

    @staticmethod
    def is_headless() -> bool:
        """Check if the system is running headless (no graphical session)."""
        return is_system_headless()

    async def arm_gpu(self, target_service: str, target_port: int) -> None:
        """
        Arm the GPU for inference and track the active heavy model.
        Aborts safely if a graphical session is active.
        """
        await arm_gpu_for_inference(target_service, target_port)
        self._active_heavy_model = target_service

    def is_gpu_occupied(self) -> bool:
        """
        Return True if a heavy GPU model is currently tracked as active.

        Heavy models are: professional, coder, creative, scholar, architect.
        Lightweight models (worker, chatter, frontdesk, lifeboat, reasoning)
        are NOT considered "heavy."
        """
        heavy_models = {"professional", "coder", "creative", "scholar", "architect"}
        # Strip "llama-" prefix if present
        active = self._active_heavy_model
        if active and active.startswith("llama-"):
            active = active.replace("llama-", "")
        return active is not None and active in heavy_models

    @property
    def active_heavy_model(self) -> Optional[str]:
        """Return the domain name of the currently active heavy GPU model."""
        return self._active_heavy_model

    @active_heavy_model.setter
    def active_heavy_model(self, value: Optional[str]) -> None:
        """Set the active heavy model (called by proxy during routing)."""
        self._active_heavy_model = value