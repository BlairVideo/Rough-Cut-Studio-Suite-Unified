# Building the macOS App (py2app)

This packages B-Roll Analyzer into a double-clickable `B-Roll Analyzer.app`
that doesn't require anyone to install Python, pip packages, ffmpeg, or run
anything from a terminal to use the app day-to-day -- and a `.dmg` you can
hand to someone else so opening it really is the only step on their end.

**This must be done on a Mac.** py2app depends on macOS's own frameworks
(Cocoa, PyObjC) to build an app bundle and cannot cross-build from
Windows/Linux. (This was actually verified while preparing this package:
running `python setup.py py2app` on Linux gets through dependency
analysis of the app's own code fine, then fails specifically trying to
locate the Tcl/Tk *framework* paths -- a macOS-only concept. That's the
expected boundary, not a bug in `setup.py`.)

## The fast path

```bash
cd path/to/broll-analyzer
./build_app.sh --vendor-ffmpeg
```

That's it -- one command produces `dist/B-Roll Analyzer.app` **and**
`dist/B-Roll Analyzer.dmg`, fully self-contained (Python, Tkinter,
OpenCV, NumPy, and ffmpeg/ffprobe all bundled inside), ad-hoc signed so
it launches on Apple Silicon, ready to hand to anyone. Everything below
explains what that script does and how to do it by hand / troubleshoot
it -- skip to **section 3** if you just want to know what "first launch
on someone else's Mac" looks like.

Drop `--vendor-ffmpeg` if you'd rather not bundle ffmpeg (smaller,
faster build; the app then needs Homebrew's ffmpeg on whatever machine
runs it, exactly like every earlier build of this app -- see section 4).

## 1. Prerequisites (one-time, on the Mac doing the build)

- **Xcode Command Line Tools**: `xcode-select --install`
- **Python 3.11 or 3.12** from **python.org** -- specifically one of
  these two, not the Mac's built-in system Python and not necessarily
  whatever the very latest release is. Two separate reasons:
    - System Python often links an old Tcl/Tk 8.5, which can't display
      PNG images (the header logo has a GIF fallback for exactly this,
      so it won't crash -- but it's still worth avoiding). python.org's
      installer bundles a modern Tcl/Tk 8.6.
    - py2app can lag behind brand-new CPython releases in ways that
      don't show up as a build failure -- the build succeeds, then the
      app SIGKILLs on launch with a `CODESIGNING`/`Invalid Page` crash
      on Apple Silicon (see Troubleshooting). Python 3.14, for example,
      restructured how several stdlib modules are packaged as separate
      `.so` files, which is exactly the kind of change that trips up
      packaging tools before they've caught up. 3.11/3.12 are mature
      enough that py2app support for them is well-trodden.
- **ffmpeg**, if you want it bundled (`--vendor-ffmpeg` above) or are
  building without bundling it: `brew install ffmpeg`.
- **dylibbundler**, only needed for `--vendor-ffmpeg`:
  `brew install dylibbundler`. This is what makes the vendored
  ffmpeg/ffprobe relocatable -- see `vendor_ffmpeg.sh`'s header
  comment for why a plain `cp` of the binaries wouldn't actually be
  self-contained.

## 2. Build steps (manual, if you're not using build_app.sh)

```bash
cd path/to/broll-analyzer          # the folder with app.py, setup.py, AppIcon.icns, etc.
python3 -m venv build_env
source build_env/bin/activate
pip install -r requirements.txt
pip install py2app
./vendor_ffmpeg.sh                 # optional -- skip for a smaller build without bundled ffmpeg
python setup.py py2app
```

The finished app is at `dist/B-Roll Analyzer.app`. Drag it wherever
you'd like (Applications folder, a shared drive, etc.) -- it's
self-contained from that point on and the `build_env` venv can be
deleted. If you vendored ffmpeg, ad-hoc sign it before distributing
(see section 3 -- `build_app.sh` does this automatically):

```bash
xattr -cr "dist/B-Roll Analyzer.app"
codesign --force --deep --sign - "dist/B-Roll Analyzer.app"
```

For a much faster dev-loop while testing changes (seconds instead of
minutes), use alias mode instead of the last command above:

