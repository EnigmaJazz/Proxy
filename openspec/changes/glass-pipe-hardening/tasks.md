# Tasks: Glass-Pipe Hardening — Parameters & Tool-Call Passthrough

## Review Workload Forecast

```json
{
  "chained_prs_recommended": true,
  "estimated_changed_lines_pr1": 210,
  "estimated_changed_lines_pr2": 180,
  "estimated_changed_lines_total": 390,
  "line_budget_400_risk": "low",
  "decision_needed_before_apply": true,
  "decision_reason": "Delivery strategy is ask-on-risk; both PRs under 400-line budget but chained PRs recommended"
}
```

| Field | Value |
|-------|-------|
| Estimated changed lines | ~390 total (PR1: ~210, PR2: ~180) |
| 400-line budget risk | Low |
| Chained PRs recommended | Yes |
| Suggested split | PR1 → PR2 (feature branch chain) |
| Delivery strategy | ask-on-risk |
| Chain strategy | feature-branch-chain |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: feature-branch-chain
400-line budget risk: Low

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 1 | Parameter plane + test harness | PR1 | Base: `feature/glass-pipe-hardening`; tests/docs included; R10 bootstraps pytest |
| 2 | Stream + tools plane | PR2 | Base: PR1 branch; depends on PR1's test harness for verification |

---

## PR1: Parameter Plane + Test Harness (~210 lines)

**R-coverage**: R10, R1, R7, R11, R2, R8, R9  
**File surface**: `pyproject.toml` (new), `tests/conftest.py` (new), `tests/glass_pipe_test.py` (new), `routes.py` (modified), `constants.py` (modified), `llm.py` (modified)  
**Dependencies**: None (first PR)  
**Test surface**: `TestParameterPassthrough`, `TestPayloadFields`, `TestGlassPipeDocLabels`, `TestHarnessSelf`  
**Rollback**: `git revert <PR1-merge>` removes test harness (additive new files) and parameter passthrough changes cleanly

### Task 1.1: Bootstrap pytest + pytest-asyncio infrastructure

**ID**: 1.1  
**Title**: Bootstrap pytest + pytest-asyncio test infrastructure **[x]**  
**R-coverage**: R10  
**Files**: 
- `pyproject.toml` (new, ~8 lines)
- `.venv/bin/pip install pytest-asyncio` (bash command, not a file change)

**Spec scenarios**: 
- `test-infrastructure REQ-1 / Scenario-1` (harness runs green)
- `test-infrastructure REQ-1 / Scenario-3` (import safety under collection)

**Test class / scenarios**: 
- `TestHarnessSelf` — asserts pytest-asyncio installed, pyproject.toml exists, asyncio_mode="auto"

**Dependencies**: None  
**Commit message**: 
```
test(proxy): bootstrap pytest + pytest-asyncio infrastructure

- Add pyproject.toml with pytest config (asyncio_mode=auto, testpaths=tests)
- Install pytest-asyncio in .venv (documented, not tracked)
- Enables R10 test harness for glass-pipe-hardening verification
```

**Rollback**: `git revert <commit>` removes pyproject.toml; pytest-asyncio remains installed  
**Risk level**: CRITICAL (blocks all test verification)  
**Line estimate**: +8 lines (pyproject.toml only; pip install is not tracked)

---

### Task 1.2: Create test harness with mocked dependencies

**ID**: 1.2  
**Title**: Create test harness with mocked dependencies **[x]**  
**R-coverage**: R10  
**Files**: 
- `tests/conftest.py` (new, ~60 lines)

**Spec scenarios**: 
- `test-infrastructure REQ-1 / Scenario-3` (import safety under collection)

**Test class / scenarios**: 
- `TestHarnessSelf` — asserts FlashRank stubbed, lifespan not run, test_app fixture available

**Dependencies**: 1.1 (pytest-asyncio installed)  
**Commit message**: 
```
test(proxy): add conftest.py with mocked dependencies and lifespan-free app

- Stub flashrank before import to prevent model download
- Mock Database, SystemdController, CoolingStateMachine, ShadowAuditor
- Use ASGITransport with lifespan off to avoid systemd/hardware init
- Expose test_app fixture for glass_pipe_test.py
```

