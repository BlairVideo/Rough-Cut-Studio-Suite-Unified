#!/usr/bin/env python3
"""Shot boundary detection + keyframe extraction + CLIP visual embeddings +
local VLM captioning/tagging for one clip -- the local ML pipeline
Spyglass's Rust core shells out to (Section 2/19 of the architecture plan:
ML/indexing work runs as a local Python sidecar, never inside the Rust core
itself, and never touches a cloud API).

Usage:
    analyze_clip.py <video_path> <keyframe_output_dir>
    analyze_clip.py --serve

The first form analyzes one clip and exits -- CLIP and moondream2 get
reloaded from disk on every invocation, which dominates gap-fill throughput
once there's more than a handful of clips queued. `--serve` instead runs
persistently (mirrors `embed_text_server.py`'s Section 12 pattern): it loads
both models once, prints "ready" to stderr, then answers a stream of
one-JSON-object-per-line requests on stdin --
    {"video_path": "...", "keyframe_dir": "..."}
-- each producing the same response shape `analyze()` returns below (or
`{"error": "message"}` on a per-clip failure, which does not end the
server -- the caller decides whether to keep sending it more clips).

Prints one JSON object to stdout on success:
    {
        "duration_sec": 605.2,
        "frame_rate": 29.97,
        "shots": [
            {
                "start_tc": 0.0,
                "end_tc": 12.4,
                "keyframe_filename": "shot_0000.jpg",
                "embedding": [0.0123, -0.045, ...],
                "caption": "A player in a blue jersey celebrates on the field.",
                "tags": ["player", "celebration", "field", "sports"],
                "caption_embedding": [0.0091, -0.02, ...]
            },
            ...
        ]
    }

`caption`/`tags`/`caption_embedding` are `null`/`[]`/`null` for a shot where
the VLM step itself failed -- a bad caption on one shot shouldn't sink the
whole clip's analysis, and the shot still has its visual embedding either
way (Section 7: tolerant of partial/missing metadata).

On failure of the clip as a whole (corrupt file, unreadable codec, etc.),
prints a one-line error message to stderr and exits non-zero -- the caller
is expected to log and skip this file rather than treat it as fatal
(Section 7).
"""
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np
import pytesseract
import torch
from PIL import Image
from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector

from parent_watchdog import exit_if_parent_dies

# Keyframes are search-result thumbnails, not archival copies -- capped
# resolution keeps the cache footprint small (Section 10: ~480px long edge
# accounts for most of the size control on its own).
KEYFRAME_MAX_DIM = 480
KEYFRAME_JPEG_QUALITY = 82

# ContentDetector's own default min_scene_len is 15 frames (~0.5s @30fps),
# short enough that fast pans, camera flashes, and quick highlight-reel
# cuts in real event footage routinely register as their own "shots" --
# confirmed live: sports/assembly footage indexed dozens of sub-second
# shots per clip, each paying the full per-shot cost (keyframe + CLIP
# embedding + VLM caption) for a span too brief to ever be a useful search
# result or export unit. Passed to ContentDetector as `min_scene_len`
# below (it accepts seconds as a float, not just frames) so the detector
# itself absorbs most of these; `_merge_short_scenes` is the backstop for
# whatever still slips through -- e.g. a trailing scene the detector can't
# extend past the video's actual end.
MIN_SHOT_DURATION_SEC = 1.0

# Fast, widely-cached CLIP checkpoint -- same library B-Roll Analyzer
# already uses locally for its energy scoring (open_clip_torch, openai
# weights), so this doesn't introduce a new download source. The
# "-quickgelu" variant matches the activation function OpenAI's own
# weights were actually trained with (open_clip warns on a mismatch
# otherwise -- this isn't cosmetic, it's the correct architecture for
# this checkpoint). Also used by `embed_text_server.py` for query-time
# embedding -- must stay in the same joint space as what's written here.
CLIP_MODEL_NAME = "ViT-B-32-quickgelu"
CLIP_PRETRAINED = "openai"

