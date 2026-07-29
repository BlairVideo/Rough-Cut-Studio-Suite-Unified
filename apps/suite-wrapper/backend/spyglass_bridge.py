"""
spyglass_bridge.py — thin wrapper around the compiled `spyglass_core`
PyO3 extension: Spyglass's Rust engine (crates/spyglass-engine +
crates/spyglass-core), linked directly into this suite's Python process
via crates/spyglass-py's PyO3 bindings — in-process, not a subprocess/
JSON-RPC sidecar. This is the integration mechanism decided in Spyglass's
own architecture plan (Section 19.2): "linking the core crate directly."

Unlike every other bridge module in this suite (harmonizer_bridge.py,
brander_bridge.py), there is no Python source here to treat as a
reference implementation or subprocess target — `spyglass_core` IS the
engine, compiled straight from Spyglass's own crates via `maturin develop`
into this venv's site-packages (a first for this suite: no other app
needs a compiled-extension install step; see the Suite README for the
build command). This module's only job is: (1) call `spyglass_core.init`
exactly once, lazily-but-idempotently, so a missing/not-yet-built
extension degrades to a clear per-call error instead of breaking every
OTHER workspace's tab at suite startup; (2) give api_spyglass.py plain
Python functions to call, matching the rest of this suite's bridge-module
convention, rather than having the mixin import spyglass_core directly.

The Python ML sidecar Spyglass's own engine shells out to for CLIP/VLM
analysis (crates/spyglass-engine's gap-fill worker -> apps/spyglass/sidecar
via subprocess) is unrelated to and unaffected by any of this — that
subprocess relationship exists inside the Rust engine regardless of
whether the engine itself is reached from a Tauri command or this bridge.
"""

import base64
import mimetypes
import os
import traceback

try:
    from . import paths
except ImportError:  # pragma: no cover — direct script import in tests
    import paths

try:
    import spyglass_core as _spyglass_core
    _IMPORT_ERROR = None
except ImportError as e:  # extension not built into this venv yet
    _spyglass_core = None
    _IMPORT_ERROR = e

_initialized = False

# Mirrors Spyglass's own standalone Tauri shell's MAX_CONCURRENCY/
# MIN_IDLE_SECONDS constants (src-tauri/src/lib.rs) — not yet exposed as a
# suite-level setting, same "fixed conservative default for now" precedent
# every other per-app constant here follows.
_MAX_CONCURRENCY = 2
_MIN_IDLE_SECONDS = 20.0


def _require_extension():
    if _spyglass_core is None:
        raise RuntimeError(
            "The spyglass_core extension isn't built into this venv yet — "
            "run `maturin develop` in apps/spyglass/crates/spyglass-py "
            f"against this suite's shared .venv. ({_IMPORT_ERROR})"
        )
    return _spyglass_core


def ensure_initialized():
    """Starts the engine on first use (idempotent). Deliberately not run
    at module-import time -- importing this module (e.g. transitively via
    suite_api.py's mixin composition) must never fail just because
    Spyglass's extension isn't built yet; only an actual Spyglass call
    should surface that as an error, the same way every other mixin's
    methods degrade individually rather than taking the whole suite down."""
    global _initialized
    if _initialized:
        return
    sc = _require_extension()
    sc.init(paths.SPYGLASS_APP_DATA_DIR, paths.SPYGLASS_SIDECAR_DIR, _MAX_CONCURRENCY, _MIN_IDLE_SECONDS)
    _initialized = True


def try_eager_init():
    """Best-effort init call for SuiteApi.__init__ (mirrors CardEaterState's
    eager start_watcher() and Spyglass's own Tauri `setup()`, which both
    start their background loops unconditionally at launch, not lazily on
    first use). Swallows and logs any failure instead of raising, so a
    not-yet-built extension degrades to "the Search tab doesn't work yet"
    rather than the whole suite failing to launch."""
    try:
        ensure_initialized()
    except Exception:
        traceback.print_exc()


