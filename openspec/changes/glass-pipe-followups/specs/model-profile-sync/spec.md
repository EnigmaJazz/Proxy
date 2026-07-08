# Model Profile Sync Specification

## Purpose

Build-time **filesystem scanner** that derives sampling-parameter profiles from GGUF files actually present on disk (the source of truth for `architecture`, `context_window`, quantization), enriched with per-file Hugging Face sampling recommendations for operator-declared model IDs, and tuned by operator overrides. Output is a committed `config/model_profiles.yaml` the proxy loads once at startup — making R17 parameter authority deterministic, grounded in real installed models, and offline-startable. No speculative model-ID list; no runtime HF calls. Covers R18. Target backend: llama.cpp / GGUF.

## Requirements

### REQ-1: GGUF files on disk are the source of truth (R18)

The scanner SHALL treat the actual GGUF files referenced by `config/local_models.yaml` as the source of truth for technical profile facts. For each declared entry the scanner SHALL verify the file exists and is readable, then read GGUF header metadata including at minimum `general.architecture`, the architecture-specific `*.context_length`, `general.file_type`, and `general.name`. The scanner MUST NOT synthesize profiles for models not present on disk.

#### Scenario-1: Scanner reads GGUF metadata

- GIVEN `config/local_models.yaml` maps `/models/deepseek-r1.gguf` → `deepseek-ai/DeepSeek-R1`
- WHEN `tools/sync_model_profiles.py sync` runs
- THEN the scanner reads `general.architecture`, `*.context_length`, `general.file_type`, and `general.name` from that file's GGUF header
- AND the written profile for that path carries those technical facts

#### Scenario-2: No speculative profiles for absent files

- GIVEN a GGUF file is not present on disk and not declared in `local_models.yaml`
- WHEN `sync` runs
- THEN no profile entry is synthesized for that model

### REQ-2: Operator-declared path → optional sampling_source (R18)

`config/local_models.yaml` SHALL map each entry from a filesystem path to an **OPTIONAL** `sampling_source`. Every declared entry MUST have a real file at the declared path; `sampling_source` is optional. The `sampling_source` MAY be any HTTP(S) URL (HF model card, vendor docs like `https://unsloth.ai/docs/models/...`, blog post) or a local file path. The scanner fetches it and uses a **two-stage extraction** because arbitrary web pages are too format-variable for a single parser:

1. **Regex fast path** — a tolerant regex on the fetched content; works for the common cases (HF model cards, vendor docs with `temperature: 0.6`-style content).
2. **LLM extractor fallback** — if the regex doesn't extract `temperature` or `top_p`, the scanner calls the proxy's own `--extractor` model (default: `coder`) via the OpenAI-compatible `/v1/chat/completions` endpoint with `response_format: json_object`. The model is asked to return `{"temperature": ..., "top_p": ...}`. Best-effort: any failure (proxy down, model missing, unparseable response) returns `None` and the row falls through to intent defaults.

The GGUF header (`general.name`, `general.architecture`, `*.context_length`) is the source of truth for the model identity — the operator does NOT need to repeat this in the YAML. Operator `overrides:` per entry SHALL win over both the sampling source and intent defaults.

#### Scenario-1: Entry with sampling_source URL — regex parses

- GIVEN `local_models.yaml` maps a path with `sampling_source: https://huggingface.co/.../README.md` and `overrides: {temperature: 0.3}`
- WHEN `sync` runs
- THEN the scanner fetches the URL, the regex extracts `temperature` / `top_p`, and `overrides.temperature: 0.3` wins

#### Scenario-2: Entry without sampling_source uses intent defaults

- GIVEN `local_models.yaml` maps a path with no `sampling_source`
- WHEN `sync` runs
- THEN the scanner emits a profile with GGUF facts and intent defaults for `temperature` / `top_p` (no fetch attempted, no warning)
- AND `sync` exits zero

#### Scenario-3: Regex fails → LLM extractor runs

