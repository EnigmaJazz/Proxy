from __future__ import annotations
from pathlib import Path
import pytest
from tools.collect_tool_call_evidence import (
    ProbeBounds, build_probe_request, classify_receiver, collect_streaming_evidence,
    hash_file, safe_exec_identity, validate_probe_target,
)

class TestExecutableSafety:
    @pytest.mark.parametrize("exec_start,reason", [
        ("/bin/llama-server --port 8080; rm -rf /", "dangerous"),
        ("/bin/llama-server --port $(cat /etc/passwd)", "dangerous"),
    ])
    def test_rejects_dangerous_exec(self, exec_start: str, reason: str) -> None:
        path, msg = safe_exec_identity(exec_start)
        assert path is None and reason in msg.lower()
    def test_rejects_symlink_swap(self, tmp_path: Path) -> None:
        real, link = tmp_path / "llama-server", tmp_path / "link"
        real.write_text("real")
        link.symlink_to(real)
        link.unlink()
        link.symlink_to(tmp_path / "evil")
        path, msg = safe_exec_identity(f"{link} --port 8080")
        assert path is None and ("symlink" in msg.lower() or "bad" in msg.lower())
    def test_rejects_hash_drift(self, tmp_path: Path) -> None:
        exe = tmp_path / "llama-server"
        exe.write_text("v1")
        expected = hash_file(exe)
        exe.write_text("v2-evil")
        path, msg = safe_exec_identity(f"{exe} --port 8080", expected_hash=expected)
        assert path is None and "hash" in msg.lower()
    def test_accepts_valid_executable(self, tmp_path: Path) -> None:
        exe = tmp_path / "llama-server"
        exe.write_text("binary")
        path, msg = safe_exec_identity(f"{exe} --port 8080 --jinja")
        assert path == exe.resolve() and msg == ""

class TestReceiverClassification:
    @pytest.mark.parametrize("unit_text,expected,src", [
        ("ExecStart=/bin/llama-server --tool-call-parser llama", "CONFIRMED_RECEIVER", "tool"),
        ("ExecStart=/bin/llama-server --jinja", "CONFIRMED_NON_RECEIVER", "absent"),
        ("[Unit]\nDescription=x", "UNRESOLVED", "no execstart"),
    ])
    def test_classify(self, unit_text: str, expected: str, src: str) -> None:
        cls, source = classify_receiver(unit_text)
        assert cls == expected and src in source.lower()

class TestProbeTargetSafety:
    @pytest.mark.parametrize("host,active,bounds,reason", [
        ("192.168.1.1", True, ProbeBounds(), "loopback"),
        ("127.0.0.1", False, ProbeBounds(), "inactive"),
        ("127.0.0.1", True, None, "bound"),
    ])
    def test_rejects_unsafe_targets(self, host: str, active: bool, bounds: ProbeBounds | None, reason: str) -> None:
        safe, msg = validate_probe_target(host, 8080, active, bounds)
        assert safe is False and reason in msg.lower()
    def test_accepts_loopback_active_with_bounds(self) -> None:
        assert validate_probe_target("127.0.0.1", 8080, True, ProbeBounds()) == (True, "")

class TestProbeRequest:
    def test_inert_streaming_request_and_bounds(self) -> None:
        req, b = build_probe_request(), ProbeBounds()
        assert req["stream"] and req["max_tokens"] <= 64 and req["tools"][0]["function"]["name"].startswith("inert_probe_")
        assert b.timeout_seconds == 20.0 and b.max_events == 256 and b.max_bytes == 64 * 1024

class TestStreamingCollection:
    @pytest.mark.asyncio
    async def test_timeout_records_failure(self) -> None:
        async def fail(_): raise TimeoutError("deadline")
        r = await collect_streaming_evidence("http://127.0.0.1:8080/v1/chat/completions", ProbeBounds(), fail)
        assert r["error"] == "timeout" and r["events"] == [] and r["executed_calls"] is False
    @pytest.mark.asyncio
    async def test_malformed_stream_records_failure(self) -> None:
        async def bad(_):
            yield b"not sse\n"
            raise ValueError("broke")
        r = await collect_streaming_evidence("http://127.0.0.1:8080/v1/chat/completions", ProbeBounds(), bad)
        assert r["error"] == "malformed_stream" and r["executed_calls"] is False
    @pytest.mark.asyncio
    async def test_event_bound_stops(self) -> None:
        async def huge(_):
            for i in range(300): yield f"data: {{\"c\":{i}}}\n\n".encode()
        r = await collect_streaming_evidence("http://127.0.0.1:8080/v1/chat/completions", ProbeBounds(max_events=5), huge)
        assert len(r["events"]) == 5 and r["truncated"] and not r["executed_calls"]
    @pytest.mark.asyncio
    async def test_byte_bound_stops(self) -> None:
        async def fat(_):
            for _ in range(10): yield b"data: " + b"x" * 8192 + b"\n\n"
        r = await collect_streaming_evidence("http://127.0.0.1:8080/v1/chat/completions", ProbeBounds(max_bytes=1024), fat)
        assert r["total_bytes"] <= 1024 and r["truncated"] and not r["executed_calls"]
