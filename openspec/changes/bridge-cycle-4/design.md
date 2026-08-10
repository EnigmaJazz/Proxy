# Design: Bridge Cycle 4 — Serve-Lifecycle Hardening + Blocking-Path Respawn

## Overview

Cycle-4 delivers the serve-lifecycle fixes scoped by the cycle-3 proposal plus
the deferred blocking-path respawn.  Four requirements (spec.md / the durable
copy under `openspec/specs/opencode-serve-lifecycle/spec.md`):

- **REQ-1 — Stream recycle precedes ensure**: the low-memory/age recycle must
  run BEFORE `ensure_opencode_serve()` so the same request's ensure respawns
  in-line.
- **REQ-2 — Stale pinned sessions self-heal**: a SUCCESSFUL `/session/status`
  fetch lacking the pinned id drops the pin; a transport-error fetch keeps it.
- **REQ-3 — Kill primitives verify the selected PID**: `/proc/<pid>/cmdline`
  is re-checked against the serve binary before `os.kill` in both kill
  primitives; a mismatched (reused) pid is never killed.
- **REQ-4 — Blocking calls receive one bounded respawn retry**:
  `opencode_chat` recycles + re-ensures + retries exactly once on a
  network-class failure after a successful first ensure; HTTP-status errors
  never retry.

**Status note (2026-08-09, verified against the working tree)**: REQ-1 is
ALREADY APPLIED in HEAD (commit `4195fd1`/`3030329` range, concurrent cycle-6
apply): `opencode_chat_stream` now calls `_recycle_serve_if_low_memory()`
before `ensure_opencode_serve()`, and `TestStreamExitHygiene::test_recycle_before_ensure_no_failure`
covers it.  REQ-2, REQ-3, and REQ-4 are still open — this design covers the
open work and treats REQ-1 as verify-only (no code change).

## Key Design Decisions

| Decision | Choice | Alternatives considered |
|---|---|---|
| D1 | REQ-2: add a `fetch_ok` success flag in the resume block; on success with `session_id not in st_map` → pop pin + pending-permission and start fresh (no abort POST — the session is gone; aborting a nonexistent session adds noise). | Reusing the busy-abort trio for stale pins — rejected: the stale case means the session no longer exists on the serve, so the abort POST is pointless. |
| D2 | REQ-2: transport-error fetch (httpx.HTTPError/ValueError) keeps the pin, exactly like today — conservative, no behavior change on network blips. | Dropping the pin on any fetch failure — rejected (could abort live sessions during transient network errors). |
| D3 | REQ-3: extract the shared matcher `_cmdline_matches_serve(cmd: str, port: str) -> bool` (NUL-normalize + `"opencode" in cmd and "serve" in cmd and f"--port {port}" in cmd`), refactor `_find_serve_pid` to use it, and add `_pid_is_serve(pid: int, port: str) -> bool` reading `/proc/<pid>/cmdline`. Both kill primitives call `_pid_is_serve` before `os.kill` and skip the kill on mismatch. | Inline re-reads in both primitives — rejected (matcher drift; the NUL-normalization regression already burned this suite once). |
| D4 | REQ-3: the verify is a sync helper called via `await asyncio.to_thread(...)` from both async primitives (mirrors how `_find_serve_pid` is already called). Verify failure (missing/read-error cmdline) → treat as mismatch → skip kill; the serve stays absent and the next ensure respawns (self-heals). | Raising on unreadable cmdline — rejected: never-raises contract of both primitives must hold. |
| D5 | REQ-4: extract the whole blocking attempt (session create → permission-poll POST loop → text extraction) into `_opencode_chat_attempt(client, ...) -> tuple[str, bool]` returning `(text, network_failed)`. `opencode_chat` runs it; on `network_failed` after a successful first ensure → `_force_recycle_serve("blocking-path respawn")` → re-ensure → run the attempt exactly once more. | Retry inside the poll loop — rejected: the serve died; the session is garbage; only a fresh session on a fresh serve can succeed. A sentinel-exception design — rejected: `opencode_chat` must never raise; a `(text, bool)` return keeps that contract trivially. |
| D6 | REQ-4: `network_failed=True` only for the network-class paths (`httpx.HTTPError`/`OSError`/`ValueError` from the poll loop, `post_task.result()`, or session-create transport errors). HTTP-status errors (`session HTTP != 200`, `message HTTP != 200`), missing id, empty response, and the WRITE-permission abort all return `network_failed=False` → no retry. | Retrying on any failure — rejected: doubles escalation latency for deterministic 5xx and permission aborts. |
| D7 | REQ-4: keep one `httpx.AsyncClient` across both attempts (created in `opencode_chat`), reused for the retry — httpx clients are safe to reuse after a transport error. The retry attempt creates a brand-new session on the (fresh) serve. | Fresh client per attempt — rejected: unnecessary churn; the test harness's `_FakeClient` already tolerates sequential reuse. |

