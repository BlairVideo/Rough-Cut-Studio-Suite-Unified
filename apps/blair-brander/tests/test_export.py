"""
tests/test_export.py

Unit tests for export.py's pure frame-scheduling logic (_frame_times,
_rle_times) -- the per-frame (t, elapsed) schedule and its run-length
dedup that collapses the settled hold-tail into one repeated render (see
_rle_times' own docstring for why this is safe) -- plus an export_png
integration test against the real renderer/fonts/assets. Deliberately
does NOT touch export_video/ffmpeg_available, which need a real ffmpeg
subprocess.
"""

from PIL import Image

from app import default_scene
from export import _frame_times, _rle_times, export_png


# ---------------------------------------------------------------------------
# _frame_times
# ---------------------------------------------------------------------------

def test_frame_times_count_matches_duration_plus_hold():
    frames = _frame_times({"duration": 2.0, "hold_seconds": 1.0}, fps=10)
    assert len(frames) == 30  # (2.0 + 1.0) * 10


def test_frame_times_t_is_clamped_to_one_during_hold_tail():
    frames = _frame_times({"duration": 2.0, "hold_seconds": 1.0}, fps=10)
    during_duration = frames[:20]
    during_hold = frames[20:]
    assert all(t < 1.0 for t, _ in during_duration)
    assert all(t == 1.0 for t, _ in during_hold)


def test_frame_times_elapsed_seconds_increases_monotonically_through_hold():
    frames = _frame_times({"duration": 2.0, "hold_seconds": 1.0}, fps=10)
    elapsed = [e for _, e in frames]
    assert elapsed == sorted(elapsed)
    assert elapsed[-1] > elapsed[19]  # still increases during the hold tail


def test_frame_times_duration_has_a_floor():
    # duration=0 would divide by zero in denom without the max(0.1, ...) floor.
    frames = _frame_times({"duration": 0.0, "hold_seconds": 0.0}, fps=10)
    assert len(frames) == 1  # round(0.1 * 10) == 1


def test_frame_times_no_hold_seconds_defaults_to_one():
    with_default = _frame_times({"duration": 1.0}, fps=10)
    explicit = _frame_times({"duration": 1.0, "hold_seconds": 1.0}, fps=10)
    assert len(with_default) == len(explicit)


# ---------------------------------------------------------------------------
# _rle_times
# ---------------------------------------------------------------------------

def test_rle_times_collapses_the_hold_tail_into_one_run():
    frames = _frame_times({"duration": 2.0, "hold_seconds": 1.0}, fps=10)
    runs = _rle_times(frames, exact=False)
    # 20 distinct-t frames during the duration + 1 collapsed hold-tail run
    assert len(runs) == 21
    last_t, last_elapsed, last_count = runs[-1]
    assert last_t == 1.0
    assert last_count == 10
    # total frame count is preserved across the collapse
    assert sum(count for _, _, count in runs) == len(frames)


def test_rle_times_exact_mode_never_collapses_hold_tail():
    # exact=True is required whenever elapsed_seconds matters (logo_grow)
    # -- every hold-tail frame must render individually since it differs
    # in elapsed even though t is pinned at 1.0.
    frames = _frame_times({"duration": 2.0, "hold_seconds": 1.0}, fps=10)
    runs = _rle_times(frames, exact=True)
    assert len(runs) == len(frames)
    assert all(count == 1 for _, _, count in runs)


def test_rle_times_only_collapses_consecutive_duplicates():
    # (0.0, 0.4) reappears at the end but is NOT adjacent to the earlier
    # (0.0, *) pair -- order-preserving RLE must keep it as its own run,
    # not merge it with the first occurrence.
    frame_times = [(0.0, 0.0), (0.0, 0.1), (0.5, 0.2), (0.5, 0.3), (0.0, 0.4)]
    runs = _rle_times(frame_times, exact=False)
    assert runs == [(0.0, 0.0, 2), (0.5, 0.2, 2), (0.0, 0.4, 1)]


def test_rle_times_no_duplicates_is_identity():
    frame_times = [(0.0, 0.0), (0.25, 0.1), (0.5, 0.2)]
    runs = _rle_times(frame_times, exact=False)
    assert runs == [(0.0, 0.0, 1), (0.25, 0.1, 1), (0.5, 0.2, 1)]


# ---------------------------------------------------------------------------
# export_png -- integration smoke test against real fonts/assets
# ---------------------------------------------------------------------------

def test_export_png_writes_a_real_png(tmp_path):
    scene = default_scene()
    out_path = tmp_path / "out.png"
    result = export_png(scene, str(out_path))
    assert result == str(out_path)
    assert out_path.exists()
    with Image.open(out_path) as img:
        assert img.format == "PNG"
        assert img.size == tuple(scene["canvas_size"])
