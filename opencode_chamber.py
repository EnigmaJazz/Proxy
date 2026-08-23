"""
opencode_chamber.py — bridge-owned OpenChamber daemon lifecycle.

Step 2 of the openchamber-bridge-backend design (2026-08-16): the proxy
owns the lifecycle of the headless OpenChamber daemon that the
OpenChamberBackend (later steps) talks to — mirroring how the serve
lifecycle lives in ``opencode_bridge.py``.  Everything here is
bridge-owned: the daemon is spawned as a detached child process with
ISOLATED XDG homes under ``OPENCHAMBER_CONFIG_DIR`` (a sibling of the
serve-config dir), its managed opencode runtime is seeded from the
user's ``~/.config/openchamber`` state, and its opencode config is the
serve template patched to pin the bridge agents to
``kinver/professional`` (the CLI ``--model`` flag does NOT override an
agent's configured model — prototype-proven 2026-08-15).

Lifecycle contract mirrors the serve: ``ensure_openchamber_daemon()``
spawns on demand, recycles on template mtime drift, respawns on daemon
death; every entry point returns a status tuple and NEVER raises.

Prototype-proven facts this module bakes in (2026-08-15,
~/opencode-workspace/openchamber-prototype):

- OpenChamber's OWN state lives in ``~/.config/openchamber`` (NOT under
  XDG_DATA_HOME).  An unseeded daemon with redirected XDG homes serves
  HOLLOW sessions: ``session send`` returns ``idle`` instantly, no text
  ever, no error — the managed opencode runtime never spawns.  Seeding
  = copy ``~/.config/openchamber/.`` into ``<iso-config>/openchamber/``
  before first spawn.
- The opencode config must be COMPLETE before the daemon spawns: the
  template jsonc, the ``prompts/`` tree (``{file:./prompts/...}`` refs
  resolve relative to the config dir), and the rate-limit-fallback
  plugin config.  A config error makes the daemon give up on its
  managed opencode until restart.
- The template's agent ``model`` is a STRING (not an object): the patch
  replaces ``"model": "<old>"`` inside the ``gentle-orchestrator``
  block with ``"model": "kinver/professional"``.
- The daemon answers GET / on its port within ~2 s of spawn.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import time
from typing import Optional

from constants import (
    OPENCHAMBER_BIN,
    OPENCHAMBER_CONFIG_DIR,
    OPENCHAMBER_SERVE_URL,
    OPCODE_CONFIG_PATH,
    _machine,
    get_logger,
)

logger = get_logger("kinver.opencode_chamber")

#: How long to wait for the daemon's HTTP listener after spawn.
_DAEMON_READY_WAIT_S = 30
#: How long to wait for the managed opencode runtime to come up on a
#: fresh spawn before the first session create (the 4s control-timeout
#: retry window the backend uses lives in the backend, not here).
_DAEMON_STABLE_WAIT_S = 5.0
#: Poll interval for the listener wait loop.
_LISTENER_POLL_S = 0.5

#: The user's real OpenChamber state (the seeding source).
_USER_OPENCHAMBER_CONFIG = os.path.expanduser("~/.config/openchamber")
#: The serve's runtime opencode dir — the source of the prompts tree and
#: the plugin install (the daemon's managed opencode needs the same
#: prompts + plugin so {file:./prompts/...} refs and the fallback plugin
#: resolve identically to the serve).
_SERVE_OPENCODE_DIR = os.path.join(
    _machine("OPENCODE_SERVE_CONFIG_DIR", os.path.expanduser("~/opencode-workspace/serve-config")),
    "opencode",
)
#: The user's rate-limit-fallback plugin config (copied for fidelity).
_USER_RATE_LIMIT_PLUGIN = os.path.expanduser(
    "~/.config/opencode/rate-limit-fallback.json",
)
#: The opencode auth.json the TUI/CLI use (copied into the daemon's
#: isolated data home so its managed opencode authenticates the same way
#: the serve does — OAuth tokens, chmod 0600).
_USER_OPENCODE_AUTH = os.path.expanduser("~/.local/share/opencode/auth.json")


def _iso_paths() -> tuple[str, str, str]:
    """The daemon's isolated XDG homes (bridge-owned, sibling of serve-config)."""
    return (
        OPENCHAMBER_CONFIG_DIR,
        os.path.join(OPENCHAMBER_CONFIG_DIR, "data"),
        os.path.join(OPENCHAMBER_CONFIG_DIR, "cache"),
    )


