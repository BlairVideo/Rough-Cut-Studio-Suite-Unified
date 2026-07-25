"""
braw_bridge.py — BRAW (Blackmagic RAW) compatibility bridge.

Unlike brander_bridge.py (which imports a SIBLING APP's own pure-Python
modules in-process), there is no sibling app here to import: Blackmagic's
RAW SDK is a proprietary, closed-source dependency the suite never
vendors or links directly into its own process. Per this project's
"local tool fallback" convention (the one non-open-source dependency
this whole feature needs), this bridge instead:

  1. (Phase 0) detects whether the Blackmagic RAW runtime is installed
     on this machine at all (bundled with DaVinci Resolve, or the free
     standalone "Blackmagic RAW" installer) — degrading gracefully,
     never raising, when it isn't.
  2. (Phase 1) shells out to a small compiled CLI helper
     (paths.BRAW_TOOL_BIN, contract documented in tools/braw/README.md)
     to transcode a .braw source into a cached, ordinary-container proxy
     that every existing ffmpeg/ffprobe/cv2/<video> code path in this
     suite can already handle — mirroring the "own process, own venv"
     isolation every other heavy dependency in this suite already uses
     (A-Sync/Transcriber/B-Roll each run in their own venv subprocess;
     this is the same idea for a proprietary SDK instead of a Python
     one). That helper does not exist yet (building it needs Xcode +
     the actual SDK headers, neither available in every dev
     environment) — braw_available() simply reports False until it is
     built and installed, which is a normal, fully-supported state, not
     an error.

  3. (Phase 2, B-Roll workspace only so far) `find_braw_files()` is the
     one shared ".braw folder scan" implementation, called from BOTH the
     suite's own process (api_broll.py, to pre-flight-queue proxy jobs
     before analysis starts) and B-Roll Analyzer's own venv subprocess
     (broll_worker.py, via a sys.path insert — this module is stdlib +
     paths.py/braw_proxy_cache.py only, so it imports cleanly there too)
     to resolve a .braw path to its cached proxy before decoding. This
     keeps ALL BRAW-awareness suite-side: B-Roll Analyzer's own
     analyzer.py is never modified, never even knows a proxy exists — it
     just gets handed an ordinary, already-playable path to decode, same
     as any other clip. Sync/Transcribe/Edit-preview substitution follow
     the same pattern in later passes.
"""

import os
import hashlib
import subprocess
import threading
import time

try:
    from . import paths, braw_proxy_cache
    from .workers import worker_protocol
except ImportError:  # pragma: no cover — direct script import in tests
    import paths
    import braw_proxy_cache
    from workers import worker_protocol

PROXY_JOB_KIND = "braw_proxy"

# ---------------------------------------------------------------------------
# Phase 0 — SDK/runtime detection
# ---------------------------------------------------------------------------

# macOS install locations for Blackmagic's RAW runtime (the
# "BlackmagicRawAPI.framework" bundle every first- and third-party
# BRAW-aware tool loads at runtime). The first three are CONFIRMED
# against a real "Blackmagic RAW" + DaVinci Resolve Studio install (this
# suite's own dev machine, addendum v28 follow-up) — every video editor
# who has installed either one already has the runtime, they just may
# not know it. The last two are the un-confirmed original guesses, kept
# as a fallback in case Blackmagic's installer layout differs elsewhere
# (e.g. an older/newer SDK version, or a non-Studio Resolve build).
_SDK_RUNTIME_CANDIDATES = (
    # The free standalone "Blackmagic RAW" installer's own Player app —
    # confirmed present alongside the SDK itself under /Applications/
    # Blackmagic RAW/.
    "/Applications/Blackmagic RAW/Blackmagic RAW Player.app/Contents/Frameworks/BlackmagicRawAPI.framework",
    # The SDK download's own bundled copy (Mac/Libraries/) — present
    # whenever the SDK itself is installed, independent of whether the
    # Player app or Resolve are.
    "/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac/Libraries/BlackmagicRawAPI.framework",
    # Bundled inside DaVinci Resolve Studio.
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Frameworks/BlackmagicRawAPI.framework",
    # Unconfirmed fallbacks — non-Studio Resolve naming, and a
    # hypothetical system-wide Frameworks install some installer
    # versions may use instead of a per-app bundle copy.
    "/Applications/DaVinci Resolve Studio/DaVinci Resolve Studio.app/Contents/Frameworks/BlackmagicRawAPI.framework",
    "/Library/Frameworks/BlackmagicRawAPI.framework",
)


