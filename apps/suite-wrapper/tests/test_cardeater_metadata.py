"""Card Eater file-metadata resolution (backend/cardeater_metadata.py): batches
files through the local `exiftool` binary, falling back to filesystem
birthtime/mtime when exiftool is missing or a file has no usable date field.
Ported from CardEater's own metadata.rs. subprocess.run is monkeypatched
throughout so these tests never depend on exiftool actually being installed
on the machine running them."""

import json
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from backend import cardeater_metadata as metadata


def _cmd_paths(cmd):
    """Extracts the trailing file-path arguments from an exiftool invocation
    (see metadata._run_exiftool's fixed 5-arg prefix)."""
    return cmd[5:]


def test_resolve_created_at_batch_uses_exif_date_when_available(monkeypatch, tmp_path):
    paths = [str(tmp_path / "a.mov"), str(tmp_path / "b.mov")]
    for p in paths:
        open(p, "wb").close()

    def fake_run(cmd, capture_output, timeout):
        entries = [{"SourceFile": p, "DateTimeOriginal": "2026:07:14 10:23:45"} for p in _cmd_paths(cmd)]
        return SimpleNamespace(stdout=json.dumps(entries).encode(), stderr=b"")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    results = metadata.resolve_created_at_batch(paths)
    for p in paths:
        rfc3339, source = results[p]
        assert source == "exif"
        dt = datetime.fromisoformat(rfc3339)
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == (2026, 7, 14, 10, 23, 45)


def test_resolve_created_at_batch_falls_back_to_filesystem_when_exiftool_missing(monkeypatch, tmp_path):
    p = tmp_path / "a.mov"
    p.write_bytes(b"x")

    def fake_run(cmd, capture_output, timeout):
        raise FileNotFoundError("exiftool not found")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    results = metadata.resolve_created_at_batch([str(p)])
    rfc3339, source = results[str(p)]
    assert source == "filesystem"
    assert rfc3339 is not None
    datetime.fromisoformat(rfc3339)  # must be a parseable ISO timestamp


def test_resolve_created_at_batch_falls_back_when_exiftool_output_unparseable(monkeypatch, tmp_path):
    p = tmp_path / "a.mov"
    p.write_bytes(b"x")

    def fake_run(cmd, capture_output, timeout):
        return SimpleNamespace(stdout=b"not json at all", stderr=b"")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    results = metadata.resolve_created_at_batch([str(p)])
    assert results[str(p)][1] == "filesystem"


def test_resolve_created_at_batch_falls_back_per_file_when_no_usable_date_field(monkeypatch, tmp_path):
    with_date = tmp_path / "with_date.mov"
    without_date = tmp_path / "without_date.mov"
    with_date.write_bytes(b"x")
    without_date.write_bytes(b"x")

    def fake_run(cmd, capture_output, timeout):
        entries = [
            {"SourceFile": str(with_date), "DateTimeOriginal": "2026:07:14 10:23:45"},
            {"SourceFile": str(without_date)},  # no date fields at all
        ]
        return SimpleNamespace(stdout=json.dumps(entries).encode(), stderr=b"")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    results = metadata.resolve_created_at_batch([str(with_date), str(without_date)])
    assert results[str(with_date)][1] == "exif"
    assert results[str(without_date)][1] == "filesystem"


def test_resolve_created_at_batch_chunks_large_path_lists(monkeypatch):
    paths = [f"/card/file{i}.mov" for i in range(metadata.CHUNK_SIZE + 50)]
    call_chunks = []

    def fake_run(cmd, capture_output, timeout):
        chunk = _cmd_paths(cmd)
        call_chunks.append(chunk)
        raise FileNotFoundError("simulate missing exiftool -- forces filesystem fallback")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)
    monkeypatch.setattr(metadata, "_fallback_to_filesystem", lambda path: (None, "unavailable"))

    results = metadata.resolve_created_at_batch(paths)
    assert len(call_chunks) == 2, "expected exactly 2 exiftool invocations for CHUNK_SIZE+50 paths"
    assert len(call_chunks[0]) == metadata.CHUNK_SIZE
    assert len(call_chunks[1]) == 50
    assert len(results) == len(paths)


