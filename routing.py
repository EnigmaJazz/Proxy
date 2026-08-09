"""
routing.py - Lane A/B dispatch, frontdesk classifier & semantic loop detection.

This module is the brain of the proxy's request routing.  It decides
which model should handle a given request based on GPU availability.

Key responsibilities:
- **Lane discrimination**: IDE keys (``sk-ide-pass`` header) → Lane B,
  everything else → Lane A.
- **Frontdesk classification**: Lane A requests are first classified by
  the 2B frontdesk model (JSON-GBNF) to determine intent, priority,
  project, and whether the query is factual (cacheable).
- **GPU-aware routing**: For CHAT/TOOL intents, lightweight requests
  route to "professional" unconditionally when the GPU is busy with a
  different specialist.  Heavy intents (CODE/SCHOLAR/etc.) go to the
  queue.
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

import difflib
import asyncio
import glob
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

import httpx

from constants import (
    FRONTEND_KEYS,
    IDE_PASSTHROUGH_HEADER,
    LOOP_LIMITS,
    ROUTE_MAP,
    _machine,
    _DREAM_FALLBACK_PHRASES,
    _DREAM_SNIP_PHRASES,
    get_logger,
)

from database import Database
from systemd import SystemdController

logger = get_logger("proxy.routing")

# ---------------------------------------------------------------------------
# Dream/soul detection — discovers Nanobot dream template phrases
# ---------------------------------------------------------------------------

# Dream phrase reading is a lazy file cache keyed by (path, mtime) so
# dream detection survives Nanobot updates (`uv tool upgrade nanobot-ai`).
# The glob pattern uses a wildcard Python version path since uv-managed
# packages move between python3.13/, python3.14/ etc. across updates.
@lru_cache(maxsize=4)
def _read_dream_phrases(latest_path: str, mtime: float) -> list[str]:
    """Read + extract dream fingerprint phrases from a template file.

    Cached by (path, mtime) — the phrases reload automatically when the
    file changes (mtime differs), and are reused across requests when it
    hasn't.  Returns ``_DREAM_FALLBACK_PHRASES`` if the file cannot be
    read (mirrors the old lazy-cache fallback).
    """
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

    logger.debug(
        "Dream phrases refreshed from %s (%d phrases)", latest_path, len(lines),
    )
    return lines


def _get_dream_phrases() -> list[str]:
    """
    Discover the latest Nanobot dream_phase1.md template and extract
    unique identifying phrases for dream/soul task detection.

    Uses a lazy file cache keyed by (path, mtime) so that the phrases
    are automatically refreshed when Nanobot is updated after proxy
    startup (``functools.lru_cache`` on ``_read_dream_phrases``).

    Returns
    -------
    list[str]
        Lowercase phrases unique to Nanobot dream prompts.
        Falls back to _DREAM_FALLBACK_PHRASES if the template is
        inaccessible.
    """
    # Glob with wildcard Python version — survives uv updates.  The nanobot
    # install lives under the user's uv tool dir; the pattern is
    # machine-specific (local_config.UV_NANOBOT_PATTERN).
    pattern = _machine(
        "UV_NANOBOT_PATTERN",
        os.path.expanduser("~/.local/share/uv/tools/nanobot-ai/lib/python*/") + "site-packages/nanobot/templates/agent/dream_phase1.md",
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

    return _read_dream_phrases(latest_path, mtime)


async def is_dream_process(raw_text: str) -> bool:
    """
    Detect whether *raw_text* originates from a Nanobot dream/autonomous
    background task by matching against fingerprint phrases from the
    dream_phase1.md template.

    Dream template phrases are unique to the autonomous memory
    consolidation prompt — they do NOT appear in regular Nanobot chat.
    Matching 2+ of these phrases in the user message provides a
    high-confidence signal that this is an autonomous background task
    that should be routed at BACKGROUND priority.

    The phrase lookup (glob + mtime + file read) is offloaded to a worker
    thread so the dream check never blocks the event loop in the request
    path (AGENTS.md rule 3).  The lru-cached file read is preserved.

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

    phrases = await asyncio.to_thread(_get_dream_phrases)
    # Match against BOTH the phase1 template phrases and the in-repo SNIP
    # variant set, so newer nanobot memory-consolidation formats (which
    # annotate SNIP attributes instead of FILE/SKIP lines) are also routed
    # as autonomous dreams instead of misrouted to the coding gate.
    phrases = list(phrases) + list(_DREAM_SNIP_PHRASES)
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
        Always False in the current routing model.  Kept as a field
        for API stability; no path sets it to True.
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

