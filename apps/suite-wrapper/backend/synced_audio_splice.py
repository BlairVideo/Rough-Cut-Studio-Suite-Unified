"""synced_audio_splice.py — splice A-Sync external audio into the timeline
that Rough Cut Studio's own XMEML exporter produces.

Why this exists
---------------
When a clip is synced in the Sync workspace and sent to Edit, only the
VIDEO becomes the Rough Cut Studio source (its transcript is shifted onto
the video timeline). The separately-recorded external audio is never part
of RCS's data model, so RCS's "Save XML" emits a timeline with no external
audio at all. This module post-processes that XMEML — WITHOUT modifying
Rough Cut Studio — to add the external synced audio as its own, offset-
aligned, non-merged audio track(s).

How it stays correct
--------------------
Rough Cut Studio lays each main cut back-to-back on V1 and writes the
authoritative timeline `<start>`/`<end>` (frames) on every V1 clipitem. We
read those positions straight from the emitted XML (never recompute them),
so the spliced audio can't drift from the picture. We only need Rough Cut
Studio's `resolved_segments` to learn each V1 clip's `source_id` +
source-domain in/out — matched positionally to the V1 clipitems, which are
in the same order.

Offset math (contract addendum v3: `video_time = audio_time + offset`):
the external audio content aligned with a cut's video in-point
`in_seconds` sits at `in_seconds - offset` in the audio file. If that is
negative (the audio began after the picture at that point), the head is
trimmed and the clip's timeline start moved forward by the shortfall.

Scope: main-track cuts only. B-roll cuts sourced from a synced clip are
rare (you cut synced A-cam on the main track) and RCS positions B-roll on
its own lanes with less predictable matching, so they're left alone (a
warning is emitted if one is skipped). XMEML only — FCPXML/OTIO would need
their own splicers.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom

try:
    from .sync_xml import resolve_emit_channels, DOWNMIX
    from . import braw_bridge
except ImportError:  # pragma: no cover — direct script import in tests
    from sync_xml import resolve_emit_channels, DOWNMIX
    import braw_bridge
from rcs_utils.ffprobe_util import probe_json

# Contract addendum v5: the durable "NOTE Source audio: ... (offset ...)"
# line handoff.py embeds in every generated transcript VTT. Collision-proof
# against RCS's own `_SOURCE_VIDEO_RE` (transcript_parser.py) since neither
# line contains the word "video". Path is the non-greedy group before
# " (offset ", offset is the signed float; emitted with exactly 3 decimal
# places so it round-trips cleanly.
_AUDIO_NOTE_RE = re.compile(
    r"source\s*audio\s*:\s*(.+?)\s*\(offset\s*([+-]?[0-9.]+)\s*s\)", re.IGNORECASE)


# --------------------------------------------------------------------------
# Association discovery (which RCS video source has synced external audio)
# --------------------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _probe_channels(path, default=2):
    """External audio channel count via ffprobe (on PATH). Falls back to
    stereo — claiming too few channels would silently drop audio."""
    try:
        data = probe_json(path, timeout=30, select_streams="a:0",
                           show_entries="stream=channels")
        streams = data.get("streams") or []
        n = int((streams[0].get("channels") if streams else 0) or 0)
        return n if n >= 1 else default
    except Exception:
        return default


_DEPTH_BY_SAMPLE_FMT = {
    "u8": 8, "u8p": 8,
    "s16": 16, "s16p": 16,
    "s32": 32, "s32p": 32,
    "flt": 32, "fltp": 32,
    "dbl": 64, "dblp": 64,
}


def _probe_audio_format(path, fps, default_samplerate=48000, default_depth=16):
    """Real (samplerate, depth, duration_frames) via ffprobe.

    Declaring the WRONG samplerate here (or omitting <file><duration>
    entirely) is not just cosmetic: Premiere's XMEML importer uses these
    values to judge whether the source has enough media for the clip's
    in/out range, and a mismatch (e.g. a 44.1kHz field recorder declared
    as 48000) can make it report "insufficient audio for edit" even though
    the real file is plenty long and perfectly in sync. Falls back to the
    advisory defaults (and duration_frames=0, i.e. omitted) only if
    ffprobe itself fails — same fail-open policy as `_probe_channels`."""
    try:
        data = probe_json(
            path, timeout=30, select_streams="a:0",
            show_entries="stream=sample_rate,bits_per_raw_sample,sample_fmt:format=duration")
        stream = (data.get("streams") or [{}])[0]
        samplerate = int(stream.get("sample_rate") or default_samplerate)
        depth = stream.get("bits_per_raw_sample")
        depth = int(depth) if depth else _DEPTH_BY_SAMPLE_FMT.get(
            stream.get("sample_fmt"), default_depth)
        duration = float((data.get("format") or {}).get("duration") or 0.0)
        duration_frames = int(round(duration * float(fps))) if duration > 0 else 0
        return samplerate, depth, duration_frames
    except Exception:
        return default_samplerate, default_depth, 0


def parse_audio_notes_from_transcript(transcript_path):
    """Read the durable `NOTE Source audio: <path> (offset <±N.NNN>s)` lines
    a transcript VTT may embed (contract addendum v5) and return a list of
    {"audio_path", "offset_seconds"}.

    This is the fallback of last resort: it lets the audio/video link
    survive even when BOTH the `.sync-offsets.json` sidecar and the
    `.ivt-cache.json` are gone (transcript moved/shared on its own), because
    handoff.py embeds this note in every VTT it writes whenever the source
    video has known synced audio.

    Best-effort by design, matching this module's other I/O: any read
    failure or missing file returns [] rather than raising. Only entries
    whose `audio_path` still exists on disk are returned (a moved/deleted
    audio file is silently dropped, same policy as the sidecar/cache
    paths), abspath-deduped so a note repeated across multiple lines only
    counts once."""
    if not transcript_path or not isinstance(transcript_path, str):
        return []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return []

    results = []
    seen = set()
    for m in _AUDIO_NOTE_RE.finditer(content):
        audio_path = m.group(1).strip().strip('"')
        if not audio_path or not os.path.isfile(audio_path):
            continue
        try:
            offset = float(m.group(2))
        except (TypeError, ValueError):
            continue
        key = os.path.abspath(audio_path)
        if key in seen:
            continue
        seen.add(key)
        results.append({"audio_path": audio_path, "offset_seconds": offset})
    return results


def discover_synced_audios(video_path, transcript_path=None):
    """Return a list of {"audio_path", "offset_seconds", "channel_count",
    "enabled", "channels"} for every external audio track synced to
    `video_path`, or [] if none.

    `channel_count` (int) is the file's real probed channel count; the
    routing fields `enabled` (bool, default True) and `channels` (the
    sidecar routing list, or None = all — addendum v4) are read straight
    from the source track and returned as-is. splice_external_audio does
    the filtering (skip disabled, emit only selected channels); discovery
    just surfaces the fields.

    Merge order (first source wins per unique abspath):

    1. CACHE `sync_tracks` (addendum v9 — one sidecar per video, not two):
       once a video has been both synced and transcribed, ALL synced
       tracks live inside `.ivt-cache.json` as `sync_tracks`/`sync_method`,
       and the standalone `.sync-offsets.json` sidecar is deleted. Checked
       via key PRESENCE (`"sync_tracks" in cache`), not truthiness — an
       empty list still means "already consolidated, nothing synced",
       distinct from "never consolidated, check the sidecar".
    2. SIDECAR (only when the cache has no `sync_tracks` key — a
       synced-but-not-yet-transcribed video, or a video transcribed by the
       standalone Local Interview Transcriber, which knows nothing about
       sync): the `.sync-offsets.json` sidecar, written whenever the user
       runs Detect Sync in the Sync workspace and updated on every nudge.
    3. LEGACY CACHE FALLBACK: an older cache (from before addendum v9, or
       written via 'Transcribe this track' before any full sync_tracks
       existed) may carry only a single `audio_source` (+ optional
       `sync_offset_seconds`) — used only if neither of the above found
       anything.
    4. TRANSCRIPT-NOTE FALLBACK (addendum v5, optional `transcript_path`):
       for any audio path NOT already found by the sources above, read it
       back out of the transcript's own embedded
       `NOTE Source audio: ...` lines via `parse_audio_notes_from_transcript`
       — this is what keeps the association alive when the sidecar/cache
       don't survive alongside the video. Entries found ONLY this way
       default `enabled=True, channels=None` (routing isn't recorded in the
       note — this is a durability fallback, not a full routing store).

    ffprobe supplies each file's channel count. Files that no longer exist
    on disk are skipped."""
    if not video_path or not isinstance(video_path, str):
        return []

    results = []
    seen = set()

    def _add_track(ap, off, enabled, channels):
        if not ap or not os.path.isfile(ap):
            return
        key = os.path.abspath(ap)
        if key in seen:
            return
        seen.add(key)
        results.append({"audio_path": ap, "offset_seconds": off,
                        "channel_count": max(1, _probe_channels(ap)),
                        "enabled": enabled, "channels": channels})

    cache = _read_json(braw_bridge.ivt_cache_path(video_path))
    cache_has_sync_tracks = isinstance(cache, dict) and "sync_tracks" in cache

    if cache_has_sync_tracks:
        for t in cache.get("sync_tracks") or []:
            try:
                off = float(t.get("offset_seconds") or 0.0)
            except (TypeError, ValueError):
                off = 0.0
            enabled = t.get("enabled")
            _add_track(t.get("path"), off,
                       True if enabled is None else bool(enabled), t.get("channels"))
    else:
        side = _read_json(braw_bridge.sync_offsets_path(video_path))
        if isinstance(side, dict):
            for t in side.get("tracks") or []:
                try:
                    off = float(t.get("offset_seconds") or 0.0)
                except (TypeError, ValueError):
                    off = 0.0
                enabled = t.get("enabled")
                _add_track(t.get("path"), off,
                           True if enabled is None else bool(enabled), t.get("channels"))

    # Legacy fallback: a cache with no sync_tracks key at all may still
    # carry the older single audio_source (+ sync_offset_seconds).
    if isinstance(cache, dict) and not cache_has_sync_tracks:
        ap = cache.get("audio_source")
        if ap:
            off = cache.get("sync_offset_seconds")
            try:
                off = float(off) if off is not None else 0.0
            except (TypeError, ValueError):
                off = 0.0
            _add_track(ap, off, True, None)

    # Transcript-note fallback (addendum v5): only for a path not already
    # found via the sidecar or the cache above — this is a durability net,
    # not a routing store, so notes-only entries get enabled=True/all-
    # channels regardless of what an earlier sidecar might have recorded
    # for a DIFFERENT path.
    if transcript_path:
        for note in parse_audio_notes_from_transcript(transcript_path):
            _add_track(note["audio_path"], note["offset_seconds"], True, None)

    return results


# Back-compat single-result helper (returns the first synced track or None).
# Original one-arg signature kept unchanged for any other caller — it just
# forwards to the two-arg version with no transcript to fall back to.
def discover_synced_audio(video_path):
    found = discover_synced_audios(video_path, None)
    return found[0] if found else None


# --------------------------------------------------------------------------
# XMEML helpers (match Rough Cut Studio's xml_builder output exactly)
# --------------------------------------------------------------------------

def _to_pathurl(path):
    """file://localhost/abs/path — byte-for-byte the form RCS's
    _to_pathurl emits, so spliced clips relink the same way its own do."""
    abspath = os.path.abspath(path).replace(os.sep, "/")
    if not abspath.startswith("/"):
        abspath = "/" + abspath
    return "file://localhost" + abspath


def _rate_elem(parent, fps):
    """RCS's rule: timebase = round(fps); ntsc TRUE for any fps not in the
    integer-clean set {24,25,30,50,60}."""
    rate = ET.SubElement(parent, "rate")
    ET.SubElement(rate, "timebase").text = str(round(fps))
    ET.SubElement(rate, "ntsc").text = "TRUE" if fps not in (24, 25, 30, 50, 60) else "FALSE"


def _audio_clip(track_el, clip_id, file_id, name, fps, start, end, in_f, out_f,
                channel, define_file=None):
    clip = ET.SubElement(track_el, "clipitem", id=clip_id)
    ET.SubElement(clip, "name").text = name
    ET.SubElement(clip, "enabled").text = "TRUE"
    ET.SubElement(clip, "duration").text = str(end - start)
    _rate_elem(clip, fps)
    ET.SubElement(clip, "start").text = str(start)
    ET.SubElement(clip, "end").text = str(end)
    ET.SubElement(clip, "in").text = str(in_f)
    ET.SubElement(clip, "out").text = str(out_f)
    if define_file is not None:
        # Full <file> definition on this file's first clip; later clips of
        # the same file reference it by id (RCS's pattern).
        path, probe_channels, samplerate, depth, duration_frames = define_file
        f = ET.SubElement(clip, "file", id=file_id)
        ET.SubElement(f, "name").text = os.path.basename(path)
        ET.SubElement(f, "pathurl").text = _to_pathurl(path)
        _rate_elem(f, fps)
        if duration_frames:
            ET.SubElement(f, "duration").text = str(duration_frames)
        media = ET.SubElement(f, "media")
        fa = ET.SubElement(media, "audio")
        sc = ET.SubElement(fa, "samplecharacteristics")
        ET.SubElement(sc, "depth").text = str(depth)
        ET.SubElement(sc, "samplerate").text = str(samplerate)
        # Source file's REAL channel count — required for Premiere to
        # resolve <sourcetrack><trackindex>. (This is a <file>-level count,
        # never a per-timeline-clipitem <channelcount>, which is the thing
        # that silently mono-collapses.)
        ET.SubElement(fa, "channelcount").text = str(probe_channels)
    else:
        ET.SubElement(clip, "file", id=file_id)
    # channel == DOWNMIX (0) -> NO <sourcetrack> (Premiere sums the whole
    # file); any other value pins the clip to that 1-based source channel.
    if channel != DOWNMIX:
        st = ET.SubElement(clip, "sourcetrack")
        ET.SubElement(st, "mediatype").text = "audio"
        ET.SubElement(st, "trackindex").text = str(channel)
    return clip


# --------------------------------------------------------------------------
# The splice
# --------------------------------------------------------------------------

def splice_external_audio(xml_string, resolved_segments, media_paths, fps,
                          discover_fn=discover_synced_audios, source_paths=None):
    """Return (new_xml_string, warnings).

    If no main cut resolves to a synced source, returns (None, []) so the
    caller can fall through to Rough Cut Studio's stock export unchanged.
    `discover_fn(video_path, transcript_path)` returns a LIST of
    synced-audio dicts (a video can be synced against several recorders —
    all are placed). `source_paths` (addendum v5, optional
    `dict[source_id -> transcript_path]`) lets discovery fall back to a
    transcript's own embedded `NOTE Source audio: ...` line when no sidecar
    or cache exists next to the video — see `discover_synced_audios`.
    """
    warnings = []
    if not xml_string:
        return None, warnings

    mains = sorted([s for s in (resolved_segments or [])
                    if (s or {}).get("track", "main") == "main"],
                   key=lambda s: s.get("order", 0))
    if not mains:
        return None, warnings

    # Which distinct sources are synced? (probe/read once per source.)
    assoc_by_source = {}
    any_synced = False
    for seg in mains:
        sid = seg.get("source_id")
        if sid in assoc_by_source:
            continue
        assoc_by_source[sid] = discover_fn((media_paths or {}).get(sid),
                                            (source_paths or {}).get(sid)) or []
        if assoc_by_source[sid]:
            any_synced = True
    if not any_synced:
        return None, warnings  # nothing to add — keep RCS's output verbatim

    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError as e:
        warnings.append(f"Could not parse the exported XML to add synced audio: {e}")
        return None, warnings

    audio_el = root.find(".//sequence/media/audio")
    video_el = root.find(".//sequence/media/video")
    if audio_el is None or video_el is None:
        warnings.append("Exported XML has no <media><audio>/<video>; synced audio not added.")
        return None, warnings

    # V1 clipitems, in document (timeline) order = main cuts in order.
    v1 = video_el.find("track")
    v1_clips = v1.findall("clipitem") if v1 is not None else []
    if len(v1_clips) != len(mains):
        warnings.append(
            f"Timeline/clip count mismatch ({len(v1_clips)} video clips vs "
            f"{len(mains)} main cuts) — synced audio matched for the first "
            f"{min(len(v1_clips), len(mains))}.")

    existing_tracks = len(audio_el.findall("track"))

    # One <file> id + a dedicated block of channel-tracks per distinct
    # synced audio file; all cuts of that file share them.
    file_ids = {}          # audio_path -> file id
    file_defined = set()   # audio_path already carrying its full <file> def
    file_tracks = {}       # audio_path -> [track elements, one per channel]
    uid = [0]

    def new_id(prefix):
        uid[0] += 1
        return f"clipitem-sync{prefix}-{uid[0]}"

    placed_any = False
    for idx in range(min(len(v1_clips), len(mains))):
        seg = mains[idx]
        assocs = assoc_by_source.get(seg.get("source_id")) or []
        if not assocs:
            continue
        v_clip = v1_clips[idx]
        try:
            tl_start = int(v_clip.findtext("start"))
            tl_end = int(v_clip.findtext("end"))
        except (TypeError, ValueError):
            warnings.append(f"Clip {idx + 1}: unreadable timeline position — skipped for synced audio.")
            continue
        span = tl_end - tl_start
        if span <= 0:
            continue
        try:
            cut_in = float(seg.get("in_seconds") or 0.0)
        except (TypeError, ValueError):
            cut_in = 0.0

        for assoc in assocs:
            # Routing (addendum v4): a disabled track is excluded entirely.
            if assoc.get("enabled") is False:
                continue
            offset = float(assoc["offset_seconds"])
            ext_in = int(round((cut_in - offset) * fps))
            new_start = tl_start
            if ext_in < 0:
                # Audio doesn't reach back that far: trim head, shift start.
                new_start = tl_start + (-ext_in)
                ext_in = 0
                if new_start >= tl_end:
                    warnings.append(
                        f"{os.path.basename(assoc['audio_path'])}: synced audio begins "
                        f"after cut {idx + 1} ends (offset {offset:+.3f}s) — that clip's "
                        f"external audio was skipped.")
                    continue
            ext_out = ext_in + (tl_end - new_start)

            audio_path = assoc["audio_path"]
            file_channels = assoc["channel_count"]
            # Which clipitems to emit — same shared resolver the Sync
            # workspace builder uses, so both agree on channel selection and
            # the downmix marker. The file-level channelcount stays the real
            # source count regardless.
            emit = resolve_emit_channels(assoc.get("channels"), file_channels)
            if audio_path not in file_ids:
                file_ids[audio_path] = f"file-sync-{len(file_ids) + 1}"
                file_tracks[audio_path] = [ET.SubElement(audio_el, "track")
                                           for _ in range(len(emit))]
            fid = file_ids[audio_path]
            tracks = file_tracks[audio_path]

            for i, ch in enumerate(emit):
                define = None
                if audio_path not in file_defined and i == 0:
                    # Real samplerate/depth/duration via ffprobe — a wrong
                    # guess here (esp. samplerate) is what caused Premiere
                    # to report "insufficient audio for edit" on correctly
                    # synced clips. The channelcount is the SOURCE file's
                    # real count.
                    samplerate, depth, duration_frames = _probe_audio_format(audio_path, fps)
                    define = (audio_path, file_channels, samplerate, depth, duration_frames)
                    file_defined.add(audio_path)
                _audio_clip(tracks[i], new_id(f"{idx + 1}f{fid[-1]}c{i + 1}"), fid,
                            os.path.basename(audio_path), fps,
                            new_start, tl_end, ext_in, ext_out, ch,
                            define_file=define)
            placed_any = True

    if not placed_any:
        return None, warnings

    # Reflect the added tracks in the sequence's output-channel count.
    total_tracks = len(audio_el.findall("track"))
    noc = audio_el.find("numOutputChannels")
    if noc is None:
        noc = ET.Element("numOutputChannels")
        audio_el.insert(0, noc)
    noc.text = str(max(existing_tracks, total_tracks))

    rough = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="  ")
    lines = [ln for ln in pretty.split("\n") if ln.strip()]
    body = "\n".join(lines)
    new_xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
               + body[body.find("\n") + 1:])
    return new_xml, warnings
