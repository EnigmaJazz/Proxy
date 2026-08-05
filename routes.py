"""
routes.py - FastAPI route handlers for the Kinver AI Proxy.

Extracted from proxy.py to keep the entry-point file clean and focused
on application assembly.  Each route handler is a standalone async
function that receives the shared dependencies (database, systemd,
cooling, hardware) via FastAPI's ``app.state``.

Routes:
- ``GET  /health``                      — Liveness probe
- ``GET  /v1/models``                   — Model discovery
- ``POST /v1/chat/completions``          — Main inference endpoint
- ``GET  /v1/system/transition-check``  — Pause queue for OS transitions
- ``GET  /v1/system/queue/resume``      — Manual queue resume

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import sqlite3
import subprocess
import time
import uuid
import httpx
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse

from constants import (
    STOP_SEQS,
    OPENAI_FORWARD_FIELDS,
    R11_AUTHORITY_FIELDS,
    ALL_MODEL_KEYS,
    BRIDGE_MODEL_KEYS,
    CODE_KEYWORDS,
    CPU_MODELS,
    FACTUAL_KEYWORDS,
    TOOL_KEYWORDS,
    _HEAVY_MODEL_KEYS,
    MODEL_LABELS,
    OPENCODE_AGENT,
    OPENCODE_SERVE_TIMEOUT,
    RUNTIME_CONTEXT_WINDOWS,
    get_logger,
)
from context_governance import (
    STATUS_SENTINEL,
    apply_context_governance,
    strip_proxy_status,
)
from search_enrichment import enrich_thin_search_results
from llm import (
    stream_llm,
    openrouter_cloud_escalation,
)
from opencode_bridge import opencode_chat
from routing import (
    RouteDecision,
    discriminate_caller,
    is_lane_b,
    classify_with_frontdesk,
    resolve_route_for_lane_a,
    detect_tool_loops,
    check_semantic_cache,
    extract_project_context,
    is_dream_process,
)
from text_to_structured import ToolCallTextToStructured, _format_status

if TYPE_CHECKING:
    from proxy import AppState

logger = get_logger("proxy.routes")

# ---------------------------------------------------------------------------
# /v1/system commands (embedded in user prompts)
# ---------------------------------------------------------------------------
_PAUSE_RE = re.compile(r"(?i)\s*/pause(?:\s+(\d+))?\s*$")
_RESUME_RE = re.compile(r"(?i)\s*/resume\s*$")
_CLOUD_RE = re.compile(r"/cloud")
_OPENCODE_RE = re.compile(r"/opencode")

# Sentinel-prefixed coding-decision question.  Visible inline (like the
# triage) and stripped from the OUTBOUND model-copy by strip_proxy_status,
# so the question never reaches the model — only the user's answer does.
_CODING_QUESTION_PREFIX = STATUS_SENTINEL + "⚡ Coding decision:"

# ---------------------------------------------------------------------------
# Tool-loop detection (defense in depth against model reasoning loops)
# ---------------------------------------------------------------------------
# When a model gets stuck calling the same tool with the same arguments
# repeatedly (e.g. ``cat /proc/self/status | grep no_new_privs`` in a
# loop), we want the proxy to break the cycle.  The detection has two
# parts:
#
#   1. **Per-job, per-turn tracking** (``_loop_detection_state``): a
#      sliding window of the last N tool call signatures seen in the
#      current request.  When 3+ identical calls appear in a row, the
#      proxy intercepts the next duplicate and emits a synthetic warning
#      instead of forwarding the tool_calls chunk.
#
#   2. **Cross-request (multi-turn) tracking** (``_session_state``):
#      detects loops ACROSS requests by hashing the first user message
#      + the server-side timestamp when the proxy first saw that
#      message.  The hash is the ``session_id``; the per-session loop
#      state is keyed by session_id.  Two simultaneous sessions with
#      the same first message but started at different times get
#      different session_ids, so they don't collide.
#
# Both states are process-local and capped with an LRU eviction so
# they don't leak memory across a long-lived proxy.

_LOOP_DETECTION_WINDOW = 3  # consecutive identical calls trigger detection
_LOOP_DETECTION_MAX_JOBS = 1000  # cap on tracked sessions (LRU eviction)
_LOOP_DETECTION_SESSION_TTL = 3600  # session state expires after 1 hour of inactivity


def _loop_state(app: FastAPI) -> dict[str, dict[str, Any]]:
    """Lazy accessor for the cross-request tool-loop state.

    The state lives on ``app.state`` (Rule 6 — no module-level mutable
    globals).  ``getattr``/``setattr`` so lifespan-less test apps and the
    real app both work.
    """
    state = app.state
    if not hasattr(state, "loop_detection_state"):
        state.loop_detection_state = {}
    return state.loop_detection_state


def _session_state(app: FastAPI) -> dict[str, dict[str, Any]]:
    """Lazy accessor for the cross-request session map on ``app.state``."""
    state = app.state
    if not hasattr(state, "session_state"):
        state.session_state = {}
    return state.session_state


def _coding_decision_state(app: FastAPI) -> dict[str, str]:
    """Lazy accessor for the per-session coding decision on ``app.state``.

    Keyed by ``session_id`` → ``"opencode"`` | ``"professional"``.  The
    decision is cached so a coding session only prompts once.
    """
    state = app.state
    if not hasattr(state, "coding_decisions"):
        state.coding_decisions = {}
    return state.coding_decisions


def _find_coding_question_index(messages: list[dict[str, Any]]) -> Optional[int]:
    """Index of the PENDING coding-decision question.

    The question is pending only when it is the last assistant message and
    the final message is the user's answer (the OpenAI turn shape:
    ``... question(assistant), answer(user)``).  Once the decision is made
    and the model responds, the question is no longer pending — later
    requests must NOT re-trigger the decision turn (which would re-route
    the old task and reprompt the model).
    """
    if not messages or messages[-1].get("role") != "user":
        return None
    question_idx = len(messages) - 2
    if question_idx < 0:
        return None
    msg = messages[question_idx]
    if (
        msg.get("role") == "assistant"
        and isinstance(msg.get("content"), str)
        and msg["content"].startswith(_CODING_QUESTION_PREFIX)
        and not msg.get("tool_calls")
    ):
        return question_idx
    return None


def _parse_coding_answer(text: str) -> str:
    """Map the user's reply to a routing decision.

    ``"opencode"`` when the reply mentions opencode; ``"cancel"`` when it
    aborts the task; otherwise the safe default ``"professional"`` (the
    local code pathway).
    """
    lowered = (text or "").strip().lower()
    if _is_cancel_answer(lowered):
        return "cancel"
    if any(k in lowered for k in ("opencode", "/opencode")):
        return "opencode"
    return "professional"


def _is_cancel_answer(text: str) -> bool:
    """True when the reply aborts the pending coding task.

    Exact-match on short abort phrases so a longer reply that merely
    contains "stop" is not treated as a cancellation.
    """
    lowered = (text or "").strip().lower().rstrip(".!")
    return lowered in (
        "cancel", "cancel it", "cancel that", "never mind",
        "nevermind", "abort", "stop", "stop it", "forget it",
    )


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """Content of the last non-empty user message."""
    for msg in reversed(messages):
        content = msg.get("content")
        if msg.get("role") == "user" and isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _looks_like_gibberish(text: str) -> bool:
    """True when the text has fewer than two alphabetic words — the
    profile of complete nonsense (e.g. ``asdfghjkl12345!!!@@@``).  Used as
    a guard so the 2B frontdesk's ``is_valid=False`` cannot false-positive
    on short-but-meaningful queries like ``help``.  The ``[role]:`` labels
    from the context dump are stripped first so they don't count as words.
    """
    stripped = re.sub(r"\[[^\]]+\]:\s*", "", text or "")
    return len(re.findall(r"[a-zA-Z]+", stripped)) < 2


# Keyboard-row substrings that almost never appear in real words — a
# reliable pure-alpha keyboard-mash signature (e.g. "asdfghjkl").
_KEYBOARD_MASH: tuple[str, ...] = ("qwerty", "asdf", "zxcv")


def _is_deterministic_noise(text: str) -> bool:
    """Strong, frontdesk-independent nonsense detection.

    Empty input; fewer than two alphabetic words WITH digits or
    punctuation (``asdfghjkl12345!!!@@@``); or a single long vowel-poor
    token / keyboard-row mash (``asdfghjkl``, ``qwertyuiop``).  Pure short
    alpha tokens (``help``, ``hi``) are NOT noise; those fall back to the
    frontdesk's ``is_valid`` judgment.  This makes the nonsense intercept
    reliable even though the 2B's is_valid is inconsistent.
    """
    stripped = re.sub(r"\[[^\]]+\]:\s*", "", text or "").strip()
    if not stripped:
        return True
    words = re.findall(r"[a-zA-Z]+", stripped)
    if len(words) >= 2:
        return False
    if re.search(r"[0-9!@#$%^&*()_+=|~`<>?{}\[\]\\/]", stripped):
        return True
    if words:
        word = words[0].lower()
        # Keyboard-row mash ("asdfghjkl", "qwertyuiop", "zxcvbnm")
        if any(seq in word for seq in _KEYBOARD_MASH):
            return True
        # Long vowel-poor token (≤1 vowel in ≥6 letters) — keyboard mash
        # like "asdfghjkl" has no natural vowel ratio.
        if len(word) >= 6:
            vowels = sum(1 for ch in word if ch in "aeiou")
            if vowels <= 1:
                return True
    return False


async def _plain_completion(text: str, client_stream: bool) -> Response:
    """Return a plain assistant-message completion (SSE or JSON).

    Used for proxy-authored replies that ARE the model's answer (nonsense
    clarification), not proxy status — so no sentinel prefix.
    """
    if client_stream:
        async def _stream() -> AsyncIterator[str]:
            chunk = {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "proxy-system",
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")

    return JSONResponse({
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "proxy-system",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


def _tool_call_signature(tc: dict[str, Any]) -> str:
    """Build a stable signature for a tool call.

    The signature is ``f"{name}:{args}"`` so two calls with the same
    function name AND the same arguments string hash to the same
    signature.  Used to detect consecutive duplicates.
    """
    name = tc.get("function", {}).get("name", "")
    args = tc.get("function", {}).get("arguments", "")
    return f"{name}:{args}"


def _first_user_message_content(messages: list[dict[str, Any]]) -> str:
    """Return the content of the first user-role message in the
    conversation, or an empty string if there is none.  This is the
    stable identifier we use for cross-request session tracking.

    We skip system messages because every nanobot-ai request has a long
    system prompt that's identical across all requests in the same
    conversation (and across different conversations that share the
    same template).  The first USER message is what actually
    distinguishes one conversation from another.
    """
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                # Some clients send content as a list of typed parts
                # (e.g. ``[{"type": "text", "text": "..."}]``).  Join the
                # text parts for a stable identifier.
                content = " ".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            return str(content) if content else ""
    return ""


def _resolve_session_id(messages: list[dict[str, Any]], app: FastAPI) -> str:
    """Return a session ID for a request, identifying the conversation.

    The session ID is the SHA-256 of the first user message combined
    with the server-side timestamp when the proxy first saw that
    first user message.  Two sessions that start with the same first
    user message but at different times get different session IDs.

    The state is cached so that subsequent requests in the same
    conversation get the same session_id (using the ORIGINAL first-
    seen timestamp, not the timestamp of the current request).
    """
    first_msg = _first_user_message_content(messages)
    if not first_msg:
        # No user message — fall back to a per-request random session.
        return f"anon:{uuid.uuid4().hex}"
    first_msg_hash = hashlib.sha256(first_msg.encode("utf-8")).hexdigest()[:16]
    session_state = _session_state(app)
    cached = session_state.get(first_msg_hash)
    now = time.time()
    if cached is not None and (now - cached["first_seen"]) < _LOOP_DETECTION_SESSION_TTL:
        # Cache hit — return the original session_id.
        cached["last_seen"] = now
        return cached["session_id"]
    # New session — first time we see this first user message (or
    # the cache expired).  Generate a fresh session_id.
    session_id = f"{first_msg_hash}:{int(now)}"
    session_state[first_msg_hash] = {
        "session_id": session_id,
        "first_seen": now,
        "last_seen": now,
    }
    # LRU-ish cap on total sessions tracked.
    if len(session_state) > _LOOP_DETECTION_MAX_JOBS:
        # Drop the session with the smallest last_seen (oldest access).
        oldest_key = min(session_state, key=lambda k: session_state[k]["last_seen"])
        session_state.pop(oldest_key, None)
    return session_id


def _check_tool_loop(
    session_id: str,
    signature: str,
    app: FastAPI,
) -> bool:
    """Record a tool call signature and return True if it's a loop.

    Returns True when the same signature has appeared at least
    ``_LOOP_DETECTION_WINDOW`` times consecutively for this session.
    Resets the window when a different signature is seen.
    """
    loop_state = _loop_state(app)
    entry = loop_state.setdefault(
        session_id, {"signatures": [], "last_seen": time.time()}
    )
    entry["last_seen"] = time.time()
    history = entry["signatures"]
    if history and history[-1] != signature:
        # Different tool call — reset the consecutive-run window.
        history.clear()
    history.append(signature)
    # Cap per-session history to the window size.
    if len(history) > _LOOP_DETECTION_WINDOW:
        del history[0 : len(history) - _LOOP_DETECTION_WINDOW]
    # LRU-ish cap on total sessions tracked.
    if len(loop_state) > _LOOP_DETECTION_MAX_JOBS:
        # Drop the session with the oldest last_seen to bound memory.
        # Each entry carries its own last_seen so the eviction is
        # self-contained: loop keys are session_ids ("hash:ts") or
        # job_ids, which do NOT match the _session_state key space
        # (bare first-message hash).
        oldest_key = min(loop_state, key=lambda k: loop_state[k]["last_seen"])
        loop_state.pop(oldest_key, None)
    return len(history) >= _LOOP_DETECTION_WINDOW and all(
        s == signature for s in history
    )


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

async def health_check(request: Request) -> JSONResponse:
    """
    Liveness probe returning proxy uptime and current status.

    Used by monitoring systems and the SystemdController health probes.
    """
    state = request.app.state
    return JSONResponse({
        "status": "ok",
        "uptime_seconds": time.time() - state.server_start_ts,
        "active_model": state.active_heavy_model or "none",
        "active_priority": state.active_priority,
        "requests_served": state.requests_served,
        "gpu_occupied": state.systemd.is_gpu_occupied()
        if state.systemd else False,
    })


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------

async def list_models(request: Request) -> JSONResponse:
    """
    Enumerate available llama.cpp models from Systemd services.

    Returns an OpenAI-compatible model list with an ``"auto"`` entry
    for proxy-managed routing.
    """
    state = request.app.state
    systemd = state.systemd

    if systemd is None:
        return JSONResponse(
            {"object": "list", "data": []},
        )

    models = await systemd.scan_models()
    # Prepend the "auto" routing entry
    models.insert(0, {
        "id": "auto",
        "object": "model",
        "owned_by": "proxy-orchestrator",
        "port": 0,
    })
    # Bridge model keys (opencode serve backend) — pickable like any model.
    for key in sorted(BRIDGE_MODEL_KEYS):
        models.append({
            "id": key,
            "object": "model",
            "owned_by": "opencode-bridge",
            "port": 0,
        })
    return JSONResponse({"object": "list", "data": models})


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------


async def _govern_messages(
    request: Request,
    messages: list[dict[str, Any]],
    model_key: str,
    max_tokens: int,
) -> list[dict[str, Any]]:
    """Apply frontend-agnostic context governance to the OUTBOUND copy.

    Governed messages replace the model-copy only; the client's stored
    conversation and the DB audit copy are never touched.  Opt out per
    request with ``X-Proxy-Context-Governance: off`` (the opt-out applies
    to the budget transforms only — the proxy-status strip below is the
    echo fix and always runs).

    The budget transform may offload oversized tool results to disk, so
    it runs in a worker thread (async-safe; never blocks the event loop).
    """
    # Always strip proxy-owned status content (sentinel-prefixed) so the
    # model never sees its own triage/loading/tool-status echoed back.
    messages = strip_proxy_status(messages)
    # Search-result enrichment (explicit user-approved R1 carve-out
    # extension): frontends execute search_web themselves and often return
    # thin SEO snippets; when that happens, append the proxy's own rich
    # search (SearXNG + FlashRank + article scrape) to the thin tool result
    # on the OUTBOUND copy so the model can answer from live data.  Opt out
    # per request with ``X-Proxy-Search-Enrichment: off``.
    if request.headers.get("x-proxy-search-enrichment", "").strip().lower() != "off":
        messages = await enrich_thin_search_results(messages)
    header = request.headers.get("x-proxy-context-governance", "")
    if header.strip().lower() == "off":
        return messages
    context_window = RUNTIME_CONTEXT_WINDOWS.get(model_key)
    if not context_window:
        return messages
    return await asyncio.to_thread(
        apply_context_governance,
        messages,
        model_key=model_key,
        context_window=context_window,
        max_output_tokens=max_tokens,
    )


async def chat_completions(request: Request) -> Response:
    """
    OpenAI-compatible chat completions endpoint.

    Routing logic:

    1. Parse request body (messages, model, temperature, top_p, max_tokens, tools).
    2. Determine lane from headers (``sk-ide-pass`` → Lane B).
    3. Lane A: classify with 2B Front Desk → intent, priority, project, is_factual.
    4. Lane B: bypass frontdesk, intent=CODE, priority=1.
    5. Semantic cache check for factual queries.
    6. Resolve route (CHAT/TOOL → professional; heavy intents enqueued).
    7. Handle embedded commands (/pause, /resume, /cloud).
    8. Stream response via SSE, with cooling lifecycle.
    """
    state = request.app.state
    db = state.database
    systemd = state.systemd
    cooler = state.cooler
    hw = state.hardware

    # ---- Parse request body -----------------------------------------------
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    messages: list[dict[str, Any]] = body.get("messages", [])
    if not messages:
        return JSONResponse({"error": "messages array required"}, status_code=400)

    requested_model = body.get("model", "auto").lower()
    tools = body.get("tools", None)
    # The proxy always streams to llama.cpp internally, but a client that
    # did NOT request streaming expects a JSON ChatCompletion body back
    # (OpenAI non-streaming semantics — the default when ``stream`` is
    # omitted or false).  nanobot's dream/heartbeat cron calls the
    # provider non-streaming, and the OpenAI SDK only sends
    # ``"stream": true`` when streaming is requested, so the request
    # body arrives WITHOUT the field.  Without this default the proxy
    # returned raw SSE text, the SDK stored it as the assistant message,
    # and zero tool_calls were parsed.  See ``_collect_chat_completion``.
    client_stream = bool(body.get("stream", False))
    temperature = body.get("temperature", 0.7)
    top_p = body.get("top_p", 1.0)
    max_tokens = body.get("max_tokens", 4096)
    thinking_budget_tokens = body.get("thinking_budget_tokens")

    # R11 fields beyond temperature/top_p/max_tokens — forwarded in direct
    # mode and overridden by profile values in auto-routed/dream mode.
    seed = body.get("seed")
    top_logprobs = body.get("top_logprobs")
    response_format = body.get("response_format")

    # ---- Discriminate caller type ------------------------------------------
    headers = dict(request.headers)
    caller_type = discriminate_caller(headers)
    lane_b = is_lane_b(headers)
    is_dream = False

    # ---- Prepare messages (Glass Pipe Rule: NO text alteration) ------------
    processed_messages: list[dict[str, Any]] = list(messages)  # Shallow copy

    # ---- Lane B (IDE passthrough): bypass frontdesk ----------------------
    if caller_type == "IDE" or lane_b:
        # Classic IDE clients (aider/cline/vscode) get no tools — they
        # handle text-only model responses.  opencode is different: it is
        # an agent framework that sends its OWN tool definitions and
        # executes the model's tool calls itself, so its tools must be
        # forwarded or its agent loop degrades to text-only answers.
        if "opencode" not in request.headers.get("user-agent", "").lower():
            tools = None
        # Messages pass through unchanged
        logger.info("Lane B (IDE passthrough) — bypassing frontdesk")
    elif caller_type == "AGENTIC":
        # ---- AGENTIC: handle tool loops and inject native tools -------------
        # Strip "llama-" prefix if present for domain matching
        model_domain = requested_model
        if model_domain.startswith("llama-"):
            model_domain = model_domain.replace("llama-", "")

        # Dream detection via Nanobot template phrases (see routing.py).
        # Template phrases like "extract new facts from conversation history"
        # only appear in Nanobot's autonomous dream tasks — they do NOT
        # appear in regular chat, making them a reliable fingerprint.
        raw_text = " ".join(
            m.get("content", "") for m in processed_messages
            if isinstance(m.get("content"), str)
        ).lower()
        is_dream = await is_dream_process(raw_text)

        effective_domain = model_domain if model_domain in (
            "coder", "architect", "professional", "creative", "scholar",
        ) else "standard"

        # Tool loop detection — strip tools on detected loops to prevent
        # agentic death spirals.  No prompt content is injected (Glass Pipe).
        # Frontends own tool execution; the proxy only strips access to halt
        # runaway loops.  Native tool injection (web_search) and tool usage
        # instructions will be provided via MCP server in a future iteration.
        is_looping, loop_reason = await detect_tool_loops(
            processed_messages,
            domain=effective_domain,
            database=db,
        )

        if is_looping:
            logger.warning(
                "Tool loop detected for domain '%s': %s — stripping tools",
                effective_domain, loop_reason,
            )
            tools = None

    # R17 trigger: proxy owns model pick → proxy owns sampling parameters.
    # Direct calls (client picked the model) keep R1/R7 client-wins.
    auto_authority = (requested_model == "auto") or (is_dream and caller_type == "AGENTIC")

    # ---- Build full conversation context for frontdesk classification ------
    # Uses newest-first truncation: the latest user message is ALWAYS
    # fully preserved.  Older messages are dropped first when the
    # 2000-char budget is exceeded.  This ensures the 2B model sees
    # the complete current request with recent context for references.
    MAX_CONTEXT_CHARS = 2000

    context_messages = []
    total_chars = 0

    for m in reversed(processed_messages):
        role = m.get("role", "")
        content = m.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        line = f"[{role}]: {content}\n"
        if total_chars + len(line) > MAX_CONTEXT_CHARS:
            break
        context_messages.insert(0, line)  # Prepend for chronological order
        total_chars += len(line)

    user_text = "".join(context_messages).strip()

    # ---- Handle embedded commands -------------------------------------------
    # /pause [minutes]
    pause_match = _PAUSE_RE.search(user_text) if user_text else None
    if pause_match:
        duration_mins = int(pause_match.group(1)) if pause_match.group(1) else 60
        return await _handle_pause_command(duration_mins, state)

    # /resume
    if user_text and _RESUME_RE.search(user_text):
        return await _handle_resume_command(state)

    # /cloud
    if user_text and _CLOUD_RE.search(user_text.lower()):
        return await _handle_cloud_command(user_text)

    # /opencode — direct the request to the opencode agent instead of a
    # local model (the programmatic escape hatch for coding tasks).
    if user_text and _OPENCODE_RE.search(user_text.lower()):
        return await _handle_opencode_command(user_text)

    # model: "opencode" — client picked the opencode bridge model.  This
    # bypasses llama routing entirely: the task goes to the headless
    # opencode serve backend (gentle-orchestrator SDD agent).
    if requested_model in BRIDGE_MODEL_KEYS:
        return await _handle_opencode_request(processed_messages, client_stream)

    # ---- Dream/soul fast-path: bypass frontdesk, route to professional ----
    if is_dream:
        logger.info("Dream/soul process detected — routing to professional (priority 3)")
        port = await systemd.get_port("professional")
        route = RouteDecision(
            model_key="professional",
            port=port,
            is_cpu_fallback=False,
            hardware_path="hybrid",
            priority=3,
            intent="ARCHITECT",
            project_id="soul",
            is_factual=False,
            is_lane_b=False,
            bypass_frontdesk=True,
            tools_required=False,
        )
        # Jump straight to payload building, skipping frontdesk + cache
        # R17: dream/soul path is authoritative — use professional profile values
        # when available, otherwise fall back to the legacy hardcoded defaults.
        profiles = state.model_profiles
        entry = profiles.resolve("ARCHITECT", "professional") if profiles else None
        if entry is not None:
            parameters = {**entry.values}
        else:
            parameters = {
                "temperature": 0.2,
                "max_tokens": 4096,
                "thinking_budget_tokens": 4096,
            }

        job_id = str(uuid.uuid4())
        if db:
            job_id = await db.enqueue_job(
                messages_json=json.dumps(processed_messages),
                priority=3,
                intent="ARCHITECT",
                project_id="soul",
                tools_json=json.dumps(tools) if tools else None,
                parameters_json=json.dumps(parameters),
                lane="lane_a",
                is_lane_b=False,
                caller_type="AGENTIC",
            )
        payload = {
            "messages": await _govern_messages(
                request,
                processed_messages,
                model_key="professional",
                max_tokens=int(parameters["max_tokens"]),
            ),
            "temperature": parameters["temperature"],
            "max_tokens": parameters["max_tokens"],
            "stream": True,
            "stop": STOP_SEQS,
        }
        if "thinking_budget_tokens" in parameters:
            payload["thinking_budget_tokens"] = parameters["thinking_budget_tokens"]
        if tools:
            payload["tools"] = tools

        proxy_preamble: list[str] = []
        if entry is not None:
            # Only report fields actually forwarded to the model — the
            # dream payload carries a subset of the R11 profile set.
            replaced_fields = [f for f in entry.values if f in payload]
            params_replaced_payload = {
                "kind": "params_replaced",
                "ts": int(time.time()),
                "data": {
                    "model": "professional",
                    "replaced": replaced_fields,
                    "values": {field: entry.values[field] for field in replaced_fields},
                },
            }
            proxy_preamble.append(
                _make_proxy_event("kinver.proxy.params_replaced", params_replaced_payload)
            )

        from cooling import CoolingStateMachine
        hardware_path = CoolingStateMachine.hardware_path_for_model("professional")
        session_id = _resolve_session_id(processed_messages, request.app)
        stream_gen = _event_stream_with_model_startup(
            state=state, app=request.app, route=route, payload=payload, fwd_headers={},
            job_id=job_id, project_id="soul",
            processed_messages=processed_messages,
            requested_model="professional",
            hardware_path=hardware_path,
            proxy_preamble=proxy_preamble,
            session_id=session_id,
        )
        if not client_stream:
            # Non-streaming client (e.g. nanobot dream/heartbeat): return
            # a JSON ChatCompletion assembled from the SSE stream.
            return JSONResponse(await _collect_chat_completion(stream_gen))
        return StreamingResponse(
            stream_gen,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ---- Lane A: classify intent via 2B Front Desk --------------------------
    # Check if the conversation already has tool calls in progress (from
    # a previous tool-calling interaction in this session).  If so, skip
    # frontdesk classification entirely and preserve the classified route —
    # reclassifying mid-tool-flow causes wrongful model switches that break
    # the tool execution chain.
    # Check if the LAST message in the conversation is a tool_call or tool
    # result (mid-tool-flow).  Only skip frontdesk when the model is actively
    # executing tools — NOT when the user sends a new message after a
    # previous tool-calling interaction.  This prevents "Tell me a joke"
    # from being misclassified as TOOL simply because the conversation
    # history contains old tool_calls.
    last_msg = processed_messages[-1] if processed_messages else {}
    has_tool_calls = (
        last_msg.get("role") == "assistant" and "tool_calls" in last_msg
    ) or (
        last_msg.get("role") == "tool"
    )

    classification: dict[str, Any] = {
        "is_valid": True,
        "intent": "CHAT",
        "priority": 2,
        "complexity": "low",
        "project_name": "general",
        "is_factual": False,
        "tools_required": False,
    }

    if has_tool_calls:
        # Conversation already has tool calls — preserve the classified
        # route, don't let frontdesk reclassify and switch models
        classification["intent"] = "TOOL"
        classification["tools_required"] = True
        logger.info(
            "Mid-tool-flow detected (%d messages with tool_calls) — "
            "skipping frontdesk, preserving classified route",
            sum(1 for m in processed_messages
                if m.get("role") in ("assistant", "tool")
                and ("tool_calls" in m or m.get("role") == "tool")),
        )
    elif not lane_b and caller_type != "IDE":
        # Resolve the frontdesk port live from the systemd unit file so
        # that ``call_llm()`` connects to the correct port.  Falls back
        # to 0 (hard-coded dict) if systemd resolution fails.
        frontdesk_port = await systemd.get_port("frontdesk")

        # Extract tool names from the client-provided tools array so the
        # frontdesk knows what tools are available when deciding
        # tools_required.  Example: {"type":"function","function":{"name":"web_search"}} → "web_search"
        available_tool_names = None
        if tools:
            available_tool_names = [
                t.get("function", {}).get("name", "")
                for t in tools
                if isinstance(t, dict)
            ]

        classification = await classify_with_frontdesk(
            user_text,
            frontdesk_port=frontdesk_port,
            available_tool_names=available_tool_names,
        )
        logger.info(
            "Frontdesk classified: intent=%s priority=%s project=%s factual=%s",
            classification.get("intent"),
            classification.get("priority"),
            classification.get("project_name"),
            classification.get("is_factual"),
        )
    else:
        classification["intent"] = "CODE"
        classification["priority"] = 1
        logger.info("Lane B — frontdesk bypassed, intent forced to CODE")

    # ---- Tool keyword heuristic: safety net for 2B frontdesk limitations ---
    # If the 2B frontdesk classified as CHAT but the query contains obvious
    # tool-triggering keywords (weather, file I/O, web search, exec), force
    # intent to TOOL.  Over-detection is safe because TOOL-routed requests
    # reach a capable model (professional) that handles plain chat too.
    if classification.get("intent") == "CHAT":
        user_lower = user_text.lower()
        for kw in TOOL_KEYWORDS:
            if kw in user_lower:
                logger.info(
                    "Tool keyword '%s' matched — overriding CHAT → TOOL", kw,
                )
                classification["intent"] = "TOOL"
                classification["tools_required"] = True
                break

    # ---- Code keyword heuristic: safety net for 2B frontdesk limitations ---
    # The 2B frontdesk sometimes misses explicit coding requests ("write a
    # python script") and classifies them as CHAT, which would skip the
    # coding-decision gate and the code profile.  Force CHAT → CODE when
    # the query carries a strong coding signal.  Over-detection is safe:
    # CODE and CHAT both route to Professional.
    if classification.get("intent") == "CHAT":
        user_lower = user_text.lower()
        for kw in CODE_KEYWORDS:
            if kw in user_lower:
                logger.info(
                    "Code keyword '%s' matched — overriding CHAT → CODE", kw,
                )
                classification["intent"] = "CODE"
                break

    # ---- Factual keyword heuristic: make the semantic cache useful --------
    # The 2B frontdesk's is_factual is unreliable (it marked "what is the
    # capital of france" as non-factual), starving the semantic cache.
    # Force it for obvious factual phrasings so repeat factual questions
    # are served from cache without waking the GPU.
    if not classification.get("is_factual"):
        user_lower = user_text.lower()
        for kw in FACTUAL_KEYWORDS:
            if kw in user_lower:
                classification["is_factual"] = True
                break

    # ---- Invalid-input interception (frontdesk is_valid + noise guard) ------
    # The 2B frontdesk's ``is_valid`` signal was computed but never acted
    # on: complete nonsense still burned the professional model.  Intercept
    # it here.  The deterministic noise guard fires on its own (the 2B's
    # is_valid is inconsistent); the ``is_valid`` signal additionally
    # catches pure-alpha mash like ``asdfghjkl``, guarded by the word-count
    # check so short-but-meaningful queries ("help") are never blocked.
    #
    # Mid-tool-flow requests (last message is a tool call or tool result)
    # are NEVER intercepted: their context dump includes tool results that
    # can legitimately look like noise (short numbers, JSON, "True"), and
    # intercepting would break an in-flight tool chain mid-stream.
    if not has_tool_calls and (_is_deterministic_noise(user_text) or (
        classification.get("is_valid") is False
        and _looks_like_gibberish(user_text)
    )):
        logger.info(
            "Intercepting nonsense input (%r)",
            user_text[:60],
        )
        return await _plain_completion(
            "⚡ I couldn't understand that message — it looks like it may "
            "have been garbled. Could you rephrase it?",
            client_stream,
        )

    # ---- Semantic cache check for factual queries ---------------------------
    if classification.get("is_factual"):
        cached = await check_semantic_cache(user_text, True, db)
        if cached:
            logger.info("Returning cached response for factual query")
            return await _stream_cached_response(
                cached, requested_model, client_stream=client_stream,
            )

    # ---- Resolve route (GPU-aware) ------------------------------------------
    route: RouteDecision
    if lane_b or caller_type == "IDE":
        # Lane B: always route to professional (35B MoE)
        port = await systemd.get_port("professional")
        route = RouteDecision(
            model_key="professional",
            port=port,
            is_cpu_fallback=False,
            hardware_path="gpu",
            priority=1,
            intent="CODE",
            project_id="general",
            is_factual=False,
            is_lane_b=True,
            bypass_frontdesk=True,
        )
    else:
        # Lane A: GPU-aware routing.  CHAT/TOOL contention with a
        # specialist routes to Professional unconditionally.
        route = await resolve_route_for_lane_a(
            classification, systemd,
            has_tool_history=has_tool_calls,
        )

    # ---- Client-named-model override (R19) ---------------------------------
    # When the client picks a specific model (not "auto"), use it directly
    # instead of the frontdesk-classified one. Lane B / IDE passthrough is
    # exempt — it always pins "professional" by design.  intent is preserved
    # so the profile lookup for thinking_budget_tokens still works.
    #
    # Also clears route.is_cpu_fallback: the override is an explicit client
    # request, not a fallback — the hotswap wrapper at routes.py:871
    # suppresses the cold-start when this flag is True, which would break
    # transitions like "GPU busy with coder → explicit
    # Scholar request".  Without this line, the specialist port is never
    # started and the stream ends in 'All connection attempts failed'.
    client_named_model = False
    if (
        not route.is_lane_b
        and requested_model != "auto"
        and requested_model in ALL_MODEL_KEYS
        and route.model_key != requested_model
    ):
        route.model_key = requested_model
        route.port = await systemd.get_port(requested_model)
        route.hardware_path = "cpu" if requested_model in CPU_MODELS else "gpu"
        route.is_cpu_fallback = False
        client_named_model = True
        logger.info(
            "Client-named model override: %s → %s",
            route.model_key, requested_model,
        )

    # ---- Coding-task decision gate -------------------------------------------
    # Coding requests from non-opencode clients get a one-turn choice:
    # route to OpenCode (the bridge) or the local code pathway (Professional).
    # Requests from opencode itself (Lane B / IDE) skip the gate and go
    # straight to the local model (loop-prevention requirement).
    # ``session_id`` is only resolved inside the AGENTIC branch above; the
    # accessor is idempotent (cached by first-user-message hash) so resolving
    # it here is safe for every caller.
    gate_session_id = _resolve_session_id(processed_messages, request.app)
    gate_response = await _apply_coding_decision_gate(
        app=request.app,
        messages=processed_messages,
        session_id=gate_session_id,
        client_stream=client_stream,
        route=route,
        has_tool_calls=has_tool_calls,
    )
    if gate_response is not None:
        return gate_response

    # ---- Extract project context for database ---------------------------------
    ctx = extract_project_context(user_text)
    project_name = ctx["project"] if ctx["project"] != "general" else classification.get("project_name", "general")

    # ---- Create project in database ------------------------------------------
    project_id = "general"
    if db:
        project_id = await db.get_or_create_project(project_name, ctx.get("root", ""))

    # ---- Update application state -------------------------------------------
    state.active_priority = route.priority
    # Only update active_heavy_model for GPU routes — never clear to None
    # when routing to a CPU-only model, because a heavy GPU model may still
    # be actively streaming for an ongoing generation.  Clearing it would
    # trigger cleanup logic that kills the in-progress stream.
    if route.hardware_path == "gpu":
        state.active_heavy_model = route.model_key
    state.requests_served += 1

    # ---- Build generation parameters ---------------------------------------
    # R17: when the proxy picked the model, profile values own the full R11
    # set; otherwise R1/R7 client-wins stays in force.
    profiles = state.model_profiles
    entry = profiles.resolve(route.intent, route.model_key) if profiles else None

    if auto_authority and entry is not None:
        parameters = {**entry.values}
    else:
        # R1/R7 client-wins: when the client picked the model, the client's
        # sampling parameters are authoritative. We only fill in a
        # model-specific default for thinking_budget_tokens when the client
        # omitted it.
        parameters = {
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
        }
        if entry is not None and "thinking_budget_tokens" in entry.values:
            parameters["thinking_budget_tokens"] = entry.values["thinking_budget_tokens"]
        if thinking_budget_tokens is not None:
            parameters["thinking_budget_tokens"] = thinking_budget_tokens

    # ---- Register job in database -------------------------------------------
    job_id = str(uuid.uuid4())
    if db:
        job_id = await db.enqueue_job(
            messages_json=json.dumps(processed_messages),
            priority=route.priority,
            intent=route.intent,
            project_id=project_id,
            tools_json=json.dumps(tools) if tools else None,
            parameters_json=json.dumps(parameters),
            lane="lane_b" if route.is_lane_b else "lane_a",
            is_lane_b=route.is_lane_b,
            caller_type=caller_type,
            model_override=requested_model if requested_model != "auto" else None,
        )

    # ---- Build payload for target model ------------------------------------
    # When tools are present, filter out ReAct-era stop sequences
    # ("Observation:", "```output") that can cause premature termination
    # mid-tool-call.  The model's native chat template handles tool calls
    # natively and doesn't need these Agent-loop artifacts.  Without tools,
    # all stop sequences are preserved for clean text generation.
    effective_stops = list(STOP_SEQS)
    if tools:
        effective_stops = [
            s for s in STOP_SEQS
            if s not in ("Observation:", "```output")
        ]

    payload = {
        "messages": await _govern_messages(
            request,
            processed_messages,
            model_key=route.model_key,
            max_tokens=int(parameters["max_tokens"]),
        ),
        "temperature": parameters["temperature"],
        "top_p": parameters["top_p"],
        "max_tokens": parameters["max_tokens"],
        "stream": True,
        "stop": effective_stops,
    }
    if "thinking_budget_tokens" in parameters:
        payload["thinking_budget_tokens"] = parameters["thinking_budget_tokens"]
    if tools:
        payload["tools"] = tools

    # Thinking-mode control — the proxy is the **authoritative** source
    # for ``enable_thinking`` on every request.  The service-level
    # ``--chat-template-kwargs '{"enable_thinking": false}'`` is a
    # fallback for clients that bypass the proxy, but it has been
    # observed to NOT be respected across multi-turn conversations on
    # Qwen 3.5 (the chat template honors the override for the first
    # turn only).  Per-request injection is the only reliable mechanism.
    #
    # Decision matrix (the proxy's contract):
    #
    #   - X-Proxy-Thinking: true  → enable_thinking: True (force on)
    #   - X-Proxy-Thinking: false → enable_thinking: False (force off)
    #   - complex intent (CODE/SCHOLAR/CREATIVE/ARCHITECT)
    #     AND classifier says tools NOT required → enable_thinking: True
    #   - simple intent, TOOL intent, or tools_required=True
    #                                          → enable_thinking: False
    #
    # NOTE: tool presence alone does NOT disable thinking anymore.  Every
    # real client (nanobot, opencode, OpenWebUI) attaches its tool set to
    # EVERY request, so a tools-presence gate made the thinking-ON path
    # unreachable in practice.  The intent + tools_required classification
    # is the driver; the tool-call corruption (text-based ``<tool_call>``
    # on later turns) is contained by the fixed chat template
    # (preserve_thinking: false) and the ToolCallTextToStructured state
    # machine, verified live (thinking ON + tools: 48 reasoning chunks,
    # 7 clean structured tool calls, 0 text-tool-call leak).  Mid-tool-flow
    # continuations classify as TOOL, so they stay thinking-off.
    thinking_header = headers.get("x-proxy-thinking", "").lower()
    if thinking_header == "true":
        payload["chat_template_kwargs"] = {"enable_thinking": True, "preserve_thinking": True}
    elif thinking_header == "false":
        payload["chat_template_kwargs"] = {"enable_thinking": False, "preserve_thinking": False}
    else:
        intent = (classification or {}).get("intent", "").upper()
        tools_required = (classification or {}).get("tools_required", False)
        if (
            intent in ("CODE", "SCHOLAR", "CREATIVE", "ARCHITECT")
            and not tools_required
        ):
            payload["chat_template_kwargs"] = {"enable_thinking": True, "preserve_thinking": True}
        else:
            # CHAT, TOOL, or tools_required=True: no thinking
            payload["chat_template_kwargs"] = {"enable_thinking": False, "preserve_thinking": False}

    # Forward additional OpenAI fields from the client body (R11 hardening).
    for field in OPENAI_FORWARD_FIELDS:
        if body.get(field) is not None:
            payload[field] = body[field]

    # R17: re-apply profile values for the R11 set so profile wins regardless
    # of any client-forwarded values.
    if auto_authority and entry is not None:
        for field in R11_AUTHORITY_FIELDS:
            if field in entry.values:
                payload[field] = entry.values[field]

    # ---- Forward headers for Lane B ----------------------------------------
    fwd_headers: dict[str, str] = {}
    if route.is_lane_b:
        fwd_headers["X-IDE-Mode"] = "true"

    # ---- Determine cooling hardware path -----------------------------------
    # Resolve from the cooling module's classification.  This ensures
    # hybrid models (architect, coder, creative, professional, scholar)
    # write to BOTH CPU and GPU IPC files, CPU-only models (frontdesk)
    # write only to CPU, and GPU-only models (chatter) write
    # only to GPU.
    from cooling import CoolingStateMachine
    hardware_path = CoolingStateMachine.hardware_path_for_model(route.model_key)

    # R17: build params_replaced event when authority was applied.
    proxy_preamble: list[str] = []
    if auto_authority and entry is not None:
        replaced_fields = list(entry.values.keys())
        params_replaced_payload = {
            "kind": "params_replaced",
            "ts": int(time.time()),
            "data": {
                "model": route.model_key,
                "replaced": replaced_fields,
                "values": {field: entry.values[field] for field in replaced_fields},
            },
        }
        proxy_preamble.append(
            _make_proxy_event("kinver.proxy.params_replaced", params_replaced_payload)
        )

    # ---- Build & return the SSE stream --------------------------------------
    logger.info(
        "Routing to %s (lane=%s intent=%s priority=%d cpu_fallback=%s)",
        route.model_key,
        "lane_b" if route.is_lane_b else "lane_a",
        route.intent,
        route.priority,
        route.is_cpu_fallback,
    )

    stream_gen = _event_stream_with_model_startup(
        state=state,
        app=request.app,
        route=route,
        payload=payload,
        fwd_headers=fwd_headers,
        job_id=job_id,
        project_id=project_id,
        processed_messages=processed_messages,
        requested_model=requested_model,
        hardware_path=hardware_path,
        proxy_preamble=proxy_preamble,
        client_named_model=client_named_model,
        session_id=_resolve_session_id(processed_messages, request.app),
        cache_query=user_text if classification.get("is_factual") else "",
    )
    if not client_stream:
        # Non-streaming client: return a JSON ChatCompletion assembled
        # from the SSE stream (see ``_collect_chat_completion``).
        return JSONResponse(await _collect_chat_completion(stream_gen))
    return StreamingResponse(
        stream_gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# SSE event stream generator
# ---------------------------------------------------------------------------

async def _event_stream(
    state: "AppState",
    app: FastAPI,
    route: RouteDecision,
    payload: dict[str, Any],
    fwd_headers: dict[str, str],
    job_id: str,
    project_id: str,
    processed_messages: list[dict[str, Any]],
    requested_model: str,
    hardware_path: str,
    proxy_preamble: Optional[list[str]] = None,
    client_named_model: bool = False,
    session_id: str = "",  # SHA-256[:16]:<first_seen_unix> for cross-request loop detection
    cache_query: str = "",  # user_text for semantic-cache storage (factual queries)
) -> AsyncIterator[str]:
    """
    Core SSE streaming generator.

    1. Prefill burst cooling.
    2. Stream chunks from the LLM via ``stream_llm``.
    3. On first chunk: step down cooling to GENERATION.
    4. On [DONE]: complete job in DB, baseline cooling.
    """
    db = state.database
    systemd = state.systemd
    cooler = state.cooler
    # Use the per-conversation session_id (if provided) for tool-loop
    # detection so multi-turn loops (across requests) are caught.  Fall
    # back to job_id for backwards compatibility with callers that
    # don't pass a session_id yet.
    loop_key = session_id or job_id

    # ---- Prefill burst cooling ---------------------------------------------
    if cooler:
        await cooler.prefill_burst(hardware_path)

    first_chunk_seen = False
    full_content: list[str] = []
    chunk_seq = 0
    accumulated = ""
    # State machine for converting Qwen-style text tool calls in
    # delta.content to OpenAI structured delta.tool_calls.  The fixed
    # chat template (froggeric v21) instructs the model to emit
    # <tool_call>...</tool_call> blocks in content, but OpenAI-compatible
    # clients expect delta.tool_calls JSON.  This state machine bridges
    # the two formats.  See proxy/text_to_structured.py.
    text_to_structured = ToolCallTextToStructured()
    # Buffer for accumulating tool call chunks by index, so we can
    # emit a status message with the FULL arguments once the tool call
    # is complete (not per-chunk with partial args).  Each entry is
    # an accumulated tool call dict.
    pending_tool_calls: dict[int, dict[str, Any]] = {}
    # Indices whose native structured tool_calls were already relayed to
    # the client verbatim (llama.cpp parsed them itself).  Re-emitting the
    # accumulated copy at finish would concatenate duplicate arguments at
    # the client (e.g. '{"query":...}{"query":...}') which breaks JSON
    # parsing in OpenAI-compatible clients (nanobot-ai "got str" error).
    # Text-converted calls (the state machine) are NOT in this set: their
    # delivery happens at conversion time in one chunk (see
    # tts_tool_call_indices below), so they need no finish-time emission.
    streamed_tool_call_indices: set[int] = set()
    # Indices of text-converted tool calls (ToolCallTextToStructured).
    # The state machine emits the complete tool_call in one chunk at
    # conversion time (with a unique per-call index), so the finish-time
    # status/emission below must NOT repeat them: a second copy would be
    # concatenated onto the first by per-index-accumulating clients and
    # break JSON parsing, and a duplicate status line would confuse the
    # user.
    tts_tool_call_indices: set[int] = set()

    # ---- Yield proxy-injected preamble events -----------------------------
    # These events (e.g. params_replaced) are emitted before the triage
    # status chunk so consumers see substitution signals first.
    if proxy_preamble:
        for event_line in proxy_preamble:
            yield event_line

    # ---- Yield triage metadata as first SSE chunk ---------------------------
    # Let the frontend know which model was selected and why, so users
    # see the routing announcement during the model-loading gap.  Emitted
    # as sentinel-prefixed delta.content via _make_status_chunk: visible
    # inline, but stripped from the OUTBOUND model-copy on the next
    # request (strip_proxy_status) so the model never echoes it back.
    # Previously emitted as custom events, which the model never saw but
    # which ALSO made the status invisible to nanobot — users lost all
    # feedback during loading/tool gaps.  The sentinel restores inline
    # visibility without the echo degeneration.
    #
    # Mid-tool-flow requests (last message is a tool call or a tool
    # result) skip the triage entirely: the model is continuing a chain
    # it already started, so re-announcing the route adds noise AND
    # feeds the model's own input with repeated "Proxy triage" text.
    last_msg = processed_messages[-1] if processed_messages else {}
    mid_tool_flow = (
        last_msg.get("role") == "assistant" and "tool_calls" in last_msg
    ) or last_msg.get("role") == "tool"
    triage_msg = _build_triage_message(route, client_named_model=client_named_model)
    if not mid_tool_flow:
        yield _make_status_chunk(triage_msg, kind="triage")

    # Terminal outcome of this stream: completed | failed | cancelled.
    # Stays "unknown" if the generator is closed early (client disconnect
    # delivered as GeneratorExit) so the finally block can mark the job.
    outcome = "unknown"
    try:
        async for chunk in stream_llm(
            endpoint=route.model_key,
            payload=payload,
            port=route.port,
            headers=fwd_headers,
        ):
            # ---- First chunk: step down cooling ------------------------------
            if not first_chunk_seen:
                first_chunk_seen = True
                if cooler:
                    cooler.generation_hold(hardware_path)

            chunk_seq += 1

            # ---- Extract content delta ---------------------------------------
            choices = chunk.get("choices", [])
            delta_content = ""
            if choices:
                delta = choices[0].get("delta", {})
                delta_content = delta.get("content", "")
                if delta_content:
                    full_content.append(delta_content)
                    accumulated += delta_content

            # ---- Persist chunk to database ------------------------------------
            if db and delta_content:
                # Update partial content for crash recovery
                await db.update_partial_content(job_id, accumulated)
                # Record individual chunk
                await db.record_stream_chunk(
                    job_id, chunk_seq, json.dumps(chunk),
                )

            # ---- Convert text tool calls to structured delta.tool_calls ----
            # The Qwen 3.5 chat template instructs the model to emit
            # <tool_call>...</tool_call> blocks as text in delta.content.
            # OpenAI-compatible clients (nanobot-ai) expect structured
            # delta.tool_calls JSON.  The state machine below buffers
            # content across chunks, detects the text-based tool call
            # blocks, parses them, and emits structured chunks while
            # stripping the XML text from delta.content.
            if delta_content:
                tts_emits = text_to_structured.feed(delta_content)
                # When tts_emits is empty, the state machine held the
                # content back because a tool call started but hasn't
                # closed yet: emit nothing for this chunk, and the next
                # chunk will produce the emit when the tool call
                # completes.
                # Emit each tts_emit as a separate SSE chunk, preserving
                # the original chunk's other fields (id, model, role,
                # reasoning_content, finish_reason).
                for tts in tts_emits:
                    # If this is a tool_calls emit, accumulate the tool
                    # call chunks by index so we can emit a status
                    # message with the FULL arguments once complete.
                    if "tool_calls" in tts and tts["tool_calls"]:
                        for tc in tts["tool_calls"]:
                            idx = tc.get("index", 0)
                            tts_tool_call_indices.add(idx)
                            if idx not in pending_tool_calls:
                                pending_tool_calls[idx] = {
                                    "id": tc.get("id", ""),
                                    "type": tc.get("type", "function"),
                                    "function": {
                                        "name": "",
                                        "arguments": "",
                                    },
                                }
                            acc = pending_tool_calls[idx]
                            if "id" in tc and tc["id"]:
                                acc["id"] = tc["id"]
                            if "type" in tc and tc["type"]:
                                acc["type"] = tc["type"]
                            if "function" in tc:
                                func = tc["function"]
                                if "name" in func and func["name"]:
                                    acc["function"]["name"] = func["name"]
                                if "arguments" in func and func["arguments"]:
                                    acc["function"]["arguments"] += func["arguments"]
                    tts_chunk = {
                        "id": chunk.get("id"),
                        "object": "chat.completion.chunk",
                        "created": chunk.get("created"),
                        "model": chunk.get("model"),
                        "system_fingerprint": chunk.get("system_fingerprint"),
                        "choices": [{
                            "index": choices[0].get("index", 0),
                            "delta": tts,
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(tts_chunk)}\n\n"
            elif not delta_content:
                # No content in this chunk.  Check if it has tool_calls
                # (model produced structured tool calls directly) and
                # accumulate them by index.
                delta_tool_calls = (
                    choices[0].get("delta", {}).get("tool_calls")
                    if choices else None
                )
                if delta_tool_calls:
                    for tc in delta_tool_calls:
                        idx = tc.get("index", 0)
                        streamed_tool_call_indices.add(idx)
                        if idx not in pending_tool_calls:
                            pending_tool_calls[idx] = {
                                "id": tc.get("id", ""),
                                "type": tc.get("type", "function"),
                                "function": {
                                    "name": "",
                                    "arguments": "",
                                },
                            }
                        acc = pending_tool_calls[idx]
                        if "id" in tc and tc["id"]:
                            acc["id"] = tc["id"]
                        if "type" in tc and tc["type"]:
                            acc["type"] = tc["type"]
                        if "function" in tc:
                            func = tc["function"]
                            if "name" in func and func["name"]:
                                acc["function"]["name"] = func["name"]
                            if "arguments" in func and func["arguments"]:
                                acc["function"]["arguments"] += func["arguments"]
                # Emit the original chunk verbatim (it has tool_calls
                # or other fields that need to pass through).
                yield f"data: {json.dumps(chunk)}\n\n"

            # ---- Emit status messages for completed tool calls -----------
            # When the model sets finish_reason (any non-null value), all
            # accumulated tool calls are complete.  NOTE: the verbatim
            # finish_reason chunk is already yielded above (in the elif
            # branch), so the status messages below arrive AFTER it in
            # the native structured-tool_calls path.  Also check for
            # tool loops here (consecutive identical tool calls): if
            # detected, emit a synthetic content warning INSTEAD of the
            # tool_call chunk, so nanobot-ai does not re-execute the
            # same tool.  The model sees the warning in its next turn
            # and is expected to use the previous result or take a
            # fundamentally different approach.
            finish_reason = (
                choices[0].get("finish_reason")
                if choices else None
            )
            if finish_reason and pending_tool_calls:
                looped_indices: set[int] = set()
                for idx, tc in pending_tool_calls.items():
                    sig = _tool_call_signature(tc)
                    if _check_tool_loop(loop_key, sig, app):
                        looped_indices.add(idx)
                # Emit status for non-looped tool calls (the loop warning
                # is emitted by the loop-detection branch below).
                # Text-converted calls got their status line at conversion
                # time, so skip them here to avoid duplicate lines.
                for idx, tc in pending_tool_calls.items():
                    if idx in looped_indices:
                        continue
                    if idx in tts_tool_call_indices:
                        continue
                    status_msg = _format_status(tc)
                    if status_msg:
                        yield _make_status_chunk(
                            status_msg + "\n",
                            kind="tool_status",
                        )
                # Emit the actual tool_calls chunks for non-looped calls.
                # Looped calls are intentionally NOT emitted as
                # tool_calls so nanobot-ai does not re-execute the same
                # tool; instead the warning content chunk below is the
                # sole signal to the client.  Calls already relayed
                # verbatim (native structured tool_calls) are also not
                # re-emitted here: doing so would duplicate the arguments
                # at the client.  Text-converted calls are already fully
                # delivered by the state machine in one chunk, so they are
                # not re-emitted either.
                for idx, tc in pending_tool_calls.items():
                    if idx in looped_indices:
                        continue
                    if idx in streamed_tool_call_indices:
                        continue
                    if idx in tts_tool_call_indices:
                        continue
                    tc_chunk = {
                        "id": chunk.get("id"),
                        "object": "chat.completion.chunk",
                        "created": chunk.get("created"),
                        "model": chunk.get("model"),
                        "system_fingerprint": chunk.get("system_fingerprint"),
                        "choices": [{
                            "index": choices[0].get("index", 0),
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [tc],
                            },
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(tc_chunk)}\n\n"
                # Emit a synthetic content warning for looped calls.
                # nanobot-ai sees this as the model's response, not as
                # a tool_call to execute.  The model sees this in its
                # next turn and (hopefully) stops calling the same tool.
                for idx, tc in pending_tool_calls.items():
                    if idx not in looped_indices:
                        continue
                    name = tc.get("function", {}).get("name", "?")
                    args_str = tc.get("function", {}).get("arguments", "{}")
                    try:
                        args = json.loads(args_str)
                        args_repr = json.dumps(args, ensure_ascii=False)
                    except (json.JSONDecodeError, ValueError):
                        args_repr = args_str
                    warning = (
                        f"⚠️ Tool loop detected: `{name}` was called "
                        f"{_LOOP_DETECTION_WINDOW}+ times with the same "
                        f"arguments. The proxy has stopped re-executing "
                        f"this call. The previous result is still in the "
                        f"conversation history above. Use that result or "
                        f"take a fundamentally different approach. "
                        f"(Arguments: {args_repr})"
                    )
                    # Emit the loop warning as plain content (NO sentinel)
                    # so it reaches the model's next turn as a corrective
                    # signal — and the user sees why the tool did not run.
                    # The structured event below carries the machine-
                    # readable facts for observability.
                    yield f"data: {json.dumps(_make_system_chunk(warning + '\n'))}\n\n"
                    # Also emit a custom event for observability (the
                    # user's tools, log shippers, etc. can pick this up
                    # without parsing content).
                    yield _make_proxy_event("kinver.proxy.tool_loop_detected", {
                        "job_id": job_id,
                        "tool": name,
                        "arguments": args_str,
                        "ts": int(time.time()),
                    })
                pending_tool_calls = {}

        # ---- Stream completed successfully ----------------------------------
        outcome = "completed"
        if db:
            await db.complete_job(
                job_id,
                finish_reason="stop",
                full_content="".join(full_content),
            )
            # Populate the semantic cache for factual queries so a repeated
            # question is answered from cache without waking the GPU — the
            # frontdesk's ``is_factual`` signal finally pays for itself.
            if cache_query and route.is_factual and full_content:
                try:
                    await db.cache_store(cache_query, "".join(full_content))
                except (sqlite3.Error, OSError, ValueError):
                    logger.debug("Semantic cache store failed (non-critical)")
        yield "data: [DONE]\n\n"

    # AGENTS.md rule 10 permits `except Exception` at the terminal SSE
    # stream boundary: every stream error must be converted to a
    # client-visible SSE error chunk below. Narrowing this would silently
    # truncate client streams on unexpected bugs.
    except asyncio.CancelledError:
        # Client aborted the request (Stop button / task cancellation).
        # Clean up the job so it is not left 'active' and re-processed on
        # restart, then re-raise so the cancellation propagates and the
        # stream actually stops.  The finally block resets cooling.
        # NOTE: awaiting inside a CancelledError handler is interrupted by
        # a re-delivered CancelledError, so the DB write must be shielded
        # and its re-raised cancellation suppressed — otherwise the job is
        # left in whatever state the queue worker's skip-mark set.
        outcome = "cancelled"
        logger.info("Job %s cancelled by client", job_id)
        if db:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(
                    db.cancel_job(job_id, reason="client_cancelled")
                )
        raise
    except Exception as exc:
        outcome = "failed"
        logger.exception("Stream error for job %s: %s", job_id, exc)
        if db:
            await db.fail_job(job_id)
        # Emit error as an SSE chunk so the client knows something went wrong
        error_chunk = json.dumps({
            "error": {
                "message": f"Proxy stream error: {str(exc)}",
                "type": "proxy_error",
            },
        })
        yield f"data: {error_chunk}\n\n"
        yield "data: [DONE]\n\n"

    finally:
        # ---- Client-disconnect fallback -------------------------------------
        # Some frameworks deliver a disconnect as GeneratorExit (the
        # generator is closed) instead of task cancellation.  Neither the
        # CancelledError nor the generic handler catches GeneratorExit, so
        # detect it here: if the stream never reached a terminal outcome,
        # the client went away mid-flight — mark the job cancelled.
        if outcome == "unknown" and db:
            logger.info("Job %s aborted (stream closed early)", job_id)
            await db.cancel_job(job_id, reason="client_cancelled")

        # ---- Baseline cooling -----------------------------------------------
        if cooler:
            cooler.baseline_idle()

        # ---- Update state ---------------------------------------------------
        state.active_priority = 3  # IDLE
        if db:
            await db.purge_stream_chunks(job_id)


# ---------------------------------------------------------------------------
# Model startup wrapper — starts heavy GPU models before streaming
# ---------------------------------------------------------------------------

async def _event_stream_with_model_startup(
    state: "AppState",
    app: FastAPI,
    route: RouteDecision,
    payload: dict[str, Any],
    fwd_headers: dict[str, str],
    job_id: str,
    project_id: str,
    processed_messages: list[dict[str, Any]],
    requested_model: str,
    hardware_path: str,
    proxy_preamble: Optional[list[str]] = None,
    client_named_model: bool = False,
    session_id: str = "",
    cache_query: str = "",
) -> AsyncIterator[str]:
    """
    Wrapper around ``_event_stream`` that ensures heavy GPU models are
    started and ready before attempting to stream.

    For lightweight / CPU-resident models (chatter, frontdesk)
    this is a passthrough — the model should already be running.  For
    heavy GPU models (professional, coder, creative, scholar,
    architect), this performs a hot-swap if the model is not already
    the active heavy model, and streams a "loading" feedback message
    to keep the frontend connection alive during cold start.

    The backup version handled this inside queue_worker → manage_heavy_model
    which ran systemd start + wait_for_port_readiness BEFORE streaming.
    This wrapper brings the same behaviour to the direct-streaming path.
    """
    systemd = state.systemd
    model_key = route.model_key

    # Only intervene for heavy GPU models that may need cold-starting.
    # CPU-resident and lightweight GPU models should already be running.
    if model_key in _HEAVY_MODEL_KEYS and not route.is_cpu_fallback:
        active_heavy = systemd.active_heavy_model
        if active_heavy != model_key:
            # ---- Model not running — start it with loading feedback ----------
            label = MODEL_LABELS.get(model_key, model_key)

            # Send loading feedback so the frontend doesn't timeout.
            # Emitted as sentinel-prefixed delta.content (visible inline,
            # stripped from the model-copy by strip_proxy_status).
            loading_msg = (
                f"🔃 [Proxy: Loading {label}, please wait..."
                f"(cold start may take 30-120 seconds)]"
            )
            yield _make_status_chunk(loading_msg, kind="loading")

            try:
                # Before loading a heavy GPU model, stop any lightweight
                # GPU models (chatter) that may occupy VRAM.
                # The systemd.hot_swap() only stops the previous *heavy*
                # model — chatter runs alongside and must be
                # explicitly stopped to free VRAM for the heavy model.
                gpu_lightweights = ["chatter"]
                for lw in gpu_lightweights:
                    if await systemd.is_active(lw):
                        logger.info(
                            "Stopping lightweight GPU model '%s' "
                            "to free VRAM for heavy model '%s'", lw, model_key,
                        )
                        await systemd.stop_service(lw)
                        # Wait for VRAM to actually be released
                        await asyncio.sleep(3)

                # Now hot-swap to the target heavy model with VRAM freed.
                # systemd.hot_swap() calls systemctl stop on the previous
                # heavy model (if any), waits VRAM_RELEASE_DELAY, starts
                # the new model, and waits for port readiness.
                logger.info(
                    "Starting heavy model '%s' before streaming (was: %s)",
                    model_key, active_heavy,
                )
                await systemd.hot_swap(
                    from_domain=active_heavy or "",
                    to_domain=model_key,
                )
                # Port may have changed — update the route
                new_port = await systemd.get_port(model_key)
                route.port = new_port
                logger.info(
                    "Model '%s' ready on port %d — proceeding with stream",
                    model_key, new_port,
                )
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                logger.exception("Failed to start heavy model '%s': %s", model_key, exc)
                # Emit the error and bail — don't try to stream to a dead port
                error_chunk = json.dumps({
                    "error": {
                        "message": (
                            f"Proxy error: Failed to load {label}. "
                            f"Please try again later or use a lighter model."
                        ),
                        "type": "proxy_model_load_error",
                    },
                })
                yield f"data: {error_chunk}\n\n"
                yield "data: [DONE]\n\n"
                return

            # ---- Restore project-specific KV cache if available --------------
            # This dramatically speeds up warm starts for project sessions
            # by loading a pre-saved KV cache from NVMe.  Mirrors the
            # manage_slot_cache behaviour from the backup's queue_worker.
            if project_id and project_id != "general":
                from llm import manage_slot_cache
                cache_filename = f"{project_id}_{model_key}.bin"
                try:
                    await manage_slot_cache(route.port, "restore", cache_filename)
                    cache_msg = (
                        f"💾 [Proxy: Restored project cache "
                        f"'{project_id}' for {label}]"
                    )
                    yield _make_status_chunk(cache_msg, kind="cache")
                except httpx.HTTPError:
                    logger.debug(
                        "Cache restore skipped for %s (non-critical)",
                        cache_filename,
                    )

    # ---- Delegate to the core streaming generator ---------------------------
    async for chunk in _event_stream(
        state=state,
        app=app,
        route=route,
        payload=payload,
        fwd_headers=fwd_headers,
        job_id=job_id,
        project_id=project_id,
        processed_messages=processed_messages,
        requested_model=requested_model,
        hardware_path=hardware_path,
        proxy_preamble=proxy_preamble,
        client_named_model=client_named_model,
        session_id=session_id,
        cache_query=cache_query,
    ):
        yield chunk

    # ---- Save project-specific KV cache if heavy model ----------------------
    if model_key in _HEAVY_MODEL_KEYS and project_id and project_id != "general":
        from llm import manage_slot_cache
        cache_filename = f"{project_id}_{model_key}.bin"
        try:
            await manage_slot_cache(route.port, "save", cache_filename)
            logger.debug("Saved KV cache for project '%s' model '%s'", project_id, model_key)
        except httpx.HTTPError:
            logger.debug("Cache save failed for %s (non-critical)", cache_filename)


async def _collect_chat_completion(
    stream: AsyncIterator[str],
) -> dict[str, Any]:
    """
    Collect the proxy's SSE event stream into a JSON ChatCompletion.

    The proxy always streams internally (SSE), but a client that
    requested ``stream: false`` expects one JSON body with OpenAI
    non-streaming semantics.  Without this wrapper, the OpenAI SDK
    receives the raw SSE text as a plain string, parses zero
    tool_calls, and the agent loop executes nothing (the nanobot
    dream/heartbeat failure).

    Model content deltas are concatenated into ``message.content``,
    streamed tool_calls are accumulated per index into
    ``message.tool_calls``, and proxy-injected status chunks
    (triage/loading/system) plus ``kinver.proxy.*`` preamble events are
    filtered out — they are streaming UX, not model output.  An error
    chunk in the stream is surfaced as an OpenAI-shaped error body.
    """
    content_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    model_name = "proxy"
    error: Optional[dict[str, Any]] = None

    async for raw in stream:
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                obj = json.loads(line[len("data: "):])
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            if "error" in obj:
                error = obj["error"]
                continue
            # Accept any chunk carrying a ``choices`` list.  Native model
            # chunks are relayed verbatim and may omit the ``object``
            # field, so keying on ``object == "chat.completion.chunk"``
            # would silently drop their tool_calls.
            if not isinstance(obj.get("choices"), list):
                continue
            if obj.get("model") == "proxy-system":
                continue
            model_name = obj.get("model", model_name)
            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_calls.setdefault(idx, {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
            if "usage" in obj:
                usage = obj["usage"]

    if error is not None:
        return {"error": error}

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts),
    }
    if tool_calls:
        message["tool_calls"] = [
            tool_calls[idx] for idx in sorted(tool_calls)
        ]

    body: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason or "stop",
        }],
    }
    if usage is not None:
        body["usage"] = usage
    return body


# ---------------------------------------------------------------------------
# Embedded command handlers
# ---------------------------------------------------------------------------

async def _handle_pause_command(
    duration_mins: int,
    state: "AppState",
) -> StreamingResponse:
    """
    Handle the /pause [minutes] command embedded in a user prompt.

    Pauses the queue for the specified duration.  Returns a streaming
    response with status updates.
    """
    async def _stream() -> AsyncIterator[str]:
        pause_msg = (
            f"_⏸️ [Proxy: Attempting to pause queue for "
            f"{duration_mins} minutes...]_\n\n"
        )
        yield f"data: {json.dumps(_make_system_chunk(pause_msg))}\n\n"

        # Execute the pause logic via the state's transition management
        success, msg = await state.try_pause_queue(duration_mins * 60)
        if not success:
            yield f"data: {json.dumps(_make_system_chunk(f'⚠️ **Failed:** {msg}'))}\n\n"
        else:
            yield f"data: {json.dumps(_make_system_chunk('✅ **Success:** Queue paused.'))}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


async def _handle_resume_command(state: "AppState") -> StreamingResponse:
    """
    Handle the /resume command embedded in a user prompt.

    Manually unpauses the queue.
    """
    async def _stream() -> AsyncIterator[str]:
        state.try_resume_queue()
        msg = "_▶️ [Proxy: Queue resumed manually.]_\n\n"
        yield f"data: {json.dumps(_make_system_chunk(msg))}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


async def _handle_cloud_command(user_text: str) -> StreamingResponse:
    """
    Handle the /cloud command embedded in a user prompt.

    Routes the request directly to OpenRouter.
    """
    async def _stream() -> AsyncIterator[str]:
        status_msg = "_⏳ [Proxy: Routing concurrently to OpenRouter...]_\n\n"
        yield f"data: {json.dumps(_make_system_chunk(status_msg))}\n\n"

        cloud_resp = await openrouter_cloud_escalation(1, user_text)
        chunk = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "cloud",
            "choices": [{
                "index": 0,
                "delta": {"content": cloud_resp},
                "finish_reason": None,
            }],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# OpenCode bridge handlers
# ---------------------------------------------------------------------------

async def _opencode_task_response(task_text: str, client_stream: bool) -> Response:
    """Run a task through the opencode bridge and return the response.

    Streams an SSE response (or a JSON ChatCompletion for non-streaming
    clients, mirroring the proxy's stream-omitted default).  Shared by the
    ``model: "opencode"`` route, the ``/opencode`` command, and the
    coding-decision gate.
    """
    if not task_text:
        return JSONResponse(
            {"error": "opencode model requires a user message"},
            status_code=400,
        )

    async def _stream() -> AsyncIterator[str]:
        status_msg = (
            f"_⏳ [Proxy: Directing to OpenCode ({OPENCODE_AGENT} agent)...]_\n\n"
        )
        yield f"data: {json.dumps(_make_system_chunk(status_msg))}\n\n"
        resp_text = await opencode_chat(task_text, agent=OPENCODE_AGENT)
        chunk = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "opencode",
            "choices": [{
                "index": 0,
                "delta": {"content": resp_text},
                "finish_reason": None,
            }],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    if client_stream:
        return StreamingResponse(_stream(), media_type="text/event-stream")

    resp_text = await opencode_chat(task_text, agent=OPENCODE_AGENT)
    return JSONResponse({
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "opencode",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": resp_text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


async def _handle_opencode_request(
    messages: list[dict[str, Any]],
    client_stream: bool,
) -> Response:
    """Handle ``model: "opencode"`` — direct the task to the opencode agent.

    Extracts the latest user message as the task text and runs it through
    the headless opencode serve backend (``opencode_bridge.opencode_chat``).
    Returns a streamed SSE response (or a JSON ChatCompletion for
    non-streaming clients, mirroring the proxy's stream-omitted default).
    """
    return await _opencode_task_response(_last_user_text(messages), client_stream)


# ---------------------------------------------------------------------------
# Coding-task decision gate
# ---------------------------------------------------------------------------

async def _coding_decision_response(question: str, client_stream: bool) -> Response:
    """Return the coding-decision question as a chat completion.

    The question is sentinel-prefixed so it renders inline in the frontend
    AND is stripped from the OUTBOUND model-copy on the next turn.  The
    stream chunk is built as a raw dict (NOT via ``_make_status_chunk``,
    which returns a full SSE line and would double-encode here).
    """
    if client_stream:
        async def _stream() -> AsyncIterator[str]:
            chunk = {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "proxy-system",
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": question,  # already sentinel-prefixed
                    },
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")

    return JSONResponse({
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "proxy-system",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": question},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


async def _apply_coding_decision_gate(
    app: FastAPI,
    messages: list[dict[str, Any]],
    session_id: str,
    client_stream: bool,
    route: RouteDecision,
    has_tool_calls: bool,
) -> Optional[Response]:
    """Prompt the user for the coding route, or apply a cached decision.

    Coding requests from non-opencode clients get a one-turn choice:
    route to OpenCode (the bridge) or the local code pathway (Professional).
    Requests from opencode itself (Lane B / IDE) skip the gate entirely and
    go straight to the local model — the loop-prevention requirement.

    Returns a ``Response`` when the gate must take over (question emission,
    opencode routing, or the professional decision turn), or ``None`` to
    continue the normal flow.
    """
    if route.is_lane_b:
        return None  # opencode caller → local model directly, never prompt

    decisions = _coding_decision_state(app)

    # ---- Decision turn: the user answered the coding question ----------
    question_idx = _find_coding_question_index(messages)
    if question_idx is not None:
        answer_text = _last_user_text(messages)
        decision = _parse_coding_answer(answer_text)
        if decision == "cancel":
            # The user aborted the coding task without choosing a route.
            # Do NOT cache a decision — the next coding task prompts again.
            logger.info("Coding decision for session %s: cancelled", session_id)
            return await _plain_completion(
                "⚡ Task cancelled.", client_stream,
            )
        decisions[session_id] = decision
        logger.info("Coding decision for session %s: %s", session_id, decision)
        task_messages = messages[:question_idx]  # task = convo up to the question
        if decision == "opencode":
            return await _opencode_task_response(
                _last_user_text(task_messages), client_stream,
            )
        # professional: drop the question + answer; the flow continues to
        # the local code pathway with the original task as the last turn.
        messages[:] = task_messages
        route.intent = "CODE"
        return None

    # ---- Fresh coding request: prompt once, then cache the choice ------
    if route.intent == "CODE" and not has_tool_calls:
        decision = decisions.get(session_id)
        if decision == "opencode":
            return await _opencode_task_response(_last_user_text(messages), client_stream)
        if decision is None:
            question = (
                f"{_CODING_QUESTION_PREFIX} Coding task detected — route to OpenCode "
                f"or the local code pathway (Professional)? Reply `opencode` or `local`."
            )
            logger.info("Prompting coding decision for session %s", session_id)
            return await _coding_decision_response(question, client_stream)
    return None


async def _handle_opencode_command(user_text: str) -> StreamingResponse:
    """Handle the /opencode command embedded in a user prompt.

    Directs the remaining text to the opencode agent (gentle-orchestrator), mirroring
    the /cloud command flow.
    """
    task_text = _OPENCODE_RE.sub("", user_text, count=1).strip()
    if not task_text:
        task_text = user_text.strip()

    async def _stream() -> AsyncIterator[str]:
        status_msg = (
            f"_⏳ [Proxy: Routing concurrently to OpenCode ({OPENCODE_AGENT} agent)...]_\n\n"
        )
        yield f"data: {json.dumps(_make_system_chunk(status_msg))}\n\n"

        resp_text = await opencode_chat(task_text, agent=OPENCODE_AGENT)
        chunk = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "opencode",
            "choices": [{
                "index": 0,
                "delta": {"content": resp_text},
                "finish_reason": None,
            }],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Semantic cache response streamer
# ---------------------------------------------------------------------------

async def _stream_cached_response(
    cached_text: str,
    model_name: str,
    client_stream: bool = True,
) -> Response:
    """Serve a cached response for a factual query cache hit.

    Streams the entire cached text as a single content delta followed by
    [DONE] for streaming clients; returns a JSON ChatCompletion for
    non-streaming clients (mirroring the proxy's stream-omitted default).
    """
    async def _stream() -> AsyncIterator[str]:
        chunk = {
            "id": f"chatcmpl-cached-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_name if model_name != "auto" else "cache",
            "choices": [{
                "index": 0,
                "delta": {"content": cached_text},
                "finish_reason": "stop",
            }],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    if client_stream:
        return StreamingResponse(_stream(), media_type="text/event-stream")

    return JSONResponse({
        "id": f"chatcmpl-cached-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name if model_name != "auto" else "cache",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": cached_text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


# ---------------------------------------------------------------------------
# Helper: make a system-styled chunk for proxy messages
# ---------------------------------------------------------------------------

def _make_system_chunk(content: str) -> dict[str, Any]:
    """Create an SSE chunk dict styled as a system message for proxy
    status updates used by the command endpoints (pause, resume,
    cloud status) and the model-directed tool-loop warning.  Streaming
    conversation status (triage, loading, tool status, cache) uses
    _make_status_chunk instead — sentinel-prefixed content that is
    visible inline but stripped from the OUTBOUND model-copy.
    """
    return {
        "id": f"sys-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "proxy-system",
        "choices": [{
            "index": 0,
            "delta": {"content": content},
            "finish_reason": None,
        }],
    }


def _make_proxy_event(event: str, payload: dict[str, Any]) -> str:
    """
    Build a top-level SSE event line for proxy-injected signals.

    Returns the full ``event:`` + ``data:`` frame so consumers can
    distinguish params_replaced/status/tool_stripped events from
    ordinary model chunks.
    """
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _make_status_chunk(content: str, kind: str = "status") -> str:
    """Render proxy-injected UX (triage, loading, tool status, cache) as
    visible ``delta.content`` with the ``STATUS_SENTINEL`` prefix.

    Status is emitted as regular content so frontends render it inline
    during loading and tool gaps (the original UX requirement), but it
    carries two markers that keep it out of the model's input:

    - the sentinel prefix lets ``strip_proxy_status`` drop the chunk from
      the OUTBOUND model-copy on the next request, so the model never
      sees its own status echoed back (the triage-echo degeneration that
      produced the "routing loop with no output" symptom);
    - ``model: "proxy-system"`` keeps the stream=false collector from
      gluing status into the JSON ChatCompletion (the dream path).

    Loop warnings are NOT sent through this helper — they are
    model-directed corrective signals and must reach the next turn, so
    they are emitted as plain content via ``_make_system_chunk``.
    """
    return f"data: {json.dumps({
        'id': f'chatcmpl-status-{int(time.time())}',
        'object': 'chat.completion.chunk',
        'created': int(time.time()),
        'model': 'proxy-system',
        'choices': [{
            'index': 0,
            'delta': {
                'role': 'assistant',
                'content': f'{STATUS_SENTINEL}{content}',
            },
            'finish_reason': None,
        }],
    })}\n\n"


# ---------------------------------------------------------------------------
# Triage message builder — transparent first SSE chunk
# ---------------------------------------------------------------------------

def _build_triage_message(
    route: RouteDecision,
    client_named_model: bool = False,
) -> str:
    """
    Build a human-readable triage message that informs the user which
    model was selected and why, without altering the conversation.

    Example output (auto-routed, classifier picked the model):
        🔍 Proxy triage: classified as CODE (priority 1).
        Routing to professional on port 13103.

    Example output (R19 client override — client specified the model):
        🔀 Client specified Professional (35B MoE). Routing on port 13103.
        (frontdesk suggested CHAT, client model wins.)

    Parameters
    ----------
    route : RouteDecision
        The resolved routing decision.
    client_named_model : bool
        True when the R19 client-named-model override actually changed
        the route (classifier and client disagreed).  When True the
        message is rewritten to reflect that the client picked the model
        rather than the classifier, since reporting "classified as CHAT"
        while routing to Professional is actively misleading.

    Returns
    -------
    str
        A single-line informational message, no trailing newline.
    """
    # Determine the human-readable model description
    model_label = MODEL_LABELS.get(
        route.model_key,
        route.model_key.capitalize(),
    )

    # R19 client override path: rewrite the message so the "classified
    # as X" line doesn't lie about the destination.  The classifier's
    # intent is preserved as context (in parens) so the user can see
    # what the frontdesk would have picked.
    if client_named_model:
        parts = [
            f"🔀 Client specified {model_label}.",
            f"Routing on port {route.port}.",
        ]
        if route.intent:
            parts.append(f"(frontdesk suggested {route.intent}, client model wins.)")
        return " ".join(parts)

    # Standard auto-routed path
    parts = [
        f"🔍 Proxy triage: classified as {route.intent}",
        f"(priority {route.priority}).",
        f"Routing to {model_label}",
    ]

    if route.is_cpu_fallback:
        parts.append("(⚠️ CPU fallback — GPU occupied)")

    # Port info for transparency
    parts.append(f"on port {route.port}.")

    return " ".join(parts)
