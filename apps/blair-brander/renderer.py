"""
renderer.py — pure local rendering. No network, no browser, no external
services. Everything is drawn with Pillow onto an RGBA canvas so the
alpha channel (transparency) survives all the way to export.

scene["ai_background_path"] (Studio Suite only — the standalone app never
sets it) points to an already-downloaded local image file; this module
only ever reads that file from disk with Pillow, same as it already does
for logos via assets.py. Whatever produced that file (a network call to
an image-generation API) happens entirely in Studio Suite's own backend,
never here — this module's "no network" invariant is unchanged.
"""

import math
import os
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import brand
import assets


def hex_to_rgba(hex_color, alpha=255):
    r, g, b = brand.hex_to_rgb(hex_color)
    return (r, g, b, alpha)


def darken(hex_color, factor=0.6):
    r, g, b, _ = hex_to_rgba(hex_color)
    return "#{:02x}{:02x}{:02x}".format(int(r * factor), int(g * factor), int(b * factor))


def load_font(font_key, style, size):
    # Thin wrapper so callers can keep passing float sizes (e.g. after the
    # lower-third scale multiplier) — the cache below keys on the *int*
    # pixel size actually handed to FreeType, so 46 and 46.0 share one
    # entry instead of missing.
    return _load_font_cached(font_key, style, max(1, int(size)))


@lru_cache(maxsize=128)
def _load_font_cached(font_key, style, size):
    """ImageFont.truetype() re-parses the font file from disk on every
    call, and load_font() is hit multiple times per frame (subtitle font,
    plus every probe inside fit_font_size). A clip only ever touches a
    handful of (font, style, size) combos, so cache the parsed font —
    same established pattern as _vignette_mask/_gradient_layer below.
    Cheap to warm per spawn worker: a few truetype() calls, not per-frame."""
    path = brand.font_path(font_key, style)
    return ImageFont.truetype(path, size)


# ---------------------------------------------------------------------------
# Easing helpers (all take t in [0,1])
# ---------------------------------------------------------------------------
def ease_out_cubic(t):
    t = max(0.0, min(1.0, t))
    return 1 - pow(1 - t, 3)


def ease_out_back(t):
    t = max(0.0, min(1.0, t))
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * pow(t - 1, 3) + c1 * pow(t - 1, 2)


def ease_in_out_cubic(t):
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 4 * t * t * t
    return 1 - pow(-2 * t + 2, 3) / 2


def ease_in_cubic(t):
    t = max(0.0, min(1.0, t))
    return t * t * t


def clamp01(t):
    return max(0.0, min(1.0, t))


def phase_progress(start, end, t):
    if end <= start:
        return 1.0
    return clamp01((t - start) / (end - start))


def compute_outro(style, outro_p):
    """Returns (alpha_mult, dx, scale_mult) for an exiting element.
    outro_p is that element's own outro phase progress in [0,1]."""
    if style == "none" or outro_p <= 0:
        return 1.0, 0.0, 1.0
    e = ease_in_cubic(outro_p)
    if style == "slide":
        return (1 - e), e * 300, 1.0
    if style == "zoom":
        return (1 - e), 0.0, max(0.05, 1.0 - 0.4 * e)
    if style == "wipe":
        return 1.0, 0.0, 1.0  # handled via mask, not alpha/scale
    # "fade" and fallback
    return (1 - e), 0.0, 1.0


def apply_wipe_mask(layer, frac, w, h):
    """Composite `layer` against transparent using a left-to-right reveal
    mask sized to `frac` of the canvas width (1.0 = fully visible, 0.0 =
    fully hidden). Shared by title/subtitle/logo/divider layers so every
    element wipes using identical mask geometry."""
    if frac >= 1.0:
        return layer
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.rectangle([0, 0, w * max(0.0, frac), h], fill=255)
    transparent = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    return Image.composite(layer, transparent, mask)


# ---------------------------------------------------------------------------
# Text drawing with manual letter-spacing support
# ---------------------------------------------------------------------------
def draw_tracked_text(draw, xy, text, font, fill, tracking=0, anchor_center_x=None):
    widths = [draw.textlength(ch, font=font) for ch in text]
    total_w = sum(widths) + tracking * max(0, len(text) - 1)
    x = anchor_center_x - total_w / 2 if anchor_center_x is not None else xy[0]
    y = xy[1]
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += w + tracking
    return total_w


