"""Real, end-to-end BRAW proxy generation -- unlike test_braw_bridge.py's
fake stand-in tool, this drives the ACTUAL compiled braw_proxy_tool
(built by tools/braw/build.sh against the real Blackmagic RAW SDK)
against the real sample.braw clip the SDK ships. Skipped whenever either
isn't present on this machine -- same policy as test_sync_peaks.py
skipping without ffmpeg/A-Sync's venv: an unverifiable case shouldn't
fail an otherwise-green run.

No paths/candidates are monkeypatched here (contrast test_braw_bridge.py)
-- this test intentionally exercises braw_bridge exactly as a real launch
of Studio Suite would see it."""

import os
import shutil
import time

import pytest

from backend import braw_bridge
from backend.jobs import JobManager

# Ships with the SDK itself, used by its own ExtractFrame/ExtractAudio/
# ProcessClipCPU samples as their default test clip.
_SAMPLE_BRAW = ("/Applications/Blackmagic RAW/Blackmagic RAW SDK/Media/sample.braw")


def _skip_unless_real_tool_available():
    if not braw_bridge.braw_available():
        pytest.skip(f"BRAW SDK/tool not available on this machine: {braw_bridge.unavailable_reason()}")
    if not os.path.isfile(_SAMPLE_BRAW):
        pytest.skip(f"SDK sample clip not found at {_SAMPLE_BRAW}")


def _wait_for_job(manager, job_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get_job_dict(job_id)
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_real_braw_proxy_generation_produces_playable_media(monkeypatch, tmp_path):
    _skip_unless_real_tool_available()
    from backend import paths, braw_proxy_cache
    monkeypatch.setattr(paths, "PROXIES_DIR", str(tmp_path / "proxies"))

    manager = JobManager()
    res = braw_bridge.request_proxy(manager, _SAMPLE_BRAW)
    assert res["ok"] is True, res
    assert not res.get("cached"), "first run should never hit a pre-existing cache"

    job = _wait_for_job(manager, res["job_id"])
    assert job["status"] == "done", job
    proxy_path = job["result"]["proxy_path"]
    assert os.path.isfile(proxy_path)
    assert os.path.getsize(proxy_path) > 0

    if shutil.which("ffprobe") is not None:
        import subprocess
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
             "-of", "csv=p=0", proxy_path],
            capture_output=True, text=True, timeout=30,
        )
        assert probe.returncode == 0, probe.stderr
        streams = probe.stdout.strip().splitlines()
        assert any("video" in s for s in streams), f"expected a video stream, got: {streams}"

    # Second request for the same source must short-circuit to the cache
    # (no second job) -- exercises braw_proxy_cache end-to-end for real,
    # not just against the hand-built fixtures in test_braw_proxy_cache.py.
    second = braw_bridge.request_proxy(manager, _SAMPLE_BRAW)
    assert second == {"ok": True, "cached": True, "proxy_path": proxy_path}
