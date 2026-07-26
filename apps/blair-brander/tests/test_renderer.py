"""
tests/test_renderer.py

Unit tests for renderer.py: the pure easing/geometry helpers, plus a
handful of render_frame()/render_still() integration smoke tests run
against the real, checked-in fonts/assets (same fixtures the app itself
ships with -- no synthetic/mocked media involved, matching how this
module was manually sanity-checked before, per the app's own CLAUDE.md).
"""

import pytest
from PIL import Image

import brand
import renderer
from app import default_scene


# ---------------------------------------------------------------------------
# Easing / geometry helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn", [
    renderer.ease_out_cubic, renderer.ease_out_back,
    renderer.ease_in_out_cubic, renderer.ease_in_cubic,
])
def test_easing_functions_start_and_end_points(fn):
    assert fn(0.0) == pytest.approx(0.0, abs=1e-6)
    assert fn(1.0) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("fn", [
    renderer.ease_out_cubic, renderer.ease_in_out_cubic, renderer.ease_in_cubic,
])
def test_easing_functions_clamp_outside_unit_range(fn):
    assert fn(-5.0) == pytest.approx(fn(0.0))
    assert fn(5.0) == pytest.approx(fn(1.0))


def test_clamp01():
    assert renderer.clamp01(-1.0) == 0.0
    assert renderer.clamp01(2.0) == 1.0
    assert renderer.clamp01(0.5) == 0.5


def test_phase_progress_basic():
    assert renderer.phase_progress(0.0, 0.5, 0.0) == 0.0
    assert renderer.phase_progress(0.0, 0.5, 0.25) == pytest.approx(0.5)
    assert renderer.phase_progress(0.0, 0.5, 1.0) == 1.0


def test_phase_progress_degenerate_range_is_always_done():
    # end <= start is a degenerate/instant phase -- always fully progressed.
    assert renderer.phase_progress(0.5, 0.5, 0.0) == 1.0
    assert renderer.phase_progress(0.8, 0.2, 0.5) == 1.0


def test_hex_to_rgba():
    assert renderer.hex_to_rgba("#004b8d", alpha=128) == (0x00, 0x4b, 0x8d, 128)
    assert renderer.hex_to_rgba("#ffffff") == (255, 255, 255, 255)


def test_darken():
    assert renderer.darken("#ffffff", factor=0.5) == "#7f7f7f"
    assert renderer.darken("#000000", factor=0.5) == "#000000"


def test_compute_outro_none_style_is_fully_visible():
    assert renderer.compute_outro("none", 0.5) == (1.0, 0.0, 1.0)


def test_compute_outro_zero_progress_is_fully_visible_regardless_of_style():
    assert renderer.compute_outro("fade", 0.0) == (1.0, 0.0, 1.0)


def test_compute_outro_fade_fully_exited_is_invisible():
    alpha, dx, scale = renderer.compute_outro("fade", 1.0)
    assert alpha == pytest.approx(0.0, abs=1e-6)
    assert dx == 0.0
    assert scale == 1.0


def test_compute_outro_slide_moves_and_fades():
    alpha, dx, scale = renderer.compute_outro("slide", 1.0)
    assert alpha == pytest.approx(0.0, abs=1e-6)
    assert dx > 0


def test_compute_outro_zoom_shrinks():
    alpha, dx, scale = renderer.compute_outro("zoom", 1.0)
    assert scale < 1.0


def test_compute_outro_wipe_keeps_alpha_scale_unchanged():
    # wipe is handled via a reveal mask elsewhere, not alpha/scale here.
    alpha, dx, scale = renderer.compute_outro("wipe", 1.0)
    assert (alpha, dx, scale) == (1.0, 0.0, 1.0)


# ---------------------------------------------------------------------------
# plateau_t
# ---------------------------------------------------------------------------

def test_plateau_t_no_outro_is_one():
    scene = default_scene()
    scene["outro_animation"] = "none"
    assert renderer.plateau_t(scene) == 1.0


def test_plateau_t_with_outro_is_between_ins_end_and_outs_start():
    scene = default_scene()
    scene["outro_animation"] = "fade"
    scene["title_in_end"] = 0.4
    scene["title_out_start"] = 0.8
    scene["subtitle_in_end"] = 0.3
    scene["subtitle_out_start"] = 0.9
    scene["logo"] = "None"  # exclude logo timing from the max/min
    t = renderer.plateau_t(scene)
    assert 0.4 <= t <= 0.9


# ---------------------------------------------------------------------------
# render_frame / render_still -- integration smoke tests against real
# checked-in fonts/assets (no mocks; same fixtures the app ships with).
# ---------------------------------------------------------------------------

