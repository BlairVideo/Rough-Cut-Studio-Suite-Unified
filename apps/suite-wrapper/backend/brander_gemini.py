"""
brander_gemini.py — Gemini-backed "AI mode" for the Graphics workspace.

Mirrors Rough Cut Studio's gemini_client.py conventions exactly: one plain
`requests` POST to the generateContent endpoint (no SDK), the API key sent
ONLY in the `x-goog-api-key` header (never a URL param, never logged),
responseMimeType pinned to application/json with a strict responseSchema,
exponential-backoff retries on 429/503, and defensive scrubbing of the key
from any exception text.

Trust boundary: the model's answer is treated as UNTRUSTED input. It may
only propose a flat "scene update" of whitelisted fields, and every single
field is re-validated/clamped server-side by validate_update() before it
touches a scene — enum fields are checked against the live brand constants
(including custom logos), colors against a #rrggbb regex, and numerics are
clamped to the UI's own ranges. Invalid values are dropped with a
human-readable note rather than failing the whole request.

validate_update() is a PURE function (json/re only, no Brander imports, no
network) so the selftest can unit-test the whole validation path with a
fake response dict.
"""

import json
import re
import time
import random

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Same auto-updated alias RCS's gemini_client uses, so the default doesn't
# go stale as Google ships new model generations.
DEFAULT_MODEL = "gemini-flash-latest"

# Image generation is a genuinely different capability (multimodal output,
# not the responseSchema/JSON path above) and isn't covered by the
# "-latest" text alias — Google's image-capable models are versioned
# separately. Update this constant if Google renames/deprecates it; every
# other piece of generate_graphic_image() is model-agnostic.
IMAGE_MODEL = "gemini-2.5-flash-image"

RETRYABLE_STATUSES = {429, 503}
MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 2.0

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_TEXT_LEN = 200

# ---------------------------------------------------------------------------
# The whitelist. Ranges match the Graphics form's own sliders (shell.html) —
# with logo_height's ceiling at addendum v22's raised 900 (renderer.py's own
# render-time cap was raised alongside it, from 0.32*H to 0.85*H) — plus the
# addendum-mandated duration/hold clamps.
# ---------------------------------------------------------------------------

STRING_FIELDS = ("title", "subtitle")

INT_FIELDS = {
    "title_size": (40, 300),
    "subtitle_size": (16, 140),
    "logo_height": (40, 900),
    "logo_opacity": (0, 100),
    "vignette": (0, 100),
    "text_offset_x": (-400, 400),
    "text_offset_y": (-400, 400),
}

BOOL_FIELDS = ("divider", "transparent_bg", "logo_grow")

FLOAT_FIELDS = {
    "duration": (0.5, 30.0),
    "hold_seconds": (0.0, 10.0),
}

COLOR_FIELDS = ("bg_color", "accent_color", "text_color", "logo_custom_color")

# The twelve normalized animation-timing floats, all clamped to 0..1.
TIMING_FIELDS = (
    "title_in_start", "title_in_end",
    "subtitle_in_start", "subtitle_in_end",
    "logo_in_start", "logo_in_end",
    "title_out_start", "title_out_end",
    "subtitle_out_start", "subtitle_out_end",
    "logo_out_start", "logo_out_end",
)

LOGO_COLOR_MODES = ("original", "white", "custom")


class BranderGeminiError(Exception):
    pass


def _enum_fields(options):
    """field name -> list of allowed values, built from the live options
    dict (so custom logos imported this session are legal values)."""
    return {
        "title_font": list(options.get("fonts") or []),
        "subtitle_font": list(options.get("fonts") or []),
        "layout": list(options.get("layouts") or []),
        "background_style": list(options.get("background_styles") or []),
        "animation": list(options.get("animations") or []),
        "outro_animation": list(options.get("outro_animations") or []),
        "logo": ["None"] + list(options.get("logos") or []),
        "logo_placement": list(options.get("logo_placements") or []),
        "logo_color_mode": list(LOGO_COLOR_MODES),
        "canvas_preset_name": list((options.get("canvas_presets") or {}).keys()),
        "lower_third_position": list(options.get("lower_third_positions") or []),
    }


# ---------------------------------------------------------------------------
# Validation (pure — unit-tested in --selftest with a fake response dict)
# ---------------------------------------------------------------------------