# A compact, fully local vision-language model -- open weights downloaded
# once from HuggingFace (same category as CLIP's own weights, or Whisper's
# in the Transcriber), then run entirely on-device. Deliberately NOT the
# `moondream` pip package: that package's client defaults to Moondream's
# *cloud* API and its "local" mode still routes through the same client
# tied to an api_key, which conflicts with this Suite's no-cloud-API
# mandate. Loading the actual open-source checkpoint via `transformers`
# avoids that entirely.
VLM_MODEL_ID = "vikhyatk/moondream2"
VLM_REVISION = "2024-08-26"  # pinned: newer revisions require newer transformers than this venv carries

CAPTION_PROMPT = (
    "Describe what is happening in this shot in one concise sentence, "
    "focusing on the people, action, and setting. Do not mention the "
    "subjects' gender or count how many boys/girls/students are present --"
    " describe the activity, not the people."
)
# Deliberately gives NO literal example tags (e.g. "mascot, cheering,
# classroom, outdoors", this prompt's own wording until this fix) -- an
# earlier version of this prompt spelled those out as illustrative examples,
# and moondream2 (a small VLM, prone to instruction leakage the same way it
# echoed "3 to 5" back as a tag, see `_parse_tags`) echoed those exact four
# words back as tags on shots that had nothing to do with them. Confirmed
# live: those four words alone each covered 23-25% of a 1622-shot archive
# (see spyglass-core's `search.rs::normalized_tag_rarity` comment), not
# because a quarter of the school's footage is actually about mascots. Unlike
# the digit/gender backstops in `_parse_tags`, there's no safe deterministic
# filter for this after the fact -- these are legitimate tag vocabulary, not
# something that's always wrong -- so the fix has to be in the prompt itself.
TAGS_PROMPT = (
    "List 3 to 5 short, comma-separated keyword tags for the specific "
    "objects, activities, and setting actually visible in THIS image. Do "
    "not reuse any word from these instructions as a tag -- describe only "
    "what you observe in the image itself, not a generic category. Do not "
    "tag, quote, or transcribe any text, numbers, names, or scores visible "
    "in the image itself (scoreboards, jerseys, signs, lower-thirds, "
    "captions). "
    "Do not tag or count the subjects' gender (no \"boy\"/\"girl\"/\"boys\"/"
    "\"girls\"/\"male\"/\"female\" tags, and no headcounts like \"two boys\") --"
    " this is a private school archive and tags must describe activities and "
    "settings, not identify or count students by sex. "
    "Respond with only the tags, nothing else."
)
MAX_TAGS_PER_SHOT = 6

# Deterministic backstop for TAGS_PROMPT's gender/headcount instruction,
# same rationale as the digit backstop below: the VLM isn't guaranteed to
# follow the prompt, and letting a tag classify shots by the sex of the
# students in them is exactly what this suite's private-school data-privacy
# mandate rules out, whether or not it also includes an on-screen-text leak.
_GENDER_TAG_WORDS = {"boy", "boys", "girl", "girls", "male", "female", "males", "females"}

# Second deterministic backstop for TAGS_PROMPT's "don't tag on-screen text"
# instruction, alongside the digit filter above -- the digit filter only
# catches text WITH a digit (jersey numbers, scores). It does nothing for
# on-screen text made of ordinary words: confirmed live on screen-recording
# footage (a flight check-in app), where moondream2 -- faced with a frame
# that's almost entirely UI chrome rather than a photographic scene --
# ignored the prompt and read the interface back verbatim as tags:
# `"boarding now" button`, `"united" logo`, `"watch for your group number"
# button`, `focus on the process... not the result!`. These share a
# structural signature no legitimate short keyword tag has: they're wrapped
# in quotes (the model is literally quoting what it read), end in a UI-role
# word, carry sentence punctuation (`!`/`?`/`...`), or simply run far longer
# than a keyword tag ever needs to. `_looks_like_onscreen_text` catches that
# shape without needing OCR ground truth.
_UI_ROLE_WORDS = {
    "button", "logo", "icon", "banner", "menu", "tab", "link", "label",
    "header", "heading", "screen", "app", "interface", "ad", "advertisement",
    "notification", "sign", "signage", "ticket", "price", "watermark",
}
_QUOTE_CHARS = '"“”'  # straight + curly double quotes
MAX_TAG_WORDS = 4  # legitimate tags observed live are 1-3 words ("art class", "vatican museums")

