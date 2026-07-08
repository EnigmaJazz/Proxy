# Delta for Glass-Pipe Stream Integrity

## ADDED Requirements

### REQ-7: params_replaced emitted on auto-routed substitution (R17 data plane)

When the proxy applies REQ-6 (glass-pipe-passthrough) auto-routed parameter authority, it SHALL emit `event: kinver.proxy.params_replaced` BEFORE the model's first content/tool chunk. The event SHALL carry the values actually forwarded so subscribing UIs can surface the substitution. The event MUST NOT be emitted for direct calls where REQ-1/REQ-3 client-wins applied. Standard OpenAI SSE clients MUST be able to ignore the unknown event type without protocol break.

#### Scenario-1: Auto-routed substitution emits event

- GIVEN an auto-routed call triggers REQ-6 parameter replacement
- WHEN the SSE stream opens
- THEN `event: kinver.proxy.params_replaced` is emitted before the first model chunk
- AND no `delta.content` carries the substitution text

#### Scenario-2: Direct call emits no params_replaced

- GIVEN a direct call where client-wins (REQ-1/REQ-3) applied
- WHEN the SSE stream is processed
- THEN no `event: kinver.proxy.params_replaced` line is emitted

#### Scenario-3: Standard client ignores the event

- GIVEN a standard OpenAI SSE client receives `event: kinver.proxy.params_replaced`
- WHEN the client parses the stream
- THEN the client ignores the unknown event type and does not surface it as assistant content

### REQ-8: kinver.proxy.params_replaced payload schema (R17)

Every `event: kinver.proxy.params_replaced` line SHALL carry a JSON payload using the unified envelope: `{"kind": "params_replaced", "ts": <epoch_seconds_int>, "data": {"model": "<str>", "replaced": [<field>, ...], "values": {...}}}`. The `replaced` array MUST list each R11 field the proxy substituted. The `values` object MUST carry the values forwarded to the model for those fields. The `ts` field MUST be a Unix epoch integer in seconds.

#### Scenario-1: params_replaced payload shape

- GIVEN an auto-routed call replaces `temperature` and `seed`
- WHEN the `event: kinver.proxy.params_replaced` line is emitted
- THEN the JSON payload has `kind: "params_replaced"`, an integer `ts`, `data.model` string, `data.replaced` including `"temperature"` and `"seed"`, and `data.values` carrying the forwarded values

## Notes

- **Placement decision (resolves proposal tension):** `params_replaced` is a TOP-LEVEL `event: kinver.proxy.params_replaced` with its own `kind`, NOT a `data.subkind` under `event: kinver.proxy.status`. Rationale: REQ-2's `kind` enum (`triage`, `loading`, `cache_restore`, `pause`, `resume`, `cloud`) is reserved for UI status banners. `params_replaced` is a data-plane substitution signal, not a banner. The existing precedent for non-banner proxy signals (`kinver.proxy.tool_stripped`, `kinver.proxy.audit_halt`) is a dedicated top-level event type with its own `kind`. Following that precedent keeps the REQ-2 enum focused on banners and gives `params_replaced` its own envelope (REQ-8). The openai-python SDK ignores unknown SSE event types (verified during glass-pipe-hardening), so a new top-level type is non-breaking.