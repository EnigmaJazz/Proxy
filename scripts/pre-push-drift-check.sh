#!/usr/bin/env bash
# Pre-push gate: block the push when config/model_profiles.yaml has drifted
# from what the filesystem scan produces.  Runs locally because the check
# reads the GGUF model files, which exist only on this machine.
#
# Install:  ln -s ../../scripts/pre-push-drift-check.sh .git/hooks/pre-push
# Skip once: DRIFT_SKIP=1 git push

if [ "${DRIFT_SKIP:-0}" = "1" ]; then
  exit 0
fi

root="$(git rev-parse --show-toplevel)" || exit 1

"$root/.venv/bin/python" "$root/tools/sync_model_profiles.py" --check || {
  echo "Model profile drift check failed. Regenerate and commit config/model_profiles.yaml." >&2
  echo "Bypass once: DRIFT_SKIP=1 git push" >&2
  exit 1
}
