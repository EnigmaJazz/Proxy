"""Tests for opencode_backends.py — transport backends + the selector.

The openchamber CLI transport is exercised against a FAKE ``openchamber``
CLI on PATH (session-aware create/send/messages/list with a JSON message
store), mirroring the fake-CLI fixture in test_opencode_chamber.py.  No
real daemon is ever started: ``ensure_openchamber_daemon`` and
``is_openchamber_daemon_running`` are stubbed in the autouse fixture.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

import opencode_backends as ob
import opencode_chamber as oc

FAKE_SESSION_ID = "ses_fake00000000000000000001"
FAKE_MESSAGE_ID = "msg_fake00000000000000000001"


FAKE_CLI = r"""#!/usr/bin/env python3
import json, os, sys
FAKE_CLI_LOG = os.environ.get("FAKE_CLI_LOG")
with open(FAKE_CLI_LOG, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")

def _arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else ""

def _load_store():
    try:
        with open(os.environ["FAKE_MESSAGES_STORE"]) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

def _save_store(store):
    with open(os.environ["FAKE_MESSAGES_STORE"], "w") as f:
        json.dump(store, f)

cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "session" and len(sys.argv) > 2:
    sub = sys.argv[2]
    if sub == "create":
        # The failure counter lives in the SHARED store, not the env: an
        # env decrement inside the subprocess would die with it.
        store = _load_store()
        fails = store.get("create_failures_left", 0)
        if fails > 0:
            store["create_failures_left"] = fails - 1
            _save_store(store)
            print("create error", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"status": "ok", "sessionId": "ses_fake00000000000000000001",
                          "sessionStatus": {"type": "idle"}}))
    elif sub == "send":
        sid = _arg("--session")
        store = _load_store()
        if os.environ.get("FAKE_SMOKE_ENGAGED", "1") == "1":
            msgs = store.get(sid, [])
            msgs.append({"id": "msg_fake00000000000000000001", "role": "assistant",
                         "createdAt": 1786820611723, "completedAt": 1786820706479,
                         "text": "PONG"})
            store[sid] = msgs
            _save_store(store)
        print(json.dumps({"status": "ok", "action": "send", "sessionId": sid,
                          "model": {"providerID": "kinver", "modelID": "professional"},
                          "promptDispatched": True, "sessionStatus": {"type": "idle"}}))
    elif sub == "messages":
        sid = _arg("--session")
        if os.environ.get("FAKE_UNKNOWN_SESSION", "") == sid:
            print("session not found", file=sys.stderr)
            sys.exit(1)
        store = _load_store()
        print(json.dumps({"status": "ok", "sessionId": sid,
                          "sessionStatus": {"type": "idle"},
                          "messages": store.get(sid, [])}))
    elif sub == "list":
        store = _load_store()
        print(json.dumps({"status": "ok", "sessions": [{"id": sid} for sid in store]}))
    else:
        print("ok")
else:
    print("ok")
"""


@pytest.fixture(autouse=True)
def _fake_cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Install the fake CLI on PATH + stub the daemon lifecycle per test.

    Mirrors test_opencode_chamber's fixture: the fake binary records argv
    and answers create/send/messages/list from a JSON store.  The daemon
    lifecycle is stubbed so no real spawn ever happens; the module-level
    ``BACKEND`` instance is untouched (each test builds its own
    ``OpenChamberBackend()``).
    """
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    cli = fake_bin / "openchamber"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    monkeypatch.setenv("FAKE_CLI_LOG", str(tmp_path / "cli-args.log"))
    monkeypatch.setenv("FAKE_MESSAGES_STORE", str(tmp_path / "messages.json"))
    monkeypatch.setenv("FAKE_SMOKE_ENGAGED", "1")
    monkeypatch.setenv("PATH", f"{fake_bin}:" + os.environ.get("PATH", ""))
    monkeypatch.setattr(oc, "OPENCHAMBER_BIN", str(cli))
    monkeypatch.setattr(oc, "_OPENCHAMBER_BIN_DIR", str(fake_bin))
    monkeypatch.setattr(oc, "OPENCHAMBER_CONFIG_DIR", str(tmp_path / "iso-config"))
    monkeypatch.setattr(oc, "OPENCHAMBER_SERVE_URL", "http://127.0.0.1:8791")

    async def _fake_ensure() -> tuple[bool, str]:
        return True, "ok"

    monkeypatch.setattr(oc, "ensure_openchamber_daemon", _fake_ensure)
    monkeypatch.setattr(oc, "is_openchamber_daemon_running", lambda: True)


def _backend() -> ob.OpenChamberBackend:
    return ob.OpenChamberBackend()


def _seed_store(tmp_path: Path, msgs: list[dict[str, Any]]) -> None:
    store = tmp_path / "messages.json"
    store.write_text(json.dumps({FAKE_SESSION_ID: msgs}))


