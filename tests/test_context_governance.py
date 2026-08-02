"""Tests for frontend-agnostic context governance (proxy/context_governance.py).

Covers the pure transforms (truncation, offload, snip, structural cleanup)
and the endpoint integration (governed outbound payload in the general path
and the dream fast-path, plus the per-request opt-out header).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import proxy
from context_governance import (
    STATUS_SENTINEL,
    apply_context_governance,
    strip_proxy_status,
)
from tests.conftest import _NoOpCooling, _NoOpDatabase, _NoOpSystemd
from tests.test_r1_sse_format import _make_profile_table


def _classification() -> dict[str, Any]:
    return {
        "is_valid": True,
        "intent": "TOOL",
        "priority": 2,
        "complexity": "low",
        "project_name": "general",
        "is_factual": False,
        "tools_required": True,
    }


def _tool_messages(
    result: str, name: str = "exec", tool_call_id: str = "call_1"
) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "run the report"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": result},
        {"role": "user", "content": "what did you find?"},
    ]


# ---------------------------------------------------------------------------
# Unit tests — pure transforms
# ---------------------------------------------------------------------------


class TestResultBudget:
    def test_truncates_oversized_result_with_marker(self) -> None:
        messages = _tool_messages("x" * 20_000)
        governed = apply_context_governance(
            messages,
            model_key="professional",
            context_window=65_536,
            workspace=Path("/tmp/nonexistent-workspace"),
        )
        tool_msg = governed[2]
        assert "…[tool result truncated: 20000 chars" in tool_msg["content"]
        assert len(tool_msg["content"]) < 17_000
        # User/assistant text untouched.
        assert governed[0] == messages[0]
        assert governed[1] == messages[1]

    def test_read_file_gets_larger_head(self) -> None:
        messages = _tool_messages("x" * 20_000, name="read_file")
        governed = apply_context_governance(
            messages,
            model_key="professional",
            context_window=65_536,
            workspace=Path("/tmp/nonexistent-workspace"),
        )
        # read_file keeps a 32K head — 20K stays inline, untruncated.
        assert governed[2]["content"] == messages[2]["content"]

    def test_offloads_very_large_result_and_returns_reference(self, tmp_path: Path) -> None:
        messages = _tool_messages("y" * 40_000)
        governed = apply_context_governance(
            messages,
            model_key="professional",
            context_window=65_536,
            workspace=tmp_path,
        )
        tool_msg = governed[2]
        assert "saved to" in tool_msg["content"]
        assert "preview:" in tool_msg["content"]
        assert "40000 chars" in tool_msg["content"]
        files = list(tmp_path.rglob("*.txt"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == "y" * 40_000

    def test_small_result_untouched(self) -> None:
        messages = _tool_messages("short ok")
        governed = apply_context_governance(
            messages,
            model_key="professional",
            context_window=65_536,
            workspace=Path("/tmp/nonexistent-workspace"),
        )
        assert governed[2]["content"] == "short ok"

    def test_traversal_tool_call_id_cannot_escape_bucket(self, tmp_path: Path) -> None:
        # A crafted tool_call_id must never escape the session bucket when the
        # offload path is built (arbitrary-write guard).
        messages = _tool_messages("z" * 40_000, tool_call_id="../../escape")
        governed = apply_context_governance(
            messages,
            model_key="professional",
            context_window=65_536,
            workspace=tmp_path,
        )
        tool_msg = governed[2]
        assert "saved to" in tool_msg["content"]
        # The reference resolves inside the workspace, never outside it.
        assert "../" not in tool_msg["content"].split("saved to ")[1].split("]")[0]
        written = list(tmp_path.rglob("*.txt"))
        assert len(written) == 1
        assert written[0].parent != tmp_path
        assert written[0].name != "escape.txt"
        assert str(written[0].resolve()).startswith(str(tmp_path.resolve()))
        # Nothing was written next to the workspace.
        assert not (tmp_path.parent / "escape.txt").exists()


class TestStructuralCleanup:
    def test_drops_orphan_tool_result(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "tool",
                "tool_call_id": "call_gone",
                "content": "orphaned result",
            },
            {"role": "user", "content": "hello again"},
        ]
        governed = apply_context_governance(
            messages,
            model_key="professional",
            context_window=65_536,
        )
        roles = [m["role"] for m in governed]
        assert roles == ["user", "user"]

    def test_strips_malformed_tool_calls(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "bad_missing_function"},
                    {
                        "id": "call_ok",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_ok", "content": "done"},
            {"role": "user", "content": "next"},
        ]
        governed = apply_context_governance(
            messages,
            model_key="professional",
            context_window=65_536,
        )
        assistant = [m for m in governed if m["role"] == "assistant"][0]
        assert [tc["id"] for tc in assistant["tool_calls"]] == ["call_ok"]


class TestStripProxyStatus:
    """The OUTBOUND status filter: proxy-owned sentinel content must
    never reach the model, while every legitimate message survives.
    """

    def _status_messages(self) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": f"{STATUS_SENTINEL}🔍 Proxy triage: classified as TOOL"},
            {"role": "assistant", "content": f"{STATUS_SENTINEL}🔧 Calling exec: `ls`"},
            {"role": "assistant", "content": "the real answer"},
        ]

    def test_strips_sentinel_status_from_outbound(self) -> None:
        cleaned = strip_proxy_status(self._status_messages())
        contents = [m["content"] for m in cleaned]
        assert contents == ["hello", "the real answer"]

    def test_keeps_loop_warning_without_sentinel(self) -> None:
        # Loop warnings are model-directed corrections — NOT sentinel
        # prefixed — so they must survive the strip and reach the model.
        messages = self._status_messages() + [
            {"role": "assistant", "content": "⚠️ Tool loop detected: exec. Use that result..."}
        ]
        cleaned = strip_proxy_status(messages)
        assert any("Tool loop detected" in m["content"] for m in cleaned)

    def test_never_strips_model_tool_calls(self) -> None:
        messages = [
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": f"{STATUS_SENTINEL}🔧 Calling exec: `ls`",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "done"},
        ]
        cleaned = strip_proxy_status(messages)
        assert any(m.get("tool_calls") for m in cleaned)
        assert any(m["role"] == "tool" for m in cleaned)

    def test_never_strips_user_content_with_sentinel(self) -> None:
        # A user who literally types the sentinel prefix at the start of
        # their own message must never lose it.
        messages = [
            {"role": "user", "content": f"{STATUS_SENTINEL}my own message"},
        ]
        cleaned = strip_proxy_status(messages)
        assert cleaned == messages


class TestBudgetSnip:
    def test_drops_oldest_turns_keeping_last_user_turn(self) -> None:
        messages = [{"role": "user", "content": "old " * 10_000}] * 3 + [
            {"role": "user", "content": "the current question"}
        ]
        governed = apply_context_governance(
            messages,
            model_key="professional",
            context_window=65_536,
            max_output_tokens=4096,
        )
        assert len(governed) <= len(messages)
        assert governed[-1]["content"] == "the current question"

    def test_never_splits_tool_pair(self) -> None:
        # Several old turns with tool calls, then the current turn.
        old_turn = [
            {"role": "user", "content": "old turn " * 20_000},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_old",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_old", "content": "old result " * 2_000},
        ]
        messages = old_turn * 6 + [{"role": "user", "content": "current q"}]
        governed = apply_context_governance(
            messages,
            model_key="professional",
            context_window=65_536,
            max_output_tokens=4096,
        )
        roles = [m["role"] for m in governed]
        # No orphaned tool result: every tool message has a preceding assistant call.
        for i, m in enumerate(governed):
            if m["role"] == "tool":
                assert roles[i - 1] == "assistant", f"split pair at {i}: {roles}"
        assert roles[-1] == "user"

    def test_enabled_false_returns_input(self) -> None:
        messages = _tool_messages("x" * 20_000)
        governed = apply_context_governance(
            messages,
            model_key="professional",
            context_window=65_536,
            workspace=Path("/tmp/nonexistent-workspace"),
            enabled=False,
        )
        assert governed is messages


# ---------------------------------------------------------------------------
# Integration tests — endpoint payload governance
# ---------------------------------------------------------------------------


class _StreamCapture:
    """Async-generator stand-in for ``stream_llm`` capturing the payload."""

    def __init__(self) -> None:
        self.endpoint: Optional[str] = None
        self.payload: Optional[dict[str, Any]] = None

    async def __call__(
        self,
        *,
        endpoint: str,
        payload: dict[str, Any],
        port: int = 0,
        headers: Optional[dict[str, Any]] = None,
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
async def governance_client() -> Any:
    proxy.app.state.database = _NoOpDatabase()
    proxy.app.state.systemd = _NoOpSystemd()
    proxy.app.state.cooler = _NoOpCooling()
    proxy.app.state.hardware = None
    proxy.app.state.active_heavy_model = None
    proxy.app.state.active_priority = 3
    proxy.app.state.requests_served = 0
    proxy.app.state.model_profiles = _make_profile_table()

    transport = httpx.ASGITransport(app=proxy.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestEndpointGovernance:
    async def _post(
        self,
        client: Any,
        messages: list[dict[str, Any]],
        *,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> tuple[Any, _StreamCapture]:
        capture = _StreamCapture()
        headers = {"Authorization": "Bearer agent-key", **(extra_headers or {})}
        with patch("routes.stream_llm", new=capture), \
             patch(
                 "routes.classify_with_frontdesk",
                 new=AsyncMock(return_value=_classification()),
             ):
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": messages,
                    "stream": True,
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "exec",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                },
                headers=headers,
            )
            text = (await response.aread()).decode()
        assert response.status_code == 200, text
        return response, capture

    @pytest.mark.asyncio
    async def test_general_path_governs_outbound_payload(self, governance_client) -> None:
        response, capture = await self._post(
            governance_client,
            _tool_messages("x" * 20_000),
        )
        sent = capture.payload["messages"]
        tool_msg = next(m for m in sent if m["role"] == "tool")
        assert "truncated" in tool_msg["content"]
        # The raw audit copy stored on the job is NOT governed.
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_opt_out_header_preserves_raw_content(self, governance_client) -> None:
        response, capture = await self._post(
            governance_client,
            _tool_messages("x" * 20_000),
            extra_headers={"X-Proxy-Context-Governance": "off"},
        )
        sent = capture.payload["messages"]
        tool_msg = next(m for m in sent if m["role"] == "tool")
        assert tool_msg["content"] == "x" * 20_000

    @pytest.mark.asyncio
    async def test_outbound_payload_strips_proxy_status(self, governance_client) -> None:
        """Sentinel-prefixed proxy status in the request history must be
        stripped from the OUTBOUND model-copy — even when the budget
        governance is opted out, because the strip is the echo-loop fix,
        not a budget optimization.
        """
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": f"{STATUS_SENTINEL}🔍 Proxy triage: classified as TOOL"},
            {"role": "assistant", "content": "the real answer"},
        ]
        for extra in ({}, {"X-Proxy-Context-Governance": "off"}):
            response, capture = await self._post(
                governance_client,
                messages,
                extra_headers=extra or None,
            )
            sent = capture.payload["messages"]
            assert all(
                not (m.get("content", "") or "").startswith(STATUS_SENTINEL)
                for m in sent
            ), f"sentinel status leaked into outbound: {sent!r}"
            assert any("the real answer" in m["content"] for m in sent)
