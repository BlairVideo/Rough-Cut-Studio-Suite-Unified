# Rough Cut Studio Suite

One native desktop app (no browser) that unifies the four production tools in
this folder:

| Workspace  | Powered by                  | What it does |
|------------|-----------------------------|--------------|
| Sync       | A-Sync                      | Lines up camera video with separate audio recordings (waveform cross-correlation or embedded timecode), proxy-free |
| Transcribe | Local Interview Transcriber | Batch mlx-whisper transcription + optional pyannote speaker diarization, fully local |
| B-Roll     | B-Roll Analyzer             | Scores a folder of clips, finds best segments, optional CLIP "high energy" detection |
| Graphics   | Blair Brander               | Brand-compliant title cards / lower thirds with live preview and alpha-channel export |
| Edit       | Rough Cut Studio            | The full Rough Cut Studio app, unchanged — LLM rough cuts (Gemini or local Ollama), Cuts table, XMEML/FCPXML/OTIO export |

## Launch

```bash
./launch_studio_suite.sh
```

First run creates `.venv` and installs the (tiny) requirements; after that it
starts instantly. The four original apps still run standalone exactly as
before — nothing in them was modified.

## How the pieces talk

- **Transcribe → Edit**: after a transcription finishes, "Send to Edit" writes
  a WebVTT (with a `NOTE Source video:` header) into `assets/transcripts/` and
  registers it as an Edit source with the media file already linked. The
  standard `<video>.ivt-cache.json` is also written next to the video, so the
  standalone transcriber sees the same result.
- **B-Roll → Edit**: analyze a folder, tick the segments you want, "Send
  Selected to Edit" — each clip becomes a media-linked source and the chosen
  segments land in the Cuts table as B-Roll (V2) rows. The analyzer's own
  `.broll_analyzer_cache.json` is shared both ways with the standalone app.
  "Export Premiere XML…" still produces the analyzer's ranked-bin XML directly.
- **Graphics → Edit**: "Send to Edit" renders a transparent qtrle `.mov` into
  `assets/graphics/` and drops it into the Cuts table as a B-Roll row.

## Audio-video sync (Sync workspace)

- Pick a camera video and any number of external audio recordings (field
  recorder, lavs, boom); "Detect Sync" computes each file's offset by
  waveform cross-correlation (or embedded BWF/camera timecode). Offsets are
  nudgeable to the millisecond and persist next to the video — in a
  `<video>.sync-offsets.json` sidecar if the video hasn't been transcribed
  yet, or folded directly into its `<video>.ivt-cache.json` once it has
  (so a video that's both synced and transcribed gets exactly one sidecar
  file, not two — see "One sidecar per video" below).
- **Preview Sync**: play the picture with the external audio locked to it at
  the current (nudged) offset — the browser mixes the original files live
  over the local preview server, so nothing is rendered to disk. Per-track
  Mute/Solo let you check one mic at a time.
- **Track selection / routing**: each audio file has an Enabled toggle and a
  channel selector (all channels / a single channel / downmix to mono). The
  choice is saved in the sidecar and honored everywhere — the Sync-workspace
  XML, the Edit-workspace export splice, and transcription (a single
  selected channel is transcribed directly). Disabled tracks are excluded
  from preview, export, and transcription.
- **Transcribe this track (no proxy)**: transcribes the external audio file
  directly — nothing is rendered or merged — and shifts every segment's
  timestamps by the sync offset so the transcript lands on the *video's*
  timeline. The cache is written next to the video, so the transcript
  editor, Send to Edit, and the standalone Transcriber all see it aligned.
- **Export Premiere XML…**: an XMEML sequence where the video and every
  audio recording appear as **separate, non-merged clips** referencing the
  original files — one clipitem per audio channel, external audio placed at
  its sync offset (negative offsets trim the head), optional camera-audio
  tracks. No proxy media is generated at any point.
- **One sidecar per video**: syncing and transcribing the same video no
  longer leaves two separate files behind. Whichever happens second folds
  its data into whatever the first one already wrote — sync-then-transcribe
  merges the sidecar into `.ivt-cache.json` (deleting the sidecar);
  transcribe-then-sync writes routing straight into the existing
  `.ivt-cache.json` (no sidecar ever created). A video that's only synced,
  or only transcribed, still gets just the one file it needs either way.

## Edit-workspace layout polish

Cosmetic refinements to the embedded Rough Cut Studio UI (applied entirely
from the suite's own stylesheet/script — Rough Cut Studio's own files are
never modified):
- **Generate Script** sits to the right of the prompt box, and the
  **Runtime** readout sits to the right of **Target duration** — both were
  stacked in one column before.
- **Script, Cuts, Export, and History** get more vertical room: shrinking
  the Creative Brief section's own footprint hands that space straight to
  the tabs below (they already stretch to fill whatever's left).
- **Transcripts are searchable**: opening a transcript now shows a search
  box above the segment list — type to filter by speaker or text, with a
  live "N / total" count. Clears automatically each time a transcript is
  (re)opened.

## Favorites (Edit workspace)

- **Favorite a transcript line**: open any source's transcript (the "view"
  button in the Sources list), and a small star sits next to each line's
  "+ Add" button — click it to favorite/unfavorite that line. Favorites
  persist in `assets/favorites.json` and survive across launches, even for
  a transcript that isn't currently loaded.
