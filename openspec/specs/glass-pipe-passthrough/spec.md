# Glass-Pipe Passthrough Specification

## Purpose

Client-sent sampling parameters and the `tools` array reach the downstream model unchanged by default. Intent-based defaults fill only gaps the client left absent. The implicit mid-tool-flow route lock is client-disarmable. Anchors R1, R7, R11, R3, R13.

## Requirements

### REQ-1: Client-sent sampling parameters win (R1)

The proxy SHALL forward client-sent `temperature`, `top_p`, and `max_tokens` verbatim. Intent-based defaults SHALL apply ONLY when the corresponding client field is absent from the request body. The fallback order is: client value → intent default → global default. Each intent default MUST be documented in code comments as a fallback value. REQ-6 (auto-routed authority) defines the sole exception: in auto-routed or dream/soul fast-path mode, client values for the R11 field set are replaced, not preserved.

#### Scenario-1: Client sends temperature for CODE intent

- GIVEN a client sends `{"temperature": 0.7}` for a DIRECT (non-auto-routed) request classified as CODE
- WHEN the proxy builds the forwarded payload
- THEN the model receives `temperature: 0.7`
- AND the CODE intent default (0.1) is NOT applied

#### Scenario-2: Client omits temperature

- GIVEN a client omits `temperature` for a request classified as CODE
- WHEN the proxy builds the forwarded payload
- THEN the model receives the CODE intent default `temperature: 0.1`

### REQ-2: thinking_budget_tokens is client-overrideable (R7)

The proxy SHALL forward client-sent `thinking_budget_tokens` verbatim. The intent default for `thinking_budget_tokens` SHALL apply ONLY when the client omitted the field.

#### Scenario-1: Client sets thinking_budget_tokens

- GIVEN a client sends `{"thinking_budget_tokens": 8192}` for a CODE intent
- WHEN the proxy forwards the payload
- THEN the model receives `thinking_budget_tokens: 8192`

#### Scenario-2: Client omits thinking_budget_tokens

- GIVEN a client omits `thinking_budget_tokens` for a CODE intent
- WHEN the proxy forwards the payload
- THEN the intent default applies, not 8192

### REQ-3: Forward the full OpenAI field set (R11)

The proxy SHALL forward every OpenAI chat-completion field the client sent, not only a hardcoded subset. The forward set MUST include at minimum: `tool_choice`, `parallel_tool_calls`, `frequency_penalty`, `presence_penalty`, `logit_bias`, `seed`, `user`, `response_format`, `top_logprobs`, `n`, `logprobs`. Client-sent values win on every field, with the sole exception of REQ-6 auto-routed authority, which replaces — not gaps — the listed R11 fields with profile values.

#### Scenario-1: Client sends response_format and seed on a direct call

- GIVEN a client sends `{"model": "deepseek-r1", "response_format": {"type": "json_object"}, "seed": 42}`
- WHEN the proxy forwards the payload
- THEN both fields are present in the forwarded payload with the client's values

#### Scenario-2: Client omits optional OpenAI fields

- GIVEN a client sends only `messages` and `stream` on a direct call
- WHEN the proxy forwards the payload
- THEN no optional OpenAI field is injected by the proxy

### REQ-4: Lane B / IDE tools passthrough (R3)

The proxy SHALL forward the client's `tools` array unchanged for every Lane B / IDE caller. The unconditional `tools = None` assignment for Lane B MUST be removed. No `X-Kinver-Strip-Tools` opt-in header is introduced in this pass. The breaking change for any client that depended on the strip MUST be documented in the CHANGELOG.

#### Scenario-1: Lane B caller with tools

- GIVEN a Lane B / IDE caller sends a non-empty `tools` array
- WHEN the proxy forwards the payload
- THEN `tools` is present and unchanged at the model

#### Scenario-2: Lane B caller without tools

- GIVEN a Lane B / IDE caller sends no `tools` field
- WHEN the proxy forwards the payload
- THEN `tools` is absent or null at the model (no proxy injection)

### REQ-5: Mid-tool-flow lock is client-disarmable (R13)

The proxy SHALL honor an explicit opt-out header `X-Kinver-Allow-Mid-Tool-Switch: true` to disable the implicit mid-tool-flow route lock for that request. When the header is absent, the current implicit lock behavior SHALL be preserved unchanged.

#### Scenario-1: Client opts out of mid-tool-flow lock

- GIVEN a request mid-tool-flow sends `X-Kinver-Allow-Mid-Tool-Switch: true`
- WHEN the proxy resolves the route
- THEN the implicit tool-flow lock is disabled for that request

#### Scenario-2: Client does not opt out

- GIVEN a request mid-tool-flow sends no opt-out header
- WHEN the proxy resolves the route
- THEN the existing implicit mid-tool-flow lock behavior applies

### REQ-6: Auto-routed parameter authority (R17)

When the proxy owns model selection — triggered by auto-routed mode (`requested_model == "auto"`) OR the dream/soul fast-path — the proxy SHALL replace client-sent values across the full R11 field set with profile-sourced values. The authoritative field set MUST include: `temperature`, `top_p`, `max_tokens`, `thinking_budget_tokens`, `seed`, `top_logprobs`, `response_format`, `n`. Client values for these fields SHALL NOT be consulted in triggered mode. Authority MUST NOT be optional: no opt-out header and no per-client override exist. For direct calls (client picked the model), REQ-1 and REQ-3 client-wins SHALL remain in force unchanged. The substitution SHALL be observable via the `params_replaced` SSE event (see glass-pipe-stream-integrity REQ-7).

#### Scenario-1: Auto-routed call with client temperature

- GIVEN a client sends `{"model": "auto", "temperature": 0.9}`
- WHEN the proxy builds the forwarded payload in auto-routed mode
- THEN the model receives the profile `temperature`, NOT 0.9
- AND a `params_replaced` SSE event is emitted

#### Scenario-2: Dream/soul fast-path with client seed

- GIVEN the dream/soul fast-path is active and the client sends `{"seed": 42}`
- WHEN the proxy forwards the payload
- THEN the model receives the profile `seed`, NOT 42
- AND a `params_replaced` SSE event is emitted

#### Scenario-3: Direct call unaffected

- GIVEN a client sends `{"model": "deepseek-r1", "temperature": 0.9}`
- WHEN the proxy forwards the payload
- THEN the model receives `temperature: 0.9` (REQ-1 client-wins unchanged)

## Notes

- Open question resolved: no `X-Kinver-Strip-Tools` opt-in header in this pass (R3 mitigation deferred). Strip-the-tools behavior is removed outright; document the breaking change in CHANGELOG. Loop-detection strip (R4) still applies per-turn.
- R17 swaps the *source* of intent defaults (committed `config/model_profiles.yaml`, derived by the R18 filesystem scanner from GGUF metadata + per-file HF sampling + operator overrides) for the auto-routed path, not their values. Per-intent tuning in `routes.py` stays unchanged for direct calls. See `model-profile-sync` spec.
- No opt-out header and no per-client override: authority is unconditional in triggered mode by design.