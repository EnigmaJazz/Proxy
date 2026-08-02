"""Tests for the Qwen ``<tool_call>`` text → OpenAI structured
``delta.tool_calls`` state machine."""
from __future__ import annotations

import json
import pytest

from text_to_structured import (
    ToolCallTextToStructured,
    _format_status,
    _parse_tool_call_inner,
)


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------

class TestParseToolCallInner:
    def test_parses_function_name_and_args(self) -> None:
        text = (
            "<function=exec_shell>\n"
            "<parameter=command>sensors</parameter>\n"
            "</function>"
        )
        result = _parse_tool_call_inner(text, "call_1")
        assert result is not None
        assert result["function"]["name"] == "exec_shell"
        args = json.loads(result["function"]["arguments"])
        assert args == {"command": "sensors"}

    def test_parses_multiple_parameters(self) -> None:
        text = (
            "<function=web_search>\n"
            "<parameter=query>Qwen 3.5</parameter>\n"
            "<parameter>num_results</parameter>\n"  # bare "<parameter>" without =
            "<parameter=format>json</parameter>\n"
            "</function>"
        )
        # The regex requires "=" inside the tag, so bare <parameter> won't match.
        # Only the two = ones are captured. This is a known limitation
        # of the parser.
        result = _parse_tool_call_inner(text, "call_1")
        assert result is not None
        args = json.loads(result["function"]["arguments"])
        assert args == {"query": "Qwen 3.5", "format": "json"}

    def test_parses_json_typed_values(self) -> None:
        text = (
            "<function=configure>\n"
            "<parameter=count>42</parameter>\n"
            "<parameter=ratio>0.5</parameter>\n"
            "<parameter=active>true</parameter>\n"
            "</function>"
        )
        result = _parse_tool_call_inner(text, "call_1")
        assert result is not None
        args = json.loads(result["function"]["arguments"])
        assert args == {"count": 42, "ratio": 0.5, "active": True}

    def test_returns_none_without_function_tag(self) -> None:
        text = "<parameter=command>ls</parameter>"  # no <function=...>
        result = _parse_tool_call_inner(text, "call_1")
        assert result is None

    def test_preserves_multiline_argument_value(self) -> None:
        text = (
            "<function=write_file>\n"
            "<parameter=content>line 1\nline 2\nline 3</parameter>\n"
            "</function>"
        )
        result = _parse_tool_call_inner(text, "call_1")
        assert result is not None
        args = json.loads(result["function"]["arguments"])
        assert args == {"content": "line 1\nline 2\nline 3"}


# ---------------------------------------------------------------------------
# Status formatter
# ---------------------------------------------------------------------------

class TestFormatStatus:
    """The status message is what shows in Telegram before each tool
    call.  Custom formats for the local tools (exec, read_file, etc.)
    keep it one line of useful info; the generic fallback handles
    unknown tool names."""

    def test_exec_uses_command_in_code_block(self) -> None:
        tc = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "exec",
                "arguments": json.dumps({"command": "sensors 2>/dev/null"}),
            },
        }
        status = _format_status(tc)
        assert status == "🔧 exec: `sensors 2>/dev/null`"

    def test_exec_truncates_long_commands(self) -> None:
        long_cmd = "echo " + "x" * 500
        tc = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "exec",
                "arguments": json.dumps({"command": long_cmd}),
            },
        }
        status = _format_status(tc)
        assert "…" in status
        assert len(status) < 250

    def test_web_search_uses_query(self) -> None:
        tc = {
            "function": {
                "name": "web_search",
                "arguments": json.dumps({"query": "Qwen 3.5"}),
            },
        }
        status = _format_status(tc)
        assert 'web_search' in status
        assert "Qwen 3.5" in status

    def test_read_file_uses_path(self) -> None:
        tc = {
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": "/etc/zramswap"}),
            },
        }
        status = _format_status(tc)
        assert "read_file" in status
        assert "/etc/zramswap" in status

    def test_generic_fallback_for_unknown_tool(self) -> None:
        tc = {
            "function": {
                "name": "unknown_tool",
                "arguments": json.dumps({"arg": "value"}),
            },
        }
        status = _format_status(tc)
        assert "unknown_tool" in status
        assert "arg" in status

    def test_handles_malformed_arguments(self) -> None:
        # Arguments that don't parse as JSON fall back to a simple
        # "name(args)" format.
        tc = {
            "function": {
                "name": "exec",
                "arguments": "not valid json",
            },
        }
        status = _format_status(tc)
        assert "exec" in status
        assert "not valid json" in status


