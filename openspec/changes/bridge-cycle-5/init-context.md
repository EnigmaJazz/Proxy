# SDD Init Context — bridge-cycle-5

Change cycle: `bridge-cycle-5` (OpenCode bridge reliability — cycle 5)
Initialized: 2026-08-09 | Persistence: Both (Engram + OpenSpec)
Branch: `sdd/opencode-bridge-sdd-reliability/pr-5`
Prior cycles: `bridge-cycle-1` (proposal), `bridge-cycle-2` (serve liveness probe), `bridge-cycle-3` (recycle ordering / stale-pin drop / verify-before-kill), `bridge-cycle-4` (init only), `bridge-docs` (bridge docs + specs) — all under `openspec/changes/`

## Project

- **Name**: Kinver AI proxy (`kinver-hub-proxy`)
- **Stack**: Python 3.14.4 (venv `.venv/`), FastAPI 0.136.3, uvicorn 0.48.0, uvloop event loop; httpx 0.28.1 (ASGITransport for in-process API tests); SQLite job queue (`ai_queue.db`) for queue-worker escalation
- **Module map**: `proxy.py` (app + lifespan + AppState wiring), `routes.py` (chat-completions + SSE streaming), `routing.py` (RouteDecision / ROUTE_MAP / frontdesk), `constants.py` (single source of truth), `llm.py`, `database.py`, `cooling.py`, `systemd.py`, `tools/web_search.py`, `search_enrichment.py`, `opencode_bridge.py` (route to headless `opencode serve` backend), `tools.py` (legacy), `profile_loader.py`, `tools/sync_model_profiles.py`
- **Executable**: FastAPI app served via uvicorn; CLI tooling under `tools/`; SDD cycle drivers under `scripts/`

## Test setup

- Runner: pytest 9.1.0 + pytest-asyncio 1.4.0 (strict mode)
- **Test command: `.venv/bin/python -m pytest`** (canonical; equivalent to `pytest tests/ -q` used in earlier cycles)
- Harness: `tests/conftest.py` stubs FlashRank + heavy deps, lifespan disabled; async fixtures MUST use `@pytest_asyncio.fixture` (never `@pytest.fixture`)
- Bridge harness: `tests/test_opencode_bridge.py` (2398 lines) — hermetic `_FakeClient`/`_FakeResp` scripting, no live serve, no real /proc; `@pytest.mark.real_recycle` gated tests with fake pids

## Strict TDD

- **strict_tdd: true** — AGENTS.md rule 7: "new code paths MUST have a regression test". Enforcement: `gga` pre-commit hook reviews `*.py`/`*.ts` against AGENTS.md (STRICT_MODE=true).

## Conventions (AGENTS.md hard rules)

1. **Glass Pipe Rule R1**: proxy never alters client content, tool definitions, or sampling params on direct calls. Intent-based overrides FORBIDDEN. Carve-outs (outbound model-copy only, opt-out headers, never touch user/assistant text or the DB audit copy): context governance + search-result enrichment.
2. **No payload injection in flight**: no synthetic assistant deltas into in-flight `tool_calls` JSON; triage metadata in a separate SSE chunk (`_make_system_chunk`) before the first model chunk or in `:` comment lines.
3. **Async everywhere** in the request path; all I/O awaited.
4. **Type hints required**: `Optional[X]`, `tuple[...]` / `dict[...]` / `list[...]`.
5. **Dataclasses for structured state** (`RouteDecision` etc. in `routing.py`).
6. **No global state outside `proxy.app.state`**; module-level mutable globals forbidden except cached constants in `constants.py` (module-level constants in `opencode_bridge.py` are the pre-existing accepted pattern).
7. **Tests mandatory** for new code paths.
8. **No AI attribution**: no `Co-Authored-By:` trailers; conventional commits `type(scope): subject`.
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
- Prior cycles: `bridge-cycle-2` (serve liveness probe: any HTTP response = alive, 3s probe timeout, bounded 4-probe drain), `bridge-cycle-3` (recycle-before-ensure ordering ⇒ in-line respawn; stale-pin drop on successful status fetch; verify-before-kill pid-reuse guard), `bridge-docs` (bridge docs + specs). Post-cycle fixes: serve data/cache isolation, stability mode + config-drift auto-recycle, wedge detection (never kill the serve for a wedged tool; stale-part ignore; 600s task-tool wedge threshold), zombie-session sweep, inline-fallback rule for sub-agent delivery.
- Current: branch `sdd/opencode-bridge-sdd-reliability/pr-5`; cycle `bridge-cycle-5` starts now. Cycle 4 was initialized (init-context only) but never completed; cycle 5 supersedes it.
- Artifact store: Both (Engram observations + OpenSpec files).

## Reference docs

- `AGENTS.md` — review rules + module map + scope conventions
- `openspec/changes/glass-pipe-followups/` — R17/R18 specs and design
- `openspec/changes/bridge-docs/specs/bridge-docs/spec.md` — bridge documentation spec
- `docs/brainstorms/2026-07-05-proxy-glass-pipe-hardening-requirements.md` — R1–R10 violations background
- `openspec/changes/README.md` — prior init-context refresh (2026-08-09)

## Skill registry

- Path: `.atl/skill-registry.md` (repo root), refreshed 2026-08-09 via `gentle-ai skill-registry refresh --force` — 14 user-scope skills (branch-pr, chained-pr, judgment-day, rdd-defect-workflow, work-unit-commits, systemic-issue-triage, playwright-cli, etc.). Delegator use only: pass exact `SKILL.md` paths to subagents. The `sdd-*` family is delegate-only and excluded from the registry; the orchestrator injects paths directly.

## Engram persistence

- Topic: `sdd-init/kinver-hub-proxy` (project `kinver-hub-proxy`, scope `project`, session `sdd-init`) — this init context row (established cycle 4; refreshed for cycle 5).
- Testing capabilities: `sdd/kinver-hub-proxy/testing-capabilities` (separate observation row).
- No `openspec/config.yaml` was created (OpenSpec defaults apply; conventions documented in `openspec/changes/README.md`).