**Rollback**: `git revert <commit>` removes tests/conftest.py  
**Risk level**: CRITICAL (blocks test collection if FlashRank/lifespan not stubbed)  
**Line estimate**: +60 lines

---

### Task 1.3: Implement parameter passthrough (client wins)

**ID**: 1.3  
**Title**: Implement parameter passthrough — client wins for temperature/top_p/max_tokens **[x]**  
**R-coverage**: R1, R7  
**Files**: 
- `routes.py` (modified, lines 446-478, ~15 lines added/modified)

**Spec scenarios**: 
- `glass-pipe-passthrough REQ-1 / Scenario-1, Scenario-2` (temperature)
- `glass-pipe-passthrough REQ-2 / Scenario-1, Scenario-2` (thinking_budget_tokens)

**Test class / scenarios**: 
- `TestParameterPassthrough` — asserts client-sent temperature/top_p/max_tokens/thinking_budget_tokens forwarded verbatim; intent defaults apply only when client omitted

**Dependencies**: 1.2 (test harness exists)  
**Commit message**: 
```
feat(proxy): client-wins for temperature/top_p/max_tokens/thinking_budget_tokens

- Replace parameters.update() with _client_wins(body, name, fallback) helper
- Intent defaults apply only when client field absent
- Add # Glass Pipe fallback comments for each intent default
- Fixes R1 (temperature/top_p/max_tokens) and R7 (thinking_budget_tokens)
```

**Rollback**: `git revert <commit>` restores intent-based overrides  
**Risk level**: CRITICAL (core Glass Pipe violation fix)  
**Line estimate**: +15 / -10 lines

---

### Task 1.4: Expand payload to forward full OpenAI field set

**ID**: 1.4  
**Title**: Expand payload to forward full OpenAI field set **[x]**  
**R-coverage**: R11  
**Files**: 
- `constants.py` (modified, near line 238, ~8 lines added)
- `routes.py` (modified, lines 518-529, ~8 lines modified)

**Spec scenarios**: 
- `glass-pipe-passthrough REQ-3 / Scenario-1, Scenario-2` (forward full field set)

**Test class / scenarios**: 
- `TestPayloadFields` — asserts response_format/seed/tool_choice/parallel_tool_calls forwarded; omitted fields absent

**Dependencies**: 1.2 (test harness exists)  
**Commit message**: 
```
feat(proxy): forward full OpenAI field set in payload build

- Add OPENAI_FORWARD_FIELDS tuple to constants.py (tool_choice, parallel_tool_calls, etc.)
- Replace fixed 8-field payload build with loop over forward set
- Client-sent values forwarded as-is; no validation, no transformation
- Fixes R11 (silently dropped fields)
```

**Rollback**: `git revert <commit>` restores 8-field-only payload  
**Risk level**: WARNING (expands proxy surface; no validation)  
**Line estimate**: +16 / -8 lines

---

### Task 1.5: Document stop-sequence filter as Glass-Pipe exception

**ID**: 1.5  
**Title**: Document stop-sequence filter as Glass-Pipe exception **[x]**  
**R-coverage**: R8  
**Files**: 
- `routes.py` (modified, lines 511-516, ~2 lines added)

**Spec scenarios**: 
- `glass-pipe-exceptions REQ-2 / Scenario-1` (stop-seq filter comment present)

**Test class / scenarios**: 
- `TestGlassPipeDocLabels` — asserts source contains "Glass Pipe exception — intentional" label

**Dependencies**: 1.2 (test harness exists)  
**Commit message**: 
```
docs(proxy): label stop-sequence filter as intentional Glass-Pipe exception

- Add comment explaining why Observation:/```output removal is tool-favorable
- No behavior change; documentation only
- Fixes R8 (undocumented exception)
```

**Rollback**: `git revert <commit>` removes comment  
**Risk level**: SUGGESTION (documentation only)  
**Line estimate**: +2 lines

---

### Task 1.6: Document translate_to_deepseek_r1 as Glass-Pipe exception

**ID**: 1.6  
**Title**: Document translate_to_deepseek_r1 as Glass-Pipe exception **[x]**  
**R-coverage**: R9  
**Files**: 
- `llm.py` (modified, lines 113-149, ~3 lines added)

**Spec scenarios**: 
- `glass-pipe-exceptions REQ-3 / Scenario-1` (translate function label present)

**Test class / scenarios**: 
- `TestGlassPipeDocLabels` — asserts docstring contains "Glass-Pipe exception — intentional"

**Dependencies**: 1.2 (test harness exists)  
**Commit message**: 
```
docs(llm): label translate_to_deepseek_r1 as structural Glass-Pipe exception