def sdk_runtime_path():
    """First existing candidate install of the Blackmagic RAW runtime,
    or None if none are present on this machine."""
    for candidate in _SDK_RUNTIME_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def tool_built():
    """Whether this suite's own compiled proxy-generation helper exists
    and is executable. False in every dev environment until someone
    builds it against the real SDK (see tools/braw/README.md)."""
    return os.path.isfile(paths.BRAW_TOOL_BIN) and os.access(paths.BRAW_TOOL_BIN, os.X_OK)


def braw_available():
    """True only when BOTH the Blackmagic RAW runtime is installed AND
    this suite's own proxy tool has been built — everything BRAW-aware
    in this suite must gate on this (or on unavailable_reason() being
    None) rather than assuming either half implies the other."""
    return sdk_runtime_path() is not None and tool_built()


def status():
    """Diagnostic snapshot — every field the future Settings/About panel
    or an error message needs to explain WHICH half is missing."""
    sdk_path = sdk_runtime_path()
    tool_ok = tool_built()
    return {
        "available": sdk_path is not None and tool_ok,
        "sdk_runtime_found": sdk_path is not None,
        "sdk_runtime_path": sdk_path,
        "tool_built": tool_ok,
        "tool_path": paths.BRAW_TOOL_BIN,
    }


def unavailable_reason():
    """Human-readable sentence for the same {"ok": False, "error": ...}
    contract every other optional-dependency call site in this suite
    uses (e.g. api_broll.py's BROLL_PYTHON-missing check) — None if BRAW
    support is fully available."""
    st = status()
    if st["available"]:
        return None
    if not st["sdk_runtime_found"]:
        return ("Blackmagic RAW isn't installed on this machine — install "
                "DaVinci Resolve or the free Blackmagic RAW SDK/runtime, "
                "then relaunch Studio Suite.")
    return ("The Blackmagic RAW runtime was found, but Studio Suite's BRAW "
            f"proxy tool hasn't been built yet (expected at {paths.BRAW_TOOL_BIN}).")


# ---------------------------------------------------------------------------
# Phase 2 — shared .braw discovery (suite process AND sibling-venv workers)
# ---------------------------------------------------------------------------

BRAW_EXTENSION = ".braw"


def find_braw_files(folder):
    """Recursive .braw scan of `folder`, mirroring B-Roll Analyzer's own
    analyzer.find_video_files() os.walk semantics (join order,
    case-insensitive extension match) so a caller can freely combine the
    two lists (sorted(analyzer.find_video_files(folder) +
    find_braw_files(folder))) and get one consistent, folder-relative-
    path-friendly ordering. The ONE implementation of this scan — callers
    are api_broll.py (suite venv) and broll_worker.py (B-Roll Analyzer's
    own venv, via a sys.path insert for this stdlib-only module) — so a
    future Sync/Transcribe caller reuses this instead of a third copy."""
    found = []
    for root, _, names in os.walk(folder):
        for name in names:
            if os.path.splitext(name)[1].lower() == BRAW_EXTENSION:
                found.append(os.path.join(root, name))
    return found


# ---------------------------------------------------------------------------
# Addendum v55 — .braw sidecar fallback location (Transcribe/Sync)
# ---------------------------------------------------------------------------
# .ivt-cache.json and .sync-offsets.json normally live NEXT TO their video
# (matching the standalone Local Interview Transcriber's own convention,
# for interop) — but a .braw source routinely lives on read-only/
# removable camera media where writing ANYTHING next to it isn't
# reliable (the same reasoning that already centralizes proxies under
# assets/proxies/ instead of next to the source). Centralized here
# instead, for .braw sources ONLY — an ordinary video's sidecars are
# untouched, still written next to the source exactly as before. No
# interop is lost: the standalone transcriber can't decode .braw at all,
# so it was never going to read either sidecar for one anyway.
#
# Unlike braw_proxy_cache.py's proxy index, no index file is needed here:
# a video's fallback path is always recomputed deterministically from
# `video_path` itself by whichever caller already has that path in hand
# (never reverse-looked-up from the sidecar back to its source).

