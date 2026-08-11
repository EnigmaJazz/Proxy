# SDD Init Context — bridge-cycle-11

Change cycle: `bridge-cycle-11` (blocking-path reliability trio — connect-only respawn, wedge detection, error-path cleanup)
Initialized: 2026-08-11 | Persistence: Both (Engram + OpenSpec) — OpenSpec filesystem authoritative
Branch: `fix/sdd-cycle-autonomous-delivery`
Prior cycles: `bridge-cycle-1`..`bridge-cycle-10` under `openspec/changes/` — cycle-10 landed stall-aware task abort + serve-accurate artifact-store text at `6f99989` (current HEAD).

## Project

- **Name**: Kinver AI proxy (`kinver-hub-proxy`)
- **Stack**: Python 3.14 (venv `.venv/`), FastAPI 0.136.3, uvicorn, uvloop event loop; httpx 0.28.1; SQLite job queue (`ai_queue.db`) for queue-worker escalation
- **Module map**: `proxy.py`, `routes.py`, `routing.py`, `constants.py`, `llm.py`, `database.py`, `cooling.py`, `systemd.py`, `tools/web_search.py`, `search_enrichment.py`, `opencode_bridge.py` (route to headless `opencode serve` backend), `tools.py` (legacy), `profile_loader.py`, `tools/sync_model_profiles.py`
- **Executable**: FastAPI app served via uvicorn; CLI tooling under `tools/`; SDD cycle drivers under `scripts/`

## Test setup

- Runner: pytest + pytest-asyncio (strict mode)
- **Test command: `.venv/bin/python -m pytest`**
- Harness: `tests/conftest.py` stubs FlashRank + heavy deps, lifespan disabled; async fixtures MUST use `@pytest_asyncio.fixture`
- Bridge harness: `tests/test_opencode_bridge.py` — hermetic `_FakeClient`/`_FakeResp`/`_FakeStream` scripting, `hermetic_serve` autouse guard, `@pytest.mark.real_recycle` gated tests with fake pids

## Strict TDD

- **strict_tdd: true** — AGENTS.md rule 7: "new code paths MUST have a regression test". Enforcement: `gga` pre-commit hook reviews `*.py` against AGENTS.md (STRICT_MODE=true).

## Conventions (AGENTS.md hard rules)

1. **Glass Pipe Rule R1** + documented carve-outs (context governance, date/time stamp, search-result enrichment — outbound model-copy only, opt-out headers).
2. **No payload injection in flight**; triage metadata via separate SSE chunk or `:` comments.
3. **Async everywhere** in the request path.
4. **Type hints required**; 5. **Dataclasses for structured state**; 6. **No global state outside `proxy.app.state`** (module-level constants in `opencode_bridge.py` are the pre-existing accepted pattern).
7. **Tests mandatory**; 8. **No AI attribution**, conventional commits `type(scope): subject`; 9. **No `print()`** (logger; CLI stdout deliverable excepted); 10. **No bare `except:`** (process-boundary and terminal SSE-stream exceptions allowed).

## Session preflight (orchestrator-provided, autonomous mode)

- **Pace**: Automatic (no interactive stops; gatekeeper between phases)
- **Artifact store**: Both (Engram + OpenSpec) → OpenSpec filesystem authoritative (Engram unavailable in this runtime)
- **PRs**: Single PR (`delivery_strategy = single-pr`) — user-supplied preflight choice for this change
- **Review budget**: 400 lines
- **Code writer**: Cloud model (no local-model one-file constraint)
