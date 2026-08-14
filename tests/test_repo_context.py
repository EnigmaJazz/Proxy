"""Repo-context injection for the local (professional) pathway.

The professional model has no tools and no visibility of the opencode
agents' work (the session text is out of reach), so a future
conversation can't pick up the code.  The proxy captures completed
opencode tasks (summary + working-tree snapshot) and appends a compact
repo-state block to the OUTBOUND system message (R1 carve-out,
2026-08-14).  Opt out per request with ``X-Proxy-Repo-Context: off``.
"""
from __future__ import annotations

import types
from typing import Any

import pytest


def _govern(msgs: list[dict[str, Any]], headers: dict[str, str]) -> Any:
    """Run routes._govern_messages with the given headers."""
    from routes import _govern_messages

    request = types.SimpleNamespace(headers=headers)
    return _govern_messages(request, msgs, model_key="professional", max_tokens=4096)


class TestRepoContextCapture:
    """Completed opencode tasks are captured with their repo snapshot."""

    def test_capture_stores_summary_and_tree(self) -> None:
        import routes

        routes._RECENT_OPENCODE_WORK.clear()
        try:
            routes._capture_opencode_work(
                ["The task is complete. ", "I created the monitor script."]
            )
            assert len(routes._RECENT_OPENCODE_WORK) == 1
            entry = routes._RECENT_OPENCODE_WORK[0]
            assert "The task is complete." in entry["summary"]
            # The working-tree snapshot reflects the agent's files (the
            # repo is the shared artifact between the two pathways).
            assert "scripts/monitor_local_model_tokens.py" in entry["status"]
        finally:
            routes._RECENT_OPENCODE_WORK.clear()

    def test_capture_ignores_empty_summary(self) -> None:
        import routes

        routes._RECENT_OPENCODE_WORK.clear()
        try:
            routes._capture_opencode_work(["", "  "])
            assert routes._RECENT_OPENCODE_WORK == []
        finally:
            routes._RECENT_OPENCODE_WORK.clear()

    def test_capture_bounded(self) -> None:
        import routes

        routes._RECENT_OPENCODE_WORK.clear()
        try:
            for i in range(5):
                routes._capture_opencode_work([f"task {i}"])
            assert len(routes._RECENT_OPENCODE_WORK) == routes._MAX_RECENT_WORK
        finally:
            routes._RECENT_OPENCODE_WORK.clear()


class TestRepoContextInjection:
    """The block lands in the OUTBOUND system message, appended (KV-cache
    position), with the per-request opt-out."""

    @pytest.mark.asyncio
    async def test_appended_by_default(self) -> None:
        import routes

        routes._RECENT_OPENCODE_WORK.clear()
        try:
            routes._capture_opencode_work(["Fixed the token monitor."])
            msgs = [{"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "hi"}]
            out = await _govern(msgs, {})
            # Appended after the stable system content (cache-visible
            # prefix intact), never mutating the input.
            assert out[0]["content"].startswith("You are helpful.")
            assert "Fixed the token monitor." in out[0]["content"]
            assert "[Recent OpenCode task" in out[0]["content"]
            assert "[Recent commits]" in out[0]["content"]
            assert msgs[0]["content"] == "You are helpful."  # input untouched
            assert out[1] == msgs[1]
        finally:
            routes._RECENT_OPENCODE_WORK.clear()

    @pytest.mark.asyncio
    async def test_opt_out(self) -> None:
        import routes

        routes._RECENT_OPENCODE_WORK.clear()
        try:
            routes._capture_opencode_work(["Some work."])
            msgs = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "hi"}]
            out = await _govern(
                msgs, {"x-proxy-repo-context": "off", "x-proxy-date-time": "off"},
            )
            assert out[0]["content"] == "sys"
        finally:
            routes._RECENT_OPENCODE_WORK.clear()

    @pytest.mark.asyncio
    async def test_quiet_without_captured_work_still_injects_live_state(
        self,
    ) -> None:
        import routes

        routes._RECENT_OPENCODE_WORK.clear()
        try:
            msgs = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "hi"}]
            out = await _govern(msgs, {})
            # No captured tasks, but the live working-tree/commit state is
            # still injected (the repo IS the shared artifact).
            assert "[Recent commits]" in out[0]["content"]
        finally:
            routes._RECENT_OPENCODE_WORK.clear()
