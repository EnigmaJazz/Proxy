# Proposal: Bridge Cycle 4 — Serve-Lifecycle Hardening + Blocking-Path Respawn

**One-line summary**: Deliver the cycle-3 serve-lifecycle fixes (recycle-before-ensure, stale-pin drop, verify-before-kill) that were NEVER applied to the code, plus the deferred blocking-path respawn in `opencode_chat`. ~350 lines, single PR.

## Intent (Why)

`bridge-cycle-3/proposal.md` scoped three serve-lifecycle gaps, but none reached the code — all three defects are still in the working tree:

- **Gap A (deterministic failure)**: `opencode_chat_stream` calls `ensure_opencode_serve()` (:637) before `_recycle_serve_if_low_memory()` (:648); the memory/age recycle SIGTERMs the serve the same request just confirmed alive, killing concurrent sessions with NO in-line respawn. Only the `autonomous` path (:636) recycles first.
- **Gap B (no self-heal)**: the resume block drops the pin only when `st == "busy"`; a recycled serve lacks the pinned id → `st = None` → pin KEPT → `prompt_async` posts to a nonexistent session, failing every retry.
- **Gap C (pid-reuse race)**: both kill primitives `_find_serve_pid()` then `os.kill(pid, 15)` (:1452–1457, :1475–1478) without re-verifying the pid's cmdline — TOCTOU.
- **Blocking path (deferred by cycle-3)**: `opencode_chat` (:537, used by `opencode_escalation` for queue-worker cloud escalation) has no recycle-before-ensure, no respawn, no retry — a serve death mid-call returns `[OpenCode Bridge Network Error: ...]` and the escalation fails.

**Outcome**: a recycle respawns in-line; stale pins self-heal; kills never hit reused pids; the blocking path survives one serve death per call.

## Scope

### In Scope

- Move `_recycle_serve_if_low_memory()` BEFORE `ensure_opencode_serve()` so the same request's ensure respawns in-line (mirrors the production autonomous `_force_recycle_serve` ordering).
- Stale-pin drop in the resume block: SUCCESSFUL `/session/status` fetch lacking the pinned id → drop pin → fresh POST /session; transport-error fetch → keep pin (conservative).
- New verify-before-kill helper re-checking `/proc/<pid>/cmdline` against the serve binary before `os.kill`; skip kill on mismatch — used by BOTH kill primitives.
- Blocking-path respawn in `opencode_chat`: on network error after a successful ensure, `_force_recycle_serve()` → re-ensure → retry the call exactly once; return the error string if the retry fails. Bounded, no loop.
- 6 regression tests (~150 lines) on the hermetic harness.

### Out of Scope

- Docs reconciliation (`docs/opencode-bridge.md` never landed on this lineage — separate docs cycle).
- `opencode-serve-config.opencode.jsonc` — never touch, never commit.
- `scripts/` driver changes; wedge detector; permission relay; zombie-sweep behavior.

## Exploration Summary

Exploration confirmed all three cycle-3 gaps still open (evidence table in explore.md) and the blocking path fully unhardened — a serve death mid-call returns the network-error string and the queue-worker escalation fails. Ranked scope: A/B/C + blocking respawn ≈ 300–380 changed lines incl. tests, fits the 400-line review budget.

## Assumptions & Edge Cases

- Recycle before ensure ⇒ the same request's ensure spawns the fresh serve in-line (deterministic; mirrors autonomous order already in production).
- Pin dropped ONLY on a successful status fetch lacking the id; fetch failures keep the pin conservatively.
- cmdline mismatch ⇒ kill skipped; serve stays absent ⇒ next ensure spawns (self-heals).
- Blocking retry fires ONLY on network-class errors (`httpx.HTTPError`/`OSError`/`ValueError`) after a successful ensure; HTTP status errors never retry; single retry, no loop.

## Capabilities

`openspec/specs/` researched — the existing `opencode-bridge-blocking-path` spec covers permission hygiene only; NO spec covers the bridge serve lifecycle.

### New Capabilities

- `opencode-serve-lifecycle`: stream-start recycle ordering (recycle before ensure ⇒ in-line respawn); stale-pinned-session self-heal (drop pin when a successful status fetch lacks it; keep on fetch failure); verify-before-kill pid-reuse guard in both kill primitives; extended with the blocking-path in-line respawn (single bounded retry after a network failure).

### Modified Capabilities

None.

## Approach

Four surgical edits in `opencode_bridge.py` (~50 lines): reorder the recycle call ahead of ensure; gate the pin-drop on status-fetch success in the resume block; add the verify-before-kill helper shared by both kill primitives; wrap `opencode_chat`'s ensure→POST sequence with a single recycle + retry. 6 regression tests in `tests/test_opencode_bridge.py` (~150 lines) on the existing hermetic fixture (`_FakeClient`/`_FakeResp`, `@pytest.mark.real_recycle`, `os.kill` recorder, `_serve_health`/`_find_serve_pid` patched — no live serve, no real /proc). No routes.py/proxy.py/scripts/constants/config changes.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `opencode_bridge.py` | Modified | Recycle ordering in `opencode_chat_stream` entry; pin-drop gate in resume block; verify-before-kill helper in `_recycle_serve_if_low_memory` + `_force_recycle_serve`; bounded respawn in `opencode_chat` |
| `tests/test_opencode_bridge.py` | Modified | 6 new tests (~150 lines): in-line respawn ordering; stale-pin drop; pin kept on fetch failure; pid-match kill / pid-mismatch skip; blocking single retry respawn; blocking no-retry-on-http-error |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Recycle-before-ensure still kills concurrent sessions of other requests | Med | Fires only at stream start; mirrors proven autonomous ordering; residual collateral-damage policy documented in design, deferred |
| Pin dropped for a genuinely-live session on odd status data | Low | Drop only on successful fetch lacking the id; conservative by design |
| Blocking retry doubles escalation latency or retries a healthy-serve failure | Low | Network-class errors only, single retry, error string returned on retry failure; bounded |

## Rollback Plan

`git revert` the single commit — function-level edits confined to `opencode_bridge.py` + tests; prior ordering, pin, kill, and retry semantics restored exactly; no config/DB/schema/dependency impact.

## Dependencies

Cycle-3 delivered items are NOT in the working tree — this cycle delivers them (no cycle-3 code to build on; its proposal is context only). Builds on the in-tree cycle-2 liveness probe and the hermetic bridge test harness. No external dependencies.

## Success Criteria

- [ ] Recycle fires BEFORE ensure; the same request's ensure respawns in-line — stream reaches POST /session with no Network Error.
- [ ] Successful `/session/status` fetch lacking the pinned id → pin dropped, fresh POST /session; transport-error fetch → pin retained.
- [ ] `os.kill` fires only when `/proc/<pid>/cmdline` still matches the serve binary.
- [ ] `opencode_chat` recycles + retries exactly once after a post-ensure network error, and returns the error string if the retry fails (no loop).
- [ ] `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` green (77 existing + 6 new); no live serve or real /proc touched.
- [ ] Diff touches only `opencode_bridge.py` + tests; `opencode-serve-config.opencode.jsonc` untouched.

## Delivery Shape

| Forecast item | Value |
|---------------|-------|
| Estimated changed lines | ~350 |
| 400-line review budget risk | Low |
| Chained PRs recommended | No |
| Delivery strategy | single-pr |
| Decision needed before apply | No |

Single PR. Suggested commit: `fix(bridge): recycle serve before ensure, drop stale pins, verify pids, respawn blocking calls`
