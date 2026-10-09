# Verification Report — bridge-cycle-9

- **Change**: `bridge-cycle-9` — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills
- **Mode**: Runtime code + tests (hermetic; no live serve)
- **Persistence**: openspec file (`openspec/changes/bridge-cycle-9/verify-report.md`); Engram mirror unavailable in this runtime — filesystem authoritative
- **Verified at**: HEAD `30be86b` (branch `sdd/opencode-bridge-sdd-reliability/pr-5`), working tree clean
- **Verification strategy**: source inspection of the landed fixes (grep/line anchors in `opencode_bridge.py`), targeted + full pytest runs, `py_compile` syntax check. Cross-checked against `proposal.md`, `specs/spec.md`, `design.md`, `tasks.md`.

## Tasks Status

All 17 tasks in `tasks.md` are `[x]` complete (Fix 1 code 1–5, Fix 1 tests 6–10, Fix 2 code/tests 11–12, Fix 3 code/tests 13–16, verification 17). No pending task blocks verification.

## Evidence — Landed Code (HEAD state)

| Fix | Anchor (opencode_bridge.py, current HEAD) | Present |
|---|---|---|
| Fix 1 | `seen_question_pids: set[str] = set()` per-call state `:869`; `resumed` seed call `:874-876` | ✅ |
| Fix 1 | `_seed_resumed_session_state` helper `:1567-1616` (best-effort GET, catches `(httpx.HTTPError, OSError, ValueError)`, never raises) | ✅ |
| Fix 1 | `_yield_part_deltas` kwarg `:1269`; question-branch short-circuit `:1310` (before fetch-retry loop); `_poll_session_deltas` kwarg `:1625`, pass-through `:1644`; both call sites `:976`, `:1222` | ✅ |
| Fix 2 | `_find_serve_pid` `:1847-1880`: NUL-normalized cmdline tokenized; requires `opencode` + `serve` tokens; exact `--port=<port>` token OR exact adjacent `("--port", port)` pair | ✅ |
| Fix 3 | `_DRAIN_PROBES = 4` `:1657`, `_DRAIN_PROBE_S = 0.25`; `_drain_serve_shutdown` `:1661-1672` (poll `is_opencode_serve_running()`, early break, never raises); called after verified kill at `:1702` (`_recycle_serve_if_low_memory`) and `:1724` (`_force_recycle_serve`) | ✅ |

Commit trail: Fix source landed in `08471ea` (bridge fixes rode along per apply-progress); the +479 test lines landed in `30be86b`; final state verified at HEAD `30be86b`, `git status --short` clean for both files.

## Test Evidence (fresh runs this verify phase)

```
$ .venv/bin/python -m pytest tests/test_opencode_bridge.py -q
153 passed in 15.82s            # 140 pre-existing + 13 new cycle-9 tests

$ .venv/bin/python -m pytest tests/ -q
449 passed in 16.78s            # full suite green

$ .venv/bin/python -m pytest tests/test_opencode_bridge.py -q -k "SeedResumed or ResumedSessionPolling or FindServePidExact or ServeDrain"
14 passed, 139 deselected      # cycle-9 classes execute (no skips, incl. real_recycle drain tests)

$ .venv/bin/python -m py_compile opencode_bridge.py tests/test_opencode_bridge.py
(clean — COMPILE_CLEAN)
```

New test surface: `_SeedPollClient` (`:1181`), `TestSeedResumedSessionState` (`:1303`, 2 tests), `TestResumedSessionPollingReplay` (`:1365`, 4 tests), `TestFindServePidExactMatch` (`:2365`, 3 tests), `TestServeDrain` (`:2451`, `@pytest.mark.real_recycle`, 4 tests) = 13 new tests; targeted filter returns 14 because `_find_serve_pid` NUL space-form coverage is shared.

## Compliance Matrix (specs/spec.md)

