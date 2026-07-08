"""
tools/sync_model_profiles.py - Build-time sync of model profiles.

Generates ``config/model_profiles.yaml`` from ``config/local_models.yaml``.
The scanner treats actual GGUF files on disk as the source of truth for
architecture, context window, and quantization.  Hugging Face is consulted
per declared ``hf_model_id`` for recommended sampling parameters only.
Runtime never calls Hugging Face; the committed YAML is the source of truth.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import yaml

# Make constants importable whether this script is run from repo root or tools/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from constants import get_logger  # noqa: E402
from tools.gguf_reader import GGUFMetadata, GGUFReadError, read_gguf_metadata  # noqa: E402

logger = get_logger("proxy.sync_model_profiles")

CONFIG_ROOT = Path(__file__).resolve().parent.parent / "config"
DEFAULT_MODELS_PATH = CONFIG_ROOT / "local_models.yaml"
DEFAULT_PROFILES_PATH = CONFIG_ROOT / "model_profiles.yaml"

# Model keys map to a single intent bucket used in the generated YAML.
# This is the only remaining model_key→intent mapping; numeric baselines
# were retired when GGUF became the ground-truth source of context_window.
_MODEL_INTENTS: dict[str, str] = {
    "reasoning": "code",
    "coder": "code",
    "professional": "code",
    "architect": "code",
    "creative": "chat",
    "scholar": "chat",
    "worker": "code",
    "chatter": "chat",
    "frontdesk": "chat",
    "lifeboat": "chat",
}

_INTENT_DEFAULTS: dict[str, dict[str, Any]] = {
    "code": {"temperature": 0.2, "top_p": 0.95, "thinking_budget_tokens": 4096},
    "chat": {"temperature": 0.7, "top_p": 1.0, "thinking_budget_tokens": 0},
}

_FALLBACK_ROWS: list[dict[str, Any]] = [
    {"model": "*", "intent": "code", "temperature": 0.2, "top_p": 0.95, "max_tokens": 8192, "thinking_budget_tokens": 2048},
    {"model": "*", "intent": "chat", "temperature": 0.7, "top_p": 1.0, "max_tokens": 4096, "thinking_budget_tokens": 0},
]

_R11_KEYS: tuple[str, ...] = (
    "temperature", "top_p", "max_tokens", "thinking_budget_tokens",
    "seed", "top_logprobs", "response_format", "n",
)


def derive_max_tokens(context_window: int) -> int:
    """Return ``int(context_window * 0.9)`` with 10 % headroom."""
    return int(context_window * 0.9)


def discover_models(models_dir: Path) -> int:
    """Walk *models_dir* for ``.gguf`` files and print a YAML template for ``local_models.yaml``.

    The operator reviews the output, fills in the actual ``hf_model_id`` for each
    model (the scanner cannot guess the HF repo from a filename — that was the
    hallucination problem this R18 design deliberately avoids), and pastes the
    entries into ``config/local_models.yaml`` under the top-level ``models:`` key.

    Exit codes:
      0 - template printed (or no GGUF files found; logged as a warning)
      1 - models_dir is not a directory
    """
    if not models_dir.exists():
        logger.error("Directory not found: %s", models_dir)
        return 1
    if not models_dir.is_dir():
        logger.error("Not a directory: %s", models_dir)
        return 1

    gguf_files = sorted(models_dir.glob("*.gguf"))
    if not gguf_files:
        logger.warning("No GGUF files found in %s", models_dir)
        return 0

    print(f"# Discovered GGUF files in {models_dir}")
    print("# Add the entries you want to config/local_models.yaml under `models:`")
    print("# `sampling_source` is OPTIONAL — point it at any URL (vendor docs, HF")
    print("# model card, GitHub README, blog post) or local file path that carries")
    print("# the model's recommended sampling parameters. The scanner parses it")
    print("# with a tolerant regex; if it can't extract anything, intent defaults")
    print("# fill in.  See https://huggingface.co/<org>/<model>/raw/main/README.md")
    print("# for the HF model-card URL shape.")
    print("models:")
    seen_keys: set[str] = set()
    for path in gguf_files:
        model_key = _filename_to_model_key(path.stem)
        if model_key in seen_keys:
            logger.warning("Duplicate model_key %r from %s — rename one before pasting", model_key, path)
        seen_keys.add(model_key)
        print(f"  {model_key}:")
        print(f"    path: {path}")
        print(f"    sampling_source: <URL or local path to the model's recommended sampling params>")
    return 0


def _filename_to_model_key(stem: str) -> str:
    """Convert a GGUF filename stem to a ``model_key``.

    Lowercase and replace ``.`` and ``_`` with ``-``.  The operator can rename
    any collisions or shorten long quantized names (``Qwen3.6-35B-A3B-UD-Q4_K_XL``
    becomes ``qwen3-6-35b-a3b-ud-q4-k-xl``); the goal here is just a starting
    point, not a canonical mapping.
    """
    return stem.lower().replace("_", "-").replace(".", "-")


def fetch_sampling_source(source: str, client: httpx.Client) -> Optional[str]:
    """Fetch the operator-declared sampling source: an HTTP(S) URL or a local file path.

    Returns the raw text content, or ``None`` on failure.  ``file://`` URLs
    are also accepted.
    """
    if source.startswith(("http://", "https://")):
        try:
            response = client.get(source, follow_redirects=True, timeout=30.0)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", source, exc)
            return None
    if source.startswith("file://"):
        path = Path(source[len("file://"):])
    else:
        path = Path(source)
    if not path.exists():
        logger.warning("Sampling source not found: %s", path)
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None


# Match the first numeric value following a "temperature" mention.  Tolerant of:
#   "temperature of $0.6$"          → 0.6
#   "temperature: 0.7"              → 0.7
#   "temperature = 0.5-0.7 (recommended)"  → 0.5  (first number; closer to model default)
#   "Set the temperature within 0.5-0.7"   → 0.5
_TEMP_RE = re.compile(
    r"temperature[:\s=]+(?:of\s+|within\s+|range\s+of\s+)?\$?(\d+\.?\d*)",
    re.IGNORECASE,
)
# Match the first numeric value following a "top_p" / "top-p" mention.
_TOP_P_RE = re.compile(
    r"top[-_\s]?p[:\s=]+(?:of\s+|value\s+of\s+|is\s+)?\$?(\d+\.?\d*)",
    re.IGNORECASE,
)


EXTRACTOR_MODEL_DEFAULT = "coder"
EXTRACTOR_URL_DEFAULT = os.environ.get("PROXY_EXTRACTOR_URL", "http://localhost:13000/v1/chat/completions")
EXTRACTOR_TIMEOUT = float(os.environ.get("PROXY_EXTRACTOR_TIMEOUT", "60"))


def extract_with_llm(
    content: str,
    source: str,
    client: httpx.Client,
    extractor_model: str = EXTRACTOR_MODEL_DEFAULT,
    extractor_url: str = EXTRACTOR_URL_DEFAULT,
) -> Optional[dict[str, Any]]:
    """Ask the proxy's own model to extract sampling parameters from *content*.

    The proxy serves a local GGUF model (default: ``coder``) and is
    expected to be running on the same machine.  The call is best-effort;
    on any failure (proxy down, model missing, response unparseable) we
    return ``None`` so the caller can fall through to intent defaults.

    The proxy always returns Server-Sent Events (SSE) regardless of the
    ``stream`` parameter on the request, so we parse the SSE stream and
    accumulate the ``delta.content`` chunks from chat-completion events.
    """
    system_prompt = (
        "You are an expert at extracting sampling parameters from model "
        "documentation. Respond with a JSON object containing 'temperature' "
        "and/or 'top_p' fields with numeric values. If no recommendations "
        "are found, respond with an empty JSON object. Do not include any "
        "other text."
    )
    user_prompt = (
        f"Extract the recommended sampling parameters (temperature, top_p) "
        f"from this documentation for the model. Return ONLY a JSON object.\n\n"
        f"---\n{content}\n---"
    )
    try:
        # The proxy always returns SSE regardless of the ``stream`` parameter,
        # so we read the response line-by-line and accumulate content.
        response = client.post(
            extractor_url,
            json={
                "model": extractor_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                "top_p": 0.95,
            },
            timeout=EXTRACTOR_TIMEOUT,
        )
        response.raise_for_status()
        accumulated: list[str] = []
        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            # Reasoning models put output in `reasoning_content`; chat models
            # in `content`. Check both so the extractor works regardless of
            # which model the proxy routes to.
            chunk = delta.get("content") or delta.get("reasoning_content")
            if isinstance(chunk, str) and chunk:
                accumulated.append(chunk)
        message = "".join(accumulated)
        if not message:
            logger.warning("Extractor returned no content for %s", source)
            return None
        parsed = json.loads(message)
        if not isinstance(parsed, dict):
            logger.warning("Extractor returned non-dict for %s: %r", source, parsed)
            return None
        params: dict[str, Any] = {}
        if isinstance(parsed.get("temperature"), (int, float)):
            params["temperature"] = float(parsed["temperature"])
        if isinstance(parsed.get("top_p"), (int, float)):
            params["top_p"] = float(parsed["top_p"])
        if params:
            logger.info("LLM extractor returned %s for %s", params, source)
        return params or None
    except Exception as exc:
        logger.warning("LLM extraction failed for %s: %s", source, exc)
        return None


def parse_sampling_params(content: str, source: str) -> dict[str, Any]:
    """Extract recommended sampling parameters from a sampling source.

    Returns a dict of any parameters found.  An empty dict means the content
    was readable but carried no temperature / top_p (caller should fall back
    to intent defaults).  Returns ``None`` when the content is empty/unreadable.
    """
    if not isinstance(content, str) or not content:
        return None

    params: dict[str, Any] = {}

    temp_match = _TEMP_RE.search(content)
    if temp_match:
        params["temperature"] = float(temp_match.group(1))

    top_p_match = _TOP_P_RE.search(content)
    if top_p_match:
        params["top_p"] = float(top_p_match.group(1))

    return params


def build_profile_entry(
    model_key: str,
    meta: GGUFMetadata,
    entry: dict[str, Any],
    hf_params: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Compose one ``model_profiles.yaml`` row.

    Precedence: operator ``overrides:`` > HF sampling > GGUF-derived defaults.
    When *hf_params* is ``None`` (unparseable HF card) the row still carries
    GGUF facts and max_tokens; temperature/top_p are omitted so the runtime
    resolver falls back to legacy intent defaults.
    """
    overrides = entry.get("overrides", {}) if isinstance(entry, dict) else {}
    intent = _MODEL_INTENTS.get(model_key, "chat")
    intent_defaults = _INTENT_DEFAULTS.get(intent, {})

    row: dict[str, Any] = {
        "model": model_key,
        "intent": intent,
        "architecture": meta.architecture,
        "file_type": meta.file_type,
        "context_window": meta.context_length,
        "max_tokens": overrides.get("max_tokens", derive_max_tokens(meta.context_length)),
        # Intent defaults are the baseline for creative-direction params.
        "temperature": intent_defaults.get("temperature"),
        "top_p": intent_defaults.get("top_p"),
        "thinking_budget_tokens": overrides.get(
            "thinking_budget_tokens", intent_defaults.get("thinking_budget_tokens", 0)
        ),
        "seed": None,
        "top_logprobs": None,
        "response_format": None,
        "n": 1,
    }

    # HF sampling: creative direction; overrides the intent baseline.
    if hf_params:
        for key in ("temperature", "top_p"):
            if key in hf_params:
                row[key] = hf_params[key]

    # Per-entry overrides win over everything.
    for key, value in overrides.items():
        if key in _R11_KEYS or key in ("temperature", "top_p", "max_tokens", "thinking_budget_tokens"):
            row[key] = value

    return row


