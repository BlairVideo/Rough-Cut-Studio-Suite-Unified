"""Unit tests for analyze_clip.py's pure parsing logic -- doesn't load any
ML model, so this runs fast and needs only the stdlib `unittest` runner:

    ./.venv/bin/python -m unittest test_analyze_clip.py -v
"""
import unittest
from unittest import mock

import numpy as np

import analyze_clip
from analyze_clip import (
    TAGS_PROMPT,
    _cosine,
    _extract_onscreen_tokens,
    _looks_like_onscreen_text,
    _matches_onscreen_tokens,
    _parse_tags,
    _resize_to_max_dim,
    _strip_generic_caption_tail,
    caption_hub_score,
)


class TagsPromptTests(unittest.TestCase):
    def test_does_not_hand_the_model_literal_example_tags_to_echo(self):
        # Regression: TAGS_PROMPT used to include "(for example: mascot,
        # cheering, classroom, outdoors)". moondream2 echoed those exact
        # four words back as tags regardless of the image's actual content --
        # confirmed live, each covered 23-25% of a 1622-shot archive. There's
        # no safe deterministic post-hoc filter for this (unlike the digit/
        # gender backstops below) since these are legitimate tag vocabulary,
        # not something that's always wrong -- so this must stay fixed in
        # the prompt text itself.
        self.assertNotIn("for example", TAGS_PROMPT.lower())
        for leaked_example in ("mascot", "cheering", "classroom", "outdoors"):
            self.assertNotIn(leaked_example, TAGS_PROMPT.lower())


class ParseTagsTests(unittest.TestCase):
    def test_splits_and_normalizes_comma_separated_tags(self):
        self.assertEqual(
            _parse_tags("Mascot, Cheering, Outdoors"),
            ["mascot", "cheering", "outdoors"],
        )

    def test_drops_purely_numeric_fragments(self):
        # Observed failure mode: the model echoing "3 to 5" from the
        # prompt back as if it were a tag itself.
        self.assertEqual(_parse_tags("3, 4, 5"), [])

    def test_drops_any_tag_containing_a_digit(self):
        # A tag with a digit almost always means the VLM read on-screen
        # text (a jersey number, a scoreboard score/clock) instead of
        # describing subject matter -- must never become a searchable/
        # filterable tag (private-school data-privacy mandate).
        self.assertEqual(_parse_tags("no23, 42-10, mascot"), ["mascot"])

    def test_drops_single_character_fragments(self):
        self.assertEqual(_parse_tags("a, mascot, b"), ["mascot"])

    def test_dedupes_case_insensitively(self):
        self.assertEqual(_parse_tags("Mascot, mascot, MASCOT"), ["mascot"])

    def test_caps_at_max_tags_per_shot(self):
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"]
        self.assertEqual(len(_parse_tags(", ".join(words))), 6)

    def test_strips_trailing_period_from_last_tag(self):
        self.assertEqual(_parse_tags("mascot, cheering."), ["mascot", "cheering"])

    def test_empty_input_yields_no_tags(self):
        self.assertEqual(_parse_tags(""), [])

    def test_drops_bare_gender_tags(self):
        # A shot must never be tagged/filterable by the sex of the students
        # in it (private-school data-privacy mandate) -- deterministic
        # backstop for TAGS_PROMPT's "don't tag gender" instruction.
        self.assertEqual(_parse_tags("boy, girl, boys, girls, male, female, mascot"), ["mascot"])

    def test_drops_gender_headcount_phrases(self):
        # The digit backstop alone doesn't catch a spelled-out headcount
        # like "two boys" -- must be matched as a whole word within the tag.
        self.assertEqual(_parse_tags("two boys, three girls, cheering"), ["cheering"])

    def test_does_not_false_positive_on_substrings_of_gender_words(self):
        # Word-matched, not substring-matched -- a tag that happens to
        # contain "boy"/"girl" as part of a longer, unrelated word must
        # survive.
        self.assertEqual(_parse_tags("cowboy hat, girlfriend"), ["cowboy hat", "girlfriend"])

    def test_drops_quoted_onscreen_text_tags(self):
        # Regression: moondream2 on screen-recording footage (a flight
        # check-in app) quoted UI text back verbatim instead of describing
        # subject matter -- confirmed live: `"boarding now" button`,
        # `"united" logo`.
        self.assertEqual(
            _parse_tags('"boarding now" button, "united" logo, mascot'),
            ["mascot"],
        )

    def test_drops_tags_ending_in_a_ui_role_word_even_unquoted(self):
        self.assertEqual(_parse_tags("departure banner, cheering"), ["cheering"])

    def test_drops_tags_with_sentence_punctuation(self):
        # A slogan/quote transcribed verbatim carries punctuation no
        # keyword tag needs.
        self.assertEqual(
            _parse_tags("focus on the process... not the result!, mascot"),
            ["mascot"],
        )

    def test_drops_implausibly_long_tags(self):
        self.assertEqual(_parse_tags("watch for your group number, mascot"), ["mascot"])

    def test_keeps_short_legitimate_multiword_tags(self):
        self.assertEqual(_parse_tags("vatican museums, art class"), ["vatican museums", "art class"])

    def test_drops_tags_matching_ocr_extracted_onscreen_tokens(self):
        onscreen = frozenset({"museums", "vatican", "welcome"})
        self.assertEqual(
            _parse_tags("vatican museums, mascot", onscreen_tokens=onscreen),
            ["mascot"],
        )

    def test_does_not_drop_tags_only_partially_overlapping_onscreen_tokens(self):
        # Only some of the tag's content words were seen on screen -- not
        # enough to call the whole tag a text transcription.
        onscreen = frozenset({"museums"})
        self.assertEqual(_parse_tags("vatican museums", onscreen_tokens=onscreen), ["vatican museums"])

    def test_ignores_stopwords_when_matching_onscreen_tokens(self):
        # "the" alone showing up in OCR'd text is a coincidence, not
        # evidence this specific tag was read off the screen.
        onscreen = frozenset({"the", "judgement", "last"})
        self.assertEqual(
            _parse_tags("the last judgement", onscreen_tokens=onscreen),
            [],
        )


