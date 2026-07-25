"""
harmonize_worker.py — piano multicam sync-alignment worker for the suite.

RUN WITH THE SHARED WORKSPACE VENV PYTHON (paths.SHARED_VENV_PYTHON) — numpy,
scipy, librosa, and soundfile are workspace dependencies of
apps/harmonizer/backend (see its own pyproject.toml). align.py is imported,
never modified (same sibling-app rule as A-Sync/sync_core.py).

Params arrive as a JSON string in argv[1]. One mode (params.mode):

  {"mode": "align", "ref": str, "takes": [str, ...], "no_retime": [str, ...]?}
      -> progress lines through the pipeline
      -> result {"report": {...}} — the exact same report shape align.py's
         own CLI `main()` writes to disk (reference, takes, ref_duration,
         take_durations, coarse_offsets_sec, coarse_offset_confidence,
         anchors, segments, skipped_anchors, excluded_leadin_ref_sec,
         waveforms, merge_tolerance, flag_speed_min, flag_speed_max) PLUS
         one extra key not in align.py's own dict, "no_retime_takes" (the
         validated, sorted list actually passed to build_segments), so the
         frontend can label those takes in the summary without having to
         separately track what it asked for.

`no_retime` (basenames of takes, matching align.py's own `--no-retime` CLI
flag): takes known to share the reference's own audio source (e.g. fed
from the same recorder) skip matching entirely and get one straight
segment positioned by the coarse offset alone — see align.py's
build_segments docstring comment. Any name not among the takes' own
basenames is a RuntimeError, same validation align.py's main() does.

Uses align.py's own default tuning parameters (window_radius=0.15,
search_radius=0.5, merge_tolerance=0.02, flag_speed_min=0.5,
flag_speed_max=2.0, candidates_per_anchor=3, min_peak_separation=0.05,
speed_penalty_weight=3.0, confidence_weight=1.0, max_lead_in=60.0,
no_retime=[]) — no tunable-params UI in v1.

A .braw take is fine here — align.py's own load_mono() already resolves it
via the bundled Blackmagic RAW SDK (braw_sdk/), independent of the suite's
own braw_bridge/proxy system. The v1 BRAW restriction is export-only (see
harmonizer_bridge.py), not analysis.

Output protocol (stdout, one JSON object per line — see backend/jobs.py):
    {"type":"progress","progress":0-100,"detail":"..."}
    {"type":"result","data":{...}}
    {"type":"error","message":"..."}

STDOUT HYGIENE: same fd-redirect preamble as the other workers — the real
stdout fd is duplicated for protocol use and fd 1 is redirected to stderr
BEFORE align.py (and with it numpy/scipy/librosa) is imported, so any stray
banner lands in the job's stderr ring buffer, never in the protocol stream.
"""

import os
import sys
import json
import traceback

# ---- protocol stream setup (BEFORE the numpy/scipy/librosa import) --------
_PROTO = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
sys.stdout = sys.stderr

# ---- locate harmonizer/backend (same __file__-relative pattern as --------
# sync_worker's ASYNC_DIR). This file lives at <parent>/apps/suite-wrapper/
# backend/workers/; harmonizer is a sibling of suite-wrapper under apps/.
HARMONIZER_BACKEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "harmonizer", "backend",
)
if HARMONIZER_BACKEND_DIR not in sys.path:
    sys.path.insert(0, HARMONIZER_BACKEND_DIR)

import align  # noqa: E402  (Harmonizer's own analysis core — numpy+scipy+librosa)

# Message shapes come from the ONE shared protocol definition (A-2) — bare
# import when run as a script (sys.path[0] is this directory), package
# import when the suite imports this module in-process.
try:
    import worker_protocol
except ImportError:
    from backend.workers import worker_protocol

WINDOW_RADIUS = 0.15
SEARCH_RADIUS = 0.5
MERGE_TOLERANCE = 0.02
FLAG_SPEED_MIN = 0.5
FLAG_SPEED_MAX = 2.0
CANDIDATES_PER_ANCHOR = 3
MIN_PEAK_SEPARATION = 0.05
SPEED_PENALTY_WEIGHT = 3.0
CONFIDENCE_WEIGHT = 1.0
MAX_LEAD_IN = 60.0


def emit(obj):
    _PROTO.write(json.dumps(obj) + "\n")
    _PROTO.flush()


