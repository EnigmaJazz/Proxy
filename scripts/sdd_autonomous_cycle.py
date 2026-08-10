#!/usr/bin/env python3
"""Drive the AUTONOMOUS SDD cycle through the opencode-sdd bridge model.

One message carries the change AND the full preflight choices, so the
orchestrator runs proposal -> spec -> design -> tasks -> apply -> verify
-> archive in a single long-lived turn (timeout=3600s), never stopping
for per-phase questions.
"""
import argparse
import asyncio
import glob
import os
import subprocess
import sys
import time

from typing import Optional

import httpx

sys.path.insert(0, ".")

import opencode_bridge

from scripts.sdd_cycle_common import CycleStalled, guard_stall

OPENCODE_SERVE_URL = "http://127.0.0.1:18900"
SESSION_KEY_PREFIX = "sdd-autonomous-cycle"

#: After the bridge stream ends (the orchestrator keeps working in
#: sub-agent sessions, so the stream can end before the cycle does), wait
#: up to this long for the change's OpenSpec artifacts to appear.  A full
#: cycle takes ~30 min (proposal → archive), so the window must exceed
#: that; verified 2026-08-08: the change dir appears ~10 min in, the
#: archive report ~30 min in.
ARTIFACT_WAIT_S: float = 2400.0
ARTIFACT_POLL_S: float = 20.0

#: The go-proxy service whose request log is the "working" signal — the
#: opencode-go proxy (port 8788) serves every model call the opencode
#: serve makes.  Its journald request lines are the live activity signal
#: (session parts lag by design; see 2026-08-10).
_GO_PROXY_SERVICE = "opencode-go-proxy"

#: How far back the go-proxy journal window reaches per hold poll.
_GO_PROXY_ACTIVITY_WINDOW_S: float = 120.0

#: The opencode serve can die mid-cycle (1.18.15: silent death under
#: sustained multi-session traffic — 2026-08-08).  The bridge auto-
#: respawns it on the next call, and the pinned session survives; the
#: driver RESUMES the session instead of giving up.  Max stream attempts.
MAX_STREAM_ATTEMPTS: int = 12


def _artifacts_for(change: str) -> list[str]:
    """Sorted *.md artifacts for a change dir (or [])."""
    return sorted(glob.glob(f"openspec/changes/{change}/*.md"))


def _count_go_proxy_calls(journal_text: str) -> int:
    """Count model-call request lines in the go-proxy journal text.

    Pure parser so tests can inject a fake journal output; the live
    journal's request lines carry the stream/request markers.
    """
    return sum(
        1 for ln in journal_text.splitlines()
        if any(k in ln for k in ("stream", "request"))
    )


