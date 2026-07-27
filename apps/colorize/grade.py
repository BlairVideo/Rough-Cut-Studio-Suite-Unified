"""
grade.py -- GradeState: the one serializable description of a color grade,
plus apply_grade_to_rgb, the pure-Python reference implementation of the
grade pipeline applied to a single RGB triple.

Why a reference implementation lives in Python at all: the live preview
(colorize.js) applies the identical pipeline per-pixel in a GLSL fragment
shader for real-time GPU scrubbing, but export never touches the GPU --
ffmpeg_graph.py samples THIS function across a 3D lattice to bake the
grade into a .cube LUT, which ffmpeg's own lut3d filter then applies
during encode. Keeping one pipeline definition here (rather than hand
-translating GLSL into ffmpeg filter args) is what guarantees the
exported file matches what was previewed -- there is exactly one place
the grade math is defined for export purposes. colorize.js's GLSL must
be kept in step with this function by hand; every stage below is ordered
and named to make that translation mechanical.

Every color value in this module is a float in [0, 1] (linear display
range, not stops/percent) unless documented otherwise -- GradeState's own
fields use the -100..100 / stops-style ranges a grading UI exposes, and
are converted to [0, 1]-space math at the top of apply_grade_to_rgb.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple

# The 8 hue bands a secondary HSL qualifier exposes, in degrees at each
# band's center -- matches the traditional red/orange/yellow/green/aqua/
# blue/purple/magenta wheel used by DaVinci-style HSL curves.
HSL_BANDS = ["red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta"]
_HSL_BAND_CENTERS = [0.0, 30.0, 60.0, 120.0, 180.0, 240.0, 285.0, 315.0]
_HSL_BAND_WIDTH = 45.0  # degrees of falloff on either side of a band center

IDENTITY_CURVE = [[0.0, 0.0], [1.0, 1.0]]


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    """Hermite smoothstep, matching GLSL's built-in exactly -- used for the
    tone-range masks below so colorize.js's shader can call `smoothstep()`
    directly instead of hand-porting this."""
    t = _clamp((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _clamp3(rgb: Tuple[float, float, float]) -> Tuple[float, float, float]:
    r, g, b = rgb
    return (_clamp(r), _clamp(g), _clamp(b))


@dataclass
class GradeState:
    """One clip's full grade. All fields default to a true no-op identity
    grade, so GradeState() round-trips pixels unchanged."""

    # -- Basic corrections --
    exposure: float = 0.0          # stops, typically -4..4
    contrast: float = 0.0          # -100..100, pivoted at 18% grey (0.18)
    temperature: float = 0.0       # -100 (cooler/blue) .. 100 (warmer/orange)
    tint: float = 0.0              # -100 (green) .. 100 (magenta)
    saturation: float = 0.0        # -100..100
    vibrance: float = 0.0          # -100..100, saturation weighted toward low-sat pixels

    # -- Tone range (Lumetri-style), each -100..100. Additive, masked by a
    # smoothstep luma weight so each control only touches its own end of
    # the tonal range -- highlights/shadows have a wide, gentle mask;
    # whites/blacks are narrower and squared for a sharper, more targeted
    # effect near the extreme ends, same distinction Lumetri draws between
    # the two control pairs.
    highlights: float = 0.0
    shadows: float = 0.0
    whites: float = 0.0
    blacks: float = 0.0

    # -- Primary 3-way wheels (lift/gamma/gain), each an (r, g, b) offset --
    # lift: shadow offset, additive, typically -0.25..0.25
    # gamma: midtone power adjustment, multiplicative around 0.5, typically -0.25..0.25
    # gain: highlight multiplier, typically -0.25..0.25 (0 = *1.0)
    lift: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    gamma: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    gain: Tuple[float, float, float] = (0.0, 0.0, 0.0)

    # -- Secondary HSL bands: {band_name: {"hue": -100..100, "sat": -100..100, "lum": -100..100}} --
    hsl: dict = field(default_factory=lambda: {
        band: {"hue": 0.0, "sat": 0.0, "lum": 0.0} for band in HSL_BANDS
    })

    # -- Curves: sorted [x, y] control points in [0, 1], per channel --
    curve_master: List[List[float]] = field(default_factory=lambda: [list(p) for p in IDENTITY_CURVE])
    curve_r: List[List[float]] = field(default_factory=lambda: [list(p) for p in IDENTITY_CURVE])
    curve_g: List[List[float]] = field(default_factory=lambda: [list(p) for p in IDENTITY_CURVE])
    curve_b: List[List[float]] = field(default_factory=lambda: [list(p) for p in IDENTITY_CURVE])

    # -- Creative LUT --
    lut_id: Optional[str] = None       # key into the suite's imported-LUT cache, not a raw path
    lut_intensity: float = 100.0       # 0..100, blended against the pre-LUT graded pixel

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "GradeState":
        if not data:
            return GradeState()
        defaults = GradeState()
        kwargs = {}
        for f in defaults.__dataclass_fields__:
            if f in data and data[f] is not None:
                kwargs[f] = data[f]
        state = GradeState(**kwargs)
        # Tuples survive a JSON round-trip as lists -- normalize back.
        state.lift = tuple(state.lift)
        state.gamma = tuple(state.gamma)
        state.gain = tuple(state.gain)
        return state

    def is_identity(self) -> bool:
        return self.to_dict() == GradeState().to_dict()


def _eval_curve(points: List[List[float]], x: float) -> float:
    """Piecewise Catmull-Rom spline through sorted control points,
    clamped to [0, 1] output -- avoids the overshoot ringing a naive
    Catmull-Rom can produce past the curve's first/last point by
    clamping x to the point range before evaluating."""
    if not points or len(points) < 2:
        return _clamp(x)
    pts = sorted(points, key=lambda p: p[0])
    xs = [p[0] for p in pts]
    x = _clamp(x, xs[0], xs[-1])
    i = bisect.bisect_right(xs, x) - 1
    i = max(0, min(i, len(pts) - 2))
    p0 = pts[i - 1] if i - 1 >= 0 else pts[i]
    p1 = pts[i]
    p2 = pts[i + 1]
    p3 = pts[i + 2] if i + 2 < len(pts) else pts[i + 1]
    x1, y1 = p1
    x2, y2 = p2
    if x2 == x1:
        return _clamp(y1)
    t = (x - x1) / (x2 - x1)
    t2, t3 = t * t, t * t * t
    y0, y3 = p0[1], p3[1]
    y = 0.5 * (
        (2 * y1)
        + (-y0 + y2) * t
        + (2 * y0 - 5 * y1 + 4 * y2 - y3) * t2
        + (-y0 + 3 * y1 - 3 * y2 + y3) * t3
    )
    return _clamp(y)


def _hsl_band_weight(hue_deg: float, band_index: int) -> float:
    """Raised-cosine falloff weight in [0, 1] for how strongly a hue
    belongs to the given HSL band -- smooth, so adjacent-band edits blend
    rather than hard-cutting."""
    center = _HSL_BAND_CENTERS[band_index]
    d = abs(((hue_deg - center) + 180.0) % 360.0 - 180.0)
    if d >= _HSL_BAND_WIDTH:
        return 0.0
    return 0.5 * (1.0 + math.cos(math.pi * d / _HSL_BAND_WIDTH))


def _rgb_to_hsl(r: float, g: float, b: float) -> Tuple[float, float, float]:
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2.0
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        h = (g - b) / d + (6.0 if g < b else 0.0)
    elif mx == g:
        h = (b - r) / d + 2.0
    else:
        h = (r - g) / d + 4.0
    return h * 60.0, s, l


def _hsl_to_rgb(h: float, s: float, l: float) -> Tuple[float, float, float]:
    if s <= 0:
        return l, l, l

    def hue_to_rgb(p, q, t):
        t = t % 1.0
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    h_norm = (h % 360.0) / 360.0
    return (
        hue_to_rgb(p, q, h_norm + 1 / 3),
        hue_to_rgb(p, q, h_norm),
        hue_to_rgb(p, q, h_norm - 1 / 3),
    )


def apply_grade_to_rgb(r: float, g: float, b: float, grade: GradeState) -> Tuple[float, float, float]:
    """Apply the full primary+secondary grade pipeline to one [0,1] RGB
    triple. Stage order matches colorize.js's fragment shader exactly --
    keep both in step by hand if either changes."""

    # 1. Exposure (stops -> multiplicative gain: 2^stops).
    if grade.exposure:
        gain2 = 2.0 ** grade.exposure
        r, g, b = r * gain2, g * gain2, b * gain2

    # 2. White balance: temperature warms/cools via a blue<->red channel
    # tilt, tint shifts green<->magenta -- a linear channel-gain
    # approximation (not full Planckian-locus color science), matching
    # what the GLSL preview shader can cheaply do per-pixel.
    if grade.temperature or grade.tint:
        t = grade.temperature / 100.0
        tn = grade.tint / 100.0
        r *= (1.0 + 0.30 * t)
        b *= (1.0 - 0.30 * t)
        g *= (1.0 + 0.20 * tn)
        r *= (1.0 - 0.10 * tn)
        b *= (1.0 - 0.10 * tn)

    # 3. Lift / gamma / gain (3-way primary wheels). lift = shadow
    # offset, gamma = midtone power, gain = highlight multiplier -- the
    # same semantics as ffmpeg's colorbalance filter and every
    # DaVinci-style color wheel set.
    lr, lg, lb = grade.lift
    gr, gg, gb = grade.gamma
    hr, hg, hb = grade.gain
    r = (r + lr) * (1.0 + hr)
    g = (g + lg) * (1.0 + hg)
    b = (b + lb) * (1.0 + hb)
    r = r ** (1.0 / max(0.05, 1.0 + gr)) if r > 0 else r
    g = g ** (1.0 / max(0.05, 1.0 + gg)) if g > 0 else g
    b = b ** (1.0 / max(0.05, 1.0 + gb)) if b > 0 else b

    # 4. Contrast, pivoted at 18% grey.
    if grade.contrast:
        c = 1.0 + (grade.contrast / 100.0)
        pivot = 0.18
        r = (r - pivot) * c + pivot
        g = (g - pivot) * c + pivot
        b = (b - pivot) * c + pivot

    r, g, b = _clamp3((r, g, b))

    # 5. Tone range (highlights/shadows/whites/blacks): additive shift
    # applied equally to r/g/b (no tinting), weighted by luma-based
    # smoothstep masks so each control only affects its own end of the
    # tonal range. Whites/blacks use a squared, narrower mask than
    # highlights/shadows for a sharper, more targeted effect near the
    # extremes -- the same distinction Lumetri draws between the two pairs.
    if grade.highlights or grade.shadows or grade.whites or grade.blacks:
        luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
        w_highlights = _smoothstep(0.35, 1.0, luma)
        w_shadows = 1.0 - _smoothstep(0.0, 0.65, luma)
        w_whites = _smoothstep(0.6, 1.0, luma) ** 2
        w_blacks = (1.0 - _smoothstep(0.0, 0.4, luma)) ** 2
        delta = 0.5 * (
            (grade.highlights / 100.0) * w_highlights
            + (grade.shadows / 100.0) * w_shadows
            + (grade.whites / 100.0) * w_whites
            + (grade.blacks / 100.0) * w_blacks
        )
        r, g, b = r + delta, g + delta, b + delta
        r, g, b = _clamp3((r, g, b))

    # 6. HSL secondary bands, then master saturation/vibrance, all done
    # in HSL space in one round-trip to avoid repeated conversions.
    hsl_active = any(
        grade.hsl.get(band, {}).get(k, 0.0)
        for band in HSL_BANDS for k in ("hue", "sat", "lum")
    )
    if hsl_active or grade.saturation or grade.vibrance:
        h, s, l = _rgb_to_hsl(r, g, b)
        if hsl_active:
            dh = ds = dl = 0.0
            for i, band in enumerate(HSL_BANDS):
                w = _hsl_band_weight(h, i)
                if w <= 0.0:
                    continue
                edit = grade.hsl.get(band, {})
                dh += w * (edit.get("hue", 0.0) / 100.0) * 30.0
                ds += w * (edit.get("sat", 0.0) / 100.0)
                dl += w * (edit.get("lum", 0.0) / 100.0)
            h = (h + dh) % 360.0
            s = _clamp(s + ds)
            l = _clamp(l + dl)
        if grade.saturation:
            s = _clamp(s * (1.0 + grade.saturation / 100.0))
        if grade.vibrance:
            # Vibrance protects already-saturated pixels: effect strength
            # falls off as s approaches 1.
            s = _clamp(s + (grade.vibrance / 100.0) * (1.0 - s))
        r, g, b = _hsl_to_rgb(h, s, l)

    r, g, b = _clamp3((r, g, b))

    # 7. Curves -- master applied last on top of the per-channel curves,
    # matching the standard "channel curves feed the master curve" order.
    if grade.curve_r != IDENTITY_CURVE:
        r = _eval_curve(grade.curve_r, r)
    if grade.curve_g != IDENTITY_CURVE:
        g = _eval_curve(grade.curve_g, g)
    if grade.curve_b != IDENTITY_CURVE:
        b = _eval_curve(grade.curve_b, b)
    if grade.curve_master != IDENTITY_CURVE:
        r = _eval_curve(grade.curve_master, r)
        g = _eval_curve(grade.curve_master, g)
        b = _eval_curve(grade.curve_master, b)

    return _clamp3((r, g, b))