- Add explicit exception label to docstring
- No behavior change; documentation only
- Fixes R9 (undocumented exception)
```

**Rollback**: `git revert <commit>` removes label  
**Risk level**: SUGGESTION (documentation only)  
**Line estimate**: +3 lines

---

### Task 1.7: Add test classes for parameter plane and doc labels

**ID**: 1.7  
**Title**: Add test classes for parameter plane and doc labels **[x]**  
**R-coverage**: R10 (test coverage for R1, R7, R11, R2, R8, R9)  
**Files**: 
- `tests/glass_pipe_test.py` (new, ~180 lines)

**Spec scenarios**: 
- `glass-pipe-passthrough REQ-1, REQ-2, REQ-3` (all scenarios)
- `glass-pipe-exceptions REQ-1, REQ-2, REQ-3` (all scenarios)
- `test-infrastructure REQ-1 / Scenario-1, Scenario-2` (harness green/red)

**Test class / scenarios**: 
- `TestParameterPassthrough` (R1, R7)
- `TestPayloadFields` (R11)
- `TestGlassPipeDocLabels` (R2, R8, R9)
- `TestHarnessSelf` (R10 self-test)

**Dependencies**: 1.3, 1.4, 1.5, 1.6 (all parameter plane changes landed)  
**Commit message**: 
```
test(proxy): add glass_pipe_test.py with parameter plane and doc label tests

- TestParameterPassthrough: client-wins for temperature/top_p/max_tokens/thinking_budget_tokens
- TestPayloadFields: full OpenAI field set forwarded
- TestGlassPipeDocLabels: stop-seq and translate_to_deepseek_r1 labeled
- TestHarnessSelf: harness self-test (pytest-asyncio, import safety)
- All tests pass against PR1 changes; would fail red on regression
```

**Rollback**: `git revert <commit>` removes tests/glass_pipe_test.py  
**Risk level**: CRITICAL (verification gate for PR1)  
**Line estimate**: +180 lines

---

### PR1 Line Budget Breakdown

| Task | Lines Added | Lines Modified/Deleted | Total |
|------|-------------|------------------------|-------|
| 1.1 | +8 | 0 | 8 |
| 1.2 | +60 | 0 | 60 |
| 1.3 | +15 | -10 | 25 |
| 1.4 | +16 | -8 | 24 |
| 1.5 | +2 | 0 | 2 |
| 1.6 | +3 | 0 | 3 |
| 1.7 | +180 | 0 | 180 |
| **Total** | **+284** | **-18** | **~210** |

**Largest single task**: 1.7 (180 lines) — test file; flagged but acceptable (test code is verbose)

---

## PR2: Stream + Tools Plane (~180 lines)

**R-coverage**: R3, R4, R5, R14, R6, R12, R13, R15, R16  
**File surface**: `routes.py` (modified), `auditing.py` (modified), `database.py` (modified)  
**Dependencies**: PR1 merged (test harness exists for verification)  
**Test surface**: `TestToolsPassthrough`, `TestMidToolFlowOptOut`, `TestStreamIntegrity`, `TestLoopStripEvent`, `TestGuillotine`, `TestAuditorToolCalls`, `TestStreamToolCallExtraction`, `TestDBToolCallAccum`  
**Rollback**: `git revert <PR2-merge>` restores `tools = None` for Lane B, `audit_override` finish reason, and text-only auditor feed

### Task 2.1: Remove Lane B unconditional tools strip

**ID**: 2.1  
**Title**: Remove Lane B unconditional tools strip **[x]**  
**R-coverage**: R3  
**Files**: 
- `routes.py` (modified, lines 172-176, ~1 line deleted)

**Spec scenarios**: 
- `glass-pipe-passthrough REQ-4 / Scenario-1, Scenario-2` (Lane B tools passthrough)

**Test class / scenarios**: 
- `TestToolsPassthrough` — asserts Lane B caller with tools → tools present in payload; Lane B without tools → no injection

**Dependencies**: PR1 merged  
**Commit message**: 
```
feat(proxy): remove unconditional tools=None for Lane B/IDE callers

