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
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, AsyncIterator, Optional

import httpx

from constants import (
    OPENCODE_AGENT,
    OPENCODE_BIN,
    OPENCODE_SERVE_TIMEOUT,
    OPENCODE_SERVE_URL,
    OPENCODE_BRIDGE_DIRECTORY,
    OPENCODE_SERVE_CONFIG_DIR,
    OPENCODE_SERVE_PURE,
    OPENCODE_WORKSPACE_DIR,
    OPCODE_CONFIG_PATH,
    _machine,
    get_logger,
)

logger = get_logger("kinver.opencode_bridge")

# System prompt sent to every bridge session.  The gentle-orchestrator's
# persona (AGENTS.md) defaults direct replies to Rioplatense Spanish, which
# is wrong for an API-facing bridge consumed by OpenWebUI/nanobot — pin
# English unless the user's own message is in another language.
_BRIDGE_SYSTEM_PROMPT = (
    "You are an API-backed coding assistant reached through a proxy bridge. "
    "Respond in English unless the user's message is written in another "
    "language. Keep the final answer concise and in English.\n\n"
    "The user CAN answer your follow-up questions in their next message, so "
    "if the task is genuinely ambiguous and the decision materially changes "
    "the result, ask a clarifying question and stop — the user's answer will "
    "resume this same session and you may ask again if needed.  Otherwise "
    "make reasonable assumptions, state them briefly, and complete the task."
)

