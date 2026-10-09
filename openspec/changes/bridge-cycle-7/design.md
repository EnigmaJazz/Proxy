# Design: Bridge Cycle 7 — Resumed-Session Polling Replay Prevention + Serve-PID Port-Form Matching

## Overview

`opencode_chat_stream()` (opencode_bridge.py :736-1216) is the interactive streaming path (routes.py `/opencode`, SDD cycle drivers, opencode-sdd model). When a PINNED session is resumed (multi-turn clarifying-question flow, `session_map` pin at :789-821) and the `/event` SSE bus closes mid-run (`StopAsyncIteration`, :912), the polling fallback `_poll_session_deltas` (:1535-1565) scans the session's FULL message list and re-yields pre-existing history, because the per-call part state (`user_mids`/`text_lens`/`tool_state`, :849-852) is seeded empty on every request. Four replay symptoms on a resumed conversation:

- **(a) Full-history text replay**: every old assistant `text` part re-yields its ENTIRE text as a fresh `("text", ...)` delta (text branch :1258-1265, `prev = text_lens.get(pid, 0)` with an empty `text_lens`).
- **(b) Tool-status spam**: every old tool part re-emits `🔧 …` / `✅ <name> done` / `⚠️ <name> failed` chunks (tool branch :1411-1432, empty `tool_state`).
- **(c) Reasoning re-announcement**: every old `reasoning` part re-emits `("status", "🧠 thinking…\n")` once (reasoning branch :1266-1276).
- **(d) Stale-question stream kill (worst)**: an OLD resolved `question` part (tool `question` with persisted `state.input`, :1279-1410) re-yields a `("question", ...)` delta; `_poll_session_deltas` yields it (:1560-1562) and the polling caller stops the whole stream (:954-956). routes.py then shows the OLD question as visible content while the agent's actual response to the user's already-posted answer never streams.

The event-bus path is immune (live events only, no replay — :855-858), so the defect fires only when the bus closes and polling starts — which is exactly the routine case the fallback exists for (idle-connection closes, :912-916). Entirely untested today; the suite passes because polling tests script empty message lists.

Secondary defect: `_find_serve_pid` (:1741-1762) matches the cmdline ONLY for the substring `"--port {port}"` (space form, :1756). Proxy-spawned serves use the space form (:592-594) so they match, but a manually/other-tooling-started serve with `--port={port}` (equals form) is never found — so age recycling, low-memory recycling, config-drift recycle, the stale-part wedge guard, and `_serve_health` all silently no-op for such serves. The substring match also false-positives on digit-prefix ports (`"--port 18999"` matches a cmdline containing `"--port 189990"`).

This design: (1) seeds per-call part state ONCE at stream start for resumed sessions via one best-effort message-list GET, threading a seen-question-part set through `_yield_part_deltas`/`_poll_session_deltas` so polling emits only NEW changes; (2) tokenizes the cmdline in `_find_serve_pid` to match both `--port P` and `--port=P` exactly. Success-path bytes are unchanged; fresh sessions seed nothing; all existing tests stay green.

## Key Design Decisions

