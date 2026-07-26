"""
sync_core.py
------------
Engine for the local Video/Audio Sync Tool.

Everything here runs locally via ffmpeg/ffprobe (must be installed and on PATH)
plus numpy/scipy for the waveform cross-correlation. No network calls are made
anywhere in this module -- all processing happens on files already on disk.

Public API used by sync_app.py:

    probe(path)                                -> dict (raw ffprobe json)
    ProbeInfo.from_probe(probe_dict)            -> convenience wrapper
    extract_mono_pcm(path, samplerate, stream)  -> np.ndarray (float32, mono)
    waveform_offset(ref, target, samplerate)    -> float seconds
    read_bwf_timeref(wav_path)                  -> (samples_since_midnight, samplerate) or None
    video_timecode_seconds(path)                -> float seconds-since-midnight or None
    compute_offset(video_path, audio_path, method) -> float seconds
    AudioTrackSpec                              -> dataclass describing one external audio input
    build_export_command(...)                   -> list[str] ffmpeg argv
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from scipy.signal import correlate

from rcs_utils.ffprobe_util import probe_json


# --------------------------------------------------------------------------
# Basic tool checks
# --------------------------------------------------------------------------

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd: List[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Timed out after {timeout}s running: {' '.join(cmd)}")


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def probe(path: str) -> dict:
    """Run ffprobe and return the parsed JSON description of a media file."""
    return probe_json(path, timeout=15, show_format=True, show_streams=True)


@dataclass
class ProbeInfo:
    path: str
    duration: float
    has_video: bool
    has_audio: bool
    video_fps: Optional[float] = None
    audio_samplerate: Optional[int] = None
    audio_channels: Optional[int] = None
    audio_sample_fmt: Optional[str] = None
    audio_bits_per_sample: Optional[int] = None
    audio_codec_name: Optional[str] = None
    timecode_tag: Optional[str] = None  # e.g. "01:00:00:00" from a tmcd stream or format tag

    @classmethod
    def from_probe(cls, data: dict, path: str) -> "ProbeInfo":
        fmt = data.get("format", {})
        duration = float(fmt.get("duration", 0.0) or 0.0)
        has_video = False
        has_audio = False
        video_fps = None
        audio_sr = None
        audio_ch = None
        audio_fmt = None
        audio_bits = None
        audio_codec = None
        timecode_tag = fmt.get("tags", {}).get("timecode")

        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and not has_video:
                has_video = True
                rate = s.get("avg_frame_rate") or s.get("r_frame_rate")
                if rate and rate != "0/0":
                    num, _, den = rate.partition("/")
                    try:
                        video_fps = float(num) / float(den) if den else float(num)
                    except (ValueError, ZeroDivisionError):
                        video_fps = None
            elif s.get("codec_type") == "audio" and not has_audio:
                has_audio = True
                audio_sr = int(s.get("sample_rate", 0)) or None
                audio_ch = s.get("channels")
                audio_fmt = s.get("sample_fmt")
                audio_codec = s.get("codec_name")
                # bits_per_raw_sample reflects the *source* container's bit depth (e.g. 24
                # for pcm_s24le, which otherwise decodes to the same sample_fmt as 32-bit
                # int); bits_per_sample is the fallback for formats that don't set it.
                raw_bits = s.get("bits_per_raw_sample")
                bits = s.get("bits_per_sample")
                for candidate in (raw_bits, bits):
                    if candidate not in (None, "N/A", 0, "0"):
                        try:
                            audio_bits = int(candidate)
                            break
                        except (TypeError, ValueError):
                            pass
            elif s.get("codec_type") == "data" and s.get("codec_tag_string", "").lower() == "tmcd":
                timecode_tag = timecode_tag or s.get("tags", {}).get("timecode")

        return cls(
            path=path, duration=duration, has_video=has_video, has_audio=has_audio,
            video_fps=video_fps, audio_samplerate=audio_sr, audio_channels=audio_ch,
            audio_sample_fmt=audio_fmt, audio_bits_per_sample=audio_bits,
            audio_codec_name=audio_codec, timecode_tag=timecode_tag,
        )

    @property
    def audio_format_label(self) -> str:
        """
        Human-readable bit-depth/format description, e.g. "24-bit integer",
        "32-bit float". This tool normalizes all audio processing and export
        to 32-bit float -- its maximum supported precision -- regardless of
        source depth, so anything above that (64-bit float) is reported as
        such but will be downsampled to 32-bit float during decode/export.
        """
        fmt = (self.audio_sample_fmt or "").rstrip("p")  # strip planar suffix
        bits = self.audio_bits_per_sample
        if fmt in ("flt",):
            return "32-bit float"
        if fmt in ("dbl",):
            return "64-bit float"
        if bits:
            return f"{bits}-bit integer"
        # Fallback purely from sample_fmt if bits_per_sample was unavailable.
        fallback_bits = {"u8": 8, "s16": 16, "s32": 32, "s64": 64}.get(fmt)
        return f"{fallback_bits}-bit integer" if fallback_bits else "unknown format"

    @property
    def exceeds_32bit_float(self) -> bool:
        """True if the source exceeds this tool's 32-bit float processing ceiling."""
        fmt = (self.audio_sample_fmt or "").rstrip("p")
        if fmt == "dbl":
            return True
        return bool(self.audio_bits_per_sample and self.audio_bits_per_sample > 32)