# Stopwords excluded when checking a tag's remaining content words against
# `_extract_onscreen_tokens` below -- otherwise a tag like "the last
# judgement" would need "the" to also appear as an isolated OCR token, which
# is a coincidence, not evidence the tag itself was read off the screen.
_TAG_STOPWORDS = {"a", "an", "the", "and", "or", "of", "in", "on", "for", "to", "not", "your", "with"}


def _looks_like_onscreen_text(tag: str) -> bool:
    if any(ch in tag for ch in _QUOTE_CHARS):
        return True
    if any(ch in tag for ch in "!?") or "..." in tag:
        return True
    words = tag.split()
    if len(words) > MAX_TAG_WORDS:
        return True
    return bool(words) and words[-1] in _UI_ROLE_WORDS


# Deterministic OCR-based backstop, for the on-screen-text tags
# `_looks_like_onscreen_text` can't catch -- a bare-word tag with no quotes,
# punctuation, or UI-role suffix (e.g. a heading or menu item transcribed
# without any of that surrounding structure). Runs local Tesseract OCR
# (same "local binary" pattern as this suite's use of ffmpeg/ExifTool
# elsewhere -- no cloud API) against the same keyframe already being
# captioned, and drops any tag whose remaining content words all appear
# among the text Tesseract actually found on screen. Deliberately
# conservative: only a *whole* tag matching on-screen text is dropped,
# never a single word within an otherwise-legitimate multi-word tag.
_tesseract_available: bool | None = None


def _check_tesseract() -> bool:
    global _tesseract_available
    if _tesseract_available is None:
        try:
            pytesseract.get_tesseract_version()
            _tesseract_available = True
        except Exception:
            print(
                "warning: tesseract OCR binary not found on PATH (brew install "
                "tesseract) -- on-screen-text tag filtering falls back to the "
                "structural backstop only for this run",
                file=sys.stderr,
            )
            _tesseract_available = False
    return _tesseract_available


def _extract_onscreen_tokens(image: Image.Image) -> set[str]:
    if not _check_tesseract():
        return set()
    try:
        raw = pytesseract.image_to_string(image)
    except Exception:  # noqa: BLE001 -- a bad OCR call must not sink the shot
        return set()
    return {t for t in re.findall(r"[a-z']+", raw.lower()) if len(t) >= 3}


def _matches_onscreen_tokens(tag: str, onscreen_tokens: frozenset[str]) -> bool:
    if not onscreen_tokens:
        return False
    content_words = [w for w in tag.split() if w not in _TAG_STOPWORDS and len(w) >= 3]
    return bool(content_words) and all(w in onscreen_tokens for w in content_words)


_clip_model = None
_clip_preprocess = None
_clip_tokenizer = None
_vlm_model = None
_vlm_tokenizer = None


def _device_and_dtype():
    if torch.backends.mps.is_available():
        return "mps", torch.float16
    return "cpu", torch.float32


def _load_clip():
    global _clip_model, _clip_preprocess, _clip_tokenizer
    if _clip_model is None:
        import open_clip

        model, _, preprocess = open_clip.create_model_and_transforms(
            CLIP_MODEL_NAME, pretrained=CLIP_PRETRAINED
        )
        model.eval()
        _clip_model = model
        _clip_preprocess = preprocess
        _clip_tokenizer = open_clip.get_tokenizer(CLIP_MODEL_NAME)
    return _clip_model, _clip_preprocess, _clip_tokenizer


