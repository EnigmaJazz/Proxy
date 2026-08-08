# Delta for Test Infrastructure

## ADDED Requirements

### REQ-2: Bridge test suite hermetic — never kills a live opencode serve

The bridge test suite (`tests/test_opencode_bridge.py`) MUST be hermetic: no test run MAY kill or recycle a live `opencode serve`. The serve recycle path (e.g. `_recycle_serve_if_low_memory` or any equivalent that invokes `os.kill` on the serve pid) MUST be patched to a no-op for ALL real-stream tests exercising `opencode_chat_stream` — not only a subset. The serve pid MUST remain unchanged across a full `tests/test_opencode_bridge.py` run. The suite MUST include a regression guard asserting that `os.kill` (or the equivalent recycle primitive) never fires against the serve pid during the run. This requirement does NOT alter REQ-1 (glass_pipe harness); it adds bridge-specific hermeticity.

#### Scenario-1: serve pid unchanged across full bridge suite

- GIVEN a live `opencode serve` is running with pid P
- WHEN the full `tests/test_opencode_bridge.py` suite runs
- THEN pid P is still alive and unchanged at the end of the run
- AND no test issued `os.kill` against pid P

#### Scenario-2: recycle path patched for all real-stream tests

- GIVEN the suite has N real-stream tests exercising `opencode_chat_stream` (currently ~15 distinct tests / 17 real invocation lines)
- WHEN the suite is collected and run
- THEN every one of those tests has the recycle path patched to a no-op
- AND the patch is not limited to a single test entry

#### Scenario-3: os.kill regression guard

- GIVEN the hermeticity patches are in place
- WHEN any test invokes a recycle primitive
- THEN the suite's regression guard fails the run if `os.kill` actually fires against the serve pid

#### Scenario-4: live serve survives a stream-test run

- GIVEN a live serve process exists before the suite starts
- WHEN the suite runs to completion
- THEN the same serve process is still serving (no recycle, no respawn)