# Apply Progress — bridge-cycle-6 (Streaming-Path Error-Exit Hygiene + Serve-Recycle Ordering)

Status: COMPLETE — all 21 tasks checked in `tasks.md`.

## Summary

All GREEN edits for `bridge-cycle-6` were found already committed on branch
`sdd/opencode-bridge-sdd-reliability/pr-5` (applied by the prior interrupted
run; helper + exits landed in `fbbd17e`, later bridge fixes in `3030329`).
This run completed the phase: full RED/GREEN verification, suite runs, and
phase artifacts.

## Verified code state (all present, committed)

1. `_abort_stream_session_best_effort` helper — `opencode_bridge.py:330-354`
   (falsy `session_id` no-op; calls cycle-5 `_abort_session_best_effort`;
   pops `pending_permissions[session_id]` and `session_map[session_key]`
   when containers/keys provided; never raises).
2. Recycle-before-ensure — `opencode_chat_stream`: `_recycle_serve_if_low_memory()`
   at `:781` runs BEFORE `ensure_opencode_serve()` at `:782` (non-autonomous
   path; autonomous force-recycle unchanged at `:775-776`).
3. Prompt non-204 exit — cleanup trio at `:871-878`, then
   `[OpenCode Bridge Error: prompt HTTP <code>]` verbatim.
4. `message.updated` `info.error` exit — trio at `:1185-1192`, then
   `[OpenCode Bridge Error: <info.error>]` verbatim.
5. Outer `except (httpx.HTTPError, OSError, ValueError)` — trio guarded by
   `if session_id:` at `:1212-1221`, then `[OpenCode Bridge Network Error: ...]` verbatim.
6. DRY: pinned-busy (`:822`), timeout (`:905`), polling timeout (`:949`),
   wedge ×2 (`:1080`, `:1120`) all use the helper; yields verbatim.
7. `_recycle_serve_if_low_memory` internals, error strings, `opencode_chat`
   blocking path, routes.py, proxy.py, config, systemd untouched.

## Tests (all hermetic, no live serve — `hermetic_serve` autouse guard)

- `_FakeClient` scripting: `prompt_status: int = 204` (`:83`), `fail_get_after`
  + `_get_count` (`:85`, `:102`), `prompt_async` POST branch (`:130`).
- `TestStreamExitHygiene` (`:550-695`): the four regression tests.

| Test | Result |
|---|---|
| `test_stream_network_error_aborts_and_drops_pin` | PASS — network error + abort + pin drop + pending-pop |
| `test_message_updated_error_aborts_and_drops_pin` | PASS — `[OpenCode Bridge Error: boom]` + trio |
| `test_prompt_non_204_drops_pin` | PASS — `[OpenCode Bridge Error: prompt HTTP 500]` + trio |
| `test_recycle_before_ensure_no_failure` | PASS — order `["recycle", "ensure"]`, stream completes, pin kept |

## Test results

- `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` → **140 passed** (136 + 4 new)
- `.venv/bin/python -m pytest tests/ -q` → **431 passed** (full suite green)

## Known deviation from design (accepted)

- Design/test-plan task 1.5 scripted the mid-stream failure with
  `fail_get_after = 1`; the committed test uses `raise_on_stream = True`
  instead. Both deterministically trigger the SAME outer-except handler
  (`raise_on_stream` raises on the `/event` SSE read inside the loop), so
  REQ-4 coverage is equivalent. The `fail_get_after`/`_get_count` scripting
  requested by REQ-7 exists in `_FakeClient` but is not exercised by a
  dedicated test. No functional gap; noted for verify.

## Commit state

- Branch: `sdd/opencode-bridge-sdd-reliability/pr-5`
- HEAD: `3030329` — working tree clean except untracked `openspec/changes/*` artifacts
- Cycle-6 code+tests committed in `fbbd17e` (previous run); no new commits needed this run.