- GIVEN `local_models.yaml` maps a path with a `sampling_source` whose content has no regex-matchable `temperature` or `top_p`
- WHEN `sync` runs
- THEN the scanner falls back to the LLM extractor (calls the proxy's `--extractor` model with JSON output)
- AND the extracted params (or intent defaults if the LLM call fails) populate the row

### REQ-3: Scanner CLI exposes sync, check, and watch (R18)

`tools/sync_model_profiles.py` SHALL expose `--check` (drift detection; non-zero exit on drift, zero when in sync — drift is a filesystem change, a removed/renamed file, OR an HF card update for a known `hf_model_id`) and `--watch` (regenerate the YAML on source changes; proxy hot-reload is OUT of scope — `--watch` only drafts files). The default mode (no flags) writes the YAML.

#### Scenario-1: --check exits zero when in sync

- GIVEN `config/model_profiles.yaml` matches current GGUF files and HF cards
- WHEN `tools/sync_model_profiles.py --check` runs
- THEN the exit code is 0

#### Scenario-2: --check exits non-zero on filesystem drift

- GIVEN a declared GGUF file was removed from disk since the last committed YAML
- WHEN `tools/sync_model_profiles.py --check` runs
- THEN the exit code is non-zero

#### Scenario-3: --watch drafts only

- GIVEN `tools/sync_model_profiles.py --watch` is running
- WHEN a declared GGUF file is replaced with a different context_length
- THEN the YAML is regenerated on disk
- AND the running proxy is NOT hot-reloaded

### REQ-4: CI drift gate hard-blocks on profile drift (R18)

CI SHALL run `sync --check` as a HARD gate. Drift SHALL fail the build and block merge. The gate MUST NOT auto-regenerate the YAML; a human MUST commit the regenerated file.

#### Scenario-1: Drift fails CI

- GIVEN a PR introduces drift in `config/model_profiles.yaml`
- WHEN CI runs the drift gate
- THEN the build fails and merge is blocked

### REQ-5: max_tokens derived from context window with 10% headroom (R18)

The scanner SHALL derive `max_tokens` as `context_window * 0.9` (10% headroom for system + prompt) using the GGUF `*.context_length` value. Per-deployment overrides in `config/model_profiles.yaml` SHALL win over the derived baseline.

#### Scenario-1: Derivation with headroom

- GIVEN GGUF metadata reports `context_length: 32768`
- WHEN the scanner writes the profile
- THEN `max_tokens` is `29491` (32768 * 0.9) unless an override exists

#### Scenario-2: Per-deployment override wins

- GIVEN the YAML carries an explicit `max_tokens: 8192` override
- WHEN the proxy loads profiles at startup
- THEN the forwarded `max_tokens` is `8192`, not the derived value

### REQ-6: Unparseable HF card skipped and logged; file remains (R18)

When the HF model card for a declared `hf_model_id` cannot be parsed, the scanner SHALL skip the HF sampling fetch for that entry, LOG a warning with the model ID and failure reason, and STILL emit a profile for that file carrying GGUF-derived facts and the `max_tokens` headroom derivation. The scanner MUST NOT abort the whole run on one unparseable card, and MUST NOT silently drop the entry without a log line.

#### Scenario-1: Unparseable card is skipped, not fatal

- GIVEN one declared `hf_model_id` has an unparseable HF card
- WHEN `sync` runs
- THEN that file's profile still includes GGUF-derived `max_tokens` and architecture
- AND a warning is logged and other files still sync

### REQ-7: File missing or GGUF critical fields missing → ERROR (R18)

If a declared path in `local_models.yaml` does not exist or is unreadable, the scanner SHALL raise a clear ERROR listing the missing path and abort `sync` — the operator MUST fix the declaration before sync succeeds. If a readable GGUF file is missing critical metadata fields (`general.architecture` or `*.context_length`), the scanner SHALL raise a clear ERROR naming the file and the missing field — a profile cannot be derived without `context_length` for the headroom calculation. SKIP-AND-LOG (REQ-6) applies ONLY to unparseable HF cards, never to missing files or missing critical GGUF fields.

#### Scenario-1: Declared file missing aborts sync

- GIVEN `local_models.yaml` declares `/models/ghost.gguf` and that file does not exist
- WHEN `sync` runs
- THEN the scanner raises an ERROR naming `/models/ghost.gguf` and exits non-zero
- AND no `config/model_profiles.yaml` is written

#### Scenario-2: Missing context_length aborts sync

- GIVEN a readable GGUF file lacks `*.context_length`
- WHEN `sync` runs
- THEN the scanner raises an ERROR naming the file and the missing field

### REQ-8: Runtime loads committed YAML; no runtime HF calls (R18)

The proxy runtime SHALL load `config/model_profiles.yaml` once at startup. The runtime proxy SHALL NOT make HF or network calls to resolve profiles. If the YAML is missing or malformed, the proxy SHALL degrade safely (e.g. `state.model_profiles = None`) and serve direct calls under REQ-1/REQ-3 client-wins; auto-routed profile lookup SHALL return None so intent defaults apply.

#### Scenario-1: Proxy starts offline

- GIVEN `config/model_profiles.yaml` is committed and present
- WHEN the proxy starts with no network access
- THEN profiles load and the proxy serves auto-routed requests

#### Scenario-2: Malformed YAML degrades safely

- GIVEN `config/model_profiles.yaml` is malformed
- WHEN the proxy starts
- THEN startup logs a warning, `state.model_profiles` is `None`, and direct calls still serve

### REQ-9: Intent-aware unknown-model fallback (R18)

`config/model_profiles.yaml` SHALL include two fallback rows with `model=*`: `intent=code` (code-tuned) and `intent=chat` (chat-tuned). When the route resolves to a model ID absent from the table, the proxy SHALL apply the fallback matching the inferred intent and log the substitution. The resolver order SHALL be: exact match → model-wildcard → intent-fallback → None.

#### Scenario-1: Unknown model with code intent

- GIVEN the route resolves to an unknown model and intent is CODE
- WHEN the proxy forwards the payload in auto-routed mode
- THEN the code-intent fallback applies (e.g. `temp=0.2, top_p=0.95`)
- AND the unknown-model substitution is logged

#### Scenario-2: Unknown model with chat intent

- GIVEN the route resolves to an unknown model and intent is CHAT
- WHEN the proxy forwards the payload in auto-routed mode
- THEN the chat-intent fallback applies (e.g. `temp=0.7, top_p=1.0`)

## Notes

- GGUF metadata is GROUND TRUTH (technical facts); HF sampling is CREATIVE DIRECTION; operator `overrides:` are DEPLOYMENT TUNING. Precedence: overrides > HF sampling > GGUF-derived defaults.
- `gguf` Python package and `huggingface_hub` are build-time-only deps for `tools/sync_model_profiles.py`; never added to runtime deps.
- `sync --watch` drafts files only; proxy loads profiles once at startup. Runtime hot-reload is out of scope.
- The scanner does NOT discover models — it processes only paths the operator declared in `local_models.yaml`.