def _keyframe_data_uri(path):
    """Reads a keyframe JPEG Spyglass's own Rust engine already extracted
    at index time and returns a 'data:image/...;base64,...' string, or
    None if there's no path or the file can't be read. Spyglass's own
    Tauri shell loads `keyframe_path` via `convertFileSrc` (see
    ShotCard.tsx) -- pywebview has no equivalent asset protocol, and this
    suite's page is served over pywebview's built-in HTTP server (see
    main.py's `_rewrite_link_hrefs` docstring), where WKWebView blocks
    file:// subresources on an http-origin page. A bare `file://` <img
    src> therefore renders nothing. Mirrors rough-cut-studio's
    thumbnails.extract_thumbnail_data_uri, but reads a keyframe already on
    disk instead of invoking ffmpeg on demand."""
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _attach_keyframe_data_uris(results):
    for r in results:
        r["keyframe_data_uri"] = _keyframe_data_uri(r.get("keyframe_path"))
    return results


def _offline_root_dirs():
    """Watched-root paths whose backing volume isn't currently mounted, per
    `list_watched_roots`'s own `is_online` (`Path::exists()`, computed fresh
    on every call -- see spyglass-py/src/lib.rs). Normalized + `os.sep`-
    suffixed so prefix matching in `_is_under_offline_root` can't conflate
    sibling roots with a shared string prefix (e.g. "/Volumes/DriveA" vs.
    "/Volumes/DriveA2")."""
    return [
        os.path.normpath(root["path"]) + os.sep
        for root in _spyglass_core.list_watched_roots()
        if not root.get("is_online", True)
    ]


def _is_under_offline_root(file_path, offline_root_dirs):
    normalized = os.path.normpath(file_path) + os.sep
    return any(normalized.startswith(root_dir) for root_dir in offline_root_dirs)


def _filter_offline_results(results):
    """Drops shots whose clip lives under a watched root that's currently
    unreachable (drive unplugged) -- there's no watched_root_id FK on
    `clips`/`shots` (see spyglass-core's schema), so membership is inferred
    by path prefix, same as the Rust scanner does for its own root-removal
    cascade (`path_is_under_any`)."""
    offline_root_dirs = _offline_root_dirs()
    if not offline_root_dirs:
        return results
    return [r for r in results if not _is_under_offline_root(r.get("clip_file_path", ""), offline_root_dirs)]


def search_shots(query, filters, limit=None):
    ensure_initialized()
    results = _filter_offline_results(_spyglass_core.search_shots(query, filters or {}, limit))
    return _attach_keyframe_data_uris(results)


def browse_shots(filters, limit=None):
    ensure_initialized()
    results = _filter_offline_results(_spyglass_core.browse_shots(filters or {}, limit))
    return _attach_keyframe_data_uris(results)


def find_similar_shots(shot_id):
    ensure_initialized()
    results = _filter_offline_results(_spyglass_core.find_similar_shots(int(shot_id)))
    return _attach_keyframe_data_uris(results)


def list_facet_options():
    ensure_initialized()
    return _spyglass_core.list_facet_options()


def list_folder_children(parent_path=None):
    ensure_initialized()
    return _spyglass_core.list_folder_children(parent_path)


def list_favorite_shots():
    ensure_initialized()
    results = _filter_offline_results(_spyglass_core.list_favorite_shots())
    return _attach_keyframe_data_uris(results)


# ---------------- Tag correction / favoriting ----------------

def add_tag(shot_id, label):
    ensure_initialized()
    return _spyglass_core.add_tag(int(shot_id), label)


def remove_tag(shot_id, label):
    ensure_initialized()
    return _spyglass_core.remove_tag(int(shot_id), label)


def set_shot_favorite(shot_id, favorite):
    ensure_initialized()
    return _spyglass_core.set_shot_favorite(int(shot_id), bool(favorite))


def purge_onscreen_text_tags():
    ensure_initialized()
    return _spyglass_core.purge_onscreen_text_tags()


def backfill_recorded_at():
    """One-off repair for clips registered before `recorded_at` existed (or
    scanned while its ffprobe/mtime probe failed): re-probes every clip
    still missing it and fills the column in, so `SortBy::NewestFirst`/
    `OldestFirst` reflect real footage capture dates instead of falling
    back to `ingested_at` (scan time) for the whole archive -- see
    spyglass_core::db::backfill_recorded_at's doc comment. Safe to re-run;
    never touches a clip that already has a `recorded_at`."""
    ensure_initialized()
    return _spyglass_core.backfill_recorded_at()


# ---------------- Pool tray ----------------

def get_pool():
    ensure_initialized()
    return _spyglass_core.get_pool()


def add_shot_to_pool(shot_id):
    ensure_initialized()
    return _spyglass_core.add_shot_to_pool(int(shot_id))


