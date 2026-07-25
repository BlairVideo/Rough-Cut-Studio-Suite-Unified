"""
thumbnails.py

Extracts a single frame from a local video file at a given timestamp, for
the storyboard thumbnails shown on the Cuts tab. Uses the `ffmpeg` binary
already on the editor's machine (the same tool your transcription app uses
for audio extraction) via a plain subprocess call — no network access, no
extra Python dependencies, no writing to disk (the frame is piped straight
into memory and returned as base64 so it can be embedded as a data URI in
the UI, sidestepping pywebview's restrictions on loading local file:// URLs
into the page).

If ffmpeg isn't installed or the frame can't be extracted, functions here
return None / a clear error rather than raising — a missing thumbnail
should never block script or XML generation.
"""

import base64
import shutil
import subprocess

_FFMPEG_PATH = None
_FFMPEG_CHECKED = False


def ffmpeg_available() -> bool:
    global _FFMPEG_PATH, _FFMPEG_CHECKED
    if not _FFMPEG_CHECKED:
        _FFMPEG_PATH = shutil.which("ffmpeg")
        _FFMPEG_CHECKED = True
    return _FFMPEG_PATH is not None


def extract_thumbnail_jpeg(video_path: str, timestamp_seconds: float, max_width: int = 320, timeout: int = 15):
    """
    Returns raw JPEG bytes for a frame near `timestamp_seconds` in
    `video_path`, scaled to `max_width` wide (aspect preserved), or None if
    ffmpeg is missing, the file doesn't exist, or extraction fails.
    """
    if not ffmpeg_available():
        return None

    timestamp_seconds = max(0.0, timestamp_seconds)
    cmd = [
        _FFMPEG_PATH,
        "-ss", f"{timestamp_seconds:.3f}",
        "-i", video_path,
        "-frames:v", "1",
        "-vf", f"scale={max_width}:-2",
        "-q:v", "4",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-loglevel", "error",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def extract_thumbnail_data_uri(video_path: str, timestamp_seconds: float, max_width: int = 320):
    """Same as extract_thumbnail_jpeg, but returns a ready-to-use
    'data:image/jpeg;base64,...' string, or None on failure."""
    jpeg_bytes = extract_thumbnail_jpeg(video_path, timestamp_seconds, max_width)
    if not jpeg_bytes:
        return None
    encoded = base64.b64encode(jpeg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
