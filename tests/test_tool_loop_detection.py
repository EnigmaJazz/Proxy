"""Tests for the tool-loop detection in ``_check_tool_loop``."""
from __future__ import annotations

import types

from typing import Any

from routes import (
    _check_tool_loop,
    _first_user_message_content,
    _loop_state,
    _resolve_session_id,
    _tool_call_signature,
)


def _fresh_app() -> types.SimpleNamespace:
    """Return a fake app with a fresh, empty ``app.state``.

    The loop/session state now lives on ``app.state`` (Rule 6), so each
    test gets a fresh, isolated state by creating a new fake app.
    """
    return types.SimpleNamespace(state=types.SimpleNamespace())


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

class TestToolCallSignature:
    def test_name_plus_args(self) -> None:
        tc = {"function": {"name": "exec", "arguments": '{"command":"ls"}'}}
        assert _tool_call_signature(tc) == 'exec:{"command":"ls"}'

    def test_different_args_different_signature(self) -> None:
        a = {"function": {"name": "exec", "arguments": '{"command":"ls"}'}}
        b = {"function": {"name": "exec", "arguments": '{"command":"pwd"}'}}
        assert _tool_call_signature(a) != _tool_call_signature(b)

    def test_different_name_different_signature(self) -> None:
        a = {"function": {"name": "exec", "arguments": '{"command":"ls"}'}}
        b = {"function": {"name": "read_file", "arguments": '{"path":"x"}'}}
        assert _tool_call_signature(a) != _tool_call_signature(b)

    def test_missing_function_fields(self) -> None:
        # Missing function dict should produce a stable empty signature.
        assert _tool_call_signature({}) == ":"


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------