def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_update(update, scene, options):
    """Whitelist-validate a raw scene-update dict (the model's parsed JSON
    answer) against the current scene and options.

    Returns (clean_updates, notes):
      * clean_updates — ONLY whitelisted fields whose values passed their
        enum/regex/type check, with numerics clamped to the UI ranges.
      * notes — the model's own `notes` strings (if any) followed by one
        note per dropped/clamped field, ready to toast.

    `scene` is accepted for parity with the callers' data flow (and future
    cross-field checks); validation itself is field-local by design so a
    bad field can never poison a good one. Pure function: no I/O, no
    imports of Brander modules, no network.
    """
    notes = []
    clean = {}

    if not isinstance(update, dict):
        return {}, ["The AI response was not a JSON object — no changes applied."]

    # The model's own commentary rides along in a `notes` array.
    raw_notes = update.get("notes")
    if isinstance(raw_notes, list):
        notes.extend(str(n).strip() for n in raw_notes
                     if isinstance(n, str) and n.strip())
    elif raw_notes is not None:
        notes.append("Ignored malformed notes from the AI response.")

    enum_fields = _enum_fields(options)

    for key, value in update.items():
        if key == "notes":
            continue

        if key in STRING_FIELDS:
            if isinstance(value, str):
                text = value
                if len(text) > MAX_TEXT_LEN:
                    text = text[:MAX_TEXT_LEN]
                    notes.append(f"Trimmed {key} to {MAX_TEXT_LEN} characters.")
                clean[key] = text
            else:
                notes.append(f"Dropped {key}: expected text.")

        elif key in INT_FIELDS:
            lo, hi = INT_FIELDS[key]
            if _is_number(value):
                v = int(round(value))
                clamped = min(hi, max(lo, v))
                if clamped != v:
                    notes.append(f"Clamped {key} from {v} to {clamped} (allowed {lo}–{hi}).")
                clean[key] = clamped
            else:
                notes.append(f"Dropped {key}: expected a number.")

        elif key in FLOAT_FIELDS:
            lo, hi = FLOAT_FIELDS[key]
            if _is_number(value):
                v = float(value)
                clamped = min(hi, max(lo, v))
                if clamped != v:
                    notes.append(f"Clamped {key} from {v:g} to {clamped:g} (allowed {lo:g}–{hi:g}).")
                clean[key] = clamped
            else:
                notes.append(f"Dropped {key}: expected a number.")

        elif key in TIMING_FIELDS:
            if _is_number(value):
                v = float(value)
                clamped = min(1.0, max(0.0, v))
                if clamped != v:
                    notes.append(f"Clamped {key} to {clamped:g} (timings run 0–1).")
                clean[key] = clamped
            else:
                notes.append(f"Dropped {key}: expected a number between 0 and 1.")

        elif key in BOOL_FIELDS:
            if isinstance(value, bool):
                clean[key] = value
            else:
                notes.append(f"Dropped {key}: expected true or false.")

        elif key in COLOR_FIELDS:
            if isinstance(value, str) and HEX_COLOR_RE.match(value.strip()):
                clean[key] = value.strip().lower()
            else:
                notes.append(f"Dropped {key}: {value!r} is not a #rrggbb color.")

        elif key in enum_fields:
            allowed = enum_fields[key]
            if isinstance(value, str) and value in allowed:
                clean[key] = value
            else:
                notes.append(f"Dropped {key}: {value!r} is not one of the available options.")

        else:
            notes.append(f"Ignored unknown field {key!r} from the AI response.")

    return clean, notes


# ---------------------------------------------------------------------------
# Request path
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = """You are a brand-graphics design assistant for Blair Academy title
cards and lower thirds. You are given the CURRENT SCENE settings as JSON
and a request from the editor.

Respond with a flat JSON object containing ONLY the scene fields you want
to change (never echo fields you are leaving alone), plus a "notes" array
of short human-readable strings describing each change you made and why.

Rules:
  - Only use values allowed by the response schema. Enumerated fields
    (fonts, layouts, logos, placements, presets, ...) must use one of the
    listed options EXACTLY as written.
  - Colors are "#rrggbb" hex strings. Prefer the Blair brand palette
    provided in the request unless the editor asks for something else.
  - Keep text tasteful and brief; sizes and timings within their ranges.
  - Timing fields (*_in_start/.../*_out_end) are normalized 0..1 positions
    within the animation. An element's IN must finish before its OUT begins.
  - If the request is unclear or asks for something unsupported, change
    nothing and explain in "notes"."""


