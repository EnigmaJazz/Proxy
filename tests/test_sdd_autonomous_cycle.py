"""Tests for the cycle driver's progress-based hold (Unit 2 of the
SDD-cycle-autonomous-delivery plan: go-proxy request-flow signal,
hold decision state machine, and the fallback replay window reader)."""

import json

import pytest

from scripts import sdd_autonomous_cycle as driver
from opencode_bridge import _replay_in_flight_for_any_session


class TestCountGoProxyCalls:
    def test_counts_request_lines(self) -> None:
        text = (
            "2026-08-10T10:00:00Z [info] stream request start\n"
            "2026-08-10T10:00:01Z [info] chunk\n"
            "2026-08-10T10:00:02Z [info] request complete\n"
            "2026-08-10T10:00:03Z [info] keepalive\n"
        )
        assert driver._count_go_proxy_calls(text) == 2

    def test_empty_text_is_zero(self) -> None:
        assert driver._count_go_proxy_calls("") == 0

    def test_unrelated_lines_ignored(self) -> None:
        text = "heartbeat\nstartup banner\n[warning] nothing\n"
        assert driver._count_go_proxy_calls(text) == 0


class TestHoldDecision:
    def _full(self) -> int:
        return int(driver.ARTIFACT_WAIT_S / driver.ARTIFACT_POLL_S)

    def test_working_activity_resets_budget(self) -> None:
        action, budget = driver.hold_decision(
            recent_calls=3, session_busy=False, replay_in_flight=False,
            budget=1,
        )
        assert action == "hold"
        assert budget == self._full()

    def test_busy_session_holds_even_without_calls(self) -> None:
        action, budget = driver.hold_decision(
            recent_calls=0, session_busy=True, replay_in_flight=False,
            budget=5,
        )
        assert action == "hold"
        assert budget == self._full()

    def test_no_activity_drains_one_poll(self) -> None:
        action, budget = driver.hold_decision(
            recent_calls=0, session_busy=False, replay_in_flight=False,
            budget=10,
        )
        assert action == "drain"
        assert budget == 9

    def test_budget_exhausted_resumes(self) -> None:
        action, budget = driver.hold_decision(
            recent_calls=0, session_busy=False, replay_in_flight=False,
            budget=1,
        )
        assert action == "resume"
        assert budget == 0

    def test_replay_pauses_the_drain(self) -> None:
        action, budget = driver.hold_decision(
            recent_calls=0, session_busy=False, replay_in_flight=True,
            budget=7,
        )
        assert action == "hold"
        assert budget == 7  # unchanged: neither reset nor drained

    def test_replay_wins_over_working_activity(self) -> None:
        action, budget = driver.hold_decision(
            recent_calls=0, session_busy=False, replay_in_flight=True,
            budget=3,
        )
        assert action == "hold"
        assert budget == 3