def emit_progress(progress, detail):
    emit(worker_protocol.make_progress(progress, detail))


def run_align(params):
    ref_path = params.get("ref")
    take_paths = [p for p in (params.get("takes") or []) if p]

    if not ref_path or not os.path.isfile(ref_path):
        raise RuntimeError(f"Reference file not found: {ref_path}")
    if not take_paths:
        raise RuntimeError("No takes were given.")
    missing = [p for p in take_paths if not os.path.isfile(p)]
    if missing:
        raise RuntimeError(f"Take file(s) not found: {', '.join(missing)}")

    take_names = [os.path.basename(p) for p in take_paths]
    no_retime_names = {n for n in (params.get("no_retime") or []) if isinstance(n, str) and n}
    unknown = no_retime_names - set(take_names)
    if unknown:
        raise RuntimeError(f"no_retime name(s) not among takes: {sorted(unknown)}")

    emit_progress(0, "Loading reference…")
    ref_audio = align.load_mono(ref_path)
    takes_audio = []
    total = len(take_paths)
    for i, (name, path) in enumerate(zip(take_names, take_paths)):
        emit_progress(5 + (i / total) * 25, f"Loading {name}…")
        takes_audio.append(align.load_mono(path))

    emit_progress(35, "Computing coarse offsets…")
    coarse_offsets = {}
    for name, audio in zip(take_names, takes_audio):
        tau, conf = align.coarse_offset(audio, ref_audio, MAX_LEAD_IN)
        coarse_offsets[name] = (tau, conf)

    emit_progress(50, "Detecting anchors…")
    anchors = align.build_anchors(
        ref_audio, takes_audio, take_names, coarse_offsets,
        WINDOW_RADIUS, SEARCH_RADIUS, CANDIDATES_PER_ANCHOR, MIN_PEAK_SEPARATION,
    )

    ref_duration = len(ref_audio) / align.SR
    take_durations = {n: len(a) / align.SR for n, a in zip(take_names, takes_audio)}

    emit_progress(80, "Solving alignment…")
    segments, skipped_anchor_counts, leadin_ref_sec, anchors_report = align.build_segments(
        anchors, ref_duration, take_durations, take_names, coarse_offsets, MERGE_TOLERANCE,
        FLAG_SPEED_MIN, FLAG_SPEED_MAX, SPEED_PENALTY_WEIGHT, CONFIDENCE_WEIGHT,
        frozenset(no_retime_names),
    )

    emit_progress(95, "Building waveform previews…")
    waveforms = {"reference": align.waveform_peaks(ref_audio)}
    for name, audio in zip(take_names, takes_audio):
        waveforms[name] = align.waveform_peaks(audio)

    report = {
        "reference": os.path.basename(ref_path),
        "takes": take_names,
        "ref_duration": ref_duration,
        "take_durations": take_durations,
        "coarse_offsets_sec": {n: v[0] for n, v in coarse_offsets.items()},
        "coarse_offset_confidence": {n: v[1] for n, v in coarse_offsets.items()},
        "anchors": anchors_report,
        "segments": segments,
        "skipped_anchors": skipped_anchor_counts,
        "excluded_leadin_ref_sec": leadin_ref_sec,
        "waveforms": waveforms,
        "merge_tolerance": MERGE_TOLERANCE,
        "flag_speed_min": FLAG_SPEED_MIN,
        "flag_speed_max": FLAG_SPEED_MAX,
        "no_retime_takes": sorted(no_retime_names),
    }
    emit(worker_protocol.make_result({"report": report}))


def selfcheck():
    """Import-only smoke test: align.py imported cleanly in this interpreter
    (which pulls in numpy + scipy + librosa) and the functions this worker
    relies on exist. No audio decode, no ffmpeg run."""
    for name in ("load_mono", "coarse_offset", "build_anchors", "build_segments",
                 "waveform_peaks", "gcc_phat"):
        if not hasattr(align, name):
            raise RuntimeError(f"align.py is missing expected attribute: {name}")
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
        mode = params.get("mode")
        if mode == "align":
            run_align(params)
        else:
            raise RuntimeError(f"Unknown harmonize worker mode: {mode}")
        return 0
    except Exception as e:
        traceback.print_exc()
        emit(worker_protocol.make_error(str(e) or repr(e)))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
