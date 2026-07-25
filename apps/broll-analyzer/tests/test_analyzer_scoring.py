"""
tests/test_analyzer_scoring.py
Unit tests for analyzer.py's composite-scoring and segment-selection
logic (_normalize, _composite, _score_clip/rescore_clip).

Deliberately does NOT touch analyze_clip() itself, or anything that
opens/decodes a real video file (_open_video_capture, _capture_thumbnail,
open_segment_capture, _probe_audio_format, _probe_fps_fallback) -- those
need real media fixtures and belong in a separate integration-style
suite. Everything here operates on FrameSample/ClipResult objects built
by hand, so it runs fast and needs nothing on disk.
"""

import pytest

from analyzer import (
    ClipResult,
    FrameSample,
    Segment,
    WEIGHT_EXPOSURE,
    WEIGHT_SHARPNESS,
    WEIGHT_STABILITY,
    SAMPLE_INTERVAL_SEC,
    _composite,
    _normalize,
    rescore_clip,
)


def make_result(n_samples, duration=None, **overrides):
    """A minimal ClipResult with `n_samples` FrameSamples spaced
    SAMPLE_INTERVAL_SEC apart, starting at t=0 -- matching the real
    spacing analyze_clip() produces. `duration` defaults to just past
    the last sample's own interval, same as analyze_clip's real
    frame_idx/fps-derived duration."""
    samples = [
        FrameSample(time_sec=i * SAMPLE_INTERVAL_SEC, sharpness=0.0,
                    exposure=0.0, motion_mag=0.0, motion_jitter=0.0)
        for i in range(n_samples)
    ]
    if duration is None:
        duration = n_samples * SAMPLE_INTERVAL_SEC
    result = ClipResult(path="fake.mp4", filename="fake.mp4", duration=duration,
                         fps=24.0, width=1920, height=1080, samples=samples)
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


# ---------------------------------------------------------------------
# _normalize
# ---------------------------------------------------------------------

class TestNormalize:
    def test_midpoint_scales_to_fifty(self):
        assert _normalize(5, 0, 10) == pytest.approx(50.0)

    def test_clamps_below_range_to_zero(self):
        assert _normalize(-100, 0, 10) == 0.0

    def test_clamps_above_range_to_hundred(self):
        assert _normalize(1000, 0, 10) == 100.0

    def test_degenerate_range_returns_fifty(self):
        # hi <= lo has no meaningful scale -- documented fallback.
        assert _normalize(42, 5, 5) == 50.0
        assert _normalize(42, 10, 5) == 50.0


# ---------------------------------------------------------------------
# _composite
# ---------------------------------------------------------------------

class TestComposite:
    def test_weights_match_module_constants_with_no_stability_set(self):
        # A bare FrameSample has no dynamic stability_score attribute
        # (that's only added by _score_clip) -- _composite must fall
        # back to the documented default of 50.0 rather than raising.
        sample = FrameSample(time_sec=0.0, sharpness=100.0, exposure=0.0,
                              motion_mag=0.0, motion_jitter=0.0)
        expected = 100.0 * WEIGHT_SHARPNESS + 0.0 * WEIGHT_EXPOSURE + 50.0 * WEIGHT_STABILITY
        assert _composite(sample, energy_weight=0.0) == pytest.approx(expected)

    def test_zero_energy_weight_ignores_energy_entirely(self):
        sample = FrameSample(time_sec=0.0, sharpness=80.0, exposure=80.0,
                              motion_mag=0.0, motion_jitter=0.0, energy=0.0)
        sample.__dict__["stability_score"] = 80.0
        technical = 80.0 * WEIGHT_SHARPNESS + 80.0 * WEIGHT_EXPOSURE + 80.0 * WEIGHT_STABILITY
        assert _composite(sample, energy_weight=0.0) == pytest.approx(technical)

    def test_energy_weight_blends_technical_and_energy(self):
        sample = FrameSample(time_sec=0.0, sharpness=0.0, exposure=0.0,
                              motion_mag=0.0, motion_jitter=0.0, energy=100.0)
        sample.__dict__["stability_score"] = 0.0
        # technical == 0 for this sample, so at energy_weight=0.5 the
        # composite should be exactly half of the energy score.
        assert _composite(sample, energy_weight=0.5) == pytest.approx(50.0)
        # energy_weight=1.0 -> pure energy score.
        assert _composite(sample, energy_weight=1.0) == pytest.approx(100.0)