class TestCheckToolLoop:
    def test_first_call_not_a_loop(self) -> None:
        app = _fresh_app()
        assert _check_tool_loop("job-1", "exec:ls", app) is False

    def test_two_identical_calls_not_a_loop(self) -> None:
        # The window is 3 consecutive calls; the 2nd one is still in
        # the loop, the 3rd is the trigger.
        app = _fresh_app()
        assert _check_tool_loop("job-1", "exec:ls", app) is False
        assert _check_tool_loop("job-1", "exec:ls", app) is False

    def test_three_identical_calls_trigger_loop(self) -> None:
        app = _fresh_app()
        assert _check_tool_loop("job-1", "exec:ls", app) is False
        assert _check_tool_loop("job-1", "exec:ls", app) is False
        assert _check_tool_loop("job-1", "exec:ls", app) is True

    def test_fourth_and_later_calls_stay_in_loop(self) -> None:
        app = _fresh_app()
        for _ in range(3):
            _check_tool_loop("job-1", "exec:ls", app)
        assert _check_tool_loop("job-1", "exec:ls", app) is True
        assert _check_tool_loop("job-1", "exec:ls", app) is True

    def test_different_signature_resets_window(self) -> None:
        app = _fresh_app()
        _check_tool_loop("job-1", "exec:ls", app)
        _check_tool_loop("job-1", "exec:ls", app)
        # Different tool call — resets the consecutive-run window.
        assert _check_tool_loop("job-1", "exec:pwd", app) is False
        # Have to start over to trigger loop detection.
        assert _check_tool_loop("job-1", "exec:ls", app) is False
        assert _check_tool_loop("job-1", "exec:ls", app) is False
        assert _check_tool_loop("job-1", "exec:ls", app) is True

    def test_separate_jobs_have_separate_state(self) -> None:
        app = _fresh_app()
        # job-1 is in a loop; job-2 is fresh.
        _check_tool_loop("job-1", "exec:ls", app)
        _check_tool_loop("job-1", "exec:ls", app)
        _check_tool_loop("job-1", "exec:ls", app)
        assert _check_tool_loop("job-2", "exec:ls", app) is False

    def test_eviction_drops_oldest_session(self, monkeypatch: Any) -> None:
        """The LRU eviction must drop the session with the oldest
        last_seen, not an arbitrary one.

        Regression: the old code looked up ``last_seen`` in
        ``_session_state`` — a different key space (bare first-message
        hash vs full session_id/job_id) — so every lookup missed and
        ``min()`` evicted the first key instead of the oldest session.
        Here the first key (s1) has the NEWEST last_seen, so the two
        behaviours pick different victims.
        """
        monkeypatch.setattr("routes._LOOP_DETECTION_MAX_JOBS", 2)
        app = _fresh_app()
        _check_tool_loop("s1", "exec:ls", app)  # first key
        _check_tool_loop("s2", "exec:ls", app)
        state = _loop_state(app)
        # Force distinct last_seen so eviction is deterministic.
        state["s1"]["last_seen"] = 200.0  # newest
        state["s2"]["last_seen"] = 100.0  # oldest
        # Third session pushes past the cap → s2 (oldest) is evicted.
        _check_tool_loop("s3", "exec:ls", app)
        state = _loop_state(app)
        assert "s1" in state
        assert "s2" not in state
        assert "s3" in state

    def test_handles_alternating_calls(self) -> None:
        app = _fresh_app()
        # Alternating signatures never trigger a loop.
        for _ in range(5):
            _check_tool_loop("job-1", "exec:ls", app)
            _check_tool_loop("job-1", "exec:pwd", app)
        # No loop because the consecutive-run is broken every other call.
        assert _check_tool_loop("job-1", "exec:ls", app) is False

    def test_handles_mixed_consecutive_with_reset(self) -> None:
        app = _fresh_app()
        # 2 exec:ls, then 1 read_file, then 2 exec:ls — not a loop
        # because the read_file reset the window.
        _check_tool_loop("job-1", "exec:ls", app)
        _check_tool_loop("job-1", "exec:ls", app)
        _check_tool_loop("job-1", "read_file:x", app)
        _check_tool_loop("job-1", "exec:ls", app)
        assert _check_tool_loop("job-1", "exec:ls", app) is False

    def test_real_world_pattern(self) -> None:
        """Reproduce the exact pattern from the runaway job: the model
        calls ``cat /proc/self/status | grep no_new_privs`` repeatedly
        with minor escaping variations.  Each variation is a different
        signature, so the loop detector does NOT trigger — but
        identically-escaped repeats do trigger after 3."""
        app = _fresh_app()
        base = 'exec:{"command":"cat /proc/self/status | grep no_new_privs"}'
        # 3 identical → trigger
        _check_tool_loop("job-1", base, app)
        _check_tool_loop("job-1", base, app)
        assert _check_tool_loop("job-1", base, app) is True
        # Same call with different grep pattern → different sig, not a
        # loop.  This matches the actual runaway pattern where the
        # model varies its grep patterns.
        app = _fresh_app()  # fresh state instead of the removed _clear_tool_loop
        v1 = 'exec:{"command":"cat /proc/self/status | grep NoNew"}'
        v2 = 'exec:{"command":"cat /proc/self/status | grep NoNewPrivs"}'
        for _ in range(2):
            _check_tool_loop("job-1", v1, app)
            _check_tool_loop("job-1", v2, app)
        assert _check_tool_loop("job-1", v1, app) is False
        assert _check_tool_loop("job-1", v2, app) is False


# ---------------------------------------------------------------------------
# First-user-message extraction (for session_id)
# ---------------------------------------------------------------------------

class TestFirstUserMessageContent:
    def test_extracts_string_content(self) -> None:
        messages = [
            {"role": "system", "content": "you are a helper"},
            {"role": "user", "content": "what is the cpu temperature"},
        ]
        assert _first_user_message_content(messages) == "what is the cpu temperature"

    def test_skips_system_messages(self) -> None:
        # Multi-line system prompts (common in nanobot-ai) should not
        # be the session identifier.
        messages = [
            {"role": "system", "content": "very long system prompt..."},
            {"role": "user", "content": "hi"},
        ]
        assert _first_user_message_content(messages) == "hi"

    def test_joins_typed_content_parts(self) -> None:
        # Some clients send content as a list of typed parts.
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
            ]},
        ]
        assert _first_user_message_content(messages) == "hello  world"

    def test_no_user_message_returns_empty(self) -> None:
        messages = [{"role": "system", "content": "system only"}]
        assert _first_user_message_content(messages) == ""


