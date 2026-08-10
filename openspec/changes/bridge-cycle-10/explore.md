# Exploration — bridge-cycle-10 (adopt preserved in-flight work: blocking-path respawn, stale-pin self-heal, pid-reuse guards)

## 1. Change subject and provenance

- **Subject**: Land the preserved in-flight work from commit `6cd1204`
  ("in-flight: blocking-path respawn + pid-reuse guards (OUT OF cycle-8 scope; preserved, not part of delivery)")
  into the delivery tree. The work is currently **uncommitted** in the working tree:
  `git diff HEAD -- opencode_bridge.py tests/test_opencode_bridge.py` = **543 insertions / 99 deletions**
  (303 lines changed in `opencode_bridge.py`, 339 in `tests/test_opencode_bridge.py`), all tests green.
- **Adoption pattern**: identical to cycle-9 — preserved code lands as ONE change via the full SDD
  pipeline (proposal → design → tasks → apply → verify → archive), single PR
  (`delivery_strategy = single-pr`), 400-line review budget.
- **Three independent fixes**, adopted together (they were preserved as one unit and share one test
  harness, but each is separately testable):
  - **(A) Blocking-path bounded respawn** — `opencode_chat` now retries exactly once after a
    network-class failure mid-call.
  - **(B) Stale-pin self-heal** — `opencode_chat_stream` drops a pinned session id the serve no
    longer lists (only on a successful `/session/status` fetch).
  - **(C) PID-reuse guards** — recycle kills re-verify the pid's cmdline before `os.kill`, closing
    the TOCTOU race.

## 2. Current state verified

- **Branch**: `sdd/opencode-bridge-sdd-reliability/pr-5`; **HEAD**: `4dfaefe`
  (latest landed bridge fix: `30be86b` cycle-9 "wedge threshold 300s"; `08471ea` cycle-9
  replay-prevention/adopt landings).
- **Working tree**: `M opencode_bridge.py`, `M tests/test_opencode_bridge.py` (the preserved in-flight
  work); untracked `openspec/changes/bridge-cycle-10/` (+ cycle-4/6/7/8/9 artifact folders).
- **Bridge suite**: `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` →
  **164 passed** (153 cycle-9 baseline + 11 new in-flight tests). Confirmed green in this exploration.
- **Full suite**: **462 tests collected** (449+ at cycle-9 verify, plus the 11 new in-flight tests
  and other cycle additions) — consistent with cycle-9 verify claims.
- **Relevant spec**: `openspec/specs/opencode-bridge-blocking-path/` exists and is in scope for this
  adoption; `openspec/changes/bridge-cycle-10/specs/spec.md` is the cycle spec file.

## 3. Anatomy of the in-flight change

### Fix A — Blocking-path bounded respawn (`opencode_chat`)

- **`_opencode_chat_attempt(client, ...) -> tuple[str, bool]`** — `opencode_bridge.py:687`. One
  blocking attempt (create session → POST with permission polling → extract text). Returns
  `(result_string, network_failed)`; **never raises**. `network_failed=True` ONLY on the
  network-class paths:
  - message-POST transport failure caught inside the permission-polling loop
    (`except (httpx.HTTPError, OSError)`),
  - the outer attempt `except (httpx.HTTPError, OSError, ValueError)`.
  HTTP-status errors (e.g. session HTTP 503), missing session id, empty responses, and permission
  aborts all return `(message, False)` → **no retry**.
- **Retry gate** — `opencode_chat` (`opencode_bridge.py:663-684`): first `ensure_opencode_serve()`
  fails → `[OpenCode Bridge Failed: opencode serve not reachable.]`; else one
  `_opencode_chat_attempt`. If `network_failed` is falsy → return. If network-failed:
  `_force_recycle_serve("blocking-path respawn")` → re-`ensure_opencode_serve()` (failure returns the
  not-reachable error) → **retry the whole call EXACTLY ONCE**, no loop, second result returned
  unconditionally.
- Every attempt creates a **fresh session** (`POST /session` inside the attempt), so the retry can
  never conflict with a pinned/busy session from the failed attempt.

### Fix B — Stale-pin self-heal (`opencode_chat_stream` pin-resume block)

- **Block anchors**: `opencode_bridge.py:850-894` (pin-resume; `fetch_ok` recorded at `:862-871`).
- `/session/status` fetch now sets `fetch_ok = True` only on success
  (`except (httpx.HTTPError, ValueError)` → `st = None`, fetch_ok stays False).
- **Stale drop** (`:872-883`): if `fetch_ok and session_id not in st_map` → the pinned session is
  gone (serve recycled / session dropped); log line, `pending_permissions.pop(session_id, None)`,
  `session_map.pop(session_key, None)`, `session_id = None` → session starts fresh. No abort POST
  (aborting a nonexistent session adds noise).
- **Conservative keep**: a transport-error fetch (fetch_ok False) keeps the pin — avoids
  thrashing the pin when the serve is merely unreachable.
- Existing `st == "busy" and not just_approved_permission` abort logic
  (`:884-894`, `_abort_stream_session_best_effort`) is **unchanged**.

### Fix C — PID-reuse guards

- **`_cmdline_matches_serve(cmd: str, port: str) -> bool`** — `opencode_bridge.py:1931`. Shared
  NUL-normalized, tokenized matcher extracted from `_find_serve_pid` (the old space-form match
  never matched a NUL-separated `/proc/<pid>/cmdline`). **Exact-port-only**: `--port=<port>` token
  OR adjacent `("--port", port)` pair; requires both `opencode` and `serve` tokens; rejects prefix
  false-positives (`--port 189990` vs search `18999`, and `--port 18999` vs search `1899` — cycle-9
  bug).
