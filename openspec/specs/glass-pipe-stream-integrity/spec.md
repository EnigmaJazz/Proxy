# Glass-Pipe Stream Integrity Specification

## Purpose

Proxy-injected stream signals (triage, loading, cache restore, pause, resume, cloud, loop-detection tool strip, Guillotine halt) are emitted as non-content SSE events on dedicated `event: kinver.proxy.*` event types so they never appear as `delta.content` and never corrupt `delta.tool_calls`. Anchors R5, R14, R4, R6.

## Requirements

### REQ-1: Proxy status emitted as kinver.proxy.status (R5, R14)

The proxy SHALL emit the triage banner, model-loading status, project-cache-restored status, and the pause/resume/cloud embedded-command messages as SSE events of type `event: kinver.proxy.status`. These messages MUST NOT be emitted as `delta.content` in `chat.completion.chunk` payloads. Standard OpenAI clients ignore unknown SSE event types; Kinver frontends subscribe to render.

#### Scenario-1: Triage banner on stream open

- GIVEN any request routed through the proxy
- WHEN the SSE stream opens
- THEN a line `event: kinver.proxy.status` with a structured JSON payload is emitted
- AND no chunk's `delta.content` carries the triage banner text

#### Scenario-2: Pause/resume embedded commands

- GIVEN a client issues a pause or resume embedded command
- WHEN the embedded-command handler streams its response
- THEN the status message is emitted as `event: kinver.proxy.status`
- AND no `delta.content` carries the pause/resume text

### REQ-2: kinver.proxy.status payload schema (R5, R14)

Every `event: kinver.proxy.status` line SHALL carry a JSON data payload conforming to the unified envelope: `{"kind": "<status-kind>", "ts": <epoch_seconds_int>, "data": {...}}`. The `kind` field MUST be one of: `triage`, `loading`, `cache_restore`, `pause`, `resume`, `cloud`. The `ts` field MUST be a Unix epoch integer in seconds. The `data` object MAY carry kind-specific fields.

#### Scenario-1: Triage payload shape

- GIVEN a triage banner is emitted on stream open
- WHEN the client parses the `event: kinver.proxy.status` line
- THEN the JSON payload has `kind` equal to `"triage"`, an integer `ts`, and a `data` object

#### Scenario-2: Unknown event type ignored by standard client

- GIVEN a standard OpenAI SSE client receives `event: kinver.proxy.status`
- WHEN the client parses the stream
- THEN the client ignores the unknown event type and does not surface it as assistant content

### REQ-3: Loop-detection tool strip emits event before stripping (R4)

When `detect_tool_loops` triggers a tools strip, the proxy SHALL emit `event: kinver.proxy.tool_stripped` BEFORE stripping the `tools` array for the current turn. The strip SHALL apply to the current request only, not conversation-wide. If loop detection still triggers on the next turn, the event SHALL be re-emitted.

#### Scenario-1: Loop detected, event emitted before strip

- GIVEN `detect_tool_loops` returns true for an agentic turn
- WHEN the proxy strips tools
- THEN the SSE stream contains `event: kinver.proxy.tool_stripped` before the model's first chunk
- AND the strip applies only to the current turn

#### Scenario-2: Next turn re-evaluates fresh

- GIVEN a turn had tools stripped due to loop detection
- WHEN the next turn is processed
- THEN loop detection is re-evaluated independently
- AND tools are forwarded unless loop detection triggers again

### REQ-4: kinver.proxy.tool_stripped payload schema (R4)

Every `event: kinver.proxy.tool_stripped` line SHALL carry a JSON payload using the unified envelope: `{"kind": "tool_stripped", "ts": <epoch_seconds_int>, "data": {"reason": "<str>", "domain": "<str>"}}`.

#### Scenario-1: tool_stripped payload shape

- GIVEN loop detection triggers a tool strip
- WHEN the `event: kinver.proxy.tool_stripped` line is emitted
- THEN the JSON payload has `kind: "tool_stripped"`, an integer `ts`, and `data.reason` plus `data.domain` string fields

### REQ-5: Guillotine emits no synthetic content during tool_calls (R6)

The Guillotine path MUST NOT emit synthetic `delta.content` (e.g. `[PROXY AUDIT OVERRIDE…]`) when the in-flight stream is emitting `tool_calls`. The non-standard `finish_reason: "audit_override"` SHALL be removed. The terminating chunk SHALL be a standards-compliant error chunk with `finish_reason: "stop"` and a short human-readable reason. The proxy SHALL emit `event: kinver.proxy.audit_halt` in parallel with the terminating chunk carrying the halt reason.

#### Scenario-1: FATAL during tool_calls

- GIVEN the auditor raises FATAL while the in-flight stream's last delta carried `tool_calls`
- WHEN the Guillotine path runs
- THEN the client receives no `delta.content` chunk with audit-override text
- AND the terminating chunk has `finish_reason: "stop"`
- AND an `event: kinver.proxy.audit_halt` line is emitted

#### Scenario-2: FATAL during plain content (non-tool-call stream)

- GIVEN the auditor raises FATAL while the in-flight stream carries only text content
- WHEN the Guillotine path runs
- THEN the terminating chunk has `finish_reason: "stop"` and `event: kinver.proxy.audit_halt` is emitted
- AND no `audit_override` finish reason appears

### REQ-6: kinver.proxy.audit_halt payload schema (R6)

Every `event: kinver.proxy.audit_halt` line SHALL carry a JSON payload using the unified envelope: `{"kind": "audit_halt", "ts": <epoch_seconds_int>, "data": {"reason": "<str>"}}`.

#### Scenario-1: audit_halt payload shape

- GIVEN the Guillotine terminates a stream
- WHEN the `event: kinver.proxy.audit_halt` line is emitted
- THEN the JSON payload has `kind: "audit_halt"`, an integer `ts`, and a `data.reason` string

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

- Open question resolved: unified envelope `{"kind","ts","data"}` for all three proxy SSE event types: `kinver.proxy.status`, `kinver.proxy.tool_stripped`, `kinver.proxy.audit_halt`. `ts` is Unix epoch seconds (int).
- **Placement decision:** `params_replaced` is a TOP-LEVEL `event: kinver.proxy.params_replaced` with its own `kind`, NOT a `data.subkind` under `event: kinver.proxy.status`. Rationale: REQ-2's `kind` enum is reserved for UI status banners. `params_replaced` is a data-plane substitution signal, not a banner. The existing precedent for non-banner proxy signals (`kinver.proxy.tool_stripped`, `kinver.proxy.audit_halt`) is a dedicated top-level event type with its own `kind`. Following that precedent keeps the REQ-2 enum focused on banners and gives `params_replaced` its own envelope (REQ-8). The openai-python SDK ignores unknown SSE event types (verified during glass-pipe-hardening), so a new top-level type is non-breaking.