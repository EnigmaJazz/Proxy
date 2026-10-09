# Proposal: Bridge Cycle 2 — Harden the opencode serve Liveness Probe

**One-line summary**: Re-define "serve is up" as *any* received HTTP response (2xx/3xx/4xx/5xx); only transport failure = DOWN — fixing duplicate spawns and mid-cycle recycles. ~60–90 lines, single PR; executes unapplied `bridge-cycle-1/proposal.md`.

## Intent (Why)

The probe (`opencode_bridge.py:406–413`) returns True only on GET `/config` → 200; the version-specific serve API makes live serves that 404 or answer 5xx while degraded read as DOWN:
- spawn-gate (431) spawns a duplicate serve (EADDRINUSE);
- config-drift drain (439–444) breaks on a still-bound port;
- `sdd_autonomous_cycle.py:165` force-recycles a healthy serve mid-cycle.

**Outcome**: live serves are never duplicated or recycled for answering non-2xx.

## Scope

### In Scope

- Probe: any received HTTP response = ALIVE; only `httpx.HTTPError` (ConnectError/ConnectTimeout/ReadTimeout) or `OSError` = DOWN.
- Timeout 5.0s → 3.0s; docstring restated with aliveness semantics.
- Name/signature unchanged — call sites (431, 442, 509) and `sdd_autonomous_cycle.py:165` inherit.
- New regression tests; existing 200/ConnectError tests keep passing.

### Out of Scope

- `_serve_health` / `_find_serve_pid` recycle machinery; `/proc` memory pressure; `opencode_chat` / `opencode_chat_stream` / permission relay / wedge detector.
- New config keys; dependency changes; docs.
- `opencode-serve-config.opencode.jsonc` — uncommitted local file; do not touch or commit.

## Exploration Summary

Probe = GET `/config`, 200-only; "alive" = "transport responding ⇒ port held". Drain worst case ≈13s (4 × 3.25s) vs ~21s; slow-boot transient False self-heals — accepted. Harness: `_FakeClient.get_status` scripts status; `raise_on == "get"` → ConnectError; add `get_read_timeout` → `httpx.ReadTimeout`.

## Assumptions & Edge Cases

- A 404/5xx responder holds the port — spawn fails EADDRINUSE anyway; not down = no duplicate spawn.
- Slow first boot (> 3s) → transient False → self-heals; accepted.
- Drain bounded ≈13s (was ~21s).

## Capabilities

`openspec/specs/` researched — no spec covers the bridge serve lifecycle.

### New Capabilities

- `opencode-serve-liveness`: probe liveness semantics — any HTTP status = alive, transport failure only = down; bounded drain latency; slow-boot self-heal accepted. Consumed by spawn gate, drain, cycle driver.

### Modified Capabilities

None.

## Approach

Replace `resp.status_code == 200` with "response received ⇒ alive" (buffered `.get()`); timeout 3.0; restate docstring. Tests: scripted statuses (404/500/3xx → True), ReadTimeout branch (→ False). No call-site changes.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `opencode_bridge.py:406–413` | Modified | Probe: any HTTP response = alive; transport failure only = down; timeout 3.0s. |
| `tests/test_opencode_bridge.py` | Modified | New aliveness tests (404/500/3xx→True, ReadTimeout→False); existing untouched. |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Slow first boot within 3s → transient false DOWN | Med | Self-heals next poll; documented. |
| Non-serve process answering the port looks "alive" | Low | Port held ⇒ spawn would EADDRINUSE anyway; same class as today. |
| ReadTimeout branch complicates fake client | Low | Additive branch only; existing tests untouched. |

## Rollback Plan

`git revert` the single commit — function-level only; no config/DB/dependency/schema impact; 200-only semantics restored.

## Dependencies

None (httpx pinned 0.28.1; `httpx.ReadTimeout` importable).

## Success Criteria

- [ ] Probe: True on 200/404/500/3xx; False on ConnectError/ReadTimeout.
- [ ] `ensure_opencode_serve()` never spawns while a live serve answers 404/5xx.
- [ ] Drain bounded ≈13s.
- [ ] Diff touches only probe + tests.
- [ ] `pytest tests/ -q` green; `opencode-serve-config.opencode.jsonc` untouched.

## Delivery Shape

| Forecast item | Value |
|---------------|-------|
| Estimated changed lines | ~60–90 |
| 400-line review budget risk | Low |
| Chained PRs recommended | No |
| Delivery strategy | single-pr |
| Decision needed before apply | No |

Single PR, under budget; tests hermetic.