| Requirement | Scenario | Verdict | Evidence |
|---|---|---|---|
| REQ-1: Resumed-session polling replay prevention | Scenario-1 (resumed follow-up survives bus closure; no history replay, no stale-question stop, new deltas stream) | **PASS** | `TestResumedSessionPollingReplay.test_resumed_session_never_replays_history`: seeded old text/reasoning/tool/question parts suppressed; new-turn text part streamed; no "🧠/✅/⚠️" spam; no question delta; ends without network-error chunk. |
| REQ-1 | Scenario-2 (seed fetch fails safely) | **PASS** | `test_seed_failure_degrades_to_current_behavior` (seed_status=500) and raising-client variant (`raise_on_seed=True`): stream completes without raising; `TestSeedResumedSessionState.test_seed_failure_never_raises` covers HTTPError + non-200. |
| REQ-1 | Scenario-3 (fresh session remains unseeded) | **PASS** | `test_fresh_session_does_not_seed`: first message-list GET occurs AFTER the prompt POST (polling only) — no pre-prompt seed GET for unpinned sessions. |
| REQ-2: Exact serve-PID port matching | Scenario-1 (equals-form `--port=18999` matches) | **PASS** | `TestFindServePidExactMatch.test_equals_form_matches`: cmdline `opencode\0serve\0--port=18999` → pid returned. |
| REQ-2 | Scenario-2 (digit prefixes rejected both ways) | **PASS** | `test_longer_advertised_value_rejected` (`--port 189990` vs search "18999" → None) and `test_shorter_search_rejected` (`--port 18999` vs search "1899" → None); existing NUL space-form test (`:1997-2042` area) still green. |
| REQ-3: Bounded drain after recycle kills | Scenario-1 (listener stops during drain) | **PASS** | `TestServeDrain`: scripted `is_opencode_serve_running` True×2 then False → 3 calls, early return, no raise — asserted for both `_force_recycle_serve` and `_recycle_serve_if_low_memory`. |
| REQ-3 | Scenario-2 (listener outlives budget) | **PASS** | always-up script → exactly 4 probes (loop `range(_DRAIN_PROBES)`), returns without raising; `hermetic_serve` autouse guard intact (normal tests noop the recycle helpers). |

## Correctness & Design Coherence

| Dimension | Result | Notes |
|---|---|---|
| All tasks complete | ✅ | 17/17 `[x]` in `tasks.md`. |
| Spec requirements covered | ✅ | All 3 requirements / 7 scenarios pass. |
| Design decisions followed | ✅ | D1 (Optional kwarg threading), D2 (seed after pin, before prompt), D3 (assistant ids in `user_mids`), D4 (short-circuit before fetch-retry), D5 (tokenizer), D6 (bounded drain constants), D7 (`_SeedPollClient`), D8 (`real_recycle` marker) — all implemented as specified; drain probe count reconciled to `range(_DRAIN_PROBES)` (task 15 note supersedes design §5.6 "5 calls" parenthetical — the loop calls once per probe). |
| Hermetic guard preserved | ✅ | `hermetic_serve` autouse noop intact; drain tests use `@pytest.mark.real_recycle` + fake pid 424242. |
| No regression | ✅ | 140 pre-existing bridge tests + full suite (431 → 449) all green. |
| AGENTS.md rules | ✅ | Dataclass/state conventions untouched; type hints present; no `print()`; no bare `except:` (typed catches only); no AI attribution in commits. |

## Issue Review

### CRITICAL
None.

### WARNING
None.

### SUGGESTION
- Apply-progress recorded the +461 test addition as "uncommitted in the working tree" at the time it was written; by verification time the tests were committed in `30be86b` (the commit also carried wedge-threshold + date-injection work). The working tree is now clean. No action needed — recorded for traceability.

## Command Evidence

- Test command: `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` → exit 0, 153 passed; `.venv/bin/python -m pytest tests/ -q` → exit 0, 449 passed.
- Targeted: `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q -k "SeedResumed or ResumedSessionPolling or FindServePidExact or ServeDrain"` → exit 0, 14 passed.
- Build/type-check: `.venv/bin/python -m py_compile opencode_bridge.py tests/test_opencode_bridge.py` → exit 0, no output.
- Source evidence: `git status --short` empty for `opencode_bridge.py` / `tests/test_opencode_bridge.py`; grep anchors per table above.

## Verdict

**PASS**

All requirements (REQ-1, REQ-2, REQ-3) and all seven scenarios pass with fresh test evidence at HEAD `30be86b`. The three fixes are landed, hermetic, and regression-free; the working tree is clean.
