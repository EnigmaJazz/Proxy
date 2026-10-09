# Proposal: OpenCode Bridge Reference Documentation

## Intent

The OpenCode bridge (repo-root `opencode_bridge.py`, ~1530 lines) has grown significant machinery — serve lifecycle/recycling, permission relay, wedge detection, SDD-autonomous mode — with no stable reference doc (`AGENTS.md` maps it in one line; `docs/brainstorms/` are dated requirement docs). Wrong mental models (blocking/streaming confusion, stale pre-`d9f7284` serve-scoped config) have bitten maintainers. Deliver one current reference doc at `docs/opencode-bridge.md`; pure docs change, zero runtime risk.

## Scope

### In Scope

Create `docs/opencode-bridge.md` (plain reference markdown, English):

1. **What it does** — translates a simple OpenAI-style task into headless `opencode serve` HTTP API (`POST /session` → `POST /session/:id/message`, `prompt_async` + `/event` SSE) so frontends (OpenWebUI, nanobot) reach the opencode agent instead of a local llama model.
2. **Serve lifecycle** — detached child with user's GLOBAL config (`~/.config/opencode`); recycling on low memory (<2GB), 1800s uptime (tool-runner wedging), config mtime drift, before SDD-autonomous cycles.
3. **Two code paths** — blocking `opencode_chat` (fresh session/call, no permission relay; queue-worker `CLOUD_ESCALATION_BACKEND="opencode"`) vs streaming `opencode_chat_stream` (pinned sessions, permission relay, wedge detection; `routes.py` `model: "opencode"`, `/opencode`).
4. **Permission relay** — `external_directory` gate auto-allows READ, relays WRITE/git asks as pending questions (keyed by session id); autonomous SDD mode (`model "opencode-sdd"`, `_SDD_AUTONOMOUS_SYSTEM_PROMPT`) auto-allows all.
5. **Completion model** — step-finish + session-idle quiet periods (8s/3s), polling fallback on SSE close, 120s wedge detection (abort + kill serve), 4-min zombie sweep; `_strip_proxy_status_text` strips sentinel-prefixed triage.
6. **Configuration** — serve/workspace/bridge-path/bin/agent/timeout keys plus `OPCODE_CONFIG_PATH`, `BRIDGE_MODEL_KEYS`, `OPENCODE_SDD_TIMEOUT`, `CLOUD_ESCALATION_BACKEND`, all in `constants.py`.
7. **Gotchas** — module at repo root (NOT `proxy/opencode_bridge.py`); serve HTTP API version-specific (v1.18.15 observed).

Use function names, never line numbers. Match CURRENT code: global config (no `OPENCODE_SERVE_CONFIG_DIR`); autonomous mode is an explicit flag.

### Out of Scope

- No code changes (`opencode_bridge.py`, `routes.py`, `constants.py`, tests).
- No README rewrite, no `AGENTS.md` update, no API redesign.

## Capabilities

Pure documentation — no spec-level behavior changes. **New:** None · **Modified:** None

## Approach

Single reference markdown at `docs/opencode-bridge.md`, structured per Scope, with a blocking-vs-streaming table. Verified against `constants.py` at write time.

## Affected Areas

- `docs/opencode-bridge.md` — New (the deliverable)
- `opencode_bridge.py` — None (subject; documented only)

## Risks

- Conflating blocking vs streaming — Med — separate sections + table
- Drift from current code — Med — function names; explicit framing
- Serve HTTP API drift (v1.18.15) — Med — state observed version
- Path confusion (`proxy/` vs root) — Low — gotcha callout

## Rollback Plan

Delete `docs/opencode-bridge.md` or revert the docs PR. File-only, zero runtime risk.

## Dependencies

None.

## Success Criteria

- Doc exists with all Scope sections
- Blocking and streaming paths described separately
- No `OPENCODE_SERVE_CONFIG_DIR` / serve-scoped XDG references
- Config keys match current `constants.py`
