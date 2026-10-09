# Tasks: Bridge Cycle 4 — Serve-Lifecycle Hardening + Blocking-Path Respawn

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~300 (incl. tests) |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | Single PR |
| Delivery strategy | single-pr |
| Chain strategy | single-pr |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: single-pr
400-line budget risk: Low

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | REQ-2/3/4 in one commit | Single PR | `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` | N/A — hermetic `_FakeClient` (no live serve, no real /proc) | `git revert` the single commit |

## Context

- **Strict TDD** (`strict_tdd: true`): RED tests first → GREEN code → verification.
- **Do NOT touch**: `routes.py`, `proxy.py`, `opencode-serve-config.opencode.jsonc`, existing error-string formats, REQ-1 code (recycle-before-ensure already in HEAD, `opencode_chat_stream` :781 — verify-only).
- **New helpers**: `_cmdline_matches_serve(cmd, port)` + `_pid_is_serve(pid, port)` in `opencode_bridge.py`; `_find_serve_pid` (:1747) refactors onto the matcher.
- Re-locate anchors with grep; line numbers shift.

## Phase 1: RED Regression Tests (TDD — tests first)

- [ ] 1.1 `_FakeClient.__init__`: add `status_map` (REQ-2) + `fail_post_times: int = 0` (REQ-4)
- [ ] 1.2 `_FakeClient.get` `/session/status` → `status_map` if set, else `{session_id: {"type": "idle"}}`
- [ ] 1.3 `_FakeClient.post`: if `fail_post_times > 0` and `"/message" in url`, decrement + `raise httpx.ConnectError`
- [ ] 1.4 `TestStreamExitHygiene::test_stale_pin_dropped_when_status_lacks_id` — `status_map={}`, pinned `smap`; fresh `/session` POST, `smap` holds new id, no `/abort`
- [ ] 1.5 `test_pin_kept_on_status_fetch_transport_error` — `raise_timeout_on="get"`; NO fresh `/session` POST (pin kept)
- [ ] 1.6 `TestServeHealth::test_recycle_skips_kill_on_pid_mismatch` — `_pid_is_serve`→False; kill recorder empty
- [ ] 1.7 `test_recycle_kills_on_pid_match` — `_pid_is_serve`→True; killed == [12345]
- [ ] 1.8 `test_force_recycle_skips_kill_on_pid_mismatch` + `test_force_recycle_kills_on_pid_match`
- [ ] 1.9 `test_pid_is_serve_matches_cmdline` — `_FakeProc` fake `/proc/<pid>/cmdline`; match + mismatch
- [ ] 1.10 `TestOpenCodeChatHardening::test_blocking_respawn_single_retry` — `fail_post_times=1`; 2 message POSTs, 1 recycle, 2 ensures, result == text
- [ ] 1.11 `test_blocking_respawn_second_failure_returns_error` — `fail_post_times=2`; error string, 1 recycle, 2 POSTs
- [ ] 1.12 `test_blocking_no_retry_on_http_error` — `message_status=503`; HTTP error string, no recycle, 1 POST
- [ ] 1.13 `test_blocking_respawn_reensure_failure` — ensure True→False; not-reachable string, 1 recycle
- [ ] 1.14 Update `test_recycles_old_serve`: add `_pid_is_serve`→True patch
- [ ] 1.15 Verify RED — new tests FAIL, 140 existing pass

## Phase 2: GREEN — opencode_bridge.py Edit

- [ ] 2.1 REQ-2 resume block (:810): `fetch_ok` flag; drop pin when `fetch_ok and session_id not in st_map` (design §1 exact shape); busy branch unchanged
- [ ] 2.2 REQ-3: add `_cmdline_matches_serve` (NUL-normalize + `"opencode"`/`"serve"`/`--port {port}`) + `_pid_is_serve` (read `/proc/<pid>/cmdline`, OSError→False)
- [ ] 2.3 Refactor `_find_serve_pid` (:1747) loop onto `_cmdline_matches_serve` — byte-identical
- [ ] 2.4 REQ-3 `_recycle_serve_if_low_memory` (:1599): skip kill unless `await asyncio.to_thread(_pid_is_serve, pid, port)`
- [ ] 2.5 REQ-3 `_force_recycle_serve` (:1622): same `_pid_is_serve` guard before `os.kill(pid, 15)`
- [ ] 2.6 REQ-4: split `opencode_chat` (:628) → wrapper + `_opencode_chat_attempt(client, ...) -> tuple[str, bool]`; wrapper recycles, re-ensures, retries once on `network_failed` (D5–D7); never raises
- [ ] 2.7 Verify GREEN — ~150 green

## Phase 3: Verification

- [ ] 3.1 `.venv/bin/python -m pytest tests/ -q` — full suite green
- [ ] 3.2 `git diff --stat` — only `opencode_bridge.py`, `tests/test_opencode_bridge.py`, `openspec/changes/bridge-cycle-4/`, `openspec/specs/opencode-serve-lifecycle/`
- [ ] 3.3 Commit: `fix(bridge): drop stale pins, verify pids before kill, respawn blocking calls`
