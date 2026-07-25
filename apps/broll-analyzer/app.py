"""
app.py
B-Roll Analyzer - desktop GUI app.

Pick a folder of b-roll video clips, analyze them for technical quality
(sharpness, exposure, stability/motion), review the ranked results, and
export a Premiere Pro-compatible XML (Final Cut Pro XML) containing a
ranked bin of clips plus a ready-made "best selects" sequence.

Run with:  python app.py
Requires:  opencv-python, numpy  (pip install -r requirements.txt)
"""

import os
import queue
import threading
import traceback
import concurrent.futures
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from analyzer import (analyze_clip, find_video_files, ClipResult, Segment, rescore_clip,
                       thumbnail_ppm_bytes, thumbnail_is_stale, refresh_thumbnail,
                       open_segment_capture, read_next_segment_frame, limit_opencv_threads)
from xml_export import export_xml
import result_cache
import app_settings


def _ensure_homebrew_on_path():
    """A GUI app launched via Finder/LaunchServices on macOS (which is
    how a packaged .app bundle is normally opened) does not inherit the
    PATH a login shell sets up from .zshrc/.bash_profile -- so
    Homebrew-installed tools like ffmpeg/ffprobe can be completely
    invisible to this app's subprocess calls even when they work fine
    from Terminal on the very same machine. Without this, a packaged
    build would silently fall back to generic audio-format/fps
    assumptions for everyone, regardless of whether ffmpeg is actually
    installed. Run once at import time, before any analysis can start;
    a harmless no-op on Linux/Windows dev runs or a Mac without
    Homebrew, since it only adds directories that actually exist and
    aren't already on PATH.

    If this build vendored its own copy of ffmpeg/ffprobe (see
    vendor_ffmpeg.sh + setup.py's RESOURCES_BIN handling), that copy is
    placed FIRST on PATH -- ahead of Homebrew -- so a fully
    self-contained build behaves identically on a machine that has a
    different (or no) ffmpeg installed system-wide, rather than picking
    up whatever happens to be on that machine's PATH."""
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    new_parts = list(parts)

    # py2app sets RESOURCEPATH (pointing at Contents/Resources) in the
    # running bundle's environment -- absent entirely when running from
    # source, which is exactly when we want to skip this. Placed FIRST
    # so a self-contained build's own vendored copy always wins, rather
    # than picking up whatever happens to be on this particular machine.
    resource_path = os.environ.get("RESOURCEPATH")
    if resource_path:
        bundled_bin = os.path.join(resource_path, "bin")
        if os.path.isdir(bundled_bin) and bundled_bin not in new_parts:
            new_parts.insert(0, bundled_bin)

    # Homebrew stays lowest-priority, same as before: only consulted if
    # neither a bundled copy nor anything already on PATH provides these.
    for homebrew_dir in ("/opt/homebrew/bin", "/usr/local/bin"):
        if os.path.isdir(homebrew_dir) and homebrew_dir not in new_parts:
            new_parts.append(homebrew_dir)

    if new_parts != parts:
        os.environ["PATH"] = os.pathsep.join(new_parts)


_ensure_homebrew_on_path()

try:
    import vision_energy
except ImportError:
    vision_energy = None


def _analyze_clip_worker(path, window_sec, max_segments, enable_energy, energy_weight):
    """Runs in a worker process (see ProcessPoolExecutor in
    BRollAnalyzerApp._run_analysis). Must be a plain module-level
    function -- not a method or a closure -- so it can be pickled and
    sent to the worker. No progress_cb is passed here: per-frame
    progress can't cross a process boundary cheaply, so progress is
    instead reported per-completed-file by the caller.

    If analyze_clip raises outright (rather than recording the problem
    in ClipResult.error itself), catch it here so one bad file can't
    crash the whole batch -- mirrors the try/except that used to wrap
    the single-threaded call directly in the GUI thread."""
    try:
        return analyze_clip(path, progress_cb=None, window_sec=window_sec,
                             max_segments=max_segments, enable_energy=enable_energy,
                             energy_weight=energy_weight)
    except Exception as e:
        return ClipResult(path=path, filename=os.path.basename(path),
                           duration=0, fps=0, width=0, height=0,
                           error=f"{e}\n{traceback.format_exc(limit=1)}")


def _worker_init():
    """Runs once per ProcessPoolExecutor worker process, before it
    analyzes any clips -- see _run_analysis's executor construction.
    Each worker here is already its own OS process running in parallel
    with `num_workers` siblings; without this, OpenCV's own internal
    multithreading (Laplacian, optical flow) would ALSO fan out across
    every CPU core inside each of those processes, causing severe
    oversubscription instead of clean parallel scaling."""
    limit_opencv_threads()


# ---------------------------------------------------------------------
# Brand palette / typography.
#
# Base surfaces are a dark grey derived from Blair's own Cool Grey
# (PMS 430) -- same hue, darkened -- rather than a generic/off-brand
# charcoal. Accents (buttons, headings, selection) come straight from
# Blair's brand palette (2025 Graphic & Editorial Style Guide, "Color
# Palette" section), with Orange used sparingly for selection/progress
# highlights exactly as the guide intends ("used to add visual
# interest," not as a base color).
#
# Fonts follow the guide's "Website Fonts" page, since this is a
# screen UI rather than a print piece: Proxima Nova for body/headings,
# Bree Serif for call-out-style text (the app's header), matching what
# blair.edu itself uses -- distinct from the print-only Avenir Next LT
# Pro / Adobe Garamond Pro pairing used in the guide's other materials.
#
# Everything here only affects on-screen appearance -- no network
# activity, credentials, or data handling is involved.
# ---------------------------------------------------------------------
BRAND = {
    "bg":            "#1e2224",   # Dark Blair grey -- Cool Grey (PMS 430,
                                   # #72808a) darkened to a low-lightness tint
                                   # at the same hue, rather than a generic
                                   # off-brand charcoal
    "surface":       "#272c2f",   # panels, table body -- same grey ramp, one
                                   # step lighter than bg
    "surface_alt":   "#33393d",   # input fields, alternating/hover surfaces
    "border":        "#41494e",   # subtle dividers on dark surfaces
    "blair_blue":    "#004b8d",   # PMS 288 -- primary buttons, banner
    "dark_blue":     "#093266",   # PMS 534 -- table headings
    "sky":           "#5f8fb4",   # PMS 7454 (secondary) -- hover/active accent
    "cool_grey":     "#a3a7ac",   # PMS 430, lightened for legible on-dark text
    "text":          "#e9e7e5",   # Web Grey 1 -- primary text on dark surfaces
    "orange":        "#f15d22",   # PMS 1665 -- sparing highlight only
    "red":           "#da1a32",   # PMS 186 (secondary palette) -- failed/error state
    "yellow":        "#dd971a",   # PMS 131 (secondary palette) -- warning state
    "white":         "#ffffff",
}


