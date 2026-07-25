"""
waveform_view.py
-----------------
Pure-numpy peak/RMS downsampling (easy to unit test) plus a Tkinter Canvas
widget that renders a waveform, a movable playhead, and -- for the synced
comparison view -- a draggable track that reports offset nudges.

No audio or video decoding lives here; feed it numpy arrays produced by
media_playback.py or sync_core.py.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np


def compute_peaks(samples: np.ndarray, num_buckets: int) -> np.ndarray:
    """
    Downsample a (possibly multi-channel) float audio array into `num_buckets`
    (min, max) pairs suitable for drawing a waveform at a given pixel width.
    Multi-channel input is averaged to mono first.

    Returns an array of shape (num_buckets, 2): [:,0]=min, [:,1]=max, each in
    [-1, 1]. Empty/degenerate input returns zeros.
    """
    if samples is None or samples.size == 0 or num_buckets <= 0:
        return np.zeros((max(num_buckets, 0), 2), dtype=np.float32)

    if samples.ndim > 1:
        mono = samples.mean(axis=1)
    else:
        mono = samples

    n = mono.shape[0]
    if n == 0:
        return np.zeros((num_buckets, 2), dtype=np.float32)

    # Pad so it divides evenly into num_buckets chunks.
    bucket_size = max(1, int(np.ceil(n / num_buckets)))
    padded_len = bucket_size * num_buckets
    if padded_len > n:
        mono = np.pad(mono, (0, padded_len - n), mode="constant")

    chunks = mono.reshape(num_buckets, bucket_size)
    mins = chunks.min(axis=1)
    maxs = chunks.max(axis=1)
    peaks = np.stack([mins, maxs], axis=1).astype(np.float32)
    return np.clip(peaks, -1.0, 1.0)


@dataclass
class WaveformTrack:
    """One row of waveform data drawn inside a WaveformCanvas."""
    label: str
    peaks: np.ndarray          # shape (num_buckets, 2), pre-computed at canvas width
    duration: float            # seconds spanned by `peaks` (before any offset)
    color: str = "#00b2ba"
    offset_seconds: float = 0.0   # only meaningful in "compare" mode
    draggable: bool = False


class WaveformCanvas(tk.Canvas):
    """
    Renders one or more WaveformTrack rows stacked vertically, all sharing a
    common time axis. Supports:
      - click/drag anywhere to seek -> on_seek(seconds)
      - dragging a `draggable` track horizontally to nudge its offset ->
        on_offset_drag(track_index, delta_seconds)
      - an external playhead position, set via set_playhead(seconds)
    """

    def __init__(self, parent, width=600, height=90, bg="#12181f",
                 on_seek: Optional[Callable[[float], None]] = None,
                 on_offset_drag: Optional[Callable[[int, float], None]] = None,
                 **kwargs):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, **kwargs)
        self.on_seek = on_seek
        self.on_offset_drag = on_offset_drag
        self.tracks: list[WaveformTrack] = []
        self.view_duration = 1.0   # total seconds visible across the canvas width
        self.playhead_seconds = 0.0
        self._drag_track_index: Optional[int] = None
        self._drag_start_x = 0
        self._drag_start_offset = 0.0

        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_tracks(self, tracks: list[WaveformTrack], view_duration: Optional[float] = None):
        self.tracks = tracks
        if view_duration is not None:
            self.view_duration = max(view_duration, 0.001)
        elif tracks:
            self.view_duration = max((t.duration + t.offset_seconds for t in tracks), default=1.0)
        self.redraw()

    def set_playhead(self, seconds: float):
        """
        Cheap update for the playhead position -- called continuously during
        playback (many times a second), so it must NOT redraw the waveform
        bars. Only the single playhead line is touched.
        """
        self.playhead_seconds = seconds
        if self.tracks:
            self._draw_playhead()
        else:
            self.redraw()

    def _time_to_x(self, t: float) -> float:
        w = max(self.winfo_width(), 1)
        return (t / self.view_duration) * w if self.view_duration else 0

    def _x_to_time(self, x: float) -> float:
        w = max(self.winfo_width(), 1)
        return (x / w) * self.view_duration

    def _draw_playhead(self):
        h = max(self.winfo_height(), 1)
        px = self._time_to_x(self.playhead_seconds)
        self.delete("playhead")
        self.create_line(px, 0, px, h, fill="#f15d22", width=2, tags="playhead")

    def redraw(self):
        """Full redraw of the static waveform content (bars, labels) plus the
        playhead. This is the expensive path -- call it when the underlying
        data, offsets, or widget size change, not on every playhead tick.

        Each track is drawn as a single filled polygon (the envelope of its
        bucketed min/max peaks) instead of one canvas line per bucket. With
        up to 600 buckets per track, that was up to 600 separate canvas
        items per track being deleted and recreated on every redraw --
        including on every mouse-move event while dragging a track to nudge
        its offset (see _on_drag). One polygon per track cuts that to a
        single item, independent of bucket count."""
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        if not self.tracks:
            self.create_text(w // 2, h // 2, text="(no audio loaded)",
                             fill="#5b6b76", font=("Helvetica", 9))
            return

        row_h = h / len(self.tracks)
        for i, track in enumerate(self.tracks):
            top = i * row_h
            mid = top + row_h / 2
            self.create_line(0, mid, w, mid, fill="#243040")
            self.create_text(6, top + 10, text=track.label, anchor="w",
                             fill="#8fa3ad", font=("Helvetica", 8))

            peaks = track.peaks
            n = len(peaks)
            if n == 0:
                continue
            # Map each bucket's left-edge time (offset by track.offset_seconds) to x.
            bucket_span = track.duration / n
            amp = (row_h / 2) * 0.85
            t = track.offset_seconds + np.arange(n) * bucket_span
            x = self._time_to_x(t)
            mn, mx = peaks[:, 0], peaks[:, 1]
            y_top = mid - mx * amp
            y_bot = mid - mn * amp
            # Envelope polygon: top edge left->right, then bottom edge right->left.
            poly_x = np.concatenate([x, x[::-1]])
            poly_y = np.concatenate([y_top, y_bot[::-1]])
            coords = np.empty(poly_x.size * 2)
            coords[0::2] = poly_x
            coords[1::2] = poly_y
            self.create_polygon(*coords.tolist(), fill=track.color, outline=track.color)

        self._draw_playhead()

    # ---- interaction -----------------------------------------------

    def _track_index_at_y(self, y: float) -> int:
        h = max(self.winfo_height(), 1)
        row_h = h / max(len(self.tracks), 1)
        idx = int(y // row_h)
        return max(0, min(idx, len(self.tracks) - 1))

    def _on_press(self, event):
        idx = self._track_index_at_y(event.y) if self.tracks else -1
        if idx >= 0 and self.tracks[idx].draggable:
            self._drag_track_index = idx
            self._drag_start_x = event.x
            self._drag_start_offset = self.tracks[idx].offset_seconds
        else:
            self._drag_track_index = None
            if self.on_seek:
                self.on_seek(self._x_to_time(event.x))

    def _on_drag(self, event):
        if self._drag_track_index is not None:
            dx = event.x - self._drag_start_x
            dt = self._x_to_time(dx) - self._x_to_time(0)  # convert pixel delta to seconds delta
            track = self.tracks[self._drag_track_index]
            track.offset_seconds = self._drag_start_offset + dt
            self.redraw()
        elif self.on_seek:
            self.on_seek(self._x_to_time(event.x))

    def _on_release(self, event):
        if self._drag_track_index is not None and self.on_offset_drag:
            track = self.tracks[self._drag_track_index]
            delta = track.offset_seconds - self._drag_start_offset
            self.on_offset_drag(self._drag_track_index, delta)
        self._drag_track_index = None
