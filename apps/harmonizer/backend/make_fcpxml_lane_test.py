#!/usr/bin/env python3
"""Diagnostic: does a connected clip's `offset` in a flat spine sibling (with
`lane` set) mean absolute sequence position, or something relative to the
primary storyline item it's nearest to in document order? One primary clip
(lane unset) + one lane=1 clip with a distinctive, easy-to-recognize 5s
offset, no gaps/timeMaps, to read the answer cleanly off Resolve's import."""

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

    library = sub(fcpxml, "library")
    event = sub(library, "event", name="Harmonizer Lane Test")
    project = sub(event, "project", name="Harmonizer Lane Test")
    sequence = sub(project, "sequence", format="fmt1", duration="20s", audioLayout="stereo", audioRate="48k")
    spine = sub(sequence, "spine")
    primary = sub(spine, "asset-clip", ref="asset_take", name="Primary", offset="0s", start="0s", duration="20s")
    sub(primary, "asset-clip", ref="asset_take", name="Lane1_offset5s", lane="1", offset="5s", start="0s", duration="3s")

    rough = ET.tostring(fcpxml, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="    ")
    pretty = "\n".join(line for line in pretty.split("\n") if line.strip())
    with open(out, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        f.write(pretty.split("\n", 1)[1] + "\n")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
