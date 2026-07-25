# Exploration: Professional as Default

> Artifact store: openspec. Read-only investigation. No source files were modified.
> Source of truth: this artifact + the file:line citations below.

## Current State

The proxy is a 5-tier routing system with the 2B Front-Desk model as the
classifier and the 35B MoE Professional as one of several heavy GPU models
that get hot-swapped in and out of VRAM. Today, every request goes through
this sequence:

1. **Discriminate caller** (`routes.py:177`): IDE header / User-Agent → IDE vs AGENTIC
2. **Lane A vs Lane B** (`routes.py:178,455-469`): `sk-ide-pass` → Lane B (force professional, bypass frontdesk, intent=CODE)
3. **Dream fast-path** (`routes.py:271-354`): Nanobot dream phrases → skip frontdesk, force architect, priority 3
4. **Frontdesk classification** (`routes.py:397-425`): 2B JSON-GBNF call → `{intent, priority, complexity, project_name, is_factual, tools_required}`
5. **Tool keyword heuristic override** (`routes.py:435-444`): if CHAT + matches TOOL_KEYWORDS → force TOOL
6. **Mid-tool-flow lock** (`routes.py:362-396`): if last message is assistant+tool_calls OR role=tool → force TOOL, skip frontdesk
7. **Resolve route** (`routes.py:454-477`): Lane B → professional; Lane A → `resolve_route_for_lane_a` (CHAT/TOOL with GPU busy → Lifeboat; heavy → GPU)
8. **R19 client override** (`routes.py:484-499`): if client named a model and it disagrees with the route, override `route.model_key` to the requested model
9. **Build parameters** (`routes.py:519-537`): R17 `auto_authority` branch (profile-owns R11) vs R1/R7 client-wins else branch
10. **Build payload** (`routes.py:577-600`): forward client OpenAI fields, re-apply profile R11 if auto_authority
11. **Cold-start heavy model** (`routes.py:833-984`): if `model_key ∈ _HEAVY_MODEL_KEYS` and not active → `systemd.hot_swap`
12. **Stream** (`routes.py:671-823`): `stream_llm` chunks → SSE; emit triage system chunk first

The current `ROUTE_MAP` (`routing.py:213-221`) maps the 7 classifier intents
to 7 models. The proxy's value proposition is "model picking by intent" and
the current expense of intent picking is two heavy GPU hot-swaps a day on
average (CHAT and CODE both trigger a swap to a different model).

## Affected Areas

### Routing (`routing.py`, `routes.py`)

