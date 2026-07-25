"""suite_api.py's Edit-workspace BRAW substitution overrides (Phase 2,
remainder): get_thumbnail/get_preview_url/export_video_preview swap a
linked .braw source's media_paths entry for its cached proxy for the
duration of RCS's own real call, then restore the original path
regardless of outcome; link_media_file/batch_relink_media eagerly queue
proxy generation for any newly-linked .braw file.

Uses the real `api` fixture (real SuiteApi, real RCS Api reached via
MRO/super()) so RCS's actual get_thumbnail/get_preview_url/
export_video_preview/link_media_file/batch_relink_media logic runs
unmodified -- only braw_bridge and the actual decode/dialog calls are
mocked, so this stays a fast, no-real-media/no-real-window unit test."""

import os

import sources

from backend import suite_api


def _link_source(api, source_id, media_path):
    api.sources[source_id] = {"path": media_path + ".vtt", "segments": []}
    api.media_paths[source_id] = media_path


class _FakeWindow:
    """Minimal stand-in for pywebview's window -- just enough for
    create_file_dialog to return a scripted result, so link_media_file/
    batch_relink_media's own dialog-then-link logic runs for real."""

    def __init__(self, dialog_result):
        self._dialog_result = dialog_result

    def create_file_dialog(self, *args, **kwargs):
        return self._dialog_result


# ---------- get_thumbnail ----------

