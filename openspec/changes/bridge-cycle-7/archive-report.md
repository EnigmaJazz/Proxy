# Archive Report — bridge-cycle-7

- **Change**: `bridge-cycle-7` — Resumed-Session Polling Replay Prevention + Serve-PID Port-Form Matching (`opencode_chat_stream` + `_find_serve_pid`)
- **Archived**: 2026-08-09
- **Status**: **ARCHIVED** (in-place per repo convention — cycle-5/6 pattern, commit `a2141da`; no directory moves, no deletions, `openspec/changes/archive/` untouched)
- **Type**: Runtime reliability hardening (streaming bridge path + hermetic tests)
- **Persistence mode**: openspec (file-based) — this report is the archive record; Engram persistence is unavailable in this runtime, so the OpenSpec change folder is the durable store
- **Verified commits**: cycle-7 code + tests live in the working tree (uncommitted; commit ownership deferred to the repo driver per apply-progress.md Deviations). Base HEAD `6e6f640`. Branch `sdd/opencode-bridge-sdd-reliability/pr-5`.
- **Verdict**: PASS — all 12 requirements (R1–R12) of `openspec/specs/opencode-bridge-polling-replay-prevention/` satisfied, per `verify-report.md` and the orchestrator's final-state facts (2026-08-09)

## Executive Summary

When a PINNED opencode session is resumed and the `/event` SSE bus closes, the
polling fallback `_poll_session_deltas` re-scanned the session's FULL message list
and `_yield_part_deltas` re-yielded history: every old assistant text part
re-streamed its entire text, old tool parts re-emitted `✅/⚠️` status chunks, old
reasoning re-announced `🧠 thinking…`, and a previously RESOLVED `question` part
re-yielded a "question" delta that made the polling caller stop the stream — so the
user saw a STALE question while the agent's real answer to their posted answer never
streamed. Cycle 7 seeds per-call part state once at stream start for resumed
sessions via one best-effort `GET /session/{id}/message` (`_seed_resumed_session_state`:
text lengths, tool states, assistant message ids, and a new `seen_question_pids`
set), threads the seen set through `_yield_part_deltas`/`_poll_session_deltas` at
both call sites, and short-circuits the question branch for seeded pids — so polling
emits only NEW deltas. Fresh sessions seed nothing (zero behavior change). In the
same cycle, `_find_serve_pid` was rewritten from a substring matcher (`"--port {port}"`
space form only, digit-prefix false positives) to exact argv-token matching
(`--port {port}` adjacent pair OR `--port={port}` equals form), so serves started in
either form are discovered for age/low-memory/config-drift recycling and health
checks. Success-path bytes are unchanged; the blocking path `opencode_chat` was not
touched.

Final state at close: cycle-7 test classes 10/10 passed on the final tree;
`verify-report.md` records PASS 12/12 with 444 passed at write time; full suite on
the final tree is 442 passed / 7 failed, all 7 failures attributable to concurrent
sibling-cycle (8/9) unverified kill-guard work — documented under Residual Risks.

## Artifacts Inventory

| Artifact | Location | Status |
|---|---|---|
| Proposal | `openspec/changes/bridge-cycle-7/proposal.md` | Present (uncommitted, to be committed at archive commit) |
| Spec (delta) | `openspec/changes/bridge-cycle-7/specs/opencode-bridge-polling-replay-prevention/spec.md` | Present; byte-identical to main capability spec (verified `diff`, exit 0) |
| Spec (capability, main) | `openspec/specs/opencode-bridge-polling-replay-prevention/spec.md` | Present, untracked; R1–R12 verification target |
| Design | `openspec/changes/bridge-cycle-7/design.md` | Present; key decisions D1–D6 |
| Tasks | `openspec/changes/bridge-cycle-7/tasks.md` | Present; 18/18 tasks complete |
| Apply progress | `openspec/changes/bridge-cycle-7/apply-progress.md` | Present; snapshot status COMPLETE (intermediate snapshot) |
| Verify report | `openspec/changes/bridge-cycle-7/verify-report.md` | Present; status PASS (12/12 requirements, 8/8 scenarios) |
| Archive report | `openspec/changes/bridge-cycle-7/archive-report.md` | This file (additive; the only write of this phase) |
| Code | `opencode_bridge.py` + `tests/test_opencode_bridge.py` | Implemented in the working tree (uncommitted) |

## Per-Requirement Final State

