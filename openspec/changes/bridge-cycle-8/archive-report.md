# Archive Report — bridge-cycle-8

- **Change**: `bridge-cycle-8` — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills
- **Archived**: 2026-08-10
- **Type**: Runtime code + tests (opencode bridge hardening)
- **Persistence mode**: both — filesystem authoritative (OpenSpec change folder); Engram mirror unavailable in this runtime (recorded in proposal.md; no observation IDs to reference)
- **Verified commit**: HEAD `ea4192b` (`ea4192be6d988cb82f3dc6376d9b3df108581aaf`) on branch `sdd/opencode-bridge-sdd-reliability/pr-5`; implementation `08471ea` and test additions `30be86b` are both ancestors of HEAD
- **Verdict**: PASS — all 3 requirements / 7 scenarios per `verify-report.md`, re-confirmed by fresh archive-time evidence (164 bridge / 465 full-suite tests)
- **Archive scope note**: this phase wrote ONLY the missing `archive-report.md` per orchestrator instruction. The change folder remains in place (untracked, like the other cycle folders); delta requirements were already adopted into the main capability specs under `openspec/specs/` in the prior cycle. No code, tests, or other artifacts were modified.

## Change Summary

The OpenCode bridge (`opencode_bridge.py`, repo root) had three pending reliability defects that this cycle fixed:

1. **Resumed-session polling replay (REQ-1)** — per-call part state (`user_mids`/`text_lens`/`tool_state`) is fed only by live `/event` bus messages. When a pinned session's bus closes, the polling fallback re-scans the FULL message list and re-yields history — old text, reasoning, tool chunks, and a stale resolved `question` part that kills the stream before the real answer streams. Fix: one best-effort `GET /session/{id}/message` seed after pin resolution and before `prompt_async` (`_seed_resumed_session_state`, opencode_bridge.py:1634 in the working tree), plus a new `seen_question_pids` set threaded through `_poll_session_deltas` → `_yield_part_deltas` (both call sites) that suppresses seeded question pids before the fetch-retry loop. Fresh sessions never seed; seed failures never raise.
2. **Exact serve-PID port matching (REQ-2)** — `_find_serve_pid` matched the NUL-normalized cmdline by substring: `--port=18999` never matched and `--port 189990` false-positived for search "18999". Fix: tokenize the cmdline, require `opencode` + `serve` tokens, match exact `--port=<port>` token or exact adjacent `("--port", port)` pair — equality only, no prefix/substring matching (opencode_bridge.py:1970 in the working tree).
3. **Bounded drain after recycle kills (REQ-3)** — both recycle helpers (`_recycle_serve_if_low_memory`, `_force_recycle_serve`) killed the serve and returned immediately; `ensure_opencode_serve`'s liveness probe still answered True mid-SIGTERM, so no respawn happened and the stream POSTed into a dying listener. Fix: `_drain_serve_shutdown()` (opencode_bridge.py:1728) polls `is_opencode_serve_running()` up to 4 × 0.25s after a verified kill, breaks early when down, never raises (mirrors the existing config-drift drain).

Adopted scope: REQ-1/REQ-2 requirements R1–R12 come from the pending `openspec/specs/opencode-bridge-polling-replay-prevention/` spec (cycle-7 work unapplied); REQ-3 adds the bounded-drain requirement to `openspec/specs/opencode-serve-lifecycle/` (REQ-5 "Recycle kills drain the dying listener"). The cycle-8 delta spec in this folder records the adoption and the `seen_question_pids` threading detail.

## Artifact Inventory

| Artifact | Path | Status |
|---|---|---|
| Proposal | `openspec/changes/bridge-cycle-8/proposal.md` | ✅ exists |
| Spec | `openspec/changes/bridge-cycle-8/spec.md` (change root, repo convention — not under `specs/`) | ✅ exists |
| Design | `openspec/changes/bridge-cycle-8/design.md` | ✅ exists |
| Tasks | `openspec/changes/bridge-cycle-8/tasks.md` | ✅ exists — 23/23 `[x]` |
| Apply progress | `openspec/changes/bridge-cycle-8/apply-progress.md` | ✅ exists — COMPLETE (implementation committed; stale "uncommitted tests" note corrected below) |
| Verify report | `openspec/changes/bridge-cycle-8/verify-report.md` | ✅ exists — PASS |
| Archive report | `openspec/changes/bridge-cycle-8/archive-report.md` | ✅ this file (final missing artifact) |
| Deliverable | `opencode_bridge.py` + `tests/test_opencode_bridge.py` | ✅ committed at `08471ea` (fix source) + `30be86b` (tests), both ancestors of HEAD |

## Requirement Satisfaction (from verify-report + fresh evidence)

| Requirement | Scenarios | Verdict |
|---|---|---|
| REQ-1: Resumed-session polling replay prevention | 3/3 | **PASS** |
| REQ-2: Exact serve-PID port matching | 2/2 | **PASS** |
| REQ-3: Bounded drain after recycle kills | 2/2 | **PASS** |

