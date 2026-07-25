"""sync_xml.build_sync_xml -- pure XMEML generation from synthetic probe
dicts (no real ffmpeg/ffprobe needed). Covers addendum v3 (basic
positive/negative-offset tracks, camera audio, per-file channelcount
rules) and v4 (enabled/channels routing: omission, single-channel
selection, downmix)."""

import xml.etree.ElementTree as ET
from urllib.parse import unquote

import pytest

from backend import sync_xml

V_PROBE = {"duration": 10.0, "fps": 25.0, "width": 1280, "height": 720,
           "has_video": True, "has_audio": True, "audio_channels": 2,
           "audio_samplerate": 48000, "audio_bits": 16}
PLUS_PROBE = {"duration": 10.0, "has_video": False, "has_audio": True,
              "audio_channels": 2, "audio_samplerate": 48000, "audio_bits": 24}
MINUS_PROBE = {"duration": 12.0, "has_video": False, "has_audio": True,
               "audio_channels": 1, "audio_samplerate": 48000, "audio_bits": 24}
STEREO_PROBE = {"duration": 10.0, "has_video": False, "has_audio": True,
                "audio_channels": 2, "audio_samplerate": 48000, "audio_bits": 24}


@pytest.fixture
def basic_xml():
    xml_string, warnings = sync_xml.build_sync_xml(
        {"path": "/media/selftest video.mp4", "probe": V_PROBE},
        [{"path": "/media/ext plus.wav", "offset_seconds": 1.6, "probe": PLUS_PROBE},
         {"path": "/media/ext_minus.wav", "offset_seconds": -2.0, "probe": MINUS_PROBE}],
        include_camera_audio=True, sequence_name="Selftest Sync")
    assert warnings == [], f"sync_xml warnings: {warnings}"
    return ET.fromstring(xml_string)


def test_one_video_clipitem(basic_xml):
    video_items = basic_xml.findall(".//video/track/clipitem")
    assert len(video_items) == 1, f"video clipitems: {len(video_items)}"


def test_five_audio_tracks_one_clipitem_each(basic_xml):
    # 2 camera channels + 2 "ext plus" channels + 1 "ext_minus" channel,
    # each on its OWN track with exactly one clipitem.
    audio_tracks = basic_xml.findall(".//audio/track")
    assert len(audio_tracks) == 5, f"audio tracks: {len(audio_tracks)}"
    assert all(len(t.findall("clipitem")) == 1 for t in audio_tracks)


def test_positive_and_negative_offsets_produce_correct_frames(basic_xml):
    # Positive offset (+1.6 s @ 25 fps): start=40, in=0. Negative (-2.0 s):
    # start=0, in=50 (head trimmed).
    by_name = {}
    for item in basic_xml.findall(".//audio/track/clipitem"):
        by_name.setdefault(item.findtext("name"), []).append(item)

    plus_items = by_name["ext plus.wav"]
    assert len(plus_items) == 2
    for item in plus_items:
        assert item.findtext("start") == "40" and item.findtext("in") == "0", \
            f"positive-offset frames: start={item.findtext('start')} in={item.findtext('in')}"

    minus_items = by_name["ext_minus.wav"]
    assert len(minus_items) == 1
    assert minus_items[0].findtext("start") == "0" and minus_items[0].findtext("in") == "50", \
        (f"negative-offset frames: start={minus_items[0].findtext('start')} "
         f"in={minus_items[0].findtext('in')}")


def test_clipitems_never_carry_their_own_channelcount(basic_xml):
    # A <clipitem> (the timeline item) must NEVER carry its own
    # channelcount -- THAT'S what Premiere silently imports as mono;
    # channels are split per-track/per-clipitem instead.
    assert all(clip.find("channelcount") is None for clip in basic_xml.iter("clipitem")), \
        "a clipitem illegally carries its own <channelcount>"


