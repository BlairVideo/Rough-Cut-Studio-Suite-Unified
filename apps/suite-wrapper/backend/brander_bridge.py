"""
brander_bridge.py — in-process bridge to Blair Brander's UI-free core.

Blair Brander's rendering stack (brand.py / renderer.py / export.py /
prompt_ai.py / project_io.py / assets.py) is pure Pillow + stdlib — no
Tkinter — so it runs directly inside the suite venv. Only its app.py is
Tkinter, and that is deliberately NEVER imported; the one thing the suite
needs from it (default_scene()) is replicated verbatim below.

sys.path insertion happens AT MODULE IMPORT TIME, not lazily inside a
function: export.export_video uses an internal multiprocessing.Pool, and
on macOS's default "spawn" start method each child process re-imports the
`export`/`renderer` modules by name — the parent's sys.path is inherited by
spawned children, so BRANDER_DIR being on it before any pool is created is
what lets those re-imports succeed.

cwd-independence: brand.py resolves FONT_DIR/ASSET_DIR/OUTPUT_DIR from its
own __file__ (BASE_DIR = dirname(abspath(__file__))), and assets.py/
renderer.py build every file path from those constants — verified, so no
chdir is needed anywhere.

JSON safety: scene["canvas_size"] is a (w, h) tuple in Brander-land but
must cross the JS bridge as a list. scene_to_json / normalize_scene are
the two directions of that conversion; every scene entering a renderer or
export call goes through normalize_scene first.
"""

import os
import sys
import json
import base64
import io
import shutil

try:
    from . import paths
except ImportError:  # pragma: no cover — direct script import in tests
    import paths

if paths.BRANDER_DIR not in sys.path:
    sys.path.insert(0, paths.BRANDER_DIR)

import brand        # noqa: E402
import renderer     # noqa: E402
import export       # noqa: E402
import prompt_ai    # noqa: E402
import project_io   # noqa: E402
import assets       # noqa: E402  (imported so a broken assets folder fails loudly at startup)

from PIL import Image  # noqa: E402

# The standalone app builds this list inline in its Tkinter code (app.py's
# Placement OptionMenu) rather than exposing it as a brand.py constant, so
# it is replicated here verbatim. Scene key: "logo_placement", default
# "bottom-center" (every brand preset also carries a logo_placement).
LOGO_PLACEMENTS = ["top-left", "top-center", "top-right", "center",
                   "bottom-left", "bottom-center", "bottom-right"]
DEFAULT_LOGO_PLACEMENT = "bottom-center"

# ---------------------------------------------------------------------------
# Custom logo registry
#
# assets.py loads a logo as os.path.join(brand.ASSET_DIR, LOGO_SOURCES[name])
# — and os.path.join yields the second argument unchanged when it's absolute,
# so registering an ABSOLUTE path as the LOGO_SOURCES value makes the
# standalone loader (including its white-background keying in
# load_transparent) work on files that live outside the Brander app folder.
# Imported logos are copied into the suite's assets/logos/ and recorded in
# custom_logos.json there ({display_name: absolute_path}) so they survive
# restarts; entries whose file has vanished are skipped on reload.
# ---------------------------------------------------------------------------

CUSTOM_LOGOS_JSON = os.path.join(paths.LOGOS_DIR, "custom_logos.json")

# display name -> absolute path, for every custom logo registered THIS
# process (persisted entries get re-registered at import time below).
_custom_logos = {}


def _persist_custom_logos():
    """Write the registry JSON (pruning entries whose file is gone).
    Best-effort like the rest of the asset plumbing — a failed write only
    costs persistence across restarts, never the in-session registration."""
    try:
        paths.ensure_suite_dirs()
        keep = {name: p for name, p in _custom_logos.items() if os.path.isfile(p)}
        with open(CUSTOM_LOGOS_JSON, "w", encoding="utf-8") as f:
            json.dump(keep, f, indent=2)
    except OSError:
        pass


