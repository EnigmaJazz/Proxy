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
from gguf import GGUFWriter

from profile_loader import (
    ModelProfileTable,
    ProfileEntry,
    bucket,
    load_model_profiles,
)
from tools.gguf_reader import GGUFMetadata, GGUFReadError, read_gguf_metadata
import tools.sync_model_profiles as sync_profiles


# ---------------------------------------------------------------------------
# R18 GGUF reader
# ---------------------------------------------------------------------------
def _write_synthetic_gguf(
    path: Path,
    *,
    arch: str = "llama",
    context_length: int | None = 32768,
    file_type: int = 15,
    name: str = "Synthetic Model",
) -> None:
    """Write a minimal valid GGUF file for testing."""
    writer = GGUFWriter(path, arch=arch)
    if context_length is not None:
        writer.add_context_length(context_length)
    writer.add_file_type(file_type)
    writer.add_name(name)
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()


class TestGGUFReader:
    """Unit-level behaviour of tools/gguf_reader.py."""

    def test_read_valid_file_returns_metadata(self, tmp_path: Path) -> None:
        """A valid GGUF file yields a populated GGUFMetadata dataclass."""
        gguf_path = tmp_path / "valid.gguf"
        _write_synthetic_gguf(gguf_path, arch="qwen2", context_length=131072, file_type=17)

        meta = read_gguf_metadata(gguf_path)
        assert meta.architecture == "qwen2"
        assert meta.context_length == 131072
        assert meta.file_type == 17
        assert meta.name == "Synthetic Model"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """A non-existent path raises GGUFReadError naming the file."""
        missing = tmp_path / "missing.gguf"
        with pytest.raises(GGUFReadError, match="not found"):
            read_gguf_metadata(missing)

    def test_missing_architecture_raises(self, tmp_path: Path) -> None:
        """Missing general.architecture is treated as a critical error."""
        gguf_path = tmp_path / "no-arch.gguf"
        _write_synthetic_gguf(gguf_path)

        with patch("tools.gguf_reader._decode_field", side_effect=lambda _r, key: None if key == "general.architecture" else "fallback"):
            with pytest.raises(GGUFReadError, match="general.architecture"):
                read_gguf_metadata(gguf_path)

    def test_missing_context_length_raises(self, tmp_path: Path) -> None:
        """Missing <arch>.context_length is treated as a critical error."""
        gguf_path = tmp_path / "no-ctx.gguf"
        _write_synthetic_gguf(gguf_path, arch="llama", context_length=32768)

        def _fake_decode(reader, key):
            if key == "llama.context_length":
                return None
            return read_gguf_metadata.__wrapped__  # not used; fallback below

        with patch("tools.gguf_reader._decode_field") as mock_decode:
            mock_decode.side_effect = lambda _r, key: {
                "general.architecture": "llama",
                "llama.context_length": None,
                "general.file_type": 15,
                "general.name": "Synthetic Model",
            }.get(key)
            with pytest.raises(GGUFReadError, match="llama.context_length"):
                read_gguf_metadata(gguf_path)


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
    """Unit-level behaviour of the scanner helpers."""

    def test_derive_max_tokens_applies_headroom(self) -> None:
        """max_tokens is derived as ``int(context_window * 0.9)``."""
        assert sync_profiles.derive_max_tokens(32768) == 29491
        assert sync_profiles.derive_max_tokens(1000) == 900

    def test_parse_sampling_params_extracts_temperature_and_top_p(self) -> None:
        """Temperature and top_p are read from the sampling source content."""
        content = "we use a temperature of 0.6, top-p value of 0.95"
        params = sync_profiles.parse_sampling_params(content, "test")
        assert params is not None
        assert params["temperature"] == 0.6
        assert params["top_p"] == 0.95

    def test_parse_sampling_params_ignores_context_window(self) -> None:
        """Context window is no longer sourced from HF; only sampling keys matter."""
        content = "max position embeddings is 32768, temperature: 0.5"
        params = sync_profiles.parse_sampling_params(content, "test")
        assert params is not None
        assert "context_window" not in params
        assert params["temperature"] == 0.5

    def test_parse_sampling_params_returns_empty_for_no_sampling_keys(self) -> None:
        """A parseable source with no sampling keys yields an empty dict."""
        content = "this page has no sampling parameters at all"
        assert sync_profiles.parse_sampling_params(content, "test") == {}

    def test_parse_sampling_params_returns_none_for_empty_content(self) -> None:
        """An unreadable / empty source returns None so caller falls back."""
        assert sync_profiles.parse_sampling_params(None, "test") is None
        assert sync_profiles.parse_sampling_params("", "test") is None

    def test_build_profile_entry_uses_hf_sampling(self) -> None:
        """HF temperature/top_p are applied when present."""
        meta = GGUFMetadata(architecture="qwen2", context_length=32768, file_type=15, name="Q")
        row = sync_profiles.build_profile_entry(
            "coder", meta, {"overrides": {}}, {"temperature": 0.1, "top_p": 0.9}
        )
        assert row["temperature"] == 0.1
        assert row["top_p"] == 0.9
        assert row["architecture"] == "qwen2"
        assert row["context_window"] == 32768
        assert row["max_tokens"] == sync_profiles.derive_max_tokens(32768)

    def test_build_profile_entry_uses_intent_defaults_when_hf_unparseable(self) -> None:
        """Unparseable HF card falls back to intent defaults for sampling."""
        meta = GGUFMetadata(architecture="qwen2", context_length=32768, file_type=15, name="Q")
        row = sync_profiles.build_profile_entry("coder", meta, {"overrides": {}}, None)
        # coder maps to intent=code → intent_defaults provides temp/top_p
        assert row["temperature"] == 0.2
        assert row["top_p"] == 0.95
        assert row["thinking_budget_tokens"] == 4096
        assert row["architecture"] == "qwen2"
        assert row["max_tokens"] == sync_profiles.derive_max_tokens(32768)

    def test_build_profile_entry_overrides_win(self) -> None:
        """Per-entry overrides take precedence over HF and GGUF-derived values."""
        meta = GGUFMetadata(architecture="qwen2", context_length=32768, file_type=15, name="Q")
        row = sync_profiles.build_profile_entry(
            "coder", meta, {"overrides": {"temperature": 0.99, "max_tokens": 1234}}, {"temperature": 0.1}
        )
        assert row["temperature"] == 0.99
        assert row["max_tokens"] == 1234