# ---------------------------------------------------------------------
# _score_clip / rescore_clip -- segment selection
# ---------------------------------------------------------------------

class TestSegmentSelectionShortClips:
    def test_single_sample_yields_one_full_duration_segment(self):
        result = make_result(1, duration=12.0)
        result.samples[0].sharpness = 90.0
        result.samples[0].exposure = 90.0
        rescore_clip(result, window_sec=4.0, max_segments=3, enable_energy=False)
        assert len(result.segments) == 1
        assert result.segments[0].start == 0.0
        assert result.segments[0].end == 12.0
        assert result.overall_score == pytest.approx(result.best_window_score)

    def test_duration_within_window_yields_one_full_duration_segment(self):
        # 4 samples spanning 2s, well under a 4s window -- nothing to
        # slide a window across, so the whole clip is "the segment".
        result = make_result(4, duration=2.0)
        for s in result.samples:
            s.sharpness = 70.0
            s.exposure = 70.0
        rescore_clip(result, window_sec=4.0, max_segments=1, enable_energy=False)
        assert len(result.segments) == 1
        assert result.segments[0].start == 0.0
        assert result.segments[0].end == 2.0


class TestSegmentSelectionLongClips:
    def _clip_with_one_peak(self):
        # 20 samples, 0.5s apart -> 10s clip. Exposure/motion held
        # constant everywhere so only sharpness drives the score;
        # samples 9-14 (t=4.5..7.0) are a clear "good" region.
        result = make_result(20, duration=10.0)
        for i, s in enumerate(result.samples):
            s.exposure = 100.0
            s.motion_jitter = 0.0
            s.sharpness = 90.0 if 9 <= i <= 14 else 20.0
        return result

    def test_best_window_lands_inside_the_high_scoring_region(self):
        result = self._clip_with_one_peak()
        rescore_clip(result, window_sec=2.0, max_segments=1, enable_energy=False)
        assert len(result.segments) == 1
        seg = result.segments[0]
        # The chosen window must fall entirely within the peak region
        # (t=4.5 to 7.0) -- not just overlap it.
        assert seg.start >= 4.5
        assert seg.end <= 7.5
        # Every sample in the peak scores 90*0.40+100*0.25+100*0.35=96.
        assert result.best_window_score == pytest.approx(96.0)

    def test_overall_score_is_plain_mean_of_all_sample_scores(self):
        result = self._clip_with_one_peak()
        rescore_clip(result, window_sec=2.0, max_segments=1, enable_energy=False)
        low_score = 20.0 * WEIGHT_SHARPNESS + 100.0 * WEIGHT_EXPOSURE + 100.0 * WEIGHT_STABILITY
        high_score = 90.0 * WEIGHT_SHARPNESS + 100.0 * WEIGHT_EXPOSURE + 100.0 * WEIGHT_STABILITY
        expected_mean = (14 * low_score + 6 * high_score) / 20
        assert result.overall_score == pytest.approx(expected_mean)

    def test_two_distinct_peaks_yield_two_nonoverlapping_chronological_segments(self):
        # 30 samples (15s). Two separated high-scoring regions with a
        # long mediocre stretch between and around them.
        result = make_result(30, duration=15.0)
        for i, s in enumerate(result.samples):
            s.exposure = 100.0
            s.motion_jitter = 0.0
            if 2 <= i <= 6:          # t=1.0..3.5
                s.sharpness = 95.0
            elif 20 <= i <= 24:      # t=10.0..12.5
                s.sharpness = 95.0
            else:
                s.sharpness = 10.0
        rescore_clip(result, window_sec=2.0, max_segments=2, enable_energy=False)

        assert len(result.segments) == 2
        first, second = result.segments
        # Chronological order.
        assert first.start < second.start
        # Non-overlapping, with at least the 1.0s default gap between them.
        assert second.start - first.end >= 1.0
        # Each one actually lands in one of the two peak regions.
        assert first.end <= 4.5
        assert second.start >= 9.5
        # Both segments must genuinely outscore the clip's own average --
        # that's the rule that keeps low-quality clips from being padded.
        for seg in result.segments:
            assert seg.score > result.overall_score

    def test_uniform_clip_is_not_padded_to_requested_segment_count(self):
        # Every sample scores identically -- no candidate window can be
        # "genuinely better than the clip's own average", so even
        # though max_segments=3 is requested, only the single best
        # (tied) segment should be kept.
        result = make_result(20, duration=10.0)
        for s in result.samples:
            s.sharpness = 50.0
            s.exposure = 50.0
            s.motion_jitter = 0.0
        rescore_clip(result, window_sec=2.0, max_segments=3, enable_energy=False)
        assert len(result.segments) == 1

    def test_segment_end_is_clamped_to_clip_duration(self):
        # duration deliberately shorter than the last sample's own
        # SAMPLE_INTERVAL_SEC window would naively extend to.
        result = make_result(10, duration=4.6)  # last sample at t=4.5
        for i, s in enumerate(result.samples):
            s.exposure = 100.0
            s.motion_jitter = 0.0
            s.sharpness = 90.0 if i >= 7 else 20.0  # peak right at the tail
        rescore_clip(result, window_sec=2.0, max_segments=1, enable_energy=False)
        for seg in result.segments:
            assert seg.end <= result.duration
            assert seg.start >= 0.0


