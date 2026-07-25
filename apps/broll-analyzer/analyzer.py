"""
analyzer.py
Core video analysis engine for the B-Roll Analyzer app.

For each video clip, samples frames at a fixed interval and scores them on:
  - Sharpness (focus quality, via variance of Laplacian)
  - Exposure (correct brightness, low clipping)
  - Stability / motion quality (penalizes shaky/jittery footage, doesn't
    penalize smooth pans or steady motion)
  - Optionally, "high energy / exciting shot" content, via a local
    (no cloud, no Anthropic API) CLIP-based vision model -- see
    vision_energy.py. Disabled by default; enable with enable_energy=True.

It then finds the best contiguous segment(s) within the clip (a sliding
window) and reports an overall clip score plus recommended in/out points.
"""

import os
import cv2
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Callable

from rcs_utils.ffprobe_util import probe_video_fps, probe_json

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mxf", ".mkv", ".mts", ".m2ts",
                     ".ts", ".webm", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp"}

# OpenCV auto-selects a decode backend per-platform/extension, and that
# choice isn't guaranteed to be the one with the broadest codec support.
# On Windows in particular, cv2 often prefers Media Foundation (MSMF)
# ahead of FFmpeg for common containers -- MSMF's codec support depends
# on what's installed system-wide and commonly can't handle the
# professional/camera-native codecs this app explicitly targets (MXF
# wrappers, ProRes in .mov, etc.), sometimes opening "successfully" but
# decoding garbage, other times failing to open at all. The FFmpeg
# backend bundled with opencv-python is built with broad codec support
# and is the one this app is actually tested against, so it's requested
# explicitly first; only if that's unavailable in a given OpenCV build
# do we fall back to whatever OpenCV would have auto-selected.
_PREFERRED_BACKENDS = [cv2.CAP_FFMPEG, cv2.CAP_ANY]


def limit_opencv_threads() -> None:
    """Call once per process before any decode/analysis happens on it
    (see app.py's ProcessPoolExecutor `initializer=` -- each worker
    process calls this exactly once, when it starts). OpenCV's own
    calls (Laplacian, optical flow) multithread internally across all
    CPU cores by default. Since each analysis worker here is already
    its own OS process running in parallel with `num_workers` sibling
    processes, letting OpenCV ALSO fan out across every core inside
    each process causes severe oversubscription (num_workers processes
    x cpu_count threads each) instead of clean parallel scaling."""
    cv2.setNumThreads(1)


def _open_video_capture(path: str) -> "cv2.VideoCapture":
    """Open `path` trying the FFmpeg backend first (see above), falling
    back to OpenCV's default auto-selected backend if FFmpeg support
    isn't compiled into this OpenCV build. Always returns a
    VideoCapture object -- callers check .isOpened() same as before."""
    for backend in _PREFERRED_BACKENDS:
        cap = cv2.VideoCapture(path, backend)
        if cap.isOpened():
            return cap
        cap.release()
    return cv2.VideoCapture(path)  # last resort, same as the old behavior


def _probe_fps_fallback(path: str) -> Optional[float]:
    """Some containers (variable-frame-rate web-sourced clips, certain
    transport streams) report 0 or no FPS via OpenCV's metadata read
    even though the file is otherwise decodable. ffprobe frequently
    still resolves a usable rate (preferring the container's average
    over its nominal rate for VFR content) since it reads the stream
    more thoroughly than OpenCV's lightweight metadata query. Read-only,
    local, no network -- same trust model as the existing audio-format
    probe. Returns None (not a guess) if ffprobe is missing or can't
    determine a rate either -- callers already handle an unknown fps as
    a clean per-file failure rather than assuming a default."""
    return probe_video_fps(path, timeout=15)

# ---- Tunable weights -------------------------------------------------
WEIGHT_SHARPNESS = 0.40
WEIGHT_EXPOSURE = 0.25
WEIGHT_STABILITY = 0.35

SAMPLE_INTERVAL_SEC = 0.5      # how often we grab a frame to score
ANALYSIS_MAX_DIM = 480         # downscale frames before analysis for speed


