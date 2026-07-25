# Rough Cut Studio

A desktop app for video editors: upload timecoded transcripts, describe the
cut you want, and get back a readable **script** plus **Premiere Pro XML**,
**Final Cut Pro XML**, and **OpenTimelineIO** exports of the same sequence.
Ask for a revision in plain language and the model adjusts the existing cut
instead of starting over.

Two LLM providers are supported, switchable at any point from the sidebar —
even between a Generate and a later Revise:

- **Gemini** — Google's cloud API. Needs a free API key and an internet
  connection.
- **Llama (local, via [Ollama](https://ollama.com))** — runs entirely on
  your machine. No API key, no internet connection, nothing leaves this
  computer. Requires installing Ollama and pulling a model yourself first
  (e.g. `ollama pull llama3.1`).

```
[You] -> transcripts + prompt (+ optional target duration)
   |
[Gemini API, or local Llama via Ollama] -> picks segments + order, JSON only
   |
[This app] -> validates every choice against the real transcript,
              converts timecodes -> frames
   |
[Script (.md) + Premiere Pro XML + Final Cut Pro XML + OpenTimelineIO]
   |
[Premiere / Final Cut / Resolve / pipeline tools] -> Import -> cuts appear
```

It opens as its own window (via [pywebview](https://pywebview.flowrl.com/)),
not a browser tab. With the Gemini provider selected, the only outside
network call is to Google's free-tier Gemini API over HTTPS; with Llama
selected, the app instead talks to Ollama on your own machine (by default
`http://localhost:11434`) and nothing leaves it. Ctrl/Cmd+S saves your
project at any time, and the app keeps a background recovery snapshot so a
crash or accidental quit doesn't lose work you never explicitly saved.

## Requirements

- Python 3.9+
- For the **Gemini** provider: a free API key from
  https://aistudio.google.com/app/apikey (Google's Gemini Developer API has
  a free tier; check current limits on Google's pricing page since they can
  change)
- For the **Llama** provider: [Ollama](https://ollama.com) installed and
  running locally, with at least one model pulled (e.g.
  `ollama pull llama3.1`). No API key needed. Generation quality and speed
  depend entirely on the model you pull and your hardware.
- `ffmpeg` on your system PATH, for storyboard thumbnails on the Cuts tab
  (e.g. `brew install ffmpeg` on macOS). Everything else works fine without
  it — you'll just see placeholder thumbnails instead of real frames.

## Setup

```bash
cd video-script-studio
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip3 install -r requirements.txt
python3 main.py
```

The app window opens immediately — no browser required.

## Using it

1. **Add Transcript(s)** — pick one or more `.srt`, `.vtt`, or plain-text
   timecoded transcript files. The app auto-detects the format and splits
   each into indexed, timecoded segments. Click **view** on any source at
   any time (before or after generating anything) to see every parsed
   segment — index, in/out timecode, speaker, text — in a read-only
   viewer, handy for confirming a transcript parsed the way you expected.
2. **Media auto-links when possible.** If a transcript embeds its source
   video path (the `# Source video:` header your transcription app writes
   into `.txt` exports, or the equivalent WebVTT `NOTE` block), or a video
   file with the same name sits next to the transcript, it's linked
   automatically — no manual step needed. Otherwise, click "link media…"
   for that source and pick the file yourself, or click **Batch Relink…**
   to point at a whole folder once — it searches recursively and links
   every still-unlinked source whose filename matches, without touching
   anything already linked.
3. Set the **sequence name** and **frame rate** to match your project. At
   29.97 or 59.94 fps a **drop-frame timecode** checkbox appears — turn it
   on if you want displayed timecodes to match your camera's clock. This
   only changes how timecodes are *labeled*; the actual cuts and frame
   math are identical either way.
4. Under **LLM Provider**, pick **Gemini** (paste your API key, get one
   free at the link above) or **Llama** (point it at your local Ollama
   install and pick a pulled model — click **Refresh** to list what
   you've already pulled). You can change either the provider or the
   model at any point in the session, including mid-way through, without
   losing your current cut.
5. Write a **creative brief**, and optionally a **target duration**
   (e.g. `60`, `90s`, or `1:30`) — the model treats it as real guidance
   rather than padding or cutting the story short to hit it exactly.
6. Click **Generate Script**. The selected model chooses which transcript
   lines to use and in what order; the app double-checks every choice
   against your real transcripts before building anything (see "How this
   stays safe" below). A duration meter shows your actual runtime against
   the target, if set.
7. **Review and edit the cut list** on the **Cuts** tab: a small storyboard
   thumbnail shows what each cut's in-point looks like (needs `ffmpeg` —
   see Requirements) — **click a thumbnail to play that exact cut** in the
   preview player, from its in-point and pausing automatically at its
   out-point. The player shows the source, in/out points, and track for
   whatever's loaded, and **whichever row is currently playing is
   highlighted in the table** so you always know where you are — including
   while **Preview Script** plays every main cut back-to-back in order,
   where the highlight follows along cut by cut. Scrub to the frame you
   want and click **Set In** / **Set Out** to write the playhead position
   straight into that cut's timecodes, instead of typing them by hand. The
   preview player also has its own **editable editorial note** field right
   under the video — type there and it updates the same cut's note in the
   table live, no need to scroll over and find the row. The player has its
   own close button (✕) so it doesn't stay taking up space once you're
   done with it.

   **Reorder** clips by dragging the ⠿ handle on the left of each row,
   with the ▲▼ buttons, or by focusing anywhere in a row and pressing
   **Alt+↑/Alt+↓** — the handle exists because every other part of the
   row is a text field, dropdown, or button, none of which let the
   browser start a native drag. **Select multiple rows** with the
   checkboxes to delete or reassign Track in bulk — a small toolbar
   appears above the table once anything's checked. Above that, a
   **Source/Track filter and a running stats line** (cut count and total
   duration per source) help you navigate a long cut list — the filter
   only hides rows visually, it never reorders or renumbers the
   underlying list. **Ctrl/Cmd+Z undoes, Ctrl/Cmd+Shift+Z (or Ctrl+Y)
   redoes** the last structural change to the list (reorder, delete,
   duplicate, add, track change, bulk action) *or* a committed timecode
   retime, or use the Undo/Redo buttons directly — both stay out of the
   way while focus is inside a free-text field (note, on-screen text),
   where your browser's own undo takes over instead. Retime an in/out
   point, tweak an editorial note or on-screen text, **duplicate a cut**
   (⧉, next to delete — handy for splitting a segment or reusing it as
   B-roll), delete a cut, or add one manually — you can also click
   **+ Add** next to any line in a transcript's viewer (via **View** on
   the Sources list) to drop that exact segment onto the bottom of the
   cut list. To lay
   B-roll over the main track's audio instead of cutting to it, set a
   cut's **Track** to "B-Roll (V2)" and give it a **Timeline Start** —
   it'll sit silently on a second video track at that point rather than
   advancing the main sequence. Its **B-Roll Audio** setting controls what
   happens to sound: **Silent** (default) plays picture only; **Full
   Volume** lets the overlay's own audio play too, on its own track;
   **Duck Main** does the same but also turns down the main track's
   volume (a dB amount you set, default -12) for the whole duration of
   whichever main clip(s) the overlay touches — see the Notes section
   below for exactly what that last one does and doesn't do. Click
   **Apply Changes** to rebuild the script and XML from your edits, no
   model call needed — repeat as many times as you like.
8. **Not quite right? Ask for a revision** instead of starting over — type
   something like "make the opening punchier" or "cut the section about
   budget" into the box at the top of the Cuts tab and click **Revise**.
   It sees your current cut as a starting point and returns a full
   revised version; any B-roll you've placed manually is carried over
   untouched, since the model never manages B-roll. Revise uses whichever
   provider/model is currently selected — it doesn't have to be the one
   you generated with.
9. Check the **Script** and **Export** tabs — the Export tab has a small
   switcher for **Premiere Pro XML**, **Final Cut Pro XML**, and **OTIO**,
   sharing one preview pane and one **Save…** button that follows whichever
   format is selected. All
   three timeline exports are generated from the same cut list, so pick
   whichever matches where you're headed — Premiere and DaVinci Resolve
   read the Premiere XML directly; older Final Cut and Motion read
   `.fcpxml`; and the `.otio` file is the one to reach for DaVinci Resolve
   via its native OTIO import, Avid (via an adapter), or any pipeline
   tooling built around OpenTimelineIO rather than a specific NLE. On the
   **Script** tab, **Export Video Preview…** renders an actual `.mp4` of
   the current cut — the main track only (no B-roll composited in; see
   Notes below) — using `ffmpeg` to trim and concatenate the real source
   clips, re-encoded so mismatched resolutions or frame rates between
   sources don't matter. It's something you can hand to someone or watch
   on its own, not just inside the app; longer cuts can take a little
   while to render.
10. Import into your editor: **File > Import…** in Premiere Pro (`.xml`),
    drag the `.fcpxml` into a Final Cut Pro event, or import the `.otio`
    wherever OpenTimelineIO is supported. A new sequence appears with your
    cuts already on the timeline in order.

**Every generation, edit, and revision is kept on the History tab** for
this session — label, timestamp, cut count, and runtime for each one.
Click **Restore** on any entry to make it the active version again for
further editing or export; restoring doesn't erase anything; it just adds
a new entry on top, so you can always work your way back. Check the
**compare** box on two entries and click **Compare Selected** to see
their main cut lists side by side — matched cuts, additions, removals,
and changes are all called out, the same idea as a text diff.

**Working on more than one version of the cut?** Give the sidebar
**Sequences** panel a name and click **Save Current** to keep it as its
own snapshot — e.g. a "Social Cut" and a "Broadcast Cut" from the same
sources, both saved in the same project file. **load** swaps it in as the
active cut (and adds a History entry, like everything else that changes
the active cut); **remove** deletes the saved sequence, not your current
work.

**Saving your work:** click **Save Project…**, or press **Ctrl/Cmd+S**, any
time to write a JSON file capturing your sources, media links, prompt,
LLM provider/model choice, target duration, frame rate/drop-frame setting,
current cut list (including any B-roll placements), your saved
**Sequences**, and your full **History** for this session. The first save
(or a **Load Project…**) asks where to put it; after that, Ctrl/Cmd+S saves
straight back to that same file with no dialog — a real "quick save," not a
repeated prompt. **Load Project…** restores all of it (re-parsing the
transcripts from their saved paths), so picking a project back up later
gives you the same History list and saved Sequences you had when you
saved — including the ability to Restore an older iteration you'd already
moved past. Loading always adds one new "Loaded from project" entry on top
of the restored history, so it's clear where the session picked back up.

**Starting fresh:** click **+ New Project** to clear everything — sources,
cut list, history, saved sequences — back to a blank session, as if the app
had just launched. It asks for confirmation first if there's anything
unsaved, and doesn't touch your API key or provider/model choice, since
those are treated as session preferences rather than part of any one
project.

**Crash recovery happens automatically, with no setup.** The app silently
keeps a recovery snapshot of your session — separate from any project file
you've explicitly saved — that updates itself after every generate, edit,
revision, or restore, and again periodically in the background even for
edits on the Cuts tab you haven't clicked Apply Changes on yet. If the app
closes unexpectedly, the next launch shows a banner offering to restore
that snapshot or discard it; restoring works exactly like opening a
project file, just from a spot you never had to choose yourself.

## Supported transcript formats

- **SRT**: standard `HH:MM:SS,mmm --> HH:MM:SS,mmm` blocks.
- **WebVTT**: `HH:MM:SS.mmm --> HH:MM:SS.mmm` blocks.
- **Bracket/arrow**: `[00:00:01:00 - 00:00:04:12] SPEAKER: text` per line
  (frame-based timecodes after the last colon are supported too).
- **Single-timecode-per-line**: `00:12:34 Jane: text…` — the end time for
  each line is inferred from the start of the next line.

## How this stays safe

- **No hallucinated timecodes.** The model (Gemini or Llama) is only ever
  asked to choose which already-parsed segment to use (by source + index)
  and how much to trim off each end, never to invent a raw timecode. The
  app re-validates every reference against your real transcripts before
  generating anything — an out-of-range or malformed model response is
  dropped with a warning, not silently trusted. The same validation applies
  to your manual edits on the Cuts tab and to revision responses: an
  unreadable timecode, an out point before the in point, or a reference to
  a segment that doesn't exist is rejected with a clear message rather than
  silently corrupting the sequence.
- **Structured output, no scraping.** The Gemini call sets
  `responseMimeType` to `application/json` with an explicit schema, so
  there's no free-form text to parse with fragile string matching. The
  Llama/Ollama call uses Ollama's JSON mode plus an explicit shape
  described in the prompt — Ollama doesn't enforce a schema the way Gemini
  does, so the parsed result goes through the exact same re-validation
  described above regardless of which provider produced it.
- **Your API key stays local.** It's only kept in memory for the running
  session unless you explicitly tick "Remember key on this machine," which
  writes it in plain text to a local `.env` file next to the app (not
  committed to git — see `.gitignore`). Treat that file like a password.
  Project files never contain your API key. The Llama provider needs no
  key at all.
- **File access is dialog-only.** The app never reads or writes files you
  haven't explicitly picked through a native Open/Save dialog. Auto-linking
  media only ever checks for a file that already exists on disk (an
  embedded path in the transcript, or a same-named sibling file) — it
  never creates, moves, or modifies anything. This extends to project
  files: a saved `.rcstudio.json` remembers the transcript and media paths
  you linked, but re-loading it still only accepts paths ending in a
  recognized transcript (`.srt`/`.vtt`/`.txt`) or video extension, and
  requires a real file, not a folder — so a hand-edited or otherwise
  untrustworthy project file can't be used to point the app at an
  arbitrary file on your machine (a source with a rejected path is simply
  skipped, with a note explaining why, rather than silently loaded).
- **Outbound network calls depend on your provider choice.** With Gemini
  selected, the app calls `generativelanguage.googleapis.com` over HTTPS.
  With Llama selected, the app instead calls a local Ollama server (by
  default `http://localhost:11434`, or another address you explicitly set)
  — nothing about your transcripts or brief leaves the machine. Either way
  the app also loads Google Fonts once for UI type (it still works
  offline, just with system fonts).
- **Thumbnails are read-only and local.** Extracting a storyboard frame
  runs `ffmpeg` against your already-linked media file and pipes a single
  frame into memory — nothing is written to disk, no new files are
  created, and it never touches the network.
- **Video preview stays on your machine.** Clicking a thumbnail to preview
  a cut starts a tiny local HTTP server bound only to `127.0.0.1` — never
  reachable from your network, even on the same LAN. It serves exactly the
  media files you've already linked, addressed by a random per-session
  token rather than a real path, so there's no way to request an arbitrary
  file from it even in principle. It only starts the first time you click
  a thumbnail, and shuts down automatically when you close the app.

## Notes & limitations

- **Timecode display defaults to non-drop-frame**, with an optional
  drop-frame toggle at 29.97/59.94 fps (see "Using it" above). This is
  purely a display convention — the same underlying frame count either
  way — so it never affects which frames actually end up in the cut, only
  how their timecodes are labeled in the Cuts tab, script, and semicolon
  (`;`) vs colon (`:`) separator you see.
- Two XML formats are generated from the same cut list every time, so
  they never drift apart:
  - **Premiere Pro XML** uses **XMEML v5** (Final Cut Pro XML Interchange
    Format — the older, track-based interchange format Premiere itself
    exports). Also readable by DaVinci Resolve.
  - **Final Cut Pro XML** uses **FCPXML v1.11**, the modern resource-based
    format current Final Cut Pro reads natively. Structurally different
    from XMEML: instead of V1/V2 tracks, B-roll is represented as a
    "connected clip" anchored to whichever main clip it overlaps in time
    (Final Cut's native model for overlays). If a B-roll clip sits before
    your first cut or after your last one, it's anchored to that nearest
    edge clip instead and flagged in the warnings — check its placement
    after import in that case.
- Every cut in the **Premiere XML** gets a **video clip (V1)** plus a
  **left-channel audio clip (A1) and a right-channel audio clip (A2)**,
  all three referencing the same source file and linked together the way
  Premiere's own XML export links a stereo pair — so trimming or nudging
  one keeps picture and both channels in sync. Audio is always written as
  true **stereo (2 linked mono tracks, one per channel)** — this is
  hardcoded, not a setting, so a generated sequence can't quietly collapse
  to mono. (A single audio clip with a "channelcount: 2" tag and no
  channel routing looks reasonable but Premiere actually imports it as
  mono — this app builds the L/R pair that Premiere's own exports use
  instead.) The **Final Cut Pro XML** doesn't need this workaround — a
  single `asset-clip` there already carries its full stereo audio natively.
- Clip names in the script and XML always come from the **linked media
  file** (e.g. `interview_A.mp4`), never from the transcript file — a
  transcript is just an editing aid, not the media being cut. If you
  haven't linked a source yet, the app names it `<source_id>.mp4` as a
  placeholder so it's obvious what to relink in Premiere.
- If you don't link a media file for a source, the XML still contains
  valid in/out points and structure — Premiere will ask you to relocate
  the media the first time you open the sequence.
- **Revising a cut** sends the selected model your current main cut plus
  the change you describe, and asks for a complete replacement rather than
  a diff — so a revision can restructure more than you expected if the
  instruction is broad ("redo this entirely" will do exactly that).
  B-roll overlays are never sent to or touched by revision; they're
  carried over from whatever was on the Cuts tab before you clicked
  Revise.
- **B-roll overlays** live on a second video track (V2) and are silent by
  default — no audio clip is created for them, so they never compete with
  the main track's sound. If a shot needs its own audio, pull it in
  manually in Premiere. B-roll placement is a manual Cuts-tab decision;
  the model only ever proposes the main sequential cut, not B-roll timing.
- **If two B-roll clips overlap in time**, every export format (Premiere
  XML, Final Cut XML, and OTIO) automatically spreads the later one onto
  an additional track or lane (V2, V3, ...) instead of letting them
  collide, and the Warnings panel flags it so you know to double-check
  that clip's placement after import. The Cuts tab also flags an overlap
  live, right on the conflicting cuts' Timeline Start field, as soon as
  you create or edit one — you don't have to click Apply Changes first
  to find out. This live check is a quick heads-up, not the authoritative
  math the exporters use on Apply, so treat a cleared warning as "looks
  fine" rather than a guarantee down to the frame.
- **Every cut list needs at least one cut on the Main track.** If you mark
  every cut as B-roll on the Cuts tab and click Apply Changes, the app
  won't try to build a sequence with no main track — you'll get a clear
  error asking you to keep at least one cut on Main instead of a broken
  export.
- **Target duration** is guidance, not a hard constraint — Gemini is asked
  to get close without padding or gutting the story to hit it exactly, and
  the duration meter shows how far off the actual result landed so you can
  trim (or add) cuts on the Cuts tab and reapply.
- **Storyboard thumbnails** need `ffmpeg` on your PATH and a linked media
  file; without either, you'll see a placeholder instead of a frame, and
  everything else still works normally. Thumbnails are cached both in the
  backend (so reordering or editing cuts doesn't re-extract frames you've
  already seen) and in the Cuts tab itself, so an unrelated table change
  (reordering a different row, toggling a track) won't even briefly flash
  the placeholder back in for a thumbnail you've already loaded. On a long
  cut list, new thumbnails load in small batches rather than all at once —
  a deliberate limit so opening a 100+ row cut list doesn't launch that
  many `ffmpeg` processes simultaneously; everything still fills in within
  a moment, just progressively rather than instantly.
- **Video preview** needs a linked media file for that source (same
  requirement as thumbnails, minus the `ffmpeg` dependency — playback uses
  your browser engine's own video decoding). Previewing one cut plays from
  its in-point and pauses at its out-point; **Preview Script** chains every
  main cut this way automatically, switching source files between cuts as
  needed. It only plays the **main track** — B-roll overlays aren't shown,
  since a single video player genuinely can't composite two video streams
  at once the way a real NLE timeline does; you'll only see B-roll once
  you import the XML into Premiere or Final Cut Pro.
- **Undo/redo (Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z or Ctrl+Y) covers structural
  changes to the cut list, plus committed timecode retimes** —
  reordering, deleting, duplicating, adding, changing a cut's track,
  bulk actions, and a finished edit to an In/Out/Timeline Start field —
  capped at the 20 most recent each way. Making a new change after
  undoing discards whatever was available to redo, same as any standard
  editor. Both intentionally stay out of the way of your browser's native
  undo inside a focused **free-text** field (note, on-screen text), so
  typing and undo-while-typing there both behave the way you'd expect —
  timecode fields are the one exception, since a retime is normally a
  full replacement value rather than incremental typing, so there's no
  useful native-undo trail to preserve the way there is for prose.
  Undo/redo is also unrelated to **History**: it works on
  edits you haven't applied yet, while History remembers versions you
  *have* applied (via Generate, Apply Changes, or Revise). Generating,
  applying, revising, or restoring a version clears both stacks, since at
  that point there's a new baseline worth keeping rather than partial
  edits worth stepping back through.
- **History is capped at the 30 most recent entries**, in memory and in
  the project file alike — older ones are dropped quietly to keep both
  bounded during a long session. Restoring an old entry adds a new
  "Restored" entry rather than deleting anything after it, so history only
  ever grows forward, the same way undo/redo wouldn't let you lose a
  branch of edits. **Saving and loading a project now carries history with
  it** (see "Saving your work" above) — this is the one thing that keeps
  project files from being uniformly small: each history entry stores a
  full script/XML/FCPXML snapshot, so a project with many iterations can
  run to a few hundred KB. Still plain JSON, still entirely local.
- **History comparison** matches cuts by content (source + in/out + note),
  not position, using the same technique behind most text diffs — a cut
  that just moved shows as unchanged rather than a spurious remove-and-add
  pair. It only diffs the **main track**; B-roll is summarized as a count
  on each side rather than diffed cut-by-cut, to keep the view readable.
- **B-roll audio: "Duck Main" reduces the whole main clip's volume, not
  just the overlapping window.** If a B-roll clip only covers part of a
  main clip's duration, that entire main clip still gets the flat
  reduction for its full length, not just the seconds under the overlay.
  This is a deliberate simplification — frame-precise fade automation
  around just the overlap would need keyframe timing bases this app isn't
  confident are correct across both XMEML and FCPXML, and a wrong
  automation curve is worse than a simple, honest whole-clip duck you can
  refine by hand in your NLE. If more than one B-roll clip ducks the same
  main clip, the strongest reduction wins. "Full Volume" B-roll gets its
  own linked stereo audio (A3/A4 in the Premiere XML; native in FCPXML) —
  only "Silent" (the default) has no audio at all.
- **Batch Relink** matches by exact filename (not fuzzy matching) and
  never touches a source that's already linked, so it's safe to run
  repeatedly as you organize footage into new folders. It's bounded to
  20,000 scanned files so a huge or deeply nested drive can't hang the
  app; if your footage lives past that, relink the rest individually or
  point it at a narrower subfolder.
- **If adding transcripts, linking or removing media, Batch Relink, or
  saving/loading a named sequence fails unexpectedly** (a rare local I/O
  problem, not a normal "you cancelled the dialog" outcome), you'll see an
  "Unexpected error: ..." status message rather than the action silently
  doing nothing — a real failure is never mistaken for "nothing happened,
  try again."
- **Named Sequences** are explicit, user-named save points — "Social
  Cut", "Broadcast Cut" — that sit alongside History rather than
  replacing it: History is automatic and chronological, Sequences are
  deliberate and named. Loading a sequence adds a History entry too
  (like any other action that changes the active cut), so you're never
  missing an audit trail either way. Deleting a sequence only removes
  that saved snapshot — it has no effect on whatever cut is currently
  active, even if you loaded it from that same sequence.
- **The transcript viewer** shows exactly what the app parsed, not the
  raw file — useful for spotting a timecode format that didn't parse the
  way you expected. It has no editing capability by design: fix the
  source transcript and re-add it if something's wrong, rather than
  patching parsed data that would just drift from the file on disk.
- **Video preview export renders the MAIN TRACK only** — B-roll isn't
  composited in, matching the in-app "Preview Script" player's scope for
  the same reason: correctly compositing an overlay (position, size,
  timing) is a real video-compositing problem, not just concatenation,
  and this feature is a fast rough-cut preview, not a renderer. It also
  uses fast (keyframe-nearest) seeking rather than frame-accurate
  trimming, so cuts can land up to a fraction of a second off — fine for
  judging pacing and story, not for frame-accurate delivery. Every clip
  is re-encoded and normalized to a common resolution/frame rate/audio
  format before concatenating, so mismatched sources (different cameras,
  different frame rates) still combine into one playable file. Needs
  `ffmpeg` (same dependency as thumbnails) and every main cut's source
  linked — it'll tell you exactly which sources are still missing rather
  than silently skipping them.
- **Project files** (`.rcstudio.json`) otherwise store file paths, not the
  transcript or video content itself — but that also means moving
  or renaming a linked file will show up as "missing" the next time you
  load that project; just relink it.
- **OTIO export** is hand-built to the current OpenTimelineIO JSON schema
  rather than depending on the `opentimelineio` Python package, the same
  approach used for the Premiere and Final Cut exports — no extra
  dependency for you to have installed, one less thing that could be
  missing at runtime. Its output was checked against the real reference
  `opentimelineio` library during development (parsed successfully,
  correct clip counts and durations) rather than just assumed correct
  from the spec. Two things worth knowing about its shape: OTIO tracks are
  strictly sequential with no "absolute position" concept, so B-roll is
  represented as a Gap (blank space) pushing each clip to the right point
  on its own track — and if two B-roll clips overlap in time, they can't
  share one track (nothing in OTIO can occupy the same span twice on a
  single track), so overlapping clips spread across additional tracks
  (V2, V3, ...) automatically rather than silently colliding. OTIO also
  has no standardized volume-automation primitive this app can guarantee
  is portable across tools, so B-roll audio mode and any ducking amount
  are carried as metadata for reference, not as an interpreted effect.
- **Autosave and crash recovery need no setup and have no settings** — the
  recovery snapshot lives at a single fixed path (not per-project) and is
  silently overwritten after every generate, edit, revision, restore, or
  periodic background check of in-progress Cuts-tab edits. It's separate
  from Save Project entirely: closing the app cleanly doesn't delete it,
  and restoring it doesn't require having saved a project file first (or
  at all). Since it's a single slot, only the most recent session's
  recovery data is ever kept — it's meant for "don't lose the last thing
  I was doing," not a rolling history of every session (that's what
  History and named Sequences are for, and those persist properly in
  project files you do explicitly save).
- Gemini's free tier has request-rate and usage limits that can change;
  check https://ai.google.dev/pricing for current details if you hit one.
- If Gemini returns "high demand" (HTTP 503) or a rate limit (HTTP 429),
  the app automatically retries with backoff (up to 4 attempts) before
  showing an error — you'll see a live "retrying…" status while it waits.
  If it still fails after that, the model itself is genuinely overloaded;
  wait a bit, or switch models in the sidebar and try again — a different
  model is often available even when one is under heavy load. Google's
  current model line-up changes over time, so if a model ID stops working
  entirely (404), check https://ai.google.dev/gemini-api/docs/models for
  the current names and update the options in `frontend/index.html` /
  `DEFAULT_MODEL` in `backend/gemini_client.py`.
- If the **Llama** provider says it can't reach Ollama, make sure Ollama
  is actually running (`ollama serve`, or open the Ollama app) and that
  the URL in the sidebar matches where it's listening (default
  `http://localhost:11434`). Click **Refresh** next to the model dropdown
  to re-check. A 404-style "model not found" error means the model name
  hasn't been pulled yet — run `ollama pull <model>` in a terminal, then
  Refresh again. Local generation can be considerably slower than Gemini
  depending on your hardware and the model size; that's expected, not a
  bug — there's no cloud GPU behind it.
- **A Llama call feels slow even on a short/single transcript, especially
  if some time passed since your last Generate or Revise.** Ollama
  unloads a model from memory a few minutes after its last use by
  default; the app asks it to stay loaded for 30 minutes instead
  (`keep_alive`), but a longer gap, a machine restart, or another app
  competing for memory can still force a reload — which means the model
  has to be read back off disk before it can even start on your prompt,
  on top of normal processing time. The status line shows the context
  size (`num_ctx`) actually used for each Llama call, so you can see
  whether an unexpectedly large context — not a reload — is what's
  driving the slowness for a given transcript.
- **On 16GB Macs specifically:** an 8B model plus a several-thousand-token
  context can leave very little headroom once macOS and the app itself
  are accounted for, and running low on unified memory degrades
  performance far more sharply than it does on a machine with room to
  spare. If Llama is consistently slow even on modest transcripts, trying
  a smaller model (`llama3.2:3b`) is usually a bigger win on this tier of
  hardware than trying to make an 8B+ model faster.
- **Llama only used segments from one transcript, or generation/revision
  got noticeably slower with more sources added.** The app automatically
  sizes Ollama's context window (`num_ctx`) to fit your actual prompt
  (transcripts + brief), rather than trusting the model's own default —
  which is often just 2-4K tokens and would otherwise silently truncate
  a multi-transcript prompt partway through, so the model never even saw
  later sources. The tradeoff is real, though: a bigger context window
  means proportionally more compute per call, so more/longer transcripts
  will make Llama generation and revision slower, especially on CPU-only
  or memory-constrained machines — that's an inherent cost of local
  inference, not a bug. If it's too slow to be usable, trimming which
  transcripts you have loaded, or switching to Gemini for that session,
  are the practical options.
- **"This transcript set needs roughly N tokens of context... beyond the
  32,768-token limit."** This means a transcript (or combined set of
  transcripts) is large enough that fitting it into one local call would
  need a context window most consumer hardware can't hold at all — the
  KV cache such a window requires can exceed available memory before the
  model's own weights are even counted. Rather than silently trying it
  and hanging for a very long time (or crashing), the app refuses that
  request immediately with this message. There's no local model or
  hardware upgrade that reliably fixes this for a genuinely huge
  transcript on typical hardware — the practical options are switching to
  Gemini for that generation (no such local ceiling), or working with a
  smaller portion of the transcript at a time. `MAX_PRACTICAL_NUM_CTX` in
  `backend/llama_client.py` is the actual limit, deliberately
  conservative; raise it only if you know your hardware can genuinely
  back it.
- The Llama provider asks Ollama to constrain its output to a strict JSON
  schema (Ollama's "structured outputs" feature, available since Ollama
  0.5) rather than just hoping the model follows instructions — this is
  what keeps a local model's response shape reliable enough to use.
  Update Ollama (`ollama --version`, or reinstall from ollama.com) if
  you're on an older version; the app falls back automatically to
  looser JSON mode on very old installs, but a current Ollama is more
  reliable. If a Llama response is still rejected, the error message
  names the specific reason (e.g. an out-of-range segment or an
  unexpected shape) rather than a bare failure — that detail is usually
  enough to tell whether it's worth retrying or switching to a larger
  model.

## Project structure

```
video-script-studio/
├── main.py                    # opens the native app window
├── backend/
│   ├── api.py                 # bridge exposed to the frontend (js_api);
│   │                          #   also handles the editable cut list,
│   │                          #   B-roll/main track split + audio modes,
│   │                          #   duration tracking, revision requests,
│   │                          #   drop-frame state, batch relink, version
│   │                          #   history + comparison, named sequences,
│   │                          #   autosave/crash recovery, and project
│   │                          #   save/load (incl. Ctrl/Cmd+S quick-save)
│   ├── transcript_parser.py   # SRT/VTT/custom -> timecoded segments,
│   │                          #   source-video auto-link detection,
│   │                          #   target-duration parsing, and drop-frame
│   │                          #   <-> non-drop-frame timecode conversion
│   ├── gemini_client.py       # strict-JSON Gemini API call with retry
│   ├── llama_client.py        # local Ollama chat-API call (Llama, no key,
│   │                          #   no cloud) with the same generate_script
│   │                          #   interface, plus a model-list helper
│   ├── xml_builder.py         # segments -> Premiere (XMEML) XML,
│   │                          #   forced stereo audio + video/audio links,
│   │                          #   V2+ B-roll overlay tracks with silent/
│   │                          #   full/duck_main audio modes, greedy lane
│   │                          #   assignment so overlapping B-roll spreads
│   │                          #   across tracks instead of colliding
│   ├── fcpxml_builder.py      # segments -> Final Cut Pro (FCPXML) XML,
│   │                          #   resource-based format with B-roll as
│   │                          #   anchored connected clips + audio modes,
│   │                          #   greedy lane assignment so overlapping
│   │                          #   B-roll spreads across lanes instead of
│   │                          #   colliding
│   ├── otio_builder.py        # segments -> OpenTimelineIO (.otio) JSON,
│   │                          #   with greedy lane assignment so
│   │                          #   overlapping B-roll never collides
│   ├── script_writer.py       # segments -> readable Markdown script,
│   │                          #   including B-roll and duration sections
│   ├── thumbnails.py          # ffmpeg-based storyboard frame extraction
│   ├── preview_server.py      # local-only (127.0.0.1) HTTP server with
│   │                          #   Range support, for the Cuts tab video
│   │                          #   preview player
│   └── video_export.py        # ffmpeg concat pipeline: renders the main
│                              #   track's cuts into one exported .mp4
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── app.js
├── requirements.txt
├── .env.example
└── README.md
```
