"""
timeline.py — a small, dependency-free draggable timeline widget built on
tk.Canvas. Lets the editor drag when the title, subtitle, and logo start
and finish animating IN, and separately when they animate OUT, plus
scrub a playhead through the clip.

Designed to be robust against: window/parent resizing, rapid or sloppy
dragging, duration changing underneath it, and zero-width segments.
"""

import tkinter as tk

import brand

ROW_DEFS = [
    {"label": "Title", "color": "#5f8fb4",
     "in": ("title_in_start", "title_in_end", (0.0, 0.45)),
     "out": ("title_out_start", "title_out_end", (0.80, 1.0))},
    {"label": "Subtitle", "color": "#dd971a",
     "in": ("subtitle_in_start", "subtitle_in_end", (0.30, 0.70)),
     "out": ("subtitle_out_start", "subtitle_out_end", (0.78, 0.98))},
    {"label": "Logo", "color": "#74a333",
     "in": ("logo_in_start", "logo_in_end", (0.55, 0.95)),
     "out": ("logo_out_start", "logo_out_end", (0.82, 1.0))},
]

ROW_H = 30
ROW_GAP = 10
TOP_PAD = 10
AXIS_H = 26
HANDLE_W = 10
LEFT_LABEL_W = 74
RIGHT_PAD = 14
MIN_GAP = 0.03
CROSS_GAP = 0.03   # min space kept between a row's "in" and "out" segments so
                   # their handles never touch/overlap and become impossible
                   # to grab individually


def _lighten(hex_color, amount=0.55):
    r, g, b = brand.hex_to_rgb(hex_color)
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