def load_local_models(path: Path) -> dict[str, dict[str, Any]]:
    """Read ``local_models.yaml`` into ``{model_key: {path, sampling_source?, overrides?}}``.

    ``path`` is required.  ``sampling_source`` is optional — when omitted, the
    scanner uses the intent defaults for sampling parameters without trying
    to fetch a source.  ``overrides`` is optional per-entry tuning.

    Raises:
        ValueError: if the YAML is malformed or any entry is missing the
            required ``path`` field.
    """
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    if not isinstance(data, dict):
        raise ValueError("local_models.yaml must contain a top-level mapping")
    models = data.get("models")
    if not isinstance(models, dict):
        raise ValueError("local_models.yaml must contain a 'models' mapping")

    result: dict[str, dict[str, Any]] = {}
    for model_key, raw_entry in models.items():
        if not isinstance(raw_entry, dict):
            raise ValueError(
                f"Entry '{model_key}' must be a mapping with 'path'"
            )
        if "path" not in raw_entry:
            raise ValueError(f"Entry '{model_key}' is missing required 'path'")
        result[str(model_key)] = {
            "path": Path(raw_entry["path"]),
            "sampling_source": raw_entry.get("sampling_source"),
            "overrides": raw_entry.get("overrides", {}),
        }
    return result