- Delete tools = None block at routes.py:174
- Client tools always forwarded to model; empty array → model treats as no-tools
- BREAKING: clients depending on Lane B tools=None now receive tool_calls
- Document in CHANGELOG; loop-detection strip (R4) still applies per-turn
- Fixes R3 (Glass Pipe violation on tools plane)
```

**Rollback**: `git revert <commit>` restores `tools = None` for Lane B  
**Risk level**: CRITICAL (BREAKING change for IDE clients)  
**Line estimate**: +1 / -3 lines

---

### Task 2.2: Emit kinver.proxy.tool_stripped event before loop-detection strip

**ID**: 2.2  
**Title**: Emit kinver.proxy.tool_stripped event before loop-detection strip **[x]**  
**R-coverage**: R4  
**Files**: 
- `routes.py` (modified, lines 209-214, ~5 lines added)

**Spec scenarios**: 
- `glass-pipe-stream-integrity REQ-3 / Scenario-1, Scenario-2` (event before strip, per-turn)
- `glass-pipe-stream-integrity REQ-4 / Scenario-1` (payload shape)

**Test class / scenarios**: 
- `TestLoopStripEvent` — asserts `event: kinver.proxy.tool_stripped` emitted before strip; payload has kind/ts/data.reason/data.domain

**Dependencies**: 2.1 (Lane B strip removed)  
**Commit message**: 
```
feat(proxy): emit kinver.proxy.tool_stripped event before loop-detection strip

- Yield _emit_proxy_event("tool_stripped", {reason, domain}) before tools = None
- Per-turn only; re-emit next turn if still looping
- Standard OpenAI clients ignore unknown event types
- Fixes R4 (silent tools strip with zero client signal)
```

**Rollback**: `git revert <commit>` removes event emission  
**Risk level**: WARNING (new SSE event type; must be well-formed)  
**Line estimate**: +5 lines

---

### Task 2.3: Add _emit_proxy_event helper and convert triage/loading/cache to kinver.proxy.status

**ID**: 2.3  
**Title**: Add _emit_proxy_event helper and convert triage/loading/cache to kinver.proxy.status **[x]**  
**R-coverage**: R5  
**Files**: 
- `routes.py` (modified, lines ~1046 (helper), 640-641, 784, 851, ~25 lines added/modified)

**Spec scenarios**: 
- `glass-pipe-stream-integrity REQ-1 / Scenario-1, Scenario-2` (triage as event, not content)
- `glass-pipe-stream-integrity REQ-2 / Scenario-1, Scenario-2` (payload shape, unknown event ignored)

**Test class / scenarios**: 
- `TestStreamIntegrity` — asserts first SSE line is `event: kinver.proxy.status`; no banner in delta.content; payload has kind/ts/data

**Dependencies**: 2.2 (tool_stripped event exists)  
**Commit message**: 
```
feat(proxy): emit triage/loading/cache as kinver.proxy.status events

- Add _emit_proxy_event(kind, data) helper returning formatted SSE event
- Replace _make_system_chunk yields with _emit_proxy_event("status", {kind, message})
- Triage, loading, cache_restore now on dedicated event type
- Standard OpenAI clients ignore unknown event types; Kinver frontends subscribe
- Fixes R5 (synthetic content deltas polluting assistant message text)
```

**Rollback**: `git revert <commit>` restores _make_system_chunk yields  
**Risk level**: CRITICAL (breaks clients parsing banners from delta.content)  
**Line estimate**: +25 / -10 lines

---

### Task 2.4: Convert pause/resume/cloud embedded commands to kinver.proxy.status

**ID**: 2.4  
**Title**: Convert pause/resume/cloud embedded commands to kinver.proxy.status **[x]**  
**R-coverage**: R14  
**Files**: 
- `routes.py` (modified, lines 939, 944, 946, 961, 975, ~10 lines modified)

**Spec scenarios**: 
- `glass-pipe-stream-integrity REQ-1 / Scenario-2` (pause/resume as event)

**Test class / scenarios**: 
- `TestStreamIntegrity` — asserts pause/resume/cloud emitted as `event: kinver.proxy.status`; no delta.content

**Dependencies**: 2.3 (_emit_proxy_event helper exists)  
**Commit message**: 
```
feat(proxy): convert pause/resume/cloud embedded commands to kinver.proxy.status

