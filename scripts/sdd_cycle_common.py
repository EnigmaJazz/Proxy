"""Shared helpers for the SDD cycle driver scripts.

Both ``sdd_autonomous_cycle.py`` and ``sdd_bridge_cycle.py`` need the same
stall semantics (REQ-6): a cycle whose backend yields no stream deltas past
the threshold is stuck and must be aborted loudly instead of hanging forever.
"""
import asyncio
from typing import AsyncIterator, TypeVar

T = TypeVar("T")

#: A driver whose bridge stream yields no delta for this long is STALLED.
STALL_S: float = 180.0


class CycleStalled(Exception):
    """Raised when a bridge stream yields no deltas for STALL_S seconds."""


async def guard_stall(
    stream: AsyncIterator[T],
    stall_s: float = STALL_S,
) -> AsyncIterator[T]:
    """Re-emit every delta from ``stream``; raise ``CycleStalled`` on silence.

    Detection is a monotonic timer over the delta stream: ``asyncio.wait_for``
    races ``__anext__`` against the threshold, so any backend that parks
    without yielding (wedged tool runner, dead event bus) surfaces as a
    stall instead of a hang.
    """
    it = stream.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(it.__anext__(), timeout=stall_s)
        except asyncio.TimeoutError:
            raise CycleStalled(
                f"no new stream deltas for {stall_s:.0f}s — backend parked "
                "past the stall threshold"
            ) from None
        except StopAsyncIteration:
            return
        yield item
