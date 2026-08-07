#!/usr/bin/env python3
"""Drive the AUTONOMOUS SDD cycle through the opencode-sdd bridge model.

One message carries the change AND the full preflight choices, so the
orchestrator runs proposal -> spec -> design -> tasks -> apply -> verify
-> archive in a single long-lived turn (OPENCODE_SDD_TIMEOUT=3600s),
never stopping for per-phase questions.
"""
import asyncio
import sys

sys.path.insert(0, ".")

import opencode_bridge

OPENCODE_SERVE_URL = "http://127.0.0.1:18900"
SESSION_KEY = "sdd-autonomous-cycle"

TASK = """Use SDD to make this change:

CHANGE NAME: bridge-docs
DESCRIPTION: Add a small documentation file to docs/ describing what
proxy/opencode_bridge.py does and how it works (routes requests to a
headless opencode serve backend, relays permissions, streams progress).

SDD SESSION PREFLIGHT (user-supplied, do NOT ask):
- Pace: Automatic
- Artifacts: Both (Engram + OpenSpec)
- PRs: Single PR
- Review budget: 400 lines
- Code writer: Local model

Run the complete SDD cycle end-to-end now."""


async def main() -> None:
    opencode_bridge.OPENCODE_SERVE_URL = OPENCODE_SERVE_URL
    session_map: dict[str, str] = {}
    print("== autonomous SDD cycle ==", flush=True)
    async for kind, text in opencode_bridge.opencode_chat_stream(
        TASK,
        agent="gentle-orchestrator",
        session_map=session_map,
        session_key=SESSION_KEY,
        system_prompt=opencode_bridge._SDD_AUTONOMOUS_SYSTEM_PROMPT,
        timeout=3600.0,
    ):
        if kind == "question":
            print("\n[QUESTION - should not happen in autonomous mode]\n"
                  + text[:400] + "\n[/QUESTION]\n", flush=True)
        elif kind == "status":
            print(f"  {text[:130]}", flush=True)
        elif kind == "text":
            print(f"  text: {text[:200]}", flush=True)

    import glob
    print("\n== openspec artifacts ==", flush=True)
    for p in glob.glob("openspec/changes/bridge-docs/*.md"):
        print(" ", p, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
