"""
api_shared.py — constants and tiny helpers shared across the SuiteApi
mixins (contract A-1). Import-order-safe: needs only paths — but it DOES
perform the RCS sys.path bootstrap, so any module that imports api_shared
first can then import RCS backend modules bare.
"""

import os
import sys

try:
    from . import paths
except ImportError:  # pragma: no cover — direct script import in tests
    import paths

# RCS's backend modules import each other bare (`from sources import
# SourceManager`), so its backend DIR itself must be on sys.path — same
# bootstrap RCS's own main.py performs. Done HERE because api_shared is
# every mixin's first suite import.
if paths.RCS_BACKEND_DIR not in sys.path:
    sys.path.insert(0, paths.RCS_BACKEND_DIR)

__all__ = [
    "WHISPER_MODELS", "DEFAULT_WHISPER_LABEL",
    "KEYRING_SERVICE", "KEYRING_HF_TOKEN_KEY",
    "BRANDER_KEYRING_SERVICE", "BRANDER_KEYRING_GEMINI_KEY",
    "RCS_KEYRING_SERVICE", "RCS_KEYRING_GEMINI_KEY",
    "VIDEO_DIALOG_TYPES", "LOGO_DIALOG_TYPES", "LOGO_IMPORT_EXTENSIONS",
    "BROLL_CACHE_FILENAME", "EXPORT_XML_TIMEOUT_SECONDS",
    "IVT_CACHE_SUFFIX", "PREVIEW_VIDEO_EXTENSIONS",
    "SYNC_PREVIEW_AUDIO_EXTENSIONS", "SYNC_VIDEO_DIALOG_TYPES",
    "SYNC_AUDIO_DIALOG_TYPES", "SYNC_METHODS",
    "SYNC_PROBE_TIMEOUT_SECONDS", "SYNC_PEAKS_TIMEOUT_SECONDS",
    "SYNC_OFFSETS_SUFFIX",
    "HARMONIZE_REF_DIALOG_TYPES", "HARMONIZE_TAKE_DIALOG_TYPES",
    "HARMONIZE_REPORT_SUFFIX",
    "_first_path", "_all_paths",
]


def _first_path(result):
    """create_file_dialog returns None, a string, or a tuple/list of
    strings depending on platform/dialog type — normalize to one path.
    (Same helper RCS's sources.py uses.)"""
    if not result:
        return None
    if isinstance(result, (list, tuple)):
        return result[0] if result else None
    return result


def _all_paths(result):
    if not result:
        return []
    if isinstance(result, (list, tuple)):
        return list(result)
    return [result]


# The four mlx-whisper models, mirroring the transcriber app.py's
# WHISPER_MODELS verbatim (duplicated here because importing that app.py
# requires streamlit, which deliberately isn't in the suite venv — the
# transcribe worker's --selfcheck asserts the dict still exists over there).
WHISPER_MODELS = {
    "Fast (tiny, lower accuracy)": "mlx-community/whisper-tiny-mlx",
    "Balanced (small)": "mlx-community/whisper-small-mlx",
    "Recommended (medium)": "mlx-community/whisper-medium-mlx",
    "Best quality (large-v3)": "mlx-community/whisper-large-v3-mlx",
}
DEFAULT_WHISPER_LABEL = "Recommended (medium)"

KEYRING_SERVICE = "InterviewTranscriber"   # same store the standalone app uses
KEYRING_HF_TOKEN_KEY = "hf_token"

# Blair Brander's OWN Gemini key — a separate keychain service/entry from
# Rough Cut Studio's shared .env-based key (RCS's inherited
# load_saved_api_key()/save_api_key_to_disk()), so the two workspaces never
# share credentials.
BRANDER_KEYRING_SERVICE = "BlairBrander"
BRANDER_KEYRING_GEMINI_KEY = "gemini_api_key"

# Rough Cut Studio's Gemini key — historically persisted in a plaintext
# .env next to the RCS app (save_api_key_to_disk/load_saved_api_key). The
# suite overrides those two inherited methods to store it in the system
# keychain instead (its own service, distinct from Brander's), and scrubs
# the legacy plaintext copy on first read. See MASTER_BLUEPRINT.md §SEC-1.
RCS_KEYRING_SERVICE = "RoughCutStudio"
RCS_KEYRING_GEMINI_KEY = "gemini_api_key"

