# Proposal: Bridge Cycle 7 — Resumed-Session Polling Replay + Serve-PID Port-Form Matching

**One-line summary**: Stop the polling fallback (`_poll_session_deltas`) from replaying a resumed pinned session's full history (old text/tool/reasoning/question parts re-yielded as fresh deltas — including a stale "question" that kills the stream) by seeding per-call part state once at stream start for resumed sessions, and harden `_find_serve_pid` to match `--port`/`--port=` exactly instead of substring-prefix matching. ~180 lines incl. tests, single PR.

## Intent (Why)

- **Replay of history on resume + polling**: per-call state (`text_lens`/`tool_state`/`user_mids`, seeded empty at `:849-852`) is only fed by live events. When a PINNED session is resumed (`:789-821`) and the `/event` SSE bus closes mid-run (`StopAsyncIteration`, `:912`), the polling fallback (`:1535`) re-scans the session's FULL message list: every old assistant text part re-yields its full text (`:1260-1265`), every old tool part re-yields "✅/⚠️ done/failed" chunks (tool branch `:1411-1432`, keyed by empty `tool_state`), old reasoning re-emits "🧠 thinking…" (`:1266-1276`), and a previously resolved QUESTION part (tool == "question", `:1279-1410`) re-yields a "question" delta which the polling caller treats as the agent asking → it stops the stream (`:954-956`) and routes.py shows the STALE question while the agent's real answer to the posted answer never streams. The event-bus path is immune (live events only, no replay — `:855-858`).
- **Serve PID matcher**: `_find_serve_pid` (`:1741`) only matches the space form `f"--port {port}"` (`:1756`). Proxy-spawned serves use the space form (`:592-594`) so they match, but a serve started with `--port={port}` (equals form) is never found → `_serve_start_epoch_ms` (`:1488`), `_recycle_serve_if_low_memory` (`:1571`), `_force_recycle_serve` (`:1602`), `_serve_health` (`:1688`) all silently no-op. The substring match also false-positives on digit-prefix ports ("--port 18999" matches "--port 189990").

**Outcome**: a resumed multi-turn conversation streams only NEW deltas after an event-bus drop (no history replay, no stale-question stream kill), and serve discovery matches the exact `--port` token in either form with no prefix false-positives.

## Scope

### In Scope

- `opencode_bridge.py` only:
  - **Fix 1**: for RESUMED (pinned) sessions, one best-effort `GET /session/{id}/message` at stream start (before `prompt_async`): seed `text_lens` with each existing part's current text length, `tool_state` with its tool state, `user_mids` with user-message ids, and collect question-part pids into a new seen set. Thread the seen set through `_poll_session_deltas` and `_yield_part_deltas` (both call sites: polling `:951-953`, event-bus `:1196-1198`); the question branch short-circuits when pid ∈ seen. Fresh sessions seed nothing (empty set) → zero behavior change. Blocking path `opencode_chat` untouched.
  - **Fix 2**: `_find_serve_pid` — tokenize the NUL-normalized cmdline into argv tokens; match exact pair `("--port", port)` or single token `"--port={port}"`; exact equality only, no substring prefix matching.
- `tests/test_opencode_bridge.py`: additive hermetic tests using the existing `_FakeClient`/`_PollClient` harness (see Test Approach).

### Out of Scope

- `opencode_chat` blocking path, routes.py, proxy.py, config, systemd.
- Event-bus behavior, wedge/timeout detection, session abort policy (cycle 6), recycle/respawn internals.
- The `_serve_config_mtime` F5 carve-out (already documented; not extended).

## Exploration Summary

Verified against source (orchestrator gatekeeper + propose phase, 2026-08-09):