# ---------------------------------------------------------------------------
# State machine: streaming behavior
# ---------------------------------------------------------------------------

class TestFeedSingleChunk:
    def test_passthrough_text_without_tool_call(self) -> None:
        sm = ToolCallTextToStructured()
        emits = sm.feed("hello world")
        assert emits == [{"content": "hello world"}]

    def test_passthrough_with_partial_open_tag_at_end(self) -> None:
        sm = ToolCallTextToStructured()
        # "<tool_call>" is the open tag. If the buffer ends with "<t" (a
        # partial prefix), we must hold it back to avoid emitting the
        # wrong thing.
        emits = sm.feed("hello <t")
        assert emits == []  # nothing emitted; partial is held in buffer

    def test_passthrough_with_complete_open_tag_waits_for_close(self) -> None:
        sm = ToolCallTextToStructured()
        # The pre-content "hello " is valid text and is emitted
        # immediately.  The in-tool-call content (open tag + inner) is
        # held back until the close tag arrives.
        emits = sm.feed("hello <tool_call><function=foo><parameter=x>1</parameter></function>")
        assert emits == [{"content": "hello "}]

    def test_emits_full_tool_call_in_one_chunk(self) -> None:
        sm = ToolCallTextToStructured()
        text = (
            "<tool_call>"
            "<function=exec_shell>"
            "<parameter=command>ls</parameter>"
            "</function>"
            "</tool_call>"
        )
        emits = sm.feed(text)
        # Expect: a status content chunk + the structured tool call.
        assert len(emits) == 2
        assert "content" in emits[0]
        assert "exec" in emits[0]["content"] or "ls" in emits[0]["content"]
        assert "tool_calls" in emits[1]
        tc = emits[1]["tool_calls"][0]
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "exec_shell"
        assert json.loads(tc["function"]["arguments"]) == {"command": "ls"}


