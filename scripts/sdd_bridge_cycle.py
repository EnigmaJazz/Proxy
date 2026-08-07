#!/usr/bin/env python3
"""Drive a complete SDD cycle through the opencode bridge (nanobot-equivalent).

Improvements over the first driver:
- Keeps ONE session_map across all turns (pin continuity).
- Answers questions via prompt_async with messageID (the serve resolves the
  pending question tool call this way — posting a plain new user message
  makes the agent re-ask).
- Auto-answers the preflight and follow-up questions, then continues with
  "continue" until the stream ends with no pending question (cycle done).
"""
import asyncio
import json
import re
import sys
from typing import AsyncIterator, Optional

sys.path.insert(0, ".")

import httpx
import opencode_bridge

OPENCODE_SERVE_URL = "http://127.0.0.1:18900"
SESSION_KEY = "sdd-cycle-full"
CHANGE = "bridge-docs"
CHANGE_DESC = (
    "Add a small documentation file for the opencode bridge (proxy/opencode_bridge.py) "
    "to the docs/ directory describing what it does and how it works."
)

PREF_ANSWERS: list[tuple[str, str]] = [
    ("Pace", "Automatic"),
    ("Artifacts", "Both"),
    ("PRs", "Single"),
    ("Review", "400"),
    ("Code writer", "Local"),
]


def parse_groups(text: str) -> list[dict]:
    groups: list[dict] = []
    cur: Optional[dict] = None
    for raw in text.splitlines():
        ln = raw.strip()
        if ln.startswith("Options:"):
            if cur is not None:
                cur["opts"] = [o.strip() for o in ln.split("Options:", 1)[1].split("|") if o.strip()]
        elif ":" in ln and not ln.startswith("Options"):
            header, _, body = ln.partition(":")
            cur = {"header": header.strip(), "q": body.strip(), "opts": []}
            groups.append(cur)
    return groups


def answer_for(text: str) -> str:
    groups = parse_groups(text)
    if not groups:
        return "continue"
    answers: list[str] = []
    for g in groups:
        header, opts = g["header"], g["opts"]
        chosen = None
        for hdr_key, target in PREF_ANSWERS:
            if hdr_key.lower() in header.lower():
                for o in opts:
                    if target.lower() in o.lower():
                        chosen = o
                        break
                break
        if chosen is None and opts:
            chosen = opts[0]
        answers.append(f"{header}: {chosen or '(none)'}")
    return "; ".join(answers)


async def find_question_mid(sid: str) -> Optional[str]:
    """Find the messageID of the pending question tool part."""
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{OPENCODE_SERVE_URL}/session/{sid}/message")
        if r.status_code != 200:
            return None
        for m in r.json():
            for p in m.get("parts") or []:
                if p.get("type") == "tool" and p.get("tool") == "question":
                    return p.get("messageID")
    return None


async def main() -> None:
    opencode_bridge.OPENCODE_SERVE_URL = OPENCODE_SERVE_URL
    session_map: dict[str, str] = {}

    def stream(user_text: str) -> AsyncIterator[tuple[str, str]]:
        return opencode_bridge.opencode_chat_stream(
            user_text,
            agent="gentle-orchestrator",
            session_map=session_map,
            session_key=SESSION_KEY,
        )

    print(f"== full SDD cycle: {CHANGE} ==\n", flush=True)
    turns = 0
    user_text = f"Use SDD for a change. Change name: {CHANGE}. {CHANGE_DESC}"
    while turns < 40:
        turns += 1
        print(f"\n--- turn {turns} ---", flush=True)
        question: Optional[str] = None
        async for kind, text in stream(user_text):
            if kind == "question":
                question = text
                print("\n[QUESTION]\n" + text[:600] + "\n[/QUESTION]\n", flush=True)
            elif kind == "status":
                print(f"  status: {text[:110]}", flush=True)
            elif kind == "text":
                print(f"  text: {text[:220]}", flush=True)
        if question is None:
            print("\n== cycle finished (no question pending) ==", flush=True)
            break
        sid = session_map.get(SESSION_KEY)
        answer = answer_for(question)
        print(f"-> answer: {answer}", flush=True)
        # Deliver the answer resolving the pending question tool call.
        mid = await find_question_mid(sid) if sid else None
        if mid:
            async with httpx.AsyncClient(timeout=10.0) as c:
                await c.post(
                    f"{OPENCODE_SERVE_URL}/session/{sid}/prompt_async",
                    json={"agent": "gentle-orchestrator", "messageID": mid,
                          "parts": [{"type": "text", "text": answer}]},
                )
            # The stream continues after the answer; give the agent a beat.
            user_text = "continue"
        else:
            user_text = answer

    import glob
    print("\n== openspec artifacts ==", flush=True)
    for p in glob.glob(f"openspec/changes/{CHANGE}/*.md"):
        print(" ", p, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
