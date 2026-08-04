"""
app.py — Blair Academy Title & Motion Graphics Creator

A local desktop application (Tkinter — ships with Python, no browser
involved) for building brand-compliant title cards and short motion
graphics for video editing, then exporting them as a transparent PNG or
a transparent-background video clip to drop into Premiere / DaVinci /
Final Cut / CapCut etc.

RUN:
    python3 app.py

REQUIREMENTS:
    - Python 3.9+
    - Pillow  (pip install pillow --break-system-packages   # if needed)
    - ffmpeg installed and on PATH, only required for video export
      (free / open-source: https://ffmpeg.org/download.html)

Everything renders and exports locally. No accounts, no API keys, no
data ever leaves this machine. This build is sanctioned for internal
Blair Academy Communications use — all logos/seals are cleared for use.
"""

import os
import io
import json
import copy
import base64
import time
import threading
import traceback
import tkinter as tk
from tkinter import ttk, colorchooser, filedialog, messagebox

from PIL import Image, ImageTk

import brand
import renderer
import export
import prompt_ai
import project_io
from timeline import Timeline

PREVIEW_MAX_W = 900
PREVIEW_MAX_H = 620
HISTORY_LIMIT = 60
HISTORY_DEBOUNCE_MS = 700

RECENT_PROJECTS_PATH = os.path.join(brand.OUTPUT_DIR, ".recent_projects.json")
RECENT_PROJECTS_LIMIT = 12
RECENT_THUMB_WIDTH = 160


def default_scene():
    preset = brand.PRESETS[brand.DEFAULT_PRESET]
    scene = {
        "title": "COMMENCEMENT 2026",
        "subtitle": "Blair Academy",
        "logo_color_mode": "original",
        "logo_custom_color": "#ffffff",
        "logo_height": 160,
        "logo_opacity": 100,
        "logo_grow": False,
        "logo_arrangement": "back",
        "logo_key_white_bg": False,
        "title_size": 130,
        "subtitle_size": 46,
        "transparent_bg": True,
        "hold_seconds": 1.0,
        "canvas_size": brand.CANVAS_PRESETS[brand.DEFAULT_CANVAS_PRESET],
        "canvas_preset_name": brand.DEFAULT_CANVAS_PRESET,
        "layout": brand.DEFAULT_LAYOUT,
        "background_style": "Solid",
        "divider": True,
        "bg_gradient_color": None,
        "shadow_enabled": False,
        "shadow_color": "#000000",
        "shadow_opacity": 60,
        "shadow_blur": 8,
        "shadow_offset_x": 4,
        "shadow_offset_y": 4,
        "vignette": 0,
        "vignette_shape": brand.DEFAULT_VIGNETTE_SHAPE,
        "title_in_start": 0.0,
        "title_in_end": 0.45,
        "subtitle_in_start": 0.30,
        "subtitle_in_end": 0.70,
        "logo_in_start": 0.55,
        "logo_in_end": 0.95,
        "outro_animation": "none",
        "title_out_start": 0.80,
        "title_out_end": 1.0,
        "subtitle_out_start": 0.78,
        "subtitle_out_end": 0.98,
        "logo_out_start": 0.82,
        "logo_out_end": 1.0,
        "lower_third_position": brand.DEFAULT_LOWER_THIRD_POSITION,
        "lower_third_scale": 1.0,
        "lower_third_bg_color": None,
        "lower_third_bg_opacity": 75,
        "text_offset_x": 0,
        "text_offset_y": 0,
    }
    scene.update(preset)
    return scene


def _fit_width(img, target_w):
    w, h = img.size
    if w == 0:
        return img
    scale = target_w / w
    return img.resize((target_w, max(1, int(h * scale))), Image.LANCZOS)


