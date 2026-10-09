# Design: Bridge Cycle 5 — Permission-Aware Hardening of the Blocking Escalation Path

## Overview

`opencode_chat()` (opencode_bridge.py :537-600) is the blocking cloud-escalation path, used only by `opencode_escalation()` (:1087) from the queue worker (proxy.py:401). Today it performs a single blocking `POST /session/{id}/message` with `timeout=timeout` (default `OPENCODE_SERVE_TIMEOUT` = 600.0, constants.py:211) and never:

- polls `GET /permission` — an agent parked on a write/bash gate burns the full 600s (headless serve auto-rejects silently, the tool wedges "running", the session stays busy forever and blocks later tasks on the serve's tool runner),
- aborts the created session on any failure (timeout, HTTP error, network error) — a zombie busy session leaks on the serve,
- distinguishes READ (safe to auto-allow) from WRITE (must never be granted unprompted on a headless path).

This design adds a permission-aware deadline loop, a shared best-effort abort helper, session cleanup on every non-success exit, and a keyword-only `autonomous` flag — without touching the plain success path or the streaming path.

## Key Design Decisions

| Decision | Choice | Alternatives considered |
|---|---|---|
| D1 | Message POST runs as `asyncio.create_task`; a poller loop checks `post_task.done()` and polls permissions concurrently. | `asyncio.wait_for` on the whole flow — cannot poll while blocked; `to_thread` — wrong primitive. |
| D2 | Found permissions are handled through the EXISTING `_handle_permission_event` (:298) with the call's `autonomous` value. | Duplicating classification logic — rejected (drift risk, violates single-policy rule already enforced on the streaming path). |
| D3 | WRITE/git ask (handler returns a question string) → abort session + clear error string. No relay, no auto-grant. | Auto-allow writes — rejected: the queue-worker path is headless, no user consent is possible, and the repo's consent discipline forbids unprompted grants. |
| D4 | `autonomous=True` preserves auto-allow-all via the shared handler (existing semantics, REQ-4); `opencode_escalation` stays `False`. | Adding a separate escalation grant policy — rejected: keep one policy, one flag. |
| D5 | Abort via new tiny helper `_abort_session_best_effort(client, session_id)` (never raises). | Inlining the 3-line abort twice — the helper documents the "best-effort" contract and mirrors `_post_permission_response`'s shape. |
| D6 | New module constant `_BLOCKING_PERMISSION_POLL_S: float = 5.0` next to `_WEDGE_CHECK_INTERVAL_S` (:121); poll sleep `min(0.25, poll_s)`. | Polling the serve every 0.25s — wasteful; 5s matches the streaming path's permission-check cadence class. Tests monkeypatch the constant to 0.01. |

## Detailed Changes

### 1. `opencode_bridge.py` — new module constant (near :121)

```python
#: Blocking-path (opencode_chat) permission-poll cadence.
_BLOCKING_PERMISSION_POLL_S: float = 5.0
```

### 2. `opencode_bridge.py` — new helper (near `_post_permission_response`, :275)

```python
async def _abort_session_best_effort(
    client: httpx.AsyncClient, session_id: str,
) -> None:
    """Best-effort abort of one opencode session. Never raises."""
    try:
        await client.post(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/abort",
            timeout=10.0,
        )
    except (httpx.HTTPError, OSError):
        pass
```

### 3. `opencode_chat()` (:537-600) — deadline loop + cleanup

Signature: add keyword-only `autonomous: bool = False`. Body changes between session creation (:565) and text extraction:

```python
post_task = asyncio.create_task(
    client.post(
        f"{OPENCODE_SERVE_URL}/session/{session_id}/message",
        json=payload, timeout=timeout,
    )
)
try:
    last_poll = time.monotonic()
    while True:
        if post_task.done():
            resp = post_task.result()      # raises httpx errors here
            break
        if time.monotonic() - last_poll >= _BLOCKING_PERMISSION_POLL_S:
            last_poll = time.monotonic()
            perm = await _detect_pending_permission(client, session_id)
            if perm is not None:
                question = await _handle_permission_event(
                    client, session_id, perm, {}, autonomous=autonomous,
                )
                if question:
                    # Headless blocking call: no user can answer a WRITE
                    # ask; abort and surface a clear error instead of
                    # burning the timeout or granting unprompted.
                    await _abort_session_best_effort(client, session_id)
                    return (
                        "[OpenCode Bridge Error: agent requested write "
                        "permission — headless escalation cannot relay "
                        "questions; session aborted.]"
                    )
        await asyncio.sleep(min(0.25, _BLOCKING_PERMISSION_POLL_S))
except (httpx.HTTPError, OSError) as exc:
    await _abort_session_best_effort(client, session_id)
    return f"[OpenCode Bridge Network Error: {str(exc)}]"
finally:
    if not post_task.done():
        post_task.cancel()
```

Then the unchanged status/text handling: `resp.status_code != 200` → **add** `await _abort_session_best_effort(client, session_id)` before returning `[OpenCode Bridge Error: message HTTP ...]`; extraction/empty-response unchanged. Session-create errors (:563-567) keep NO abort (no id available; the serve-side zombie is handled by the existing `_abort_zombie_sessions` sweep at :1625).

Exception coverage: `httpx.ReadTimeout`/`TimeoutException`/`ConnectError` are all `httpx.HTTPError` subclasses — the existing catch tuple covers the message-phase timeout, and the abort runs before the error string is returned.

### 4. `opencode_escalation()` (:1087) — docstring note

Document that the headless queue-worker path runs with `autonomous=False`: READ auto-allowed, WRITE aborts; no unprompted grants (REQ-6). No behavioral code change.

### 5. `tests/test_opencode_bridge.py` — `_FakeClient` scripting

- `raise_timeout_on == "post"` branch in `post()`: `raise httpx.ReadTimeout("read timed out")` (mirrors :85-86).
- `permission_records: list[dict[str, Any]] = []` attribute; in `get()`: `if url.endswith("/permission"): return _FakeResp(200, self.permission_records)`.
- Abort observability: existing `post_calls` records every URL — assert `any(u.endswith("/abort") for u, _ in client.post_calls)`.

Record shapes that classify deterministically through `_handle_permission_event` + `_classify_permission_access` (:230):

- READ: `{"id": "perm_1", "sessionID": "ses_0001", "permission": "external_directory", "patterns": ["cat /etc/os-release"], "tool": {"messageID": "m1", "callID": "c1"}}` → not a write-tool type, cmd heuristic on `cat /etc/os-release` → "read" → POST "always", returns None.
- WRITE: `{"id": "perm_2", "sessionID": "ses_0001", "permission": "write", "patterns": ["rm -rf /x"], "tool": {"messageID": "m1", "callID": "c1"}}` → `_WRITE_TOOL_TYPES` ("write") → "write" → question string.
- The tool `messageID` triggers `GET /session/{id}/message/{mid}` in `_handle_permission_event`; `_FakeClient.get` already returns `{"info": {}, "parts": []}` for `/message/` URLs (:87-89) → tool_name stays `""`, cmd falls back to `patterns[0]`. Deterministic.

### 6. New `TestOpenCodeChatHardening` (6 tests, all hermetic, monkeypatch `_BLOCKING_PERMISSION_POLL_S` → 0.01)

1. `test_timeout_aborts_session` — `raise_timeout_on = "post"`; `await opencode_chat("hi", timeout=10.0)` → return string starts `[OpenCode Bridge Network Error:`; `any("/abort" in u ...)` True.
2. `test_message_503_aborts_session` — `message_status = 503` → `[OpenCode Bridge Error: message HTTP 503]`; abort recorded.
3. `test_read_permission_auto_allowed` — `permission_records = [READ record]`; result == extracted text; `post_calls` contains `.../permissions/perm_1` with `{"response": "always"}`; no abort.
4. `test_write_permission_aborts` — `permission_records = [WRITE record]`; result contains "write permission" and `[OpenCode Bridge Error`; abort recorded.
5. `test_autonomous_write_auto_allowed` — same WRITE record, `autonomous=True`; result == extracted text; permission response "always" posted; no abort.
6. `test_success_no_permissions_unchanged` — no records; result identical to existing success test; no abort, no permission POST.

## Verification Plan

1. `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` — 121+ existing + 6 new green (hermetic; no live serve, no real /proc).
2. `.venv/bin/python -m pytest` — full suite (~412 tests) green.
3. `git status`/`git diff --stat` — only `opencode_bridge.py`, `tests/test_opencode_bridge.py`, plus OpenSpec artifacts for this change; `opencode-serve-config.opencode.jsonc` untouched.
4. Commit: `fix(bridge): harden the blocking escalation path with permission polling and session cleanup`.

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Poll loop adds latency to plain runs | Low | One extra HTTP GET per 5s max; loop exits on POST completion; success path byte-for-byte unchanged |
| Abort kills a session that would have completed | Low | Abort fires only on failure exits (timeout/HTTP/network) and on write-permission aborts — never on success |
| WRITE abort degrades escalation capability | Med | Conservative by design (no unprompted grants); the interactive `/opencode` stream path relays questions properly; documented in escalation docstring |
| Permission classification drift vs streaming path | Low | Same helpers (`_detect_pending_permission`, `_handle_permission_event`, `_classify_permission_access`) used verbatim |
| Cancellation leaks the POST task | Low | `finally` cancels a still-running task |

## Rollback

`git revert` the single commit — function-level edits confined to `opencode_bridge.py` + tests; prior blocking semantics restored exactly; no config/DB/dependency/schema impact.
