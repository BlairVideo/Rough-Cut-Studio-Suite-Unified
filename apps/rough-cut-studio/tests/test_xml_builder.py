"""
tests/test_xml_builder.py

Unit tests for xml_builder.py's build_premiere_xml (XMEML v5 for
Premiere Pro): frame math, the mandatory two-mono-clip stereo pattern
(see this app's CLAUDE.md -- a single channelcount=2 clipitem silently
imports as mono in Premiere), B-roll lane assignment for overlapping
clips, and duck_main audio-level filters. Output is parsed back with
ElementTree rather than string-matched, so assertions survive
pretty-printing changes.
"""

import xml.etree.ElementTree as ET

import pytest

from xml_builder import build_premiere_xml


def _seg(order, source_path="/media/a.mov", in_s=0.0, out_s=2.0, name="A", note=None):
    seg = {
        "order": order,
        "source_path": source_path,
        "source_name": name,
        "in_seconds": in_s,
        "out_seconds": out_s,
    }
    if note is not None:
        seg["note"] = note
    return seg


def test_single_clip_basic_structure():
    xml_string, warnings = build_premiere_xml(
        "My Sequence", fps=25.0, resolved_segments=[_seg(0, out_s=4.0)],
    )
    assert warnings == []
    root = ET.fromstring(xml_string)
    assert root.tag == "xmeml"
    sequence = root.find("sequence")
    assert sequence.find("name").text == "My Sequence"
    # 4 seconds at 25fps == 100 frames
    assert sequence.find("duration").text == "100"


def test_stereo_audio_uses_two_linked_mono_clips_not_channelcount_two():
    xml_string, _ = build_premiere_xml(
        "Seq", fps=25.0, resolved_segments=[_seg(0)],
    )
    root = ET.fromstring(xml_string)
    audio_tracks = root.find("sequence/media/audio").findall("track")
    assert len(audio_tracks) == 2  # left, right
    left_clip = audio_tracks[0].find("clipitem")
    right_clip = audio_tracks[1].find("clipitem")
    assert left_clip is not None and right_clip is not None
    assert left_clip.find("sourcetrack/trackindex").text == "1"
    assert right_clip.find("sourcetrack/trackindex").text == "2"
    # No channelcount=2 audio clipitem should exist anywhere.
    assert root.find("sequence/media/audio/format/channelcount").text == "2"
    for clip in root.iter("clipitem"):
        assert clip.find("channelcount") is None


def test_stereo_clips_are_linked_to_video_clip():
    xml_string, _ = build_premiere_xml(
        "Seq", fps=25.0, resolved_segments=[_seg(0)],
    )
    root = ET.fromstring(xml_string)
    video_clip = root.find("sequence/media/video/track/clipitem")
    link_refs = {link.find("linkclipref").text for link in video_clip.findall("link")}
    audio_clips = root.findall("sequence/media/audio/track/clipitem")
    audio_ids = {c.get("id") for c in audio_clips}
    assert link_refs == {video_clip.get("id")} | audio_ids


def test_multiple_clips_share_one_file_element_per_source():
    segs = [
        _seg(0, source_path="/media/a.mov", in_s=0.0, out_s=2.0),
        _seg(1, source_path="/media/a.mov", in_s=5.0, out_s=7.0),
        _seg(2, source_path="/media/b.mov", in_s=0.0, out_s=1.0),
    ]
    xml_string, _ = build_premiere_xml("Seq", fps=25.0, resolved_segments=segs)
    root = ET.fromstring(xml_string)
    video_clips = root.findall("sequence/media/video/track/clipitem")
    assert len(video_clips) == 3
    # First occurrence of a source defines the <file>; repeats reference it by id only.
    file_elements = [c.find("file") for c in video_clips]
    assert file_elements[0].find("pathurl") is not None
    assert file_elements[1].find("pathurl") is None  # a.mov repeat: id-only reference
    assert file_elements[2].find("pathurl") is not None  # b.mov: new file
    assert file_elements[0].get("id") == file_elements[1].get("id")
    assert file_elements[0].get("id") != file_elements[2].get("id")


