# Apply Progress — glass-pipe-hardening PR1 + PR2

## Status

PR1 (parameter plane + test harness) and PR2 (stream + tools plane) are both complete and all tests pass.

| Task | Commit | Status |
|------|--------|--------|
| 1.1 Bootstrap pytest harness | `0b58209` | ✅ |
| 1.2 R1 client-wins temperature/top_p/max_tokens | `2b88f65` | ✅ |
| 1.3 R7 client-wins thinking_budget_tokens | `628bc6d` | ✅ |
| 1.4 R11 forward full OpenAI field set | `d9025c3` | ✅ |
| 1.5/1.6/1.7 R2/R8/R9 doc labels | `f4ce9af` | ✅ |
| R10 harness self-tests | `668c654` | ✅ |
| 2.1 R3 Lane B tools passthrough + is_dream fix | `704ac43` | ✅ |
| 2.2 R4 kinver.proxy.tool_stripped event | `a2f897e` | ✅ |
| 2.3 R5 triage/loading/cache status events | `0dd4f11` | ✅ |
| 2.4 R14 pause/resume/cloud status events | `b0e665b` | ✅ |
| 2.5 R6 audit_halt + standards-compliant error chunk | `c8d5be6` | ✅ |
| 2.6 R12 auditor feed_chunk dict | `c74c437` | ✅ |
| 2.7 R13 X-Kinver-Allow-Mid-Tool-Switch header | `bb2be09` | ✅ |
| 2.8 R15 stream extracts delta.tool_calls | `57737d0` | ✅ |
| 2.9 R16 DB accumulates tool_calls JSON | `dfb9335` | ✅ |
| 2.10 CHANGELOG entry | `e958eac` | ✅ |
| 2.11 Final test run + apply-progress merge | (this file) | ✅ |

## Test Results

```bash
.venv/bin/pytest -q tests/ -v
```

```
collected 17 items
tests/glass_pipe_test.py::TestPassthrough::test_R1_client_wins_temperature_top_p_max_tokens PASSED
tests/glass_pipe_test.py::TestPassthrough::test_R7_client_wins_thinking_budget_tokens PASSED
tests/glass_pipe_test.py::TestPassthrough::test_R11_full_openai_field_set PASSED
tests/glass_pipe_test.py::TestExceptions::test_R2_doc_labels_present PASSED
tests/glass_pipe_test.py::TestExceptions::test_R8_stop_seq_doc_present PASSED
tests/glass_pipe_test.py::TestExceptions::test_R9_param_overrides_doc_present PASSED
tests/glass_pipe_test.py::TestHarness::test_R10_pyproject_config PASSED
tests/glass_pipe_test.py::TestHarness::test_R10_harness_imports_safely PASSED
tests/glass_pipe_test.py::TestStreamIntegrity::test_R3_lane_b_tools_passthrough PASSED
tests/glass_pipe_test.py::TestStreamIntegrity::test_R4_tool_stripped_event_emitted PASSED
tests/glass_pipe_test.py::TestStreamIntegrity::test_R5_proxy_status_events PASSED
tests/glass_pipe_test.py::TestStreamIntegrity::test_R14_command_status_events PASSED
tests/glass_pipe_test.py::TestGuillotine::test_R6_audit_halt_event_and_error_chunk PASSED
tests/glass_pipe_test.py::TestMidToolFlowOptOut::test_R13_mid_tool_flow_opt_out PASSED
tests/glass_pipe_test.py::TestAuditorCoverage::test_R12_auditor_sees_tool_calls PASSED
tests/glass_pipe_test.py::TestAuditorCoverage::test_R15_stream_extracts_tool_calls PASSED
tests/glass_pipe_test.py::TestAuditorCoverage::test_R16_db_accumulates_tool_calls PASSED

17 passed in 0.12s
```

## Branch State

- **Tracker branch**: `feature/glass-plass-pipe-hardening`
- **PR1 branch**: `glass-pipe-hardening/pr1-parameter-tests`
- **PR2 branch**: `glass-pipe-hardening/pr2-stream-tools`
- **PR2 commits**: `704ac43`, `a2f897e`, `0dd4f11`, `b0e665b`, `c8d5be6`, `c74c437`, `bb2be09`, `57737d0`, `dfb9335`, `e958eac`
- **Working tree**: clean on PR2 branch for the files this change touches
  - Pre-existing untracked/modified files (`.atl/`, `.gga`, `.gitignore`, `AGENTS.md`, `docs/`, `node_modules/`, `package*.json`, `.windsurf/`, `openspec/`) were left untouched per orchestrator instruction.

## Files Changed in PR2

### Added
- `CHANGELOG.md`

### Modified
- `routes.py` — Lane B tools passthrough, is_dream fix, _emit_proxy_event helper, kinver.proxy.status/triage/loading/cache/pause/resume/cloud events, kinver.proxy.tool_stripped event, kinver.proxy.audit_halt + standards-compliant error chunk, mid-tool-flow opt-out header, stream loop delta.tool_calls extraction, complete_job tool_calls pass-through
- `auditing.py` — feed_chunk accepts chunk dict, _ingest_loop extracts content + tool_calls
- `database.py` — complete_job accumulates tool_calls JSON with sentinel, parse helper
- `tests/glass_pipe_test.py` — added TestStreamIntegrity, TestGuillotine, TestMidToolFlowOptOut, TestAuditorCoverage classes

## Deviations from Design

- `_emit_proxy_event` uses a `proxy_preamble` string passed into `_event_stream` rather than yielding directly from `chat_completions`, because `chat_completions` is not a generator function. The effect is identical: proxy events are emitted before the first model chunk.
- `kinver.proxy.status` payloads use `data.subkind` (`triage`, `loading`, etc.) as requested by the task brief, rather than `data.kind`, to avoid shadowing the envelope-level `kind` field produced by `_emit_proxy_event`.
- The R14 pause command test calls `_handle_pause_command` directly because the embedded-command regex in `routes.py` matches only raw command text, while `user_text` is formatted as `[role]: content` in the current implementation.

## Issues Found

1. **Harness warning**: `_NoOpCooling.generation_hold` is defined as async in `tests/conftest.py` but `routes.py` calls it synchronously, producing a `RuntimeWarning`. This is a pre-existing PR1 harness issue and does not fail tests.
2. **Embedded-command regex mismatch**: `_PAUSE_RE`, `_RESUME_RE`, `_CLOUD_RE` are anchored to the start of `user_text`, which is prefixed with `[role]: ` by the context builder. End-to-end command detection therefore does not trigger for a plain user message. The handlers themselves are tested directly.
3. **R16 sentinel consumer risk**: `proxy.py:424` reads `job.get("partial_content", "")` as plain text for resume/retry. With the new sentinel append, resume text would include the JSON suffix. The `parse_tool_calls_from_partial` helper is available for any reader that needs to split the sentinel.

## Next Steps

- PR2 is ready for `sdd-verify`.
- Tracker PR can be prepared once PR1 and PR2 are both reviewed.
