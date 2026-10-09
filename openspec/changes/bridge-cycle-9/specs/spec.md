# Specification: Bridge Cycle 9 — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills

## Purpose

The OpenCode bridge MUST ensure resumed conversations stream only new deltas after polling fallback and serve discovery matches the exact port token in either supported argv form. After a recycle kill, the bridge MUST boundedly drain the dying listener so `ensure_opencode_serve` can reliably respawn it within the initiating request.

## Requirements

### REQ-1: Resumed-session polling replay prevention

Cycle 9 MUST adopt R1–R7 from `openspec/specs/opencode-bridge-polling-replay-prevention/spec.md`. A resumed pinned session MUST perform one best-effort message-list GET before `prompt_async`, seeding existing assistant message IDs, text/reasoning lengths, tool states, and `seen_question_pids`; seed failures MUST NOT raise. The seen-question set MUST be threaded through `_poll_session_deltas` and `_yield_part_deltas` at both call sites, and the question branch MUST suppress a seeded PID before entering its fetch-retry loop. Fresh sessions MUST remain unseeded, while event-bus handling and the blocking path MUST remain unchanged.

#### Scenario-1: Resumed follow-up survives bus closure

- GIVEN a pinned session with historical assistant text, reasoning, tool, and resolved-question parts
- WHEN seeding completes, the prompt posts, and bus closure activates polling fallback
- THEN historical parts and seeded questions are not replayed
- AND only new-turn deltas stream, with a new question retaining existing stop behavior

#### Scenario-2: Seed fetch fails safely

- GIVEN a pinned session whose message-list seed GET fails
- WHEN resumed streaming continues
- THEN the seed failure does not raise
- AND polling safely degrades to existing unseeded behavior

#### Scenario-3: Fresh session remains unseeded

- GIVEN no session pin exists
- WHEN a fresh streaming session starts
- THEN no seed message-list GET occurs
- AND fresh-session behavior remains unchanged

### REQ-2: Exact serve-PID port matching

Cycle 9 MUST adopt R8–R12 from `openspec/specs/opencode-bridge-polling-replay-prevention/spec.md`. PID discovery MUST tokenize the NUL-normalized cmdline and match either exact adjacent `--port`, `<port>` tokens or an exact `--port=<port>` token; substring and prefix matching MUST NOT be used. Candidates MUST contain `opencode` and `serve`, malformed or unreadable entries MUST be skipped without raising, and NUL-separated space-form cmdlines MUST remain valid.

#### Scenario-1: Equals-form port matches

- GIVEN a cmdline contains exact `opencode`, `serve`, and `--port=18999` tokens
- WHEN serve discovery searches for port `18999`
- THEN that serve PID is returned

#### Scenario-2: Digit prefixes are rejected both ways

- GIVEN cmdlines advertise `--port 18999` and `--port 189990`
- WHEN discovery searches for `1899` or compares `18999` with `189990`
- THEN neither shorter search nor longer advertised value produces a match

### REQ-3: Bounded drain after recycle kills

After a verified `os.kill(pid, SIGTERM)`, both `_recycle_serve_if_low_memory` and `_force_recycle_serve` MUST poll `is_opencode_serve_running()` for up to four probes approximately 0.25 seconds apart, breaking early when the serve reports down. This MUST apply to low-memory and age-triggered recycling. The drain MUST never raise or exceed its bounded budget; no drain MUST run when PID verification skips the kill.

#### Scenario-1: Listener stops during drain

- GIVEN either recycle helper sends a verified SIGTERM
- WHEN the listener reports down before all four probes complete
- THEN probing stops immediately
- AND `ensure_opencode_serve` can observe the stopped listener and respawn it

#### Scenario-2: Listener outlives the budget

- GIVEN a verified SIGTERM was sent and the listener remains up through every probe
- WHEN the four-probe budget is exhausted
- THEN the helper returns without raising or exceeding the budget
- AND subsequent processing continues without an additional drain wait

## Verification

Acceptance MUST run `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q`; the full suite MUST run `.venv/bin/python -m pytest tests/ -q`. Verification MUST be hermetic with no live serve. The existing NUL PID test at approximately `tests/test_opencode_bridge.py:1997-2042` and the `hermetic_serve` autouse guard MUST remain green.