def tracked_text_width(draw, text, font, tracking=0):
    widths = [draw.textlength(ch, font=font) for ch in text]
    return sum(widths) + tracking * max(0, len(text) - 1)


def fit_font_size(draw, text, font_key, style, start_size, max_width, tracking=0, min_size=28):
    """Shrink font size until the tracked text width fits max_width.

    None of the inputs vary with animation time t, yet render_frame()
    calls this on EVERY frame — and each call re-measures every character
    at every probed size. Delegate to an lru_cache'd pure helper so the
    per-frame cost is a dict lookup. The `draw` argument is kept for
    call-site compatibility but is no longer used for measuring: text
    metrics depend only on the font (and the draw's fontmode, which is
    "L" for every RGBA canvas this app renders), not on which image the
    draw wraps, so the cached helper measures on its own scratch canvas."""
    return _fit_font_size_cached(text, font_key, style, start_size, max_width,
                                 tracking, min_size)


@lru_cache(maxsize=256)
def _fit_font_size_cached(text, font_key, style, start_size, max_width, tracking, min_size):
    # All cache-key inputs are hashable value types (str/int/float) — no
    # dicts, no PIL objects — so identical scenes always hit the same slot.
    draw = ImageDraw.Draw(Image.new("RGBA", (8, 8), (0, 0, 0, 0)))
    size = start_size
    font = load_font(font_key, style, size)
    width = tracked_text_width(draw, text, font, tracking)
    while width > max_width and size > min_size:
        size = max(min_size, int(size * 0.92))
        font = load_font(font_key, style, size)
        width = tracked_text_width(draw, text, font, tracking)
    return font, size


# ---------------------------------------------------------------------------
# Vignette (cached per canvas size + strength)
# ---------------------------------------------------------------------------
# Edge length of the small grid the vignette falloff is evaluated on before
# being upscaled to the full canvas. 256 keeps the worst-case bilinear
# interpolation error of the quadratic falloff (and the Rectangular shape's
# diagonal ridge) at a max per-pixel delta of 2/255 vs. the old full-res
# loop at 1080p — visually identical — while cutting the Python-loop work
# from ~2.07M iterations to at most 65K.
_VIGNETTE_GRID = 256


@lru_cache(maxsize=32)
def _vignette_mask(w, h, strength_pct, shape):
    """Return a grayscale 'L' image: 255 = no darkening (center), lower at
    the edges. strength_pct in [0,100]. shape in brand.VIGNETTE_SHAPES.

    The falloff formula is unchanged, but instead of evaluating it per
    output pixel (a w*h Python loop — ~2.07M iterations at 1080p, paid
    again by every cold spawn worker during video export) it is sampled
    on a small grid and bilinearly upscaled. Each small-grid sample is
    taken at the exact full-resolution coordinate Pillow's BILINEAR
    resize maps that source pixel to ((i + 0.5) * scale - 0.5), so the
    upscale interpolates the true full-res field rather than a shifted
    copy of it. Parity vs. the old loop was measured at 1920x1080 across
    all three shapes and strengths 10/35/60/100: max per-pixel delta 2."""
    strength = strength_pct / 100.0
    cx, cy = w / 2, h / 2
    max_r_circular = min(cx, cy)

    ws, hs = min(w, _VIGNETTE_GRID), min(h, _VIGNETTE_GRID)
    sx, sy = w / ws, h / hs

    small = Image.new("L", (ws, hs), 0)
    px = small.load()
    for ys in range(hs):
        dy = (ys + 0.5) * sy - 0.5 - cy
        for xs in range(ws):
            dx = (xs + 0.5) * sx - 0.5 - cx
            if shape == "Circular":
                r = math.hypot(dx, dy) / max_r_circular
            elif shape == "Rectangular":
                r = max(abs(dx) / cx, abs(dy) / cy)
            else:  # "Elliptical" — dx/dy normalized SEPARATELY by cx/cy
                # (not a shared radius) so the iso-falloff contour is a
                # true ellipse matching the canvas aspect ratio, reaching
                # r=1 at all four edge midpoints. The old formula
                # (hypot(dx, dy) / hypot(cx, cy)) traced circles — same
                # family as "Circular" above, just scaled to the corner
                # distance — so on a non-square canvas it left the
                # top/bottom edges far brighter than the sides instead of
                # vignetting the whole border evenly.
                r = math.hypot(dx / cx, dy / cy)
            darken_amt = clamp01(r ** 2) * strength
            px[xs, ys] = int(255 * darken_amt)
    if (ws, hs) == (w, h):
        return small
    return small.resize((w, h), Image.BILINEAR)


