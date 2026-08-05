"""
constants.py - Central configuration constants for the Kinver AI Proxy.

This module serves as the single source of truth for all configurable
parameters: model endpoints, hardware limits, cooling profiles, API keys,
tool configurations, and file paths.  No other module should contain
hard-coded magic numbers or URLs.

Maintainers: James Stansfield
"""
from __future__ import annotations

import os
import logging
import re
from pathlib import Path

from prometheus_client import Gauge

# ---------------------------------------------------------------------------
# Project root & directory helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT: Path = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Runtime context windows — operator-declared n_ctx per model
# ---------------------------------------------------------------------------
# These are the TRUE caps the running llama-server processes enforce (the
# ``-c`` value in the llama-*.service units), NOT the GGUF native context from
# model_profiles.yaml (which may be larger, e.g. professional GGUF 262144 vs
# server 65536). Context governance uses these so the input-budget snip is
# grounded in the real runtime limit.
RUNTIME_CONTEXT_WINDOWS: dict[str, int] = {
    "frontdesk": 12_288,
    "chatter": 32_768,
    "professional": 65_536,
    "scholar": 32_768,
    "creative": 32_768,
    "architect": 32_768,
    "coder": 32_768,
}

# ---------------------------------------------------------------------------
# Context governance — frontend-agnostic tool-call context budget
# ---------------------------------------------------------------------------
# Mirrors the discipline nanobot-ai applies inside its agent loop, but lives in
# the proxy so EVERY frontend gets it. See proxy/context_governance.py.
CONTEXT_GOVERNANCE_DEFAULT_ENABLED: bool = True
MAX_TOOL_RESULT_CHARS: int = 16_000       # per-tool result cap (all tools)
READ_FILE_RESULT_CHARS: int = 32_000      # read_file keeps a larger head inline
TOOL_RESULT_PREVIEW_CHARS: int = 800      # preview length in offload references
SNIP_SAFETY_BUFFER_TOKENS: int = 1_024    # headroom below the budget
TOOL_RESULTS_DIR_NAME: str = "tool_results"
OFFLOAD_MAX_FILES: int = 500              # cap on offloaded result files
ESTIMATED_CHARS_PER_TOKEN: int = 4        # cheap ASCII-biased token estimate

# ---------------------------------------------------------------------------
# Logger (reusable — returns a pre-configured logger for any module name)
# ---------------------------------------------------------------------------

LOG_FORMAT: str = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S"

