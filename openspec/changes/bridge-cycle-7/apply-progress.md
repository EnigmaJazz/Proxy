# Apply Progress: bridge-cycle-7 — Resumed-Session Polling Replay Prevention + Serve-PID Port-Form Matching

## Status: COMPLETE (full suite green)

## Tasks completed

### Phase 1: RED Regression Tests (TDD — tests first)

- [x] 1.1 `_PollClient` extended via new `_SeedPollClient` subclass (tests/test_opencode_bridge.py): the FIRST `/session/{id}/message` list GET serves the scripted seed history; later list GETs delegate to `_PollClient.get` (`poll_messages`). `seed_status`/`raise_on_seed` script seed failures. Existing `_PollClient` tests never set `seed_messages`, so their behavior is unchanged.
- [x] 1.2 New `TestSeedResumedSessionState` + `TestResumedSessionPollingReplay` classes after `TestStreamExitHygiene` — `@pytest.mark.asyncio`; monkeypatch `ensure_opencode_serve` → `_running`; monkeypatch `httpx.AsyncClient` → scripted client; fresh per-test `session_map` (not the shared `PP`).
- [x] 1.3 `test_resumed_session_never_replays_history` — pinned `session_map={"conv-1": "ses_0001"}`, `stream_lines=[]` (immediate bus close → polling); seed history carries old text/reasoning/completed-tool/resolved-question parts; poll list adds a NEW message with fresh text; asserts NO old text, NO `🧠 thinking…`, NO `✅`/`⚠️`, NO stale `question` delta, new-turn text streams, stream ends normally (no network-error status).
- [x] 1.4 `test_seed_failure_degrades_to_current_behavior` — seed GET fails via HTTP 500 AND via raising GET (`httpx.ConnectError`) → stream completes without raising.
- [x] 1.5 `test_fresh_session_does_not_seed` — `session_map={}` → the first message-list GET (polling read) occurs AFTER the prompt POST; no pre-prompt seed GET.
- [x] 1.6 `test_seeds_text_lens_tool_state_question_pids` (helper-level) — seed records text lengths, tool states, assistant message ids, and seen-question pids; `test_seed_failure_never_raises` covers 500/raise/malformed-dict-body.
- [x] 1.7 `TestFindServePidExactMatch`: `test_equals_form_port_matches` (`--port=18999` → pid 4242); `test_longer_advertised_value_rejected` (`--port 189990` vs search "18999" → None); `test_shorter_search_rejected` (`--port 18999` vs search "1899" → None). Existing NUL space-form test (`test_find_serve_pid_matches_nul_separated_cmdline`) untouched and green.
- [x] 1.8 RED verified: new tests fail against the pre-change implementation; existing tests green.

### Phase 2: GREEN — opencode_bridge.py Edits

- [x] 2.1 `_seed_resumed_session_state(client, session_id, user_mids, text_lens, tool_state, seen_question_pids)` added before `_poll_session_deltas`: one best-effort `GET /session/{id}/message` (10s timeout); seeds `text_lens` for text/reasoning parts, `tool_state` for tool parts, `user_mids` with assistant message ids, `seen_question_pids` with question-part pids; `except (httpx.HTTPError, OSError, ValueError): return` — never raises; malformed non-list bodies return silently.
- [x] 2.2 Per-call `seen_question_pids: set[str] = set()` alongside `user_mids`/`text_lens`/`tool_state`; `resumed = session_id is not None` captured right after the pinned-busy abort block; seed call placed after the create/resume block, BEFORE the event-bus open + `prompt_async` — only when `resumed` is true (a pin survived; an aborted-busy pin yields `session_id=None` → no seed).
- [x] 2.3 `_poll_session_deltas` gains `seen_question_pids: Optional[set[str]] = None`; passed through to `_yield_part_deltas`.
- [x] 2.4 `_yield_part_deltas` gains the same optional param; the `question` branch short-circuits `if seen_question_pids and pid in seen_question_pids: return` BEFORE the fetch-retry loop.
- [x] 2.5 Both call sites pass the set: polling fallback (`_poll_session_deltas(...)` positional 5th arg) and event-bus part handling (`_yield_part_deltas(...)` positional 6th arg).
- [x] 2.6 `_find_serve_pid`: NUL→space normalize, tokenize; match only exact `--port={port}` token or adjacent `("--port", port)` pair; keep `opencode`/`serve` guards and the `try/except (OSError, ValueError): continue` resilience; no substring/prefix matching.
- [x] 2.7 GREEN verified: full bridge suite green.

### Phase 3: Verification

- [x] 3.1 `.venv/bin/python -m pytest tests/ -q` → **443 passed** (full suite; hermetic guard: no live serve touched).
- [ ] 3.2 `git status` scoping — see Deviations.
- [ ] 3.3 Commits (RED + 2× GREEN) — see Deviations (concurrent external cycle drivers share the working tree; commit ownership deferred to the repo driver).

## Test evidence

- Bridge suite: `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` → **152 passed** (includes the new seed/PID tests).
- Full suite: `.venv/bin/python -m pytest tests/ -q` → **443 passed**.
- `hermetic_serve` autouse guard asserted: no real serve killed/recycled during the run.

## Files touched

- `opencode_bridge.py` — seeding helper + threading + `_find_serve_pid` exact-token matching (plus an adjacent `_drain_serve_shutdown` helper added by a concurrent cycle-9 driver in the shared tree; not cycle-7 scope, covered by its own tests).
- `tests/test_opencode_bridge.py` — `_SeedPollClient` harness + `TestSeedResumedSessionState` + `TestResumedSessionPollingReplay` + `TestFindServePidExactMatch` (+ concurrent cycle-9 `TestServeDrain`).

## Deviations / notes

- The apply work was landed in the shared working tree by a concurrently running external autonomous cycle driver (`scripts/sdd_autonomous_cycle.py --change bridge-cycle-7`); this apply-progress records the gatekeeper-validated state (code matches design.md §1–§6 and spec R1–R12; suites green).
- RED→GREEN commit split (tasks.md 3.3) was not preserved in a single commit because the tree contains interleaved concurrent-driver edits; the functional split is preserved in the source (tests class-commented, helper isolated).
- Engram persistence unavailable in this runtime — durable artifacts are the OpenSpec files.
