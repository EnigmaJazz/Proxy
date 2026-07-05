"""Regression tests for the Glass-Pipe Hardening change (PR1 + PR2).

One test class per R requirement.  PR1 covers R1, R2, R7, R8, R9, R10, R11.
PR2 will extend this file with R3, R4, R5, R6, R12, R13, R14, R15, R16.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import proxy


# ---------------------------------------------------------------------------
# SSE parsing helpers
# ---------------------------------------------------------------------------
def _parse_sse_lines(raw: bytes) -> list[str]:
    """Split raw SSE response into individual lines."""
    return raw.decode("utf-8").splitlines()


def _extract_event_lines(lines: list[str], prefix: str = "event: kinver.proxy.") -> list[dict[str, Any]]:
    """Return parsed JSON payloads for every SSE event line matching ``prefix``."""
    events: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            data_line = lines[i + 1] if i + 1 < len(lines) else ""
            if data_line.startswith("data: "):
                events.append(json.loads(data_line[len("data: "):]))
    return events


# ---------------------------------------------------------------------------
# PR1: Parameter plane
# ---------------------------------------------------------------------------
class TestPassthrough:
    """R1, R7, R11 — client-sent parameters and OpenAI fields are forwarded."""

    async def test_R1_client_wins_temperature_top_p_max_tokens(self, app_client: httpx.AsyncClient) -> None:
        """Client-sent temperature/top_p/max_tokens survive intent defaults."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        classification = {
            "is_valid": True,
            "intent": "CODE",
            "priority": 1,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.7,
            "top_p": 0.95,
            "max_tokens": 2048,
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        assert captured, "stream_llm was not called"
        payload = captured[0]
        assert payload["temperature"] == 0.7
        assert payload["top_p"] == 0.95
        assert payload["max_tokens"] == 2048

    async def test_R7_client_wins_thinking_budget_tokens(self, app_client: httpx.AsyncClient) -> None:
        """Client-sent thinking_budget_tokens survives intent defaults."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        classification = {
            "is_valid": True,
            "intent": "CODE",
            "priority": 1,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "thinking_budget_tokens": 8192,
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        assert captured, "stream_llm was not called"
        assert captured[0]["thinking_budget_tokens"] == 8192

    async def test_R11_full_openai_field_set(self, app_client: httpx.AsyncClient) -> None:
        """Optional OpenAI fields are forwarded when client sends them."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        classification = {
            "is_valid": True,
            "intent": "CODE",
            "priority": 1,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "seed": 42,
            "response_format": {"type": "json_object"},
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        assert captured, "stream_llm was not called"
        payload = captured[0]
        assert payload["tool_choice"] == "auto"
        assert payload["parallel_tool_calls"] is True
        assert payload["seed"] == 42
        assert payload["response_format"] == {"type": "json_object"}
        assert "frequency_penalty" not in payload


class TestExceptions:
    """R2, R8, R9 — intentional Glass-Pipe exceptions are documented."""

    async def test_R2_doc_labels_present(self, app_client: httpx.AsyncClient) -> None:
        """translate_to_deepseek_r1 carries an intentional-exception label."""
        repo_root = Path(__file__).parent.parent
        routes_source = (repo_root / "routes.py").read_text()
        llm_source = (repo_root / "llm.py").read_text()
        assert "Glass Pipe exception" in routes_source
        assert "Glass Pipe exception" in llm_source

    async def test_R8_stop_seq_doc_present(self, app_client: httpx.AsyncClient) -> None:
        """Stop-sequence filter carries an intentional-exception label."""
        repo_root = Path(__file__).parent.parent
        routes_source = (repo_root / "routes.py").read_text()
        assert "Glass Pipe exception" in routes_source
        assert "ReAct-era stops" in routes_source or "mid-tool-call" in routes_source

    async def test_R9_param_overrides_doc_present(self, app_client: httpx.AsyncClient) -> None:
        """Parameter intent-default block carries an intentional-exception label."""
        repo_root = Path(__file__).parent.parent
        routes_source = (repo_root / "routes.py").read_text()
        assert "Glass Pipe exception" in routes_source
        assert "intent defaults only fill gaps" in routes_source