## Detailed Changes

### 1. `opencode_bridge.py` — REQ-2: stale-pin self-heal (resume block of `opencode_chat_stream`, ~:790-812)

Current code:

```python
try:
    st_resp = await client.get(
        f"{OPENCODE_SERVE_URL}/session/status", timeout=10.0,
    )
    st_map = st_resp.json()
    st = (st_map.get(session_id) or {}).get("type")
except (httpx.HTTPError, ValueError):
    st = None
if st == "busy" and not just_approved_permission:
```

New code:

```python
fetch_ok = False
try:
    st_resp = await client.get(
        f"{OPENCODE_SERVE_URL}/session/status", timeout=10.0,
    )
    st_map = st_resp.json()
    fetch_ok = True
    st = (st_map.get(session_id) or {}).get("type")
except (httpx.HTTPError, ValueError):
    st = None
if fetch_ok and session_id not in st_map:
    # Stale pin: the serve was recycled (or the session is gone), so a
    # successful status fetch no longer lists the pinned id.  Posting to
    # it would fail every retry — drop the pin and start fresh.  A
    # transport-error fetch keeps the pin conservatively (below).
    logger.info(
        "pinned session %s no longer on serve — dropping pin and starting fresh",
        session_id[:16],
    )
    pending_permissions.pop(session_id, None)
    if session_map is not None and session_key:
        session_map.pop(session_key, None)
    session_id = None
elif st == "busy" and not just_approved_permission:
    # ... unchanged existing busy-abort block ...
```

Note: `st_map` is only referenced after `fetch_ok` was set, so the `not in`
test cannot see an empty dict from a failed fetch.  The `just_approved_permission`
carve-out does not apply here: a session that just got an approval but is no
longer listed is gone — a fresh session is the only viable continuation.

### 2. `opencode_bridge.py` — REQ-3: shared cmdline matcher + `_pid_is_serve`

Refactor `_find_serve_pid` (~:1747) to use a new module-level helper, and add
the single-pid verifier right before it:

```python
def _cmdline_matches_serve(cmd: str, port: str) -> bool:
    """True when a normalized /proc cmdline belongs to the opencode serve
    bound to ``port``.  /proc/<pid>/cmdline separates argv with NUL bytes,
    so NULs are normalized to spaces before matching (regression 2026-08-07)."""
    cmd = cmd.replace("\x00", " ")
    return "opencode" in cmd and "serve" in cmd and f"--port {port}" in cmd


def _pid_is_serve(pid: int, port: str) -> bool:
    """Re-verify ``pid`` still belongs to the opencode serve before a kill.

    Guards the pid-reuse TOCTOU race: the pid found by ``_find_serve_pid``
    may have died and been recycled by the OS between the scan and the
    kill.  Unreadable/missing cmdline counts as mismatch (never raises)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().decode("utf-8", "ignore")
        return _cmdline_matches_serve(cmd, port)
    except OSError:
        return False
```

`_find_serve_pid` body becomes a loop over `/proc` entries that replaces its
inline NUL-normalize + match with `_cmdline_matches_serve(cmd, port)` —
behavior byte-identical.

### 3. `opencode_bridge.py` — REQ-3: verify before kill in BOTH primitives

`_recycle_serve_if_low_memory` (~:1577) — after `pid = await asyncio.to_thread(_find_serve_pid, port)`:

