# A-Sync — Video / Audio Sync Tool

A local Tkinter desktop app for editors who record camera audio and
separate 32-bit float audio (boom mic, lavs, a field recorder): sync one
or more external audio files to a camera video, preview the result, and
export an uncompressed synced master. Everything runs locally via
`ffmpeg`/`ffprobe`, OpenCV, and `sounddevice`/PortAudio — no uploads, no
accounts, no network access of any kind.

This app is also embedded as Studio Suite's **Sync workspace** (see
`../CLAUDE.md` for the suite-wide "don't modify a sibling app's files
without asking" rule) — Studio Suite calls this app's own worker
(`Studio Suite/backend/workers/sync_worker.py`) via *this app's own venv
interpreter*, so a change here affects both the standalone app and the
suite identically. That's usually a reason FOR fixing something here
rather than suite-side, not a reason for extra caution.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # numpy, scipy, opencv-python-headless, Pillow, sounddevice
brew install ffmpeg               # ffmpeg/ffprobe on PATH
python3 sync_app.py
```

## Architecture

```
sync_app.py         Tkinter UI + app state. Entry point (run this).
sync_core.py        Pure functions: waveform cross-correlation, BWF/timecode
                     parsing, offset computation, export command building.
                     No UI, no Tkinter import -- safe to import headlessly
                     (this is exactly what Studio Suite's sync_worker.py does).
media_playback.py   Local-only playback engine for preview panels:
                     AudioPlayer (one buffer), MixPlayer (several offset
                     buffers played together, for "synced preview"),
                     VideoFrameSource (pull frames by timestamp).
waveform_view.py    Pure-numpy peak/RMS downsampling (compute_peaks -- unit
                     testable, no Tkinter) + a Canvas widget that renders
                     the waveform/playhead/draggable-offset track.
ffprobe_util.py     Shared probe helper -- byte-identical copy in B-Roll
                     Analyzer/Local Interview Transcriber/Studio Suite (not
                     cross-imported; each app must stay independently
                     runnable in its own venv -- see PATH gotcha below).
```

- **Offset convention**: `sync_core.waveform_offset`/`compute_offset`
  return how many seconds the **external audio** must be delayed to line
  up with the video; negative means the audio starts before the video.
  Studio Suite's Sync workspace uses this exact same convention — see its
  own `CLAUDE.md`.
- **Waveform sync** (`waveform_offset`): FFT-based cross-correlation
  (`scipy.signal.correlate(..., method="fft")`), not a naive O(n²) loop —
  stays practical on multi-minute clips. Both signals are extracted at a
  reduced correlation sample rate (`compute_waveform_offset`'s
  `corr_samplerate`, default 8000 Hz) and capped at `max_seconds` (default
  600s) of decoded audio per file — a file longer than that only has its
  first `max_seconds` analyzed.
- **Timecode sync** (`compute_timecode_offset`): reads a WAV's BWF `bext`
  chunk `TimeReference` field (`read_bwf_timeref` — hand-parses the RIFF
  chunk list, no library dependency) and the video's embedded
  `tmcd`/format timecode tag, converts both to seconds-since-midnight, and
  diffs them. No waveform analysis involved; requires both sides to
  actually carry timecode metadata.
- **Export codecs** (`VIDEO_CODEC_PRESETS`): `v210` (10-bit uncompressed
  4:2:2, QuickTime-compatible — the usual "uncompressed" choice), `ffv1`
  (mathematically lossless, much smaller), `rawvideo` (true uncompressed,
  largest), `copy` (no video re-encode, only audio is processed/muxed).

## Gotchas

- **"Seek storm" freeze during playback** (fixed): recomputing a target
  frame index from wall-clock elapsed time every playback tick and
  comparing it to the last frame read looks reasonable, but timer jitter
  means it almost never lines up with "the very next frame" — nearly
  every tick then triggers a full seek, and seeking a compressed video is
  far more expensive than reading the next frame sequentially. If you
  touch the playback tick loop in `media_playback.py`, keep sequential
  reads sequential; don't reintroduce a per-tick seek recompute.
- **`sync_core.py` has zero Tkinter/UI imports on purpose** — this is
  what lets Studio Suite's `sync_worker.py` import and call it headlessly
  in a subprocess. Don't add a UI-layer import to this file.
- Playback needs a real audio output device; if `sounddevice`/PortAudio
  can't find one, every `media_playback.py` method raises a catchable
  `PlaybackUnavailable` rather than crashing — preserve that contract if
  you touch playback code, since the GUI depends on catching it to show a
  friendly message instead of a traceback.
- GUI-launched processes (Dock/Finder `open`, not a Terminal shell) get a
  different `PATH` — missing Homebrew's `/opt/homebrew/bin` has broken
  `ffprobe` resolution before. `ffprobe_util.py` already guards against
  this; if you vendor a fix here, copy it to the other apps' own
  identical copies too (B-Roll Analyzer, Local Interview Transcriber,
  Studio Suite).
