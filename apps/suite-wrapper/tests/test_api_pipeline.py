"""api_pipeline.py -- PipelineMixin.pipeline_list_videos: the one thing
the "Run Pipeline" frontend feature can't do itself (scan a folder for
video files). Pure filesystem logic, no job/window/self-state involved,
so the mixin is exercised directly rather than via the composed SuiteApi."""

import os

from backend.api_pipeline import PipelineMixin


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00")


def test_missing_folder_returns_error():
    res = PipelineMixin().pipeline_list_videos("/no/such/folder")
    assert res == {"ok": False, "error": "Folder not found: /no/such/folder"}


def test_finds_videos_recursively_and_sorts(tmp_path):
    _touch(str(tmp_path / "b.mp4"))
    _touch(str(tmp_path / "sub" / "a.mov"))
    _touch(str(tmp_path / "sub" / "clip.braw"))
    _touch(str(tmp_path / "notes.txt"))  # not a video -- excluded

    res = PipelineMixin().pipeline_list_videos(str(tmp_path))

    assert res["ok"] is True
    names = sorted(os.path.basename(p) for p in res["videos"])
    assert names == ["a.mov", "b.mp4", "clip.braw"]


def test_skips_dotfiles_and_dot_directories(tmp_path):
    _touch(str(tmp_path / ".DS_Store.mp4"))  # dotfile, even with a video extension
    _touch(str(tmp_path / ".hidden" / "clip.mp4"))
    _touch(str(tmp_path / "real.mp4"))

    res = PipelineMixin().pipeline_list_videos(str(tmp_path))

    assert res["ok"] is True
    names = [os.path.basename(p) for p in res["videos"]]
    assert names == ["real.mp4"]


def test_empty_folder_returns_empty_list(tmp_path):
    res = PipelineMixin().pipeline_list_videos(str(tmp_path))
    assert res == {"ok": True, "videos": []}
