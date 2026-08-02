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
import hashlib
import json
import re
import subprocess
import time
import uuid
import httpx
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

from constants import (
    STOP_SEQS,
    OPENAI_FORWARD_FIELDS,
    R11_AUTHORITY_FIELDS,
    ALL_MODEL_KEYS,
    CPU_MODELS,
    TOOL_KEYWORDS,
    _HEAVY_MODEL_KEYS,
    get_logger,
)
from llm import (
    stream_llm,
    openrouter_cloud_escalation,
)
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
from text_to_structured import ToolCallTextToStructured

if TYPE_CHECKING:
    from proxy import AppState

logger = get_logger("proxy.routes")

# ---------------------------------------------------------------------------
# /v1/system commands (embedded in user prompts)
# ---------------------------------------------------------------------------
_PAUSE_RE = re.compile(r"(?i)\s*/pause(?:\s+(\d+))?\s*$")
_RESUME_RE = re.compile(r"(?i)\s*/resume\s*$")
_CLOUD_RE = re.compile(r"/cloud")

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


def _loop_state(app: FastAPI) -> dict[str, list[str]]:
    """Lazy accessor for the cross-request tool-loop state.

    The state lives on ``app.state`` (Rule 6 — no module-level mutable
    globals).  ``getattr``/``setattr`` so lifespan-less test apps and the
    real app both work.
    """
    state = app.state
    if not hasattr(state, "loop_detection_state"):
        state.loop_detection_state = {}
    return state.loop_detection_state


def _session_state(app: FastAPI) -> dict[str, dict]:
    """Lazy accessor for the cross-request session map on ``app.state``."""
    state = app.state
    if not hasattr(state, "session_state"):
        state.session_state = {}
    return state.session_state


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
    history = loop_state.setdefault(session_id, [])
    if history and history[-1] != signature:
        # Different tool call — reset the consecutive-run window.
        history.clear()
    history.append(signature)
    # Cap per-session history to the window size.
    if len(history) > _LOOP_DETECTION_WINDOW:
        del history[0 : len(history) - _LOOP_DETECTION_WINDOW]
    # LRU-ish cap on total sessions tracked.
    if len(loop_state) > _LOOP_DETECTION_MAX_JOBS:
        # Drop the oldest session_id to bound memory.
        oldest_key = min(
            loop_state,
            key=lambda k: _session_state(app).get(k, {}).get("last_seen", 0),
        )
        loop_state.pop(oldest_key, None)
    return len(history) >= _LOOP_DETECTION_WINDOW and all(
        s == signature for s in history
    )


def _clear_tool_loop(session_id: str, app: FastAPI) -> None:
    """Drop a session's loop-detection state (called on stream end)."""
    _loop_state(app).pop(session_id, None)


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
    return JSONResponse({"object": "list", "data": models})


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------