def apply_vignette(canvas, strength_pct, shape="Elliptical"):
    if strength_pct <= 0:
        return canvas
    w, h = canvas.size
    mask = _vignette_mask(w, h, int(strength_pct), shape)
    black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    black.putalpha(mask)
    return Image.alpha_composite(canvas, black)


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------
@lru_cache(maxsize=16)
def _gradient_layer(w, h, base, c2):
    """Diagonal two-color gradient, RGB tuples in, cached per (size, colors)
    since it's fully deterministic but otherwise gets recomputed on every
    render_frame() call (hundreds of times during a video export).

    The old per-pixel Python loop (~2.07M iterations at 1080p, paid again
    by every cold spawn worker) exploited nothing about the shape of the
    gradient; but the pixel value depends only on x + y, which takes just
    w + h - 1 distinct values. So: evaluate the EXACT original formula
    once per anti-diagonal into a per-channel byte ramp, then assemble
    each channel image row-by-row — row y is simply ramp[y : y + w] —
    with C-level bytes slicing. Bit-identical to the old loop (measured
    max per-pixel delta 0 at 1920x1080 for the brand color pairs and
    full-range stress pairs; the min/max clamp matches PIL's own clamping
    of the overshoot where (x + y) / diag exceeds 1.0)."""
    diag = math.hypot(w, h)
    bands = []
    for b0, b1 in zip(base, c2):
        ramp = bytes(
            max(0, min(255, int(b0 + (b1 - b0) * (k / diag))))
            for k in range(w + h - 1)
        )
        bands.append(Image.frombytes("L", (w, h), b"".join(ramp[y:y + w] for y in range(h))))
    bands.append(Image.new("L", (w, h), 255))  # opaque alpha, as before
    return Image.merge("RGBA", bands)


def _cover_fit(img, w, h):
    """Resize `img` to fully cover a w×h box (preserving aspect ratio,
    upscaling if needed) then center-crop the overflow — the same
    "background-size: cover" behavior as CSS, so an arbitrary
    AI-generated image (whatever its native aspect ratio) always fills
    the canvas with no letterboxing, at the cost of cropping some edges."""
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return img.crop((left, top, left + w, top + h))


def render_background(w, h, scene):
    if scene.get("transparent_bg", True):
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))

    ai_path = scene.get("ai_background_path")
    if ai_path and os.path.isfile(ai_path):
        try:
            return _cover_fit(Image.open(ai_path).convert("RGBA"), w, h)
        except Exception:
            pass  # corrupt/unreadable file -- fall through to the normal styles below

    style = scene.get("background_style", "Solid")
    base = hex_to_rgba(scene["bg_color"], 255)

    if style == "Gradient":
        color2 = scene.get("bg_gradient_color") or darken(scene["bg_color"], 0.55)
        c2 = hex_to_rgba(color2, 255)
        return _gradient_layer(w, h, base[:3], c2[:3]).copy()

    return Image.new("RGBA", (w, h), base)


