# Project Context & Guidelines

## 1. Project Overview
* **Name:** B-Roll Analyzer
* **Description:** A local desktop app for video editors (built for Blair
  Academy). Point it at a folder of b-roll video clips; it scores each
  clip on technical quality (sharpness, exposure, stability, optionally
  "high energy" content via a local CLIP model), finds the best
  segment(s) in each, and exports a Premiere Pro-compatible XML with a
  ranked bin + ready-made "best selects" sequence.
* **Current status:** Feature-complete for its intended scope. A `pytest`
  suite (`tests/`) covers `analyzer.py`'s scoring/segment-selection logic
  and `result_cache.py`'s cache-hit logic (see §4) -- `app.py`'s Tkinter
  layer, `xml_export.py`, and anything touching real video decode
  (`analyze_clip`, thumbnail/segment capture, `vision_energy.py`) still
  have no automated coverage; see §7. Packaging (both a py2app build and
  a full-capability Platypus build) already exists and works; see §6.
* **Docs to read first:** `README.md` (user-facing behavior, exhaustive
  "Notes / limitations" section) is accurate and detailed -- read it
  before making non-trivial changes. There is no `HANDOFF.md` in this
  project; maintainer-facing rationale instead lives inline as code
  comments/docstrings throughout `app.py`/`analyzer.py`/`xml_export.py`
  (this codebase leans heavily on "why" comments at the point of use
  rather than a separate design doc -- read the docstring on a function
  before changing it).

## 2. Tech Stack & Environment
* **Language:** Python 3.9+
* **GUI:** Plain **Tkinter/ttk** (`tkinter`, `tkinter.ttk`) -- a single
  `tk.Tk` subclass, `BRollAnalyzerApp` in `app.py`, builds the entire
  window imperatively. **Not** a web app, **not** pywebview, no HTML/JS
  runtime. `broll_analyzer_ui.html` in this folder is a static,
  standalone HTML/CSS/JS *design reference* (see its own header comment)
  used to share the look of the app with people who can't run Python --
  it is never loaded, served, or imported by `app.py`, has no live data
  or backend, and is not kept in sync automatically. If you change
  `app.py`'s `BRAND` palette or layout in a way that matters for design
  review, update `broll_analyzer_ui.html`'s `:root` CSS variables by hand
  to match, or it will silently drift.
* **Video/image analysis:** `opencv-python` (`cv2`) + `numpy`. Optical
  flow (`calcOpticalFlowFarneback`) for motion/stability, Laplacian
  variance for sharpness, frame-mean/clipping for exposure.
* **Optional "high energy" scoring:** `torch` + `open_clip_torch` +
  `pillow`, fully local zero-shot CLIP classification (`vision_energy.py`).
  No network calls after the one-time model-weight download; no
  Anthropic API or any other cloud API is involved anywhere in this app.
  Degrades gracefully (`vision_energy.is_available()` gates it) if these
  aren't installed.
* **External tool (optional, not a pip dependency):** `ffmpeg`/`ffprobe`
  on `PATH`, used read-only via `subprocess` to probe real audio
  channel/samplerate/bit-depth (`analyzer._probe_audio_format`) and as an
  FPS fallback for containers OpenCV can't read metadata from
  (`analyzer._probe_fps_fallback`). Everything else works without it;
  missing ffprobe just means conservative stereo/16-bit/48kHz defaults.
