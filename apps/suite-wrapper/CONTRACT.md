# Studio Suite — Architecture Contract (v1)

This document is the binding contract between the suite backend and frontend.
Both are built in parallel against it. Do not deviate from names/shapes here;
add extras freely, but everything listed must exist exactly as specified.

## What this is

An all-in-one native desktop suite (pywebview, NOT a browser tab) unifying four
existing apps that live as siblings of this folder and MUST NOT be modified:

- `../Local Interview Transcriber` — mlx-whisper + pyannote transcription (Streamlit app)
- `../B-Roll Analyzer`             — clip scoring/selects (Tkinter app, UI-free core modules)
- `../Blair Brander`               — brand title/lower-third graphics (Tkinter app, UI-free core modules)
- `../Rough Cut Studio`            — LLM rough-cut editor (pywebview app; its backend + frontend are REUSED wholesale)

Principles:
- Existing apps stay intact and independently runnable. The suite imports their
  UI-free modules and/or spawns their venv interpreters in subprocesses.
- All local processing stays local. The ONLY network calls are Rough Cut
  Studio's existing Gemini path (and its optional Ollama localhost path) and
  one-time HF/CLIP model weight downloads that the underlying libraries already do.
- Heavy work runs as simultaneous background jobs (multiple OS processes).

## Directory layout (this folder = SUITE_DIR)

```
Studio Suite/
  CONTRACT.md                 (this file)
  main.py                     entry point: composes the page, creates pywebview window
  requirements.txt            pywebview>=6, requests, Pillow>=10, keyring>=25 (+pyobjc frameworks on macOS)
  launch_studio_suite.sh      creates/uses .venv, installs reqs if missing, runs main.py
  backend/
    __init__.py
    suite_api.py              class SuiteApi(RoughCutApi) — the single js_api
    jobs.py                   JobManager (thread-safe), Job dataclass, subprocess runner
    paths.py                  resolves SUITE_DIR, sibling app dirs, worker interpreters, assets dirs
    handoff.py                VTT builders + send-to-edit helpers
    brander_bridge.py         imports Blair Brander core modules (sys.path insert)
    workers/
      transcribe_worker.py    run with Transcriber's .venv python
      broll_worker.py         run with B-Roll Analyzer's .venv python
  frontend/
    shell.html                template with placeholders (see Page composition)
    suite.css
    suite.js
    _generated/               main.py writes index.html here at each launch (create dir if missing)
  assets/
    transcripts/              generated VTT handoff files
    graphics/                 exported Brander PNG/MOV files
```

Sibling app dirs (note: parent folders contain LEADING SPACES — always build
paths with os.path.join from `os.path.dirname(SUITE_DIR)`, never hardcode strings):

- `RCS_DIR       = <parent>/Rough Cut Studio`
- `IVT_DIR       = <parent>/Local Interview Transcriber`
- `BROLL_DIR     = <parent>/B-Roll Analyzer`
- `BRANDER_DIR   = <parent>/Blair Brander`
- `IVT_PYTHON    = IVT_DIR/.venv/bin/python`
- `BROLL_PYTHON  = BROLL_DIR/.venv/bin/python`

## Page composition (main.py, at every launch)

1. Read `RCS_DIR/frontend/index.html`. Extract:
   - `RCS_HEAD_LINKS`: all `<link ...>` tags from its `<head>` (fonts/styles).
     Local link targets are COPIED into `_generated/` (as `rcs-<basename>`)
     and the href rewritten to that bare relative filename. Absolute URLs
     (Google Fonts) pass through. Rationale: pywebview auto-starts its
     built-in HTTP server for local windows, and WKWebView blocks `file://`
     subresources on an http-origin page — absolute file URIs render the
     window unstyled. Same-directory relative refs work under both schemes.
   - `RCS_BODY`: inner HTML of `<body>`, minus its `<script ... app.js ...>` tag(s).
2. Copy `RCS_DIR/frontend/app.js` → `_generated/rcs-app.js` and
   `frontend/suite.css` / `frontend/suite.js` → `_generated/`, then read
   `frontend/shell.html` and replace placeholders:
   - `{{RCS_HEAD_LINKS}}` — as above
   - `{{RCS_BODY}}` — as above (shell.html places it inside `#workspace-edit`)
   - `{{RCS_APPJS_SRC}}` — `rcs-app.js` (relative)
   - `{{SUITE_CSS_HREF}}` / `{{SUITE_JS_SRC}}` — `suite.css` / `suite.js` (relative)
3. Write result to `frontend/_generated/index.html`; `webview.create_window`
   with that path, `js_api=SuiteApi()` instance, title "Rough Cut Studio Suite",
   width 1440, height 900, min_size (1100, 720), background_color "#101116",
   text_select True. Assign `api.window = window` after create_window (RoughCutApi
   requires it), then `webview.start(debug=False)`.
4. `main.py --selftest` must, WITHOUT opening a window: compose the page to
   _generated, instantiate SuiteApi, call transcriber_models(), brander_defaults(),
   suite_list_jobs(), print "SELFTEST OK" and exit 0 (nonzero + traceback on failure).

Script load order in shell.html: RCS app.js first, then suite.js (both classic
scripts at end of body). RCS app.js top-level `const/let/function` declarations
are accessible to suite.js.

## SuiteApi (backend/suite_api.py)

`class SuiteApi(Api)` where `Api` is imported from `RCS_DIR/backend/api.py`
(sys.path.insert of `RCS_DIR/backend`). ALL Rough Cut Studio methods are
inherited unchanged — the RCS frontend keeps working against
`window.pywebview.api.*`. New methods below. Every method returns a dict with
at least `{"ok": bool}` and `{"error": str}` when not ok; never raise to JS.
pywebview calls these on worker threads — JobManager must be thread-safe (lock).

### Jobs

- `suite_list_jobs()` → `{ok, jobs: [Job...]}` newest first.
  Job = `{id: str, kind: str, label: str, status: "queued"|"running"|"done"|"error"|"cancelled",
  progress: float 0..100, detail: str, error: str|null, result: dict|null,
  created_at: float, finished_at: float|null}`
  kinds: `"transcribe" | "broll" | "brander_video" | "brander_send"`.
  `result` may be large (segments/clips) — that is fine, frontend fetches jobs by polling this.
- `suite_cancel_job(job_id)` → `{ok}` (terminate subprocess / set cancel flag; status → "cancelled")
- `suite_clear_finished_jobs()` → `{ok}` (drop done/error/cancelled jobs from the list)

Subprocess worker protocol (stdout, one JSON object per line):
`{"type":"progress","progress":0-100,"detail":"..."}` /
`{"type":"result","data":{...}}` / `{"type":"error","message":"..."}`.
stderr is captured; on nonzero exit without a result, job status = "error" with
last stderr lines in `error`. Workers receive their params as a JSON string in argv[1].

Concurrency: jobs of different kinds always run simultaneously. Transcribe jobs
are additionally throttled by a per-kind limit (default 1 running at a time —
Metal/MPS contention; see IVT CLAUDE.md) with the rest "queued"; broll/brander
jobs run immediately. Limit adjustable via `transcriber_set_parallel(n)` → `{ok}`.

### Transcriber (namespaced `transcriber_`)

- `transcriber_models()` → `{ok, models: [{label, repo}], default_label}` — the 4 mlx-whisper models
  (tiny/small/medium/large-v3, same labels+repos as IVT app.py WHISPER_MODELS; default "Recommended (medium)").
- `transcriber_pick_videos()` → native multi-file open dialog (video extensions
  .mp4 .mov .mxf .avi .mkv .m4v + .mp3 .wav ok to include video-only) → `{ok, paths: [str]}`
- `transcriber_hf_token_status()` → `{ok, present: bool}` — keyring service
  "InterviewTranscriber", key "hf_token" (same store the IVT app uses).
- `transcriber_save_hf_token(token)` → `{ok}` (empty string deletes; keyring only, never a file)
- `transcriber_start(paths: [str], model_label: str, enable_diarization: bool)` →
  `{ok, job_ids: [str]}` — one job per path, label = video basename.
  Job runs `workers/transcribe_worker.py` with `IVT_PYTHON`.
  Job result data: `{video_path, segments: [{start,end,text,speaker,avg_logprob,no_speech_prob}],
  speakers: [str], cache_path: str}`. The worker MUST also write the standard
  `<video>.ivt-cache.json` next to the video (same schema as the IVT app: path,
  name, video_size, video_mtime, speakers, segments, speaker_labels: {},
  excluded_speakers: []) so the standalone app sees the result too.
- `transcriber_send_to_edit(job_id)` → `{ok, source_id, vtt_path}` — builds a VTT
  from the finished job's segments (see Handoff), writes it to assets/transcripts/,
  ingests it into the inherited RCS state via `self._add_transcript(vtt_path)`.
  Frontend then re-renders the RCS source list.

### B-Roll (namespaced `broll_`)

- `broll_pick_folder()` → folder dialog → `{ok, path}`
- `broll_start(folder, options)` → `{ok, job_id}`; options (all optional, defaults):
  `{window_sec: 4.0, max_segments: 1, min_segment_gap_sec: 1.0, enable_energy: false,
  energy_weight: 0.35, max_workers: 3}`. Runs `workers/broll_worker.py` with `BROLL_PYTHON`.
  Worker uses B-Roll Analyzer's analyzer.py + result_cache.py (cache read AND write in
  the analyzed folder, keyed as that app does) and its own ProcessPoolExecutor
  (max_workers) internally; progress = completed/total files.
  Job result data: `{folder, clips: [{path, filename, duration, fps, width, height,
  overall_score, best_window_start, best_window_end, segments: [{start,end,score}],
  thumbnail_data_uri: str|null, error: str|null}]}` sorted best-first.
- `broll_send_to_edit(selections: [{path, start, end}])` → `{ok, cuts: [CutSpec], sources_added: [str]}`.
  For each unique clip path: write (if not already present) a synthetic one-cue VTT
  in assets/transcripts/ named `<stem> — broll.vtt` with `NOTE Source video: <path>`
  and a single cue 00:00:00.000 → clip duration, text `B-roll: <filename>`; ingest via
  `self._add_transcript(...)` (this auto-links the media path via the NOTE header).
  CutSpec = `{source_id, start_seconds, end_seconds, in_tc, out_tc, track: "broll"}`
  (timecodes via inherited `self.format_timecode`). The FRONTEND inserts these as
  b-roll rows in the RCS Cuts table (see suite.js responsibilities).
- `broll_export_xml(job_id, selected_paths: [str]|null)` → native save dialog →
  `{ok, path}` — exports the analyzer's own Premiere XML via its xml_export.export_xml
  (reconstruct ClipResults from the folder cache inside a short-lived call to the
  broll worker subprocess OR by importing result_cache with the suite python — note
  result_cache/analyzer import cv2/numpy which are NOT in the suite venv, so this
  MUST go through the worker subprocess: add a `mode:"export_xml"` to broll_worker.py).

### Brander (namespaced `brander_`) — runs in-process (Pillow only)

`brander_bridge.py` inserts `BRANDER_DIR` into sys.path and imports brand,
renderer, export, prompt_ai, project_io, assets. (These are Tkinter-free.)

- `brander_defaults()` → `{ok, scene, options}` where scene = a JSON-safe copy of
  the app's default scene (replicate app.py default_scene(): the literal dict +
  `scene.update(brand.PRESETS[brand.DEFAULT_PRESET])`; tuples → lists) and options =
  `{presets: [names], preset_values: {name: dict}, fonts: [names], primary_colors: {name: hex},
  secondary_colors: {name: hex}, canvas_presets: {name: [w,h]}, layouts, background_styles,
  animations, outro_animations, lower_third_positions, vignette_shapes, logos: [names], fps}`.
- `brander_preview(scene, t: float, max_width: int = 960)` → `{ok, data_uri, width, height}` —
  render_frame(scene, t) downscaled to max_width, PNG data URI. Convert
  scene["canvas_size"] list→tuple before rendering. Composite transparent
  frames over a checkerboard OR dark background for display (frontend shows as-is).
- `brander_still_preview(scene, max_width)` → same but render_still (plateau moment).
- `brander_interpret(prompt_text, scene)` → `{ok, scene, notes: [str]}` (local prompt_ai.interpret).
- `brander_export_png(scene)` → save dialog (.png) → `{ok, path}` (export.export_png).
- `brander_export_video(scene, codec: "mov"|"webm")` → save dialog (matching ext) →
  `{ok, job_id}` — background job (kind "brander_video", thread-based is fine;
  export.export_video manages its own multiprocessing pool). Result: `{path}`.
- `brander_send_to_edit(scene)` → `{ok, job_id}` — job kind "brander_send": exports
  qtrle alpha `.mov` to `assets/graphics/<slug>-<timestamp>.mov`, then on completion
  creates a synthetic b-roll VTT source for it exactly like broll_send_to_edit
  (duration = scene duration + hold_seconds). Job result:
  `{media_path, source_id, cut: CutSpec}` — frontend inserts the cut row.
- `brander_save_project(scene)` / `brander_load_project()` → dialogs, .blairtitle
  via project_io → `{ok, path}` / `{ok, scene, path}`.

### Handoff VTT format (backend/handoff.py)

