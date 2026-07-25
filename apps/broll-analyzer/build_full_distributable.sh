#!/bin/bash
# build_full_distributable.sh
#
# Assembles the "B-Roll Analyzer (Full)" folder: a relocatable copy of
# your working build_env venv plus the app source, laid out so
# launch_broll_analyzer.sh (wrapped by Platypus into a .app) can find and
# run them. Run this from the project folder that already contains a
# working build_env/ (the one you tested `torchvision.ops.nms` in).
#
# Usage:
#   ./build_full_distributable.sh
#
# Result:
#   dist_full/B-Roll Analyzer (Full)/
#     ├── runtime/   <- relocatable copy of build_env
#     └── src/       <- app.py, analyzer.py, vision_energy.py, etc.
#
# After this runs, open Platypus, point it at launch_broll_analyzer.sh,
# set the icon to AppIcon.icns, and save the generated .app directly into
# the same "B-Roll Analyzer (Full)" folder alongside runtime/ and src/.
# See PLATYPUS_PACKAGING.md for the exact Platypus settings.

set -euo pipefail

PROJECT_DIR="$(pwd)"
VENV_SRC="$PROJECT_DIR/build_env"
DIST_DIR="$PROJECT_DIR/dist_full/B-Roll Analyzer (Full)"

SOURCE_FILES=(
    app.py
    analyzer.py
    vision_energy.py
    xml_export.py
    result_cache.py
    app_settings.py
    AppIcon.icns
)

if [[ ! -d "$VENV_SRC" ]]; then
    echo "ERROR: build_env/ not found in $PROJECT_DIR" >&2
    echo "Run this from the project folder, with build_env/ already set up" >&2
    echo "and torch/torchvision/open_clip_torch installed and verified." >&2
    exit 1
fi

echo "== Verifying build_env has a working torch/torchvision pair =="
"$VENV_SRC/bin/python3" -c "
import torch, torchvision
torchvision.ops.nms
print(f'torch {torch.__version__} / torchvision {torchvision.__version__} OK')
"

echo "== Cleaning previous dist_full/ =="
rm -rf "$PROJECT_DIR/dist_full"
mkdir -p "$DIST_DIR/runtime" "$DIST_DIR/src"

echo "== Copying venv (this can take a minute -- torch is large) =="
# --copy-links: relocatable venvs the pip3 venv module makes are usually
# real files already, but some Homebrew Pythons symlink the interpreter
# itself, so this forces a self-contained copy that doesn't depend on the
# original build_env still existing at its original path.
rsync -a --copy-links "$VENV_SRC/" "$DIST_DIR/runtime/"

echo "== Fixing up the venv's own internal paths for its new location =="
# venvs record their original absolute path in a few places (pyvenv.cfg,
# and shebang lines in bin/*); update them so `runtime/bin/python3` is
# self-consistent no matter where this distributable folder ends up.
NEW_VENV_PATH="$DIST_DIR/runtime"
if [[ -f "$NEW_VENV_PATH/pyvenv.cfg" ]]; then
    sed -i '' "s|^home = .*|home = $(dirname "$(readlink -f "$NEW_VENV_PATH/bin/python3" 2>/dev/null || echo "$NEW_VENV_PATH/bin/python3")")|" "$NEW_VENV_PATH/pyvenv.cfg" || true
fi
for f in "$NEW_VENV_PATH"/bin/*; do
    if [[ -f "$f" ]] && head -c 2 "$f" 2>/dev/null | grep -q '^#!'; then
        sed -i '' "1s|^#!.*/bin/python.*|#!$NEW_VENV_PATH/bin/python3|" "$f" 2>/dev/null || true
    fi
done

echo "== Copying source files =="
for f in "${SOURCE_FILES[@]}"; do
    if [[ -f "$PROJECT_DIR/$f" ]]; then
        cp "$PROJECT_DIR/$f" "$DIST_DIR/src/$f"
    else
        echo "  WARNING: $f not found in $PROJECT_DIR, skipping" >&2
    fi
done

echo ""
echo "Done. Distributable staged at:"
echo "  $DIST_DIR"
echo ""
echo "Next: open Platypus, use launch_broll_analyzer.sh as the script,"
echo "save the .app directly into that folder (see PLATYPUS_PACKAGING.md),"
echo "then zip the whole '$( basename "$DIST_DIR" )' folder to share it."
