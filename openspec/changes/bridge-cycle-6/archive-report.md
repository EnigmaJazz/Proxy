# Archive Report — bridge-cycle-6

- **Change**: `bridge-cycle-6` — Streaming-path error-exit hygiene + serve-recycle ordering (`opencode_chat_stream`)
- **Archived**: 2026-08-09
- **Status**: **ARCHIVED** (in-place per repo convention — cycle-5 pattern, commit `a2141da`; no directory moves, no deletions, `openspec/changes/archive/` untouched)
- **Type**: Runtime reliability hardening (streaming bridge path + hermetic tests)
- **Persistence mode**: openspec (file-based) — this report is the archive record; Engram handled by the orchestrator per repo practice
- **Verified commits**: cycle-6 code+tests in `fbbd17e`; later bridge fixes in `3030329`, `08471ea`; branch `sdd/opencode-bridge-sdd-reliability/pr-5`. Current HEAD is `99c660c` (a scripts-only commit that landed after the launch-prompt state; it does not touch cycle-6 code).
- **Verdict**: PASS — all 7 requirements (REQ-1..REQ-7) of `openspec/specs/opencode-bridge-stream-exit-hygiene/` satisfied, per `verify-report.md` and the orchestrator's final-state facts (2026-08-09)

## Executive Summary

Cycle 5 hardened the blocking path (`opencode_chat`); cycle 6 closed the streaming
path's (`opencode_chat_stream`) three error exits that returned without cleanup —
prompt HTTP != 204, a `message.updated` event carrying `info.error`, and the outer
`except (httpx.HTTPError, OSError, ValueError)` — and fixed the
serve-recycle-before-respawn ordering bug that failed one request roughly every
30 minutes of serve uptime. All cleanup now flows through one shared best-effort
helper `_abort_stream_session_best_effort` (abort + pin drop + pending-permission
pop; never raises), the four existing inline cleanup sites were DRY'd onto it, and
the non-autonomous path now runs `_recycle_serve_if_low_memory()` before
`ensure_opencode_serve()` so a recycled serve is always respawned before any
request is attempted. Success-path behavior is byte-for-byte unchanged.

Final state at close: `TestStreamExitHygiene` 4 passed; `tests/test_opencode_bridge.py`
153 passed; full suite 444 passed (all hermetic, `hermetic_serve` autouse guard).
The working tree has no uncommitted cycle-6 changes.

## Artifacts Inventory

| Artifact | Location | Status |
|---|---|---|
| Proposal | `openspec/changes/bridge-cycle-6/proposal.md` | Present (uncommitted, to be committed at archive commit) |
| Spec (delta) | `openspec/changes/bridge-cycle-6/specs/opencode-bridge-stream-exit-hygiene/spec.md` | Present; byte-identical to main capability spec |
| Spec (capability, main) | `openspec/specs/opencode-bridge-stream-exit-hygiene/spec.md` | Present, untracked; REQ-1..REQ-7 verification target |
| Design | `openspec/changes/bridge-cycle-6/design.md` | Present |
| Tasks | `openspec/changes/bridge-cycle-6/tasks.md` | Present; 21/21 tasks complete |
| Apply progress | `openspec/changes/bridge-cycle-6/apply-progress.md` | Present; snapshot status COMPLETE (intermediate snapshot) |
| Verify report | `openspec/changes/bridge-cycle-6/verify-report.md` | Present; status PASS |
| Archive report | `openspec/changes/bridge-cycle-6/archive-report.md` | This file (additive; the only write of this phase) |
| Code | `opencode_bridge.py` + `tests/test_opencode_bridge.py` | Committed (`fbbd17e`, plus later fixes `3030329`, `08471ea`) |

## Per-Requirement Final State

| Req | Requirement | Final state | Evidence |
|-----|-------------|-------------|----------|
| REQ-1 | Shared best-effort stream cleanup helper | PASS | `_abort_stream_session_best_effort` present (opencode_bridge.py:334-358): falsy `session_id` no-op, delegates abort to cycle-5 `_abort_session_best_effort` (catches `(httpx.HTTPError, OSError)`), pops `pending_permissions[session_id]` and `session_map[session_key]`, never raises; name distinct from the blocking-path helper |
| REQ-2 | Prompt non-204 exit cleans up | PASS | Trio runs, then `[OpenCode Bridge Error: prompt HTTP <code>]` yielded verbatim (:888-897) |
| REQ-3 | `message.updated` error exit cleans up | PASS | `info.get("error")` branch runs trio, then `[OpenCode Bridge Error: <info.error>]` verbatim (:1203-1212) |
| REQ-4 | Outer network error exit cleans up | PASS | `except (httpx.HTTPError, OSError, ValueError)` with `if session_id:` guard runs trio, then `[OpenCode Bridge Network Error: ...]` verbatim (:1231-1240) |
| REQ-5 | Success + existing cleanup paths unchanged | PASS | Pinned-busy/timeout/polling-timeout/wedge sites DRY'd onto helper with verbatim yields; success path untouched; full suite green |
| REQ-6 | Recycle before ensure | PASS | `_recycle_serve_if_low_memory()` at :785 runs before `ensure_opencode_serve()` at :786 on the non-autonomous path; autonomous force-recycle ordering unchanged |
| REQ-7 | Regression coverage | PASS | `TestStreamExitHygiene` (4 tests) green; `_FakeClient` gains `prompt_status` / `fail_get_after` / `_get_count` scripting; abort observability via recorded `post_calls` |

