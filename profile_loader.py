"""
profile_loader.py - Runtime loader for build-time-synced model profiles.

Reads ``config/model_profiles.yaml`` once at proxy startup and exposes a
resolver used by R17 parameter authority.  Safe-degrades to ``None`` when
the file is missing or malformed so a bad config commit cannot brick the proxy.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from constants import get_logger

logger = get_logger("proxy.profile_loader")

# Proxy intent vocabulary mapped to the two YAML buckets.
_CODE_INTENTS: frozenset[str] = frozenset({"CODE", "ARCHITECT", "TOOL", "PROFESSIONAL"})
_CHAT_INTENTS: frozenset[str] = frozenset({"CHAT", "CREATIVE", "SCHOLAR"})

# Keys that are metadata on a YAML row, not forwarded sampling parameters.
_METADATA_KEYS: frozenset[str] = frozenset({"model", "intent", "context_window"})


@dataclass(frozen=True)
class ProfileEntry:
    """A resolved profile row ready to be applied to a forwarded payload."""

    model: str
    intent: str
    values: dict[str, Any]
    source: str


def bucket(intent: str) -> str:
    """Map a proxy intent to the YAML ``code`` or ``chat`` bucket."""
    normalized = intent.upper()
    if normalized in _CODE_INTENTS:
        return "code"
    if normalized in _CHAT_INTENTS:
        return "chat"
    return "chat"


class ModelProfileTable:
    """In-memory profile table with intent-aware fallback resolution."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows: list[dict[str, Any]] = list(rows)

    def resolve(self, intent: str, model_key: str) -> Optional[ProfileEntry]:
        """
        Resolve the best profile row for an intent + model_key.

        Lookup order:
          1. exact row where row.model == model_key AND row.intent == bucket(intent)
          2. exact row where row.model == model_key AND row.intent == "*"
          3. fallback row where row.model == "*" AND row.intent == bucket(intent)
          4. None
        """
        intent_bucket = bucket(intent)

        for row in self.rows:
            if row.get("model") == model_key and row.get("intent") == intent_bucket:
                return self._make_entry(row, model_key, intent_bucket, "exact")

        for row in self.rows:
            if row.get("model") == model_key and row.get("intent") == "*":
                return self._make_entry(row, model_key, intent_bucket, "model_wildcard")

        for row in self.rows:
            if row.get("model") == "*" and row.get("intent") == intent_bucket:
                logger.info(
                    "Unknown model '%s' for intent '%s' — using %s fallback",
                    model_key, intent, intent_bucket,
                )
                return self._make_entry(row, model_key, intent_bucket, "intent_fallback")

        return None

    @staticmethod
    def _make_entry(
        row: dict[str, Any],
        model_key: str,
        intent_bucket: str,
        source: str,
    ) -> ProfileEntry:
        values = {
            key: value
            for key, value in row.items()
            if key not in _METADATA_KEYS and value is not None
        }
        return ProfileEntry(
            model=model_key,
            intent=intent_bucket,
            values=values,
            source=source,
        )


def load_model_profiles(yaml_path: Path) -> Optional[ModelProfileTable]:
    """
    Load ``config/model_profiles.yaml`` at startup.

    Returns ``None`` on missing or malformed YAML so the proxy can fall back to
    the legacy intent-default table.  Errors are logged.
    """
    try:
        with yaml_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError:
        logger.warning("Model profiles not found at %s — falling back to legacy defaults", yaml_path)
        return None
    except yaml.YAMLError as exc:
        logger.warning("Malformed model profiles YAML at %s: %s — falling back", yaml_path, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("Model profiles YAML has no top-level mapping — falling back")
        return None

    rows = data.get("profiles")
    if not isinstance(rows, list):
        logger.warning("Model profiles YAML missing 'profiles' list — falling back")
        return None

    return ModelProfileTable(rows)