#: The user's openchamber binary dir (the CLI the probe drives).
_OPENCHAMBER_BIN_DIR = os.path.expanduser("~/.bun/bin")
#: Bounded window for the spawn-time smoke probe's poll (the first turn
#: through the managed runtime can take a while on a cold model).
_SMOKE_PROBE_POLL_S = 120.0
_SMOKE_PROBE_INTERVAL_S = 3.0


def _chamber_env() -> dict[str, str]:
    """The isolated env the daemon AND the CLI probe share.

    Both must see the SAME XDG homes so the CLI talks to the isolated
    daemon (not the user's 8789 daemon).  PATH is pinned to the known
    toolchain dirs (bun/openchamber, ~/.local/bin, ~/.opencode/bin, the
    system dirs) so the managed opencode resolves the same tools the
    serve gets; the user's custom PATH entries are intentionally not
    inherited (deterministic tool resolution across daemon restarts).
    """
    env = dict(os.environ)
    iso_config, iso_data, iso_cache = _iso_paths()
    env["XDG_CONFIG_HOME"] = iso_config
    env["XDG_DATA_HOME"] = iso_data
    env["XDG_CACHE_HOME"] = iso_cache
    env["PATH"] = (
        _OPENCHAMBER_BIN_DIR + ":"
        + os.path.expanduser("~/.local/bin") + ":"
        + os.path.expanduser("~/.opencode/bin") + ":"
        + "/usr/local/bin:/usr/bin:/bin"
    )
    return env


def _config_mtime() -> Optional[float]:
    """Template mtime for the drift gate; None when unreadable (no-drift)."""
    try:
        return os.path.getmtime(OPCODE_CONFIG_PATH)
    except OSError:
        return None


