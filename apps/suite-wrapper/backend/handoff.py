"""
handoff.py — VTT builders + "send to edit" helpers.

Rough Cut Studio ingests transcripts, not media, so every handoff into the
Edit workspace goes through a generated WebVTT file written into the
suite's assets/transcripts/ folder:

  * a full transcript VTT for finished transcription jobs, and
  * a synthetic single-cue "b-roll" VTT for b-roll clips and exported
    Brander graphics.

Both embed a `NOTE Source video: <abs path>` header, which RCS's parser
(detect_linked_media in its transcript_parser.py) uses to auto-link the
media file — so a sent source arrives already linked, no manual relink.

RCS derives source_id from the VTT filename stem, so filenames must be
unique per source video: on a stem collision with a DIFFERENT video, a
" -2"/" -3" suffix is appended rather than silently overwriting another
source's transcript.
"""

import os
import re

try:  # package import (suite runtime) vs. direct script import (tests)
    from . import paths, synced_audio_splice
except ImportError:  # pragma: no cover
    import paths
    import synced_audio_splice

_SOURCE_VIDEO_RE = re.compile(r"source\s*video\s*:\s*(.+)", re.IGNORECASE)


def format_vtt_time(seconds):
    """HH:MM:SS.mmm with hours ALWAYS present — RCS's VTT parser expects
    the full form, and it keeps every generated file self-consistent."""
    try:
        seconds = max(0.0, float(seconds))
    except (TypeError, ValueError):
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def build_transcript_vtt(video_path, segments, audio_refs=None):
    """Build a WEBVTT document from transcription segments (dicts with
    start/end/text and optional speaker). The speaker prefix is only
    emitted when a speaker exists — RCS parses 'Name: text' into its own
    speaker column.

    `audio_refs` (contract addendum v5, optional list of
    {"path", "offset_seconds"}) embeds one durable
    `NOTE Source audio: <path> (offset <±N.NNN>s)` line per ref, right
    after the `NOTE Source video:` line — this is what lets
    `synced_audio_splice.discover_synced_audios` recover the audio/video
    link from the transcript alone, even if the sync sidecar/cache are
    gone. Malformed refs are skipped (best-effort, never raises)."""
    lines = ["WEBVTT", "", f"NOTE Source video: {video_path}"]
    for ref in audio_refs or []:
        try:
            path = (ref or {}).get("path")
            offset = float((ref or {}).get("offset_seconds") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if not path:
            continue
        lines.append(f"NOTE Source audio: {path} (offset {offset:+.3f}s)")
    lines.append("")
    for seg in segments or []:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        speaker = str(seg.get("speaker", "") or "").strip()
        cue_text = f"{speaker}: {text}" if speaker else text
        lines.append(f"{format_vtt_time(seg.get('start'))} --> {format_vtt_time(seg.get('end'))}")
        lines.append(cue_text)
        lines.append("")
    return "\n".join(lines)


def build_broll_vtt(media_path, duration_seconds, cue_text=None):
    """Single-cue VTT covering the whole clip, used to register a b-roll
    clip (or exported graphic) as an RCS source. The one cue spans
    0 -> duration so RCS has a real segment to hang timecodes on."""
    duration_seconds = max(0.1, float(duration_seconds or 0.1))
    if not cue_text:
        cue_text = f"B-roll: {os.path.basename(media_path)}"
    return "\n".join([
        "WEBVTT",
        "",
        f"NOTE Source video: {media_path}",
        "",
        f"{format_vtt_time(0)} --> {format_vtt_time(duration_seconds)}",
        cue_text,
        "",
    ])


def _embedded_source_path(vtt_path):
    """The media path a previously generated VTT points at, or None."""
    try:
        with open(vtt_path, "r", encoding="utf-8", errors="replace") as f:
            m = _SOURCE_VIDEO_RE.search(f.read())
        return m.group(1).strip().strip('"') if m else None
    except OSError:
        return None


def _stem_taken_by_other(api, stem, vtt_path):
    """True if RCS already holds a source with this id that ISN'T our own
    generated VTT — overwriting it would silently replace a source the
    user added themselves (RCS source_id == filename stem)."""
    try:
        existing = api.sources.get(stem)
    except Exception:
        return False
    if not existing:
        return False
    return os.path.abspath(existing.get("path", "")) != os.path.abspath(vtt_path)


def unique_vtt_path(api, base_stem, media_path):
    """Pick a VTT path in assets/transcripts/ whose stem is unique per
    source video: reuse an existing file if it already belongs to this
    exact media file, otherwise append ' -2', ' -3', ... until free."""
    paths.ensure_suite_dirs()
    # Filesystem-safe stem (keep it readable; RCS shows it as the source name).
    stem = re.sub(r'[\\/:*?"<>|]', "_", base_stem).strip() or "source"
    candidate_stem = stem
    n = 1
    while True:
        candidate = os.path.join(paths.TRANSCRIPTS_DIR, candidate_stem + ".vtt")
        if os.path.exists(candidate):
            if _embedded_source_path(candidate) == media_path and \
               not _stem_taken_by_other(api, candidate_stem, candidate):
                return candidate  # ours, same video — safe to reuse/overwrite
        elif not _stem_taken_by_other(api, candidate_stem, candidate):
            return candidate
        n += 1
        candidate_stem = f"{stem} -{n}"


def _ingest_vtt(api, vtt_path):
    """Feed a generated VTT into the inherited RCS state and normalize the
    outcome. RCS's _add_transcript returns a dict with source_id (+ error
    key on parse failure) — never raises for parse problems."""
    info = api._add_transcript(vtt_path)
    if not isinstance(info, dict):
        raise RuntimeError("Unexpected response from transcript ingestion.")
    if info.get("error"):
        raise RuntimeError(f"RCS couldn't parse the generated VTT: {info['error']}")
    return info


def send_transcript_to_edit(api, video_path, segments):
    """Write a full transcript VTT for a finished transcribe job and ingest
    it. Returns {"ok", "source_id", "vtt_path"}.

    Contract addendum v5: before writing, check whether `video_path` has
    ANY known synced external audio (sidecar/cache only — the transcript
    doesn't exist yet at this point, so the note fallback doesn't apply
    here) and embed it as a durable note in the VTT. This makes every
    ordinary "Send to Edit" — not just the Sync workspace's own export —
    carry the audio/video link, so the two workspaces no longer need to be
    used in the same session for the linkage to survive."""
    stem = os.path.splitext(os.path.basename(video_path))[0]
    vtt_path = unique_vtt_path(api, stem, video_path)
    audio_refs = [{"path": a["audio_path"], "offset_seconds": a["offset_seconds"]}
                  for a in synced_audio_splice.discover_synced_audios(video_path)]
    content = build_transcript_vtt(video_path, segments, audio_refs)
    with open(vtt_path, "w", encoding="utf-8") as f:
        f.write(content)
    info = _ingest_vtt(api, vtt_path)
    return {"ok": True, "source_id": info["source_id"], "vtt_path": vtt_path}


def reingest_source(api, vtt_path):
    """Re-parse an existing VTT already sitting in assets/transcripts/ back
    into `api.sources` (contract addendum v6: a favorite made in an
    earlier session may point at a source that hasn't been (re)loaded into
    Edit yet this session). Thin public wrapper around `_ingest_vtt` so
    callers outside this module don't reach into a name-mangled helper."""
    return _ingest_vtt(api, vtt_path)


def ensure_broll_source(api, media_path, duration_seconds, cue_text=None):
    """Make sure `media_path` is registered as an RCS b-roll source,
    writing its synthetic VTT only if not already present. Returns
    {"source_id", "vtt_path"}. Named '<stem> — broll' so a clip's real
    transcript (if one is ever made) can't collide with its b-roll stub."""
    stem = os.path.splitext(os.path.basename(media_path))[0] + " — broll"
    vtt_path = unique_vtt_path(api, stem, media_path)
    if not os.path.exists(vtt_path):
        with open(vtt_path, "w", encoding="utf-8") as f:
            f.write(build_broll_vtt(media_path, duration_seconds, cue_text))
    info = _ingest_vtt(api, vtt_path)
    return {"source_id": info["source_id"], "vtt_path": vtt_path}


def build_cut_spec(api, source_id, start_seconds, end_seconds, track="broll"):
    """CutSpec dict the frontend inserts into the RCS Cuts table. Timecodes
    come from the inherited format_timecode so they match the project's
    current fps/drop-frame settings exactly."""

    def tc(seconds):
        try:
            res = api.format_timecode(seconds)
            if isinstance(res, dict) and res.get("ok"):
                return res["tc"]
        except Exception:
            pass
        return "00:00:00:00"

    start_seconds = max(0.0, float(start_seconds))
    end_seconds = max(start_seconds, float(end_seconds))
    return {
        "source_id": source_id,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "in_tc": tc(start_seconds),
        "out_tc": tc(end_seconds),
        "track": track,
    }
