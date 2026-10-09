"""Regression tests for SystemdController liveness probing and systemctl hardening.

2026-08-12 incident: an external script (``systemctl isolate
graphical.target`` from unlock.sh) stopped llama-professional while the
proxy still recorded it as the active heavy model.  ``ensure_professional_resident``
returned early on that stale state and the queue worker's hot-swap skipped,
leaving the service dead for 1.5 hours.  PATH fragility had also produced
"systemctl missing" failures earlier the same day.

These tests cover:
- ``probe_model_port``: the cheap TCP liveness check used to verify recorded state
- absolute ``/usr/bin/systemctl`` in every subprocess invocation
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from typing import Any
from unittest.mock import AsyncMock

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


class _FakeProc:
    """Minimal stand-in for the asyncio subprocess handle."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def communicate(self) -> tuple[None, None]:
        return None, None


class _FakeWriter:
    """Minimal asyncio StreamWriter stand-in (sync close, async wait_closed)."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class TestSystemctlAbsolutePath:
    """Every systemctl invocation uses ``/usr/bin/systemctl`` so PATH
    issues cannot silently break model management."""

    @pytest.mark.asyncio
    async def test_start_service_uses_absolute_systemctl(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh, ctrl = systemd
        calls: list[tuple[Any, ...]] = []

        async def _exec(*args: Any, **kwargs: Any) -> _FakeProc:
            calls.append(args)
            return _FakeProc()

        monkeypatch.setattr(fresh.asyncio, "create_subprocess_exec", _exec)
        await ctrl.start_service("professional")
        assert calls[0] == ("/usr/bin/systemctl", "start", "llama-professional")

    @pytest.mark.asyncio
    async def test_stop_service_uses_absolute_systemctl(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh, ctrl = systemd
        calls: list[tuple[Any, ...]] = []

        async def _exec(*args: Any, **kwargs: Any) -> _FakeProc:
            calls.append(args)
            return _FakeProc()

        monkeypatch.setattr(fresh.asyncio, "create_subprocess_exec", _exec)
        await ctrl.stop_service("professional")
        assert calls[0] == ("/usr/bin/systemctl", "stop", "llama-professional")

    @pytest.mark.asyncio
    async def test_restart_service_uses_absolute_systemctl(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh, ctrl = systemd
        calls: list[tuple[Any, ...]] = []

        async def _exec(*args: Any, **kwargs: Any) -> _FakeProc:
            calls.append(args)
            return _FakeProc()

        monkeypatch.setattr(fresh.asyncio, "create_subprocess_exec", _exec)
        await ctrl.restart_service("professional")
        assert calls[0] == ("/usr/bin/systemctl", "restart", "llama-professional")

    @pytest.mark.asyncio
    async def test_is_active_uses_absolute_systemctl(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh, ctrl = systemd
        calls: list[tuple[Any, ...]] = []

        async def _exec(*args: Any, **kwargs: Any) -> _FakeProc:
            calls.append(args)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(fresh.asyncio, "create_subprocess_exec", _exec)
        assert await ctrl.is_active("professional") is True
        assert calls[0] == ("/usr/bin/systemctl", "is-active", "--quiet", "llama-professional")


class TestProbeModelPort:
    """The cheap TCP liveness probe used to verify recorded heavy-model state."""

    @pytest.mark.asyncio
    async def test_true_when_port_accepts_connection(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh, ctrl = systemd
        opened: list[tuple[str, int]] = []

        async def _open(host: str, port: int) -> tuple[Any, _FakeWriter]:
            opened.append((host, port))
            return object(), _FakeWriter()

        monkeypatch.setattr(fresh.asyncio, "open_connection", _open)
        assert await ctrl.probe_model_port(13109) is True
        assert opened == [("127.0.0.1", 13109)]

    @pytest.mark.asyncio
    async def test_false_when_connection_refused(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh, ctrl = systemd

        async def _refused(host: str, port: int) -> tuple[Any, Any]:
            raise ConnectionRefusedError("connection refused")

        monkeypatch.setattr(fresh.asyncio, "open_connection", _refused)
        assert await ctrl.probe_model_port(13109) is False

    @pytest.mark.asyncio
    async def test_false_on_timeout(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh, ctrl = systemd

        async def _hang(host: str, port: int) -> tuple[Any, Any]:
            await asyncio.sleep(5)

        monkeypatch.setattr(fresh.asyncio, "open_connection", _hang)
        assert await ctrl.probe_model_port(13109, timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_never_raises_on_oserror(
        self, systemd: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh, ctrl = systemd

        async def _boom(host: str, port: int) -> tuple[Any, Any]:
            raise OSError("probe failed")

        monkeypatch.setattr(fresh.asyncio, "open_connection", _boom)
        assert await ctrl.probe_model_port(13109) is False
