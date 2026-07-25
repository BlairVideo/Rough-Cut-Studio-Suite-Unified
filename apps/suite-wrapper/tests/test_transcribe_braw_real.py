"""Real, end-to-end .braw support in the Transcribe workspace (BRAW
compatibility plan, Phase 2 — suite-side-only substitution, same pattern
as test_broll_braw_real.py/test_sync_braw_real.py). Exercises the actual
compiled braw_proxy_tool, the SDK's real sample.braw clip, and a real
Local Interview Transcriber subprocess (its own venv) — not mocks.
Skipped whenever the SDK/tool or the transcriber's own venv isn't present
on this machine.

The transcriber's own app.py is never modified — transcribe_worker.py
resolves video_path through its cached BRAW proxy via
braw_bridge.wait_for_decode_path BEFORE calling app.extract_audio (see
that worker's module docstring). What these tests prove that the mocked
unit tests can't: a real transcribe job actually decodes through the
proxy end to end, AND the resulting .ivt-cache.json is still keyed by
the ORIGINAL .braw path (never the proxy), exactly like every other
BRAW-substituted workspace."""

import json
import os
import shutil
import time

import pytest

from backend import api_shared, braw_bridge, braw_proxy_cache, paths

_SAMPLE_BRAW = "/Applications/Blackmagic RAW/Blackmagic RAW SDK/Media/sample.braw"

# Smallest/fastest whisper model — this test only needs a job that
# completes successfully and produces a schema-correct cache, not an
# accurate transcript.
_FAST_MODEL_LABEL = "Fast (tiny, lower accuracy)"


def _skip_unless_ready():
    if not braw_bridge.braw_available():
        pytest.skip(f"BRAW SDK/tool not available: {braw_bridge.unavailable_reason()}")
    if not os.path.isfile(_SAMPLE_BRAW):
        pytest.skip(f"SDK sample clip not found at {_SAMPLE_BRAW}")
    if not os.path.isfile(paths.IVT_PYTHON):
        pytest.skip("Local Interview Transcriber's venv not set up -- can't run a real job")


def _wait_for_job(api, job_id, timeout=600.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = api.jobs.get_job_dict(job_id)
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish in time")


@pytest.fixture
def braw_clip_with_proxy(api, tmp_path):
    """Same shared-setup idea as test_sync_braw_real.py's fixture of the
    same name: copies the SDK's sample.braw into a temp location and
    pre-generates (and waits for) its real cached proxy, so the
    transcribe test below starts from a deterministic already-cached
    state. Cleans up the REAL assets/proxies/ entry afterward — the
    transcribe worker runs as a separate process reading the real
    paths.py from disk, so redirecting PROXIES_DIR here would be
    invisible to it."""
    _skip_unless_ready()
    clip_path = str(tmp_path / "clip.braw")
    shutil.copyfile(_SAMPLE_BRAW, clip_path)

    proxy_res = braw_bridge.request_proxy(api.jobs, clip_path)
    assert proxy_res["ok"], proxy_res
    assert "job_id" in proxy_res
    proxy_job = _wait_for_job(api, proxy_res["job_id"])
    assert proxy_job["status"] == "done", proxy_job

    try:
        yield clip_path
    finally:
        proxy_path = braw_proxy_cache.find_cached_proxy(clip_path)
        braw_proxy_cache.forget_proxy(clip_path)
        if proxy_path is not None and os.path.isfile(proxy_path):
            os.remove(proxy_path)
        # The test below writes a real .ivt-cache.json into the REAL
        # assets/ivt_cache/ dir (addendum v55's centralized fallback for
        # a .braw video) -- clean it up so no test debris survives.
        cache_path = braw_bridge.ivt_cache_path(clip_path)
        if os.path.isfile(cache_path):
            os.remove(cache_path)


def test_transcribe_braw_clip_end_to_end(api, braw_clip_with_proxy):
    clip_path = braw_clip_with_proxy

    start = api.transcriber_start([clip_path], _FAST_MODEL_LABEL, False)
    assert start["ok"], start
    assert start["braw_proxy_jobs"] == [], "proxy already cached -- no new job needed"

    job = _wait_for_job(api, start["job_ids"][0])
    assert job["status"] == "done", job
    result = job["result"]
    assert result["video_path"] == clip_path, \
        "must reference the ORIGINAL .braw path, not the proxy"
    assert isinstance(result.get("segments"), list)

    # Addendum v55: a .braw video's cache is centralized under
    # paths.IVT_CACHE_DIR instead of next to the (routinely read-only)
    # source -- confirm it landed there, NOT at the old next-to-source
    # location, since that's exactly the fix being proven here.
    cache_path = braw_bridge.ivt_cache_path(clip_path)
    assert result["cache_path"] == cache_path
    assert os.path.dirname(cache_path) == paths.IVT_CACHE_DIR
    old_style_path = clip_path + api_shared.IVT_CACHE_SUFFIX
    assert cache_path != old_style_path
    assert not os.path.isfile(old_style_path)
    assert os.path.isfile(cache_path)
    with open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    assert cache["path"] == clip_path


def test_transcriber_start_without_preexisting_proxy_does_not_race(api, tmp_path):
    """Reproduces the same real production bug already covered for
    B-Roll/Sync: transcriber_start queues the proxy job and the
    "transcribe" job in the SAME call, with no ordering guarantee
    between the two. Deliberately does NOT use the braw_clip_with_proxy
    fixture (which pre-generates and waits) -- that would defeat the
    point. Relies on braw_bridge's effectively-unbounded wait
    (addendum v48) rather than racing a fixed timeout."""
    _skip_unless_ready()
    clip_path = str(tmp_path / "clip.braw")
    shutil.copyfile(_SAMPLE_BRAW, clip_path)

    try:
        start = api.transcriber_start([clip_path], _FAST_MODEL_LABEL, False)
        assert start["ok"], start
        assert len(start["braw_proxy_jobs"]) == 1

        job = _wait_for_job(api, start["job_ids"][0], timeout=900.0)
        assert job["status"] == "done", job
        assert job["result"]["video_path"] == clip_path
    finally:
        proxy_path = braw_proxy_cache.find_cached_proxy(clip_path)
        braw_proxy_cache.forget_proxy(clip_path)
        if proxy_path is not None and os.path.isfile(proxy_path):
            os.remove(proxy_path)
        cache_path = braw_bridge.ivt_cache_path(clip_path)
        if os.path.isfile(cache_path):
            os.remove(cache_path)
