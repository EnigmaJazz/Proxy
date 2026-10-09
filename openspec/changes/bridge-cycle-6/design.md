# Design: Bridge Cycle 6 — Streaming-Path Error-Exit Hygiene + Serve-Recycle Ordering

## Overview

`opencode_chat_stream()` (opencode_bridge.py :677-1156) is the interactive streaming path (routes.py `/opencode`, SDD cycle drivers, opencode-sdd model). Cycle 5 hardened the BLOCKING path (`opencode_chat`, :537-675); the streaming path still has three error exits that return without cleanup, and one ordering bug that fails a request after every serve recycle:

- **Leak exits**: (1) `prompt_async` HTTP != 204 (:806-810) — the session was created AND pinned before the POST, then abandoned; (2) `message.updated` with `info.error` (:1132-1136); (3) the outer `except (httpx.HTTPError, OSError, ValueError)` (:1154-1155) — a transient mid-loop failure (SSE read, `GET /message`, `GET /permission`, `GET /session/status`) leaves the agent possibly still running on the serve with the `session_map` pin pointing at it. The wedge/timeout exits already do the cleanup trio (best-effort abort + pin drop + `pending_permissions` pop) — see :754-763 (pinned-busy), :831-843 (timeout), :880-891 (polling timeout), :1013-1035 and :1058-1078 (wedge).
- **Recycle ordering**: `ensure_opencode_serve()` (:712) runs BEFORE `_recycle_serve_if_low_memory()` (:723). The recycle SIGTERMs the serve (`os.kill(pid, 15)`, :1511-1547) on low memory or uptime > `_SERVE_RECYCLE_AFTER_S` (1800.0s, :98) and does NOT respawn; the next session POST then fails with `ConnectError` → spurious `[OpenCode Bridge Network Error: ...]`. Deterministic roughly every 30 minutes of serve uptime. The autonomous path already orders correctly (force-recycle :709-711 before ensure :712).

This design closes the three exits with the established cleanup trio via one shared best-effort helper, DRYs the five existing inline cleanup sites onto it, and moves the recycle check before the ensure. Success-path bytes are unchanged; all 136 existing bridge tests must stay green.

## Key Design Decisions

| Decision | Choice | Alternatives considered |
|---|---|---|
| D1 | New helper `_abort_stream_session_best_effort(client, session_id, *, session_map=None, session_key=None, pending_permissions=None)`: calls the cycle-5 abort helper `_abort_session_best_effort` (single abort implementation), then pops `pending_permissions[session_id]` and `session_map[session_key]` when the containers/keys are provided. Guards falsy `session_id`; never raises. | Abort-only helper with inline pops at every site — duplicates the two pops 8×; a full-trio helper with optional args is a straight replacement at every site. |
| D2 | Distinct helper name from cycle-5's `_abort_session_best_effort` so the two cycles' code is merge-safe and each helper documents its path. | Reusing/renaming the cycle-5 helper — churn and merge risk; rejected. |
| D3 | Recycle ordering: move `await _recycle_serve_if_low_memory()` to BEFORE `if not await ensure_opencode_serve():` (right after the autonomous force-recycle block). `_abort_zombie_sessions` stays AFTER ensure (it needs a live serve). `_recycle_serve_if_low_memory` internals untouched. | Re-ensure after recycle — leaves a dead-serve window and duplicates ensure logic; rejected. Recycle inside the try after client creation — keeps the bug (kill after ensure); rejected. |
| D4 | Leak exits call the trio helper then yield the EXISTING error strings verbatim (`[OpenCode Bridge Error: prompt HTTP <code>]`, `[OpenCode Bridge Error: <info.error>]`, `[OpenCode Bridge Network Error: <str(exc)>]`). Outer except guards with `if session_id:` (session create itself may have failed inside the try). | Changing error strings — rejected (monitoring/UX contract). Aborting only when pinned — rejected: standalone callers also leak sessions. |
| D5 | DRY is a straight replacement at the five existing sites (same calls, same order, same yields); helper does abort → pending-pop → pin-pop. Existing sites differ only in pop order (semantically independent dict pops). | Full refactor of the site control flow — churn without behavior change; rejected. |
| D6 | Test scripting: `_FakeClient` gains `prompt_status: int = 204` (checked on the `prompt_async` POST branch, mirroring `message_status`) and `fail_get_after: Optional[int] = None` (a `_get_count` counter; every GET after the Nth raises `httpx.ReadTimeout`). Existing `raise_on`/`raise_timeout_on` behavior unchanged. | A per-URL failure switch — overkill; a simple Nth-GET switch covers all three GET endpoints deterministically. |

## Detailed Changes

### 1. `opencode_bridge.py` — new helper (right after `_abort_session_best_effort`, ~:325)

```python
async def _abort_stream_session_best_effort(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    session_map: Optional[dict[str, str]] = None,
    session_key: Optional[str] = None,
    pending_permissions: Optional[dict[str, tuple[str, bool]]] = None,
) -> None:
    """Streaming-path cleanup trio: best-effort abort + pin drop + pending pop.

    Distinct from the blocking-path helper ``_abort_session_best_effort``
    (cycle 5) so the two paths stay merge-safe.  Never raises; a falsy
    ``session_id`` (session create failed) is a no-op.
    """
    if not session_id:
        return
    await _abort_session_best_effort(client, session_id)
    if pending_permissions is not None:
        pending_permissions.pop(session_id, None)
    if session_map is not None and session_key:
        session_map.pop(session_key, None)
```

### 2. `opencode_bridge.py` — recycle before ensure (in `opencode_chat_stream`)

