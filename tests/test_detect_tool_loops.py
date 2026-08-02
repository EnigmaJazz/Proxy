"""Regression tests for detect_tool_loops (agentic death-spiral detection).

The old implementation had an absolute role-limit check: if the TOTAL
number of tool calls in the conversation reached the domain limit, every
subsequent request stripped tools.  That broke every long agentic session —
once a conversation crossed N total calls (4 for professional, 3 for
standard), tools were stripped forever, the model degenerated into text
stubs and triage echoes, and the user saw the "loops with no output"
flood.  The detector must only fire on a CONSECUTIVE run of the same tool
with near-identical args.
"""

from __future__ import annotations

from typing import Any

import pytest

from routing import detect_tool_loops


def _assistant_with_calls(calls: list[tuple[str, str]]) -> dict[str, Any]:
    """Build an assistant message whose tool_calls carry name+arguments."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
            for i, (name, args) in enumerate(calls)
        ],
    }


def _messages_with_calls(calls: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """One assistant message per call, alternating with tool results."""
    messages: list[dict] = []
    for i, (name, args) in enumerate(calls):
        messages.append(_assistant_with_calls([(name, args)]))
        messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": "ok"})
    return messages


@pytest.mark.asyncio
async def test_long_diverse_conversation_is_not_a_loop() -> None:
    """Regression: 3+ total calls must NOT strip tools.

    A long conversation using many DIFFERENT tools (web_search, read_file,
    exec, apply_patch...) is legitimate.  The old role-limit check flagged
    it once the count crossed the domain limit.
    """
    calls = [
        ("web_search", '{"query": "weather london"}'),
        ("web_search", '{"query": "news today"}'),
        ("read_file", '{"path": "/tmp/a.py"}'),
        ("exec", '{"command": "ls"}'),
        ("apply_patch", '{"patch": "--- a\\n+++ b\\n"}'),
        ("edit_file", '{"path": "/tmp/b.py", "new_str": "x"}'),
        ("write_file", '{"path": "/tmp/c.py", "content": "y"}'),
    ]
    messages = _messages_with_calls(calls)

    looping, reason = await detect_tool_loops(messages, domain="standard")

    assert looping is False
    assert reason == ""


@pytest.mark.asyncio
async def test_consecutive_identical_calls_still_trigger() -> None:
    """The death spiral the detector exists for: same tool, same args, 3x."""
    calls = [
        ("read_file", '{"path": "/tmp/mem.md"}'),
        ("read_file", '{"path": "/tmp/mem.md"}'),
        ("read_file", '{"path": "/tmp/mem.md"}'),
    ]
    messages = _messages_with_calls(calls)

    looping, reason = await detect_tool_loops(messages, domain="standard")

    assert looping is True
    assert "read_file" in reason


@pytest.mark.asyncio
async def test_run_resets_on_different_tool() -> None:
    """Two identical calls followed by a different tool are NOT a loop."""
    calls = [
        ("read_file", '{"path": "/tmp/mem.md"}'),
        ("read_file", '{"path": "/tmp/mem.md"}'),
        ("exec", '{"command": "ls"}'),
    ]
    messages = _messages_with_calls(calls)

    looping, _ = await detect_tool_loops(messages, domain="standard")

    assert looping is False


@pytest.mark.asyncio
async def test_domain_limit_respected() -> None:
    """Scholar allows 6 repeats; 4 identical calls stay under it."""
    calls = [
        ("web_search", '{"query": "kinver history"}'),
        ("web_search", '{"query": "kinver history"}'),
        ("web_search", '{"query": "kinver history"}'),
        ("web_search", '{"query": "kinver history"}'),
    ]
    messages = _messages_with_calls(calls)

    looping, _ = await detect_tool_loops(messages, domain="scholar")

    assert looping is False


@pytest.mark.asyncio
async def test_similar_but_not_identical_args_counts_as_run() -> None:
    """Near-identical args (>=85% similarity) still count toward the run."""
    calls = [
        ("exec", '{"command": "cat /sys/class/drm/card0/device/mem_info_vram_total"}'),
        ("exec", '{"command": "cat /sys/class/drm/card0/device/mem_info_vram_total"}'),
        ("exec", '{"command": "cat /sys/class/drm/card0/device/mem_info_vram_total"}'),
    ]
    messages = _messages_with_calls(calls)

    looping, reason = await detect_tool_loops(messages, domain="standard")

    assert looping is True
    assert "exec" in reason
