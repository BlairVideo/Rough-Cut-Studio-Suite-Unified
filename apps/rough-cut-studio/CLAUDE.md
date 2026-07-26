# Project Context & Guidelines

## 1. Project Overview
* **Name:** Rough Cut Studio
* **Description:** A local desktop app for video editors. Takes one or more
  timecoded interview transcripts plus a creative brief, asks an LLM
  (Gemini or a local Llama model via Ollama) which transcript segments to
  cut together, and produces a readable Markdown script plus Premiere Pro
  XML (XMEML), Final Cut Pro XML (FCPXML), and OpenTimelineIO exports of
  the same sequence. Cuts can be hand-edited (reorder, retime, add B-roll)
  and revised in plain language without starting over.
* **Current Status:** Feature-complete for its intended scope (see
  `HANDOFF.md` §2). A round of bug fixes (generation-lock coverage, an
  all-B-roll crash, preview-server Range/token handling, XMEML warning
  parity, and several `app.js` fixes) landed after the initial build,
  followed by a performance/UX pass on the Cuts tab (incremental table
  rendering, a thumbnail concurrency queue, duplicate-row and Alt+Up/Down
  reorder, timecode-field undo, a source/track filter with aggregate
  stats, and inline B-roll overlap warnings), two more bug fixes found
  after that round shipped (`#selectAllRows` ignoring the active filter;
  `tcEditSnapshot` corrupting undo ordering), and a further pass that
  extracted `backend/sources.py` from `api.py`, closed a project-file
  path-validation gap, and fixed a set of accessibility findings across
  the UI — see `HANDOFF.md` §3 for what changed and why. A `pytest`
  suite (`tests/`) now covers `transcript_parser.py` and the three
  export builders (`xml_builder.py`/`fcpxml_builder.py`/
  `otio_builder.py`) — see `HANDOFF.md` §7/§8 for what's covered and
  what's still ad hoc (`api.py`'s round-trip behavior is the top
  remaining gap; a packaged installer is second).
* **Docs to read first:** `README.md` (user-facing behavior, exhaustive
  limitations list) and `HANDOFF.md` (maintainer-facing design decisions,
  known fragile spots, security posture). Both are accurate and detailed —
  read them before making non-trivial changes here.

