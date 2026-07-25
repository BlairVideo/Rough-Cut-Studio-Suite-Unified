"""
transcribe_worker.py — one-video transcription worker for the suite.

RUN WITH THE TRANSCRIBER'S OWN VENV PYTHON (Local Interview Transcriber/
.venv/bin/python) — that's where mlx-whisper, pyannote, torch and keyring
live. The suite venv never imports any of this.

Params arrive as a JSON string in argv[1]:
    {"video_path": str, "model_repo": str, "model_label": str,
     "enable_diarization": bool, "app_dir": str (optional override),
     "audio_path": str (optional), "offset_seconds": float (optional),
     "audio_channel": int (optional, 1-based single source channel)}

SYNCED EXTERNAL AUDIO (contract addendum v3 — proxy-free): when
`audio_path` is present, the WAV fed to whisper/pyannote is extracted from
THAT file (extract_audio's ffmpeg -vn call works on pure audio files too)
instead of the video, and after merge/normalize every segment is shifted
by `offset_seconds` onto the VIDEO timeline (video_time = audio_time +
offset; segments ending at/before 0 are dropped, starts clamped to >= 0 —
see shift_segments_to_video_time). The cache is still written next to the
VIDEO and fingerprints the video file, so the transcript aligns with the
video everywhere downstream; the cache JSON additionally carries a
top-level "audio_source" key (extra keys are ignored by the standalone
app's loader).

Output protocol (stdout, one JSON object per line — see backend/jobs.py):
    {"type":"progress","progress":0-100,"detail":"..."}
    {"type":"result","data":{video_path, segments, speakers, cache_path}}
    {"type":"error","message":"..."}

IMPORT STRATEGY (the decision the contract asks to be documented):
The transcriber's app.py was inspected and importing it directly is safe —
all Streamlit UI code lives inside functions and its main() is guarded by
`if __name__ == "__main__"`; the only module-level side effects are
constants/CSS strings, and the heavy libraries (mlx_whisper, pyannote,
torch) are imported lazily INSIDE the pipeline functions. So no streamlit
shim / reimplementation is needed: we import `app` and call its pure
pipeline functions (extract_audio, transcribe_audio, diarize_audio,
merge_transcript_and_speakers, normalize_speaker_names) directly — one
source of truth, identical behavior to the standalone app, including its
tqdm monkeypatch for mlx-whisper progress. The one app.py function we do
NOT reuse is save_cache(): it reads st.session_state, so the (identical-
schema) .ivt-cache.json is written here instead.

STDOUT HYGIENE: importing streamlit and running whisper/pyannote can make
third-party code print to stdout, which would corrupt the JSON-line
protocol. So before ANY heavy import, the real stdout fd is duplicated for
protocol use and fd 1 is redirected to stderr — stray prints land in the
job's stderr ring buffer instead of the protocol stream.

SINGLE-THREADED BY DESIGN: everything here runs sequentially in this one
process (Metal/MPS constraint — see the transcriber's own docs). The suite
throttles how many of these processes run at once (default 1).
"""

import os
import sys
import json
import shutil
import tempfile
import traceback
import subprocess

# ---- protocol stream setup (BEFORE any heavy import) ----------------------
_PROTO = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
sys.stdout = sys.stderr


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


# ---- locate and import the transcriber app ---------------------------------
# Default: this file lives at <parent>/apps/suite-wrapper/backend/workers/,
# and the transcriber is a sibling of suite-wrapper under apps/. An explicit
# app_dir param wins.
_DEFAULT_IVT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "interview-transcriber",
)


def _import_app(app_dir):
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    import app  # noqa: F401  (the transcriber's app.py — safe, see module docstring)
    return app


# This file's own dir is backend/workers/ -- its parent, backend/, is where
# braw_bridge.py/braw_proxy_cache.py/paths.py live (stdlib + each other
# only), so they import cleanly here too, in the transcriber's own venv --
# same sys.path insert as sync_worker.py/broll_worker.py, letting this
# worker resolve a .braw path to its cached proxy WITHOUT the transcriber's
# own app.py ever knowing BRAW exists (braw_bridge.py's module docstring,
# Phase 2).
SUITE_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SUITE_BACKEND_DIR not in sys.path:
    sys.path.insert(0, SUITE_BACKEND_DIR)