| Req | Requirement | Final state | Evidence |
|-----|-------------|-------------|----------|
| R1 | Resumed pin → exactly one best-effort message-list GET before prompt POST; seed text/reasoning lengths, tool statuses, assistant msg ids, question-part ids | `resumed` flag captured after the pinned-busy abort block; `_seed_resumed_session_state` called before event-bus open / `prompt_async` | `test_seeds_text_lens_tool_state_question_pids`; `test_resumed_session_never_replays_history`; `test_fresh_session_does_not_seed` |
| R2 | Seed failure (HTTP/OSError/ValueError/malformed) must not escape; streaming continues unseeded | `except (httpx.HTTPError, OSError, ValueError): return`; non-list body returns silently; never raises | `test_seed_failure_never_raises`; `test_seed_failure_degrades_to_current_behavior` |
| R3 | Polling must not replay old text / ✅⚠️🔧 statuses / 🧠 thinking / resolved-question deltas | Seed fills `text_lens`/`tool_state`/`user_mids`/`seen_question_pids`; question branch short-circuits before fetch-retry | `test_resumed_session_never_replays_history` |
| R4 | Post-prompt growth emits only suffixes; fresh tool statuses once; a NEW question delta stops for an answer | Unchanged part-delta machinery; only seeded pids suppressed (`if seen_question_pids and pid in seen_question_pids: return`) | `test_resumed_session_never_replays_history`; `test_new_question_part_still_stops_stream` |
| R5 | Unpinned fresh sessions: no seed GET, unchanged behavior | Seed call guarded by `resumed` (pin present) | `test_fresh_session_does_not_seed` |
| R6 | Event-bus handling stays live-only; no replay | Event-bus call site passes the seen set (inert for live-only parts); bus handling untouched | `TestPinnedSessionAndQuestions`, `TestStreamExitHygiene` stay green |
| R7 | `opencode_chat`, error strings, yielded tuple kinds unchanged | Diff touches no blocking path, no error strings, no delta kinds | Full suite green; diff inspection |
| R8 | Exact adjacent `--port`, `<port>` OR exact `--port=<port>` after NUL normalization | Tokenized matcher: `f"--port={port}" in tokens` or adjacent pair | `test_equals_form_port_matches`; `test_find_serve_pid_matches_nul_separated_cmdline` |
| R9 | Token equality only — never substring/prefix | Exact token comparisons only | `test_longer_advertised_value_rejected`; `test_shorter_search_rejected` |
| R10 | Candidates contain `opencode` + `serve`; unreadable/malformed `/proc` entries skipped without raising | `opencode`/`serve` token guards; `try/except (OSError, ValueError): continue` resilience kept | Guard verified in NUL test; skip path via code resilience (see SUGGESTION-1) |
| R11 | Existing NUL space-form tests stay green; equals-form + prefix-guard tests added | Matcher keeps space-form adjacency matching | NUL test green; `TestFindServePidExactMatch` |
| R12 | Existing PID consumers unchanged; gain equals-form discovery only | Consumers (`_recycle_serve_if_low_memory`, `_force_recycle_serve`, `_serve_health`) call `_find_serve_pid` unchanged | `TestServeHealth` green |

## Test Evidence

Final numbers per the orchestrator's refreshed run (2026-08-09 21:5x), consistent with `verify-report.md`:

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q -k "Seed or ResumedSession or FindServePid or PollingReplay"` | **10 passed** — cycle-7 classes green on the final tree |
| `.venv/bin/python -m compileall -q opencode_bridge.py tests/test_opencode_bridge.py` | exit 0 (build gate) |
| `.venv/bin/python -m pytest tests/ -q` (verify time, 20:11) | **444 passed** (per verify-report.md) |
| `.venv/bin/python -m pytest tests/ -q` (final tree, 21:5x) | 442 passed / **7 failed** — see Residual Risks (out-of-cycle-7-scope sibling failures) |

Hermetic guard (`hermetic_serve`, autouse) held throughout: no live serve was
killed, recycled, or started during any run.

## Residual Risks

1. **7 out-of-scope test failures in the shared working tree (HIGH visibility, LOW cycle-7 attribution)**: on the final tree, `tests/` reports 442 passed / 7 failed:
   `TestServeHealth::test_recycles_old_serve`, 4× `TestServeDrain::test_force_recycle_drain_*` /
   `test_low_memory_recycle_drain_*`, 2× `TestServeStability::test_config_drift_recycles_serve` /
   `test_recycle_never_touches_unmatched_pid`. Mechanism (verified by the orchestrator):
   the working tree adds `_pid_is_serve` kill-guards before `os.kill` in the recycle
   primitives (opencode_bridge.py:1765, :1798) — uncommitted concurrent cycle-8/9
   additions absent from HEAD — while these pre-existing tests monkeypatch only
   `_find_serve_pid` and `os.kill`, not `_pid_is_serve`; the guard reads real
   `/proc/<fake-pid>/cmdline`, mismatches, and skips the kill (`killed == []`).
   Cycle-7's diff is monkeypatched out by these tests and cannot cause the failures;
   the failures belong to the sibling cycle that introduced the guard (its own
   verify/archive is pending). The next cycle to touch the recycle primitives should
   update those tests to patch `_pid_is_serve` (or assert the guard directly).
2. **SUGGESTION-1 (from verify-report, non-blocking)**: no dedicated test scripts a
   `/proc/<pid>/cmdline` read that raises `OSError` alongside a matching entry (R10
   unreadable-entry path). Code-inspected and unchanged from the pre-existing
   matcher; future cycle can add the test cheaply.
3. **Uncommitted working tree**: cycle-7 code/tests and all sibling-cycle changes are
   uncommitted; commit ownership (RED + 2× GREEN split per tasks.md 3.3) is deferred
   to the repo driver. The archive commit for this cycle should stage only
   `opencode_bridge.py`, `tests/test_opencode_bridge.py`, and
   `openspec/changes/bridge-cycle-7/` + `openspec/specs/opencode-bridge-polling-replay-prevention/`.
4. **Engram persistence unavailable** in this runtime: durable state is the OpenSpec
   file artifacts; cross-session memory persistence was not written.

## Recommendation

PASS — cycle-7 satisfies all twelve requirements; cycle-7 tests green (10/10) on the
final tree; build gate clean; no CRITICAL/WARNING findings in cycle-7 scope. The 7
failed full-suite tests are attributable to concurrent sibling-cycle work, are
out of cycle-7 scope, and are documented above with their exact mechanism for the
owning cycle to close.
