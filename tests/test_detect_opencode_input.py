"""Tests for scripts/detect_opencode_input.py (agent-needs-input detection).

The script is importable as a module (repo root is on sys.path via
conftest), so the detection core and the ``--once`` exit-code contract are
exercised directly against a canned threaded HTTP server that mimics the
opencode serve API — no live serve needed.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import pytest

from scripts import detect_opencode_input as doi


class _CannedServe(ThreadingHTTPServer):
    """Serve fixed JSON per path, like the real opencode serve API."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def base_url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        server: _CannedServe = self.server
        body = server.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # silence test noise
        pass


@pytest.fixture
def serve():
    """Yield a running canned serve; stop it afterwards."""
    server = _CannedServe({})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _perm(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "perm_1",
        "sessionID": "sess_abc",
        "permission": "external_directory",
        "tool": "bash",
        "patterns": ["/home/**"],
        "metadata": {"filepath": "/home/james/data.txt"},
    }
    base.update(overrides)
    return base


def _question(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "q_1",
        "sessionID": "sess_abc",
        "questions": [{"question": "Which test framework?"}],
    }
    base.update(overrides)
    return base


def test_clean_serve_needs_no_input(serve: _CannedServe) -> None:
    serve.routes = {
        "/permission": [],
        "/question": [],
        "/session/status": {"sess_abc": {"type": "busy"}},
    }
    snap = doi.poll_serve(serve.base_url, timeout=1.0)
    assert snap.serve_alive
    assert not snap.needs_input
    assert snap.busy_sessions == ["sess_abc"]  # busy is context, not a signal
    assert doi.main(["--once", "--base-url", serve.base_url]) == 0


def test_permission_signal_uses_filepath_target(serve: _CannedServe) -> None:
    serve.routes = {
        "/permission": [_perm(permission="write", tool="edit")],
        "/question": [],
        "/session/status": {},
    }
    snap = doi.poll_serve(serve.base_url, timeout=1.0)
    assert snap.needs_input
    sig = snap.signals[0]
    assert sig.kind == "permission"
    assert sig.session_id == "sess_abc"
    assert sig.request_id == "perm_1"
    assert sig.target == "/home/james/data.txt"
    assert sig.tool == "edit"
    assert sig.detail == "write (edit)"


def test_bash_permission_targets_the_command(serve: _CannedServe) -> None:
    serve.routes = {
        "/permission": [_perm(permission="bash", tool="bash",
                              metadata={"command": "git push origin main"})],
        "/question": [],
        "/session/status": {},
    }
    snap = doi.poll_serve(serve.base_url, timeout=1.0)
    assert snap.needs_input
    assert snap.signals[0].target == "git push origin main"
    assert snap.signals[0].detail == "bash"


def test_question_signal_detected(serve: _CannedServe) -> None:
    serve.routes = {
        "/permission": [],
        "/question": [_question()],
        "/session/status": {},
    }
    snap = doi.poll_serve(serve.base_url, timeout=1.0)
    assert snap.needs_input
    sig = snap.signals[0]
    assert sig.kind == "question"
    assert sig.request_id == "q_1"
    assert sig.target == "Which test framework?"
    assert doi.main(["--once", "--base-url", serve.base_url]) == 1


def test_session_filter_ignores_other_sessions(serve: _CannedServe) -> None:
    serve.routes = {
        "/permission": [
            _perm(id="perm_1", sessionID="sess_abc"),
            _perm(id="perm_2", sessionID="sess_other"),
        ],
        "/question": [],
        "/session/status": {},
    }
    snap = doi.poll_serve(serve.base_url, timeout=1.0, session_filter="sess_other")
    assert [s.session_id for s in snap.signals] == ["sess_other"]


def test_duplicate_signals_deduped_per_session_kind(serve: _CannedServe) -> None:
    serve.routes = {
        "/permission": [_perm(id="perm_1"), _perm(id="perm_2")],
        "/question": [],
        "/session/status": {},
    }
    snap = doi.poll_serve(serve.base_url, timeout=1.0)
    assert len(snap.signals) == 1


def test_serve_unreachable_exits_2() -> None:
    snap = doi.poll_serve("http://127.0.0.1:1", timeout=0.5)
    assert not snap.serve_alive
    assert doi.main(["--once", "--base-url", "http://127.0.0.1:1", "--timeout", "0.5"]) == 2


def test_unexpected_api_shape_is_an_error(serve: _CannedServe) -> None:
    serve.routes = {
        "/permission": {"not": "a list"},
        "/question": [],
        "/session/status": {},
    }
    snap = doi.poll_serve(serve.base_url, timeout=1.0)
    assert not snap.serve_alive
    assert "unexpected API shape" in snap.error


def test_json_render_shape(serve: _CannedServe, capsys: pytest.CaptureFixture) -> None:
    serve.routes = {
        "/permission": [_perm()],
        "/question": [],
        "/session/status": {},
    }
    assert doi.main(["--once", "--json", "--base-url", serve.base_url]) == 1
    line = json.loads(capsys.readouterr().out.strip())
    assert line["needs_input"] is True
    assert line["signals"][0]["kind"] == "permission"
    assert line["signals"][0]["session_id"] == "sess_abc"