@dataclass
class FrameSample:
    time_sec: float
    sharpness: float
    exposure: float
    motion_mag: float          # raw optical flow magnitude (mean)
    motion_jitter: float       # frame-to-frame direction/magnitude instability
    energy: float = 0.0        # optional local-vision-model "excitement" score, 0-100


@dataclass
class Segment:
    """A single recommended in/out range within a clip."""
    start: float
    end: float
    score: float


@dataclass
class ClipResult:
    path: str
    filename: str
    duration: float
    fps: float
    width: int
    height: int
    overall_score: float = 0.0
    best_window_start: float = 0.0
    best_window_end: float = 0.0
    best_window_score: float = 0.0
    # All recommended segments for this clip, ordered by start time.
    # best_window_* above mirrors segments[0] sorted by score (highest
    # first) for backwards compatibility with code that only wants one.
    segments: List[Segment] = field(default_factory=list)
    samples: List[FrameSample] = field(default_factory=list)
    error: Optional[str] = None
    # Optional local-vision-model ("high energy / exciting shot") scoring.
    # Fully local (CLIP via open_clip) -- no cloud/Anthropic API involved.
    energy_enabled: bool = False
    mean_energy_score: float = 0.0
    energy_error: Optional[str] = None
    # Source audio format, probed from the file itself (see
    # _probe_audio_format) so the exported sequence can carry the
    # clip's *actual* channel count/sample rate/bit depth rather than
    # assuming every clip is generic stereo.
    audio_channels: int = 2
    audio_samplerate: int = 48000
    audio_bit_depth: int = 16
    # Human-readable channel preset (e.g. "Mono", "Stereo", "5.1",
    # "Adaptive (4ch)") -- the named layout an editor would see in
    # Premiere's audio settings, not just the raw channel count.
    audio_channel_layout: str = "Stereo"
    audio_format_probed: bool = False
    audio_error: Optional[str] = None
    # A small JPEG-encoded still frame taken from the middle of the
    # clip's current top-scoring segment, so the UI can show a visual
    # preview without re-decoding the video. Re-captured (see
    # _maybe_update_thumbnail) whenever the best segment's midpoint
    # moves -- e.g. because window length or segments-per-clip changed
    # -- and left alone otherwise, so unchanged settings don't trigger
    # a redundant disk read. Local-only: this frame never leaves the
    # machine, isn't embedded in the exported XML, and adds no network
    # or credential surface.
    thumbnail_jpeg: Optional[bytes] = None
    thumbnail_time: Optional[float] = None

    @property
    def label(self):
        return os.path.splitext(self.filename)[0]


def _normalize(value, lo, hi):
    """Clamp+scale a value into 0-100 given an expected [lo, hi] range."""
    if hi <= lo:
        return 50.0
    v = (value - lo) / (hi - lo) * 100.0
    return float(max(0.0, min(100.0, v)))


def _resize_for_analysis(frame):
    h, w = frame.shape[:2]
    scale = ANALYSIS_MAX_DIM / max(h, w)
    if scale < 1.0:
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    return frame


# Sample formats ffprobe reports -> approximate bit depth. Anything not
# listed here (e.g. compressed formats ffprobe can't resolve to a PCM
# width) falls back to the 16-bit default rather than guessing.
_SAMPLE_FMT_BITS = {
    "u8": 8, "u8p": 8,
    "s16": 16, "s16p": 16,
    "s32": 32, "s32p": 32,
    "flt": 32, "fltp": 32,
    "dbl": 64, "dblp": 64,
}

# ffprobe's own channel_layout names (e.g. "mono", "stereo", "5.1",
# "5.1(side)") -> the display preset name editors recognize. Anything
# not listed here is title-cased as-is (covers most ffmpeg layouts).
_LAYOUT_LABELS = {
    "mono": "Mono",
    "stereo": "Stereo",
    "2.1": "2.1",
    "3.0": "3.0",
    "4.0": "Quad",
    "quad": "Quad",
    "5.0": "5.0",
    "5.1": "5.1",
    "5.1(side)": "5.1",
    "6.1": "6.1",
    "7.1": "7.1",
    "7.1(wide)": "7.1",
}

# Fallback if ffprobe doesn't report a channel_layout at all: infer a
# sensible preset name from the raw channel count alone.
_CHANNEL_COUNT_LABELS = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}