* **Export format:** Final Cut Pro XML (xmeml v5), hand-built with plain
  string templates in `xml_export.py` -- no `xml.etree.ElementTree`, no
  `opentimelineio`, no runtime XML-library dependency. This is the
  **only** export format; there is no FCPXML or OTIO exporter in this
  codebase (don't assume otherwise from the module name).
* **Concurrency:** Per-clip analysis is CPU-bound and independent, so
  `app.py` fans it out across a `concurrent.futures.ProcessPoolExecutor`
  (see §4). The GUI itself runs single-threaded on Tk's main thread.

## 3. File Map
* `app.py` -- the entire GUI + orchestration layer (~1600 lines, single
  `BRollAnalyzerApp(tk.Tk)` class). Entry point: `python app.py`.
* `analyzer.py` -- core scoring engine: `analyze_clip()` (full decode +
  score), `rescore_clip()` (cheap re-score from cached samples),
  thumbnail/segment-preview frame capture, video file discovery.
* `xml_export.py` -- `export_xml()`, the single Premiere/FCP XML builder.
* `result_cache.py` -- per-folder `.broll_analyzer_cache.json` (settings-
  independent per-frame samples, so window/segment/energy-weight changes
  never require re-decoding).
* `app_settings.py` -- `~/.broll_analyzer_settings.json` (UI option
  values only, restored on next launch).
* `vision_energy.py` -- optional local CLIP "high energy" scorer.
* `broll_analyzer_ui.html` -- static design-reference mockup only (see §2).
* `tests/` -- `pytest` suite (see §4/§7). `conftest.py` (repo root,
  otherwise empty) and `pytest.ini` exist solely to make `tests/`able to
  `import analyzer` / `import result_cache` without installing this
  project as a package -- don't delete either just because they look
  unused.
* Packaging (not part of app runtime): `setup.py` (py2app),
  `build_app.sh` / `build_full_distributable.sh` / `vendor_ffmpeg.sh` /
  `launch_broll_analyzer.sh`, `BUILD_MACOS.md`, `PLATYPUS_PACKAGING.md`.

## 4. How to Run & Test
* **Setup:** `python -m venv .venv && source .venv/bin/activate` (Windows:
  `.venv\Scripts\activate`)
* **Install:** `pip install -r requirements.txt` (the `torch`/
  `open_clip_torch`/`pillow` block is optional -- only needed for the
  energy-detection checkbox)
* **Run:** `python app.py` -- opens the Tkinter window directly.
* **Automated tests:** `pip install -r requirements-dev.txt && pytest`
  runs the suite in `tests/` -- pure unit tests against
  `analyzer.py`'s scoring/segment-selection logic and
  `result_cache.py`'s cache-hit logic, built entirely from
  hand-constructed `ClipResult`/`FrameSample` objects and `tmp_path`
  fixtures (no real video files, no `ffmpeg`, no GPU/CLIP model needed
  -- runs in well under a second). Run this after touching either
  module. **Not yet covered** by this suite: `app.py`'s Tkinter/
  threading layer, `xml_export.py`'s XML output, and anything that
  actually decodes a video file (`analyze_clip`, thumbnail/segment
  capture, `_probe_audio_format`/`_probe_fps_fallback`, `vision_energy.py`)
  -- those need real media fixtures and belong in a separate
  integration-style suite if/when one gets built.
* **Manual smoke check:** after any change, also run `python -m
  py_compile app.py analyzer.py xml_export.py result_cache.py
  app_settings.py vision_energy.py`, then validate against the flows in
  `README.md`'s "Using the app" section (analyze a small folder, sort
  columns, click a segment chip to preview inline, export XML, re-import
  into Premiere or at least eyeball the XML structure) -- `pytest`
  passing does not exercise any of that.

## 5. Code Style & Architectural Rules
* **Threading model:** the Tk main thread never blocks on analysis.
  `_start_analysis` spawns one `threading.Thread` (`self.worker_thread`)
  running `_run_analysis`, which itself owns a `ProcessPoolExecutor` and
  is the *only* thread allowed to touch `self._executor`. **Cancel is
  cooperative, never destructive:** `_cancel_analysis` only sets a
  `threading.Event`; it never calls `executor.shutdown()`/cancels futures
  from the main thread, because doing so concurrently with the worker
  thread's own wait on those futures previously deadlocked the process
  pool. If you add a new long-running operation, follow this same
  pattern (background thread owns its executor/resources exclusively;
  other threads only set flags it polls).
* **Worker-process functions must be picklable:** `_analyze_clip_worker`
  is a plain module-level function (not a method/closure) specifically so
  `ProcessPoolExecutor` can pickle and ship it. Keep any new
  process-pool task the same way, and make sure it catches its own
  exceptions into a return value (`ClipResult.error`) rather than letting
  them propagate as a `BrokenProcessPool`-style failure.
* **Generation counters, not cancellation, for background UI refreshes:**
  `_preview_generation` and `_segment_generation` are incremented
  whenever a background thumbnail-refresh thread or segment-decode
  thread/poll-loop should be considered stale (new selection, re-render,
  app close). Those threads/loops check `generation == self._X_generation`
  before touching the UI or looping again, rather than being interrupted.
  Reuse this pattern for any new background-thread-updates-the-UI feature.
* **Per-run settings must be threaded through, not re-read live:** UI
  option controls (checkboxes/spinboxes) are **not** disabled while an
  analysis run is in progress, so a value read via `self.some_var.get()`
  inside a completion callback can silently differ from what the run
  actually used. `_run_analysis` captures `enable_energy` etc. once at
  the top and threads it through `_on_analysis_complete(..., enable_energy=...)`
  and `self._last_enable_energy` rather than re-reading
  `self.enable_energy.get()` later -- a prior bug here caused the
  Energy column/preview text to keep showing a cached clip's old energy
  score even after unchecking "Detect high-energy" and re-running
  Analyze. Follow the same capture-at-start-of-run pattern for any new
  per-run option.
