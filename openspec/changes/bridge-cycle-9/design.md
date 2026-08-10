# Design: Bridge Cycle 9 — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills

Status: ACCEPTED (autonomous cycle, orchestrator inline design phase — the sdd-design sub-agent's
execution was interrupted by the serve's tool runner; written inline by the orchestrator from
explore-phase anchors, all line numbers re-verified against source).

## 1. Design Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D1 | `seen_question_pids` threading | New per-call state `set[str]`; passed as `Optional[set[str]] = None` keyword arg through `_poll_session_deltas` and `_yield_part_deltas` | Positional callers (existing unit tests call `_poll_session_deltas(client, id, set(), {}, {})`) stay green; a None set means "no history seeded" → zero behavior change for fresh sessions / direct unit callers. |
| D2 | Seed timing | Best-effort `GET /session/{id}/message` AFTER pin resolution, BEFORE `client.stream` + `prompt_async` | The event bus is fire-and-forget; seeding must happen before the prompt POST so the bus/polling phases see seeded state. Resumed-only: fresh sessions (new `POST /session`) must not seed. |
| D3 | `user_mids` seed semantics | Seed with the RESUMED session's existing ASSISTANT message ids | Matches cycle-7 design semantics: `user_mids` is a seen-parts filter at `_poll_session_deltas` (:1561) — the name is a historical misnomer; it holds assistant ids, and polling skips parts whose messageID is already in it. |
| D4 | Question suppression point | Short-circuit inside `_yield_part_deltas` question branch, BEFORE the fetch-retry loop (currently at :1322) | Both the polling pass-through (:1563-1565) and the event-bus call (:1202-1204) share the branch; suppressing at the branch root avoids the 12s retry race and the yield for any seeded pid. |
| D5 | PID matching | Tokenize the NUL-normalized cmdline (`cmd.replace("\x00", " ").split()`); require `opencode` and `serve` in tokens; match exact adjacent `("--port", port)` pair OR exact single token `f"--port={port}"` | Exact equality only — fixes the `--port=18999` never-matches and `--port 189990` prefix-false-positive bugs while keeping the existing NUL space-form test green (tokens are identical). |
| D6 | Drain shape | New helper `_drain_serve_shutdown()` + module constants `_DRAIN_PROBES = 4`, `_DRAIN_PROBE_S = 0.25`; called after the verified kill in BOTH recycle helpers | Constants make the bounded loop testable (tests patch them small instead of sleeping 1s). Mirrors the config-drift drain at :516-520. Never raises. |
| D7 | Test seeding client | Extend `_PollClient` with `seed_messages`/`seed_status`; the FIRST `/session/{id}/message` list GET serves `seed_messages` (when set), subsequent ones serve `poll_messages` | The seed GET and the poll GET hit the same endpoint. Existing `_PollClient` tests never set `seed_messages` → behavior unchanged. Faithful to reality: at seed time the new turn has not been POSTed yet, so seed content is history-only. |
| D8 | Drain tests | `@pytest.mark.real_recycle` (skips the hermetic noop patches, keeps the guard), monkeypatched `_find_serve_pid` → fake pid, scripted `is_opencode_serve_running` | The hermetic autouse guard noops both recycle helpers entirely; only real_recycle lets the helper body (including the drain) execute, and a fake pid keeps the live serve untouched. |

## 2. Fix 1 — Resumed-session polling replay prevention

### 2.1 New helper

```python
async def _seed_resumed_session_state(
    client: httpx.AsyncClient,
    session_id: str,
    user_mids: set[str],
    text_lens: dict[str, int],
    tool_state: dict[str, str],
    seen_question_pids: set[str],
) -> None:
    """Best-effort seed of per-call part state for a RESUMED pinned session.

    The per-call text_lens/tool_state/user_mids are normally fed only by
    live events; when the /event bus closes, the polling fallback re-scans
    the FULL message list and re-yields history (old text, thinking,
    tool chunks, and a stale question that stops the stream).  Seeding the
    existing part state (text lengths, tool states, assistant message ids,
    question part ids) once before the prompt lets the polling deltas
    suppress everything that existed BEFORE this request.  Never raises:
    a failed seed degrades to the current replay behavior.
    """
    try:
        resp = await client.get(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/message", timeout=10.0,
        )
        if resp.status_code != 200:
            return
        for m in resp.json():
            role = (m.get("info") or {}).get("role")
            if role == "assistant":
                mid = m.get("id")
                if mid:
                    user_mids.add(mid)
                for p in m.get("parts") or []:
                    pid = str(p.get("id") or "")
                    ptype = p.get("type")
                    if ptype in ("text", "reasoning"):
                        text = str(p.get("text") or "")
                        if text:
                            text_lens[pid] = len(text)
                    elif ptype == "tool":
                        status = str((p.get("state") or {}).get("status") or "")
                        if status:
                            tool_state[pid] = status
                        if p.get("tool") == "question" and pid:
                            seen_question_pids.add(pid)
    except (httpx.HTTPError, OSError, ValueError):
        return
```