def _channel_layout_label(channels: int, ffprobe_layout: Optional[str]) -> str:
    if ffprobe_layout:
        key = ffprobe_layout.strip().lower()
        if key in _LAYOUT_LABELS:
            return _LAYOUT_LABELS[key]
        if key and key != "unknown":
            return ffprobe_layout.strip().title()
    if channels in _CHANNEL_COUNT_LABELS:
        return _CHANNEL_COUNT_LABELS[channels]
    if channels > 0:
        return f"Adaptive ({channels}ch)"
    return "Stereo"


def _probe_audio_format(path: str, result: ClipResult) -> None:
    """Read the source file's *actual* audio channel count, sample rate,
    bit depth, and named channel-layout preset (mono/stereo/5.1/etc.) via
    ffprobe (part of the local ffmpeg install; no network access, just
    metadata inspection of the file already on disk) and record it on
    `result`. If ffprobe isn't installed, times out, or the file has no
    audio stream, we leave the conservative stereo/16-bit/48kHz defaults
    in place and note why via `result.audio_error` -- analysis and
    export still proceed normally, they just can't tailor the exported
    track layout/preset to this file."""
    try:
        info = probe_json(path, timeout=15, select_streams="a:0",
                           show_entries="stream=channels,sample_rate,sample_fmt,channel_layout")
        streams = info.get("streams") or []
        if not streams:
            # Genuinely no audio stream in the source (as opposed to
            # ffprobe being unavailable, handled below) -- clear the
            # stereo/48kHz defaults so xml_export.py knows not to
            # fabricate audio clipitems/media for a file that has none.
            result.audio_channels = 0
            result.audio_channel_layout = "None"
            result.audio_error = "No audio stream found in file"
            return
        stream = streams[0]
        channels = int(stream.get("channels") or 0)
        samplerate = int(stream.get("sample_rate") or 0)
        sample_fmt = stream.get("sample_fmt")
        ffprobe_layout = stream.get("channel_layout")
        if channels > 0:
            result.audio_channels = channels
        if samplerate > 0:
            result.audio_samplerate = samplerate
        if sample_fmt in _SAMPLE_FMT_BITS:
            result.audio_bit_depth = _SAMPLE_FMT_BITS[sample_fmt]
        result.audio_channel_layout = _channel_layout_label(
            result.audio_channels, ffprobe_layout)
        result.audio_format_probed = True
    except FileNotFoundError:
        result.audio_error = ("ffprobe not found -- assuming stereo "
                               "16-bit/48kHz for export (install ffmpeg "
                               "for accurate audio metadata)")
    except (RuntimeError, ValueError) as e:
        result.audio_error = f"Could not probe audio format: {e}"


def _sharpness_score(gray):
    # Variance of Laplacian: higher = more in-focus detail
    lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    # Typical handheld/consumer footage: ~0 (blurry) to ~2000+ (very sharp)
    return _normalize(lap_var, 5, 1200)


def _exposure_score(gray):
    mean_brightness = float(np.mean(gray))
    # Ideal midtone brightness around 110-150 (0-255 scale)
    ideal_lo, ideal_hi = 90, 170
    if ideal_lo <= mean_brightness <= ideal_hi:
        brightness_score = 100.0
    elif mean_brightness < ideal_lo:
        brightness_score = _normalize(mean_brightness, 0, ideal_lo)
    else:
        brightness_score = _normalize(255 - mean_brightness, 0, 255 - ideal_hi)

    # Clipping penalty: % of pixels pure black or pure white
    total = gray.size
    clipped = float(np.sum(gray <= 3) + np.sum(gray >= 252)) / total
    clip_penalty = _normalize(clipped, 0.0, 0.15)  # 0% clipped -> 0 penalty scaled, 15%+ -> full
    clip_score = 100.0 - clip_penalty

    return (brightness_score * 0.7) + (clip_score * 0.3)


