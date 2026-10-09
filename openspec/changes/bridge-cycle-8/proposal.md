# Proposal: Bridge Cycle 8 — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills

**One-line summary**: Land the two unapplied cycle-7 fixes — (1) seed per-call part state at stream start for resumed pinned sessions so the polling fallback never replays history or re-surfaces a stale "question" that kills the stream, (2) make `_find_serve_pid` match `--port`/`--port=` exactly instead of substring-prefix matching — plus a new (3) bounded listener drain after recycle kills so `ensure_opencode_serve` reliably respawns instead of POSTing into a dying listener. ~268 lines incl. tests, single PR.

## Intent (Why)

- **Fix 1 — resumed-session polling replay (unapplied cycle-7 work)**: per-call state (`user_mids`/`text_lens`/`tool_state`, seeded empty at `:849-852`, no `seen_question_pids`) is only fed by live events. When a PINNED session is resumed (`:789-821`) and the `/event` SSE bus closes (`StopAsyncIteration` `:912`), the polling fallback `_poll_session_deltas` (`:1535-1565`) re-scans the FULL message list and `_yield_part_deltas` (`:1238-1432`) re-yields: old text (full text, `text_lens.get(pid,0)==0` `:1258-1265`), old reasoning ("🧠 thinking…" `:1266-1276`), old tool parts ("✅/⚠️" chunks `:1411-1432`), and a previously resolved question part (`:1279-1410`) yields a question delta which the polling caller (`:954-956`) treats as the agent asking → stops the stream with a STALE question while the real answer never streams. The event-bus path is immune (live events only).
- **Fix 2 — serve-PID matcher (unapplied cycle-7 work)**: `_find_serve_pid` (`:1741-1762`) only matches substring, space-form `f"--port {port}"` (`:1756`). `--port=18999` never matches; `--port 189990` false-positives for search "18999". Downstream users silently no-op: `_serve_start_epoch_ms` `:1488`, `_recycle_serve_if_low_memory` `:1571`, `_force_recycle_serve` `:1602`, `_serve_health` `:1688`, wedge stale-part guard `:1469`.
- **Fix 3 — NEW: no drain after recycle kills**: `_force_recycle_serve` (kill `:1592-1597`) and `_recycle_serve_if_low_memory` (kill `:1613-1622`) send `os.kill(pid, 15)` and return immediately. `ensure_opencode_serve` (`:501`) then probes `is_opencode_serve_running()` which still answers `/config` True mid-SIGTERM → no respawn → the stream POSTs against a dying listener → spurious `[OpenCode Bridge Network Error: …]`. The config-drift branch (`:509-514`) already drains (4 × 0.25s); the two explicit recycle paths do not.

**Outcome**: resumed conversations stream only NEW deltas after a bus drop (no history replay, no stale-question kill), serve discovery matches the exact port token in either form, and recycle always hands a dead listener to `ensure_opencode_serve` for respawn.

## Scope

### In Scope

