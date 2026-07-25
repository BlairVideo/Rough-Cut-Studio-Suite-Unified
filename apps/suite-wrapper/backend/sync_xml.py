"""
sync_xml.py — non-merged XMEML (v5) sequence builder for the Sync
workspace. Pure stdlib; runs in the suite venv, in-process.

build_sync_xml(video, tracks, include_camera_audio, sequence_name)
    -> (xml_string, warnings)

  video  = {"path": str, "probe": Probe}
  tracks = [{"path": str, "offset_seconds": float, "probe": Probe}]
  Probe  = the sync_worker probe dict (duration/fps/width/height/
           has_audio/audio_channels/audio_samplerate/audio_bits/…) —
           passed through from the sync job result / sync_probe so no
           re-probing happens here.

Conventions follow the suite's two existing builders — timebase/ntsc
rounding and file-URL pathurls from the B-Roll Analyzer's xml_export.py,
linked-clip structure from Rough Cut Studio's xml_builder.py:

  * The sequence rate/duration/dimensions come from the VIDEO's probe.
  * V1 carries ONE clipitem for the full video file, start 0, referencing
    the ORIGINAL video path.
  * Camera audio (only when include_camera_audio): one clipitem PER SOURCE
    CHANNEL — never a single channelcount=2 item, which Premiere silently
    imports as mono — each on its own track, <sourcetrack> pinned to its
    channel, all linked to the video clipitem via <link> groups.
  * Each external audio file: one clipitem per channel on its own
    subsequent track(s), referencing the ORIGINAL audio file path — no
    merged media, no rendered file. Channels of the same file link to each
    other (one master clip per file) but external files do NOT link to the
    video clip: separate masters, exactly the "non-merged clips"
    requirement.
  * Offset sign (contract addendum v3: video_time = audio_time + offset):
    positive offset -> start = round(offset*fps), in = 0; negative ->
    start = 0, in = round(-offset*fps) (head trimmed). end = min(sequence
    end, start + remaining source duration). A track whose audible range
    falls entirely outside [0, sequence end] is dropped with a warning.
  * <channelcount> is written on each <file>'s <media><audio> block (the
    source file's real channel count — Premiere needs this to resolve
    which source channel a clipitem's <sourcetrack><trackindex> pulls
    from; omitting it left every external audio clipitem unable to link
    to its file, which is why the first version of this builder produced
    XML that opened with no audio at all). What must NEVER happen is a
    *clipitem* on the TIMELINE claiming <channelcount>2</channelcount> for
    itself — that's the pattern that silently collapses to mono in
    Premiere (see the B-Roll Analyzer's xml_export.py, which this file-def
    shape is copied from verbatim). Channel routing on the timeline is
    still expressed only through one clipitem per channel + <sourcetrack>;
    the sequence's own output width is declared via <numOutputChannels>.
"""

import os
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom


# --------------------------------------------------------------------------
# Shared channel-routing resolver (contract addendum v4)
# --------------------------------------------------------------------------

# The DOWNMIX sentinel: an emit-spec of 0 means "emit ONE mono clipitem with
# NO <sourcetrack>" — Premiere then plays the file's full mixdown.
DOWNMIX = 0


def resolve_emit_channels(channels, file_channel_count):
    """Interpret a synced track's sidecar ``channels`` routing field into the
    ordered list of clipitems to emit for one audio file. This is the SINGLE
    source of truth shared by both XMEML builders (``sync_xml`` and
    ``synced_audio_splice``) so they always agree.

    Args:
        channels: the sidecar routing value —
            * ``None``  -> all source channels (default),
            * ``[]``    -> all source channels (empty means all; never zero),
            * ``[1, 2, ...]`` -> the selected 1-based SOURCE channel indices,
            * a list CONTAINING ``0`` (e.g. ``[0]``) -> the downmix marker.
        file_channel_count: the file's real channel count N (>= 1).

    Returns a NON-EMPTY list of int emit-specs, each interpreted by the
    builders identically:
        * ``0`` (``DOWNMIX``) -> emit exactly ONE mono clipitem with NO
          ``<sourcetrack>`` (Premiere sums the whole file). When a 0 is
          present the result is always exactly ``[0]``.
        * ``k`` -> emit a clipitem whose ``<sourcetrack><trackindex>`` is
          ``k`` (a 1-based source channel).

    Rules: ``None``/empty -> ``[1..N]``; a list containing 0 -> ``[0]``;
    otherwise the requested channels clamped to the valid ``1..N`` range,
    de-duplicated, ascending; if nothing valid survives, falls back to all
    channels ``[1..N]`` (the builders never emit zero clipitems)."""
    n = max(1, int(file_channel_count or 1))
    all_channels = list(range(1, n + 1))
    if channels is None:
        return all_channels
    try:
        requested = list(channels)
    except TypeError:
        return all_channels
    if not requested:
        return all_channels
    # Coerce to ints; ignore anything non-numeric.
    ints = []
    for c in requested:
        try:
            ints.append(int(c))
        except (TypeError, ValueError):
            continue
    if DOWNMIX in ints:
        return [DOWNMIX]
    valid = sorted({c for c in ints if 1 <= c <= n})
    return valid or all_channels


