"""synced_audio_splice.py -- splicing external synced audio into RCS's
XMEML export (addendum v3), and the durable audio-note-in-VTT discovery
path (addendum v5)."""

import os
import xml.etree.ElementTree as ET

from backend import sync_xml, synced_audio_splice

V_PROBE = {"duration": 10.0, "fps": 25.0, "width": 1280, "height": 720,
           "has_video": True, "has_audio": True, "audio_channels": 2,
           "audio_samplerate": 48000, "audio_bits": 16}


def test_splice_external_audio_honors_enabled_and_channels_routing():
    # (addendum v3/v4) synced_audio_splice honors the SAME routing as
    # build_sync_xml, via a stubbed discover_fn.
    base_xml = sync_xml.build_sync_xml(
        {"path": "/media/A.mp4", "probe": V_PROBE}, [],
        include_camera_audio=False, sequence_name="splice base")[0]
    # Rewrite pathurls to RCS's file://localhost form + give V1 a source.
    resolved = [{"order": 0, "track": "main", "source_id": "srcA", "in_seconds": 0.0}]
    media_paths = {"srcA": "/media/A.mp4"}

    def _stub_discover(vp, tp=None):
        # Addendum v5 gave splice_external_audio's discover_fn a second
        # positional arg (transcript_path); accept (and ignore) it here
        # so this stub keeps matching the real call signature.
        return [
            {"audio_path": "/media/on.wav", "offset_seconds": 0.0,
             "channel_count": 2, "enabled": True, "channels": [1]},
            {"audio_path": "/media/off.wav", "offset_seconds": 0.0,
             "channel_count": 2, "enabled": False, "channels": None},
        ]

    spliced, _ = synced_audio_splice.splice_external_audio(
        base_xml, resolved, media_paths, 25.0, discover_fn=_stub_discover)
    assert spliced is not None, "splice returned None with a synced source"
    root_sp = ET.fromstring(spliced)
    sp_files = {f.findtext("name") for f in root_sp.iter("file")}
    assert "on.wav" in sp_files and "off.wav" not in sp_files, \
        f"splice disabled track not omitted: {sp_files}"
    on_clips = [c for c in root_sp.findall(".//audio/track/clipitem")
                if c.findtext("name") == "on.wav"]
    assert len(on_clips) == 1, f"channels=[1] splice clip count: {len(on_clips)}"
    assert on_clips[0].findtext("sourcetrack/trackindex") == "1", \
        "spliced channel selection wrong"


def test_parse_audio_notes_from_transcript(tmp_path):
    # (addendum v5) synthetic transcript with 2 valid audio notes (one
    # positive, one negative offset), 1 malformed audio-note line (no
    # "(offset ...)"), and the ordinary video-note line -- exactly 2
    # entries come back, correctly parsed; the malformed/video lines
    # contribute nothing.
    aud_a = str(tmp_path / "boom mic.wav")
    aud_b = str(tmp_path / "lav.wav")
    for p in (aud_a, aud_b):
        open(p, "wb").close()
    transcript_path = str(tmp_path / "clip.vtt")
    with open(transcript_path, "w", encoding="utf-8") as f:
        f.write(
            "WEBVTT\n\n"
            "NOTE Source video: /media/some video.mp4\n"
            f"NOTE Source audio: {aud_a} (offset +1.500s)\n"
            f"NOTE Source audio: {aud_b} (offset -0.420s)\n"
            "NOTE Source audio: not a real note, no offset here\n\n"
            "00:00:00.000 --> 00:00:01.000\nHello\n\n"
        )
    notes = synced_audio_splice.parse_audio_notes_from_transcript(transcript_path)
    assert len(notes) == 2, f"parse_audio_notes_from_transcript count: {notes}"
    by_path = {n["audio_path"]: n["offset_seconds"] for n in notes}
    assert by_path.get(aud_a) == 1.5, f"positive offset parse: {by_path}"
    assert by_path.get(aud_b) == -0.42, f"negative offset parse: {by_path}"


def test_parse_audio_notes_from_transcript_with_no_notes_is_empty(tmp_path):
    plain_transcript = str(tmp_path / "plain.vtt")
    with open(plain_transcript, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\nNOTE Source video: /media/plain.mp4\n\n"
                "00:00:00.000 --> 00:00:01.000\nHi\n\n")
    assert synced_audio_splice.parse_audio_notes_from_transcript(plain_transcript) == []


def test_discover_synced_audios_for_plain_video_is_empty(tmp_path):
    # No regression: a plain video with no sidecar, no cache, and no
    # transcript note anywhere produces discover_synced_audios(...) == []
    # -- ordinary videos never get a spurious audio note.
    plain_video = str(tmp_path / "plain.mp4")
    plain_transcript = str(tmp_path / "plain.vtt")
    with open(plain_transcript, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\nNOTE Source video: /media/plain.mp4\n\n"
                "00:00:00.000 --> 00:00:01.000\nHi\n\n")
    assert synced_audio_splice.discover_synced_audios(plain_video) == []
    assert synced_audio_splice.discover_synced_audios(plain_video, plain_transcript) == []
