"""
tests/test_sync_core.py

Unit tests for sync_core.py's pure logic: ProbeInfo parsing from raw
ffprobe-shaped dicts, waveform cross-correlation offset math, BWF bext
TimeReference parsing (hand-built WAV fixtures, no ffmpeg needed), video
timecode-tag parsing, export command construction, and audio-track
grouping. Deliberately does NOT invoke real ffmpeg/ffprobe (ffmpeg_available,
extract_mono_pcm, decode_audio_array, probe/probe_info) or anything that
needs a real media file -- those need real fixtures and belong in a
separate integration-style suite, same split B-Roll Analyzer's own
suite already uses for analyze_clip().
"""

import struct

import numpy as np
import pytest

from sync_core import (
    AudioTrackSpec,
    ProbeInfo,
    _delay_or_trim_filter,
    bwf_timecode_seconds,
    build_export_command,
    compute_offset,
    group_audio_tracks_by_output,
    read_bwf_timeref,
    video_timecode_seconds,
    waveform_offset,
)


# ---------------------------------------------------------------------------
# ProbeInfo.from_probe
# ---------------------------------------------------------------------------

def test_probeinfo_parses_video_and_audio_streams():
    data = {
        "format": {"duration": "12.5", "tags": {}},
        "streams": [
            {"codec_type": "video", "avg_frame_rate": "24000/1001"},
            {"codec_type": "audio", "sample_rate": "48000", "channels": 2,
             "sample_fmt": "flt", "codec_name": "pcm_f32le", "bits_per_raw_sample": "32"},
        ],
    }
    info = ProbeInfo.from_probe(data, "/media/a.mov")
    assert info.duration == pytest.approx(12.5)
    assert info.has_video and info.has_audio
    assert info.video_fps == pytest.approx(24000 / 1001)
    assert info.audio_samplerate == 48000
    assert info.audio_channels == 2
    assert info.audio_bits_per_sample == 32


def test_probeinfo_no_streams():
    info = ProbeInfo.from_probe({"format": {}}, "/media/a.mov")
    assert not info.has_video
    assert not info.has_audio
    assert info.duration == 0.0


def test_probeinfo_prefers_bits_per_raw_sample_over_bits_per_sample():
    # bits_per_raw_sample reflects the source container's real bit depth
    # (e.g. 24 for pcm_s24le); bits_per_sample is only a fallback.
    data = {"format": {}, "streams": [
        {"codec_type": "audio", "sample_rate": "48000", "bits_per_raw_sample": "24", "bits_per_sample": "32"},
    ]}
    info = ProbeInfo.from_probe(data, "/media/a.wav")
    assert info.audio_bits_per_sample == 24


def test_probeinfo_falls_back_to_bits_per_sample_when_raw_missing():
    data = {"format": {}, "streams": [
        {"codec_type": "audio", "sample_rate": "48000", "bits_per_raw_sample": "N/A", "bits_per_sample": "16"},
    ]}
    info = ProbeInfo.from_probe(data, "/media/a.wav")
    assert info.audio_bits_per_sample == 16


def test_probeinfo_timecode_tag_from_data_stream():
    data = {"format": {"tags": {}}, "streams": [
        {"codec_type": "data", "codec_tag_string": "tmcd", "tags": {"timecode": "01:00:00:00"}},
    ]}
    info = ProbeInfo.from_probe(data, "/media/a.mov")
    assert info.timecode_tag == "01:00:00:00"


def test_probeinfo_timecode_tag_prefers_format_tag_over_stream_tag():
    data = {"format": {"tags": {"timecode": "02:00:00:00"}}, "streams": [
        {"codec_type": "data", "codec_tag_string": "tmcd", "tags": {"timecode": "01:00:00:00"}},
    ]}
    info = ProbeInfo.from_probe(data, "/media/a.mov")
    assert info.timecode_tag == "02:00:00:00"


@pytest.mark.parametrize("sample_fmt,bits,expected", [
    ("flt", None, "32-bit float"),
    ("fltp", None, "32-bit float"),  # planar suffix stripped
    ("dbl", None, "64-bit float"),
    (None, 24, "24-bit integer"),
    (None, 16, "16-bit integer"),
    ("s32", None, "32-bit integer"),  # fallback purely from sample_fmt
    (None, None, "unknown format"),
])
def test_probeinfo_audio_format_label(sample_fmt, bits, expected):
    info = ProbeInfo(path="x", duration=0, has_video=False, has_audio=True,
                      audio_sample_fmt=sample_fmt, audio_bits_per_sample=bits)
    assert info.audio_format_label == expected