async def chat_completions(request: Request) -> StreamingResponse:
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

    # ---- Lane B (IDE passthrough): strip tools, bypass frontdesk -----------
    if caller_type == "IDE" or lane_b:
        tools = None  # IDE gets no tool injection
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
            "messages": processed_messages,
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
            replaced_fields = list(entry.values.keys())
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
        return StreamingResponse(
            _event_stream_with_model_startup(
                state=state, app=request.app, route=route, payload=payload, fwd_headers={},
                job_id=job_id, project_id="soul",
                processed_messages=processed_messages,
                requested_model="professional",
                hardware_path=hardware_path,
                proxy_preamble=proxy_preamble,
                session_id=session_id,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ---- Lane A: classify intent via 2B Front Desk --------------------------
    # Check if the conversation already has tool calls in progress (from
    # a previous Worker interaction in this session).  If so, skip frontdesk
    # classification entirely and keep Worker — reclassifying mid-tool-flow
    # causes wrongful model switches (CODE → Professional, TOOL → Worker)
    # that break the tool execution chain.
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
        # Conversation already has tool calls — stay with Worker, don't
        # let frontdesk reclassify and accidentally switch models
        classification["intent"] = "TOOL"
        classification["tools_required"] = True
        logger.info(
            "Mid-tool-flow detected (%d messages with tool_calls) — "
            "skipping frontdesk, staying on Worker",
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
    # intent to TOOL.  Over-detection is safe because Worker handles chat too.
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

    # ---- Semantic cache check for factual queries ---------------------------
    if classification.get("is_factual"):
        cached = await check_semantic_cache(user_text, True, db)
        if cached:
            logger.info("Returning cached response for factual query")
            return await _stream_cached_response(cached, requested_model)

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
        "messages": processed_messages,
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
    #   - no header, tools in request        → enable_thinking: False
    #   - no header, no tools, complex intent
    #     (CODE/SCHOLAR/CREATIVE/ARCHITECT)
    #     AND classifier says tools NOT required → enable_thinking: True
    #   - no header, no tools, simple intent  → enable_thinking: False
    #   - no header, no tools, complex intent
    #     BUT classifier says tools required   → enable_thinking: False
    thinking_header = headers.get("x-proxy-thinking", "").lower()
    if thinking_header == "true":
        payload["chat_template_kwargs"] = {"enable_thinking": True, "preserve_thinking": True}
    elif thinking_header == "false":
        payload["chat_template_kwargs"] = {"enable_thinking": False, "preserve_thinking": False}
    elif not tools:
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
    else:
        # Tools in request, no header: explicit opt-out.  Defense in
        # depth — even if the service's ``--chat-template-kwargs`` is
        # ignored after the first turn (which it is, per Qwen 3.5's
        # chat template behavior), the proxy's per-request setting is
        # always honored.
        #
        # ``preserve_thinking: false`` is critical here: the fixed
        # chat template (froggeric v21) emits an empty ``<think>\n\n
        # ``</think>\n\n`` placeholder when ``enable_thinking: false``
        # is set.  The model fills that placeholder on turn 4+ of
        # tool-calling flows, re-introducing the bug.  Setting
        # ``preserve_thinking: false`` strips past ``<think>`` blocks
        # from the history so the model doesn't see its own previous
        # thinking pattern and decide to continue it.
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
    # write only to CPU, and GPU-only models (worker, chatter) write
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

    return StreamingResponse(
        _event_stream_with_model_startup(
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
        ),
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

    # ---- Yield proxy-injected preamble events -----------------------------
    # These events (e.g. params_replaced) are emitted before the triage
    # status chunk so consumers see substitution signals first.
    if proxy_preamble:
        for event_line in proxy_preamble:
            yield event_line

    # ---- Yield triage metadata as first SSE chunk ---------------------------
    # Let the frontend know which model was selected and why, so users
    # understand the routing decision.  This is informational only and
    # does not affect the conversation content (Glass Pipe Rule).
    # Emitted as delta.content via _make_system_chunk so nanobot-ai and
    # similar clients display it in the chat — this gives the user
    # feedback during the long model-loading delay.  The original
    # concern that this would corrupt tool-calling was a red herring;
    # the actual cause of the tool-call XML-in-chat bug was Qwen 3.5's
    # thinking mode emitting reasoning_content adjacent to the tool
    # call, which the proxy now disables via chat_template_kwargs.
    triage_msg = _build_triage_message(route, client_named_model=client_named_model)
    yield f"data: {json.dumps(_make_system_chunk(triage_msg))}\n\n"

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
            else:
                # delta_content was non-empty but produced no tts emits
                # (e.g., content was held back because a tool call started
                # but hasn't closed yet).  Don't emit anything for this
                # chunk — the next chunk will produce the emit when the
                # tool call completes.
                pass

            # ---- Emit status messages for completed tool calls -----------
            # When the model sets finish_reason (any non-null value), all
            # accumulated tool calls are complete.  Emit the status
            # messages BEFORE the finish_reason chunk so the user sees
            # the tool call description before the stream end.  Also
            # check for tool loops here (consecutive identical tool
            # calls): if detected, emit a synthetic content warning
            # INSTEAD of the tool_call chunk, so nanobot-ai does not
            # re-execute the same tool.  The model sees the warning in
            # its next turn and is expected to use the previous result
            # or take a fundamentally different approach.
            finish_reason = (
                choices[0].get("finish_reason")
                if choices else None
            )
            if finish_reason and pending_tool_calls:
                from text_to_structured import _format_status
                looped_indices: set[int] = set()
                for idx, tc in pending_tool_calls.items():
                    sig = _tool_call_signature(tc)
                    if _check_tool_loop(loop_key, sig, app):
                        looped_indices.add(idx)
                # Emit status for non-looped tool calls (the loop warning
                # is emitted by the loop-detection branch below).
                for idx, tc in pending_tool_calls.items():
                    if idx in looped_indices:
                        continue
                    status_msg = _format_status(tc)
                    if status_msg:
                        status_chunk = {
                            "id": chunk.get("id"),
                            "object": "chat.completion.chunk",
                            "created": chunk.get("created"),
                            "model": chunk.get("model"),
                            "system_fingerprint": chunk.get("system_fingerprint"),
                            "choices": [{
                                "index": choices[0].get("index", 0),
                                "delta": {"content": status_msg + "\n"},
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(status_chunk)}\n\n"
                # Emit the actual tool_calls chunks for non-looped calls.
                # Looped calls are intentionally NOT emitted as
                # tool_calls so nanobot-ai does not re-execute the same
                # tool; instead the warning content chunk below is the
                # sole signal to the client.
                for idx, tc in pending_tool_calls.items():
                    if idx in looped_indices:
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
                    warn_chunk = {
                        "id": chunk.get("id"),
                        "object": "chat.completion.chunk",
                        "created": chunk.get("created"),
                        "model": chunk.get("model"),
                        "system_fingerprint": chunk.get("system_fingerprint"),
                        "choices": [{
                            "index": choices[0].get("index", 0),
                            "delta": {"content": warning + "\n"},
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(warn_chunk)}\n\n"
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
        if db:
            await db.complete_job(
                job_id,
                finish_reason="stop",
                full_content="".join(full_content),
            )
        yield "data: [DONE]\n\n"

    # AGENTS.md rule 10 permits `except Exception` at the terminal SSE
    # stream boundary: every stream error must be converted to a
    # client-visible SSE error chunk below. Narrowing this would silently
    # truncate client streams on unexpected bugs.
    except Exception as exc:
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
) -> AsyncIterator[str]:
    """
    Wrapper around ``_event_stream`` that ensures heavy GPU models are
    started and ready before attempting to stream.

    For lightweight / CPU-resident models (chatter, worker, frontdesk)
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
            model_labels = {
                "professional": "Professional (35B MoE)",
                "coder": "Coder (27B Dense)",
                "creative": "Creative (long-form)",
                "scholar": "Scholar (deep research)",
                "architect": "Architect (multi-stage planning)",
            }
            label = model_labels.get(model_key, model_key)

            # Send loading feedback so the frontend doesn't timeout.
            # Emitted as delta.content via _make_system_chunk so the user
            # sees a status message while waiting for the heavy model to
            # cold-start (30-120s).  Originally emitted as a custom SSE
            # event but the user wanted the status visible in the chat.
            loading_msg = (
                f"🔃 [Proxy: Loading {label}, please wait..."
                f"(cold start may take 30-120 seconds)]"
            )
            yield f"data: {json.dumps(_make_system_chunk(loading_msg))}\n\n"

            try:
                # Before loading a heavy GPU model, stop any lightweight
                # GPU models (worker, chatter) that may occupy VRAM.
                # The systemd.hot_swap() only stops the previous *heavy*
                # model — worker/chatter run alongside and must be
                # explicitly stopped to free VRAM for the heavy model.
                gpu_lightweights = ["worker", "chatter"]
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
                    yield f"data: {json.dumps(_make_system_chunk(cache_msg))}\n\n"
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
    async def _stream():
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
    async def _stream():
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
    async def _stream():
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
# Semantic cache response streamer
# ---------------------------------------------------------------------------

async def _stream_cached_response(
    cached_text: str,
    model_name: str,
) -> StreamingResponse:
    """
    Stream a cached response as SSE chunks for a factual query cache hit.

    Sends the entire cached text as a single content delta followed by
    [DONE] — the client sees it as an instant completion.
    """
    async def _stream():
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

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Helper: make a system-styled chunk for proxy messages
# ---------------------------------------------------------------------------

def _make_system_chunk(content: str) -> dict[str, Any]:
    """
    Create an SSE chunk dict styled as a system message for proxy
    status updates (pause, resume, triage, etc.).
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
    model_descriptions = {
        "frontdesk":     "Front Desk (2B classifier)",
        "chatter":       "Chatter (9B fast chat)",
        "worker":        "Worker (9B tool-capable)",
        "professional":  "Professional (35B MoE)",
        "coder":         "Coder (27B Dense)",
        "creative":      "Creative (long-form)",
        "scholar":       "Scholar (deep research)",
        "architect":     "Architect (multi-stage planning)",
    }
    model_label = model_descriptions.get(
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
