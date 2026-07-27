"""
project.py -- Colorize's project/clip/preset data model and JSON
(de)serialization. Pure data + pure functions only: this module never
touches the filesystem itself (no path decisions, no os.makedirs) --
the suite-wrapper bridge owns where project/preset JSON sidecars live
(apps/suite-wrapper/assets/colorize/), matching every other workspace's
JSON-sidecar convention (no shared SQLite exists in this suite).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from grade import GradeState

PROJECT_SCHEMA_VERSION = 1


@dataclass
class ColorizeClip:
    id: str
    source_path: str
    in_seconds: float = 0.0
    out_seconds: Optional[float] = None   # None = uncut end of clip
    grade: GradeState = field(default_factory=GradeState)
    lut_id: Optional[str] = None
    order: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_path": self.source_path,
            "in_seconds": self.in_seconds,
            "out_seconds": self.out_seconds,
            "grade": self.grade.to_dict(),
            "lut_id": self.lut_id,
            "order": self.order,
        }

    @staticmethod
    def from_dict(data: dict) -> "ColorizeClip":
        return ColorizeClip(
            id=data.get("id") or uuid.uuid4().hex,
            source_path=data["source_path"],
            in_seconds=float(data.get("in_seconds", 0.0) or 0.0),
            out_seconds=(float(data["out_seconds"]) if data.get("out_seconds") is not None else None),
            grade=GradeState.from_dict(data.get("grade") or {}),
            lut_id=data.get("lut_id"),
            order=int(data.get("order", 0)),
        )

    @staticmethod
    def new(source_path: str, order: int = 0) -> "ColorizeClip":
        return ColorizeClip(id=uuid.uuid4().hex, source_path=source_path, order=order)


@dataclass
class ColorizeProject:
    id: str
    name: str
    clips: List[ColorizeClip] = field(default_factory=list)
    version: int = PROJECT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "id": self.id,
            "name": self.name,
            "clips": [c.to_dict() for c in sorted(self.clips, key=lambda c: c.order)],
        }

    @staticmethod
    def from_dict(data: dict) -> "ColorizeProject":
        if not data:
            raise ValueError("Empty project data")
        version = int(data.get("version", PROJECT_SCHEMA_VERSION))
        if version > PROJECT_SCHEMA_VERSION:
            raise ValueError(
                f"Project schema version {version} is newer than this build supports "
                f"({PROJECT_SCHEMA_VERSION}) -- update Colorize before opening it")
        clips = [ColorizeClip.from_dict(c) for c in data.get("clips", [])]
        return ColorizeProject(
            id=data.get("id") or uuid.uuid4().hex,
            name=data.get("name") or "Untitled Project",
            clips=clips,
            version=version,
        )

    @staticmethod
    def new(name: str) -> "ColorizeProject":
        return ColorizeProject(id=uuid.uuid4().hex, name=name, clips=[])


@dataclass
class GradePreset:
    id: str
    name: str
    grade: GradeState

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "grade": self.grade.to_dict()}

    @staticmethod
    def from_dict(data: dict) -> "GradePreset":
        return GradePreset(
            id=data.get("id") or uuid.uuid4().hex,
            name=data.get("name") or "Untitled Preset",
            grade=GradeState.from_dict(data.get("grade") or {}),
        )

    @staticmethod
    def new(name: str, grade: GradeState) -> "GradePreset":
        return GradePreset(id=uuid.uuid4().hex, name=name, grade=grade)