- Replace _make_system_chunk with _emit_proxy_event("status", {kind, message})
- pause, resume, cloud now on dedicated event type
- Fixes R14 (embedded command synthetic content)
```

**Rollback**: `git revert <commit>` restores _make_system_chunk for embedded commands  
**Risk level**: SUGGESTION (less critical than R5; embedded commands are rare)  
**Line estimate**: +10 / -5 lines

---

### Task 2.5: Rewrite Guillotine to emit audit_halt event + error chunk with finish_reason=stop

**ID**: 2.5  
**Title**: Rewrite Guillotine to emit audit_halt event + error chunk with finish_reason=stop **[x]**  
**R-coverage**: R6  
**Files**: 
- `routes.py` (modified, lines 888-917, ~20 lines modified)

**Spec scenarios**: 
- `glass-pipe-stream-integrity REQ-5 / Scenario-1, Scenario-2` (no synthetic content, finish_reason=stop, audit_halt event)
- `glass-pipe-stream-integrity REQ-6 / Scenario-1` (audit_halt payload shape)

**Test class / scenarios**: 
- `TestGuillotine` — asserts FATAL during tool_calls → no delta.content with [PROXY AUDIT OVERRIDE]; terminating chunk has finish_reason="stop" and error.type="proxy_audit_halt"; `event: kinver.proxy.audit_halt` emitted

**Dependencies**: 2.3 (_emit_proxy_event helper exists)  
**Commit message**: 
```
feat(proxy): Guillotine emits audit_halt event + standards-compliant error chunk

- Emit _emit_proxy_event("audit_halt", {reason}) before final chunk
- Final chunk: {error: {message, type: "proxy_audit_halt"}, choices: [{delta: {}, finish_reason: "stop"}]}
- Remove audit_override finish reason and [PROXY AUDIT OVERRIDE…] content
- Matches existing proxy-error shape (routes.py:703-709); no tool_call corruption
- Fixes R6 (synthetic content during tool_calls corrupts JSON)
```

**Rollback**: `git revert <commit>` restores audit_override finish reason and synthetic content  
**Risk level**: CRITICAL (core stream integrity fix; breaks clients expecting audit_override)  
**Line estimate**: +20 / -15 lines

---

### Task 2.6: Update feed_chunk to accept chunk dict and extract tool_calls

**ID**: 2.6  
**Title**: Update feed_chunk to accept chunk dict and extract tool_calls **[x]**  
**R-coverage**: R12  
**Files**: 
- `auditing.py` (modified, lines 187-210, ~10 lines modified)
- `routes.py` (modified, lines 674-675, ~2 lines modified)

**Spec scenarios**: 
- `glass-pipe-auditor-coverage REQ-1 / Scenario-1, Scenario-2` (feed_chunk accepts dict, sees tool_calls)

**Test class / scenarios**: 
- `TestAuditorToolCalls` — asserts feed_chunk with tool_calls delta → auditor records it; chunk with content only → no crash

**Dependencies**: 2.5 (Guillotine rewritten)  
**Commit message**: 
```
feat(auditing): feed_chunk accepts chunk dict and extracts tool_calls

- Change signature: feed_chunk(text) → feed_chunk(chunk: dict)
- _ingest_loop extracts delta.content AND delta.tool_calls
- Update caller at routes.py:674 to feed full chunk dict
- Queue holds heterogeneous chunks (text + tool_calls)
- Fixes R12 (auditor blind to tool_calls deltas)
```

**Rollback**: `git revert <commit>` restores feed_chunk(text) signature  
**Risk level**: WARNING (interface change; all callers must update)  
**Line estimate**: +12 / -5 lines

---

### Task 2.7: Add X-Kinver-Allow-Mid-Tool-Switch opt-out header

**ID**: 2.7  
**Title**: Add X-Kinver-Allow-Mid-Tool-Switch opt-out header **[x]**  
**R-coverage**: R13  
**Files**: 
- `routes.py` (modified, lines 314-343, ~8 lines added)

**Spec scenarios**: 
- `glass-pipe-passthrough REQ-5 / Scenario-1, Scenario-2` (header disables lock; absent preserves lock)

**Test class / scenarios**: 
- `TestMidToolFlowOptOut` — asserts header present → lock disabled; header absent → lock preserved

**Dependencies**: 2.6 (auditor updated)  
**Commit message**: 
```
feat(proxy): add X-Kinver-Allow-Mid-Tool-Switch opt-out header

