"""
opencode_bridge.py — route requests to a headless opencode serve backend.

OpenCode exposes an HTTP API via ``opencode serve`` (session/message/part
endpoints).  This module translates a simple OpenAI-style task into that
API so frontends (OpenWebUI, nanobot) can DIRECT a request to the opencode
agent (the full agentic loop with bash/edit/read tools) instead of a local
llama model:

    POST /session                       → create a fresh session
    POST /session/:id/message           → run the agent (blocks until done)
    response.parts[].text               → collected assistant text

Used by:
    - routes.py: ``model: "opencode"`` requests and the ``/opencode``
      embedded command.
    - proxy.py: queue-worker cloud escalation when local tiers are
      exhausted (``CLOUD_ESCALATION_BACKEND = "opencode"``).

The response text is post-processed to drop proxy-status content
(sentinel-prefixed triage) that the opencode client accumulates as
assistant text.

Message-path HTTP primitives route through the module-level ``BACKEND``
instance from ``opencode_backends.py`` (openchamber-bridge-backend design,
step 1): session create/send/fetch/abort, permission and question relay.
The ``/event`` SSE bus and the serve lifecycle (spawn/recycle/config
sync) stay in this module — they are serve-specific and belong to later
steps of the design.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx

from constants import (
    OPENCODE_AGENT,
    OPENCODE_BIN,
    OPENCODE_SERVE_TIMEOUT,
    OPENCODE_SERVE_URL,
    OPENCODE_SECURE_SERVE_URL,
    OPENCODE_BRIDGE_DIRECTORY,
    OPENCODE_SERVE_CONFIG_DIR,
    OPENCODE_SERVE_PURE,
    OPENCODE_WORKSPACE_DIR,
    OPCODE_CONFIG_PATH,
    _machine,
    get_logger,
)
from opencode_backends import BACKEND

logger = get_logger("kinver.opencode_bridge")

# System prompt sent to every bridge session.  It carries ONLY the channel
# contract: the API-facing identity/language policy, the single-stream reply
# plus turn-end/question contract, and the narration requirement.  The model's
# own agent config (the agent the server selects) owns orchestration and tool
# policy, so the bridge does not restate them.  The gentle-orchestrator's
# persona (AGENTS.md) defaults direct replies to Rioplatense Spanish, which
# is wrong for an API-facing bridge consumed by OpenWebUI/nanobot — pin
# English unless the user's own message is in another language.
_BRIDGE_SYSTEM_PROMPT = (
    "You are an API-backed coding assistant reached through a proxy bridge. "
    "Respond in English unless the user's message is written in another "
    "language. Keep the final answer concise and in English.\n\n"
    "Deliver each request as one stream: the user expects the finished "
    "result in one stream, not a series of continue-pings.  End your turn "
    "only when the task is done, or when you must ask the user a clarifying "
    "question (the user CAN answer in their next message and the same "
    "session resumes).\n\n"
    "NARRATE (MANDATORY): before each step, tell the user in one short "
    "sentence what you are doing and why; after each step, tell them what "
    "happened in one short sentence.  Never run a tool in silence — a "
    "silent tool run looks like a dead stream to the user.\n"
)

#: The exact marker the orchestrator ends its final message with when an
#: inline phase fails to produce its artifact (the loud terminal failure
#: of the sub-agent contract).  The cycle driver matches it to exit
#: loudly instead of resuming.
_SDD_TERMINAL_FAILURE_MARKER = "SDD-CYCLE-TERMINAL-FAILURE"


#: Canonical SDD sub-agent contract. Shared verbatim with the cycle
#: driver's task text (``build_task``) and the serve-config orchestrator
#: prompt; the sync test ``test_sdd_subagent_contract_sites_in_sync``
#: guards against drift.  Supersedes the old "delegate ONCE, DO NOT retry"
#: fallback rule with the bounded contract (artifact-check-first, one
#: retry, inline fallback, loud terminal failure).
_SDD_SUBAGENT_CONTRACT = (
    "SUB-AGENT CONTRACT (MANDATORY): delegate each SDD phase to its phase "
    "sub-agent (sdd-explore/sdd-propose/sdd-spec/sdd-design/sdd-tasks/"
    "sdd-apply/sdd-verify/sdd-archive). If the sub-agent's result does not "
    "arrive (the task tool hangs or errors), CHECK the phase artifact on "
    "disk FIRST: if it exists, the work landed despite the delivery "
    "failure - the phase is complete. If it is missing, RETRY the "
    "sub-agent ONCE. If the retry also fails, check the artifact again: "
    "exists -> complete; missing -> perform that phase INLINE yourself "
    "with your own tools (read/write/edit/bash) and write the artifact "
    "yourself. NEVER delegate a phase more than twice (initial + one "
    "retry), and never start a retry while the original sub-agent session "
    "is still running. A phase is COMPLETE ONLY when its artifact file "
    "exists - proposal.md, specs/<change>/spec.md, design.md, tasks.md, "
    "the applied code + tests, verify-report.md, and finally "
    "archive-report.md. Never skip a phase's artifact. If an inline phase "
    "also fails to produce its artifact, STOP the cycle immediately: end "
    "your final message with the exact marker "
    "'" + _SDD_TERMINAL_FAILURE_MARKER + ": <phase>' and list the artifacts produced "
    "so far - do NOT retry, do NOT continue the pipeline, do NOT ask the "
    "user anything."
)

# Recycle the opencode serve after this uptime: the serve's agent-loop
# tool runner progressively wedges (bash hangs on trivial commands even
# with healthy memory); a fresh serve runs bash reliably.
_SERVE_RECYCLE_AFTER_S: float = 1800.0


# Quiet period after a finished step before the bridge considers the
# agentic session complete (the final summary message follows the last
# tool step within milliseconds; a step-finish alone is not the end).
_EVENT_QUIET_TIMEOUT: float = 8.0
# Faster quiet threshold once a step has finished: the next message (or
# the end) arrives within milliseconds, so 3s of silence means done.
_EVENT_FINAL_TIMEOUT: float = 3.0

# A tool part stuck in "running" with no output for this long is WEDGED:
# the serve's tool runner marked the tool started but never executed it,
# so the session stays "busy" forever while the client sees keepalives.
_TOOL_WEDGE_AFTER_S: float = 300.0

#: A ``task`` tool part waits on a sub-agent session, which legitimately
#: runs for many minutes (the TUI's SDD cycles routinely take 5-20 min
#: per sub-agent phase).  The 120s tool threshold would abort healthy
#: phases at the first sub-agent lull, so task parts get their own,
#: much longer window (2026-08-09).
_SESSION_CONTEXT_CAP_TOKENS: int = 60_000


#: A session's context CANNOT be reset by the agent inside it (the
#: professional-as-subagent has no tools to shed history).  The bridge is
#: the only reset point: a pinned session whose estimated context exceeds
#: _SESSION_CONTEXT_CAP_TOKENS is dropped on the next resume and a fresh
#: session starts — the durable state lives in the artifact/repo files
#: (2026-08-14, user-directed after the weight_loss cycle hit 114k tokens
#: and paid a 7.5-minute full re-prefill).
_TASK_WEDGE_AFTER_S: float = 600.0

#: STALL-AWARE ABORT (2026-08-11): a task part running this long whose
#: child session has dropped out of the live status map means the
#: sub-agent finished but the delivery never propagated (upstream
#: #11865/#23296; the serve's tool runner also wedges on glob calls,
#: stranding the child).  Fire the wedge early so the orchestrator's
#: artifact-check-first proceeds WITHOUT redoing the phase.
_TASK_STALL_AFTER_S: float = 180.0

# How often the stream checks the session for a wedged tool part.
_WEDGE_CHECK_INTERVAL_S: float = 10.0

# How often the stream re-announces the currently running tool command
# from the message list (bus path).  The announce must be faster than the
# wedge check: tools 10-60s long would otherwise run to completion
# before the first wedge cycle and the user would never see the command.
_ANNOUNCE_CHECK_INTERVAL_S: float = 3.0

#: Idle grace after the serve reports a finished step + idle session.
#: Multi-step turns (work-continuity) pause between steps while the model
#: generates the next action (5-40s on this hardware), and the serve can
#: briefly report "idle" between assistant messages — an eager return on
#: the first idle event cut the stream mid-turn while the agent kept
#: working (observed live 2026-08-14: the feed stopped after the grep
#: step; the session produced 45 more parts unseen).  The stream returns
#: only after this quiet window with NO new content; any new part resets
#: it.
_IDLE_GRACE_S: float = 45.0

#: Fallback plugin's project-local replay log (the rate-limit-fallback
#: plugin mirrors its log into the repo's git dir).
_FALLBACK_REPLAY_LOG = Path(__file__).parent / ".git" / "gentle-ai" / "rate-limit-fallback.log"

#: A replay window stays open for this long after its last event before
#: the reader treats it as stale.  Replays legitimately run 5-20 min, so
#: the horizon must exceed that; the bound exists so a phantom window
#: (start without a terminal event) cannot pause the hold/wedge forever.
_FALLBACK_REPLAY_STALE_S: float = 1800.0


def _replay_in_flight_for_any_session() -> bool:
    """True when the fallback plugin shows a replay window open for any
    session: a ``fallback_cycle_started`` event within the staleness
    horizon with no terminal event after it.  The wedge timer and the
    cycle driver's hold pause while a replay is in flight; a missing or
    unreadable log fails closed to False (today's behavior).
    """
    try:
        lines = _FALLBACK_REPLAY_LOG.read_text(
            encoding="utf-8", errors="replace",
        ).splitlines()
    except OSError:
        return False
    open_window = False
    horizon_ms = _FALLBACK_REPLAY_STALE_S * 1000.0
    now_ms = time.time() * 1000.0
    for line in lines[-200:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ts = rec.get("timestamp")
        if not isinstance(ts, str):
            continue
        try:
            ms = (
                datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                * 1000.0
            )
        except ValueError:
            continue
        if now_ms - ms > horizon_ms:
            continue
        event = str(rec.get("event", ""))
        if event == "fallback_cycle_started":
            open_window = True
        elif any(k in event for k in
                 ("completed", "failed", "cleared", "settled")):
            open_window = False
    return open_window


#: How often the BLOCKING path (opencode_chat) polls GET /permission while
#: its message POST is in flight.  The blocking call has no SSE bus, so a
#: parked permission gate is only visible through polling; 5s matches the
#: streaming path's permission-check cadence class.
_BLOCKING_PERMISSION_POLL_S: float = 5.0

# Sentinel-prefixed proxy status the opencode client accumulates into the
# assistant text (the proxy emits triage as the first SSE chunk).  The
# triage formats are stable (routes._build_triage_message).  Stripping the
# leading segment keeps the response clean without touching model text.
_STATUS_SEGMENT_RE = re.compile(
    r"\u200b(?:"
    r"🔍 Proxy triage:.*?on port \d+\."
    r"|🔀 Client specified.*?wins\.\)"
    r"|🔀 Client specified.*?on port \d+\."
    r")"
)

# Permission requests from the opencode serve (opencode >= 1.18): the
# external_directory gate fires when a bash tool touches paths outside the
# session workspace.  In headless serve mode there is no interactive user
# to answer, so an unanswered gate silently auto-rejects AND leaves the
# tool wedged in "running" — the root cause of the bridge wedges.  The
# bridge relays every permission it sees to the USER.  Pending permissions
# are keyed by session id so the next request (which resumes the pinned
# session) can answer them.

_PERMISSION_QUESTION_TEMPLATE: str = (
    "🔒 The coding agent wants to access a path outside its workspace.\n\n"
    "Target: {target}\n"
    "Command: {cmd}\n\n"
    "Reply `allow` (this once), `always` (auto-allow access like this), "
    "or `reject`."
)

# Git ask-rules (git commit/push/reset/rebase) fire the "bash" permission
# type.  Every one of those is mutating, so relay ALL of them to the user
# (there is no read variant worth auto-allowing).
_GIT_PERMISSION_QUESTION_TEMPLATE: str = (
    "🔒 The coding agent wants to run a git command.\n\n"
    "Command: {cmd}\n\n"
    "Reply `allow` (this once), `always` (auto-allow git commands like "
    "this), or `reject`."
)


def _permission_target(perm: dict[str, Any]) -> str:
    """Best human-readable description of what a permissioned access
    targets: the exact file path when the tool knows one (write/edit
    tools carry ``metadata.filepath``), else the parent directory, else
    the matched pattern.  Never raises."""
    try:
        meta = perm.get("metadata") or {}
        if isinstance(meta, dict):
            fp = str(meta.get("filepath") or "").strip()
            if fp:
                return fp
            parent = str(meta.get("parentDir") or "").strip()
            if parent:
                return parent + "/"
        patterns = perm.get("patterns") or []
        if patterns:
            return str(patterns[0])
    except (TypeError, ValueError):
        pass
    return "(unknown path)"


# Permission types the bridge relays to the user.  external_directory is
# the write-outside-workspace gate; bash is the command-pattern gate (the
# git ask-rules in opencode.json permission.bash).  write/edit are the
# 1.18.15 write/edit TOOL gates (REQ-1): they may omit the SSE
# permission.updated event entirely (F2), so they also flow through the
# POLLING paths — GET /permission records carry ``permission: "write"`` /
# ``"edit"`` as the value, which this set matches (task 1.9).
_RELAYED_PERMISSION_TYPES: tuple[str, ...] = (
    "external_directory", "bash", "write", "edit",
)

#: Status emitted while a permission prompt is pending under a backend that
#: does NOT answer permissions (the secure server, whose own
#: ``opencode-perm`` resolves prompts out-of-band).  The bridge relays no
#: question and POSTs no answer — it only tells the client the session is
#: parked, once per permission id.
_WAITING_PERMISSION_STATUS = (
    "🔐 waiting on a permission prompt "
    "(the session is paused; resolve it with opencode-perm)"
)


def _parse_permission_answer(answer: str, *, write: bool = False) -> str:
    """Map the user's reply to a permission response: "once"|"always"|"reject".

    "always" wins (so "allow always" is not misread as a one-shot);
    "reject"/"deny"/a standalone "no" rejects; "allow"/"yes"/"y"/"ok"/
    "go ahead" grants once.

    For WRITE-class permissions (``write=True``) the default is STRICT:
    only an explicit allow/always grants — an unrelated follow-up, a bare
    "continue", or empty text REJECTS.  The relay exists so the human
    decides external writes; anything short of an explicit yes is a no.
    """
    lowered = (answer or "").strip().lower()
    if "always" in lowered:
        return "always"
    if "reject" in lowered or "deny" in lowered or re.search(r"\bno\b", lowered):
        return "reject"
    if write:
        if any(tok in lowered for tok in ("allow", "yes", "y", "ok", "go ahead", "approve")):
            return "once"
        return "reject"
    return "once"


async def _post_permission_response(
    session_id: str,
    permission_id: str,
    response: str,
) -> bool:
    """POST a permission decision to the opencode serve (best-effort).

    Returns True when the serve accepted the response; False on network
    errors (the caller proceeds with the pinned continuation either way).
    Never raises.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await BACKEND.reply_permission(
            client, session_id, permission_id, response,
        )


async def _abort_session_best_effort(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Best-effort abort of one opencode session (blocking-path cleanup).

    The blocking escalation path has no SSE bus, so it cannot watch for
    wedged tools; when it gives up on a session (timeout, HTTP error,
    write-permission abort) it must not leave a busy zombie behind.
    Never raises — the backend contract guarantees it (see
    ``OpenCodeBackend.abort_session``).
    """
    await BACKEND.abort_session(client, session_id)


async def _abort_stream_session_best_effort(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    session_map: Optional[dict[str, str]] = None,
    session_key: Optional[str] = None,
    pending_permissions: Optional[dict[str, tuple[str, bool]]] = None,
) -> None:
    """Streaming-path cleanup trio: best-effort abort + pin drop + pending
    permission pop.

    Every ``opencode_chat_stream`` error exit runs this so a failed or
    abandoned request never leaves a busy agent session on the serve with
    the ``session_map`` pin pointing at it.  Distinct from the blocking-path
    helper ``_abort_session_best_effort`` (cycle 5) so the two paths stay
    merge-safe.  Never raises; a falsy ``session_id`` (session create
    failed) is a no-op.
    """
    if not session_id:
        return
    await _abort_session_best_effort(client, session_id)
    if pending_permissions is not None:
        pending_permissions.pop(session_id, None)
    if session_map is not None and session_key:
        session_map.pop(session_key, None)


async def _handle_permission_event(
    client: httpx.AsyncClient,
    session_id: str,
    perm: dict[str, Any],
    pending_permissions: dict[str, tuple[str, bool]],
) -> Optional[str]:
    """Handle one permission request from the opencode serve.

    THE single permission handler, shared by all four relay paths (the
    polling-wedge check, the event-bus-timeout check, the ``permission.updated``
    SSE event, and the completion resolver) so the relay policy lives in
    ONE place.

    ``perm`` is either an SSE event ``properties`` dict (keys: id, type,
    title, metadata, tool) or a GET /permission record (keys: id,
    permission, patterns, tool).  Returns None only when the record is
    malformed; otherwise returns a question string for the USER to decide
    (write/git ask).  Never raises.
    """
    if not BACKEND.answers_permissions:
        # A non-answering backend (the secure server) never relays a
        # permission: its own ``opencode-perm`` process resolves prompts
        # out-of-band, so there is no question to build and no pending entry
        # to answer on a later request.  Callers keep the request/stream
        # alive and report the wait instead.
        return None
    pid = str(perm.get("id") or "")
    perm_type = str(perm.get("permission") or perm.get("type") or "")
    if not pid or not perm_type:
        return None
    # Pull the command for the question display: SSE events carry it in
    # metadata.command/title; GET /permission records carry patterns and the
    # tool part's state.input.command.
    cmd = ""
    meta = perm.get("metadata") or {}
    if isinstance(meta, dict):
        cmd = str(meta.get("command") or "")
    cmd = cmd or str(perm.get("title") or "")
    cmd = cmd or str((perm.get("patterns") or [""])[0])
    try:
        tool_info = perm.get("tool") or {}
        mid = tool_info.get("messageID")
        call_id = tool_info.get("callID")
        if mid:
            msg_resp = await BACKEND.fetch_message(client, session_id, mid)
            if msg_resp.status_code == 200:
                for part in (msg_resp.json().get("parts") or []):
                    if part.get("callID") == call_id or part.get("id") == call_id:
                        cmd = str(((part.get("state") or {}).get("input") or {}).get("command") or cmd)
                        break
    except (httpx.HTTPError, OSError, ValueError):
        pass
    pending_permissions[session_id] = (pid, True)
    template = (_GIT_PERMISSION_QUESTION_TEMPLATE
                if perm_type == "bash"
                else _PERMISSION_QUESTION_TEMPLATE)
    return template.format(target=_permission_target(perm), cmd=cmd[:300])


async def _detect_pending_permission(
    client: httpx.AsyncClient,
    session_id: str,
) -> Optional[dict[str, Any]]:
    """Return the first pending relayed permission for a session.

    The polling fallback cannot see ``permission.updated`` SSE events (the
    bus is closed), but the serve exposes pending requests at GET
    /permission.  A tool waiting on the gate looks identical to a wedged
    tool in the message list (status=running, no output), so this must be
    checked BEFORE the wedge detector fires — otherwise a write that is
    simply waiting for the user would be killed as a wedge.

    Returns the permission record dict (with id/sessionID/patterns/tool) or
    None.  Never raises.

    F2 (opencode 1.18.15): write/edit TOOL gates may omit the
    ``permission.updated`` SSE event entirely — the GET /permission record
    carries ``permission: "write"`` / ``"edit"`` as its value, which
    ``_RELAYED_PERMISSION_TYPES`` now matches (task 1.9), so write/edit
    asks are caught here and relayed exactly like the event-bus path.
    """
    try:
        resp = await BACKEND.fetch_permissions(client)
        if resp.status_code != 200:
            return None
        for perm in resp.json():
            if perm.get("sessionID") != session_id:
                continue
            if perm.get("permission") in _RELAYED_PERMISSION_TYPES:
                return perm
    except (httpx.HTTPError, OSError, ValueError):
        pass
    return None


def _waiting_permission_status(
    perm: dict[str, Any], announced_ids: set[str],
) -> Optional[str]:
    """The once-per-permission "waiting" status for a non-answering backend.

    On a backend that answers permissions (the serve/openchamber transports)
    this returns None — the caller relays the permission as a question
    instead, exactly as before.  On the secure backend there is no question
    to relay and no answer to POST (``opencode-perm`` resolves the prompt
    out-of-band), so the caller keeps the stream alive and reports the wait.
    Deduped by permission id (the existing announce-dedupe pattern), so a
    poll every tick never repeats the line.  Returns None when this
    permission id was already announced or has no id.
    """
    if BACKEND.answers_permissions:
        return None
    pid = str(perm.get("id") or "")
    if not pid or pid in announced_ids:
        return None
    announced_ids.add(pid)
    return _WAITING_PERMISSION_STATUS


def _strip_proxy_status_text(text: str) -> str:
    """Drop proxy-status segments from a collected assistant text string."""
    if not text or "\u200b" not in text:
        return text
    return _STATUS_SEGMENT_RE.sub("", text)


async def is_opencode_serve_running() -> bool:
    """True when a complete HTTP response arrives from the serve transport.

    Transport-liveness semantics: ANY received HTTP response — 2xx, 3xx,
    4xx, or 5xx — proves a process holds the configured port, so a
    non-2xx responder (version-specific, missing, or degraded /config)
    still blocks a duplicate spawn.  Only an ``httpx.HTTPError``
    (ConnectError / ConnectTimeout / ReadTimeout) or ``OSError`` means
    down.  The buffered ``.get()`` completion is the received-response
    boundary; the body is never explicitly read and the status is never
    interpreted.
    """
    try:
        async with httpx.AsyncClient() as client:
            await client.get(f"{OPENCODE_SERVE_URL}/config", timeout=3.0)
            return True
    except (httpx.HTTPError, OSError):
        return False


async def _secure_serve_answering() -> bool:
    """Pure health probe for the systemd-owned secure server.

    ANY received HTTP response proves a process holds the secure port
    (the same transport-liveness semantics as ``is_opencode_serve_running``);
    only a transport error (``httpx.HTTPError`` / ``OSError``) means down.
    Never raises, and never touches the serve-lifecycle helpers — it probes
    ``OPENCODE_SECURE_SERVE_URL`` directly, so no spawn/recycle/probe against
    the bridge-owned serve runs under the secure backend.
    """
    try:
        async with httpx.AsyncClient() as client:
            await client.get(f"{OPENCODE_SECURE_SERVE_URL}/config", timeout=3.0)
            return True
    except (httpx.HTTPError, OSError):
        return False


def _serve_unreachable_message() -> str:
    """The bridge-failure text for ``ensure_opencode_serve`` returning False.

    On a backend that owns no server (the secure backend) the message names
    the systemd-owned server so the operator knows the proxy will not start
    it; the serve backends keep the original byte-identical string.
    """
    if not BACKEND.owns_server:
        return (
            "[OpenCode Bridge Failed: the systemd-owned secure opencode "
            f"server ({OPENCODE_SECURE_SERVE_URL}) is not answering; start "
            "secure-opencode.service (the proxy never starts it).]"
        )
    return "[OpenCode Bridge Failed: opencode serve not reachable.]"


async def ensure_opencode_serve() -> bool:
    """Ensure the opencode serve backend is up (spawn if missing).

    Returns True when the backend is answering.  Spawns ``opencode serve``
    as a detached child process bound to ``OPENCODE_SERVE_URL`` — the
    proxy owns its lifecycle so no systemd unit is required.  Never raises.

    On a backend that does NOT own its server (``BACKEND.owns_server`` is
    False — the systemd-owned secure server), this degenerates to a pure
    health probe: no spawn, no config sync, no auth copy and no signal.
    A down secure server returns False and the callers fail with an error
    naming it; the proxy never starts it.

    Config-drift gate (REQ-5): when a serve IS running and the serve
    template (OPCODE_CONFIG_PATH) is newer than the mtime cached at the
    last successful spawn, the serve is recycled via ``_force_recycle_serve``
    and respawned so the edited config actually loads.  An unreadable
    template is treated as no-drift.
    """
    global _serve_config_mtime
    if not BACKEND.owns_server:
        return await _secure_serve_answering()
    mtime = await asyncio.to_thread(_config_mtime)
    if await is_opencode_serve_running():
        if mtime is None:
            return True  # template unreadable — treat as no-drift
        if _serve_config_mtime is not None and mtime > _serve_config_mtime:
            logger.warning(
                "opencode serve config drifted (mtime %.3f > cached %.3f) — "
                "recycling serve", mtime, _serve_config_mtime,
            )
            await _force_recycle_serve()
            # Drain the old listener so the respawn can bind the port.
            for _ in range(4):
                if not await is_opencode_serve_running():
                    break
                await asyncio.sleep(0.25)
        else:
            if _serve_config_mtime is None:
                # Proxy restarted while the serve survived: adopt the
                # current template as the baseline, no recycle.
                _serve_config_mtime = mtime
            return True
    return await _spawn_serve(mtime)



def _sync_openai_auth_into_serve(serve_env: dict[str, str]) -> None:
    """Refresh the serve's openai OAuth access token from the TUI/CLI
    auth.json and copy the auth file into the serve's ISOLATED data
    home (the chatgpt-headless route the TUI/CLI use — the env apiKey
    alone is rejected: the ChatGPT-Plus OAuth token lacks the
    api.responses.write scope).  Sync file I/O: callers run it via
    asyncio.to_thread.  Never raises.
    """
    try:
        with open(
            os.path.expanduser("~/.local/share/opencode/auth.json"),
            encoding="utf-8",
        ) as _af:
            _auth = json.load(_af)
        _tok = (_auth.get("openai") or {}).get("access")
        if _tok:
            serve_env["OPENAI_API_KEY"] = _tok
        # The serve's data home is ISOLATED (XDG_DATA_HOME ->
        # serve-config), so its auth lookup would miss the TUI/CLI's
        # auth.json (OAuth tokens) and fall back to the env apiKey
        # (which the API rejects).  Copy the auth file into the serve's
        # data home so the serve resolves the openai provider through
        # the SAME chatgpt-headless OAuth route the TUI/CLI use
        # (2026-08-09).
        _serve_auth = os.path.join(
            OPENCODE_SERVE_CONFIG_DIR, "opencode", "auth.json",
        )
        _src_auth = os.path.expanduser("~/.local/share/opencode/auth.json")
        if os.path.exists(_src_auth):
            os.makedirs(os.path.dirname(_serve_auth), exist_ok=True)
            with open(_src_auth, "rb") as _sf, open(_serve_auth, "wb") as _df:
                _df.write(_sf.read())
            # The copy holds OAuth tokens: enforce the same 0600 the
            # source auth.json carries (open('wb') would create it
            # 0644 under a normal umask — world-readable).
            try:
                os.chmod(_serve_auth, 0o600)
            except OSError:
                pass
    except (OSError, ValueError, TypeError):
        pass

async def _spawn_serve(mtime: Optional[float]) -> bool:
    """Spawn the opencode serve with the serve-scoped config (candidate B).

    Syncs the reduced template into ``OPENCODE_SERVE_CONFIG_DIR`` and
    spawns with ``XDG_CONFIG_HOME`` pointing there, so ONLY the referenced
    plugins load (the rate-limit-fallback plugin; the wedge-prone
    skill-registry / review-result-artifacts / model-variants .ts plugins
    never auto-load — the serve-config dir holds no .ts files).
    ``OPENCODE_SERVE_PURE`` toggles candidate A (``--pure``, no plugins).
    Records ``_serve_config_mtime`` on success so the drift gate can
    compare.  Never raises.
    """
    if not BACKEND.owns_server:
        # Systemd-owned server (secure backend): the proxy never spawns,
        # config-syncs or auth-copies it — the whole spawn cluster is a
        # no-op and ensure_opencode_serve stays a pure health probe.
        return False
    global _serve_config_mtime
    try:
        port = OPENCODE_SERVE_URL.rsplit(":", 1)[-1]
        await asyncio.to_thread(_sync_serve_config)
        serve_log = await asyncio.to_thread(_open_serve_log)
        # The serve inherits a minimal systemd PATH; give it the usual
        # user paths so the fallback plugin's gentle-ai binary resolves.
        serve_env = dict(os.environ)
        serve_env["PATH"] = (
            _machine("LINUXBREW_PREFIX", os.path.expanduser("~/.linuxbrew")) + "/bin:"
            + _machine("LOCAL_BIN_DIR", os.path.expanduser("~/.local/bin")) + ":"
            + _machine("OPENCODE_BIN_DIR", os.path.expanduser("~/.opencode/bin")) + ":"
            + "/usr/local/bin:/usr/bin:/bin"
        )
        # Candidate B: serve-scoped config dir (never ~/.config/opencode).
        serve_env["XDG_CONFIG_HOME"] = OPENCODE_SERVE_CONFIG_DIR
        # The auth sync is sync file I/O: keep it off the event loop
        # (Rule 3 discipline — the config sync and log opens do the same).
        await asyncio.to_thread(_sync_openai_auth_into_serve, serve_env)
        # Full serve isolation (2026-08-09): the serve must NOT share the
        # TUI's data/cache locations.  The shared session DB + plugin
        # caches caused cross-process contention (systematic plugin's
        # models.json cache, session storage churn) and let the serve's
        # stalls poison the TUI and vice versa.  Data (sessions) and cache
        # now live under the serve-config dir, unique to the serve.
        serve_env["XDG_DATA_HOME"] = OPENCODE_SERVE_CONFIG_DIR
        serve_env["XDG_CACHE_HOME"] = os.path.join(OPENCODE_SERVE_CONFIG_DIR, "cache")
        args = [
            OPENCODE_BIN, "serve", "--port", port, "--hostname", "127.0.0.1",
        ]
        if OPENCODE_SERVE_PURE:
            args.append("--pure")  # candidate A fallback: no plugins at all
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=serve_log,
            stderr=serve_log,
            start_new_session=True,
            cwd=OPENCODE_WORKSPACE_DIR,
            env=serve_env,
        )
        logger.info(
            "Opened opencode serve (pid=%s) on %s", proc.pid, OPENCODE_SERVE_URL,
        )
        # Wait briefly for the listener to come up.
        for _ in range(10):
            await asyncio.sleep(0.5)
            if await is_opencode_serve_running():
                _serve_config_mtime = mtime
                return True
    except asyncio.CancelledError:
        # Cancellation must propagate (never swallow it).
        raise
    except OSError as exc:
        logger.error("Failed to spawn opencode serve: %s", exc)
    return False


async def opencode_chat(
    user_text: str,
    *,
    agent: str = OPENCODE_AGENT,
    timeout: float = OPENCODE_SERVE_TIMEOUT,
    system_prompt: str = _BRIDGE_SYSTEM_PROMPT,
    directory: Optional[str] = None,
) -> str:
    """Send a task to headless opencode and return the assistant text.

    Creates a fresh session per call, posts the message (blocking until
    the agent finishes), and concatenates the ``text`` parts.  Proxy-status
    sentinel segments are stripped from the result.  Returns an error
    string on failure (escalation-friendly, never raises).

    While the message POST is in flight the call polls ``GET /permission``
    (``_BLOCKING_PERMISSION_POLL_S`` cadence): permission asks are NOT
    granted (headless callers have no user to relay a question to) — the
    session is aborted and a clear error is returned instead of burning
    the full timeout on a parked agent.  On every
    non-success exit the created session is aborted best-effort so no busy
    zombie is left on the serve.

    A network-class failure after a successful first ensure triggers one
    bounded respawn: the serve is force-recycled, re-ensured, and the whole
    call retried exactly once.  HTTP-status errors and permission aborts
    never retry.
    """
    if not await ensure_opencode_serve():
        return _serve_unreachable_message()
    async with httpx.AsyncClient() as client:
        text, network_failed = await _opencode_chat_attempt(
            client, user_text, agent=agent, timeout=timeout,
            system_prompt=system_prompt,
            directory=directory,
        )
        if not network_failed:
            return text
        # The serve died mid-call (network-class failure after a successful
        # ensure): recycle once, re-ensure, and retry the whole call once.
        # No loop — a failed re-ensure or failed retry returns its error.
        await _force_recycle_serve("blocking-path respawn")
        if not await ensure_opencode_serve():
            return _serve_unreachable_message()
        text, _ = await _opencode_chat_attempt(
            client, user_text, agent=agent, timeout=timeout,
            system_prompt=system_prompt,
            directory=directory,
        )
        return text


async def _opencode_chat_attempt(
    client: httpx.AsyncClient,
    user_text: str,
    *,
    agent: str,
    timeout: float,
    system_prompt: str,
    directory: Optional[str] = None,
) -> tuple[str, bool]:
    """One blocking attempt: create session, POST with permission polling,
    extract text.

    Returns ``(result_string, network_failed)``.  ``network_failed`` is True
    ONLY for the network-class paths (httpx.HTTPError/OSError/ValueError) so
    the caller can trigger the bounded respawn; HTTP-status errors, missing
    ids, empty responses, and permission aborts return False.  Never raises.
    """
    try:
        # ---- 1. Create a fresh session --------------------------------
        resp = await BACKEND.create_session(
            client, directory=directory or OPENCODE_BRIDGE_DIRECTORY,
        )
        if resp.status_code != 200:
            return f"[OpenCode Bridge Error: session HTTP {resp.status_code}]", False
        session_id = resp.json().get("id")
        if not session_id:
            return "[OpenCode Bridge Error: no session id returned.]", False

        # ---- 2. Post the message, watching for permission gates ------
        # The POST runs as a task so the poller below can watch GET
        # /permission concurrently (httpx clients are concurrency-safe).
        post_task = asyncio.create_task(
            BACKEND.send_message(
                client, session_id,
                agent=agent, system_prompt=system_prompt, user_text=user_text,
                timeout=timeout,
            )
        )
        try:
            last_poll = time.monotonic()
            while True:
                if post_task.done():
                    # Raises httpx/OSError on transport failure.
                    resp = post_task.result()
                    break
                if time.monotonic() - last_poll >= _BLOCKING_PERMISSION_POLL_S:
                    last_poll = time.monotonic()
                    perm = await _detect_pending_permission(client, session_id)
                    if perm is not None:
                        question = await _handle_permission_event(
                            client, session_id, perm, {},
                        )
                        # A non-answering backend (secure) leaves ``question``
                        # None: the prompt is resolved out-of-band, so the
                        # loop keeps polling and never aborts the session.
                        if question:
                            # Interactive-mode WRITE/git ask on a headless
                            # blocking call: no user can answer, and the
                            # queue-worker path must never grant writes
                            # unprompted — abort and surface a clear error
                            # instead of streaming keepalives or granting.
                            await _abort_session_best_effort(client, session_id)
                            return (
                                "[OpenCode Bridge Error: agent requested write "
                                "permission — headless escalation cannot relay "
                                "questions; session aborted.]",
                                False,
                            )
                await asyncio.sleep(min(0.25, _BLOCKING_PERMISSION_POLL_S))
        except (httpx.HTTPError, OSError) as exc:
            await _abort_session_best_effort(client, session_id)
            return f"[OpenCode Bridge Network Error: {str(exc)}]", True
        finally:
            if not post_task.done():
                post_task.cancel()
        if resp.status_code != 200:
            await _abort_session_best_effort(client, session_id)
            return f"[OpenCode Bridge Error: message HTTP {resp.status_code}]", False
        data = resp.json()
        parts = data.get("parts", [])
        chunks = [
            str(p.get("text") or "")
            for p in parts
            if p.get("type") == "text" and p.get("text")
        ]
        return (
            _strip_proxy_status_text("".join(chunks)).strip()
            or "[OpenCode Bridge Error: empty response.]",
            False,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout,
            httpx.RemoteProtocolError, OSError) as exc:
        return f"[OpenCode Bridge Network Error: {str(exc)}]", True
    except (httpx.HTTPError, ValueError) as exc:
        # ReadTimeout / malformed body: the serve may be healthy and the
        # agent still working.  NEVER recycle on these — the respawn would
        # SIGTERM the serve and destroy every concurrent session.
        return f"[OpenCode Bridge Error: {str(exc)}]", False


async def opencode_chat_stream(
    user_text: str,
    *,
    agent: str = OPENCODE_AGENT,
    session_map: Optional[dict[str, str]] = None,
    session_key: Optional[str] = None,
    pending_permissions: Optional[dict[str, tuple[str, bool]]] = None,
    system_prompt: str = _BRIDGE_SYSTEM_PROMPT,
    timeout: float = OPENCODE_SERVE_TIMEOUT,
    directory: Optional[str] = None,
) -> AsyncIterator[tuple[str, str]]:
    """Stream a task through headless opencode, yielding assistant content live.

    Uses ``prompt_async`` (no-wait send) + the ``/event`` SSE bus instead of
    the blocking message POST, so the caller sees the agent's output as it is
    generated rather than one blob after minutes.  When ``session_map`` +
    ``session_key`` are given, the opencode session is pinned per conversation:
    follow-ups reuse the SAME agent session (it keeps its tool state and
    remembers what it built), and a clarifying question yields
    ("question", text) and stops — the session stays pinned so the next
    request can post the user's answer and continue.

    Yields (kind, text) tuples: "text" → assistant content, "reasoning" →
    thinking (separate delta field), "status" → sentinel-prefixed feedback,
    "question" → the agent is waiting for user input (stop streaming).
    On failure yields a status tuple (never raises).
    """
    # Recycle BEFORE ensure: the recycle SIGTERMs the serve (low memory or
    # uptime > _SERVE_RECYCLE_AFTER_S) and never respawns, so running it
    # after ensure would break the next request with a spurious ConnectError.
    # The ensure below respawns a fresh serve when a recycle fired.
    await _recycle_serve_if_low_memory()
    if not await ensure_opencode_serve():
        yield ("status", _serve_unreachable_message())
        return
    # Rule 6: pending-permission state lives on app.state in routes.py and is
    # threaded in; standalone callers (scripts/tests) fall back to a fresh
    # per-call dict.
    pending_permissions = (
        pending_permissions if pending_permissions is not None else {}
    )
    session_id: Optional[str] = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
        try:
            session_id = (
                session_map.get(session_key)
                if session_map and session_key else None
            )
            # Set when the user's message answered a pending question (the
            # resume path posts it as a question reply, so no prompt POST).
            skip_prompt = False
            # A pending question request owned by the pinned session (the
            # agent's question tool is waiting on the user).
            pending_question_id: Optional[str] = None
            pending_question_count = 0
            if session_id:
                pending_question_id, pending_question_count = await _find_pending_question(
                    client, session_id,
                )
            if session_id:
                # Follow-up in the same conversation: reuse the pinned agent
                # session so it retains its tool state and context.
                # A session the serve no longer LISTS is stale: the serve was
                # recycled (or the session was dropped), so posting to the
                # pinned id would fail every retry — drop the pin and start
                # fresh.  Only a SUCCESSFUL existence probe may trigger the
                # drop; a transport-error fetch keeps the pin conservatively.
                # A session's real existence must NOT be inferred from
                # /session/status: the serve's status map only holds BUSY
                # sessions — idle sessions are deleted from it on
                # completion (upstream SessionStatus.set deletes on idle,
                # observed live 2026-08-13 on 1.18.18).  A healthy idle
                # pinned session is therefore absent from the map, and
                # reading that absence as "session gone" dropped EVERY
                # pin on the follow-up: each "continue" started a fresh
                # session and the agent lost the task context
                # ('runs a few commands and then stops').  Existence is
                # probed with GET /session/<id> (200 = live, 404 =
                # genuinely gone — serve recycled or dropped).
                fetch_ok = False
                exists = True
                try:
                    get_resp = await BACKEND.session_exists(client, session_id)
                    fetch_ok = True
                    exists = get_resp.status_code == 200
                except (httpx.HTTPError, ValueError):
                    pass
                if fetch_ok and not exists:
                    # Stale pin: a successful existence probe shows the
                    # session is gone — no abort POST is needed (aborting
                    # a nonexistent session adds noise).
                    logger.info(
                        "pinned session %s no longer on serve — dropping pin and starting fresh",
                        session_id[:16],
                    )
                    pending_permissions.pop(session_id, None)
                    if session_map is not None and session_key:
                        session_map.pop(session_key, None)
                    session_id = None
            # True when the session_id came from the session map (a pin),
            # NOT from a fresh POST /session below — only resumed sessions
            # seed the poll-state with pre-existing history.
            resumed = session_id is not None
            user_mids: set[str] = set()
            asst_mid: Optional[str] = None
            text_lens: dict[str, int] = {}
            tool_state: dict[str, str] = {}
            # Tool parts already surfaced as a 🔧 status by the polling
            # announce (bus path) — never re-announce them.
            announced_tool_pids: set[str] = set()
            pending_done = False
            session_busy = True  # assume working until a status event says idle
            seen_question_pids: set[str] = set()
            # Permission ids already reported as "waiting" under a backend
            # that does not answer permissions (the secure server) — dedupes
            # the status so a poll every tick never repeats the line.
            announced_permission_ids: set[str] = set()
            running_question_times: list[float] = []
            if resumed and session_id is not None:
                # Seed part state for the resumed conversation so the
                # polling fallback never replays history or re-surfaces a
                # stale question that kills the stream.  Best-effort.  The
                # seeding also records running-question start times (the
                # orphan check below uses them — no extra message-list GET,
                # so the seeded read stays the FIRST one) and returns a
                # rough context-token estimate for the bridge-level reset:
                # an over-cap session cannot shed its own history, so the
                # next resume starts fresh (durable state lives in the
                # artifact files).
                est = await _seed_resumed_session_state(
                    client, session_id, user_mids, text_lens,
                    tool_state, seen_question_pids, running_question_times,
                )
                if est > _SESSION_CONTEXT_CAP_TOKENS:
                    logger.info(
                        "pinned session %s over the context cap (%d tokens) "
                        "— starting fresh", session_id[:16], est,
                    )
                    if session_map is not None and session_key:
                        session_map.pop(session_key, None)
                    session_id = None
                    resumed = False
            # A pending QUESTION from the previous turn: the agent is
            # waiting on the user (the question tool parks the current
            # step).  The user's message IS the answer — post it to the
            # serve's question reply endpoint so the tool completes and
            # the agent continues with the answer in context.  Posting
            # the answer as a new prompt instead leaves the step
            # blocked forever (observed live 2026-08-14: a
            # 'notification channel' question stayed running 8+ minutes
            # across 5 user messages that all only got keepalives).
            if session_id:
                if pending_question_id and pending_question_count:
                    await _post_question_answer(
                        client, session_id, pending_question_id, user_text,
                        pending_question_count,
                    )
                    skip_prompt = True
                elif (
                    running_question_times
                    and time.time() * 1000 - max(running_question_times) > 60_000
                ):
                    # The question part is parked in "running" but the
                    # serve holds NO pending request for it — the
                    # question is orphaned (serve recycled or the ask
                    # was lost) and can never be answered.  Abort and
                    # start fresh; the resilient wrapper retries the
                    # full task.
                    logger.warning(
                        "orphaned pending question in session %s — aborting and starting fresh",
                        session_id[:16],
                    )
                    await _abort_stream_session_best_effort(
                        client, session_id,
                        session_map=session_map, session_key=session_key,
                        pending_permissions=pending_permissions,
                    )
                    session_id = None
            if not session_id:
                resp = await BACKEND.create_session(
                    client, directory=directory or OPENCODE_BRIDGE_DIRECTORY,
                )
                if resp.status_code != 200:
                    yield ("status", f"[OpenCode Bridge Error: session HTTP {resp.status_code}]")
                    return
                session_id = resp.json().get("id")
                if not session_id:
                    yield ("status", "[OpenCode Bridge Error: session created without id]")
                    return
                if session_map is not None and session_key:
                    session_map[session_key] = session_id
            # Open the event bus BEFORE sending the message: the bus is
            # fire-and-forget (no replay), so connecting after prompt_async
            # misses the early events (user message, assistant start, first
            # reasoning/tool parts) and the stream would look empty.
            async with client.stream("GET", f"{OPENCODE_SERVE_URL}/event") as ev:
                async_resp = None
                if not skip_prompt:
                    async_resp = await BACKEND.send_prompt(
                        client, session_id,
                        agent=agent, system_prompt=system_prompt,
                        user_text=user_text,
                    )
                if async_resp is not None and async_resp.status_code != 204:
                    # Session was created and pinned before this POST — do
                    # not leak it on a failed prompt.
                    await _abort_stream_session_best_effort(
                        client, session_id,
                        session_map=session_map, session_key=session_key,
                        pending_permissions=pending_permissions,
                    )
                    yield ("status", f"[OpenCode Bridge Error: prompt HTTP {async_resp.status_code}]")
                    return

                # An agentic session can produce several assistant messages
                # (reasoning/tool step, then a final summary).  Completion =
                # a finished step AND the session idle: a step-finish sets
                # pending_done, a ``session.status`` event tells us whether
                # the session is still busy.  Quiet alone is NOT enough — a
                # multi-step agent can pause >3s between steps while still
                # busy (long tool runs, model generation), and returning on
                # quiet mid-work cut the stream with the session still
                # running.  Only complete when the session went idle.
                ev_iter = ev.aiter_lines()
                started = time.monotonic()
                bus_last_wedge_check = time.monotonic()
                # First quiet timeout announces immediately (due from the
                # start), then every _ANNOUNCE_CHECK_INTERVAL_S.
                bus_last_announce_check = time.monotonic() - _ANNOUNCE_CHECK_INTERVAL_S
                # Multi-step turns report idle briefly between steps: the
                # stream returns only after this quiet window with no new
                # content (see _IDLE_GRACE_S).
                idle_grace_started: Optional[float] = None
                while True:
                    # Bounded total duration: a hung agent tool (e.g. a
                    # package-manager command stuck on a lock) leaves the
                    # session "busy" forever; cap the wait, abort the
                    # session, and surface a clear error instead of
                    # streaming keepalives indefinitely.
                    if time.monotonic() - started > timeout:
                        logger.error(
                            "opencode bridge timeout after %.0fs — aborting session %s",
                            OPENCODE_SERVE_TIMEOUT, session_id[:16],
                        )
                        await _abort_stream_session_best_effort(
                            client, session_id,
                            session_map=session_map, session_key=session_key,
                            pending_permissions=pending_permissions,
                        )
                        yield ("status", "[OpenCode Bridge Error: timed out waiting for the agent]",)
                        return
                    try:
                        line = await asyncio.wait_for(
                            anext(ev_iter),
                            timeout=_EVENT_FINAL_TIMEOUT if pending_done
                            else _EVENT_QUIET_TIMEOUT,
                        )
                    except StopAsyncIteration:
                        # The /event SSE bus closed (the serve closes idle
                        # connections) but the session may still be working.
                        # Fall back to polling the session's message list
                        # until it completes or the total timeout fires.
                        # The event-bus keepalive path is gone here, so a
                        # genuinely busy but quiet agent (a long tool run
                        # with no output) would send the client ZERO chunks
                        # and trip its stall detector (nanobot kills streams
                        # silent for 90s).  Track the last client-visible
                        # emission and emit "still working" status chunks on
                        # quiet, mirroring the event-bus phase.
                        polling_started = time.monotonic()
                        last_emit = polling_started
                        last_wedge_check = time.monotonic()
                        # The serve's /session/status reports "busy" FOREVER
                        # for prompt_async sessions even after the agent
                        # finished (observed live 2026-08-07: a fully
                        # completed session never flipped to "idle"), so the
                        # st == "idle" check below alone never returns and
                        # the bridge burns the full OPENCODE_SERVE_TIMEOUT.
                        # Completion is detectable from the message list: a
                        # step-finish with no new content across the idle
                        # grace window means the agent is done (multi-step
                        # turns pause between steps — see _IDLE_GRACE_S).
                        idle_since: Optional[float] = None
                        while True:
                            if time.monotonic() - started > timeout:
                                logger.error(
                                    "opencode bridge timeout during polling — aborting session %s",
                                    session_id[:16],
                                )
                                await _abort_stream_session_best_effort(
                                    client, session_id,
                                    session_map=session_map, session_key=session_key,
                                    pending_permissions=pending_permissions,
                                )
                                yield ("status", "[OpenCode Bridge Error: timed out waiting for the agent]")
                                return
                            cycle_content = False
                            async for delta in _poll_session_deltas(
                                client, session_id, user_mids, text_lens, tool_state,
                                seen_question_pids,
                            ):
                                if delta[0] == "question":
                                    yield delta
                                    return
                                if delta[0] == "_step_finish":
                                    pending_done = True
                                    continue
                                yield delta
                                cycle_content = True
                                last_emit = time.monotonic()
                                idle_since = None  # the agent is working
                            # A RUNNING tool in the newest assistant
                            # message means the agent is still working — a
                            # freshly started tool produces no content delta
                            # until it emits output, so the quiet-cycles
                            # logic below would otherwise declare the session
                            # "done" seconds after a tool starts (regression
                            # 2026-08-07: stream ended 4s into a wedged
                            # bash run, stranding its pending permission).
                            if pending_done and not cycle_content:
                                still_running = False
                                try:
                                    msg_resp = await BACKEND.fetch_messages(client, session_id)
                                    if msg_resp.status_code == 200:
                                        msgs = msg_resp.json()
                                        for m in msgs:
                                            if (m.get("info") or {}).get("role") != "assistant":
                                                continue
                                            for p in m.get("parts") or []:
                                                if p.get("type") == "tool" and p.get("tool") != "question":
                                                    st = p.get("state") or {}
                                                    if st.get("status") == "running" and not st.get("output"):
                                                        still_running = True
                                                        break
                                            if still_running:
                                                break
                                except (httpx.HTTPError, OSError, ValueError):
                                    pass
                                if still_running:
                                    idle_since = None
                                    # Do not emit "done" — the agent is mid
                                    # tool; keep the stream alive.
                                    cycle_content = True
                            # Multi-step agents pause between steps; a
                            # finished session shows a step-finish then goes
                            # quiet.  UNIFIED idle grace: a finished step OR
                            # an idle session, with zero new content across
                            # the grace window = done (the model's inter-step
                            # generation takes 5-40s on this hardware, and
                            # the serve can briefly report idle between
                            # steps).  New content or a busy status resets
                            # the timer.
                            try:
                                st_resp = await BACKEND.fetch_session_status(client)
                                st = (st_resp.json().get(session_id) or {}).get("type")
                            except (httpx.HTTPError, ValueError):
                                st = None
                            if (pending_done or st == "idle") and not cycle_content:
                                if idle_since is None:
                                    idle_since = time.monotonic()
                                elif time.monotonic() - idle_since > _IDLE_GRACE_S:
                                    # The agent looks done, but it may be
                                    # parked on a permission the bridge
                                    # has not relayed yet.  Resolve it BEFORE
                                    # returning: surface the question so the
                                    # next request answers it.
                                    pending_perm = await _detect_pending_permission(client, session_id)
                                    if pending_perm:
                                        question = await _handle_permission_event(
                                            client, session_id, pending_perm,
                                            pending_permissions,
                                        )
                                        if question:
                                            yield ("question", question)
                                            return
                                        if BACKEND.answers_permissions:
                                            return
                                        # Non-answering backend: the prompt is
                                        # resolved out-of-band, so the parked
                                        # session must NOT look done.  Report
                                        # the wait once and restart the idle
                                        # grace so polling continues.
                                        status = _waiting_permission_status(
                                            pending_perm, announced_permission_ids,
                                        )
                                        if status:
                                            yield ("status", status)
                                        idle_since = time.monotonic()
                                    else:
                                        return
                            else:
                                idle_since = None
                            # Real agent work with no new parts (long bash
                            # run, model generation) must not read as a
                            # dead stream: emit a keepalive after quiet —
                            # UNLESS the idle grace is counting (the agent
                            # finished; the stream is only waiting out the
                            # grace — see the bus-path guard).
                            if (
                                time.monotonic() - last_emit > _EVENT_QUIET_TIMEOUT
                                and not (
                                    (pending_done or st == "idle")
                                    and not cycle_content
                                )
                            ):
                                yield (
                                    "status",
                                    "⏳ still working… "
                                    f"(polling {time.monotonic() - polling_started:.0f}s)",
                                )
                                last_emit = time.monotonic()
                            # A tool part wedged in "running" with no output
                            # (the runner never executed it) would hold the
                            # session busy forever.  Detect it, abort the
                            # session, drop the pin, recycle the serve, and
                            # surface a clear error instead of streaming
                            # keepalives indefinitely.  First check for a
                            # PENDING PERMISSION — a write waiting for the
                            # user looks identical to a wedge in the message
                            # list (status=running, no output), and must be
                            # relayed instead of killed.
                            if time.monotonic() - last_wedge_check >= _WEDGE_CHECK_INTERVAL_S:
                                last_wedge_check = time.monotonic()
                                pending_perm = await _detect_pending_permission(client, session_id)
                                if pending_perm:
                                    question = await _handle_permission_event(
                                        client, session_id, pending_perm,
                                        pending_permissions,
                                    )
                                    if question:
                                        yield ("question", question)
                                        return
                                    status = _waiting_permission_status(
                                        pending_perm, announced_permission_ids,
                                    )
                                    if status:
                                        yield ("status", status)
                                    continue
                                if await _detect_wedged_tool(client, session_id):
                                    await _abort_stream_session_best_effort(
                                        client, session_id,
                                        session_map=session_map, session_key=session_key,
                                        pending_permissions=pending_permissions,
                                    )
                                    # NOTE: never kill the serve here.  The
                                    # serve hosts OTHER sessions (concurrent
                                    # cycles); recycling it for one wedged
                                    # tool destroys every live session (seen
                                    # 2026-08-08: a wedged bash in one cycle
                                    # SIGTERMed the serve mid-other-cycle).
                                    # The session abort frees the tool runner.
                                    yield ("status", "[OpenCode Bridge Error: agent tool runner wedged — session aborted. Please retry.]")
                                    return
                            await asyncio.sleep(1.0)
                    except asyncio.TimeoutError:
                        if pending_done and not session_busy:
                            # A finished step + idle session looks done —
                            # but multi-step turns (work-continuity) pause
                            # between steps while the model generates the
                            # next action, and the serve can briefly report
                            # idle between assistant messages.  Return only
                            # after the idle grace with NO new content;
                            # the catch-up poll below delivers the next
                            # step's parts and resets the grace when the
                            # agent continues.
                            if idle_grace_started is None:
                                idle_grace_started = time.monotonic()
                            elif time.monotonic() - idle_grace_started > _IDLE_GRACE_S:
                                return
                        else:
                            idle_grace_started = None
                        # A parked WRITE tool never emits permission.updated
                        # (opencode 1.18.15 write/edit tools omit the SSE
                        # event; only bash emits it), so the event-bus path
                        # must poll GET /permission too.  A write waiting
                        # for the user looks identical to a wedge in the
                        # message list (status=running, no output) — relay
                        # it as a question, never kill it.
                        if time.monotonic() - bus_last_wedge_check >= _WEDGE_CHECK_INTERVAL_S:
                            bus_last_wedge_check = time.monotonic()
                            pending_perm = await _detect_pending_permission(client, session_id)
                            if pending_perm:
                                question = await _handle_permission_event(
                                    client, session_id, pending_perm,
                                    pending_permissions,
                                )
                                if question:
                                    yield ("question", question)
                                    return
                                status = _waiting_permission_status(
                                    pending_perm, announced_permission_ids,
                                )
                                if status:
                                    yield ("status", status)
                                continue
                            if await _detect_wedged_tool(client, session_id):
                                await _abort_stream_session_best_effort(
                                    client, session_id,
                                    session_map=session_map, session_key=session_key,
                                    pending_permissions=pending_permissions,
                                )
                                # NOTE: never kill the serve here — see the
                                # polling-wedge path above (2026-08-08: the
                                # serve hosts concurrent sessions; killing it
                                # destroys them all).
                                yield ("status", "[OpenCode Bridge Error: agent tool runner wedged — session aborted. Please retry.]")
                                return
                        # Command-level visibility even when the SSE bus
                        # drops running-state updates: announce the
                        # currently running tool from the message list, on
                        # its own faster cadence (the 10s wedge timer would
                        # miss tools that finish within a cycle).
                        if time.monotonic() - bus_last_announce_check >= _ANNOUNCE_CHECK_INTERVAL_S:
                            bus_last_announce_check = time.monotonic()
                            announce = await _current_running_tool(
                                client, session_id, tool_state, announced_tool_pids,
                            )
                            if announce:
                                yield ("status", announce)
                        # Catch-up poll: the serve's SSE drops events
                        # (upstream #35066 — text and tool deltas vanish
                        # while the bus stays open), so the user missed the
                        # agent's narration until the next poll/continue.
                        # The message list holds the parts; poll on quiet
                        # timeouts (deduped by part id/length) and deliver
                        # them live.  New content also keeps the stream
                        # open while the agent keeps working.
                        async for delta in _poll_session_deltas(
                            client, session_id, user_mids, text_lens, tool_state,
                            seen_question_pids,
                        ):
                            if delta[0] == "question":
                                yield delta
                                return
                            if delta[0] == "_step_finish":
                                pending_done = True
                                continue
                            yield delta
                            idle_grace_started = None  # the agent is working
                        # Keep the client connection alive during long tool
                        # phases (and show the agent is still working).  The
                        # idle-grace tail is NOT work — the agent finished
                        # and the stream is waiting out the 45s grace — so
                        # stop the "still working" keepalives there (the
                        # frontend's 90s stall-killer is longer than the
                        # grace).
                        if not (pending_done and not session_busy):
                            yield ("status", "⏳ still working…")
                        continue
                    if not line.startswith("data: "):
                        continue
                    try:
                        evt = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    props = evt.get("properties") or {}
                    if props.get("sessionID") != session_id:
                        continue
                    etype = evt.get("type")
                    if etype == "permission.updated":
                        # The serve asked for a permission decision.  The
                        # external_directory gate fires when a bash tool
                        # touches paths outside the session workspace; the
                        # bash gate fires for git ask-rules (commit/push/
                        # reset/rebase).  A headless serve auto-rejects
                        # silently and leaves the tool stuck "running" (the
                        # bridge wedges).  A permission ask is relayed as a
                        # question; the stream stops so the USER decides on
                        # the next request.
                        # (the session stays pinned).
                        if props.get("type") in _RELAYED_PERMISSION_TYPES:
                            question = await _handle_permission_event(
                                client, session_id, props, pending_permissions,
                            )
                            if question:
                                yield ("question", question)
                                return
                            status = _waiting_permission_status(
                                props, announced_permission_ids,
                            )
                            if status:
                                yield ("status", status)
                            continue
                        # Other permission types: no existing branch matches,
                        # so fall through to the rest of the loop body.
                    if etype == "session.status":
                        # Track whether the agent is still working:
                        # properties.status.type is "busy" | "idle".
                        st = (props.get("status") or {}).get("type")
                        if st == "busy":
                            session_busy = True
                        elif st == "idle":
                            session_busy = False
                        continue
                    if etype == "message.updated":
                        info = props.get("info") or {}
                        mid = info.get("id")
                        role = info.get("role")
                        if role == "user" and mid:
                            user_mids.add(mid)
                        elif role == "assistant" and asst_mid is None:
                            asst_mid = mid
                            if info.get("error"):
                                # The agent errored out — clean up the
                                # session instead of leaking it.
                                await _abort_stream_session_best_effort(
                                    client, session_id,
                                    session_map=session_map, session_key=session_key,
                                    pending_permissions=pending_permissions,
                                )
                                yield ("status", f"[OpenCode Bridge Error: {info['error']}]")
                                return
                    elif etype == "message.part.updated":
                        part = props.get("part") or {}
                        # Accept parts from ANY assistant message; only
                        # user-message parts are excluded so the echoed
                        # prompt never streams back.
                        if part.get("messageID") in user_mids:
                            continue
                        async for delta in _yield_part_deltas(
                            part, text_lens, tool_state, session_id, client,
                            seen_question_pids,
                        ):
                            if delta[0] == "_step_finish":
                                pending_done = True
                            elif delta[0] == "question":
                                yield delta
                                return
                            else:
                                yield delta
        except (httpx.HTTPError, OSError, ValueError) as exc:
            if session_id:
                # A transient mid-stream failure must not strand the agent
                # (possibly still running) with the pin pointing at it.
                await _abort_stream_session_best_effort(
                    client, session_id,
                    session_map=session_map, session_key=session_key,
                    pending_permissions=pending_permissions,
                )
            yield ("status", f"[OpenCode Bridge Network Error: {str(exc)}]")


async def _find_pending_question(
    client: httpx.AsyncClient,
    session_id: str,
) -> tuple[Optional[str], int]:
    """Look up a pending question request owned by ``session_id``.

    The agent's question tool parks the session's current step until the
    user answers (POST /question/<id>/reply).  The serve keeps pending
    question requests in memory and lists them at GET /question; the
    entry carries the request id needed for the reply.  Never raises;
    returns (request_id, question_count) or (None, 0).
    """
    try:
        resp = await BACKEND.fetch_questions(client)
        if resp.status_code != 200:
            return None, 0
        for entry in resp.json():
            if entry.get("sessionID") == session_id:
                qs = entry.get("questions") or []
                return str(entry.get("id") or ""), len(qs)
    except (httpx.HTTPError, OSError, ValueError):
        pass
    return None, 0


async def _post_question_answer(
    client: httpx.AsyncClient,
    session_id: str,
    question_id: str,
    answer: str,
    question_count: int,
) -> bool:
    """POST the user's answer to the serve's question reply endpoint.

    The question tool completes with the answer and the agent continues
    with it in context.  Returns True when the serve accepted the reply.
    Never raises — the backend logs and returns False on transport errors
    (see ``OpenCodeBackend.reply_question``).
    """
    return await BACKEND.reply_question(
        client, session_id, question_id, answer, question_count,
    )




async def opencode_chat_stream_resilient(
    user_text: str,
    **kwargs: Any,
) -> AsyncIterator[tuple[str, str]]:
    """``opencode_chat_stream`` with one automatic retry on a fresh serve.

    The headless serve's tool runner fails at a high rate (upstream defect
    family anomalyco/opencode #35066: glob/grep/read error or wedge, and a
    tool error can kill the session processing silently).  When the first
    attempt ends in a bridge failure WITHOUT delivering any assistant text
    or question, the serve is recycled (fresh runner) and the FULL task is
    retried once — the same bounded-retry pattern the SDD cycle driver
    uses.  A stream that delivered text or ended in a question is never
    retried (the agent produced output or is waiting for the user); a
    failed attempt that already delivered text surfaces its error as-is so
    the partial result is never duplicated.  Never raises; yields (kind,
    text) tuples exactly like the underlying stream.
    """
    attempt1: list[tuple[str, str]] = []
    failed: Optional[str] = None
    delivered = False  # any text or question surfaced to the caller
    async for kind, payload in opencode_chat_stream(user_text, **kwargs):
        if kind in ("text", "question"):
            delivered = True
            for k, p in attempt1:
                yield k, p
            attempt1.clear()
            yield kind, payload
            if kind == "question":
                return  # agent is waiting on the user — never retry
            continue
        if isinstance(payload, str) and payload.startswith("[OpenCode Bridge"):
            failed = payload
        attempt1.append((kind, payload))
    if failed and not delivered:
        # The first attempt died before producing any output: recycle the
        # serve (its tool runner is suspect) and retry the full task once.
        logger.warning(
            "opencode bridge attempt failed (%s) — retrying once on a fresh serve",
            failed[:80],
        )
        yield (
            "status",
            "[OpenCode Bridge: attempt failed — retrying once on a fresh serve…]\n",
        )
        await _force_recycle_serve("plain bridge auto-retry")
        async for kind, payload in opencode_chat_stream(user_text, **kwargs):
            yield kind, payload
        return
    for k, p in attempt1:
        yield k, p


# ---------------------------------------------------------------------------
# Escalation (mirrors llm.openrouter_cloud_escalation)
# ---------------------------------------------------------------------------

async def opencode_escalation(stage: int, prompt: str) -> str:
    """Fallback for the queue worker when local tiers are exhausted.

    Directs the user prompt to the opencode gentle-orchestrator agent
    OpenRouter.  ``stage`` is informational (passed through to logging).

    Conservative by design: this headless worker path never grants a
    permission; a permission ask aborts the session with a clear error (no
    user is present to relay the question).
    """
    logger.info("OpenCode escalation (stage=%d): %r", stage, prompt[:200])
    return await opencode_chat(prompt, agent=OPENCODE_AGENT)



async def _yield_part_deltas(
    part: dict[str, Any],
    text_lens: dict[str, int],
    tool_state: dict[str, str],
    session_id: str,
    client: httpx.AsyncClient,
    seen_question_pids: Optional[set[str]] = None,
) -> AsyncIterator[tuple[str, str]]:
    """Yield stream deltas for one opencode part (shared by the event bus
    and the polling fallback).

    Yields (kind, text): "text" → assistant content, "reasoning" → thinking,
    "status" → tool progress feedback, "question" → the agent is asking the
    user (caller must stop), "_step_finish" → a step completed (caller tracks
    pending_done).  Never raises.
    """
    pid = str(part.get("id") or "")
    ptype = part.get("type")
    if ptype == "step-finish":
        yield ("_step_finish", "")
        return
    if ptype == "text":
        text = str(part.get("text") or "")
        prev = text_lens.get(pid, 0)
        if len(text) > prev:
            text_lens[pid] = len(text)
            if text[prev:].strip():
                yield ("text", text[prev:])
        return
    if ptype == "reasoning":
        text = str(part.get("text") or "")
        prev = text_lens.get(pid, 0)
        if len(text) > prev:
            text_lens[pid] = len(text)
            # Announce thinking ONCE per part (first text only) instead of
            # streaming the raw reasoning tokens — the user wants to know
            # the agent is thinking without dumping the chain-of-thought.
            if prev == 0 and text[prev:].strip():
                yield ("status", "🧠 thinking…\n")
        return
    if ptype == "tool":
        name = str(part.get("tool") or "")
        if name == "question":
            # Seeded history — suppress the stale question BEFORE the
            # fetch-retry loop: the part existed before this request, so
            # re-yielding it would stop the stream with the user's old
            # question instead of streaming the new turn.
            if seen_question_pids and pid in seen_question_pids:
                return
            # The agent is asking the user.  The event part often omits the
            # input; fetch the persisted part to read state.input.questions[].
            # A single question OR a MULTI-GROUP preflight (SDD Session
            # Preflight asks Pace/Artifacts/PRs/Review in one call) must be
            # relayed losslessly: every group in order, with headers and
            # option labels.
            qtext = ""
            opts: list[str] = []
            multi: list[tuple[str, str, list[str]]] = []
            state = part.get("state") or {}
            inp = state.get("input") if isinstance(state, dict) else None
            if isinstance(inp, dict):
                questions = inp.get("questions")
                if isinstance(questions, list) and questions:
                    for q in questions:
                        if not isinstance(q, dict):
                            continue
                        header = str(q.get("header") or "")
                        body = str(q.get("question") or "")
                        qopts = [str(o.get("label") or "") for o in (q.get("options") or [])
                                 if isinstance(o, dict) and o.get("label")]
                        multi.append((header, body, qopts))
                    if multi:
                        qtext = multi[0][1]
                        opts = multi[0][2]
                else:
                    qtext = str(inp.get("question") or "")
            else:
                qtext = str(inp or "")
            if not qtext and not multi:
                # The serve persists question parts lazily: the event carries
                # the tool marker immediately but state.input (the actual
                # questions) only lands 5-8s later.  Wait long enough for
                # persistence (30 x 0.4s = 12s) before giving up, so the
                # SDD preflight's full multi-group envelope is relayed
                # losslessly rather than collapsing to "Could you clarify?".
                for _attempt in range(30):
                    fetched: bool = False
                    try:
                        # Preferred: the part's own message (carries input).
                        mid = part.get("messageID") or ""
                        if mid:
                            msg_resp = await BACKEND.fetch_message(client, session_id, mid)
                            if msg_resp.status_code == 200:
                                fetched = True
                                for p2 in (msg_resp.json().get("parts") or []):
                                    if p2.get("id") == pid and isinstance(p2.get("state"), dict):
                                        i2 = (p2.get("state") or {}).get("input")
                                        if isinstance(i2, dict):
                                            qs = i2.get("questions")
                                            if isinstance(qs, list) and qs:
                                                for q in qs:
                                                    if not isinstance(q, dict):
                                                        continue
                                                    header = str(q.get("header") or "")
                                                    body = str(q.get("question") or "")
                                                    qopts = [str(o.get("label") or "") for o in (q.get("options") or [])
                                                             if isinstance(o, dict) and o.get("label")]
                                                    multi.append((header, body, qopts))
                                                if multi:
                                                    qtext = multi[0][1]
                                                    opts = multi[0][2]
                                            else:
                                                qtext = str(i2.get("question") or "")
                    except (httpx.HTTPError, ValueError):
                        pass
                    if not qtext and not multi:
                        # Fallback: the serve omits messageID on some part
                        # objects; scan the full message list and find the
                        # question part by its persisted id (it carries the
                        # complete multi-group input there).
                        try:
                            msg_resp = await BACKEND.fetch_messages(client, session_id)
                            if msg_resp.status_code == 200:
                                fetched = True
                                for m in msg_resp.json():
                                    for p2 in (m.get("parts") or []):
                                        if p2.get("type") == "tool" and p2.get("tool") == "question":
                                            st2 = p2.get("state") or {}
                                            i2 = st2.get("input") if isinstance(st2, dict) else None
                                            if not isinstance(i2, dict):
                                                continue
                                            qs = i2.get("questions")
                                            if isinstance(qs, list) and qs:
                                                for q in qs:
                                                    if not isinstance(q, dict):
                                                        continue
                                                    header = str(q.get("header") or "")
                                                    body = str(q.get("question") or "")
                                                    qopts = [str(o.get("label") or "") for o in (q.get("options") or [])
                                                             if isinstance(o, dict) and o.get("label")]
                                                    multi.append((header, body, qopts))
                                                if multi:
                                                    qtext = multi[0][1]
                                                    opts = multi[0][2]
                                                    break
                                            elif i2.get("question"):
                                                qtext = str(i2.get("question") or "")
                                                opts = [str(o.get("label") or "") for o in (i2.get("options") or [])
                                                        if isinstance(o, dict) and o.get("label")]
                                            if qtext or multi:
                                                break
                        except (httpx.HTTPError, ValueError):
                            pass
                    if qtext or multi:
                        break
                    if not fetched:
                        break
                    await asyncio.sleep(0.4)
            if len(multi) > 1:
                # Multi-group preflight: relay every group losslessly.
                lines = []
                for header, body, qopts in multi:
                    if header:
                        lines.append(f"{header}: {body}")
                    else:
                        lines.append(body)
                    if qopts:
                        lines.append(f"  Options: {' | '.join(qopts)}")
                yield ("question", "\n".join(lines))
                return
            if opts:
                qtext = f"{qtext} (Options: {' | '.join(opts)})"
            yield ("question", qtext or "Could you clarify?")
            return
        state = str((part.get("state") or {}).get("status") or "")
        # Track each tool part's state by PART ID (like text_lens above):
        # the same tool name can appear in several parts with different
        # states (e.g. two bash parts, one errored, one still running), and
        # name-keying flips the recorded state on every poll so the running
        # part re-emits its status chunk once per second.  Keying by part id
        # keeps each part's transitions independent and monotonic.
        if name and state != tool_state.get(pid):
            tool_state[pid] = state
            if state == "running":
                st = part.get("state") or {}
                inp = st.get("input") if isinstance(st, dict) else None
                cmd = str(inp.get("command") or "") if isinstance(inp, dict) else ""
                if cmd:
                    yield ("status", f"🔧 {name}: {cmd[:120]}\n")
                else:
                    # Non-bash tools (glob/grep/read) carry no command:
                    # show the pattern/path so the user still sees what
                    # the tool is doing.
                    pattern = str(inp.get("pattern") or "") if isinstance(inp, dict) else ""
                    if pattern:
                        yield ("status", f"🔧 {name}: {pattern[:80]}\n")
                    else:
                        path = str(inp.get("filePath") or "") if isinstance(inp, dict) else ""
                        if path:
                            yield ("status", f"🔧 {name}: {path[:100]}\n")
                        else:
                            yield ("status", f"🔧 {name}…\n")
            elif state == "completed":
                yield ("status", f"✅ {name} done\n")
            elif state == "error":
                yield ("status", f"⚠️ {name} failed\n")
        return


async def _current_running_tool(
    client: httpx.AsyncClient,
    session_id: str,
    tool_state: dict[str, str],
    announced: set[str],
) -> Optional[str]:
    """Best-effort status for the NEWEST running tool part not yet
    announced, read from the persisted message list.

    The serve's SSE bus drops tool running-state updates (upstream defect
    family anomalyco/opencode #35066 — completions arrive, 'running'
    events often do not), so the event-bus path never emits the 🔧 command
    status for a long-running tool and the user sees only keepalives.
    Polling the message list is immune to the drops.  Parts already
    processed by the delta path (``tool_state``) are skipped — they were
    announced on their own state transition.  Never raises; returns None
    when nothing new is running.
    """
    try:
        resp = await BACKEND.fetch_messages(client, session_id)
        if resp.status_code != 200:
            return None
        for m in reversed(resp.json()):
            if (m.get("info") or {}).get("role") != "assistant":
                continue
            for p in reversed(m.get("parts") or []):
                if p.get("type") != "tool" or p.get("tool") == "question":
                    continue
                pid = str(p.get("id") or "")
                st = p.get("state") or {}
                if st.get("status") != "running":
                    continue
                if pid in tool_state or pid in announced:
                    return None
                announced.add(pid)
                # Also record the state in tool_state so the delta path
                # (a later-arriving SSE running event) skips the same part
                # instead of announcing it a second time.
                tool_state[pid] = "running"
                inp = st.get("input") if isinstance(st, dict) else None
                cmd = str((inp or {}).get("command") or "") if isinstance(inp, dict) else ""
                if cmd:
                    return f"🔧 {p.get('tool')}: {cmd[:120]}\n"
                pattern = str((inp or {}).get("pattern") or "") if isinstance(inp, dict) else ""
                if pattern:
                    return f"🔧 {p.get('tool')}: {pattern[:80]}\n"
                path = str((inp or {}).get("filePath") or "") if isinstance(inp, dict) else ""
                if path:
                    return f"🔧 {p.get('tool')}: {path[:100]}\n"
                return f"🔧 {p.get('tool')}…\n"
    except (httpx.HTTPError, OSError, ValueError):
        return None
    return None


async def _child_wedged(client: httpx.AsyncClient, child_id: str) -> bool:
    """True when the child session's own tool runner is wedged: a
    running tool part (not ``question``) with no output, started past
    the tool-wedge threshold.  The serve's runner degrades
    progressively (bash/glob/read all strand), so a wedged child never
    finishes and the parent's delivery is stuck with it.  Failure reads
    False (no extra early abort).  Never raises.
    """
    try:
        resp = await BACKEND.fetch_messages(client, child_id)
        if resp.status_code != 200:
            return False
        now_ms = int(time.time() * 1000)
        for m in resp.json():
            for p in m.get("parts") or []:
                if p.get("type") != "tool" or p.get("tool") == "question":
                    continue
                st = p.get("state") or {}
                if st.get("status") != "running" or st.get("output"):
                    continue
                start = (st.get("time") or {}).get("start")
                if start is None:
                    continue
                if now_ms - int(start) > _TOOL_WEDGE_AFTER_S * 1000:
                    return True
    except (httpx.HTTPError, OSError, ValueError):
        return False
    return False


async def _session_live(client: httpx.AsyncClient, session_id: str) -> bool:
    """True when the session appears in the serve's live status map
    (the map holds the active sessions; a finished session drops out).
    Failure to fetch reads True — the stall-abort never fires on a
    transport error.  Never raises.
    """
    try:
        resp = await BACKEND.fetch_session_status(client)
        if resp.status_code != 200:
            return True
        st_map = resp.json()
        if not isinstance(st_map, dict):
            return True
        return session_id in st_map
    except (httpx.HTTPError, OSError, ValueError):
        return True


async def _detect_wedged_tool(client: httpx.AsyncClient, session_id: str) -> bool:
    """Detect a tool part stuck in "running" with no output.

    The serve's tool runner can mark a tool part as started (status
    "running") but never actually execute it, leaving the session busy
    forever.  A running tool part with no output whose start is older than
    ``_TOOL_WEDGE_AFTER_S`` is wedged, not working.  Never raises.

    STALE-PART GUARD (2026-08-08): tool parts started BEFORE the current
    serve process are debris from a dead serve (the serve dies silently
    under load; the session storage survives and the pinned session is
    resumed on a fresh serve).  Aborting the session for stale debris
    kills a healthy resumed cycle, so parts older than the serve's start
    are ignored.
    """
    try:
        serve_start_ms = await asyncio.to_thread(_serve_start_epoch_ms)
        resp = await BACKEND.fetch_messages(client, session_id)
        if resp.status_code != 200:
            return False
        now_ms = int(time.time() * 1000)
        for m in resp.json():
            for p in m.get("parts") or []:
                if p.get("type") != "tool" or p.get("tool") == "question":
                    continue
                st = p.get("state") or {}
                if st.get("status") != "running" or st.get("output"):
                    continue
                start = (st.get("time") or {}).get("start")
                if start is None:
                    continue
                start_ms = int(start)
                if serve_start_ms is not None and start_ms < serve_start_ms:
                    # Stale part from a previous serve — not a live wedge.
                    continue
                elapsed_s = (now_ms - start_ms) / 1000
                threshold_s = (
                    _TASK_WEDGE_AFTER_S if p.get("tool") == "task"
                    else _TOOL_WEDGE_AFTER_S
                )
                # STALL-AWARE ABORT (2026-08-11): the sub-agent's
                # completion never propagates to the parent's task part
                # (upstream #11865/#23296; the serve's tool runner also
                # wedges on glob calls, stranding the child).  A task
                # part old enough whose child has dropped out of the
                # live status map means the work finished but the
                # delivery is stuck: fire early so the orchestrator's
                # artifact-check-first proceeds WITHOUT redoing the
                # phase.
                if p.get("tool") == "task" and start_ms < now_ms - _TASK_STALL_AFTER_S * 1000:
                    child_id = (p.get("state") or {}).get(
                        "metadata", {},
                    ).get("sessionId")
                    if child_id and (
                        not await _session_live(client, child_id)
                        or await _child_wedged(client, child_id)
                    ):
                        logger.warning(
                            "stalled task delivery: child %s not live or "
                            "wedged while task part runs %.0fs — session %s",
                            str(child_id)[:16], elapsed_s,
                            str(session_id)[:16],
                        )
                        return True
                if now_ms - start_ms > threshold_s * 1000:
                    # REPLAY CARVE-OUT (2026-08-10): a fallback-model
                    # replay can legitimately run 5-20 min with the task
                    # part silent (the replayed turn streams no output
                    # until it lands).  Do not wedge while the fallback
                    # log shows a replay in flight; fails closed to the
                    # pre-carve-out behavior when the log is unreadable.
                    if p.get("tool") == "task" and await asyncio.to_thread(
                        _replay_in_flight_for_any_session,
                    ):
                        continue
                    logger.warning(
                        "wedged tool part %r running without output for %.0fs — session %s",
                        p.get("tool"), elapsed_s, str(session_id)[:16],
                    )
                    return True
    except (httpx.HTTPError, OSError, ValueError):
        return False
    return False


def _serve_start_epoch_ms() -> Optional[int]:
    """Epoch-ms when the current serve process started (from /proc), or
    None when it cannot be determined (no stale guard)."""
    try:
        pid = _find_serve_pid(OPENCODE_SERVE_URL.rsplit(":", 1)[-1])
        if not pid:
            return None
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            parts = fh.read().split()
        start_ticks = int(parts[21])
        with open("/proc/uptime", encoding="utf-8") as fh:
            uptime_s = float(fh.read().split()[0])
        start_s = time.time() - uptime_s + start_ticks / os.sysconf("SC_CLK_TCK")
        return int(start_s * 1000)
    except (OSError, ValueError, IndexError):
        return None


async def _seed_resumed_session_state(
    client: httpx.AsyncClient,
    session_id: str,
    user_mids: set[str],
    text_lens: dict[str, int],
    tool_state: dict[str, str],
    seen_question_pids: set[str],
    running_question_times: Optional[list[float]] = None,
) -> int:
    """Best-effort seed of per-call part state for a RESUMED pinned session.

    The per-call text_lens/tool_state/user_mids are normally fed only by
    live events; when the /event bus closes, the polling fallback re-scans
    the FULL message list and re-yields history (old text, thinking, tool
    chunks, and a stale question that stops the stream).  Seeding the
    existing part state (text lengths, tool states, assistant message ids,
    question part ids) once before the prompt lets the polling deltas
    suppress everything that existed BEFORE this request.  Never raises:
    a failed seed degrades to the current replay behavior.

    ``running_question_times`` (optional list) collects the start times of
    any question tool parts still parked in "running" — the orphaned-
    question check uses them without a second message-list GET.

    Returns the estimated context size in tokens (text/reasoning chars /
    4, 0 when the fetch failed) — the bridge-level context cap check.
    """
    total_chars = 0
    try:
        resp = await BACKEND.fetch_messages(client, session_id)
        if resp.status_code != 200:
            return 0
        body = resp.json()
        if not isinstance(body, list):
            # Malformed/dict bodies (e.g. an error object) must not raise.
            return 0
        for m in body:
            role = (m.get("info") or {}).get("role")
            if role == "assistant":
                mid = m.get("id")
                if mid:
                    user_mids.add(mid)
                for p in m.get("parts") or []:
                    pid = str(p.get("id") or "")
                    ptype = p.get("type")
                    if ptype in ("text", "reasoning"):
                        text = str(p.get("text") or "")
                        total_chars += len(text)
                        if text:
                            text_lens[pid] = len(text)
                    elif ptype == "tool":
                        status = str((p.get("state") or {}).get("status") or "")
                        if status:
                            tool_state[pid] = status
                        if p.get("tool") == "question" and pid:
                            seen_question_pids.add(pid)
                            if running_question_times is not None and status == "running":
                                start = (p.get("state") or {}).get("time") or {}
                                s = start.get("start")
                                if s is not None:
                                    running_question_times.append(float(s))
    except (httpx.HTTPError, OSError, ValueError):
        return 0
    return total_chars // 4


async def _poll_session_deltas(
    client: httpx.AsyncClient,
    session_id: str,
    user_mids: set[str],
    text_lens: dict[str, int],
    tool_state: dict[str, str],
    seen_question_pids: Optional[set[str]] = None,
) -> AsyncIterator[tuple[str, str]]:
    """Poll a session's message list for new parts (used when the /event SSE
    bus closes but the session is still busy).  Yields deltas; the CALLER
    checks the session status and decides when to stop polling."""
    try:
        resp = await BACKEND.fetch_messages(client, session_id)
        if resp.status_code != 200:
            return
        for m in resp.json():
            if (m.get("info") or {}).get("role") != "assistant":
                continue
            for p in m.get("parts") or []:
                if p.get("messageID") in user_mids:
                    continue
                async for delta in _yield_part_deltas(
                    p, text_lens, tool_state, session_id, client,
                    seen_question_pids,
                ):
                    if delta[0] == "question":
                        yield delta
                        return
                    yield delta
    except (httpx.HTTPError, OSError, ValueError):
        return





_DRAIN_PROBES = 4
_DRAIN_PROBE_S = 0.25


async def _drain_serve_shutdown() -> None:
    """Boundedly wait for the killed serve listener to stop.

    is_opencode_serve_running() answers True mid-SIGTERM, so
    ensure_opencode_serve would otherwise skip the respawn and the stream
    POSTs into a dying listener.  Poll a few times and return — never
    raises, never exceeds the budget.
    """
    for _ in range(_DRAIN_PROBES):
        if not await is_opencode_serve_running():
            return
        await asyncio.sleep(_DRAIN_PROBE_S)


async def _recycle_serve_if_low_memory() -> None:
    """Restart the opencode serve when the system is critically short of
    memory.

    The serve's ~1GB RSS is a significant reclaim when the box is swap
    thrashing, and a memory-starved serve hangs its bash tool on trivial
    commands (the agent-loop tool runner cannot spawn/complete shells).
    Killing the serve lets the next ``ensure_opencode_serve`` respawn a
    fresh one.  Never raises.
    """
    if not BACKEND.owns_server:
        return
    try:
        port = OPENCODE_SERVE_URL.rsplit(":", 1)[-1]
        pressure, elapsed = await asyncio.to_thread(_serve_health, port)
        reason = None
        if pressure:
            reason = "low memory"
        elif elapsed is not None and elapsed > _SERVE_RECYCLE_AFTER_S:
            reason = f"up {elapsed / 60:.0f} min (progressive tool-runner wedging)"
        if not reason:
            return
        logger.warning("Recycling opencode serve: %s", reason)
        pid = await asyncio.to_thread(_find_serve_pid, port)
        if pid:
            if not await asyncio.to_thread(_pid_is_serve, pid, port):
                # Pid-reuse race guard: this pid no longer belongs to the
                # serve (it died and the OS recycled the pid between the
                # scan and the kill).  Skip the kill; the serve stays
                # absent and the next ensure respawns a fresh one.
                logger.warning(
                    "skipping recycle: pid %s no longer matches the serve", pid,
                )
                return
            try:
                os.kill(pid, 15)
            except (OSError, ProcessLookupError):
                pass
            await _drain_serve_shutdown()
    except (OSError, ValueError):
        return


async def _force_recycle_serve(reason: str = "long-lived call") -> None:
    """Kill the opencode serve unconditionally so the next
    ``ensure_opencode_serve`` respawns a fresh one.

    Used by the plain-bridge auto-retry and the config-drift gate (REQ-5):
    the serve's tool runner progressively wedges (bash hangs on trivial
    commands), so a fresh serve before a retry removes that risk.  The
    drift gate recycles when the serve template changes under a running
    serve.  Never raises.
    """
    if not BACKEND.owns_server:
        return
    try:
        port = OPENCODE_SERVE_URL.rsplit(":", 1)[-1]
        pid = await asyncio.to_thread(_find_serve_pid, port)
        if pid:
            if not await asyncio.to_thread(_pid_is_serve, pid, port):
                # Pid-reuse race guard: the found pid no longer belongs to
                # the serve — never signal a reused pid.  The serve stays
                # absent and the next ensure respawns a fresh one.
                logger.warning(
                    "skipping force-recycle: pid %s no longer matches the serve", pid,
                )
                return
            logger.warning("Forcing opencode serve recycle (%s)", reason)
            os.kill(pid, 15)
            await _drain_serve_shutdown()
    except (OSError, ProcessLookupError):
        pass
    except (OSError, ValueError):
        return


# F5 (tasks 4.4): the config-mtime cache lives module-level, NOT on
# proxy.app.state — the spawn gate (ensure_opencode_serve) is a
# bridge-internal function with no request/app handle (pending_permissions
# is threaded IN from routes as a plain dict; this cache is owned by the
# spawn gate itself, so there is no clean app.state channel).  Design.md
# chose module-level; review-accepted via F5.  Set after every successful
# spawn; drives the REQ-5 drift recycle.
_serve_config_mtime: Optional[float] = None


def _config_mtime() -> Optional[float]:
    """mtime of OPCODE_CONFIG_PATH, or None when unreadable (no drift)."""
    try:
        return os.path.getmtime(OPCODE_CONFIG_PATH)
    except OSError:
        return None


def _sync_serve_config() -> None:
    """Idempotently copy the serve template + fallback-plugin config into the
    serve-config dir.

    opencode is an XDG app: with XDG_CONFIG_HOME=OPENCODE_SERVE_CONFIG_DIR it
    reads <dir>/opencode/opencode.jsonc — so the template lands in the
    ``opencode/`` subdir (empirically verified 2026-08-08: the file at the
    XDG base root is NOT read).  The reduced file there (file:// plugin ref
    for the fallback plugin ONLY, no .ts plugin files) is the serve's whole
    config.  The fallback plugin resolves its OWN config
    (rate-limit-fallback.json) via $XDG_CONFIG_HOME/opencode/ too — without
    a copy there it initializes with its defaults (logging OFF, which
    would blind the REQ-4 fallback-log evidence), so the proxy syncs the
    user's plugin config alongside the template.  Copies are skipped when
    the destination already matches, so repeated spawns do not churn the
    dir."""
    if not BACKEND.owns_server:
        # Secure backend: the server's config is systemd-owned; nothing to sync.
        return
    dst_dir = os.path.join(OPENCODE_SERVE_CONFIG_DIR, "opencode")
    os.makedirs(dst_dir, exist_ok=True)
    _copy_if_changed(
        OPCODE_CONFIG_PATH, os.path.join(dst_dir, "opencode.jsonc"),
    )
    plugin_cfg = os.path.expanduser(
        "~/.config/opencode/rate-limit-fallback.json"
    )
    if os.path.exists(plugin_cfg):
        _copy_if_changed(
            plugin_cfg, os.path.join(dst_dir, "rate-limit-fallback.json"),
        )


def _copy_if_changed(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst`` when the contents differ (idempotent)."""
    with open(src, encoding="utf-8") as fh:
        data = fh.read()
    try:
        with open(dst, encoding="utf-8") as fh:
            if fh.read() == data:
                return
    except OSError:
        pass
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(data)
    logger.info("Synced opencode serve config template -> %s", dst)