VIDEO_DIALOG_TYPES = (
    # pywebview's file-filter validator only accepts \w and spaces in the
    # description (see webview/util.py parse_file_type) -- no "/", "&", etc.
    # .braw included (Phase 3, addendum v51) so a BRAW source is directly
    # pickable here instead of only via "All files" -- transcribe_worker.py
    # already resolves it through its cached proxy (Phase 2, addendum v50).
    "Video and audio files (*.mp4;*.mov;*.mxf;*.avi;*.mkv;*.m4v;*.mp3;*.wav;*.braw)",
    "All files (*.*)",
)

LOGO_DIALOG_TYPES = (
    # Dialog-label rule: descriptions must match ^[\w ]+$ before the (...)
    # — letters, digits, underscore, spaces ONLY.
    "PNG or JPEG image (*.png;*.jpg;*.jpeg)",
    "All files (*.*)",
)
LOGO_IMPORT_EXTENSIONS = (".png", ".jpg", ".jpeg")

BROLL_CACHE_FILENAME = ".broll_analyzer_cache.json"
EXPORT_XML_TIMEOUT_SECONDS = 300

# Same suffix + schema as the standalone transcriber's CACHE_SUFFIX — the
# .ivt-cache.json next to each video is the shared, editable truth for a
# finished transcription (both apps read/write it).
IVT_CACHE_SUFFIX = ".ivt-cache.json"

# Mirrors RCS transcript_parser.VIDEO_EXTENSIONS — the same container set
# its _is_allowed_media_path() gate enforces before anything is handed to
# ffmpeg or served over the local preview HTTP server.
PREVIEW_VIDEO_EXTENSIONS = (".mp4", ".mov", ".mxf", ".avi", ".mkv", ".m4v")

# Sync-workspace audio containers the local preview server may serve to an
# <audio> element (addendum v4 A). The RCS PreviewServer already does
# mimetype + byte-range for these — only the real-file + extension gate here
# lets anything reach it.
SYNC_PREVIEW_AUDIO_EXTENSIONS = (".wav", ".aif", ".aiff", ".mp3", ".m4a",
                                 ".flac", ".caf")

# ---- Sync workspace (contract addendum v3) ----
# Dialog descriptions obey the same ^[\w ]+$ rule as the others.
SYNC_VIDEO_DIALOG_TYPES = (
    # .braw included (Phase 3, addendum v51) -- sync_worker.py already
    # resolves it through its cached proxy (Phase 2, addendum v30).
    "Video files (*.mp4;*.mov;*.mxf;*.avi;*.mkv;*.m4v;*.braw)",
    "All files (*.*)",
)
SYNC_AUDIO_DIALOG_TYPES = (
    "Audio files (*.wav;*.aif;*.aiff;*.mp3;*.m4a;*.flac;*.caf)",
    "All files (*.*)",
)
SYNC_METHODS = ("waveform", "timecode")
SYNC_PROBE_TIMEOUT_SECONDS = 60
# Decoding audio for waveform peaks is heavier than a plain ffprobe
# metadata read (SYNC_PROBE_TIMEOUT_SECONDS), so this gets its own,
# slightly more generous budget for the same "one call, several files"
# batch shape.
SYNC_PEAKS_TIMEOUT_SECONDS = 90
# Suite-owned sidecar next to the video (A-Sync itself persists nothing);
# best-effort read/write like the IVT cache.
SYNC_OFFSETS_SUFFIX = ".sync-offsets.json"

# ---- Harmonize workspace (Harmonizer integration) ----
HARMONIZE_REF_DIALOG_TYPES = (
    "Audio files (*.wav;*.aif;*.aiff;*.mp3;*.m4a)",
    "All files (*.*)",
)
HARMONIZE_TAKE_DIALOG_TYPES = (
    # .braw included so takes are directly pickable -- align.py's own
    # load_mono() already resolves it via the Blackmagic RAW SDK; export to
    # Resolve is the part that's still gated on non-BRAW takes (v1).
    "Video and audio files (*.mp4;*.mov;*.mxf;*.m4v;*.wav;*.braw)",
    "All files (*.*)",
)
# Suite-owned sidecar next to the reference file (Harmonizer's prototype
# scripts persist nothing) so switching workspaces or relaunching doesn't
# force a costly re-analysis of the same reference + takes.
HARMONIZE_REPORT_SUFFIX = ".harmonize-report.json"
