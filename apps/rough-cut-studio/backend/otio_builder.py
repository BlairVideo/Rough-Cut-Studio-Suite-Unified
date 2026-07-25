"""
otio_builder.py

Builds an OpenTimelineIO (.otio) document -- a JSON-based interchange
format (not XML like the other two exports) maintained by the Academy
Software Foundation. It's less a "yet another NLE format" and more a
neutral hub: DaVinci Resolve, Avid (via adapters), and most pipeline
tooling built in Python can read it directly, and tools that don't
support the .otio file itself can usually get there via `otioconvert`.

Structure (see https://opentimelineio.readthedocs.io for the full spec):
  Timeline -> tracks (a Stack) -> one or more Track (Video/Audio) ->
  Clip / Gap children. Every OTIO object carries an "OTIO_SCHEMA" key
  identifying its type and schema version, e.g. "Clip.1".

This module hand-builds that JSON directly rather than depending on the
`opentimelineio` PyPI package, matching how xml_builder.py and
fcpxml_builder.py hand-build their formats -- no extra dependency for
the person running the app, and one less thing that could be missing at
runtime. The schema itself was cross-checked against the current OTIO
docs, and this module's output is validated against the real reference
`opentimelineio` library during development.

Track model note: OTIO tracks are strictly sequential -- a clip's
position is implied by the cumulative duration of everything before it
on that track, not an absolute timestamp. So placing B-roll at an
explicit point means inserting a Gap to "push" the clip to the right
spot -- the standard OTIO idiom for this. If two B-roll clips would
overlap in time, they can't both live on one track (nothing in a single
OTIO track can occupy the same span twice), so overlapping clips are
spread across additional tracks (V2, V3, ...) via a greedy scheduling
pass -- the classic "minimum meeting rooms" approach -- so nothing is
silently dropped or corrupted.

Audio: OTIO doesn't model channel-level (L/R) routing the way XMEML
does -- it operates at the clip/track level. A parallel "A1" Audio-kind
track mirrors the main video track's clips (same source, same in/out)
for tools that expect an explicit audio track; B-roll audio_mode and any
ducking amount are recorded as metadata for reference, not as an
interpreted OTIO effect, since OTIO has no standardized volume-automation
primitive this app can confidently guarantee is portable across tools.

No network access is used here; this is local JSON generation.
"""

import os
import json
from urllib.parse import quote


def _to_file_url(path: str) -> str:
    abspath = os.path.abspath(path)
    normalized = abspath.replace(os.sep, "/")
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return "file://" + quote(normalized, safe="/:")


def _rational_time(value, fps):
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": fps, "value": value}


def _time_range(start_frames, duration_frames, fps):
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "start_time": _rational_time(start_frames, fps),
        "duration": _rational_time(duration_frames, fps),
    }


def _gap(duration_frames, fps):
    return {
        "OTIO_SCHEMA": "Gap.1",
        "name": "Gap",
        "source_range": _time_range(0, duration_frames, fps),
        "effects": [],
        "markers": [],
        "metadata": {},
    }


def _clip(seg, fps, extra_metadata=None):
    in_frames = max(0, round(seg["in_seconds"] * fps))
    out_frames = max(in_frames + 1, round(seg["out_seconds"] * fps))
    duration = out_frames - in_frames
    clip = {
        "OTIO_SCHEMA": "Clip.1",
        "name": seg.get("source_name", "Clip"),
        "media_reference": {
            "OTIO_SCHEMA": "ExternalReference.1",
            "target_url": _to_file_url(seg["source_path"]),
            "available_range": None,
            "metadata": {},
        },
        "source_range": _time_range(in_frames, duration, fps),
        "effects": [],
        "markers": [],
        "metadata": {"rough_cut_studio": extra_metadata or {}},
    }
    return clip, duration


def _track(name, kind, children):
    return {
        "OTIO_SCHEMA": "Track.1",
        "name": name,
        "kind": kind,
        "children": children,
        "source_range": None,
        "markers": [],
        "effects": [],
        "metadata": {},
    }


def build_otio(
    sequence_name: str,
    fps: float,
    resolved_segments: list,
    broll_segments: list = None,
    video_width: int = 1920,
    video_height: int = 1080,
):
    """
    resolved_segments / broll_segments: same shapes used by xml_builder
    and fcpxml_builder (order/source_path/source_name/in_seconds/
    out_seconds/note, plus timeline_start_seconds and audio_mode for
    B-roll).

    Returns (otio_json_string, warnings_list).
    """
    resolved_segments = sorted(resolved_segments, key=lambda s: s["order"])
    broll_segments = broll_segments or []
    warnings = []

    if not resolved_segments:
        raise ValueError("build_otio requires at least one main cut.")

    v1_children = []
    a1_children = []
    for seg in resolved_segments:
        meta = {}
        if seg.get("note"):
            meta["editorial_note"] = seg["note"]
        if seg.get("on_screen_text"):
            meta["on_screen_text"] = seg["on_screen_text"]
        v_clip, _ = _clip(seg, fps, meta)
        v1_children.append(v_clip)
        a_clip, _ = _clip(seg, fps, meta)
        a1_children.append(a_clip)

    tracks_children = [_track("V1", "Video", v1_children)]

    if broll_segments:
        sorted_broll = sorted(broll_segments, key=lambda s: s.get("timeline_start_seconds") or 0.0)
        lanes = []

        for seg in sorted_broll:
            start_frame = max(0, round((seg.get("timeline_start_seconds") or 0.0) * fps))
            meta = {"audio_mode": seg.get("audio_mode", "silent")}
            if seg.get("duck_db") is not None and seg.get("audio_mode") == "duck_main":
                meta["duck_db"] = seg["duck_db"]
            if seg.get("note"):
                meta["editorial_note"] = seg["note"]
            clip, duration = _clip(seg, fps, meta)
            end_frame = start_frame + duration

            placed_lane = next((lane for lane in lanes if lane["cursor_frames"] <= start_frame), None)
            if placed_lane is None:
                placed_lane = {"cursor_frames": 0, "children": []}
                lanes.append(placed_lane)
                if len(lanes) > 1:
                    warnings.append(
                        f"OTIO: B-roll '{seg.get('source_name')}' overlaps another B-roll clip in time -- "
                        f"placed on an additional track (V{len(lanes) + 1}) rather than dropped or overlapped."
                    )

            gap_frames = start_frame - placed_lane["cursor_frames"]
            if gap_frames > 0:
                placed_lane["children"].append(_gap(gap_frames, fps))
            placed_lane["children"].append(clip)
            placed_lane["cursor_frames"] = end_frame

        for i, lane in enumerate(lanes):
            tracks_children.append(_track(f"V{i + 2}", "Video", lane["children"]))

    tracks_children.append(_track("A1", "Audio", a1_children))

    timeline = {
        "OTIO_SCHEMA": "Timeline.1",
        "name": sequence_name,
        "global_start_time": _rational_time(0, fps),
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "tracks",
            "children": tracks_children,
            "source_range": None,
            "markers": [],
            "effects": [],
            "metadata": {},
        },
        "metadata": {
            "rough_cut_studio": {
                "video_width": video_width,
                "video_height": video_height,
                "fps": fps,
            }
        },
    }

    return json.dumps(timeline, indent=2), warnings
