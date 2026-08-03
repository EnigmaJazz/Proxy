"""
tools.py - Native Tool Executor & Web Search for Kinver Hub.  (LEGACY)

NOTE: the ``tools/`` package shadows this module at import time, so the
live tool registry and the search pipeline live in the package:
``tools/web_search.py``.  This file is kept as the documented registry
router and reuses the package implementation.  Production code should
import ``from tools.web_search import execute_web_search``.

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

Maintainers: James Stansfield
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Optional, Any

from constants import get_logger
from database import Database
from systemd import SystemdController
from tools.web_search import execute_web_search

logger = get_logger("proxy.tools")


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
