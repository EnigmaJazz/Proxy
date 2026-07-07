"""
tools/sync_model_profiles.py - Build-time sync of HF model profiles.

Generates ``config/model_profiles.yaml`` from ``config/local_models.yaml``.
Runtime never calls Hugging Face; the committed YAML is the source of truth.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import yaml

# Make constants importable whether this script is run from repo root or tools/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from constants import get_logger  # noqa: E402

logger = get_logger("proxy.sync_model_profiles")

CONFIG_ROOT = Path(__file__).resolve().parent.parent / "config"
DEFAULT_MODELS_PATH = CONFIG_ROOT / "local_models.yaml"
DEFAULT_PROFILES_PATH = CONFIG_ROOT / "model_profiles.yaml"

HF_API_ROOT = "https://huggingface.co/api/models"

# Fallback values used when a model is gated / unreachable at build time.
# These are the operator-curated baselines; per-deployment overrides win.
_BASELINES: dict[str, dict[str, Any]] = {
    "reasoning":    {"intent": "code", "context_window": 131072, "temperature": 0.6, "top_p": 0.95, "thinking_budget_tokens": 4096},
    "coder":        {"intent": "code", "context_window": 131072, "temperature": 0.1, "top_p": 0.90, "thinking_budget_tokens": 4096},
    "professional": {"intent": "code", "context_window": 131072, "temperature": 0.3, "top_p": 0.95, "thinking_budget_tokens": 1024},
    "architect":    {"intent": "code", "context_window": 131072, "temperature": 0.2, "top_p": 0.95, "thinking_budget_tokens": 4096},
    "creative":     {"intent": "chat", "context_window": 131072, "temperature": 0.7, "top_p": 1.00, "thinking_budget_tokens": 2048},
    "scholar":      {"intent": "chat", "context_window": 131072, "temperature": 0.4, "top_p": 0.95, "thinking_budget_tokens": 2048},
    "worker":       {"intent": "code", "context_window": 131072, "temperature": 0.2, "top_p": 0.95, "thinking_budget_tokens": 2048},
    "chatter":      {"intent": "chat", "context_window": 32768,  "temperature": 0.7, "top_p": 1.00, "thinking_budget_tokens": 0},
    "frontdesk":    {"intent": "chat", "context_window": 32768,  "temperature": 0.3, "top_p": 1.00, "thinking_budget_tokens": 0},
    "lifeboat":     {"intent": "chat", "context_window": 131072, "temperature": 0.7, "top_p": 1.00, "thinking_budget_tokens": 0},
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
    """Fetch ``config.json`` from a HF model repo, or ``None`` on failure."""
    url = f"https://huggingface.co/{hf_model_id}/resolve/main/config.json"
    try:
        response = client.get(url, follow_redirects=True, timeout=30.0)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("Could not fetch model card for %s: %s", hf_model_id, exc)
        return None


def parse_sampling_params(card: dict[str, Any], hf_model_id: str) -> Optional[dict[str, Any]]:
    """Extract context_window (and optional temperature/top_p) from config.json."""
    if not isinstance(card, dict):
        logger.warning("Unparseable model card for %s: not a JSON object", hf_model_id)
        return None

    context_window = None
    for key in ("sliding_window", "max_position_embeddings", "max_sequence_length", "n_positions"):
        value = card.get(key)
        if isinstance(value, int) and value > 0:
            context_window = value
            break

    if context_window is None:
        logger.warning("Unparseable model card for %s: no context_window found", hf_model_id)
        return None

    params: dict[str, Any] = {"context_window": context_window}

    card_temperature = card.get("temperature")
    if isinstance(card_temperature, (int, float)):
        params["temperature"] = float(card_temperature)

    card_top_p = card.get("top_p")
    if isinstance(card_top_p, (int, float)):
        params["top_p"] = float(card_top_p)

    return params


def build_profile_entry(
    model_key: str,
    hf_model_id: str,
    card: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Compose one ``model_profiles.yaml`` row; returns ``None`` to skip."""
    baseline = _BASELINES.get(model_key)
    if baseline is None:
        logger.warning("No baseline for model_key '%s' — skipping", model_key)
        return None

    if card is None:
        params: dict[str, Any] = {}
    else:
        params = parse_sampling_params(card, hf_model_id)
        if params is None:
            return None

    context_window = params.get("context_window", baseline["context_window"])
    row: dict[str, Any] = {
        "model": model_key,
        "intent": baseline["intent"],
        "context_window": context_window,
        "max_tokens": derive_max_tokens(context_window),
        "temperature": params.get("temperature", baseline["temperature"]),
        "top_p": params.get("top_p", baseline["top_p"]),
        "thinking_budget_tokens": baseline["thinking_budget_tokens"],
        "seed": None,
        "top_logprobs": None,
        "response_format": None,
        "n": 1,
    }
    return row


def load_local_models(path: Path) -> dict[str, str]:
    """Load ``local_models.yaml`` and return the ``model_key -> hf_model_id`` map."""
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("local_models.yaml must contain a top-level mapping")
    models = data.get("models")
    if not isinstance(models, dict):
        raise ValueError("local_models.yaml must contain a 'models' mapping")
    return {str(k): str(v) for k, v in models.items()}


def build_profiles(
    local_models_path: Path,
    fetcher: Optional[Callable[[str, httpx.Client], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Generate the full profile YAML structure from ``local_models.yaml``."""
    fetcher = fetcher or fetch_model_card
    models = load_local_models(local_models_path)

    rows: list[dict[str, Any]] = []
    with httpx.Client() as client:
        for model_key, hf_model_id in models.items():
            card = fetcher(hf_model_id, client)
            row = build_profile_entry(model_key, hf_model_id, card)
            if row:
                rows.append(row)
            else:
                logger.warning("Skipped model '%s' (%s)", model_key, hf_model_id)

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


def check_profiles(local_models_path: Path, profiles_path: Path) -> int:
    """
    Regenerate profiles and compare with the committed file.

    Exit codes:
      0 - in sync
      1 - drift detected
      2 - missing/malformed local_models.yaml
    """
    try:
        generated = build_profiles(local_models_path)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        logger.error("Invalid local_models.yaml: %s", exc)
        return 2

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
        description="Sync HF model profiles into config/model_profiles.yaml",
    )
    parser.add_argument(
        "command",
        choices=["sync"],
        help="Subcommand (only 'sync' is supported)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Drift detection mode: exit 0 in sync, 1 on drift, 2 on config error",
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

    data = build_profiles(args.models)
    write_profiles(args.profiles, data)
    logger.info("Wrote %d profile rows to %s", len(data["profiles"]), args.profiles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
