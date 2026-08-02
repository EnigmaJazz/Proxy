"""Regression tests for embedded CLI commands and frontdesk project flow.

Two real bugs are covered here:

1. ``/pause`` and ``/resume`` were dead code: the command regexes were
   ``^``-anchored but ``user_text`` is built from ``[role]: content``
   context lines (e.g. ``[user]: /pause 30``), so ``.match()`` never
   succeeded.  They now use ``.search()`` like the ``/cloud`` regex.

2. The frontdesk classifier emits ``project_name``, but two call sites
   read ``classification.get("project")`` (always ``None``) and the
   project fallback compared ``extract_project_context`` against a
   ``"default"`` sentinel it never returns.  The classifier's project
   now flows through to ``get_or_create_project``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import proxy
from fastapi.responses import JSONResponse


def _classification(*, project_name: str = "general") -> dict[str, Any]:
    return {
        "is_valid": True,
        "intent": "CHAT",
        "priority": 2,
        "complexity": "low",
        "project_name": project_name,
        "is_factual": False,
        "tools_required": False,
    }


class _StreamCapture:
    """Async-generator stand-in for ``stream_llm`` that yields minimal chunks."""

    def __init__(self) -> None:
        self.endpoint: str | None = None
        self.payload: dict[str, Any] | None = None

    async def __call__(
        self,
        *,
        endpoint: str,
        payload: dict[str, Any],
        port: int = 0,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.endpoint = endpoint
        self.payload = payload
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                },
            ],
        }


@pytest_asyncio.fixture
async def cmd_client() -> Any:
    """Yield an httpx async client against the real app with state stubbed."""
    from tests.conftest import (
        _NoOpCooling,
        _NoOpDatabase,
        _NoOpSystemd,
    )

    proxy.app.state.database = _NoOpDatabase()
    proxy.app.state.systemd = _NoOpSystemd()
    proxy.app.state.cooler = _NoOpCooling()
    proxy.app.state.hardware = None
    proxy.app.state.active_heavy_model = None
    proxy.app.state.active_priority = 3
    proxy.app.state.requests_served = 0

    transport = httpx.ASGITransport(app=proxy.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestEmbeddedPauseResumeCommands:
    """``/pause`` and ``/resume`` typed by the user must reach their
    handlers even though ``user_text`` is prefixed with ``[user]: ``.
    """

    @pytest.mark.asyncio
    async def test_pause_command_reaches_handler(self, cmd_client) -> None:
        mock_pause = AsyncMock(return_value=JSONResponse({"paused": True}))
        with patch("routes._handle_pause_command", new=mock_pause):
            response = await cmd_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "/pause 30"}],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200
        mock_pause.assert_awaited_once()
        args, _kwargs = mock_pause.await_args
        assert args[0] == 30
        assert args[1] is proxy.app.state

    @pytest.mark.asyncio
    async def test_resume_command_reaches_handler(self, cmd_client) -> None:
        mock_resume = AsyncMock(return_value=JSONResponse({"resumed": True}))
        with patch("routes._handle_resume_command", new=mock_resume):
            response = await cmd_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "/resume"}],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            await response.aread()

        assert response.status_code == 200
        mock_resume.assert_awaited_once()
        args, _kwargs = mock_resume.await_args
        assert args[0] is proxy.app.state


class TestClassificationProjectNameFlowsThrough:
    """A classification carrying a ``project_name`` must drive the
    project used for the job — not the hardcoded ``general`` fallback.
    """

    @pytest.mark.asyncio
    async def test_project_name_is_used_not_general(self, cmd_client) -> None:
        capture = _StreamCapture()
        mock_get_project = AsyncMock(return_value="proj-1")
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification(project_name="myproject")),
             ), \
             patch.object(
                 proxy.app.state.database,
                 "get_or_create_project",
                 new=mock_get_project,
             ):
            response = await cmd_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                headers={"Authorization": "Bearer agent-key"},
            )
            text = (await response.aread()).decode()

        assert response.status_code == 200, text
        mock_get_project.assert_awaited_once()
        args, _kwargs = mock_get_project.await_args
        assert args[0] == "myproject"