- Per-call state seeded empty at `:849-852`; polling fallback re-scans full message list (`:1535-1565`) and yields question deltas that stop the caller (`:954-956`, `:1560-1562`). Event-bus path uses the same `_yield_part_deltas` but only sees live parts (`:1196-1198`), so it is replay-immune.
- Pin resume path at `:789-821`; event bus opened before prompt (`:855-858`), so seeding must happen at/after pin acquisition but before `prompt_async`.
- `_find_serve_pid` `:1741-1762`: NUL-normalized cmdline, space-form-only substring match at `:1756`; spawn uses space form (`:592-594`).
- Hermetic harness: `_FakeClient` `:56`, `_PollClient` `:1151-1162` (scripts the message-list GET branch via `poll_messages`); existing NUL-separated test `tests/test_opencode_bridge.py:1997-2042` must stay green.

**Persistence note**: Engram persistence is unavailable in this runtime; the durable store is this OpenSpec change folder (filesystem).

## Assumptions & Edge Cases

- Seeding is best-effort: if the pre-prompt GET fails (non-200/network error) or returns nothing, degrade to current behavior — replay may occur but the stream must NOT raise.
- The seen-question set only suppresses question deltas for parts that existed BEFORE this request's prompt; a question asked after the prompt (new part) still yields and stops the stream as today.
- Seeding also fixes the "✅ done" tool spam and text/reasoning replay for resumed sessions; fresh sessions see zero change (empty seed).
- `_find_serve_pid`: existing NUL-separated space-form test stays green; equals-form matches; digit-prefix ("--port 189990" vs search "18999") does NOT match; malformed cmdlines keep the existing try/except resilience.

## Capabilities

Prior specs: `openspec/specs/opencode-bridge-blocking-path/` (cycle 5), `openspec/specs/opencode-bridge-stream-exit-hygiene/` (cycle 6). Neither covers polling-replay prevention or serve-PID matching; both are new.

### New Capabilities

- `opencode-bridge-polling-replay-prevention`: resumed-session part-state seeding at stream start (text lengths, tool state, user-message ids, seen-question set) so the polling fallback never replays history or re-surfaces a stale question.
- `opencode-bridge-serve-pid-matching`: exact argv-token `--port`/`--port=` matching in `_find_serve_pid` (space and equals forms, no digit-prefix false positives).

### Modified Capabilities

None.

## Approach

1. Fix 1: after pin acquisition in `opencode_chat_stream` (resumed branch `:789-821`), best-effort GET the session's messages; seed `text_lens` (len of each existing assistant text part), `tool_state` (each tool part's `state`), `user_mids` (user part messageIDs), and a new `seen_question_pids: set[str]`. Pass the set into `_poll_session_deltas` → `_yield_part_deltas`; question branch returns None when pid is seen. Fresh sessions: empty seed, no GET (or GET only on resume).
2. Fix 2: rewrite the `_find_serve_pid` match: split NUL-normalized cmdline on whitespace into tokens; match if `"--port"` and `port` are adjacent tokens, or a token equals `f"--port={port}"`.
3. Additive tests; run `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q`.

## Test Approach

New hermetic tests (no live serve):

1. Resumed pinned session + empty `stream_lines` (forces immediate `StopAsyncIteration`) + scripted `poll_messages` containing old assistant text/tool/question parts → stream yields NO old text replay, NO "✅ done"/"⚠️ failed" spam, NO stale-question stop; a scripted new-turn delta still streams.
2. Resumed session where the seeding GET fails (non-200) → degrades to current behavior without raising (replay allowed, no exception).
3. `_find_serve_pid`: equals-form `--port=18999` matches; `--port 189990` does NOT match search for `18999`; existing NUL space-form test stays green.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `opencode_bridge.py` | Modified | Fix 1: seeding GET + `seen_question_pids` threaded through `_poll_session_deltas` (`:1535`) and `_yield_part_deltas` (`:1238`), both call sites (`:951-953`, `:1196-1198`), question-branch short-circuit (`:1279-1410`). Fix 2: `_find_serve_pid` token matching (`:1741-1762`). |
| `tests/test_opencode_bridge.py` | Modified | New `_PollClient`-based replay tests + `_find_serve_pid` equals-form/prefix-guard cases; existing NUL test (`:1997`) untouched. |

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~180 (150–220) |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | Single PR |
| Delivery strategy | single-pr |

Decision needed before apply: No
Chained PRs recommended: No
400-line budget risk: Low