def _serve_health(port: str) -> tuple[bool, Optional[float]]:
    """Sync health probe (offloaded by the caller): (memory_pressure,
    serve_elapsed_seconds).  Returns (False, None) when the serve is
    healthy and recent — callers recycle only on low memory or age."""
    pressure = _memory_pressure()
    pid = _find_serve_pid(port)
    elapsed: Optional[float] = None
    if pid:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                parts = fh.read().split()
            start_ticks = int(parts[21])
            with open("/proc/uptime", encoding="utf-8") as fh:
                uptime_s = float(fh.read().split()[0])
            # Process uptime = system uptime - (start ticks / clock rate).
            elapsed = uptime_s - (start_ticks / os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError, IndexError):
            elapsed = None
    return pressure, elapsed




def _open_serve_log() -> Any:
    """Open (create) the serve log file with sync I/O off the event loop."""
    os.makedirs(OPENCODE_WORKSPACE_DIR, exist_ok=True)
    return open(
        os.path.join(OPENCODE_WORKSPACE_DIR, "opencode-serve.log"), "ab", buffering=0,
    )


def _memory_pressure() -> bool:
    """True when the system is critically short of memory (sync /proc read,
    offloaded to a worker thread by the caller — Rule 3)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
                    break
            else:
                return False
        if avail_kb < 2_000_000:
            logger.warning(
                "Low memory (%.1fGB available) — recycling opencode serve",
                avail_kb / 1048576,
            )
            return True
    except (OSError, ValueError):
        pass
    return False


def _cmdline_matches_serve(cmd: str, port: str) -> bool:
    """True when a normalized /proc cmdline belongs to the opencode serve
    bound to ``port``.

    /proc/<pid>/cmdline separates argv with NUL bytes, not spaces — the old
    space-form match NEVER matched any process, so serve recycling (age,
    low-memory, wedge recovery) silently did nothing and a wedged tool
    runner lived forever.  NULs are normalized to spaces and the argv is
    tokenized before matching.  PORT MATCHING IS EXACT ONLY: neither a
    longer advertised value (--port 189990 vs search "18999") nor a shorter
    search (--port 18999 vs search "1899") may match (prefix-false-positive
    bug, cycle 9).
    """
    tokens = cmd.replace("\x00", " ").split()
    if not any("opencode" in tok for tok in tokens) or "serve" not in tokens:
        return False
    if f"--port={port}" in tokens:
        return True
    for i, tok in enumerate(tokens):
        if tok == "--port" and i + 1 < len(tokens) and tokens[i + 1] == port:
            return True
    return False


def _pid_is_serve(pid: int, port: str) -> bool:
    """Re-verify ``pid`` still belongs to the opencode serve before a kill.

    Guards the pid-reuse TOCTOU race: the pid found by ``_find_serve_pid``
    may have died and been recycled by the OS between the scan and the
    kill.  Unreadable/missing cmdline counts as mismatch (never raises).
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().decode("utf-8", "ignore")
        return _cmdline_matches_serve(cmd, port)
    except OSError:
        return False


def _find_serve_pid(port: str) -> Optional[int]:
    """Locate the opencode serve process pid by scanning /proc cmdlines."""
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as fh:
                    cmd = fh.read().decode("utf-8", "ignore")
                if _cmdline_matches_serve(cmd, port):
                    return int(entry)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return None
