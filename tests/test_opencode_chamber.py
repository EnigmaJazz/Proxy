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
import pytest_asyncio

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
    # Exactly ONE pin (gentle-orchestrator); the build sibling stays cloud.
    assert patched.count('"model": "kinver/professional"') == 1
    assert patched.count('"model": "opencode-go/deepseek-v4-flash"') == 1


def test_patch_agent_model_noop_without_marker() -> None:
    text = '{"agent": {"build": {"model": "opencode-go/x"}}}\n'
    assert oc._patch_agent_model(text) == text


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
