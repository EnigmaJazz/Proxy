# Design: OpenCode Bridge Reference Documentation

## Technical Approach

Docs-only deliverable. The "design" is the **document structure + writing rules** for `docs/opencode-bridge.md`, not runtime architecture. One plain reference markdown file, structured per the proposal's seven scope areas, anchored to current `opencode_bridge.py` / `constants.py` by **identifier name**. Verified-verbatim config keys, a blocking-vs-streaming table between the two path sections, and an explicit observe-version drift warning.

## Architecture Decisions

| Decision | Option | Tradeoff | Choice & Rationale |
|---|---|---|---|
| Path presentation | Two sections + shared table vs one merged section | Merged is shorter; separate avoids the documented R44 confusion | **Separate sections + comparison table** — proposal risk #1 is conflating blocking/streaming |
| Code reference style | Line numbers vs identifier names | Line numbers are precise but rot on every edit | **Identifier names only** (REQ-DOC-3) — drift-safe |
| Serve config framing | Mention legacy serve-scoped XDG vs omit | Mentioning adds context but risks reuse | **Omit entirely; state global config** (obsolete since d9f7284) |
| Version statement | Pin a version vs qualitative | Pin reads as a contract | **State v1.18.15 observed + drift warning** — not a stability promise |

## Document Outline (docs/opencode-bridge.md)

Exact section order; each `##` maps to a REQ-DOC-2 scope area:

| # | Section heading | REQ-DOC-2 scope area |
|---|---|---|
| 1 | `# The OpenCode Bridge` (H1 title) | — |
| 2 | `## Overview — What the Bridge Does` | (1) OpenAI-style task → headless `opencode serve` HTTP API |
| 3 | `## Serve Lifecycle` | (2) detached child, global config, recycling triggers |
| 4 | `## Blocking Path: \`opencode_chat\`` | (3a) fresh session, no permission relay, queue-worker escalation |
| 5 | `## Blocking vs Streaming — At a Glance` | **comparison table between the two path sections** (REQ-DOC-2 Scenario-1) |
| 6 | `## Streaming Path: \`opencode_chat_stream\`` | (3b) pinned sessions, permission relay, wedge detection, `/opencode` |
| 7 | `## Permission Relay` | (4) external_directory gate, write/git relay, autonomous auto-allow |
| 8 | `## Completion Model` | (5) quiet periods, polling fallback, wedge/zombie, sentinel strip |
| 9 | `## Configuration` | (6) keys from `constants.py` + bridge module constants |
| 10 | `## Gotchas` | (7) repo-root module path, version drift |

**Comparison table columns**: Trigger entrypoint · Reused function · Session model · Permission relay · Output delivery · Failure shape. Rows: blocking (`/opencode` queue-worker escalation, `CLOUD_ESCALATION_BACKEND="opencode"`, `opencode_chat`); streaming (`model: "opencode"` / `/opencode` command, `opencode_chat_stream`); pinned follow-up (same streaming fn, reused `session_id`).

## Content Sourcing Map

Every section names its source-of-truth identifiers (apply MUST read these to write the prose):

| Section | Source of truth (`opencode_bridge.py` unless noted) |
|---|---|
| Overview | `opencode_chat`, `opencode_chat_stream`, `opencode_escalation`; HTTP surfaces `POST /session`, `POST /session/:id/message`, `prompt_async`, `/event` SSE (referenced inside `opencode_chat_stream`) |
| Serve Lifecycle | `ensure_opencode_serve`, `_spawn_serve`, `is_opencode_serve_running`; recycle: `_recycle_serve_if_low_memory` (via `_memory_pressure` < 2_000_000 kB, `_serve_health`), `_force_recycle_serve` (uptime gate `_SERVE_RECYCLE_AFTER_S` 1800s); drift gate `ensure_opencode_serve` + `_config_mtime` / `_serve_config_mtime` (template `OPCODE_CONFIG_PATH`); before-SDD recycle (`autonomous`→`_force_recycle_serve`); helpers `_find_serve_pid`, `_open_serve_log` |
| Blocking Path | `opencode_chat` (fresh session per call; blocking `POST /session/:id/message`; `_strip_proxy_status_text`); queue-worker entrypoint `opencode_escalation`; `CLOUD_ESCALATION_BACKEND` selection lives in queue worker (routes.py + constants.py) |
| Comparison Table | `opencode_chat` vs `opencode_chat_stream` signatures (`autonomous`, `session_map`, `session_key`, `pending_permissions`) |
| Streaming Path | `opencode_chat_stream` (`prompt_async` + `/event` SSE; pinned session reuse via `session_map`/`session_key`; `autonomous` flag); `_yield_part_deltas`, `_poll_session_deltas`; `routes.py` `model: "opencode"` / `/opencode` (lines 784–796, 2346–2348); `_BRIDGE_SYSTEM_PROMPT` default, `_SDD_AUTONOMOUS_SYSTEM_PROMPT` for `sdd=True` |
| Permission Relay | `_handle_permission_event`, `_classify_external_access`, `_classify_permission_access`, `_post_permission_response`, `_parse_permission_answer`, `_permission_target`; `_RELAYED_PERMISSION_TYPES` (`external_directory`, `bash`, `write`, `edit`); `_EXTERNAL_WRITE_TOKENS`, `_WRITE_TOOL_TYPES`, `_PERMISSION_QUESTION_TEMPLATE`, `_GIT_PERMISSION_QUESTION_TEMPLATE`; autonomous auto-allow (`autonomous` branch) |
| Completion Model | `_EVENT_QUIET_TIMEOUT` 8s (step-finish quiet), `_EVENT_FINAL_TIMEOUT` 3s (session-idle quiet), `_TOOL_WEDGE_AFTER_S` 120s, `_WEDGE_CHECK_INTERVAL_S` 10s; `_detect_wedged_tool` (abort + `_force_recycle_serve`), `_abort_zombie_sessions` (240_000 ms ≈ 4 min sweep, skips `protected_ids`), `_poll_session_deltas` (SSE-close polling fallback); `_strip_proxy_status_text` + `_STATUS_SEGMENT_RE` sentinel strip |
| Configuration | `constants.py` (verbatim): `OPENCODE_SERVE_URL`, `OPENCODE_WORKSPACE_DIR`, `OPENCODE_BRIDGE_DIRECTORY`, `OPENCODE_BIN`, `OPENCODE_AGENT`, `OPENCODE_SERVE_TIMEOUT`, `OPENCODE_SERVE_PURE`, `OPCODE_CONFIG_PATH`, `BRIDGE_MODEL_KEYS`, `OPENCODE_SDD_TIMEOUT`, `CLOUD_ESCALATION_BACKEND`. Bridge module constants: `_SERVE_RECYCLE_AFTER_S`, `_EVENT_QUIET_TIMEOUT`, `_EVENT_FINAL_TIMEOUT`, `_TOOL_WEDGE_AFTER_S`, `_WEDGE_CHECK_INTERVAL_S`, `_BRIDGE_SYSTEM_PROMPT`, `_SDD_AUTONOMOUS_SYSTEM_PROMPT`, `_RELAYED_PERMISSION_TYPES` |
| Gotchas | module at **repo root** (`opencode_bridge.py`, NOT `proxy/opencode_bridge.py`); serve HTTP API version-specific — comments cite "opencode >= 1.18" / "1.18.15" (`_RELAYED_PERMISSION_TYPES` comment, `_handle_permission_event` F2 note); `OPENCODE_AGENT = "gentle-orchestrator"` |