```bash
python setup.py py2app -A
```

This makes `dist/B-Roll Analyzer.app` a thin wrapper that runs directly
from your source files rather than copying everything -- great for
iterating, but don't distribute an alias-mode build; run the plain
`python setup.py py2app` for anything you're actually going to hand to
someone else.

## 3. First launch / distributing to other machines

This build isn't notarized (that requires an Apple Developer ID, which
is a separate, ongoing paid enrollment -- worth doing if this gets
rolled out school-wide, but out of scope here). Two separate things
happen on a machine that isn't the one that built it, and it's worth
knowing which is which:

- **Apple Silicon (M1/M2/M3/...) will refuse to launch the app at all**
  if it's completely unsigned -- not a warning, it just won't run.
  `build_app.sh` handles this automatically with an ad-hoc signature
  (`codesign --sign -`); if you built by hand, run the two commands
  in section 2 above once before distributing.
- **Gatekeeper's "unidentified developer" prompt** shows regardless of
  ad-hoc signing, the first time the app is opened on any given
  machine, because it isn't notarized by Apple. This is expected and
  only needs handling once per machine:
    - Right-click (or Control-click) the app -> **Open** -> **Open**
      again in the confirmation dialog, **or**
    - From Terminal: `xattr -cr "B-Roll Analyzer.app"`

Distributing via the `.dmg` `build_app.sh` produces (rather than, say,
a zip) doesn't change either of these -- there's no way around them
short of Apple notarization -- but it does make the handoff itself
clean: open the dmg, drag the app to the Applications shortcut inside
it, eject.

## 4. What's included vs. what isn't

- **Always included in the bundle**: the app itself, Python, Tkinter,
  OpenCV, NumPy, and the header logo (embedded directly in `app.py` as
  image data, so there's no separate assets folder to lose track of).
- **Included if you ran `./vendor_ffmpeg.sh` (or `build_app.sh
  --vendor-ffmpeg`)**: `ffmpeg`/`ffprobe`, made relocatable via
  `dylibbundler` and placed in `Contents/Resources/bin` +
  `Contents/Resources/lib`. `app.py`'s `_ensure_homebrew_on_path`
  checks there first, ahead of Homebrew, so a bundled build behaves
  identically regardless of what's (or isn't) installed on the
  machine running it.
- **Not bundled otherwise, needs to be present on the machine running
  it**: `ffmpeg`/`ffprobe` (optional either way -- graceful
  stereo/16-bit/48kHz fallback if genuinely unavailable, surfaced per
  clip rather than failing silently).
- **Bundled by default**: `torch`, `open_clip_torch`, and `pillow` --
  the local CLIP-based "Detect high-energy / exciting shots" feature.
  This makes the build noticeably larger and slower to produce (`torch`
  alone is several hundred MB), but it's the difference between a
  built app where every feature actually works and one where the
  Energy checkbox fails with "pip install torch open_clip_torch
  pillow" the moment someone tries it -- confusing on a machine that
  was never meant to need a terminal at all.

  To trade that feature away for a smaller/faster build instead:
  `BRA_SKIP_ENERGY=1 ./build_app.sh --vendor-ffmpeg` (or, building by
  hand: `BRA_SKIP_ENERGY=1 python setup.py py2app`). The app still
  degrades gracefully without them -- the checkbox just shows as
  unavailable, identical to running the unpackaged script without
  them installed.

  One thing bundling `torch`/`open_clip_torch` does *not* include even
  so: the actual CLIP model *weights* (as opposed to the library code)
  still download once from their normal open-source host the first
  time someone actually uses the energy-scoring checkbox, per
  `requirements.txt`'s existing note -- there's no fully-offline way to
  ship model weights inside a lightweight app bundle, and that's the
  same behavior the unpackaged script has always had.

## 5. Troubleshooting