```
WEBVTT

NOTE Source video: /abs/path/to/video.mp4

00:00:01.000 --> 00:00:04.000
Speaker 1: Hello there
```
(hours always present HH:MM:SS.mmm; speaker prefix only when a speaker exists;
RCS's parser auto-detects VTT and auto-links media from the NOTE header.)
Filenames must be unique per source video (append ` -2`, ` -3` on collision, since
RCS source_id = filename stem). After `self._add_transcript`, return its source_id.

## Frontend (shell.html / suite.css / suite.js)

Layout (dark, Premiere/Resolve-inspired, reusing RCS's design tokens
--bg-void/--bg-panel/--amber/--teal etc. from its style.css which is loaded first):

- Fixed top bar `#suiteTopbar`: brand "ROUGH CUT STUDIO SUITE", centered workspace
  tabs (`.suite-ws-tab`, data-ws attr): "Transcribe", "B-Roll", "Graphics", "Edit";
  right side: jobs button `#suiteJobsBtn` with running-count badge `#suiteJobsBadge`.
- Workspace containers, exactly one visible at a time (default "Edit" if RCS has
  restorable state, else "Transcribe"):
  `#workspace-transcribe`, `#workspace-broll`, `#workspace-graphics`,
  `#workspace-edit` (contains `{{RCS_BODY}}` unmodified).
- Jobs drawer `#suiteJobsDrawer` (slide-over right panel): per-job card with kind
  icon, label, progress bar, detail line, cancel ✕ for queued/running, error text,
  and for done jobs a context action button ("Send to Edit" for transcribe;
  "Open in Edit" behaviors as below). "Clear finished" button. Poll
  `suite_list_jobs` every 1s while drawer open OR any job non-terminal; stop
  polling when idle.
- Toasts `#suiteToasts` for confirmations/errors.

Workspace panels (build clean, professional forms — no placeholder lorem):

1. Transcribe: file list (add via transcriber_pick_videos, remove), model
   dropdown (transcriber_models), diarization toggle + HF-token status row
   (present ✓ / input to save), parallel limit stepper, "Start Transcription" →
   transcriber_start. Finished jobs render a transcript preview (first N
   segments, speaker-colored) + "Send to Edit" → transcriber_send_to_edit, then
   toast + refresh RCS sources (call the RCS frontend's source re-render — see below).
2. B-Roll: folder picker row, options (window sec, segments/clip, min gap,
   energy toggle + weight, workers), "Analyze Folder" → broll_start. Results
   grid (from job result): thumbnail, filename, score bar, per-segment chips
   with checkbox selection (default: all segments of top clips unchecked;
   user selects), "Send Selected to Edit as B-Roll" → broll_send_to_edit then
   insert rows (below), "Export Premiere XML…" → broll_export_xml.
3. Graphics: left form column (title/subtitle text, preset dropdown, layout,
   canvas, colors via swatch rows from options, font dropdowns, logo dropdown +
   color mode, divider checkbox, background style, sizes/opacity sliders,
   AI-prompt bar wired to brander_interpret with notes toast), right column:
   live preview image (call brander_still_preview on any change, debounced
   ~200ms; a time scrubber 0..1 calling brander_preview while dragging) +
   buttons: Export PNG…, Export Video… (codec select), Save/Load Project…,
   "Send to Edit" → brander_send_to_edit.
4. Edit: the untouched RCS UI.

### suite.js ↔ RCS frontend integration (critical)

- Wait for `pywebviewready` (same pattern as RCS `whenApiReady`).
- After any `*_send_to_edit` that adds sources, refresh the RCS source list by
  calling the RCS frontend's own render path. READ `../Rough Cut Studio/frontend/app.js`
  (2400 lines) first and use its actual top-level functions/state (they are
  accessible from a sibling classic script). Expected available: a sources
  render function and `state.editSegments` + row-insert helpers
  (`appendCutRow`/`renderEditTable`/`pushUndoSnapshot` or equivalents — verify
  real names in the file, do not guess).
- Inserting b-roll cut rows from CutSpecs: follow RCS rules — spread-copy cut
  objects, preserve/assign `_cid` the way app.js does for its own "Add Cut Row",
  push an undo snapshot after syncing pending DOM edits (use its own helpers).
  If there is no generated result yet (RCS `#outputBlock` hidden / no cuts),
  fall back gracefully: still add the source, and toast "Source added — cuts
  will be insertable after you generate or add a first main cut" (b-roll
  requires ≥1 main cut at export time, but rows may still be added to the table
  if RCS supports it — decide from the code, prefer inserting whenever the Cuts
  table exists).
- Switch to the Edit workspace automatically after a successful send-to-edit.
- Do NOT modify RCS files. All glue lives in suite.js.

## Verification requirements (both agents)

- Backend: `python main.py --selftest` passes using the suite venv; also
  `IVT_PYTHON workers/transcribe_worker.py --selfcheck` and
  `BROLL_PYTHON workers/broll_worker.py --selfcheck` (import-only smoke: import
  their app modules, print "WORKER OK", no model downloads, no video decode).
- Frontend: shell.html placeholders exactly as named; `node --check` suite.js.
- Neither agent modifies ANY file outside `Studio Suite/`.

---

# Contract Addendum v2 — feature round 2 (2026-07-12)

Same rules as v1: names/shapes below are binding for both sides; sibling app
folders stay unmodified. All paths relative to THIS Studio Suite folder
(inside "Rough Cut Studio Suite — All-In-One").

**Dialog-label rule (bug class fixed once already):** pywebview validates
file-dialog descriptions against `^[\w ]+$` before the `(...)` — letters,
digits, underscore, and spaces ONLY. No `/`, `&`, `-`, or punctuation in any
new `file_types` description.

## A. Transcribe — transcript editing + speaker ops (persisted to cache)

The editable truth for a finished transcription is the app-standard
`<video>.ivt-cache.json` (same file the standalone Transcriber reads). The
suite reads/writes it directly (plain JSON + os.stat — no heavy deps):

- `transcriber_load_cache(video_path)` → `{ok, found: bool, segments, speakers,
  speaker_labels: {raw_name: display}, excluded_speakers: [raw_name]}` —
  `found:false` when absent or stale (validity = size equal AND int(mtime)
  equal, exactly like the standalone app). Lets the Transcribe workspace
  reopen past results after an app restart without re-transcribing.
- `transcriber_update_transcript(video_path, segments, speakers,
  speaker_labels, excluded_speakers)` → `{ok, cache_path}` — rewrites the
  cache with the edited state (schema identical to v1 write_ivt_cache but
  honoring the passed labels/exclusions). Validates each segment has
  numeric start/end and string text/speaker; rejects otherwise.
- `transcriber_send_to_edit(job_id)` (CHANGED): before building the VTT,
  reload the cache for the job's video; if found, use its segments +
  speaker_labels + excluded_speakers instead of the stale job result —
  excluded speakers' segments are dropped, display names substituted
  (mirror the standalone `_visible_segments`).
- `transcriber_send_cache_to_edit(video_path)` → same as above but for a
  cache-loaded file with no job this session. Returns `{ok, source_id, vtt_path}`.

Frontend (Transcribe workspace): finished/loaded files get an editor:
per-segment text editing, per-segment speaker reassignment (dropdown of
speakers + "New speaker…"), speaker rows with rename (label), merge
(reassign all segments of A into B), include/exclude toggle (isolation).
"Save Edits" → update_transcript; "Send to Edit" uses the saved state.
An "Open existing transcription…" flow calls transcriber_pick_videos then
transcriber_load_cache per file.

## B. B-Roll — segment preview

- `broll_preview_url(path)` → `{ok, url}` — serves the clip through the
  inherited RCS `self.preview_server.url_for(path)` (lazy-started, token
  URL, Range support). Guard: `os.path.isfile(path)` AND extension in RCS's
  allowed video set (reuse its `_is_allowed_media_path`-equivalent check).
- Frontend: each clip card gets a preview player (collapsed until first
  play). A segment chip's ▶ loads the URL once, seeks to `start`, plays and
  loops `[start, end)` via timeupdate. Only one active preview at a time.

## C. Blair Brander

1. **Logo placement** — `brander_defaults().options` gains
   `"logo_placements": ["top-left","top-center","top-right","center",
   "bottom-left","bottom-center","bottom-right"]` (matches the standalone
   app's list; scene key `logo_placement`, default "bottom-center").
   Frontend adds the select to the Logo section.
2. **Logo import** — `brander_import_logo()` → open dialog
   (`"PNG or JPEG image (*.png;*.jpg;*.jpeg)"`) → copies the file into
   `assets/logos/` (new `paths.LOGOS_DIR`, added to ensure_suite_dirs),
   registers `brand.LOGO_SOURCES["Custom: <stem>"] = <absolute copied path>`
   (assets.py does `os.path.join(ASSET_DIR, filename)` — an absolute
   filename wins the join, verified), persists the registry to
   `assets/logos/custom_logos.json`, returns `{ok, logos: [all names],
   selected: "Custom: <stem>"}`. brander_bridge re-registers persisted
   entries at import time (skipping missing files). Name collisions get
   " 2", " 3" suffixes. White-background keying (`load_transparent`)
   applies to imports too — document in the UI hint that logos on white
   are auto-keyed.
3. **Logo scale** — frontend slider `logo_height` range becomes 40–640
   (was capped low); `lower_third_scale` range 0.5–2.0. No backend change
   (renderer's fit_height upscales with LANCZOS).
4. **Gemini AI mode** — new `backend/brander_gemini.py` +
   `brander_ai_generate(prompt_text, scene, api_key=None)` →
   `{ok, scene, notes: [str], provider: "gemini"}`.
   - Key precedence: explicit arg, else inherited `load_saved_api_key()`
     (.env / env var — the same key store RCS uses). Missing key →
     `{ok:false, error:"no_api_key", ...}` so the UI can prompt.
   - Direct `requests` POST like RCS's gemini_client (model
     "gemini-flash-latest", key ONLY in `x-goog-api-key` header, never
     logged/echoed, responseSchema-enforced JSON). Response is a flat
     "scene update" object of WHITELISTED fields only: title, subtitle
     (strings ≤200 chars), title_size/subtitle_size/logo_height/
     logo_opacity/vignette (ints, clamped to UI ranges), divider/
     transparent_bg (bool), duration/hold_seconds (floats clamped 0.5–30 /
     0–10), bg_color/accent_color/text_color/logo_custom_color (#rrggbb
     validated by regex), title_font/subtitle_font (enum: FONTS keys),
     layout, background_style, animation, outro_animation, logo,
     logo_placement, logo_color_mode, canvas_preset_name,
     lower_third_position (all enum-validated against brand constants +
     current custom logos), the twelve *_in_/*_out_ timing floats (clamped
     0..1), plus `notes: [str]`. EVERY field is re-validated/clamped
     server-side after the response; invalid values are dropped with a
     note. Applied onto a copy of the incoming scene — canvas_size synced
     from canvas_preset_name via CANVAS_PRESETS.
   - Network scope: this endpoint call is the ONLY new network traffic.
   - Frontend: AI bar gains a mode toggle `Local | Gemini`. Gemini mode
     with no key shows a one-line key field (session-only, passed as arg;
     "Save key" button calls the inherited `save_api_key_to_disk`).
     Notes render as toasts, same as Local mode.
5. **Animation timeline** — frontend widget (no backend): rows Title /
   Subtitle / Logo. Each row shows an IN bar (`{el}_in_start..{el}_in_end`)
   and, when `outro_animation != "none"`, an OUT bar (`{el}_out_start..`).
   Bars drag as a whole; each end drags individually. Clamps: 0..1, bar
   min-width 0.02, IN must end before OUT starts with ≥0.02 gap (the
   standalone timeline.py's CROSS_GAP behavior). Snap to
   `1/(fps*duration)`. A time scrubber underneath drives
   `brander_preview(scene, t)` (existing throttle), with a Play button
   stepping t in real time (duration+hold seconds). Editing pushes
   Graphics undo entries.

## D. Edit workspace — B-roll timeline (frontend only, suite-owned)

`#suiteTimeline`, a collapsible panel suite.js injects at the bottom of
`#workspace-edit` (header bar "TIMELINE" + collapse chevron; body ~150px):

- Reads RCS `state.editSegments` (after `readEditTableIntoState()`ing
  pending DOM edits): V1 lane = main cuts laid end-to-end (length =
  out_seconds − in_seconds, from in_tc/out_tc parsed at RCS `rulerFps`);
  b-roll lanes = greedy interval scheduling of b-roll cuts at their
  timeline_start (parse `timeline_start_tc`; blank → 0). Blocks show name
  + duration; amber = main, teal = b-roll.
- B-roll blocks drag horizontally (snap to whole frames at rulerFps;
  clamp ≥ 0). Drop → RCS's own mandated order: `flushTcEditSnapshot()`,
  `readEditTableIntoState()`, `pushUndoSnapshot()`, set the cut's
  `timeline_start_tc` (format seconds → SMPTE at rulerFps, non-drop-frame;
  drop-frame projects: format via `await api.format_timecode(seconds)`
  instead), then update that row's timeline-start input in the DOM and
  call `refreshBrollOverlapWarnings()` — do NOT full-rebuild the table for
  a one-field change (RCS rule). Main-track blocks are not draggable
  (their order lives in the Cuts table).
- Clicking a block scrolls its Cuts-table row into view + flashes it.
- Refresh: on workspace switch to Edit, after our own drags, after "send
  to edit" insertions, and via a throttled MutationObserver on
  `#editTableBody` so RCS-side edits (undo, Apply, reorder) re-render the
  timeline. If required RCS globals are missing, the panel shows a quiet
  "timeline unavailable" note instead of breaking.

## E. Undo/redo throughout

suite.js gets one `SuiteUndo` helper (bounded stacks, max 50, snapshot =
JSON string) with named domains:

- `graphics` — every committed scene change (form field commit, swatch,
  preset apply, AI apply, timeline drag end; sliders push once on
  release/commit, not per input event). Undo/redo buttons in the Graphics
  header + keyboard.
- `transcribe` — one domain per video file; snapshots {segments,
  speaker_labels, excluded_speakers, speakers} around every editor
  mutation (text commit, rename, merge, exclude, segment speaker change).
  Buttons in the editor header + keyboard.
- B-Roll segment selection toggles — same mechanism, cheap.
- Edit workspace: RCS's own undo/redo already covers the Cuts table, and
  the suite timeline routes through `pushUndoSnapshot()` so drags are
  undoable inside RCS's stack.
- Keyboard routing: cmd/ctrl+Z and shift+cmd/ctrl+Z dispatch to the ACTIVE
  workspace's domain — but NEVER when the Edit workspace is active or the
  event target sits inside `#workspace-edit` (RCS owns those), and never
  when typing in a text field with native undo focus unless the field is
  one of our editor fields whose change was already committed.

## F. Verification additions (both agents)

- Backend: extend `--selftest` to assert `logo_placements` present,
  `transcriber_load_cache` on a temp fake video returns found:false, and
  `brander_ai_generate` with no key returns ok:false error "no_api_key"
  (no network call). Gemini request path unit-tested by validating a FAKE
  response dict through the validator (no live call).
- Frontend: `node --check`; stub-page screenshots of: transcript editor
  (labels/merge/exclude visible), b-roll preview player + chips, Graphics
  logo placement select + import button + timeline widget with draggable
  bars + AI mode toggle, Edit workspace with #suiteTimeline showing main
  and b-roll blocks (stubbed segments), undo/redo buttons.

---

# Contract Addendum v3 — A-Sync integration (2026-07-13)

Fifth app: `../A-Sync` (audio/video sync). Its `sync_core.py` is fully
headless (module-level imports: numpy + scipy only; ffmpeg/ffprobe on PATH
at runtime). A-Sync now has its own venv (`../A-Sync/.venv`, full
requirements incl. scipy). Its Tkinter app (`sync_app.py`) stays untouched
and independently runnable. A-Sync has NO timeline/XML export of its own —
the suite builds that.

**Offset semantics (from sync_core.waveform_offset docstring, binding
everywhere):** `compute_offset(video_path, audio_path, method)` returns the
seconds the EXTERNAL AUDIO must be DELAYED to line up with the video;
negative = the audio starts before the video (trim its head). Therefore:
`video_time = audio_time + offset`. Arg order is always (video, audio) —
video is the reference.

## paths.py additions
`ASYNC_DIR = <parent>/A-Sync`, `ASYNC_PYTHON = ASYNC_DIR/.venv/bin/python`,
`SYNC_WORKER = backend/workers/sync_worker.py`.

## New worker: backend/workers/sync_worker.py (run with ASYNC_PYTHON)
Same JSON-line protocol + fd-redirect preamble + __main__ guard as the
other workers; sys.path-inserts ASYNC_DIR (resolved from __file__, same
pattern as broll_worker). Modes via params.mode:
- `"probe"` `{paths: [str]}` → result `{probes: {path: Probe}}` where
  Probe = `{duration, fps, width, height, has_video, has_audio,
  audio_channels, audio_samplerate, audio_sample_fmt, audio_bits,
  audio_format_label, timecode_tag}` — built from sync_core.probe() raw
  JSON (width/height read from the first video stream of the raw probe —
  ProbeInfo alone lacks them) + ProbeInfo fields.
- `"detect"` `{video_path, audio_paths: [str], method: "waveform"|"timecode"}`
  → progress per file → result `{video: {path, probe: Probe},
  tracks: [{path, filename, offset_seconds, probe: Probe, error: str|null}],
  method}`. For waveform: decode the video's reference PCM ONCE
  (sync_core.extract_mono_pcm(video, 8000, 600.0)) and call
  sync_core.waveform_offset(ref, extract_mono_pcm(audio, 8000, 600.0), 8000)
  per audio file (avoids re-decoding the video N times). For timecode:
  sync_core.compute_timecode_offset per pair (per-file errors recorded, not
  fatal). One bad file never fails the batch.
- `--selfcheck` → import sync_core, assert compute_offset/waveform_offset/
  extract_mono_pcm/probe_info exist, print "WORKER OK".

## SuiteApi additions (namespaced `sync_`)
- `sync_pick_video()` → single-file dialog, video extensions
  (.mp4 .mov .mxf .avi .mkv .m4v) → `{ok, path}`.
- `sync_pick_audio()` → multi-file dialog, label
  `"Audio files (*.wav;*.aif;*.aiff;*.mp3;*.m4a;*.flac;*.caf)"` (obeys the
  ^[\w ]+$ description rule) → `{ok, paths}`.
- `sync_probe(paths: [str])` → short-lived worker subprocess (mode probe,
  timeout ~60s) → `{ok, probes}`. Used for the info line under pickers.
- `sync_start(video_path, audio_paths, method="waveform")` →
  `{ok, job_id}` — job kind `"sync"` (new; runs immediately, no per-kind
  throttle) with ASYNC_PYTHON + SYNC_WORKER, cwd ASYNC_DIR.
- `sync_save_offsets(video_path, tracks)` / `sync_load_offsets(video_path)`
  → persist/restore a sidecar `<video>.sync-offsets.json`
  `{video_path, method, tracks: [{path, offset_seconds}], updated_at}`
  (A-Sync itself persists nothing; this sidecar is suite-owned, written
  next to the video, best-effort like the IVT cache). load returns
  `{ok, found, ...sidecar}`.
- `sync_send_to_transcriber(video_path, audio_path, offset_seconds,
  model_label, enable_diarization)` → `{ok, job_id}` — a normal
  "transcribe" job (per-kind throttle applies) whose params ALSO carry
  `{audio_path, offset_seconds}`. **No proxy is rendered anywhere.**
- `sync_export_xml(payload)` → save dialog (`"Premiere XML (*.xml)"`,
  default `<video stem> synced.xml`) → `{ok, path}`. payload =
  `{video: {path, probe}, tracks: [{path, offset_seconds, probe}],
  include_camera_audio: bool, sequence_name: str}` (probes come from the
  sync job result / sync_probe — the frontend passes them through so the
  export needs no re-probing). Built in-process by new
  `backend/sync_xml.py` (pure stdlib).

## backend/sync_xml.py — non-merged XMEML (the user's core requirement)
`build_sync_xml(video, tracks, include_camera_audio, sequence_name) -> str`
producing xmeml v5, following the conventions of the two existing builders
(READ `../B-Roll Analyzer/xml_export.py` for timebase/ntsc handling and
pathurl encoding, and `../Rough Cut Studio/backend/xml_builder.py` for
linked-clip structure):
- Sequence: timebase/ntsc from the video's fps (same rounding rules as the
  b-roll exporter), duration = video duration, dimensions from probe
  width/height.
- V1: ONE video clipitem for the full video file, start 0. Its `<file>`
  references the ORIGINAL video path (file URL-encoded pathurl).
- Camera audio (only when include_camera_audio): one clipitem PER SOURCE
  CHANNEL (the suite-wide rule: never a single channelcount=2 item),
  linked to the video clipitem via `<link>` groups, on tracks A1..An.
- Each external audio file: one clipitem PER CHANNEL on its own subsequent
  track(s), referencing the ORIGINAL audio file path — **no merged media,
  no rendered file**. Placement: `start = round(offset_seconds * fps)`,
  `in = 0`; negative offset → `start = 0`, `in = round(-offset_seconds *
  fps)` (head trimmed); `end = min(sequence_end, start + remaining
  duration)`; clips whose audible range falls entirely outside [0,
  sequence_end] are dropped with a warning in the return. Channels of the
  same file link to each other via `<link>` (one master clip per file),
  but external files do NOT link to the video clip — separate masters,
  exactly "non-merged".
- Every source audio channel gets `<sourcetrack><trackindex>` pinned to
  its channel index, samplerate/depth from probe.
- Return also `{"warnings": [...]}` alongside the XML string
  (`build_sync_xml` returns `(xml_str, warnings)`), surfaced in the UI
  toast after save.

## transcribe_worker.py change — proxy-free synced transcription
Params gain OPTIONAL `{audio_path: str, offset_seconds: float}`:
- When `audio_path` present: `extract_audio(audio_path, tmp_wav)` (the
  existing ffmpeg -vn call works on pure audio files) instead of the video.
- After merge/normalize: shift every segment by `offset_seconds`
  (`start += o; end += o` — maps audio-domain times onto the VIDEO
  timeline per the v3 sign convention), then drop segments with
  `end <= 0` and clamp `start = max(0.0, start)`.
- The cache is still written next to the VIDEO (`video_path` + suffix,
  fingerprint of the video file) so the transcript aligns with the video
  everywhere downstream (editor, send-to-edit VTT `NOTE Source video:` =
  the video). Add a top-level `"audio_source": audio_path` key to the
  cache JSON (extra keys are ignored by the standalone app's loader —
  verified it reads only specific fields). Job result data also carries
  `audio_source` and `offset_seconds`.
- `--selfcheck` unchanged.

## Frontend — Sync workspace
New FIRST tab "Sync" (order: Sync | Transcribe | B-Roll | Graphics | Edit;
default workspace stays Transcribe). `#workspace-sync`, two-column like the
others:
- Left rail: Video row (pick + probe info line: duration · fps · WxH ·
  audio format · timecode tag if any), Audio list (add/remove, per-file
  format label from probe), Method select ("Waveform analysis (default)" |
  "Embedded timecode (BWF/camera TC)"), "Detect Sync" → sync_start (toast +
  jobs drawer; on completion render results into the workspace, and
  sync_save_offsets the detected values).
- Results panel: per-track rows — filename, format label, offset readout
  formatted `+1.234 s (+1234 ms)` (3-decimal s / whole ms, matching
  A-Sync's own display), nudge buttons `-100ms -10ms +10ms +100ms` plus a
  direct ms-precision input, per-track error state. Any change →
  debounced sync_save_offsets + a `sync` SuiteUndo domain entry.
  On picking a video that has a `<video>.sync-offsets.json`, offer
  "Load saved offsets" (sync_load_offsets).
- Actions per track: "Transcribe this track (no proxy)" → small inline
  confirm strip reusing the Transcribe workspace's current model +
  diarization settings → sync_send_to_transcriber → toast + drawer (the
  finished job appears in the Transcribe workspace like any other, editor
  and Send-to-Edit included — timestamps already on the video timeline).
- Footer actions: "Include camera audio" checkbox +
  "Export Premiere XML…" → sync_export_xml with the CURRENT (possibly
  nudged) offsets and the probes from the job/sidecar; toast the saved
  path + any warnings.
- Jobs drawer: kind "sync" gets a card like the others (label = video
  basename, progress per audio file).

## Verification (both agents; backend also extends --selftest)
- Backend: `ASYNC_PYTHON backend/workers/sync_worker.py --selfcheck` →
  WORKER OK. Real-media e2e in the scratchpad (guard __main__): generate
  test media with ffmpeg (a 10s 1280x720 testsrc video with a 1kHz beep
  pattern burned into its audio track, and a WAV of the same beep pattern
  offset by a KNOWN amount, e.g. audio recorded 1.5s "early" so expected
  offset = +1.5) → run the detect worker → assert |offset − expected| ≤
  0.05s; probe mode returns width/height/fps; build_sync_xml output parses
  with xml.etree, has exactly one video clipitem + per-channel audio
  clipitems on separate tracks + correct start/in frames for a positive
  AND a negative offset case + distinct file pathurls (no merged
  references); transcribe shift logic unit-tested (fake segments through
  the shift/drop/clamp helper — expose it as a pure function in the worker
  for testability). Selftest additions: sync_load_offsets on missing file
  → found:false; sync_xml builder round-trip on synthetic probes (no
  ffmpeg needed).
- Frontend: stub-page screenshots of the Sync workspace (pickers + probe
  lines, per-track offsets with nudges, method select, export row);
  exercise a nudge (assert offset text + save call) and undo; assert
  sync_send_to_transcriber called with the CURRENT nudged offset.

---

# Contract Addendum v4 — Sync preview + audio routing (2026-07-13)

Two features for the Sync workspace. Same rules as before (dialog labels
match `^[\w ]+$`; error contract; RCS files untouched).

## Routing model (per synced audio track) — the shared data shape

Each track in the Sync workspace and in the `<video>.sync-offsets.json`
sidecar gains TWO optional fields (backward compatible — absent means the
defaults, so old sidecars keep working):

- `enabled`: bool, default **true**. A disabled track is excluded from the
  synced preview, the XML export (Sync workspace AND the Edit-workspace
  splice), and is not offered for transcription.
- `channels`: list[int] of 1-based SOURCE channel indices to include,
  default **null = all channels**. E.g. a 2-ch recorder where only the lav
  matters → `[1]`. An empty list is treated as "all" (never emit zero).

Sidecar schema (extends v3):
```
{video_path, method, updated_at,
 tracks: [{path, offset_seconds, enabled: bool, channels: [int]|null}, ...]}
```

`SuiteApi.sync_save_offsets(video_path, tracks, method)` must persist
`enabled`/`channels` when present on the incoming track dicts (default
true / null when absent). `sync_load_offsets` returns them as-is.

## A. sync_preview_url(path) → {ok, url} | {ok:false,error}

Like `broll_preview_url` but accepts BOTH video containers
(PREVIEW_VIDEO_EXTENSIONS) and the sync audio extensions
(.wav .aif .aiff .mp3 .m4a .flac .caf). Serves via
`self.preview_server.url_for(path)` (the inherited RCS server already does
mimetype + byte-range, verified — works for `<audio>` and `<video>`).
Real-file + extension gate only; nothing else reaches the server.

## B. Frontend — synced media preview (proxy-free, browser-mixed)

In the Sync results panel, a "Preview Sync" player:
- One MUTED `<video>` element = the picture (src = sync_preview_url(video)).
- One `<audio>` element per ENABLED track (src = sync_preview_url(track)).
- A single transport (Play/Pause + a scrubber showing video time). On each
  tick (rAF or `timeupdate`), keep every audio element locked to the
  picture using the v3 sign convention: `audio.currentTime =
  video.currentTime - offset` (per track). When `video.currentTime <
  offset` (audio hasn't started yet) pause/silence that track; resume when
  it passes. Re-lock on seek and whenever an offset is nudged (live).
- Per-track preview controls in each row: a Mute and a Solo toggle
  (preview-only; solo = mute all others). Disabled tracks don't play.
- Tolerance: only hard-correct an audio element when it drifts > ~40 ms
  from its target (avoids constant re-seek stutter — mirror the standalone
  A-Sync MixPlayer's "seek once, then let it run" lesson).
- Nothing is rendered or written to disk; this is pure client-side sync of
  the original files over the local preview server. Stop all elements when
  leaving the workspace or closing the player. One player at a time.

## C. Frontend — routing UI (per track row)

Each track row (already shows filename, format, offset, nudges) gains:
- An **enable** checkbox (default on). Off → row dims; excluded everywhere.
- A **channel selection** control, populated from the track's probed
  `audio_channels`: "All channels" (default), "Channel 1", "Channel 2", …
  up to N; for N≥2 also "Downmix to mono". Selection maps to the sidecar
  `channels` field: All→null, "Channel k"→[k], "Downmix to mono"→a special
  marker — represent downmix as `channels: [0]` (0 = "all channels summed
  to one"), which the exporters interpret as a single mono clipitem summing
  the source (sourcetrack omitted / trackindex 0 meaning the whole file).
  Keep it simple: if downmix is hard to express in XMEML cleanly, treat
  "Downmix to mono" as `[1]` fallback and note it — but PREFER emitting a
  single clipitem with no `<sourcetrack>` (Premiere then uses the file's
  full mixdown). Decide from what imports cleanly; document the choice.
- Any routing change: update the row, debounce-persist via
  `sync_save_offsets` (same ~500ms debounce as offset nudges), push a
  "sync" SuiteUndo entry, and if the preview is playing, add/remove/mute
  the corresponding audio element live.
- "Transcribe this track" is hidden/disabled for a disabled track; when a
  single channel is selected it passes that channel to the backend (below).

## D. Exports honor routing

- `sync_xml.build_sync_xml`: skip tracks with `enabled == false`; for an
  enabled track emit clipitems ONLY for the selected `channels` (null =
  all). File-level `<channelcount>` stays the SOURCE file's real count
  (Premiere needs it to resolve `<sourcetrack>`); only the emitted
  clipitems are filtered. The `tracks` dicts passed to build_sync_xml gain
  optional `enabled`/`channels`; the frontend forwards them in the
  sync_export_xml payload.
- `synced_audio_splice`: `discover_synced_audios` reads `enabled`/`channels`
  from the sidecar and returns them per track; the splice skips disabled
  tracks and emits only the selected channels (default all). File-level
  channelcount unchanged.
- Downmix marker handling must be identical in both builders.

## E. Transcription honors channel selection (optional single channel)

- `sync_send_to_transcriber(video_path, audio_path, offset_seconds,
  model_label, enable_diarization, channel=None)` — new optional `channel`
  (1-based source channel, or None for the current whole-file mono
  downmix). Passed through to the transcribe job params as `audio_channel`.
- `transcribe_worker`: when `audio_channel` is set, extract that one source
  channel to the mono WAV (ffmpeg `-af "pan=mono|c0=c{channel-1}"` on the
  existing extract step) instead of the default `-ac 1` downmix. When
  None/absent, behavior is exactly as today. Everything else (offset shift,
  cache) unchanged.

## F. Verification

- Backend: `sync_preview_url` accepts a real .wav and a real .mp4, rejects a
  .txt; sidecar round-trip preserves enabled/channels; build_sync_xml with
  a disabled track omits it entirely, with `channels=[1]` on a stereo file
  emits exactly ONE clipitem (sourcetrack 1) while file channelcount stays
  2; the splice honors the same via a stubbed sidecar; transcribe_worker
  channel-extract path unit-tested (ffmpeg arg construction, no real
  decode); `main.py --selftest` still green; py_compile.
- Frontend: `node --check`; stub-page screenshots of the routing controls
  (enable + channel select) and the preview player with per-track
  mute/solo; assert a channel-selection change persists via
  sync_save_offsets with the right `channels` value and that a disabled
  track is dropped from the export payload; the preview player wiring
  (audio elements created per enabled track, offset lock math) exercised
  with a mocked media element (real media playback is suppressed in the
  stub pane — assert currentTime targets, same allowance as prior rounds).
- Neither agent modifies RCS or the sibling apps; both work only in the
  All-In-One Studio Suite copy.

---

# Contract Addendum v5 — transcript editor video reference + durable audio/video linking (2026-07-13)

Two features for the Transcribe workspace. RCS files untouched. Dialog-label
rule (`^[\w ]+$`) still applies to any new dialog descriptions (none needed
here).

## Background facts (verified, binding)

- RCS's `self.media_paths[source_id]` holds exactly ONE path (str) — there is
  no way for RCS's own data model to link two media files to one source.
  `detect_linked_media` (RCS transcript_parser.py) returns `Optional[str]`
  via `_SOURCE_VIDEO_RE = re.compile(r"source\s*video\s*:\s*(.+)", re.I)`,
  matched against the FIRST such line anywhere in the transcript content.
- The transcript editor (`#tEditor`, suite.js `openTranscriptEditor`/
  `renderTranscriptEditor`) has NO video/audio element today — text/speaker
  controls only. `S.tEd.videoPath` is known at render time but unused for
  playback. `renderTranscriptEditor` rebuilds `#tEditor`'s `innerHTML`
  wholesale on most edits — any `<video>` placed inside that region would be
  destroyed and recreated (losing playback state) on every keystroke-driven
  re-render. **A persistent player must live OUTSIDE that innerHTML churn**,
  exactly like `#syPlayer` already does for the Sync workspace.
- `synced_audio_splice.discover_synced_audios(video_path)` today reads TWO
  sources of truth: the `<video>.sync-offsets.json` sidecar (primary) and the
  video's `.ivt-cache.json` `audio_source`/`sync_offset_seconds` (fallback).
  Both require a file sitting next to the video — if the transcript (.vtt)
  is moved/shared without those, the audio association is silently lost even
  though the transcript itself still references the video correctly.
- `handoff.build_transcript_vtt(video_path, segments)` and
  `send_transcript_to_edit(api, video_path, segments)` today embed ONLY
  `NOTE Source video: {video_path}` — never anything about audio, and never
  consult `synced_audio_splice` at all.

## What "linked" means given RCS's one-path constraint

The VIDEO is (and stays) the source's one linked `media_path`, unchanged.
"Both audio and video linked" is achieved the same way the Sync workspace's
own XML export already achieves it: the AUDIO is carried into the FINAL
Premiere XML as a separate, non-merged, offset-aligned track via the
existing `synced_audio_splice` mechanism in `SuiteApi.save_xml`. The gap
being closed here is durability: that mechanism must keep working even when
the sidecar/cache aren't present, by teaching it to read the association
back out of the TRANSCRIPT FILE ITSELF (which now embeds it), and by making
EVERY "Send to Edit" (not just the Sync workspace's own flow) embed that
note whenever the underlying video has ANY known synced audio.

## A. Durable audio note in the exported transcript VTT

**NOTE line format** (one per synced audio track, order-independent,
collision-proof against RCS's own `_SOURCE_VIDEO_RE` since neither line
contains "video"):
```
NOTE Source audio: /abs/path/to/audio.wav (offset +1.500s)
NOTE Source audio: /abs/path/to/boom.wav (offset -0.420s)
```
Regex to both read this back: `re.compile(r"source\s*audio\s*:\s*(.+?)\s*\(offset\s*([+-]?[0-9.]+)\s*s\)", re.IGNORECASE)` — path is the non-greedy group before ` (offset `, offset is the signed float. Emit with exactly 3 decimal places (`f"{offset:+.3f}s"`) to round-trip cleanly.

- `backend/synced_audio_splice.py`: add
  `parse_audio_notes_from_transcript(transcript_path) -> list[{"audio_path", "offset_seconds"}]`
  — reads the file (best-effort, `errors="replace"`, returns `[]` on any
  failure/missing file), applies the regex above via `finditer` (supports
  multiple lines), and returns only entries whose `audio_path` exists on
  disk (`os.path.isfile`), abspath-deduped.
- `discover_synced_audios(video_path, transcript_path=None)` gains the
  optional second param. Merge order (first source wins per unique abspath,
  matching the existing sidecar-primary docstring convention): sidecar →
  cache fallback → **transcript-note fallback** (only used for a path not
  already found by the first two). Entries found ONLY via the transcript
  note default `enabled=True, channels=None` (routing isn't recorded in the
  note — this is a durability fallback, not a full routing store) and get
  `channel_count` via the same `_probe_channels` helper.
- `splice_external_audio(xml_string, resolved_segments, media_paths, fps, discover_fn=..., source_paths=None)`
  gains optional `source_paths: dict[source_id -> transcript_path]`. For each
  main cut, call `discover_fn(media_paths.get(source_id), source_paths.get(source_id) if source_paths else None)`
  — `discover_fn`'s signature must accept the optional second positional arg
  (update the default `discover_synced_audios` call site accordingly; the
  existing `discover_synced_audio` back-compat singular wrapper keeps its
  original one-arg signature unchanged for any other caller).
- `SuiteApi.save_xml` passes `source_paths = {sid: (self.sources.get(sid) or {}).get("path") for sid in ...}`
  built from `self.sources` (inherited RCS state — `sources[source_id]["path"]`
  is the ingested transcript's own file path) alongside the existing
  `media_paths` dict, so discovery can fall back to the transcript's own
  embedded note when no sidecar/cache exists.

## B. Every "Send to Edit" embeds the note (not just the Sync workspace's own export)

- `handoff.build_transcript_vtt(video_path, segments, audio_refs=None)` —
  new optional third param, `audio_refs: list[{"path","offset_seconds"}]`.
  When given, append one `NOTE Source audio: ...` line per ref (format
  above) immediately after the existing `NOTE Source video:` line, before
  the blank line that precedes cues.
- `handoff.send_transcript_to_edit(api, video_path, segments)` — before
  building content, call
  `synced_audio_splice.discover_synced_audios(video_path)` (sidecar/cache
  only at this point — the transcript doesn't exist yet) and pass any
  results as `audio_refs` to `build_transcript_vtt`. This means: syncing a
  video in the Sync workspace, then later doing a perfectly ordinary "Send
  to Edit" from the TRANSCRIBE workspace (not the Sync tab), still embeds
  the audio note automatically — the two workspaces no longer need to be
  used in the same session for the linkage to survive.
- No change needed to `transcriber_send_to_edit`/`transcriber_send_cache_to_edit`
  in suite_api.py — they already funnel through `handoff.send_transcript_to_edit`.

## C. Transcript editor video reference (frontend)

New STATIC player, sibling of `#tEditor` (not inside its churned innerHTML),
modeled directly on `#syPlayer`:
```html
<div class="suite-tedplayer" id="tEdPlayer" hidden>
  <div class="suite-tedplayer__stage"><video id="tEdPlayerVideo" playsinline></video></div>
  <div class="suite-tedplayer__transport">
    <button id="tEdPlayerPlay">▶</button>
    <input type="range" id="tEdPlayerScrub" min="0" max="1" step="0.001" value="0" />
    <span id="tEdPlayerTc">0:00 / 0:00</span>
  </div>
</div>
```
(NOT muted — this is the only audio source when no sync track exists; it's
the plain reference video, same as double-clicking the file.)

- A toggle button in the transcript-editor header, "Show Video" / "Hide
  Video" (next to Undo/Redo, before Save/Send) — hidden entirely when
  `S.tEd.videoPath` is falsy or the file isn't a supported video container
  (reuse the same extension check `broll_preview_url` already enforces;
  don't add a new backend method — `broll_preview_url(videoPath)` already
  validates+serves any `PREVIEW_VIDEO_EXTENSIONS` file generically, not
  literally b-roll-specific, and this workspace already has `call(...)`
  wired).
- Opening: fetch the URL once (cache on `S.tEd`), set `<video>` src, show
  the player, wire Play/Pause + scrubber exactly like the Sync player's
  `toggleSyncPlay`/`syncPlayerSeek` (no need for the multi-track offset-lock
  machinery — this is a single plain video, not a synced mix).
- Per-segment "jump to time" control: a small ▶ button added to each
  segment row (`tEdSegmentRow`) — clicking it opens the player if not
  already open, seeks to `seg.start`, and plays. Delegate via the editor's
  existing delegated click handler (`wireTranscriptEditor`) using `data-i`,
  matching the established pattern for other per-row actions in this editor.
- Closing/teardown: stop and detach the video when the editor is closed
  (Back button), when switching to a different transcript/job, and when
  leaving the Transcribe workspace (hook into the existing workspace-switch
  teardown list alongside `stopBrollPreview`/`teardownSyncPlayer`).
- Style: match `.suite-sync-player` exactly (same tokens, same compact
  transport bar), new class `.suite-tedplayer` scoped rules only.

## D. Verification

- Backend: real-media test — generate a video+audio pair, run the sync
  detect flow's data (sidecar), call `handoff.send_transcript_to_edit`,
  assert the written VTT contains a well-formed `NOTE Source audio: ... (offset ...)`
  line; then DELETE the sidecar and cache, re-`discover_synced_audios` by
  passing ONLY the transcript path (no sidecar/cache) and assert the audio
  is still found via the note fallback with the correct offset; run
  `splice_external_audio` with `source_paths` supplied and confirm the
  external audio still appears in the spliced XML even with no sidecar
  present. Also: a plain (never-synced) video's VTT must NOT gain any audio
  note (no regression to the ordinary path), and `main.py --selftest` gains
  a synthetic-data assertion for the regex parse function. py_compile.
- Frontend: `node --check`; stub-page screenshot of the transcript editor
  with the video player open, a jump-to-segment click seeking+playing the
  video (assert via currentTime target, same allowance as prior rounds for
  suppressed media playback in the test pane), and confirm the player
  survives a segment TEXT edit re-render (video's `currentTime`/`src` must
  NOT reset — this is the whole point of keeping it outside the innerHTML
  region) and is torn down when leaving the workspace or closing the
  editor. Zero console errors. Regression screenshot of B-Roll/Sync/Graphics
  workspaces (shell.html changed).

# Contract Addendum v6 — Favorite transcript lines + Favorites tab (2026-07-13)

## Scope

Three asks, all inside RCS's Edit workspace: (1) favorite a line in a
transcript, (2) a "Favorites" tab beside Script/Cuts/Export/History listing
every favorited line with a way to push it into Cuts, (3) a visible marker
on a favorited line's row in the Cuts tab. Per `CONTRACT.md`'s standing
rule, RCS's own files (`index.html`/`style.css`/`app.js`/`api.py`/
`sources.py`) are NOT modified — everything here is `suite.js`/`suite.css`
DOM overlay + new `SuiteApi` methods, following the exact precedents
`insertBrollCuts`/`injectTranscriptSearch`/`injectSuiteTimeline` already
established (see `suite.js` around those names).

"Transcript" here means RCS's own transcript-viewer modal
(`viewTranscript`/`#transcriptModalBody`, opened from a source row inside
the Edit workspace) — not the separate pre-ingestion editor in the
Transcribe workspace (`S.tEd`). This keeps the whole feature in one
identity space: RCS's own `Segment` objects (`index`, `start_seconds`,
`end_seconds`, `start_tc`, `end_tc`, `speaker`, `text`), keyed by
`source_id` (== the backing VTT's filename stem).

## Identity & persistence: why `vtt_path`, not just `source_id`

`self.sources` is rebuilt empty every launch and only repopulated when a
transcript is actually sent/loaded into Edit this session — `source_id` on
its own would make a favorite silently unresolvable in any session where
that transcript hasn't been (re)opened yet. But the generated VTT file
itself is permanent (`assets/transcripts/<stem>.vtt`, never deleted). So
each favorite stores the **VTT path**, from which `source_id` is always
re-derivable (`os.path.splitext(os.path.basename(vtt_path))[0]`, the same
rule `handoff.py` already uses), and "add to Cuts" lazily re-ingests that
VTT (`handoff._ingest_vtt`) if the source isn't currently loaded — no new
resolution mechanism, reusing what `unique_vtt_path`/`_ingest_vtt` already
do for B-roll/Brander sources.

## A. Storage — `backend/favorites.py` + `assets/favorites.json`

`paths.py` gains `FAVORITES_FILE = os.path.join(ASSETS_DIR, "favorites.json")`.

One JSON array, each entry:
```json
{
  "id": "f_9c2a1b7e",
  "vtt_path": "/abs/path/assets/transcripts/Interview 1.vtt",
  "source_id": "Interview 1",
  "index": 4,
  "start_seconds": 12.4, "end_seconds": 15.9,
  "start_tc": "00:00:12:10", "end_tc": "00:00:15:22",
  "speaker": "Jordan", "text": "...",
  "created_at": "2026-07-13T18:04:00"
}
```
`source_id`/`start_tc`/`end_tc`/`speaker`/`text` are denormalized display
copies captured at favorite-time (so the Favorites tab renders without
needing the source loaded); `start_seconds`/`end_seconds` are what
"Add to Cuts" actually uses (recomputed timecodes at the project's
*current* fps via `handoff.build_cut_spec`, same as every other handoff
path — never trust a stored `*_tc` string as authoritative for a new cut).

`favorites.py`: `load()`/`save(list)` (plain JSON read/write, `[]` on any
read failure — same fail-open policy as every other sidecar in this
project), `new_id()` (`"f_" + uuid4().hex[:8]`).

## B. SuiteApi additions (namespaced `suite_favorite_*` + one list method)

- `suite_list_favorites()` -> `{"ok": True, "favorites": [...]}`, newest
  (`created_at`) first.
- `suite_toggle_favorite(source_id, index)`: requires `source_id` to be a
  currently loaded source (`self.sources.get(source_id)`) — favoriting only
  happens from an already-open transcript modal, so this is never the lazy
  path. Finds the `Segment` with that `.index`; if a favorite already
  exists for `(vtt_path, index)` removes it (toggle off), else appends a
  new entry built from the segment's own fields. Persists on every call
  (no explicit save step, matching `.sync-offsets.json`'s always-on-write
  policy). Returns `{"ok": True, "favorited": bool, "favorite": {...} | None}`.
- `suite_remove_favorite(favorite_id)`: removes by `id`, persists, returns
  `{"ok": True}` (idempotent — removing a missing id is not an error).
- `suite_favorite_add_to_cuts(favorite_id)`: looks up the favorite; if its
  `source_id` isn't in `self.sources`, re-ingests `vtt_path` via
  `handoff._ingest_vtt` when the file still exists on disk, else returns
  `{"ok": False, "error": "..."}` (transcript file was moved/deleted).
  Then `handoff.build_cut_spec(self, source_id, start_seconds, end_seconds,
  track="main")` — reusing the existing helper verbatim (it already takes
  a `track` argument; every prior caller just happened to pass `"broll"`).
  Returns `{"ok": True, "cut": {...CutSpec}}` for the frontend to insert.

## C. Frontend — favorite star in the transcript-viewer modal

`suite.js` already has a `MutationObserver` on `#transcriptModalBody`
(`injectTranscriptSearch`, resets the search box whenever RCS repopulates
the table for a newly opened transcript). A second, adjacent observer
callback (same target/options — `{childList: true}` — not a second
`observe()` call architecturally required, but keep the concerns separate:
one function resets search state, a new one injects/refreshes star
buttons) runs after every repopulation:

- For each `<tr>`, append a star toggle into the existing
  `.transcript-table__add-cell` (sibling of the "+ Add" button — no new
  `<td>`/header column needed): `☆`/`★`, `data-tact="fav-toggle"
  data-idx="${seg.index}"`, class `is-fav` when
  `S.favorites.some(f => f.source_id === state.transcriptModalSourceId &&
  f.index === seg.index)`.
- A delegated click listener on `#transcriptModalBody` (coexists fine with
  RCS's own listener on the same element — different `closest()` target,
  `data-tact` vs RCS's `data-act`) calls `suite_toggle_favorite(sourceId,
  idx)`, patches `S.favorites` locally (push/splice — no refetch), flips
  that one button's glyph/class in place, and — since the same line may
  also be sitting in the Cuts tab already — calls
  `refreshCutsRowFavoriteMarkers()` (part D) so the two views never
  disagree.
- `S.favorites` is loaded once at `boot()` via `suite_list_favorites` and
  kept as the single in-memory source of truth for all three UI surfaces
  (modal stars, Cuts markers, Favorites tab) — every mutating call updates
  it locally rather than refetching, matching this codebase's existing
  bias against unnecessary round-trips (`syncPlayerLock`'s throttle,
  `SuiteUndo`'s bounded local stacks).

## D. Frontend — Cuts-tab favorite marker

Cuts rows don't carry `index`/segment identity once inserted (only
`source_id` + `in_tc`/`out_tc`), so a row "is favorited" is matched by
`(source_id, in_tc, out_tc)` against `S.favorites` — good enough since two
distinct favorited lines never share an identical in/out on the same
source. `refreshCutsRowFavoriteMarkers()` walks `#editTableBody tr`,
reading each row's source/in/out cells (same cells `injectSuiteTimeline`'s
observer already reads to place timeline blocks), toggling a small
`.suite-cut-fav` badge/star it injects once per row (idempotent — skip a
row that already has one) into the row's existing note/actions cell.
Called: after the boot-time favorites load, after any toggle (C), and from
the *existing* `#editTableBody` MutationObserver callback
(`injectSuiteTimeline`, `suite.js` ~3216-3227) so it re-runs whenever RCS
itself rewrites the table (Apply, undo/redo, add/delete row) — no new
observer, one more call added to that existing callback.

## E. Frontend — injected 5th "Favorites" tab

Per the tab-bar research: `activateTab()` re-queries `.tab`/`.tab-panel`
live on every call, so a tab injected anywhere in the DOM works with zero
changes to RCS's own switching logic — only the injected button's own
click listener must be wired manually (the once-at-parse-time listener
loop in `app.js` won't see a node added after it ran).

At `boot()`, once (idempotency-guarded like `injectTranscriptSearch`):
```js
document.querySelector(".tabs").insertAdjacentHTML("beforeend",
  `<button class="tab" id="tabBtnFavorites" data-tab="favorites" role="tab"
    aria-selected="false" aria-controls="tabFavorites">Favorites</button>`);
document.getElementById("tabHistory").insertAdjacentHTML("afterend",
  `<div class="tab-panel" id="tabFavorites" role="tabpanel"
    aria-labelledby="tabBtnFavorites"><div class="suite-fav-list" id="suiteFavList"></div></div>`);
document.getElementById("tabBtnFavorites").addEventListener("click", () => {
  activateTab("favorites");
  renderFavoritesPanel();
});
```
`renderFavoritesPanel()` renders `S.favorites` (newest first) into
`#suiteFavList`: source name, `start_tc`–`end_tc`, speaker, text, an
"Add to Cuts" button, and a remove (★) button. Empty state: "No favorites
yet — star a line in a transcript to see it here."

- **Add to Cuts** (`data-tact="fav-add-cuts" data-fid="${id}"`): calls
  `suite_favorite_add_to_cuts(id)`; on success, mirrors `insertBrollCuts`'s
  mandated mutation order (`suite.js` ~2988-3000) but for a single
  main-track cut, matching RCS's own transcript-modal "+ Add" shape
  exactly (`track:"main"`, `source_text` filled with the favorite's text,
  no `timeline_start_tc` key — main cuts don't carry one until Apply):
  `flushTcEditSnapshot()` -> `readEditTableIntoState()` ->
  `pushUndoSnapshot()` -> `state.editSegments.push(seg)` ->
  `appendCutRow(seg)` -> `activateTab("edit")` (jump to Cuts so the user
  sees it landed) -> `refreshCutsRowFavoriteMarkers()`. Guarded by the same
  `rcsEditReady()` typeof-checks as every other externally-driven Cuts
  mutation; degrade to a toast if RCS's structure isn't ready.
- **Remove** (`data-tact="fav-remove" data-fid="${id}"`): calls
  `suite_remove_favorite(id)`, splices `S.favorites` locally, re-renders
  the panel and calls `refreshCutsRowFavoriteMarkers()`/the modal star
  refresh if a transcript modal happens to be open.

## F. Styling

`.suite-fav-btn` (star buttons): small, no background, `--text-faint`
default / `--amber` when `.is-fav` — matches the existing `.row-btn`
sizing so it sits flush next to RCS's own "+ Add" button. `.suite-fav-list`
/ `.suite-fav-card` reuse the suite's existing card/list tokens (same
family as `.suite-clip__stage` cards) rather than inventing new visual
language. `.suite-cut-fav` Cuts-row badge: an inline star matching the
modal's, no layout shift (row height must not change — inject into
existing cell padding, not a new column).

## G. Verification

- Backend: `py_compile` all changed files; a synthetic test — register a
  fake source in `self.sources` with 2 segments, `suite_toggle_favorite`
  one on then off (assert file round-trips to `[]`), toggle one on and
  call `suite_favorite_add_to_cuts` twice (once with the source still
  loaded, once after manually clearing `self.sources` to force the
  re-ingest path) and assert both return a well-formed CutSpec with
  `track:"main"`. `main.py --selftest` gains this as a real assertion, not
  just a manual script.
- Frontend: `node --check`; browser-harness test against the composed page
  with a stubbed `pywebview.api` — open the transcript modal, click a
  star, confirm it flips and `#tabBtnFavorites`'s panel lists it; click
  "Add to Cuts", confirm a new row appears in `#editTableBody` with the
  right source/timecodes and the tab switches to Cuts; confirm the new
  Cuts row shows the favorite badge; unfavorite from the Favorites tab and
  confirm the Cuts badge and modal star both clear. Zero console errors.
  `main.py --selftest` still passes (placeholder/composition regression).
  RCS's own files unchanged (mtime check). Sync both `Studio Suite`
  copies.

# Contract Addendum v7 — Cuts/preview favorite star + narrower Audio column (2026-07-13)

## Why the matching key has to change

v6's favoriting only worked from the transcript-viewer modal, where every
row is a parsed `Segment` with a real `.index`. The user now wants to
favorite directly from a **Cuts row** or the **preview window** — but a
Cuts row's in/out may not correspond to any parsed segment at all (a
manually added row, an edited timecode, a B-roll clip cut from a synthetic
single-cue VTT). There is no `index` to key on in those places.

Fix: favorites are now matched/deduped by **time range** —
`(vtt_path, start_seconds, end_seconds)`, tolerance 0.05s — instead of
`(vtt_path, index)`. A transcript-modal favorite still stores its
segment's `index` for reference, but matching never depends on it. This is
strictly more permissive than before (every old index-keyed favorite has a
`start_seconds`/`end_seconds` already, so nothing is lost), and now
anything with a `source_id` + a time range can be favorited.

## A. `backend/favorites.py`

- `find(favorites, vtt_path, start_seconds, end_seconds, tol=0.05)` —
  replaces the old index-based `find`; matches on abspath'd `vtt_path` and
  both endpoints within `tol` seconds.
- `build(vtt_path, source_id, start_seconds, end_seconds, start_tc="",
  end_tc="", speaker="", text="", index=None)` — flat constructor,
  `index` now optional (`None` for a favorite made outside the transcript
  modal).

## B. `SuiteApi` additions/changes

- `suite_toggle_favorite(source_id, index)` (transcript modal, unchanged
  signature): now looks up the segment, then delegates to a shared
  `_toggle_favorite_range(...)` helper — same external behavior, new
  internal matcher.
- `suite_toggle_favorite_range(source_id, start_seconds, end_seconds,
  text="", speaker="")` (new — Cuts row / preview window): `source_id`
  must be a currently loaded source (true by construction for both
  callers — a Cuts row's source dropdown and a previewed cut both only
  ever reference a live `self.sources` entry). Computes `start_tc`/`end_tc`
  via the inherited `format_timecode` and delegates to the same
  `_toggle_favorite_range` helper.
- `_toggle_favorite_range(vtt_path, source_id, start_seconds, end_seconds,
  start_tc, end_tc, speaker, text, index)`: the shared toggle body both
  public methods above call — looks up an existing favorite via the new
  range-based `favorites.find`, removes it if found (toggle off), else
  builds and appends one (toggle on). Persists on every call, same as v6.

## C. Frontend — clickable star on every Cuts row

`refreshCutsRowFavoriteMarkers()` (v6) only ever rendered a **passive**
`<span>` badge when a row happened to already match a favorite made
elsewhere. It's now a real `<button class="row-btn suite-fav-btn
suite-cut-fav-btn">` injected once per row into `td:last-child`'s
`.reorder-cell__inner` (alongside RCS's own dup/delete buttons), glyph/
class kept in sync with `S.favorites` exactly as before — the only change
is it's now clickable.

A delegated click listener on `#editTableBody` (wired once at boot,
idempotency-guarded the same way `injectTranscriptFavoriteStars` guards
its own listener): reads the row's `source_id`/`in_tc`/`out_tc` straight
from its live `<select>`/`<input>` values (same cells the badge-matching
already reads), converts `in_tc`/`out_tc` to seconds with the existing
`suiteTcToSeconds(tc, fps)` helper, reads `.script-text-cell`'s text as
the favorite's display text, and calls `suite_toggle_favorite_range`.
Updates `S.favorites` locally (push the returned favorite, or filter it
out by the same `source_id`+`in_tc`+`out_tc` triple on unfavorite — the
same pattern the transcript-modal handler already uses with
`source_id`+`index`), then refreshes all three surfaces: this row's own
button, the transcript-modal stars (in case the same line is open there
too), and the Favorites tab panel.

## D. Frontend — favorite star in the preview window

`#previewPlayer`'s header (`.preview-player__header`, flex
`justify-content: space-between` between `.preview-player__title` and
`#btnClosePreview`) gets a star button inserted between the two, once at
boot (idempotency-guarded).

RCS's `populatePreviewInfo(seg, ...)` (the function that fills in
`#previewSource`/`#previewInOut`/`#previewTrack` every time the previewed
cut changes — including automatically, mid-queue, during "▶ Preview
Script") is not hooked directly; instead a `MutationObserver` on
`#previewSource` (`{childList: true, characterData: true, subtree:
true}` — `.textContent =` replaces its one text node, a childList
mutation on the element) re-evaluates the star's favorited state every
time RCS itself changes which cut is being previewed. Same
`rcsEditReady()`-style typeof-guards on `state.editSegments`/
`state.previewingCid` as everywhere else in this file.

Click handler: resolves the current segment via
`state.editSegments.find(s => s._cid === state.previewingCid)` (the same
lookup RCS's own `previewNote`/`btnSetIn`/`btnSetOut` handlers use),
reads its `source_id`/`in_tc`/`out_tc`/`source_text`, and calls
`suite_toggle_favorite_range` exactly like the Cuts-row star — no new
backend surface needed, both stars share one endpoint.

## E. Narrower "B-Roll Audio" column

RCS's OWN `index.html` already ships an explicit `<colgroup>` for
`.edit-table` (13 `<col style="width: ...">` entries, one per header:
select/reorder/preview/track/source/in/out/timeline-start/**audio**/
script-text/note/on-screen-text/actions) — a fact this addendum initially
missed. A `<colgroup>` is authoritative over any per-cell width under
`table-layout: fixed`, so a first attempt at a plain
`th/td:nth-child(9) { width: 92px }` CSS rule in `suite.css` was silently
overridden by RCS's own colgroup every time, and a second attempt at
*injecting a brand-new* `<colgroup>` (measuring "natural" widths via
`getBoundingClientRect` before rebuilding it) was needlessly fragile —
those measurements depend on the Cuts sub-tab/Edit workspace actually
being visible at call time, which is timing-sensitive and, worse, was
moot: RCS's own colgroup made any freshly-built one redundant to begin
with.

The actual fix, `narrowAudioColumn()` in `suite.js`: find RCS's existing
`.edit-table colgroup col` elements (static markup, present from initial
page load regardless of tab visibility — no measurement or timing concern
at all) and edit the 9th one's inline `style.width` down to `92px`,
handing the reclaimed pixels to the 10th (`Script Text`, which already
ellipsis-truncates so it absorbs extra width safely). Idempotency guard
via a `data-suite-narrowed` marker on the 9th `<col>`. Called once, at
`boot()` — no `switchWs`/`MutationObserver` hook needed, since the
`<col>` elements are always present and editable regardless of which
workspace or which Script/Cuts/Export/History sub-tab is currently active.

## F. Verification

- Backend: extend the v6 synthetic test — after the existing index-based
  assertions, call `suite_toggle_favorite_range` with a time range that
  does NOT match either parsed segment (e.g. 10.0–12.0s) and confirm it
  favorites/unfavorites independently (doesn't collide with the
  index-based favorite on the same source); confirm the resulting
  favorite has `index: None`. `py_compile` all changed files.
- Frontend: `node --check`; browser-harness — click a Cuts row's star with
  no prior favorite on that source (a row added by hand, not via a
  transcript), confirm it appears in the Favorites tab; click the preview
  window's star while previewing that same cut, confirm it reads as
  already favorited (proves both stars agree on one row); unfavorite from
  either one and confirm the other clears too. Screenshot confirming the
  Audio column is visibly narrower than before. Zero console errors.
  `main.py --selftest` passes. RCS's own files unchanged (mtime check).
  Sync both `Studio Suite` copies.

# Contract Addendum v8 — Audio-select width, Brander render fixes, dedicated Gemini key (2026-07-14)

## A. Cuts table — audio-mode select sized to its header text

`.audio-cell select`/`.duck-db-input` stretched to fill the whole (v7-
narrowed) 92px column via RCS's own `width:100%` rule. `suite.css` now
overrides both to a fixed `78px` — measured to match the "B-Roll Audio"
header label's own rendered text width at the header's font/letter-
spacing (~77px via canvas text measurement), not the wider column box.
Both controls get the same width so they stay visually aligned stacked in
one cell (`.audio-cell` is `flex-direction:column`).

## B. Blair Brander `renderer.py` — one-time sibling-file exception

Two bugs were root-caused to Blair Brander's OWN `renderer.py` (not suite
code): (1) `compute_outro("wipe", ...)` returned no alpha reduction and
the wipe-reveal mask was applied only to the title layer, so Subtitle/
Logo/the divider never animated out under a `"wipe"` outro (fade/slide/
zoom outros already worked correctly for all four elements); (2) the
Lower-Third collision-avoidance remap (only triggered when the plate
occupies the same vertical half as the chosen logo placement) recognized
only `"left"`/`"right"` suffixes, so a `"-center"` `logo_placement`
silently snapped to `"-right"` instead of staying centered.