import braw_bridge  # noqa: E402


# ---- cache writing (schema-identical to the standalone app) ----------------


def write_ivt_cache(app, video_path, segments, speakers, audio_source=None,
                    sync_offset_seconds=None):
    """Write <video>.ivt-cache.json exactly as the standalone app does
    (app.save_cache), minus the st.session_state lookups: a fresh
    transcription has no per-speaker relabels or exclusions yet, so those
    fields are their empty defaults. Best-effort like the original.
    `audio_source` (the synced external audio file, if one was used) and
    `sync_offset_seconds` are recorded as extra top-level keys — the
    standalone app's loader reads only its known fields, so the additions
    are invisible to it. Recording the offset here (not just in the
    sync-offsets sidecar) lets the Edit-workspace XML export find the
    synced audio + its offset self-containedly, even if the sidecar was
    moved or deleted.

    Contract addendum v9 (one sidecar per video, not two): if a
    `.sync-offsets.json` sidecar already exists next to this video (it was
    synced BEFORE being transcribed), its full routing data is folded into
    this cache as `sync_tracks`/`sync_method`/`sync_updated_at` — plain
    os/json only, since this runs in the Transcriber's OWN venv/process
    and can't import suite modules — and the sidecar is deleted once this
    write succeeds, so the video ends up with just one sidecar file
    instead of two. `audio_source`/`sync_offset_seconds` above record
    which ONE track was actually fed to whisper for THIS transcription — a
    distinct, still-useful fact even after this fuller merge (several
    recorders may be synced but only one transcribed)."""
    try:
        stat = os.stat(video_path)
        video_size, video_mtime = stat.st_size, int(stat.st_mtime)
    except OSError:
        video_size, video_mtime = None, None

    data = {
        "path": video_path,
        "name": os.path.basename(video_path),
        "video_size": video_size,
        "video_mtime": video_mtime,
        "speakers": speakers,
        "segments": [
            {
                "start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker,
                "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob,
            }
            for s in segments
        ],
        "speaker_labels": {},
        "excluded_speakers": [],
    }
    if audio_source:
        data["audio_source"] = audio_source
        if sync_offset_seconds is not None:
            try:
                data["sync_offset_seconds"] = float(sync_offset_seconds)
            except (TypeError, ValueError):
                pass

    # Addendum v55: both sidecar paths redirect to a centralized fallback
    # for a .braw video_path (routinely read-only/removable camera media)
    # -- braw_bridge.sync_offsets_path/ivt_cache_path no-op to the
    # original next-to-source convention for any other extension.
    sidecar_path = braw_bridge.sync_offsets_path(video_path)
    sidecar_to_remove = None
    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            sidecar = json.load(f)
        if isinstance(sidecar, dict) and isinstance(sidecar.get("tracks"), list):
            data["sync_tracks"] = sidecar["tracks"]
            data["sync_method"] = sidecar.get("method") or "waveform"
            data["sync_updated_at"] = sidecar.get("updated_at")
            sidecar_to_remove = sidecar_path
    except Exception:
        pass  # no sidecar, or unreadable -- nothing to fold in

    cache_path = braw_bridge.ivt_cache_path(video_path)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        return None
    if sidecar_to_remove:
        try:
            os.remove(sidecar_to_remove)
        except OSError:
            pass  # cosmetic cleanup only -- the cache already has the data
    return cache_path


def build_channel_extract_cmd(source_path, out_wav, channel):
    """ffmpeg command extracting ONE source channel (1-based) to a mono
    16 kHz PCM WAV — the SAME output format as the transcriber app's
    extract_audio, but the ``-ac 1`` full downmix is replaced by a pan
    filter isolating a single source channel (``pan=mono|c0=c{channel-1}``,
    0-based inside the filter). PURE and stdlib-only (returns the arg list,
    runs nothing) so the arg construction is unit-testable with no decode.

    Used only when a specific channel is requested (addendum v4 E); the
    whole-file mono path stays app.extract_audio, byte-for-byte as before."""
    ch0 = int(channel) - 1
    return [
        "ffmpeg", "-y", "-i", source_path,
        "-vn",                 # no video
        "-af", f"pan=mono|c0=c{ch0}",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        out_wav,
    ]