**Built app's Energy checkbox shows an error mentioning `pip install
torch open_clip_torch pillow`.** This means the app was built with
these excluded (`BRA_SKIP_ENERGY=1`, or a `setup.py` from before it
bundled them by default) -- and the message itself is telling you
correctly that torch/open_clip_torch aren't inside *that* bundle,
regardless of what's installed in any venv on the build machine.
Rebuild without `BRA_SKIP_ENERGY` set (plain `./build_app.sh
--vendor-ffmpeg` already bundles them) and hand out the new `.app`/`.dmg`
-- there's no way to add this after the fact to an already-built app;
it has to be present at `python setup.py py2app` time.

**Built app crashes immediately on launch; Console/the crash reporter
shows `Termination Reason: Namespace CODESIGNING, Code 2, Invalid
Page` and `Exception Type: EXC_BAD_ACCESS ... SIGKILL (Code Signature
Invalid)`.** This means some individual file *inside* the bundle
(one of the many `.so`/`.dylib` files -- stdlib C-extensions, OpenCV,
NumPy, or Pillow's own vendored image-codec dylibs) failed macOS's
code-signature check the moment the app tried to load it -- not a bug
in this app's Python code. Two things to try, in order:

1. **Rebuild with Python 3.11 or 3.12** instead of whatever's newest,
   if you built with something else. This is the single most common
   fix for exactly this crash signature -- see the reasoning in
   section 1's prerequisites.
2. **Rebuild with the current `build_app.sh`** if you're on an older
   copy of it. It now signs every nested `.so`/`.dylib` individually
   (inside-out) before signing the app itself, rather than relying on
   `codesign --deep` alone -- Apple's own guidance is that `--deep`
   isn't guaranteed to correctly cover every nested binary in a bundle
   with this many loose native library files, and an incompletely- or
   incorrectly-signed nested file is exactly what produces this crash.

If it still crashes after both, the crash report's "Binary Images"
section usually won't show which specific file it was trying to load
(dyld crashes mid-parse, before it finishes registering the image) --
but it will show everything that loaded *successfully* just before.
Compare that list against what changed in your last build (new
dependency? newly vendored ffmpeg?) for the most likely culprit, and
try excluding it (or building without `--vendor-ffmpeg` /
`BRA_SKIP_ENERGY=1`, depending which) to confirm.

**Built app shows a generic "Launch error / See the py2app website for
debugging launch issues" dialog (with "Visit Website"/"Terminate"
buttons), instead of a real error message.** This is py2app's own
catch-all crash handler -- it deliberately hides the actual Python
traceback behind this dialog since there's no Terminal window to print
it to when the app is double-clicked from Finder. Always get the real
error first:
```bash
"/Applications/B-Roll Analyzer.app/Contents/MacOS/B-Roll Analyzer"
```
This runs the exact same binary Finder does, but prints the full
traceback to Terminal instead of swallowing it. If that traceback ends
in something like:
```
RuntimeError: operator torchvision::nms does not exist
```
this is already fixed as of the current `setup.py` -- `torchvision` (a
transitive dependency of `open_clip_torch`, for its CoCa-model support)
has its own compiled extension that registers native ops into torch's
runtime at import time, and needs the same wholesale-copy `packages`
treatment torch itself does (see the comment above `ENERGY_PACKAGES`
in `setup.py`) rather than py2app's default static-analysis freezing.
Rebuild with the current `setup.py` and this specific error should be
gone. Any *other* traceback ending here means a different package has
the same category of problem (a compiled extension / native op
registration py2app's default freezing didn't handle) -- the fix is
the same shape: add that package's top-level name to `ENERGY_PACKAGES`
(or `PACKAGES` directly, if it's unrelated to energy scoring) in
`setup.py` and rebuild.

**Build fails with `ImportError: No module named 'open_clip_torch'`
during py2app's `collect_packagedirs()` step (before producing any
`.app` at all).** A PyPI naming mismatch, not a missing dependency:
`open_clip_torch` is the name you `pip install`, but the actual
importable package is named `open_clip` (its own code lives at
`open_clip/__init__.py`, not `open_clip_torch/__init__.py` -- pip
distribution names and import names don't have to match, and this is
one of the more common cases where they don't). py2app's `packages`
option needs a real import name to find a package's bootstrap file, so
listing the pip name there fails immediately. Already fixed as of the
current `setup.py` (`ENERGY_PACKAGES` uses `"open_clip"`) -- if you
still hit this, you're on an older copy; grab the current `setup.py`
and rebuild. The general lesson if a *different* dependency ever hits
this same error: check that package's own top-level folder name inside
`build_env/lib/python3.*/site-packages/` rather than assuming it
matches whatever you typed after `pip install`.

**Build fails with `RecursionError: maximum recursion depth exceeded`,
often after a line like `Skip compiling '...' due to recursion error`.**
This is a known py2app bug, not anything specific to this app --
`setup.py` already works around it (see the comment above
`sys.setrecursionlimit(10000)` near the top of the file). If you're
using an older copy of `setup.py` without that line, or it still
happens with it: confirm `python setup.py py2app -A` (alias mode)
completes fine -- if it does, this confirms the diagnosis, since alias
mode skips the scanning step that triggers it. Try raising the limit
further (e.g. `20000`) if 10000 isn't enough; if instead you get a hard
crash/segfault rather than this same clean Python exception, lower it
back down and let me know, since that would mean something else is
going on.

**Build fails during the "tkinter" recipe step, or the built app opens
and immediately closes / shows a blank window.** Almost always a Tcl/Tk
version issue. Confirm you're building with python.org's Python, not
Homebrew's or the system one:
```bash
python3 -c "import tkinter; print(tkinter.Tcl().eval('info patchlevel'))"
```
Should print `8.6.x`. If it prints `8.5.x`, reinstall Python from
python.org and rebuild.

**Built app crashes on launch with a `dyld`/library-not-found error
mentioning `cv2` or `opencv`.** Try rebuilding in a clean venv using
`opencv-python-headless` instead of `opencv-python` in
`requirements.txt` before running pip install -- it has fewer
GUI-related dynamic libraries that occasionally trip up py2app's
dependency bundling, and this app doesn't use OpenCV's own GUI
functions (`cv2.imshow` etc.) at all, so nothing is lost.

**Built app refuses to open on Apple Silicon, no dialog at all (not
even the Gatekeeper prompt).** Almost certainly a missing ad-hoc
signature -- see section 3. `codesign --force --deep --sign - "dist/B-Roll
Analyzer.app"`, then try again.

