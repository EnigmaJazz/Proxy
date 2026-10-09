# Proposal: Bridge Cycle 1 — Harden the opencode serve Liveness Probe

**One-line summary**: Re-define "serve is up" as *any* HTTP response (2xx/3xx/4xx/5xx) instead of only HTTP 200, so API drift (404 on `/config`) or degraded 5xx answers no longer read as "serve down" — killing duplicate spawns, broken config-drain, and spurious mid-cycle serve recycles. Transport failure only (`httpx.HTTPError` / `OSError`) counts as DOWN. ~60–80 lines, single PR.

## Intent (Why)

`is_opencode_serve_running()` (`opencode_bridge.py:406–413`) returns True only on GET `{OPENCODE_SERVE_URL}/config` → HTTP 200. But the opencode serve HTTP API is version-specific (documented drift warning; v1.18.15 observed). A live serve that 404s `/config` (API drift) or answers 5xx while degraded is misclassified as DOWN, causing:

- `ensure_opencode_serve()` spawn-gate (line 431) spawns a **duplicate serve** that cannot bind the port (EADDRINUSE) and reports "serve not reachable" while a healthy serve runs.
- The config-drift drain loop (lines 439–444) breaks early on a still-bound port.
- `scripts/sdd_autonomous_cycle.py:155` sees phantom "serve down" and **force-recycles a healthy serve mid-cycle**.

**User-facing outcome**: operators and SDD cycle drivers stop seeing phantom "serve down" alerts and duplicate spawns when the serve's API drifts or degrades; a live serve is never force-recycled for answering a non-2xx status.

## Scope

### In Scope

- `is_opencode_serve_running()` semantics: any received HTTP response (2xx/3xx/4xx/5xx) = ALIVE; only transport-level failure = DOWN (`httpx.HTTPError`: ConnectError/ConnectTimeout/ReadTimeout; `OSError`). No `raise_for_status`, no explicit body read (plain buffered `.get()` inside the async context manager).
- Timeout lowered 5.0s → 3.0s; docstring restated with aliveness semantics.
- Public function name/signature unchanged — call sites (lines 431, 442, 509) and `scripts/sdd_autonomous_cycle.py:155` inherit the fix without edits.
- New regression tests (see Success Criteria); existing 200→True and ConnectError→False tests keep passing.

### Out of Scope

- `_serve_health` / `_find_serve_pid` (proc-based recycle machinery, separate purpose), `/proc` memory pressure logic, `opencode_chat` / `opencode_chat_stream` / permission relay / wedge detector.
- New config keys; dependency changes; `docs/opencode-bridge.md` (unmerged on main).
- `opencode-serve-config.opencode.jsonc` — uncommitted local modifications, serve-environment config; **do not touch, do not commit**.

## Exploration Summary

- **Problem evidence**: probe = GET `/config`, 200-only (`opencode_bridge.py:410–411`); consequences at lines 431, 439–444, 509 and `scripts/sdd_autonomous_cycle.py:155`.
- **Fix**: 2xx/3xx/4xx/5xx → alive; only `httpx.HTTPError` (ConnectError/ConnectTimeout/ReadTimeout) + `OSError` → down; timeout 5.0→3.0.
- **Deliberate semantics (document)**: in the drain loop, "alive" = "transport responding ⇒ port held". Worst-case drain latency bounded: 4 × (3s + 0.25s) ≈ 13s vs ~21s today. Slow-boot spawn window: a 3s probe timeout can report failure moments before a slow first boot answers — self-heals on the next `ensure_opencode_serve`; **accepted behavior**.
- **Test harness**: `tests/test_opencode_bridge.py` `_FakeClient.get_status` scripts arbitrary status (default 200); `raise_on == "get"` raises ConnectError; `httpx.ReadTimeout` importable (httpx 0.28.1). Additive branch needed for the read-timeout case.

## Assumptions & Edge Cases

- A 404/5xx responder still owns the port → spawn would fail EADDRINUSE anyway; not treating it as down prevents the duplicate-spawn failure mode.
- Slow boot (first response > 3s) → transient False → next `ensure_opencode_serve()` poll self-heals; accepted, documented.
- Drain loop worst case ≈13s (was ~21s) — bounded and shorter.

## Capabilities

> Contract for sdd-spec. Researched `openspec/specs/` — no existing spec covers the bridge serve lifecycle.

### New Capabilities

- `opencode-serve-liveness`: codifies liveness semantics of the serve probe — any HTTP status = alive, transport failure only = down; bounded drain-loop latency; slow-boot self-heal is accepted behavior. Consumed by spawn gate, config-drift drain, and SDD autonomous cycle.

### Modified Capabilities

None.

## Approach

Minimal diff: replace the `resp.status_code == 200` check with "response received ⇒ alive" (drop `raise_for_status`, keep buffered `.get()`), lower timeout to 3.0, restate docstring. Tests script status via `_FakeClient.get_status` (404/500/3xx) and add a small `raise_on` branch for `ReadTimeout`. No call-site changes.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `opencode_bridge.py:406–413` | Modified | Liveness probe: any HTTP response = alive; transport failure only = down; timeout 3.0s. |
| `tests/test_opencode_bridge.py` | Modified | New aliveness tests (404/500/3xx→True, ReadTimeout→False); existing 200/ConnectError tests unchanged. |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Slow first boot within 3s → transient false DOWN | Med | Self-heals on next `ensure_opencode_serve()`; documented accepted behavior. |
| A non-serve process answering on the port looks "alive" | Low | Same misclassification as today (200 check); a transport response means the port is held — spawn would EADDRINUSE anyway. |
| ReadTimeout test branch complicates the fake client | Low | Small additive `raise_on` branch; existing tests untouched. |

## Rollback Plan

Revert the single commit (`git revert`) — pure function-level change, no config/DB/dependency/schema impact; prior 200-only semantics restored exactly.

## Dependencies

None (httpx already pinned; `httpx.ReadTimeout` available in 0.28.1).

## Success Criteria

- [ ] `is_opencode_serve_running()` → True on 200, 404, 500, and 3xx scripted responses.
- [ ] → False on `httpx.ConnectError` (existing) and `httpx.ReadTimeout`.
- [ ] `ensure_opencode_serve()` does NOT spawn when a live serve answers 404/5xx (no duplicate serve).
- [ ] Drain loop terminates with bounded latency ≈13s (4 × 3.25s).
- [ ] `git diff` touches only `opencode_bridge.py` probe + tests (no recycle machinery, chat paths, config keys, or docs).
- [ ] `pytest tests/ -q` green; `opencode-serve-config.opencode.jsonc` not modified/committed.

## Delivery Shape

| Forecast item | Value |
|---------------|-------|
| Estimated changed lines | ~60–80 |
| 400-line review budget risk | Low |
| Chained PRs recommended | No |
| Delivery strategy | single-pr |
| Decision needed before apply | No |

Single PR, under budget. Bridge tests stay hermetic (never kill a live opencode serve).