class TestFeedMultiChunk:
    def test_tool_call_split_across_two_chunks(self) -> None:
        sm = ToolCallTextToStructured()
        # Chunk 1: open tag + start of inner
        emits_1 = sm.feed("<tool_call><function=exec_shell><parameter=command>")
        assert emits_1 == []  # buffered
        # Chunk 2: rest + close tag
        emits_2 = sm.feed("ls</parameter></function></tool_call>")
        # Expect: status + tool_call
        assert len(emits_2) == 2
        assert "content" in emits_2[0]
        tc = emits_2[1]["tool_calls"][0]
        assert tc["function"]["name"] == "exec_shell"
        assert json.loads(tc["function"]["arguments"]) == {"command": "ls"}

    def test_content_before_and_after_tool_call(self) -> None:
        sm = ToolCallTextToStructured()
        # Pre-content
        emits = sm.feed("Let me check. ")
        assert emits == [{"content": "Let me check. "}]
        # Open + inner
        emits = sm.feed(
            "<tool_call><function=exec_shell><parameter=command>ls</parameter></function>"
        )
        assert emits == []
        # Close
        emits = sm.feed("</tool_call>")
        # Expect: status + tool_call
        assert len(emits) == 2
        assert "content" in emits[0]
        assert "tool_calls" in emits[1]
        # Post-content
        emits = sm.feed(" Done.")
        assert emits == [{"content": " Done."}]

    def test_two_tool_calls_in_one_response(self) -> None:
        sm = ToolCallTextToStructured()
        text = (
            "<tool_call><function=a><parameter=x>1</parameter></function></tool_call>"
            "<tool_call><function=b><parameter=y>2</parameter></function></tool_call>"
        )
        emits = sm.feed(text)
        # Expect: status(a) + tool_call(a) + status(b) + tool_call(b)
        assert len(emits) == 4
        assert "content" in emits[0]
        assert emits[1]["tool_calls"][0]["function"]["name"] == "a"
        assert "content" in emits[2]
        assert emits[3]["tool_calls"][0]["function"]["name"] == "b"
        assert json.loads(emits[1]["tool_calls"][0]["function"]["arguments"]) == {"x": 1}
        assert json.loads(emits[3]["tool_calls"][0]["function"]["arguments"]) == {"y": 2}

    def test_two_tool_calls_get_distinct_indices(self) -> None:
        """Consecutive text tool calls must carry distinct per-stream
        indices.  OpenAI clients (nanobot-ai) accumulate
        ``delta.tool_calls`` by ``index``; a hardcoded index of 0 would
        merge every converted call into one buffer and concatenate
        their argument strings (the "got str" retry loop)."""
        sm = ToolCallTextToStructured()
        text = (
            "<tool_call><function=a><parameter=x>1</parameter></function></tool_call>"
            "<tool_call><function=b><parameter=y>2</parameter></function></tool_call>"
        )
        emits = sm.feed(text)
        calls = [e["tool_calls"][0] for e in emits if "tool_calls" in e]
        assert [c["index"] for c in calls] == [0, 1]
        assert calls[0]["id"] != calls[1]["id"]
        assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1}
        assert json.loads(calls[1]["function"]["arguments"]) == {"y": 2}

    def test_parse_inner_index_parameter(self) -> None:
        text = "<function=a><parameter=x>1</parameter></function>"
        assert _parse_tool_call_inner(text, "call_1", 3)["index"] == 3
        assert _parse_tool_call_inner(text, "call_1")["index"] == 0

    def test_tool_call_split_at_partial_prefix(self) -> None:
        """The model might emit chunks that split inside the open tag
        itself: e.g. chunk 1 ends with '<t' and chunk 2 starts with
        'ool_call>'.  The state machine must hold the partial prefix."""
        sm = ToolCallTextToStructured()
        # Chunk 1 ends with partial prefix
        emits_1 = sm.feed("Let me check <t")
        assert emits_1 == []  # partial prefix held
        # Chunk 2 completes the tag and the tool call
        emits_2 = sm.feed(
            "ool_call><function=foo><parameter=x>1</parameter></function></tool_call>"
        )
        # The state machine emits the pre-content ("Let me check "),
        # then a status message, then the structured tool call.
        assert len(emits_2) == 3
        assert emits_2[0] == {"content": "Let me check "}
        assert "content" in emits_2[1]  # status message
        assert emits_2[2]["tool_calls"][0]["function"]["name"] == "foo"

    def test_partial_open_at_chunk_boundary(self) -> None:
        """Chunk 1: '<tool_call>' (complete open tag, no close yet)."""
        sm = ToolCallTextToStructured()
        emits_1 = sm.feed("<tool_call><function=foo>")
        assert emits_1 == []  # no close, nothing emitted
        emits_2 = sm.feed("<parameter=x>1</parameter></function></tool_call>")
        # Expect: status + tool_call
        assert len(emits_2) == 2
        assert "content" in emits_2[0]  # status
        assert emits_2[1]["tool_calls"][0]["function"]["name"] == "foo"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestFlush:
    def test_flush_emits_remaining_buffer_as_content(self) -> None:
        # Flush emits whatever is held in the buffer at end of stream.
        # Here we feed a chunk that puts the state machine into
        # "in tool call" mode (open tag seen, no close tag), so the
        # inner content is held.  Flush then emits the held buffer as
        # content (a tool call that was started but never closed).
        sm = ToolCallTextToStructured()
        sm.feed("<tool_call><function=foo>")  # open tag, no close
        # feed() above should have produced no emissions (waiting for close).
        # The open tag has been stripped from the buffer; only the
        # inner content remains.
        emits = sm.flush()
        # The held buffer is emitted as raw content (best-effort recovery).
        assert len(emits) == 1
        assert emits[0] == {"content": "<function=foo>"}

    def test_flush_drops_partial_open_prefix(self) -> None:
        """If the stream ends with a partial '<tool_call>' prefix that
        never received a close tag, it was probably a false positive
        in the model output.  Drop it to avoid emitting bogus content."""
        sm = ToolCallTextToStructured()
        sm.feed("text <t")
        emits = sm.flush()
        assert emits == []

    def test_flush_empty_buffer(self) -> None:
        sm = ToolCallTextToStructured()
        assert sm.flush() == []


