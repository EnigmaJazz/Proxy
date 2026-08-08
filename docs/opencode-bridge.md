# The OpenCode Bridge

`opencode_bridge.py` (at the **repo root**) routes requests to a headless
`opencode serve` backend. A client that picks model `"opencode"`, embeds the
`/opencode` command, or triggers queue-worker cloud escalation directs the
request to the opencode agent — the full agentic loop with bash/edit/read
tools — instead of a local llama model. This document is the reference for the
module as it exists today. Code references use **identifier names**, never line
numbers.

## Overview — What the Bridge Does

The bridge translates a simple OpenAI-style task into the opencode serve HTTP
API. Two code paths exist, both defined in `opencode_bridge.py`:

- **Blocking** — `opencode_chat`: one fresh session per call, posts the message
  and blocks until the agent finishes, returns the collected assistant text.
- **Streaming** — `opencode_chat_stream`: sends the task with `prompt_async`
  (no-wait) and follows the live `/event` SSE bus, yielding content as it is
  generated. Pinned sessions, permission relay, and wedge detection live here.

The two paths are used by different entrypoints:

- `routes.py` routes `model: "opencode"` requests and the `/opencode` embedded
  command to `opencode_chat_stream`.
- `proxy.py`'s queue worker calls `opencode_escalation` for cloud escalation
  when local tiers are exhausted and `CLOUD_ESCALATION_BACKEND = "opencode"`;
  `opencode_escalation` delegates to `opencode_chat`.

Both paths create sessions against the headless serve and post-process the
collected assistant text with `_strip_proxy_status_text` to drop
sentinel-prefixed proxy-status content (triage) that the opencode client
accumulates as assistant text.

The HTTP surface the bridge speaks to:

```text
POST /session                          create a fresh session
POST /session/:id/message              run the agent (blocks until done)
POST /session/:id/prompt_async         start the agent without waiting
GET  /event                            SSE bus: live part/status/permission events
GET  /session/:id/message              poll a session's parts (SSE-close fallback)
GET  /session/:id/status               session busy/idle state
POST /session/:id/permissions/:id      answer a permission request
POST /session/:id/abort                stop a stuck session
GET  /permission                       list pending permission requests
GET  /config                           liveness probe
```

## Serve Lifecycle

The proxy owns the opencode serve process — no systemd unit required.

- **Startup** — `ensure_opencode_serve` checks `is_opencode_serve_running`
  (a `GET /config` probe) and spawns the serve if it is missing, returning True
  only when the backend answers. `_spawn_serve` launches `opencode serve` as a
  **detached child** (`create_subprocess_exec` with `start_new_session=True`)
  bound to `OPENCODE_SERVE_URL`, with `cwd=OPENCODE_WORKSPACE_DIR` and stdout/
  stderr writing to the serve log opened by `_open_serve_log`
  (`opencode-serve.log` under the workspace directory). The child's PATH is
  augmented with the usual user paths so the fallback plugin's `gentle-ai`
  binary resolves under systemd's minimal PATH.
- **Global config** — the serve reads the user's **GLOBAL** config
  `~/.config/opencode` (the same config the TUI uses; `OPCODE_CONFIG_PATH` =
  `~/.config/opencode/opencode.json`), so no config sync is needed.
  `OPENCODE_SERVE_PURE` toggles a documented fallback (`--pure`, no plugins).
- **Recycling** — `_recycle_serve_if_low_memory` kills the serve when
  `_memory_pressure` reports critically low memory (MemAvailable < 2,000,000 kB)
  or when the serve's uptime exceeds `_SERVE_RECYCLE_AFTER_S` (1800s): the
  serve's agent-loop tool runner progressively wedges (bash hangs on trivial
  commands even with healthy memory), and a fresh serve runs bash reliably.
  `_force_recycle_serve` kills the serve unconditionally — it is used before
  SDD-autonomous cycles and by the config-drift gate.
- **Config-drift gate** — `ensure_opencode_serve` compares the template mtime
  from `_config_mtime` against `_serve_config_mtime` cached at the last
  successful spawn; when the template is newer, the serve is recycled via
  `_force_recycle_serve` and respawned so the edited config actually loads.
  An unreadable template is treated as no-drift.
- **Helpers** — `_find_serve_pid` locates the serve process by scanning
  `/proc` cmdlines (NUL-normalized); `_serve_health` returns
  `(memory_pressure, serve_elapsed_seconds)` from `/proc` for the recycle
  decision.

## Blocking Path: `opencode_chat`

`opencode_chat(user_text, *, agent=OPENCODE_AGENT, model_id=None,
provider_id="kinver", timeout=OPENCODE_SERVE_TIMEOUT,
system_prompt=_BRIDGE_SYSTEM_PROMPT) -> str`