def _blend_hex(hex_a: str, hex_b: str, t: float) -> str:
    """Blend two '#rrggbb' colors; t=0 -> hex_a, t=1 -> hex_b. Used to
    derive muted, full-row-background versions of the guide's Red/
    Yellow (which are vivid, print-oriented accent colors -- filling an
    entire table row with either at full saturation would be too loud
    for a status indicator someone has to look at for a while) by
    tinting them into the table's own dark surface color, rather than
    inventing new off-brand hex values from scratch."""
    a = tuple(int(hex_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(hex_b[i:i + 2], 16) for i in (1, 3, 5))
    mixed = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return "#{:02x}{:02x}{:02x}".format(*mixed)


# Muted row-tint backgrounds for the results table: the guide's actual
# Red/Yellow blended into the table's surface color at a low ratio, so
# a flagged row reads as "highlighted" rather than replacing the whole
# dark theme with a jarring solid block of color.
BRAND["red_row"] = _blend_hex(BRAND["surface"], BRAND["red"], 0.35)
BRAND["yellow_row"] = _blend_hex(BRAND["surface"], BRAND["yellow"], 0.35)


def _first_available_font(candidates, fallback="TkDefaultFont"):
    """Return the first font family from `candidates` that's actually
    installed on this machine, else `fallback`. Brand fonts are
    licensed assets that may not be installed on every workstation, so
    we degrade gracefully instead of silently rendering with a
    missing-font placeholder."""
    try:
        installed = set(tkfont.families())
    except Exception:
        return fallback
    for name in candidates:
        if name in installed:
            return name
    return fallback



# The official Blair seal (no-ribbon version, per the 2025 Graphic &
# Editorial Style Guide's "General Blair Seals" page), pre-cropped and
# composited onto a plain white badge so the mark's blue ink reads
# clearly against the Blair Blue header bar -- the guide itself always
# shows the seal on a white/light field, never directly on solid blue.
# Use of Blair's seal requires the Director of Communications'
# endorsement per the guide; this asset is included on that basis for
# this school-sanctioned tool.
#
# Embedded directly as base64 (PNG, with a GIF fallback) rather than
# shipped as separate files in an assets/ folder: a prior version did
# exactly that, and it broke the moment app.py was saved/downloaded on
# its own without that folder coming along -- an easy thing to lose
# when copying files individually. Embedding means the logo can never
# go missing independently of the code that displays it. Two formats
# are kept because tk.PhotoImage only gained native PNG support in
# Tcl/Tk 8.6; GIF has been supported by every Tk version, so it's
# tried second if PNG fails specifically on format grounds (seen in
# practice on macOS's system-bundled Python, which often still links
# the older Tk 8.5).
_SEAL_PNG_B64 = """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"""

_SEAL_GIF_B64 = """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"""


def _load_seal_photo():
    """Best-effort load of the embedded header seal image, PNG first
    then GIF. Returns None (header just renders without the logo) only
    if this Tcl/Tk build can decode neither -- and prints *why*, so
    that's diagnosable instead of silently unexplained."""
    format_errors = []
    for label, data in (("PNG", _SEAL_PNG_B64), ("GIF", _SEAL_GIF_B64)):
        try:
            return tk.PhotoImage(data=data)
        except tk.TclError as e:
            format_errors.append(f"{label}: {e}")

    print("Note: could not load the header logo, continuing without it.")
    for err in format_errors:
        print(f"  {err}")
    print("  (This Python's Tcl/Tk build may be older than 8.6 and can't "
          "decode either embedded image format.)")


class BRollAnalyzerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("B-Roll Analyzer \u2014 Blair Academy")
        self.geometry("1300x650")
        self.minsize(1180, 520)

        self._seal_photo = _load_seal_photo()  # kept alive for the header's lifetime

        self.folder_path = tk.StringVar()
        self.status_text = tk.StringVar(value="Choose a folder of b-roll clips to begin.")
        self.results: list[ClipResult] = []
        self.worker_thread = None
        self._executor = None
        self._cancel_event = threading.Event()
        self._rendered_results: list[ClipResult] = []
        self._row_thumbnails: list = []   # keeps small row-icon PhotoImages alive
        self._preview_photo = None        # keeps the larger preview-panel PhotoImage alive
        self._preview_generation = 0      # invalidates in-flight background thumbnail refreshes
        self._analyzed_folder = None      # folder the current self.results came from (for cache updates)
        self._failed_results: list[ClipResult] = []
        self._energy_failed_results: list[ClipResult] = []
        # Whether energy scoring was actually requested for the run that
        # produced self.results -- captured once at analysis start (see
        # _run_analysis) rather than read live from self.enable_energy,
        # since the checkbox isn't disabled while a run is in progress and
        # a clip's cached ClipResult.energy_enabled reflects whether its
        # *samples* carry energy data, not whether this run wants it shown.
        self._last_enable_energy = False
        # Inline segment-preview playback (in the Preview panel, not a
        # popup window): _segment_generation invalidates any in-flight
        # decode thread/poll loop when playback is stopped/replaced (new
        # selection, new segment click, Replay, or app close), the same
        # pattern _preview_generation already uses for thumbnail refreshes.
        self._segment_generation = 0
        self._active_segment = None       # (ClipResult, Segment) currently shown, for Replay
        self._preview_mode_iid = None     # row iid whose panel is showing segment playback
        self._segment_frame_delay_ms = 42  # updated once the real fps is known

        self._apply_brand_style()
        self._build_ui()
        self._load_persisted_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    def _apply_brand_style(self):
        """Apply the school's brand colors/typography to the window and
        its ttk widgets, in a dark theme using the website's font
        system. Purely cosmetic: no files are read/written and no
        network calls are made, so this carries no security risk."""
        self.configure(background=BRAND["bg"])

        # Website fonts (Style Guide, "Website Fonts"): Proxima Nova for
        # body/headings, Bree Serif for call-out-style text. These may
        # not be installed on this machine, so fall back to widely
        # available fonts that read similarly on screen.
        body_family = _first_available_font(
            ["Proxima Nova", "Avenir Next LT Pro", "Avenir", "Helvetica Neue",
             "Segoe UI", "Helvetica", "Arial"])
        heading_family = _first_available_font(
            ["Bree Serif", "Georgia", "Times New Roman"])

        self.body_font = (body_family, 11)
        self.body_font_bold = (body_family, 11, "bold")
        self.heading_font = (heading_family, 22)
        self.heading_font_small = (heading_family, 13)
        self.small_font = (body_family, 10)

        self.option_add("*Font", self.body_font)

        style = ttk.Style(self)
        # 'clam' is the most reliably themeable built-in ttk theme across
        # platforms for custom colors (default/aqua/vista ignore many
        # color options).
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BRAND["bg"],
                         foreground=BRAND["text"], font=self.body_font)
        style.configure("TFrame", background=BRAND["bg"])
        style.configure("TLabel", background=BRAND["bg"],
                         foreground=BRAND["text"], font=self.body_font)
        style.configure("Muted.TLabel", background=BRAND["bg"],
                         foreground=BRAND["cool_grey"])
        style.configure("Header.TFrame", background=BRAND["blair_blue"])
        style.configure("Header.TLabel", background=BRAND["blair_blue"],
                         foreground=BRAND["white"], font=self.heading_font)

        style.configure("TButton", background=BRAND["blair_blue"],
                         foreground=BRAND["white"], font=self.body_font_bold,
                         padding=6, borderwidth=0, focuscolor=BRAND["blair_blue"])
        style.map("TButton",
                  background=[("active", BRAND["sky"]),
                              ("disabled", BRAND["surface_alt"])],
                  foreground=[("disabled", BRAND["cool_grey"])])

        # Used only for the "View Issues" button, and only once an
        # analysis run actually has failed/energy-failed clips to
        # report -- red is reserved for that state so it isn't
        # competing with Blair Blue for attention the rest of the time.
        style.configure("Issues.TButton", background=BRAND["red"],
                         foreground=BRAND["white"], font=self.body_font_bold,
                         padding=6, borderwidth=0, focuscolor=BRAND["red"])
        style.map("Issues.TButton",
                  background=[("active", BRAND["orange"]),
                              ("disabled", BRAND["surface_alt"])],
                  foreground=[("disabled", BRAND["cool_grey"])])

        # Segment chips under the Preview panel's video (see
        # _render_segment_buttons): plain TButton for "not currently
        # playing", this style for whichever one is -- Sky is already
        # the guide's designated hover/active accent, so this reads as
        # "selected" without inventing a new color.
        style.configure("SegmentActive.TButton", background=BRAND["sky"],
                         foreground=BRAND["white"], font=self.body_font_bold,
                         padding=6, borderwidth=0, focuscolor=BRAND["sky"])
        style.map("SegmentActive.TButton",
                  background=[("active", BRAND["blair_blue"])])

        style.configure("TEntry", fieldbackground=BRAND["surface_alt"],
                         foreground=BRAND["text"], insertcolor=BRAND["text"],
                         bordercolor=BRAND["border"])
        style.configure("TSpinbox", fieldbackground=BRAND["surface_alt"],
                         foreground=BRAND["text"], insertcolor=BRAND["text"],
                         arrowcolor=BRAND["sky"], bordercolor=BRAND["border"])
        style.configure("TCheckbutton", background=BRAND["bg"],
                         foreground=BRAND["text"], font=self.body_font)
        style.configure("TRadiobutton", background=BRAND["bg"],
                         foreground=BRAND["text"], font=self.body_font)
        # As with the Treeview rows, "clam" otherwise shades the whole
        # label whenever the cursor passes over a checkbutton/radiobutton
        # ("active" state) even though nothing was clicked. Mapping
        # "active" back to the normal background/foreground cancels
        # that hover effect for options like "All scored clips", "Top
        # N", "Score above", the energy checkbox, and "Score"/"Clip
        # Name" -- only an actual selection changes their appearance.
        style.map("TCheckbutton",
                  background=[("active", BRAND["bg"])],
                  foreground=[("active", BRAND["text"]),
                              ("disabled", BRAND["cool_grey"])])
        style.map("TRadiobutton",
                  background=[("active", BRAND["bg"])],
                  foreground=[("active", BRAND["text"]),
                              ("disabled", BRAND["cool_grey"])])

        style.configure("Treeview", background=BRAND["surface"],
                         fieldbackground=BRAND["surface"],
                         foreground=BRAND["text"],
                         rowheight=40, font=self.body_font,
                         bordercolor=BRAND["border"])
        style.configure("Treeview.Heading", background=BRAND["dark_blue"],
                         foreground=BRAND["white"], font=self.body_font_bold,
                         relief="flat")
        style.map("Treeview.Heading", background=[("active", BRAND["blair_blue"])])
        # Only the actively-selected row should stand out. ttk's "clam"
        # theme otherwise tints/underlines whichever row the mouse
        # happens to be hovering over ("active" state) even when it
        # isn't selected -- mapping "active" back to the normal
        # surface/text colors here cancels that hover effect, so text
        # doesn't shift in appearance just because the cursor passed
        # over it.
        style.map("Treeview",
                  background=[("selected", BRAND["orange"]),
                              ("active", BRAND["surface"])],
                  foreground=[("selected", BRAND["bg"]),
                              ("active", BRAND["text"])])

        style.configure("TProgressbar", background=BRAND["orange"],
                         troughcolor=BRAND["surface_alt"],
                         bordercolor=BRAND["surface_alt"])
        style.configure("TScrollbar", background=BRAND["surface_alt"],
                         troughcolor=BRAND["bg"],
                         arrowcolor=BRAND["text"])

    # ------------------------------------------------------------------
    def _build_ui(self):
        header = ttk.Frame(self, style="Header.TFrame", padding=(14, 10))
        header.pack(fill="x")

        if self._seal_photo is not None:
            # No fill/expand -- pack's default center anchor vertically
            # centers this against the header's full height automatically.
            ttk.Label(header, image=self._seal_photo, background=BRAND["blair_blue"]
                      ).pack(side="left", padx=(0, 12))

        # Title + subtitle stacked in their own frame (rather than side
        # by side on one baseline, which is what previously caused the
        # subtitle to look mismatched against the title -- two labels
        # of very different font sizes packed with side="left" and an
        # inconsistent per-label pady don't share a visual baseline).
        # This frame sizes itself to exactly fit its two children, then
        # gets centered against the header's height the same way the
        # logo does, so logo and text block align as a pair regardless
        # of which one is taller.
        title_block = ttk.Frame(header, style="Header.TFrame")
        title_block.pack(side="left")
        ttk.Label(title_block, text="B-Roll Analyzer", style="Header.TLabel"
                  ).pack(side="top", anchor="w")
        ttk.Label(title_block, text="Technical clip scoring & Premiere export \u2014 Blair Academy",
                  background=BRAND["blair_blue"], foreground=BRAND["cool_grey"],
                  font=self.small_font).pack(side="top", anchor="w", pady=(2, 0))

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="B-Roll Folder:").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.folder_path)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="Browse...", command=self._choose_folder).pack(side="left")
        self.analyze_btn = ttk.Button(top, text="Analyze", command=self._start_analysis)
        self.analyze_btn.pack(side="left", padx=(6, 0))
        self.cancel_btn = ttk.Button(top, text="Cancel", command=self._cancel_analysis,
                                      state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))

        options = ttk.Frame(self, padding=(10, 0))
        options.pack(fill="x")
        ttk.Label(options, text="Best-segment length (sec):").pack(side="left")
        self.window_sec = tk.DoubleVar(value=4.0)
        ttk.Spinbox(options, from_=1, to=30, increment=1, width=5,
                    textvariable=self.window_sec).pack(side="left", padx=(4, 16))

        ttk.Label(options, text="Segments per clip:").pack(side="left")
        self.max_segments = tk.IntVar(value=1)
        ttk.Spinbox(options, from_=1, to=10, increment=1, width=5,
                    textvariable=self.max_segments).pack(side="left", padx=(4, 16))

        ttk.Label(options, text="Include top:").pack(side="left")
        self.top_mode = tk.StringVar(value="all")
        ttk.Radiobutton(options, text="All scored clips", variable=self.top_mode,
                         value="all").pack(side="left")
        ttk.Radiobutton(options, text="Top N:", variable=self.top_mode,
                         value="topn").pack(side="left", padx=(8, 2))
        self.top_n = tk.IntVar(value=10)
        ttk.Spinbox(options, from_=1, to=200, width=5,
                    textvariable=self.top_n).pack(side="left")
        ttk.Radiobutton(options, text="Score above:", variable=self.top_mode,
                         value="threshold").pack(side="left", padx=(12, 2))
        self.min_score = tk.DoubleVar(value=60.0)
        ttk.Spinbox(options, from_=0, to=100, width=5,
                    textvariable=self.min_score).pack(side="left")
        # Live-updates row tinting as the threshold changes (see
        # _retag_rows) -- registered here, but only ever fires once the
        # spinbox is actually touched, by which point the table/tree
        # this callback references already exists.
        self.min_score.trace_add("write", self._retag_rows)

        # Analysis is CPU-bound and per-file independent, so clips are
        # decoded/scored across multiple processes in parallel (see
        # _run_analysis). Default to all-but-one CPU core; the user can
        # dial it down if they want to keep the machine free for other
        # work, or if enabling energy scoring below (each worker process
        # loads its own copy of the CLIP model, so more workers there
        # means more RAM/VRAM used, not just more CPU).
        parallel_row = ttk.Frame(self, padding=(10, 0))
        parallel_row.pack(fill="x")
        ttk.Label(parallel_row, text="Parallel workers:").pack(side="left")
        cpu_count = os.cpu_count() or 4
        default_workers = max(1, cpu_count - 1)
        self.max_workers = tk.IntVar(value=default_workers)
        ttk.Spinbox(parallel_row, from_=1, to=max(1, cpu_count), width=5,
                    textvariable=self.max_workers).pack(side="left", padx=(4, 6))
        ttk.Label(parallel_row, text=f"(this machine has {cpu_count} CPU core(s))",
                  style="Muted.TLabel", font=self.small_font).pack(side="left")

        # Optional local vision model ("high energy / exciting shot" detection).
        # Fully local (CLIP via open_clip) -- no cloud/Anthropic API involved.
        energy_row = ttk.Frame(self, padding=(10, 4))
        energy_row.pack(fill="x")
        self._energy_available = bool(vision_energy and vision_energy.is_available())
        self.enable_energy = tk.BooleanVar(value=False)
        energy_check = ttk.Checkbutton(
            energy_row, text="Detect high-energy / exciting shots (local CLIP model, slower)",
            variable=self.enable_energy, command=self._on_energy_toggle,
            state="normal" if self._energy_available else "disabled")
        energy_check.pack(side="left")
        if not self._energy_available:
            ttk.Label(energy_row, text="  (requires: pip install torch open_clip_torch pillow)",
                      style="Muted.TLabel", font=self.small_font).pack(side="left")
        ttk.Label(energy_row, text="   Energy weight:").pack(side="left", padx=(12, 2))
        self.energy_weight_pct = tk.IntVar(value=35)
        self.energy_weight_spin = ttk.Spinbox(
            energy_row, from_=0, to=100, width=5, textvariable=self.energy_weight_pct,
            state="disabled")
        self.energy_weight_spin.pack(side="left")
        ttk.Label(energy_row, text="% (0 = ignore energy, 100 = energy only)",
                  style="Muted.TLabel", font=self.small_font).pack(side="left", padx=(4, 0))

        order_row = ttk.Frame(self, padding=(10, 4))
        order_row.pack(fill="x")
        ttk.Label(order_row, text="Sequence clips by:").pack(side="left")
        self.sequence_order = tk.StringVar(value="score")
        ttk.Radiobutton(order_row, text="Score (best first)", variable=self.sequence_order,
                         value="score").pack(side="left", padx=(6, 8))
        ttk.Radiobutton(order_row, text="Clip Name (A-Z)", variable=self.sequence_order,
                         value="name").pack(side="left")

        # Results table
        table_frame = ttk.Frame(self, padding=10)
        table_frame.pack(fill="both", expand=True)

        ttk.Label(table_frame, text="Click \"Clip\", \"Score\", or \"Energy\" column headers to sort."
                                     "  Select a row to preview its best segment.",
                  style="Muted.TLabel", font=self.small_font).pack(anchor="w")
        ttk.Label(table_frame, text="Click a \u25b6 time range -- in the Segments column here, "
                                     "or under the video on the left -- to play just that "
                                     "segment in the Preview panel.",
                  style="Muted.TLabel", font=self.small_font).pack(anchor="w")
        ttk.Label(table_frame, text="Rows tinted red score below the \"Score above\" value; "
                                     "yellow means energy scoring wasn't available for that clip.",
                  style="Muted.TLabel", font=self.small_font).pack(anchor="w", pady=(0, 4))

        content_row = ttk.Frame(table_frame)
        content_row.pack(fill="both", expand=True)

        # Preview panel: shows a larger still of the currently selected
        # clip, with its info and clickable segment times laid out below
        # the image, or -- when a segment time is clicked (here or in the
        # table) -- plays that segment's frames right here. Purely a
        # local, in-memory display either way: images/frames are decoded
        # straight from the clip's own file or cached thumbnail bytes
        # and never touch the network or the exported XML.
        preview_panel = ttk.Frame(content_row, width=340, padding=(0, 0, 10, 0))
        preview_panel.pack(side="left", fill="y")
        preview_panel.pack_propagate(False)
        ttk.Label(preview_panel, text="Preview", font=self.heading_font_small
                  ).pack(anchor="w", pady=(0, 6))
        self.preview_image_label = tk.Label(
            preview_panel, background=BRAND["surface"], foreground=BRAND["cool_grey"],
            text="Select a clip\nto preview", font=self.small_font,
            width=24, height=8)
        self.preview_image_label.pack(fill="x")

        # Info block, directly under the video/thumbnail: clip name,
        # score/energy/duration, then which segment is currently shown.
        info_frame = ttk.Frame(preview_panel)
        info_frame.pack(fill="x", pady=(8, 0))
        self.preview_title_label = ttk.Label(info_frame, text="", font=self.body_font_bold,
                                              wraplength=320, justify="left")
        self.preview_title_label.pack(anchor="w")
        self.preview_meta_label = ttk.Label(info_frame, text="", style="Muted.TLabel",
                                             font=self.small_font, wraplength=320, justify="left")
        self.preview_meta_label.pack(anchor="w", pady=(2, 0))
        self.preview_segment_label = ttk.Label(info_frame, text="", style="Muted.TLabel",
                                                font=self.small_font, wraplength=320, justify="left")
        self.preview_segment_label.pack(anchor="w", pady=(2, 0))

        # Clickable segment times for the selected clip, right under the
        # info block -- the same segments shown in the table's Segments
        # column, so a clip can be previewed without touching the table
        # at all. Rebuilt by _render_segment_buttons whenever the
        # selected row (or its segments) change.
        ttk.Label(preview_panel, text="Segments", style="Muted.TLabel",
                  font=self.small_font).pack(anchor="w", pady=(10, 2))
        self.preview_segments_frame = ttk.Frame(preview_panel)
        self.preview_segments_frame.pack(fill="x")

        # Only shown while a segment is actively playing (or just finished)
        # in the panel above -- packed/unpacked in _play_segment_inline and
        # _end_segment_preview_mode rather than existing permanently, so
        # the plain thumbnail view (the common case) doesn't carry unused
        # buttons.
        preview_controls = ttk.Frame(preview_panel)
        self.preview_replay_btn = ttk.Button(preview_controls, text="\u21ba Replay",
                                              command=self._replay_active_segment)
        self.preview_replay_btn.pack(side="left")
        self.preview_thumbnail_btn = ttk.Button(preview_controls, text="Show Thumbnail",
                                                 command=self._show_static_thumbnail_again)
        self.preview_thumbnail_btn.pack(side="left", padx=(6, 0))
        self._preview_controls_frame = preview_controls

        tree_container = ttk.Frame(content_row)
        tree_container.pack(side="left", fill="both", expand=True)

        columns = ("rank", "name", "score", "energy", "duration", "best_segment")
        # show="tree headings" adds the implicit "#0" column, used here
        # purely to carry each row's small thumbnail icon (see
        # _render_table) -- it has no text/heading of its own.
        self.tree = ttk.Treeview(tree_container, columns=columns, show="tree headings")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=58, minwidth=58, stretch=False, anchor="center")
        self._base_headings = {"rank": "#", "name": "Clip", "score": "Score",
                                "energy": "Energy", "duration": "Duration (s)",
                                "best_segment": "Segments"}
        widths = {"rank": 40, "name": 260, "score": 70, "energy": 70,
                  "duration": 100, "best_segment": 250}
        self.sort_column = "score"
        self.sort_reverse = True
        self._ok_results: list[ClipResult] = []
        # Treeview column ids for the `columns=` tuple are "#1".."#N" in
        # the order given (with "#0" reserved for the implicit tree/icon
        # column) -- computed here, rather than hardcoded, so this stays
        # correct if the column order above ever changes.
        self._segment_col_id = f"#{columns.index('best_segment') + 1}"
        for col in columns:
            if col in ("name", "score", "energy"):
                self.tree.heading(col, text=self._base_headings[col],
                                   command=lambda c=col: self._on_sort_heading_click(c))
            else:
                self.tree.heading(col, text=self._base_headings[col])
            self.tree.column(col, width=widths[col], anchor="center" if col != "name" else "w")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_row_select)
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self.tree.bind("<Motion>", self._on_tree_motion, add="+")
        self.tree.bind("<Leave>", lambda _e: self.tree.configure(cursor=""), add="+")

        # Status tags applied per-row in _render_table/_retag_rows. Text
        # stays the theme's normal color -- only the background tints,
        # so the row reads as "flagged" without sacrificing legibility.
        # low_score checked first below (tag_configure priority follows
        # insertion order when tags overlap, but a row is only ever
        # given one of these two in practice -- see _row_tags_for).
        self.tree.tag_configure("low_score", background=BRAND["red_row"],
                                 foreground=BRAND["text"])
        self.tree.tag_configure("energy_fallback", background=BRAND["yellow_row"],
                                 foreground=BRAND["text"])

        scrollbar = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        # Bottom bar
        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.export_btn = ttk.Button(bottom, text="Export Premiere XML...",
                                      command=self._export_xml, state="disabled")
        self.export_btn.pack(side="right")
        self.issues_btn = ttk.Button(bottom, text="View Issues", command=self._show_issues_dialog,
                                      state="disabled")
        self.issues_btn.pack(side="right", padx=(0, 8))

        status_bar = ttk.Label(self, textvariable=self.status_text, anchor="w",
                                relief="flat", padding=(8, 5),
                                background=BRAND["surface"],
                                foreground=BRAND["text"], font=self.small_font)
        status_bar.pack(fill="x", side="bottom")

    # ------------------------------------------------------------------
    def _load_persisted_settings(self):
        """Apply any settings saved from a previous run. Best-effort and
        silent: a missing/corrupt file just leaves the defaults already
        set by _build_ui in place. Only known, already-existing option
        variables are touched -- this can't introduce new state."""
        saved = app_settings.load_settings()
        if not saved:
            return
        try:
            if "folder_path" in saved and os.path.isdir(saved["folder_path"]):
                self.folder_path.set(saved["folder_path"])
            if "window_sec" in saved:
                self.window_sec.set(float(saved["window_sec"]))
            if "max_segments" in saved:
                self.max_segments.set(int(saved["max_segments"]))
            if "top_mode" in saved and saved["top_mode"] in ("all", "topn", "threshold"):
                self.top_mode.set(saved["top_mode"])
            if "top_n" in saved:
                self.top_n.set(int(saved["top_n"]))
            if "min_score" in saved:
                self.min_score.set(float(saved["min_score"]))
            if "max_workers" in saved:
                cpu_count = os.cpu_count() or 4
                self.max_workers.set(max(1, min(int(saved["max_workers"]), cpu_count)))
            if "enable_energy" in saved and self._energy_available:
                self.enable_energy.set(bool(saved["enable_energy"]))
                self._on_energy_toggle()
            if "energy_weight_pct" in saved:
                self.energy_weight_pct.set(int(saved["energy_weight_pct"]))
            if "sequence_order" in saved and saved["sequence_order"] in ("score", "name"):
                self.sequence_order.set(saved["sequence_order"])
        except (TypeError, ValueError, tk.TclError):
            pass  # a malformed value just leaves that one field at its default

    def _current_settings_dict(self):
        return {
            "folder_path": self.folder_path.get(),
            "window_sec": self.window_sec.get(),
            "max_segments": self.max_segments.get(),
            "top_mode": self.top_mode.get(),
            "top_n": self.top_n.get(),
            "min_score": self.min_score.get(),
            "max_workers": self.max_workers.get(),
            "enable_energy": self.enable_energy.get(),
            "energy_weight_pct": self.energy_weight_pct.get(),
            "sequence_order": self.sequence_order.get(),
        }

    def _on_close(self):
        self._segment_generation += 1  # stop any in-flight segment-preview thread/poll loop
        try:
            app_settings.save_settings(self._current_settings_dict())
        except Exception:
            pass  # never block quitting on a settings-save problem
        self.destroy()

    # ------------------------------------------------------------------
    def _choose_folder(self):
        path = filedialog.askdirectory(title="Select B-Roll Folder")
        if path:
            self.folder_path.set(path)

    def _on_energy_toggle(self):
        self.energy_weight_spin.config(
            state="normal" if self.enable_energy.get() else "disabled")

    def _start_analysis(self):
        folder = self.folder_path.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Invalid Folder", "Please choose a valid folder first.")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            return

        try:
            self.window_sec.get()
            self.max_segments.get()
            self.top_n.get()
            self.min_score.get()
            self.max_workers.get()
            self.energy_weight_pct.get()
        except tk.TclError:
            messagebox.showerror(
                "Invalid Setting",
                "One of the option fields (best-segment length, segments per "
                "clip, top N, score threshold, parallel workers, or energy "
                "weight) isn't a valid number. Please check those fields and "
                "try again.")
            return

        files = find_video_files(folder)
        if not files:
            messagebox.showinfo("No Clips Found",
                                 "No video files were found in that folder.")
            return

        for row in self.tree.get_children():
            self.tree.delete(row)
        self.results = []
        self._ok_results = []
        self._rendered_results = []
        self._preview_generation += 1
        self._show_preview(None, None)
        self.export_btn.config(state="disabled")
        self.analyze_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._cancel_event.clear()
        self.progress["value"] = 0
        self._analyzed_folder = folder
        self._failed_results = []
        self._energy_failed_results = []
        self.issues_btn.config(state="disabled", text="View Issues", style="TButton")
        self.status_text.set(f"Found {len(files)} clip(s). Starting analysis...")

        self.worker_thread = threading.Thread(target=self._run_analysis, args=(files, folder), daemon=True)
        self.worker_thread.start()

    def _cancel_analysis(self):
        """Stop an in-progress analysis run. This only sets a flag --
        it deliberately never touches self._executor directly. Calling
        executor.shutdown()/cancelling futures from the main thread
        while the background analysis thread is concurrently blocked
        inside a wait on those same futures can hang the process pool
        (confirmed: this was the original bug -- Cancel silently did
        nothing because the two threads deadlocked around the
        executor's internal state). Instead, _run_analysis itself
        polls this flag from the same thread that owns the executor
        and reacts to it there, which is safe.

        Clips already queued but not yet dispatched to a worker are
        dropped within about 0.2s; a clip already mid-decode is left
        to finish (there's no clean way to interrupt a native decode
        call part-way through), so a short delay after clicking Cancel
        is normal. Whatever finished before cancelling still gets
        shown and cached."""
        if not (self.worker_thread and self.worker_thread.is_alive()):
            return
        self._cancel_event.set()
        self.cancel_btn.config(state="disabled")
        self.status_text.set("Cancelling... finishing clip(s) already in progress.")

    def _run_analysis(self, files, folder):
        total = len(files)
        results = [None] * total
        fingerprints = [None] * total
        completed = 0

        window_sec = self.window_sec.get()
        max_segments = max(1, int(self.max_segments.get()))
        enable_energy = self.enable_energy.get()
        energy_weight = max(0.0, min(1.0, self.energy_weight_pct.get() / 100.0))

        # --- Cache pass: reuse any clip whose file hasn't changed and
        # whose cached samples already include energy scoring if that's
        # being requested now. Re-scoring a cache hit for the current
        # window/segment/energy-weight settings is cheap (no decode),
        # so it's done synchronously here rather than farmed out to a
        # worker process.
        cache_entries = result_cache.load_cache(folder)
        to_submit = []
        cache_hits = 0
        for i, path in enumerate(files):
            fp = result_cache.file_fingerprint(path)
            fingerprints[i] = fp
            rel = os.path.relpath(path, folder)
            entry = cache_entries.get(rel)
            if result_cache.is_entry_usable(entry, fp, need_energy=enable_energy):
                try:
                    result = result_cache.result_from_entry(path, entry)
                    rescore_clip(result, window_sec=window_sec, max_segments=max_segments,
                                 energy_weight=energy_weight, enable_energy=enable_energy)
                    results[i] = result
                    cache_hits += 1
                    completed += 1
                    continue
                except Exception:
                    pass  # any problem reusing the entry -> fall through to re-analysis
            to_submit.append(i)

        if cache_hits:
            self.after(0, lambda: self._set_progress(
                completed / total * 100,
                f"Loaded {cache_hits}/{total} clip(s) from cache..."))

        # --- Parallel pass: everything not served from cache.
        if to_submit and not self._cancel_event.is_set():
            num_workers = max(1, min(int(self.max_workers.get()), len(to_submit)))
            self.after(0, lambda: self.status_text.set(
                f"Analyzing {len(to_submit)} clip(s) with {num_workers} parallel worker(s)..."))

            self._executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=num_workers, initializer=_worker_init)
            try:
                # Only ever submit up to `num_workers` clips at a time,
                # rather than handing the executor the whole list
                # upfront. This keeps every not-yet-dispatched clip as
                # a plain Python list entry (trivially "cancellable" --
                # it's simply never submitted) instead of a Future
                # already queued inside the executor, which is what
                # lets Cancel drop the remaining queue instantly rather
                # than waiting for everything already handed off.
                submit_queue = list(to_submit)
                pending = {}  # future -> file index

                def submit_next():
                    while submit_queue and len(pending) < num_workers:
                        i = submit_queue.pop(0)
                        fut = self._executor.submit(
                            _analyze_clip_worker, files[i], window_sec,
                            max_segments, enable_energy, energy_weight)
                        pending[fut] = i

                submit_next()
                while pending:
                    # Poll with a short timeout (rather than blocking
                    # indefinitely on as_completed) so this loop -- and
                    # only this loop, on this thread -- notices a
                    # Cancel click within a fraction of a second and
                    # reacts to it itself, instead of a different
                    # thread reaching into the executor concurrently.
                    done, _ = concurrent.futures.wait(
                        pending.keys(), timeout=0.2,
                        return_when=concurrent.futures.FIRST_COMPLETED)

                    if self._cancel_event.is_set():
                        submit_queue.clear()
                        for fut in list(pending.keys()):
                            if fut not in done:
                                fut.cancel()  # no-op if already running; fine either way

                    for future in done:
                        i = pending.pop(future)
                        path = files[i]
                        try:
                            result = future.result()
                        except concurrent.futures.CancelledError:
                            continue  # dropped before it started; leave as None
                        except Exception as e:
                            # Belt-and-suspenders: _analyze_clip_worker
                            # already catches analyze_clip's own
                            # exceptions, but a worker process could
                            # still die/crash outright (e.g. segfault in
                            # a native codec library), which surfaces
                            # here as a BrokenProcessPool-style error.
                            result = ClipResult(path=path, filename=os.path.basename(path),
                                                 duration=0, fps=0, width=0, height=0,
                                                 error=f"{e}\n{traceback.format_exc(limit=1)}")
                        results[i] = result
                        completed += 1
                        pct = completed / total * 100
                        self.after(0, lambda p=pct, c=completed, path=path: self._set_progress(
                            p, f"Analyzed ({c}/{total}): {os.path.basename(path)}"))

                    if not self._cancel_event.is_set():
                        submit_next()
            finally:
                self._executor.shutdown(wait=True)
                self._executor = None

        cancelled = self._cancel_event.is_set()

        # --- Save a fresh cache for everything that analyzed cleanly
        # (skip errors, so a file that failed this run -- e.g. locked,
        # briefly unreadable -- still gets retried next time instead of
        # being remembered as permanently broken).
        new_entries = {}
        for i, r in enumerate(results):
            if r is None or r.error:
                continue
            fp = fingerprints[i]
            if fp is None:
                continue
            rel = os.path.relpath(files[i], folder)
            new_entries[rel] = result_cache.entry_from_result(r, fp)
        result_cache.save_cache(folder, new_entries)

        final_results = [r for r in results if r is not None]
        final_results.sort(key=lambda r: r.overall_score, reverse=True)
        self.after(0, lambda: self._on_analysis_complete(
            final_results, cancelled=cancelled, cache_hits=cache_hits, total=total,
            enable_energy=enable_energy))

    def _set_progress(self, value, status):
        self.progress["value"] = value
        if status:
            self.status_text.set(status)

    def _on_analysis_complete(self, results, cancelled=False, cache_hits=0, total=None,
                               enable_energy=False):
        self.results = results
        # Captured from the run that actually produced `results`, not read
        # live from the checkbox: the checkbox isn't disabled while a run
        # is in progress, so toggling it mid-run must not change how this
        # run's own results are judged/displayed after the fact.
        self._last_enable_energy = enable_energy
        self._ok_results = [r for r in results if not r.error]
        failed = [r for r in results if r.error]
        energy_failed = [r for r in self._ok_results
                         if enable_energy and not r.energy_enabled and r.energy_error]
        self._failed_results = failed
        self._energy_failed_results = energy_failed

        self._render_table()

        if total is None:
            total = len(results)
        if cancelled:
            msg = f"Cancelled. {len(self._ok_results)}/{total} clip(s) analyzed before stopping."
        else:
            msg = f"Analyzed {len(self._ok_results)} clip(s)."
            if cache_hits:
                msg += f" ({cache_hits} loaded from cache.)"
        if failed:
            msg += f" {len(failed)} failed."
            for f in failed:
                print(f"FAILED: {f.filename}: {f.error}")
        if energy_failed:
            msg += f" Energy scoring unavailable for {len(energy_failed)} clip(s)."
            print(f"Energy scoring issue: {energy_failed[0].energy_error}")

        issue_count = len(failed) + len(energy_failed)
        if issue_count:
            # Still logged to the console too (handy if the app is being
            # run from a terminal), but the button is what makes this
            # visible to anyone who isn't watching one.
            self.issues_btn.config(state="normal", style="Issues.TButton",
                                    text=f"View Issues ({issue_count})")
        else:
            self.issues_btn.config(state="disabled", style="TButton", text="View Issues")

        self.status_text.set(msg)
        self.analyze_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if self._ok_results:
            self.export_btn.config(state="normal")

    def _show_issues_dialog(self):
        """A simple, read-only window listing every clip that failed to
        analyze outright, plus every clip where energy scoring couldn't
        run even though it was requested -- so this information doesn't
        live only in a terminal the person may not be watching."""
        if not self._failed_results and not self._energy_failed_results:
            return

        win = tk.Toplevel(self)
        win.title("Analysis Issues")
        win.configure(background=BRAND["bg"])
        win.geometry("560x420")
        win.minsize(420, 260)
        win.transient(self)

        header = ttk.Frame(win, padding=(12, 10))
        header.pack(fill="x")
        ttk.Label(header, text="Analysis Issues", font=self.heading_font_small).pack(anchor="w")

        text_frame = ttk.Frame(win, padding=(12, 0, 12, 8))
        text_frame.pack(fill="both", expand=True)
        text = tk.Text(text_frame, wrap="word", background=BRAND["surface"],
                        foreground=BRAND["text"], insertbackground=BRAND["text"],
                        borderwidth=0, font=self.body_font)
        text.pack(side="left", fill="both", expand=True)
        text_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text_scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=text_scroll.set)

        text.tag_configure("section", font=self.body_font_bold, foreground=BRAND["white"])
        text.tag_configure("failed_name", font=self.body_font_bold, foreground=BRAND["red"])
        text.tag_configure("warning_name", font=self.body_font_bold, foreground=BRAND["yellow"])
        text.tag_configure("detail", foreground=BRAND["cool_grey"])

        if self._failed_results:
            text.insert("end", f"Failed to analyze ({len(self._failed_results)})\n", "section")
            for r in self._failed_results:
                text.insert("end", f"{r.filename}\n", "failed_name")
                # Only the first line of `error` is the human-readable
                # message; anything after is a truncated traceback kept
                # for the console log, not worth showing here.
                reason = (r.error or "Unknown error").splitlines()[0]
                text.insert("end", f"  {reason}\n\n", "detail")

        if self._energy_failed_results:
            text.insert("end", f"Energy scoring unavailable ({len(self._energy_failed_results)})\n",
                        "section")
            for r in self._energy_failed_results:
                text.insert("end", f"{r.filename}\n", "warning_name")
                reason = (r.energy_error or "Unknown reason").splitlines()[0]
                text.insert("end", f"  {reason}\n"
                                    f"  (Technical score is still valid; only the energy "
                                    f"dimension is missing for this clip.)\n\n", "detail")

        text.configure(state="disabled")

        footer = ttk.Frame(win, padding=(12, 0, 12, 12))
        footer.pack(fill="x")
        ttk.Button(footer, text="Close", command=win.destroy).pack(side="right")

    def _on_sort_heading_click(self, col):
        if self.sort_column == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = col
            # Sensible default direction per column: scores/energy
            # high-to-low, names A-to-Z.
            self.sort_reverse = (col in ("score", "energy"))
        self._render_table()

    def _render_table(self):
        self._end_segment_preview_mode()
        for row in self.tree.get_children():
            self.tree.delete(row)

        if self.sort_column == "name":
            key_fn = lambda r: r.filename.lower()
        elif self.sort_column == "energy":
            key_fn = lambda r: r.mean_energy_score if self._energy_active(r) else -1.0
        else:  # "score"
            key_fn = lambda r: r.overall_score
        ordered = sorted(self._ok_results, key=key_fn, reverse=self.sort_reverse)
        self._rendered_results = ordered

        for col in ("name", "score", "energy"):
            base = self._base_headings[col]
            if self.sort_column == col:
                arrow = " \u25bc" if self.sort_reverse else " \u25b2"
                self.tree.heading(col, text=base + arrow)
            else:
                self.tree.heading(col, text=base)

        # Fresh list each render -- the previous render's items were all
        # just deleted above, so nothing still needs the old PhotoImages
        # kept alive; replacing the list lets them be garbage collected.
        row_thumbnails = []
        for rank, r in enumerate(ordered, start=1):
            if r.segments:
                seg = "   ".join(f"\u25b6 {s.start:.1f}-{s.end:.1f}s" for s in r.segments)
            else:
                seg = f"\u25b6 {r.best_window_start:.1f}-{r.best_window_end:.1f}s"
            energy_display = f"{r.mean_energy_score:.0f}" if self._energy_active(r) else "-"
            icon = self._make_row_icon(r)
            if icon is not None:
                row_thumbnails.append(icon)
            self.tree.insert("", "end", image=icon if icon is not None else "", values=(
                rank, r.filename, f"{r.overall_score:.1f}", energy_display,
                f"{r.duration:.1f}", seg), tags=self._row_tags_for(r))
        self._row_thumbnails = row_thumbnails

    def _energy_active(self, r: ClipResult) -> bool:
        """Whether an energy score should actually be shown for `r`:
        both the clip's own samples must carry energy data AND the run
        that produced the currently-displayed results must have wanted
        energy applied. Without the second half, a clip served from a
        cache entry computed with energy scoring on would keep showing
        its old energy score/column even after unchecking "Detect
        high-energy" and re-running Analyze on an unchanged folder --
        the checkbox would look like it did nothing."""
        return bool(r.energy_enabled and self._last_enable_energy)

    def _row_tags_for(self, r: ClipResult) -> tuple:
        """Which status tag (if any) a row should carry. A low score is
        treated as the more consequential flag, so it takes precedence
        over the energy-fallback tag if a clip somehow triggers both --
        a row only ever carries one tint, not a blend of two."""
        try:
            threshold = self.min_score.get()
        except tk.TclError:
            threshold = None
        if threshold is not None and r.overall_score < threshold:
            return ("low_score",)
        if r.energy_error:
            # r.energy_error is only ever set when energy scoring was
            # actually requested for this clip's own analysis run (see
            # analyze_clip) and didn't fully succeed -- independent of
            # whatever the "Detect high-energy" checkbox happens to be
            # set to right now, so toggling it afterward doesn't cause
            # already-computed rows to flicker their tag on and off.
            return ("energy_fallback",)
        return ()

    def _retag_rows(self, *_args):
        """Lightweight re-tint: updates just the tag on each existing
        row without rebuilding thumbnails or re-inserting items. Bound
        to the "Score above" threshold so dragging that spinbox gives
        immediate visual feedback on which clips are currently under
        it, without waiting for another Analyze run."""
        for iid, r in zip(self.tree.get_children(), self._rendered_results):
            self.tree.item(iid, tags=self._row_tags_for(r))

    def _make_row_icon(self, result: ClipResult):
        """Small (~48px-wide) PhotoImage for the row's #0 column, or None
        if the clip has no thumbnail (e.g. capture failed) -- Treeview
        just shows no icon for that row in that case."""
        if not result.thumbnail_jpeg:
            return None
        ppm = thumbnail_ppm_bytes(result.thumbnail_jpeg, max_dim=48)
        if ppm is None:
            return None
        try:
            return tk.PhotoImage(data=ppm)
        except tk.TclError:
            return None

    def _on_row_select(self, _event=None):
        self._preview_generation += 1
        sel = self.tree.selection()
        if not sel:
            self._end_segment_preview_mode()
            self._show_preview(None, None)
            return
        iid = sel[0]
        if iid == self._preview_mode_iid:
            # This selection event was caused by our own segment-time
            # click handling (see _on_tree_click) selecting this same
            # row -- leave the segment playback already underway alone
            # instead of resetting the panel back to the static thumbnail.
            return
        self._end_segment_preview_mode()
        try:
            idx = self.tree.index(iid)
        except tk.TclError:
            self._show_preview(None, None)
            return
        if 0 <= idx < len(self._rendered_results):
            self._show_preview(self._rendered_results[idx], iid)
        else:
            self._show_preview(None, None)

    def _end_segment_preview_mode(self):
        """Stop any inline segment playback and hide its Replay/Show
        Thumbnail controls, without touching the preview image itself --
        callers follow this with _show_preview (or a fresh segment
        preview) to decide what actually appears next."""
        self._preview_mode_iid = None
        self._active_segment = None
        self._segment_generation += 1  # invalidates the in-flight decode thread/poll loop
        self._preview_controls_frame.pack_forget()
        self._render_segment_buttons(None)

    def _show_preview(self, result: Optional[ClipResult], iid: Optional[str]):
        """Update the preview panel for the selected clip.

        Displays whatever thumbnail the clip already has immediately
        (instant, no I/O). If that thumbnail is stale for the clip's
        *current* best segment -- e.g. window length or segments-per-
        clip changed since it was captured -- a background thread
        re-seeks just this one file and swaps in the refreshed image
        once it's ready. This is the only place a thumbnail gets
        refreshed after the initial analysis: doing it here, on
        selection, means the cost scales with how many clips someone
        actually looks at, not with library size (see
        analyzer.refresh_thumbnail's docstring for why rescoring itself
        deliberately doesn't do this for every cached clip at once).
        """
        generation = self._preview_generation
        self._render_segment_buttons(result)
        if result is None or not result.thumbnail_jpeg:
            self._preview_photo = None
            self.preview_image_label.config(
                image="", text="Select a clip\nto preview" if result is None
                      else "No preview\navailable", width=24, height=8)
            self.preview_title_label.config(text="")
            self.preview_meta_label.config(text="")
            self.preview_segment_label.config(text="")
        else:
            self._display_thumbnail_now(result)

        if result is not None and thumbnail_is_stale(result):
            def worker():
                changed = refresh_thumbnail(result)
                if changed:
                    # Only persist to the folder's on-disk cache if this
                    # refresh is still current -- a superseded refresh
                    # (a new selection, or a new Analyze run, started
                    # since this thread began) must not overwrite the
                    # cache with a stale snapshot, e.g. clobbering a
                    # fresh Analyze run's just-saved results with an
                    # older folder-wide cache read-modify-write.
                    if generation == self._preview_generation:
                        self._persist_refreshed_thumbnail(result)
                    self.after(0, lambda: self._on_thumbnail_refreshed(
                        result, iid, generation))
            threading.Thread(target=worker, daemon=True).start()

    def _persist_refreshed_thumbnail(self, result: ClipResult):
        """Write a just-refreshed thumbnail back into that folder's
        on-disk cache entry immediately, so the work of selecting this
        row survives an app restart or another Analyze run instead of
        reverting to the stale thumbnail that was cached before the
        refresh (see result_cache.update_thumbnail's docstring). Runs
        on the background thread that already did the seek -- this is
        a small JSON patch, not a new class of I/O for that thread.
        Best-effort: any problem here just means the refresh stays
        in-memory-only for this session, same as before this existed."""
        if not self._analyzed_folder:
            return
        try:
            rel_path = os.path.relpath(result.path, self._analyzed_folder)
            result_cache.update_thumbnail(
                self._analyzed_folder, rel_path,
                result.thumbnail_jpeg, result.thumbnail_time)
        except (OSError, ValueError):
            pass

    def _on_thumbnail_refreshed(self, result: ClipResult, iid: Optional[str], generation: int):
        # Ignore results from a stale request -- the selection moved on
        # (or the table was re-rendered) while the seek was in flight.
        if generation != self._preview_generation:
            return
        self._display_thumbnail_now(result)
        if iid is not None and self.tree.exists(iid):
            icon = self._make_row_icon(result)
            if icon is not None:
                self._row_thumbnails.append(icon)  # keep alive alongside the others
                self.tree.item(iid, image=icon)

    def _display_thumbnail_now(self, result: ClipResult):
        """Decode result's current thumbnail bytes straight into the
        preview panel. No file I/O -- purely an in-memory image decode
        of bytes already on the ClipResult."""
        ppm = thumbnail_ppm_bytes(result.thumbnail_jpeg, max_dim=200)
        if ppm is None:
            self._preview_photo = None
            self.preview_image_label.config(image="", text="No preview\navailable",
                                             width=24, height=8)
            self.preview_title_label.config(text="")
            self.preview_meta_label.config(text="")
            self.preview_segment_label.config(text="")
            return
        try:
            photo = tk.PhotoImage(data=ppm)
        except tk.TclError:
            self._preview_photo = None
            self.preview_image_label.config(image="", text="No preview\navailable",
                                             width=24, height=8)
            self.preview_title_label.config(text="")
            self.preview_meta_label.config(text="")
            self.preview_segment_label.config(text="")
            return
        self._preview_photo = photo  # keep a reference alive
        self.preview_image_label.config(image=photo, text="", width=0, height=0)
        energy_text = f"{result.mean_energy_score:.0f}/100" if self._energy_active(result) else "-"
        self.preview_title_label.config(text=result.filename)
        self.preview_meta_label.config(
            text=f"Score: {result.overall_score:.1f}/100   Energy: {energy_text}   "
                 f"Duration: {result.duration:.1f}s")
        self.preview_segment_label.config(
            text=f"Best segment: {result.best_window_start:.1f}s "
                 f"\u2013 {result.best_window_end:.1f}s")

    # ------------------------------------------------------------------
    # Segment-time click-to-preview: clicking a "\u25b6 start-end" time
    # range in the Segments column plays just that in/out range right in
    # the Preview panel (to the right of the table), reusing the same
    # image widget the static thumbnail already uses. Playback reads
    # frames straight from the already-selected local source file (via
    # analyzer.open_segment_capture / read_next_segment_frame) -- no
    # network access, no subprocess/shell calls, and nothing written to
    # disk, matching the existing thumbnail feature's local-only footprint.
    EMBEDDED_SEGMENT_MAX_DIM = 300  # fits the 340px-wide Preview panel without a scrollbar

    def _on_tree_motion(self, event):
        """Hand cursor over a clickable segment-time cell; default
        cursor everywhere else in the table."""
        if (self.tree.identify_region(event.x, event.y) == "cell"
                and self.tree.identify_column(event.x) == self._segment_col_id
                and self.tree.identify_row(event.y)):
            self.tree.configure(cursor="hand2")
        else:
            self.tree.configure(cursor="")

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != self._segment_col_id:
            return
        row_iid = self.tree.identify_row(event.y)
        if not row_iid:
            return
        try:
            idx = self.tree.index(row_iid)
        except tk.TclError:
            return
        if not (0 <= idx < len(self._rendered_results)):
            return
        result = self._rendered_results[idx]
        segments = result.segments or [
            Segment(result.best_window_start, result.best_window_end, result.overall_score)]
        if len(segments) == 1:
            self._select_row_for_segment(row_iid)
            self._play_segment_inline(result, segments[0])
            return
        bbox = self.tree.bbox(row_iid, self._segment_col_id)
        if not bbox:
            return
        seg_idx = self._segment_index_at_x(event.x - bbox[0], bbox[2], segments)
        if seg_idx is not None:
            self._select_row_for_segment(row_iid)
            self._play_segment_inline(result, segments[seg_idx])

    def _select_row_for_segment(self, row_iid):
        """Mark `row_iid` as "already showing a segment preview" before
        (possibly) selecting it, so _on_row_select -- whether it fires
        synchronously as part of selection_set below or slightly later --
        recognizes this as its own doing and leaves the panel alone
        instead of resetting it to the static thumbnail right after
        _play_segment_inline sets up the preview."""
        self._preview_mode_iid = row_iid
        if row_iid not in self.tree.selection():
            self.tree.selection_set(row_iid)

    def _segment_index_at_x(self, local_x, cell_width, segments):
        """Which segment (by index) the given x offset within the
        Segments cell falls on, approximating the same center-anchored
        layout Treeview uses to draw the cell text. Only needed for
        multi-segment rows -- a single-segment row is handled without
        this, since the whole cell is unambiguously that one segment."""
        seg_font = tkfont.Font(font=self.body_font)
        sep = "   "
        parts = [f"\u25b6 {s.start:.1f}-{s.end:.1f}s" for s in segments]
        widths = [seg_font.measure(p) for p in parts]
        sep_width = seg_font.measure(sep)
        total_width = sum(widths) + sep_width * (len(parts) - 1)
        x = max(0, (cell_width - total_width) // 2)
        for i, w in enumerate(widths):
            if x <= local_x <= x + w:
                return i
            x += w + sep_width
        return None

    def _play_segment_inline(self, result: ClipResult, segment: Segment):
        if not os.path.isfile(result.path):
            messagebox.showerror("Preview Unavailable",
                                  f"Couldn't find the source file:\n{result.path}")
            return
        self._active_segment = (result, segment)
        energy_text = f"{result.mean_energy_score:.0f}/100" if self._energy_active(result) else "-"
        self.preview_title_label.config(text=result.filename)
        self.preview_meta_label.config(
            text=f"Score: {result.overall_score:.1f}/100   Energy: {energy_text}   "
                 f"Duration: {result.duration:.1f}s")
        self.preview_segment_label.config(
            text=f"Playing: {segment.start:.1f}s \u2013 {segment.end:.1f}s")
        self._render_segment_buttons(result, active_segment=segment)
        self._preview_controls_frame.pack(anchor="w", pady=(10, 0))
        self._segment_generation += 1
        generation = self._segment_generation
        self._start_segment_playback(result.path, segment.start, segment.end, generation)

    def _render_segment_buttons(self, result: Optional[ClipResult],
                                 active_segment: Optional[Segment] = None):
        """(Re)build the row(s) of clickable "\u25b6 start-end" segment
        chips under the Preview panel's info block for `result` -- the
        same segments shown in the table's Segments column, so a clip
        can be scrubbed through without touching the table. Wraps to a
        new row every few chips so it stays readable at the panel's
        fixed width regardless of how many segments a clip has."""
        for child in self.preview_segments_frame.winfo_children():
            child.destroy()
        if result is None:
            return
        segments = result.segments or [
            Segment(result.best_window_start, result.best_window_end, result.overall_score)]
        per_row = 3
        row = None
        for i, seg in enumerate(segments):
            if i % per_row == 0:
                row = ttk.Frame(self.preview_segments_frame)
                row.pack(fill="x", pady=(0, 4))
            is_active = active_segment is not None and seg is active_segment
            btn = ttk.Button(row, text=f"\u25b6 {seg.start:.1f}-{seg.end:.1f}s",
                              style="SegmentActive.TButton" if is_active else "TButton",
                              command=lambda r=result, s=seg: self._on_segment_button_click(r, s))
            btn.pack(side="left", padx=(0, 4))

    def _on_segment_button_click(self, result: ClipResult, segment: Segment):
        iid = self._iid_for_result(result)
        if iid is not None:
            self._select_row_for_segment(iid)
        self._play_segment_inline(result, segment)

    def _iid_for_result(self, result: ClipResult) -> Optional[str]:
        """The Treeview row iid currently showing `result`, or None if
        it's not in the currently rendered table (e.g. the table was
        re-sorted/re-rendered between building the segment chips and
        clicking one)."""
        try:
            idx = self._rendered_results.index(result)
        except ValueError:
            return None
        children = self.tree.get_children()
        if 0 <= idx < len(children):
            return children[idx]
        return None

    def _replay_active_segment(self):
        if not self._active_segment:
            return
        result, segment = self._active_segment
        self._segment_generation += 1
        generation = self._segment_generation
        self._start_segment_playback(result.path, segment.start, segment.end, generation)

    def _show_static_thumbnail_again(self):
        """Leaves segment-preview mode and reverts the panel to the
        currently selected row's ordinary thumbnail (or the empty-state
        message if nothing's selected)."""
        self._end_segment_preview_mode()
        sel = self.tree.selection()
        if sel:
            try:
                idx = self.tree.index(sel[0])
            except tk.TclError:
                idx = -1
            if 0 <= idx < len(self._rendered_results):
                self._show_preview(self._rendered_results[idx], sel[0])
                return
        self._show_preview(None, None)

    def _start_segment_playback(self, path: str, start: float, end: float, generation: int):
        """Kick off (or restart, for Replay) inline segment playback.
        `generation` is captured by the decode thread and poll loop so a
        superseded run (new selection, new segment clicked, Replay
        pressed again, or app close) stops touching the UI/queue as soon
        as it notices -- see _segment_generation's docstring in __init__."""
        if generation != self._segment_generation:
            return
        self._preview_photo = None
        self.preview_image_label.config(image="", text="Loading preview\u2026",
                                         width=24, height=8)
        frame_queue = queue.Queue(maxsize=8)
        self._segment_frame_queue = frame_queue
        threading.Thread(target=self._segment_decode_worker,
                          args=(path, start, end, generation, frame_queue), daemon=True).start()
        self.after(20, lambda: self._poll_segment_queue(generation, frame_queue))

    def _segment_decode_worker(self, path: str, start: float, end: float,
                                generation: int, frame_queue: "queue.Queue"):
        """Runs on a background thread: opens+seeks the file, then
        streams frames into `frame_queue` for the main thread to display.
        Reads/decodes only -- no writes, no network, no subprocess calls.
        Capped at a generous frame count so a malformed/very long segment
        can't stream indefinitely."""
        cap, fps = open_segment_capture(path, start)
        if cap is None:
            frame_queue.put(("error", None))
            return
        frame_queue.put(("fps", fps))
        try:
            frames_sent = 0
            max_frames = 3000  # generous headroom over the 30s max segment length at high fps
            while frames_sent < max_frames:
                if generation != self._segment_generation:
                    break
                ppm = read_next_segment_frame(cap, end, max_dim=self.EMBEDDED_SEGMENT_MAX_DIM)
                if ppm is None:
                    break
                # Blocks with a timeout (rather than forever) purely so
                # this thread notices it's been superseded and exits
                # promptly, instead of hanging on a full queue nobody is
                # draining anymore.
                while generation == self._segment_generation:
                    try:
                        frame_queue.put(("frame", ppm), timeout=0.5)
                        break
                    except queue.Full:
                        continue
                if generation != self._segment_generation:
                    break
                frames_sent += 1
        finally:
            cap.release()
        frame_queue.put(("done", None))

    def _poll_segment_queue(self, generation: int, frame_queue: "queue.Queue"):
        if generation != self._segment_generation:
            return  # superseded -- a newer selection/segment/replay owns the panel now
        try:
            kind, payload = frame_queue.get_nowait()
        except queue.Empty:
            self.after(20, lambda: self._poll_segment_queue(generation, frame_queue))
            return
        if kind == "error":
            self.preview_image_label.config(image="", text="Couldn't open this clip\n"
                                                             "for preview.", width=24, height=8)
            return
        if kind == "fps":
            fps = payload or 24.0
            self._segment_frame_delay_ms = max(15, int(1000.0 / fps))
            self.after(1, lambda: self._poll_segment_queue(generation, frame_queue))
            return
        if kind == "frame":
            try:
                photo = tk.PhotoImage(data=payload)
            except tk.TclError:
                self.after(1, lambda: self._poll_segment_queue(generation, frame_queue))
                return
            self._preview_photo = photo  # keep alive -- Tk would otherwise GC it immediately
            self.preview_image_label.config(image=photo, text="", width=0, height=0)
            self.after(self._segment_frame_delay_ms,
                       lambda: self._poll_segment_queue(generation, frame_queue))
            return
        # "done": last frame stays on screen; Replay starts it again from the top.

    # ------------------------------------------------------------------
    def _selected_results(self):
        ok_results = [r for r in self.results if not r.error]
        ok_results.sort(key=lambda r: r.overall_score, reverse=True)
        mode = self.top_mode.get()
        if mode == "topn":
            return ok_results[: max(1, int(self.top_n.get()))]
        if mode == "threshold":
            return [r for r in ok_results if r.overall_score >= self.min_score.get()]
        return ok_results

    def _export_xml(self):
        selected = self._selected_results()
        if not selected:
            messagebox.showinfo("Nothing to Export",
                                 "No clips meet the current selection criteria.")
            return

        if self.sequence_order.get() == "name":
            selected = sorted(selected, key=lambda r: r.filename.lower())
        else:
            selected = sorted(selected, key=lambda r: r.overall_score, reverse=True)

        out_path = filedialog.asksaveasfilename(
            title="Save Premiere Pro XML",
            defaultextension=".xml",
            filetypes=[("Final Cut Pro XML", "*.xml")],
            initialfile="broll_selects.xml",
        )
        if not out_path:
            return
        try:
            export_xml(selected, out_path, show_energy=self._last_enable_energy)
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))
            return
        messagebox.showinfo(
            "Export Complete",
            f"Exported {len(selected)} clip(s) to:\n{out_path}\n\n"
            "In Premiere Pro: File > Import... and select this XML.")


if __name__ == "__main__":
    app = BRollAnalyzerApp()
    app.mainloop()
