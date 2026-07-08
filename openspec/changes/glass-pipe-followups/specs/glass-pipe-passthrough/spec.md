# Delta for Glass-Pipe Passthrough

## ADDED Requirements

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

## MODIFIED Requirements

### REQ-1: Client-sent sampling parameters win (R1)

The proxy SHALL forward client-sent `temperature`, `top_p`, and `max_tokens` verbatim. Intent-based defaults SHALL apply ONLY when the corresponding client field is absent from the request body. The fallback order is: client value → intent default → global default. Each intent default MUST be documented in code comments as a fallback value. REQ-6 (auto-routed authority) defines the sole exception: in auto-routed or dream/soul fast-path mode, client values for the R11 field set are replaced, not preserved.

(Previously: client-sent `temperature`/`top_p`/`max_tokens` won unconditionally.)

#### Scenario-1: Client sends temperature for CODE intent

- GIVEN a client sends `{"temperature": 0.7}` for a DIRECT (non-auto-routed) request classified as CODE
- WHEN the proxy builds the forwarded payload
- THEN the model receives `temperature: 0.7`
- AND the CODE intent default (0.1) is NOT applied

#### Scenario-2: Client omits temperature

- GIVEN a client omits `temperature` for a request classified as CODE
- WHEN the proxy builds the forwarded payload
- THEN the model receives the CODE intent default `temperature: 0.1`

### REQ-3: Forward the full OpenAI field set (R11)

The proxy SHALL forward every OpenAI chat-completion field the client sent, not only a hardcoded subset. The forward set MUST include at minimum: `tool_choice`, `parallel_tool_calls`, `frequency_penalty`, `presence_penalty`, `logit_bias`, `seed`, `user`, `response_format`, `top_logprobs`, `n`, `logprobs`. Client-sent values win on every field, with the sole exception of REQ-6 auto-routed authority, which replaces — not gaps — the listed R11 fields with profile values.

(Previously: client-sent values won on every R11 field unconditionally.)

#### Scenario-1: Client sends response_format and seed on a direct call

- GIVEN a client sends `{"model": "deepseek-r1", "response_format": {"type": "json_object"}, "seed": 42}`
- WHEN the proxy forwards the payload
- THEN both fields are present in the forwarded payload with the client's values

#### Scenario-2: Client omits optional OpenAI fields

- GIVEN a client sends only `messages` and `stream` on a direct call
- WHEN the proxy forwards the payload
- THEN no optional OpenAI field is injected by the proxy

## Notes

- R17 swaps the *source* of intent defaults (committed `config/model_profiles.yaml`, derived by the R18 filesystem scanner from GGUF metadata + per-file HF sampling + operator overrides) for the auto-routed path, not their values. Per-intent tuning in `routes.py` stays unchanged for direct calls. See `model-profile-sync` spec.
- No opt-out header and no per-client override: authority is unconditional in triggered mode by design.