def analyze_clip(path: str, progress_cb: Optional[Callable[[float], None]] = None,
                  window_sec: float = 4.0, max_segments: int = 1,
                  min_segment_gap_sec: float = 1.0,
                  enable_energy: bool = False,
                  energy_weight: float = 0.35) -> ClipResult:
    filename = os.path.basename(path)
    cap = _open_video_capture(path)
    if not cap.isOpened():
        return ClipResult(path=path, filename=filename, duration=0, fps=0,
                           width=0, height=0, error="Could not open file "
                           "(unsupported or corrupt codec/container)")

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        if fps <= 0:
            # OpenCV's own metadata read failed to resolve a frame rate --
            # try ffprobe before giving up on the file entirely (see
            # _probe_fps_fallback's docstring for why this sometimes
            # succeeds where OpenCV's lightweight metadata query doesn't).
            probed = _probe_fps_fallback(path)
            if probed:
                fps = probed
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        # Container-reported frame_count is unreliable for several formats
        # this app targets (MTS/M2TS camera-native footage, MXF, some
        # transport streams) -- it's frequently 0, wildly wrong, or a
        # bitrate-based estimate rather than an exact count. This is only a
        # starting estimate for `duration`; it gets corrected below to the
        # actual number of frames decoded, once decoding finishes.
        duration = (frame_count / fps) if fps else 0.0

        result = ClipResult(path=path, filename=filename, duration=duration,
                             fps=fps, width=width, height=height)

        # Read the file's actual audio channel/samplerate/bit-depth so the
        # exported sequence can preserve its real format instead of assuming
        # generic stereo. This is metadata-only (no decoding of audio
        # samples) and never touches the network.
        _probe_audio_format(path, result)

        if fps <= 0:
            result.error = "Could not determine frame rate (unsupported or corrupt codec/container)"
            return result

        # Optional local vision model ("high energy / exciting shot" scoring).
        # Fully local -- no Anthropic API or any other cloud API is involved.
        # If dependencies aren't installed, we degrade gracefully: technical
        # scoring proceeds as normal and result.energy_error explains why.
        energy_scorer = None
        energy_batch_size = 32
        pil_image_cls = None
        if enable_energy:
            try:
                import vision_energy
                from PIL import Image as _PILImage  # ships with the optional torch/open_clip block
                if not vision_energy.is_available():
                    raise vision_energy.VisionEnergyError(vision_energy.availability_message())
                # Batched API: sampled frames are accumulated below and
                # scored energy_batch_size at a time -- one model forward
                # pass per chunk instead of one per frame, which is where
                # nearly all of the old per-frame call overhead went (see
                # vision_energy.score_frames_energy).
                energy_scorer = vision_energy.score_frames_energy
                energy_batch_size = getattr(vision_energy, "BATCH_SIZE", energy_batch_size)
                pil_image_cls = _PILImage
                result.energy_enabled = True
            except Exception as e:
                result.energy_error = str(e)
                enable_energy = False

        sample_step_frames = max(1, int(round(SAMPLE_INTERVAL_SEC * fps)))
        prev_gray = None
        prev_flow_vec = None  # mean (dx, dy) of previous step, for jitter calc

        frame_idx = 0
        samples: List[FrameSample] = []

        # Frames waiting for batched energy scoring: the (already
        # downscaled) sampled frame as a PIL image, plus the index of the
        # FrameSample its score belongs to. Samples are appended with
        # energy=0.0 as a placeholder and the real score is written back
        # when the batch flushes -- every energy_batch_size frames, so a
        # long clip never holds more than one batch of frames in memory.
        energy_pending_images: List = []
        energy_pending_indices: List[int] = []

        def _flush_energy_batch():
            """Score everything accumulated so far and write each score
            onto its FrameSample. Same failure semantics as the old
            per-frame call: a failure mid-clip shouldn't kill the whole
            clip -- note the error once, stop energy scoring for the rest
            of this clip, and leave the affected samples at energy=0.0
            (the value un-scored samples always had)."""
            nonlocal energy_scorer
            if not energy_pending_images:
                return
            try:
                scores = energy_scorer(energy_pending_images)
                for sample_idx, score in zip(energy_pending_indices, scores):
                    samples[sample_idx].energy = score
            except Exception as e:
                if result.energy_error is None:
                    result.energy_error = f"Energy scoring failed mid-clip: {e}"
                energy_scorer = None
                result.energy_enabled = False
            energy_pending_images.clear()
            energy_pending_indices.clear()

        while True:
            ok = cap.grab()
            if not ok:
                break
            if frame_idx % sample_step_frames == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                frame = _resize_for_analysis(frame)
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                sharp = _sharpness_score(gray)
                expo = _exposure_score(gray)

                motion_mag = 0.0
                jitter = 0.0
                if prev_gray is not None and prev_gray.shape == gray.shape:
                    flow = cv2.calcOpticalFlowFarneback(
                        prev_gray, gray, None, 0.5, 2, 15, 2, 5, 1.2, 0)
                    fx, fy = flow[..., 0], flow[..., 1]
                    mag = np.sqrt(fx ** 2 + fy ** 2)
                    motion_mag = float(np.mean(mag))
                    mean_vec = (float(np.mean(fx)), float(np.mean(fy)))
                    if prev_flow_vec is not None:
                        jitter = float(np.hypot(mean_vec[0] - prev_flow_vec[0],
                                                 mean_vec[1] - prev_flow_vec[1]))
                    prev_flow_vec = mean_vec

                if energy_scorer is not None:
                    # Queue this sample's frame for the next batched
                    # scoring pass instead of paying a model call per
                    # frame; the score lands on the same sample index the
                    # old per-frame call wrote to, so downstream scoring
                    # and segment selection see identical data. The
                    # BGR->PIL conversion sat inside the old per-frame
                    # try as well, so a failure here degrades to
                    # technical-only scoring the same way a scoring
                    # failure does, rather than killing the clip.
                    try:
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        energy_pending_images.append(pil_image_cls.fromarray(rgb))
                        energy_pending_indices.append(len(samples))
                    except Exception as e:
                        if result.energy_error is None:
                            result.energy_error = f"Energy scoring failed mid-clip: {e}"
                        energy_scorer = None
                        result.energy_enabled = False
                        energy_pending_images.clear()
                        energy_pending_indices.clear()

                samples.append(FrameSample(
                    time_sec=frame_idx / fps,
                    sharpness=sharp,
                    exposure=expo,
                    motion_mag=motion_mag,
                    motion_jitter=jitter,
                    energy=0.0,  # placeholder; batched flush fills this in
                ))
                prev_gray = gray

                if len(energy_pending_images) >= energy_batch_size:
                    _flush_energy_batch()

                if progress_cb and duration > 0:
                    progress_cb(min(1.0, (frame_idx / fps) / duration))
            frame_idx += 1

        # Score the tail-end frames still queued for energy scoring
        # (clips whose sample count isn't a multiple of the batch size)
        # before anything below reads samples' energy values.
        _flush_energy_batch()
    finally:
        cap.release()

    if not samples:
        result.error = "No frames could be sampled"
        return result

    # Container-reported duration (used only as a starting estimate,
    # and only for progress-bar percentage above) is frequently wrong
    # for exactly the professional/camera-native formats this app
    # targets. Now that decoding has actually reached end-of-stream,
    # frame_idx is the real, verified frame count -- use it instead,
    # so segment timing and the exported XML's durations/timecodes
    # reflect the file's actual length rather than its metadata.
    result.duration = frame_idx / fps

    if result.energy_enabled:
        result.mean_energy_score = float(np.mean([s.energy for s in samples]))

    result.samples = samples
    effective_energy_weight = energy_weight if result.energy_enabled else 0.0
    _score_clip(result, window_sec=window_sec, max_segments=max_segments,
                min_segment_gap_sec=min_segment_gap_sec,
                energy_weight=effective_energy_weight)
    # A fresh analysis has already paid the cost of fully decoding this
    # file, so one extra seek+read for a thumbnail is negligible here --
    # unlike a settings-only rescore of many cached clips at once (see
    # refresh_thumbnail's docstring for why that path stays lazy).
    refresh_thumbnail(result)
    return result


