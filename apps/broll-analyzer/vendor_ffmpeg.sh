#!/bin/bash
# vendor_ffmpeg.sh -- prepare a relocatable ffmpeg/ffprobe for bundling
# into the .app (see setup.py's VENDOR_DIR handling and
# app.py's _ensure_homebrew_on_path).
#
# Run this ONCE on the Mac doing the build, before `python setup.py
# py2app` (or just use ./build_app.sh, which calls this automatically).
# It is entirely optional: skip it and the app still builds and runs
# fine, it just needs Homebrew's ffmpeg present on whatever machine
# runs it (same as every earlier build of this app).
#
# Why this isn't as simple as `cp $(which ffmpeg) vendor/ffmpeg/bin/`:
# Homebrew's ffmpeg/ffprobe binaries are dynamically linked against a
# pile of Homebrew-installed .dylib files (libx264, libvpx, libmp3lame,
# etc.), referenced by their absolute Homebrew install paths (e.g.
# /opt/homebrew/opt/x264/lib/libx264.dylib). Copying just the binaries
# into the app bundle would produce something that only actually works
# on a machine that *also* happens to have that exact same Homebrew
# layout -- not self-contained at all. `dylibbundler` rewrites those
# references to relative, bundle-internal paths and copies the
# .dylib files alongside the binaries, which is the standard tool for
# exactly this job (what py2app itself does for pure Python C
# extensions, just not for arbitrary external binaries it doesn't know
# about).
#
# This deliberately reuses whatever ffmpeg is already installed via
# Homebrew (per the project's existing macOS/Homebrew workflow) rather
# than downloading a prebuilt static binary from a third party, so
# there's nothing here fetching or trusting an unfamiliar binary over
# the network -- only the same Homebrew install this app has always
# recommended (see BUILD_MACOS.md prerequisites).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR_DIR="$SCRIPT_DIR/vendor/ffmpeg"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "vendor_ffmpeg.sh must be run on macOS (it inspects/rewrites" >&2
    echo "Mach-O binaries with otool/install_name_tool)." >&2
    exit 1
fi

FFMPEG_BIN="$(command -v ffmpeg || true)"
FFPROBE_BIN="$(command -v ffprobe || true)"

if [[ -z "$FFMPEG_BIN" || -z "$FFPROBE_BIN" ]]; then
    echo "ffmpeg/ffprobe not found on PATH. Install via Homebrew first:" >&2
    echo "    brew install ffmpeg" >&2
    exit 1
fi

if ! command -v dylibbundler >/dev/null 2>&1; then
    echo "dylibbundler not found. Install via Homebrew first:" >&2
    echo "    brew install dylibbundler" >&2
    exit 1
fi

echo "Vendoring ffmpeg from:  $FFMPEG_BIN"
echo "Vendoring ffprobe from: $FFPROBE_BIN"

rm -rf "$VENDOR_DIR"
mkdir -p "$VENDOR_DIR/bin" "$VENDOR_DIR/lib"

cp "$FFMPEG_BIN" "$VENDOR_DIR/bin/ffmpeg"
cp "$FFPROBE_BIN" "$VENDOR_DIR/bin/ffprobe"
chmod +w "$VENDOR_DIR/bin/ffmpeg" "$VENDOR_DIR/bin/ffprobe"

# dylibbundler copies each binary's dependent .dylib files into -d and
# rewrites the binary's own load commands (via install_name_tool) to
# point at them using @executable_path/../lib/<name> -- relative to
# wherever the binary itself ends up, so this keeps working once
# py2app moves both into Contents/Resources/bin and Contents/Resources/lib.
# -of forces overwriting a fixup that's already been (partially) done,
# so re-running this script is safe.
for bin in "$VENDOR_DIR/bin/ffmpeg" "$VENDOR_DIR/bin/ffprobe"; do
    dylibbundler -od -b \
        -x "$bin" \
        -d "$VENDOR_DIR/lib" \
        -p "@executable_path/../lib/" \
        -of
done

echo
echo "Done. Vendored, relocatable copies are at:"
echo "  $VENDOR_DIR/bin/ffmpeg"
echo "  $VENDOR_DIR/bin/ffprobe"
echo "  $VENDOR_DIR/lib/*.dylib  ($(find "$VENDOR_DIR/lib" -type f | wc -l | tr -d ' ') file(s))"
echo
echo "Sanity check -- running the vendored ffprobe standalone:"
"$VENDOR_DIR/bin/ffprobe" -version | head -1
echo
echo "Next: python setup.py py2app  (setup.py will pick these up automatically)"
