"""Real, end-to-end .braw support in the B-Roll workspace (BRAW
compatibility plan, Phase 2 — suite-side-only substitution). Exercises
the actual compiled braw_proxy_tool, the SDK's real sample.braw clip, and
a real B-Roll Analyzer subprocess (its own venv) — not mocks. Skipped
whenever the SDK/tool or B-Roll Analyzer's own venv isn't present on this
machine, same policy as test_sync_peaks.py.

What this proves that the mocked unit tests can't: B-Roll Analyzer's own
analyzer.py NEVER receives a .braw path (it only ever sees the resolved
proxy .mov) yet the clip's `path` in every result the frontend/cache/
export sees is the ORIGINAL .braw file — the swap-back in
broll_worker.py's run_analyze happens at exactly the right point (after
decode + thumbnail capture, before the cache write)."""

import os
import shutil
import time

import pytest

from backend import braw_bridge, braw_proxy_cache, paths

# Ships with the SDK itself.
_SAMPLE_BRAW = "/Applications/Blackmagic RAW/Blackmagic RAW SDK/Media/sample.braw"


def _skip_unless_ready():
    if not braw_bridge.braw_available():
        pytest.skip(f"BRAW SDK/tool not available: {braw_bridge.unavailable_reason()}")
    if not os.path.isfile(_SAMPLE_BRAW):
        pytest.skip(f"SDK sample clip not found at {_SAMPLE_BRAW}")
    if not os.path.isfile(paths.BROLL_PYTHON):
        pytest.skip("B-Roll Analyzer's venv not set up -- can't run a real analyze job")