- **Favorites tab**: a fifth tab next to Script/Cuts/Export/History lists
  every favorited line (source, timecode, speaker, text) with an
  **+ Add to Cuts** button — that pushes it onto the main track (V1) of the
  Cuts table, re-linking its source automatically if it isn't loaded this
  session — and a star to remove it from Favorites.
- **Favorite star on every Cuts row**: click the star at the end of any
  Cuts row to favorite/unfavorite it directly — works even for a manually
  added or edited cut with no matching transcript line, not just ones sent
  over from a transcript.
- **Favorite star in the preview window**: the same star sits in the
  preview player's header, favoriting whichever cut is currently loaded
  there — all three surfaces (transcript modal, Cuts row, preview window,
  Favorites tab) always agree.
- **Narrower "B-Roll Audio" column**: the Cuts table's Audio column takes
  less horizontal room, handing the extra space to Script Text.

## Editing tools (feature round 2)

- **Transcript editor** (Transcribe): edit segment text, reassign a segment's
  speaker, rename speakers (labels), merge one speaker into another, and
  include/exclude (isolate) speakers. Edits persist to the standard
  `<video>.ivt-cache.json`, so the standalone Transcriber sees them too, and
  "Send to Edit" always uses the saved state (excluded speakers are dropped,
  display names substituted). "Open existing transcription…" reloads any
  previously transcribed video from its cache without re-transcribing.
  A "Show Video" toggle opens a reference player next to the segment list —
  each segment has a small ▶ that jumps the video to that moment — so you
  can watch/listen while editing text or reassigning speakers.
- **Transcripts link both audio and video, durably**: if a video has ever
  been synced against an external audio recording (Sync workspace), every
  "Send to Edit" — from either workspace — embeds that association directly
  in the exported transcript (a `NOTE Source audio: ...` line), not just in
  a sidecar file next to the video. So even if the transcript is later
  reopened on its own, without that sidecar present, Rough Cut Studio's
  Premiere XML export still includes the original audio as its own linked,
  non-merged track alongside the video — the link travels with the file.
- **Segment preview** (B-Roll): every segment chip plays its exact time range
  right in the clip's thumbnail spot (served over the local preview server;
  loops the segment) — the video replaces the thumbnail in place rather than
  opening a separate player, so the card never grows or shifts.
- **Graphics animation timeline**: draggable in/out bars for Title, Subtitle,
  and Logo (with outro bars when an outro animation is set), frame-snapped,
  plus a scrubber and Play button — the same control the standalone Brander's
  timeline gives you.
- **Logo tools** (Graphics): placement select (7 positions), "Import Logo…"
  for your own PNG/JPEG (white backgrounds auto-keyed; imports persist in
  `assets/logos/`), and a larger size range (up to 640 px; lower-third scale
  to 2.0×).
- **AI titles — Local or Gemini** (Graphics): the prompt bar has two modes.
  *Local* is the original offline keyword interpreter. *Gemini* sends your
  prompt plus the current scene to the Gemini API for more dynamic
  suggestions — every returned field is validated against the brand's allowed
  fonts/colors/layouts before it touches the scene. Graphics has its own
  dedicated Gemini API key (system keychain, separate from the Edit
  workspace's key) — a status row under the prompt bar shows whether one's
  saved, with a "change"/"set" link to update it; the key is header-only,
  never logged.
- **Edit-workspace timeline**: a collapsible timeline panel under the Rough
  Cut Studio UI showing the main track and stacked B-roll lanes; drag a
  B-roll block to retime it (frame-snapped, undoable through Rough Cut
  Studio's own undo).
- **Undo/redo throughout**: ⌘Z / ⇧⌘Z work in every workspace — Graphics scene
  edits, transcript edits, B-roll selections — and the Edit workspace keeps
  Rough Cut Studio's native undo untouched.

## Simultaneous processing

Every heavy task is a background job (jobs button, top right): transcription
runs in the Transcriber's own venv/process, clip analysis in the Analyzer's
venv with its own worker pool, graphics exports on suite threads — all at the
same time, while the Edit workspace stays interactive. Transcription defaults
to one video at a time (Metal/MPS contention; raise the "parallel" stepper to
allow more at once).

## Privacy / network

Identical posture to the original apps: transcription, clip analysis, and
graphics never leave the machine (one-time model-weight downloads aside).
The only cloud calls are the Edit workspace's existing Gemini option and the
Graphics prompt bar's opt-in Gemini mode (its Local mode stays fully offline) —
both use the same key handling as standalone Rough Cut Studio (`.env` /
in-memory), and local Ollama remains available as the offline alternative for
Edit generation.

## Maintainer notes

`CONTRACT.md` documents the backend/frontend architecture: `SuiteApi`
subclasses Rough Cut Studio's `Api`, the window page is re-composed from
Rough Cut Studio's untouched frontend at every launch, and each sibling app is
reached via its own venv interpreter (`backend/workers/`) or an in-process
bridge (`backend/brander_bridge.py`).

The four/five sibling apps' own files are otherwise never modified — one
explicit, user-approved exception: Blair Brander's `renderer.py` was patched
directly to fix two real rendering bugs (a `"wipe"` outro that only animated
out the Title, leaving Subtitle/Logo/the divider fully visible; a Lower-Third
logo-placement bug that snapped `"-center"` placements to `"-right"`) — both
bugs also affect the standalone Blair Brander app, not just this suite. See
`CONTRACT.md` addendum v8.