class TestStreamIntegrity:
    """R3, R4, R5, R6, R14 — SSE stream integrity and proxy events."""

    async def test_R3_lane_b_tools_passthrough(self, app_client: httpx.AsyncClient) -> None:
        """Lane B/IDE callers forward client tools unchanged."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            if False:
                yield {}

        tools = [{"type": "function", "function": {"name": "read_file"}}]
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": tools,
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            response = await app_client.post(
                "/v1/chat/completions",
                json=body,
                headers={"sk-ide-pass": "sk-ide-pass"},
            )
        assert response.status_code == 200
        assert captured, "stream_llm was not called"
        assert captured[0].get("tools") == tools

    async def test_R4_tool_stripped_event_emitted(self, app_client: httpx.AsyncClient) -> None:
        """Loop detection emits kinver.proxy.tool_stripped before the first chunk."""
        captured: list[dict] = []

        async def _fake_stream(*, payload: dict, **kwargs):
            captured.append(payload)
            yield {"choices": [{"delta": {"content": "ok"}}]}

        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.detect_tool_loops", new=AsyncMock(return_value=(True, "repeated_tool_call"))):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        lines = _parse_sse_lines(response.content)
        events = _extract_event_lines(lines)
        tool_stripped = [e for e in events if e.get("kind") == "tool_stripped"]
        assert tool_stripped, "tool_stripped event not found"
        assert tool_stripped[0]["data"]["reason"] == "repeated_tool_call"
        # The event must appear before any model data chunk.
        first_data_idx = next(
            (i for i, line in enumerate(lines) if line.startswith("data: {")),
            None,
        )
        event_idx = next(
            (i for i, line in enumerate(lines) if line == "event: kinver.proxy.tool_stripped"),
            None,
        )
        assert event_idx is not None and first_data_idx is not None
        assert event_idx < first_data_idx

    async def test_R5_proxy_status_events(self, app_client: httpx.AsyncClient) -> None:
        """Triage banner is emitted as kinver.proxy.status, not delta.content."""

        async def _fake_stream(*, payload: dict, **kwargs):
            yield {"choices": [{"delta": {"content": "hello"}}]}

        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "model": "professional",
        }
        classification = {
            "is_valid": True,
            "intent": "CHAT",
            "priority": 2,
            "complexity": "low",
            "project_name": "general",
            "is_factual": False,
            "tools_required": False,
        }
        with patch("routes.stream_llm", side_effect=_fake_stream):
            with patch("routes.classify_with_frontdesk", new=AsyncMock(return_value=classification)):
                response = await app_client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer agent-key"},
                )
        assert response.status_code == 200
        lines = _parse_sse_lines(response.content)
        events = _extract_event_lines(lines)
        status_events = [e for e in events if e.get("kind") == "status"]
        assert status_events, "kinver.proxy.status event not found"
        triage_events = [e for e in status_events if e.get("data", {}).get("subkind") == "triage"]
        assert triage_events, "triage subkind not found"
        # No banner text should appear inside a content delta.
        for line in lines:
            if line.startswith("data: {"):
                try:
                    chunk = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                assert "Proxy triage" not in (content or ""), "triage leaked into delta.content"

    async def test_R14_command_status_events(self, app_client: httpx.AsyncClient) -> None:
        """Pause/resume/cloud embedded commands emit kinver.proxy.status."""
        from routes import _handle_pause_command

        mock_state = MagicMock()
        mock_state.try_pause_queue = AsyncMock(return_value=(True, ""))
        response = await _handle_pause_command(5, mock_state)
        body = "".join([chunk async for chunk in response.body_iterator]).encode("utf-8")
        lines = _parse_sse_lines(body)
        events = _extract_event_lines(lines)
        pause_events = [
            e for e in events
            if e.get("kind") == "status" and e.get("data", {}).get("subkind") == "pause"
        ]
        assert pause_events, "pause status event not found"
        # No synthetic content chunk should carry the pause text.
        for line in lines:
            if line.startswith("data: {"):
                try:
                    chunk = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                assert "Queue paused" not in (content or ""), "pause text leaked into delta.content"


class TestHarness:
    """R10 — the test harness itself is sane."""

    async def test_R10_pyproject_config(self, app_client: httpx.AsyncClient) -> None:
        """pyproject.toml configures pytest-asyncio in auto mode."""
        repo_root = Path(__file__).parent.parent
        pyproject = (repo_root / "pyproject.toml").read_text()
        assert 'asyncio_mode = "auto"' in pyproject
        assert 'testpaths = ["tests"]' in pyproject

    async def test_R10_harness_imports_safely(self, app_client: httpx.AsyncClient) -> None:
        """The harness can be imported and exercised without real hardware."""
        response = await app_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