def _composite(sample, energy_weight: float = 0.0) -> float:
    stability = sample.__dict__.get("stability_score", 50.0)
    technical = (sample.sharpness * WEIGHT_SHARPNESS +
                 sample.exposure * WEIGHT_EXPOSURE +
                 stability * WEIGHT_STABILITY)
    if energy_weight <= 0.0:
        return technical
    energy = getattr(sample, "energy", 0.0)
    return technical * (1.0 - energy_weight) + energy * energy_weight


THUMBNAIL_MAX_DIM = 320   # generous enough to downscale for either the
                          # row icon or the larger preview panel; stored
                          # once, resized on display as needed.
THUMBNAIL_JPEG_QUALITY = 80


def _capture_thumbnail(path: str, time_sec: float) -> Optional[bytes]:
    """Seek to `time_sec` in the source file and grab a single frame as
    a small JPEG (in memory only -- never written to disk except inside
    the existing local cache file, and never sent anywhere). Failure
    just means no thumbnail -- never fatal to analysis/export."""
    cap = _open_video_capture(path)
    try:
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, time_sec) * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        h, w = frame.shape[:2]
        scale = THUMBNAIL_MAX_DIM / max(h, w)
        if scale < 1.0:
            frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))))
        ok, buf = cv2.imencode(".jpg", frame,
                                [cv2.IMWRITE_JPEG_QUALITY, THUMBNAIL_JPEG_QUALITY])
        if not ok:
            return None
        return buf.tobytes()
    except Exception:
        return None
    finally:
        cap.release()


