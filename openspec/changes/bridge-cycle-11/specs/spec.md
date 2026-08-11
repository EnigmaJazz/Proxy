# Specification: Bridge Cycle 11 — Blocking-Path Reliability Trio

**Change:** `bridge-cycle-11`  
**Parent capability:** `opencode-serve-lifecycle`  
**Status:** Draft

## Context

The blocking `_opencode_chat_attempt` path MUST gain connect-only recovery, poll-cadence wedge handling, and complete error-path session cleanup. The change applies only to blocking calls; it preserves the existing single bounded retry and prevents one stalled session from recycling the shared serve.

## Requirements

### REQ-4: Blocking calls receive one bounded connect-failure respawn retry (MODIFIED)

After an initial successful ensure, `opencode_chat` MUST force-recycle, re-ensure, and retry exactly once only when the first attempt fails with `httpx.ConnectError`, `httpx.ConnectTimeout`, `httpx.RemoteProtocolError`, or `OSError`. `httpx.ReadTimeout`, every other `httpx.HTTPError`, `ValueError`, HTTP-status errors, permission aborts, missing IDs, and empty responses MUST NOT recycle or retry. A failed re-ensure or retry MUST terminate recovery without a third attempt.

(Previously: all `httpx.HTTPError`, `OSError`, and `ValueError` paths could request the bounded respawn.)

#### Acceptance criteria

- GIVEN initial ensure succeeds and the first attempt has a connect-class failure
- WHEN recycle and re-ensure succeed
- THEN the failed session is aborted and exactly one fresh attempt runs
- AND no second recycle or third attempt can occur

- GIVEN a created session encounters `ReadTimeout`, another non-connect `HTTPError`, or `ValueError`
- WHEN the attempt returns its bridge error
- THEN the session is aborted and the failure is classified as non-recyclable
- AND no recycle, re-ensure, or retry occurs

- GIVEN a blocking request returns a semantic or HTTP-status failure
- WHEN `opencode_chat` handles it
- THEN its existing error semantics remain and no recovery cycle starts

### REQ-6: Blocking calls detect wedged tools at poll cadence (ADDED)

While the message POST is pending, the blocking path MUST check `_detect_wedged_tool` at each permission-poll cadence, after pending-permission detection and handling. A tool running without output beyond `_TOOL_WEDGE_AFTER_S` MUST cause `_abort_session_best_effort` and a non-recyclable wedge error. Existing stale-part, task-threshold, and replay carve-outs MUST remain unchanged.

#### Acceptance criteria

- GIVEN the POST remains pending and the detector reports a wedge
- WHEN a poll cycle completes its permission check first
- THEN the session is aborted and a clear wedge error is returned
- AND the serve is never recycled or retried

- GIVEN a parked write permission and wedge-like message state coexist
- WHEN the poll cycle evaluates them
- THEN permission handling wins and returns the existing permission error
- AND wedge handling does not supersede that result

### REQ-7: Every created-session error exit aborts best-effort (ADDED)

Every non-success exit from `_opencode_chat_attempt` after obtaining a session ID MUST invoke `_abort_session_best_effort`, including malformed `resp.json()` data. Abort failure MUST NOT raise or replace the original bridge error. A successful non-empty response MUST NOT be aborted.

#### Acceptance criteria

- GIVEN a message POST returns HTTP 200 with a body whose `json()` raises `ValueError`
- WHEN the attempt parses the response
- THEN it aborts the session and returns the original non-recyclable bridge error
- AND no serve recycle or retry occurs

- GIVEN any other post-creation non-success or a successful non-empty response
- WHEN the attempt exits
- THEN the former is aborted best-effort and the latter is not aborted

## Verification plan

Add 4–5 focused tests to `TestOpenCodeChatHardening` in `tests/test_opencode_bridge.py`, using scripted fake clients/mock HTTPX behavior and monkeypatched ensure/recycle/detector functions. Tests MUST make no real network request and MUST remain protected by the autouse `hermetic_serve` guard.

```bash
.venv/bin/python -m pytest tests/test_opencode_bridge.py -q
.venv/bin/python -m pytest tests/ -q
```

## Out of scope and delivery limits

- No changes to `routes.py`, `proxy.py`, the streaming path, `scripts/`, constants, or configuration.
- No live-serve tests and no weakening or bypassing of `hermetic_serve`.
- One PR, approximately 30 bridge lines plus 170 test lines; total MUST remain under 400 changed lines. No `size:exception`.
