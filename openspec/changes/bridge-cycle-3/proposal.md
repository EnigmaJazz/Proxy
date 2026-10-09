# Proposal: Bridge Cycle 3 — Safe, Self-Healing Serve Recycling at Stream Start

**One-line summary**: Fix the serve-recycle machinery in `opencode_bridge.py` (deferred by cycles 1–2): recycle-before-ensure ordering, stale-pin drop on resume, verify-before-kill in both kill primitives. ~170 lines, single PR; builds on the uncommitted cycle-2 liveness probe.

## Intent (Why)

Three defects make stream-start recycling kill the request that triggered it and leave dead conversations behind:

- **Gap A (deterministic failure)**: `opencode_chat_stream` order is `ensure_opencode_serve()` (:630) → `_recycle_serve_if_low_memory()` (:641). The memory/age recycle SIGTERMs the serve the liveness probe JUST confirmed alive; the same request then hits GET /session/status (:655), POST /session (:683), POST prompt_async (:720) against the dying serve → Network Error, NO in-line respawn. Fires ~once per 30 min uptime (`_SERVE_RECYCLE_AFTER_S` 1800s) plus every low-memory stream start; also destroys concurrent sessions.
- **Gap B (no self-heal)**: after any recycle, the stale pinned session id is absent from the fresh serve's /session/status map → `st = None` → busy-abort skipped → pin KEPT → prompt_async posts to a nonexistent session → error; every retry repeats the failure.
- **Gap C (pid-reuse race)**: both kill primitives scan /proc for the pid, then `os.kill(pid, 15)` (:1441–1444, :1464–1467) without re-verifying the pid still belongs to the serve.

**Outcome**: a recycle at stream start respawns in-line in the same request; stale pins self-heal; kills never hit a reused pid.

## Scope

### In Scope

- Move `_recycle_serve_if_low_memory()` BEFORE `ensure_opencode_serve()` (:630) so the same request's ensure respawns in-line (mirrors autonomous `_force_recycle_serve` ordering at :629).
- Stale-pin drop in the resume block: on a SUCCESSFUL status-map fetch lacking the pinned id, drop the pin so a fresh POST /session fires; on transport-error fetch, keep the pin (conservative).
- New helper re-verifying `/proc/<pid>/cmdline` matches the serve binary before `os.kill`; skip kill on mismatch — used by both kill primitives.
- 5 new regression tests (~130 lines) on the hermetic harness (`_FakeClient`/`_FakeResp`, `@pytest.mark.real_recycle` + fake pid 12345 + `os.kill` recorder; `_serve_health`/`_find_serve_pid` patched — no live serve, no real /proc).

### Out of Scope

- `opencode_chat` / escalation-path hardening (later cycles).
- Age-recycle collateral-damage policy — residual risk, documented in design.
- Wedge detector / permission relay (already hardened); `scripts/` driver changes.
- `opencode-serve-config.opencode.jsonc` — never touch, never commit.
- Liveness probe (cycle 2, done; in working tree, not re-scoped).

## Exploration Summary

Gap A is deterministic: recycle after ensure ⇒ the probe-verified serve is killed mid-request; first httpx error yields `[OpenCode Bridge Network Error: ...]` and returns with no respawn. Gap B: absent pinned id → `st = None` → busy-abort skipped → pin kept → prompt_async to a nonexistent session, failing forever. Gap C: /proc scan then kill is TOCTOU. Tests prove call order and kill behavior without touching a real serve.

## Assumptions & Edge Cases

- Recycle fires before ensure ⇒ the same request's ensure spawns the fresh serve in-line (deterministic, mirrors autonomous order).
- Pin dropped ONLY on a successful status fetch that lacks the id; fetch failures keep the pin conservatively.
- cmdline mismatch ⇒ kill skipped; serve stays absent ⇒ next ensure spawns (self-heals).

## Capabilities

`openspec/specs/` researched — no spec covers the bridge serve lifecycle (cycles 1–2 noted this). Adjacent cycle-2 `opencode-serve-liveness` proposal is unmodified by this change.

### New Capabilities

- `opencode-serve-lifecycle`: stream-start recycle ordering (recycle before ensure ⇒ in-line respawn); stale-pinned-session self-heal (drop pin when a successful status fetch lacks it; keep on fetch failure); verify-before-kill pid-reuse guard in both kill primitives.

### Modified Capabilities

None.

## Approach

Three surgical edits in `opencode_bridge.py` (~40 lines): reorder the recycle call ahead of ensure; gate the stale-pin drop on status-fetch success in the resume block; add the verify-before-kill helper used by both kill primitives. 5 regression tests in `tests/test_opencode_bridge.py` (~130 lines). No routes.py/proxy.py/scripts/constants/config changes.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `opencode_bridge.py` | Modified | Reorder recycle (:641) before ensure (:630); stale-pin drop in resume block (:655–681); verify-before-kill helper at :1441–1444 and :1464–1467. |
| `tests/test_opencode_bridge.py` | Modified | 5 new tests: in-line respawn; stale-pin drop; pin kept on fetch failure; pid-match kill / pid-reuse skip; autonomous double-fire → one stream. |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Recycle still kills concurrent sessions of other requests | Med | Fires only at stream start; residual collateral-damage policy documented in design, deferred |
| Pin kept for a genuinely-gone session | Low | Only on transport-error fetch; conservative by design |
| Verify-before-kill skips a legit kill on cmdline race | Low | Skipped serve self-heals via next ensure spawn |

## Rollback Plan

`git revert` the single commit — function-level edits confined to `opencode_bridge.py` + tests; prior ordering and pin semantics restored exactly; no config/DB/dependency/schema impact.

## Dependencies

None. Builds on the uncommitted cycle-2 liveness probe already in the working tree (not re-scoped).

## Success Criteria

- [ ] Recycle fires BEFORE ensure; the same request's ensure respawns in-line — stream reaches POST /session with no Network Error.
- [ ] Successful /session/status fetch lacking the pinned id → pin dropped, fresh POST /session, pin re-set; no prompt_async to the stale id.
- [ ] Status-fetch transport error → pin retained.
- [ ] `os.kill` fires only when `/proc/<pid>/cmdline` still matches the serve binary.
- [ ] `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` green (130 existing + 5 new); no live serve or real /proc touched.
- [ ] Diff touches only `opencode_bridge.py` + tests; `opencode-serve-config.opencode.jsonc` untouched.

## Delivery Shape

| Forecast item | Value |
|---------------|-------|
| Estimated changed lines | ~170 |
| 400-line review budget risk | Low |
| Chained PRs recommended | No |
| Delivery strategy | single-pr |
| Decision needed before apply | No |

Single PR. Suggested commit: `fix(bridge): recycle the serve before the liveness gate and drop stale pins`.
