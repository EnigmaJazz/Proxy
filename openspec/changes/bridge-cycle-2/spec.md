# OpenCode Serve Liveness Specification

## Purpose

Define serve liveness by transport reachability rather than HTTP success so a process holding the configured port is not duplicated or recycled merely because it returns a non-2xx status.

## Requirements

### REQ-1: Any HTTP response proves liveness

`is_opencode_serve_running()` MUST return `True` after receiving any HTTP response, including 2xx, 3xx, 4xx, and 5xx. It MUST NOT call `raise_for_status()` or interpret the status code. It MUST return `False` only when the probe raises `httpx.HTTPError` (including `ConnectError`, `ConnectTimeout`, or `ReadTimeout`) or `OSError`; these failures mean no usable transport response was received.

#### Scenario-1: Status codes are alive

- GIVEN the serve probe receives status 200, 302, 404, or 500
- WHEN `is_opencode_serve_running()` completes
- THEN it returns `True` for every status

#### Scenario-2: Transport failures are down

- GIVEN the probe raises `ConnectError`, `ConnectTimeout`, `ReadTimeout`, or `OSError`
- WHEN `is_opencode_serve_running()` handles the failure
- THEN it returns `False`

### REQ-2: Probe operation is bounded and buffered

The probe MUST use a 3.0-second client timeout instead of 5.0 seconds. It SHALL perform buffered `.get()` inside the asynchronous HTTP client context manager and MUST NOT explicitly read the response body; completion of `.get()` is the received-response boundary.

#### Scenario-1: Probe exceeds its response window

- GIVEN a connection is established but no complete response arrives within 3.0 seconds
- WHEN buffered `.get()` raises `ReadTimeout`
- THEN the probe returns `False` without an explicit body-read operation

#### Scenario-2: Slow first boot self-heals

- GIVEN a first boot begins answering only after the 3.0-second probe window
- WHEN the first poll times out and a later poll receives a response
- THEN the first result is transiently `False` and the later result is `True`
- AND this transient false-negative is intentional accepted behavior

### REQ-3: Existing consumers inherit transport-liveness semantics

The function name and signature MUST remain unchanged. Existing call sites—`ensure_opencode_serve()`'s spawn gate, the config-drift drain loop, the additional `opencode_bridge.py` call near line 509, and `scripts/sdd_autonomous_cycle.py` force-recycle—MUST inherit the new result without call-site edits. A live responder returning 404 or 5xx MUST prevent `ensure_opencode_serve()` from spawning another process and MUST NOT trigger status-based force-recycle behavior.

#### Scenario-1: Non-success responder blocks duplicate spawn

- GIVEN an existing serve answers the probe with 404 or 500
- WHEN `ensure_opencode_serve()` evaluates its spawn gate
- THEN no replacement or duplicate serve process is spawned

#### Scenario-2: Config-drift drain remains bounded

- GIVEN the old serve continues responding and therefore holds the port
- WHEN the config-drift drain performs its capped four checks
- THEN “alive” means transport responding, regardless of status
- AND the drain terminates after approximately 13 seconds at worst (4 × approximately 3.25 seconds)

### REQ-4: Regression delivery is narrow and test-first

Regression tests MUST be added before the production change and MUST cover 200, 3xx, 404, and 500 as `True`; `ConnectError` and `ReadTimeout` as `False`; non-spawn on live 404/5xx; and bounded drain termination. The implementation diff MUST touch only the probe and its tests. `pytest tests/ -q` MUST pass, and `opencode-serve-config.opencode.jsonc` MUST remain untouched.

#### Scenario-1: Acceptance suite passes

- GIVEN the probe and regression tests are the only changed implementation areas
- WHEN `pytest tests/ -q` runs
- THEN the full suite passes
- AND `opencode-serve-config.opencode.jsonc` has no diff

## Out of Scope

- `_serve_health` and `_find_serve_pid` recycle machinery
- `/proc` memory-pressure behavior
- Chat, streaming, permission-relay, and wedge-detector paths
- New configuration keys, dependencies, or documentation