- Creates a **fresh session per call** (`POST /session` with
  `directory=OPENCODE_BRIDGE_DIRECTORY`), posts the message as one blocking
  `POST /session/:id/message` request, concatenates the returned `text` parts,
  and returns `_strip_proxy_status_text` of the result.
- **No permission relay, no session pinning, no wedge detection** — the path
  is a single request/response. It never raises: failures return an error
  string (escalation-friendly), e.g. `[OpenCode Bridge Error: ...]` or
  `[OpenCode Bridge Network Error: ...]`.
- **Queue-worker entrypoint** — `opencode_escalation(stage, prompt)` is the
  cloud-escalation fallback used by the queue worker when local tiers are
  exhausted and `CLOUD_ESCALATION_BACKEND = "opencode"` (the alternative is
  `"openrouter"`, the legacy OpenRouter failover). It calls `opencode_chat`
  with `agent=OPENCODE_AGENT`.

## Blocking vs Streaming — At a Glance

| | **Blocking** | **Streaming** | **Pinned follow-up** |
|---|---|---|---|
| **Trigger entrypoint** | Queue-worker escalation: `CLOUD_ESCALATION_BACKEND="opencode"` → `opencode_escalation` | `model: "opencode"` or the `/opencode` command in `routes.py` | Next request on an already-pinned conversation (`model: "opencode"` / `/opencode`) |
| **Reused function** | `opencode_chat` | `opencode_chat_stream` | `opencode_chat_stream` (same streaming fn) |
| **Session model** | Fresh session per call, discarded | Fresh session per conversation, pinned in `session_map` under `session_key` | Reused `session_id` from the pinned map (keeps tool state) |
| **Permission relay** | None | Yes — read auto-allowed, write/git relayed as pending questions | Yes — pending write/git asks answered on resume |
| **Output delivery** | Full assistant text blob at the end | Live SSE deltas: `text` / `reasoning` / `status` / `question` | Live SSE deltas |
| **Failure shape** | Error string in the return value | `status` tuples; wedged tool → abort session, kill serve, retry error | Same as streaming; busy pinned session aborted and restarted fresh |

## Streaming Path: `opencode_chat_stream`

`opencode_chat_stream(user_text, *, agent=OPENCODE_AGENT, model_id=None,
provider_id="kinver", session_map=None, session_key=None,
pending_permissions=None, just_approved_permission=False,
system_prompt=_BRIDGE_SYSTEM_PROMPT, timeout=OPENCODE_SERVE_TIMEOUT,
autonomous=False)` — an async generator yielding `(kind, text)` tuples:
`"text"` (assistant content), `"reasoning"` (thinking), `"status"`
(sentinel-prefixed feedback), `"question"` (the agent is waiting for user
input — stop streaming). On failure it yields a `status` tuple, never raises.

- **Transport** — uses `prompt_async` (no-wait send) plus the `/event` SSE
  bus, opened **before** sending the message: the bus is fire-and-forget (no
  replay), so connecting after `prompt_async` would miss the early events.
  `_yield_part_deltas` turns each part update into stream deltas — text
  increments per part id (`text_lens`), a one-time `🧠 thinking…` announcement
  per reasoning part (never the raw chain-of-thought), tool state transitions
  as `🔧 running` / `✅ done` / `⚠️ failed` chunks, `step-finish` markers, and
  `question` tool parts relayed losslessly (including multi-group SDD Session
  Preflight envelopes, fetched from the persisted message list when the event
  omits the input).
- **Pinned sessions** — when `session_map` + `session_key` are given, the
  opencode session is pinned per conversation: follow-ups reuse the SAME agent
  session (it keeps its tool state and remembers what it built). A pinned
  session that is still **busy** (a previously hung tool) is aborted, unpinned,
  and restarted fresh — unless `just_approved_permission` is set, because a
  session that just resumed after a permission approval is legitimately busy
  executing the approved tool. A `question` yield stops the stream; the
  session stays pinned so the next request posts the user's answer and
  continues.
- **Autonomous mode** — `autonomous=True` is an **explicit flag** used when
  the client picks model `"opencode-sdd"`; it is NOT inferred from timeouts.
  The system prompt switches to `_SDD_AUTONOMOUS_SYSTEM_PROMPT`, which drives
  the orchestrator to run the COMPLETE SDD cycle in one long-lived turn (no
  clarifying questions, no per-phase chat). Before anything else it calls
  `_force_recycle_serve` so the cycle never runs on a progressively-wedged
  tool runner.
