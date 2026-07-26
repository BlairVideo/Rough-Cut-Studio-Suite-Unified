"""
tests/test_brand.py

Unit tests for brand.py's pure color-math helpers (the single source of
truth for hex parsing per the app's CLAUDE.md) and font_path resolution.
"""

import os

import pytest

import brand


def test_hex_to_rgb():
    assert brand.hex_to_rgb("#004b8d") == (0x00, 0x4b, 0x8d)
    assert brand.hex_to_rgb("ffffff") == (255, 255, 255)  # leading '#' optional
    assert brand.hex_to_rgb("#000000") == (0, 0, 0)


def test_relative_luminance_white_is_one_black_is_zero():
    assert brand.relative_luminance("#ffffff") == pytest.approx(1.0, abs=1e-6)
    assert brand.relative_luminance("#000000") == pytest.approx(0.0, abs=1e-6)


def test_contrast_ratio_black_on_white_is_max():
    assert brand.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)


def test_contrast_ratio_identical_colors_is_one():
    assert brand.contrast_ratio("#004b8d", "#004b8d") == pytest.approx(1.0, abs=1e-6)


def test_contrast_ratio_symmetric():
    a = brand.contrast_ratio("#004b8d", "#ffffff")
    b = brand.contrast_ratio("#ffffff", "#004b8d")
    assert a == pytest.approx(b)


def test_font_path_resolves_regular_and_italic():
    regular = brand.font_path("Modern Sans (clean body)", "regular")
    italic = brand.font_path("Modern Sans (clean body)", "italic")
    assert regular.endswith("AvenirNextLTPro-Regular.otf")
    assert italic.endswith("AvenirNextLTPro-It.otf")
    assert os.path.isfile(regular)
    assert os.path.isfile(italic)


def test_font_path_unknown_style_falls_back_to_regular():
    # Bree Serif has the same file for both regular/italic, but this
    # checks the general fallback behavior via .get(style, entry["regular"]).
    path = brand.font_path("Modern Sans (clean body)", "nonexistent-style")
    assert path.endswith("AvenirNextLTPro-Regular.otf")


def test_every_preset_references_a_real_font_and_logo():
    # Every PRESETS entry must resolve to files that actually exist on
    # disk, or default_scene()/render_frame() would fail for that preset.
    for name, preset in brand.PRESETS.items():
        for style in ("regular", "italic"):
            assert os.path.isfile(brand.font_path(preset["title_font"], style)), name
            assert os.path.isfile(brand.font_path(preset["subtitle_font"], style)), name
        logo_file = brand.LOGO_SOURCES[preset["logo"]]
        assert os.path.isfile(os.path.join(brand.ASSET_DIR, logo_file)), name


def test_all_colors_are_valid_hex():
    for name, value in brand.ALL_COLORS.items():
        assert value.startswith("#") and len(value) == 7, name
        brand.hex_to_rgb(value)  # raises if unparseable
