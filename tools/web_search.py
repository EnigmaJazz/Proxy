"""
tools/web_search.py — native web search pipeline (SearXNG → Trafilatura → FlashRank).

This module lives inside the ``tools`` package so it is importable as
``from tools.web_search import execute_web_search``.  (The repository root
also contains a legacy ``tools.py``; the ``tools/`` package shadows that
module at import time, so production code must import the package path.)

Pipeline:
    1. Query the local SearXNG instance (JSON format).
    2. Concurrently scrape each result URL with Trafilatura (HTML → Markdown).
    3. Rerank extracted passages with FlashRank (ms-marco-MiniLM-12-v2).
    4. Return formatted Markdown blocks of the top results.

Used by:
    - ``search_enrichment`` — enriches thin frontend search_web results on
      the OUTBOUND model-copy (the only current production caller).
    - ``tools.py`` — legacy registry router (kept for reference).

All I/O is async (httpx) and compatible with uvloop.
"""
from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any, Optional

import httpx
from flashrank import Ranker, RerankRequest
from trafilatura import extract

from constants import DEPTH_CONFIG, SEARXNG_URL, get_logger

logger = get_logger("proxy.tools.web_search")


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
