"""
result_cache.py
On-disk cache of per-clip analysis results, so re-running the app on
the same folder -- or just tweaking segment length, segments-per-clip,
or energy weight -- doesn't require re-decoding every video from
scratch.

Cache file: "<analyzed folder>/.broll_analyzer_cache.json" -- lives
inside the folder you point the app at, one cache file per folder.
It's read and written only within that folder; nothing is uploaded,
sent over a network, or shared anywhere else.

What's cached is the expensive, settings-independent part of analysis:
per-frame technical samples (sharpness, exposure, motion) and, if
computed, the local CLIP "energy" score. The composite score, best
segment(s), and overall score are cheap to recompute from those
samples for any combination of window length / segments-per-clip /
energy weight (see analyzer.rescore_clip), so changing those settings
doesn't invalidate the cache or require touching the source file
again.

Cache entries are keyed by each file's path (relative to the analyzed
folder, so the cache stays valid if the whole folder is moved/copied
elsewhere) plus its size and modification time, so an edited,
replaced, or re-encoded file is automatically treated as new and
re-analyzed rather than served a stale result.
"""

import os
import json
import base64
from dataclasses import asdict
from typing import Dict, Optional, Tuple

from analyzer import ClipResult, FrameSample

CACHE_FILENAME = ".broll_analyzer_cache.json"
CACHE_VERSION = 1


def cache_path_for_folder(folder: str) -> str:
    return os.path.join(folder, CACHE_FILENAME)


def load_cache(folder: str) -> Dict[str, dict]:
    """Best-effort load: any problem reading/parsing the cache file
    (missing, corrupt, wrong version, permissions) just means an empty
    cache -- every clip gets freshly analyzed, exactly as if caching
    didn't exist. Caching is purely a speed optimization and should
    never be a reason the app fails to run."""
    path = cache_path_for_folder(folder)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return {}
        clips = data.get("clips", {})
        return clips if isinstance(clips, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}


def save_cache(folder: str, entries: Dict[str, dict]) -> None:
    """Best-effort save via a temp file + atomic replace, so a crash or
    concurrent run can't leave a half-written, corrupt cache file
    behind. Any failure (read-only folder, disk full, permissions) is
    swallowed -- losing the cache just means slower re-analysis next
    time, not a broken app."""
    path = cache_path_for_folder(folder)
    tmp_path = path + ".tmp"
    payload = {"version": CACHE_VERSION, "clips": entries}
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_path, path)  # atomic on POSIX and Windows
    except OSError:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def file_fingerprint(path: str) -> Optional[Tuple[int, float]]:
    """(size, mtime) used to detect whether a file has changed since
    it was cached. Returns None if the file can't be stat'd (e.g. it
    disappeared between listing the folder and analyzing it)."""
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime
    except OSError:
        return None


def entry_from_result(result: ClipResult, fingerprint: Tuple[int, float]) -> dict:
    """Serialize a successfully-analyzed ClipResult into a cache entry.
    Only the settings-independent fields are stored -- overall_score,
    segments, and best_window_* are recomputed fresh from `samples` on
    load (see analyzer.rescore_clip) rather than cached, since they
    depend on window/segment/energy-weight settings that may differ
    next run."""
    size, mtime = fingerprint
    return {
        "size": size,
        "mtime": mtime,
        "filename": result.filename,
        "duration": result.duration,
        "fps": result.fps,
        "width": result.width,
        "height": result.height,
        "energy_enabled": result.energy_enabled,
        "energy_error": result.energy_error,
        "audio_channels": result.audio_channels,
        "audio_samplerate": result.audio_samplerate,
        "audio_bit_depth": result.audio_bit_depth,
        "audio_channel_layout": result.audio_channel_layout,
        "audio_format_probed": result.audio_format_probed,
        "audio_error": result.audio_error,
        "samples": [asdict(s) for s in result.samples],
        # Stored as base64 since JSON has no binary type. Purely a
        # cosmetic preview frame -- local-only, never uploaded, never
        # written into the exported XML.
        "thumbnail_jpeg_b64": (base64.b64encode(result.thumbnail_jpeg).decode("ascii")
                                if result.thumbnail_jpeg else None),
        "thumbnail_time": result.thumbnail_time,
    }