- **`_pid_is_serve(pid: int, port: str) -> bool`** — `opencode_bridge.py:1955`. Reads
  `/proc/<pid>/cmdline` and applies the matcher; `OSError` → False; never raises. Guards the
  pid-reuse TOCTOU race (pid died and was OS-recycled between scan and kill).
- **`_find_serve_pid(port)`** — `opencode_bridge.py:1970`. Refactored onto the shared matcher;
  behavior unchanged.
- **Kill call sites (both guarded)**:
  - `_recycle_serve_if_low_memory` (`:1742`) — pid check at `:1763-1773`; mismatch → warning,
    skip kill, return; serve stays absent and next ensure respawns fresh; match → `os.kill(pid, 15)`
    then `_drain_serve_shutdown()` (`:1728`, bounded `_DRAIN_PROBES=4` × `_DRAIN_PROBE_S=0.25`,
    cycle-9).
  - `_force_recycle_serve(reason="long-lived call")` (`:1783`) — pid check at `:1796-1805`; same
    mismatch → warning + return; match → kill + drain. Callers: SDD-autonomous long-lived calls
    (`:519`) and the wedge-recovery path in the streaming loop (`:828`), plus the new blocking-path
    respawn (`:676`).
- **Hermeticity**: tests gate recycle behind the autouse `hermetic_serve` fixture
  (`tests/test_opencode_bridge.py:285`, `_noop_recycle` at `:308-311`), so no test ever kills a
  live serve; the new pid-guard unit tests patch `/proc` reads via `_patch_proc_cmdline`.

## 4. Test coverage inventory (11 new + patched)

New tests (all passing, part of the 164):

| Test | Class | Verifies |
|------|-------|----------|
| `test_blocking_respawn_single_retry` | `TestOpenCodeChatHardening` | network failure → one recycle+retry succeeds |
| `test_blocking_respawn_second_failure_returns_error` | `TestOpenCodeChatHardening` | retry failure returns its error, no infinite loop |
| `test_blocking_no_retry_on_http_error` | `TestOpenCodeChatHardening` | HTTP 503 → no retry (network_failed=False) |
| `test_blocking_respawn_reensure_failure` | `TestOpenCodeChatHardening` | re-ensure fails → not-reachable error |
| `test_stale_pin_dropped_when_status_lacks_id` | `TestStalePinSelfHeal` | pin dropped + fresh session on successful status fetch |
| `test_pin_kept_on_status_fetch_transport_error` | `TestStalePinSelfHeal` | transport-error fetch keeps pin conservatively |
| `test_recycle_kills_on_pid_match` / `test_recycle_skips_kill_on_pid_mismatch` | `TestServeHealth`/recycle | low-memory recycle honors `_pid_is_serve` |
| `test_force_recycle_kills_on_pid_match` / `test_force_recycle_skips_kill_on_pid_mismatch` | recycle/force-recycle | force-recycle honors `_pid_is_serve` |
| `test_pid_is_serve_matches_cmdline` | (unit, `~:2674`, via `_patch_proc_cmdline`) | exact-port match; prefix/suffix port mismatch → False; unreadable cmdline → False |

Patched existing tests (confirmation patches):
- `TestServeDrain` (`~:2792`) and `TestServeStability` (`~:2984`, `~:3062`) — monkeypatch
  `_pid_is_serve` → True so drain/age-based recycle paths still exercise the kill+drain flow.

## 5. Risks and edge cases

- **Bounded respawn vs. busy session**: each attempt creates a FRESH session, so the retry never
  reuses a possibly-busy session from the failed attempt — no pin conflict, no queueing behind a
  zombie. Exact-once retry means a genuinely dying serve costs at most one extra ensure+attempt,
  then an error string is returned (never a hang).
- **Conservative pin-keep on transport error**: if the serve is merely unreachable (not recycled),
  the pin is kept and the existing busy-abort logic still applies; if the serve was recycled but
  the status fetch also failed, the pin survives one request and is dropped on the next successful
  fetch (self-healing, no infinite retry).
- **TOCTOU guard**: `_pid_is_serve` re-reads `/proc/<pid>/cmdline` immediately before the kill;
  mismatch → skip kill → serve stays absent → next ensure respawns fresh. An OSError (pid already
  gone) is treated as mismatch, so the guard never raises and never signals a reused pid.
- **Respawn invokes `_force_recycle_serve`** — the same function used by SDD-autonomous mode and
  the config-drift gate; in tests it is hermetically stubbed (`hermetic_serve` autouse), so the new
  respawn path adds no live-process risk to the suite.
- **Wedge-detection interplay**: Fix A's recycle runs the bounded `_drain_serve_shutdown` before
  the re-ensure, so the respawn does not POST into a dying listener (same guarantee as cycle-9
  recycle paths).
- **Scope isolation**: none of the three fixes touches client message content, tool definitions, or
  sampling parameters (R1 glass-pipe rule unaffected); all changes are bridge-internal lifecycle
  behavior.

## 6. Recommendation

**Adopt and land as one change via the full SDD pipeline** (proposal → design → tasks → apply →
verify → archive), single PR, on the existing `sdd/opencode-bridge-sdd-reliability/pr-5` branch —
the same pattern cycle-9 used for preserved work. The working tree is green (164 bridge / 462
total collected), the three fixes are independently testable and documented with regression tests,
and the guard logic (exact-port matching, pid-reuse TOCTOU, bounded drain) is already partially
speced in `openspec/specs/opencode-bridge-blocking-path/`.

**Ready for Proposal: Yes.** Orchestrator should proceed to the proposal phase on
`bridge-cycle-10`; no further exploration needed. One note for the proposal: the change is already
fully written and tested, so proposal/design phases are confirm-and-document rather than
greenfield; the 400-line review budget applies to the ~640-line landed diff (543 insertions).
