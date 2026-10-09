# Verify Report — bridge-cycle-8

- **Change**: `bridge-cycle-8` — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills
- **Date**: 2026-08-09
- **Phase status**: PASS (all requirements verified, hermetic, no live serve)
- **Verifier**: SDD verify phase (inline continuation — sub-agent delegation interrupted; performed by orchestrator per fallback rule)
- **Persistence**: openspec file (`openspec/changes/bridge-cycle-8/verify-report.md`); Engram unavailable in this runtime

## Verification Commands

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` | **153 passed** in 15.7s |
| `.venv/bin/python -m pytest tests/ -q` | **449 passed** in 16.9s |
| `.venv/bin/python -m py_compile opencode_bridge.py tests/test_opencode_bridge.py` | clean |

Full-suite count grew from the apply-time 444 to 449 because two post-apply commits
(`99c660c` scripts, `6e6f640` routes) added tests; the bridge suite count (153) is
identical to the apply-time evidence. No cycle-8 test is skipped or xfailed.

## Requirements Compliance Matrix

| Req | Requirement | Evidence | Verdict |
|---|---|---|---|
| REQ-1 | Resumed-session polling replay prevention | `_seed_resumed_session_state` (`opencode_bridge.py:1634`) performs one best-effort `GET /session/{id}/message` (timeout 10.0); seeds `text_lens`/`tool_state`/`user_mids`/`seen_question_pids`; non-200 / non-list / malformed bodies return silently; `except (httpx.HTTPError, OSError, ValueError)` never raises. `resumed = session_id is not None` captured before the fresh-session POST; seed called only when resumed, before event-bus open + `prompt_async`. `seen_question_pids: Optional[set[str]] = None` on `_yield_part_deltas` (`:1336`) and `_poll_session_deltas` (`:1692`); forwarded at `:1711`; both call sites pass the set (`:1043`, `:1289`); question-branch short-circuit `if seen_question_pids and pid in seen_question_pids: return` (`:1310`) BEFORE the fetch-retry loop. Tests: `_SeedPollClient`, `TestSeedResumedSessionState` (2), `TestResumedSessionPollingReplay` (4) — all green. | PASS |
| REQ-1 S1 | Resumed follow-up survives bus closure, no history replay, new question stops | `test_resumed_session_never_replays_history` + `test_new_question_part_still_stops_stream` green | PASS |
| REQ-1 S2 | Seed fetch fails safely, degrades without raising | `test_seed_failure_degrades_to_current_behavior` + `test_seed_failure_never_raises` green | PASS |
| REQ-1 S3 | Fresh session remains unseeded | `test_fresh_session_does_not_seed` green | PASS |
| REQ-2 | Exact serve-PID port matching | `_find_serve_pid` (`:1847`): tokenizes NUL-normalized cmdline (`cmd.replace("\x00", " ").split()`); requires an `opencode`-containing token AND exact `serve` token; matches exact `--port={port}` token OR exact adjacent `("--port", port)` pair with bounds check; no substring/prefix matching; malformed entries skipped via `except (OSError, ValueError): continue`, outer `except OSError` retained. Tests: `TestFindServePidExactMatch` (3) + pre-existing NUL space-form test (`TestServeHealth.test_find_serve_pid_matches_nul_separated_cmdline`) all green. | PASS |
| REQ-2 S1 | Equals-form `--port=18999` matches | `test_equals_form_port_matches` green | PASS |
| REQ-2 S2 | Digit prefixes rejected both ways | `test_longer_advertised_value_rejected` + `test_shorter_search_rejected` green | PASS |
| REQ-3 | Bounded drain after recycle kills | `_DRAIN_PROBES = 4` (`:1724`), `_DRAIN_PROBE_S = 0.25` (`:1725`); `_drain_serve_shutdown()` (`:1661`): ≤4 `is_opencode_serve_running()` probes at ~0.25s, early break when down, never raises. Called after the verified kill inside `if pid:` in `_recycle_serve_if_low_memory` (`:1702`) and `_force_recycle_serve` (`:1724`). `hermetic_serve` autouse guard intact. Tests: `TestServeDrain` (`@pytest.mark.real_recycle`, fake pid 424242, 0.01 s cadence; 4 tests) all green. | PASS |
| REQ-3 S1 | Listener stops during drain, early break after probe 3 | `test_force_recycle_drain_breaks_early_when_down` (3 running-fn calls) green | PASS |
| REQ-3 S2 | Listener outlives budget, returns without raising | `test_force_recycle_drain_exhausts_budget_without_raising` (4 calls) + low-memory variants green | PASS |

## Scope Compliance

- Only `opencode_bridge.py` (committed at `08471ea`) + additive tests in
  `tests/test_opencode_bridge.py` (uncommitted, +477 lines) + cycle-8 OpenSpec
  artifacts. No routes.py / proxy.py / config / systemd changes. Blocking
  `opencode_chat` path untouched. Fresh-session and event-bus behavior unchanged.

## Residual Risks / Notes

- **Stale-question suppression is seed-scoped**: a question part created AFTER the
  prompt (new pid) still yields and stops the stream — intended behavior.
- **Seed latency**: one extra round-trip per resumed request (≤10 s worst case,
  typically <100 ms), bounded and best-effort.
- **Seed shape drift**: if the message-list body shape changes, replay may recur —
  exactly the pre-change behavior, never worse.
- **Unrelated in-flight work preserved**: an out-of-scope bridge refactor
  (blocking-path respawn `_opencode_chat_attempt`, pid-reuse guards
  `_pid_is_serve`/`_cmdline_matches_serve`) was stashed during verification
  (`git stash` entry "in-flight: blocking-path respawn + pid-reuse guards…")
  because its real-/proc pid re-verification breaks the scripted-fake-pid drain
  tests. It is NOT part of cycle-8 scope and is NOT included in this change.

## Verdict

**PASS** — 153/153 bridge tests, 449/449 full suite, compile clean, REQ-1/2/3 and all
six scenarios verified against the current tree, hermetic (no live serve), no scope drift.
