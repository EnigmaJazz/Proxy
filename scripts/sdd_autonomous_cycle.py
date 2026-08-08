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


def build_task(change: str) -> str:
    """Compose the SDD task prompt for a change name."""
    return f"""Use SDD to make this change:

CHANGE NAME: {change}

SDD SESSION PREFLIGHT (user-supplied, do NOT ask):
- Pace: Automatic
- Artifacts: Both (Engram + OpenSpec)
- PRs: Single PR
- Review budget: 400 lines
- Code writer: Local model

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
    return parser


async def main(change: str) -> None:
    opencode_bridge.OPENCODE_SERVE_URL = OPENCODE_SERVE_URL
    session_map: dict[str, str] = {}
    # Unique per run: a fresh key avoids colliding with a concurrent cycle
    # on the same serve (each run pins its own session).
    session_key = f"{SESSION_KEY_PREFIX}-{change}-{int(time.time())}"
    print("== autonomous SDD cycle ==", flush=True)
    try:
        async for kind, text in guard_stall(
            opencode_bridge.opencode_chat_stream(
                build_task(change),
                agent="gentle-orchestrator",
                session_map=session_map,
                session_key=session_key,
                system_prompt=opencode_bridge._SDD_AUTONOMOUS_SYSTEM_PROMPT,
                timeout=3600.0,  # matches OPENCODE_SDD_TIMEOUT
                autonomous=True,  # explicit flag (PR-1): force-recycle + auto-allow
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
        print(f"\n[STALL] {exc}", flush=True)
        print("[RECOVERY] force-recycling the serve, aborting the cycle with "
              "non-zero exit; report persistent stalls with the driver output "
              "attached.", flush=True)
        await opencode_bridge._force_recycle_serve("SDD cycle stalled")
        raise SystemExit(1) from None

    print("\n== openspec artifacts ==", flush=True)
    # The bridge stream can end while the orchestrator still works in
    # sub-agent sessions — poll for the artifacts instead of checking once.
    deadline = time.monotonic() + ARTIFACT_WAIT_S
    found: list[str] = []
    while time.monotonic() < deadline:
        found = sorted(glob.glob(f"openspec/changes/{change}/*.md"))
        if found:
            break
        await asyncio.sleep(ARTIFACT_POLL_S)
    for p in found:
        print(" ", p, flush=True)
    if not found:
        print(
            f"[NO ARTIFACTS] change {change!r} produced no OpenSpec artifacts "
            "within the wait window — the cycle is still in flight or failed.",
            flush=True,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    args = build_parser().parse_args()
    asyncio.run(main(args.change))
