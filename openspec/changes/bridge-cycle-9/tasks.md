# Tasks: Bridge Cycle 9 — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills

Apply order: Fix 1 code → Fix 1 tests → Fix 2 code → Fix 2 tests → Fix 3 code → Fix 3 tests → verification. Fix 2 and Fix 3 are independent of Fix 1. All line anchors verified against source 2026-08-09.

## Fix 1 — Code

- [x] **1. `_seed_resumed_session_state` helper** — `opencode_bridge.py`, before `_poll_session_deltas` (:1541); per design §2.1: best-effort `client.get(f"{OPENCODE_SERVE_URL}/session/{session_id}/message", timeout=10.0)`; non-200 → return; parse inside the try with `body = resp.json()` and `if not isinstance(body, list): return` (design §2.1 escape hatch — dict bodies would raise on `m.get`); assistant msgs → `user_mids.add(id)`; text/reasoning parts with text → `text_lens[pid]=len(text)`; tool parts → `tool_state[pid]=status`; `tool=="question"` → `seen_question_pids.add(pid)`; catch `(httpx.HTTPError, OSError, ValueError)` → return, never raises. Acceptance: task 7 green.
- [x] **2. `resumed` flag + seed call site** — `opencode_chat_stream`: `resumed = session_id is not None` before `if not session_id:` (:828); declare `seen_question_pids: set[str] = set()` with per-call state (:855-859); after the session block, before the payload dict (:843): `if resumed: await _seed_resumed_session_state(client, session_id, user_mids, text_lens, tool_state, seen_question_pids)`. Acceptance: fresh sessions never seed (task 10).
- [x] **3. `_yield_part_deltas` kwarg + question short-circuit** — :1244: append `seen_question_pids: Optional[set[str]] = None`; top of `if name == "question":` (:1285), before the fetch-retry loop (:1322): `if seen_question_pids and pid in seen_question_pids: return`. Acceptance: seeded pid suppressed; `test_question_yields_text_and_stops` (:1829) stays green.
- [x] **4. `_poll_session_deltas` kwarg + pass-through** — :1541: append `seen_question_pids: Optional[set[str]] = None`; pass into `_yield_part_deltas` at :1563-1565. Acceptance: positional unit callers (:1193, :1227, :1251) unchanged.
- [x] **5. Both call sites pass the set** — polling fallback (:957-959) and event-bus (:1202-1204) append `seen_question_pids`. Acceptance: `py_compile` clean.

## Fix 1 — Tests (tests/test_opencode_bridge.py, additive)

- [x] **6. `_SeedPollClient` (D7)** — next to `_PollClient` (:1151): subclass with `seed_messages=[]`, `seed_status=200`, `raise_on_seed=False`; first list GET (`url.endswith(f"/session/{self.session_id}/message")`) serves seed (raise / non-200 / messages), later list GETs delegate to `_PollClient.get` (poll_messages). Acceptance: existing `_PollClient` tests (:1178-1255) green.
- [x] **7. Seed unit tests** — `TestSeedResumedSessionState`: (a) scripted assistant msg with text `prt_old_text` + reasoning + tool `completed` + question `prt_q` → assert `text_lens`, `tool_state`, `seen_question_pids == {"prt_q"}`, mid ∈ `user_mids`; (b) `test_seed_failure_never_raises`: `httpx.ConnectError`-raising client and `seed_status=500` both return silently.
- [x] **8. E2E resumed replay (Scenario-1)** — `TestResumedSessionPollingReplay.test_resumed_session_never_replays_history`: `smap={"conv-1":"ses_0001"}`, `_SeedPollClient` `seed_messages=[old msg: text+reasoning+completed tool+question]`, `poll_messages=[old msg + new msg with new text pid]`, `stream_lines=[]`, `ensure_opencode_serve` → True; assert: old text absent, no "🧠/✅/⚠️", no question delta, new text streamed, ends without "[OpenCode Bridge Network Error".
- [x] **9. Seed-failure degradation (Scenario-2)** — `seed_status=500`, `poll_messages=[]`, `stream_lines=[]` → stream completes without raising; repeat with `raise_on_seed=True` → identical.
- [x] **10. Fresh session no seed (Scenario-3)** — `smap={}`, plain `_FakeClient`, `stream_lines=[]`; from `client.calls` assert first `/session/ses_0001/message` list-GET index > `prompt_async` POST index (no pre-prompt seed GET).

## Fix 2 — Code

- [x] **11. `_find_serve_pid` token matching** — :1747-1768: keep /proc scan, NUL normalization (:1761), try/except; replace the :1762 condition with the §3 matcher: `tokens = cmd.replace("\x00"," ").split()`; `continue` unless `"opencode" in tokens and "serve" in tokens`; `if f"--port={port}" in tokens: return int(entry)`; else adjacent `("--port", port)` pair → `return int(entry)`. Exact equality only. Acceptance: task 12 + existing NUL test (:1997-2042) green.

## Fix 2 — Tests

- [x] **12. Exact-match tests** — `TestFindServePidExactMatch`, replicate `_fake_listdir`/`_fake_open`/local `_FakeProc` (:2015-2036): (a) `opencode\0serve\0--port=18999` ↔ "18999" → 4242; (b) `--port\0189990` ↔ "18999" → None (longer advertised value rejected); (c) `--port\018999` ↔ "1899" → None (shorter search rejected). Existing NUL test untouched and green.

## Fix 3 — Code

- [x] **13. `_DRAIN_PROBES = 4`, `_DRAIN_PROBE_S = 0.25` + `_drain_serve_shutdown`** — insert before `_recycle_serve_if_low_memory` (:1577): `for _ in range(_DRAIN_PROBES): if not await is_opencode_serve_running(): return; await asyncio.sleep(_DRAIN_PROBE_S)` (design §4.1); never raises.
- [x] **14. Drain after both kills** — `_recycle_serve_if_low_memory`: inside `if pid:` after the kill try/except (:1600-1603); `_force_recycle_serve`: inside `if pid:` after `os.kill(pid, 15)` (:1624): add `await _drain_serve_shutdown()`. Hermetic guard (:268-300) keeps nooping both helpers in normal tests.

## Fix 3 — Tests

- [x] **15. Drain tests, `_force_recycle_serve`** — `TestServeDrain` `@pytest.mark.real_recycle` (pattern :1956-1976): monkeypatch `_find_serve_pid`→424242, `os.kill`→recorder, `_DRAIN_PROBE_S=0.01`; scripted `is_opencode_serve_running`: (a) True×2 then False → 3 calls (2 up + 1 down check), early return; (b) always True → exactly 4 calls, no raise (the §4.1 loop calls once per probe; the §5.6 "(5 calls)" parenthetical conflicts with the loop and is superseded).
- [x] **16. Same, `_recycle_serve_if_low_memory`** — same setup plus `_serve_health` → `lambda port: (True, 0.0)` (signature verified :1694: `(pressure, elapsed)` → pressure branch, kill at :1601); assert 3-call early break and 4-call exhaustion, no raise.

## Verification

- [x] **17. Run the suite** — `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` (acceptance: 140 pre-existing bridge tests + all new green), then `.venv/bin/python -m pytest tests/ -q` (full suite), then `.venv/bin/python -m py_compile opencode_bridge.py tests/test_opencode_bridge.py` (syntax sanity). Report exact bridge and full-suite pass counts in the apply summary; 0 failures/errors.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~281 (Fix1 ~168, Fix2 ~52, Fix3 ~61) |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | Single PR |
| Delivery strategy | single-pr |

Decision needed before apply: No
Chained PRs recommended: No
400-line budget risk: Low
