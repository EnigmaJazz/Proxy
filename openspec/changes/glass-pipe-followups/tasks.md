# Tasks: Glass-Pipe Followups — Parameter Authority & HF Profile Sync

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~480 (PR1: ~250, PR2: ~230) |
| 400-line budget risk | Medium |
| Chained PRs recommended | Yes |
| Suggested split | PR1 (R18 toolchain+runtime) → PR2 (R17 authority+tests) |
| Delivery strategy | ask-on-risk |
| Chain strategy | pending |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: pending
400-line budget risk: Medium

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 1 | R18 toolchain + runtime loader | PR1 | ~250 lines; CI gate + offline-start verified |
| 2 | R17 authority + test coverage | PR2 | Base: PR1 branch; ~230 lines; depends on PR1 loader |

## Phase 1: R18 Build-Time Toolchain

- [x] 1.1 Create `config/local_models.yaml` — model_key→hf_model_id map for all 10 routeable models
- [x] 1.2 Create `tools/sync_model_profiles.py` — CLI: `sync`, `--check` (exit 0/1/2), `--watch`. Fns: `fetch_model_card`, `parse_sampling_params`, `derive_max_tokens` (ctx×0.9), `build_profile_entry`. Unparseable: WARNING+skip
- [x] 1.3 Run `sync` to generate `config/model_profiles.yaml` — per-model rows + two `model:"*"` fallback rows (code/chat) + `overrides:`. Commit generated file
- [x] 1.4 Add CI gate: `python tools/sync_model_profiles.py --check` as hard-block step

## Phase 2: R18 Runtime Loader

- [x] 2.1 Create `profile_loader.py` — `ProfileEntry` dataclass; `ModelProfileTable.resolve(intent, model_key)`: exact→model-wildcard→intent-fallback→None. `bucket()`: {CODE,ARCHITECT,TOOL,PROFESSIONAL}→"code", {CHAT,CREATIVE,SCHOLAR}→"chat". `load_model_profiles()` returns None on missing/malformed (logged)
- [x] 2.2 Wire `proxy.py` — add `model_profiles` attr to `AppState`; in lifespan (after ShadowAuditor): `state.model_profiles = load_model_profiles(PROJECT_ROOT/"config"/"model_profiles.yaml")`

## Phase 3: R17 Parameter Authority

- [ ] 3.1 Add `R11_AUTHORITY_FIELDS` tuple to `constants.py` (near `OPENAI_FORWARD_FIELDS`): temperature, top_p, max_tokens, thinking_budget_tokens, seed, top_logprobs, response_format, n
- [ ] 3.2 IP-1: `routes.py` — add `auto_authority = (requested_model=="auto") or (is_dream and caller_type=="AGENTIC")` after line ~187
- [ ] 3.3 IP-2/3: At `routes.py:459`, branch on `auto_authority`. Triggered+entry: `parameters={**entry.values}`. Not triggered: existing setdefault block. AFTER forward loop (~554): re-apply `for f in R11_AUTHORITY_FIELDS: payload[f]=entry.values[f]`
- [ ] 3.4 IP-4: When `auto_authority and entry`: append `_emit_proxy_event("params_replaced", {model, replaced, values})` to `proxy_preamble`
- [ ] 3.5 IP-4b: Dream path (`routes.py:265-317`) — resolve profile for architect/ARCHITECT. Replace hardcoded 0.2/4096/4096 with profile values. Fallback to hardcoded when no profile. Append params_replaced to preamble

## Phase 4: Test Coverage

- [x] 4.1 `TestProfileLoader` — resolve precedence (exact→wildcard→fallback→None); None+log on missing/malformed file
- [x] 4.2 `TestProfileFallback` — missing model_key → code-intent fallback for CODE, chat for CHAT; substitution logged
- [ ] 4.3 `TestAutoRoutedAuthority` — POST model:"auto" temp:0.9 seed:42 → forwarded == profile NOT client; params_replaced precedes first chunk
- [ ] 4.4 `TestDreamPathAuthority` — dream active → architect payload uses profile NOT hardcoded; params_replaced emitted
- [ ] 4.5 `TestParamsReplacedEvent` — parse JSON: kind=="params_replaced", ts int, data.replaced list, data.values; direct call → NO event
- [x] 4.6 `TestSyncCheckDrift` — stub HF fetcher; exit 0 in-sync, 1 drift, 2 malformed config; unparseable card skipped+logged

## Phase 5: 4R Pre-Merge Review (Process)

- [ ] 5.1 Register follow-up: run review-readability, review-reliability, review-resilience, review-risk against full diff before merge
