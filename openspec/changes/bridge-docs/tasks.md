# Tasks: OpenCode Bridge Reference Documentation

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~300–400 (one new file) |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | Single PR |
| Delivery strategy | single-pr |
| Chain strategy | pending (n/a — single-pr) |

```text
Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: pending
400-line budget risk: Low
```

## TDD Note

Docs-only change — no runtime code. "Tests" are the design's verification-plan grep/audit checks (Task 5), not pytest.

## Phase 1: Write Document Sections

### [x] Task 1 — Scaffold, Overview, Serve Lifecycle
- **Description**: Create `docs/opencode-bridge.md` (plain English markdown, ~250–400 lines) with H1 `# The OpenCode Bridge`; `## Overview — What the Bridge Does` (identifiers `opencode_chat`, `opencode_chat_stream`, `opencode_escalation`; HTTP surfaces `POST /session`, `POST /session/:id/message`, `prompt_async`, `/event` SSE); `## Serve Lifecycle` (detached child via `ensure_opencode_serve`/`_spawn_serve`; GLOBAL config `~/.config/opencode`; recycle via `_recycle_serve_if_low_memory`, `_force_recycle_serve` 1800s uptime, config mtime drift; helpers `_find_serve_pid`, `_open_serve_log`). Identifier names only, never line numbers; never mention `OPENCODE_SERVE_CONFIG_DIR` or serve-scoped config.
- **Acceptance**: Sections 1–3 present with named identifiers; banned string absent.
- **Evidence**: `grep -nE 'OPENCODE_SERVE_CONFIG_DIR|serve-scoped' docs/opencode-bridge.md` returns nothing.

### [x] Task 2 — Blocking Path, Comparison Table, Streaming Path
- **Description**: `## Blocking Path: opencode_chat` (fresh session per call, blocking `POST /session/:id/message`, `_strip_proxy_status_text`; queue-worker entry `opencode_escalation` with `CLOUD_ESCALATION_BACKEND="opencode"`). `## Blocking vs Streaming — At a Glance` table (columns: Trigger entrypoint · Reused function · Session model · Permission relay · Output delivery · Failure shape; rows: blocking, streaming, pinned follow-up with reused `session_id`). `## Streaming Path: opencode_chat_stream` (`prompt_async` + `/event` SSE; pinned reuse via `session_map`/`session_key`; `autonomous` flag; `_yield_part_deltas`, `_poll_session_deltas`; `model: "opencode"` / `/opencode`).
- **Acceptance**: Both paths separate sections with the comparison table between them; triggers named unambiguously.
- **Evidence**: Heading audit (`grep '^## '`) shows both path sections with table between; prose names the three entry triggers.

### [x] Task 3 — Permission Relay, Completion Model
- **Description**: `## Permission Relay`: `_handle_permission_event`, `_classify_external_access`, `_classify_permission_access`, `_post_permission_response`, `_permission_target`; `_RELAYED_PERMISSION_TYPES` (external_directory read auto-allow; write/git relayed as pending questions keyed by session id via `_PERMISSION_QUESTION_TEMPLATE`/`_GIT_PERMISSION_QUESTION_TEMPLATE`); autonomous auto-allow. `## Completion Model`: `_EVENT_QUIET_TIMEOUT` 8s, `_EVENT_FINAL_TIMEOUT` 3s, `_poll_session_deltas` SSE-close polling fallback, `_detect_wedged_tool` 120s (abort + `_force_recycle_serve`), `_abort_zombie_sessions` 4-min sweep, `_strip_proxy_status_text`/`_STATUS_SEGMENT_RE` sentinel strip.
- **Acceptance**: REQ-DOC-3 Scenario-3 identifiers present; no line numbers.
- **Evidence**: Grep finds `_detect_wedged_tool`, `_abort_zombie_sessions`, `_force_recycle_serve` in the doc.

### [x] Task 4 — Configuration, Gotchas
- **Description**: `## Configuration`: the 11 verbatim `constants.py` keys (`OPENCODE_SERVE_URL`, `OPENCODE_WORKSPACE_DIR`, `OPENCODE_BRIDGE_DIRECTORY`, `OPENCODE_BIN`, `OPENCODE_AGENT`, `OPENCODE_SERVE_TIMEOUT`, `OPENCODE_SERVE_PURE`, `OPCODE_CONFIG_PATH`, `BRIDGE_MODEL_KEYS`, `OPENCODE_SDD_TIMEOUT`, `CLOUD_ESCALATION_BACKEND`) + bridge constants (`_SERVE_RECYCLE_AFTER_S`, `_EVENT_QUIET_TIMEOUT`, `_EVENT_FINAL_TIMEOUT`, `_TOOL_WEDGE_AFTER_S`, `_WEDGE_CHECK_INTERVAL_S`, `_BRIDGE_SYSTEM_PROMPT`, `_SDD_AUTONOMOUS_SYSTEM_PROMPT`, `_RELAYED_PERMISSION_TYPES`). `## Gotchas`: module at repo root (NOT `proxy/opencode_bridge.py`); opencode serve HTTP API version-specific — state v1.18.15 observed + drift warning.
- **Acceptance**: Every listed key exists verbatim in `constants.py`; no invented key; autonomous described as an explicit flag for `"opencode-sdd"`.
- **Evidence**: For each key, `grep -nE '^KEY:' constants.py` matches.

## Phase 2: Verification & Commit

### [x] Task 5 — Verification plan + conventional commit
- **Description**: Apply the design verification plan: `test -f docs/opencode-bridge.md`; banned-string grep; per-key existence greps; heading audit (7 scope headings + table placement); `git diff --name-only` shows ONLY `docs/opencode-bridge.md` (no-code-change guard). Commit `docs(bridge): add opencode bridge reference doc` — no `Co-Authored-By` or AI attribution.
- **Acceptance**: All checks pass; diff touches only the new doc; commit message conventional.
- **Evidence**: Command outputs + `git show --stat` of the commit.
