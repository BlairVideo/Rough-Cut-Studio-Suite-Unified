#!/usr/bin/env python3
"""
Video / Audio Sync Tool
========================
A local desktop app (Tkinter) for syncing a camera's video to one or more
external 32-bit float audio recordings, by waveform or timecode, with
uncompressed export, source metadata preservation, and an option to keep
the camera's on-board audio as additional channel(s).

This version adds:
  - Pre-sync previews: watch the camera clip and preview each external audio
    file on its own (with waveform) before syncing anything.
  - Synced previews: a combined waveform comparison you can nudge by hand
    (drag on the waveform or use the ms buttons), plus a "play synced mix"
    button that plays the camera video with all audio tracks mixed together
    at their current offsets, so you can watch & listen to confirm
    alignment before exporting.
  - A dark UI theme.

Everything still runs locally via ffmpeg/ffprobe/OpenCV/sounddevice -- no
uploads, no accounts, no network access of any kind.

Requirements (see README.md):
    - Python 3.9+, ffmpeg + ffprobe on PATH, Tkinter
    - pip install -r requirements.txt   (numpy, scipy, opencv-python-headless,
      Pillow, sounddevice)

Run with:
    python3 sync_app.py
"""

from __future__ import annotations

import sys
import time
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import sync_core as sc
import media_playback as mpb
from waveform_view import WaveformCanvas, WaveformTrack, compute_peaks

try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except Exception:
    Image = None
    ImageTk = None
    _PIL_AVAILABLE = False

VIDEO_PREVIEW_W = 480
VIDEO_PREVIEW_H = 270

# --------------------------------------------------------------------------
# Visual style tokens -- dark theme
# (Blue/grey/teal palette in the spirit of the project's brand style guide,
#  re-balanced for a dark UI: the same accent hues, on near-black surfaces,
#  used purely as a color/typography scheme -- not any institutional
#  branding or logo.)
# --------------------------------------------------------------------------

COLOR_BG = "#0d1117"          # app background (near-black)
COLOR_PANEL = "#161b22"       # panel background
COLOR_PANEL_ALT = "#1c232c"   # slightly raised panel (rows, inputs)
COLOR_BORDER = "#2a333d"      # subtle borders / separators
COLOR_HEADER = "#0077a0"      # Blair Blue-ish, brightened for dark bg -- header bar
COLOR_PRIMARY = "#1f8fd6"     # brighter blue for primary actions on dark bg
COLOR_PRIMARY_ACTIVE = "#5f8fb4"  # Sky -- hover/active
COLOR_ACCENT = "#00b2ba"      # Teal -- secondary accent, playhead-adjacent UI
COLOR_WARN = "#f15d22"        # Orange -- warnings / playhead marker
COLOR_TEXT = "#e6edf3"        # near-white body text
COLOR_TEXT_MUTED = "#8b96a3"  # muted grey text
COLOR_WAVE_REF = "#5f8fb4"    # camera / reference waveform color
COLOR_WAVE_TRACK = "#00b2ba"  # external track waveform color

FONT_FAMILY = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"
FONT_BODY = (FONT_FAMILY, 10)
FONT_LABEL = (FONT_FAMILY, 10, "bold")
FONT_HEADER = (FONT_FAMILY, 15, "bold")
FONT_SMALL = (FONT_FAMILY, 9)