class TestMalformedInput:
    def test_no_function_tag_falls_back_to_content(self) -> None:
        sm = ToolCallTextToStructured()
        text = (
            "<tool_call>"
            "<parameter=command>ls</parameter>"  # missing <function=...>
            "</tool_call>"
        )
        emits = sm.feed(text)
        assert len(emits) == 1
        # Malformed → emitted as raw content
        assert emits[0] == {"content": text}

    def test_close_without_open_is_passthrough(self) -> None:
        sm = ToolCallTextToStructured()
        emits = sm.feed("some text</tool_call>more text")
        # No open tag found, everything is content.
        assert emits == [{"content": "some text</tool_call>more text"}]


# ---------------------------------------------------------------------------
# Smoke test: the real bug pattern from nanobot-ai
# ---------------------------------------------------------------------------

class TestRealWorldPattern:
    """Reproduce the exact bug the user reported: on turn 4 of a
    multi-turn tool-calling flow, Professional emits:

        let me check the value
        <tool_call>
        <function=exec_shell>
        <parameter=command>cat /etc/default/zramswap</parameter>
        </function>
        </tool_call>

    The state machine must convert this to a structured delta.tool_calls
    chunk with no text leakage.
    """

    def test_bug_pattern_converted_to_structured(self) -> None:
        sm = ToolCallTextToStructured()
        # Simulate the chunks as they would arrive from the model.
        # The text arrives in small chunks.
        chunks = [
            "let me check the value\n",
            "<tool_call>",
            "\n<function=exec_shell>",
            "\n<parameter=command>cat /etc/default/zramswap</parameter>",
            "\n</function>",
            "\n</tool_call>",
        ]
        all_emits: list = []
        for chunk in chunks:
            all_emits.extend(sm.feed(chunk))
        all_emits.extend(sm.flush())

        # The structured tool call should be present, with NO text-based
        # <tool_call> XML in the content.
        tool_call_emits = [e for e in all_emits if "tool_calls" in e]
        assert len(tool_call_emits) == 1
        tc = tool_call_emits[0]["tool_calls"][0]
        assert tc["function"]["name"] == "exec_shell"
        assert json.loads(tc["function"]["arguments"]) == {
            "command": "cat /etc/default/zramswap"
        }

        # No content chunk should contain the <tool_call> XML text.
        for emit in all_emits:
            if "content" in emit:
                assert "<tool_call>" not in emit["content"]
                assert "</tool_call>" not in emit["content"]

        # The "let me check the value" pre-text should be in a content chunk.
        pre_text = "".join(
            e["content"] for e in all_emits if "content" in e
        )
        assert "let me check the value" in pre_text

        # A status message for the tool call should be present, emitted
        # BEFORE the structured tool call.
        status_emits = [
            e
            for e in all_emits
            if "content" in e
            and ("exec" in e["content"] or "zram" in e["content"])
        ]
        assert len(status_emits) >= 1
        # The status must come before the tool call in the emit order.
        last_status_idx = max(
            i for i, e in enumerate(all_emits) if e is status_emits[-1]
        )
        first_tool_call_idx = next(
            i for i, e in enumerate(all_emits) if "tool_calls" in e
        )
        assert last_status_idx < first_tool_call_idx
