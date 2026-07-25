"""
media_playback.py
------------------
Local-only playback engine for the preview panels:
  - AudioPlayer: plays one decoded audio buffer with seeking
  - MixPlayer: plays a synced mix of several offset buffers at once
               (used for the "synced preview")
  - VideoFrameSource: pulls individual frames out of a video file by
    timestamp, for driving an on-screen preview

All decoding goes through sync_core.decode_audio_array (ffmpeg), so no new
decoding path is introduced. Playback uses `sounddevice`/PortAudio, which
talks directly to your local sound card -- nothing is sent over a network.
If no audio output device is available (or PortAudio isn't installed),
every method here raises a clear, catchable PlaybackUnavailable so the GUI
can show a friendly message instead of crashing.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

import sync_core as sc

try:
    import sounddevice as sd
    _SOUNDDEVICE_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - depends on host system
    sd = None
    _SOUNDDEVICE_IMPORT_ERROR = exc

try:
    import cv2
    _CV2_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - depends on host system
    cv2 = None
    _CV2_IMPORT_ERROR = exc

try:
    from PIL import Image
    _PIL_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    Image = None
    _PIL_IMPORT_ERROR = exc


class PlaybackUnavailable(RuntimeError):
    """Raised when audio/video playback can't proceed on this system."""


PREVIEW_SAMPLERATE = 44100
PREVIEW_MAX_SECONDS = 180.0  # cap decoded preview length to keep memory bounded


def load_preview_audio(path: str, channels: int = 2) -> np.ndarray:
    """Decode up to PREVIEW_MAX_SECONDS of audio for playback/waveform use."""
    return sc.decode_audio_array(path, samplerate=PREVIEW_SAMPLERATE, channels=channels,
                                 max_seconds=PREVIEW_MAX_SECONDS)


# --------------------------------------------------------------------------
# Single-track player
# --------------------------------------------------------------------------

class AudioPlayer:
    """
    Plays one in-memory float32 buffer (shape [n, channels]) with seeking.
    Uses a callback-driven sounddevice.OutputStream.
    """

    def __init__(self, samples: np.ndarray, samplerate: int = PREVIEW_SAMPLERATE):
        self.samples = samples
        self.samplerate = samplerate
        self._pos = 0  # frame index
        self._stream = None
        self._playing = False
        self.on_stop: Optional[callable] = None

    @property
    def duration(self) -> float:
        return self.samples.shape[0] / float(self.samplerate)

    def seek(self, seconds: float):
        self._pos = int(max(0.0, seconds) * self.samplerate)
        self._pos = min(self._pos, self.samples.shape[0])

    def position_seconds(self) -> float:
        return self._pos / float(self.samplerate)

    def _callback(self, outdata, frames, time_info, status):
        end = min(self._pos + frames, self.samples.shape[0])
        chunk = self.samples[self._pos:end]
        n = chunk.shape[0]
        outdata[:n] = chunk
        if n < frames:
            outdata[n:] = 0
            self._pos = self.samples.shape[0]
            raise sd.CallbackStop()
        self._pos = end

    def play(self):
        if sd is None:
            raise PlaybackUnavailable(f"Audio playback library not available: {_SOUNDDEVICE_IMPORT_ERROR}")
        if self._pos >= self.samples.shape[0]:
            self._pos = 0
        channels = self.samples.shape[1] if self.samples.ndim > 1 else 1

        def finished():
            self._playing = False
            if self.on_stop:
                self.on_stop()

        try:
            self._stream = sd.OutputStream(
                samplerate=self.samplerate, channels=channels,
                callback=self._callback, finished_callback=finished)
            self._stream.start()
            self._playing = True
        except Exception as exc:
            raise PlaybackUnavailable(f"Could not open an audio output device: {exc}") from exc

    def pause(self):
        # Hand the actual stream off to a background thread rather than
        # tearing it down here: even abort()/close() are synchronous PortAudio
        # calls that wait on the host API's own stop confirmation -- measured
        # ~100ms on this machine's CoreAudio device -- and pause() runs
        # directly on the GUI thread from the Play/Pause button callback, so
        # that wait was a visible freeze every time. Updating _stream/_playing
        # immediately lets the GUI (and a follow-up play()) proceed instantly.
        stream = self._stream
        self._stream = None
        self._playing = False
        if stream is not None:
            threading.Thread(target=_teardown_stream, args=(stream,), daemon=True).start()

    def is_playing(self) -> bool:
        return self._playing


def _teardown_stream(stream):
    try:
        stream.abort()
        stream.close()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Synced multi-track mix player (for the "Synced Preview")
# --------------------------------------------------------------------------

@dataclass
class MixTrack:
    samples: np.ndarray       # shape (n, channels)
    offset_seconds: float
    label: str = ""


