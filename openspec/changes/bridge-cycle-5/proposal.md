# Proposal: Bridge Cycle 5 — Permission-Aware Hardening of the Blocking Escalation Path

**One-line summary**: Harden `opencode_chat` (`opencode_bridge.py` :537-600), the blocking cloud-escalation path, with a permission-aware deadline loop, session abort on every non-success exit, and hermetic tests. ~200 lines, single PR; `autonomous=False` stays on the queue-worker path.

## Intent (Why)

The queue-worker escalation path (cloud fallback when local tiers are exhausted) leaks sessions and wedges the serve for the full 600s timeout:

- **No abort on any exit**: message HTTP != 200, `httpx.ReadTimeout` → HTTPError, missing id, network error — every path returns an error string with the session left alive. An agent parked on an unanswered permission gate wedges the tool "running" for the full `OPENCODE_SERVE_TIMEOUT`, blocking later tasks on the serve's tool runner.
- **No permission handling**: `opencode_chat` never consults GET /permission, so read tools that should auto-allow instead burn the timeout.

**Outcome**: no path leaves a session alive; READ permissions auto-allow; WRITE/git permissions fail fast with a clear error instead of wedging.

## Scope

### In Scope

- Permission-aware deadline loop in `opencode_chat`: message POST as `asyncio.create_task`; poll `_detect_pending_permission` every `_BLOCKING_PERMISSION_POLL_S` (new constant, 5.0s default; sleep `min(0.25, poll_s)`); READ → `_handle_permission_event` auto-allow; WRITE/git interactive → abort + clear error string; `autonomous=True` → auto-allow-all (existing semantics). New `autonomous: bool = False` parameter.
- Best-effort `_abort_session_best_effort(client, session_id)` (never raises) on message HTTP != 200, timeout/network error, and write-permission abort; streaming-path inline aborts stay as-is.
- `opencode_escalation` keeps conservative `autonomous=False` — documented in docstring.
- Tests: `_FakeClient` gains post-timeout raise (`raise_timeout_on == "post"`), GET /permission scripted route, abort recording via `post_calls`; new `TestOpenCodeChatHardening` (6 scenarios).

### Out of Scope

- Streaming path (`opencode_chat_stream`), routes.py, proxy.py, `_abort_zombie_sessions` refactor.
- `opencode-serve-config.opencode.jsonc` — never touch; docs.

## Exploration Summary

Every non-success exit in `opencode_chat` (:563-599) returns an error string with the session still live. Permission machinery is fully reusable — `_detect_pending_permission` (~:369), `_handle_permission_event` (~:298), `_post_permission_response` — and the abort endpoint (`POST /session/{id}/abort`, used at :680/:761/:810/:946/:991) exists. `asyncio.create_task` is unused in the module. Harness is hermetic (`_FakeClient`/`_FakeResp`); `TestOpenCodeChat` covers success/session-500/message-503/connect-error/empty.

## Assumptions & Edge Cases

- READ auto-allow mirrors streaming semantics; writes never auto-granted unprompted on the headless worker path.
- Poll task cancelled cleanly when POST finishes first (`CancelledError` propagates; no orphan task).
- Abort is best-effort: failure logs, never changes the returned error string.
- Existing `TestOpenCodeChat` success cases stay green; success path adds only polling overhead ≤ poll interval.

## Capabilities

`openspec/specs/` researched — no runtime spec covers the bridge (bridge-docs spec under `openspec/changes/bridge-docs/specs/bridge-docs/` is documentation-only, REQ-DOC-*).

### New Capabilities

- `opencode-bridge-blocking-path`: permission-aware deadline loop for `opencode_chat` (poll cadence, READ auto-allow, WRITE/git abort, `autonomous` semantics) and session abort on every non-success exit.

### Modified Capabilities

None.

## Approach

One function rewrite in `opencode_bridge.py` (~60 lines: create_task deadline loop, permission polling, abort on all failure exits) + `_abort_session_best_effort` (~10) + `_BLOCKING_PERMISSION_POLL_S` + escalation docstring; ~130 test lines. No routes.py/proxy.py/constants/config changes.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `opencode_bridge.py` | Modified | `opencode_chat` (:537-600): create_task loop, permission poll, abort on every non-success exit; new `_abort_session_best_effort`, `_BLOCKING_PERMISSION_POLL_S`; escalation docstring. |
| `tests/test_opencode_bridge.py` | Modified | `_FakeClient` scripting additions; `TestOpenCodeChatHardening` 6 tests. |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Poll loop adds latency to plain runs | Low | Task cancelled when POST completes; success path unchanged |
| Abort kills a session that would have completed | Low | Abort fires only on failure exits and write-permission aborts |
| Permission logic drifts from streaming path | Low | Reuses existing helpers verbatim; no new relay logic |

## Rollback Plan

`git revert` the single commit — function-level edits confined to `opencode_bridge.py` + tests; prior semantics restored exactly; no config/DB/dependency impact.

## Dependencies

None.

## Success Criteria

- [ ] Timeout/network error and message HTTP 503 → abort POST recorded; clear error string returned.
- [ ] READ permission → auto-allowed, POST completes. WRITE (interactive) → abort + explicit error; `autonomous=True` write → auto-allowed.
- [ ] No failure exit leaves a session alive on the serve.
- [ ] `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` green; diff touches only `opencode_bridge.py` + tests.

## Delivery Shape

| Forecast item | Value |
|---------------|-------|
| Estimated changed lines | ~200 |
| 400-line review budget risk | Low |
| Chained PRs recommended | No |
| Delivery strategy | single-pr |
| Decision needed before apply | No |

Single PR. Suggested commit: `fix(bridge): harden the blocking escalation path`.
