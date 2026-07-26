"""
tests/test_prompt_ai.py

Unit tests for prompt_ai.py's interpret() -- the local, deterministic
keyword/regex parser standing in for a hosted LLM (see the module's own
"SECURITY NOTE"). Covers each keyword category it recognizes and the
"leave unmentioned fields untouched" contract that lets follow-up
prompts layer on top of a previous result.
"""

import pytest

import brand
import prompt_ai


def _base_scene():
    preset = dict(brand.PRESETS[brand.DEFAULT_PRESET])
    preset.setdefault("title", "COMMENCEMENT 2026")
    preset.setdefault("subtitle", "Blair Academy")
    preset.setdefault("layout", brand.DEFAULT_LAYOUT)
    preset.setdefault("logo", "Blair Seal (crest)")
    preset.setdefault("logo_placement", "bottom-center")
    preset.setdefault("uppercase_title", True)
    preset.setdefault("divider", True)
    preset.setdefault("transparent_bg", True)
    preset.setdefault("vignette", 0)
    return preset


def test_interpret_returns_new_dict_not_mutated_in_place():
    base = _base_scene()
    base_copy = dict(base)
    scene, notes = prompt_ai.interpret("upbeat, orange background", base)
    assert base == base_copy  # original untouched
    assert scene is not base


def test_interpret_unmentioned_fields_left_untouched():
    base = _base_scene()
    base["title"] = "MY CUSTOM TITLE"
    scene, _ = prompt_ai.interpret("bounce animation", base)
    assert scene["title"] == "MY CUSTOM TITLE"


def test_interpret_explicit_title_and_subtitle_text():
    scene, notes = prompt_ai.interpret('title: "Welcome Home" subtitle: "Alumni Weekend"', _base_scene())
    assert scene["title"] == "Welcome Home"
    assert scene["subtitle"] == "Alumni Weekend"


def test_interpret_mood_applies_preset():
    scene, notes = prompt_ai.interpret("make it upbeat and fun and playful", _base_scene())
    assert scene["bg_color"] == brand.PRESETS["Pop & Upbeat"]["bg_color"]
    assert any("Pop & Upbeat" in n for n in notes)


def test_interpret_picks_mood_with_most_keyword_hits():
    # "athletics"+"game"+"team" (3 hits) should beat "community" (1 hit).
    scene, _ = prompt_ai.interpret("athletics game for the team, welcome community", _base_scene())
    assert scene["bg_color"] == brand.PRESETS["Athletics / High Energy"]["bg_color"]


def test_interpret_aspect_ratio_keywords():
    scene, notes = prompt_ai.interpret("make this for instagram stories", _base_scene())
    assert scene["canvas_preset_name"] == "9:16 Vertical (Stories / Reels / TikTok)"
    assert scene["canvas_size"] == brand.CANVAS_PRESETS["9:16 Vertical (Stories / Reels / TikTok)"]


def test_interpret_layout_lower_third():
    scene, _ = prompt_ai.interpret("use a lower third instead", _base_scene())
    assert scene["layout"] == "Lower Third"


def test_interpret_lower_third_position():
    base = _base_scene()
    base["layout"] = "Lower Third"
    scene, _ = prompt_ai.interpret("put it in the top right", base)
    assert scene["lower_third_position"] == "Top Right"


def test_interpret_lower_third_explicit_percentage():
    base = _base_scene()
    base["layout"] = "Lower Third"
    scene, notes = prompt_ai.interpret("lower third at 150%", base)
    assert scene["lower_third_scale"] == pytest.approx(1.5)


def test_interpret_lower_third_percentage_is_clamped():
    base = _base_scene()
    base["layout"] = "Lower Third"
    scene, _ = prompt_ai.interpret("lower third at 999%", base)
    assert scene["lower_third_scale"] == pytest.approx(1.8)  # clamped to max


def test_interpret_background_color_phrase():
    scene, notes = prompt_ai.interpret("orange background", _base_scene())
    assert scene["bg_color"] == "#f15d22"
    assert scene["transparent_bg"] is False


def test_interpret_transparent_background_overrides_color():
    scene, _ = prompt_ai.interpret("transparent background", _base_scene())
    assert scene["transparent_bg"] is True


def test_interpret_prefers_longer_color_phrase_match():
    # "burnt orange" (12 chars) should win over the shorter "orange" (6 chars)
    # substring match when both appear in the color phrase.
    scene, _ = prompt_ai.interpret("burnt orange background", _base_scene())
    assert scene["bg_color"] == "#c6671d"


def test_interpret_accent_and_text_color():
    scene, _ = prompt_ai.interpret("yellow accent, red text", _base_scene())
    assert scene["accent_color"] == "#dd971a"
    assert scene["text_color"] == "#da1a32"