# Duplicated by hand from api_shared.py's IVT_CACHE_SUFFIX/
# SYNC_OFFSETS_SUFFIX (this module stays stdlib + paths.py/
# braw_proxy_cache.py only, so it can't import api_shared — same
# hand-duplication precedent as api_shared.py's own WHISPER_MODELS
# comment, kept in sync by hand across that import boundary).
_IVT_CACHE_SUFFIX = ".ivt-cache.json"
_SYNC_OFFSETS_SUFFIX = ".sync-offsets.json"


def _braw_sidecar_fallback_path(video_path, suffix):
    paths.ensure_suite_dirs()
    key = hashlib.sha1(os.path.abspath(video_path).encode("utf-8")).hexdigest()
    return os.path.join(paths.IVT_CACHE_DIR, key + suffix)


def ivt_cache_path(video_path):
    """Where <video>.ivt-cache.json lives for THIS video: next to it
    normally, or the centralized fallback if it's a .braw source."""
    if os.path.splitext(video_path)[1].lower() == BRAW_EXTENSION:
        return _braw_sidecar_fallback_path(video_path, _IVT_CACHE_SUFFIX)
    return video_path + _IVT_CACHE_SUFFIX


def sync_offsets_path(video_path):
    """Same idea as ivt_cache_path, for <video>.sync-offsets.json."""
    if os.path.splitext(video_path)[1].lower() == BRAW_EXTENSION:
        return _braw_sidecar_fallback_path(video_path, _SYNC_OFFSETS_SUFFIX)
    return video_path + _SYNC_OFFSETS_SUFFIX


# ---------------------------------------------------------------------------
# Phase 1 — proxy generation as a background job
# ---------------------------------------------------------------------------

find_cached_proxy = braw_proxy_cache.find_cached_proxy  # convenience re-export
cache_usage = braw_proxy_cache.cache_usage              # convenience re-export
clear_cache = braw_proxy_cache.clear_cache               # convenience re-export


# How long a per-file worker will wait for a proxy that's known to be in
# progress (queue_missing_proxies already started it) before giving up.
# Effectively unbounded (addendum v48): "braw_proxy" jobs are throttled
# to a couple running at once (jobs.py), so in a folder with several
# BRAW files a given proxy job may sit legitimately QUEUED behind others
# for longer than any fixed guess could cover — a worker waiting on it
# has no way to tell "still queued" apart from "actually stuck" (this
# runs in a separate venv subprocess with no visibility into the
# suite's own JobManager, only the on-disk proxy cache it polls below).
# Safe to wait this long precisely because the wait always runs inside
# an already-parallel per-file worker (see wait_for_decode_path's
# docstring), never the single discovery/dispatch loop, so one slow
# proxy still never stalls unrelated files.
BRAW_PROXY_WAIT_TIMEOUT_SECONDS = 86400.0
BRAW_PROXY_POLL_INTERVAL_SECONDS = 1.0


def resolve_decode_path(path):
    """For an ordinary file, the decode path IS the file. For a .braw
    file, resolve to its cached proxy — generated ahead of time by the
    SUITE's own process (via request_proxy(), which has direct access to
    the JobManager), never by the calling worker itself: actually
    generating a proxy shells out to the compiled BRAW SDK tool, which
    belongs in its own labeled "braw_proxy" Jobs-drawer entry, not hidden
    inside an already-running analyze/sync/transcribe job.

    Returns (decode_path, None) or (None, human_readable_error) — an
    IMMEDIATE check, no waiting. Only safe to call from a place where a
    "not ready yet" answer is fine to fail fast on. For a per-file worker
    that runs concurrently with other files (B-Roll's pool child,
    called from a single already-parallel slot), use
    wait_for_decode_path() instead — see its docstring for why: the
    fire-and-forget queue_missing_proxies() call and the analyze/sync job
    that follows it start at the same time with NO ordering guarantee,
    so an immediate check here races the proxy job and — for a small/
    first-time file — usually loses."""
    if os.path.splitext(path)[1].lower() != BRAW_EXTENSION:
        return path, None
    proxy_path = find_cached_proxy(path)
    if proxy_path is not None:
        return proxy_path, None
    reason = unavailable_reason()
    if reason is not None:
        return None, reason
    failure = braw_proxy_cache.find_proxy_failure(path)
    if failure is not None:
        return None, f"BRAW proxy generation failed: {failure}"
    return None, ("This BRAW clip's proxy hasn't finished generating yet — "
                   "check the Jobs drawer, then re-run once it's done.")


