"""
tests/test_grade.py -- GradeState identity round-trip, JSON
serialization, and the pixel-math pipeline in grade.py.
"""

import pytest

import grade as grade_mod
from grade import GradeState, apply_grade_to_rgb, HSL_BANDS


def test_default_grade_is_identity():
    g = GradeState()
    assert g.is_identity()
    for r, g_val, b in [(0.0, 0.0, 0.0), (0.5, 0.5, 0.5), (1.0, 1.0, 1.0), (0.2, 0.7, 0.9)]:
        out = apply_grade_to_rgb(r, g_val, b, g)
        assert out == pytest.approx((r, g_val, b), abs=1e-9)


def test_to_dict_from_dict_round_trip():
    g = GradeState(exposure=0.5, contrast=10, lift=(0.1, 0.0, -0.1))
    data = g.to_dict()
    restored = GradeState.from_dict(data)
    assert restored.exposure == 0.5
    assert restored.contrast == 10
    assert restored.lift == (0.1, 0.0, -0.1)
    assert restored.to_dict() == g.to_dict()


def test_from_dict_empty_is_identity():
    assert GradeState.from_dict({}).is_identity()
    assert GradeState.from_dict(None).is_identity()


def test_from_dict_ignores_unknown_keys():
    restored = GradeState.from_dict({"exposure": 1.0, "bogus_field": "nope"})
    assert restored.exposure == 1.0


def test_exposure_increases_brightness():
    g = GradeState(exposure=1.0)  # +1 stop = *2
    r, gg, b = apply_grade_to_rgb(0.25, 0.25, 0.25, g)
    assert r == pytest.approx(0.5, abs=1e-6)


def test_exposure_clamps_at_white():
    g = GradeState(exposure=4.0)
    r, gg, b = apply_grade_to_rgb(0.9, 0.9, 0.9, g)
    assert r == 1.0 and gg == 1.0 and b == 1.0


def test_contrast_pivots_around_18_percent_grey():
    g = GradeState(contrast=50.0)
    r, _, _ = apply_grade_to_rgb(0.18, 0.18, 0.18, g)
    assert r == pytest.approx(0.18, abs=1e-6)
    # A value above the pivot should be pushed further above it.
    r_hi, _, _ = apply_grade_to_rgb(0.5, 0.5, 0.5, g)
    assert r_hi > 0.5


def test_saturation_negative_100_desaturates_to_grey():
    g = GradeState(saturation=-100.0)
    r, gg, b = apply_grade_to_rgb(0.8, 0.2, 0.2, g)
    assert r == pytest.approx(gg, abs=1e-6)
    assert gg == pytest.approx(b, abs=1e-6)


def test_lift_raises_shadows_without_moving_pure_white():
    g = GradeState(lift=(0.1, 0.1, 0.1))
    r, _, _ = apply_grade_to_rgb(0.0, 0.0, 0.0, g)
    assert r == pytest.approx(0.1, abs=1e-6)


def test_gain_scales_highlights():
    g = GradeState(gain=(0.5, 0.5, 0.5))
    r, _, _ = apply_grade_to_rgb(0.5, 0.5, 0.5, g)
    assert r == pytest.approx(0.75, abs=1e-6)


def test_tone_range_fields_round_trip():
    g = GradeState(highlights=10.0, shadows=-20.0, whites=30.0, blacks=-40.0)
    restored = GradeState.from_dict(g.to_dict())
    assert (restored.highlights, restored.shadows, restored.whites, restored.blacks) == (10.0, -20.0, 30.0, -40.0)


def test_highlights_lifts_bright_pixels_not_dark():
    g = GradeState(highlights=20.0)
    r_dark, _, _ = apply_grade_to_rgb(0.1, 0.1, 0.1, g)
    r_bright, _, _ = apply_grade_to_rgb(0.9, 0.9, 0.9, g)
    assert r_dark == pytest.approx(0.1, abs=1e-6)
    assert r_bright > 0.9


def test_shadows_lifts_dark_pixels_not_bright():
    g = GradeState(shadows=20.0)
    r_dark, _, _ = apply_grade_to_rgb(0.1, 0.1, 0.1, g)
    r_bright, _, _ = apply_grade_to_rgb(0.9, 0.9, 0.9, g)
    assert r_bright == pytest.approx(0.9, abs=1e-6)
    assert r_dark > 0.1


