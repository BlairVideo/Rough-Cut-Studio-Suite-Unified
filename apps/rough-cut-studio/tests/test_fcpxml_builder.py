"""
tests/test_fcpxml_builder.py

Unit tests for fcpxml_builder.py's build_fcpxml (FCPXML 1.11 for Final
Cut Pro / DaVinci Resolve): rational-time formatting, one <asset> per
unique source file, B-roll anchored as connected clips with an offset
relative to their HOST clip (not the timeline) as the module's own
docstring warns is easy to get backwards, lane assignment for
overlapping B-roll, and the two host-anchoring edge cases (B-roll
starting before the first / after the last main clip).
"""

import xml.etree.ElementTree as ET

import pytest

from fcpxml_builder import build_fcpxml


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


def test_requires_at_least_one_main_cut():
    with pytest.raises(ValueError):
        build_fcpxml("Seq", fps=25.0, resolved_segments=[])


def test_single_clip_basic_structure():
    xml_string, warnings = build_fcpxml("My Sequence", fps=25.0, resolved_segments=[_seg(0, out_s=4.0)])
    assert warnings == []
    root = ET.fromstring(xml_string)
    assert root.tag == "fcpxml"
    project = root.find("library/event/project")
    assert project.get("name") == "My Sequence"
    sequence = project.find("sequence")
    # 4 seconds at 25fps = 100 frames * (100/2500)s/frame = 400/2500s, reduced by gcd(400,2500)=100
    assert sequence.get("duration") == "4/1s"


def test_one_asset_per_unique_source_file():
    segs = [
        _seg(0, source_path="/media/a.mov", out_s=2.0),
        _seg(1, source_path="/media/a.mov", in_s=5.0, out_s=7.0),
        _seg(2, source_path="/media/b.mov", out_s=1.0),
    ]
    xml_string, _ = build_fcpxml("Seq", fps=25.0, resolved_segments=segs)
    root = ET.fromstring(xml_string)
    assets = root.findall("resources/asset")
    assert len(assets) == 2
    clips = root.findall("library/event/project/sequence/spine/asset-clip")
    assert len(clips) == 3
    # both a.mov clips reference the same asset id
    assert clips[0].get("ref") == clips[1].get("ref")
    assert clips[0].get("ref") != clips[2].get("ref")


def test_broll_offset_is_relative_to_host_clip_not_timeline():
    # Main clip 0: [0, 5)s. Main clip 1: [5, 10)s. B-roll starts at 7s,
    # so it should be anchored to main clip 1 (the host) with an offset
    # of 7 - 5 = 2s -- NOT an absolute-timeline offset of 7s. Getting
    # this backwards is exactly the bug this module's docstring warns
    # about (every overlay would silently land in the wrong place).
    main = [_seg(0, source_path="/media/a.mov", out_s=5.0), _seg(1, source_path="/media/a.mov", out_s=5.0)]
    broll = [{**_seg(0, source_path="/media/br.mov", out_s=1.0), "timeline_start_seconds": 7.0}]
    xml_string, warnings = build_fcpxml("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    assert warnings == []
    root = ET.fromstring(xml_string)
    main_clips = root.findall("library/event/project/sequence/spine/asset-clip")
    host_clip = main_clips[1]
    broll_clip = host_clip.find("asset-clip")
    assert broll_clip is not None
    assert broll_clip.get("offset") == "2/1s"
    assert broll_clip.get("lane") == "1"


def test_broll_before_first_clip_anchors_to_first_with_warning():
    main = [_seg(0, out_s=5.0)]
    broll = [{**_seg(0, source_path="/media/br.mov", out_s=1.0), "timeline_start_seconds": -3.0}]
    xml_string, warnings = build_fcpxml("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    assert len(warnings) == 1
    assert "before the first main" in warnings[0]
    root = ET.fromstring(xml_string)
    host_clip = root.find("library/event/project/sequence/spine/asset-clip")
    broll_clip = host_clip.find("asset-clip")
    assert broll_clip.get("offset") == "0s"


def test_broll_after_last_clip_anchors_to_last_with_warning():
    main = [_seg(0, out_s=5.0)]
    broll = [{**_seg(0, source_path="/media/br.mov", out_s=1.0), "timeline_start_seconds": 20.0}]
    xml_string, warnings = build_fcpxml("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    assert len(warnings) == 1
    assert "after the last main" in warnings[0]


def test_overlapping_broll_gets_separate_lanes_and_warning():
    main = [_seg(0, out_s=10.0)]
    broll = [
        {**_seg(0, source_path="/media/br1.mov", out_s=3.0), "timeline_start_seconds": 0.0},
        {**_seg(0, source_path="/media/br2.mov", out_s=3.0), "timeline_start_seconds": 1.0},
    ]
    xml_string, warnings = build_fcpxml("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    assert len(warnings) == 1
    root = ET.fromstring(xml_string)
    host_clip = root.find("library/event/project/sequence/spine/asset-clip")
    lanes = {c.get("lane") for c in host_clip.findall("asset-clip")}
    assert lanes == {"1", "2"}


def test_silent_broll_gets_adjust_volume_muted():
    main = [_seg(0, out_s=10.0)]
    broll = [{**_seg(0, source_path="/media/br.mov", out_s=1.0),
              "timeline_start_seconds": 0.0, "audio_mode": "silent"}]
    xml_string, _ = build_fcpxml("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    root = ET.fromstring(xml_string)
    host_clip = root.find("library/event/project/sequence/spine/asset-clip")
    broll_clip = host_clip.find("asset-clip")
    adjust = broll_clip.find("adjust-volume")
    assert adjust is not None
    assert adjust.get("amount") == "-96dB"


def test_full_audio_broll_has_no_adjust_volume():
    main = [_seg(0, out_s=10.0)]
    broll = [{**_seg(0, source_path="/media/br.mov", out_s=1.0),
              "timeline_start_seconds": 0.0, "audio_mode": "full"}]
    xml_string, _ = build_fcpxml("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    root = ET.fromstring(xml_string)
    host_clip = root.find("library/event/project/sequence/spine/asset-clip")
    broll_clip = host_clip.find("asset-clip")
    assert broll_clip.find("adjust-volume") is None


def test_duck_main_applies_adjust_volume_to_main_clip():
    main = [_seg(0, out_s=5.0), _seg(1, out_s=5.0)]
    xml_string, _ = build_fcpxml("Seq", fps=25.0, resolved_segments=main, main_duck_db={1: -6.0})
    root = ET.fromstring(xml_string)
    clips = root.findall("library/event/project/sequence/spine/asset-clip")
    assert clips[0].find("adjust-volume") is None
    assert clips[1].find("adjust-volume").get("amount") == "-6.0dB"


@pytest.mark.parametrize("fps,expected", [
    (25.0, "1/25s"),
    (24.0, "1/24s"),
    (23.976, "1001/24000s"),
    (29.97, "1001/30000s"),
])
def test_ntsc_and_integer_rates_use_exact_frame_duration_fractions(fps, expected):
    # One-frame clip -> its duration string should equal the exact,
    # reduced per-frame fraction for that rate (NTSC rates use Apple's
    # own non-decimal convention, e.g. 1001/24000s at 23.976).
    xml_string, _ = build_fcpxml("Seq", fps=fps, resolved_segments=[_seg(0, out_s=1.0 / fps)])
    root = ET.fromstring(xml_string)
    clip = root.find("library/event/project/sequence/spine/asset-clip")
    assert clip.get("duration") == expected
