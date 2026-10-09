# Tasks: Bridge Cycle 2 — Harden the OpenCode Serve Liveness Probe

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~60–90 |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | Single PR |
| Delivery strategy | single-pr |
| Chain strategy | pending |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: pending
400-line budget risk: Low

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | Transport-liveness probe + regressions | PR 1 (single) | `pytest tests/test_opencode_bridge.py -q` | N/A — hermetic `_FakeClient`, no real serve | `git revert` single commit; no config/DB/dependency impact |

## Phase 1: RED Regression Tests (TDD — tests first)

- [ ] 1.1 `tests/test_opencode_bridge.py` — Additive `_FakeClient` scripting (lines ~68–88): add `self.raise_timeout_on = ""` and `self.get_timeouts: list = []`; in `get()`, after the existing `raise_on == "get"` branch, raise `httpx.ReadTimeout("read timed out")` when `raise_timeout_on == "get"`; append `kwargs.get("timeout")` to `get_timeouts`. Existing tests untouched.
- [ ] 1.2 `tests/test_opencode_bridge.py` — In `TestIsRunning` (line ~394), add parameterized `test_alive_for_any_status` with `@pytest.mark.parametrize("status", [200, 302, 404, 500])`: set `fake_client.get_status = status`; assert `await is_opencode_serve_running() is True`.
- [ ] 1.3 `tests/test_opencode_bridge.py` — Add `test_not_running_on_read_timeout`: `fake_client.raise_timeout_on = "get"`; assert `await is_opencode_serve_running() is False`. (ConnectError test at ~400 stays; `_FakeResp` deliberately has no `raise_for_status`/body-read API, guarding those exclusions.)
- [ ] 1.4 `tests/test_opencode_bridge.py` — Add `test_probe_uses_three_second_timeout`: call probe; assert `fake_client.get_timeouts == [3.0]`.
- [ ] 1.5 `tests/test_opencode_bridge.py` — Add parameterized `test_no_spawn_when_404_500_alive` (`[404, 500]`): real probe + `ensure_opencode_serve()`, `monkeypatch` `_config_mtime` → `lambda: None`, replace `_spawn_serve` with async recorder; assert result is `True` and spawn calls == `[]`.
- [ ] 1.6 `tests/test_opencode_bridge.py` — Add `test_drain_bounded_to_four_probes`: patch `_serve_config_mtime` → 1000.0, `_config_mtime` → `lambda: 2000.0` (drift path), script `is_opencode_serve_running` as always-True, recorder for `asyncio.sleep`, noop `_force_recycle_serve`, async-recorder `_spawn_serve`; assert result `True`, ≤ 4 drain probe calls, exactly 4 sleeps of `0.25`.
- [ ] 1.7 Verify RED — Run `pytest tests/test_opencode_bridge.py -q`; expect tasks 1.2–1.6 to FAIL, existing 121 to pass.

## Phase 2: GREEN — Probe Edit

- [ ] 2.1 `opencode_bridge.py` (lines 406–413) — Keep buffered `.get()` and `(httpx.HTTPError, OSError)` boundary; change `timeout=5.0` → `timeout=3.0`; replace `return resp.status_code == 200` with `return True`; NO `raise_for_status()`, NO explicit body read; restate docstring: any received HTTP response = alive; `httpx.HTTPError`/`OSError` = down. Name/signature unchanged — call sites (spawn gate ~431, drain ~442, ~509) and `scripts/sdd_autonomous_cycle.py:165` inherit.

## Phase 3: Verification

- [ ] 3.1 `pytest tests/test_opencode_bridge.py -q` → all pass (121 + new).
- [ ] 3.2 `pytest tests/ -q` → full suite (~412 tests) green.
- [ ] 3.3 `git status` + `git diff --stat` → only `opencode_bridge.py` and `tests/test_opencode_bridge.py`; `opencode-serve-config.opencode.jsonc` shows no new diff (pre-existing local modifications stay untouched, uncommitted).
- [ ] 3.4 Commit single commit (e.g. `fix(bridge): any HTTP response means serve alive`); no PR (single-pr delivery).
