"""
tests/test_app_helpers.py

Unit tests for app.py's pure/file-local helpers: confidence flagging,
timecode formatting, the per-video JSON cache (validity keyed on file
size + int(mtime), NOT a content hash -- see the app's own CLAUDE.md),
global settings persistence, diarization/transcript merging (the
overlap-based speaker-label picker), speaker-merge/cleanup, and the
transcript export builders (TXT/SRT/VTT). Deliberately does NOT touch
anything that needs mlx-whisper, pyannote, a real audio file, or a live
Streamlit script run (main(), process_one_video(), transcribe_audio(),
diarize_audio()) -- those need real media/GPU and belong in a separate
integration-style suite.
"""

import json
import os

import pytest

import app


# ---------------------------------------------------------------------------
# is_low_confidence
# ---------------------------------------------------------------------------

def test_is_low_confidence_flags_low_avg_logprob():
    seg = app.Segment(start=0, end=1, text="x", avg_logprob=-1.5, no_speech_prob=0.0)
    assert app.is_low_confidence(seg)


def test_is_low_confidence_flags_high_no_speech_prob():
    seg = app.Segment(start=0, end=1, text="x", avg_logprob=0.0, no_speech_prob=0.9)
    assert app.is_low_confidence(seg)


def test_is_low_confidence_false_for_confident_segment():
    seg = app.Segment(start=0, end=1, text="x", avg_logprob=-0.2, no_speech_prob=0.1)
    assert not app.is_low_confidence(seg)


# ---------------------------------------------------------------------------
# fmt_timecode / fmt_srt_time / fmt_vtt_time
# ---------------------------------------------------------------------------

def test_fmt_timecode():
    assert app.fmt_timecode(3725) == "01:02:05"
    assert app.fmt_timecode(0) == "00:00:00"
    assert app.fmt_timecode(-5) == "00:00:00"  # clamped


def test_fmt_srt_time():
    assert app.fmt_srt_time(3725.123) == "01:02:05,123"
    assert app.fmt_srt_time(0) == "00:00:00,000"
    assert app.fmt_srt_time(-1) == "00:00:00,000"


def test_fmt_vtt_time():
    assert app.fmt_vtt_time(3725.123) == "01:02:05.123"


# ---------------------------------------------------------------------------
# _cache_path / load_cache -- pure file I/O, no session_state involved
# ---------------------------------------------------------------------------

def test_cache_path():
    assert app._cache_path("/videos/interview.mp4") == "/videos/interview.mp4" + app.CACHE_SUFFIX


def test_load_cache_missing_file_returns_none(tmp_path):
    video = tmp_path / "interview.mp4"
    video.write_bytes(b"\x00" * 10)
    assert app.load_cache(str(video)) is None


def test_load_cache_returns_none_when_video_changed(tmp_path):
    video = tmp_path / "interview.mp4"
    video.write_bytes(b"\x00" * 10)
    stat = os.stat(video)
    cache_data = {"video_size": 999999, "video_mtime": int(stat.st_mtime), "segments": []}
    with open(app._cache_path(str(video)), "w") as f:
        json.dump(cache_data, f)
    assert app.load_cache(str(video)) is None  # size mismatch


def test_load_cache_valid_when_size_and_mtime_match(tmp_path):
    video = tmp_path / "interview.mp4"
    video.write_bytes(b"\x00" * 10)
    stat = os.stat(video)
    cache_data = {"video_size": stat.st_size, "video_mtime": int(stat.st_mtime), "segments": ["ok"]}
    with open(app._cache_path(str(video)), "w") as f:
        json.dump(cache_data, f)
    loaded = app.load_cache(str(video))
    assert loaded == cache_data


def test_load_cache_malformed_json_returns_none(tmp_path):
    video = tmp_path / "interview.mp4"
    video.write_bytes(b"\x00")
    with open(app._cache_path(str(video)), "w") as f:
        f.write("{not valid json")
    assert app.load_cache(str(video)) is None


