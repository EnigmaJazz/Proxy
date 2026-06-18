"""
routing.py - Lane A/B dispatch, frontdesk classifier & semantic loop detection.

This module is the brain of the proxy's request routing.  It decides
which model should handle a given request and whether the GPU is free
or a CPU fallback (Lifeboat) is needed.

Key responsibilities:
- **Lane discrimination**: IDE keys (``sk-ide-pass`` header) → Lane B,
  everything else → Lane A.
- **Frontdesk classification**: Lane A requests are first classified by
  the 2B frontdesk model (JSON-GBNF) to determine intent, priority,
  project, and whether the query is factual (cacheable).
- **GPU-aware routing**: For CHAT/TOOL intents, if the GPU is busy with
  a heavy model, route to CPU Lifeboat immediately without pausing
  the queue.  Heavy intents (CODE/SCHOLAR/etc.) go to the queue.
- **Tool-loop detection**: Uses database-backed semantic search
  (sqlite-vec) with a difflib fallback to detect repetitive tool calls
  and prevent agentic death spirals.
- **Semantic cache**: Factual queries are checked against the
  sqlite-vec cache before waking the GPU.

Usage::

    from routing import RouteDecision, resolve_route, classify_with_frontdesk

    classification = await classify_with_frontdesk(user_text)
    decision = await resolve_route(classification, is_lane_b, systemd, db)

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import difflib
import glob
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, FrozenSet

from constants import (
    FRONTEND_KEYS,
    IDE_PASSTHROUGH_HEADER,
    get_logger,
)

logger = get_logger("proxy.routing")

# ---------------------------------------------------------------------------
# Dream/soul detection — discovers Nanobot dream template phrases
# ---------------------------------------------------------------------------

# Default fallback phrases extracted from Nanobot dream_phase1.md template.
# These are used if the installed Nanobot package cannot be found or read.
# The phrases are unique to Nanobot's dream/autonomous consolidation prompt
# and do NOT appear in normal conversational requests — making them a
# reliable fingerprint for background autonomous tasks.
_DREAM_FALLBACK_PHRASES: list[str] = [
    "extract new facts from conversation history",
    "output one line per finding",
    "deduplicate existing memory files",
    "atomic fact (not already in memory)",
    "[file] atomic fact",
    "[file-remove] reason for removal",
    "[skill] kebab-case-name",
    "[skip] if nothing needs updating",
    "find and flag redundant, overlapping, or stale content",
    "update memory files based on the analysis below",
]

# Lazy cache with mtime staleness checks so dream detection survives
# Nanobot updates (`uv tool upgrade nanobot-ai`).  The glob pattern
# uses a wildcard Python version path since uv-managed packages move
# between python3.13/, python3.14/ etc. across updates.
_dream_cache: dict = {"phrases": None, "mtime": 0.0, "path": ""}

def _get_dream_phrases() -> list[str]:
    """
    Discover the latest Nanobot dream_phase1.md template and extract
    unique identifying phrases for dream/soul task detection.

    Uses lazy caching with file mtime checks so that the phrases are
    automatically refreshed when Nanobot is updated after proxy startup.

    Returns
    -------
    list[str]
        Lowercase phrases unique to Nanobot dream prompts.
        Falls back to _DREAM_FALLBACK_PHRASES if the template is
        inaccessible.
    """
    # Glob with wildcard Python version — survives uv updates
    pattern = (
        "/home/james/.local/share/uv/tools/nanobot-ai/lib/python*/"
        "site-packages/nanobot/templates/agent/dream_phase1.md"
    )
    paths = sorted(glob.glob(pattern))
    if not paths:
        logger.debug("Dream template not found — using fallback phrases")
        return _DREAM_FALLBACK_PHRASES

    latest_path = paths[-1]
    try:
        mtime = os.path.getmtime(latest_path)
    except OSError:
        return _DREAM_FALLBACK_PHRASES

    # Reuse cached phrases if the file hasn't changed
    if _dream_cache["path"] == latest_path and _dream_cache["mtime"] == mtime:
        return _dream_cache["phrases"]  # type: ignore[return-value]

    # Read the template and extract distinct phrases (lines > 15 chars,
    # non-comment, non-empty) as lowercase for substring matching.
    try:
        with open(latest_path, "r") as f:
            text = f.read().lower()
    except OSError:
        return _DREAM_FALLBACK_PHRASES

    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip("- #*▶").strip()
        if len(stripped) > 15:
            lines.append(stripped)

    _dream_cache["phrases"] = lines
    _dream_cache["mtime"] = mtime
    _dream_cache["path"] = latest_path
    logger.debug(
        "Dream phrases refreshed from %s (%d phrases)", latest_path, len(lines),
    )
    return lines


def is_dream_process(raw_text: str) -> bool:
    """
    Detect whether *raw_text* originates from a Nanobot dream/autonomous
    background task by matching against fingerprint phrases from the
    dream_phase1.md template.

    Dream template phrases are unique to the autonomous memory
    consolidation prompt — they do NOT appear in regular Nanobot chat.
    Matching 2+ of these phrases in the user message provides a
    high-confidence signal that this is an autonomous background task
    that should be routed at BACKGROUND priority.

    Parameters
    ----------
    raw_text : str
        Lowercased concatenation of all user message content.

    Returns
    -------
    bool
        True if the text matches dream-specific template phrases.
    """
    if not raw_text:
        return False

    phrases = _get_dream_phrases()
    matches = 0
    for phrase in phrases:
        if phrase in raw_text:
            matches += 1
            if matches >= 2:
                logger.info(
                    "Dream process detected: matched %d template phrases", matches,
                )
                return True

    return False


# ---------------------------------------------------------------------------
# Tool keyword heuristic — safety net when frontdesk misclassifies CHAT
# ---------------------------------------------------------------------------

# Keywords that indicate a request likely needs tool access (web search,
# file I/O, or system exec).  Over-detection is safe because the Worker
# model can handle plain chat just as well as Chatter.
# All matching is done lowercase with substring matching.
TOOL_KEYWORDS: FrozenSet[str] = frozenset({
    # Weather / temporal — need live data
    "weather", "forecast", "tomorrow", "tonight", "next week",
    "this weekend", "next month", "today's",
    # Web search / live data
    "search", "look up", "latest", "news", "current",
    "stock", "price",
    # File I/O
    "read the file", "read file", "open file", "save file",
    "write file", "edit file", "modify", "rename",
    # Exec / system
    "run command", "execute",
})

# ---------------------------------------------------------------------------
# ROUTE_MAP: classified intent → local model endpoint key
#
#   CHAT      → chatter  (9B, fast chat model)
#   TOOL      → worker   (9B, tool-capable model)
#   CODE      → professional (35B MoE, heavy coding model)
#   SCHOLAR   → scholar  (deep research)
#   PROFESSIONAL → professional (professional writing / 35B MoE)
#   CREATIVE  → creative (long-form creative writing)
#   ARCHITECT → architect (complex multi-stage planning)
#
#   Lane B (IDE passthrough) ALWAYS goes to professional regardless
#   of intent — the frontdesk is bypassed entirely.
# ---------------------------------------------------------------------------
ROUTE_MAP: Dict[str, str] = {
    "CHAT":         "chatter",
    "TOOL":         "worker",
    "CODE":         "professional",  # 35B MoE
    "SCHOLAR":      "scholar",
    "PROFESSIONAL": "professional",
    "CREATIVE":     "creative",
    "ARCHITECT":    "architect",
}

# Heavy GPU models (require VRAM allocation, can't be quickly swapped)
HEAVY_MODELS: set[str] = {"professional", "coder", "creative", "scholar", "architect"}

# CPU-only models (always resident, never hot-swapped)
CPU_MODELS: set[str] = {"reasoning", "lifeboat", "frontdesk"}

# ---------------------------------------------------------------------------
# RouteDecision dataclass
# ---------------------------------------------------------------------------


@dataclass
class RouteDecision:
    """
    The result of the routing decision for a single request.

    Attributes
    ----------
    model_key : str
        The endpoint key passed to ``stream_llm()`` (resolved via
        ``LLAMA_ENDPOINTS`` in llm.py for the actual HTTP URL).
    port : int
        The TCP port the model listens on.
    is_cpu_fallback : bool
        True if this route uses the CPU Lifeboat model because the GPU
        is occupied.
    hardware_path : str
        ``"cpu"``, ``"gpu"``, or ``"none"`` (for cloud) — used by the
        cooling state machine.
    priority : int
        1=HIGH, 2=NORMAL, 3=BACKGROUND.
    intent : str
        Classified intent (CHAT, CODE, TOOL, etc.).
    project_id : str
        Slugified project name.
    is_factual : bool
        True if the query is a simple factual question (cacheable).
    is_lane_b : bool
        True if this is an IDE passthrough request.
    bypass_frontdesk : bool
        True if the frontdesk classification was skipped (Lane B).
    tools_required : bool
        True if the task requires web search, file I/O, or shell commands.
    """
    model_key: str = "chatter"
    port: int = 8083
    is_cpu_fallback: bool = False
    hardware_path: str = "gpu"
    priority: int = 2
    intent: str = "CHAT"
    project_id: str = "general"
    is_factual: bool = False
    is_lane_b: bool = False
    bypass_frontdesk: bool = False
    tools_required: bool = False


# ---------------------------------------------------------------------------
# Caller type discrimination
# ---------------------------------------------------------------------------

def discriminate_caller(headers: dict) -> str:
    """
    Categorize the incoming request as IDE or AGENTIC based on auth
    headers and user-agent.

    Parameters
    ----------
    headers : dict
        The HTTP request headers.

    Returns
    -------
    str
        ``"IDE"`` or ``"AGENTIC"``.
    """
    # Check authorization header for known API tokens
    auth_header = headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        if token in FRONTEND_KEYS:
            return FRONTEND_KEYS[token]

    # Check user-agent for known IDE/agent signatures
    user_agent = headers.get("user-agent", "").lower()
    # Nanobot is intentionally NOT in ide_agents — it is AGENTIC so that
    # dream/soul background task detection works correctly inside the
    # AGENTIC-only branch.  Classifying it as IDE would cause every
    # Nanobot request (which always references soul.md/memory.md/user.md
    # in its memory bank) to be misdetected as a dream process.
    ide_agents = {"aider", "cline", "vscode", "opencode"}
    for agent in ide_agents:
        if agent in user_agent:
            return "IDE"

    return "AGENTIC"


# ---------------------------------------------------------------------------
# Lane detection
# ---------------------------------------------------------------------------

def is_lane_b(headers: dict) -> bool:
    """
    Return True if the ``sk-ide-pass`` header is present and matches
    the expected value — this triggers Lane B (IDE coding, bypasses
    frontdesk, routes directly to 35B MoE).
    """
    ide_header = headers.get(IDE_PASSTHROUGH_HEADER, "")
    return ide_header == IDE_PASSTHROUGH_HEADER


# ---------------------------------------------------------------------------
# Front-Desk classifier (Lane A only)
#   Calls the 2B frontdesk model to classify user intent.
#   This is a PROXY-INTERNAL call — the frontdesk prompt is generated
#   by the proxy, not injected from the client.
# ---------------------------------------------------------------------------

# The frontdesk prompt is loaded from disk via load_role_prompt.
# This allows the prompt to be iterated on without touching proxy code.
# The placeholder {project_list} is injected at runtime with the
# current project roster from the database.
_FRONT_DESK_BASE_PROMPT: Optional[str] = None


def _get_frontdesk_prompt() -> str:
    """
    Lazily load the frontdesk prompt from disk.

    Uses ``load_role_prompt("frontdesk")`` which reads from
    ``/home/james/kinver-hub/prompts/frontdesk.txt``.
    Cached after first load since the prompt doesn't change at runtime.
    """
    global _FRONT_DESK_BASE_PROMPT
    if _FRONT_DESK_BASE_PROMPT is None:
        from llm import load_role_prompt
        _FRONT_DESK_BASE_PROMPT = load_role_prompt("frontdesk")
    return _FRONT_DESK_BASE_PROMPT


async def classify_with_frontdesk(
    user_text: str,
    database=None,  # Optional Database for project list injection
    frontdesk_port: int = 0,
    available_tool_names: Optional[list[str]] = None,
) -> dict:
    """
    Run the 2B frontdesk model to classify the user's intent.

    The full conversation context (last N messages, newest-first
    truncation) is passed so the 2B model can reference earlier
    exchanges when classifying intent and complexity.

    Parameters
    ----------
    user_text : str
        The full conversation context (not just the latest message).
        Should be pre-built by the caller with newest-first truncation.
    database : Database or None
        Used to inject the current project roster into the prompt.
    frontdesk_port : int
        TCP port of the frontdesk llama.cpp server, resolved live from
        the systemd unit file by the caller.  When ``> 0``, passed
        through to ``call_llm()`` which constructs the URL directly,
        bypassing the hard-coded ``LLAMA_ENDPOINTS`` fallback.  Defaults
        to 0 (uses the dict fallback).
    available_tool_names : list[str] or None
        Names of tools provided by the client (e.g. ``["web_search"]``).
        Injected into the frontdesk prompt so the 2B model knows what
        tools are available and can accurately decide whether
        ``tools_required`` should be true.  Without this context the
        2B model cannot know that e.g. "what's the weather?" needs
        web_search.

    Returns
    -------
    dict
        Classification result with keys: is_valid, intent, priority,
        complexity, project_name, is_factual, tools_required.
        Falls back to safe defaults on failure.
    """
    defaults: dict = {
        "is_valid": True,
        "intent": "CHAT",
        "priority": 2,
        "complexity": "low",
        "project_name": "general",
        "is_factual": False,
        "tools_required": False,
    }

    try:
        # ---- Build the full prompt with project list injected --------------
        base_prompt = _get_frontdesk_prompt()

        # Build project list string for injection
        project_list_str = "No existing projects."
        if database:
            try:
                projects = await database.get_all_projects()
                if projects:
                    lines = []
                    for p in projects:
                        name = p.get("display_name", p["id"])
                        root = p.get("root_path", "")
                        lines.append(
                            f"- {p['id']}: {name}"
                            + (f" ({root})" if root else "")
                        )
                    project_list_str = "\n".join(lines)
            except Exception:
                logger.debug("Failed to fetch project list (non-critical)")

        # Inject project list into prompt
        prompt = base_prompt.replace("{project_list}", project_list_str)

        # ---- Call the frontdesk model via the legacy /completion endpoint ----
        # The 2B frontdesk model uses raw completions (not chat API) for
        # structured JSON-GBNF output.  This is the same endpoint used by
        # the ZRAM keepalive worker and other CPU-bound classifiers.
        from llm import call_model

        # Build the full prompt: system instructions + user request.
        # NOTE: available_tool_names are NOT injected into the prompt.
        # Injecting conversational tool hints confuses the 2B model into
        # answering the user's request directly instead of outputting JSON.
        # Tool-need detection is handled deterministically by the
        # TOOL_KEYWORDS heuristic in routes.py.
        full_prompt = prompt + "\n\nUser request: " + user_text

        content = await call_model(
            port=frontdesk_port,
            prompt=full_prompt,
            profile="json_gbnf",       # Enforce structured JSON output
            max_tokens=256,
        )
        content = content.strip()

        # Strip markdown fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0]

        # Use raw_decode to extract ONLY the first complete JSON object.
        # The 2B frontdesk model sometimes outputs concatenated objects
        # (e.g. {"intent":"CHAT","priority":2}{"extra":"garbage"}) which
        # would cause json.loads() to fail or produce malformed values
        # like "priority": "}{" leaking into the user-visible triage message.
        # raw_decode returns (parsed_dict, end_index) and ignores trailing junk.
        decoder = json.JSONDecoder()
        classification, _ = decoder.raw_decode(content)

        # Normalize priority: the GBNF schema defines it as a string but
        # the frontdesk prompt says integers.  Handle both cases so that
        # named strings ("high", "normal", "background") or malformed
        # values never leak into user-visible output.
        raw_priority = classification.get("priority", 2)
        if isinstance(raw_priority, str):
            raw_lower = raw_priority.strip().lower()
            if raw_lower in ("high", "1"):
                classification["priority"] = 1
            elif raw_lower in ("background", "low", "3"):
                classification["priority"] = 3
            elif raw_lower in ("normal", "2"):
                classification["priority"] = 2
            else:
                # Unrecognized string — try int conversion, fallback to 2
                try:
                    classification["priority"] = int(raw_lower)
                except (ValueError, TypeError):
                    classification["priority"] = 2
        elif isinstance(raw_priority, (int, float)):
            classification["priority"] = int(raw_priority)
        else:
            classification["priority"] = 2

        # Merge with defaults to ensure all 7 keys are present
        return {**defaults, **classification}

    except Exception:
        logger.exception("Frontdesk classification failed — using defaults")
        return defaults


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def resolve_model(intent: str, is_lane_b: bool = False) -> str:
    """
    Given a classified intent and lane, return the llama.cpp service key.

    Lane B always uses the 35B MoE (professional) regardless of intent.
    Lane A uses the ROUTE_MAP.
    """
    if is_lane_b:
        return "professional"
    return ROUTE_MAP.get(intent, "chatter")


# ---------------------------------------------------------------------------
# GPU-aware routing for Lane A (Lifeboat fallback logic)
# ---------------------------------------------------------------------------

async def resolve_route_for_lane_a(
    classification: dict,
    systemd,  # SystemdController
    has_tool_history: bool = False,  # True if conversation has tool_calls
) -> RouteDecision:
    """
    Determine the target model for a Lane A request, taking GPU
    occupancy into account.

    **CHAT / TOOL with GPU busy**: Falls back to CPU Lifeboat immediately.
    The queue is NOT paused — these requests are handled synchronously
    without GPU contention.

    **Heavy intents (CODE/SCHOLAR/PROFESSIONAL/CREATIVE/ARCHITECT)**:
    These require the GPU and are enqueued.  The queue worker handles
    ordering and escalation.

    Parameters
    ----------
    classification : dict
        Frontdesk classification result (intent, priority, project, etc.).
    systemd : SystemdController
        Used for port resolution and GPU occupancy checks.

    Returns
    -------
    RouteDecision
        The resolved routing decision.
    """
    intent = classification.get("intent", "CHAT")
    priority = classification.get("priority", 2)
    project_name = classification.get("project_name", "general")
    tools_required = classification.get("tools_required", False)
    is_factual = classification.get("is_factual", False)
    complexity = classification.get("complexity", "low")

    model_key = ROUTE_MAP.get(intent, "chatter")

    # ---- Low-complexity CHAT — try Chatter first, fallback to Lifeboat ----
    if intent == "CHAT" and complexity == "low":
        if not systemd.is_gpu_occupied():
            port = await systemd.get_port("chatter")
            logger.info("Routing CHAT → chatter (GPU free, port %d)", port)
            return RouteDecision(
                model_key="chatter",
                port=port,
                is_cpu_fallback=False,
                hardware_path="gpu",
                priority=priority,
                intent=intent,
                project_id=_slugify(project_name),
                is_factual=is_factual,
                tools_required=tools_required,
            )
        else:
            # GPU is busy with a heavy model → immediate CPU Lifeboat
            port = await systemd.get_port("lifeboat")
            logger.info(
                "Routing CHAT → lifeboat (GPU occupied by %s, port %d)",
                systemd.active_heavy_model, port,
            )
            return RouteDecision(
                model_key="lifeboat",
                port=port,
                is_cpu_fallback=True,
                hardware_path="cpu",
                priority=priority,
                intent=intent,
                project_id=_slugify(project_name),
                is_factual=is_factual,
                tools_required=tools_required,
            )

    # ---- TOOL execution — try Worker first, fallback to Lifeboat ----
    if intent == "TOOL":
        if not systemd.is_gpu_occupied():
            port = await systemd.get_port("worker")
            logger.info("Routing TOOL → worker (GPU free, port %d)", port)
            return RouteDecision(
                model_key="worker",
                port=port,
                is_cpu_fallback=False,
                hardware_path="gpu",
                priority=priority,
                intent=intent,
                project_id=_slugify(project_name),
                is_factual=is_factual,
                tools_required=tools_required,
            )
        else:
            # GPU is occupied by a heavy model.  If the conversation already
            # has tool_call history, we CANNOT fall back to Lifeboat — its
            # chat template (Jinja) rejects messages with tool_calls/tool_call_id
            # fields, producing "Only text chunks are supported" errors.
            # Instead, keep Worker on GPU by using its port directly (Worker
            # is always running alongside heavy models as a lightweight service).
            if has_tool_history:
                port = await systemd.get_port("worker")
                logger.info(
                    "Routing TOOL → worker (GPU occupied by %s, but has tool "
                    "history — Lifeboat template would reject tool_calls, "
                    "so staying on Worker, port %d)",
                    systemd.active_heavy_model, port,
                )
                return RouteDecision(
                    model_key="worker",
                    port=port,
                    is_cpu_fallback=False,
                    hardware_path="gpu",
                    priority=priority,
                    intent=intent,
                    project_id=_slugify(project_name),
                    is_factual=is_factual,
                    tools_required=True,
                )

            # No tool history — safe to use Lifeboat
            port = await systemd.get_port("lifeboat")
            logger.info(
                "Routing TOOL → lifeboat (GPU occupied by %s, port %d)",
                systemd.active_heavy_model, port,
            )
            return RouteDecision(
                model_key="lifeboat",
                port=port,
                is_cpu_fallback=True,
                hardware_path="cpu",
                priority=priority,
                intent=intent,
                project_id=_slugify(project_name),
                is_factual=is_factual,
                tools_required=tools_required,
            )

    # ---- Heavy intents — route to GPU model, enqueue ----
    port = await systemd.get_port(model_key)
    logger.info(
        "Routing %s → %s (heavy model, port %d)",
        intent, model_key, port,
    )
    return RouteDecision(
        model_key=model_key,
        port=port,
        is_cpu_fallback=False,
        hardware_path="gpu",
        priority=priority,
        intent=intent,
        project_id=_slugify(project_name),
        is_factual=is_factual,
        tools_required=tools_required,
    )


# ---------------------------------------------------------------------------
# Semantic loop detection (sqlite-vec with difflib fallback)
# ---------------------------------------------------------------------------

# Maximum tool call repetitions allowed per domain before breaking the loop
LOOP_LIMITS: Dict[str, int] = {
    "scholar":      6,
    "architect":    4,
    "coder":        4,
    "professional": 4,
    "creative":     5,
    "standard":     3,
    "worker":       2,
    "CHAT":         2,
    "TOOL":         2,
}


async def detect_tool_loops(
    messages: list[dict],
    domain: str = "standard",
    database=None,  # Optional Database for semantic search
) -> tuple[bool, str]:
    """
    Detect repetitive tool call patterns that indicate an agentic death
    spiral (the model calling the same tool with near-identical args
    repeatedly).

    Uses database-backed semantic search if available, falling back to
    difflib string similarity.

    Parameters
    ----------
    messages : list[dict]
        The full message history.
    domain : str
        The classified domain (determines repetition thresholds).
    database : Database or None
        Optional database handle for sqlite-vec semantic search.

    Returns
    -------
    tuple[bool, str]
        ``(is_looping, reason_string)``.  Reason is empty if not looping.
    """
    max_loops = LOOP_LIMITS.get(domain, LOOP_LIMITS["standard"])

    # ---- Extract tool call history ------------------------------------------
    historical_calls: list[dict] = []
    for m in messages:
        if m.get("role") == "assistant" and "tool_calls" in m:
            for tc in m["tool_calls"]:
                if tc.get("type") == "function":
                    historical_calls.append({
                        "name": tc["function"]["name"],
                        "args": tc["function"]["arguments"],
                    })

    if not historical_calls:
        return False, ""

    # ---- Check role limit (absolute count) -----------------------------------
    if len(historical_calls) >= max_loops:
        return True, f"Role limit of {max_loops} tool calls reached"

    # ---- Check for repetitive calls (near-identical args) --------------------
    if len(historical_calls) < 2:
        return False, ""

    latest_call = historical_calls[-1]
    for prev_call in historical_calls[:-1]:
        if latest_call["name"] != prev_call["name"]:
            continue

        # Use difflib for string similarity on arguments
        ratio = difflib.SequenceMatcher(
            None,
            latest_call["args"],
            prev_call["args"],
        ).ratio()

        if ratio > 0.85:
            reason = (
                f"Repetitive tool call detected: "
                f"'{latest_call['name']}' with {ratio:.0%} similar args"
            )
            logger.warning("Tool loop detected: %s", reason)

            # If database is available, record the loop pattern
            if database is not None:
                try:
                    await database.store_lesson(
                        project_id="system",
                        pattern_text=f"{latest_call['name']}: {latest_call['args'][:200]}",
                        source="loop_detection",
                    )
                except Exception:
                    pass

            return True, reason

    return False, ""


# ---------------------------------------------------------------------------
# Semantic cache check
# ---------------------------------------------------------------------------

async def check_semantic_cache(
    query_text: str,
    is_factual: bool,
    database,  # Database
) -> Optional[str]:
    """
    If the query is marked as factual, check the semantic cache for a
    recent matching response.  Returns the cached text on a hit, or
    None on a miss.

    This allows factual queries to be answered instantly without waking
    the GPU.
    """
    if not is_factual or database is None:
        return None

    try:
        cached = await database.cache_lookup(query_text)
        if cached:
            logger.info("Semantic cache HIT for factual query")
        return cached
    except Exception:
        logger.debug("Semantic cache lookup failed (non-critical)")
        return None


# ---------------------------------------------------------------------------
# Project context extraction (file paths from prompt)
# ---------------------------------------------------------------------------

def extract_project_context(prompt: str) -> dict:
    """
    Extract file paths from the prompt text and infer a project name
    from the directory structure.

    Parameters
    ----------
    prompt : str
        The user prompt text.

    Returns
    -------
    dict
        ``{"project": str, "file_paths": list[str]}``.
    """
    # Regex for Unix-style absolute or relative file paths
    path_pattern = r"(?:/[a-zA-Z0-9_.-]+)+/[a-zA-Z0-9_.-]+\.[a-zA-Z0-9]+"
    file_paths = list(set(re.findall(path_pattern, prompt)))

    project_name = "general"
    if file_paths:
        parts = file_paths[0].split("/")
        if len(parts) >= 3:
            project_name = parts[-3] if len(parts) > 3 else parts[-2]

    return {"project": project_name, "file_paths": file_paths}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    """Convert a project name to a safe slug for database use."""
    slug = name.lower().strip().replace(" ", "-")
    return slug if slug else "general"