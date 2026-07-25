"""Harmonize workspace (Harmonizer integration): sidecar round-trip,
upfront rejection, a real end-to-end alignment job in Harmonizer's own
venv, FCPXML export, and the Send-to-Resolve SystemExit->error contract.

align.py/make_fcpxml.py/import_to_resolve.py are never modified — these
tests exercise the suite-owned glue (api_harmonize.py, harmonizer_bridge.py,
backend/workers/harmonize_worker.py) that calls their existing functions.
"""

import os
import time

import pytest

from backend import harmonizer_bridge, paths


def _wait_for_job(api, job_id, timeout=90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = api.jobs.get_job_dict(job_id)
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish in time")


# ---------------------------------------------------------------------------
# Pure-Python: sidecar round-trip (mirrors test_sync_offsets.py)
# ---------------------------------------------------------------------------

def test_harmonize_load_report_for_missing_sidecar_is_found_false(api, tmp_path):
    missing_ref = str(tmp_path / "no-such-ref.wav")
    loaded = api.harmonize_load_report(missing_ref)
    assert loaded.get("ok") is True and loaded.get("found") is False, loaded


def test_harmonize_save_then_load_report_round_trip(api, tmp_path):
    ref_path = str(tmp_path / "ref.wav")
    report = {"reference": "ref.wav", "takes": ["take1.wav"], "segments": {}}
    saved = api.harmonize_save_report(ref_path, ["take1.wav"], report)
    assert saved.get("ok"), saved
    assert saved["path"] == str(tmp_path / "ref.harmonize-report.json")

    loaded = api.harmonize_load_report(ref_path)
    assert loaded.get("found") is True, loaded
    assert loaded["report"] == report
    assert loaded["take_paths"] == ["take1.wav"]


# ---------------------------------------------------------------------------
# Upfront rejection, no subprocess spawned
# ---------------------------------------------------------------------------

def test_harmonize_start_rejects_missing_reference_without_spawning(monkeypatch, api, tmp_path):
    monkeypatch.setattr(api.jobs, "start_subprocess_job",
                         lambda *a, **k: pytest.fail("should not spawn a subprocess"))
    take = tmp_path / "take1.wav"
    take.write_bytes(b"")
    res = api.harmonize_start(str(tmp_path / "no-such-ref.wav"), [str(take)])
    assert res.get("ok") is False


def test_harmonize_start_rejects_missing_take_without_spawning(monkeypatch, api, tmp_path):
    monkeypatch.setattr(api.jobs, "start_subprocess_job",
                         lambda *a, **k: pytest.fail("should not spawn a subprocess"))
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"")
    res = api.harmonize_start(str(ref), [str(tmp_path / "no-such-take.wav")])
    assert res.get("ok") is False


def test_harmonize_start_rejects_no_takes_without_spawning(monkeypatch, api, tmp_path):
    monkeypatch.setattr(api.jobs, "start_subprocess_job",
                         lambda *a, **k: pytest.fail("should not spawn a subprocess"))
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"")
    res = api.harmonize_start(str(ref), [])
    assert res.get("ok") is False


# ---------------------------------------------------------------------------
# Real subprocess integration (mirrors test_sync_peaks.py)
# ---------------------------------------------------------------------------

def test_harmonize_start_real_alignment_end_to_end(api):
    if not os.path.isfile(paths.HARMONIZER_PYTHON):
        pytest.skip("Harmonizer venv missing -- harmonize_start not verifiable")
    ref = os.path.join(paths.HARMONIZER_BACKEND_DIR, "ref.wav")
    takes = [os.path.join(paths.HARMONIZER_BACKEND_DIR, f"take{i}.wav") for i in (1, 2, 3)]
    if not os.path.isfile(ref) or not all(os.path.isfile(t) for t in takes):
        pytest.skip("Harmonizer prototype's committed test fixtures are missing")

    start = api.harmonize_start(ref, takes)
    assert start.get("ok"), start
    try:
        job = _wait_for_job(api, start["job_id"])
        assert job["status"] == "done", job

        report = job["result"]["report"]
        for key in ("reference", "takes", "ref_duration", "take_durations",
                    "coarse_offsets_sec", "coarse_offset_confidence", "anchors",
                    "segments", "skipped_anchors", "excluded_leadin_ref_sec", "waveforms"):
            assert key in report, f"report missing {key!r}: {sorted(report.keys())}"
        assert report["takes"] == ["take1.wav", "take2.wav", "take3.wav"]
        assert report["ref_duration"] > 0
    finally:
        # api.jobs is a process-wide singleton (jobs.get_job_manager()), not
        # per-test state -- clear this job so it doesn't leak into an
        # unrelated later test in the same pytest session that asserts an
        # empty job list (e.g. test_suite_api_edit_braw.py's
        # test_link_media_file_does_not_queue_for_a_non_braw_result).
        api.jobs.clear_finished()