Notes:
- Non-empty text only: an empty part keeps the default `text_lens.get(pid, 0) == 0` (no behavior change).
- `tool_state[pid] = status` mirrors the live update pattern at :1424-1425 so a historical completed/error part never re-emits its "✅/⚠️" chunk (the live branch yields only on state CHANGE from the seeded value).
- Malformed entries (`resp.json()` not a list, missing keys) must not raise: `for m in resp.json()` over a dict iterates keys (harmless); guard `role == "assistant"` and `m.get` everywhere. If needed, wrap the json parse in the same try — the existing pattern (`_poll_session_deltas` :1551-1571) treats non-200 and malformed bodies as "no deltas".

### 2.2 Call site (opencode_chat_stream)

- In `opencode_chat_stream`, introduce `resumed = session_id is not None` right before the `if not session_id:` block (~:828) — i.e. the session_id came from the session map (pin) rather than a fresh POST.
- After the `if not session_id:` block and BEFORE the payload/`client.stream` (~:843), add:

```python
            seen_question_pids: set[str] = set()
            if resumed:
                # Seed part state for the resumed conversation so the
                # polling fallback never replays history or re-surfaces a
                # stale question that kills the stream.  Best-effort.
                await _seed_resumed_session_state(
                    client, session_id, user_mids, text_lens,
                    tool_state, seen_question_pids,
                )
```

- `seen_question_pids` is declared next to the existing per-call state (`user_mids`/`asst_mid`/`text_lens`/`tool_state`/`pending_done`, ~:855-859).

### 2.3 Threading

- `_yield_part_deltas(part, text_lens, tool_state, session_id, client, seen_question_pids: Optional[set[str]] = None)` — signature extended with a trailing keyword arg.
- `_poll_session_deltas(client, session_id, user_mids, text_lens, tool_state, seen_question_pids: Optional[set[str]] = None)` — same.
- Question branch short-circuit (in `_yield_part_deltas`, at the top of the `if name == "question":` block, BEFORE the fetch-retry loop at :1322):

```python
            if seen_question_pids and pid in seen_question_pids:
                return  # seeded history — suppress the stale question
```

- Both call sites pass the set:
  - polling fallback ~:957-959: `_poll_session_deltas(client, session_id, user_mids, text_lens, tool_state, seen_question_pids)`
  - event-bus ~:1202-1204: `_yield_part_deltas(part, text_lens, tool_state, session_id, client, seen_question_pids)`

Note on the event-bus site: live events never carry historical part ids, so the extra argument is a consistency guard (a seeded pid can never re-yield if the serve ever re-emits an update for a pre-prompt part).

## 3. Fix 2 — Exact serve-PID port matching

Replace the match in `_find_serve_pid` (:1747-1768). Keep the /proc scan, NUL normalization, and try/except resilience; change only the matching:

```python
                tokens = cmd.replace("\x00", " ").split()
                if "opencode" not in tokens or "serve" not in tokens:
                    continue
                if f"--port={port}" in tokens:
                    return int(entry)
                for i, tok in enumerate(tokens):
                    if (
                        tok == "--port" and i + 1 < len(tokens)
                        and tokens[i + 1] == port
                    ):
                        return int(entry)
```

- Exact equality only: `--port=18999` matches search "18999"; `--port 189990` does NOT match; `--port 1899` does NOT match search "18999".
- Existing NUL space-form test (`tests/test_opencode_bridge.py` ~:1997-2042) stays green: `opencode\0serve\0--port\018999` → tokens `["opencode", "serve", "--port", "18999"]` → adjacent-pair match.
- Downstream users (`_serve_start_epoch_ms` :1494, `_serve_health` :1694, recycle helpers, wedge stale-part guard :1475) are unchanged and benefit automatically.

## 4. Fix 3 — Bounded drain after recycle kills

### 4.1 New module constants + helper

```python
_DRAIN_PROBES = 4
_DRAIN_PROBE_S = 0.25


async def _drain_serve_shutdown() -> None:
    """Boundedly wait for the killed serve listener to stop.

    is_opencode_serve_running() answers True mid-SIGTERM, so
    ensure_opencode_serve would otherwise skip the respawn and the stream
    POSTs into a dying listener.  Poll a few times and return — never
    raises, never exceeds the budget.
    """
    for _ in range(_DRAIN_PROBES):
        if not await is_opencode_serve_running():
            return
        await asyncio.sleep(_DRAIN_PROBE_S)
```

Placement: near the recycle helpers (before `_recycle_serve_if_low_memory` :1577).

### 4.2 Call sites

- `_recycle_serve_if_low_memory` (~:1600-1603): inside `if pid:` after the kill try/except, add `await _drain_serve_shutdown()`.
- `_force_recycle_serve` (~:1621-1624): inside `if pid:` after `os.kill(pid, 15)`, add `await _drain_serve_shutdown()`.

Both helpers keep their "never raises" contract (the drain never raises). The hermetic autouse guard (`hermetic_serve` :268-300) noops both helpers entirely in normal tests, so no test hits the real drain unless marked `real_recycle`.

