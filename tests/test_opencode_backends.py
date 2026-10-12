"""Tests for opencode_backends.py — the selector and the secure backend.

The openchamber CLI transport and its fake-CLI fixture were retired in
bridge v4 T9, so these tests cover the two remaining transports: the
selector map (serve / secure / unset / unknown), the secure backend's
ownership and permission flags, its ``127.0.0.1:4096`` URLs, and the
no-spawn/no-signal lifecycle guard.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import types
from typing import Any, Optional

import httpx
import pytest

import opencode_backends as ob


# -- Selector ---------------------------------------------------------------

def test_backend_selector_defaults_to_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCODE_BACKEND", raising=False)
    mod = importlib.reload(ob)
    assert isinstance(mod.BACKEND, mod.ServeBackend)


def test_backend_selector_unknown_value_falls_back_to_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_BACKEND", "bogus")
    mod = importlib.reload(ob)
    assert isinstance(mod.BACKEND, mod.ServeBackend)
    monkeypatch.delenv("OPENCODE_BACKEND", raising=False)
    importlib.reload(ob)


# -- Secure backend (bridge v4, T4) -----------------------------------------

@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, "ServeBackend"),
        ("serve", "ServeBackend"),
        ("bogus", "ServeBackend"),
        ("secure", "SecureBackend"),
    ],
)
def test_backend_selector_maps_each_value(
    monkeypatch: pytest.MonkeyPatch,
    env_value: Optional[str],
    expected: str,
) -> None:
    """secure/serve/unset/unknown each pick the right class."""
    if env_value is None:
        monkeypatch.delenv("OPENCODE_BACKEND", raising=False)
    else:
        monkeypatch.setenv("OPENCODE_BACKEND", env_value)
    mod = importlib.reload(ob)
    assert type(mod.BACKEND).__name__ == expected
    # Restore the production default for the rest of the suite.
    monkeypatch.delenv("OPENCODE_BACKEND", raising=False)
    importlib.reload(ob)


def test_secure_backend_owns_no_server() -> None:
    """SecureBackend is a concrete ServeBackend pointed at 127.0.0.1:4096
    that never owns the server lifecycle; the serve backend still does."""
    assert ob.SecureBackend.__abstractmethods__ == frozenset()
    assert issubclass(ob.SecureBackend, ob.ServeBackend)
    assert ob.SecureBackend.base_url == "http://127.0.0.1:4096"
    assert ob.SecureBackend.owns_server is False
    assert ob.ServeBackend.owns_server is True


def test_answers_permissions_flag_scoped_to_secure() -> None:
    """The permission-relay capability defaults True and is switched off
    ONLY for the secure backend (T5, ct-58464)."""
    assert ob.OpenCodeBackend.answers_permissions is True
    assert ob.ServeBackend.answers_permissions is True
    assert ob.SecureBackend.answers_permissions is False


class _RecordingResp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _RecordingClient:
    """httpx.AsyncClient stand-in that records every URL it is asked to hit."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> _RecordingResp:
        self.urls.append(url)
        return _RecordingResp()

    async def post(self, url: str, **kwargs: Any) -> _RecordingResp:
        self.urls.append(url)
        return _RecordingResp()


def _send_signatures() -> list[Any]:
    """Every send primitive that could carry a model override: the ABC
    contract plus the concrete serve transport (T6, ct-58463)."""
    return [
        ob.OpenCodeBackend.send_prompt,
        ob.OpenCodeBackend.send_message,
        ob.ServeBackend.send_prompt,
        ob.ServeBackend.send_message,
    ]


def test_send_signatures_carry_no_model_override_params() -> None:
    """T6 (ct-58463): no send primitive accepts model_id/provider_id, so a
    caller CANNOT hand the transport a model override — model selection
    belongs to the server's own agent config."""
    import inspect

    for fn in _send_signatures():
        params = inspect.signature(fn).parameters
        assert "model_id" not in params, (fn, sorted(params))
        assert "provider_id" not in params, (fn, sorted(params))


