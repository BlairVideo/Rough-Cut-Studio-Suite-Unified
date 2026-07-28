"""
ffmpeg_graph.py -- turns a GradeState (+ optional creative LUT + in/out
points + an output preset) into an ffmpeg command line, and parses
ffmpeg's `-progress pipe:1` output back into (percent, detail) pairs a
JobManager thread job can feed straight to progress_cb.

Export strategy: rather than hand-translating each grade control into a
different ffmpeg filter (risking order-of-operations drift from the
GLSL preview shader), `bake_grade_lut` samples grade.apply_grade_to_rgb
across a 3D lattice -- the exact same function the live preview mirrors
in GLSL -- and writes it as a .cube file. Export then applies that one
baked LUT via ffmpeg's own `lut3d` filter. This guarantees the exported
frame matches the previewed frame exactly, instead of hoping a chain of
eq/colorbalance/curves filters reproduces the shader's math.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

from grade import GradeState, apply_grade_to_rgb
from lut import CubeLut, sample_lut, write_cube

DEFAULT_BAKE_SIZE = 33  # ffmpeg's own lut3d comfortably handles up to 65; 33 is a fast, visually lossless default

# Named delivery presets. `args` are appended after `-i <source>` and the
# baked-LUT `-vf`; callers may override container/codec but these cover
# the two cases called out in the plan: share-ready H.264 and archive
# -grade ProRes. Both use VideoToolbox hardware encoding rather than
# libx264/prores_ks software encoding -- every machine this suite runs on
# is Apple Silicon (see the root CLAUDE.md's Local-First Philosophy and
# interview-transcriber's own Apple-Silicon-only mlx-whisper dependency),
# so there's no software-encode-by-default path to preserve. Benchmarked
# on a 1080p/20s clip: h264_videotoolbox ran ~2x faster than libx264
# -preset slow -crf 18 at a fifth of the CPU load; prores_videotoolbox
# ran ~4.4x faster than prores_ks. VideoToolbox has no CRF-style rate
# control, so share_h264's bitrate is picked per-clip by
# _h264_bitrate_for_height below rather than fixed here -- the suite's
# footage is almost all 4K, and a bitrate sized for 1080p would band
# badly upscaled to that, while one sized for 4K would bloat a rare
# 1080p export for no visual gain.
#
# -allow_sw 1 on both encoders: Apple's hardware AVC encoder tops out at
# H.264 level 5.2 (4096px-class frame widths) and the hardware session
# can also simply be busy/unavailable (multiple concurrent VT sessions,
# another app holding it) -- either way `Cannot create compression
# session: -12903` is VideoToolbox refusing to open a hardware session,
# and ffmpeg's own stderr for that error literally suggests this flag.
# It's a fallback, not a default swap: VT still tries hardware first and
# only drops to software when hardware init fails, so normal 4K/1080p
# exports are unaffected -- this only kicks in for the sources (5K+,
# hardware-contended) that would otherwise hard-fail the export.
OUTPUT_PRESETS = {
    "share_h264": {
        "container": "mp4",
        "video_args": ["-c:v", "h264_videotoolbox", "-allow_sw", "1", "-b:v", "12M", "-pix_fmt", "yuv420p"],
        "audio_args": ["-c:a", "aac", "-b:a", "192k"],
    },
    "archive_prores422": {
        "container": "mov",
        "video_args": ["-c:v", "prores_videotoolbox", "-allow_sw", "1", "-profile:v", "3", "-pix_fmt", "yuv422p10le"],
        "audio_args": ["-c:a", "pcm_s16le"],
    },
}

# (max_height_exclusive, bitrate) tiers for share_h264's -b:v, checked in
# order -- the first tier whose bound exceeds the source height wins.
# Sized generously for "share-ready" quality, not minimum-viable: 40M at
# 2160p comfortably clears typical 4K30 delivery bitrates (YouTube's own
# 4K SDR upload guidance sits around 35-45M) so hardware-encoded output
# doesn't look like a downgrade next to the old libx264 -crf 18 path.
_H264_BITRATE_TIERS = [
    (721, "6M"),     # <=720p
    (1081, "12M"),   # <=1080p
    (1441, "20M"),   # <=1440p/QHD
    (2161, "40M"),   # <=2160p/4K
]
_H264_BITRATE_ABOVE_4K = "64M"  # 6K/8K source footage


def _h264_bitrate_for_height(height: Optional[int]) -> str:
    """Falls back to the 1080p tier when height is unknown (e.g. probing
    failed) -- matches share_h264's own OUTPUT_PRESETS default."""
    if not height:
        return "12M"
    for max_height, bitrate in _H264_BITRATE_TIERS:
        if height < max_height:
            return bitrate
    return _H264_BITRATE_ABOVE_4K