class Timeline(tk.Canvas):
    def __init__(self, parent, width=620, get_scene=None, on_drag=None, on_scrub=None, **kwargs):
        n_rows = len(ROW_DEFS)
        height = TOP_PAD + n_rows * (ROW_H + ROW_GAP) + AXIS_H + 12
        bg = kwargs.pop("bg", "#26272b")
        super().__init__(parent, width=width, height=height, background=bg,
                          highlightthickness=1, highlightbackground="#3f4045", **kwargs)
        self.get_scene = get_scene
        self.on_drag = on_drag
        self.on_scrub = on_scrub
        self._drag = None
        self._scrub_t = 1.0
        self._width = width

        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Motion>", self._on_hover)
        self.bind("<Double-Button-1>", self._on_double_click)
        self.bind("<Configure>", self._on_resize)

        self._recompute_plot_bounds(width)

    # ------------------------------------------------------------------
    def _recompute_plot_bounds(self, width):
        self._width = max(200, width)
        self.plot_x0 = LEFT_LABEL_W
        self.plot_x1 = self._width - RIGHT_PAD

    def _on_resize(self, event):
        if event.width and abs(event.width - self._width) > 2:
            self._recompute_plot_bounds(event.width)
            self.redraw()

    # ------------------------------------------------------------------
    def set_scrub(self, t):
        self._scrub_t = max(0.0, min(1.0, t))
        self.redraw()

    def _duration(self):
        scene = self.get_scene()
        return max(0.1, scene.get("duration", 3.0))

    def _x_for_frac(self, frac):
        return self.plot_x0 + frac * (self.plot_x1 - self.plot_x0)

    def _frac_for_x(self, x):
        span = max(1, self.plot_x1 - self.plot_x0)
        return max(0.0, min(1.0, (x - self.plot_x0) / span))

    def _row_y(self, i):
        return TOP_PAD + i * (ROW_H + ROW_GAP)

    def _snap(self, frac):
        # Snap to the nearest exportable video frame rather than a flat
        # percentage — this tracks the mouse more smoothly on longer clips
        # (where 1% would be a coarse ~100ms+ step) while still landing on
        # a value that actually corresponds to a distinct rendered frame.
        frame_frac = 1.0 / max(1, brand.FPS * self._duration())
        return round(frac / frame_frac) * frame_frac

    def _segment_vals(self, scene, row, which):
        start_key, end_key, default = row[which]
        return (scene.get(start_key, default[0]), scene.get(end_key, default[1]))

    # ------------------------------------------------------------------
    def redraw(self):
        self.delete("all")
        scene = self.get_scene() if self.get_scene else {}
        duration = self._duration()

        for i, row in enumerate(ROW_DEFS):
            y0 = self._row_y(i)
            y1 = y0 + ROW_H
            self.create_rectangle(self.plot_x0, y0, self.plot_x1, y1,
                                   fill="#333438", outline="")
            self.create_text(6, (y0 + y1) / 2, text=row["label"], anchor="w",
                              font=("TkDefaultFont", 9), fill="#cfcfcf")

            in_start, in_end = self._segment_vals(scene, row, "in")
            out_start, out_end = self._segment_vals(scene, row, "out")

            ix0, ix1 = self._x_for_frac(in_start), self._x_for_frac(in_end)
            ox0, ox1 = self._x_for_frac(out_start), self._x_for_frac(out_end)

            self.create_rectangle(ix0, y0 + 3, ix1, y1 - 3, fill=row["color"], outline="")
            self.create_rectangle(ox0, y0 + 3, ox1, y1 - 3, fill=_lighten(row["color"]), outline="",
                                   stipple="gray50")

            for x in (ix0, ix1):
                self.create_rectangle(x - HANDLE_W / 2, y0, x + HANDLE_W / 2, y1,
                                       fill="#ffffff", outline="#222")
            for x in (ox0, ox1):
                self.create_rectangle(x - HANDLE_W / 2, y0, x + HANDLE_W / 2, y1,
                                       fill="#dddddd", outline="#222")

            self.create_text(ix0, y0 - 2, text=f"in {in_start*duration:.1f}s", anchor="sw",
                              font=("TkDefaultFont", 7), fill="#9a9a9a")
            self.create_text(ox1, y0 - 2, text=f"out {out_end*duration:.1f}s", anchor="se",
                              font=("TkDefaultFont", 7), fill="#9a9a9a")

        axis_y = self._row_y(len(ROW_DEFS))
        self.create_line(self.plot_x0, axis_y, self.plot_x1, axis_y, fill="#555")
        n_ticks = 6
        for k in range(n_ticks + 1):
            frac = k / n_ticks
            x = self._x_for_frac(frac)
            self.create_line(x, axis_y, x, axis_y + 5, fill="#555")
            self.create_text(x, axis_y + 7, text=f"{frac*duration:.1f}s", anchor="n",
                              font=("TkDefaultFont", 7), fill="#888")

        px = self._x_for_frac(self._scrub_t)
        self.create_line(px, 0, px, axis_y, fill="#e0574a", width=2, tags=("playhead",))
        self.create_polygon(px - 5, 0, px + 5, 0, px, 8, fill="#e0574a", tags=("playhead",))

    # ------------------------------------------------------------------
    def _hit_test(self, x, y):
        axis_y = self._row_y(len(ROW_DEFS))
        if y > axis_y - 6:
            return ("axis", None, None)
        scene = self.get_scene()
        for i, row in enumerate(ROW_DEFS):
            y0 = self._row_y(i)
            y1 = y0 + ROW_H
            if not (y0 - 6 <= y <= y1 + 6):
                continue
            for which in ("in", "out"):
                s0, s1 = self._segment_vals(scene, row, which)
                x0, x1 = self._x_for_frac(s0), self._x_for_frac(s1)
                if abs(x - x0) <= HANDLE_W:
                    return (i, which, "left")
                if abs(x - x1) <= HANDLE_W:
                    return (i, which, "right")
            for which in ("in", "out"):
                s0, s1 = self._segment_vals(scene, row, which)
                x0, x1 = self._x_for_frac(s0), self._x_for_frac(s1)
                if x0 < x < x1:
                    return (i, which, "move")
            return None
        return None

    def _on_hover(self, event):
        if self._drag:
            return
        hit = self._hit_test(event.x, event.y)
        if hit is None:
            self.configure(cursor="")
        elif hit[0] == "axis":
            self.configure(cursor="sb_h_double_arrow")
        elif hit[2] in ("left", "right"):
            self.configure(cursor="sb_h_double_arrow")
        else:
            self.configure(cursor="fleur")

    def _on_double_click(self, event):
        hit = self._hit_test(event.x, event.y)
        if hit is None or hit[0] == "axis":
            return
        row_i, which, _mode = hit
        scene = self.get_scene()
        start_key, end_key, default = ROW_DEFS[row_i][which]
        scene[start_key], scene[end_key] = default
        self.redraw()
        if self.on_drag:
            self.on_drag(fast=False)

    def _on_press(self, event):
        hit = self._hit_test(event.x, event.y)
        if hit is None:
            return
        if hit[0] == "axis":
            frac = self._frac_for_x(event.x)
            self._scrub_t = frac
            self.redraw()
            if self.on_scrub:
                self.on_scrub(frac)
            self._drag = ("axis", None, None, None)
            return
        row_i, which, mode = hit
        scene = self.get_scene()
        start_key, end_key, default = ROW_DEFS[row_i][which]
        vals = (scene.get(start_key, default[0]), scene.get(end_key, default[1]))
        self._drag = (row_i, which, mode, event.x, vals)

    def _on_motion(self, event):
        if not self._drag:
            return
        row_i, which, mode, start_x, start_vals = self._drag

        if row_i == "axis":
            frac = self._frac_for_x(event.x)
            self._scrub_t = frac
            self.redraw()
            if self.on_scrub:
                self.on_scrub(frac)
            return

        start_key, end_key, _default = ROW_DEFS[row_i][which]
        start_f0, end_f0 = start_vals
        dx = event.x - start_x
        dfrac = dx / max(1, (self.plot_x1 - self.plot_x0))

        scene = self.get_scene()
        # The "in" segment must always stay before the "out" segment (with a
        # small gap) so their handles never land on top of each other.
        other_which = "out" if which == "in" else "in"
        other_start, other_end = self._segment_vals(scene, ROW_DEFS[row_i], other_which)

        if mode == "left":
            new_start = max(0.0, min(end_f0 - MIN_GAP, start_f0 + dfrac))
            if which == "out":
                new_start = min(max(new_start, other_end + CROSS_GAP), end_f0 - MIN_GAP)
            scene[start_key] = self._snap(new_start)
        elif mode == "right":
            new_end = min(1.0, max(start_f0 + MIN_GAP, end_f0 + dfrac))
            if which == "in":
                new_end = max(min(new_end, other_start - CROSS_GAP), start_f0 + MIN_GAP)
            scene[end_key] = self._snap(new_end)
        elif mode == "move":
            width = max(MIN_GAP, end_f0 - start_f0)
            new_start = max(0.0, min(1.0 - width, start_f0 + dfrac))
            if which == "in":
                new_start = max(0.0, min(new_start, other_start - CROSS_GAP - width))
            else:
                new_start = min(1.0 - width, max(new_start, other_end + CROSS_GAP))
            scene[start_key] = self._snap(new_start)
            scene[end_key] = self._snap(new_start + width)

        self.redraw()
        if self.on_drag:
            self.on_drag(fast=True)

    def _on_release(self, event):
        if self._drag and self._drag[0] != "axis" and self.on_drag:
            self.on_drag(fast=False)
        self._drag = None