PREVIEW_MAX_DIM = 640  # cap on the long edge of segment-preview playback frames


def open_segment_capture(path: str, start_sec: float):
    """Open `path` for segment-preview playback and seek to `start_sec`.
    Returns (cap, fps) on success, or (None, 0.0) if the file can't be
    opened -- callers treat that the same as any other "no preview
    available" case. On success, the caller owns `cap` and must call
    `cap.release()` when done (see the segment-preview window in
    app.py). Local-only: this only ever reads the already-selected
    source file on disk, exactly like thumbnail capture, and touches
    nothing else."""
    cap = _open_video_capture(path)
    if not cap.isOpened():
        cap.release()
        return None, 0.0
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if fps <= 0:
        probed = _probe_fps_fallback(path)
        fps = probed if probed else 24.0  # sane default so playback speed is never zero/undefined
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, start_sec) * 1000.0)
    return cap, fps


def read_next_segment_frame(cap, end_sec: float, max_dim: int = PREVIEW_MAX_DIM) -> Optional[bytes]:
    """Read the next frame from a capture already positioned by
    `open_segment_capture` and return it as PPM bytes ready for
    `tk.PhotoImage(data=...)`, or None once playback has reached
    `end_sec` (or the end of the file). Same in-memory decode/resize/
    re-encode approach as the existing thumbnail path -- nothing is
    written to disk and nothing leaves the machine."""
    pos_sec = (cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000.0
    if end_sec > 0 and pos_sec > end_sec:
        return None
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    h, w = frame.shape[:2]
    scale = max_dim / max(h, w)
    if scale < 1.0:
        frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))))
    ok, buf = cv2.imencode(".ppm", frame)
    if not ok:
        return None
    return buf.tobytes()


def thumbnail_is_stale(result: ClipResult, tolerance_sec: float = 0.5) -> bool:
    """True if the clip's stored thumbnail no longer matches its current
    best segment (e.g. window length/segments-per-clip/energy weight
    changed since it was captured), or if there's no thumbnail yet.
    Cheap and does no file I/O -- just a comparison of two numbers."""
    if result.duration <= 0:
        return False
    if result.thumbnail_jpeg is None or result.thumbnail_time is None:
        return True
    mid = max(0.0, min(result.duration, (result.best_window_start + result.best_window_end) / 2.0))
    return abs(mid - result.thumbnail_time) > tolerance_sec


