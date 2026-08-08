# Design: OpenCode Bridge SDD Reliability

## Technical Approach

Four independent, revertible layers, each backed by one spec requirement: (1) relay/auto-allow policy (REQ-1/2/3), (2) test hermeticity (test-infrastructure REQ-2), (3) serve stability mode (REQ-4) + config-drift (REQ-5), (4) driver parameterization + stall handling (REQ-6). REQ-7 (3 consecutive cycles) is the verification headline; the instrument is the autonomous driver. All proxy-side changes live at the repo root (no `proxy/` prefix); the bridge module is `opencode_bridge.py`.

## Architecture Decisions

### Decision: Autonomous-mode signal carrier
- **Choice**: Add explicit `autonomous: bool = False` parameter to `opencode_chat_stream` and `_opencode_task_response`; `_handle_opencode_request` passes `autonomous=sdd` (the one place that resolves `requested_model == "opencode-sdd"`).
- **Alternatives**: infer from `timeout > OPENCODE_SERVE_TIMEOUT` (the existing line-541 trigger); read the model key deep inside the bridge.
- **Rationale**: explicit flag is the single, readable boundary the event handler closes over; the SDD model key is resolved once in `routes.py` (Rule 5 — structured state at the boundary). Long-timeout inference would silently auto-allow on any future >600s call.

### Decision: Relay extension + auto-allow policy
- **Choice**: Extend `_RELAYED_PERMISSION_TYPES` to `("external_directory", "bash", "write", "edit")` (REQ-1). Factor the four duplicated permission blocks (event-bus `permission.updated` ~1003, polling-wedge ~827, event-bus-timeout ~917, completion `_resolve_pending_permission` ~275) into one helper `_handle_permission_event(client, session_id, perm, pending_permissions, *, autonomous) -> Optional[str]`. When `autonomous` it POSTs `"always"` via `_post_permission_response` and returns `None` (no question surfaced, no pending state stored) for ALL relayed types incl. `write`/`edit`/git. Interactive mode keeps the read→auto-allow / write→relay split (REQ-1).
- **Rationale**: collapses ~4 clones; auto-allow failure already bounded by `_post_permission_response` (10s timeout, never raises) — fits Rule 2 (no in-flight injection: the answer goes to the serve, not the client stream).
- git asks fire as `type == "bash"` — already in the set; auto-allow covers REQ-2 Scenario-2.

### Decision: /opencode parity (REQ-3)
- **Choice**: Route `_handle_opencode_command` (routes.py:2501) through `_opencode_task_response(..., client_stream=True, pending_permissions=_pending_permissions_state(app))` instead of the bare `opencode_chat` blocking call. Plain-text response shape is preserved: the stream's `text` deltas are emitted as SSE `content` chunks (identical to today's single-chunk shape); the only visible change is a `⏳ [Proxy: Directing to OpenCode...]` status line, which is an improvement.
- **Alternatives**: document partial parity and keep `opencode_chat` (no relay).
- **Rationale**: full parity removes a silent write/edit drop path with one call swap; the stream path already owns session/permission handling.

### Decision: Test hermeticity fixture (test-infrastructure REQ-2)
- **Choice**: Module-level `autouse` function-scope fixture `hermetic_serve` in `tests/test_opencode_bridge.py`. It: records the live serve pid once (`_find_serve_pid(port)`); noops `_recycle_serve_if_low_memory` and `_force_recycle_serve` (async noops); wraps `opencode_bridge.os.kill` with a recorder (`killed_pids: list[int]`). Teardown asserts the recorded serve pid is STILL ALIVE and `serve_pid not in killed_pids`. Tests that exercise the REAL recycle primitive (`TestServeHealth`) opt out via `@pytest.mark.real_recycle` → fixture skips the noop patches but still runs the guard (their patched `_find_serve_pid` returns `12345`/`None`, never the live serve pid, so the guard is vacuously satisfied).
- **Rationale**: function scope lets each test's `monkeypatch` override `os.kill` where it must (Rule 7 `@pytest_asyncio.fixture` elsewhere; these are sync monkeypatch-only tests). The guard catches any future real-stream path (e.g. the wedge kill at lines 901/984) that escapes the noop.
- Fixture must NOT affect: `TestServeHealth` (marker-gated), and pure-unit helpers (`_find_serve_pid`, `_serve_health`, `_strip_proxy_status_text`).

