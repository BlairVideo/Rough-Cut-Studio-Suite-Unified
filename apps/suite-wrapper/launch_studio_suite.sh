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

echo "[suite] Syncing the shared workspace venv (uv sync)..."
(cd "$REPO_ROOT" && uv sync --package suite-wrapper)

exec "$REPO_ROOT/.venv/bin/python" main.py "$@"
