# Proposal: Bridge Cycle 6 — Streaming-Path Error-Exit Hygiene + Serve-Recycle Ordering

**One-line summary**: Close the three `opencode_chat_stream` error exits that leak sessions (prompt HTTP != 204, `message.updated` error, outer network except), DRY the cleanup trio onto one shared best-effort helper, and fix the serve-recycle-before-respawn ordering bug that fails one request every ~30 min of serve uptime. ~200 lines incl. tests, single PR.

## Intent (Why)

Cycle 5 hardened the blocking path (`opencode_chat`); the streaming path still has gaps:

- **Session leaks on error exits**: the wedge and timeout exits already abort the session, drop the `session_map` pin, and pop `pending_permissions`. Three other exits do NOT: `prompt_async` HTTP != 204 (`:732-734`), a `message.updated` event carrying `info.error` (`:1059-1061`), and the outer `except (httpx.HTTPError, OSError, ValueError)` (`:1079-1080`). A transient mid-stream failure (SSE ReadError, `GET /message` or `GET /permission` failure) leaves the agent possibly still running on the serve, with the pin pointing at it; the next request for that conversation only survives if the busy-check backstop catches it.
- **Serve-recycle ordering bug**: `opencode_chat_stream` calls `ensure_opencode_serve()` (`:637`) and only THEN `_recycle_serve_if_low_memory()` (`:648`). The recycle SIGTERMs the serve (`os.kill(pid, 15)`, `:1543`) when memory pressure or uptime > `_SERVE_RECYCLE_AFTER_S` (1800s, `:98`) and does NOT respawn. The session POST at `:690` then fails with `ConnectError` → spurious `[OpenCode Bridge Network Error: ...]`. Deterministic recurrence roughly every 30 minutes of serve uptime. The autonomous path already orders this correctly (force-recycle at `:635-636` before ensure at `:637`).

**Outcome**: every streaming error exit cleans up its session (abort + pin-drop + pending-pop); a recycled serve is respawned before any request is attempted; transient mid-stream failures cannot strand a running agent.

## Scope

### In Scope

- `opencode_bridge.py` only:
  - New shared helper `_abort_stream_session_best_effort(client, session_id)` — best-effort abort POST `/session/{id}/abort`, guards falsy `session_id`, never raises. Named distinctly from cycle-5's planned `_abort_session_best_effort` (blocking path) to keep the two cycles' helpers merge-safe.
  - Apply the full cleanup trio (abort + `session_map.pop(session_key)` + `pending_permissions.pop(session_id)`) to the three leaking exits in `opencode_chat_stream`: prompt HTTP != 204, `message.updated` error, outer network except.
  - DRY the four existing inline cleanup sites (timeout `:755-768`, two wedge sites `:944-953` / `:988-998`, plus the new ones) onto the helper where it is a straight replacement.
  - Fix recycle ordering: in `opencode_chat_stream`, move `_recycle_serve_if_low_memory()` BEFORE `ensure_opencode_serve()` on the non-autonomous path (mirroring the autonomous ordering), so a recycle is always followed by a fresh ensure. `_recycle_serve_if_low_memory` itself untouched.
- `tests/test_opencode_bridge.py`: additive `_FakeClient` scripting (`prompt_status` attribute mirroring `message_status`; `fail_get_after` mid-stream raise switch) + new hermetic tests (see Test Approach).

### Out of Scope

