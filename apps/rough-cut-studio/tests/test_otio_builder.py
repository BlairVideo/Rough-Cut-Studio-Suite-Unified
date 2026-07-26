"""
tests/test_otio_builder.py

Unit tests for otio_builder.py's build_otio (hand-built OpenTimelineIO
JSON). OTIO tracks are strictly sequential -- a clip's position is
implied by cumulative duration, not an absolute timestamp -- so placing
B-roll at an explicit point requires inserting a Gap of the right size;
that's the main thing under test here, plus the same overlapping-B-roll
lane assignment used by the other two exporters and the "at least one
main cut" contract.
"""

import json

import pytest

from otio_builder import build_otio


def _seg(order, source_path="/media/a.mov", in_s=0.0, out_s=2.0, name="A"):
    return {
        "order": order,
        "source_path": source_path,
        "source_name": name,
        "in_seconds": in_s,
        "out_seconds": out_s,
    }


def _stack(otio_json):
    return json.loads(otio_json)["tracks"]


def test_requires_at_least_one_main_cut():
    with pytest.raises(ValueError):
        build_otio("Seq", fps=25.0, resolved_segments=[])


def test_basic_structure_has_v1_and_a1_tracks():
    otio_json, warnings = build_otio("My Sequence", fps=25.0, resolved_segments=[_seg(0, out_s=4.0)])
    assert warnings == []
    doc = json.loads(otio_json)
    assert doc["OTIO_SCHEMA"] == "Timeline.1"
    assert doc["name"] == "My Sequence"
    track_names = [t["name"] for t in doc["tracks"]["children"]]
    assert track_names == ["V1", "A1"]
    v1 = doc["tracks"]["children"][0]
    assert len(v1["children"]) == 1
    clip = v1["children"][0]
    assert clip["OTIO_SCHEMA"] == "Clip.1"
    assert clip["source_range"]["duration"]["value"] == 100  # 4s @ 25fps


def test_a1_track_mirrors_v1_clips():
    segs = [_seg(0, out_s=2.0), _seg(1, out_s=3.0)]
    otio_json, _ = build_otio("Seq", fps=25.0, resolved_segments=segs)
    doc = json.loads(otio_json)
    v1_clips = doc["tracks"]["children"][0]["children"]
    a1_clips = doc["tracks"]["children"][-1]["children"]
    assert len(v1_clips) == len(a1_clips) == 2
    for v_clip, a_clip in zip(v1_clips, a1_clips):
        assert v_clip["source_range"] == a_clip["source_range"]
        assert v_clip["media_reference"]["target_url"] == a_clip["media_reference"]["target_url"]


def test_broll_offset_inserts_a_gap_of_the_right_size():
    # B-roll starting at 3s on an otherwise-empty V2 lane needs a leading
    # Gap of exactly 3s (75 frames @ 25fps) to land at the right spot,
    # since OTIO tracks have no absolute-position field.
    main = [_seg(0, out_s=10.0)]
    broll = [{**_seg(0, source_path="/media/br.mov", out_s=1.0), "timeline_start_seconds": 3.0}]
    otio_json, warnings = build_otio("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    assert warnings == []
    doc = json.loads(otio_json)
    v2 = next(t for t in doc["tracks"]["children"] if t["name"] == "V2")
    assert len(v2["children"]) == 2
    gap, clip = v2["children"]
    assert gap["OTIO_SCHEMA"] == "Gap.1"
    assert gap["source_range"]["duration"]["value"] == 75
    assert clip["OTIO_SCHEMA"] == "Clip.1"


def test_broll_at_zero_offset_has_no_leading_gap():
    main = [_seg(0, out_s=10.0)]
    broll = [{**_seg(0, source_path="/media/br.mov", out_s=1.0), "timeline_start_seconds": 0.0}]
    otio_json, _ = build_otio("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    doc = json.loads(otio_json)
    v2 = next(t for t in doc["tracks"]["children"] if t["name"] == "V2")
    assert len(v2["children"]) == 1
    assert v2["children"][0]["OTIO_SCHEMA"] == "Clip.1"


def test_overlapping_broll_spreads_across_additional_tracks_with_warning():
    main = [_seg(0, out_s=10.0)]
    broll = [
        {**_seg(0, source_path="/media/br1.mov", out_s=3.0), "timeline_start_seconds": 0.0},
        {**_seg(0, source_path="/media/br2.mov", out_s=3.0), "timeline_start_seconds": 1.0},
    ]
    otio_json, warnings = build_otio("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    assert len(warnings) == 1
    doc = json.loads(otio_json)
    track_names = [t["name"] for t in doc["tracks"]["children"]]
    assert "V2" in track_names and "V3" in track_names


def test_duck_main_metadata_only_recorded_for_duck_main_mode():
    main = [_seg(0, out_s=10.0)]
    broll = [{**_seg(0, source_path="/media/br.mov", out_s=1.0),
              "timeline_start_seconds": 0.0, "audio_mode": "duck_main", "duck_db": -6.0}]
    otio_json, _ = build_otio("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    doc = json.loads(otio_json)
    v2 = next(t for t in doc["tracks"]["children"] if t["name"] == "V2")
    clip = v2["children"][0]
    assert clip["metadata"]["rough_cut_studio"]["audio_mode"] == "duck_main"
    assert clip["metadata"]["rough_cut_studio"]["duck_db"] == -6.0


def test_full_audio_mode_does_not_record_duck_db():
    main = [_seg(0, out_s=10.0)]
    broll = [{**_seg(0, source_path="/media/br.mov", out_s=1.0),
              "timeline_start_seconds": 0.0, "audio_mode": "full", "duck_db": -6.0}]
    otio_json, _ = build_otio("Seq", fps=25.0, resolved_segments=main, broll_segments=broll)
    doc = json.loads(otio_json)
    v2 = next(t for t in doc["tracks"]["children"] if t["name"] == "V2")
    clip = v2["children"][0]
    assert "duck_db" not in clip["metadata"]["rough_cut_studio"]


def test_clips_ordered_by_segment_order_not_input_order():
    segs = [_seg(1, out_s=3.0, name="Second"), _seg(0, out_s=2.0, name="First")]
    otio_json, _ = build_otio("Seq", fps=25.0, resolved_segments=segs)
    doc = json.loads(otio_json)
    v1_clips = doc["tracks"]["children"][0]["children"]
    assert [c["name"] for c in v1_clips] == ["First", "Second"]
