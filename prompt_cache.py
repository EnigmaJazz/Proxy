"""Prompt priming — keep the professional model's KV-cache warm.

Frontends (nanobot, OpenWebUI) send a large fixed system prompt with
every request; the model re-processes it each time, which dominates the
time-to-first-token.  Priming runs the known system prompts through the
model once (a trivial completion) so the llama.cpp KV-cache holds the
processed prompt; subsequent real requests with the same system message
reuse the cached prefix and start streaming almost immediately.

The proxy ALSO registers system messages it observes on the wire
(first-seen per caller) so an unregistered frontend prompt gets primed
automatically on the next idle window.

Design notes
------------
- Priming is best-effort and never raises: a failure just means the
  next request pays the normal prefill cost.
- Priming runs on the professional's slot when it is idle (the
  residency monitor owns the schedule); it never preempts an active
  generation.
- The disk-cache slot-save/restore (llama-server ``--slot-save-path``)
  is a follow-up: primed slots can be persisted and restored across
  model restarts.  ``prime()`` here warms the in-memory KV-cache.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from llm import call_model

#: Registered system prompts, by name (the value is the system message
#: text).  The registry is process-local; observed prompts are added at
#: runtime and the nanobot/OpenWebUI defaults can be seeded by callers.
_PROMPTS: dict[str, str] = {}

#: Last time each prompt was primed (epoch seconds), to avoid re-priming
#: the same prompt on every idle tick.
_LAST_PRIMED: dict[str, float] = {}

#: Minimum seconds between re-primes of the same prompt.
_PRIME_INTERVAL_S: float = 600.0


def register_prompt(name: str, system_prompt: str) -> None:
    """Register (or refresh) a system prompt to be primed."""
    if system_prompt and system_prompt.strip():
        _PROMPTS[name] = system_prompt


def register_observed_system_prompt(
    caller: str, system_prompt: Any,
) -> None:
    """Register the system message observed on a request (first-seen per
    caller).  ``caller`` is e.g. ``nanobot`` or ``openwebui``; the same
    caller's system message is refreshed on change so priming always uses
    the CURRENT prompt."""
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        return
    key = f"observed:{caller}"
    if _PROMPTS.get(key) != system_prompt:
        _PROMPTS[key] = system_prompt
        _LAST_PRIMED.pop(key, None)


def registered_prompts() -> dict[str, str]:
    """Snapshot of the registry (for tests / diagnostics)."""
    return dict(_PROMPTS)


async def prime(port: int = 0) -> dict[str, bool]:
    """Prime every registered system prompt on the professional model.

    Runs each prompt once with a trivial completion so the KV-cache holds
    the processed prefix.  Returns {name: primed_ok}.  Best-effort:
    failures are recorded as False and never raise.
    """
    results: dict[str, bool] = {}
    now = time.monotonic()
    for name, prompt in list(_PROMPTS.items()):
        last = _LAST_PRIMED.get(name, 0.0)
        if now - last < _PRIME_INTERVAL_S:
            results[name] = True  # already primed recently — skip
            continue
        try:
            await asyncio.to_thread(
                call_model,
                port or 13109,
                f"{prompt}\n\nSay OK.",
                max_tokens=4,
            )
            _LAST_PRIMED[name] = now
            results[name] = True
        except (OSError, ValueError):
            results[name] = False
    return results
