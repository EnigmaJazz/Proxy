"""
tools.py - Native Tool Executor & Web Search for Kinver Hub.

Handles:
- Live web search via local SearXNG instance with FlashRank CPU reranking
- Article ingestion via Trafilatura (HTML → Markdown)
- Lifeboat Reflexion loop for search result synthesis & self-audit
- Central tool registry router (called by proxy.py for native tool execution)
- Response compression to protect limited LLM context windows

All I/O is async (httpx) and compatible with uvloop.  The SearXNG
instance is statically compiled and bound to localhost:8081.

IMPORTANT (Glass Pipe Rule):
    Tool execution does NOT inject ``load_role_prompt`` into frontend
    messages.  Proxy-internal prompts (lifeboat synthesis, auditor
    evaluation) are generated within this module and are not derived
    from user-supplied text.

Usage::

    from tools import execute_tool

    result = await execute_tool(job, tool_call_dict, stream_feedback_cb, db, systemd)

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional, Dict, Any, List

import httpx
from flashrank import Ranker, RerankRequest
from trafilatura import extract

from constants import (
    DEPTH_CONFIG,
    SEARXNG_URL,
    get_logger,
)

logger = get_logger("proxy.tools")

# ---------------------------------------------------------------------------
# FlashRank reranker — initialised once at module load
# ---------------------------------------------------------------------------
_RANKER = None
try:
    _RANKER = Ranker(model_name="ms-marco-MiniLM-L-12-v2", cache_dir="/tmp/flashrank_cache")
    logger.info("FlashRank CPU reranker initialised")
except Exception as exc:
    logger.error("Failed to initialise FlashRank: %s", exc)


# ---------------------------------------------------------------------------
# Tool registry router — called by proxy.py for native tool execution
# ---------------------------------------------------------------------------

async def execute_tool(
    job,                       # Job dict with keys: id, project_id, messages, tools, etc.
    tool_call_dict: dict,
    stream_feedback_callback=None,  # Optional async callback(job, message)
    database=None,             # Optional Database handle
    systemd=None,              # Optional SystemdController
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
        Used to resolve the lifeboat/reasoning model ports.

    Returns
    -------
    str
        The compressed tool result text, or an error string.
    """
    name = tool_call_dict.get("name", "unknown_tool")

    # Parse arguments — they may be a JSON string or a dict
    args: dict = {}
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

        # Step 2: Synthesize results via Lifeboat Reflexion loop
        if stream_feedback_callback:
            await stream_feedback_callback(
                job, "Synthesizing research via RAM-resident Lifeboat & Reasoning...",
            )

        if systemd is not None:
            lifeboat_port = await systemd.get_port("lifeboat")
            reasoning_port = await systemd.get_port("reasoning")
        else:
            # Fallback ports
            lifeboat_port = 8090
            reasoning_port = 8085

        summary = await lifeboat_reflexion_loop(
            raw_markdown, query, depth, lifeboat_port, reasoning_port,
        )
        return summary

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
    if _RANKER is None:
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
            ranked = _RANKER.rerank(rerank_request)

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
    except Exception:
        logger.debug("Article scrape failed for %s", url)
    return None


# ---------------------------------------------------------------------------
# Lifeboat Reflexion loop — CPU-bound summarization + self-audit
# ---------------------------------------------------------------------------

async def lifeboat_reflexion_loop(
    raw_markdown: str,
    query: str,
    depth: str,
    lifeboat_port: int = 8090,
    reasoning_port: int = 8085,
) -> str:
    """
    CPU-bound Reflexion loop: summarize search results and self-audit
    using the Lifeboat and Reasoning models.

    The Lifeboat model generates a summary.  The Reasoning model
    evaluates it.  If the evaluation is not ``OK``, the feedback is
    appended to the Lifeboat prompt and a new summary is generated.
    Up to 3 attempts.

    Parameters
    ----------
    raw_markdown : str
        The raw search results in Markdown format.
    query : str
        The original user query.
    depth : str
        Search depth profile name.
    lifeboat_port : int
        TCP port of the Lifeboat model.
    reasoning_port : int
        TCP port of the Reasoning (Auditor) model.

    Returns
    -------
    str
        The best summary obtained across attempts.
    """
    from llm import call_model

    cfg = DEPTH_CONFIG.get(depth.lower(), DEPTH_CONFIG["standard"])

    # Build the initial Lifeboat prompt — this is PROXY-INTERNAL
    lb_prompt = (
        f"You are a research synthesizer. Summarize the search results below "
        f"in a clear, factual manner. Limit your response to a maximum of "
        f"{cfg['summary_words']} words. Prioritize raw facts over commentary.\n\n"
        f"Query: {query}\n\n"
        f"Search Results:\n{raw_markdown}"
    )

    best_summary = ""

    for attempt in range(3):
        # Generate summary via Lifeboat (CPU model)
        summary = await call_model(
            lifeboat_port,
            lb_prompt,
            profile="analytical",
            max_tokens=1024,
        )

        if not summary:
            continue

        best_summary = summary

        # Evaluate the summary via Reasoning model
        auditor_prompt = (
            "You are a search result auditor. Evaluate the summary below "
            "against the original search context. Reply with exactly:\n"
            "- OK (if the summary is accurate and complete)\n"
            "- FEEDBACK: <specific issues to fix>\n\n"
            f"Original Context:\n{raw_markdown[:2000]}...\n\n"
            f"Summary to Evaluate:\n{summary}"
        )

        evaluation = await call_model(
            reasoning_port,
            auditor_prompt,
            profile="deterministic",
            max_tokens=80,
        )

        if evaluation.strip().upper().startswith("OK"):
            return summary

        # Append feedback and retry
        lb_prompt += (
            f"\n\n[Previous attempt feedback: {evaluation.strip()}. "
            "Please rewrite the summary to address these issues.]"
        )

    # Return the best attempt even if none passed audit
    return best_summary if best_summary else "[Search synthesis failed after 3 attempts.]"


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