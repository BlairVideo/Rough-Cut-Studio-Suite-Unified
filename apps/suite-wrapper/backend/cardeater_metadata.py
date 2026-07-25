"""
cardeater_metadata.py — resolves each source file's creation-time metadata.

Python port of Card Eater's own metadata.rs: batches files through the local
`exiftool` binary (never a cloud service), falling back to the filesystem's
own creation/modification time when exiftool is missing or a specific file
has no usable date field.
"""

import os
import json
import subprocess
from datetime import datetime, timezone

# exiftool's own datetime format: colon-separated date, no timezone.
EXIF_DATE_FMT = "%Y:%m:%d %H:%M:%S"

# Chunk size for exiftool invocations. Keeps the argument list well under
# ARG_MAX even for cards with tens of thousands of files, while still
# amortizing process-spawn overhead across a large batch.
CHUNK_SIZE = 200


def resolve_created_at_batch(paths):
    """Returns {path: (created_at_rfc3339_or_None, source)} for every path,
    source in {"exif", "filesystem", "unavailable"}."""
    results = {}
    for i in range(0, len(paths), CHUNK_SIZE):
        chunk = paths[i:i + CHUNK_SIZE]
        exif_results = _run_exiftool(chunk)
        if exif_results is not None:
            for path in chunk:
                results[path] = exif_results.get(path) or _fallback_to_filesystem(path)
        else:
            # exiftool missing or failed entirely for this chunk: fall back per-file.
            for path in chunk:
                results[path] = _fallback_to_filesystem(path)
    return results


def _run_exiftool(chunk):
    """Runs `exiftool -j` over a chunk of paths and returns a map of path ->
    resolved (created_at, source) for files that had a usable date field.
    Returns None if the exiftool binary itself could not be run (missing)
    or output couldn't be parsed as JSON at all."""
    try:
        proc = subprocess.run(
            ["exiftool", "-j", "-DateTimeOriginal", "-CreateDate", "-FileModifyDate", *chunk],
            capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    try:
        entries = json.loads(proc.stdout)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(entries, list):
        return None

    result = {}
    for entry in entries:
        source_file = entry.get("SourceFile")
        if not source_file:
            continue
        raw = (
            entry.get("DateTimeOriginal")
            or entry.get("CreateDate")
            or entry.get("FileModifyDate")
        )
        if raw:
            rfc3339 = _parse_exif_datetime(raw)
            if rfc3339:
                result[source_file] = (rfc3339, "exif")
                continue
        # No usable date field / unparseable value: leave for filesystem fallback.
    return result


def _parse_exif_datetime(raw):
    # ExifTool sometimes appends a timezone offset (e.g.
    # "2026:07:14 10:23:45-04:00"); strip anything after the seconds field
    # before parsing with the plain format.
    base = raw[:19]
    try:
        naive = datetime.strptime(base, EXIF_DATE_FMT)
    except ValueError:
        return None
    # Treat as local time (matches the original's Local.from_local_datetime,
    # falling back to system local offset on any ambiguity).
    local = naive.astimezone()
    return local.isoformat()


# Numeric ('#') tags disable exiftool's print-conversion so values come
# back as plain numbers/strings (seconds, pixels) rather than formatted
# text like "0:00:12" or "1920x1080" — easier for the viewer panel to
# format itself.
_EXTENDED_TAGS = (
    "-ImageWidth#", "-ImageHeight#", "-Duration#", "-VideoFrameRate#",
    "-FileType", "-MIMEType", "-Make", "-Model",
)

_EXTENDED_METADATA_EMPTY = {
    "available": False, "width": None, "height": None,
    "duration_secs": None, "frame_rate": None,
    "file_type": None, "mime_type": None,
    "camera_make": None, "camera_model": None,
}


def resolve_extended_metadata(path):
    """Best-effort technical metadata (dimensions, duration, frame rate,
    camera make/model) for a single file, via a one-off `exiftool -j` call
    — used on-demand by the Copy workspace's viewer panel, not the batched
    creation-date resolution above (running this eagerly for every file on
    a large card would slow scanning down for a field only shown once a
    file is actually being viewed). Never raises: any exiftool failure
    (missing binary, corrupt/unsupported file, unparseable output) yields
    an all-None result with available=False rather than an error, same
    graceful-degradation contract as resolve_created_at_batch."""
    try:
        proc = subprocess.run(
            ["exiftool", "-j", *_EXTENDED_TAGS, path],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return dict(_EXTENDED_METADATA_EMPTY)

    try:
        entries = json.loads(proc.stdout)
    except (ValueError, UnicodeDecodeError):
        return dict(_EXTENDED_METADATA_EMPTY)
    if not isinstance(entries, list) or not entries:
        return dict(_EXTENDED_METADATA_EMPTY)

    entry = entries[0]
    return {
        "available": True,
        "width": entry.get("ImageWidth"),
        "height": entry.get("ImageHeight"),
        "duration_secs": entry.get("Duration"),
        "frame_rate": entry.get("VideoFrameRate"),
        "file_type": entry.get("FileType"),
        "mime_type": entry.get("MIMEType"),
        "camera_make": entry.get("Make"),
        "camera_model": entry.get("Model"),
    }


def _fallback_to_filesystem(path):
    try:
        st = os.stat(path)
    except OSError:
        return (None, "unavailable")
    # macOS/BSD stat exposes true birthtime; fall back to mtime elsewhere.
    ts = getattr(st, "st_birthtime", None)
    if ts is None:
        ts = st.st_mtime
    rfc3339 = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return (rfc3339, "filesystem")