def get_logger(name: str = "proxy") -> logging.Logger:
    """Return a pre-configured logger for the given *name*."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    return logger

# ---------------------------------------------------------------------------
# Environment load helper (reads .env from project root)
# ---------------------------------------------------------------------------

from dotenv import load_dotenv as _load_dotenv
_load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# API keys & tokens
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_SITE_URL: str = "https://github.com/EnigmaJazz/Proxy"
OPENROUTER_SITE_NAME: str = "Kinver Local AI Hub"

# Telegram notification credentials (optional — alerts fire if both are set)
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# IDE-bypass header — the presence of this header signals Lane B routing
IDE_PASSTHROUGH_HEADER: str = "sk-ide-pass"

# ---------------------------------------------------------------------------
# Frontend key discrimination (maps API tokens → caller type)
# ---------------------------------------------------------------------------

FRONTEND_KEYS: dict[str, str] = {
    "ide-key": "IDE",
    "agent-key": "AGENTIC",
}

# ---------------------------------------------------------------------------
# Llama.cpp model endpoints — FALLBACK ONLY
#
#   These hard-coded URLs are a LAST-RESORT fallback for ``llm.py``.
#   In normal operation, every caller passes the live-resolved ``port``
#   parameter (sourced from ``systemd.get_port()``) to ``stream_llm()``
#   or ``call_llm()``.  The port-based URL takes priority over this dict.
#
#   Resolution order in ``stream_llm()``:
#     1. ``api_url`` override (explicit full URL)
#     2. ``port`` parameter (live systemd port → constructed URL)
#     3. ``LLAMA_ENDPOINTS`` (this dict, hard-coded fallback)
#
#   The endpoints below serve as:
#     - Documentation of the intended port layout
#     - A fallback if systemd port resolution fails (port=0)
#     - An auditable reference without access to a running machine
#
#   The "cloud" entry has no systemd unit, so it is always used directly.
# ---------------------------------------------------------------------------

LLAMA_ENDPOINTS: dict[str, str] = {
    # Tier 1 — lightweight always-on models
    "frontdesk":    "http://127.0.0.1:8081/v1/chat/completions",
    "chatter":      "http://127.0.0.1:8083/v1/chat/completions",
    # Tier 2 — heavy GPU models
    "professional": "http://127.0.0.1:8084/v1/chat/completions",
    "scholar":      "http://127.0.0.1:8086/v1/chat/completions",
    "creative":     "http://127.0.0.1:8087/v1/chat/completions",
    "architect":    "http://127.0.0.1:8088/v1/chat/completions",
    "coder":        "http://127.0.0.1:8089/v1/chat/completions",
    # Cloud failover
    "cloud":        "https://openrouter.ai/api/v1/chat/completions",
}

# All client-pickable model keys. Derived from LLAMA_ENDPOINTS (excluding
# "cloud" which has no local systemd unit). Used by routes.py to validate
# client-named model requests before overriding the frontdesk-classified
# route (R19 client-named-model override).
ALL_MODEL_KEYS: tuple[str, ...] = tuple(
    key for key in LLAMA_ENDPOINTS if key != "cloud"
)

# ---------------------------------------------------------------------------
# OpenCode bridge (routes requests to a headless opencode serve backend)
# ---------------------------------------------------------------------------

# Headless opencode serve backend (opencode_bridge.py).  A client that
# picks model "opencode" (or embeds /opencode) directs the request to the
# opencode agent instead of a local llama model.
OPENCODE_SERVE_URL: str = "http://127.0.0.1:18900"

# Absolute path to the opencode binary.  systemd services run with a
# minimal PATH that does not include ~/.opencode/bin, so the bridge spawn
# must not rely on PATH resolution.
OPENCODE_BIN: str = "~/.opencode/bin/opencode"

# Agent used by the bridge for coding tasks.  The Gentle AI SDD
# orchestrator coordinates the full SDD cycle (and handles direct tasks)
# instead of opencode's plain build agent.
OPENCODE_AGENT: str = "gentle-orchestrator"

# How long to wait for the opencode agent to finish a task.
OPENCODE_SERVE_TIMEOUT: float = 600.0

# Bridge model keys exposed to clients, validated alongside ALL_MODEL_KEYS.
# "opencode" routes to the opencode serve bridge instead of llama.cpp.
BRIDGE_MODEL_KEYS: frozenset[str] = frozenset({"opencode"})

# Queue-worker escalation backend after local tiers are exhausted:
# "opencode" → headless opencode serve (build agent);
# "openrouter" → legacy OpenRouter failover (llm.openrouter_cloud_escalation).
CLOUD_ESCALATION_BACKEND: str = "opencode"

# ---------------------------------------------------------------------------
# Systemd service discovery
# ---------------------------------------------------------------------------

SYSTEMD_DIR: str = "/etc/systemd/system/"
SERVICE_PATTERN: re.Pattern = re.compile(r"^llama-([a-zA-Z0-9_]+)\.service$")

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

# Predictive cooling IPC files (monitored by Cooler Control daemon)
CPU_TEMP_PATH: Path = Path("/tmp/cpu_temp.txt")
GPU_TEMP_PATH: Path = Path("/tmp/gpu_temp.txt")

# SQLite database (replaces RAM queue + JSON persistence)
DB_PATH: Path = PROJECT_ROOT / "ai_queue.db"

# ---------------------------------------------------------------------------
# SQLite schema migrations — executed in order during startup
# ---------------------------------------------------------------------------
MIGRATIONS: list[str] = [
    # ---- Enable WAL mode (must be first, outside a transaction) -------------
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",

    # ---- jobs table ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id              TEXT PRIMARY KEY,
        priority        INTEGER NOT NULL DEFAULT 2,
        state           TEXT NOT NULL DEFAULT 'queued',
        intent          TEXT NOT NULL DEFAULT 'CHAT',
        project_id      TEXT,
        messages_json   TEXT NOT NULL,
        tools_json      TEXT,
        parameters_json TEXT,
        failure_count   INTEGER NOT NULL DEFAULT 0,
        current_tier    TEXT,
        partial_content TEXT DEFAULT '',
        finish_reason   TEXT,
        lane            TEXT NOT NULL DEFAULT 'lane_a',
        is_lane_b       INTEGER NOT NULL DEFAULT 0,
        caller_type     TEXT DEFAULT 'AGENTIC',
        model_override  TEXT,
        created_at      TEXT NOT NULL,
        started_at      TEXT,
        completed_at    TEXT,
        FOREIGN KEY (project_id) REFERENCES projects(id)
    )
    """,

    # ---- projects table -----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS projects (
        id              TEXT PRIMARY KEY,
        display_name    TEXT,
        root_path       TEXT,
        created_at      TEXT NOT NULL,
        last_active_at  TEXT NOT NULL
    )
    """,

    # ---- semantic_cache table ------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS semantic_cache (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        query_hash      TEXT NOT NULL UNIQUE,
        query_text      TEXT NOT NULL,
        response_text   TEXT NOT NULL,
        embedding       BLOB,
        hit_count       INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        expires_at      TEXT NOT NULL
    )
    """,

    # ---- lessons_learned table -----------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS lessons_learned (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id      TEXT NOT NULL,
        pattern_text    TEXT NOT NULL,
        embedding       BLOB,
        source          TEXT,
        created_at      TEXT NOT NULL,
        FOREIGN KEY (project_id) REFERENCES projects(id)
    )
    """,

    # ---- stream_chunks table -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS stream_chunks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id          TEXT NOT NULL,
        seq             INTEGER NOT NULL,
        chunk_json      TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES jobs(id)
    )
    """,

    # ---- indexes ------------------------------------------------------------
    "CREATE INDEX IF NOT EXISTS idx_jobs_state_priority ON jobs(state, priority)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_stream_chunks_job ON stream_chunks(job_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_semantic_cache_hash ON semantic_cache(query_hash)",
    "CREATE INDEX IF NOT EXISTS idx_lessons_project ON lessons_learned(project_id)",
]

