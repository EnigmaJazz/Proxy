# Specs Index

## glass-pipe-hardening (archived)

> Delta spec index for change `glass-pipe-hardening`. All capabilities are NEW
> (initially `openspec/specs/` was empty — full specs, not deltas). One spec per capability,
> one or more scenarios per requirement; CRITICAL fixes carry happy + edge scenarios.

### Capability → R coverage

| # | Capability | Spec path | R coverage | Reqs | Scenarios |
|---|-------------|-----------|------------|------|-----------|
| 1 | glass-pipe-passthrough | `glass-pipe-passthrough/spec.md` | R1, R7, R11, R3, R13 | 5 | 10 |
| 2 | glass-pipe-stream-integrity | `glass-pipe-stream-integrity/spec.md` | R5, R14, R4, R6 | 6 | 10 |
| 3 | glass-pipe-auditor-coverage | `glass-pipe-auditor-coverage/spec.md` | R12, R15, R16 | 3 | 6 |
| 4 | glass-pipe-exceptions | `glass-pipe-exceptions/spec.md` | R2, R8, R9 | 3 | 3 |
| 5 | test-infrastructure | `test-infrastructure/spec.md` | R10 | 1 | 3 |
| | **Subtotal** | | **R1–R16 (all 16)** | **18** | **32** |

## professional-default-routing (NEW)

> Full spec for change `professional-as-default`. Professional becomes the resident
> default for auto-routed CHAT, TOOL, and CODE while preserving explicit model
> choice, profile authority, and specialist hotswaps.

### Capability → REQ coverage

| # | Capability | Spec path | REQ coverage | Reqs | Scenarios |
|---|-------------|-----------|--------------|------|-----------|
| 6 | professional-default-routing | `professional-default-routing/spec.md` | REQ-1 through REQ-8 | 8 | 8 |
| | **Total** | | **R1–R16 + REQ-1–REQ-8** | **26** | **40** |

## R → Capability cross-reference

| R | Capability | Locked decision |
|---|------------|-----------------|
| R1 | glass-pipe-passthrough | client wins `temperature`/`top_p`/`max_tokens` |
| R2 | glass-pipe-exceptions | single-value-per-param + fallback doc |
| R3 | glass-pipe-passthrough | passthrough for all Lane B/IDE; strip removed (BREAKING) |
| R4 | glass-pipe-stream-integrity | `event: kinver.proxy.tool_stripped` before strip, one-turn |
| R5 | glass-pipe-stream-integrity | triage/loading/cache → `event: kinver.proxy.status` |
| R6 | glass-pipe-stream-integrity | error chunk `finish_reason="stop"` + parallel `audit_halt` event |
| R7 | glass-pipe-passthrough | client wins `thinking_budget_tokens` |
| R8 | glass-pipe-exceptions | stop-seq filter labeled Glass-Pipe exception |
| R9 | glass-pipe-exceptions | `translate_to_deepseek_r1` labeled exception |
| R10 | test-infrastructure | bootstrap pytest + pytest-asyncio harness |
| R11 | glass-pipe-passthrough | forward full OpenAI field set |
| R12 | glass-pipe-auditor-coverage | `feed_chunk` takes dict, sees `tool_calls` |
| R13 | glass-pipe-passthrough | `X-Kinver-Allow-Mid-Tool-Switch` opt-out header |
| R14 | glass-pipe-stream-integrity | pause/resume/cloud → `event: kinver.proxy.status` |
| R15 | glass-pipe-auditor-coverage | stream loop extracts `delta.tool_calls` |
| R16 | glass-pipe-auditor-coverage | `complete_job` accumulates `tool_calls` as JSON string |

## Open questions resolved during spec

1. **`kinver.proxy.status` payload schema** — unified envelope `{"kind","ts","data"}` shared by all three `kinver.proxy.*` event types. `ts` is Unix epoch seconds (int). `kind` enumerates the signal; `data` carries kind-specific fields.
2. **R16 storage shape** — JSON string in the existing content column; no schema migration, no new column.
3. **R3 mitigation header** — deferred. No `X-Kinver-Strip-Tools` opt-in this pass; clients that depended on the strip are broken; document in CHANGELOG. Loop-detection strip (R4) still applies per-turn.
4. **R10 strict_tdd re-evaluation** — `strict_tdd` stays `false`; re-evaluate as a follow-up trigger after the harness exists.

## Open questions remaining

None. All four open questions from the proposal are resolved in-spec and this index.

## SSE event type → payload `kind` map

| SSE event type | `kind` values | Trigger |
|----------------|---------------|---------|
| `event: kinver.proxy.status` | `triage`, `loading`, `cache_restore`, `pause`, `resume`, `cloud` | proxy status banners / embedded commands |
| `event: kinver.proxy.tool_stripped` | `tool_stripped` | loop-detection strip (R4) |
| `event: kinver.proxy.audit_halt` | `audit_halt` | Guillotine FATAL halt (R6) |

## Coverage summary

- Happy paths: covered (every requirement has ≥1 happy scenario)
- Edge cases: covered for CRITICAL fixes (R1, R3, R4, R5/R14, R6, R10 — happy + edge)
- Error states: covered (FATAL scenarios in stream-integrity; regression scenario in test-infra)
- All 16 R's covered, no R dropped, no R invented.