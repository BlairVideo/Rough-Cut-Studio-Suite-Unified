"""
setup.py -- py2app build script for B-Roll Analyzer.

Builds a self-contained macOS .app bundle. This MUST be run on macOS
(py2app depends on PyObjC/Cocoa and cannot cross-build from another
OS). See BUILD_MACOS.md for full step-by-step instructions.

Quick reference (or just run ./build_app.sh, which does all of this
plus code-signing and a distributable .dmg in one step):
    python3 -m venv build_env
    source build_env/bin/activate
    pip install -r requirements.txt py2app
    ./vendor_ffmpeg.sh          # optional -- see that script's header
    python setup.py py2app
    # -> dist/B-Roll Analyzer.app

For a fast dev-loop build that symlinks back to this source folder
instead of copying everything (rebuilds in seconds, but the source
files must stay in place for it to run):
    python setup.py py2app -A

Environment variables this script reads:
    BRA_SKIP_ENERGY=1     Exclude torch/open_clip_torch/pillow (the
                          "Detect high-energy / exciting shots"
                          feature) for a smaller/faster build, at the
                          cost of that feature not working in the
                          built app. Bundled by default -- see the
                          comment above EXCLUDES below.
"""

import os
import sys
from setuptools import setup

# Works around a known, long-standing py2app bug (see
# github.com/ronaldoussoren/py2app issues #316 and #498): during the
# full (non-alias) build, py2app's dependency scanner walks the AST of
# every bundled module to check for dynamic imports, using Python's
# recursive ast.NodeVisitor. Large modules -- numpy is a common
# trigger, given its size -- can exceed Python's default recursion
# limit (1000) partway through that walk, raising "RecursionError:
# maximum recursion depth exceeded" from inside py2app itself, not
# from any bug in this app's own code. `python setup.py py2app -A`
# (alias mode) is unaffected since it skips this scanning step
# entirely -- if that runs fine but a full build doesn't, this is
# almost certainly the cause. Raising the limit here, before py2app's
# scan begins, is the community-documented workaround; 10000 is
# comfortably past what's been reported necessary without meaningfully
# risking an actual C-stack overflow (which would show as a hard
# crash/segfault rather than this clean Python exception -- if that
# happens instead, lower this back down and report it, since it'd mean
# something unrelated to this known issue is going on).
sys.setrecursionlimit(10000)

APP = ["app.py"]

DATA_FILES = []
# No assets/ folder is needed here: the header logo is embedded as
# base64 image data directly inside app.py (see _SEAL_PNG_B64 /
# _SEAL_GIF_B64), specifically so there's nothing extra that could be
# left behind or mismatched when packaging.

# ---------------------------------------------------------------------
# Vendored ffmpeg/ffprobe: makes the built app work identically on any
# Mac, regardless of whether that machine has Homebrew or ffmpeg
# installed at all -- the whole point of "self-contained." This is
# entirely optional and additive: if ./vendor/ffmpeg hasn't been
# populated (see vendor_ffmpeg.sh), the build proceeds exactly as
# before and the app falls back to Homebrew/PATH at runtime, same as
# every earlier build of this app.
#
# vendor_ffmpeg.sh copies the binaries AND rewrites their dylib
# dependencies to be relocatable (via dylibbundler), laid out as:
#   vendor/ffmpeg/bin/ffmpeg
#   vendor/ffmpeg/bin/ffprobe
#   vendor/ffmpeg/lib/*.dylib          (whatever they depend on, if not
#                                        already statically linked)
# py2app's DATA_FILES copies a given source directory's *contents* into
# a same-named subfolder of Contents/Resources, which is exactly the
# `Resources/bin`, `Resources/lib` layout _ensure_homebrew_on_path in
# app.py already knows to look for via the RESOURCEPATH env var py2app
# sets at runtime.
VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "ffmpeg")


def _vendor_data_files():
    files = []
    for subdir in ("bin", "lib"):
        src_dir = os.path.join(VENDOR_DIR, subdir)
        if not os.path.isdir(src_dir):
            continue
        entries = [os.path.join(src_dir, name) for name in os.listdir(src_dir)
                   if os.path.isfile(os.path.join(src_dir, name))]
        if entries:
            files.append((subdir, entries))
    return files


vendored = _vendor_data_files()
DATA_FILES.extend(vendored)
if vendored:
    print(f"setup.py: bundling vendored ffmpeg from {VENDOR_DIR} "
          f"({sum(len(f) for _, f in vendored)} file(s)) -- "
          f"see vendor_ffmpeg.sh")
