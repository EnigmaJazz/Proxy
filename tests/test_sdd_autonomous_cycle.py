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

    def test_part_growth_holds_and_resets_budget(self) -> None:
        action, budget = driver.hold_decision(
            parts_grew=True, replay_in_flight=False, budget=1,
        )
        assert action == "hold"
        assert budget == self._full()

    def test_no_growth_drains_one_poll(self) -> None:
        action, budget = driver.hold_decision(
            parts_grew=False, replay_in_flight=False, budget=10,
        )
        assert action == "drain"
        assert budget == 9

    def test_budget_exhausted_resumes(self) -> None:
        action, budget = driver.hold_decision(
            parts_grew=False, replay_in_flight=False, budget=1,
        )
        assert action == "resume"
        assert budget == 0

    def test_replay_pauses_the_drain(self) -> None:
        action, budget = driver.hold_decision(
            parts_grew=False, replay_in_flight=True, budget=7,
        )
        assert action == "hold"
        assert budget == 7  # unchanged: neither reset nor drained

    def test_replay_wins_over_growth_decision_pause(self) -> None:
        # A replay pauses the drain without resetting the budget.
        action, budget = driver.hold_decision(
            parts_grew=True, replay_in_flight=True, budget=3,
        )
        assert action == "hold"
        assert budget == 3




class TestServeTotalPartsDb:
    """The cycle-scoped work signal reads the serve's SQLite store —
    the HTTP status API 404s persisted sessions after a recycle."""

    def _make_db(self, tmp_path, session_parts: dict[str, int]) -> str:
        import sqlite3 as _sq

        db_path = tmp_path / "serve-config" / "opencode" / "opencode.db"
        db_path.parent.mkdir(parents=True)
        con = _sq.connect(str(db_path))
        con.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT)")
        for sid, n in session_parts.items():
            con.execute("INSERT INTO session (id) VALUES (?)", (sid,))
            for i in range(n):
                con.execute(
                    "INSERT INTO part (id, session_id) VALUES (?, ?)",
                    (f"{sid}-p{i}", sid),
                )
        con.commit()
        con.close()
        return str(tmp_path / "serve-config")

    def test_totals_all_sessions(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "opencode_bridge.OPENCODE_SERVE_CONFIG_DIR",
            self._make_db(tmp_path, {"ses_a": 5, "ses_b": 3}),
        )
        assert driver._serve_total_parts_db() == 8

    def test_missing_db_is_negative(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "opencode_bridge.OPENCODE_SERVE_CONFIG_DIR",
            str(tmp_path / "nope"),
        )
        assert driver._serve_total_parts_db() == -1
