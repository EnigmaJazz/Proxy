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
import logging
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


def fetch_model_card(hf_model_id: str, client: httpx.Client) -> Optional[dict[str, Any]]:
    """Fetch the model card (``README.md``) from a HF model repo, or ``None`` on failure.

    Sampling parameters are documented in the README, not in ``config.json``
    (which carries architecture and tokenizer details).  We return a small
    wrapper ``{"readme": <text>}`` so the parser has the raw markdown to
    extract sampling recommendations from.
    """
    url = f"https://huggingface.co/{hf_model_id}/resolve/main/README.md"
    try:
        response = client.get(url, follow_redirects=True, timeout=30.0)
        response.raise_for_status()
        return {"readme": response.text}
    except Exception as exc:
        logger.warning("Could not fetch model card for %s: %s", hf_model_id, exc)
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


def parse_sampling_params(card: dict[str, Any], hf_model_id: str) -> Optional[dict[str, Any]]:
    """Extract recommended sampling parameters from a HF model card (README).

    Returns a dict of any parameters found, or ``None`` when the card is
    unparseable / fetchable.  An empty dict means the card was readable but
    carried no temperature / top_p (caller should fall back to intent defaults).
    """
    if not isinstance(card, dict):
        logger.warning("Unparseable model card for %s: not a JSON object", hf_model_id)
        return None

    readme = card.get("readme", "")
    if not isinstance(readme, str) or not readme:
        return {}

    params: dict[str, Any] = {}

    temp_match = _TEMP_RE.search(readme)
    if temp_match:
        params["temperature"] = float(temp_match.group(1))

    top_p_match = _TOP_P_RE.search(readme)
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
    """Read ``local_models.yaml`` into ``{model_key: {path, hf_model_id, overrides?}}``.

    Raises:
        ValueError: if the YAML is malformed or any entry is missing the
            required ``path`` or ``hf_model_id`` field.
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
                f"Entry '{model_key}' must be a mapping with 'path' and 'hf_model_id'"
            )
        if "path" not in raw_entry:
            raise ValueError(f"Entry '{model_key}' is missing required 'path'")
        if "hf_model_id" not in raw_entry:
            raise ValueError(f"Entry '{model_key}' is missing required 'hf_model_id'")
        result[str(model_key)] = {
            "path": Path(raw_entry["path"]),
            "hf_model_id": str(raw_entry["hf_model_id"]),
            "overrides": raw_entry.get("overrides", {}),
        }
    return result


def build_profiles(
    local_models_path: Path,
    fetcher: Optional[Callable[[str, httpx.Client], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Generate the full profile YAML structure from ``local_models.yaml``.

    Raises:
        ValueError: malformed ``local_models.yaml`` or missing required fields.
        GGUFReadError: declared file missing or missing critical GGUF fields.
    """
    fetcher = fetcher or fetch_model_card
    models = load_local_models(local_models_path)

    rows: list[dict[str, Any]] = []
    with httpx.Client() as client:
        for model_key, entry in models.items():
            path = entry["path"]
            hf_model_id = entry["hf_model_id"]

            logger.info("Reading GGUF metadata for %s from %s", model_key, path)
            meta = read_gguf_metadata(path)

            logger.info("Fetching HF card for %s (%s)", model_key, hf_model_id)
            card = fetcher(hf_model_id, client)
            if card is None:
                logger.warning(
                    "Unparseable HF card for %s (%s) — emitting GGUF facts only",
                    model_key, hf_model_id,
                )
                hf_params: Optional[dict[str, Any]] = None
            else:
                hf_params = parse_sampling_params(card, hf_model_id)
                if hf_params is None:
                    logger.warning(
                        "Unparseable HF card for %s (%s) — emitting GGUF facts only",
                        model_key, hf_model_id,
                    )

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
    fetcher: Optional[Callable[[str, httpx.Client], Optional[dict[str, Any]]]] = None,
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
        generated = build_profiles(local_models_path, fetcher=fetcher)
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
        return check_profiles(args.models, args.profiles)

    try:
        data = build_profiles(args.models)
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