Per the project's standing rule, sibling apps' own files are never
modified — this was flagged to the user as a real conflict (the fix
lives entirely inside `renderer.py`, not in suite glue), and the user
explicitly approved patching `renderer.py` directly, as a one-time,
per-incident exception (see the `sibling-app-file-exception` note) rather
than a change to that standing policy.

Fix: a shared `apply_wipe_mask(layer, frac, w, h)` helper (left-to-right
reveal mask) is now applied to the title, divider, subtitle, and logo
layers alike — each using ITS OWN outro phase progress (`subtitle_out_p`/
`logo_out_p`, not title's), since subtitle/logo carry independent
`*_out_start`/`*_out_end` timing. The Lower-Third remap's `side` fallback
now recognizes `"center"` alongside `"left"`/`"right"`, so `"-center"`
round-trips through the flip unchanged instead of defaulting to `"-right"`.
Verified empirically (render `renderer.render_frame()` directly with
isolated scenes, comparing per-region alpha sums before/after for outro
styles `wipe`/`fade`, and resolved logo pixel position for all 7
placements under both Lower-Third vertical positions) — see the
verification script kept in the session scratchpad, not committed to the
repo. No suite files needed any change for this fix.

## C. Blair Brander — dedicated Gemini API key (no longer shares RCS's)

Previously `brander_ai_generate` fell back to RCS's inherited
`load_saved_api_key()` (Rough Cut Studio's shared, plaintext-`.env`-backed
key) whenever no explicit session key was typed into a bare, error-
triggered password input (`gxAiKeyRow`/`gxAiKey`, "Use key"/"Save key").
Blair Brander now has its own, fully independent credential — no fallback
to RCS's key at all.

- **Storage**: system keychain (via the `keyring` package, same mechanism
  the Transcribe workspace already uses for its Hugging Face token), under
  a distinct service/key pair: `BRANDER_KEYRING_SERVICE = "BlairBrander"`,
  `BRANDER_KEYRING_GEMINI_KEY = "gemini_api_key"` (`suite_api.py`) — never
  written to any file, never shared with RCS's `.env`.
- **Backend**: `brander_gemini_key_status()` → `{"ok": True, "present":
  bool}`; `brander_save_gemini_key(key)` → keychain set (non-empty) or
  delete (empty string), `{"ok": True}` either way (deleting an absent key
  is not an error). `brander_ai_generate(prompt_text, scene)` dropped its
  `api_key` parameter entirely — reads only from the keychain; no key ->
  `{"ok": False, "error": "no_api_key", ...}`, same shape the UI already
  handled.
- **Frontend**: the old bare `gxAiKeyRow`/`gxAiKey` row is replaced with a
  `.suite-token-row`-style status+edit row (`gxGeminiKeyRow`/
  `gxGeminiKeyStatus`/`gxGeminiKeyEdit`/`gxGeminiKeyInput`/
  `gxGeminiKeySave`) — same visual pattern as the Transcribe workspace's
  HF-token row (reuses its `.suite-token-status`/`.suite-token-edit` CSS
  verbatim, no new styles needed). Shown proactively whenever Gemini mode
  is selected (not just after an error) via `refreshBranderGeminiKeyStatus()`
  (also called once at boot so the status is warm before the user ever
  switches to Gemini mode); a `no_api_key` error from `brander_ai_generate`
  still reveals the edit sub-row defensively as a fallback.

## D. Verification

- Backend: `py_compile` all changed Python files (including Blair
  Brander's `renderer.py`, `app.py`, `brand.py`, `assets.py`, `export.py`,
  `prompt_ai.py`, `project_io.py`, `timeline.py` — confirm the WHOLE
  sibling app still compiles clean after the one-time exception edit).
  `main.py --selftest`'s stale C4 block (written for the old `api_key`-
  argument signature) was rewritten: `keyring.get_password`/
  `set_password`/`delete_password` are monkeypatched with an in-memory
  fake for the duration of the check (restored in a `finally`, confirmed
  afterward that no real "BlairBrander" keychain entry was created) so the
  test never touches the real OS keychain; asserts the full status/save/
  clear/double-clear round-trip plus the `no_api_key` path.
- Frontend: `node --check`; browser-harness screenshot of the Gemini key
  row in both states (missing/present), confirming save flips the status
  text and collapses the edit row, "change" re-reveals it, and the row
  appears immediately on switching to Gemini mode (not only after an
  error). Zero console errors. Cuts-table audio select measured at the
  target width in a real DOM, header/body still aligned. RCS's own files
  unchanged (mtime check) — only the intentionally-excepted Blair Brander
  `renderer.py` differs. Sync both `Studio Suite` copies.

# Contract Addendum v9 — one sidecar per video, not two (2026-07-14)

## Why a full merge into ONE file isn't safe

`.ivt-cache.json` is NOT suite-owned — the standalone Local Interview
Transcriber reads/writes this exact file itself, and critically, its mere
EXISTENCE marks a video "already transcribed"
(`Local Interview Transcriber/app.py:1385`, `if cached: fr.status = "done"`,
for ANY cache including one with zero segments). `.sync-offsets.json` is
100% suite-owned (A-Sync itself persists nothing). If a video is synced
*before* it's ever transcribed — a normal order of operations — writing
an `.ivt-cache.json` early just to hold sync data would make the
standalone Transcriber silently skip it (shows "done", 0 segments) until
the user notices and ticks "Force re-process." So the two files can't
always collapse into one.

## What actually merges, and when

Once a video is BOTH synced and transcribed, its sync/routing data moves
INTO `.ivt-cache.json` (as extra keys the standalone app already tolerates
— it was already doing this for a single `audio_source`/
`sync_offset_seconds` pair since addendum v3) and `.sync-offsets.json` is
deleted. A sync-only video keeps its sidecar until it's transcribed, at
which point the sidecar is folded in and removed. Net effect across the
four possible orderings:

| Order | Result |
|---|---|
| Sync only (never transcribed) | 1 file: sidecar |
| Transcribe only (never synced) | 1 file: cache (unchanged from before) |
| Sync, then transcribe | `write_ivt_cache` folds the sidecar's `tracks`/`method` into the cache it's about to write (as new keys `sync_tracks`/`sync_method`/`sync_updated_at`, alongside the existing single-track `audio_source`/`sync_offset_seconds`, which records which ONE track was actually fed to whisper — a different, still-useful fact when several recorders were synced but only one was transcribed) → deletes the sidecar once the write succeeds. **1 file: cache.** |
| Transcribe, then sync | `sync_save_offsets` finds a valid existing cache, read-modify-writes `sync_tracks`/`sync_method`/`sync_updated_at` directly into it (preserving segments/speakers/etc.), and never creates a sidecar at all. **1 file: cache.** |
| Legacy dual-file state (both already exist, from before this addendum) | The next `sync_load_offsets` call (Sync workspace opens that video) detects both, folds the sidecar into the cache, deletes the sidecar. **Migrates to 1 file: cache**, opportunistically, on next touch — no migration script needed. |

## New cache keys (`sync_tracks`, `sync_method`, `sync_updated_at`)

Same shape as `.sync-offsets.json`'s own `tracks`/`method` fields — each
track dict is `{path, offset_seconds, enabled?, channels?}` (addendum v4
routing, when present). Consolidation is a straight copy, not a
reinterpretation, so `discover_synced_audios`'s routing semantics
(`resolve_emit_channels`, disabled-track exclusion) apply identically
regardless of which file the data came from.

## Fixed along the way: `transcriber_update_transcript` was dropping these keys

Discovered while designing this: `transcriber_update_transcript`
(`suite_api.py`) already rebuilt `.ivt-cache.json` from scratch on every
transcript edit (rename a speaker, merge, exclude) using only
`path/name/video_size/video_mtime/speakers/segments/speaker_labels/
excluded_speakers` — silently dropping `audio_source`/
`sync_offset_seconds` (and, after this addendum, `sync_tracks`/
`sync_method`/`sync_updated_at`) on the very first edit after
transcription. This made the new merge fragile (a single speaker rename
would un-merge a video back to needing its sidecar again, except the
sidecar would already be gone). Fixed: read the existing cache first (raw
read, not gated on the staleness check — the fresh write about to happen
makes the video fingerprint valid again regardless) and carry forward any
of those five keys present in the old data before overwriting.

## Call-site changes

- `backend/workers/transcribe_worker.py` `write_ivt_cache`: after building
  `data`, read `video_path + SYNC_OFFSETS_SUFFIX` directly (plain
  `os`/`json` — this runs in the Transcriber's OWN venv/process, which
  cannot import suite modules); if present and has `tracks`, fold into
  `sync_tracks`/`sync_method`/`sync_updated_at`. Delete the sidecar only
  after the cache write itself succeeds.
- `backend/suite_api.py` `sync_save_offsets`: check `self._read_ivt_cache`
  first; if a valid cache exists, write the routing fields into it
  (read-modify-write, all other keys untouched) instead of the sidecar,
  and remove any lingering sidecar file. Else, unchanged (writes the
  sidecar).
- `backend/suite_api.py` `sync_load_offsets`: check the cache's
  `sync_tracks` first; if absent, fall back to the sidecar — and if BOTH
  exist (legacy state), consolidate right then (fold sidecar into cache,
  delete sidecar) before returning. Response shape
  (`{ok, found, video_path, method, tracks, updated_at}`) is unchanged
  regardless of which file backed it, so the Sync workspace frontend
  needs zero changes.
- `backend/synced_audio_splice.py` `discover_synced_audios`: the
  "sidecar-primary" layer now checks the cache's `sync_tracks` key first
  (if PRESENT — even an empty list counts, meaning consolidation already
  happened and there's nothing synced), falling back to the sidecar file
  only when that key is absent (not-yet-consolidated / sync-only video).
  The existing legacy-singular-fields and transcript-note fallback layers
  are unchanged.

## Verification

- Backend: real-media test in `main.py --selftest` covering all four
  orderings from the table above (assert exact file existence — sidecar
  present/absent, cache present — at each step, not just returned data),
  plus a transcript-edit-after-consolidation case asserting
  `transcriber_update_transcript` doesn't drop `sync_tracks`. `py_compile`
  all changed files.
- No RCS/A-Sync/standalone-Transcriber files touched. Sync both
  `Studio Suite` copies.

# Addendum v10 — Master Blueprint execution: architecture + performance pass

Executes the architecture (A-0..A-5) and performance (PERF-1/2/4/5) items
of `MASTER_BLUEPRINT.md` (the SEC items landed just before this pass).
PERF-3 was investigated and closed as a FALSE POSITIVE — see below.

## Sibling-app files patched (second explicit, user-approved exception)

The user chose "Patch siblings directly" over suite-side monkeypatching
for the performance items, extending the renderer.py precedent to this
pass (per-incident approval, still not a blanket rule change):

- `B-Roll Analyzer/vision_energy.py` — device pick now prefers **MPS**
  (was "cuda else cpu", i.e. always CPU on Apple Silicon); new batched
  `score_frames_energy(pil_images)` API (BATCH_SIZE=32 chunks, one model
  forward pass per chunk); `score_frame_energy` kept as a batch-of-one
  wrapper. Verified live on MPS: batch vs single max delta 1.1e-5.
- `B-Roll Analyzer/analyzer.py` — the decode loop queues sampled frames
  (placeholder energy=0.0) and batch-flushes every BATCH_SIZE via the new
  API; failure semantics identical to the old per-frame call. Analyzer
  pytest suite: 48 passed.
- `B-Roll Analyzer/result_cache.py` — cache-validity mtime compare is now
  tolerant (|Δ| <= 1e-6 s) instead of exact float `!=`; stored JSON format
  deliberately unchanged (shared with older app copies).
- `Blair Brander/renderer.py` — `load_font` + font-fit/text-layout now
  lru_cached; `_vignette_mask` sampled on a 256-grid + bilinear upscale
  (max per-pixel delta 2/255 vs old, measured all shapes × strengths at
  1080p); `_gradient_layer` rebuilt per anti-diagonal with bytes slicing
  (bit-identical, delta 0). Cold-start per spawn worker: vignette
  680→25 ms, gradient 669→7 ms.
- `Blair Brander/export.py` — consecutive duplicate frame times (the
  hold_seconds tail, all t==1.0) are run-length encoded: rendered once,
  bytes written N times. Verified: identical frame count (150) and
  identical decoded-content SHA vs the old exporter; ~29% faster on a
  2s+1s-hold export.
- `Studio Suite/backend/workers/broll_worker.py` `_pool_init` — also caps
  torch intra-op threads to 1 per pool child (guarded import), mirroring
  the existing cv2 cap.

## A-1: suite_api.py split into per-workspace mixins

`backend/suite_api.py` (1,825 lines) is now ~200 lines composing:

    class SuiteApi(SecurityMixin, TranscriberMixin, BrollMixin,
                   FavoritesMixin, SyncMixin, BranderMixin, Api)

New modules: `api_shared.py` (all shared constants + `_first_path`/
`_all_paths` + the RCS sys.path bootstrap), `api_security.py`,
`api_transcriber.py`, `api_broll.py`, `api_favorites.py`, `api_sync.py`,
`api_brander.py`. RCS's Api is LAST in the MRO so mixin overrides win and
their `super()` calls fall through to RCS. suite_api.py re-exports the
api_shared constants, so pre-split callers (`main.py --selftest`, verify
scripts) that read `suite_api.WHISPER_MODELS` etc. keep working.
suite_api.py itself keeps `__init__`, the Jobs surface (incl.
`_require_window`/`_finished_job`), and the `save_xml` splice override.
Verified: composed-class method inventory matches pre-split exactly
(the only "missing" names were module helpers and nested locals).

## A-2: one worker-protocol definition

`backend/workers/worker_protocol.py` is now the single schema for the
params-in-argv[1] / JSON-lines-on-stdout contract: `make_progress` /
`make_result` / `make_error` on the build side (all three workers'
`emit_*` route through it), `parse_line` on the receive side (jobs.py).
Stdlib-only, lives in workers/ so a worker run as a script imports it
bare while suite code imports `backend.workers.worker_protocol`.

## A-3: mirrored constants now checked for EQUALITY

- transcribe_worker `--selfcheck` dumps `{whisper_models, cache_suffix}`
  as a JSON line before "WORKER OK"; `main.py --selftest` runs it in the
  transcriber's venv and asserts equality with `suite_api.WHISPER_MODELS`
  / `IVT_CACHE_SUFFIX` (skips with a notice if that venv is absent).
- `main.py --selftest` ast-parses Blair Brander's app.py and asserts
  `brander_bridge.default_scene()`'s body (ast dump minus docstring) and
  `LOGO_PLACEMENTS` still match the sibling source.

## A-4: save_xml splice failures are no longer silent

The exception path still falls back to RCS's stock export, but now also
appends a `warnings` entry AND pushes a deferred (600 ms — after the
frontend's own "saved" status lands) `setStatus(..., "error")` via
`evaluate_js`, following RCS's own `_notify_retry` push pattern.

## A-5: boot-time RCS coupling check (suite.js)

`assertRcsHooks()` (called first thing in `boot()`) inventories every RCS
symbol/DOM anchor the overlay reaches by name — 8 functions,
`state.editSegments`, 4 static DOM ids, the 13-col `.edit-table`
colgroup — and `console.warn`s per missing hook plus a summary. Warnings
only, never a throw. All anchors verified static in RCS's index.html.

## A-0: two-copy sync is now one command

`Studio Suite/sync_copies.sh`: one-way rsync (`--checksum`) of CODE files
only (.py .js .css .html .md .sh .txt), primary → All-In-One. NEVER
touches `assets/` (the copies hold different user data), `.env`, venvs,
or caches; no `--delete`. `--check` = dry-run drift report ("IN SYNC" /
file list).

## PERF-5 (small fixes)

- `sync_worker.py` timecode mode reads the VIDEO's embedded timecode once
  per batch (was one full ffprobe per audio file), same error text.
- `broll_send_to_edit` parses each folder's analyzer cache once per send
  (`_broll_clip_duration` `_memo` param), not once per clip.
- suite.js: `refreshCutsRowFavoriteMarkers` is now rAF-coalesced
  (`scheduleCutsRowFavoriteMarkers`) in the `#editTableBody` observer —
  one star scan per frame instead of one per mutation, trailing-edge safe.

## PERF-3: closed as false positive

The blueprint claimed selection changes rebuild the b-roll grid. Verified
untrue: `renderBrollResults()` has exactly ONE caller (new analysis
results in `onJobDone`); checkbox toggles and undo/redo already update
chips/checkboxes in place. No change made; blueprint annotated.

## Verification

- `main.py --selftest` green in BOTH copies (now includes the v10 A-3
  equality checks). All changed .py files `py_compile` clean; suite.js
  `node --check` clean. All three worker `--selfcheck`s pass in their own
  venvs (exercises worker_protocol's bare-import path).
- B-Roll pytest suite: 48 passed. CLIP batch-vs-single parity on MPS.
- Brander: mask/gradient parity + wipe-outro regression (alpha bbox None
  at t=1.0) + byte-identical export verified against the pre-change copy.
- Frontend: composed page loaded in a browser with a stubbed pywebview —
  boot completes, all injections land, `assertRcsHooks` reports ZERO
  missing hooks.

# Addendum v11 — Edit-workspace UI bug batch (5 fixes)

Five user-reported UI bugs on the Edit workspace, all fixed suite-side
(frontend/suite.js + frontend/suite.css only — no RCS files touched).

## 1. Favorites stars inconsistent (Cuts page / preview window)

Root cause: the three favorite-star surfaces used three different
matching schemes. The transcript-modal star matched by
`(source_id, index)`, while the Cuts-row and preview-window stars
(`suite_toggle_favorite_range`, which always stores `index: null`)
matched by `(source_id, start_tc, end_tc)` — so a favorite made from a
Cuts row or the preview never showed as favorited in the transcript
modal. Separately, `currentPreviewSegment()` read the CACHED
`state.editSegments` copy of a cut's in/out timecodes, which only syncs
with the live DOM input on blur/change — so right after retyping a
timecode, the Cuts-row star (always DOM-live) and the preview star
(cache-read) could disagree about the exact same cut, which is the
literal "inconsistent between the Cuts page and the preview window" bug.
Fixes, all in `suite.js`:
- `refreshTranscriptFavoriteStars` now looks up the segment's
  `start_tc`/`end_tc` from `state.transcriptModalSegments` and matches on
  those, same key as the other two surfaces.
- `currentPreviewSegment()` now reads `source_id`/`in_tc`/`out_tc`
  directly from the previewed row's own live DOM cells (found via
  `tr[data-cid]`), not from `state.editSegments` — so it and the
  Cuts-row star always read the identical live values.
- The Cuts-table `change` listener (fires on a committed in/out edit)
  now also calls `scheduleCutsRowFavoriteMarkers()` and
  `refreshPreviewFavoriteStar()` — previously it only refreshed the
  suite timeline, leaving a retyped row's star stale until an unrelated
  childList mutation happened to re-scan it.

## 2. B-Roll Audio dropdown misaligned with its row

RCS's own `.audio-cell { display:flex; flex-direction:column }` (needed
to stack the select above the duck-dB input) takes that `<td>` out of
normal table-cell layout, so `.edit-table td { vertical-align:middle }`
no longer has any effect on it — every sibling cell (Track, Source, …)
stayed centered while Audio sat packed at the top. Fix: added
`#workspace-edit .audio-cell { justify-content: center; }` — the
flex-column equivalent of `vertical-align:middle`. Verified: the audio
cell's content is now exactly center-aligned (0px offset), matching the
Source cell's own alignment (0.25px, i.e. also centered).

## 3. Editorial note column too wide

RCS's own colgroup (`index.html`, 13 `<col>`s) ships the "Editorial note"
column (index 10) with NO explicit width — under `table-layout:fixed`,
the one column with no width absorbs 100% of whatever's left over after
every other column's fixed width is subtracted, so it silently rendered
far wider than a short annotation field needs. Fix, in `suite.js`
(function renamed `narrowAudioColumn` → `fixEditTableColumnWidths` since
it now does both jobs): cap Editorial note to 200px and make Script Text
(col 9 — the column that actually benefits from extra room) the new
unconstrained absorber by clearing its width instead. Same
"exactly one flexible column" scheme RCS's own colgroup already relied
on, just relocated to the column that should have it.

## 4. Runtime pill placement

The pill (`#durationMeter`) was already grid-placed in the SAME row as
Target Duration, but in the grid's second column — visually on the
opposite side of the panel from it, separated by the whole width of the
prompt textarea/column 1, reading as unrelated rather than paired. Fix:
`relocateRuntimeMeter()` (suite.js, called once at boot) reparents
`#durationMeter` to be a literal DOM sibling of the Target Duration
input, inside the same `.field--inline` label — RCS's own
`renderDurationMeter()` only ever looks the element up by id and sets
hidden/className/textContent, so moving it in the DOM has no effect on
RCS's code. `.field--inline` (already `display:flex; flex-direction:row`
per RCS) now spans the full row width and wraps if needed, so the pill
always renders immediately next to the Target Duration input instead of
across the panel from it.

## 5. Left Sources panel horizontal scrollbar

RCS's `.panel` sets only `overflow-y: auto`, never `overflow-x` — but
overflow-x/overflow-y are a computed pair, so the unset axis is force-
computed to `auto` too. `.source-item__name` (a source's filename,
verbatim) is `white-space: nowrap` (needed for its own
`text-overflow: ellipsis`) but had no `min-width: 0`, so as a flex child
it refused to shrink below its full unwrapped text width — a long
filename overflowed the fixed 320px `.panel--side` column, and the
forced `overflow-x: auto` turned that into a horizontal scrollbar. Fix:
`#workspace-edit .panel--side .source-item__name { min-width: 0; }` (lets
the ellipsis actually engage) plus an explicit
`#workspace-edit .panel--side { overflow-x: hidden; }` as defense in
depth, matching the pattern `.suite-controls` (every other workspace's
left rail) already uses. Scoped to `.panel--side` only —
`.panel--main`'s Cuts table has its own dedicated
`.edit-table-wrap { overflow: auto }` for its legitimate horizontal
scroll, untouched by this.

## Verification

All 5 fixes verified in a browser against the real composed page (stubbed
`window.pywebview`, synthetic source/cut data, `outputBlock.hidden` forced
false since that's normally only unhidden after a real generation): the
audio cell's content sits exactly centered (measured 0px offset from the
Source cell's own centering); the colgroup shows Editorial note at 200px
and Script Text with no explicit width (auto/flexible); the Runtime pill
renders inside `.field--inline`, immediately beside Target Duration; the
Sources panel shows `scrollWidth === clientWidth` (no overflow) even with
a very long synthetic filename designed to trigger it. `node --check`
clean, `main.py --selftest` green. Sync both `Studio Suite` copies.

(Debugging note, not a real bug: verifying this in a plain browser
required a cache-busting query string on the `<script>`/`<link>` tags —
the browser was serving a stale cached copy of `suite.js` across repeated
navigations to the same URL despite the on-disk file being fresh, which
briefly looked like the fixes weren't taking effect. Not a concern for
the real app, where pywebview loads the composed page exactly once per
launch from a freshly regenerated `_generated/index.html`.)

# Addendum v12 — Edit-workspace UI bug batch, round 2

Follow-up on Addendum v11: four of its fixes needed a deeper correction,
plus one new feature (column resizing). All still suite-side only
(`frontend/suite.js` + `frontend/suite.css`) — no RCS files touched.

## Runtime pill still overflowing past the Creative Brief field

v11's fix reparented `#durationMeter` into `.field--inline` but let that
label span BOTH grid columns (`grid-column: 1 / -1`) with the meter set
to `flex: 1 1 160px` (flex-grow: 1) — so the pill grew to fill the ENTIRE
row width, well past where the `#prompt` textarea above it ends on the
right. Fix: `.field--inline` is back to `grid-column: 1` only (the SAME
column `#prompt` occupies), and `.duration-meter` is `flex: 0 1 auto;
min-width: 0` (no grow) — it now sizes to its own content, bounded by the
textarea's own width, wrapping onto its own line via the row's existing
`flex-wrap: wrap` if a narrow window leaves no room. Verified at three
viewport widths (1920, 900, 650px): never extends past `#prompt`'s right
edge; wraps cleanly with no horizontal overflow at 650px.

## Favorite star moved onto the preview thumbnail

Previously sat in `.preview-player__header`, next to the "Preview" title
and ✕ close button — a full row above the actual video. suite.js's
`injectPreviewFavoriteStar` now wraps `#previewVideo` in a new
`.suite-preview-thumb-wrap` div (moving the existing video element in,
not cloning it, so RCS's own `el("previewVideo")` lookups are unaffected)
and overlays the star absolutely on its top-right corner — the same
rounded-badge-over-media treatment `.suite-seg-play` already uses for the
B-Roll segment cards' play button. Verified: star sits exactly 8px from
the wrap's top and right edges, i.e. directly on the thumbnail.

## Favorites stars: the remaining mismatch class

v11 fixed CROSS-SURFACE consistency (all three stars agreeing with each
other) but all three still matched on timecode STRINGS
(`start_tc`/`end_tc`), not the numeric `start_seconds`/`end_seconds` the
backend's own `favorites.find()` actually uses (±0.05s tolerance). A
tc string is regenerated from seconds via `format_timecode()` at
favoriting time and can differ from whatever string currently sits in a
row's in/out input — a different `rulerFps` read, or an independent
timecode round-trip — even when the underlying range is the same well
within the backend's own tolerance, silently breaking the star's exact
string match while the backend still correctly considers it favorited.
Fix: a single shared `isFavoritedRange(sourceId, startSeconds,
endSeconds)` (± the same 0.05s `FAV_TOLERANCE_SECONDS`) now backs every
star-state check (transcript modal, Cuts row, preview window) AND every
local-cache removal filter after a toggle-off — all converted to
seconds via the existing `suiteTcToSeconds()` before comparing, exactly
mirroring `favorites.py find()`'s own logic.

## B-Roll Audio dropdown: the real root cause

v11's `justify-content: center` on `.audio-cell` (RCS's own
`display:flex; flex-direction:column`) turned out not to work: a
flex-display box substituting for a table cell does NOT get stretched to
the row's algorithmically-determined height — it sizes to its own
content only. Confirmed empirically: a "Silent"-mode audio cell measured
42px tall inside a 49px row, so centering within that undersized 42px
box left the select ~3.5px above the row's TRUE center (where the
Source cell's own select — a plain, unmodified `<td>` — correctly sits,
via RCS's ordinary `.edit-table td { vertical-align: middle }`). Fix:
`.audio-cell` is overridden back to `display: table-cell; vertical-align:
middle` (suite.css loads after RCS's style.css with identical selector
specificity, so it cleanly wins); the select/duck-db-input now stack via
plain `display: block` + `margin-top: 3px` instead of a flex column.
Verified across three audio-mode rows (silent/full/duck_main): audio-cell
height now exactly equals row height in every case, and the select's
vertical center exactly matches the Source cell's (0px delta), vs. the
~3.5px delta measured before this fix.

## New: adjustable Cuts table column widths

`injectColumnResizeHandles()` (suite.js) adds a drag handle to each
header `<th>`'s right edge for columns 2 ("Preview") through 11
("On-screen text") — the checkbox/spacer columns (0, 1) and the tiny
actions column (12) are excluded as not worth resizing. Dragging a handle
trades width between the two adjacent columns symmetrically (delta added
to the left column, subtracted from the right, both clamped to a 40px
floor) — the table's total width never changes regardless of which pair
is being resized, the same scheme most spreadsheet/data-grid resizers
use. The one unconstrained column (Script Text) is "frozen" into an
explicit pixel width — measured from its live rendered size — the first
time either of its two boundaries is dragged; from then on it behaves
like any other fixed column. Widths persist in
`localStorage["suiteEditTableColWidths.v1"]` (a per-user display
preference, never written to any project file or backend store) and are
re-applied once at boot, after `fixEditTableColumnWidths()`'s own
defaults, so a saved width always wins over the default. Verified via a
simulated drag: dragging the Preview/Track boundary +30px produced
Preview 84→114, Track 120→90 (sum constant at 204 before and after);
localStorage captured the full 13-column array (`null` for the
still-untouched auto column); reloading the page re-applied the exact
saved widths.

## Verification

`node --check` clean, `main.py --selftest` green. All fixes verified in
a browser against the real composed page (stubbed `window.pywebview`,
synthetic multi-row Cuts data spanning all three audio modes, multiple
viewport widths for the Runtime pill). Sync both `Studio Suite` copies.

(Debugging note, not a real bug — recurring from v11: this environment's
browser cache repeatedly served a stale `suite.js`/`suite.css` across
navigations to the same URL, at one point even making the column-resize
persistence look broken for one single check before a same-session
re-check confirmed it was correct. A fresh cache-busting query string per
reload was needed throughout. Irrelevant to the real app, which loads
fresh from a freshly regenerated `_generated/index.html` exactly once
per launch.)

# Addendum v13 — Favorite star repositioning, round 3

Two small placement changes on top of v12, both suite-side only
(`frontend/suite.js` + `frontend/suite.css`).

## Preview-window star moved to the left corner

`.suite-preview-fav-btn` was `top: 8px; right: 8px` (top-right corner of
`#previewVideo`, via `.suite-preview-thumb-wrap`). Now `top: 8px; left:
8px` — same overlay mechanism, opposite corner. Verified: 8px from both
the wrap's top and left edges.

## Cuts-row star moved next to the Preview thumbnail

Previously injected into the LAST cell's `.reorder-cell__inner`,
alongside RCS's own duplicate/delete buttons — a full row away from the
Preview thumbnail. `refreshCutsRowFavoriteMarkers` (suite.js) now
appends it into `.thumb-cell` (column 2) instead, and it's overlaid on
the thumbnail's corner — the same corner-badge treatment as the
preview-window star, just scaled down (16px vs. 26px) to fit a Cuts
row's much smaller 64x36px thumbnail. `.thumb-cell` picks up
`position: relative` (suite.css) purely to anchor it; this has no effect
on the table's own layout. Verified: star sits 2px from the thumb-cell's
top and right edges, and no longer appears in the last cell.

## Verification

`node --check` clean, `main.py --selftest` green. Both placements
confirmed via `getBoundingClientRect` measurements and a visual
screenshot against the real composed page. Sync both `Studio Suite`
copies.

# Addendum v14 — "No logo" option in the Graphics workspace

The standalone Blair Brander app already fully supported "no logo": its
own logo dropdown prepends a "None" option ahead of `brand.LOGO_SOURCES`
(`Blair Brander/app.py:447-449`), and `renderer.py` already treats a
`"None"` (or falsy/missing) `scene["logo"]` as "skip the logo layer
entirely" in both its compositing step (`renderer.py:562-563`) and its
animation-timing plateau calculation (`renderer.py:643,650`) — no
`brand.LOGO_SOURCES[...]` lookup ever happens for that sentinel, so
there was no crash risk to guard against. Studio Suite's Graphics
workspace was simply never given that option: its logo list came from
`options_dict()["logos"]`, built solely from
`list(brand.LOGO_SOURCES.keys())`.

This was a pure suite-side gap — no Blair Brander files needed to
change:

- `backend/brander_bridge.py` `options_dict()`: `"logos"` now returns
  `["None"] + list(brand.LOGO_SOURCES.keys())`, mirroring the standalone
  app's own dropdown ordering exactly.
- `frontend/suite.js`'s scene→UI sync for `#gxLogo` now falls back to
  `"None"` (not just leaving the select on its previous value) when
  `scene.logo` is missing/falsy OR no longer a valid option (e.g. a
  deleted custom logo) — mirrors `app.py`'s own `s.get("logo") or
  "None"`. The bind that writes a selection back into the scene
  (`scene.logo = e.target.value`) and `fillSelect()` both needed zero
  changes — already generic enough to carry the new option through.
- `brander_import_logo`'s own logos list (`api_brander.py:116`) reuses
  `brander_bridge.options_dict()["logos"]` directly, so it picks up
  "None" automatically with no separate change.
- No collision risk with the custom-logo import namespace: every
  imported logo's display name is always `"Custom: <stem>"`-prefixed
  (`_unique_logo_name`, `brander_bridge.py`), never the bare word
  "None".

## Verification

Backend: `brander_defaults()` confirmed `"None"` is now the first entry
in `options.logos`. Rendered the SAME scene once with its real logo and
once with `logo` overridden to `"None"` via `brander_still_preview` —
both succeeded and produced different image bytes, confirming the
no-logo render genuinely omits the logo layer (not merely accepted and
ignored). Frontend: loaded the real composed page with a stubbed
`brander_defaults` response built from the actual backend output —
`#gxLogo` shows `"None"` first, correctly defaults to the scene's real
logo, and selecting `"None"` fires cleanly with no console errors.
`py_compile`/`node --check` clean, `main.py --selftest` green. Sync both
`Studio Suite` copies.

# Addendum v15 — Favorite B-Roll segments + a 6th "B-Roll" tab

Two related features: (1) favoriting individual B-Roll Analyzer segments
from the grid, and (2) a new "B-Roll" tab in the Edit workspace listing
those favorites, alongside RCS's native Script/Cuts/Export/History and
the suite-injected Favorites tab.

## Design: one favorites store, two `kind`s — not a parallel scheme

A B-Roll segment (analyzer.py's `Segment`: a clip file path + start/end
seconds) has no transcript/VTT/source_id of its own. Rather than invent a
second identity/matching scheme, B-Roll favorites reuse the EXACT same
`favorites.json` store and the exact same synthetic-VTT machinery
`broll_send_to_edit` already relies on
(`handoff.ensure_broll_source` — idempotent per clip path, so
favoriting the same segment twice always resolves to the same
`vtt_path`). This means `favorites.find()`'s matching (vtt_path +
seconds, ± 0.05s) needed ZERO changes.

