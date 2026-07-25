"""broll_send_to_edit -- checkmarked B-Roll Analyzer segments are sent to
the B-Roll tab (as kind="broll" entries in the shared favorites store)
instead of being inserted into the Cuts table directly. The B-Roll
favorite/star toggle itself was removed; this is the only way a
kind="broll" entry gets created now."""

import os


def make_clip(tmp_path, name="clip.mp4"):
    p = tmp_path / name
    p.write_bytes(b"\x00" * 16)
    return str(p)


def test_send_to_edit_creates_broll_favorites_not_cuts(api, tmp_path):
    clip = make_clip(tmp_path)
    res = api.broll_send_to_edit([{"path": clip, "start": 1.0, "end": 3.0, "score": 82}])
    assert res.get("ok"), res
    assert "cuts" not in res, "should no longer return CutSpecs directly"
    assert len(res["added"]) == 1
    added = res["added"][0]
    assert added["kind"] == "broll"
    assert added["clip_path"] == clip
    assert added["start_seconds"] == 1.0 and added["end_seconds"] == 3.0
    assert added["score"] == 82

    listed = api.suite_list_favorites()
    assert listed.get("ok") and len(listed["favorites"]) == 1
    assert listed["favorites"][0]["kind"] == "broll"


def test_send_to_edit_is_idempotent_for_the_same_segment(api, tmp_path):
    clip = make_clip(tmp_path)
    sel = [{"path": clip, "start": 1.0, "end": 3.0, "score": 50}]
    first = api.broll_send_to_edit(sel)
    assert first.get("ok") and len(first["added"]) == 1

    second = api.broll_send_to_edit(sel)
    assert second.get("ok")
    assert len(second["added"]) == 0, "re-sending the same segment must not create a duplicate"
    assert len(api.suite_list_favorites()["favorites"]) == 1


def test_send_to_edit_multiple_segments_same_clip_shares_one_source(api, tmp_path):
    clip = make_clip(tmp_path)
    res = api.broll_send_to_edit([
        {"path": clip, "start": 0.0, "end": 2.0, "score": 10},
        {"path": clip, "start": 5.0, "end": 7.0, "score": 90},
    ])
    assert res.get("ok"), res
    assert len(res["added"]) == 2
    assert len(res["sources_added"]) == 1, "one clip -> one shared synthetic source, not two"
    assert res["added"][0]["source_id"] == res["added"][1]["source_id"]


def test_sent_segment_can_be_added_to_cuts_as_broll_track(api, tmp_path):
    clip = make_clip(tmp_path)
    sent = api.broll_send_to_edit([{"path": clip, "start": 1.0, "end": 3.0}])
    fav_id = sent["added"][0]["id"]

    cut = api.suite_favorite_add_to_cuts(fav_id)
    assert cut.get("ok"), cut
    assert cut["cut"]["track"] == "broll"


def test_send_to_edit_rejects_missing_file(api, tmp_path):
    missing = str(tmp_path / "nope.mp4")
    res = api.broll_send_to_edit([{"path": missing, "start": 0.0, "end": 1.0}])
    assert res.get("ok") is False
    assert not api.favorites


def test_send_to_edit_rejects_empty_selection(api):
    res = api.broll_send_to_edit([])
    assert res.get("ok") is False
