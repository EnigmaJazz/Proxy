# SDD Init Context — bridge-cycle-4

Change cycle: `bridge-cycle-4` (OpenCode bridge reliability — cycle 4)
Initialized: 2026-08-09 | Persistence: Both (Engram + OpenSpec)
Branch: `sdd/opencode-bridge-sdd-reliability/pr-5`
Prior cycles: `bridge-cycle-1`, `bridge-cycle-2`, `bridge-cycle-3`, `bridge-docs` (all under `openspec/changes/`)

## Project

- **Name**: Kinver AI proxy (`kinver-hub-proxy`)
- **Stack**: Python 3.14.4 (venv `.venv/`), FastAPI 0.136.3, uvicorn 0.48.0, uvloop event loop; httpx 0.28.1 (ASGITransport for in-process API tests); SQLite job queue (`ai_queue.db`) for queue-worker escalation
- **Module map**: `proxy.py` (app + lifespan + AppState wiring), `routes.py` (chat-completions + SSE streaming), `routing.py` (RouteDecision / ROUTE_MAP / frontdesk), `constants.py` (single source of truth), `llm.py`, `database.py`, `cooling.py`, `systemd.py`, `tools/web_search.py`, `search_enrichment.py`, `opencode_bridge.py` (route to headless `opencode serve` backend), `tools.py` (legacy), `profile_loader.py`, `tools/sync_model_profiles.py`
- **Executable**: FastAPI app served via uvicorn; CLI tooling under `tools/`

## Test setup

- Runner: pytest 9.1.0 + pytest-asyncio 1.4.0 (strict mode)
- **Test command: `.venv/bin/python -m pytest`** (canonical; equivalent to `pytest tests/ -q` used in earlier cycles)
- Verified: `.venv/bin/python -m pytest --version` → pytest 9.1.0 on Python 3.14.4 (2026-08-09)
- Harness: `tests/conftest.py` stubs FlashRank + heavy deps, lifespan disabled; async fixtures MUST use `@pytest_asyncio.fixture` (never `@pytest.fixture`)
- Suite size (prior refresh): ~412 tests collect green; bridge-cycle-2 verified `tests/test_opencode_bridge.py` → 121 passed

## Strict TDD

- **strict_tdd: true** — AGENTS.md rule 7: "new code paths MUST have a regression test"; the pre-harness "strict_tdd stays false" note is superseded (harness exists). Enforcement: `gga` pre-commit hook reviews `*.py`/`*.ts` against AGENTS.md (STRICT_MODE=true).

## Conventions (AGENTS.md hard rules)

1. **Glass Pipe Rule R1**: proxy never alters client content, tool definitions, or sampling params on direct calls. Intent-based `temperature`/`top_p`/`max_tokens` overrides FORBIDDEN. Carve-outs (outbound model-copy only, opt-out via `X-Proxy-Context-Governance: off` / `X-Proxy-Search-Enrichment: off`, never touch user/assistant text or the DB audit copy): context governance (tool-result truncation/offload) and search-result enrichment (thin-result enrichment).
2. **No payload injection in flight**: no synthetic assistant deltas into in-flight `tool_calls` JSON; triage metadata goes in a separate SSE chunk (`_make_system_chunk`) before the first model chunk or in `:` comment lines.
3. **Async everywhere** in the request path; all I/O awaited.
4. **Type hints required**: `Optional[X]`, `tuple[...]` / `dict[...]` / `list[...]` (no `Tuple`/`Dict`/`List`).
5. **Dataclasses for structured state** (`RouteDecision` etc. in `routing.py`, not `routes.py`).
6. **No global state outside `proxy.app.state`**; module-level mutable globals forbidden except cached constants in `constants.py`.
7. **Tests mandatory** for new code paths (harness in `tests/conftest.py`).
8. **No AI attribution**: no `Co-Authored-By:` trailers; conventional commits `type(scope): subject` (`fix(scope):`, `feat(scope):`, `chore:`).
9. **No `print()`**: `get_logger(name)` from constants.py; CLI stdout deliverables excepted.
10. **No bare `except:`**; `except Exception` only at process boundaries (lifespan, queue worker) and terminal SSE stream boundary.

## Session preflight (orchestrator-provided)

| Setting | Value |
|---|---|
| Pace | Automatic |
| Artifacts | Both (Engram + OpenSpec) |
| PRs | Single PR |
| Review budget | 400 lines |
| Code writer | Cloud model |

## Project context — bridge reliability program

- Program: harden the OpenCode bridge (`opencode_bridge.py`, `model: "opencode"`, `/opencode`, queue-worker escalation).
- Prior cycles (all under `openspec/changes/`): `bridge-cycle-1` (proposal), `bridge-cycle-2` (init-context/proposal/spec/design/tasks — serve liveness probe), `bridge-cycle-3` (proposal — stream-start recycle ordering, stale-pin drop, verify-before-kill), `bridge-docs` (proposal/design/specs/tasks/archive-report/verify-report — bridge docs + specs).
- Current: branch `sdd/opencode-bridge-sdd-reliability/pr-5`; cycle `bridge-cycle-4` starts now.
- Artifact store: Both (Engram observations + OpenSpec files).

## Reference docs

- `AGENTS.md` — review rules + module map + scope conventions
- `openspec/changes/glass-pipe-followups/` — R17/R18 specs and design
- `openspec/changes/bridge-docs/specs/` — bridge specs (task-cited `openspec/specs/opencode-bridge` does not exist on disk; actual bridge specs live under `bridge-docs/specs`)
- `docs/brainstorms/2026-07-05-proxy-glass-pipe-hardening-requirements.md` — R1–R10 violations background (task-cited `docs/opencode-bridge.md` does not exist on disk)
- `openspec/changes/README.md` — prior init-context refresh (2026-08-09)

## Skill registry

- Path: `.atl/skill-registry.md` (repo root), refreshed 2026-08-09 via `gentle-ai skill-registry refresh --force` — 14 user-scope skills (branch-pr, chained-pr, judgment-day, rdd-defect-workflow, work-unit-commits, systemic-issue-triage, playwright-cli, etc.). Delegator use only: pass exact `SKILL.md` paths to subagents. The `sdd-*` family (sdd-init/explore/propose/spec/design/tasks/apply/verify/archive/onboard) is delegate-only and intentionally excluded from the registry; the orchestrator injects paths directly.

## Engram persistence

- Topic: `sdd-init/kinver-hub-proxy` (project `kinver-hub-proxy`, scope `project`, session `sdd-init`) — this init context row.
- Testing capabilities: `sdd/kinver-hub-proxy/testing-capabilities` (separate observation row).
- Prior manual-save rows exist under topic `sdd-init/kinver-hub/proxy` (session `manual-save-*`, 2026-08-09) — legacy slash key; the canonical init row uses the hyphenated key per the orchestrator contract. No `openspec/config.yaml` was created (OpenSpec defaults apply; conventions documented in `openspec/changes/README.md`).
