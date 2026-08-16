"""Tests for scripts/monitor_local_model_tokens.py (token monitor robustness).

The monitor's slot-shape handling is exercised directly: llama.cpp reports
idle slots with ``next_token`` absent OR an empty list, and the renderer
must tolerate both (regression for the review finding R3-monitor-next-token-empty).
"""
from __future__ import annotations

import json

from scripts import monitor_local_model_tokens as mlm


def _slot(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 0,
        "is_processing": False,
        "next_token": [{"has_next_token": False, "n_remain": 12}],
        "n_prompt_tokens": 5,
        "n_decoded": 42,
    }
    base.update(overrides)
    return base


def test_render_json_tolerates_empty_next_token_list() -> None:
    """Regression: next_token=[] raised IndexError before the fix."""
    slots = [_slot(next_token=[])]
    out = mlm.render_snapshot(
        trackers={0: mlm.SlotTracker()},
        slots=slots,
        gen_delta=1,
        prompt_delta=0,
        gen_rate=1.0,
        prompt_rate=0.0,
        gen_cum=1,
        prompt_cum=0,
        elapsed=1.0,
        as_json=True,
    )
    line = json.loads(out)
    assert line["slots"][0]["remain"] is None
    assert line["slots"][0]["decoded"] == 42


def test_render_json_tolerates_missing_next_token() -> None:
    slots = [_slot(next_token=None)]
    out = mlm.render_snapshot(
        trackers={0: mlm.SlotTracker()},
        slots=slots,
        gen_delta=0,
        prompt_delta=0,
        gen_rate=0.0,
        prompt_rate=0.0,
        gen_cum=0,
        prompt_cum=0,
        elapsed=1.0,
        as_json=True,
    )
    line = json.loads(out)
    assert line["slots"][0]["remain"] is None


def test_render_human_tolerates_empty_next_token_list() -> None:
    """Regression: the human-readable branch had the same IndexError."""
    slots = [_slot(next_token=[])]
    out = mlm.render_snapshot(
        trackers={0: mlm.SlotTracker()},
        slots=slots,
        gen_delta=1,
        prompt_delta=0,
        gen_rate=1.0,
        prompt_rate=0.0,
        gen_cum=1,
        prompt_cum=0,
        elapsed=1.0,
        as_json=False,
    )
    assert "remain=None" in out


def test_render_human_keeps_remain_when_present() -> None:
    slots = [_slot()]
    out = mlm.render_snapshot(
        trackers={0: mlm.SlotTracker()},
        slots=slots,
        gen_delta=0,
        prompt_delta=0,
        gen_rate=0.0,
        prompt_rate=0.0,
        gen_cum=0,
        prompt_cum=0,
        elapsed=1.0,
        as_json=False,
    )
    assert "remain=12" in out
