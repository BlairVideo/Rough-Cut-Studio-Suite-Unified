"""Real, end-to-end .braw support in the Sync workspace (BRAW
compatibility plan, Phase 2 — suite-side-only substitution, same pattern
as test_broll_braw_real.py). Exercises the actual compiled
braw_proxy_tool, the SDK's real sample.braw clip, and a real A-Sync
subprocess (its own venv) — not mocks. Skipped whenever the SDK/tool,
ffmpeg, or A-Sync's own venv isn't present on this machine.

sync_core.py is never modified — every path this worker hands it is
resolved by braw_bridge.wait_for_decode_path() in sync_worker.py first
(see that module's docstring). What these tests prove that the mocked
unit tests can't: probe/peaks/detect all correctly decode through the
proxy while every OUTPUT still references the ORIGINAL .braw path, AND
(test_sync_start_without_preexisting_proxy_does_not_race) that a real
first-time sync_start call — with no proxy pre-generated, exactly like
a real click in the app — doesn't lose the race between
queue_missing_proxies and the "sync" job it starts in the same call."""

import os
import shutil
import subprocess
import time

import pytest

from backend import braw_bridge, braw_proxy_cache, paths

_SAMPLE_BRAW = "/Applications/Blackmagic RAW/Blackmagic RAW SDK/Media/sample.braw"


def _skip_unless_ready():
    if not braw_bridge.braw_available():
        pytest.skip(f"BRAW SDK/tool not available: {braw_bridge.unavailable_reason()}")
    if not os.path.isfile(_SAMPLE_BRAW):
        pytest.skip(f"SDK sample clip not found at {_SAMPLE_BRAW}")
    if not os.path.isfile(paths.ASYNC_PYTHON):
        pytest.skip("A-Sync's venv not set up -- can't run a real sync job")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH -- needed to build the test external-audio fixture")


def _wait_for_job(api, job_id, timeout=90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = api.jobs.get_job_dict(job_id)
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish in time")


@pytest.fixture
def braw_clip_with_proxy(api, tmp_path):
    """Copies the SDK's sample.braw into a temp location and pre-generates
    (and waits for) its real cached proxy -- shared setup for every test
    below, so each test starts from a deterministic, already-cached
    state rather than racing the worker subprocess against proxy
    generation. Cleans up the REAL assets/proxies/ entry afterward (same
    monkeypatch limitation noted in test_broll_braw_real.py: sync_worker.py
    runs as a separate process that reads the real paths.py from disk, so
    redirecting PROXIES_DIR here would be invisible to it)."""
    _skip_unless_ready()
    clip_path = str(tmp_path / "clip.braw")
    shutil.copyfile(_SAMPLE_BRAW, clip_path)

    proxy_res = braw_bridge.request_proxy(api.jobs, clip_path)
    assert proxy_res["ok"], proxy_res
    assert "job_id" in proxy_res
    proxy_job = _wait_for_job(api, proxy_res["job_id"])
    assert proxy_job["status"] == "done", proxy_job

    try:
        yield clip_path, proxy_job["result"]["proxy_path"]
    finally:
        proxy_path = braw_proxy_cache.find_cached_proxy(clip_path)
        braw_proxy_cache.forget_proxy(clip_path)
        if proxy_path is not None and os.path.isfile(proxy_path):
            os.remove(proxy_path)


def test_braw_video_probe_and_peaks_end_to_end(api, braw_clip_with_proxy):
    clip_path, _proxy_path = braw_clip_with_proxy

    probe_res = api.sync_probe([clip_path])
    assert probe_res["ok"], probe_res
    assert probe_res["braw_proxy_jobs"] == [], "proxy already cached -- no new job needed"
    probe = probe_res["probes"][clip_path]
    assert "error" not in probe, probe
    assert probe["duration"] > 0
    assert probe["has_video"] is True
    assert probe["has_audio"] is True
    assert probe["audio_channels"] == 2
    assert probe["audio_samplerate"] == 48000
    # Addendum v56: the generated proxy now carries a real embedded
    # starting timecode (a single QuickTime tmcd sample spanning the
    # whole clip) sourced from the BRAW SDK's own GetTimecodeForFrame --
    # this is the SDK sample.braw's actual real timecode, confirmed via
    # direct ffprobe inspection while implementing the fix, not a value
    # invented for this test.
    assert probe["timecode_tag"] == "22:23:40:20"

    peaks_res = api.sync_peaks([clip_path])
    assert peaks_res["ok"], peaks_res
    assert peaks_res["braw_proxy_jobs"] == []
    peaks_entry = peaks_res["peaks"][clip_path]
    assert "error" not in peaks_entry, peaks_entry
    assert len(peaks_entry["peaks"]) > 0
    assert peaks_entry["duration"] > 0


def test_braw_video_sync_detect_end_to_end(api, braw_clip_with_proxy, tmp_path):
    clip_path, proxy_path = braw_clip_with_proxy

    # External "audio" that's literally the video's own audio track --
    # a real waveform-correlation run against it should land offset ~0.
    audio_path = str(tmp_path / "external_audio.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", proxy_path, "-vn",
         "-acodec", "pcm_s16le", audio_path],
        check=True, timeout=30,
    )

    start = api.sync_start(clip_path, [audio_path], method="waveform")
    assert start["ok"], start
    assert start["braw_proxy_jobs"] == [], "video's proxy already cached, audio isn't .braw"

    job = _wait_for_job(api, start["job_id"])
    assert job["status"] == "done", job
    result = job["result"]
    assert result["video"]["path"] == clip_path, "must reference the ORIGINAL .braw path, not the proxy"
    assert result["video"]["probe"]["duration"] > 0

    track = result["tracks"][0]
    assert track["error"] is None, track["error"]
    assert track["path"] == audio_path
    assert abs(track["offset_seconds"]) < 0.01, \
        "external audio is the video's own audio -- offset should be ~0"


