"""
broll_worker.py — folder analysis / XML export worker for the suite.

RUN WITH THE B-ROLL ANALYZER'S OWN VENV PYTHON (B-Roll Analyzer/
.venv/bin/python) — that's where cv2/numpy/torch/open_clip live; none of
them exist in the suite venv, which is exactly why even the "cheap"
export_xml path goes through this subprocess.

Params arrive as a JSON string in argv[1]. Two modes:

  {"mode": "analyze" (default), "folder": str,
   "window_sec": 4.0, "max_segments": 1, "min_segment_gap_sec": 1.0,
   "enable_energy": false, "energy_weight": 0.35, "max_workers": 3}

  {"mode": "export_xml", "folder": str, "output_path": str,
   "selected_paths": [str]|null, ...same scoring options...}

Output protocol (stdout, one JSON object per line — see backend/jobs.py).

Analysis mirrors the standalone app's own flow (app.py _run_analysis):
cache pass first (result_cache.is_entry_usable -> result_from_entry +
rescore_clip, no decode), then a ProcessPoolExecutor over the misses using
a module-level picklable worker function (the analogue of app.py's
_analyze_clip_worker), then save_cache with entry_from_result for every
fresh success. Progress = files completed / total.

MULTIPROCESSING NOTE: macOS uses the "spawn" start method, so each pool
child re-imports THIS file as `__mp_main__` — everything at module level
must therefore be side-effect-safe (imports + constants only; argv parsing
and all real work live under the __main__ guard), and the B-Roll
Analyzer's dir must be put on sys.path at module level so the re-import
can find `analyzer`. The stdout redirection below is also top-level on
purpose: pool children inherit fd tables, and redirecting their fd 1 to
stderr (a second time — harmless) guarantees a chatty native codec in a
child can never write into the parent's protocol stream.
"""

import os
import sys
import json
import base64
import traceback
import concurrent.futures

# ---- protocol stream setup (BEFORE cv2/numpy import — some builds print
# banners to stdout) ---------------------------------------------------------
_PROTO = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
sys.stdout = sys.stderr

# ---- locate the analyzer (module level: pool children need this too) -------
# This file lives at <parent>/apps/suite-wrapper/backend/workers/; the
# analyzer is a sibling of suite-wrapper under apps/. Deterministic (no
# argv) because spawn children re-import this module with different argv.
BROLL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "broll-analyzer",
)
if BROLL_DIR not in sys.path:
    sys.path.insert(0, BROLL_DIR)

# This file's own dir is backend/workers/ -- its parent, backend/, is where
# braw_bridge.py/braw_proxy_cache.py/paths.py live. Those three are stdlib +
# each other only (no pywebview/keyring/cv2), so they import cleanly here
# too, in B-Roll Analyzer's own venv -- lets this worker resolve a .braw
# path to its cached proxy WITHOUT B-Roll's own analyzer.py ever knowing
# BRAW exists (see braw_bridge.py's module docstring, Phase 2).
SUITE_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SUITE_BACKEND_DIR not in sys.path:
    sys.path.insert(0, SUITE_BACKEND_DIR)

import analyzer       # noqa: E402
import result_cache   # noqa: E402
import xml_export     # noqa: E402
import braw_bridge     # noqa: E402

# Message shapes come from the ONE shared protocol definition (A-2) —
# bare import when run as a script (sys.path[0] is this directory),
# package import when the suite imports this module in-process.
try:
    import worker_protocol
except ImportError:
    from backend.workers import worker_protocol


def emit(obj):
    _PROTO.write(json.dumps(obj) + "\n")
    _PROTO.flush()


def emit_progress(progress, detail):
    emit(worker_protocol.make_progress(progress, detail))


DEFAULT_OPTIONS = {
    "window_sec": 4.0,
    "max_segments": 1,
    "min_segment_gap_sec": 1.0,
    "enable_energy": False,
    "energy_weight": 0.35,
    "max_workers": 3,
}

# Below this, a clip's own full duration is shorter than any segment could
# usefully be -- e.g. an accidental "record then immediately stop" take.
# analyzer.py reports these accurately (real, correctly-decoded footage,
# not a decode failure), so this is a suite-side display/export filter
# rather than a fix to the analyzer itself: too-short clips are shown as
# an error-style card (no segment chips, can't be selected) instead of a
# normal result whose "best segment" is really just the whole unusable
# clip.
MIN_USABLE_DURATION_SEC = 1.0