## Writing Guidelines

1. Reference **function/identifier names**, never line numbers (REQ-DOC-3).
2. NEVER mention `OPENCODE_SERVE_CONFIG_DIR` or "serve-scoped XDG/config". State serve uses the **global** config (`~/.config/opencode`, same as TUI) — sourced from `_spawn_serve` docstring + `OPCODE_CONFIG_PATH` comment.
3. Autonomous mode is an **explicit flag** for model `"opencode-sdd"` (the `autonomous` param; `_SDD_AUTONOMOUS_SYSTEM_PROMPT`), NOT a timeout inference.
4. State the observed opencode serve HTTP API version (**v1.18.15**) with a **drift warning** — the API surface (session/message/permission SSE) is version-specific and may change.
5. Every config key MUST exist verbatim in `constants.py`. The 11 keys above are the verified set; do NOT invent keys. Numeric thresholds (`_EVENT_QUIET_TIMEOUT` etc.) cite the bridge module constant by name.
6. Length target: **~250–400 lines** of plain markdown. Single file, English, no generated-site scaffold.
7. Use fenced code blocks ONLY for the HTTP sequence and the permission template shapes; keep prose dense.

## Verification Plan (apply-phase checks)

| Scenario | Concrete check |
|---|---|
| REQ-DOC-1 Scenario-1 | `test -f docs/opencode-bridge.md`; `git diff --name-only` shows NO edits to `opencode_bridge.py`, `routes.py`, `constants.py`, `tests/`, `AGENTS.md`, any `README*` |
| REQ-DOC-2 Scenario-1 | Heading audit: grep `^## ` yields all seven scope headings; a comparison table exists between the two path sections |
| REQ-DOC-2 Scenario-2 | Doc text names queue-worker escalation (`CLOUD_ESCALATION_BACKEND="opencode"` → `opencode_chat`), the `/opencode` command + `model: "opencode"` → `opencode_chat_stream`, and pinned follow-up (reused `session_id`) |
| REQ-DOC-3 Scenario-1 | `grep -nE 'OPENCODE_SERVE_CONFIG_DIR|serve-scoped' docs/opencode-bridge.md` returns nothing; global-config behavior stated |
| REQ-DOC-3 Scenario-2 | For each listed config key: `grep -nE '^KEY:' constants.py` confirms verbatim existence; no fabricated key |
| REQ-DOC-3 Scenario-3 | Completion-model section names `_detect_wedged_tool` (120s + abort+kill recycle), `_abort_zombie_sessions` (4-min sweep), `_force_recycle_serve` by identifier — no line numbers |

## No-Code-Change Guard

Apply MUST NOT touch `opencode_bridge.py`, `routes.py`, `constants.py`, `tests/`, `AGENTS.md`, or any README. The deliverable is the single file `docs/opencode-bridge.md` (new). Any code modification is out of scope and breaks REQ-DOC-1.

## File Changes

| File | Action | Description |
|---|---|---|
| `docs/opencode-bridge.md` | Create | Single English reference markdown, ~250–400 lines, seven scope sections + comparison table |
| all others | — | Untouched (no-code-change guard) |

## Interfaces / Contracts

N/A — no new runtime interfaces; documentation only.

## Testing Strategy

| Layer | What to Test | Approach |
|---|---|---|
| "Unit" | Banned strings absent | `grep` checks above |
| "Unit" | Config keys verbatim | key-existence cross-check vs `constants.py` |
| "Integration" | Structure | heading audit + table placement |
| E2E | — | N/A (docs-only) |

## Threat Matrix

N/A — no routing, shell, subprocess, VCS/PR automation, executable-file classification, or process-integration boundary. Pure documentation.

## Migration / Rollout

No migration required. Rollback = delete `docs/opencode-bridge.md` (or revert the docs PR). File-only, zero runtime risk.

## Open Questions

None. All sourcing identifiers and config keys verified verbatim against current code at design time.