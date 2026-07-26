"""Tests for the tool-loop detection in ``_check_tool_loop``."""
from __future__ import annotations

import pytest

from routes import (
    _check_tool_loop,
    _clear_tool_loop,
    _tool_call_signature,
    _loop_detection_state,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Clear the module-level loop detection state between tests."""
    _loop_detection_state.clear()
    yield
    _loop_detection_state.clear()


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
        assert _check_tool_loop("job-1", "exec:ls") is False

    def test_two_identical_calls_not_a_loop(self) -> None:
        # The window is 3 consecutive calls; the 2nd one is still in
        # the loop, the 3rd is the trigger.
        assert _check_tool_loop("job-1", "exec:ls") is False
        assert _check_tool_loop("job-1", "exec:ls") is False

    def test_three_identical_calls_trigger_loop(self) -> None:
        assert _check_tool_loop("job-1", "exec:ls") is False
        assert _check_tool_loop("job-1", "exec:ls") is False
        assert _check_tool_loop("job-1", "exec:ls") is True

    def test_fourth_and_later_calls_stay_in_loop(self) -> None:
        for _ in range(3):
            _check_tool_loop("job-1", "exec:ls")
        assert _check_tool_loop("job-1", "exec:ls") is True
        assert _check_tool_loop("job-1", "exec:ls") is True

    def test_different_signature_resets_window(self) -> None:
        _check_tool_loop("job-1", "exec:ls")
        _check_tool_loop("job-1", "exec:ls")
        # Different tool call — resets the consecutive-run window.
        assert _check_tool_loop("job-1", "exec:pwd") is False
        # Have to start over to trigger loop detection.
        assert _check_tool_loop("job-1", "exec:ls") is False
        assert _check_tool_loop("job-1", "exec:ls") is False
        assert _check_tool_loop("job-1", "exec:ls") is True

    def test_separate_jobs_have_separate_state(self) -> None:
        # job-1 is in a loop; job-2 is fresh.
        _check_tool_loop("job-1", "exec:ls")
        _check_tool_loop("job-1", "exec:ls")
        _check_tool_loop("job-1", "exec:ls")
        assert _check_tool_loop("job-2", "exec:ls") is False

    def test_clear_tool_loop_resets_state(self) -> None:
        _check_tool_loop("job-1", "exec:ls")
        _check_tool_loop("job-1", "exec:ls")
        _check_tool_loop("job-1", "exec:ls")
        _clear_tool_loop("job-1")
        # After clearing, the next call is fresh.
        assert _check_tool_loop("job-1", "exec:ls") is False

    def test_handles_alternating_calls(self) -> None:
        # Alternating signatures never trigger a loop.
        for _ in range(5):
            _check_tool_loop("job-1", "exec:ls")
            _check_tool_loop("job-1", "exec:pwd")
        # No loop because the consecutive-run is broken every other call.
        assert _check_tool_loop("job-1", "exec:ls") is False

    def test_handles_mixed_consecutive_with_reset(self) -> None:
        # 2 exec:ls, then 1 read_file, then 2 exec:ls — not a loop
        # because the read_file reset the window.
        _check_tool_loop("job-1", "exec:ls")
        _check_tool_loop("job-1", "exec:ls")
        _check_tool_loop("job-1", "read_file:x")
        _check_tool_loop("job-1", "exec:ls")
        assert _check_tool_loop("job-1", "exec:ls") is False

    def test_real_world_pattern(self) -> None:
        """Reproduce the exact pattern from the runaway job: the model
        calls ``cat /proc/self/status | grep no_new_privs`` repeatedly
        with minor escaping variations.  Each variation is a different
        signature, so the loop detector does NOT trigger — but
        identically-escaped repeats do trigger after 3."""
        base = 'exec:{"command":"cat /proc/self/status | grep no_new_privs"}'
        # 3 identical → trigger
        _check_tool_loop("job-1", base)
        _check_tool_loop("job-1", base)
        assert _check_tool_loop("job-1", base) is True
        # Same call with different grep pattern → different sig, not a
        # loop.  This matches the actual runaway pattern where the
        # model varies its grep patterns.
        _clear_tool_loop("job-1")
        v1 = 'exec:{"command":"cat /proc/self/status | grep NoNew"}'
        v2 = 'exec:{"command":"cat /proc/self/status | grep NoNewPrivs"}'
        for _ in range(2):
            _check_tool_loop("job-1", v1)
            _check_tool_loop("job-1", v2)
        assert _check_tool_loop("job-1", v1) is False
        assert _check_tool_loop("job-1", v2) is False
