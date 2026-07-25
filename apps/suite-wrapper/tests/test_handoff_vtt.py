"""handoff.build_transcript_vtt -- the durable audio-note line embedded
in generated transcript VTTs (addendum v5)."""

from backend import handoff


def test_build_transcript_vtt_embeds_one_note_line_per_audio_ref():
    vtt_with_audio = handoff.build_transcript_vtt(
        "/media/interview.mp4", [{"start": 0, "end": 1, "text": "Hi"}],
        audio_refs=[{"path": "/media/lav.wav", "offset_seconds": 1.5},
                    {"path": "/media/boom.wav", "offset_seconds": -0.42}])
    assert "NOTE Source audio: /media/lav.wav (offset +1.500s)" in vtt_with_audio, \
        vtt_with_audio
    assert "NOTE Source audio: /media/boom.wav (offset -0.420s)" in vtt_with_audio, \
        vtt_with_audio


def test_build_transcript_vtt_without_audio_refs_adds_no_note_line():
    # Regression: audio_refs=None (the ordinary path) adds no note line.
    vtt_no_audio = handoff.build_transcript_vtt(
        "/media/interview.mp4", [{"start": 0, "end": 1, "text": "Hi"}])
    assert "Source audio" not in vtt_no_audio, vtt_no_audio
