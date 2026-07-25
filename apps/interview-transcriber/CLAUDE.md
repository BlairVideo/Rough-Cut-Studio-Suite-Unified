# Local Interview Transcriber

**What it does**: Local-only macOS desktop app (Streamlit UI wrapped in a
pywebview native window) that batch-transcribes video interviews using
mlx-whisper (Apple Silicon Metal acceleration) with optional speaker
diarization via pyannote.audio. Nothing leaves the machine except a
one-time HF model-weight download.

## Key files

- `app.py` — the entire app (UI + pipeline + persistence), single file, ~1600 lines.
- `launcher.py` — spawns `streamlit run app.py` headless as a subprocess, waits
  for the port, opens it in a native `pywebview` window; `atexit` kills the
  Streamlit subprocess on window close.
- `branding/blair_seal.png` — optional logo asset (not bundled; the seal is
  Blair Academy IP — see `branding/README.txt`). App falls back to a dashed
  placeholder if absent.
- `requirements.txt`, `SETUP.md` — install/run instructions.

## Running it

```bash
source .venv/bin/activate
python launcher.py          # native window (normal use)
# or, for faster dev iteration in a browser tab with hot reload:
streamlit run app.py
```

Requires Homebrew `ffmpeg` on PATH. See `SETUP.md` for the full first-time
setup (HF token + license acceptance for diarization).

## Data / cache model

- Per-video cache: `<video_path>.ivt-cache.json`, written **next to the
  source video** (not in a central app-data dir). Validity is keyed on
  `(file size, int(mtime))` of the video — NOT a content hash. A rewrite of
  identical size within the same wall-clock second will NOT invalidate it.
- Global settings (`model_label`, `export_format`, `enable_diarization`,
  `rough_cut_studio_path`) persist to
  `~/Library/Application Support/InterviewTranscriber/settings.json`,
  written only when something actually changed (checked against
  `st.session_state._saved_settings`).
- HF token persists to the macOS Keychain via `keyring` (service
  `InterviewTranscriber`) — never written to `settings.json`.
- Cache/settings writes are best-effort. `save_cache()` returns `bool`
  success; `process_one_video()` surfaces a `st.warning` on the file's card
  if the write failed (e.g. video lives on a read-only/disconnected external
  volume) so the user isn't silently surprised by a full re-transcription
  later — this app is routinely used with videos on external drives.

## Threading model — do not "fix" this without understanding why

- Processing is strictly synchronous, one file per Streamlit rerun, on the
  main thread. `process_one_video()` runs, then the batch driver calls
  `st.rerun()` to advance to the next queued file.
- This is deliberate: Metal/MPS (used by mlx-whisper) can behave
  inconsistently or silently degrade across threads, so backgrounding
  transcription (e.g. `threading.Thread`/`asyncio`) is explicitly avoided.
- Consequence: the *batch-level* elapsed/ETA display only updates *between*
  files, never mid-file, and Pause/Cancel are only checked before starting
  the next file, never mid-file. This is by design, not a bug.
- Within a single file, `process_one_video(..., progress_callback=...)` DOES
  report live progress ("Extracting audio" / "Transcribing… N%" /
  "Diarizing speakers… N%") to an `st.progress` placeholder in the batch
  driver — this doesn't contradict the no-threading rule: it's a plain
  callback invoked synchronously from inside the same main-thread call, and
  `st.progress(...)` updates flush to the browser as they happen within a
  single script run, no rerun or thread required. See "Progress reporting"
  below for how the callback actually gets its numbers.

## Progress reporting (added after the initial build)