def test_clips_are_placed_in_order_regardless_of_input_order():
    # order=1 should end up second on the timeline even if passed first.
    segs = [_seg(1, out_s=3.0), _seg(0, out_s=2.0)]
    xml_string, _ = build_premiere_xml("Seq", fps=25.0, resolved_segments=segs)
    root = ET.fromstring(xml_string)
    video_clips = root.findall("sequence/media/video/track/clipitem")
    starts = [int(c.find("start").text) for c in video_clips]
    assert starts == [0, 50]  # first clip (order 0) is 2s == 50 frames at 25fps


def test_no_source_dims_defaults_to_square_1080p():
    xml_string, _ = build_premiere_xml("Seq", fps=25.0, resolved_segments=[_seg(0)])
    root = ET.fromstring(xml_string)
    seq_char = root.find("sequence/media/video/format/samplecharacteristics")
    assert seq_char.find("width").text == "1920"
    assert seq_char.find("height").text == "1080"
    assert seq_char.find("pixelaspectratio").text == "square"
    assert seq_char.find("anamorphic").text == "FALSE"
    file_char = root.find("sequence/media/video/track/clipitem/file/media/video/samplecharacteristics")
    assert file_char.find("pixelaspectratio").text == "square"


def test_source_dims_used_for_sequence_and_file_geometry():
    source_dims = {"/media/a.mov": {"width": 720, "height": 480, "par_num": 10, "par_den": 11}}
    xml_string, _ = build_premiere_xml(
        "Seq", fps=25.0, resolved_segments=[_seg(0)], source_dims=source_dims,
    )
    root = ET.fromstring(xml_string)
    seq_char = root.find("sequence/media/video/format/samplecharacteristics")
    assert seq_char.find("width").text == "720"
    assert seq_char.find("height").text == "480"
    assert seq_char.find("pixelaspectratio").text == "NTSC-601"
    assert seq_char.find("anamorphic").text == "TRUE"
    file_char = root.find("sequence/media/video/track/clipitem/file/media/video/samplecharacteristics")
    assert file_char.find("width").text == "720"
    assert file_char.find("pixelaspectratio").text == "NTSC-601"


def test_source_dims_per_file_not_global():
    # Two sources with different real geometry -- each <file> must reflect
    # its own dimensions/PAR, not whichever source happened to seed the
    # sequence-level defaults.
    segs = [
        _seg(0, source_path="/media/a.mov", out_s=2.0),
        _seg(1, source_path="/media/b.mov", in_s=0.0, out_s=1.0),
    ]
    source_dims = {
        "/media/a.mov": {"width": 1920, "height": 1080, "par_num": 1, "par_den": 1},
        "/media/b.mov": {"width": 1440, "height": 1080, "par_num": 4, "par_den": 3},
    }
    xml_string, _ = build_premiere_xml("Seq", fps=25.0, resolved_segments=segs, source_dims=source_dims)
    root = ET.fromstring(xml_string)
    file_elements = [c.find("file") for c in root.findall("sequence/media/video/track/clipitem")]
    a_char = file_elements[0].find("media/video/samplecharacteristics")
    b_char = file_elements[1].find("media/video/samplecharacteristics")
    assert a_char.find("width").text == "1920"
    assert a_char.find("pixelaspectratio").text == "square"
    assert b_char.find("width").text == "1440"
    # 4:3 isn't one of XMEML's known non-square presets -- falls back to
    # "square" rather than guessing an unsupported enum value.
    assert b_char.find("pixelaspectratio").text == "square"