- `opencode_chat` blocking path (cycle 5's scope), routes.py, proxy.py, config, systemd.
- Wedge detection, zombie sweep, escalation behavior, error-string formats.
- `_recycle_serve_if_low_memory` / `_force_recycle_serve` internals.

## Exploration Summary

Verified against source (orchestrator gatekeeper, 2026-08-09):

- Wedge/timeout exits already do the full trio: timeout `:755-768` (abort + pin-drop + pending-pop), wedge `:944-953` and `:988-998` (same trio). The cleanup pattern is repeated inline 4+ times — DRY-able.
- `prompt_async` non-204 (`:732-734`) returns after the session was created AND pinned (`:702-703`) — pin leak + live session.
- `message.updated` with `info.error` (`:1059-1061`) returns with no cleanup.
- Outer except (`:1079-1080`) returns with no cleanup; a mid-loop `/event` SSE read, `GET /message` or `GET /permission` failure triggers it (all `httpx.HTTPError` subclasses).
- `_recycle_serve_if_low_memory` (`:1511`) SIGTERMs via `os.kill(pid, 15)` on low memory or uptime > 1800s and never respawns; `ensure_opencode_serve` (`:433`) is the respawn path. Ordering in the stream path is ensure-then-recycle (`:637`/`:648`), so a recycle always breaks the next request.
- Hermetic harness: `_FakeClient` records `post_calls` (abort observability), `TestHermeticServe`/`TestServeStability` monkeypatch `ensure_opencode_serve`/`_recycle_serve_if_low_memory`/`os.kill`/`_find_serve_pid` — never touches a real serve. No mid-stream raise facility yet (`raise_on` is first-call only); no `prompt_status` attribute yet.

## Assumptions & Edge Cases

- Aborting on the outer except intentionally matches the established wedge/timeout policy: a transient failure ends the session rather than resuming a possibly-stale agent; the dropped pin makes the next request start fresh.
- The helper is best-effort: abort failure only logs and never changes the returned error string.
- `fail_get_after` fires exactly once at the Nth GET, then every subsequent GET fails — simulates a mid-loop `/permission` or `/message` failure deterministically.
- Existing 130 bridge tests stay green; success-path behavior is unchanged (the only non-error change is the recycle/ensure reorder, which is a no-op when no recycle fires).

## Capabilities

Prior spec: `openspec/specs/opencode-bridge-blocking-path/` (cycle 5 — blocking path). This change touches a different code region with its own requirements.

### New Capabilities

- `opencode-bridge-stream-exit-hygiene`: streaming-path error-exit cleanup (abort + pin-drop + pending-pop on every error exit, shared best-effort helper) and serve-recycle-before-respawn ordering.

### Modified Capabilities

None.

## Approach

1. `_FakeClient`: add `prompt_status: int = 204` (checked in the `prompt_async` POST branch) and `fail_get_after: Optional[int] = None` (N successful GETs, then `httpx.ReadTimeout` on every GET).
2. New helper `_abort_stream_session_best_effort` in `opencode_bridge.py` near the stream path; refactor the existing inline cleanup trios onto it.
3. Apply the trio to the three leaking exits.
4. Move `_recycle_serve_if_low_memory()` before `ensure_opencode_serve()` in `opencode_chat_stream` (non-autonomous path).
5. Four new hermetic tests (below); run `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q`.

## Test Approach

New tests (hermetic, no live serve):

1. `test_stream_network_error_aborts_and_drops_pin` — session created, then a mid-stream GET raises `httpx.ReadTimeout` (`fail_get_after`): yielded `[OpenCode Bridge Network Error: ...]`, `/abort` POST recorded in `post_calls`, `session_key` popped from `session_map`, `pending_permissions` cleared.
2. `test_message_updated_error_aborts_and_drops_pin` — `stream_lines` carries a `message.updated` event with `info.error`: error yielded, abort recorded, pin dropped.
3. `test_prompt_non_204_drops_pin` — `prompt_status = 500`: error yielded, abort recorded, pin popped.
4. `test_recycle_before_ensure_no_failure` — monkeypatched `_recycle_serve_if_low_memory` (kills serve) + `ensure_opencode_serve` (respawn): stream completes instead of yielding a network error; no kill observed after ensure.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `opencode_bridge.py` | Modified | `_abort_stream_session_best_effort` helper; cleanup trio on 3 leaking exits + DRY of existing sites; recycle-before-ensure reorder in `opencode_chat_stream`; `_recycle_serve_if_low_memory` untouched. |
| `tests/test_opencode_bridge.py` | Modified | `_FakeClient` scripting (`prompt_status`, `fail_get_after`); 4 new tests in a new `TestStreamExitHygiene` class. |

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~200 |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | Single PR |
| Delivery strategy | single-pr |

Decision needed before apply: No
Chained PRs recommended: No
400-line budget risk: Low
