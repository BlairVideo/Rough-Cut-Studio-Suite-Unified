"""Card Eater copy/verify engine (backend/cardeater_copy.py + cardeater_verify.py)
-- the highest-risk code in the Card Eater workspace, since it moves and
verifies irreplaceable camera footage. Ported from CardEater/src-tauri/src/
copy_engine.rs + verify.rs (see cardeater_copy.py's own module docstring for
the exact behavioral contract: disk-space preflight, chunked streaming copy,
overlapped verify-worker pool, one retry on hash mismatch, pause/cancel
checked only at file boundaries, per-destination concurrency)."""

import os
import time

import pytest

from backend import cardeater_copy as copy_engine
from backend import cardeater_naming as naming
from backend import cardeater_verify as verify
from backend.api_cardeater import CardEaterState


@pytest.fixture
def state(tmp_path):
    return CardEaterState(str(tmp_path / "cardeater.sqlite3"))


def _template(**overrides):
    tpl = {
        "name": "Test",
        "folder_template": "{YYYYMMDD}_{Name}",
        "file_template": "{YYYYMMDD}_{Name}_{Seq}.{ext}",
        "date_source": "card_insert",
        "seq_start": None,
        "seq_padding": 3,
        "no_subfolder": False,
        "use_source_filename": False,
        "no_sequence": False,
    }
    tpl.update(overrides)
    return tpl


def _wait_for_completion(state, job_id, timeout=5.0):
    """Polls get_job_status until every destination reaches a terminal
    state, or fails the test -- copy work runs on real background threads
    started by start_job, so tests observe it the same way the frontend's
    polling loop does."""
    terminal = {"complete", "failed", "cancelled", "paused"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = copy_engine.get_job_status(state, job_id)
        if all(d["status"] in terminal for d in status["destinations"]):
            return status
        time.sleep(0.02)
    pytest.fail(f"job {job_id} did not reach a terminal state within {timeout}s")


# ---------------------------------------------------------------------------
# Disk space preflight
# ---------------------------------------------------------------------------

def test_check_disk_space_reports_ok_for_small_requirement(tmp_path):
    checks = copy_engine.check_disk_space([str(tmp_path)], bytes_needed=10)
    assert len(checks) == 1
    assert checks[0]["ok"] is True
    assert checks[0]["available_bytes"] >= 10


def test_check_disk_space_reports_not_ok_for_absurd_requirement(tmp_path):
    checks = copy_engine.check_disk_space([str(tmp_path)], bytes_needed=10 ** 18)
    assert checks[0]["ok"] is False


def test_check_disk_space_walks_up_to_nearest_existing_ancestor(tmp_path):
    # The destination subfolder doesn't exist yet at preflight time (it's
    # created at copy time) -- disk_usage must still resolve via the
    # nearest existing ancestor rather than erroring.
    not_yet_created = tmp_path / "does" / "not" / "exist" / "yet"
    checks = copy_engine.check_disk_space([str(not_yet_created)], bytes_needed=10)
    assert checks[0]["ok"] is True


# ---------------------------------------------------------------------------
# BLAKE3 verification
# ---------------------------------------------------------------------------

def test_verify_pair_matches_identical_content(tmp_path):
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"identical content")
    dst.write_bytes(b"identical content")
    result = verify.verify_pair(str(src), str(dst))
    assert result["matched"] is True
    assert result["hash_source"] == result["hash_dest"]


def test_verify_pair_detects_mismatched_content(tmp_path):
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"original bytes")
    dst.write_bytes(b"corrupted bytes")
    result = verify.verify_pair(str(src), str(dst))
    assert result["matched"] is False
    assert result["hash_source"] != result["hash_dest"]


def test_hash_file_is_deterministic(tmp_path):
    p1 = tmp_path / "a.bin"
    p2 = tmp_path / "b.bin"
    p1.write_bytes(b"same bytes twice")
    p2.write_bytes(b"same bytes twice")
    assert verify.hash_file(str(p1)) == verify.hash_file(str(p2))


def test_hash_file_raises_oserror_for_missing_file(tmp_path):
    with pytest.raises(OSError):
        verify.hash_file(str(tmp_path / "nope.bin"))


# ---------------------------------------------------------------------------
# JobControl state machine
# ---------------------------------------------------------------------------

def test_job_control_running_returns_immediately(state):
    control = copy_engine.JobControl()
    cancelled = copy_engine._wait_for_turn(state, dest_id=999999, control=control)
    assert cancelled is False


def test_job_control_cancelled_returns_true(state):
    control = copy_engine.JobControl()
    control.state = copy_engine.JobControl.CANCELLED
    cancelled = copy_engine._wait_for_turn(state, dest_id=999999, control=control)
    assert cancelled is True