def test_source_file_channelcount_is_present_and_correct(basic_xml):
    # The source <file>'s own <media><audio><channelcount> IS required
    # (Premiere needs it to resolve each clipitem's
    # <sourcetrack><trackindex>) and legitimately says "2" for the stereo
    # "ext plus.wav".
    file_channelcounts = {f.findtext("name"): f.findtext("media/audio/channelcount")
                          for f in basic_xml.iter("file") if f.find("name") is not None}
    assert file_channelcounts.get("ext plus.wav") == "2", \
        f"stereo file's own channelcount missing/wrong: {file_channelcounts}"


def test_pathurls_are_distinct_per_source(basic_xml):
    pathurls = {unquote(el.text) for el in basic_xml.iter("pathurl")}
    assert len(pathurls) == 3, f"pathurls: {pathurls}"


def test_track_pushed_fully_out_of_range_is_dropped_with_a_warning():
    _, drop_warnings = sync_xml.build_sync_xml(
        {"path": "/media/selftest video.mp4", "probe": V_PROBE},
        [{"path": "/media/way early.wav", "offset_seconds": -30.0, "probe": MINUS_PROBE}],
        include_camera_audio=False, sequence_name="Selftest Sync 2")
    assert len(drop_warnings) == 1 and "way early.wav" in drop_warnings[0], \
        f"drop warnings: {drop_warnings}"


def test_disabled_track_is_entirely_absent_from_the_xml():
    # (addendum v4a)
    xml_dis, _ = sync_xml.build_sync_xml(
        {"path": "/media/selftest video.mp4", "probe": V_PROBE},
        [{"path": "/media/keep.wav", "offset_seconds": 0.0, "probe": STEREO_PROBE},
         {"path": "/media/drop.wav", "offset_seconds": 0.0, "probe": STEREO_PROBE,
          "enabled": False}],
        include_camera_audio=False, sequence_name="v4 disabled")
    root_dis = ET.fromstring(xml_dis)
    names_dis = {f.findtext("name") for f in root_dis.iter("file")}
    assert "keep.wav" in names_dis and "drop.wav" not in names_dis, \
        f"disabled track not omitted: {names_dis}"


def test_channel_selection_produces_one_clipitem_with_sourcetrack():
    # (addendum v4b) a stereo track with channels=[1] -> exactly ONE audio
    # clipitem, <sourcetrack><trackindex>1, and the file's <channelcount>=2.
    xml_one, _ = sync_xml.build_sync_xml(
        {"path": "/media/selftest video.mp4", "probe": V_PROBE},
        [{"path": "/media/stereo.wav", "offset_seconds": 0.0, "probe": STEREO_PROBE,
          "channels": [1]}],
        include_camera_audio=False, sequence_name="v4 one channel")
    root_one = ET.fromstring(xml_one)
    clips_one = root_one.findall(".//audio/track/clipitem")
    assert len(clips_one) == 1, f"channels=[1] clip count: {len(clips_one)}"
    st_idx = clips_one[0].findtext("sourcetrack/trackindex")
    assert st_idx == "1", f"channels=[1] sourcetrack: {st_idx}"
    cc_one = root_one.find(".//file/media/audio/channelcount")
    assert cc_one is not None and cc_one.text == "2", \
        f"stereo file channelcount not preserved: {cc_one}"


def test_downmix_channel_selection_has_no_sourcetrack():
    # (addendum v4c) channels=[0] (downmix) -> one clipitem with NO
    # <sourcetrack>.
    xml_dm, _ = sync_xml.build_sync_xml(
        {"path": "/media/selftest video.mp4", "probe": V_PROBE},
        [{"path": "/media/stereo.wav", "offset_seconds": 0.0, "probe": STEREO_PROBE,
          "channels": [0]}],
        include_camera_audio=False, sequence_name="v4 downmix")
    root_dm = ET.fromstring(xml_dm)
    clips_dm = root_dm.findall(".//audio/track/clipitem")
    assert len(clips_dm) == 1, f"downmix clip count: {len(clips_dm)}"
    assert clips_dm[0].find("sourcetrack") is None, \
        "downmix clipitem must have no <sourcetrack>"
    cc_dm = root_dm.find(".//file/media/audio/channelcount")
    assert cc_dm is not None and cc_dm.text == "2", \
        f"downmix file channelcount not preserved: {cc_dm}"
