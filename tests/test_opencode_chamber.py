"""Tests for opencode_chamber.py (bridge-owned openchamber daemon lifecycle).

The lifecycle is exercised against a FAKE `openchamber` CLI on PATH: the
stub records argv (env construction, port/password flags), answers the
health endpoint (spawned as a tiny HTTP server), and fakes the session
create/send/messages responses so the spawn-time smoke probe can be
driven deterministically.  No real daemon is ever started.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

import opencode_chamber as oc


# -- Fake openchamber CLI -------------------------------------------------

FAKE_SESSION_ID = "ses_fake00000000000000000001"


FAKE_CLI = r"""#!/usr/bin/env python3
import json, os, sys
FAKE_CLI_LOG = os.environ.get("FAKE_CLI_LOG")
with open(FAKE_CLI_LOG, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "session" and len(sys.argv) > 2 and sys.argv[2] == "create":
    print("ses_fake00000000000000000001")
elif cmd == "session" and len(sys.argv) > 2 and sys.argv[2] == "send":
    sid = "ses_fake00000000000000000001"
    engaged = os.environ.get("FAKE_SMOKE_ENGAGED", "1") == "1"
    if engaged:
        store_path = os.environ.get("FAKE_MESSAGES_STORE", "")
        try:
            with open(store_path) as f:
                _FAKE_MESSAGES = json.load(f)
        except (OSError, json.JSONDecodeError):
            _FAKE_MESSAGES = {}
        _FAKE_MESSAGES[sid] = [{"role": "assistant", "parts": [{"type": "text", "text": "PONG"}]}]
        with open(store_path, "w") as f:
            json.dump(_FAKE_MESSAGES, f)
    print('{"status": "ok", "sessionStatus": {"type": "idle"}}')
elif cmd == "session" and len(sys.argv) > 2 and sys.argv[2] == "messages":
    store = os.environ.get("FAKE_MESSAGES_STORE", "{}")
    try:
        with open(store) as f:
            msgs = json.load(f)
    except (OSError, json.JSONDecodeError):
        msgs = {}
    print(json.dumps(msgs.get("ses_fake00000000000000000001", [])))
elif cmd == "stop":
    print("stopped")
else:
    print("ok")
"""


@pytest.fixture(autouse=True)
def _fake_cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Install the fake CLI on PATH and reset its state per test."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    cli = fake_bin / "openchamber"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    log_path = tmp_path / "cli-args.log"
    monkeypatch.setenv("FAKE_CLI_LOG", str(log_path))
    store = tmp_path / "messages.json"
    monkeypatch.setenv("FAKE_MESSAGES_STORE", str(store))
    monkeypatch.setenv("FAKE_SMOKE_ENGAGED", "1")
    monkeypatch.setenv("PATH", f"{fake_bin}:" + os.environ.get("PATH", ""))
    monkeypatch.setattr(oc, "OPENCHAMBER_BIN", str(cli))
    monkeypatch.setattr(oc, "_OPENCHAMBER_BIN_DIR", str(fake_bin))
    # Point the module at throwaway iso dirs so no test touches real state.
    monkeypatch.setattr(oc, "OPENCHAMBER_CONFIG_DIR", str(tmp_path / "iso-config"))
    monkeypatch.setattr(oc, "OPENCHAMBER_SERVE_URL", "http://127.0.0.1:8791")


# -- Seeding ----------------------------------------------------------------

def test_sync_openchamber_config_seeds_state_and_patches_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    src = tmp_path / "src-openchamber"
    (src / "sub").mkdir(parents=True)
    (src / "jwt-secret").write_text("secret")
    (src / "sub" / "file").write_text("x")
    monkeypatch.setattr(oc, "_USER_OPENCHAMBER_CONFIG", str(src))

    template = tmp_path / "template.jsonc"
    template.write_text(
        '{\n  "agent": {\n    "gentle-orchestrator": {\n      "model": "opencode-go/deepseek-v4-flash"\n    }\n  }\n}\n'
    )
    monkeypatch.setattr(oc, "OPCODE_CONFIG_PATH", str(template))

    iso = Path(oc._iso_paths()[0])
    oc._sync_openchamber_config()

    # Seeded state exists in the iso config home.
    assert (iso / "openchamber" / "jwt-secret").read_text() == "secret"
    assert (iso / "openchamber" / "sub" / "file").read_text() == "x"

    # The agent model is pinned.
    patched = (iso / "opencode" / "opencode.jsonc").read_text()
    assert '"model": "kinver/professional"' in patched
    assert '"model": "opencode-go/deepseek-v4-flash"' not in patched


def test_patch_agent_model_only_touches_gentle_orchestrator() -> None:
    text = (
        '{\n  "agent": {\n    "gentle-orchestrator": {\n      "model": "opencode-go/deepseek-v4-flash"\n    },\n'
        '    "build": {\n      "model": "opencode-go/some-other"\n    }\n  }\n}\n'
    )
    patched = oc._patch_agent_model(text)
    assert patched is not None
    assert '"model": "kinver/professional"' in patched
    assert '"model": "opencode-go/some-other"' in patched  # untouched


def test_patch_agent_model_keeps_sibling_with_same_model() -> None:
    """Regression (R2): a sibling agent sharing the SAME model string must
    stay on the cloud model — only gentle-orchestrator is pinned."""
    text = (
        '{\n  "agent": {\n    "gentle-orchestrator": {\n      "model": "opencode-go/deepseek-v4-flash"\n    },\n'
        '    "build": {\n      "model": "opencode-go/deepseek-v4-flash"\n    }\n  }\n}\n'
    )
    patched = oc._patch_agent_model(text)
    assert patched is not None
    # Exactly ONE pin (gentle-orchestrator); the build sibling stays cloud.
    assert patched.count('"model": "kinver/professional"') == 1
    assert patched.count('"model": "opencode-go/deepseek-v4-flash"') == 1


def test_patch_agent_model_noop_without_marker(caplog: pytest.LogCaptureFixture) -> None:
    text = '{"agent": {"build": {"model": "opencode-go/x"}}}\n'
    assert oc._patch_agent_model(text) is None
    assert "patch failed" in caplog.text.lower()


# -- Spawn / ensure ---------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_openchamber_daemon_spawns_and_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Full happy path: spawn → health → smoke probe engaged → ok."""
    # Fake health: the daemon answers GET / via a tiny server.
    server_calls: list[str] = []

    class FakeDaemon:
        def __init__(self) -> None:
            from http.server import BaseHTTPRequestHandler, HTTPServer

            class H(BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802
                    server_calls.append(self.path)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")

                def log_message(self, *a: Any) -> None:
                    pass

            self.httpd = HTTPServer(("127.0.0.1", 8791), H)
            self.thread = None

        def serve(self) -> None:
            import threading

            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.thread.start()

        def stop(self) -> None:
            self.httpd.shutdown()
            if self.thread:
                self.thread.join(timeout=2)

    daemon = FakeDaemon()
    daemon.serve()

    async def _fake_probe() -> tuple[bool, str]:
        return True, ""

    monkeypatch.setattr(oc, "_spawn_time_smoke_probe", _fake_probe)
    try:
        # Point the spawn at the fake binary; health probe hits the server.
        ok, status = await oc.ensure_openchamber_daemon()
        assert ok
        assert status == "ok"
        assert server_calls, "health probe never hit the fake daemon"
    finally:
        daemon.stop()


@pytest.mark.asyncio
async def test_ensure_openchamber_daemon_hollow_probe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The hollow-session gate: probe failure STOPS the daemon (CRITICAL R4
    regression) and does NOT write the mtime cache, so the next ensure
    re-spawns and re-probes."""
    stopped: list[bool] = []

    async def _fake_probe() -> tuple[bool, str]:
        return False, "hollow session — managed opencode never engaged (state seeding failed)"

    async def _fake_stop() -> bool:
        stopped.append(True)
        return True

    async def _fake_spawn() -> bool:
        return True

    def _fake_running() -> bool:  # sync — the module calls it via to_thread
        return False

    monkeypatch.setattr(oc, "_spawn_time_smoke_probe", _fake_probe)
    monkeypatch.setattr(oc, "_spawn_openchamber_daemon", _fake_spawn)
    monkeypatch.setattr(oc, "stop_openchamber_daemon", _fake_stop)
    monkeypatch.setattr(oc, "is_openchamber_daemon_running", _fake_running)
    monkeypatch.setattr(oc, "OPENCHAMBER_CONFIG_DIR", str(tmp_path / "iso"))
    ok, status = await oc.ensure_openchamber_daemon()
    assert not ok
    assert status == "daemon_dead"
    assert stopped, "hollow daemon must be stopped, not left running"
    # No mtime cache may be written (the cache lives under the iso dir).
    assert not (tmp_path / "iso" / "config-mtime").exists()


@pytest.mark.asyncio
async def test_ensure_openchamber_daemon_drift_recycles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Config-drift recycle: a newer template mtime than the cache stops the
    daemon, respawns it, re-probes, and writes the new mtime (regression for
    the review finding R4-drift-recycle: the drift branch was untested)."""
    order: list[str] = []
    health_calls = 0

    async def _fake_probe() -> tuple[bool, str]:
        order.append("probe")
        return True, ""

    async def _fake_stop() -> bool:
        order.append("stop")
        return True

    async def _fake_spawn() -> bool:
        order.append("spawn")
        return True

    def _fake_running() -> bool:  # sync — the module calls it via to_thread
        # First call = initial health check (True → enter drift branch);
        # drain-loop calls = False → break immediately (no 2s drain sleep).
        nonlocal health_calls
        health_calls += 1
        return health_calls == 1

    monkeypatch.setattr(oc, "_spawn_time_smoke_probe", _fake_probe)
    monkeypatch.setattr(oc, "_spawn_openchamber_daemon", _fake_spawn)
    monkeypatch.setattr(oc, "stop_openchamber_daemon", _fake_stop)
    monkeypatch.setattr(oc, "is_openchamber_daemon_running", _fake_running)
    monkeypatch.setattr(oc, "OPENCHAMBER_CONFIG_DIR", str(tmp_path / "iso"))
    # Cached mtime is older than the template's, so drift is detected.
    (tmp_path / "iso").mkdir(exist_ok=True)
    (tmp_path / "iso" / "config-mtime").write_text("1.000000", encoding="utf-8")
    monkeypatch.setattr(oc, "_config_mtime", lambda: 2.0)

    ok, status = await oc.ensure_openchamber_daemon()

    assert ok
    assert status == "ok"
    # Drift recycle ordering: stop → drain → spawn → probe → cache write.
    assert order == ["stop", "spawn", "probe"], order
    cached = (tmp_path / "iso" / "config-mtime").read_text(encoding="utf-8")
    assert float(cached) == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_ensure_openchamber_daemon_drift_probe_failure_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Drift-recycle probe failure: the recycled daemon is hollow — stop it
    and do NOT write the new mtime (the next ensure re-spawns and re-probes)."""
    stopped: list[bool] = []
    health_calls = 0

    async def _fake_probe() -> tuple[bool, str]:
        return False, "hollow session after drift recycle"

    async def _fake_stop() -> bool:
        stopped.append(True)
        return True

    async def _fake_spawn() -> bool:
        return True

    def _fake_running() -> bool:
        # First call = health check (True → enter drift branch); drain = False.
        nonlocal health_calls
        health_calls += 1
        return health_calls == 1

    monkeypatch.setattr(oc, "_spawn_time_smoke_probe", _fake_probe)
    monkeypatch.setattr(oc, "_spawn_openchamber_daemon", _fake_spawn)
    monkeypatch.setattr(oc, "stop_openchamber_daemon", _fake_stop)
    monkeypatch.setattr(oc, "is_openchamber_daemon_running", _fake_running)
    monkeypatch.setattr(oc, "OPENCHAMBER_CONFIG_DIR", str(tmp_path / "iso"))
    (tmp_path / "iso").mkdir(exist_ok=True)
    (tmp_path / "iso" / "config-mtime").write_text("1.000000", encoding="utf-8")
    monkeypatch.setattr(oc, "_config_mtime", lambda: 2.0)

    ok, status = await oc.ensure_openchamber_daemon()

    assert not ok
    assert status == "daemon_dead"
    assert len(stopped) >= 2, "hollow recycled daemon must be stopped (drift + probe paths)"
    # The stale cache stays — the new mtime must NOT be written.
    cached = (tmp_path / "iso" / "config-mtime").read_text(encoding="utf-8")
    assert float(cached) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_smoke_probe_detects_hollow_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End-to-end probe against the fake CLI: hollow mode → no messages."""
    monkeypatch.setenv("FAKE_SMOKE_ENGAGED", "0")
    monkeypatch.setattr(oc, "_SMOKE_PROBE_POLL_S", 6.0)
    monkeypatch.setattr(oc, "_SMOKE_PROBE_INTERVAL_S", 0.5)
    ok, reason = await oc._spawn_time_smoke_probe()
    assert not ok
    assert "hollow" in reason


@pytest.mark.asyncio
async def test_smoke_probe_passes_when_runtime_engages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("FAKE_SMOKE_ENGAGED", "1")
    ok, reason = await oc._spawn_time_smoke_probe()
    assert ok, reason


def test_messages_have_content_accepts_any_text() -> None:
    """The probe passes on ANY message content — not only the PONG literal
    (R2/R3 regression): an error message proves the runtime engaged."""
    assert oc._messages_have_content('[{"role": "assistant", "parts": [{"type": "text", "text": "ERROR: model unavailable"}]}]')
    assert oc._messages_have_content('[{"role": "assistant", "parts": [{"type": "text", "text": "PONG"}]}]')
    assert not oc._messages_have_content("[]")
    assert not oc._messages_have_content("not json at all")


# -- Stop / drift -----------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_openchamber_daemon_waits_for_listener_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_running() -> bool:  # sync — the module calls it via to_thread
        return False

    monkeypatch.setattr(oc, "is_openchamber_daemon_running", _fake_running)
    assert await oc.stop_openchamber_daemon() is True


def test_is_openchamber_daemon_running_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oc, "OPENCHAMBER_SERVE_URL", "http://127.0.0.1:1")
    assert oc.is_openchamber_daemon_running() is False
  # -- Spawn lifecycle (F1) -----------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_openchamber_daemon_builds_argv_env_and_polls_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """F1: spawn lifecycle — argv, XDG env, config/auth perms, readiness polling."""
    # Make the poll loop fast and deterministic.
    monkeypatch.setattr(oc, "_DAEMON_READY_WAIT_S", 0.5)
    monkeypatch.setattr(oc, "_LISTENER_POLL_S", 0.05)
    monkeypatch.setattr(oc, "_DAEMON_STABLE_WAIT_S", 0)
    # Speed up the sleeps inside the spawn loop (keep real asyncio.sleep
    # otherwise would dominate test time for the failure-fallback path).
    orig_sleep = asyncio.sleep

    async def _fast_sleep(secs: float) -> None:
        await orig_sleep(min(secs, 0.01))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    # Template that the sync step must patch.
    template = tmp_path / "template.jsonc"
    template.write_text(
        '{\n  "agent": {\n    "gentle-orchestrator": {\n      "model": "opencode-go/deepseek-v4-flash"\n    }\n  }\n}\n'
    )
    monkeypatch.setattr(oc, "OPCODE_CONFIG_PATH", str(template))

    # Source auth.json that must be copied with 0600 into the iso data home.
    fake_auth_src = tmp_path / "auth-src.json"
    fake_auth_src.write_text('{"token": "test"}')
    monkeypatch.setattr(oc, "_USER_OPENCODE_AUTH", str(fake_auth_src))
    # No openchamber state seeding needed for this test (is dir check guards it).
    # Capture argv + env from the subprocess call.
    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 12345
        returncode = None

    async def _fake_create(*args: Any, **kwargs: Any) -> _FakeProc:  # type: ignore[no-untyped-def]
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)

    # Readiness: two polls false then true → exercises the poll loop.
    poll_count = [0]

    def _fake_running() -> bool:
        poll_count[0] += 1
        return poll_count[0] >= 3

    monkeypatch.setattr(oc, "is_openchamber_daemon_running", _fake_running)

    ok = await oc._spawn_openchamber_daemon()
    assert ok is True, "spawn should succeed when listener becomes ready"
    # -- argv shape --
    args = captured["args"]
    assert args[0] == oc.OPENCHAMBER_BIN
    assert args[1] == "serve"
    assert "--port" in args
    assert "--host" in args
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert "--ui-password" in args
    pwd = args[args.index("--ui-password") + 1]
    assert len(pwd) == 16 and all(c in "0123456789abcdef" for c in pwd)
    port = oc.OPENCHAMBER_SERVE_URL.rsplit(":", 1)[-1]
    assert args[args.index("--port") + 1] == port
    assert captured["kwargs"]["start_new_session"] is True
    # -- XDG env --
    env = captured["kwargs"]["env"]
    iso_config, iso_data, iso_cache = oc._iso_paths()
    assert env["XDG_CONFIG_HOME"] == iso_config
    assert env["XDG_DATA_HOME"] == iso_data
    assert env["XDG_CACHE_HOME"] == iso_cache
    # -- generated config --
    patched_path = Path(iso_config) / "opencode" / "opencode.jsonc"
    assert patched_path.is_file()
    patched_text = patched_path.read_text()
    assert '"model": "kinver/professional"' in patched_text
    # -- auth permissions (0600) --
    dst_auth = Path(iso_data) / "opencode" / "auth.json"
    assert dst_auth.is_file()
    mode = oct(dst_auth.stat().st_mode)[-3:]
    assert mode == "600", f"auth.json must be 0600, got {mode}"
    # -- readiness handling --
    assert poll_count[0] >= 3, "poll loop must have retried before returning True"
    # -- daemon.log created --
    assert (Path(iso_config) / "daemon.log").is_file()