| Decision | Choice | Alternatives considered |
|---|---|---|
| D1 | New helper `_seed_stream_part_state(client, session_id, user_mids, text_lens, tool_state, seen_question_pids)` — one best-effort `GET /session/{id}/message` (10s timeout), wrapped in `except (httpx.HTTPError, OSError, ValueError): pass` (never raises, R2). For each assistant-role message: `user_mids.add(mid)`; for each part: `text`/`reasoning` → `text_lens[pid] = len(text)`; non-question `tool` → `tool_state[pid] = state`; `tool` with `"question"` → `seen_question_pids.add(pid)`. | Seeding lazily inside `_poll_session_deltas` on first poll — runs after the bus already closed and re-scans repeatedly; seeding once at stream start is simpler and covers every later poll. Rejected: re-scan-on-first-poll duplicates the GET and complicates the poll loop. |
| D2 | Seed call site: ONLY when the pinned-resume block produced a `session_id` (:789-821), placed between the pinned-busy handling and the event-bus open, i.e. right after `session_id` is final (after the create block, before `async with client.stream(...)` at :859). Fresh sessions (create path) skip the seed entirely (R5). | Seeding before the pinned-busy status check — an aborted busy session would seed then abort; wasted GET. Seeding after the bus opens — the bus can deliver events between seed and prompt; ordering is safe either way, but seeding before the bus avoids racing the first prompt events. |
| D3 | New parameter `seen_question_pids: Optional[set[str]] = None` threaded through `_yield_part_deltas` (default None, so standalone callers/tests are unaffected) and `_poll_session_deltas` (same). The question branch short-circuits BEFORE the fetch-retry loop at :1309: `if seen_question_pids and pid in seen_question_pids: return`. Both call sites in `opencode_chat_stream` pass the set (polling :951-953 and event-bus :1196-1198 — defensive symmetry; the bus is live-only so the set is inert there). | A module-level "already seen" registry — global mutable state, forbidden by AGENTS.md rule 6. Keying by messageID instead of part id — a resolved old question keeps its part id; message ids change per message; part id is the stable key used by `text_lens`/`tool_state` already. |
| D4 | `_find_serve_pid` matching: after NUL→space normalization (:1755), split into argv tokens; candidate matches when some token is `"--port"` and the NEXT token equals `port`, OR some token equals `f"--port={port}"`. Keep the `"opencode" in cmd` and `"serve" in cmd` guards. Exact token equality only — no substring/prefix matching (R9). Never raises (R10). | Regex on the whole cmdline — same prefix false-positive class as today. Rejected. |
| D5 | Seeding failure degrades silently to today's behavior (R2): an unreadable/empty/unparseable message list leaves the sets empty → old behavior; no error surfaced, no error-string change, no flag surfaced. | Surfacing a warning log on seed failure — noise on every transient failure; the failure is benign (replay is cosmetic-ish, not fatal). Accepted as a `logger.debug` note only, no warning. |
| D6 | Test scripting: reuse `_PollClient` (tests/test_opencode_bridge.py :1151-1162) whose `get()` scripts the message-LIST branch, plus a `_BusyThenIdleClient`-style busy-override for multi-poll scenarios. New tests live in a new `TestResumedSessionSeeding` class; the `_find_serve_pid` tests extend the existing `TestServeHealth` class. | New bespoke fake client — the harness already covers both URL forms; reuse keeps the diff additive. |

## Detailed Changes

### 1. `opencode_bridge.py` — new seeding helper (before `opencode_chat_stream`, ~:736)

```python
async def _seed_stream_part_state(
    client: httpx.AsyncClient,
    session_id: str,
    user_mids: set[str],
    text_lens: dict[str, int],
    tool_state: dict[str, str],
    seen_question_pids: set[str],
) -> None:
    """Seed per-call part state from the session's existing message list.

    Called once at stream start when a PINNED session is resumed.  The
    polling fallback (_poll_session_deltas) scans the FULL message list
    when the /event SSE bus closes; with empty per-call state it re-yields
    the conversation's history (full old text, tool-status chunks,
    "thinking" announcements, and the stale resolved question that stops
    the stream).  Recording each existing part's current length/state and
    the question-part ids makes polling emit only NEW changes.  Best-
    effort by design (R2): any failure leaves the sets empty and the
    stream proceeds exactly as before.  Never raises.
    """
    try:
        resp = await client.get(
            f"{OPENCODE_SERVE_URL}/session/{session_id}/message", timeout=10.0,
        )
        if resp.status_code != 200:
            return
        for m in resp.json():
            if (m.get("info") or {}).get("role") != "assistant":
                continue
            mid = (m.get("info") or {}).get("id")
            if mid:
                user_mids.add(str(mid))
            for p in m.get("parts") or []:
                pid = str(p.get("id") or "")
                if not pid:
                    continue
                ptype = p.get("type")
                if ptype in ("text", "reasoning"):
                    text_lens[pid] = len(str(p.get("text") or ""))
                elif ptype == "tool":
                    if p.get("tool") == "question":
                        seen_question_pids.add(pid)
                    else:
                        tool_state[pid] = str((p.get("state") or {}).get("status") or "")
    except (httpx.HTTPError, OSError, ValueError):
        logger.debug("failed to seed stream part state for %s", str(session_id)[:16])
```