# Legacy paths (kept for transition / backward compat)
MODELS_DIR: str = "~/kinver-hub/models/"
PROMPTS_DIR: str = "~/kinver-hub/prompts/"
ENV_NGL_FILE: str = "~/kinver-hub/.env.ngl"
CACHE_DIR: str = "~/kinver-hub/cache/"
RECOVERY_FILE: str = str(PROJECT_ROOT / "recovery_state.json")
PERSISTENT_QUEUE_FILE: str = str(PROJECT_ROOT / "background_queue.json")

# ---------------------------------------------------------------------------
# Thermal / cooling profile constants
# ---------------------------------------------------------------------------

class CoolingPreset:
    """
    Named constants for the three-stage cooling state machine.

    Values are millidegrees Celsius integers written to the IPC files
    monitored by the Cooler Control daemon:

    - BASELINE (30000): Idle / silent — written on startup and after stream ends.
    - GENERATION (50000): Sustained decode load — written on first token/chunk.
    - PREFILL (75000): Preemptive burst before matrix ops — written before
      forwarding the inference payload, followed by a 1-second buffer delay
      to allow fans to physically accelerate.
    """
    BASELINE: int = 30000
    GENERATION: int = 50000
    PREFILL: int = 75000

# ---------------------------------------------------------------------------
# Hardware thermal limits (degrees Celsius)

# ---------------------------------------------------------------------------

THERMAL_LIMITS: dict[str, dict[str, float | str]] = {
    "k10temp": {
        "warn": 85.0, "crit": 93.0, "max": 95.0,
        "name": "Ryzen 7700 CPU",
    },
    "amdgpu_core": {
        "warn": 90.0, "crit": 100.0, "max": 105.0,
        "name": "RX 6700XT Core",
    },
    "amdgpu_vram": {
        "warn": 95.0, "crit": 102.0, "max": 105.0,
        "name": "RX 6700XT VRAM",
    },
    "nvme": {
        "warn": 65.0, "crit": 75.0, "max": 80.0,
        "name": "Samsung 990 PRO NVMe",
    },
}

# Legacy hardware limits alias (used by older code paths)
MAX_CPU_TEMP: int = 95
MAX_GPU_TEMP: int = 110
GPU_MEM_GB: int = 12

# ---------------------------------------------------------------------------
# Tool / search configuration
# ---------------------------------------------------------------------------

# SearXNG instance URL (statically compiled, local)
SEARXNG_URL: str = "http://localhost:8081/search"

