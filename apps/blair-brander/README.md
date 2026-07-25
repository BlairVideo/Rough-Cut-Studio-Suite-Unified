# Blair Academy — Title & Motion Graphics Creator

A local desktop app for building brand-compliant video titles and motion
graphics for Blair Academy, and exporting them as a transparent PNG or a
transparent-background video clip to drop straight into your editing
timeline (Premiere, DaVinci Resolve, Final Cut, CapCut, etc.).

Built to run **entirely on your own computer** — no browser tab, no
account, no cloud rendering, no API keys. Everything in this document
describes what the app does and why it was built this way.

---

## 1. What this is

- A **desktop app window** (Python + Tkinter — Tkinter ships with Python,
  so there's nothing extra to install for the interface itself).
- A **live preview** of your title card, built from Blair's real brand
  colors, seals, and type choices (pulled from `2025_Style_Guide_final.pdf`).
- A **design control panel**: text, fonts, brand colors, logo/seal
  placement, and animation style.
- An **"AI" prompt bar** that understands plain-English design requests
  like *"clean elegant navy background, seal at bottom center, slow fade"*
  — see [Section 5](#5-the-ai-prompt-bar-how-it-actually-works) for exactly
  how this works and why it's local instead of cloud-based.
- **Export** to a transparent PNG (still title card) or a
  transparent-background video clip (animated lower third / opener /
  bumper) that overlays cleanly on top of any footage.

## 2. Setup

**Requirements:**
- Python 3.9 or newer ([python.org](https://www.python.org/downloads/) — free)
- The `Pillow` image library
- `ffmpeg`, free & open-source, **only needed if you want video export**
  (still-image/PNG export works without it)

**Install steps (one time):**

```bash
# Optional but recommended: use a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 1. Install Python dependencies (just Pillow)
pip3 install -r requirements.txt
# if that's blocked by your system Python instead of a venv, use:
pip3 install -r requirements.txt --break-system-packages

# 2. Install ffmpeg (free, open-source) — only needed for video export
#    macOS (with Homebrew):
brew install ffmpeg
#    Windows: download a build from https://ffmpeg.org/download.html
#             and add its /bin folder to your PATH
#    Linux:
sudo apt install ffmpeg
```

**Run the app:**

```bash
cd blair_titles
python3 app.py
```

If you see `ModuleNotFoundError: No module named 'PIL'`, it means the
`pip install -r requirements.txt` step above didn't run (or ran in a
different Python/venv than the one launching `app.py`) — run
`pip install pillow` in the same terminal you use to run `python3 app.py`
and try again.

A regular desktop window opens — nothing runs in a browser, and the app
makes zero network requests. You could disconnect from the internet
entirely and it would work exactly the same.

## 3. Using the app

The app opens in a dark interface designed for long editing sessions.
The preview and timeline both resize to fill available space as you
resize the window — maximize it for a much larger working canvas.

0. **Projects** — use the **File** menu (or the New / Open… / Save /
   Save As… buttons at the top of the left panel) to save your work to a
   `.blairtitle` file and pick it back up later. `Ctrl+S` saves, `Ctrl+O`
   opens, `Ctrl+N` starts fresh. Project files are plain JSON on your own
   disk — nothing is uploaded.
0b. **Undo / Redo** — every meaningful change (preset, prompt, color, timing
   drag, slider) is tracked. Use the toolbar buttons or `Ctrl+Z` / `Ctrl+Y`
   at any time. Rapid changes (typing, dragging) are debounced into a
   single history step once you pause, so undo doesn't require dozens of
   presses to get back one real edit.
0c. **Playback** — hit the ▶ Play button (or press Space) to watch the
   animation loop in real time, right in the preview. Pause freezes it in
   place, Stop resets to the beginning, and the time readout shows exactly
   where you are (e.g. "1.24s / 3.00s"). Uncheck **Loop** if you'd rather
   it play once and stop on the last frame. Playback automatically pauses
   the moment you touch a control or drag the timeline, so you never fight
   it while editing.
1. **Pick a Style Preset** to start from one of five on-brand looks:
   *Clean & Elegant*, *Pop & Upbeat*, *Traditional / Formal*,
   *Athletics / High Energy*, and *Warm / Community*.
2. **Pick a Format** — an aspect ratio (16:9 landscape video, 9:16
   vertical for Stories/Reels/TikTok, 1:1 square or 4:5 portrait for
   Instagram feed posts) and a **Layout**:
   - *Full Title Card* — centered title/subtitle, the classic opener/bumper look.
   - *Lower Third* — left-aligned name/caption bar over the bottom of the
     frame. The logo auto-relocates out of the caption bar's way if left
     at a default bottom position.
3. **Type your Title / Subtitle** in the Text panel.
4. **Adjust colors** using the "Brand color…" buttons — these open Blair's
   official palette first so it's hard to go off-brand by accident; a
   "Custom color…" option is available if needed.
   - **Background style**: *Solid* or *Gradient* (pick a second color, or
     leave it on auto and it'll derive a complementary darker shade of
     your background color automatically).
   - **Vignette**: a slider that softly darkens the edges/corners, plus a
     **shape** control — *Elliptical* (matches the frame's aspect ratio),
     *Circular* (a rounder, lens-like spotlight regardless of aspect), or
     *Rectangular* (a soft, even frame darkening all four edges).
5. **Choose a logo/seal** (all Blair marks are cleared for use in this
   sanctioned internal tool — see Section 4), its placement, its **size**
   and **opacity** (independent sliders — opacity is handy for a subtle
   watermark-style mark instead of a bold one), and whether it renders
   in its original color or as a white knockout.
6. **Pick an animation** — eight entrance styles: `fade`, `slide`, `zoom`,
   `bounce`, `wipe`, `stagger` (a letter-by-letter cascade), `typewriter`,
   and `none` (static) — plus an independent **Outro style** (`fade`,
   `slide`, `zoom`, `wipe`, or `none`) for how the title, subtitle, and
   logo exit at the end of the clip.
7. **Fine-tune timing on the Timeline**, centered below the preview: each
   of the three lanes (Title / Subtitle / Logo) shows **two** segments —
   a bright one for the entrance window and a muted one for the exit
   window. Grab an edge to change when that segment starts or finishes,
   drag the middle to shift the whole window, or **double-click a
   segment to reset it** to its default timing. Drag anywhere on the
   time axis beneath the lanes to scrub the preview, or just hit Play —
   the red playhead tracks live playback across the timeline too. The
   timeline resizes cleanly with the window and shows live hover
   feedback (cursor changes) so it's clear what you're about to grab.

7b. **Lower Third fine control** — when Layout is set to *Lower Third*,
   extra controls appear: **Position** (Bottom/Top × Left/Center/Right,
   six options total), **Scale** (50%–180%) to size the name plate up or
   down, and **Plate color / Plate opacity** to control the background
   bar behind the text independently of the overall background color —
   leave it on auto to match your background, or pick any brand color
   (black at ~60% opacity is a common broadcast look).
8. **Export**:
   - *Still (transparent PNG)* — a single settled frame.
   - *Video — MOV (recommended)* — an animated clip with a true alpha
     channel. Safest choice; every editor tested during development reads
     its transparency correctly with no extra setup.
   - *Video — WebM* — smaller file size, also has a real alpha channel,
     but a few tools' default VP9 decoders will show a black box instead
     of transparency unless told to use the `libvpx-vp9` decoder
     explicitly. If that happens, re-export as MOV instead.

Exported files land wherever you choose to save them; nothing is
uploaded anywhere. Note that exporting an image/video is separate from
saving a *project* — export gives you a finished PNG/MOV/WebM for your
editing timeline, while saving a project (`.blairtitle`) preserves all
your settings so you can keep editing later.

## 4. Brand accuracy notes — sanctioned use

**This build is authorized for internal Blair Academy Communications
use.** All seal and logo lockups included are cleared for use in this
tool — there's no additional sign-off step baked into the app.

- **Colors** are the exact hex/PMS values from the 2025 Style Guide
  (pages 13–14): Blair Blue `#004b8d`, Dark Blue `#093266`, the secondary
  palette (Orange, Red, Yellow, Teal, etc.), and so on.
- **Fonts**: Blair's actual brand typefaces (Avenir Next LT Pro, Adobe
  Garamond Pro, Archer, Trajan Pro, Haettenschweiler) are commercial,
  licensed fonts that live in Blair's Adobe/Creative Cloud account — this
  app can't legally redistribute them. Instead it bundles free,
  open-license (SIL Open Font License) substitutes chosen to closely
  match each one visually:

  | Brand font | Bundled substitute |
  |---|---|
  | Adobe Garamond Pro | EB Garamond |
  | Trajan Pro | Cinzel |
  | Haettenschweiler | Anton |
  | Avenir Next LT Pro (+ weights) | Poppins (Regular/SemiBold/Bold) |
  | Avenir Next Condensed | Oswald |
  | Bree Serif | **Bree Serif itself** — it's already a free, approved Blair font |

  If Blair's design team has the real fonts installed (via Creative
  Cloud), drop matching `.ttf`/`.otf` files into the `fonts/` folder using
  the filenames referenced in `brand.py`, and the app will use the
  genuine brand typeface automatically — no code changes needed.

  **Note if you cloned this from git:** the real Avenir Next LT Pro,
  Adobe Garamond Pro, and Haettenschweiler files are intentionally
  excluded from this repository (see the root `.gitignore`) — committing
  licensed commercial fonts to version control can violate the vendor's
  redistribution terms, even in a private repo. A fresh clone runs fine
  on the free substitutes above with no missing-file errors; to render
  with the genuine brand typefaces, copy the real font files (from
  Blair's Creative Cloud account, or from an existing working copy of
  this app) into `fonts/` using the filenames `brand.py` expects.

- **Seals/logos**: several of the source PNGs provided (the "white" and
  "_KO" knockout variants) were flattened with a plain white background
  and no alpha channel, so they render blank. Rather than ship broken
  assets, the app derives clean transparent and white-knockout versions
  **on the fly** from the working full-color artwork — see `assets.py`.

## 5. The AI prompt bar — how it actually works

This was a deliberate design decision worth explaining:

The prompt bar is a **local, rule-based interpreter** (`prompt_ai.py`),
not a connection to a hosted AI model. Typing a sentence runs it through
keyword matching entirely on your machine — there is no network call.

**Why build it this way instead of wiring up a cloud LLM API:**
- **No data ever leaves the editor's computer.** Titles, event names, and
  internal project language never get sent anywhere.
- **No API keys to buy, manage, rotate, or accidentally leak.**
- **Free forever**, no usage limits, and it keeps working with no
  internet connection (useful when editing on location).
- **No new attack surface.** A hosted-AI integration means a new
  third-party service in the loop, new credentials to protect, and a new
  place data could leak. Keeping this fully local avoids that entirely.

It understands mood words (*elegant, upbeat, athletic, formal,
community*), color names, animation styles (*fade, slide, bounce,
typewriter*), speed words (*slow/fast*, or an explicit "3 seconds"),
logo choice & placement (*"seal at bottom center"*, *"monogram top
right"*), and quoted text overrides (`title: "Welcome Home"`). Try a few
phrases and watch the preview update — the app also reports back exactly
what it understood after each prompt, so nothing happens silently.

If Blair ever wants genuine natural-language flexibility beyond these
patterns, `prompt_ai.interpret()` is the single, isolated function to
swap out for a real model call — everything else in the app just
consumes the dictionary it returns, so that upgrade wouldn't touch the
rest of the codebase. That's a deliberate architecture choice, not a
missing feature.

## 6. Project structure

```
blair_titles/
  app.py          — the Tkinter desktop application (run this)
  brand.py        — colors, fonts, style presets, aspect ratios (single source of truth)
  assets.py       — logo/seal loading + transparency + recoloring
  renderer.py     — draws each frame with Pillow (layouts, backgrounds, animation, logo)
  timeline.py     — the draggable per-element timing widget below the preview
  project_io.py   — save/load .blairtitle project files (plain JSON)
  export.py       — PNG export + local ffmpeg video export
  prompt_ai.py    — the local, offline "AI" prompt interpreter
  assets/         — Blair's logo/seal source PNGs
  fonts/          — bundled open-license font files
  output/         — nothing by default; your exports land where you choose to save them
```

## 7. Extending it

- **New style preset**: add an entry to `PRESETS` in `brand.py`.
- **New brand color**: add it to `PRIMARY_COLORS` or `SECONDARY_COLORS` in
  `brand.py` — it'll automatically show up in the color picker.
- **New animation style**: add a branch in `renderer.render_frame()`'s
  animation section, plus a keyword mapping in `prompt_ai.ANIMATION_KEYWORDS`.
