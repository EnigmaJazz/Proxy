# AGENTS.md — Code Review Rules for the Kinver AI Proxy

This file is consumed by the `gga` pre-commit hook (`.gga`, `RULES_FILE="AGENTS.md"`).
It is the prompt context for the AI review provider. Keep rules short, concrete, and
project-specific.

## Stack

- Python 3.14, FastAPI 0.136.3, uvloop event loop
- pytest + pytest-asyncio (strict mode)
- httpx ASGITransport for in-process API tests
- dataclasses for internal state objects (RouteDecision, etc.)
- SQLite (`ai_queue.db`) for the job queue

## Module map

- `proxy.py` — FastAPI app + lifespan + AppState wiring
- `routes.py` — chat-completions endpoint, parameter build, SSE streaming
- `routing.py` — RouteDecision, ROUTE_MAP, resolve_route_for_lane_a, frontdesk classifier
- `constants.py` — single source of truth for endpoints, hardware limits, field sets
- `llm.py` — stream_llm, call_llm, chat-template translation
- `database.py` — SQLite job queue (enqueue/complete/fail/stream chunks)
- `cooling.py` — CoolingStateMachine (CPU/GPU IPC files for fans)
- `systemd.py` — model service discovery + hotswap
- `tools/web_search.py` — native web search pipeline (SearXNG → Trafilatura → FlashRank)
- `search_enrichment.py` — enrich thin frontend search_web results on the OUTBOUND copy
- `opencode_bridge.py` — route requests to a headless opencode serve backend (`model: "opencode"`, `/opencode`, queue-worker escalation)
- `tools.py` — legacy tool registry (shadowed by the `tools/` package; kept for reference)
- `profile_loader.py` — ModelProfileTable (R17/R18)
- `tools/sync_model_profiles.py` — filesystem-grounded profile scanner (R18)

## Hard rules

