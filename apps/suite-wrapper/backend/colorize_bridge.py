"""
colorize_bridge.py — in-process bridge to Colorize's UI-free core
(grade.py / lut.py / ffmpeg_graph.py / project.py, apps/colorize/).

Colorize's core is pure stdlib (no Pillow/numpy/cv2), so — like Blair
Brander's bridge — it runs directly inside the shared suite venv rather
than as a subprocess worker. sys.path insertion happens at MODULE IMPORT
TIME, not lazily inside a function, matching brander_bridge.py's own
reasoning: consistency with every other bridge in this suite, and safety
against a future caller instantiating a job before the path is set.

Naming collision note: apps/colorize's modules are named generically
(`grade`, `lut`, `project`, `ffmpeg_graph`) rather than package-qualified
— verified there is no existing `grade`/`lut`/`project`/`ffmpeg_graph`
top-level import anywhere else in this suite before adding COLORIZE_DIR
to sys.path.

Storage: every Colorize project/preset/LUT is a JSON sidecar under
paths.COLORIZE_ASSETS_DIR (no shared SQLite/settings system exists in
this suite outside CardEater's own DB — see paths.py). LUTS_DIR keeps
both the original imported file (export always samples/bakes against
the ORIGINAL LUT file, matching the suite's "export references original
media" convention — see braw_bridge.py) and a cached WebGL-preview JSON
per LUT id, so colorize_list_luts never has to re-parse the LUT file.
"""

import json
import os
import shutil
import sys
import tempfile
import uuid

try:
    from . import paths
except ImportError:  # pragma: no cover — direct script import in tests
    import paths

if paths.COLORIZE_DIR not in sys.path:
    sys.path.insert(0, paths.COLORIZE_DIR)

# api_shared.py normally performs this same bootstrap, but this module can
# be imported (directly, in tests, or via a different mixin's own import
# order) before api_shared has run -- do it here too, unconditionally and
# idempotently, so `import preview_server` below never depends on import
# order elsewhere in the suite.
if paths.RCS_BACKEND_DIR not in sys.path:
    sys.path.insert(0, paths.RCS_BACKEND_DIR)

import grade as colorize_grade         # noqa: E402
import lut as colorize_lut             # noqa: E402
import ffmpeg_graph as colorize_ffmpeg # noqa: E402
import project as colorize_project     # noqa: E402

from grade import GradeState                                   # noqa: E402
from lut import CubeLut, LutParseError                          # noqa: E402
from ffmpeg_graph import ExportSpec, run_export, OUTPUT_PRESETS # noqa: E402
from project import ColorizeClip, ColorizeProject, GradePreset  # noqa: E402

from rcs_utils import ffprobe_util  # noqa: E402

# Rough Cut Studio's small, dependency-free, loopback-only byte-range HTTP
# server (apps/rough-cut-studio/backend/preview_server.py) -- reused here
# with Colorize's OWN separate instance/token map (not RCS's shared one)
# so removing a Colorize clip's preview token never touches RCS's
# Edit-workspace preview tokens, and vice versa. WKWebView's <video> can't
# reliably load local file:// URLs, but happily plays a loopback http://
# URL with Range support, which is what this server provides.
import preview_server as colorize_preview_server  # noqa: E402

_preview_server = colorize_preview_server.PreviewServer()


def get_preview_url(path):
    """A local http://127.0.0.1 URL colorize.js's <video> element can
    load for `path`, or None if the file doesn't exist."""
    if not path or not os.path.exists(path):
        return None
    return _preview_server.url_for(path)


def forget_preview(path):
    _preview_server.forget(path)


# ---------------------------------------------------------------------------
# small JSON-sidecar helpers
# ---------------------------------------------------------------------------

def _write_json_atomic(path, data):
    """Write-then-rename so a crash mid-write never leaves a truncated
    project/preset/LUT-metadata file behind."""
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _list_json_files(directory):
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(".json") and not name.startswith(".")
    )


# ---------------------------------------------------------------------------
# clip probing
# ---------------------------------------------------------------------------

def probe_clip(path):
    """Duration/fps/resolution/codec for the media bin, via the suite's
    shared ffprobe utility rather than reimplementing ffprobe calls."""
    duration = ffprobe_util.probe_duration_seconds(path)
    fps = ffprobe_util.probe_video_fps(path)
    info = ffprobe_util.probe_json(path, timeout=15, show_streams=True, show_format=True)
    width = height = codec = None
    streams = (info or {}).get("streams") or []
    for s in streams:
        if s.get("codec_type") == "video":
            width = s.get("width")
            height = s.get("height")
            codec = s.get("codec_name")
            break
    return {
        "path": path,
        "duration_seconds": duration,
        "fps": fps,
        "width": width,
        "height": height,
        "codec": codec,
    }


# ---------------------------------------------------------------------------
# LUT library
# ---------------------------------------------------------------------------

def import_lut(source_path):
    """Parses + validates the LUT, copies the original file into
    COLORIZE_LUTS_DIR (export bakes/samples against this copy, never the
    user's original location — protects against the source being on
    removable camera media or later moved), and caches the WebGL preview
    payload alongside it so repeated colorize_list_luts calls never
    re-parse. Returns the LUT's metadata dict, or raises LutParseError /
    OSError for the caller to translate into an {"ok": False} response."""
    paths.ensure_suite_dirs()
    parsed = colorize_lut.parse_lut_file(source_path)  # raises LutParseError on bad input

    lut_id = uuid.uuid4().hex
    ext = os.path.splitext(source_path)[1].lower()
    stored_original = os.path.join(paths.COLORIZE_LUTS_DIR, f"{lut_id}{ext}")
    shutil.copy2(source_path, stored_original)

    name = parsed.title or os.path.splitext(os.path.basename(source_path))[0]
    meta = {
        "id": lut_id,
        "name": name,
        "size": parsed.size,
        "original_filename": os.path.basename(source_path),
        "stored_path": stored_original,
    }
    _write_json_atomic(os.path.join(paths.COLORIZE_LUTS_DIR, f"{lut_id}.json"), meta)
    _write_json_atomic(
        os.path.join(paths.COLORIZE_LUTS_DIR, f"{lut_id}.preview.json"),
        colorize_lut.to_preview_json(parsed),
    )
    return meta


