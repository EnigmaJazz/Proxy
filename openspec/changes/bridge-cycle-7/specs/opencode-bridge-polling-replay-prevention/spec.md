# Polling Replay Prevention and Serve PID Matching

## Capability Description

`opencode-bridge-polling-replay-prevention` SHALL seed resumed history once so polling emits only new changes. `opencode-bridge-serve-pid-matching` SHALL find exact ports in either argv form.

## Requirements

| ID | Requirement | Verification |
|---|---|---|
| R1 | For a resumed `session_map` pin, stream start MUST make exactly one best-effort message-list GET before prompt POST, seeding every existing text/reasoning length, tool status, user-message ID, and question-part ID. | Scripted list; S1. |
| R2 | HTTP error/timeout, `OSError`, `ValueError`, or empty/unparsable seed data MUST NOT escape; streaming MUST continue unseeded with current behavior and errors. | Failed GET; S3. |
| R3 | Polling MUST NOT replay old assistant text, `✅/⚠️/🔧` tool statuses, `🧠 thinking…`, or resolved-question deltas. | Zero replay; S1, S4. |
| R4 | Post-prompt growth MUST emit only text suffixes, fresh tool statuses once, and a new `question` delta that stops for an answer. | New turn; S1, S4. |
| R5 | Unpinned fresh sessions MUST perform no seed GET and MUST behave unchanged. | GET count; S2. |
| R6 | Event-bus handling MUST remain live-events-only and MUST NOT replay history. | Existing tests; S8. |
| R7 | `opencode_chat`, error strings, and yielded tuple kinds MUST remain unchanged. | Compatibility; S8. |
| R8 | After NUL normalization, PID discovery MUST match exact adjacent `--port`, `<port>` tokens or exact `--port=<port>`. | Both forms; S5. |
| R9 | Matching MUST use token equality, never substring/prefix matching. | Guards; S6, S7. |
| R10 | Candidates MUST contain `opencode` and `serve`; unreadable/malformed `/proc` entries MUST be skipped without raising. | `/proc` cases; S5, S7. |
| R11 | Existing NUL space-form tests MUST stay green; equals-form and prefix-guard tests MUST be added. | Suite; S5, S6. |
| R12 | Existing PID consumers MUST remain unchanged and gain equals-form discovery only. | Consumers; S5. |

## Scenarios

### S1: Resumed follow-up survives bus closure
- GIVEN pinned history containing every seeded part type
- WHEN the prompt posts, the bus closes, and polling sees history plus growing new text
- THEN one seed GET occurs, no history replays, and only the new suffix streams

### S2: Fresh session bus closure
- GIVEN no session pin exists
- WHEN its event bus closes
- THEN no seed GET occurs and polling remains unchanged

### S3: Seed fetch degrades safely
- GIVEN the seed GET fails by any R2 condition
- WHEN resumed streaming proceeds
- THEN polling runs unseeded without an exception or changed client error

### S4: Old and new questions
- GIVEN an old resolved question was seeded
- WHEN polling sees it and a post-prompt question
- THEN only the new `question` delta emits and stops the stream

### S5: Equals-form manual serve
- GIVEN a cmdline has exact `opencode`, `serve`, and `--port=18999` tokens
- WHEN discovery searches for 18999
- THEN its PID is returned to unchanged consumers

### S6: Prefix ports are rejected
- GIVEN cmdlines contain `--port 18999` or `--port 189990`
- WHEN discovery searches for 1899 or 18999 respectively
- THEN neither longer port matches

### S7: Two serves on one machine
- GIVEN two serves advertise different ports and another `/proc` entry is unreadable
- WHEN either exact port is requested
- THEN only its serve matches; the unreadable entry is skipped without error

### S8: Streaming compatibility
- GIVEN resumed streaming receives live bus deltas
- WHEN streaming and blocking suites run
- THEN live handling, tuple kinds, error strings, and blocking behavior remain unchanged

## Edge Cases

Empty histories seed empty state; repeated old parts remain suppressed; malformed seeds degrade per R2; malformed cmdlines are skipped.

## Out of Scope

Blocking behavior, stream-exit hygiene, spawn arguments, and recycle thresholds are excluded.

## Relation to Adjacent Capabilities

`opencode-bridge-stream-exit-hygiene` (cycle 6) is untouched. This is additive to `opencode-bridge-blocking-path`.
