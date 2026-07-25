# Video / Audio Sync Tool

A local desktop app for editors who record camera audio and separate
32-bit float audio (boom mic, lavs, a field recorder) and need to preview,
line up, and export a synced, uncompressed master.

**Everything happens on your own machine.** There is no server component,
no account, no file upload, and no network access of any kind — the app
only calls local, open-source tools (`ffmpeg`/`ffprobe`, OpenCV, PortAudio).
It costs nothing to run beyond installing free software.

## What's new in this version

- **Fixed a freeze during playback.** Two compounding causes, found by
  actually measuring frame-by-frame cost rather than guessing:
  1. Every playback tick was recomputing a video frame index from
     wall-clock elapsed time and comparing it to the last frame read. Timer
     jitter meant this almost never lined up with "the very next frame," so
     nearly every tick triggered a full seek — and seeking a compressed
     video is far more expensive than just reading the next frame in
     sequence (measured ~117ms per tick vs. ~15ms). Fixed by seeking once
     when playback starts, then reading sequentially afterward, the way any
     normal video player does.
  2. Moving the waveform playhead was redrawing the *entire* waveform (all
     bars, every track) on every tick, measured at up to 33ms per redraw
     with several tracks visible — all to move one line. Fixed so the
     playhead is now a single lightweight canvas item that moves on its
     own; the bars are only redrawn when the actual data or offsets change.
  Combined, a realistic HD clip went from needing 360%+ of its per-frame
  time budget to about 58%, and a sustained multi-second playback test
  showed no growing lag (drift stayed under a millisecond over 4 seconds).
- **Fixed a freeze when adding files.** Adding an audio file (or loading a
  video) now returns instantly — the file's waveform and format probe both
  run in the background instead of blocking the window. This mattered most
  for realistic field-recording lengths: decoding a long continuous roll
  for preview could take several seconds, and adding several such files
  back-to-back multiplied that. Each row now shows "Analyzing…" and its
  Play button stays disabled until its own load finishes, without
  affecting anything else you're doing in the app. Export is blocked with
  a clear message if you try to run it before a track has finished loading.
- **Removed manual volume controls.** There are no more volume sliders
  anywhere in the app.
- **Removed automatic audio normalization.** No source is auto-gained
  anymore — every file (and the camera's on-board audio) plays and exports
  at its original recorded level, unmodified. There is no gain adjustment
  of any kind, manual or automatic; sources should be recorded and
  gain-staged the way you want them to sound.
- **Fixed:** the UI freezing while dragging a volume slider on the Sync tab
  (it was recomputing the full waveform display on every drag tick even
  though volume doesn't change what's drawn — moot now that the slider is
  gone, but the underlying peak-caching fix remains in place for other
  redraws).
- **Fixed:** the video preview collapsing to a tiny sliver once a clip was
  loaded (a Tkinter quirk where setting an image on a `Label` silently
  reinterprets its `width`/`height` from character units to pixel units —
  the preview now reserves its real pixel size from the start).
- **Expanded audio format support:** the app detects and displays each
  source's actual bit depth (16/24/32-bit integer, 32-bit float, 64-bit
  float) rather than assuming everything is already 32-bit float. **32-bit
  float remains the highest precision this tool processes and exports at**
  — anything higher (64-bit float) is clearly flagged and downsampled to
  32-bit float, and anything lower is decoded up to 32-bit float, so every
  export is consistently `pcm_f32le` regardless of what you feed in.

---

## 1. Install prerequisites (one-time)

