"""
xml_builder.py

Builds a Final Cut Pro XML Interchange Format (XMEML v5) file from a resolved
list of edit decisions. Premiere Pro imports this format directly
(File > Import) and populates a new sequence with the cuts already placed
on the timeline in order, referencing the original source media by file path.

No network access is required or used here; this is local XML generation.

Frame-rate note: timecodes are treated as non-drop-frame. If your source
footage uses drop-frame timecode (common at 29.97/59.94 fps on some cameras),
re-check the sequence settings after import.

Audio is always written as true stereo: two linked mono tracks per clip
group, each pulling one channel out of the source file via <sourcetrack>.
This is how Final Cut/Premiere's own XML export represents a stereo clip --
a single audio clipitem with channelcount=2 and no channel routing is not
enough; Premiere will import that as mono. See STEREO_CHANNELS below;
this isn't a tunable parameter.

Optional B-roll clips sit on a second video track (V2) at an explicit
timeline position -- or, if two B-roll clips overlap in time, on further
tracks (V3, V4, ...) via greedy lane assignment, since a single XMEML
track can't hold two overlapping clipitems. Each clip has an "audio_mode":
  - "silent" (default): no audio at all for the overlay -- the classic
    picture-only B-roll pattern.
  - "full": the overlay's own audio plays too, on its own stereo track
    pair (A3/A4), left untouched.
  - "duck_main": same as "full", but every MAIN clip the overlay's time
    range touches has its audio level reduced (a flat reduction for the
    clip's entire duration, not a frame-precise fade in/out around just
    the overlap -- see build_premiere_xml's docstring for why).
"""

import os
import uuid
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom

STEREO_CHANNELS = 2


def _uid():
    return uuid.uuid4().hex[:16].upper()


def _rate_elem(parent, fps: float):
    rate = ET.SubElement(parent, "rate")
    timebase = ET.SubElement(rate, "timebase")
    ntsc = fps not in (24, 25, 30, 50, 60)
    timebase.text = str(round(fps))
    ntsc_el = ET.SubElement(rate, "ntsc")
    ntsc_el.text = "TRUE" if ntsc else "FALSE"


def _db_to_amplitude(db: float) -> float:
    return round(10 ** (db / 20.0), 4)


def _add_audio_levels_filter(clipitem_el, db: float):
    filt = ET.SubElement(clipitem_el, "filter")
    effect = ET.SubElement(filt, "effect")
    ET.SubElement(effect, "name").text = "Audio Levels"
    ET.SubElement(effect, "effectid").text = "audiolevels"
    ET.SubElement(effect, "effectcategory").text = "audiolevels"
    ET.SubElement(effect, "effecttype").text = "audiolevels"
    ET.SubElement(effect, "mediatype").text = "audio"
    parameter = ET.SubElement(effect, "parameter")
    ET.SubElement(parameter, "parameterid").text = "level"
    ET.SubElement(parameter, "name").text = "Level"
    ET.SubElement(parameter, "value").text = str(_db_to_amplitude(db))