def test_harmonize_start_no_retime_skips_matching_for_that_take(api):
    """A take flagged no_retime (same source as the reference, e.g. fed
    from the same recorder) should get exactly one straight segment
    (align.py's build_segments, no_retime_takes branch) instead of the
    usual multi-segment path -- and the report should say which take(s)
    were treated that way (harmonize_worker.py's own added "no_retime_takes"
    key, not part of align.py's own CLI report shape)."""
    if not os.path.isfile(paths.HARMONIZER_PYTHON):
        pytest.skip("Harmonizer venv missing -- harmonize_start not verifiable")
    ref = os.path.join(paths.HARMONIZER_BACKEND_DIR, "ref.wav")
    takes = [os.path.join(paths.HARMONIZER_BACKEND_DIR, f"take{i}.wav") for i in (1, 2, 3)]
    if not os.path.isfile(ref) or not all(os.path.isfile(t) for t in takes):
        pytest.skip("Harmonizer prototype's committed test fixtures are missing")

    start = api.harmonize_start(ref, takes, no_retime=["take2.wav"])
    assert start.get("ok"), start
    try:
        job = _wait_for_job(api, start["job_id"])
        assert job["status"] == "done", job
        report = job["result"]["report"]
        assert report["no_retime_takes"] == ["take2.wav"]
        assert len(report["segments"]["take2.wav"]) == 1, \
            f"no_retime take should be one straight segment: {report['segments']['take2.wav']}"
    finally:
        api.jobs.clear_finished()


def test_harmonize_start_rejects_unknown_no_retime_name(api):
    if not os.path.isfile(paths.HARMONIZER_PYTHON):
        pytest.skip("Harmonizer venv missing -- harmonize_start not verifiable")
    ref = os.path.join(paths.HARMONIZER_BACKEND_DIR, "ref.wav")
    take = os.path.join(paths.HARMONIZER_BACKEND_DIR, "take1.wav")
    if not os.path.isfile(ref) or not os.path.isfile(take):
        pytest.skip("Harmonizer prototype's committed test fixtures are missing")

    start = api.harmonize_start(ref, [take], no_retime=["nonexistent.wav"])
    assert start.get("ok"), start
    try:
        job = _wait_for_job(api, start["job_id"])
        assert job["status"] == "error", job
        assert "nonexistent.wav" in (job.get("error") or "")
    finally:
        api.jobs.clear_finished()


def test_harmonize_save_then_load_report_round_trip_preserves_no_retime(api, tmp_path):
    ref_path = str(tmp_path / "ref.wav")
    report = {"reference": "ref.wav", "takes": ["take1.wav", "take2.wav"], "segments": {}}
    saved = api.harmonize_save_report(ref_path, ["take1.wav", "take2.wav"], report, no_retime=["take2.wav"])
    assert saved.get("ok"), saved

    loaded = api.harmonize_load_report(ref_path)
    assert loaded.get("found") is True, loaded
    assert loaded["no_retime"] == ["take2.wav"]


# ---------------------------------------------------------------------------
# Export, in-process, no venv skip needed
# ---------------------------------------------------------------------------

def _minimal_report(take_name):
    """Just enough of align.py's report shape for make_fcpxml.build_fcpxml
    to run: one take, one segment spanning its whole (assumed 1:1) length."""
    return {
        "takes": [take_name],
        "segments": {take_name: [
            {"ref_start": 0.0, "ref_end": 1.0, "take_start": 0.0, "take_end": 1.0,
             "speed_factor": 1.0, "flagged": False},
        ]},
        "excluded_leadin_ref_sec": {take_name: 0.0},
    }


_PLACEHOLDER_TAKE = os.path.join(paths.HARMONIZER_BACKEND_DIR, "real_test", "placeholder_C024.mp4")


def _skip_unless_placeholder_take():
    if not os.path.isfile(_PLACEHOLDER_TAKE):
        pytest.skip("Harmonizer prototype's real_test/placeholder_C024.mp4 fixture is missing")
    import shutil
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not on PATH -- can't probe the take for export")


