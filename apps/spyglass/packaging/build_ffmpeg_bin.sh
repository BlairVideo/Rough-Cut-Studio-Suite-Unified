#!/usr/bin/env bash
# Produces a fully de-Homebrewed ffmpeg + ffprobe pair for the packaged
# Spyglass.app (Phase 7), reusing Rough Cut Studio's proven dylibbundler
# recipe (see ../../Rough Cut Studio/HANDOFF.md's packaging section)
# generalized from one binary to two sharing a single libs/ directory --
# ffmpeg and ffprobe are separate executables in the same Homebrew
# `ffmpeg` formula and link nearly the same ~30 non-system dylibs
# (libavcodec, libavformat, libswscale, openssl, ...), so one
# dylibbundler invocation covering both avoids bundling that set twice.
#
# Never run dylibbundler against the real Homebrew binaries -- always a
# copy in a staging directory, since it rewrites the binary in place.
#
# Idempotent: wipes and rebuilds build_staging/ffmpeg-bin each run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE_DIR="$SCRIPT_DIR/build_staging/ffmpeg-bin"

if ! command -v dylibbundler >/dev/null 2>&1; then
  echo "dylibbundler not found -- install with 'brew install dylibbundler'" >&2
  exit 1
fi

FFMPEG_PREFIX="$(brew --prefix ffmpeg)"
if [ ! -x "$FFMPEG_PREFIX/bin/ffmpeg" ] || [ ! -x "$FFMPEG_PREFIX/bin/ffprobe" ]; then
  echo "Homebrew ffmpeg/ffprobe not found at $FFMPEG_PREFIX/bin -- install with 'brew install ffmpeg'" >&2
  exit 1
fi

echo "== Rebuilding ffmpeg-bin from scratch =="
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/libs"

echo "== Copying Homebrew ffmpeg/ffprobe (never bundling the real Homebrew binaries in place) =="
cp "$FFMPEG_PREFIX/bin/ffmpeg" "$STAGE_DIR/ffmpeg"
cp "$FFMPEG_PREFIX/bin/ffprobe" "$STAGE_DIR/ffprobe"

echo "== Running dylibbundler over both binaries, sharing one libs/ dir =="
dylibbundler -od -b \
  -x "$STAGE_DIR/ffmpeg" \
  -x "$STAGE_DIR/ffprobe" \
  -d "$STAGE_DIR/libs" \
  -p "@executable_path/libs/"

echo "== Verifying self-containment under a stripped PATH (real encode + probe, not just -version) =="
TMP_CLIP="$(mktemp -t spyglass_ffmpeg_probe).mp4"
env -i PATH=/usr/bin:/bin "$STAGE_DIR/ffmpeg" -y -f lavfi -i "testsrc=duration=1:size=64x64:rate=5" -c:v libx264 "$TMP_CLIP"
env -i PATH=/usr/bin:/bin "$STAGE_DIR/ffprobe" -v error -show_entries stream=codec_name -of json "$TMP_CLIP"
rm -f "$TMP_CLIP"

echo "== Sweeping for leaked absolute Homebrew paths =="
LEAKED=0
for f in "$STAGE_DIR/ffmpeg" "$STAGE_DIR/ffprobe" "$STAGE_DIR"/libs/*; do
  if otool -L "$f" 2>/dev/null | grep -qE '/opt/homebrew|/usr/local'; then
    echo "LEAK: $f"
    otool -L "$f" | grep -E '/opt/homebrew|/usr/local'
    LEAKED=1
  fi
done

if [ "$LEAKED" -ne 0 ]; then
  echo "One or more files still reference an absolute Homebrew path -- not relocatable." >&2
  exit 1
fi

echo "== ffmpeg-bin staged cleanly at $STAGE_DIR =="
