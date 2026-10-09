# SDD Init Context — bridge-cycle-10

Change cycle: `bridge-cycle-10` (OpenCode bridge reliability — cycle 10)
Initialized: 2026-08-09 | Persistence: Both (Engram + OpenSpec) — Engram mirror unavailable in this runtime; OpenSpec filesystem authoritative
Branch: `sdd/opencode-bridge-sdd-reliability/pr-5`
Prior cycles: `bridge-cycle-1` (proposal), `bridge-cycle-2` (serve liveness probe), `bridge-cycle-3` (recycle ordering / stale-pin drop / verify-before-kill), `bridge-cycle-4` (init only), `bridge-cycle-5` (archive), `bridge-cycle-6` (stream exit hygiene), `bridge-cycle-7`/`bridge-cycle-8` (unapplied specs, adopted by cycle-9), `bridge-cycle-9` (replay-prevention adoption, exact serve-PID matching, bounded drain after recycle kills — landed at `30be86b`/`08471ea`) — all under `openspec/changes/`

## Project

- **Name**: Kinver AI proxy (`kinver-hub-proxy`)
- **Stack**: Python 3.14.4 (venv `.venv/`), FastAPI 0.136.3, uvicorn 0.48.0, uvloop event loop; httpx 0.28.1; SQLite job queue (`ai_queue.db`) for queue-worker escalation
- **Module map**: `proxy.py`, `routes.py`, `routing.py`, `constants.py`, `llm.py`, `database.py`, `cooling.py`, `systemd.py`, `tools/web_search.py`, `search_enrichment.py`, `opencode_bridge.py` (route to headless `opencode serve` backend), `tools.py` (legacy), `profile_loader.py`, `tools/sync_model_profiles.py`
- **Executable**: FastAPI app served via uvicorn; CLI tooling under `tools/`; SDD cycle drivers under `scripts/`

## Test setup

- Runner: pytest + pytest-asyncio (strict mode)
- **Test command: `.venv/bin/python -m pytest`**
- Harness: `tests/conftest.py` stubs FlashRank + heavy deps, lifespan disabled; async fixtures MUST use `@pytest_asyncio.fixture`
- Bridge harness: `tests/test_opencode_bridge.py` — hermetic `_FakeClient`/`_FakeResp`/`_FakeStream` scripting, `hermetic_serve` autouse guard, `@pytest.mark.real_recycle` gated tests with fake pids; suite baseline at cycle-10 start: **164 passed** in `test_opencode_bridge.py` (153 cycle-9 + 11 new in-flight tests), full suite 449+

## Strict TDD

- **strict_tdd: true** — AGENTS.md rule 7: "new code paths MUST have a regression test". Enforcement: `gga` pre-commit hook reviews `*.py` against AGENTS.md (STRICT_MODE=true).

## Conventions (AGENTS.md hard rules)

1. **Glass Pipe Rule R1** + documented carve-outs (context governance, date/time stamp, search-result enrichment — outbound model-copy only, opt-out headers).
2. **No payload injection in flight**; triage metadata via separate SSE chunk or `:` comment lines.
3. **Async everywhere** in the request path.
4. **Type hints required**; 5. **Dataclasses for structured state**; 6. **No global state outside `proxy.app.state`** (module-level constants in `opencode_bridge.py` are the pre-existing accepted pattern; `_serve_config_mtime` F5 carve-out documented).
7. **Tests mandatory**; 8. **No AI attribution**, conventional commits `type(scope): subject`; 9. **No `print()`** (logger; CLI stdout deliverable excepted); 10. **No bare `except:`** (process-boundary and terminal SSE-stream exceptions allowed).

## Session preflight (orchestrator-provided, autonomous mode)

- **Pace**: Automatic (no interactive stops; gatekeeper between phases)
- **Artifact store**: Both (Engram + OpenSpec) → OpenSpec filesystem authoritative (Engram unavailable)
- **PRs**: Single PR (`delivery_strategy = single-pr`) — user-supplied preflight choice for this change
- **Review budget**: 400 lines
- **Code writer**: Cloud model (no local-model one-file constraint)

## Cycle-10 subject (from task text + working-tree state)

Land the preserved in-flight work from commit `6cd1204` ("in-flight: blocking-path respawn + pid-reuse guards (OUT OF cycle-8 scope; preserved, not part of delivery)"): the uncommitted `opencode_bridge.py` + `tests/test_opencode_bridge.py` changes currently in the working tree, comprising (A) blocking-path bounded respawn in `opencode_chat`, (B) stale-pin self-heal in `opencode_chat_stream`, (C) pid-reuse guards (`_cmdline_matches_serve` / `_pid_is_serve`) on recycle kills — same adopt-and-land pattern as cycle-9.
