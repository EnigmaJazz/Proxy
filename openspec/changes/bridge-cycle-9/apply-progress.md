# Apply Progress: Bridge Cycle 9 — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills

Status: COMPLETE (autonomous cycle; the apply sub-agent wrote the tests, then the
serve stream timed out before the source landed — the orchestrator verified the
landed source inline on continuation and completed the apply phase).

## Fix 1 — Resumed-session polling replay prevention (code + tests)

- [x] `_seed_resumed_session_state` helper (`opencode_bridge.py:1567-1616`): best-effort
  `GET /session/{id}/message` seed of `user_mids` (assistant message ids), `text_lens`
  (non-empty text part lengths), `tool_state` (tool part statuses), and
  `seen_question_pids`; non-200 / non-list / malformed bodies return silently;
  catches `(httpx.HTTPError, OSError, ValueError)` — never raises.
- [x] `resumed = session_id is not None` + seed call site in `opencode_chat_stream`
  (`:869-876`) — after pin resolution, before the payload/`client.stream`; fresh
  sessions never seed.
- [x] `seen_question_pids: Optional[set[str]] = None` kwarg on `_yield_part_deltas`
  (`:1269`) and `_poll_session_deltas` (`:1625`); question-branch short-circuit
  (`:1310`) before the fetch-retry loop; both call sites pass the set (`:976`, `:1222`).
- [x] Tests: `_SeedPollClient` (first list GET serves scripted seed), `TestSeedResumedSessionState`
  (2), `TestResumedSessionPollingReplay` (4: never-replays-history, new-question-still-stops,
  seed-failure degradation, fresh-session-no-seed).

## Fix 2 — Exact serve-PID port matching (code + tests)

- [x] `_find_serve_pid` (`:1847-1880`): NUL-normalized cmdline tokenized; requires
  `opencode` and `serve` tokens; exact `--port=<port>` token OR exact adjacent
  `("--port", port)` pair; no substring/prefix matching; malformed entries skipped.
- [x] Tests: `TestFindServePidExactMatch` (equals-form matches; longer advertised
  value rejected; shorter search rejected); existing NUL space-form test untouched
  and green.

## Fix 3 — Bounded drain after recycle kills (code + tests)

- [x] `_DRAIN_PROBES = 4`, `_DRAIN_PROBE_S = 0.25`, `_drain_serve_shutdown`
  (`:1657-1672`): polls `is_opencode_serve_running()` up to 4 × 0.25s, early break,
  never raises.
- [x] Drain called after verified kill in `_recycle_serve_if_low_memory` (`:1702`) and
  `_force_recycle_serve` (`:1724`); hermetic `hermetic_serve` autouse guard intact.
- [x] Tests: `TestServeDrain` (`@pytest.mark.real_recycle`, 4: force-recycle early
  break + budget exhaustion, low-memory early break + budget exhaustion).

## Verification (task 17)

- `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` → **153 passed** (140 pre-existing + 13 new), 0 failures/errors.
- `.venv/bin/python -m pytest tests/ -q` → **444 passed** (431 pre-existing + 13 new), 0 failures/errors.
- `.venv/bin/python -m py_compile opencode_bridge.py tests/test_opencode_bridge.py` → clean.

## Notes

- Source changes landed in commit `08471ea` (the commit message covers the professional
  context change; the cycle-9 bridge fixes rode along — recorded here for the archive
  report). The +461-line test addition is uncommitted in the working tree.
- Review workload forecast: ~281 changed lines, 400-line budget risk Low, single PR —
  no `size:exception` needed.
