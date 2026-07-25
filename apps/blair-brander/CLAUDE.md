# Blair Brander — Title & Motion Graphics Creator

A local Tkinter desktop app for Blair Academy Communications that builds
brand-compliant animated title cards / lower-thirds and exports them as a
transparent PNG or an alpha-channel video clip for editors (Premiere,
DaVinci, Final Cut, CapCut). See [README.md](README.md) for the full
user-facing walkthrough — this file is oriented at whoever (human or
Claude) is editing the code.

## Run it

```bash
pip install -r requirements.txt   # just Pillow
python3 app.py
```

`ffmpeg` (via `shutil.which`) is only required for video export; PNG
export and the UI work without it. No API keys, no network calls,
no env vars — this is deliberate, see "No AI API, on purpose" below.

## Architecture

Everything revolves around one shared **`scene` dict** (shape defined by
`default_scene()` in [app.py](app.py)) — title/subtitle text, brand
colors, fonts, canvas size, logo choice, and per-element animation
in/out timestamps as fractions of the timeline (`0.0`–`1.0`). Every
module reads and/or writes this dict; there's no other shared state.

```
app.py        Tkinter UI + app state (the only stateful/UI layer). Entry point.
  ├─ brand.py      Single source of truth: colors, fonts, presets, canvas sizes, UI theme.
  ├─ renderer.py   Pure function render_frame(scene, t) -> PIL.Image. No UI, no I/O.
  ├─ assets.py     Logo loading/transparency-keying/recoloring, in-memory cache.
  ├─ prompt_ai.py  Local regex/keyword interpreter, interpret(text, scene) -> (scene, notes).
  ├─ project_io.py JSON save/load of the scene dict (.blairtitle files).
  ├─ export.py     PNG export + video export (shells out to ffmpeg via subprocess).
  └─ timeline.py   tk.Canvas widget for dragging in/out animation timing, calls back into app.py.
```

- `renderer.render_frame(scene, t)` is a pure function of its inputs — no
  hidden state, safe to call from a background thread. `render_still()`
  picks the "settled" plateau moment (after entrances finish, before any
  outro starts) rather than assuming `t=1.0`.
- `prompt_ai.interpret()` returns a *new* scene dict rather than mutating
  in place. It's a local, offline, deterministic keyword matcher — not a
  network call.
- `app.py` owns `self.scene` and is the only place it's mutated in
  response to UI events. Anything reading `scene` off the UI thread
  (e.g. a video-export worker thread) should get a `copy.deepcopy()`
  snapshot, not a live reference — see the export race note below.

## No AI API, on purpose

The "AI" prompt bar (`prompt_ai.py`) is a local keyword/regex interpreter,
not a hosted LLM call — no data leaves the machine, no API keys, works
offline. This is a deliberate, documented decision (see README.md §5),
not a missing feature. If that's ever revisited, `prompt_ai.interpret()`
is the single, isolated swap-in point for a real model call — every
other module just consumes the dict it returns.

## Brand assets

- **Colors**: `brand.py` `PRIMARY_COLORS` / `SECONDARY_COLORS`, sourced
  from the 2025 Style Guide (pp. 13–14). Add a new brand color there and
  it automatically appears in every color picker.
- **Fonts**: `brand.py` `FONTS` dict, each entry has `regular`/`italic`
  file names (resolved against `fonts/`) plus a `brand_match` label.
  Real, licensed Blair typefaces (Avenir Next LT Pro family,
  Haettenschweiler) are already dropped into `fonts/` and wired up.
  **Adobe Garamond Pro is a known gap**: `fonts/` only has Bold/Semibold/
  Italic weights, no true Regular, so "Garamond (elegant serif)"
  intentionally still uses the EB Garamond substitute — swap it once a
  real Regular-weight file is supplied. Trajan Pro has no file in
  `fonts/` at all (still on the Cinzel substitute).
- **Logos/seals**: `brand.py` `LOGO_SOURCES` maps display names to PNGs
  in `assets/`. Transparent and white-knockout variants are derived at
  *runtime* in `assets.py` rather than shipped, because the supplied
  `*_white.png` / `*_KO.png` source files are flattened with no alpha
  channel and would render blank.
- **New style preset** → add an entry to `brand.PRESETS`.
- **New animation** → add a branch in `renderer.render_frame()`'s
  animation section, plus a keyword mapping in
  `prompt_ai.ANIMATION_KEYWORDS`.

## Performance notes

