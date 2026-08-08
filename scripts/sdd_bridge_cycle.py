#!/usr/bin/env python3
"""Drive a complete SDD cycle through the opencode bridge (nanobot-equivalent).

Improvements over the first driver:
- Keeps ONE session_map across all turns (pin continuity).
- Answers questions via prompt_async with messageID (the serve resolves the
  pending question tool call this way — posting a plain new user message
  makes the agent re-ask).
- Auto-answers the preflight and follow-up questions, then continues with
  "continue" until the stream ends with no pending question (cycle done).
- Runs in autonomous mode (permission events auto-allowed); preflight
  questions still surface and are answered here.
- Any turn whose stream yields no delta for STALL_S is a stall: the serve
  is recycled and the cycle aborts with a non-zero exit.
"""
import argparse
import asyncio
import glob
import sys
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

sys.path.insert(0, ".")

import httpx
import opencode_bridge

from scripts.sdd_cycle_common import STALL_S, CycleStalled, guard_stall

OPENCODE_SERVE_URL = "http://127.0.0.1:18900"
SESSION_KEY = "sdd-cycle-full"

PREF_ANSWERS: list[tuple[str, str]] = [
    ("Pace", "Automatic"),
    ("Artifacts", "Both"),
    ("PRs", "Single"),
    ("Review", "400"),
    ("Code writer", "Local"),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Drive a complete SDD cycle through the opencode bridge.",
    )
    parser.add_argument(
        "--change",
        required=True,
        help="Change name (openspec/changes/<name>) the cycle runs against.",
    )
    parser.add_argument(
        "--desc",
        default="",
        help="Optional change description appended to the SDD prompt.",
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


@dataclass
class QuestionGroup:
    """One parsed preflight question: header label, question text, options."""

    header: str
    q: str
    opts: list[str] = field(default_factory=list)


def parse_groups(text: str) -> list[QuestionGroup]:
    groups: list[QuestionGroup] = []
    cur: Optional[QuestionGroup] = None
    for raw in text.splitlines():
        ln = raw.strip()
        if ln.startswith("Options:"):
            if cur is not None:
                cur.opts = [o.strip() for o in ln.split("Options:", 1)[1].split("|") if o.strip()]
        elif ":" in ln and not ln.startswith("Options"):
            header, _, body = ln.partition(":")
            cur = QuestionGroup(header=header.strip(), q=body.strip())
            groups.append(cur)
    return groups


def answer_for(text: str) -> str:
    groups = parse_groups(text)
    if not groups:
        return "continue"
    answers: list[str] = []
    for g in groups:
        header, opts = g.header, g.opts
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


async def main(change: str, change_desc: str, code_writer: str = "local") -> None:
    opencode_bridge.OPENCODE_SERVE_URL = OPENCODE_SERVE_URL
    session_map: dict[str, str] = {}

    def stream(user_text: str) -> AsyncIterator[tuple[str, str]]:
        return opencode_bridge.opencode_chat_stream(
            user_text,
            agent="gentle-orchestrator",
            session_map=session_map,
            session_key=SESSION_KEY,
            autonomous=True,  # explicit flag (PR-1): permission events auto-allowed
        )

    print(f"== full SDD cycle: {change} ==\n", flush=True)
    turns = 0
    writer_label = "Local model" if code_writer == "local" else "Cloud model"
    user_text = (
        f"Use SDD for a change. Change name: {change}. "
        f"Code writer: {writer_label} (user-supplied preflight, do NOT ask)."
    )
    if change_desc:
        user_text += f" {change_desc}"
    while turns < 40:
        turns += 1
        print(f"\n--- turn {turns} ---", flush=True)
        question: Optional[str] = None
        try:
            async for kind, text in guard_stall(stream(user_text)):
                if kind == "question":
                    question = text
                    print("\n[QUESTION]\n" + text[:600] + "\n[/QUESTION]\n", flush=True)
                elif kind == "status":
                    print(f"  status: {text[:110]}", flush=True)
                elif kind == "text":
                    print(f"  text: {text[:220]}", flush=True)
        except CycleStalled as exc:
            print(f"\n[STALL] {exc}", flush=True)
            print("[RECOVERY] force-recycling the serve, aborting the cycle with "
                  "non-zero exit; report persistent stalls with the driver output "
                  "attached.", flush=True)
            await opencode_bridge._force_recycle_serve("SDD cycle stalled")
            raise SystemExit(1) from None
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

    print("\n== openspec artifacts ==", flush=True)
    for p in glob.glob(f"openspec/changes/{change}/*.md"):
        print(" ", p, flush=True)


if __name__ == "__main__":
    from scripts.sdd_autonomous_cycle import choose_code_writer
    args = build_parser().parse_args()
    writer = choose_code_writer(args.code_writer)
    asyncio.run(main(args.change, args.desc, writer))
