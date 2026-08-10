# Design: Bridge Cycle 8 — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills

## Overview

Three opencode-bridge reliability fixes, all confined to `opencode_bridge.py` plus additive hermetic tests:

1. **Fix 1 — resumed-session polling replay prevention**: per-call part state (`user_mids`/`text_lens`/`tool_state`) is normally fed only by live `/event` bus deltas. When a PINNED session is resumed and the bus closes, `_poll_session_deltas` re-scans the FULL message list and `_yield_part_deltas` re-yields history — including a resolved `question` tool part, which the polling caller treats as the agent asking and stops the stream with a STALE question while the real answer never streams. Fix: one best-effort `GET /session/{id}/message` after pin acquisition, before `prompt_async`, seeds the per-call state (text lengths, tool states, assistant message ids) plus a new `seen_question_pids` set threaded through both call sites; the question branch short-circuits for seeded pids.
2. **Fix 2 — exact serve-PID port matching**: `_find_serve_pid` only substring-matched the space form `f"--port {port}"` on the NUL-normalized cmdline. Equals-form `--port=18999` never matched; digit-prefix `--port 189990` false-positived for search `18999`. Fix: tokenize and require exact adjacent `("--port", port)` tokens or an exact `--port={port}` token; no substring/prefix matching.
3. **Fix 3 — bounded drain after recycle kills**: both recycle helpers sent `os.kill(pid, 15)` and returned immediately; `ensure_opencode_serve` probes `is_opencode_serve_running()`, which still answers True mid-SIGTERM, so no respawn happened and the stream POSTed into a dying listener. Fix: a bounded drain helper (up to 4 probes × 0.25 s, early break, never raises) runs after every verified kill.

All three fixes are already implemented in the working tree (uncommitted) with hermetic tests; this design documents the implemented approach so the artifacts and the code stay in lockstep.

## Key Design Decisions

| Decision | Choice | Alternatives considered |
|---|---|---|
| D1 | Seeding happens ONLY for resumed sessions (`session_id` came from the session map, not from a fresh POST). A `resumed = session_id is not None` flag is captured BEFORE the fresh-session POST. | Seeding every session — rejected: fresh sessions have empty history; an extra GET per request would add latency for zero benefit (and change the fresh-session request pattern, breaking test `test_fresh_session_does_not_seed`). |
| D2 | Seeding is a single best-effort GET (`timeout=10.0`) whose every failure mode returns silently: non-200 status, malformed non-list body, `httpx.HTTPError` / `OSError` / `ValueError`. | Raising on seed failure — rejected: a seed failure must degrade to today's replay behavior, never kill the request. |
| D3 | The seen-question short-circuit sits at the TOP of the question branch, BEFORE the fetch-retry loop, so a seeded pid is suppressed without any network fetch. | Suppressing after the fetch — rejected: the fetch is exactly the "agent is asking" path; suppression must be decided purely from seed state. |
| D4 | `seen_question_pids` is a new `Optional[set[str]]` parameter on `_yield_part_deltas`/`_poll_session_deltas` with default `None` (empty-seen semantics). | Required parameter at both call sites — rejected: the signature stays backward-compatible for any direct test callers; `None` behaves as an empty set. |
| D5 | `_find_serve_pid` tokenizes `cmd.replace("\x00", " ").split()`; requires at least one token containing `opencode` AND an exact `serve` token; then matches `f"--port={port}" in tokens` OR an adjacent `("--port", port)` pair. | Plain `in` substring checks on tokens — rejected (prefix false-positives); regex on the raw cmdline — rejected (NUL handling complexity, no benefit over tokenization). |
| D6 | Drain is a tiny module-level helper `_drain_serve_shutdown()` with `_DRAIN_PROBES = 4`, `_DRAIN_PROBE_S = 0.25` (mirroring the config-drift drain at `:509-514`), called after the kill inside the `if pid:` block of both recycle helpers. | Inlining the loop twice — rejected (drift risk); a longer drain — rejected: `~1s` worst case is the same class as the existing drift-gate drain, and `ensure_opencode_serve`'s own probe remains the final arbiter. |
| D7 | Drain tests use the existing `@pytest.mark.real_recycle` autouse-fixture marker so the hermetic guard does NOT noop the helper bodies; `_find_serve_pid`/`os.kill`/`is_opencode_serve_running`/probe constants are monkeypatched (fake pid 424242, probe cadence 0.01 s). | Directly calling the drain helper — rejected: the tests must prove the HELPERS drain, not just the helper in isolation. |

## Detailed Changes

### 1. `opencode_chat_stream` — resumed flag + seed (around :825-880)

After pin acquisition (`session_id` from the session map, before the fresh-session `POST /session`):

```python
resumed = session_id is not None
```

And in the per-call state block, after the empty state dicts:

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

