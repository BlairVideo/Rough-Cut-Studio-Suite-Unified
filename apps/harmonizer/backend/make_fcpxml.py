#!/usr/bin/env python3
"""Harmonizer Phase 2 prototype: align.py's JSON report -> FCPXML project.

Places each take on its own lane in one sequence (each retimed via a
per-segment timeMap, per the report's speed factors), plus the reference
audio underneath, so Resolve opens it as stacked tracks ready to be grouped
into a multicam clip inside Resolve itself. Original media is referenced by
path, never transcoded or copied -- Resolve's own conform engine applies the
retiming.

Usage:
    python make_fcpxml.py --report real_test/report.json \\
        --ref-media "/path/to/STE-006.wav" \\
        --take-media "/path/to/A001_..._C024.mp4" "/path/to/A001_..._C025.mp4" "/path/to/A001_..._C026.mp4" \\
        --out sync.fcpxml
"""

import argparse
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote
from xml.dom import minidom

TIME_DENOM = 30000

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BRAW_EXTRACT_CLIP_INFO = os.path.join(_SCRIPT_DIR, "braw_sdk", "Samples", "ExtractClipInfo", "ExtractClipInfo")


def ffprobe_json(path):
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def probe_braw(path):
    """ffprobe has no BRAW demuxer, so structural properties come from
    Blackmagic's own SDK instead (see braw_sdk/Samples/ExtractClipInfo)."""
    if not os.path.exists(_BRAW_EXTRACT_CLIP_INFO):
        raise FileNotFoundError(f"Blackmagic RAW SDK tool not found at {_BRAW_EXTRACT_CLIP_INFO}")
    result = subprocess.run(
        [_BRAW_EXTRACT_CLIP_INFO, os.path.abspath(path)],
        cwd=os.path.dirname(_BRAW_EXTRACT_CLIP_INFO),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ExtractClipInfo failed for {path}: {result.stderr.strip()}")
    info = json.loads(result.stdout)
    fps = Fraction(info["frame_rate"]).limit_denominator(1000)
    return {
        "duration": info["frame_count"] / float(info["frame_rate"]),
        "has_video": True,
        "has_audio": info["has_audio"],
        "width": info["width"],
        "height": info["height"],
        "fps": fps,
        "audio_rate": info["audio_rate"] if info["has_audio"] else None,
        "audio_channels": info["audio_channels"] if info["has_audio"] else None,
        "tc_start": info["tc_start"],
    }


def probe_media(path):
    if os.path.splitext(path)[1].lower() == ".braw":
        return probe_braw(path)

    info = ffprobe_json(path)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    duration = float(info["format"]["duration"])
    fps = None
    if video:
        num, den = video["r_frame_rate"].split("/")
        fps = Fraction(int(num), int(den))
    # Real (non-proxy) camera originals often carry embedded timecode the
    # same way BRAW does; grab it if ffprobe surfaces it so the same
    # tcStart-mismatch failure mode doesn't resurface for other formats.
    tc_start = (info["format"].get("tags") or {}).get("timecode") or (video or {}).get("tags", {}).get("timecode")

    return {
        "duration": duration,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "width": int(video["width"]) if video else None,
        "height": int(video["height"]) if video else None,
        "fps": fps,
        "audio_rate": int(audio["sample_rate"]) if audio else None,
        "audio_channels": int(audio["channels"]) if audio else None,
        "tc_start": tc_start,
    }


def fcp_time(seconds, denom=TIME_DENOM):
    """For source-domain values (timeMap's `value`, source durations) --
    doesn't need to land on a frame boundary."""
    frac = Fraction(seconds).limit_denominator(denom * 100)
    frac = Fraction(round(frac * denom), denom)
    if frac.denominator == 1:
        return f"{frac.numerator}s"
    return f"{frac.numerator}/{frac.denominator}s"


def fcp_frame_time(seconds, fps):
    """For timeline-domain values (clip/gap offset & duration, timeMap's
    `time`, sequence duration) -- these are edit points and MUST land on an
    exact frame boundary at the project's edit rate, or Resolve's FCPXML
    parser silently corrupts the position (observed as garbage int32-min
    starts in its import log) instead of rejecting the file outright."""
    frame_count = round(seconds * float(fps))
    frac = Fraction(frame_count, 1) / fps
    if frac.denominator == 1:
        return f"{frac.numerator}s"
    return f"{frac.numerator}/{frac.denominator}s"


def sub(parent, tag, **attrs):
    return ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items() if v is not None})


