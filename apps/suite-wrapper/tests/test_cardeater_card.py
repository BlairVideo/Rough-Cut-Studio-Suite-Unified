"""Card Eater card detection/scanning (backend/cardeater_card.py). Ported
from CardEater's own card_detect.rs. metadata.resolve_created_at_batch is
monkeypatched in scan_card_files tests so they never shell out to a real
exiftool binary."""

import os

import pytest

from backend import cardeater_card as card_detect


def test_has_dcim_true_when_present(tmp_path):
    (tmp_path / "DCIM").mkdir()
    assert card_detect.has_dcim(str(tmp_path)) is True


def test_has_dcim_is_case_insensitive(tmp_path):
    (tmp_path / "dcim").mkdir()
    assert card_detect.has_dcim(str(tmp_path)) is True


def test_has_dcim_false_when_absent(tmp_path):
    (tmp_path / "SOMETHING_ELSE").mkdir()
    assert card_detect.has_dcim(str(tmp_path)) is False


def test_looks_like_camera_card_true_for_private_sony_style(tmp_path):
    (tmp_path / "PRIVATE").mkdir()
    assert card_detect.looks_like_camera_card(str(tmp_path)) is True


def test_looks_like_camera_card_false_for_neither(tmp_path):
    (tmp_path / "random_folder").mkdir()
    assert card_detect.looks_like_camera_card(str(tmp_path)) is False


def test_looks_like_camera_card_true_for_media_in_root(tmp_path):
    """Regression test: a card whose camera writes clips directly into
    the root (no DCIM/PRIVATE layout at all) must still be detected --
    this was reported as "plugging in a real card didn't auto-detect
    it" for exactly this layout."""
    (tmp_path / "clip0001.mov").write_bytes(b"x")
    assert card_detect.looks_like_camera_card(str(tmp_path)) is True


def test_looks_like_camera_card_true_for_media_one_level_deep(tmp_path):
    """A crew's own camera-labeled subfolder (e.g. "A001" for the A
    camera) one level under the card's root, with no DCIM/PRIVATE
    anywhere -- same real-world layout as the root-level case above."""
    sub = tmp_path / "A001"
    sub.mkdir()
    (sub / "clip0001.mov").write_bytes(b"x")
    assert card_detect.looks_like_camera_card(str(tmp_path)) is True


def test_looks_like_camera_card_false_for_non_media_files_only(tmp_path):
    (tmp_path / "readme.txt").write_bytes(b"x")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "log.json").write_bytes(b"{}")
    assert card_detect.looks_like_camera_card(str(tmp_path)) is False


def test_looks_like_camera_card_false_for_general_purpose_drive_with_incidental_media(tmp_path):
    """Regression test: a general-purpose external hard drive that happens
    to contain some video/photo file somewhere on it (a Movies folder, an
    old export, etc.) alongside unrelated content must NOT be treated as a
    camera card -- only a volume that's (shallowly) homogeneous camera
    media should auto-activate. This was reported as an external drive
    incorrectly showing up as a detected card."""
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "notes.txt").write_bytes(b"hi")
    (tmp_path / "old_vacation_clip.mov").write_bytes(b"x")
    assert card_detect.looks_like_camera_card(str(tmp_path)) is False


def test_looks_like_camera_card_false_for_media_mixed_with_other_top_level_dirs(tmp_path):
    (tmp_path / "A001").mkdir()
    (tmp_path / "A001" / "clip0001.mov").write_bytes(b"x")
    (tmp_path / "Backups").mkdir()
    (tmp_path / "Backups" / "archive.zip").write_bytes(b"x")
    assert card_detect.looks_like_camera_card(str(tmp_path)) is False


def test_walk_stats_skips_junk_entries(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "clip1.mov").write_bytes(b"0123456789")  # 10 bytes
    (tmp_path / "DCIM" / "clip2.mov").write_bytes(b"01234")       # 5 bytes
    (tmp_path / ".DS_Store").write_bytes(b"junk metadata")
    (tmp_path / ".Trashes").mkdir()
    (tmp_path / ".Trashes" / "deleted.mov").write_bytes(b"should not be counted")

    total_files, total_bytes = card_detect.walk_stats(str(tmp_path))
    assert total_files == 2
    assert total_bytes == 15


def test_scan_card_files_raises_for_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        card_detect.scan_card_files(str(tmp_path / "nope"))


def test_scan_card_files_skips_junk_and_computes_relative_folder(tmp_path, monkeypatch):
    (tmp_path / "DCIM" / "100CANON").mkdir(parents=True)
    (tmp_path / "DCIM" / "100CANON" / "IMG_0001.jpg").write_bytes(b"photo bytes")
    (tmp_path / "clip_root.mov").write_bytes(b"root level clip")
    (tmp_path / ".DS_Store").write_bytes(b"junk")

    monkeypatch.setattr(
        card_detect.metadata, "resolve_created_at_batch",
        lambda paths: {p: (None, "unavailable") for p in paths},
    )

    entries = card_detect.scan_card_files(str(tmp_path))
    by_name = {e["name"]: e for e in entries}

    assert set(by_name) == {"IMG_0001.jpg", "clip_root.mov"}
    assert by_name["IMG_0001.jpg"]["relative_folder"] == os.path.join("DCIM", "100CANON")
    assert by_name["IMG_0001.jpg"]["ext"] == "jpg"
    assert by_name["IMG_0001.jpg"]["size_bytes"] == len(b"photo bytes")
    assert by_name["clip_root.mov"]["relative_folder"] == ""
    assert by_name["clip_root.mov"]["created_at_source"] == "unavailable"


def test_scan_card_files_uses_resolved_metadata(tmp_path, monkeypatch):
    p = tmp_path / "clip.mov"
    p.write_bytes(b"x")
    monkeypatch.setattr(
        card_detect.metadata, "resolve_created_at_batch",
        lambda paths: {str(p): ("2026-07-14T10:00:00+00:00", "exif")},
    )
    entries = card_detect.scan_card_files(str(tmp_path))
    assert entries[0]["created_at"] == "2026-07-14T10:00:00+00:00"
    assert entries[0]["created_at_source"] == "exif"


def test_build_card_info(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "clip.mov").write_bytes(b"0123456789")

    info = card_detect.build_card_info(str(tmp_path), is_dev_fallback=True)
    assert info["mount_path"] == str(tmp_path)
    assert info["label"] == os.path.basename(str(tmp_path))
    assert info["total_files"] == 1
    assert info["total_bytes"] == 10
    assert info["has_dcim"] is True
    assert info["is_dev_fallback"] is True
