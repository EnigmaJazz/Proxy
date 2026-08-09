# Tasks: Bridge Cycle 5 — Permission-Aware Hardening of the Blocking Escalation Path

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~200 |
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
| 1 | Deadline loop + abort helper + `autonomous` + regressions | PR 1 (single) | `pytest tests/test_opencode_bridge.py -q` | N/A — hermetic `_FakeClient`, no live serve | `git revert` single commit; edits confined to `opencode_bridge.py` + tests; no config/DB/dependency impact |

## Context

- Strict TDD enforced (repo `strict_tdd: true`): Phase 1 tests land RED before any Phase 2 edit.
- Reuse existing machinery verbatim (design D2/D3): `_detect_pending_permission` (:369), `_handle_permission_event` (:298), `_classify_permission_access` (:230) — no new relay/classification logic.
- Do NOT touch the streaming path (`opencode_chat_stream`, :602), routes.py, proxy.py, `_abort_zombie_sessions` (:1625), or `opencode-serve-config.opencode.jsonc`.
- `httpx.ReadTimeout`/`TimeoutException`/`ConnectError` are all `httpx.HTTPError` subclasses — covered by the existing catch tuple.

## Phase 1: RED Regression Tests (TDD — tests first)

- [ ] 1.1 `tests/test_opencode_bridge.py` `_FakeClient.post()` (:95-104) — Add `permission_records: list[dict[str, Any]] = []` to `__init__` (:60-72).
- [ ] 1.2 `tests/test_opencode_bridge.py` `_FakeClient.post()` — Add timeout branch AFTER the `/session` return (:101): `if self.raise_timeout_on == "post" and "/message" in url: raise httpx.ReadTimeout("read timed out")` — session create succeeds, message POST raises.
- [ ] 1.3 `tests/test_opencode_bridge.py` `_FakeClient.get()` (:80-93) — Add before final fallback (:93): `if url.endswith("/permission"): return _FakeResp(200, self.permission_records)` (matches `_detect_pending_permission` GET, opencode_bridge.py:393). Existing `/message/` branch (:87-89) stays.
- [ ] 1.4 New `TestOpenCodeChatHardening` class after `TestOpenCodeChat` (:359-397) — `pytest_asyncio` fixtures, `monkeypatch` `_BLOCKING_PERMISSION_POLL_S` → 0.01. Add `test_timeout_aborts_session`: `raise_timeout_on = "post"`; `await opencode_chat("hi", timeout=10.0)` → result starts `[OpenCode Bridge Network Error:`; `any(u.endswith("/abort") for u, _ in client.post_calls)`.
- [ ] 1.5 Same class — `test_message_503_aborts_session`: `fake_client.message_status = 503` → result == `[OpenCode Bridge Error: message HTTP 503]`; abort recorded.
- [ ] 1.6 Same class — `test_read_permission_auto_allowed`: `permission_records = [{"id": "perm_1", "sessionID": "ses_0001", "permission": "external_directory", "patterns": ["cat /etc/os-release"], "tool": {"messageID": "m1", "callID": "c1"}}]`; one text `message_parts`; result == extracted text; `post_calls` has `/permissions/perm_1` with `{"response": "always"}`; no abort.
- [ ] 1.7 Same class — `test_write_permission_aborts`: WRITE record (`"permission": "write"`, `patterns: ["rm -rf /x"]`, same tool shape) → result contains `write permission` and `[OpenCode Bridge Error`; abort recorded; no `"always"` POST.
- [ ] 1.8 Same class — `test_autonomous_write_auto_allowed`: same WRITE record, `autonomous=True` → result == extracted text; `"always"` posted; no abort.
- [ ] 1.9 Same class — `test_success_no_permissions_unchanged`: no records → result identical to `test_success_collects_text_parts` (:361); no abort, no `/permissions/` POST.
- [ ] 1.10 Verify RED — `pytest tests/test_opencode_bridge.py -q` → 1.4–1.9 FAIL, existing 121 pass.

## Phase 2: GREEN — opencode_bridge.py Edit

- [ ] 2.1 `opencode_bridge.py` (near :121) — Add `_BLOCKING_PERMISSION_POLL_S: float = 5.0` next to `_WEDGE_CHECK_INTERVAL_S`.
- [ ] 2.2 `opencode_bridge.py` (near `_post_permission_response`, :275) — Add `async def _abort_session_best_effort(client: httpx.AsyncClient, session_id: str) -> None`: `POST {OPENCODE_SERVE_URL}/session/{id}/abort`, `timeout=10.0`; catch `(httpx.HTTPError, OSError)` → pass; docstring "never raises".
- [ ] 2.3 `opencode_chat` signature (:537-545) — Add keyword-only `autonomous: bool = False`; extend docstring: permission polling, READ auto-allow, WRITE abort, best-effort cleanup.
- [ ] 2.4 `opencode_chat` (:581-585) — Replace blocking message POST with `post_task = asyncio.create_task(client.post(...))` (same URL/payload/timeout).
- [ ] 2.5 `opencode_chat` — Poller loop scaffold: `last_poll = time.monotonic()`; `while True`: if `post_task.done()` → `resp = post_task.result()` (raises httpx errors here) and `break`; else `await asyncio.sleep(min(0.25, _BLOCKING_PERMISSION_POLL_S))`.
- [ ] 2.6 `opencode_chat` — Permission poll inside loop: every `_BLOCKING_PERMISSION_POLL_S` (tracked via `last_poll`) → `await _detect_pending_permission(client, session_id)`; if not None → `await _handle_permission_event(client, session_id, perm, {}, autonomous=autonomous)`.
- [ ] 2.7 `opencode_chat` — Question branch: if handler returns a question → `await _abort_session_best_effort(client, session_id)`; return `"[OpenCode Bridge Error: agent requested write permission — headless escalation cannot relay questions; session aborted.]"`.
- [ ] 2.8 `opencode_chat` — Wrap loop in `try`/`except (httpx.HTTPError, OSError, ValueError)`/`finally`: on exception → abort best-effort, return `[OpenCode Bridge Network Error: {exc}]` (covers ReadTimeout/TimeoutException/ConnectError); `finally` cancels `post_task` if not done.
- [ ] 2.9 `opencode_chat` (:586-587) — Message `resp.status_code != 200` branch: `await _abort_session_best_effort(client, session_id)` before returning `[OpenCode Bridge Error: message HTTP ...]`. Session-create errors (:563-567) unchanged — no id, no abort.
- [ ] 2.10 `opencode_escalation` docstring (:1087) — Note: headless queue-worker path runs `autonomous=False` — READ auto-allowed, WRITE aborts, no unprompted grants. No behavioral change.
- [ ] 2.11 Verify GREEN — `pytest tests/test_opencode_bridge.py -q` → all pass (121 existing + 6 new).

## Phase 3: Verification

- [ ] 3.1 `.venv/bin/python -m pytest tests/ -q` → full suite (~412 tests) green.
- [ ] 3.2 `git status` → only `opencode_bridge.py`, `tests/test_opencode_bridge.py`, `openspec/changes/bridge-cycle-5/`, `openspec/specs/opencode-bridge-blocking-path/` (new files untracked).
- [ ] 3.3 `git diff --stat` → `opencode-serve-config.opencode.jsonc` shows no new diff (untouched by this change).
- [ ] 3.4 Commit single commit `fix(bridge): harden the blocking escalation path with permission polling and session cleanup` — stage ONLY intended files; do NOT stage `openspec/changes/bridge-cycle-4/` (pre-existing untracked leftover from a prior interrupted cycle).