class TestLLMExtractor:
    """LLM extractor fallback when the regex parser doesn't extract params."""

    def _mock_client(self, response_json: dict[str, Any]) -> Any:
        """Return a mock httpx.Client whose .post() yields *response_json*."""
        import httpx

        class _Resp:
            def __init__(self) -> None:
                self._json = response_json

            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, Any]:
                return self._json

        class _Client:
            def post(self, url: str, json: dict[str, Any], timeout: float = 0) -> _Resp:
                return _Resp()

        return _Client()

    def test_extract_with_llm_returns_parsed_params(self) -> None:
        """A well-formed JSON response yields temperature + top_p."""
        content = "some messy documentation with no clear pattern"
        client = self._mock_client({
            "choices": [{"message": {"content": '{"temperature": 0.7, "top_p": 0.9}'}}]
        })
        params = sync_profiles.extract_with_llm(content, "https://example.com/x", client, "coder")
        assert params == {"temperature": 0.7, "top_p": 0.9}

    def test_extract_with_llm_returns_none_for_empty_dict(self) -> None:
        """An empty JSON object means 'no params found' — caller falls back."""
        client = self._mock_client({
            "choices": [{"message": {"content": "{}"}}]
        })
        params = sync_profiles.extract_with_llm("text", "test", client, "coder")
        assert params is None

    def test_extract_with_llm_returns_none_on_http_failure(self) -> None:
        """Network errors return None so the caller falls back to intent defaults."""
        import httpx

        class _FailingClient:
            def post(self, url: str, json: dict[str, Any], timeout: float = 0) -> None:
                raise httpx.ConnectError("proxy down")

        params = sync_profiles.extract_with_llm("text", "test", _FailingClient(), "coder")
        assert params is None

    def test_build_profiles_falls_back_to_llm_when_regex_empty(
        self, tmp_path: Path
    ) -> None:
        """Integration: when the regex returns nothing, the LLM extractor runs."""
        gguf_path = tmp_path / "test.gguf"
        _write_synthetic_gguf(gguf_path, context_length=10000)

        models = tmp_path / "local_models.yaml"
        models.write_text(
            f"models:\n"
            f"  coder:\n"
            f"    path: {gguf_path}\n"
            f"    sampling_source: https://example.com/coder\n"
        )

        # Fetcher returns content that the regex won't parse.
        def _fetch(source: str, client: Any) -> str:
            return "documentation that mentions nothing about temperature or top_p"

        # Extractor returns clean params.
        def _extract(content: str, source: str, client: Any, extractor_model: str) -> dict[str, Any]:
            return {"temperature": 0.3, "top_p": 0.85}

        data = sync_profiles.build_profiles(
            models, fetcher=_fetch, extractor=_extract, extractor_model="coder"
        )
        row = next(r for r in data["profiles"] if r.get("model") == "coder")
        assert row["temperature"] == 0.3
        assert row["top_p"] == 0.85


