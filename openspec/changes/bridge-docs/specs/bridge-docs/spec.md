# Bridge Documentation Specification

## Purpose

Governs the reference document `docs/opencode-bridge.md` describing the repo-root
`opencode_bridge.py` module. Pure documentation: requirements state what the DOC
must satisfy, not proxy runtime behavior. Runtime bridge behavior is governed by
`openspec/specs/opencode-bridge` (sibling change `opencode-bridge-sdd-reliability`).

## Requirements

### REQ-DOC-1: Document exists at the canonical path as plain reference markdown

The deliverable MUST be a single file at `docs/opencode-bridge.md`, written in
plain reference markdown (no generated-site format) in English. It MUST NOT alter
any runtime code or existing doc (`opencode_bridge.py`, `routes.py`,
`constants.py`, `tests/`, `AGENTS.md`, READMEs).

#### Scenario-1: doc present and plain

- GIVEN the change is applied
- WHEN a maintainer opens `docs/opencode-bridge.md`
- THEN a single English reference markdown file exists there
- AND no runtime code or existing doc file was modified by this change

### REQ-DOC-2: Covers the seven scope areas, blocking and streaming paths separate

The doc MUST cover, as distinct sections: (1) what the bridge does (OpenAI-style
task → headless `opencode serve` HTTP API); (2) serve lifecycle (detached child,
user's global config, recycling on low memory / 1800s uptime / config mtime drift
/ before SDD-autonomous cycles); (3) the two code paths described SEPARATELY —
blocking `opencode_chat` (fresh session, no permission relay, queue-worker
`CLOUD_ESCALATION_BACKEND="opencode"`) vs streaming `opencode_chat_stream` (pinned
sessions, permission relay, wedge detection; `model: "opencode"` and `/opencode`);
(4) permission relay (`external_directory` read auto-allow, write/git relay as
pending questions keyed by session id; autonomous mode auto-allow); (5) completion
model (step-finish + session-idle quiet periods, polling fallback on SSE close,
120s wedge detection with abort+kill, 4-min zombie sweep, sentinel stripping);
(6) configuration keys from `constants.py`; (7) gotchas. A blocking-vs-streaming
comparison table MUST separate the two paths.

#### Scenario-1: all seven scope sections present

- GIVEN the finished doc
- WHEN its section headings are reviewed
- THEN all seven scope areas appear as distinct sections
- AND a blocking-vs-streaming comparison table exists

#### Scenario-2: maintainer can tell which path handles a request

- GIVEN a maintainer reads the doc without opening `opencode_bridge.py`
- WHEN they ask whether a request flows via queue-worker escalation, the
  `/opencode` command, or a pinned follow-up stream
- THEN the blocking-vs-streaming section and table identify the path unambiguously

### REQ-DOC-3: Accuracy matches CURRENT code

The doc MUST match the current `opencode_bridge.py` and `constants.py`. The serve
lifecycle MUST state the serve uses the user's GLOBAL config
(`~/.config/opencode`, same as the TUI) and MUST NOT mention
`OPENCODE_SERVE_CONFIG_DIR` or any serve-scoped XDG config (obsolete since
d9f7284). Autonomous mode MUST be described as an explicit flag for model
`"opencode-sdd"` (with `_SDD_AUTONOMOUS_SYSTEM_PROMPT`), NOT a timeout inference.
Code references MUST use function/identifier names, never line numbers. The doc
MUST note the opencode serve HTTP API surface is version-specific (v1.18.15
observed) and may drift. Every configuration key listed MUST exist verbatim in
`constants.py` at the time the doc is written.

#### Scenario-1: no obsolete config references

- GIVEN the finished doc
- WHEN it is searched for `OPENCODE_SERVE_CONFIG_DIR` or "serve-scoped config"
- THEN neither appears and the global-config behavior is stated instead

#### Scenario-2: config keys verified against constants.py

- GIVEN the doc's configuration section
- WHEN each listed key is checked against `constants.py`
- THEN every key exists verbatim and no fabricated key appears

#### Scenario-3: maintainer debugging a wedged session finds the behavior

- GIVEN a maintainer debugging a session wedged in "running"
- WHEN they read the completion-model section
- THEN the 120s wedge detection, abort+kill-serve recycle, and 4-min zombie
  sweep are described by function name and locatable without line numbers

## Notes

- Docs-only change: this spec verifies the DOC, not the bridge. Config-key and
  numeric values are anchored to current code but are explicitly drift-prone —
  REQ-DOC-3 frames the serve HTTP API and any constant as version/time-specific.
