"""Tests for scripts/monitor_local_model_tokens.py (token monitor robustness).

The monitor's slot-shape handling is exercised directly: llama.cpp reports
idle slots with ``next_token`` absent OR an empty list, and the renderer
must tolerate both (regression for the review finding R3-monitor-next-token-empty).
"""
from __future__ import annotations

import json
from typing import Optional

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

# -- SlotTracker behavioral coverage (F2) -----------------------------------

def _slot_with_counters(
    decoded: int, prompt: int, task: Optional[int] = None,
) -> dict[str, object]:
    """Helper: minimal slot dict for SlotTracker.observe."""
    d: dict[str, object] = {"n_decoded": decoded, "n_prompt_tokens": prompt}
    if task is not None:
        d["id_task"] = task
    return d


def test_tracker_first_observation_sets_baseline_without_delta() -> None:
    t = mlm.SlotTracker()
    t.observe(_slot_with_counters(decoded=10, prompt=5, task=1))
    assert t.decoded == 10
    assert t.prompt == 5
    assert t.task == 1
    assert t.gen_delta == 0
    assert t.prompt_delta == 0
    assert t.gen_total == 0
    assert t.prompt_total == 0


def test_tracker_positive_generation_and_prompt_deltas() -> None:
    t = mlm.SlotTracker()
    t.observe(_slot_with_counters(decoded=10, prompt=5, task=1))
    t.observe(_slot_with_counters(decoded=15, prompt=8, task=1))
    assert t.gen_delta == 5
    assert t.prompt_delta == 3
    assert t.gen_total == 5
    assert t.prompt_total == 3
    # Another increment accumulates in window, totals grow cumulatively.
    t.observe(_slot_with_counters(decoded=20, prompt=10, task=1))
    assert t.gen_delta == 10  # 5 + 5
    assert t.prompt_delta == 5  # 3 + 2
    assert t.gen_total == 10
    assert t.prompt_total == 5


def test_tracker_counter_decreases_do_not_produce_negative_deltas() -> None:
    t = mlm.SlotTracker()
    t.observe(_slot_with_counters(decoded=20, prompt=10, task=1))
    t.observe(_slot_with_counters(decoded=15, prompt=8, task=1))
    # Decrease must be ignored — no negative delta, totals unchanged.
    assert t.gen_delta == 0
    assert t.prompt_delta == 0
    assert t.gen_total == 0
    assert t.prompt_total == 0
    # Baseline moves to the lower value; a later increase counts from there.
    t.observe(_slot_with_counters(decoded=18, prompt=9, task=1))
    assert t.gen_delta == 3
    assert t.prompt_delta == 1
    assert t.gen_total == 3
    assert t.prompt_total == 1
    # Reset window clears deltas but not totals.
    t.reset_window()
    assert t.gen_delta == 0
    assert t.prompt_delta == 0
    assert t.gen_total == 3
    assert t.prompt_total == 1


def test_tracker_task_change_resets_window_baseline() -> None:
    t = mlm.SlotTracker()
    t.observe(_slot_with_counters(decoded=10, prompt=5, task=1))
    t.observe(_slot_with_counters(decoded=20, prompt=10, task=1))
    assert t.gen_delta == 10
    assert t.prompt_delta == 5
    # Task changes: gen delta reset, no accumulation on that poll.
    t.observe(_slot_with_counters(decoded=2, prompt=3, task=2))
    assert t.gen_delta == 0
    # Prompt delta on task-change poll still applies (prompt branch is not gated by task).
    # 3 - 10 = -7 → no positive delta, so prompt_delta also reset to 0.
    assert t.prompt_delta == 0
    # Totals unchanged on the task-change poll.
    assert t.gen_total == 10
    assert t.prompt_total == 5
    # Next poll with same new task accumulates from new baseline.
    t.observe(_slot_with_counters(decoded=7, prompt=9, task=2))
    assert t.gen_delta == 5  # 7 - 2
    assert t.prompt_delta == 6  # 9 - 3
    assert t.gen_total == 15
    assert t.prompt_total == 11
    assert t.decoded == 7
    assert t.task == 2


def test_tracker_task_change_with_prompt_increase() -> None:
    """Task change where prompt grows shows the split handling: gen reset, prompt still counts."""
    t = mlm.SlotTracker()
    t.observe(_slot_with_counters(decoded=10, prompt=5, task=1))
    t.observe(_slot_with_counters(decoded=12, prompt=15, task=2))
    # New task → gen_delta reset, no gen accumulation
    assert t.gen_delta == 0
    assert t.gen_total == 0
    # Prompt: 15 - 5 = 10 → still counted (prompt branch not suppressed)
    assert t.prompt_delta == 10
    assert t.prompt_total == 10


def test_tracker_cumulative_totals_across_many_polls() -> None:
    t = mlm.SlotTracker()
    t.observe(_slot_with_counters(decoded=0, prompt=0, task=1))
    for expected_total, decoded in enumerate([5, 9, 12, 20], start=1):
        prev_total = t.gen_total
        t.observe(_slot_with_counters(decoded=decoded, prompt=0, task=1))
        # Total grows monotonically with each positive step
        assert t.gen_total > prev_total
    assert t.gen_total == 20
    # Window delta accumulates until reset_window
    assert t.gen_delta == 20
    t.reset_window()
    assert t.gen_delta == 0
    assert t.gen_total == 20
    # Further polls continue accumulating totals even after reset
    t.observe(_slot_with_counters(decoded=25, prompt=0, task=1))
    assert t.gen_delta == 5
    assert t.gen_total == 25


def test_tracker_handles_next_token_fallback() -> None:
    """_decoded fallback: n_decoded absent but next_token carries it."""
    t = mlm.SlotTracker()
    t.observe({"next_token": [{"n_decoded": 10}], "n_prompt_tokens": 0})  # type: ignore[dict-item]
    assert t.decoded == 10
    t.observe({"next_token": [{"n_decoded": 15}], "n_prompt_tokens": 0})  # type: ignore[dict-item]
    assert t.gen_delta == 5
    assert t.gen_total == 5