def wait_for_decode_path(path, timeout=BRAW_PROXY_WAIT_TIMEOUT_SECONDS,
                          poll_interval=BRAW_PROXY_POLL_INTERVAL_SECONDS):
    """Like resolve_decode_path, but when BRAW IS available and the
    proxy simply hasn't finished generating yet, polls for up to
    `timeout` seconds instead of failing immediately — closing the race
    documented on resolve_decode_path: api_broll.py's broll_start (and
    api_sync.py's sync_start/sync_probe/sync_peaks) queue a proxy job and
    start their own analyze/sync job in the SAME call, with no ordering
    guarantee between the two, so the proxy is very often still mid-
    generation by the time a worker first checks for it.

    ONLY call this from somewhere the wait can't stall unrelated work —
    a per-file pool child process (broll_worker.py's _analyze_one) or a
    worker that's already inherently sequential per file anyway
    (sync_worker.py). Never call it from a single discovery/dispatch loop
    that every other file's processing is queued behind, or one slow
    proxy serializes the whole batch behind it.

    Addendum v54: also polls for a recorded proxy FAILURE (a genuinely
    crashed/errored proxy job, not just one still queued behind others)
    and returns that error immediately rather than waiting out the full
    timeout — closing the "still not fixed" gap addendum v48 flagged.

    Returns (decode_path, None) or (None, human_readable_error)."""
    if os.path.splitext(path)[1].lower() != BRAW_EXTENSION:
        return path, None
    reason = unavailable_reason()
    if reason is not None:
        return None, reason
    proxy_path = find_cached_proxy(path)
    failure = braw_proxy_cache.find_proxy_failure(path)
    deadline = time.time() + timeout
    while proxy_path is None and failure is None and time.time() < deadline:
        time.sleep(poll_interval)
        proxy_path = find_cached_proxy(path)
        failure = braw_proxy_cache.find_proxy_failure(path)
    if failure is not None:
        return None, f"BRAW proxy generation failed: {failure}"
    if proxy_path is None:
        return None, ("This BRAW clip's proxy is taking longer than expected to "
                       "generate — check the Jobs drawer, then re-run once it's done.")
    return proxy_path, None


def request_proxy(job_manager, source_path, on_done=None):
    """Ensure a playable proxy exists for `source_path` (a .braw file),
    starting a background "braw_proxy" job if no current cached proxy
    already covers it. Returns one of:
        {"ok": True, "cached": True, "proxy_path": ...}   already have one
        {"ok": True, "job_id": ...}                        job started
        {"ok": False, "error": ...}                        bad input, or
                                                            BRAW unavailable
    Mirrors the pre-flight-check + start_*_job shape every other
    subprocess-backed workspace already uses (e.g. api_broll.py's
    broll_start checking paths.BROLL_PYTHON before starting a job)."""
    if not source_path or not isinstance(source_path, str) or not os.path.isfile(source_path):
        return {"ok": False, "error": f"File not found: {source_path}"}
    if os.path.splitext(source_path)[1].lower() != ".braw":
        return {"ok": False, "error": "Not a .braw file."}

    cached = braw_proxy_cache.find_cached_proxy(source_path)
    if cached:
        return {"ok": True, "cached": True, "proxy_path": cached}

    reason = unavailable_reason()
    if reason:
        return {"ok": False, "error": reason}

    output_path = braw_proxy_cache.proxy_output_path(source_path)
    # A fresh attempt supersedes whatever a previous one recorded — never
    # let a stale failure shadow this new try (addendum v54).
    braw_proxy_cache.clear_proxy_failure(source_path)

    def run(progress_cb, cancel_event):
        try:
            return _run_proxy_tool(source_path, output_path, progress_cb, cancel_event)
        except Exception as e:
            # A cooperative cancel isn't a real failure -- JobManager
            # already handles that path itself (job.status == "cancelled"
            # short-circuits _run_thread_job before this would even
            # matter); only record genuine errors so wait_for_decode_path
            # elsewhere can bail out fast instead of hanging its timeout.
            if not cancel_event.is_set():
                braw_proxy_cache.record_proxy_failure(source_path, str(e))
            raise

    def _on_done(job):
        braw_proxy_cache.register_proxy(source_path, output_path)
        if on_done is not None:
            on_done(job)

    job_id = job_manager.start_thread_job(
        kind=PROXY_JOB_KIND,
        label=os.path.basename(source_path),
        fn=run,
        on_done=_on_done,
    )
    return {"ok": True, "job_id": job_id}


