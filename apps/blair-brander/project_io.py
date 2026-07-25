"""
project_io.py — save/load project files (.blairtitle, plain JSON under the
hood). Local disk only; no network involved.
"""

import json

PROJECT_EXTENSION = ".blairtitle"
FORMAT_VERSION = 1


def scene_to_json_dict(scene):
    """Convert a scene dict into something json.dump can handle directly
    (tuples -> lists), tagged with a format version for future migrations."""
    out = {}
    for k, v in scene.items():
        if isinstance(v, tuple):
            out[k] = {"__tuple__": True, "items": list(v)}
        else:
            out[k] = v
    return {"format_version": FORMAT_VERSION, "scene": out}


def json_dict_to_scene(data):
    """Reverse of scene_to_json_dict — restores tuples where they were."""
    raw = data.get("scene", data)  # tolerate a bare scene dict too
    scene = {}
    for k, v in raw.items():
        if isinstance(v, dict) and v.get("__tuple__"):
            scene[k] = tuple(v["items"])
        else:
            scene[k] = v
    return scene


def save_project(scene, path):
    payload = scene_to_json_dict(scene)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def load_project(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return json_dict_to_scene(data)