def build_profiles(
    local_models_path: Path,
    fetcher: Optional[Callable[[str, httpx.Client], Optional[str]]] = None,
    extractor: Optional[Callable[[str, str, httpx.Client], Optional[dict[str, Any]]]] = None,
    extractor_model: str = EXTRACTOR_MODEL_DEFAULT,
) -> dict[str, Any]:
    """Generate the full profile YAML structure from ``local_models.yaml``.

    For each entry with a ``sampling_source``:
      1. Fetch the source content
      2. Try the regex parser (fast, deterministic)
      3. If the regex didn't extract anything, fall back to the LLM extractor
         (the proxy's own model, default ``coder``)
      4. If both fail, the row uses intent defaults for sampling parameters

    Raises:
        ValueError: malformed ``local_models.yaml`` or missing required fields.
        GGUFReadError: declared file missing or missing critical GGUF fields.
    """
    fetcher = fetcher or fetch_sampling_source
    extractor = extractor or extract_with_llm
    models = load_local_models(local_models_path)

    rows: list[dict[str, Any]] = []
    with httpx.Client() as client:
        for model_key, entry in models.items():
            path = entry["path"]
            sampling_source = entry.get("sampling_source")

            logger.info("Reading GGUF metadata for %s from %s", model_key, path)
            meta = read_gguf_metadata(path)

            hf_params: Optional[dict[str, Any]] = None
            if sampling_source:
                logger.info("Fetching sampling source for %s (%s)", model_key, sampling_source)
                content = fetcher(sampling_source, client)
                if content is None:
                    logger.warning(
                        "Unparseable sampling source for %s (%s) — emitting GGUF facts only",
                        model_key, sampling_source,
                    )
                else:
                    hf_params = parse_sampling_params(content, sampling_source)
                    if not hf_params:
                        logger.info(
                            "Regex didn't extract params from %s — falling back to LLM extractor",
                            sampling_source,
                        )
                        hf_params = extractor(content, sampling_source, client, extractor_model)
                        if not hf_params:
                            logger.warning(
                                "LLM extractor returned no params for %s — using intent defaults",
                                sampling_source,
                            )
            else:
                logger.info("No sampling_source for %s — using intent defaults", model_key)

            row = build_profile_entry(model_key, meta, entry, hf_params)
            rows.append(row)

    rows.extend(_FALLBACK_ROWS)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profiles": rows,
        "overrides": {},
    }


