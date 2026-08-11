"""Tests for proxy search-result enrichment (search_enrichment.py).

Covers the explicit user-approved R1 carve-out extension: thin frontend
web-search results on the OUTBOUND model-copy are enriched with the
proxy's own rich search (tools.web_search.execute_web_search), while
client content, the DB audit copy, and user/assistant text stay untouched.
"""
from __future__ import annotations

import types
from typing import Any

import pytest

from search_enrichment import (
    _find_thin_search_results,
    _is_thin_result,
    enrich_thin_search_results,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

THIN_SNIPPET_JSON = (
    '[{"title": "Kinver weather", "link": "https://x.example/f", '
    '"snippet": "Kinver 7 day weather forecast"}, '
    '{"title": "Meteored", "link": "https://y.example/f", '
    '"snippet": "14 day forecast with hourly details"}]'
)

RICH_TEXT = "### Source 1: https://weather.example\nPatchy rain nearby, 23C.\n\n---\n### Source 2: ...\n" * 40


def _search_messages(
    result: str,
    tool_name: str = "search_web",
    arguments: str = '{"query": "weather forecast in Kinver"}',
    tool_call_id: str = "call_1",
) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "what's the weather in Kinver?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": tool_call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": arguments},
            }],
        },
        {"role": "tool", "tool_call_id": tool_call_id, "content": result},
    ]


async def _fake_search(query: str, depth: str = "standard") -> str:
    return f"### Source 1: https://x.example/{query.replace(' ', '_')}\nRich answer for {query} at {depth} depth."


# ---------------------------------------------------------------------------
# Thin-result detection
# ---------------------------------------------------------------------------

class TestThinDetection:
    def test_json_snippet_array_is_thin(self) -> None:
        assert _is_thin_result(THIN_SNIPPET_JSON) is True

    def test_empty_result_is_thin(self) -> None:
        assert _is_thin_result("") is True

    def test_short_text_is_thin(self) -> None:
        assert _is_thin_result("short snippet only") is True

    def test_long_text_is_rich(self) -> None:
        assert _is_thin_result(RICH_TEXT) is False

    def test_already_enriched_is_not_thin(self) -> None:
        assert (
            _is_thin_result(
                'snippet\n\n---\n[Proxy-enriched search results for "x"]\n### Source 1: ...'
            )
            is False
        )


# ---------------------------------------------------------------------------
# Pair discovery
# ---------------------------------------------------------------------------

