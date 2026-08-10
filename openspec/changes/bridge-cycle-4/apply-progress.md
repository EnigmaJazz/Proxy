# Apply Progress: Bridge Cycle 4 — Serve-Lifecycle Hardening + Blocking-Path Respawn

Status: COMPLETE (2026-08-09, autonomous session; apply executed inline by the
orchestrator after sub-agent delegation was interrupted — fallback rule).

## Session preflight (user-supplied)

- Pace: Automatic
- Artifact store: Both (Engram + OpenSpec) — engram MCP not callable in this
  runtime; OpenSpec files are authoritative for this session.
- PRs: Single PR (`single-pr`)
- Review budget: 400 lines

## What was implemented

Working tree base: HEAD `30be86b` (re-checked after the tree moved mid-cycle;
the apply was re-applied against the final HEAD).

### REQ-1 — Stream recycle precedes ensure (verify-only, already in HEAD)
`opencode_chat_stream` already calls `_recycle_serve_if_low_memory()` before
`ensure_opencode_serve()` (non-autonomous path). No code change; covered by
existing `TestStreamExitHygiene::test_recycle_before_ensure_no_failure`.

### REQ-2 — Stale pinned sessions self-heal (`opencode_chat_stream` resume block)
- Added a `fetch_ok` success flag around the `/session/status` GET.
- `fetch_ok and session_id not in st_map` → drop the pin
  (`pending_permissions.pop` + `session_map.pop`) and start fresh; no abort
  POST (the session is gone).
- Transport-error fetch keeps the pin conservatively (unchanged behavior).

### REQ-3 — Kill primitives verify the selected PID
- New `_cmdline_matches_serve(cmd, port)` — NUL-normalized token matcher with
  EXACT port equality (preserves the cycle-9 tokenized semantics; refactors
  the inline logic out of `_find_serve_pid` byte-identically).
- New `_pid_is_serve(pid, port)` — re-reads `/proc/<pid>/cmdline`; unreadable
  cmdline counts as mismatch; never raises.
- `_recycle_serve_if_low_memory` and `_force_recycle_serve` both skip
  `os.kill` when verification fails (warn + return; the serve self-heals via
  the next ensure). Drain (`_drain_serve_shutdown`) runs only after a real
  kill, per REQ-5 semantics.

### REQ-4 — Blocking calls receive one bounded respawn retry (`opencode_chat`)
- Split into thin wrapper `opencode_chat` + `_opencode_chat_attempt(client,
  ...) -> tuple[str, bool]` (never raises).
- Wrapper: on `network_failed` after a successful first ensure →
  `_force_recycle_serve("blocking-path respawn")` → re-ensure → retry exactly
  once. Failed re-ensure returns the not-reachable string; a failed retry
  returns its error string. No loop.
- `network_failed=True` only for network-class paths
  (httpx.HTTPError/OSError/ValueError); HTTP-status errors, missing ids,
  empty responses, and WRITE-permission aborts return `False` (no retry).
- One `httpx.AsyncClient` reused across both attempts.

## Test changes (`tests/test_opencode_bridge.py`)

- `_FakeClient`: added `status_map` (REQ-2) + `fail_post_times` (REQ-4);
  `/session/status` honors `status_map`; message POSTs honor `fail_post_times`.
- NEW REQ-2: `TestStalePinSelfHeal::test_stale_pin_dropped_when_status_lacks_id`,
  `test_pin_kept_on_status_fetch_transport_error`.
- NEW REQ-3: `TestServeHealth::test_recycle_kills_on_pid_match`,
  `test_recycle_skips_kill_on_pid_mismatch`,
  `test_force_recycle_kills_on_pid_match`,
  `test_force_recycle_skips_kill_on_pid_mismatch`,
  `test_pid_is_serve_matches_cmdline`.
- NEW REQ-4: `TestOpenCodeChatHardening::test_blocking_respawn_single_retry`,
  `test_blocking_respawn_second_failure_returns_error`,
  `test_blocking_no_retry_on_http_error`,
  `test_blocking_respawn_reensure_failure`.
- MODIFIED (REQ-3 verification now gates kills): `TestServeHealth::
  test_recycles_old_serve`, `TestServeDrain._patch_drain`,
  `TestServeStability::test_config_drift_recycles_serve`,
  `TestServeStability::test_recycle_never_touches_unmatched_pid` — all patch
  `_pid_is_serve` → True for the fake pids they expect to be killed.

## Verification run

- `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` → **164 passed**
  (was 152 baseline at cycle start; +12 net new/updated cycle-4 coverage).

## Notes

- The working tree contains NO unrelated uncommitted code (HEAD moved during
  the cycle; cycles 6–9 work was committed upstream). The pre-existing stash
  `stash@{0}` (blocking-path respawn + pid-reuse guards, from an older base)
  was NOT touched — this cycle's implementation is fresher and based on HEAD.
- No commit was created by the apply phase; commit/delivery is handled by the
  verify/archive step per the single-pr delivery strategy.
