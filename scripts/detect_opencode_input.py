#!/usr/bin/env python3
"""Detect when an opencode agent (opencode serve) is waiting for human input.

An opencode agent running headless under ``opencode serve`` can block in
exactly two ways, both surfaced by the serve's HTTP API:

- a pending PERMISSION (``GET /permission``): a tool (bash / write / edit /
  external_directory) hit the permission gate and the agent cannot continue
  until a human replies allow/always/reject;
- a pending QUESTION (``GET /question``): the agent's question tool parked
  the current step until a human answers.

This script polls those endpoints and reports whether any session (or a
specific ``--session``) needs input.  Useful as a watchdog for unattended
bridge sessions (the proxy's opencode_bridge.py) and for CI gates that
must not block on a headless agent:

    # one-shot check (the exit code IS the result)
    python scripts/detect_opencode_input.py --once && echo clean

    # watcher that only prints when an agent is asking for input
    python scripts/detect_opencode_input.py --alert-only

    # watcher with full status, machine-readable JSON lines
    python scripts/detect_opencode_input.py --json

Exit codes (``--once`` mode):
    0  no opencode agent needs input
    1  at least one session is waiting for human input
    2  opencode serve unreachable or the API returned an error
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

DEFAULT_BASE_URL = "http://127.0.0.1:18900"
DEFAULT_INTERVAL = 2.0
DEFAULT_TIMEOUT = 5.0

# NOTE (deliberate): the endpoint default is re-declared here instead of
# importing constants.py, and the target extraction mirrors
# opencode_bridge._permission_target instead of importing the bridge.
# This watchdog must stay stdlib-only and import-free of the proxy: it is
# meant to run when the proxy/bridge are WEDGED or down (its whole point
# is detecting that state), and importing the proxy's own modules could
# import the very app that is stuck.  Keep DEFAULT_BASE_URL in sync with
# constants.OPENCODE_SERVE_URL and the preference chain with the bridge.

log = logging.getLogger("detect_opencode_input")


@dataclass
class InputSignal:
    """One concrete reason an opencode agent is waiting for human input."""

    kind: str                        # "permission" | "question"
    session_id: str
    request_id: str                  # serve-side id used to reply/approve
    target: str                      # what the agent wants (path / cmd / text)
    tool: str = ""                   # tool name (permission records only)
    detail: str = ""                 # permission type / question text


@dataclass
class ServeSnapshot:
    """Full detection result for one poll."""

    signals: list[InputSignal] = field(default_factory=list)
    busy_sessions: list[str] = field(default_factory=list)
    serve_alive: bool = True
    error: str = ""

    @property
    def needs_input(self) -> bool:
        return bool(self.signals)


def fetch_json(url: str, timeout: float) -> Any:
    """GET ``url`` and parse the JSON body.  Raises on HTTP/network errors."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _permission_target(perm: dict[str, Any]) -> str:
    """Best human-readable description of what a permissioned access targets.

    Mirrors opencode_bridge._permission_target: exact file path from
    metadata, else parent directory, else the first matched pattern.
    """
    try:
        meta = perm.get("metadata") or {}
        if isinstance(meta, dict):
            fp = str(meta.get("filepath") or "").strip()
            if fp:
                return fp
            parent = str(meta.get("parentDir") or "").strip()
            if parent:
                return parent + "/"
        patterns = perm.get("patterns") or []
        if patterns:
            return str(patterns[0])
    except (TypeError, ValueError):
        pass
    return "(unknown path)"


def _question_text(entry: dict[str, Any]) -> str:
    """Extract a readable question string from a /question entry.

    Entries carry a ``questions`` list (the bridge posts one answer per
    question); items may be dicts with a ``question``/``text`` field or
    plain strings.  Falls back to the first non-empty string found.
    """
    qs = entry.get("questions") or []
    for q in qs:
        if isinstance(q, dict):
            for key in ("question", "text", "prompt"):
                val = q.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        elif isinstance(q, str) and q.strip():
            return q.strip()
    return "(question tool — text unavailable)"


