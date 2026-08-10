# Verify Report: Bridge Cycle 4 — Serve-Lifecycle Hardening + Blocking-Path Respawn

Status: PASS (2026-08-09, autonomous session; verify executed inline by the
orchestrator — sub-agent delegation was interrupted, fallback rule applied).

## Evidence

### 1. Focused suite
`.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` → **164 passed**.
Cycle-4 coverage (12 new/updated tests):
- REQ-2: `TestStalePinSelfHeal` (2 tests) — pin dropped on successful status
  fetch lacking the id; pin kept on transport-error fetch.
- REQ-3: `TestServeHealth` pid-match/mismatch pairs for both kill primitives
  (4 tests) + `test_pid_is_serve_matches_cmdline` (exact-port + unreadable
  cmdline).
- REQ-4: `TestOpenCodeChatHardening` respawn tests (4 tests) — single retry,
  double failure, no-retry-on-HTTP-error, re-ensure failure.
- Updated pre-existing kill-asserting tests to patch `_pid_is_serve` → True
  (`test_recycles_old_serve`, `TestServeDrain._patch_drain`,
  `TestServeStability` drift tests) — required by REQ-3's verify-before-kill.

### 2. Full suite
`.venv/bin/python -m pytest tests/ -q` → **460 passed** (whole repo, incl.
routes/queue/async strict mode). No regressions.

### 3. Requirement-by-requirement

| Req | Code evidence | Test evidence |
|-----|---------------|---------------|
| REQ-1 recycle precedes ensure | `opencode_chat_stream` calls `_recycle_serve_if_low_memory()` before `ensure_opencode_serve()` (HEAD, verify-only) | existing `test_recycle_before_ensure_no_failure` |
| REQ-2 stale-pin self-heal | `fetch_ok` flag; drop on `fetch_ok and session_id not in st_map`; transport error keeps pin | `TestStalePinSelfHeal` |
| REQ-3 verify-before-kill | `_cmdline_matches_serve` + `_pid_is_serve`; both primitives skip kill on mismatch; `_find_serve_pid` refactored byte-identically | `TestServeHealth` pid pairs + cmdline test |
| REQ-4 bounded respawn | `opencode_chat` wrapper + `_opencode_chat_attempt`; single recycle+re-ensure+retry on network-class failure only | `TestOpenCodeChatHardening` respawn tests |

### 4. Scope check
- `git diff --stat`: `opencode_bridge.py` (cycle-4 edits), `tests/test_opencode_bridge.py` (+339, cycle-4 tests), plus `routes.py` (+32) which is an UNRELATED concurrent change by another session ("LOCAL-APPLY BYPASS") that appeared mid-cycle — explicitly excluded from this change's delivery and commit.
- OpenSpec artifacts: `openspec/changes/bridge-cycle-4/` (spec/design/tasks/apply-progress) + durable `openspec/specs/opencode-serve-lifecycle/`.
- `opencode-serve-config.opencode.jsonc` untouched. No routes.py/proxy.py/scripts changes from this cycle.

## Residual risks

| Risk | Severity | Note |
|------|----------|------|
| Recycle-before-ensure residual collateral damage for concurrent sessions | Low | Pre-existing policy, documented in design; unchanged by this cycle |
| `_pid_is_serve` misses a serve due to cmdline variance → recycle skipped | Low | Same matcher as `_find_serve_pid` (proven); mismatch self-heals via next ensure |
| Blocking retry doubles escalation latency on a genuinely-dead serve | Low | Bounded: exactly one retry, network-class errors only |
| Concurrent `routes.py` change uncommitted in the tree | — | Environmental; must not be swept into cycle-4's commit |

## Conclusion

All four requirements implemented and green. Recommend: single commit of
`opencode_bridge.py` + `tests/test_opencode_bridge.py` + cycle-4 OpenSpec
artifacts, then archive the change.
