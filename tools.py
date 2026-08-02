"""
tools.py - Native Tool Executor & Web Search for Kinver Hub.

Handles:
- Live web search via local SearXNG instance with FlashRank CPU reranking
- Article ingestion via Trafilatura (HTML → Markdown)
- Central tool registry router (called by proxy.py for native tool execution)
- Response compression to protect limited LLM context windows

All I/O is async (httpx) and compatible with uvloop.  The SearXNG
instance is statically compiled and bound to localhost:8081.

IMPORTANT (Glass Pipe Rule):
    Tool execution does NOT inject ``load_role_prompt`` into frontend
    messages.  Proxy-internal prompts are generated within this module
    and are not derived from user-supplied text.

Usage::

    from tools import execute_tool

    result = await execute_tool(job, tool_call_dict, stream_feedback_cb, db, systemd)

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Optional, Any

import httpx
from flashrank import Ranker, RerankRequest
from trafilatura import extract

from constants import (
    DEPTH_CONFIG,
    SEARXNG_URL,
    get_logger,
)

from database import Database
from systemd import SystemdController

logger = get_logger("proxy.tools")

# ---------------------------------------------------------------------------
# FlashRank reranker — initialised lazily and cached (once)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _get_ranker() -> Optional[Ranker]:
    """Lazily initialise the FlashRank CPU reranker (cached after first load)."""
    try:
        ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2", cache_dir="/tmp/flashrank_cache")
        logger.info("FlashRank CPU reranker initialised")
        return ranker
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("Failed to initialise FlashRank: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Tool registry router — called by proxy.py for native tool execution
# ---------------------------------------------------------------------------

async def execute_tool(
    job: dict[str, Any],       # Job dict with keys: id, project_id, messages, tools, etc.
    tool_call_dict: dict[str, Any],
    stream_feedback_callback: Optional[Callable[[dict[str, Any], str], Awaitable[None]]] = None,  # async callback(job, message)
    database: Optional[Database] = None,     # Optional Database handle
    systemd: Optional[SystemdController] = None,  # Optional SystemdController
) -> str:
    """
    Central registry router for proxy-native tools.

    Dispatches the named tool with its arguments and returns the
    compressed result.  Currently supports ``web_search`` / ``search``
    / ``search_web``.

    Parameters
    ----------
    job : dict-like
        The job context (must have at least ``id`` and ``project_id``).
    tool_call_dict : dict
        The function dict from the LLM's tool call, with keys ``name``
        and ``arguments`` (JSON string or dict).
    stream_feedback_callback : callable or None
        Optional async ``(job, message)`` callback for streaming UI feedback.
    database : Database or None
        Optional database handle for caching search results.
    systemd : SystemdController or None
        Optional systemd controller (unused by the current web_search
        implementation).

    Returns
    -------
    str
        The compressed tool result text, or an error string.
    """
    name = tool_call_dict.get("name", "unknown_tool")

    # Parse arguments — they may be a JSON string or a dict
    args: dict[str, Any] = {}
    raw_args = tool_call_dict.get("arguments", "{}")
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {"query": raw_args}
    elif isinstance(raw_args, dict):
        args = raw_args

    # ---- Web search dispatch -----------------------------------------------
    if name in ("web_search", "search", "search_web"):
        query = args.get("query", "")
        depth = args.get("depth", "standard")

        if not query:
            return "[Search Error: No query provided.]"

        # Step 1: Execute web search + rerank
        if stream_feedback_callback:
            await stream_feedback_callback(
                job, f"Executing Native Web Search: '{query}' ({depth} depth)",
            )

        raw_markdown = await execute_web_search(query, depth)

        if raw_markdown.startswith("[Search"):
            return raw_markdown  # Error already formatted

        return raw_markdown

    return f"[Error: Native tool '{name}' not recognized.]"


# ---------------------------------------------------------------------------
# Web search pipeline: SearXNG → Trafilatura extraction → FlashRank rerank
# ---------------------------------------------------------------------------

async def execute_web_search(query: str, depth: str = "standard") -> str:
    """
    Core data pipeline for web discovery with semantic CPU reranking.

    1. Queries SearXNG for search results
    2. Concurrently scrapes each result URL with Trafilatura
    3. Reranks extracted passages with FlashRank (ms-marco-MiniLM)
    4. Returns formatted Markdown blocks of the top results

    Parameters
    ----------
    query : str
        The search query string.
    depth : str
        One of ``"fast"``, ``"standard"``, ``"deep"`` — determines
        how many results to fetch and how deeply to scrape.

    Returns
    -------
    str
        Markdown-formatted search results, or an error string.
    """
    ranker = _get_ranker()
    if ranker is None:
        return "[Search System Error: Reranker failed to initialize.]"

    cfg = DEPTH_CONFIG.get(depth.lower(), DEPTH_CONFIG["standard"])

    async with httpx.AsyncClient() as client:
        try:
            # ---- 1. SearXNG query -------------------------------------------
            resp = await client.get(
                SEARXNG_URL,
                params={"q": query, "format": "json", "count": cfg["count"]},
                timeout=10.0,
            )
            if resp.status_code != 200:
                return f"[Search Error: HTTP {resp.status_code} from SearXNG]"

            results = resp.json().get("results", [])[: cfg["count"]]
            if not results:
                return "[Search returned no URLs to scrape.]"

            # ---- 2. Concurrent article scraping -----------------------------
            tasks = [
                _scrape_article(client, res["url"], cfg["chars"])
                for res in results
            ]
            pages = await asyncio.gather(*tasks)
            candidates = [p for p in pages if p]

            if not candidates:
                return "[Search failed: Could not extract readable text.]"

            # ---- 3. FlashRank reranking -------------------------------------
            rerank_request = RerankRequest(query=query, passages=candidates)
            ranked = ranker.rerank(rerank_request)

            # ---- 4. Format output -------------------------------------------
            formatted_blocks: list[str] = []
            for i, r in enumerate(ranked[: cfg["gold"]]):
                source_url = (
                    r["meta"]["url"]
                    if "meta" in r and "url" in r["meta"]
                    else r.get("id", f"source-{i}")
                )
                formatted_blocks.append(
                    f"### Source {i + 1}: {source_url}\n{r['text']}"
                )

            return "\n\n---\n\n".join(formatted_blocks)

        except httpx.RequestError:
            return (
                f"[Search Execution Failed: Could not connect to SearXNG "
                f"instance at {SEARXNG_URL}.]"
            )


async def _scrape_article(
    client: httpx.AsyncClient,
    url: str,
    char_limit: int,
) -> Optional[dict]:
    """
    Fetch a URL and extract readable text via Trafilatura.

    Returns a dict with keys ``id``, ``text``, ``meta`` suitable for
    FlashRank reranking, or None on failure.
    """
    try:
        resp = await client.get(url, timeout=4.0, follow_redirects=True)
        text = extract(
            resp.text,
            output_format="markdown",
            include_tables=True,
        )
        if text:
            return {
                "id": url,
                "text": text[:char_limit],
                "meta": {"url": url},
            }
    except (httpx.HTTPError, ValueError, TypeError, IndexError):
        logger.debug("Article scrape failed for %s", url)
    return None


# ---------------------------------------------------------------------------
# Response compressor (truncate + ellipsis)
# ---------------------------------------------------------------------------

def compress_response(raw_text: str, max_chars: int = 2000) -> str:
    """
    Truncate *raw_text* to *max_chars* with an ellipsis if cut.

    Used to protect LLM context windows from search-result bloat.
    """
    if len(raw_text) <= max_chars:
        return raw_text
    return raw_text[:max_chars] + "\n\n[...truncated...]"