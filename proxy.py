"""
proxy.py - Kinver AI Proxy server entry-point.

This is the main entry point for the Kinver Hub Hybrid API Gateway.
It assembles all components (database, systemd controller, cooling
state machine, hardware governor) and exposes the OpenAI-compatible
API via FastAPI on port 13000.

Architecture:
- uvloop event loop for maximum async performance
- FastAPI lifespan manages component initialisation and teardown
- Background tasks: thermal monitor, queue worker, ZRAM keepalive
- All routes are defined in routes.py and wired here
- Dependencies are passed via app.state (no global singletons)

Glass Pipe Rule:
    This proxy MUST NOT alter, inject, or sanitize the text content of
    system prompts or user messages sent by external frontends.
    Prompting is strictly the responsibility of the client software.
    The proxy may only generate prompts for its own internal routing
    tasks (frontdesk classification).

Usage::

    python proxy.py

    # Or via systemd:
    systemctl start ai-proxy.service

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
import uvloop
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Configure logging (to both rotating file and stdout)
# ---------------------------------------------------------------------------
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

# File handler with rotation (10 MB max, keep 5 backups)
file_handler = RotatingFileHandler(
    Path(__file__).resolve().parent / "proxy.log",
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

logger.info("PROXY STARTING — Logging configured")

# ---------------------------------------------------------------------------
# Import internal modules (after logging is set up)
# ---------------------------------------------------------------------------
from constants import (
    PROJECT_ROOT,
    DB_PATH,
    SENSOR_INTERVAL,
)
from database import Database
from systemd import SystemdController
from cooling import CoolingStateMachine
from hardware import (
    HardwareGovernor,
    ThermalState,
    default_thermal_state,
    thermal_monitor_task,
)
from llm import (
    openrouter_cloud_escalation,
)
from opencode_bridge import (
    ensure_opencode_serve,
    opencode_escalation,
)
from constants import CLOUD_ESCALATION_BACKEND
from profile_loader import load_model_profiles, ModelProfileTable

# Route handlers (imported from routes.py)
from routes import (
    health_check,
    list_models,
    chat_completions,
    _opencode_session_state,
)

# ---------------------------------------------------------------------------
# Application state (attached to app.state during lifespan)
# ---------------------------------------------------------------------------


class AppState:
    """
    Mutable application state shared across all route handlers via
    ``request.app.state``.

    Attributes
    ----------
    database : Database
        Async SQLite interface for the job queue and semantic cache.
    systemd : SystemdController
        Async Systemd service lifecycle manager.
    cooler : CoolingStateMachine
        3-stage predictive cooling state machine.
    hardware : HardwareGovernor
        GPU workload isolation and thermal monitoring.
    server_start_ts : float
        Monotonic timestamp of server start (for uptime calculation).
    active_priority : int
        Current active job priority (1=HIGH, 2=NORMAL, 3=IDLE).
    active_heavy_model : str or None
        Domain name of the currently active heavy GPU model.
    requests_served : int
        Total number of requests processed since startup.
    model_profiles : ModelProfileTable or None
        Build-time-synced HF model profile table (R18).
    pause_task : asyncio.Task or None
        Active queue-pause timer task.
    transition_pause : asyncio.Event
        Set when the queue is paused for OS transitions.
    thermal_halt : asyncio.Event
        Set when thermal critical thresholds are breached.
    """

    def __init__(self) -> None:
        self.database: Optional[Database] = None
        self.systemd: Optional[SystemdController] = None
        self.cooler: Optional[CoolingStateMachine] = None
        self.hardware: Optional[HardwareGovernor] = None
        self.server_start_ts: float = time.time()
        self.active_priority: int = 3  # IDLE
        self.active_heavy_model: Optional[str] = None
        self.requests_served: int = 0
        self.model_profiles: Optional[ModelProfileTable] = None
        self.pause_task: Optional[asyncio.Task] = None
        self.transition_pause: asyncio.Event = asyncio.Event()
        self.thermal_halt: asyncio.Event = asyncio.Event()
        self.thermal_state: ThermalState = default_thermal_state()

    # ------------------------------------------------------------------
    # Pause / resume queue (for OS transitions)
    # ------------------------------------------------------------------

    async def try_pause_queue(self, duration_secs: int) -> tuple[bool, str]:
        """
        Attempt to pause the queue for *duration_secs*.

        Returns (success, message).  If a high-priority task (priority
        <= 2) is active, the pause is denied.
        """
        if self.active_priority <= 2:
            return False, "Cannot pause: A high-priority task is actively running."

        # Cancel any existing pause timer
        if self.pause_task and not self.pause_task.done():
            self.pause_task.cancel()

        async def _pause_timer(seconds: int):
            try:
                self.transition_pause.set()
                logger.info("Queue paused for %d seconds (OS transition)", seconds)
                await asyncio.sleep(seconds)
                self.transition_pause.clear()
                logger.info("Queue resumed automatically after pause")
            except asyncio.CancelledError:
                pass

        self.pause_task = asyncio.create_task(_pause_timer(duration_secs))
        # Brief delay to let the queue worker observe the pause event
        await asyncio.sleep(1.5)
        return True, f"Queue paused for {duration_secs} seconds."

    def try_resume_queue(self) -> None:
        """
        Manually resume the queue (cancel the pause timer).
        """
        if self.pause_task and not self.pause_task.done():
            self.pause_task.cancel()
        self.transition_pause.clear()
        logger.info("Queue manually resumed")


# ---------------------------------------------------------------------------
# Lifespan: initialise and tear down all components
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    FastAPI lifespan context manager.

    On startup:
    1. Initialise Database (WAL mode, migrations, sqlite-vec)
    2. Initialise SystemdController
    3. Initialise CoolingStateMachine, write BASELINE
    4. Initialise HardwareGovernor
    5. Load model profiles
    6. Spawn background tasks (thermal monitor, queue worker, ZRAM keepalive)
    7. Restore pending jobs from database

    On shutdown:
    1. Cancel background tasks
    2. Write BASELINE cooling
    3. Close database
    """
    state: AppState = app.state  # type: ignore[attr-defined]
    logger.info("Kinver Hybrid API Gateway starting (uvloop=%s)", uvloop is not None)

    # ---- 1. Database -------------------------------------------------------
    db = Database(DB_PATH)
    await db.initialize()
    state.database = db

    # ---- 2. Systemd Controller ---------------------------------------------
    systemd = SystemdController()
    state.systemd = systemd

    # ---- 3. Cooling State Machine ------------------------------------------
    cooler = CoolingStateMachine()
    state.cooler = cooler
    cooler.baseline_idle()  # Silent idle at startup

    # ---- 4. Hardware Governor ----------------------------------------------
    hw = HardwareGovernor()
    state.hardware = hw

    # ---- 5. Model profiles (R18) -------------------------------------------
    state.model_profiles = load_model_profiles(
        PROJECT_ROOT / "config" / "model_profiles.yaml"
    )

    # ---- 5b. OpenCode serve backend (best-effort) --------------------------
    # The bridge (model "opencode", /opencode, queue-worker escalation)
    # needs a headless opencode serve listening on OPENCODE_SERVE_URL.
    # Spawn it here when missing; never block startup on it.
    if CLOUD_ESCALATION_BACKEND == "opencode":
        await ensure_opencode_serve()

    # ---- 5c. Warm the pinned-opencode-session map (Rule 3) -----------------
    # _opencode_session_state loads the disk-backed session map lazily on
    # first access.  Warm it HERE (startup, off the request path) so the
    # one-time sync disk read never runs on a request — the request path
    # then only ever touches the cached app.state dict.
    _opencode_session_state(app)  # noqa: B018 — intentional warm-up

    # ---- 6. Background tasks -----------------------------------------------
    # Thermal monitor (reads sensors, enforces shutdown thresholds,
    # keeps professional resident on the GPU when it is free)
    thermal_task = asyncio.create_task(
        thermal_monitor_task(state.thermal_state, SENSOR_INTERVAL, systemd),
    )

    # Queue worker (processes enqueued jobs from the database)
    queue_task = asyncio.create_task(queue_worker(state))

    # ZRAM keepalive (pings core models to prevent Linux swap-out)
    zram_task = asyncio.create_task(zram_keepalive_worker(state))

    # ---- 7. Restore pending jobs from database -----------------------------
    await _restore_pending_jobs(state)

    logger.info("Kinver Hybrid API Gateway ready on port 13000")

    try:
        yield
    finally:
        logger.info("Shutting down Kinver Hybrid API Gateway...")

        # Cancel background tasks
        for task in (thermal_task, queue_task, zram_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Baseline cooling
        cooler.baseline_idle()

        # Close database
        await db.close()

        logger.info("Kinver Hybrid API Gateway shutdown complete")


# ---------------------------------------------------------------------------
# Queue worker — processes jobs from the database
# ---------------------------------------------------------------------------

async def queue_worker(state: AppState) -> None:
    """
    Main lifecycle orchestrator that processes priority-ordered jobs
    from the SQLite database.

    Runs forever until cancelled.  Respects:
    - thermal_halt: pauses when hardware is critically hot
    - transition_pause: pauses for OS transitions / gaming mode
    """
    logger.info("Queue worker started")

    while True:
        # ---- Respect pause events -------------------------------------------
        if state.thermal_halt.is_set():
            logger.info("Queue paused due to thermal halt")
            await asyncio.sleep(2)
            continue

        if state.transition_pause.is_set():
            logger.info("Queue paused due to OS transition")
            await asyncio.sleep(2)
            continue

        # ---- Dequeue next job from database ---------------------------------
        db = state.database
        if db is None:
            await asyncio.sleep(1)
            continue

        job = await db.dequeue_next(max_priority=state.active_priority)
        if job is None:
            # No jobs available — brief sleep before polling again
            await asyncio.sleep(1)
            continue

        state.active_priority = job["priority"]
        state.requests_served += 1

        logger.info(
            "Processing job %s (intent=%s priority=%d failure_count=%d)",
            job["id"], job["intent"], job["priority"], job["failure_count"],
        )

        # ---- Process based on intent ----------------------------------------
        try:
            intent = job["intent"]
            failure_count = job["failure_count"]
            systemd = state.systemd

            # ---- CHAT / TOOL intents are handled by direct streaming --------
            # The queue worker must NOT steal these jobs from the direct
            # SSE streaming path.  Completing them here would race with the
            # active stream, causing double-processing, escalation to heavy
            # models with insufficient context, and corrupted output.
            if intent in ("CHAT", "TOOL"):
                logger.info(
                    "Job %s skipped by queue worker (intent=%s — handled by direct stream)",
                    job["id"], intent,
                )
                # Mark as completed so it doesn't get re-dequeued endlessly
                await db.complete_job(
                    job["id"],
                    finish_reason="stop",
                    full_content="",
                )
                state.active_priority = 3
                continue

            # ---- Escalation matrix: failure_count → model tier ------------
            if failure_count == 0:
                target_model = intent  # First attempt: use classified intent
            elif failure_count == 1:
                target_model = "professional"  # Second attempt: 35B MoE
                logger.info("Job %s escalated to professional (failure_count=1)", job["id"])
            elif failure_count == 2:
                target_model = "coder"  # Third attempt: 27B Dense
                logger.info("Job %s escalated to coder (failure_count=2)", job["id"])
            else:
                # Max local tiers exhausted → route to cloud
                logger.info(
                    "Job %s escalated to cloud (failure_count=%d, local exhausted)",
                    job["id"], failure_count,
                )
                messages = json.loads(job["messages_json"])
                user_text = " ".join(
                    m["content"] for m in messages if m.get("role") == "user"
                )
                if CLOUD_ESCALATION_BACKEND == "opencode":
                    logger.info(
                        "Job %s escalated to opencode (failure_count=%d, local exhausted)",
                        job["id"], failure_count,
                    )
                    cloud_resp = await opencode_escalation(failure_count, user_text)
                else:
                    cloud_resp = await openrouter_cloud_escalation(failure_count, user_text)
                await db.complete_job(
                    job["id"],
                    finish_reason="cloud_escalation",
                    full_content=cloud_resp,
                )
                state.active_priority = 3
                continue

            # Normalize to a known model key
            if target_model not in systemd._port_cache:
                # Default to professional if the model is unknown
                target_model = "professional"

            # ---- Hot-swap if needed -----------------------------------------
            current_heavy = systemd.active_heavy_model
            needed_port = await systemd.get_port(target_model)

            if current_heavy != target_model and target_model in (
                "professional", "coder", "creative", "scholar", "architect",
            ):
                logger.info("Hot-swapping GPU from %s to %s", current_heavy, target_model)
                await systemd.hot_swap(
                    from_domain=current_heavy or "",
                    to_domain=target_model,
                )
                state.active_heavy_model = target_model

            # ---- This is a queued job — update DB state and stream ----------
            # For now, queued jobs complete in the queue worker
            # Full streaming support will come in a future iteration
            await db.complete_job(
                job["id"],
                finish_reason="stop",
                full_content=job.get("partial_content", ""),
            )
            logger.info("Job %s completed", job["id"])

        except Exception:  # noqa: BLE001 — queue-worker boundary (AGENTS.md
        # rule 10): a catch-all here keeps one bad job from killing the worker
        # loop; each job is failed + escalated individually.
            logger.exception("Job %s failed with exception", job["id"])
            await db.fail_job(job["id"])
            # Re-queue for escalation
            await db.escalate_job(job["id"], "professional")

        finally:
            state.active_priority = 3  # IDLE
            # Clean up: unload heavy model if queue is empty.
            # Only unload for heavy GPU models — lightweight models
            # (chatter) is handled by direct streaming and
            # must not be disrupted by the queue worker.
            if db:
                pending = await db.get_pending_jobs()
                if not pending and systemd:
                    await _cleanup_idle_heavy(systemd, state)


async def _cleanup_idle_heavy(
    systemd: SystemdController,
    state: AppState,
) -> None:
    """
    Idle-queue cleanup for heavy GPU models.

    Professional is the resident default; when it is active we keep it
    loaded to avoid 30–120 second cold starts on the next default request.
    Specialists are unloaded normally.  If the controller has lost track of
    an externally started Professional, we reconcile by probing systemd and
    keep it resident.  Fail-safe: any probe error skips destructive cleanup.
    """
    active = systemd.active_heavy_model
    if active is None:
        try:
            if await systemd.is_active("professional"):
                active = "professional"
                systemd.active_heavy_model = active
        except OSError:
            logger.warning(
                "Failed to probe professional service during idle cleanup; "
                "skipping destructive unload as a fail-safe"
            )
            return

    if active == "professional":
        logger.info("Queue empty — Professional remains resident")
        state.active_heavy_model = "professional"
        return

    await systemd.unload_all_heavy()
    state.active_heavy_model = None


# ---------------------------------------------------------------------------
# ZRAM keepalive worker — pings core models to prevent swap-out
# ---------------------------------------------------------------------------

async def zram_keepalive_worker(state: AppState) -> None:
    """
    Periodically pings core CPU-resident models (frontdesk, chatter)
    to prevent Linux from swapping them out of RAM to ZRAM.

    Runs every 240 seconds (4 minutes).
    """
    core_domains = ["frontdesk", "chatter"]
    systemd = state.systemd

    if systemd is None:
        logger.warning("ZRAM keepalive: no SystemdController available")
        return

    logger.info("ZRAM keepalive worker started (domains=%s)", core_domains)

    while True:
        await asyncio.sleep(240)  # 4 minutes
        async with httpx.AsyncClient() as client:
            for domain in core_domains:
                try:
                    # Use the completions endpoint with a minimal no-op prompt
                    # to trigger a cache access, keeping pages warm in RAM
                    port = await systemd.get_port(domain)
                    await client.post(
                        f"http://127.0.0.1:{port}/completion",
                        json={
                            "prompt": "[SYSTEM_KEEPALIVE]",
                            "max_tokens": 0,
                            "cache_prompt": False,
                            "thinking_budget_tokens": 0,
                        },
                        timeout=2.0,
                    )
                    logger.debug("ZRAM keepalive pinged %s on port %d", domain, port)
                except (httpx.HTTPError, OSError, ValueError):
                    logger.debug("ZRAM keepalive ping failed for %s (non-critical)", domain)


# ---------------------------------------------------------------------------
# Restore pending jobs from database at startup
# ---------------------------------------------------------------------------

async def _restore_pending_jobs(state: AppState) -> None:
    """
    At startup, scan the database for jobs that were interrupted by a
    previous crash (state = 'queued' or 'active') and log them.

    The queue worker will pick them up naturally on its next poll.
    """
    db = state.database
    if db is None:
        return

    try:
        pending = await db.get_pending_jobs()
        if pending:
            logger.info(
                "Restored %d pending job(s) from previous session", len(pending),
            )
            for job in pending:
                logger.debug(
                    "  Pending job %s: intent=%s priority=%d state=%s",
                    job["id"], job["intent"], job["priority"], job["state"],
                )
        else:
            logger.info("No pending jobs to restore")
    except Exception:  # noqa: BLE001 — lifespan boundary (AGENTS.md rule 10):
    # startup restore must never abort the whole app on one bad queued job.
        logger.exception("Failed to restore pending jobs (non-fatal)")


# ---------------------------------------------------------------------------
# FastAPI application assembly
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Kinver Hub Hybrid API Gateway",
    description=(
        "High-performance, stateful AI proxy with GPU-aware routing, "
        "and predictive cooling for AMD hardware."
    ),
    version="3.0.0",
    lifespan=lifespan,
)