def _load_vlm():
    global _vlm_model, _vlm_tokenizer
    if _vlm_model is None:
        from transformers import AutoModelForCausalLM, AutoTokenizer

        device, dtype = _device_and_dtype()
        model = AutoModelForCausalLM.from_pretrained(
            VLM_MODEL_ID, revision=VLM_REVISION, trust_remote_code=True, torch_dtype=dtype
        ).to(device)
        tokenizer = AutoTokenizer.from_pretrained(VLM_MODEL_ID, revision=VLM_REVISION)
        _vlm_model = model
        _vlm_tokenizer = tokenizer
    return _vlm_model, _vlm_tokenizer


def _resize_to_max_dim(frame_bgr: np.ndarray, max_dim: int) -> np.ndarray:
    h, w = frame_bgr.shape[:2]
    scale = max_dim / max(h, w)
    if scale >= 1.0:
        return frame_bgr
    new_size = (int(w * scale), int(h * scale))
    return cv2.resize(frame_bgr, new_size, interpolation=cv2.INTER_AREA)


def _resize_for_keyframe(frame_bgr: np.ndarray) -> np.ndarray:
    return _resize_to_max_dim(frame_bgr, KEYFRAME_MAX_DIM)


def embed_image(image: Image.Image) -> list[float]:
    model, preprocess, _ = _load_clip()
    tensor = preprocess(image).unsqueeze(0)
    with torch.no_grad():
        features = model.encode_image(tensor)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.squeeze(0).tolist()


def embed_text(text: str) -> list[float]:
    """Embeds text into the same joint space as `embed_image` -- shared with
    `embed_text_server.py` so query-time and index-time embeddings are
    always produced by the identical model/preprocessing path."""
    model, _, tokenizer = _load_clip()
    tokens = tokenizer([text])
    with torch.no_grad():
        features = model.encode_text(tokens)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.squeeze(0).tolist()


def _parse_tags(raw: str, onscreen_tokens: frozenset[str] = frozenset()) -> list[str]:
    """Splits the VLM's comma-separated response into clean tags. Filters
    out purely numeric fragments and single characters -- observed in
    practice on unusual (non-photographic) frames, where the model echoes
    the "3 to 5" instruction text back as if it were a tag itself rather
    than following it.

    Also drops any tag containing a digit at all, not just purely-numeric
    ones. This is a deterministic backstop for TAGS_PROMPT's "don't tag
    on-screen text" instruction, which the VLM isn't guaranteed to follow --
    a tag like "no23" or "42-10" almost always means the model read a
    jersey number or a scoreboard score/clock rather than describing actual
    subject matter, and letting text visible in the footage leak into
    searchable/filterable tags is exactly what this suite's private-school
    data-privacy mandate rules out. No legitimate subject-matter tag
    (mascot, cheering, classroom) needs a digit, so this costs nothing on
    the recall side.

    Also drops any tag containing a whole-word gender/headcount term (see
    `_GENDER_TAG_WORDS`) -- a deterministic backstop for TAGS_PROMPT's "don't
    tag gender" instruction, catching both a bare "boy"/"girl" tag and a
    headcount phrase like "two boys" (word-matched, not substring, so
    "cowboy hat" or "girlfriend" -- neither of which the VLM should ever
    actually produce here -- wouldn't false-positive on the substring
    alone).

    Also drops any tag matching `_looks_like_onscreen_text` (quoted,
    UI-role-suffixed, sentence-punctuated, or implausibly long -- see that
    function) or `_matches_onscreen_tokens` against `onscreen_tokens` (an
    OCR pass over the same keyframe, from `_extract_onscreen_tokens`) --
    together the deterministic backstop for on-screen text made of ordinary
    words, which the digit filter above does nothing for."""
    seen = set()
    tags = []
    for part in raw.split(","):
        tag = part.strip().strip(".").lower()
        if not tag or len(tag) < 2 or tag in seen or any(ch.isdigit() for ch in tag):
            continue
        if _GENDER_TAG_WORDS.intersection(tag.split()):
            continue
        if _looks_like_onscreen_text(tag) or _matches_onscreen_tokens(tag, onscreen_tokens):
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= MAX_TAGS_PER_SHOT:
            break
    return tags