class _FakeWindow:
    """Minimal stand-in for pywebview's window -- just enough for
    create_file_dialog to return a scripted save path, so
    harmonize_export_xml's own dialog-then-export logic runs for real.
    Records the kwargs it was called with (e.g. save_filename) so tests
    can assert on the SUGGESTED filename, not just the final result."""

    def __init__(self, dialog_result):
        self._dialog_result = dialog_result
        self.last_call_kwargs = None

    def create_file_dialog(self, *args, **kwargs):
        self.last_call_kwargs = kwargs
        return self._dialog_result


def test_harmonize_export_xml_writes_well_formed_fcpxml(api, tmp_path):
    _skip_unless_placeholder_take()
    take_name = os.path.basename(_PLACEHOLDER_TAKE)
    report = _minimal_report(take_name)

    output_path = str(tmp_path / "harmonized.fcpxml")
    api.window = _FakeWindow(output_path)
    res = api.harmonize_export_xml("/tmp/ref.wav", [_PLACEHOLDER_TAKE], report)
    assert res.get("ok"), res
    assert res["path"] == output_path
    assert "reference_note" in res

    import xml.etree.ElementTree as ET
    ET.parse(output_path)  # raises on malformed XML


def test_harmonize_export_xml_uses_custom_timeline_name(api, tmp_path):
    """The Harmonize workspace's optional timeline-name field should both
    suggest that name as the save dialog's default filename AND land in
    the FCPXML's <event>/<project> name (make_fcpxml.build_fcpxml's own
    sequence_name param) -- not just fall through to the timestamped
    default."""
    _skip_unless_placeholder_take()
    take_name = os.path.basename(_PLACEHOLDER_TAKE)
    report = _minimal_report(take_name)

    output_path = str(tmp_path / "My Custom Timeline.fcpxml")
    window = _FakeWindow(output_path)
    api.window = window
    res = api.harmonize_export_xml("/tmp/ref.wav", [_PLACEHOLDER_TAKE], report,
                                    timeline_name="My Custom Timeline")
    assert res.get("ok"), res
    assert window.last_call_kwargs["save_filename"] == "My Custom Timeline.fcpxml"

    with open(output_path, "r", encoding="utf-8") as f:
        xml_content = f.read()
    assert '<event name="My Custom Timeline"' in xml_content
    assert '<project name="My Custom Timeline"' in xml_content


def test_harmonize_export_xml_rejects_braw_takes_without_probing(api):
    report = _minimal_report("a.braw")
    res = api.harmonize_export_xml("/tmp/ref.wav", ["/tmp/a.braw"], report)
    assert res.get("ok") is False
    assert "BRAW" in res.get("error", "")


def test_harmonize_send_to_resolve_rejects_braw_takes(api):
    report = _minimal_report("a.braw")
    res = api.harmonize_send_to_resolve("/tmp/ref.wav", ["/tmp/a.braw"], report)
    assert res.get("ok") is False
    assert "BRAW" in res.get("error", "")


# ---------------------------------------------------------------------------
# Send-to-Resolve error path, no real Resolve needed
# ---------------------------------------------------------------------------

def test_harmonize_send_to_resolve_converts_systemexit_to_error(monkeypatch, api):
    """connect_resolve()/the rest of import_to_resolve.py's functions raise
    SystemExit on failure (no Resolve running, project not found, ...)
    rather than returning error values -- confirm the bridge converts that
    into the normal {ok, error} contract instead of letting a BaseException
    escape into SuiteApi. Mocked deterministically (not run against a real
    Resolve) so this test's outcome doesn't depend on whether Resolve
    happens to be running on the machine executing the suite, and so it
    never pokes a real, possibly-in-use Resolve session."""
    _skip_unless_placeholder_take()

    def fake_connect_resolve():
        raise SystemExit("Could not connect to Resolve -- is it running?")
    monkeypatch.setattr(harmonizer_bridge.import_to_resolve, "connect_resolve", fake_connect_resolve)

    take_name = os.path.basename(_PLACEHOLDER_TAKE)
    report = _minimal_report(take_name)
    res = api.harmonize_send_to_resolve("/tmp/ref.wav", [_PLACEHOLDER_TAKE], report)
    assert res.get("ok") is False
    assert "Could not connect to Resolve" in res.get("error", "")