def build_premiere_xml(
    sequence_name: str,
    fps: float,
    resolved_segments: list,
    broll_segments: list = None,
    main_duck_db: dict = None,
    video_width: int = 1920,
    video_height: int = 1080,
    audio_sample_rate: int = 48000,
    audio_depth: int = 16,
):
    """Returns (xml_string, warnings_list)."""
    resolved_segments = sorted(resolved_segments, key=lambda s: s["order"])
    broll_segments = broll_segments or []
    main_duck_db = main_duck_db or {}
    warnings = []

    xmeml = ET.Element("xmeml", version="5")
    sequence = ET.SubElement(xmeml, "sequence", id=f"sequence-{_uid()}")
    ET.SubElement(sequence, "name").text = sequence_name
    ET.SubElement(sequence, "duration").text = "0"
    _rate_elem(sequence, fps)

    media = ET.SubElement(sequence, "media")

    video = ET.SubElement(media, "video")
    video_format = ET.SubElement(video, "format")
    vsc = ET.SubElement(video_format, "samplecharacteristics")
    _rate_elem(vsc, fps)
    ET.SubElement(vsc, "width").text = str(video_width)
    ET.SubElement(vsc, "height").text = str(video_height)
    video_track = ET.SubElement(video, "track")

    audio = ET.SubElement(media, "audio")
    ET.SubElement(audio, "numOutputChannels").text = str(STEREO_CHANNELS)
    audio_format = ET.SubElement(audio, "format")
    asc = ET.SubElement(audio_format, "samplecharacteristics")
    ET.SubElement(asc, "depth").text = str(audio_depth)
    ET.SubElement(asc, "samplerate").text = str(audio_sample_rate)
    ET.SubElement(audio_format, "channelcount").text = str(STEREO_CHANNELS)
    audio_track_left = ET.SubElement(audio, "track")
    audio_track_right = ET.SubElement(audio, "track")

    timeline_pos = 0
    file_id_cache = {}

    for i, seg in enumerate(resolved_segments):
        in_frames = max(0, round(seg["in_seconds"] * fps))
        out_frames = max(in_frames + 1, round(seg["out_seconds"] * fps))
        clip_len = out_frames - in_frames
        clip_name = seg.get("source_name", "Clip")
        source_path = seg["source_path"]
        clip_index = i + 1

        is_new_file = source_path not in file_id_cache
        if is_new_file:
            file_id_cache[source_path] = f"file-{_uid()}"
        file_id = file_id_cache[source_path]

        video_id = f"clipitem-V{clip_index}-{_uid()}"
        audio_l_id = f"clipitem-AL{clip_index}-{_uid()}"
        audio_r_id = f"clipitem-AR{clip_index}-{_uid()}"
        link_ids = (video_id, audio_l_id, audio_r_id)
        link_tracks = (1, 1, 2)

        v_clip = ET.SubElement(video_track, "clipitem", id=video_id)
        ET.SubElement(v_clip, "name").text = clip_name
        ET.SubElement(v_clip, "duration").text = str(clip_len)
        _rate_elem(v_clip, fps)
        ET.SubElement(v_clip, "start").text = str(timeline_pos)
        ET.SubElement(v_clip, "end").text = str(timeline_pos + clip_len)
        ET.SubElement(v_clip, "in").text = str(in_frames)
        ET.SubElement(v_clip, "out").text = str(out_frames)

        if is_new_file:
            _build_file_element(v_clip, file_id, clip_name, source_path, fps,
                                 video_width, video_height,
                                 audio_sample_rate, audio_depth)
        else:
            ET.SubElement(v_clip, "file", id=file_id)

        note = seg.get("note")
        if note:
            marker = ET.SubElement(v_clip, "marker")
            ET.SubElement(marker, "name").text = note[:80]
            ET.SubElement(marker, "comment").text = note
            ET.SubElement(marker, "in").text = "0"
            ET.SubElement(marker, "out").text = "-1"

        _add_stereo_links(v_clip, link_ids, link_tracks, clip_index)

        duck_db = main_duck_db.get(seg["order"])

        al_clip = _add_audio_channel_clip(
            audio_track_left, audio_l_id, file_id, clip_name, fps,
            timeline_pos, clip_len, in_frames, out_frames,
            source_channel=1, link_ids=link_ids, link_tracks=link_tracks, clip_index=clip_index,
        )
        ar_clip = _add_audio_channel_clip(
            audio_track_right, audio_r_id, file_id, clip_name, fps,
            timeline_pos, clip_len, in_frames, out_frames,
            source_channel=2, link_ids=link_ids, link_tracks=link_tracks, clip_index=clip_index,
        )
        if duck_db is not None:
            _add_audio_levels_filter(al_clip, duck_db)
            _add_audio_levels_filter(ar_clip, duck_db)

        timeline_pos += clip_len

    total_frames = timeline_pos

    if broll_segments:
        # Overlapping B-roll clips can't share a single XMEML track --
        # Premiere/Final Cut expect clipitems on one track to never overlap
        # in time. Assign each clip a "lane" with the same greedy interval-
        # scheduling approach otio_builder.py uses (process in start-time
        # order, reuse a lane once it's free, otherwise open a new one), so
        # overlapping B-roll spreads across additional video tracks
        # (V2, V3, ...) instead of colliding. Audio-bearing B-roll gets the
        # same treatment: each lane gets its own stereo track pair,
        # allocated lazily so silent B-roll never grows the audio section.
        # `video_track` (main) is always sequence track 1; broll video
        # lanes are 2, 3, ... in the order their tracks are created here.
        # `audio_track_left`/`audio_track_right` (main) are tracks 1/2;
        # broll audio lanes take the next free indices from there.
        next_broll_video_index = 2
        next_broll_audio_index = 3
        lanes = []  # each: {"cursor": int, "video_track": Element, "video_index": int,
                    #        "audio_left": Element|None, "audio_right": Element|None, "audio_index": int|None}

        order_by_start = sorted(
            range(len(broll_segments)),
            key=lambda bi: broll_segments[bi].get("timeline_start_seconds") or 0.0,
        )

        for bi in order_by_start:
            seg = broll_segments[bi]
            in_frames = max(0, round(seg["in_seconds"] * fps))
            out_frames = max(in_frames + 1, round(seg["out_seconds"] * fps))
            clip_len = out_frames - in_frames
            start_frame = max(0, round(seg.get("timeline_start_seconds", 0) * fps))
            end_frame = start_frame + clip_len
            total_frames = max(total_frames, end_frame)

            lane = next((ln for ln in lanes if ln["cursor"] <= start_frame), None)
            if lane is None:
                lane = {
                    "cursor": 0,
                    "video_track": ET.SubElement(video, "track"),
                    "video_index": next_broll_video_index,
                    "audio_left": None,
                    "audio_right": None,
                    "audio_index": None,
                }
                if lanes:
                    warnings.append(
                        f"Premiere: B-roll '{seg.get('source_name')}' overlaps another B-roll clip in time -- "
                        f"placed on an additional track (V{next_broll_video_index}) rather than dropped or overlapped."
                    )
                next_broll_video_index += 1
                lanes.append(lane)
            lane["cursor"] = end_frame
            broll_track = lane["video_track"]
            broll_video_track_index = lane["video_index"]

            clip_name = f'{seg.get("source_name", "B-Roll")} \u00b7 B-ROLL'
            source_path = seg["source_path"]
            is_new_file = source_path not in file_id_cache
            if is_new_file:
                file_id_cache[source_path] = f"file-{_uid()}"
            file_id = file_id_cache[source_path]

            audio_mode = seg.get("audio_mode", "silent")
            broll_clip_index = 1000 + bi
            video_id = f"clipitem-BR{bi}-{_uid()}"

            clip = ET.SubElement(broll_track, "clipitem", id=video_id)
            ET.SubElement(clip, "name").text = clip_name
            ET.SubElement(clip, "duration").text = str(clip_len)
            _rate_elem(clip, fps)
            ET.SubElement(clip, "start").text = str(start_frame)
            ET.SubElement(clip, "end").text = str(end_frame)
            ET.SubElement(clip, "in").text = str(in_frames)
            ET.SubElement(clip, "out").text = str(out_frames)

            if is_new_file:
                _build_file_element(clip, file_id, seg.get("source_name", "B-Roll"), source_path, fps,
                                     video_width, video_height,
                                     audio_sample_rate, audio_depth)
            else:
                ET.SubElement(clip, "file", id=file_id)

            note = seg.get("note")
            if note:
                marker = ET.SubElement(clip, "marker")
                ET.SubElement(marker, "name").text = note[:80]
                ET.SubElement(marker, "comment").text = note
                ET.SubElement(marker, "in").text = "0"
                ET.SubElement(marker, "out").text = "-1"

            if audio_mode == "silent":
                continue

            if lane["audio_left"] is None:
                lane["audio_left"] = ET.SubElement(audio, "track")
                lane["audio_right"] = ET.SubElement(audio, "track")
                lane["audio_index"] = next_broll_audio_index
                next_broll_audio_index += 2

            audio_l_id = f"clipitem-BRAL{bi}-{_uid()}"
            audio_r_id = f"clipitem-BRAR{bi}-{_uid()}"
            link_ids = (video_id, audio_l_id, audio_r_id)
            link_tracks = (broll_video_track_index, lane["audio_index"], lane["audio_index"] + 1)
            _add_stereo_links(clip, link_ids, link_tracks, broll_clip_index)

            _add_audio_channel_clip(
                lane["audio_left"], audio_l_id, file_id, clip_name, fps,
                start_frame, clip_len, in_frames, out_frames,
                source_channel=1, link_ids=link_ids, link_tracks=link_tracks, clip_index=broll_clip_index,
            )
            _add_audio_channel_clip(
                lane["audio_right"], audio_r_id, file_id, clip_name, fps,
                start_frame, clip_len, in_frames, out_frames,
                source_channel=2, link_ids=link_ids, link_tracks=link_tracks, clip_index=broll_clip_index,
            )

    sequence.find("duration").text = str(total_frames)

    rough = ET.tostring(xmeml, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="  ")
    lines = [ln for ln in pretty.split("\n") if ln.strip()]
    body = "\n".join(lines)
    xml_string = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body[body.find("\n") + 1:]
    return xml_string, warnings


