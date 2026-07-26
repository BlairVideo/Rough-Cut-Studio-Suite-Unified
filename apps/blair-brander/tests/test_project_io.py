"""
tests/test_project_io.py

Unit tests for project_io.py's .blairtitle save/load round trip,
including tuple<->list preservation (JSON has no tuple type) since
scene["canvas_size"] is a tuple that must survive the round trip
unchanged for render_frame() to keep working on a reloaded project.
"""

import json

import project_io


def _sample_scene():
    return {
        "title": "COMMENCEMENT 2026",
        "canvas_size": (1920, 1080),
        "bg_color": "#004b8d",
        "vignette": 0,
    }


def test_scene_to_json_dict_converts_tuples():
    data = project_io.scene_to_json_dict(_sample_scene())
    assert data["format_version"] == project_io.FORMAT_VERSION
    assert data["scene"]["canvas_size"] == {"__tuple__": True, "items": [1920, 1080]}
    assert data["scene"]["title"] == "COMMENCEMENT 2026"


def test_json_dict_to_scene_restores_tuples():
    data = project_io.scene_to_json_dict(_sample_scene())
    restored = project_io.json_dict_to_scene(data)
    assert restored["canvas_size"] == (1920, 1080)
    assert isinstance(restored["canvas_size"], tuple)


def test_json_dict_to_scene_tolerates_bare_scene_dict():
    # No {"format_version", "scene"} wrapper -- just a raw scene dict.
    bare = {"title": "x", "canvas_size": {"__tuple__": True, "items": [1080, 1080]}}
    restored = project_io.json_dict_to_scene(bare)
    assert restored["canvas_size"] == (1080, 1080)
    assert restored["title"] == "x"


def test_save_and_load_project_round_trip(tmp_path):
    path = tmp_path / "test.blairtitle"
    scene = _sample_scene()
    project_io.save_project(scene, str(path))
    loaded = project_io.load_project(str(path))
    assert loaded == scene
    assert isinstance(loaded["canvas_size"], tuple)


def test_save_project_writes_valid_json(tmp_path):
    path = tmp_path / "test.blairtitle"
    project_io.save_project(_sample_scene(), str(path))
    with open(path) as f:
        raw = json.load(f)
    assert raw["format_version"] == 1
    assert "scene" in raw


def test_project_extension_constant():
    assert project_io.PROJECT_EXTENSION == ".blairtitle"