# --------------------------------------------------------------------------
# Small helpers (same rounding rules as the B-Roll exporter)
# --------------------------------------------------------------------------

def _frames(seconds, fps):
    return int(round(float(seconds) * float(fps)))


def _rate_elem(parent, fps):
    """timebase = round(fps); ntsc TRUE only for the 23.976/29.97/59.94
    family (fps rounds to 24/30/60 but isn't integral) — the B-Roll
    exporter's rule."""
    fps = float(fps or 0.0) or 25.0
    fps_int = int(round(fps))
    ntsc = fps_int in (24, 30, 60) and abs(fps - fps_int) > 0.01
    rate = ET.SubElement(parent, "rate")
    ET.SubElement(rate, "timebase").text = str(fps_int)
    ET.SubElement(rate, "ntsc").text = "TRUE" if ntsc else "FALSE"


def _file_url(path):
    """file:///Users/... — same encoding as the B-Roll exporter (ET handles
    the XML escaping when serializing)."""
    abspath = os.path.abspath(path).replace(os.sep, "/")
    if not abspath.startswith("/"):
        abspath = "/" + abspath
    return "file://" + abspath


def _probe_get(probe, key, default=None):
    if isinstance(probe, dict):
        value = probe.get(key)
        if value is not None:
            return value
    return default


def _clipitem(track_el, item_id, name, fps, start, end, in_f, out_f):
    clip = ET.SubElement(track_el, "clipitem", id=item_id)
    ET.SubElement(clip, "name").text = name
    ET.SubElement(clip, "enabled").text = "TRUE"
    ET.SubElement(clip, "duration").text = str(end - start)
    _rate_elem(clip, fps)
    ET.SubElement(clip, "start").text = str(start)
    ET.SubElement(clip, "end").text = str(end)
    ET.SubElement(clip, "in").text = str(in_f)
    ET.SubElement(clip, "out").text = str(out_f)
    return clip


def _sourcetrack(clip_el, channel):
    st = ET.SubElement(clip_el, "sourcetrack")
    ET.SubElement(st, "mediatype").text = "audio"
    ET.SubElement(st, "trackindex").text = str(channel)


def _add_links(clip_el, members):
    """members: [(clipitem_id, mediatype, sequence_trackindex)] — every
    clipitem in a link group carries the full member list (RCS's
    _add_stereo_links pattern; clipindex is 1, each track here holds one
    clip)."""
    for linkref, mediatype, trackindex in members:
        link = ET.SubElement(clip_el, "link")
        ET.SubElement(link, "linkclipref").text = linkref
        ET.SubElement(link, "mediatype").text = mediatype
        ET.SubElement(link, "trackindex").text = str(trackindex)
        ET.SubElement(link, "clipindex").text = "1"


def _file_def(parent, file_id, path, probe, fps, with_video, audio_channels=None):
    """Full <file> definition (first use only; later clipitems reference it
    as <file id=.../>). The <audio> block's <channelcount> describes the
    SOURCE FILE's real channel count — required for Premiere to resolve a
    clipitem's <sourcetrack><trackindex> back to a specific source channel
    (see module docstring for why this is NOT the same thing as the
    banned per-clipitem channelcount=2 pattern)."""
    file_el = ET.SubElement(parent, "file", id=file_id)
    ET.SubElement(file_el, "name").text = os.path.basename(path)
    ET.SubElement(file_el, "pathurl").text = _file_url(path)
    _rate_elem(file_el, fps)
    duration = float(_probe_get(probe, "duration", 0.0) or 0.0)
    if duration > 0:
        ET.SubElement(file_el, "duration").text = str(_frames(duration, fps))
    media = ET.SubElement(file_el, "media")
    if with_video:
        fvideo = ET.SubElement(media, "video")
        fvchar = ET.SubElement(fvideo, "samplecharacteristics")
        _rate_elem(fvchar, fps)
        ET.SubElement(fvchar, "width").text = str(int(_probe_get(probe, "width", 1920) or 1920))
        ET.SubElement(fvchar, "height").text = str(int(_probe_get(probe, "height", 1080) or 1080))
    if _probe_get(probe, "has_audio", True):
        faudio = ET.SubElement(media, "audio")
        fachar = ET.SubElement(faudio, "samplecharacteristics")
        ET.SubElement(fachar, "depth").text = str(int(_probe_get(probe, "audio_bits", 16) or 16))
        ET.SubElement(fachar, "samplerate").text = str(int(_probe_get(probe, "audio_samplerate", 48000) or 48000))
        channels = audio_channels if audio_channels is not None else \
            int(_probe_get(probe, "audio_channels", 1) or 1)
        ET.SubElement(faudio, "channelcount").text = str(max(1, int(channels)))
    return file_el


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------

