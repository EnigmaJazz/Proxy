# Proposal: Bridge Cycle 9 — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills

**One-line summary**: Land the three pending opencode-bridge fixes specified in the unapplied cycle-8 folder (which adopted unapplied cycle-7 work) — (1) seed per-call part state at stream start for resumed pinned sessions so the polling fallback never replays history or re-surfaces a stale "question" that kills the stream, (2) make `_find_serve_pid` match `--port`/`--port=` exactly instead of substring matching, plus (3) bounded listener drain after recycle kills so `ensure_opencode_serve` reliably respawns. ~268 lines incl. tests, single PR.

## Intent (Why)

- **Fix 1 — resumed-session polling replay (adopted from unapplied cycle-7/8)**: per-call state (`user_mids`/`text_lens`/`tool_state`, seeded empty at `:855-859`) is only fed by live events. When a PINNED session is resumed (`:799-827`) and the `/event` SSE bus closes, the polling fallback `_poll_session_deltas` (`:1541-1547`, call at `:957-959`) re-scans the FULL message list and `_yield_part_deltas` (`:1244-1250`) re-yields old text/reasoning/tool parts and a previously resolved question part → the caller (`:960-962`) stops the stream with a STALE question while the real answer never streams. The event-bus path is replay-immune (live events only). Note: per cycle-7 design, `user_mids` holds ASSISTANT message ids used as a seen-parts filter (`:1561`) — the name is a historical misnomer.
- **Fix 2 — serve-PID matcher (adopted)**: `_find_serve_pid` (`:1747-1768`) matches the NUL-normalized cmdline by substring `f"--port {port}" in cmd` (`:1762`). `--port=18999` never matches; `--port 189990` false-positives for search "18999". Downstream users silently no-op: `_serve_start_epoch_ms` `:1494`, `_recycle_serve_if_low_memory` `:1577`, `_force_recycle_serve` `:1608`, `_serve_health` `:1694`, wedge stale-part guard `:1475`.
- **Fix 3 — NEW: no drain after recycle kills**: `_force_recycle_serve` (kill `:1624`) and `_recycle_serve_if_low_memory` (kill `:1601`; age trigger `:1593-1594` via `_SERVE_RECYCLE_AFTER_S`) send `os.kill(pid, 15)` and return immediately. `ensure_opencode_serve` (`:492`) probes `is_opencode_serve_running()` which still answers True mid-SIGTERM → no respawn → the stream POSTs against a dying listener → spurious network error. The config-drift branch already drains (`:516-520`, 4 × 0.25s early-break); the two explicit recycle paths do not.

**Outcome**: resumed conversations stream only NEW deltas after a bus drop (no history replay, no stale-question kill), serve discovery matches the exact port token in either form, and recycle always hands a dead listener to `ensure_opencode_serve` for respawn.

## Scope

### In Scope

