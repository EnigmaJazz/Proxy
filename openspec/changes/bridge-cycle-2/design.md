# Design: Bridge Cycle 2 — Harden the OpenCode Serve Liveness Probe

## Technical Approach

Treat the existing buffered `GET /config` as a transport-liveness probe, not an endpoint-health check. In `is_opencode_serve_running()` (`opencode_bridge.py:406`), replace `timeout=5.0` and `return resp.status_code == 200` (`:410-411`) with `await client.get(..., timeout=3.0)` followed by `return True`. Keep the existing `(httpx.HTTPError, OSError) -> False` boundary. Do not call `raise_for_status()` or explicitly read the body; completion of buffered `.get()` is the received-response boundary.

## Architecture Decisions

| Decision | Alternatives considered | Rationale |
|---|---|---|
| Any received 2xx/3xx/4xx/5xx response means alive | Require 200; accept only 2xx | The port is held and a duplicate spawn would fail even when `/config` is version-specific, missing, redirecting, or degraded. |
| Use the existing buffered `.get()` with a 3.0-second timeout | Streaming request; explicit body read; retain 5.0s | This preserves the current HTTPX lifecycle while bounding each probe; buffered `.get()` already defines completion without extra reads. |
| Preserve the function interface and consumers | Add health states or edit each caller | A boolean transport contract is sufficient and lets every lifecycle consumer inherit the fix atomically. |

## Data Flow

    spawn gate / drain / spawn wait / cycle driver
                       │
                       ▼
          is_opencode_serve_running()
             │ response (any status) ──→ True
             └ HTTPError or OSError ───→ False

No signature or call-site changes are permitted. Existing calls in `ensure_opencode_serve()` (`opencode_bridge.py:431,442`), `_spawn_serve()` (`:509`), and `scripts/sdd_autonomous_cycle.py:165` inherit the semantics.

## File Changes

| File | Action | Description |
|---|---|---|
| `tests/test_opencode_bridge.py` | Modify first | Add RED regressions and additive fake-client scripting. |
| `opencode_bridge.py` | Modify second | Restate the liveness docstring and make the exact probe edit above. |

## Interfaces / Contracts

`async def is_opencode_serve_running() -> bool` remains unchanged. `True` means a complete HTTP response was received; `False` means `httpx.HTTPError` (including connect/read timeouts) or `OSError`. It does not mean `/config` is healthy.

## Testing Strategy

| Layer | What to test | Approach |
|---|---|---|
| Unit | Status and timeout semantics | Keep existing `test_running_when_200` and ConnectError test untouched. Add a parameterized `[200, 302, 404, 500] -> True` test. Add `_FakeClient.raise_timeout_on = ""`; in `get()`, immediately after the existing `raise_on == "get"` branch, raise `httpx.ReadTimeout("read timed out")` when `raise_timeout_on == "get"`, then assert `False`. Record passed GET timeouts additively and assert `3.0`. `_FakeResp` deliberately has no `raise_for_status`/body-read API, guarding those exclusions. |
| Integration | Duplicate-spawn prevention | Parameterize 404/500, use the real probe and `ensure_opencode_serve()`, patch `_config_mtime` to return `None`, replace `_spawn_serve` with an async recorder, and assert `True` with zero spawn calls. The shared fake client is hermetic and never contacts a real serve. |
| Lifecycle | Capped drain | Script initial/drain liveness, patch sleep/recycle/spawn, and assert at most four drain probes and four `0.25s` sleeps. The operational worst case is approximately `4 × (3.0s + 0.25s) = 13s`. |

Strict TDD order: (1) add/run tests and observe status, non-spawn, and 3.0s assertions fail; (2) edit production; (3) run `pytest tests/test_opencode_bridge.py -q`; (4) run `pytest tests/ -q`.

## Threat Matrix

Process integration triggers matrix review, but this change does not cross any listed VCS/executable-classification boundary.

| Boundary | Applicability | Design response | Planned RED tests |
|---|---|---|---|
| Documentation-like paths | N/A — no file classification or execution | None | None |
| Git repository selection | N/A — no Git invocation | None | None |
| Commit state | N/A — no commit operation | None | None |
| Push state | N/A — no push operation | None | None |
| PR commands | N/A — no PR command composition | None | None |

## Migration / Rollout

No migration, configuration, dependency, or feature flag is required. Roll back by reverting the probe and tests. Do not touch `opencode-serve-config.opencode.jsonc`.

Slow boot beyond 3 seconds may transiently return `False`; later polling self-heals, as accepted by the spec. A non-serve responder on the port intentionally counts as alive because spawning would collide. Redirects are not followed for classification; the received 3xx itself proves liveness. ReadTimeout, ConnectError/ConnectTimeout, and OSError remain down results.

## Open Questions

None.