class LooksLikeOnscreenTextTests(unittest.TestCase):
    def test_bare_keyword_tag_is_not_onscreen_text(self):
        self.assertFalse(_looks_like_onscreen_text("mascot"))
        self.assertFalse(_looks_like_onscreen_text("vatican museums"))

    def test_curly_quotes_are_also_caught(self):
        self.assertTrue(_looks_like_onscreen_text("“united” logo"))


class MatchesOnscreenTokensTests(unittest.TestCase):
    def test_no_tokens_never_matches(self):
        self.assertFalse(_matches_onscreen_tokens("mascot", frozenset()))

    def test_tag_with_no_content_words_does_not_match(self):
        # Every word is a stopword or too short -- nothing to compare.
        self.assertFalse(_matches_onscreen_tokens("of a", frozenset({"of", "a"})))


class ExtractOnscreenTokensTests(unittest.TestCase):
    def setUp(self):
        self._saved = analyze_clip._tesseract_available
        analyze_clip._tesseract_available = None

    def tearDown(self):
        analyze_clip._tesseract_available = self._saved

    def test_degrades_to_empty_set_when_tesseract_binary_is_missing(self):
        with mock.patch("pytesseract.get_tesseract_version", side_effect=Exception("not found")):
            self.assertEqual(_extract_onscreen_tokens(mock.Mock()), set())

    def test_degrades_to_empty_set_on_ocr_failure(self):
        with mock.patch("pytesseract.get_tesseract_version", return_value="5.0"), \
             mock.patch("pytesseract.image_to_string", side_effect=Exception("boom")):
            self.assertEqual(_extract_onscreen_tokens(mock.Mock()), set())

    def test_lowercases_and_tokenizes_ocr_output(self):
        with mock.patch("pytesseract.get_tesseract_version", return_value="5.0"), \
             mock.patch("pytesseract.image_to_string", return_value="Boarding Now\nGate B12"):
            self.assertEqual(_extract_onscreen_tokens(mock.Mock()), {"boarding", "now", "gate"})