def probe_info(path: str) -> ProbeInfo:
    return ProbeInfo.from_probe(probe(path), path)


# --------------------------------------------------------------------------
# Audio extraction for waveform comparison
# --------------------------------------------------------------------------

def extract_mono_pcm(path: str, samplerate: int = 8000, max_seconds: Optional[float] = None,
                      stream_selector: str = "a:0") -> np.ndarray:
    """
    Decode the given media file's audio (any container: video or wav) down to a
    mono float32 PCM numpy array at `samplerate` Hz, entirely via ffmpeg piped
    to stdout. Used only for cross-correlation, not for the final export.
    """
    data = decode_audio_array(path, samplerate=samplerate, channels=1,
                              max_seconds=max_seconds, stream_selector=stream_selector)
    return data[:, 0] if data.ndim == 2 else data


def decode_audio_array(path: str, samplerate: int = 44100, channels: int = 2,
                        max_seconds: Optional[float] = None,
                        stream_selector: str = "a:0") -> np.ndarray:
    """
    Decode a media file's audio to a float32 numpy array of shape
    (num_samples, channels), at the requested sample rate. Used both for the
    waveform correlation (low-rate mono) and for full-quality preview
    playback (higher rate, stereo/mono as requested).
    """
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(path),
        "-map", f"0:{stream_selector}",
        "-ac", str(channels),
        "-ar", str(samplerate),
        "-f", "f32le",
    ]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["pipe:1"]

    # Full-file decodes (max_seconds=None) have no -t bound, so give this a
    # generous ceiling rather than none at all -- a hung/corrupt input should
    # fail loudly instead of hanging the app indefinitely.
    proc = _run(cmd, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"Could not extract audio from {path}: {proc.stderr.decode(errors='replace')}")
    flat = np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32)
    if flat.size % channels != 0:
        flat = flat[: flat.size - (flat.size % channels)]
    return flat.reshape(-1, channels)


# --------------------------------------------------------------------------
# Waveform (cross-correlation) sync
# --------------------------------------------------------------------------

def waveform_offset(ref: np.ndarray, target: np.ndarray, samplerate: int) -> float:
    """
    Return the number of seconds `target` must be DELAYED to line up with `ref`.
    A negative value means `target` starts before `ref` and must be trimmed
    (or the negative delay applied) instead.

    Uses FFT-based cross-correlation (fast, O(n log n)) rather than a naive
    O(n^2) loop, so it stays practical on clips that are many minutes long.
    """
    if ref.size == 0 or target.size == 0:
        raise ValueError("Empty audio buffer supplied to waveform_offset")

    # Normalize to reduce sensitivity to differing recorder gain levels.
    ref_n = (ref - ref.mean()) / (ref.std() + 1e-9)
    tgt_n = (target - target.mean()) / (target.std() + 1e-9)

    corr = correlate(ref_n, tgt_n, mode="full", method="fft")
    lag_index = int(np.argmax(corr))
    # For correlate(ref, target, 'full'), the lag axis runs from
    # -(len(target)-1) to (len(ref)-1). lag > 0 means target should be
    # shifted forward (delayed) to match ref.
    lag = lag_index - (len(tgt_n) - 1)
    return lag / float(samplerate)


def compute_waveform_offset(video_path: str, audio_path: str,
                             corr_samplerate: int = 8000,
                             max_seconds: Optional[float] = 600.0) -> float:
    """High-level helper: extract low-res audio from both files and correlate."""
    ref = extract_mono_pcm(video_path, corr_samplerate, max_seconds, stream_selector="a:0")
    tgt = extract_mono_pcm(audio_path, corr_samplerate, max_seconds, stream_selector="a:0")
    return waveform_offset(ref, tgt, corr_samplerate)