- **Lifecycle within a stream** — `_recycle_serve_if_low_memory` runs first,
  then `_abort_zombie_sessions` (protecting the pinned sessions), then the
  session is created or reused. Quiet "still working…" keepalives are emitted
  so client stall detectors (e.g. nanobot killing streams silent for 90s)
  never trip during long tool phases.

## Permission Relay

A headless serve has no interactive user to answer permission gates: an
unanswered gate silently auto-rejects **and** leaves the tool wedged in
"running" — the root cause of the bridge wedges. The relay solves this for the
relayed types in `_RELAYED_PERMISSION_TYPES` (`external_directory`, `bash`,
`write`, `edit`), which arrive either as `permission.updated` SSE events or as
`GET /permission` records (write/edit tool gates may omit the SSE event
entirely — the polling record carries the type instead).

`_handle_permission_event` is **the single permission handler**, shared by all
four relay paths (the polling-wedge check, the event-bus-timeout check, the
`permission.updated` SSE event, and the completion resolver) so the
read→auto-allow / write→relay / autonomous→auto-allow policy lives in one
place:

1. **Autonomous mode** auto-allows everything: POST `"always"` with no client
   surface and no pending state.
2. **READ** external access is auto-allowed (POST `"always"`). Classification
   is type-aware first — `_classify_permission_access` treats any
   `_WRITE_TOOL_TYPES` type (`write`/`edit`/`patch`/...) as WRITE by
   definition, and falls back to `_classify_external_access` for bash
   commands: a bounded heuristic (any redirect `>` or a known mutating token
   from `_EXTERNAL_WRITE_TOKENS` ⇒ write; `cat`/`ls`/`head` reads ⇒ read).
3. **WRITE / git** asks are relayed to the user: the permission is recorded in
   `pending_permissions` **keyed by session id**, and a question string is
   built from `_PERMISSION_QUESTION_TEMPLATE` (external_directory) or
   `_GIT_PERMISSION_QUESTION_TEMPLATE` (bash git ask-rules — every git command
   is mutating, so all are relayed). `_permission_target` produces the
   human-readable target (exact file path, else parent directory, else the
   matched pattern).

The question shapes the user sees:

```text
🔒 The coding agent wants to access a path outside its workspace.
Target: {target}
Command: {cmd}
Reply `allow` (this once), `always` (auto-allow access like this), or `reject`.

🔒 The coding agent wants to run a git command.
Command: {cmd}
Reply `allow` (this once), `always` (auto-allow git commands like this), or `reject`.
```

When the next request resumes the pinned session, `_parse_permission_answer`
maps the user's reply to `once` / `always` / `reject` ("always" wins;
`reject`/`deny`/a bare "no" rejects). For WRITE-class permissions the default
is STRICT: only an explicit allow/always grants — anything short of an
explicit yes is a no. The decision is posted back with
`_post_permission_response` (best-effort, never raises).

`_detect_pending_permission` polls `GET /permission` for a pending relayed
request and is checked **before** the wedge detector fires: a write simply
waiting for the user looks identical to a wedged tool in the message list
(status `running`, no output), and must be relayed rather than killed.

## Completion Model

Streaming completion is a **finished step AND a session idle** — quiet alone is
not enough, because a multi-step agent can pause between steps while still
busy.

- **Quiet periods** — while the SSE bus is open, the stream waits
  `_EVENT_QUIET_TIMEOUT` (8s) for the next line after a step-finish, and
  `_EVENT_FINAL_TIMEOUT` (3s) once a step has finished (the final summary
  follows the last tool step within milliseconds). A `session.status` event
  (`busy`/`idle`) tracks whether the agent is still working; completion returns
  only on a finished step with the session idle.
- **Polling fallback** — when the `/event` bus closes (the serve closes idle
  connections) but the session may still be working, `_poll_session_deltas`
  polls the session's message list. Two consecutive poll cycles with a
  step-finish and zero new content mean done; a tool still in `running` keeps
  the stream alive. A parked WRITE permission is resolved (relayed) before
  returning.
- **Total-duration bound** — the stream caps the wait at `timeout`
  (`OPENCODE_SERVE_TIMEOUT`; SDD-autonomous turns use the larger
  `OPENCODE_SDD_TIMEOUT`): on expiry it aborts the session, drops the pin, and
  yields a clear error status instead of streaming keepalives forever.
- **Wedge detection** — `_detect_wedged_tool` finds a tool part stuck in
  `running` with no output older than `_TOOL_WEDGE_AFTER_S` (120s), checked
  every `_WEDGE_CHECK_INTERVAL_S` (10s) on both the event-bus and polling
  paths. On a wedge the bridge aborts the session, drops the pin and pending
  permission, kills the serve (`_find_serve_pid` + SIGTERM, i.e. a
  `_force_recycle_serve` recycle), and yields
  `[OpenCode Bridge Error: agent tool runner wedged — session aborted, serve
  recycled. Please retry.]`.