- Read request.headers.get("X-Kinver-Allow-Mid-Tool-Switch")
- If "true" (case-insensitive), skip has_tool_calls short-circuit
- Header absent or "false" preserves current implicit lock
- No new public API; header-only opt-out
- Fixes R13 (implicit mid-tool-flow lock with no client opt-out)
```

**Rollback**: `git revert <commit>` removes header read  
**Risk level**: SUGGESTION (opt-out only; default behavior unchanged)  
**Line estimate**: +8 lines

---

### Task 2.8: Extract delta.tool_calls in stream loop and pass to auditor + DB

**ID**: 2.8  
**Title**: Extract delta.tool_calls in stream loop and pass to auditor + DB **[x]**  
**R-coverage**: R15  
**Files**: 
- `routes.py` (modified, lines 664-688, ~10 lines added)

**Spec scenarios**: 
- `glass-pipe-auditor-coverage REQ-2 / Scenario-1, Scenario-2` (tool_calls extracted, passed to auditor + DB)

**Test class / scenarios**: 
- `TestStreamToolCallExtraction` — asserts delta.tool_calls extracted; passed to auditor.feed_chunk and accumulated in full_tool_calls list

**Dependencies**: 2.6 (auditor accepts chunk dict)  
**Commit message**: 
```
feat(proxy): extract delta.tool_calls in stream loop for auditor + DB

- After delta_content extraction, also extract delta_tool_calls = delta.get("tool_calls")
- Accumulate into full_tool_calls: list = [] (alongside full_content)
- Pass full chunk dict to auditor.feed_chunk (per R12)
- Chunk dict already contains tool_calls (passed through at line 687)
- Fixes R15 (proxy blind to tool_calls in stream)
```

**Rollback**: `git revert <commit>` removes tool_calls extraction  
**Risk level**: WARNING (expands stream loop surface)  
**Line estimate**: +10 lines

---

### Task 2.9: Update complete_job to accumulate tool_calls as JSON string

**ID**: 2.9  
**Title**: Update complete_job to accumulate tool_calls as JSON string **[x]**  
**R-coverage**: R16  
**Files**: 
- `database.py` (modified, lines 398-411, ~8 lines modified)
- `routes.py` (modified, lines 691-695, ~3 lines modified)

**Spec scenarios**: 
- `glass-pipe-auditor-coverage REQ-3 / Scenario-1, Scenario-2` (tool_calls persisted as JSON)

**Test class / scenarios**: 
- `TestDBToolCallAccum` — asserts stream with tool_calls → DB row stores JSON; stream without → plain text only

**Dependencies**: 2.8 (tool_calls accumulated in stream loop)  
**Commit message**: 
```
feat(database): complete_job accumulates tool_calls as JSON string

- Add tool_calls_json: str = "" parameter to complete_job
- Write partial_content = full_content + ("|||TOOL_CALLS|||" + tool_calls_json if tool_calls_json else "")
- No schema migration; sentinel-appended JSON in existing column
- Update caller at routes.py:691 to pass json.dumps(full_tool_calls) if full_tool_calls
- RISK: any existing reader of partial_content as plain text receives concatenation
- Mitigation: audit grep "partial_content" consumers before merge
- Fixes R16 (tool_calls info lost after stream completion)
```

**Rollback**: `git revert <commit>` restores complete_job(full_content) signature  
**Risk level**: WARNING (sentinel append may break plain-text readers)  
**Line estimate**: +11 / -3 lines

---

### Task 2.10: Add CHANGELOG entry for R3 BREAKING change

**ID**: 2.10  
**Title**: Add CHANGELOG entry for R3 BREAKING change **[x]**  
**R-coverage**: R3 (documentation)  
**Files**: 
- `CHANGELOG.md` (modified or new, ~10 lines added)

**Spec scenarios**: 
- None (documentation only)

**Test class / scenarios**: 
- None (no behavioral test)

**Dependencies**: 2.1 (R3 landed)  
**Commit message**: 
```
docs: add CHANGELOG entry for R3 BREAKING change (Lane B tools passthrough)

