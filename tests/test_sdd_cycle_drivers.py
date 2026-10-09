"""Tests for the SDD cycle driver scripts (REQ-6: parameterization + stalls).

The scripts are importable as modules (repo root is on sys.path via
conftest), so the argparse/parameterization and the stall-timer logic are
exercised directly without spawning a live opencode serve.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

import scripts.sdd_autonomous_cycle as autonomous_cycle
import scripts.sdd_bridge_cycle as bridge_cycle
from scripts.sdd_cycle_common import STALL_S, CycleStalled, guard_stall


def test_autonomous_cycle_parser_change() -> None:
    args = autonomous_cycle.build_parser().parse_args(["--change", "my-change"])
    assert args.change == "my-change"


def test_autonomous_cycle_parser_change_required() -> None:
    with pytest.raises(SystemExit):
        autonomous_cycle.build_parser().parse_args([])


def test_autonomous_task_uses_change_name() -> None:
    task = autonomous_cycle.build_task("my-change")
    assert "CHANGE NAME: my-change" in task
    assert "bridge-docs" not in task


def test_bridge_cycle_parser_change_and_desc() -> None:
    args = bridge_cycle.build_parser().parse_args(
        ["--change", "my-change", "--desc", "fix the thing"]
    )
    assert args.change == "my-change"
    assert args.desc == "fix the thing"


def test_bridge_cycle_parser_change_required() -> None:
    with pytest.raises(SystemExit):
        bridge_cycle.build_parser().parse_args([])


def test_stall_threshold_matches_design() -> None:
    assert STALL_S == 180.0


def test_parse_groups_extracts_questions_and_options() -> None:
    text = (
        "Options:\n"
        "Pace: What pace?\n"
        "Options: Automatic | Manual\n"
        "Artifacts: Which artifacts?\n"
        "Options: Engram | OpenSpec | Both\n"
    )
    groups = bridge_cycle.parse_groups(text)
    assert [(g.header, g.q, g.opts) for g in groups] == [
        ("Pace", "What pace?", ["Automatic", "Manual"]),
        ("Artifacts", "Which artifacts?", ["Engram", "OpenSpec", "Both"]),
    ]


def test_answer_for_matches_pref_answers() -> None:
    text = (
        "Pace: What pace?\n"
        "Options: Automatic | Manual\n"
        "Artifacts: Which artifacts?\n"
        "Options: Engram | OpenSpec | Both\n"
    )
    assert bridge_cycle.answer_for(text) == "Pace: Automatic; Artifacts: Both"


def test_answer_for_falls_back_to_first_option() -> None:
    text = "Budget: How many lines?\nOptions: 400 | 800"
    assert bridge_cycle.answer_for(text) == "Budget: 400"


def test_answer_for_continue_without_questions() -> None:
    assert bridge_cycle.answer_for("no questions here") == "continue"


@pytest.mark.asyncio
async def test_guard_stall_raises_on_silence() -> None:
    async def silent() -> AsyncIterator[tuple[str, str]]:
        await asyncio.sleep(10.0)
        yield ("text", "never")

    with pytest.raises(CycleStalled):
        async for _ in guard_stall(silent(), stall_s=0.05):
            pytest.fail("silent stream must yield nothing before stalling")


@pytest.mark.asyncio
async def test_guard_stall_passes_deltas_and_terminates() -> None:
    async def gen() -> AsyncIterator[tuple[str, str]]:
        yield ("status", "a")
        yield ("text", "b")

    out = [item async for item in guard_stall(gen(), stall_s=0.05)]
    assert out == [("status", "a"), ("text", "b")]


@pytest.mark.asyncio
async def test_guard_stall_delta_resets_the_timer() -> None:
    async def slow() -> AsyncIterator[tuple[str, str]]:
        yield ("status", "start")
        await asyncio.sleep(0.03)
        yield ("text", "still alive")
        await asyncio.sleep(10.0)
        yield ("text", "never")

    collected: list[str] = []
    with pytest.raises(CycleStalled):
        async for _, text in guard_stall(slow(), stall_s=0.05):
            collected.append(text)
    # The 0.03s gap is under the 0.05s threshold, so the second delta
    # arrives before the stall fires; only the final 10s silence stalls.
    assert collected == ["start", "still alive"]
