#!/usr/bin/env bash
# Builds a relocatable copy of the Python sidecar (Phase 7 packaging) that
# can run on any Apple Silicon Mac -- no system Python, no Homebrew.
#
# The dev sidecar's `.venv` (see ../sidecar/.venv/pyvenv.cfg) is built from
# python.org's Framework Python, whose interpreter binary links
# `/Library/Frameworks/Python.framework/...` by absolute path -- that
# framework won't exist on another machine, so a plain `cp -r` of the dev
# venv does not work elsewhere. Everything else installed into the venv
# (torch, opencv, Pillow, etc.) already uses @rpath/@loader_path-relative
# linkage and needs no relinking -- confirmed by an otool -L sweep of the
# dev venv before writing this script. So the fix is narrowly scoped to
# the interpreter: rebuild the venv from a python-build-standalone
# (astral-sh) CPython distribution, which is built specifically for
# redistribution, instead of python.org's Framework build. Prebuilt wheels
# don't care which CPython distribution produced the interpreter, so
# ../sidecar/requirements.txt is reused unchanged.
#
# Idempotent: wipes and rebuilds build_staging/sidecar-runtime each run.
# Output layout mirrors the dev `sidecar/` directory on purpose (.venv/ +
# the two scripts as siblings), so `resolve_venv_python`/
# `SidecarCommand::real`/`EmbedServer::start` in the Rust code need no
# changes to work against either tree.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Pinned python-build-standalone release (bump deliberately, not silently --
# re-derive the SHA256 below from that release's own SHA256SUMS file when
# you do).
PBS_TAG="20260718"
PBS_ASSET="cpython-3.11.15+20260718-aarch64-apple-darwin-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_ASSET}"
PBS_SHA256="125587d03495bebdf30ec9e549a8469c97c0925d863ff401f24f157fd44d91d6"

PYTHON_STANDALONE_DIR="$SCRIPT_DIR/python-standalone"
STAGE_DIR="$SCRIPT_DIR/build_staging/sidecar-runtime"

echo "== Fetching python-build-standalone ${PBS_TAG} (if not already cached) =="
mkdir -p "$PYTHON_STANDALONE_DIR"
ARCHIVE="$PYTHON_STANDALONE_DIR/$PBS_ASSET"
if [ ! -f "$ARCHIVE" ]; then
  curl -sL -o "$ARCHIVE" "$PBS_URL"
fi

echo "== Verifying SHA256 =="
ACTUAL_SHA256="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
if [ "$ACTUAL_SHA256" != "$PBS_SHA256" ]; then
  echo "SHA256 mismatch for $ARCHIVE" >&2
  echo "  expected: $PBS_SHA256" >&2
  echo "  actual:   $ACTUAL_SHA256" >&2
  exit 1
fi

INTERP_DIR="$PYTHON_STANDALONE_DIR/extracted"
if [ ! -x "$INTERP_DIR/python/bin/python3.11" ]; then
  echo "== Extracting interpreter =="
  rm -rf "$INTERP_DIR"
  mkdir -p "$INTERP_DIR"
  tar -xzf "$ARCHIVE" -C "$INTERP_DIR"
fi
STANDALONE_PYTHON="$INTERP_DIR/python/bin/python3.11"

echo "== Rebuilding sidecar-runtime from scratch =="
# Spotlight (mdworker) actively indexes a freshly-created venv this large
# and can drop a fresh .DS_Store back into a directory `rm -rf` just
# emptied, making it report "Directory not empty" -- retry a few times
# rather than failing on that transient race.
for attempt in 1 2 3 4 5; do
  rm -rf "$STAGE_DIR" 2>/dev/null && break
  sleep 1
done
mkdir -p "$SCRIPT_DIR/build_staging"
# Ask Spotlight not to index build_staging/ at all -- avoids the race
# above recurring on every future rebuild, not just this one.
touch "$SCRIPT_DIR/build_staging/.metadata_never_index"
mkdir -p "$STAGE_DIR"

echo "== Creating relocatable venv (--copies, not the default symlink mode) =="
"$STANDALONE_PYTHON" -m venv --copies "$STAGE_DIR/.venv"

echo "== Installing sidecar/requirements.txt =="
"$STAGE_DIR/.venv/bin/pip" install --upgrade pip
"$STAGE_DIR/.venv/bin/pip" install -r "$REPO_ROOT/sidecar/requirements.txt"

echo "== Copying sidecar scripts in as siblings of .venv/ =="
cp "$REPO_ROOT/sidecar/analyze_clip.py" "$STAGE_DIR/analyze_clip.py"
cp "$REPO_ROOT/sidecar/embed_text_server.py" "$STAGE_DIR/embed_text_server.py"
cp "$REPO_ROOT/sidecar/parent_watchdog.py" "$STAGE_DIR/parent_watchdog.py"

echo "== Verifying critical imports resolve under a stripped PATH =="
env -i PATH=/usr/bin:/bin "$STAGE_DIR/.venv/bin/python3.11" -c "
import torch, transformers, open_clip, cv2, scenedetect, accelerate, einops
from PIL import Image
torch.zeros(1)
print('torch', torch.__version__, '/ transformers', transformers.__version__, '/ cv2', cv2.__version__)
"

echo "== Sweeping for leaked absolute paths (Framework/Homebrew) =="
# Excludes /System/Library/Frameworks (real, always-present macOS system
# frameworks -- fine to reference) -- only python.org's own
# /Library/Frameworks/Python.framework, Homebrew, or /usr/local are leaks.
LEAKED=0
while IFS= read -r -d '' f; do
  leaked_lines="$(otool -L "$f" 2>/dev/null | grep -E '/Library/Frameworks|/opt/homebrew|/usr/local' | grep -v '/System/Library' || true)"
  if [ -n "$leaked_lines" ]; then
    echo "LEAK: $f"
    echo "$leaked_lines"
    LEAKED=1
  fi
done < <(find "$STAGE_DIR" \( -name "*.so" -o -name "*.dylib" -o -name "python3.11" \) -print0)

if [ "$LEAKED" -ne 0 ]; then
  echo "One or more files reference an absolute Framework/Homebrew path -- not relocatable." >&2
  exit 1
fi

echo "== sidecar-runtime staged cleanly at $STAGE_DIR =="