def _seed_create_failures(tmp_path: Path, failures: int) -> None:
    (tmp_path / "messages.json").write_text(json.dumps({"create_failures_left": failures}))


def _cli_calls(tmp_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (tmp_path / "cli-args.log").read_text().splitlines()]


# -- Selector ---------------------------------------------------------------

def test_backend_selector_defaults_to_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCODE_BACKEND", raising=False)
    mod = importlib.reload(ob)
    assert isinstance(mod.BACKEND, mod.ServeBackend)
    assert not isinstance(mod.BACKEND, mod.OpenChamberBackend)


def test_backend_selector_openchamber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_BACKEND", "openchamber")
    mod = importlib.reload(ob)
    assert isinstance(mod.BACKEND, mod.OpenChamberBackend)
    # Restore the production default for the rest of the suite.
    monkeypatch.delenv("OPENCODE_BACKEND", raising=False)
    importlib.reload(ob)


def test_backend_selector_unknown_value_falls_back_to_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_BACKEND", "bogus")
    mod = importlib.reload(ob)
    assert isinstance(mod.BACKEND, mod.ServeBackend)
    monkeypatch.delenv("OPENCODE_BACKEND", raising=False)
    importlib.reload(ob)


# -- Create / send / messages round-trip ------------------------------------

@pytest.mark.asyncio
async def test_create_session_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    backend = _backend()
    workdir = str(tmp_path / "work")
    async with httpx.AsyncClient() as client:
        resp = await backend.create_session(client, directory=workdir)
    assert resp.status_code == 200
    assert resp.json() == {"id": FAKE_SESSION_ID}
    # The CLI argv is recorded verbatim.
    assert _cli_calls(tmp_path) == [["session", "create", "--dir", workdir, "--json"]]
    # The directory is recorded for the later --dir-carrying calls.
    assert backend._session_dirs == {FAKE_SESSION_ID: workdir}


@pytest.mark.asyncio
async def test_create_session_retries_control_timeout_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _seed_create_failures(tmp_path, 2)
    backend = _backend()
    async with httpx.AsyncClient() as client:
        resp = await backend.create_session(client, directory=str(tmp_path / "work"))
    assert resp.status_code == 200
    assert len(_cli_calls(tmp_path)) == 3  # two failures + one success


@pytest.mark.asyncio
async def test_create_session_fails_after_exhausted_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _seed_create_failures(tmp_path, 99)
    monkeypatch.setattr(ob, "_CREATE_RETRIES", 2)
    backend = _backend()
    async with httpx.AsyncClient() as client:
        resp = await backend.create_session(client, directory=str(tmp_path / "work"))
    assert resp.status_code == 500
    assert len(_cli_calls(tmp_path)) == 2  # exactly the retry budget


@pytest.mark.asyncio
async def test_create_session_raises_when_daemon_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _fake_ensure_fail() -> tuple[bool, str]:
        return False, "daemon_dead"

    monkeypatch.setattr(oc, "ensure_openchamber_daemon", _fake_ensure_fail)
    backend = _backend()
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await backend.create_session(client, directory=str(tmp_path / "work"))


@pytest.mark.asyncio
async def test_send_prompt_dispatches_via_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    backend = _backend()
    workdir = str(tmp_path / "work")
    async with httpx.AsyncClient() as client:
        await backend.create_session(client, directory=workdir)
        resp = await backend.send_prompt(
            client, FAKE_SESSION_ID,
            agent="gentle-orchestrator", system_prompt="sys", user_text="hello",
            model_id=None, provider_id="kinver",
        )
    assert resp.status_code == 204
    send_call = _cli_calls(tmp_path)[-1]
    assert send_call[0:4] == ["session", "send", "--session", FAKE_SESSION_ID]
    assert send_call[4:6] == ["--dir", workdir]
    assert send_call[6:8] == ["--prompt", "hello"]
    assert "--wait" in send_call and "--json" in send_call


@pytest.mark.asyncio
async def test_fetch_messages_normalizes_plain_text_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The prototype's messages payload (plain text fields, no parts) is
    translated into the serve's message-list shape the bridge consumes."""
    _seed_store(tmp_path, [
        {"id": "msg_user1", "role": "user", "text": "hello"},
        {"id": "msg_assistant1", "role": "assistant", "text": "PONG"},
    ])
    backend = _backend()
    backend._session_dirs[FAKE_SESSION_ID] = str(tmp_path / "work")
    async with httpx.AsyncClient() as client:
        resp = await backend.fetch_messages(client, FAKE_SESSION_ID)
    assert resp.status_code == 200
    body = resp.json()
    assert body[0] == {
        "info": {"role": "user"},
        "parts": [{"type": "text", "text": "hello"}],
        "id": "msg_user1",
    }
    assert body[1] == {
        "info": {"role": "assistant"},
        "parts": [{"type": "text", "text": "PONG"}],
        "id": "msg_assistant1",
    }


