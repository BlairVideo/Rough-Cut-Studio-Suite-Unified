"""
tests/test_project.py -- ColorizeClip/ColorizeProject/GradePreset
dataclass <-> dict round trips and validation.
"""

import pytest

from grade import GradeState
from project import ColorizeClip, ColorizeProject, GradePreset, PROJECT_SCHEMA_VERSION


def test_clip_new_has_uuid_and_defaults():
    clip = ColorizeClip.new("/media/a.mov", order=2)
    assert clip.id
    assert clip.source_path == "/media/a.mov"
    assert clip.in_seconds == 0.0
    assert clip.out_seconds is None
    assert clip.order == 2
    assert clip.grade.is_identity()


def test_clip_round_trip():
    clip = ColorizeClip.new("/media/a.mov")
    clip.in_seconds = 1.5
    clip.out_seconds = 9.25
    clip.grade.exposure = 0.5
    clip.lut_id = "some-lut"
    data = clip.to_dict()
    restored = ColorizeClip.from_dict(data)
    assert restored.id == clip.id
    assert restored.in_seconds == 1.5
    assert restored.out_seconds == 9.25
    assert restored.grade.exposure == 0.5
    assert restored.lut_id == "some-lut"


def test_clip_from_dict_generates_id_if_missing():
    clip = ColorizeClip.from_dict({"source_path": "/x.mov"})
    assert clip.id


def test_clip_from_dict_requires_source_path():
    with pytest.raises(KeyError):
        ColorizeClip.from_dict({})


def test_project_round_trip_preserves_clip_order():
    project = ColorizeProject.new("My Shoot")
    clip_a = ColorizeClip.new("/a.mov", order=1)
    clip_b = ColorizeClip.new("/b.mov", order=0)
    project.clips = [clip_a, clip_b]

    data = project.to_dict()
    assert data["version"] == PROJECT_SCHEMA_VERSION
    # Sorted by `order` on serialization: b (order=0) before a (order=1).
    assert data["clips"][0]["source_path"] == "/b.mov"
    assert data["clips"][1]["source_path"] == "/a.mov"

    restored = ColorizeProject.from_dict(data)
    assert restored.name == "My Shoot"
    assert len(restored.clips) == 2


def test_project_from_dict_rejects_future_schema_version():
    data = ColorizeProject.new("X").to_dict()
    data["version"] = PROJECT_SCHEMA_VERSION + 1
    with pytest.raises(ValueError):
        ColorizeProject.from_dict(data)


def test_project_from_dict_empty_raises():
    with pytest.raises(ValueError):
        ColorizeProject.from_dict({})


def test_grade_preset_round_trip():
    grade = GradeState(exposure=0.3, saturation=10.0)
    preset = GradePreset.new("Warm Look", grade)
    data = preset.to_dict()
    restored = GradePreset.from_dict(data)
    assert restored.name == "Warm Look"
    assert restored.grade.exposure == 0.3
    assert restored.grade.saturation == 10.0
    assert restored.id == preset.id
