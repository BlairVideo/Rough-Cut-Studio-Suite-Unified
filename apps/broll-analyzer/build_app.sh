#!/bin/bash
# build_app.sh -- one-command build of a self-contained B-Roll Analyzer.app
# plus a distributable .dmg, ready to hand to anyone on a Mac.
#
# Run on macOS, from this folder:
#     ./build_app.sh              # ffmpeg NOT vendored (needs Homebrew's
#                                  # ffmpeg on whatever machine runs the app)
#     ./build_app.sh --vendor-ffmpeg   # fully self-contained: bundles
#                                  # ffmpeg/ffprobe too (see vendor_ffmpeg.sh)
#
# Every build already includes everything needed for every feature to
# work, including the CLIP-based energy-scoring checkbox (torch/
# open_clip_torch/pillow) -- that's the whole point of "self-contained."
# That does make for a noticeably larger, slower build; if you'd rather
# trade the energy-scoring feature away for a smaller/faster one:
#     BRA_SKIP_ENERGY=1 ./build_app.sh --vendor-ffmpeg
#
# What this does, in order:
#   1. Fresh venv (build_env/), so the build never picks up whatever
#      happens to already be pip-installed globally on your machine.
#   2. pip install -r requirements.txt + py2app into it.
#   3. Optionally vendor ffmpeg/ffprobe (--vendor-ffmpeg).
#   4. python setup.py py2app -- the actual build.
#   5. Ad-hoc code-sign the .app. This is NOT the same as Apple
#      notarization (that needs a paid Developer ID -- see
#      BUILD_MACOS.md) and does NOT remove the Gatekeeper
#      "unidentified developer" prompt on first launch. What it DOES
#      do: Apple Silicon (M1/M2/M3/...) Macs refuse to run an
#      *entirely* unsigned binary at all as of macOS 11+, not just warn
#      about it -- ad-hoc signing (`codesign --sign -`) satisfies that
#      requirement using a local, free, identity-less signature. Without
#      this step, the built app may simply fail to launch at all on
#      Apple Silicon, with no useful error message.
#   6. Package into a .dmg with a shortcut to /Applications, so handing
#      this to someone else is "open the dmg, drag the app over."

set -euo pipefail

# Works around a known macOS/APFS race, not anything wrong with this
# script or your filesystem: if Spotlight (mds) is actively indexing a
# directory while `rm -rf` is deleting it -- which happens easily with
# a torch install, since torch/include/ alone is thousands of small
# C++ header files -- a single rm -rf pass can fail with "Directory
# not empty" even though every file it's complaining about really is
# gone by the time you look. It reliably succeeds within a couple of
# retries once the race window passes.
robust_rm_rf() {
    local dir="$1" attempt
    for attempt in 1 2 3 4 5; do
        rm -rf "$dir" 2>/dev/null || true
        [[ ! -e "$dir" ]] && return 0
        sleep 1
    done
    # Last attempt: let any real error (permissions, etc.) surface
    # normally instead of swallowing it silently.
    rm -rf "$dir"
}

if [[ "$(uname)" != "Darwin" ]]; then
    echo "build_app.sh must be run on macOS -- py2app cannot cross-build" >&2
    echo "from Linux/Windows. See BUILD_MACOS.md for why." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="B-Roll Analyzer"
VENDOR_FFMPEG=0
for arg in "$@"; do
    case "$arg" in
        --vendor-ffmpeg) VENDOR_FFMPEG=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

echo "== 1/6: Setting up a fresh build_env venv =="
robust_rm_rf build_env
python3 -m venv build_env
source build_env/bin/activate
python3 -c "import tkinter, sys; print('tkinter Tcl/Tk', tkinter.Tcl().eval('info patchlevel'))" \
    || { echo "This Python's tkinter looks broken -- see BUILD_MACOS.md" \
              "prerequisites (use python.org's installer, not system Python)." >&2; exit 1; }

echo
echo "== 2/6: Installing dependencies =="
pip install --upgrade pip >/dev/null
pip install -r requirements.txt
pip install py2app
# requirements.txt already includes torch/open_clip_torch/pillow
# unconditionally -- BRA_SKIP_ENERGY (if set) only affects setup.py's
# EXCLUDES list further down, i.e. whether py2app bundles them into the
# .app, not whether they get installed into this throwaway venv.

