"""
fcpxml_builder.py

Builds a Final Cut Pro X XML (FCPXML) document, version 1.11 -- the modern
interchange format used by current Final Cut Pro (and read by DaVinci
Resolve), as opposed to the older XMEML format xml_builder.py produces for
Premiere Pro. The two formats are structurally quite different:

  * XMEML is track-based (V1/V2, A1/A2) with explicit clipitems on each
    track and <link> elements tying video/audio together.
  * FCPXML is resource-based: a <resources> block declares each source
    file once as an <asset>, and the timeline (<spine>) places <asset-clip>
    elements that reference those assets. A single asset-clip already
    carries its own synced audio -- no manual L/R track splitting needed.
    Overlays (our B-roll) aren't a second track; they're "connected clips"
    nested inside a host clip with a lane attribute, positioned by an
    offset that's relative to that HOST CLIP's own start, not the overall
    timeline. Getting that relative-vs-absolute distinction backwards is
    the single easiest way to silently misplace every overlay on import,
    so it's called out explicitly here.

All times are expressed as exact rational seconds ("N/Ds") matching the
project's frame duration, e.g. "100/2500s" per frame at 25fps, so every
value lands precisely on a frame boundary -- no float rounding drift.

No network access is required or used here; this is local XML generation.
"""

import os
import math
import uuid
from urllib.parse import quote
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom

FCPXML_VERSION = "1.11"
STEREO_CHANNELS = 2

# NTSC-family rates use non-reduced fractions matching Apple's own
# convention (e.g. 1001/24000s for 23.976), rather than a decimal
# approximation. Everything else is treated as an exact integer rate.
_NTSC_FRAME_DURATIONS = {
    23.976: (1001, 24000),
    29.97: (1001, 30000),
    59.94: (1001, 60000),
}


def _frame_duration_fraction(fps: float):
    for rate, fraction in _NTSC_FRAME_DURATIONS.items():
        if abs(fps - rate) < 0.01:
            return fraction
    return (100, int(round(fps * 100)))


def _uid():
    return uuid.uuid4().hex.upper()


def _seconds_to_fcp_time(total_seconds: float, fps: float) -> str:
    """Frame-accurate rational time string, e.g. '12345/2500s'."""
    num, den = _frame_duration_fraction(fps)
    frames = max(0, round(total_seconds * fps))
    numerator = frames * num
    if numerator == 0:
        return "0s"
    g = math.gcd(numerator, den)
    return f"{numerator // g}/{den // g}s"


def _format_name_label(fps: float, height: int) -> str:
    if abs(fps - round(fps)) < 0.001:
        rate_label = str(round(fps))
    else:
        rate_label = str(fps).replace(".", "")
    return f"FFVideoFormat{height}p{rate_label}"


def _to_fcp_src_url(path: str) -> str:
    abspath = os.path.abspath(path)
    normalized = abspath.replace(os.sep, "/")
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return "file://" + quote(normalized, safe="/:")


