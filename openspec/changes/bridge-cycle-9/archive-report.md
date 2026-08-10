# Archive Report — bridge-cycle-9

- **Change**: `bridge-cycle-9` — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills
- **Archived**: 2026-08-09
- **Type**: Runtime code + tests (opencode bridge hardening)
- **Persistence mode**: both — filesystem authoritative (OpenSpec change folder); Engram mirror unavailable in this runtime (recorded in proposal.md; no observation IDs to reference)
- **Verified commit**: HEAD `30be86b` (`30be86be5e56dc506de4b672107820c74e57485e`) on branch `sdd/opencode-bridge-sdd-reliability/pr-5`; working tree clean
- **Verdict**: PASS — all 3 requirements / 7 scenarios, per `verify-report.md`

## Change Summary

The OpenCode bridge (`opencode_bridge.py`, repo root) had three pending defects that this cycle fixed:

1. **Resumed-session polling replay (REQ-1)** — per-call part state (`user_mids`/`text_lens`/`tool_state`) is fed only by live `/event` bus messages. When a pinned session's bus closes, the polling fallback re-scans the FULL message list and re-yields history — old text, reasoning, tool chunks, and a stale resolved `question` part that kills the stream before the real answer streams. Fix: one best-effort `GET /session/{id}/message` seed after pin resolution and before `prompt_async` (`_seed_resumed_session_state`), plus a new `seen_question_pids` set threaded through `_poll_session_deltas` → `_yield_part_deltas` (both call sites) that suppresses seeded question pids before the fetch-retry loop. Fresh sessions never seed; seed failures never raise.
2. **Exact serve-PID port matching (REQ-2)** — `_find_serve_pid` matched the NUL-normalized cmdline by substring (`f"--port {port}" in cmd`): `--port=18999` never matched, `--port 189990` false-positived for search "18999". Fix: tokenize the cmdline, require `opencode` + `serve` tokens, match exact `--port=<port>` token or exact adjacent `("--port", port)` pair — equality only, no prefix/substring matching.
3. **Bounded drain after recycle kills (REQ-3)** — both recycle helpers (`_recycle_serve_if_low_memory`, `_force_recycle_serve`) killed the serve and returned immediately; `ensure_opencode_serve`'s liveness probe still answered True mid-SIGTERM, so no respawn happened and the stream POSTed into a dying listener. Fix: `_drain_serve_shutdown()` polls `is_opencode_serve_running()` up to 4 × 0.25s after a verified kill, breaks early when down, never raises (mirrors the existing config-drift drain).

Adopted scope: REQ-1/REQ-2 requirements R1–R12 come from the pending `openspec/specs/opencode-bridge-polling-replay-prevention/` spec (cycle-8 folder unapplied); REQ-3 is new to `openspec/specs/opencode-serve-lifecycle/`. The cycle-9 delta spec in this folder records the adoption and threading detail.

## Artifact Inventory

| Artifact | Path | Status |
|---|---|---|
| Proposal | `openspec/changes/bridge-cycle-9/proposal.md` | ✅ exists |
| Spec | `openspec/changes/bridge-cycle-9/specs/spec.md` | ✅ exists |
| Design | `openspec/changes/bridge-cycle-9/design.md` | ✅ exists |
| Tasks | `openspec/changes/bridge-cycle-9/tasks.md` | ✅ exists — 17/17 `[x]` |
| Apply progress | `openspec/changes/bridge-cycle-9/apply-progress.md` | ✅ exists — COMPLETE |
| Verify report | `openspec/changes/bridge-cycle-9/verify-report.md` | ✅ written this cycle — PASS |
| Archive report | `openspec/changes/bridge-cycle-9/archive-report.md` | ✅ this file |
| Deliverable | `opencode_bridge.py` + `tests/test_opencode_bridge.py` | ✅ committed at HEAD `30be86b` |

## Requirement Satisfaction (from verify-report)

| Requirement | Scenarios | Verdict |
|---|---|---|
| REQ-1: Resumed-session polling replay prevention | 3/3 | **PASS** |
| REQ-2: Exact serve-PID port matching | 2/2 | **PASS** |
| REQ-3: Bounded drain after recycle kills | 2/2 | **PASS** |

Total: 3/3 requirements, 7/7 scenarios PASS, per `verify-report.md` (fresh evidence: bridge suite 153 passed, full suite 449 passed, targeted 14 passed, `py_compile` clean). No CRITICAL or WARNING issues. One SUGGESTION recorded for traceability (apply-progress stale "uncommitted tests" note — tests were committed in `30be86b` before verification; working tree clean).

## Final-State Facts (at close)

- **Deliverables**: Fix 1 (`_seed_resumed_session_state` + `seen_question_pids` threading, `opencode_bridge.py:869-876, 976, 1222, 1269, 1310, 1567-1644`), Fix 2 (tokenizer in `_find_serve_pid` `:1847-1880`), Fix 3 (`_DRAIN_PROBES = 4` `:1657`, `_drain_serve_shutdown` `:1661-1672`, call sites `:1702`, `:1724`); tests `_SeedPollClient` + 13 new tests (`TestSeedResumedSessionState`, `TestResumedSessionPollingReplay`, `TestFindServePidExactMatch`, `TestServeDrain`).
- **Commit trail**: fix source landed in `08471ea` (bridge fixes rode along with the professional-context change, per apply-progress note); test additions (+479 lines in `tests/test_opencode_bridge.py`) landed in `30be86b` alongside unrelated wedge-threshold/date-injection work (coupled only by commit timing, not by logic). No cycle-9 commit carries a `Co-Authored-By:` trailer; conventional messages only.
- **Review workload**: forecast ~281 changed lines, 400-line budget risk Low, single PR, no `size:exception` needed — matches the session preflight `single-pr` choice. No chained PRs.
- **Verification**: 153 bridge / 449 total tests green; targeted cycle-9 classes 14/14 execute (incl. `real_recycle`-marked drain tests); `py_compile` clean; `hermetic_serve` autouse guard intact.
- **Spec deltas**: REQ-1/REQ-2 adopt pending spec `opencode-bridge-polling-replay-prevention` (no new requirements — delta spec records adoption + `seen_question_pids` threading); REQ-3 adds the bounded-drain requirement to `opencode-serve-lifecycle`. Capability specs live under `openspec/specs/`; no new capability spec created.

## Open Items

- None blocking. Out-of-scope items remain open for future cycles (unchanged from proposal): wedge/timeout policy tuning, polling quiet-done cutoff (needs live validation), blocking-path wedge detection, driver artifact-wait cap, `scripts/`.

## Rollback Note

Revert the bridge portion at HEAD: `git revert 30be86b` also reverts unrelated wedge-threshold/date-injection changes, so prefer surgical revert of the specific hunks in `opencode_bridge.py` + `tests/test_opencode_bridge.py` (or a targeted `git revert 08471ea` for the fix source) if the maintainer wants the cycle-9 behavior out while keeping the rest. No config/systemd changes involved; no migration required.

## Final State

SDD cycle complete: planned, implemented, verified (PASS — 153 bridge / 449 total tests), and archived. Resumed conversations no longer replay history or die on stale questions after a bus drop; serve discovery matches exact port tokens; recycle kills drain the listener so the serve reliably respawns.
