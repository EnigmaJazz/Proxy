# Tasks: Bridge Cycle 7 — Polling Replay + Serve-PID Matching

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~150–200 |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | Single PR |
| Delivery strategy | single-pr |
| Chain strategy | pending |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: pending
400-line budget risk: Low

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | RED seed-replay + PID tests | RED | `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` (new fail, existing green) | N/A — hermetic fakes; `hermetic_serve` guard | `git revert` RED commit |
| 2 | GREEN seeding + threading + tokenize | GREEN | same command | N/A — same as unit 1 | `git revert`; bridge + tests only |

## Context

- Strict TDD (`strict_tdd: true`): Phase 1 RED before Phase 2 edits.
- Never kill a real serve — `hermetic_serve` autouse guard; do NOT touch `opencode_chat`, routes.py, proxy.py, config, systemd, error strings, cycle-5/6 helpers.
- AGENTS.md: type hints, no `print()`, no bare `except`, conventional commits, no `Co-Authored-By`, regression tests mandatory.

## Phase 1: RED Regression Tests (TDD — tests first)

- [x] 1.1 `_PollClient` (:1151-1162): add `busy_polls` `/session/status` override (per `_BusyThenIdleClient` :156); no new variant.
- [x] 1.2 New `TestResumedSessionSeeding` after `TestStreamExitHygiene` (:550) — `@pytest.mark.asyncio`; monkeypatch `ensure_opencode_serve` → `_running`, `httpx.AsyncClient` → scripted client; fresh `session_map`/`pending` (NOT `PP`).
- [x] 1.3 `test_resume_seed_suppresses_history_replay` — pinned `session_map={"conv": "ses_0001"}`, `stream_lines=[]`, old text/tool/reasoning/question parts; 2nd cycle superset-text + busy-then-idle → no replay, no premature stop, seed GET first, only suffix streams.
- [x] 1.4 `test_resume_seed_failure_degrades` — seed GET fails (`get_status=500`/`raise_timeout_on`) → no raise.
- [x] 1.5 `test_fresh_session_no_seed_get` — `session_map={}` → no message-list GET in calls.
- [x] 1.6 `test_new_question_part_still_stops_stream` — new-pid question → `("question", ...)` + stop.
- [x] 1.7 `TestServeHealth` (:1957): `test_find_serve_pid_matches_equals_form` (`--port=18999` → pid); `test_find_serve_pid_rejects_prefix_port` (189990/18999 both ways → None); NUL test (:1997) untouched.
- [x] 1.8 Verify RED: new tests FAIL, existing green.

## Phase 2: GREEN — opencode_bridge.py Edits

- [x] 2.1 Add `_seed_stream_part_state(client, session_id, user_mids, text_lens, tool_state, seen_question_pids)` before `opencode_chat_stream` :736 per §1; `except (httpx.HTTPError, OSError, ValueError): logger.debug(...)`; never raises.
- [x] 2.2 `:849-852`: add `seen_question_pids: set[str] = set()`; seed after create/resume block, before `client.stream(...)` :859, if `session_id and session_map.get(session_key)`.
- [x] 2.3 `_poll_session_deltas` (:1535): add `seen_question_pids: Optional[set[str]] = None`; pass through (:1557-1559).
- [x] 2.4 `_yield_part_deltas` (:1238): same param; question branch (:1279) short-circuit before fetch-retry (:1309): `if seen_question_pids and pid in seen_question_pids: return`.
- [x] 2.5 Pass set at call sites — polling :951-953, event-bus :1196-1198.
- [x] 2.6 `_find_serve_pid` (:1741-1762): replace substring matcher (:1756) — NUL→space, tokenize, exact `--port`/next or `--port={port}`; keep opencode/serve guards + try/except.
- [x] 2.7 Verify GREEN: full bridge suite green.

## Phase 3: Verification

- [x] 3.1 `.venv/bin/python -m pytest tests/ -q` → full suite green (hermetic guard: no live serve).
- [x] 3.2 `git status` → only `opencode_bridge.py`, `tests/test_opencode_bridge.py`, `openspec/changes/bridge-cycle-7/`, `openspec/specs/opencode-bridge-polling-replay-prevention/`.
- [x] 3.3 Commits: RED `test(bridge): resume-seeding regression tests + pid equals-form cases`; GREEN `fix(bridge): seed stream part state on resumed sessions — no polling history replay`, `fix(bridge): match serve pid --port= equals form and exact tokens`; stage intended files; no `Co-Authored-By`.