def build_fcpxml(
    sequence_name: str,
    fps: float,
    resolved_segments: list,
    broll_segments: list = None,
    main_duck_db: dict = None,
    video_width: int = 1920,
    video_height: int = 1080,
    audio_sample_rate: int = 48000,
):
    """
    resolved_segments: the "main" cuts, same shape as xml_builder's:
        {"order", "source_path", "source_name", "in_seconds", "out_seconds", "note"}

    broll_segments: optional overlays, same shape plus "timeline_start_seconds"
    and "audio_mode" ("silent" | "full" | "duck_main", default "silent").
    Each is anchored (as a connected clip) to whichever main clip's
    timespan it falls within; one that falls before the first or after the
    last main clip is anchored to that nearest edge clip instead, and a
    warning is returned so you know to double check its placement. B-roll
    clips that overlap each other in absolute timeline position are placed
    on separate lanes (2, 3, ...) rather than all sharing lane 1, since
    Final Cut renders same-lane connected clips on one shared row
    regardless of which host clip they're anchored to.

    main_duck_db: optional {order: db} — main clips whose audio should be
    flatly attenuated for their entire duration because a "duck_main"
    B-roll clip overlaps them (computed by the caller, api.py, since only
    it knows the overlap in time). Applied via the same built-in
    <adjust-volume> element used to mute silent B-roll, just with a less
    extreme value.

    Returns (xml_string, warnings_list).
    """
    resolved_segments = sorted(resolved_segments, key=lambda s: s["order"])
    broll_segments = broll_segments or []
    main_duck_db = main_duck_db or {}
    warnings = []

    if not resolved_segments:
        raise ValueError("build_fcpxml requires at least one main cut.")

    # ---- compute each main clip's absolute timeline position (seconds) ----
    timeline_pos = 0.0
    main_spans = []  # (seg, abs_start, abs_end)
    for seg in resolved_segments:
        clip_len = max(0.0, seg["out_seconds"] - seg["in_seconds"])
        main_spans.append((seg, timeline_pos, timeline_pos + clip_len))
        timeline_pos += clip_len
    total_duration_seconds = timeline_pos

    # ---- resources: one <format>, one <asset> per unique source file ----
    fcpxml = ET.Element("fcpxml", version=FCPXML_VERSION)
    resources = ET.SubElement(fcpxml, "resources")

    format_id = "r1"
    frame_num, frame_den = _frame_duration_fraction(fps)
    ET.SubElement(
        resources, "format",
        id=format_id,
        name=_format_name_label(fps, video_height),
        frameDuration=f"{frame_num}/{frame_den}s",
        width=str(video_width),
        height=str(video_height),
    )

    all_segments_for_assets = resolved_segments + broll_segments
    asset_id_by_path = {}
    max_out_by_path = {}
    for seg in all_segments_for_assets:
        path = seg["source_path"]
        max_out_by_path[path] = max(max_out_by_path.get(path, 0.0), seg["out_seconds"])

    next_id = 2
    for path, max_out in max_out_by_path.items():
        asset_id = f"r{next_id}"
        next_id += 1
        asset_id_by_path[path] = asset_id
        display_name = os.path.basename(path)
        asset_duration = _seconds_to_fcp_time(max_out + 1.0, fps)  # small safety buffer
        asset = ET.SubElement(
            resources, "asset",
            id=asset_id,
            name=display_name,
            uid=_uid(),
            start="0s",
            duration=asset_duration,
            hasVideo="1",
            format=format_id,
            videoSources="1",
            hasAudio="1",
            audioSources="1",
            audioChannels=str(STEREO_CHANNELS),
            audioRate=str(audio_sample_rate),
        )
        ET.SubElement(asset, "media-rep", kind="original-media", src=_to_fcp_src_url(path))

    # ---- library / event / project / sequence / spine ----
    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", name=f"{sequence_name} Event")
    project = ET.SubElement(event, "project", name=sequence_name)
    sequence = ET.SubElement(
        project, "sequence",
        format=format_id,
        duration=_seconds_to_fcp_time(total_duration_seconds, fps),
        tcStart="0s",
        tcFormat="NDF",
    )
    spine = ET.SubElement(sequence, "spine")

    one_frame_seconds = 1.0 / fps
    main_clip_elements = []  # parallel to main_spans, for anchoring B-roll

    for seg, abs_start, abs_end in main_spans:
        clip_len = abs_end - abs_start
        clip = ET.SubElement(
            spine, "asset-clip",
            ref=asset_id_by_path[seg["source_path"]],
            name=seg.get("source_name", "Clip"),
            offset=_seconds_to_fcp_time(abs_start, fps),
            duration=_seconds_to_fcp_time(clip_len, fps),
            start=_seconds_to_fcp_time(seg["in_seconds"], fps),
            format=format_id,
            tcFormat="NDF",
            audioRole="dialogue",
        )
        note = seg.get("note")
        if note:
            ET.SubElement(
                clip, "marker",
                start="0s",
                duration=_seconds_to_fcp_time(one_frame_seconds, fps),
                value=note[:200],
            )

        duck_db = main_duck_db.get(seg["order"])
        if duck_db is not None:
            ET.SubElement(clip, "adjust-volume", amount=f"{duck_db}dB")

        main_clip_elements.append(clip)

    # ---- B-roll: connected clips, offset relative to their host ----
    # Final Cut renders every connected clip sharing a given lane number on
    # the same horizontal row above the primary storyline, regardless of
    # which host clip each is anchored to -- so two B-roll clips that
    # overlap in absolute timeline position will visually collide if both
    # are hardcoded to lane 1, even when they're attached to different main
    # clips. Assign lanes via the same greedy interval-scheduling approach
    # used for the XMEML and OTIO exports: process clips in absolute
    # start-time order, reuse a lane once its last clip has ended, otherwise
    # open a new one.
    broll_order = sorted(
        range(len(broll_segments)),
        key=lambda i: broll_segments[i].get("timeline_start_seconds") or 0.0,
    )
    lane_cursors = []  # lane_cursors[n] = absolute end time (seconds) of the last clip in lane n+1
    lane_by_index = {}
    for i in broll_order:
        seg = broll_segments[i]
        abs_start = seg.get("timeline_start_seconds", 0.0) or 0.0
        abs_end = abs_start + max(0.0, seg["out_seconds"] - seg["in_seconds"])
        lane_n = next((n for n, cursor in enumerate(lane_cursors) if cursor <= abs_start), None)
        if lane_n is None:
            lane_cursors.append(abs_end)
            lane_n = len(lane_cursors) - 1
            if lane_n > 0:
                warnings.append(
                    f"FCPXML: B-roll '{seg.get('source_name')}' overlaps another B-roll clip in time -- "
                    f"placed on an additional lane ({lane_n + 1}) rather than dropped or overlapped."
                )
        else:
            lane_cursors[lane_n] = abs_end
        lane_by_index[i] = lane_n + 1  # FCPXML lanes are 1-based

    for bi, seg in enumerate(broll_segments):
        target_start = seg.get("timeline_start_seconds", 0.0) or 0.0
        clip_len = max(0.0, seg["out_seconds"] - seg["in_seconds"])

        host_index = None
        for i, (_, abs_start, abs_end) in enumerate(main_spans):
            if abs_start <= target_start < abs_end:
                host_index = i
                break

        if host_index is None:
            if target_start < main_spans[0][1]:
                host_index = 0
                warnings.append(
                    f"FCPXML: B-roll '{seg.get('source_name')}' starts before the first main "
                    "clip -- anchored to the start of the first clip instead; check its placement."
                )
            else:
                host_index = len(main_spans) - 1
                warnings.append(
                    f"FCPXML: B-roll '{seg.get('source_name')}' starts after the last main "
                    "clip ends -- anchored to the last clip instead; check its placement."
                )

        host_seg, host_abs_start, host_abs_end = main_spans[host_index]
        relative_offset = max(0.0, target_start - host_abs_start)
        host_element = main_clip_elements[host_index]

        broll_clip = ET.SubElement(
            host_element, "asset-clip",
            ref=asset_id_by_path[seg["source_path"]],
            name=f'{seg.get("source_name", "B-Roll")} \u00b7 B-ROLL',
            lane=str(lane_by_index[bi]),
            offset=_seconds_to_fcp_time(relative_offset, fps),
            duration=_seconds_to_fcp_time(clip_len, fps),
            start=_seconds_to_fcp_time(seg["in_seconds"], fps),
            format=format_id,
        )
        audio_mode = seg.get("audio_mode", "silent")
        if audio_mode == "silent":
            # Mute the overlay's native audio so it never competes with the
            # host track's sound -- Final Cut's built-in volume adjustment,
            # dropped low enough to be effectively silent.
            ET.SubElement(broll_clip, "adjust-volume", amount="-96dB")
        # "full" and "duck_main" both leave the overlay's own audio alone;
        # "duck_main" instead attenuates the host main clip (see above).

        note = seg.get("note")
        if note:
            ET.SubElement(
                broll_clip, "marker",
                start="0s",
                duration=_seconds_to_fcp_time(one_frame_seconds, fps),
                value=note[:200],
            )

    rough = ET.tostring(fcpxml, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="  ")
    lines = [ln for ln in pretty.split("\n") if ln.strip()]
    body = "\n".join(lines)
    xml_string = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body[body.find("\n") + 1:]
    return xml_string, warnings