def _load_custom_logos():
    """Re-register persisted custom logos into brand.LOGO_SOURCES. Called
    once at module import; entries whose file vanished are skipped (and
    dropped from the JSON on the next persist)."""
    try:
        with open(CUSTOM_LOGOS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for name, path in data.items():
        if isinstance(name, str) and isinstance(path, str) and os.path.isfile(path):
            _custom_logos[name] = path
            brand.LOGO_SOURCES[name] = path


def remove_custom_logo(name):
    """Delete a previously-imported custom logo: drops it from
    brand.LOGO_SOURCES and the persisted registry, and best-effort removes
    its file from assets/logos/. Refuses to touch a built-in logo (only
    entries registered this session or restored from custom_logos.json at
    startup are removable). Returns (True, None) or (False, error_message)."""
    if name not in _custom_logos:
        return False, f'"{name}" isn\'t an imported logo — only imported logos can be removed.'
    path = _custom_logos.pop(name)
    brand.LOGO_SOURCES.pop(name, None)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass  # the registry entry is already gone; a leftover file is harmless
    _persist_custom_logos()
    return True, None


def custom_logo_sources():
    """{display_name: absolute_path} of every logo registered THIS session
    (plus whatever _load_custom_logos() restored at import time) — pass to
    export.export_video's extra_logo_sources= so its render workers (fresh
    processes that never ran register_custom_logo themselves) can still
    find a custom/imported logo instead of raising a KeyError."""
    return dict(_custom_logos)


def _unique_logo_name(stem):
    """'Custom: <stem>', with ' 2', ' 3', ... appended on a collision with
    ANY existing logo name (built-in or custom)."""
    base = f"Custom: {stem}".strip()
    name = base
    n = 1
    while name in brand.LOGO_SOURCES:
        n += 1
        name = f"{base} {n}"
    return name


def register_custom_logo(src_path):
    """Copy `src_path` into assets/logos/, register it under a unique
    'Custom: <stem>' display name, persist the registry, and return the
    display name. Raises on I/O failure (callers wrap into their error
    contract). The opt-in white-background keying in
    assets.load_transparent (scene["logo_key_white_bg"]) applies to these
    imports exactly as it does to built-in logos."""
    src_path = os.path.abspath(src_path)
    if not os.path.isfile(src_path):
        raise FileNotFoundError(f"Logo file not found: {src_path}")
    paths.ensure_suite_dirs()

    stem, ext = os.path.splitext(os.path.basename(src_path))
    stem = stem.strip() or "logo"
    dest = os.path.join(paths.LOGOS_DIR, stem + ext)
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(paths.LOGOS_DIR, f"{stem}-{n}{ext}")
    shutil.copy2(src_path, dest)

    name = _unique_logo_name(stem)
    _custom_logos[name] = dest
    brand.LOGO_SOURCES[name] = dest
    _persist_custom_logos()
    return name


_load_custom_logos()


def default_scene():
    """Replicates Blair Brander app.py's default_scene() verbatim (that
    function lives in the Tkinter app module, which must not be imported
    here). Keep in sync with the original if it ever changes."""
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


def normalize_scene(scene):
    """JS -> Python direction: returns a NEW scene dict safe to hand to
    renderer/export — canvas_size coerced back to a (w, h) int tuple (JSON
    round-trips it as a list) with a sane fallback if it's missing or
    mangled."""
    scene = dict(scene or {})
    size = scene.get("canvas_size")
    try:
        w, h = int(size[0]), int(size[1])
        if w <= 0 or h <= 0:
            raise ValueError
        scene["canvas_size"] = (w, h)
    except (TypeError, ValueError, IndexError, KeyError):
        scene["canvas_size"] = tuple(brand.CANVAS_PRESETS[brand.DEFAULT_CANVAS_PRESET])
    return scene


def scene_to_json(scene):
    """Python -> JS direction: tuples become lists so the dict is plain
    JSON. (Scenes only ever nest one level of tuple — canvas_size — but
    convert any tuple value defensively.)"""
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in dict(scene or {}).items()}