def read_options(params):
    """Merge request params over the defaults, coercing types defensively —
    these values ultimately come from form fields in the frontend."""
    opts = dict(DEFAULT_OPTIONS)
    for key in ("window_sec", "min_segment_gap_sec", "energy_weight"):
        try:
            opts[key] = float(params.get(key, opts[key]))
        except (TypeError, ValueError):
            pass
    for key in ("max_segments", "max_workers"):
        try:
            opts[key] = max(1, int(params.get(key, opts[key])))
        except (TypeError, ValueError):
            pass
    opts["enable_energy"] = bool(params.get("enable_energy", opts["enable_energy"]))
    return opts


def _find_all_video_files(folder):
    """analyzer.find_video_files' own list (sibling code, untouched --
    doesn't know .braw exists) plus a separate .braw scan, combined and
    sorted the same way find_video_files already sorts its own results.
    The ONE file-discovery entry point both run_analyze and
    rebuild_from_cache use, so a .braw clip's cached analysis is found by
    XML export exactly the same way a fresh analysis found it."""
    return sorted(analyzer.find_video_files(folder) + braw_bridge.find_braw_files(folder))


def _analyze_one(path, window_sec, max_segments, min_segment_gap_sec,
                 enable_energy, energy_weight):
    """Pool worker — module-level and picklable, mirroring app.py's
    _analyze_clip_worker: one bad file records its error in the ClipResult
    instead of taking the whole batch down.

    `path` is always the ORIGINAL source path (never a proxy). For a
    .braw file, this waits (bounded, braw_bridge.wait_for_decode_path)
    for its cached proxy INSIDE this pool child — deliberately not in
    run_analyze's single discovery loop, so one slow-to-generate proxy
    (api_broll.py's broll_start queues it fire-and-forget, with no
    ordering guarantee against this job starting) only occupies ONE pool
    slot, never blocks any other file's analysis. The returned
    ClipResult's .path is left as whatever analyze_clip set it to (== the
    resolved decode path for a .braw source) — run_analyze swaps it back
    to the ORIGINAL path once every step still needing real decoded
    bytes (thumbnail refresh included) is done."""
    decode_path, err = braw_bridge.wait_for_decode_path(path)
    if err is not None:
        return analyzer.ClipResult(
            path=path, filename=os.path.basename(path),
            duration=0, fps=0, width=0, height=0, error=err)
    try:
        return analyzer.analyze_clip(
            decode_path, progress_cb=None, window_sec=window_sec,
            max_segments=max_segments, min_segment_gap_sec=min_segment_gap_sec,
            enable_energy=enable_energy, energy_weight=energy_weight)
    except Exception as e:
        return analyzer.ClipResult(
            path=path, filename=os.path.basename(path),
            duration=0, fps=0, width=0, height=0,
            error=f"{e}\n{traceback.format_exc(limit=1)}")


def _pool_init():
    """Per-child init: cap OpenCV's internal threading so N parallel worker
    processes don't each ALSO fan out across every core (same rationale as
    the standalone app's _worker_init)."""
    analyzer.limit_opencv_threads()
    # Same oversubscription story for torch: with energy scoring on, each
    # pool child loads its own CLIP model, and torch's intra-op pool
    # defaults to every core — N children x cpu_count threads. Guarded so
    # the technical-scoring-only path (torch not installed — it's an
    # optional dependency of the analyzer) keeps working exactly as before.
    try:
        import torch
        torch.set_num_threads(1)
    except ImportError:
        pass


def _thumbnail_data_uri(result):
    if not result.thumbnail_jpeg:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(result.thumbnail_jpeg).decode("ascii")


def clip_payload(result):
    """The JSON shape the suite frontend renders in its results grid."""
    error = result.error
    if error is None and result.duration < MIN_USABLE_DURATION_SEC:
        error = (f"Clip is only {result.duration:.1f}s long — too short to "
                 "use as b-roll.")
    return {
        "path": result.path,
        "filename": result.filename,
        "duration": result.duration,
        "fps": result.fps,
        "width": result.width,
        "height": result.height,
        "overall_score": result.overall_score,
        "best_window_start": result.best_window_start,
        "best_window_end": result.best_window_end,
        "segments": [{"start": s.start, "end": s.end, "score": s.score}
                     for s in (result.segments or [])],
        "thumbnail_data_uri": _thumbnail_data_uri(result),
        "error": error,
    }