@pytest.mark.parametrize("sample_fmt,bits,expected", [
    ("dbl", None, True),
    (None, 24, False),
    (None, 32, False),
    (None, 64, True),
    ("flt", None, False),
])
def test_probeinfo_exceeds_32bit_float(sample_fmt, bits, expected):
    info = ProbeInfo(path="x", duration=0, has_video=False, has_audio=True,
                      audio_sample_fmt=sample_fmt, audio_bits_per_sample=bits)
    assert info.exceeds_32bit_float is expected


# ---------------------------------------------------------------------------
# waveform_offset
# ---------------------------------------------------------------------------

def _shared_signal(n=4000, seed=42):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(n).astype(np.float32)


def test_waveform_offset_target_has_leading_content_ref_lacks_is_negative():
    # target's file contains extra recorded time at the front that ref
    # doesn't have (e.g. the external recorder was started before the
    # camera) -- lines up with ref only by trimming target's start, i.e.
    # a negative offset (see the module docstring: "negative means target
    # starts before ref and must be trimmed").
    base = _shared_signal()
    shift = 240  # samples @ 8000Hz = 0.03s
    sr = 8000
    target = np.concatenate([np.zeros(shift, dtype=np.float32), base])[: len(base)]
    offset = waveform_offset(base, target, sr)
    assert offset == pytest.approx(-shift / sr, abs=1.0 / sr)


def test_waveform_offset_target_missing_leading_content_ref_has_is_positive():
    # target is missing the first `shift` samples of the shared content
    # that ref has (external recorder started after the camera) -- target
    # must be delayed (started later) in the final timeline to line up.
    base = _shared_signal()
    shift = 240
    sr = 8000
    target = np.concatenate([base[shift:], np.zeros(shift, dtype=np.float32)])
    offset = waveform_offset(base, target, sr)
    assert offset == pytest.approx(shift / sr, abs=1.0 / sr)


def test_waveform_offset_zero_when_identical():
    base = _shared_signal()
    assert waveform_offset(base, base, 8000) == pytest.approx(0.0, abs=1e-9)


def test_waveform_offset_swapping_ref_and_target_negates_result():
    base = _shared_signal()
    shift = 240
    sr = 8000
    target = np.concatenate([np.zeros(shift, dtype=np.float32), base])[: len(base)]
    assert waveform_offset(base, target, sr) == pytest.approx(-waveform_offset(target, base, sr))


def test_waveform_offset_empty_buffer_raises():
    with pytest.raises(ValueError):
        waveform_offset(np.array([], dtype=np.float32), _shared_signal(), 8000)
    with pytest.raises(ValueError):
        waveform_offset(_shared_signal(), np.array([], dtype=np.float32), 8000)


# ---------------------------------------------------------------------------
# read_bwf_timeref / bwf_timecode_seconds -- hand-built WAV fixtures
# ---------------------------------------------------------------------------

def _riff_chunk(chunk_id: bytes, data: bytes) -> bytes:
    chunk = chunk_id + struct.pack("<I", len(data)) + data
    if len(data) % 2 == 1:
        chunk += b"\x00"
    return chunk


def _fmt_chunk(sample_rate: int) -> bytes:
    # AudioFormat=1 (PCM), NumChannels=1, SampleRate, ByteRate, BlockAlign=2, BitsPerSample=16
    data = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    return _riff_chunk(b"fmt ", data)


def _bext_chunk(time_reference: int) -> bytes:
    # Per EBU Tech 3285: Description[256] + Originator[32] +
    # OriginatorReference[32] + OriginationDate[10] + OriginationTime[8]
    # = 338 bytes, then TimeReferenceLow/High (4 bytes each) at 338/342.
    data = bytearray(346)
    low = time_reference & 0xFFFFFFFF
    high = (time_reference >> 32) & 0xFFFFFFFF
    struct.pack_into("<I", data, 338, low)
    struct.pack_into("<I", data, 342, high)
    return _riff_chunk(b"bext", bytes(data))


def _build_wav(tmp_path, sample_rate=48000, bext_time_reference=None, include_fmt=True):
    body = b""
    if include_fmt:
        body += _fmt_chunk(sample_rate)
    if bext_time_reference is not None:
        body += _bext_chunk(bext_time_reference)
    body += _riff_chunk(b"data", b"\x00\x00" * 4)
    wav = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body
    path = tmp_path / "test.wav"
    path.write_bytes(wav)
    return str(path)


