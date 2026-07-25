"""
assets.py — local, offline logo processing.

Blair's supplied PNGs have a plain white background instead of true
transparency. This module keys the white out to alpha at load time and
caches the result, entirely in memory / on local disk — no network calls,
no external services. It can also derive a white "knockout" silhouette of
any logo for use on dark or photographic backgrounds.
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


def load_transparent(logo_name, threshold=245):
    """Load a brand logo by its display name and return an RGBA image with
    the white background keyed out to transparency."""
    filename = brand.LOGO_SOURCES[logo_name]
    path = os.path.join(brand.ASSET_DIR, filename)
    key = _cache_key(path, f"trans-{threshold}")
    if key in _cache:
        return _cache[key].copy()

    im = Image.open(path).convert("RGBA")
    r, g, b, a = im.split()
    # A pixel is "white background" iff every channel is >= threshold, i.e.
    # min(r, g, b) >= threshold. Computed at the C level via ImageChops
    # instead of a per-pixel Python loop.
    min_rgb = ImageChops.darker(ImageChops.darker(r, g), b)
    keep_mask = min_rgb.point(lambda v: 0 if v >= threshold else 255)
    im.putalpha(ImageChops.multiply(a, keep_mask))
    _cache[key] = im
    return im.copy()


def load_white_knockout(logo_name, threshold=245):
    """Return a pure-white silhouette version of a logo (alpha preserved),
    for placement on dark or busy backgrounds."""
    key = ("ko", logo_name, threshold)
    if key in _cache:
        return _cache[key].copy()

    base = load_transparent(logo_name, threshold)
    r, g, b, a = base.split()
    white = Image.new("RGBA", base.size, (255, 255, 255, 255))
    white.putalpha(a)
    _cache[key] = white
    return white.copy()


def recolor(logo_name, hex_color, threshold=245):
    """Return the logo recolored (flat fill) to an arbitrary brand hex color,
    preserving the original alpha/antialiasing."""
    key = ("recolor", logo_name, hex_color, threshold)
    if key in _cache:
        return _cache[key].copy()

    base = load_transparent(logo_name, threshold)
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