class _FakeWindow:
    """Minimal stand-in for pywebview's window -- just enough for
    create_file_dialog to return a scripted save path, so
    sync_export_xml's own dialog-then-export logic runs for real."""

    def __init__(self, dialog_result):
        self._dialog_result = dialog_result

    def create_file_dialog(self, *args, **kwargs):
        return self._dialog_result


def test_braw_video_sync_export_xml_end_to_end(api, braw_clip_with_proxy, tmp_path):
    """Closes the same gap as test_broll_braw_real.py's
    test_braw_clip_export_xml_end_to_end, for the Sync workspace's own
    export: sync_xml.build_sync_xml only ever consumes the probe dicts
    already computed by sync_start (its own docstring: "no re-probing
    happens here"), so it should already be BRAW-safe by construction --
    this proves it rather than leaving it as an assumption, per
    CONTRACT.md addendum v30's "export XML wiring... not done in this
    pass" note."""
    clip_path, proxy_path = braw_clip_with_proxy

    audio_path = str(tmp_path / "external_audio.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", proxy_path, "-vn",
         "-acodec", "pcm_s16le", audio_path],
        check=True, timeout=30,
    )

    start = api.sync_start(clip_path, [audio_path], method="waveform")
    assert start["ok"], start
    job = _wait_for_job(api, start["job_id"])
    assert job["status"] == "done", job
    result = job["result"]
    assert result["video"]["path"] == clip_path

    output_path = str(tmp_path / "sample synced.xml")
    api.window = _FakeWindow(output_path)
    payload = {
        "video": result["video"],
        "tracks": result["tracks"],
        "include_camera_audio": True,
        "sequence_name": "BRAW sync export test",
    }
    export_res = api.sync_export_xml(payload)
    assert export_res["ok"], export_res
    assert export_res["path"] == output_path
    assert os.path.isfile(output_path)

    with open(output_path, "r", encoding="utf-8") as f:
        xml_content = f.read()
    assert clip_path in xml_content, \
        "exported XML must reference the ORIGINAL .braw path"
    assert proxy_path not in xml_content, \
        "the ephemeral proxy path must never leak into an export"
    assert audio_path in xml_content


def test_sync_start_without_preexisting_proxy_does_not_race(api, tmp_path):
    """Reproduces the same real production bug as
    test_broll_braw_real.py's equivalent test: sync_start queues the
    proxy job and the "sync" job in the SAME call, no ordering guarantee
    between the two. Deliberately does NOT use the braw_clip_with_proxy
    fixture (which pre-generates and waits for the proxy) -- that would
    defeat the point. The external "audio" here is a synthetic tone
    (can't extract real audio from the .braw before a proxy exists), so
    this only checks that the video side resolves/decodes without
    racing into "proxy not ready yet" -- correctness of the correlation
    itself is already covered by test_braw_video_sync_detect_end_to_end."""
    _skip_unless_ready()
    clip_path = str(tmp_path / "clip.braw")
    shutil.copyfile(_SAMPLE_BRAW, clip_path)
    audio_path = str(tmp_path / "tone.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", audio_path],
        check=True, timeout=30,
    )

    try:
        start = api.sync_start(clip_path, [audio_path], method="waveform")
        assert start["ok"], start
        assert len(start["braw_proxy_jobs"]) == 1

        job = _wait_for_job(api, start["job_id"], timeout=120.0)
        assert job["status"] == "done", job
        result = job["result"]
        assert result["video"]["path"] == clip_path
        assert result["video"]["probe"]["duration"] > 0
        track = result["tracks"][0]
        assert track["error"] is None, track["error"]
    finally:
        proxy_path = braw_proxy_cache.find_cached_proxy(clip_path)
        braw_proxy_cache.forget_proxy(clip_path)
        if proxy_path is not None and os.path.isfile(proxy_path):
            os.remove(proxy_path)


def test_sync_preview_url_resolves_braw_clip(api, braw_clip_with_proxy):
    clip_path, proxy_path = braw_clip_with_proxy
    res = api.sync_preview_url(clip_path)
    assert res["ok"], res
    assert res["url"]


def test_sync_preview_url_reports_not_ready_without_a_proxy(api, monkeypatch, tmp_path):
    """Doesn't need the real SDK/tool -- confirms the suite-side gate
    reports a clear, non-crashing error for a .braw path with no cached
    proxy, rather than handing the raw .braw file to RCS's PreviewServer."""
    monkeypatch.setattr(paths, "PROXIES_DIR", str(tmp_path / "proxies"))
    clip_path = str(tmp_path / "clip.braw")
    with open(clip_path, "wb") as f:
        f.write(b"\x00" * 16)  # not a real BRAW file -- never decoded in this test

    res = api.sync_preview_url(clip_path)
    assert res == {"ok": False, "error":
                    "This BRAW clip's proxy hasn't finished generating yet — "
                    "check the Jobs drawer."}
