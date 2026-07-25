"""One sidecar per video, not two (addendum v9): .sync-offsets.json and
.ivt-cache.json must never both persist for the same video -- whichever
comes second folds into (or consolidates) the other."""

import json
import os
import sys
from types import SimpleNamespace

import pytest

# transcribe_worker.py redirects stdout to stderr as a MODULE-LEVEL side
# effect (it's designed to run standalone as a worker process's own
# stdout-is-the-protocol-stream setup) -- importing it here for
# write_ivt_cache would otherwise silently corrupt pytest's own output
# capture. Save/restore both the Python sys.stdout object and the real
# fd 1 around the import, once, at module load.
_saved_stdout_obj = sys.stdout
_saved_fd1 = os.dup(1)
try:
    from backend.workers import transcribe_worker
finally:
    sys.stdout = _saved_stdout_obj
    os.dup2(_saved_fd1, 1)
    os.close(_saved_fd1)

from backend import synced_audio_splice


class _FakeApp:
    CACHE_SUFFIX = ".ivt-cache.json"


def make_video(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"\x00" * 16)
    return str(p)


def make_audio(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"")
    return str(p)


def fake_segment(start, end, text, speaker="Jordan"):
    return SimpleNamespace(start=start, end=end, text=text, speaker=speaker,
                            avg_logprob=-0.1, no_speech_prob=0.01)


def sidecar_path(v):
    return v + ".sync-offsets.json"


def cache_path(v):
    return v + ".ivt-cache.json"


def test_sync_only_creates_sidecar_but_no_cache(api, tmp_path):
    v1 = make_video(tmp_path, "sync_only.mp4")
    a1 = make_audio(tmp_path, "sync_only_boom.wav")
    r1 = api.sync_save_offsets(v1, [{"path": a1, "offset_seconds": 0.5}])
    assert r1.get("ok"), r1
    assert os.path.exists(sidecar_path(v1)), "sidecar should exist (sync-only)"
    assert not os.path.exists(cache_path(v1)), "no cache should exist yet (sync-only)"
    loaded1 = api.sync_load_offsets(v1)
    assert loaded1.get("found") and loaded1["tracks"][0]["path"] == a1, loaded1


def test_sync_then_transcribe_folds_sidecar_into_cache(api, tmp_path):
    v1 = make_video(tmp_path, "sync_then_transcribe.mp4")
    a1 = make_audio(tmp_path, "sync_then_transcribe_boom.wav")
    r1 = api.sync_save_offsets(v1, [{"path": a1, "offset_seconds": 0.5}])
    assert r1.get("ok"), r1

    transcribe_worker.write_ivt_cache(
        _FakeApp(), v1, [fake_segment(0.0, 1.0, "Hello")], ["Jordan"])
    assert os.path.exists(cache_path(v1)), "cache should now exist"
    assert not os.path.exists(sidecar_path(v1)), \
        "sidecar should be gone after write_ivt_cache folds it in"
    with open(cache_path(v1), "r", encoding="utf-8") as f:
        cache1 = json.load(f)
    assert cache1.get("sync_tracks") == [{"path": a1, "offset_seconds": 0.5}], cache1
    assert cache1.get("sync_method") == "waveform", cache1

    discovered1 = synced_audio_splice.discover_synced_audios(v1)
    assert len(discovered1) == 1 and discovered1[0]["audio_path"] == a1, discovered1


def test_transcribe_then_sync_updates_cache_directly_no_sidecar(api, tmp_path):
    v2 = make_video(tmp_path, "transcribe_then_sync.mp4")
    a2 = make_audio(tmp_path, "transcribe_then_sync_lav.wav")
    transcribe_worker.write_ivt_cache(
        _FakeApp(), v2, [fake_segment(0.0, 2.0, "Second video")], ["Jordan"])
    assert os.path.exists(cache_path(v2)) and not os.path.exists(sidecar_path(v2))

    r2 = api.sync_save_offsets(v2, [{"path": a2, "offset_seconds": -0.2}])
    assert r2.get("ok"), r2
    assert not os.path.exists(sidecar_path(v2)), \
        "sidecar must never be created when a cache already exists"
    with open(cache_path(v2), "r", encoding="utf-8") as f:
        cache2 = json.load(f)
    assert cache2.get("sync_tracks") == [{"path": a2, "offset_seconds": -0.2}], cache2
    assert cache2.get("segments") and cache2["segments"][0]["text"] == "Second video", \
        "the transcript itself must survive the read-modify-write"
    loaded2 = api.sync_load_offsets(v2)
    assert loaded2.get("found") and loaded2["tracks"][0]["path"] == a2, loaded2


def test_legacy_dual_sidecar_state_consolidates_on_first_load(api, tmp_path):
    # Legacy dual-file state (both already exist, e.g. from before this
    # addendum) -> sync_load_offsets consolidates on first touch.
    v3 = make_video(tmp_path, "legacy_dual.mp4")
    a3 = make_audio(tmp_path, "legacy_dual_field.wav")
    transcribe_worker.write_ivt_cache(
        _FakeApp(), v3, [fake_segment(0.0, 1.5, "Legacy")], ["Jordan"])
    with open(sidecar_path(v3), "w", encoding="utf-8") as f:
        json.dump({"video_path": v3, "method": "timecode",
                   "tracks": [{"path": a3, "offset_seconds": 1.1}],
                   "updated_at": 12345.0}, f)
    assert os.path.exists(cache_path(v3)) and os.path.exists(sidecar_path(v3)), \
        "both files must exist to set up the legacy scenario"

    loaded3 = api.sync_load_offsets(v3)
    assert loaded3.get("found") and loaded3["tracks"][0]["path"] == a3, loaded3
    assert loaded3["method"] == "timecode", loaded3
    assert not os.path.exists(sidecar_path(v3)), \
        "legacy sidecar should be consolidated away on first sync_load_offsets"
    with open(cache_path(v3), "r", encoding="utf-8") as f:
        cache3 = json.load(f)
    assert cache3.get("sync_tracks") == [{"path": a3, "offset_seconds": 1.1}], cache3
    assert cache3.get("segments") and cache3["segments"][0]["text"] == "Legacy", \
        "consolidation must not disturb the existing transcript"

    # Fix verification: transcriber_update_transcript must NOT drop
    # sync_tracks (the bug this addendum found and fixed along the way).
    edited = api.transcriber_update_transcript(
        v3, [{"start": 0.0, "end": 1.5, "text": "Legacy (edited)",
              "speaker": "Jordan", "avg_logprob": -0.1, "no_speech_prob": 0.01}],
        ["Jordan"], {}, [])
    assert edited.get("ok"), edited
    with open(cache_path(v3), "r", encoding="utf-8") as f:
        cache3b = json.load(f)
    assert cache3b["segments"][0]["text"] == "Legacy (edited)", cache3b
    assert cache3b.get("sync_tracks") == [{"path": a3, "offset_seconds": 1.1}], \
        f"transcript edit must preserve sync_tracks: {cache3b}"


def test_plain_video_with_neither_file_is_found_false_with_no_side_effects(api, tmp_path):
    v4 = make_video(tmp_path, "plain.mp4")
    loaded4 = api.sync_load_offsets(v4)
    assert loaded4.get("ok") and loaded4.get("found") is False, loaded4
    assert not os.path.exists(cache_path(v4)) and not os.path.exists(sidecar_path(v4))