# ---------------------------------------------------------------------------
# Session ID resolution (for cross-request loop detection)
# ---------------------------------------------------------------------------

class TestResolveSessionId:
    """``_resolve_session_id`` returns a stable session_id per
    conversation.  Two simultaneous sessions with the same first
    user message get DIFFERENT session_ids because the time
    component is the FIRST time the proxy saw that first message.
    """

    def test_same_first_message_same_session_id_across_calls(self) -> None:
        app = _fresh_app()
        messages = [{"role": "user", "content": "what is the cpu temp"}]
        s1 = _resolve_session_id(messages, app)
        s2 = _resolve_session_id(messages, app)
        assert s1 == s2  # same session, same session_id

    def test_different_first_messages_different_session_ids(self) -> None:
        app = _fresh_app()
        m1 = [{"role": "user", "content": "what is the cpu temp"}]
        m2 = [{"role": "user", "content": "what is the memory usage"}]
        assert _resolve_session_id(m1, app) != _resolve_session_id(m2, app)

    def test_session_id_format(self) -> None:
        """The session_id is ``<first_user_msg_hash>:<first_seen_unix>``."""
        app = _fresh_app()
        messages = [{"role": "user", "content": "test message"}]
        s = _resolve_session_id(messages, app)
        # Format: <16 hex chars>:<digits>
        parts = s.split(":")
        assert len(parts) == 2
        assert len(parts[0]) == 16
        assert all(c in "0123456789abcdef" for c in parts[0])
        assert parts[1].isdigit()

    def test_conversation_with_tool_results_still_resolves(self) -> None:
        """Subsequent requests have assistant messages with tool_calls
        and tool results.  The first USER message is still the same."""
        app = _fresh_app()
        m1 = [
            {"role": "system", "content": "long prompt"},
            {"role": "user", "content": "check no_new_privs"},
        ]
        s1 = _resolve_session_id(m1, app)
        m2 = [
            {"role": "system", "content": "long prompt"},
            {"role": "user", "content": "check no_new_privs"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        s2 = _resolve_session_id(m2, app)
        assert s1 == s2  # same conversation, same session_id

    def test_no_user_message_returns_anonymous_session(self) -> None:
        app = _fresh_app()
        messages = [{"role": "system", "content": "system only"}]
        s = _resolve_session_id(messages, app)
        assert s.startswith("anon:")


# ---------------------------------------------------------------------------
# Cross-request loop detection via session_id
# ---------------------------------------------------------------------------

class TestCrossRequestLoopDetection:
    """The end-to-end behavior: detect a loop across multiple HTTP
    requests in the same conversation.  Each request is a new job
    (new job_id) but the session_id is the same, so the loop state
    carries over.
    """

    def test_loop_detected_across_two_requests(self) -> None:
        """Reproduce the runaway: request 1 calls exec, request 2
        calls exec with the same args, request 3 triggers loop."""
        app = _fresh_app()
        messages = [
            {"role": "user", "content": "check no_new_privs repeatedly"},
        ]
        # First request — model makes the first call
        sig = "exec:ls"
        # Simulate the proxy being called 3 times (across 3 requests)
        # for the same conversation, each time the model making the
        # same tool call.
        session_id = _resolve_session_id(messages, app)
        # First request: signature seen once
        assert _check_tool_loop(session_id, sig, app) is False
        # Second request: signature seen twice (carries over via
        # the shared session_id).
        assert _check_tool_loop(session_id, sig, app) is False
        # Third request: signature seen three times → loop.
        assert _check_tool_loop(session_id, sig, app) is True

    def test_different_conversations_have_separate_state(self) -> None:
        """Two conversations with the same first user message but
        different first-seen times get different session_ids and
        separate loop state."""
        app = _fresh_app()
        messages = [{"role": "user", "content": "check no_new_privs"}]
        s1 = _resolve_session_id(messages, app)
        # Both calls return the same session_id (same conversation).
        s2 = _resolve_session_id(messages, app)
        assert s1 == s2
        # A different first user message → different session.
        other = [{"role": "user", "content": "check memory usage"}]
        s3 = _resolve_session_id(other, app)
        assert s1 != s3