# moondream2 recurring boilerplate: a generic mood-descriptor clause
# ("...creating a relaxed and inviting atmosphere.", "...conveying a sense
# of community and connection.") tacked onto the end of many otherwise
# distinct captions -- confirmed live, ~6% of captions in a real archive
# (97/1629) end in this exact template. Because it's boilerplate rather
# than actual scene content, CLIP's text encoder treats captions sharing it
# as anomalously similar to *any* query regardless of subject matter (a
# text-embedding "hub") -- one such caption was confirmed clearing the
# search relevance floor against every one of several completely unrelated
# test queries, consistently outranking genuinely on-topic shots.
# A 1-2 word mood/theme phrase, optionally paired with a second one via
# "and" (e.g. "relaxed", "relaxed and inviting", "unity and shared
# experience") -- reused across the three tail shapes actually observed.
_MOOD_PHRASE = r"[a-z]+(?:\s+[a-z]+)?(?:\s+and\s+[a-z]+(?:\s+[a-z]+)?)?"
_GENERIC_CAPTION_TAIL = re.compile(
    rf",?\s*(?:creating|conveying|evoking)\s+an?\s+"
    rf"(?:{_MOOD_PHRASE}\s+(?:atmosphere|ambiance|mood)"
    rf"|(?:atmosphere|ambiance|mood)\s+of\s+{_MOOD_PHRASE}"
    rf"|sense\s+of\s+{_MOOD_PHRASE})"
    rf"\.?\s*$",
    re.IGNORECASE,
)


def _strip_generic_caption_tail(caption: str) -> str:
    """Strips `_GENERIC_CAPTION_TAIL` before embedding a caption -- only the
    text fed to the embedding model is affected; the caption stored and
    shown in the UI keeps its original, unmodified wording. Falls back to
    the original caption if stripping would leave nothing (a caption that's
    *only* the generic clause), since an empty embedding input is worse
    than a boilerplate one."""
    stripped = _GENERIC_CAPTION_TAIL.sub("", caption).strip()
    return stripped if stripped else caption


# Fixed, diverse battery of anchor phrases spanning likely school-archive
# content domains -- used only to measure a caption's own "hubness" (its
# baseline similarity to text in general, independent of any real query),
# never shown to the user or treated as a real tag/caption. Confirmed live
# that stripping `_GENERIC_CAPTION_TAIL` alone does *not* fix the hub
# problem -- a hub caption re-embedded after stripping its boilerplate tail
# still averaged 0.648 similarity across 20 unrelated test queries, versus
# 0.40 for an ordinary caption from the same clip -- so the hub-ness is
# structural to where that particular embedding sits in CLIP's text space,
# not a phrasing artifact. This anchor battery is what quantifies that per
# caption so search can subtract it back out (migration 009).
HUB_SCORE_ANCHOR_PHRASES = [
    "students eating lunch in the cafeteria",
    "a basketball game in the gym",
    "a science class experiment",
    "an art class painting project",
    "a robotics club competition",
    "a graduation ceremony on stage",
    "a swim meet at the pool",
    "a chess club match",
    "a debate tournament",
    "a chemistry lab session",
    "a marching band performance on the field",
    "a yearbook photo shoot",
    "construction of a new building",
    "snow on campus in winter",
    "students studying in the library",
    "a theater rehearsal on stage",
    "a faculty meeting",
    "orientation day for new students",
    "a homecoming dance",
    "students volunteering for community service",
    "a field trip to a museum",
    "an assembly in the auditorium",
    "students walking in a hallway between classes",
    "a portrait photo of one student",
]