def is_openchamber_daemon_running() -> bool:
    """True when the daemon's HTTP listener answers on OPENCHAMBER_SERVE_URL.

    Sync HTTP probe (urllib) so callers can use it from to_thread or a
    plain sync context; the serve's ``is_opencode_serve_running`` is the
    same pattern.  Never raises.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"{OPENCHAMBER_SERVE_URL}/", timeout=2.0,
        ) as resp:
            return resp.status == 200
    except (OSError, ValueError):
        return False


def _patch_agent_model(config_text: str) -> str:
    """Pin the bridge agents to kinver/professional in the daemon config.

    The CLI ``--model`` flag does not override an agent's configured
    model (prototype-proven: send used opencode-go/deepseek-v4-flash
    despite --model kinver/professional).  The template's agent model is
    a STRING — patch the ``gentle-orchestrator`` block's ``"model"``
    value only.  Deterministic string surgery on the JSONC text (the
    file is JSONC — comments — so a text patch beats json round-trip).
    """
    marker = '"gentle-orchestrator"'
    start = config_text.find(marker)
    if start < 0:
        return config_text
    # Find the model key inside this block (the block ends at the next
    # top-level key or closing brace; the template keeps agents at the
    # same indent, so scan forward to the next line at indent 0 or the
    # closing '}' of the agent block).
    end = config_text.find("\n}", start)
    if end < 0:
        return config_text
    block = config_text[start:end]
    # Replace only the gentle-orchestrator block's OWN model key: the
    # first occurrence inside the block.  Sibling agents that share the
    # same model string must stay untouched (they run the serve config's
    # cloud model, not the bridge's local pin).
    old_model = '"model": "opencode-go/deepseek-v4-flash"'
    idx = block.find(old_model)
    if idx < 0:
        return config_text
    new_block = block[:idx] + '"model": "kinver/professional"' + block[idx + len(old_model):]
    return config_text[:start] + new_block + config_text[end:]


def _sync_openchamber_config() -> None:
    """Assemble the daemon's opencode config BEFORE spawn (never raises).

    Sync file I/O: callers run it via asyncio.to_thread.  The config
    must be complete before the daemon spawns (a config error makes the
    daemon give up on its managed opencode until restart):
      1. the serve template (permission pre-allow + kinver provider),
         patched to pin the bridge agent to kinver/professional
      2. the prompts tree ({file:./prompts/...} refs resolve relative
         to the config dir; cp -rT semantics — the dest exists)
      3. the rate-limit-fallback plugin config (fidelity)
      4. the opencode auth.json into the ISOLATED data home (0600)
    """
    try:
        iso_config, iso_data, _ = _iso_paths()
        oc_dir = os.path.join(iso_config, "opencode")
        os.makedirs(oc_dir, exist_ok=True)

        # 1. Template → patched opencode.jsonc
        template = open(OPCODE_CONFIG_PATH, encoding="utf-8").read()
        patched = _patch_agent_model(template)
        with open(os.path.join(oc_dir, "opencode.jsonc"), "w", encoding="utf-8") as f:
            f.write(patched)

        # 2. Prompts tree (from the serve's runtime dir — the live copy).
        src_prompts = os.path.join(_SERVE_OPENCODE_DIR, "prompts")
        dst_prompts = os.path.join(oc_dir, "prompts")
        if os.path.isdir(src_prompts):
            if os.path.isdir(dst_prompts):
                shutil.rmtree(dst_prompts)
            shutil.copytree(src_prompts, dst_prompts)

        # 3. Rate-limit fallback plugin config (optional, for fidelity).
        if os.path.isfile(_USER_RATE_LIMIT_PLUGIN):
            shutil.copy2(_USER_RATE_LIMIT_PLUGIN, os.path.join(oc_dir, "rate-limit-fallback.json"))

        # 4. Auth into the ISOLATED data home (the managed opencode reads
        #    auth from its data home; the serve does the same — 0600).
        if os.path.isfile(_USER_OPENCODE_AUTH):
            dst_auth = os.path.join(iso_data, "opencode", "auth.json")
            os.makedirs(os.path.dirname(dst_auth), exist_ok=True)
            shutil.copy2(_USER_OPENCODE_AUTH, dst_auth)
            try:
                os.chmod(dst_auth, 0o600)
            except OSError:
                pass

        # Seed OpenChamber's OWN state (the hollow-session gotcha).
        if os.path.isdir(_USER_OPENCHAMBER_CONFIG):
            dst_oc_state = os.path.join(iso_config, "openchamber")
            os.makedirs(dst_oc_state, exist_ok=True)
            for entry in os.listdir(_USER_OPENCHAMBER_CONFIG):
                src = os.path.join(_USER_OPENCHAMBER_CONFIG, entry)
                dst = os.path.join(dst_oc_state, entry)
                if os.path.isdir(src):
                    if os.path.isdir(dst):
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                elif os.path.isfile(src):
                    shutil.copy2(src, dst)
    except OSError as exc:
        logger.error("openchamber config sync failed: %s", exc)


async def _spawn_openchamber_daemon() -> bool:
    """Spawn the daemon detached with isolated XDG homes (never raises)."""
    log_handle = None
    try:
        iso_config, iso_data, iso_cache = _iso_paths()
        os.makedirs(iso_config, exist_ok=True)
        os.makedirs(iso_data, exist_ok=True)
        os.makedirs(iso_cache, exist_ok=True)
        await asyncio.to_thread(_sync_openchamber_config)

        port = OPENCHAMBER_SERVE_URL.rsplit(":", 1)[-1]
        daemon_log = os.path.join(OPENCHAMBER_CONFIG_DIR, "daemon.log")
        log_handle = open(daemon_log, "ab")

        env = _chamber_env()

        proc = await asyncio.create_subprocess_exec(
            OPENCHAMBER_BIN, "serve",
            "--port", port,
            "--host", "127.0.0.1",
            "--ui-password", secrets.token_hex(8),
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            env=env,
        )
        # The child holds the log fd; the parent must not keep its copy
        # (leaks one fd per spawn/recycle cycle otherwise).
        log_handle.close()
        logger.info(
            "Opened openchamber daemon (pid=%s) on %s", proc.pid, OPENCHAMBER_SERVE_URL,
        )
        for _ in range(int(_DAEMON_READY_WAIT_S / _LISTENER_POLL_S)):
            await asyncio.sleep(_LISTENER_POLL_S)
            if await asyncio.to_thread(is_openchamber_daemon_running):
                # Let the managed runtime settle before the first create
                # (the backend's create retry handles the 4s control
                # timeout; this just avoids the first-call race).
                await asyncio.sleep(_DAEMON_STABLE_WAIT_S)
                return True
    except asyncio.CancelledError:
        raise
    except OSError as exc:
        logger.error("Failed to spawn openchamber daemon: %s", exc)
    finally:
        if log_handle is not None:
            # The child holds the log fd after a successful exec; on the
            # OSError path this is what prevents the fd leak (R3/R4).
            log_handle.close()
    return False


async def stop_openchamber_daemon() -> bool:
    """Best-effort stop of the daemon on OPENCHAMBER_SERVE_URL (never raises).

    Returns True when the listener is gone afterwards (or was never up).
    """
    try:
        port = OPENCHAMBER_SERVE_URL.rsplit(":", 1)[-1]
        proc = await asyncio.create_subprocess_exec(
            OPENCHAMBER_BIN, "stop", "--port", port,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=15.0)
    except (OSError, asyncio.TimeoutError):
        pass
    # Wait for the listener to go away.
    for _ in range(20):
        if not await asyncio.to_thread(is_openchamber_daemon_running):
            return True
        await asyncio.sleep(0.5)
    return False


async def _run_cli(args: list[str], timeout: float) -> tuple[int, str]:
    """Run one openchamber CLI command in the isolated env (never raises).

    Returns ``(returncode, stdout_text)``; stderr is captured alongside
    stdout so hollow-session diagnostics are visible in the probe log.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            OPENCHAMBER_BIN, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_chamber_env(),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, (out or b"").decode("utf-8", "replace")
    except (OSError, asyncio.TimeoutError):
        return 1, ""