```python
if pid:
    if not await asyncio.to_thread(_pid_is_serve, pid, port):
        # Pid-reuse race guard: this pid no longer belongs to the serve.
        logger.warning("skipping recycle: pid %s no longer matches the serve", pid)
        return
    try:
        os.kill(pid, 15)
    except (OSError, ProcessLookupError):
        pass
```

`_force_recycle_serve` (~:1608) — same guard before its `os.kill(pid, 15)`:

```python
if pid:
    if not await asyncio.to_thread(_pid_is_serve, pid, port):
        logger.warning("skipping force-recycle: pid %s no longer matches the serve", pid)
        return
    logger.warning("Forcing opencode serve recycle (%s)", reason)
    os.kill(pid, 15)
```

### 4. `opencode_bridge.py` — REQ-4: bounded blocking respawn

Split `opencode_chat` (~:630-757) into a thin public wrapper + the attempt:

```python
async def opencode_chat(
    user_text: str,
    *,
    agent: str = OPENCODE_AGENT,
    model_id: Optional[str] = None,
    provider_id: str = "kinver",
    timeout: float = OPENCODE_SERVE_TIMEOUT,
    system_prompt: str = _BRIDGE_SYSTEM_PROMPT,
    autonomous: bool = False,
) -> str:
    """... (existing docstring, plus:) A network-class failure after a
    successful first ensure triggers one bounded respawn: recycle the
    serve, re-ensure, retry exactly once.  HTTP-status errors and
    permission aborts never retry."""
    if not await ensure_opencode_serve():
        return "[OpenCode Bridge Failed: opencode serve not reachable.]"
    async with httpx.AsyncClient() as client:
        text, network_failed = await _opencode_chat_attempt(
            client, user_text, agent=agent, model_id=model_id,
            provider_id=provider_id, timeout=timeout,
            system_prompt=system_prompt, autonomous=autonomous,
        )
        if not network_failed:
            return text
        # The serve died mid-call (network-class failure after a successful
        # ensure): recycle once, re-ensure, and retry the whole call once.
        await _force_recycle_serve("blocking-path respawn")
        if not await ensure_opencode_serve():
            return "[OpenCode Bridge Failed: opencode serve not reachable.]"
        text, _ = await _opencode_chat_attempt(
            client, user_text, agent=agent, model_id=model_id,
            provider_id=provider_id, timeout=timeout,
            system_prompt=system_prompt, autonomous=autonomous,
        )
        return text


async def _opencode_chat_attempt(
    client: httpx.AsyncClient,
    user_text: str,
    *,
    agent: str,
    model_id: Optional[str],
    provider_id: str,
    timeout: float,
    system_prompt: str,
    autonomous: bool,
) -> tuple[str, bool]:
    """One blocking attempt: create session, POST with permission polling,
    extract text.  Returns (result_string, network_failed).  Never raises."""
    try:
        # 1. Create a fresh session (unchanged).
        resp = await client.post(...)
        if resp.status_code != 200:
            return f"[OpenCode Bridge Error: session HTTP {resp.status_code}]", False
        session_id = resp.json().get("id")
        if not session_id:
            return "[OpenCode Bridge Error: no session id returned.]", False
        # 2. POST with permission polling (unchanged, cycle-5 machinery).
        ... post_task / poll loop; network failures return
            (f"[OpenCode Bridge Network Error: {exc}]", True)
        # 3. Status/text handling (unchanged); message HTTP != 200 returns
        #    (error, False); empty response returns (error, False).
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return f"[OpenCode Bridge Network Error: {str(exc)}]", True
```

The existing body moves into the attempt verbatim, with every return turned
into a `(value, False)` tuple except the three network-class paths, which
become `(value, True)`.  No error-string text changes.

### 5. `tests/test_opencode_bridge.py` — additive scripting

- `_FakeClient.__init__`: `self.status_map: Optional[dict[str, Any]] = None`
  (REQ-2) and `self.fail_post_times: int = 0` (REQ-4).
- `_FakeClient.get`: for `/session/status`, return
  `_FakeResp(200, self.status_map if self.status_map is not None else {self.session_id: {"type": "idle"}})`.