def file_uri(path):
    return "file://" + quote(str(Path(path).resolve()))


def build_fcpxml(report, take_infos, take_media_paths, sequence_name=None):
    # Resolve refuses to import a second timeline with a name that already
    # exists in the target project (confirmed: ImportTimelineFromFile returns
    # None with no diagnostic when re-importing into the same project), so a
    # fixed name breaks the common case of running this more than once
    # against your own currently-open project. Default to a timestamped name.
    if sequence_name is None:
        sequence_name = f"Harmonizer Sync {datetime.now():%Y-%m-%d %H%M%S}"

    take_names = report["takes"]
    ref_duration = max(
        report["segments"][name][-1]["ref_end"] for name in take_names
    )

    fcpxml = ET.Element("fcpxml", version="1.13")
    resources = sub(fcpxml, "resources")

    video_fps = take_infos[0]["fps"]

    def tl_frac(seconds):
        """Snap a timeline-domain second value to an exact frame boundary at
        the project's edit rate, as an exact Fraction (in seconds)."""
        frame_count = round(seconds * float(video_fps))
        return Fraction(frame_count, 1) / video_fps

    def tl_time(seconds):
        return frame_str(tl_frac(seconds))

    def frame_str(frac):
        if frac.denominator == 1:
            return f"{frac.numerator}s"
        return f"{frac.numerator}/{frac.denominator}s"

    frame_duration = tl_time(1 / float(video_fps))
    fmt = sub(
        resources, "format", id="fmt1", name=f"Harmonizer {take_infos[0]['width']}x{take_infos[0]['height']}",
        frameDuration=frame_duration, width=take_infos[0]["width"], height=take_infos[0]["height"],
    )

    take_asset_ids = {}
    for name, info, path in zip(take_names, take_infos, take_media_paths):
        asset_id = f"asset_{name}"
        take_asset_ids[name] = asset_id
        asset = sub(
            resources, "asset", id=asset_id, name=name,
            start="0s", duration=tl_time(info["duration"]),
            hasVideo="1", hasAudio="1" if info["has_audio"] else "0",
            format="fmt1", videoSources="1",
            audioSources="1" if info["has_audio"] else None,
            audioChannels=info["audio_channels"], audioRate=info["audio_rate"],
        )
        sub(asset, "media-rep", kind="original-media", src=file_uri(path))

    ref_duration_frac = tl_frac(ref_duration)

    library = sub(fcpxml, "library")
    event = sub(library, "event", name=sequence_name)
    project = sub(event, "project", name=sequence_name)
    sequence = sub(
        project, "sequence", format="fmt1", duration=frame_str(ref_duration_frac),
        audioLayout="stereo", audioRate="48k",
    )
    spine = sub(sequence, "spine")

    # Hand-authored FCPXML multicam (<media><multicam>/<mc-clip>) crashed
    # Resolve's importer outright, even in a minimal 2-angle form with no
    # retiming at all -- confirmed across 3 variants. Falling back to plain
    # stacked tracks instead: each take is nested as a connected clip (on its
    # own lane) inside a plain <gap> spanning the full reference duration.
    # Connected-clip `offset` is relative to the anchor's own local zero, NOT
    # absolute sequence position -- confirmed by a diagnostic where a flat
    # sibling with `lane` landed at an unpredictable, per-asset-dependent
    # offset, while a properly nested connected clip landed exactly on the
    # requested frame. A gap's local zero is the sequence's absolute zero
    # (same as the reference's own start), so each take's lead-in value
    # already equals its correct nested offset with no conversion.
    #
    # The reference audio itself is NOT in this file: DaVinci Resolve
    # 21.0.1's FCPXML importer cannot link a pure audio-only asset -- it
    # imports but never attaches a MediaPoolItem, confirmed in 4 structural
    # variants (primary item, connected item, with/without a format
    # reference, explicit videoSources="0"), and declaring hasVideo="1" on a
    # file with no video stream fails the import outright. Since the
    # reference needs no retiming and always starts at frame 0, it's added
    # separately after import via add_reference_audio.py, which uses
    # Resolve's scripting API for this one simple, deterministic placement.
    #
    # Group the takes into an actual multicam clip inside Resolve itself
    # afterward (New Multicam Clip Using Selected Clips), a well-supported
    # native Resolve operation.
    anchor = sub(spine, "gap", offset="0s", start="0s", duration=frame_str(ref_duration_frac))
    take_info_by_name = dict(zip(take_names, take_infos))

    for idx, name in enumerate(take_names, start=1):
        leadin_frac = tl_frac(report["excluded_leadin_ref_sec"][name])
        clip_duration_frac = ref_duration_frac - leadin_frac
        tc_start = take_info_by_name[name].get("tc_start")
        segs = report["segments"][name]
        # Resolve doesn't use timeMap's own first value as the clip's source
        # in-point -- confirmed on real data where a take with a genuine
        # mid-file trim (39.55s lead-in) still had Resolve reading source
        # frame 0 as the start (GetSourceStartTime()==0.0), silently showing
        # ~40s of unwanted pre-roll footage instead of skipping it. Resolve
        # does correctly derive the overall span length from the timeMap, so
        # setting `start` to the real source in-point lines the two up.
        clip = sub(
            anchor, "asset-clip", ref=take_asset_ids[name], name=name, lane=idx,
            offset=frame_str(leadin_frac), start=fcp_time(segs[0]["take_start"]),
            duration=frame_str(clip_duration_frac),
            tcStart=tc_start, tcFormat="NDF" if tc_start else None,
        )
        time_map = sub(clip, "timeMap")
        sub(
            time_map, "timept",
            time=frame_str(tl_frac(segs[0]["ref_start"]) - leadin_frac), value=fcp_time(segs[0]["take_start"]),
            interp="linear",
        )
        for seg in segs:
            sub(
                time_map, "timept",
                time=frame_str(tl_frac(seg["ref_end"]) - leadin_frac), value=fcp_time(seg["take_end"]),
                interp="linear",
            )

    return fcpxml


def main():
    parser = argparse.ArgumentParser(description="Phase 2 prototype: JSON sync report -> FCPXML")
    parser.add_argument("--report", required=True)
    parser.add_argument("--ref-media", required=True, help="path to the ORIGINAL reference audio file")
    parser.add_argument("--take-media", required=True, nargs="+", help="paths to the ORIGINAL take video files, same order as --takes used in align.py")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    report = json.load(open(args.report))
    take_infos = [probe_media(p) for p in args.take_media]

    fcpxml_el = build_fcpxml(report, take_infos, args.take_media)

    rough = ET.tostring(fcpxml_el, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="    ")
    pretty = "\n".join(line for line in pretty.split("\n") if line.strip())

    with open(args.out, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        f.write(pretty.split("\n", 1)[1] + "\n")

    print(f"Wrote {args.out}")
    print(
        "Reference audio isn't in this file (Resolve can't link audio-only "
        "FCPXML assets). After importing, run:\n"
        f"  python add_reference_audio.py --project <resolve-project-name> "
        f"--ref-media \"{args.ref_media}\""
    )


if __name__ == "__main__":
    main()
