# Proposal: Professional as Default

## Intent

Make Professional the default for auto-routed chat and tool work, avoiding 30–120s swaps. Specialists remain available when explicitly selected or classified as necessary.

## Problem

CHAT and TOOL select other heavy models despite Professional being stronger; queue cleanup can then recreate its cold-start cost.

## Approach

Choose **profile-first pivot (variant of D, with frontdesk bypass deferred to follow-up)**: add exact profiles, make Professional resident, then flip CHAT/TOOL in `ROUTE_MAP`. The original Approach D also included a frontdesk bypass (heuristic short-circuit); that piece is explicitly **deferred to a follow-up change** (tracked below) because the live test (see Live Test Result) shows the chat-tool flip works as-is and adding the bypass in the same change would inflate scope past the 400-line review budget.

## Scope

### In Scope
- Route auto CHAT/TOOL/CODE to Professional; retain specialists.
- Add exact `professional/chat` and `professional/code` profiles; no `default` bucket.
- Keep `chatter`/`worker` explicitly routable; pin Professional across an empty queue.

### Non-goals
- Frontdesk bypass (deferred to follow-up: `professional-default-frontdesk-bypass`).
- Retiring `chatter`/`worker` (kept as opt-in for direct clients per R19).
- Multi-heavy redesign (one-heavy architecture preserved; Professional is the resident).
- R1/R7 direct-call authority changes.
- Service-level flag changes (`--jinja` / `--tool-call-parser`) — the live test shows Professional emits structured `tool_calls` natively with the current service config.

## Capabilities

### New Capabilities
- `professional-default-routing`: Defaults, resident lifecycle, and explicit-specialist compatibility.

### Modified Capabilities
- None. `model-profile-sync` supports exact rows; direct-call Glass-Pipe rules remain unchanged.

## Affected Areas

- `routing.py` — `ROUTE_MAP` rows for `CHAT`, `TOOL`, and the existing `CODE`.
- `routes.py` — no functional change; the R19 override at lines 484-499 already preserves opt-in for clients that name a non-default model.
- `config/model_profiles.yaml` — add exact rows for `(professional, chat)` and `(professional, code)`.
- `proxy.py` — gate `unload_all_heavy` (lines 451-453) so it skips when `active_heavy_model == "professional"`; the hotswap wrapper for specialists remains unchanged.
- `openspec/specs/glass-pipe-passthrough/spec.md` — no change (R1/R7 client-wins still applies; `chatter`/`worker` remain valid `model` values).
- `tests/` — add tests pinning the new auto-CHAT/TOOL/CODE → Professional routing and the queue unload gate.

## Dependencies

- `R19` (commit `b4deac5`) — required; this change builds on the client-named-model override and the R1/R7 client-wins in the parameter build.
- `model-profile-sync` (R18) — required; the new `(professional, chat)` and `(professional, code)` rows are authored via the existing sync tool and stored in `config/model_profiles.yaml`.
- `glass-pipe-passthrough` capability — unchanged; R1/R7 contract is preserved.

## Follow-up (not part of this change)

- `professional-default-frontdesk-bypass` — heuristic short-circuit for chat/tool requests to skip the 2B classifier. Estimated 80-150 lines, fits in a single PR.

## Open Questions Answered

1. Keep `chatter`/`worker` in `ALL_MODEL_KEYS`; removal silently breaks direct clients.
2. ~~Professional is unproven: it MUST pass a live structured-delta test with `--jinja` and verified parser before TOOL flips; otherwise TOOL stays Worker.~~ **Resolved 2026-07-19** (see Live Test Result): Professional emits structured `tool_calls` deltas natively with no service-level config change. TOOL flip is cleared.
3. Add exact `professional/chat` and `professional/code`; no `default` bucket.
4. Skip queue-empty unload for Professional; unload only on a specialist transition.
5. Keep `auto_authority` trigger to `model: "auto"` + AGENTIC dream; explicit `model: "professional"` remains R1/R7 direct-call (client-wins on sampling parameters).
6. Keep frontdesk for specialist/project/priority classification; defer its latency optimization to `professional-default-frontdesk-bypass`.
7. Keep one-heavy architecture: Professional is resident; hotswap remains for specialists and receives transition tests.

## Proposal Question Round

- ~~Confirm the Professional parser value and provide the live structured-tool-delta result; this gates the TOOL route flip.~~ **Resolved 2026-07-19** (see Live Test Result).

## Live Test Result (2026-07-19)

```
Request:  POST /v1/chat/completions  model=professional, tools=[web_search]
          message="What is the current weather in London?"
Response: 151 total chunks
          - 8 chunks with delta.tool_calls (chunks 142–149)
          - First tool_call chunk:
            {"index":0, "id":"xUrK0xdq3IIzkOZINeFqK3OyUAywnTVw",
             "type":"function",
             "function":{"name":"web_search", "arguments":"{"}}
          - Final assembled call: web_search({"query": "current weather in London"})
          - finish_reason: tool_calls
```

Professional emits valid OpenAI-compatible `tool_calls` JSON via its native chat template. No service-level config change required.

## Risks

| Risk | Mitigation |
|---|---|
| ~~Malformed Professional tool calls~~ | ~~Gate flip; retain Worker.~~ **Closed by live test (2026-07-19).** |
| Queue unload cold starts | Pin and test Professional. |
| Weak profile | Exact rows and payload tests. |
| Direct-client break | Keep keys and R1/R7. |

## Alternatives Considered

- **A:** breaks compatibility and skips the tool gate.
- **B:** leaves the routing direction ambiguous.
- **C:** risks classification before its replacement exists.

## Rollback Plan

Revert route/profile/queue commits. Worker remains routable. Restore the prior Professional unit and restart if validation fails; no data migration.

## Reference Docs

- `exploration.md`; `openspec/changes/tool-call-template/proposal.md`
- R19: `b4deac5`
- `openspec/specs/glass-pipe-passthrough/spec.md`

## Success Criteria

- [ ] Auto CHAT/CODE/TOOL uses Professional without a redundant swap.
- [ ] Live test confirms Professional emits structured `tool_calls` deltas. **(DONE 2026-07-19)**
- [ ] Explicit Worker/Chatter and direct parameters remain unchanged.
