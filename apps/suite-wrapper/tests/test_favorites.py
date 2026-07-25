"""Favorites (addendum v6: index-based transcript-line favorites, v7:
range-based favorites for the Cuts row / preview window)."""

import os

import pytest

from backend import favorites as favorites_mod
from transcript_parser import Segment  # RCS_BACKEND_DIR is on sys.path via main.py


@pytest.fixture
def favtest(api, tmp_path):
    """Registers a "favtest" source on `api` backed by a real 2-line VTT
    on disk, with two known Segments, and an empty in-memory favorites
    list. source_id is ALWAYS the VTT's filename stem in real usage
    (RCS's _add_transcript) -- named "favtest" here so the api.sources
    entry and the later re-ingest-from-disk path resolve to the same
    source_id."""
    vtt_path = str(tmp_path / "favtest.vtt")
    with open(vtt_path, "w", encoding="utf-8") as f:
        f.write(
            "WEBVTT\n\n"
            f"NOTE Source video: {tmp_path / 'video.mp4'}\n\n"
            "00:00:00.000 --> 00:00:02.000\nJordan: First line\n\n"
            "00:00:02.000 --> 00:00:04.000\nJordan: Second line\n\n"
        )
    seg0 = Segment(index=0, start_seconds=0.0, end_seconds=2.0,
                    start_tc="00:00:00:00", end_tc="00:00:02:00",
                    speaker="Jordan", text="First line")
    seg1 = Segment(index=1, start_seconds=2.0, end_seconds=4.0,
                    start_tc="00:00:02:00", end_tc="00:00:04:00",
                    speaker="Jordan", text="Second line")
    api.sources["favtest"] = {"path": vtt_path, "segments": [seg0, seg1]}
    api.favorites = []
    return vtt_path


def test_toggle_favorite_persists_and_unpersists(api, favtest):
    toggled_on = api.suite_toggle_favorite("favtest", 0)
    assert toggled_on.get("ok") and toggled_on.get("favorited") is True, toggled_on
    assert len(favorites_mod.load()) == 1, "favorite not persisted"

    toggled_off = api.suite_toggle_favorite("favtest", 0)
    assert toggled_off.get("ok") and toggled_off.get("favorited") is False, toggled_off
    assert favorites_mod.load() == [], "unfavorite not persisted"


def test_favorite_add_to_cuts(api, favtest):
    favor_again = api.suite_toggle_favorite("favtest", 1)
    assert favor_again.get("ok") and favor_again.get("favorited") is True
    listed = api.suite_list_favorites()
    assert listed.get("ok") and len(listed["favorites"]) == 1, listed
    fav_id = listed["favorites"][0]["id"]

    cut_loaded = api.suite_favorite_add_to_cuts(fav_id)
    assert cut_loaded.get("ok"), cut_loaded
    assert cut_loaded["cut"]["track"] == "main", cut_loaded
    assert cut_loaded["cut"]["source_id"] == "favtest", cut_loaded
    assert cut_loaded["cut"]["source_text"] == "Second line", cut_loaded


def test_favorite_add_to_cuts_reingests_from_disk_when_source_dropped(api, favtest):
    favor = api.suite_toggle_favorite("favtest", 1)
    assert favor.get("ok") and favor.get("favorited") is True
    fav_id = api.suite_list_favorites()["favorites"][0]["id"]
    assert api.suite_favorite_add_to_cuts(fav_id).get("ok")

    # Force the lazy re-ingest path: drop the loaded source and confirm
    # add-to-cuts still works by re-parsing the VTT off disk.
    del api.sources["favtest"]
    cut_reingested = api.suite_favorite_add_to_cuts(fav_id)
    assert cut_reingested.get("ok"), cut_reingested
    assert cut_reingested["cut"]["track"] == "main", cut_reingested
    assert cut_reingested["cut"]["source_id"] == "favtest", cut_reingested


def test_range_based_favorite_toggle_and_list(api, favtest):
    # (addendum v7) favorite seg1 by index first, so we can prove a
    # range favorite on a DIFFERENT range doesn't collide with it.
    favor = api.suite_toggle_favorite("favtest", 1)
    assert favor.get("ok") and favor.get("favorited") is True

    # A range matching NEITHER seg0 nor seg1 proves matching is by time
    # range, not index.
    range_on = api.suite_toggle_favorite_range("favtest", 10.0, 12.0, text="Custom clip")
    assert range_on.get("ok") and range_on.get("favorited") is True, range_on
    assert range_on["favorite"]["index"] is None, range_on
    assert range_on["favorite"]["text"] == "Custom clip", range_on

    listed = api.suite_list_favorites()
    assert listed.get("ok") and len(listed["favorites"]) == 2, listed

    range_off = api.suite_toggle_favorite_range("favtest", 10.0, 12.0)
    assert range_off.get("ok") and range_off.get("favorited") is False, range_off
    listed_after = api.suite_list_favorites()
    assert len(listed_after["favorites"]) == 1, listed_after  # only seg1's favorite remains


def test_range_based_favorite_on_unknown_source_fails(api, favtest):
    unknown_source = api.suite_toggle_favorite_range("no-such-source", 0.0, 1.0)
    assert unknown_source.get("ok") is False, unknown_source