def test_read_bwf_timeref_plain_wav_returns_none(tmp_path):
    path = _build_wav(tmp_path, bext_time_reference=None)
    assert read_bwf_timeref(path) is None


def test_read_bwf_timeref_parses_real_offset(tmp_path):
    # 12:00:00.5 since midnight @ 48000Hz = 43200.5 * 48000 samples.
    time_ref = int(43200.5 * 48000)
    path = _build_wav(tmp_path, sample_rate=48000, bext_time_reference=time_ref)
    result = read_bwf_timeref(path)
    assert result == (time_ref, 48000)


def test_read_bwf_timeref_large_time_reference_uses_high_word(tmp_path):
    # A value that overflows a 32-bit low word, to confirm the high/low
    # DWORD pair is reassembled correctly rather than truncated.
    time_ref = (1 << 32) + 12345
    path = _build_wav(tmp_path, sample_rate=48000, bext_time_reference=time_ref)
    result = read_bwf_timeref(path)
    assert result == (time_ref, 48000)


def test_bwf_timecode_seconds(tmp_path):
    time_ref = 48000 * 3661  # 1h 1m 1s
    path = _build_wav(tmp_path, sample_rate=48000, bext_time_reference=time_ref)
    assert bwf_timecode_seconds(path) == pytest.approx(3661.0)


def test_bwf_timecode_seconds_no_bext_returns_none(tmp_path):
    path = _build_wav(tmp_path, bext_time_reference=None)
    assert bwf_timecode_seconds(path) is None


def test_read_bwf_timeref_not_a_wav_file(tmp_path):
    path = tmp_path / "not_wav.bin"
    path.write_bytes(b"this is not a RIFF file at all")
    assert read_bwf_timeref(str(path)) is None


def test_read_bwf_timeref_bext_chunk_too_short_is_ignored(tmp_path):
    # A bext chunk shorter than the real TimeReference field's end offset
    # (346 bytes) must be skipped, not read out-of-bounds / garbage.
    body = _fmt_chunk(48000) + _riff_chunk(b"bext", b"\x00" * 100) + _riff_chunk(b"data", b"\x00\x00")
    wav = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body
    path = tmp_path / "short_bext.wav"
    path.write_bytes(wav)
    assert read_bwf_timeref(str(path)) is None


# ---------------------------------------------------------------------------
# video_timecode_seconds / compute_offset dispatch
# ---------------------------------------------------------------------------

def test_video_timecode_seconds_parses_hhmmssff(monkeypatch):
    import sync_core

    fake_info = ProbeInfo(path="x", duration=0, has_video=True, has_audio=False,
                           video_fps=25.0, timecode_tag="01:02:03:12")
    monkeypatch.setattr(sync_core, "probe_info", lambda path: fake_info)
    seconds = video_timecode_seconds("/media/a.mov")
    assert seconds == pytest.approx(1 * 3600 + 2 * 60 + 3 + 12 / 25.0)


def test_video_timecode_seconds_handles_semicolon_dropframe_separator(monkeypatch):
    import sync_core

    fake_info = ProbeInfo(path="x", duration=0, has_video=True, has_audio=False,
                           video_fps=29.97, timecode_tag="00:00:10;05")
    monkeypatch.setattr(sync_core, "probe_info", lambda path: fake_info)
    seconds = video_timecode_seconds("/media/a.mov")
    assert seconds == pytest.approx(10 + 5 / 29.97)


def test_video_timecode_seconds_no_tag_returns_none(monkeypatch):
    import sync_core

    fake_info = ProbeInfo(path="x", duration=0, has_video=True, has_audio=False, timecode_tag=None)
    monkeypatch.setattr(sync_core, "probe_info", lambda path: fake_info)
    assert video_timecode_seconds("/media/a.mov") is None


def test_compute_offset_unknown_method_raises():
    with pytest.raises(ValueError):
        compute_offset("/media/a.mov", "/media/a.wav", method="telepathy")


def test_compute_offset_dispatches_to_named_function(monkeypatch):
    import sync_core

    monkeypatch.setattr(sync_core, "compute_waveform_offset", lambda v, a: 1.5)
    monkeypatch.setattr(sync_core, "compute_timecode_offset", lambda v, a: 2.5)
    assert compute_offset("v", "a", method="waveform") == 1.5
    assert compute_offset("v", "a", method="timecode") == 2.5