def list_luts():
    metas = []
    for meta_path in _list_json_files(paths.COLORIZE_LUTS_DIR):
        if meta_path.endswith(".preview.json"):
            continue
        try:
            metas.append(_read_json(meta_path))
        except (OSError, json.JSONDecodeError):
            continue  # a corrupt sidecar shouldn't take down the whole library listing
    return sorted(metas, key=lambda m: m.get("name", ""))


def get_lut_preview_json(lut_id):
    """The flat WebGL-upload payload for a cached LUT (see lut.py's
    to_preview_json) -- returns None if the id is unknown."""
    preview_path = os.path.join(paths.COLORIZE_LUTS_DIR, f"{lut_id}.preview.json")
    if not os.path.isfile(preview_path):
        return None
    return _read_json(preview_path)


def resolve_lut_original_path(lut_id):
    meta_path = os.path.join(paths.COLORIZE_LUTS_DIR, f"{lut_id}.json")
    if not os.path.isfile(meta_path):
        return None
    return _read_json(meta_path).get("stored_path")


def load_lut_for_baking(lut_id):
    """The parsed CubeLut for a cached LUT id, for ffmpeg_graph.bake_grade_lut
    to blend against -- re-parses the STORED original (not the JSON
    preview payload, which has already lost the .cube's DOMAIN_MIN/MAX)."""
    stored_path = resolve_lut_original_path(lut_id)
    if not stored_path or not os.path.isfile(stored_path):
        return None
    return colorize_lut.parse_lut_file(stored_path)


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------

def save_project(project_obj: ColorizeProject):
    paths.ensure_suite_dirs()
    path = os.path.join(paths.COLORIZE_PROJECTS_DIR, f"{project_obj.id}.json")
    _write_json_atomic(path, project_obj.to_dict())
    return project_obj.id


def load_project(project_id):
    path = os.path.join(paths.COLORIZE_PROJECTS_DIR, f"{project_id}.json")
    if not os.path.isfile(path):
        return None
    return ColorizeProject.from_dict(_read_json(path))


def list_projects():
    summaries = []
    for path in _list_json_files(paths.COLORIZE_PROJECTS_DIR):
        try:
            data = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        summaries.append({
            "id": data.get("id"),
            "name": data.get("name"),
            "clip_count": len(data.get("clips") or []),
        })
    return sorted(summaries, key=lambda p: p.get("name") or "")


def delete_project(project_id):
    path = os.path.join(paths.COLORIZE_PROJECTS_DIR, f"{project_id}.json")
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


# ---------------------------------------------------------------------------
# grade presets
# ---------------------------------------------------------------------------

def save_preset(preset: GradePreset):
    paths.ensure_suite_dirs()
    path = os.path.join(paths.COLORIZE_PRESETS_DIR, f"{preset.id}.json")
    _write_json_atomic(path, preset.to_dict())
    return preset.id


def list_presets():
    presets = []
    for path in _list_json_files(paths.COLORIZE_PRESETS_DIR):
        try:
            presets.append(GradePreset.from_dict(_read_json(path)).to_dict())
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return sorted(presets, key=lambda p: p.get("name") or "")


def delete_preset(preset_id):
    path = os.path.join(paths.COLORIZE_PRESETS_DIR, f"{preset_id}.json")
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _probe_video_height(path):
    """Narrow, fail-open probe for just the video stream's height -- drives
    share_h264's resolution-aware bitrate tier (see ffmpeg_graph.py's
    _h264_bitrate_for_height). Deliberately its own targeted ffprobe call
    rather than reusing probe_clip()'s fuller probe: probe_clip also probes
    fps via a separate ffprobe invocation that export has no use for."""
    try:
        info = ffprobe_util.probe_json(
            path, timeout=15, select_streams="v:0", show_entries="stream=height")
        streams = (info or {}).get("streams") or []
        return streams[0].get("height") if streams else None
    except Exception:
        return None


def export_clip(clip: ColorizeClip, output_path, output_preset, progress_cb, cancel_event):
    """Runs one clip's graded/trimmed export. Always reads clip.source_path
    directly (never a preview proxy) — matches braw_bridge's "export
    always references original media" convention, which this bridge
    inherits rather than re-decides."""
    creative_lut = None
    if clip.lut_id:
        creative_lut = load_lut_for_baking(clip.lut_id)
        if creative_lut is None:
            raise ValueError(f"LUT '{clip.lut_id}' referenced by this clip is missing or unreadable")

    duration = None
    probed_duration = ffprobe_util.probe_duration_seconds(clip.source_path)
    out_seconds = clip.out_seconds if clip.out_seconds is not None else probed_duration
    if out_seconds is not None:
        duration = max(0.0, out_seconds - clip.in_seconds)

    spec = ExportSpec(
        source_path=clip.source_path,
        output_path=output_path,
        grade=clip.grade,
        in_seconds=clip.in_seconds,
        out_seconds=out_seconds,
        creative_lut=creative_lut,
        preset=output_preset,
        source_height=_probe_video_height(clip.source_path),
    )
    return run_export(spec, total_duration_seconds=duration, progress_cb=progress_cb,
                       cancel_event=cancel_event)
