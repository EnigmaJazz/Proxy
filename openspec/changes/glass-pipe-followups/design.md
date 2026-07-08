# Design: Glass-Pipe Followups — Parameter Authority & HF Profile Sync

> Artifact store: openspec. Source of truth: proposal.md + specs/*. No tracked files
> are touched by this phase. Implementation: **2 chained PRs** (PR1 ~250 lines:
> R18 build-time scanner toolchain + GGUF reader + runtime loader wiring + R18 tests;
> PR2 ~230 lines: R17 parameter authority on both code paths + R17 tests + 4R review
> fan-out), each under the 400-line budget. Tracker: `feature/glass-pipe-followups`.
> Locked decisions (R17 triggers, R18 scanner shape, `params_replaced` top-level event)
> are NOT re-litigated here — this design covers HOW.
>
> **This is a DELTA pass**: the R17 parts (Flow A, all IPs in §6) and the resolver
> (§4 `ModelProfileTable`, §5 `ProfileEntry`) are preserved verbatim; only the R18
> build-time toolchain + `local_models.yaml` schema + generated YAML + R18
> risks/open-questions are rewritten for the scanner pivot.

## 1. Context & Goals

R17 hands the proxy authoritative ownership of sampling parameters when the proxy
itself picks the model. R18 sources those values from a **build-time filesystem
scanner**: the GGUF files on disk are ground truth (architecture `general.architecture`,
`*.context_length`, `general.file_type`, `general.name`); Hugging Face is consulted
**per declared `hf_model_id`** for sampling recommendations (creative direction)
only, and only AFTER the scanner verifies the file exists. The scanner writes a
committed `config/model_profiles.yaml` (GGUF facts + HF sampling + operator overrides)
the proxy loads once at startup — deterministic, offline-startable, zero runtime HF
calls. Target backend: **llama.cpp / GGUF**.

> **Delta note (this pass)**: R18 was originally an HF-only scraper. A user pivot
> redefines R18 as the filesystem scanner above. The R17 parts (Flow A, all IPs in
> §6) and the resolver (§4 `ModelProfileTable`, §5 `ProfileEntry`) are UNCHANGED —
> only the build-time toolchain + `local_models.yaml` schema + generated YAML are
> rewritten. **Pre-pivot R18 is already shipped** (`profile_loader.py`, the scraper
> `tools/sync_model_profiles.py`, `config/*.yaml`, `proxy.py` step 5.5, the CI gate)
> per commits `85cfc3a` / `24df7ee`; this pass MODIFIES the scanner + config in
> place. The runtime loader is stable — no source change to `profile_loader.py` or
> `proxy.py`.

| Concern | Code (verified) |
|---------|-----------------|
| Auto-routed trigger | `requested_model = body.get("model","auto").lower()` (`routes.py:161`); auto-resolved at `routes.py:435` via `resolve_route_for_lane_a`; flagged at `routes.py:514` (`requested_model != "auto"` → `model_override`) |
| Dream/soul fast-path trigger | `routes.py:265` (`is_dream and caller_type == "AGENTIC"`) — **separate code path**, builds its own payload at `routes.py:295-302` and returns its own `StreamingResponse` at `routes.py:305` |
| Client-wins parameter build (direct mode) | `routes.py:459-499` `parameters.setdefault(...)` per intent branch |
| Payload build + R11 forward loop | `routes.py:541-554`; the `OPENAI_FORWARD_FIELDS` loop (`routes.py:552`) overlaps the R11 set (`seed`, `top_logprobs`, `response_format`, `n`) |
| Established proxy-event channel | `_emit_proxy_event(kind, data)` (`routes.py:1118`); `proxy_preamble` (`routes.py:174`) yielded at `routes.py:670` BEFORE the triage `status` event (`routes.py:673`) and the first model chunk (`routes.py:679`) |
| Startup state assembly | `proxy.py` lifespan (`proxy.py:208-272`); `AppState` (`proxy.py:114-159`) is the cross-route shared state accessed as `request.app.state` |

**Goals**: profile-owns (not client-owns) the R11 set in triggered mode; `params_replaced`
SSE event makes the substitution observable; build-time **filesystem scanner** with a
hard CI drift gate (filesystem change OR HF card update for a known `hf_model_id`);
offline start.

**Non-goals**: re-tune intent defaults (R17 swaps their *source*, not their values);
runtime HF fetch; proxy-level hot-reload of profiles (`--watch` drafts files only);
**synthesize profiles for models not present on disk**; new public endpoints; DB migration.

## 2. Architecture

```
                BUILD TIME  (SCANNER, llama.cpp/GGUF target)          RUNTIME
 Operator ──▶ tools/sync_model_profiles.py                           proxy.py lifespan (step 5.5 — SHIPPED)
              ├─ read config/local_models.yaml                        ├─ load_model_profiles()
              │   (model_key → {path, hf_model_id, overrides?})        │   (config/model_profiles.yaml)
              ├─ for each entry: VERIFY file exists at path            └─ state.model_profiles = table
              │   missing/unreadable → ERROR (sync aborts; exit 3)          │ (unchanged runtime contract)
              ├─ read GGUF header via tools/gguf_reader.py                  ▼
              │   general.architecture, *.context_length,             routes.chat_completions
              │   general.file_type, general.name, …                   ├─ detect R17 trigger
              │   missing arch/ctx_length → ERROR (exit 3)            │   (auto OR dream fast-path)
              ├─ REQUIRE hf_model_id per entry                        ├─ resolve route → model_key + intent
              │   missing → ERROR (exit 2; config error)              ├─ IF triggered:
              ├─ fetch HF model card per hf_model_id                  │     profile = resolve(intent, model_key)
              │   (creative direction: temp/top_p/…)                   │     payload R11 = profile values
              ├─ unparseable HF card → SKIP+LOG (row still emitted      ├─ ELSE (direct): client-wins (unchanged)
              │   with GGUF facts + max_tokens derivation)             ├─ IF triggered: build params_replaced event
              ├─ combine: GGUF > HF > overrides (overrides win)         └─ StreamingResponse(preamble=…+params_replaced)
              ├─ max_tokens = context_length * 0.9
              └─ write config/model_profiles.yaml
                         │
                   git commit
                         │
                         ▼
             CI: sync --check (HARD GATE — SHIPPED, commit 24df7ee)
                 drift = FS change (removed/renamed/changed ctx_length)
                          OR HF card update for a known hf_model_id
                 drift → build fails
```

**Four proxy-injected SSE event types** (REQ-7 adds the fourth):

| Event type | `kind` | Envelope |
|------------|--------|----------|
| `kinver.proxy.status` | triage, loading, cache_restore, pause, resume, cloud | `{kind, ts, data}` |
| `kinver.proxy.tool_stripped` | tool_stripped | `{kind, …, data:{reason,domain}}` |
| `kinver.proxy.audit_halt` | audit_halt | `{kind, …, data:{reason}}` |
| `kinver.proxy.params_replaced` (NEW) | params_replaced | `{kind, ts, data:{model, replaced[], values{}}}` |

## 3. Sequence Diagrams

### Flow A — R17 auto-routed parameter authority

```
 Client                routes.chat_completions          profile_loader          target model
   │                          │                              │                       │
   │ POST model:"auto"        │                              │                       │
   │ temp=0.9,seed=42         │                              │                       │
   ├─────────────────────────▶│                              │                       │
   │                          │ requested_model=="auto" → trigger                         │
   │                          │ resolve_route_for_lane_a → route.model_key, route.intent   │
   │                          │ resolve(intent, model_key) ─▶│                       │
   │                          │ ◀────── ProfileEntry ────────│                       │
   │                          │ (unknown? → intent=code/chat fallback row; logged)       │
   │                          │ build payload: R11 fields = profile values                │
   │                          │   (client temp=0.9, seed=42 NOT consulted)                 │
   │                          │ build params_replaced event string                        │
   │                          │ proxy_preamble += params_replaced                         │
   │                          │                              │                       │
   │ ◀ SSE: event: kinver.proxy.tool_stripped (only if loop) │                       │
   │ ◀ SSE: event: kinver.proxy.params_replaced              │                       │
   │      data:{model, replaced:[temp,seed,…], values:{…}}   │                       │
   │ ◀ SSE: event: kinver.proxy.status kind:triage           │                       │
   │ ◀ SSE: data: <model chunk> ──────────────────────────────────────────────────────▶│
   │                          │                              │                       │
```

**Ordering** (both paths share the `_event_stream` preamble at `routes.py:670-676`):
`tool_stripped` (if any) → `params_replaced` → `triage` status → first model chunk.
Both `tool_stripped` and `params_replaced` are appended to `proxy_preamble` in
`chat_completions` *before* the `return StreamingResponse(...)`; the triage event is
emitted inside `_event_stream` after the preamble yield.

### Flow C — R18 build-time filesystem scan to runtime lookup

> Renamed from "Flow B". Replaces the pre-pivot HF-only scrape with the scanner
> pivot: GGUF files on disk are ground truth; HF is per-declared-`hf_model_id`
> only, after the file is verified present.

```
 Operator    tools/sync_model_profiles.py    config/*.yaml   GGUF file   HF           CI
   │              │                               │              │          │            │
   │ sync ───────▶│ read local_models.yaml        │              │          │            │
   │              │ for each entry (model_key, path, hf_model_id):          │            │
   │              │   os.path.exists(path)? ────────────────────▶ NO → ERROR (exit 3),  │
   │              │                                                  abort, no YAML       │
   │              │   read GGUF header (arch, *.context_length, …) ──▶│                 │
   │              │     missing arch/ctx_length → ERROR (exit 3)       │                 │
   │              │   hf_model_id present? no → ERROR (exit 2; config) │                 │
   │              │   fetch HF card for hf_model_id ─────────────────────────────▶│     │
   │              │   ◀── card JSON ────────────────────────────────────────────│       │
   │              │   unparseable? → SKIP+LOG; row keeps GGUF facts +        │           │
   │              │     max_tokens (temp/top_p omitted → resolver → legacy   │           │
   │              │     intent-default in routes.py)                         │           │
   │              │   max_tokens = context_length * 0.9                      │           │
   │              │   combine: GGUF (ground) > HF (direction) > overrides     │           │
   │              │ write model_profiles.yaml ─────▶│                                   │
   │              │ + write two intent=* fallback rows                              │
   │ git commit ◀──────────────────────────────────│                                   │
   │              │                                                                    │
   │              │ sync --check (regen in-mem, diff committed YAML)                  │
   │              │ drift = FS change (removed/renamed/changed ctx_length)            │
   │              │       OR HF card update for a known hf_model_id                   │
   │              │ exit 0 in-sync / 1 drift / 2 config error ─────────────────────────▶ FAIL build
   │ proxy.py lifespan (step 5.5 — already wired)                                          │
   │ load_model_profiles() → state.model_profiles = table + resolve() fn                   │
   │ offline start ✓ (no HF call at runtime)                                                │
```

## 4. Module Surface

### `tools/sync_model_profiles.py` (MODIFY — pre-pivot scraper → filesystem scanner)

> Already shipped pre-pivot (commit `85cfc3a` `feat(r18): …` + CI gate `24df7ee`).
> This pass REWRITES the body: the flat `model_key → hf_model_id` map is replaced
> by a path-anchored schema; HF-only fetch is replaced by GGUF-header read +
> per-file HF fetch. `_BASELINES` is RETIRED (GGUF is now the `context_length`
> source; the curated temp/top_p baselines migrate to explicit `overrides:`).

```
usage: sync_model_profiles.py [--check] [--watch]

modes:
  (default)     verify each declared file, read its GGUF header, fetch HF per
                hf_model_id, combine, write config/model_profiles.yaml
  --check       regenerate in-memory, diff vs committed YAML; exit 0 if
                identical, exit 1 on drift (FS change OR HF card update for a
                known hf_model_id), exit 2 on malformed local_models.yaml
  sync --watch  re-run sync on local_models.yaml change (drafts files; proxy
                hot-reload OUT of scope — proxy loads once at startup)

exit codes:
  0  success / in-sync
  1  drift detected (--check) → HARD CI BLOCK
  2  config error (malformed local_models.yaml, OR an entry missing REQUIRED
     `path`/`hf_model_id`) → CI BLOCK
  3  file-missing / unreadable / GGUF-critical-missing → ERROR, abort sync
  (unparseable HF card for ONE file: NOT an exit code — skipped + logged; the
   row is still emitted carrying GGUF facts + the max_tokens derivation)
```

### `tools/gguf_reader.py` (new, build-time-only dep)

```python
@dataclass(frozen=True)
class GGUFMetadata:
    architecture: str     # general.architecture
    context_length: int   # <arch>.context_length  (arch-keyed, read AFTER arch)
    file_type: int        # general.file_type (quantization)
    name: str             # general.name

def read_gguf_metadata(path: Path) -> GGUFMetadata:
    """Open `path`, parse the GGUF header, return technical facts.
    Raises GGUFReadError on: missing file, unreadable, truncated header, OR
    missing CRITICAL fields (general.architecture / *.context_length) → caller
    raises a clear ERROR and aborts sync. Uses the `gguf` PyPI package
    (build-time-only; the runtime proxy never imports it). Fallback: a minimal
    manual GGUF-header parser covers the four fields above if `gguf` is absent
    in a runner (see Open Questions)."""
```

### Scanner internals (`tools/sync_model_profiles.py`)

```python
def load_local_models(path: Path) -> dict[str, dict]:
    """Read local_models.yaml → {model_key: {path, hf_model_id, overrides?}}.
    Raises ValueError on any entry missing `path` or `hf_model_id` (both
    REQUIRED, REQ-2) → caller exits 2 (config error)."""

def build_profile_entry(model_key: str, meta: GGUFMetadata,
                        entry: dict, hf_params: dict | None) -> dict:
    """Combine GGUF meta (GROUND TRUTH) + HF sampling (CREATIVE DIRECTION) +
    operator `overrides:` (DEPLOYMENT TUNING). Returns a row even when
    hf_params is None (unparseable card): carries context_window +
    max_tokens; temp/top_p from overrides or omitted (resolver strips None
    → legacy intent-default fills the gap in routes.py)."""

def derive_max_tokens(context_length: int) -> int:
    """Return int(context_length * 0.9). 10% headroom for system + prompt."""
```

**Three error classes** (REQ-2 mandatory `hf_model_id`; REQ-7 fail-fast file/metadata):

| Class | Trigger | Exit | Notes |
|-------|---------|------|-------|
| Config error | malformed `local_models.yaml`, or any entry missing REQUIRED `path`/`hf_model_id` | 2 | Logged + abort; no YAML written (REQ-2) |
| File/metadata error | declared path missing/unreadable, or GGUF header missing `general.architecture`/`*.context_length` | 3 | Logged naming file + field; abort; no YAML written (REQ-7) |
| Unparseable HF card | HF `config.json` for ONE declared `hf_model_id` can't be parsed | (not an exit) | SKIP+LOG; row still emitted with GGUF facts + max_tokens; other files sync (REQ-6) |

Never silently drop a declared entry (REQ-5/REQ-6/REQ-7).

### YAML loader (runtime — new module `profile_loader.py`)

```python
def load_model_profiles(yaml_path: Path) -> ModelProfileTable | None:
    """Read config/model_profiles.yaml once at proxy startup.
    Returns None on missing/malformed (logged) → caller falls back to legacy
    intent-default table; auto-routed calls then skip params_replaced entirely.
    Hard-fail is avoided so a misconfigured commit doesn't brick the proxy."""

class ModelProfileTable:
    def resolve(self, intent: str, model_key: str) -> ProfileEntry:
        """Lookup order:
           1. exact row where row.model == model_key AND row.intent == bucket(intent)
           2. exact row where row.model == model_key AND row.intent == "*" (any intent)
           3. fallback row where row.model == "*" AND row.intent == bucket(intent)
           4. if none: return None → caller uses legacy intent default, no event emitted"""
```

`bucket(intent)` maps the proxy's `route.intent` vocabulary to the two YAML buckets:
`{CODE, ARCHITECT, TOOL, PROFESSIONAL}` → `"code"`; `{CHAT, CREATIVE, SCHOLAR}` → `"chat"`.

## 5. Data Structures

### `config/local_models.yaml` (MODIFY — schema pivot from shipped flat map)

```yaml
# Maps proxy model_key → operator-declared file (REQUIRED path) + REQUIRED
# hf_model_id. The scanner VERIFIES each path exists on disk and reads its
# GGUF header. BOTH `path` and `hf_model_id` are REQUIRED: an entry missing
# either is a config error → exit 2 (REQ-2). Path MUST exist and be readable →
# exit 3 on miss (REQ-7). Run `tools/sync_model_profiles.py sync` after editing.
#
# Schema shift: the shipped flat map (commit 85cfc3a) was `model_key: hf_model_id`.
# The pivot nests `path` + `hf_model_id` under each entry. `overrides:` here is
# the PER-ENTRY layer, baked into the row at sync time; the top-level
# `overrides:` in model_profiles.yaml is the PER-DEPLOYMENT layer applied at load.
models:
  reasoning:
    path: ~/kinver-hub/models/DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf
    hf_model_id: deepseek-ai/DeepSeek-R1-Distill-Qwen-32B
  coder:
    path: ~/kinver-hub/models/qwen25-coder-32b-q4_k_m.gguf
    hf_model_id: Qwen/Qwen2.5-Coder-32B-Instruct
  architect:
    path: ~/kinver-hub/models/qwen3-32b-instruct-q4_k_m.gguf
    hf_model_id: Qwen/Qwen3-32B-Instruct
    overrides:
      max_tokens: 8192          # cap a 32B model for VRAM headroom
  professional:
    path: ~/kinver-hub/models/qwen25-72b-instruct-q4_k_m.gguf
    hf_model_id: Qwen/Qwen2.5-72B-Instruct
  creative:
    path: ~/kinver-hub/models/llama-3.3-70b-instruct-q4_k_m.gguf
    hf_model_id: meta-llama/Llama-3.3-70B-Instruct
  scholar:
    path: ~/kinver-hub/models/qwen3-30b-a3b-instruct-q4_k_m.gguf
    hf_model_id: Qwen/Qwen3-30B-A3B-Instruct
  worker:
    path: ~/kinver-hub/models/qwen25-9b-instruct-q5_k_m.gguf
    hf_model_id: Qwen/Qwen2.5-9B-Instruct
  chatter:
    path: ~/kinver-hub/models/DeepSeek-R1-Distill-Qwen-3B-Q4_K_M.gguf
    hf_model_id: deepseek-ai/DeepSeek-R1-Distill-Qwen-3B
  frontdesk:
    path: ~/kinver-hub/models/qwen25-3b-instruct-q5_k_m.gguf
    hf_model_id: Qwen/Qwen2.5-3B-Instruct
  lifeboat:
    path: ~/kinver-hub/models/llama-3.2-3b-instruct-q4_k_m.gguf
    hf_model_id: meta-llama/Llama-3.2-3B-Instruct
```

> The `model_key` remains the top-level key so the shipped runtime resolver
> (`ModelProfileTable.resolve(intent, model_key)`) is unchanged — the scanner
> emits each row's `model:` field from this key. Earlier proposals keyed the YAML
> by filesystem path; that was rejected because it would force a fragile
> name→model_key derivation and break the shipped resolver contract.

### `config/model_profiles.yaml` (MODIFY — generated, committed, GGUF-anchored)

```yaml
# Generated by tools/sync_model_profiles.py sync. DO NOT hand-edit sampling
# values; per-deployment overrides live under `overrides:` (REQ-4/REQ-5).
# CI runs `sync --check` and HARD-BLOCKS on drift (filesystem change OR HF
# card update for a known hf_model_id).
generated_at: 2026-07-07T22:30:00Z
profiles:
  - model: reasoning            # proxy model_key
    intent: code
    architecture: qwen2          # GGUF general.architecture (NEW ground-truth fact)
    file_type: 15               # GGUF general.file_type (quantization; NEW)
    context_window: 32768       # GGUF *.context_length (GROUND TRUTH, not HF)
    max_tokens: 29491           # derived: 32768 * 0.9 (overridable)
    temperature: 0.6            # HF sampling (creative direction)
    top_p: 0.95
    thinking_budget_tokens: 4096
    seed: null                  # null = proxy does not pin
    top_logprobs: null
    response_format: null
    n: 1
  - model: coder
    intent: code
    architecture: qwen2
    file_type: 15
    context_window: 131072
    max_tokens: 117964
    temperature: 0.1
    top_p: 0.9
    thinking_budget_tokens: 4096
    seed: null
    top_logprobs: null
    response_format: null
    n: 1
  # ... one row per (model_key, intent_bucket) with a GGUF file on disk ...
  # A row whose HF card was unparseable (REQ-6) carries architecture,
  # file_type, context_window, max_tokens, thinking_budget_tokens but OMITS
  # temperature/top_p — the resolver strips None so legacy intent-defaults
  # in routes.py fill those gaps.
  #
  # Intent-aware unknown-model fallback (always present, REQ-9):
  - model: "*"
    intent: code
    temperature: 0.2
    top_p: 0.95
    max_tokens: 8192
    thinking_budget_tokens: 2048
  - model: "*"
    intent: chat
    temperature: 0.7
    top_p: 1.0
    max_tokens: 4096
    thinking_budget_tokens: 0
overrides:                     # per-deployment; wins over GGUF + HF (REQ-4)
  reasoning:
    max_tokens: 8192           # e.g. cap a 32B GPU model for VRAM headroom
  # coder:
  #   max_tokens: -1           # local llama.cpp convention: -1 = no hard cap
```

### Lookup contract — `ProfileEntry`

```python
@dataclass(frozen=True)
class ProfileEntry:
    model: str
    intent: str
    values: dict[str, Any]   # R11 keys: temperature, top_p, max_tokens,
                             # thinking_budget_tokens, seed, top_logprobs,
                             # response_format, n
    source: str             # "exact" | "intent_fallback" | "model_wildcard"
```

`resolve(intent, model_key)` returns a `ProfileEntry` or `None`; `None` triggers the
legacy intent-default path with NO `params_replaced` event (safe degrade).

## 6. Integration Points (routes.py)

### IP-1 — Trigger detection

In `chat_completions`, right after `requested_model` parse (`routes.py:161`) and
dream detection (`routes.py:187`), introduce a single flag computed once:

```python
# R17 — proxy owns model pick: parameter authority applies.
# Direct calls (client picked the model) keep R1/R7 client-wins.
auto_authority = (requested_model == "auto") or (is_dream and caller_type == "AGENTIC")
```

### IP-2 — Profile lookup + substitution (auto-routed path)

At `routes.py:459`, branch on `auto_authority`. The existing `setdefault` client-wins
block (`474-499`) is wrapped so it runs ONLY when `not auto_authority` (direct mode,
unchanged behavior). When `auto_authority`:

```python
profiles = state.model_profiles
entry = profiles.resolve(route.intent, route.model_key) if profiles else None
if auto_authority and entry is not None:
    parameters = {**entry.values}            # profile OWNS the full R11 set
elif not auto_authority:
    # existing 459-499 client-wins block (unchanged)
else:
    # auto_authority but no profile / None entry → legacy intent default, no event
    # existing 459-499 block runs
replaced_fields = list(entry.values.keys()) if (auto_authority and entry) else []
```

### IP-3 — R11 overlap with OPENAI_FORWARD_FIELDS

The forward loop at `routes.py:552-554` would otherwise clobber profile values for
`seed`, `top_logprobs`, `response_format`, `n` (all in both sets). In auto mode the
loop MUST skip the overlap or the profile values MUST be re-applied after it. Chosen
(highest signal, lowest risk): **re-apply profile values for the R11 fields AFTER the
forward loop**, so profile wins regardless of loop order:

```python
if auto_authority and entry is not None:
    for f in R11_AUTHORITY_FIELDS:           # tuple in constants.py
        payload[f] = entry.values[f]        # profile override is final
```

`R11_AUTHORITY_FIELDS` is added to `constants.py` near `OPENAI_FORWARD_FIELDS`:

```python
R11_AUTHORITY_FIELDS: tuple[str, ...] = (
    "temperature", "top_p", "max_tokens", "thinking_budget_tokens",
    "seed", "top_logprobs", "response_format", "n",
)
```

### IP-4 — `params_replaced` event build + emit

Built in `chat_completions` AFTER the payload (`routes.py:554`), appended to
`proxy_preamble` (which `_event_stream` already yields at `routes.py:670`). No new
parameter on `_event_stream` / `_event_stream_with_model_startup`:

```python
if auto_authority and entry is not None:
    proxy_preamble += _emit_proxy_event(
        "params_replaced",
        {
            "model": route.model_key,
            "replaced": replaced_fields,
            "values": {f: payload[f] for f in replaced_fields if f in payload},
        },
    )
```

`_emit_proxy_event` (`routes.py:1118`) already wraps this as
`event: kinver.proxy.params_replaced\ndata: {…}\n\n` with the `{kind, ts, data}` envelope
(REQ-8 satisfied by the existing helper — `kind="params_replaced"`, `ts=int(time.time())`).

### IP-4b — Dream/soul fast-path (`routes.py:265-317`)

Same pattern in the dream block: after the local payload build (`routes.py:295-302`),
run IP-2/IP-3/IP-4 against `route.model_key="architect"` (intent="ARCHITECT" → code
bucket). Replace the hardcoded `temperature:0.2/max_tokens:4096/thinking_budget_tokens:4096`
with profile values when an entry resolves; fall back to the current hardcoded values
when no profile loaded (safe degrade). Append `params_replaced` to the `proxy_preamble=""`
passed at `routes.py:313`.

### IP-5 — Startup YAML load

In `proxy.py` lifespan (`proxy.py:208-272`), between step 5 (ShadowAuditor) and step 6
(background tasks):

```python
from profile_loader import load_model_profiles
state.model_profiles = load_model_profiles(PROJECT_ROOT / "config" / "model_profiles.yaml")
# None on missing/malformed → logged; auto-routed calls degrade to legacy defaults.
```

`AppState` (`proxy.py:114`) gains `self.model_profiles: Optional["ModelProfileTable"] = None`.

### IP-6 — Unknown-model intent-aware fallback

Resolved entirely inside `ModelProfileTable.resolve` (see §4): exact →
`model==key AND intent==bucket` → `model==key AND intent=="*"` →
`model=="*" AND intent==bucket`. The fallback hit is logged in `resolve` (REQ-6
"the unknown-model substitution is logged").

## 7. File Changes

| File | Action | Description |
|------|--------|-------------|
| `tools/sync_model_profiles.py` | Modify (already shipped pre-pivot) | Rewrite scraper → **filesystem scanner**: verify each declared file, read GGUF header via `tools/gguf_reader.py`, fetch HF per `hf_model_id`, combine GGUF+HF+overrides, write YAML, retire `_BASELINES`; `sync`/`--check`/`--watch` |
| `tools/gguf_reader.py` | Create | GGUF header parser → `GGUFMetadata(architecture, context_length, file_type, name)`; raises `GGUFReadError` on missing file / missing critical fields. Build-time-only `gguf` dep (with manual-parser fallback) |
| `config/local_models.yaml` | Modify (schema pivot) | `model_key → {path: REQ, hf_model_id: REQ, overrides?}`; replaces shipped flat `model_key: hf_model_id` map (REQ-2) |
| `config/model_profiles.yaml` | Modify (regenerate) | GGUF-anchored rows: `architecture`, `file_type`, `context_window` from the GGUF header (`ground truth`) + HF sampling (`creative direction`) + `overrides:`; two `model=*` intent fallback rows (REQ-9) |
| `profile_loader.py` | **No source change** | Runtime contract stable — `load_model_profiles()` + `ModelProfileTable.resolve()` + `ProfileEntry` already shipped and unchanged |
| `proxy.py` | **No source change** | `AppState.model_profiles` + lifespan step 5.5 already wired (already shipped) |
| `routes.py` | Modify (R17) | IP-1 trigger flag; IP-2/3/4 auto-routed branch + profile override after forward loop + params_replaced event; IP-4b dream-path same |
| `constants.py` | Modify (R17) | Add `R11_AUTHORITY_FIELDS` near `OPENAI_FORWARD_FIELDS` |
| `tests/glass_pipe_test.py` | Modify | R17: `TestAutoRoutedAuthority`, `TestDreamPathAuthority`, `TestParamsReplacedEvent`, `TestProfileLoader`, `TestProfileFallback`. R18: `TestScannerGGUFRead`, `TestScannerFileMissing`, `TestScannerMissingHFModelId`, `TestScannerGGUFCriticalMissing`, `TestScannerHFUnparseableSkip`, `TestSyncCheckDrift` |
| `.github/workflows/*.yml` (or CI config) | Modify (extend shipped gate) | `sync --check` is already present (`24df7ee`); gate is EXTENDED to treat filesystem drift (removed/renamed/changed `context_length`) as drift in addition to HF card drift |

## 8. Testing Strategy

| Layer | What | Approach |
|-------|------|----------|
| Unit | `ModelProfileTable.resolve` ordering | Construct a table in-memory; assert exact→wildcard→fallback precedence; assert `None` on empty table |
| Unit | `tools/gguf_reader.read_gguf_metadata` | Use a tiny synthetic GGUF fixture (or a sample committed under `tests/fixtures/`): valid header → `GGUFMetadata`; truncation / missing `general.architecture` / missing `*.context_length` → `GGUFReadError`; missing file → `GGUFReadError` |
| Unit | scanner `load_local_models` schema (REQ-2) | Entry missing `path` OR missing `hf_model_id` → `ValueError` (→ exit 2); valid nesting → `{model_key: {path, hf_model_id, overrides?}}` |
| Unit | scanner `build_profile_entry` combine | overrides > HF > GGUF order; `hf_params=None` (unparseable, REQ-6) → row emitted with `architecture/file_type/context_window/max_tokens/thinking_budget_tokens`; `temperature`/`top_p` omitted (resolver strips `None`) |
| Unit | `derive_max_tokens` | Headroom math: `context_length*0.9`; confirm integer truncation |
| Unit | `load_model_profiles` degrade | Missing file → `None` + log; malformed YAML → `None` + log; valid → table |
| Integration | R17 auto-routed authority | Patch `llm.stream_llm` to record `payload`; POST `model:"auto"` with `temperature:0.9,seed:42`; assert forwarded values == profile, NOT client values; assert `event: kinver.proxy.params_replaced` line precedes first `data:` model chunk |
| Integration | R17 dream path | Patch `is_dream_process` → True; assert architect payload uses profile values; assert params_replaced emitted |
| Integration | R17 direct call unaffected | POST `model:"deepseek-r1"` with `temperature:0.9`; assert forwarded `temperature:0.9` (client-wins); assert NO `params_replaced` line |
| Integration | params_replaced envelope (REQ-8) | Parse the event JSON; assert `kind=="params_replaced"`, `ts` int, `data.replaced` list, `data.values` carries forwarded values |
| Integration | unknown-model fallback |_PATCH `state.model_profiles` to a table missing the resolved `model_key`; assert intent-fallback row applied + logged; assert params_replaced emitted with `model=` the unknown key |
| Integration | scanner end-to-end (R18) | temp dir + synthetic GGUF fixture + stub HF fetcher; run `sync`; assert written YAML carries `architecture/context_window` from GGUF, `temp/top_p` from HF stub, per-entry override wins |
| Integration | scanner error exits (REQ-2/REQ-7) | declared path missing → exit 3 + no YAML; missing `hf_model_id` → exit 2; GGUF missing `context_length` → exit 3; HF card unparseable → row present + WARNING logged + exit 0 |
| Integration | `sync --check` gate (REQ-4) | Stub HF + GGUF; exit 0 in-sync; exit 1 on FS drift (fixture file removed) AND exit 1 on HF card change; exit 2 on malformed `local_models.yaml` |
| E2E | offline start | Commit a `model_profiles.yaml`; start `app` with no network; assert profiles load + an auto-routed request serves |

## 9. Risks and Rollback

| Level | Risk | Mitigation |
|-------|------|-----------|
| CRITICAL | R17 must patch **two** code paths (auto `459-596` AND dream `265-317`). Forgetting the dream path leaves the hardcoded `0.2/4096/4096` payload authoritative → inconsistent authority. | Single `auto_authority` flag computed once (IP-1); both paths read it. `TestDreamPathAuthority` regression asserts profile values on the dream path. |
| CRITICAL | R11 ↔ `OPENAI_FORWARD_FIELDS` overlap: forward loop (`552`) would clobber profile `seed/top_logprobs/response_format/n`. | Profile re-applied AFTER the forward loop (IP-3). `TestAutoRoutedAuthority` asserts forwarded `seed` == profile, NOT client, for an auto-routed call sending `seed`. |
| WARNING | `max_tokens` semantic shift: current code intent default is `-1` (no hard cap, `routes.py:478`); profile-derived value is positive (`ctx*0.9`). Models that interpreted `-1` as "no cap" now get a hard cap. | Per-deployment `overrides:` can restore `-1` for any model (documented in YAML + CHANGELOG). Default `overrides.coder.max_tokens: -1` recommended in the committed YAML. |
| WARNING | Profile file missing/malformed at startup. | `load_model_profiles` returns `None`; routes.py falls back to legacy intent-default block; NO `params_replaced` emitted. Proxy still serves direct + auto-routed (with legacy defaults). CI drift gate prevents committed drift. |
| WARNING | Auto-routed clients that sent `temperature`/`seed` silently lose control (proposal-level). | `params_replaced` surfaces the substitution; CHANGELOG entry flags the authority shift. |
| INFO | `mem_save` MCP tool reported unreliable in prior sub-agents on this project. Non-obvious discovery (two-path trigger, R11/forward overlap, `-1` semantic) persisted **in this design artifact** (§3 IP-4b, §6 IP-3, §9) rather than relying solely on engram. Retry `mem_save` at sdd-tasks; on failure, keep the design as the durable record. |
| INFO | Build-time network dependency for `sync`. | Build-time only; runtime proxy never calls HF. `--check` uses fresh fetch (CI has network); local offline sync uses last-known cards or skips with logs. |

### R18 scanner risks (delta)

| Level | Risk | Mitigation |
|-------|------|-----------|
| CRITICAL | Declared `path` in `local_models.yaml` missing/unreadable → sync must ERROR (REQ-7), not silently skip. A pre-pivot scraper had no file check. | `read_gguf_metadata` raises `GGUFReadError` on missing file → `sync` exits 3, aborts, writes no YAML. `TestScannerFileMissing` asserts exit 3 + no YAML. |
| CRITICAL | Entry missing REQUIRED `hf_model_id` (REQ-2) — config error, must abort. Pre-pivot schema had `hf_model_id` as the value (always present by construction); the pivot could let an operator forget it. | `load_local_models` raises `ValueError` on any entry missing `hf_model_id` → exit 2. `TestScannerMissingHFModelId` asserts exit 2 + no YAML. |
| CRITICAL | GGUF header missing `general.architecture` or `*.context_length` → cannot derive `max_tokens` headroom (REQ-7). | `read_gguf_metadata` raises naming file + missing field → exit 3. `TestScannerGGUFCriticalMissing` asserts exit 3. |
| WARNING | **Schema migration from shipped flat map**: `config/local_models.yaml` is already committed as `model_key: hf_model_id` (commit `85cfc3a`). The pivot nests values; sdd-apply MUST regenerate the file or `sync` fails at `load_local_models` (expects a mapping-of-mappings). | First `sync` after pivot writes the new schema; CI gate forces the commit. Document the schema break in the PR1 changelog. |
| WARNING | `_BASELINES` retirement loses operator-curated temp/top_p fallbacks when HF card is unparseable. Pre-pivot those baselines filled the gap; post-pivot the row simply omits temp/top_p. | Missing HF → resolver strips `None` → routes.py legacy intent-default fills temp/top_p. Operators who want the old value move it to explicit `overrides:`. Documented in §5. |
| WARNING | GGUF `*.context_length` field key is architecture-specific (`qwen2.context_length`, `llama.context_length`, …). `gguf_reader` MUST read `general.architecture` FIRST, then fetch `<arch>.context_length`. | `read_gguf_metadata` reads arch first, then the arch-keyed context_length; raises if either is missing. Verified against `gguf` package convention at sdd-apply. |
| INFO | **R17 ordering dependency (DELTA FINDING)**: the R17 IPs reference post-hardening line numbers — IP-3 "forward loop at `routes.py:552`", IP-4 "`_emit_proxy_event` at `routes.py:1118`". Verified AGAINST CURRENT SOURCE: `OPENAI_FORWARD_FIELDS`, `_emit_proxy_event`, `R11_AUTHORITY_FIELDS`, `auto_authority` are all ABSENT from `routes.py`/`constants.py`; `routes.py:518-525` is still the old closed 8-field build. The hardening R11 forward-loop + SSE helper have NOT landed in source yet (design/spec archived, code pending). Meanwhile R18 runtime (`profile_loader.py`, `proxy.py` step 5.5) + the pre-pivot scanner + CI gate ARE already shipped. **sdd-tasks MUST sequence the R17 hardening (forward-loop + `_emit_proxy_event`) before R17 authority.** R18 scanner rewrite is independent and can proceed in PR1. Persisted here per hard-rule; engram save attempted (prior sub-agents reported `ctx_memory(write)` failures). |

**Rollback**: R17+R18 ship in 2 chained PRs. `git revert <merge-commits>` restores
the prior intent-default `setdefault` block, the hardcoded dream payload, and removes
`profile_loader.py` + `config/*.yaml` + `tools/sync_model_profiles.py` + `tools/gguf_reader.py`
(all additive new files → clean revert). No DB migration, no persisted state.
`params_replaced` is an unknown SSE event type that standard OpenAI clients ignore —
reverting drops the event with no protocol break.

## 10. Open Questions

- [ ] `seed: null` in profile — does the upstream llama.cpp server treat an absent
  `seed` as non-deterministic (desired) or as `0`? Verify against the target server's
  `--seed` default at sdd-apply; if it pins to 0, omit `seed` from the forwarded
  payload rather than sending `null`.
- [ ] `response_format: null` and `top_logprobs: null` — confirm the local server
  accepts these as omitted vs. erroring on `null`. If it errors, omit absent fields
  from the payload entirely (profile stores `null`, forwarding strips `None`).
- [ ] CI runner availability: does the project's CI have network access for
  `sync --check`'s fresh HF fetch? If not, `--check` must compare against committed
  "golden" HF card snapshots instead. Resolve at sdd-tasks.
- [ ] Should `overrides:` (per-deployment) be `.gitignore`d to allow local VRAM caps
  without committing them, or always committed? Default: committed (deterministic).
  Re-open at sdd-tasks if operators need local-only overrides.
- [ ] `gguf` PyPI package availability in the CI runner (build-time-only dep). If
  the runner can't install it, `tools/gguf_reader.py` falls back to a minimal
  manual GGUF-header parser (the format is documented; only `general.architecture`,
  `<arch>.context_length`, `general.file_type`, `general.name` are needed). Verify
  at sdd-tasks; pick the path with the least CI friction.
- [ ] Confirm the `gguf` package exposes the arch-keyed context length via
  `reader.get(f"{arch}.context_length")` (or equivalent) for every architecture the
  deployment ships (qwen2, llama, …). Verify at sdd-apply against the real files
  under `~/kinver-hub/models/`.
- [ ] `local_models.yaml` per-entry `overrides:` (baked at sync) vs
  `model_profiles.yaml` top-level `overrides:` (applied at load) — confirm
  precedence and whether both can coexist for the same `model_key`. Default:
  per-entry bakes the value into the row; per-deployment overrides apply on top
  at load. Resolve semantics at sdd-tasks.