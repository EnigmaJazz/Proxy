# Archive Report: Glass-Pipe Followups

**Change**: glass-pipe-followups
**Status**: COMPLETE
**Archived**: 2026-07-08
**Tracker**: `feature/glass-pipe-followups` @ `b4d32a4`

## Summary

Glass-pipe-followups addressed two gaps in the proxy's behavior:

1. **R17 — Parameter authority in auto-routed mode**: When the proxy picks the model (via auto-routing or dream/soul fast-path), it owns the sampling parameters. Client values are replaced with profile-sourced values from `config/model_profiles.yaml`. A new `event: kinver.proxy.params_replaced` SSE event announces the substitution so UIs can surface it. This is the 4th proxy event type, peer to `kinver.proxy.status`, `kinver.proxy.tool_stripped`, and `kinver.proxy.audit_halt`.

2. **R18 — Filesystem-grounded model profiles**: The proxy's sampling recommendations come from a build-time scanner that reads actual GGUF files on disk (the source of truth) and fetches Hugging Face sampling params per-file via `sampling_source`. An LLM extractor (using the proxy's own model) handles pages where regex parsing fails. The scanner CLI (`tools/sync_model_profiles.py`) supports `sync`, `--check` (hard CI gate), and `--watch`.

3. **Carry-over cleanups**: 3 of the original 4 carry-over cleanups were already shipped in glass-pipe-hardening. The 4R review fan-out was completed as part of PR1's verification.

## PRs

| PR | Title | Status | Diff |
|----|-------|--------|------|
| #1 | feat(r18): filesystem-grounded profile scanner rewrite | Merged | 709 lines |
| #2 | feat(r17): proxy owns sampling parameters in auto-routed and dream paths | Merged | ~170 lines |

## Key design decisions

1. **R17 trigger**: `(requested_model == "auto") OR (is_dream AND caller_type == "AGENTIC")` — single `auto_authority` flag, set once after `requested_model` parse and dream detection.

2. **R17 authority scope**: Full R11 field set — `temperature`, `top_p`, `max_tokens`, `thinking_budget_tokens`, `seed`, `top_logprobs`, `response_format`, `n`. No opt-out header, no per-client override.

3. **R17 two-path trigger**: Both the auto-routed shared path (`routes.py:445-490`) and the dream/soul fast-path (`routes.py:254-298`) use the same `auto_authority` flag and profile lookup. The fast-path was initially missed in the proposal but caught by sdd-design.

4. **R11 ↔ payload build overlap**: Profile values are re-applied AFTER the payload build (`routes.py:520-533`) so profile wins regardless of any client-forwarded values.

5. **R18 source of truth**: GGUF files on disk. No speculative model_id list. `sampling_source` is an optional operator-declared URL/path to fetch sampling recommendations from. The HF fetch is per-file and only happens when the operator opts in.

6. **R18 LLM extractor**: When regex parsing fails on the `sampling_source`, the scanner calls the proxy's own `coder` model (or `--extractor` model) via `/v1/chat/completions` to extract `{temperature, top_p}` as structured JSON. Best-effort: any failure falls back to intent defaults.

7. **R18 `params_replaced` event placement**: Top-level `event: kinver.proxy.params_replaced`, NOT a subkind of `status`. Follows the precedent of `tool_stripped` and `audit_halt`.

## Critical findings

1. **Pre-pivot R18 was already shipped** (commit `85cfc3a`): the original filesystem scanner, `profile_loader.py`, `proxy.py` lifespan wiring, and CI gate were all committed before this change started. The new R18 scanner rewrites these in place.

2. **R17 ordering dependency**: `OPENAI_FORWARD_FIELDS`, `R11_AUTHORITY_FIELDS`, and the `auto_authority` flag were all absent from the pre-refactor code. sdd-tasks correctly identified Phase 3 (hardening) as a prerequisite for Phase 4 (authority). Without this sequencing, PR2 wouldn't compile.

3. **Pre-refactor proxy restructure**: The main refactor (commit `1196ee1`) split `routes.py` into `routes.py` + `routing.py`, moved the tool registry to `tools.py`, and restructured the entire codebase. All SDD specs and designs were re-spec'd against the new code layout.

4. **Subagent stalls**: Multiple subagents (sdd-spec, sdd-apply) stalled or returned empty results. The sdd-apply for PR2 returned an empty Result Contract but left partial changes. The orchestrator completed the work inline. This is a known pattern on this project.

## Follow-up items

1. **R17 integration tests**: The current test suite has 41 R18 tests. R17 integration tests (auto_authority flag, profile substitution, params_replaced emission end-to-end) are deferred to a follow-up. The test harness stubs out database/systemd/hardware/cooling and would need a substantial new fixture to exercise `chat_completions` end-to-end.

2. **Proxy speed**: The proxy's hotswap mechanism takes 30-120s to load a model from cold. The LLM extractor inherits this latency. A future improvement: proxy-level caching of hotswapped models, or a dedicated extractor model that's always warm.

3. **`__pycache__` cleanup**: The repository has committed `__pycache__/*.pyc` files. These should be removed from git and added to `.gitignore`. This is a follow-up cleanup task.

4. **Lost untracked files**: Several untracked files (AGENTS.md, openspec/config.yaml, package.json, package-lock.json) were accidentally deleted during a `git clean -fd` operation. These need to be recreated if they were important.

## Artifacts

| Artifact | Location |
|----------|----------|
| Proposal | `openspec/changes/archive/glass-pipe-followups-2026-07-08/proposal.md` |
| Design | `openspec/changes/archive/glass-pipe-followups-2026-07-08/design.md` |
| Tasks | `openspec/changes/archive/glass-pipe-followups-2026-07-08/tasks.md` |
| R17 delta (passthrough) | `openspec/changes/archive/glass-pipe-followups-2026-07-08/specs/glass-pipe-passthrough/spec.md` |
| R17 delta (stream integrity) | `openspec/changes/archive/glass-pipe-followups-2026-07-08/specs/glass-pipe-stream-integrity/spec.md` |
| R18 spec | `openspec/changes/archive/glass-pipe-followups-2026-07-08/specs/model-profile-sync/spec.md` |
| Spec store (updated) | `openspec/specs/glass-pipe-passthrough/spec.md` (REQ-6 added, REQ-1/REQ-3 updated) |
| Spec store (updated) | `openspec/specs/glass-pipe-stream-integrity/spec.md` (REQ-7/REQ-8 added) |
| Spec store (new) | `openspec/specs/model-profile-sync/spec.md` |

## Test results

- 41 R18 tests pass
- R17 live test passed: `event: kinver.proxy.params_replaced` emitted with correct profile values when `model: "auto"` was sent
- Drift check: clean