Note: `user_mids` is populated with ASSISTANT message ids here. `_poll_session_deltas` filters on `role == "assistant"` messages (:1552-1553) and `_yield_part_deltas` skips parts whose `messageID in user_mids` (:1194, :1555-1556). Recording assistant message ids as "already seen" marks their PARTS as seen by the caller filter, which is precisely the history-suppression wanted for polling; the event-bus path never receives old parts so the same set is inert there. (The pre-existing "user_mids" name is a slight misnomer for this use; keeping the name avoids signature churn in `_poll_session_deltas`.)

### 2. `opencode_chat_stream` — per-call state + seed call + threading

- :849-852: add a fifth per-call state holder:
  ```python
  seen_question_pids: set[str] = set()
  ```
- After the create/resume block (after `session_id` is final — i.e. after the `if not session_id:` create branch, before `payload`/`async with client.stream(...)`), insert:
  ```python
  # Resumed pinned session: record the existing conversation's part state
  # so the polling fallback (bus closed) never replays history as fresh
  # deltas (cycle 7).  Fresh sessions seed nothing (empty history).
  if session_id and session_map is not None and session_key and session_map.get(session_key):
      await _seed_stream_part_state(
          client, session_id, user_mids, text_lens, tool_state,
          seen_question_pids,
      )
  ```
  (The resumed-path condition mirrors the pin reuse at :789-793: a pin existed and was not aborted. A pin that was aborted as busy has `session_id = None` afterwards and the create branch makes a fresh session — no seed.)
- Polling call site (:951-953): pass `seen_question_pids` as a keyword argument to `_poll_session_deltas`.
- Event-bus part handling (:1196-1198): pass `seen_question_pids` as a keyword argument to `_yield_part_deltas`.

### 3. `_poll_session_deltas` (:1535-1565) — signature + passthrough

```python
async def _poll_session_deltas(
    client: httpx.AsyncClient,
    session_id: str,
    user_mids: set[str],
    text_lens: dict[str, int],
    tool_state: dict[str, str],
    seen_question_pids: Optional[set[str]] = None,
) -> AsyncIterator[tuple[str, str]]:
```
and at the `_yield_part_deltas(...)` call (:1557-1559): `async for delta in _yield_part_deltas(p, text_lens, tool_state, session_id, client, seen_question_pids=seen_question_pids):`.

### 4. `_yield_part_deltas` (:1238-1432) — signature + question short-circuit

```python
async def _yield_part_deltas(
    part: dict[str, Any],
    text_lens: dict[str, int],
    tool_state: dict[str, str],
    session_id: str,
    client: httpx.AsyncClient,
    seen_question_pids: Optional[set[str]] = None,
) -> AsyncIterator[tuple[str, str]]:
```
At the top of the `if name == "question":` branch (:1279), BEFORE the fetch-retry loop:
```python
        if name == "question":
            # A question part that already existed when this request started
            # (seeded on resume) was relayed in an earlier turn — never
            # re-yield it; a fresh question part (new pid) still stops the
            # stream so the user can answer.
            if seen_question_pids and pid in seen_question_pids:
                return
```
No other branch changes. `text`/`reasoning`/`tool` branches are already gated by the seeded `text_lens`/`tool_state` (growing lengths emit only suffixes; equal states emit nothing).

### 5. `_find_serve_pid` (:1741-1762) — tokenized port matching

Replace the matcher line (:1756) with exact argv-token matching:

```python
        # /proc/<pid>/cmdline separates argv with NUL bytes; normalize NULs
        # to spaces and split into tokens so "--port" and the port match as
        # EXACT tokens (both "--port P" and "--port=P" forms) — substring
        # matching misreads "--port 189990" as port 18999 and misses the
        # equals form used by manually started serves.
        norm = cmd.replace("\x00", " ")
        if "opencode" not in norm or "serve" not in norm:
            continue
        toks = norm.split()
        matched = False
        for i, tok in enumerate(toks):
            if tok == f"--port={port}":
                matched = True
                break
            if tok == "--port" and i + 1 < len(toks) and toks[i + 1] == port:
                matched = True
                break
        if matched:
            return int(entry)
```
(The `"opencode" in cmd`/`"serve" in cmd` guards are now applied to the normalized string; behavior for unreadable entries unchanged — `except (OSError, ValueError): continue`.)

