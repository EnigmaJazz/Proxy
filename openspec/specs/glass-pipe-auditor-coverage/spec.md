# Glass-Pipe Auditor Coverage Specification

## Purpose

The ShadowAuditor and DB persistence observe `tool_calls` deltas alongside text deltas, so function-call information is available for audit decisions and post-stream inspection. Anchors R12, R15, R16.

## Requirements

### REQ-1: feed_chunk accepts chunk dict and sees tool_calls (R12)

The `feed_chunk` interface SHALL accept the full streaming chunk dict (not only a text string). The auditor SHALL extract `delta.content` AND `delta.tool_calls` from the chunk. The caller at the stream loop SHALL feed the full chunk to the auditor.

#### Scenario-1: chunk with tool_calls delta

- GIVEN a streaming chunk carries `delta.tool_calls` and no `delta.content`
- WHEN the stream loop calls `feed_chunk`
- THEN the auditor receives the full chunk dict and observes `delta.tool_calls`

#### Scenario-2: chunk with content only (no tool_calls)

- GIVEN a streaming chunk carries `delta.content` and no `delta.tool_calls`
- WHEN the stream loop calls `feed_chunk` with the full chunk dict
- THEN the auditor extracts `delta.content` and does not crash on missing `tool_calls`

### REQ-2: Stream loop extracts tool_calls for auditor and DB (R15)

The SSE stream loop SHALL extract `delta.tool_calls` alongside `delta.content` from every chunk. Extracted `tool_calls` SHALL be passed to the auditor feed and to the DB accumulator.

#### Scenario-1: tool_calls delta extracted

- GIVEN a chunk in the stream loop carries `delta.tool_calls`
- WHEN the loop processes the chunk
- THEN `delta.tool_calls` is extracted and forwarded to both the auditor and the DB accumulator

#### Scenario-2: chunk without tool_calls

- GIVEN a chunk in the stream loop carries only `delta.content`
- WHEN the loop processes the chunk
- THEN no `tool_calls` is extracted and the existing content path is unchanged

### REQ-3: complete_job accumulates tool_calls as JSON string (R16)

The `complete_job` function SHALL accumulate `tool_calls` alongside `full_content` text. The accumulated `tool_calls` SHALL be serialized as a JSON string stored in the existing content column — no schema migration, no new DB column. Tool-call info MUST survive stream completion into the DB.

#### Scenario-1: stream with tool_calls persisted

- GIVEN a completed stream accumulated text content plus one or more `tool_calls` deltas
- WHEN `complete_job` runs
- THEN the DB row stores both the text content and the accumulated `tool_calls` as a JSON string

#### Scenario-2: stream without tool_calls persisted

- GIVEN a completed stream accumulated only text content
- WHEN `complete_job` runs
- THEN the DB row stores the text content and an empty/absent `tool_calls` JSON without error

## Notes

- Open question resolved: R16 storage shape is JSON string in the existing content column (no new column, no schema migration).