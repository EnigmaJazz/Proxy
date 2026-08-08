"""Image-request routing: IMAGE intent → professional with the image profile.

Image requests must never reach the opencode bridge (its model has no
vision) or be misrouted to CODE by the text-only frontdesk classifier.
The proxy forces the IMAGE intent, routes to professional, and applies
the image sampling bucket (low temperature, no long CoT).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import constants
import profile_loader
import routes


class TestImageRouteMap:
    def test_image_routes_to_professional(self) -> None:
        """The IMAGE intent maps to the vision-capable model explicitly —
        not by the fallback."""
        assert constants.ROUTE_MAP.get("IMAGE") == "professional"
        assert "IMAGE" in constants.ROUTE_MAP


class TestImageProfileBucket:
    def test_bucket_image(self) -> None:
        assert profile_loader.bucket("IMAGE") == "image"
        assert profile_loader.bucket("image") == "image"

    def test_resolve_image_row(self) -> None:
        """The professional/image row carries vision-analysis sampling."""
        table = profile_loader.ModelProfileTable([
            {"model": "professional", "intent": "image",
             "temperature": 0.2, "top_p": 0.9,
             "thinking_budget_tokens": 0, "max_tokens": 4096},
        ])
        entry = table.resolve("IMAGE", "professional")
        assert entry is not None
        assert entry.intent == "image"
        assert entry.values["temperature"] == 0.2
        assert entry.values["top_p"] == 0.9
        assert entry.values["thinking_budget_tokens"] == 0

    def test_generated_profiles_contain_image_row(self) -> None:
        """The synced profile table carries a professional/image row."""
        import yaml
        data = yaml.safe_load(
            Path("config/model_profiles.yaml").read_text(encoding="utf-8"),
        )
        image_rows = [
            r for r in data["profiles"]
            if r.get("intent") == "image" and r.get("model") == "professional"
        ]
        assert image_rows, "professional/image row missing from model_profiles.yaml"
        assert image_rows[0]["temperature"] == 0.2
        assert image_rows[0]["top_p"] == 0.9
        assert image_rows[0]["thinking_budget_tokens"] == 0


class TestRequestHasImage:
    def test_openai_image_url_part(self) -> None:
        msg = [{"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ]}]
        assert routes._request_has_image(msg) is True

    def test_anthropic_image_part(self) -> None:
        msg = [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "data": "abc"}},
        ]}]
        assert routes._request_has_image(msg) is True

    def test_inline_base64_text(self) -> None:
        msg = [{"role": "user", "content": "look at data:image/png;base64,AAAA"}]
        assert routes._request_has_image(msg) is True

    def test_no_image(self) -> None:
        msg = [{"role": "user", "content": "plain text question"}]
        assert routes._request_has_image(msg) is False

    def test_image_in_tool_result(self) -> None:
        msg = [
            {"role": "user", "content": "check the tool"},
            {"role": "tool", "content": "data:image/jpeg;base64,BBBB"},
        ]
        assert routes._request_has_image(msg) is True
