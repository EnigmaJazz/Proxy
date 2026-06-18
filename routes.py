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
import json
import logging
import re
import time
import uuid
from typing import Optional, Dict, Any, AsyncIterator, List

from fastapi import Request
from fastapi.responses import StreamingResponse, JSONResponse

from constants import (
    IDE_PASSTHROUGH_HEADER,
    STOP_SEQS,
    NATIVE_TOOLS,
    CoolingPreset,
    get_logger,
)
from llm import (
    stream_llm,
    call_llm,
    call_model_chat,
    translate_to_deepseek_r1,
    openrouter_cloud_escalation,
    _inject_provider_metadata,
)
from routing import (
    RouteDecision,
    discriminate_caller,
    is_lane_b,
    classify_with_frontdesk,
    resolve_model,
    resolve_route_for_lane_a,
    detect_tool_loops,
    check_semantic_cache,
    extract_project_context,
    is_dream_process,
    TOOL_KEYWORDS,
)
from auditing import ShadowAuditor

logger = get_logger("proxy.routes")

# ---------------------------------------------------------------------------
# /v1/system commands (embedded in user prompts)
# ---------------------------------------------------------------------------
_PAUSE_RE = re.compile(r"(?i)^\s*/pause(?:\s+(\d+))?\s*$")
_RESUME_RE = re.compile(r"(?i)^\s*/resume\s*$")
_CLOUD_RE = re.compile(r"/cloud")


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
    6. Resolve route (GPU-aware with Lifeboat fallback for CHAT/TOOL).
    7. Handle embedded commands (/pause, /resume, /cloud).
    8. Stream response via SSE, with cooling lifecycle and optional shadow auditing.
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

    messages: list = body.get("messages", [])
    if not messages:
        return JSONResponse({"error": "messages array required"}, status_code=400)

    requested_model = body.get("model", "auto").lower()
    tools = body.get("tools", None)
    temperature = body.get("temperature", 0.7)
    top_p = body.get("top_p", 1.0)
    max_tokens = body.get("max_tokens", 4096)

    # ---- Discriminate caller type ------------------------------------------
    headers = dict(request.headers)
    caller_type = discriminate_caller(headers)
    lane_b = is_lane_b(headers)

    # ---- Prepare messages (Glass Pipe Rule: NO text alteration) ------------
    processed_messages: list = list(messages)  # Shallow copy

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
        is_dream = is_dream_process(raw_text)

        effective_domain = model_domain if model_domain in (
            "coder", "architect", "reasoning", "professional", "creative", "scholar",
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
    pause_match = _PAUSE_RE.match(user_text.strip()) if user_text else None
    if pause_match:
        duration_mins = int(pause_match.group(1)) if pause_match.group(1) else 60
        return await _handle_pause_command(duration_mins, state)

    # /resume
    if user_text and _RESUME_RE.match(user_text.strip()):
        return await _handle_resume_command(state)

    # /cloud
    if user_text and _CLOUD_RE.search(user_text.lower()):
        return await _handle_cloud_command(user_text)

    # ---- Dream/soul fast-path: bypass frontdesk, route to architect --------
    if is_dream:
        logger.info("Dream/soul process detected — routing to architect (priority 3)")
        port = await systemd.get_port("architect")
        route = RouteDecision(
            model_key="architect",
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
        job_id = str(uuid.uuid4())
        if db:
            job_id = await db.enqueue_job(
                messages_json=json.dumps(processed_messages),
                priority=3,
                intent="ARCHITECT",
                project_id="soul",
                tools_json=None,
                parameters_json=json.dumps({"temperature": 0.2, "max_tokens": 4096, "thinking_budget_tokens": 4096}),
                lane="lane_a",
                is_lane_b=False,
                caller_type="AGENTIC",
            )
        payload = {
            "messages": processed_messages,
            "temperature": 0.2,
            "max_tokens": 4096,
            "stream": True,
            "stop": STOP_SEQS,
            "thinking_budget_tokens": 4096,
        }
        from cooling import CoolingStateMachine
        hardware_path = CoolingStateMachine.hardware_path_for_model("architect")
        return StreamingResponse(
            _event_stream_with_model_startup(
                state=state, route=route, payload=payload, fwd_headers={},
                job_id=job_id, project_id="soul",
                processed_messages=processed_messages,
                requested_model="architect",
                hardware_path=hardware_path,
                auditor_active=False,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ---- Lane A: classify intent via 2B Front Desk --------------------------
    # Check if the conversation already has tool calls in progress (from
    # a previous Worker interaction in this session).  If so, skip frontdesk
    # classification entirely and keep Worker — reclassifying mid-tool-flow
    # causes wrongful model switches (CODE → Professional, TOOL → Lifeboat)
    # that break the tool execution chain.
    has_tool_calls = any(
        m.get("role") == "assistant" and "tool_calls" in m
        for m in processed_messages
    ) or any(
        m.get("role") == "tool"
        for m in processed_messages
    )

    classification: dict = {
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
            classification.get("project"),
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
        # Lane A: GPU-aware routing with Lifeboat fallback.
        # Pass has_tool_history so Lifeboat is skipped when the conversation
        # already contains tool_calls (Lifeboat's template rejects them).
        route = await resolve_route_for_lane_a(
            classification, systemd,
            has_tool_history=has_tool_calls,
        )

    # ---- Extract project context for database ---------------------------------
    ctx = extract_project_context(user_text)
    project_name = ctx["project"] if ctx["project"] != "default" else classification.get("project", "general")

    # ---- Create project in database ------------------------------------------
    project_id = "general"
    if db:
        project_id = await db.get_or_create_project(project_name, ctx.get("root", ""))

    # ---- Update application state -------------------------------------------
    state.active_priority = route.priority
    # Only update active_heavy_model for GPU routes — never clear to None
    # when routing to CPU/lifeboat, because a heavy GPU model may still be
    # actively streaming for an ongoing generation.  Clearing it would
    # trigger cleanup logic that kills the in-progress stream.
    if route.hardware_path == "gpu":
        state.active_heavy_model = route.model_key
    state.requests_served += 1

    # ---- Build generation parameters ---------------------------------------
    parameters = {
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }

    # Set thinking_budget_tokens and other domain-specific params
    if route.intent in ("CODE", "ARCHITECT"):
        parameters.update({
            "temperature": 0.1,
            "top_p": 0.9,
            "max_tokens": -1,  # No hard cap for code
            "thinking_budget_tokens": 4096,
        })
    elif route.intent in ("CREATIVE", "SCHOLAR"):
        parameters.update({
            "temperature": 0.4,
            "top_p": 0.95,
            "max_tokens": 8192,
            "thinking_budget_tokens": 2048,
        })
    elif route.intent == "PROFESSIONAL":
        parameters.update({
            "temperature": 0.3,
            "top_p": 0.95,
            "max_tokens": 4096,
            "thinking_budget_tokens": 1024,
        })
    else:
        parameters.update({
            "max_tokens": 2048,
            "thinking_budget_tokens": 0,
        })

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

    # ---- Determine if shadow auditing is active ------------------------------
    auditor_active = ShadowAuditor.should_audit(route.intent, route.is_lane_b, tools)

    # ---- Translate system→user for reasoning models -------------------------
    # (Only if the target model is DeepSeek R1 — structural translation only)
    payload_messages = processed_messages
    if route.model_key == "reasoning":
        payload_messages = translate_to_deepseek_r1(processed_messages)

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
        "messages": payload_messages,
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

    # ---- Forward headers for Lane B ----------------------------------------
    fwd_headers: Dict[str, str] = {}
    if route.is_lane_b:
        fwd_headers["X-IDE-Mode"] = "true"

    # ---- Determine cooling hardware path -----------------------------------
    # Resolve from the cooling module's classification.  This ensures
    # hybrid models (architect, coder, creative, professional, scholar)
    # write to BOTH CPU and GPU IPC files, CPU-only models (lifeboat,
    # reasoning, frontdesk) write only to CPU, and GPU-only models
    # (worker, chatter) write only to GPU.
    from cooling import CoolingStateMachine
    hardware_path = CoolingStateMachine.hardware_path_for_model(route.model_key)

    # ---- Build & return the SSE stream --------------------------------------
    logger.info(
        "Routing to %s (lane=%s intent=%s priority=%d cpu_fallback=%s audit=%s)",
        route.model_key,
        "lane_b" if route.is_lane_b else "lane_a",
        route.intent,
        route.priority,
        route.is_cpu_fallback,
        auditor_active,
    )

    return StreamingResponse(
        _event_stream_with_model_startup(
            state=state,
            route=route,
            payload=payload,
            fwd_headers=fwd_headers,
            job_id=job_id,
            project_id=project_id,
            processed_messages=processed_messages,
            requested_model=requested_model,
            hardware_path=hardware_path,
            auditor_active=auditor_active,
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
    state,
    route: RouteDecision,
    payload: dict,
    fwd_headers: dict,
    job_id: str,
    project_id: str,
    processed_messages: list,
    requested_model: str,
    hardware_path: str,
    auditor_active: bool,
) -> AsyncIterator[str]:
    """
    Core SSE streaming generator.

    1. Prefill burst cooling.
    2. If auditing is active, start the ShadowAuditor.
    3. Stream chunks from the LLM via ``stream_llm``.
    4. On first chunk: step down cooling to GENERATION.
    5. Feed chunks to auditor if active.
    6. On [DONE]: complete job in DB, baseline cooling, stop auditor.
    7. On audit FATAL: Graceful Guillotine → clean stream termination.
    """
    db = state.database
    systemd = state.systemd
    cooler = state.cooler

    audit_fatal_triggered = False
    audit_fatal_reason = ""

    # ---- Setup fatal callback for auditor -----------------------------------
    async def _on_audit_fatal(jid: str, reason: str):
        nonlocal audit_fatal_triggered, audit_fatal_reason
        audit_fatal_triggered = True
        audit_fatal_reason = reason

    # ---- Prefill burst cooling ---------------------------------------------
    if cooler:
        await cooler.prefill_burst(hardware_path)

    # ---- Start shadow auditor if active ------------------------------------
    auditor = state.auditor if auditor_active else None
    if auditor and auditor_active:
        auditor.start(
            job_id=job_id,
            project_id=project_id,
            messages=processed_messages,
            on_fatal=_on_audit_fatal,
        )

    first_chunk_seen = False
    full_content: list[str] = []
    chunk_seq = 0
    accumulated = ""

    # ---- Yield triage metadata as first SSE chunk ---------------------------
    # Let the frontend know which model was selected and why, so users
    # understand the routing decision.  This is informational only and
    # does not affect the conversation content (Glass Pipe Rule).
    triage_msg = _build_triage_message(route)
    yield f"data: {json.dumps(_make_system_chunk(triage_msg))}\n\n"

    try:
        async for chunk in stream_llm(
            endpoint=route.model_key,
            payload=payload,
            port=route.port,
            headers=fwd_headers,
        ):
            # ---- Check for auditor fatal mid-stream --------------------------
            if audit_fatal_triggered:
                yield await _graceful_guillotine_chunk(job_id, audit_fatal_reason)
                return

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

            # ---- Feed to shadow auditor (non-blocking) ------------------------
            if auditor and auditor_active and delta_content:
                auditor.feed_chunk(delta_content)

            # ---- Persist chunk to database ------------------------------------
            if db and delta_content:
                # Update partial content for crash recovery
                await db.update_partial_content(job_id, accumulated)
                # Record individual chunk
                await db.record_stream_chunk(
                    job_id, chunk_seq, json.dumps(chunk),
                )

            # ---- Emit SSE line ------------------------------------------------
            yield f"data: {json.dumps(chunk)}\n\n"

        # ---- Stream completed successfully ----------------------------------
        if db:
            await db.complete_job(
                job_id,
                finish_reason="stop",
                full_content="".join(full_content),
            )
        yield "data: [DONE]\n\n"

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
        # ---- Stop auditor ----------------------------------------------------
        if auditor and auditor_active:
            auditor.stop()

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

# Heavy GPU models that may need cold-starting before streaming
_HEAVY_MODEL_KEYS = {"professional", "coder", "creative", "scholar", "architect"}

async def _event_stream_with_model_startup(
    state,
    route: RouteDecision,
    payload: dict,
    fwd_headers: dict,
    job_id: str,
    project_id: str,
    processed_messages: list,
    requested_model: str,
    hardware_path: str,
    auditor_active: bool,
) -> AsyncIterator[str]:
    """
    Wrapper around ``_event_stream`` that ensures heavy GPU models are
    started and ready before attempting to stream.

    For lightweight / CPU-resident models (chatter, worker, frontdesk,
    lifeboat, reasoning) this is a passthrough — the model should already
    be running.  For heavy GPU models (professional, coder, creative,
    scholar, architect), this performs a hot-swap if the model is not
    already the active heavy model, and streams a "loading" feedback
    message to keep the frontend connection alive during cold start.

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

            # Send loading feedback so the frontend doesn't timeout
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
            except Exception as exc:
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
                except Exception:
                    logger.debug(
                        "Cache restore skipped for %s (non-critical)",
                        cache_filename,
                    )

    # ---- Delegate to the core streaming generator ---------------------------
    async for chunk in _event_stream(
        state=state,
        route=route,
        payload=payload,
        fwd_headers=fwd_headers,
        job_id=job_id,
        project_id=project_id,
        processed_messages=processed_messages,
        requested_model=requested_model,
        hardware_path=hardware_path,
        auditor_active=auditor_active,
    ):
        yield chunk

    # ---- Save project-specific KV cache if heavy model ----------------------
    if model_key in _HEAVY_MODEL_KEYS and project_id and project_id != "general":
        from llm import manage_slot_cache
        cache_filename = f"{project_id}_{model_key}.bin"
        try:
            await manage_slot_cache(route.port, "save", cache_filename)
            logger.debug("Saved KV cache for project '%s' model '%s'", project_id, model_key)
        except Exception:
            logger.debug("Cache save failed for %s (non-critical)", cache_filename)


# ---------------------------------------------------------------------------
# Graceful Guillotine — cleanly terminate a stream after an audit failure
# ---------------------------------------------------------------------------

async def _graceful_guillotine_chunk(
    job_id: str,
    reason: str,
) -> str:
    """
    Generate the final SSE chunk that cleanly terminates a stream after
    a confirmed audit failure.

    Balances trailing JSON/Markdown syntax and appends the proxy audit
    override message before closing.
    """
    # The override message informs the client that the proxy halted the
    # stream due to a quality/safety concern.
    override_msg = (
        f"\n\n[PROXY AUDIT OVERRIDE: Error detected. Stream halted. "
        f"Reason: {reason}]"
    )

    chunk = {
        "id": f"chatcmpl-{job_id[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "proxy-audit-override",
        "choices": [{
            "index": 0,
            "delta": {"content": override_msg},
            "finish_reason": "audit_override",
        }],
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Embedded command handlers
# ---------------------------------------------------------------------------

async def _handle_pause_command(
    duration_mins: int,
    state,
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


async def _handle_resume_command(state) -> StreamingResponse:
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

def _make_system_chunk(content: str) -> dict:
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


# ---------------------------------------------------------------------------
# Triage message builder — transparent first SSE chunk
# ---------------------------------------------------------------------------

def _build_triage_message(route: RouteDecision) -> str:
    """
    Build a human-readable triage message that informs the user which
    model was selected and why, without altering the conversation.

    Example output:
        🔍 Proxy triage: classified as CODE (priority 1).
        Routing to professional on port 13103.

    Parameters
    ----------
    route : RouteDecision
        The resolved routing decision.

    Returns
    -------
    str
        A single-line informational message, no trailing newline.
    """
    # Determine the human-readable model description
    model_descriptions = {
        "frontdesk":     "Front Desk (2B classifier)",
        "lifeboat":      "Lifeboat (8B CPU fallback)",
        "chatter":       "Chatter (9B fast chat)",
        "worker":        "Worker (9B tool-capable)",
        "reasoning":     "Auditor (8B DeepSeek-R1 reasoning)",
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

    # Build the triage line
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
