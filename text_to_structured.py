"""State machine for converting Qwen-style text tool calls to OpenAI
structured ``delta.tool_calls``.

The Qwen 3.5 chat template (and the fixed v21 template at
``~/kinver-hub/config/qwen-fixed.jinja``) instructs the model
to emit tool calls in the XML text format:

    <tool_call>
    <function=NAME>
    <parameter=KEY1>VALUE1</parameter>
    <parameter=KEY2>VALUE2</parameter>
    </function>
    </tool_call>

This is what the model was trained to produce, and the model follows
this instruction even on turn 4+ of multi-turn flows.  OpenAI-compatible
clients (nanobot-ai, etc.) expect structured ``delta.tool_calls`` JSON
instead.  This module bridges the two: the proxy feeds the model's
content stream through ``ToolCallTextToStructured`` and emits structured
``delta.tool_calls`` chunks while stripping the text from the content.

The state machine is buffer-based because the ``<tool_call>`` start and
``</tool_call>`` end can land in different SSE chunks.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any


# Tags emitted by the Qwen 3.5 chat template's system prompt
# (see ~/kinver-hub/config/qwen-fixed.jinja line 124).
_OPEN_TAG = "<tool_call>"
_CLOSE_TAG = "</tool_call>"
_FUNCTION_TAG_RE = re.compile(r"<function=([^>\s]+)>")
_PARAMETER_TAG_RE = re.compile(
    r"<parameter=([^>\s]+)>(.*?)</parameter>",
    re.DOTALL,
)


def _format_status(tool_call: dict[str, Any]) -> str:
    """Build a short human-readable status message for a parsed tool call.

    The status is emitted as a content delta *before* the structured
    ``delta.tool_calls`` chunk so nanobot-ai (and the user via Telegram)
    can see what the model is calling without seeing the raw
    ``<tool_call>`` XML.  Falls back to a generic format for tool
    names that don't have a custom handler.
    """
    name = tool_call.get("function", {}).get("name", "?")
    args_str = tool_call.get("function", {}).get("arguments", "{}")
    try:
        args = json.loads(args_str)
    except (json.JSONDecodeError, ValueError):
        return f"🔧 Calling {name}({args_str})"

    # Custom formats for the tools registered in the local system so
    # the most common calls are one line of useful information.
    if name == "exec" and "command" in args:
        cmd = args["command"]
        # Truncate very long commands so the chat stays readable.
        if len(cmd) > 200:
            cmd = cmd[:200] + "…"
        return f"🔧 exec: `{cmd}`"
    if name == "web_search" and "query" in args:
        return f"🔧 web_search: \"{args['query']}\""
    if name == "web_fetch" and "url" in args:
        return f"🔧 web_fetch: {args['url']}"
    if name == "read_file" and "path" in args:
        return f"🔧 read_file: {args['path']}"
    if name == "write_file" and "path" in args:
        return f"🔧 write_file: {args['path']}"
    if name == "edit_file" and "path" in args:
        return f"🔧 edit_file: {args['path']}"
    if name == "list_files" and "path" in args:
        return f"🔧 list_files: {args['path']}"
    if name == "cron":
        # Cron args: schedule, command, channel, user_id
        schedule = args.get("schedule", "?")
        command = args.get("command", "?")
        return f"🔧 cron ({schedule}): `{command}`"
    # Generic fallback
    return f"🔧 {name}({json.dumps(args, ensure_ascii=False)})"


def _parse_tool_call_inner(text: str, call_id: str) -> dict[str, Any] | None:
    """Parse the text between ``<tool_call>`` and ``</tool_call>``.

    Returns a dict suitable for inclusion in ``delta.tool_calls``:

        {
            "index": 0,
            "id": "call_xxx",
            "type": "function",
            "function": {"name": "exec_shell", "arguments": "{...}"}
        }

    Returns ``None`` if the text doesn't contain a recognizable
    ``<function=NAME>`` block (in which case the caller should fall
    back to emitting the text as plain content).
    """
    name_match = _FUNCTION_TAG_RE.search(text)
    if not name_match:
        return None
    name = name_match.group(1).strip()

    args: dict[str, Any] = {}
    for param_match in _PARAMETER_TAG_RE.finditer(text):
        key = param_match.group(1).strip()
        value = param_match.group(2).strip()
        # Try to parse as JSON (handles ints, floats, booleans, lists, etc.)
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            # Keep as a string. Strip surrounding quotes if the model
            # emitted them anyway.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
        args[key] = value

    return {
        "index": 0,
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


class ToolCallTextToStructured:
    """Streaming state machine that converts Qwen ``<tool_call>`` text
    in the model's content to OpenAI structured ``delta.tool_calls`` JSON.

    Usage::

        sm = ToolCallTextToStructured()
        for sse_chunk in model_stream:
            for emit in sm.feed(sse_chunk.delta.content):
                yield emit  # {"content": "..."} or {"tool_calls": [...]}
        for emit in sm.flush():
            yield emit  # any remaining content at end of stream

    The state machine preserves content outside ``<tool_call>...</tool_call>``
    blocks exactly.  Inside the blocks, it parses the XML and emits
    structured chunks.  Malformed blocks (no ``<function=NAME>`` tag)
    fall back to emitting the text as content.
    """

    def __init__(self) -> None:
        self._buffer: str = ""
        self._call_counter: int = 0
        # True once we've stripped an open tag and are waiting for the
        # close tag.  While True, ALL content is buffered (never emitted
        # as content) until the close tag is found.
        self._in_tool_call: bool = False

    def _next_call_id(self) -> str:
        self._call_counter += 1
        return f"call_tc_{int(time.time() * 1000)}_{self._call_counter}"

    @staticmethod
    def _ends_with_partial_open(buffer: str) -> bool:
        """Check if the buffer ends with a partial prefix of ``<tool_call>``.

        The model's content might end with a chunk that started ``<t``,
        ``<to``, etc.  We must hold those characters back until the next
        chunk so we don't emit the text prefix as content prematurely.
        """
        for i in range(1, len(_OPEN_TAG)):
            prefix = _OPEN_TAG[:i]
            if buffer.endswith(prefix):
                return True
        return False

    @staticmethod
    def _ends_with_partial_close(buffer: str) -> bool:
        """Check if the buffer ends with a partial prefix of ``</tool_call>``.

        Same logic as ``_ends_with_partial_open`` but for the close tag,
        so we hold the buffer back when the model is in the middle of
        emitting the close tag across chunk boundaries.
        """
        for i in range(1, len(_CLOSE_TAG)):
            prefix = _CLOSE_TAG[:i]
            if buffer.endswith(prefix):
                return True
        return False

    def feed(self, content: str) -> list[dict[str, Any]]:
        """Feed a content chunk. Return a list of emit candidates.

        Each emit candidate is a dict with EITHER ``"content"`` (text
        to emit as a content delta) OR ``"tool_calls"`` (a list of
        tool-call dicts to emit as a tool_calls delta).
        """
        if not content:
            return []
        self._buffer += content
        emits: list[dict[str, Any]] = []

        while True:
            # If we're inside a tool call (open tag seen, close tag not
            # yet seen), buffer everything until we find the close tag.
            if self._in_tool_call:
                if self._ends_with_partial_close(self._buffer):
                    break
                close_idx = self._buffer.find(_CLOSE_TAG)
                if close_idx == -1:
                    break
                # Parse and emit the tool call, then exit tool-call mode.
                inner = self._buffer[:close_idx]
                parsed = _parse_tool_call_inner(inner, self._next_call_id())
                if parsed is None:
                    # Malformed — fall back to emitting the raw text as
                    # content. This way the user still sees the model's
                    # intent even if we can't parse it.
                    emits.append({"content": _OPEN_TAG + inner + _CLOSE_TAG})
                else:
                    # Emit a human-readable status message BEFORE the
                    # structured tool call so nanobot-ai (and the user
                    # via Telegram) can see what the model is doing
                    # without the raw <tool_call> XML.
                    emits.append({"content": _format_status(parsed) + "\n"})
                    emits.append({"tool_calls": [parsed]})
                self._buffer = self._buffer[close_idx + len(_CLOSE_TAG):]
                self._in_tool_call = False
                # Continue the loop to handle any further text or tool
                # calls in the buffer.
                continue

            # Not inside a tool call — look for the next one.
            if _OPEN_TAG in self._buffer:
                start_idx = self._buffer.find(_OPEN_TAG)
                # Emit any content BEFORE the open tag.
                if start_idx > 0:
                    pre = self._buffer[:start_idx]
                    if pre:
                        emits.append({"content": pre})
                    self._buffer = self._buffer[start_idx + len(_OPEN_TAG):]
                else:
                    self._buffer = self._buffer[len(_OPEN_TAG):]
                # Enter tool-call mode and loop to find the close tag.
                self._in_tool_call = True
                continue

            # No complete open tag in the buffer. Emit the buffer
            # only if it doesn't end with a partial prefix of the
            # open tag (otherwise we'd risk emitting ``<t`` as content
            # only to have the next chunk start ``ool_call>``).
            if self._ends_with_partial_open(self._buffer):
                break
            if self._buffer:
                emits.append({"content": self._buffer})
                self._buffer = ""
            break

        return emits

    def flush(self) -> list[dict[str, Any]]:
        """Flush any remaining buffer at end of stream.

        If a partial ``<tool_call>`` was being buffered without a
        matching close tag, it falls back to content.  Otherwise the
        buffer is just trailing text.
        """
        if not self._buffer:
            return []
        # If the buffer is exactly a partial open-tag prefix, drop it.
        if self._ends_with_partial_open(self._buffer):
            self._buffer = ""
            return []
        emits: list[dict[str, Any]] = [{"content": self._buffer}]
        self._buffer = ""
        return emits