echo
if [[ "$VENDOR_FFMPEG" == "1" ]]; then
    echo "== 3/6: Vendoring ffmpeg/ffprobe =="
    ./vendor_ffmpeg.sh
else
    echo "== 3/6: Skipping ffmpeg vendoring (pass --vendor-ffmpeg to include it) =="
fi

echo
echo "== 4/6: Building the .app with py2app =="
robust_rm_rf build
robust_rm_rf dist
python setup.py py2app

APP_PATH="dist/$APP_NAME.app"
if [[ ! -d "$APP_PATH" ]]; then
    echo "Build did not produce '$APP_PATH' -- see output above." >&2
    exit 1
fi

echo
echo "== 5/6: Code-signing (inside-out: nested binaries first, app last) =="
# Strip any quarantine attribute this checkout might have picked up
# (e.g. if the source itself was downloaded/unzipped) before signing.
xattr -cr "$APP_PATH"

# Deliberately NOT relying on `codesign --deep` alone here. Apple's own
# codesign documentation describes --deep as a convenience for simple
# cases, not a guarantee -- and this bundle is not a simple case: it
# contains hundreds of loose .so files (every stdlib C-extension under
# Python 3.x's lib-dynload, plus OpenCV/NumPy, plus -- since energy
# scoring is bundled by default -- Pillow's own vendored image-codec
# dylibs like libtiff/libjpeg/libopenjp2). If --deep signs those out of
# order or misses one, the app can still *build* and even *open* on an
# Intel Mac, then get SIGKILL'd with "CODESIGNING ... Invalid Page" the
# moment it tries to dlopen the affected file on Apple Silicon, which
# validates every executable page far more strictly. Signing explicitly
# inside-out -- deepest nested binaries first, the .app itself last --
# is what Apple recommends instead, and directly targets that failure
# mode rather than hoping --deep happened to cover everything.
find "$APP_PATH" \( -name "*.so" -o -name "*.dylib" \) -type f -print0 |
    xargs -0 -I{} codesign --force --sign - --timestamp=none "{}"

# The embedded Python.framework's own binary is named "Python" with no
# file extension, so the find above (which only matches *.so/*.dylib)
# doesn't catch it -- needs signing explicitly.
PYTHON_FRAMEWORK_BIN="$(find "$APP_PATH/Contents/Frameworks" \
    -path "*/Python.framework/Versions/*/Python" -type f 2>/dev/null | head -1)"
if [[ -n "$PYTHON_FRAMEWORK_BIN" ]]; then
    codesign --force --sign - --timestamp=none "$PYTHON_FRAMEWORK_BIN"
fi

# Any other loose executables directly under Frameworks/ that the two
# passes above didn't already cover.
find "$APP_PATH/Contents/Frameworks" -maxdepth 1 -type f -perm -u+x -print0 2>/dev/null |
    xargs -0 -I{} codesign --force --sign - --timestamp=none "{}" 2>/dev/null || true

# Finally, the app bundle itself -- signed last, once everything it
# contains already carries a valid signature of its own.
codesign --force --sign - --timestamp=none "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH" \
    && echo "Signature verified (ad-hoc, inside-out)." \
    || echo "WARNING: codesign verification reported an issue -- see output above." >&2

echo
echo "== 6/6: Building a distributable .dmg =="
DMG_STAGE="$(mktemp -d)"
trap 'rm -rf "$DMG_STAGE"' EXIT
cp -R "$APP_PATH" "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"
DMG_PATH="dist/$APP_NAME.dmg"
rm -f "$DMG_PATH"
hdiutil create -volname "$APP_NAME" -srcfolder "$DMG_STAGE" -ov -format UDZO "$DMG_PATH" >/dev/null

echo
echo "Done."
echo "  App: $APP_PATH"
echo "  DMG: $DMG_PATH  <- hand this file to anyone; they open it and"
echo "                     drag the app into the Applications shortcut."
echo
echo "First launch on any machine (including this one) still needs one"
echo "of the two Gatekeeper steps in BUILD_MACOS.md section 3 -- ad-hoc"
echo "signing (above) satisfies Apple Silicon's launch requirement, but"
echo "it doesn't remove that one-time 'unidentified developer' prompt."
echo "That's expected without a paid Apple Developer ID for notarization."
