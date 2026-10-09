# Exploration — bridge-cycle-11 (blocking-path reliability: connect-only respawn gate, wedge detection, error-path cleanup)

## 1. Current State

The bridge (`opencode_bridge.py`, 2172 lines) has two independent delivery paths:

- **Streaming path** (`opencode_chat_stream`) — event bus + polling fallback, wedge
  detection (`_detect_wedged_tool`, `:1648-1708`, thresholds `_TOOL_WEDGE_AFTER_S`
  300s / `_TASK_WEDGE_AFTER_S` 600s), permission relay, stale-pin self-heal,
  exit-hygiene trio. Heavily hardened through cycles 4–10.
- **Blocking path** (`opencode_chat` / `_opencode_chat_attempt`) — used by
  `opencode_escalation` (queue-worker cloud escalation, `proxy.py:401`). Creates a
  fresh session, POSTs the message (blocking, `timeout=OPENCODE_SERVE_TIMEOUT=600s`),
  polls `GET /permission` every `_BLOCKING_PERMISSION_POLL_S` (5s) while the POST is
  in flight, and runs ONE bounded respawn (cycle-10/review-fix) on network-class
  failures.

**Verification baseline**: working tree clean at `3b00002`; full suite green
(`502 passed`; bridge suite `181 passed`; driver suites `33 passed`).

## 2. The Chosen Gap — the blocking path contradicts its own no-recycle rule, has no wedge detection, and leaks sessions on malformed bodies

Three defects in `_opencode_chat_attempt` (`opencode_bridge.py:795-905`), all
reproduced hermetically below. Together they form one coherent theme: **the
queue-worker/blocking escalation path lacks the reliability discipline the
streaming path already has.**

### Defect 1 (P1, correctness) — a ReadTimeout on the blocking message POST force-recycles the serve and destroys every concurrent session

**Root cause**: the inner poll-loop exception handler
(`opencode_bridge.py:877-879`) classifies EVERY `httpx.HTTPError` (including
`ReadTimeout`) as `network_failed=True`:

```python
except (httpx.HTTPError, OSError) as exc:
    await _abort_session_best_effort(client, session_id)
    return f"[OpenCode Bridge Network Error: {str(exc)}]", True   # ← ReadTimeout → True
```

`opencode_chat`'s retry gate (`:779-792`) then reacts to any `network_failed=True`
with `_force_recycle_serve("blocking-path respawn")` — a SIGTERM of the whole
serve. The review-fix commit `37640d5` narrowed the OUTER exception split
(`:898-905`) to "recycles only on connect-class failures … ReadTimeout and
malformed bodies never recycle the serve (would kill concurrent sessions)" — but
missed this inner handler. The outer comment at `:902-904` states the intent
verbatim: "NEVER recycle on these — the respawn would SIGTERM the serve and
destroy every concurrent session."

**When it fires**: a wedged tool in the queue-worker path (the serve's documented
bash-hang failure mode) burns the full 600s `OPENCODE_SERVE_TIMEOUT`, then the
ReadTimeout **SIGTERMs the serve** — killing every concurrent streaming session
(including live autonomous SDD cycles) — and retries the call once more on a fresh
serve (another 600s hang on a still-wedged runner). Total: a 20-minute hang PLUS
destruction of concurrent sessions, exactly what the review-fix prohibited.

**Hermetic repro (confirmed pre-fix)** — `raise_timeout_on = "post"` against the
existing `_FakeClient`:

```
RESULT: [OpenCode Bridge Network Error: read timed out]
ensure_calls: 2          ← re-ensure ran
recycle_calls: ['blocking-path respawn']   ← THE BUG: serve SIGTERMed
message_posts: 2         ← full retry attempt
aborts: 2
```

### Defect 2 (P2, reliability) — the blocking path has no wedge detection (documented open item)

A tool wedged in "running" with no output holds the message POST open for the full
600s with zero progress feedback and no early abort. The streaming path detects
the same condition in ~5 min (`_TOOL_WEDGE_AFTER_S` 300s + 10s cadence) and
aborts the session with a clear error; the blocking path just hangs until the
ReadTimeout. This is an explicitly-recorded open item from cycle-9's archive
report ("blocking-path wedge detection"). The fix rides the EXISTING 5s
permission-poll cadence (`:855-876`) — the same loop already calls
`_detect_pending_permission`; the wedge check slots in after it (permission check
first — a parked write looks identical to a wedge in the message list, the same
ordering rule the streaming path uses at `:1266-1278`).

### Defect 3 (P3, hygiene) — a malformed message-POST body returns WITHOUT aborting the session