## 2. Tech Stack & Environment
* **Language:** Python 3.9+
* **App shell:** [pywebview](https://pywebview.flowrl.com/) — opens as a
  native desktop window (WKWebView/WebView2/WebKitGTK depending on OS), not
  a browser tab and not Tkinter.
* **Frontend:** Vanilla JavaScript, no framework — `frontend/index.html`,
  `frontend/app.js`, `frontend/style.css`. DOM is built via template
  strings with delegated event listeners.
* **AI integration:** Two interchangeable providers, both exposing the same
  `generate_script(...)` shape:
  * **Gemini** — Google's free-tier Gemini Developer API, called directly
    over HTTPS via `requests` (no SDK). Schema-enforced JSON output.
  * **Llama (local, via [Ollama](https://ollama.com))** — no API key, no
    internet; talks to Ollama's REST API on `localhost:11434` by default.
* **Key libraries:** `pywebview`, `requests` (see `requirements.txt` — kept
  intentionally minimal). The three timeline exports (XMEML, FCPXML, OTIO)
  are hand-built with `xml.etree.ElementTree` / stdlib `json`, deliberately
  with **no** `opentimelineio` or XML-library runtime dependency.

## 3. How to Run & Test
* **Setup:** `python -m venv .venv && source .venv/bin/activate` (Windows:
  `.venv\Scripts\activate`)
* **Install:** `pip install -r requirements.txt`
* **Run:** `python main.py` — opens the app's own window immediately.
* **ffmpeg** (not a pip dependency) is required on PATH for storyboard
  thumbnails and video preview export; everything else works without it.
* **Testing:** `uv run pytest apps/rough-cut-studio` (or `uv run pytest`
  from the repo root for the full workspace suite) covers
  `transcript_parser.py` and the three export builders — see
  `HANDOFF.md` §7 for what's in scope and what isn't yet (`api.py`'s
  round-trip behavior). Also validate changes manually against the flows
  described in `README.md`, and check `node --check frontend/app.js`
  after JS edits.

## 4. Code Style & Architectural Rules
* **Backend bridge:** `backend/api.py` is the single `Api` class exposed to
  the frontend as `js_api` — nearly everything routes through it. Keep
  provider-specific logic isolated to `_call_llm`; everything downstream
  (`_resolve_segments`, `_finalize_outputs`) must stay provider-agnostic.
* **`SourceManager` (`backend/sources.py`) owns `sources`/`media_paths`/
  `fps`/`drop_frame`/the thumbnail cache**, extracted from `Api` because
  that state has little coupling to the generation pipeline. `Api`
  exposes those same names as `@property` get/set pairs proxying to
  `self._sources_mgr` — this is what lets the rest of `api.py` keep
  reading/writing `self.sources` etc. unchanged. If you add a field to
  this state, add the property pair on `Api` too, or external code
  reading `self.<field>` breaks. `SourceManager` takes the owning `Api`
  instance, not a `window` argument — `main.py` assigns `api.window`
  after construction, so `window`/`preview_server` are lazy properties.
* **Threading:** pywebview calls every `js_api` method on a worker thread,
  not the GUI thread — never touch Tkinter or similar main-thread-only
  toolkits here. `generate()`/`revise()`/`rebuild_outputs()` are guarded by
  `self._generation_lock` since they mutate shared state
  (`self.last_result`, `self.history`, `self._last_meta`); keep any new
  method that mutates that same state behind the same lock.
* **Validation:** every segment an LLM chooses is re-validated against the
  real parsed transcript in `_resolve_segments` before it can affect any
  output — never trust a model's indices/timecodes directly.
* **Secrets:** the Gemini API key must never be logged, never included in
  an exception message, never written to a project file or the autosave
  snapshot, and only sent via request headers (never a URL query string,
  which can leak into `requests`/`urllib3` exception text).
* **Stereo audio (XMEML):** always built as two linked mono clipitems
  (L/R), never a single `channelcount=2` clip — the latter silently
  imports as mono in Premiere. See `xml_builder.py`.
* **B-roll overlap handling:** overlapping B-roll clips must be spread
  across separate tracks/lanes (XMEML tracks, FCPXML lanes, OTIO tracks) —
  never left to collide on a single track/lane. All three exporters use
  the same greedy interval-scheduling ("minimum meeting rooms") approach.
* **Frontend `_cid`:** every cut object in `state.editSegments` carries a
  client-assigned `_cid` for row identity across re-renders (preview
  highlight, Set In/Out, live note sync). Always spread `{...original}`
  when copying/updating a cut object rather than rebuilding it field by
  field, or `_cid` (and any other forward-compatible field) gets silently
  dropped.
* **Undo/redo:** snapshot `state.editSegments` via
  `pushUndoSnapshot()`/an equivalent *after* syncing any pending DOM edits
  into state (`readEditTableIntoState()`), not before — otherwise a stale
  snapshot can discard in-progress edits elsewhere in the table. Timecode
  fields (`in_tc`/`out_tc`/`timeline_start_tc`) are the one exception to
  "structural changes only": they get their own undo step via a
  pre-edit snapshot captured on `focusin` (see `tcEditSnapshot`), since a
  text input has no "before" value left to snapshot by the time its
  `change` event fires. Any *other* handler that pushes its own undo
  snapshot while a `.tc-input` could still be focused with an
  uncommitted edit (Alt+Up/Down reorder, `setInOutFromPlayhead`) must
  call `flushTcEditSnapshot()` first — otherwise the stale snapshot lands
  on the stack *after* the newer one once the field is eventually
  blurred, corrupting chronological order.
* **Cuts-table single-row edits use DOM surgery, not a full rebuild:**
  `renderEditTable()` (full rebuild) is only for operations that touch
  many rows at once (undo/redo, bulk actions, applying a fresh
  generation/revision result). A single-row change should go through
  `buildRowElement()` + `renumberRows()`/`replaceRowElement()`/
  `appendCutRow()`/`moveRow()` instead — a full rebuild on a large cut
  list is both janky and destroys every row's DOM node (losing focus,
  requiring every thumbnail to reload) for an edit that only touched one
  row. `moveRow()` specifically repositions the *other* row via
  `insertBefore`, never the moved row's own node — moving `tr` itself
  measurably blurs a focused descendant in this app's target webview.
* **Thumbnail loads must go through `enqueueThumbnail()`**, never call
  `loadRowThumbnail()` directly — it's a concurrency-limited queue
  (`THUMBNAIL_CONCURRENCY`) guarding against a large cut list firing
  hundreds of concurrent `ffmpeg`-backed `js_api` calls at once.
* **File access:** only ever through native Open/Save dialogs or
  already-linked media paths — never read/write arbitrary paths. A
  loaded project file/autosave snapshot is untrusted input despite
  coming in through a dialog itself: `_is_allowed_transcript_path`/
  `_is_allowed_media_path` in `api.py` restrict a source's `path`/
  `media_path` to the same extensions the Add Transcript/Link Media
  dialogs already allow, requiring a real file — don't loosen this to
  accept arbitrary paths from a project file.
* **Error handling:** public `Api` methods should return
  `{"ok": False, "error": ...}` on failure rather than letting an
  exception escape to the frontend; wrap file I/O and JSON parsing
  accordingly, especially anything touching a user-supplied or autosave
  file that could be malformed.

## 5. Immediate Goals & Next Steps
See `HANDOFF.md` §8 for the maintained priority list. In short:
- [x] Stand up a real `pytest` suite for transcript parsing/timecode math
      and the XML/FCPXML/OTIO builders — done, see `tests/`.
- [ ] Extend that suite to `api.py` round-trips (generate/edit/save/
      reload) — still ad hoc, see `HANDOFF.md` §7/§8.
- [ ] Package a real installer (PyInstaller/py2app) — running the app
      currently requires a terminal.
- [ ] Frame-precise B-roll audio ducking, if it turns out to matter.
- [ ] B-roll compositing in the video preview export, if the main-track-only
      rough preview isn't enough on its own.