def refresh_thumbnail(result: ClipResult) -> bool:
    """Explicitly (re)capture result.thumbnail_jpeg to match its current
    best segment. Always seeks the file -- callers decide *when* that
    cost is worth paying (see thumbnail_is_stale for a free pre-check).
    Returns True if the thumbnail was (re)captured, False if the file
    couldn't be read (thumbnail_jpeg is left as whatever it was).

    Deliberately NOT called automatically during scoring/rescoring: a
    settings change (window length, segments-per-clip, energy weight)
    can shift the best-segment midpoint for every cached clip in a
    folder at once, and eagerly reseeking every one of those files here
    would silently reintroduce the exact per-clip disk cost the result
    cache exists to avoid -- turning an "instant" settings tweak into a
    batch of file opens proportional to library size. Instead, this is
    called only for a clip actually being decoded fresh (analyze_clip)
    or a clip a person is actively looking at (the UI's preview panel),
    so the cost scales with attention, not with library size."""
    if result.duration <= 0:
        return False
    mid = max(0.0, min(result.duration, (result.best_window_start + result.best_window_end) / 2.0))
    thumb = _capture_thumbnail(result.path, mid)
    if thumb is None:
        return False
    result.thumbnail_jpeg = thumb
    result.thumbnail_time = mid
    return True


def _score_clip(result: ClipResult, window_sec: float = 4.0,
                 max_segments: int = 1, min_segment_gap_sec: float = 1.0,
                 energy_weight: float = 0.0):
    samples = result.samples
    if not samples:
        result.overall_score = 0.0
        return

    # Stability score per sample: low jitter is good. Use the global
    # jitter distribution to normalize (most footage is mildly shaky at
    # times). Recomputed here -- rather than once at decode time -- so
    # it's correctly derived from motion_jitter regardless of whether
    # `samples` just came off the decoder (analyze_clip) or were
    # restored from the on-disk cache (rescore_clip via
    # result_cache.result_from_entry), which reconstructs plain
    # FrameSample objects that have never had this dynamic attribute
    # attached.
    jitters = np.array([s.motion_jitter for s in samples])
    jitter_ceiling = max(0.5, float(np.percentile(jitters, 90)) * 1.5) if len(jitters) else 1.0
    for s in samples:
        s.__dict__["stability_score"] = 100.0 - _normalize(s.motion_jitter, 0, jitter_ceiling)

    scores = [_composite(s, energy_weight) for s in samples]
    result.overall_score = float(np.mean(scores))

    times = [s.time_sec for s in samples]
    n = len(samples)

    if n == 1 or result.duration <= window_sec:
        seg = Segment(start=0.0, end=result.duration, score=result.overall_score)
        result.segments = [seg]
        result.best_window_start = seg.start
        result.best_window_end = seg.end
        result.best_window_score = seg.score
        return

    # Build every candidate window's average score using a two-pointer
    # sweep (same approach as the original single-best-window logic, but
    # this time we keep every (i, j, avg) triple instead of just the max).
    candidates = []  # (avg_score, start_idx, end_idx_inclusive)
    j = 0
    window_sum = 0.0
    count = 0
    for i in range(n):
        if j < i:
            j = i
            window_sum = 0.0
            count = 0
        while j < n and times[j] - times[i] <= window_sec:
            window_sum += scores[j]
            count += 1
            j += 1
        if count > 0:
            avg = window_sum / count
            candidates.append((avg, i, j - 1))
        window_sum -= scores[i]
        count -= 1

    # Greedy non-max suppression: take the best-scoring candidate, then
    # repeatedly take the next-best one that doesn't overlap (with a
    # buffer of min_segment_gap_sec) any segment already chosen.
    candidates.sort(key=lambda c: c[0], reverse=True)
    chosen: List[tuple] = []  # (avg, start_idx, end_idx)
    max_segments = max(1, int(max_segments))

    def overlaps(a, b):
        a_start = times[a[1]] - min_segment_gap_sec
        a_end = times[a[2]] + SAMPLE_INTERVAL_SEC + min_segment_gap_sec
        b_start = times[b[1]]
        b_end = times[b[2]] + SAMPLE_INTERVAL_SEC
        return not (b_end <= a_start or b_start >= a_end)

    for cand in candidates:
        if len(chosen) >= max_segments:
            break
        # Always keep the single best segment, even on a uniformly
        # mediocre clip. Beyond that, only add segments that are
        # genuinely better than the clip's own average -- otherwise
        # `max_segments` would pad weak clips with filler footage just
        # to hit the requested count.
        if chosen and cand[0] <= result.overall_score:
            continue
        if any(overlaps(c, cand) for c in chosen):
            continue
        chosen.append(cand)

    if not chosen:
        chosen = [candidates[0]] if candidates else [(result.overall_score, 0, n - 1)]

    # Order selected segments chronologically for export, but track the
    # single highest-scoring one separately for best_window_* fields.
    chosen_sorted_by_score = sorted(chosen, key=lambda c: c[0], reverse=True)
    top = chosen_sorted_by_score[0]
    result.best_window_start = max(0.0, times[top[1]])
    result.best_window_end = min(result.duration, times[top[2]] + SAMPLE_INTERVAL_SEC)
    result.best_window_score = top[0]

    chosen.sort(key=lambda c: c[1])  # chronological order
    result.segments = [
        Segment(
            start=max(0.0, times[i]),
            end=min(result.duration, times[j] + SAMPLE_INTERVAL_SEC),
            score=avg,
        )
        for avg, i, j in chosen
    ]