# ---------------------------------------------------------------------------
# save_cache / clear_file_cache -- need st.session_state
# ---------------------------------------------------------------------------

def test_save_cache_and_load_cache_round_trip(fake_session_state, tmp_path):
    video = tmp_path / "interview.mp4"
    video.write_bytes(b"\x00" * 100)
    fr = app.FileResult(
        path=str(video), name="interview.mp4",
        segments=[app.Segment(start=0.0, end=1.0, text="Hi", speaker="Speaker 1")],
        speakers=["Speaker 1"],
    )
    fake_session_state.speaker_names[fr.path] = {"Speaker 1": "Jane"}
    fake_session_state[f"excluded::{fr.path}"] = {"Speaker 2"}

    assert app.save_cache(fr) is True
    loaded = app.load_cache(str(video))
    assert loaded["segments"][0]["text"] == "Hi"
    assert loaded["speaker_labels"] == {"Speaker 1": "Jane"}
    assert loaded["excluded_speakers"] == ["Speaker 2"]


def test_save_cache_returns_false_when_write_fails(fake_session_state, tmp_path):
    # Parent directory doesn't exist -> open() raises -> best-effort False,
    # not an exception (see save_cache's own docstring on why).
    missing_dir_video = tmp_path / "does_not_exist" / "interview.mp4"
    fr = app.FileResult(path=str(missing_dir_video), name="interview.mp4")
    assert app.save_cache(fr) is False


def test_clear_file_cache_removes_file_and_resets_state(fake_session_state, tmp_path):
    video = tmp_path / "interview.mp4"
    video.write_bytes(b"\x00" * 10)
    fr = app.FileResult(
        path=str(video), name="interview.mp4",
        segments=[app.Segment(start=0, end=1, text="hi")],
        speakers=["Speaker 1"], status="done", from_cache=True,
        undo_segments=[app.Segment(start=0, end=1, text="old")],
    )
    app.save_cache(fr)
    assert os.path.exists(app._cache_path(str(video)))

    fake_session_state.speaker_names[fr.path] = {"Speaker 1": "Jane"}
    fake_session_state[f"excluded::{fr.path}"] = {"Speaker 1"}
    fake_session_state[f"chk::{fr.path}::Speaker 1"] = True
    fake_session_state[f"label::{fr.path}::Speaker 1"] = "Jane"
    fake_session_state["unrelated_key"] = "should survive"

    app.clear_file_cache(fr)

    assert not os.path.exists(app._cache_path(str(video)))
    assert fr.segments == []
    assert fr.speakers == []
    assert fr.status == "pending"
    assert fr.from_cache is False
    assert fr.undo_segments is None
    assert fr.path not in fake_session_state.speaker_names
    assert f"excluded::{fr.path}" not in fake_session_state
    assert f"chk::{fr.path}::Speaker 1" not in fake_session_state
    assert f"label::{fr.path}::Speaker 1" not in fake_session_state
    assert fake_session_state["unrelated_key"] == "should survive"


# ---------------------------------------------------------------------------
# load_settings / save_settings
# ---------------------------------------------------------------------------

def test_load_settings_missing_file_returns_empty_dict(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "SETTINGS_PATH", str(tmp_path / "nonexistent" / "settings.json"))
    assert app.load_settings() == {}


def test_save_and_load_settings_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "APP_SUPPORT_DIR", str(tmp_path / "AppSupport"))
    monkeypatch.setattr(app, "SETTINGS_PATH", str(tmp_path / "AppSupport" / "settings.json"))
    settings = {"model_label": "Best quality (large-v3)", "enable_diarization": True}
    app.save_settings(settings)
    assert app.load_settings() == settings


# ---------------------------------------------------------------------------
# merge_transcript_and_speakers
# ---------------------------------------------------------------------------