_hub_anchor_vectors: list[list[float]] | None = None


def _hub_anchor_vectors_cached() -> list[list[float]]:
    """Lazily embeds `HUB_SCORE_ANCHOR_PHRASES` once and reuses the result
    for every shot in this process -- re-embedding a fixed battery per shot
    would be pure waste."""
    global _hub_anchor_vectors
    if _hub_anchor_vectors is None:
        _hub_anchor_vectors = [embed_text(phrase) for phrase in HUB_SCORE_ANCHOR_PHRASES]
    return _hub_anchor_vectors


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def caption_hub_score(caption_embedding: list[float]) -> float:
    """A caption embedding's average cosine similarity against the fixed
    anchor battery -- its own baseline closeness to text in general,
    independent of any specific query. Search subtracts this from a
    caption's raw similarity to a real query (migration 009) so a hub
    caption's structural closeness to *everything* cancels out, leaving
    only what's actually specific to that query."""
    anchors = _hub_anchor_vectors_cached()
    return sum(_cosine(caption_embedding, anchor) for anchor in anchors) / len(anchors)


def caption_and_tag(image: Image.Image) -> tuple[str | None, list[str], list[float] | None, float | None]:
    """Runs the local VLM gap-fill pass on one keyframe (Section 6 step 5) --
    the only source of subject-matter tags anywhere in the pipeline. Returns
    (caption, tags, caption_embedding, caption_hub_score); any failure here
    degrades to (None, [], None, None) rather than failing the whole shot --
    the visual embedding alone still makes the shot searchable."""
    try:
        model, tokenizer = _load_vlm()
        enc_image = model.encode_image(image)
        caption = model.answer_question(enc_image, CAPTION_PROMPT, tokenizer).strip()
        tags_raw = model.answer_question(enc_image, TAGS_PROMPT, tokenizer)
        onscreen_tokens = frozenset(_extract_onscreen_tokens(image))
        tags = _parse_tags(tags_raw, onscreen_tokens)
        caption_embedding = embed_text(_strip_generic_caption_tail(caption)) if caption else None
        hub_score = caption_hub_score(caption_embedding) if caption_embedding else None
        return caption or None, tags, caption_embedding, hub_score
    except Exception:  # noqa: BLE001 -- a bad VLM call must not sink the shot
        return None, [], None, None


def _merge_short_scenes(scenes: list[tuple[float, float]], min_duration: float) -> list[tuple[float, float]]:
    """Folds any scene shorter than `min_duration` into a neighbor instead
    of dropping it, so the result still covers the same total span with no
    gaps -- a dropped short scene would otherwise leave a hole in the
    clip's shot coverage that gap-fill never revisits."""
    if len(scenes) <= 1:
        return list(scenes)

    merged = [list(scenes[0])]
    for start, end in scenes[1:]:
        if merged[-1][1] - merged[-1][0] < min_duration:
            # Previous scene is still too short -- absorb this one into it
            # (cascades further on the next iteration if it's still short).
            merged[-1][1] = end
        else:
            merged.append([start, end])

    if len(merged) > 1 and merged[-1][1] - merged[-1][0] < min_duration:
        # Nothing after the last scene to absorb into -- fold it backward
        # into its predecessor instead of leaving a stray sliver.
        last = merged.pop()
        merged[-1][1] = last[1]

    return [(start, end) for start, end in merged]


def _detect_scenes(video_path: str):
    video = open_video(video_path)
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(min_scene_len=MIN_SHOT_DURATION_SEC))
    scene_manager.detect_scenes(video=video)
    scene_list = scene_manager.get_scene_list()
    duration_sec = video.duration.seconds

    if not scene_list:
        # No cuts detected -- PySceneDetect returns an empty list for a
        # single continuous shot rather than one span covering it, so we
        # synthesize that single shot ourselves.
        return duration_sec, [(0.0, duration_sec)]

    shots = [(start.seconds, end.seconds) for start, end in scene_list]
    return duration_sec, _merge_short_scenes(shots, MIN_SHOT_DURATION_SEC)