async def _spawn_time_smoke_probe() -> tuple[bool, str]:
    """Verify the managed opencode runtime actually runs turns (never raises).

    The design's spawn-time gate: an unseeded daemon serves HOLLOW
    sessions (send returns idle instantly, no text, no error — the
    managed runtime never spawned).  A listener health check cannot see
    that; only one real create → send → poll cycle proves the runtime
    engaged.  Pass = the session accumulates ANY message content after
    the send (assistant text OR an error message — the runtime
    responded; a model outage is transient, hollowness is not).
    """
    import tempfile

    try:
        with tempfile.TemporaryDirectory(prefix="oc-smoke-") as workdir:
            rc, out = await _run_cli(
                ["session", "create", "--dir", workdir], timeout=30.0,
            )
            if rc != 0:
                return False, f"smoke create failed (rc={rc})"
            m = re.search(r"ses_[A-Za-z0-9]+", out)
            if not m:
                return False, "smoke create returned no session id"
            session_id = m.group(0)

            rc, _ = await _run_cli(
                ["session", "send", "--session", session_id, "--dir", workdir,
                 "--prompt", "Reply with exactly: PONG", "--wait"],
                timeout=30.0,
            )
            if rc != 0:
                return False, f"smoke send failed (rc={rc})"

            deadline = time.monotonic() + _SMOKE_PROBE_POLL_S
            while time.monotonic() < deadline:
                rc, out = await _run_cli(
                    ["session", "messages", "--session", session_id, "--dir", workdir],
                    timeout=15.0,
                )
                # ANY message content (assistant text OR an error
                # message) proves the runtime engaged; hollowness leaves
                # the list empty forever.  The model's actual reply is
                # irrelevant — only that the runtime produced something.
                if rc == 0 and _messages_have_content(out):
                    logger.info("openchamber smoke probe: runtime engaged (turn completed)")
                    return True, ""
                await asyncio.sleep(_SMOKE_PROBE_INTERVAL_S)
            return False, "hollow session — managed opencode never engaged (state seeding failed)"
    except OSError as exc:
        return False, f"smoke probe error: {exc}"


