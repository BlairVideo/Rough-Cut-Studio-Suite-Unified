#!/usr/bin/env python3
"""Recomputes one take's segments after a manual anchor edit (nudge, delete,
or insert) in the QA UI's waveform view, without rerunning the full DP-based
analysis pipeline -- editing an anchor only changes the piecewise mapping for
that one take, not the matching itself. Updates the report file in place so
a later import_to_resolve.py run picks up the edited segments, and prints
just the updated segment list so the UI can refresh immediately.

Usage:
    python recompute_segments.py --report report.json --take <name> --points points.json

points.json is an ordered list of {"ref_time": float, "take_time": float},
including the take's own start/end boundary points (unchanged from the
original report) plus whichever anchors the user kept/moved/added in between.
"""

import argparse
import json

from align import segments_from_path


def main():
    parser = argparse.ArgumentParser(description="Recompute one take's segments from an edited anchor list")
    parser.add_argument("--report", required=True)
    parser.add_argument("--take", required=True)
    parser.add_argument("--points", required=True)
    args = parser.parse_args()

    report = json.load(open(args.report))
    points = json.load(open(args.points))
    points = sorted(points, key=lambda p: p["ref_time"])

    segments = segments_from_path(
        points, report["merge_tolerance"], report["flag_speed_min"], report["flag_speed_max"]
    )

    report["segments"][args.take] = segments
    with open(args.report, "w") as f:
        json.dump(report, f)

    print(json.dumps(segments))


if __name__ == "__main__":
    main()