Total: 3/3 requirements, 7/7 scenarios PASS, per `verify-report.md`. Fresh archive-time evidence (orchestrator re-ran at close, 2026-08-10): `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` → **164 passed** (verify-report's 153 + 11 out-of-scope in-flight pid-reuse tests, all pass); `.venv/bin/python -m pytest tests/ -q` → **465 passed** (verify-report said 449). `py_compile` clean per verify report. No CRITICAL or WARNING issues in the verification record.

## Final-State Facts (at close)

- **Deliverables**: Fix 1 (`_seed_resumed_session_state` + `seen_question_pids` threading — `opencode_bridge.py:1634` in the current working tree), Fix 2 (exact tokenizer in `_find_serve_pid` — `:1970`), Fix 3 (`_DRAIN_PROBES = 4` / `_DRAIN_PROBE_S = 0.25`, `_drain_serve_shutdown` — `:1728`); tests `_SeedPollClient` + four test classes (`TestSeedResumedSessionState`, `TestResumedSessionPollingReplay`, `TestFindServePidExactMatch`, `TestServeDrain`) — all present in `git show HEAD:tests/test_opencode_bridge.py`.
- **Commit trail** (correcting the stale apply-progress note): fix source landed in **`08471ea`** (`feat(bridge): double professional context to 131072; one-file local delegation` — bridge fixes rode along with the unrelated professional-context change); the cycle-8 regression tests landed in **`30be86b`** (`fix(bridge): wedge threshold 300s for apply bash; kinver professional context 131072`) — i.e., the 461-line test addition is COMMITTED at HEAD, contrary to apply-progress's "remained uncommitted" snapshot. Both commits are ancestors of HEAD `ea4192b`. No commit carries a `Co-Authored-By:` trailer; conventional messages only.
- **Review workload**: forecast ~268 changed lines (proposal) / ~281 (cycle-9 archive), 400-line budget risk Low, single PR, delivery strategy `single-pr` — matches the session preflight; no `size:exception` needed; no chained PRs.
- **Verification**: 164 bridge / 465 full-suite tests green at close (fresh run); verify-time snapshot was 153/449. `hermetic_serve` autouse guard intact; targeted drain tests execute via `@pytest.mark.real_recycle` with fake pid 424242 and 0.01 s cadence.
- **Spec deltas**: REQ-1/REQ-2 adopt `openspec/specs/opencode-bridge-polling-replay-prevention/` (R1–R12, scenarios S1–S8 — already present in the main spec; no new requirements, delta records adoption + `seen_question_pids` threading); REQ-3 adds the bounded-drain requirement to `openspec/specs/opencode-serve-lifecycle/` (REQ-5, present in the main spec). Capability specs live under `openspec/specs/`; no new capability spec created.

## Open Items

- **Out-of-scope in-flight work (NOT part of this change)**: a blocking-path respawn + pid-reuse guards refactor (`_opencode_chat_attempt`, `_pid_is_serve`, `_cmdline_matches_serve`, plus the `TestStalePinSelfHeal` test class) is currently applied in the working tree — `git diff` on `opencode_bridge.py` and `tests/test_opencode_bridge.py` shows ONLY this out-of-scope work uncommitted; cycle-8's own code is fully committed. It is preserved as in-flight state, including `stash@{0}` ("in-flight: blocking-path respawn + pid-reuse guards (OUT OF cycle-8 scope; preserved, not part of delivery)"). Belongs to a future cycle (likely cycle-10).
- None blocking within cycle-8 scope. Remaining out-of-scope items unchanged from proposal: wedge/timeout policy tuning, B2 polling quiet-done cutoff (needs live validation, own cycle), B3 blocking-path wedge detection, B4 driver artifact-wait cap, `scripts/`.

## Rollback Note

The cycle-8 behavior lives in two commits that also carry unrelated changes, so a plain full revert is not surgical:

- `git revert 08471ea` reverts the fix source but ALSO reverts the unrelated professional-context change (constants.py) and the `scripts/sdd_autonomous_cycle.py` edit riding in that commit.
- `git revert 30be86b` reverts the regression tests but ALSO reverts the unrelated wedge-threshold change (120s → 300s in `opencode_bridge.py`) and the `opencode-serve-config.opencode.jsonc` context edit.

Prefer a targeted hunk-level revert of `opencode_bridge.py` (the `_seed_resumed_session_state` / `seen_question_pids` threading, `_find_serve_pid` tokenizer, `_drain_serve_shutdown` + call sites) and `tests/test_opencode_bridge.py` (the four cycle-8 test classes + `_SeedPollClient`) to remove cycle-8 behavior while keeping the unrelated changes. No config/systemd changes were made; no migration required.

## Final State

SDD cycle complete: planned, implemented, verified (PASS — fresh evidence 164 bridge / 465 total tests), and archived. Resumed conversations no longer replay history or die on stale questions after a bus drop; serve discovery matches exact port tokens in either argv form; recycle kills bounded-drain the listener so `ensure_opencode_serve` reliably respawns.