# ---------------------------------------------------------------------------
# Main frame renderer
# ---------------------------------------------------------------------------
def render_frame(scene, t=1.0, elapsed_seconds=None):
    """
    scene: dict — see app.py default_scene() for the full shape.
    t: overall animation progress in [0, 1]. t=1 is the settled end state —
      reached once real elapsed time passes scene["duration"]; every
      in/out timing field (title_in_start, logo_out_end, etc.) is a
      fraction of THIS window, not the hold tail below.
    elapsed_seconds: optional real elapsed time across the WHOLE clip,
      including scene["hold_seconds"] — t alone can't express "still
      moving during the settled hold tail" since it's clamped to 1.0
      there (every other caller relies on that: export._rle_times
      collapses the whole hold tail to one repeated render because t, and
      therefore the frame, doesn't change across it). Only effects that
      specifically need to keep animating through the hold tail
      (currently just logo_grow) read this; every other animation branch
      below is keyed on t exactly as before. None (the default) falls
      back to t-only behavior — safe for callers that only ever render a
      single settled moment (render_still, PNG export) or don't know
      about hold_seconds at all.
    Returns: RGBA PIL.Image sized per scene["canvas_size"].
    """
    W, H = scene.get("canvas_size", brand.CANVAS_SIZE)

    canvas = render_background(W, H, scene)
    draw = ImageDraw.Draw(canvas)

    title = scene.get("title", "")
    subtitle = scene.get("subtitle", "")
    title_display = title.upper() if scene.get("uppercase_title", True) else title

    text_color = scene["text_color"]
    accent_color = scene["accent_color"]

    layout = scene.get("layout", "Full Title Card")
    is_lower_third = layout == "Lower Third"

    title_size = scene.get("title_size", 130 if not is_lower_third else 72)
    subtitle_size = scene.get("subtitle_size", 46 if not is_lower_third else 32)
    tracking = scene.get("letter_spacing", 0)

    lt_scale = 1.0
    if is_lower_third:
        lt_scale = max(0.5, min(1.8, scene.get("lower_third_scale", 1.0)))
        title_size = title_size * lt_scale
        subtitle_size = subtitle_size * lt_scale

    max_text_w = W * (0.88 if not is_lower_third else min(0.85, 0.56 * lt_scale))
    title_font, title_size = fit_font_size(draw, title_display, scene["title_font"], "regular",
                                            title_size, max_text_w, tracking)
    subtitle_font = load_font(scene["subtitle_font"], "italic", subtitle_size)

    cx, cy = W / 2, H / 2

    # Manual title/subtitle repositioning (addendum v22): a flat pixel
    # offset applied to the whole title+subtitle block (and, for Lower
    # Third, its background plate) on top of whatever the layout/position
    # settings already compute — lets an editor nudge text off the
    # default dead-center (or off its Lower Third corner) without a
    # dedicated per-layout control. Defaults to 0 (identical to pre-v22
    # behavior) if the scene doesn't carry these keys.
    text_offset_x = scene.get("text_offset_x", 0)
    text_offset_y = scene.get("text_offset_y", 0)

    anim = scene.get("animation", "fade")
    duration = max(0.1, scene.get("duration", 3.0))

    title_in_start = scene.get("title_in_start", 0.0)
    title_in_end = scene.get("title_in_end", 0.45)
    subtitle_in_start = scene.get("subtitle_in_start", 0.30)
    subtitle_in_end = scene.get("subtitle_in_end", 0.70)
    logo_in_start = scene.get("logo_in_start", 0.55)
    logo_in_end = scene.get("logo_in_end", 0.95)

    title_p = phase_progress(title_in_start, title_in_end, t)
    subtitle_p = phase_progress(subtitle_in_start, subtitle_in_end, t)
    logo_p = phase_progress(logo_in_start, logo_in_end, t)

    title_alpha, title_dx, title_dy, title_scale = 1.0, 0, 0, 1.0
    subtitle_alpha, subtitle_dx = 1.0, 0
    logo_alpha, logo_dy = 1.0, 0
    wipe_frac = 1.0
    stagger_progress = None

    if anim == "fade":
        title_alpha = ease_out_cubic(title_p)
        subtitle_alpha = ease_out_cubic(subtitle_p)
        logo_alpha = ease_out_cubic(logo_p)
        title_dy = (1 - ease_out_cubic(title_p)) * 25

    elif anim == "slide":
        e = ease_out_cubic(title_p)
        title_alpha = e
        title_dx = (1 - e) * -300
        e2 = ease_out_cubic(subtitle_p)
        subtitle_alpha = e2
        subtitle_dx = (1 - e2) * 300
        logo_alpha = ease_out_cubic(logo_p)

    elif anim == "zoom":
        e = ease_out_cubic(title_p)
        title_alpha = e
        title_scale = max(0.05, 1.35 - 0.35 * e)
        subtitle_alpha = ease_out_cubic(subtitle_p)
        logo_alpha = ease_out_cubic(logo_p)

    elif anim == "bounce":
        e = ease_out_back(title_p)
        title_alpha = clamp01(title_p * 3)
        title_scale = max(0.05, e)
        subtitle_alpha = clamp01(subtitle_p * 3)
        logo_alpha = clamp01(logo_p * 3)
        logo_dy = (1 - ease_out_cubic(logo_p)) * 40

    elif anim == "wipe":
        wipe_frac = ease_in_out_cubic(title_p)
        title_alpha = 1.0 if title_p > 0 else 0.0
        subtitle_alpha = ease_out_cubic(subtitle_p)
        logo_alpha = ease_out_cubic(logo_p)

    elif anim == "stagger":
        stagger_progress = title_p
        title_alpha = 1.0
        subtitle_alpha = ease_out_cubic(subtitle_p)
        logo_alpha = ease_out_cubic(logo_p)

    elif anim == "typewriter":
        n_chars = max(1, len(title_display))
        visible = int(round(n_chars * ease_out_cubic(clamp01(title_p * 1.3))))
        title_display = title_display[:visible]
        title_alpha = 1.0
        subtitle_alpha = ease_out_cubic(subtitle_p)
        logo_alpha = ease_out_cubic(logo_p)

    else:  # "none" / static
        pass

    # ---- Outro (exit) animation, layered on top of the intro result ------
    outro_style = scene.get("outro_animation", "none")
    title_out_start = scene.get("title_out_start", 0.80)
    title_out_end = scene.get("title_out_end", 1.0)
    subtitle_out_start = scene.get("subtitle_out_start", 0.78)
    subtitle_out_end = scene.get("subtitle_out_end", 0.98)
    logo_out_start = scene.get("logo_out_start", 0.82)
    logo_out_end = scene.get("logo_out_end", 1.0)

    title_out_p = phase_progress(title_out_start, title_out_end, t)
    subtitle_out_p = phase_progress(subtitle_out_start, subtitle_out_end, t)
    logo_out_p = phase_progress(logo_out_start, logo_out_end, t)

    title_out_alpha, title_out_dx, title_out_scale = compute_outro(outro_style, title_out_p)
    subtitle_out_alpha, subtitle_out_dx, _ = compute_outro(outro_style, subtitle_out_p)
    logo_out_alpha, _, _ = compute_outro(outro_style, logo_out_p)

    title_alpha *= title_out_alpha
    title_dx += title_out_dx
    title_scale *= title_out_scale
    subtitle_alpha *= subtitle_out_alpha
    subtitle_dx += subtitle_out_dx
    logo_alpha *= logo_out_alpha

    def _wipe_out_frac(out_p):
        """Reveal-window fraction for wipe-style outros (1.0 = fully
        visible, 0.0 = fully wiped away), computed from an element's own
        outro phase progress so elements with independent out_start/out_end
        timing (subtitle, logo) wipe out on their own schedule rather than
        title's."""
        if outro_style == "wipe" and out_p > 0:
            return 1.0 - ease_in_out_cubic(out_p)
        return 1.0

    wipe_out_frac = _wipe_out_frac(title_out_p)
    subtitle_wipe_out_frac = _wipe_out_frac(subtitle_out_p)
    logo_wipe_out_frac = _wipe_out_frac(logo_out_p)

    # ---- Layout geometry -------------------------------------------------
    title_bbox = draw.textbbox((0, 0), title_display or " ", font=title_font)
    title_h = title_bbox[3] - title_bbox[1]
    subtitle_h = 0
    sb = None
    if subtitle:
        sb = draw.textbbox((0, 0), subtitle, font=subtitle_font)
        subtitle_h = sb[3] - sb[1]

    divider_h = 6 if scene.get("divider") else 0
    gap = 34 if not is_lower_third else 18
    block_h = title_h + (gap + divider_h + gap if scene.get("divider") else gap) + subtitle_h

    if is_lower_third:
        position = scene.get("lower_third_position", brand.DEFAULT_LOWER_THIRD_POSITION)
        vert = "Top" if position.startswith("Top") else "Bottom"
        horiz = "Center" if "Center" in position else ("Right" if "Right" in position else "Left")

        # Measure actual text so the plate hugs the content at any alignment
        title_actual_w = tracked_text_width(draw, title_display, title_font, tracking)
        subtitle_actual_w = tracked_text_width(draw, subtitle, subtitle_font) if subtitle else 0
        content_w = max(title_actual_w, subtitle_actual_w, 10)

        margin_side = W * 0.06
        margin_v = H * 0.10
        plate_pad = 24 * lt_scale

        if horiz == "Left":
            text_x = margin_side
        elif horiz == "Right":
            text_x = W - margin_side - content_w
        else:
            text_x = cx - content_w / 2
        text_x += text_offset_x
        plate_left = text_x - plate_pad
        plate_right = text_x + content_w + plate_pad

        if vert == "Top":
            plate_top = margin_v - plate_pad * 0.4
            plate_bottom = plate_top + block_h + plate_pad * 2
        else:
            plate_bottom = H - margin_v + plate_pad * 0.4
            plate_top = plate_bottom - block_h - plate_pad * 2
        plate_top += text_offset_y
        plate_bottom += text_offset_y

        plate_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        pd = ImageDraw.Draw(plate_layer)
        plate_color = scene.get("lower_third_bg_color") or scene["bg_color"]
        plate_opacity = clamp01(scene.get("lower_third_bg_opacity", 75) / 100.0)
        plate_alpha = int(255 * plate_opacity * clamp01(max(title_p, 0.05)) * title_out_alpha)
        pd.rectangle([plate_left, plate_top, plate_right, plate_bottom],
                     fill=hex_to_rgba(plate_color, plate_alpha))
        pd.rectangle([plate_left, plate_top, plate_left + 6, plate_bottom],
                     fill=hex_to_rgba(accent_color, plate_alpha))
        canvas = Image.alpha_composite(canvas, plate_layer)
        draw = ImageDraw.Draw(canvas)

        top = plate_top + plate_pad

        # Keep the logo out of the plate's way: if the plate occupies the
        # bottom, push a bottom-anchored logo to the top (and vice versa).
        lp = scene.get("logo_placement", "bottom-center")
        if vert == "Bottom" and lp.startswith("bottom"):
            side = lp.split("-")[1] if "-" in lp and lp.split("-")[1] in ("left", "right", "center") else "right"
            scene = dict(scene)
            scene["logo_placement"] = f"top-{side}"
        elif vert == "Top" and lp.startswith("top"):
            side = lp.split("-")[1] if "-" in lp and lp.split("-")[1] in ("left", "right", "center") else "right"
            scene = dict(scene)
            scene["logo_placement"] = f"bottom-{side}"
    else:
        top = cy - block_h / 2 + text_offset_y
        text_x = 0
        cx = cx + text_offset_x

    # ---- Title layer -------------------------------------------------
    title_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    tl_draw = ImageDraw.Draw(title_layer)
    ty = top - title_bbox[1]

    if stagger_progress is not None:
        _draw_stagger_text(tl_draw, title_display, title_font, hex_to_rgba(text_color, 255),
                            tracking, text_x if is_lower_third else None,
                            None if is_lower_third else cx, ty, stagger_progress)
    else:
        draw_tracked_text(tl_draw, (text_x, ty + title_dy), title_display, title_font,
                           hex_to_rgba(text_color, 255), tracking=tracking,
                           anchor_center_x=None if is_lower_third else (cx + title_dx))

    title_mask_frac = min(wipe_frac, wipe_out_frac)
    if title_mask_frac < 1.0:
        title_layer = apply_wipe_mask(title_layer, title_mask_frac, W, H)

    if title_scale != 1.0:
        nw, nh = max(1, int(W * title_scale)), max(1, int(H * title_scale))
        scaled = title_layer.resize((nw, nh), Image.LANCZOS)
        tmp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        tmp.paste(scaled, (int((W - nw) / 2), int((H - nh) / 2)), scaled)
        title_layer = tmp

    if title_alpha < 1.0:
        r, g, b, a = title_layer.split()
        a = a.point(lambda v: int(v * title_alpha))
        title_layer.putalpha(a)

    canvas = Image.alpha_composite(canvas, title_layer)
    draw = ImageDraw.Draw(canvas)

    # ---- Divider ------------------------------------------------------
    divider_y = top + title_h + gap + divider_h / 2
    if scene.get("divider"):
        div_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dd = ImageDraw.Draw(div_layer)
        div_w = (180 if not is_lower_third else 60) * max(0.05, subtitle_p if anim != "none" else 1.0)
        div_alpha = int(255 * title_out_alpha)
        if is_lower_third:
            dd.rectangle([text_x, divider_y - divider_h / 2, text_x + div_w, divider_y + divider_h / 2],
                         fill=hex_to_rgba(accent_color, div_alpha))
        else:
            dd.rectangle([cx - div_w / 2, divider_y - divider_h / 2, cx + div_w / 2, divider_y + divider_h / 2],
                         fill=hex_to_rgba(accent_color, div_alpha))
        if wipe_out_frac < 1.0:
            div_layer = apply_wipe_mask(div_layer, wipe_out_frac, W, H)
        canvas = Image.alpha_composite(canvas, div_layer)
        draw = ImageDraw.Draw(canvas)

    # ---- Subtitle -------------------------------------------------------
    if subtitle:
        sub_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sub_layer)
        sy = top + title_h + (gap * 2 + divider_h if scene.get("divider") else gap) - sb[1]
        draw_tracked_text(sd, (text_x, sy), subtitle, subtitle_font,
                           hex_to_rgba(text_color, 255), tracking=0,
                           anchor_center_x=None if is_lower_third else (cx + subtitle_dx))
        if subtitle_alpha < 1.0:
            r, g, b, a = sub_layer.split()
            a = a.point(lambda v: int(v * subtitle_alpha))
            sub_layer.putalpha(a)
        if subtitle_wipe_out_frac < 1.0:
            sub_layer = apply_wipe_mask(sub_layer, subtitle_wipe_out_frac, W, H)
        canvas = Image.alpha_composite(canvas, sub_layer)

    # ---- Logo -----------------------------------------------------------
    logo_name = scene.get("logo")
    if logo_name and logo_name != "None":
        color_mode = scene.get("logo_color_mode", "original")
        if color_mode == "white":
            logo_img = assets.load_white_knockout(logo_name)
        elif color_mode == "custom":
            logo_img = assets.recolor(logo_name, scene.get("logo_custom_color", "#ffffff"))
        else:
            logo_img = assets.load_transparent(logo_name)

        logo_h = scene.get("logo_height", 160)
        logo_h = min(logo_h, int(H * 0.85))
        logo_img = assets.fit_height(logo_img, logo_h)

        if scene.get("logo_grow"):
            # Subtle, slow scale-up that keeps easing from 1.0x to a
            # modest +8% for as long as the logo is on screen — the
            # WHOLE clip including the hold tail, not just until t
            # reaches 1.0 (see elapsed_seconds' docstring on render_frame
            # for why t alone can't express that). Applied BEFORE
            # lw/lh/positions are computed below so the logo grows
            # outward from whichever point its placement already anchors
            # (its own center for a "-center"/"center" placement, the
            # margin corner otherwise) rather than drifting toward one
            # corner.
            if elapsed_seconds is None:
                # No real-time info available (a caller that only knows
                # t, e.g. render_still's single settled snapshot) — fall
                # back to the old t-only behavior: growth confined to the
                # pre-hold window, capped once t reaches 1.0.
                grow_p = phase_progress(logo_in_end, 1.0, t)
            else:
                duration_s = max(0.1, scene.get("duration", 3.0))
                total_life_s = duration_s + max(0.0, scene.get("hold_seconds", 1.0))
                grow_p = phase_progress(logo_in_end * duration_s, total_life_s, elapsed_seconds)
            grow_scale = 1.0 + 0.08 * ease_out_cubic(grow_p)
            if grow_scale != 1.0:
                gw, gh = logo_img.size
                logo_img = logo_img.resize(
                    (max(1, round(gw * grow_scale)), max(1, round(gh * grow_scale))),
                    Image.LANCZOS)

        lw, lh = logo_img.size
        margin = max(30, int(W * 0.03))
        placement = scene.get("logo_placement", "bottom-center")
        positions = {
            "top-left": (margin, margin),
            "top-right": (W - lw - margin, margin),
            "top-center": (cx - lw / 2, margin),
            "bottom-left": (margin, H - lh - margin),
            "bottom-right": (W - lw - margin, H - lh - margin),
            "bottom-center": (cx - lw / 2, H - lh - margin),
            "center": (cx - lw / 2, cy - lh / 2),
        }
        lx, ly = positions.get(placement, positions["bottom-center"])
        ly += logo_dy

        if logo_alpha < 1.0:
            r, g, b, a = logo_img.split()
            a = a.point(lambda v: int(v * logo_alpha))
            logo_img.putalpha(a)

        logo_opacity = clamp01(scene.get("logo_opacity", 100) / 100.0)
        if logo_opacity < 1.0:
            r, g, b, a = logo_img.split()
            a = a.point(lambda v: int(v * logo_opacity))
            logo_img.putalpha(a)

        logo_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        logo_layer.paste(logo_img, (int(lx), int(ly)), logo_img)
        if logo_wipe_out_frac < 1.0:
            logo_layer = apply_wipe_mask(logo_layer, logo_wipe_out_frac, W, H)
        canvas = Image.alpha_composite(canvas, logo_layer)

    # ---- Vignette (applied last so it darkens the whole composite) -------
    vignette_strength = scene.get("vignette", 0)
    if vignette_strength:
        canvas = apply_vignette(canvas, vignette_strength, scene.get("vignette_shape", "Elliptical"))

    return canvas


