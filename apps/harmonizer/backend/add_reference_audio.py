#!/usr/bin/env python3
"""Adds the reference audio to a timeline after FCPXML import.

Resolve's FCPXML importer cannot link a pure audio-only asset (confirmed:
hasVideo="0" imports but never attaches a MediaPoolItem, in every structural
variant tried -- primary item, connected/lane item, with/without a format
reference; hasVideo="1" on a file with no video stream fails the import
outright). The reference needs no retiming and always starts at frame 0 by
construction, so this is a simple, deterministic fix-up rather than a
sync computation: import the file as ordinary media and append it to a new
audio track at frame 0.

Usage:
    python add_reference_audio.py --project "Harmonizer Sync" \\
        --timeline "Harmonizer Sync" --ref-media "/path/to/STE-006.wav"
"""

import argparse

import DaVinciResolveScript as dvr_script


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--timeline", default=None, help="defaults to the project's current timeline")
    parser.add_argument("--ref-media", required=True)
    args = parser.parse_args()

    resolve = dvr_script.scriptapp("Resolve")
    if resolve is None:
        raise SystemExit("Could not connect to Resolve (is it running?)")

    pm = resolve.GetProjectManager()
    project = pm.LoadProject(args.project)
    if project is None:
        raise SystemExit(f"Could not load project '{args.project}'")

    if args.timeline:
        timeline = None
        for i in range(1, project.GetTimelineCount() + 1):
            t = project.GetTimelineByIndex(i)
            if t.GetName() == args.timeline:
                timeline = t
                break
        if timeline is None:
            raise SystemExit(f"Could not find timeline '{args.timeline}' in project '{args.project}'")
        project.SetCurrentTimeline(timeline)
    else:
        timeline = project.GetCurrentTimeline()
        if timeline is None:
            raise SystemExit("Project has no current timeline and --timeline was not given")

    media_pool = project.GetMediaPool()
    imported = media_pool.ImportMedia([args.ref_media])
    if not imported:
        raise SystemExit(f"ImportMedia failed for {args.ref_media}")
    ref_item = imported[0]

    # Audio-only media pool items report an empty "Frames" property; Resolve
    # still gives a timecode "Duration" (HH:MM:SS:FF) at the project's frame
    # rate, so parse that instead.
    fps = float(ref_item.GetClipProperty("FPS") or timeline.GetSetting("timelineFrameRate"))
    hh, mm, ss, ff = (int(x) for x in ref_item.GetClipProperty("Duration").split(":"))
    total_frames = ((hh * 3600 + mm * 60 + ss) * round(fps)) + ff

    if not timeline.AddTrack("audio"):
        print("Warning: AddTrack('audio') returned False (track may already exist)")
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
        raise SystemExit("AppendToTimeline failed for the reference clip")

    print(f"Added reference audio to track A{new_track_index} at frame 0 of timeline '{timeline.GetName()}'")


if __name__ == "__main__":
    main()
