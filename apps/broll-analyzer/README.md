# B-Roll Analyzer

A local desktop app that scans a folder of b-roll video clips, scores each
one on technical quality, and exports a Premiere Pro-compatible XML so you
can drop the best footage straight into a project.

## How it scores clips

For each clip, frames are sampled twice a second and scored on:

- **Sharpness** — variance of the Laplacian (focus/detail quality)
- **Exposure** — how close the brightness is to a healthy midtone, with a
  penalty for blown highlights or crushed blacks
- **Stability / motion** — optical flow is used to detect jittery, shaky
  motion vs. smooth pans or steady shots; shaky footage is penalized,
  smooth motion is not

These combine into a single 0–100 score per frame. The app then slides a
window (default 4 seconds, adjustable) across each clip to find its
**best contiguous segment(s)** — those are the parts used in the exported
sequence, even if the rest of the clip is mediocre.

By default it picks one segment per clip, but you can raise "Segments per
clip" to pull multiple distinct good moments out of the same source file
(e.g. a sharp opening shot and a separate sharp moment later, skipping a
shaky or out-of-focus stretch in between). Segments never overlap, are
kept at least 1 second apart, and a clip only gets extra segments if they
genuinely outscore the clip's own average — so a clip with only one good
moment won't be padded with filler just to hit the requested count.

## Optional: detecting high-energy / exciting shots (local, offline)

