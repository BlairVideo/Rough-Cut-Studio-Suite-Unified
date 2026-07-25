"""
sync_worker.py — media probe / sync-offset detection worker for the suite.

RUN WITH A-SYNC'S OWN VENV PYTHON (A-Sync/.venv/bin/python) — numpy and
scipy live there. sync_core.py is fully headless (module-level imports:
numpy + scipy only); ffmpeg/ffprobe must be on PATH at runtime. A-Sync's
code is imported, never modified.

Params arrive as a JSON string in argv[1]. Three modes (params.mode):

  {"mode": "probe", "paths": [str]}
      -> result {"probes": {path: Probe}} — per-path failures are recorded
         as {"error": str} entries, never fatal for the batch.

  {"mode": "detect", "video_path": str, "audio_paths": [str],
   "method": "waveform"|"timecode"}
      -> progress per audio file
      -> result {"video": {"path", "probe"}, "tracks": [{path, filename,
         offset_seconds, probe, error}], "method"} — one bad file records
         its error on its own track entry and never fails the batch.

  {"mode": "peaks", "paths": [str], "num_buckets": int (optional)}
      -> result {"peaks": {path: {"peaks": [[min,max],...], "duration":
         float} | {"error": str}}} — waveform data for the suite's
         browser-based Sync workspace visual (see run_peaks). Same
         per-path-failure policy as "probe". Deliberately independent
         of "detect"'s own decode — callable on its own, e.g. after
         restoring already-saved offsets with no fresh detection run.

Probe = {duration, fps, width, height, has_video, has_audio,
audio_channels, audio_samplerate, audio_sample_fmt, audio_bits,
audio_format_label, timecode_tag} — built from sync_core.probe()'s raw
ffprobe JSON (width/height come from the first video stream there;
ProbeInfo alone lacks them) plus ProbeInfo's derived fields.

OFFSET SIGN CONVENTION (binding — contract addendum v3, from
sync_core.waveform_offset's docstring): the offset is the number of
seconds the EXTERNAL AUDIO must be DELAYED to line up with the video;
negative = the audio starts before the video (trim its head). So:
video_time = audio_time + offset. Argument order is always (video, audio)
— the video is the reference.

For waveform detection the video's reference PCM is decoded ONCE
(extract_mono_pcm(video, 8000, 600.0)) and sync_core.waveform_offset is
called directly per audio file, so the video is never re-decoded N times.
For timecode detection sync_core.compute_timecode_offset runs per pair
(it raises when either side lacks embedded timecode — caught per file).

Output protocol (stdout, one JSON object per line — see backend/jobs.py):
    {"type":"progress","progress":0-100,"detail":"..."}
    {"type":"result","data":{...}}
    {"type":"error","message":"..."}

STDOUT HYGIENE: same fd-redirect preamble as the other workers — the real
stdout fd is duplicated for protocol use and fd 1 is redirected to stderr
BEFORE sync_core (and with it numpy/scipy) is imported, so any stray
banner lands in the job's stderr ring buffer, never in the protocol
stream.

BRAW (BRAW compatibility plan, Phase 2 — suite-side-only substitution,
same pattern as broll_worker.py): every path this worker hands to
sync_core first goes through braw_bridge.wait_for_decode_path(), which
resolves a .braw path to its cached proxy — waiting (bounded) if
api_sync.py's fire-and-forget queue_missing_proxies() call started that
proxy generating just before this job did, since there's no ordering
guarantee between the two (this worker is entirely sequential per file
already, so waiting here only makes THIS job take longer, never stalls
some other job the way waiting in a shared discovery loop would). Or a
clear per-path error if BRAW isn't available at all, or the proxy really
never showed up. sync_core.py itself never learns .braw exists. Every
OUTPUT dict below is still keyed/labeled by the ORIGINAL path, never the
proxy — only the bytes actually decoded come from the resolved path.
Addendum v56: the generated proxy now carries a real embedded starting
timecode (braw_proxy_tool.mm writes one QuickTime tmcd sample spanning
the whole clip, sourced from the BRAW SDK's own GetTimecodeForFrame/
QueryTimecodeInfo) — "timecode" method sync against a .braw video works
exactly like any other file now; no change was needed here since
sync_core.py's own probe() already looks for a codec_tag_string == "tmcd"
stream generically.
"""

import os
import sys
import json
import traceback

# ---- protocol stream setup (BEFORE the numpy/scipy import) -----------------
_PROTO = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
sys.stdout = sys.stderr

# ---- locate a-sync (same __file__-relative pattern as broll_worker) --------
# This file lives at <parent>/apps/suite-wrapper/backend/workers/; a-sync is
# a sibling of suite-wrapper under apps/.
ASYNC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "a-sync",
)
if ASYNC_DIR not in sys.path:
    sys.path.insert(0, ASYNC_DIR)

