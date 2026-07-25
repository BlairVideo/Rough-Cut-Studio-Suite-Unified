# Handoff: Rough Cut Studio

This document is for whoever takes over technical ownership of this app —
not for end users (that's `README.md`, which is thorough and should stay
the first stop for anything about how the app behaves or its known
limitations). This doc is about what a new maintainer needs before
changing code: what's load-bearing, what's genuinely untested, what's
fragile, and what to do next.

If you're an editor at Blair just trying to *use* the app, stop here and
go read `README.md` instead.

---

## 1. What this is, in one paragraph

A local desktop app (pywebview) that takes timecoded interview
transcripts, sends a creative brief to an LLM — Google's Gemini API or a
locally-running Llama model via Ollama, switchable at any point in the
session — and gets back a proposed cut — which you can then edit by hand
(reorder, retime, add B-roll, add ducking) — and export as a Markdown
script, Premiere Pro XML, Final Cut Pro XML, or OpenTimelineIO, or render
straight to an MP4 preview. With the Gemini provider, the only network
call is that API request; with the Llama provider, the only "network"
call is to Ollama on the local machine, so nothing leaves it. No file
uploads, no telemetry, no other network access either way. It's a
companion project to the separate Interview Transcription App
(mlx-whisper + pyannote), which is out of scope for this handoff.

## 2. Current state

Feature-complete for its intended scope, built and hardened iteratively
over one continuous engagement (no formal sprints/tickets — see §7 on
testing for what that means practically). Everything described in
`README.md` is implemented and was tested at build time. Nothing is a
stub or a "coming soon."

**Sizeable, load-bearing pieces**, roughly in the order they'd matter to
a new maintainer:

| Area | File(s) | Notes |
|---|---|---|
| Bridge to frontend | `backend/api.py` (~1,900 lines) | Everything routes through this one `Api` class exposed as `js_api`. It's the biggest file by a wide margin and the first place to look for almost anything. Provider dispatch (`_call_llm`) branches to Gemini or Llama but everything downstream (`_resolve_segments`, `_finalize_outputs`) is provider-agnostic. |
| Source/transcript management | `backend/sources.py` | A `SourceManager` class, extracted from `api.py`, owning `sources`/`media_paths`/`fps`/`drop_frame`/the thumbnail cache. `Api` exposes those same names as `@property` proxies — see §3. |
| Frontend logic | `frontend/app.js` (~2,400 lines) | No framework — vanilla JS, DOM built via template strings, delegated event listeners. |
| LLM providers | `backend/gemini_client.py`, `backend/llama_client.py` | Both expose the same `generate_script(...)` shape so `api.py` can call either interchangeably. Gemini gets schema-enforced JSON from the API itself; Llama gets Ollama's JSON *mode* (valid JSON, not a validated shape) plus the shape spelled out in the prompt — `_resolve_segments` in `api.py` is what actually guards against a malformed response either way, not the client module. |
| Premiere export | `backend/xml_builder.py` | Hand-built XMEML v5. Forced stereo (two linked mono tracks), B-roll on V2, optional B-roll audio on A3/A4. |
| Final Cut export | `backend/fcpxml_builder.py` | Hand-built FCPXML v1.11. B-roll as anchored connected clips (lane=1), parent-relative offsets. |
| OTIO export | `backend/otio_builder.py` | Hand-built OTIO JSON. Greedy lane assignment for overlapping B-roll (see §5). |
| Transcript parsing | `backend/transcript_parser.py` | SRT/VTT/bracket/single-timecode formats; drop-frame timecode math. |
| Video preview export | `backend/video_export.py` | ffmpeg concat pipeline, main track only. |

## 3. Design decisions a new maintainer should know before changing things

These aren't obvious from reading the code cold, and getting them wrong
while "cleaning up" is the most likely way to introduce a regression.

- **No XML/JSON library dependencies for the three timeline exports.**
  XMEML, FCPXML, and OTIO are all hand-built via `xml.etree.ElementTree`
  / plain `dict` + `json`, not via `opentimelineio` or similar. This was
  deliberate — no extra dependency for the person running the app, one
  less thing that can be missing at runtime. If you add a real OTIO
  dependency later, know that you're trading a stdlib-only build for a
  pip dependency; that's a legitimate call, just don't do it by accident.
