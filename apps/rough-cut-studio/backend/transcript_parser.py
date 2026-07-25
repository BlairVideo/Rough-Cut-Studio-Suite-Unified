"""
transcript_parser.py

Parses timecoded video transcripts into a normalized list of segments:

    {
        "index": int,            # 0-based order within this source
        "start_seconds": float,  # start time in seconds
        "end_seconds": float,    # end time in seconds
        "start_tc": "HH:MM:SS:FF",  # SMPTE timecode at the given frame rate
        "end_tc": "HH:MM:SS:FF",
        "speaker": str | None,
        "text": str,
    }

Supported input formats (auto-detected per file):
  1. SRT           "1\n00:00:01,000 --> 00:00:04,000\nHello there\n\n"
  2. WebVTT        "00:00:01.000 --> 00:00:04.000\nHello there"
  3. Bracket/arrow "[00:00:01:00 - 00:00:04:00] SPEAKER: Hello there"
                   "00:00:01:00 --> 00:00:04:00  Hello there"
  4. Single-timecode-per-line (e.g. auto-generated interview transcripts):
                   "00:12:34 Jane: We started filming in March."
     End time for each line is inferred from the start of the next line
     (or +4s for the final line).

No network access and no external services are used for parsing; this is
pure text processing.
"""

import os
import re
from dataclasses import dataclass, asdict
from typing import List, Optional


TIMECODE_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})(?:[:;,.](\d{1,3}))?"
)

SRT_BLOCK_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{1,3})"
)

BRACKET_ARROW_RE = re.compile(
    r"[\[\(]?\s*(\d{1,2}:\d{2}:\d{2}(?:[:,.]\d{1,3})?)\s*(?:-->|-|to)\s*"
    r"(\d{1,2}:\d{2}:\d{2}(?:[:,.]\d{1,3})?)\s*[\]\)]?\s*(.*)"
)

SINGLE_TC_LINE_RE = re.compile(
    r"^\s*[\[\(]?(\d{1,2}:\d{2}:\d{2}(?:[:,.]\d{1,3})?)[\]\)]?\s*[-–:]?\s*(.*)$"
)

SPEAKER_RE = re.compile(r"^\s*([A-Za-z0-9 ._'-]{1,30}):\s*(.*)$")


@dataclass
class Segment:
    index: int
    start_seconds: float
    end_seconds: float
    start_tc: str
    end_tc: str
    speaker: Optional[str]
    text: str

    def to_dict(self):
        return asdict(self)


def _parse_fraction(frac: Optional[str], is_frame_field: bool, fps: float) -> float:
    """Interpret the 4th timecode group as either milliseconds or a frame count."""
    if not frac:
        return 0.0
    if is_frame_field:
        # value is a frame count, e.g. HH:MM:SS:FF
        frames = int(frac)
        return frames / fps
    # value is milliseconds, left-padded/truncated to 3 digits
    ms = frac.ljust(3, "0")[:3]
    return int(ms) / 1000.0


def is_drop_frame_capable(fps: float) -> bool:
    """Drop-frame timecode is only a defined convention at 29.97 and 59.94
    fps (it exists to keep the displayed clock aligned with wall-clock time
    at NTSC rates). At other rates the concept doesn't apply."""
    return abs(fps - 29.97) < 0.01 or abs(fps - 59.94) < 0.01


def _drop_count_for(fps_int: int) -> int:
    # 2 frame numbers dropped per non-exempt minute at ~30fps, 4 at ~60fps.
    return 4 if fps_int == 60 else 2