# Same suite-backend sys.path insert as broll_worker.py, for the same
# reason: braw_bridge.py/braw_proxy_cache.py/paths.py are stdlib +
# each-other only, so they import cleanly in A-Sync's own venv too — lets
# this worker resolve a .braw path to its cached proxy WITHOUT
# sync_core.py ever knowing BRAW exists (braw_bridge.py's module
# docstring, Phase 2).
SUITE_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SUITE_BACKEND_DIR not in sys.path:
    sys.path.insert(0, SUITE_BACKEND_DIR)

import sync_core  # noqa: E402  (A-Sync's engine — numpy + scipy only)
from waveform_view import compute_peaks  # noqa: E402  (A-Sync's own waveform helper)
import braw_bridge  # noqa: E402

CORR_SAMPLERATE = 8000      # sync_core's own correlation default
CORR_MAX_SECONDS = 600.0    # cap decode at 10 minutes, like A-Sync's app
NUM_PEAK_BUCKETS = 800      # resolution for the browser-side waveform canvas

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


def build_probe(path):
    """The suite's Probe dict for one file: ProbeInfo's derived fields plus
    width/height read straight from the raw ffprobe JSON's first video
    stream (ProbeInfo doesn't carry dimensions)."""
    raw = sync_core.probe(path)
    info = sync_core.ProbeInfo.from_probe(raw, path)
    width = height = None
    for stream in raw.get("streams", []):
        if stream.get("codec_type") == "video":
            width = stream.get("width")
            height = stream.get("height")
            break
    return {
        "duration": info.duration,
        "fps": info.video_fps,
        "width": width,
        "height": height,
        "has_video": info.has_video,
        "has_audio": info.has_audio,
        "audio_channels": info.audio_channels,
        "audio_samplerate": info.audio_samplerate,
        "audio_sample_fmt": info.audio_sample_fmt,
        "audio_bits": info.audio_bits_per_sample,
        "audio_format_label": info.audio_format_label,
        "timecode_tag": info.timecode_tag,
    }


def run_probe(params):
    paths_list = [p for p in (params.get("paths") or []) if p]
    if not paths_list:
        raise RuntimeError("No files were given to probe.")
    probes = {}
    total = len(paths_list)
    for i, path in enumerate(paths_list):
        emit_progress(i / total * 100, f"Probing {os.path.basename(path)}…")
        # Keyed by the ORIGINAL path always (see module docstring's Probe
        # shape / the suite's own path<->file correlation) — only the
        # bytes handed to sync_core are ever the resolved decode path.
        decode_path, err = braw_bridge.wait_for_decode_path(path)
        if err is not None:
            probes[path] = {"error": err}
            continue
        try:
            probes[path] = build_probe(decode_path)
        except Exception as e:
            probes[path] = {"error": str(e) or repr(e)}
    emit(worker_protocol.make_result({"probes": probes}))


def run_detect(params):
    video_path = params.get("video_path")
    audio_paths = [p for p in (params.get("audio_paths") or []) if p]
    method = params.get("method") or "waveform"

    if not video_path or not os.path.isfile(video_path):
        raise RuntimeError(f"Video file not found: {video_path}")
    if not audio_paths:
        raise RuntimeError("No audio files were given.")
    if method not in ("waveform", "timecode"):
        raise RuntimeError(f"Unknown sync method: {method}")

    # Only ONE video per detect run, so a resolution failure here is fatal
    # for the whole batch (same policy as the "Video file not found" check
    # above) rather than a per-track error. sync_core itself only ever
    # sees video_decode_path from here on; every OUTPUT below still
    # references the original video_path.
    video_decode_path, err = braw_bridge.wait_for_decode_path(video_path)
    if err is not None:
        raise RuntimeError(err)

    emit_progress(0, "Probing video…")
    video_probe = build_probe(video_decode_path)

    ref = None
    video_tc = None
    if method == "waveform":
        # Decode the reference PCM exactly once for the whole batch.
        emit_progress(4, "Decoding video reference audio…")
        ref = sync_core.extract_mono_pcm(video_decode_path, CORR_SAMPLERATE, CORR_MAX_SECONDS)
    else:
        # Same once-per-batch principle for timecode mode (PERF-5):
        # sync_core.compute_timecode_offset(video, audio) re-reads the
        # VIDEO's embedded timecode — a full ffprobe — for every audio
        # file. Read it once here; the per-file loop below then only pays
        # for the audio side's bext chunk. Error-message parity with
        # compute_timecode_offset is kept exactly (same ValueError text).
        #
        # Addendum v56: a .braw video's proxy now carries a real embedded
        # timecode track (tools/braw/braw_proxy_tool.mm), sourced from the
        # BRAW SDK's own timecode accessors — this returns the camera's
        # actual recorded starting timecode now, same as any other file.
        emit_progress(4, "Reading video timecode…")
        video_tc = sync_core.video_timecode_seconds(video_decode_path)

    tracks = []
    total = len(audio_paths)
    for i, audio_path in enumerate(audio_paths):
        name = os.path.basename(audio_path)
        emit_progress(10 + i / total * 88, f"Syncing {name}…")

        probe = None
        offset = None
        error = None
        # Audio sources are never realistically .braw (that's this
        # worker's VIDEO input), but resolving defensively here costs
        # nothing and keeps every path-taking call in this function
        # going through the same one gate.
        audio_decode_path, error = braw_bridge.wait_for_decode_path(audio_path)
        if error is None:
            try:
                probe = build_probe(audio_decode_path)
            except Exception as e:
                error = str(e) or repr(e)
        if error is None:
            try:
                if method == "waveform":
                    target = sync_core.extract_mono_pcm(
                        audio_decode_path, CORR_SAMPLERATE, CORR_MAX_SECONDS)
                    offset = float(sync_core.waveform_offset(
                        ref, target, CORR_SAMPLERATE))
                else:
                    if video_tc is None:
                        raise ValueError(
                            f"No embedded timecode found on video: {video_path}")
                    audio_tc = sync_core.bwf_timecode_seconds(audio_decode_path)
                    if audio_tc is None:
                        raise ValueError(
                            "No BWF timecode (bext chunk) found on audio: "
                            f"{audio_path}")
                    offset = float(audio_tc - video_tc)
            except Exception as e:
                # Per-file failure (no timecode, undecodable audio, …) —
                # recorded on the track, never fatal for the batch.
                error = str(e) or repr(e)

        tracks.append({
            "path": audio_path,
            "filename": name,
            "offset_seconds": offset,
            "probe": probe,
            "error": error,
        })

    emit(worker_protocol.make_result({
        "video": {"path": video_path, "probe": video_probe},
        "tracks": tracks,
        "method": method,
    }))