def _recent_go_proxy_calls(window_s: float = _GO_PROXY_ACTIVITY_WINDOW_S) -> int:
    """Model-call activity through the opencode-go proxy in the recent
    window — the authoritative "working" signal (session-part timestamps
    lag by design; the go-proxy's request log reflects live model
    activity).  Unavailable journald fails closed to 0.
    """
    try:
        out = subprocess.run(
            ["journalctl", "-u", _GO_PROXY_SERVICE, "--since",
             f"{int(window_s)} sec ago", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=15.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    return _count_go_proxy_calls(out)


def hold_decision(*, recent_calls: int, session_busy: bool,
                  replay_in_flight: bool, budget: int) -> tuple[str, int]:
    """One poll of the driver's hold state machine.

    Returns (action, new_budget) consumed directly by the wait loop:
      - ("hold", full_budget)  working activity: reset the budget.
      - ("hold", budget)       replay in flight: pause the drain.
      - ("drain", budget - 1)  no working activity: drain one poll.
      - ("resume", 0)          budget exhausted: resume now.
    """
    full_budget = int(ARTIFACT_WAIT_S / ARTIFACT_POLL_S)
    if replay_in_flight:
        # The replay window pauses the drain entirely; a phantom replay
        # window is bounded by its staleness horizon on the bridge side.
        return "hold", budget
    if recent_calls > 0 or session_busy:
        return "hold", full_budget
    if budget <= 1:
        return "resume", 0
    return "drain", budget - 1


async def _serve_sessions_active() -> bool:
    """True when ANY session on the serve has a running tool part (the
    session is busy generating/executing, not stalled).

    The session parts' timestamps LAG (the serve's storage writes parts
    late), so a quiet-looking phase is often still working — the model
    calls keep flowing through the go-proxy.  The driver must not resume
    against busy sessions; only a serve with NO working session is a
    genuine stall candidate (2026-08-09).
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{OPENCODE_SERVE_URL}/session/status", timeout=10.0,
            )
            if resp.status_code != 200:
                return False
            status_map = resp.json()
            if not isinstance(status_map, dict):
                return False
            return any(
                (s.get("type") == "busy" or bool(s.get("running")))
                for s in status_map.values()
                if isinstance(s, dict)
            )
    except (httpx.HTTPError, ValueError, OSError):
        return False


def _cycle_complete(change: str) -> bool:
    """True when the change dir carries the archive-report.md marker --
    the archive phase's report is the LAST artifact a full SDD cycle
    writes (proposal.md alone only proves the cycle started)."""
    return os.path.exists(
        f"openspec/changes/{change}/archive-report.md",
    )


def build_task(change: str, code_writer: str = "local") -> str:
    """Compose the SDD task prompt for a change name.

    ``code_writer`` is "local" (apply uses the local model — may contend
    with other local-model traffic) or "cloud" (apply uses the cloud
    model, keeping the local models free for communication).
    """
    writer_label = "Local model" if code_writer == "local" else "Cloud model"
    return f"""Use SDD to make this change:

CHANGE NAME: {change}

SDD SESSION PREFLIGHT (user-supplied, do NOT ask):
- Pace: Automatic
- Artifacts: Both (Engram + OpenSpec)
- PRs: Single PR
- Review budget: 400 lines
- Code writer: {writer_label}

Run the complete SDD cycle end-to-end now.

LOCAL-MODEL DELEGATION RULE (MANDATORY): when delegating code work to
the LOCAL model (apply's local writer), delegate ONE FILE at a time —
one task per file — for big tasks.  Never bundle multiple files into a
single local-model task: the local context window is limited, and a
per-file task keeps each delegation within budget.

TOOL RETRY RULE (MANDATORY): when a tool call fails with a transient
error (e.g. "Tool execution aborted", connection resets), retry the
tool ONCE immediately before giving up.  The serve's tool runner
intermittently aborts in-flight tool executions (writes included);
the retry normally succeeds.

{opencode_bridge._SDD_SUBAGENT_CONTRACT}
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Drive one autonomous SDD cycle through the opencode-sdd bridge model.",
    )
    parser.add_argument(
        "--change",
        required=True,
        help="Change name (openspec/changes/<name>) the cycle runs against.",
    )
    parser.add_argument(
        "--code-writer",
        choices=("local", "cloud"),
        default="local",
        help="Who writes the apply-phase code: 'local' (default; may contend "
             "with other local-model traffic) or 'cloud' (keeps the local "
             "models free for communication).",
    )
    return parser



def choose_code_writer(code_writer: Optional[str]) -> str:
    """Resolve the apply-phase writer: an explicit --code-writer wins; a
    non-interactive run (no stdin) defaults to 'local'; otherwise the user
    is asked BEFORE the cycle starts."""
    if code_writer:
        return code_writer
    try:
        answer = input(
            "Apply-phase code writer — 'local' (may contend with other "
            "local-model traffic) or 'cloud' (keeps local models free for "
            "communication)? [local/cloud] "
        ).strip().lower()
    except (EOFError, OSError):
        print("[no stdin — defaulting code writer to 'local']", flush=True)
        return "local"
    return "cloud" if answer == "cloud" else "local"


async def main(change: str, code_writer: str = "local") -> None:
    opencode_bridge.OPENCODE_SERVE_URL = OPENCODE_SERVE_URL
    session_map: dict[str, str] = {}
    # Unique per run: a fresh key avoids colliding with a concurrent cycle
    # on the same serve (each run pins its own session).
    session_key = f"{SESSION_KEY_PREFIX}-{change}-{int(time.time())}"
    print("== autonomous SDD cycle ==", flush=True)
    print(f"[code writer: {code_writer}]", flush=True)
    try:
        for attempt in range(MAX_STREAM_ATTEMPTS):
            # Resume the SAME pinned session across attempts: the serve
            # may die mid-cycle (silent death, opencode 1.18.15) and the
            # next bridge call respawns it — the pinned session resumes
            # where it stalled.  No-op after the cycle completes.
            try:
                async for kind, text in guard_stall(
                    opencode_bridge.opencode_chat_stream(
                        build_task(change, code_writer),
                        agent="gentle-orchestrator",
                        session_map=session_map,
                        session_key=session_key,
                        system_prompt=opencode_bridge._SDD_AUTONOMOUS_SYSTEM_PROMPT,
                        timeout=7200.0,  # apply phases run past 60 min; the 3600s cap aborted them
                        autonomous=True,  # PR-1: force-recycle + auto-allow
                    )
                ):
                    if kind == "question":
                        print("\n[QUESTION - should not happen in autonomous mode]\n"
                              + text[:400] + "\n[/QUESTION]\n", flush=True)
                    elif kind == "status":
                        print(f"  {text[:130]}", flush=True)
                    elif kind == "text":
                        print(f"  text: {text[:200]}", flush=True)
            except CycleStalled as exc:
                print(f"\n[STALL attempt {attempt + 1}] {exc}", flush=True)
                # Force-recycle + resume on the next attempt.
                await opencode_bridge._force_recycle_serve("SDD cycle stalled")
                continue
            if _cycle_complete(change):
                break
            print(f"[stream {attempt + 1} ended; cycle still working — waiting]",
                  flush=True)
            # Let the orchestrator/sub-agents work, then resume the stream.
            # Resume EARLY when the serve dies (its sessions stall): the
            # next attempt force-recycles + respawns + resumes the pin.
            dead_polls = 0
            wait_budget = int(ARTIFACT_WAIT_S / ARTIFACT_POLL_S)
            while wait_budget > 0:
                if _cycle_complete(change):
                    break
                # The go-proxy's model-call flow is the authoritative
                # "working" signal (part timestamps lag by design); the
                # serve's busy flag stays as the secondary signal.  A
                # fallback-model replay pauses the drain entirely.
                recent_calls = await asyncio.to_thread(
                    _recent_go_proxy_calls,
                )
                replay = await asyncio.to_thread(
                    opencode_bridge._replay_in_flight_for_any_session,
                )
                session_busy = await _serve_sessions_active()
                action, wait_budget = hold_decision(
                    recent_calls=recent_calls,
                    session_busy=session_busy,
                    replay_in_flight=replay,
                    budget=wait_budget,
                )
                print(
                    f"[HOLD] calls={recent_calls} busy={session_busy} "
                    f"replay={replay} action={action} budget={wait_budget}",
                    flush=True,
                )
                if action == "resume":
                    print("[HOLD] budget drained; resuming pinned session",
                          flush=True)
                    break
                serve_up = await opencode_bridge.is_opencode_serve_running()
                dead_polls = 0 if serve_up else dead_polls + 1
                if dead_polls >= 3:  # ~60s with a dead serve → resume now
                    print("[serve down; resuming pinned session]", flush=True)
                    break
                await asyncio.sleep(ARTIFACT_POLL_S)
            if _cycle_complete(change):
                break
    except SystemExit:
        raise
    except Exception as exc:  # never crash the driver on a transport error
        print(f"[ERROR] {exc}", flush=True)
        raise SystemExit(1) from None

    print("\n== openspec artifacts ==", flush=True)
    for p in _artifacts_for(change):
        print(" ", p, flush=True)
    if not _artifacts_for(change):
        print(
            f"[NO ARTIFACTS] change {change!r} produced no OpenSpec artifacts "
            "across all stream attempts — the cycle is still in flight or failed.",
            flush=True,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    args = build_parser().parse_args()
    writer = choose_code_writer(args.code_writer)
    asyncio.run(main(args.change, writer))
