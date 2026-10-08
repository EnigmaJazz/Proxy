# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` is the code-review contract (consumed by the `gga` pre-commit hook) and holds the hard rules (R1 Glass Pipe, no in-flight payload injection, async-only request path, no module-level mutable globals, no `print()`, no bare `except:`). Read it before changing request-path code. This file only adds what it doesn't cover.

## What this is

An OpenAI-compatible proxy (`POST /v1/chat/completions`, `GET /v1/models`, `GET /health`) in front of locally hosted llama.cpp-style model services. It routes requests to models, hot-swaps heavy models via systemd, queues background work in SQLite, manages cooling, and can hand a request to a headless `opencode serve` backend. Python 3.14, FastAPI, uvloop; it listens on `0.0.0.0:13000`.

## Commands

All Python runs from the repo-local venv. The repo is flat (no package directory, no `pyproject.toml`); modules import each other by bare name from the repo root.

```bash
.venv/bin/python proxy.py                                # run the server (uvicorn + uvloop, port 13000)
.venv/bin/python -m pytest tests/ -v                     # full suite
.venv/bin/python -m pytest tests/test_routing.py -v      # one file
.venv/bin/python -m pytest tests/test_routing.py -k name # one test
.venv/bin/python tools/sync_model_profiles.py --check        # profile drift check (needs the local GGUF models; ~634 tests take ~70s)
```

- CI (`.github/workflows/ci.yml`) runs only the profile drift check, not pytest. Run the tests locally. CI invokes it as `sync --check`, but the script has no `sync` subcommand (argparse rejects it), so that CI step looks broken.
- Async tests must use `@pytest_asyncio.fixture` (strict mode), not plain `@pytest.fixture`.
- `tests/conftest.py` stubs `flashrank`, swaps `Database` and the other heavy dependencies for no-ops *before* `import proxy`, and disables the lifespan, then drives the real app through `httpx.ASGITransport`. Use its fixtures instead of building your own app. An autouse fixture shrinks the bridge's `_IDLE_GRACE_S` (45s in production) so scripted streams finish quickly.
- Machine-specific paths live in `local_config.py` (git-ignored). Copy `local_config.example.py` to create it. Code must fall back to generic defaults when it is absent. `ai-proxy.service` is a template with `__PLACEHOLDER__` values.

## Architecture

Request flow: `proxy.py` (FastAPI app, `AppState`, lifespan, background workers) → `routes.py` (`chat_completions` plus slash-style embedded commands like `/pause`, `/resume`, `/cloud`, `/opencode`) → `routing.py` (`resolve_route_for_lane_a`, the frontdesk classifier, `ROUTE_MAP`, `RouteDecision`) → `llm.py` (`stream_llm` / `call_llm`, chat-template translation) → the model service.

Pieces that span several files:

- **Lifespan and shared state.** `proxy.py` builds `AppState` and wires `Database`, `SystemdController`, `CoolingStateMachine`, the hardware monitor and the model-profile table. It also starts `queue_worker`, `zram_keepalive_worker` and `_cleanup_idle_heavy`. Per-request state belongs on the request and shared state on `app.state.*`. The module-level globals that exist are the documented carve-outs in `AGENTS.md`.
- **Model residency.** `systemd.py` discovers model services and hot-swaps them. Heavy models are listed in `_HEAVY_MODEL_KEYS` in `routing.py`, so a new heavy worker must be added there or hotswap won't trigger. `hardware.py` runs the residency and thermal monitors, `cooling.py` writes CPU/GPU IPC files for the fans, and `prompt_cache.py` primes frontend system prompts.
- **Model profiles (R17/R18).** `profile_loader.py` loads `ModelProfileTable`. `tools/sync_model_profiles.py` scans the filesystem to regenerate profiles. The CI drift gate fails if committed profiles diverge from what the scan produces.
- **Outbound-copy mutations.** `context_governance.py`, the date stamp in `routes.py` (`_inject_current_datetime`), and `search_enrichment.py` may change only the outbound model copy. Never change the client's stored conversation or the DB audit copy. Each has an `X-Proxy-*: off` per-request opt-out. These are the only sanctioned exceptions to the Glass Pipe rule. New exceptions need an explicit carve-out in `AGENTS.md`.
- **opencode bridge.** `opencode_bridge.py` is the core: it handles `model: "opencode"`, `/opencode`, and queue-worker escalation. `opencode_backends.py` is the transport abstraction. `OPENCODE_BACKEND` selects `ServeBackend` (default, stateless) or `OpenChamberBackend` (keeps a session→directory map). `opencode_chamber.py` is the OpenChamber CLI side. The serve config is generated from `opencode-serve-config.opencode.jsonc`.
- **Native web search.** `tools/web_search.py` runs SearXNG → Trafilatura → FlashRank. The `tools/` package shadows the legacy `tools.py`, which is kept for reference only.
- **Constants.** Endpoints, hardware limits and field sets live in `constants.py`. Add new ones there, not inline, and use `get_logger(name)` from it for logging.
- **Persistence.** `ai_queue.db` (SQLite, job queue and stream chunks) plus `background_queue.json` and `recovery_state.json` for restart recovery. All are git-ignored runtime state.

## Workflow conventions

- Conventional commits: `type(scope): subject`. No `Co-Authored-By` or AI attribution trailers (AGENTS.md rule 8).
- After each routed task, append a row to `ROUTER-LOG.md` and add a `ROUTED: <class>@<gate-outcome>` trailer to the commit (recipe in `AGENTS.md` → "Task routing").
- New code paths need a regression test (rule 7).
- Design history lives in `openspec/` (specs, changes) and `docs/brainstorms/` (R1–R10 violations, R19 fix background).
- `AGENTS.md`'s module map writes some paths as `proxy/…`. That is stale. The files sit at the repo root.
