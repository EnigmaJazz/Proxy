# OpenCode Bridge Specification

## Purpose

Governs the proxy module routing select chat requests to a headless `opencode serve` backend (`model: "opencode"`, `/opencode` command, queue-worker escalation): how permission-question events reach the inbound SSE client, how an autonomous SDD cycle treats them, and how the serve stays alive across a full cycle.

## Requirements

### REQ-1: Interactive mode relays permission-question events

In interactive bridge mode the proxy MUST surface every backend permission-question event to the inbound SSE client as an answerable question. The relay set MUST include `external_directory`, `bash`, `write`, `edit`. The proxy MUST NOT silently ignore or auto-deny `write`/`edit` events, and MUST relay on every bridge path including `/opencode`. (Policy: relay set EXTENDED to include `write`/`edit` — fixes the `opencode_bridge.py:188` ignore bug.)

#### Scenario-1: write event relayed

- GIVEN interactive mode and a backend `write` question
- WHEN the proxy consumes it
- THEN it forwards it to the SSE client as a question and waits for an answer

#### Scenario-2: /opencode path relays write

- GIVEN a `/opencode` command triggers a `write` question upstream
- WHEN the proxy handles the response
- THEN the event is relayed on the same terms as the stream path

### REQ-2: Autonomous mode auto-allows permission events (POLICY DECISION)

**Decision:** In SDD-autonomous mode (model `"opencode-sdd"`, the user pre-approves the whole cycle), permission events arising inside the cycle — `write`, `edit`, `bash`, and git `commit`/`push`/`reset` asks — MUST be auto-allowed. Auto-allow MUST be cycle-scoped; interactive bridge mode keeps REQ-1 relay unchanged.

**Rationale:** One unanswered ask parks the backend tool; the 120s wedge detector then kills the session — fatal to a one-turn cycle. The cycle is the user's explicit pre-approved request, so its asks are pre-approved by construction. Git asks are included: a stall on `git commit` is as fatal as one on `write`.

#### Scenario-1: write auto-allowed in autonomous mode

- GIVEN autonomous mode and a `write` question
- WHEN the proxy consumes it
- THEN it answers allow without waiting for a client answer and the cycle continues unwedged

#### Scenario-2: git commit auto-allowed in autonomous mode

- GIVEN autonomous mode and a `git commit`/`push`/`reset` ask
- WHEN the proxy consumes it
- THEN it answers allow and the cycle does NOT stall on the git ask

#### Scenario-3: interactive mode does not auto-allow write

- GIVEN interactive mode and a `write` question
- WHEN the proxy consumes it
- THEN REQ-1 relay fires (auto-allow does NOT fire)

### REQ-3: /opencode command route parity

The `/opencode` route MUST apply the same relay/auto-allow policy as the `model: "opencode"` stream path. If it cannot share the stream relay, the limitation MUST be documented and MUST NOT silently drop `write`/`edit` events.

#### Scenario-1: /opencode uses the stream relay path

- GIVEN a `/opencode` request
- WHEN the proxy handles it
- THEN it flows through `opencode_chat_stream` (or equivalent) and write/edit events surface identically to the stream path

#### Scenario-2: partial parity documented

- GIVEN the route cannot use the stream path
- WHEN the bridge ships
- THEN the spec documents which types are NOT relayed and no write/edit event is silently ignored

### REQ-4: Serve stability mode preserves fallback replay (POLICY DECISION)

**Decision:** The stability mode MUST preserve the `opencode-rate-limit-fallback` plugin replay — sub-agent "Task cancelled" reconciliation depends on it. The spec REQUIRES the OUTCOME (a full cycle completes without wedge/crash AND fallback replay stays functional) and does NOT prescribe the mechanism (`--pure`, plugin reduction, serve-only config dir). Mechanism is a design choice gated by empirical one-cycle validation; the first candidate completing a cycle without wedge/crash while keeping fallback replay wins.

#### Scenario-1: full cycle completes under the chosen mode

- GIVEN the chosen stability mode is active
- WHEN one full cycle runs proposal → archive
- THEN it completes without wedge or crash

#### Scenario-2: fallback replay preserved

- GIVEN the chosen mode and a sub-agent hits the rate limit mid-cycle
- WHEN the rate-limit-fallback plugin replays on a mapped fallback model
- THEN the replay completes and the cycle continues (cancelled task recoverable)

#### Scenario-3: candidate that wedges OR loses fallback is rejected

- GIVEN a candidate completes a cycle but wedges, crashes, or loses fallback replay
- THEN it is rejected regardless of partial success and another candidate is tried

### REQ-5: Config-drift handling for opencode.json edits

When `opencode.json` is hot-edited while a serve runs, the proxy MUST either auto-detect the drift and recycle the serve, or document the required manual recycle step. Drift MUST NOT silently leave a stale serve serving the old config.

#### Scenario-1: edit auto-recycles the serve

- GIVEN a serve runs for an autonomous cycle
- WHEN `opencode.json` is modified on disk
- THEN the proxy recycles the serve before the next request and the cycle continues against the new config

#### Scenario-2: manual recycle documented

- GIVEN auto-detect is not implemented
- WHEN the spec ships
- THEN it documents the manual recycle step and the limitation is visible to the operator

### REQ-6: Driver scripts parameterized; stall handling defined

`sdd_bridge_cycle.py` and `sdd_autonomous_cycle.py` MUST accept the change name as a parameter and MUST NOT hardcode a change name (e.g. `"bridge-docs"`). Each driver MUST define stall handling: what counts as a stall, how it is detected, and what recovery (recycle/abort/report) runs.

#### Scenario-1: driver runs with an arbitrary change name

- GIVEN a change name `my-change`
- WHEN the driver is invoked with it
- THEN it runs the cycle against `my-change` with no hardcoded override

#### Scenario-2: stall detected and handled

- GIVEN a driver runs and the backend parks past the stall threshold
- WHEN the stall is detected
- THEN the defined recovery (recycle, abort, or report) runs

### REQ-7: Three consecutive end-to-end autonomous SDD cycles

Three consecutive full autonomous SDD cycles (proposal → archive) MUST complete end-to-end with zero wedge or crash. Any cycle wedging or crashing fails the criterion, even if the others succeed.

#### Scenario-1: three consecutive cycles green

- GIVEN the bridge with all fixes applied
- WHEN three consecutive full cycles run
- THEN all complete proposal → archive with zero wedge/crash

#### Scenario-2: a wedge in any cycle fails it

- GIVEN three cycles are running
- WHEN any one wedges or crashes
- THEN the criterion fails regardless of the others

## Notes

- **Policy decisions for design to implement:** (1) git ask-rules auto-allowed in autonomous mode; (2) `write`/`edit` auto-allowed in autonomous mode and relayed in interactive with the relay set extended to include them; (3) stability mode REQUIRES the outcome, not the mechanism — all in REQ-2/REQ-1/REQ-4 respectively.
- Bridge test hermeticity is in the `test-infrastructure` delta (REQ-2 there).
- `opencode` upstream patches and `glass-pipe-followups` are out of scope; this spec covers proxy-side relay, policy, serve lifecycle, and drivers only.