def test_render_still_returns_rgba_image_at_canvas_size():
    scene = default_scene()
    img = renderer.render_still(scene)
    assert isinstance(img, Image.Image)
    assert img.mode == "RGBA"
    assert img.size == tuple(scene["canvas_size"])


def test_render_frame_transparent_background_has_zero_alpha_corners():
    scene = default_scene()
    scene["transparent_bg"] = True
    img = renderer.render_frame(scene, t=0.0)
    corner = img.getpixel((0, 0))
    assert corner[3] == 0  # fully transparent alpha


def test_render_frame_solid_background_fills_corner_with_bg_color():
    scene = default_scene()
    scene["transparent_bg"] = False
    scene["background_style"] = "Solid"
    scene["bg_color"] = "#004b8d"
    scene["logo"] = "None"
    img = renderer.render_frame(scene, t=0.0)
    corner = img.getpixel((0, 0))
    assert corner[:3] == (0x00, 0x4b, 0x8d)
    assert corner[3] == 255


def test_render_frame_at_t_zero_title_not_yet_visible_for_fade():
    scene = default_scene()
    scene["animation"] = "fade"
    scene["title_in_start"] = 0.0
    scene["title_in_end"] = 0.5
    img_start = renderer.render_frame(scene, t=0.0)
    img_settled = renderer.render_still(scene)
    # Fully faded-out should differ from fully settled (some pixel changed).
    assert img_start.tobytes() != img_settled.tobytes()


@pytest.mark.parametrize("anim", ["fade", "slide", "zoom", "bounce", "wipe", "stagger", "typewriter", "none"])
def test_render_frame_every_animation_style_runs_without_error(anim):
    scene = default_scene()
    scene["animation"] = anim
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        img = renderer.render_frame(scene, t=t)
        assert img.size == tuple(scene["canvas_size"])


@pytest.mark.parametrize("outro", ["fade", "slide", "zoom", "wipe", "none"])
def test_render_frame_every_outro_style_runs_without_error(outro):
    scene = default_scene()
    scene["outro_animation"] = outro
    for t in (0.7, 0.85, 1.0):
        img = renderer.render_frame(scene, t=t)
        assert img.size == tuple(scene["canvas_size"])


def test_render_frame_lower_third_layout_runs_without_error():
    scene = default_scene()
    scene["layout"] = "Lower Third"
    img = renderer.render_still(scene)
    assert img.size == tuple(scene["canvas_size"])


@pytest.mark.parametrize("preset_name", list(brand.PRESETS.keys()))
def test_render_still_every_preset_runs_without_error(preset_name):
    scene = default_scene()
    scene.update(brand.PRESETS[preset_name])
    img = renderer.render_still(scene)
    assert img.size == tuple(scene["canvas_size"])


# ---------------------------------------------------------------------------
# Drop shadow
# ---------------------------------------------------------------------------

def test_drop_shadow_layer_none_when_source_fully_transparent():
    layer = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    assert renderer.drop_shadow_layer(layer, 100, 100, 4, 4, 8, "#000000", 60) is None


def test_drop_shadow_layer_none_when_opacity_zero():
    layer = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
    assert renderer.drop_shadow_layer(layer, 100, 100, 4, 4, 8, "#000000", 0) is None


def test_drop_shadow_layer_paints_shadow_color_at_offset():
    layer = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    layer.paste((255, 255, 255, 255), (10, 10, 20, 20))
    shadow = renderer.drop_shadow_layer(layer, 100, 100, 5, 5, 0, "#ff0000", 100)
    assert shadow is not None
    # Unblurred, offset by (5, 5): a source pixel at (15, 15) lands at (20, 20).
    px = shadow.getpixel((20, 20))
    assert px[:3] == (255, 0, 0)
    assert px[3] == 255


def test_drop_shadow_layer_opacity_scales_alpha():
    layer = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    layer.paste((255, 255, 255, 255), (10, 10, 90, 90))
    shadow = renderer.drop_shadow_layer(layer, 100, 100, 0, 0, 0, "#000000", 50)
    assert shadow.getpixel((50, 50))[3] == pytest.approx(127, abs=1)


def test_render_frame_shadow_enabled_runs_without_error_and_darkens_pixel():
    scene = default_scene()
    scene["logo"] = "None"
    scene["shadow_enabled"] = False
    plain = renderer.render_still(scene)

    scene["shadow_enabled"] = True
    scene["shadow_color"] = "#000000"
    scene["shadow_opacity"] = 100
    scene["shadow_blur"] = 0
    scene["shadow_offset_x"] = 6
    scene["shadow_offset_y"] = 6
    shadowed = renderer.render_still(scene)

    assert shadowed.size == plain.size
    assert shadowed.tobytes() != plain.tobytes()


def test_render_frame_shadow_disabled_is_default():
    scene = default_scene()
    assert scene["shadow_enabled"] is False
