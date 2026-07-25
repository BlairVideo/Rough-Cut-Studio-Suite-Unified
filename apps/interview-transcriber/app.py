"""
Local Video Interview Transcriber
----------------------------------
A privacy-focused, fully local desktop tool (Streamlit GUI) for batch
transcribing + diarizing long-form video interviews on Apple Silicon.

Pipeline per file (strict sequential queue):
  1. Extract audio-only stream to a temp WAV (ffmpeg)
  2. Transcribe with mlx-whisper (Apple Metal accelerated)
  3. Diarize with pyannote.audio (local, free w/ HF access token)
  4. Merge transcript segments with speaker labels
  5. Delete temp audio, gc.collect() to release Unified Memory
  6. Repeat for next file

Nothing ever leaves the machine except the one-time pyannote model
download from Hugging Face (a free API/service used only to fetch
model weights, not to process your audio).
"""

import os
import gc
import re
import csv
import json
import time
import base64
import shutil
import functools
import tempfile
import subprocess
import copy
from io import StringIO
from dataclasses import dataclass, field

import streamlit as st

from rcs_utils.ffprobe_util import probe_duration_seconds

# ----------------------------------------------------------------------
# LOCAL PERSISTENCE
# Nothing is ever sent anywhere. Non-sensitive settings (model, format,
# etc.) live in the user's own Application Support folder; per-video
# transcript caches are stored alongside each video file itself.
# ----------------------------------------------------------------------
APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/InterviewTranscriber")
SETTINGS_PATH = os.path.join(APP_SUPPORT_DIR, "settings.json")
KEYRING_SERVICE = "InterviewTranscriber"
KEYRING_HF_TOKEN_KEY = "hf_token"
CACHE_SUFFIX = ".ivt-cache.json"

# ----------------------------------------------------------------------
# THEME / CSS
# Matches Blair Academy's official Graphic & Editorial Style Guide:
# primary color palette (Blair Blue PMS 288, Dark Blue PMS 534, Cool/Warm
# Grey, Athletic Blue) plus secondary accents (Orange, Red, Yellow,
# Green, Teal), and the two primary typefaces used in Blair's print and
# electronic materials (Avenir Next LT Pro for UI/body text, Adobe
# Garamond Pro as a serif accent). Colors and type choices are general
# design-system elements; the actual Blair seal/wordmark are trademarked
# and copyrighted, so they're deliberately NOT redrawn here — see
# render_header_logo() / render_sidebar_logo() below, which display the
# real approved asset once it's placed in the branding/ folder.
# ----------------------------------------------------------------------
CUSTOM_CSS = """
<style>
:root {
    /* Primary palette */
    --primary: #004b8d;        /* Blair Blue — PMS 288 */
    --primary-dark: #093266;   /* Dark Blue — PMS 534 */
    --athletic-blue: #002244;  /* Athletic Blue — PMS 289 */
    --cool-grey: #72808a;      /* Cool Grey — PMS 430 */
    --warm-grey: #99928a;      /* Warm Grey — PMS 402 */
    /* Secondary accents */
    --accent-orange: #f15d22;  /* Orange — PMS 1665 */
    --accent-red: #da1a32;     /* Red — PMS 186 */
    --accent-yellow: #dd971a;  /* Yellow — PMS 131 */
    --accent-green: #74a333;   /* Green — PMS 377 */
    --accent-teal: #00b2ba;    /* Teal — PMS 7466 */
    /* Surfaces (Web Grey 1 & 2) */
    --surface: #f1f3f4;
    --surface-alt: #e9e7e5;
    --text-main: #1c2126;
    --text-muted: #4a555f;
    /* Typography */
    --font-sans: 'Avenir Next LT Pro', 'Avenir Next', 'Helvetica Neue', Arial, sans-serif;
    --font-serif: 'Adobe Garamond Pro', Garamond, 'Times New Roman', serif;
}
html, body, [class*="css"]  {
    font-family: var(--font-sans);
}
.stApp {
    background-color: var(--surface);
    color: var(--text-main);
}
/* App header / logo area — logo and title must be true DOM siblings
   inside one flex container for align-items:center to do anything;
   rendering them in separate st.columns (as an earlier version did)
   silently breaks this, since each column is an independent block. */
.blair-header {
    display: flex;
    align-items: center;
    gap: 0.85em;
    padding-bottom: 0.6em;
    margin-bottom: 0.4em;
    border-bottom: 3px solid var(--primary);
}
.blair-header .blair-logo-img {
    height: 48px;
    width: auto;
    display: block;
    border-radius: 8px;
    flex-shrink: 0;
}
.blair-header .blair-logo-placeholder {
    width: 48px;
    height: 48px;
    border: 1.5px dashed var(--cool-grey);
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.55em;
    color: var(--cool-grey);
    text-align: center;
    line-height: 1.1;
    flex-shrink: 0;
    font-family: var(--font-sans);
}
.blair-header .blair-title-block h1 {
    font-family: var(--font-serif);
    margin: 0 !important;
    line-height: 1.1;
    border: none !important;
}
/* Sidebar logo: centered, fixed modest size regardless of sidebar width */
.blair-sidebar-logo {
    display: flex;
    justify-content: center;
    margin: 0.2em 0 1em 0;
}
.blair-sidebar-logo-img {
    width: 132px;
    height: auto;
    border-radius: 14px;
}
.blair-sidebar-logo-placeholder {
    width: 100%;
    padding: 0.7em 0.5em;
    margin-bottom: 0.6em;
    border: 1.5px dashed rgba(255, 255, 255, 0.4);
    border-radius: 6px;
    text-align: center;
    font-size: 0.72em;
    color: rgba(255, 255, 255, 0.75) !important;
    font-family: var(--font-sans);
}
/* Top toolbar (hamburger menu / running-man / deploy button area) —
   otherwise renders with default low-contrast styling on top of our
   custom background. Use currentColor rather than forcing a literal
   fill, since Streamlit's icons mix fill/stroke and a forced fill
   breaks their outlines (looks like icons "disappear"). */
[data-testid="stHeader"] {
    background-color: var(--primary-dark) !important;
}
[data-testid="stHeader"],
[data-testid="stHeader"] button,
[data-testid="stToolbar"],
[data-testid="stToolbar"] button,
[data-testid="stStatusWidget"] {
    color: #ffffff !important;
}
[data-testid="stHeader"] svg,
[data-testid="stToolbar"] svg,
[data-testid="stStatusWidget"] svg {
    fill: currentColor !important;
    stroke: currentColor !important;
}
[data-testid="stHeader"] button:hover {
    background-color: rgba(255, 255, 255, 0.15) !important;
}
/* Force readable dark text everywhere in the main body,
   overriding Streamlit's default white text when the browser/OS is in
   dark mode (otherwise white-on-light becomes invisible). Sidebar is
   handled separately below since it has a dark background. */
.stApp, .stApp p, .stApp span, .stApp label, .stApp li, .stApp div,
.stMarkdown, .stMarkdown p, .stMarkdown li,
[data-testid="stCaptionContainer"],
[data-testid="stWidgetLabel"] label,
[data-testid="stWidgetLabel"] p {
    color: var(--text-main) !important;
}
/* st.text() / <pre> / <code> blocks (used for the transcript preview) —
   these aren't covered by the rule above, so in dark mode they were
   left at the browser default (often light text on a transparent
   background, i.e. invisible on our light panel). */
.stApp pre, .stApp code, .stApp pre span,
[data-testid="stText"], [data-testid="stCodeBlock"] {
    background-color: #ffffff !important;
    color: var(--text-main) !important;
    border: 1px solid var(--surface-alt);
    border-radius: 4px;
    font-family: var(--font-sans);
}
[data-testid="stExpander"] {
    background-color: #ffffff;
    border: 1px solid var(--surface-alt) !important;
    border-radius: 4px;
}
/* Expander header (the clickable "Show selected files" / "Preview
   transcript" bar): Streamlit gives this its own background — separate
   from the outer container above — that turns dark when expanded/open,
   which combined with our forced dark text made it unreadable. Force
   the header and everything inside it (label, chevron icon) to stay
   light, in both closed and open states. */
[data-testid="stExpander"] summary,
[data-testid="stExpander"] summary *,
[data-testid="stExpander"] details summary,
[data-testid="stExpander"] details[open] summary,
[data-testid="stExpander"] details[open] summary * {
    background-color: #ffffff !important;
    color: var(--text-main) !important;
}
[data-testid="stExpander"] summary svg,
[data-testid="stExpander"] summary path {
    fill: var(--text-main) !important;
    stroke: var(--text-main) !important;
}
[data-testid="stExpander"] summary:hover,
[data-testid="stExpander"] summary:hover * {
    background-color: var(--surface) !important;
}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {
    font-weight: 500 !important;
}
/* Sidebar: dark background, light text */
[data-testid="stSidebar"] {
    background-color: var(--primary-dark) !important;
    border-right: 1px solid var(--primary-dark);
}
[data-testid="stSidebar"] *,
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] label,
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {
    color: #ffffff !important;
    font-weight: 500 !important;
}
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3 {
    font-weight: 700 !important;
}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    opacity: 0.9;
}
[data-testid="stSidebar"] hr {
    border-color: rgba(255, 255, 255, 0.3) !important;
}
/* Text input / password / selectbox / multiselect fields: force light
   background + dark text so typed content is always legible, even
   inside the dark sidebar. Multiselect is a separate widget class
   (.stMultiSelect) from selectbox (.stSelectbox) and was previously
   missed entirely, leaving it fully unstyled. */
.stTextInput input,
.stSelectbox div[data-baseweb="select"] > div,
.stMultiSelect div[data-baseweb="select"] > div,
textarea {
    background-color: #ffffff !important;
    color: var(--text-main) !important;
    border: 1px solid var(--surface-alt) !important;
}
/* Multiselect's selected-item "tag" pills */
[data-baseweb="tag"] {
    background-color: var(--primary) !important;
    border-color: var(--primary) !important;
}
[data-baseweb="tag"] span, [data-baseweb="tag"] div {
    color: #ffffff !important;
}
[data-baseweb="tag"] svg {
    fill: #ffffff !important;
}
/* Open dropdown menu (options list, group headers, search box, etc.) —
   force contrast on every descendant, not just the option rows, since
   BaseWeb renders several element types in here (headers, dividers)
   that a narrower "li only" rule misses. ARIA-role selectors are
   included as a fallback in case the data-baseweb attribute naming
   differs between BaseWeb/Streamlit versions; both the shorthand
   `background` and `background-color` are set since some of these
   elements get their background via inline `background` specifically. */
[data-baseweb="popover"], [data-baseweb="popover"] *,
[data-baseweb="menu"], [data-baseweb="menu"] *,
ul[role="listbox"], ul[role="listbox"] *,
[role="listbox"], [role="listbox"] *,
[role="option"], [role="option"] * {
    background: #ffffff !important;
    background-color: #ffffff !important;
    color: var(--text-main) !important;
    -webkit-text-fill-color: var(--text-main) !important;
}
/* Keep a visible highlight on hover/selected so options remain
   distinguishable now that everything defaults to white. */
[data-baseweb="menu"] li:hover,
[data-baseweb="menu"] [aria-selected="true"],
[role="option"]:hover,
[role="option"][aria-selected="true"] {
    background: var(--surface) !important;
    background-color: var(--surface) !important;
}
h1, h2, h3 {
    color: var(--primary-dark) !important;
    font-weight: 700 !important;
}
.stButton>button,
[data-testid^="stBaseButton"] {
    background-color: var(--primary) !important;
    color: #ffffff !important;
    border-radius: 4px;
    border: 1px solid var(--primary) !important;
    padding: 0.5em 1.4em;
    font-weight: 600;
    letter-spacing: 0.02em;
    transition: all 0.2s ease;
}
.stButton>button:hover,
[data-testid^="stBaseButton"]:hover {
    background-color: var(--athletic-blue) !important;
    border-color: var(--athletic-blue) !important;
    color: #ffffff !important;
}
.stButton>button p,
[data-testid^="stBaseButton"] p {
    color: #ffffff !important;
}
/* Disabled buttons (e.g. "Retry" while a batch is running) otherwise
   fall back to Streamlit's own default disabled styling, which reads
   as a plain dark/black button — override it to a muted version of our
   own palette instead, so it's clearly disabled but not illegible.
   Covers the native `disabled` attribute, `aria-disabled`, and the
   `data-testid="stBaseButton-*"` pattern used by recent Streamlit
   versions, since it's unclear which one actually applies without
   being able to inspect the live DOM directly. */
.stButton>button:disabled,
.stButton>button[disabled],
.stButton>button[aria-disabled="true"],
[data-testid^="stBaseButton"]:disabled,
[data-testid^="stBaseButton"][disabled],
[data-testid^="stBaseButton"][aria-disabled="true"],
button:disabled,
button[aria-disabled="true"] {
    background-color: var(--surface-alt) !important;
    border-color: var(--surface-alt) !important;
    color: var(--cool-grey) !important;
    opacity: 1 !important;
}
.stButton>button:disabled *,
.stButton>button[disabled] *,
.stButton>button[aria-disabled="true"] *,
[data-testid^="stBaseButton"]:disabled *,
[data-testid^="stBaseButton"][disabled] *,
[data-testid^="stBaseButton"][aria-disabled="true"] *,
button:disabled *,
button[aria-disabled="true"] * {
    color: var(--cool-grey) !important;
    fill: var(--cool-grey) !important;
}
.stProgress > div > div > div > div {
    background-color: var(--accent-orange);
}
.file-card {
    background: #ffffff;
    border: 1px solid var(--surface-alt);
    border-left: 4px solid var(--primary);
    border-radius: 4px;
    padding: 0.9em 1.1em;
    margin-bottom: 0.6em;
    color: var(--text-main) !important;
}
.file-card * {
    color: var(--text-main) !important;
}
.status-pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 0.75em;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
}
.status-done { background: #e8f0dd; color: #5c8129 !important; }
.status-pending { background: var(--surface-alt); color: var(--text-muted) !important; }
.status-active { background: #fbead2; color: var(--accent-yellow) !important; }
.status-error { background: #fbe3e6; color: var(--accent-red) !important; }
</style>
"""