@pytest.mark.asyncio
async def test_send_message_returns_assistant_text_parts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    backend = _backend()
    workdir = str(tmp_path / "work")
    monkeypatch.setattr(ob, "_POLL_INTERVAL_S", 0.05)
    async with httpx.AsyncClient() as client:
        await backend.create_session(client, directory=workdir)
        resp = await backend.send_message(
            client, FAKE_SESSION_ID,
            agent="gentle-orchestrator", system_prompt="sys", user_text="hello",
            model_id=None, provider_id="kinver", timeout=10.0,
        )
    assert resp.status_code == 200
    assert resp.json()["parts"] == [{"type": "text", "text": "PONG"}]


@pytest.mark.asyncio
async def test_send_message_wedge_timeout_returns_empty_parts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Hollow session (no assistant text ever): the poll burns the budget
    and the blocking caller surfaces the empty-response error."""
    monkeypatch.setenv("FAKE_SMOKE_ENGAGED", "0")
    monkeypatch.setattr(ob, "_POLL_INTERVAL_S", 0.02)
    backend = _backend()
    workdir = str(tmp_path / "work")
    async with httpx.AsyncClient() as client:
        await backend.create_session(client, directory=workdir)
        resp = await backend.send_message(
            client, FAKE_SESSION_ID,
            agent="gentle-orchestrator", system_prompt="sys", user_text="hello",
            model_id=None, provider_id="kinver", timeout=0.25,
        )
    assert resp.status_code == 200
    assert resp.json()["parts"] == []


# -- Existence / abort ------------------------------------------------------

@pytest.mark.asyncio
async def test_session_exists_live_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    backend = _backend()
    backend._session_dirs[FAKE_SESSION_ID] = str(tmp_path / "work")
    async with httpx.AsyncClient() as client:
        resp = await backend.session_exists(client, FAKE_SESSION_ID)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_session_exists_gone_session_returns_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("FAKE_UNKNOWN_SESSION", FAKE_SESSION_ID)
    backend = _backend()
    backend._session_dirs[FAKE_SESSION_ID] = str(tmp_path / "work")
    async with httpx.AsyncClient() as client:
        resp = await backend.session_exists(client, FAKE_SESSION_ID)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_session_exists_daemon_down_raises_keeps_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(oc, "is_openchamber_daemon_running", lambda: False)
    backend = _backend()
    backend._session_dirs[FAKE_SESSION_ID] = str(tmp_path / "work")
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await backend.session_exists(client, FAKE_SESSION_ID)


@pytest.mark.asyncio
async def test_abort_session_is_logged_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    backend = _backend()
    async with httpx.AsyncClient() as client:
        await backend.abort_session(client, FAKE_SESSION_ID)  # never raises
    assert not (tmp_path / "cli-args.log").exists()  # no CLI call made


# -- Serve-specific no-op mappings -----------------------------------------

@pytest.mark.asyncio
async def test_permission_and_question_noops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    backend = _backend()
    async with httpx.AsyncClient() as client:
        perms = await backend.fetch_permissions(client)
        assert perms.status_code == 200
        assert perms.json() == []
        assert (await backend.fetch_questions(client)).json() == []
        assert await backend.reply_permission(client, "s1", "p1", "always") is True
        assert await backend.reply_question(client, "s1", "q1", "yes", 1) is True
    assert not (tmp_path / "cli-args.log").exists()  # no CLI call made


@pytest.mark.asyncio
async def test_fetch_message_finds_and_misses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _seed_store(tmp_path, [
        {"id": "msg_a1", "role": "assistant", "text": "PONG"},
    ])
    backend = _backend()
    backend._session_dirs[FAKE_SESSION_ID] = str(tmp_path / "work")
    async with httpx.AsyncClient() as client:
        hit = await backend.fetch_message(client, FAKE_SESSION_ID, "msg_a1")
        miss = await backend.fetch_message(client, FAKE_SESSION_ID, "msg_nope")
    assert hit.status_code == 200
    assert hit.json() == {"parts": [{"type": "text", "text": "PONG"}]}
    assert miss.status_code == 404


@pytest.mark.asyncio
async def test_session_status_and_list_normalize(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _seed_store(tmp_path, [
        {"id": "msg_a1", "role": "assistant", "text": "PONG"},
    ])
    backend = _backend()
    async with httpx.AsyncClient() as client:
        st = await backend.fetch_session_status(client)
        listed = await backend.list_sessions(client)
    assert st.status_code == 200
    assert st.json() == {FAKE_SESSION_ID: {"type": "idle"}}
    assert listed.status_code == 200
    assert listed.json() == [{"id": FAKE_SESSION_ID}]