* **`ClipResult.energy_enabled` means "this clip's cached *samples*
  contain energy data," not "energy is active for the current run."**
  A cache hit can carry `energy_enabled=True` even when the current run
  has energy scoring turned off (see `analyzer.rescore_clip`'s
  docstring). Any UI code deciding whether to *display* an energy score
  must check both that flag *and* `self._last_enable_energy` -- use the
  `self._energy_active(result)` helper in `app.py` rather than reading
  `result.energy_enabled` directly for display purposes.
* **Caching split:** `analyzer.py` deliberately separates the expensive,
  settings-independent part of analysis (per-frame samples -- sharpness,
  exposure, motion, optional energy) from the cheap, settings-dependent
  part (composite score, best segment(s) -- `_score_clip`/`rescore_clip`).
  Only the former is cached (`result_cache.py`); changing window length /
  segments-per-clip / energy weight must never require re-decoding a
  cached clip, only re-running `rescore_clip`.
* **Video decode always goes through `analyzer._open_video_capture`**
  (tries the FFmpeg backend first, falls back to OpenCV's auto-selected
  one) -- never call `cv2.VideoCapture(path)` directly elsewhere; the
  FFmpeg-first choice exists specifically for camera-native formats
  (MXF, ProRes-in-MOV, MTS/M2TS) that other backends mishandle.
* **XMEML audio (`xml_export.py`):** every source audio channel --
  including both channels of a stereo clip -- is exported as its own
  linked clipitem pinned to that channel via `<sourcetrack>`, on its own
  sequence track. Never emit a single clipitem with
  `<channelcount>2</channelcount>` for a stereo source; that silently
  imports as mono in Premiere. This applies uniformly regardless of
  channel count (mono = 1 item, stereo = 2, 5.1 = 6, etc.).
* **Validation:** an LLM/model is not involved in selecting segments --
  segment selection is deterministic (`_score_clip`'s sliding-window +
  greedy non-overlap logic) and doesn't need re-validation the way an
  LLM-chosen index would, but a segment's start/end must always be
  clamped to `[0, result.duration]` before it reaches export (see
  `_score_clip`'s `max(0.0, ...)`/`min(result.duration, ...)` calls) --
  keep that pattern for any new segment-producing code path.
* **Secrets:** there are none in this app (no API keys, no network
  auth) -- the CLIP energy model downloads its weights from a public,
  unauthenticated host once, then runs fully offline. If a future change
  introduces any credential or cloud call, treat that as a significant
  architectural change worth calling out here explicitly, not a
  drop-in addition.
* **File access:** only through native Tkinter dialogs
  (`filedialog.askdirectory`/`asksaveasfilename`) or paths already
  discovered via `find_video_files`/an existing `ClipResult.path` --
  never read/write arbitrary user-supplied paths.
* **Error handling:** public-facing entry points (`_analyze_clip_worker`,
  cache load/save, settings load/save) catch their own exceptions and
  degrade gracefully (return a `ClipResult.error`, or fall back to
  defaults) rather than raising into the GUI thread or crashing the
  worker pool. Keep new I/O-touching code (especially anything reading a
  user's file or the autosave/cache JSON, which could be malformed) to
  that same standard.

## 6. Packaging
Two supported paths to a double-clickable macOS app, both already
working (see `BUILD_MACOS.md` / `PLATYPUS_PACKAGING.md` for full
details) -- packaging is **not** an open item:
* `./build_app.sh [--vendor-ffmpeg]` -- py2app build, smaller, code-signed
  ad-hoc for Apple Silicon.
* `build_full_distributable.sh` (Platypus-based) -- runs the real,
  unfrozen venv rather than a frozen py2app bundle; use this if the
  py2app build hits PyTorch op-registration issues with energy detection
  enabled.

## 7. Immediate Goals & Next Steps
- [x] Stand up a real `pytest` suite for `analyzer._score_clip`'s
      sliding-window/segment-selection logic and `result_cache.py`'s
      cache-hit/fingerprint logic -- done, see `tests/` and §4.
- [ ] Extend test coverage to `xml_export.py`'s frame/timecode math and
      per-channel audio splitting (pure string/math logic, no video
      decode needed -- same style as the existing suite).
- [ ] A real integration-style suite against small fixture video files
      for `analyzer.analyze_clip` end-to-end (real decode + `ffprobe`)
      and `app.py`'s threading/cancel/cache-reuse orchestration --
      meaningfully more setup than the current unit-test suite, since it
      needs actual media on disk.
- [ ] Frame-precise B-roll audio ducking, if it turns out to matter.
- [ ] Consider whether the Tkinter-variable reads inside
      `_run_analysis` (called on a background thread) are worth
      hardening -- they currently read `tk.Variable.get()` off the main
      thread, which works in practice but isn't a pattern to expand
      without care.