class MixPlayer:
    """
    Plays several tracks summed together in real time, each shifted by its
    own offset_seconds -- the same math the exporter applies, just live and
    mixed to stereo for listening rather than kept as discrete channels.
    """

    def __init__(self, tracks: List[MixTrack], samplerate: int = PREVIEW_SAMPLERATE, channels: int = 2):
        self.tracks = tracks
        self.samplerate = samplerate
        self.channels = channels
        self._pos = 0  # global frame index, t=0 is the sync reference point
        self._stream = None
        self._playing = False
        self.on_stop: Optional[callable] = None
        # `tracks` is fixed for the lifetime of this player (a new MixPlayer
        # is built each time playback starts), so the total length can be
        # computed once here instead of recomputed -- via a Python loop over
        # every track -- on every real-time audio callback.
        self._total_len_frames = int(self.duration * self.samplerate)

    @property
    def duration(self) -> float:
        end = 0.0
        for t in self.tracks:
            end = max(end, t.offset_seconds + t.samples.shape[0] / self.samplerate)
        return end

    def seek(self, seconds: float):
        self._pos = int(max(0.0, seconds) * self.samplerate)

    def position_seconds(self) -> float:
        return self._pos / float(self.samplerate)

    def _mix_chunk(self, start_frame: int, frames: int) -> np.ndarray:
        out = np.zeros((frames, self.channels), dtype=np.float32)
        for track in self.tracks:
            offset_frames = int(round(track.offset_seconds * self.samplerate))
            # Which samples of `track` fall within [start_frame, start_frame+frames)?
            src_start = start_frame - offset_frames
            src_end = src_start + frames
            clip_src_start = max(src_start, 0)
            clip_src_end = min(src_end, track.samples.shape[0])
            if clip_src_end <= clip_src_start:
                continue
            dst_start = clip_src_start - src_start
            dst_end = dst_start + (clip_src_end - clip_src_start)
            chunk = track.samples[clip_src_start:clip_src_end]
            if chunk.shape[1] != self.channels:
                # simple channel adapt: mono->stereo duplicate, or downmix to mono
                if chunk.shape[1] == 1 and self.channels == 2:
                    chunk = np.repeat(chunk, 2, axis=1)
                elif chunk.shape[1] > self.channels:
                    chunk = chunk[:, :self.channels]
            out[dst_start:dst_end] += chunk
        # Soft clip protection: avoid harsh digital clipping if several tracks sum loud.
        peak = np.max(np.abs(out)) if out.size else 0.0
        if peak > 1.0:
            out = out / peak
        return out

    def _callback(self, outdata, frames, time_info, status):
        if self._pos >= self._total_len_frames:
            outdata[:] = 0
            raise sd.CallbackStop()
        chunk = self._mix_chunk(self._pos, frames)
        outdata[:] = chunk
        self._pos += frames

    def play(self):
        if sd is None:
            raise PlaybackUnavailable(f"Audio playback library not available: {_SOUNDDEVICE_IMPORT_ERROR}")

        def finished():
            self._playing = False
            if self.on_stop:
                self.on_stop()

        try:
            self._stream = sd.OutputStream(
                samplerate=self.samplerate, channels=self.channels,
                callback=self._callback, finished_callback=finished)
            self._stream.start()
            self._playing = True
        except Exception as exc:
            raise PlaybackUnavailable(f"Could not open an audio output device: {exc}") from exc

    def pause(self):
        # See AudioPlayer.pause(): hand the stream to a background thread for
        # teardown instead of blocking the GUI thread on abort()/close().
        stream = self._stream
        self._stream = None
        self._playing = False
        if stream is not None:
            threading.Thread(target=_teardown_stream, args=(stream,), daemon=True).start()

    def is_playing(self) -> bool:
        return self._playing


# --------------------------------------------------------------------------
# Video frame access
# --------------------------------------------------------------------------

class VideoFrameSource:
    """Thin wrapper over cv2.VideoCapture for pulling frames by timestamp."""

    def __init__(self, path: str):
        if cv2 is None:
            raise PlaybackUnavailable(f"Video preview library not available: {_CV2_IMPORT_ERROR}")
        self.path = path
        self._cap = cv2.VideoCapture(path)
        if not self._cap.isOpened():
            raise PlaybackUnavailable(f"Could not open video file for preview: {path}")
        self.fps = self._cap.get(cv2.CAP_PROP_FPS) or 25.0
        frame_count = self._cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        self.duration = (frame_count / self.fps) if self.fps else 0.0
        self._last_frame_index = -1

    def get_frame_image(self, seconds: float):
        """Return a PIL.Image (RGB) for the frame nearest `seconds`, or None.
        Used for one-off seeks (scrubbing, initial display) -- for continuous
        playback, use seek_to_time() once followed by read_next_frame_image()
        repeatedly, which is dramatically cheaper."""
        if Image is None:
            raise PlaybackUnavailable(f"Pillow not available: {_PIL_IMPORT_ERROR}")
        target_index = int(round(max(0.0, seconds) * self.fps))
        if target_index != self._last_frame_index + 1:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, target_index)
        ok, frame_bgr = self._cap.read()
        if not ok:
            return None
        self._last_frame_index = target_index
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        return Image.fromarray(frame_rgb)

    def seek_to_time(self, seconds: float):
        """Position the decoder at `seconds` without reading a frame yet.
        Call this once when starting/resuming playback; follow with repeated
        read_next_frame_image() calls rather than re-seeking every tick."""
        target_index = int(round(max(0.0, seconds) * self.fps))
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, target_index)
        self._last_frame_index = target_index - 1

    def read_next_frame_image(self):
        """Read the next frame in sequence -- no seeking, so it's fast
        (roughly 8x cheaper than a seek in testing). This is what playback's
        per-tick advance should use."""
        if Image is None:
            raise PlaybackUnavailable(f"Pillow not available: {_PIL_IMPORT_ERROR}")
        ok, frame_bgr = self._cap.read()
        if not ok:
            return None
        self._last_frame_index += 1
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        return Image.fromarray(frame_rgb)

    def release(self):
        if self._cap is not None:
            self._cap.release()