**`vendor_ffmpeg.sh` fails with "dylibbundler not found."**
`brew install dylibbundler`, then re-run it.

**`vendor_ffmpeg.sh` succeeds, but the built app's exports still show
"ffprobe not found" per-clip.** Confirm the vendored copy actually
runs standalone first: `dist/B-Roll Analyzer.app/Contents/Resources/bin/ffprobe
-version`. If that fails with a dyld/library error, the dylibbundler
step didn't fully resolve every dependency -- re-run `./vendor_ffmpeg.sh`
(it's safe to re-run) and check its output for errors from
`dylibbundler` itself. If the standalone check above succeeds but the
*app* still reports ffprobe missing, confirm you rebuilt with `python
setup.py py2app` *after* running `vendor_ffmpeg.sh` (setup.py only
picks up `vendor/ffmpeg` contents at build time, not automatically).

**App doesn't visibly do anything when double-clicked, no error
dialog.** Run the actual binary from Terminal to see console output
instead of the .app wrapper:
```bash
"dist/B-Roll Analyzer.app/Contents/MacOS/B-Roll Analyzer"
```

**Logo doesn't appear in the header, or a message about it prints in
Terminal when run the way shown just above.** The app already reports
the specific reason (see `_load_seal_photo` in `app.py`) rather than
failing silently -- read that message first. In a properly-built app
bundle with python.org's Tcl/Tk 8.6, this shouldn't happen at all.

**`hdiutil create` fails while building the `.dmg`.** Usually means a
previous `dist/B-Roll Analyzer.dmg` is still mounted -- eject it
(`hdiutil detach /Volumes/B-Roll\ Analyzer`) and re-run.

## 6. Icon source

`AppIcon.icns` is built from Blair's official "Crew B" mark
(`BlairB_PMS_289.png`, a clean high-resolution asset with real alpha
transparency -- no PDF extraction needed for this one), placed on a
Blair Blue rounded-square background. The mark's built-in white
keyline is what makes it read clearly against that background; a
white or transparent background were both tried and looked
noticeably worse (the keyline nearly disappears against white, and
the letterform's internal counters show inconsistent see-through on
transparent, since those interior shapes are cut out of the source
art too, not filled with white).