# ---------------------------------------------------------------------------
# _delay_or_trim_filter / group_audio_tracks_by_output
# ---------------------------------------------------------------------------

def test_delay_filter_positive_offset_builds_adelay():
    filt = _delay_or_trim_filter(0.25, channels=2)
    assert filt == "adelay=250|250:all=1"


def test_delay_filter_zero_offset_builds_adelay_zero():
    assert _delay_or_trim_filter(0.0, channels=1) == "adelay=0:all=1"


def test_delay_filter_negative_offset_builds_atrim():
    filt = _delay_or_trim_filter(-0.5, channels=2)
    assert filt == "atrim=start=0.500000,asetpts=PTS-STARTPTS"


def test_group_audio_tracks_by_output_orders_by_track_number():
    a = AudioTrackSpec(path="a.wav", track=2)
    b = AudioTrackSpec(path="b.wav", track=1)
    c = AudioTrackSpec(path="c.wav", track=1)
    groups = group_audio_tracks_by_output([a, b, c])
    assert [num for num, _ in groups] == [1, 2]
    assert groups[0][1] == [b, c]  # original relative order preserved within a group
    assert groups[1][1] == [a]


# ---------------------------------------------------------------------------
# build_export_command
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_probe_info(monkeypatch):
    """Avoid a real ffprobe call: every source is treated as 2-channel audio."""
    import sync_core

    def _fake(path):
        return ProbeInfo(path=path, duration=10.0, has_video=True, has_audio=True, audio_channels=2)

    monkeypatch.setattr(sync_core, "probe_info", _fake)


def test_build_export_command_unknown_codec_raises():
    with pytest.raises(ValueError):
        build_export_command("v.mov", [], "out.mov", video_codec="not-a-codec")


def test_build_export_command_single_track_maps_video_and_audio(stub_probe_info):
    tracks = [AudioTrackSpec(path="a.wav", offset_seconds=0.0)]
    cmd = build_export_command("v.mov", tracks, "out.mov")
    assert cmd[0] == "ffmpeg"
    assert "-i" in cmd and "v.mov" in cmd and "a.wav" in cmd
    assert "-map" in cmd
    map_indices = [i for i, arg in enumerate(cmd) if arg == "-map"]
    mapped = [cmd[i + 1] for i in map_indices]
    assert "0:v" in mapped
    assert "[a1]" in mapped
    assert "-c:a" in cmd and "pcm_f32le" in cmd
    assert cmd[-1] == "out.mov"


def test_build_export_command_no_audio_tracks_video_only(stub_probe_info):
    cmd = build_export_command("v.mov", [], "out.mov")
    map_indices = [i for i, arg in enumerate(cmd) if arg == "-map"]
    mapped = [cmd[i + 1] for i in map_indices]
    # trailing "0:d?" is the always-appended optional data/timecode stream
    assert mapped == ["0:v", "0:d?"]
    assert "-c:a" not in cmd


def test_build_export_command_merges_tracks_sharing_output_number(stub_probe_info):
    tracks = [
        AudioTrackSpec(path="lav1.wav", track=1),
        AudioTrackSpec(path="lav2.wav", track=1),
        AudioTrackSpec(path="boom.wav", track=2),
    ]
    cmd = build_export_command("v.mov", tracks, "out.mov")
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "amerge=inputs=2" in filter_complex
    map_indices = [i for i, arg in enumerate(cmd) if arg == "-map"]
    mapped = [cmd[i + 1] for i in map_indices]
    # track 1 (merged) and track 2 (solo) each become one mapped output
    # stream; trailing "0:d?" is the always-appended optional data stream.
    assert mapped == ["0:v", "[t1]", "[a3]", "0:d?"]


def test_build_export_command_keep_camera_audio_adds_extra_stream(stub_probe_info):
    tracks = [AudioTrackSpec(path="a.wav")]
    cmd = build_export_command("v.mov", tracks, "out.mov", keep_camera_audio=True)
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:a]anull[cam]" in filter_complex
    map_indices = [i for i, arg in enumerate(cmd) if arg == "-map"]
    mapped = [cmd[i + 1] for i in map_indices]
    assert "[cam]" in mapped
    assert mapped[-1] == "0:d?"  # always-appended optional data stream


def test_build_export_command_copy_codec_uses_stream_copy(stub_probe_info):
    cmd = build_export_command("v.mov", [], "out.mov", video_codec="copy")
    idx = cmd.index("-c:v")
    assert cmd[idx + 1] == "copy"
