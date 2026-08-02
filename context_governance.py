"""Frontend-agnostic context governance for tool-call-heavy conversations.

Mirrors the budget discipline nanobot-ai applies inside its own agent loop
(``agent/context_governance.py``) but lives in the proxy, so EVERY frontend —
nanobot, curl, web UIs, custom clients — gets the same protection regardless
of the client-side agent.

The transform is a pure function over the OUTBOUND model-copy:

  1. structural cleanup   — drop orphan tool results and malformed tool_calls
  2. tool-result budget   — truncate oversized results; offload very large
                            results to a proxy-owned directory and replace
                            them with a compact reference the model can
                            ``read_file`` if it needs more
  3. input-budget snip    — drop the oldest complete turns from the head when
                            the estimated prompt would overflow the model's
                            runtime context window

Constraints (deliberate):

- Only ``role: \"tool\"`` result messages are truncated or replaced. User and
  assistant text is NEVER altered.
- The client's stored conversation and the DB job audit copy are untouched —
  governance operates on a copy that is only ever sent to the model.
- The snip never splits a tool_call/result pair and never drops the most
  recent user turn.
- Truncation and offload append a marker so the model knows a result was
  partial and why (prevents silent re-call loops).
- Per-request opt-out: ``X-Proxy-Context-Governance: off``.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

from constants import (
    ESTIMATED_CHARS_PER_TOKEN,
    MAX_TOOL_RESULT_CHARS,
    OFFLOAD_MAX_FILES,
    READ_FILE_RESULT_CHARS,
    SNIP_SAFETY_BUFFER_TOKENS,
    TOOL_RESULT_PREVIEW_CHARS,
    TOOL_RESULTS_DIR_NAME,
)

logger = logging.getLogger("kinver.context_governance")


def default_tool_results_root() -> Path:
    """Proxy-owned offload directory (``~/.kinver-proxy/tool_results``)."""
    return Path.home() / ".kinver-proxy" / TOOL_RESULTS_DIR_NAME


def apply_context_governance(
    messages: list[dict[str, Any]],
    *,
    model_key: str,
    context_window: Optional[int],
    max_output_tokens: int = 4096,
    workspace: Optional[Path] = None,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """Return a governed COPY of ``messages`` for the model.

    ``messages`` itself is never mutated.  With ``enabled=False`` the input
    list is returned as-is.
    """
    if not enabled or not messages:
        return messages

    governed = _cleanup_structure([dict(m) for m in messages])
    session_key = _session_key(messages)
    governed = _apply_result_budget(
        governed,
        session_key=session_key,
        workspace=workspace or default_tool_results_root(),
    )

    if context_window and context_window > 0:
        budget = _input_budget(context_window, max_output_tokens)
        governed = _snip_to_budget(governed, budget)

    if governed is not messages:
        return governed
    return messages


# ---------------------------------------------------------------------------
# 1. Structural cleanup
# ---------------------------------------------------------------------------


def _cleanup_structure(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop orphan tool results and strip malformed tool_calls.

    A tool result whose ``tool_call_id`` has no matching assistant call in the
    conversation is dead weight (and a known confusion source for models).
    """
    call_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict) and tc.get("id"):
                call_ids.add(str(tc["id"]))

    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "tool":
            tool_call_id = msg.get("tool_call_id")
            if not tool_call_id or str(tool_call_id) not in call_ids:
                logger.debug("Dropping orphan tool result %s", tool_call_id)
                continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            valid = [
                tc
                for tc in msg["tool_calls"]
                if isinstance(tc, dict) and tc.get("id") and tc.get("function")
            ]
            if not valid:
                logger.debug("Stripping malformed tool_calls from assistant turn")
                continue
            if len(valid) != len(msg["tool_calls"]):
                msg = dict(msg)
                msg["tool_calls"] = valid
        cleaned.append(msg)
    return cleaned


# ---------------------------------------------------------------------------
# 2. Tool-result budget
# ---------------------------------------------------------------------------