def extract_single_channel(source_path, out_wav, channel):
    """Run build_channel_extract_cmd. Raises RuntimeError on ffmpeg failure,
    mirroring the transcriber app's extract_audio error contract."""
    cmd = build_channel_extract_cmd(source_path, out_wav, channel)
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg timed out after 1800s")
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {result.stderr.decode(errors='ignore')[-500:]}")


def shift_segments_to_video_time(segments, offset_seconds):
    """Map audio-domain segment times onto the VIDEO timeline (contract
    addendum v3 sign convention: video_time = audio_time + offset).

    PURE module-level function (stdlib only, no app import) so it is unit-
    testable with fake segments: works on any objects exposing mutable
    .start/.end attributes. For every segment: start += o, end += o; a
    segment whose shifted end <= 0 lies entirely before the video starts
    and is DROPPED; a shifted start below 0 is CLAMPED to 0.0. Returns the
    surviving segments (mutated in place) in their original order."""
    o = float(offset_seconds or 0.0)
    survivors = []
    for seg in segments:
        start = seg.start + o
        end = seg.end + o
        if end <= 0:
            continue
        seg.start = max(0.0, start)
        seg.end = end
        survivors.append(seg)
    return survivors


def _read_hf_token():
    """Read the HF token from the exact keyring slot the standalone app
    uses. Read here (not passed in argv) so the token never appears in a
    process list."""
    try:
        import keyring
        return keyring.get_password("InterviewTranscriber", "hf_token") or ""
    except Exception:
        return ""


# ---- pipeline ---------------------------------------------------------------
# Progress budget across phases (single 0-100 scale for the job card):
#   extract 0-4, transcribe 4-70 (or 4-96 without diarization),
#   diarize 70-96, merge/cache 96-100.