- `transcribe_audio()`'s progress comes from **monkeypatching `tqdm.tqdm`**
  process-wide for the duration of one `mlx_whisper.transcribe(..., verbose=False)`
  call (mlx_whisper has no public progress callback; it drives an internal
  tqdm bar via `import tqdm; tqdm.tqdm(total=content_frames, ...)`). The
  patch subclasses the *original* `tqdm.tqdm`, overrides `update()` to also
  invoke `progress_callback(n/total)`, swaps it in, and restores the
  original in a `finally`. This mutates the shared `tqdm` module attribute
  (not a local copy), so it's only safe because the app is strictly
  single-threaded and sequential — the patch is fully applied and reverted
  before diarization (which also uses tqdm, via pyannote/rich) even starts.
  Do not parallelize transcription without redesigning this.
- `diarize_audio()`'s progress uses pyannote's own public `hook=` mechanism
  (`_DiarizationProgressHook`, mirroring `pyannote.audio.pipelines.utils.hook.ProgressHook`'s
  call signature: `hook(step_name, step_artifact, file=, total=, completed=)`).
  Wrapped in a `try/except TypeError` fallback to calling the pipeline
  without `hook=` — pyannote's pipeline signatures have shifted across
  versions before (see the two-model-ID fallback in the same function), and
  the community-1 pipeline is fetched dynamically from the Hub so its exact
  `apply()` signature can't be verified statically.
- `process_one_video()`'s `progress_callback(phase, fraction, detail)`
  resets `fraction` to 0.0 at the start of each phase (extract/transcribe/diarize)
  — there's no attempt to blend these into one global percentage, since
  phase durations vary hugely by file length, model size, and whether
  diarization is on.

## Non-obvious gotchas for future changes

- **Streamlit widget key staleness**: a widget that passes both `value=` and
  `key=` ignores `value` once that `key` already exists in `session_state`
  from a prior rerun. Several per-speaker/per-file widget keys —
  `chk::{path}::{spk}`, `label::{path}::{spk}`, `editbox::{path}`,
  `merge_target::{path}`, `merge_sources::{path}`, `preview_cache::{path}`,
  `search::{path}` — must be cleared whenever a file is reprocessed (Retry /
  Force re-process), or stale checkbox/label/edit/merge/search state (or a
  crash, if the new speaker count differs from a stale `merge_target`) can
  silently reattach to the fresh result. `clear_file_cache()` is the single
  place responsible for this (it also resets `fr.undo_segments = None`,
  since that's a `FileResult` field rather than a session_state key) — if
  you add a new per-file widget key or mutable `FileResult` field, wire its
  cleanup into `clear_file_cache()` too.
- **Undo is single-level, in-memory only**: `fr.undo_segments` holds one
  snapshot (`copy.deepcopy(fr.segments)`) taken right before an "Apply
  Edits" click, not a full history stack. It's not persisted by
  `save_cache()` and doesn't survive an app restart or a Retry. If you need
  multi-level undo later, this field is the place it'd need to become a
  list/stack instead of a single snapshot.
- `process_one_video()`'s temp working directory is created once
  (`tempfile.mkdtemp(prefix="ivt_")`) and removed unconditionally in
  `finally`, regardless of whether `ffmpeg` extraction ever produced output —
  don't reintroduce a "only clean up if the file exists" check, that leaks a
  directory on every extraction failure.
- `merge_transcript_and_speakers`'s `label_for()` picks the diarization turn
  with the greatest overlap margin around a segment's midpoint, checking
  *all* turns rather than returning on the first one that merely contains
  the midpoint — needed because overlapping turns (cross-talk) are common in
  interview audio and turn order isn't guaranteed to be chronological.
- The "Preview transcript" and "Edit Transcript Text" expanders run their
  body on every Streamlit rerun for every finished file in the batch (an
  expander's contents execute even while collapsed) — both are memoized in
  `session_state` (`preview_cache::{path}`, and the editbox's default text is
  only built once) to avoid O(total segments across the whole batch) work on
  every unrelated click. If you add more per-file UI that formats
  transcripts, follow the same pattern rather than reformatting unconditionally.
- File selection goes exclusively through the native `osascript` multi-file
  picker (`browse_for_video_files`) — there's no folder-scan/glob code path.
