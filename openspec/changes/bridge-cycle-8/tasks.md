# Tasks: Bridge Cycle 8 — Replay Prevention, Exact Serve-PID Match, Bounded Drain

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~268 |
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
| 1 | Fixes 1+2+3 (~268 lines) | PR 1 (single) | `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` | N/A — hermetic harnesses | `git revert`; only bridge + tests touched |

## Context

Apply-verification checklist: work already landed; design-scope only. Evidence refs: B = `opencode_bridge.py`, T = `tests/test_opencode_bridge.py`.

## Phase 1: Fix 1 — Resumed-Session Replay Prevention (REQ-1)

- [x] 1.1 `resumed = session_id is not None` before fresh-session POST — B:835.
- [x] 1.2 Per-call `seen_question_pids: set[str] = set()` beside `user_mids`/`text_lens`/`tool_state` — B:869.
- [x] 1.3 `_seed_resumed_session_state(client, session_id, user_mids, text_lens, tool_state, seen_question_pids)` (B:1567): best-effort GET `/session/{id}/message` (timeout 10.0); seeds `text_lens`/`tool_state`/`user_mids`/`seen_question_pids`; failures (non-200, bad body, HTTP/OSError/ValueError) return silently.
- [x] 1.4 Seed called only when `resumed`, before event-bus open + `prompt_async` — B:870-877.
- [x] 1.5 Both helpers gain `seen_question_pids: Optional[set[str]] = None`; poll forwards — B:1625/1269/1644.
- [x] 1.6 Question branch short-circuits pre-fetch: `if seen_question_pids and pid in seen_question_pids: return` — B:1310.
- [x] 1.7 Both call sites pass the set (B:976, B:1222); fresh sessions unchanged.
- [x] 1.8 Tests: `_SeedPollClient` (first-GET seed, `seed_status`/`raise_on_seed`); `TestSeedResumedSessionState` (`test_seeds_text_lens_tool_state_question_pids`, `test_seed_failure_never_raises`); `TestResumedSessionPollingReplay` (`test_resumed_session_never_replays_history`, `test_new_question_part_still_stops_stream`, `test_seed_failure_degrades_to_current_behavior`, `test_fresh_session_does_not_seed`) — T:1165-1550.

## Phase 2: Fix 2 — Exact Serve-PID Matching (REQ-2)

- [x] 2.1 `_find_serve_pid` tokenizes NUL-normalized cmdline; needs `opencode` token + exact `serve` token — B:1865-1867.
- [x] 2.2 Exact `f"--port={port}" in tokens` — B:1868.
- [x] 2.3 Exact adjacent `("--port", port)` pair, bounds-checked; no substring/prefix — B:1870-1875.
- [x] 2.4 Resilience kept: `except (OSError, ValueError): continue`, outer `except OSError` — B:1876-1879.
- [x] 2.5 Tests: NUL space-form green (`TestServeHealth.test_find_serve_pid_matches_nul_separated_cmdline`); new `TestFindServePidExactMatch` via `_FakeProcFile`/`_patch_proc_cmdline` (`test_equals_form_port_matches`, `test_longer_advertised_value_rejected`, `test_shorter_search_rejected`) — T:2262-2388.

## Phase 3: Fix 3 — Bounded Drain After Recycle Kills (REQ-3)

- [x] 3.1 `_DRAIN_PROBES = 4`, `_DRAIN_PROBE_S = 0.25` — B:1657-1658.
- [x] 3.2 `_drain_serve_shutdown()`: ≤4 `is_opencode_serve_running()` probes, early break, never raises — B:1661-1672.
- [x] 3.3 Called after kill inside `if pid:` in `_recycle_serve_if_low_memory` — B:1702.
- [x] 3.4 Called after kill inside `if pid:` in `_force_recycle_serve` — B:1724.
- [x] 3.5 `hermetic_serve` guard intact; tests use `@pytest.mark.real_recycle`, fake pid 424242, 0.01 s cadence — T:2435-2452.
- [x] 3.6 `TestServeDrain`: `test_force_recycle_drain_breaks_early_when_down` (3 calls), `test_force_recycle_drain_exhausts_budget_without_raising` (4 calls), `test_low_memory_recycle_drain_breaks_early_when_down`, `test_low_memory_recycle_drain_exhausts_budget_without_raising` — T:2455-2545.

## Phase 4: Verification

- [x] 4.1 `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` → 153 passed.
- [x] 4.2 `.venv/bin/python -m pytest tests/ -q` → full suite green; hermetic (no live serve).
- [x] 4.3 `git status` → `opencode_bridge.py` + `tests/test_opencode_bridge.py` + `openspec/changes/bridge-cycle-8/` only; no scope drift.
- [x] 4.4 Commit one PR: `test(bridge): cycle-8 replay/pid/drain regression tests`; `fix(bridge): seed resumed session state, exact serve-pid match, bounded drain`; no `Co-Authored-By`.