def _draw_stagger_text(draw, text, font, fill, tracking, left_x, center_x, y, progress):
    """Each character fades + rises in with its own staggered start time."""
    widths = [draw.textlength(ch, font=font) for ch in text]
    total_w = sum(widths) + tracking * max(0, len(text) - 1)
    x0 = center_x - total_w / 2 if center_x is not None else left_x

    n = max(1, len(text))
    spread = 0.6
    x = x0
    for i, (ch, w) in enumerate(zip(text, widths)):
        char_start = (i / n) * spread
        char_end = char_start + (1 - spread) + spread / n
        cp = phase_progress(char_start, min(1.0, char_end), progress)
        e = ease_out_cubic(cp)
        dy = (1 - e) * 18
        if e > 0:
            r, g, b, a = fill
            draw.text((x, y + dy), ch, font=font, fill=(r, g, b, int(a * e)))
        x += w + tracking


def plateau_t(scene):
    """The 'fully visible' moment: after every element has finished
    entering, and before any outro exit begins. Falls back to 1.0 when
    there's no outro configured (matches pre-outro behavior)."""
    ins_end = max(
        scene.get("title_in_end", 0.45),
        scene.get("subtitle_in_end", 0.70) if scene.get("subtitle") else 0.0,
        scene.get("logo_in_end", 0.95) if scene.get("logo") and scene.get("logo") != "None" else 0.0,
    )
    if scene.get("outro_animation", "none") == "none":
        return 1.0
    outs_start = min(
        scene.get("title_out_start", 0.80),
        scene.get("subtitle_out_start", 0.78) if scene.get("subtitle") else 1.0,
        scene.get("logo_out_start", 0.82) if scene.get("logo") and scene.get("logo") != "None" else 1.0,
    )
    t_still = (ins_end + outs_start) / 2 if outs_start > ins_end else ins_end
    return clamp01(t_still)


def render_still(scene):
    """Render the frame at the 'fully visible' plateau — after every
    element has finished entering, and before any outro exit begins.
    (t=1.0 alone isn't safe to assume anymore now that outro animations
    can make the very end of the clip faded/slid away.)"""
    return render_frame(scene, t=plateau_t(scene))
