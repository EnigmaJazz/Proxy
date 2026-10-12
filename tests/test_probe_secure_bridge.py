"""Tests for scripts/probe_secure_bridge.py (bridge v4, task T8).

The probe is importable as a module (the repo root is on ``sys.path`` via
``tests/conftest.py``), so its decision layer is exercised directly — no live
server, no network, no subprocess.  The import form mirrors the sibling
``tests/test_detect_opencode_input.py`` (``from scripts import <module>``).

``_remove_probe_dir`` is SAFETY-CRITICAL: its HARD GUARD may remove ONLY
``<workspace>/.opencode_bridge_probe`` (that basename, directly in that parent,
and not a symlink).  Every refusal case asserts the foreign target STILL EXISTS
afterwards, as a positive control on the guard.

Hermetic: ``tmp_path`` only — no live server, no network, no subprocess.  The
full suite's ``tests/conftest.py`` needs ``httpx``/``prometheus_client`` (absent
in the sandbox worker), so these tests were executed under a stub harness that
skips conftest; all passed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import probe_secure_bridge as psb

#: An absolute-path-looking token (the historical bug: the prompt carried an
#: out-of-workspace absolute write target).  ``**/*`` does not match because the
#: ``/`` is not followed by a path character, and a relative ``dir/file`` is
#: excluded by the lookbehind.
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w./~])/[A-Za-z0-9_][A-Za-z0-9_./-]*")


def _fact(facts: list[psb.Check], prefix: str) -> psb.Check:
    """Return the acceptance fact whose name starts with ``prefix``."""
    for fact in facts:
        if fact.name.startswith(prefix):
            return fact
    raise AssertionError(
        f"no acceptance fact starting {prefix!r}; got {[f.name for f in facts]}"
    )


@pytest.fixture
def absent_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the session/log scan at a missing dir, so facts 4/5 are hermetic.

    ``_acceptance_facts`` reads the module-level ``_OPENCODE_DATA_DIR`` at call
    time; without this the test would walk the host's real
    ``~/.local/share/opencode`` (non-deterministic and slow).
    """
    missing = tmp_path / "opencode_data_missing"
    monkeypatch.setattr(psb, "_OPENCODE_DATA_DIR", str(missing))
    return missing


# -- _remove_probe_dir (SAFETY-CRITICAL) ------------------------------------


def test_remove_probe_dir_removes_exact_fixture_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    probe_dir = workspace / ".opencode_bridge_probe"
    (probe_dir / "nested").mkdir(parents=True)
    (probe_dir / "nested" / "out.txt").write_text("x", encoding="utf-8")

    assert psb._remove_probe_dir(probe_dir, workspace) is True
    assert not probe_dir.exists()
    assert workspace.is_dir()  # the parent workspace itself is untouched


