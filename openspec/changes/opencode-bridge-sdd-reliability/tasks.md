# Tasks: OpenCode Bridge SDD Reliability

## Review Workload Forecast

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

Estimated lines: 480–560. Delivery: auto-chain.

### Work Units

Test base: `pytest tests/test_opencode_bridge.py`.

| PR | Goal | Test cmd | Runtime harness | Rollback |
|----|------|----------|-----------------|----------|
| 1 | Relay + auto-allow (REQ-1/2) | `-k "permission or classify"` | Write gate → question | Revert `fix(bridge)` |
| 2 | /opencode parity (REQ-3) | `-k "opencode_command"` | curl `/opencode write` live serve | Revert `fix(routes)` |
| 3 | Hermeticity (test-infra REQ-2) | full run, live serve | N/A — hermeticity is the boundary | Revert `test(bridge)` |
| 4 | Stability + drift (REQ-4/5) | `-k "serve or drift or spawn"` | One driver cycle; fallback log | Env toggles off default |
| 5 | Drivers (REQ-6) | `python scripts/sdd_autonomous_cycle.py --help` | Run `--change my-change`; stall sim | Revert `chore(scripts)` |

## Phase 1: Relay + auto-allow (REQ-1/2)

- [x] 1.1 RED `_classify_permission_access` write/edit → "write" (F2, no cmd fallthrough)
- [x] 1.2 RED `test_permission_write_relayed_interactive`: write event → ("question", text)
- [x] 1.3 RED `test_autonomous_auto_allows_write` + `test_autonomous_auto_allows_git_commit`: POST "always", no question
- [x] 1.4 RED `test_interactive_does_not_auto_allow_write`
- [x] 1.5 `_RELAYED_PERMISSION_TYPES` (opencode_bridge.py) += write/edit
- [x] 1.6 `autonomous: bool = False` on `opencode_chat_stream` + `_opencode_task_response`; `_handle_opencode_request` → `autonomous=sdd`; drop timeout inference
- [x] 1.7 `_handle_permission_event(client, session_id, perm, pending_permissions, *, autonomous) -> Optional[str]` (None=handled, str=question)
- [x] 1.8 Fold 4 duplicated blocks (polling `_detect_pending_permission`, event-bus-timeout, `permission.updated`, completion `_resolve_pending_permission`) into helper
- [x] 1.9 F2: note GET /permission value for write/edit gates

## Phase 2: /opencode parity (REQ-3)

- [x] 2.1 RED `test_opencode_command_write_relay`: /opencode write → question (interactive) / auto-allow (autonomous)
- [x] 2.2 F3: `_handle_opencode_command(user_text, app)`; call site passes `request.app`
- [x] 2.3 `_handle_opencode_command` via `_opencode_task_response(client_stream=True, autonomous=False, pending_permissions=_pending_permissions_state(app))`; drop bare `opencode_chat`; shape preserved

## Phase 3: Test hermeticity (test-infra REQ-2)

- [x] 3.1 autouse `hermetic_serve` in tests/test_opencode_bridge.py: record `_find_serve_pid(port)`; noop `_recycle_serve_if_low_memory`/`_force_recycle_serve`; recorder-wrap `opencode_bridge.os.kill`; pure-unit helpers unaffected
- [x] 3.2 Teardown: pid alive, never killed, unchanged across full run (~15/17 via scope — F4)
- [x] 3.3 `@pytest.mark.real_recycle` on `TestServeHealth`: skip noops, keep guard (fake pid 12345)

## Phase 4: Stability + drift (REQ-4/5)

- [x] 4.1 F1 probe FIRST: reduced config via XDG loads before wiring candidate B; else A (`--pure`)
- [x] 4.2 constants.py: `OPENCODE_SERVE_PURE: bool = False`, `OPENCODE_SERVE_CONFIG_DIR`, `OPCODE_CONFIG_PATH`
- [x] 4.3 `ensure_opencode_serve`: serve_env XDG_CONFIG_HOME=serve-config, reduced plugins (keep fallback-mapped; drop skill-registry/review-result-artifacts/model-variants); `--pure` via 4.2
- [x] 4.4 mtime drift: stat `OPCODE_CONFIG_PATH`, newer → `_force_recycle_serve` + respawn, cache update; F5: `_serve_config_mtime` on app.state (like `pending_permissions`) or review-accepted
- [x] 4.5 RED `test_serve_spawn_pure_flag`, `test_config_drift_recycles_serve`, `test_recycle_never_touches_unmatched_pid` (pid 12345)
- [x] 4.6 One cycle: no wedge; fallback log no `fallback_chain_exhausted`, ≥1 `fallback_cycle_started→COMPLETED`; else A

## Phase 5: Driver scripts (REQ-6)

- [x] 5.1 Both drivers (scripts/sdd_autonomous_cycle.py, scripts/sdd_bridge_cycle.py): argparse `--change NAME`, drop "bridge-docs", glob on arg, `--desc` on bridge; no-delta > `STALL_S=180s` → break + non-zero exit

## Phase 6: REQ-7 (verify)

- [ ] 6.1 3 consecutive cycles; any wedge/crash fails