Move the `await _recycle_serve_if_low_memory()` line from inside the try (:723) to immediately after the autonomous force-recycle block (:709-711), before `if not await ensure_opencode_serve():` (:712). Resulting order for the non-autonomous path: recycle → ensure (respawn if needed) → client ctx → try → zombie sweep → pinned-session check → session create/pin → prompt_async → event loop. The autonomous path is unchanged (`_force_recycle_serve` already precedes ensure).

### 3. `opencode_bridge.py` — close the three leak exits

Prompt non-204 (:806-810):

```python
if async_resp.status_code != 204:
    await _abort_stream_session_best_effort(
        client, session_id,
        session_map=session_map, session_key=session_key,
        pending_permissions=pending_permissions,
    )
    yield ("status", f"[OpenCode Bridge Error: prompt HTTP {async_resp.status_code}]")
    return
```

`message.updated` error (:1132-1136): before the existing `yield ("status", f"[OpenCode Bridge Error: {info['error']}]")` + return, insert the same trio call.

Outer except (:1154-1155):

```python
except (httpx.HTTPError, OSError, ValueError) as exc:
    if session_id:
        await _abort_stream_session_best_effort(
            client, session_id,
            session_map=session_map, session_key=session_key,
            pending_permissions=pending_permissions,
        )
    yield ("status", f"[OpenCode Bridge Network Error: {str(exc)}]")
```

### 4. `opencode_bridge.py` — DRY the five existing cleanup sites

Each of the five sites (pinned-busy :754-763, timeout :831-843, polling timeout :880-891, wedge :1013-1035, wedge :1058-1078) currently inlines the abort try/except + two pops. Replace each abort block + pops with one helper call (pinned-busy keeps its `session_id = None` after; yields stay verbatim). Net −2 to −4 lines per site.

### 5. `tests/test_opencode_bridge.py` — `_FakeClient` scripting

```python
self.prompt_status: int = 204
self.fail_get_after: Optional[int] = None
self._get_count: int = 0
```

In `post()`: `if "prompt_async" in url: return _FakeResp(self.prompt_status, {})`.
In `get()`: after the `raise_timeout_on` check, `self._get_count += 1; if self.fail_get_after is not None and self._get_count > self.fail_get_after: raise httpx.ReadTimeout("read timed out")`.

### 6. `tests/test_opencode_bridge.py` — `TestStreamExitHygiene` (after `TestOpenCodeChatHardening`)

All tests: `@pytest.mark.asyncio`, monkeypatched `ensure_opencode_serve` → `async def _running(*a, **k) -> bool: return True`, `httpx.AsyncClient` → `_FakeClient`, session pinned via `session_map={}`, `session_key="conv"`, pending via a fresh dict `pending={}` (NOT the shared `PP` — hermetic per-test isolation).

| Test | Scripting | Assertions |
|---|---|---|
| `test_stream_network_error_aborts_and_drops_pin` | `stream_lines = []` (event bus ends → polling fallback), `fail_get_after = 1` (GET #1 = zombie-sweep `/session` list is swallowed by its own try/except; GET #2 = `/session/status` raises) | one `("status", ...)` delta starting `[OpenCode Bridge Network Error:`; `any(u.endswith("/abort") for u, _ in post_calls)`; `session_map == {}` (pin dropped); `pending == {}` (popped) |
| `test_message_updated_error_aborts_and_drops_pin` | `stream_lines = [_evt("message.updated", sessionID="ses_0001", info={"id": "msg_a", "role": "assistant", "error": "boom"})]` | `deltas == [("status", "[OpenCode Bridge Error: boom]")]`; abort recorded; pin dropped; pending popped |
| `test_prompt_non_204_drops_pin` | `prompt_status = 500`, `stream_lines = []` | `deltas == [("status", "[OpenCode Bridge Error: prompt HTTP 500]")]`; abort recorded; pin popped; pending popped |
| `test_recycle_before_ensure_no_failure` | `order: list[str]`; monkeypatch `_recycle_serve_if_low_memory` → async recorder appending `"recycle"` (overrides the autouse hermetic noop); `ensure_opencode_serve` → `_running` appending `"ensure"`; `stream_lines = _stream_events()` | `order == ["recycle", "ensure"]`; deltas contain `("text", ...)` with "Created file." and "Done."; no `status`/error delta; no abort |

## Test Plan

| Phase | Command | Expectation |
|---|---|---|
| Phase 1 (RED) | add `TestStreamExitHygiene` + scripting, run `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q -x` | new tests FAIL (exits still leak / order still wrong); existing 136 pass |
| Phase 2 (GREEN) | apply bridge edits, run `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` | 140 pass |
| Phase 3 (suite) | `.venv/bin/python -m pytest tests/ -q` | full suite green (guards: 412+ tests, no live serve touched — hermetic_serve autouse asserts) |

## Risks & Open Questions

- **Behavior change (intended)**: aborting on a transient mid-stream failure now ends the session instead of leaving it pinned; the dropped pin makes the next request start fresh. Matches the established wedge/timeout policy; no monitoring contract change (error strings verbatim).
- **Helper reuse**: `_abort_stream_session_best_effort` calls cycle-5's `_abort_session_best_effort` — if cycle-5's helper is ever renamed, cycle-6 must follow. Both live in the same module; the call chain is explicit.
- **Zombie-sweep GET ordering**: `fail_get_after = 1` relies on the sweep's `/session` GET being swallowed internally — verified (`_abort_zombie_sessions` wraps everything in try/except `(httpx.HTTPError, OSError, ValueError)`).
- **Budget**: ~45 bridge lines + ~120 test lines ≈ 165 changed lines — well under the 400-line budget; single PR.
- **Out of scope**: blocking path (`opencode_chat`), routes.py, proxy.py, config, systemd, wedge/zombie/escalation logic, error-string formats, `_recycle_serve_if_low_memory` internals.