def _frame_to_dropframe_components(total_frames: int, fps_int: int, drop: int):
    """SMPTE drop-frame encode: maps a raw frame count to the (h, m, s, f)
    the displayed clock would show, skipping frame numbers 0..drop-1 at the
    start of every minute except every 10th. This only relabels frames —
    it never changes how many frames exist or where they sit in the edit."""
    frames_per_min = fps_int * 60 - drop
    frames_per_10min = fps_int * 600 - drop * 9
    d, m = divmod(total_frames, frames_per_10min)
    if m < drop:
        adj_frame = total_frames + drop * 9 * d
    else:
        adj_frame = total_frames + drop * 9 * d + drop * ((m - drop) // frames_per_min)
    frames = adj_frame % fps_int
    total_secs = adj_frame // fps_int
    s = total_secs % 60
    mnt = (total_secs // 60) % 60
    h = total_secs // 3600
    return int(h), int(mnt), int(s), int(frames)


def _dropframe_components_to_frame(h: int, m: int, s: int, f: int, fps_int: int, drop: int) -> int:
    """Inverse of _frame_to_dropframe_components: a displayed drop-frame
    clock reading back to a raw frame count."""
    total_minutes = 60 * h + m
    frame_number = fps_int * 3600 * h + fps_int * 60 * m + fps_int * s + f
    frame_number -= drop * (total_minutes - total_minutes // 10)
    return frame_number


def timecode_to_seconds(tc: str, fps: float, drop_frame: bool = False) -> float:
    tc = tc.strip()
    m = TIMECODE_RE.match(tc)
    if not m:
        raise ValueError(f"Unrecognized timecode: {tc!r}")
    h, mnt, s, frac = m.groups()
    sep_match = re.search(r"\d{2}([:;,.])\d{1,3}$", tc)
    sep = sep_match.group(1) if sep_match else None
    is_frame_field = sep in (":", ";")

    # Drop-frame timecode is conventionally written with a ';' before the
    # frames field, but plenty of real-world sources (camera-burned
    # timecode, some transcription tools) label drop-frame with an ordinary
    # ':' instead. Gating this on the separator alone meant a project with
    # drop-frame explicitly enabled would silently fall back to non-drop
    # math for any ':'-separated timecode, producing a systematic drift
    # with no warning. The project-level `drop_frame` flag is what the user
    # actually chose (see the "drop-frame timecode" checkbox, only shown at
    # 29.97/59.94 fps) -- once that's on, any frame-count-style separator
    # (':' or ';') should be interpreted as drop-frame, not just ';'.
    # Millisecond-style separators (',' '.') are never drop-frame notation
    # regardless, and are already excluded via `is_frame_field`.
    use_df = drop_frame and is_drop_frame_capable(fps) and is_frame_field
    if use_df:
        fps_int = max(1, round(fps))
        drop = _drop_count_for(fps_int)
        frames_field = int(frac) if frac else 0
        raw_frames = _dropframe_components_to_frame(int(h), int(mnt), int(s), frames_field, fps_int, drop)
        return raw_frames / fps

    total = int(h) * 3600 + int(mnt) * 60 + int(s)
    total += _parse_fraction(frac, is_frame_field, fps)
    return total


def seconds_to_smpte(total_seconds: float, fps: float, drop_frame: bool = False) -> str:
    if total_seconds < 0:
        total_seconds = 0.0
    total_frames = round(total_seconds * fps)
    fps_int = max(1, round(fps))

    if drop_frame and is_drop_frame_capable(fps):
        drop = _drop_count_for(fps_int)
        h, m, s, frames = _frame_to_dropframe_components(total_frames, fps_int, drop)
        return f"{h:02d}:{m:02d}:{s:02d};{frames:02d}"

    frames = int(total_frames % fps_int)
    total_secs = total_frames // fps_int
    s = int(total_secs % 60)
    m = int((total_secs // 60) % 60)
    h = int(total_secs // 3600)
    return f"{h:02d}:{m:02d}:{s:02d}:{frames:02d}"


def seconds_to_frames(total_seconds: float, fps: float) -> int:
    return max(0, round(total_seconds * fps))


def _finalize(raw_segments, fps: float) -> List[Segment]:
    segments = []
    for i, (start_s, end_s, speaker, text) in enumerate(raw_segments):
        text = text.strip()
        if not text:
            continue
        if end_s <= start_s:
            end_s = start_s + 0.5
        segments.append(
            Segment(
                index=i,
                start_seconds=round(start_s, 3),
                end_seconds=round(end_s, 3),
                start_tc=seconds_to_smpte(start_s, fps),
                end_tc=seconds_to_smpte(end_s, fps),
                speaker=speaker,
                text=text,
            )
        )
    # re-index after any drops
    for i, seg in enumerate(segments):
        seg.index = i
    return segments


def _split_speaker(text: str):
    m = SPEAKER_RE.match(text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, text.strip()


def _parse_srt_or_vtt(content: str, fps: float) -> List[Segment]:
    raw = []
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = SRT_BLOCK_RE.search(line)
        if m:
            start_s = timecode_to_seconds(m.group(1), fps)
            end_s = timecode_to_seconds(m.group(2), fps)
            i += 1
            text_lines = []
            while i < n and lines[i].strip() != "":
                text_lines.append(lines[i].strip())
                i += 1
            text = " ".join(text_lines)
            speaker, text = _split_speaker(text)
            raw.append((start_s, end_s, speaker, text))
        i += 1
    return _finalize(raw, fps)


def _parse_bracket_arrow(content: str, fps: float) -> List[Segment]:
    raw = []
    for line in content.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        m = BRACKET_ARROW_RE.match(line.strip())
        if not m:
            continue
        start_s = timecode_to_seconds(m.group(1), fps)
        end_s = timecode_to_seconds(m.group(2), fps)
        speaker, text = _split_speaker(m.group(3))
        raw.append((start_s, end_s, speaker, text))
    return _finalize(raw, fps)


def _parse_single_timecode_lines(content: str, fps: float, default_tail=4.0) -> List[Segment]:
    entries = []
    for line in content.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        m = SINGLE_TC_LINE_RE.match(line)
        if not m:
            continue
        start_s = timecode_to_seconds(m.group(1), fps)
        speaker, text = _split_speaker(m.group(2))
        entries.append([start_s, None, speaker, text])
    for i in range(len(entries)):
        if i + 1 < len(entries):
            entries[i][1] = entries[i + 1][0]
        else:
            entries[i][1] = entries[i][0] + default_tail
    return _finalize([tuple(e) for e in entries], fps)


def parse_transcript(content: str, fps: float = 25.0) -> List[Segment]:
    """
    Auto-detects the transcript format and returns a normalized list of Segment.
    Tries the richest/most specific formats first.
    """
    if not content or not content.strip():
        return []

    if SRT_BLOCK_RE.search(content):
        segs = _parse_srt_or_vtt(content, fps)
        if segs:
            return segs

    # Try bracket/arrow style line-by-line
    sample_lines = [l for l in content.split("\n") if l.strip()][:20]
    bracket_hits = sum(1 for l in sample_lines if BRACKET_ARROW_RE.match(l.strip()))
    if bracket_hits >= max(1, len(sample_lines) // 3):
        segs = _parse_bracket_arrow(content, fps)
        if segs:
            return segs

    # Fall back to single-timecode-per-line
    segs = _parse_single_timecode_lines(content, fps)
    return segs


def parse_duration_string(text) -> "float | None":
    """
    Parses a human-entered target duration into seconds. Accepts:
      "90"        -> 90.0
      "90s"       -> 90.0
      "1:30"      -> 90.0   (MM:SS)
      "01:02:03"  -> 3723.0 (HH:MM:SS)
    Returns None for empty input, and raises ValueError for anything else
    unparseable (so the caller can show a clear message instead of
    silently ignoring a typo).
    """
    if text is None:
        return None
    text = str(text).strip().lower().rstrip("s")
    if not text:
        return None

    if ":" in text:
        parts = text.split(":")
        if not all(p.isdigit() for p in parts):
            raise ValueError(f"Couldn't read '{text}' as a duration.")
        parts = [int(p) for p in parts]
        if len(parts) == 2:
            m, s = parts
            return float(m * 60 + s)
        if len(parts) == 3:
            h, m, s = parts
            return float(h * 3600 + m * 60 + s)
        raise ValueError(f"Couldn't read '{text}' as a duration.")

    try:
        return float(text)
    except ValueError:
        raise ValueError(f"Couldn't read '{text}' as a duration.")


def seconds_to_duration_label(total_seconds: float) -> str:
    """Human-readable duration for display, e.g. 92.4 -> '1m 32s'."""
    total_seconds = max(0, round(total_seconds))
    m, s = divmod(total_seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def parse_transcript_file(path: str, fps: float = 25.0) -> List[Segment]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return parse_transcript(content, fps)


# Video containers we'll look for when matching a transcript to its source
# video by filename (used when no embedded path is found or it's stale).
# .braw (Blackmagic RAW) is included so Studio Suite's embedded Edit
# workspace can link/auto-link a BRAW source at all -- Studio Suite itself
# transparently substitutes a cached, ordinary-container proxy wherever
# this app would actually decode the file (thumbnails, preview, preview
# export); this app's own code never needs to know BRAW exists.
VIDEO_EXTENSIONS = (".mp4", ".mov", ".mxf", ".avi", ".mkv", ".m4v", ".braw")

# Transcript file types this app knows how to parse -- mirrors the dialog
# filter in SourceManager.pick_transcript_files() ("Transcripts
# (*.srt;*.vtt;*.txt)"). Used to validate transcript paths that didn't come
# from that dialog (e.g. a `path` field loaded from a project file), so a
# project-file-driven load can't be used to read arbitrary files on disk.
TRANSCRIPT_EXTENSIONS = (".srt", ".vtt", ".txt")

_SOURCE_VIDEO_RE = re.compile(r"source\s*video\s*:\s*(.+)", re.IGNORECASE)


def detect_linked_media(transcript_path: str, content: str) -> Optional[str]:
    """
    Looks for the source video path that the transcription app embeds when
    exporting: a `# Source video: <path>` header in .txt exports, or a
    WebVTT `NOTE` block containing the same line. SRT has no comment syntax,
    so for .srt files (or if the embedded path is missing/stale) this falls
    back to matching a video file with the same name as the transcript in
    the same folder.

    Returns an existing file path, or None if nothing could be found.
    """
    m = _SOURCE_VIDEO_RE.search(content)
    if m:
        candidate = m.group(1).strip().strip('"')
        if candidate and os.path.exists(candidate):
            return candidate

    # Fallback: same file name, common video extension, same directory.
    directory = os.path.dirname(os.path.abspath(transcript_path))
    stem = os.path.splitext(os.path.basename(transcript_path))[0]
    for ext in VIDEO_EXTENSIONS:
        candidate = os.path.join(directory, stem + ext)
        if os.path.exists(candidate):
            return candidate

    return None
