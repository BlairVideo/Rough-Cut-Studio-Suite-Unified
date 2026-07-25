#!/usr/bin/env python3
"""Diagnostic: can Resolve link a pure audio-only asset via ImportTimelineFromFile
at all? Isolated from video takes, lanes, and timeMap entirely."""

import subprocess
import sys
import xml.etree.ElementTree as ET
from xml.dom import minidom

sys.path.insert(0, ".")
from make_fcpxml import ffprobe_json, file_uri, sub  # noqa: E402


def main():
    ref_media = sys.argv[1]
    out = sys.argv[2]
    include_format = "--with-format" in sys.argv

    info = ffprobe_json(ref_media)
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    duration = float(info["format"]["duration"])

    fcpxml = ET.Element("fcpxml", version="1.13")
    resources = sub(fcpxml, "resources")
    sub(resources, "format", id="fmt1", name="Harmonizer Audio", frameDuration="1/30s")
    asset = sub(
        resources, "asset", id="asset_ref", name="Reference",
        start="0s", duration=f"{round(duration*30)}/30s",
        hasVideo="0", hasAudio="1", format="fmt1" if include_format else None,
        videoSources="0",
        audioSources="1", audioChannels=int(audio["channels"]), audioRate=int(audio["sample_rate"]),
    )
    sub(asset, "media-rep", kind="original-media", src=file_uri(ref_media))

    library = sub(fcpxml, "library")
    event = sub(library, "event", name="Harmonizer Audio Only Test")
    project = sub(event, "project", name="Harmonizer Audio Only Test")
    sequence = sub(project, "sequence", format="fmt1", duration=f"{round(duration*30)}/30s", audioLayout="stereo", audioRate="48k")
    spine = sub(sequence, "spine")
    sub(spine, "asset-clip", ref="asset_ref", name="Reference", offset="0s", start="0s", duration=f"{round(duration*30)}/30s")

    rough = ET.tostring(fcpxml, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="    ")
    pretty = "\n".join(line for line in pretty.split("\n") if line.strip())
    with open(out, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        f.write(pretty.split("\n", 1)[1] + "\n")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