def rebuild_from_cache(folder, options, paths_filter=None):
    """Reconstruct rescored ClipResults for every cached, unchanged file in
    `folder` (optionally restricted to paths_filter). Used by export_xml
    mode — no video decode happens here at all."""
    entries = result_cache.load_cache(folder)
    wanted = None
    if paths_filter:
        wanted = {os.path.abspath(p) for p in paths_filter}
    results = []
    for path in _find_all_video_files(folder):
        if wanted is not None and os.path.abspath(path) not in wanted:
            continue
        rel = os.path.relpath(path, folder)
        entry = entries.get(rel)
        fp = result_cache.file_fingerprint(path)
        if not result_cache.is_entry_usable(entry, fp, need_energy=False):
            continue
        try:
            result = result_cache.result_from_entry(path, entry)
            analyzer.rescore_clip(
                result,
                window_sec=options["window_sec"],
                max_segments=options["max_segments"],
                min_segment_gap_sec=options["min_segment_gap_sec"],
                energy_weight=options["energy_weight"],
                enable_energy=options["enable_energy"])
            # A too-short clip never has selectable chips in the UI (see
            # clip_payload), so it can only reach here via an "export
            # everything" run (wanted is None) rather than an explicit
            # pick — keep it out of the XML the same way it's kept out of
            # the results grid.
            if wanted is None and result.error is None \
                    and result.duration < MIN_USABLE_DURATION_SEC:
                continue
            results.append(result)
        except Exception:
            continue  # unusable entry — skip rather than fail the export
    return results


def run_analyze(params):
    folder = params.get("folder")
    if not folder or not os.path.isdir(folder):
        raise RuntimeError(f"Folder not found: {folder}")
    options = read_options(params)

    emit_progress(0, "Scanning folder for video files…")
    files = _find_all_video_files(folder)
    if not files:
        raise RuntimeError("No video files found in that folder.")
    total = len(files)

    # --- cache pass: reuse anything unchanged (cheap rescore, no decode).
    cache_entries = result_cache.load_cache(folder)
    results = [None] * total
    fingerprints = [None] * total
    to_submit = []
    completed = 0
    for i, path in enumerate(files):
        fp = result_cache.file_fingerprint(path)
        fingerprints[i] = fp
        entry = cache_entries.get(os.path.relpath(path, folder))
        if result_cache.is_entry_usable(entry, fp, need_energy=options["enable_energy"]):
            try:
                result = result_cache.result_from_entry(path, entry)
                analyzer.rescore_clip(
                    result,
                    window_sec=options["window_sec"],
                    max_segments=options["max_segments"],
                    min_segment_gap_sec=options["min_segment_gap_sec"],
                    energy_weight=options["energy_weight"],
                    enable_energy=options["enable_energy"])
                results[i] = result
                completed += 1
                continue
            except Exception:
                pass  # any reuse problem -> re-analyze
        to_submit.append(i)

    if completed:
        emit_progress(completed / total * 100,
                      f"Loaded {completed}/{total} clip(s) from cache…")

    # --- parallel pass over the cache misses. `_analyze_one` is handed
    # the ORIGINAL path for every file (braw or not) — it resolves a
    # .braw file to its cached proxy itself, waiting inside its own pool
    # slot if one is still generating (see that function's docstring for
    # why that wait must never happen HERE, in this single dispatch
    # loop: it would serialize every other file behind it).
    if to_submit:
        num_workers = max(1, min(options["max_workers"], len(to_submit)))
        emit_progress(completed / total * 100,
                      f"Analyzing {len(to_submit)} clip(s) with {num_workers} worker(s)…")
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=num_workers, initializer=_pool_init) as executor:
            future_to_index = {
                executor.submit(
                    _analyze_one, files[i], options["window_sec"],
                    options["max_segments"], options["min_segment_gap_sec"],
                    options["enable_energy"], options["energy_weight"]): i
                for i in to_submit
            }
            for future in concurrent.futures.as_completed(future_to_index):
                i = future_to_index[future]
                path = files[i]
                try:
                    result = future.result()
                except Exception as e:
                    # A worker process dying outright (native codec crash)
                    # surfaces here — record it per-file, keep going.
                    result = analyzer.ClipResult(
                        path=path, filename=os.path.basename(path),
                        duration=0, fps=0, width=0, height=0,
                        error=f"{e}\n{traceback.format_exc(limit=1)}")
                results[i] = result
                completed += 1
                emit_progress(completed / total * 100,
                              f"Analyzed {completed}/{total}: {os.path.basename(path)}")

    # --- thumbnails: fresh analyses already captured one (analyze_clip ->
    # refresh_thumbnail, using result.path == decode_path at this point --
    # correct for a .braw clip too, still not yet swapped back below);
    # cache hits keep their cached frame even if the best window moved (a
    # slightly stale preview beats reseeking every file — matches the
    # standalone app's on-demand policy). Only capture when a clip has
    # none at all.
    for result in results:
        if result is not None and result.error is None and result.thumbnail_jpeg is None:
            try:
                analyzer.refresh_thumbnail(result)
            except Exception:
                pass

    # --- swap .braw results' path/filename back from the (ephemeral,
    # cache-dir-local) proxy path to the real source path, now that every
    # step needing actual decoded bytes (analysis, thumbnail capture) is
    # done. Everything from here on — cache keys, the frontend payload,
    # XML export — must reference the ORIGINAL .braw file, matching this
    # suite's "export always references original media" convention
    # (CONTRACT.md), never a path inside assets/proxies/ that may be
    # pruned or regenerated later. A successfully-decoded .braw result's
    # .path is still whatever analyze_clip set it to inside _analyze_one
    # (the resolved proxy path) — a failed one (proxy never showed up,
    # BRAW unavailable) already used the ORIGINAL path for its error
    # placeholder, so `result.path != files[i]` is exactly the "actually
    # went through a proxy" check.
    for i in to_submit:
        result = results[i]
        if (result is not None and result.error is None
                and os.path.splitext(files[i])[1].lower() == braw_bridge.BRAW_EXTENSION
                and result.path != files[i]):
            original_path = files[i]
            result.path = original_path
            result.filename = os.path.basename(original_path)

    # --- write the cache back (fresh successes overwrite/extend; entries
    # for files no longer present are left alone, same as the app).
    for i in to_submit:
        result, fp = results[i], fingerprints[i]
        if result is not None and result.error is None and fp is not None:
            cache_entries[os.path.relpath(result.path, folder)] = \
                result_cache.entry_from_result(result, fp)
    result_cache.save_cache(folder, cache_entries)

    clips = [clip_payload(r) for r in results if r is not None]
    # Best-first; failed clips (score 0, error set) sink to the bottom.
    clips.sort(key=lambda c: (c["error"] is not None, -(c["overall_score"] or 0.0)))

    emit(worker_protocol.make_result({"folder": folder, "options": options, "clips": clips}))


