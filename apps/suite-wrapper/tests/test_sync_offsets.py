"""A-Sync integration (addendum v3/v4): the .sync-offsets.json sidecar
save/load round-trip, including v4's per-track enabled/channels routing
fields."""

import os

from backend import paths


def test_sync_load_offsets_for_missing_sidecar_is_found_false(api, tmp_path):
    missing_video = str(tmp_path / "no-such-video.mp4")
    assert not os.path.exists(missing_video + ".sync-offsets.json")
    offsets = api.sync_load_offsets(missing_video)
    assert offsets.get("ok") is True and offsets.get("found") is False, \
        f"sync_load_offsets (missing): {offsets}"


def test_sync_save_then_load_offsets_round_trip(api, tmp_path):
    tmp_video = str(tmp_path / "clip.mp4")
    saved = api.sync_save_offsets(
        tmp_video,
        [{"path": "/ext/track one.wav", "offset_seconds": 1.234}],
        method="waveform")
    assert saved.get("ok"), f"sync_save_offsets: {saved}"
    assert saved["path"] == tmp_video + ".sync-offsets.json"
    loaded = api.sync_load_offsets(tmp_video)
    assert loaded.get("found") is True, f"sync_load_offsets: {loaded}"
    assert loaded["method"] == "waveform"
    assert loaded["tracks"] == [{"path": "/ext/track one.wav", "offset_seconds": 1.234}]


def test_sync_save_then_load_offsets_round_trip_for_a_braw_video(monkeypatch, api, tmp_path):
    """Addendum v55: a .braw video_path's sidecar is centralized under
    paths.IVT_CACHE_DIR instead of next to the (routinely read-only)
    source -- confirms save/load still round-trip correctly through the
    new location, purely a path redirection, no real BRAW SDK needed."""
    monkeypatch.setattr(paths, "IVT_CACHE_DIR", str(tmp_path / "ivt_cache"))
    braw_video = str(tmp_path / "clip.braw")

    saved = api.sync_save_offsets(
        braw_video,
        [{"path": "/ext/track one.wav", "offset_seconds": 1.234}],
        method="waveform")
    assert saved.get("ok"), f"sync_save_offsets: {saved}"
    assert saved["path"] != braw_video + ".sync-offsets.json"
    assert os.path.dirname(saved["path"]) == str(tmp_path / "ivt_cache")
    assert os.path.isfile(saved["path"])

    loaded = api.sync_load_offsets(braw_video)
    assert loaded.get("found") is True, f"sync_load_offsets: {loaded}"
    assert loaded["method"] == "waveform"
    assert loaded["tracks"] == [{"path": "/ext/track one.wav", "offset_seconds": 1.234}]


def test_sync_preview_url_gates_on_real_file_and_extension(api, tmp_path):
    # (addendum v4) url_for never reads the bytes -- the gate is real-file
    # + extension.
    wav_path = str(tmp_path / "take one.wav")
    mp4_path = str(tmp_path / "cam.mp4")
    txt_path = str(tmp_path / "notes.txt")
    for p in (wav_path, mp4_path, txt_path):
        open(p, "wb").close()

    pv_wav = api.sync_preview_url(wav_path)
    assert pv_wav.get("ok") and pv_wav.get("url"), f"sync_preview_url wav: {pv_wav}"
    pv_mp4 = api.sync_preview_url(mp4_path)
    assert pv_mp4.get("ok") and pv_mp4.get("url"), f"sync_preview_url mp4: {pv_mp4}"
    pv_txt = api.sync_preview_url(txt_path)
    assert pv_txt.get("ok") is False, f"sync_preview_url txt: {pv_txt}"
    pv_missing = api.sync_preview_url(str(tmp_path / "nope.wav"))
    assert pv_missing.get("ok") is False, f"sync_preview_url missing: {pv_missing}"


def test_sync_offsets_round_trip_preserves_routing_fields(api, tmp_path):
    # (addendum v4) enabled/channels routing fields must survive the
    # sidecar round-trip intact.
    v4_video = str(tmp_path / "clip.mp4")
    saved = api.sync_save_offsets(
        v4_video,
        [{"path": "/ext/lav.wav", "offset_seconds": 0.5,
          "enabled": False, "channels": [1]}],
        method="waveform")
    assert saved.get("ok"), f"sync_save_offsets v4: {saved}"
    loaded = api.sync_load_offsets(v4_video)
    assert loaded["tracks"] == [
        {"path": "/ext/lav.wav", "offset_seconds": 0.5,
         "enabled": False, "channels": [1]}], \
        f"routing round-trip: {loaded['tracks']}"
