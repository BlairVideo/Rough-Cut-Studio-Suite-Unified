#!/bin/bash
# sync_copies.sh — one-way CODE sync between the two full copies of the tree:
#
#   primary:  /Users/cj/Developer/Blair/Rough Cut Studio Suite
#             (moved 2026-07-20 from /Applications/ Claude Apps/ Rough Cut
#             Studio Suite — leading-space path retired, see git history)
#   derived:  /Applications/ Claude Apps/Rough Cut Studio Suite — All-In-One
#
# RETIRED 2026-07-14: the derived tree was archived (compressed, verified
# byte-identical, then removed) as part of the venv-consolidation pass —
# see VENV_CONSOLIDATION_PLAN.md "step 7". The archive lives at
# ~/Archives/Rough Cut Studio Suite — All-In-One (archived 2026-07-14).tar.gz.
# This script now exits with a pointer to that fact instead of the old
# generic "both trees must exist" error. If the derived tree is ever
# restored (extracted from the archive) or a new second copy is created,
# this script works exactly as before with no changes needed.
#
# Only code/doc files travel (.py .js .css .html .md .sh .txt). It NEVER
# touches, in either direction:
#   - assets/            (favorites.json, transcripts, logos, graphics — the
#                         two copies legitimately hold DIFFERENT user data)
#   - .env               (secrets never sync; SEC-1 scrubs them per-copy)
#   - .venv/ .venv-base/ .venv.bak*/ __pycache__/ _generated/ .DS_Store
#     (.venv-base is the shared cross-app package venv introduced by the
#     venv-consolidation pass — each app venv points at it via a .pth file;
#     it must stay per-tree, not sync, and definitely not partially sync
#     via the .py/.md include whitelist below, which would otherwise copy
#     stray source files out of torch/numpy/etc. without their binaries)
#
# Usage:
#   ./sync_copies.sh          # copy changed code files primary -> All-In-One
#   ./sync_copies.sh --check  # dry-run: report code drift, change nothing
#
# No --delete: a file removed from the primary must be removed from the
# derived copy deliberately, by hand. See MASTER_BLUEPRINT.md §A-0.
set -euo pipefail

SRC="/Users/cj/Developer/Blair/Rough Cut Studio Suite/"
DST="/Applications/ Claude Apps/Rough Cut Studio Suite — All-In-One/"

MODE="sync"
if [ "${1:-}" = "--check" ]; then
  MODE="check"
elif [ -n "${1:-}" ]; then
  echo "Unknown option: $1 (only --check is supported)" >&2
  exit 2
fi

if [ ! -d "$SRC" ] || [ ! -d "$DST" ]; then
  echo "ERROR: expected both tree copies to exist:" >&2
  echo "  $SRC" >&2
  echo "  $DST" >&2
  if [ ! -d "$DST" ]; then
    echo "" >&2
    echo "The derived copy was archived and removed 2026-07-14 (see" >&2
    echo "VENV_CONSOLIDATION_PLAN.md). The archive is at:" >&2
    echo "  ~/Archives/Rough Cut Studio Suite — All-In-One (archived 2026-07-14).tar.gz" >&2
    echo "This script has nothing to do until/unless that tree is restored" >&2
    echo "or a new second copy is created at the path above." >&2
  fi
  exit 1
fi

# --checksum: compare by content, not mtime — the copies were historically
# synced with `cp`, which leaves differing timestamps on identical files.
FLAGS=(-a --checksum --itemize-changes)
[ "$MODE" = "check" ] && FLAGS+=(--dry-run)

# Filter order matters (first match wins): hard excludes, then the code
# whitelist, then exclude everything else.
CHANGES=$(rsync "${FLAGS[@]}" \
  --exclude '.venv/' \
  --exclude '.venv-base/' \
  --exclude '.venv.bak*/' \
  --exclude '__pycache__/' \
  --exclude '_generated/' \
  --exclude '.pytest_cache/' \
  --exclude 'assets/' \
  --exclude '.env' \
  --exclude '.DS_Store' \
  --exclude '*.pyc' \
  --include '*/' \
  --include '*.py' \
  --include '*.js' \
  --include '*.css' \
  --include '*.html' \
  --include '*.md' \
  --include '*.sh' \
  --include '*.txt' \
  --exclude '*' \
  "$SRC" "$DST" | grep '^>f' || true)
# '^>f' keeps only real content transfers; '.f..t....' lines are
# timestamp-attribute touches on byte-identical files — not drift.

if [ -z "$CHANGES" ]; then
  echo "IN SYNC — no code drift between the two copies."
else
  if [ "$MODE" = "check" ]; then
    echo "DRIFT — these code files differ (primary -> All-In-One):"
  else
    echo "SYNCED these code files (primary -> All-In-One):"
  fi
  echo "$CHANGES"
fi
