#!/usr/bin/env python3
"""Minimal test: ONE take's timeMap-retimed asset-clip directly on the spine,
no multicam wrapper. Used to isolate whether Resolve's problem is with
timeMap import itself, or specifically with the mc-clip/multicam construct."""

import json
import sys
from fractions import Fraction

sys.path.insert(0, ".")
from make_fcpxml import fcp_time, file_uri, probe_media, sub  # noqa: E402
import xml.etree.ElementTree as ET
from xml.dom import minidom


def main():
    report = json.load(open(sys.argv[1]))
    take_media = sys.argv[2]
    out = sys.argv[3]
    name = report["takes"][0]

    info = probe_media(take_media)
    video_fps = info["fps"]

    def tl_frac(seconds):
        return Fraction(round(seconds * float(video_fps)), 1) / video_fps

    def frame_str(frac):
        if frac.denominator == 1:
            return f"{frac.numerator}s"
        return f"{frac.numerator}/{frac.denominator}s"

    def tl_time(seconds):
        return frame_str(tl_frac(seconds))

    fcpxml = ET.Element("fcpxml", version="1.13")
    resources = sub(fcpxml, "resources")
    sub(resources, "format", id="fmt1", name="Harmonizer Test", frameDuration=tl_time(1 / float(video_fps)), width=info["width"], height=info["height"])
    asset = sub(
        resources, "asset", id="asset_take", name=name,
        start="0s", duration=tl_time(info["duration"]),
        hasVideo="1", hasAudio="1", format="fmt1", videoSources="1", audioSources="1",
        audioChannels=info["audio_channels"], audioRate=info["audio_rate"],
    )
    sub(asset, "media-rep", kind="original-media", src=file_uri(take_media))

    library = sub(fcpxml, "library")
    event = sub(library, "event", name="Harmonizer Simple Test")
    project = sub(event, "project", name="Harmonizer Simple Test")

    leadin_frac = tl_frac(report["excluded_leadin_ref_sec"][name])
    segs = report["segments"][name]
    ref_duration = segs[-1]["ref_end"]
    ref_duration_frac = tl_frac(ref_duration)
    clip_duration_frac = ref_duration_frac - leadin_frac

    sequence = sub(project, "sequence", format="fmt1", duration=frame_str(ref_duration_frac), audioLayout="stereo", audioRate="48k")
    spine = sub(sequence, "spine")
    if leadin_frac > 0:
        sub(spine, "gap", offset="0s", start="0s", duration=frame_str(leadin_frac))
    clip = sub(
        spine, "asset-clip", ref="asset_take", name=name,
        offset=frame_str(leadin_frac), start="0s", duration=frame_str(clip_duration_frac),
    )
    time_map = sub(clip, "timeMap")
    sub(time_map, "timept", time=frame_str(tl_frac(segs[0]["ref_start"]) - leadin_frac), value=fcp_time(segs[0]["take_start"]), interp="linear")
    for seg in segs:
        sub(time_map, "timept", time=frame_str(tl_frac(seg["ref_end"]) - leadin_frac), value=fcp_time(seg["take_end"]), interp="linear")

    rough = ET.tostring(fcpxml, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="    ")
    pretty = "\n".join(line for line in pretty.split("\n") if line.strip())
    with open(out, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        f.write(pretty.split("\n", 1)[1] + "\n")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
