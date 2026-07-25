#!/bin/bash
# launch_studio_suite.sh — one-command launcher for Rough Cut Studio Suite.
#
# Post-monorepo-migration: dependencies are no longer this app's own local
# .venv + requirements.txt. Every Python app in the suite (this one
# included) shares ONE `uv`-managed venv at the repo root (see paths.py's
# SHARED_VENV_PYTHON and ../../pyproject.toml's [tool.uv.workspace]) — this
# script just syncs and runs against that shared environment instead of
# creating its own. Safe to re-run any time.
set -euo pipefail

cd "$(dirname "$0")"
REPO_ROOT="$(cd ../.. && pwd)"

# Same class of bug as ffprobe_util.py's _ensure_common_ffmpeg_dirs_on_path()
# (see this repo's suite-wrapper CLAUDE.md "GUI-launch gotchas"): a process
# launched via Finder/Dock/`open` (as opposed to a Terminal shell) gets a
# minimal PATH that doesn't include Homebrew's bin dir -- that's normally
# added by .zshrc/.zprofile, which only load for interactive shells. `uv`
# is genuinely installed (`brew install uv`), but without this the packaged
# .app fails at this exact line with "uv: command not found" even though
# the identical script works fine run directly from a terminal. Only ever
# APPENDS known install locations that exist and aren't already present --
# never removes or reorders anything already on PATH.
for dir in /opt/homebrew/bin /opt/homebrew/sbin /usr/local/bin /opt/local/bin; do
    if [ -d "$dir" ] && [[ ":$PATH:" != *":$dir:"* ]]; then
        PATH="$PATH:$dir"
    fi
done
export PATH

echo "[suite] Syncing the shared workspace venv (uv sync)..."
# Deliberately a FULL workspace sync, not `--package suite-wrapper`. This is
# one shared venv across every app (see paths.py's SHARED_VENV_PYTHON) --
# scoping the sync to just this package's own deps uninstalls everything
# the OTHER apps' worker subprocesses need at runtime (torch, mlx-whisper,
# streamlit, pyannote, ...), since uv treats an unlisted package as no
# longer wanted in that venv. Caught by an actual end-to-end launch, not by
# any test -- the test suite mocks worker subprocesses rather than
# depending on the live venv's package set.
(cd "$REPO_ROOT" && uv sync)

exec "$REPO_ROOT/.venv/bin/python" main.py "$@"