def _apply_result_budget(
    messages: list[dict[str, Any]],
    *,
    session_key: str,
    workspace: Path,
) -> list[dict[str, Any]]:
    """Truncate/offload oversized tool results in place on the copy."""
    changed = False
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            continue
        name = str(msg.get("name") or "")
        cap = READ_FILE_RESULT_CHARS if name == "read_file" else MAX_TOOL_RESULT_CHARS
        if len(content) <= cap:
            continue
        tool_call_id = str(msg.get("tool_call_id") or "")
        reference = _offload_or_truncate(
            content,
            cap=cap,
            name=name,
            session_key=session_key,
            tool_call_id=tool_call_id,
            workspace=workspace,
        )
        msg["content"] = reference
        changed = True
    return messages if changed else messages


def _offload_or_truncate(
    content: str,
    *,
    cap: int,
    name: str,
    session_key: str,
    tool_call_id: str,
    workspace: Path,
) -> str:
    """Offload results far over cap; otherwise keep the head with a marker.

    The reference string carries the absolute path and a preview, so the model
    can ``read_file`` the full result if it genuinely needs more — without
    dragging every large result into every subsequent prompt.
    """
    if tool_call_id and len(content) > cap * 2:
        try:
            path = _write_offload(content, session_key, tool_call_id, workspace)
            preview = content[:TOOL_RESULT_PREVIEW_CHARS].replace("\n", " ")
            return (
                f"[tool result: {len(content)} chars, saved to {path}]\n"
                f"preview: {preview}\n"
            )
        except OSError as exc:  # pragma: no cover - disk failure fallback
            logger.warning("Tool-result offload failed for %s: %s", tool_call_id, exc)
    return f"{content[:cap]}\n\n…[tool result truncated: {len(content)} chars, showing first {cap}]"


def _write_offload(content: str, session_key: str, tool_call_id: str, workspace: Path) -> Path:
    bucket = workspace / session_key
    bucket.mkdir(parents=True, exist_ok=True)
    path = bucket / f"{tool_call_id}.txt"
    path.write_text(content, encoding="utf-8")
    _trim_offload_dir(workspace)
    return path


def _trim_offload_dir(workspace: Path) -> None:
    """Keep the offload directory bounded by total file count (oldest first)."""
    try:
        files = sorted(workspace.rglob("*.txt"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    while len(files) > OFFLOAD_MAX_FILES:
        try:
            files.pop(0).unlink()
        except OSError:
            break


# ---------------------------------------------------------------------------
# 3. Input-budget snip
# ---------------------------------------------------------------------------


def _input_budget(context_window: int, max_output_tokens: int) -> int:
    """Reserve output room (bounded, at least 4k) plus a safety buffer."""
    reserve = max(4096, min(max_output_tokens, context_window // 4))
    return max(0, context_window - reserve - SNIP_SAFETY_BUFFER_TOKENS)


def _estimate_tokens(message: dict[str, Any]) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return max(1, len(content) // ESTIMATED_CHARS_PER_TOKEN)
    if isinstance(content, list):
        total = sum(
            len(str(part.get("text", ""))) if isinstance(part, dict) else len(str(part))
            for part in content
        )
        return max(1, total // ESTIMATED_CHARS_PER_TOKEN)
    return 1


def _snip_to_budget(
    messages: list[dict[str, Any]],
    budget_tokens: int,
) -> list[dict[str, Any]]:
    """Drop the oldest whole turns from the head until the rest fits.

    Atomic units: a user message alone, an assistant message alone (no tool
    calls), or an assistant tool-call message TOGETHER with its consecutive
    tool results.  The most recent user turn is never dropped.
    """
    if budget_tokens <= 0:
        return messages
    total = sum(_estimate_tokens(m) for m in messages)
    if total <= budget_tokens:
        return messages

    remaining = list(messages)
    i = 0
    while i < len(remaining) and total > budget_tokens:
        msg = remaining[i]
        group = [msg]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            j = i + 1
            while j < len(remaining) and remaining[j].get("role") == "tool":
                group.append(remaining[j])
                j += 1
        group_tokens = sum(_estimate_tokens(m) for m in group)

        if msg.get("role") == "user" and not any(
            m.get("role") == "user" for m in remaining[i + 1:]
        ):
            break  # never drop the last user turn
        if not group:
            break
        del remaining[i : i + len(group)]
        total -= group_tokens
    return remaining


def _session_key(messages: list[dict[str, Any]]) -> str:
    """Stable bucket key derived from the first user message content."""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            if isinstance(content, list):
                text = "".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict)
                )
                if text.strip():
                    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return "default"