class TestPairDiscovery:
    def test_finds_search_pair(self) -> None:
        msgs = _search_messages(THIN_SNIPPET_JSON)
        pairs = _find_thin_search_results(msgs)
        assert pairs == [(2, "weather forecast in Kinver", "standard")]

    def test_respects_depth_argument(self) -> None:
        msgs = _search_messages(
            THIN_SNIPPET_JSON,
            arguments='{"query": "q", "depth": "deep"}',
        )
        assert _find_thin_search_results(msgs) == [(2, "q", "deep")]

    def test_handles_dict_arguments(self) -> None:
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "web_search", "arguments": {"query": "q"}}},
            ]},
            {"role": "tool", "tool_call_id": "c", "content": "thin"},
        ]
        assert _find_thin_search_results(msgs) == [(1, "q", "standard")]

    def test_non_search_tool_ignored(self) -> None:
        msgs = _search_messages(THIN_SNIPPET_JSON, tool_name="read_file")
        assert _find_thin_search_results(msgs) == []

    def test_missing_query_ignored(self) -> None:
        msgs = _search_messages(THIN_SNIPPET_JSON, arguments='{"depth": "standard"}')
        assert _find_thin_search_results(msgs) == []

    def test_no_following_result_ignored(self) -> None:
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "search_web", "arguments": '{"query": "q"}'}},
            ]},
            {"role": "user", "content": "next turn"},
        ]
        assert _find_thin_search_results(msgs) == []

    def test_rich_result_not_paired(self) -> None:
        msgs = _search_messages(RICH_TEXT)
        assert _find_thin_search_results(msgs) == []

    def test_batched_calls_pair_in_order(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": '{"path": "a"}'}},
                    {"function": {"name": "search_web", "arguments": '{"query": "q1"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "r1", "content": "file contents, not a search result but long enough"},
            {"role": "tool", "tool_call_id": "r2", "content": "thin snippet"},
        ]
        assert _find_thin_search_results(msgs) == [(2, "q1", "standard")]

    def test_multiple_rounds(self) -> None:
        msgs = _search_messages(THIN_SNIPPET_JSON) + [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "search_web", "arguments": '{"query": "q2"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_2", "content": "also thin"},
        ]
        assert _find_thin_search_results(msgs) == [
            (2, "weather forecast in Kinver", "standard"),
            (4, "q2", "standard"),
        ]


# ---------------------------------------------------------------------------
# Enrichment behaviour
# ---------------------------------------------------------------------------

class TestEnrichment:
    @pytest.mark.asyncio
    async def test_thin_json_snippet_enriched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tools.web_search.execute_web_search", _fake_search)
        msgs = _search_messages(THIN_SNIPPET_JSON)
        out = await enrich_thin_search_results(msgs)
        assert "[Proxy-enriched search results for 'weather forecast in Kinver']" in out[2]["content"]
        assert "Rich answer for weather forecast in Kinver" in out[2]["content"]
        # Original client content preserved (appended, never replaced)
        assert out[2]["content"].startswith(THIN_SNIPPET_JSON)
        # Input never mutated
        assert msgs[2]["content"] == THIN_SNIPPET_JSON

    @pytest.mark.asyncio
    async def test_rich_result_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _should_not_run(*args: Any, **kwargs: Any) -> str:
            raise AssertionError("enrichment must not run for rich results")

        monkeypatch.setattr("tools.web_search.execute_web_search", _should_not_run)
        msgs = _search_messages(RICH_TEXT)
        out = await enrich_thin_search_results(msgs)
        assert out is msgs  # returned as-is
        assert out[2]["content"] == RICH_TEXT

    @pytest.mark.asyncio
    async def test_empty_result_enriched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tools.web_search.execute_web_search", _fake_search)
        msgs = _search_messages("")
        out = await enrich_thin_search_results(msgs)
        assert "Rich answer" in out[2]["content"]

    @pytest.mark.asyncio
    async def test_search_error_preserves_original(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _error(*args: Any, **kwargs: Any) -> str:
            return "[Search Error: HTTP 500 from SearXNG]"

        monkeypatch.setattr("tools.web_search.execute_web_search", _error)
        msgs = _search_messages(THIN_SNIPPET_JSON)
        out = await enrich_thin_search_results(msgs)
        assert out[2]["content"] == THIN_SNIPPET_JSON

    @pytest.mark.asyncio
    async def test_exception_preserves_original(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _boom(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("searxng down")

        monkeypatch.setattr("tools.web_search.execute_web_search", _boom)
        msgs = _search_messages(THIN_SNIPPET_JSON)
        out = await enrich_thin_search_results(msgs)
        assert out[2]["content"] == THIN_SNIPPET_JSON

    @pytest.mark.asyncio
    async def test_disabled_returns_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _should_not_run(*args: Any, **kwargs: Any) -> str:
            raise AssertionError("must not run when disabled")

        monkeypatch.setattr("tools.web_search.execute_web_search", _should_not_run)
        msgs = _search_messages(THIN_SNIPPET_JSON)
        out = await enrich_thin_search_results(msgs, enabled=False)
        assert out is msgs

    @pytest.mark.asyncio
    async def test_never_touches_user_or_assistant_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tools.web_search.execute_web_search", _fake_search)
        msgs = _search_messages(THIN_SNIPPET_JSON)
        user_before = msgs[0]["content"]
        assistant_before = msgs[1]["content"]
        out = await enrich_thin_search_results(msgs)
        assert out[0]["content"] == user_before
        assert out[1]["content"] == assistant_before

    @pytest.mark.asyncio
    async def test_idempotent_not_re_enriched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        async def _counting(query: str, depth: str = "standard") -> str:
            calls.append(query)
            return f"### Source 1\nRich for {query}"

        monkeypatch.setattr("tools.web_search.execute_web_search", _counting)
        msgs = _search_messages(THIN_SNIPPET_JSON)
        once = await enrich_thin_search_results(msgs)
        twice = await enrich_thin_search_results(once)
        assert len(calls) == 1  # second pass sees the marker and skips
        assert "Rich for weather forecast in Kinver" in twice[2]["content"]

    @pytest.mark.asyncio
    async def test_multiple_rounds_enriched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tools.web_search.execute_web_search", _fake_search)
        msgs = _search_messages(THIN_SNIPPET_JSON) + [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "search_web", "arguments": '{"query": "q2"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_2", "content": "thin"},
        ]
        out = await enrich_thin_search_results(msgs)
        assert "weather forecast in Kinver" in out[2]["content"]
        assert "Rich answer for q2" in out[4]["content"]


# ---------------------------------------------------------------------------
# Date/time injection — routes._inject_current_datetime
# ---------------------------------------------------------------------------

class TestDateTimeInjection:
    """The OUTBOUND system message gets the current date + time (frontends
    never send it), with ``X-Proxy-Date-Time: off`` as the opt-out."""

    def _inject(self, msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from routes import _inject_current_datetime
        return _inject_current_datetime(msgs)

    def test_prefixes_existing_system_message(self) -> None:
        msgs = [{"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hi"}]
        out = self._inject(msgs)
        # KV-CACHE POSITION (2026-08-11): the stamp is APPENDED to the
        # system message so the stable system content stays the
        # cache-visible prefix — a prepended time changed the prefix
        # every request and forced full ~45s prefills.
        assert out[0]["content"].startswith("You are helpful.")
        assert "Today's date:" in out[0]["content"]
        assert "Current time:" in out[0]["content"]
        assert "Current time:" in out[0]["content"][-40:]  # stamp at the very end
        assert out[1] == msgs[1]  # user message untouched

    def test_inserts_system_message_when_absent(self) -> None:
        msgs = [{"role": "user", "content": "hi"}]
        out = self._inject(msgs)
        assert len(out) == 2
        assert out[0]["role"] == "system"
        assert out[0]["content"].startswith("Today's date:")
        assert out[1] == msgs[0]

    def test_never_mutates_input(self) -> None:
        msgs = [{"role": "system", "content": "You are helpful."}]
        self._inject(msgs)
        assert msgs[0]["content"] == "You are helpful."

    @pytest.mark.asyncio
    async def test_govern_wiring_default_on(self) -> None:
        from routes import _govern_messages
        import types as _types
        request = _types.SimpleNamespace(headers={})
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        out = await _govern_messages(request, msgs, model_key="professional", max_tokens=4096)
        assert out[0]["content"].startswith("sys")
        assert "Today's date:" in out[0]["content"]

    @pytest.mark.asyncio
    async def test_govern_wiring_opt_out(self) -> None:
        from routes import _govern_messages
        import types as _types
        request = _types.SimpleNamespace(headers={"x-proxy-date-time": "off"})
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        out = await _govern_messages(request, msgs, model_key="professional", max_tokens=4096)
        assert out[0]["content"] == "sys"


# ---------------------------------------------------------------------------
# Wire tests — routes._govern_messages (OUTBOUND copy + opt-out header)
# ---------------------------------------------------------------------------

class TestGovernMessagesWiring:
    """The enrichment runs from ``routes._govern_messages`` on the outbound
    model-copy, default-on, with ``X-Proxy-Search-Enrichment: off`` as the
    per-request opt-out.
    """

    async def _govern(
        self, msgs: list[dict[str, Any]], headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        from routes import _govern_messages

        request = types.SimpleNamespace(headers=headers)
        return await _govern_messages(request, msgs, model_key="professional", max_tokens=4096)

    @pytest.mark.asyncio
    async def test_enrichment_runs_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tools.web_search.execute_web_search", _fake_search)
        msgs = _search_messages(THIN_SNIPPET_JSON)
        out = await self._govern(msgs, {})
        assert "Rich answer for weather forecast in Kinver" in out[3]["content"]
        # DB audit copy source is untouched (input never mutated)
        assert msgs[2]["content"] == THIN_SNIPPET_JSON

    @pytest.mark.asyncio
    async def test_opt_out_header_disables_enrichment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _should_not_run(*args: Any, **kwargs: Any) -> str:
            raise AssertionError("enrichment must not run with opt-out header")

        monkeypatch.setattr("tools.web_search.execute_web_search", _should_not_run)
        msgs = _search_messages(THIN_SNIPPET_JSON)
        out = await self._govern(msgs, {"x-proxy-search-enrichment": "off"})
        assert out[3]["content"] == THIN_SNIPPET_JSON
