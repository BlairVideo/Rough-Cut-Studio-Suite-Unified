"""Card Eater /Volumes polling (backend/cardeater_volume_watcher.py). Ported
from CardEater's own volume_watcher.rs. _list_current_volume_paths and the
card_detect calls are monkeypatched throughout so these tests never touch
the real /Volumes or spawn the real polling thread (run()/start() are
exercised only implicitly via _run_once, the pure-logic step run() loops)."""

import pytest

from backend import cardeater_volume_watcher as watcher


def test_diff_volumes_detects_appeared_and_disappeared():
    appeared, disappeared = watcher.diff_volumes({"/Volumes/A"}, {"/Volumes/A", "/Volumes/B"})
    assert appeared == ["/Volumes/B"]
    assert disappeared == []

    appeared, disappeared = watcher.diff_volumes({"/Volumes/A", "/Volumes/B"}, {"/Volumes/B"})
    assert appeared == []
    assert disappeared == ["/Volumes/A"]


def test_diff_volumes_no_change():
    appeared, disappeared = watcher.diff_volumes({"/Volumes/A"}, {"/Volumes/A"})
    assert appeared == []
    assert disappeared == []


@pytest.fixture
def registry():
    return watcher.CardRegistry()


def _fake_card_info(path):
    return {"id": path, "label": path.rsplit("/", 1)[-1], "mount_path": path,
            "total_files": 0, "total_bytes": 0, "has_dcim": True, "is_dev_fallback": False}


def test_run_once_activates_a_camera_looking_card(registry, monkeypatch):
    monkeypatch.setattr(watcher, "_list_current_volume_paths", lambda: {"/Volumes/CARD1"})
    monkeypatch.setattr(watcher.card_detect, "looks_like_camera_card", lambda p: True)
    monkeypatch.setattr(watcher.card_detect, "build_card_info", lambda p, is_dev_fallback: _fake_card_info(p))

    current = watcher._run_once(registry, known=set())
    assert current == {"/Volumes/CARD1"}
    assert registry.active is not None
    assert registry.active["mount_path"] == "/Volumes/CARD1"


def test_run_once_ignores_non_camera_looking_volume(registry, monkeypatch):
    monkeypatch.setattr(watcher, "_list_current_volume_paths", lambda: {"/Volumes/Installer"})
    monkeypatch.setattr(watcher.card_detect, "looks_like_camera_card", lambda p: False)

    watcher._run_once(registry, known=set())
    assert registry.active is None


def test_run_once_ignores_second_card_while_one_already_active(registry, monkeypatch):
    registry.active = _fake_card_info("/Volumes/CARD1")
    monkeypatch.setattr(watcher, "_list_current_volume_paths", lambda: {"/Volumes/CARD1", "/Volumes/CARD2"})
    monkeypatch.setattr(watcher.card_detect, "looks_like_camera_card", lambda p: True)
    monkeypatch.setattr(watcher.card_detect, "build_card_info", lambda p, is_dev_fallback: _fake_card_info(p))

    watcher._run_once(registry, known={"/Volumes/CARD1"})
    assert registry.active["mount_path"] == "/Volumes/CARD1", \
        "a second inserted card while one is active must be silently ignored (Phase 1: no simultaneous ingest)"


def test_run_once_clears_active_card_on_disappearance(registry, monkeypatch):
    registry.active = _fake_card_info("/Volumes/CARD1")
    monkeypatch.setattr(watcher, "_list_current_volume_paths", lambda: set())

    watcher._run_once(registry, known={"/Volumes/CARD1"})
    assert registry.active is None


def test_run_once_disappearance_processed_before_appearance_same_tick(registry, monkeypatch):
    """Card ejected and a different card inserted in the same poll tick: the
    active card must be cleared BEFORE the newly appeared path is evaluated,
    so the new card can immediately become active rather than being
    silently ignored as a 'second card while one is active'."""
    registry.active = _fake_card_info("/Volumes/CARD1")
    monkeypatch.setattr(watcher, "_list_current_volume_paths", lambda: {"/Volumes/CARD2"})
    monkeypatch.setattr(watcher.card_detect, "looks_like_camera_card", lambda p: True)
    monkeypatch.setattr(watcher.card_detect, "build_card_info", lambda p, is_dev_fallback: _fake_card_info(p))

    watcher._run_once(registry, known={"/Volumes/CARD1"})
    assert registry.active["mount_path"] == "/Volumes/CARD2"


def test_run_once_swallows_oserror_from_build_card_info(registry, monkeypatch):
    monkeypatch.setattr(watcher, "_list_current_volume_paths", lambda: {"/Volumes/CARD1"})
    monkeypatch.setattr(watcher.card_detect, "looks_like_camera_card", lambda p: True)

    def raise_oserror(p, is_dev_fallback):
        raise OSError("volume ejected mid-scan")

    monkeypatch.setattr(watcher.card_detect, "build_card_info", raise_oserror)
    watcher._run_once(registry, known=set())  # must not raise
    assert registry.active is None


def test_card_registry_starts_with_no_active_card():
    assert watcher.CardRegistry().active is None