- **Audio is always forced to true stereo in XMEML** (`xml_builder.py`).
  A single audio clipitem with `channelcount=2` and no channel routing
  imports as mono in Premiere — this was an actual bug hit during
  development. The fix (two linked mono tracks pulling channels 1/2 via
  `<sourcetrack>`) is not optional cleanup; removing it silently breaks
  stereo audio on import.
- **B-roll "Duck Main" reduces the whole overlapping main clip's volume,
  not just the overlap window.** This was a deliberate scope decision,
  not a shortcut taken under time pressure — frame-precise fade
  automation needs a keyframe timing base for FCPXML that wasn't
  confidently verified, and a wrong automation curve is worse than an
  honest, simple whole-clip duck. If someone wants frame-precise ducking
  later, that's real work, not a quick fix (see §8).
- **OTIO tracks are strictly sequential** (no absolute-position concept,
  unlike XMEML/FCPXML). B-roll placement uses `Gap` items to push clips
  to the right spot, and overlapping B-roll gets spread across
  additional tracks (V2, V3, ...) via a greedy "minimum meeting rooms"
  assignment in `otio_builder.py`. This was validated against the real
  `opentimelineio` reference library during development (see §7) — if
  you touch this function, re-validate against the real library, not
  just by eyeballing the JSON.
- **`_cid` (client-side id) on frontend cut objects.** The backend never
  returns a stable id per cut, so the frontend assigns one
  (`app.js: newCid()`) the moment a cut enters `state.editSegments`, and
  carries it through edits via object spread. This is what makes the
  preview player's "now playing" row highlight, Set In/Out, and the
  preview's editorial-note sync survive reordering/deletion. If you
  refactor `renderEditTable` or `readEditTableIntoState`, make sure
  `_cid` still gets preserved (spread `...original` first, don't
  overwrite it).
- **`[hidden] { display: none !important; }`** near the top of
  `style.css` is not decorative — several components
  (`.modal-overlay`, `.block--output`, `.preview-player`,
  `.bulk-actions`) set their own `display: flex`, which is an author
  rule at equal specificity to the browser's default `[hidden]` rule and
  therefore wins over it without this override. This was a real bug
  (the transcript modal appearing on launch) before the fix. If a new
  hidden-by-default element is added later without this global rule in
  mind, check that it actually stays hidden.
- **Autosave and Save Project are two entirely separate mechanisms** that
  happen to share a serialization format via `_build_project_dict()` /
  `_apply_loaded_project()` in `api.py`. Autosave writes to a single
  fixed path (`tempfile.gettempdir()/rough_cut_studio_autosave.json`),
  silently, no dialog, overwritten constantly. Save Project writes to a
  user-chosen path. Don't merge these into one code path beyond the
  shared dict-building/applying helpers — their trigger conditions and
  failure handling are intentionally different (autosave must *never*
  raise or interrupt the action that triggered it; Save Project should
  surface errors to the user).
- **API keys never touch a project file or the autosave snapshot.**
  `_build_project_dict()` and `_last_meta` deliberately never include
  `api_key`. If you add new fields to what gets persisted, keep this
  invariant — check any new field against this before it ships.
- **`_generation_lock` must guard every method that mutates
  `self.last_result` / `self.history` / `self.sources` / `self._last_meta`,
  not just `generate()`/`revise()`/`rebuild_outputs()`.** This was missed
  for `new_project()`, `load_project()`/`restore_autosave()` (both funnel
  through `_apply_loaded_project()`), `restore_history_entry()`, and
  `load_sequence()` — pywebview dispatches each `js_api` method on its own
  worker thread, so e.g. "New Project" racing an in-flight `generate()`
  could have interleaved writes to the same state. Fixed by wrapping each
  in the same non-blocking `acquire()`/`try`/`finally` pattern `generate()`
  already used. Guard any new state-mutating method the same way.
- **`_finalize_outputs()` requires at least one "main"-track cut before
  calling the FCPXML/OTIO builders.** `build_fcpxml`/`build_otio`
  deliberately raise `ValueError` if `main_list` is empty (there's no
  sane sequence without a main track), but nothing downstream used to
  catch it — marking every cut as B-roll and clicking Apply Changes
  crashed with an uncaught exception instead of the `{"ok": False, ...}`
  shape every other failure path returns. Fixed with an early check in
  `_finalize_outputs()` itself, since every caller (`generate`, `revise`,
  `rebuild_outputs`, `restore_history_entry`, `load_sequence`) funnels
  through it.