def load_recent_projects():
    """Load the recent-projects list (most-recent-first). Tolerant of a
    missing/corrupt file — recent-projects tracking is a nice-to-have and
    should never block the app from starting or opening/saving a project."""
    try:
        with open(RECENT_PROJECTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_recent_projects(entries):
    os.makedirs(brand.OUTPUT_DIR, exist_ok=True)
    with open(RECENT_PROJECTS_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def _make_thumbnail_b64(scene):
    still = renderer.render_still(scene)
    thumb = _fit_width(still.convert("RGBA"), RECENT_THUMB_WIDTH)
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def record_recent_project(path, scene):
    """Add/move `path` to the front of the recent-projects list, embedding a
    small thumbnail. Best-effort: any failure here (thumbnail render, disk
    write, etc.) must never block or crash an actual save/open, so callers
    should wrap this in try/except."""
    abs_path = os.path.abspath(path)
    entries = [e for e in load_recent_projects() if e.get("path") != abs_path]
    entries.insert(0, {
        "path": abs_path,
        "name": os.path.splitext(os.path.basename(abs_path))[0],
        "last_touched": time.time(),
        "thumbnail_b64": _make_thumbnail_b64(scene),
    })
    save_recent_projects(entries[:RECENT_PROJECTS_LIMIT])


def prune_recent_project(path):
    """Remove a recent entry (e.g. because its file no longer exists)."""
    try:
        abs_path = os.path.abspath(path)
        entries = [e for e in load_recent_projects() if e.get("path") != abs_path]
        save_recent_projects(entries)
    except Exception:
        pass


def _format_last_touched(ts):
    try:
        return time.strftime("%b %d, %Y %I:%M %p", time.localtime(ts))
    except Exception:
        return ""


def make_checkerboard(w, h, box=16):
    img = Image.new("RGB", (w, h), (58, 58, 58))
    px = img.load()
    for y in range(h):
        for x in range(w):
            if (x // box + y // box) % 2 == 0:
                px[x, y] = (44, 44, 44)
    return img


class BlairTitleApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Blair Academy — Title & Motion Graphics Creator — Untitled")
        self.root.geometry("1760x1080")
        self.root.minsize(1360, 860)
        self.root.configure(bg=brand.UI_DARK_BG)

        self.scene = default_scene()
        self.scrub_t = 1.0
        self.project_path = None
        self._preview_job = None
        self._history_job = None
        self._syncing = False
        self._restoring = False
        self._checker_cache = {}
        self.preview_max_w = PREVIEW_MAX_W
        self.preview_max_h = PREVIEW_MAX_H

        self.is_playing = False
        self._play_after_id = None
        self._play_start_wallclock = None
        self._play_total_seconds = 1.0

        self._history = [copy.deepcopy(self.scene)]
        self._history_index = 0

        self._build_menu()
        self._build_layout()
        self._sync_controls_from_scene()
        self.schedule_preview()
        self._update_undo_redo_state()

        self.root.bind_all("<Control-z>", lambda e: self.undo())
        self.root.bind_all("<Control-Z>", lambda e: self.undo())
        self.root.bind_all("<Control-y>", lambda e: self.redo())
        self.root.bind_all("<Control-Shift-Z>", lambda e: self.redo())
        self.root.bind_all("<Control-n>", lambda e: self.new_project())
        self.root.bind_all("<Control-o>", lambda e: self.open_project())
        self.root.bind_all("<Control-s>", lambda e: self.save_project())
        self.root.bind_all("<Control-Shift-S>", lambda e: self.save_project_as())
        self.root.bind_all("<space>", self._on_space_key)
        # ttk.Button / Checkbutton / OptionMenu(Menubutton) all have their own
        # built-in <space> action (invoke / toggle / post the dropdown). Since
        # Tk runs the widget's own class binding *and then* the "all" binding
        # for the same keypress, whichever of those controls last had mouse
        # focus (very often the Play/Pause button itself, right after being
        # clicked) would fire its own action AND our play/pause toggle in the
        # same keystroke — toggling play state twice and appearing to do
        # nothing. Route space through our single handler for these classes
        # instead so it can't double-fire.
        for widget_class in ("TButton", "TCheckbutton", "TMenubutton", "Button"):
            self.root.bind_class(widget_class, "<space>", self._on_space_key)

    # ------------------------------------------------------------------
    # Menu bar
    # ------------------------------------------------------------------
    def _build_menu(self):
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="New Project", command=self.new_project, accelerator="Ctrl+N")
        file_menu.add_command(label="Open Project…", command=self.open_project, accelerator="Ctrl+O")
        self.open_recent_menu = tk.Menu(file_menu, tearoff=0, postcommand=self._populate_open_recent_menu)
        file_menu.add_cascade(label="Open Recent", menu=self.open_recent_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Save Project", command=self.save_project, accelerator="Ctrl+S")
        file_menu.add_command(label="Save Project As…", command=self.save_project_as, accelerator="Ctrl+Shift+S")
        menubar.add_cascade(label="File", menu=file_menu)
        self.root.config(menu=menubar)

    def _populate_open_recent_menu(self):
        menu = self.open_recent_menu
        menu.delete(0, "end")
        entries = load_recent_projects()
        if not entries:
            menu.add_command(label="(No recent projects)", state="disabled")
            return
        for entry in entries:
            path = entry.get("path", "")
            name = entry.get("name") or os.path.splitext(os.path.basename(path))[0]
            menu.add_command(label=name, command=lambda p=path: self.open_recent_project(p))

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_layout(self):
        outer = ttk.Frame(self.root, padding=10, style="Dark.TFrame")
        outer.pack(fill="both", expand=True)
        left = ttk.Frame(outer, style="Dark.TFrame")
        left.pack(side="left", fill="y", padx=(0, 10))
        right = ttk.Frame(outer, style="Dark.TFrame")
        right.pack(side="left", fill="both", expand=True)
        self._build_controls(left)
        self._build_preview(right)

    def _build_controls(self, parent):
        container = tk.Canvas(parent, width=400, highlightthickness=0, bg=brand.UI_DARK_BG)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=container.yview)
        scroll_frame = ttk.Frame(container, style="Dark.TFrame")
        scroll_frame.bind("<Configure>", lambda e: container.configure(scrollregion=container.bbox("all")))
        container.create_window((0, 0), window=scroll_frame, anchor="nw", width=400)
        container.configure(yscrollcommand=scrollbar.set)
        container.pack(side="left", fill="y")
        scrollbar.pack(side="left", fill="y")

        f = scroll_frame
        self.color_swatches = {}

        project_toolbar = ttk.Frame(f, style="Dark.TFrame")
        project_toolbar.pack(fill="x", pady=(0, 6), padx=2)
        ttk.Button(project_toolbar, text="New", command=self.new_project).pack(side="left", padx=(0, 4))
        ttk.Button(project_toolbar, text="Open…", command=self.open_project).pack(side="left", padx=4)
        ttk.Button(project_toolbar, text="Save", command=self.save_project).pack(side="left", padx=4)
        ttk.Button(project_toolbar, text="Recent…", command=self.show_recent_projects_dialog).pack(side="left", padx=4)

        toolbar = ttk.Frame(f, style="Dark.TFrame")
        toolbar.pack(fill="x", pady=(0, 10), padx=2)
        self.undo_btn = ttk.Button(toolbar, text="\u21b6 Undo", command=self.undo)
        self.undo_btn.pack(side="left", padx=(0, 6))
        self.redo_btn = ttk.Button(toolbar, text="\u21b7 Redo", command=self.redo)
        self.redo_btn.pack(side="left")
        ttk.Label(toolbar, text="  (Ctrl+Z / Ctrl+Y)", style="Dim.TLabel").pack(side="left")

        box = ttk.LabelFrame(f, text="AI Design Prompt  (local, offline \u2014 no data leaves this computer)")
        box.pack(fill="x", pady=(0, 10), padx=2)
        self.prompt_var = tk.StringVar()
        entry = ttk.Entry(box, textvariable=self.prompt_var)
        entry.pack(fill="x", padx=8, pady=(8, 4))
        entry.bind("<Return>", lambda e: self.apply_prompt())
        ttk.Button(box, text="Apply Prompt", command=self.apply_prompt).pack(padx=8, pady=(0, 8), anchor="e")
        ttk.Label(
            box, wraplength=370, style="Dim.TLabel",
            text='e.g. "pop upbeat, square format, red gradient background, circular vignette, '
                 'bounce in, lower third" \u2014 set text with quotes: title: "Welcome Home"'
        ).pack(fill="x", padx=8, pady=(0, 8))

        pf = ttk.LabelFrame(f, text="Style Preset")
        pf.pack(fill="x", pady=6, padx=2)
        self.preset_var = tk.StringVar(value=brand.DEFAULT_PRESET)
        ttk.OptionMenu(pf, self.preset_var, brand.DEFAULT_PRESET,
                       *brand.PRESETS.keys(), command=self.apply_preset).pack(fill="x", padx=8, pady=8)

        fmt = ttk.LabelFrame(f, text="Format")
        fmt.pack(fill="x", pady=6, padx=2)
        ttk.Label(fmt, text="Aspect ratio").pack(anchor="w", padx=8)
        self.aspect_var = tk.StringVar()
        ttk.OptionMenu(fmt, self.aspect_var, brand.DEFAULT_CANVAS_PRESET,
                       *brand.CANVAS_PRESETS.keys(), command=lambda v: self.on_aspect_change(v)
                       ).pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(fmt, text="Layout").pack(anchor="w", padx=8)
        self.layout_var = tk.StringVar()
        ttk.OptionMenu(fmt, self.layout_var, brand.DEFAULT_LAYOUT, *brand.LAYOUTS,
                       command=lambda v: self.on_layout_change(v)).pack(fill="x", padx=8, pady=(0, 6))

        self.lower_third_controls = ttk.Frame(fmt, style="Dark.TFrame")
        ttk.Label(self.lower_third_controls, text="Lower third position").pack(anchor="w", padx=8)
        self.lt_position_var = tk.StringVar()
        ttk.OptionMenu(self.lower_third_controls, self.lt_position_var, brand.DEFAULT_LOWER_THIRD_POSITION,
                       *brand.LOWER_THIRD_POSITIONS, command=lambda v: self.on_change()
                       ).pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(self.lower_third_controls, text="Lower third scale").pack(anchor="w", padx=8)
        self.lt_scale_var = tk.IntVar(value=100)
        ltsrow = ttk.Frame(self.lower_third_controls, style="Dark.TFrame")
        ltsrow.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Scale(ltsrow, from_=50, to=180, variable=self.lt_scale_var,
                  command=lambda v: self.on_change()).pack(side="left", fill="x", expand=True)
        self.lt_scale_label = ttk.Label(ltsrow, text="100%", width=5)
        self.lt_scale_label.pack(side="left", padx=6)

        ltbgrow = ttk.Frame(self.lower_third_controls, style="Dark.TFrame")
        ltbgrow.pack(fill="x", padx=8, pady=4)
        ttk.Label(ltbgrow, text="Plate color", width=16).pack(side="left")
        self.lt_bg_swatch = tk.Canvas(ltbgrow, width=28, height=20, highlightthickness=1, highlightbackground="#666")
        self.lt_bg_swatch.pack(side="left", padx=6)
        ttk.Button(ltbgrow, text="Brand color\u2026",
                   command=lambda: self.pick_brand_color("lower_third_bg_color", self.lt_bg_swatch)).pack(side="left")
        self.color_swatches["lower_third_bg_color"] = self.lt_bg_swatch

        ttk.Label(self.lower_third_controls, text="Plate opacity").pack(anchor="w", padx=8)
        self.lt_bg_opacity_var = tk.IntVar(value=75)
        ltoprow = ttk.Frame(self.lower_third_controls, style="Dark.TFrame")
        ltoprow.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Scale(ltoprow, from_=0, to=100, variable=self.lt_bg_opacity_var,
                  command=lambda v: self.on_change()).pack(side="left", fill="x", expand=True)
        self.lt_bg_opacity_label = ttk.Label(ltoprow, text="75%", width=5)
        self.lt_bg_opacity_label.pack(side="left", padx=6)
        # packed/unpacked dynamically depending on the selected layout

        tf = ttk.LabelFrame(f, text="Text")
        tf.pack(fill="x", pady=6, padx=2)
        ttk.Label(tf, text="Title").pack(anchor="w", padx=8)
        self.title_var = tk.StringVar()
        ttk.Entry(tf, textvariable=self.title_var).pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(tf, text="Subtitle").pack(anchor="w", padx=8)
        self.subtitle_var = tk.StringVar()
        ttk.Entry(tf, textvariable=self.subtitle_var).pack(fill="x", padx=8, pady=(0, 6))
        self.uppercase_var = tk.BooleanVar()
        ttk.Checkbutton(tf, text="ALL CAPS title", variable=self.uppercase_var,
                         command=self.on_change).pack(anchor="w", padx=8, pady=(0, 8))
        for var in (self.title_var, self.subtitle_var):
            var.trace_add("write", lambda *a: self.on_change())

        ff = ttk.LabelFrame(f, text="Typography (brand-safe substitutes \u2014 see README)")
        ff.pack(fill="x", pady=6, padx=2)
        ttk.Label(ff, text="Title font").pack(anchor="w", padx=8)
        self.title_font_var = tk.StringVar()
        ttk.OptionMenu(ff, self.title_font_var, list(brand.FONTS.keys())[0],
                       *brand.FONTS.keys(), command=lambda v: self.on_change()).pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(ff, text="Subtitle font").pack(anchor="w", padx=8)
        self.subtitle_font_var = tk.StringVar()
        ttk.OptionMenu(ff, self.subtitle_font_var, list(brand.FONTS.keys())[0],
                       *brand.FONTS.keys(), command=lambda v: self.on_change()).pack(fill="x", padx=8, pady=(0, 8))

        cf = ttk.LabelFrame(f, text="Brand Colors & Background")
        cf.pack(fill="x", pady=6, padx=2)
        for label, key in [("Background", "bg_color"), ("Accent / Divider", "accent_color"),
                            ("Text", "text_color")]:
            row = ttk.Frame(cf, style="Dark.TFrame")
            row.pack(fill="x", padx=8, pady=4)
            ttk.Label(row, text=label, width=16).pack(side="left")
            swatch = tk.Canvas(row, width=28, height=20, highlightthickness=1, highlightbackground="#666")
            swatch.pack(side="left", padx=6)
            ttk.Button(row, text="Brand color\u2026", command=lambda k=key, s=swatch: self.pick_brand_color(k, s)).pack(side="left")
            self.color_swatches[key] = swatch

        self.contrast_warning_var = tk.StringVar(value="")
        self.contrast_warning_label = ttk.Label(cf, textvariable=self.contrast_warning_var,
                                                  style="Warning.TLabel", wraplength=340)
        # Packed/hidden dynamically by _update_contrast_warning() — blank/hidden
        # when contrast is fine so it doesn't waste vertical space.

        self.divider_var = tk.BooleanVar()
        self.divider_checkbutton = ttk.Checkbutton(cf, text="Show accent divider between title & subtitle",
                         variable=self.divider_var, command=self.on_change)
        self.divider_checkbutton.pack(anchor="w", padx=8, pady=(0, 4))

        self.transparent_var = tk.BooleanVar()
        ttk.Checkbutton(cf, text="Transparent background (recommended for overlays)",
                         variable=self.transparent_var, command=self.on_change).pack(anchor="w", padx=8, pady=(4, 4))

        ttk.Label(cf, text="Background style (used when not transparent)").pack(anchor="w", padx=8)
        self.bg_style_var = tk.StringVar()
        ttk.OptionMenu(cf, self.bg_style_var, "Solid", *brand.BACKGROUND_STYLES,
                       command=lambda v: self.on_bg_style_change(v)).pack(fill="x", padx=8, pady=(0, 6))

        self.gradient_row = ttk.Frame(cf, style="Dark.TFrame")
        ttk.Label(self.gradient_row, text="Gradient to", width=16).pack(side="left")
        self.gradient_swatch = tk.Canvas(self.gradient_row, width=28, height=20, highlightthickness=1, highlightbackground="#666")
        self.gradient_swatch.pack(side="left", padx=6)
        ttk.Button(self.gradient_row, text="Brand color\u2026",
                   command=lambda: self.pick_brand_color("bg_gradient_color", self.gradient_swatch)).pack(side="left")
        self.color_swatches["bg_gradient_color"] = self.gradient_swatch

        ttk.Label(cf, text="Vignette (darkens edges)").pack(anchor="w", padx=8, pady=(6, 0))
        self.vignette_var = tk.IntVar(value=0)
        vrow = ttk.Frame(cf, style="Dark.TFrame")
        vrow.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Scale(vrow, from_=0, to=100, variable=self.vignette_var,
                  command=lambda v: self.on_change()).pack(side="left", fill="x", expand=True)
        self.vignette_label = ttk.Label(vrow, text="0%", width=5)
        self.vignette_label.pack(side="left", padx=6)

        ttk.Label(cf, text="Vignette shape").pack(anchor="w", padx=8)
        self.vignette_shape_var = tk.StringVar()
        ttk.OptionMenu(cf, self.vignette_shape_var, brand.DEFAULT_VIGNETTE_SHAPE, *brand.VIGNETTE_SHAPES,
                       command=lambda v: self.on_change()).pack(fill="x", padx=8, pady=(0, 8))

        sf = ttk.LabelFrame(f, text="Drop Shadow (title, subtitle & logo)")
        sf.pack(fill="x", pady=6, padx=2)
        self.shadow_enabled_var = tk.BooleanVar()
        ttk.Checkbutton(sf, text="Enable drop shadow", variable=self.shadow_enabled_var,
                         command=self.on_shadow_enabled_change).pack(anchor="w", padx=8, pady=(4, 6))

        self.shadow_controls = ttk.Frame(sf, style="Dark.TFrame")

        sdrow = ttk.Frame(self.shadow_controls, style="Dark.TFrame")
        sdrow.pack(fill="x", padx=8, pady=4)
        ttk.Label(sdrow, text="Color", width=16).pack(side="left")
        self.shadow_color_swatch = tk.Canvas(sdrow, width=28, height=20, highlightthickness=1, highlightbackground="#666")
        self.shadow_color_swatch.pack(side="left", padx=6)
        ttk.Button(sdrow, text="Brand color\u2026",
                   command=lambda: self.pick_brand_color("shadow_color", self.shadow_color_swatch)).pack(side="left")
        self.color_swatches["shadow_color"] = self.shadow_color_swatch

        ttk.Label(self.shadow_controls, text="Opacity").pack(anchor="w", padx=8)
        self.shadow_opacity_var = tk.IntVar(value=60)
        sorow = ttk.Frame(self.shadow_controls, style="Dark.TFrame")
        sorow.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Scale(sorow, from_=0, to=100, variable=self.shadow_opacity_var,
                  command=lambda v: self.on_change()).pack(side="left", fill="x", expand=True)
        self.shadow_opacity_label = ttk.Label(sorow, text="60%", width=5)
        self.shadow_opacity_label.pack(side="left", padx=6)

        ttk.Label(self.shadow_controls, text="Blur / softness").pack(anchor="w", padx=8)
        self.shadow_blur_var = tk.IntVar(value=8)
        sbrow = ttk.Frame(self.shadow_controls, style="Dark.TFrame")
        sbrow.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Scale(sbrow, from_=0, to=40, variable=self.shadow_blur_var,
                  command=lambda v: self.on_change()).pack(side="left", fill="x", expand=True)
        self.shadow_blur_label = ttk.Label(sbrow, text="8px", width=5)
        self.shadow_blur_label.pack(side="left", padx=6)

        ttk.Label(self.shadow_controls, text="Offset X").pack(anchor="w", padx=8)
        self.shadow_offset_x_var = tk.IntVar(value=4)
        sxrow = ttk.Frame(self.shadow_controls, style="Dark.TFrame")
        sxrow.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Scale(sxrow, from_=-40, to=40, variable=self.shadow_offset_x_var,
                  command=lambda v: self.on_change()).pack(side="left", fill="x", expand=True)
        self.shadow_offset_x_label = ttk.Label(sxrow, text="4px", width=5)
        self.shadow_offset_x_label.pack(side="left", padx=6)

        ttk.Label(self.shadow_controls, text="Offset Y").pack(anchor="w", padx=8)
        self.shadow_offset_y_var = tk.IntVar(value=4)
        syrow = ttk.Frame(self.shadow_controls, style="Dark.TFrame")
        syrow.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Scale(syrow, from_=-40, to=40, variable=self.shadow_offset_y_var,
                  command=lambda v: self.on_change()).pack(side="left", fill="x", expand=True)
        self.shadow_offset_y_label = ttk.Label(syrow, text="4px", width=5)
        self.shadow_offset_y_label.pack(side="left", padx=6)
        # self.shadow_controls itself is packed/unpacked dynamically by
        # _update_shadow_controls_visibility() depending on shadow_enabled_var,
        # same show/hide pattern as the logo custom-color row.

        lf = ttk.LabelFrame(f, text="Logo / Seal \u2014 Blair-sanctioned, all marks cleared for use")
        lf.pack(fill="x", pady=6, padx=2)
        ttk.Label(lf, text="Mark").pack(anchor="w", padx=8)
        self.logo_var = tk.StringVar()
        logo_options = ["None"] + list(brand.LOGO_SOURCES.keys())
        ttk.OptionMenu(lf, self.logo_var, "None", *logo_options,
                       command=lambda v: self.on_change()).pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(lf, text="Placement").pack(anchor="w", padx=8)
        self.placement_var = tk.StringVar()
        placements = ["top-left", "top-center", "top-right", "center",
                      "bottom-left", "bottom-center", "bottom-right"]
        ttk.OptionMenu(lf, self.placement_var, "bottom-center", *placements,
                       command=lambda v: self.on_change()).pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(lf, text="Size").pack(anchor="w", padx=8)
        self.logo_scale_var = tk.IntVar(value=160)
        lsrow = ttk.Frame(lf, style="Dark.TFrame")
        lsrow.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Scale(lsrow, from_=40, to=900, variable=self.logo_scale_var,
                  command=lambda v: self.on_change()).pack(side="left", fill="x", expand=True)
        self.logo_scale_label = ttk.Label(lsrow, text="160px", width=6)
        self.logo_scale_label.pack(side="left", padx=6)
        ttk.Label(lf, text="Opacity").pack(anchor="w", padx=8)
        self.logo_opacity_var = tk.IntVar(value=100)
        lorow = ttk.Frame(lf, style="Dark.TFrame")
        lorow.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Scale(lorow, from_=0, to=100, variable=self.logo_opacity_var,
                  command=lambda v: self.on_change()).pack(side="left", fill="x", expand=True)
        self.logo_opacity_label = ttk.Label(lorow, text="100%", width=6)
        self.logo_opacity_label.pack(side="left", padx=6)
        ttk.Label(lf, text="Color treatment").pack(anchor="w", padx=8)
        self.logo_color_mode_var = tk.StringVar()
        ttk.OptionMenu(lf, self.logo_color_mode_var, "original", "original", "white", "custom",
                       command=lambda v: self.on_logo_color_mode_change(v)).pack(fill="x", padx=8, pady=(0, 6))
        self.logo_custom_color_row = ttk.Frame(lf, style="Dark.TFrame")
        ttk.Label(self.logo_custom_color_row, text="Custom color", width=16).pack(side="left")
        self.logo_custom_color_swatch = tk.Canvas(self.logo_custom_color_row, width=28, height=20,
                                                    highlightthickness=1, highlightbackground="#666")
        self.logo_custom_color_swatch.pack(side="left", padx=6)
        ttk.Button(self.logo_custom_color_row, text="Brand color…",
                   command=lambda: self.pick_brand_color("logo_custom_color", self.logo_custom_color_swatch)).pack(side="left")
        self.color_swatches["logo_custom_color"] = self.logo_custom_color_swatch
        self.logo_key_white_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(lf, text="Key out white background to transparency",
                        variable=self.logo_key_white_var,
                        command=self.on_change).pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(lf, text=brand.TRADEMARK_NOTICE, wraplength=370, style="Accent.TLabel").pack(fill="x", padx=8, pady=(0, 8))

        af = ttk.LabelFrame(f, text="Animation")
        af.pack(fill="x", pady=6, padx=2)
        ttk.Label(af, text="Entrance style").pack(anchor="w", padx=8)
        self.anim_var = tk.StringVar()
        ttk.OptionMenu(af, self.anim_var, brand.ANIMATIONS[0], *brand.ANIMATIONS,
                       command=lambda v: self.on_change()).pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(af, text="Outro style").pack(anchor="w", padx=8)
        self.outro_var = tk.StringVar()
        ttk.OptionMenu(af, self.outro_var, brand.DEFAULT_OUTRO, *brand.OUTRO_ANIMATIONS,
                       command=lambda v: self.on_change()).pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(af, text="Total duration (seconds)").pack(anchor="w", padx=8)
        self.duration_var = tk.DoubleVar(value=3.0)
        dur_row = ttk.Frame(af, style="Dark.TFrame")
        dur_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Scale(dur_row, from_=1.0, to=10.0, variable=self.duration_var,
                  command=lambda v: self.on_change()).pack(side="left", fill="x", expand=True)
        self.duration_label = ttk.Label(dur_row, text="3.0s", width=5)
        self.duration_label.pack(side="left", padx=6)
        ttk.Label(af, text="Fine-tune exact entrance and exit timing for each element on "
                            "the Timeline below the preview \u2193", wraplength=370, style="Dim.TLabel"
                  ).pack(fill="x", padx=8, pady=(0, 8))

        ef = ttk.LabelFrame(f, text="Export (local files only)")
        ef.pack(fill="x", pady=(6, 20), padx=2)
        ttk.Button(ef, text="Export Still (transparent PNG)", command=self.export_png).pack(fill="x", padx=8, pady=4)
        ttk.Button(ef, text="Export Video \u2014 MOV (alpha, recommended)", command=lambda: self.export_video("mov")).pack(fill="x", padx=8, pady=4)
        ttk.Button(ef, text="Export Video \u2014 WebM (alpha, smaller file)", command=lambda: self.export_video("webm")).pack(fill="x", padx=8, pady=4)
        ttk.Label(ef, text="If your editor shows a black box instead of transparency on a WebM "
                            "import, re-export as MOV \u2014 the alpha data is fine, some tools' "
                            "default decoders just don't read it back.",
                  wraplength=370, style="Dim.TLabel").pack(fill="x", padx=8, pady=(0, 4))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(ef, textvariable=self.status_var, wraplength=370, style="Accent.TLabel").pack(fill="x", padx=8, pady=(4, 8))

    def _build_preview(self, parent):
        ttk.Label(parent, text="Live Preview  (checkerboard = transparent)",
                  style="Heading.TLabel").pack(anchor="w", pady=(0, 6))
        self.preview_frame = ttk.Frame(parent, style="Dark.TFrame")
        self.preview_frame.pack(fill="both", expand=True)
        self.preview_label = tk.Label(self.preview_frame, bd=1, relief="solid",
                                       bg=brand.UI_PANEL_BG, highlightthickness=0)
        self.preview_label.pack(expand=True)

        transport = ttk.Frame(parent, style="Dark.TFrame")
        transport.pack(pady=(16, 4))
        self.play_btn = ttk.Button(transport, text="\u25b6 Play", command=self.toggle_play, width=10)
        self.play_btn.pack(side="left", padx=(0, 6))
        ttk.Button(transport, text="\u23f9 Stop", command=self.stop_play, width=8).pack(side="left", padx=(0, 12))
        self.time_label = ttk.Label(transport, text="0.00s / 3.00s", style="Heading.TLabel")
        self.time_label.pack(side="left", padx=(0, 12))
        self.loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(transport, text="Loop", variable=self.loop_var).pack(side="left", padx=(0, 12))
        ttk.Label(transport, text="(Space to play/pause)", style="Dim.TLabel").pack(side="left")

        ttk.Label(parent, text="Timeline  (drag segment edges to retime \u00b7 drag the axis to scrub \u00b7 double-click to reset)",
                  style="Heading.TLabel").pack(pady=(10, 6))
        timeline_wrap = ttk.Frame(parent, style="Dark.TFrame")
        timeline_wrap.pack(fill="x", expand=False)
        self.timeline = Timeline(timeline_wrap, width=900, get_scene=lambda: self.scene,
                                  on_drag=self._on_timeline_drag, on_scrub=self._on_timeline_scrub,
                                  bg=brand.UI_PANEL_BG_ALT)
        self.timeline.pack(fill="x", expand=True, padx=4)

        # Make the preview area track the actual space available as the
        # window is resized, so it fills unused space instead of staying
        # a fixed small size.
        parent.bind("<Configure>", self._on_preview_area_resize)

    def _on_preview_area_resize(self, event):
        new_w = max(320, event.width - 24)
        new_h = max(240, int(event.height * 0.6))
        if abs(new_w - self.preview_max_w) > 6 or abs(new_h - self.preview_max_h) > 6:
            self.preview_max_w = new_w
            self.preview_max_h = new_h
            self.schedule_preview(fast=True)

    # ------------------------------------------------------------------
    # Sync helpers
    # ------------------------------------------------------------------
    def _sync_controls_from_scene(self):
        self._syncing = True
        try:
            s = self.scene
            self.title_var.set(s["title"])
            self.subtitle_var.set(s["subtitle"])
            self.uppercase_var.set(s["uppercase_title"])
            self.title_font_var.set(s["title_font"])
            self.subtitle_font_var.set(s["subtitle_font"])
            self.logo_var.set(s.get("logo") or "None")
            self.placement_var.set(s.get("logo_placement", "bottom-center"))
            self.logo_color_mode_var.set(s.get("logo_color_mode", "original"))
            self.logo_key_white_var.set(bool(s.get("logo_key_white_bg", False)))
            self.logo_scale_var.set(int(s.get("logo_height", 160)))
            self.logo_opacity_var.set(int(s.get("logo_opacity", 100)))
            self.anim_var.set(s.get("animation", "fade"))
            self.outro_var.set(s.get("outro_animation", "none"))
            self.duration_var.set(s.get("duration", 3.0))
            self.transparent_var.set(s.get("transparent_bg", True))
            self.aspect_var.set(s.get("canvas_preset_name", brand.DEFAULT_CANVAS_PRESET))
            self.layout_var.set(s.get("layout", brand.DEFAULT_LAYOUT))
            self.lt_position_var.set(s.get("lower_third_position", brand.DEFAULT_LOWER_THIRD_POSITION))
            self.lt_scale_var.set(int(round(s.get("lower_third_scale", 1.0) * 100)))
            self.lt_bg_opacity_var.set(int(s.get("lower_third_bg_opacity", 75)))
            self.bg_style_var.set(s.get("background_style", "Solid"))
            self.divider_var.set(bool(s.get("divider", True)))
            self.vignette_var.set(int(s.get("vignette", 0)))
            self.vignette_shape_var.set(s.get("vignette_shape", brand.DEFAULT_VIGNETTE_SHAPE))
            self.shadow_enabled_var.set(bool(s.get("shadow_enabled", False)))
            self.shadow_opacity_var.set(int(s.get("shadow_opacity", 60)))
            self.shadow_blur_var.set(int(s.get("shadow_blur", 8)))
            self.shadow_offset_x_var.set(int(s.get("shadow_offset_x", 4)))
            self.shadow_offset_y_var.set(int(s.get("shadow_offset_y", 4)))
            for key, swatch in self.color_swatches.items():
                if key == "bg_gradient_color" and not s.get("bg_gradient_color"):
                    val = renderer.darken(s.get("bg_color", "#004b8d"), 0.55)
                elif key == "lower_third_bg_color" and not s.get("lower_third_bg_color"):
                    val = s.get("bg_color", "#004b8d")
                else:
                    val = s.get(key) or "#ffffff"
                swatch.configure(bg=val)
            self.duration_label.configure(text=f"{s.get('duration', 3.0):.1f}s")
            self.vignette_label.configure(text=f"{int(s.get('vignette', 0))}%")
            self.logo_scale_label.configure(text=f"{int(s.get('logo_height', 160))}px")
            self.logo_opacity_label.configure(text=f"{int(s.get('logo_opacity', 100))}%")
            self.lt_bg_opacity_label.configure(text=f"{int(s.get('lower_third_bg_opacity', 75))}%")
            self.lt_scale_label.configure(text=f"{int(round(s.get('lower_third_scale', 1.0) * 100))}%")
            self.shadow_opacity_label.configure(text=f"{int(s.get('shadow_opacity', 60))}%")
            self.shadow_blur_label.configure(text=f"{int(s.get('shadow_blur', 8))}px")
            self.shadow_offset_x_label.configure(text=f"{int(s.get('shadow_offset_x', 4))}px")
            self.shadow_offset_y_label.configure(text=f"{int(s.get('shadow_offset_y', 4))}px")
            self._update_gradient_row_visibility()
            self._update_lower_third_controls_visibility()
            self._update_logo_custom_color_visibility()
            self._update_shadow_controls_visibility()
            self._update_contrast_warning()
        finally:
            self._syncing = False
        if hasattr(self, "timeline"):
            self.timeline.redraw()

    def _sync_scene_from_controls(self):
        s = self.scene
        s["title"] = self.title_var.get()
        s["subtitle"] = self.subtitle_var.get()
        s["uppercase_title"] = self.uppercase_var.get()
        s["title_font"] = self.title_font_var.get()
        s["subtitle_font"] = self.subtitle_font_var.get()
        s["logo"] = self.logo_var.get()
        s["logo_placement"] = self.placement_var.get()
        s["logo_color_mode"] = self.logo_color_mode_var.get()
        s["logo_key_white_bg"] = self.logo_key_white_var.get()
        s["logo_height"] = int(self.logo_scale_var.get())
        s["logo_opacity"] = int(self.logo_opacity_var.get())
        s["animation"] = self.anim_var.get()
        s["outro_animation"] = self.outro_var.get()
        s["duration"] = round(float(self.duration_var.get()), 1)
        s["transparent_bg"] = self.transparent_var.get()
        s["layout"] = self.layout_var.get()
        s["lower_third_position"] = self.lt_position_var.get()
        s["lower_third_scale"] = round(int(self.lt_scale_var.get()) / 100.0, 2)
        s["lower_third_bg_opacity"] = int(self.lt_bg_opacity_var.get())
        s["background_style"] = self.bg_style_var.get()
        s["divider"] = self.divider_var.get()
        s["vignette"] = int(self.vignette_var.get())
        s["vignette_shape"] = self.vignette_shape_var.get()
        s["shadow_enabled"] = self.shadow_enabled_var.get()
        s["shadow_opacity"] = int(self.shadow_opacity_var.get())
        s["shadow_blur"] = int(self.shadow_blur_var.get())
        s["shadow_offset_x"] = int(self.shadow_offset_x_var.get())
        s["shadow_offset_y"] = int(self.shadow_offset_y_var.get())
        self.duration_label.configure(text=f"{s['duration']:.1f}s")
        self.vignette_label.configure(text=f"{s['vignette']}%")
        self.logo_scale_label.configure(text=f"{s['logo_height']}px")
        self.logo_opacity_label.configure(text=f"{s['logo_opacity']}%")
        self.lt_bg_opacity_label.configure(text=f"{s['lower_third_bg_opacity']}%")
        self.lt_scale_label.configure(text=f"{int(self.lt_scale_var.get())}%")
        self.shadow_opacity_label.configure(text=f"{s['shadow_opacity']}%")
        self.shadow_blur_label.configure(text=f"{s['shadow_blur']}px")
        self.shadow_offset_x_label.configure(text=f"{s['shadow_offset_x']}px")
        self.shadow_offset_y_label.configure(text=f"{s['shadow_offset_y']}px")
        for key, swatch in self.color_swatches.items():
            if key in self._EXPLICIT_ONLY_COLOR_KEYS:
                continue
            s[key] = swatch.cget("bg")
        # Colors/layout/transparent-bg can all change here without a full
        # _sync_controls_from_scene() round trip (e.g. picking a brand color),
        # so recalculate live rather than only on scene loads.
        self._update_contrast_warning()

    def _update_gradient_row_visibility(self):
        if self.bg_style_var.get() == "Gradient":
            self.gradient_row.pack(fill="x", padx=8, pady=4)
        else:
            self.gradient_row.pack_forget()

    def _update_logo_custom_color_visibility(self):
        if self.logo_color_mode_var.get() == "custom":
            self.logo_custom_color_row.pack(fill="x", padx=8, pady=(0, 6))
        else:
            self.logo_custom_color_row.pack_forget()

    def _update_shadow_controls_visibility(self):
        if self.shadow_enabled_var.get():
            self.shadow_controls.pack(fill="x")
        else:
            self.shadow_controls.pack_forget()

    def on_shadow_enabled_change(self):
        self._update_shadow_controls_visibility()
        self.on_change()

    def _update_contrast_warning(self):
        """Nudge (never block) when text/background contrast is low. Uses
        the plate color for Lower Third (matching renderer.py's own
        lower_third_bg_color-or-bg_color fallback) and skips the check
        entirely for a transparent Full Title Card background, since there's
        no way to know what footage it'll be composited over."""
        s = self.scene
        text_color = s.get("text_color") or "#ffffff"
        if s.get("layout") == "Lower Third":
            effective_bg_color = s.get("lower_third_bg_color") or s.get("bg_color", "#004b8d")
        elif not s.get("transparent_bg"):
            effective_bg_color = s.get("bg_color", "#004b8d")
        else:
            effective_bg_color = None

        if effective_bg_color and brand.contrast_ratio(text_color, effective_bg_color) < brand.MIN_TEXT_CONTRAST:
            self.contrast_warning_var.set("⚠ Low contrast — text may be hard to read")
            self.contrast_warning_label.pack(fill="x", padx=8, pady=(0, 4), before=self.divider_checkbutton)
        else:
            self.contrast_warning_var.set("")
            self.contrast_warning_label.pack_forget()

    def on_logo_color_mode_change(self, value):
        self._update_logo_custom_color_visibility()
        self.on_change()

    def _update_lower_third_controls_visibility(self):
        if self.layout_var.get() == "Lower Third":
            self.lower_third_controls.pack(fill="x")
        else:
            self.lower_third_controls.pack_forget()

    def on_bg_style_change(self, value):
        self._update_gradient_row_visibility()
        self.on_change()

    def on_layout_change(self, value):
        self._update_lower_third_controls_visibility()
        self.on_change()

    def on_aspect_change(self, preset_name):
        self.scene["canvas_preset_name"] = preset_name
        self.scene["canvas_size"] = brand.CANVAS_PRESETS[preset_name]
        self.on_change()

    def pick_brand_color(self, key, swatch):
        win = tk.Toplevel(self.root)
        win.title("Choose brand color")
        win.geometry("260x440")
        win.configure(bg=brand.UI_PANEL_BG)
        for name, hexval in brand.ALL_COLORS.items():
            row = tk.Frame(win, bg=brand.UI_PANEL_BG)
            row.pack(fill="x", padx=6, pady=2)
            tk.Canvas(row, width=20, height=20, bg=hexval, highlightthickness=1).pack(side="left", padx=4)
            tk.Button(row, text=name, anchor="w", bg=brand.UI_PANEL_BG, fg=brand.UI_TEXT,
                      activebackground=brand.UI_PANEL_BG_ALT, relief="flat",
                      command=lambda h=hexval, w=win: self._set_color(key, swatch, h, w)).pack(side="left", fill="x", expand=True)
        ttk.Separator(win).pack(fill="x", pady=6)
        ttk.Button(win, text="Custom color\u2026", command=lambda: self._custom_color(key, swatch, win)).pack(pady=6)

    _EXPLICIT_ONLY_COLOR_KEYS = ("bg_gradient_color", "lower_third_bg_color", "logo_custom_color")

    def _set_color(self, key, swatch, hexval, win):
        swatch.configure(bg=hexval)
        if key in self._EXPLICIT_ONLY_COLOR_KEYS:
            self.scene[key] = hexval
        win.destroy()
        self.on_change()
        self.commit_history()

    def _custom_color(self, key, swatch, win):
        rgb, hexval = colorchooser.askcolor(title="Custom color")
        if hexval:
            swatch.configure(bg=hexval)
            if key in self._EXPLICIT_ONLY_COLOR_KEYS:
                self.scene[key] = hexval
            win.destroy()
            self.on_change()
            self.commit_history()

    def apply_preset(self, preset_name):
        if self.is_playing:
            self.pause_play()
        self.scene["bg_gradient_color"] = None
        self.scene["lower_third_bg_color"] = None
        self.scene.update(brand.PRESETS[preset_name])
        self._sync_controls_from_scene()
        self.on_change()
        self.commit_history()

    def apply_prompt(self):
        text = self.prompt_var.get().strip()
        if not text:
            return
        if self.is_playing:
            self.pause_play()
        self._sync_scene_from_controls()
        new_scene, notes = prompt_ai.interpret(text, self.scene)
        self.scene = new_scene
        self._sync_controls_from_scene()
        self.status_var.set("AI prompt applied: " + "; ".join(notes))
        self.schedule_preview()
        self.commit_history()

    # ------------------------------------------------------------------
    # Timeline callbacks
    # ------------------------------------------------------------------
    def _on_timeline_drag(self, fast=True):
        if self.is_playing:
            self.pause_play()
        self.schedule_preview(fast=fast)
        if not fast:
            self.commit_history()

    def _on_timeline_scrub(self, frac):
        if self.is_playing:
            self.pause_play()
        self.scrub_t = frac
        self.schedule_preview(fast=True)

    # ------------------------------------------------------------------
    # Undo / redo
    # ------------------------------------------------------------------
    def commit_history(self):
        if self._restoring:
            return
        if self._history_job:
            self.root.after_cancel(self._history_job)
            self._history_job = None
        snapshot = copy.deepcopy(self.scene)
        if self._history and self._history[self._history_index] == snapshot:
            return
        self._history = self._history[:self._history_index + 1]
        self._history.append(snapshot)
        if len(self._history) > HISTORY_LIMIT:
            self._history.pop(0)
        self._history_index = len(self._history) - 1
        self._update_undo_redo_state()

    def _schedule_history_commit(self):
        if self._restoring:
            return
        if self._history_job:
            self.root.after_cancel(self._history_job)
        self._history_job = self.root.after(HISTORY_DEBOUNCE_MS, self.commit_history)

    def undo(self):
        if self.is_playing:
            self.pause_play()
        if self._history_index > 0:
            self._history_index -= 1
            self._restore_from_history()

    def redo(self):
        if self.is_playing:
            self.pause_play()
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._restore_from_history()

    def _restore_from_history(self):
        self._restoring = True
        try:
            self.scene = copy.deepcopy(self._history[self._history_index])
            self._sync_controls_from_scene()
            self.schedule_preview()
        finally:
            self._restoring = False
        self._update_undo_redo_state()

    def _update_undo_redo_state(self):
        self.undo_btn.configure(state="normal" if self._history_index > 0 else "disabled")
        self.redo_btn.configure(state="normal" if self._history_index < len(self._history) - 1 else "disabled")

    # ------------------------------------------------------------------
    # Preview rendering
    # ------------------------------------------------------------------
    def on_change(self):
        if self._syncing:
            return
        if self.is_playing:
            self.pause_play()
        self._sync_scene_from_controls()
        self.schedule_preview()
        self._schedule_history_commit()

    def schedule_preview(self, fast=False):
        if self._preview_job:
            self.root.after_cancel(self._preview_job)
        delay = 30 if fast else 120
        self._preview_job = self.root.after(delay, self.render_preview)

    def _preview_size(self):
        w, h = self.scene.get("canvas_size", brand.CANVAS_SIZE)
        max_w = getattr(self, "preview_max_w", PREVIEW_MAX_W)
        max_h = getattr(self, "preview_max_h", PREVIEW_MAX_H)
        scale = min(max_w / w, max_h / h)
        return max(1, int(w * scale)), max(1, int(h * scale))

    def _checker(self, w, h):
        key = (w, h)
        if key not in self._checker_cache:
            self._checker_cache[key] = make_checkerboard(w, h)
        return self._checker_cache[key]

    def render_preview(self):
        if not self._syncing:
            self._sync_scene_from_controls()
        try:
            t = getattr(self, "scrub_t", 1.0)
            frame = renderer.render_frame(self.scene, t=t)
            pw, ph = self._preview_size()
            small = frame.resize((pw, ph), Image.LANCZOS)
            checker = self._checker(pw, ph).convert("RGBA")
            composed = Image.alpha_composite(checker, small)
            self._tk_img = ImageTk.PhotoImage(composed)
            self.preview_label.configure(image=self._tk_img)
            if hasattr(self, "timeline"):
                self.timeline.set_scrub(t)
            self._update_time_label(t)
        except Exception as e:
            self.status_var.set(f"Preview error: {e}")
            traceback.print_exc()

    def _update_time_label(self, t):
        duration = max(0.1, self.scene.get("duration", 3.0))
        if hasattr(self, "time_label"):
            self.time_label.configure(text=f"{t * duration:0.2f}s / {duration:0.2f}s")

    # ------------------------------------------------------------------
    # Playback (Play / Pause / Stop)
    # ------------------------------------------------------------------
    def _on_space_key(self, event):
        widget = self.root.focus_get()
        if isinstance(widget, (tk.Entry, tk.Text)):
            return  # let space type normally in text fields
        self.toggle_play()
        return "break"

    def toggle_play(self):
        if self.is_playing:
            self.pause_play()
        else:
            self.start_play()

    def start_play(self):
        self._sync_scene_from_controls()
        duration = max(0.1, self.scene.get("duration", 3.0))
        hold = max(0.0, self.scene.get("hold_seconds", 1.0))
        self._play_total_seconds = duration + hold

        start_t_seconds = self.scrub_t * duration
        if self.scrub_t >= 0.999:
            start_t_seconds = 0.0  # replay from the start if it was sitting at the end

        self._play_start_wallclock = time.time() - start_t_seconds
        self.is_playing = True
        self.play_btn.configure(text="\u23f8 Pause")
        self._play_tick()

    def pause_play(self):
        self.is_playing = False
        if self._play_after_id:
            self.root.after_cancel(self._play_after_id)
            self._play_after_id = None
        if hasattr(self, "play_btn"):
            self.play_btn.configure(text="\u25b6 Play")

    def stop_play(self):
        self.pause_play()
        self.scrub_t = 0.0
        self.render_preview()

    def _play_tick(self):
        if not self.is_playing:
            return
        duration = max(0.1, self.scene.get("duration", 3.0))
        total = max(0.1, self._play_total_seconds)
        elapsed = time.time() - self._play_start_wallclock

        if not self.loop_var.get() and elapsed >= total:
            self.scrub_t = 1.0
            self._render_playback_frame(1.0, duration)
            self.pause_play()
            return

        t_seconds = elapsed % total
        t = min(1.0, t_seconds / duration)
        self.scrub_t = t
        self._render_playback_frame(t, t_seconds)
        self._play_after_id = self.root.after(33, self._play_tick)

    def _render_playback_frame(self, t, t_seconds):
        try:
            frame = renderer.render_frame(self.scene, t=t)
            pw, ph = self._preview_size()
            small = frame.resize((pw, ph), Image.LANCZOS)
            checker = self._checker(pw, ph).convert("RGBA")
            composed = Image.alpha_composite(checker, small)
            self._tk_img = ImageTk.PhotoImage(composed)
            self.preview_label.configure(image=self._tk_img)
            self.timeline.set_scrub(t)
            duration = max(0.1, self.scene.get("duration", 3.0))
            self.time_label.configure(text=f"{min(t_seconds, duration):0.2f}s / {duration:0.2f}s")
        except Exception as e:
            self.status_var.set(f"Playback error: {e}")
            self.pause_play()

    # ------------------------------------------------------------------
    # Project file management (New / Open / Save / Save As)
    # ------------------------------------------------------------------
    def _project_display_name(self):
        if not self.project_path:
            return "Untitled"
        return os.path.splitext(os.path.basename(self.project_path))[0]

    def _update_title(self):
        self.root.title(f"Blair Academy — Title & Motion Graphics Creator — {self._project_display_name()}")

    def new_project(self):
        if not messagebox.askyesno(
            "New Project", "Start a new project? Unsaved changes will be lost unless you've saved."
        ):
            return
        if self.is_playing:
            self.pause_play()
        self.scene = default_scene()
        self.project_path = None
        self._history = [copy.deepcopy(self.scene)]
        self._history_index = 0
        self._update_undo_redo_state()
        self._sync_controls_from_scene()
        self.scrub_t = 1.0
        self.schedule_preview()
        self._update_title()
        self.status_var.set("New project started.")

    def _load_scene_from_path(self, path):
        """Load the scene dict at `path` and make it the active project.
        Shared by the file-dialog Open flow and the recent-projects flow so
        both get the same old/partial-project-file tolerance (merging the
        loaded dict onto default_scene() rather than trusting it wholesale).
        Raises on failure; caller is responsible for reporting errors."""
        loaded_scene = project_io.load_project(path)
        if self.is_playing:
            self.pause_play()
        merged = default_scene()
        merged.update(loaded_scene)
        self.scene = merged
        self.project_path = path
        self._history = [copy.deepcopy(self.scene)]
        self._history_index = 0
        self._update_undo_redo_state()
        self._sync_controls_from_scene()
        self.schedule_preview()
        self._update_title()

    def _record_recent_project(self, path):
        """Best-effort recent-projects bookkeeping. Never let a thumbnail
        render/JSON write hiccup block or crash a real save/open."""
        try:
            record_recent_project(path, self.scene)
        except Exception:
            traceback.print_exc()

    def open_project(self):
        path = filedialog.askopenfilename(
            defaultextension=project_io.PROJECT_EXTENSION,
            filetypes=[("Blair Title Project", f"*{project_io.PROJECT_EXTENSION}"), ("All files", "*.*")],
            initialdir=brand.OUTPUT_DIR)
        if not path:
            return
        try:
            self._load_scene_from_path(path)
        except Exception as e:
            messagebox.showerror("Open failed", f"Could not open project:\n{e}")
            return
        self._record_recent_project(path)
        self.status_var.set(f"Opened: {path}")

    def open_recent_project(self, path):
        if not os.path.exists(path):
            messagebox.showerror("Open failed", f"This project file no longer exists:\n{path}")
            prune_recent_project(path)
            return
        try:
            self._load_scene_from_path(path)
        except Exception as e:
            messagebox.showerror("Open failed", f"Could not open project:\n{e}")
            return
        self._record_recent_project(path)
        self.status_var.set(f"Opened: {path}")

    def save_project(self):
        self._sync_scene_from_controls()
        if not self.project_path:
            self.save_project_as()
            return
        try:
            project_io.save_project(self.scene, self.project_path)
            self.status_var.set(f"Saved: {self.project_path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            return
        self._record_recent_project(self.project_path)

    def save_project_as(self):
        self._sync_scene_from_controls()
        initial = self._project_display_name() if self.project_path else "blair_title_project"
        path = filedialog.asksaveasfilename(
            defaultextension=project_io.PROJECT_EXTENSION,
            filetypes=[("Blair Title Project", f"*{project_io.PROJECT_EXTENSION}")],
            initialdir=brand.OUTPUT_DIR, initialfile=initial)
        if not path:
            return
        try:
            project_io.save_project(self.scene, path)
            self.project_path = path
            self._update_title()
            self.status_var.set(f"Saved: {path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            return
        self._record_recent_project(path)

    # ------------------------------------------------------------------
    # Recent Projects dialog
    # ------------------------------------------------------------------
    def show_recent_projects_dialog(self):
        entries = load_recent_projects()

        win = tk.Toplevel(self.root)
        win.title("Recent Projects")
        win.geometry("420x560")
        win.configure(bg=brand.UI_PANEL_BG)

        ttk.Label(win, text="Recent Projects", style="Heading.TLabel").pack(anchor="w", padx=10, pady=(10, 4))

        canvas = tk.Canvas(win, highlightthickness=0, bg=brand.UI_PANEL_BG)
        scrollbar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        list_frame = tk.Frame(canvas, bg=brand.UI_PANEL_BG)
        list_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=list_frame, anchor="nw", width=380)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        scrollbar.pack(side="left", fill="y", pady=(0, 10), padx=(0, 10))

        win._thumb_images = []  # keep PhotoImage refs alive for the dialog's lifetime
        stale_paths = []
        any_shown = False

        for entry in entries:
            path = entry.get("path", "")
            if not path or not os.path.exists(path):
                if path:
                    stale_paths.append(path)
                continue
            any_shown = True
            row = tk.Frame(list_frame, bg=brand.UI_PANEL_BG_ALT, cursor="hand2")
            row.pack(fill="x", padx=4, pady=4)

            thumb_label = tk.Label(row, bg=brand.UI_PANEL_BG_ALT, bd=0)
            thumb_b64 = entry.get("thumbnail_b64")
            if thumb_b64:
                try:
                    thumb_img = Image.open(io.BytesIO(base64.b64decode(thumb_b64)))
                    photo = ImageTk.PhotoImage(thumb_img)
                    win._thumb_images.append(photo)
                    thumb_label.configure(image=photo)
                except Exception:
                    pass
            thumb_label.pack(side="left", padx=6, pady=6)

            text_frame = tk.Frame(row, bg=brand.UI_PANEL_BG_ALT)
            text_frame.pack(side="left", fill="x", expand=True, padx=6, pady=6)
            name = entry.get("name") or os.path.splitext(os.path.basename(path))[0]
            tk.Label(text_frame, text=name, bg=brand.UI_PANEL_BG_ALT, fg=brand.UI_TEXT,
                     font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(text_frame, text=_format_last_touched(entry.get("last_touched", 0)),
                     bg=brand.UI_PANEL_BG_ALT, fg=brand.UI_TEXT_DIM, anchor="w").pack(fill="x")
            tk.Label(text_frame, text=path, bg=brand.UI_PANEL_BG_ALT, fg=brand.UI_TEXT_DIM,
                     anchor="w", wraplength=220, font=("TkDefaultFont", 7)).pack(fill="x")

            def open_this(p=path, w=win):
                w.destroy()
                self.open_recent_project(p)

            for widget in (row, thumb_label, text_frame):
                widget.bind("<Button-1>", lambda e, fn=open_this: fn())
            for child in text_frame.winfo_children():
                child.bind("<Button-1>", lambda e, fn=open_this: fn())

        if not any_shown:
            tk.Label(list_frame, text="(No recent projects)", bg=brand.UI_PANEL_BG,
                     fg=brand.UI_TEXT_DIM).pack(anchor="w", padx=6, pady=10)

        if stale_paths:
            for p in stale_paths:
                prune_recent_project(p)

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_png(self):
        if self.is_playing:
            self.pause_play()
        self._sync_scene_from_controls()
        path = filedialog.asksaveasfilename(
            defaultextension=".png", filetypes=[("PNG image", "*.png")],
            initialdir=brand.OUTPUT_DIR, initialfile="blair_title.png")
        if not path:
            return
        try:
            export.export_png(self.scene, path)
            self.status_var.set(f"Saved: {path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    def export_video(self, codec):
        if self.is_playing:
            self.pause_play()
        self._sync_scene_from_controls()
        ext = ".webm" if codec == "webm" else ".mov"
        path = filedialog.asksaveasfilename(
            defaultextension=ext, filetypes=[(codec.upper(), f"*{ext}")],
            initialdir=brand.OUTPUT_DIR, initialfile=f"blair_title{ext}")
        if not path:
            return
        self.status_var.set("Rendering video locally\u2026 this may take a few seconds.")
        self.root.update_idletasks()

        # Export renders frame-by-frame over several seconds; snapshot the
        # scene so edits made on the UI thread mid-export can't mutate it
        # out from under the render loop.
        scene_snapshot = copy.deepcopy(self.scene)

        def worker():
            try:
                export.export_video(scene_snapshot, path, codec=codec)
                self.root.after(0, lambda: self.status_var.set(f"Saved: {path}"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Export failed", str(e)))
                self.root.after(0, lambda: self.status_var.set("Export failed."))

        threading.Thread(target=worker, daemon=True).start()


def _setup_dark_theme(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    bg = brand.UI_DARK_BG
    panel = brand.UI_PANEL_BG
    panel_alt = brand.UI_PANEL_BG_ALT
    border = brand.UI_BORDER
    text = brand.UI_TEXT
    dim = brand.UI_TEXT_DIM
    accent = brand.UI_ACCENT
    entry_bg = brand.UI_ENTRY_BG

    root.option_add("*Menu.background", panel)
    root.option_add("*Menu.foreground", text)
    root.option_add("*Menu.activeBackground", accent)
    root.option_add("*Menu.activeForeground", "#ffffff")

    style.configure(".", background=bg, foreground=text, fieldbackground=entry_bg,
                     bordercolor=border, lightcolor=panel, darkcolor=panel)
    style.configure("Dark.TFrame", background=bg)
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=text)
    style.configure("Dim.TLabel", background=bg, foreground=dim, font=("TkDefaultFont", 8))
    style.configure("Accent.TLabel", background=bg, foreground="#7fd08a", font=("TkDefaultFont", 8))
    style.configure("Warning.TLabel", background=bg, foreground="#e2b93b", font=("TkDefaultFont", 8, "bold"))
    style.configure("Heading.TLabel", background=bg, foreground=text, font=("TkDefaultFont", 11, "bold"))
    style.configure("TLabelframe", background=bg, foreground=text, bordercolor=border)
    style.configure("TLabelframe.Label", background=bg, foreground=accent, font=("TkDefaultFont", 9, "bold"))
    style.configure("TButton", background=panel_alt, foreground=text, bordercolor=border,
                     focuscolor=panel_alt, padding=5)
    style.map("TButton", background=[("active", accent), ("disabled", panel)],
              foreground=[("disabled", dim)])
    style.configure("TCheckbutton", background=bg, foreground=text)
    style.map("TCheckbutton", background=[("active", bg)])
    style.configure("TEntry", fieldbackground=entry_bg, foreground=text, insertcolor=text,
                     bordercolor=border)
    style.configure("TMenubutton", background=panel_alt, foreground=text, bordercolor=border,
                     arrowcolor=text, padding=4)
    style.map("TMenubutton", background=[("active", accent)])
    style.configure("TScale", background=bg, troughcolor=panel_alt)
    style.configure("TScrollbar", background=panel_alt, troughcolor=bg, bordercolor=border,
                     arrowcolor=text)
    style.configure("TSeparator", background=border)
    style.configure("Horizontal.TScale", background=bg)


def main():
    os.makedirs(brand.OUTPUT_DIR, exist_ok=True)
    root = tk.Tk()
    _setup_dark_theme(root)
    app = BlairTitleApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