class TestScannerSchemaValidation:
    """local_models.yaml schema enforcement."""

    def test_missing_path_is_config_error(self, tmp_path: Path) -> None:
        """An entry without 'path' raises ValueError → exit 2."""
        models = tmp_path / "local_models.yaml"
        models.write_text("models:\n  badmodel:\n    hf_model_id: org/model\n")
        with pytest.raises(ValueError, match="missing required 'path'"):
            sync_profiles.load_local_models(models)

    def test_sampling_source_is_optional(self, tmp_path: Path) -> None:
        """An entry without 'sampling_source' loads fine — intent defaults cover the gap."""
        gguf_path = tmp_path / "model.gguf"
        _write_synthetic_gguf(gguf_path, context_length=10000)
        models = tmp_path / "local_models.yaml"
        models.write_text(f"models:\n  goodmodel:\n    path: {gguf_path}\n")
        loaded = sync_profiles.load_local_models(models)
        assert "goodmodel" in loaded
        assert loaded["goodmodel"]["sampling_source"] is None
        assert loaded["goodmodel"]["overrides"] == {}

    def test_flat_value_is_config_error(self, tmp_path: Path) -> None:
        """The old flat 'model_key: hf_model_id' shape is rejected."""
        models = tmp_path / "local_models.yaml"
        models.write_text("models:\n  badmodel: org/model\n")
        with pytest.raises(ValueError, match="mapping with 'path'"):
            sync_profiles.load_local_models(models)


class TestScannerFileErrors:
    """Declared file and GGUF critical-field errors."""

    def test_missing_file_aborts_sync(self, tmp_path: Path) -> None:
        """A declared path that does not exist raises GGUFReadError."""
        models = tmp_path / "local_models.yaml"
        models.write_text("models:\n  testmodel:\n    path: /tmp/ghost-file-that-does-not-exist.gguf\n    sampling_source: https://example.com/x\n")
        with pytest.raises(GGUFReadError, match="not found"):
            sync_profiles.build_profiles(models, fetcher=lambda _h, _c: "")

    def test_missing_critical_field_aborts_sync(self, tmp_path: Path) -> None:
        """Missing context_length raises GGUFReadError."""
        gguf_path = tmp_path / "no-ctx.gguf"
        _write_synthetic_gguf(gguf_path, arch="llama")

        models = tmp_path / "local_models.yaml"
        models.write_text(f"models:\n  testmodel:\n    path: {gguf_path}\n    sampling_source: https://example.com/x\n")

        with patch("tools.sync_model_profiles.read_gguf_metadata") as mock_read:
            mock_read.side_effect = GGUFReadError("Missing llama.context_length")
            with pytest.raises(GGUFReadError, match="context_length"):
                sync_profiles.build_profiles(models, fetcher=lambda _h, _c: "")


