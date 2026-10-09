# OpenCode Bridge Streaming-Path Exit-Hygiene Specification

## Purpose

The streaming path (`opencode_chat_stream`) SHALL stop leaking sessions on its three error exits that currently return without cleanup, and SHALL no longer fail one request after every serve recycle. Every error exit in the streaming path SHALL perform the same cleanup trio the wedge/timeout exits already perform (best-effort abort, pin drop, pending-permission pop), implemented through one shared best-effort helper. The serve-recycle check SHALL run before the ensure/respawn step so a recycled serve is always respawned before any request is attempted. Success-path behavior is byte-for-byte unchanged.

## Requirements

### REQ-1: Shared best-effort stream cleanup helper

`opencode_bridge.py` SHALL define `_abort_stream_session_best_effort(client: httpx.AsyncClient, session_id: str) -> None` used by the streaming-path error exits. It SHALL issue `POST /session/{id}/abort` best-effort, SHALL guard falsy `session_id`, SHALL catch `(httpx.HTTPError, OSError)` and SHALL never raise. A failed abort SHALL only log (existing logger pattern) and SHALL never change the error string the caller yields. The helper's name SHALL be distinct from the blocking-path helper `_abort_session_best_effort` (cycle-5) so the two cycles merge safely.

#### Scenario-1: Abort endpoint fails

- GIVEN the session abort POST raises `httpx.ReadTimeout`
- WHEN the helper runs
- THEN the exception is caught and swallowed
- AND the caller's error string is yielded unchanged

#### Scenario-2: No session id

- GIVEN a falsy/empty session id
- WHEN the helper runs
- THEN it returns immediately with no HTTP call

### REQ-2: Prompt non-204 exit cleans up

When `POST /session/{id}/prompt_async` returns HTTP != 204 (`opencode_chat_stream`), the exit SHALL run the cleanup trio via `_abort_stream_session_best_effort`, drop the `session_map` pin for `session_key` (when `session_map` and `session_key` are present), pop `pending_permissions[session_id]` (when present), and then yield `[OpenCode Bridge Error: prompt HTTP <code>]` exactly as today.

#### Scenario-1: Prompt HTTP 500

- GIVEN `prompt_async` returns HTTP 500 after the session was created and pinned
- WHEN the error branch runs
- THEN `POST /session/{id}/abort` is issued best-effort
- AND the `session_map` pin is dropped and `pending_permissions` entry popped
- AND the yielded string is `[OpenCode Bridge Error: prompt HTTP 500]`

### REQ-3: message.updated error exit cleans up

When a `message.updated` event carries `info.error` (`opencode_chat_stream`), the exit SHALL run the cleanup trio, drop the pin, pop `pending_permissions`, and then yield `[OpenCode Bridge Error: {info['error']}]` exactly as today.

#### Scenario-1: Agent error event

- GIVEN the event bus delivers `message.updated` with `info.error` set
- WHEN the error branch runs
- THEN the session is aborted best-effort, the pin dropped, and the pending entry popped
- AND the yielded string is `[OpenCode Bridge Error: <info.error>]`

### REQ-4: Outer network error exit cleans up

The outer `except (httpx.HTTPError, OSError, ValueError)` of `opencode_chat_stream` SHALL run the cleanup trio (abort best-effort, pin drop, `pending_permissions` pop) before yielding `[OpenCode Bridge Network Error: ...]`. This covers mid-loop failures on `/event` SSE reads, `GET /message`, and `GET /permission`.

#### Scenario-1: Mid-stream GET fails

- GIVEN the event loop's next GET (e.g. `/permission`) raises `httpx.ReadTimeout` mid-stream
- WHEN the outer except runs
- THEN the session is aborted best-effort, the pin dropped, and the pending entry popped
- AND the yielded string starts with `[OpenCode Bridge Network Error:`

### REQ-5: Success and existing cleanup paths unchanged

Success paths SHALL NOT abort: a stream that completes normally (final step + idle session) yields the assistant parts and leaves no session pinned. The existing wedge and timeout cleanup exits SHALL keep their behavior (abort + pin-drop + pending-pop with the same error strings). The DRY refactor SHALL be a straight replacement (same calls, same order, same yield strings).

#### Scenario-1: Suite green

- GIVEN the implemented changes
- WHEN `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` runs
- THEN all existing 130 tests and the new tests pass without a live serve or real /proc

### REQ-6: Recycle before ensure

In `opencode_chat_stream`, on the non-autonomous path, `_recycle_serve_if_low_memory()` SHALL run BEFORE `ensure_opencode_serve()` (mirroring the autonomous path's force-recycle-before-ensure ordering), so a recycle (low memory or uptime > `_SERVE_RECYCLE_AFTER_S`) is always followed by a fresh ensure/respawn before any session POST. `_recycle_serve_if_low_memory` and `_force_recycle_serve` internals are untouched; the autonomous path keeps its existing force-recycle ordering.

#### Scenario-1: Uptime recycle

- GIVEN the serve has been up longer than `_SERVE_RECYCLE_AFTER_S`
- WHEN a streaming request starts
- THEN the recycle runs first and kills the serve
- AND `ensure_opencode_serve` respawns a fresh serve
- AND the request completes instead of yielding a network error

#### Scenario-2: No recycle

- GIVEN the serve is healthy and young
- WHEN a streaming request starts
- THEN the reorder is a no-op
- AND behavior is identical to today

### REQ-7: Regression coverage

New code paths SHALL have hermetic regression tests in `tests/test_opencode_bridge.py` extending `_FakeClient` with: (a) `prompt_status: int = 204` scripted on the `prompt_async` POST branch; (b) `fail_get_after: Optional[int] = None` (the first N GETs succeed, then every GET raises `httpx.ReadTimeout`); (c) abort observability through the recorded `post_calls`. A `TestStreamExitHygiene` class SHALL cover: mid-stream GET failure → network error + abort + pin drop + pending-pop; `message.updated` error → error + abort + pin drop; `prompt_status = 500` → prompt HTTP error + abort + pin pop; recycle-before-ensure ordering via monkeypatched `_recycle_serve_if_low_memory` (kills serve) + `ensure_opencode_serve` (respawns) → stream completes with no spurious network error and no kill after ensure.

#### Scenario-1: Hermetic determinism

- GIVEN the implemented tests
- WHEN they run in `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q`
- THEN they pass without a live serve, real /proc, or network access

## Notes

- Capability directory: `openspec/specs/opencode-bridge-stream-exit-hygiene/`. This is a distinct code region from the cycle-5 blocking-path capability (`opencode-bridge-blocking-path`); the two helper names differ intentionally.
- No changes to routes.py, proxy.py, config, systemd, error-string formats, wedge detection, zombie sweep, or escalation behavior.
- `opencode-serve-config.opencode.jsonc` is never touched by this change.
