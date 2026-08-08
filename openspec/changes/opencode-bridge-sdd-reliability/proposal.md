# Proposal: OpenCode Bridge SDD Reliability

## Intent

Make full SDD cycles through the opencode bridge run reliably end-to-end: fix the permission-relay wedge, make bridge tests hermetic, stabilize the serve.

## Why Now

Design/apply phases write files; with `write/edit: "ask"` live since Aug 7 23:55, write gates are IGNORED by the relay (opencode_bridge.py:188 covers only `external_directory`/`bash`), the tool parks, and the 120s wedge detector kills the session. Tests also kill the production serve mid-run (proven 08:46 today, pid 2262). Autonomous cycles are non-viable.

## Scope

### In Scope
- **Write/edit gate relay** (fix 1): extend relay set + filters at `:375`/`:1003`; auto-allow in SDD-autonomous mode, relay in interactive mode.
- **Test hermeticity** (fix 2): noop `_recycle_serve_if_low_memory` (pattern at `tests/test_opencode_bridge.py:1395`) for ALL real-stream tests; assert `os.kill` never fires.
- **Serve stability** (fix 4): `--pure` vs plugin reduction (`skill-registry.ts`, `review-result-artifacts.ts`, `model-variants.ts`, `opencode-rate-limit-fallback-mapped`), gated by one full autonomous cycle.
- **/opencode parity** (fix 5): route `routes.py:2521` through `opencode_chat_stream` or document the limitation.
- **Driver scripts** (fix 6): parameterize change name (`sdd_bridge_cycle.py:27`, `sdd_autonomous_cycle.py`); define stall handling.
- **Config-drift** (fix 7): document/auto-detect `opencode.json` hot edits → serve recycle.
- **Baseline spec** `opencode-bridge`.

### Out of Scope
- opencode upstream patches; `glass-pipe-followups`; user TUI config beyond serve needs; git ask-rules in TUI mode.

## Capabilities

### New Capabilities
- `opencode-bridge`: permission relay (write/edit/git), serve lifecycle + stability mode, /opencode parity, SDD-autonomous policy, config-drift handling.

### Modified Capabilities
- `test-infrastructure`: bridge tests MUST be hermetic — never kill a live opencode serve.

## Approach

1. **Correctness layer** — relay extension + test hermeticity (fixes 1, 2, 5, 6).
2. **Stability layer** — `--pure` vs plugin reduction, chosen by one full autonomous cycle per candidate; the mode completing without wedge/crash wins. Decision in design.md.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `opencode_bridge.py:188,352-375,1003` | Modified | write/edit gate relay |
| `tests/test_opencode_bridge.py` | Modified | hermetic recycle, all stream tests |
| `routes.py:2501-2521` | Modified | /opencode via stream path |
| `scripts/sdd_*.py` | Modified | parameterized change name |
| serve config + `openspec/specs/opencode-bridge` | New | stability mode; baseline spec |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Auto-allow write in autonomous mode reduces safety | Med | Serve-scoped only; interactive mode keeps relay |
| `--pure` loses rate-limit-fallback replay | Med | Empirical gate; prefer plugin reduction if it passes |
| Git ask stalls in autonomous mode | Med | Policy decision in spec |

## Rollback Plan

Each fix is independently revertible: relay set + filters (one commit), test patch (one file), serve mode flag (env var). Re-run one cycle to confirm.

## Dependencies

- opencode v1.18.15 serve permission/plugin behavior (empirically validated, not assumed)

## Success Criteria

- [ ] 3 consecutive full autonomous SDD cycles complete end-to-end (proposal → archive) with zero wedge/crash
- [ ] Bridge suite (36 `opencode_chat_stream` uses) green WITHOUT killing the live serve — `os.kill` never fires
- [ ] Write/edit gates relay or auto-allow per policy on every bridge path, including `/opencode`
- [ ] Serve pid unchanged across a full test run
- [ ] Git commit/push in autonomous mode auto-allowed or relayed — no stall