The event bus still opens BEFORE `prompt_async`; seeding happens before that too (it must precede the prompt, otherwise the new turn's parts would contaminate the seed).

### 2. `opencode_bridge.py` — new helper `_seed_resumed_session_state` (before `_poll_session_deltas`)

```python
async def _seed_resumed_session_state(client, session_id, user_mids,
                                      text_lens, tool_state,
                                      seen_question_pids) -> None:
```

- GETs `{OPENCODE_SERVE_URL}/session/{session_id}/message` with `timeout=10.0`.
- Non-200 → return; body not a list (e.g. an error object) → return.
- For every message with `info.role == "assistant"`: add `m["id"]` to `user_mids` (historically misnamed; it is a seen-parts filter, see cycle-7 note); per part:
  - `text`/`reasoning` with truthy text → `text_lens[pid] = len(text)`;
  - `tool` with a truthy `state.status` → `tool_state[pid] = status`;
  - `tool == "question"` → `seen_question_pids.add(pid)`.
- Wraps the whole body in `except (httpx.HTTPError, OSError, ValueError): return`.

### 3. `_yield_part_deltas` / `_poll_session_deltas` — seen-set threading

Both gain `seen_question_pids: Optional[set[str]] = None`. `_poll_session_deltas` forwards it to `_yield_part_deltas`. In the question branch (`ptype == "tool"`, `name == "question"`), the first statement:

```python
if seen_question_pids and pid in seen_question_pids:
    return
```

Both call sites pass the set: the polling caller (`:969`) and the event-bus caller (`:1215`). For a fresh session the set stays empty → zero behavior change; the event-bus path sees only live parts, so the set is inert there.

### 4. `_find_serve_pid` — exact token matching (around :1853-1880)

```python
tokens = cmd.replace("\x00", " ").split()
if not any("opencode" in tok for tok in tokens) or "serve" not in tokens:
    continue
if f"--port={port}" in tokens:
    return int(entry)
for i, tok in enumerate(tokens):
    if (tok == "--port" and i + 1 < len(tokens)
            and tokens[i + 1] == port):
        return int(entry)
```

The existing `except (OSError, ValueError): continue` malformed-entry resilience and the outer `except OSError` stay. The pre-existing NUL-separated space-form test stays green (adjacent-token form).

### 5. `_drain_serve_shutdown` + both recycle helpers

```python
_DRAIN_PROBES = 4
_DRAIN_PROBE_S = 0.25

async def _drain_serve_shutdown() -> None:
    for _ in range(_DRAIN_PROBES):
        if not await is_opencode_serve_running():
            return
        await asyncio.sleep(_DRAIN_PROBE_S)
```

Called immediately after the verified kill inside `if pid:` in both `_recycle_serve_if_low_memory` (low-memory OR age triggers) and `_force_recycle_serve` (long-lived call / config drift). The `hermetic_serve` autouse guard still noops both helpers in tests unless `real_recycle` is marked.

## Test Design (hermetic, no live serve)

| Test group | Coverage |
|---|---|
| `_SeedPollClient` harness | `_PollClient` variant whose FIRST `/session/{id}/message` GET serves scripted seed history; later GETs delegate to `poll_messages`. `seed_status`/`raise_on_seed` script failures. Existing `_PollClient` tests are unaffected. |
| `TestSeedResumedSessionState` | Seeding populates `text_lens`, `tool_state`, `user_mids`, `seen_question_pids`; failure paths (HTTP 500, raising GET, dict body) return silently. |
| `TestResumedSessionPollingReplay` | Scenario-1 (REQ-1): resumed session + empty stream bus + scripted history (old text/reasoning/completed tool/resolved question) + new-turn delta → no replay, no stale-question stop, new delta streams, no network-error. Scenario-2: seed failure degrades without raising. Scenario-3: fresh session performs NO seed GET before the prompt POST. |
| `TestFindServePidExactMatch` | Equals-form `--port=18999` matches; longer advertised `--port 189990` rejected for search `18999`; shorter search `1899` rejected for advertised `18999`. Uses `_patch_proc_cmdline` (`/proc/4242/cmdline` fake). |
| `TestServeDrain` (`@pytest.mark.real_recycle`) | Force-recycle drain breaks early when down (3 running-fn calls), exhausts budget without raising when always up (4 calls); low-memory recycle drain breaks early. Monkeypatched `_find_serve_pid` (fake pid 424242), `os.kill`, `is_opencode_serve_running`, probe constants (0.01 s cadence). |

## Integration Points & Risks

- **Integration**: seeding runs before the event bus opens; the seen set is inert on the event-bus path; `opencode_chat` (blocking path), routes.py, proxy.py, config, and systemd are untouched.
- **Risk (accepted)**: a seed GET adds one round-trip per resumed request (~10 s worst-case timeout, typically <100 ms). If the serve is slow, the request waits up to 10 s before the prompt — bounded and best-effort.
- **Risk (accepted)**: if the seed misses parts (e.g. body shape drift), replay may still occur — exactly today's behavior, never worse.
- **Risks addressed**: stale-question stream kills on resume; serve recycling silently no-op'ing for equals-form ports; network errors after recycle kills.

## Verification

Acceptance: `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q`; full suite `.venv/bin/python -m pytest tests/ -q`. Both must stay hermetic. The pre-existing NUL space-form `_find_serve_pid` test and the `hermetic_serve` guard remain green.
