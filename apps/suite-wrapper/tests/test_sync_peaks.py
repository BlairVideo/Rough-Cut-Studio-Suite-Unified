"""sync_peaks -- downsampled waveform peaks for the Sync workspace's
waveform visual (addendum v20). A real integration test: spawns the
actual sync_worker.py subprocess in A-Sync's own venv against real
ffmpeg-generated audio, unlike the pure-Python tests in
test_sync_offsets.py. Skipped if either A-Sync's venv or ffmpeg isn't
set up on this machine -- the equality/behavior is then unverifiable,
which shouldn't fail an otherwise-green run (same policy as
test_sibling_drift.py's WHISPER_MODELS check)."""

import shutil
import subprocess

import pytest

from backend import paths


def _ffmpeg_available():
    return shutil.which("ffmpeg") is not None


@pytest.fixture
def two_test_tones(tmp_path):
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not on PATH -- can't generate test audio")
    a = str(tmp_path / "tone_a.wav")
    b = str(tmp_path / "tone_b.wav")
    for path, freq, dur in ((a, 440, 2), (b, 880, 1)):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}",
             path],
            capture_output=True, timeout=30, check=True)
    return a, b


def test_sync_peaks_returns_real_waveform_data(api, two_test_tones):
    if not __import__("os").path.exists(paths.ASYNC_PYTHON):
        pytest.skip("A-Sync venv missing -- sync_peaks not verifiable")
    a, b = two_test_tones
    res = api.sync_peaks([a, b])
    assert res.get("ok"), res
    peaks = res["peaks"]
    assert a in peaks and b in peaks
    assert "error" not in peaks[a], peaks[a]
    assert len(peaks[a]["peaks"]) > 0
    assert all(len(bucket) == 2 for bucket in peaks[a]["peaks"][:5]), \
        "each bucket should be a [min, max] pair"
    assert peaks[a]["duration"] == pytest.approx(2.0, abs=0.1)
    assert peaks[b]["duration"] == pytest.approx(1.0, abs=0.1)


def test_sync_peaks_records_per_file_error_without_failing_the_batch(api, two_test_tones, tmp_path):
    if not __import__("os").path.exists(paths.ASYNC_PYTHON):
        pytest.skip("A-Sync venv missing -- sync_peaks not verifiable")
    a, _ = two_test_tones
    missing = str(tmp_path / "does-not-exist.wav")
    # sync_peaks itself gates on os.path.isfile before ever spawning the
    # worker, so a mix of a real file with a path that vanishes between
    # the isfile check and the decode isn't reachable from here -- but a
    # file that exists yet has no valid audio (empty file) is, and
    # should record its own per-file error rather than failing entirely.
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    res = api.sync_peaks([a, str(empty)])
    assert res.get("ok"), res
    assert "error" not in res["peaks"][a]
    assert "error" in res["peaks"][str(empty)]


def test_sync_peaks_rejects_missing_file_upfront(api, tmp_path):
    missing = str(tmp_path / "nope.wav")
    res = api.sync_peaks([missing])
    assert res.get("ok") is False