`data = resp.json()` (`:886`) — a `ValueError` propagates to the outer
`except (httpx.HTTPError, ValueError)` (`:901-905`) which returns the error string
**without** `_abort_session_best_effort`, violating the function's own docstring
promise (`:763-764`: "On every non-success exit the created session is aborted
best-effort"). Confirmed pre-fix: `aborts fired: 0` on a malformed 200 body.

## 3. Affected Areas

- `opencode_bridge.py:877-879` — inner poll-loop except (Defect 1: split into
  connect-class → `True` / other-HTTPError+ValueError → `False`, abort in both).
- `opencode_bridge.py:855-876` — poll-cadence block (Defect 2: add the
  `_detect_wedged_tool` check after the permission check; on wedge: abort +
  `(error, False)` — never recycle, mirroring the streaming path's
  "never kill the serve for one wedged tool" rule at `:1284-1293`).
- `opencode_bridge.py:886` — message-body parse (Defect 3: wrap `resp.json()` in
  try/except ValueError → abort → error string).
- `tests/test_opencode_bridge.py` — 4–5 new hermetic tests in
  `TestOpenCodeChatHardening` (pattern: `fake_client`/`_PollClient` +
  monkeypatched `ensure_opencode_serve`/`_force_recycle_serve` + the autouse
  `hermetic_serve` guard).
- NOT touched: routes.py, proxy.py, the streaming path, scripts/, constants,
  config — the change is entirely inside the blocking attempt and its tests.

## 4. Approaches Considered

| # | Approach | Pros | Cons | Effort |
|---|----------|------|------|--------|
| A | **Fix all three blocking-path defects (recommended)**: connect-only respawn gate in the inner except; wedge detection on the existing 5s poll cadence; abort-on-malformed-body | Directly completes the review-fix intent; addresses the recorded open item; defense-in-depth (wedge detection catches most wedges at ~300s, the respawn gate stops serve-kills for the rest); all hermetic-testable with existing harness; ~30 bridge lines + ~170 test lines ≈ 200 changed lines, well under the 400-line budget | Three sub-fixes in one change (cohesive theme: "blocking path gets streaming-path discipline") | **Low** |
| B | Defect 1 only (respawn gate) | Smallest possible change (~12 bridge lines + 1 test) | Leaves the 600s no-progress hang (Defect 2) and the abort leak (Defect 3) — under-delivers the cycle's value | Low |
| C | Bump `_TASK_WEDGE_AFTER_S` 600 → 1200s (sub-agent phase length) | One-line constant change | Unproven: whether a healthy sub-agent phase actually leaves the task part output-less is serve-internal behavior; risks hiding real wedges; no hermetic proof of a live false-wedge | Very low value |
| D | Extend the zombie sweep to protect actively-generating sub-agent sessions | Closes a speculative sweep blind spot | Depends on unproven serve timestamp-lag semantics; the driver's go-proxy hold already mitigates on the cycle side; touches `_abort_zombie_sessions` shared by the streaming path | Low/uncertain |
| E | Replay-log reader session-scoping (`_replay_in_flight_for_any_session`) | Tighter carve-out | The global any-session behavior is conservative-by-design; the log records don't carry the session↔tool mapping needed to scope it | Medium, not a defect |

## 5. Recommendation

**Approach A** — land all three blocking-path fixes as bridge-cycle-11 (single PR,
`delivery_strategy = single-pr`). Rationale:

1. **Defect 1 is the headline**: a direct, reproduced contradiction of the
   just-landed review-fix (`37640d5`) with destructive consequences (serve
   SIGTERM kills concurrent sessions; double 600s hang). It is the "respawn race"
   class of gap the exploration brief targets.
2. **Defect 2 is the recorded open item** from cycle-9's archive report and makes
   the queue-worker path behave like the streaming path (early abort at ~300s
   with a clear error instead of a 600s silent hang).
3. **Defect 3 closes the error-path cleanup contract** ("never raises, never
   leaks" hygiene already applied to the streaming path's cleanup trio).
4. All three are proven real (hermetic repros above), all are testable with the
   existing `_FakeClient`/`_PollClient` harness, and the combined scope (~200
   changed lines including tests) fits the 400-line review budget with margin.

## 6. Risks

- **Queue-worker behavior change on ReadTimeout**: with Defect 1 fixed, a wedged
  tool in the blocking path returns an error string after 600s instead of
  recycle+retry. Genuine serve-death still recycles (ConnectError/
  ConnectTimeout/RemoteProtocolError/OSError remain `network_failed=True`), and
  Defect 2 aborts most wedges at ~300s before the ReadTimeout. The residual case
  (serve event-loop stall that accepts connections but never responds) matches
  the streaming path's accepted behavior.
- **False wedge in the blocking path**: the same risk profile the streaming path
  already accepts (`_TOOL_WEDGE_AFTER_S` 300s, running-with-no-output, after the
  current serve's start). Permission check runs FIRST so a parked write is never
  killed as a wedge.
- **Test-order coupling**: the new tests must not perturb the autouse
  `hermetic_serve` kill guard (pattern: monkeypatched `_force_recycle_serve`
  recorder, never a real kill) — same as the existing `TestOpenCodeChatHardening`
  tests.
- **Scope discipline**: the streaming path is deliberately untouched; the two
  paths stay merge-safe because Defect 1/2/3 live only inside
  `_opencode_chat_attempt`.

## 7. Ready for Proposal

**Yes.** Three concrete, hermetic-reproduced defects with exact anchors
(`opencode_bridge.py:877-879`, `:855-876`, `:886`), a recommended fix (Approach
A), a fitting scope (~200 changed lines incl. tests), and a green baseline
(502 passed). The orchestrator should tell the user: the proposal phase can
proceed with the three-fix blocking-path reliability change; no clarifications
needed.
