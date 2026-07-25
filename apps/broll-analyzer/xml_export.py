"""
xml_export.py
Builds a Final Cut Pro XML (xmeml v5) file from analyzed clips.

This format is natively importable by Adobe Premiere Pro (File > Import...
or drag the .xml into a project). The output contains:
  - A bin ("B-Roll Analysis") with every analyzed clip as a master clip
    (encoded as <clip> elements, which is what Premiere expects for
    Project-panel/bin items as opposed to timeline items), named with
    its quality score for easy browsing.
  - A sequence ("Best B-Roll Selects") that places the best-scoring
    segment of each selected clip back-to-back on the timeline, in rank
    order, so an editor can drop it straight into a project as a
    ready-made selects reel. Each video segment is paired with a
    matching audio item per source channel at the same in/out points --
    a mono clip gets one linked audio item, a stereo clip gets two
    (L/R, each pinned to its source channel via <sourcetrack>), a 5.1
    clip gets six, and so on, each on its own sequence track. This is
    deliberate even for stereo: a single clipitem with
    <channelcount>2</channelcount> silently imports as mono in Premiere,
    so every channel is always split into its own linked item instead.
    The sequence's audio format explicitly declares its output channel
    width (sized to the widest source channel count in the batch)
    rather than leaving it unstated. Original audio stays attached, in
    sync, and in its native format rather than being stripped out. Each
    clip's named channel preset (Mono/Stereo/5.1/etc.) is also carried
    through into its bin comments for easy reference.
"""

import os
from xml.sax.saxutils import escape
from typing import List, Optional
from analyzer import ClipResult, Segment