def _messages_have_content(messages_json: str) -> bool:
    """True when the session messages output carries ANY content.

    Tolerates the CLI's plain-JSON shape and an empty/error body; only a
    non-empty array with at least one text/error part counts as engaged.
    """
    try:
        data = json.loads(messages_json)
        if not isinstance(data, list) or not data:
            return False
        for msg in data:
            if not isinstance(msg, dict):
                continue
            parts = msg.get("parts") or []
            for part in parts:
                if isinstance(part, dict) and str(part.get("text") or "").strip():
                    return True
        return False
    except (ValueError, TypeError):
        # Not JSON at all (e.g. a CLI error banner) — no content proven.
        return False


async def ensure_openchamber_daemon() -> tuple[bool, str]:
    """Ensure the OpenChamber daemon is up (spawn if missing).  Never raises.

    Returns ``(ok, status)``:
      ``(True, "ok")``                 — daemon answering
      ``(False, "daemon_dead")``       — health check failed and respawn
                                        failed, OR the spawn-time smoke probe
                                        found a hollow session (the daemon is
                                        stopped so the next call re-spawns)
    Mirrors the serve's ``ensure_opencode_serve``: health-first, spawn on
    demand, template-mtime drift recycles the daemon before the next use.
    """
    if await asyncio.to_thread(is_openchamber_daemon_running):
        mtime = _config_mtime()
        if mtime is not None:
            cached = _read_cached_mtime()
            if cached is not None and mtime > cached:
                logger.warning(
                    "openchamber config drifted (mtime %.3f > cached %.3f) — "
                    "recycling daemon", mtime, cached,
                )
                await stop_openchamber_daemon()
                # Drain the old listener so the respawn can bind the port.
                for _ in range(8):
                    if not await asyncio.to_thread(is_openchamber_daemon_running):
                        break
                    await asyncio.sleep(0.25)
                ok = await _spawn_openchamber_daemon()
                if ok:
                    ok, probe = await _spawn_time_smoke_probe()
                    if not ok:
                        # CRITICAL (R4): a hollow daemon must NOT stay up
                        # and must NOT be cached as current — stop it and
                        # skip the mtime write so the next ensure
                        # re-spawns and re-probes instead of serving
                        # hollow sessions forever.
                        logger.error("openchamber smoke probe failed after drift recycle: %s", probe)
                        await stop_openchamber_daemon()
                        return False, "daemon_dead"
                    _write_cached_mtime(_config_mtime())
                return (True, "ok") if ok else (False, "daemon_dead")
        return True, "ok"
    # Daemon absent or dead: (re)spawn.
    ok = await _spawn_openchamber_daemon()
    if ok:
        ok, probe = await _spawn_time_smoke_probe()
        if not ok:
            # Same CRITICAL guard as the drift path: stop the hollow
            # daemon and do NOT cache the mtime, so the next ensure
            # re-spawns and re-probes.
            logger.error("openchamber smoke probe failed on fresh spawn: %s", probe)
            await stop_openchamber_daemon()
            return False, "daemon_dead"
        _write_cached_mtime(_config_mtime())
    return (True, "ok") if ok else (False, "daemon_dead")


# -- mtime drift cache (scalar, per-process; same class as _serve_config_mtime) --


def _cached_mtime_path() -> str:
    """Resolve the mtime cache path lazily (test isolation friendly)."""
    return os.path.join(OPENCHAMBER_CONFIG_DIR, "config-mtime")


def _read_cached_mtime() -> Optional[float]:
    try:
        with open(_cached_mtime_path(), encoding="utf-8") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_cached_mtime(mtime: Optional[float]) -> None:
    if mtime is None:
        return
    try:
        path = _cached_mtime_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{mtime:.6f}")
    except OSError:
        pass