# SDD-AUTONOMOUS system prompt: used when the client picks model
# "opencode-sdd".  The orchestrator must run the COMPLETE SDD cycle in ONE
# long-lived turn — no clarifying questions, no per-phase chat — because
# the bridge is a single request/response and the user will not answer
# follow-ups mid-cycle.  The preflight choices are supplied in the task
# text; the orchestrator caches them and proceeds.
_SDD_AUTONOMOUS_SYSTEM_PROMPT = (
    "You are the SDD orchestrator in AUTONOMOUS mode through a proxy "
    "bridge. Run the COMPLETE Spec-Driven Development cycle for the "
    "requested change in ONE continuous turn: session preflight (use the "
    "choices already given in the task text - do NOT ask), init guard, "
    "then proposal, spec, design, tasks, apply, verify, archive "
    "back-to-back, delegating each phase to the appropriate sub-agent.\n\n"
    "CRITICAL RULES:\n"
    "- Never stop to ask a clarifying question or present the interactive "
    "proceed/adjust/stop menu. The user cannot answer mid-cycle on this "
    "channel; the whole cycle must complete autonomously in this turn.\n"
    "- If the preflight choices are embedded in the task text, treat them "
    "as the user-supplied session preflight and do not re-ask.\n"
    "- Stream concise status lines between phases so the caller sees "
    "progress (e.g. 'SDD: exploring', 'SDD: proposing', ...).\n"
    "- Apply the Gatekeeper between phases; on failure fix once or abort "
    "with a clear error - never loop.\n"
    "- LOCAL-MODEL DELEGATION (MANDATORY): when delegating code work to "
    "the LOCAL model (apply's local writer), delegate ONE FILE at a time "
    "— one task per file — for big tasks; never bundle multiple files "
    "into one local-model task (the local context window is limited).\n"
    "- TOOL RETRY (MANDATORY): when a tool call fails with a transient "
    "error (e.g. \"Tool execution aborted\", connection reset), retry the "
    "tool ONCE immediately before giving up - the serve's tool runner "
    "intermittently aborts in-flight executions and the retry normally "
    "succeeds.  The same rule applies to every sub-agent you delegate to "
    "(say so in the delegation prompt).\n"
    "- Keep the final summary short: change name, artifacts produced, "
    "tests run, and any remaining risk.\n"
    "You still have full sub-agent access; use it for every phase. Do NOT "
    "ask the user anything."
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
_TASK_WEDGE_AFTER_S: float = 600.0
# How often the stream checks the session for a wedged tool part.
_WEDGE_CHECK_INTERVAL_S: float = 10.0

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
# bridge auto-allows READ commands and asks the USER about WRITE commands.
# Pending write permissions are keyed by session id so the next request
# (which resumes the pinned session) can answer them.

# Bounded WRITE classifier for the external_directory gate: any redirect,
# a whole-word "write", or one of these mutating command tokens marks the
# command WRITE (needs user approval); everything else (cat/ls/head on
# /etc or /home) is READ and auto-allowed.  echo-with-redirect is covered
# by the ">" check alone.
_EXTERNAL_WRITE_TOKENS: tuple[str, ...] = (
    "mv ", "cp ", "rm ", "touch ", "mkdir ", "rmdir ", "ln ",
    "chmod ", "chown ", "tee ", "sed -i", "install ", "dd ",
    "git commit", "git push", "git reset", "git checkout", "printf ",
)

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


def _classify_external_access(cmd: str) -> str:
    """Classify a permissioned external-directory command as "read"/"write".

    Bounded heuristic (kept intentionally simple): any redirect or a known
    mutating command token ⇒ "write"; everything else (``cat /etc/os-release``,
    ``ls /home``, ``head -5 /etc/passwd``) ⇒ "read".
    """
    c = (cmd or "").strip()
    if ">" in c:
        return "write"
    if re.search(r"\bwrite\b", c):
        return "write"
    if any(token in c for token in _EXTERNAL_WRITE_TOKENS):
        return "write"
    return "read"


_WRITE_TOOL_TYPES: frozenset[str] = frozenset({
    "write", "edit", "patch", "create", "append", "delete", "remove",
})


def _classify_permission_access(perm_type: str, tool_name: str, cmd: str) -> str:
    """Classify a permissioned external access as "read"/"write".

    Type-aware first (F2): a permission whose TYPE is itself a write tool
    ("write"/"edit"/"patch"/...) is a WRITE by definition — the cmd
    heuristic must NOT run on it, because write/edit permission records
    carry no bash command and an empty cmd would read as "read"
    (auto-allowing the write even in interactive mode).  A ``write``/
    ``edit`` TOOL on an external_directory gate is likewise WRITE.  Bash
    commands fall back to ``_classify_external_access`` (redirects / known
    mutating tokens ⇒ write; cat/ls/head ⇒ read).  Used by both the event-
    bus permission handler and the polling resolver so a ``write`` tool to
    an external dir surfaces a question instead of being auto-allowed.
    """
    if perm_type in _WRITE_TOOL_TYPES:
        return "write"
    if perm_type == "external_directory" and tool_name in _WRITE_TOOL_TYPES:
        return "write"
    return _classify_external_access(cmd)


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
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{OPENCODE_SERVE_URL}/session/{session_id}/permissions/{permission_id}",
                json={"response": response},
                timeout=10.0,
            )
            return resp.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