def _add_audio_channel_clip(track_el, clip_id, file_id, clip_name, fps,
                             timeline_pos, clip_len, in_frames, out_frames,
                             source_channel, link_ids, link_tracks, clip_index):
    clip = ET.SubElement(track_el, "clipitem", id=clip_id)
    ET.SubElement(clip, "name").text = clip_name
    ET.SubElement(clip, "duration").text = str(clip_len)
    _rate_elem(clip, fps)
    ET.SubElement(clip, "start").text = str(timeline_pos)
    ET.SubElement(clip, "end").text = str(timeline_pos + clip_len)
    ET.SubElement(clip, "in").text = str(in_frames)
    ET.SubElement(clip, "out").text = str(out_frames)
    ET.SubElement(clip, "file", id=file_id)
    sourcetrack = ET.SubElement(clip, "sourcetrack")
    ET.SubElement(sourcetrack, "mediatype").text = "audio"
    ET.SubElement(sourcetrack, "trackindex").text = str(source_channel)
    _add_stereo_links(clip, link_ids, link_tracks, clip_index)
    return clip


def _build_file_element(parent, file_id, clip_name, source_path, fps,
                         video_width, video_height,
                         audio_sample_rate, audio_depth):
    file_el = ET.SubElement(parent, "file", id=file_id)
    ET.SubElement(file_el, "name").text = clip_name
    ET.SubElement(file_el, "pathurl").text = _to_pathurl(source_path)
    _rate_elem(file_el, fps)
    fmedia = ET.SubElement(file_el, "media")

    fvideo = ET.SubElement(fmedia, "video")
    fvchar = ET.SubElement(fvideo, "samplecharacteristics")
    ET.SubElement(fvchar, "width").text = str(video_width)
    ET.SubElement(fvchar, "height").text = str(video_height)

    faudio = ET.SubElement(fmedia, "audio")
    fachar = ET.SubElement(faudio, "samplecharacteristics")
    ET.SubElement(fachar, "depth").text = str(audio_depth)
    ET.SubElement(fachar, "samplerate").text = str(audio_sample_rate)
    ET.SubElement(faudio, "channelcount").text = str(STEREO_CHANNELS)


def _add_stereo_links(clipitem_el, link_ids, link_tracks, clip_index):
    video_id, audio_l_id, audio_r_id = link_ids
    video_track, audio_l_track, audio_r_track = link_tracks
    for mediatype, linkref, track_index in (
        ("video", video_id, video_track),
        ("audio", audio_l_id, audio_l_track),
        ("audio", audio_r_id, audio_r_track),
    ):
        link = ET.SubElement(clipitem_el, "link")
        ET.SubElement(link, "linkclipref").text = linkref
        ET.SubElement(link, "mediatype").text = mediatype
        ET.SubElement(link, "trackindex").text = str(track_index)
        ET.SubElement(link, "clipindex").text = str(clip_index)


def _to_pathurl(path: str) -> str:
    abspath = os.path.abspath(path)
    normalized = abspath.replace(os.sep, "/")
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return "file://localhost" + normalized
