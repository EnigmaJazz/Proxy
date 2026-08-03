"""
search_enrichment.py — enrich thin frontend web-search results (OUTBOUND copy only).

Frontends own tool execution: OpenWebUI / nanobot run ``search_web`` /
``web_search`` themselves and feed the result back as a ``role: "tool"``
message on the next request.  Those results are frequently thin SEO
snippets (a JSON array of ``{title, link, snippet}``), so the model punts
("I can't access real-time data") instead of answering from live content.

This module detects that pattern on the OUTBOUND model-copy and, when the
result is thin, runs the proxy's own rich search pipeline
(``tools.web_search.execute_web_search`` — SearXNG + Trafilatura +
FlashRank) and APPENDS the rich Markdown to the tool result, so the model
has real content to answer from.

Glass Pipe scope — explicit user-approved extension of the R1 carve-out
(2026-08-03, "must only change the proxy"):
  - Appends only; never replaces client content.
  - Never touches user or assistant text.
  - Never mutates the client's stored conversation or the DB audit copy
    (the transform runs on the governed copy built by
    ``routes._govern_messages``).
  - Fires only for search-type tool results that are thin.
  - Per-request opt-out: ``X-Proxy-Search-Enrichment: off``.
  - Failures degrade gracefully (original result preserved).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from constants import get_logger

logger = get_logger("kinver.search_enrichment")

# Tool names treated as web search.  Must match tools.py dispatch names.
SEARCH_TOOL_NAMES: frozenset[str] = frozenset({"search_web", "web_search", "search"})

# Below this many chars a search result is considered "thin" and eligible.
THIN_RESULT_CHARS: int = 1_200

# Marker embedded in enriched results; used for idempotence (a result that
# already carries it is never re-enriched).
_ENRICHED_MARKER = "[Proxy-enriched search results for"


def _tool_args(tool_call: dict[str, Any]) -> dict[str, Any]:
    """Parse a tool_call's ``arguments`` (JSON string or dict)."""
    fn = tool_call.get("function") or {}
    raw = fn.get("arguments", "{}")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _is_thin_result(content: str) -> bool:
    """True when a client search result carries little usable content."""
    if _ENRICHED_MARKER in content:
        return False  # already enriched on a previous pass — idempotent
    stripped = content.strip()
    if not stripped:
        return True  # empty result — worth enriching
    # OpenWebUI-style snippet array: [{title, link, snippet}, ...]
    if stripped.startswith("[") and ('"snippet"' in stripped or '"title"' in stripped):
        return True
    return len(stripped) < THIN_RESULT_CHARS


def _find_thin_search_results(
    messages: list[dict[str, Any]],
) -> list[tuple[int, str, str]]:
    """Return ``(tool_result_index, query, depth)`` for thin search results.

    A pair is an assistant message with a search-type ``tool_calls`` entry
    followed by its ``role: "tool"`` result.  OpenAI ordering semantics:
    the tool results arrive in the same order as the calls in the batch.
    """
    pairs: list[tuple[int, str, str]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if (
            msg.get("role") == "assistant"
            and isinstance(msg.get("tool_calls"), list)
            and msg["tool_calls"]
        ):
            calls = msg["tool_calls"]
            searches: dict[int, tuple[str, str]] = {}
            for offset, tc in enumerate(calls):
                fn = tc.get("function") or {}
                if fn.get("name") in SEARCH_TOOL_NAMES:
                    args = _tool_args(tc)
                    query = args.get("query")
                    if isinstance(query, str) and query.strip():
                        searches[offset] = (
                            query.strip(),
                            str(args.get("depth", "standard")),
                        )
            # Consume the next len(calls) role:"tool" messages in order.
            j = i + 1
            consumed = 0
            while j < n and consumed < len(calls):
                m = messages[j]
                if m.get("role") != "tool":
                    break  # result sequence interrupted — stop pairing
                if consumed in searches:
                    query, depth = searches[consumed]
                    content = m.get("content")
                    if isinstance(content, str) and _is_thin_result(content):
                        pairs.append((j, query, depth))
                consumed += 1
                j += 1
            i = max(i + 1, j)  # skip consumed results to avoid re-scanning
        else:
            i += 1
    return pairs


async def enrich_thin_search_results(
    messages: list[dict[str, Any]],
    *,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` with thin search results enriched.

    The input list is never mutated.  With ``enabled=False`` (or no
    eligible thin results) the input list is returned as-is.
    """
    if not enabled or not messages:
        return messages
    pairs = _find_thin_search_results(messages)
    if not pairs:
        return messages

    # Lazy import: the search pipeline pulls in FlashRank + Trafilatura,
    # which should only load when there is actually something to enrich.
    from tools.web_search import execute_web_search

    async def _enrich_one(query: str, depth: str) -> Optional[str]:
        try:
            return await execute_web_search(query, depth)
        except Exception:  # noqa: BLE001 — enrichment must never break the request
            logger.exception("Search enrichment failed for query %r", query)
            return None

    enriched = await asyncio.gather(
        *(_enrich_one(query, depth) for _, query, depth in pairs)
    )

    enriched_messages = [dict(m) for m in messages]
    for (index, query, _depth), rich in zip(pairs, enriched):
        if not rich:
            continue
        if rich.startswith("[Search") or rich.startswith("[Search Error"):
            continue
        msg = enriched_messages[index]
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        msg["content"] = (
            f"{content}\n\n---\n{_ENRICHED_MARKER} '{query}']\n{rich}"
        )
    return enriched_messages
