"""braw_bridge.py -- Phase 0 (SDK/runtime detection, always graceful) and
Phase 1 (proxy generation as a background job). No real Blackmagic RAW
SDK or sample .braw media is used or required: detection is exercised by
monkeypatching the candidate-path list / paths.BRAW_TOOL_BIN, and proxy
generation is exercised against a small fake stand-in "tool" script that
speaks the documented worker_protocol JSON-lines contract (see
tools/braw/README.md) -- exactly what a real compiled tool would need to
satisfy, without needing the proprietary SDK to build one."""

import os
import time

from backend import paths, braw_bridge, braw_proxy_cache
from backend.jobs import JobManager


def _redirect_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PROXIES_DIR", str(tmp_path / "proxies"))
    monkeypatch.setattr(paths, "BRAW_TOOL_BIN", str(tmp_path / "no_such_tool"))
    monkeypatch.setattr(braw_bridge, "_SDK_RUNTIME_CANDIDATES", (str(tmp_path / "no_such_sdk"),))


# ---------------------------------------------------------------------------
# Phase 0 -- detection
# ---------------------------------------------------------------------------

def test_sdk_runtime_path_is_none_when_no_candidate_exists(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    assert braw_bridge.sdk_runtime_path() is None


def test_sdk_runtime_path_finds_the_first_existing_candidate(monkeypatch, tmp_path):
    present = tmp_path / "BlackmagicRawAPI.framework"
    present.mkdir()
    monkeypatch.setattr(braw_bridge, "_SDK_RUNTIME_CANDIDATES",
                        (str(tmp_path / "missing"), str(present)))
    assert braw_bridge.sdk_runtime_path() == str(present)


def test_tool_built_is_false_when_binary_is_missing(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    assert braw_bridge.tool_built() is False


def test_tool_built_is_false_when_present_but_not_executable(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    tool = tmp_path / "braw_proxy_tool"
    tool.write_text("not actually executable")
    tool.chmod(0o644)
    monkeypatch.setattr(paths, "BRAW_TOOL_BIN", str(tool))
    assert braw_bridge.tool_built() is False


def test_tool_built_is_true_for_an_executable_file(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    tool = tmp_path / "braw_proxy_tool"
    tool.write_text("#!/bin/sh\necho hi\n")
    tool.chmod(0o755)
    monkeypatch.setattr(paths, "BRAW_TOOL_BIN", str(tool))
    assert braw_bridge.tool_built() is True


def test_braw_available_false_when_neither_half_present(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    assert braw_bridge.braw_available() is False


def test_unavailable_reason_mentions_missing_runtime_first(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    reason = braw_bridge.unavailable_reason()
    assert reason is not None
    assert "install" in reason.lower()


def test_unavailable_reason_mentions_tool_when_only_sdk_present(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    monkeypatch.setattr(braw_bridge, "_SDK_RUNTIME_CANDIDATES", (str(sdk_dir),))
    reason = braw_bridge.unavailable_reason()
    assert reason is not None
    assert "hasn't been built" in reason


def test_status_reports_both_flags_independently(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    st = braw_bridge.status()
    assert st == {
        "available": False,
        "sdk_runtime_found": False,
        "sdk_runtime_path": None,
        "tool_built": False,
        "tool_path": str(tmp_path / "no_such_tool"),
    }


def test_braw_available_true_when_both_present(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    monkeypatch.setattr(braw_bridge, "_SDK_RUNTIME_CANDIDATES", (str(sdk_dir),))
    tool = tmp_path / "braw_proxy_tool"
    tool.write_text("#!/bin/sh\necho hi\n")
    tool.chmod(0o755)
    monkeypatch.setattr(paths, "BRAW_TOOL_BIN", str(tool))
    assert braw_bridge.braw_available() is True
    assert braw_bridge.unavailable_reason() is None


# ---------------------------------------------------------------------------
# Phase 1 -- proxy generation job
# ---------------------------------------------------------------------------

def make_source(tmp_path, name="clip.braw"):
    p = tmp_path / name
    p.write_bytes(b"\x00" * 32)
    return str(p)


def _make_fake_tool(tmp_path, *, exit_code=0, emit_result=True, sleep_seconds=0.0):
    """A stand-in for the not-yet-built compiled tool: a tiny Python
    script that speaks worker_protocol's exact JSON-lines contract (see
    tools/braw/README.md), so exercising braw_bridge._run_proxy_tool
    against it validates the real contract without needing the SDK."""
    tool = tmp_path / "fake_braw_tool.py"
    tool.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, time\n"
        "src, out = sys.argv[1], sys.argv[2]\n"
        f"time.sleep({sleep_seconds})\n"
        "print(json.dumps({'type': 'progress', 'progress': 50, 'detail': 'decoding'}), flush=True)\n"
        + ("open(out, 'wb').write(b'fake proxy bytes')\n" if emit_result else "")
        + ("print(json.dumps({'type': 'result', 'data': {}}), flush=True)\n" if emit_result
           else "print(json.dumps({'type': 'error', 'message': 'boom'}), flush=True)\n")
        + f"sys.exit({exit_code})\n"
    )
    tool.chmod(0o755)
    return str(tool)


def _wait_for_job(manager, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get_job_dict(job_id)
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def test_request_proxy_rejects_a_missing_file(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    manager = JobManager()
    res = braw_bridge.request_proxy(manager, str(tmp_path / "nope.braw"))
    assert res == {"ok": False, "error": f"File not found: {tmp_path / 'nope.braw'}"}


def test_request_proxy_rejects_a_non_braw_extension(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    manager = JobManager()
    not_braw = tmp_path / "clip.mov"
    not_braw.write_bytes(b"\x00")
    res = braw_bridge.request_proxy(manager, str(not_braw))
    assert res == {"ok": False, "error": "Not a .braw file."}


def test_request_proxy_returns_cached_proxy_without_starting_a_job(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    source = make_source(tmp_path)
    proxy_path = braw_proxy_cache.proxy_output_path(source)
    os.makedirs(os.path.dirname(proxy_path), exist_ok=True)
    open(proxy_path, "wb").write(b"already cached")
    braw_proxy_cache.register_proxy(source, proxy_path)

    manager = JobManager()
    res = braw_bridge.request_proxy(manager, source)

    assert res == {"ok": True, "cached": True, "proxy_path": proxy_path}
    assert manager.list_jobs() == []  # no job needed for an already-cached proxy


def test_request_proxy_errors_when_braw_unavailable(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)  # no SDK, no tool
    manager = JobManager()
    source = make_source(tmp_path)
    res = braw_bridge.request_proxy(manager, source)
    assert res["ok"] is False
    assert "install" in res["error"].lower()


def test_request_proxy_runs_the_tool_and_registers_the_result(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    monkeypatch.setattr(braw_bridge, "_SDK_RUNTIME_CANDIDATES", (str(sdk_dir),))
    monkeypatch.setattr(paths, "BRAW_TOOL_BIN", _make_fake_tool(tmp_path))

    manager = JobManager()
    source = make_source(tmp_path)
    res = braw_bridge.request_proxy(manager, source)
    assert res["ok"] is True
    job_id = res["job_id"]

    job = _wait_for_job(manager, job_id)
    assert job["status"] == "done", job
    proxy_path = job["result"]["proxy_path"]
    assert os.path.isfile(proxy_path)

    # Registered in the cache -- a second request now short-circuits to it.
    second = braw_bridge.request_proxy(manager, source)
    assert second == {"ok": True, "cached": True, "proxy_path": proxy_path}


def test_request_proxy_job_reports_error_on_tool_failure(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    monkeypatch.setattr(braw_bridge, "_SDK_RUNTIME_CANDIDATES", (str(sdk_dir),))
    monkeypatch.setattr(paths, "BRAW_TOOL_BIN",
                        _make_fake_tool(tmp_path, exit_code=1, emit_result=False))

    manager = JobManager()
    source = make_source(tmp_path)
    res = braw_bridge.request_proxy(manager, source)
    job = _wait_for_job(manager, res["job_id"])

    assert job["status"] == "error"
    assert job["error"] == "boom"
    assert braw_proxy_cache.find_cached_proxy(source) is None


def test_braw_proxy_jobs_are_throttled_to_one_at_a_time(monkeypatch, tmp_path):
    """Matches jobs.py's _kind_limits entry for "braw_proxy" (mirrors
    "transcribe"'s existing throttle) -- a second proxy job queues rather
    than running concurrently."""
    _redirect_dirs(monkeypatch, tmp_path)
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    monkeypatch.setattr(braw_bridge, "_SDK_RUNTIME_CANDIDATES", (str(sdk_dir),))
    monkeypatch.setattr(paths, "BRAW_TOOL_BIN", _make_fake_tool(tmp_path, sleep_seconds=0.3))

    manager = JobManager()
    manager.set_kind_limit("braw_proxy", 1)
    source_a = make_source(tmp_path, "a.braw")
    source_b = make_source(tmp_path, "b.braw")

    res_a = braw_bridge.request_proxy(manager, source_a)
    res_b = braw_bridge.request_proxy(manager, source_b)
    assert res_a["ok"] and res_b["ok"]

    job_b = manager.get_job_dict(res_b["job_id"])
    assert job_b["status"] == "queued"

    _wait_for_job(manager, res_a["job_id"])
    job_b = _wait_for_job(manager, res_b["job_id"])
    assert job_b["status"] == "done"


# ---------------------------------------------------------------------------
# Phase 2 -- Edit-workspace substitution's one sibling-file dependency
# ---------------------------------------------------------------------------

def test_rcs_video_extensions_includes_braw():
    """Guards the one approved sibling-file edit (Rough Cut Studio/
    backend/transcript_parser.py's VIDEO_EXTENSIONS) against an
    accidental future revert -- RCS has no pytest suite of its own to
    catch this, and without .braw in this tuple a linked .braw source is
    pruned by api_security.py's _suite_prune_disallowed_media (and by
    RCS's own project-load gate) before any Edit-preview substitution
    code ever runs."""
    import transcript_parser  # RCS's own module; on sys.path via paths.RCS_BACKEND_DIR
    assert ".braw" in transcript_parser.VIDEO_EXTENSIONS


# ---------------------------------------------------------------------------
# Phase 3 -- extension-allowlist gating (dialog filters)
# ---------------------------------------------------------------------------

def test_transcriber_video_dialog_types_includes_braw():
    from backend import api_shared
    assert "*.braw" in api_shared.VIDEO_DIALOG_TYPES[0]


def test_sync_video_dialog_types_includes_braw():
    from backend import api_shared
    assert "*.braw" in api_shared.SYNC_VIDEO_DIALOG_TYPES[0]


def test_rcs_link_media_file_dialog_includes_braw():
    """link_media_file's file_types filter is a literal inside the
    function body (not a module constant, since it's RCS's own file we
    only ever get one narrow approved edit into at a time) -- guard it
    via source inspection so an accidental future revert of this second
    sibling-file line is still caught, same rationale as
    test_rcs_video_extensions_includes_braw above."""
    import inspect
    import sources  # RCS's own module; on sys.path via paths.RCS_BACKEND_DIR
    source = inspect.getsource(sources.SourceManager.link_media_file)
    assert "*.braw" in source


# ---------------------------------------------------------------------------
# Addendum v54 -- proxy job failure signal
# ---------------------------------------------------------------------------

def test_request_proxy_records_a_findable_failure_on_tool_error(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    monkeypatch.setattr(braw_bridge, "_SDK_RUNTIME_CANDIDATES", (str(sdk_dir),))
    monkeypatch.setattr(paths, "BRAW_TOOL_BIN",
                        _make_fake_tool(tmp_path, exit_code=1, emit_result=False))

    manager = JobManager()
    source = make_source(tmp_path)
    assert braw_proxy_cache.find_proxy_failure(source) is None

    res = braw_bridge.request_proxy(manager, source)
    job = _wait_for_job(manager, res["job_id"])
    assert job["status"] == "error"

    assert braw_proxy_cache.find_proxy_failure(source) == "boom"


def test_request_proxy_clears_a_stale_failure_before_a_fresh_attempt(monkeypatch, tmp_path):
    _redirect_dirs(monkeypatch, tmp_path)
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    monkeypatch.setattr(braw_bridge, "_SDK_RUNTIME_CANDIDATES", (str(sdk_dir),))
    source = make_source(tmp_path)

    monkeypatch.setattr(paths, "BRAW_TOOL_BIN",
                        _make_fake_tool(tmp_path, exit_code=1, emit_result=False))
    manager = JobManager()
    first = braw_bridge.request_proxy(manager, source)
    _wait_for_job(manager, first["job_id"])
    assert braw_proxy_cache.find_proxy_failure(source) == "boom"

    # A retry (e.g. the user re-runs Analyze) must not be shadowed by the
    # previous attempt's failure -- request_proxy clears it up front, the
    # instant a fresh job is actually started.
    monkeypatch.setattr(paths, "BRAW_TOOL_BIN", _make_fake_tool(tmp_path))
    second = braw_bridge.request_proxy(manager, source)
    assert braw_proxy_cache.find_proxy_failure(source) is None
    second_job = _wait_for_job(manager, second["job_id"])
    assert second_job["status"] == "done"


def test_wait_for_decode_path_fails_fast_on_a_recorded_failure(monkeypatch, tmp_path):
    """The whole point: a genuinely crashed/errored proxy must not make a
    waiter sit out its full timeout -- confirmed here with a timeout far
    longer than this test's own runtime budget, so a regression back to
    "just poll find_cached_proxy and time out" would make this test hang
    instead of quietly passing."""
    monkeypatch.setattr(paths, "PROXIES_DIR", str(tmp_path / "proxies"))
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    monkeypatch.setattr(braw_bridge, "_SDK_RUNTIME_CANDIDATES", (str(sdk_dir),))
    monkeypatch.setattr(paths, "BRAW_TOOL_BIN", str(tmp_path / "fake_tool"))
    with open(paths.BRAW_TOOL_BIN, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(paths.BRAW_TOOL_BIN, 0o755)

    clip_path = str(tmp_path / "clip.braw")
    with open(clip_path, "wb") as f:
        f.write(b"\x00" * 16)
    braw_proxy_cache.record_proxy_failure(clip_path, "simulated crash")

    started = time.time()
    decode_path, err = braw_bridge.wait_for_decode_path(
        clip_path, timeout=60.0, poll_interval=0.05)
    elapsed = time.time() - started

    assert decode_path is None
    assert err == "BRAW proxy generation failed: simulated crash"
    assert elapsed < 5.0, "must fail fast, not wait out the 60s timeout"


def test_resolve_decode_path_reports_a_recorded_failure_instead_of_the_generic_message(
        monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PROXIES_DIR", str(tmp_path / "proxies"))
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    monkeypatch.setattr(braw_bridge, "_SDK_RUNTIME_CANDIDATES", (str(sdk_dir),))
    monkeypatch.setattr(paths, "BRAW_TOOL_BIN", str(tmp_path / "fake_tool"))
    with open(paths.BRAW_TOOL_BIN, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(paths.BRAW_TOOL_BIN, 0o755)

    clip_path = str(tmp_path / "clip.braw")
    with open(clip_path, "wb") as f:
        f.write(b"\x00" * 16)
    braw_proxy_cache.record_proxy_failure(clip_path, "simulated crash")

    decode_path, err = braw_bridge.resolve_decode_path(clip_path)
    assert decode_path is None
    assert err == "BRAW proxy generation failed: simulated crash"


# ---------------------------------------------------------------------------
# Addendum v55 -- .braw sidecar fallback location
# ---------------------------------------------------------------------------

def test_ivt_cache_path_unchanged_for_an_ordinary_video():
    assert braw_bridge.ivt_cache_path("/movies/interview.mp4") == \
        "/movies/interview.mp4.ivt-cache.json"


def test_sync_offsets_path_unchanged_for_an_ordinary_video():
    assert braw_bridge.sync_offsets_path("/movies/interview.mp4") == \
        "/movies/interview.mp4.sync-offsets.json"


def test_ivt_cache_path_redirects_a_braw_video_to_the_fallback_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IVT_CACHE_DIR", str(tmp_path / "ivt_cache"))
    result = braw_bridge.ivt_cache_path("/media/card/A001.braw")
    assert os.path.dirname(result) == str(tmp_path / "ivt_cache")
    assert result.endswith(".ivt-cache.json")
    assert "A001" not in result, "must be hash-keyed, not filename-derived"


def test_sync_offsets_path_redirects_a_braw_video_to_the_fallback_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IVT_CACHE_DIR", str(tmp_path / "ivt_cache"))
    result = braw_bridge.sync_offsets_path("/media/card/A001.braw")
    assert os.path.dirname(result) == str(tmp_path / "ivt_cache")
    assert result.endswith(".sync-offsets.json")


def test_braw_sidecar_fallback_paths_are_deterministic_and_distinct(monkeypatch, tmp_path):
    """Same input -> same output (a second caller must find what the
    first one wrote), and the two sidecar kinds for the SAME video must
    never collide with each other."""
    monkeypatch.setattr(paths, "IVT_CACHE_DIR", str(tmp_path / "ivt_cache"))
    video = "/media/card/A001.braw"
    assert braw_bridge.ivt_cache_path(video) == braw_bridge.ivt_cache_path(video)
    assert braw_bridge.ivt_cache_path(video) != braw_bridge.sync_offsets_path(video)