def test_interpret_animation_keywords():
    for phrase, expected in [
        ("bounce it in", "bounce"),
        ("zoom in", "zoom"),
        ("wipe reveal", "wipe"),
        ("letter cascade", "stagger"),
        ("typewriter effect", "typewriter"),
        ("slide in", "slide"),
        ("fade in gently", "fade"),
        ("keep it static", "none"),
    ]:
        scene, _ = prompt_ai.interpret(phrase, _base_scene())
        assert scene["animation"] == expected, phrase


def test_interpret_outro_keywords():
    scene, _ = prompt_ai.interpret("slide out at the end", _base_scene())
    assert scene["outro_animation"] == "slide"


@pytest.mark.parametrize("phrase", [
    "make it 7 seconds long",  # plural -- previously silently ignored, see DURATION_RE
    "make it 7 second long",
    "make it 7 secs long",
    "make it 7 sec long",
    "make it 7s long",
])
def test_interpret_duration_explicit_seconds(phrase):
    scene, notes = prompt_ai.interpret(phrase, _base_scene())
    assert scene["duration"] == pytest.approx(7.0)
    assert any("Duration set to 7" in n for n in notes)


def test_interpret_duration_slow_and_fast_keywords():
    base = _base_scene()
    base["duration"] = 3.0
    slow_scene, _ = prompt_ai.interpret("make it slower and more gentle", base)
    assert slow_scene["duration"] == 5.0

    fast_scene, _ = prompt_ai.interpret("make it fast and snappy", base)
    assert fast_scene["duration"] == 2.0


def test_interpret_logo_keywords():
    scene, _ = prompt_ai.interpret("use the plain seal crest", _base_scene())
    assert scene["logo"] == "Blair Seal (crest)"


def test_interpret_logo_keywords_specific_beats_generic():
    # "ribbon" is specific to the ribbon lockup; the plain crest's own
    # keywords ("seal"/"crest") are generic enough to appear in this same
    # phrase, so the more specific match must win, not whichever entry
    # happens to be checked first.
    scene, _ = prompt_ai.interpret("use the ribbon seal", _base_scene())
    assert scene["logo"] == "Blair Seal + Ribbon"


def test_interpret_no_logo():
    scene, notes = prompt_ai.interpret("no logo please", _base_scene())
    assert scene["logo"] == "None"


def test_interpret_logo_placement():
    scene, _ = prompt_ai.interpret("seal centered at the bottom", _base_scene())
    assert scene["logo_placement"] == "bottom-center"


def test_interpret_logo_color_mode_white_knockout():
    scene, _ = prompt_ai.interpret("white knockout logo", _base_scene())
    assert scene["logo_color_mode"] == "white"


def test_interpret_case_keywords():
    upper_scene, _ = prompt_ai.interpret("all caps title", _base_scene())
    assert upper_scene["uppercase_title"] is True

    lower_scene, _ = prompt_ai.interpret("use sentence case", _base_scene())
    assert lower_scene["uppercase_title"] is False


def test_interpret_divider_on_off():
    off_scene, _ = prompt_ai.interpret("no divider please", _base_scene())
    assert off_scene["divider"] is False

    on_scene, _ = prompt_ai.interpret("with a line under the title", _base_scene())
    assert on_scene["divider"] is True


def test_interpret_vignette_explicit_percentage():
    scene, notes = prompt_ai.interpret("vignette at 40%", _base_scene())
    assert scene["vignette"] == 40


def test_interpret_vignette_keyword_defaults_to_50():
    scene, _ = prompt_ai.interpret("add moody edges", _base_scene())
    assert scene["vignette"] == 50


def test_interpret_vignette_shape_keywords():
    scene, _ = prompt_ai.interpret("circular vignette / spotlight", _base_scene())
    assert scene["vignette_shape"] == "Circular"


def test_interpret_no_recognized_keywords_returns_helpful_note():
    scene, notes = prompt_ai.interpret("asdkjaslkdj random gibberish text", _base_scene())
    assert len(notes) == 1
    assert "No recognized design keywords" in notes[0]


def test_interpret_full_example_from_module_docstring():
    prompt = ('clean elegant title, blue background, seal centered at the bottom, '
              'slow fade in, subtitle in italics')
    scene, notes = prompt_ai.interpret(prompt, _base_scene())
    assert scene["bg_color"] == "#004b8d"  # "blue background" (== the preset's own color too)
    assert scene["logo_placement"] == "bottom-center"
    assert scene["animation"] == "fade"
    assert scene["duration"] == 5.0  # "slow" bumps duration up