1. **Python 3.9+** — [python.org](https://www.python.org/downloads/). On
   Windows/macOS, the official installer includes Tkinter (the GUI toolkit)
   automatically. On Linux, also run:
   ```
   sudo apt install python3-tk libportaudio2
   ```
   (`libportaudio2` is the local audio-playback library used for the
   preview/monitoring feature; it talks directly to your sound card.)
2. **ffmpeg** (includes `ffprobe`) — [ffmpeg.org/download.html](https://ffmpeg.org/download.html),
   or via a package manager:
   - macOS: `brew install ffmpeg`
   - Windows: `choco install ffmpeg` (or download a build and add it to PATH)
   - Linux: `sudo apt install ffmpeg`
3. **Python packages**:
   ```
   pip install -r requirements.txt --user
   ```
   (numpy + scipy for the sync math, opencv-python-headless + Pillow for
   video preview frames, sounddevice for audio monitoring)

Only install ffmpeg from its official site or your OS's package manager —
never from a random third-party download link — to avoid the usual
security risks that come with unofficial builds.

## 2. Run it

```
python3 sync_app.py
```

The app has three tabs, used in order:

### Tab 1 — Load & Preview

- **Camera video file** — the clip whose picture you're keeping. Loading it
  shows a video preview (Play/Pause and a waveform of its on-board audio)
  so you can review the raw clip before doing anything else. Its detected
  audio format (e.g. "24-bit integer • 48kHz • 2ch") is shown next to the
  waveform.
- **External audio files** — add one or more audio files. Supported input
  depths are 16/24/32-bit integer and 32/64-bit float; **32-bit float is
  this tool's own processing ceiling**, so a 64-bit float source will be
  clearly flagged and downsampled to 32-bit float, and everything else is
  decoded up to 32-bit float. Each file gets its own row with its own
  waveform, Play/Pause, and detected-format label, so you can listen to
  and inspect each file before committing to anything.

### Tab 2 — Sync & Adjust

- **Sync method** — Waveform (cross-correlation against the camera's
  on-board audio) or Timecode (embedded camera TC + BWF `TimeReference`).
  Click **Detect sync for all** to compute an offset for every track.
- **Synced comparison waveform** — shows the camera's reference waveform
  stacked above each external track, all drawn on the same timeline using
  each track's *current* offset. **Drag a track left/right directly on the
  waveform** to nudge it, or use the ±10ms/±100ms buttons next to each
  track for fine adjustment — either way, the offset shown updates live and
  is what gets used for export.
- **Play synced mix** — plays the camera video together with all audio
  tracks mixed at their current offsets (camera audio included only if
  "keep camera audio" is checked), so you can watch and listen to confirm
  everything lines up before exporting.
- **Keep camera's on-board audio as additional channel(s)** — when
  checked, the camera's original audio isn't discarded; it's kept as its
  own additional output audio track alongside the synced external tracks
  in the export.
- **Track** — each external file has an output track number (defaults to
  a distinct number per file, in add order). Give two or more files the
  **same** Track number to mix them together onto one output audio
  stream; keep them on different numbers to keep them as separate,
  independently-selectable audio tracks in the exported file. The
  camera's on-board audio (if kept) is always its own separate track.

### Tab 3 — Export

- **Video export format**:
  - `v210` — 10-bit uncompressed 4:2:2, the format most NLEs mean by
    "uncompressed"; QuickTime-compatible. **Recommended default.**
  - `ffv1` — mathematically lossless (bit-exact) but actually compressed,
    so files are much smaller.
  - `rawvideo` — genuinely raw, uncompressed frames. Correct but produces
    very large files.
  - `copy` — no video re-encoding at all: the original video stream is
    stream-copied through unchanged (fastest export, no generation loss).
    Only the audio is processed/synced. Keep the output in the same
    container as the source (the app defaults the output extension to
    match); copying into an incompatible container can fail.
- **Export synced file** — runs the export. The log lists each source's
  detected bit depth and the resulting output track layout before running;
  all processed audio is written as 32-bit float PCM (`pcm_f32le`)
  regardless of source depth, and the source video's metadata (and
  timecode track, if present) is copied to the output.
- **Cancel** — stops an export in progress. The partially-written output
  file is removed automatically since it's incomplete/unusable.

The log panel at the bottom shows the actual `ffmpeg` command and its
output, so you can see exactly what's happening at every step.

## 3. Notes & limitations

- **Preview playback** decodes up to the first 3 minutes of each file at
  44.1kHz for responsiveness; the actual **export** always uses the full,
  original-quality audio and the offsets you set on the Sync tab. There is
  no gain adjustment anywhere in the app — every source plays and exports
  at its original recorded level.
- **32-bit float is this tool's maximum audio precision.** Sources at or
  below that (16/24/32-bit int, 32-bit float) are decoded up to 32-bit
  float; a 64-bit float source is downsampled to 32-bit float. This is
  flagged in the UI and logged at export time so it's never silent.
- **No audio output device found?** The app will tell you rather than
  crash. Video-only preview still works in that case; it just plays back
  silently on a wall-clock timer instead of following an audio clock.
- **Waveform sync** analyzes up to the first 10 minutes of each file by
  default (adjustable in `sync_core.compute_waveform_offset`).
- **Timecode sync** requires the audio file to contain a BWF `bext` chunk
  with `TimeReference` set (most professional field recorders do this
  automatically) and the video to have an embedded start timecode. If
  either is missing, use Waveform sync instead.
- Uncompressed exports are large. A few minutes of 4K footage in `v210` or
  `rawvideo` can easily reach many gigabytes — make sure you have disk
  space, or choose `ffv1` (or `copy`, which doesn't touch the video at all).
- This tool syncs and channel-manages audio; it does not do noise
  reduction, EQ, or other audio restoration.

## 4. Files in this project

| File                | Purpose                                                        |
|---------------------|-----------------------------------------------------------------|
| `sync_app.py`       | Tkinter desktop GUI (entry point) — dark theme, 3-tab workflow  |
| `sync_core.py`      | Engine: probing, format detection, waveform/timecode offset detection, ffmpeg export command building |
| `media_playback.py` | Local audio playback (single + synced mix) via sounddevice, video frame access via OpenCV |
| `waveform_view.py`  | Waveform peak computation + the interactive Tkinter waveform widget (seek, drag-to-nudge) |
| `requirements.txt`  | Python package list                                             |