def run(params):
    video_path = params.get("video_path")
    model_repo = params.get("model_repo")
    enable_diarization = bool(params.get("enable_diarization"))
    app_dir = params.get("app_dir") or _DEFAULT_IVT_DIR
    audio_path = params.get("audio_path")  # optional synced external audio
    audio_channel = params.get("audio_channel")  # optional 1-based channel
    if audio_channel is not None:
        try:
            audio_channel = int(audio_channel)
        except (TypeError, ValueError):
            raise RuntimeError(f"Invalid audio_channel: {params.get('audio_channel')!r}")
        if audio_channel < 1:
            raise RuntimeError(f"Invalid audio_channel: {params.get('audio_channel')!r}")
    try:
        offset_seconds = float(params.get("offset_seconds") or 0.0)
    except (TypeError, ValueError):
        raise RuntimeError(f"Invalid offset_seconds: {params.get('offset_seconds')!r}")

    if not video_path or not os.path.isfile(video_path):
        raise RuntimeError(f"Video file not found: {video_path}")
    if audio_path and not os.path.isfile(audio_path):
        raise RuntimeError(f"Audio file not found: {audio_path}")
    if not model_repo:
        raise RuntimeError("No whisper model specified.")

    emit_progress(0, "Loading transcription pipeline…")
    app = _import_app(app_dir)

    hf_token = ""
    if enable_diarization:
        hf_token = _read_hf_token()
        if not hf_token:
            raise RuntimeError(
                "Speaker diarization is enabled but no Hugging Face token is "
                "saved. Add one in the Transcribe panel (or disable diarization)."
            )

    transcribe_end = 70.0 if enable_diarization else 96.0
    tmp_dir = tempfile.mkdtemp(prefix="suite_ivt_")
    try:
        # The whisper/pyannote input: the synced external audio when one
        # was given (no proxy is ever rendered), otherwise the video --
        # resolved through its cached BRAW proxy if it's a .braw source
        # (wait_for_decode_path no-ops for any other extension). Safe to
        # wait here (bounded, but effectively unbounded per braw_bridge's
        # own addendum v48) since this worker already runs one file per
        # process (module docstring: SINGLE-THREADED BY DESIGN) -- never a
        # shared dispatch loop another file's processing queues behind.
        # video_path itself is NEVER reassigned: write_ivt_cache below
        # keys the .ivt-cache.json sidecar on it, so no swap-back is
        # needed the way broll_worker.py's run_analyze requires.
        source_path = audio_path or video_path
        if not audio_path:
            source_path, err = braw_bridge.wait_for_decode_path(video_path)
            if err is not None:
                raise RuntimeError(err)
        emit_progress(1, "Extracting external audio…" if audio_path
                      else "Extracting audio…")
        tmp_wav = os.path.join(tmp_dir, "audio.wav")
        if audio_channel is not None:
            # Isolate the requested SOURCE channel (addendum v4 E) instead
            # of the app's whole-file -ac 1 downmix. Own ffmpeg call because
            # the app's extract_audio takes only (path, out_wav).
            extract_single_channel(source_path, tmp_wav, audio_channel)
        else:
            app.extract_audio(source_path, tmp_wav)

        emit_progress(4, "Transcribing… 0%")
        transcript_segments = app.transcribe_audio(
            tmp_wav, model_repo,
            progress_callback=lambda frac: emit_progress(
                4 + frac * (transcribe_end - 4),
                f"Transcribing… {int(frac * 100)}%",
            ),
        )

        diarization_turns = []
        if enable_diarization:
            emit_progress(transcribe_end, "Diarizing speakers…")
            diarization_turns = app.diarize_audio(
                tmp_wav, hf_token,
                progress_callback=lambda step, frac: emit_progress(
                    transcribe_end + frac * (96 - transcribe_end),
                    f"Diarizing speakers ({step})… {int(frac * 100)}%",
                ),
            )

        emit_progress(96, "Merging segments…")
        segments, speaker_order = app.merge_transcript_and_speakers(
            transcript_segments, diarization_turns)
        name_map = app.normalize_speaker_names(speaker_order)
        for seg in segments:
            seg.speaker = name_map.get(seg.speaker, seg.speaker)
        speakers = list(name_map.values()) or ["Speaker 1"]

        if audio_path:
            # AFTER merge/normalize: map the audio-domain times onto the
            # video timeline per the v3 sign convention.
            emit_progress(97, "Shifting segments onto the video timeline…")
            segments = shift_segments_to_video_time(segments, offset_seconds)

        emit_progress(98, "Writing cache…")
        cache_path = write_ivt_cache(
            app, video_path, segments, speakers,
            audio_source=audio_path,
            sync_offset_seconds=offset_seconds if audio_path else None)

        emit(worker_protocol.make_result({
            "video_path": video_path,
            "segments": [
                {
                    "start": s.start, "end": s.end, "text": s.text,
                    "speaker": s.speaker, "avg_logprob": s.avg_logprob,
                    "no_speech_prob": s.no_speech_prob,
                }
                for s in segments
            ],
            "speakers": speakers,
            "cache_path": cache_path,
            "audio_source": audio_path,
            "offset_seconds": offset_seconds,
        }))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def selfcheck():
    """Import-only smoke test: prove the app module imports cleanly in this
    interpreter and the pipeline functions exist. No model download, no
    transcription, no audio decode."""
    app = _import_app(_DEFAULT_IVT_DIR)
    for name in ("extract_audio", "transcribe_audio", "diarize_audio",
                 "merge_transcript_and_speakers", "normalize_speaker_names",
                 "Segment", "CACHE_SUFFIX", "WHISPER_MODELS"):
        if not hasattr(app, name):
            raise RuntimeError(f"Transcriber app.py is missing expected attribute: {name}")
    if not callable(braw_bridge.wait_for_decode_path):
        raise RuntimeError("braw_bridge.wait_for_decode_path is missing.")
    # A-3: dump the constants the suite MIRRORS by hand-copy
    # (suite_api.WHISPER_MODELS / IVT_CACHE_SUFFIX) so main.py --selftest
    # can assert they still MATCH the transcriber's own values, not merely
    # exist — drift would otherwise surface only as wrong model ids at
    # transcription time. One JSON line, then the human-readable OK.
    _PROTO.write(json.dumps({
        "whisper_models": app.WHISPER_MODELS,
        "cache_suffix": app.CACHE_SUFFIX,
    }) + "\n")
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
        run(params)
        return 0
    except Exception as e:
        traceback.print_exc()
        emit(worker_protocol.make_error(str(e) or repr(e)))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