def test_merge_assigns_speaker_by_containing_turn():
    turns = [(0.0, 5.0, "A"), (5.0, 10.0, "B")]
    segments = [{"start": 1.0, "end": 2.0, "text": "hi", "avg_logprob": 0.0, "no_speech_prob": 0.0}]
    result_segments, speaker_order = app.merge_transcript_and_speakers(segments, turns)
    assert len(result_segments) == 1
    assert result_segments[0].speaker == "A"
    assert speaker_order == ["A"]


def test_merge_falls_back_to_nearest_turn_in_a_gap():
    # Midpoint 5.75 falls in the gap between turn A (ends at 5.0) and
    # turn B (starts at 7.0) -- A is closer (0.75 away vs 1.25).
    turns = [(0.0, 5.0, "A"), (7.0, 10.0, "B")]
    segments = [{"start": 5.5, "end": 6.0, "text": "hi"}]
    result_segments, _ = app.merge_transcript_and_speakers(segments, turns)
    assert result_segments[0].speaker == "A"


def test_merge_no_diarization_turns_defaults_to_speaker_zero():
    segments = [{"start": 0.0, "end": 1.0, "text": "hi"}]
    result_segments, speaker_order = app.merge_transcript_and_speakers(segments, [])
    assert result_segments[0].speaker == "Speaker 0"
    assert speaker_order == ["Speaker 0"]


def test_merge_skips_empty_text_segments():
    segments = [
        {"start": 0.0, "end": 1.0, "text": ""},
        {"start": 1.0, "end": 2.0, "text": "real text"},
    ]
    result_segments, _ = app.merge_transcript_and_speakers(segments, [])
    assert len(result_segments) == 1
    assert result_segments[0].text == "real text"


def test_merge_speaker_order_is_first_appearance():
    turns = [(0.0, 1.0, "B"), (1.0, 2.0, "A"), (2.0, 3.0, "B")]
    segments = [
        {"start": 0.2, "end": 0.5, "text": "one"},
        {"start": 1.2, "end": 1.5, "text": "two"},
        {"start": 2.2, "end": 2.5, "text": "three"},
    ]
    _, speaker_order = app.merge_transcript_and_speakers(segments, turns)
    assert speaker_order == ["B", "A"]


# ---------------------------------------------------------------------------
# normalize_speaker_names
# ---------------------------------------------------------------------------

def test_normalize_speaker_names():
    mapping = app.normalize_speaker_names(["SPEAKER_01", "SPEAKER_00"])
    assert mapping == {"SPEAKER_01": "Speaker 1", "SPEAKER_00": "Speaker 2"}


# ---------------------------------------------------------------------------
# merge_speakers
# ---------------------------------------------------------------------------

def test_merge_speakers_reassigns_segments_and_speaker_list(fake_session_state, tmp_path):
    video = tmp_path / "interview.mp4"
    video.write_bytes(b"\x00")
    fr = app.FileResult(
        path=str(video), name="interview.mp4",
        segments=[
            app.Segment(start=0, end=1, text="a", speaker="Speaker 1"),
            app.Segment(start=1, end=2, text="b", speaker="Speaker 2"),
        ],
        speakers=["Speaker 1", "Speaker 2"],
    )
    fake_session_state.speaker_names[fr.path] = {"Speaker 2": "Bob"}
    fake_session_state[f"excluded::{fr.path}"] = {"Speaker 2"}

    app.merge_speakers(fr, target="Speaker 1", sources=["Speaker 2"])

    assert [s.speaker for s in fr.segments] == ["Speaker 1", "Speaker 1"]
    assert fr.speakers == ["Speaker 1"]
    assert "Speaker 2" not in fake_session_state[f"excluded::{fr.path}"]
    assert "Speaker 2" not in fake_session_state.speaker_names[fr.path]


def test_merge_speakers_no_op_when_only_target_given(fake_session_state, tmp_path):
    fr = app.FileResult(path=str(tmp_path / "v.mp4"), name="v.mp4",
                         segments=[app.Segment(start=0, end=1, text="a", speaker="Speaker 1")],
                         speakers=["Speaker 1"])
    app.merge_speakers(fr, target="Speaker 1", sources=["Speaker 1"])
    assert fr.speakers == ["Speaker 1"]  # unchanged, no crash