else:
    print(f"setup.py: no vendored ffmpeg found at {VENDOR_DIR} -- "
          f"building without it (app falls back to Homebrew/PATH at "
          f"runtime, same as before; run ./vendor_ffmpeg.sh first for "
          f"a fully self-contained build)")

# torch / open_clip_torch / pillow power the "Detect high-energy /
# exciting shots" feature. ALWAYS bundled now, so a built .app has every
# feature working out of the box -- previously these were excluded by
# default (opt-in via BRA_BUNDLE_ENERGY=1), which is exactly why a
# default build's Energy checkbox failed with "pip install torch
# open_clip_torch pillow": that pip install already happens into
# build_env (requirements.txt lists them unconditionally), but py2app
# was stripping them back out of the finished bundle regardless.
#
# Trade-off, so it's not a surprise: torch alone is several hundred MB,
# so this build is noticeably larger and slower to produce than one
# without it. If that's ever worth trading back for a smaller/faster
# build at the cost of the energy-scoring feature, set BRA_SKIP_ENERGY=1.
SKIP_ENERGY = os.environ.get("BRA_SKIP_ENERGY") == "1"
# NOTE: "open_clip" here, not "open_clip_torch" -- open_clip_torch is
# the PyPI *distribution* name (what you `pip install`), but the
# importable Python package is actually named `open_clip` (its own
# __init__.py lives at open_clip/__init__.py, not
# open_clip_torch/__init__.py). py2app's `packages` option needs a real
# import name to locate the package's bootstrap file -- passing the pip
# name instead fails with `ImportError: No module named 'open_clip_torch'`
# during py2app's collect_packagedirs() step, before it produces any
# output at all.
ENERGY_PACKAGES = ["torch", "torchvision", "open_clip", "PIL"]
EXCLUDES = ENERGY_PACKAGES + ["matplotlib"] if SKIP_ENERGY else ["matplotlib"]
if SKIP_ENERGY:
    print("setup.py: BRA_SKIP_ENERGY=1 -- excluding torch/open_clip_torch/"
          "pillow (smaller/faster build, but the Energy checkbox will show "
          "as unavailable in the built app, same as running the unpackaged "
          "script without them installed)")
else:
    print("setup.py: bundling torch/open_clip_torch/pillow so energy "
          "scoring works in the built app (expect a much larger, slower "
          "build than one without them -- set BRA_SKIP_ENERGY=1 to skip)")

# `packages` tells py2app to copy a package's directory wholesale and
# leave its internals alone, rather than statically analyzing/freezing
# it like ordinary project code. cv2/numpy already needed this --
# torch needs it even more: it dynamically loads a large web of its
# own compiled backends in ways static analysis can't trace, and
# letting py2app's default freezing process attempt that is a common
# source of a build that succeeds but then fails at launch with no
# useful error (py2app's generic "Launch error" dialog specifically --
# it swallows the real traceback; run the built binary directly from
# Terminal to see what actually failed under it, if this still happens).
#
# torchvision is here for the same reason despite this app never
# importing it directly: open_clip_torch pulls it in transitively (for
# CoCa-model support), and it has its own compiled extension that
# registers native ops like `torchvision::nms` into torch's runtime at
# import time -- left out of `packages`, that registration silently
# fails under py2app's default freezing with `RuntimeError: operator
# torchvision::nms does not exist`, even though the pure-Python half of
# torchvision imports just fine. Same root cause as the torch one
# above, just one level removed.
PACKAGES = ["cv2", "numpy"] if SKIP_ENERGY else ["cv2", "numpy"] + ENERGY_PACKAGES

OPTIONS = {
    "py2app": {
        "iconfile": "AppIcon.icns",
        "packages": PACKAGES,
        "excludes": EXCLUDES,
        "plist": {
            "CFBundleName": "B-Roll Analyzer",
            "CFBundleDisplayName": "B-Roll Analyzer",
            "CFBundleIdentifier": "edu.blairacademy.brollanalyzer",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHumanReadableCopyright": "Blair Academy",
            # Prevents a bundled command-line matplotlib/other backend
            # from trying to activate as a background/menu-bar-only
            # process; this is a normal windowed Tk app.
            "LSUIElement": False,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.13",
        },
    }
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options=OPTIONS,
    setup_requires=["py2app"],
)
