"""R18 tests for the Glass-Pipe Followups change.

PR1 covers the build-time HF profile sync toolchain and the runtime loader.
R17 authority tests live in PR2.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from profile_loader import (
    ModelProfileTable,
    ProfileEntry,
    bucket,
    load_model_profiles,
)
import tools.sync_model_profiles as sync_profiles


# ---------------------------------------------------------------------------
# R18 runtime loader
# ---------------------------------------------------------------------------
class TestProfileLoader:
    """ModelProfileTable.resolve precedence and safe degradation."""

    def test_bucket_maps_code_intents(self) -> None:
        """CODE, ARCHITECT, TOOL and PROFESSIONAL map to the code bucket."""
        for intent in ("CODE", "ARCHITECT", "TOOL", "PROFESSIONAL"):
            assert bucket(intent) == "code"

    def test_bucket_maps_chat_intents(self) -> None:
        """CHAT, CREATIVE and SCHOLAR map to the chat bucket."""
        for intent in ("CHAT", "CREATIVE", "SCHOLAR"):
            assert bucket(intent) == "chat"

    def test_resolve_exact_match_wins(self) -> None:
        """Exact (intent, model) row beats model-wildcard and fallback rows."""
        table = ModelProfileTable([
            {"model": "coder", "intent": "code", "temperature": 0.1},
            {"model": "coder", "intent": "*", "temperature": 0.5},
            {"model": "*", "intent": "code", "temperature": 0.9},
        ])
        entry = table.resolve("CODE", "coder")
        assert entry is not None
        assert entry.values["temperature"] == 0.1
        assert entry.source == "exact"

    def test_resolve_model_wildcard_second(self) -> None:
        """Model-specific wildcard row beats intent fallback."""
        table = ModelProfileTable([
            {"model": "coder", "intent": "*", "temperature": 0.5},
            {"model": "*", "intent": "code", "temperature": 0.9},
        ])
        entry = table.resolve("CODE", "coder")
        assert entry is not None
        assert entry.values["temperature"] == 0.5
        assert entry.source == "model_wildcard"

    def test_resolve_intent_fallback_third(self) -> None:
        """Intent fallback row is used when no model-specific row exists."""
        table = ModelProfileTable([
            {"model": "*", "intent": "code", "temperature": 0.2},
        ])
        entry = table.resolve("CODE", "unknown-model")
        assert entry is not None
        assert entry.values["temperature"] == 0.2
        assert entry.source == "intent_fallback"

    def test_resolve_returns_none_when_empty(self) -> None:
        """An empty table resolves to None."""
        table = ModelProfileTable([])
        assert table.resolve("CODE", "anything") is None

    def test_load_missing_file_returns_none(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Missing YAML degrades safely to None and logs a warning."""
        with caplog.at_level(logging.WARNING):
            result = load_model_profiles(tmp_path / "missing.yaml")
        assert result is None
        assert "not found" in caplog.text.lower()

    def test_load_malformed_yaml_returns_none(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Malformed YAML degrades safely to None and logs a warning."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("not: valid: yaml: [")
        with caplog.at_level(logging.WARNING):
            result = load_model_profiles(bad)
        assert result is None
        assert "malformed" in caplog.text.lower() or "falling back" in caplog.text.lower()

    def test_load_missing_profiles_key_returns_none(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """YAML without a 'profiles' list degrades to None."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("generated_at: now\n")
        with caplog.at_level(logging.WARNING):
            result = load_model_profiles(bad)
        assert result is None


class TestProfileFallback:
    """Intent-aware fallback for unknown models."""

    @pytest.fixture
    def table(self) -> ModelProfileTable:
        return ModelProfileTable([
            {"model": "*", "intent": "code", "temperature": 0.2, "top_p": 0.95, "max_tokens": 8192},
            {"model": "*", "intent": "chat", "temperature": 0.7, "top_p": 1.0, "max_tokens": 4096},
        ])

    def test_unknown_code_model_uses_code_fallback(self, table: ModelProfileTable, caplog: pytest.LogCaptureFixture) -> None:
        """Unknown model with CODE intent picks the code fallback row."""
        with caplog.at_level(logging.INFO):
            entry = table.resolve("CODE", "unknown")
        assert entry is not None
        assert entry.values["temperature"] == 0.2
        assert entry.source == "intent_fallback"
        assert "unknown model" in caplog.text.lower()

    def test_unknown_chat_model_uses_chat_fallback(self, table: ModelProfileTable) -> None:
        """Unknown model with CHAT intent picks the chat fallback row."""
        entry = table.resolve("CHAT", "unknown")
        assert entry is not None
        assert entry.values["temperature"] == 0.7
        assert entry.source == "intent_fallback"


# ---------------------------------------------------------------------------
# R18 build-time sync
# ---------------------------------------------------------------------------
class TestSyncModelProfiles:
    """Unit-level behaviour of the scraper helpers."""

    def test_derive_max_tokens_applies_headroom(self) -> None:
        """max_tokens is derived as ``int(context_window * 0.9)``."""
        assert sync_profiles.derive_max_tokens(32768) == 29491
        assert sync_profiles.derive_max_tokens(1000) == 900

    def test_parse_sampling_params_extracts_context_window(self) -> None:
        """Context window is read from recognised config keys."""
        card = {"max_position_embeddings": 32768}
        params = sync_profiles.parse_sampling_params(card, "test")
        assert params is not None
        assert params["context_window"] == 32768

    def test_parse_sampling_params_prefers_sliding_window(self) -> None:
        """When both keys exist, sliding_window wins (larger typical context)."""
        card = {"max_position_embeddings": 32768, "sliding_window": 131072}
        params = sync_profiles.parse_sampling_params(card, "test")
        assert params is not None
        assert params["context_window"] == 131072

    def test_parse_sampling_params_returns_none_without_context(self) -> None:
        """A card with no recognised context key is unparseable."""
        card = {"architectures": ["Foo"]}
        assert sync_profiles.parse_sampling_params(card, "test") is None

    def test_build_profile_entry_skips_unparseable_card(self) -> None:
        """A malformed card causes the model to be skipped, not fatal."""
        row = sync_profiles.build_profile_entry("coder", "org/model", {"foo": "bar"})
        assert row is None

    def test_build_profile_entry_uses_baseline_when_fetch_fails(self) -> None:
        """When the HF fetch fails, the operator baseline supplies values."""
        row = sync_profiles.build_profile_entry("frontdesk", "org/model", None)
        assert row is not None
        assert row["model"] == "frontdesk"
        assert row["intent"] == "chat"
        assert row["max_tokens"] == sync_profiles.derive_max_tokens(32768)


class TestSyncCheckDrift:
    """Scraper CLI drift detection and exit-code contract."""

    @pytest.fixture
    def tmp_models(self, tmp_path: Path) -> Path:
        """Return a local_models.yaml with one synthetic model."""
        models = tmp_path / "local_models.yaml"
        models.write_text("models:\n  testmodel: org/testmodel\n")
        return models

    def fake_fetcher(self, context_window: int = 10000) -> Any:
        """Return a fetcher that serves a synthetic config.json."""
        def _fetch(hf_model_id: str, client: Any) -> dict[str, Any]:
            return {"max_position_embeddings": context_window}
        return _fetch

    def test_check_exits_zero_when_in_sync(self, tmp_path: Path, tmp_models: Path) -> None:
        """sync --check returns 0 when the committed file matches generated data."""
        profiles = tmp_path / "model_profiles.yaml"
        data = sync_profiles.build_profiles(tmp_models, fetcher=self.fake_fetcher())
        sync_profiles.write_profiles(profiles, data)
        assert sync_profiles.check_profiles(tmp_models, profiles) == 0

    def test_check_exits_one_on_drift(self, tmp_path: Path, tmp_models: Path) -> None:
        """sync --check returns 1 when the committed file is stale."""
        profiles = tmp_path / "model_profiles.yaml"
        data = sync_profiles.build_profiles(tmp_models, fetcher=self.fake_fetcher())
        sync_profiles.write_profiles(profiles, data)

        # Introduce drift.
        committed = yaml.safe_load(profiles.read_text())
        committed["profiles"][0]["max_tokens"] = 12345
        profiles.write_text(yaml.safe_dump(committed))

        assert sync_profiles.check_profiles(tmp_models, profiles) == 1

    def test_check_exits_two_on_malformed_config(self, tmp_path: Path) -> None:
        """sync --check returns 2 when local_models.yaml is invalid."""
        bad_models = tmp_path / "bad_models.yaml"
        bad_models.write_text("not a valid mapping: [")
        profiles = tmp_path / "model_profiles.yaml"
        profiles.write_text("profiles: []\noverrides: {}\n")
        assert sync_profiles.check_profiles(bad_models, profiles) == 2

    def test_unparseable_card_is_skipped_and_logged(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A card with no context_window is skipped with a warning."""
        models = tmp_path / "local_models.yaml"
        models.write_text("models:\n  badmodel: org/badmodel\n")

        def _bad_fetch(hf_model_id: str, client: Any) -> dict[str, Any]:
            return {"architectures": ["Unknown"]}

        with caplog.at_level(logging.WARNING):
            data = sync_profiles.build_profiles(models, fetcher=_bad_fetch)

        assert not any(r.get("model") == "badmodel" for r in data["profiles"])
        assert "skipped" in caplog.text.lower() or "unparseable" in caplog.text.lower()


class TestCommittedProfiles:
    """The committed profile YAML is loadable and contains required rows."""

    def test_committed_profiles_load(self) -> None:
        """``config/model_profiles.yaml`` loads into a ModelProfileTable."""
        table = load_model_profiles(Path("config/model_profiles.yaml"))
        assert table is not None

    def test_committed_profiles_have_fallback_rows(self) -> None:
        """The committed file includes code and chat fallback rows."""
        table = load_model_profiles(Path("config/model_profiles.yaml"))
        assert table is not None
        code_fallback = table.resolve("CODE", "no-such-model")
        chat_fallback = table.resolve("CHAT", "no-such-model")
        assert code_fallback is not None
        assert chat_fallback is not None
        assert code_fallback.values["temperature"] != chat_fallback.values["temperature"]

    def test_committed_profiles_cover_all_local_models(self) -> None:
        """Every model in local_models.yaml has a profile row."""
        models = sync_profiles.load_local_models(Path("config/local_models.yaml"))
        table = load_model_profiles(Path("config/model_profiles.yaml"))
        assert table is not None
        for model_key in models:
            entry = table.resolve("CODE", model_key) or table.resolve("CHAT", model_key)
            assert entry is not None, f"missing profile row for {model_key}"