def run_peaks(params):
    """Downsampled waveform peaks for the suite's browser-based Sync
    workspace — the counterpart to the standalone A-Sync app's own
    WaveformCanvas, which already computes these from decoded PCM via
    the exact same compute_peaks() helper. Deliberately independent of
    run_detect's own decode (rather than piggybacking on it): keeps the
    correlation/timecode result shape untouched, and lets peaks be
    (re)requested on their own — e.g. after restoring already-saved
    offsets, where no fresh detection runs at all."""
    paths_list = [p for p in (params.get("paths") or []) if p]
    if not paths_list:
        raise RuntimeError("No files were given.")
    num_buckets = int(params.get("num_buckets") or NUM_PEAK_BUCKETS)

    result = {}
    total = len(paths_list)
    for i, path in enumerate(paths_list):
        emit_progress(i / total * 100, f"Loading waveform for {os.path.basename(path)}…")
        # Keyed by the ORIGINAL path always — only the bytes handed to
        # sync_core are ever the resolved decode path.
        decode_path, err = braw_bridge.wait_for_decode_path(path)
        if err is not None:
            result[path] = {"error": err}
            continue
        try:
            samples = sync_core.extract_mono_pcm(decode_path, CORR_SAMPLERATE, CORR_MAX_SECONDS)
            peaks = compute_peaks(samples, num_buckets)
            duration = samples.shape[0] / float(CORR_SAMPLERATE)
            result[path] = {"peaks": peaks.tolist(), "duration": duration}
        except Exception as e:
            result[path] = {"error": str(e) or repr(e)}
    emit(worker_protocol.make_result({"peaks": result}))


def selfcheck():
    """Import-only smoke test: sync_core imported cleanly in this
    interpreter (which pulls in numpy + scipy) and the API surface the
    suite relies on exists. No ffmpeg run, no audio decode. Also confirms
    braw_bridge (suite-owned, stdlib-only) imports cleanly from THIS venv
    via the sys.path insert above — see broll_worker.py's selfcheck for
    why this is worth checking explicitly."""
    for name in ("compute_offset", "waveform_offset", "extract_mono_pcm",
                 "probe_info", "probe", "ProbeInfo", "compute_timecode_offset"):
        if not hasattr(sync_core, name):
            raise RuntimeError(f"sync_core.py is missing expected attribute: {name}")
    if not callable(compute_peaks):
        raise RuntimeError("waveform_view.compute_peaks is missing.")
    if not callable(braw_bridge.wait_for_decode_path):
        raise RuntimeError("braw_bridge.wait_for_decode_path is missing.")
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
        if mode == "probe":
            run_probe(params)
        elif mode == "detect":
            run_detect(params)
        elif mode == "peaks":
            run_peaks(params)
        else:
            raise RuntimeError(f"Unknown sync worker mode: {mode}")
        return 0
    except Exception as e:
        traceback.print_exc()
        emit(worker_protocol.make_error(str(e) or repr(e)))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
