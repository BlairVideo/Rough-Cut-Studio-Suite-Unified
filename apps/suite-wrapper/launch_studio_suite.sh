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