def build_sync_xml(video, tracks, include_camera_audio=False,
                   sequence_name="Synced Sequence"):
    """Returns (xml_string, warnings). See module docstring."""
    warnings = []

    video_path = (video or {}).get("path")
    if not video_path:
        raise ValueError("build_sync_xml: video.path is required")
    vprobe = (video or {}).get("probe") or {}
    fps = float(_probe_get(vprobe, "fps", 0.0) or 0.0) or 25.0
    video_duration = float(_probe_get(vprobe, "duration", 0.0) or 0.0)
    seq_frames = max(1, _frames(video_duration, fps))
    width = int(_probe_get(vprobe, "width", 1920) or 1920)
    height = int(_probe_get(vprobe, "height", 1080) or 1080)

    cam_channels = 0
    if include_camera_audio and _probe_get(vprobe, "has_audio", False):
        cam_channels = max(0, int(_probe_get(vprobe, "audio_channels", 0) or 0))

    # ---- place every external track first (frame math + drop pass) --------
    placed = []
    for t in tracks or []:
        path = (t or {}).get("path")
        if not path:
            continue
        # Routing (addendum v4): a disabled track is excluded entirely.
        if (t or {}).get("enabled") is False:
            continue
        name = os.path.basename(path)
        probe = t.get("probe") or {}
        try:
            offset = float(t.get("offset_seconds") or 0.0)
        except (TypeError, ValueError):
            offset = 0.0
        file_channels = max(1, int(_probe_get(probe, "audio_channels", 1) or 1))
        # The clipitems to actually emit for this file (shared resolver so
        # the Edit-workspace splice interprets `channels` identically).
        emit = resolve_emit_channels(t.get("channels"), file_channels)
        source_duration = float(_probe_get(probe, "duration", 0.0) or 0.0)

        if offset >= 0:
            start = _frames(offset, fps)
            in_f = 0
        else:
            start = 0
            in_f = _frames(-offset, fps)

        if source_duration > 0:
            file_frames = _frames(source_duration, fps)
        else:
            # Unknown source duration: assume it can fill the sequence.
            warnings.append(
                f"{name}: source duration unknown — clip extended to the "
                f"end of the sequence.")
            file_frames = in_f + max(0, seq_frames - start)

        remaining = file_frames - in_f
        end = min(seq_frames, start + remaining)
        if remaining <= 0 or start >= seq_frames or end <= start:
            warnings.append(
                f"{name}: audible range falls entirely outside the sequence "
                f"(offset {offset:+.3f} s) — dropped from the XML.")
            continue

        placed.append({
            "path": path, "name": name, "probe": probe,
            "file_channels": file_channels, "emit": emit,
            "start": start, "in": in_f, "end": end,
        })

    # ---- sequence audio track layout (one track per emitted clipitem) -----
    # Camera channels occupy tracks 1..cam_channels, then each external
    # file's EMITTED channels take the next consecutive tracks.
    total_audio_tracks = cam_channels + sum(len(p["emit"]) for p in placed)

    seq_samplerate = int(_probe_get(vprobe, "audio_samplerate", 0) or 0)
    seq_depth = int(_probe_get(vprobe, "audio_bits", 0) or 0)
    if not seq_samplerate:
        for p in placed:
            seq_samplerate = int(_probe_get(p["probe"], "audio_samplerate", 0) or 0)
            seq_depth = seq_depth or int(_probe_get(p["probe"], "audio_bits", 0) or 0)
            if seq_samplerate:
                break
    seq_samplerate = seq_samplerate or 48000
    seq_depth = seq_depth or 16

    # ---- skeleton ----------------------------------------------------------
    xmeml = ET.Element("xmeml", version="5")
    sequence = ET.SubElement(xmeml, "sequence", id="sequence-1")
    ET.SubElement(sequence, "name").text = str(sequence_name or "Synced Sequence")
    ET.SubElement(sequence, "duration").text = str(seq_frames)
    _rate_elem(sequence, fps)
    media = ET.SubElement(sequence, "media")

    video_el = ET.SubElement(media, "video")
    vformat = ET.SubElement(video_el, "format")
    vsc = ET.SubElement(vformat, "samplecharacteristics")
    _rate_elem(vsc, fps)
    ET.SubElement(vsc, "width").text = str(width)
    ET.SubElement(vsc, "height").text = str(height)
    vtrack = ET.SubElement(video_el, "track")

    audio_el = ET.SubElement(media, "audio")
    ET.SubElement(audio_el, "numOutputChannels").text = str(max(1, total_audio_tracks))
    aformat = ET.SubElement(audio_el, "format")
    asc = ET.SubElement(aformat, "samplecharacteristics")
    ET.SubElement(asc, "depth").text = str(seq_depth)
    ET.SubElement(asc, "samplerate").text = str(seq_samplerate)

    # ---- V1: the one video clipitem ---------------------------------------
    video_name = os.path.basename(video_path)
    video_item_id = "clipitem-video-1"
    v_clip = _clipitem(vtrack, video_item_id, video_name, fps,
                       start=0, end=seq_frames, in_f=0, out_f=seq_frames)
    video_audio_channels = int(_probe_get(vprobe, "audio_channels", 0) or 0) or max(1, cam_channels)
    _file_def(v_clip, "file-video", video_path, vprobe, fps, with_video=True,
              audio_channels=video_audio_channels)

    cam_ids = [f"clipitem-cam-a{ch}" for ch in range(1, cam_channels + 1)]
    cam_group = ([(video_item_id, "video", 1)] +
                 [(cam_ids[ch - 1], "audio", ch) for ch in range(1, cam_channels + 1)])
    if cam_channels:
        _add_links(v_clip, cam_group)

    # ---- camera audio: one clipitem per source channel, linked to V1 ------
    for ch in range(1, cam_channels + 1):
        track_el = ET.SubElement(audio_el, "track")
        a_clip = _clipitem(track_el, cam_ids[ch - 1], video_name, fps,
                           start=0, end=seq_frames, in_f=0, out_f=seq_frames)
        ET.SubElement(a_clip, "file", id="file-video")
        _sourcetrack(a_clip, ch)
        _add_links(a_clip, cam_group)

    # ---- external files: per-channel clipitems, separate master clips -----
    # `emit` (from resolve_emit_channels) lists the clipitems to write: a
    # 1-based source channel each carrying <sourcetrack>, or a single
    # DOWNMIX (0) marker for one mono clip with NO <sourcetrack>. The
    # file-level <channelcount> stays the SOURCE file's real count either
    # way (Premiere needs it to resolve <sourcetrack>).
    next_track_index = cam_channels + 1
    for fi, p in enumerate(placed, start=1):
        file_id = f"file-audio-{fi}"
        out_f = p["in"] + (p["end"] - p["start"])
        emit = p["emit"]
        item_ids = [f"clipitem-ext{fi}-a{i}" for i in range(1, len(emit) + 1)]
        group = [(item_ids[i], "audio", next_track_index + i)
                 for i in range(len(emit))]
        for i, ch in enumerate(emit):
            track_el = ET.SubElement(audio_el, "track")
            a_clip = _clipitem(track_el, item_ids[i], p["name"], fps,
                               start=p["start"], end=p["end"],
                               in_f=p["in"], out_f=out_f)
            if i == 0:
                _file_def(a_clip, file_id, p["path"], p["probe"], fps,
                          with_video=False, audio_channels=p["file_channels"])
            else:
                ET.SubElement(a_clip, "file", id=file_id)
            if ch != DOWNMIX:
                _sourcetrack(a_clip, ch)
            if len(emit) > 1:
                # Channels of one file link to EACH OTHER only — never to
                # the video clip (separate masters = non-merged).
                _add_links(a_clip, group)
        next_track_index += len(emit)

    # ---- serialize (RCS xml_builder's pretty-print + DOCTYPE pattern) -----
    rough = ET.tostring(xmeml, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="  ")
    lines = [ln for ln in pretty.split("\n") if ln.strip()]
    body = "\n".join(lines)
    xml_string = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
                  + body[body.find("\n") + 1:])
    return xml_string, warnings
