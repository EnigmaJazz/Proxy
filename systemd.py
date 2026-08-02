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
    await systemd.hot_swap("worker", "professional")

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional, List, Dict

import httpx
import aiofiles
import aiofiles.os as aio_os

from constants import (
    SYSTEMD_DIR,
    SERVICE_PATTERN,
    TCP_TIMEOUT,
    get_logger,
)

logger = get_logger("proxy.systemd")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEALTH_CHECK_INTERVAL: float = 0.5  # seconds between /health polls
DEFAULT_HEALTH_TIMEOUT: float = 120.0  # seconds before giving up on a model
VRAM_RELEASE_DELAY: float = 2.0  # seconds to let VRAM free between swaps


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
        self._port_cache: Dict[str, int] = {}
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
            "systemctl", "start", service_name,
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
            "systemctl", "stop", service_name,
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
            "systemctl", "restart", service_name,
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
            "systemctl", "is-active", "--quiet", service_name,
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
            Service domain name (e.g. ``"worker"``, ``"chatter"``).

        Returns
        -------
        int
            The resolved port number.  Falls back to 13105 (worker default)
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
        except Exception:
            logger.exception("Failed to read unit file for domain '%s'", domain)

        # Fallback: if not found, try the worker port as default
        if domain != "worker":
            logger.debug("Falling back to worker port for domain '%s'", domain)
            return await self.get_port("worker")

        # Ultimate fallback
        port = 13105
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
        deadline = asyncio.get_event_loop().time() + timeout

        logger.info("Waiting for port %d readiness (timeout=%.1fs)...", port, timeout)

        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    resp = await client.get(url, timeout=httpx.Timeout(1.0))
                    if resp.status_code == 200:
                        logger.info("Port %d is ready", port)
                        return True
                except Exception:
                    pass  # Not ready yet — retry after interval
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)

        logger.warning("Port %d did not become ready within %.1fs", port, timeout)
        return False

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
        the default worker service.
        """
        if self._active_heavy_model:
            logger.info("Unloading heavy model: %s", self._active_heavy_model)
            await self.stop_service(self._active_heavy_model)
            self._active_heavy_model = None

        # Ensure worker is running as the default light model
        await self.start_service("worker")
        worker_port = await self.get_port("worker")
        logger.info("Worker service started on port %d", worker_port)

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    async def scan_models(self) -> list[dict]:
        """
        Enumerate all llama-<name>.service units and return them as an
        OpenAI-compatible ``/v1/models`` list.

        Each returned dict has keys: ``id``, ``object``, ``owned_by``, ``port``.
        """
        discovered: list[dict] = []

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
            except Exception:
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
        Return True if a heavy GPU model is currently active.

        Heavy models are: professional, coder, creative, scholar, architect.
        Lightweight models (worker, chatter, frontdesk)
        are NOT considered "heavy" and can coexist or be quickly swapped.
        """
        heavy_models = {"professional", "coder", "creative", "scholar", "architect"}
        return (
            self._active_heavy_model is not None
            and self._active_heavy_model in heavy_models
        )