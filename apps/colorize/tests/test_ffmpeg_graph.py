"""
tests/test_ffmpeg_graph.py -- LUT baking, ffmpeg argv construction,
progress-line parsing, and run_export's cancellation/error handling
(against a fake subprocess module -- no real ffmpeg binary required).
"""

import os
import threading

import pytest

from grade import GradeState
from lut import CubeLut, identity_lut
import ffmpeg_graph as fg
from ffmpeg_graph import (ExportSpec, bake_grade_lut, build_export_command,
                           parse_progress_line, prepare_export, run_export)


def test_bake_grade_lut_identity_grade_is_near_identity():
    lut = bake_grade_lut(GradeState(), size=5)
    assert lut.size == 5
    # Corners should map to themselves for a no-op grade.
    assert lut.data[lut.index(0, 0, 0)] == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)
    assert lut.data[lut.index(4, 4, 4)] == pytest.approx((1.0, 1.0, 1.0), abs=1e-6)


def test_bake_grade_lut_blends_creative_lut_by_intensity():
    # A creative LUT that always outputs pure red, blended at 50%.
    size = 3
    red_data = [(1.0, 0.0, 0.0)] * (size ** 3)
    creative = CubeLut(size=size, data=red_data)
    grade = GradeState(lut_intensity=50.0)
    baked = bake_grade_lut(grade, creative_lut=creative, size=size)
    # Lattice point (1,1,1) -> input (0.5, 0.5, 0.5) under identity grade,
    # blended 50% toward pure red.
    mid_index = baked.index(1, 1, 1)
    r, g, b = baked.data[mid_index]
    assert r == pytest.approx(0.75, abs=1e-6)   # 0.5 + (1.0 - 0.5) * 0.5
    assert g == pytest.approx(0.25, abs=1e-6)   # 0.5 + (0.0 - 0.5) * 0.5


def test_build_export_command_includes_trim_and_progress_flags():
    spec = ExportSpec(
        source_path="/media/clip.mov",
        output_path="/out/clip_graded.mp4",
        grade=GradeState(),
        in_seconds=2.5,
        out_seconds=7.0,
        preset="share_h264",
    )
    cmd = build_export_command(spec, "/tmp/baked.cube")
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd
    assert "-i" in cmd and spec.source_path in cmd
    assert "-t" in cmd
    assert "-progress" in cmd and "pipe:1" in cmd
    assert spec.output_path == cmd[-1]
    vf_index = cmd.index("-vf")
    assert "lut3d=file=" in cmd[vf_index + 1]


def test_build_export_command_no_out_point_omits_dash_t():
    spec = ExportSpec(
        source_path="/media/clip.mov", output_path="/out/o.mp4",
        grade=GradeState(), in_seconds=0.0, out_seconds=None,
    )
    cmd = build_export_command(spec, "/tmp/baked.cube")
    assert "-t" not in cmd


def test_build_export_command_unknown_preset_raises():
    spec = ExportSpec(
        source_path="/a", output_path="/b", grade=GradeState(), preset="not_a_real_preset")
    with pytest.raises(ValueError):
        build_export_command(spec, "/tmp/baked.cube")


def test_prepare_export_writes_temp_lut_and_cleans_up_is_caller_responsibility(tmp_path):
    spec = ExportSpec(
        source_path="/media/clip.mov", output_path="/out/o.mp4",
        grade=GradeState(), preset="share_h264",
    )
    cmd = prepare_export(spec, tmp_dir=str(tmp_path))
    vf_index = cmd.index("-vf")
    lut_arg = cmd[vf_index + 1]
    lut_path = lut_arg.split("file=", 1)[1].strip("'")
    assert os.path.exists(lut_path)
    os.remove(lut_path)  # caller's responsibility, per docstring


def test_parse_progress_line_out_time_ms():
    parsed = parse_progress_line("out_time_ms=5000000", total_duration_seconds=10.0)
    assert parsed["percent"] == pytest.approx(50.0, abs=1e-6)
    assert parsed["done"] is False


def test_parse_progress_line_progress_end():
    parsed = parse_progress_line("progress=end", total_duration_seconds=10.0)
    assert parsed["done"] is True
    assert parsed["percent"] == 100.0