def write_profiles(path: Path, data: dict[str, Any]) -> None:
    """Write profiles YAML with deterministic ordering and a header comment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated by tools/sync_model_profiles.py sync.\n"
        "# DO NOT hand-edit sampling values; per-deployment overrides live under the\n"
        "# `overrides:` key. CI runs `sync --check` and HARD-BLOCKS on drift.\n"
    )
    with path.open("w", encoding="utf-8") as handle:
        handle.write(header)
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)


def profiles_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare generated profiles ignoring ``generated_at``."""
    return (
        left.get("profiles") == right.get("profiles")
        and left.get("overrides") == right.get("overrides")
    )


def check_profiles(
    local_models_path: Path,
    profiles_path: Path,
    fetcher: Optional[Callable[[str, httpx.Client], Optional[str]]] = None,
    extractor: Optional[Callable[[str, str, httpx.Client], Optional[dict[str, Any]]]] = None,
    extractor_model: str = EXTRACTOR_MODEL_DEFAULT,
) -> int:
    """
    Regenerate profiles and compare with the committed file.

    Exit codes:
      0 - in sync
      1 - drift detected (filesystem change or HF card update)
      2 - missing/malformed local_models.yaml
      3 - declared file missing or GGUF critical fields missing
    """
    try:
        generated = build_profiles(
            local_models_path, fetcher=fetcher, extractor=extractor, extractor_model=extractor_model
        )
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        logger.error("Invalid local_models.yaml: %s", exc)
        return 2
    except GGUFReadError as exc:
        logger.error("GGUF read error: %s", exc)
        return 3

    try:
        with profiles_path.open("r", encoding="utf-8") as handle:
            committed = yaml.safe_load(handle)
    except FileNotFoundError:
        logger.error("Committed profiles not found at %s", profiles_path)
        return 1
    except yaml.YAMLError as exc:
        logger.error("Committed profiles YAML is malformed: %s", exc)
        return 1

    if not profiles_equal(generated, committed):
        logger.error("Profile drift detected — run `sync` and commit the regenerated file")
        return 1

    logger.info("Profiles are in sync")
    return 0


