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

    .venv/bin/python scripts/probe_secure_bridge.py [--proxy-url URL] [--force]

Exit codes::

    0  every precondition and every T8 acceptance fact PASSED
    1  the probe ran; at least one acceptance fact FAILED
    2  a blocking precondition FAILED (the request was NOT sent)
    3  the stream is parked on a MANUAL permission prompt (resolve, then re-run)
    4  the probe ran; outcome inconclusive (UNKNOWN only, no FAIL)

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
        "",
        "Finally reply with a short numbered summary: for each step, name the tool you",
        "used and whether it completed. For step 4 say whether you wrote directly or",
        "delegated, and if you delegated, name the delegate.",
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
    text = content.lstrip("\u200b")
    print(text if is_opencode else f"(proxy) {text}", end="", flush=True)
    if is_opencode and text:
        outcome.opencode_chunks += 1
        outcome.text.append(text)
        if text.strip():
            outcome.received_reply = True
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
                        try:
                            item = await asyncio.wait_for(queue.get(), timeout=budget)
                        except asyncio.TimeoutError:
                            if outcome.waiting_permission:
                                if park_deadline is None:
                                    park_deadline = now + args.prompt_wait
                                    print(flush=True)
                                    print(
                                        "⏳ parked on a permission prompt; waiting "
                                        f"{args.prompt_wait:.0f}s for you to resolve it "
                                        "out-of-band (opencode-perm)…",
                                        flush=True,
                                    )
                                else:
                                    log.info("still parked; %.0fs left", park_deadline - now)
                                continue
                            outcome.stream_error = f"stream idle > {args.read_timeout:.0f}s"
                            break
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
    print(flush=True)
    return outcome


# -- Post-request acceptance facts ------------------------------------------


def _read_write_file(path: str) -> tuple[bool, str]:
    try:
        return True, Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, ""


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


# -- Entry -------------------------------------------------------------------


def _cleanup_probe_dir(probe_dir: Path) -> None:
    """Best-effort removal of the probe fixture subdirectory; never raises.

    Runs from the entry point's ``finally`` so a failed or partial run still
    cleans up.  Removal failure is logged and swallowed: the probe's result is
    already decided, and a leftover fixture directory must not fail a run.
    """
    try:
        shutil.rmtree(probe_dir)
    except OSError as exc:
        log.warning("probe cleanup could not remove %s: %s", probe_dir, exc)


async def _amain(args: argparse.Namespace) -> int:
    print("=== secure-bridge probe (bridge v4 / T8) ===")
    print(f"proxy URL : {args.proxy_url}")
    print(f"secure URL: {OPENCODE_SECURE_SERVE_URL}")
    print(f"workspace : {OPENCODE_BRIDGE_DIRECTORY}")
    print(f"agent     : {OPENCODE_AGENT}")
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

        if outcome.waiting_permission and (
            outcome.parked_and_timed_out or not outcome.completed_tools
        ):
            _print_manual_instruction(marker)
            if outcome.parked_and_timed_out:
                return EXIT_PERMISSION_PENDING

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
        _cleanup_probe_dir(probe_dir)


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
        help="seconds to wait for a manual permission resolution (default: %(default)s)",
    )
    parser.add_argument(
        "--read-timeout", type=float, default=90.0,
        help="per-chunk SSE idle timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--total-timeout", type=float, default=900.0,
        help="total request budget in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="bypass the registration-activation gate",
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
