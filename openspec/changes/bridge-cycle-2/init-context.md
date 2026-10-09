# SDD Init Context — bridge-cycle-2

Change cycle: `bridge-cycle-2` (OpenCode bridge cycle 2)
Initialized: 2026-08-09 | Persistence: OpenSpec (filesystem authoritative — Engram MCP unavailable this session)
Prior cycle: `openspec/changes/bridge-cycle-1/proposal.md` (2026-08-09)

## Stack

- Python 3.14.4 (venv `.venv/`), FastAPI 0.136.3, uvicorn 0.48.0, uvloop event loop
- httpx 0.28.1 (ASGITransport for in-process API tests)
- SQLite job queue (`ai_queue.db`) for queue-worker escalation
- Target module: `opencode_bridge.py` — routes `model: "opencode"` requests to a headless `opencode serve` backend (`/opencode`, queue-worker escalation)

## Test setup

- Runner: pytest 9.1.0 + pytest-asyncio 1.4.0 (strict mode)
- Verification (2026-08-09): `pytest tests/test_opencode_bridge.py -q` → **121 passed in 11.21s** (exit 0) — harness is green before this cycle starts
- Harness: `tests/conftest.py` stubs FlashRank + heavy deps, lifespan disabled; async fixtures MUST use `@pytest_asyncio.fixture` (never `@pytest.fixture`)
- Wider suite: ~412 tests collect green per prior init refresh (verify before merge)

## Strict TDD

- **strict_tdd: true** — AGENTS.md rule 7: "new code paths MUST have a regression test"; the pre-harness "strict_tdd stays false" note is superseded (harness exists). Enforcement: `gga` pre-commit hook reviews `*.py`/`*.ts` against AGENTS.md (STRICT_MODE=true).

## Skill registry

- Path: `.atl/skill-registry.md` (repo root), last refreshed 2026-08-08 — 14 skills (user scope: branch-pr, chained-pr, judgment-day, rdd-defect-workflow, work-unit-commits, systemic-issue-triage, etc.). Cache: `.atl/.skill-registry.cache.json`. Delegator use only: pass exact `SKILL.md` paths to subagents.

## Prior SDD context found

- `openspec/` exists (active + archived change sets); `openspec/config.yaml` is ABSENT (not created — OpenSpec defaults apply; conventions documented in `openspec/changes/README.md`)
- Prior sdd-init marker: `openspec/changes/README.md` "SDD init context (2026-08-09, refresh)" — records stack/testing/TDD/enforcement + Engram mirror topic `sdd-init/kinver-hub/proxy` (alias `sdd-init/kinver-proxy`); this note supersedes/refreshes it for bridge-cycle-2
- Archived changes: `openspec/changes/archive/glass-pipe-followups-2026-07-08/`, `openspec/changes/archive/professional-as-default-2026-07-25/`
- Legacy SDD layout: `sdd/explore-tool-call-corruption/exploration.md` (older engine, kept for reference)

## Engram note

Engram MCP tools are NOT available in this session; filesystem artifacts are authoritative. Retain mirror topic `sdd-init/kinver-hub/proxy` for post-session backfill if a tooled session returns.