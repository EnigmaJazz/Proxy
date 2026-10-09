# Exploration — bridge-cycle-4

Date: 2026-08-09 · Branch: `sdd/opencode-bridge-sdd-reliability/pr-5` · Store: both

## Executive summary

The bridge-reliability program hardened `opencode_chat_stream` and its serve
lifecycle across cycles 1–3, but the three items scoped in the
`bridge-cycle-3` proposal (recycle-before-ensure ordering, stale-pin drop on
resume, verify-before-kill pid-reuse guard) were **never applied** to the
code — the proposal exists in `openspec/changes/bridge-cycle-3/proposal.md`
but none of its three gaps is fixed in the working tree. The blocking
`opencode_chat` / `opencode_escalation` path (queue-worker cloud escalation)
remains completely unhardened, exactly as cycle-3 deferred it ("later
cycles"). Cycle 4 should deliver the missing serve-lifecycle hardening plus
the deferred escalation-path hardening, in one single PR.

## Current-state map (function identifiers, no line numbers)

- `opencode_chat_stream` — pinned-session streaming path (routes.py
  `model: "opencode"`, `/opencode`, SDD-autonomous mode). Entry sequence:
  autonomous-only `_force_recycle_serve()` → `ensure_opencode_serve()` →
  `_recycle_serve_if_low_memory()` → `_abort_zombie_sessions()` →
  resume-or-create session → `prompt_async` + `/event` SSE + polling
  fallback, completion on step-finish + session idle.
- `opencode_chat` — blocking path, fresh session per call, blocking
  `/session/{id}/message` POST, used by `opencode_escalation` (queue worker
  `CLOUD_ESCALATION_BACKEND="opencode"`).
- `_recycle_serve_if_low_memory` / `_force_recycle_serve` — the two kill
  primitives; both `_find_serve_pid()` then `os.kill(pid, 15)`.
- `_serve_health`, `_find_serve_pid`, `_memory_pressure` — /proc-based
  probes; `_serve_health` computes serve elapsed age for the 1800s
  recycle-after interval.
- `_abort_zombie_sessions` — busy-session hygiene with protected ids.
- Wedge detector `_detect_wedged_tool` — tool-part staleness; never kills the
  serve (it hosts other sessions).
- Test harness: `tests/test_opencode_bridge.py` (77 tests) with hermetic
  `_FakeClient`/`_FakeResp`, `@pytest.mark.real_recycle` guard, `os.kill`
  recorder — the exact pattern cycle-3 designed its regression tests on.

## Evidence: the three cycle-3 gaps are still open

| Gap | Cycle-3 claim | Current code |
|-----|---------------|--------------|
| A — recycle ordering | recycle BEFORE ensure ⇒ in-line respawn | `opencode_chat_stream` still calls `ensure_opencode_serve()` first, `_recycle_serve_if_low_memory()` after — only the `autonomous` flag path recycles before ensure. A low-memory/age recycle SIGTERMs the serve the same request just confirmed alive; concurrent sessions die. |
| B — stale-pin drop | drop pin when a SUCCESSFUL status fetch lacks the id | Resume block drops the pin only for `st == "busy"`; an absent id yields `st=None` → pin kept → `prompt_async` posts to a nonexistent session, failing every retry. |
| C — verify-before-kill | re-verify `/proc/<pid>/cmdline` before `os.kill` | Neither kill primitive re-verifies the pid; pid-reuse TOCTOU race remains. |

## Candidate scopes (ranked)

1. **A/B/C serve-lifecycle hardening** (highest value, scoped+designed by
   cycle-3, test pattern exists): ~40 code lines + ~130 test lines ≈ 170.
2. **Blocking/escalation-path hardening** (explicitly deferred by cycle-3):
   `opencode_chat` has no recycle-before-ensure, no in-line respawn, no retry —
   a serve death mid-call returns `[OpenCode Bridge Network Error: ...]` and
   the queue-worker escalation fails. Add bounded in-line respawn: on network
   failure after ensure, recycle once, re-ensure, single retry. ≈ 100 lines
   with tests.
3. **Docs reconciliation** — `docs/opencode-bridge.md` exists only in
   non-ancestor commit `61cc95c` (bridge-docs archived on a different
   lineage); the current tree has no bridge reference doc. Real spec lives at
   `openspec/changes/bridge-docs/specs/`. OUT OF SCOPE for this cycle (code
   focus; doc drift needs its own docs cycle against current code).
4. Sweep/completion edge cases — no new defects found beyond the above.

## Recommended cycle-4 scope

- In scope: A (recycle-before-ensure in `opencode_chat_stream`), B (stale-pin
  drop on successful status fetch lacking the id), C (verify-before-kill in
  both kill primitives), plus the deferred escalation-path hardening
  (bounded in-line respawn in `opencode_chat`).
- Out of scope: docs reconciliation, `opencode-serve-config.opencode.jsonc`,
  `scripts/` driver changes, wedge-detector behavior, permission relay.
- Budget check: A/B/C + blocking respawn ≈ 300–380 changed lines incl. tests
  — fits the 400-line review budget with margin; tasks phase will forecast
  exactly and slice if needed.

## Risks

- Recycling before ensure changes kill timing: the same request must then
  spawn in-line — deterministic per cycle-3 design, mirrors the autonomous
  path already in production.
- Pin-drop must stay conservative on transport-error fetches.
- Blocking-path retry must be bounded (single retry, no loop).

## Next

`/sdd-propose` — open `proposal.md` for bridge-cycle-4 with the scope above.