def remove_shot_from_pool(shot_id):
    ensure_initialized()
    return _spyglass_core.remove_shot_from_pool(int(shot_id))


def reorder_pool(shot_ids):
    ensure_initialized()
    return _spyglass_core.reorder_pool([int(s) for s in shot_ids])


def clear_pool():
    ensure_initialized()
    return _spyglass_core.clear_pool()


def export_pool_to_premiere_xml(destination_path, sequence_name):
    ensure_initialized()
    return _spyglass_core.export_pool_to_premiere_xml(destination_path, sequence_name)


# ---------------- Watched roots ----------------

def list_watched_roots():
    ensure_initialized()
    return _spyglass_core.list_watched_roots()


def add_watched_root(label, path, volume_id=None, approved_by=None):
    ensure_initialized()
    return _spyglass_core.add_watched_root(label, path, volume_id, approved_by)


def set_watched_root_access_level(root_id, access_level):
    ensure_initialized()
    return _spyglass_core.set_watched_root_access_level(int(root_id), access_level)


def remove_watched_root(root_id):
    ensure_initialized()
    return _spyglass_core.remove_watched_root(int(root_id))


def reset_watched_root(root_id):
    ensure_initialized()
    return _spyglass_core.reset_watched_root(int(root_id))


def requeue_short_shot_clips():
    ensure_initialized()
    return _spyglass_core.requeue_short_shot_clips()


def relink_watched_root(root_id, new_path):
    ensure_initialized()
    return _spyglass_core.relink_watched_root(int(root_id), new_path)


def scan_watched_root(root_id):
    ensure_initialized()
    return _spyglass_core.scan_watched_root(int(root_id))


# ---------------- Index backup / restore ----------------

def backup_index(dest_path):
    ensure_initialized()
    return _spyglass_core.backup_index(dest_path)


def restore_index(backup_path):
    ensure_initialized()
    return _spyglass_core.restore_index(backup_path)


# ---------------- Gap-fill queue ----------------

def retry_failed_jobs(root_id=None):
    ensure_initialized()
    return _spyglass_core.retry_failed_jobs(int(root_id) if root_id is not None else None)


def set_queue_paused(paused):
    ensure_initialized()
    return _spyglass_core.set_queue_paused(bool(paused))


def get_queue_paused():
    ensure_initialized()
    return _spyglass_core.get_queue_paused()


def force_gap_fill_now():
    ensure_initialized()
    return _spyglass_core.force_gap_fill_now()


def get_background_work_status():
    ensure_initialized()
    return _spyglass_core.get_background_work_status()


# ---------------- Consolidate & Copy export ----------------

def estimate_consolidate_export(destination_path, copy_mode):
    ensure_initialized()
    return _spyglass_core.estimate_consolidate_export(destination_path, copy_mode)


def start_consolidate_export(destination_path, pool_name, copy_mode, folder_structure):
    ensure_initialized()
    return _spyglass_core.start_consolidate_export(destination_path, pool_name, copy_mode, folder_structure)


def get_consolidate_export_status():
    ensure_initialized()
    return _spyglass_core.get_consolidate_export_status()


def export_copied_files_to_premiere_xml(destination_path, sequence_name):
    ensure_initialized()
    return _spyglass_core.export_copied_files_to_premiere_xml(destination_path, sequence_name)


# ---------------- Native video preview (Phase 4) ----------------
#
# These three thin wrappers exist so api_spyglass.py never has to `import
# spyglass_core` directly (same convention as every function above) --
# but unlike those, the CALLER (api_spyglass.py) carries real
# responsibility for how these get invoked: every one of these must run
# on the main thread (AppKit isn't safe to touch from pywebview's own
# js_api worker thread), which `debug_webview_frame`/
# `open_native_video_preview`/`close_native_video_preview` all enforce on
# the Rust side via `MainThreadMarker` -- calling from the wrong thread
# raises cleanly rather than corrupting AppKit state. See
# api_spyglass.py's `_run_on_main_thread` for the actual dispatch.

def debug_webview_frame(view_ptr):
    ensure_initialized()
    return _spyglass_core.debug_webview_frame(view_ptr)


def open_native_video_preview(view_ptr, path, start_tc, x, y, width, height):
    ensure_initialized()
    return _spyglass_core.open_native_video_preview(view_ptr, path, start_tc, x, y, width, height)


def close_native_video_preview():
    ensure_initialized()
    return _spyglass_core.close_native_video_preview()
