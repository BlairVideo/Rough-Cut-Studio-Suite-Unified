"""api_security.py's _add_transcript/pick_transcript_files extension
(Phase 2, Edit-preview remainder): the third way a .braw source can get
linked in the Edit workspace, alongside link_media_file/batch_relink_media
(suite_api.py) -- an embedded `NOTE Source video: <path.braw>` line in a
sent/imported transcript, auto-linked by RCS's own detect_linked_media
now that VIDEO_EXTENSIONS includes .braw. Proxy generation must be queued
(fire-and-forget) right after ingestion, not left until first preview."""

from backend import api_security


def _write_vtt_with_source_video(path, source_video_path):
    path.write_text(
        "WEBVTT\n\n"
        f"NOTE Source video: {source_video_path}\n\n"
        "00:00:00.000 --> 00:00:01.000\n"
        "Hello\n",
        encoding="utf-8",
    )


def test_add_transcript_queues_braw_proxy_for_auto_linked_media(monkeypatch, api, tmp_path):
    braw_clip = tmp_path / "clip.braw"
    braw_clip.write_bytes(b"\x00")
    vtt_path = tmp_path / "transcript.vtt"
    _write_vtt_with_source_video(vtt_path, braw_clip)

    queued = []
    monkeypatch.setattr(api_security.braw_bridge, "queue_missing_proxies",
                        lambda job_manager, paths: queued.append(list(paths)) or [])

    result = api._add_transcript(str(vtt_path))
    assert result.get("error") is None, result
    source_id = result["source_id"]
    assert api.media_paths.get(source_id) == str(braw_clip), \
        "must survive the allowlist prune now that VIDEO_EXTENSIONS includes .braw"
    assert queued and str(braw_clip) in queued[-1]


def test_add_transcript_does_not_queue_when_nothing_is_linked(monkeypatch, api, tmp_path):
    """A transcript with no resolvable embedded media (or none at all)
    must not blow up queue_missing_proxies -- it's called with whatever
    media_paths already holds (possibly empty), which the real function
    already handles fine; this just confirms the call site itself never
    raises when there's nothing .braw to queue."""
    vtt_path = tmp_path / "transcript.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n", encoding="utf-8")

    result = api._add_transcript(str(vtt_path))
    assert result.get("error") is None, result
