#!/usr/bin/env python3
"""Live probe of the secure-opencode bridge path (bridge v4, task T8).

Checks the secure-bridge preconditions, then sends exactly ONE bridged request
through the proxy's PUBLIC API (``POST /v1/chat/completions`` with
``model: "opencode"``, streamed).  The prompt asks the agent to exercise tools
in sequence: a glob, a grep, a read of a known file, and the OUTCOME of a new
file.  Every fixture lives in the workspace-RELATIVE subdirectory
``.opencode_bridge_probe``: the glob/grep steps scope to that relative
subdirectory (the tools cannot resolve the workspace root), and the write
target ``.opencode_bridge_probe/out.txt`` is INSIDE the workspace, so it
crosses no workspace/protected-path boundary.  A read-only agent MUST delegate
the write to a writable worker through its subagent/task tool.

NOTE (glob/grep scoping): the glob and grep tools resolve an existing RELATIVE
subdirectory but NOT the workspace root — a root, ``.`` or an absolute search
path returns ``search path does not exist`` even while ``read`` succeeds there.
This is the ENVIRONMENT's normal tool scoping, not a bridge result and not a
bridge failure; the probe scopes its steps to the relative subdirectory instead
of trying to fix it.

This is a CLI: stdout IS the deliverable (the streamed reply plus the
PASS/FAIL/UNKNOWN report), so the ``print()`` calls below are the AGENTS.md
rule-9 CLI carve-out.  Diagnostics go through ``get_logger`` to stderr.

The probe never resolves a permission prompt: the secure server's own
``opencode-perm`` process does that out-of-band.  While a prompt is pending the
probe waits a bounded window, printing what it waits for, and then exits with a
distinct code.  A fully-preallowed server config (NO prompt) is a valid,
reportable outcome — not a failure.

Usage::

    .venv/bin/python scripts/probe_secure_bridge.py [--proxy-url URL] [--force] [--keep-probe-dir]

Exit codes::

    0  every precondition and every T8 acceptance fact PASSED
    1  the probe ran; at least one acceptance fact FAILED
    2  a blocking precondition FAILED (the request was NOT sent)
    3  the stream is parked on a MANUAL permission prompt (resolve, then re-run)
    4  the probe ran; outcome inconclusive (UNKNOWN only, no FAIL)
    5  the stream is parked on a QUESTION (answer it in the opencode UI, then re-run)

Parked states: a bridged turn can park in TWO ways and the probe reports which.
A pending PERMISSION is surfaced by the bridge as the ``_WAITING_PERMISSION_STATUS``
status text (exit 3).  A pending QUESTION is the bridge's ``("question", <text>)``
delta (see ``opencode_bridge.opencode_chat_stream``), which the proxy emits as the
final PLAIN (un-sentinelled) content chunk before it ends the stream; the probe
recognizes it from that streamed delta (exit 5), so a legitimate question is never
reported as an acceptance FAIL.  For either, the probe prints a boxed MANUAL STEP,
waits its bounded window, then polls the write-outcome file for a post-stream grace
before evaluating fact 3.

The stream-idle abort bound is ``--idle-timeout`` (default 600s, printed when it
fires); the post-stream file grace is ``--file-grace`` (default 120s).

The secure URL and the workspace are read from ``constants`` — never hardcoded.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

sys.path.insert(0, ".")

from constants import (  # noqa: E402  (repo-root import after sys.path fix)
    OPENCODE_AGENT,
    OPENCODE_BRIDGE_DIRECTORY,
    OPENCODE_SECURE_SERVE_URL,
    get_logger,
)

log = get_logger("probe_secure_bridge")

EXIT_OK = 0
EXIT_ACCEPTANCE_FAILED = 1
EXIT_PRECONDITION_FAILED = 2
EXIT_PERMISSION_PENDING = 3
EXIT_UNKNOWN = 4
EXIT_QUESTION_PENDING = 5

#: Tool names the bridge announces with ``✅ <name> done`` /
#: ``🔧 <name>`` (opencode_bridge._yield_part_deltas).  Duplicated so the probe
#: stays import-light and runs even if the bridge module cannot import.
_TOOLS = ("glob", "grep", "read", "write")
#: Tool names for a subagent/delegation invocation.  The opencode delegation
#: tool is ``task`` (``subagent`` covers an alias); the bridge renders any tool
#: as ``✅ <name> done`` / ``🔧 <name>`` / ``⚠️ <name> failed``.
_DELEGATION_TOOLS = ("task", "subagent")
#: Exact content the agent must write to the in-workspace outcome file.
_WRITE_CONTENT = "secure-bridge-probe-ok"
#: Prefix of the token in the known read file.
_READ_SENTINEL = "PROBE-TOKEN-"
#: Probe fixture directory, RELATIVE to the bridge workspace.  The glob/grep
#: tools resolve an existing relative subdirectory but NOT the workspace root
#: (see the module NOTE), and a relative in-workspace write crosses no
#: workspace/protected-path boundary.
_PROBE_DIRNAME = ".opencode_bridge_probe"
#: Token file the glob/grep/read steps target, relative to the workspace.
_PROBE_TARGET_REL = f"{_PROBE_DIRNAME}/target.txt"
#: Write-outcome file, relative to the workspace (INSIDE it).
_PROBE_OUT_REL = f"{_PROBE_DIRNAME}/out.txt"
#: Data dir the opencode session store + log live under.
_OPENCODE_DATA_DIR = os.path.expanduser("~/.local/share/opencode")


@dataclass
class Check:
    """One precondition or acceptance fact with its observed evidence."""

    name: str
    status: str  # "PASS" | "FAIL" | "UNKNOWN"
    evidence: str


@dataclass
class ProbeOutcome:
    """Everything observed on the single bridged request."""

    received_reply: bool = False
    received_done: bool = False
    waiting_permission: bool = False
    parked_and_timed_out: bool = False
    parked_question: bool = False
    question_text: str = ""
    last_plain_content: str = ""
    idle_timed_out: bool = False
    opencode_chunks: int = 0
    completed_tools: list[str] = field(default_factory=list)
    running_tools: list[str] = field(default_factory=list)
    failed_tools: list[str] = field(default_factory=list)
    delegation_signals: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    stream_error: str = ""


def _print_check(check: Check) -> None:
    print(f"[{check.status:<7}] {check.name}: {check.evidence}")


def _waiting_permission_marker() -> str:
    """Canonical waiting-permission text from the bridge; stable fallback."""
    try:
        from opencode_bridge import _WAITING_PERMISSION_STATUS
    except ImportError:
        return "waiting on a permission prompt"
    return str(_WAITING_PERMISSION_STATUS)


#: The bridge's question-delta option suffix: ``_yield_part_deltas`` renders a
#: question tool's option labels as ``"<question> (Options: a | b)"``.
_QUESTION_OPTIONS_MARK = "(Options: "


def _looks_like_question(text: str) -> bool:
    """True when ``text`` looks like the bridge's ``("question", <text>)`` delta.

    The proxy turns a question delta into PLAIN (un-sentinelled) assistant
    content and then ends the stream, so on the wire the only distinguishing
    shape is the text itself: the bridge renders a question tool's text as its
    body (optionally suffixed with ``" (Options: …)"``) or, for a multi-group
    preflight, ``"<header>: <body>"`` lines, and an agent's clarifying question
    ends with ``?``.  Conservative — whitespace-only is never a question, and a
    trailing ``?`` or the option suffix is required.  Deliberately a shape
    heuristic: the bridge does not tag the delta on the wire.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    return _QUESTION_OPTIONS_MARK in text


