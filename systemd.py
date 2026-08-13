"""
systemd.py - Systemd service controller for Kinver Hub model lifecycle.

Provides a unified async abstraction over ``systemctl start/stop/restart``
operations for llama.cpp model services.  Replaces the direct
``asyncio.create_subprocess_exec`` calls previously scattered throughout
proxy.py.

Key responsibilities:
- Service lifecycle: start, stop, restart, is_active
- Port resolution: parse ``--port`` from service unit files
- Model discovery: enumerate all ``llama-*.service`` units
- Hot-swap: orchestrate a stop→start sequence with VRAM release delay
- Health probing: poll ``/health`` endpoint until a model is ready

All methods are ``async`` and designed to run under uvloop without
blocking the event loop.

Usage::

    systemd = SystemdController()
    port = systemd.get_port("professional")
    await systemd.hot_swap("creative", "professional")

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Optional

import httpx
import aiofiles
import aiofiles.os as aio_os

from constants import (
    SYSTEMD_DIR,
    SERVICE_PATTERN,
    PROFESSIONAL_RESIDENT_ENABLED,
    GPU_BUSY_VRAM_GB,
    get_logger,
)

logger = get_logger("proxy.systemd")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEALTH_CHECK_INTERVAL: float = 0.5  # seconds between /health polls
DEFAULT_HEALTH_TIMEOUT: float = 120.0  # seconds before giving up on a model
VRAM_RELEASE_DELAY: float = 2.0  # seconds to let VRAM free between swaps
# Liveness probe timeout (seconds) for the cheap TCP port check used to
# verify recorded heavy-model state before trusting it (2026-08-12 incident:
# an external ``systemctl isolate`` stopped llama-professional while the proxy
# still recorded it as active).
PROBE_TIMEOUT: float = 1.0
# Absolute systemctl path — PATH fragility in the proxy's environment produced
# "systemctl missing" failures (2026-08-12); model management must never
# silently break because systemctl cannot be resolved.
SYSTEMCTL: str = "/usr/bin/systemctl"


# ---------------------------------------------------------------------------
# SystemdController
# ---------------------------------------------------------------------------

class SystemdController:
    """
    Async wrapper around systemctl for managing llama.cpp model services.

    Maintains an in-memory port cache so that service unit files are only
    parsed once per domain during the proxy's lifetime.

    Parameters
    ----------
    systemd_dir : str
        Path to the systemd unit directory (defaults to ``/etc/systemd/system/``).
    """

    def __init__(self, systemd_dir: str = SYSTEMD_DIR) -> None:
        self._systemd_dir: str = systemd_dir
        # port cache: domain_name → port_number
        self._port_cache: dict[str, int] = {}
        # Track which GPU-heavy models are currently active
        self._active_heavy_model: Optional[str] = None

    # ------------------------------------------------------------------
    # Service lifecycle operations
    # ------------------------------------------------------------------

    async def start_service(self, domain: str) -> None:
        """
        Start a llama-<domain> service via systemctl.

        Parameters
        ----------
        domain : str
            The service domain name (e.g. ``"professional"``, ``"coder"``).
        """
        service_name = f"llama-{domain}"
        logger.info("Starting service: %s", service_name)
        proc = await asyncio.create_subprocess_exec(
            SYSTEMCTL, "start", service_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_text = stderr.decode().strip() if stderr else "unknown error"
            logger.error("Failed to start %s: %s", service_name, err_text)
            raise RuntimeError(f"systemctl start {service_name} failed: {err_text}")

    async def stop_service(self, domain: str) -> None:
        """
        Stop a llama-<domain> service via systemctl.

        This is non-fatal — failures are logged but not raised, since the
        service may already be stopped.
        """
        service_name = f"llama-{domain}"
        logger.info("Stopping service: %s", service_name)
        proc = await asyncio.create_subprocess_exec(
            SYSTEMCTL, "stop", service_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "Failed to stop %s (may already be stopped): %s",
                service_name,
                stderr.decode().strip() if stderr else "unknown",
            )

    async def restart_service(self, domain: str) -> None:
        """
        Restart a llama-<domain> service via systemctl.
        """
        service_name = f"llama-{domain}"
        logger.info("Restarting service: %s", service_name)
        proc = await asyncio.create_subprocess_exec(
            SYSTEMCTL, "restart", service_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_text = stderr.decode().strip() if stderr else "unknown error"
            logger.error("Failed to restart %s: %s", service_name, err_text)
            raise RuntimeError(f"systemctl restart {service_name} failed: {err_text}")

    async def is_active(self, domain: str) -> bool:
        """
        Return True if the llama-<domain> service is currently active.
        """
        service_name = f"llama-{domain}"
        proc = await asyncio.create_subprocess_exec(
            SYSTEMCTL, "is-active", "--quiet", service_name,
        )
        await proc.communicate()
        return proc.returncode == 0

    # ------------------------------------------------------------------
    # Port resolution
    # ------------------------------------------------------------------

    async def get_port(self, domain: str) -> int:
        """
        Resolve the TCP port for a llama-<domain> service by parsing
        its systemd unit file for ``--port``.

        Results are cached in-memory after the first lookup.

        Parameters
        ----------
        domain : str
            Service domain name (e.g. ``"chatter"``, ``"professional"``).

        Returns
        -------
        int
            The resolved port number.  Falls back to 13109 (professional default)
            if the file cannot be read or parsed.
        """
        domain = domain.lower().strip()

        # Check cache first
        if domain in self._port_cache:
            return self._port_cache[domain]

        service_name = f"llama-{domain}"
        unit_path = os.path.join(self._systemd_dir, f"{service_name}.service")

        try:
            async with aiofiles.open(unit_path, "r") as f:
                content = await f.read()
            match = re.search(r"--port\s+(\d+)", content)
            if match:
                port = int(match.group(1))
                self._port_cache[domain] = port
                logger.debug("Resolved port %d for domain '%s'", port, domain)
                return port
        except FileNotFoundError:
            logger.debug(
                "Unit file %s not found for domain '%s'", unit_path, domain
            )
        except (OSError, ValueError):
            logger.exception("Failed to read unit file for domain '%s'", domain)

        # Fallback: if not found, try the professional port as default
        if domain != "professional":
            logger.debug("Falling back to professional port for domain '%s'", domain)
            return await self.get_port("professional")

        # Ultimate fallback
        port = 13109
        self._port_cache[domain] = port
        return port

    # ------------------------------------------------------------------
    # Health probing
    # ------------------------------------------------------------------

    async def wait_for_ready(
        self,
        port: int,
        timeout: float = DEFAULT_HEALTH_TIMEOUT,
    ) -> bool:
        """
        Poll ``http://127.0.0.1:{port}/health`` until the model responds
        with HTTP 200, or the timeout expires.

        Parameters
        ----------
        port : int
            TCP port of the llama.cpp server.
        timeout : float
            Maximum seconds to wait (default 120s).

        Returns
        -------
        bool
            True if the endpoint became healthy, False on timeout.
        """
        url = f"http://127.0.0.1:{port}/health"
        deadline = asyncio.get_running_loop().time() + timeout

        logger.info("Waiting for port %d readiness (timeout=%.1fs)...", port, timeout)

        async with httpx.AsyncClient() as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    resp = await client.get(url, timeout=httpx.Timeout(1.0))
                    if resp.status_code == 200:
                        logger.info("Port %d is ready", port)
                        return True
                except httpx.HTTPError:
                    pass  # Not ready yet — retry after interval
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)

        logger.warning("Port %d did not become ready within %.1fs", port, timeout)
        return False

    async def probe_model_port(
        self,
        port: int,
        timeout: float = PROBE_TIMEOUT,
    ) -> bool:
        """
        Cheap TCP liveness check for a model port.

        Verifies that something is actually listening on ``127.0.0.1:port``
        with a short connect timeout (~1s).  Used to validate the RECORDED
        heavy-model state before trusting it: an external stop (e.g. an
        ``unlock.sh`` ``systemctl isolate``) kills the service while the
        proxy still believes it is loaded — without the probe, residency
        and hot-swap decisions skip on stale state and the service stays
        dead (2026-08-12 incident, 1.5h of non-restart).

        Never raises: connection refused / timeout / any OSError means the
        port is dead and the caller treats the model as not serving.

        Parameters
        ----------
        port : int
            TCP port to probe.
        timeout : float
            Maximum seconds to wait for the connect (default 1s).

        Returns
        -------
        bool
            True if the port accepts a TCP connection.
        """
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=timeout,
            )
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    # ------------------------------------------------------------------
    # Hot-swap orchestration
    # ------------------------------------------------------------------

    async def hot_swap(self, from_domain: str, to_domain: str) -> int:
        """
        Orchestrate a GPU model swap: stop *from_domain*, wait for VRAM
        release, start *to_domain*, wait for readiness.

        Parameters
        ----------
        from_domain : str
            The currently active model domain to stop.
            Can be ``""`` or ``None`` if no model is currently loaded.
        to_domain : str
            The target model domain to start.

        Returns
        -------
        int
            The TCP port of the newly started model.

        Raises
        ------
        RuntimeError
            If the target model fails to become ready within the timeout.
        """
        if from_domain and from_domain != to_domain:
            logger.info("Hot-swapping: %s → %s", from_domain, to_domain)
            await self.stop_service(from_domain)
            # Allow GPU VRAM to be released before starting the new model
            await asyncio.sleep(VRAM_RELEASE_DELAY)
        else:
            logger.info("Starting model: %s (no prior model to stop)", to_domain)

        await self.start_service(to_domain)
        port = await self.get_port(to_domain)

        ready = await self.wait_for_ready(port)
        if not ready:
            raise RuntimeError(
                f"Model '{to_domain}' failed to become ready on port {port}"
            )

        self._active_heavy_model = to_domain
        logger.info("Model '%s' is ready on port %d", to_domain, port)
        return port

    async def unload_all_heavy(self) -> None:
        """
        Stop the currently active heavy GPU model (if any) and restart
        the default professional service.
        """
        if self._active_heavy_model:
            logger.info("Unloading heavy model: %s", self._active_heavy_model)
            await self.stop_service(self._active_heavy_model)
            self._active_heavy_model = None

        # Ensure professional is running as the default model
        await self.start_service("professional")
        professional_port = await self.get_port("professional")
        logger.info("Professional service started on port %d", professional_port)

    # ------------------------------------------------------------------
    # Professional residency (keep-warm)
    # ------------------------------------------------------------------

    async def ensure_professional_resident(self, gpu_vram_used_gb: float) -> None:
        """
        Keep the professional model loaded whenever the GPU is free.

        Called periodically by the thermal monitor.  No-op unless the feature
        is enabled, no other heavy model is active, and the GPU is not busy
        with non-model work (gaming/rendering — inferred from VRAM usage at or
        above ``GPU_BUSY_VRAM_GB``).  Never raises: systemctl failures are
        logged as warnings and left for the next check.

        Parameters
        ----------
        gpu_vram_used_gb : float
            Current GPU VRAM usage in GiB (from the thermal monitor state).
        """
        if not PROFESSIONAL_RESIDENT_ENABLED:
            return
        if self._active_heavy_model is not None:
            # Recorded state may be STALE: an external stop (e.g. an
            # unlock.sh ``systemctl isolate``) kills the service while the
            # proxy still believes it is loaded (2026-08-12 incident — 1.5h
            # of non-restart).  Probe the recorded model's port: a live port
            # means a heavy model genuinely owns the GPU (routing owns it),
            # a dead port means the record is stale — clear it and proceed
            # to (re)start professional.  The probe never raises.
            try:
                recorded_port = await self.get_port(self._active_heavy_model)
                if await self.probe_model_port(recorded_port):
                    return  # a heavy model is genuinely serving
            except (OSError, RuntimeError):
                logger.debug(
                    "Failed to probe recorded model '%s' — assuming stale",
                    self._active_heavy_model,
                )
            logger.warning(
                "Cleared stale active-heavy-model state '%s' "
                "(port %s not serving)",
                self._active_heavy_model, recorded_port,
            )
            self._active_heavy_model = None
        if gpu_vram_used_gb >= GPU_BUSY_VRAM_GB:
            return  # GPU busy with other work (gaming/rendering) — don't fight it

        try:
            if await self.is_active("professional"):
                return  # already loaded
            await self.start_service("professional")
        except (OSError, RuntimeError) as exc:
            logger.warning("Failed to keep professional resident: %s", exc)
            return

        self._active_heavy_model = "professional"
        logger.info("Professional model loaded resident (GPU free)")

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    async def scan_models(self) -> list[dict[str, Any]]:
        """
        Enumerate all llama-<name>.service units and return them as an
        OpenAI-compatible ``/v1/models`` list.

        Each returned dict has keys: ``id``, ``object``, ``owned_by``, ``port``.
        """
        discovered: list[dict[str, Any]] = []

        try:
            entries = await aio_os.listdir(self._systemd_dir)
        except FileNotFoundError:
            logger.warning("Systemd directory %s not found", self._systemd_dir)
            return discovered
        except PermissionError:
            logger.warning("Permission denied reading %s", self._systemd_dir)
            return discovered

        for filename in entries:
            match = SERVICE_PATTERN.match(filename)
            if not match:
                continue

            model_name = match.group(1)
            unit_path = os.path.join(self._systemd_dir, filename)

            try:
                async with aiofiles.open(unit_path, "r") as f:
                    content = await f.read()
                port_match = re.search(r"--port\s+(\d+)", content)
                if port_match:
                    discovered.append({
                        "id": model_name,
                        "object": "model",
                        "owned_by": "systemd",
                        "port": int(port_match.group(1)),
                    })
            except (OSError, ValueError):
                logger.debug("Skipping unreadable unit file: %s", filename)

        logger.debug("Discovered %d models from systemd", len(discovered))
        return discovered

    # ------------------------------------------------------------------
    # Active model tracking
    # ------------------------------------------------------------------

    @property
    def active_heavy_model(self) -> Optional[str]:
        """Return the domain name of the currently active heavy GPU model."""
        return self._active_heavy_model

    @active_heavy_model.setter
    def active_heavy_model(self, value: Optional[str]) -> None:
        """Set the active heavy model (called by proxy during routing)."""
        self._active_heavy_model = value

    def is_gpu_occupied(self) -> bool:
        """
        Return True if a heavy GPU model is currently loaded.

        Heavy models are: professional, coder, creative, scholar, architect.
        Lightweight models (chatter, frontdesk)
        are NOT considered "heavy" and can coexist or be quickly swapped.
        Note: professional may be loaded but resident-IDLE (kept warm by
        ``ensure_professional_resident`` when the GPU is free) — this flag
        reports the loaded state, not the busy state.
        """
        heavy_models = {"professional", "coder", "creative", "scholar", "architect"}
        return (
            self._active_heavy_model is not None
            and self._active_heavy_model in heavy_models
        )