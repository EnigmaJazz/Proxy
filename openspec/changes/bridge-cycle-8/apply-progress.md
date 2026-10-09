# Apply Progress — bridge-cycle-8

- **Change**: `bridge-cycle-8` — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills
- **Status**: COMPLETE (implementation landed, tests green, pending verify/archive)
- **Persistence**: openspec file (`openspec/changes/bridge-cycle-8/apply-progress.md`); Engram unavailable in this runtime

## Implementation State

All three fixes are implemented in `opencode_bridge.py` and covered by additive hermetic tests in `tests/test_opencode_bridge.py`:

| Fix | Code | Tests |
|---|---|---|
| Fix 1 — resumed-session replay prevention (REQ-1) | `resumed` flag before fresh-session POST; `_seed_resumed_session_state` (best-effort `GET /session/{id}/message`, timeout 10.0, seeds `text_lens`/`tool_state`/`user_mids`/`seen_question_pids`, never raises); seen-set threaded through `_poll_session_deltas` → `_yield_part_deltas` at both call sites; question branch short-circuits pre-fetch | `_SeedPollClient`; `TestSeedResumedSessionState` (2); `TestResumedSessionPollingReplay` (4: never-replays, new-question-still-stops, seed-failure-degrades, fresh-does-not-seed) |
| Fix 2 — exact serve-PID port matching (REQ-2) | `_find_serve_pid`: tokenized NUL-normalized cmdline; exact `--port={port}` token OR adjacent `("--port", port)` pair; no substring/prefix; malformed-entry resilience kept | `_FakeProcFile` + `_patch_proc_cmdline`; `TestFindServePidExactMatch` (3: equals-form matches, longer advertised rejected, shorter search rejected); pre-existing NUL space-form test stays green |
| Fix 3 — bounded drain after recycle kills (REQ-3) | `_DRAIN_PROBES = 4`, `_DRAIN_PROBE_S = 0.25`; `_drain_serve_shutdown()` (≤4 `is_opencode_serve_running()` probes, early break, never raises); called after kill inside `if pid:` in `_recycle_serve_if_low_memory` and `_force_recycle_serve`; `hermetic_serve` guard intact | `TestServeDrain` (`@pytest.mark.real_recycle`, fake pid 424242, 0.01 s cadence): force-recycle early-break (3 probes), force-recycle budget-exhaust (4 probes), low-memory early-break, low-memory budget-exhaust |

## Landing Details

- The bridge implementation landed in commit `08471ea` (external harness sweep of the shared worktree at 20:12, alongside constants/scripts changes). The cycle-8 code is byte-identical to the designed approach (verified against `design.md`).
- The 461-line regression-test addition remained uncommitted; this apply phase:
  - normalized four stale `bridge-cycle-9` docstring labels to `bridge-cycle-8` in the new test classes (they reference the cycle-8 spec REQ-1/2/3);
  - re-ran the bridge suite.

## Test Evidence (apply-time)

```
$ .venv/bin/python -m pytest tests/test_opencode_bridge.py -q
153 passed in 15.83s
```

Full-suite run and compliance matrix: see `verify-report.md`.

## Scope Guard

Only `tests/test_opencode_bridge.py` (uncommitted) and the cycle-8 OpenSpec artifacts remain to be delivered; `opencode_bridge.py` implementation is committed at HEAD. No routes.py/proxy.py/config/systemd changes. Blocking `opencode_chat` path untouched. Other untracked cycle folders (cycle-4/6/7/9, capability specs) are NOT part of this change and were left untouched.