# -- Preconditions ----------------------------------------------------------


def _systemctl_value(unit: str, prop: str) -> Optional[str]:
    """Read one ``systemctl show`` property, or None when unavailable."""
    try:
        proc = subprocess.run(
            ["systemctl", "show", unit, f"--property={prop}", "--value"],
            capture_output=True, text=True, timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("systemctl show %s %s unavailable: %s", unit, prop, exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _parse_environ(blob: str) -> dict[str, str]:
    """Parse a NUL- or newline-separated KEY=VALUE environment blob."""
    env: dict[str, str] = {}
    for entry in blob.replace("\n", "\0").split("\0"):
        if "=" in entry:
            key, _, val = entry.partition("=")
            env[key] = val
    return env


def _proxy_backend_selection(unit: str) -> Check:
    """Read OPENCODE_BACKEND from the running proxy's environment.

    Tries the unit's MainPID ``/proc/<pid>/environ`` (authoritative), then the
    unit's configured ``Environment=``, then this probe's own env.  Never
    assumes: an unset/unreadable value is reported as such.
    """
    name = "proxy OPENCODE_BACKEND=secure"
    observations: list[str] = []
    value: Optional[str] = None
    source = ""
    pid_text = _systemctl_value(unit, "MainPID")
    if pid_text and pid_text.isdigit() and int(pid_text) > 0:
        try:
            blob = Path(f"/proc/{pid_text}/environ").read_bytes().decode(
                "utf-8", "replace",
            )
            env = _parse_environ(blob)
            observations.append(
                f"{unit} MainPID {pid_text} env={env.get('OPENCODE_BACKEND')!r}"
            )
            if "OPENCODE_BACKEND" in env:
                value, source = env["OPENCODE_BACKEND"], f"{unit} MainPID env"
        except OSError as exc:
            observations.append(f"/proc/{pid_text}/environ unreadable: {exc!r}")
    else:
        observations.append(f"{unit} MainPID={pid_text!r}")
    if value is None:
        env_blob = _systemctl_value(unit, "Environment")
        if env_blob is not None:
            parsed = _parse_environ(env_blob)
            observations.append(f"{unit} Environment={parsed.get('OPENCODE_BACKEND')!r}")
            if "OPENCODE_BACKEND" in parsed:
                value, source = parsed["OPENCODE_BACKEND"], f"{unit} Environment"
    observations.append(f"probe process env={os.environ.get('OPENCODE_BACKEND')!r}")
    evidence = "; ".join(observations)
    if value == "secure":
        return Check(name, "PASS", f"{source} -> 'secure'")
    if value is not None:
        return Check(name, "FAIL", f"{source} -> {value!r} (NOT 'secure'); {evidence}")
    return Check(name, "UNKNOWN", f"not readable from the proxy process; {evidence}")


async def _check_secure_server(
    client: httpx.AsyncClient, base_url: str,
) -> tuple[Check, Optional[dict[str, Any]]]:
    """GET the secure server's ``/config`` with a bounded timeout."""
    name = "secure server answers GET /config"
    target = f"{base_url.rstrip('/')}/config"
    try:
        resp = await client.get(target, timeout=5.0)
    except (httpx.HTTPError, OSError) as exc:
        return Check(name, "FAIL", f"GET {target} failed: {exc!r}"), None
    if resp.status_code != 200:
        return Check(name, "FAIL", f"GET {target} -> HTTP {resp.status_code}"), None
    try:
        body = resp.json()
    except ValueError as exc:
        return Check(name, "PASS", f"GET {target} -> HTTP 200 (non-JSON: {exc!r})"), None
    if isinstance(body, dict):
        return Check(
            name, "PASS",
            f"GET {target} -> HTTP 200 (JSON object, {len(body)} top-level keys)",
        ), body
    return Check(name, "PASS", f"GET {target} -> HTTP 200 (JSON {type(body).__name__})"), None


def _check_workspace(path: str) -> Check:
    name = "bridge workspace exists"
    p = Path(path)
    if p.is_dir():
        return Check(name, "PASS", f"{path} is a directory")
    if p.exists():
        return Check(name, "FAIL", f"{path} exists but is not a directory")
    return Check(name, "FAIL", f"{path} does not exist")


def _check_registration(workspace: str) -> Check:
    """Look for the workspace artifacts ``register-project.ts`` creates.

    Cannot prove a restart from here; a missing artifact is REPORTED as
    pre-restart rather than guessed.
    """
    name = "registration activated (broker/secure restarted)"
    ws = Path(workspace)
    atl = (ws / ".atl").is_dir()
    cg = (ws / ".codegraph").is_dir()
    if atl and cg:
        return Check(name, "PASS", f"{workspace} has .atl/ and .codegraph/")
    missing = [n for n, ok in ((".atl", atl), (".codegraph", cg)) if not ok]
    return Check(
        name, "FAIL",
        f"{workspace} is missing {', '.join(missing)} — the broker/secure services "
        "look PRE-RESTART (or registration did not complete); the broker env + "
        "launcher root cannot be inspected from this repo",
    )


# -- The single bridged request ---------------------------------------------


def _build_prompt(
    known_rel: Optional[str], write_rel: str, token: str,
) -> str:
    """The explicit tool-sequence prompt for the one bridged request.

    Every path is RELATIVE to the bridge workspace and scoped to the
    ``.opencode_bridge_probe`` subdirectory: the glob/grep tools resolve an
    existing relative subdirectory but NOT the workspace root, and a relative
    in-workspace write crosses no workspace/protected-path boundary.
    """
    lines = [
        "This is an automated probe of the secure opencode bridge. Execute ALL of the",
        "following steps IN ORDER using your tools. Do not skip any step. Narrate each",
        "step in one short sentence.",
        "",
        '1. GLOB: use the glob tool with the pattern "**/*" (or "*") in the RELATIVE',
        f"   directory {_PROBE_DIRNAME} (inside the current workspace) and report one",
        "   path you found.",
        "2. GREP: use the grep tool to search for the exact string "
        f'"{token}" in the RELATIVE directory {_PROBE_DIRNAME} and report the match.',
    ]
    if known_rel:
        lines += [
            f"3. READ: use the read tool to read the file {known_rel} and report the",
            f'   token it contains (it contains "{token}").',
        ]
    else:
        lines += [
            "3. READ: use the read tool to read any small text file you found in step 1",
            "   under the relative directory and report its first line.",
        ]
    lines += [
        f"4. WRITE OUTCOME: the file at the workspace-RELATIVE path {write_rel} must",
        f"   exist with exactly this content: {_WRITE_CONTENT}",
        "   The target is INSIDE the current workspace and is RELATIVE (not an absolute",
        "   or out-of-workspace path), so there is no boundary to cross.",
        "   You may be a read-only agent: if you have no write/edit/apply_patch tool of",
        "   your own, you MUST delegate this write to a writable worker using your",
        "   subagent / delegation (task) tool, and make sure the delegated write lands.",
        "   A write made ONLY inside a worker's sandbox does NOT land on the host.",
        "   Instruct your delegate to COMPLETE the write and then EXPORT it by running",
        "   sandbox_finish followed by sandbox_apply (the operator approves the apply).",
        "",
        "Finally reply with a short numbered summary: for each step, name the tool you",
        "used and whether it completed. For step 4 say whether you wrote directly or",
        "delegated; if you delegated, name the delegate and state whether the delegate",
        "finished the write AND applied it (sandbox_finish + sandbox_apply).",
    ]
    return "\n".join(lines)


def _apply_chunk(chunk: dict[str, Any], outcome: ProbeOutcome, marker: str) -> None:
    """Fold one SSE chunk into the outcome; print assistant content to stdout."""
    choices = chunk.get("choices") or []
    if not choices:
        return
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if not content:
        return
    is_opencode = str(chunk.get("model") or "") == "opencode"
    is_status = content.startswith("\u200b")  # STATUS_SENTINEL: proxy status text
    text = content.lstrip("\u200b")
    print(text if is_opencode else f"(proxy) {text}", end="", flush=True)
    if is_opencode and text:
        outcome.opencode_chunks += 1
        outcome.text.append(text)
        if text.strip():
            outcome.received_reply = True
        if not is_status:
            # Plain assistant content: the candidate for the bridge's terminal
            # ("question", <text>) delta (the proxy emits it un-sentinelled and
            # then ends the stream).
            outcome.last_plain_content = text
    if marker in text:
        outcome.waiting_permission = True
    for tool in _TOOLS:
        if f"✅ {tool} done" in text and tool not in outcome.completed_tools:
            outcome.completed_tools.append(tool)
        if f"🔧 {tool}" in text and tool not in outcome.running_tools:
            outcome.running_tools.append(tool)
        if f"⚠️ {tool} failed" in text and tool not in outcome.failed_tools:
            outcome.failed_tools.append(tool)
    for tool in _DELEGATION_TOOLS:
        if tool in outcome.delegation_signals:
            continue
        if (
            f"✅ {tool} done" in text
            or f"🔧 {tool}" in text
            or f"⚠️ {tool} failed" in text
        ):
            outcome.delegation_signals.append(tool)


async def _pump_lines(resp: httpx.Response, queue: "asyncio.Queue[Optional[str]]") -> None:
    """Forward SSE lines to the queue; None marks end-of-stream."""
    try:
        async for line in resp.aiter_lines():
            await queue.put(line)
    except (httpx.HTTPError, OSError) as exc:
        await queue.put(f"__ERROR__:{exc!r}")
    finally:
        await queue.put(None)


async def _stream_request(
    args: argparse.Namespace, prompt: str, marker: str,
) -> ProbeOutcome:
    """Send the ONE bridged request and read the SSE stream with bounds.

    A reader task stays live across our wait timeouts so a parked (silent)
    stream is never torn down by a per-read timeout.
    """
    outcome = ProbeOutcome()
    url = f"{args.proxy_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": "opencode",
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    start = time.monotonic()
    last_activity = start
    park_deadline: Optional[float] = None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(args.total_timeout, connect=10.0),
    ) as client:
        try:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    outcome.stream_error = f"HTTP {resp.status_code}"
                    return outcome
                queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
                reader = asyncio.create_task(_pump_lines(resp, queue))
                try:
                    while True:
                        now = time.monotonic()
                        if park_deadline is not None and now >= park_deadline:
                            outcome.parked_and_timed_out = True
                            break
                        if now - start >= args.total_timeout:
                            outcome.stream_error = f"total timeout {args.total_timeout:.0f}s"
                            break
                        budget = args.read_timeout
                        if park_deadline is not None:
                            budget = min(budget, max(0.5, park_deadline - now))
                        # Cap the read budget by the remaining idle allowance so
                        # the idle abort fires close to --idle-timeout, not a
                        # whole --read-timeout past it.
                        idle_left = args.idle_timeout - (now - last_activity)
                        budget = min(budget, max(0.5, idle_left))
                        try:
                            item = await asyncio.wait_for(queue.get(), timeout=budget)
                        except asyncio.TimeoutError:
                            now = time.monotonic()
                            question_pending = _looks_like_question(
                                outcome.last_plain_content,
                            )
                            if outcome.waiting_permission or question_pending:
                                if park_deadline is None:
                                    park_deadline = now + args.prompt_wait
                                    print(flush=True)
                                    if outcome.waiting_permission:
                                        print(
                                            "⏳ parked on a permission prompt; waiting "
                                            f"{args.prompt_wait:.0f}s for you to resolve it "
                                            "out-of-band (opencode-perm)…",
                                            flush=True,
                                        )
                                    else:
                                        outcome.parked_question = True
                                        outcome.question_text = outcome.last_plain_content
                                        print(
                                            "⏳ parked on a QUESTION; waiting "
                                            f"{args.prompt_wait:.0f}s for you to answer it "
                                            "in the opencode UI…",
                                            flush=True,
                                        )
                                else:
                                    log.info("still parked; %.0fs left", park_deadline - now)
                                continue
                            if now - last_activity >= args.idle_timeout:
                                outcome.idle_timed_out = True
                                outcome.stream_error = (
                                    f"stream idle > {args.idle_timeout:.0f}s "
                                    "(--idle-timeout; no SSE chunk received)"
                                )
                                break
                            continue
                        last_activity = time.monotonic()
                        if item is None:
                            break
                        if item.startswith("__ERROR__:"):
                            outcome.stream_error = item[len("__ERROR__:"):]
                            break
                        if not item.startswith("data:"):
                            continue  # comments/blank lines
                        data = item[len("data:"):].strip()
                        if data == "[DONE]":
                            outcome.received_done = True
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if isinstance(chunk, dict):
                            _apply_chunk(chunk, outcome, marker)
                finally:
                    reader.cancel()
        except (httpx.HTTPError, OSError) as exc:
            outcome.stream_error = repr(exc)
    # A parked QUESTION ends the stream: the proxy emits the bridge's
    # ("question", <text>) delta as plain content and then stops.  Recognize it
    # from the streamed delta so a legitimate question is never an acceptance
    # FAIL (a permission park is reported separately).
    if not outcome.waiting_permission and _looks_like_question(
        outcome.last_plain_content,
    ):
        outcome.parked_question = True
        outcome.question_text = outcome.last_plain_content
    print(flush=True)
    return outcome


# -- Post-request acceptance facts ------------------------------------------


def _read_write_file(path: str) -> tuple[bool, str]:
    try:
        return True, Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, ""


async def _await_write_outcome(
    path: str, timeout: float, *, interval: float = 2.0,
) -> tuple[bool, float]:
    """Poll ``path`` until it exists or ``timeout`` seconds elapse.

    The delegated write can land AFTER the stream ends (the agent parked on a
    question the operator answered, or a late worker write), so fact 3 ("work
    is on disk") is evaluated only after this grace window.  Returns
    ``(found, elapsed_seconds)``; a non-positive ``timeout`` checks once and
    returns immediately.
    """
    start = time.monotonic()
    while True:
        found, _ = _read_write_file(path)
        elapsed = time.monotonic() - start
        if found or timeout <= 0 or elapsed >= timeout:
            return found, elapsed
        await asyncio.sleep(min(interval, max(0.05, timeout - elapsed)))


def _scan_sessions(data_dir: Path, since: float, *, cap: int = 20000) -> Optional[str]:
    """Newest ``ses_*`` artifact under ``data_dir`` modified at/after ``since``."""
    if not data_dir.is_dir():
        return None
    newest: Optional[tuple[float, str]] = None
    seen = 0
    for root, dirs, files in os.walk(data_dir):
        for name in dirs + files:
            seen += 1
            if seen > cap:
                return newest[1] if newest else None
            if "ses_" not in name:
                continue
            p = Path(root) / name
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime >= since and (newest is None or mtime > newest[0]):
                newest = (mtime, str(p))
    return newest[1] if newest else None


#: Delegation verbs/phrases the agent's own summary may use.
_DELEGATION_CUES = ("delegat", "subagent", "sub-agent", "handed off", "handed over")
#: A delegation statement that NAMES the delegate (``... to <Name>``).  Used
#: only as a prose fallback: never a PASS basis without the file on disk.
_DELEGATE_NAME_RE = re.compile(
    r"(?:delegat\w*|handed (?:off|over))\s+"
    r"(?:the\s+\S+\s+)?to\s+(?:the\s+)?([A-Za-z][A-Za-z0-9_.-]{2,})",
    re.IGNORECASE,
)
#: Tokens that are NOT delegate names (English words a delegation sentence uses).
_NOT_A_DELEGATE = frozenset({
    "the", "a", "an", "my", "its", "our", "your", "it", "them", "him", "her",
    "you", "us", "me", "worker", "writable", "subagent", "sub-agent", "agent",
    "delegate", "task", "tool", "write", "writes", "writer", "file", "path",
    "sandbox", "one", "someone", "another",
})


def _named_delegate(text: str) -> Optional[str]:
    """Return the delegate name when the summary says it delegated to one.

    Conservative prose-only fallback: requires a delegation cue AND a
    ``delegated ... to <Name>`` token that is not a common English word.  The
    stream marker is the primary evidence; this supports a PASS only when the
    file on disk also exists.
    """
    if not any(cue in text.lower() for cue in _DELEGATION_CUES):
        return None
    match = _DELEGATE_NAME_RE.search(text)
    if match is None:
        return None
    name = match.group(1)
    return None if name.lower() in _NOT_A_DELEGATE else name


def _acceptance_facts(
    outcome: ProbeOutcome, write_path: str, since: float,
) -> list[Check]:
    """The T8 acceptance facts: the five originals plus a delegation fact 2b
    after fact 2, each PASS/FAIL/UNKNOWN with evidence."""
    facts: list[Check] = []

    if outcome.received_reply:
        facts.append(Check(
            "1. streamed reply arrived", "PASS",
            f"{outcome.opencode_chunks} content chunk(s); [DONE]={outcome.received_done}",
        ))
    elif outcome.received_done:
        facts.append(Check(
            "1. streamed reply arrived", "UNKNOWN",
            "[DONE] received but no assistant content chunk",
        ))
    else:
        facts.append(Check(
            "1. streamed reply arrived", "FAIL",
            f"no streamed content (stream_error={outcome.stream_error or 'none'})",
        ))

    if outcome.completed_tools:
        facts.append(Check(
            "2. tool results are in the reply", "PASS",
            f"completed: {', '.join(outcome.completed_tools)}",
        ))
    elif outcome.running_tools:
        facts.append(Check(
            "2. tool results are in the reply", "UNKNOWN",
            f"started but not completed: {', '.join(outcome.running_tools)}",
        ))
    else:
        facts.append(Check(
            "2. tool results are in the reply", "FAIL",
            "no '✅ <tool> done' marker observed in the stream",
        ))

    ok, content = _read_write_file(write_path)
    named_delegate = _named_delegate("".join(outcome.text))
    stream_write = (
        "write" in outcome.completed_tools or "write" in outcome.running_tools
    )
    if outcome.delegation_signals:
        facts.append(Check(
            "2b. delegation observed", "PASS",
            "stream shows a subagent/delegation tool marker: "
            + ", ".join(outcome.delegation_signals),
        ))
    elif named_delegate and ok:
        facts.append(Check(
            "2b. delegation observed", "PASS",
            f"no stream marker; the agent's summary names the delegate "
            f"{named_delegate!r} AND {write_path} exists (prose + on-disk sources)",
        ))
    elif stream_write or ok:
        source = "a write tool marker" if stream_write else f"the on-disk file {write_path}"
        facts.append(Check(
            "2b. delegation observed", "FAIL",
            f"no delegation observed, but the write outcome is present ({source}); "
            "the read-only agent was expected to delegate the write",
        ))
    else:
        facts.append(Check(
            "2b. delegation observed", "UNKNOWN",
            "the stream shows neither a write nor a delegation",
        ))

    if ok and _WRITE_CONTENT in content:
        facts.append(Check(
            "3. work is on disk", "PASS", f"{write_path} contains {_WRITE_CONTENT!r}",
        ))
    elif ok:
        facts.append(Check(
            "3. work is on disk", "FAIL",
            f"{write_path} exists but content={content[:120]!r}",
        ))
    else:
        facts.append(Check("3. work is on disk", "FAIL", f"{write_path} does not exist"))

    data_dir = Path(_OPENCODE_DATA_DIR)
    session = _scan_sessions(data_dir, since)
    if session:
        facts.append(Check(
            "4. opencode session exists", "PASS", f"new session artifact: {session}",
        ))
    elif data_dir.is_dir():
        facts.append(Check(
            "4. opencode session exists", "FAIL",
            f"no 'ses_' artifact modified since request start under {data_dir}",
        ))
    else:
        facts.append(Check("4. opencode session exists", "UNKNOWN", f"{data_dir} missing"))

    log_path = data_dir / "log" / "opencode.log"
    if log_path.is_file():
        facts.append(Check("5. log file exists", "PASS", str(log_path)))
    else:
        facts.append(Check("5. log file exists", "FAIL", f"{log_path} missing"))

    return facts


def _print_manual_instruction(marker: str) -> None:
    """The out-of-band resolution instruction (NOT automatable)."""
    print()
    print("=" * 74)
    print("MANUAL STEP — the secure server parked the session on a permission prompt.")
    print(f"  observed status: {marker}")
    print("The proxy cannot answer it (SecureBackend.answers_permissions=False) and will")
    print("not POST a reply. Resolve it OUT-OF-BAND, then re-run this probe.")
    print()
    print("  opencode-perm once <id>     # grant once (recommended for the probe)")
    print("  opencode-perm deny <id>     # reject")
    print()
    print("<id> is the pending permission id shown by opencode-perm (it tails")
    print("~/.local/share/opencode/log/opencode.log) or by the opencode UI.")
    print()
    print("NOTE: this invocation form is taken from the bridge-v4 tracker's T0 mapping")
    print("(odd/tasks/bridge-v4.md); the probe did NOT verify opencode-perm's own CLI.")
    print("If opencode-perm is unavailable, resolve the prompt in the opencode UI.")
    print("=" * 74)


def _print_question_instruction(question_text: str) -> None:
    """The out-of-band instruction for a parked QUESTION (NOT automatable)."""
    print()
    print("=" * 74)
    print("MANUAL STEP — the secure server parked the session on a QUESTION.")
    shown = question_text.strip() or "(question text unavailable)"
    print(f"  the agent asks: {shown}")
    print()
    print("The agent is waiting for YOUR answer; the probe will NOT answer it. Answer")
    print("the agent in the opencode UI (the session is pinned, so your reply resumes")
    print("the same agent).  The probe keeps waiting inside its bounded window for the")
    print("answer and a late write, then exits 5 (parked on a question) rather than")
    print("reporting a FAIL for a legitimate question.")
    print()
    print("Re-run this probe after answering if it exits 5.")
    print("=" * 74)


# -- Entry -------------------------------------------------------------------


def _remove_probe_dir(probe_dir: Path, workspace: Path) -> bool:
    """Best-effort removal of the probe fixture subdirectory; never raises.

    HARD GUARD: the ONLY path this may remove is exactly
    ``<workspace>/.opencode_bridge_probe`` — that directory name in that parent,
    and not a symlink.  A path that fails the guard is refused and logged, so a
    mis-built target can never rmtree the workspace or any path outside the
    probe's own fixture directory.  Returns True when the entry is absent
    afterwards (removed, or already gone) and False when it still exists.

    Removal failure is logged and swallowed: the probe's result is already
    decided, and a leftover fixture directory must not fail a run.
    """
    if (
        probe_dir.name != _PROBE_DIRNAME
        or probe_dir.parent.resolve() != workspace.resolve()
        or probe_dir.is_symlink()
    ):
        log.warning(
            "refusing to remove %s: it is not exactly %s/%s (or is a symlink)",
            probe_dir, workspace, _PROBE_DIRNAME,
        )
        return not os.path.lexists(probe_dir)
    if not probe_dir.exists():
        return True
    try:
        shutil.rmtree(probe_dir)
    except OSError as exc:
        log.warning("probe cleanup could not remove %s: %s", probe_dir, exc)
    return not os.path.lexists(probe_dir)


async def _amain(args: argparse.Namespace) -> int:
    print("=== secure-bridge probe (bridge v4 / T8) ===")
    print(f"proxy URL : {args.proxy_url}")
    print(f"secure URL: {OPENCODE_SECURE_SERVE_URL}")
    print(f"workspace : {OPENCODE_BRIDGE_DIRECTORY}")
    print(f"agent     : {OPENCODE_AGENT}")
    print(f"idle bound: {args.idle_timeout:.0f}s (--idle-timeout); "
          f"file grace: {args.file_grace:.0f}s (--file-grace)")
    print()

    print("-- preconditions --")
    backend = _proxy_backend_selection(args.proxy_unit)
    _print_check(backend)
    workspace = _check_workspace(OPENCODE_BRIDGE_DIRECTORY)
    _print_check(workspace)
    registration = _check_registration(OPENCODE_BRIDGE_DIRECTORY)
    _print_check(registration)
    async with httpx.AsyncClient() as client:
        secure, config_body = await _check_secure_server(client, OPENCODE_SECURE_SERVE_URL)
    _print_check(secure)
    print()

    blocking: list[Check] = [c for c in (backend, secure, workspace) if c.status == "FAIL"]
    if registration.status != "PASS" and not args.force:
        blocking.append(registration)
    if blocking:
        print("BLOCKED — a precondition failed; the bridged request was NOT sent.")
        for check in blocking:
            _print_check(check)
        print("Fix the precondition (or pass --force for the registration gate) and re-run.")
        return EXIT_PRECONDITION_FAILED
    if registration.status != "PASS":
        print("WARNING: registration activation is not proven (--force). Continuing.")
    if backend.status == "UNKNOWN":
        print("WARNING: OPENCODE_BACKEND=secure could not be confirmed; continuing.")

    if config_body is not None:
        agents = config_body.get("agent")
        names = sorted(agents) if isinstance(agents, dict) else []
        print(f"secure /config top-level keys: {sorted(config_body)}")
        if names:
            print(f"secure /config agents: {names}")
        print("mitigation-transfer (manual, T6 advisory R4-prompt-mitigation-transfer): the")
        print(f"probe does NOT assert it — confirm the selected agent ({OPENCODE_AGENT})")
        print("carries the bash-first/retry-once tool discipline and the multi-step")
        print("work-continuity expectation in the server's own config.")
        print()

    ws = Path(OPENCODE_BRIDGE_DIRECTORY)
    token = f"{_READ_SENTINEL}{os.getpid():x}{int(time.time()):x}"
    probe_dir = ws / _PROBE_DIRNAME
    print(f"probe dir : {probe_dir}")
    if args.keep_probe_dir:
        print("            start cleanup SKIPPED (--keep-probe-dir)")
    else:
        stale = probe_dir.exists() or probe_dir.is_symlink()
        if _remove_probe_dir(probe_dir, ws):
            print(
                "            start cleanup: removed a STALE probe dir"
                if stale else "            start cleanup: nothing stale to remove"
            )
        else:
            print(
                f"            start cleanup FAILED — {probe_dir} could not be removed; "
                "a stale out.txt would corrupt fact 3 (see the log)",
            )
    known_rel: Optional[str] = _PROBE_TARGET_REL
    known_path = probe_dir / "target.txt"
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
        known_path.write_text(f"{token}\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not create the read-target file %s: %s", known_path, exc)
        known_rel = None

    write_rel = args.write_path or _PROBE_OUT_REL
    write_path = Path(write_rel)
    if not write_path.is_absolute():
        write_path = ws / write_path
    else:
        log.warning(
            "write path %s is ABSOLUTE — the probe expects an inside-workspace "
            "RELATIVE target", write_rel,
        )

    prompt = _build_prompt(known_rel, write_rel, token)
    log.info("probe prompt:\n%s", prompt)
    print("-- bridged request (model=opencode, streamed) --")
    print(prompt)
    print("-- reply stream --")

    since = time.time()
    marker = _waiting_permission_marker()
    try:
        outcome = await _stream_request(args, prompt, marker)

        print()
        if outcome.stream_error:
            log.warning("stream error: %s", outcome.stream_error)

        # Still parked on a permission after the in-stream wait: report the
        # MANUAL STEP and exit 3 (no facts to evaluate — the turn never ran).
        if outcome.waiting_permission and outcome.parked_and_timed_out:
            _print_manual_instruction(marker)
            return EXIT_PERMISSION_PENDING

        # Post-stream file grace (after a normal end, a parked question or an
        # idle abort): a late delegated write must be seen before fact 3.
        grace_found, grace_waited = await _await_write_outcome(
            str(write_path), args.file_grace,
        )
        print(
            f"-- post-stream file grace -- polled {write_path} for "
            f"{grace_waited:.0f}s of a {args.file_grace:.0f}s bound "
            f"({'found' if grace_found else 'not present yet'})",
        )

        if outcome.parked_question and not grace_found:
            # The run is still parked on a question: report the MANUAL STEP and
            # keep waiting inside the bounded --prompt-wait window for the
            # operator's answer and the write it triggers.
            _print_question_instruction(outcome.question_text)
            remaining = max(0.0, args.prompt_wait - grace_waited)
            if remaining > 0:
                print(
                    f"-- parked-question wait -- polling {write_path} up to "
                    f"{remaining:.0f}s more (--prompt-wait)…",
                )
                grace_found, extra = await _await_write_outcome(
                    str(write_path), remaining,
                )
                print(
                    f"-- parked-question wait -- polled {extra:.0f}s "
                    f"({'found' if grace_found else 'still not present'})",
                )
            if not grace_found:
                print()
                print(
                    "RESULT: exit 5 (parked on a question — answer it in the "
                    "opencode UI, then re-run)",
                )
                return EXIT_QUESTION_PENDING

        if outcome.waiting_permission and not outcome.completed_tools:
            # Not timed out: the prompt may have been resolved after the stream
            # ended.  Report the MANUAL STEP, then evaluate the final facts.
            _print_manual_instruction(marker)

        print("-- T8 acceptance facts --")
        facts = _acceptance_facts(outcome, str(write_path), since)
        exit_code = EXIT_OK
        for fact in facts:
            _print_check(fact)
            if fact.status == "FAIL":
                exit_code = EXIT_ACCEPTANCE_FAILED
            elif fact.status == "UNKNOWN" and exit_code == EXIT_OK:
                exit_code = EXIT_UNKNOWN
        if registration.status != "PASS" and exit_code == EXIT_OK:
            exit_code = EXIT_ACCEPTANCE_FAILED
        print()
        print(f"RESULT: exit {exit_code}")
        return exit_code
    finally:
        if args.keep_probe_dir:
            print(
                f"-- cleanup: SKIPPED (--keep-probe-dir); probe dir left at {probe_dir} --",
            )
        elif _remove_probe_dir(probe_dir, ws):
            print(f"-- cleanup: removed the probe dir {probe_dir} --")
        else:
            print(
                f"-- cleanup: NOT removed — {probe_dir} still exists. A late delegated "
                "write export (sandbox_finish/sandbox_apply) can complete after this "
                "probe exits; see the log for the OS error.",
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Live probe of the secure-opencode bridge path (bridge v4, T8).",
    )
    parser.add_argument(
        "--proxy-url", default=os.environ.get("PROBE_PROXY_URL", "http://127.0.0.1:13000"),
        help="proxy public base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--proxy-unit", default="ai-proxy.service",
        help="systemd unit whose env is checked for OPENCODE_BACKEND",
    )
    parser.add_argument(
        "--write-path", default=None,
        help="workspace-RELATIVE file the agent must write "
             f"(default: {_PROBE_OUT_REL})",
    )
    parser.add_argument(
        "--prompt-wait", type=float, default=600.0,
        help="seconds to wait for a manual permission resolution OR a parked "
             "question to be answered (default: %(default)s)",
    )
    parser.add_argument(
        "--file-grace", type=float, default=120.0,
        help="seconds to poll the write-outcome file after the stream ends, "
             "before evaluating fact 3 (default: %(default)s)",
    )
    parser.add_argument(
        "--idle-timeout", type=float, default=600.0,
        help="stream-idle abort bound: no SSE chunk for this many seconds ends "
             "the stream with the precise reason (default: %(default)s)",
    )
    parser.add_argument(
        "--read-timeout", type=float, default=90.0,
        help="per-chunk SSE read budget in seconds; capped by --idle-timeout "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--total-timeout", type=float, default=900.0,
        help="total request budget in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="bypass the registration-activation gate",
    )
    parser.add_argument(
        "--keep-probe-dir", action="store_true",
        help="skip BOTH the start-time stale-directory cleanup and the end-of-run "
             "cleanup, leaving .opencode_bridge_probe in the workspace for debugging",
    )
    parser.add_argument("--verbose", action="store_true", help="debug logs on stderr")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        log.warning("interrupted")
        return EXIT_UNKNOWN


if __name__ == "__main__":
    sys.exit(main())