1. **Glass Pipe Rule (R1)**: do not alter client message content, tool definitions, or
   sampling parameters when the client picked the model. Proxy only fills gaps (e.g.
   `thinking_budget_tokens` from a profile). Intent-based overrides of
   `temperature`/`top_p`/`max_tokens` are FORBIDDEN on direct calls.

   **Documented carve-out — context governance** (`proxy/context_governance.py`): the
   proxy MAY truncate/offload oversized `role: "tool"` results and snip the oldest
   complete turns in the OUTBOUND model-copy only, to protect the model's runtime
   context window in tool-call-heavy conversations. It never touches user or assistant
   text, never splits tool_call/result pairs, never drops the latest user turn, never
   mutates the client's stored conversation or the DB audit copy, and is per-request
   opt-out via `X-Proxy-Context-Governance: off`. This is a mechanical budget
   optimization, not an intent alteration.

   **Documented carve-out — current date/time stamp** (`routes.py`
   `_inject_current_datetime`): frontends (nanobot, OpenWebUI) never send
   the date, so the models they drive run date-blind. The proxy MAY add
   the current DATE to the OUTBOUND system message, APPENDED after the
   system content (not prefixed): the stable system prefix must stay
   KV-cache-visible so the model's prompt cache hits across requests.
   The stamp is DATE-ONLY (2026-08-11) — a clock time changed the
   cache-visible prefix every request and invalidated every checkpoint
   (the server's "erased invalidated context checkpoint" logs), forcing
   full prefills even on session follow-ups; the date changes once per
   day. It never touches user or assistant text, never mutates the
   client's stored conversation or the DB audit copy, and is per-request
   opt-out via `X-Proxy-Date-Time: off`. This is a content-availability
   fix mirroring the opencode app's own date-only stamp for its
   sessions.

   **Documented carve-out — search-result enrichment** (`proxy/search_enrichment.py`):
   frontends own tool execution and often return thin `search_web` results (a JSON
   array of `{title, link, snippet}`) that the model cannot answer from. When a
   search-type tool result on the OUTBOUND copy is thin, the proxy MAY append its own
   rich search (SearXNG + FlashRank + article scrape via `tools/web_search.py`) to
   that result. It never replaces client content, never touches user or assistant
   text, never mutates the client's stored conversation or the DB audit copy, fires
   only for thin search-type results, is idempotent (an already-enriched result is
   never re-enriched), and is per-request opt-out via
   `X-Proxy-Search-Enrichment: off`. Failures degrade to the original result. This is
   a content-availability fix for frontends whose own search returns thin snippets;
   explicit user-approved extension of the R1 carve-out (2026-08-03, proxy-only
   changes).

2. **No payload injection in flight**: the proxy never injects synthetic assistant
   content deltas (triage/loading messages, audit overrides) into in-flight
   `tool_calls` JSON. Triage metadata must go in a separate SSE chunk
   (`_make_system_chunk`) BEFORE the first model chunk, or in a comment line (`:`).

3. **Async everywhere in the request path**: routes are `async def`, dependencies are
   `async`, all I/O is awaited. Sync code in the request path is a bug.

4. **Type hints required**: function signatures must have type hints. Use
   `Optional[X]` for nullable, `tuple[...]` / `dict[...]` / `list[...]` (not
   `Tuple`/`Dict`/`List` from typing unless Python 3.9 compat is needed).

5. **Dataclasses for structured state**: new structured state objects use
   `@dataclass`, not dicts. Update `RouteDecision` and similar in routing.py, not
   in routes.py.

6. **No global state outside `proxy.app.state`**: per-request state lives on the
   request, shared state lives on `proxy.app.state.*`. Module-level mutable
   globals are forbidden except cached constants in `constants.py`.

   **Documented carve-out — serve-config mtime cache** (`opencode_bridge.py`
   `_serve_config_mtime`): a scalar `Optional[float]` cache of the serve
   config's last-synced mtime, mutated only in `_sync_serve_config` /
   `_spawn_serve`. The spawn gate has no `app.state` handle (it runs from
   scripts and tests too), so threading app state through would couple the
   bridge's core to the FastAPI app. Accepted as a project exception
   (2026-08-09, F5 note in `opencode_bridge.py`); the cache is a scalar
   timestamp, never a container.

   **Documented carve-out — prompt priming registries** (`prompt_cache.py`
   `_PROMPTS` / `_LAST_PRIMED`): runtime-mutable module-level registries
   for the frontend system-prompt priming. The priming runs from the
   residency monitor loop (hardware.py) AND the request path (routes.py)
   with no app.state handle in the monitor, so threading app state through
   would couple the cache to the FastAPI app. Accepted as a project
   exception (2026-08-11, F6 note in `prompt_cache.py`); the registries
   are small, per-process, and die with the process.

7. **Tests**: new code paths MUST have a regression test. Use the harness in
   `tests/conftest.py` (stubs FlashRank + heavy deps, lifespan disabled).
   Async tests need `@pytest_asyncio.fixture` (NOT plain `@pytest.fixture`).

8. **No AI attribution in commits**: `Co-Authored-By:` trailers are forbidden.
   Use conventional commits: `type(scope): subject`.

9. **No `print()`**: use `get_logger(name)` from constants.py. **CLI carve-out:**
   CLI tools whose stdout IS the deliverable (e.g. `tools/sync_model_profiles.py`
   emitting the YAML template) may use `print()` for that stdout deliverable;
   all diagnostics must go through a logger to stderr.

10. **No bare `except:`**: catch specific exceptions. `except Exception` is OK
    only at process boundaries (lifespan, queue worker) and at the terminal
    SSE stream boundary where any error must become a client-visible error chunk.

## Scope conventions

- Bug fixes: `fix(scope): subject` — e.g. `fix(routing): add worker to _HEAVY_MODEL_KEYS for hotswap`
- Features: `feat(scope): subject` — e.g. `feat(r18): model profile sync toolchain`
- Housekeeping: `chore: subject` — e.g. `chore: remove committed binaries`

## Reference docs

- `openspec/changes/glass-pipe-followups/` — R17/R18 specs and design
- `docs/brainstorms/2026-07-05-proxy-glass-pipe-hardening-requirements.md` — R1–R10 violations
- `docs/brainstorms/2026-07-19-client-named-model-fix.md` — R19 fix background
