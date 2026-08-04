"""
assets.py — local, offline logo processing.

Some supplied logo PNGs have a plain white background instead of true
transparency. This module can key the white out to alpha at load time
(opt-in per scene via `logo_key_white_bg` — see load_transparent's
docstring for why it's not automatic) and caches the result, entirely in
memory / on local disk — no network calls, no external services. It can
also derive a white "knockout" silhouette of any logo for use on dark or
photographic backgrounds.
"""

import os
import hashlib
from PIL import Image, ImageOps, ImageChops

import brand

_cache = {}


def _cache_key(path, mode):
    stat = os.stat(path)
    raw = f"{path}:{mode}:{stat.st_mtime}:{stat.st_size}".encode()
    return hashlib.sha1(raw).hexdigest()


def load_transparent(logo_name, threshold=245, key_white=False):
    """Load a brand logo by its display name, returning an RGBA image.

    White-background keying is opt-in (`key_white=True`) rather than
    automatic: a flat `min(r, g, b) >= threshold` test punches
    transparency through ANY near-white pixel in the whole image, not
    just the actual background, so it can eat legitimate white details
    (lettering, a white ring/border) inside a logo that already has its
    own real alpha channel. Only turn it on for source files that are
    known to be flattened onto a plain white background with no alpha.
    """
    filename = brand.LOGO_SOURCES[logo_name]
    path = os.path.join(brand.ASSET_DIR, filename)
    key = _cache_key(path, f"trans-{threshold}-{key_white}")
    if key in _cache:
        return _cache[key].copy()

    im = Image.open(path).convert("RGBA")
    if key_white:
        r, g, b, a = im.split()
        # A pixel is "white background" iff every channel is >= threshold,
        # i.e. min(r, g, b) >= threshold. Computed at the C level via
        # ImageChops instead of a per-pixel Python loop.
        min_rgb = ImageChops.darker(ImageChops.darker(r, g), b)
        keep_mask = min_rgb.point(lambda v: 0 if v >= threshold else 255)
        im.putalpha(ImageChops.multiply(a, keep_mask))
    _cache[key] = im
    return im.copy()


def load_white_knockout(logo_name, threshold=245, key_white=False):
    """Return a pure-white silhouette version of a logo (alpha preserved),
    for placement on dark or busy backgrounds."""
    key = ("ko", logo_name, threshold, key_white)
    if key in _cache:
        return _cache[key].copy()

    base = load_transparent(logo_name, threshold, key_white)
    r, g, b, a = base.split()
    white = Image.new("RGBA", base.size, (255, 255, 255, 255))
    white.putalpha(a)
    _cache[key] = white
    return white.copy()


def recolor(logo_name, hex_color, threshold=245, key_white=False):
    """Return the logo recolored (flat fill) to an arbitrary brand hex color,
    preserving the original alpha/antialiasing."""
    key = ("recolor", logo_name, hex_color, threshold, key_white)
    if key in _cache:
        return _cache[key].copy()

    base = load_transparent(logo_name, threshold, key_white)
    _, _, _, a = base.split()
    rgb = brand.hex_to_rgb(hex_color)
    flat = Image.new("RGBA", base.size, rgb + (255,))
    flat.putalpha(a)
    _cache[key] = flat
    return flat.copy()


def fit_height(img, target_h):
    w, h = img.size
    if h == 0:
        return img
    scale = target_h / h
    return img.resize((max(1, int(w * scale)), target_h), Image.LANCZOS)