def test_remove_probe_dir_refuses_foreign_name_and_leaves_it_intact(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    foreign = workspace / "not_the_probe_dir"
    foreign.mkdir(parents=True)
    keeper = foreign / "keep.txt"
    keeper.write_text("keep", encoding="utf-8")

    assert psb._remove_probe_dir(foreign, workspace) is False
    # Positive control: the refused target STILL EXISTS, byte-for-byte.
    assert foreign.is_dir()
    assert keeper.read_text(encoding="utf-8") == "keep"


def test_remove_probe_dir_refuses_wrong_parent_and_leaves_it_intact(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    foreign = elsewhere / ".opencode_bridge_probe"  # right name, wrong parent
    foreign.mkdir(parents=True)
    keeper = foreign / "keep.txt"
    keeper.write_text("keep", encoding="utf-8")

    assert psb._remove_probe_dir(foreign, workspace) is False
    # Positive control: the refused target STILL EXISTS under its real parent.
    assert foreign.is_dir()
    assert keeper.read_text(encoding="utf-8") == "keep"
    assert workspace.is_dir()  # the named workspace was not touched either


def test_remove_probe_dir_refuses_symlink_and_leaves_target_intact(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_target = tmp_path / "real_target"
    real_target.mkdir()
    keeper = real_target / "keep.txt"
    keeper.write_text("keep", encoding="utf-8")
    link = workspace / ".opencode_bridge_probe"
    link.symlink_to(real_target, target_is_directory=True)

    assert psb._remove_probe_dir(link, workspace) is False
    # Positive control: the symlink survives AND so does what it points at.
    assert link.is_symlink()
    assert real_target.is_dir()
    assert keeper.read_text(encoding="utf-8") == "keep"


def test_remove_probe_dir_symlinked_workspace_cannot_bypass_guard(
    tmp_path: Path,
) -> None:
    """A non-canonical (symlinked) workspace path must not let the guard act
    on a foreign target: parent + basename are compared on the RESOLVED probe
    dir, not on an unresolved path."""
    real_workspace = tmp_path / "real_workspace"
    real_workspace.mkdir()
    link_workspace = tmp_path / "link_workspace"
    link_workspace.symlink_to(real_workspace, target_is_directory=True)

    # Right basename, but NOT directly in the workspace: a foreign sibling one
    # level down.  Reached through the symlinked workspace it is still foreign.
    foreign = real_workspace / "nested" / ".opencode_bridge_probe"
    foreign.mkdir(parents=True)
    keeper = foreign / "keep.txt"
    keeper.write_text("keep", encoding="utf-8")

    attempted = link_workspace / "nested" / ".opencode_bridge_probe"
    assert psb._remove_probe_dir(attempted, link_workspace) is False
    # Positive control: the foreign target STILL EXISTS, byte-for-byte.
    assert foreign.is_dir()
    assert keeper.read_text(encoding="utf-8") == "keep"
    assert real_workspace.is_dir()  # the resolved workspace was not touched


def test_remove_probe_dir_returns_true_when_absent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    absent = workspace / ".opencode_bridge_probe"

    assert psb._remove_probe_dir(absent, workspace) is True
    assert not absent.exists()


# -- _looks_like_question ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Which file should I read?", True),  # trailing question mark
        ("Choose a target (Options: alpha | beta)", True),  # bridge option suffix
        ("I read the file and it contains the token.", False),  # declarative
        ("   ", False),  # whitespace-only is never a question
    ],
)
def test_looks_like_question(text: str, expected: bool) -> None:
    assert psb._looks_like_question(text) is expected


# -- _named_delegate --------------------------------------------------------


def test_named_delegate_extracts_a_real_name() -> None:
    assert (
        psb._named_delegate("I delegated the write to Alpha and it applied.")
        == "Alpha"
    )


def test_named_delegate_strips_trailing_punctuation() -> None:
    # The name pattern allows '.'/'-' INSIDE a name, so a sentence-final period
    # is captured into the token; it must be stripped: "Alpha." -> "Alpha".
    assert psb._named_delegate("I delegated the write to Alpha.") == "Alpha"
    # An internal hyphen is preserved; only the trailing edge punctuation goes.
    assert psb._named_delegate("I delegated the write to Beta-X.") == "Beta-X"


def test_named_delegate_rejects_common_stop_words() -> None:
    # The regex matches, but these tokens are stop-words, not delegate names.
    # The stop word is followed by a space so it is not merged with punctuation.
    assert (
        psb._named_delegate("I delegated the write to the worker and it applied.")
        is None
    )
    assert (
        psb._named_delegate("I delegated the write to the tool and it applied.")
        is None
    )
    # A CAPITALIZED stop-word must also be rejected: the stop-word test is
    # case-folded, so capitalization cannot evade it.
    assert (
        psb._named_delegate("I delegated the write to Worker and it applied.")
        is None
    )
    assert psb._named_delegate("I delegated the write to The.") is None


def test_named_delegate_returns_none_without_a_delegate() -> None:
    assert psb._named_delegate("I wrote the file directly.") is None
    # delegation cue present but no "to <Name>" token
    assert psb._named_delegate("I delegated the write.") is None


# -- _parse_environ ---------------------------------------------------------


def test_parse_environ_nul_separated() -> None:
    assert psb._parse_environ("A=1\0B=two\0C=") == {"A": "1", "B": "two", "C": ""}


def test_parse_environ_skips_entries_without_equals() -> None:
    assert psb._parse_environ("A=1\0JUNK\0B=2") == {"A": "1", "B": "2"}


def test_parse_environ_empty_blob() -> None:
    assert psb._parse_environ("") == {}


def test_parse_environ_keeps_equals_in_value_and_accepts_newlines() -> None:
    assert psb._parse_environ("PATH=/a=b\nOTHER=x") == {"PATH": "/a=b", "OTHER": "x"}


# -- _build_prompt ----------------------------------------------------------


def test_build_prompt_carries_required_clauses() -> None:
    write_rel = ".opencode_bridge_probe/out.txt"
    known_rel = ".opencode_bridge_probe/target.txt"
    token = "PROBE-TOKEN-test"

    prompt = psb._build_prompt(known_rel, write_rel, token)

    # the relative, in-workspace write target and the known read path
    assert write_rel in prompt
    assert known_rel in prompt
    # the read-only / delegate clause
    assert "read-only" in prompt.lower()
    assert "delegate" in prompt.lower()
    # the sandbox_finish + sandbox_apply export clause
    assert "sandbox_finish" in prompt
    assert "sandbox_apply" in prompt
    # the numbered summary requirement
    assert "numbered summary" in prompt


def test_build_prompt_has_no_out_of_workspace_absolute_target() -> None:
    prompt = psb._build_prompt(
        ".opencode_bridge_probe/target.txt",
        ".opencode_bridge_probe/out.txt",
        "PROBE-TOKEN-test",
    )
    # The historical bug was an out-of-workspace ABSOLUTE write target.
    assert not _ABSOLUTE_PATH_RE.search(prompt), prompt
    assert str(Path.home()) not in prompt


# -- _acceptance_facts (the verdict engine) ---------------------------------


def test_fact1_streamed_reply_pass_fail_unknown(
    tmp_path: Path, absent_data_dir: Path,
) -> None:
    write = tmp_path / "out.txt"

    # PASS: a real assistant content chunk arrived
    facts = psb._acceptance_facts(
        psb.ProbeOutcome(received_reply=True, received_done=True, opencode_chunks=3),
        str(write), 0.0,
    )
    assert _fact(facts, "1.").status == "PASS"

    # UNKNOWN: [DONE] but no content chunk — an unchecked fact, never a PASS
    facts = psb._acceptance_facts(
        psb.ProbeOutcome(received_done=True), str(write), 0.0,
    )
    assert _fact(facts, "1.").status == "UNKNOWN"

    # FAIL: neither content nor [DONE]
    facts = psb._acceptance_facts(psb.ProbeOutcome(), str(write), 0.0)
    assert _fact(facts, "1.").status == "FAIL"


def test_fact2_tool_results_pass_unknown_fail(
    tmp_path: Path, absent_data_dir: Path,
) -> None:
    write = tmp_path / "out.txt"

    facts = psb._acceptance_facts(
        psb.ProbeOutcome(completed_tools=["glob"]), str(write), 0.0,
    )
    assert _fact(facts, "2.").status == "PASS"

    # started-but-not-done is UNKNOWN, never a PASS
    facts = psb._acceptance_facts(
        psb.ProbeOutcome(running_tools=["glob"]), str(write), 0.0,
    )
    assert _fact(facts, "2.").status == "UNKNOWN"

    facts = psb._acceptance_facts(psb.ProbeOutcome(), str(write), 0.0)
    assert _fact(facts, "2.").status == "FAIL"


def test_fact2b_delegation_pass_on_positive_stream_marker(
    tmp_path: Path, absent_data_dir: Path,
) -> None:
    facts = psb._acceptance_facts(
        psb.ProbeOutcome(delegation_signals=["task"]),
        str(tmp_path / "out.txt"), 0.0,
    )
    assert _fact(facts, "2b.").status == "PASS"


def test_fact2b_prose_only_is_unknown_never_pass(
    tmp_path: Path, absent_data_dir: Path,
) -> None:
    write = tmp_path / "out.txt"
    prose = "I delegated the write to Alpha and it applied the change."

    # prose names a delegate AND the file exists -> still NOT a PASS: the
    # positive stream marker is required, so a prose-only claim is UNKNOWN.
    write.write_text(psb._WRITE_CONTENT, encoding="utf-8")
    facts = psb._acceptance_facts(
        psb.ProbeOutcome(text=[prose]), str(write), 0.0,
    )
    fact = _fact(facts, "2b.")
    assert fact.status == "UNKNOWN"
    assert "Alpha" in fact.evidence

    # prose names a delegate but the file is ABSENT -> also UNKNOWN, never PASS
    write.unlink()
    facts = psb._acceptance_facts(
        psb.ProbeOutcome(text=[prose]), str(write), 0.0,
    )
    assert _fact(facts, "2b.").status == "UNKNOWN"


def test_fact2b_delegation_fail_when_a_write_happened_without_delegation(
    tmp_path: Path, absent_data_dir: Path,
) -> None:
    write = tmp_path / "out.txt"
    write.write_text(psb._WRITE_CONTENT, encoding="utf-8")
    facts = psb._acceptance_facts(
        psb.ProbeOutcome(completed_tools=["write"]), str(write), 0.0,
    )
    assert _fact(facts, "2b.").status == "FAIL"


def test_fact2b_delegation_unknown_when_nothing_observed(
    tmp_path: Path, absent_data_dir: Path,
) -> None:
    facts = psb._acceptance_facts(
        psb.ProbeOutcome(), str(tmp_path / "out.txt"), 0.0,
    )
    assert _fact(facts, "2b.").status == "UNKNOWN"


def test_fact3_work_on_disk_pass_fail(
    tmp_path: Path, absent_data_dir: Path,
) -> None:
    write = tmp_path / "out.txt"
    outcome = psb.ProbeOutcome()

    # absent -> FAIL
    facts = psb._acceptance_facts(outcome, str(write), 0.0)
    assert _fact(facts, "3.").status == "FAIL"

    # present but wrong content -> FAIL
    write.write_text("not-the-token", encoding="utf-8")
    facts = psb._acceptance_facts(outcome, str(write), 0.0)
    assert _fact(facts, "3.").status == "FAIL"

    # present with the exact required content -> PASS
    write.write_text(f"prefix {psb._WRITE_CONTENT} suffix", encoding="utf-8")
    facts = psb._acceptance_facts(outcome, str(write), 0.0)
    assert _fact(facts, "3.").status == "PASS"


def test_unchecked_fact_is_unknown_never_pass(
    tmp_path: Path, absent_data_dir: Path,
) -> None:
    # A fresh outcome checks nothing: 2b is UNKNOWN and 4 (session scan) is
    # UNKNOWN because the data dir is missing — neither is ever reported PASS.
    facts = psb._acceptance_facts(
        psb.ProbeOutcome(), str(tmp_path / "out.txt"), 0.0,
    )
    assert _fact(facts, "2b.").status == "UNKNOWN"
    assert _fact(facts, "4.").status == "UNKNOWN"


# -- _apply_chunk -----------------------------------------------------------


def test_apply_chunk_records_completed_tool_and_reply(
    capsys: pytest.CaptureFixture,
) -> None:
    outcome = psb.ProbeOutcome()
    psb._apply_chunk(
        {"model": "opencode", "choices": [{"delta": {"content": "✅ read done"}}]},
        outcome, "__no_marker__",
    )
    capsys.readouterr()
    assert outcome.completed_tools == ["read"]
    assert outcome.received_reply is True
    assert outcome.opencode_chunks == 1


def test_apply_chunk_records_delegation_marker(
    capsys: pytest.CaptureFixture,
) -> None:
    outcome = psb.ProbeOutcome()
    psb._apply_chunk(
        {"model": "opencode", "choices": [{"delta": {"content": "🔧 task"}}]},
        outcome, "__no_marker__",
    )
    capsys.readouterr()
    assert outcome.delegation_signals == ["task"]
    assert outcome.running_tools == []  # "task" is not in the tool-name list
    assert outcome.received_reply is True


def test_apply_chunk_marker_and_write_tool(
    capsys: pytest.CaptureFixture,
) -> None:
    outcome = psb.ProbeOutcome()
    psb._apply_chunk(
        {"model": "opencode", "choices": [{"delta": {"content": "🔧 write"}}]},
        outcome, "__no_marker__",
    )
    psb._apply_chunk(
        {"model": "opencode", "choices": [{"delta": {"content": "✅ write done"}}]},
        outcome, "__no_marker__",
    )
    capsys.readouterr()
    assert outcome.running_tools == ["write"]
    assert outcome.completed_tools == ["write"]


def test_apply_chunk_ignores_non_opencode_plain_content(
    capsys: pytest.CaptureFixture,
) -> None:
    outcome = psb.ProbeOutcome()
    # a proxy-framed chunk (model != opencode) with plain content
    psb._apply_chunk(
        {"model": "local", "choices": [{"delta": {"content": "hello"}}]},
        outcome, "__no_marker__",
    )
    # no choices at all
    psb._apply_chunk({"model": "opencode", "choices": []}, outcome, "__no_marker__")
    capsys.readouterr()
    assert outcome.completed_tools == []
    assert outcome.received_reply is False
    assert outcome.opencode_chunks == 0


def test_apply_chunk_tool_marker_is_not_model_gated(
    capsys: pytest.CaptureFixture,
) -> None:
    # OBSERVED actual behaviour: reply counting is gated on ``model=="opencode"``
    # but the tool/delegation marker folding is NOT — a marker in any chunk is
    # recorded.  Asserted as-is; the production module is out of scope.
    outcome = psb.ProbeOutcome()
    psb._apply_chunk(
        {"choices": [{"delta": {"content": "✅ read done"}}]},
        outcome, "__no_marker__",
    )
    capsys.readouterr()
    assert outcome.completed_tools == ["read"]
    assert outcome.received_reply is False
    assert outcome.opencode_chunks == 0