class StripGenericCaptionTailTests(unittest.TestCase):
    # Real captions pulled live from the archive -- this is a confirmed
    # bug fix, not a hypothetical: moondream2's recurring "creating a/an
    # [mood] atmosphere"/"conveying a sense of [mood]" tail made a caption's
    # CLIP text embedding anomalously similar to unrelated queries (a
    # "hub"), and it turned out ~6% of all captions in the archive (97 of
    # 1629) shared some form of this exact template.

    def test_strips_mood_and_atmosphere(self):
        self.assertEqual(
            _strip_generic_caption_tail(
                "A choir of people in black robes performs in a grand church, "
                "creating a harmonious and majestic atmosphere."
            ),
            "A choir of people in black robes performs in a grand church",
        )

    def test_strips_single_word_mood_atmosphere(self):
        self.assertEqual(
            _strip_generic_caption_tail(
                "People gather in a modern lounge, seated on ottomans, "
                "creating a relaxed atmosphere."
            ),
            "People gather in a modern lounge, seated on ottomans",
        )

    def test_strips_sense_of_two_word_phrase(self):
        self.assertEqual(
            _strip_generic_caption_tail(
                "A large group of people gathers in a snowy field, "
                "creating a sense of unity and shared experience."
            ),
            "A large group of people gathers in a snowy field",
        )

    def test_strips_atmosphere_of_variant(self):
        self.assertEqual(
            _strip_generic_caption_tail(
                "A dimly lit auditorium hosts a captivated audience, "
                "creating an atmosphere of anticipation and engagement."
            ),
            "A dimly lit auditorium hosts a captivated audience",
        )

    def test_leaves_real_trailing_content_after_the_clause_alone(self):
        # "against a black background" is actual content, not boilerplate
        # -- must not be swallowed just because "creating a sense of X and
        # Y" appears earlier in the same trailing clause.
        caption = (
            "A group of people walk along a dark path, their silhouettes "
            "creating a sense of depth and movement against a black background."
        )
        self.assertEqual(_strip_generic_caption_tail(caption), caption)

    def test_falls_back_to_original_when_stripping_would_leave_nothing(self):
        caption = "creating a relaxed and inviting atmosphere."
        self.assertEqual(_strip_generic_caption_tail(caption), caption)

    def test_caption_with_no_generic_tail_is_unchanged(self):
        caption = "A player in a blue jersey celebrates on the field."
        self.assertEqual(_strip_generic_caption_tail(caption), caption)


class CaptionHubScoreTests(unittest.TestCase):
    # `caption_hub_score` calls the real CLIP text encoder to embed its
    # anchor battery on first use (`_hub_anchor_vectors_cached`) -- mocking
    # that out keeps this test fast and model-free, exercising only the
    # pure averaging/cosine math the fix actually depends on.

    def test_cosine_of_identical_vectors_is_one(self):
        self.assertAlmostEqual(_cosine([1.0, 0.0], [1.0, 0.0]), 1.0, places=6)

    def test_cosine_of_orthogonal_vectors_is_zero(self):
        self.assertAlmostEqual(_cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)

    def test_averages_similarity_across_every_anchor(self):
        with mock.patch("analyze_clip._hub_anchor_vectors_cached", return_value=[[1.0, 0.0], [0.0, 1.0]]):
            # [0.6, 0.8] has cosine 0.6 against the first anchor and 0.8
            # against the second -- the hub score is their average.
            self.assertAlmostEqual(caption_hub_score([0.6, 0.8]), 0.7, places=6)

    def test_identical_to_every_anchor_scores_one(self):
        anchors = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
        with mock.patch("analyze_clip._hub_anchor_vectors_cached", return_value=anchors):
            self.assertAlmostEqual(caption_hub_score([1.0, 0.0]), 1.0, places=6)

    def test_orthogonal_to_every_anchor_scores_zero(self):
        anchors = [[1.0, 0.0], [1.0, 0.0]]
        with mock.patch("analyze_clip._hub_anchor_vectors_cached", return_value=anchors):
            self.assertAlmostEqual(caption_hub_score([0.0, 1.0]), 0.0, places=6)


class ResizeToMaxDimTests(unittest.TestCase):
    def test_downsizes_the_long_edge_to_max_dim_and_keeps_aspect_ratio(self):
        frame = np.zeros((480, 960, 3), dtype=np.uint8)  # 2:1, long edge 960
        resized = _resize_to_max_dim(frame, 240)
        h, w = resized.shape[:2]
        self.assertEqual(w, 240)
        self.assertEqual(h, 120)

    def test_leaves_a_frame_already_at_or_under_max_dim_unchanged(self):
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        resized = _resize_to_max_dim(frame, 240)
        self.assertEqual(resized.shape, frame.shape)


if __name__ == "__main__":
    unittest.main()