- **`preview_server.py` must parse the suffix Range form (`bytes=-500`,
  meaning "last 500 bytes"), not just `start-end`/`start-`.** It didn't —
  a suffix request was silently misread as `bytes=0-500`. Also,
  `remove_source()` now calls `preview_server.forget(path)` so a removed
  source's media stops being servable via its old token instead of
  staying reachable for the rest of the process's life, and `shutdown()`
  now calls `server_close()` so the listening socket is actually released
  instead of left open.
- **All three timeline exporters should warn when B-roll overlap forces
  it onto an extra track/lane, not just FCPXML and OTIO.**
  `build_premiere_xml()` did the identical greedy lane-spreading silently.
  It now returns `(xml_string, warnings)` like the other two builders —
  see `_finalize_outputs()` for how the three warning lists get combined.
  Give a fourth export format the same treatment if one is ever added.
- **Two `app.js` handlers were pushing an undo snapshot *before* syncing
  in-progress DOM edits into `state.editSegments`** — the transcript
  modal's "+ Add" button and `setInOutFromPlayhead()` (Set In/Set Out) —
  unlike every other mutating handler, which could silently discard an
  edit typed into another row. Fixed by calling `readEditTableIntoState()`
  first, matching the rest of the file. This ordering is still just a
  per-handler convention, not structurally enforced — watch for it in any
  new mutating handler.
- **Cuts-tab thumbnails are cached client-side** (`state.thumbnailCache`,
  keyed by `source_id::media_path::in_seconds`) so re-rendering the table
  after an unrelated change (reorder, track toggle, etc.) doesn't refetch
  every row's thumbnail over the pywebview bridge and cause visible
  flicker. `media_path` is part of the key specifically so relinking a
  source invalidates its old cached frame instead of showing a stale one.
- **The transcript modal is a real dialog, not just `hidden` toggling** —
  `role="dialog"`/`aria-modal` in `index.html`, plus initial focus on
  open, focus restored to the opener on close, and a Tab-key focus trap
  in `app.js` (`openTranscriptModal()`/`closeTranscriptModal()`). Follow
  the same pattern for any future modal rather than a bare
  `.hidden = true/false`.
- **Several `js_api` call sites had no error handling** (`addTranscripts`,
  `linkMedia`, `removeSource`, `viewTranscript`, `batchRelink`, the
  sequence save/load/delete handlers, `btnSaveScript`/`btnSaveExport`) —
  if the Python side threw instead of returning `{"ok": False, ...}`, the
  promise rejected with no visible feedback. All now wrap the call in
  try/catch and call `setStatus("Unexpected error: " + err, "error")`,
  matching the pattern already used by the history-restore handler. Do
  the same for any new `js_api` call site.
- **The Cuts table (`editTableBody`) uses targeted DOM surgery for
  single-row edits, not a full rebuild.** `buildRowElement(seg, i, total)`
  builds one `<tr>`; `renderEditTable()` (full rebuild) is reserved for
  operations that legitimately touch many rows at once — undo/redo, bulk
  delete/set-track, applying a fresh generation/revision result. Every
  single-row operation instead does direct DOM surgery and then calls
  `renumberRows()` (fixes `data-idx` and the ▲/▼ boundary-disabled state):
  delete → `tr.remove()`; move up/down/drag → `moveRow()`/node
  repositioning; track/audio_mode toggle → `replaceRowElement()` (rebuilds
  just that one row); add/duplicate → `appendCutRow()` or an inline
  `insertAdjacentElement`. This exists because a full rebuild on a
  100–500-row cut list is both janky and destroys every row's DOM node
  (losing focus, thumbnails, etc.) for edits that only actually changed
  one row. If you add a new single-row mutation, follow this pattern
  rather than reaching for `renderEditTable()` out of convenience.
- **`moveRow(tr, direction)` moves the *other* row, never `tr` itself.**
  This looks backwards but is deliberate: moving `tr`'s own DOM node to a
  position *before* an earlier sibling via `insertBefore(tr, prev)`
  measurably blurs a focused descendant in this app's target webview,
  even though `tr` stays connected throughout. Repositioning the
  untouched sibling around the stationary `tr` instead reliably preserves
  focus in both directions — this is what makes the Alt+Up/Alt+Down
  keyboard reorder shortcut usable for rapid repeated moves. If you touch
  this function, re-verify focus survives a move in both directions;
  don't "simplify" it back to moving `tr` directly.
