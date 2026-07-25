# Tasks: Professional as Default

## Change summary

Route auto CHAT/TOOL/CODE traffic to resident Professional, add exact profiles, preserve Professional across empty queues, and retain explicit specialist/R1/R7 behavior.

## Review Workload Forecast

| Field | Value |
|---|---|
| Estimated changed lines | ~350–380 |
| 400-line budget risk | Low (near ceiling) |
| Chained PRs recommended | No |
| Suggested split | One PR; work-unit commits for routing, profiles/scanner, queue, tests |
| Delivery strategy | interactive / ask-on-risk |
| Chain strategy | pending (no chain required) |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: pending
400-line budget risk: Low

## PR strategy recommendation

Use one focused PR with tests kept beside each behavior. If implementation exceeds 400 authored changed lines, stop and split into PR1 routing+profiles, PR2 scanner+queue, and PR3 integration tests; do not use `size:exception` without maintainer approval.

## Tasks

### Phase 1: Routing

- [x] **Task 1.1: Add routing RED coverage.** **Description:** Test REQ-1/2/3 for free, occupied, resident-Professional, and mid-tool states. **Files:** `tests/test_professional_default.py`. **Acceptance:** Tests fail against current CHAT/TOOL destinations and capture expected safe fallback behavior. **Phase:** 1. Routing.
- [x] **Task 1.2: Update ROUTE_MAP and special blocks.** **Description:** Change CHAT/TOOL/CODE defaults and replace CHAT/TOOL special branches while preserving Lifeboat contention and tool-history safety. **Files:** `routing.py`. **Acceptance:** Focused routing tests pass; specialists remain unchanged.

### Phase 2: Profiles and scanner

- [x] **Task 2.1: Add exact Professional profile rows.** **Description:** Add `professional/chat` and `professional/code` rows with REQ-4 values and no default bucket. **Files:** `config/model_profiles.yaml`. **Acceptance:** Loader resolves exact rows and all four values match the spec.
- [x] **Task 2.2: Emit multiple scanner intents.** **Description:** Convert `_MODEL_INTENTS` to deterministic tuples, pass intent into `build_profile_entry`, and emit both Professional rows. **Files:** `tools/sync_model_profiles.py`, `tests/glass_pipe_test.py`. **Acceptance:** Synthetic Professional generation emits chat and code rows; `--check` reports in-sync.

### Phase 3: Queue lifecycle

- [x] **Task 3.1: Add queue-gate RED tests.** **Description:** Test REQ-7/8: Professional active skips unload; specialist active unloads and hotswap remains possible. **Files:** `tests/test_professional_default.py`, `tests/conftest.py`. **Acceptance:** Tests fail before the gate and record unload calls.
- [x] **Task 3.2: Gate idle cleanup.** **Description:** Extract/test `_cleanup_idle_heavy`, reconcile an externally active Professional, fail safe on probe `OSError`, and unload specialists normally. **Files:** `proxy.py`. **Acceptance:** Queue cleanup preserves Professional and clears specialist state.

### Phase 4: Integration and regression tests

- [x] **Task 4.1: Pin payload and explicit-model regressions.** **Description:** Cover Scenarios 1–6, including chat/code profiles, TOOL mid-flow, Professional client-wins, chatter/worker opt-in, and specialist opt-in. **Files:** `tests/test_professional_default.py`, `tests/test_client_named_model.py`. **Acceptance:** REQ-1–6 pass and R19 tests remain green.
- [x] **Task 4.2: Verify complete change.** **Description:** Run scanner drift checks and the full async suite; inspect diff budget and generated YAML determinism. **Files:** all changed files. **Acceptance:** `pytest -q` passes, scanner `--check` exits 0, no out-of-scope frontdesk or R1/R7 changes.

## Test plan

Add routing, profile-resolution, queue-gate, and payload tests for REQ-1 through REQ-8. Update scanner generation/drift and committed-profile assertions in `tests/glass_pipe_test.py`; extend `_NoOpSystemd` call recording only as required. Run `pytest -q` and `python tools/sync_model_profiles.py --check`.
