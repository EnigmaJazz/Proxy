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
import sys
import time

sys.path.insert(0, ".")

import opencode_bridge

from scripts.sdd_cycle_common import STALL_S, CycleStalled, guard_stall

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

#: The opencode serve can die mid-cycle (1.18.15: silent death under
#: sustained multi-session traffic — 2026-08-08).  The bridge auto-
#: respawns it on the next call, and the pinned session survives; the
#: driver RESUMES the session instead of giving up.  Max stream attempts.
MAX_STREAM_ATTEMPTS: int = 12


def _artifacts_for(change: str) -> list[str]:
    """Sorted *.md artifacts for a change dir (or [])."""
    return sorted(glob.glob(f"openspec/changes/{change}/*.md"))


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

Run the complete SDD cycle end-to-end now."""


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
                        timeout=3600.0,  # matches OPENCODE_SDD_TIMEOUT
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
            if _artifacts_for(change):
                break
            print(f"[stream {attempt + 1} ended; cycle still working — waiting]",
                  flush=True)
            # Let the orchestrator/sub-agents work, then resume the stream.
            # Resume EARLY when the serve dies (its sessions stall): the
            # next attempt force-recycles + respawns + resumes the pin.
            dead_polls = 0
            for _ in range(int(ARTIFACT_WAIT_S / ARTIFACT_POLL_S)):
                if _artifacts_for(change):
                    break
                serve_up = await opencode_bridge.is_opencode_serve_running()
                dead_polls = 0 if serve_up else dead_polls + 1
                if dead_polls >= 3:  # ~60s with a dead serve → resume now
                    print("[serve down; resuming pinned session]", flush=True)
                    break
                await asyncio.sleep(ARTIFACT_POLL_S)
            if _artifacts_for(change):
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
    asyncio.run(main(args.change, args.code_writer))
