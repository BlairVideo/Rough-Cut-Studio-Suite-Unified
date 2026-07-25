"""
video_export.py

Renders the current main-track cut list into a single, real video file
using ffmpeg -- something you can hand to someone or watch outside the
app, as opposed to the in-app player which only plays sequentially and
produces nothing you can save.

Scope, deliberately: this exports the MAIN TRACK ONLY, same as the in-app
"Preview Script" player. B-roll overlays aren't composited in -- doing
that correctly (position, size, timing) is a real video-compositing
problem, not just concatenation, and this feature is a fast rough-cut
preview, not a renderer. Once picture-lock matters enough to need B-roll
baked in, that's what the Premiere/Final Cut XML exports and a real NLE
are for.

Approach: each cut becomes its own ffmpeg input, trimmed with `-ss`/`-t`
(fast input-side seeking -- see the accuracy note below), then every input
is normalized to a common resolution/frame rate/audio format and
concatenated via ffmpeg's `concat` *filter* (not the concat demuxer, which
requires identical source formats and can't trim). Re-encoding is
unavoidable here since sources may differ in resolution, frame rate, or
codec.

Accuracy note: `-ss` before `-i` seeks to the nearest keyframe rather than
the exact frame, which is fast but can be off by a fraction of a second on
footage with a large GOP size. That's an acceptable tradeoff for a preview
export -- it's for judging pacing and story, not frame-accurate delivery.
Frame-accurate cuts are exactly what the XML exports + a real NLE give you.

No network access is used here; this only shells out to a local ffmpeg.
"""

import shutil
import subprocess


def ffmpeg_path():
    return shutil.which("ffmpeg")


def build_preview_export(clip_specs, output_path, fps, video_width=1920, video_height=1080, timeout=1800):
    """
    clip_specs: ordered list of {"source_path": str, "in_seconds": float, "out_seconds": float}

    Returns (ok: bool, error_message: str | None).
    """
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return False, "ffmpeg not found on this machine -- video preview export needs it."
    if not clip_specs:
        return False, "No cuts to export."

    cmd = [ffmpeg, "-y"]
    filter_parts = []
    concat_refs = ""

    for i, spec in enumerate(clip_specs):
        duration = max(0.04, spec["out_seconds"] - spec["in_seconds"])  # floor: at least one frame-ish
        cmd += ["-ss", f"{spec['in_seconds']:.3f}", "-t", f"{duration:.3f}", "-i", spec["source_path"]]
        # Normalize every clip to the same resolution (letterboxed, not
        # cropped, so nothing important gets cut off), frame rate, and
        # audio format before handing them to concat -- required whenever
        # sources differ, and harmless when they don't.
        filter_parts.append(
            f"[{i}:v]scale={video_width}:{video_height}:force_original_aspect_ratio=decrease,"
            f"pad={video_width}:{video_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
            f"setpts=PTS-STARTPTS[v{i}];"
        )
        filter_parts.append(
            f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}];"
        )
        concat_refs += f"[v{i}][a{i}]"

    filter_complex = "".join(filter_parts) + f"{concat_refs}concat=n={len(clip_specs)}:v=1:a=1[outv][outa]"

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        return False, "Export timed out -- try a shorter cut, or export in smaller pieces."
    except OSError as e:
        return False, f"Couldn't run ffmpeg: {e}"

    if result.returncode != 0:
        # ffmpeg's stderr is long; the actual error is almost always in the
        # last few lines, so surface just that rather than a wall of text.
        tail_lines = [ln for ln in result.stderr.strip().splitlines() if ln.strip()][-8:]
        return False, "ffmpeg failed:\n" + "\n".join(tail_lines)

    return True, None