class TestScannerHFUnparseable:
    """Unparseable HF card handling."""

    def test_unparseable_sampling_source_is_skipped_and_logged(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed sampling-source fetch logs a warning but the row is still emitted."""
        gguf_path = tmp_path / "test.gguf"
        _write_synthetic_gguf(gguf_path, context_length=10000)

        models = tmp_path / "local_models.yaml"
        models.write_text(f"models:\n  testmodel:\n    path: {gguf_path}\n    sampling_source: https://example.com/bad\n")

        def _bad_fetch(source: str, client: Any) -> None:
            return None

        with caplog.at_level(logging.WARNING):
            data = sync_profiles.build_profiles(models, fetcher=_bad_fetch)

        assert any(r.get("model") == "testmodel" for r in data["profiles"])
        assert "unparseable" in caplog.text.lower() or "could not fetch" in caplog.text.lower()

    def test_missing_sampling_source_uses_intent_defaults(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An entry with no sampling_source logs an info note and uses intent defaults."""
        gguf_path = tmp_path / "test.gguf"
        _write_synthetic_gguf(gguf_path, context_length=10000)

        # Use 'coder' so the model_key maps to intent=code via _MODEL_INTENTS.
        models = tmp_path / "local_models.yaml"
        models.write_text(f"models:\n  coder:\n    path: {gguf_path}\n")

        with caplog.at_level(logging.INFO):
            data = sync_profiles.build_profiles(models, fetcher=lambda _s, _c: "")

        row = next(r for r in data["profiles"] if r.get("model") == "coder")
        # coder maps to intent=code → intent_defaults
        assert row["temperature"] == 0.2
        assert row["top_p"] == 0.95
        assert "intent defaults" in caplog.text.lower()


class TestSyncCheckDrift:
    """Scanner CLI drift detection and exit-code contract."""

    @pytest.fixture
    def tmp_models(self, tmp_path: Path) -> Path:
        """Return a local_models.yaml with one synthetic model."""
        gguf_path = tmp_path / "test.gguf"
        _write_synthetic_gguf(gguf_path, context_length=10000)
        models = tmp_path / "local_models.yaml"
        models.write_text(
            f"models:\n"
            f"  testmodel:\n"
            f"    path: {gguf_path}\n"
            f"    sampling_source: https://example.com/testmodel\n"
        )
        return models

    def fake_fetcher(self, context_window: int = 10000) -> Any:
        """Return a fetcher that serves a synthetic sampling-source document."""
        def _fetch(source: str, client: Any) -> str:
            return "we recommend temperature: 0.5, top-p: 0.9"
        return _fetch

    def test_check_exits_zero_when_in_sync(self, tmp_path: Path, tmp_models: Path) -> None:
        """sync --check returns 0 when the committed file matches generated data."""
        profiles = tmp_path / "model_profiles.yaml"
        fetcher = self.fake_fetcher()
        data = sync_profiles.build_profiles(tmp_models, fetcher=fetcher)
        sync_profiles.write_profiles(profiles, data)
        assert sync_profiles.check_profiles(tmp_models, profiles, fetcher=fetcher) == 0

    def test_check_exits_one_on_drift(self, tmp_path: Path, tmp_models: Path) -> None:
        """sync --check returns 1 when the committed file is stale."""
        profiles = tmp_path / "model_profiles.yaml"
        fetcher = self.fake_fetcher()
        data = sync_profiles.build_profiles(tmp_models, fetcher=fetcher)
        sync_profiles.write_profiles(profiles, data)

        # Introduce drift.
        committed = yaml.safe_load(profiles.read_text())
        committed["profiles"][0]["max_tokens"] = 12345
        profiles.write_text(yaml.safe_dump(committed))

        assert sync_profiles.check_profiles(tmp_models, profiles, fetcher=fetcher) == 1

    def test_check_exits_two_on_malformed_config(self, tmp_path: Path) -> None:
        """sync --check returns 2 when local_models.yaml is invalid."""
        bad_models = tmp_path / "bad_models.yaml"
        bad_models.write_text("not a valid mapping: [")
        profiles = tmp_path / "model_profiles.yaml"
        profiles.write_text("profiles: []\noverrides: {}\n")
        assert sync_profiles.check_profiles(bad_models, profiles) == 2

    def test_check_exits_three_on_missing_file(self, tmp_path: Path) -> None:
        """sync --check returns 3 when a declared GGUF file is missing."""
        models = tmp_path / "local_models.yaml"
        models.write_text(
            "models:\n"
            "  testmodel:\n"
            "    path: /tmp/ghost-file-that-does-not-exist.gguf\n"
            "    hf_model_id: org/testmodel\n"
        )
        profiles = tmp_path / "model_profiles.yaml"
        profiles.write_text("profiles: []\noverrides: {}\n")
        assert sync_profiles.check_profiles(models, profiles) == 3


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