### Decision: Stability mode — candidate B first (REQ-4)
- **Choice**: Default first candidate = **B (serve-scoped config dir)**: spawn the serve with `XDG_CONFIG_HOME={OPENCODE_WORKSPACE_DIR}/serve-config`, holding a copy of `~/.config/opencode/opencode.json` with a REDUCED plugin array that KEEPS `opencode-rate-limit-fallback-mapped` and DROPS the wedge-prone `skill-registry.ts`, `review-result-artifacts.ts`, `model-variants.ts`. Fallback candidate = **A (`--pure`)** via `OPENCODE_SERVE_PURE=1` env toggling a `--pure` arg in `ensure_opencode_serve`.
- **Empirical validation protocol** (apply phase, per REQ-4): for each candidate run ONE `scripts/sdd_autonomous_cycle.py <change>` cycle; assert (i) cycle completes proposal → archive with no `[OpenCode Bridge Error: ... wedged ...]` or `[timed out]` in driver output, (ii) `~/.local/share/opencode/logs/rate-limit-fallback.log` (and project `.git/gentle-ai/rate-limit-fallback.log`) shows NO `fallback_chain_exhausted`, (iii) `opencode-serve.log` shows no panic/crash, (iv) at least one `fallback_cycle_started → COMPLETED` survives in the log (proves replay preserved). A candidate that completes the cycle but breaks (ii)/(iv) is REJECTED even though (i) passed (REQ-4 Scenario-3).
- **Rationale**: `--pure` is blunt — it disables ALL plugin auto-load incl. the fallback plugin mid-cycle, and the orchestrator's sub-agent "Task cancelled" reconciliation depends on it. Candidate B is surgical and structurally protects the REQ-4 invariant; the proposal risk register agrees ("prefer plugin reduction if it passes"). Switching to A only if B fails (iii).
- **TUI isolation**: `XDG_CONFIG_HOME` is set only on the serve subprocess env (`serve_env`); the user's TUI keeps `~/.config/opencode` via the user's real XDG. No TUI behavior change.

### Decision: Config-drift auto-detect (REQ-5)
- **Choice**: mtime-based detect inside `ensure_opencode_serve`. Module-level `_serve_config_mtime: Optional[float] = None`; on entry, stat `~/.config/opencode/opencode.json` (guard `OSError` → treat as no-drift); if a serve IS running AND `mtime > _serve_config_mtime` → kill + clear pid cache (reuse the `_force_recycle_serve` kill path) then fall through to respawn, and update the cached mtime to the new file. Set `_serve_config_mtime` right after every successful spawn.
- **Alternatives**: per-request check (too hot, the gate is `ensure_opencode_serve` which already runs per request — same cost, so we keep it there); manual-documented step (rejected: drift wedges are silent and fatal).
- **Rationale**: one file covers the hot-edit case (plugin array changes); the check lives at the single spawn/health gate so it fires before the next request rebuilds state.

## Data Flow

    request → routes.py
      model "opencode-sdd" → _handle_opencode_request(autonomous=sdd)
      /opencode              → _handle_opencode_command → _opencode_task_response(autonomous=False)
        → opencode_chat_stream(autonomous=...) → ensure_opencode_serve()
            └─ config-drift mtime check → recycle if changed
        → /event bus ──→ _handle_permission_event(autonomous)
                            │ autonomous True  → POST /permissions/{id} {"response":"always"} (no client surface)
                            │ autonomous False → write/edit → ("question", text) (relay)
        → text deltas → SSE content chunks

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `opencode_bridge.py` | Modify | `_RELAYED_PERMISSION_TYPES` += `write`/`edit`; new `_handle_permission_event(...) -> Optional[str]` helper; replace 4 relay blocks; `opencode_chat_stream` + force-recycle keyed on new `autonomous` param (replaces line-541 timeout inference); `ensure_opencode_serve` config-drift mtime check + `_serve_config_mtime` cache + `--pure`/`XDG_CONFIG_HOME` spawn toggles (`OPENCODE_SERVE_PURE`, `OPENCODE_SERVE_CONFIG_DIR` constants). |
| `routes.py` | Modify | `_opencode_task_response` adds `autonomous` param; `_handle_opencode_request` passes `autonomous=sdd`; `_handle_opencode_command` routes through `_opencode_task_response` (drop bare `opencode_chat`). |
| `constants.py` | Modify | add `OPENCODE_SERVE_PURE: bool = False`, `OPENCODE_SERVE_CONFIG_DIR` default, `OPCODE_CONFIG_PATH`. |
| `tests/test_opencode_bridge.py` | Modify | add autouse `hermetic_serve` fixture + `_real_recycle` marker on `TestServeHealth`; add auto-allow + autonomous tests; add `/opencode` relay test. |
| `scripts/sdd_autonomous_cycle.py` | Modify | argparse `--change NAME` (drops hardcoded `bridge-docs`); glob output keyed on the arg; stall handling (no-delta > `STALL_S=180s` → break + non-zero exit). |
| `scripts/sdd_bridge_cycle.py` | Modify | argparse `--change NAME`/`--desc`; same stall handling. |