def _grab_frame_at(cap: cv2.VideoCapture, time_sec: float) -> np.ndarray | None:
    cap.set(cv2.CAP_PROP_POS_MSEC, time_sec * 1000.0)
    ok, frame = cap.read()
    return frame if ok else None


def analyze(video_path: str, keyframe_dir: Path) -> dict:
    duration_sec, shots = _detect_scenes(video_path)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"could not open video for keyframe extraction: {video_path}")

    # Needed for accurate Premiere Pro XMEML export (Section 14) -- every
    # clipitem's frame math depends on knowing each source clip's real
    # frame rate rather than assuming a constant across a mixed archive.
    frame_rate = cap.get(cv2.CAP_PROP_FPS)
    frame_rate = frame_rate if frame_rate and frame_rate > 0 else None

    keyframe_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, (start_tc, end_tc) in enumerate(shots):
        midpoint = start_tc + (end_tc - start_tc) / 2.0
        frame = _grab_frame_at(cap, midpoint)
        if frame is None:
            # A handful of frames near a corrupt/truncated region shouldn't
            # sink the whole clip's analysis -- skip this shot, keep going.
            continue

        resized = _resize_for_keyframe(frame)
        filename = f"shot_{i:04d}.jpg"
        out_path = keyframe_dir / filename
        cv2.imwrite(str(out_path), resized, [cv2.IMWRITE_JPEG_QUALITY, KEYFRAME_JPEG_QUALITY])

        image = Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB))
        embedding = embed_image(image)
        caption, tags, caption_embedding, caption_hub_score = caption_and_tag(image)

        results.append(
            {
                "start_tc": start_tc,
                "end_tc": end_tc,
                "keyframe_filename": filename,
                "embedding": embedding,
                "caption": caption,
                "tags": tags,
                "caption_embedding": caption_embedding,
                "caption_hub_score": caption_hub_score,
            }
        )

    cap.release()
    return {"duration_sec": duration_sec, "frame_rate": frame_rate, "shots": results}


def serve() -> int:
    """Persistent mode -- see the module docstring's `--serve` section.
    Keeps this process (and its loaded CLIP/moondream2 weights) alive
    across many clips instead of the one-shot mode's pay-the-load-cost-
    every-time behavior, which was the dominant cost in gap-fill throughput
    once more than a few clips were queued."""
    # Trigger both model loads eagerly rather than on the first request, so
    # the caller's first real clip isn't the one that pays the load latency.
    _load_clip()
    _load_vlm()
    print("ready", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            output = analyze(request["video_path"], Path(request["keyframe_dir"]))
            print(json.dumps(output), flush=True)
        except Exception as exc:  # noqa: BLE001 -- one bad clip must not kill the server
            print(json.dumps({"error": str(exc)}), flush=True)

    return 0


def main() -> int:
    # A single clip's analysis (scene detection + CLIP + moondream2
    # captioning) can run for minutes -- long enough that this process must
    # not outlive an unclean exit of its Rust host (force-quit, crash,
    # SIGKILL bypasses the host's own `run_sidecar`/`AnalyzeWorker` timeout/
    # cancel kill, since that code never gets to run). Applies to both
    # modes below, so it's checked once here rather than in each.
    exit_if_parent_dies()

    if len(sys.argv) == 2 and sys.argv[1] == "--serve":
        return serve()

    if len(sys.argv) != 3:
        print("usage: analyze_clip.py <video_path> <keyframe_output_dir>\n       analyze_clip.py --serve", file=sys.stderr)
        return 2

    video_path, keyframe_dir = sys.argv[1], Path(sys.argv[2])
    try:
        output = analyze(video_path, keyframe_dir)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any failure
        # here must be reported and skipped, never crash the whole batch.
        print(f"analyze_clip failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
