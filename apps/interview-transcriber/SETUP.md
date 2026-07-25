# Local Interview Transcriber — Setup Guide (macOS / Apple Silicon)

A fully local, privacy-first batch transcription + diarization tool for
long-form video interviews. Nothing is uploaded anywhere except a one-time,
free model download from Hugging Face.

---

## 1. Prerequisites

- macOS 13+ on Apple Silicon (M1/M2/M3/M4)
- [Homebrew](https://brew.sh) installed
- Python 3.10–3.13 (mlx-whisper requires Apple Silicon + Python ≥3.9; the
  app's own venv currently runs 3.13 — verified end-to-end, including a
  real transcription, on 2026-07-15)

## 2. Install FFmpeg (via Homebrew)

```bash
brew install ffmpeg
```

Verify:

```bash
ffmpeg -version
```

## 3. Create a project folder and virtual environment

```bash
mkdir -p ~/interview-transcriber && cd ~/interview-transcriber
python3 -m venv .venv
source .venv/bin/activate
```

## 4. Install Python dependencies

Copy `app.py` and `requirements.txt` into this folder, then:

```bash
pip3 install --upgrade pip
pip3 install -r requirements.txt
```

> First install can take a few minutes — `pyannote.audio` pulls in
> `torch`/`torchaudio`.

## 5. Get a free Hugging Face token (only needed for diarization)

1. Create a free account at https://huggingface.co
2. Accept the license on **all three** of these model pages (pyannote
   has been migrating between pipeline versions, and depending on which
   version installs, the app may need any of them):
   - https://huggingface.co/pyannote/speaker-diarization-community-1
   - https://huggingface.co/pyannote/speaker-diarization-3.1
   - https://huggingface.co/pyannote/segmentation-3.0
3. Generate a **read** access token at
   https://huggingface.co/settings/tokens
4. Keep this token handy — you'll paste it into the app's sidebar. It's
   used once per session to download model weights; it is never sent
   anywhere else, and your audio/video files never touch Hugging Face.

If you'd rather skip diarization entirely, just uncheck "Enable speaker
diarization" in the app — you'll still get a full timecoded transcript,
just without speaker labels.

## 6. Run the app

```bash
python3 launcher.py
```

This starts the Streamlit server in the background (headless, no browser
tab) and opens the app in its own native window using `pywebview` — it
behaves like a regular desktop app rather than a browser page. Closing
the window shuts down the background server automatically.

(You can still run `streamlit run app.py` directly if you specifically
want it in a browser tab instead.)

## 7. Using the app

1. Click **🔍 Select Video Files** in the sidebar — this opens the
   native macOS file picker. Select one or more video files
   (`.mp4`, `.mov`, `.mkv`, `.avi`, `.m4v`, `.webm`); ⌘-click to
   multi-select. You can click the button again to add more files, or
   **Clear selection** to start over.
2. Pick a **Whisper model** — `medium` is a good accuracy/speed balance
   on M-series chips; use `large-v3` for the highest quality if you have
   16GB+ Unified Memory.
3. Paste your **Hugging Face token** if diarization is enabled (saved to
   macOS Keychain so you only do this once).
4. Click **Start Batch Processing**. Files are processed strictly one at
   a time; the app extracts audio, transcribes, diarizes, then deletes
   the temporary audio file and frees memory before moving to the next
   video — this keeps memory use flat even across many 20+ minute files.
5. Once a file finishes, uncheck any speakers you want excluded (e.g. the
   interviewer), or type a name/role into a speaker's label field.
6. Right above the file list, choose your **Export format** (SRT, VTT, or
   Plain text) — pick this right before exporting, since it applies to
   whatever you save next. Then click **💾 Save Transcript As...** on a
   file (or **💾 Export All** to go through every finished file). Each
   save opens a native macOS Save dialog where you choose the folder and
   filename yourself:

   ```
   [00:15:32] Speaker 1: ...text...
   ```

   ready to paste into an LLM for script/XML generation.

## Session persistence & editing

- **Cached results** — once a video is processed, its transcript is saved
  next to the video itself as `<video filename>.ivt-cache.json` (e.g.
  `interview.mp4.ivt-cache.json`). Re-selecting the same video later
  (even after quitting the app, even in a different folder location as
  long as the file itself hasn't changed) loads it instantly instead of
  re-transcribing. The cache is invalidated automatically if the video
  file's size or modified-date changes. Check **"Force re-process"** in
  the sidebar to redo a file anyway (e.g. after changing the Whisper
  model). If you move or share a video, its cache file travels with it
  if you copy both together.
- **Remembered settings** — your export format, Whisper model, diarization
  toggle, and Rough Cut Studio path are remembered between launches. The
  Hugging Face token is saved separately and securely in the macOS
  Keychain (via the `keyring` package) — use **"Forget saved token"** in
  the sidebar to remove it.
- **Editable transcript text** — each finished file has an **"✏️ Edit
  Transcript Text"** section where you can fix misheard words/names
  directly, one line per entry. Keep the same number of lines (don't add
  or remove any) and only edit the text after the colon, then click
  **"Apply Edits"**. If a change doesn't look right, click **"↩ Undo Last
  Edit"** (appears right after Apply Edits) to revert to the text as it was
  just before your last "Apply Edits" click — this only holds one step of
  history, and it resets if you leave and come back to the app.
- **Search within a transcript** — each finished file has a **"🔎 Search
  transcript"** section; type a word or phrase to see every matching line
  (with timecode and speaker) without scrolling through the full preview.

## Pause/cancel, time estimates, and Rough Cut Studio

- **Time estimate & per-file progress** — processing runs synchronously,
  one file at a time, on the main thread (deliberately not backgrounded —
  Metal/MPS can behave inconsistently across threads, and a straightforward
  sequential design is more reliable than a live-updating one that risks
  silently slowing things down). The batch-level elapsed/remaining estimate
  above the progress bar updates **between files, not continuously during
  one**, since it's based on actual measured per-file durations, which
  aren't known until a file finishes. Within the current file, though, a
  second progress bar shows real-time progress through "Extracting audio",
  "Transcribing… N%", and (if enabled) "Diarizing speakers… N%". Before any
  file in the batch has finished, the batch-level remaining-time estimate is
  a rough guess based on the video's duration (via `ffprobe`) and the
  selected Whisper model's typical speed, labeled "(rough estimate)"; once
  at least one file completes, it switches to a real estimate based on
  actual measured speed for the rest of the batch.
- **Pause / Resume** — click **⏸ Pause** to stop the queue after the
  current file finishes (never mid-file). Click **▶ Resume** to continue
  from where it left off — the next file starts fresh, nothing is lost.
- **Cancel Batch** — stops the queue the same way, after the current
  file finishes. Any file that hadn't started yet stays untouched;
  re-select it later to process it, or check "Force re-process" if you
  want to redo something that partially ran.
- **Rough Cut Studio link** — in the sidebar, click **"Locate Rough Cut
  Studio..."** and point it at either the app's `.app` bundle or its
  `main.py` launcher. Once set, a **"🎬 Launch Rough Cut Studio"** button
  appears in the sidebar, and a matching button shows up right after you
  save a transcript so you can jump straight there and drag the file in.
  Note: if Rough Cut Studio needs its own virtual environment, launching
  its `main.py` via the system `python3` may fail — in that case, launch
  it manually from its own activated environment instead, or package it
  as a `.app` so `open` can handle it.

## Notes on performance & memory

- Temp audio files are written to a per-file temp directory and deleted
  immediately after processing that file (no duplicated video data is
  ever kept on disk).
- `gc.collect()` runs after every file to release Unified Memory before
  the next (long) file begins.
- If you hit memory pressure on an 8GB Mac, use the `tiny` or `small`
  Whisper model, and process fewer files per batch.

## Troubleshooting

- **"ffmpeg not found"** — re-run `brew install ffmpeg` and restart your
  terminal so PATH updates take effect.
- **Diarization errors about gated repo / 401 / 403** — make sure you
  accepted the license on all three pages: `pyannote/speaker-diarization-community-1`,
  `pyannote/speaker-diarization-3.1`, and `pyannote/segmentation-3.0`,
  using the same account that generated your token, and that the token
  has at least "read" scope. The app tries `community-1` first and
  falls back to `3.1` automatically.
- **Slow first run** — the first transcription/diarization call downloads
  model weights (a few hundred MB to a few GB depending on model size);
  subsequent runs use the local cache.
