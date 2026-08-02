from __future__ import annotations

import hashlib, inspect, re, secrets, uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProbeBounds:
    max_tokens: int = 64
    timeout_seconds: float = 20.0
    max_events: int = 256
    max_bytes: int = 64 * 1024


META = re.compile("[;|&`\n\r<>]")
SUBST = re.compile(r"\$\(|`[^`]*`|\$\{[^}]*\}|\$[A-Za-z_]")


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_exec_identity(exec_start: str, expected_hash: str | None = None) -> tuple[Path | None, str]:
    raw = exec_start.strip()
    if not raw:
        return None, "empty ExecStart"
    if META.search(raw) or SUBST.search(raw):
        return None, "rejected: dangerous shell syntax"
    token = raw.split(None, 1)[0].strip("'\"")
    exe = Path(token)
    if not exe.is_absolute():
        return None, "rejected: relative path"
    try:
        canonical = exe.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "rejected: bad symlink"
    if not canonical.is_file():
        return None, "rejected: not a file"
    if expected_hash and not secrets.compare_digest(hash_file(canonical), expected_hash):
        return None, "rejected: hash drift"
    return canonical, ""


def classify_receiver(unit_text: str) -> tuple[str, str]:
    m = re.search(r"^ExecStart=(.+)$", unit_text, re.MULTILINE)
    if not m:
        return "UNRESOLVED", "no ExecStart"
    line = m.group(1)
    if "--tool-call-parser" in line or "--tools" in line:
        return "CONFIRMED_RECEIVER", "tool keyword present"
    return "CONFIRMED_NON_RECEIVER", "tool keyword absent"


def validate_probe_target(host: str, port: int, active: bool, bounds: ProbeBounds | None) -> tuple[bool, str]:
    if bounds is None:
        return False, "missing bounds"
    if not active:
        return False, "inactive service"
    if host not in ("127.0.0.1", "::1", "localhost"):
        return False, "non-loopback target"
    if not 1 <= port <= 65535:
        return False, "invalid port"
    return True, ""


def build_probe_request() -> dict:
    name = f"inert_probe_{uuid.uuid4().hex[:8]}"
    tool = {"type": "function", "function": {"name": name, "description": "Inert probe.", "parameters": {"type": "object", "properties": {}}}}
    return {"model": "inspected-receiver", "messages": [{"role": "user", "content": "Probe tool-call template capability."}], "tools": [tool], "stream": True, "max_tokens": 64}


async def collect_streaming_evidence(url: str, bounds: ProbeBounds, stream_fn: Callable[[str], AsyncIterator[bytes]]) -> dict:
    events: list[str] = []
    total = 0
    truncated = False
    error = ""
    try:
        stream = stream_fn(url)
        if inspect.isawaitable(stream):
            stream = await stream
        async for chunk in stream:
            if total + len(chunk) > bounds.max_bytes:
                truncated = True
                break
            total += len(chunk)
            for line in chunk.decode("utf-8", errors="replace").splitlines():
                if line.startswith("data:"):
                    events.append(line)
                    if len(events) >= bounds.max_events:
                        truncated = True
                        break
            if truncated:
                break
    except TimeoutError:
        error = "timeout"
    except Exception:
        error = "malformed_stream"
    return {"url": url, "events": events, "total_bytes": total, "truncated": truncated, "error": error, "executed_calls": False}
