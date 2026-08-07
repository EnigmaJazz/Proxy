"""Unit tests for routing.evaluate_coding_task (local difficulty assessment).

The evaluator asks the local frontdesk model to judge a coding task's
difficulty and recommend a route (opencode vs local).  It is strictly
best-effort: any failure returns safe defaults so the coding gate keeps
working.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from routing import _CODING_DIFFICULTY_PROMPT, evaluate_coding_task

DEFAULTS = {
    "difficulty": "medium",
    "recommendation": "local",
    "reason": "",
}


class TestEvaluateCodingTask:
    @pytest.mark.asyncio
    async def test_parses_frontdesk_json(self) -> None:
        """A well-formed frontdesk response maps to the assessment dict, and
        the call uses the dedicated profile / endpoint shape."""
        with patch(
            "llm.call_model",
            new=AsyncMock(return_value=json.dumps({
                "difficulty": "high",
                "recommendation": "opencode",
                "reason": "multi-file",
            })),
        ) as mock_call:
            result = await evaluate_coding_task(
                "refactor the auth module", model_port=9999,
            )

        assert result == {
            "difficulty": "high",
            "recommendation": "opencode",
            "reason": "multi-file",
        }
        mock_call.assert_awaited_once()
        kwargs = mock_call.await_args.kwargs
        assert kwargs["port"] == 9999
        assert kwargs["profile"] == "json_gbnf_difficulty"
        assert kwargs["max_tokens"] == 256
        assert kwargs["prompt"].startswith(_CODING_DIFFICULTY_PROMPT)
        assert kwargs["prompt"].endswith("Task: refactor the auth module")

    @pytest.mark.asyncio
    async def test_strips_fences_and_trailing_junk(self) -> None:
        """Markdown fences and concatenated trailing garbage are tolerated
        (mirrors classify_with_frontdesk's raw_decode handling)."""
        with patch(
            "llm.call_model",
            new=AsyncMock(return_value=(
                '```json\n{"difficulty": "low", "recommendation": "local", '
                '"reason": "one file"}{"extra": 1}\n```'
            )),
        ):
            result = await evaluate_coding_task("fix the typo")

        assert result["difficulty"] == "low"
        assert result["recommendation"] == "local"
        assert result["reason"] == "one file"

    @pytest.mark.asyncio
    async def test_merges_with_defaults_when_keys_missing(self) -> None:
        """Missing keys fall back to defaults so all three are always present."""
        with patch(
            "llm.call_model",
            new=AsyncMock(return_value='{"recommendation": "opencode"}'),
        ):
            result = await evaluate_coding_task("task")

        assert result["recommendation"] == "opencode"
        assert result["difficulty"] == "medium"
        assert result["reason"] == ""

    @pytest.mark.asyncio
    async def test_returns_defaults_on_http_error(self) -> None:
        with patch(
            "llm.call_model",
            new=AsyncMock(side_effect=httpx.ConnectError("connection refused")),
        ):
            result = await evaluate_coding_task("task")

        assert result == DEFAULTS

    @pytest.mark.asyncio
    async def test_returns_defaults_on_json_decode_error(self) -> None:
        with patch("llm.call_model", new=AsyncMock(return_value="not json at all")):
            result = await evaluate_coding_task("task")

        assert result == DEFAULTS

    @pytest.mark.asyncio
    async def test_returns_defaults_on_empty_response(self) -> None:
        with patch("llm.call_model", new=AsyncMock(return_value="")):
            result = await evaluate_coding_task("task")

        assert result == DEFAULTS


    @pytest.mark.asyncio
    async def test_complexity_signal_escalates_local_to_opencode(self) -> None:
        """The 2B frontdesk under-judges multi-file work as local; a strong
        complexity signal in the task text must escalate to opencode."""
        with patch(
            "llm.call_model",
            new=AsyncMock(return_value=json.dumps({
                "difficulty": "low",
                "recommendation": "local",
                "reason": "single-file",
            })),
        ):
            result = await evaluate_coding_task(
                "refactor the entire auth module across 12 files to async",
            )

        assert result["recommendation"] == "opencode"
        assert result["difficulty"] == "medium"  # low → medium, never high

    @pytest.mark.asyncio
    async def test_no_complexity_signal_keeps_local(self) -> None:
        """A simple task with no complexity signal keeps the local route."""
        with patch(
            "llm.call_model",
            new=AsyncMock(return_value=json.dumps({
                "difficulty": "low",
                "recommendation": "local",
                "reason": "single-file",
            })),
        ):
            result = await evaluate_coding_task("write a small hello world script")

        assert result["recommendation"] == "local"
        assert result["difficulty"] == "low"
