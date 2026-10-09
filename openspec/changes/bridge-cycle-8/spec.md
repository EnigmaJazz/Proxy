# Specification: Bridge Cycle 8 — Replay-Prevention Adoption, Exact Serve-PID Matching, Bounded Drain After Recycle Kills

## Purpose

The OpenCode bridge MUST ensure resumed conversations stream only new deltas after polling fallback and serve discovery matches the exact port token in either supported argv form. After a recycle kill, the bridge MUST hand a stopped listener to `ensure_opencode_serve` so the initiating request can reliably respawn the serve.

## Requirements

### REQ-1: Resumed-session polling replay prevention

Cycle 8 MUST adopt R1–R7 from `openspec/specs/opencode-bridge-polling-replay-prevention/spec.md`. Resumed sessions MUST seed existing part state and `seen_question_pids` once through a best-effort message-list GET before prompt POST; failures MUST degrade without raising. The seen set MUST flow through `_poll_session_deltas` and `_yield_part_deltas` at both call sites. Polling MUST suppress historical text, tool, reasoning, and resolved-question deltas while allowing a new question to stop the stream; fresh sessions, event-bus behavior, and the blocking path MUST remain unchanged.

#### Scenario-1: Resumed follow-up survives bus closure

- GIVEN a pinned session whose history contains text, tool, reasoning, and resolved-question parts
- WHEN the seed GET completes, the prompt posts, and bus closure causes polling fallback
- THEN no historical part is replayed
- AND only new-turn deltas stream, with any new question stopping the stream

#### Scenario-2: Seed fetch fails safely

- GIVEN a pinned session whose message-list seed GET fails
- WHEN resumed streaming continues
- THEN the request does not raise because of the seed failure
- AND polling degrades to the existing unseeded behavior

#### Scenario-3: Fresh session remains unseeded

- GIVEN no session pin exists
- WHEN a fresh streaming session starts
- THEN no seed message-list GET occurs
- AND existing fresh-session behavior remains unchanged

### REQ-2: Exact serve-PID port matching

Cycle 8 MUST adopt R8–R12 from `openspec/specs/opencode-bridge-polling-replay-prevention/spec.md`. PID discovery MUST require `opencode` and `serve`, match exact adjacent `--port`, `<port>` tokens or exact `--port=<port>`, and MUST never use substring equality; malformed `/proc` entries MUST be skipped, and the existing NUL-separated space-form behavior MUST remain valid.

#### Scenario-1: Equals-form port matches

- GIVEN a cmdline contains exact `opencode`, `serve`, and `--port=18999` tokens
- WHEN serve discovery searches for port `18999`
- THEN that serve PID is returned

#### Scenario-2: Digit prefixes are rejected both ways

- GIVEN serve cmdlines advertise ports `18999` and `189990`
- WHEN discovery searches for `1899` or compares `18999` against `189990`
- THEN neither shorter search nor longer advertised port produces a prefix match

### REQ-3: Bounded drain after recycle kills

After a verified `os.kill(pid, SIGTERM)`, `_recycle_serve_if_low_memory` and `_force_recycle_serve` MUST poll `is_opencode_serve_running()` on a bounded schedule of up to four probes at approximately 0.25-second intervals, breaking early when the serve no longer reports running. The drain MUST never raise or exceed its budget; if the serve remains up, the helper MUST return as before. No drain MUST run when PID verification under lifecycle REQ-3 caused the kill to be skipped.

#### Scenario-1: Listener stops during drain

- GIVEN a verified recycle kill was sent and the serve reports down on probe three
- WHEN the drain runs before `ensure_opencode_serve`
- THEN the drain breaks after probe three
- AND ensure detects the stopped listener and respawns the serve

#### Scenario-2: Listener outlives the budget

- GIVEN a verified recycle kill was sent and the serve remains running through every probe
- WHEN the bounded drain exhausts its budget
- THEN the helper returns without raising or waiting beyond the budget
- AND subsequent processing behaves as before

## Verification

The acceptance command is `.venv/bin/python -m pytest tests/test_opencode_bridge.py -q`; the full-suite command is `.venv/bin/python -m pytest tests/ -q`. Verification MUST remain hermetic with no live serve, while the NUL-separated PID test at `tests/test_opencode_bridge.py:1997-2042` and the `hermetic_serve` autouse guard stay green.
