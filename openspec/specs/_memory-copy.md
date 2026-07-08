# Memory Copy — sdd/glass-pipe-hardening/spec

> Condensed copy of the spec phase output for cross-session recovery.
> Full canonical specs live alongside this file at `specs/<capability>/spec.md`.
> Note: Engram `mem_save` MCP tool is NOT exposed in this environment;
> `ctx_memory` rejected content payloads repeatedly, so this file is the durable fallback.

## Change

`glass-pipe-hardening` — enforced Glass-Pipe invariant on the non-text planes of the proxy (sampling parameters, `tools` array, SSE stream, auditor/DB tool-call coverage) plus a pytest regression harness.

## Capabilities (5 NEW — openspec/specs/ was empty)

| # | Capability | R coverage | Reqs | Scenarios |
|---|-------------|------------|------|-----------|
| 1 | glass-pipe-passthrough | R1, R7, R11, R3, R13 | 5 | 10 |
| 2 | glass-pipe-stream-integrity | R5, R14, R4, R6 | 6 | 10 |
| 3 | glass-pipe-auditor-coverage | R12, R15, R16 | 3 | 6 |
| 4 | glass-pipe-exceptions | R2, R8, R9 | 3 | 3 |
| 5 | test-infrastructure | R10 | 1 | 3 |
| **Total** | | **R1–R16 (all 16)** | **18** | **32** |

## Open questions resolved during spec

1. **`kinver.proxy.status` payload schema** — unified envelope `{"kind":"<kind>","ts":<int epoch-seconds>,"data":{...}}` shared by ALL three `kinver.proxy.*` event types (`status`, `tool_stripped`, `audit_halt`). `kind` enumerates: triage|loading|cache_restore|pause|resume|cloud (status event), tool_stripped, audit_halt.
2. **R16 storage shape** — JSON string in the existing content column; no schema migration, no new column. Easy to query with `json_extract`.
3. **R3 mitigation header** — deferred. No `X-Kinver-Strip-Tools` this pass. Strip removed outright → BREAKING for any IDE that depended on it; document in CHANGELOG. Loop-detection strip (R4) still applies per-turn.
4. **`strict_tdd`** — stays `false` until R10 lands; re-evaluate as a follow-up trigger after the harness exists.

## SSE event type → payload `kind` map

- `event: kinver.proxy.status`        → kinds: triage, loading, cache_restore, pause, resume, cloud
- `event: kinver.proxy.tool_stripped` → kind: tool_stripped; data: {reason, domain}
- `event: kinver.proxy.audit_halt`    → kind: audit_halt;   data: {reason}

## Key locked decisions carried verbatim into specs

- R3: passthrough for ALL IDE callers (aider/cline/vscode/opencode/sk-ide-pass)
- R4: `event: kinver.proxy.tool_stripped` BEFORE stripping, one-turn only, re-emit next turn
- R5/R14: `event: kinver.proxy.status` for triage/loading/cache/pause/resume/cloud
- R6: error chunk `finish_reason="stop"` + parallel `event: kinver.proxy.audit_halt`; no synthetic `delta.content` during tool_calls; `audit_override` finish reason removed
- R1/R7: client wins for temperature, top_p, max_tokens, thinking_budget_tokens
- R11: forward the full OpenAI field set
- R12/R15/R16: `delta.tool_calls` visible to auditor and DB

## Coverage

- Happy paths: covered (every requirement has ≥1 happy scenario)
- Edge cases: covered for CRITICAL fixes (R1, R3, R4, R5/R14, R6, R10 — happy + edge)
- Error states: covered (FATAL scenarios in stream-integrity; regression scenario in test-infra)
- All 16 R's covered; none dropped; none invented.

## Next phase

`sdd-design` — translate these specs into a technical design (sequence diagrams for the SSE event emission and the tool_calls extraction/accumulation flows; AD records for the payload schema, R16 JSON-string choice, and R3 breaking change).

## Files

- `openspec/changes/glass-pipe-hardening/specs/README.md` — index, R-coverage, OQ resolutions
- `openspec/changes/glass-pipe-hardening/specs/glass-pipe-passthrough/spec.md`
- `openspec/changes/glass-pipe-hardening/specs/glass-pipe-stream-integrity/spec.md`
- `openspec/changes/glass-pipe-hardening/specs/glass-pipe-auditor-coverage/spec.md`
- `openspec/changes/glass-pipe-hardening/specs/glass-pipe-exceptions/spec.md`
- `openspec/changes/glass-pipe-hardening/specs/test-infrastructure/spec.md`
- `openspec/changes/glass-pipe-hardening/specs/_memory-copy.md` — this file