def run_export_xml(params):
    folder = params.get("folder")
    output_path = params.get("output_path")
    if not folder or not os.path.isdir(folder):
        raise RuntimeError(f"Folder not found: {folder}")
    if not output_path:
        raise RuntimeError("No output path given for the XML export.")
    options = read_options(params)
    selected = params.get("selected_paths") or None

    emit_progress(10, "Rebuilding clip results from cache…")
    results = rebuild_from_cache(folder, options, paths_filter=selected)
    if not results:
        raise RuntimeError(
            "No analyzed clips available to export — run (or re-run) the "
            "analysis first, then export.")
    results.sort(key=lambda r: r.overall_score, reverse=True)

    emit_progress(60, "Writing Premiere XML…")
    xml_export.export_xml(
        results, output_path,
        sequence_name=params.get("sequence_name") or "Best B-Roll Selects",
        show_energy=options["enable_energy"])
    emit(worker_protocol.make_result({"path": output_path, "clip_count": len(results)}))


def selfcheck():
    """Import-only smoke test: analyzer/result_cache/xml_export imported
    fine in this interpreter (which pulls in cv2/numpy). No video decode,
    no CLIP model load. Also confirms braw_bridge (a suite-owned, stdlib-
    only module) imports cleanly from THIS venv via the sys.path insert
    above -- that import silently no-oping would otherwise only surface
    as a mysterious NameError deep inside run_analyze on a .braw folder."""
    for mod, name in ((analyzer, "analyze_clip"), (analyzer, "rescore_clip"),
                      (analyzer, "find_video_files"), (result_cache, "load_cache"),
                      (result_cache, "entry_from_result"), (xml_export, "export_xml"),
                      (braw_bridge, "find_braw_files"), (braw_bridge, "find_cached_proxy"),
                      (braw_bridge, "wait_for_decode_path")):
        if not hasattr(mod, name):
            raise RuntimeError(f"{mod.__name__} is missing expected attribute: {name}")
    _PROTO.write("WORKER OK\n")
    _PROTO.flush()


def main(argv):
    if len(argv) > 1 and argv[1] == "--selfcheck":
        selfcheck()
        return 0
    try:
        params = json.loads(argv[1]) if len(argv) > 1 else {}
    except (ValueError, IndexError):
        emit(worker_protocol.make_error("Worker started without valid JSON params in argv[1]."))
        return 2
    try:
        if params.get("mode", "analyze") == "export_xml":
            run_export_xml(params)
        else:
            run_analyze(params)
        return 0
    except Exception as e:
        traceback.print_exc()
        emit(worker_protocol.make_error(str(e) or repr(e)))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