# --------------------------------------------------------------------------
# Timecode sync (BWF bext TimeReference + video tmcd / format timecode tag)
# --------------------------------------------------------------------------

def read_bwf_timeref(wav_path: str) -> Optional[Tuple[int, int]]:
    """
    Manually parse the RIFF/WAV chunk list looking for a 'bext' (Broadcast
    Wave Format) chunk, and return (time_reference_samples, sample_rate).
    TimeReference is the number of audio samples since local midnight, which
    is how field recorders (Sound Devices, Zoom, Tascam, etc.) embed timecode.
    Returns None if the file has no bext chunk (i.e. it's a plain WAV).
    """
    path = Path(wav_path)
    with open(path, "rb") as f:
        riff = f.read(12)
        if len(riff) < 12 or riff[0:4] != b"RIFF" or riff[8:12] != b"WAVE":
            return None

        sample_rate = None
        time_ref = None

        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            chunk_id = header[0:4]
            chunk_size = struct.unpack("<I", header[4:8])[0]
            data = f.read(chunk_size)
            if chunk_size % 2 == 1:  # chunks are word-aligned
                f.read(1)

            if chunk_id == b"fmt " and len(data) >= 16:
                sample_rate = struct.unpack("<I", data[4:8])[0]
            elif chunk_id == b"bext" and len(data) >= 346:
                # Per EBU Tech 3285, the bext chunk lays out Description[256] +
                # Originator[32] + OriginatorReference[32] + OriginationDate[10] +
                # OriginationTime[8] = 338 bytes before the 8-byte (low32, high32)
                # TimeReference field.
                low = struct.unpack("<I", data[338:342])[0]
                high = struct.unpack("<I", data[342:346])[0]
                time_ref = (high << 32) | low

        if time_ref is None or sample_rate is None:
            return None
        return time_ref, sample_rate


def bwf_timecode_seconds(wav_path: str) -> Optional[float]:
    result = read_bwf_timeref(wav_path)
    if result is None:
        return None
    samples, sr = result
    return samples / float(sr)


def video_timecode_seconds(path: str) -> Optional[float]:
    """
    Read an embedded start timecode (e.g. from a camera's tmcd track or
    container format tag) and convert HH:MM:SS:FF to seconds-since-midnight
    using the file's own frame rate.
    """
    info = probe_info(path)
    if not info.timecode_tag:
        return None
    parts = info.timecode_tag.replace(";", ":").split(":")
    if len(parts) != 4:
        return None
    hh, mm, ss, ff = (int(p) for p in parts)
    fps = info.video_fps or 30.0
    return hh * 3600 + mm * 60 + ss + (ff / fps)


def compute_timecode_offset(video_path: str, audio_path: str) -> float:
    """
    Return the number of seconds the audio file must be DELAYED to align with
    the video, based purely on embedded timecode (no waveform analysis).
    """
    video_tc = video_timecode_seconds(video_path)
    audio_tc = bwf_timecode_seconds(audio_path)
    if video_tc is None:
        raise ValueError(f"No embedded timecode found on video: {video_path}")
    if audio_tc is None:
        raise ValueError(f"No BWF timecode (bext chunk) found on audio: {audio_path}")
    return audio_tc - video_tc


def compute_offset(video_path: str, audio_path: str, method: str = "waveform") -> float:
    if method == "timecode":
        return compute_timecode_offset(video_path, audio_path)
    elif method == "waveform":
        return compute_waveform_offset(video_path, audio_path)
    else:
        raise ValueError(f"Unknown sync method: {method}")


# --------------------------------------------------------------------------
# Export command building
# --------------------------------------------------------------------------

VIDEO_CODEC_PRESETS = {
    # key -> (ffmpeg args, recommended container extension, description)
    "v210": (["-c:v", "v210"], ".mov",
             "10-bit uncompressed 4:2:2 (industry-standard 'uncompressed', QuickTime-compatible)"),
    "ffv1": (["-c:v", "ffv1", "-level", "3", "-g", "1"], ".mkv",
             "Mathematically lossless (much smaller than true uncompressed, bit-exact)"),
    "rawvideo": (["-c:v", "rawvideo"], ".avi",
                 "True uncompressed raw frames (largest possible file size)"),
    "copy": (["-c:v", "copy"], None,
             "No video re-encoding -- the original video stream is copied through "
             "unchanged (fastest export, no generation loss; only the audio is "
             "processed). Keep the output in the same container as the source."),
}