def test_parse_progress_line_ignores_unknown_keys():
    assert parse_progress_line("frame=120", total_duration_seconds=10.0) is None
    assert parse_progress_line("bitrate=1234.5kbits/s", total_duration_seconds=10.0) is None


def test_parse_progress_line_no_duration_returns_none_for_time():
    assert parse_progress_line("out_time_ms=5000000", total_duration_seconds=None) is None


class _FakeStream:
    """Stands in for Popen's text-mode stdout/stderr: iterable line-by-line
    (like the real TextIOWrapper) and supports .readlines() the same way."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)

    def readlines(self):
        return self._lines


class _FakeProc:
    """Minimal stand-in for subprocess.Popen used by run_export's tests."""

    def __init__(self, stdout_lines, returncode=0, stderr_lines=None):
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines or [])
        self._returncode = returncode
        self.terminated = False

    def wait(self):
        return self._returncode

    def terminate(self):
        self.terminated = True


class _FakeSubprocessModule:
    def __init__(self, proc):
        self._proc = proc
        self.PIPE = "PIPE"

    def Popen(self, *args, **kwargs):
        return self._proc


def test_run_export_success_reports_completion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = ExportSpec(
        source_path=str(tmp_path / "in.mov"),
        output_path=str(tmp_path / "out.mp4"),
        grade=GradeState(), preset="share_h264", bake_size=3,
    )
    proc = _FakeProc(stdout_lines=["out_time_ms=5000000\n", "progress=end\n"], returncode=0)
    fake_module = _FakeSubprocessModule(proc)

    events = []
    result = run_export(
        spec, total_duration_seconds=10.0,
        progress_cb=lambda pct, detail: events.append((pct, detail)),
        cancel_event=threading.Event(),
        subprocess_module=fake_module,
    )
    assert result["path"] == spec.output_path
    assert events[-1][0] == 100


def test_run_export_nonzero_exit_raises(tmp_path):
    spec = ExportSpec(
        source_path=str(tmp_path / "in.mov"), output_path=str(tmp_path / "out.mp4"),
        grade=GradeState(), preset="share_h264", bake_size=3,
    )
    proc = _FakeProc(stdout_lines=[], returncode=1, stderr_lines=["ffmpeg: error opening input\n"])
    fake_module = _FakeSubprocessModule(proc)

    with pytest.raises(RuntimeError, match="ffmpeg exited with code 1"):
        run_export(
            spec, total_duration_seconds=10.0,
            progress_cb=lambda pct, detail: None,
            cancel_event=threading.Event(),
            subprocess_module=fake_module,
        )


def test_run_export_cancelled_terminates_and_raises(tmp_path):
    spec = ExportSpec(
        source_path=str(tmp_path / "in.mov"), output_path=str(tmp_path / "out.mp4"),
        grade=GradeState(), preset="share_h264", bake_size=3,
    )
    cancel_event = threading.Event()
    cancel_event.set()  # already cancelled before the export starts reading stdout
    proc = _FakeProc(stdout_lines=["out_time_ms=1000000\n"], returncode=0)
    fake_module = _FakeSubprocessModule(proc)

    with pytest.raises(RuntimeError, match="Cancelled"):
        run_export(
            spec, total_duration_seconds=10.0,
            progress_cb=lambda pct, detail: None,
            cancel_event=cancel_event,
            subprocess_module=fake_module,
        )
    assert proc.terminated is True


def test_run_export_cleans_up_temp_lut_file_on_success(tmp_path):
    spec = ExportSpec(
        source_path=str(tmp_path / "in.mov"), output_path=str(tmp_path / "out.mp4"),
        grade=GradeState(), preset="share_h264", bake_size=3,
    )
    proc = _FakeProc(stdout_lines=["progress=end\n"], returncode=0)
    fake_module = _FakeSubprocessModule(proc)

    seen_lut_paths = []
    real_remove = os.remove

    def spy_remove(path):
        seen_lut_paths.append(path)
        real_remove(path)

    import ffmpeg_graph as fg_mod
    orig = fg_mod.os.remove
    fg_mod.os.remove = spy_remove
    try:
        run_export(
            spec, total_duration_seconds=None,
            progress_cb=lambda pct, detail: None,
            cancel_event=threading.Event(),
            subprocess_module=fake_module,
        )
    finally:
        fg_mod.os.remove = orig

    assert len(seen_lut_paths) == 1
    assert not os.path.exists(seen_lut_paths[0])