## Test Evidence

Final numbers per the orchestrator's run (2026-08-09), consistent with `verify-report.md`:

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q -k "TestStreamExitHygiene"` | 4 passed |
| `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` | 153 passed |
| `.venv/bin/python -m pytest tests/ -q` | 444 passed |

All hermetic: `hermetic_serve` autouse fixture guards against live-serve access; no
real /proc, network, or serve process involved.

Final-State Authority note: `apply-progress.md` records 140/431 passed at apply time —
an intermediate snapshot. The close-state counts above (153/444) come from
`verify-report.md` and the orchestrator's final run, which included later-cycle
additions to the suite; the snapshot's completion claims remain true, its counts were
superseded.

## Accepted Deviations

- **Mid-stream failure scripting (accepted, non-blocking)**: design task 1.5 / REQ-7
  scripted the mid-stream failure with `fail_get_after = 1`; the committed test uses
  `raise_on_stream = True` instead. Both deterministically trigger the SAME outer-except
  handler (REQ-4 coverage is equivalent — `raise_on_stream` raises on the `/event` SSE
  read inside the loop). The `fail_get_after`/`_get_count` scripting requested by REQ-7
  exists in `_FakeClient` but is not exercised by a dedicated test. No functional gap;
  recorded in `verify-report.md` and carried to close. Per `verify-report` observation
  at verification time; verified still accurate at close.

## Delivery Notes

- Single PR (no chaining): ~165-200 changed lines (code + tests) — within the 400-line
  review budget; no `size:exception` needed.
- `fbbd17e` is a MIXED commit: it carries cycle-6 code+tests in `opencode_bridge.py` and
  `tests/test_opencode_bridge.py`, but also non-cycle-6 content (serve-config auth-route
  change, bridge-cycle-4 exploration artifacts). Cycle-6's own scope (`opencode_bridge.py`
  + tests only) was confirmed; `routes.py`, `proxy.py`, config, systemd,
  `_recycle_serve_if_low_memory` internals, and error-string formats were untouched.
- `opencode-serve-config.opencode.jsonc` is not part of cycle-6's change surface.

## Scope Note (uncommitted work in tree)

The working tree contains uncommitted work for OTHER cycles: `openspec/changes/bridge-cycle-7/`,
`bridge-cycle-8/`, `bridge-cycle-9/`, `openspec/specs/opencode-bridge-polling-replay-prevention/`,
`openspec/specs/opencode-bridge-serve-lifecycle/` (untracked), `openspec/changes/bridge-cycle-4/design.md`
and related cycle-4 artifacts, and uncommitted cycle-8 code+tests in `opencode_bridge.py` /
`tests/test_opencode_bridge.py`. None of that belongs to bridge-cycle-6; it was NOT archived,
NOT evaluated, and NOT modified here. It passes (included in the 153/444 counts) but remains
uncommitted.

## Rollback Boundary

Cycle-6's function-level edits are confined to `opencode_bridge.py` and
`tests/test_opencode_bridge.py` within mixed commit `fbbd17e`. A clean cycle-6 rollback
is a targeted revert of the cycle-6 hunks in those two files (helper + three leak-exit
trio calls + recycle/ensure reorder + `TestStreamExitHygiene` + `_FakeClient` scripting);
reverting `fbbd17e` wholesale would also undo that commit's non-cycle-6 content
(serve-config auth-route change, cycle-4 artifacts) and must be avoided. No config, DB,
dependency, or schema impact; prior streaming semantics (leaking exits, ensure-then-recycle)
restored by reverting only the cycle-6 hunks.

## Remaining Steps

1. Once the working tree is clean of other cycles' uncommitted work, commit this change's
   artifacts — including this archive report and the untracked
   `openspec/specs/opencode-bridge-stream-exit-hygiene/` capability spec — as
   `chore(openspec): archive bridge-cycle-6` (matching the cycle-5 archive commit pattern).
   NOT performed here: the tree carries another cycle's uncommitted work, and committing now
   would sweep it in.
2. No push / no PR created in this phase (per commit policy).

## Close Verdict

**CLOSED** — cycle delivered its single-PR scope: every streaming error exit cleans up
its session, a recycled serve is respawned before any request is attempted, all
requirements verified (REQ-1..REQ-7 PASS), full suite green (444 passed, hermetic),
artifacts persisted. SDD cycle complete; ready for the next change.