- Document that Lane B/IDE callers now receive tool_calls (tools=None removed)
- Clients depending on unconditional strip are broken
- No opt-in strip header this pass (deferred)
- Loop-detection strip (R4) still applies per-turn
- Rollback: git revert PR2 restores tools=None for Lane B
```

**Rollback**: `git revert <commit>` removes CHANGELOG entry  
**Risk level**: WARNING (documentation; critical for user awareness)  
**Line estimate**: +10 lines

---

### Task 2.11: Add test classes for stream + tools plane

**ID**: 2.11  
**Title**: Add test classes for stream + tools plane **[x]**  
**R-coverage**: R10 (test coverage for R3, R4, R5, R14, R6, R12, R13, R15, R16)  
**Files**: 
- `tests/glass_pipe_test.py` (modified, ~150 lines added)

**Spec scenarios**: 
- `glass-pipe-passthrough REQ-4, REQ-5` (all scenarios)
- `glass-pipe-stream-integrity REQ-1, REQ-2, REQ-3, REQ-4, REQ-5, REQ-6` (all scenarios)
- `glass-pipe-auditor-coverage REQ-1, REQ-2, REQ-3` (all scenarios)

**Test class / scenarios**: 
- `TestToolsPassthrough` (R3)
- `TestMidToolFlowOptOut` (R13)
- `TestStreamIntegrity` (R5, R14)
- `TestLoopStripEvent` (R4)
- `TestGuillotine` (R6)
- `TestAuditorToolCalls` (R12)
- `TestStreamToolCallExtraction` (R15)
- `TestDBToolCallAccum` (R16)

**Dependencies**: 2.1–2.9 (all stream + tools changes landed)  
**Commit message**: 
```
test(proxy): add stream + tools plane tests to glass_pipe_test.py