def test_unrecognized_par_falls_back_to_square_not_a_guess():
    source_dims = {"/media/a.mov": {"width": 2000, "height": 1080, "par_num": 3, "par_den": 2}}
    xml_string, _ = build_premiere_xml("Seq", fps=25.0, resolved_segments=[_seg(0)], source_dims=source_dims)
    root = ET.fromstring(xml_string)
    seq_char = root.find("sequence/media/video/format/samplecharacteristics")
    assert seq_char.find("pixelaspectratio").text == "square"
    assert seq_char.find("anamorphic").text == "FALSE"


def test_non_overlapping_broll_shares_single_extra_track_no_warning():
    main = [_seg(0, out_s=10.0)]
    broll = [
        {**_seg(0, source_path="/media/br1.mov", out_s=1.0), "timeline_start_seconds": 0.0},
        {**_seg(0, source_path="/media/br2.mov", out_s=1.0), "timeline_start_seconds": 2.0},
    ]
    xml_string, warnings = build_premiere_xml(
        "Seq", fps=25.0, resolved_segments=main, broll_segments=broll,
    )
    assert warnings == []
    root = ET.fromstring(xml_string)
    video_tracks = root.findall("sequence/media/video/track")
    assert len(video_tracks) == 2  # main + one broll lane
    assert len(video_tracks[1].findall("clipitem")) == 2


def test_overlapping_broll_gets_separate_tracks_and_warning():
    main = [_seg(0, out_s=10.0)]
    broll = [
        {**_seg(0, source_path="/media/br1.mov", out_s=3.0), "timeline_start_seconds": 0.0},
        {**_seg(0, source_path="/media/br2.mov", out_s=3.0), "timeline_start_seconds": 1.0},
    ]
    xml_string, warnings = build_premiere_xml(
        "Seq", fps=25.0, resolved_segments=main, broll_segments=broll,
    )
    assert len(warnings) == 1
    assert "overlaps" in warnings[0]
    root = ET.fromstring(xml_string)
    video_tracks = root.findall("sequence/media/video/track")
    assert len(video_tracks) == 3  # main + 2 broll lanes, one clip each
    assert len(video_tracks[1].findall("clipitem")) == 1
    assert len(video_tracks[2].findall("clipitem")) == 1


def test_silent_broll_gets_no_audio_track():
    main = [_seg(0, out_s=10.0)]
    broll = [{**_seg(0, source_path="/media/br1.mov", out_s=1.0),
              "timeline_start_seconds": 0.0, "audio_mode": "silent"}]
    xml_string, _ = build_premiere_xml("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    root = ET.fromstring(xml_string)
    audio_tracks = root.findall("sequence/media/audio/track")
    assert len(audio_tracks) == 2  # only the main clip's L/R, no broll audio


def test_full_audio_broll_gets_its_own_stereo_track_pair():
    main = [_seg(0, out_s=10.0)]
    broll = [{**_seg(0, source_path="/media/br1.mov", out_s=1.0),
              "timeline_start_seconds": 0.0, "audio_mode": "full"}]
    xml_string, _ = build_premiere_xml("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    root = ET.fromstring(xml_string)
    audio_tracks = root.findall("sequence/media/audio/track")
    assert len(audio_tracks) == 4  # main L/R + broll L/R


def test_duck_main_applies_audio_levels_filter_to_correct_clip():
    main = [_seg(0, out_s=5.0), _seg(1, out_s=5.0)]
    xml_string, _ = build_premiere_xml(
        "Seq", fps=25.0, resolved_segments=main, main_duck_db={1: -12.0},
    )
    root = ET.fromstring(xml_string)
    audio_clips = root.findall("sequence/media/audio/track/clipitem")
    # main[0] (order 0) has 2 audio clips (L/R) with no filter; main[1] (order 1) has 2 with a filter.
    ducked = [c for c in audio_clips if c.find("filter") is not None]
    assert len(ducked) == 2
    for clip in ducked:
        value = clip.find("filter/effect/parameter/value").text
        assert float(value) == pytest.approx(10 ** (-12.0 / 20.0), rel=1e-3)