- `routing.py:213-221` — `ROUTE_MAP`: the central table this change re-targets. `CHAT→chatter`, `TOOL→worker`, `CODE→professional` are the three rows that change.
- `routing.py:224` — `HEAVY_MODELS`: the "is this a heavy GPU model?" set. Professional stays heavy; chatter/worker are currently in `_HEAVY_MODEL_KEYS` at `routes.py:831` (after R19 fix `c484269`).
- `routing.py:227` — `CPU_MODELS`: {reasoning, lifeboat, frontdesk}. Unaffected.
- `routing.py:284-318` — `discriminate_caller`: returns "IDE" for known IDE user-agents. Unaffected.
- `routing.py:325-332` — `is_lane_b`: still gates Lane B bypass. Unaffected.
- `routing.py:364-504` — `classify_with_frontdesk`: returns 7-key dict. **Cost is the 5-15% latency** mentioned in the prompt; runs `call_model` over `frontdesk_port` (default 0, fallback `LLAMA_ENDPOINTS["frontdesk"]`).
- `routing.py:511-520` — `resolve_model`: trivial helper, will need a new branch.
- `routing.py:527-677` — `resolve_route_for_lane_a`: 4 branches. The CHAT-low and TOOL branches are the most affected; the heavy-intent tail (`CODE/SCHOLAR/PROFESSIONAL/CREATIVE/ARCHITECT → GPU model, enqueue`) becomes the exception, not the rule.
- `routing.py:698-781` — `detect_tool_loops`: uses LOOP_LIMITS keyed on model domain. If we retire worker, the `worker: 2` row becomes unreachable.
- `routes.py:228-230` — `auto_authority` flag: `(requested_model == "auto") or (is_dream and caller_type == "AGENTIC")`. **This is the critical trigger for R17 parameter authority**. After the change, `requested_model = "professional"` with a client name should also trigger `auto_authority` (the proxy is honoring the model's "professional" identity → it owns the params).
- `routes.py:271-354` — Dream fast-path: currently hardcoded `temperature: 0.2, max_tokens: 4096, thinking_budget_tokens: 4096` fallback. Already uses R17 profile lookup at line 291.
- `routes.py:435-444` — TOOL_KEYWORDS override. With Professional as default, the keyword override still applies (intent=TOOL → still go to professional, but the keyword detection stays).
- `routes.py:484-499` — R19 client override: 4-condition gate. **If chatter/worker are retired from `ALL_MODEL_KEYS`, the override can no longer pick them; clients who send `model: "chatter"` would fall through (auto-routed equivalent).**
- `routes.py:515-516` — `state.active_heavy_model` write: only on `route.hardware_path == "gpu"`. After the change, almost every request writes "professional" here.
- `routes.py:519-537` — Parameter build: R17 auto_authority + R1/R7 client-wins + profile-resolved `thinking_budget_tokens` only.

### Models & Profiles

- `constants.py:93-117` — `LLAMA_ENDPOINTS` and derived `ALL_MODEL_KEYS` tuple (lines 115-117, excludes "cloud"). 10 routable model keys today.
- `constants.py:115-117` — `ALL_MODEL_KEYS = tuple(key for key in LLAMA_ENDPOINTS if key != "cloud")` — **this is the set the R19 override gates against**. Retiring chatter/worker from `LLAMA_ENDPOINTS` removes them from this set automatically.
- `constants.py:259-270` — `OPENAI_FORWARD_FIELDS` — does NOT include `tool_choice` (removed in R17 commit `9904917` because Qwen 3.5 Worker garbles when forwarded).
- `constants.py:275-284` — `R11_AUTHORITY_FIELDS` — the field set profile wins on when auto_authority.
- `profile_loader.py:54-82` — `ModelProfileTable.resolve`: 3-step lookup (exact → model-wildcard → intent-fallback). The (CHAT, professional) and (TOOL, professional) tuples need resolution.
- `profile_loader.py:21-22` — `_CODE_INTENTS` includes `{CODE, ARCHITECT, TOOL, PROFESSIONAL}`; `_CHAT_INTENTS` is `{CHAT, CREATIVE, SCHOLAR}`. So CHAT → "chat" bucket, TOOL → "code" bucket.
- `config/model_profiles.yaml:58-70` — `professional` row has `intent: code` (line 59). **No `professional` row with `intent: chat` exists today**. The resolver would fall back to `model: "*", intent: chat` (line 142-147: `temperature: 0.7, top_p: 1.0, max_tokens: 4096, thinking_budget_tokens: 0`).
- `config/local_models.yaml:23-24` — `professional: ~/kinver-hub/models/Professional.gguf` (22 GB Qwen3.6 35B MoE per `llama-professional.service`).
- `config/local_models.yaml:19-20` — `worker: ~/kinver-hub/models/Worker.gguf` (7.6 GB Qwen 3.5 9B).
- `config/local_models.yaml:29-30` — `chatter: ~/kinver-hub/models/Lifeboat.gguf` (4.9 GB Mistral3 — actually uses the Lifeboat GGUF, not a separate model).
- `config/local_models.yaml:33-34` — `lifeboat: ~/kinver-hub/models/Lifeboat.gguf`. Same file as chatter.

### Hotswap & Heavy Model Mechanics

- `routes.py:831` — `_HEAVY_MODEL_KEYS = {"professional", "coder", "creative", "scholar", "architect", "chatter", "worker"}`. **All 7 currently-orchestrated models are flagged heavy** (after the R19 fix `c484269` added worker and `17a0d9d` added chatter).
- `routes.py:867-985` — `_event_stream_with_model_startup`: the wrapper that performs `systemd.hot_swap` if the target heavy model is not currently active, with a "🔃 Loading..." feedback chunk. **Each swap costs 30-120s** (the documented cold-start time).
- `systemd.py:78` — `_active_heavy_model: Optional[str]`. Single-track — only one heavy model at a time.
- `systemd.py:261-303` — `hot_swap(from_domain, to_domain)`: stop from → 2s VRAM release → start to → wait_for_ready. `systemd.py:53` `VRAM_RELEASE_DELAY = 2.0`.
- `systemd.py:381-393` — `is_gpu_occupied()`: returns True iff `_active_heavy_model` is in the 5 heavy models. After the change, "professional" is almost always the answer, so `is_gpu_occupied()` is almost always True.
- `cooling.py:260-261` — `hardware_path_for_model`: `professional ∈ hybrid_models`. So Professional uses BOTH CPU and GPU IPC files.
- `cooling.py:238-267` — `hardware_path_for_model`: returns "cpu"/"gpu"/"hybrid"/"none". Professional is "hybrid".
- `proxy.py:158,425,453` — `state.active_heavy_model` is set/cleared in two places: chat-completions (515-516) and queue worker (425, 453).
- `proxy.py:413-424` — Queue worker hot-swap path: `if current_heavy != target_model and target_model in (heavy_models) → hot_swap`.

### R17 Auto-Authority and R19 Client Override

- `routes.py:228-230` — `auto_authority = (requested_model == "auto") or (is_dream and caller_type == "AGENTIC")`. **This is the only place the proxy decides "I own the params"**.
- `routes.py:230` — Dream branch is `is_dream AND caller_type == "AGENTIC"`. IDE dream → not auto_authority (IDE picks the model, R1 client-wins).
- `routes.py:484-499` — R19 client override is **silent** on `auto_authority`. It only changes `route.model_key/port/hardware_path`; the parameter build branch (auto_authority vs client-wins) is decided by the flag at line 230. **A client who sends `model: "professional"` triggers R19 override but does NOT trigger auto_authority** — the else branch (client-wins) runs. This is the bug-or-feature to resolve in the proposal.
- `routes.py:525-537` — R17 vs R1/R7 fork. After this change, "client names professional" should be treated like "client names any other model" — R1/R7 client-wins — unless the proposal decides "professional is the default, naming it = declaring the default" (which would flip it to R17).

### Tool Calling on Professional

- `routes.py:163, 587-588` — Tools are passed through: `if tools: payload["tools"] = tools` (line 587-588). No tool_choice forwarding (deliberate, commit `9904917`).
- `~/kinver-hub/prompts/worker.txt:23-34` — The Worker system prompt is **explicitly trained on the OpenAI tool_calls JSON format** with the exact `Action: {"tool_calls": [...]}` pattern. The Lifeboat prompt (`lifeboat.txt`) is identical.
- `~/kinver-hub/prompts/worker.txt` and `lifeboat.txt` are the only prompts that teach the tool-call pattern. **Professional, Architect, Creative, Scholar have no role prompts**; they fall through to the model's GGUF-embedded template + `--jinja` (where present).
- `routes.py:601-641` — TOOL routing in `resolve_route_for_lane_a`: the `has_tool_history` branch keeps Worker on GPU even when busy. **This entire "lock to Worker" code is about preserving tool-calling JSON quality**. If we route TOOL to Professional, this branch is no longer needed (Professional is always-on, not hot-swapped).
- `routes.py:362-396` — Mid-tool-flow lock: if last message is assistant+tool_calls OR role=tool → force intent=TOOL, skip frontdesk. **If TOOL goes to Professional, the lock is harmless** (just forces the same destination).
- `routing.py:618-641` — Lifeboat template rejection: Lifeboat's chat template rejects messages with `tool_calls`/`tool_call_id`. This is why mid-tool-flow can't fall back to Lifeboat. **Professional has no such restriction** (no role prompt injected).
- `llama-professional.service` and `llama-worker.service` — Neither has `--tool-call-parser`. Per the open proposal `tool-call-template`, this is the **root cause of the existing tool-call garbling on Worker**. Adding `--jinja` + `--tool-call-parser` to Professional is the live test the proposal needs.
- `tools.py:62-127` — `execute_tool` is the proxy-side tool registry router. Routes by `tool_call_dict["name"]`. Currently only `web_search/search/search_web` are supported. If Professional emits structured `tool_calls`, the proxy can execute them; if it doesn't, the proxy forwards the (possibly garbled) text per Glass Pipe.

### Test Infrastructure

- `tests/conftest.py:39-161` — 4 `_NoOp*` classes: Database, Systemd, Cooling, Auditor. FlashRank stubbed. Lifespan disabled.
- `tests/conftest.py:172-187` — `app_client` fixture. **Documented as broken for async** (uses `@pytest.fixture` not `@pytest_asyncio.fixture`, fails in strict mode). R19 tests declare their own `r19_client` fixture in `test_client_named_model.py:34-55`.
- `tests/test_client_named_model.py:62-81` — `_make_profile_table` builds a 2-row table (`chatter/chat`, `coder/code`). **The new R20 tests will need a `professional/chat` row** (currently absent from the loaded YAML too).
- `tests/test_client_named_model.py:112-134` — `_StreamCapture` patches `stream_llm` to record endpoint + payload + port + headers. The pattern to copy.
- `tests/test_client_named_model.py:149-251` — 3 tests pinning the R19 contract. Use `patch("routes.classify_with_frontdesk", ...)` and `patch("routes.resolve_route_for_lane_a", ...)` to isolate the route from the frontdesk.
- `tests/glass_pipe_test.py` — 41 R18 tests. Pattern: lifespan-free, `_NoOp*` deps, in-process `httpx.ASGITransport`.

### Active State Mechanics

- `proxy.py:158` — `state.active_heavy_model: Optional[str] = None`.
- `routes.py:515-516` — `if route.hardware_path == "gpu": state.active_heavy_model = route.model_key`. **This is the only write that doesn't condition on `_HEAVY_MODEL_KEYS` membership** — any GPU model updates it.
- `systemd.py:301` — `systemd.hot_swap` also sets `_active_heavy_model = to_domain`. **Two write sites can race**: the chat-completions path and the queue worker.
- `proxy.py:451-453` — Queue worker's `finally` block: `if not pending: state.active_heavy_model = None`. This **aggressively unloads heavy models when the queue is empty**, which is great for "single heavy at a time" but means Professional will be unloaded after the queue empties. **Today this is fine because CHAT (the dominant case) doesn't go to Professional**. After the change, CHAT goes to Professional, so this unload behavior is now a problem: it would unload Professional between every request. **Critical risk to surface in proposal**.

### OpenSpec / R17 Contract

- `openspec/changes/glass-pipe-followups/proposal.md` — the R17/R18 specs already shipped (R18 in commit `b4d32a4`, R17 in `cbd90a5`).
- `openspec/specs/glass-pipe-passthrough/spec.md:90-112` — REQ-6 defines the R17 auto-routed exception. Auto-routed is `model: "auto"` only; R19 was carved out as a separate "client-wins" path.
- `openspec/changes/glass-pipe-followups/design.md:39-44` — Locked R17 trigger: `auto_authority = (requested_model == "auto") or (is_dream and caller_type == "AGENTIC")`. **Any change to this trigger is a NEW capability, not a MODIFIED one** — it changes which client requests the proxy owns.

## Approaches

### A. Big-Bang: Re-architect ROUTE_MAP (CHAT/TOOL → professional) + Retire Chatter/Worker

`ROUTE_MAP` becomes `{CHAT: professional, TOOL: professional, CODE: professional, SCHOLAR: scholar, PROFESSIONAL: professional, CREATIVE: creative, ARCHITECT: architect}`. Chatter/Worker service units are disabled. ALL_MODEL_KEYS shrinks. Specialist routing preserved via `client_named_model` (R19) and the dream path.

- Pros: One change in `routing.py` collapses 3 hotswaps/day to 0. Profile resolver gets exact (CHAT, professional) and (TOOL, professional) rows. R19 still routes to specialists when client names them.
- Cons: ALL_MODEL_KEYS shrinking is a breaking change for clients that send `model: "chatter"`. Tool-calling JSON quality is at risk (Worker prompt is the only one teaching the format). Profile rows for (CHAT, professional) and (TOOL, professional) must exist (currently only the (CODE, professional) row exists). Queue worker's `unload_all_heavy` runs after every queue-empty cycle and would unload Professional — needs gate.
- Effort: Medium

### B. Soft Pivot: Default Professional for `auto`, Keep Chatter/Worker for `model: "chatter" | "worker"`

ROUTE_MAP unchanged. The R19 client override becomes the entry point for specialists. Auto-routed mode now defaults to Professional; explicit `model: "chatter"` still hits Worker. `auto_authority` extended: `(requested_model == "auto" OR requested_model == "professional")`.

- Pros: No breaking change for clients. R19 already does the heavy lifting. Additive.
- Cons: Clients that send `model: "auto"` no longer see a lightweight path; heavy model loaded at all times. Profile (CHAT, professional) / (TOOL, professional) rows still needed. Tool-calling JSON quality risk identical to A. Queue worker unload still problematic.
- Effort: Medium-Low

### C. Heuristic-Only: Skip Frontdesk for CHAT/TOOL, Route to Professional

Bypass `classify_with_frontdesk` for the obvious cases (no tools, no IDE, short prompt). Frontdesk stays for ambiguous cases. `auto_authority` extends to the new short-circuited path.

- Pros: Saves 5-15% latency (the frontdesk 2B call). Removes a real bottleneck.
- Cons: Adds complexity. The frontdesk's tool-need detection is the safety net for ambiguous tool requests (e.g., "what's the file at /etc/hosts say?"). Removing it without an equivalent heuristic risks mis-routing.
- Effort: Medium-High

### D. Profile-First Pivot (Hybrid of A + C)

Combine A's route map pivot with C's frontdesk skip. The proxy's main job becomes parameter tuning by intent (R17 does this already), and the frontdesk becomes a classifier for SPECIFIC intent choices (Coder, Scholar, etc.) — not for the CHAT-vs-TOOL-vs-PROFESSIONAL decision.

- Pros: Maximally aligned with the user's stated re-architecture. Removes the frontdesk as a single point of failure for default traffic.
- Cons: Requires (1) profile (CHAT, professional) and (TOOL, professional) rows, (2) live test that Professional handles tool-calling JSON acceptably, (3) retire chatter/worker from ALL_MODEL_KEYS (or keep for opt-in), (4) gate queue worker unload.
- Effort: High

## Recommendation

**Approach D** is the right path — it matches the user's stated re-architecture ("Professional becomes the default for all CHAT and TOOL traffic … the proxy's main job becomes parameter tuning by intent"). Approach A is the minimum viable version of D; approach B is a stepping-stone if full retirement is too aggressive.

**Open questions the proposal MUST answer** before any code:
1. Retire `chatter` and `worker` from `ALL_MODEL_KEYS`, or keep as opt-in for clients that want them?
2. Does Professional (Qwen3.6 35B MoE) emit structured `tool_calls` deltas natively, or does it need `--jinja` + `--tool-call-parser` like the open `tool-call-template` proposal flagged? (Live test required.)
3. Should the (CHAT, professional) and (TOOL, professional) profiles share a `code` bucket or get a new `default` bucket? Today CHAT → "chat" bucket → falls back to `model: "*", intent: chat` (0.7 temp, 1.0 top_p, 4096 max_tokens, 0 thinking). That might be too low-quality for "Professional" framing. **Probably add explicit rows.**
4. Should the queue worker's `unload_all_heavy` (`proxy.py:451-453`) be skipped when the active model is `professional` (i.e., keep Professional loaded, only unload when switching away)?
5. Should `auto_authority` extend to `requested_model == "professional"` (i.e., client explicitly says "use the default")? Or stay strictly "auto" + "dream"?
6. Does the frontdesk still have a job after the pivot? It still classifies SCHOLAR/CREATIVE/ARCHITECT/CODE-via-coder requests, plus project name and priority. The 5-15% latency is on EVERY request — is that acceptable? (See Approach C.)
7. The `active_heavy_model` field, the `_HEAVY_MODEL_KEYS` set, the queue worker escalation matrix (`proxy.py:380-407`), the `_event_stream_with_model_startup` wrapper — all are designed for a "single heavy at a time" world. Professional-as-default is "professional always loaded". Does that simplification simplify the hot-swap path or expose hidden assumptions?

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Tool-calling JSON quality on Professional degrades vs Worker. Worker has an explicit role prompt; Professional has none. | Med | Live test (Steps 4.5/5.3 below) before retiring Worker. If it fails, keep Worker as opt-in for `model: "worker"`. |
| Profile rows (CHAT, professional) and (TOOL, professional) are missing — resolver falls back to chat-bucket defaults (0.7 temp, 4096 max_tokens) which may be too low. | High | Add explicit rows in `config/model_profiles.yaml` for (professional/chat) and (professional/code) before merging. |
| Queue worker `unload_all_heavy` (`proxy.py:451-453`) unloads Professional after every queue-empty cycle, triggering a 30-120s reload on the next request. | High | Add a gate: skip unload when `_active_heavy_model == "professional"`. Make unload happen only on explicit transitions (dream path, manual /cloud, OS transition pause). |
| Client-named `model: "chatter"` no longer routes anywhere (after retirement from ALL_MODEL_KEYS) — silently falls through to auto-routed default. | Med | Keep chatter/worker in ALL_MODEL_KEYS but remove from ROUTE_MAP. R19 still routes the request to that model. Document. |
| Frontdesk cost (5-15% latency) is now on every default request. | Med | After profile is solid, consider Approach C: short-circuit frontdesk for obvious chat/tool cases. |
| Breaking change for clients that send `model: "auto"` expecting chatter. | High | Major version bump or feature flag. |
| The `unload_all_heavy` in queue worker (`proxy.py:451`) and the `state.active_heavy_model` write in `routes.py:515-516` race. | Low | Both writes are protected by Python's GIL; effect is last-writer-wins. Audit both. |
| Profile (CHAT, professional) conflicts with `intent=chat, model=professional` if the resolver picks it for (TOOL, professional) too. | Med | Add TWO rows: (professional/chat) and (professional/code). Test both. |

## Ready for Proposal

Yes — the map is complete. The proposal phase should:
1. Decide between A (big-bang) and D (hybrid of A + C).
2. Resolve the 7 open questions above.
3. Draft a single change folder `openspec/changes/professional-as-default/` with `proposal.md`, `specs/`, `design.md`, `tasks.md`, and a follow-up `verify-report.md` after a live test confirming Professional handles tool-calling JSON acceptably.

The proposal should be chained: it touches `routing.py`, `routes.py`, `config/model_profiles.yaml`, `config/local_models.yaml`, `constants.py` (possibly), and adds tests. Estimated 250-400 lines; fits in a single PR if scoping is tight, but the live test on Professional + tool-calling is a hard prerequisite for the "retire Worker" decision and may need to land as a separate PR.