# Attach empty AppState — populated during lifespan startup
app.state = AppState()

# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

app.add_api_route("/health", health_check, methods=["GET"])
app.add_api_route("/v1/models", list_models, methods=["GET"])
app.add_api_route("/v1/chat/completions", chat_completions, methods=["POST"])


async def _transition_check_route(request: Request) -> JSONResponse:
    """GET /v1/system/transition-check — current cooling/queue transition state."""
    return await _transition_check(request)


async def _resume_queue_route(request: Request) -> JSONResponse:
    """GET /v1/system/queue/resume — resume a paused job queue."""
    return await _resume_queue(request)


app.add_api_route(
    "/v1/system/transition-check",
    _transition_check_route,
    methods=["GET"],
)
app.add_api_route(
    "/v1/system/queue/resume",
    _resume_queue_route,
    methods=["GET"],
)


# ---------------------------------------------------------------------------
# /v1/system endpoints (inline — simple enough to keep in proxy.py)
# ---------------------------------------------------------------------------

async def _transition_check(request: Request) -> JSONResponse:
    """
    API for OS transitions and gaming mode.

    Query params: ``?duration=X`` (seconds, default 10).
    """
    duration = int(request.query_params.get("duration", 10))
    state: AppState = request.app.state
    success, message = await state.try_pause_queue(duration)
    if not success:
        return JSONResponse(
            status_code=423,
            content={"status": "busy", "message": message},
        )
    return JSONResponse({
        "status": "ready",
        "pause_duration": duration,
        "message": message,
    })


async def _resume_queue(request: Request) -> JSONResponse:
    """
    Manual override to unpause the queue if a gaming session ends early.
    """
    state: AppState = request.app.state
    state.try_resume_queue()
    return JSONResponse({
        "status": "resumed",
        "message": "Queue manually resumed.",
    })


# ---------------------------------------------------------------------------
# Main entry-point: install uvloop and run via uvicorn
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    # Install uvloop as the default event loop policy
    # This must happen BEFORE uvicorn.run() creates the event loop
    uvloop.install()
    logger.info("uvloop event-loop policy installed")

    uvicorn.run(
        "proxy:app",
        host="0.0.0.0",
        port=13000,
        log_level="info",
        reload=False,
        timeout_keep_alive=300,  # 5 minutes — matches long generation timeout
    )