- **Thumbnail loads go through a concurrency-limited queue
  (`enqueueThumbnail`/`pumpThumbnailQueue`, `THUMBNAIL_CONCURRENCY = 4`),
  not a direct call per row.** Each thumbnail load is a `js_api` call that
  the backend serves by shelling out to `ffmpeg` synchronously
  (`backend/thumbnails.py`) — rendering a 100–500-row cut list without a
  cap would fire that many concurrent ffmpeg processes at once. Every
  code path that wants a row's thumbnail loaded must go through
  `enqueueThumbnail()`, never call `loadRowThumbnail()` directly.
- **Timecode-field edits (`in_tc`/`out_tc`/`timeline_start_tc`) now get
  their own undo step; other free-text fields (note/on-screen-text) still
  don't.** This narrows the old "undo/redo is structural changes only"
  rule. The mechanism (`tcEditSnapshot`, captured on `focusin` of a
  `.tc-input` and pushed to the undo stack on the native `change` event
  at commit/blur) exists because a text input has no "before" value
  available at commit time — the DOM already holds the new value by then
  — so the snapshot has to be taken earlier, at focus. Free-text fields
  deliberately keep relying on the browser's own native undo instead,
  since incremental typing doesn't have a single meaningful "before"
  moment the way a full-value timecode replacement does.
- **The Cuts-tab source/track filter (`state.cutsFilter`,
  `rowMatchesFilter`/`applyRowFilter`) is purely visual** — it only
  toggles a row's `hidden` property and never reorders, removes, or
  renumbers `state.editSegments` or a row's `data-idx`. This means
  ▲/▼-boundary and reorder logic still operate on true array position,
  not visual position, under an active filter — a filtered-out row at
  true index 0 still blocks the first *visible* row from moving further
  up. That's an accepted trade-off of keeping the filter simple, not a
  bug to "fix" by making index logic filter-aware.
- **Inline B-roll overlap warnings (`refreshBrollOverlapWarnings`) use an
  intentionally approximate, non-drop-frame timecode parser
  (`approxTcToSeconds`), not the real conversion.** They can't trust
  `timeline_start_seconds`/`in_seconds`/`out_seconds` either, since those
  are only recomputed by the backend on Apply and go stale the moment
  someone edits a timecode field by hand without applying. This is a
  live "heads up, these probably overlap" hint only — the backend's
  actual drop-frame-aware conversion remains the sole authority for lane
  placement on export. Don't upgrade the client-side parser to replicate
  drop-frame semantics; it doesn't need to be exact, just close enough to
  flag overlaps as they're created. If you add a new place that mutates a
  B-roll cut's track/timing, call `refreshBrollOverlapWarnings()` there
  too — it's idempotent (clears a stale flag as readily as it sets a new
  one), but only for rows it's actually told to re-check.
- **`#selectAllRows` and `updateBulkUI()`'s header-checkbox tri-state are
  scoped to currently-*visible* rows only, not the full
  `state.editSegments` array.** Without this, checking "Select All" under
  an active Source/Track filter would silently select every row in the
  whole list — including ones hidden by the filter that the user never
  looked at — and a subsequent Bulk Delete would then delete far more
  than intended. This was a real data-loss bug, not a hypothetical. The
  "`N` selected" count and `btnBulkDelete`/`btnBulkSetTrack` themselves
  are deliberately *not* scoped to visibility — a row selected while
  visible and then hidden by a later filter change keeps counting as
  selected rather than silently losing its `_selected` flag. Don't merge
  these two scopes; they're intentionally different.
- **`tcEditSnapshot` (the pre-edit snapshot behind timecode-field undo)
  must be flushed via `flushTcEditSnapshot()` before any *other* handler
  pushes its own undo snapshot while a `.tc-input` could still be
  focused with an uncommitted edit.** This is exactly the Alt+Up/Alt+Down
  keyboard-reorder case (type a new value, reorder without blurring
  first) and the Set In/Set Out case (`setInOutFromPlayhead`, which
  writes a *different* row's timecode via script while an unrelated
  field is mid-edit) — both call `flushTcEditSnapshot()` first for
  exactly this reason. Without it, the stale snapshot lands on the undo
  stack *after* the newer one once the field is eventually blurred,
  corrupting the stack's chronological order (an Undo jumps back further
  than expected, skipping an intermediate state). If you add a new
  handler that pushes an undo snapshot and could plausibly run while a
  timecode field is focused but not yet blurred, call
  `flushTcEditSnapshot()` first, same as these two.