- `opencode_bridge.py` only:
  - **Fix 1**: for RESUMED (pinned) sessions, one best-effort `GET /session/{id}/message` after pin acquisition (before `prompt_async`): seed `text_lens` (len of each existing assistant text part), `tool_state` (each tool part's state), `user_mids`, and a new `seen_question_pids: set[str]`. Thread the seen set through `_poll_session_deltas` → `_yield_part_deltas` (both call sites: polling `:951-953`, event-bus `:1196-1198`); the question branch short-circuits when pid ∈ seen. Fresh sessions seed nothing (empty set) → zero behavior change. Seeding failure degrades to current behavior, never raises. Blocking path `opencode_chat` untouched.
  - **Fix 2**: `_find_serve_pid` — tokenize the NUL-normalized cmdline; match exact adjacent pair `("--port", port)` or a single token `"--port={port}"`; exact equality only, no substring/prefix matching.
  - **Fix 3**: after the kill in `_force_recycle_serve` and `_recycle_serve_if_low_memory`, bounded drain mirroring `:511-514` — poll `is_opencode_serve_running()` up to ~4 × 0.25s, break early when down — so `ensure_opencode_serve` reliably respawns. The hermetic `hermetic_serve` autouse guard nooping both helpers must stay intact.
- `tests/test_opencode_bridge.py`: additive hermetic tests (see Test Approach).

### Out of Scope

- `opencode_chat` blocking path, routes.py, proxy.py, config, systemd, event-bus behavior.
- Wedge/timeout detection policy; B2 (polling quiet-done cutoff tuning — needs live validation, own cycle); B3 (blocking-path wedge detection); B4 (driver artifact-wait cap); scripts.

## Exploration Summary

Verified against source (orchestrator gatekeeper + propose phase, 2026-08-09):

- Per-call state seeded empty at `:849-852`; polling fallback re-scans full message list (`:1535-1565`); question delta stops the caller (`:954-956`). Event-bus path (`:1196-1198`) is replay-immune.
- Pin resume path `:789-821`; event bus opened before prompt — seeding must happen after pin acquisition, before `prompt_async`.
- `_find_serve_pid` `:1741-1762`: NUL-normalized cmdline, space-form-only substring match at `:1756`.
- Both recycle helpers kill and return immediately (`:1592-1597`, `:1613-1622`); config-drift branch already drains (`:509-514`); `is_opencode_serve_running` answers True mid-SIGTERM → missed respawn.
- Hermetic harness: `_FakeClient` `:38-136`, `_BusyThenIdleClient` `:156-171`, `_PollClient` `:1151-1162` (scripts the message-list GET branch via `poll_messages`), `hermetic_serve` autouse guard `:220-299`, `PP` `:184`, `_clean_pending_permissions` `:187-198`, `fake_client` `:174-178`. Existing NUL-separated `_find_serve_pid` test `:1997-2042` must stay green. Suite currently 140 bridge / 431 total green.

**Persistence note**: Engram persistence is unavailable in this runtime; the durable store is this OpenSpec change folder (filesystem).

## Assumptions & Edge Cases

- Seeding is best-effort: a failed/empty pre-prompt GET degrades to current behavior — replay may occur but the stream MUST NOT raise.
- The seen-question set only suppresses question deltas for parts that existed BEFORE this request's prompt; a post-prompt question (new part) still yields and stops the stream as today.
- Fix 2: existing NUL space-form test stays green; equals-form matches; digit-prefix ("--port 189990" vs search "18999") does NOT match; malformed cmdlines keep the existing try/except resilience.
- Fix 3: drain is bounded (~1s worst case); breaks early when the serve is confirmed down; if the budget expires with the serve still up, `ensure_opencode_serve`'s own probe decides — no worse than today. `hermetic_serve` guard keeps tests hermetic.

## Capabilities

Prior specs exist: `openspec/specs/opencode-bridge-polling-replay-prevention/` (covers Fix 1 + Fix 2 requirements, R1–R12) and `openspec/specs/opencode-serve-lifecycle/` (recycle/respawn). Cycle-7 proposal/design/tasks are unapplied; Fix 1 and Fix 2 are adopted from that pending work.

### New Capabilities

None.

### Modified Capabilities

- `opencode-bridge-polling-replay-prevention`: lands Fix 1 + Fix 2. Requirements R1–R12 already specified — delta spec records adoption and the `seen_question_pids` threading detail (both call sites), no new requirements.
- `opencode-serve-lifecycle`: NEW requirement — after kill, `_force_recycle_serve` and `_recycle_serve_if_low_memory` MUST bounded-drain (`is_opencode_serve_running()` poll, ~4 × 0.25s, early break) so `ensure_opencode_serve` respawns; hermetic noop guard preserved.

## Approach

1. Fix 1: in the resumed branch of `opencode_chat_stream`, best-effort GET the session's messages; seed `text_lens` (per-part current text length), `tool_state` (per-part state), `user_mids`, and `seen_question_pids` (question part pids). Pass the seen set into `_poll_session_deltas` → `_yield_part_deltas`; the question branch returns None when pid ∈ seen. Fresh sessions: empty seed, no behavior change.
2. Fix 2: rewrite the `_find_serve_pid` match — split the NUL-normalized cmdline on whitespace into tokens; match adjacent `"--port"` + `port` tokens or a token equal to `f"--port={port}"`; exact equality only.
3. Fix 3: add a small drain loop helper (poll `is_opencode_serve_running()`, ~4 × 0.25s, early break) called after kill in both recycle helpers; keep the `hermetic_serve` guard intact.
4. Additive tests; run `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q`, then full suite.

## Test Approach

New hermetic tests (no live serve):

1. Resumed pinned session + empty `stream_lines` (forces immediate `StopAsyncIteration`) + scripted `poll_messages` containing old assistant text/tool/question parts → no old text replay, no "✅ done"/"⚠️ failed" spam, no stale-question stop; a scripted new-turn delta still streams.
2. Resumed session where the seeding GET fails (non-200) → degrades to current behavior without raising (replay allowed, no exception).
3. `_find_serve_pid`: equals-form `--port=18999` matches; `--port 189990` does NOT match search for `18999`; existing NUL space-form test (`:1997-2042`) stays green.
4. Fix 3: with `is_opencode_serve_running` monkeypatched (down after first probe / never down), drain breaks early when down and returns without raising when the budget is exhausted; `hermetic_serve` guard still noops the helpers.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `opencode_bridge.py` | Modified | Fix 1: seeding GET + `seen_question_pids` threaded through `_poll_session_deltas` (`:1535`) and `_yield_part_deltas` (`:1238`), both call sites (`:951-953`, `:1196-1198`), question-branch short-circuit (`:1279-1410`). Fix 2: `_find_serve_pid` token matching (`:1741-1762`). Fix 3: bounded drain after kill in `_force_recycle_serve` (`:1592-1597`) and `_recycle_serve_if_low_memory` (`:1613-1622`). |
| `tests/test_opencode_bridge.py` | Modified | New `_PollClient`-based replay/seed-failure tests, `_find_serve_pid` equals-form/prefix-guard cases, drain-loop tests; existing NUL test (`:1997`) untouched. |

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~268 (Fix1 ~160 incl. tests, Fix2 ~58, Fix3 ~50) |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | Single PR |
| Delivery strategy | single-pr |

Decision needed before apply: No
Chained PRs recommended: No
400-line budget risk: Low