# Where a Blair-approved logo/seal file should be placed to appear in
# the app. Deliberately not bundled or redrawn here — Blair's seal is a
# registered copyright and "BLAIR ACADEMY" a registered trademark, so
# this app only displays the real, approved file once it's placed here.
BRANDING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "branding")
LOGO_PATH = os.path.join(BRANDING_DIR, "blair_seal.png")


@functools.lru_cache(maxsize=1)
def _logo_data_uri() -> str:
    """Base64-encode the logo file once per process so it can be embedded
    directly in an HTML string (needed for correct flex alignment —
    st.image() renders into its own separate element that CSS flex rules
    can't reach)."""
    with open(LOGO_PATH, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render_header_logo() -> str:
    """Return the HTML for the logo as used inside the top .blair-header
    flex row (real image if available, dashed placeholder otherwise)."""
    if os.path.exists(LOGO_PATH):
        return f'<img class="blair-logo-img" src="{_logo_data_uri()}" alt="Blair Academy seal">'
    return '<div class="blair-logo-placeholder">LOGO<br>HERE</div>'


def render_sidebar_logo() -> None:
    """Render the centered sidebar logo (real image if available, dashed
    placeholder otherwise)."""
    if os.path.exists(LOGO_PATH):
        st.sidebar.markdown(
            f'<div class="blair-sidebar-logo">'
            f'<img class="blair-sidebar-logo-img" src="{_logo_data_uri()}" alt="Blair Academy seal">'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.markdown(
            '<div class="blair-sidebar-logo-placeholder">'
            'Place approved Blair seal at branding/blair_seal.png</div>',
            unsafe_allow_html=True,
        )


WHISPER_MODELS = {
    "Fast (tiny, lower accuracy)": "mlx-community/whisper-tiny-mlx",
    "Balanced (small)": "mlx-community/whisper-small-mlx",
    "Recommended (medium)": "mlx-community/whisper-medium-mlx",
    "Best quality (large-v3)": "mlx-community/whisper-large-v3-mlx",
}

# Very rough real-time speed multipliers on Apple Silicon, used only to
# seed an initial time estimate before any file in the batch has actually
# finished (after which real measured speed takes over). Not precise —
# actual speed varies by chip and audio content.
WHISPER_SPEED_FACTOR = {
    "Fast (tiny, lower accuracy)": 12.0,
    "Balanced (small)": 6.0,
    "Recommended (medium)": 3.0,
    "Best quality (large-v3)": 1.5,
}


# ----------------------------------------------------------------------
# Data structures
# ----------------------------------------------------------------------
@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str = "Speaker 0"
    avg_logprob: float = 0.0     # Whisper's average log-probability for this segment
    no_speech_prob: float = 0.0  # Whisper's probability this segment is actually silence/noise


# Heuristic thresholds for flagging segments worth double-checking
# against the audio — not a hard error, just a signal.
LOW_CONFIDENCE_AVG_LOGPROB = -1.0
LOW_CONFIDENCE_NO_SPEECH_PROB = 0.6


def is_low_confidence(seg: "Segment") -> bool:
    return seg.avg_logprob < LOW_CONFIDENCE_AVG_LOGPROB or seg.no_speech_prob > LOW_CONFIDENCE_NO_SPEECH_PROB


@dataclass
class FileResult:
    path: str
    name: str
    segments: list = field(default_factory=list)
    speakers: list = field(default_factory=list)
    status: str = "pending"  # pending | active | done | error
    error: str = ""
    from_cache: bool = False
    cache_warning: str = ""  # set if the on-disk cache write failed (e.g. read-only/disconnected volume)
    undo_segments: object = None  # single-level undo: snapshot of segments from just before the last "Apply Edits"


def _cache_path(video_path: str) -> str:
    """Cache file lives right next to the video, e.g.
    'interview.mp4' -> 'interview.mp4.ivt-cache.json'."""
    return video_path + CACHE_SUFFIX


def save_cache(fr: "FileResult") -> bool:
    """Persist a finished (or edited) file's results next to the source
    video so a future launch can skip re-transcribing it. Best-effort:
    never raises, since caching should never block the UI. Returns whether
    the write actually succeeded, so callers can surface a warning instead
    of silently re-transcribing on a future launch (e.g. a read-only or
    disconnected external volume)."""
    try:
        stat = os.stat(fr.path)
        video_size, video_mtime = stat.st_size, int(stat.st_mtime)
    except OSError:
        video_size, video_mtime = None, None

    data = {
        "path": fr.path,
        "name": fr.name,
        "video_size": video_size,
        "video_mtime": video_mtime,
        "speakers": fr.speakers,
        "segments": [
            {
                "start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker,
                "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob,
            }
            for s in fr.segments
        ],
        "speaker_labels": st.session_state.speaker_names.get(fr.path, {}),
        "excluded_speakers": sorted(st.session_state.get(f"excluded::{fr.path}", set())),
    }
    try:
        with open(_cache_path(fr.path), "w", encoding="utf-8") as f:
            json.dump(data, f)
        return True
    except Exception:
        return False


def load_cache(video_path: str):
    """Return cached data for a video, or None if there's no cache file
    or the video has changed size/modified-time since it was cached."""
    path = _cache_path(video_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    try:
        stat = os.stat(video_path)
        if data.get("video_size") != stat.st_size or data.get("video_mtime") != int(stat.st_mtime):
            return None  # video changed since this cache was written
    except OSError:
        pass

    return data


def clear_file_cache(fr: "FileResult") -> None:
    """Delete a file's on-disk cache and reset its in-memory state back
    to pending, so it can be individually reprocessed without touching
    the rest of the batch or requiring the global 'Force re-process'
    checkbox."""
    try:
        cache_path = _cache_path(fr.path)
        if os.path.exists(cache_path):
            os.remove(cache_path)
    except Exception:
        pass
    fr.segments = []
    fr.speakers = []
    fr.status = "pending"
    fr.error = ""
    fr.from_cache = False
    fr.undo_segments = None
    st.session_state.speaker_names.pop(fr.path, None)
    st.session_state.pop(f"excluded::{fr.path}", None)

    # Also drop every per-speaker/per-file widget key for this path.
    # Reprocessing reuses the same deterministic "Speaker 1"/"Speaker 2"...
    # naming, and Streamlit widgets ignore their `value=` default once a
    # `key=` already exists in session_state — so without this, stale
    # checkbox/label/edit/merge state from the previous run would silently
    # reattach to the freshly reprocessed file (and a stale merge_target
    # can crash the card if the new speaker count differs).
    stale_prefixes = (
        f"chk::{fr.path}::", f"label::{fr.path}::",
        f"editbox::{fr.path}", f"merge_target::{fr.path}", f"merge_sources::{fr.path}",
        f"preview_cache::{fr.path}", f"search::{fr.path}",
    )
    for key in [k for k in st.session_state.keys() if k.startswith(stale_prefixes)]:
        st.session_state.pop(key, None)


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(settings: dict) -> None:
    try:
        os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except Exception:
        pass


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def fmt_timecode(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(total_ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_vtt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(total_ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def browse_for_video_files() -> list:
    """Open a native macOS file picker (via osascript) for selecting one
    or more video files. Runs as a separate process, so it works reliably
    from inside Streamlit's script-runner thread (unlike tkinter, whose
    dialogs must run on the main thread on macOS)."""
    script = (
        'set theFiles to choose file with prompt "Select video files" '
        'with multiple selections allowed '
        'of type {"public.movie","com.apple.quicktime-movie","public.mpeg-4"}\n'
        'set thePaths to {}\n'
        'repeat with f in theFiles\n'
        '  set end of thePaths to POSIX path of f\n'
        'end repeat\n'
        'set AppleScript\'s text item delimiters to linefeed\n'
        'return thePaths as text'
    )
    result = subprocess.run(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        # User cancelled, or osascript unavailable (non-macOS)
        return []
    output = result.stdout.decode("utf-8").strip()
    return [p for p in output.split("\n") if p]


def browse_for_save_path(default_name: str, default_dir: str = "") -> str:
    """Open a native macOS 'Save As' dialog (via osascript) letting the
    user choose both the destination folder and the filename. Returns
    the full chosen path, or '' if cancelled."""
    escaped_name = default_name.replace('"', '\\"')
    location_clause = ""
    if default_dir and os.path.isdir(default_dir):
        escaped_dir = default_dir.replace('"', '\\"')
        location_clause = f'default location (POSIX file "{escaped_dir}")'
    script = (
        f'set thePath to choose file name with prompt "Save transcript as" '
        f'default name "{escaped_name}" {location_clause}\n'
        'return POSIX path of thePath'
    )
    result = subprocess.run(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8").strip()


def browse_for_app_or_script(prompt: str = "Select Rough Cut Studio (app or main.py)") -> str:
    """Native macOS file picker for locating another app — either a
    packaged .app bundle or a launcher .py script."""
    script = f'return POSIX path of (choose file with prompt "{prompt}")'
    result = subprocess.run(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8").strip()


def reveal_in_finder(path: str) -> None:
    """Open Finder with the given file selected."""
    try:
        subprocess.run(["open", "-R", path], timeout=10)
    except Exception:
        pass


def launch_rough_cut_studio(path: str) -> bool:
    """Launch Rough Cut Studio from a configured path. Supports either a
    packaged .app bundle (via `open`) or a Python launcher script (run
    with the system `python3` on PATH, since it likely lives in its own
    virtual environment separate from this app's). Returns True if the
    launch command was issued successfully (not a guarantee the app's
    window actually opened)."""
    if not path:
        return False
    try:
        if path.endswith(".app"):
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["python3", path], cwd=os.path.dirname(path) or None)
        return True
    except Exception:
        return False


def extract_audio(video_path: str, out_wav: str) -> None:
    """Pull only the audio stream to a mono 16kHz WAV (required by both
    mlx-whisper and pyannote). No copy of the video itself is made."""
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn",                # no video
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        out_wav,
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=1800)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg did not finish extracting audio from {video_path} within 30 minutes")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode(errors='ignore')[-500:]}")


def transcribe_audio(audio_path: str, model_repo: str, progress_callback=None) -> list:
    """Run mlx-whisper (Apple Metal accelerated). Returns a list of dicts:
    {"start": float, "end": float, "text": str, "avg_logprob": float, "no_speech_prob": float}
    The confidence fields are used to flag segments worth double-checking.

    If `progress_callback` is given, it's called with a float in [0, 1] as
    transcription advances. mlx_whisper has no public progress API, so this
    works by temporarily swapping out the `tqdm.tqdm` class that its
    `transcribe()` reads its progress bar from (`mlx_whisper.transcribe`
    does `import tqdm` and calls `tqdm.tqdm(...)`, which is the same tqdm
    module object everywhere in the process — not a private copy). This is
    safe here specifically because processing is strictly single-threaded
    and sequential (see module docstring / SETUP.md): the patch is applied
    and reverted entirely within this one synchronous call, before
    diarization (which also uses tqdm, via pyannote) even starts."""
    import mlx_whisper
    import tqdm as _tqdm_module

    if progress_callback is None:
        result = mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo=model_repo,
            word_timestamps=False,
        )
    else:
        original_tqdm_cls = _tqdm_module.tqdm

        class _ProgressReportingTqdm(original_tqdm_cls):
            def update(self, n=1):
                ret = super().update(n)
                if self.total:
                    try:
                        progress_callback(min(1.0, self.n / self.total))
                    except Exception:
                        pass
                return ret

        _tqdm_module.tqdm = _ProgressReportingTqdm
        try:
            # verbose=False (not None) is required for mlx_whisper's internal
            # progress bar to be non-disabled (see its `disable=verbose is not
            # False`), so our patched `update()` actually gets called. False
            # (vs True) also keeps it from printing each segment's text.
            result = mlx_whisper.transcribe(
                audio_path,
                path_or_hf_repo=model_repo,
                word_timestamps=False,
                verbose=False,
            )
        finally:
            _tqdm_module.tqdm = original_tqdm_cls

    return [
        {
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"].strip(),
            "avg_logprob": seg.get("avg_logprob", 0.0),
            "no_speech_prob": seg.get("no_speech_prob", 0.0),
        }
        for seg in result.get("segments", [])
    ]


def _load_pipeline(model_id: str, hf_token: str):
    """Load a pyannote Pipeline, tolerating both the old `use_auth_token`
    and new `token` kwarg names across pyannote.audio versions."""
    from pyannote.audio import Pipeline

    try:
        return Pipeline.from_pretrained(model_id, token=hf_token)
    except TypeError:
        return Pipeline.from_pretrained(model_id, use_auth_token=hf_token)


class _DiarizationProgressHook:
    """A pyannote pipeline `hook` callable (see pyannote.audio.pipelines.utils.hook.ProgressHook
    for the reference implementation/signature this mirrors). pyannote calls
    this repeatedly as `hook(step_name, step_artifact, file=..., total=..., completed=...)`
    while it works through its internal steps (segmentation, embeddings, etc.);
    we just forward `(step_name, completed/total)` to a simpler callback."""

    def __init__(self, progress_callback):
        self.progress_callback = progress_callback

    def __call__(self, step_name, step_artifact, file=None, total=None, completed=None):
        if not total:
            return
        if completed is None:
            completed = total
        try:
            self.progress_callback(step_name, min(1.0, completed / total))
        except Exception:
            pass


def diarize_audio(audio_path: str, hf_token: str, progress_callback=None):
    """Run pyannote.audio speaker diarization locally.

    Tries the current recommended pipeline first, then falls back to the
    older one, since pyannote has been migrating pipelines and gated-repo
    dependencies between versions:
      1. pyannote/speaker-diarization-community-1 (current, most accurate)
      2. pyannote/speaker-diarization-3.1 (legacy, still supported)

    Both require a free Hugging Face account with the model license
    accepted on the respective model page, plus an access token. The
    token is only used to download model weights once; all audio
    processing happens on-device.

    If `progress_callback(step_name: str, fraction: float)` is given, it's
    wired up via pyannote's own `hook=` mechanism. If a given pipeline
    version's `apply()` doesn't accept `hook` (signature has shifted across
    pyannote releases before), we silently fall back to running it without
    progress reporting rather than failing the whole diarization pass.
    """
    model_ids = [
        "pyannote/speaker-diarization-community-1",
        "pyannote/speaker-diarization-3.1",
    ]

    last_error = None
    for model_id in model_ids:
        try:
            pipeline = _load_pipeline(model_id, hf_token)
            if progress_callback is not None:
                try:
                    output = pipeline(audio_path, hook=_DiarizationProgressHook(progress_callback))
                except TypeError:
                    output = pipeline(audio_path)
            else:
                output = pipeline(audio_path)

            # community-1 exposes results via `.speaker_diarization`
            # (an iterable of (turn, speaker) pairs); 3.1 returns an
            # Annotation object directly with `.itertracks()`.
            annotation = getattr(output, "speaker_diarization", output)

            turns = []
            if hasattr(annotation, "itertracks"):
                for turn, _, speaker in annotation.itertracks(yield_label=True):
                    turns.append((turn.start, turn.end, speaker))
            else:
                for turn, speaker in annotation:
                    turns.append((turn.start, turn.end, speaker))
            return turns

        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(
        "Diarization failed for all known pyannote pipelines. Make sure "
        "you've accepted the license on BOTH "
        "huggingface.co/pyannote/speaker-diarization-community-1 and "
        "huggingface.co/pyannote/speaker-diarization-3.1 (plus "
        "huggingface.co/pyannote/segmentation-3.0), using the same "
        f"account that generated your token. Last error: {last_error}"
    )


def merge_transcript_and_speakers(transcript_segments, diarization_turns) -> list:
    """Assign a speaker label to each whisper segment based on which
    diarization turn covers the segment's midpoint (falls back to the
    turn with greatest overlap)."""
    segments = []
    speaker_order = []

    def label_for(mid_point):
        best_speaker, best_overlap = None, float("-inf")
        for t_start, t_end, spk in diarization_turns:
            overlap = min(t_end, mid_point) - max(t_start, mid_point)
            if overlap > best_overlap:
                best_overlap, best_speaker = overlap, spk
        return best_speaker or "Speaker 0"

    for seg_data in transcript_segments:
        start, end, text = seg_data["start"], seg_data["end"], seg_data["text"]
        if not text:
            continue
        mid = (start + end) / 2
        speaker = label_for(mid) if diarization_turns else "Speaker 0"
        if speaker not in speaker_order:
            speaker_order.append(speaker)
        segments.append(Segment(
            start=start, end=end, text=text, speaker=speaker,
            avg_logprob=seg_data.get("avg_logprob", 0.0),
            no_speech_prob=seg_data.get("no_speech_prob", 0.0),
        ))

    return segments, speaker_order


def normalize_speaker_names(speaker_order: list) -> dict:
    """Map raw diarization labels (e.g. SPEAKER_00) to friendly
    'Speaker 1', 'Speaker 2', ... in order of first appearance."""
    mapping = {}
    for i, raw in enumerate(speaker_order):
        mapping[raw] = f"Speaker {i + 1}"
    return mapping


def merge_speakers(fr: "FileResult", target: str, sources: list) -> None:
    """Fold one or more detected speakers into a single target speaker
    within a file — useful when diarization splits one person into
    multiple IDs. Reassigns their segments, updates the speaker list,
    and cleans up any per-speaker labels/exclusions for the merged-away
    IDs, then persists the result."""
    sources = [s for s in sources if s != target]
    if not sources:
        return
    for seg in fr.segments:
        if seg.speaker in sources:
            seg.speaker = target
    fr.speakers = [s for s in fr.speakers if s not in sources]

    excluded_key = f"excluded::{fr.path}"
    excluded = st.session_state.get(excluded_key, set())
    labels = st.session_state.speaker_names.get(fr.path, {})
    for s in sources:
        excluded.discard(s)
        labels.pop(s, None)
    st.session_state[excluded_key] = excluded

    save_cache(fr)


def _visible_segments(segments: list, excluded_speakers: set, speaker_labels: dict):
    speaker_labels = speaker_labels or {}
    for seg in segments:
        if seg.speaker in excluded_speakers:
            continue
        display_name = (speaker_labels.get(seg.speaker) or "").strip() or seg.speaker
        yield seg, display_name


def build_txt(file_name: str, segments: list, excluded_speakers: set,
              speaker_labels: dict = None, source_path: str = None) -> str:
    lines = [f"# Transcript: {file_name}"]
    if source_path:
        lines.append(f"# Source video: {source_path}")
    lines.append("")
    for seg, display_name in _visible_segments(segments, excluded_speakers, speaker_labels):
        lines.append(f"[{fmt_timecode(seg.start)}] {display_name}: {seg.text}")
    return "\n".join(lines) + "\n"


def build_srt(file_name: str, segments: list, excluded_speakers: set,
              speaker_labels: dict = None, source_path: str = None) -> str:
    # Standard SRT has no comment/metadata syntax, so the source video
    # isn't embedded here — association relies on the exported .srt
    # sharing the same base filename as the video (the default suggested
    # name already does this), which is how editors auto-pair captions.
    lines = []
    idx = 1
    for seg, display_name in _visible_segments(segments, excluded_speakers, speaker_labels):
        lines.append(str(idx))
        lines.append(f"{fmt_srt_time(seg.start)} --> {fmt_srt_time(seg.end)}")
        lines.append(f"{display_name}: {seg.text}")
        lines.append("")
        idx += 1
    return "\n".join(lines) + "\n"


def build_vtt(file_name: str, segments: list, excluded_speakers: set,
              speaker_labels: dict = None, source_path: str = None) -> str:
    lines = ["WEBVTT"]
    if source_path:
        lines.append(f"NOTE Source video: {source_path}")
    lines.append("")
    for seg, display_name in _visible_segments(segments, excluded_speakers, speaker_labels):
        lines.append(f"{fmt_vtt_time(seg.start)} --> {fmt_vtt_time(seg.end)}")
        lines.append(f"{display_name}: {seg.text}")
        lines.append("")
    return "\n".join(lines) + "\n"


EXPORT_FORMATS = {
    "SRT (.srt)": (".srt", build_srt),
    "WebVTT (.vtt)": (".vtt", build_vtt),
    "Plain text (.txt)": (".txt", build_txt),
}


def build_transcript(file_name: str, segments: list, excluded_speakers: set,
                      speaker_labels: dict, export_format: str, source_path: str = None) -> str:
    _, builder = EXPORT_FORMATS[export_format]
    return builder(file_name, segments, excluded_speakers, speaker_labels, source_path)


def export_single_file(fr: "FileResult", export_format: str):
    """Build the transcript for one file in the chosen format, prompt the
    user with a native Save As dialog, and write it if they confirm.
    Returns the saved path, or None if the user cancelled."""
    save_cache(fr)  # persist latest labels/exclusions alongside the export

    excluded_key = f"excluded::{fr.path}"
    excluded = st.session_state.get(excluded_key, set())
    labels = st.session_state.speaker_names.get(fr.path, {})
    content = build_transcript(fr.name, fr.segments, excluded, labels, export_format, fr.path)

    ext, _ = EXPORT_FORMATS[export_format]
    base = os.path.splitext(fr.name)[0]
    safe_name = re.sub(r"[^A-Za-z0-9_\-]+", "_", base)
    suggested_name = f"{safe_name}{ext}"

    save_path = browse_for_save_path(suggested_name)
    if not save_path:
        return None

    with open(save_path, "w", encoding="utf-8") as f:
        f.write(content)

    st.session_state.setdefault("last_export_paths", {})[fr.path] = save_path
    return save_path


def build_batch_summary_csv(files: list) -> str:
    """Build a CSV manifest of the current batch: one row per file with
    its status, detected/labeled speakers, and last export path (if
    any). Useful for tracking a larger production across sessions."""
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "File Name", "Source Path", "Status", "Cached",
        "Speaker Count", "Speakers", "Low-Confidence Segments", "Last Export Path",
    ])
    for fr in files:
        labels = st.session_state.speaker_names.get(fr.path, {})
        speaker_display = "; ".join(
            (labels.get(spk) or "").strip() or spk for spk in fr.speakers
        )
        flagged = sum(1 for seg in fr.segments if is_low_confidence(seg))
        last_export = st.session_state.last_export_paths.get(fr.path, "")
        writer.writerow([
            fr.name, fr.path, fr.status, "yes" if fr.from_cache else "no",
            len(fr.speakers), speaker_display, flagged, last_export,
        ])
    return buf.getvalue()


def process_one_video(
    video_path: str, model_repo: str, enable_diarization: bool, hf_token: str,
    progress_callback=None,
) -> None:
    """Run the full pipeline (extract → transcribe → diarize → merge →
    cache) for a single video, mutating its FileResult in place. Files
    are processed strictly one at a time, synchronously, by the batch
    driver in main().

    If given, `progress_callback(phase: str, fraction: float, detail: str)`
    is called as the file moves through "extract"/"transcribe"/"diarize"
    phases, with `fraction` resetting to 0.0 at the start of each phase
    (there's no single meaningful percentage across phases of very
    different, file-dependent duration). This is safe to call synchronously
    from within a Streamlit rerun — updating an `st.progress` placeholder
    mid-script works without threading, unlike the batch-level elapsed/ETA
    display, which genuinely can't be refreshed until this whole call
    returns."""
    def on_progress(phase, fraction, detail):
        if progress_callback is not None:
            try:
                progress_callback(phase, fraction, detail)
            except Exception:
                pass

    fr = st.session_state.results[video_path]
    fr.status = "active"
    tmp_dir = tempfile.mkdtemp(prefix="ivt_")
    try:
        # 1. Extract audio only, to a temp file
        on_progress("extract", 0.0, "Extracting audio…")
        tmp_wav = os.path.join(tmp_dir, "audio.wav")
        extract_audio(video_path, tmp_wav)

        # 2. Transcribe (Apple Metal via mlx-whisper)
        on_progress("transcribe", 0.0, "Transcribing… 0%")
        transcript_segments = transcribe_audio(
            tmp_wav, model_repo,
            progress_callback=(
                lambda frac: on_progress("transcribe", frac, f"Transcribing… {int(frac * 100)}%")
            ) if progress_callback is not None else None,
        )

        # 3. Diarize (optional)
        diarization_turns = []
        if enable_diarization:
            on_progress("diarize", 0.0, "Diarizing speakers…")
            diarization_turns = diarize_audio(
                tmp_wav, hf_token,
                progress_callback=(
                    lambda step, frac: on_progress(
                        "diarize", frac, f"Diarizing speakers ({step})… {int(frac * 100)}%"
                    )
                ) if progress_callback is not None else None,
            )

        # 4. Merge + friendly speaker names
        segments, speaker_order = merge_transcript_and_speakers(transcript_segments, diarization_turns)
        name_map = normalize_speaker_names(speaker_order)
        for seg in segments:
            seg.speaker = name_map.get(seg.speaker, seg.speaker)

        fr.segments = segments
        fr.speakers = list(name_map.values()) or ["Speaker 1"]
        fr.status = "done"
        fr.from_cache = False

        if save_cache(fr):
            fr.cache_warning = ""
        else:
            fr.cache_warning = (
                "Couldn't write the cache file next to this video (is the "
                "drive read-only or disconnected?) — it will be fully "
                "re-transcribed if you reprocess it later."
            )

    except Exception as e:
        fr.status = "error"
        fr.error = str(e)

    finally:
        # 5. Clean up temp audio dir (even if extraction itself failed
        # before producing any output) + free Unified Memory
        shutil.rmtree(tmp_dir, ignore_errors=True)
        gc.collect()


def get_video_duration_seconds(path: str):
    """Read a video's duration via ffprobe (ships alongside ffmpeg).
    Returns None if unavailable for any reason."""
    return probe_duration_seconds(path, timeout=10)


def estimate_seconds_for_video(path: str, model_label: str, enable_diarization: bool):
    """Rough upfront processing-time guess for a video, used only until
    real measured per-file speed is available from this batch."""
    duration = get_video_duration_seconds(path)
    if duration is None:
        return None
    factor = WHISPER_SPEED_FACTOR.get(model_label, 3.0)
    estimate = duration / factor
    if enable_diarization:
        estimate *= 1.4  # rough overhead for the diarization pass
    return estimate


# ----------------------------------------------------------------------
# Streamlit app
# ----------------------------------------------------------------------
def main():
    page_icon = LOGO_PATH if os.path.exists(LOGO_PATH) else "🎙️"
    st.set_page_config(page_title="Local Interview Transcriber", page_icon=page_icon, layout="wide")
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    st.markdown(
        f'<div class="blair-header">{render_header_logo()}'
        f'<div class="blair-title-block"><h1>Local Interview Transcriber</h1></div></div>',
        unsafe_allow_html=True,
    )
    st.caption("100% on-device transcription + diarization for Apple Silicon. "
               "No audio or video ever leaves your machine.")

    if "results" not in st.session_state:
        st.session_state.results = {}  # path -> FileResult
    if "speaker_names" not in st.session_state:
        st.session_state.speaker_names = {}  # path -> {raw: friendly}
    if "selected_videos" not in st.session_state:
        st.session_state.selected_videos = []
    if "last_export_paths" not in st.session_state:
        st.session_state.last_export_paths = {}  # path -> last saved transcript path
    if "batch_active" not in st.session_state:
        st.session_state.batch_active = False
        st.session_state.batch_queue = []
        st.session_state.batch_total = 0
        st.session_state.batch_paused = False
        st.session_state.batch_durations = []

    # Load remembered settings / saved token once per app launch (not
    # every rerun) so the person doesn't have to re-enter them each time.
    if "settings_loaded" not in st.session_state:
        _settings = load_settings()
        st.session_state._saved_export_format = _settings.get("export_format")
        st.session_state._saved_model_label = _settings.get("model_label")
        st.session_state._saved_enable_diarization = _settings.get("enable_diarization", True)
        st.session_state.rough_cut_studio_path = _settings.get("rough_cut_studio_path", "")
        st.session_state.settings_loaded = True

    if "hf_token_loaded" not in st.session_state:
        saved_token = ""
        try:
            import keyring
            saved_token = keyring.get_password(KEYRING_SERVICE, KEYRING_HF_TOKEN_KEY) or ""
        except Exception:
            pass
        st.session_state._saved_hf_token = saved_token
        st.session_state.hf_token_loaded = True

    # ---------------- Sidebar: configuration ----------------
    with st.sidebar:
        render_sidebar_logo()
        st.header("Settings")

        st.markdown("**Video files**")
        if st.button("🔍 Select Video Files", use_container_width=True):
            picked = browse_for_video_files()
            if picked:
                # Merge with any already-selected files, de-duplicated
                combined = list(dict.fromkeys(st.session_state.selected_videos + picked))
                st.session_state.selected_videos = combined
        if st.session_state.selected_videos:
            st.caption(f"{len(st.session_state.selected_videos)} file(s) selected")
            if st.button("Clear selection", use_container_width=True):
                st.session_state.selected_videos = []
        else:
            st.caption("No files selected yet.")

        force_reprocess = st.checkbox(
            "Force re-process (ignore cache)",
            value=False,
            help="By default, files that were already transcribed in a "
                 "previous session are loaded from the local cache instead "
                 "of being re-run through Whisper/pyannote.",
        )

        st.markdown("---")
        st.markdown("**🎬 Rough Cut Studio** (optional)")
        if st.button("Locate Rough Cut Studio...", use_container_width=True):
            picked_app = browse_for_app_or_script()
            if picked_app:
                st.session_state.rough_cut_studio_path = picked_app
        if st.session_state.rough_cut_studio_path:
            st.caption(f"`{st.session_state.rough_cut_studio_path}`")
            if st.button("🎬 Launch Rough Cut Studio", use_container_width=True):
                if launch_rough_cut_studio(st.session_state.rough_cut_studio_path):
                    st.success("Launched Rough Cut Studio.")
                else:
                    st.error("Couldn't launch Rough Cut Studio — check the path above.")
        else:
            st.caption("Point this at Rough Cut Studio's .app or main.py "
                       "for a one-click launch after exporting.")

        st.markdown("---")
        model_options = list(WHISPER_MODELS.keys())
        default_model_idx = (
            model_options.index(st.session_state._saved_model_label)
            if st.session_state._saved_model_label in model_options else 2
        )
        model_label = st.selectbox("Whisper model (mlx-whisper)", model_options, index=default_model_idx)
        model_repo = WHISPER_MODELS[model_label]

        st.markdown("---")
        enable_diarization = st.checkbox(
            "Enable speaker diarization (pyannote)",
            value=st.session_state._saved_enable_diarization,
        )
        hf_token = ""
        if enable_diarization:
            hf_token = st.text_input(
                "Hugging Face access token",
                type="password",
                value=st.session_state._saved_hf_token,
                help=(
                    "Free token from huggingface.co/settings/tokens. "
                    "You must also accept the license on BOTH "
                    "huggingface.co/pyannote/speaker-diarization-community-1 "
                    "and huggingface.co/pyannote/speaker-diarization-3.1 "
                    "(same account as your token). Saved to macOS Keychain "
                    "so you don't have to re-enter it every launch."
                ),
            )
            if hf_token != st.session_state._saved_hf_token:
                try:
                    import keyring
                    if hf_token:
                        keyring.set_password(KEYRING_SERVICE, KEYRING_HF_TOKEN_KEY, hf_token)
                    else:
                        keyring.delete_password(KEYRING_SERVICE, KEYRING_HF_TOKEN_KEY)
                except Exception:
                    pass
                st.session_state._saved_hf_token = hf_token

            if hf_token and st.button("Forget saved token", use_container_width=True):
                try:
                    import keyring
                    keyring.delete_password(KEYRING_SERVICE, KEYRING_HF_TOKEN_KEY)
                except Exception:
                    pass
                st.session_state._saved_hf_token = ""
                st.rerun()

        st.markdown("---")
        if not check_ffmpeg():
            st.error("ffmpeg not found on PATH. Install with Homebrew (see setup instructions).")

        start_clicked = st.button(
            "▶ Start Batch Processing",
            use_container_width=True,
            disabled=st.session_state.batch_active,
        )

    # Remember these choices for next launch (cheap, tiny file; skip the
    # token itself here since that's handled separately via Keychain).
    # (export_format is saved separately, where that control now lives.)
    # Only write to disk when something actually changed - this block runs
    # on every rerun (i.e. every widget interaction anywhere in the app).
    current_settings = {
        "export_format": st.session_state._saved_export_format,
        "model_label": model_label,
        "enable_diarization": enable_diarization,
        "rough_cut_studio_path": st.session_state.rough_cut_studio_path,
    }
    if current_settings != st.session_state.get("_saved_settings"):
        save_settings(current_settings)
        st.session_state._saved_settings = current_settings

    # ---------------- Selected videos ----------------
    videos = st.session_state.selected_videos
    if videos:
        st.write(f"**{len(videos)}** video file(s) queued for processing.")
        with st.expander("Show selected files"):
            for v in videos:
                st.write(f"- `{v}`")

    # Initialize FileResult entries for newly selected videos, and drop
    # any that were removed from the selection. Newly-seen videos get an
    # immediate cache lookup so previously-transcribed files show as done
    # without waiting for "Start Batch Processing".
    _segment_fields = {"start", "end", "text", "speaker", "avg_logprob", "no_speech_prob"}
    for v in videos:
        if v not in st.session_state.results:
            fr = FileResult(path=v, name=os.path.basename(v))
            cached = load_cache(v)
            if cached:
                fr.segments = [
                    Segment(**{k: val for k, val in seg.items() if k in _segment_fields})
                    for seg in cached.get("segments", [])
                ]
                fr.speakers = cached.get("speakers", [])
                fr.status = "done"
                fr.from_cache = True
                st.session_state.speaker_names[v] = cached.get("speaker_labels", {})
                st.session_state[f"excluded::{v}"] = set(cached.get("excluded_speakers", []))
            st.session_state.results[v] = fr
    for existing_path in list(st.session_state.results.keys()):
        if existing_path not in videos:
            del st.session_state.results[existing_path]
            st.session_state.speaker_names.pop(existing_path, None)
            st.session_state.pop(f"excluded::{existing_path}", None)

    # ---------------- Start batch: build the queue, then hand off ----------------
    if start_clicked:
        to_process = videos if force_reprocess else [
            v for v in videos if st.session_state.results[v].status != "done"
        ]

        if not videos:
            st.error("No video files selected. Click \"Select Video Files\" first.")
        elif not check_ffmpeg():
            st.error("ffmpeg is required. Install it first (see setup instructions).")
        elif enable_diarization and not hf_token:
            st.error("Diarization is enabled but no Hugging Face token was provided.")
        elif not to_process:
            st.info("All selected files are already cached. Check \"Force re-process\" to redo them.")
        else:
            st.session_state.batch_active = True
            st.session_state.batch_queue = list(to_process)
            st.session_state.batch_total = len(to_process)
            st.session_state.batch_paused = False
            st.session_state.batch_start_time = time.time()
            st.session_state.batch_durations = []
            st.rerun()

    # ---------------- Batch driver ----------------
    # Processes exactly one file per Streamlit rerun (synchronously, on
    # the main thread — no background threads), then reruns itself. This
    # keeps things simple and avoids any risk of Metal/MPS behaving
    # differently across threads, which can silently slow things down or
    # misbehave. The tradeoff: the batch-level elapsed/remaining estimate
    # only updates between files, since that reflects actual per-file
    # durations which aren't known until a file finishes. Within a single
    # file, though, `process_one_video`'s `progress_callback` updates a
    # `st.progress` placeholder live — this works without threading because
    # Streamlit flushes widget updates to the browser as they happen during
    # a script run, not only when the run ends. Pause/Cancel are checked
    # right here, before starting the next file — never mid-file.
    if st.session_state.get("batch_active"):
        total = st.session_state.batch_total
        completed = total - len(st.session_state.batch_queue)
        elapsed = time.time() - st.session_state.batch_start_time

        status_box = st.empty()
        st.progress(completed / total if total else 0.0)

        ctrl_cols = st.columns(2)
        with ctrl_cols[0]:
            pause_label = "▶ Resume" if st.session_state.batch_paused else "⏸ Pause"
            if st.button(pause_label, use_container_width=True):
                st.session_state.batch_paused = not st.session_state.batch_paused
                st.rerun()
        with ctrl_cols[1]:
            if st.button("✖ Cancel Batch", use_container_width=True):
                st.session_state.batch_active = False
                st.session_state.batch_queue = []
                st.rerun()

        if st.session_state.batch_paused:
            status_box.warning(
                f"⏸ Paused — {completed}/{total} done "
                f"(elapsed {fmt_timecode(elapsed)}). Click Resume to continue."
            )
        elif not st.session_state.batch_queue:
            status_box.success(
                f"✅ Batch complete: {total} file(s) processed in {fmt_timecode(elapsed)}."
            )
            st.session_state.batch_active = False
        else:
            video_path = st.session_state.batch_queue[0]
            fr = st.session_state.results[video_path]

            if st.session_state.batch_durations:
                avg_per_file = sum(st.session_state.batch_durations) / len(st.session_state.batch_durations)
                eta_str = fmt_timecode(avg_per_file * len(st.session_state.batch_queue))
            else:
                guess = estimate_seconds_for_video(video_path, model_label, enable_diarization)
                eta_str = f"~{fmt_timecode(guess)} (rough estimate)" if guess else "estimating…"

            status_box.info(
                f"Processing **{completed + 1}/{total}**: `{fr.name}`  —  "
                f"elapsed {fmt_timecode(elapsed)}  —  est. remaining {eta_str}"
            )
            file_progress_box = st.empty()
            file_progress_box.progress(0.0, text="Starting…")

            def update_file_progress(phase, fraction, detail):
                file_progress_box.progress(fraction, text=detail)

            file_start = time.time()
            process_one_video(
                video_path, model_repo, enable_diarization, hf_token,
                progress_callback=update_file_progress,
            )
            st.session_state.batch_durations.append(time.time() - file_start)
            st.session_state.batch_queue.pop(0)
            st.rerun()

    # ---------------- Results / speaker filtering / export ----------------
    if st.session_state.results:
        st.markdown("---")
        st.subheader("Files")

        format_options = list(EXPORT_FORMATS.keys())
        default_format_idx = (
            format_options.index(st.session_state._saved_export_format)
            if st.session_state._saved_export_format in format_options else 0
        )
        export_format = st.selectbox(
            "Export format (choose right before saving/exporting below)",
            format_options,
            index=default_format_idx,
        )
        if export_format != st.session_state._saved_export_format:
            st.session_state._saved_export_format = export_format
            save_settings({
                "export_format": export_format,
                "model_label": model_label,
                "enable_diarization": enable_diarization,
                "rough_cut_studio_path": st.session_state.rough_cut_studio_path,
            })

        for path, fr in st.session_state.results.items():
            with st.container():
                st.markdown('<div class="file-card">', unsafe_allow_html=True)
                cols = st.columns([3, 1, 1.3])
                with cols[0]:
                    st.markdown(f"**{fr.name}**")
                with cols[1]:
                    pill_class = {
                        "pending": "status-pending",
                        "active": "status-active",
                        "done": "status-done",
                        "error": "status-error",
                    }[fr.status]
                    st.markdown(
                        f'<span class="status-pill {pill_class}">{fr.status}</span>',
                        unsafe_allow_html=True,
                    )
                with cols[2]:
                    if fr.status in ("done", "error"):
                        if st.button(
                            "🔄 Retry",
                            key=f"retry::{path}",
                            use_container_width=True,
                            disabled=st.session_state.batch_active,
                            help="Clear this file's cache and mark it pending — next "
                                 "\"Start Batch Processing\" will redo just this file.",
                        ):
                            clear_file_cache(fr)
                            st.rerun()

                if fr.status == "error":
                    st.error(fr.error)

                if fr.cache_warning:
                    st.warning(fr.cache_warning)

                if fr.status == "done" and fr.from_cache:
                    st.caption("✅ Loaded from a previous session (cached) — no re-processing needed.")

                if fr.status == "done" and fr.segments:
                    excluded_key = f"excluded::{path}"
                    if excluded_key not in st.session_state:
                        st.session_state[excluded_key] = set()
                    st.session_state.speaker_names.setdefault(path, {})

                    st.caption(
                        "Detected speakers — uncheck to exclude, or type a name/role "
                        "(e.g. \"Interviewer\", \"Jane Doe\") to relabel in the export:"
                    )
                    speaker_cols = st.columns(len(fr.speakers))
                    for i, spk in enumerate(fr.speakers):
                        with speaker_cols[i]:
                            checked = st.checkbox(spk, value=True, key=f"chk::{path}::{spk}")
                            if not checked:
                                st.session_state[excluded_key].add(spk)
                            else:
                                st.session_state[excluded_key].discard(spk)

                            label_value = st.text_input(
                                "Label",
                                value=st.session_state.speaker_names[path].get(spk, ""),
                                placeholder=spk,
                                key=f"label::{path}::{spk}",
                                label_visibility="collapsed",
                            )
                            st.session_state.speaker_names[path][spk] = label_value

                    if len(fr.speakers) > 1:
                        with st.expander("🔀 Merge Speakers"):
                            st.caption(
                                "If diarization split one person into two IDs, merge them "
                                "here. Pick the speaker to keep, then whichever other "
                                "speaker(s) should be folded into it."
                            )
                            merge_target = st.selectbox(
                                "Keep this speaker",
                                fr.speakers,
                                key=f"merge_target::{path}",
                            )
                            merge_sources = st.multiselect(
                                "Merge these into it",
                                [s for s in fr.speakers if s != merge_target],
                                key=f"merge_sources::{path}",
                            )
                            if st.button("Merge", key=f"merge_btn::{path}", disabled=not merge_sources):
                                merge_speakers(fr, merge_target, merge_sources)
                                st.session_state.pop(f"merge_target::{path}", None)
                                st.session_state.pop(f"merge_sources::{path}", None)
                                st.success(f"Merged {', '.join(merge_sources)} into {merge_target}.")
                                st.rerun()

                    with st.expander("✏️ Edit Transcript Text"):
                        flagged_count = sum(1 for seg in fr.segments if is_low_confidence(seg))
                        caption_text = (
                            "Fix misheard words/names here, one line per entry. Keep the "
                            "same number of lines — only edit the text after the colon."
                        )
                        if flagged_count:
                            caption_text += (
                                f" ⚠️ {flagged_count} segment(s) below are flagged "
                                "\"LOW CONFIDENCE\" — Whisper itself wasn't confident about "
                                "these, so it's worth a quick check against the audio."
                            )
                        st.caption(caption_text)

                        edit_key = f"editbox::{path}"
                        # A text_area with both `key=` and `value=` ignores `value` once
                        # `key` already exists in session_state, so there's no point
                        # rebuilding this O(segments) string on every rerun after the
                        # first — only build it once per file (or after Retry/edits
                        # clear the key).
                        if edit_key not in st.session_state:
                            editable_lines = []
                            for i, seg in enumerate(fr.segments):
                                display_name = (st.session_state.speaker_names[path].get(seg.speaker) or "").strip() or seg.speaker
                                flag = "⚠️ LOW CONFIDENCE — " if is_low_confidence(seg) else ""
                                editable_lines.append(f"{i + 1}) {flag}{display_name}: {seg.text}")
                            st.session_state[edit_key] = "\n".join(editable_lines)
                        edited_text = st.text_area(
                            "Transcript text",
                            value=st.session_state[edit_key],
                            height=300,
                            key=edit_key,
                            label_visibility="collapsed",
                        )
                        if st.button("Apply Edits", key=f"apply::{path}"):
                            new_lines = edited_text.split("\n")
                            if len(new_lines) != len(fr.segments):
                                st.error(
                                    f"Line count changed ({len(new_lines)} vs "
                                    f"{len(fr.segments)} expected) — please edit text only, "
                                    "without adding or removing lines."
                                )
                            else:
                                # The "⚠️ LOW CONFIDENCE — " marker is a display-only hint —
                                # strip it here so it never gets saved into the real text.
                                pattern = re.compile(r"^\d+\)\s*(?:⚠️ LOW CONFIDENCE — )?[^:]*:\s?(.*)$")
                                updated = 0
                                fr.undo_segments = copy.deepcopy(fr.segments)
                                for i, line in enumerate(new_lines):
                                    m = pattern.match(line)
                                    if m and m.group(1) != fr.segments[i].text:
                                        fr.segments[i].text = m.group(1)
                                        updated += 1
                                save_cache(fr)
                                st.success(f"Applied edits to {updated} line(s).")
                                st.rerun()
                        if fr.undo_segments is not None:
                            if st.button("↩ Undo Last Edit", key=f"undo_edit::{path}"):
                                fr.segments = fr.undo_segments
                                fr.undo_segments = None
                                st.session_state.pop(f"editbox::{path}", None)
                                st.session_state.pop(f"preview_cache::{path}", None)
                                save_cache(fr)
                                st.success("Reverted the last edit.")
                                st.rerun()

                    with st.expander("🔎 Search transcript"):
                        search_query = st.text_input(
                            "Search",
                            key=f"search::{path}",
                            label_visibility="collapsed",
                            placeholder="Search this transcript...",
                        )
                        if search_query.strip():
                            MAX_SEARCH_MATCHES = 200
                            query_lower = search_query.strip().lower()
                            matches = [
                                (seg, display_name)
                                for seg, display_name in _visible_segments(
                                    fr.segments,
                                    st.session_state[excluded_key],
                                    st.session_state.speaker_names[path],
                                )
                                if query_lower in seg.text.lower()
                            ]
                            if not matches:
                                st.caption("No matches.")
                            else:
                                caption = f"{len(matches)} match(es)"
                                if len(matches) > MAX_SEARCH_MATCHES:
                                    caption += f" — showing first {MAX_SEARCH_MATCHES}"
                                st.caption(caption)
                                for seg, display_name in matches[:MAX_SEARCH_MATCHES]:
                                    st.markdown(
                                        f"`[{fmt_timecode(seg.start)}]` **{display_name}:** {seg.text}"
                                    )

                    with st.expander("Preview transcript"):
                        # build_transcript is O(segments) and this block runs on every
                        # rerun for every finished file regardless of which expander is
                        # actually open — memoize per-file, keyed on the inputs that can
                        # actually change the output, so an unrelated click elsewhere in
                        # the UI doesn't re-format every long interview in the batch.
                        preview_sig = (
                            len(fr.segments),
                            frozenset(st.session_state[excluded_key]),
                            tuple(sorted(st.session_state.speaker_names[path].items())),
                            export_format,
                        )
                        preview_cache_key = f"preview_cache::{path}"
                        cached_sig, cached_content = st.session_state.get(preview_cache_key, (None, None))
                        if cached_sig == preview_sig:
                            preview_content = cached_content
                        else:
                            preview_content = build_transcript(
                                fr.name,
                                fr.segments,
                                st.session_state[excluded_key],
                                st.session_state.speaker_names[path],
                                export_format,
                                fr.path,
                            )
                            st.session_state[preview_cache_key] = (preview_sig, preview_content)
                        st.text(preview_content[:3000] + ("..." if len(preview_content) > 3000 else ""))

                    if st.button("💾 Save Transcript As...", key=f"save::{path}", use_container_width=True):
                        saved_path = export_single_file(fr, export_format)
                        if saved_path:
                            st.success(f"Saved to `{saved_path}`")
                            after_cols = st.columns(2)
                            with after_cols[0]:
                                if st.button("📂 Reveal in Finder", key=f"reveal::{path}", use_container_width=True):
                                    reveal_in_finder(saved_path)
                            with after_cols[1]:
                                if st.session_state.rough_cut_studio_path and st.button(
                                    "🎬 Open Rough Cut Studio", key=f"rcs::{path}", use_container_width=True
                                ):
                                    if launch_rough_cut_studio(st.session_state.rough_cut_studio_path):
                                        st.success("Launched — drag the exported file in when it opens.")
                                    else:
                                        st.error("Couldn't launch Rough Cut Studio.")
                        else:
                            st.info("Save cancelled.")

                st.markdown("</div>", unsafe_allow_html=True)

        done_files = [fr for fr in st.session_state.results.values() if fr.status == "done"]
        if done_files:
            if st.button("💾 Export All (prompts for each file)", use_container_width=True):
                written, cancelled = [], 0
                for fr in done_files:
                    saved_path = export_single_file(fr, export_format)
                    if saved_path:
                        written.append(saved_path)
                    else:
                        cancelled += 1
                if written:
                    st.success(f"Saved {len(written)} transcript(s).")
                    for p in written:
                        st.write(f"- `{p}`")
                    if st.session_state.rough_cut_studio_path:
                        if st.button("🎬 Open Rough Cut Studio", key="rcs::batch", use_container_width=True):
                            if launch_rough_cut_studio(st.session_state.rough_cut_studio_path):
                                st.success("Launched — drag the exported files in when it opens.")
                            else:
                                st.error("Couldn't launch Rough Cut Studio.")
                if cancelled:
                    st.info(f"{cancelled} save(s) were cancelled.")

        if st.session_state.results:
            if st.button("📋 Export Batch Summary (CSV)", use_container_width=True):
                csv_content = build_batch_summary_csv(list(st.session_state.results.values()))
                summary_path = browse_for_save_path("batch_summary.csv")
                if summary_path:
                    with open(summary_path, "w", encoding="utf-8", newline="") as f:
                        f.write(csv_content)
                    st.success(f"Saved batch summary to `{summary_path}`")
                else:
                    st.info("Save cancelled.")


if __name__ == "__main__":
    main()
