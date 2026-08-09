# OpenCode Bridge Blocking-Path Specification

## Purpose

The blocking cloud-escalation path (`opencode_chat`, used only by the queue-worker escalation tier via `opencode_escalation`) SHALL stop leaking sessions and burning the full 600s timeout when the agent parks on a permission gate. It gains the same permission hygiene as the streaming path (READ auto-allow, WRITE never granted unprompted on the headless worker path) and aborts its session on every non-success exit. The plain success path is byte-for-byte unchanged.

## Requirements

### REQ-1: Permission-aware deadline loop

While the blocking message POST is in flight, `opencode_chat` SHALL poll `_detect_pending_permission(client, session_id)` every `_BLOCKING_PERMISSION_POLL_S` seconds (new module constant, default 5.0; poll sleep capped at `min(0.25, poll_s)`). The message POST SHALL run as an `asyncio.create_task` so polling and the POST progress concurrently. Every detected relayed permission record SHALL be handled through the single shared `_handle_permission_event(client, session_id, perm, {}, autonomous=autonomous)`.

#### Scenario-1: Agent parks on a permission while the POST is in flight

- GIVEN a blocking `opencode_chat` call whose message POST does not finish immediately
- WHEN `GET /permission` returns a relayed-permission record for the session
- THEN the record is handled via `_handle_permission_event` with the call's `autonomous` value
- AND the polling loop continues unless the handler aborts

#### Scenario-2: No permission ever appears

- GIVEN a blocking call with no pending permissions
- WHEN the POST completes
- THEN the loop exits with no extra HTTP calls beyond the poll cadence
- AND the result is the extracted assistant text

### REQ-2: READ permissions are auto-allowed

A permission classified "read" by the shared `_classify_permission_access` SHALL be auto-allowed (POST "always" via the shared handler) and the POST SHALL be allowed to complete normally.

#### Scenario-1: READ record appears mid-flight

- GIVEN a pending permission record classified as read
- WHEN the poller handles it
- THEN the permission response "always" is posted
- AND `opencode_chat` returns the completed message text

### REQ-3: WRITE permissions are never granted unprompted on the headless path

When `_handle_permission_event` returns a question string (interactive-mode write/git ask — no user is listening on the queue-worker path), `opencode_chat` SHALL abort the session (best-effort `POST /session/{id}/abort`) and return a clear error string naming the write-permission abort. It SHALL NOT relay the question and SHALL NOT auto-grant the write.

#### Scenario-1: WRITE record appears mid-flight, interactive mode

- GIVEN a pending permission record classified as write with `autonomous=False`
- WHEN the poller handles it
- THEN the session is aborted via POST /session/{id}/abort
- AND the returned string is an `[OpenCode Bridge Error: ...]` message naming the write-permission abort

#### Scenario-2: WRITE record with autonomous=True

- GIVEN a pending permission record with `autonomous=True`
- WHEN the poller handles it
- THEN the shared handler auto-allows it (POST "always", existing semantics)
- AND the POST completes normally

### REQ-4: autonomous parameter

`opencode_chat` SHALL gain `autonomous: bool = False` as a keyword-only parameter. `autonomous=True` delegates every relayed ask to the existing auto-allow-all branch of `_handle_permission_event`. Default `False` keeps the conservative abort behavior of REQ-3.

#### Scenario-1: Signature compatibility

- GIVEN existing callers pass the original positional/keyword arguments
- WHEN `opencode_chat` is invoked
- THEN behavior is unchanged and `autonomous` defaults to False

### REQ-5: Session cleanup on every non-success exit

`opencode_chat` SHALL best-effort abort the created session on: message HTTP != 200, httpx/OSError/ValueError during the message phase (including timeout), and write-permission abort (REQ-3). A private helper `_abort_session_best_effort(client, session_id)` SHALL implement the abort (never raises; catches `(httpx.HTTPError, OSError)`). Success paths SHALL NOT abort. The pre-message session-create failures (session HTTP != 200) have no session id and SHALL NOT abort.

#### Scenario-1: Message POST times out

- GIVEN the message POST raises `httpx.ReadTimeout`
- WHEN the error is caught
- THEN `POST /session/{id}/abort` is issued best-effort
- AND the returned string is an `[OpenCode Bridge Network Error: ...]` message

#### Scenario-2: Message HTTP 503

- GIVEN the message POST returns HTTP 503
- WHEN the error branch runs
- THEN `POST /session/{id}/abort` is issued best-effort
- AND the returned string is `[OpenCode Bridge Error: message HTTP 503]`

#### Scenario-3: Success path

- GIVEN the message POST returns 200 with text parts
- WHEN the text is extracted
- THEN no abort is issued
- AND the extracted text is returned unchanged

### REQ-6: Escalation stays conservative

`opencode_escalation` SHALL keep `autonomous=False` (no unprompted grants on the headless queue-worker path) and SHALL document this in its docstring.

#### Scenario-1: Escalation never auto-grants writes

- GIVEN a queue worker escalates a job through `opencode_escalation`
- WHEN the agent requests a write permission
- THEN the session is aborted and the job completes with the error string
- AND no permission is granted

### REQ-7: Regression coverage

New code paths SHALL have hermetic regression tests in `tests/test_opencode_bridge.py` extending `_FakeClient` with: (a) `raise_timeout_on == "post"` raising `httpx.ReadTimeout`; (b) a `GET /permission` route returning scripted records `[{id, sessionID, permission, patterns, tool}]`; (c) abort observability through the recorded `post_calls`. Scenarios: timeout → abort + network error; message 503 → abort; READ auto-allowed and POST completes; WRITE → abort + error naming permission; autonomous=True write → auto-allowed and completes; success path unchanged. Existing `TestOpenCodeChat` tests SHALL pass unchanged.

#### Scenario-1: Suite green

- GIVEN the implemented changes
- WHEN `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` runs
- THEN all existing and new tests pass without a live serve or real /proc

## Notes

- Capability directory: `openspec/specs/opencode-bridge-blocking-path/`. No prior spec covered the blocking path; the streaming-path specs are unaffected.
- The abort helper duplicates no streaming-path logic; the streaming path keeps its inline aborts as-is.
- `opencode-serve-config.opencode.jsonc` is never touched by this change.