def build_response_schema(options):
    """The strict responseSchema for generationConfig — a flat OBJECT of
    the whitelisted fields (enums pinned to the live option lists) plus
    the notes array. Nothing is required: the model only sends what it
    changes."""
    props = {}
    for key in STRING_FIELDS:
        props[key] = {"type": "STRING"}
    for key in INT_FIELDS:
        props[key] = {"type": "INTEGER"}
    for key in FLOAT_FIELDS:
        props[key] = {"type": "NUMBER"}
    for key in TIMING_FIELDS:
        props[key] = {"type": "NUMBER"}
    for key in BOOL_FIELDS:
        props[key] = {"type": "BOOLEAN"}
    for key in COLOR_FIELDS:
        props[key] = {"type": "STRING"}
    for key, allowed in _enum_fields(options).items():
        props[key] = {"type": "STRING", "enum": list(allowed)}
    props["notes"] = {"type": "ARRAY", "items": {"type": "STRING"}}
    return {"type": "OBJECT", "properties": props}


def _scrub_secret(err, secret):
    """str(err) with any occurrence of the raw API key removed — same
    defensive scrub RCS's gemini_client performs, so a key can never end
    up in a wrapped error message, log, or printed traceback."""
    text = str(err)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return text


def _wait_before_retry(attempt):
    # Exponential backoff with jitter: ~2s, ~4s, ~8s.
    time.sleep(BASE_DELAY_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5))


def _format_palette(options):
    lines = []
    for group in ("primary_colors", "secondary_colors"):
        for name, hex_color in (options.get(group) or {}).items():
            lines.append(f"  {name}: {hex_color}")
    return "\n".join(lines) or "  (none provided)"


def generate_scene_update(api_key, prompt_text, scene_json, options,
                          model=DEFAULT_MODEL, timeout=60):
    """POST the prompt + current scene to Gemini and return the parsed
    (still UNVALIDATED) scene-update dict. Callers must pass the result
    through validate_update() before applying it.

    Raises BranderGeminiError for anything non-transient, or if the model
    is still overloaded/rate-limited after MAX_ATTEMPTS tries. The key is
    sent only in the x-goog-api-key header and scrubbed from every error.
    """
    import requests  # deferred so validate_update stays importable anywhere

    if not api_key or not api_key.strip():
        raise BranderGeminiError("No Gemini API key was provided.")

    user_content = (
        f"EDITOR REQUEST:\n{(prompt_text or '').strip() or '(no request text)'}\n\n"
        f"CURRENT SCENE:\n{json.dumps(scene_json, indent=2)}\n\n"
        f"BLAIR BRAND PALETTE (name: hex):\n{_format_palette(options)}"
    )

    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": build_response_schema(options),
            "temperature": 0.4,
        },
    }

    url = GEMINI_ENDPOINT.format(model=model)

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                data=json.dumps(body),
                timeout=timeout,
            )
        except requests.RequestException as e:
            last_error = BranderGeminiError(
                f"Network error calling Gemini: {_scrub_secret(e, api_key)}")
            if attempt < MAX_ATTEMPTS:
                _wait_before_retry(attempt)
                continue
            raise last_error from e

        if resp.status_code in RETRYABLE_STATUSES:
            last_error = BranderGeminiError(
                f"Gemini is temporarily unavailable (HTTP {resp.status_code}). "
                f"Retried {MAX_ATTEMPTS} times — wait a moment and try again.")
            if attempt < MAX_ATTEMPTS:
                _wait_before_retry(attempt)
                continue
            raise last_error

        if resp.status_code != 200:
            # Not transient (bad request, bad key, ...) — fail immediately.
            raise BranderGeminiError(
                f"Gemini returned HTTP {resp.status_code}: "
                f"{_scrub_secret(resp.text[:500], api_key)}")

        try:
            data = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise BranderGeminiError("Unexpected Gemini response shape.") from e

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise BranderGeminiError(
                f"Gemini did not return valid JSON: {e}") from e

    raise last_error or BranderGeminiError("Gemini request failed for an unknown reason.")


# ---------------------------------------------------------------------------
# Image generation ("completely custom" background graphics)
# ---------------------------------------------------------------------------

