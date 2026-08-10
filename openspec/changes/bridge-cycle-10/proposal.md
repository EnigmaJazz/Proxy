# Proposal: Bridge Cycle 10 — Blocking-Path Bounded Respawn, Stale-Pin Self-Heal, PID-Reuse Guards (Adoption of Preserved In-Flight Work)

**One-line summary**: Land the preserved in-flight work from commit `6cd1204` (three independent bridge-lifecycle fixes: blocking-path bounded respawn, stale-pin self-heal, pid-reuse guards) as ONE adopt-and-land change — 543 insertions / 99 deletions across `opencode_bridge.py` + `tests/test_opencode_bridge.py`, already green in the working tree, single PR, `size:exception` pre-approved.

## Intent (Why)

Three fixes, preserved as one unit from cycle-8 scope (shared test harness, separately testable):

- **Fix A — Blocking-path bounded respawn**: `opencode_chat` (`:663-684` retry gate) refactored onto `_opencode_chat_attempt` (`:687`) returning `(result, network_failed)` — True ONLY on network-class paths (message-POST transport failure inside the permission loop, outer `httpx.HTTPError/OSError/ValueError`). On network failure: `_force_recycle_serve("blocking-path respawn")` → re-ensure → retry the WHOLE call exactly once, no loop. HTTP-status errors, missing session id, empty response, and write-permission aborts never retry. Rationale: the queue-worker/blocking path previously surfaced a hard network error and died whenever the serve recycled or died mid-call (OOM kill, tool-runner wedge); the streaming path already had the stale-pin self-heal — this mirrors that resilience.
- **Fix B — Stale-pin self-heal** (`opencode_chat_stream` pin-resume `:850-894`): a SUCCESSFUL `/session/status` fetch whose map no longer lists the pinned id drops the pin (pop pending_permissions + session_map, fresh `/session` POST); a transport-error fetch keeps the pin conservatively. Rationale: a stale pin previously POSTed to a nonexistent session and failed every retry until timeout.
- **Fix C — PID-reuse guards**: new `_cmdline_matches_serve` (`:1931`, shared NUL-normalized exact-port matcher extracted from `_find_serve_pid` — `--port=<port>` token OR adjacent `("--port", port)` pair, requires opencode+serve tokens, rejects prefix false-positives) and `_pid_is_serve` (`:1955`, reads `/proc/<pid>/cmdline`, OSError → False, never raises). Both recycle kills (`:1763-1773`, `:1796-1805`) verify `_pid_is_serve` BEFORE `os.kill`; mismatch → warning, skip kill, serve stays absent, next ensure respawns fresh. Rationale: TOCTOU — the scanned pid can die and be OS-recycled before the kill; the guard makes a wrong-pid kill impossible.

**Outcome**: blocking calls survive serve churn with one bounded respawn; resumed streams self-heal stale pins immediately; recycle kills can never hit a reused pid — all bridge-internal lifecycle behavior, R1 glass-pipe unaffected (no client content, tools, or sampling changes).

## Scope

### In Scope

- `opencode_bridge.py`: Fix A attempt-refactor + single-retry gate; Fix B stale-pin drop; Fix C shared matcher + `_pid_is_serve` + both guarded kill sites.
- `tests/test_opencode_bridge.py`: 11 new hermetic tests (4 respawn, 2 stale-pin, 4 recycle/pid-guard, 1 cmdline unit via `_patch_proc_cmdline` ~`:2674`) + `_pid_is_serve` confirmation patches in TestServeDrain/TestServeStability; existing 153 cycle-9 tests untouched.

### Out of Scope

- routes.py, proxy.py, config, systemd; event-bus behavior; wedge/timeout policy; streaming replay-prevention (cycle-9, already landed); new live-serve integration tests; scripts.

## Exploration Summary

Verified (explore phase, read-only, 2026-08-09) against the CURRENT working tree on `sdd/opencode-bridge-sdd-reliability/pr-5`: `git diff HEAD` = **543 insertions / 99 deletions** (303 lines `opencode_bridge.py`, 339 lines tests); suite green — **164 bridge passed** (153 cycle-9 baseline + 11 new), **462 total collected**. Anchors confirmed: retry gate `:663-684`, `_opencode_chat_attempt` `:687`, pin-resume `:850-894` (fetch_ok `:862-871`, stale drop `:872-883`), `_cmdline_matches_serve` `:1931`, `_pid_is_serve` `:1955`, guarded kills `:1763-1773` / `:1796-1805`, `_find_serve_pid` `:1970` (behavior unchanged). Hermeticity: autouse `hermetic_serve` (`:285`) / `_noop_recycle` (`:308-311`) keeps the suite kill-free.

**Persistence note**: Engram persistence is unavailable in this runtime; the durable store is this OpenSpec change folder (filesystem) — same as cycle-9.

## Assumptions & Edge Cases

- Every blocking attempt creates a FRESH session → the retry never reuses a busy/pinned session; exact-once retry means a dying serve costs at most one extra ensure+attempt, then an error string (never a hang).
- Pin kept on status transport error: a recycled serve whose fetch also failed self-heals on the next successful fetch (no infinite retry).
- `_pid_is_serve` OSError treated as mismatch → guard never raises, never signals a reused pid.
- Respawn recycles through the bounded `_drain_serve_shutdown` (4 × 0.25s) before re-ensure — no POST into a dying listener.

## Capabilities

### New Capabilities

None — no new capability specs.

### Modified Capabilities

- `opencode-serve-lifecycle`: Fix C lands the pid-verify requirement — kill primitives MUST re-read `/proc/<pid>/cmdline` (shared exact-port matcher) immediately before `os.kill`, skipping on mismatch/unreadable (REQ-3). Fixes A and B are likewise already speced there as REQ-4 (one bounded respawn) and REQ-2 (stale-pin self-heal). Cycle-10 records the adoption as a delta spec in this change folder.
- `opencode-bridge-polling-replay-prevention`: unaffected — cycle-9 already landed; no requirement changes.

## Approach

Full SDD pipeline (proposal → design → tasks → apply → verify → archive), adopt-and-land as ONE change on `pr-5` — the cycle-9 pattern: **apply = verify the already-preserved working tree** (164 bridge / 462 total green) and land via commits; single PR (`delivery_strategy = single-pr`).

**Delivery decision (recorded)**: the ~642 changed lines (543+/99−) exceed the 400-line review budget; per the user-supplied session preflight this is the maintainer-approved **`size:exception`** for this preserved in-flight unit — identical to how cycle-9 landed ~700 lines as one change. Not an open question.

Rollback: revert the landing commits (one logical change, two files). Success criteria: 164 bridge tests green, full suite green, landed commits on `pr-5`, delta spec recorded.