- `renderer.render_frame()` always renders at the *actual* export canvas
  resolution (e.g. 1920×1080), even for the live preview and playback
  scrubbing — `app.py` downsamples afterward. This is intentional: many
  layout constants in `renderer.py` (gaps, margins, divider thickness,
  plate padding) are absolute pixel values tied to that resolution, not
  fractions of a variable render size, so rendering directly at a
  smaller preview resolution would throw off proportions relative to the
  final export. If preview performance ever becomes a problem, the fix
  is to parameterize those constants by a scale factor — not to just
  swap in a smaller canvas size.
- Per-frame hot paths that *are* cheap to memoize are cached:
  `renderer._vignette_mask` and `renderer._gradient_layer` (both
  `lru_cache`d per size/color), and `assets.load_transparent` /
  `load_white_knockout` / `recolor` (in-memory cache in `assets._cache`,
  keyed off file mtime for the file-backed ones). Adding a new expensive,
  deterministic-per-scene-value render step should follow this pattern
  rather than recomputing per frame.
- `assets.load_transparent`'s white-background keying uses flood fill
  from the image border (`_flood_background_mask`) plus a soft
  antialiasing ramp at the actual boundary (`_antialias_ramp`), not a
  flat per-pixel threshold — a flat threshold alone punches transparent
  holes through isolated near-white compression artifacts trapped
  *inside* solid-color fills, and binarizes genuinely anti-aliased edges
  (thin lockup lettering) into a speckled look. `Image.point()` with a
  callable is unreliable on "I" (32-bit) mode in some Pillow versions
  (silently produces an all-zero result outside an ~8-bit domain), so
  the sentinel-to-mask extraction after flood fill uses a direct pixel
  loop rather than `.point()` — flood fill itself already dominates the
  cost, so this is negligible overhead and avoids that version quirk.

## Gotchas

- `export_video()` in `app.py` renders frames on a background thread
  over several seconds; it's given a `copy.deepcopy()` snapshot of
  `self.scene` taken right before the thread starts, specifically so
  edits made on the UI thread mid-export can't mutate the dict the
  render loop is reading. Keep passing a snapshot, not `self.scene`
  itself, into any future background work.
- Hex color parsing has one home: `brand.hex_to_rgb()`. `renderer.py`,
  `assets.py`, and `timeline.py` all call into it — don't reintroduce a
  local copy of the `#rrggbb` → tuple parsing.
- `scene["logo_color_mode"]` supports `"original"` / `"white"` /
  `"custom"`; the "custom" option and its color swatch row in the Logo
  panel only appear once that mode is selected (`_update_logo_custom_color_visibility`
  in `app.py`). If you add another logo color mode, follow the same
  show/hide pattern rather than always rendering the control.
- No automated tests exist. Sanity-check render changes with a quick
  script (`renderer.render_frame(default_scene(), t=...)` /
  `export.export_png(...)`) rather than assuming a change compiles and
  is correct.
- `scene["divider"]` (the accent-color rule between title and subtitle)
  has a UI checkbox ("Show accent divider…" in Brand Colors &
  Background) as well as preset defaults and `prompt_ai.py` keyword
  handling ("no divider" / "with a line"). All three write the same
  `divider` key — keep new ways of setting it consistent with that.
- `timeline.py` enforces `CROSS_GAP` between each row's "in" and "out"
  segments (dragging either one, or moving a whole segment, clamps
  against the other) — this is deliberate, not a bug: without it the two
  segments' handles can land on the same pixel and become impossible to
  grab individually. Keep that clamp if you touch `_on_motion`.
- Timeline drag snapping (`Timeline._snap`) snaps to the nearest
  exportable video frame (`1 / (brand.FPS * duration)`), not a fixed
  percentage — it's intentionally duration-dependent so it stays fine-
  grained on long clips.
- Space (play/pause) is bound both via `root.bind_all` and via
  `root.bind_class` overrides on `TButton`/`TCheckbutton`/`TMenubutton`/
  `Button` in `app.py`. This is deliberate: those widget classes have
  their own built-in `<space>` action (invoke/toggle/post-dropdown), and
  Tk fires the widget's own class binding *and then* the "all" bindtag
  for the same keypress — so whichever control last had focus (very
  often the Play/Pause button itself, right after being clicked) would
  double-fire the toggle in one keystroke and appear to do nothing. If
  you add a new interactive ttk widget class to the UI, check whether it
  has a default `<space>` action before assuming the global shortcut
  works while it's focused.
- `blair_seal_horizontal.png` and `blair_seal_vertical.png` in `assets/`
  were cropped to their actual content bounding box (with a small
  uniform padding) — the originals shipped on an oversized, uncropped
  420×280 canvas with the real artwork filling only 33%/64% of it,
  which threw off `assets.fit_height()`'s scaling relative to the other
  logo choices. If new logo source PNGs are dropped in, check their
  content fill ratio the same way before assuming `fit_height()` will
  size them consistently with the rest of `LOGO_SOURCES`.