def test_get_thumbnail_passes_through_for_non_braw_source(monkeypatch, api, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")
    _link_source(api, "src1", str(clip))

    def fail_if_called(*a, **k):
        raise AssertionError("find_cached_proxy must not be called for a non-.braw source")
    monkeypatch.setattr(suite_api.braw_bridge, "find_cached_proxy", fail_if_called)
    monkeypatch.setattr(sources, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(sources, "extract_thumbnail_data_uri", lambda path, t: "data:fake")

    res = api.get_thumbnail("src1", 0.0)
    assert res == {"ok": True, "data_uri": "data:fake"}
    assert api.media_paths["src1"] == str(clip)


def test_get_thumbnail_reports_not_ready_without_cached_proxy(monkeypatch, api, tmp_path):
    clip = tmp_path / "clip.braw"
    clip.write_bytes(b"\x00")
    _link_source(api, "src1", str(clip))
    monkeypatch.setattr(suite_api.braw_bridge, "find_cached_proxy", lambda p: None)

    res = api.get_thumbnail("src1", 0.0)
    assert res == {"ok": False, "error":
                    "This BRAW clip's proxy hasn't finished generating yet — "
                    "check the Jobs drawer."}
    assert api.media_paths["src1"] == str(clip), "must be left untouched"


def test_get_thumbnail_substitutes_proxy_and_restores_afterward(monkeypatch, api, tmp_path):
    clip = tmp_path / "clip.braw"
    clip.write_bytes(b"\x00")
    proxy = tmp_path / "proxy.mov"
    proxy.write_bytes(b"\x00")
    _link_source(api, "src1", str(clip))
    monkeypatch.setattr(suite_api.braw_bridge, "find_cached_proxy", lambda p: str(proxy))
    monkeypatch.setattr(sources, "ffmpeg_available", lambda: True)

    seen_paths = []

    def fake_extract(path, seconds):
        seen_paths.append(path)
        assert api.media_paths["src1"] == str(proxy), "RCS's own call must see the PROXY path"
        return "data:fake"
    monkeypatch.setattr(sources, "extract_thumbnail_data_uri", fake_extract)

    res = api.get_thumbnail("src1", 0.0)
    assert res == {"ok": True, "data_uri": "data:fake"}
    assert seen_paths == [str(proxy)]
    assert api.media_paths["src1"] == str(clip), "must be restored after the call"


def test_get_thumbnail_restores_even_when_the_inner_call_raises(monkeypatch, api, tmp_path):
    clip = tmp_path / "clip.braw"
    clip.write_bytes(b"\x00")
    proxy = tmp_path / "proxy.mov"
    proxy.write_bytes(b"\x00")
    _link_source(api, "src1", str(clip))
    monkeypatch.setattr(suite_api.braw_bridge, "find_cached_proxy", lambda p: str(proxy))
    monkeypatch.setattr(sources, "ffmpeg_available", lambda: True)

    def boom(path, seconds):
        raise RuntimeError("simulated ffmpeg crash")
    monkeypatch.setattr(sources, "extract_thumbnail_data_uri", boom)

    try:
        api.get_thumbnail("src1", 0.0)
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert api.media_paths["src1"] == str(clip), "must be restored even after an exception"


# ---------- get_preview_url ----------

def test_get_preview_url_substitutes_proxy_and_restores_afterward(monkeypatch, api, tmp_path):
    clip = tmp_path / "clip.braw"
    clip.write_bytes(b"\x00")
    proxy = tmp_path / "proxy.mov"
    proxy.write_bytes(b"\x00")
    _link_source(api, "src1", str(clip))
    monkeypatch.setattr(suite_api.braw_bridge, "find_cached_proxy", lambda p: str(proxy))

    seen_paths = []

    def fake_url_for(path):
        seen_paths.append(path)
        return f"http://127.0.0.1:0/fake/{os.path.basename(path)}"
    monkeypatch.setattr(api.preview_server, "url_for", fake_url_for)

    res = api.get_preview_url("src1")
    assert res["ok"], res
    assert seen_paths == [str(proxy)]
    assert api.media_paths["src1"] == str(clip)


def test_get_preview_url_reports_not_ready_without_cached_proxy(monkeypatch, api, tmp_path):
    clip = tmp_path / "clip.braw"
    clip.write_bytes(b"\x00")
    _link_source(api, "src1", str(clip))
    monkeypatch.setattr(suite_api.braw_bridge, "find_cached_proxy", lambda p: None)

    res = api.get_preview_url("src1")
    assert res == {"ok": False, "error":
                    "This BRAW clip's proxy hasn't finished generating yet — "
                    "check the Jobs drawer."}


# ---------- export_video_preview ----------

def test_export_video_preview_reports_not_ready_without_cached_proxy(monkeypatch, api, tmp_path):
    clip = tmp_path / "clip.braw"
    clip.write_bytes(b"\x00")
    _link_source(api, "src1", str(clip))
    monkeypatch.setattr(suite_api.braw_bridge, "find_cached_proxy", lambda p: None)

    res = api.export_video_preview()
    assert res == {"ok": False, "error":
                    "One or more BRAW clips' proxies haven't finished "
                    "generating yet — check the Jobs drawer."}


def test_export_video_preview_restores_media_paths_even_when_super_fails_early(
        monkeypatch, api, tmp_path):
    """No script has been generated (api.last_result is None), so RCS's
    own export_video_preview fails immediately with its own error -- this
    proves the swap/restore around super() happens (and is undone) no
    matter how the wrapped call turns out, without needing to build a
    full fake project + window + ffmpeg export chain."""
    clip = tmp_path / "clip.braw"
    clip.write_bytes(b"\x00")
    proxy = tmp_path / "proxy.mov"
    proxy.write_bytes(b"\x00")
    _link_source(api, "src1", str(clip))
    monkeypatch.setattr(suite_api.braw_bridge, "find_cached_proxy", lambda p: str(proxy))
    assert api.last_result is None

    res = api.export_video_preview()
    assert res == {"ok": False, "error": "Generate a script first."}
    assert api.media_paths["src1"] == str(clip), "must be restored even though super() failed"


# ---------- link_media_file / batch_relink_media (eager proxy queueing) ----------

def test_link_media_file_queues_proxy_for_a_braw_result(monkeypatch, api, tmp_path):
    braw_path = tmp_path / "clip.braw"
    braw_path.write_bytes(b"\x00")
    api.sources["src1"] = {"path": "fake.vtt", "segments": []}
    api.window = _FakeWindow([str(braw_path)])

    queued = []
    monkeypatch.setattr(suite_api.braw_bridge, "queue_missing_proxies",
                        lambda job_manager, paths: queued.append(list(paths)) or [])

    res = api.link_media_file("src1")
    assert res["ok"], res
    assert res["media_path"] == str(braw_path)
    assert queued == [[str(braw_path)]]


def test_link_media_file_does_not_queue_for_a_non_braw_result(api, tmp_path):
    """Uses the REAL queue_missing_proxies (not mocked) -- it already
    no-ops for a non-.braw path internally (its own documented contract),
    so linking an ordinary video must complete with zero new jobs."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")
    api.sources["src1"] = {"path": "fake.vtt", "segments": []}
    api.window = _FakeWindow([str(clip)])

    res = api.link_media_file("src1")
    assert res["ok"], res
    assert api.jobs.list_jobs() == []


def test_batch_relink_media_queues_proxies_for_matched_braw_files(monkeypatch, api, tmp_path):
    folder = tmp_path / "footage"
    folder.mkdir()
    braw_path = folder / "src1.braw"
    braw_path.write_bytes(b"\x00")

    api.sources["src1"] = {"path": "fake.vtt", "segments": []}
    api.window = _FakeWindow(str(folder))

    queued = []
    monkeypatch.setattr(suite_api.braw_bridge, "queue_missing_proxies",
                        lambda job_manager, paths: queued.append(sorted(paths)) or [])

    res = api.batch_relink_media()
    assert res["ok"], res
    assert res["linked_count"] == 1
    assert queued == [[str(braw_path)]]