@dataclass
class AudioTrackSpec:
    path: str
    offset_seconds: float = 0.0   # positive = delay, negative = trim from start
    label: str = ""
    track: int = 1   # output track/stream number; specs sharing a number are
                     # mixed together into one output audio stream, distinct
                     # numbers become distinct output audio streams


def _delay_or_trim_filter(offset_seconds: float, channels: int) -> str:
    """Build the adelay/atrim portion of a filter chain for one input's offset."""
    if offset_seconds >= 0:
        ms = int(round(offset_seconds * 1000))
        delays = "|".join([str(ms)] * max(channels, 1))
        return f"adelay={delays}:all=1"
    else:
        trim = abs(offset_seconds)
        return f"atrim=start={trim:.6f},asetpts=PTS-STARTPTS"


def group_audio_tracks_by_output(
    audio_tracks: List[AudioTrackSpec],
) -> "list[tuple[int, list[AudioTrackSpec]]]":
    """
    Group audio track specs by their `track` (output stream) number, returning
    groups in ascending track-number order with each group's members in their
    original relative order. Shared by build_export_command (to decide what
    gets mixed together) and the export log (to describe the grouping to the
    user) so the two can never disagree about what "Track N" contains.
    """
    groups: "dict[int, list[AudioTrackSpec]]" = {}
    for spec in audio_tracks:
        groups.setdefault(spec.track, []).append(spec)
    return [(num, groups[num]) for num in sorted(groups)]


def build_export_command(
    video_path: str,
    audio_tracks: List[AudioTrackSpec],
    output_path: str,
    keep_camera_audio: bool = False,
    video_codec: str = "v210",
) -> List[str]:
    """
    Build the full ffmpeg command that:
      - syncs each external audio track to the video (adelay/atrim per offset)
      - mixes tracks that share an output `track` number together into one
        output audio stream (via amerge), and keeps differently-numbered
        tracks as separate output audio streams -- so the exported file can
        carry several independently-selectable audio tracks, each optionally
        combining more than one source file
      - optionally appends the camera's original on-board audio as its own
        additional output audio stream, rather than replacing it
      - encodes video per `video_codec` preset -- either uncompressed, or
        (for "copy") stream-copied through with no re-encoding at all
      - encodes all processed audio as 32-bit float PCM (pcm_f32le)
      - copies source metadata (and timecode track, if present)
    """
    if video_codec not in VIDEO_CODEC_PRESETS:
        raise ValueError(f"Unknown video codec preset: {video_codec}")
    vcodec_args, _, _ = VIDEO_CODEC_PRESETS[video_codec]

    cmd = ["ffmpeg", "-y", "-i", str(video_path)]
    for track in audio_tracks:
        cmd += ["-i", str(track.path)]

    # Map each spec to its ffmpeg input index (1-based, video is input 0) by
    # identity rather than value, since two specs can otherwise compare equal.
    input_index_by_id = {id(spec): i for i, spec in enumerate(audio_tracks, start=1)}

    filter_parts = []
    all_audio_labels = []
    for track_number, members in group_audio_tracks_by_output(audio_tracks):
        member_labels = []
        for spec in members:
            i = input_index_by_id[id(spec)]
            info = probe_info(spec.path)
            channels = info.audio_channels or 1
            delay_filter = _delay_or_trim_filter(spec.offset_seconds, channels)
            label = f"a{i}"
            filter_parts.append(f"[{i}:a]{delay_filter}[{label}]")
            member_labels.append(f"[{label}]")

        if len(member_labels) > 1:
            group_label = f"t{track_number}"
            merge_inputs = "".join(member_labels)
            filter_parts.append(
                f"{merge_inputs}amerge=inputs={len(member_labels)}[{group_label}]"
            )
            all_audio_labels.append(f"[{group_label}]")
        else:
            all_audio_labels.append(member_labels[0])

    if keep_camera_audio:
        filter_parts.append("[0:a]anull[cam]")
        all_audio_labels.append("[cam]")

    map_args = ["-map", "0:v"]
    for lbl in all_audio_labels:
        map_args += ["-map", lbl]
    # else (all_audio_labels empty): no audio tracks at all -> video only

    cmd += ["-filter_complex", ";".join(filter_parts)] if filter_parts else []
    cmd += map_args
    cmd += vcodec_args
    if all_audio_labels:
        cmd += ["-c:a", "pcm_f32le"]
    cmd += ["-map_metadata", "0", "-map", "0:d?"]
    cmd += [str(output_path)]
    return cmd