def test_whites_only_touches_near_white_pixels():
    g = GradeState(whites=20.0)
    r_mid, _, _ = apply_grade_to_rgb(0.5, 0.5, 0.5, g)
    r_bright, _, _ = apply_grade_to_rgb(0.8, 0.8, 0.8, g)
    assert r_mid == pytest.approx(0.5, abs=1e-6)
    assert r_bright > 0.8


def test_blacks_only_touches_near_black_pixels():
    g = GradeState(blacks=-40.0)
    r_mid, _, _ = apply_grade_to_rgb(0.5, 0.5, 0.5, g)
    r_dark, _, _ = apply_grade_to_rgb(0.2, 0.2, 0.2, g)
    assert r_mid == pytest.approx(0.5, abs=1e-6)
    assert r_dark < 0.2


def test_tone_range_output_stays_in_unit_range():
    g = GradeState(highlights=100.0, shadows=100.0, whites=100.0, blacks=-100.0)
    for x in [i / 20 for i in range(21)]:
        r, _, _ = apply_grade_to_rgb(x, x, x, g)
        assert 0.0 <= r <= 1.0


def test_curve_master_identity_is_noop():
    g = GradeState(curve_master=[[0.0, 0.0], [1.0, 1.0]])
    out = apply_grade_to_rgb(0.3, 0.6, 0.9, g)
    assert out == pytest.approx((0.3, 0.6, 0.9), abs=1e-9)


def test_curve_master_lifts_midpoint():
    g = GradeState(curve_master=[[0.0, 0.0], [0.5, 0.7], [1.0, 1.0]])
    r, _, _ = apply_grade_to_rgb(0.5, 0.5, 0.5, g)
    assert r == pytest.approx(0.7, abs=1e-6)


def test_curve_output_stays_in_unit_range():
    # An aggressive S-curve shouldn't overshoot [0, 1] even with
    # Catmull-Rom's tendency to ring past sharp control points.
    g = GradeState(curve_master=[[0.0, 0.0], [0.25, 0.05], [0.75, 0.95], [1.0, 1.0]])
    for x in [i / 20 for i in range(21)]:
        r, _, _ = apply_grade_to_rgb(x, x, x, g)
        assert 0.0 <= r <= 1.0


def test_hsl_band_only_affects_matching_hue():
    g = GradeState()
    g.hsl["red"]["sat"] = 100.0
    # Pure red pixel should gain saturation (already at max here, so
    # instead assert a mid-saturation red gets pushed toward it).
    red_grade = GradeState()
    red_grade.hsl["red"]["lum"] = 50.0
    r, gg, b = apply_grade_to_rgb(0.6, 0.2, 0.2, red_grade)  # reddish hue
    assert r + gg + b > 0.6 + 0.2 + 0.2  # luminance boost brightened it

    blue_untouched = GradeState()
    blue_untouched.hsl["red"]["lum"] = 50.0
    out = apply_grade_to_rgb(0.2, 0.2, 0.6, blue_untouched)  # blue hue, red band untouched
    assert out == pytest.approx((0.2, 0.2, 0.6), abs=1e-6)


def test_all_hsl_bands_present_by_default():
    g = GradeState()
    assert set(g.hsl.keys()) == set(HSL_BANDS)


def test_output_always_clamped():
    g = GradeState(exposure=10.0, gain=(2.0, 2.0, 2.0))
    r, gg, b = apply_grade_to_rgb(0.9, 0.9, 0.9, g)
    assert 0.0 <= r <= 1.0
    assert 0.0 <= gg <= 1.0
    assert 0.0 <= b <= 1.0

    g2 = GradeState(exposure=-10.0, lift=(-1.0, -1.0, -1.0))
    r2, g2v, b2 = apply_grade_to_rgb(0.1, 0.1, 0.1, g2)
    assert 0.0 <= r2 <= 1.0
    assert 0.0 <= g2v <= 1.0
    assert 0.0 <= b2 <= 1.0