def _tc_string(seconds: float, fps: float) -> str:
    """Seconds -> HH:MM:SS:FF timecode string (non-drop-frame)."""
    if fps <= 0:
        fps = 25.0
    fps_int = int(round(fps))
    total_frames = int(round(seconds * fps))
    frames = total_frames % fps_int
    total_seconds = total_frames // fps_int
    s = total_seconds % 60
    m = (total_seconds // 60) % 60
    h = total_seconds // 3600
    return f"{h:02d}:{m:02d}:{s:02d}:{frames:02d}"


def _frames(seconds: float, fps: float) -> int:
    return int(round(seconds * fps))


def _rate_block(fps: float, indent: str = "") -> str:
    fps_int = int(round(fps))
    ntsc = "TRUE" if fps_int in (24, 30, 60) and abs(fps - fps_int) > 0.01 else "FALSE"
    return (f"{indent}<rate>\n"
            f"{indent}  <timebase>{fps_int}</timebase>\n"
            f"{indent}  <ntsc>{ntsc}</ntsc>\n"
            f"{indent}</rate>")


def _timecode_block(seconds: float, fps: float, indent: str = "") -> str:
    return (f"{indent}<timecode>\n"
            f"{indent}  <string>{_tc_string(seconds, fps)}</string>\n"
            f"{indent}  <frame>{_frames(seconds, fps)}</frame>\n"
            f"{indent}  <displayformat>NDF</displayformat>\n"
            f"{_rate_block(fps, indent + '  ')}\n"
            f"{indent}</timecode>")


def _file_url(path: str) -> str:
    abspath = os.path.abspath(path).replace(os.sep, "/")
    if not abspath.startswith("/"):
        abspath = "/" + abspath
    return "file://" + abspath  # -> file:///Users/... or file:///home/...


def _file_definition(clip: ClipResult, file_id: str, fps: float, indent: str = "  ") -> str:
    # Sources with no audio stream at all (clip.audio_channels == 0, set
    # by analyzer._probe_audio_format when ffprobe finds zero audio
    # streams) must omit <media><audio> entirely rather than declaring
    # a <channelcount>0</channelcount> block -- declaring an <audio>
    # section at all tells Premiere this file has audio media to
    # conform/link against, and since no audio clipitems are ever
    # emitted for such a clip (see export_xml's per-channel loop),
    # Premiere fails to auto-link the (nonexistent) audio and the
    # video clipitem is left unlinked.
    audio_xml = ""
    if clip.audio_channels > 0:
        audio_xml = f"""
{indent}    <audio>
{indent}      <samplecharacteristics>
{indent}        <depth>{clip.audio_bit_depth}</depth>
{indent}        <samplerate>{clip.audio_samplerate}</samplerate>
{indent}      </samplecharacteristics>
{indent}      <channelcount>{clip.audio_channels}</channelcount>
{indent}    </audio>"""
    return f"""{indent}<file id="{file_id}">
{indent}  <name>{escape(clip.filename)}</name>
{indent}  <pathurl>{escape(_file_url(clip.path))}</pathurl>
{_rate_block(fps, indent + "  ")}
{indent}  <duration>{_frames(clip.duration, fps)}</duration>
{_timecode_block(0.0, fps, indent + "  ")}
{indent}  <media>
{indent}    <video>
{indent}      <samplecharacteristics>
{_rate_block(fps, indent + "        ")}
{indent}        <width>{clip.width}</width>
{indent}        <height>{clip.height}</height>
{indent}      </samplecharacteristics>
{indent}    </video>{audio_xml}
{indent}  </media>
{indent}</file>"""


def _comments_xml(clip: ClipResult, indent: str = "        ", show_energy: bool = True) -> str:
    lines = [f"{indent}<comments>",
             f"{indent}  <mastercomment1>Quality score: {clip.overall_score:.1f}/100</mastercomment1>"]
    if clip.energy_enabled and show_energy:
        lines.append(f"{indent}  <mastercomment2>Energy score: {clip.mean_energy_score:.0f}/100</mastercomment2>")
    lines.append(
        f"{indent}  <mastercomment3>Audio: {escape(clip.audio_channel_layout)} "
        f"({clip.audio_channels}ch, {clip.audio_samplerate}Hz/{clip.audio_bit_depth}-bit)</mastercomment3>")
    lines.append(f"{indent}</comments>")
    return "\n".join(lines)


def _bin_clip_xml(clip: ClipResult, clip_id: str, file_id: str, fps: float,
                   show_energy: bool = True) -> str:
    """A master-clip entry for the Project panel / bin. Uses <clip>, not
    <clipitem> -- <clipitem> is reserved for items placed on a track."""
    in_f = 0
    out_f = _frames(clip.duration, fps)
    return f"""      <clip id="{clip_id}">
        <name>{escape(clip.label)}</name>
        <duration>{out_f}</duration>
{_rate_block(fps, "        ")}
        <in>{in_f}</in>
        <out>{out_f}</out>
        <ismasterclip>TRUE</ismasterclip>
{_file_definition(clip, file_id, fps, indent="        ")}
{_comments_xml(clip, indent="        ", show_energy=show_energy)}
      </clip>"""


def _track_clipitem_xml(clip: ClipResult, item_id: str, file_id: str, fps: float,
                         in_sec: float, out_sec: float,
                         timeline_start_frames: int, name_suffix: str = ""):
    """An item placed on the sequence's video track. References the file
    by id only (the full <file> definition already appeared in the bin),
    per xmeml's id-reuse convention.

    Deliberately uses a single `fps` (the SEQUENCE's rate, passed in by
    export_xml) for every field here -- `<rate>`, `<in>`, `<out>`,
    `<duration>`, and `<start>`/`<end>`. An earlier version of this
    function tried expressing `<in>`/`<out>`/`<duration>`/`<rate>` at
    the clip's own native rate (to match the source file's true frame
    rate) while keeping `<start>`/`<end>` at the sequence rate, on the
    theory that Premiere resolves in/out points against the referenced
    file's own native rate. Confirmed wrong by an actual Premiere
    import: Premiere reads a clipitem's `<in>`/`<out>` at the SEQUENCE's
    rate regardless of what the clipitem's own `<rate>` (or its file's
    native rate) declares, so a clip whose native fps differs from the
    sequence's showed up with a diagonal-hash "insufficient media"
    warning -- the in/out frame numbers, computed at the clip's own
    (different) native rate, resolved to a longer real-world duration
    than Premiere expected and than the source file actually has. Do
    not reintroduce a per-clip rate here without verifying against a
    real Premiere import first."""
    in_f = _frames(in_sec, fps)
    out_f = _frames(out_sec, fps)
    dur_f = out_f - in_f
    timeline_end_frames = timeline_start_frames + dur_f
    display_name = clip.label + name_suffix

    xml = f"""          <clipitem id="{item_id}">
            <name>{escape(display_name)}</name>
            <enabled>TRUE</enabled>
            <duration>{_frames(clip.duration, fps)}</duration>
{_rate_block(fps, "            ")}
            <start>{timeline_start_frames}</start>
            <end>{timeline_end_frames}</end>
            <in>{in_f}</in>
            <out>{out_f}</out>
            <file id="{file_id}"/>
{_comments_xml(clip, indent="            ")}
          </clipitem>"""
    return xml, timeline_end_frames


def _track_clipitem_xml_audio(clip: ClipResult, item_id: str, file_id: str, fps: float,
                               in_sec: float, out_sec: float,
                               timeline_start_frames: int, name_suffix: str = "",
                               channel: Optional[int] = None):
    """The audio counterpart of _track_clipitem_xml: same in/out/timeline
    position as the video segment it pairs with, so the clip's original
    sound stays attached and in sync rather than being left silent.

    Uses a single `fps` (the sequence's rate) for every field, same as
    _track_clipitem_xml -- see that function's docstring for why a
    per-clip native rate was tried and reverted after a real Premiere
    import showed it causing "insufficient media" errors.

    `channel`, when given (which is always, in practice -- see
    export_xml's per-channel loop), narrows this item to one discrete
    source channel via <sourcetrack>, placed on that channel's own
    sequence track. This applies uniformly to mono, stereo, and
    multichannel sources alike: a single stereo clipitem carrying
    <channelcount>2</channelcount> silently imports as mono in Premiere,
    so stereo is always split into two linked mono items (L/R) rather
    than kept as one combined item."""
    in_f = _frames(in_sec, fps)
    out_f = _frames(out_sec, fps)
    dur_f = out_f - in_f
    timeline_end_frames = timeline_start_frames + dur_f
    display_name = clip.label + name_suffix

    sourcetrack_xml = ""
    if channel is not None:
        sourcetrack_xml = f"""
            <sourcetrack>
              <mediatype>audio</mediatype>
              <trackindex>{channel}</trackindex>
            </sourcetrack>"""

    xml = f"""          <clipitem id="{item_id}">
            <name>{escape(display_name)}</name>
            <enabled>TRUE</enabled>
            <duration>{_frames(clip.duration, fps)}</duration>
{_rate_block(fps, "            ")}
            <start>{timeline_start_frames}</start>
            <end>{timeline_end_frames}</end>
            <in>{in_f}</in>
            <out>{out_f}</out>
            <file id="{file_id}"/>{sourcetrack_xml}
          </clipitem>"""
    return xml, timeline_end_frames


def export_xml(results: List[ClipResult], output_path: str,
                sequence_name: str = "Best B-Roll Selects",
                show_energy: bool = True):
    """
    results: list of ClipResult, expected already sorted best-first.
    Writes a Final Cut Pro XML (v5) file Premiere Pro can import.

    show_energy: whether the energy-score bin comment (mastercomment2)
    should appear at all for this export run, independent of whether a
    given clip's cached data happens to include one. A cache hit can
    carry `ClipResult.energy_enabled=True` even when the run that
    produced `results` had energy scoring turned off (see
    analyzer.rescore_clip's docstring) -- callers should pass whether
    energy scoring was actually active for that run, not rely on the
    per-clip flag alone.
    """
    if not results:
        raise ValueError("No clips to export")

    fpses = [c.fps for c in results if c.fps]
    if fpses:
        from collections import Counter
        sequence_fps = Counter(fpses).most_common(1)[0][0]
    else:
        sequence_fps = 25.0

    # Every clip's audio is split one discrete channel per sequence
    # track -- a stereo clip lands on 2 tracks (L, R), a 5.1 clip on 6,
    # a mono clip on 1 -- rather than carrying a clip's audio as one
    # combined interleaved item. Track count is sized to the widest
    # channel count among this batch's clips; clips needing fewer
    # tracks than that simply leave the extra ones empty for their time
    # range. Each clip's own <file>/<audio> block (see _bin_clip_xml)
    # and mastercomment3 report that clip's real channel count/layout
    # regardless of how its channels get distributed across tracks here.
    def _tracks_needed(channels: int) -> int:
        return max(1, channels)

    max_audio_channels = max((_tracks_needed(c.audio_channels) for c in results), default=1)
    audio_track_items = [[] for _ in range(max_audio_channels)]

    bin_clips = []
    timeline_clipitems = []
    timeline_cursor = 0

    for idx, clip in enumerate(results):
        clip_fps = clip.fps or sequence_fps
        clip_id = f"masterclip-{idx+1}"
        file_id = f"file-{idx+1}"

        bin_clips.append(_bin_clip_xml(clip, clip_id, file_id, clip_fps, show_energy=show_energy))

        # A clip may have multiple recommended segments; place each one
        # on the timeline, back-to-back, in chronological (source) order.
        segments = clip.segments or [
            Segment(start=clip.best_window_start, end=clip.best_window_end,
                    score=clip.best_window_score)
        ]
        for seg_idx, seg in enumerate(segments):
            item_id = f"clipitem-{idx+1}-{seg_idx+1}"
            suffix = f" (seg {seg_idx+1})" if len(segments) > 1 else ""
            seg_start_frames = timeline_cursor
            tl_item, timeline_cursor = _track_clipitem_xml(
                clip, item_id, file_id, sequence_fps,
                seg.start, seg.end, timeline_cursor, name_suffix=suffix)
            timeline_clipitems.append(tl_item)

            # One audio item per source channel, each pinned (via
            # `channel=`/<sourcetrack>) to that channel and placed on
            # its own sequence track.
            for ch in range(1, clip.audio_channels + 1):
                a_item, _ = _track_clipitem_xml_audio(
                    clip, f"{item_id}-a{ch}", file_id, sequence_fps,
                    seg.start, seg.end, seg_start_frames, channel=ch,
                    name_suffix=suffix)
                audio_track_items[ch - 1].append(a_item)

    total_seq_frames = timeline_cursor
    width = results[0].width or 1920
    height = results[0].height or 1080

    from collections import Counter
    audio_formats = [(c.audio_samplerate, c.audio_bit_depth) for c in results]
    sequence_samplerate, sequence_bit_depth = (
        Counter(audio_formats).most_common(1)[0][0] if audio_formats else (48000, 16))

    # Matches however many discrete channel-tracks the batch actually
    # needs -- 2 for stereo, 6 for 5.1, 1 if every clip is mono -- now
    # that every clip's channels are split one per track above.
    sequence_output_channels = max_audio_channels

    audio_tracks_xml = "\n".join(
        f"            <track>\n{chr(10).join(items)}\n            </track>"
        for items in audio_track_items
    )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="5">
  <project>
    <name>B-Roll Analysis</name>
    <children>
      <bin>
        <name>B-Roll Analysis - Ranked Clips</name>
        <children>
{chr(10).join(bin_clips)}
        </children>
      </bin>
      <sequence id="sequence-1">
        <name>{escape(sequence_name)}</name>
        <duration>{total_seq_frames}</duration>
{_rate_block(sequence_fps, "        ")}
        <in>-1</in>
        <out>-1</out>
{_timecode_block(0.0, sequence_fps, "        ")}
        <media>
          <video>
            <format>
              <samplecharacteristics>
{_rate_block(sequence_fps, "                ")}
                <width>{width}</width>
                <height>{height}</height>
              </samplecharacteristics>
            </format>
            <track>
{chr(10).join(timeline_clipitems)}
            </track>
          </video>
          <audio>
            <numOutputChannels>{sequence_output_channels}</numOutputChannels>
            <format>
              <samplecharacteristics>
                <depth>{sequence_bit_depth}</depth>
                <samplerate>{sequence_samplerate}</samplerate>
                <channelcount>{sequence_output_channels}</channelcount>
              </samplecharacteristics>
            </format>
{audio_tracks_xml}
          </audio>
        </media>
      </sequence>
    </children>
  </project>
</xmeml>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(xml)
    return output_path
