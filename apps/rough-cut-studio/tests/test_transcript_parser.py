"""
tests/test_transcript_parser.py

Unit tests for transcript_parser.py: SMPTE timecode <-> seconds
conversion (including drop-frame), the four auto-detected transcript
formats (SRT, WebVTT, bracket/arrow, single-timecode-per-line), duration
string parsing, and detect_linked_media's embedded-path/filename-fallback
matching. Pure text/math logic, no I/O beyond parse_transcript_file's
own thin wrapper (covered separately via tmp_path).
"""

import pytest

from transcript_parser import (
    detect_linked_media,
    is_drop_frame_capable,
    parse_duration_string,
    parse_transcript,
    parse_transcript_file,
    seconds_to_duration_label,
    seconds_to_smpte,
    timecode_to_seconds,
)


# ---------------------------------------------------------------------------
# timecode_to_seconds / seconds_to_smpte
# ---------------------------------------------------------------------------

def test_timecode_to_seconds_frame_field():
    # 25fps, frame field (colon-separated 4th group) -> frames / fps
    assert timecode_to_seconds("00:00:01:05", fps=25.0) == pytest.approx(1.2)


def test_timecode_to_seconds_millisecond_field():
    # comma-separated 4th group is milliseconds, not a frame count
    assert timecode_to_seconds("00:00:01,500", fps=25.0) == pytest.approx(1.5)


def test_timecode_to_seconds_no_fraction():
    assert timecode_to_seconds("00:01:30", fps=25.0) == pytest.approx(90.0)


def test_timecode_to_seconds_invalid_raises():
    with pytest.raises(ValueError):
        timecode_to_seconds("not a timecode", fps=25.0)


def test_seconds_to_smpte_non_drop_roundtrip():
    tc = seconds_to_smpte(90.2, fps=25.0)
    assert tc == "00:01:30:05"
    assert timecode_to_seconds(tc, fps=25.0) == pytest.approx(90.2)


def test_is_drop_frame_capable():
    assert is_drop_frame_capable(29.97)
    assert is_drop_frame_capable(59.94)
    assert not is_drop_frame_capable(25.0)
    assert not is_drop_frame_capable(30.0)


def test_seconds_to_smpte_drop_frame_one_hour_reference_point():
    # The defining property of drop-frame timecode: it's designed to
    # realign with wall-clock time on the hour (and every 10th minute).
    # At exactly 3600s of elapsed real time and 29.97fps, drop-frame
    # timecode reads exactly 01:00:00;00 -- a standard reference value
    # for verifying a drop-frame implementation.
    assert seconds_to_smpte(3600.0, fps=29.97, drop_frame=True) == "01:00:00;00"


def test_seconds_to_smpte_drop_frame_uses_semicolon_separator():
    tc = seconds_to_smpte(10.0, fps=29.97, drop_frame=True)
    assert ";" in tc
    assert tc.count(":") == 2


def test_drop_frame_roundtrip_via_semicolon_separator():
    # A ':'-separated (not ';') drop-frame reading should still be
    # interpreted as drop-frame once the project's drop_frame flag is
    # on -- see the docstring in timecode_to_seconds. Round-trip through
    # seconds_to_smpte -> replace ';' with ':' -> timecode_to_seconds.
    original_seconds = 125.4
    tc = seconds_to_smpte(original_seconds, fps=29.97, drop_frame=True)
    colon_tc = tc.replace(";", ":")
    recovered = timecode_to_seconds(colon_tc, fps=29.97, drop_frame=True)
    assert recovered == pytest.approx(original_seconds, abs=1.0 / 29.97)


def test_seconds_to_smpte_non_drop_frame_rate_ignores_drop_flag():
    # drop_frame=True is a no-op at a rate that isn't drop-frame capable
    tc_drop_requested = seconds_to_smpte(61.0, fps=25.0, drop_frame=True)
    tc_plain = seconds_to_smpte(61.0, fps=25.0, drop_frame=False)
    assert tc_drop_requested == tc_plain
    assert ";" not in tc_drop_requested


def test_seconds_to_smpte_clamps_negative_to_zero():
    assert seconds_to_smpte(-5.0, fps=25.0) == "00:00:00:00"


# ---------------------------------------------------------------------------
# parse_duration_string / seconds_to_duration_label
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("90", 90.0),
    ("90s", 90.0),
    ("1:30", 90.0),
    ("01:02:03", 3723.0),
    (None, None),
    ("", None),
    ("   ", None),
])
def test_parse_duration_string_valid(text, expected):
    assert parse_duration_string(text) == expected


@pytest.mark.parametrize("text", ["abc", "1:2:3:4", "1:xy"])
def test_parse_duration_string_invalid_raises(text):
    with pytest.raises(ValueError):
        parse_duration_string(text)


def test_seconds_to_duration_label():
    assert seconds_to_duration_label(45) == "45s"
    assert seconds_to_duration_label(92) == "1m 32s"
    assert seconds_to_duration_label(3725) == "1h 2m 5s"


# ---------------------------------------------------------------------------
# parse_transcript: format auto-detection
# ---------------------------------------------------------------------------