def result_from_entry(path: str, entry: dict) -> ClipResult:
    """Reconstruct a ClipResult from a cache entry. The caller is
    expected to call analyzer.rescore_clip() on the result afterward to
    populate overall_score/segments/best_window_* for the current
    settings -- this function only restores the raw sampled data."""
    samples = [FrameSample(**s) for s in entry.get("samples", [])]
    thumb_b64 = entry.get("thumbnail_jpeg_b64")
    thumbnail_jpeg = None
    if thumb_b64:
        try:
            thumbnail_jpeg = base64.b64decode(thumb_b64)
        except (ValueError, TypeError):
            thumbnail_jpeg = None
    audio_channels = entry.get("audio_channels", 2)
    audio_channel_layout = entry.get("audio_channel_layout", "Stereo")
    audio_error = entry.get("audio_error")
    if audio_error == "No audio stream found in file" and audio_channels != 0:
        # Self-heal cache entries written before analyzer._probe_audio_format
        # started clearing these fields for a genuinely audio-less source --
        # older entries left the stereo default in place alongside this
        # error, which made xml_export.py fabricate phantom audio media/
        # clipitems for a file that has none. Corrected read-side, like the
        # mtime-tolerance handling above, so existing cache files don't need
        # a full re-decode to pick up the fix.
        audio_channels = 0
        audio_channel_layout = "None"
    return ClipResult(
        path=path,
        filename=entry.get("filename", os.path.basename(path)),
        duration=entry.get("duration", 0.0),
        fps=entry.get("fps", 0.0),
        width=entry.get("width", 0),
        height=entry.get("height", 0),
        samples=samples,
        energy_enabled=entry.get("energy_enabled", False),
        energy_error=entry.get("energy_error"),
        audio_channels=audio_channels,
        audio_samplerate=entry.get("audio_samplerate", 48000),
        audio_bit_depth=entry.get("audio_bit_depth", 16),
        audio_channel_layout=audio_channel_layout,
        audio_format_probed=entry.get("audio_format_probed", False),
        audio_error=entry.get("audio_error"),
        thumbnail_jpeg=thumbnail_jpeg,
        thumbnail_time=entry.get("thumbnail_time"),
    )


def update_thumbnail(folder: str, rel_path: str, thumbnail_jpeg: Optional[bytes],
                      thumbnail_time: Optional[float]) -> None:
    """Patch just one cache entry's thumbnail fields in place, leaving
    everything else already cached for that folder untouched.

    Used to persist a UI-triggered on-demand thumbnail refresh (see
    analyzer.refresh_thumbnail) back to disk immediately, rather than
    only ever being written by the next full Analyze run's batch
    save_cache() call. Without this, selecting a row to get an
    up-to-date preview only updated the in-memory ClipResult -- closing
    and reopening the app (or just clicking Analyze again) would reload
    the older, stale thumbnail from disk and silently discard the
    refresh, forcing the same file to be reseeked again next time.

    Best-effort like the rest of this module: if the folder's cache no
    longer exists, or this particular entry isn't in it (e.g. the file
    was removed from the cache between the refresh starting and
    finishing), this simply does nothing -- the refresh stays
    in-memory-only for the rest of this session, exactly as before this
    function existed, rather than raising or recreating a bogus entry."""
    entries = load_cache(folder)
    entry = entries.get(rel_path)
    if entry is None:
        return
    entry["thumbnail_jpeg_b64"] = (base64.b64encode(thumbnail_jpeg).decode("ascii")
                                    if thumbnail_jpeg else None)
    entry["thumbnail_time"] = thumbnail_time
    save_cache(folder, entries)


def is_entry_usable(entry: Optional[dict], fingerprint: Optional[Tuple[int, float]],
                     need_energy: bool) -> bool:
    """Whether a cache entry can be reused as-is: the file must be
    unchanged (matching size/mtime), and if energy scoring is being
    requested now, the cached samples must already include it (energy
    scoring can't be added to a cache entry that was computed without
    it without re-decoding, since it requires the actual frame
    pixels)."""
    if entry is None or fingerprint is None:
        return False
    size, mtime = fingerprint
    if entry.get("size") != size:
        return False
    # mtime is stored as the raw float st.st_mtime, and floats that have
    # round-tripped through JSON -- or come off filesystems/OSes that
    # quantize timestamps slightly differently between stat calls -- can
    # differ in the last few bits without the file having changed at all.
    # An exact `!=` here turned those phantom differences into full
    # re-decodes, so compare with a tiny absolute tolerance instead.
    # 1e-6 s is far below any real filesystem timestamp granularity, so a
    # genuinely modified file still always misses. Deliberately a
    # read-side-only change: the stored JSON format is untouched, because
    # this cache file is shared with older copies of the app and a format
    # change would invalidate every existing cache.
    cached_mtime = entry.get("mtime")
    if not isinstance(cached_mtime, (int, float)) \
            or abs(cached_mtime - mtime) > 1e-6:
        return False
    if need_energy and not entry.get("energy_enabled"):
        return False
    return True