def discriminate_caller(headers: dict[str, str]) -> str:
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

def is_lane_b(headers: dict[str, str]) -> bool:
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
@lru_cache(maxsize=1)
def _front_desk_prompt() -> str:
    """Load the frontdesk prompt from disk (cached after first load).

    Uses ``load_role_prompt("frontdesk")`` which reads from
    ``<kinver-home>/prompts/frontdesk.txt``.  Cached since the
    prompt doesn't change at runtime.
    """
    from llm import load_role_prompt
    return load_role_prompt("frontdesk")


def _get_frontdesk_prompt() -> str:
    """Return the cached frontdesk prompt (see ``_front_desk_prompt``)."""
    return _front_desk_prompt()


async def classify_with_frontdesk(
    user_text: str,
    database: Optional[Database] = None,  # Optional Database for project list injection
    frontdesk_port: int = 0,
    available_tool_names: Optional[list[str]] = None,
) -> dict[str, Any]:
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
    defaults: dict[str, Any] = {
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
        base_prompt = await asyncio.to_thread(_get_frontdesk_prompt)

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
            except (sqlite3.Error, OSError, ValueError):
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

    except (httpx.HTTPError, json.JSONDecodeError, OSError):
        logger.exception("Frontdesk classification failed — using defaults")
        return defaults


_CODING_DIFFICULTY_PROMPT = (
    "You are a coding-task difficulty assessor for a proxy that routes "
    "coding requests between two pathways: a local code model (Professional) "
    "and an agentic coding tool (OpenCode).\n"
    "Judge the SPECIFIC task below. Analyze it yourself; do not repeat these "
    "instructions back.\n"
    "Output a single JSON object with exactly these fields:\n"
    '- "difficulty": one of "low", "medium", or "high".\n'
    '- "recommendation": one of "opencode" or "local".\n'
    '- "reason": one short sentence explaining the choice.\n'
    "Guidance:\n"
    '- ROUTE TO LOCAL ("recommendation": "local", low/medium difficulty): '
    'single-file or single-script tasks, small edits, quick fixes, one-off '
    'scripts, self-contained functions, simple questions about a file.\n'
    '- ROUTE TO OPENCODE ("recommendation": "opencode", medium/high difficulty): '
    'multi-file changes, refactors across modules, unfamiliar codebases, '
    'architecture or design work, exploratory work, tasks needing tool use, '
    'tests, debugging, or many iterative steps.\n'
    "Examples:\n"
    '- "print hello world" -> difficulty low, recommendation local.\n'
    '- "add a function to utils.py" -> difficulty low, recommendation local.\n'
    '- "refactor auth across 12 files to async" -> difficulty high, '
    'recommendation opencode.\n'
    '- "debug why the service crashes only in production" -> difficulty high, '
    'recommendation opencode.\n'
    "Reply with ONLY the JSON object — no commentary, no markdown."
)


async def evaluate_coding_task(
    user_text: str,
    model_port: int = 0,
) -> dict[str, Any]:
    """Ask a local model to judge a coding task's difficulty and recommend a
    route (opencode vs local).  Defaults to the professional model port
    (the capable local code model); the caller may pass any port.  The 2B
    frontdesk proved too weak for this judgment (it called a 12-file
    refactor "low/local"), so the gate feeds professional.  Best-effort:
    on any failure returns safe defaults (difficulty medium, recommendation
    "local", reason "") so the coding gate still works.
    """
    defaults: dict[str, Any] = {
        "difficulty": "medium",
        "recommendation": "local",
        "reason": "",
    }

    try:
        # Same endpoint pattern as classify_with_frontdesk: raw completions
        # through the legacy /completion endpoint with JSON-GBNF enforcement.
        from llm import call_model

        full_prompt = _CODING_DIFFICULTY_PROMPT + "\n\nTask: " + user_text

        content = await call_model(
            port=model_port,
            prompt=full_prompt,
            profile="json_gbnf_difficulty",  # Enforce structured JSON output
            max_tokens=256,
        )
        content = content.strip()

        # Strip markdown fences if present (mirrors classify_with_frontdesk).
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0]

        # raw_decode extracts ONLY the first complete JSON object, ignoring
        # any trailing garbage the small model may append.
        decoder = json.JSONDecoder()
        assessment, _ = decoder.raw_decode(content)

        # Merge with defaults so all three keys are always present.
        assessment = {**defaults, **assessment}

    except (httpx.HTTPError, json.JSONDecodeError, OSError):
        logger.exception(
            "Coding task difficulty evaluation failed — using defaults"
        )
        assessment = dict(defaults)

    # Deterministic complexity escalation (safety net over the small 2B
    # frontdesk model, mirroring the routing-layer keyword heuristics).
    # The 2B model under-judges complex multi-file work as "local" — a
    # 12-file refactor came back low/local.  Strong complexity signals in
    # the task text force an opencode recommendation so the user never
    # gets a misleading "run it locally" for genuinely hard work.
    lowered = (user_text or "").lower()
    complexity_signals = (
        "refactor", "refactoring", "across ", "multi-file", "multiple files",
        "entire module", "whole codebase", "architecture", "unfamiliar code",
        "production", "debug", "debugging", "migrate", "migration",
        "12 files", "10 files", "20 files", "all files", "test suite",
        "full test coverage", "design document", "microservice", "plugin",
        "rewrite", "restructure", "integrate", "integration",
    )
    if (
        any(sig in lowered for sig in complexity_signals)
        and assessment.get("recommendation") == "local"
    ):
        logger.info(
            "Complexity signal in coding task — escalating recommendation "
            "local → opencode",
        )
        assessment["recommendation"] = "opencode"
        if assessment.get("difficulty") == "low":
            assessment["difficulty"] = "medium"
        if not assessment.get("reason"):
            assessment["reason"] = "Task shows multi-file or complex signals."

    return assessment


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def _safe_json_parse(text: str) -> Any:
    """Best-effort JSON extraction from a model reply (the first {...}
    block).  Returns None on failure."""
    import json as _json
    try:
        return _json.loads(text.strip())
    except ValueError:
        pass
    try:
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            return _json.loads(text[start:end + 1])
    except ValueError:
        pass
    return None


