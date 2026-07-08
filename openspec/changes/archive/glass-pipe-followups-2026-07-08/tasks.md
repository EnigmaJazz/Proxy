# Tasks: Glass-Pipe Followups — Parameter Authority & HF Profile Sync

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~230 (PR2 only; PR1 ~250 already merged) |
| 400-line budget risk | Low (PR2 alone is well under budget) |
| Chained PRs recommended | No (PR1 merged; PR2 is the remaining slice) |
| Suggested split | PR1 ✅ merged → PR2 (R17 hardening+authority+tests) |
| Delivery strategy | ask-on-risk |
| Chain strategy | stacked-to-main (PR1 merged to tracker; PR2 targets tracker) |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: stacked-to-main
400-line budget risk: Low

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 1 | R18 toolchain + runtime loader | PR1 ✅ | ~250 lines; merged (commit cbd90a5) |
| 2 | R17 hardening + authority + tests | PR2 | ~230 lines; depends on PR1 loader; includes hardening prerequisite (Phase 3) |

## Phase 1: R18 Build-Time Toolchain

- [x] 1.1 Create `config/local_models.yaml` — model_key→hf_model_id map for all 10 routeable models
- [x] 1.2 Create `tools/sync_model_profiles.py` — CLI: `sync`, `--check` (exit 0/1/2), `--watch`. Fns: `fetch_model_card`, `parse_sampling_params`, `derive_max_tokens` (ctx×0.9), `build_profile_entry`. Unparseable: WARNING+skip
- [x] 1.3 Run `sync` to generate `config/model_profiles.yaml` — per-model rows + two `model:"*"` fallback rows (code/chat) + `overrides:`. Commit generated file
- [x] 1.4 Add CI gate: `python tools/sync_model_profiles.py --check` as hard-block step

## Phase 2: R18 Runtime Loader

- [x] 2.1 Create `profile_loader.py` — `ProfileEntry` dataclass; `ModelProfileTable.resolve(intent, model_key)`: exact→model-wildcard→intent-fallback→None. `bucket()`: {CODE,ARCHITECT,TOOL,PROFESSIONAL}→"code", {CHAT,CREATIVE,SCHOLAR}→"chat". `load_model_profiles()` returns None on missing/malformed (logged)
- [x] 2.2 Wire `proxy.py` — add `model_profiles` attr to `AppState`; in lifespan (after ShadowAuditor): `state.model_profiles = load_model_profiles(PROJECT_ROOT/"config"/"model_profiles.yaml")`

## Phase 3: R17 Hardening (Prerequisite)

- [ ] 3.1 Add `OPENAI_FORWARD_FIELDS` tuple to `constants.py` near `STOP_SEQS`: tool_choice, parallel_tool_calls, frequency_penalty, presence_penalty, logit_bias, seed, user, response_format, top_logprobs, n, logprobs
- [ ] 3.2 Read R11 fields from request body in `routes.py` after line 162: extract seed, top_logprobs, response_format, n, etc. from body with `body.get()`
- [ ] 3.3 Add R11 forward loop to payload build in `routes.py` after line 527: `for f in OPENAI_FORWARD_FIELDS: if body.get(f) is not None: payload[f] = body[f]`
- [ ] 3.4 Add preamble event mechanism: add `proxy_preamble: list[str]` parameter to `_event_stream` (line 581) and `_event_stream_with_model_startup` (line 734); yield preamble events before triage event at line 641

## Phase 4: R17 Parameter Authority

- [ ] 4.1 Add `R11_AUTHORITY_FIELDS` tuple to `constants.py` (near `OPENAI_FORWARD_FIELDS`): temperature, top_p, max_tokens, thinking_budget_tokens, seed, top_logprobs, response_format, n
- [ ] 4.2 IP-1: `routes.py` — initialize `is_dream = False` before line 177 (AGENTIC block); add `auto_authority = (requested_model=="auto") or (is_dream and caller_type=="AGENTIC")` after line ~193 (after `is_dream` computed)
- [ ] 4.3 IP-2/3: At `routes.py:445` (start of parameters construction), branch on `auto_authority`. Triggered+entry: `parameters={**entry.values}`. Not triggered: existing intent-default block (through `routes.py:478`). AFTER payload build (~533): re-apply `for f in R11_AUTHORITY_FIELDS: payload[f]=entry.values[f]`
- [ ] 4.4 IP-4: When `auto_authority and entry`: build params_replaced event string via `_make_system_chunk`; append to `proxy_preamble` list passed to `_event_stream`
- [ ] 4.5 IP-4b: Dream path (`routes.py:254-298`) — resolve profile for architect/ARCHITECT. Replace hardcoded 0.2/4096/4096 with profile values. Fallback to hardcoded when no profile. Build params_replaced event and append to `proxy_preamble` before `return StreamingResponse(...)` at `routes.py:295`

## Phase 5: Test Coverage

- [x] 5.1 `TestProfileLoader` — resolve precedence (exact→wildcard→fallback→None); None+log on missing/malformed file
- [x] 5.2 `TestProfileFallback` — missing model_key → code-intent fallback for CODE, chat for CHAT; substitution logged
- [ ] 5.3 `TestAutoRoutedAuthority` — POST model:"auto" temp:0.9 seed:42 → forwarded == profile NOT client; params_replaced precedes first chunk
- [ ] 5.4 `TestDreamPathAuthority` — dream active → architect payload uses profile NOT hardcoded; params_replaced emitted
- [ ] 5.5 `TestParamsReplacedEvent` — parse JSON: kind=="params_replaced", ts int, data.replaced list, data.values; direct call → NO event
- [x] 5.6 `TestSyncCheckDrift` — stub HF fetcher; exit 0 in-sync, 1 drift, 2 malformed config; unparseable card skipped+logged

## Phase 6: 4R Pre-Merge Review (Process)

- [ ] 6.1 Register follow-up: run review-readability, review-reliability, review-resilience, review-risk against full diff before merge