### 6. No changes

- Blocking path (`opencode_chat` :622-734): untouched (R7).
- `_yield_part_deltas` text/reasoning/tool branch semantics: unchanged.
- Error strings, yielded tuple kinds, `_recycle_serve_if_low_memory`, `_force_recycle_serve`, `_serve_health`, `_serve_start_epoch_ms`: unchanged (they only start finding equals-form serves).

## Test Design (additive, hermetic — no live serve; `hermetic_serve` autouse guard)

New class `TestResumedSessionSeeding` (after `TestStreamExitHygiene`, tests/test_opencode_bridge.py :550) — `@pytest.mark.asyncio`, monkeypatch `ensure_opencode_serve` → `_running` (True), `httpx.AsyncClient` → the scripted client, fresh per-test `session_map`/`pending` (NOT the shared `PP`):

1. **`test_resume_seed_suppresses_history_replay`** — `_PollClient` (list branch scripts `poll_messages`) with an OLD assistant message carrying: a completed `text` part (long text), a `tool` part with `state.status == "completed"`, a `reasoning` part, and a resolved `question` part with `state.input.questions`. `session_map = {"conv": "ses_0001"}` (pinned resume), `stream_lines = []` (immediate `StopAsyncIteration` → polling). First poll cycle: `/session/status` returns idle → the loop exits via the idle branch (:1030-1040). Assert: `session_map` still pins the session; the yielded deltas contain NO old text, NO `✅/⚠️/🔧` chunks, NO `🧠 thinking…`, NO `("question", ...)`; and the FIRST GET on `client.calls` (after session-status) was the message-list GET (seed). To also assert new-turn streaming after seeding, script a second poll message whose text part is a SUPERSET of the old one (same part id, longer text) and use a busy-then-idle status override so a second poll cycle runs — assert only the suffix streams.

2. **`test_resume_seed_failure_degrades`** — resume with the message-list GET failing (`get_status = 500` or `raise_timeout_on`): stream completes/errors exactly as today (no raise, no new error string).

3. **`test_fresh_session_no_seed_get`** — no pin (`session_map = {}`): assert NO message-list GET in `client.calls` (the list URL never appears) and behavior unchanged.

4. **`test_new_question_part_still_stops_stream`** — resumed session seeded with an old resolved question; a NEW question part (fresh pid) appears in a poll message → `("question", ...)` yields and the stream stops (R4 guard).

`TestServeHealth` additions (next to `test_find_serve_pid_matches_nul_separated_cmdline` :1997):

5. **`test_find_serve_pid_matches_equals_form`** — cmdline `opencode\x00serve\x00--port=18999\x00--hostname\x00127.0.0.1` → `_find_serve_pid("18999") == pid`.
6. **`test_find_serve_pid_rejects_prefix_port`** — cmdline contains `--port 189990` → `_find_serve_pid("18999") is None`; and `--port 18999` → `_find_serve_pid("189990") is None`.
7. Space-form NUL test (:1997) stays green unchanged.

Suite verification: `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q` (all existing + new green), then `tests/ -q` for the full suite. `hermetic_serve` asserts no real serve was touched.

## Risks / Notes

- The seed GET adds ONE bounded request (10s timeout) per resumed stream; failure degrades to today's behavior (R2). No warning spam — a single `logger.debug`.
- Suppression scope is exactly the pre-prompt history: fresh pids (new parts/questions) are unaffected (R4).
- `_yield_part_deltas`/`_poll_session_deltas` default the new parameter to `None`, so direct unit callers (existing tests :1182-1232) are untouched.
- Adjacent cycle-6 capability (`opencode-bridge-stream-exit-hygiene`) and the blocking path are untouched; cycle-5's `_abort_session_best_effort` untouched.
- `_find_serve_pid` behavior for proxy-spawned serves is unchanged (space form still matches); only previously-missed equals-form serves and prefix false positives change.
- Estimated changed lines: ~150-200 incl. tests → within the 400-line budget, single PR.