IMAGE_SYSTEM_INSTRUCTION = """You are a background-graphics generator for Blair Academy video
title cards and lower thirds. Generate a single still image suitable as a
full-bleed BACKGROUND PLATE behind large title text that will be drawn on
top of it afterward by a separate renderer.

Strict brand rules:
  - Use ONLY colors from the Blair Academy palette supplied below (small
    tonal variation for shading/gradients within a listed color is fine;
    introducing an unrelated hue is not).
  - Keep the overall look clean, professional, and suitable for an
    academic institution — no cartoonish, garish, or joke imagery.
  - Leave generous open space (a clear, low-detail region, not just low
    contrast) somewhere in the frame — large title text will be
    overlaid on top of your image afterward, and busy detail behind text
    hurts legibility.
  - Do NOT render any text, letters, numbers, logos, or watermarks into
    the image yourself — the title text and logo are added later as a
    separate layer. An image containing baked-in text will look wrong
    once real text is drawn on top of it.
"""


def _format_palette_prose(options):
    """Same palette as _format_palette but as an inline comma list — reads
    more naturally inside an image-generation prompt than the indented
    "name: hex" block used in the text-scene-update prompt."""
    names = []
    for group in ("primary_colors", "secondary_colors"):
        names.extend((options.get(group) or {}).keys())
    return ", ".join(names) or "Blair Blue, Dark Blue, Cool Grey"


def generate_graphic_image(api_key, prompt_text, options, model=IMAGE_MODEL, timeout=90):
    """POST a prompt to a Gemini image-generation model and return
    (image_bytes, mime_type) for the first generated image part.

    Mirrors generate_scene_update's auth/retry/error-handling conventions
    exactly (same header-only key, same retryable-status backoff, same
    secret-scrubbing) — the only real difference is the response shape:
    an image model returns inlineData (base64 image bytes) in a content
    part instead of a JSON text part, so generationConfig requests
    responseModalities: ["IMAGE"] rather than responseMimeType/
    responseSchema.

    Raises BranderGeminiError for anything non-transient, or if the model
    is still overloaded/rate-limited after MAX_ATTEMPTS tries — same
    contract as generate_scene_update."""
    import base64
    import requests  # deferred, same reason as generate_scene_update

    if not api_key or not api_key.strip():
        raise BranderGeminiError("No Gemini API key was provided.")

    user_content = (
        f"EDITOR REQUEST:\n{(prompt_text or '').strip() or '(no specific request — use your judgment)'}\n\n"
        f"BLAIR BRAND PALETTE (use only these colors): {_format_palette_prose(options)}"
    )

    body = {
        "systemInstruction": {"parts": [{"text": IMAGE_SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
        },
    }

    url = GEMINI_ENDPOINT.format(model=model)

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                data=json.dumps(body),
                timeout=timeout,
            )
        except requests.RequestException as e:
            last_error = BranderGeminiError(
                f"Network error calling Gemini: {_scrub_secret(e, api_key)}")
            if attempt < MAX_ATTEMPTS:
                _wait_before_retry(attempt)
                continue
            raise last_error from e

        if resp.status_code in RETRYABLE_STATUSES:
            last_error = BranderGeminiError(
                f"Gemini is temporarily unavailable (HTTP {resp.status_code}). "
                f"Retried {MAX_ATTEMPTS} times — wait a moment and try again.")
            if attempt < MAX_ATTEMPTS:
                _wait_before_retry(attempt)
                continue
            raise last_error

        if resp.status_code != 200:
            raise BranderGeminiError(
                f"Gemini returned HTTP {resp.status_code}: "
                f"{_scrub_secret(resp.text[:500], api_key)}")

        try:
            data = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise BranderGeminiError("Unexpected Gemini response shape.") from e

        for part in parts:
            inline = part.get("inlineData")
            if inline and inline.get("data"):
                try:
                    image_bytes = base64.b64decode(inline["data"])
                except (ValueError, TypeError) as e:
                    raise BranderGeminiError("Gemini's image data wasn't valid base64.") from e
                return image_bytes, inline.get("mimeType") or "image/png"

        raise BranderGeminiError(
            "Gemini didn't return an image — it may have declined the request "
            "(try rephrasing the prompt).")

    raise last_error or BranderGeminiError("Gemini request failed for an unknown reason.")