async def reclassify_with_professional(
    user_text: str,
    model_port: int = 0,
) -> dict[str, Any]:
    """Ask the professional model to reclassify a request before answering.

    The 2B frontdesk under-judges complex work (it called a 12-file
    refactor "low").  The professional's reclassification runs on the
    SAME context the answering pass will use — the professional's
    KV-cache makes the reclassification preprocessing reusable by the
    answering call, so the added latency is the marginal decode, not a
    full re-read.  The proxy then assigns the correct intent-based
    sampling parameters to the actual answering call.  Best-effort: on
    any failure returns {} so the frontdesk's classification stands.
    """
    defaults: dict[str, Any] = {}
    try:
        from llm import call_model

        prompt = (
            "You are the proxy's reclassifier.  Given the request context "
            "below, reclassify it for correct routing and sampling.  Reply "
            "with EXACTLY this JSON — no commentary, no markdown:\n"
            '{"intent": "CHAT|TOOL|CODE|SCHOLAR|IMAGE|PROFESSIONAL", '
            '"priority": 1|2|3, "complexity": "low|medium|high", '
            '"parameters": {"temperature": 0.0-1.5, "top_p": 0.0-1.0, '
            '"thinking_budget_tokens": 0-8192}}\n'
            "Guidance: coding and multi-step work -> CODE; research/deep "
            "analysis -> SCHOLAR; image-bearing requests -> IMAGE; casual "
            "conversation -> CHAT; tool-requiring requests -> TOOL.  "
            "Priority 1 = heavy model required, 3 = light.  "
            "Parameters: complex/analytic work benefits from lower "
            "temperature (0.1-0.3) and a thinking budget; casual chat from "
            "higher temperature; simple factual answers from zero thinking.\n"
            f"REQUEST CONTEXT:\n{user_text[:4000]}"
        )
        port = model_port or 13109  # professional default port
        text = await asyncio.to_thread(
            call_model, port, prompt, max_tokens=256,
        )
        payload = _safe_json_parse(text)
        if not isinstance(payload, dict):
            return defaults
        result = dict(defaults)
        if isinstance(payload.get("intent"), str):
            result["intent"] = payload["intent"].upper()
        # Values are clamped + type-checked: the reclassifier's output is
        # steerable via prompt injection in user_text, and unclamped values
        # would flow straight into the generation sampling parameters.
        prio = payload.get("priority")
        if isinstance(prio, int) and 1 <= prio <= 3:
            result["priority"] = prio
        if isinstance(payload.get("complexity"), str):
            result["complexity"] = payload["complexity"].lower()
        if isinstance(payload.get("parameters"), dict):
            params: dict[str, Any] = {}
            temp = payload["parameters"].get("temperature")
            if isinstance(temp, (int, float)) and 0.0 <= float(temp) <= 1.5:
                params["temperature"] = float(temp)
            top_p = payload["parameters"].get("top_p")
            if isinstance(top_p, (int, float)) and 0.0 <= float(top_p) <= 1.0:
                params["top_p"] = float(top_p)
            tb = payload["parameters"].get("thinking_budget_tokens")
            if isinstance(tb, int) and 0 <= tb <= 8192:
                params["thinking_budget_tokens"] = tb
            if params:
                result["parameters"] = params
        return result
    except (httpx.HTTPError, OSError, ValueError, AttributeError):
        return defaults

