"""Repo-context injection for the local (professional) pathway.

The professional model has no tools and no visibility of the opencode
agents' work (the session text is out of reach), so a future
conversation can't pick up the code.  The proxy captures completed
opencode tasks (summary + working-tree snapshot) and appends a compact
repo-state block (including the work files' CONTENT) to the OUTBOUND
system message (R1 carve-out, 2026-08-14).  The block reads the bridge
directory (OPENCODE_BRIDGE_DIRECTORY — the nanobot workspace).  Opt
out per request with ``X-Proxy-Repo-Context: off``.
"""
from __future__ import annotations

import subprocess
import types
from typing import Any

import pytest


@pytest.fixture
def work_repo(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real temp git repo standing in for the bridge directory (the
    nanobot workspace).  routes.OPENCODE_BRIDGE_DIRECTORY is patched to it
    so the capture/block/content reads are hermetic."""
    import routes

    repo = tmp_path / "work"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "base.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    monkeypatch.setattr(routes, "OPENCODE_BRIDGE_DIRECTORY", str(repo))
    return repo


def _govern(msgs: list[dict[str, Any]], headers: dict[str, str]) -> Any:
    """Run routes._govern_messages with the given headers."""
    from routes import _govern_messages

    request = types.SimpleNamespace(headers=headers)
    return _govern_messages(request, msgs, model_key="professional", max_tokens=4096)


class TestRepoContextCapture:
    """Completed opencode tasks are captured with their repo snapshot."""

    def test_capture_stores_summary_and_tree(self, work_repo: Any) -> None:
        import routes

        (work_repo / "work.py").write_text("def work(): pass\n", encoding="utf-8")
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
            assert "work.py" in entry["status"]
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
    async def test_appended_by_default(self, work_repo: Any) -> None:
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
    async def test_opt_out(self, work_repo: Any) -> None:
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
        self, work_repo: Any,
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


class TestRepoContentSection:
    """The work files' CONTENT lands in the block — the local model's only
    way to read and edit them (it has no tools)."""

    def _section(self, status: str, repo: Any) -> str:
        from routes import _repo_content_section
        return _repo_content_section(status, repo=repo)

    def test_untracked_file_content_included(self, tmp_path: Any) -> None:
        f = tmp_path / "scripts"
        f.mkdir()
        (f / "monitor.py").write_text(
            "#!/usr/bin/env python3\n\ndef main() -> None:\n    print('hi')\n",
            encoding="utf-8",
        )
        out = self._section("?? scripts/monitor.py", tmp_path)
        assert "[File: scripts/monitor.py]" in out
        assert "def main() -> None:" in out
        assert "print('hi')" in out

    def test_oversized_untracked_file_skipped(self, tmp_path: Any) -> None:
        f = tmp_path / "scripts"
        f.mkdir()
        big = f / "huge.bin"
        big.write_bytes(b"\x00" * 300_000)
        out = self._section("?? scripts/huge.bin", tmp_path)
        assert out == ""

    def test_truncation_marker(self, tmp_path: Any) -> None:
        f = tmp_path / "scripts"
        f.mkdir()
        long_file = f / "long.py"
        long_file.write_text(
            "\n".join(f"line {i}" for i in range(500)), encoding="utf-8",
        )
        out = self._section("?? scripts/long.py", tmp_path)
        assert "more lines truncated" in out
        assert "line 399" in out
        assert "line 499" not in out

    def test_modified_tracked_file_diff_included(self, tmp_path: Any) -> None:
        # tmp_path is not a git repo — the diff call fails gracefully and
        # the section stays quiet for tracked modifications.
        out = self._section("M ROUTER-LOG.md", tmp_path)
        assert out == ""

    def test_nanobot_internal_state_skipped(self, tmp_path: Any) -> None:
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "history.jsonl").write_text("private", encoding="utf-8")
        (tmp_path / "SOUL.md").write_text("soul content", encoding="utf-8")
        out = self._section("?? memory/history.jsonl\n?? SOUL.md", tmp_path)
        assert "history.jsonl" not in out
        assert "[File: SOUL.md]" in out


class TestSddOutputCopy:
    """A completed SDD cycle's artifacts are copied into the nanobot
    workspace (sdd-work/<change>/) so the local model sees the final
    output — the cycle itself runs in the proxy repo (OpenSpec store)."""

    def test_copies_change_artifacts_and_summary(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import routes

        sdd_repo = tmp_path / "proxy-repo"
        changes = sdd_repo / "openspec" / "changes" / "demo-change"
        changes.mkdir(parents=True)
        (changes / "tasks.md").write_text("task list", encoding="utf-8")
        (changes / "design.md").write_text("design doc", encoding="utf-8")

        work = tmp_path / "workspace"
        work.mkdir()
        monkeypatch.setattr(routes, "OPENCODE_SDD_DIRECTORY", str(sdd_repo))
        monkeypatch.setattr(routes, "OPENCODE_BRIDGE_DIRECTORY", str(work))

        dst = routes._copy_sdd_output_to_workspace(
            "demo-change", "The cycle is complete."
        )
        assert dst == str(work / "sdd-work" / "demo-change")
        assert (work / "sdd-work" / "demo-change" / "tasks.md").read_text() == "task list"
        assert (work / "sdd-work" / "demo-change" / "design.md").read_text() == "design doc"
        assert (work / "sdd-work" / "demo-change" / "FINAL-SUMMARY.md").read_text() == "The cycle is complete."

    def test_missing_change_dir_is_quiet(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        import routes

        sdd_repo = tmp_path / "proxy-repo"
        sdd_repo.mkdir()
        work = tmp_path / "workspace"
        work.mkdir()
        monkeypatch.setattr(routes, "OPENCODE_SDD_DIRECTORY", str(sdd_repo))
        monkeypatch.setattr(routes, "OPENCODE_BRIDGE_DIRECTORY", str(work))

        assert routes._copy_sdd_output_to_workspace("nope", "x") == ""
        assert not (work / "sdd-work").exists()