- TestToolsPassthrough: Lane B tools forwarded; no injection
- TestMidToolFlowOptOut: header disables lock; absent preserves lock
- TestStreamIntegrity: triage/loading/cache/pause/resume/cloud as kinver.proxy.status
- TestLoopStripEvent: tool_stripped event before strip; per-turn
- TestGuillotine: audit_halt event + error chunk; no synthetic content
- TestAuditorToolCalls: feed_chunk accepts dict; sees tool_calls
- TestStreamToolCallExtraction: delta.tool_calls extracted; passed to auditor + DB
- TestDBToolCallAccum: complete_job stores tool_calls JSON; sentinel-split
- All tests pass against PR2 changes; would fail red on regression
```

**Rollback**: `git revert <commit>` removes test classes  
**Risk level**: CRITICAL (verification gate for PR2)  
**Line estimate**: +150 lines

---

### PR2 Line Budget Breakdown

| Task | Lines Added | Lines Modified/Deleted | Total |
|------|-------------|------------------------|-------|
| 2.1 | +1 | -3 | 4 |
| 2.2 | +5 | 0 | 5 |
| 2.3 | +25 | -10 | 35 |
| 2.4 | +10 | -5 | 15 |
| 2.5 | +20 | -15 | 35 |
| 2.6 | +12 | -5 | 17 |
| 2.7 | +8 | 0 | 8 |
| 2.8 | +10 | 0 | 10 |
| 2.9 | +11 | -3 | 14 |
| 2.10 | +10 | 0 | 10 |
| 2.11 | +150 | 0 | 150 |
| **Total** | **+262** | **-41** | **~180** |

**Largest single task**: 2.11 (150 lines) — test file; flagged but acceptable (test code is verbose)

---

## Cross-PR Considerations

### Order of Merges
1. **PR1 first**: lands test harness + parameter plane. All tests must pass.
2. **PR2 second**: targets PR1's feature branch. All tests must pass. PR2's test classes (2.11) verify PR2's changes against PR1's harness.

### CHANGELOG Entry
- **Who writes it**: Task 2.10 (PR2's first documentation task)
- **When**: After R3 lands (task 2.1), before PR2 merge
- **What content**: R3 BREAKING change (Lane B tools passthrough); list of fixes (R1–R16); rollback instructions

### Test Invocation
```bash
cd <REPO_ROOT> && .venv/bin/pytest -q tests/
```

### Test Bootstrap
- `pytest-asyncio` install is part of task 1.1 (R10)
- Install command: `.venv/bin/pip install pytest-asyncio`
- Do this BEFORE any test runs

### R3 BREAKING Change
- **What**: Clients depending on Lane B `tools=None` now receive `tool_calls`
- **Documentation**: CHANGELOG entry (task 2.10) + commit message body (task 2.1)
- **Mitigation**: No opt-in strip header this pass (deferred); loop-detection strip (R4) still applies per-turn
- **Rollback**: `git revert <PR2-merge>` restores `tools=None` for Lane B

---

## Open Questions Carried Forward

1. **R3 mitigation header** (deferred per proposal): Should a future pass add `X-Kinver-Strip-Tools: true` opt-in for the rare IDE that genuinely wants the strip? Still deferred — not blocking this pass.

2. **R16 sentinel suffix consumer audit**: Before PR2 merges, run `grep "partial_content"` to confirm no existing consumer reads `partial_content` as plain text and would break on the appended JSON. If a consumer exists, switch R16 to a parallel in-memory accumulator (still no migration).

3. **SSE event parser verification**: R10's `TestStreamIntegrity` asserts the `event: kinver.proxy.*` lines are well-formed. Verify against `openai-python` SDK SSE parser during PR1 review to confirm unknown event types are ignored.

4. **strict_tdd re-evaluation trigger**: `strict_tdd: false` until R10 lands. Re-evaluate after PR1 merges and the harness exists. Trigger: post-PR1-merge review.

---

## File Index

### New Files
| File | Purpose | R |
|------|---------|---|
| `pyproject.toml` | pytest + pytest-asyncio config | R10 |
| `tests/conftest.py` | Test harness with mocked dependencies | R10 |
| `tests/glass_pipe_test.py` | One test class per R1–R9 | R10 |

### Modified Files
| File | Changes | R |
|------|---------|---|
| `routes.py` | Parameter passthrough, tools passthrough, SSE events, Guillotine, mid-tool-flow opt-out, stream loop tool_calls extraction | R1, R2, R3, R4, R5, R6, R7, R8, R11, R13, R14, R15 |
| `llm.py` | Doc-only: translate_to_deepseek_r1 exception label | R9 |
| `auditing.py` | feed_chunk accepts chunk dict, extracts tool_calls | R12 |
| `database.py` | complete_job accumulates tool_calls as JSON string | R16 |
| `constants.py` | OPENAI_FORWARD_FIELDS tuple constant | R11 |
| `CHANGELOG.md` | R3 BREAKING change documentation | R3 |

### Design Artifacts (openspec)
| File | Purpose |
|------|---------|
| `openspec/changes/glass-pipe-hardening/proposal.md` | Intent, scope, approach |
| `openspec/changes/glass-pipe-hardening/exploration.md` | Codebase analysis, violations |
| `openspec/changes/glass-pipe-hardening/design.md` | Technical design, architecture |
| `openspec/changes/glass-pipe-hardening/tasks.md` | This file — implementation tasks |
| `openspec/changes/glass-pipe-hardening/specs/README.md` | Spec index |
| `openspec/changes/glass-pipe-hardening/specs/glass-pipe-passthrough/spec.md` | R1, R7, R11, R3, R13 |
| `openspec/changes/glass-pipe-hardening/specs/glass-pipe-stream-integrity/spec.md` | R5, R14, R4, R6 |
| `openspec/changes/glass-pipe-hardening/specs/glass-pipe-auditor-coverage/spec.md` | R12, R15, R16 |
| `openspec/changes/glass-pipe-hardening/specs/glass-pipe-exceptions/spec.md` | R2, R8, R9 |
| `openspec/changes/glass-pipe-hardening/specs/test-infrastructure/spec.md` | R10 |