def rescore_clip(result: ClipResult, window_sec: float = 4.0, max_segments: int = 1,
                  min_segment_gap_sec: float = 1.0, energy_weight: float = 0.35,
                  enable_energy: bool = True) -> ClipResult:
    """Recompute overall_score, best segment(s), and best_window_* from
    a ClipResult's already-collected per-frame samples, without
    re-decoding or re-sampling the source video.

    Used by result_cache.py: window length, segments-per-clip, and
    energy weight only affect how per-frame samples are *combined*
    into a score, not the samples themselves -- so a cached clip can be
    re-scored for new settings almost instantly instead of re-running
    the (comparatively expensive) frame decode + optical flow + CLIP
    scoring pass.

    `result.energy_enabled` and `enable_energy` answer two different
    questions and must both be true for energy to affect the score:
      - result.energy_enabled: do the cached *samples* actually contain
        real per-frame CLIP energy values (data availability -- set
        once, when the samples were collected, and left untouched
        here so a re-save of the cache doesn't lose that information).
      - enable_energy: does the *current* run want energy factored in
        at all (e.g. the "Detect high-energy shots" checkbox).
    A clip cached with energy data must stop influencing the score the
    moment the checkbox is unchecked -- without this, energy weighting
    would silently keep applying to any clip served from cache,
    regardless of the current setting, since the cached sample data
    doesn't know the checkbox was ever toggled.

    Deliberately does NOT touch result.thumbnail_jpeg. Settings changes
    here can move the best segment for every cached clip in a folder at
    once, and refreshing every clip's thumbnail on every rescore would
    reseek every source file just to redraw icons nobody may ever look
    at. See analyzer.refresh_thumbnail / thumbnail_is_stale -- callers
    (the UI) refresh a clip's thumbnail on demand instead, e.g. only
    when that clip is actually selected for preview."""
    apply_energy = enable_energy and result.energy_enabled
    effective_energy_weight = energy_weight if apply_energy else 0.0
    _score_clip(result, window_sec=window_sec, max_segments=max_segments,
                min_segment_gap_sec=min_segment_gap_sec,
                energy_weight=effective_energy_weight)
    if result.energy_enabled and result.samples:
        result.mean_energy_score = float(np.mean([s.energy for s in result.samples]))
    return result


def thumbnail_ppm_bytes(jpeg_bytes: bytes, max_dim: int = 200) -> Optional[bytes]:
    """Decode a stored thumbnail JPEG and re-encode it as PPM, scaled to
    fit within max_dim on its longest side. PPM is a plain, uncompressed
    raster format that Tk's built-in PhotoImage can load directly (via
    `tk.PhotoImage(data=...)`) with no extra imaging library (e.g. PIL)
    required -- keeps the thumbnail feature dependency-free. Returns
    None on any decode failure (corrupt/missing bytes), which callers
    treat as "no preview available", never as an error."""
    if not jpeg_bytes:
        return None
    try:
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        scale = max_dim / max(h, w)
        if scale < 1.0:
            img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))))
        ok, buf = cv2.imencode(".ppm", img)
        if not ok:
            return None
        return buf.tobytes()
    except Exception:
        return None


def find_video_files(folder: str) -> List[str]:
    files = []
    for root, _, names in os.walk(folder):
        for name in names:
            if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
                files.append(os.path.join(root, name))
    return sorted(files)
