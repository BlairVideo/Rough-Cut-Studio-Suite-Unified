#!/usr/bin/env bash
# tools/run_tests.sh — run every workspace member's pytest suite, each in
# its own subprocess AND scoped to that member's own tests/ directory.
#
# Why this exists instead of a plain `uv run pytest` from the repo root:
# that collects every app's tests into ONE shared Python process, and
# Python caches imported modules globally by name in sys.modules. Three
# apps in this monorepo (blair-brander, broll-analyzer, interview-
# transcriber) each name their real Tkinter/Streamlit entrypoint
# `app.py` — fine standalone, but in one combined pytest session
# whichever app's `app.py` happens to import first silently "wins" for
# every other app's `import app` for the rest of that session (a hard
# ImportError once an expected name is missing — what actually surfaced
# once blair-brander's and interview-transcriber's test suites both
# started importing their own app.py — or worse, silently wrong
# behavior if two apps' app.py ever defined a same-named function
# differently).
#
# `uv run --package X` alone is NOT enough: it only selects which
# member's dependency set/venv the command resolves against, not what
# directory pytest actually scans. Invoked from the repo root with no
# path argument, pytest still walks the whole tree from cwd and
# re-collects every member's tests into that one subprocess -- so this
# script also `cd`s into each member's own directory before invoking
# pytest, so that member's own pytest.ini (testpaths = tests) actually
# scopes collection to just its own suite.
#
# Usage:
#   tools/run_tests.sh              # run every member's suite
#   tools/run_tests.sh -k foo       # forward extra args to every `pytest` call
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Workspace members with a pytest suite (pytest.ini + tests/), by
# directory name under apps/ (which matches each member's uv package
# name for all seven of these). Members without either (harmonizer-
# backend, spyglass/crates/spyglass-py, rcs-utils) are skipped -- there's
# nothing for pytest to collect there yet.
MEMBERS=(
  a-sync
  blair-brander
  broll-analyzer
  colorize
  interview-transcriber
  rough-cut-studio
  suite-wrapper
)

FAILED=()
PASSED=()

for name in "${MEMBERS[@]}"; do
  echo
  echo "=== $name ==="
  if (cd "$REPO_ROOT/apps/$name" && uv run --package "$name" pytest "$@"); then
    PASSED+=("$name")
  else
    FAILED+=("$name")
  fi
done

echo
echo "==================== summary ===================="
if [ "${#PASSED[@]}" -gt 0 ]; then
  for name in "${PASSED[@]}"; do
    echo "  PASS  $name"
  done
fi
if [ "${#FAILED[@]}" -gt 0 ]; then
  for name in "${FAILED[@]}"; do
    echo "  FAIL  $name"
  done
fi

if [ "${#FAILED[@]}" -gt 0 ]; then
  echo
  echo "${#FAILED[@]} of ${#MEMBERS[@]} member(s) failed: ${FAILED[*]}"
  exit 1
fi

echo
echo "All ${#MEMBERS[@]} member(s) passed."