def resolve_model(intent: str, is_lane_b: bool = False) -> str:
    """
    Given a classified intent and lane, return the llama.cpp service key.

    Lane B always uses the 35B MoE (professional) regardless of intent.
    Lane A uses the ROUTE_MAP.
    """
    if is_lane_b:
        return "professional"
    return ROUTE_MAP.get(intent, "professional")


# ---------------------------------------------------------------------------
# GPU-aware routing for Lane A (contention → Professional)
# ---------------------------------------------------------------------------

async def resolve_route_for_lane_a(
    classification: dict[str, Any],
    systemd: SystemdController,
    has_tool_history: bool = False,  # True if conversation has tool_calls
) -> RouteDecision:
    """
    Determine the target model for a Lane A request, taking GPU
    occupancy into account.

    **CHAT / TOOL with GPU busy**: Routes to "professional"
    unconditionally.  Lightweight requests never fall back to a CPU
    model and are never enqueued — they are handled synchronously
    against the GPU.

    **Heavy intents (CODE/SCHOLAR/PROFESSIONAL/CREATIVE/ARCHITECT)**
    require the GPU and are enqueued.  The queue worker handles
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

    # CHAT/TOOL always route to professional (ROUTE_MAP encodes this);
    # lightweight requests are handled synchronously, never fall back to a
    # CPU model, and are never enqueued.
    model_key = ROUTE_MAP.get(intent, "professional")
    port = await systemd.get_port(model_key)
    is_cpu_fallback = False
    hardware_path = "gpu"
    tools_required = tools_required or (intent == "TOOL" and has_tool_history)

    if intent in ("CHAT", "TOOL"):
        logger.info(
            "Routing %s → professional (port %d)",
            intent, port,
        )
        return RouteDecision(
            model_key=model_key,
            port=port,
            is_cpu_fallback=is_cpu_fallback,
            hardware_path=hardware_path,
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


async def detect_tool_loops(
    messages: list[dict],
    domain: str = "standard",
    database: Optional[Database] = None,  # Optional Database for semantic search
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

    if len(historical_calls) < 2:
        return False, ""

    # ---- Check for consecutive repetitive calls (death spiral) -------------
    # A loop is the model repeating the SAME tool with near-identical args
    # back-to-back.  Count the trailing run of matching calls.  A long
    # conversation that used many DIFFERENT tools is legitimate and must not
    # trip the detector — the old absolute role-limit check (total call
    # count >= limit) stripped tools from every request once the conversation
    # crossed N calls, which broke every long agentic session (the model
    # could no longer call tools and degenerated into text stubs / empty
    # output, producing exactly the "loops with no output" symptom).
    latest_call = historical_calls[-1]
    run = 1
    for prev_call in reversed(historical_calls[:-1]):
        if prev_call["name"] != latest_call["name"]:
            break
        ratio = difflib.SequenceMatcher(
            None,
            latest_call["args"],
            prev_call["args"],
        ).ratio()
        if ratio < 0.85:
            break
        run += 1
        if run >= max_loops:
            reason = (
                f"Repetitive tool call detected: "
                f"'{latest_call['name']}' called {run} times in a row "
                f"with {ratio:.0%} similar args"
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
                except (sqlite3.Error, OSError, ValueError):
                    pass

            return True, reason

    return False, ""


# ---------------------------------------------------------------------------
# Semantic cache check
# ---------------------------------------------------------------------------

async def check_semantic_cache(
    query_text: str,
    is_factual: bool,
    database: Optional[Database],  # Database
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
    except (sqlite3.Error, OSError, ValueError):
        logger.debug("Semantic cache lookup failed (non-critical)")
        return None


# ---------------------------------------------------------------------------
# Project context extraction (file paths from prompt)
# ---------------------------------------------------------------------------

def extract_project_context(prompt: str) -> dict[str, Any]:
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