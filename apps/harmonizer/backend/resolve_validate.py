#!/usr/bin/env python3
"""Connects to a running DaVinci Resolve via its scripting API, imports the
generated FCPXML as a new project's timeline, and reports back what Resolve
actually did with it (track/clip counts, whether it registered as multicam,
whether the retimed clip durations match what we asked for) -- this is the
round-trip validation the plan calls the riskiest unknown in Phase 2."""

import sys

import DaVinciResolveScript as dvr_script


def main():
    fcpxml_path = sys.argv[1]
    project_name = sys.argv[2] if len(sys.argv) > 2 else "Harmonizer FCPXML Test"

    resolve = dvr_script.scriptapp("Resolve")
    if resolve is None:
        print("ERROR: could not connect to Resolve (is it running?)")
        sys.exit(1)

    pm = resolve.GetProjectManager()
    project = pm.CreateProject(project_name)
    if project is None:
        # already exists from a previous run; load it instead
        project = pm.LoadProject(project_name)
        if project is None:
            print(f"ERROR: could not create or load project '{project_name}'")
            sys.exit(1)

    media_pool = project.GetMediaPool()
    timeline = media_pool.ImportTimelineFromFile(fcpxml_path)
    if timeline is None:
        print("IMPORT FAILED: ImportTimelineFromFile returned None")
        sys.exit(1)

    print(f"IMPORT OK: timeline '{timeline.GetName()}' created")
    print(f"  duration (frames): {timeline.GetEndFrame() - timeline.GetStartFrame() + 1}")
    print(f"  video track count: {timeline.GetTrackCount('video')}")
    print(f"  audio track count: {timeline.GetTrackCount('audio')}")

    for track_type in ("video", "audio"):
        n_tracks = timeline.GetTrackCount(track_type)
        for t in range(1, n_tracks + 1):
            items = timeline.GetItemListInTrack(track_type, t)
            print(f"  {track_type} track {t}: {len(items) if items else 0} item(s)")
            for item in items or []:
                name = item.GetName()
                is_mc = False
                try:
                    is_mc = bool(item.GetMediaPoolItem() and item.GetMediaPoolItem().GetClipProperty("Type") == "Multicam")
                except Exception:
                    pass
                print(
                    f"    - {name}: start={item.GetStart()} end={item.GetEnd()} "
                    f"duration={item.GetDuration()} multicam={is_mc}"
                )


if __name__ == "__main__":
    main()
