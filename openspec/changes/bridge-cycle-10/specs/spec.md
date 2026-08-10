# Specification: Bridge Cycle 10 — Blocking-Path Bounded Respawn, Stale-Pin Self-Heal, PID-Reuse Guards

**Change:** `bridge-cycle-10`  
**Summary:** Adopt and land three preserved OpenCode serve-lifecycle fixes.  
**Status:** Adopted (from Draft)  
**Proposed:** 2026-08-09  
**Adopted:** 2026-08-10

## Delta requirements

### A1: Blocking calls receive one bounded respawn

One `opencode_chat` call MUST make at most two attempts. Each MUST create a fresh session and return an error string rather than raise. Retry MUST occur exactly once only when the first attempt reports `httpx.HTTPError`, `OSError`, or `ValueError`, including message-POST transport failure during permission polling. Between attempts, the bridge MUST call `_force_recycle_serve("blocking-path respawn")` and re-ensure. HTTP-status errors, missing session IDs, empty responses, and permission aborts MUST NOT retry; failed re-ensure or second-attempt failure MUST terminate recovery.

#### Scenario-1: Network failure recovers once

- GIVEN initial ensure succeeds and the first fresh-session attempt has a network-class failure
- WHEN recycle and re-ensure succeed
- THEN exactly one fresh-session retry runs and its result is returned without raising

#### Scenario-2: Recovery remains bounded

- GIVEN recovery is triggered
- WHEN re-ensure fails or the second attempt fails
- THEN an applicable bridge error string is returned
- AND no third attempt or second recycle occurs

#### Scenario-3: Semantic failures do not retry

- GIVEN an attempt has an HTTP-status error, missing ID, empty response, or permission abort
- WHEN the attempt returns its bridge error
- THEN no recycle, re-ensure, or retry occurs

### B1: Stale pinned sessions self-heal

After a successful `/session/status` fetch, if its map lacks the pinned session ID, the bridge MUST remove that ID from `pending_permissions`, remove the conversation pin from `session_map`, and start a fresh session. A transport-error status fetch MUST retain the pin.

#### Scenario-1: Successful status omits the pin

- GIVEN a conversation has a pinned session
- WHEN a successful status map does not contain its ID
- THEN both associated state entries are removed
- AND the request creates and uses a fresh session

#### Scenario-2: Status transport fails

- GIVEN a conversation has a pinned session
- WHEN the status fetch fails in transport
- THEN the pin is retained and no fresh session is created solely for that failure

### C1: Recycle kills reject PID reuse

Immediately before either recycle helper calls `os.kill`, it MUST re-read `/proc/<pid>/cmdline` through `_pid_is_serve` and shared `_cmdline_matches_serve`. The matcher MUST normalize NUL separators, require `opencode` and `serve` tokens, and accept only exact `--port=<port>` or adjacent `--port`, `<port>` tokens; prefix matches MUST be rejected. `_pid_is_serve` MUST return `False` on `OSError` and MUST NOT raise. A mismatch MUST warn, skip kill and drain, leave the serve absent, and allow the next ensure to respawn it.

#### Scenario-1: Verified serve is terminated

- GIVEN the selected PID still has a matching serve cmdline and exact port
- WHEN either recycle helper performs final verification
- THEN it sends SIGTERM and runs the existing bounded drain

#### Scenario-2: Reused or unreadable PID is protected

- GIVEN the cmdline mismatches, has only a port prefix match, or cannot be read
- WHEN final verification runs
- THEN it returns false without raising, warns, and does not kill or drain
- AND the next ensure may spawn a fresh serve

## Testing

Acceptance MUST retain 11 new hermetic tests: four blocking-respawn, two stale-pin, four recycle/PID-guard, and one cmdline unit test. Existing drain and stability tests MUST patch `_pid_is_serve` affirmatively where kill flow is intended. `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` MUST remain **164 passed**, and `.venv/bin/python -m pytest tests/ -q` MUST remain green without contacting or killing a live serve.

## Non-requirements

- No changes to `routes.py`, `proxy.py`, configuration, systemd, event-bus behavior, or wedge/timeout policy.
- No change to R1 glass-pipe behavior: client content, tools, and sampling parameters remain untouched.

## Reference

- Parent capability: [`opencode-serve-lifecycle`](../../../specs/opencode-serve-lifecycle/spec.md) (REQ-2, REQ-3, REQ-4)
- Change artifacts: [proposal](../proposal.md), [exploration](../explore.md), [initial context](../init-context.md)