def _wait_for_job(api, job_id, timeout=90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = api.jobs.get_job_dict(job_id)
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish in time")


def test_braw_start_without_preexisting_proxy_does_not_race(api, tmp_path):
    """Reproduces a real bug found in production use: broll_start queues
    the proxy job and the "broll" analyze job in the SAME call, with NO
    ordering guarantee between the two. The original implementation
    resolved each .braw file's decode path ONCE in run_analyze's single
    discovery loop, immediately before the pool was built -- for a
    small/first-time clip, the analyze subprocess's own startup was often
    FASTER than the proxy tool, so the clip almost always came back
    "proxy hasn't finished generating yet" on the very first Analyze
    click. Fixed by moving the resolution into _analyze_one (the pool
    child) and having IT wait (bounded, braw_bridge.wait_for_decode_path)
    for the proxy instead of failing immediately. This test intentionally
    does NOT pre-generate the proxy (contrast every other test in this
    file) -- that's the whole point, it drives broll_start exactly the
    way a real first-time click does."""
    _skip_unless_ready()
    folder = tmp_path / "clips"
    folder.mkdir()
    clip_path = str(folder / "sample.braw")
    shutil.copyfile(_SAMPLE_BRAW, clip_path)

    try:
        start = api.broll_start(str(folder))
        assert start["ok"], start
        assert len(start["braw_proxy_jobs"]) == 1

        job = _wait_for_job(api, start["job_id"], timeout=120.0)
        assert job["status"] == "done", job
        clip = job["result"]["clips"][0]
        assert clip["error"] is None, clip["error"]
        assert clip["path"] == clip_path
        assert clip["duration"] > 0
    finally:
        proxy_path = braw_proxy_cache.find_cached_proxy(clip_path)
        braw_proxy_cache.forget_proxy(clip_path)
        if proxy_path is not None and os.path.isfile(proxy_path):
            os.remove(proxy_path)


def test_braw_clip_analyzes_end_to_end_through_broll_workspace(api, tmp_path):
    _skip_unless_ready()
    # Deliberately NOT redirecting paths.PROXIES_DIR here (contrast the
    # cache-only tests in test_braw_proxy_cache.py): broll_worker.py runs
    # in a SEPARATE Python process (B-Roll Analyzer's own venv/
    # interpreter), which imports its own fresh copy of paths.py from
    # disk -- monkeypatch.setattr only ever affects the current process's
    # module object, so a patched PROXIES_DIR here would be invisible to
    # that subprocess and the proxy it wrote would never be found,
    # exactly the bug this comment is warning the next editor away from.
    # This test therefore writes a REAL proxy into the REAL assets/
    # proxies/ dir and cleans it up in `finally`.
    folder = tmp_path / "clips"
    folder.mkdir()
    clip_path = str(folder / "sample.braw")
    shutil.copyfile(_SAMPLE_BRAW, clip_path)

    try:
        # Pre-generate the proxy directly and wait for it -- makes the
        # analyze pass below deterministic (no race between the worker
        # subprocess's own startup and proxy generation finishing).
        proxy_res = braw_bridge.request_proxy(api.jobs, clip_path)
        assert proxy_res["ok"], proxy_res
        assert "job_id" in proxy_res, "first request for this clip should start a real job"
        proxy_job = _wait_for_job(api, proxy_res["job_id"])
        assert proxy_job["status"] == "done", proxy_job

        start = api.broll_start(str(folder))
        assert start["ok"], start
        assert start["braw_proxy_jobs"] == [], "proxy already cached -- broll_start shouldn't queue another"

        broll_job = _wait_for_job(api, start["job_id"])
        assert broll_job["status"] == "done", broll_job
        clips = broll_job["result"]["clips"]
        assert len(clips) == 1
        clip = clips[0]
        assert clip["error"] is None, clip["error"]
        assert clip["path"] == clip_path, "must reference the ORIGINAL .braw path, not the proxy"
        assert clip["duration"] > 0
        # NOT asserting thumbnail_data_uri here: the SDK's sample.braw is a
        # single-frame (~67ms) demo clip, and B-Roll Analyzer's own
        # _capture_thumbnail already returns no thumbnail for an ordinary
        # (non-BRAW) clip this short too -- confirmed directly against
        # analyzer.analyze_clip() on a matching 1-frame .mp4 fixture. A
        # sibling-app characteristic of degenerate single-frame sources,
        # not something this substitution layer could or should paper over.

        # Preview + favorite-thumbnail resolution both transparently
        # substitute the cached proxy for the .braw path -- neither RCS's
        # PreviewServer nor thumbnails.py ever sees the .braw path itself.
        preview = api.broll_preview_url(clip_path)
        assert preview["ok"], preview
        assert preview["url"]

        thumb = api.suite_broll_favorite_thumbnail(clip_path, 0.0)
        assert thumb["ok"], thumb
        assert thumb["data_uri"].startswith("data:image/jpeg;base64,")

        # Re-running Analyze on the same folder must be a cache hit for
        # the .braw clip (keyed by the ORIGINAL path's rel-path +
        # fingerprint) -- proves the path swap-back landed before the
        # cache write, not just in the frontend payload for this one run.
        second_start = api.broll_start(str(folder))
        assert second_start["ok"], second_start
        assert second_start["braw_proxy_jobs"] == []
        second_job = _wait_for_job(api, second_start["job_id"])
        assert second_job["status"] == "done", second_job
        assert second_job["result"]["clips"][0]["path"] == clip_path
        assert second_job["result"]["clips"][0]["error"] is None
    finally:
        # Leave no trace in the real assets/proxies/ dir.
        proxy_path = braw_proxy_cache.find_cached_proxy(clip_path)
        braw_proxy_cache.forget_proxy(clip_path)
        if proxy_path is not None and os.path.isfile(proxy_path):
            os.remove(proxy_path)


class _FakeWindow:
    """Minimal stand-in for pywebview's window -- just enough for
    create_file_dialog to return a scripted save path, so
    broll_export_xml's own dialog-then-export logic runs for real."""

    def __init__(self, dialog_result):
        self._dialog_result = dialog_result

    def create_file_dialog(self, *args, **kwargs):
        return self._dialog_result


def test_braw_clip_export_xml_end_to_end(api, tmp_path):
    """Closes the gap CONTRACT.md's own addendum v30 flagged and left
    open: "export XML wiring beyond what rebuild_from_cache's file-list
    change already covers" was never actually verified end-to-end. Traced
    through the code, this should already work (rebuild_from_cache /
    result_from_entry / rescore_clip / xml_export.export_xml all operate
    on already-cached data or plain strings, no fresh file access) -- this
    test proves it rather than leaving it as an assumption."""
    _skip_unless_ready()
    folder = tmp_path / "clips"
    folder.mkdir()
    clip_path = str(folder / "sample.braw")
    shutil.copyfile(_SAMPLE_BRAW, clip_path)

    try:
        proxy_res = braw_bridge.request_proxy(api.jobs, clip_path)
        assert proxy_res["ok"], proxy_res
        proxy_job = _wait_for_job(api, proxy_res["job_id"])
        assert proxy_job["status"] == "done", proxy_job
        proxy_path = proxy_job["result"]["proxy_path"]

        start = api.broll_start(str(folder))
        assert start["ok"], start
        broll_job = _wait_for_job(api, start["job_id"])
        assert broll_job["status"] == "done", broll_job
        assert broll_job["result"]["clips"][0]["path"] == clip_path

        output_path = str(tmp_path / "Best B-Roll Selects.xml")
        api.window = _FakeWindow(output_path)
        export_res = api.broll_export_xml(start["job_id"])
        assert export_res["ok"], export_res
        assert export_res["path"] == output_path
        assert os.path.isfile(output_path)

        with open(output_path, "r", encoding="utf-8") as f:
            xml_content = f.read()
        assert clip_path in xml_content, \
            "exported XML must reference the ORIGINAL .braw path"
        assert proxy_path not in xml_content, \
            "the ephemeral proxy path must never leak into an export"
    finally:
        proxy_path = braw_proxy_cache.find_cached_proxy(clip_path)
        braw_proxy_cache.forget_proxy(clip_path)
        if proxy_path is not None and os.path.isfile(proxy_path):
            os.remove(proxy_path)


def test_braw_preview_and_thumbnail_report_not_ready_without_a_proxy(api, monkeypatch, tmp_path):
    """Doesn't need the real SDK/tool -- just confirms the suite-side gate
    in api_broll.py reports a clear, non-crashing error for a .braw path
    with no cached proxy, rather than handing the raw .braw file to RCS's
    PreviewServer/thumbnails.py (which can't decode it)."""
    monkeypatch.setattr(paths, "PROXIES_DIR", str(tmp_path / "proxies"))
    clip_path = str(tmp_path / "clip.braw")
    with open(clip_path, "wb") as f:
        f.write(b"\x00" * 16)  # not a real BRAW file -- never decoded in this test

    preview = api.broll_preview_url(clip_path)
    assert preview == {"ok": False, "error":
                        "This BRAW clip's proxy hasn't finished generating yet — "
                        "check the Jobs drawer."}

    thumb = api.suite_broll_favorite_thumbnail(clip_path, 0.0)
    assert thumb == {"ok": False, "error":
                      "This BRAW clip's proxy hasn't finished generating yet — "
                      "check the Jobs drawer."}