**Commit / work-unit ordering** (each independently revertible):
1. fix(bridge): relay set + `_handle_permission_event` helper + `autonomous` auto-allow (REQ-1/2).
2. fix(routes): /opencode parity via stream path (REQ-3).
3. test(bridge): hermetic `hermetic_serve` autouse fixture + os.kill guard (test-infrastructure REQ-2).
4. feat(bridge): serve stability mode env toggles (REQ-4) + config-drift mtime (REQ-5).
5. chore(scripts): driver parameterization + stall handling (REQ-6).

## Interfaces / Contracts

```python
# opencode_bridge.py
_RELAYED_PERMISSION_TYPES: tuple[str, ...] = (
    "external_directory", "bash", "write", "edit",
)
async def opencode_chat_stream(
    user_text: str, *, ..., autonomous: bool = False,
    timeout: float = OPENCODE_SERVE_TIMEOUT,
) -> AsyncIterator[tuple[str, str]]: ...
async def _handle_permission_event(
    client: httpx.AsyncClient, session_id: str, perm: dict[str, Any],
    pending_permissions: dict[str, tuple[str, bool]], *,
    autonomous: bool,
) -> Optional[str]:  # None = handled (auto-allow/relay allow-continue); str = question to relay
async def ensure_opencode_serve() -> bool:  # now config-drift-aware
```

```python
# routes.py
async def _opencode_task_response(
    task_text, client_stream, *, ..., autonomous: bool = False,
) -> Response: ...
async def _handle_opencode_command(user_text: str) -> StreamingResponse:
    # now calls _opencode_task_response(..., autonomous=False, client_stream=True)
```

## Testing Strategy

| Layer | What to Test | Approach |
|-------|-------------|----------|
| Unit | `_classify_permission_access` + `_handle_permission_event` autonomous vs interactive; `/opencode` routes through stream; config-drift mtime triggers recycle. | `_FakeClient` harness; monkeypatch `_find_serve_pid`/`os.kill`. |
| Regression (hermeticity) | `hermetic_serve` guard: serve pid unchanged, `os.kill` never hit serve pid across the 36 stream tests. | autouse fixture teardown assertion. |
| E2E (REQ-7) | 3 consecutive autonomous cycles proposal → archive. | `scripts/sdd_autonomous_cycle.py`; assert no wedge/timeout, fallback log intact. |

## Threat Matrix

| Boundary | Applicability | Design response | RED test |
|---|---|---|---|
| Shell/subprocess (serve spawn) | Applicable — `ensure_opencode_serve` adds `--pure`/`XDG_CONFIG_HOME`. | Toggles are subprocess-env-scoped only; default off; mtime drift recycles deterministically. | spawn carries `--pure` only when `OPENCODE_SERVE_PURE`; drift test kills+respawns. |
| Process integration (serve recycle) | Applicable — drift recycle + force-recycle. | Recycles target ONLY the matched pid (`_find_serve_pid` by `--port`); never the TUI. | recycle does not touch a non-matching pid (fake pid `12345`). |
| Git/VCS automation (auto-allow git asks) | Applicable — autonomous mode POSTs `always` for `bash`-type git asks. | Auto-allow is cycle/serve-scoped; interactive mode keeps git relay to the user. | autonomous `git commit` ask → POST `always`, no client question; interactive → question. |
| Routing (permission relay + /opencode parity) | Applicable — relay set extended, `/opencode` now streams. | Relay on every bridge path incl. `/opencode`; no write/edit silently dropped. | `/opencode` write event surfaces as question (interactive) / auto-allow (autonomous). |
| Documentation-like paths / commit-index / PR-form | N/A — no doc-classification, index-state, or PR-command boundary in this change. | — | — |

## Migration / Rollout

No data migration. Each layer is a single revertible commit (env-flagged stability mode, relay-set one-liner, autouse fixture, route swap, script argparse). Rollback per proposal: revert the commit, rerun one cycle.

## Open Questions

None. Every REQ has a decided implementation:
- REQ-1/2/3 → relay extension + `autonomous` flag + `/opencode` via stream.
- REQ-4 → candidate B (serve-scoped config) first, A (`--pure`) fallback, empirical one-cycle gate.
- REQ-5 → mtime auto-detect in `ensure_opencode_serve`.
- REQ-6 → argparse `--change` + stall handling in both scripts.
- REQ-7 → autonomous driver is the instrument; 3 consecutive cycles is the verify gate.
- test-infrastructure REQ-2 → autouse `hermetic_serve` fixture + os.kill guard + `_real_recycle` marker.