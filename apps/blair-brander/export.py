"""
export.py — local-only export. Frames are rendered with Pillow and, for
video, handed to the ffmpeg binary already installed on this machine via
subprocess. No frames or project data ever leave the computer.
"""

import os
import shutil
import subprocess

import multiprocessing

import brand
import renderer


def export_png(scene, out_path):
    img = renderer.render_still(scene)
    img.save(out_path, "PNG")
    return out_path


def _render_frame_bytes(args):
    """Top-level (picklable) worker for multiprocessing.Pool.

    Must stay a plain module-level function (not a lambda/closure/method):
    on macOS's default "spawn" start method, child processes re-import this
    module by reference to locate the target callable. export.py is a plain
    importable module (app.py is the __main__ entry point), so this import
    does not re-run any Tkinter GUI setup.
    """
    scene, t, elapsed = args
    frame = renderer.render_frame(scene, t=t, elapsed_seconds=elapsed)
    return frame.tobytes()


def _register_extra_logos(extra_logo_sources):
    """Pool `initializer`: on macOS's default "spawn" start method, every
    worker process gets its OWN fresh import of `brand` — any logo added at
    runtime (e.g. a host app's "Import Logo" feature registering a custom
    entry directly into brand.LOGO_SOURCES) exists only in the parent
    process's copy and is invisible here, so a render that references it
    raises KeyError deep in assets.load_transparent(). Passing that extra
    mapping through as `initargs` and merging it here, once per worker
    before any frames render, keeps every worker's LOGO_SOURCES consistent
    with the caller's. A no-op (empty dict) for callers that never
    register logos at runtime, so the standalone app's own export path is
    unaffected."""
    if extra_logo_sources:
        brand.LOGO_SOURCES.update(extra_logo_sources)


def _frame_times(scene, fps):
    """Per-frame (t, elapsed_seconds) pairs for the whole export (duration
    + hold_seconds). `t` is the existing 0..1 "duration-scaled" progress
    (clamped to 1.0 once elapsed passes `duration`) every animation branch
    in render_frame keys off. `elapsed_seconds` is real elapsed time from
    0 across the WHOLE clip including the hold tail — used only by
    effects that keep animating once t has settled (currently just
    logo_grow's continuous scale-up; see render_frame's docstring)."""
    duration = max(0.1, scene.get("duration", 3.0))
    hold = scene.get("hold_seconds", 1.0)   # extra time fully settled at the end
    total_frames = int(round((duration + hold) * fps))
    denom = max(1, int(duration * fps))
    return [(min(1.0, i / denom), i / fps) for i in range(total_frames)]


def _rle_times(frame_times, exact=False):
    """Run-length encode consecutive duplicate frames into
    [(t, elapsed), count] runs.

    render_frame(scene, t, elapsed_seconds) is a pure function of its
    inputs, so when `exact` is False, frames are deduped on `t` ALONE —
    safe because every animation branch (other than logo_grow) ignores
    elapsed_seconds entirely, so two frames sharing a `t` always render
    byte-identical regardless of elapsed. Every frame of the hold_seconds
    tail shares t == 1.0, so this collapses the whole tail to one render
    repeated `count` times (at 30fps and the default 1s hold, ~30
    full-canvas renders saved per export) without changing the frame
    count, order, or content ffmpeg receives.

    `exact=True` (pass whenever scene.get("logo_grow") — or any future
    effect that reads elapsed_seconds) dedupes on the FULL (t, elapsed)
    pair instead, so the hold tail is never collapsed — each of its
    frames genuinely differs once something animates through it. Only
    *consecutive* duplicates are folded either way, so this stays
    order-preserving for any times sequence, monotonic or not."""
    runs = []
    for t, elapsed in frame_times:
        key = (t, elapsed) if exact else t
        if runs and runs[-1][0] == key:
            runs[-1][2] += 1
        else:
            runs.append([key, (t, elapsed), 1])
    return [(t, elapsed, count) for _key, (t, elapsed), count in runs]


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def export_video(scene, out_path, fps=None, codec="mov", extra_logo_sources=None):
    """
    extra_logo_sources: optional {display_name: absolute_path} of logos
      registered at runtime (outside brand.py's own module-level
      LOGO_SOURCES) that the caller needs every render worker to know
      about too — see _register_extra_logos above. None/omitted for a
      plain export of only brand.py's built-in logos.

    codec:
      "mov"  -> QuickTime Animation codec (qtrle). RECOMMENDED DEFAULT.
                Real alpha channel, and every video editor plus ffmpeg's
                own default decoder reads its transparency back correctly
                with zero extra flags. Files are larger than webm but this
                is the safest choice for editors like Premiere, DaVinci
                Resolve, and Final Cut.
      "webm" -> VP9 with alpha (yuva420p). Smaller files. The alpha data
                genuinely is embedded (verified during testing), but some
                tools' *default* VP9 decode path ignores the alpha plane
                unless they explicitly invoke the libvpx-vp9 decoder. If
                your editor shows a black box instead of transparency,
                re-export as mov instead.
    """
    if not ffmpeg_available():
        raise RuntimeError(
            "ffmpeg was not found on this computer. Install it locally "
            "(it is free and open-source) from https://ffmpeg.org/download.html "
            "then try exporting again."
        )

    fps = fps or brand.FPS
    W, H = scene.get("canvas_size", brand.CANVAS_SIZE)
    frame_times = _frame_times(scene, fps)

    if codec == "mov":
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pix_fmt", "rgba",
            "-s", f"{W}x{H}",
            "-r", str(fps),
            "-i", "-",
            "-c:v", "qtrle",
            out_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pix_fmt", "rgba",
            "-s", f"{W}x{H}",
            "-r", str(fps),
            "-i", "-",
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-auto-alt-ref", "0",
            "-b:v", "0",
            "-crf", "18",
            out_path,
        ]

    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    # One render per distinct consecutive (t, elapsed) — the hold tail
    # collapses to a single render whose bytes are written count times,
    # UNLESS logo_grow (or any future hold-tail-animating effect) needs
    # every one of those frames rendered individually — see _rle_times.
    runs = _rle_times(frame_times, exact=bool(scene.get("logo_grow")))
    worker_args = [(scene, t, elapsed) for t, elapsed, _count in runs]
    broken_pipe = False
    try:
        with multiprocessing.Pool(processes=os.cpu_count(),
                                   initializer=_register_extra_logos,
                                   initargs=(extra_logo_sources or {},)) as pool:
            for (_t, _elapsed, count), frame_bytes in zip(
                    runs, pool.imap(_render_frame_bytes, worker_args)):
                try:
                    for _ in range(count):
                        proc.stdin.write(frame_bytes)
                except BrokenPipeError:
                    broken_pipe = True
                if broken_pipe:
                    break
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        # Popen.communicate() unconditionally flushes self.stdin if it's
        # non-None, even though we just closed it above — raises
        # "ValueError: flush of closed file". Clearing the reference makes
        # communicate() skip that stale flush/close attempt entirely.
        proc.stdin = None

    try:
        _, stderr = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError("ffmpeg did not finish muxing within 120s after the last frame")
    if proc.returncode != 0 or broken_pipe:
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        raise RuntimeError(f"ffmpeg failed:\n{stderr_text[-2000:]}")

    return out_path
