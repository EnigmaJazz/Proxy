"""Machine-specific configuration template — safe to commit.

Copy this file to ``local_config.py`` (git-ignored) and set the values
for YOUR machine.  The proxy falls back to safe generic defaults when
``local_config.py`` is absent, so a fresh clone works without it.

Use generic placeholders here — never commit real absolute paths.
"""

# Kinver hub root (the parent directory that holds models/, prompts/, ...).
KINVER_HOME: str = "/home/you/kinver-hub"

# opencode headless serve workspace (logs, sessions).
OPENCODE_WORKSPACE_DIR: str = "/home/you/opencode-workspace"

# opencode CLI binary.
OPENCODE_BIN: str = "/home/you/.opencode/bin/opencode"

# Directory holding the opencode CLI binary (PATH entry for the serve).
OPENCODE_BIN_DIR: str = "/home/you/.opencode/bin"

# User-local bin dir (PATH entry for the serve).
LOCAL_BIN_DIR: str = "/home/you/.local/bin"

# Linuxbrew prefix (PATH entry for the serve).
LINUXBREW_PREFIX: str = "/home/you/.linuxbrew"

# Shared GPU-state env file written by the hardware monitor.
SHARED_ENV_FILE: str = "/home/you/kinver-hub/gpu_state.env"

# Fan-notify script invoked by the hardware monitor.
NOTIFY_SCRIPT: str = "/home/you/ar-notify.sh"

# Glob pattern matching the user-level nanobot install (routing.py).
UV_NANOBOT_PATTERN: str = "/home/you/.local/share/uv/tools/nanobot-ai/lib/python*/"
