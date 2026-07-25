"""Transcribe-workspace API surface: model list and cache loading for a
video that has no cache yet."""

import os
import sys

from backend import api_transcriber


def test_transcriber_models_lists_four_with_medium_default(api):
    models = api.transcriber_models()
    assert models.get("ok") and len(models["models"]) == 4, f"transcriber_models: {models}"
    assert models["default_label"] == "Recommended (medium)"


def test_transcriber_load_cache_for_missing_video_is_found_false(api, tmp_path):
    missing_video = str(tmp_path / "no-such-video.mp4")
    assert not os.path.exists(missing_video)
    loaded = api.transcriber_load_cache(missing_video)
    assert loaded.get("ok") is True and loaded.get("found") is False, \
        f"transcriber_load_cache: {loaded}"


def test_transcriber_start_queues_missing_braw_proxies(monkeypatch, api, tmp_path):
    """Mirrors api_broll.py's broll_start: BRAW proxy generation must be
    kicked off (fire-and-forget) alongside the per-file transcribe jobs,
    not left until the worker itself first asks for a decode path — see
    braw_bridge.wait_for_decode_path's own docstring for why the race
    matters. Mocks IVT_PYTHON/start_subprocess_job so this stays a fast
    unit test with no real transcriber subprocess involved."""
    braw_clip = tmp_path / "clip.braw"
    braw_clip.write_bytes(b"\x00")
    plain_clip = tmp_path / "clip.mp4"
    plain_clip.write_bytes(b"\x00")

    monkeypatch.setattr(api_transcriber.paths, "IVT_PYTHON", sys.executable)
    monkeypatch.setattr(api.jobs, "start_subprocess_job",
                        lambda **kwargs: "fake-job-id")

    queued_with = []

    def fake_queue_missing_proxies(job_manager, paths_arg):
        queued_with.append(list(paths_arg))
        return []

    monkeypatch.setattr(api_transcriber.braw_bridge, "queue_missing_proxies",
                        fake_queue_missing_proxies)

    result = api.transcriber_start(
        [str(braw_clip), str(plain_clip)], "Recommended (medium)", False)

    assert result.get("ok"), result
    assert len(result["job_ids"]) == 2
    assert queued_with == [[str(braw_clip), str(plain_clip)]]
