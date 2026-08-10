# Changes — Kinver AI Proxy

Active change sets live here as `openspec/changes/<change>/` (proposal, spec(s), design, tasks, verify-report).
Completed cycles move to `changes/archive/<change>-<date>/`.

## SDD init context (2026-08-09, refresh)

- **Stack**: Python 3.14.4, FastAPI 0.136.3, uvicorn 0.48.0, uvloop 0.22.1, httpx 0.28.1 (ASGITransport tests), SQLite job queue (`ai_queue.db`).
- **Testing**: pytest 9.1.0 + pytest-asyncio 1.4.0 (strict mode). Command: `pytest tests/ -q` (guard: `-x`). Harness `tests/conftest.py` stubs FlashRank + no-op Database/Systemd/Cooling, lifespan disabled. Async fixtures MUST use `@pytest_asyncio.fixture`, never `@pytest.fixture`. 412 tests collect green.
- **Strict TDD**: `true`. AGENTS.md rule 7 requires a regression test for every new code path; the R10-era "strict_tdd stays false until harness exists" note is superseded (harness exists).
- **Conventions** (AGENTS.md): dataclasses for structured state; type hints required (`Optional[X]`, `tuple[...]`); no module-level mutable globals (state on `proxy.app.state.*`); no `print()` — `get_logger(name)` from `constants.py` (CLI stdout deliverables excepted); no bare `except:`; conventional commits `fix(scope):` / `feat(scope):` / `chore:`; no `Co-Authored-By:` trailers; Glass-Pipe Rule R1 (proxy never alters client content/params on direct calls) with documented carve-outs (context governance, search enrichment).
- **Enforcement**: `gga` pre-commit hook reviews `*.py`/`*.ts` with `AGENTS.md` as rules file (STRICT_MODE=true); CI gates `model_profiles.yaml` drift only.

Engram mirror: topic `sdd-init/kinver-hub/proxy` (project `kinver-hub/proxy`), alias `sdd-init/kinver-proxy`.

> This file records the SDD init guard result. It is not a change set; existing `changes/*` and archived specs are untouched.