def test_parse_transcript_empty_returns_empty_list():
    assert parse_transcript("", fps=25.0) == []
    assert parse_transcript("   \n  ", fps=25.0) == []


def test_parse_transcript_srt():
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:04,000\n"
        "Jane: We started filming in March.\n"
        "\n"
        "2\n"
        "00:00:04,500 --> 00:00:06,000\n"
        "It was raining a lot.\n"
    )
    segs = parse_transcript(srt, fps=25.0)
    assert len(segs) == 2
    assert segs[0].speaker == "Jane"
    assert segs[0].text == "We started filming in March."
    assert segs[0].start_seconds == pytest.approx(1.0)
    assert segs[0].end_seconds == pytest.approx(4.0)
    assert segs[1].index == 1


def test_parse_transcript_webvtt():
    vtt = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "Hello there\n\n"
        "00:00:05.000 --> 00:00:07.000\n"
        "Second line\n"
    )
    segs = parse_transcript(vtt, fps=25.0)
    assert len(segs) == 2
    assert segs[0].text == "Hello there"
    assert segs[0].speaker is None


def test_parse_transcript_bracket_arrow():
    content = (
        "[00:00:01:00 - 00:00:04:00] SPEAKER: Hello there\n"
        "[00:00:04:00 - 00:00:06:00] SPEAKER: More talking\n"
        "[00:00:06:00 - 00:00:08:00] SPEAKER: Even more\n"
    )
    segs = parse_transcript(content, fps=25.0)
    assert len(segs) == 3
    assert segs[0].speaker == "SPEAKER"
    assert segs[0].text == "Hello there"


def test_parse_transcript_single_timecode_per_line():
    content = (
        "00:12:34 Jane: We started filming in March.\n"
        "00:12:40 Jane: It rained a lot that week.\n"
    )
    segs = parse_transcript(content, fps=25.0)
    assert len(segs) == 2
    assert segs[0].speaker == "Jane"
    assert segs[0].start_seconds == pytest.approx(12 * 60 + 34)
    # end time is inferred from the next line's start
    assert segs[0].end_seconds == pytest.approx(12 * 60 + 40)
    # last line gets a default +4s tail since there's no next line
    assert segs[1].end_seconds == pytest.approx(segs[1].start_seconds + 4.0)


def test_parse_transcript_drops_blank_text_segments():
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:04,000\n"
        "\n"
        "\n"
        "2\n"
        "00:00:04,500 --> 00:00:06,000\n"
        "Real text\n"
    )
    segs = parse_transcript(srt, fps=25.0)
    assert len(segs) == 1
    assert segs[0].text == "Real text"
    assert segs[0].index == 0  # re-indexed after the blank one was dropped


def test_parse_transcript_zero_length_segment_gets_min_duration():
    # A segment whose end <= start (malformed/degenerate input) should
    # still produce a usable clip rather than a zero/negative-length one.
    srt = "1\n00:00:01,000 --> 00:00:01,000\nOops\n"
    segs = parse_transcript(srt, fps=25.0)
    assert len(segs) == 1
    assert segs[0].end_seconds > segs[0].start_seconds


# ---------------------------------------------------------------------------
# parse_transcript_file
# ---------------------------------------------------------------------------

def test_parse_transcript_file(tmp_path):
    path = tmp_path / "transcript.srt"
    path.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\nHello from disk\n",
        encoding="utf-8",
    )
    segs = parse_transcript_file(str(path), fps=25.0)
    assert len(segs) == 1
    assert segs[0].text == "Hello from disk"


# ---------------------------------------------------------------------------
# detect_linked_media
# ---------------------------------------------------------------------------

def test_detect_linked_media_embedded_path(tmp_path):
    video_path = tmp_path / "interview.mov"
    video_path.write_bytes(b"\x00")
    transcript_path = tmp_path / "somewhere_else.txt"
    content = f"# Source video: {video_path}\n\n00:00:01 Hello\n"
    assert detect_linked_media(str(transcript_path), content) == str(video_path)


def test_detect_linked_media_embedded_path_stale_falls_back_to_filename(tmp_path):
    # The embedded path points at a file that no longer exists; fall
    # back to same-name-same-folder matching instead of trusting it.
    transcript_path = tmp_path / "interview.txt"
    real_video = tmp_path / "interview.mp4"
    real_video.write_bytes(b"\x00")
    content = "# Source video: /no/such/path/interview.mov\n\n00:00:01 Hello\n"
    assert detect_linked_media(str(transcript_path), content) == str(real_video)


def test_detect_linked_media_filename_fallback(tmp_path):
    transcript_path = tmp_path / "clip_01.srt"
    video_path = tmp_path / "clip_01.mp4"
    video_path.write_bytes(b"\x00")
    assert detect_linked_media(str(transcript_path), "no header here") == str(video_path)


def test_detect_linked_media_none_found(tmp_path):
    transcript_path = tmp_path / "clip_01.srt"
    assert detect_linked_media(str(transcript_path), "no header, no matching video") is None