def options_dict():
    """Everything the Graphics form needs to build its controls, straight
    from brand.py's constants."""
    return {
        "presets": list(brand.PRESETS.keys()),
        "preset_values": {name: dict(vals) for name, vals in brand.PRESETS.items()},
        "fonts": list(brand.FONTS.keys()),
        "primary_colors": dict(brand.PRIMARY_COLORS),
        "secondary_colors": dict(brand.SECONDARY_COLORS),
        "canvas_presets": {name: list(size) for name, size in brand.CANVAS_PRESETS.items()},
        "layouts": list(brand.LAYOUTS),
        "background_styles": list(brand.BACKGROUND_STYLES),
        "animations": list(brand.ANIMATIONS),
        "outro_animations": list(brand.OUTRO_ANIMATIONS),
        "lower_third_positions": list(brand.LOWER_THIRD_POSITIONS),
        "vignette_shapes": list(brand.VIGNETTE_SHAPES),
        # "None" first — the standalone app's own logo dropdown (app.py's
        # OptionMenu) already prepends this exact sentinel ahead of
        # LOGO_SOURCES, and renderer.py already treats a "None" (or
        # falsy/missing) scene["logo"] as "skip the logo entirely" in both
        # its compositing step and its animation-timing plateau — so this
        # is the ONE built-in sentinel value that's safe to add without
        # touching Blair Brander's own files. Never collides with a
        # user-imported logo's display name, which is always
        # "Custom: <filename>" (see register_custom_logo/_unique_logo_name
        # below), never the bare word "None".
        "logos": ["None"] + list(brand.LOGO_SOURCES.keys()),  # built-ins first, then customs
        "logo_placements": list(LOGO_PLACEMENTS),
        "fps": brand.FPS,
    }


_CHECKER_LIGHT = (58, 59, 64, 255)
_CHECKER_DARK = (42, 43, 48, 255)
_CHECKER_SQUARE = 16


def _checkerboard(width, height):
    """Dark checkerboard backdrop for previewing transparent frames — the
    classic 'this area is alpha' signal, tinted dark to sit comfortably in
    the suite's dark UI."""
    tile = Image.new("RGBA", (_CHECKER_SQUARE * 2, _CHECKER_SQUARE * 2), _CHECKER_LIGHT)
    dark = Image.new("RGBA", (_CHECKER_SQUARE, _CHECKER_SQUARE), _CHECKER_DARK)
    tile.paste(dark, (0, 0))
    tile.paste(dark, (_CHECKER_SQUARE, _CHECKER_SQUARE))
    board = Image.new("RGBA", (width, height))
    for y in range(0, height, tile.height):
        for x in range(0, width, tile.width):
            board.paste(tile, (x, y))
    return board


def frame_to_data_uri(frame, max_width=960):
    """Downscale an RGBA frame, composite it over a checkerboard (so alpha
    is visible), and return (png_data_uri, width, height)."""
    frame = frame.convert("RGBA")
    w, h = frame.size
    max_width = max(64, int(max_width or 960))
    if w > max_width:
        scale = max_width / w
        frame = frame.resize((max_width, max(1, int(round(h * scale)))), Image.LANCZOS)
        w, h = frame.size
    composed = Image.alpha_composite(_checkerboard(w, h), frame)
    buf = io.BytesIO()
    composed.convert("RGB").save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{data}", w, h


def render_preview(scene, t=1.0, max_width=960, elapsed_seconds=None):
    """render_frame at time t (0..1) as a display-ready data URI.
    elapsed_seconds: real elapsed time across the WHOLE clip (duration +
    hold_seconds) — see renderer.render_frame's docstring. Needed for
    effects (currently just logo_grow) that keep animating through the
    hold tail, which t alone can't express (it's clamped to 1.0 there)."""
    frame = renderer.render_frame(
        normalize_scene(scene), t=max(0.0, min(1.0, float(t))),
        elapsed_seconds=None if elapsed_seconds is None else max(0.0, float(elapsed_seconds)))
    return frame_to_data_uri(frame, max_width)


def render_still_preview(scene, max_width=960):
    """render_still (the animation's 'plateau' moment, everything fully
    landed) as a display-ready data URI."""
    frame = renderer.render_still(normalize_scene(scene))
    return frame_to_data_uri(frame, max_width)


def scene_duration_seconds(scene):
    """Total exported video length: animation duration + settled hold."""
    scene = dict(scene or {})
    try:
        duration = max(0.1, float(scene.get("duration", 3.0)))
    except (TypeError, ValueError):
        duration = 3.0
    try:
        hold = max(0.0, float(scene.get("hold_seconds", 1.0)))
    except (TypeError, ValueError):
        hold = 1.0
    return duration + hold