def apply_theme(root: tk.Tk) -> ttk.Style:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=COLOR_BG)

    style.configure("TFrame", background=COLOR_BG)
    style.configure("Panel.TFrame", background=COLOR_PANEL)
    style.configure("Row.TFrame", background=COLOR_PANEL_ALT)
    style.configure("Header.TFrame", background=COLOR_HEADER)

    style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT, font=FONT_BODY)
    style.configure("Panel.TLabel", background=COLOR_PANEL, foreground=COLOR_TEXT, font=FONT_BODY)
    style.configure("Row.TLabel", background=COLOR_PANEL_ALT, foreground=COLOR_TEXT, font=FONT_BODY)
    style.configure("Header.TLabel", background=COLOR_HEADER, foreground="white", font=FONT_HEADER)
    style.configure("Muted.TLabel", background=COLOR_PANEL, foreground=COLOR_TEXT_MUTED, font=FONT_SMALL)
    style.configure("RowMuted.TLabel", background=COLOR_PANEL_ALT, foreground=COLOR_TEXT_MUTED, font=FONT_SMALL)
    style.configure("FieldLabel.TLabel", background=COLOR_PANEL, foreground=COLOR_TEXT, font=FONT_LABEL)

    style.configure("Primary.TButton", background=COLOR_PRIMARY, foreground="white",
                     font=FONT_LABEL, padding=8, borderwidth=0)
    style.map("Primary.TButton",
              background=[("active", COLOR_PRIMARY_ACTIVE), ("disabled", COLOR_BORDER)])

    style.configure("Secondary.TButton", background=COLOR_PANEL_ALT, foreground=COLOR_TEXT,
                     font=FONT_BODY, padding=6, borderwidth=1)
    style.map("Secondary.TButton", background=[("active", COLOR_BORDER)])

    style.configure("Nudge.TButton", background=COLOR_PANEL_ALT, foreground=COLOR_ACCENT,
                     font=(FONT_FAMILY, 9, "bold"), padding=3, borderwidth=1)
    style.map("Nudge.TButton", background=[("active", COLOR_BORDER)])

    style.configure("TCheckbutton", background=COLOR_PANEL, foreground=COLOR_TEXT, font=FONT_BODY)
    style.configure("TRadiobutton", background=COLOR_PANEL, foreground=COLOR_TEXT, font=FONT_BODY)
    style.configure("TCombobox", font=FONT_BODY, fieldbackground=COLOR_PANEL_ALT,
                     background=COLOR_PANEL_ALT, foreground=COLOR_TEXT, selectbackground=COLOR_PANEL_ALT,
                     selectforeground=COLOR_TEXT, arrowcolor=COLOR_TEXT)
    style.map("TCombobox",
              fieldbackground=[("readonly", COLOR_PANEL_ALT), ("disabled", COLOR_PANEL_ALT)],
              foreground=[("readonly", COLOR_TEXT), ("disabled", COLOR_TEXT_MUTED)],
              background=[("readonly", COLOR_PANEL_ALT)],
              selectbackground=[("readonly", COLOR_PANEL_ALT)],
              selectforeground=[("readonly", COLOR_TEXT)])
    # The dropdown listbox itself is a plain Tk listbox under the hood, styled
    # via option database rather than ttk styles.
    root.option_add("*TCombobox*Listbox.background", COLOR_PANEL_ALT)
    root.option_add("*TCombobox*Listbox.foreground", COLOR_TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", COLOR_PRIMARY)
    root.option_add("*TCombobox*Listbox.selectForeground", "white")
    style.configure("TEntry", fieldbackground=COLOR_PANEL_ALT, foreground=COLOR_TEXT,
                     insertcolor=COLOR_TEXT)
    style.configure("TSpinbox", fieldbackground=COLOR_PANEL_ALT, foreground=COLOR_TEXT,
                     insertcolor=COLOR_TEXT, arrowcolor=COLOR_TEXT)
    style.configure("Horizontal.TProgressbar", background=COLOR_ACCENT, troughcolor=COLOR_BORDER)
    style.configure("TScale", background=COLOR_PANEL, troughcolor=COLOR_PANEL_ALT)
    style.configure("Horizontal.TScale", background=COLOR_PANEL_ALT, troughcolor=COLOR_BORDER)
    style.configure("TNotebook", background=COLOR_BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=COLOR_PANEL, foreground=COLOR_TEXT_MUTED,
                     font=FONT_LABEL, padding=(14, 8))
    style.map("TNotebook.Tab",
              background=[("selected", COLOR_HEADER)],
              foreground=[("selected", "white")])

    return style


def format_offset(seconds: float) -> str:
    return f"{seconds:+.3f} s ({seconds*1000:+.0f} ms)"


def describe_source_format(info: sc.ProbeInfo) -> str:
    """Short label like '24-bit integer \u2022 48kHz \u2022 2ch' for display next to a source."""
    if not info.has_audio:
        return "(no audio track)"
    sr = f"{info.audio_samplerate/1000:.0f}kHz" if info.audio_samplerate else "?kHz"
    ch = f"{info.audio_channels}ch" if info.audio_channels else "?ch"
    label = info.audio_format_label
    if info.exceeds_32bit_float:
        label += "  \u2192 will export at 32-bit float (max supported)"
    return f"{label}  \u2022  {sr}  \u2022  {ch}"


# --------------------------------------------------------------------------
# A single audio-only preview strip: waveform + play/pause + seek + volume.
# Used both for pre-sync raw-file review and reused (minus play button) as a
# data holder feeding the synced overlay in tab 2.
# --------------------------------------------------------------------------

class AudioPreviewStrip:
    def __init__(self, parent, title: str, on_error=None):
        self.on_error = on_error or (lambda msg: None)
        self.samples = None       # decoded float32 (n, channels)
        self.samplerate = mpb.PREVIEW_SAMPLERATE
        self.peaks = None         # cached downsampled peaks -- computed once in load()
        self.duration = 0.0
        self.player: mpb.AudioPlayer | None = None
        self.ready = False        # False until the background load finishes
        self._tick_job = None

        self.frame = ttk.Frame(parent, style="Row.TFrame")
        top = ttk.Frame(self.frame, style="Row.TFrame")
        top.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(top, text=title, style="Row.TLabel", font=FONT_LABEL).pack(side="left")

        self.play_btn = ttk.Button(top, text="\u25B6 Play", style="Secondary.TButton",
                                   command=self.toggle_play, width=9)
        self.play_btn.pack(side="right")
        self.play_btn.state(["disabled"])

        self.wave = WaveformCanvas(self.frame, height=70, on_seek=self._on_seek)
        self.wave.pack(fill="x", padx=8, pady=(0, 8))

    def load(self, path: str):
        """
        Kick off loading in the background and return immediately -- decoding
        a preview buffer can take several seconds on a long field recording,
        and doing that on the UI thread is what made adding files freeze the
        app.
        """
        threading.Thread(target=self._load_worker, args=(path,), daemon=True).start()

    def _load_worker(self, path: str):
        try:
            samples = mpb.load_preview_audio(path, channels=2)
            samplerate = mpb.PREVIEW_SAMPLERATE
            duration = samples.shape[0] / samplerate
            peaks = compute_peaks(samples, 600)
        except Exception as exc:
            message = str(exc)
            self.frame.after(0, lambda: self._apply_load_failure(message))
            return

        self.frame.after(0, lambda: self._apply_loaded(samples, samplerate, duration, peaks))

    def _apply_load_failure(self, message: str):
        if not self.frame.winfo_exists():
            return
        self.on_error(f"Could not load audio preview: {message}")

    def _apply_loaded(self, samples, samplerate, duration, peaks):
        if not self.frame.winfo_exists():
            return
        self.samples = samples
        self.samplerate = samplerate
        self.duration = duration
        self.peaks = peaks
        self.wave.set_tracks([WaveformTrack("", peaks, duration, color=COLOR_WAVE_TRACK)],
                             view_duration=duration)
        self.ready = True
        self.play_btn.state(["!disabled"])

    def _on_seek(self, seconds: float):
        if self.player is not None:
            self.player.seek(seconds)
            self.wave.set_playhead(seconds)

    def toggle_play(self):
        if self.samples is None:
            self.on_error("No audio loaded yet.")
            return
        if self.player is not None and self.player.is_playing():
            self.pause()
            return
        self.player = mpb.AudioPlayer(self.samples, self.samplerate)
        self.player.on_stop = self._on_playback_stopped
        try:
            self.player.play()
        except mpb.PlaybackUnavailable as exc:
            self.on_error(str(exc))
            return
        self.play_btn.configure(text="\u23F8 Pause")
        self._start_ticking()

    def pause(self):
        if self.player is not None:
            self.player.pause()
        self.play_btn.configure(text="\u25B6 Play")
        self._stop_ticking()

    def _on_playback_stopped(self):
        self.frame.after(0, lambda: self.play_btn.configure(text="\u25B6 Play"))
        self._stop_ticking()

    def _start_ticking(self):
        def tick():
            if self.player is not None and self.player.is_playing():
                self.wave.set_playhead(self.player.position_seconds())
                self._tick_job = self.frame.after(50, tick)
        self._tick_job = self.frame.after(50, tick)

    def _stop_ticking(self):
        if self._tick_job is not None:
            try:
                self.frame.after_cancel(self._tick_job)
            except Exception:
                pass  # job already fired/cancelled -- expected race, not an error
            self._tick_job = None

    def destroy(self):
        self.pause()
        self.frame.destroy()


# --------------------------------------------------------------------------
# Data model for one external audio track (spans both tabs)
# --------------------------------------------------------------------------

class AudioRow:
    def __init__(self, app: "SyncApp", path: str):
        self.app = app
        self.path = path
        self.offset_seconds = 0.0
        self.offset_var = tk.StringVar(value="not synced yet")
        self.label = Path(path).stem
        self.format_info = None
        # Output track/stream number for export. Defaults to a distinct
        # number per file (its 1-based add order), so by default every file
        # keeps its own output track; the user can set two rows to the same
        # number to mix those files onto one output track instead.
        self.track_var = tk.IntVar(value=len(app.audio_rows) + 1)

        # -- pre-sync tab row --
        self.frame = ttk.Frame(app.tracks_container, style="Row.TFrame")
        head = ttk.Frame(self.frame, style="Row.TFrame")
        head.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(head, text=Path(path).name, style="Row.TLabel", font=FONT_LABEL).pack(side="left")

        self.format_var = tk.StringVar(value="probing format\u2026")
        self.format_label = ttk.Label(head, textvariable=self.format_var, style="RowMuted.TLabel",
                                      foreground=COLOR_ACCENT)
        self.format_label.pack(side="left", padx=(10, 0))

        ttk.Button(head, text="Remove", style="Secondary.TButton",
                   command=self.remove).pack(side="right")

        self.preview = AudioPreviewStrip(self.frame, "Preview (unsynced)", on_error=app.show_error)
        self.preview.frame.pack(fill="x")
        self.frame.pack(fill="x", pady=(0, 6), padx=4)

        # Both of these run in background threads and return immediately --
        # neither format probing nor audio decoding ever blocks the UI, even
        # for long field recordings.
        threading.Thread(target=self._probe_worker, args=(path,), daemon=True).start()
        self.preview.load(path)

        # -- synced tab row (offset controls; created lazily by SyncApp) --
        self.sync_row_frame = None

    def _probe_worker(self, path: str):
        try:
            info = sc.probe_info(path)
        except Exception as exc:
            message = str(exc)
            self.app.root.after(0, lambda: self._apply_probe_failure(message))
            return
        self.app.root.after(0, lambda: self._apply_probe_result(info))

    def _apply_probe_failure(self, message: str):
        if not self.format_label.winfo_exists():
            return
        self.format_var.set(f"format probe failed: {message}")

    def _apply_probe_result(self, info: sc.ProbeInfo):
        if not self.format_label.winfo_exists():
            return
        self.format_info = info
        self.format_var.set(describe_source_format(info))
        self.format_label.configure(foreground=COLOR_WARN if info.exceeds_32bit_float else COLOR_ACCENT)
        if info.exceeds_32bit_float:
            self.app.log(f"[format] {Path(self.path).name} is {info.audio_format_label}; "
                        f"this tool processes and exports at 32-bit float (its maximum supported "
                        f"precision), so it will be downsampled to 32-bit float.")

    def remove(self):
        self.preview.destroy()
        self.frame.destroy()
        if self.sync_row_frame is not None:
            self.sync_row_frame.destroy()
        self.app.audio_rows.remove(self)
        self.app.refresh_sync_tab()

    def to_spec(self) -> sc.AudioTrackSpec:
        try:
            track = self.track_var.get()
        except tk.TclError:
            track = 1  # Spinbox left in an invalid state; fall back to its own track
        return sc.AudioTrackSpec(path=self.path, offset_seconds=self.offset_seconds,
                                  label=self.label, track=track)

    def nudge(self, delta_seconds: float):
        self.offset_seconds += delta_seconds
        self.offset_var.set(format_offset(self.offset_seconds))
        self.app.refresh_sync_waveform()


# --------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------

class SyncApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Video / Audio Sync Tool")
        self.root.geometry("980x760")
        self.style = apply_theme(root)

        self.video_path_var = tk.StringVar(value="")
        self.method_var = tk.StringVar(value="waveform")
        self.keep_camera_var = tk.BooleanVar(value=False)
        self.codec_var = tk.StringVar(value="v210")
        self.output_path_var = tk.StringVar(value="")
        self.audio_rows: list[AudioRow] = []

        self.camera_audio_samples = None
        self.camera_peaks = None
        self.camera_duration = 0.0
        self.camera_video_source: mpb.VideoFrameSource | None = None
        self.camera_player: mpb.AudioPlayer | None = None
        self._video_tick_job = None
        self._video_play_t0 = None
        self._video_play_start_pos = 0.0

        self.mix_player: mpb.MixPlayer | None = None
        self._synced_tick_job = None
        self._synced_play_t0 = None
        self._synced_play_start_pos = 0.0

        self._export_proc = None
        self._export_cancelled = False

        self._build_layout()
        self._check_ffmpeg()

    # ---- top-level layout -----------------------------------------------

    def _build_layout(self):
        header = ttk.Frame(self.root, style="Header.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="Video / Audio Sync Tool", style="Header.TLabel").pack(
            side="left", padx=16, pady=14)
        ttk.Label(header, text="Runs 100% locally  \u2022  No uploads  \u2022  No account needed",
                  background=COLOR_HEADER, foreground="#d7ecf7", font=FONT_SMALL).pack(
            side="left", padx=(0, 16))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        self.tab_preview = ttk.Frame(self.notebook, style="TFrame")
        self.tab_sync = ttk.Frame(self.notebook, style="TFrame")
        self.tab_export = ttk.Frame(self.notebook, style="TFrame")
        self.notebook.add(self.tab_preview, text="1. Load & Preview")
        self.notebook.add(self.tab_sync, text="2. Sync & Adjust")
        self.notebook.add(self.tab_export, text="3. Export")

        self._build_preview_tab(self.tab_preview)
        self._build_sync_tab(self.tab_sync)
        self._build_export_tab(self.tab_export)

    # ---- Tab 1: Load & Preview -------------------------------------------

    def _build_preview_tab(self, parent):
        video_panel = ttk.Frame(parent, style="Panel.TFrame")
        video_panel.pack(fill="x", pady=(0, 10), padx=2)
        video_panel.configure(padding=12)

        ttk.Label(video_panel, text="Camera video file", style="FieldLabel.TLabel").grid(
            row=0, column=0, sticky="w")
        video_entry = ttk.Entry(video_panel, textvariable=self.video_path_var, width=56, font=FONT_BODY)
        video_entry.grid(row=1, column=0, sticky="we", pady=(2, 8))
        ttk.Button(video_panel, text="Browse\u2026", style="Secondary.TButton",
                   command=self.choose_video).grid(row=1, column=1, padx=(8, 0))
        video_panel.columnconfigure(0, weight=1)

        preview_row = ttk.Frame(video_panel, style="Panel.TFrame")
        preview_row.grid(row=2, column=0, columnspan=2, sticky="we", pady=(4, 0))

        self._blank_frame_photo = None
        if _PIL_AVAILABLE:
            blank = Image.new("RGB", (VIDEO_PREVIEW_W, VIDEO_PREVIEW_H), "#000000")
            self._blank_frame_photo = ImageTk.PhotoImage(blank)
        self.video_canvas = tk.Label(preview_row, bg="#000000", image=self._blank_frame_photo,
                                     width=VIDEO_PREVIEW_W, height=VIDEO_PREVIEW_H,
                                     borderwidth=0, highlightthickness=0)
        self.video_canvas.pack(side="left", padx=(0, 12))
        self._video_photo = self._blank_frame_photo  # keep a reference so Tk doesn't garbage-collect it

        controls = ttk.Frame(preview_row, style="Panel.TFrame")
        controls.pack(side="left", fill="both", expand=True)

        btn_row = ttk.Frame(controls, style="Panel.TFrame")
        btn_row.pack(fill="x")
        self.video_play_btn = ttk.Button(btn_row, text="\u25B6 Play", style="Secondary.TButton",
                                         command=self.toggle_video_play, width=9)
        self.video_play_btn.pack(side="left")

        wave_head = ttk.Frame(controls, style="Panel.TFrame")
        wave_head.pack(fill="x", pady=(8, 2))
        ttk.Label(wave_head, text="Camera audio waveform", style="Muted.TLabel").pack(side="left")
        self.camera_format_var = tk.StringVar(value="")
        ttk.Label(wave_head, textvariable=self.camera_format_var, style="Muted.TLabel",
                 foreground=COLOR_ACCENT).pack(side="left", padx=(10, 0))
        self.camera_wave = WaveformCanvas(controls, height=70, on_seek=self._on_camera_seek)
        self.camera_wave.pack(fill="x")

        # --- External audio files ---
        audio_panel = ttk.Frame(parent, style="Panel.TFrame")
        audio_panel.pack(fill="both", expand=True, pady=(0, 4), padx=2)
        audio_panel.configure(padding=12)

        row = ttk.Frame(audio_panel, style="Panel.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="External 32-bit float audio files", style="FieldLabel.TLabel").pack(side="left")
        ttk.Button(row, text="Add audio file(s)\u2026", style="Secondary.TButton",
                   command=self.add_audio_files).pack(side="right")

        canvas_scroll = tk.Canvas(audio_panel, bg=COLOR_PANEL, highlightthickness=0)
        scrollbar = ttk.Scrollbar(audio_panel, orient="vertical", command=canvas_scroll.yview)
        self.tracks_container = ttk.Frame(canvas_scroll, style="Panel.TFrame")
        self.tracks_container.bind(
            "<Configure>", lambda e: canvas_scroll.configure(scrollregion=canvas_scroll.bbox("all")))
        canvas_scroll.create_window((0, 0), window=self.tracks_container, anchor="nw")
        canvas_scroll.configure(yscrollcommand=scrollbar.set)
        canvas_scroll.pack(side="left", fill="both", expand=True, pady=(8, 0))
        scrollbar.pack(side="right", fill="y")

    # ---- Tab 2: Sync & Adjust -------------------------------------------

    def _build_sync_tab(self, parent):
        top = ttk.Frame(parent, style="Panel.TFrame")
        top.pack(fill="x", pady=(0, 10), padx=2)
        top.configure(padding=12)

        ttk.Label(top, text="Sync method", style="FieldLabel.TLabel").pack(anchor="w")
        method_frame = ttk.Frame(top, style="Panel.TFrame")
        method_frame.pack(anchor="w", pady=(4, 8))
        ttk.Radiobutton(method_frame, text="Waveform (cross-correlation)", value="waveform",
                        variable=self.method_var).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(method_frame, text="Timecode (embedded TC / BWF)", value="timecode",
                        variable=self.method_var).pack(side="left")

        action_row = ttk.Frame(top, style="Panel.TFrame")
        action_row.pack(fill="x")
        ttk.Button(action_row, text="Detect sync for all", style="Primary.TButton",
                   command=self.detect_all).pack(side="left")
        ttk.Checkbutton(action_row, text="Keep camera's on-board audio as additional channel(s)",
                        variable=self.keep_camera_var,
                        command=self.refresh_sync_waveform).pack(side="left", padx=(16, 0))

        # --- Combined waveform comparison ---
        wave_panel = ttk.Frame(parent, style="Panel.TFrame")
        wave_panel.pack(fill="x", pady=(0, 10), padx=2)
        wave_panel.configure(padding=12)
        ttk.Label(wave_panel, text="Synced comparison  (drag a track to nudge its offset)",
                  style="FieldLabel.TLabel").pack(anchor="w")
        self.sync_wave = WaveformCanvas(wave_panel, height=160, on_seek=self._on_sync_seek,
                                        on_offset_drag=self._on_wave_offset_drag)
        self.sync_wave.pack(fill="x", pady=(6, 0))

        play_row = ttk.Frame(wave_panel, style="Panel.TFrame")
        play_row.pack(fill="x", pady=(8, 0))
        self.synced_play_btn = ttk.Button(play_row, text="\u25B6 Play synced mix", style="Primary.TButton",
                                          command=self.toggle_synced_play)
        self.synced_play_btn.pack(side="left")

        # --- Per-track offset/output-track controls ---
        tracks_panel = ttk.Frame(parent, style="Panel.TFrame")
        tracks_panel.pack(fill="both", expand=True, padx=2)
        tracks_panel.configure(padding=12)
        ttk.Label(tracks_panel,
                 text="Per-track offset & output track  (give two files the same output "
                      "Track number to mix them onto one output audio stream)",
                 style="FieldLabel.TLabel").pack(anchor="w")
        self.sync_tracks_container = ttk.Frame(tracks_panel, style="Panel.TFrame")
        self.sync_tracks_container.pack(fill="both", expand=True, pady=(6, 0))

    def _build_sync_row(self, row: AudioRow):
        frame = ttk.Frame(self.sync_tracks_container, style="Row.TFrame")
        frame.pack(fill="x", pady=(0, 4), padx=4)

        ttk.Label(frame, text=row.label, style="Row.TLabel", font=FONT_LABEL, width=24,
                 anchor="w").grid(row=0, column=0, sticky="w", padx=(6, 8), pady=6)
        ttk.Label(frame, textvariable=row.offset_var, style="Row.TLabel", width=20,
                 anchor="w").grid(row=0, column=1, sticky="w")

        nudge_frame = ttk.Frame(frame, style="Row.TFrame")
        nudge_frame.grid(row=0, column=2, padx=8)
        for ms in (-100, -10, 10, 100):
            text = f"{ms:+d}ms"
            ttk.Button(nudge_frame, text=text, style="Nudge.TButton", width=6,
                      command=lambda r=row, d=ms/1000.0: r.nudge(d)).pack(side="left", padx=1)

        track_frame = ttk.Frame(frame, style="Row.TFrame")
        track_frame.grid(row=0, column=3, padx=(12, 6))
        ttk.Label(track_frame, text="Track", style="RowMuted.TLabel").pack(side="left", padx=(0, 4))
        ttk.Spinbox(track_frame, from_=1, to=32, width=3, wrap=False,
                   textvariable=row.track_var).pack(side="left")

        row.sync_row_frame = frame

    def refresh_sync_tab(self):
        """Rebuild the per-track offset/track rows and the comparison waveform."""
        for child in list(self.sync_tracks_container.winfo_children()):
            child.destroy()
        for row in self.audio_rows:
            self._build_sync_row(row)
        self.refresh_sync_waveform()

    def refresh_sync_waveform(self):
        """
        Rebuild the WaveformTrack list from already-computed peaks. This must
        stay cheap -- it's called on every offset nudge/drag -- so it never
        recomputes peak data; that's cached once when audio is first loaded
        (see AudioPreviewStrip.load / load_video).
        """
        tracks = []
        if self.camera_peaks is not None:
            tracks.append(WaveformTrack("Camera (reference)", self.camera_peaks, self.camera_duration,
                                        color=COLOR_WAVE_REF, offset_seconds=0.0, draggable=False))
        for row in self.audio_rows:
            if row.preview.peaks is None:
                continue
            tracks.append(WaveformTrack(row.label, row.preview.peaks, row.preview.duration,
                                        color=COLOR_WAVE_TRACK, offset_seconds=row.offset_seconds,
                                        draggable=True))
        self.sync_wave.set_tracks(tracks)

    def _on_wave_offset_drag(self, track_index: int, delta_seconds: float):
        # track_index 0 is the camera reference row if present and not draggable,
        # so map back to the corresponding AudioRow by matching label order.
        draggable_rows = self.audio_rows
        offset_in_list = track_index - (1 if self.camera_audio_samples is not None else 0)
        if 0 <= offset_in_list < len(draggable_rows):
            row = draggable_rows[offset_in_list]
            row.offset_seconds += delta_seconds
            row.offset_var.set(format_offset(row.offset_seconds))
        self.refresh_sync_waveform()

    def _on_sync_seek(self, seconds: float):
        if self.mix_player is not None:
            self.mix_player.seek(seconds)
        self.sync_wave.set_playhead(seconds)

    # ---- Tab 3: Export -------------------------------------------

    def _build_export_tab(self, parent):
        export_panel = ttk.Frame(parent, style="Panel.TFrame")
        export_panel.pack(fill="x", pady=(0, 10), padx=2)
        export_panel.configure(padding=12)

        self.codec_display_var = tk.StringVar()
        ttk.Label(export_panel, text="Video export format", style="FieldLabel.TLabel").grid(
            row=0, column=0, sticky="w")
        codec_box = ttk.Combobox(export_panel, textvariable=self.codec_display_var, state="readonly",
                                  width=60, font=FONT_BODY, values=[
                                      "v210 - 10-bit uncompressed 4:2:2 (recommended)",
                                      "ffv1 - lossless compressed (smaller file)",
                                      "rawvideo - true uncompressed (largest file)",
                                      "copy - no video re-encode, just sync + remux audio (fastest)",
                                  ])
        codec_box.current(0)
        codec_box.grid(row=1, column=0, sticky="w", pady=(2, 8))
        codec_box.bind("<<ComboboxSelected>>", self._on_codec_change)
        self._on_codec_change()

        ttk.Label(export_panel, text="Output file", style="FieldLabel.TLabel").grid(
            row=2, column=0, sticky="w")
        out_entry = ttk.Entry(export_panel, textvariable=self.output_path_var, width=64, font=FONT_BODY)
        out_entry.grid(row=3, column=0, sticky="we", pady=(2, 0))
        ttk.Button(export_panel, text="Save as\u2026", style="Secondary.TButton",
                   command=self.choose_output).grid(row=3, column=1, padx=(8, 0))
        export_panel.columnconfigure(0, weight=1)

        action_row = ttk.Frame(parent, style="TFrame")
        action_row.pack(fill="x", padx=2)
        self.export_btn = ttk.Button(action_row, text="Export synced file", style="Primary.TButton",
                                     command=self.run_export)
        self.export_btn.pack(side="left")
        self.cancel_btn = ttk.Button(action_row, text="Cancel", style="Secondary.TButton",
                                     command=self.cancel_export)
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.cancel_btn.state(["disabled"])
        self.progress = ttk.Progressbar(action_row, mode="indeterminate", length=200)
        self.progress.pack(side="left", padx=12)

        log_panel = ttk.Frame(parent, style="Panel.TFrame")
        log_panel.pack(fill="both", expand=True, pady=(10, 0), padx=2)
        ttk.Label(log_panel, text="Log", style="FieldLabel.TLabel").pack(anchor="w", padx=8, pady=(6, 0))
        self.log_text = tk.Text(log_panel, height=10, bg="#05070a", fg="#9fd3e6",
                                insertbackground="#9fd3e6", font=("Consolas", 9), relief="flat")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

    # ---- helpers ------------------------------------------------------

    def log(self, msg: str):
        self.log_text.insert("end", msg.rstrip() + "\n")
        self.log_text.see("end")
        self.root.update_idletasks()

    def show_error(self, msg: str):
        self.log(f"ERROR: {msg}")
        messagebox.showerror("Error", msg)

    def _check_ffmpeg(self):
        if not sc.ffmpeg_available():
            self.log("WARNING: ffmpeg/ffprobe were not found on your PATH.")
            messagebox.showwarning(
                "ffmpeg not found",
                "ffmpeg and ffprobe must be installed and on your PATH for this tool to work.\n\n"
                "Install ffmpeg yourself from ffmpeg.org (or your OS package manager) and "
                "restart this app.")

    def _on_codec_change(self, event=None):
        text = self.codec_display_var.get()
        key = text.split(" - ")[0].strip()
        self.codec_var.set(key)

    # ---- file pickers ---------------------------------------------------

    def choose_video(self):
        path = filedialog.askopenfilename(
            title="Choose camera video file",
            filetypes=[("Video files", "*.mp4 *.mov *.mxf *.avi *.mkv"), ("All files", "*.*")])
        if path:
            self.load_video(path)

    def load_video(self, path: str):
        self.video_path_var.set(path)
        if not self.output_path_var.get():
            base = Path(path).with_suffix("")
            self.output_path_var.set(str(base) + "_synced.mov")

        self.camera_format_var.set("probing format\u2026")
        self.video_play_btn.state(["disabled"])
        threading.Thread(target=self._load_video_worker, args=(path,), daemon=True).start()

    def _load_video_worker(self, path: str):
        video_source = None
        video_error = None
        try:
            video_source = mpb.VideoFrameSource(path)
        except mpb.PlaybackUnavailable as exc:
            video_error = str(exc)

        format_text = ""
        exceeds = False
        format_label = ""
        format_error = None
        try:
            info = sc.probe_info(path)
            format_text = describe_source_format(info)
            exceeds = info.exceeds_32bit_float
            format_label = info.audio_format_label
        except Exception as exc:
            format_error = str(exc)

        samples = None
        peaks = None
        duration = 0.0
        audio_error = None
        try:
            samples = mpb.load_preview_audio(path, channels=2)
            duration = samples.shape[0] / mpb.PREVIEW_SAMPLERATE
            peaks = compute_peaks(samples, 600)
        except Exception as exc:
            audio_error = str(exc)

        self.root.after(0, lambda: self._apply_video_loaded(
            video_source, video_error, format_text, exceeds, format_label,
            samples, peaks, duration, audio_error, format_error))

    def _apply_video_loaded(self, video_source, video_error, format_text, exceeds, format_label,
                            samples, peaks, duration, audio_error, format_error=None):
        if not self.video_canvas.winfo_exists():
            return  # window closed while loading

        self.camera_video_source = video_source
        if video_error:
            self.show_error(video_error)
        if format_error:
            self.log(f"[format] Could not determine camera audio format: {format_error}")

        self.camera_format_var.set(format_text)
        if exceeds:
            self.log(f"[format] Camera audio is {format_label}; this tool processes and "
                    f"exports at 32-bit float (its maximum supported precision), so it "
                    f"will be downsampled to 32-bit float.")

        if audio_error:
            self.camera_audio_samples = None
            self.camera_peaks = None
            self.camera_duration = 0.0
            self.show_error(f"Could not decode camera audio for preview: {audio_error}")
        else:
            self.camera_audio_samples = samples
            self.camera_peaks = peaks
            self.camera_duration = duration
            self.camera_wave.set_tracks([WaveformTrack("Camera audio", peaks, duration,
                                                        color=COLOR_WAVE_REF)], view_duration=duration)

        self.video_play_btn.state(["!disabled"])
        if self.camera_video_source is not None:
            self._show_video_frame(0.0)
        self.refresh_sync_waveform()

    def add_audio_files(self):
        paths = filedialog.askopenfilenames(
            title="Choose external audio file(s) (16/24/32-bit int or 32/64-bit float; "
                  "32-bit float is this tool's max processing precision)",
            filetypes=[("Audio files", "*.wav *.wave *.bwf *.aif *.aiff *.caf *.w64 *.rf64 *.flac"),
                      ("All files", "*.*")])
        for p in paths:
            self.audio_rows.append(AudioRow(self, p))
        self.refresh_sync_tab()

    def choose_output(self):
        codec = self.codec_var.get()
        if codec == "copy":
            # No fixed container for a stream copy -- match the source
            # video's own extension so the container isn't changed either.
            video = self.video_path_var.get().strip()
            default_ext = Path(video).suffix if video else ".mov"
        else:
            ext_map = {"v210": ".mov", "ffv1": ".mkv", "rawvideo": ".avi"}
            default_ext = ext_map.get(codec, ".mov")
        path = filedialog.asksaveasfilename(
            title="Save synced export as",
            defaultextension=default_ext,
            filetypes=[("Video file", f"*{default_ext}"), ("All files", "*.*")])
        if path:
            self.output_path_var.set(path)

    # ---- camera video preview (tab 1) ---------------------------------

    def _show_video_frame(self, seconds: float):
        if self.camera_video_source is None:
            return
        try:
            img = self.camera_video_source.get_frame_image(seconds)
            if img is None:
                return
            img.thumbnail((VIDEO_PREVIEW_W, VIDEO_PREVIEW_H))
            self._video_photo = ImageTk.PhotoImage(img)
            self.video_canvas.configure(image=self._video_photo)
        except Exception as exc:
            self.log(f"Video preview error: {exc}")

    def _advance_video_frame(self):
        """
        Advance to the next sequential frame during playback. Unlike
        _show_video_frame (which seeks to an arbitrary timestamp -- the
        right tool for scrubbing, but ~8x more expensive per call), this
        never seeks, which is what keeps continuous playback from lagging.
        It also uses fast nearest-neighbor resampling instead of the default
        (much slower) filter -- imperceptible at this preview size, but
        meaningfully cheaper per tick.
        """
        if self.camera_video_source is None:
            return
        try:
            img = self.camera_video_source.read_next_frame_image()
            if img is None:
                return
            w, h = img.size
            scale = min(VIDEO_PREVIEW_W / w, VIDEO_PREVIEW_H / h, 1.0)
            if scale < 1.0:
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.NEAREST)
            self._video_photo = ImageTk.PhotoImage(img)
            self.video_canvas.configure(image=self._video_photo)
        except Exception as exc:
            self.log(f"Video preview error: {exc}")

    def _video_tick_interval_ms(self) -> int:
        fps = self.camera_video_source.fps if self.camera_video_source else None
        return max(10, round(1000 / fps)) if fps else 40

    def _on_camera_seek(self, seconds: float):
        self._video_play_start_pos = seconds
        self.camera_wave.set_playhead(seconds)
        self._show_video_frame(seconds)
        if self.camera_player is not None:
            self.camera_player.seek(seconds)

    def toggle_video_play(self):
        if self.camera_audio_samples is None and self.camera_video_source is None:
            self.show_error("Load a camera video file first.")
            return
        if self.camera_player is not None and self.camera_player.is_playing():
            self._pause_video()
            return

        start_pos = self._video_play_start_pos
        if self.camera_video_source is not None:
            try:
                self.camera_video_source.seek_to_time(start_pos)
            except Exception as exc:
                self.log(f"Video seek error: {exc}")
        if self.camera_audio_samples is not None:
            self.camera_player = mpb.AudioPlayer(self.camera_audio_samples, mpb.PREVIEW_SAMPLERATE)
            self.camera_player.seek(start_pos)
            self.camera_player.on_stop = lambda: self.root.after(0, self._pause_video)
            try:
                self.camera_player.play()
            except mpb.PlaybackUnavailable as exc:
                self.show_error(str(exc))
                self.camera_player = None

        self._video_play_t0 = time.monotonic() - start_pos
        self.video_play_btn.configure(text="\u23F8 Pause")
        self._tick_video()

    def _pause_video(self):
        if self.camera_player is not None:
            self._video_play_start_pos = self.camera_player.position_seconds()
            self.camera_player.pause()
        self.video_play_btn.configure(text="\u25B6 Play")
        if self._video_tick_job is not None:
            try:
                self.root.after_cancel(self._video_tick_job)
            except Exception:
                pass  # job already fired/cancelled -- expected race, not an error
            self._video_tick_job = None

    def _tick_video(self):
        if self.camera_player is not None and not self.camera_player.is_playing():
            self._pause_video()
            return
        elapsed = time.monotonic() - self._video_play_t0
        duration = self.camera_video_source.duration if self.camera_video_source else None
        if duration and elapsed >= duration:
            self._video_play_start_pos = 0.0
            self._pause_video()
            return
        self._advance_video_frame()
        self.camera_wave.set_playhead(elapsed)
        self._video_tick_job = self.root.after(self._video_tick_interval_ms(), self._tick_video)

    # ---- sync detection (tab 2) ---------------------------------------

    def detect_all(self):
        video = self.video_path_var.get().strip()
        if not video:
            self.show_error("Choose a camera video file first (on the Load & Preview tab).")
            return
        if not self.audio_rows:
            self.show_error("Add at least one external audio file first.")
            return
        threading.Thread(target=self._detect_all_worker, args=(video,), daemon=True).start()

    def _detect_all_worker(self, video: str):
        self._set_busy(True, "Detecting sync\u2026")
        method = self.method_var.get()
        for row in list(self.audio_rows):
            try:
                offset = sc.compute_offset(video, row.path, method=method)
                row.offset_seconds = offset
                row.offset_var.set(format_offset(offset))
                self.log(f"[sync] {row.label}: offset {offset:+.3f}s ({method})")
            except Exception as exc:
                row.offset_var.set("failed")
                self.log(f"[sync] {row.label}: FAILED - {exc}")
        self.root.after(0, self.refresh_sync_waveform)
        self._set_busy(False)

    def _set_busy(self, busy: bool, message: str = ""):
        def apply():
            if busy:
                self.export_btn.state(["disabled"])
                self.progress.start(12)
                if message:
                    self.log(message)
            else:
                self.export_btn.state(["!disabled"])
                self.progress.stop()
        self.root.after(0, apply)

    # ---- synced mix preview (tab 2) ------------------------------------

    def _build_mix_tracks(self) -> list[mpb.MixTrack]:
        tracks = []
        if self.keep_camera_var.get() and self.camera_audio_samples is not None:
            tracks.append(mpb.MixTrack(self.camera_audio_samples, 0.0, "camera"))
        for row in self.audio_rows:
            if row.preview.samples is not None:
                tracks.append(mpb.MixTrack(row.preview.samples, row.offset_seconds, row.label))
        return tracks

    def toggle_synced_play(self):
        if self.mix_player is not None and self.mix_player.is_playing():
            self._pause_synced()
            return
        tracks = self._build_mix_tracks()
        if not tracks:
            self.show_error("Load a camera video and at least one audio file first.")
            return

        start_pos = self._synced_play_start_pos
        if self.camera_video_source is not None:
            try:
                self.camera_video_source.seek_to_time(start_pos)
            except Exception as exc:
                self.log(f"Video seek error: {exc}")
        self.mix_player = mpb.MixPlayer(tracks, samplerate=mpb.PREVIEW_SAMPLERATE, channels=2)
        self.mix_player.seek(start_pos)
        self.mix_player.on_stop = lambda: self.root.after(0, self._pause_synced)
        try:
            self.mix_player.play()
        except mpb.PlaybackUnavailable as exc:
            self.show_error(str(exc))
            self.mix_player = None
            return

        self._synced_play_t0 = time.monotonic() - start_pos
        self.synced_play_btn.configure(text="\u23F8 Pause")
        self._tick_synced()

    def _pause_synced(self):
        if self.mix_player is not None:
            self._synced_play_start_pos = self.mix_player.position_seconds()
            self.mix_player.pause()
        self.synced_play_btn.configure(text="\u25B6 Play synced mix")
        if self._synced_tick_job is not None:
            try:
                self.root.after_cancel(self._synced_tick_job)
            except Exception:
                pass  # job already fired/cancelled -- expected race, not an error
            self._synced_tick_job = None

    def _tick_synced(self):
        if self.mix_player is not None and not self.mix_player.is_playing():
            self._pause_synced()
            return
        elapsed = time.monotonic() - self._synced_play_t0
        self.sync_wave.set_playhead(elapsed)
        if self.camera_video_source is not None:
            self._advance_video_frame()
        self._synced_tick_job = self.root.after(self._video_tick_interval_ms(), self._tick_synced)

    # ---- export ---------------------------------------------------

    def run_export(self):
        video = self.video_path_var.get().strip()
        output = self.output_path_var.get().strip()
        if not video:
            self.show_error("Choose a camera video file first.")
            return
        if not output:
            self.show_error("Choose where to save the exported file.")
            return
        if not sc.ffmpeg_available():
            self.show_error("ffmpeg/ffprobe not found. Install ffmpeg first.")
            return
        not_ready = [row.label for row in self.audio_rows if not row.preview.ready]
        if not_ready:
            self.show_error("Still loading audio for: " + ", ".join(not_ready) +
                            ". Please wait a moment for it to finish and try again.")
            return

        specs = [row.to_spec() for row in self.audio_rows]
        keep_cam = self.keep_camera_var.get()
        codec = self.codec_var.get()

        self._export_cancelled = False
        self.cancel_btn.state(["!disabled"])
        threading.Thread(target=self._export_worker, args=(video, specs, output, keep_cam, codec),
                         daemon=True).start()

    def cancel_export(self):
        proc = self._export_proc
        if proc is None or proc.poll() is not None:
            return
        self._export_cancelled = True
        self.log("Cancelling export\u2026")
        self.cancel_btn.state(["disabled"])
        try:
            proc.terminate()
        except Exception:
            pass  # process already exited between poll() and here -- expected race, not an error
        # Give ffmpeg a few seconds to exit on its own after SIGTERM before
        # forcing it -- this is scheduled via after() rather than a blocking
        # wait so the GUI thread never stalls on it.
        self.root.after(4000, self._force_kill_export, proc)

    def _force_kill_export(self, proc):
        if proc.poll() is None:
            self.log("ffmpeg did not exit after Cancel; forcing it to stop.")
            try:
                proc.kill()
            except Exception:
                pass  # process already exited between poll() and here -- expected race, not an error

    def _export_worker(self, video, specs, output, keep_cam, codec):
        self._set_busy(True, "Exporting\u2026 this can take a while for uncompressed video.")
        try:
            self.log("Source formats (all audio is normalized to 32-bit float on export):")
            try:
                vinfo = sc.probe_info(video)
                if vinfo.has_audio:
                    self.log(f"  camera on-board audio: {vinfo.audio_format_label}")
            except Exception as exc:
                self.log(f"  camera on-board audio: could not probe format ({exc})")
            for spec in specs:
                try:
                    ainfo = sc.probe_info(spec.path)
                    self.log(f"  {Path(spec.path).name}: {ainfo.audio_format_label}")
                except Exception as exc:
                    self.log(f"  {Path(spec.path).name}: could not probe format ({exc})")

            self.log("Output audio tracks:")
            for track_number, members in sc.group_audio_tracks_by_output(specs):
                names = " + ".join(Path(m.path).name for m in members)
                self.log(f"  Track {track_number}: {names}")
            if keep_cam:
                self.log("  + camera on-board audio (kept as its own additional track)")

            cmd = sc.build_export_command(
                video_path=video, audio_tracks=specs, output_path=output,
                keep_camera_audio=keep_cam, video_codec=codec)
            self.log("Running: " + " ".join(cmd))
            import subprocess
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
            self._export_proc = proc
            for line in proc.stdout:
                self.log(line)
            proc.wait()
            if self._export_cancelled:
                self.log("Export cancelled.")
                try:
                    Path(output).unlink(missing_ok=True)
                    self.log(f"Removed incomplete output file: {output}")
                except Exception as exc:
                    self.log(f"Could not remove incomplete output file {output}: {exc}")
                self.root.after(0, lambda: messagebox.showinfo("Export cancelled",
                                 "Export was cancelled."))
            elif proc.returncode == 0:
                self.log(f"Done. Wrote {output}")
                self.root.after(0, lambda: messagebox.showinfo("Export complete",
                                 f"Synced file exported to:\n{output}"))
            else:
                self.log(f"ffmpeg exited with code {proc.returncode}")
                self.root.after(0, lambda: messagebox.showerror("Export failed",
                                 "ffmpeg reported an error. See the log for details."))
        except Exception:
            self.log(traceback.format_exc())
            self.root.after(0, lambda: messagebox.showerror("Export failed", "See the log for details."))
        finally:
            self._export_proc = None
            self.root.after(0, lambda: self.cancel_btn.state(["disabled"]))
            self._set_busy(False)


def main():
    root = tk.Tk()
    app = SyncApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