- **`backend/sources.py`'s `SourceManager` owns `sources`/`media_paths`/
  `fps`/`drop_frame`/the thumbnail cache; `Api` exposes those same names
  as `@property` get/set pairs proxying to it.** This is what lets
  `generate()`, `_resolve_segments()`, `_finalize_outputs()`,
  `_apply_loaded_project_unsafe()`, `_build_project_dict()`, and
  `new_project()` keep reading/writing `self.sources` etc. completely
  unchanged even though the real data now lives on `self._sources_mgr`.
  `SourceManager` holds a reference to the owning `Api` instance (not a
  frozen `window` argument) because `main.py` assigns `api.window` only
  *after* `webview.create_window(...)` returns, well after
  `Api.__init__`/`SourceManager.__init__` have already run — `window`
  and `preview_server` are lazy properties on `SourceManager` reading
  `self._api.window`/`self._api.preview_server` for exactly this reason.
  If you add a new field to this state, add the property pair on `Api`
  too, or external code that reads `self.<field>` will break.
- **Loading a project file (or an autosave snapshot — same code path)
  now rejects a source's `path`/`media_path` if it isn't a real file
  with an allow-listed extension** (`_is_allowed_transcript_path`/
  `_is_allowed_media_path` in `api.py`, matching the same extensions the
  Add Transcript / Link Media dialogs already restrict to). A
  `.rcstudio.json` is just hand-editable JSON — without this check, a
  crafted one could point `path` at an arbitrary file (e.g. `~/.ssh/
  id_rsa`), have it "parsed" as a lenient transcript, and have its
  contents sent to an LLM provider as a normal-looking source once the
  user clicks Generate. A rejected `path` is skipped with a note in the
  result (same shape as the pre-existing "file doesn't exist" case); a
  rejected `media_path` doesn't fail the whole source, just skips linking
  it (same as "no media linked" elsewhere). Don't loosen this to accept
  arbitrary extensions "just in case" — it exists specifically because
  this input is untrusted in a way a user-driven dialog pick is not.

## 4. Dependencies and their risk profile

- **pywebview** — renders through a different native engine per OS
  (WKWebView / WebView2 / WebKitGTK). This has already caused one real,
  shipped bug (a CSS auto-layout table quirk that only manifested on one
  engine). Any layout work should be treated as "verify on more than one
  platform if possible," not "looks right in one browser, done."
- **ffmpeg** — required for thumbnails and video preview export;
  optional for everything else. Detected via `shutil.which`. If it's
  missing, both features degrade with a clear error rather than
  crashing — verify that still holds if you touch `thumbnails.py` or
  `video_export.py`.
- **Gemini API** — one of two provider options, and the only one requiring
  a network call to a third party. Model names (`gemini-flash-latest`,
  `gemini-3.1-pro-preview`, etc.) are hardcoded in `frontend/index.html`'s
  Gemini model selector and referenced in `gemini_client.py`'s
  `DEFAULT_MODEL`. Google can and does change/retire model names; if
  Gemini generation starts failing across the board, check that first
  before assuming an app bug.