def queue_missing_proxies(job_manager, paths):
    """For every .braw path in `paths` lacking a current cached proxy,
    start a "braw_proxy" job (idempotent — request_proxy no-ops into a
    cache hit if one already exists, and silently skips a non-.braw path
    or a still-unavailable SDK/tool rather than erroring the caller's own
    request). Returns the list of newly-queued {"path", "job_id"}
    entries, for a caller to fold into its own response (e.g.
    api_broll.py's broll_start, api_sync.py's sync_start/sync_probe/
    sync_peaks) so a future frontend can watch them in the Jobs drawer.
    Fire-and-forget — never blocks on the jobs it starts."""
    queued = []
    for path in paths:
        if not path or os.path.splitext(path)[1].lower() != BRAW_EXTENSION:
            continue
        res = request_proxy(job_manager, path)
        if res.get("ok") and res.get("job_id"):
            queued.append({"path": path, "job_id": res["job_id"]})
    return queued


def _run_proxy_tool(source_path, output_path, progress_cb, cancel_event):
    """Shell out to the compiled BRAW proxy tool (contract documented in
    tools/braw/README.md): invoked as

        braw_proxy_tool <source_path> <output_path>

    and expected to speak the exact same JSON-lines stdout protocol as
    every Python subprocess worker in this suite
    (backend/workers/worker_protocol.py) — one
    {"type": "progress"|"result"|"error", ...} object per line, so the
    same wire format documented/parsed there covers both a Python
    worker and a compiled one.

    Cooperative cancellation, matching JobManager's own subprocess
    handling: `cancel_event` is polled once per stdout line, and the
    process is terminated (best-effort) if it's set — the thread-job
    contract (jobs.py's _run_thread_job) treats any exception raised
    while cancelled as expected fallout, not a real error."""
    paths.ensure_suite_dirs()
    proc = subprocess.Popen(
        [paths.BRAW_TOOL_BIN, source_path, output_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stderr_lines = []

    def drain_stderr():
        try:
            for line in proc.stderr:
                line = line.rstrip("\n")
                if line.strip():
                    stderr_lines.append(line)
        except Exception:
            pass

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    err_thread.start()

    result_data = None
    worker_error = None
    try:
        for line in proc.stdout:
            if cancel_event.is_set():
                break
            msg = worker_protocol.parse_line(line)
            if msg is None:
                continue  # stray non-protocol output — ignore
            if msg["type"] == worker_protocol.PROGRESS:
                if msg["progress"] is not None:
                    progress_cb(msg["progress"], msg["detail"])
            elif msg["type"] == worker_protocol.RESULT:
                result_data = msg["data"]
            elif msg["type"] == worker_protocol.ERROR:
                worker_error = msg["message"]
    except Exception:
        pass  # pipe closed by termination — exit-code/cancel handling below decides

    if cancel_event.is_set():
        try:
            proc.terminate()
        except Exception:
            pass
        raise RuntimeError("Cancelled")

    proc.wait()
    err_thread.join(timeout=2.0)

    if worker_error is not None:
        raise RuntimeError(worker_error)
    if result_data is None or not os.path.isfile(output_path):
        tail = "\n".join(stderr_lines[-10:]) or \
            f"braw_proxy_tool exited with code {proc.returncode} and produced no output."
        raise RuntimeError(tail[-4000:])
    return {"proxy_path": output_path, "source_path": source_path}