def watch_profiles(local_models_path: Path, profiles_path: Path) -> None:
    """Re-run ``sync`` whenever ``local_models.yaml`` changes."""
    logger.info("Watching %s for changes (Ctrl-C to stop)", local_models_path)
    last_mtime: Optional[float] = None
    while True:
        try:
            current_mtime = local_models_path.stat().st_mtime
        except FileNotFoundError:
            current_mtime = None

        if last_mtime is None or current_mtime != last_mtime:
            logger.info("Change detected — regenerating %s", profiles_path)
            data = build_profiles(local_models_path)
            write_profiles(profiles_path, data)
            last_mtime = current_mtime

        time.sleep(1.0)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Sync model profiles into config/model_profiles.yaml",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Drift detection mode: exit 0 in sync, 1 on drift, 2 on config error, 3 on file/metadata error",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch local_models.yaml and regenerate profiles on change",
    )
    parser.add_argument(
        "--models",
        type=Path,
        default=DEFAULT_MODELS_PATH,
        help=f"Path to local_models.yaml (default: {DEFAULT_MODELS_PATH})",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        default=DEFAULT_PROFILES_PATH,
        help=f"Path to model_profiles.yaml (default: {DEFAULT_PROFILES_PATH})",
    )
    parser.add_argument(
        "--extractor",
        default=EXTRACTOR_MODEL_DEFAULT,
        help=f"Model name to use as the LLM extractor fallback (default: {EXTRACTOR_MODEL_DEFAULT})",
    )
    args = parser.parse_args(argv)

    if args.check and args.watch:
        parser.error("--check and --watch are mutually exclusive")

    if args.watch:
        try:
            watch_profiles(args.models, args.profiles)
        except KeyboardInterrupt:
            logger.info("Watch stopped")
        return 0

    if args.check:
        return check_profiles(args.models, args.profiles, extractor_model=args.extractor)

    try:
        data = build_profiles(args.models, extractor_model=args.extractor)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        logger.error("Invalid local_models.yaml: %s", exc)
        return 2
    except GGUFReadError as exc:
        logger.error("GGUF read error: %s", exc)
        return 3

    write_profiles(args.profiles, data)
    logger.info("Wrote %d profile rows to %s", len(data["profiles"]), args.profiles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