def test_send_signatures_carry_no_agent_param() -> None:
    """T8-found agent fix, rule 7: no send primitive accepts an ``agent``
    parameter, so a caller CANNOT pin the agent — the server's own config
    (its ``default_agent``) selects the agent, exactly as for the
    interactive session."""
    import inspect

    for fn in _send_signatures():
        params = inspect.signature(fn).parameters
        assert "agent" not in params, (fn, sorted(params))


class _PayloadClient:
    """httpx.AsyncClient stand-in that records each POST's JSON body."""

    def __init__(self) -> None:
        self.payloads: list[Any] = []

    async def post(self, url: str, **kwargs: Any) -> _RecordingResp:
        self.payloads.append(kwargs.get("json"))
        return _RecordingResp(200)


@pytest.mark.asyncio
async def test_send_payload_never_carries_model_override() -> None:
    """T6 (ct-58463) rule 7: the outbound payload has NO ``model`` key on
    either send primitive — the streaming prompt_async (``send_prompt``)
    and the blocking message POST (``send_message``)."""
    backend = ob.ServeBackend()
    client = _PayloadClient()
    await backend.send_prompt(
        client, "ses1", system_prompt="s",
        user_text="u",
    )
    await backend.send_message(
        client, "ses1", system_prompt="s",
        user_text="u", timeout=1.0,
    )
    assert len(client.payloads) == 2, client.payloads
    for payload in client.payloads:
        assert "model" not in payload, payload
        assert "agent" not in payload, payload
        assert payload["system"] == "s", payload
        assert payload["parts"] == [{"type": "text", "text": "u"}], payload


@pytest.mark.asyncio
async def test_send_payload_never_carries_agent_key() -> None:
    """T8-found agent fix, rule 7: the outbound payload has NO ``agent`` key
    on either send primitive — the streaming prompt_async (``send_prompt``)
    and the blocking message POST (``send_message``).  Agent selection
    belongs to the server's own config, so the bridge cannot pin a
    read-only agent (mirrors the T6 model-override removal)."""
    backend = ob.ServeBackend()
    client = _PayloadClient()
    await backend.send_prompt(
        client, "ses1", system_prompt="s", user_text="u",
    )
    await backend.send_message(
        client, "ses1", system_prompt="s", user_text="u", timeout=1.0,
    )
    assert len(client.payloads) == 2, client.payloads
    for payload in client.payloads:
        assert "agent" not in payload, payload
        assert payload["system"] == "s", payload
        assert payload["parts"] == [{"type": "text", "text": "u"}], payload


@pytest.mark.asyncio
async def test_secure_backend_urls_target_port_4096() -> None:
    """Every one of the 13 ABC primitives builds a URL at 127.0.0.1:4096."""
    backend = ob.SecureBackend()
    client = _RecordingClient()
    await backend.create_session(client, directory="/tmp/work")
    await backend.send_prompt(
        client, "ses1", system_prompt="s", user_text="u",
    )
    await backend.fetch_messages(client, "ses1")
    await backend.session_exists(client, "ses1")
    await backend.abort_session(client, "ses1")
    await backend.send_message(
        client, "ses1", system_prompt="s", user_text="u",
        timeout=1.0,
    )
    await backend.fetch_message(client, "ses1", "mid1")
    await backend.fetch_session_status(client)
    await backend.list_sessions(client)
    await backend.fetch_permissions(client)
    assert await backend.reply_permission(client, "ses1", "pid1", "always")
    await backend.fetch_questions(client)
    assert await backend.reply_question(client, "ses1", "qid1", "yes", 1)
    assert client.urls, "recorder saw no calls"
    assert all(
        u.startswith("http://127.0.0.1:4096/") for u in client.urls
    ), client.urls