- `backend/favorites.py` `build()`: three new optional fields —
  `kind="transcript"` (default) or `"broll"`; `clip_path`/`score`
  (B-Roll-only display metadata, `None` for a transcript favorite).
  `find()` is untouched — kind plays no role in matching, exactly like
  `index` already didn't (v7).
- `backend/api_favorites.py` `_toggle_favorite_range`: threads
  `kind`/`clip_path`/`score` through to `build()`. `suite_favorite_add_to_cuts`:
  now picks `track="broll"` vs `"main"` based on `fav.get("kind")` — the
  VTT re-ingest path is unchanged and works for either kind, since a
  B-Roll favorite's VTT was already written by `ensure_broll_source` at
  favoriting time, not just at add-to-cuts time.
- `backend/api_broll.py`: new `suite_toggle_broll_favorite(path,
  start_seconds, end_seconds, score=None)` — validates the clip exists,
  calls `handoff.ensure_broll_source` for its (durable, idempotent)
  source_id/vtt_path, then `self._toggle_favorite_range(..., kind="broll",
  clip_path=path, score=score)`. Because it reuses the SAME source a
  normal "Send to Edit" would create, a segment favorited here and later
  sent to Cuts (via either this favorite's own "+ Add to Cuts" or the
  ordinary B-Roll "Send Selected") lands on a Cuts row whose
  source_id/timecodes already match the favorite — so the EXISTING
  Cuts-row and preview-window stars recognize it as favorited too, with
  no extra code.

## Frontend: grid star + new tab

- `frontend/suite.js` `renderBrollResults()`: each segment chip gets a
  new `.suite-seg-fav-btn` button (`data-tact="broll-fav-toggle"`),
  a chipwrap SIBLING of the existing play button — not inside the
  selection `<label>`, so clicking it never also toggles selection.
  Distinct from the pre-existing bare `<span class="star">★</span>`,
  which is static score-label decoration, not a control.
  `isBrollFavorited(path, start, end)` (mirrors `isFavoritedRange`, but
  matches on `clip_path` + `kind:"broll"` instead of `source_id`) sets
  its initial state. Clicking it (`toggleBrollSegmentFavorite`, wired
  into the existing `#bGrid` delegated click listener) calls
  `suite_toggle_broll_favorite`, updates `S.favorites` locally, and does
  a TARGETED update of just that one button (`setFavStarState`) — not a
  full `renderBrollResults()` rebuild, same reasoning as the existing
  selection-checkbox handler.
- A 6th tab, "B-Roll" (`injectBrollFavoritesTab`, same injection pattern
  as `injectFavoritesTab` — `activateTab()` live-queries `.tab`/
  `.tab-panel` on every call, so a tab added anywhere in the DOM at any
  time just works, per v6's own design), anchored right after Favorites'
  own panel to keep tab order Script/Cuts/Export/History/Favorites/
  **B-Roll**. `renderBrollFavoritesPanel()` filters `S.favorites` to
  `kind==="broll"` (mirrored: `renderFavoritesPanel()` now excludes
  `kind==="broll"` so a segment never shows in both tabs). Reuses
  `addFavoriteToCuts`/`removeFavorite` verbatim — both already generic
  over any favorite id; `addFavoriteToCuts` now branches on
  `cut.track === "broll"` to route through `insertBrollCuts([cut])`
  (refreshing `state.sources` first, same as `broll_send_to_edit`'s own
  caller) instead of the transcript-favorite's hand-built main-track row.
- `refreshBrollSegmentStars()`: targeted re-scan of the grid's own star
  buttons after a removal from the NEW tab, so the grid never goes stale
  relative to `S.favorites` regardless of which surface (grid or tab)
  triggered the change.

## Verification

Backend (real Python calls, no browser): toggle-on produces a favorite
with `kind:"broll"`, correct `clip_path`/`score`; toggle-off (same
path/range) correctly un-favorites; `suite_favorite_add_to_cuts` on a
broll favorite — even after clearing `self.sources` to simulate a fresh
launch — correctly re-ingests the source and returns a cut with
`track:"broll"`. Regression-tested the ORIGINAL transcript-favorite path
end to end (toggle/add-to-cuts/toggle-off) to confirm the schema/method
signature extensions didn't disturb it — all fields (`kind`, `clip_path`,
`score`) come back `None`/`"transcript"` as expected. Frontend: the grid
star's click→toggle wiring invokes the backend exactly once per click
(confirmed via a monotonic-timestamp call counter) and correctly
transitions ☆→★/is-fav/title; the new tab injects in the correct
position (`Script, Cuts, Export, History, Favorites, B-Roll`), renders a
favorited segment's card with filename/timecodes/score, and
`activateTab`'s existing live-query mechanics activate it correctly. No
console errors. `node --check`/`main.py --selftest` green. Sync both
`Studio Suite` copies.

(Debugging note, not a real bug: repeated DOM-injection tests in the
SAME long-lived browser tab across many `navigate()` calls intermittently
showed a stale/duplicated mock-favorites array — consistent with this
same environment's already-documented script/state caching quirk
(v11/v12's own notes) rather than a defect in the click handler, which a
monotonic call-time counter confirmed fires exactly once per click.)

# Addendum v16 — Hide B-Roll sources from the Sources panel, enrich the
B-Roll tab's cards, fix favorites surviving a project switch

Three unrelated fixes/features requested together, all suite-side only.

## Sources panel no longer lists B-Roll synthetic sources

Favoriting or sending a B-Roll Analyzer segment registers its clip as a
REAL RCS source (`handoff.ensure_broll_source`), named `"<clip stem> —
broll"` so it can carry timecodes/media-link like any other source — but
it was cluttering the Sources list a user builds by adding their own
transcripts, and `renderSources()`/`state.sources` (`app.js`) can't be
touched. Fixed entirely in `frontend/suite.js` via a MutationObserver on
`#sourceList` (`injectBrollSourceFilter`/`hideBrollSourceItems`, wired
into `boot()`): every `<li class="source-item">` whose displayed name
contains the `" — broll"` marker (`unique_vtt_path`'s stem suffix,
including its `" -2"`/`" -3"` collision variants) gets `display: none`
after every RCS-driven re-render. If hiding leaves NO source visible,
RCS's own "No transcripts added yet." hint never renders (it only
appears when `state.sources` is completely empty) — a suite-injected
`<li class="block__hint suite-broll-hint">` fills that gap, removed
again the moment a real source is visible or RCS's own native hint is
present. `assets/` state (the sources themselves) is untouched; this is
a pure display filter.

## B-Roll tab cards: thumbnail, in-place preview, editorial notes, media link

`renderBrollFavoritesPanel()`'s cards previously showed only
filename/timecodes/score. Now:

- **Thumbnail + in-place preview**: each card reuses the EXACT
  `.suite-clip`/`.suite-clip__stage`/`.suite-clip__thumb`/
  `.suite-clip__preview` structure (and `playBrollSegment`/
  `stopBrollPreview`) the B-Roll Analyzer grid already uses — a card
  needs class `suite-clip` and a `[data-stage]`/`[data-preview]` pair for
  those functions' generic `btn.closest(".suite-clip")` lookups to work
  unchanged, so the ▶ overlay button (new `.suite-seg-play--overlay`
  modifier, absolutely positioned bottom-left of the stage) plays the
  clip IN PLACE of the thumbnail with zero new preview logic. The
  thumbnail itself is lazy-loaded via a new backend method,
  `suite_broll_favorite_thumbnail(path, start_seconds)`
  (`api_broll.py`) — pulls a frame straight from `clip_path` via
  `extract_thumbnail_data_uri` (the same extractor RCS's own
  `get_thumbnail` uses), deliberately bypassing `get_thumbnail`/
  `self.media_paths` entirely: a B-Roll favorite already carries its own
  `clip_path`, so this needs no source loaded/linked this session, unlike
  RCS's `source_id`-keyed thumbnails (which would go stale across
  launches). Frontend load is a small concurrency-limited queue
  (`enqueueBrollFavThumbnail`, `BROLL_FAV_THUMB_CONCURRENCY = 3`) —
  mirrors RCS's own `enqueueThumbnail` idea for the Cuts table, kept
  separate since that one is tightly coupled to `<tr>` rows.
- **Editorial note**: `favorites.py`'s `build()` now always includes a
  `"note": ""` field (kind-neutral, not just B-Roll) — never set at
  creation (so toggling a favorite off/on never has a stale note to
  lose), only via a new method, `suite_update_favorite_note(favorite_id,
  note)` (`api_favorites.py`). The card's `<textarea
  data-tact="broll-note">` saves on `change` (blur/Enter), not per
  keystroke — same "commit on change" behavior as RCS's own Cuts-table
  free-text fields.
- **Media link**: a new `suite_reveal_broll_media(path)` method
  (`api_broll.py`) reveals the clip in the Finder (`open -R`, macOS
  only — matches this suite's target platform; a small reimplementation
  of the same command Local Interview Transcriber's own
  `reveal_in_finder` uses, not an import of it — sibling-app files are
  never modified NOR imported as a runtime dependency). Surfaced as a
  `🔗 <filename>` button in the card's actions row.
- `.suite-fav-card--broll` lays the card out as a row (fixed 160px
  thumbnail stage + flexible body) instead of the plain Favorites-tab
  card's vertical block, since a thumbnail needs real width to read.

## Favorites now cleared on "New Project" / "Load Project"

Reported bug: favorited lines/clips persisted across an RCS "New
Project" or "Load Project", because `favorites.json` is a single
suite-wide store, entirely independent of RCS's own project-file
concept — neither `new_project()` nor `load_project()` (`api.py`, RCS's
own, never modified directly) ever touched `self.favorites`. Fixed via
two `FavoritesMixin` overrides (`api_favorites.py`) following this
codebase's established override pattern (`api_security.py`'s
`_autosave`/`autosave_working_state`): call `super().new_project()` /
`super().load_project()` first, and only clear+persist
`self.favorites = []` when that call actually succeeded (`{"ok": True}`)
— a generation-lock conflict, a cancelled file dialog, or a rejected/
corrupt project file must leave the current favorites untouched. Scoped
to these two entry points only; `restore_autosave` (crash recovery of
the SAME session, not a project switch) is deliberately left alone.

## Verification

Backend (real Python calls against a real ffmpeg-generated test clip, no
browser): `suite_broll_favorite_thumbnail` returns a real `data:image/...`
URI for a valid clip+timestamp and a clean error for a missing file;
`suite_update_favorite_note` persists the note to `favorites.json` and
errors on an unknown id; `suite_reveal_broll_media` errors on a missing
file and (with `subprocess.run` mocked, so no Finder window actually
opens during the test) invokes exactly `["open", "-R", path]` on
success; `new_project()`/`load_project()` were verified BOTH ways —
clearing `self.favorites` (in memory and on disk) when RCS's own
super() call reports `{"ok": True}`, and leaving favorites completely
untouched when it reports failure/cancellation (mocked at the `Api`
base-class level to isolate the override's own logic from RCS's
internals). `py_compile`/`node --check` clean, `main.py --selftest`
green.

Frontend: loaded the real composed page with a mocked
`window.pywebview.api` (a `Proxy` defaulting unknown methods to
`{ok:true}`, plus explicit handlers for `list_sources`,
`suite_list_favorites`, `suite_toggle_broll_favorite`,
`suite_broll_favorite_thumbnail`, `suite_reveal_broll_media`,
`suite_update_favorite_note`). Confirmed: a mixed source list (one real
transcript + two `" — broll"`-suffixed sources, one with the `" -2"`
collision suffix) renders with only the real one visible; an all-B-Roll
source list shows the suite-injected "No transcripts added yet." hint,
which disappears again once a real source reappears; a B-Roll tab card
renders with its mocked thumbnail already swapped in for the placeholder,
clicking ▶ sets `is-previewing` on the stage and inserts a `<video>`
(same toggle behavior as the Analyzer grid), editing the note fires the
`suite_update_favorite_note` call with the typed text, and clicking the
🔗 button fires `suite_reveal_broll_media` with the clip's path. No
console errors in any of these. Sync both `Studio Suite` copies.

# Addendum v17 — Favorites/B-Roll STILL not clearing (real root cause),
persist them in the project file, B-Roll tab grid layout

v16 fixed the backend half of "favorites don't clear on New/Load
Project" but the bug was still visible in the app — this addendum finds
and fixes the actual remaining cause, plus two related requests: saving
Favorites/B-Roll clips IN the project file (not just a suite-wide
store), and turning the B-Roll tab into a grid.

## The real root cause: a stale frontend cache, not a backend bug

v16's backend fix (`FavoritesMixin.new_project`/`load_project` clearing
`self.favorites`) was independently verified correct via direct Python
calls and never the problem. What was missing: `S.favorites`
(`frontend/suite.js`) is an in-memory CACHE, populated exactly ONCE at
`boot()` via `loadFavorites()` — nothing ever re-fetched it afterward.
So a successful New/Load Project correctly emptied/replaced
`self.favorites` on the BACKEND, but the Favorites tab, the B-Roll tab,
and every star indicator kept rendering whatever `S.favorites` still
held from boot, until the next full relaunch. Fixed by
`wrapProjectLifecycleApiMethods()` (called once at the top of `boot()`):
wraps `window.pywebview.api.new_project`/`.load_project` themselves (NOT
RCS's `app.js`, which calls them directly at click time with no hook to
attach an "afterward" step to) so that a successful result triggers
`refreshFavoritesAfterProjectChange()` — re-runs `loadFavorites()` and
repaints both tabs plus every star (`refreshTranscriptFavoriteStars`,
`refreshCutsRowFavoriteMarkers`, `refreshPreviewFavoriteStar`,
`refreshBrollSegmentStars`). Wrapping the API object's own methods is
transparent to `app.js` — its `await
window.pywebview.api.new_project()` call reaches the wrapper with zero
changes on its side.

## Favorites now saved/loaded WITH the project file

Reported: "Save the Favorites and B-Roll in project files." Previously
favorites lived ONLY in the suite-wide `assets/favorites.json` —
completely independent of RCS's own `.rcstudio.json` project files.
Now:

- `FavoritesMixin._build_project_dict(meta)` (`api_favorites.py`)
  overrides RCS's own dict-builder to add `project["favorites"] =
  self.favorites` before returning it. This is the ONE place that needed
  to change for saving: `save_project`, `save_project_to_path`, AND
  RCS's own `_autosave`/`autosave_working_state` all build their output
  dict through this single method, so every one of them now embeds
  favorites automatically with no separate override per call site.
- `FavoritesMixin.load_project` no longer unconditionally clears
  favorites (v16's behavior) — it now re-reads the JUST-LOADED file (by
  the `path` RCS's own `load_project` returns on success) and sets
  `self.favorites` to that file's own `"favorites"` list via a new
  `_read_favorites_from_project_file(path)` helper, replacing whatever
  was in memory. An OLDER project file saved before this feature existed
  has no `"favorites"` key — treated the same as an explicit empty list
  (not "leave unchanged"), so switching to one still correctly clears
  out the previous project's favorites. Deliberately does NOT hook the
  shared `_apply_loaded_project_unsafe` (used by BOTH `load_project` and
  `restore_autosave`): the two callers aren't distinguishable at that
  layer, and `restore_autosave` (crash recovery of the SAME session)
  must never touch favorites — re-reading the file independently by path
  sidesteps that collision entirely. `new_project`'s v16 behavior
  (unconditional clear to `[]`) is unchanged: a blank slate has no
  project file to carry favorites in.
- The suite-wide `favorites.json` still exists and is kept in sync
  (`favorites.save(...)`) on every load/new — it remains the live
  store for the CURRENTLY open project's favorites (crash-recovery/
  autosave source of truth), same role it always had; the project file
  is now the durable, portable copy that travels with a saved project.

## B-Roll tab: grid instead of a list

`#suiteBrollFavList` gets a new `suite-broll-fav-grid` modifier class
(`display: grid; grid-template-columns: repeat(auto-fill, minmax(240px,
1fr))` — same auto-fill idea as the B-Roll Analyzer's own
`.suite-broll-grid`) alongside the existing `suite-fav-list` base class,
so the plain Favorites tab's vertical list is untouched. Each card
reverts from v16's horizontal row (thumbnail sidebar + text alongside)
to a vertical layout matching the Analyzer grid's own `.suite-clip`
cards — a ~240px grid cell is too narrow for a thumbnail next to text.
`.suite-fav-card__stage` is now full-width (was a fixed 160px sidebar).
The media-link button moved out of the actions row onto its own
full-width truncating row (`.suite-fav-card__link { display: block;
width: 100%; ... }`) since a narrow card can't fit it alongside "+ Add
to Cuts"/★ on one line anymore.

## Verification

Backend (real Python calls, no browser, against a real ffmpeg-generated
test clip): `save_project_to_path` writes a `"favorites"` array into the
project JSON matching `self.favorites` exactly; `load_project`
(file-dialog mocked to return a known path) REPLACES in-memory favorites
with that file's own list — tested against a favorite made in the
"previous" session to confirm it's gone, not merged; an old-format file
with no `"favorites"` key correctly clears to `[]` rather than leaving
the previous project's favorites in place; a cancelled load dialog
leaves favorites completely untouched. `py_compile`/`node --check`
clean, `main.py --selftest` green.

Frontend: loaded the real composed page with a mocked
`window.pywebview.api` (this time a plain-object-semantics `Proxy` —
own-property assignments override the default handler, matching real
pywebview behavior, which is what `wrapProjectLifecycleApiMethods`
depends on). Confirmed `api.__suiteLifecycleWrapped` flips true after
`boot()`; clicking RCS's real `#btnNewProject` button (stubbing
`window.confirm`) empties the B-Roll tab's grid immediately, no reload
needed; clicking `#btnLoadProject` swaps in that mocked call's different
favorite immediately. Confirmed the B-Roll tab renders as an actual CSS
grid (`getComputedStyle().display === "grid"`, two auto-fill columns at
the test viewport width) with both cards' vertical layout intact
(thumbnail, note, media link, actions). No console errors. Sync both
`Studio Suite` copies.

# Addendum v18 — Top menu reorder, persisted settings for all three
workspaces, B-Roll tile timecode fix, Cuts header stacking fix

Five requested items, all suite-side only.

## Top menu order

