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

# ---------------------------------------------------------------------------
# Project root & directory helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT: Path = Path(__file__).resolve().parent

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
    "worker":       "http://127.0.0.1:8082/v1/chat/completions",
    "chatter":      "http://127.0.0.1:8083/v1/chat/completions",
    # Tier 2 — heavy GPU models
    "professional": "http://127.0.0.1:8084/v1/chat/completions",
    "scholar":      "http://127.0.0.1:8086/v1/chat/completions",
    "creative":     "http://127.0.0.1:8087/v1/chat/completions",
    "architect":    "http://127.0.0.1:8088/v1/chat/completions",
    "coder":        "http://127.0.0.1:8089/v1/chat/completions",
    # CPU-only models (never hot-swapped)
    "reasoning":    "http://127.0.0.1:8085/v1/chat/completions",
    "lifeboat":     "http://127.0.0.1:8090/v1/chat/completions",
    # Cloud failover
    "cloud":        "https://openrouter.ai/api/v1/chat/completions",
}

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

# Legacy paths (kept for transition / backward compat)
MODELS_DIR: str = "/home/james/kinver-hub/models/"
PROMPTS_DIR: str = "/home/james/kinver-hub/prompts/"
ENV_NGL_FILE: str = "/home/james/kinver-hub/.env.ngl"
CACHE_DIR: str = "/home/james/kinver-hub/cache/"
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
    "spd5118": {
        "warn": 70.0, "crit": 80.0, "max": 85.0,
        "name": "Crucial DDR5 RAM",
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
    "tool_choice",
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