# Web-search depth profiles (used by tools.py)
DEPTH_CONFIG: dict[str, dict[str, int]] = {
    "fast":     {"count": 10, "gold": 3,  "chars": 2000, "summary_words": 150},
    "standard": {"count": 25, "gold": 7,  "chars": 3500, "summary_words": 300},
    "deep":     {"count": 50, "gold": 15, "chars": 4000, "summary_words": 600},
}

# Native tools registered with the proxy (made available to frontends)
NATIVE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Searches the live web and reads full articles. "
                "Use this instead of frontend search tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The explicit search query.",
                    },
                    "depth": {
                        "type": "string",
                        "enum": ["fast", "standard", "deep"],
                    },
                },
                "required": ["query"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Routing & classifier configuration
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

# Keywords that indicate a request likely needs tool access (web search,
# file I/O, or system exec).  Over-detection is safe because TOOL-routed
# requests reach the professional model, which handles plain chat fine.
# All matching is done lowercase with substring matching.
TOOL_KEYWORDS: frozenset[str] = frozenset({
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

# Safety-net keywords that force a CHAT classification to CODE.  The 2B
# frontdesk sometimes misses explicit coding requests (e.g. "write a
# python script") even though the prompt defines CODE; this deterministic
# layer catches the common code-writing phrasings.  Over-detection is safe:
# CODE and CHAT both route to Professional — the difference is the
# coding-decision gate, the code profile, and the triage message.
CODE_KEYWORDS: frozenset[str] = frozenset({
    # Code-writing verbs
    "write a script", "write a python", "write python", "write a program",
    "write code", "write a function", "write a class", "write a script that",
    "create a script", "create a python", "create a program",
    "create a function", "create a class", "create a script that",
    "generate a script", "generate code", "generate a function",
    "implement a", "implement the", "refactor", "refactoring",
    "debug", "debugging", "compile", "lint", "code review",
    "review this code", "review the code",
    "fix the bug", "fix this code", "fix the code", "fix the error",
    "syntax error", "stack trace", "traceback", "unit test", "pytest",
    "test case", "test suite", "write tests", "write a test",
    "parse json", "json parsing",
    # Language / artifact mentions
    "python script", "python file", "python code", "python function",
    "script in python", "code in python", "in python",
    "bash script", "shell script", "shell command", "bash script that",
    "javascript", "typescript", "node.js", "nodejs",
    "flask app", "django", "fastapi", "sqlalchemy", "react component",
    "html file", "css file", "json file", "yaml file",
    "api endpoint", "api client", "api route", "rest api", "graphql",
    "sql query", "database schema", "regex", "regex pattern", "regexp",
    "function that", "function called", "class called", "class that",
    "method that", "function returns", "function takes",
    "python code that", "code that",
})

# ---------------------------------------------------------------------------
# ROUTE_MAP: classified intent → local model endpoint key
#
#   CHAT      → professional (35B MoE)
#   TOOL      → professional (35B MoE, handles tool_calls natively)
#   CODE      → professional (35B MoE, heavy coding model)
#   SCHOLAR   → scholar  (deep research)
#   PROFESSIONAL → professional (professional writing / 35B MoE)
#   CREATIVE  → creative (long-form creative writing)
#   ARCHITECT → architect (complex multi-stage planning)
#
#   Lane B (IDE passthrough) ALWAYS goes to professional regardless
#   of intent — the frontdesk is bypassed entirely.
# ---------------------------------------------------------------------------
ROUTE_MAP: dict[str, str] = {
    "CHAT":         "professional",
    "TOOL":         "professional",
    "CODE":         "professional",  # 35B MoE
    "SCHOLAR":      "scholar",
    "PROFESSIONAL": "professional",
    "CREATIVE":     "creative",
    "ARCHITECT":    "architect",
}

# Heavy GPU models (require VRAM allocation, can't be quickly swapped)
HEAVY_MODELS: set[str] = {"professional", "coder", "creative", "scholar", "architect"}

# CPU-only models (always resident, never hot-swapped)
CPU_MODELS: set[str] = {"frontdesk"}

# Heavy model keys that may need cold-starting before streaming (includes
# the always-on Chatter model, so it is a superset of HEAVY_MODELS).
_HEAVY_MODEL_KEYS: set[str] = {"professional", "coder", "creative", "scholar", "architect", "chatter"}

# Human-readable model labels for user-facing triage/status messages.
# Single source of truth shared by _build_triage_message and the
# cold-start feedback in the streaming path.
MODEL_LABELS: dict[str, str] = {
    "frontdesk":    "Front Desk (2B classifier)",
    "chatter":      "Chatter (9B fast chat)",
    "professional": "Professional (35B MoE)",
    "coder":        "Coder (27B Dense)",
    "creative":     "Creative (long-form)",
    "scholar":      "Scholar (deep research)",
    "architect":    "Architect (multi-stage planning)",
}

# Maximum tool call repetitions allowed per domain before breaking the loop
LOOP_LIMITS: dict[str, int] = {
    "scholar":      6,
    "architect":    4,
    "coder":        4,
    "professional": 4,
    "creative":     5,
    "standard":     3,
    "CHAT":         2,
    "TOOL":         2,
}

# ---------------------------------------------------------------------------
# Prometheus metric descriptors
#
# These are STATELESS metric descriptors (label templates), not runtime
# state: each Gauge registers with the default collector registry at import
# time and only its numeric VALUE is mutated at runtime via ``.set()``.  The
# /metrics scrape reads the default registry, so the descriptors may live
# here as configuration.
# ---------------------------------------------------------------------------
metric_cpu_temp = Gauge("cpu_temp_celsius", "CPU temperature (°C)")
metric_gpu_edge_temp = Gauge("gpu_edge_temp_celsius", "GPU edge temperature (°C)")
metric_gpu_junc_temp = Gauge("gpu_junc_temp_celsius", "GPU junction temperature (°C)")
metric_gpu_vram_temp = Gauge("gpu_vram_temp_celsius", "GPU VRAM temperature (°C)")
metric_gpu_used_vram_gb = Gauge("gpu_used_vram_gb", "GPU VRAM in use (GiB)")
metric_ram_used_pct = Gauge("ram_used_percent", "System RAM usage (%)")

# ---------------------------------------------------------------------------
# LLM inference constants
# ---------------------------------------------------------------------------

# Universal stop sequences applied to all local model calls
STOP_SEQS: list[str] = [
    "<|eot_id|>",
    "<|im_end|>",
    "<|endoftext|>",
    "</s>",
    "Observation:",
    "```output",
]

# OpenAI chat-completion fields forwarded verbatim from the client body
# when present (R11 forwarding / hardening).  These are in addition to the
# core sampling parameters (temperature, top_p, max_tokens) which are read
# explicitly in routes.py.
OPENAI_FORWARD_FIELDS: tuple[str, ...] = (
    "parallel_tool_calls",
    "frequency_penalty",
    "presence_penalty",
    "logit_bias",
    "seed",
    "user",
    "response_format",
    "top_logprobs",
    "n",
    "logprobs",
)

# R17 parameter-authority field set.  When the proxy owns the model pick
# (auto-routed or dream/soul fast-path), these fields are sourced from the
# model profile instead of the client request.
R11_AUTHORITY_FIELDS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "max_tokens",
    "thinking_budget_tokens",
    "seed",
    "top_logprobs",
    "response_format",
    "n",
)

# Retry / timeout settings for LLM HTTP calls
MAX_RETRIES: int = 3
# Retry delay bumped from 2.0 to 5.0 seconds to give heavy GPU models
# more time to complete cold starts.  With the _event_stream_with_model_startup
# wrapper ensuring models are started before streaming, this retry delay
# now serves as a safety net for edge cases where the port takes slightly
# longer than expected to accept connections.
RETRY_DELAY: float = 5.0  # seconds between retries
REQUEST_TIMEOUT: float = 300.0  # 5 minutes for long generations

# Thermal monitor polling interval (seconds)
SENSOR_INTERVAL: float = 3.0

# TCP health-check timeout (seconds)
TCP_TIMEOUT: float = 5.0

# Streaming buffer size (bytes)
BLOCK_SIZE: int = 65536  # 64 KiB

# ---------------------------------------------------------------------------
# Priority tier definitions (used by the SQLite queue)
# ---------------------------------------------------------------------------

# Tier 1 = HIGH    (IDE coding, urgent agent tasks)
# Tier 2 = NORMAL  (standard chat, tool calls, general agentic)
# Tier 3 = BACKGROUND (dream/soul processes, batch work)
# Tier 4 = DAEMON  (compaction, maintenance — reserved for future use)