# ---------------------------------------------------------------------------
# _visible_segments / build_txt / build_srt / build_vtt / build_transcript
# ---------------------------------------------------------------------------

def _sample_segments():
    return [
        app.Segment(start=0.0, end=2.0, text="Hello there", speaker="Speaker 1"),
        app.Segment(start=2.5, end=4.0, text="General Kenobi", speaker="Speaker 2"),
    ]


def test_visible_segments_excludes_and_applies_labels():
    segs = _sample_segments()
    visible = list(app._visible_segments(segs, {"Speaker 2"}, {"Speaker 1": "Jane"}))
    assert len(visible) == 1
    seg, display_name = visible[0]
    assert seg.text == "Hello there"
    assert display_name == "Jane"


def test_visible_segments_falls_back_to_raw_speaker_id_when_unlabeled():
    segs = _sample_segments()
    visible = list(app._visible_segments(segs, set(), {}))
    names = [name for _, name in visible]
    assert names == ["Speaker 1", "Speaker 2"]


def test_build_txt_includes_header_and_source_video():
    out = app.build_txt("interview.mp4", _sample_segments(), set(), {}, source_path="/x/interview.mov")
    assert out.startswith("# Transcript: interview.mp4\n")
    assert "# Source video: /x/interview.mov" in out
    assert "[00:00:00] Speaker 1: Hello there" in out


def test_build_srt_format():
    out = app.build_srt("interview.mp4", _sample_segments(), set(), {})
    lines = out.strip("\n").split("\n")
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:02,000"
    assert lines[2] == "Speaker 1: Hello there"
    assert lines[4] == "2"


def test_build_vtt_format():
    out = app.build_vtt("interview.mp4", _sample_segments(), set(), {}, source_path="/x/interview.mov")
    assert out.startswith("WEBVTT\n")
    assert "NOTE Source video: /x/interview.mov" in out
    assert "00:00:00.000 --> 00:00:02.000" in out


def test_build_transcript_dispatches_by_format():
    segs = _sample_segments()
    txt = app.build_transcript("interview.mp4", segs, set(), {}, "Plain text (.txt)")
    srt = app.build_transcript("interview.mp4", segs, set(), {}, "SRT (.srt)")
    vtt = app.build_transcript("interview.mp4", segs, set(), {}, "WebVTT (.vtt)")
    assert txt.startswith("# Transcript")
    assert srt.startswith("1\n")
    assert vtt.startswith("WEBVTT")


# ---------------------------------------------------------------------------
# build_batch_summary_csv
# ---------------------------------------------------------------------------

def test_build_batch_summary_csv(fake_session_state):
    fr = app.FileResult(
        path="/videos/a.mp4", name="a.mp4", status="done", from_cache=True,
        speakers=["Speaker 1", "Speaker 2"],
        segments=[
            app.Segment(start=0, end=1, text="ok", avg_logprob=0.0, no_speech_prob=0.0),
            app.Segment(start=1, end=2, text="unsure", avg_logprob=-2.0, no_speech_prob=0.0),
        ],
    )
    fake_session_state.speaker_names["/videos/a.mp4"] = {"Speaker 1": "Jane"}
    fake_session_state.last_export_paths["/videos/a.mp4"] = "/exports/a.srt"

    csv_text = app.build_batch_summary_csv([fr])
    lines = csv_text.strip("\r\n").split("\r\n")
    assert lines[0].startswith("File Name,Source Path,Status")
    row = lines[1].split(",")
    assert row[0] == "a.mp4"
    assert row[2] == "done"
    assert row[3] == "yes"
    assert row[4] == "2"
    assert "Jane" in row[5] and "Speaker 2" in row[5]
    assert row[6] == "1"  # one low-confidence segment
    assert row[7] == "/exports/a.srt"
