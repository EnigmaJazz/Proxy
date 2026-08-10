# Verify Report — bridge-cycle-6 (Streaming-Path Error-Exit Hygiene + Serve-Recycle Ordering)

Status: **PASS**

## Executive Summary

All seven requirements of `openspec/specs/opencode-bridge-stream-exit-hygiene/` (REQ-1..REQ-7)
are satisfied by the committed implementation on branch `sdd/opencode-bridge-sdd-reliability/pr-5`
(HEAD `08471ea`, cycle-6 code landed in `fbbd17e`). Every streaming-path error exit of
`opencode_chat_stream` now runs the full cleanup trio (best-effort abort + pin drop +
pending-permission pop) through the shared helper `_abort_stream_session_best_effort`, and the
serve-recycle check runs before ensure/respawn on the non-autonomous path. Success-path behavior
is unchanged; the full test suite is green (444 passed).

## Per-Requirement Verification

| Req | Requirement | Result | Evidence (opencode_bridge.py) |
|-----|-------------|--------|-------------------------------|
| REQ-1 | Shared best-effort helper | PASS | `_abort_stream_session_best_effort` at :334-358: `async`, falsy `session_id` no-op (:352-353), delegates abort to cycle-5 `_abort_session_best_effort` (catches `(httpx.HTTPError, OSError)` at :330-331), pops `pending_permissions[session_id]` (:355-356) and `session_map[session_key]` (:357-358), never raises. Name distinct from cycle-5 helper. |
| REQ-2 | Prompt non-204 exit cleans up | PASS | :888-897 — trio call (:891-895) then `yield ("status", f"[OpenCode Bridge Error: prompt HTTP {async_resp.status_code}]")` (:896), return (:897). Error string verbatim. |
| REQ-3 | `message.updated` error exit cleans up | PASS | :1203-1212 — `info.get("error")` branch runs trio (:1206-1210) then `yield ("status", f"[OpenCode Bridge Error: {info['error']}]")` (:1211), return (:1212). Verbatim. |
| REQ-4 | Outer network error exit cleans up | PASS | :1231-1240 — `except (httpx.HTTPError, OSError, ValueError)` with `if session_id:` guard (:1232), trio (:1235-1239), then `yield ("status", f"[OpenCode Bridge Network Error: {str(exc)}]")` (:1240). Verbatim. |
| REQ-5 | Success + existing cleanup paths unchanged | PASS | Pinned-busy DRY site :826-830 (then `session_id = None` at :831); timeout/polling-timeout/wedge sites (:905, :949, :1080, :1120) all call the helper with verbatim yields; success path (prompt POST :883-887, event loop) untouched. Full suite green — 444 passed. |
| REQ-6 | Recycle before ensure | PASS | :785 `await _recycle_serve_if_low_memory()` runs BEFORE :786 `ensure_opencode_serve()` on the non-autonomous path; autonomous force-recycle unchanged (:779-780). Comment documents the ordering (:781-784). |
| REQ-7 | Regression coverage | PASS | `TestStreamExitHygiene` (4 tests) pass; `_FakeClient` gains `prompt_status`/`fail_get_after` scripting; abort observability via `post_calls` (apply-progress records :83, :85, :102, :130). |

## Test Evidence

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q -k "TestStreamExitHygiene"` | 4 passed |
| `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` | 153 passed |
| `.venv/bin/python -m pytest tests/ -q` | 444 passed |

All hermetic: `hermetic_serve` autouse fixture guards against live-serve access; no real /proc,
network, or serve process involved.

## Known Deviation (accepted, non-blocking)

Design task 1.5 scripted the mid-stream failure with `fail_get_after = 1`; the committed test uses
`raise_on_stream = True` instead. Both deterministically trigger the same outer-except handler
(REQ-4 coverage is equivalent); the `fail_get_after`/`_get_count` scripting required by REQ-7
exists in `_FakeClient` but is not exercised by a dedicated test. No functional gap.

## Risks / Observations

- **Out of scope (observation only):** the working tree contains uncommitted work for later cycles
  (bridge-cycle-7/8/9 artifacts under `openspec/changes/`, plus uncommitted cycle-8 code and tests
  in `opencode_bridge.py` / `tests/test_opencode_bridge.py`, e.g. `_seed_resumed_session_state`,
  `TestSeedResumedSessionState`). These were NOT evaluated here and must not be archived with this
  change. They currently pass (included in the 153/444 counts) but remain uncommitted.
- No routes.py / proxy.py / config / systemd / error-string / `_recycle_serve_if_low_memory`
  internal changes — confirmed out of diff scope.

## Next Recommended Phase

`archive`
