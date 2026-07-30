"""
tests/test_api_set_fps.py

Regression test for a real bug: changing the fps dropdown updated
Api.fps/SourceManager.fps correctly, but save_xml/save_fcpxml/save_otio
(api.py) just hand out the cached self.last_result["xml"]/["fcpxml"]/
["otio"] string from the last generate()/rebuild_outputs() call -- which
set_fps() never refreshed. So a user who built a cut, then changed fps,
then saved directly (without clicking Apply Changes/Build again) got an
export still baked at the OLD frame rate. set_fps() now re-finalizes the
existing last_result (if any) at the new fps so a subsequent save is
always current -- this exercises that path directly via _finalize_outputs,
without needing a real LLM call.
"""

import re

from api import Api
from transcript_parser import seconds_to_smpte


def _seed_last_result(api, fps=25.0):
    """Bypasses generate()/the LLM entirely -- seeds self.last_result the
    same way _finalize_outputs does, with one linked main cut."""
    api.sources["src1"] = {"path": "/tmp/fake.srt", "segments": []}
    api.media_paths["src1"] = "/tmp/fake_media_that_does_not_exist.mov"
    api.fps = fps
    in_seconds, out_seconds = 0.0, 2.0
    resolved = [{
        "order": 0,
        "track": "main",
        "source_id": "src1",
        "source_name": "src1",
        "in_seconds": in_seconds,
        "out_seconds": out_seconds,
        "in_tc": seconds_to_smpte(in_seconds, fps, False),
        "out_tc": seconds_to_smpte(out_seconds, fps, False),
        "note": "",
    }]
    return api._finalize_outputs("Test Seq", "summary", resolved)


def _timebase(xml_string):
    m = re.search(r"<timebase>(\d+)</timebase>", xml_string)
    return m.group(1) if m else None


def test_set_fps_refreshes_cached_export_when_a_cut_exists():
    api = Api()
    out = _seed_last_result(api, fps=25.0)
    assert out["ok"] is True
    assert _timebase(api.last_result["xml"]) == "25"

    api.set_fps(30)

    assert api.fps == 30.0
    # This is the actual reported bug: save_xml() (api.py) hands out
    # self.last_result["xml"] verbatim -- it must already reflect 30fps
    # here, without any Apply Changes/Build click in between.
    assert _timebase(api.last_result["xml"]) == "30"
    # FCPXML has no <timebase> -- it encodes fps as a frameDuration
    # fraction on the sequence's <format> resource instead.
    assert 'frameDuration="100/3000s"' in api.last_result["fcpxml"]


def test_set_fps_leaves_no_export_alone_when_none_exists_yet():
    # No cut has been built yet -- set_fps must not choke on a None
    # last_result (it did before this fix's `if ... or not self.last_result`
    # guard would have needed adding).
    api = Api()
    res = api.set_fps(30)
    assert res["ok"] is True
    assert api.fps == 30.0
    assert api.last_result is None


def test_set_fps_history_records_the_frame_rate_change():
    api = Api()
    _seed_last_result(api, fps=25.0)
    history_len_before = len(api.history)

    api.set_fps(29.97)

    assert len(api.history) == history_len_before + 1
    assert "29.97" in api.history[-1]["label"]
