#!/usr/bin/env python3
"""Frontdesk classification accuracy probe.

Runs a golden set of requests through the REAL frontdesk model and reports
what it classified, versus the expected intent.  Also applies the routing
heuristics (tool/code keyword + factual) to show what the effective routed
intent would be after the safety nets.

Usage: .venv/bin/python scripts/probe_frontdesk.py
"""
import asyncio
import json
import sys
from typing import Any, Optional

sys.path.insert(0, ".")

import systemd
import routing

# Golden set: (label, request).  Labels are what a careful human router
# would pick given the frontdesk prompt's intent definitions.
GOLDEN: list[tuple[str, str]] = [
    # --- CODE ---
    ("CODE", "write a python script that downloads a file"),
    ("CODE", "add a function to utils.py that parses JSON"),
    ("CODE", "fix the bug in src/auth.py line 42"),
    ("CODE", "refactor the auth module across 12 files to async"),
    ("CODE", "write a bash script that backs up the database"),
    ("CODE", "create a unit test for the parser"),
    ("CODE", "write a script to check for available updates and notify on boot"),
    ("CODE", "why does my python function return None?"),
    ("CODE", "search the web for python tutorials then write a script"),
    # --- CHAT ---
    ("CHAT", "tell me a joke"),
    ("CHAT", "what is the capital of france"),
    ("CHAT", "how do i make coffee"),
    ("CHAT", "explain quantum computing to me"),
    # --- TOOL ---
    ("TOOL", "what's the weather forecast tomorrow"),
    ("TOOL", "search for the latest AMD GPU drivers"),
    ("TOOL", "read the file /etc/nginx/nginx.conf"),
    ("TOOL", "look up the current stock price of nvidia"),
    ("TOOL", "check the news for today"),
    # --- SCHOLAR ---
    ("SCHOLAR", "compare transformer architectures BERT vs GPT vs T5"),
    ("SCHOLAR", "analyze the impact of AVX-512 on LLM inference throughput"),
    # --- PROFESSIONAL ---
    ("PROFESSIONAL", "write a project proposal for migrating our database to postgres"),
    ("PROFESSIONAL", "draft a technical specification for a REST API"),
    # --- CREATIVE ---
    ("CREATIVE", "write a short story about a robot discovering emotions"),
    # --- ARCHITECT ---
    ("ARCHITECT", "design a microservice architecture for a payments system"),
    # --- noise / invalid ---
    ("INVALID", "asdfghjkl qwerty zxcvbn"),
]

# Deterministic keyword heuristics — read from the REAL constants so the
# probe always reflects what the proxy actually routes.
from constants import TOOL_KEYWORDS, CODE_KEYWORDS, SCHOLAR_KEYWORDS


def heuristic_intent(text: str) -> str:
    """Effective intent after the code-first/tool-second/scholar keyword
    layer (mirrors the routes.py ordering: CODE, then TOOL, then SCHOLAR)."""
    lowered = text.lower()
    if any(kw in lowered for kw in CODE_KEYWORDS):
        return "CODE"
    if any(kw in lowered for kw in TOOL_KEYWORDS):
        return "TOOL"
    if any(kw in lowered for kw in SCHOLAR_KEYWORDS):
        return "SCHOLAR"
    return "CHAT"


def frontdesk_intent(classif: dict[str, Any]) -> str:
    return str(classif.get("intent") or "?").upper()


def pass_or_fail(actual: str, expected: str) -> tuple[bool, str]:
    if actual == expected:
        return True, ""
    # ACCEPTABLE near-misses: a specific label falling back to CHAT on a
    # genuinely ambiguous/shared phrasing, or CHAT for a request the prompt
    # defines as borderline.  CODE ↔ TOOL confusion is a real miss.
    near_miss = (
        (actual == "CHAT" and expected in ("PROFESSIONAL", "CREATIVE"))
        or (actual == "TOOL" and expected == "SCHOLAR")
    )
    if near_miss:
        return True, "(near-miss)"
    return False, "(MISS)"


async def main() -> None:
    ctl = systemd.SystemdController()
    port = await ctl.get_port("frontdesk")
    print(f"frontdesk port: {port}\n")

    rows: list[tuple[str, str, str, str, str]] = []
    n_pass = 0
    for expected, text in GOLDEN:
        classif = await routing.classify_with_frontdesk(text, frontdesk_port=port)
        raw = frontdesk_intent(classif)
        eff = heuristic_intent(text)
        ok, note = pass_or_fail(raw, expected)
        if ok:
            n_pass += 1
        rows.append((text[:58], expected, raw, eff, note))

    width = max(len(r[0]) for r in rows)
    print(f"{'request':<{width}}  exp     frontd  heur   verdict")
    print("-" * (width + 45))
    for text, exp, raw, eff, note in rows:
        mark = "  " if note else ""
        print(f"{text:<{width}}  {exp:<6}  {raw:<6}  {eff:<5}  {note or 'ok'}{mark}")

    print(f"\nRaw frontdesk: {n_pass}/{len(GOLDEN)} exact + near-miss matches")
    # Report effective after heuristics: what the proxy would actually route
    eff_pass = sum(1 for _, exp, raw, eff, note in rows
                   if eff == exp or (eff == "CODE" and exp in ("CODE",)))
    print(f"Heuristic-corrected: {eff_pass}/{len(GOLDEN)}")


if __name__ == "__main__":
    asyncio.run(main())