def test_job_control_paused_then_cancelled(state, monkeypatch):
    monkeypatch.setattr(copy_engine, "PAUSE_POLL_INTERVAL", 0.01)
    control = copy_engine.JobControl()
    control.state = copy_engine.JobControl.PAUSED

    import threading

    def flip_to_cancelled():
        time.sleep(0.05)
        control.state = copy_engine.JobControl.CANCELLED

    threading.Thread(target=flip_to_cancelled, daemon=True).start()
    cancelled = copy_engine._wait_for_turn(state, dest_id=999999, control=control)
    assert cancelled is True


# ---------------------------------------------------------------------------
# Full start_job integration (real files, real background copy threads)
# ---------------------------------------------------------------------------

def _make_source_files(source_dir, specs):
    """specs: list of (name, ext, content). Returns FileEntry dicts."""
    files = []
    for name, ext, content in specs:
        path = source_dir / name
        path.write_bytes(content)
        files.append({
            "path": str(path), "relative_folder": "", "name": name, "ext": ext,
            "size_bytes": len(content), "created_at": None, "created_at_source": "unavailable",
        })
    return files


def test_start_job_copies_and_verifies_real_files(tmp_path, state):
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()

    specs = [("clip1.mov", "mov", b"clip one content"), ("clip2.mov", "mov", b"clip two content")]
    files = _make_source_files(source_dir, specs)
    template = _template()

    # Expected names: destination starts empty, so this mirrors what the
    # job itself will resolve against the fresh target folder.
    expected_names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z")

    req = {
        "source_card_label": "TESTCARD", "source_path": str(source_dir),
        "card_insert_date": "2026-07-14T00:00:00Z", "event_name": "GameDay",
        "manual_date": None, "template": template, "files": files,
        "destinations": [str(dest_dir)],
    }
    handle = copy_engine.start_job(state, req)
    status = _wait_for_completion(state, handle["job_id"])

    assert status["status"] == "complete", status
    dest = status["destinations"][0]
    assert dest["status"] == "complete", dest
    assert dest["files_copied"] == 2
    assert dest["files_verified"] == 2
    assert dest["resolved_path"] == os.path.join(str(dest_dir), "20260714_GameDay")

    for (name, _ext, content), expected_name in zip(specs, expected_names):
        copied = os.path.join(dest["resolved_path"], expected_name)
        assert os.path.isfile(copied), f"expected {copied} to exist"
        assert open(copied, "rb").read() == content, f"{copied} content mismatch"


def test_start_job_requires_at_least_one_destination(state):
    req = {
        "source_card_label": "TESTCARD", "source_path": "/card", "card_insert_date": "2026-07-14T00:00:00Z",
        "event_name": "GameDay", "manual_date": None, "template": _template(), "files": [], "destinations": [],
    }
    with pytest.raises(ValueError):
        copy_engine.start_job(state, req)


def test_existing_destination_file_is_not_overwritten(tmp_path, state):
    """A template that omits {Seq} (use_source_filename here) has no
    naming-engine collision scan to fall back on -- if the destination
    already has a same-named file, the copy must skip it and flag an
    error rather than silently truncating whatever was already there."""
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    target_dir = dest_dir / "20260714_GameDay"
    target_dir.mkdir()
    preexisting = target_dir / "clip1.mov"
    preexisting.write_bytes(b"precious pre-existing content")

    files = _make_source_files(source_dir, [("clip1.mov", "mov", b"new incoming content")])
    template = _template(use_source_filename=True, file_template="{OriginalName}.{ext}")

    req = {
        "source_card_label": "TESTCARD", "source_path": str(source_dir),
        "card_insert_date": "2026-07-14T00:00:00Z", "event_name": "GameDay",
        "manual_date": None, "template": template, "files": files,
        "destinations": [str(dest_dir)],
    }
    handle = copy_engine.start_job(state, req)
    status = _wait_for_completion(state, handle["job_id"])

    dest = status["destinations"][0]
    assert dest["status"] == "failed", dest
    assert dest["files_copied"] == 0
    assert dest["files_verified"] == 0
    # The pre-existing file at the destination must be untouched.
    assert preexisting.read_bytes() == b"precious pre-existing content"