async def _abort_session_best_effort(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Best-effort abort of one opencode session (blocking-path cleanup).

    The blocking escalation path has no SSE bus, so it cannot watch for
    wedged tools; when it gives up on a session (timeout, HTTP error,
    write-permission abort) it must not leave a busy zombie behind.
    Never raises.
    """
    try:
        await client.post(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/abort",
            timeout=10.0,
        )
    except (httpx.HTTPError, OSError):
        pass


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
    *,
    autonomous: bool,
) -> Optional[str]:
    """Handle one permission request from the opencode serve.

    THE single permission handler, shared by all four relay paths (the
    polling-wedge check, the event-bus-timeout check, the ``permission.updated``
    SSE event, and the completion resolver) so the read→auto-allow /
    write→relay / autonomous→auto-allow policy lives in ONE place.

    ``perm`` is either an SSE event ``properties`` dict (keys: id, type,
    title, metadata, tool) or a GET /permission record (keys: id,
    permission, patterns, tool).  Returns None when the request was
    HANDLED silently (read auto-allowed, or an autonomous-mode auto-allow
    with no client surface and no pending state); returns a question
    string when the USER must decide (interactive-mode write/git ask).
    Never raises.
    """
    pid = str(perm.get("id") or "")
    perm_type = str(perm.get("permission") or perm.get("type") or "")
    if not pid or not perm_type:
        return None
    # Pull the command for read/write classification: SSE events carry it
    # in metadata.command/title; GET /permission records carry patterns and
    # the tool part's state.input.command.
    cmd = ""
    meta = perm.get("metadata") or {}
    if isinstance(meta, dict):
        cmd = str(meta.get("command") or "")
    cmd = cmd or str(perm.get("title") or "")
    cmd = cmd or str((perm.get("patterns") or [""])[0])
    tool_name = ""
    try:
        tool_info = perm.get("tool") or {}
        mid = tool_info.get("messageID")
        call_id = tool_info.get("callID")
        if mid:
            msg_resp = await client.get(
                f"{OPENCODE_SERVE_URL}/session/{session_id}/message/{mid}",
                timeout=10.0,
            )
            if msg_resp.status_code == 200:
                for part in (msg_resp.json().get("parts") or []):
                    if part.get("callID") == call_id or part.get("id") == call_id:
                        tool_name = str(part.get("tool") or "")
                        cmd = str(((part.get("state") or {}).get("input") or {}).get("command") or cmd)
                        break
    except (httpx.HTTPError, OSError, ValueError):
        pass
    if autonomous:
        # SDD-autonomous mode: the user pre-approved the whole cycle, so
        # every relayed ask (write/edit/bash/git) is auto-allowed — POST
        # "always" with NO client surface and NO pending state.  Bounded by
        # _post_permission_response (10s timeout, never raises).
        await _post_permission_response(session_id, pid, "always")
        return None
    if _classify_permission_access(perm_type, tool_name, cmd) == "read":
        await _post_permission_response(session_id, pid, "always")
        return None
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
    asks are caught here and relayed/auto-allowed exactly like the
    event-bus path.
    """
    try:
        resp = await client.get(f"{OPENCODE_SERVE_URL}/permission", timeout=10.0)
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


async def ensure_opencode_serve() -> bool:
    """Ensure the opencode serve backend is up (spawn if missing).

    Returns True when the backend is answering.  Spawns ``opencode serve``
    as a detached child process bound to ``OPENCODE_SERVE_URL`` — the
    proxy owns its lifecycle so no systemd unit is required.  Never raises.

    Config-drift gate (REQ-5): when a serve IS running and the serve
    template (OPCODE_CONFIG_PATH) is newer than the mtime cached at the
    last successful spawn, the serve is recycled via ``_force_recycle_serve``
    and respawned so the edited config actually loads.  An unreadable
    template is treated as no-drift.
    """
    global _serve_config_mtime
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
        # The serve's openai provider needs the ChatGPT-Plus OAuth access
        # token as its API key (the auth.json openai entry is OAuth, and
        # the SDK's provider refuses to load it without a key).  Pass it
        # via the env so no token ever lands in a config file; the token
        # is refreshed from auth.json at every spawn.
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
            # (which the API rejects: the ChatGPT-Plus OAuth token lacks
            # the api.responses.write scope).  Copy the auth file into the
            # serve's data home so the serve resolves the openai provider
            # through the SAME chatgpt-headless OAuth route the TUI/CLI
            # use (2026-08-09).
            _serve_auth = os.path.join(
                OPENCODE_SERVE_CONFIG_DIR, "opencode", "auth.json",
            )
            _src_auth = os.path.expanduser("~/.local/share/opencode/auth.json")
            if os.path.exists(_src_auth):
                os.makedirs(os.path.dirname(_serve_auth), exist_ok=True)
                with open(_src_auth, "rb") as _sf, open(_serve_auth, "wb") as _df:
                    _df.write(_sf.read())
        except (OSError, ValueError, TypeError):
            pass
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
    model_id: Optional[str] = None,
    provider_id: str = "kinver",
    timeout: float = OPENCODE_SERVE_TIMEOUT,
    system_prompt: str = _BRIDGE_SYSTEM_PROMPT,
    autonomous: bool = False,
) -> str:
    """Send a task to headless opencode and return the assistant text.

    Creates a fresh session per call, posts the message (blocking until
    the agent finishes), and concatenates the ``text`` parts.  Proxy-status
    sentinel segments are stripped from the result.  Returns an error
    string on failure (escalation-friendly, never raises).

    While the message POST is in flight the call polls ``GET /permission``
    (``_BLOCKING_PERMISSION_POLL_S`` cadence): READ asks are auto-allowed,
    WRITE/git asks are NOT granted (headless callers have no user to relay
    a question to) — the session is aborted and a clear error is returned
    instead of burning the full timeout on a parked agent.  ``autonomous``
    preserves the streaming path's auto-allow-all semantics.  On every
    non-success exit the created session is aborted best-effort so no busy
    zombie is left on the serve.
    """
    if not await ensure_opencode_serve():
        return "[OpenCode Bridge Failed: opencode serve not reachable.]"
    async with httpx.AsyncClient() as client:
        try:
            # ---- 1. Create a fresh session --------------------------------
            resp = await client.post(
                f"{OPENCODE_SERVE_URL}/session",
                params={"directory": OPENCODE_BRIDGE_DIRECTORY},
                timeout=30.0,
            )
            if resp.status_code != 200:
                return f"[OpenCode Bridge Error: session HTTP {resp.status_code}]"
            session_id = resp.json().get("id")
            if not session_id:
                return "[OpenCode Bridge Error: no session id returned.]"

            # ---- 2. Post the message, watching for permission gates ------
            payload: dict[str, Any] = {
                "agent": agent,
                "system": system_prompt,
                "parts": [{"type": "text", "text": user_text}],
            }
            if model_id:
                payload["model"] = {
                    "modelID": model_id,
                    "providerID": provider_id,
                    "variant": "default",
                }
            # The POST runs as a task so the poller below can watch GET
            # /permission concurrently (httpx clients are concurrency-safe).
            post_task = asyncio.create_task(
                client.post(
                    f"{OPENCODE_SERVE_URL}/session/{session_id}/message",
                    json=payload,
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
                                autonomous=autonomous,
                            )
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
                                    "questions; session aborted.]"
                                )
                    await asyncio.sleep(min(0.25, _BLOCKING_PERMISSION_POLL_S))
            except (httpx.HTTPError, OSError) as exc:
                await _abort_session_best_effort(client, session_id)
                return f"[OpenCode Bridge Network Error: {str(exc)}]"
            finally:
                if not post_task.done():
                    post_task.cancel()
            if resp.status_code != 200:
                await _abort_session_best_effort(client, session_id)
                return f"[OpenCode Bridge Error: message HTTP {resp.status_code}]"
            data = resp.json()
            parts = data.get("parts", [])
            chunks = [
                str(p.get("text") or "")
                for p in parts
                if p.get("type") == "text" and p.get("text")
            ]
            return _strip_proxy_status_text("".join(chunks)).strip() or (
                "[OpenCode Bridge Error: empty response.]"
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return f"[OpenCode Bridge Network Error: {str(exc)}]"


async def opencode_chat_stream(
    user_text: str,
    *,
    agent: str = OPENCODE_AGENT,
    model_id: Optional[str] = None,
    provider_id: str = "kinver",
    session_map: Optional[dict[str, str]] = None,
    session_key: Optional[str] = None,
    pending_permissions: Optional[dict[str, tuple[str, bool]]] = None,
    just_approved_permission: bool = False,
    system_prompt: str = _BRIDGE_SYSTEM_PROMPT,
    timeout: float = OPENCODE_SERVE_TIMEOUT,
    autonomous: bool = False,
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
    # SDD-autonomous mode (model "opencode-sdd", explicit flag — not the
    # old timeout inference): force a fresh serve BEFORE spawning so the
    # cycle never runs on a progressively-wedged tool runner.
    if autonomous:
        await _force_recycle_serve()
    # Recycle BEFORE ensure: the recycle SIGTERMs the serve (low memory or
    # uptime > _SERVE_RECYCLE_AFTER_S) and never respawns, so running it
    # after ensure would break the next request with a spurious ConnectError.
    # The ensure below respawns a fresh serve when a recycle fired.
    await _recycle_serve_if_low_memory()
    if not await ensure_opencode_serve():
        yield ("status", "[OpenCode Bridge Failed: opencode serve not reachable.]")
        return
    # Rule 6: pending-permission state lives on app.state in routes.py and is
    # threaded in; standalone callers (scripts/tests) fall back to a fresh
    # per-call dict.
    pending_permissions = (
        pending_permissions if pending_permissions is not None else {}
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
        try:
            protected = set(session_map.values()) if session_map else None
            await _abort_zombie_sessions(client, protected)
            session_id = (
                session_map.get(session_key)
                if session_map and session_key else None
            )
            if session_id:
                # Follow-up in the same conversation: reuse the pinned agent
                # session so it retains its tool state and context.  But a
                # session that is STILL BUSY (a previously hung agent tool)
                # can never accept new work — abort it, drop the pin, and
                # start fresh instead of queueing behind a zombie.
                try:
                    st_resp = await client.get(
                        f"{OPENCODE_SERVE_URL}/session/status", timeout=10.0,
                    )
                    st_map = st_resp.json()
                    st = (st_map.get(session_id) or {}).get("type")
                except (httpx.HTTPError, ValueError):
                    st = None
                if st == "busy" and not just_approved_permission:
                    # A session that just resumed after a permission
                    # approval is legitimately busy executing the approved
                    # tool (or generating its summary) — do NOT abort it.
                    # Only a busy session with NO such resume is stuck.
                    logger.warning(
                        "pinned session %s is busy (likely stuck) — aborting and starting fresh",
                        session_id[:16],
                    )
                    await _abort_stream_session_best_effort(
                        client, session_id,
                        session_map=session_map, session_key=session_key,
                        pending_permissions=pending_permissions,
                    )
                    session_id = None
            # True when the session_id came from the session map (a pin),
            # NOT from a fresh POST /session below — only resumed sessions
            # seed the poll-state with pre-existing history.
            resumed = session_id is not None
            if not session_id:
                resp = await client.post(
                    f"{OPENCODE_SERVE_URL}/session",
                    params={"directory": OPENCODE_BRIDGE_DIRECTORY},
                    timeout=30.0,
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
            payload: dict[str, Any] = {
                "agent": agent,
                "system": system_prompt,
                "parts": [{"type": "text", "text": user_text}],
            }
            if model_id:
                payload["model"] = {
                    "modelID": model_id,
                    "providerID": provider_id,
                    "variant": "default",
                }

            user_mids: set[str] = set()
            asst_mid: Optional[str] = None
            text_lens: dict[str, int] = {}
            tool_state: dict[str, str] = {}
            pending_done = False
            session_busy = True  # assume working until a status event says idle
            seen_question_pids: set[str] = set()
            if resumed:
                # Seed part state for the resumed conversation so the
                # polling fallback never replays history or re-surfaces a
                # stale question that kills the stream.  Best-effort.
                await _seed_resumed_session_state(
                    client, session_id, user_mids, text_lens,
                    tool_state, seen_question_pids,
                )
            # Open the event bus BEFORE sending the message: the bus is
            # fire-and-forget (no replay), so connecting after prompt_async
            # misses the early events (user message, assistant start, first
            # reasoning/tool parts) and the stream would look empty.
            async with client.stream("GET", f"{OPENCODE_SERVE_URL}/event") as ev:
                async_resp = await client.post(
                    f"{OPENCODE_SERVE_URL}/session/{session_id}/prompt_async",
                    json=payload,
                    timeout=30.0,
                )
                if async_resp.status_code != 204:
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
                        # step-finish with no new content across two
                        # consecutive poll cycles means the agent is done.
                        finish_quiet_cycles = 0
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
                                    msg_resp = await client.get(
                                        f"{OPENCODE_SERVE_URL}/session/{session_id}/message",
                                        timeout=10.0,
                                    )
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
                                    finish_quiet_cycles = 0
                                    # Do not emit "done" — the agent is mid
                                    # tool; keep the stream alive.
                                    cycle_content = True
                            # Multi-step agents pause between steps; a
                            # finished session shows a step-finish then goes
                            # quiet.  Two consecutive cycles with a step-
                            # finish and zero new content = done.
                            if pending_done and not cycle_content:
                                finish_quiet_cycles += 1
                                if finish_quiet_cycles >= 2:
                                    # The agent looks done, but it may be
                                    # parked on a permission the bridge
                                    # never answered.  Resolve it BEFORE
                                    # returning: READ -> auto-allow (tool
                                    # proceeds), WRITE -> surface a question
                                    # so the next request answers it.
                                    pending_perm = await _detect_pending_permission(client, session_id)
                                    if pending_perm:
                                        question = await _handle_permission_event(
                                            client, session_id, pending_perm,
                                            pending_permissions, autonomous=autonomous,
                                        )
                                        if question:
                                            yield ("question", question)
                                            return
                                    return
                            else:
                                finish_quiet_cycles = 0
                            try:
                                st_resp = await client.get(
                                    f"{OPENCODE_SERVE_URL}/session/status", timeout=10.0,
                                )
                                st = (st_resp.json().get(session_id) or {}).get("type")
                            except (httpx.HTTPError, ValueError):
                                st = None
                            if st == "idle":
                                pending_perm = await _detect_pending_permission(client, session_id)
                                if pending_perm:
                                    question = await _handle_permission_event(
                                        client, session_id, pending_perm,
                                        pending_permissions, autonomous=autonomous,
                                    )
                                    if question:
                                        yield ("question", question)
                                        return
                                return
                            # Real agent work with no new parts (long bash
                            # run, model generation) must not read as a
                            # dead stream: emit a keepalive after quiet.
                            if time.monotonic() - last_emit > _EVENT_QUIET_TIMEOUT:
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
                                        pending_permissions, autonomous=autonomous,
                                    )
                                    if question:
                                        yield ("question", question)
                                        return
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
                                    # The session abort frees the tool runner;
                                    # the autonomous force-recycle handles
                                    # serve health at the next long call.
                                    yield ("status", "[OpenCode Bridge Error: agent tool runner wedged — session aborted. Please retry.]")
                                    return
                            await asyncio.sleep(1.0)
                    except asyncio.TimeoutError:
                        if pending_done and not session_busy:
                            return
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
                                    pending_permissions, autonomous=autonomous,
                                )
                                if question:
                                    yield ("question", question)
                                    return
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
                        # Keep the client connection alive during long tool
                        # phases (and show the agent is still working).
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
                        # bridge wedges).  Auto-allow READ external-dir
                        # commands; WRITE and git commands yield a question
                        # and stop so the USER decides on the next request
                        # (the session stays pinned).  Autonomous mode
                        # auto-allows everything (REQ-2).
                        if props.get("type") in _RELAYED_PERMISSION_TYPES:
                            question = await _handle_permission_event(
                                client, session_id, props, pending_permissions,
                                autonomous=autonomous,
                            )
                            if question:
                                yield ("question", question)
                                return
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


# ---------------------------------------------------------------------------
# Escalation (mirrors llm.openrouter_cloud_escalation)
# ---------------------------------------------------------------------------

async def opencode_escalation(stage: int, prompt: str) -> str:
    """Fallback for the queue worker when local tiers are exhausted.

    Directs the user prompt to the opencode gentle-orchestrator agent
    OpenRouter.  ``stage`` is informational (passed through to logging).

    Conservative by design: this headless worker path runs with
    ``autonomous=False`` — READ permissions are auto-allowed, WRITE/git
    asks abort the session with a clear error (no user is present to
    relay a question, and unprompted grants are never issued).
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
                            msg_resp = await client.get(
                                f"{OPENCODE_SERVE_URL}/session/{session_id}/message/{mid}",
                                timeout=10.0,
                            )
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
                            msg_resp = await client.get(
                                f"{OPENCODE_SERVE_URL}/session/{session_id}/message",
                                timeout=10.0,
                            )
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
                    yield ("status", f"🔧 {name}…\n")
            elif state == "completed":
                yield ("status", f"✅ {name} done\n")
            elif state == "error":
                yield ("status", f"⚠️ {name} failed\n")
        return


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
        resp = await client.get(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/message", timeout=10.0,
        )
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
                if now_ms - start_ms > threshold_s * 1000:
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


async def _session_busy_on_current_serve(
    client: httpx.AsyncClient, session_id: str, serve_start_ms: int,
) -> bool:
    """True when the session has a tool part that is RUNNING on the current
    serve (started after the serve process).  Such sessions are quiet-but-
    working (e.g. a cycle's main session during a sub-agent phase) and must
    not be swept as zombies.  Never raises.
    """
    try:
        resp = await client.get(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/message", timeout=10.0,
        )
        if resp.status_code != 200:
            return False
        for m in resp.json():
            for p in m.get("parts") or []:
                if p.get("type") != "tool" or p.get("tool") == "question":
                    continue
                st = p.get("state") or {}
                if st.get("status") != "running" or st.get("output"):
                    continue
                start = (st.get("time") or {}).get("start")
                if start is not None and int(start) >= serve_start_ms:
                    return True
    except (httpx.HTTPError, OSError, ValueError):
        pass
    return False


async def _seed_resumed_session_state(
    client: httpx.AsyncClient,
    session_id: str,
    user_mids: set[str],
    text_lens: dict[str, int],
    tool_state: dict[str, str],
    seen_question_pids: set[str],
) -> None:
    """Best-effort seed of per-call part state for a RESUMED pinned session.

    The per-call text_lens/tool_state/user_mids are normally fed only by
    live events; when the /event bus closes, the polling fallback re-scans
    the FULL message list and re-yields history (old text, thinking, tool
    chunks, and a stale question that stops the stream).  Seeding the
    existing part state (text lengths, tool states, assistant message ids,
    question part ids) once before the prompt lets the polling deltas
    suppress everything that existed BEFORE this request.  Never raises:
    a failed seed degrades to the current replay behavior.
    """
    try:
        resp = await client.get(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/message", timeout=10.0,
        )
        if resp.status_code != 200:
            return
        body = resp.json()
        if not isinstance(body, list):
            # Malformed/dict bodies (e.g. an error object) must not raise.
            return
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
                        if text:
                            text_lens[pid] = len(text)
                    elif ptype == "tool":
                        status = str((p.get("state") or {}).get("status") or "")
                        if status:
                            tool_state[pid] = status
                        if p.get("tool") == "question" and pid:
                            seen_question_pids.add(pid)
    except (httpx.HTTPError, OSError, ValueError):
        return


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
        resp = await client.get(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/message", timeout=10.0,
        )
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

    Used by long-lived calls (SDD-autonomous mode) and the config-drift
    gate (REQ-5): the serve's tool runner progressively wedges (bash hangs
    on trivial commands), and an SDD cycle can burn a full hour on a
    wedged runner.  A fresh serve at cycle start removes that risk.  The
    drift gate recycles when the serve template changes under a running
    serve.  Never raises.
    """
    try:
        port = OPENCODE_SERVE_URL.rsplit(":", 1)[-1]
        pid = await asyncio.to_thread(_find_serve_pid, port)
        if pid:
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


def _find_serve_pid(port: str) -> Optional[int]:
    """Locate the opencode serve process pid by scanning /proc cmdlines."""
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as fh:
                    cmd = fh.read().decode("utf-8", "ignore")
                # /proc/<pid>/cmdline separates argv with NUL bytes, not
                # spaces — the old space-form match NEVER matched any
                # process, so serve recycling (age, low-memory, wedge
                # recovery) silently did nothing and a wedged tool runner
                # lived forever.  Normalize NULs to spaces and tokenize
                # before matching.  PORT MATCHING IS EXACT ONLY: neither a
                # longer advertised value (--port 189990 vs search "18999")
                # nor a shorter search (--port 18999 vs search "1899") may
                # match (prefix-false-positive bug, cycle 9).
                tokens = cmd.replace("\x00", " ").split()
                if not any("opencode" in tok for tok in tokens) or "serve" not in tokens:
                    continue
                if f"--port={port}" in tokens:
                    return int(entry)
                for i, tok in enumerate(tokens):
                    if (
                        tok == "--port" and i + 1 < len(tokens)
                        and tokens[i + 1] == port
                    ):
                        return int(entry)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return None


async def _abort_zombie_sessions(
    client: httpx.AsyncClient,
    protected_ids: Optional[set[str]] = None,
) -> None:
    """Abort sessions that are stuck (busy with no recent activity).

    The opencode serve processes agent sessions through a sequential tool
    runner: a hung tool (e.g. an agent bash call that never completes)
    leaves the session busy FOREVER and blocks every later task behind it —
    which made the bridge look like it "consistently fails" after one
    zombie session accumulated.  A busy session whose last update is older
    than a few minutes is stuck, not working; abort it so new tasks get a
    free slot.  Never raises (best-effort hygiene).

    ``protected_ids`` holds sessions currently pinned as active
    conversations in the session map.  Pinned sessions sit idle BY DESIGN
    between user messages (clarifying questions, multi-turn coding), so
    they are never swept here — the busy-status check at resume time
    handles the genuinely stuck pinned session instead.
    """
    try:
        resp = await client.get(f"{OPENCODE_SERVE_URL}/session", timeout=10.0)
        if resp.status_code != 200:
            return
        now_ms = int(time.time() * 1000)
        threshold_ms = 240_000
        serve_start_ms = await asyncio.to_thread(_serve_start_epoch_ms)
        for s in resp.json():
            sid = s.get("id")
            if not sid:
                continue
            if protected_ids and sid in protected_ids:
                # Pinned conversation waiting on the user — not a zombie.
                continue
            updated = ((s.get("time") or {}).get("updated") or 0)
            if updated and now_ms - updated > threshold_ms:
                if (
                    serve_start_ms is not None
                    and updated >= serve_start_ms
                    and await _session_busy_on_current_serve(
                        client, sid, serve_start_ms,
                    )
                ):
                    # Quiet but working (sub-agent phase) — not a zombie.
                    logger.debug(
                        "opencode session %s quiet but busy — skipped by sweep",
                        str(sid)[:16],
                    )
                    continue
                logger.warning(
                    "opencode zombie session %s idle for %.0fs — aborting",
                    str(sid)[:16], (now_ms - updated) / 1000,
                )
                try:
                    await client.post(
                        f"{OPENCODE_SERVE_URL}/session/{sid}/abort", timeout=10.0,
                    )
                except (httpx.HTTPError, OSError):
                    pass
    except (httpx.HTTPError, OSError, ValueError):
        return
