# OpenCode Serve-Lifecycle Specification

## Purpose

The OpenCode bridge MUST make serve recycling recoverable within the initiating request, discard demonstrably stale session pins, protect unrelated reused processes, and give blocking calls one bounded recovery attempt after a serve transport failure.

## Requirements

### REQ-1: Stream recycle precedes ensure

`opencode_chat_stream` MUST run `_recycle_serve_if_low_memory()` before `ensure_opencode_serve()` on the non-autonomous path. If recycling stops the serve, the same request's ensure MUST respawn it before any session request. The autonomous force-recycle-before-ensure behavior MUST remain intact.

#### Scenario-1: Recycle requires in-line respawn

- GIVEN a non-autonomous stream request whose serve meets a recycle condition
- WHEN stream startup runs
- THEN recycling completes before ensure
- AND ensure respawns the serve before the request creates or resumes a session

#### Scenario-2: No recycle is needed

- GIVEN a healthy serve that does not meet a recycle condition
- WHEN a stream request starts
- THEN ensure runs after the recycle check
- AND normal session processing continues unchanged

### REQ-2: Stale pinned sessions self-heal

When resuming a pinned session, a successful `/session/status` fetch that lacks the pinned session ID MUST cause the bridge to remove that pin and create a fresh session. A transport-error status fetch MUST NOT cause the pin to be removed.

#### Scenario-1: Successful status omits the pin

- GIVEN a session key is pinned to a session ID
- WHEN `/session/status` succeeds without that ID
- THEN the stale pin is removed
- AND the request creates and uses a fresh session

#### Scenario-2: Status transport fails

- GIVEN a session key is pinned to a session ID
- WHEN the status fetch raises a network-class error
- THEN the pin is retained conservatively
- AND no fresh session is created solely because of that fetch failure

### REQ-3: Kill primitives verify the selected PID

Immediately before calling `os.kill`, both `_recycle_serve_if_low_memory` and `_force_recycle_serve` MUST re-read `/proc/<pid>/cmdline` and confirm that the selected PID still matches the OpenCode serve process using the same serve-binary matching rules as PID discovery. An unreadable or mismatching cmdline MUST be treated as a failed verification, and the kill MUST be skipped.

#### Scenario-1: PID still belongs to the serve

- GIVEN either kill primitive selects a PID whose current cmdline matches the serve
- WHEN the primitive verifies the PID
- THEN it sends the termination signal to that PID

#### Scenario-2: PID was reused or cannot be verified

- GIVEN either kill primitive selects a PID whose cmdline now mismatches or is unreadable
- WHEN the primitive performs final verification
- THEN it MUST NOT call `os.kill` for that PID

### REQ-4: Blocking calls receive one bounded respawn retry

After an initial successful ensure, `opencode_chat` MUST handle a network-class failure (`httpx.HTTPError`, `OSError`, or `ValueError`) by force-recycling the serve, re-ensuring it, and retrying the complete blocking call exactly once. The retried call MUST preserve the existing session creation, permission polling, cleanup, and result semantics. A failed re-ensure or failed retry MUST return the applicable bridge error string without another recycle or retry. Non-success HTTP responses MUST return their existing HTTP error strings and MUST NOT trigger this recovery.

#### Scenario-1: Retry succeeds after a network failure

- GIVEN initial ensure succeeds and the first blocking attempt raises a network-class error
- WHEN force-recycle and re-ensure succeed
- THEN one fresh blocking attempt runs and its successful result is returned

#### Scenario-2: Retry also fails

- GIVEN recovery was triggered after the first network-class failure
- WHEN re-ensure fails or the single retry fails
- THEN an applicable bridge error string is returned
- AND no second recovery cycle or third attempt occurs

#### Scenario-3: HTTP status failure is not retried

- GIVEN a blocking session or message request returns a non-success HTTP status
- WHEN `opencode_chat` handles the response
- THEN it returns the existing HTTP error string
- AND it does not force-recycle, re-ensure, or retry

### REQ-5: Recycle kills drain the dying listener

After a verified SIGTERM kill, both `_recycle_serve_if_low_memory` and `_force_recycle_serve` MUST poll `is_opencode_serve_running()` up to 4 × 0.25s with an early break when the serve no longer reports running. If the budget is exhausted while the serve still reports running, the helper MUST behave as before without raising or forcing any further wait. When the kill is skipped because REQ-3 verification fails, no drain MUST run.

#### Scenario-1: Serve stops within the drain budget

- GIVEN either recycle helper sends a verified SIGTERM kill
- WHEN the serve reports down before all four probes complete
- THEN the drain stops probing immediately
- AND subsequent ensure can observe the stopped listener and respawn the serve

#### Scenario-2: Serve remains up or the kill is skipped

- GIVEN the serve remains running through all four probes or REQ-3 verification skips the kill
- WHEN the recycle helper completes
- THEN budget exhaustion returns without raising or forcing an additional wait
- AND a skipped kill performs no drain probes
