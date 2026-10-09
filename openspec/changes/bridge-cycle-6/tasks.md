# Tasks: Bridge Cycle 6 — Streaming-Path Error-Exit Hygiene + Serve-Recycle Ordering

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~165–200 |
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
| 1 | RED tests + GREEN edits (helper, 3 leak exits, recycle reorder, DRY) | PR 1 (single) | `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` | N/A — hermetic `_FakeClient`/`_stream_events()`; `hermetic_serve` autouse guards live serve | `git revert` commit(s); edits confined to `opencode_bridge.py` + tests; no config/DB impact |

## Context

- Strict TDD (`strict_tdd: true`): Phase 1 RED before any Phase 2 edit.
- Never kill a real serve — `hermetic_serve` autouse guards it.
- Do NOT touch `opencode_chat` blocking path, routes.py, proxy.py, config, systemd, `_recycle_serve_if_low_memory` internals, error strings.
- Helper name distinct from cycle-5 `_abort_session_best_effort` (merge-safe).
- AGENTS.md: no `print()`, type hints, no bare `except`, conventional commits, no `Co-Authored-By`.

## Phase 1: RED Regression Tests (TDD — tests first)

- [x] 1.1 `_FakeClient.__init__` — add `prompt_status: int = 204`, `fail_get_after: Optional[int] = None`, `_get_count: int = 0`.
- [x] 1.2 `_FakeClient.post()` — add `if "prompt_async" in url: return _FakeResp(self.prompt_status, {})`.
- [x] 1.3 `_FakeClient.get()` — after `raise_timeout_on` check: `_get_count += 1; if fail_get_after is not None and _get_count > fail_get_after: raise httpx.ReadTimeout("read timed out")`.
- [x] 1.4 New `TestStreamExitHygiene` after `TestOpenCodeChatHardening` — `@pytest.mark.asyncio`; monkeypatch `ensure_opencode_serve` → `_running` (True), `httpx.AsyncClient` → `_FakeClient`; fresh `session_map={}` / `session_key="conv"` / `pending={}` (NOT shared `PP`).
- [x] 1.5 `test_stream_network_error_aborts_and_drops_pin` — `stream_lines=[]`, `fail_get_after=1` → delta starts `[OpenCode Bridge Network Error:`; `/abort` in `post_calls`; `session_map == {}`; `pending == {}`.
- [x] 1.6 `test_message_updated_error_aborts_and_drops_pin` — `stream_lines=[_evt("message.updated", sessionID="ses_0001", info={"id":"msg_a","role":"assistant","error":"boom"})]` → `deltas == [("status", "[OpenCode Bridge Error: boom]")]`; abort recorded; pin dropped; pending popped.
- [x] 1.7 `test_prompt_non_204_drops_pin` — `prompt_status=500`, `stream_lines=[]` → `deltas == [("status", "[OpenCode Bridge Error: prompt HTTP 500]")]`; abort recorded; pin popped; pending popped.
- [x] 1.8 `test_recycle_before_ensure_no_failure` — recorder list; monkeypatch `_recycle_serve_if_low_memory` → `"recycle"`, `ensure_opencode_serve` → `"ensure"`; `stream_lines=_stream_events()` → `order == ["recycle","ensure"]`; text deltas "Created file."/"Done."; no error delta; no abort.
- [x] 1.9 Verify RED — `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q -x` → 1.5–1.8 FAIL; existing 136 pass.

## Phase 2: GREEN — opencode_bridge.py Edits

- [x] 2.1 Helper after `_abort_session_best_effort` (~:325) — `async def _abort_stream_session_best_effort(client, session_id, *, session_map=None, session_key=None, pending_permissions=None) -> None`: falsy `session_id` → return; call cycle-5 helper; pop `pending_permissions[session_id]`, `session_map[session_key]` when provided; never raises.
- [x] 2.2 Recycle-before-ensure — move `await _recycle_serve_if_low_memory()` from the try (:723) to after autonomous force-recycle (:709-711), before `ensure_opencode_serve()` (:712); autonomous path unchanged.
- [x] 2.3 Prompt non-204 (:806-810) — trio helper call, then `yield ("status", f"[OpenCode Bridge Error: prompt HTTP {async_resp.status_code}]")`; return.
- [x] 2.4 `message.updated` error (:1132-1136) — insert trio call before existing `yield ("status", f"[OpenCode Bridge Error: {info['error']}]")` + return.
- [x] 2.5 Outer except (:1154-1155) — `if session_id:` → trio call; yield `[OpenCode Bridge Network Error: {str(exc)}]` verbatim.
- [x] 2.6 DRY pinned-busy (:754-763) — inline abort + 2 pops → one helper call; keep `session_id = None` after; yield verbatim.
- [x] 2.7 DRY timeout (:831-843) + polling timeout (:880-891) — straight helper replacement, yields verbatim.
- [x] 2.8 DRY wedge (:1013-1035, :1058-1078) — straight helper replacement, yields verbatim.
- [x] 2.9 Verify GREEN — `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` → 140 pass (136 + 4 new).

## Phase 3: Verification

- [x] 3.1 `.venv/bin/python -m pytest tests/ -q` → full suite green (`hermetic_serve` asserts no live serve touched).
- [x] 3.2 `git status` → only `opencode_bridge.py`, `tests/test_opencode_bridge.py`, `openspec/changes/bridge-cycle-6/`, `openspec/specs/opencode-bridge-stream-exit-hygiene/` (new files untracked).
- [x] 3.3 Commit per phase boundary (single PR): RED `test(bridge): stream exit hygiene regression tests`; GREEN `fix(bridge): stream exit hygiene — cleanup trio on every error exit`, `fix(bridge): recycle serve before ensure on the stream path`. Stage only intended files; no `Co-Authored-By`.