def test_source_file_vanishing_pauses_destination_resumably(tmp_path, state):
    """A file whose path can't be opened at copy time (card removed
    mid-copy, in the real world) must PAUSE that destination with a
    descriptive error rather than failing the whole job outright -- a
    pulled card is often reinsertable, so the job stays resumable."""
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    files = [{
        "path": str(tmp_path / "source" / "missing.mov"), "relative_folder": "",
        "name": "missing.mov", "ext": "mov", "size_bytes": 10,
        "created_at": None, "created_at_source": "unavailable",
    }]
    req = {
        "source_card_label": "TESTCARD", "source_path": str(tmp_path / "source"),
        "card_insert_date": "2026-07-14T00:00:00Z", "event_name": "GameDay",
        "manual_date": None, "template": _template(), "files": files,
        "destinations": [str(dest_dir)],
    }
    handle = copy_engine.start_job(state, req)
    status = _wait_for_completion(state, handle["job_id"])
    dest = status["destinations"][0]
    assert dest["status"] == "paused", dest
    assert "Lost access to source card" in (dest["error_message"] or ""), dest


def test_forced_hash_mismatch_marks_destination_failed(tmp_path, state, monkeypatch):
    """Forces verify_pair to always report a mismatch (simulating on-disk
    corruption survivng the copy) and confirms the one-retry-then-fail
    contract: the file ends up unverified with a descriptive error, and the
    whole destination (and job) rolls up to 'failed', never silently
    reported as done."""
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    files = _make_source_files(source_dir, [("clip1.mov", "mov", b"clip content")])

    monkeypatch.setattr(
        copy_engine.verify, "verify_pair",
        lambda src, dst: {"hash_source": "aaa", "hash_dest": "bbb", "matched": False},
    )

    req = {
        "source_card_label": "TESTCARD", "source_path": str(source_dir),
        "card_insert_date": "2026-07-14T00:00:00Z", "event_name": "GameDay",
        "manual_date": None, "template": _template(), "files": files,
        "destinations": [str(dest_dir)],
    }
    handle = copy_engine.start_job(state, req)
    status = _wait_for_completion(state, handle["job_id"])
    dest = status["destinations"][0]
    assert dest["status"] == "failed", dest
    assert dest["files_verified"] == 0


def test_mark_card_safe_check(tmp_path, state):
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    files = _make_source_files(source_dir, [("clip1.mov", "mov", b"content")])
    req = {
        "source_card_label": "TESTCARD", "source_path": str(source_dir),
        "card_insert_date": "2026-07-14T00:00:00Z", "event_name": "GameDay",
        "manual_date": None, "template": _template(), "files": files,
        "destinations": [str(dest_dir)],
    }
    handle = copy_engine.start_job(state, req)
    _wait_for_completion(state, handle["job_id"])
    assert copy_engine.mark_card_safe_check(state, str(source_dir)) is True
    assert copy_engine.mark_card_safe_check(state, "/never/imported") is True  # vacuously true: no destinations at all


def test_mark_card_safe_check_false_while_incomplete(tmp_path, state):
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    files = _make_source_files(source_dir, [("clip1.mov", "mov", b"content")])
    req = {
        "source_card_label": "TESTCARD", "source_path": str(source_dir),
        "card_insert_date": "2026-07-14T00:00:00Z", "event_name": "GameDay",
        "manual_date": None, "template": _template(), "files": files,
        "destinations": [str(dest_dir)],
    }
    handle = copy_engine.start_job(state, req)
    # Deliberately don't wait for completion -- mark_card_safe_check must
    # report False while the destination is still queued/running.
    assert copy_engine.mark_card_safe_check(state, str(source_dir)) is False
    _wait_for_completion(state, handle["job_id"])


# ---------------------------------------------------------------------------
# Jobs-drawer integration
# ---------------------------------------------------------------------------

def test_list_as_generic_jobs_and_clear_finished(tmp_path, state):
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    files = _make_source_files(source_dir, [("clip1.mov", "mov", b"content")])
    req = {
        "source_card_label": "TESTCARD", "source_path": str(source_dir),
        "card_insert_date": "2026-07-14T00:00:00Z", "event_name": "GameDay",
        "manual_date": None, "template": _template(), "files": files,
        "destinations": [str(dest_dir)],
    }
    handle = copy_engine.start_job(state, req)
    _wait_for_completion(state, handle["job_id"])

    jobs = copy_engine.list_as_generic_jobs(state)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["kind"] == "cardeater_copy"
    assert job["status"] == "done"
    assert job["progress"] == 100.0
    assert "TESTCARD" in job["label"] and os.path.basename(str(dest_dir)) in job["label"]

    copy_engine.clear_finished(state)
    assert copy_engine.list_as_generic_jobs(state) == []

    # History is untouched by clear_finished -- only the session-visible set is pruned.
    from backend import cardeater_db as db
    with state.db.lock:
        history = db.list_job_summaries(state.db.conn)
    assert len(history) == 1


def test_list_as_generic_jobs_empty_when_no_jobs_started(state):
    assert copy_engine.list_as_generic_jobs(state) == []


def test_clear_finished_is_a_noop_with_no_jobs(state):
    copy_engine.clear_finished(state)  # must not raise