@pytest.mark.parametrize("raw", [
    "2026:07:14 10:23:45",
    "2026:07:14 10:23:45-04:00",
    "2026:07:14 10:23:45+09:00",
])
def test_parse_exif_datetime_strips_optional_timezone_suffix(raw):
    rfc3339 = metadata._parse_exif_datetime(raw)
    dt = datetime.fromisoformat(rfc3339)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == (2026, 7, 14, 10, 23, 45)


def test_parse_exif_datetime_returns_none_for_garbage():
    assert metadata._parse_exif_datetime("not a date") is None


def test_fallback_to_filesystem_returns_unavailable_for_missing_file(tmp_path):
    rfc3339, source = metadata._fallback_to_filesystem(str(tmp_path / "nope.mov"))
    assert rfc3339 is None
    assert source == "unavailable"


def test_fallback_to_filesystem_returns_parseable_timestamp_for_real_file(tmp_path):
    p = tmp_path / "real.mov"
    p.write_bytes(b"x")
    rfc3339, source = metadata._fallback_to_filesystem(str(p))
    assert source == "filesystem"
    datetime.fromisoformat(rfc3339)  # must not raise


# ---------------------------------------------------------------------------
# resolve_extended_metadata (viewer panel: dimensions/duration/frame rate/camera)
# ---------------------------------------------------------------------------

def test_resolve_extended_metadata_returns_fields_when_present(monkeypatch, tmp_path):
    p = tmp_path / "clip.mov"

    def fake_run(cmd, capture_output, timeout):
        entry = {
            "SourceFile": str(p), "ImageWidth": 1920, "ImageHeight": 1080,
            "Duration": 12.5, "VideoFrameRate": 29.97, "FileType": "MOV",
            "MIMEType": "video/quicktime", "Make": "Sony", "Model": "FX3",
        }
        return SimpleNamespace(stdout=json.dumps([entry]).encode(), stderr=b"")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    result = metadata.resolve_extended_metadata(str(p))
    assert result == {
        "available": True, "width": 1920, "height": 1080,
        "duration_secs": 12.5, "frame_rate": 29.97,
        "file_type": "MOV", "mime_type": "video/quicktime",
        "camera_make": "Sony", "camera_model": "FX3",
    }


def test_resolve_extended_metadata_missing_fields_stay_none(monkeypatch, tmp_path):
    p = tmp_path / "photo.jpg"

    def fake_run(cmd, capture_output, timeout):
        entry = {"SourceFile": str(p), "ImageWidth": 4000, "ImageHeight": 3000, "FileType": "JPEG"}
        return SimpleNamespace(stdout=json.dumps([entry]).encode(), stderr=b"")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    result = metadata.resolve_extended_metadata(str(p))
    assert result["available"] is True
    assert result["width"] == 4000 and result["height"] == 3000
    assert result["duration_secs"] is None
    assert result["camera_make"] is None


def test_resolve_extended_metadata_unavailable_when_exiftool_missing(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, timeout):
        raise FileNotFoundError("exiftool not found")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    result = metadata.resolve_extended_metadata(str(tmp_path / "clip.mov"))
    assert result["available"] is False
    assert all(v is None for k, v in result.items() if k != "available")


def test_resolve_extended_metadata_unavailable_on_unparseable_output(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, timeout):
        return SimpleNamespace(stdout=b"not json at all", stderr=b"")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    result = metadata.resolve_extended_metadata(str(tmp_path / "clip.mov"))
    assert result["available"] is False


def test_resolve_extended_metadata_unavailable_on_empty_entry_list(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, timeout):
        return SimpleNamespace(stdout=b"[]", stderr=b"")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    result = metadata.resolve_extended_metadata(str(tmp_path / "clip.mov"))
    assert result["available"] is False