- `_FakeClient.post`: before the normal dispatch, if `self.fail_post_times > 0`
  and `"/message" in url`: decrement and `raise httpx.ConnectError("conn refused")`.

### 6. New regression tests

REQ-2 (in/near `TestStreamExitHygiene`):
1. `test_stale_pin_dropped_when_status_lacks_id` — `status_map = {}`; pinned
   `smap = {"conv": "ses_0001"}`; stream completes; assert `post_calls`
   contains a fresh `/session` POST, `smap` holds the NEW id, no `/abort` fired.
2. `test_pin_kept_on_status_fetch_transport_error` — `raise_timeout_on = "get"`
   (status fetch raises); assert NO fresh `/session` POST (pin kept), result
   still extracts text from the pinned-session message POST.

REQ-3 (near `TestServeHealth`, plain hermetic — no live serve touched):
3. `test_recycle_skips_kill_on_pid_mismatch` — `_serve_health` pressure,
   `_find_serve_pid` → 12345, `_pid_is_serve` → False; assert kill recorder empty.
4. `test_recycle_kills_on_pid_match` — same, `_pid_is_serve` → True; assert
   killed == [12345].
5. `test_force_recycle_skips_kill_on_pid_mismatch` / `test_force_recycle_kills_on_pid_match`
   — same pair for `_force_recycle_serve`.
6. `test_pid_is_serve_matches_cmdline` — fake `/proc/<pid>/cmdline` open (same
   `_FakeProc` pattern as `test_find_serve_pid_matches_nul_separated_cmdline`);
   matching and non-matching cmdlines.

REQ-4 (in/near `TestOpenCodeChatHardening`):
7. `test_blocking_respawn_single_retry` — `fail_post_times = 1`;
   `ensure_opencode_serve` recorder (True, True); `_force_recycle_serve`
   recorder; result == extracted text; exactly 2 message POSTs; recycle called
   once; ensure called twice.
8. `test_blocking_respawn_second_failure_returns_error` — `fail_post_times = 2`;
   result starts `[OpenCode Bridge Network Error:`; exactly 1 recycle; exactly
   2 message POSTs.
9. `test_blocking_no_retry_on_http_error` — `message_status = 503`; result ==
   `[OpenCode Bridge Error: message HTTP 503]`; recycle recorder EMPTY; exactly
   1 message POST.
10. `test_blocking_respawn_reensure_failure` — `fail_post_times = 1`;
    ensure returns True then False; result ==
    `[OpenCode Bridge Failed: opencode serve not reachable.]`; exactly 1 recycle.

Modified existing tests: `TestServeHealth::test_recycles_old_serve` must also
patch `_pid_is_serve` → True (it already asserts the kill).

## Verification Plan

1. `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` — 140
   existing + ~10 new green (hermetic; no live serve, no real /proc).
2. `.venv/bin/python -m pytest tests/ -q` — full suite green.
3. `git diff --stat` — touches only `opencode_bridge.py`,
   `tests/test_opencode_bridge.py`, and this change's OpenSpec artifacts
   (`openspec/changes/bridge-cycle-4/`, `openspec/specs/opencode-serve-lifecycle/`).
4. Commit: `fix(bridge): drop stale pins, verify pids before kill, respawn blocking calls`.

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Stale-pin drop aborts a genuinely-live session on odd status data | Low | Drop only on SUCCESSFUL fetch lacking the id; transport errors keep the pin |
| `_pid_is_serve` misses the serve due to cmdline variance and blocks recycling | Low | Same matcher already proven by `_find_serve_pid` (NUL-normalized, port-scoped); mismatch self-heals via next ensure |
| Blocking retry doubles escalation latency | Low | Bounded: network-class failures only, exactly one retry; HTTP-status errors never retry |
| `test_recycles_old_serve` breaks (real-recycle class) | Certain without update | Update it to patch `_pid_is_serve` → True |

## Rollback

`git revert` the single commit — function-level edits confined to
`opencode_bridge.py` + tests; prior pin, kill, and retry semantics restored
exactly; no config/DB/schema/dependency impact.