class TestEnergyWeightGating:
    """Covers analyzer.rescore_clip's `apply_energy = enable_energy and
    result.energy_enabled` rule -- energy must only affect the score
    when BOTH the cached samples actually contain energy data AND the
    current run wants it applied. This is the analyzer-level mechanism
    app.py's UI-facing _energy_active() depends on."""

    def _clip_with_energy_gap(self):
        # Uniform zero jitter across every sample makes _score_clip's
        # dynamic stability_score come out to exactly 100 for all of
        # them (see TestSegmentSelectionLongClips._clip_with_one_peak
        # for the same trick). Combined with sharpness=exposure=50,
        # the technical composite is a fixed, easy-to-check constant.
        # Energy is pinned to 100 so any leakage into the score is
        # unambiguous.
        result = make_result(4, duration=2.0, energy_enabled=True)
        for s in result.samples:
            s.sharpness = 50.0
            s.exposure = 50.0
            s.motion_jitter = 0.0
            s.energy = 100.0
        return result

    def test_disabled_for_this_run_ignores_cached_energy_data(self):
        result = self._clip_with_energy_gap()
        rescore_clip(result, window_sec=4.0, max_segments=1,
                     energy_weight=0.9, enable_energy=False)
        # enable_energy=False -> effective weight is 0 regardless of
        # result.energy_enabled or the energy_weight argument.
        technical = 50.0 * WEIGHT_SHARPNESS + 50.0 * WEIGHT_EXPOSURE + 100.0 * WEIGHT_STABILITY
        assert result.overall_score == pytest.approx(technical)

    def test_enabled_for_run_but_no_cached_energy_data_is_ignored(self):
        result = self._clip_with_energy_gap()
        result.energy_enabled = False  # samples were never actually scored for energy
        rescore_clip(result, window_sec=4.0, max_segments=1,
                     energy_weight=0.9, enable_energy=True)
        technical = 50.0 * WEIGHT_SHARPNESS + 50.0 * WEIGHT_EXPOSURE + 100.0 * WEIGHT_STABILITY
        assert result.overall_score == pytest.approx(technical)

    def test_enabled_for_run_and_data_present_applies_the_blend(self):
        result = self._clip_with_energy_gap()
        rescore_clip(result, window_sec=4.0, max_segments=1,
                     energy_weight=0.9, enable_energy=True)
        technical = 50.0 * WEIGHT_SHARPNESS + 50.0 * WEIGHT_EXPOSURE + 100.0 * WEIGHT_STABILITY
        expected = technical * 0.1 + 100.0 * 0.9
        assert result.overall_score == pytest.approx(expected)

    def test_mean_energy_score_recomputed_even_when_not_applied_to_scoring(self):
        # rescore_clip updates mean_energy_score purely for display
        # whenever result.energy_enabled is True, independent of
        # whether `enable_energy` applied it to the composite score.
        result = self._clip_with_energy_gap()
        rescore_clip(result, window_sec=4.0, max_segments=1,
                     energy_weight=0.9, enable_energy=False)
        assert result.mean_energy_score == pytest.approx(100.0)