- `opencode_bridge.py` only:
  - **Fix 1**: for RESUMED (pinned) sessions, one best-effort `GET /session/{id}/message` after pin acquisition, before `prompt_async`: seed `text_lens` (len of each existing assistant text part), `tool_state` (each tool part's state), `user_mids` (existing assistant message ids — design semantics), and a new `seen_question_pids: set[str]` (ids of existing question tool parts). Thread the seen set through `_poll_session_deltas` → `_yield_part_deltas` (both call sites `:957-959`, `:1202-1204`); the question branch short-circuits when pid ∈ seen (before the fetch-retry loop at `:1322`). Fresh sessions seed nothing → zero behavior change. Seeding failure degrades silently (catch `httpx.HTTPError`, `OSError`, `ValueError`; never raises). Blocking `opencode_chat` path untouched.
  - **Fix 2**: `_find_serve_pid` — tokenize the NUL-normalized cmdline; match exact adjacent pair `("--port", port)` OR a single exact token `f"--port={port}"`; exact equality only, no substring/prefix matching; keep malformed-entry resilience.
  - **Fix 3**: after the verified kill in `_force_recycle_serve` AND `_recycle_serve_if_low_memory` (any trigger: low-memory OR age), poll `is_opencode_serve_running()` up to ~4 × 0.25s, break early when down, never raise, so `ensure_opencode_serve` reliably respawns. The `hermetic_serve` autouse guard nooping both helpers must stay intact.
- `tests/test_opencode_bridge.py`: additive hermetic tests (see Test Approach).

### Out of Scope

- `opencode_chat` blocking path, routes.py, proxy.py, config, systemd, event-bus behavior.
- Wedge/timeout detection policy; polling quiet-done cutoff tuning (needs live validation, own cycle); blocking-path wedge detection; driver artifact-wait cap; scripts.

## Exploration Summary

Verified against source (explore phase, read-only, 2026-08-09):

- Per-call state seeded empty at `:855-859`; polling fallback re-scans full message list (`:1541-1547`, call `:957-959`); question delta stops the caller (`:960-962`). Event-bus path (`:1202-1204`) is replay-immune.
- Pin resume `:799-827`; bus opens `:865` — seeding must happen after pin acquisition, before `prompt_async`.
- `_find_serve_pid` `:1747-1768`: NUL-normalized cmdline, space-form-only substring match at `:1762`.
- Recycle helpers kill and return (`:1577-1605`, kill `:1601`; `:1608-1628`, kill `:1624`; age trigger `:1593-1594`); config-drift branch already drains (`:516-520`); F5 `_serve_config_mtime` carve-out documented at `:1631-1638`.
- Hermetic harness: `_FakeResp` `:29`, `_FakeStream` `:38`, `_FakeClient` `:56-136`, `_BusyThenIdleClient` `:156-171`, `fake_client` `:174-178`, `PP` `:184`, `_clean_pending_permissions` `:187-198` (autouse), `hermetic_serve` autouse guard `:268-300`, `_PollClient` `:1151-1162` + `_assistant_msg` `:1165-1166` (scripts the message-list GET branch via `poll_messages`). Existing NUL `_find_serve_pid` test `:1997-2042` (`_fake_listdir`/`_fake_open`/`_FakeProc` pattern) must stay green. Suite: 140 bridge / 431 total green.

**Persistence note**: Engram persistence is unavailable in this runtime; the durable store is this OpenSpec change folder (filesystem). The cycle-8 folder (`proposal.md` + `spec.md`) is untracked and NOT applied — cycle-9 adopts and refines its scope; no cycle-8 artifacts are copied or applied.

## Assumptions & Edge Cases

- Seeding is best-effort: a failed/empty pre-prompt GET degrades to current behavior — replay may occur but the stream MUST NOT raise.
- `seen_question_pids` only suppresses question deltas for parts that existed BEFORE this request's prompt; a post-prompt question (new part) still yields and stops the stream as today.
- Fix 2: existing NUL space-form test stays green; equals-form matches; digit-prefix (`--port 189990` vs search "18999") does NOT match; malformed cmdlines keep the existing try/except resilience.
- Fix 3: drain is bounded (~1s worst case), breaks early when the serve is confirmed down; if the budget expires with the serve still up, `ensure_opencode_serve`'s own probe decides — no worse than today. `hermetic_serve` guard keeps tests hermetic.

## Capabilities

Prior specs exist: `openspec/specs/opencode-bridge-polling-replay-prevention/` (covers Fix 1 + Fix 2 requirements, R1–R12) and `openspec/specs/opencode-serve-lifecycle/` (recycle/respawn). Cycle-7/8 proposal/spec/design artifacts are unapplied; Fix 1 and Fix 2 are adopted from that pending work. Cycle-9 records the adoption as a delta spec in this change folder — no new capability specs.

### New Capabilities

None.

### Modified Capabilities

- `opencode-bridge-polling-replay-prevention`: lands Fix 1 + Fix 2. Requirements R1–R12 already specified — delta spec records adoption and the `seen_question_pids` threading detail (both call sites), no new requirements.
- `opencode-serve-lifecycle`: NEW requirement — after kill, `_force_recycle_serve` and `_recycle_serve_if_low_memory` MUST bounded-drain (`is_opencode_serve_running()` poll, ~4 × 0.25s, early break) so `ensure_opencode_serve` respawns; hermetic noop guard preserved.

## Approach

1. Fix 1: in the resumed branch of `opencode_chat_stream`, best-effort GET the session's messages; seed `text_lens` (per-part current text length), `tool_state` (per-part state), `user_mids`, and `seen_question_pids` (question part pids). Pass the seen set into `_poll_session_deltas` → `_yield_part_deltas`; the question branch returns None when pid ∈ seen (before fetch-retry `:1322`). Fresh sessions: empty seed, no behavior change.
2. Fix 2: rewrite the `_find_serve_pid` match — split the NUL-normalized cmdline on whitespace into tokens; match adjacent `"--port"` + `port` tokens or a token equal to `f"--port={port}"`; exact equality only.
3. Fix 3: add a small drain helper (poll `is_opencode_serve_running()`, ~4 × 0.25s, early break, never raises) called after the kill in both recycle helpers; keep the `hermetic_serve` guard intact.
4. Additive tests; run `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q`, then full suite.

## Test Approach

New hermetic tests (no live serve):

1. Resumed pinned session + empty `stream_lines` (forces immediate `StopAsyncIteration`) + scripted `poll_messages` containing old assistant text/tool/question parts → no old text replay, no "✅ done"/"⚠️ failed" spam, no stale-question stop; a scripted new-turn delta still streams.
2. Resumed session where the seeding GET fails (non-200/HTTP error) → degrades to current behavior without raising (replay allowed, no exception).
3. `_find_serve_pid`: equals-form `--port=18999` matches; `--port 189990` does NOT match search for "18999"; existing NUL space-form test (`:1997-2042`) stays green.
4. Fix 3: with `is_opencode_serve_running` monkeypatched (down after first probe / never down), drain breaks early when down and returns without raising when the budget is exhausted; `hermetic_serve` guard still noops the helpers.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `opencode_bridge.py` | Modified | Fix 1: seeding GET + `seen_question_pids` threaded through `_poll_session_deltas` (`:1541`) and `_yield_part_deltas` (`:1244`), both call sites (`:957-959`, `:1202-1204`), question-branch short-circuit (`:1285-1416`, fetch-retry `:1322`, yields `:1411`/`:1415`). Fix 2: `_find_serve_pid` token matching (`:1747-1768`). Fix 3: bounded drain after kill in `_force_recycle_serve` (`:1624`) and `_recycle_serve_if_low_memory` (`:1601`). |
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
