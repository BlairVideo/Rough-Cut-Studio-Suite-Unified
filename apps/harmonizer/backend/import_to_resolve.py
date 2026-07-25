#!/usr/bin/env python3
"""Generates FCPXML from a sync report and imports it directly into DaVinci
Resolve via its scripting API, then adds the reference audio -- which can
never be represented in the FCPXML itself, since Resolve's importer never
attaches a MediaPoolItem to a pure audio-only asset (confirmed earlier this
project, in four structural variants). Replaces the old flow of writing an
.fcpxml file, importing it by hand, then running add_reference_audio.py
separately.

Usage:
    python import_to_resolve.py --report report.json \\
        --ref-media "/path/to/STE-006.wav" \\
        --take-media "/path/to/take1" "/path/to/take2" "/path/to/take3" \\
        [--project "Harmonizer Sync"]

Omit --project to import into whichever project is currently open in Resolve
instead of creating or switching to a named one.
"""

import argparse
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from xml.dom import minidom

import make_fcpxml

RESOLVE_SCRIPT_API = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
RESOLVE_SCRIPT_LIB = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"


def connect_resolve():
    sys.path.insert(0, os.path.join(RESOLVE_SCRIPT_API, "Modules"))
    os.environ.setdefault("RESOLVE_SCRIPT_API", RESOLVE_SCRIPT_API)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", RESOLVE_SCRIPT_LIB)
    import DaVinciResolveScript as dvr_script

    resolve = dvr_script.scriptapp("Resolve")
    if resolve is None:
        raise SystemExit("Could not connect to Resolve -- is it running?")
    return resolve


def write_fcpxml(report, take_media, timeline_name=None):
    take_infos = [make_fcpxml.probe_media(p) for p in take_media]
    fcpxml_el = make_fcpxml.build_fcpxml(report, take_infos, take_media, sequence_name=timeline_name)

    rough = ET.tostring(fcpxml_el, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="    ")
    pretty = "\n".join(line for line in pretty.split("\n") if line.strip())

    fd, path = tempfile.mkstemp(suffix=".fcpxml", prefix="harmonizer_")
    with os.fdopen(fd, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        f.write(pretty.split("\n", 1)[1] + "\n")
    return path


def add_reference_audio(project, timeline, ref_media):
    media_pool = project.GetMediaPool()
    imported = media_pool.ImportMedia([ref_media])
    if not imported:
        raise SystemExit(f"ImportMedia failed for reference audio: {ref_media}")
    ref_item = imported[0]

    # Audio-only media pool items report an empty "Frames" property; Resolve
    # still gives a timecode "Duration" (HH:MM:SS:FF) at the project's frame
    # rate, so parse that instead.
    fps = float(ref_item.GetClipProperty("FPS") or timeline.GetSetting("timelineFrameRate"))
    hh, mm, ss, ff = (int(x) for x in ref_item.GetClipProperty("Duration").split(":"))
    total_frames = ((hh * 3600 + mm * 60 + ss) * round(fps)) + ff

    timeline.AddTrack("audio")
    new_track_index = timeline.GetTrackCount("audio")

    ok = media_pool.AppendToTimeline([
        {
            "mediaPoolItem": ref_item,
            "startFrame": 0,
            "endFrame": total_frames - 1,
            "trackIndex": new_track_index,
            "recordFrame": 0,
        }
    ])
    if not ok:
        raise SystemExit("AppendToTimeline failed for reference audio")


def main():
    parser = argparse.ArgumentParser(description="Generate FCPXML and import it straight into Resolve")
    parser.add_argument("--report", required=True)
    parser.add_argument("--ref-media", required=True)
    parser.add_argument("--take-media", required=True, nargs="+")
    parser.add_argument("--project", required=False, default=None, help="omit to use whichever project is currently open in Resolve")
    parser.add_argument("--timeline-name", required=False, default=None, help="omit to auto-generate a timestamped name")
    args = parser.parse_args()

    report = json.load(open(args.report))
    fcpxml_path = write_fcpxml(report, args.take_media, args.timeline_name)

    resolve = connect_resolve()
    pm = resolve.GetProjectManager()

    if args.project:
        project = pm.CreateProject(args.project)
        if project is None:
            project = pm.LoadProject(args.project)
            if project is None:
                raise SystemExit(f"Could not create or load Resolve project '{args.project}'")
    else:
        project = pm.GetCurrentProject()
        if project is None:
            raise SystemExit("No project is currently open in Resolve, and no --project name was given")

    media_pool = project.GetMediaPool()
    timeline = media_pool.ImportTimelineFromFile(fcpxml_path)
    if timeline is None:
        raise SystemExit(
            "Resolve rejected the generated FCPXML (ImportTimelineFromFile returned None) -- "
            "check Resolve's own import log for details"
        )

    add_reference_audio(project, timeline, args.ref_media)

    print(json.dumps({
        "project": project.GetName(),
        "timeline": timeline.GetName(),
        "fcpxml_path": fcpxml_path,
    }))


if __name__ == "__main__":
    main()