`frontend/shell.html`'s `.suite-ws-tabs` nav buttons reordered to Sync,
Transcribe, B-Roll, Edit, Graphics (Edit moved before Graphics). Purely a
markup reorder — visual tab order comes from DOM order of the nav
buttons, not from any array in `suite.js`. The one array that iterates
workspace names (`switchWs`'s panel-hide loop) doesn't affect visual
order either (it only toggles each panel's `hidden`), but was reordered
to match anyway for readability.

## Persisted settings — Interview Transcriber, B-Roll Analyzer, Rough Cut Studio

All three workspaces reset their settings fields to hardcoded HTML
defaults on every launch; nothing persisted any of them. Fixed via
`localStorage`, the same per-USER-preference idiom already established
by the Cuts table's column-width persistence (`suiteEditTableColWidths.v1`)
— saved on every `change`, restored once at boot.

- **Interview Transcriber** (`suiteTranscriberSettings.v1`): Whisper
  model (`#tModel`), diarization (`#tDiarize`), and the parallel-
  transcriptions limit (`S.tParallel`/`#tParValue`). Restoring the model
  is deferred until AFTER `transcriber_models()` fills `#tModel`'s
  `<option>`s (async) — restoring earlier would set a value the select
  doesn't have an option for yet, silently no-oping. Restoring diarize
  dispatches a synthetic `change` event so the HF-token row's visibility
  updates exactly like a real click would. Restoring the parallel count
  re-sends it to the backend via `transcriber_set_parallel` so the
  in-memory worker pool matches what's displayed.
- **B-Roll Analyzer** (`suiteBrollSettings.v1`): the six analysis
  parameters (window/max-segments/min-gap/energy toggle/energy-weight/
  workers). The folder path is deliberately NOT persisted — picking a
  folder is part of each analysis run, not a standing preference.
- **Rough Cut Studio** (`suiteRcsSettings.v1`): frame rate, drop-frame,
  LLM provider, Gemini model, Ollama host, and the selected Llama model
  — RCS's OWN elements (`#fps`/`#dropFrame`/`#provider`/`#model`/
  `#ollamaHost`/`#llamaModel`, `frontend/index.html`/`app.js`, never
  modified), reached into from `suite.js` exactly like every other
  suite/RCS DOM integration point in this file. The restore function
  mirrors RCS's OWN `load_project` handler's restoration logic
  (app.js) — same order of operations (set `#fps`/`rulerFps`, call
  `updateDropFrameVisibility`, set `#provider`, call
  `updateProviderVisibility`, set `#ollamaHost`, and for a Llama
  provider call `refreshLlamaModels({silent:true})` before setting
  `#llamaModel`, injecting a synthetic "(not pulled…)" option if the
  saved model isn't in the pulled list) since that's already a proven
  path for setting these six fields together correctly — this just
  replays it with a saved preference instead of a loaded project's
  fields. Applied once at boot, which only matters for a genuinely fresh
  launch: RCS's own `new_project`/`load_project`/`restore_autosave`
  already own resetting/restoring these fields for their own cases and
  are untouched — the persisted preference only fills the gap where
  nothing else would set them.

## B-Roll tab: timecode getting cut off

Root cause: v17's grid layout narrowed each card to ~240px, but
`.suite-fav-card__source` (the clip filename) had no shrink/truncation
of its own — a long filename pushed `.suite-fav-card__tc` (the
timecode) partly or fully out of the card. Fixed with the same "one
flexible name + fixed-width trailing fields" split
`.suite-clip__name-row` already uses in the Analyzer grid:
`.suite-fav-card__source` now truncates with an ellipsis (`flex: 1 1
auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
white-space: nowrap`), while `.suite-fav-card__tc`/`__speaker` are
`flex: none` so they always render in full.

## Cuts table: header stacking/opacity fix

Reported: column labels get overrun by scrolled rows instead of staying
in front, and look "translucent" where that happens. Root cause: RCS's
own `.edit-table th` (`style.css`) is `position: sticky` with NO
explicit `z-index` (defaults to `auto`). Scrolled ROWS have their own
positioned descendants too — `.thumb-cell` (`position: relative`,
added for the Favorites-star overlay) — landing in that SAME
z-index:auto stacking layer as the sticky header. That layer paints in
DOM tree order, and `<tbody>` comes after `<thead>`, so a scrolled row's
positioned cell paints ON TOP of the sticky header as it passes
underneath. The header was never actually translucent (its background,
`--bg-panel-raised: #1F212B`, is a fully opaque hex color) — row content
was visually bleeding over its edge. Fixed with one override,
`#workspace-edit .edit-table th { z-index: 5; background:
var(--bg-panel-raised); }` — the explicit z-index lifts the header's
whole subtree (text + the v16 column-resize handles) above every row
regardless of DOM order; reasserting the background guarantees an
opaque paint at the seam.

## Verification

`py_compile` n/a (no backend changes this addendum), `node --check`
clean, `main.py --selftest` green. Frontend: loaded the real composed
page with a mocked `window.pywebview.api`; confirmed tab order via
`.suite-ws-tab` DOM order (`sync,transcribe,broll,edit,graphics`); for
each of the three settings stores, changed the fields, confirmed the
localStorage payload, then did a full page reload (a real relaunch
proxy — localStorage survives navigation) and confirmed every field
(including derived UI state: the HF-token row's visibility, the drop-
frame row's visibility/checked state, the energy-weight input's
disabled state) came back correctly. Confirmed a long-filename B-Roll
favorite truncates its name (`scrollWidth 413 > clientWidth 32`) while
its timecode renders fully inside the card's bounds
(`tcRect.right <= cardRect.right`). Confirmed `.edit-table th`'s
computed `z-index` is `5` and populated 25 fake Cuts rows to visually
confirm the header stays crisp/opaque while scrolled. No console errors
across any of these. Sync both `Studio Suite` copies.

(Note: addenda v19–v21 — B-Roll redesign, Blair Brander AI-generated
graphics + Gemini status, and the Sync workspace waveform/zoom/multi-
project work — shipped in code but were never backfilled into this file.
Not repeated here; see the code comments in the affected modules, which
cite "addendum v19/v20/v21" directly.)

# Addendum v22 — Blair Brander: imported-logo export crash, larger logo
scale, text position controls

Three bugs/requests reported against the Graphics workspace. All three
root-caused to Blair Brander's own `renderer.py`/`export.py` — patched
directly there (per-incident user authorization, same as the earlier
wipe-outro/lower-third-placement and PERF-2 fixes) rather than worked
around suite-side, since a real fix at the source also benefits the
standalone app.

## Imported logos crash video export

Preview and PNG export both render in-process and worked fine with a
custom/imported logo; only **video** export was broken. Root cause:
`export.export_video()` renders frames across a `multiprocessing.Pool`
(macOS default "spawn" start method), and each worker process gets its
own fresh import of `brand` — a logo registered at runtime via
`brander_bridge.register_custom_logo()` only ever mutates the PARENT
process's copy of `brand.LOGO_SOURCES`, so a worker rendering a frame
with that logo raised `KeyError` in `assets.load_transparent()`. Confirmed
by reproducing it faithfully (the bug only shows up when the
`brander_bridge` import is deferred inside a `if __name__ == "__main__":`
guard exactly like `main.py`'s own lazy `from backend.suite_api import
SuiteApi` — an earlier, unguarded-import test script gave a false
negative by accidentally re-registering the custom logo in the spawned
child via its own re-executed `__main__`).

Fixed with a new optional `extra_logo_sources` param on
`export.export_video()` (`Blair Brander/export.py`) — a
`multiprocessing.Pool(initializer=_register_extra_logos,
initargs=(extra_logo_sources or {},))` that merges the extra mapping into
each worker's `brand.LOGO_SOURCES` before it renders anything. Fully
backward compatible (defaults to `{}`, a no-op — the standalone app never
passes it and behaves identically to before). `brander_bridge.py` gained
a small accessor, `custom_logo_sources()` (`dict(_custom_logos)`), and
both `export.export_video(...)` call sites in `api_brander.py`
(`brander_export_video`, `brander_send_to_edit`) now pass
`extra_logo_sources=brander_bridge.custom_logo_sources()`.

## Logo scale — real ceiling was far below the slider's

`renderer.py` separately caps rendered logo height at `min(logo_h,
int(H * 0.32))`, regardless of what `scene["logo_height"]` (the slider)
requests — on the default 1920×1080 canvas that's ~346px, well under the
UI's old 640px slider max, so most of the slider's range silently did
nothing. Raised the cap to `H * 0.85` (renderer.py). Raised the ceiling
everywhere it's echoed to match: Studio Suite's `#gxLogoHeight` slider
(640→900), the standalone app's own `logo_scale_var` Tkinter `Scale`
(420→900), and `brander_gemini.py`'s `INT_FIELDS["logo_height"]` clamp
(640→900, so the Gemini "Apply" path can't disagree with the manual
slider).

## Text position controls (new capability)

Neither app had ANY way to move title/subtitle text off its computed
position (dead-center for Full Title Card; a coarse Top/Bottom ×
Left/Center/Right enum for Lower Third) — confirmed by grepping
`renderer.py` for any offset/position field before starting. Added two
new scene fields, `text_offset_x`/`text_offset_y` (pixels, signed,
default `0` — fully backward compatible), applied in `renderer.py`
to the whole title+subtitle block as one unit:
- Full Title Card: added directly to `cx`/`top`.
- Lower Third: added to `text_x`/`plate_top`/`plate_bottom` (computed
  BEFORE the plate is drawn), so the background plate follows the text
  rather than the text drifting off it.
- The divider and subtitle both derive their position from the same
  `top`/`text_x`/`cx`, so they follow automatically with no separate
  edit needed.
- The logo is unaffected (it has its own independent placement control).

Added to both `default_scene()`s (`brander_bridge.py` and the standalone
`app.py`) and to `brander_gemini.py`'s `INT_FIELDS` (±400px clamp). Two
new range sliders, "Text position (horizontal)"/"(vertical)"
(`#gxTextOffsetX`/`#gxTextOffsetY`, -400..400px), added to Studio Suite's
Text & Style block (`shell.html`/`suite.js`) — applies to both layouts,
not just Lower Third, since the offset math is layout-generic. No
standalone-app Tkinter UI was added for these two fields, matching the
existing precedent that not every scene field has a matching Tkinter
control (`title_size`/`subtitle_size` are suite-only too) — the app.py
edit was limited to what the user asked for (default_scene() sync +
raising the existing logo-scale slider to match its own renderer.py
cap).

## Verification

`py_compile` clean on every touched `.py` file (`Blair Brander/export.py`,
`renderer.py`, `app.py`; `Studio Suite/backend/brander_bridge.py`,
`api_brander.py`, `brander_gemini.py`), `node --check` clean on
`suite.js`, full pytest suite green (56/56, unaffected by this addendum).
Empirically verified (not just "compiles"): reproduced the original
export crash faithfully in a `__main__`-guarded repro script, confirmed
it no longer raises after the fix, and confirmed the logo is genuinely
present in the exported frame (RGB bbox diff against a no-logo export at
the same settled timecode). Confirmed the raised logo cap actually lets
requested heights up to ~918px render on a 1080-tall canvas (previously
clamped ~346px), by diffing a logo-vs-no-logo render's bbox height at
several requested sizes. Confirmed text_offset_x/y visibly repositions
the title+subtitle block (and, for Lower Third, its plate) in the
expected direction/magnitude via bbox diffs against an unshifted render,
for both layouts.

## Incident: this addendum's own verification polluted real user data

While verifying the imported-logo export fix above, the diagnostic
script called `register_custom_logo()` against the REAL
`assets/logos/`/`custom_logos.json` (not a throwaway copy), leaving a
junk `"Custom: test_custom_logo"` entry + PNG in the user's actual logo
library — surfaced when the user reported imported logos "aren't
clearing." Removed by hand (file + registry entry) once found. Test
scripts touching `register_custom_logo`/any real-data path should use a
throwaway `paths`/tempdir in future, not the live `assets/` tree.

## Follow-up: "Elliptical" vignette shape wasn't actually an ellipse

Reported separately, same workspace. Root cause, in `renderer.py`'s
`_vignette_mask()`: "Elliptical" computed `math.hypot(dx, dy) /
math.hypot(cx, cy)` — a EUCLIDEAN distance normalized by the corner
distance. Iso-falloff contours of that formula are circles (same family
as "Circular", which normalizes by the min-edge distance instead) just
scaled to reach the corners — so on a 16:9 canvas it rendered a big
circle, leaving the top/bottom edges far brighter than the left/right
edges instead of an oval that vignettes the whole border evenly.
Rendered all three shapes at 100% strength before touching anything to
confirm visually (Circular/Rectangular were already correct).

Fixed by normalizing dx and dy SEPARATELY against cx/cy before combining
(`math.hypot(dx / cx, dy / cy)`) — the standard elliptical-vignette
formula, whose iso-falloff contours are true ellipses matching the
canvas aspect ratio, reaching r=1 at all four edge midpoints. Removed
the now-unused `max_r_elliptical` local. `Circular`/`Rectangular` code
paths untouched.

Verification: `py_compile` clean, full pytest suite green (56/56,
unrelated to this render-only change), re-rendered all three shapes
after the fix and visually confirmed Elliptical now traces a symmetric
oval matching the 1920×1080 aspect ratio while Circular/Rectangular are
byte-for-byte unaffected (their code paths weren't touched).

# Addendum v23 — Remove imported logos; subtle logo scale-up animation

Two requests against the Graphics workspace's Logo panel.

## Remove a custom (imported) logo

There was no way to get rid of an imported logo from within either app —
only by hand-editing `assets/logos/custom_logos.json` and deleting the
file, which is exactly what the pollution incident above required. Added
a real removal path, suite-side only (no sibling-file changes needed —
custom-logo storage/registry has always lived in `brander_bridge.py`,
not in Blair Brander's own `brand.py`):

- `brander_bridge.remove_custom_logo(name)`: pops the entry from
  `brand.LOGO_SOURCES` and the in-memory `_custom_logos` registry,
  best-effort deletes the file, re-persists `custom_logos.json`. Refuses
  (returns `(False, error)`) for anything not in `_custom_logos` — a
  built-in logo can't be removed this way.
- `api_brander.brander_remove_custom_logo(name)`: thin wrapper, same
  error-dict contract as every other Brander API method.
- Frontend: the Logo/seal `<select>` (`#gxLogo`) now sits next to a
  `#gxRemoveLogo` "✕" button, shown only while the CURRENTLY SELECTED
  value starts with `"Custom: "` (mirrors the server-side refusal — a
  built-in or "None" never shows it). Removing the active logo resets
  `scene.logo` to `"None"` and re-renders the preview; removing a
  DIFFERENT custom logo than the one currently applied leaves the active
  selection alone. Persistence for logos you keep is unchanged — only
  explicit removal clears one out now, closing prior gap.

## Logo scale-up animation ("logo_grow")

Neither app could animate the logo's SCALE at all before (only alpha and
a small vertical offset during intro/outro). Added a new bool scene
field, `logo_grow` (default `False`), applied in `renderer.py`'s logo
section: once the logo's own intro settles (past `logo_in_end`), it
eases from 1.0x up to a modest, fixed +8% by t=1.0
(`ease_out_cubic(phase_progress(logo_in_end, 1.0, t))`), then holds at
that size for the rest of the plateau. Deliberately confined to the
normal `0..1` t window rather than extending into the `hold_seconds`
tail — `export._rle_times` relies on every hold-tail frame being
byte-identical (t clamped to 1.0) to skip re-rendering it, an
intentional perf optimization documented in `export.py`; animating
through the hold would defeat that, so — like every other animation
branch in `render_frame` — this one only moves within the pre-hold
timeline. Applied to `logo_img` BEFORE `lw`/`lh`/`positions` are
computed, so it grows outward from whichever point its placement already
anchors (dead-center for "-center"/"center" placements, the margin
corner otherwise) instead of drifting toward one corner.

Added `logo_grow: False` to both `default_scene()`s (`brander_bridge.py`,
standalone `app.py`) and to `brander_gemini.py`'s `BOOL_FIELDS`. New
checkbox, "Slow subtle scale-up after intro" (`#gxLogoGrow`), in Studio
Suite's Logo panel. No standalone-app Tkinter checkbox added, same
suite-only-control precedent as the text-offset sliders in v22.

## Verification

`py_compile` clean on every touched `.py` file, `node --check` clean,
full pytest suite green (56/56). Empirically verified — not just
"compiles": `remove_custom_logo` tested end-to-end (register a throwaway
logo in a real-but-disposable slot, confirm removal drops it from both
`brand.LOGO_SOURCES` and the persisted JSON, confirm a built-in logo is
correctly refused, confirm the removal call itself leaves no residue —
this test is self-cleaning by construction). `logo_grow` verified by
diffing a with-logo render against a no-logo render at the same t to
measure the logo's actual bbox height: flat at the baseline size when
`logo_grow` is unset at t=0.97/1.0, and growing monotonically from the
baseline at `t=logo_in_end` up to ~+8% by `t=1.0` when set — matching
the formula's predicted values almost exactly.

# Addendum v24 — logo_grow now animates through the whole hold tail

Follow-up to v23: v23's logo_grow capped at +8% once `t` reached 1.0 (the
end of `duration`) and then held flat for the rest of `hold_seconds` —
reported as not animating "the whole time the logo is present." Root
cause was architectural, not a simple parameter tweak: `render_frame`'s
`t` is clamped to 1.0 for the ENTIRE hold tail by design (every animation
branch keys off it), and `export._rle_times` relies on that clamping —
it collapses the whole hold tail into ONE render repeated `count` times,
since consecutive frames sharing `t == 1.0` are provably byte-identical.
Confirmed with the user before changing this (see
[[sibling-app-file-exception]]) given the real trade-offs: slower export
for scenes using the effect, and a pre-existing timing mismatch in the
live preview that needed fixing alongside it for the preview to actually
show the new behavior.

## renderer.py / export.py (sibling files)

- `render_frame(scene, t=1.0, elapsed_seconds=None)`: new optional param.
  `elapsed_seconds` is real elapsed time across the WHOLE clip (duration
  + hold_seconds), used ONLY by logo_grow — every other animation branch
  is untouched and still keyed on `t` alone. When provided, logo_grow's
  `phase_progress` runs from `logo_in_end * duration` to
  `duration + hold_seconds` in real seconds, instead of from
  `logo_in_end` to `1.0` in the t-domain — so it keeps easing for as long
  as the logo is actually on screen. `None` (callers that don't pass it —
  `render_still`, `export_png`, any single-snapshot use) falls back
  exactly to the old t-only-capped formula; those call sites are
  unmodified.
- `export._frame_times` now returns `(t, elapsed_seconds)` pairs instead
  of bare `t`s. `_rle_times(frame_times, exact=False)` dedupes on `t`
  alone by default (unchanged fast path — safe because every OTHER
  animation ignores elapsed_seconds, so same-`t` frames are still
  provably identical) or on the full `(t, elapsed)` pair when
  `exact=True`, which never collapses the hold tail. `export_video`
  passes `exact=bool(scene.get("logo_grow"))` — scenes without the
  effect render exactly as before (verified byte-size and per-frame
  height unchanged); scenes with it render every hold-tail frame
  individually (the cost the user explicitly accepted).

## Studio Suite preview (brander_bridge.py / api_brander.py / suite.js)

`render_preview`/`brander_preview` gained a matching `elapsed_seconds`
param, threaded straight through to `render_frame`. While wiring this up,
found and fixed a REAL pre-existing bug in `suite.js`'s scrubber: the
Play button already computed its own "current position" as a fraction of
`duration + hold_seconds` (its timecode label, `${(t*total).toFixed(2)}s`,
already assumed this) but then forwarded that raw fraction straight to
the backend AS `t` — which the backend has always treated as a fraction
of `duration` ALONE. So the live preview's timing silently disagreed
with what export actually produces during the hold tail (it kept easing
toward "settled" there instead of already being frozen, per the true
contract). Fixed with one new helper, `gxScrubFracToTimes(scene, frac)`,
that correctly converts the scrubber's fraction into `(t, elapsed)`
before calling `brander_preview` — the two existing callers (the manual
scrub `input` handler, the Play `setInterval` loop) needed NO changes,
since they were already computing the right conceptual "fraction of
total lifetime" value; they just weren't being translated correctly
before this fix existed to do it.

## Verification

`py_compile`/`node --check` clean, full pytest suite green (56/56).
Empirically verified with real exports (not just unit-level checks):
- Unit-level: `_rle_times` produces one run of count 10 for a 10-frame
  hold tail when `exact=False` (unchanged), and 10 separate runs of
  count 1 when `exact=True` (never collapses).
- Exported a real .mov with `logo_grow=True`, duration=0.5s,
  hold_seconds=2.0s (2.5s total @ 10fps) and measured the logo's bbox
  height at 5 points across the ENTIRE clip via real ffmpeg frame
  extraction: 296 → 306 → 315 → 319 → 320px, smoothly increasing all the
  way to the very end of hold — not flat after the old ~0.5s cutoff.
- Same export with `logo_grow=False`: bbox height flat at 296px at both
  the start and end of hold (no regression), and the file is ~3x smaller
  (686KB vs 2.05MB) — confirms the RLE collapse still applies when the
  effect isn't in use.
- Preview API: called `render_preview` at 4 scrub positions across the
  hold tail, once with `elapsed_seconds` and once with `None` (old
  behavior) at the same `t`. With elapsed: 157→162→164px, smoothly
  approaching the cap. Without: flat 164px (fully capped) at every
  position — demonstrating the preview fix actually changes what's
  rendered, matching the export behavior above.

# Addendum v25 — Card Eater workspace (new sixth workspace, placed before Sync)

A `CardEater/` repo was added to the suite folder: a standalone macOS card-
ingest utility (copy camera-card footage/photos to one or more destination
drives, renamed per template, verified byte-for-byte with BLAKE3). Unlike
the other four sibling apps, it's a **Tauri (Rust backend) + React app**,
not Python — so it has no venv to shell out to and no `default_scene()`-
style module to import in-process. The user was asked how to integrate it
(launch as a separate window / embed UI with a Rust sidecar server / full
Python port) and chose the largest option: **a full line-for-line Python
port** of its Rust backend, wired into `SuiteApi` as a sixth workspace, with
a new vanilla-JS frontend panel replacing its React UI. `CardEater/` itself
is untouched — the port is read-only against it, same "never modify a
sibling app" rule, extended here to a sibling that happens to be Rust/React
rather than Python.

## New backend modules (`backend/cardeater_*.py`)

Each ports one Rust source file 1:1 — see each module's own docstring for
exactly what it mirrors and where the Python port deliberately simplifies:

- `cardeater_db.py` — SQLite schema (favorites, naming_templates, jobs,
  job_destinations, job_files) matching the original's 3 migrations
  collapsed into one CREATE-TABLE pass (no versioned-migrations system —
  this schema was ported wholesale, not evolved in place), plus CRUD and
  CSV export (ports `db.rs`).
- `cardeater_naming.py` — the naming/collision engine: token validation,
  per-date-group `{Seq}` sequencing, the destination-collision scan that
  takes the overall max across every distinct literal in a date group (not
  just the first file's — the mixed-extension-batch bug the original's own
  test suite calls out), folder-name resolution (ports `naming.rs`).
- `cardeater_verify.py` — BLAKE3 hashing via the `blake3` PyPI package
  (Rust bindings, so hashing speed matches the original), streaming
  hash_file/verify_pair (ports `verify.rs`).
- `cardeater_metadata.py` — batched `exiftool -j` invocation (chunks of
  200) for file creation-time resolution, filesystem ctime/mtime fallback
  (ports `metadata.rs`).
- `cardeater_card.py` — has_dcim/looks_like_camera_card (DCIM or Sony-style
  PRIVATE folder), scan_card_files, open-folder-as-card fallback (ports
  `card_detect.rs`).
- `cardeater_volume_watcher.py` — background daemon thread polling
  `/Volumes` every 1.5s, boot-volume exclusion via `os.path.realpath`,
  single-active-card constraint (ports `volume_watcher.rs`). **Difference
  from the original:** no Tauri event bus here, so this just maintains a
  thread-safe `CardRegistry` that the frontend polls (`suite_cardeater_
  get_active_card`, every 1.5s) and diffs against the last-seen card id
  itself — same effect as the original's `card-mounted`/`card-unmounted`
  events, via polling instead of push (matches Studio Suite's existing
  `pollJobs` convention rather than introducing a new push mechanism).
- `cardeater_copy.py` — the copy/verify engine: disk-space preflight,
  `JobControl` (running/paused/cancelled), one process-wide semaphore
  capping concurrent DESTINATIONS at 8 (copy work is sequential *within* a
  destination — the source card's own read speed is the bottleneck), a
  2-worker verify pool per destination overlapping hashing with the next
  file's copy, one retry on a hash mismatch, pause/cancel checked once per
  file boundary only (never mid-file — a partially-copied file is never
  rolled back), job/destination finalization (ports `copy_engine.rs`).
  **Simplification:** disk-space checking uses `shutil.disk_usage(path)`
  directly instead of the original's manual longest-matching-mount-prefix
  logic — Python's `disk_usage` already resolves to the real containing
  filesystem for any given path, making the original's own mount-table
  scan unnecessary here. **Difference:** per-destination live progress
  (MB/s, ETA, current filename, any destination-level error message) is
  kept in an in-memory dict (`CardEaterState.live`) merged into
  `get_job_status`'s response, rather than emitted as events — same
  "poll instead of push" reasoning as the volume watcher above.
- `api_cardeater.py` — `CardEaterMixin` (the `suite_cardeater_*` js_api
  surface, one method per Tauri command in `commands.rs`) plus
  `CardEaterState` (owns the DB connection, job-control/live-progress
  dicts, and the card registry). Wired into `SuiteApi`'s MRO in
  `suite_api.py` alongside the other five mixins; `__init__` opens
  `assets/cardeater.sqlite3` (new `paths.CARDEATER_DB`) and starts the
  volume-watcher thread. File preview (image/video/audio) reuses RCS's own
  `PreviewServer` (`self.preview_server.url_for(path)`) — the same local
  byte-range HTTP server the Sync/B-Roll workspaces already use for media
  preview, not a new server.

New dependency: `blake3==1.0.9` added to `requirements.txt`.

## Frontend (`shell.html` / `suite.css` / `suite.js`)

- New tab `data-ws="cardeater"`, placed **before** Sync in the top menu
  per the request. `#workspace-cardeater` gets its own 3-column grid
  (`300px 1fr 300px`: naming/destinations rail | file selector | copy
  queue) rather than the other workspaces' 2-column `340px 1fr`, so job
  progress stays visible without a modal while a copy is running.
- `suite.css`: new `.suite-ce-*` component styles built from the suite's
  own existing design tokens (`--bg-panel`, `--amber`, `--hairline`, …) —
  deliberately NOT the original React app's Tailwind "Athletic Blue" dark
  theme, for visual consistency with the rest of the suite. Queue rows
  reuse `.suite-job`/`.suite-job__status`/`.suite-job__bar`'s existing
  visual language (jobs drawer) rather than inventing new job-card styles,
  plus an added verify-progress overlay bar and MB/s·ETA line.
- `suite.js`: a vanilla-JS port of the React app's component tree/zustand
  stores onto `S.cardEater` — file selector (grouped-by-folder, shift-click
  range select, extension/date filters), naming template editor (live
  debounced preview + destination-collision check), destinations +
  favorites, job launcher (disk-space preflight before starting), copy
  queue (1s poll while any job is non-terminal, pause/resume/cancel,
  "safe to remove" check polled every 3s), job-history modal + CSV export,
  file-preview modal. No Tauri event bus, so every original `listen(...)`
  subscription in `useTauriEvents.ts` becomes a poll-and-diff instead
  (`cePollActiveCard`/`cePollJobs`), matching this file's own existing
  `pollJobs`/`ensurePolling` convention rather than adding a second one.

## Deliberate scope differences from the original React/Tauri app

- No native OS mount-event API (DiskArbitration) — same polling approach
  the original itself chose (`/Volumes` polling, not FSEvents), just done
  in Python instead of Rust.
- "Open Source" button is a placeholder toast (no Finder-reveal hook
  wired up yet) — the file Preview button covers per-file inspection;
  full Finder integration deferred to a future addendum if requested.
- Multi-card simultaneous ingest is out of scope here too, matching the
  original's own Phase-1 scope note (`card_detect.rs`/`volume_watcher.rs`
  doc comments): a second inserted card is ignored while one is active.

## Verification

Real, executed tests (no mocks) mirroring the original Rust test suite's
own key scenarios, run directly against the ported Python modules:
- Per-date-group `{Seq}` sequencing independent across dates; destination-
  collision scan resuming from the highest existing sequence; the mixed-
  extension re-import regression scenario (5 files, 3 extensions, two full
  "import" passes) — zero collisions, all 10 files present with correct,
  untouched content on disk.
- All three folder-collision states (no conflict / exists-empty /
  exists-non-empty) against real directories.
- `manual` date source with no manual date supplied raises the expected
  hard error.
- A REAL end-to-end job: `start_job` → polling `get_job_status` → a 5MB +
  small-file card copied to TWO real destinations, BLAKE3-verified, with
  the actual bytes on disk read back and compared byte-for-byte against
  the source in both destinations.
- Disk-space preflight against the real filesystem (absurd requirement
  fails, 1 byte passes).
- Favorites and naming-template CRUD (including upsert-by-name) round-
  tripped through real SQLite; job-history CSV export format checked.
- `main.py --selftest` (composes the real page, runs the full pytest
  suite): 56/56 passed, unchanged from before this addendum.
- Frontend: loaded the real composed `_generated/index.html` in a browser,
  injected a mock `window.pywebview.api` (since there's no live Python
  backend outside the real app) covering every `suite_cardeater_*` method,
  and drove the actual UI — switched to the Card Eater tab, added a
  destination from a favorite, typed an event name and confirmed the
  debounced live naming preview updated correctly, toggled "use source
  filename" and confirmed the file-template field updated/disabled
  correctly, opened the file-preview modal (video element got the mocked
  preview URL), and started a mock job — confirmed the copy queue row
  rendered progress bars, MB/s, ETA, and pause/cancel controls correctly.
  Zero console errors throughout.

# Addendum v26 — top-menu label "Copy"; copy queue merged into the Jobs drawer

Two follow-up requests: rename the top-menu tab from "Card Eater" to
"Copy" (`shell.html` only — the workspace panel's own `<h2>` title stays
"Card Eater", scope was the menu specifically), and merge the Copy
workspace's own right-hand "Copy Queue" column into the suite-wide Jobs
drawer (the same one Transcribe/B-Roll/Graphics/Sync background work
already shows up in), rather than keeping two separate places that show
job progress.

## Backend (`cardeater_copy.py` / `suite_api.py`)

- `cardeater_copy.list_as_generic_jobs(state)`: represents every copy-job
  destination created THIS session (i.e. still a key in
  `state.job_controls`) as a generic job dict matching jobs.py's
  `Job.to_dict()` shape (`id`, `kind: "cardeater_copy"`, `label`, `status`,
  `progress`, `detail`, `error`, `result`, `created_at`, `finished_at`)
  plus two extras the drawer's click-handlers need: `cardeater_job_id`
  (for pause/resume/cancel, which act on the whole job) and
  `cardeater_dest_id`. Copy+verify are blended into one 0–100 `progress`
  figure (two roughly-equal phases per file) so the bar reflects real work
  before the first file finishes verifying. `SuiteApi.suite_list_jobs`
  now returns `list_as_generic_jobs(self._cardeater) + self.jobs.list_jobs()`
  instead of just the latter.
- `cardeater_copy.clear_finished(state)`: the Card-Eater half of "Clear
  Finished" — drops terminal (complete/failed/cancelled) destinations from
  `state.job_controls`/`state.live` (the session-visible set
  `list_as_generic_jobs` reads), leaving the underlying DB rows untouched
  so job history/CSV export is unaffected. Wired into
  `SuiteApi.suite_clear_finished_jobs` alongside `self.jobs.clear_finished()`.
- **Real bug found and fixed while testing this**: pausing a copy job
  (`suite_cardeater_pause_job`) only ever flipped the in-memory
  `JobControl` atomic — it never touched `job_destinations.status` in
  SQLite, which is what `get_job_status`/`list_as_generic_jobs` actually
  report. A paused job would silently keep reporting "running" forever
  (progress just stops advancing), so the drawer could never show a
  Resume button. This mirrors the ORIGINAL Rust `copy_engine.rs`'s own
  `set_job_control`/`wait_for_turn`, which have the exact same gap — but
  that file is the untouched CardEater/ sibling repo, while
  `cardeater_copy.py` is Studio-Suite-owned, so it was fixed here directly
  rather than replicated faithfully: `_wait_for_turn` now marks
  `job_destinations.status = "paused"` the moment it starts blocking, and
  reverts to `"running"` on resume (or leaves it be on cancel, since a
  cancelled destination moves straight to its own terminal status).
  Verified with a real multi-file (4×40MB) copy job: pausing after
  `start_job` returns lands reliably before the 2nd file's boundary check,
  and the merged listing shows `status: "paused"` before resuming to
  completion.

## Frontend (`shell.html` / `suite.css` / `suite.js`)

- `shell.html`: top-menu tab text → "Copy" (`data-ws="cardeater"`
  unchanged — no internal id/class renaming). The Copy workspace's
  right-hand `<aside class="suite-controls suite-ce-queue-col">` (Copy
  Queue panel) removed entirely; `#workspace-cardeater`'s grid reverts to
  the standard 2-column `300px 1fr` (rail | file selector) other
  workspaces use. The "safe to remove card" indicator (card-specific, not
  job-specific) moved into the Card block in the left rail instead of
  living in the now-gone queue column.
- `suite.css`: removed the now-dead `.suite-ce-queue-col`/`.suite-ce-queue__list`/
  `.suite-ce-qrow*` rules; kept `.suite-ce-queue__safe` (still used for the
  relocated safe-to-remove indicator). Added `.suite-job__status.is-paused`
  (muted/neutral, the only job kind that can be paused).
- `suite.js`:
  - `JOB_ICONS`/`JOB_KIND_LABELS` gained `cardeater_copy: "⧉"` / `"Copy"`.
  - `renderDrawer()`: the generic top-right ✕ cancel button is suppressed
    for `cardeater_copy` (it would call the wrong backend endpoint); a new
    per-kind `extras` block instead renders Pause/Resume/Cancel/Open Folder
    depending on status, mirroring the `broll`/`sync` "View ..." button
    pattern already used for other kinds' done-state actions.
  - Delegated click handler gained `cc-pause`/`cc-resume`/`cc-cancel`
    (look up the full job dict by id to get `cardeater_job_id`, call
    `suite_cardeater_pause_job`/`resume_job`/`cancel_job`, then re-poll)
    and `cc-open-folder` (toast the resolved path).
  - `onJobDone`/`pollJobs`'s existing transition-detection loop (shared
    across every kind) is reused as-is for `cardeater_copy`'s
    running→done/error/cancelled toasts; a new `ceShowSummaryForJob(job)`
    hooks the same transition point to additionally pop the existing
    per-destination Job Summary modal (file counts, verification result) —
    the one piece of the old Copy Queue's UX that's more than a generic
    progress bar, kept as an addition on top of the shared drawer rather
    than folded into it.
  - Removed entirely (now dead): `S.cardEater.jobs`/`prevDestStatus`,
    `ceEnsureJobPolling`/`ceMaybeStopJobPolling`/`cePollJobs`/`ceRenderQueue`,
    and the `CE_TERMINAL` constant — `ceHandleStartJob` now just calls the
    already-existing `ensurePolling()`/`openDrawer()` (same as
    `brander_video`/`brander_send` already do) instead of running a second,
    parallel poll loop.

## Verification

- `main.py --selftest`: 56/56 passed, unchanged.
- Real, executed backend test (no mocks): started a real copy job,
  confirmed `suite_list_jobs` returns exactly one `cardeater_copy` entry
  with the correct `cardeater_job_id`/label/progress/result fields through
  to completion (`status: "done"`, `progress: 100`, verified count
  correct); confirmed `suite_clear_finished_jobs` prunes it from the live
  listing while `suite_cardeater_list_jobs` (job history) still shows it;
  confirmed pause/resume against a real multi-file job actually reaches
  `status: "paused"` in the merged listing (this is what caught the bug
  above) and resumes to completion.
- Frontend: recomposed the real page, mocked `window.pywebview.api`, and
  drove the actual Jobs drawer — confirmed the top-menu tab reads "Copy",
  opened the drawer and confirmed a `cardeater_copy` entry renders with
  the right icon/label/status/progress/detail and Pause/Cancel/Open Folder
  buttons (no duplicate generic ✕), clicked Pause and confirmed the status
  pill flips to "paused" and the button swaps to Resume (and back on
  Resume), then simulated a done transition and confirmed the Job Summary
  modal opens with the correct file/size/verification counts. Zero console
  errors throughout.

# Addendum v27 — "Send to B-Roll Analyzer" for completed Copy jobs

A completed (successful) Card Eater copy job can now be sent straight
into a B-Roll analysis of the folder it just copied into, from either
place that shows a finished copy job: the Jobs drawer's per-job actions
(status `done`) and the Job Summary modal that pops up on completion.
Only offered on success — a `failed` job's summary has no "Send to
B-Roll Analyzer" button, since there's no guarantee every file actually
landed.

## Implementation (frontend-only — no backend changes needed)

`ceSendToBroll(folderPath)` (`suite.js`) reuses B-Roll's OWN existing
entry point rather than adding a new backend method: calls
`broll_start(folderPath)` (`api_broll.py` — validates the folder exists,
starts a real subprocess analysis job, returns immediately with a
`job_id`; already used by the B-Roll workspace's own Analyze button with
no required options beyond the folder), sets `#bFolder`'s value directly
(a plain readonly `<input>`, confirmed no B-Roll-specific re-render is
needed for this), then `switchWs("broll")` + `ensurePolling()` +
`openDrawer()` — the same three calls `brander_video`/`brander_send`
already use after starting their own background jobs, so the user lands
on the B-Roll workspace watching the SAME merged Jobs drawer as the copy
job it came from.

Two call sites, both passing the copy job's `result.resolved_path`
(the real destination folder, not the top-level chosen destination):
- `renderDrawer()`'s `cardeater_copy` extras, alongside Open Folder,
  gated on `j.status === "done"`.
- `ceRenderSummary()`, alongside the existing "✓ All files verified"
  line, gated on `!failed`. Its click handler (added to the Summary
  modal's existing backdrop-click listener) closes the modal before
  calling `ceSendToBroll`, matching how switching workspaces elsewhere
  in the suite always closes whatever modal is open first.

## Verification

`main.py --selftest` + full pytest: 56/56, unchanged (no backend touched).
Frontend: recomposed the real page, mocked `broll_start` to record its
calls, drove a job through running → done — confirmed the button is
ABSENT while running, appears in the drawer once done, the Summary modal
also shows it, and clicking either one calls `broll_start` with exactly
the resolved path (no extra options), switches to the B-Roll workspace,
and populates `#bFolder` with that same path. Zero console errors.

# Addendum v28 — BRAW compatibility, Phase 0 (runtime detection) + Phase 1 (proxy jobs)

First step of a multi-phase plan to make `.braw` (Blackmagic RAW) files
usable across the suite. `.braw` is a closed, proprietary raw format —
no open-source decoder exists — so the only non-open-source dependency
this whole feature needs is Blackmagic's free **Blackmagic RAW SDK**
(dev headers) plus the **Blackmagic RAW runtime** it installs (bundled
with DaVinci Resolve, or standalone). This addendum adds the detection
scaffolding and the proxy-job plumbing; it deliberately does NOT touch
any workspace's preview/thumbnail/transcription/analysis path yet, add
`.braw` to any extension allowlist, or change the frontend — those are
later phases (transparent substitution, allowlist gating, export,
Jobs-drawer UI) of the same plan, tracked separately.

## Implementation

- **`backend/braw_bridge.py`** (new, mirrors `brander_bridge.py`'s role
  as the one bridge module for an external dependency, but shells out to
  a separate process instead of importing in-process — Blackmagic's SDK
  is proprietary and never linked directly into the suite venv):
  - `sdk_runtime_path()` / `tool_built()` / `braw_available()` /
    `status()` / `unavailable_reason()` — Phase 0. Detects the
    Blackmagic RAW runtime at a handful of documented macOS install
    locations (`_SDK_RUNTIME_CANDIDATES`) and checks for this suite's
    own compiled proxy-generation helper at `paths.BRAW_TOOL_BIN`.
    Neither half exists in a normal dev checkout — `braw_available()`
    returning `False` is the expected, fully-supported state, exactly
    like B-Roll/Transcribe's own "sibling venv not found" checks
    (`api_broll.py`'s `BROLL_PYTHON` guard).
  - `request_proxy(job_manager, source_path)` / `_run_proxy_tool(...)`
    — Phase 1. Pre-flight-checks the path (exists, `.braw` extension),
    returns an already-cached proxy immediately with no job, otherwise
    starts a `"braw_proxy"` **thread job** (not a subprocess job in
    `jobs.py`'s sense — there's no sibling venv interpreter here, just
    one compiled binary) whose function shells out to
    `paths.BRAW_TOOL_BIN` and reads its stdout through the EXACT SAME
    JSON-lines protocol every Python subprocess worker speaks
    (`backend/workers/worker_protocol.py`), so the compiled tool and a
    Python worker are indistinguishable to anything parsing their
    output. Cooperative cancellation matches `jobs.py`'s own subprocess
    handling (terminate, escalate).
- **`backend/braw_proxy_cache.py`** (new): a centralized
  `assets/proxies/index.json`, NOT a sidecar next to the source file —
  unlike the transcriber's `.ivt-cache.json`, BRAW source files
  routinely live on read-only/removable camera media where writing
  anything next to them isn't reliable. Entries are keyed by a SHA-1 of
  the source's absolute path (never the path itself — arbitrary user
  data, unsafe as a filename) and invalidated by `(size, mtime)`
  fingerprint with the same epsilon-tolerant mtime comparison as
  B-Roll's `result_cache.py` (`is_current`), plus a check that the
  proxy file itself still exists on disk.
- **`backend/paths.py`**: added `PROXIES_DIR` (registered in
  `ensure_suite_dirs()`), `BRAW_TOOL_DIR`/`BRAW_TOOL_BIN` (where the
  not-yet-built compiled helper is expected to live).
- **`backend/jobs.py`**: added `"braw_proxy"` to `_kind_limits` (default
  1, same throttle rationale as `"transcribe"` — heavy CPU/GPU decode
  work, not worth running several at once) and updated the module
  docstring/`Job.kind` comment to document it as a thread job whose
  real work happens in a child process.
- **`tools/braw/README.md`** (new): the exact CLI contract
  (`braw_proxy_tool <source.braw> <output.mov>`, JSON-lines stdout,
  cancellation via SIGTERM) that whoever builds the real compiled tool
  next (needs Xcode + the actual SDK, not assumed present in every dev
  environment) must satisfy — `braw_bridge.py` is already written
  against this contract, so the tool is a drop-in once it exists; no
  suite code changes needed.
- No sibling app file was touched — this is entirely suite-owned code,
  same as `brander_bridge.py`.

## Verification

`main.py --selftest` + full pytest: 195/195 passed (was 169; +26 new
tests in `tests/test_braw_bridge.py` and `tests/test_braw_proxy_cache.py`,
zero regressions elsewhere). New tests cover: SDK/tool detection in
every combination (neither/one/both present), `unavailable_reason()`'s
two distinct messages, the proxy cache's fingerprint hit/miss/tolerance/
missing-file cases (mirroring `result_cache.py`'s own test style), and
`request_proxy` end-to-end against a small fake stand-in tool script
that speaks the documented `worker_protocol` contract — covering the
cached-shortcut path, the unavailable-error path, a full job run through
to `"done"` with the result registered in the cache, a tool-failure run
reaching `"error"` with the tool's own message surfaced, and the
`"braw_proxy"` kind's one-at-a-time throttle actually queuing a second
job. No real BRAW SDK, runtime, or sample media was available in this
session (tracked as an open item) — nothing above exercises an actual
BRAW decode; that only becomes possible once the compiled tool in
`tools/braw/` is built against the real SDK.

# Addendum v29 — `braw_proxy_tool` built and verified against a real BRAW SDK

Follow-up to v28: the user obtained and installed the free Blackmagic
RAW SDK, located at `/Applications/Blackmagic RAW/Blackmagic RAW SDK/`
on their machine (alongside the standalone Player app and Speed Test
app under the same `/Applications/Blackmagic RAW/` directory — all
three, plus DaVinci Resolve Studio, bundle their own copy of the
`BlackmagicRawAPI.framework` runtime, giving `braw_bridge.py`'s
detection five real, confirmed candidates instead of the original two
unverified guesses). This addendum implements and — critically —
*actually builds and runs* the tool `tools/braw/README.md` (v28)
specified only as a contract for someone else to satisfy later.

## Implementation

- **`tools/braw/braw_proxy_tool.mm`** (new, Objective-C++): opens a
  `.braw` clip via `IBlackmagicRawFactory`/`IBlackmagicRaw`/
  `IBlackmagicRawClip`, decodes every frame sequentially (one in flight
  at a time via a `dispatch_semaphore`, deliberately simpler than the
  SDK's own pipelined `ProcessClipCPU` sample — see the tool's own
  header comment for why), requesting `blackmagicRawResourceFormatBGRAU8`
  specifically so it lines up with `CVPixelBuffer`'s native
  `kCVPixelFormatType_32BGRA` with no channel-reorder step. Frames are
  copied into an `AVAssetWriterInputPixelBufferAdaptor` (H.264 video);
  audio (`IBlackmagicRawClipAudio`, when present) is read in ~1-second
  chunks (mirroring the SDK's own `ExtractAudio` sample) and appended as
  linear PCM via a second `AVAssetWriterInput`. Emits the exact
  `worker_protocol` JSON-lines contract v28 already specified
  (`progress`/`result`/`error`), so `braw_bridge._run_proxy_tool`
  (written against that contract before this tool existed) needed zero
  changes. `SIGTERM`/`SIGINT` flip an atomic flag polled between
  frames/audio chunks so cancellation unwinds promptly and deletes any
  partial output rather than needing `jobs.py`'s `SIGKILL` escalation.
- **`tools/braw/build.sh`** (new): compiles `braw_proxy_tool.mm` +
  the SDK's own `BlackmagicRawAPIDispatch.cpp` (never copied into this
  repo — compiled straight from wherever the SDK is actually installed,
  auto-detected or overridden via `BRAW_SDK_INCLUDE`) with `clang++`,
  linking `Foundation`/`AVFoundation`/`CoreMedia`/`CoreVideo`/
  `CoreAudio`/`CoreFoundation`. No Xcode project — a plain script,
  matching this suite's general preference for simple build scripts
  over heavier tooling for internal-only tools.
- **`backend/braw_bridge.py`**: `_SDK_RUNTIME_CANDIDATES` updated from
  two unverified guesses to five real, confirmed install locations
  (Blackmagic RAW Player.app, the SDK's own `Mac/Libraries/` copy, and
  DaVinci Resolve, with the original two guesses kept as fallbacks for
  other machines/SDK versions).
- **`.gitignore`**: the compiled `tools/braw/braw_proxy_tool` binary
  itself is excluded (machine-specific build artifact, like every
  `.venv/`) — only the source and build script are tracked.
- **`tests/test_braw_proxy_tool_real.py`** (new): a REAL integration
  test — unlike `test_braw_bridge.py`'s fake stand-in tool, this drives
  the actual compiled binary against the actual `sample.braw` clip the
  SDK ships (`.../Blackmagic RAW SDK/Media/sample.braw`), skipped (not
  failed) if either isn't present on the machine running the suite —
  same policy `test_sync_peaks.py` already uses for missing ffmpeg/
  A-Sync.

## Verification

Built clean on the **first** attempt (zero compile errors) — confirms
the API usage inferred from reading `BlackmagicRawAPI.h` and the SDK's
own `ExtractFrame.cpp`/`ExtractAudio.cpp`/`ProcessClipCPU.cpp` samples
was accurate. Ran the built tool directly against the SDK's real
`sample.braw` (a genuine 4608×2592 BRAW frame with a 48kHz/24-bit stereo
audio track) and independently verified the output three ways: `ffmpeg
-v error ... -f null -` decoded it with **zero errors**; `ffprobe`
reported `probe_score=100` with correct `h264`/`pcm_s24le` streams at
the source's exact resolution/sample rate; B-Roll Analyzer's own venv
opened it with `cv2.VideoCapture` and read a real frame back. Extracted
that frame as a JPEG and visually inspected it — correctly colored,
sharp, no channel-swap or stride-corruption artifacts (confirms the
BGRA8 pixel-format choice and the manual per-row `memcpy` stride
handling are both correct). `braw_bridge.status()` now reports
`{"available": true, "sdk_runtime_found": true, "tool_built": true}` on
this machine with zero code changes to the detection logic itself — v28's
scaffolding worked exactly as designed against a real install.
`main.py --selftest` + full pytest: 196/196 passed (was 195; +1 new
real integration test, zero regressions).

# Addendum v30 — BRAW transparent substitution, B-Roll workspace (suite-side only)

Phase 2 of the BRAW compatibility plan, scoped to one workspace first
(B-Roll) per an explicit decision with the user: **every substitution
happens in suite-owned code — B-Roll Analyzer's own `analyzer.py` is
never modified and never learns `.braw` exists.** The usual "patch the
sibling app directly, it also helps standalone use" reasoning doesn't
apply here — a standalone B-Roll Analyzer has no access to this suite's
proprietary-SDK bridge (`braw_bridge.py`/`tools/braw/`) at all, so it
could never generate a proxy for itself even with `.braw` added to its
own extension list. Keeping BRAW-awareness entirely suite-side costs
nothing standalone use would have gained anyway, and matches how every
other special-case integration in this codebase already works (Blair
Brander's bridge, Card Eater's bridge).

## The centralized-cache wrinkle

Proxies live in `assets/proxies/` (Phase 1's design — BRAW sources are
often on read-only camera media), not next to the source file. B-Roll
Analyzer's own `find_video_files()` (os.walk + its own `VIDEO_EXTENSIONS`)
therefore never sees a `.braw` file OR its proxy. The fix: **all BRAW
discovery/resolution happens in suite-owned code that sits in front of**
`analyzer.py`'s calls, never inside them.

## Implementation

- **`backend/braw_bridge.py`**: new `find_braw_files(folder)` — the ONE
  shared `.braw` folder-scan implementation (mirrors
  `analyzer.find_video_files`'s own os.walk/extension-match semantics so
  the two lists combine cleanly). Called from BOTH the suite's own
  process (`api_broll.py`) and B-Roll Analyzer's own venv subprocess
  (`broll_worker.py`, via a sys.path insert — confirmed this module is
  stdlib + `paths.py`/`braw_proxy_cache.py` only, so it imports cleanly
  in that venv too, verified live: `broll_worker.py --selfcheck` passes).
- **`backend/workers/broll_worker.py`** (suite-owned — the actual
  `cv2.VideoCapture` call lives in the sibling's `analyzer.py`, but the
  file discovery and dispatch around it are entirely this file):
  - `_find_all_video_files(folder)` = `sorted(analyzer.find_video_files(folder) + braw_bridge.find_braw_files(folder))`,
    used by both `run_analyze` and `rebuild_from_cache` (export path) so
    a cached `.braw` analysis is discoverable by XML export the same way
    a fresh one was discovered.
  - `_resolve_decode_path(path)`: ordinary files decode as themselves; a
    `.braw` file resolves to its cached proxy via
    `braw_bridge.find_cached_proxy` — a READ-ONLY lookup. This worker
    never generates a proxy itself (that shells out to the compiled SDK
    tool and belongs in its own labeled `"braw_proxy"` Jobs-drawer entry,
    started from the suite's own process — see `api_broll.py` below). A
    `.braw` file with no proxy yet gets an immediate per-file
    `ClipResult.error` ("hasn't finished generating yet — re-run
    Analyze") instead of occupying a pool slot, using
    `braw_bridge.unavailable_reason()` to distinguish "BRAW isn't
    available on this machine at all" from "still generating".
  - **Path swap-back timing** (the subtle part): `_analyze_one` is
    called with the resolved *decode* path, so `ClipResult.path` is the
    proxy path all the way through scoring AND `analyze_clip`'s own
    internal `refresh_thumbnail` call, AND the module's separate
    "capture a thumbnail for any result still missing one" fallback pass
    — all three need real decodable bytes. Only AFTER that fallback pass
    (once nothing further ever needs to touch the actual video bytes) is
    `result.path`/`result.filename` swapped back to the ORIGINAL `.braw`
    path — before the cache-key computation (`os.path.relpath(result.path, folder)`,
    which would otherwise try to relpath a path in `assets/proxies/`
    against the analyzed folder) and before the frontend payload/export
    ever see it, matching this suite's "export always references
    original media" convention.
- **`backend/api_broll.py`**:
  - `broll_start`: BEFORE starting the "broll" job, scans the folder via
    `braw_bridge.find_braw_files` and calls `braw_bridge.request_proxy`
    for each — idempotent (a cached hit returns immediately with no new
    job) and fire-and-forget (doesn't block starting the analyze job).
    Returns the queued proxy job ids as `braw_proxy_jobs` alongside the
    existing `job_id`.
  - `broll_preview_url` / `suite_broll_favorite_thumbnail`: resolve a
    `.braw` path to its cached proxy before handing anything to RCS's
    (untouched) `PreviewServer`/`thumbnails.py` — both are generic,
    format-agnostic byte-servers/ffmpeg-callers that don't care what
    container they're given, so substituting the path at the call site
    is sufficient; neither file needed a single line changed.
- **`backend/suite_api.py`**: new `suite_braw_status()` — cross-workspace
  (not B-Roll-specific), wraps `braw_bridge.status()` for a future
  frontend "BRAW not available" hint.

## Verification

`main.py --selftest` + full pytest: 198/198 passed (was 196; +2 new
tests in `tests/test_broll_braw_real.py`, zero regressions). New tests:
- A REAL end-to-end run (not mocked) — copies the SDK's own
  `sample.braw` into a temp folder, pre-generates its proxy via the real
  compiled tool, calls `api.broll_start()`, waits for the real B-Roll
  Analyzer subprocess to finish, and confirms: the returned clip's
  `path` is the ORIGINAL `.braw` path (not the proxy) with no error and
  a real duration; `broll_preview_url`/`suite_broll_favorite_thumbnail`
  both resolve and succeed; re-running Analyze on the same folder is a
  genuine cache hit keyed by the original path (proving the swap-back
  landed before the cache write, not just in that one run's payload).
- A mocked test confirming `broll_preview_url`/
  `suite_broll_favorite_thumbnail` report a clear "proxy not ready yet"
  error for a `.braw` path with no cached proxy, rather than handing an
  undecodable raw file to RCS's preview/thumbnail code.

**Bug caught while writing the real test, worth flagging for future
subprocess-based tests**: `monkeypatch.setattr(paths, "PROXIES_DIR", ...)`
only patches the CURRENT process's module object — `broll_worker.py`
runs as a separate Python interpreter that reads its own fresh copy of
`paths.py` from disk, so a monkeypatched `PROXIES_DIR` is invisible to
it. The real integration test therefore writes a genuine proxy into the
REAL `assets/proxies/` dir and removes it (file + index entry) in a
`finally` block — confirmed via `assets/proxies/index.json` after the
run that no test debris was left behind.

**Not done in this pass** (by agreed scope): Sync, Transcribe, and
Edit-preview substitution; extension-allowlist gating (Phase 3); export
XML wiring beyond what `rebuild_from_cache`'s file-list change already
covers; Jobs-drawer UI for `braw_proxy` (Phase 5). Same suite-side-only
pattern established here (shared `braw_bridge` helper + per-worker
decode-path resolution + swap-back-before-cache-write) is the template
for each of those.

# Addendum v31 — BRAW transparent substitution, Sync workspace (suite-side only)

Extends v30's pattern to the Sync workspace. Same rule: **A-Sync's own
`sync_core.py`/`waveform_view.py` are never modified** — every `.braw`
path is resolved to its cached proxy in suite-owned code before it ever
reaches a sibling function.

## Refactor first: shared resolution logic, now used by two workers

`broll_worker.py`'s `_resolve_decode_path` (v30) was pulled up into
**`backend/braw_bridge.py` as `resolve_decode_path(path)`** — it needed
nothing B-Roll-specific, and Sync now needs the identical logic
(resolve `.braw` → cached proxy, or a clear "not ready"/"unavailable"
error, never generating one itself). `broll_worker.py` was updated to
call the shared function; behavior unchanged (verified: its own tests
and `--selfcheck` still pass). Also added **`braw_bridge.queue_missing_proxies(job_manager, paths)`**
— the "scan a list of paths, fire-and-forget-queue a proxy job for each
uncached `.braw` one" logic `broll_start` already had inline, now shared
by `api_sync.py`'s three job-starting methods too.

## Where Sync differs from B-Roll (and why it doesn't matter here)

Sync's file discovery is per-file native dialogs, not a folder walk — so
there's no `find_video_files`-equivalent gap to route around; a `.braw`
path only needs resolving right before the sibling's own path-taking
functions are called. Also, unlike B-Roll, there's no per-folder cache
file — every probe/detect/peaks call is a fresh subprocess run, so
there's no "swap the path back before the cache write" step; the
substitution is simpler: resolve right before the `sync_core` call,
keep the ORIGINAL path in every output dict/key (which the existing code
already did by referencing the outer path variables directly, not
anything decode-derived).

## Implementation

- **`backend/workers/sync_worker.py`**: same sys.path bootstrap as
  `broll_worker.py` (confirmed via `--selfcheck` in A-Sync's own venv).
  `run_probe`/`run_peaks` resolve each path via
  `braw_bridge.resolve_decode_path` before calling `sync_core.probe`/
  `sync_core.extract_mono_pcm`, keeping the ORIGINAL path as the result
  dict's key. `run_detect` resolves `video_path` (fatal for the whole
  batch on failure — there's only one) and, defensively, each
  `audio_path` (audio sources are never realistically `.braw`, but
  costs nothing to route through the same one gate) before calling
  `sync_core.extract_mono_pcm`/`video_timecode_seconds`/
  `bwf_timecode_seconds`; the video/track dicts in the result already
  referenced the outer `video_path`/`audio_path` variables, so no
  further change was needed there.
  **Known, documented limitation**: the generated proxy carries no
  embedded timecode track, so `"timecode"`-method sync against a `.braw`
  video always reports "no embedded timecode found" — surfaced as the
  exact same per-track error timecode mode already gives any other
  timecode-less file, not a crash or a silently wrong offset.
  `"waveform"` mode (the default) is unaffected — it correlates real
  decoded audio, which the proxy's PCM track carries faithfully.
- **`backend/api_sync.py`**: `sync_start`/`sync_probe`/`sync_peaks` each
  call `braw_bridge.queue_missing_proxies` before starting their
  subprocess call, folding the queued job ids into a new
  `braw_proxy_jobs` response key (same shape `broll_start` already
  returns).
- **`backend/api_broll.py`**: new module-level `_resolve_playable_path(path, allowed_extensions)`
  factors out the `.braw` → proxy branch that was about to be
  duplicated a third time; `broll_preview_url` and `sync_preview_url`
  (which — unchanged from v30 — lives in this file's `BrollMixin`, not
  `api_sync.py`) both now call it. `sync_preview_url` previously had
  ZERO `.braw` awareness (confirmed via research before touching
  anything); it now resolves exactly like `broll_preview_url` always
  has. `suite_broll_favorite_thumbnail` was deliberately NOT refactored
  onto this helper — it has no general extension allowlist (any file ffmpeg
  can open was always accepted), and routing it through
  `_resolve_playable_path` would have silently added one, a real
  behavior change this pass has no business making.

## Verification

`main.py --selftest` + full pytest: 202/202 passed (was 198; +4 new
tests in `tests/test_sync_braw_real.py`, zero regressions). Before
writing the tests, empirically checked (manual real runs, not part of
the suite) how `sync_core`'s correlation math behaves against the SDK's
sample clip — a genuine edge case at ~67ms/1 frame — since a mocked test
wouldn't have caught it: `probe`/`peaks` both work cleanly; `detect`
(waveform method) also works, and correlating the clip's OWN audio
(re-extracted as an external .wav) against itself deterministically
lands `offset_seconds ≈ 0.0` — a real, non-trivial, verifiable result,
not just "didn't crash". That became the real test's fixture design
(`braw_clip_with_proxy`, shared setup: copy the sample, pre-generate and
wait for its real proxy, clean up the real `assets/proxies/` entry in
`finally` — same monkeypatch-doesn't-cross-into-the-subprocess lesson
from v30). Four real tests: probe + peaks return correct real metadata
keyed by the ORIGINAL `.braw` path (including confirming
`timecode_tag: null`, the documented limitation, rather than a crash);
detect produces the real `offset_seconds ≈ 0` result with the video
dict referencing the original path; `sync_preview_url` resolves a real
cached proxy to a servable URL; and a mocked test confirms the
"proxy not ready yet" error for an uncached `.braw` path.

**Not done in this pass**: Transcribe and Edit-preview substitution;
embedded timecode in the generated proxy (would need extending
`tools/braw/braw_proxy_tool.mm` to write an `AVMediaTypeTimecode` track
from `IBlackmagicRawClip`/`IBlackmagicRawFrame`'s own timecode
accessors — flagged as a known limitation, not attempted here); Phase 3
allowlist gating; Phase 5 UI.

# Addendum v32 — fix: BRAW proxy race made Analyze/Sync fail on (almost) every first run

**Real bug, reported by the user**: running B-Roll's Analyze on a folder
containing a `.braw` file reliably errored with "This BRAW clip's proxy
hasn't finished generating yet" — not an occasional flake, essentially
every time for a small/first-time file. Reproduced directly:

```
proxy job -> finished_at: 1784638513.84
broll job -> finished_at: 1784638513.28   (0.5s EARLIER — already reported the clip as errored)
```

## Root cause

`broll_start` (and, identically, `sync_start`/`sync_probe`/`sync_peaks`)
queue a `"braw_proxy"` job via `queue_missing_proxies` and then
immediately start the `"broll"`/`"sync"` job in the SAME call — with no
ordering guarantee between the two. `run_analyze`'s discovery loop
resolved each `.braw` file's decode path exactly ONCE, synchronously,
before ever building the pool — for a small file, the analyze
subprocess's own startup (importing cv2/numpy/torch) was routinely
*faster* than the compiled BRAW tool finishing a transcode, so the
"not ready yet" branch fired almost every time rather than the rare
edge case v30/v31 assumed it'd be.

## Fix

- **`backend/braw_bridge.py`**: new `wait_for_decode_path(path, timeout=1200.0, poll_interval=1.0)`
  — like `resolve_decode_path`, but when BRAW is available and the
  proxy simply isn't ready yet, polls up to `timeout` seconds instead of
  failing immediately. Explicitly documented as only safe to call from
  somewhere the wait can't stall unrelated work.
- **`backend/workers/broll_worker.py`**: the resolution moved INTO
  `_analyze_one` (the pool child), which now receives the ORIGINAL path
  and waits for its own proxy there — one slow proxy occupies one pool
  slot, never blocks any other file's analysis. `run_analyze`'s
  discovery loop is back to its pre-BRAW simplicity (no per-file
  resolution split); the path-swap-back check now compares
  `result.path != files[i]` directly instead of tracking a separate
  index set, since `_analyze_one` no longer reports resolution failures
  through a side channel.
- **`backend/workers/sync_worker.py`**: every `resolve_decode_path` call
  site switched to `wait_for_decode_path` — safe here without any
  further restructuring, since this worker is already strictly
  sequential per file (waiting for one file's proxy only makes that one
  sync/probe/peaks job take longer, never stalls a different job).

**Known trade-off, not fixed here**: `wait_for_decode_path`'s bound
(1200s) is generous but arbitrary, and B-Roll's `max_workers` (default
3) means a folder with more concurrently-generating `.braw` files than
worker slots will have some of them queue behind others' waits — the
`"braw_proxy"` job kind is itself globally throttled to 1-at-a-time
(`jobs.py`), so this was already true to some degree; a real fix would
either serialize proxy generation ahead of analysis with per-file
progress in the Jobs drawer, or raise the proxy throttle — Phase 5
territory, not attempted in this bug-fix pass.

## Verification

`main.py --selftest` + full pytest: 204/204 passed (was 202; +2 new
tests, zero regressions). Both new tests are the important part: they
deliberately do NOT pre-generate a proxy before calling `broll_start`/
`sync_start` — every other BRAW test in the suite does, which is
exactly how this bug slipped through the original v30/v31 test
suites despite being a near-100%-reproducible failure in real use.
`test_braw_start_without_preexisting_proxy_does_not_race` and
`test_sync_start_without_preexisting_proxy_does_not_race` now drive
both entry points exactly the way a real first click does and assert
the clip/track comes back with no error. Manually re-ran the exact
repro from the bug report after the fix — clip now analyzes
successfully (`error: null`, real duration/fps/width/height/score) on
the very first `broll_start` call, no pre-generation needed.

# Addendum v33 — fix: braw_proxy_tool crashed (uncaught NSException) on real BRAW footage

**Real bug, reported by the user**: analyzing a folder with a real
`.braw` file crashed the whole `braw_proxy_tool` process:

```
*** First throw call stack: (...)
2 AVFCore -[AVAssetWriterInput initWithMediaType:outputSettings:sourceFormatHint:] + 752
...
libc++abi: terminating due to uncaught exception of type NSException
```

## Diagnosis

Initial suspicion (odd video width/height — H.264's 4:2:0 chroma
subsampling requires even dimensions) was tested directly against
AVFoundation and **ruled out**: 4607×2592, 12288×6480 (Ursa 12K),
8192×4320 (8K) all succeeded unmodified. Systematically probing the
*audio* settings instead (the SDK's own sample clip is only ever
2-channel, so this path had never been exercised against real hardware
output) found the actual cause: **`AVAssetWriterInput` throws
`NSInvalidArgumentException: Missing required key AVChannelLayoutKey`
for any channel count beyond mono/stereo** — and several real
Blackmagic cameras record 4+ discrete audio channels (multiple
mic/XLR inputs). A second, related throw (`Bit depth can only be one
of: 24, 16, 8, 32`) was found the same way for non-standard bit depths.
Both reproduced and fixed against a standalone AVFoundation harness
before touching the real tool, to confirm root cause with certainty
rather than guessing.

## Fix (`tools/braw/braw_proxy_tool.mm`)

- **Multi-channel audio**: `audioSettings` now includes
  `AVChannelLayoutKey`, built as an `AudioChannelLayout` with
  `kAudioChannelLayoutTag_DiscreteInOrder | channelCount` — the honest
  tag for independently-recorded camera audio channels (not a real
  spatial layout like 5.1). Verified working for 1/2/4/6/8 channels.
- **Non-standard bit depth**: a clip whose reported bit depth isn't one
  of AVFoundation's four valid values (8/16/24/32) is now treated as
  having no usable audio (same graceful-degradation pattern already
  used for a zero/invalid audio format) rather than either crashing or
  silently reinterpreting the SDK's packed samples at the wrong bit
  depth (which would have produced corrupted, not just missing, audio).
- **Odd video width/height**: rounded down by at most 1px per side
  before being used for `AVVideoWidthKey`/`HeightKey` and the pixel
  buffer pool, with the per-row copy loop bounded to match (a proxy
  losing a hairline edge is imperceptible). Not the actual root cause
  found here, but a real H.264 constraint worth guarding regardless —
  and a concrete instance of the next fix's whole point:
- **The entire proxy-building body is now wrapped in `@try`/`@catch`**
  around `NSException`, converting ANY future AVFoundation validation
  throw into the tool's normal `{"type":"error",...}` JSON line instead
  of a process crash. This is the backstop — the two specific fixes
  above address the trigger actually seen in production, but
  AVFoundation's settings-validation surface is broader than any fixed
  set of guards, and a crash bypasses this tool's entire error-reporting
  contract in a way a caught exception doesn't.

## New regression coverage

**`tools/braw/test_av_settings.mm` + `.sh`** (new): a standalone
AVFoundation harness — no Blackmagic SDK, no real `.braw` media
required — that mirrors `braw_proxy_tool.mm`'s exact settings-building
logic against every dimension/channel/bit-depth combination known to
matter (real Blackmagic sensor resolutions up to 12K, 1/2/4/6/8-channel
audio, every AVFoundation-valid bit depth plus one invalid one to
confirm it's skipped rather than crashing). This exists specifically
because the real bug here involved a settings combination the existing
`.braw` sample-clip-based integration tests could never exercise (that
clip is fixed at 2-channel audio) — a class of bug that needs testing
against the validation logic directly, independent of having the
exact real media on hand that triggered it.

## Verification

Rebuilt (`build.sh`) and re-ran against the SDK's `sample.braw` —
identical output to before (2-channel 24-bit audio, even 4608×2592 —
this fix is a no-op on that clip, confirming zero regression on the
already-working path: `ffmpeg`/`ffprobe` still decode it with zero
errors). `./test_av_settings.sh`: all 15 real combinations pass
(including 4/6/8-channel audio and every real Blackmagic sensor
resolution tested), the 1 deliberately-invalid bit depth is skipped as
designed, exit code 0. `main.py --selftest` + full pytest: 204/204
passed, zero regressions (no Python code changed in this pass — the fix
is entirely in the compiled tool).

# Addendum v34 — investigating a reported multi-frame decode stall (partial fix + diagnostics added)

**Reported by the user**: decoding a real `.braw` file consistently
stalls at frame 35, across every file tried — no crash, no error, just
no further progress. Both real `.braw` samples available in this
environment (the SDK's own `Media/sample.braw`, 1 frame; the Speed
Test app's `profile.braw`, 4 frames) are far too short to ever reach
frame 35, so **this could not be reproduced directly** — everything
below comes from careful code review, not observed reproduction, and is
flagged as such rather than claimed as a confirmed fix.

## A wrong hypothesis, tested and reverted

Initial theory: `ReadComplete`'s `IBlackmagicRawFrame* frame` parameter
is a COM-style (`IUnknown`) reference-counted object that was never
released — copied faithfully from the SDK's own `ExtractFrame.cpp`/
`ProcessClipCPU.cpp` samples, which also never release it. Reasoned
that a fixed-size internal frame pool/cache (this SDK has its own
`IBlackmagicRawResourceManager`) exhausted by a consistent number of
leaked frames would explain a stall at the same frame count regardless
of file content. **Tested directly and disproven**: releasing `frame`
— tried both immediately in `ReadComplete` and deferred to
`ProcessComplete` (in case the async decode/process job still needed
it) — crashed with a segfault on the known-good 1-frame sample in
BOTH cases. This means `frame` (like `processedImage` in
`ProcessComplete`, which the SDK's own samples also never release) is
NOT owned by the callback receiver despite matching the SDK's general
IUnknown convention elsewhere — it's SDK-managed. Fully reverted;
confirmed both real samples and the full pytest suite pass again
before proceeding.

## A real, independently-found bug fixed regardless

Code review (not reproduction) found that **the `readyForMoreMediaData`
busy-wait loops (both video and audio) never check `writer.status`**.
If `AVAssetWriter` ever enters `AVAssetWriterStatusFailed` for any
reason (disk space, an internal encoder error, ...), there is no
mechanism for `readyForMoreMediaData` to ever become true again — so
this bug causes an INFINITE, completely silent spin: no crash, no
error line, just a permanent hang at whatever frame the writer happened
to fail on. That is indistinguishable from "decoding stalls" from the
outside, and is a real bug regardless of whether it's THE cause of the
reported stall. Fixed in both loops: check `writer.status !=
AVAssetWriterStatusFailed` in the loop condition, and surface a proper
`EmitError` + exit if it has failed rather than spinning forever. Also
fixed a related, clearly-dormant bug spotted in the same code: the audio
loop declared and checked an `audioFailed` flag that was never actually
*set* on any failure path — audio-side failures were being silently
swallowed entirely.

## Diagnostics added (`BRAW_PROXY_TOOL_DEBUG=1`)

Since the actual stall can't be reproduced here, `braw_proxy_tool.mm`
now has fine-grained stderr tracing (never stdout — that stays reserved
for the JSON-lines protocol), gated behind this env var, at every step
of the per-frame decode: submitting the read job, waiting on the
semaphore (blocks here if `ReadComplete`/`ProcessComplete` never
fires), inside both callbacks, waiting on `readyForMoreMediaData`
(logs every ~2s if still waiting, including `writer.status`), pixel
buffer allocation, and `appendPixelBuffer`'s actual return value
(previously never checked at all). Marked as **TEMPORARY** in the
source — remove once the actual hang is confirmed root-caused. If the
`writer.status` fix above doesn't fully resolve the reported stall, the
next step is running `BRAW_PROXY_TOOL_DEBUG=1 braw_proxy_tool
<real-file.braw> /tmp/out.mov` directly against the user's own real
footage and sharing the stderr output — that will show exactly which
one of these steps frame 35 (or wherever it next stalls) is stuck at.

## Verification

Rebuilt; both real samples (1-frame, 4-frame) still decode correctly
with zero regressions, confirmed via `ffmpeg`/`ffprobe`. Confirmed the
new stderr tracing produces the expected step-by-step output (captured
a full run of the 4-frame sample with `BRAW_PROXY_TOOL_DEBUG=1`).
`./test_av_settings.sh`: all combinations still pass. `main.py
--selftest` + full pytest: 204/204, zero regressions. **Status: the
`writer.status` fix is a real, independently-justified correctness fix,
but not confirmed as THE fix for the reported frame-35 stall** — that
requires either a longer real `.braw` sample or the user re-testing
against their own footage.

# Addendum v35 — root cause found and fixed: run loop starvation stalled the video encoder

The v34 debug tracing paid off immediately. The user ran it against
their actual footage (a real 6K clip, `Pyxis 6K`, 1925 frames) and it
stalled at frame 35 exactly as reported. The trace pinpointed it
precisely:

```
frame 35: waiting on videoInput.readyForMoreMediaData ...
frame 35: STILL waiting ... after 2s (writer.status=1)
frame 35: STILL waiting ... after 30s (writer.status=1)   (and onward, forever)
```

`writer.status=1` is `AVAssetWriterStatusWriting`, NOT `AVAssetWriterStatusFailed`
(3) — so v34's writer-failure guard correctly did not fire, because the
writer genuinely had not failed. It was legitimately still alive,
just never signaling `readyForMoreMediaData` again.

## Root cause

`braw_proxy_tool` is a plain command-line tool — no `NSApplication`, no
`CFRunLoopRun()`, nothing ever starts a run loop on its main thread. The
video-frame wait loop was a tight `usleep(1000)` spin with nothing else
happening. Some of AVFoundation's internal bookkeeping — specifically,
whatever notifies the writer that an encoded frame's buffer slot has
freed up, which is what flips `readyForMoreMediaData` back to `YES` —
apparently needs the chance to run on the calling thread's run loop.
A `usleep`-only wait never gives it that chance, so once the encoder's
internal buffer (VideoToolbox's H.264 session has a bounded number of
frames it can hold "in flight" before it needs to signal back) filled
up at a consistent depth, the notification that would have freed it up
was never delivered, and the wait became permanent. This explains every
detail of the original report: same frame count on every clip
(a fixed internal buffer depth, not file content), no crash, no error
(the writer was never in a failed state), and why neither of the
two available sample clips (1 frame, 4 frames) could reproduce it —
both are far shorter than whatever depth the internal buffer fills to.

## Fix

New `PumpRunLoopBriefly()` — `[[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode beforeDate:...]`
for 1ms — replaces the plain `usleep(1000)` in BOTH the video and audio
`readyForMoreMediaData` wait loops (the audio path has the identical
structural risk, even though it wasn't the one actually hit). This is
the standard, idiomatic fix for exactly this class of AVFoundation
gotcha in command-line tools: servicing the run loop briefly instead of
just sleeping gives any main-thread-dispatched internal callback a
chance to actually run.

## Verification

Rebuilt; both real samples (1-frame, 4-frame) still decode correctly
with zero regressions (`ffmpeg`/`ffprobe` confirm). `./test_av_settings.sh`:
all combinations still pass. `main.py --selftest` + full pytest:
204/204, zero regressions. **Not independently reproduced against the
actual 1925-frame clip that exposed the bug** — that file lives on the
user's own external volume and was never available in this environment;
the fix follows directly and specifically from the debug trace they
captured (writer never failed, wait never ended, a run-loop-starvation
signature with no other plausible explanation), and the next real
confirmation is the user re-running their same repro against the
rebuilt tool.

# Addendum v36 — v35's run-loop fix did NOT resolve it; found a second, more concrete culprit

The user rebuilt and re-ran the exact same repro (their real 6K, 1925-
frame BRAW clip). **Identical failure**: stalls at frame 35, `writer.
status=1` ("Writing", never "Failed") the whole time — v35's
`PumpRunLoopBriefly()` fix made no difference. That hypothesis is now
disproven too.

## Re-examining what's actually different about this file

Both real samples available in this environment (1 frame, 4 frames) are
4608×2592. The failing file is 6K (`Pyxis 6K` in its path) — a much
larger resolution. Re-reading `braw_proxy_tool.mm`'s video settings
with that specifically in mind found this:

```objc
AVVideoAverageBitRateKey: @((long long)encodeWidth * encodeHeight * 12),
```

**This formula never factors in frame rate at all.** For a 6144×3456
clip that computes a FLAT ~255 Mbps target, regardless of whether the
clip is 24fps or 60fps — nonsensical, since hitting a fixed bits/second
number obviously requires proportionally more bits per individual frame
at a lower frame rate. An H.264 encoder asked to sustain an unrealistic,
disproportionate bitrate for real, continuously arriving 6K frames could
plausibly get stuck unable to keep pace — which would show up exactly
as `readyForMoreMediaData` never recovering, with the writer itself
never entering a failed state (it's still "trying"), matching every
observed symptom.

**Why `test_av_settings.sh` (addendum v33) couldn't have caught this**:
that harness only verifies AVFoundation *accepts* a settings dictionary
at `AVAssetWriterInput` creation time — it never actually encodes a
sustained sequence of real frames, so it has no way to detect "the
encoder can't actually sustain this bitrate in practice." Settings-
validation and sustained-throughput are different failure classes; only
the former had a regression test.

## Fix

`AVVideoAverageBitRateKey` is now computed as
`encodeWidth * encodeHeight * frameRate * 0.1` (0.1 bits/pixel/frame —
a well-established, generous quality target for an editing/scrubbing
proxy, not a delivery master), clamped to a 2–100 Mbps range as a second
line of defense against any degenerate resolution/frame-rate
combination. For the reported file this is roughly a 2.5–5x reduction
from the old flat ~255 Mbps figure, depending on its actual frame rate
(unknown — not established from the trace alone).

## Verification

Rebuilt; both real samples still complete successfully with zero
regressions (`ffmpeg`/`ffprobe` confirm valid, decodable output — note
the *reported* `bit_rate` for these specific samples isn't a meaningful
check of the new formula: at 1 and 4 frames, encoded duration is a
handful of milliseconds, so `total_bits / duration` is dominated by
single-keyframe-size noise, not a reflection of the sustained target;
that only converges meaningfully over many seconds of real content,
which is exactly what wasn't available to test with here).
`./test_av_settings.sh`: all combinations still pass. `main.py
--selftest` + full pytest: 204/204, zero regressions. **Once again: a
concrete, well-reasoned fix for a real formula defect, but NOT
independently confirmed against the actual failing clip** — same
limitation as v35, for the same reason (that file isn't available in
this environment). v35's run-loop-pump change is left in place
(harmless, and separately a reasonable defensive practice for a
command-line AVFoundation tool) even though it didn't fix this on its
own. Next step, if this doesn't resolve it: rebuild, retry, and if it
stalls again, capture the same `BRAW_PROXY_TOOL_DEBUG=1` trace — at
minimum it will now rule bitrate in or out as a factor.

# Addendum v37 — v36's bitrate fix also failed; switching from guessing to isolating

The user rebuilt and re-ran the identical repro a third time. **Identical
failure once again**: stalls at frame 35, `writer.status=1` the entire
time. The bitrate fix (v36) made no observable difference, joining
v35's run-loop-pump fix as a second disproven hypothesis for the same
symptom.

## Changing approach

Two independent, individually well-reasoned AVFoundation-side fixes in a
row have both failed against the exact same, precisely-reproducible
symptom (same frame count, same `writer.status`, every single time,
regardless of what changed on that side). Continuing to propose a third
specific AVFoundation fix without a way to verify it locally (the
failing clip isn't available in this environment) isn't a good use of
the user's time — each cycle costs a full rebuild + multi-minute real
decode attempt. The more useful next step is a controlled experiment
that narrows down WHICH subsystem is actually stuck, rather than another
guess at a fix for the AVFoundation side specifically.

## New diagnostic: `BRAW_PROXY_TOOL_SKIP_ENCODE=1`

Decodes every frame via the BRAW SDK exactly as the real tool does
(`CreateJobReadFrame` → `Submit` → wait for `ReadComplete`/
`ProcessComplete`), but creates NO `AVAssetWriter`, NO pixel buffer, and
appends nothing — each decoded frame's bytes are simply discarded, and
progress is reported purely from decode completion. This isolates the
two independent pipelines this tool chains together:

- If this reaches the clip's full frame count (1925) cleanly with no
  stall, that DEFINITIVELY proves the problem is in AVFoundation's
  encoder/writer side, not the BRAW SDK decode — worth pursuing further
  fixes there (e.g., ProsRes instead of H.264, or a completely different
  muxing strategy) rather than abandoning that line of investigation.
- If it ALSO stalls at frame 35 with NO AVFoundation object ever
  created, that proves the opposite: the actual bottleneck is inside
  the Blackmagic RAW SDK's own decode pipeline (or this tool's use of
  it) and has nothing to do with anything fixed so far — a completely
  different, and much more consequential, direction for the next
  investigation (possibly needing Blackmagic's own support/documentation,
  since the SDK itself would be implicated).

Verified on both real samples available here (1-frame, 4-frame): decode-
only mode correctly reports per-frame progress, produces no output file,
and completes with a clean `result` line — confirmed to work
structurally before asking the user to run it against the actual
failing clip.

## Verification

Rebuilt; both real samples still work correctly in BOTH normal and
`SKIP_ENCODE` modes, with zero regressions. `./test_av_settings.sh`:
all combinations still pass (unaffected — the new diagnostic path
doesn't touch AVFoundation settings at all). `main.py --selftest` +
full pytest: 204/204, zero regressions. **This addendum adds a
diagnostic, not a fix** — the actual root cause remains unknown pending
the user running `BRAW_PROXY_TOOL_SKIP_ENCODE=1` against their real
clip and reporting whether it reaches frame 1925 or stalls at 35 again.

# Addendum v38 — decode side cleared definitively; encoder B-frame reordering is the new, more concrete suspect

The user ran `BRAW_PROXY_TOOL_SKIP_ENCODE=1` against their real
1925-frame, 6K clip. **It reached "frame 1925 of 1925" and completed
cleanly** — the BRAW SDK's own read/decode/process pipeline has zero
problem running the full clip start to finish with no stall anywhere.
This definitively rules out the SDK decode side and confirms the
bottleneck is entirely inside AVFoundation's encoder/writer, exactly as
v37's diagnostic was designed to determine.

## New suspect: B-frame reordering at 6K

`videoSettings` sets `AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel`
with no `AVVideoAllowFrameReorderingKey` — VideoToolbox's H.264 encoder
defaults to *allowing* frame reordering (B-frames) under the High
profile. Enabling B-frames means the encoder can hold a lookahead window
of frames before it starts emitting encoded output (and freeing the
input buffer slots those frames occupy), since a B-frame references
frames that come after it in presentation order. At 6K, each raw BGRA
input frame is ~75MB; if the hardware encoder's reorder/lookahead
window needs to hold more of those simultaneously than its internal
memory budget allows, the encoder can stall indefinitely waiting on its
own reorder buffer — with no failure ever surfaced, which is consistent
with every observation so far: `writer.status` stays `Writing` (never
`Failed`) throughout the stall, and the SDK's own callbacks (confirmed
again by v37/v38's diagnostic) have nothing to do with it.

This is a materially different bug class than the two AVFoundation-side
fixes already disproven (v35's run-loop-pump — a backpressure-signaling
theory; v36's bitrate formula — a sustained-throughput theory). Both of
those assumed the encoder was falling behind on ordinary work. This
theory is instead a genuine internal encoder deadlock tied to frame size
× reorder depth, independent of bitrate or signaling.

## Fix

Added `AVVideoAllowFrameReorderingKey: @NO` to `videoSettings`'s
`AVVideoCompressionPropertiesKey` dict (`braw_proxy_tool.mm`, video
settings block) — forces the encoder to emit frames strictly in
submission order with no B-frames, eliminating the reorder buffer
entirely.

## Verification

Rebuilt clean. `./test_av_settings.sh`: all combinations still pass
(this key is always valid regardless of resolution/channel count/bit
depth — unaffected by anything that harness already covers).
`main.py --selftest`: 204/204, zero regressions. Ran the real (non-
`SKIP_ENCODE`) tool against the SDK's own 1-frame `sample.braw` and
confirmed via `ffprobe` that the output now reports `has_b_frames=0`
(previously unchecked/unset) — direct confirmation the setting takes
effect, though the 1-frame sample can't exercise a reorder-buffer stall
either way. **This fix is unconfirmed against the actual failing
clip** — as with v35/v36, no real long/6K sample is available in this
environment to reproduce the stall directly. Awaiting the user rebuilding
and re-running their real clip through the **normal** (non-`SKIP_ENCODE`)
path to report whether it now reaches frame 1925/completes, or stalls
again (same frame 35, or a different one).

# Addendum v39 — v38 also failed; the fixed frame count itself is now the most important clue

The user rebuilt and re-ran the real clip through the normal path.
**Identical failure a fourth time**: stalls at frame 35. This run did
not have `BRAW_PROXY_TOOL_DEBUG=1` set, so no fine-grained trace was
captured this time (unconfirmed whether it's stuck in exactly the same
`readyForMoreMediaData` spin as the original v34 trace, or has moved to
a different internal step now that reordering is disabled).

## The real signal: four different encoder-side changes, one identical frame count

Bitrate target (v36), run-loop pumping (v35), and B-frame reordering
(v38) have now ALL been changed — independently, across three separate
rebuilds — with **zero effect on where the stall happens**. That
consistency is itself the most important data point so far: none of the
knobs that would matter if this were an ordinary encoder-throughput or
backpressure-signaling problem have changed the outcome at all. That
argues against every "the encoder is falling behind / can't keep up"
theory tried so far, in favor of something structural: either a fixed
resource ceiling independent of these settings (e.g. real memory/VRAM
pressure from ~75MB-per-frame 6K pixel buffers accumulating faster than
they're actually being consumed/recycled, since none of the settings
changed so far would change how fast buffers get freed — only how the
already-queued backlog gets encoded), or a genuine buffer-pool/IOSurface
constant on this machine that none of these settings touch.

## Next steps — gathering data instead of guessing a fifth fix

Two direct, low-cost diagnostics requested rather than another blind
code change, given four rebuild-and-wait cycles have already passed
with no improvement:

1. Re-run with `BRAW_PROXY_TOOL_DEBUG=1` set, to get the exact
   stderr trace line the process is frozen on for frame 35 (submitting
   read job / waiting on decode semaphore / waiting on
   `readyForMoreMediaData` / allocating pixel buffer / append returned)
   — confirms whether the stall is still in the same place as the
   original v34 finding or has moved.
2. While stalled, check the process's actual memory footprint (Activity
   Monitor, or `ps -o rss,vsz -p <pid>` / `top -pid <pid>` in another
   terminal) — directly tests the "pixel buffers piling up faster than
   consumed" theory. A multi-gigabyte and still-growing (or plateaued at
   a suspicious ceiling) RSS at the exact moment of the stall would
   strongly implicate real memory/backpressure exhaustion rather than a
   true logic deadlock, and would point toward a different class of fix:
   an explicit self-imposed cap on frames-in-flight rather than trusting
   `readyForMoreMediaData` alone to gate submission.

# Addendum v40 — downscaling the proxy resolution (untested against the real clip, strongest candidate yet)

The user's memory-usage data (`ps -o rss`) came back low (~130-280MB,
not climbing) and a follow-up debug trace pinpointed the exact freeze:
frame 35 decodes and processes completely successfully (`ProcessComplete
entered, result=0` — the SDK's own pipeline has no problem with this
frame), and the process then hangs forever specifically inside the
`readyForMoreMediaData` wait, with `writer.status` stuck at `Writing`
(never `Failed`).

## Reasoning

Three independent AVFoundation-side settings — bitrate target (v36),
run-loop pumping (v35), B-frame reordering (v38) — have now each been
changed across separate rebuilds with **zero effect on the exact frame
the stall happens at**. Combined with memory staying low (ruling out an
ordinary buffer-pileup leak), this pattern is the signature of the
*hardware H.264 encoder itself silently wedging* rather than anything
this tool's settings control — a category of failure real Mac hardware
video encoders are known to hit well below the theoretical resolution
limits the H.264 spec allows, and one AVFoundation does not reliably
surface as a clean writer failure (matching every observation:
`writer.status` never flips to `Failed`).

The real source here is Blackmagic Pyxis 6K (6144-wide) footage — large
enough to plausibly exceed what this specific Mac's hardware encoder can
actually sustain, even though `AVAssetWriter` accepted the video
settings at creation time without complaint.

## Fix: downscale to a real preview resolution, using an actual resize (not a crop)

`braw_proxy_tool.mm`'s encode-dimension calculation now caps the longest
edge at 1920px (aspect ratio preserved, forced even for H.264 4:2:0),
scaling down instead of just cropping to the nearest even number as
before. This is independently the right design regardless of whether it
turns out to be the actual root cause of the stall — this tool's own
contract already states the proxy is strictly a scrubbing/preview
artifact, never the export path (Premiere XML always references the
ORIGINAL `.braw`), so 6K was always more resolution than the job needed.

Since the encode resolution can now be genuinely smaller than the
decoded frame (not just 0-1px smaller from odd-dimension rounding), the
previous per-row `memcpy` (a crop) was replaced with a real resize:
`vImageScale_ARGB8888` (Accelerate framework, newly linked in
`build.sh`) blits the SDK's native-resolution decoded frame directly
into the pixel buffer at the smaller encode resolution. This function is
channel-order-agnostic — it interpolates four independent 8-bit planes
per pixel, which is exactly correct for BGRA too, since scaling doesn't
care what the channels represent.

## Verification

Rebuilt clean (with the new `-framework Accelerate` link).
`./test_av_settings.sh`: all combinations still pass (that harness
exercises AVFoundation settings acceptance directly and isn't wired
through this tool's own encode-dimension math, so it's unaffected by
this change either way). `main.py --selftest`: 204/204, zero
regressions. Ran the real (non-`SKIP_ENCODE`) tool against the SDK's own
`sample.braw` (native 4608×2592): output is now genuinely downscaled to
1920×1080 (correct 16:9 aspect ratio preserved) — confirmed via
`ffprobe`, and confirmed NOT corrupted by opening the output with
OpenCV (`cv2.VideoCapture` reads a frame of the correct shape
`(1080, 1920, 3)` with plausible, non-degenerate mean pixel values, not
a garbled or blank frame).

**This fix is unconfirmed against the actual failing clip** — same
limitation as every attempt since v34, no real long/6K sample available
in this environment to reproduce the stall directly. This is the
strongest candidate so far, though: it's the first change that alters
something structurally different (resolution actually being encoded)
rather than a tuning parameter (bitrate/profile/reordering) that could
never have addressed a genuine hardware ceiling. Awaiting the user
rebuilding and re-running their real clip to report whether it now
reaches frame 1925/completes, or stalls again (same frame 35, a
different frame, or a new symptom entirely).

# Addendum v41 — root cause found: video track was drained entirely before audio ever started (fix: interleave the two tracks)

The user rebuilt with v40's downscale fix and re-ran the real clip.
**Identical stall, same frame 35** — even at 1920×1080, the most
mundane possible H.264 encode target for any Mac made in the last
decade. This definitively kills the hardware-resolution-ceiling theory
from v40, and combined with v35/v36/v38 also having zero effect, means
**every setting that affects video-encode quality (bitrate, profile,
reordering, resolution) has now been ruled out** — nothing that tunable
could explain a fixed, identical stall frame across five independent
changes.

## The actual root cause

Re-reading the tool's overall structure (not just the video settings)
surfaced it: the video loop runs to full completion — writing anywhere
from 1 to `frameCount` frames of video — before the audio track is
EVER touched (audio writing only began after `[videoInput
markAsFinished]`). `AVAssetWriter` needs multiple tracks' samples to
arrive reasonably close together in presentation time to produce a
valid, seekable QuickTime file, and enforces this itself: whichever
track gets too far ahead in presentation time has its
`readyForMoreMediaData` held at NO indefinitely until the lagging track
catches up. Since audio here got literally zero samples until the
entire video track was already written, video permanently outran the
(empty) audio track. Once video buffered some fixed, small window
ahead of audio's static zero — apparently corresponding to
~35 frames' worth of presentation time on this clip's frame rate — the
writer's own interleaving backpressure engaged and could never clear,
because audio was never given the chance to supply anything during the
video loop. This threshold is about relative PRESENTATION TIME between
tracks, not about bitrate/resolution/profile at all, which is exactly
why every encoder-settings change tried (v35, v36, v38, v40) had zero
effect on the outcome — none of them touch how far one track is allowed
to get ahead of another.

This also cleanly explains why `BRAW_PROXY_TOOL_SKIP_ENCODE=1` (v37/v38)
ran clean to frame 1925: that diagnostic never creates an
`AVAssetWriter` at all, so there was never a second track to fall behind
in the first place.

## Fix: interleave video and audio writing

Restructured `braw_proxy_tool.mm`'s video/audio writing from two fully
sequential passes (all video, then all audio) into one combined loop:
audio format setup was moved before the video loop, and a new
`writeAudioUpTo(videoTimeSeconds)` helper (a local lambda) is called
after every appended video frame, writing audio chunks (~1 second each,
same chunking as before) up to the video frame's own presentation time.
This keeps both tracks' presentation times within about a second of
each other throughout, rather than one track sitting at time zero while
the other advances for the clip's entire duration. A final
`writeAudioUpTo(std::numeric_limits<double>::max())` call after the
video loop ends flushes any remaining audio (a clip's audio track can
run slightly longer than its video track). The per-chunk writing logic
itself (buffer layout, `CMSampleBufferCreate`, the `writer.status`
failure guard) is unchanged from the original audio loop — only *when*
it runs changed.

## Verification

Rebuilt clean. `./test_av_settings.sh`: all combinations still pass
(unaffected — that harness doesn't exercise the interleaving change).
`main.py --selftest`: 204/204, zero regressions. Ran the real tool
against the SDK's own `sample.braw`: output still has both a valid
H.264 video track (1920×1080, matching v40's downscale) and a valid
`pcm_s24le` audio track (48kHz stereo), confirmed via `ffprobe` — no
regression on the known-good sample. **This 1-frame sample cannot
meaningfully exercise the new interleaving logic** (with only one video
frame, all audio still ends up written in the final flush regardless of
whether interleaving works correctly) — same limitation every fix in
this investigation has had, since no long real sample is available in
this environment. Awaiting the user rebuilding and re-running their
real clip to report whether it now reaches frame 1925/completes, stalls
again, or hits a new symptom.

# Addendum v42 — v41 moved the stall (35 -> 51): confirms the diagnosis, tightens the fix

The user rebuilt with v41's interleaving fix and re-ran the real clip.
**The stall moved, for the first time in this entire investigation**:
frame 35 -> frame 51. Every prior fix (v35 run-loop pumping, v36
bitrate, v38 reordering, v40 resolution) left the stall frozen at
exactly frame 35 no matter what changed — this is the first change that
altered the outcome at all, which is strong positive confirmation that
v41's diagnosis (writer backpressure from track presentation-time
divergence) is correct in kind. It just wasn't tight enough yet.

## Why it wasn't tight enough

`writeAudioUpTo`'s read size was `audioChunkSampleFrames`
(`audioSampleRate`, a fixed ~1 full second of samples) on every call,
regardless of how small the actual gap to close was. At video frame 1
(barely a few hundredths of a second of presentation time in), the very
first call still pulled a full 1-second chunk — jumping audio a whole
second AHEAD of video in one burst, then leaving video to grind back
toward catching up to that. That's still bursty, just with the tracks'
roles reversed from the original bug, and could trip the same
writer-side track-divergence backpressure just as easily, only later
(hence 51 instead of 35 — a bigger, but still fixed, burst before
divergence exceeds the writer's tolerance again).

## Fix: request only as many samples as needed to close the actual gap

`writeAudioUpTo`'s per-iteration read is now sized to
`min(audioChunkSampleFrames, samplesNeededToReachVideoTimeSeconds, samplesRemaining)`
instead of always the full chunk size — computed from the actual gap
(`videoTimeSeconds - audioSampleIndex/audioSampleRate`). This keeps the
two tracks' presentation times within a tiny fraction of a second of
each other continuously, rather than lurching a full second ahead then
waiting for video to catch back up.

## Verification

Rebuilt clean. `./test_av_settings.sh`: all pass. `main.py --selftest`:
204/204, zero regressions. Real tool against the SDK's own
`sample.braw`: still produces valid H.264 (1920×1080) + `pcm_s24le`
(48kHz stereo) tracks, no regression. Same limitation as every prior
round — the 1-frame sample can't meaningfully exercise fine-grained
interleaving over many frames, so this remains unconfirmed against the
actual failing clip. Awaiting the user rebuilding and re-running their
real clip once more.

# Addendum v43 — v42 landed back on the ORIGINAL frame 35, not an improvement over it; adding audio-path tracing before guessing again

The user rebuilt with v42's tighter audio catch-up and re-ran the real
clip. **Stalled again at frame 35** — not 51, not later: the exact same
frame as the pristine, untouched-original bug (v34), before any of
v35/v36/v38/v40/v41/v42 existed. Landing back on the EXACT original
number after a supposedly *tighter* fix (over v41's coarser version,
which reached 51) is suspicious rather than reassuring — if v42 were
simply "still not quite tight enough," a value between 35 and 51 (or a
new, different number) would be the expected outcome, not an exact
regression to the original.

## Why guessing a fourth variant of the interleaving fix isn't the right move yet

The video loop's `readyForMoreMediaData` wait has always had full debug
tracing (submitting read job / waiting on decode semaphore / waiting on
`readyForMoreMediaData` / STILL waiting.../ allocating pixel buffer /
append returned) since v34. The NEW audio catch-up path added in v41
had **none** — no visibility into whether `audioInput.readyForMoreMediaData`
ever blocks, what `requestSampleFrames`/`gapSeconds` actually are at the
real clip's true frame rate, or whether `GetAudioSamples`/
`appendSampleBuffer` succeed. Without that, there's no way to tell
whether frame 35 is still frozen in the *video* wait (same as always) or
has moved into the *audio* catch-up's own wait — two meaningfully
different situations that call for different next steps.

## Fix (diagnostic, not behavioral): full audio-path tracing

Added the same tracing style used in the video loop to `writeAudioUpTo`:
traces entering/exiting the `audioInput.readyForMoreMediaData` spin
(with periodic "STILL waiting" pings, mirroring the video loop's), plus
one line each for the computed request size/gap, `GetAudioSamples`'s
result, and `appendSampleBuffer`'s return value. No behavioral change —
verified on the SDK's own `sample.braw` with `BRAW_PROXY_TOOL_DEBUG=1`:
the new trace lines appear correctly and show the fine-grained catch-up
working as designed (e.g. "requesting 2000 samples from index 0
(gap=0.041667s)" — a small, proportional request, not a full ~1-second
chunk).

## Verification

Rebuilt clean. `./test_av_settings.sh`: all pass. `main.py --selftest`:
204/204, zero regressions. Confirmed new trace lines fire correctly and
look sane on the known-good sample (still only a 1-frame clip, so this
doesn't exercise sustained catch-up behavior, only confirms the tracing
itself works and the first call's math is correct).

**Requesting one more debug run from the user** — `BRAW_PROXY_TOOL_DEBUG=1`
against the real clip, pasting the last ~20-30 lines once it stalls at
whatever frame it lands on this time. This should show definitively
whether the freeze is in the video wait (same as always) or the new
audio wait (a materially different, more informative finding either
way) before another fix is attempted.

# Addendum v44 — track-divergence theory conclusively disproven; forcing software H.264 encoding

The user re-ran with the new audio tracing. The result was decisive, but
not in the direction v41/v42 suggested: audio stayed in **near-perfect
lockstep** with video the entire time (each catch-up call requested
exactly ~1600 samples for a ~0.0333s gap — this clip is 30fps, so audio
advanced by exactly one frame's worth of time on every single call,
essentially zero divergence) — and it **still stalled at frame 35**, in
the exact same `videoInput.readyForMoreMediaData` wait as the very first
trace captured back in addendum v34.

This conclusively disproves the track-presentation-time-divergence
theory (v41-v43). The frame-35-to-51 shift that motivated it (v42) was
apparently coincidental noise, not a real signal — genuinely tight
interleaving produces the identical failure. Audio, and the writer's
multi-track interleaving logic, are not the cause.

## Where this leaves the investigation

Every settings-side lever has now been tried and ruled out: bitrate
(v36), run-loop pumping (v35), B-frame reordering (v38), resolution
(v40), and track interleaving (v41-v44). Decode is independently proven
healthy (`BRAW_PROXY_TOOL_SKIP_ENCODE` ran the SDK's own pipeline clean
to the clip's full 1925 frames, v37/v38). With every tunable exhausted,
the remaining suspect is the video encoder pipeline itself — and
specifically, since nothing about the SETTINGS given to it matters,
whether the actual HARDWARE encoder session VideoToolbox is silently
choosing for this content on this Mac is broken/wedging, independent of
anything this tool asks it to do.

## Fix: force software H.264 encoding

Added `AVVideoEncoderSpecificationKey` to `videoSettings` (top level,
alongside `AVVideoCodecKey`/`AVVideoWidthKey`, not inside
`AVVideoCompressionPropertiesKey`) with
`kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder: @NO`
— strongly discourages VideoToolbox from using a hardware encoder at
all, forcing the software H.264 path instead. This is the first change
in the whole investigation that avoids the suspect subsystem entirely
rather than tuning a setting handed to it. `<VideoToolbox/VideoToolbox.h>`
newly imported; `-framework VideoToolbox` added to `build.sh`.

## Verification

Rebuilt clean with the new framework link. `./test_av_settings.sh`: all
pass (that harness doesn't exercise this key, unaffected either way).
`main.py --selftest`: 204/204, zero regressions. Real tool against the
SDK's own `sample.braw`: still produces valid H.264 (1920×1080) +
`pcm_s24le` (48kHz stereo) tracks, no regression on the known-good
sample. As with every fix since v34, the 1-frame sample can't exercise
sustained encoding over many frames, so this remains unconfirmed
against the actual failing clip — software encoding will also be
noticeably slower than hardware, which is an acceptable tradeoff for a
scrubbing proxy if it actually resolves the stall. Awaiting the user
rebuilding and re-running their real clip once more.

# Addendum v45 — RESOLVED: root cause found via a live stack sample, rewritten around AVFoundation's async API, confirmed against the real clip

The user re-ran with v44's forced software encoding and reported the
stall moved again (frame 35 -> 48) but did not resolve. At this point
the user offered to share the real test file directly, and it turned
out to already be reachable from this environment's own shell
(`/Volumes/Maelstrom/.../A001_07191440_C002.braw`), which changed the
investigation fundamentally: every prior round had relied on the user
rebuilding, running, and transcribing terminal output by hand. From
here on the file could be run, inspected, and iterated on directly.

## Finding the real root cause

The tool was run against the real file directly, with
`BRAW_PROXY_TOOL_DEBUG=1`, backgrounded so it could be inspected while
still stuck. It stalled again at frame 48, in the same
`videoInput.readyForMoreMediaData` wait as every round since v34. Live
process stats showed RSS at only ~38MB and ~4% CPU -- consistent with
an idle spin-wait, not an overloaded encoder.

With the process still alive and stuck, `sample <pid> 5` captured a
5-second stack trace of every thread. It was decisive: **every internal
AVFoundation/CoreMedia worker thread** --
`com.apple.coremedia.mediaprocessor.videocompression`,
`.audiocompression`, and `com.apple.coremedia.formatwriter.qtmovie` --
showed **100% of samples parked in `_pthread_cond_wait`**, completely
idle for the entire 5-second window. The actual encode+mux pipeline had
fully drained everything submitted so far and had nothing left to
process; it was never falling behind. `videoInput.readyForMoreMediaData`
simply never flipped back to `YES` to reflect that drained state -- a
lost wakeup between the (idle) pipeline and the property this tool had
been manually polling since v34, not a capacity problem any setting
could ever have fixed. This explains why bitrate (v36), run-loop
pumping (v35), B-frame reordering (v38), resolution (v40), audio/video
lockstep (v41-v43), and even forcing software encoding (v44) all failed
identically or nearly identically: none of them address a stuck
notification, because none of them were the actual mechanism.

## Fix: rewrite around AVFoundation's own async request API

Manually polling `readyForMoreMediaData` in a spin loop is a
documented-supported pattern for `expectsMediaDataInRealTime = NO`, but
evidently isn't reliably observable on this system for a clip and
pipeline configuration like this one. `braw_proxy_tool.mm`'s entire
video+audio writing section was rewritten around Apple's actual
recommended pattern instead: `-[AVAssetWriterInput
requestMediaDataWhenReadyOnQueue:usingBlock:]`. Each track gets its own
serial GCD queue and its own block; AVFoundation itself calls each block
whenever that specific track wants more data, rather than this tool
polling a property it can't reliably observe changing. Key structural
changes:

- `nextFrameIndex`, `anyFailed`, `videoDone`, `audioDoneFlag` are
  `__block` variables shared between the video and audio blocks (each
  block only runs on its own serial queue, so no additional locking is
  needed for these).
- `callback` (the `ToolCallback` instance registered via
  `codec->SetCallback(&callback)`) is captured as a raw pointer
  (`callbackPtr`), not by value -- a block literal makes its own const
  copy of a plain captured C++ object, which would silently decouple
  the block's copy from the actual instance the BRAW SDK calls back
  into.
- The main thread now supervises completion via two
  `dispatch_semaphore_t`s (one per track), waited on with a short
  (100ms) timeout in a loop rather than `DISPATCH_TIME_FOREVER`,
  specifically so a writer that enters `AVAssetWriterStatusFailed` (which
  stops AVFoundation from ever re-invoking either block again) can still
  be detected and reported instead of hanging forever.
- `EmitLine`/`Trace` (stdout/stderr output) now take a `std::mutex`,
  since video and audio genuinely run concurrently on separate queues
  now and could otherwise interleave mid-line and corrupt the JSON-lines
  protocol.
- The now-unused `PumpRunLoopBriefly()` (and its comment, which had
  incorrectly called the v35 fix "confirmed root cause" -- itself later
  disproven in v36) was deleted.
- The manual audio-interleaving-by-presentation-time logic from v41-v43
  was removed entirely -- proven unnecessary by v43's own trace (audio
  stayed in near-perfect lockstep and the stall still occurred
  identically), and Apple's own async pattern handles multi-track
  interleaving internally without needing this tool to pace it.

## Verification -- confirmed directly against the real clip for the first time

Rebuilt clean. `./test_av_settings.sh`: all pass. `main.py --selftest`:
204/204, zero regressions. SDK's own `sample.braw`: still produces
valid, non-corrupted H.264 (1920×1080) + `pcm_s24le` (48kHz stereo)
output (confirmed via `ffprobe` and OpenCV).

Then, for the first time in this entire investigation, **run directly
against the user's actual real file** (`A001_07191440_C002.braw`, 1925
frames, 6K, 4-channel audio) from this environment, rather than asking
the user to test blind: **completed successfully end to end** --
reached "frame 1925 of 1925", wrote remaining audio, finalized, emitted
a clean `result` line, exit 0. `ffprobe` on the output confirms: H.264
1920×1080 @ 30fps, exactly 1925 frames; `pcm_s24le` 4-channel audio,
3,080,000 samples (= 1925/30s of audio, exactly matching video
duration). OpenCV opened and read frames at indices 0, 500, 1000, 1500,
and 1924 (the very last frame) -- every one decoded correctly with
distinct, plausible, non-degenerate pixel values, confirming the whole
clip's content is intact end to end, not just nominally "complete."

**This is the first fix in the entire investigation verified directly
against the real failing clip rather than reported back by the user.**
The proxy generation stall is resolved.

# Addendum v46 — hardware encoding re-confirmed safe; forced-software-encoder scaffolding from v44 removed

With the real root cause fixed (v45), the user asked whether hardware
encoding (forced to software in v44, while the actual bug was still
undiagnosed) also works now. Re-tested directly against the real clip
with `kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder`
flipped back to `YES`: **completed successfully** -- frame 1925 of 1925,
clean `result` line, exit 0. `ffprobe` confirms identical output shape
(1920×1080 H.264 @ 30fps, 1925 frames; 4-channel `pcm_s24le` audio,
3,080,000 samples). OpenCV confirmed frames at indices 0/500/1000/1500/
1924 all decode correctly with real, distinct content (nearly identical
pixel means to the software-encoded run, as expected from two H.264
encoders producing very similar but not bit-identical output). This
confirms the hardware encoder was never actually broken -- the v45 fix
(switching from manual polling to `requestMediaDataWhenReadyOnQueue:`)
was the real and complete fix; forcing software encoding in v44 was a
reasonable hypothesis at the time but turned out to be unrelated once
the actual mechanism was understood.

Since explicitly requesting hardware acceleration is just
VideoToolbox's own default behavior, the `AVVideoEncoderSpecificationKey`
/ `encoderSpecification` dictionary was removed entirely rather than
left in as a no-op -- along with the now-unnecessary
`#import <VideoToolbox/VideoToolbox.h>` and `-framework VideoToolbox`
link in `build.sh`. A comment at the video settings block points future
readers to this addendum and v44/v45 in case anyone wonders why
VideoToolbox-specific code was tried and later removed.

Rebuilt clean. `./test_av_settings.sh`: all pass. `main.py --selftest`:
204/204. Re-ran the cleaned-up build directly against the real clip
once more: still completes successfully end to end, confirming the
cleanup introduced no regression. Proxy generation for this real 6K
Pyxis clip now uses hardware encoding (faster than the software
fallback) and completes correctly.

# Addendum v47 — post-fix cleanup: removed both temporary diagnostics, corrected stale comments

With the real fix (v45) and hardware-encoding re-confirmation (v46)
both verified against the real clip, two categories of leftover
scaffolding from the investigation were cleaned up.

## Diagnostics removed

Both `BRAW_PROXY_TOOL_DEBUG` (per-frame stderr tracing) and
`BRAW_PROXY_TOOL_SKIP_ENCODE` (decode-only isolation mode) were
explicitly commented "TEMPORARY -- remove once root-caused and fixed"
when added (v34, v37). Asked the user whether to keep either now that
the bug is fixed; the answer was to remove both. Removed:

- `g_debugTrace`, `Trace()`, and every call site (was scattered through
  `ToolCallback::ReadComplete`/`ProcessComplete` and the video writing
  block).
- `m_frameIndex` on `ToolCallback` -- existed solely for `Trace()`
  labeling, so `ResetForNextFrame()` no longer takes a frame-index
  parameter at all.
- `g_skipEncode` and the entire `SKIP_ENCODE` code path in `main()`.
- The `BRAW_PROXY_TOOL_DEBUG`/`BRAW_PROXY_TOOL_SKIP_ENCODE` `getenv()`
  reads.

`g_stdoutMutex` stays (still guards `EmitLine`, needed now that video
and audio genuinely run concurrently on separate GCD queues); only the
comment referencing `Trace` was updated since that function is gone.

## Comments corrected

Three comments asserted specific fixes (resolution downscaling, the
frame-rate-aware bitrate formula, disabling B-frame reordering) as "the
leading fix" or "next suspect" for the real stall -- all written before
the actual root cause (v45) was known. Now that it is, leaving those
claims as-is would mislead any future reader into thinking those
changes were load-bearing for the fix. All three now read as: what they
were suspected of at the time, a pointer to the addendum that disproved
it, and the honest reason each change stayed anyway (each is still a
legitimate improvement on its own terms -- correct bitrate-vs-framerate
math, no B-frames needed for a scrubbing proxy, downscaling to an
actually-useful preview resolution -- just not why the stall was fixed).
`README.md`'s implementation notes updated to match (the stale
"software encoding forced" bullet replaced with the actual current
state: hardware encoding, VideoToolbox's own default).

## Verification

Rebuilt clean. `./test_av_settings.sh`: all pass. `main.py --selftest`:
204/204, zero regressions. Re-ran directly against the real clip once
more post-cleanup: still completes successfully end to end (frame
1925/1925, clean `result` line) -- confirms none of the removed
scaffolding was accidentally load-bearing.

# Addendum v48 — fix: B-Roll Analyze errored on BRAW files still legitimately queued behind others

Reported by the user: analyzing a folder with several `.braw` files
generated a proxy for the first file, then B-Roll's analyze job started
processing every file and errored on some BRAW clips with "This BRAW
clip's proxy is taking longer than expected to generate." This is
exactly the trade-off v31/v32 already called out as a known, unfixed
gap (see v32's "Known trade-off, not fixed here").

## Root cause

Two numbers didn't account for each other:

- `jobs.py`'s `"braw_proxy"` kind limit was 1 -- only one proxy ever
  transcodes at a time; the rest of a folder's proxy jobs sit queued
  and don't even START until the one ahead of them finishes.
- `braw_bridge.wait_for_decode_path`'s timeout was a fixed 1200s (20
  min), counted from when a B-Roll pool worker begins waiting on ITS
  file, not from when that file's proxy job actually starts running.

With real BRAW clips taking ~800-900s each to transcode (measured in
v46's timing comparison), the second and later files' proxy jobs don't
even start generating until the first finishes -- so a later file's
20-minute wait window can elapse before its proxy has even begun,
producing a "taking longer than expected" error for a proxy that was
never stuck, just legitimately still in line. Compounding it:
`wait_for_decode_path` runs inside B-Roll Analyzer's own venv
subprocess, with zero visibility into the suite's in-process
`JobManager` -- it can only poll the on-disk proxy cache
(`braw_proxy_cache.find_cached_proxy`), so a proxy job that's still
queued and one that's genuinely crashed look identical after the same
1200s: there's no failure signal to short-circuit on, only silence.

## Fix

Per the user's explicit choice (asked because each option alone is a
partial fix with its own trade-off; the user chose to combine both):

- **`backend/jobs.py`**: `"braw_proxy"`'s kind limit raised from 1 to 2
  -- real parallelism for the common 2-3-file case, without assuming
  the machine can sustain much more concurrent BRAW decode+encode work
  than that (untested above 2).
- **`backend/braw_bridge.py`**: `BRAW_PROXY_WAIT_TIMEOUT_SECONDS` raised
  from 1200.0 to 86400.0 (24h) -- effectively unbounded. Safe to wait
  this long precisely because the wait already only ever runs inside an
  already-parallel per-file worker (never the single discovery/dispatch
  loop), per the existing docstring contract on `wait_for_decode_path`
  -- so waiting longer costs that one pool slot, never the rest of the
  batch. A genuinely wedged proxy tool (not observed since v45's fix)
  would now hang that one file's wait rather than failing fast with a
  message pointing at the Jobs drawer; accepted as the better failure
  mode than a false "taking longer than expected" on a proxy that's
  actually fine.

**Still not fixed**: `wait_for_decode_path` still has no way to detect
an actually-failed proxy job short of it never appearing in the cache
-- that would need threading the job's real status back across the
process boundary (e.g. a failure marker written to
`braw_proxy_cache`'s index, or a shared status file), not attempted
here. A folder with many more BRAW files than 2 will still see the
later ones queue for a long time before their proxy even starts, just
no longer erroring while they wait.

## Verification

`main.py --selftest`: 204/204, zero regressions.

# Addendum v49 — feature: size cap + oldest-first eviction on assets/proxies/

Prompted by the user asking where proxies are saved and whether they're
ever cleaned up — they weren't: every generated proxy accumulated in
`assets/proxies/` forever, uncapped, with `braw_proxy_cache.forget_proxy`
existing but never called from any real code path (only from tests, to
force a regeneration). For a suite whose proxies are ~85-90MB each
(measured off real 6K clips post-downscale, v45/v46's testing), that's
unbounded growth for any heavy BRAW workflow. User asked for a size cap
with oldest-file eviction to make room for new ones.

## Design

- **`backend/braw_proxy_cache.py`**: `proxy_cache_max_bytes()` (default
  25 GiB, `_DEFAULT_PROXY_CACHE_MAX_BYTES`, override via the
  `STUDIO_SUITE_PROXY_CACHE_MAX_BYTES` env var) and `enforce_cache_cap
  (protect_path=None)`, called from `register_proxy()` after every fresh
  proxy is recorded.
- **"Oldest" = the proxy FILE's own mtime** (generation time), not the
  source `.braw`'s mtime (which the index already tracks for a totally
  different reason — staleness detection — and would answer the wrong
  question here: a clip shot months ago but proxied five minutes ago
  should NOT be evicted before one shot yesterday but proxied a week
  ago). No index schema change needed — the file's own stat() already
  carries this.
- **Scans the directory, not just the index** (`_proxy_files_by_age`):
  a `.mov` under `assets/proxies/` counts against the budget and is
  evictable even if the index lost track of it (e.g. a crashed/
  cancelled run's leftover — this suite already had two 0-byte orphans
  on disk from exactly that, found while investigating this feature).
  Robustness against orphans was a deliberate design choice, not
  incidental — the alternative (trusting the index alone) would let
  exactly the kind of file most worth reclaiming silently escape the
  cap.
- **Soft cap, lazy enforcement**: nothing prunes proactively at launch
  or on a timer — the cap is only checked right after a new proxy is
  registered. A freshly-written proxy is never evicted by its own
  cap-enforcement pass (`protect_path`), even if it alone exceeds the
  whole budget — this is a cleanup pass, not a "reject new work" gate;
  the true hard limit is still just whatever the disk itself allows.
- **Best-effort throughout**, matching every other method in this
  module: a file that can't be removed (e.g. mid-flight from another
  process) is skipped, not raised.

**Not attempted here**: no UI surfacing of current cache usage or the
configured cap, no way to change the cap from the frontend (env var
only) — Settings-panel territory, not asked for in this pass.

## Verification

Five new tests added to `tests/test_braw_proxy_cache.py`: eviction of
the single oldest file when a new one would push the folder over a
tiny test cap; the evicted entry's index row is dropped too (no stale
hit after deletion); a proxy larger than the entire cap survives its
own registration; nothing is evicted while under cap; an orphaned
`.mov` with no index entry at all still counts toward the budget and
still gets evicted first. `main.py --selftest`: 209/209 (204 + 5 new),
zero regressions.

# Addendum v50 — Phase 2 complete: Transcribe + Edit-preview BRAW substitution

Addenda v28/v30/v31 explicitly left two workspaces unfinished for Phase 2
("transparent substitution" — every real decode of a `.braw` file points
at a cached ordinary-container proxy instead, suite-side only): Transcribe
and Edit-preview (RCS's own embedded editor). Both are done now.

## Transcribe (suite-side only, same pattern as Sync)

- **`backend/api_transcriber.py`**: `transcriber_start` now calls
  `braw_bridge.queue_missing_proxies(self.jobs, paths_list)`
  fire-and-forget before starting its per-file `"transcribe"` subprocess
  jobs (mirrors `broll_start`); the queued list comes back in the response
  as `braw_proxy_jobs`.
- **`backend/workers/transcribe_worker.py`**: same `SUITE_BACKEND_DIR`
  sys.path insert + `import braw_bridge` used by `sync_worker.py`/
  `broll_worker.py`. At the one actual decode site (`run()`'s
  `source_path = audio_path or video_path`, only reached when there's no
  synced external audio), `source_path` now resolves through
  `braw_bridge.wait_for_decode_path(video_path)` — safe unconditionally
  (a no-op for any non-`.braw` path) and safe to block on, since this
  worker runs strictly one file per process (never a shared dispatch
  loop another file queues behind). `video_path` itself is never
  reassigned, so `write_ivt_cache`'s `.ivt-cache.json` keeps keying on the
  ORIGINAL path with no swap-back logic needed — unlike `broll_worker.py`,
  which does need one.
- **Known, accepted limitation**: `.ivt-cache.json` is written next to
  `video_path`. If that's a `.braw` file still on read-only/removable
  camera media, the write fails — exactly as it already would for ANY
  file type in that situation. Pre-existing behavior this suite's own
  cache-writing code mirrors from the standalone app's convention (for
  interop), not a regression BRAW introduces; not fixed here.

## Edit-preview

Turned out NOT to be self-contained like every other workspace: RCS gates
every linked media path through a fixed extension allowlist
(`VIDEO_EXTENSIONS`, `Rough Cut Studio/backend/transcript_parser.py:353`,
enforced by `api.py`'s `_is_allowed_media_path`) used by BOTH the
transcript-import security gate (`api_security.py`'s
`_suite_prune_disallowed_media`) and RCS's own project-load gate. Without
`.braw` in that list, a linked `.braw` source was pruned before any
preview code ever ran — asked the user how to handle it (three options:
edit RCS's own list with permission, monkeypatch the check from suite-side
code only, or skip Edit-preview for this pass); **the user chose to add
`.braw` to RCS's own `VIDEO_EXTENSIONS` tuple** — the one sibling-app-file
edit in this addendum, everything else stays suite-side-only.

- **`Rough Cut Studio/backend/transcript_parser.py:353`**: `VIDEO_EXTENSIONS`
  gains `".braw"`. Confirmed (grep) this tuple has exactly four consumers,
  all plain extension-membership checks, never content parsing — the
  embedded-path/fallback matcher in `detect_linked_media`,
  `_is_allowed_media_path`, its own error message, and
  `batch_relink_media`'s stem-matching folder scan. This one line is also
  what makes `batch_relink_media` start matching `.braw` files
  automatically — no other RCS-side change needed.
- **`backend/suite_api.py`**, new "Edit-workspace BRAW substitution"
  section (same file as the existing `save_xml` splice override — Edit
  has no dedicated mixin of its own):
  - `_with_braw_proxy_substituted(source_id, call)`: if the source's
    linked media isn't `.braw`, calls straight through; otherwise resolves
    its cached proxy (or returns the same "proxy hasn't finished
    generating yet" error used elsewhere), points `self.media_paths
    [source_id]` at the proxy for the duration of `call()`, and restores
    the original `.braw` path in a `finally` block no matter how `call`
    turns out. Safe because `media_paths` is a plain mutable dict owned
    by RCS's `SourceManager` (`sources.py:81`) and proxied through a
    get/set `@property` on `Api` — no RCS file needs touching to swap it.
  - `get_thumbnail`/`get_preview_url` — thin overrides through that
    helper; 100% of RCS's own thumbnail/preview-server logic reused via
    `super()`, nothing duplicated.
  - `export_video_preview` — needs EVERY `.braw` source in the current cut
    substituted at once (RCS decodes them all in one ffmpeg run); swaps
    every one up front, bails with a single clear error if any lack a
    cached proxy (rather than a confusing partial failure deep inside
    ffmpeg), restores all of them in `finally`.
  - `link_media_file`/`batch_relink_media` — eagerly fire
    `braw_bridge.queue_missing_proxies` for any newly-linked `.braw` path,
    same "queue eagerly, resolve lazily" philosophy as every other
    workspace's own start action, so a proxy is more likely to already be
    cached by the time a preview/thumbnail/export is first requested.
- **`backend/api_security.py`**: `_add_transcript`/`pick_transcript_files`
  (already shadowed here for the SEC-2 allowlist prune) now also call a
  small new `_queue_braw_proxies_for_linked_media()` helper afterward —
  covers the third way a `.braw` source gets linked (an embedded
  `NOTE Source video:` auto-link, e.g. from "Send to Edit"), alongside
  `link_media_file`/`batch_relink_media` above.
- **Deliberately NOT touched**: `_display_clip_name`,
  `_build_project_dict`/`_apply_loaded_project_unsafe`, and
  `_finalize_outputs` (XML/OTIO export) all reference `media_paths` for
  DISPLAY or for METADATA meant for external tools (Premiere/Resolve/OTIO)
  to resolve themselves — matching this suite's established "export
  always references original media" convention. A real NLE with a BRAW
  plugin decodes the real `.braw` natively; only Studio Suite's own
  embedded ffmpeg-based preview/thumbnail/export-preview pipeline can't.
  Also not touched: `sources.py`'s `link_media_file` file-picker filter
  string (hardcoded, not derived from `VIDEO_EXTENSIONS`) — the user's
  go-ahead covered the `VIDEO_EXTENSIONS` tuple specifically; a `.braw`
  can still be picked via "All files" in that same dialog today.

## Still out of scope (unchanged from v28/v30/v31)

Phase 3's broader allowlist-gating work beyond this one `.braw` addition,
Phase 5's Jobs-drawer UI for `braw_proxy` jobs, embedded timecode in the
generated proxy, and (new, from Part A above) the read-only-media
`.ivt-cache.json` write limitation.

## Verification

Five new test files/extensions: `tests/test_transcribe_braw_real.py`
(new — real SDK/tool + real IVT venv, end-to-end against the SDK's
`sample.braw`, including the same "no preexisting proxy, don't lose the
race" case already covered for B-Roll/Sync); `tests/test_transcriber_api.py`
(+1, mocked `queue_missing_proxies` call); `tests/test_suite_api_edit_braw.py`
(new, 11 tests — substitution/restore/error-path coverage for all five
overrides, using the real `api` fixture so RCS's actual logic runs via
`super()`); `tests/test_api_security_braw.py` (new, 2 tests — the
transcript-auto-link path); `tests/test_braw_bridge.py` (+1 — guards the
one sibling-file line against an accidental future revert, since RCS has
no pytest suite of its own). `main.py --selftest`: 226/226 (209 + 17 new),
zero regressions.

# Addendum v51 — Phase 3: extension-allowlist gating (dialog filters)

Phase 2 (v30/v31/v50) made every actual DECODE of a `.braw` file work
transparently, but discovery still had a gap: a `.braw` file was only
ever found via a FOLDER scan (`braw_bridge.find_braw_files`) — every
single-file OPEN dialog's `file_types` filter still excluded `.braw`, so
picking one directly required switching the dialog to "All files" first.
Phase 3 closes that, across every such dialog in the suite (inventoried
via a full grep pass — confirmed exactly three exist, plus one frontend
client-side gate):

- **`backend/api_shared.py`**: `VIDEO_DIALOG_TYPES` (used by
  `transcriber_pick_videos`) and `SYNC_VIDEO_DIALOG_TYPES` (used by
  `sync_pick_video`) both gain `;*.braw` in their filter string. Both are
  suite-owned constants — no permission needed.
- **`Rough Cut Studio/backend/sources.py`'s `link_media_file`** — the one
  sibling-file dialog filter (hardcoded, not derived from
  `VIDEO_EXTENSIONS`) explicitly left untouched in v50 pending a fresh
  go-ahead. Asked the user this time specifically for this one line;
  approved. `file_types` now includes `*.braw`.
- **`frontend/suite.js`'s `TED_VIDEO_EXTENSIONS`** (gates whether the
  Transcribe editor's reference-video toggle/jump controls show at all)
  gains `.braw` — purely cosmetic, since `broll_preview_url` (which this
  player already calls) has resolved `.braw` through its cached proxy
  since v30; without this the toggle would stay hidden for a `.braw`
  source even though playback would have worked. `node --check
  frontend/suite.js` passes.

**Confirmed NOT needed**: `PREVIEW_VIDEO_EXTENSIONS` (api_shared.py) is
deliberately left without `.braw` — `_resolve_playable_path`
(api_broll.py) special-cases `.braw` in its own branch before ever
consulting that list, so adding it there would be redundant, not
corrective. No other `ext not in (...)`-style rejecting check exists
anywhere in `Studio Suite/backend/` beyond what's already covered.
B-Roll/Sync/Local Interview Transcriber/Blair Brander's own backends have
no `create_file_dialog` calls of their own at all — they're reached only
as subprocess workers, never for a native picker, so there was nothing
to extend there.

## Still out of scope

Phase 5's Jobs-drawer UI for `braw_proxy` jobs, embedded timecode in the
generated proxy, and the read-only-media `.ivt-cache.json` write
limitation (v50) remain unimplemented.

## Verification

Three new guard tests in `tests/test_braw_bridge.py`
(`test_transcriber_video_dialog_types_includes_braw`,
`test_sync_video_dialog_types_includes_braw`,
`test_rcs_link_media_file_dialog_includes_braw` — the last one via
`inspect.getsource` since RCS's filter is a literal inside a method body,
not a module constant). `main.py --selftest`: 229/229 (226 + 3 new), zero
regressions.

# Addendum v52 — Phase 5: Jobs-drawer UI for braw_proxy jobs

`"braw_proxy"` jobs already flowed through the exact same generic job
list every other kind uses (`jobs.py`'s `Job`/`to_dict()` — kind, label,
status, progress, detail, error), but the frontend had zero
`braw_proxy`-specific treatment: no entry in `frontend/suite.js`'s
`JOB_ICONS`/`JOB_KIND_LABELS` lookup tables (fell back to a generic "●"
icon and the raw string `"braw_proxy"` as its detail line), and no CSS
for a `"queued"` job's status pill at all — it silently fell through to
the same unstyled default as every other status, making a proxy job
sitting in `jobs.py`'s per-kind concurrency queue (throttled to 2 since
addendum v48) visually indistinguishable from one that was actually
stuck.

## Fix

- **`frontend/suite.js`**: `JOB_ICONS.braw_proxy = "◈"`,
  `JOB_KIND_LABELS.braw_proxy = "BRAW Proxy"` — both plain lookup-table
  additions, no new rendering mechanism needed (`renderDrawer`'s card
  template is already fully kind-agnostic apart from these two tables
  and the `done`-state extras chain, which `braw_proxy` deliberately
  doesn't need an entry in: unlike transcribe/broll/sync, there's no
  user action to take on a finished proxy — it's just used automatically
  wherever that clip is opened next).
  `statusCls`'s ternary chain gained a `j.status === "queued" ?
  "is-queued"` branch (previously fell through to `""`, no override).
- **`frontend/suite.css`**: new `.suite-job__status.is-queued` rule
  (`color: var(--text-muted); background: var(--bg-panel-raised)`) —
  distinct from both the plain default and `is-paused` (same text color,
  different background), built entirely from the existing shared color
  tokens (RCS's `style.css` root palette is small and deliberately not
  touched — no new global token invented). This benefits `"transcribe"`
  jobs' own queueing (throttled to 1) too, not just `braw_proxy` — the
  gap was never kind-specific, just never given styling.
- The job's `detail` line already read something informative for a
  queued job (`jobs.py`'s `_start_or_queue`: `"Waiting for an earlier
  job to finish…"`) and `label` was already just the clip's filename
  (`braw_bridge.py`'s `request_proxy`, unchanged) — both already
  consistent with every other kind's convention, so neither needed a
  backend change.

## Verification

`node --check frontend/suite.js` passes. Visually verified in the
Browser preview pane against the real composed `frontend/_generated/`
output (three hand-injected `braw_proxy` job cards — queued/running/done)
— confirmed the QUEUED pill now reads as a distinct muted "waiting" look
next to RUNNING's amber and DONE's teal, the ◈ icon renders, and detail
lines are clear. No backend changed in this pass, so `main.py --selftest`
stays at 229/229, zero regressions.

## Still out of scope

Embedded timecode in the generated proxy, and the read-only-media
`.ivt-cache.json` write limitation (v50), remain unimplemented. No
further phases remain in the original numbered plan (Phase 0 through 3,
plus 5, are now all done — no "Phase 4" was ever defined).

# Addendum v53 — verified: BRAW export XML wiring was already correct

Addendum v30 flagged "export XML wiring beyond what `rebuild_from_cache`'s
file-list change already covers" as not done, and no later addendum ever
closed it — no test exercised `broll_export_xml` or `sync_export_xml`
against a real `.braw` clip. Asked whether to close this gap; the user
said yes.

## What was actually true

Traced both export paths before writing anything:

- **B-Roll**: `rebuild_from_cache` → `result_cache.result_from_entry` →
  B-Roll Analyzer's own `rescore_clip` (`analyzer.py:747`, its own
  docstring: "without re-decoding or re-sampling the source video") →
  `xml_export.export_xml` (confirmed via grep: no `ffprobe`/`cv2` call
  anywhere in that file) — every step operates on already-cached data or
  plain strings, never touching the file again. `result.path` is already
  the ORIGINAL `.braw` path (swapped back before the cache write, v30).
- **Sync**: `sync_export_xml`'s own docstring already states probes "come
  from the sync job result / `sync_probe`... so no re-probing happens
  here" — `sync_xml.build_sync_xml` likewise just writes already-known
  probe data into XML, `video_path` untouched.

So the code was already correct by construction — the gap was that this
had never been PROVEN, only assumed. No source change was needed; this
addendum is pure verification.

## Tests added

- **`tests/test_broll_braw_real.py`**: `test_braw_clip_export_xml_end_to_end`
  — real analyze run against the SDK's `sample.braw`, then
  `broll_export_xml` (via a `_FakeWindow` stand-in scripting the save
  dialog's result, since there's no real pywebview window in a test).
  Asserts the exported XML contains the ORIGINAL `.braw` path and does
  NOT contain the ephemeral proxy path.
- **`tests/test_sync_braw_real.py`**: `test_braw_video_sync_export_xml_end_to_end`
  — same idea for `sync_export_xml`, built from a real `sync_start`
  result's `video`/`tracks` payload.

Both passed on the first run — no bug found, no fix needed.

## Verification

`main.py --selftest`: 231/231 (229 + 2 new), zero regressions.

# Addendum v54 — fix: proxy job failures now signal fast instead of hanging the full timeout

Closes the gap addendum v48 explicitly left open ("Still not fixed:
`wait_for_decode_path` still has no way to detect an actually-failed
proxy job short of it never appearing in the cache"). Requested
alongside the other two remaining known limitations.

## Fix

- **`backend/braw_proxy_cache.py`**: new `record_proxy_failure(source_path,
  error_message)` / `find_proxy_failure(source_path)` /
  `clear_proxy_failure(source_path)`, storing a `failures` dict alongside
  the existing `entries` dict in the same `index.json` — purely additive
  to the schema, so `load_index()`'s existing corruption-tolerant
  defaulting already makes old index files load fine (`INDEX_VERSION`
  stays 1, no migration needed).
- **`backend/braw_bridge.py`**:
  - `request_proxy` clears any stale failure for `source_path` right
    before starting a fresh job (so a retry is never shadowed by a
    previous attempt's error), and wraps the job's `run()` closure to
    record a failure on any exception that isn't a cooperative
    cancellation, then re-raises unchanged — `JobManager`'s own
    error/status handling is completely untouched.
  - `wait_for_decode_path` now polls `find_proxy_failure` alongside
    `find_cached_proxy` in its wait loop, returning
    `"BRAW proxy generation failed: {failure}"` immediately once one
    appears, instead of waiting out the (now effectively unbounded, v48)
    timeout.
  - `resolve_decode_path` (the immediate, no-wait check) gained the same
    failure check, so it no longer says the misleading "hasn't finished
    generating yet" once something has actually and definitively failed.

**Still not fully closed**: this only detects a failure the compiled
proxy tool itself reported (non-zero exit, a `{"type":"error"}` protocol
line, or no result file). A tool that hangs indefinitely without ever
exiting (not observed since the v45 async rewrite) still has no
independent watchdog — `wait_for_decode_path`'s wait is bounded but
effectively unbounded (86400s) for that specific case, unchanged from v48.

## Verification

Four new tests in `tests/test_braw_bridge.py`: a failed `request_proxy`
job's error is recorded and found; a fresh `request_proxy` call clears a
stale failure before starting; `wait_for_decode_path` returns a recorded
failure in well under a second against a 60s timeout (proving it fails
fast, not just correctly — a regression back to polling only
`find_cached_proxy` would make this specific test hang, not just fail);
`resolve_decode_path` reports the same failure immediately. `main.py
--selftest`: 235/235 (231 + 4 new), zero regressions.

# Addendum v55 — fix: .ivt-cache.json / .sync-offsets.json centralized for .braw sources

Closes the second remaining known limitation. `.ivt-cache.json` and
`.sync-offsets.json` both normally live next to their video (matching the
standalone Local Interview Transcriber's own convention, for interop) —
but a `.braw` source routinely lives on read-only/removable camera media
where that write isn't reliable, the exact same reasoning that already
centralizes proxies under `assets/proxies/` instead of next to the
source. While designing this, found the identical problem also affects
`.sync-offsets.json` (`api_sync.py`'s `sync_save_offsets`, written next
to the source when a video is synced before being transcribed) — asked
the user, who confirmed fixing both sidecars together rather than leaving
a half-fixed state.

## Fix

- **`backend/paths.py`**: new `IVT_CACHE_DIR` (`assets/ivt_cache/`),
  added to `ensure_suite_dirs()`.
- **`backend/braw_bridge.py`**: new `ivt_cache_path(video_path)` /
  `sync_offsets_path(video_path)` — next to the video unchanged for any
  ordinary file; for a `.braw` source, a hash-keyed path under
  `IVT_CACHE_DIR` (same `sha1(abspath)` convention as
  `braw_proxy_cache.py`, but **no index file** — unlike a proxy, a
  sidecar's location is always recomputed deterministically from
  `video_path` by whichever caller already has it, never reverse-looked-
  up). No interop lost: the standalone transcriber can't decode `.braw`
  at all, so it was never going to read either sidecar for one anyway.
  Suffix strings duplicated by hand from `api_shared.py`'s
  `IVT_CACHE_SUFFIX`/`SYNC_OFFSETS_SUFFIX` (this module stays stdlib +
  `paths.py`/`braw_proxy_cache.py` only, so it can't import
  `api_shared.py` — same hand-duplication precedent as that module's own
  `WHISPER_MODELS` comment).
- **Every read/write site switched to these** — no staleness-detection
  changes needed anywhere, purely a location change:
  - `backend/api_transcriber.py`'s `_ivt_cache_path` classmethod now
    delegates to `braw_bridge.ivt_cache_path` — this ONE change cascades
    to every caller (`_read_ivt_cache`, `transcriber_load_cache`,
    `transcriber_update_transcript`, `transcriber_send_to_edit`,
    `..._send_cache_to_edit`) AND to `api_sync.py`'s
    `sync_save_offsets`/`sync_load_offsets`, which reach the transcription
    cache via this same inherited method on the composed `SuiteApi`.
  - `backend/workers/transcribe_worker.py`'s `write_ivt_cache` switched
    both its cache-path and its sync-offsets-sidecar-read paths (the
    latter previously used a locally-duplicated `SYNC_OFFSETS_SUFFIX`
    constant, now removed as dead code).
  - `backend/api_sync.py`'s `_sync_offsets_path` staticmethod now
    delegates to `braw_bridge.sync_offsets_path`.
  - `backend/synced_audio_splice.py`'s `discover_synced_audios` — found
    TWO direct reads here while investigating (its own locally-duplicated
    `IVT_CACHE_SUFFIX` AND `SYNC_OFFSETS_SUFFIX` constants, both now
    removed), not just the one originally scoped; both switched to the
    shared `braw_bridge` functions (new import added to this file).

## Verification

New tests: `tests/test_braw_bridge.py` — `ivt_cache_path`/
`sync_offsets_path` unchanged for an ordinary video, redirected to the
fallback dir for `.braw`, deterministic (same input → same output), and
the two sidecar kinds for the same video never collide. `tests/
test_sync_offsets.py` — a `.braw` video's `sync_save_offsets`/
`sync_load_offsets` round-trips correctly through the new location.
`tests/test_transcribe_braw_real.py`'s real end-to-end test extended to
confirm the cache lands under `paths.IVT_CACHE_DIR`, not next to the
(temp, standing in for read-only media) `.braw` file, and does NOT exist
at the old next-to-source path. `main.py --selftest`: 241/241 (235 + 6
new), zero regressions.

# Addendum v56 — fix: embedded timecode in the generated proxy

Closes the last remaining known limitation. `tools/braw/braw_proxy_tool.mm`
now writes a real QuickTime `tmcd` timecode track — previously the
generated proxy carried none at all, so `A-Sync/sync_core.py`'s
"timecode" sync method always reported "no embedded timecode found" for
a `.braw` source, regardless of what the camera actually recorded.

## Design

Researched the exact BRAW SDK API and AVFoundation track-association
signature (verified against the real installed headers, not assumed,
after an earlier draft of the association call guessed a writer-level
API that turned out to be an `AVAssetWriterInput` instance method
instead) before writing any code:

- **`IBlackmagicRawClip::GetTimecodeForFrame(0, ...)`** — the STARTING
  timecode, a cheap metadata lookup (never a per-frame decode).
  **`IBlackmagicRawClipEx::QueryTimecodeInfo`** (via `QueryInterface`,
  mirroring the existing `IID_IBlackmagicRawClipAudio` pattern already
  in this file) — the drop-frame flag. Both queried once, right after
  the existing width/height/frameRate/frameCount reads.
- **A single QuickTime timecode sample spanning the whole clip** — the
  standard convention (a reader derives every other frame's timecode by
  counting forward from this one starting value at the track's own
  frame rate), not one sample per frame. Converted from the parsed
  timecode string via the standard SMPTE 12M-1 drop-frame formula (a new
  small static `ParseTimecodeToFrameNumber` helper).
- **`CMTimeCodeFormatDescriptionCreate`** (`kCMTimeCodeFormatType_TimeCode32`)
  + a new `AVAssetWriterInput` (`AVMediaTypeTimecode`), added to the
  writer alongside video/audio (before `startWriting`), associated via
  `[videoInput addTrackAssociationWithTrackOfInput:timecodeInput
  type:AVTrackAssociationTypeTimecode]` — confirmed via the real
  AVFoundation headers that this is an instance method called ON the
  video input (not a writer-level or timecode-input-level call).
- **Written synchronously**, immediately after `startSessionAtSourceTime:`
  and before either async video/audio block is even registered — no
  `requestMediaDataWhenReadyOnQueue:`/`expectsMediaDataInRealTime`
  involvement at all for this one track, so it can't reintroduce
  anything resembling the video/audio async stall this tool was so
  hard-won to fix (addendum v45/v46).
- **Degrades gracefully** at every step (unparseable timecode, format
  description creation failure, writer rejecting the input) — the track
  is simply skipped, never a hard failure; the proxy is still fully
  usable without it, same posture as the optional audio track.
- **No `A-Sync`/`sync_worker.py` changes needed** — `sync_core.py`'s
  existing `probe()` already looks for any `codec_type: "data"` stream
  with `codec_tag_string == "tmcd"`, generically, so a real timecode
  track just starts working the moment one exists.

## Verification

Ran the rebuilt tool directly against the SDK's real `sample.braw`:
`ffprobe -show_streams` confirmed a genuine `tmcd` data stream reporting
`tags.timecode=22:23:40:20` — the clip's own real embedded starting
timecode (round-tripped exactly, not a value invented for testing) — with
the association correctly propagating that same tag onto the video
stream too (only happens when the track association is genuinely
correct, not just present). `tests/test_sync_braw_real.py`'s
`test_braw_video_probe_and_peaks_end_to_end`, which previously asserted
`probe["timecode_tag"] is None` as the documented limitation, now asserts
the real `"22:23:40:20"` value — through the full suite pipeline
(`api.sync_probe`), not just a manual `ffprobe` check. `./test_av_settings.sh`
still all-pass (unaffected — that harness never touches timecode).
`main.py --selftest`: 241/241, zero regressions (no new Python tests
needed; this is native-tool behavior already exercised by the existing
real end-to-end test suite). `tools/braw/README.md` and
`sync_worker.py`'s module docstring updated to describe the new
capability instead of the limitation.

## This closes the BRAW compatibility plan

All three previously-remaining known limitations (embedded timecode,
`.ivt-cache.json`/`.sync-offsets.json` on read-only media, proxy-failure
signaling) are now fixed. Combined with every phase already being done
(v53), there is no further outstanding BRAW compatibility work tracked
anywhere in this document as of this addendum.

# Addendum v57 — fix: Copy workspace false card detection, checkbox-only selection, auto-select-on-scan

Three issues reported by the user in the Copy (Card Eater) workspace.

## Bug 1 — external hard drive auto-detected as a camera card

Plugging in an ordinary external hard drive made it show up as an
active "card" in the Copy workspace, same as a real memory card would.

**Root cause**: `cardeater_card.looks_like_camera_card`'s fallback for
cards without a DCIM/PRIVATE layout (`_has_media_files`, added for
cards that write clips straight into the root or under a crew-labeled
folder like "A001") was existential -- "does *any* file within two
levels of the root look like camera media" -- rather than checking
whether the volume actually *is* a card. Any general-purpose external
drive with so much as one video or photo file anywhere shallow in its
tree (a Movies folder, an old export, a Photos library) tripped it, and
`cardeater_volume_watcher._run_once` auto-activates the first
`looks_like_camera_card`-passing volume it sees on every mount with no
user confirmation step.

**Fix**: replaced `_has_media_files` with `_looks_like_all_media` --
same two-level shallow scan, but now a universal check (every non-junk
entry at each level must itself be camera media or an all-media
subfolder) instead of an existential one. A real card's fallback case
(clips in root, or all under one or more camera-labeled folders) is
still homogeneous camera content and still passes; a drive that mixes
in any unrelated top-level folder or file no longer does. `DCIM`/
`PRIVATE`-layout detection (the common case) is untouched.

Two new regression tests in `tests/test_cardeater_card.py` cover the
reported case directly (media file alongside an unrelated `Documents`
folder, and a camera-labeled folder alongside an unrelated `Backups`
folder) -- both now correctly return `False`; all five pre-existing
`looks_like_camera_card` tests still pass unchanged.

## Bug 2 — click/shift-click didn't highlight a range like Finder, and checkboxes couldn't act on a highlight

Two rounds of feedback on this one, converged on a two-tier model:

- Clicking a file row, or shift-clicking to range-select, didn't
  visibly do anything unless it landed precisely on that row's small
  checkbox (the original report).
- An initial fix made plain/shift clicks on the row itself toggle the
  checkbox directly -- but that's not what Finder does, and the user
  corrected it: a row click should only highlight/preview, never check
  a box by itself.
- The actual desired model (this fix): click and shift-click highlight
  a row or a contiguous range of rows, Finder-style, entirely separate
  from which files are checked for the copy job; a checkbox click then
  acts on the current highlight -- if it lands on a file that's part of
  a multi-row highlight, it checks or unchecks *every* highlighted file
  together (matching the direction that one checkbox just moved in),
  otherwise it's a plain single-file toggle.

**Implementation** (`frontend/suite.js`):

- New state: `ce.highlightedPaths` (the Finder-style row highlight,
  separate from `ce.selectedPaths` = checked-for-copy) and
  `ce.selectAnchorPath` (shift-click anchor, replacing the old
  `lastCheckedPath`, only moved by a plain click -- same fixed-anchor
  convention Finder itself uses, so repeated shift-clicks grow/shrink
  the highlighted range relative to one fixed start point instead of
  drifting).
- `ceHighlightOnly(path)`: plain click on a row (not its checkbox) sets
  the highlight to just that row and moves the anchor there; still
  focuses/previews via the existing toggle-on-second-click `ceFocusFile`.
- `ceHighlightRange(toPath)`: shift-click on a row (anywhere on it, not
  just its checkbox) replaces the highlight with the contiguous run
  between the anchor and this row (using the existing
  `ceVisibleOrderedPaths` flattened, filter/collapse-aware ordering) --
  a *replace*, not additive, matching Finder's own shift-click.
- `ceApplyCheckboxToHighlight(path)`: the checkbox's native `"change"`
  listener now calls this instead of a plain `ceToggleFileSelection`.
  When `path` is part of a highlight with more than one member, every
  highlighted file is set to match the direction this checkbox just
  moved (checked if it was previously unchecked, unchecked if it was
  previously checked) -- read from `ce.selectedPaths`'s *pre*-click
  state, which is still in sync with the rendered checkboxes at the
  moment the listener runs. Otherwise it falls back to the old
  single-file toggle.
- New CSS class `.suite-ce-file.is-highlighted` (`--teal`/`--teal-dim`,
  deliberately distinct from the existing amber `.is-focused`) so a
  highlighted range and the one row currently focused in the viewer
  read as visually separate states -- both can apply to the same row
  (the last-clicked one in a range), and `.is-focused` wins the
  background/border since it's declared later in `suite.css`.

## Bug 3 — auto-populate all files/types, but start deselected

`ceActivateCard` previously auto-selected either every file on the card,
or (for multi-cam cards) just the "A0"-prefixed camera folder, the
moment a card finished scanning -- easy to not notice and copy more (or
less) than intended.

**Fix**: on scan complete, the file list and every group still populate
immediately and fully expanded (unchanged), and the per-extension filter
buttons still cover every type found (`ceRenderExtFilters`, unchanged --
it was already filter-only, not a pre-selection), but `selectedPaths`
now always starts empty. The existing "Select All"/"Select None"
buttons and per-file/per-group checkboxes are the only way files get
selected for a copy job now.

## Verification

`main.py --selftest`: 273/273, zero regressions (includes the two new
`test_cardeater_card.py` cases above). `node --check frontend/suite.js`
confirms no syntax errors in the rewritten click/change handlers. Not
verified against the real double-clicked app window (no way to drive a
live pywebview UI from this environment) -- relaunch and manually
confirm: insert/attach a real card still auto-activates correctly, a
mixed-content external drive no longer does, click and shift-click
highlight rows (teal) without touching any checkbox, and checking one
box within a multi-row highlight checks (or unchecks) every highlighted
file together.
anywhere in this document as of this addendum.