class TestReplayWindowReader:
    def test_open_window_detected(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text(
            json.dumps({
                "timestamp": "2099-01-01T00:00:00.000Z",
                "event": "fallback_cycle_started",
                "sessionID": "ses_0001",
            })
        )
        from opencode_bridge import _FALLBACK_REPLAY_LOG
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        assert _replay_in_flight_for_any_session() is True

    def test_completed_window_is_closed(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text(
            "\n".join([
                json.dumps({
                    "timestamp": "2099-01-01T00:00:00.000Z",
                    "event": "fallback_cycle_started",
                    "sessionID": "ses_0001",
                }),
                json.dumps({
                    "timestamp": "2099-01-01T00:00:10.000Z",
                    "event": "fallback_cycle_completed",
                    "sessionID": "ses_0001",
                }),
            ])
        )
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        assert _replay_in_flight_for_any_session() is False

    def test_stale_window_is_closed(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text(
            json.dumps({
                "timestamp": "2000-01-01T00:00:00.000Z",  # decades stale
                "event": "fallback_cycle_started",
                "sessionID": "ses_0001",
            })
        )
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        assert _replay_in_flight_for_any_session() is False

    def test_missing_log_fails_closed(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "opencode_bridge._FALLBACK_REPLAY_LOG",
            tmp_path / "does-not-exist.log",
        )
        assert _replay_in_flight_for_any_session() is False

    def test_unreadable_log_fails_closed(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "rate-limit-fallback.log"
        log.write_text("this is not json\n")
        monkeypatch.setattr("opencode_bridge._FALLBACK_REPLAY_LOG", log)
        assert _replay_in_flight_for_any_session() is False


class TestTerminalFailureMarker:
    """The driver must detect the orchestrator's loud-failure marker in
    the pinned session and exit terminally instead of resuming.  Only the
    FINAL assistant message ending with the marker counts — the task text
    (user role) quotes the contract, and mid-turn quotes must not fire."""

    @staticmethod
    def _fake_client(resp_json, status=200):
        import httpx as _httpx

        class _FakeResp:
            status_code = status

            def json(self):
                return resp_json

        class _FakeClient:
            def __init__(self, *a, **kw):
                self._resp = _FakeResp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kw):
                self.url = url
                return self._resp

        return _FakeClient

    @pytest.mark.asyncio
    async def test_final_assistant_message_with_marker_detected(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opencode_bridge import _SDD_TERMINAL_FAILURE_MARKER

        client = self._fake_client([
            {"role": "user", "parts": [{"type": "text",
                                        "text": "Use SDD..."}]},
            {"role": "assistant", "parts": [{"type": "text",
                                             "text": "still working"}]},
            {"role": "assistant",
             "parts": [{"type": "text",
                        "text": f"done. {_SDD_TERMINAL_FAILURE_MARKER}: spec"}]},
        ])
        monkeypatch.setattr(driver.httpx, "AsyncClient", client)
        assert await driver._session_has_terminal_marker("ses_0001") is True

    @pytest.mark.asyncio
    async def test_user_task_text_with_marker_is_not_terminal(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opencode_bridge import _SDD_TERMINAL_FAILURE_MARKER

        client = self._fake_client([
            {"role": "user",
             "parts": [{"type": "text",
                        "text": f"rules: {_SDD_TERMINAL_FAILURE_MARKER}: <phase>"}]},
            {"role": "assistant", "parts": [{"type": "text",
                                             "text": "ok, continuing"}]},
        ])
        monkeypatch.setattr(driver.httpx, "AsyncClient", client)
        assert await driver._session_has_terminal_marker("ses_0001") is False

    @pytest.mark.asyncio
    async def test_mid_turn_quote_is_not_terminal(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opencode_bridge import _SDD_TERMINAL_FAILURE_MARKER

        client = self._fake_client([
            {"role": "assistant",
             "parts": [{"type": "text",
                        "text": f"I follow the rule: end with "
                                f"{_SDD_TERMINAL_FAILURE_MARKER}: <phase>. "
                                f"Continuing the spec now."}]},
        ])
        monkeypatch.setattr(driver.httpx, "AsyncClient", client)
        assert await driver._session_has_terminal_marker("ses_0001") is False

    @pytest.mark.asyncio
    async def test_no_marker_is_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = self._fake_client([
            {"role": "assistant",
             "parts": [{"type": "text", "text": "cycle continues"}]},
        ])
        monkeypatch.setattr(driver.httpx, "AsyncClient", client)
        assert await driver._session_has_terminal_marker("ses_0001") is False

    @pytest.mark.asyncio
    async def test_transport_error_is_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import httpx as _httpx

        class _BrokenClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kw):
                raise _httpx.ConnectError("conn refused")

        monkeypatch.setattr(driver.httpx, "AsyncClient", _BrokenClient)
        assert await driver._session_has_terminal_marker("ses_0001") is False

    @pytest.mark.asyncio
    async def test_non_text_parts_ignored(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = self._fake_client([
            {"role": "assistant",
             "parts": [{"type": "tool", "tool": "bash",
                        "state": {"status": "completed"}}]},
        ])
        monkeypatch.setattr(driver.httpx, "AsyncClient", client)
        assert await driver._session_has_terminal_marker("ses_0001") is False
