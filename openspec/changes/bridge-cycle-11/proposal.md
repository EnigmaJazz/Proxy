# Proposal: Bridge Cycle 11 — Blocking-Path Reliability Trio (Connect-Only Respawn, Wedge Detection, Error-Path Cleanup)

**One-line summary**: Three blocking-path fixes in `_opencode_chat_attempt`: ReadTimeout never recycles the serve, wedge detection on the poll cadence (~300s), abort-on-ValueError for `resp.json()` — ~200 lines, single PR, under the 400-line budget.

## Intent (Why)

The queue-worker/blocking path lacks the streaming path's reliability discipline. Three hermetic-reproduced defects:

- **P1 (correctness)** — inner poll-loop except (`:877-879`) classifies EVERY `httpx.HTTPError` incl. ReadTimeout as `network_failed=True`; a wedged tool burning the 600s POST timeout SIGTERMs the serve (killing concurrent sessions) and retries once. Contradicts review-fix `37640d5`'s "NEVER recycle" outer gate (`:898-905`).
- **P2 (reliability)** — no wedge detection (cycle-9 open item): a wedged tool hangs the full 600s; the streaming path aborts at ~300s.
- **P3 (hygiene)** — `resp.json()` (`:886`) ValueError returns via the outer except WITHOUT `_abort_session_best_effort`, violating the docstring promise (`:762-764`); confirmed `aborts: 0` on a malformed 200 body.

**Outcome**: no serve SIGTERM on ReadTimeout; connect-class failures still recycle; wedges abort early; every non-success exit aborts.

## Scope

### In Scope

- Inner except (`:877-879`): split — connect-class → `(msg, True)` + abort; other HTTPError/ValueError → `(msg, False)` + abort.
- Poll block (`:855-876`): `_detect_wedged_tool` after permission check; wedge → abort + `(error, False)`, never recycle (mirrors streaming rule).
- `:886`: `resp.json()` ValueError → abort → error string.
- Tests: 4–5 hermetic in `TestOpenCodeChatHardening` (~170 lines).

### Out of Scope

- routes.py, proxy.py, streaming path, scripts/, constants, config; exploration approaches C/D/E; live-serve tests.

## Exploration Summary

Verified against current tree (HEAD `3b00002`, clean): fix-site anchors confirmed (`:877-879`, `:855-876`, `:886`; outer gate `:898-905`; `_detect_wedged_tool` `:1648`; `_abort_session_best_effort` `:408`). Baseline green: 502 passed (bridge 181; drivers 33). Defects hermetically reproduced.

## Assumptions & Edge Cases

- Wedge check AFTER the permission check (parked write looks identical to a wedge — streaming-path ordering rule).
- False-wedge risk accepted as on the streaming path (300s+ no-output run; stale-part guard intact).
- Residual hang (serve accepts but never responds) matches streaming-path behavior; tests keep the autouse `hermetic_serve` guard kill-free.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `opencode-serve-lifecycle`: **REQ-4 amended** — recycle+retry ONLY on connect-class failures; **REQ-6 new** — blocking path wedge-checks at the poll cadence, aborts (never recycles) tools running > `_TOOL_WEDGE_AFTER_S`; **REQ-7 new** — every non-success exit aborts its session. Delta: `openspec/changes/bridge-cycle-11/specs/spec.md`.

## Approach

Full SDD pipeline as ONE change on `fix/sdd-cycle-autonomous-delivery`, single PR (`delivery_strategy = single-pr`), conventional commits. ~200 changed lines (~30 bridge + ~170 tests) — under the 400-line budget; NO `size:exception` required (unlike cycle-10's ~642).

**Rollback**: revert the landing commits (two files).

**Success criteria**: new hermetic tests green; bridge suite (181 + new) green; full suite (502 + new) green; commits landed; delta spec recorded.