class _DownAsyncClient:
    """httpx.AsyncClient stand-in whose GET raises (server down)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_DownAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _RecordingResp:
        raise httpx.ConnectError("connection refused")


class _UpAsyncClient:
    """httpx.AsyncClient stand-in whose GET answers (server up)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_UpAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _RecordingResp:
        return _RecordingResp(200)


@pytest.fixture
def _lifecycle_recorders(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[tuple[int, int]]]:
    """Record every subprocess creation and every signal sent."""
    created: list[str] = []
    signals: list[tuple[int, int]] = []

    def _rec_popen(*args: Any, **kwargs: Any) -> Any:
        created.append("subprocess.Popen")
        return types.SimpleNamespace(pid=424242)

    def _rec_run(*args: Any, **kwargs: Any) -> Any:
        created.append("subprocess.run")
        return types.SimpleNamespace(returncode=0)

    async def _rec_exec(*args: Any, **kwargs: Any) -> Any:
        created.append("asyncio.create_subprocess_exec")
        return types.SimpleNamespace(pid=424242)

    monkeypatch.setattr(subprocess, "Popen", _rec_popen)
    monkeypatch.setattr(subprocess, "run", _rec_run)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _rec_exec)
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(
        os, "killpg", lambda pid, sig: signals.append((pid, sig)),
    )
    return created, signals


@pytest.mark.asyncio
async def test_secure_ensure_probe_only_when_down(
    monkeypatch: pytest.MonkeyPatch,
    _lifecycle_recorders: tuple[list[str], list[tuple[int, int]]],
) -> None:
    """Server DOWN under the secure backend: ensure is a pure probe — it
    returns False, the failure text names the secure server, and no
    subprocess is created and no signal is sent."""
    import opencode_bridge as obr

    created, signals = _lifecycle_recorders
    monkeypatch.setattr(obr, "BACKEND", ob.SecureBackend())
    monkeypatch.setattr(obr.httpx, "AsyncClient", _DownAsyncClient)

    assert await obr.ensure_opencode_serve() is False
    assert created == []
    assert signals == []
    msg = obr._serve_unreachable_message()
    assert "secure" in msg and "4096" in msg, msg
    # The blocking caller surfaces that same clear error and still spawns
    # nothing (ensure returns False before any httpx client is built).
    err = await obr.opencode_chat("hello", timeout=1.0)
    assert "secure" in err and "4096" in err, err
    assert created == []
    assert signals == []


@pytest.mark.asyncio
async def test_secure_ensure_probe_only_when_up(
    monkeypatch: pytest.MonkeyPatch,
    _lifecycle_recorders: tuple[list[str], list[tuple[int, int]]],
) -> None:
    """Server UP under the secure backend: ensure returns True and still
    never spawns or signals (this is the proxy.py startup call path)."""
    import opencode_bridge as obr

    created, signals = _lifecycle_recorders
    monkeypatch.setattr(obr, "BACKEND", ob.SecureBackend())
    monkeypatch.setattr(obr.httpx, "AsyncClient", _UpAsyncClient)

    assert await obr.ensure_opencode_serve() is True
    assert created == []
    assert signals == []


@pytest.mark.asyncio
async def test_secure_lifecycle_functions_are_noops(
    monkeypatch: pytest.MonkeyPatch,
    _lifecycle_recorders: tuple[list[str], list[tuple[int, int]]],
) -> None:
    """The guard sites themselves: recycle / force-recycle / spawn /
    config-sync all return immediately under a non-owning backend — no
    subprocess and no SIGTERM, even when called directly."""
    import opencode_bridge as obr

    created, signals = _lifecycle_recorders
    monkeypatch.setattr(obr, "BACKEND", ob.SecureBackend())

    await obr._recycle_serve_if_low_memory()
    await obr._force_recycle_serve("test")
    assert await obr._spawn_serve(None) is False
    obr._sync_serve_config()

    assert created == []
    assert signals == []