- **Ollama** — the other provider option, entirely local. It's an external
  application the person installs themselves (not a pip package, not in
  `requirements.txt`) that exposes a REST API on `localhost:11434` by
  default. `llama_client.py` just makes plain HTTP calls to it via
  `requests`, same as the Gemini client. Unlike Gemini, there's no fixed
  model list to keep in sync — the frontend calls `list_ollama_models`
  (backed by Ollama's `/api/tags`) to show whatever the person has already
  pulled, rather than hardcoding names that assume a specific model is
  installed. If Ollama changes its API shape in a future version, that's
  the first place to check.
- **`opentimelineio` (PyPI)** — **not a runtime dependency.** It was
  installed only in the development sandbox to validate `otio_builder.py`
  against the real reference implementation (see §7). Don't add it to
  `requirements.txt`; the app doesn't need it to run.

## 5. Known limitations (the ones that matter most)

`README.md`'s "Notes & limitations" section is exhaustive and accurate —
read it. The handful most likely to generate a support request or a
"why doesn't it..." question:

1. **B-roll audio ducking is whole-clip, not frame-precise** (§3).
2. **Video preview export is main-track only** — no B-roll compositing.
   Same reasoning as above: real compositing is a different, larger
   problem than concatenation.
3. **Fast (keyframe-nearest) seeking in video preview export** — cuts can
   land up to a fraction of a second off. Fine for judging pacing, not
   for delivery.
4. **Timecodes are treated as non-drop-frame internally**; drop-frame is
   a *display* toggle only (`;` separator), not a different internal
   frame-counting model. This is standard practice but worth knowing if
   someone reports "the frame count looks different from my NLE."
5. **No installer.** Running the app requires a terminal, `pip install
   -r requirements.txt`, and `python main.py`. This is the single
   biggest adoption barrier for a non-technical editor. See §8.

## 6. Security / privacy posture

- No file is ever uploaded anywhere. With the Gemini provider selected,
  the only outbound network call is the Gemini API request itself (HTTPS,
  to `generativelanguage.googleapis.com`). With the Llama provider
  selected, the only call is to a local Ollama server (`localhost:11434`
  by default, or another host the person explicitly types in) — nothing
  leaves the machine in that mode.
- The Gemini API key is memory-only by default. If the person opts in to
  "Remember," it's written to a git-ignored `.env` file — never to a
  project file, never to the autosave snapshot (§3). The Llama provider
  has no key at all, so there's nothing to protect there.
- The local preview HTTP server (`preview_server.py`) binds to
  `127.0.0.1` only, uses opaque random tokens (never exposes raw file
  paths), and serves only media paths the app itself has registered.
- Project files (`.rcstudio.json`) store file *paths*, not file
  *contents* — except History and Sequences snapshots, which do embed
  full script/XML/FCPXML/OTIO text (that's why project files can grow to
  a few hundred KB with heavy History use; still plain local JSON).
- **A project file (or autosave snapshot) is untrusted input** — it's
  hand-editable JSON that could have come from anywhere, unlike a file
  path picked through a live dialog. Loading one only accepts a source's
  `path`/`media_path` if it's a real file with an allow-listed transcript
  or video extension (see §3); a path pointing at an arbitrary file (e.g.
  something outside any project's normal scope) is rejected rather than
  read, closing a path where a crafted project file could otherwise get
  an arbitrary local file's contents loaded as a "transcript" and sent to
  an LLM provider on the next Generate.

## 7. Testing approach — read this before assuming there's a test suite

**There is no automated test suite (no `pytest`, no CI) as of this
handoff.** Every feature in this app was validated during development
via targeted, throwaway Python scripts run directly against the `Api`
class and the builder modules — real assertions, real XML/JSON parsing,
real `ffmpeg` execution against synthetic test video files, and for
OTIO specifically, validation against the actual `opentimelineio`
reference library (installed only in the dev sandbox, not shipped). This
is meaningfully more rigorous than "looks right," but it is not the same
as a maintained, repeatable test suite living in the repo.

**If you're taking this over long-term, the single highest-leverage next
step is standing up a real `pytest` suite** covering:
- `transcript_parser.py`'s format auto-detection and timecode math
  (including drop-frame round-trips — there's a known-good 1,300+ sample
  sweep that was run ad hoc during development; that's a natural seed
  for a real parametrized test).
- `xml_builder.py` / `fcpxml_builder.py` / `otio_builder.py` against
  representative segment lists, checking clip counts, durations, and
  (for XMEML) stereo track structure.
- `api.py`'s `rebuild_outputs`, history, sequences, and autosave/restore
  round-trips — these were tested via realistic multi-step scripts during
  development (generate → edit → save → reload in a fresh `Api`
  instance → verify) and translate almost directly into test cases.

There is no frontend test coverage at all (no Jest/Playwright/etc.) —
`app.js` was validated by syntax-checking (`node --check`) plus manual
reasoning about DOM state, not automated interaction tests. This is the
weakest-covered part of the codebase.

The round of fixes described in §3 (generation-lock coverage, the
all-B-roll crash, the preview-server Range/token issues, XMEML warning
parity, and the `app.js` undo-ordering/thumbnail-cache/modal-accessibility/
error-handling fixes) was validated the same ad hoc way — throwaway
scripts calling `Api` methods directly for the backend changes, and a
live browser session against a static file server with `window.pywebview.api`
stubbed out for the `app.js` changes (there's no way to load pywebview's
real bridge outside the native app shell). None of this was captured as a
permanent test file, so the `pytest` suite recommendation below still
stands — this validation doesn't repeat itself the next time someone
touches this code.

A later performance/UX pass (the Cuts-table incremental-render rewrite,
the thumbnail concurrency queue, duplicate-row, Alt+Up/Down reorder,
timecode-field undo, the source/track filter + stats, and the inline
B-roll overlap warnings — all described in §3) was implemented as four
sequential changes, each verified independently against a live browser
session the same way, followed by one more integration pass exercising
all four together (duplicate → reorder → timecode edit → delete → undo,
checked against both DOM state and `state.editSegments`) to catch
interaction bugs a single feature's own tests wouldn't surface. Same
caveat as above: none of it is a repeatable test file.

Two bugs surfaced after that round shipped — `#selectAllRows` sweeping in
filtered-out rows, and `tcEditSnapshot` corrupting undo ordering when
combined with keyboard reorder or Set In/Out — both described in §3, both
fixed and re-verified the same ad hoc way. A follow-up round then
extracted `backend/sources.py` from `api.py`, closed the project-file
path-validation gap described in §3/§6, and fixed the accessibility
findings described in the app's UI — the backend two were verified with
throwaway scripts exercising the property-proxy boundary and a simulated
malicious project file directly, and the accessibility fixes were
verified in a live browser via `preview_snapshot`'s computed-accessible-
name output, not just checking that an attribute was present.

## 8. Suggested next steps, roughly in priority order

1. **Stand up the pytest suite described in §7.** Nothing else on this
   list matters if a future change can silently break stereo audio
   export or drop-frame timecodes without anyone noticing.
2. **Package a real installer** (PyInstaller or py2app for macOS, given
   the Blair branding is macOS/Apple-Silicon-targeted). This is the
   clearest remaining adoption barrier — right now, using the app at all
   requires comfort with a terminal.
3. **Frame-precise B-roll ducking**, if it turns out to matter in
   practice — requires nailing down FCPXML's keyframe timing base with
   real confidence (via Apple's docs or, ideally, a round-trip test
   against Final Cut itself) before touching `xml_builder.py` /
   `fcpxml_builder.py`.
4. **B-roll compositing in video preview export**, if the fast rough-cut
   preview turns out not to be enough on its own — this is a real
   video-compositing feature, not an extension of the current
   concatenation pipeline.

None of these are required for the app to keep working as-is — they're
ordered by where future effort would do the most good, not by urgency.

## 9. File inventory

```
video-script-studio/
├── main.py                    opens the native app window
├── backend/
│   ├── api.py                 1,886 lines — the bridge; almost everything routes through here
│   ├── llama_client.py          415 lines — local Ollama chat-API call, no key/cloud
│   ├── transcript_parser.py     390 lines — format parsing, timecode math
│   ├── xml_builder.py           371 lines — Premiere XMEML export
│   ├── sources.py                357 lines — SourceManager: transcripts, media links, fps/drop-frame, thumbnail cache
│   ├── fcpxml_builder.py        322 lines — Final Cut FCPXML export
│   ├── gemini_client.py         262 lines — Gemini API call, retry/backoff
│   ├── otio_builder.py          211 lines — OpenTimelineIO export
│   ├── preview_server.py        155 lines — local HTTP server for in-app preview
│   ├── video_export.py           99 lines — ffmpeg concat pipeline
│   ├── script_writer.py          82 lines — Markdown script generation
│   └── thumbnails.py             73 lines — ffmpeg-based storyboard frame extraction
├── frontend/
│   ├── app.js                 2,400 lines — all frontend logic, no framework
│   ├── style.css               1,095 lines
│   └── index.html                345 lines
├── README.md                  user-facing docs — usage, formats, exhaustive limitations list
├── HANDOFF.md                  this file
├── requirements.txt
├── .env.example
└── .gitignore
```

## 10. Where to actually start reading the code

In order:
1. `README.md`, top to bottom — understand what the app does before
   reading how.
2. `backend/api.py`'s `__init__` and `generate()` — the core happy path.
3. `backend/api.py`'s `_finalize_outputs()` — the one function almost
   every action (generate, rebuild, revise, restore, load) funnels
   through; understanding it explains most of the rest of the backend.
4. `frontend/app.js`'s `state` object declaration at the top, then
   `renderEditTable()` — the Cuts tab is the most complex piece of UI
   and most other frontend logic exists to serve it.
