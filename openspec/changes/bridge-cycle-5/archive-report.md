# Archive Report — bridge-cycle-5

- **Change**: `bridge-cycle-5` — Permission-aware hardening of the blocking escalation path (`opencode_chat`)
- **Archived**: 2026-08-09
- **Type**: Runtime reliability hardening (bridge blocking path + hermetic tests + AGENTS.md carve-out note)
- **Persistence mode**: both — filesystem authoritative; archive report at `openspec/changes/bridge-cycle-5/archive-report.md`; Engram handled by orchestrator
- **Verified commit**: `ea8dc0d` (`ea8dc0dfb866e61f6da7f5f79c4f71861f29a5ea`) on branch `sdd/opencode-bridge-sdd-reliability/pr-5`
- **Verdict**: PASS — all 7 requirements (REQ-1..REQ-7) / 11 scenarios, per `verify-report.md`

## Change Summary

The queue-worker cloud-escalation path (`opencode_chat`, the only blocking bridge
call, reached via `opencode_escalation` from `proxy.py`) previously did a single
blocking `POST /session/{id}/message` with a 600s timeout and NO cleanup: an agent
parked on a permission gate wedged the session busy for the full timeout, burned
the request, and left a busy zombie on the serve. This change gave the blocking
path the same permission hygiene as the streaming path — a deadline loop polling
`GET /permission` (`_BLOCKING_PERMISSION_POLL_S` = 5s) with READ auto-allow and
WRITE/git abort (headless worker never grants unprompted) — plus best-effort
session abort on every non-success exit, a keyword-only `autonomous` flag
(preserving auto-allow-all semantics for autonomous callers), and a conservative
escalation docstring. `AGENTS.md` gained the documented F5 carve-out for the
pre-existing `_serve_config_mtime` module-level scalar.

## Artifacts Inventory

| Artifact | Location | Status |
|---|---|---|
| Init context | `openspec/changes/bridge-cycle-5/init-context.md` | Committed |
| Proposal | `openspec/changes/bridge-cycle-5/proposal.md` | Committed |
| Design | `openspec/changes/bridge-cycle-5/design.md` | Committed |
| Tasks | `openspec/changes/bridge-cycle-5/tasks.md` | Committed |
| Spec (capability) | `openspec/specs/opencode-bridge-blocking-path/spec.md` | Committed |
| Verify report | `openspec/changes/bridge-cycle-5/verify-report.md` | Committed |
| Archive report | `openspec/changes/bridge-cycle-5/archive-report.md` | This file |
| Code | `opencode_bridge.py` (+106), `tests/test_opencode_bridge.py` (+141) | Committed (`ea8dc0d`) |
| Rules | `AGENTS.md` (+9, F5 carve-out) | Committed (`ea8dc0d`) |

## Delivery Shape

Single PR (no chaining): one implementation commit `ea8dc0d` + this archive
follow-up. ~355 changed lines (code + tests + docs + specs) — within the 400-line
review budget (forecast ~200 code/test lines; the delta above includes OpenSpec
artifacts). `opencode-serve-config.opencode.jsonc` was never touched.

## Verification Summary

- `.venv/bin/python -m pytest -q` → **427 passed** (full suite, 13.4s)
- `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` → **136 passed** (130 pre-existing + 6 new `TestOpenCodeChatHardening`)
- Compliance matrix: REQ-1..REQ-7 all **PASS** (deadline loop; READ auto-allow; WRITE abort; `autonomous` param; cleanup on non-success exits; conservative escalation; regression coverage)
- Scope guard: commit touches only `opencode_bridge.py`, `tests/test_opencode_bridge.py`, `AGENTS.md`, and this change's OpenSpec artifacts

## Residual Risks

- **Accepted**: escalation requests whose agent needs a WRITE/git permission abort with an error instead of burning the timeout — conservative by design (no unprompted grants on the headless worker path); the interactive `/opencode` stream path still relays questions.
- **Environmental note**: concurrent SDD cycles (4 and 6) shared the worktree during this cycle; cycle-6's in-flight unstaged edits contaminated the final `gga` review scope (it flagged those, not this change; this change's content passed review twice). Future cycles should serialize per-file ownership or use worktrees.

## Rollback Path

`git revert ea8dc0d` — function-level edits confined to `opencode_bridge.py` + tests; prior blocking semantics (single blocking POST, no abort) restored exactly; no config/DB/dependency/schema impact. The AGENTS.md carve-out note is inert and may stay.

## Close Verdict

**CLOSED** — cycle delivered its single-PR scope, all requirements verified, suite green, artifacts persisted in both Engram and OpenSpec.
