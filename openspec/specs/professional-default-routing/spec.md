# Professional Default Routing Specification

## Purpose

Make Professional the resident default for auto-routed CHAT, TOOL, and CODE requests while preserving explicit model choice, profile authority, and specialist hotswaps. Anchors REQ-1 through REQ-8.

## Requirements

### REQ-1: Auto-routed CHAT uses Professional

Auto-routed CHAT requests SHALL select `professional`, not `chatter` or the Lifeboat fallback.
**Rationale:** Avoid swaps to a weaker default.

#### Scenario-1: Auto CHAT profile

- GIVEN `model: "auto"` is classified CHAT
- WHEN routing and parameters resolve
- THEN Professional receives the exact chat-profile values (temperature=0.7, top_p=1.0, max_tokens=235929, thinking_budget_tokens=0)

### REQ-2: Auto-routed TOOL uses Professional

Auto-routed TOOL requests SHALL select `professional`, including mid-tool-flow requests, not `worker` or Lifeboat.
**Rationale:** Use the validated native structured-tool support.

#### Scenario-2: Auto TOOL profile

- GIVEN `model: "auto"` is classified TOOL, including mid-tool-flow
- WHEN routing and parameters resolve
- THEN Professional receives the exact code-profile values (temperature=0.2, top_p=0.95, max_tokens=235929, thinking_budget_tokens=4096)

### REQ-3: Auto-routed CODE remains Professional

Auto-routed CODE requests SHALL continue to select `professional`.
**Rationale:** Preserve the established CODE destination.

#### Scenario-3: Auto CODE preserved

- GIVEN `model: "auto"` is classified CODE
- WHEN routing and parameters resolve
- THEN Professional receives the exact code-profile values

### REQ-4: Exact Professional profiles

`config/model_profiles.yaml` SHALL contain exact `professional/chat` and `professional/code` rows, with no Professional `default` bucket: chat SHALL use `temperature=0.7`, `top_p=1.0`, `max_tokens=235929`, `thinking_budget_tokens=0`; code SHALL use `0.2`, `0.95`, `235929`, `4096` respectively.
**Rationale:** Prevent wildcard fallback and tune by intent.

### REQ-5: Explicit model selection remains available

R19 SHALL honor any valid explicit model, including `professional`, `chatter`, `worker`, `coder`, `scholar`, `creative`, and `architect`; `chatter` and `worker` SHALL remain valid opt-ins.
**Rationale:** Preserve direct-client compatibility.

#### Scenario-4: Explicit Professional

- GIVEN a client names `professional` and supplies all four R1/R7 fields
- WHEN the request is built
- THEN Professional receives the client values verbatim

#### Scenario-5: Chatter and Worker opt-in

- GIVEN a client names `chatter` or `worker`
- WHEN R19 resolves the request
- THEN the named model is used instead of Professional

#### Scenario-6: Specialist opt-in

- GIVEN a client names `coder` or another valid specialist with sampling values
- WHEN R19 resolves and builds the request
- THEN that specialist and the client values are used

### REQ-6: Direct-call sampling remains client-owned

For every explicit model, R1/R7 client-wins SHALL remain in force for client-sent `temperature`, `top_p`, `max_tokens`, and `thinking_budget_tokens`; profile authority SHALL remain limited to `model: "auto"` and the existing AGENTIC dream fast-path.
**Rationale:** Default routing MUST NOT broaden proxy authority.

### REQ-7: Professional remains resident across an empty queue

The queue worker SHALL NOT call `unload_all_heavy` or clear active-heavy state when the active heavy model is `professional` and the queue becomes empty.
**Rationale:** Prevent a 30–120 second cold start on the next default request.

#### Scenario-7: Empty queue preserves Professional

- GIVEN Professional is active and the queue becomes empty
- WHEN queue cleanup runs
- THEN Professional remains active and unload is not invoked

### REQ-8: Specialist transitions remain functional

The REQ-7 gate SHALL NOT block hotswaps from Professional to `coder`, `scholar`, `creative`, or `architect`, nor normal cleanup while a specialist is active.
**Rationale:** Preserve one-heavy specialist behavior.

#### Scenario-8: Specialist hotswap

- GIVEN Professional is active and a specialist is explicitly selected or classified
- WHEN startup orchestration runs
- THEN the specialist hotswap occurs and REQ-7 does not suppress it
- AND an explicit client override from a Lifeboat fallback route MUST clear `is_cpu_fallback` to avoid suppressing the hotswap wrapper