- **Zombie sweep** — `_abort_zombie_sessions` aborts sessions busy with no
  update for 240,000 ms (≈ 4 minutes), skipping `protected_ids` (pinned active
  conversations, which sit idle by design between user messages).
- **Sentinel stripping** — `_strip_proxy_status_text` removes
  sentinel-prefixed proxy-status segments (`_STATUS_SEGMENT_RE`) from collected
  assistant text so the client never sees the proxy's triage chunk as model
  output.

## Configuration

The bridge's tunables live in `constants.py` (all keys exist verbatim):

| Key | Default | Meaning |
|---|---|---|
| `OPENCODE_SERVE_URL` | `http://127.0.0.1:18900` | Base URL of the headless opencode serve backend |
| `OPENCODE_WORKSPACE_DIR` | `~/opencode-workspace` | Serve working directory + serve log location — deliberately outside the proxy repo so agent writes never pollute the git tree |
| `OPENCODE_BRIDGE_DIRECTORY` | `<REPO_ROOT>` | Directory bridge sessions are created in (a real project, e.g. the proxy repo hosting the OpenSpec SDD store) |
| `OPENCODE_BIN` | `~/.opencode/bin/opencode` | Absolute path to the opencode binary (systemd PATH does not include `~/.opencode/bin`) |
| `OPENCODE_AGENT` | `gentle-orchestrator` | Agent used for coding tasks — the Gentle AI SDD orchestrator, not opencode's plain build agent |
| `OPENCODE_SERVE_TIMEOUT` | `600.0` | How long to wait for the opencode agent to finish a task |
| `OPENCODE_SERVE_PURE` | `False` | Toggle candidate A (`--pure`, no plugins) for the serve spawn |
| `OPCODE_CONFIG_PATH` | `~/.config/opencode/opencode.json` | Template whose mtime drives config-drift recycling (the user's GLOBAL config) |
| `BRIDGE_MODEL_KEYS` | `{"opencode", "opencode-sdd"}` | Bridge model keys exposed to clients, validated alongside all model keys |
| `OPENCODE_SDD_TIMEOUT` | `3600.0` | Time budget for one SDD-autonomous turn (the full cycle in one long-lived turn) |
| `CLOUD_ESCALATION_BACKEND` | `opencode` | Queue-worker escalation backend: `"opencode"` (bridge) or `"openrouter"` (legacy) |

Bridge-module constants (in `opencode_bridge.py`):

| Constant | Value | Meaning |
|---|---|---|
| `_SERVE_RECYCLE_AFTER_S` | `1800.0` | Serve uptime threshold — progressive tool-runner wedging |
| `_EVENT_QUIET_TIMEOUT` | `8.0` | Quiet period after a finished step before completion |
| `_EVENT_FINAL_TIMEOUT` | `3.0` | Faster quiet threshold once a step has finished |
| `_TOOL_WEDGE_AFTER_S` | `120.0` | Tool part stuck in `running` with no output ⇒ wedged |
| `_WEDGE_CHECK_INTERVAL_S` | `10.0` | How often the stream checks for a wedged tool part |
| `_BRIDGE_SYSTEM_PROMPT` | — | Default system prompt (English replies; clarifying questions allowed) |
| `_SDD_AUTONOMOUS_SYSTEM_PROMPT` | — | System prompt for model `"opencode-sdd"` — complete SDD cycle in one turn |
| `_RELAYED_PERMISSION_TYPES` | `external_directory, bash, write, edit` | Permission types relayed to the user |

## Gotchas

- **The module is at the REPO ROOT**: `opencode_bridge.py`, **NOT**
  `proxy/opencode_bridge.py` — that path does not exist and must not be
  created. The proxy-root layout is deliberate; `constants.py` is also imported
  from the repo root.
- **The serve HTTP API is version-specific**: the session/message/part and
  permission SSE surfaces are the opencode serve API as observed on
  **v1.18.15** (comments in the module cite "opencode >= 1.18" for the
  `_RELAYED_PERMISSION_TYPES` gate and the write/edit F2 note). This is an
  observed version, **not a stability promise** — the API surface may drift
  across opencode releases; treat it as version/time-specific.
- **The serve uses the user's GLOBAL config** (`~/.config/opencode`, same as
  the TUI) — editing it while a serve runs triggers the config-drift recycle
  in `ensure_opencode_serve` so the new config actually loads.
- **Autonomous mode is an explicit flag** for model `"opencode-sdd"` (the
  `autonomous` parameter), never inferred from timeouts — a long regular
  bridge turn is still interactive.
