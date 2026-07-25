#!/usr/bin/env python3
"""Bisection test: multicam wrapper with 2 angles, each a single straight-cut
asset-clip spanning the whole duration -- no gap, no timeMap. Isolates
whether the mc-clip/multicam construct itself works in this Resolve version,
independent of the retiming/lead-in logic already validated separately."""

import sys
from fractions import Fraction

sys.path.insert(0, ".")
from make_fcpxml import file_uri, probe_media, sub  # noqa: E402
import xml.etree.ElementTree as ET
from xml.dom import minidom


def main():
    take_media = sys.argv[1]
    out = sys.argv[2]

    info = probe_media(take_media)
    video_fps = info["fps"]

    def frame_str(frac):
        if frac.denominator == 1:
            return f"{frac.numerator}s"
        return f"{frac.numerator}/{frac.denominator}s"

    def tl_time(seconds):
        frame_count = round(seconds * float(video_fps))
        return frame_str(Fraction(frame_count, 1) / video_fps)

    duration = min(info["duration"], 10.0)

    fcpxml = ET.Element("fcpxml", version="1.13")
    resources = sub(fcpxml, "resources")
    sub(resources, "format", id="fmt1", name="Harmonizer Test", frameDuration=tl_time(1 / float(video_fps)), width=info["width"], height=info["height"])
    asset = sub(
        resources, "asset", id="asset_take", name="Take1",
        start="0s", duration=tl_time(info["duration"]),
        hasVideo="1", hasAudio="1", format="fmt1", videoSources="1", audioSources="1",
        audioChannels=info["audio_channels"], audioRate=info["audio_rate"],
    )
    sub(asset, "media-rep", kind="original-media", src=file_uri(take_media))

    media = sub(resources, "media", id="mc1", name="MC Test")
    multicam = sub(media, "multicam", format="fmt1")

    angle1 = sub(multicam, "mc-angle", name="Angle 1", angleID="a1")
    sync1 = sub(angle1, "sync-clip", name="Take1", offset="0s", duration=tl_time(duration))
    sub(sync1, "asset-clip", ref="asset_take", name="Take1", offset="0s", start="0s", duration=tl_time(duration))

    angle2 = sub(multicam, "mc-angle", name="Angle 2", angleID="a2")
    sync2 = sub(angle2, "sync-clip", name="Take1", offset="0s", duration=tl_time(duration))
    sub(sync2, "asset-clip", ref="asset_take", name="Take1", offset="0s", start="0s", duration=tl_time(duration))

    library = sub(fcpxml, "library")
    event = sub(library, "event", name="Harmonizer MC Minimal Test")
    project = sub(event, "project", name="Harmonizer MC Minimal Test")
    sequence = sub(project, "sequence", format="fmt1", duration=tl_time(duration), audioLayout="stereo", audioRate="48k")
    spine = sub(sequence, "spine")
    mc_clip = sub(spine, "mc-clip", ref="mc1", name="MC Test", offset="0s", duration=tl_time(duration))
    sub(mc_clip, "mc-source", angleID="a1", srcEnable="all")

    rough = ET.tostring(fcpxml, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="    ")
    pretty = "\n".join(line for line in pretty.split("\n") if line.strip())
    with open(out, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        f.write(pretty.split("\n", 1)[1] + "\n")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
