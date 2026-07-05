"""Test harness for the Kinver AI Proxy.

Strategy B from the design doc: stub problematic third-party modules and
patch heavy dependency classes *before* ``import proxy``, then exercise the
real FastAPI app via ``httpx.ASGITransport`` with the lifespan disabled so
no real systemd/database/hardware/cooling init runs.
"""
from __future__ import annotations

import sys
import types
import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

# ---------------------------------------------------------------------------
# 1. Stub FlashRank before any import touches tools.py
# ---------------------------------------------------------------------------
_flashrank_stub = types.ModuleType("flashrank")
_flashrank_stub.Ranker = lambda *args, **kwargs: types.SimpleNamespace(
    rerank=lambda *args, **kwargs: []
)
_flashrank_stub.RerankRequest = object
sys.modules["flashrank"] = _flashrank_stub


# ---------------------------------------------------------------------------
# 2. No-op dependency classes (replace real classes before proxy imports them)
# ---------------------------------------------------------------------------
class _NoOpDatabase:
    """In-memory stand-in for the SQLite job queue."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_or_create_project(self, *args, **kwargs) -> str:
        return "general"

    async def enqueue_job(self, *args, **kwargs) -> str:
        return str(uuid.uuid4())

    async def complete_job(self, *args, **kwargs) -> None:
        pass

    async def fail_job(self, *args, **kwargs) -> None:
        pass

    async def update_partial_content(self, *args, **kwargs) -> None:
        pass

    async def record_stream_chunk(self, *args, **kwargs) -> None:
        pass

    async def purge_stream_chunks(self, *args, **kwargs) -> None:
        pass

    async def get_pending_jobs(self, *args, **kwargs) -> list:
        return []

    async def dequeue_next(self, *args, **kwargs) -> None:
        return None

    async def escalate_job(self, *args, **kwargs) -> None:
        pass


class _NoOpSystemd:
    """Fake systemd service manager that reports every model as ready."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    active_heavy_model: str | None = None

    async def scan_models(self) -> list:
        return []

    async def get_port(self, domain: str) -> int:
        return 13001

    async def is_active(self, domain: str) -> bool:
        return False

    async def stop_service(self, domain: str) -> None:
        pass

    async def hot_swap(self, *args, **kwargs) -> None:
        pass

    async def unload_all_heavy(self) -> None:
        pass

    def is_gpu_occupied(self) -> bool:
        return False


class _NoOpCooling:
    """No-op predictive cooling state machine for tests."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def prefill_burst(self, path: str) -> None:
        pass

    def generation_hold(self, path: str) -> None:
        pass

    def baseline_idle(self) -> None:
        pass

    @staticmethod
    def hardware_path_for_model(model: str) -> str:
        return "hybrid"


class _NoOpAuditor:
    """Disabled shadow auditor that never triggers a halt."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    @staticmethod
    def should_audit(*args, **kwargs) -> bool:
        return False

    def start(self, *args, **kwargs) -> None:
        pass

    def feed_chunk(self, *args, **kwargs) -> None:
        pass

    def stop(self) -> None:
        pass


# Patch the modules *before* proxy.py imports them.
import database  # noqa: E402
import systemd  # noqa: E402
import cooling  # noqa: E402
import auditing  # noqa: E402

database.Database = _NoOpDatabase  # type: ignore[misc]
systemd.SystemdController = _NoOpSystemd  # type: ignore[misc]
cooling.CoolingStateMachine = _NoOpCooling  # type: ignore[misc]
auditing.ShadowAuditor = _NoOpAuditor  # type: ignore[misc]

# ---------------------------------------------------------------------------
# 3. Now it is safe to import the real FastAPI app
# ---------------------------------------------------------------------------
import proxy  # noqa: E402


# ---------------------------------------------------------------------------
# 4. Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def app_client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield an httpx async client against the real app with lifespan off."""
    # Attach no-op component instances so routes can call them without crashing.
    proxy.app.state.database = _NoOpDatabase()
    proxy.app.state.systemd = _NoOpSystemd()
    proxy.app.state.cooler = _NoOpCooling()
    proxy.app.state.hardware = None
    proxy.app.state.auditor = _NoOpAuditor()
    proxy.app.state.active_heavy_model = None
    proxy.app.state.active_priority = 3
    proxy.app.state.requests_served = 0

    transport = httpx.ASGITransport(app=proxy.app, lifespan="off")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