Check "Detect high-energy / exciting shots" to add a fourth score
dimension based on visual content rather than technical quality. This
uses [CLIP](https://github.com/mlfoundations/open_clip) — an open-source
computer vision model — run zero-shot: each sampled frame is compared
against a handful of "exciting, high energy, dynamic action" prompts vs.
"calm, static, low energy" prompts, and the relative similarity becomes
a 0–100 "energy" score.

**This never uses the Anthropic API, or any other cloud API.** The model
weights (an OpenAI-trained CLIP checkpoint, distributed as a standard
open-source release) are downloaded once, the first time you use the
feature, from their normal public host — after that, every frame is
scored entirely on your own machine (CPU or GPU), with no network calls
and no data leaving your computer.

To enable it, install the extra (optional) dependencies:

```bash
pip3 install torch open_clip_torch pillow
```

If they're not installed, the checkbox is simply disabled — everything
else works as before.

The "Energy weight" slider controls how much this factors into each
clip's overall score and segment selection: 0% ignores it entirely
(pure technical quality, the original behavior); 100% ranks and picks
segments purely by how "exciting" they look, ignoring sharpness/
exposure/stability. A middle value (the default, 35%) blends both. The
resulting per-clip energy score also shows up as its own sortable
"Energy" column, and gets included in the exported XML's clip comments,
regardless of the weight you choose.

Note: running CLIP on every sampled frame is meaningfully slower than
technical-only analysis, especially on CPU-only machines — expect
analysis to take noticeably longer with this enabled.

## Setup

```bash
pip3 install -r requirements.txt
python3 app.py
```

Requires Python 3.9+ and Tkinter (included with most Python installs;
on Linux you may need `sudo apt install python3-tk`). The header's
Blair seal logo is embedded directly in `app.py` (no separate assets
folder needed, so there's nothing extra to keep alongside the script).
If it doesn't appear, check the terminal: the app prints exactly why
-- in practice this only happens on a Tcl/Tk build older than 8.6 that
can decode neither of the two embedded image formats (PNG or GIF),
which is uncommon but does turn up on some older macOS system Pythons.
Either way, the header still renders correctly without the logo.

### Packaging as a standalone macOS app

To hand this to someone as a double-clickable app instead of a Python
script, see **BUILD_MACOS.md** for full instructions using `py2app`.
That step has to be run on a Mac (py2app can't cross-build from another
OS) and produces a self-contained `B-Roll Analyzer.app`.

For the exported sequence to carry each clip's real audio channel
preset (Mono/Stereo/5.1/etc.), sample rate, and bit depth, the
`ffprobe` command (part of a normal [ffmpeg](https://ffmpeg.org)
install) needs to be on your `PATH`. This is a read-only metadata
lookup on the file you already selected — no network access, no
decoding of the audio itself, and no changes to the source file. If
`ffprobe` isn't found (or a clip has no readable audio stream), that
clip's audio still exports fine; it just falls back to a conservative
Stereo/16-bit/48kHz assumption instead of the file's exact preset.

## Using the app

1. Click **Browse...** and select the folder containing your b-roll clips
   (subfolders are scanned too). Supported formats: mp4, mov, m4v, avi,
   mxf, mkv, mts, m2ts, ts, webm, wmv, flv, mpg, mpeg, 3gp.
2. Optionally adjust the best-segment length, how many segments to pull
   per clip, how many clips to analyze in parallel, whether to detect
   high-energy/exciting shots (and how much weight to give that vs.
   technical quality), which clips to include (all scored clips, a
   top-N, or only clips above a score threshold), and how clips are
   ordered in the exported sequence (best score first, or alphabetically
   by clip name).
3. Click **Analyze**. Clips are analyzed in parallel across multiple
   CPU cores (see "Parallel workers" below), with live status showing
   how many have finished; this can still take a while for long or
   high-resolution footage since every clip is decoded. Click **Cancel**
   at any point to stop early -- see "Cancelling" below.
4. Review the ranked table (clip name, score, duration, recommended
   in/out segment). Each row shows a small thumbnail from its best
   segment; select a row to see a larger preview on the right. If
   window length, segments-per-clip, or energy weight changed since
   that thumbnail was captured, selecting the row refreshes it in the
   background (a single quick seek into that one file) rather than
   re-seeking every clip in the folder up front -- so tweaking a
   setting across a large library stays instant, and only the clips
   you actually look at pay the (small) cost of an updated preview.
   That refresh is written straight back into the folder's cache file,
   so it isn't lost the next time you reopen the app or re-run Analyze.
   If any clips failed to analyze, or energy scoring couldn't run for
   some of them, a red **View Issues (N)** button appears next to
   Export -- click it for a plain-language reason per clip, without
   needing to have a terminal open to see it.
   Rows in the table are also tinted to flag two conditions at a
   glance: red for a clip scoring below the "Score above" value
   (updates live as you adjust that field, regardless of which
   "Include top" mode is selected), and yellow for a clip where energy
   scoring was requested but didn't succeed for that particular file.
5. Click **Export Premiere XML...** and choose where to save the `.xml`
   file.

### Settings persistence

The folder path and every option control (segment length,
segments-per-clip, include/threshold mode, worker count, energy
detection + weight, sequence order) are saved to
`~/.broll_analyzer_settings.json` when you close the app, and restored
the next time you open it. This is a small local JSON file -- the same
kind of local, human-readable bookkeeping as the per-folder result
cache described below -- and is never uploaded or shared. Delete it any
time to reset to defaults; a missing or corrupted file just means the
app starts with its normal defaults instead of failing to launch.

### Parallel workers

Analyzing one clip doesn't depend on any other, so clips are decoded
and scored across multiple processes at once instead of one at a time.
The "Parallel workers" field defaults to one less than your CPU's core
count (leaving a core free for the UI) and can be lowered if you want
to keep the machine responsive for other work.

If "Detect high-energy / exciting shots" is also enabled, keep in mind
each worker process loads its own copy of the CLIP model, so more
workers there means more RAM (or VRAM, if using a GPU) used, not just
more CPU — lower the worker count if memory becomes tight.

### Cancelling

Clicking **Cancel** stops any clip that hasn't started yet immediately.
A clip already mid-decode in a worker process is left to finish -- video
decoding can't be safely interrupted partway through -- so there can be
a short delay after clicking Cancel before it fully stops, especially
with a high worker count. Whatever finished before cancelling is still
shown in the table, is exportable, and is written to the cache (see
below), so nothing already done is wasted.

### Result caching

Each analyzed folder gets a cache file, `.broll_analyzer_cache.json`,
written directly inside that folder (a hidden dotfile on macOS/Linux).
It stores each clip's per-frame technical samples -- the expensive part
of analysis, since it requires decoding the video -- keyed by that
file's path, size, and modification time.

On the next **Analyze** run in the same folder:
- Unchanged files are loaded from the cache and re-scored instantly for
  whatever the current best-segment length / segments-per-clip / energy
  weight settings are, without touching the source video again. Changing
  those settings alone never requires re-decoding anything.
- Files that are new, modified, or missing from the cache are analyzed
  normally.
- Turning on "Detect high-energy / exciting shots" for the first time on
  a folder that was cached without it will still require a full
  re-analysis of every clip (energy scoring needs the actual pixels,
  which aren't cached), but that folder is then cached *with* energy
  scores, so future runs -- with or without energy scoring enabled --
  can reuse it.
- Clips that failed to analyze aren't cached, so they're retried on the
  next run rather than being remembered as permanently broken.

The cache is a plain JSON file that's only ever read from and written
to the folder you selected -- nothing is uploaded or shared. Deleting
it is always safe; the app just re-analyzes everything from scratch on
the next run. For very large libraries it can grow to a few hundred KB
to a few MB depending on clip count and length, which is worth knowing
if that folder is version-controlled or synced elsewhere.

## Bringing it into Premiere Pro

In Premiere Pro: **File > Import...** and select the exported XML. You'll get:

- A **bin** named "B-Roll Analysis - Ranked Clips" containing every
  selected clip as a master clip, with its quality score in the clip's
  comments (visible in the Project panel's Comments column). Comments
  also include the clip's original audio channel preset (Mono, Stereo,
  5.1, etc.) plus its sample rate and bit depth, so that information is
  easy to check at a glance.
- A **sequence** named "Best B-Roll Selects" with each clip's segment(s)
  placed back-to-back, in the order chosen under "Sequence clips by"
  (score, best first, or clip name A-Z) — a ready-made highlight reel you
  can use as-is or break apart and rearrange. Each segment's original
  audio is placed on the sequence's audio track(s) in sync with the
  video, split one discrete channel per track (a stereo clip lands on 2
  linked L/R items, a 5.1 clip on 6, a mono clip on 1) rather than as a
  single combined item — a stereo clip carried as one item with
  `channelcount=2` silently imports as mono in Premiere, so every
  channel always gets its own linked item and track instead. The
  sequence's audio format declares its output channel width sized to
  whichever selected clip needs the most channels (2 if the widest is
  stereo, 6 if any clip is 5.1, etc.) rather than leaving it unstated.
  Either way, sound is preserved in its native format, not stripped or
  altered. If a clip contributed
  more than one segment, they're labeled "(seg 1)", "(seg 2)", etc. and
  appear consecutively in their original source order. The bin follows
  the same ordering.

## Notes / limitations

- This scores technical quality, not subject matter — it won't know if
  a perfectly sharp, stable shot is actually interesting or relevant.
  Treat the ranking as a fast first pass to surface clean footage and
  filter out blurry/shaky/over- or under-exposed takes.
- Analysis time scales with clip length and resolution since frames are
  decoded directly (not just metadata), though analyzing multiple clips
  in parallel (see "Parallel workers" above) helps considerably on
  multi-core machines. For very large libraries, consider pointing it
  at a proxy/preview folder first.
- Video is decoded via OpenCV's FFmpeg backend specifically (with a
  fallback to OpenCV's own auto-selected backend if a given install
  lacks FFmpeg support), since FFmpeg has by far the broadest codec
  and container support of the backends OpenCV can use -- this matters
  most for camera-native and broadcast formats (MXF, MTS/M2TS) where
  other backends (e.g. Windows Media Foundation) often can't decode the
  codec at all. A clip's actual duration is also verified against how
  many frames really decode, rather than trusted from container
  metadata alone -- some of these same formats report an unreliable or
  zero frame count, which previously could cause a perfectly playable
  clip to be rejected before analysis even started.
- The XML uses absolute file paths, so keep the source clips in place
  (or update paths) before importing into Premiere on another machine.
- The `.broll_analyzer_cache.json` file the app writes into each
  analyzed folder (see "Result caching" above) is app-specific bookkeeping,
  not part of your footage -- safe to delete, .gitignore, or exclude
  from backups of the source media itself.