## 5. Test Approach (all hermetic, no live serve)

New cases in `tests/test_opencode_bridge.py`, additive only:

### 5.1 Seeding helper unit test

`TestSeedResumedSessionState.test_seeds_text_lens_tool_state_question_pids`:
- Scripted client returning one assistant message with: text part (`id=prt_old_text`, long text), reasoning part, tool part (`state.status=completed`), question part (`tool=question`, `state.input.question="old?"`).
- Assert: `text_lens["prt_old_text"] == len(text)`, `tool_state` holds "completed" for the tool pid, `seen_question_pids == {"prt_q"}`, `user_mids` contains the assistant message id.
- Second case `test_seed_failure_never_raises`: client raising `httpx.ConnectError` / returning 500 → helper returns silently.

### 5.2 End-to-end resumed replay suppression

`TestResumedSessionPollingReplay.test_resumed_session_never_replays_history`:
- Use a `_SeedPollClient` (D7): `seed_messages = [old assistant msg with text + reasoning + completed tool + question parts]`, `poll_messages = [same old msg + NEW assistant msg with a new text part (new pid)]`.
- Pin: `smap = {"conv-1": "ses_0001"}`; `stream_lines = []` (immediate bus closure → polling fallback).
- Drive `opencode_chat_stream("answer", session_map=smap, session_key="conv-1", ...)`; assert:
  - the old text is NOT in the streamed deltas,
  - no "🧠 thinking…", no "✅ … done", no "⚠️ … failed",
  - NO question delta (no stale-question stop),
  - the NEW text part's delta IS streamed,
  - stream ends normally (no "[OpenCode Bridge Network Error").

### 5.3 Seed-failure degradation

`TestResumedSessionPollingReplay.test_seed_failure_degrades_to_current_behavior`:
- `_SeedPollClient` with `seed_status = 500`, `poll_messages = []`, empty `stream_lines` → drive the resumed stream; assert: no exception, stream completes; also assert a second variant where the seed GET raises (client raising on first list GET) behaves identically.

### 5.4 Fresh session never seeds

`TestResumedSessionPollingReplay.test_fresh_session_does_not_seed`:
- `smap = {}`, `_FakeClient` (list GET returns `{}`), empty `stream_lines`.
- From `client.calls`, find the prompt POST index (`"post"`, `prompt_async`) and the first message-list GET index (`"get"`, `/session/ses_0001/message` — no trailing slash); assert the list GET index is AFTER the prompt POST (polling only), i.e. no seed GET happened pre-prompt.

### 5.5 `_find_serve_pid` exact matching

Reuse the `_fake_listdir`/`_fake_open`/`_FakeProc` pattern (~:1997-2042):
- equals-form: cmdline `opencode\0serve\0--port=18999` matches search "18999";
- prefix guard: cmdline `opencode\0serve\0--port\0189990` does NOT match search "18999";
- shorter search: cmdline `--port\018999` does NOT match search "1899";
- existing NUL space-form test unchanged and green.

### 5.6 Drain behavior

`TestServeDrain.test_drain_breaks_early_when_down` (marked `@pytest.mark.real_recycle`):
- monkeypatch `_find_serve_pid` → `424242`; monkeypatch `opencode_bridge._DRAIN_PROBES = 4`, `_DRAIN_PROBE_S = 0.01`; scripted `is_opencode_serve_running` returning True for the first 2 probes then False → call `_force_recycle_serve()`; assert the helper returns and the running-fn was called 3 times (2 up + 1 down check).
- `test_drain_exhausts_budget_without_raising`: running always True → returns after 4 probes (5 calls), no exception, and (with `_DRAIN_PROBE_S` tiny) no meaningful delay.
- Same two assertions for `_recycle_serve_if_low_memory` via `_serve_health` monkeypatch returning pressure (verify its signature/return first; if `_serve_health` is complex to script, one drain test for `_force_recycle_serve` plus one for `_recycle_serve_if_low_memory` with monkeypatched `_serve_health`).

## 6. Line-budget plan

| Fix | Code | Tests | Total |
|-----|------|-------|-------|
| Fix 1 (seeding + threading) | ~48 | ~120 | ~168 |
| Fix 2 (tokenizer) | ~12 | ~40 | ~52 |
| Fix 3 (drain) | ~16 | ~45 | ~61 |
| **Total** | ~76 | ~205 | **~281** |

Under the 400-line single-pr budget with margin; no chained PRs needed. If a test balloons past the estimate, trim assertions rather than scope.

## 7. Out of scope / open questions

- Out of scope (unchanged from proposal): `opencode_chat` blocking path, routes.py, proxy.py, config, systemd, event-bus semantics, wedge/timeout policy, polling quiet-done cutoff, scripts/.
- Open questions: none — the design is executable as-is. The `_serve_health` scripting detail for the low-memory drain test is resolved at apply time by reading its signature (:1694 area) and monkeypatching it directly.