@pytest.mark.asyncio
async def test_spawn_openchamber_daemon_returns_false_when_never_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """F1 failure branch: daemon never becomes ready → False."""
    monkeypatch.setattr(oc, "_DAEMON_READY_WAIT_S", 0.3)
    monkeypatch.setattr(oc, "_LISTENER_POLL_S", 0.05)
    monkeypatch.setattr(oc, "_DAEMON_STABLE_WAIT_S", 0)
    orig_sleep = asyncio.sleep

    async def _fast_sleep(secs: float) -> None:
        await orig_sleep(min(secs, 0.01))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    template = tmp_path / "template2.jsonc"
    template.write_text(
        '{\n  "agent": {\n    "gentle-orchestrator": {\n      "model": "opencode-go/deepseek-v4-flash"\n    }\n  }\n}\n'
    )
    monkeypatch.setattr(oc, "OPCODE_CONFIG_PATH", str(template))
    monkeypatch.setattr(oc, "_USER_OPENCODE_AUTH", str(tmp_path / "nonexistent-auth.json"))

    class _FakeProc:
        pid = 9999
        returncode = None

    async def _fake_create(*args: Any, **kwargs: Any) -> _FakeProc:  # type: ignore[no-untyped-def]
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(oc, "is_openchamber_daemon_running", lambda: False)

    ok = await oc._spawn_openchamber_daemon()
    assert ok is False


@pytest.mark.asyncio
async def test_spawn_openchamber_daemon_handles_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """F1 OSError branch: create_subprocess_exec raises → False, no leak."""
    monkeypatch.setattr(oc, "_DAEMON_READY_WAIT_S", 0.2)
    monkeypatch.setattr(oc, "_LISTENER_POLL_S", 0.05)
    monkeypatch.setattr(oc, "_DAEMON_STABLE_WAIT_S", 0)

    template = tmp_path / "template3.jsonc"
    template.write_text(
        '{\n  "agent": {\n    "gentle-orchestrator": {\n      "model": "opencode-go/deepseek-v4-flash"\n    }\n  }\n}\n'
    )
    monkeypatch.setattr(oc, "OPCODE_CONFIG_PATH", str(template))

    async def _raise(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-untyped-def]
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)

    ok = await oc._spawn_openchamber_daemon()
    assert ok is False
