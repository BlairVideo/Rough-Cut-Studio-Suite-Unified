"""Offline-volume filtering in spyglass_bridge.py (backend/spyglass_bridge.py).
Search's underlying clips/shots tables have no watched_root_id FK -- a
clip's root membership is inferred by path prefix only -- so these tests
lean on `_spyglass_core.list_watched_roots()` being monkeypatched rather
than a real compiled extension, and never touch the real
spyglass_index.sqlite."""

from backend import spyglass_bridge


def _stub_roots(monkeypatch, roots):
    monkeypatch.setattr(
        spyglass_bridge, "_spyglass_core",
        type("StubCore", (), {"list_watched_roots": staticmethod(lambda: roots)}),
    )


def test_filter_offline_results_drops_shots_under_an_offline_root(monkeypatch):
    _stub_roots(monkeypatch, [
        {"path": "/Volumes/DriveA", "is_online": False},
        {"path": "/Volumes/DriveB", "is_online": True},
    ])
    results = [
        {"clip_file_path": "/Volumes/DriveA/footage/clip1.mov"},
        {"clip_file_path": "/Volumes/DriveB/footage/clip2.mov"},
    ]
    assert spyglass_bridge._filter_offline_results(results) == [
        {"clip_file_path": "/Volumes/DriveB/footage/clip2.mov"},
    ]


def test_filter_offline_results_does_not_conflate_sibling_roots_sharing_a_prefix(monkeypatch):
    """A naive `str.startswith` on the bare root path would wrongly match
    "/Volumes/DriveA2" against an offline "/Volumes/DriveA" -- the
    trailing os.sep in _offline_root_dirs is what prevents that."""
    _stub_roots(monkeypatch, [{"path": "/Volumes/DriveA", "is_online": False}])
    results = [{"clip_file_path": "/Volumes/DriveA2/footage/clip1.mov"}]
    assert spyglass_bridge._filter_offline_results(results) == results


def test_filter_offline_results_is_a_noop_when_every_root_is_online(monkeypatch):
    _stub_roots(monkeypatch, [{"path": "/Volumes/DriveA", "is_online": True}])
    results = [{"clip_file_path": "/Volumes/DriveA/footage/clip1.mov"}]
    assert spyglass_bridge._filter_offline_results(results) == results


def test_filter_offline_results_is_a_noop_with_no_watched_roots(monkeypatch):
    _stub_roots(monkeypatch, [])
    results = [{"clip_file_path": "/Volumes/DriveA/footage/clip1.mov"}]
    assert spyglass_bridge._filter_offline_results(results) == results