def poll_serve(
    base_url: str,
    timeout: float,
    session_filter: Optional[str] = None,
) -> ServeSnapshot:
    """Query the serve once and classify every input-waiting session.

    Signals come from the serve's two authoritative endpoints:
    ``GET /permission`` (a tool parked on the permission gate) and
    ``GET /question`` (the agent asked the user a question).  Sessions in
    the ``GET /session/status`` map are reported as busy context, never as
    an input signal by themselves.
    """
    snap = ServeSnapshot()
    try:
        permissions = fetch_json(f"{base_url}/permission", timeout)
        questions = fetch_json(f"{base_url}/question", timeout)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        snap.serve_alive = False
        snap.error = str(exc)
        return snap

    if not isinstance(permissions, list) or not isinstance(questions, list):
        snap.serve_alive = False
        snap.error = f"unexpected API shape: permission={type(permissions).__name__} question={type(questions).__name__}"
        return snap

    for perm in permissions:
        if not isinstance(perm, dict):
            continue
        sid = str(perm.get("sessionID") or "")
        if session_filter and sid != session_filter:
            continue
        if not sid:
            continue
        tool = str(perm.get("tool") or "")
        perm_type = str(perm.get("permission") or "")
        cmd = str(((perm.get("metadata") or {}).get("command") if isinstance(perm.get("metadata"), dict) else None) or "")
        target = _permission_target(perm)
        detail = perm_type or tool
        if tool and tool != perm_type:
            detail = f"{detail} ({tool})"
        if cmd:
            target = f"{cmd[:120]}"
        snap.signals.append(InputSignal(
            kind="permission",
            session_id=sid,
            request_id=str(perm.get("id") or ""),
            target=target,
            tool=tool,
            detail=detail,
        ))

    for entry in questions:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("sessionID") or "")
        if session_filter and sid != session_filter:
            continue
        if not sid:
            continue
        snap.signals.append(InputSignal(
            kind="question",
            session_id=sid,
            request_id=str(entry.get("id") or ""),
            target=_question_text(entry),
            detail="question",
        ))

    try:
        st = fetch_json(f"{base_url}/session/status", timeout)
        if isinstance(st, dict):
            snap.busy_sessions = [str(s) for s in st]
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        pass  # status map is context only — never fail the poll over it

    # Deduplicate: one signal per (session, kind) keeps the output stable
    # when the serve lists the same pending item under repeated poll IDs.
    seen: set[tuple[str, str]] = set()
    deduped: list[InputSignal] = []
    for sig in snap.signals:
        key = (sig.session_id, sig.kind)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(sig)
    snap.signals = deduped
    return snap


def render_human(snap: ServeSnapshot) -> str:
    """Human-readable single-poll report (one block, no ANSI)."""
    lines: list[str] = []
    if not snap.serve_alive:
        lines.append(f"opencode serve unreachable: {snap.error or 'connection failed'}")
        return "\n".join(lines)
    lines.append(
        f"opencode serve: {len(snap.busy_sessions)} session(s) busy, "
        f"{len(snap.signals)} needing input"
    )
    for sig in snap.signals:
        sid = sig.session_id[:8] + "…" if len(sig.session_id) > 8 else sig.session_id
        if sig.kind == "permission":
            lines.append(f"  ⚠ session {sid} PERMISSION ({sig.detail}): {sig.target}")
        else:
            lines.append(f"  ⚠ session {sid} QUESTION: {sig.target}")
    return "\n".join(lines)


def render_json(snap: ServeSnapshot) -> str:
    """Machine-readable single-poll report (one JSON line)."""
    return json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "serve_alive": snap.serve_alive,
        "error": snap.error or None,
        "needs_input": snap.needs_input,
        "busy_sessions": snap.busy_sessions,
        "signals": [
            {
                "kind": s.kind,
                "session_id": s.session_id,
                "request_id": s.request_id,
                "target": s.target,
                "tool": s.tool,
                "detail": s.detail,
            }
            for s in snap.signals
        ],
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Detect when an opencode agent (opencode serve) is waiting for "
            "human input (pending permission or question)."
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"opencode serve base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--session", dest="session_id", default=None,
                        help="only report sessions with this id")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"per-request HTTP timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--once", action="store_true",
                        help="poll once, print the report, exit 0/1/2")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help=f"watch-mode poll interval in seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON lines")
    parser.add_argument("--alert-only", action="store_true",
                        help="watch mode: print only when a session needs input")
    parser.add_argument("--verbose", action="store_true",
                        help="diagnostics on stderr")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )

    def render(snap: ServeSnapshot) -> str:
        return render_json(snap) if args.json else render_human(snap)

    if args.once:
        snap = poll_serve(args.base_url, args.timeout, args.session_id)
        print(render(snap))
        if not snap.serve_alive:
            return 2
        return 1 if snap.needs_input else 0

    # Watch mode: keep polling until interrupted.  Exit 0 on clean Ctrl+C.
    log.info("watch mode on %s every %.1fs", args.base_url, args.interval)
    try:
        while True:
            snap = poll_serve(args.base_url, args.timeout, args.session_id)
            if args.alert_only:
                if snap.serve_alive and snap.needs_input:
                    print(render(snap), flush=True)
            elif not args.json:
                sys.stdout.write("\033[2K\r" + render(snap))
                sys.stdout.flush()
            else:
                print(render(snap), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("stopped")
        print()  # leave the watch line when a \r-report is on screen
        return 0


if __name__ == "__main__":
    sys.exit(main())
