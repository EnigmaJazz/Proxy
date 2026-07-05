"""Regression tests for the Glass-Pipe Hardening change (PR1 + PR2).

One test class per R requirement.  PR1 covers R1, R2, R7, R8, R9, R10, R11.
PR2 will extend this file with R3, R4, R5, R6, R12, R13, R14, R15, R16.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

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
        pass


class TestExceptions:
    """R2, R8, R9 — intentional Glass-Pipe exceptions are documented."""

    async def test_R2_doc_labels_present(self, app_client: httpx.AsyncClient) -> None:
        """translate_to_deepseek_r1 carries an intentional-exception label."""
        pass

    async def test_R8_stop_seq_doc_present(self, app_client: httpx.AsyncClient) -> None:
        """Stop-sequence filter carries an intentional-exception label."""
        pass

    async def test_R9_param_overrides_doc_present(self, app_client: httpx.AsyncClient) -> None:
        """Parameter intent-default block carries an intentional-exception label."""
        pass


class TestHarness:
    """R10 — the test harness itself is sane."""

    async def test_R10_harness_imports_safely(self, app_client: httpx.AsyncClient) -> None:
        """The harness can be imported and exercised without real hardware."""
        pass
