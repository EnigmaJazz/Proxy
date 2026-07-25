# Design: Professional as Default

## Overview

Make Professional the resident auto CHAT/TOOL/CODE destination while retaining contention fallback, exact profiles, and R19 authority (`proposal.md:11-27`; `spec.md:9-47`).

## Architecture

```text
request → frontdesk/mid-tool lock → resolve_route_for_lane_a
        → Professional or contention Lifeboat → exact profile → stream
queue empty → SystemdController authority → preserve Professional / unload specialist
scanner → deterministic rows → model_profiles.yaml → sync --check
```

| Decision | Choice | Rationale |
|---|---|---|
| Profile generation | Scanner emits both rows | Hand edits are overwritten and fail the hard drift gate (`tools/sync_model_profiles.py:446-464`). |
| Empty-queue authority | `SystemdController.active_heavy_model` | It owns `hot_swap`/`unload_all_heavy`; `AppState` is only a mirrored routing view (`systemd.py:261-318,371-379`; `proxy.py:413-425`). |

## Detailed design

### 3.1 `routing.py` changes

Change all three default rows (`routing.py:213-221`) and the fallback default (`routing.py:563`):

```python
ROUTE_MAP = {
    "CHAT": "professional", "TOOL": "professional", "CODE": "professional",
    "SCHOLAR": "scholar", "PROFESSIONAL": "professional",
    "CREATIVE": "creative", "ARCHITECT": "architect",
}
model_key = ROUTE_MAP.get(intent, "professional")
```

Replace the CHAT/TOOL blocks (`routing.py:565-659`) with:

```python
professional_available = (
    not systemd.is_gpu_occupied()
    or systemd.active_heavy_model == "professional"
)
if intent == "CHAT" and complexity == "low":
    model_key = "professional" if professional_available else "lifeboat"
elif intent == "TOOL":
    model_key = "professional" if professional_available or has_tool_history else "lifeboat"
else:
    model_key = ROUTE_MAP.get(intent, "professional")
port = await systemd.get_port(model_key)
is_cpu_fallback = model_key == "lifeboat"
hardware_path = "cpu" if is_cpu_fallback else "gpu"
tools_required = tools_required or (intent == "TOOL" and has_tool_history)
```

Resident Professional counts as available. A different specialist uses Lifeboat, except mid-tool history forces Professional because Lifeboat rejects that history (`routing.py:617-643`). `CPU_MODELS` and `is_gpu_occupied` stay unchanged (`routing.py:223-227`; `systemd.py:381-393`).

### 3.2 Profile rows

Regenerate adjacent rows at `config/model_profiles.yaml:58-70`; no `default` row:

```yaml
- {model: professional, intent: chat, architecture: qwen35moe, file_type: 15, context_window: 262144, max_tokens: 235929, temperature: 0.7, top_p: 1.0, thinking_budget_tokens: 0, seed: null, top_logprobs: null, response_format: null, n: 1}
- {model: professional, intent: code, architecture: qwen35moe, file_type: 15, context_window: 262144, max_tokens: 235929, temperature: 0.2, top_p: 0.95, thinking_budget_tokens: 4096, seed: null, top_logprobs: null, response_format: null, n: 1}
```

Exact lookup already precedes wildcard fallback (`profile_loader.py:54-80`).

### 3.3 Profile sync scanner

Change `_MODEL_INTENTS` to `dict[str, tuple[str, ...]]`, with `"professional": ("chat", "code")` and singleton tuples elsewhere (`tools/sync_model_profiles.py:36-50`). Add `intent: str` to `build_profile_entry`, removing its internal lookup (`tools/sync_model_profiles.py:291-318`), then emit rows deterministically:

```python
for intent in _MODEL_INTENTS.get(model_key, ("chat",)):
    rows.append(build_profile_entry(model_key, intent, meta, entry, hf_params))
```

Tuple order is deterministic; defaults and GGUF produce the required values (`tools/sync_model_profiles.py:52-70`). Unchanged `profiles_equal` keeps `sync --check` blocking drift (`tools/sync_model_profiles.py:459-506`).

### 3.4 Queue unload gate

Extract a testable `_cleanup_idle_heavy`; call it only at `proxy.py:449-453` when `pending` is empty:

```python
active = systemd.active_heavy_model
if active is None and await systemd.is_active("professional"):
    systemd.active_heavy_model = active = "professional"
if active == "professional":
    state.active_heavy_model = "professional"
    return
await systemd.unload_all_heavy()
state.active_heavy_model = None
```

The probe reconciles an externally started Professional; an `OSError` is logged and skips destructive cleanup. Specialists still unload normally, and later requests can hotswap through the existing wrapper (`routes.py:867-923`; `systemd.py:261-303`).

### 3.5 Tests

Create `tests/test_professional_default.py`: pin CHAT/TOOL/CODE destinations, resident-vs-specialist occupancy, mid-tool Professional, exact payload profiles (Scenarios 1-3), empty-queue preservation and specialist cleanup/hotswap (Scenarios 7-8). Modify `tests/glass_pipe_test.py:464-556` for two-row scanner generation and `--check` drift; extend its committed-profile assertions. Keep R19 direct/opt-in regressions in `tests/test_client_named_model.py:149-250` for Scenarios 4-6; extend `_NoOpSystemd` call recording (`tests/conftest.py:82-109`).

## Data flow

`model:auto` + CHAT → frontdesk → Professional (free/resident GPU) → `resolve("CHAT", "professional")` selects chat row → payload `0.7/1.0/235929/0` → existing startup wrapper avoids a swap when already active → SSE (`routes.py:397-537,833-984`).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Special blocks override `ROUTE_MAP` | Replace both blocks and test occupancy states (`routing.py:565-659`). |
| Scanner returns one row | Tuple intents + generated-file drift test; never hand-edit (`tools/sync_model_profiles.py:400-464`). |
| State divergence unloads Professional | Controller authority, external-service reconciliation, fail-safe skip. |

## Threat Matrix

Routing/process integration makes the review mandatory. Routing RED cases are specified in §3.5; the reference matrix is otherwise N/A:

| Boundary | Minimum cases | Applicability | Design response | Planned RED tests |
|---|---|---|---|---|
| Documentation-like paths | `requirements.txt`, `CMakeLists.txt`, executable MDX, `README.sh` | N/A — no file classification/execution | None | None |
| Git repository selection | `git -C`, relative/absolute paths | N/A — no repository selection | None | None |
| Commit state | staged, `commit -a`, empty index | N/A — no commit automation | None | None |
| Push state | tracking, first push, refspec | N/A — no push automation | None | None |
| PR commands | `--head`, environment prefix, composition | N/A — no PR commands | None | None |

## File changes / rollout

Modify `routing.py`, `proxy.py`, `tools/sync_model_profiles.py`, `config/model_profiles.yaml`, `tests/conftest.py`, and `tests/glass_pipe_test.py`; create `tests/test_professional_default.py`. No migration or feature flag; rollback reverts these files (`proposal.md:102-104`).

## Open questions

None. Scanner determinism and queue-state authority are resolved above.
