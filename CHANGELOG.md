# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### BREAKING

- `R3` Lane B / IDE callers no longer have `tools=None` applied implicitly.
  Client tools are now forwarded to the model. Any IDE that depended on the
  implicit strip will see `tool_calls` in model responses.

### Added

- `kinver.proxy.status`, `kinver.proxy.tool_stripped`, and
  `kinver.proxy.audit_halt` SSE event types for proxy status reporting.
- `X-Kinver-Allow-Mid-Tool-Switch: true` header to opt out of the implicit
  mid-tool-flow route lock.
- Tool calls are now accumulated alongside `full_content` in job records
  (appended as JSON behind a `|||TOOL_CALLS|||` sentinel in `partial_content`).

### Fixed

- `is_dream` `UnboundLocalError` in the Lane B / IDE code path (latent crash).

### Changed

- Parameter defaults (`temperature`, `top_p`, `max_tokens`,
  `thinking_budget_tokens`) now use client-wins semantics; intent defaults fill
  gaps only.
- Payload forwarding now includes the full OpenAI field set
  (`tool_choice`, `parallel_tool_calls`, etc.) when the client sends them.
- `ShadowAuditor.feed_chunk` now accepts the full chunk dict and observes
  `delta.tool_calls`, not just `delta.content`.