def bake_grade_lut(grade: GradeState, creative_lut: Optional[CubeLut] = None,
                    size: int = DEFAULT_BAKE_SIZE) -> CubeLut:
    """Samples the full grade pipeline (and, if present, the creative LUT
    blended at grade.lut_intensity) across a size**3 lattice."""
    data = []
    denom = size - 1 if size > 1 else 1
    intensity = max(0.0, min(1.0, grade.lut_intensity / 100.0)) if creative_lut else 0.0
    for bi in range(size):
        b_in = bi / denom
        for gi in range(size):
            g_in = gi / denom
            for ri in range(size):
                r_in = ri / denom
                r, g, b = apply_grade_to_rgb(r_in, g_in, b_in, grade)
                if creative_lut is not None and intensity > 0.0:
                    lr, lg, lb = sample_lut(creative_lut, r, g, b)
                    r = r + (lr - r) * intensity
                    g = g + (lg - g) * intensity
                    b = b + (lb - b) * intensity
                data.append((r, g, b))
    return CubeLut(size=size, data=data, title="Colorize Bake")


def _format_seconds(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{secs:09.6f}"


@dataclass
class ExportSpec:
    source_path: str
    output_path: str
    grade: GradeState
    in_seconds: float = 0.0
    out_seconds: Optional[float] = None   # None = to end of clip
    creative_lut: Optional[CubeLut] = None
    preset: str = "share_h264"
    ffmpeg_bin: str = "ffmpeg"
    bake_size: int = DEFAULT_BAKE_SIZE
    source_height: Optional[int] = None   # drives share_h264's -b:v tier; None = 1080p default


def build_export_command(spec: ExportSpec, baked_lut_path: str) -> List[str]:
    if spec.preset not in OUTPUT_PRESETS:
        raise ValueError(f"Unknown output preset: {spec.preset}")
    preset = OUTPUT_PRESETS[spec.preset]
    video_args = list(preset["video_args"])
    if spec.preset == "share_h264":
        bv_index = video_args.index("-b:v")
        video_args[bv_index + 1] = _h264_bitrate_for_height(spec.source_height)

    duration = None
    if spec.out_seconds is not None:
        duration = max(0.0, spec.out_seconds - spec.in_seconds)

    cmd = [spec.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error"]
    if spec.in_seconds > 0:
        cmd += ["-ss", _format_seconds(spec.in_seconds)]
    cmd += ["-i", spec.source_path]
    if duration is not None:
        cmd += ["-t", _format_seconds(duration)]
    cmd += ["-vf", f"lut3d=file={baked_lut_path}"]
    cmd += video_args
    cmd += preset["audio_args"]
    cmd += ["-progress", "pipe:1", "-nostats", spec.output_path]
    return cmd


def prepare_export(spec: ExportSpec, tmp_dir: Optional[str] = None) -> List[str]:
    """Bakes the grade (+ optional creative LUT) into a temp .cube file
    and returns the full ffmpeg argv. Caller owns cleanup of the temp
    LUT file (its path is embedded in the returned argv, after the
    `-vf lut3d=file='...'` token, if it needs to be removed post-export)."""
    baked = bake_grade_lut(spec.grade, spec.creative_lut, size=spec.bake_size)
    fd, lut_path = tempfile.mkstemp(suffix=".cube", prefix="colorize_bake_", dir=tmp_dir)
    os.close(fd)
    write_cube(baked, lut_path)
    return build_export_command(spec, lut_path)


_TIME_RE = re.compile(r"^out_time_ms=(\d+)$")
_PROGRESS_RE = re.compile(r"^progress=(continue|end)$")


def parse_progress_line(line: str, total_duration_seconds: Optional[float]) -> Optional[dict]:
    """Parses one line of ffmpeg's `-progress pipe:1` key=value stream.
    Returns {"percent": 0-100 or None, "done": bool} for a recognized
    line, or None for a line this parser doesn't act on (most lines --
    frame=, fps=, bitrate=, etc. -- are ignored; only out_time_ms and
    progress are needed to drive a percent bar)."""
    line = line.strip()
    if not line:
        return None
    m = _TIME_RE.match(line)
    if m:
        if not total_duration_seconds or total_duration_seconds <= 0:
            return None
        out_seconds = int(m.group(1)) / 1_000_000.0
        pct = max(0.0, min(100.0, (out_seconds / total_duration_seconds) * 100.0))
        return {"percent": pct, "done": False}
    m = _PROGRESS_RE.match(line)
    if m:
        return {"percent": 100.0 if m.group(1) == "end" else None, "done": m.group(1) == "end"}
    return None


def run_export(spec: ExportSpec, total_duration_seconds: Optional[float],
                progress_cb: Callable[[float, str], None],
                cancel_event, subprocess_module=None) -> dict:
    """Runs the full export: bake -> ffmpeg subprocess -> progress
    parsing -> temp LUT cleanup. `subprocess_module` is injectable for
    tests; defaults to the real `subprocess`. Cooperative cancellation
    mirrors braw_bridge._run_proxy_tool: cancel_event is polled once per
    stdout line and the process is terminated if set."""
    import subprocess as _subprocess
    subprocess = subprocess_module or _subprocess

    progress_cb(2, "Baking grade LUT…")
    baked = bake_grade_lut(spec.grade, spec.creative_lut, size=spec.bake_size)
    fd, lut_path = tempfile.mkstemp(suffix=".cube", prefix="colorize_bake_")
    os.close(fd)
    write_cube(baked, lut_path)

    try:
        cmd = build_export_command(spec, lut_path)
        progress_cb(5, f"Exporting {os.path.basename(spec.output_path)}…")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )

        # stderr is drained concurrently on its own thread (matching
        # braw_bridge._run_proxy_tool) rather than read after the stdout
        # loop -- if ffmpeg fills its stderr pipe buffer while stdout
        # isn't being read (e.g. right after a cancel), a synchronous
        # read-after-loop would risk deadlocking on that unread pipe.
        stderr_lines: List[str] = []

        def _drain_stderr():
            try:
                for line in proc.stderr:
                    stderr_lines.append(line)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                break
            parsed = parse_progress_line(line, total_duration_seconds)
            if parsed and parsed["percent"] is not None:
                progress_cb(max(5.0, min(99.0, parsed["percent"])), "Encoding…")

        if cancel_event is not None and cancel_event.is_set():
            proc.terminate()
            raise RuntimeError("Cancelled")

        returncode = proc.wait()
        stderr_thread.join(timeout=2.0)
        if returncode != 0:
            raise RuntimeError(
                f"ffmpeg exited with code {returncode}: {''.join(stderr_lines)[-2000:]}")

        progress_cb(100, "Export finished")
        return {"path": spec.output_path}
    finally:
        try:
            os.remove(lut_path)
        except OSError:
            pass
