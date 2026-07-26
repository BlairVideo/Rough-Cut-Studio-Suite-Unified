"""
prompt_ai.py — the "integrated AI prompt" that lets the editor type a
sentence like:

    "clean elegant title, blue background, seal centered at the bottom,
     slow fade in, subtitle in italics"

and have it drive the design.

IMPORTANT / SECURITY NOTE:
This is implemented as a fully local, deterministic, rule-based parser —
not a call to a hosted LLM. That is a deliberate choice for this tool:
  - Zero network calls -> nothing about the video project, titles, or
    school messaging ever leaves the editor's machine.
  - Zero API keys / credentials to manage or leak.
  - Free forever, no rate limits, works offline (e.g. editing on set).

If Blair later wants true generative-AI phrasing understanding, the
`interpret()` function is the one place to swap in a call to a hosted
model (Anthropic's API, a local Ollama model, etc.) — everything else in
the app just consumes the returned dict, so the integration point is
small and isolated.
"""

import re
import brand

MOOD_KEYWORDS = {
    "Clean & Elegant": ["clean", "elegant", "classy", "formal", "graduation", "commencement",
                         "sophisticated", "refined", "minimal", "understated"],
    "Pop & Upbeat": ["pop", "upbeat", "fun", "energetic", "playful", "bold", "loud", "hype",
                      "exciting", "vibrant", "social media", "instagram", "reel"],
    "Traditional / Formal": ["traditional", "heritage", "historic", "founders", "trustees",
                              "ceremony", "academic", "convocation"],
    "Athletics / High Energy": ["sports", "athletics", "game", "team", "championship", "varsity",
                                 "sideline", "highlight", "pump up", "hype video"],
    "Warm / Community": ["community", "welcome", "family", "warm", "friendly", "reunion",
                          "alumni weekend", "cozy", "inviting"],
}

COLOR_KEYWORDS = {
    "blue": "#004b8d", "blair blue": "#004b8d", "navy": "#093266", "dark blue": "#093266",
    "grey": "#72808a", "gray": "#72808a", "cool grey": "#72808a", "warm grey": "#99928a",
    "orange": "#f15d22", "red": "#da1a32", "yellow": "#dd971a", "gold": "#dd971a",
    "teal": "#00b2ba", "green": "#74a333", "purple": "#44477a", "eggplant": "#770055",
    "burnt orange": "#c6671d", "white": "#ffffff", "black": "#000000",
}

ANIMATION_KEYWORDS = {
    "bounce": ["bounce", "pop in", "spring", "elastic", "punchy"],
    "zoom": ["zoom", "scale in", "grow in", "scaling"],
    "wipe": ["wipe", "reveal wipe", "swipe reveal"],
    "stagger": ["stagger", "cascade", "letter cascade"],
    "typewriter": ["typewriter", "type on", "typing"],
    "slide": ["slide", "swipe", "push", "glide"],
    "fade": ["fade", "dissolve", "gentle", "soft"],
    "none": ["static", "no animation", "still"],
}

ASPECT_KEYWORDS = {
    "9:16 Vertical (Stories / Reels / TikTok)": ["vertical", "story", "stories", "reel", "reels",
                                                   "tiktok", "portrait video", "9:16"],
    "1:1 Square (Instagram feed)": ["square", "1:1"],
    "4:5 Portrait (Instagram feed)": ["4:5", "portrait feed", "instagram portrait"],
    "16:9 Landscape (video)": ["landscape", "widescreen", "horizontal", "16:9"],
}

LAYOUT_KEYWORDS = {
    "Lower Third": ["lower third", "lower-third", "name plate", "nameplate", "caption bar", "chyron"],
    "Full Title Card": ["full screen title", "full title card", "title card", "full-screen"],
}

OUTRO_KEYWORDS = {
    "fade": ["fade out", "dissolve out", "fade away"],
    "slide": ["slide out", "swipe out", "slide away"],
    "zoom": ["zoom out", "shrink out", "shrink away"],
    "wipe": ["wipe out", "wipe away"],
    "none": ["no outro", "no exit", "stay on screen", "hold at the end", "no fade out"],
}

LOWER_THIRD_POSITION_PHRASES = {
    "Bottom Left": ["bottom left", "lower left"],
    "Bottom Center": ["bottom center", "bottom middle"],
    "Bottom Right": ["bottom right", "lower right"],
    "Top Left": ["top left", "upper left"],
    "Top Center": ["top center", "top middle"],
    "Top Right": ["top right", "upper right"],
}

SPEED_KEYWORDS = {
    "slow": ["slow", "slowly", "gentle", "leisurely", "lingering"],
    "fast": ["fast", "quick", "snappy", "rapid", "punchy"],
}

PLACEMENT_KEYWORDS = {
    "top-left": ["top left", "upper left"],
    "top-right": ["top right", "upper right"],
    "top-center": ["top center", "top middle", "top of the frame"],
    "bottom-left": ["bottom left", "lower left"],
    "bottom-right": ["bottom right", "lower right"],
    "bottom-center": ["bottom center", "bottom middle", "bottom of the frame", "centered at the bottom"],
    "center": ["centered", "middle of the frame", "center of the frame"],
}

LOGO_KEYWORDS = {
    # More specific entries first: "seal"/"crest" alone are generic enough
    # to appear inside phrasing for the other seal-based logos too (e.g.
    # "the ribbon seal"), and interpret() takes the first match, so a
    # specific keyword like "ribbon" must be checked before the plain
    # crest's generic "seal"/"crest" or it can never win.
    "Blair Seal + Ribbon": ["ribbon"],
    "Blair Seal (crest)": ["seal", "crest"],
    "Blair \"B\" Monogram": ["monogram", "the b", "\"b\" logo", "b logo"],
    "no logo": ["no logo", "without the logo", "no seal", "without a seal"],
}

# "second"/"sec"/"s" alone (no plural) so plural "seconds"/"secs" never
# matched: \b requires a word boundary immediately after the alternative,
# but "seconds" continues with another word char ('s') right where
# "second" ends, so the boundary check failed and the whole alternation
# silently rejected the plural -- the far more natural phrasing ("make it
# 7 seconds long"). `seconds?`/`secs?` (each already ordered longest-first
# within the group) restores the boundary check to the end of the actual
# matched word in both singular and plural forms.
DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b")


def _contains_any(text, keywords):
    return any(kw in text for kw in keywords)


def interpret(prompt_text, base_scene):
    """
    Parse free-text `prompt_text` and return a NEW scene dict, starting
    from `base_scene` and overriding whatever the prompt clearly specifies.
    Anything not mentioned is left untouched, so the editor can issue small
    follow-up prompts ("make it faster", "move the seal to the top right")
    without resetting everything else.
    """
    text = prompt_text.lower()
    scene = dict(base_scene)
    notes = []

    # ---- Explicit title/subtitle text overrides ---------------------------
    # e.g.  title: "Welcome Home"   or   subtitle "Alumni Weekend 2026"
    title_text_match = re.search(r'(?<!sub)\btitle[:\s]+"([^"]+)"', prompt_text, re.IGNORECASE)
    if title_text_match:
        scene["title"] = title_text_match.group(1)
        notes.append(f'Title text set to "{scene["title"]}"')
    subtitle_text_match = re.search(r'subtitle[:\s]+"([^"]+)"', prompt_text, re.IGNORECASE)
    if subtitle_text_match:
        scene["subtitle"] = subtitle_text_match.group(1)
        notes.append(f'Subtitle text set to "{scene["subtitle"]}"')

    # ---- Aspect ratio / format ---------------------------------------------
    for canvas_name, kws in ASPECT_KEYWORDS.items():
        if _contains_any(text, kws):
            scene["canvas_preset_name"] = canvas_name
            scene["canvas_size"] = brand.CANVAS_PRESETS[canvas_name]
            notes.append(f"Format set to {canvas_name}")
            break

    # ---- Layout -------------------------------------------------------------
    for layout_name, kws in LAYOUT_KEYWORDS.items():
        if _contains_any(text, kws):
            scene["layout"] = layout_name
            notes.append(f"Layout set to {layout_name}")
            break

    # ---- Lower third position / scale ----------------------------------------
    if "lower third" in text or scene.get("layout") == "Lower Third":
        for pos_name, kws in LOWER_THIRD_POSITION_PHRASES.items():
            if _contains_any(text, kws):
                scene["lower_third_position"] = pos_name
                notes.append(f"Lower third position set to {pos_name}")
                break

        lt_scale_match = re.search(r"lower third[^0-9]*(\d{2,3})\s*%", text)
        if lt_scale_match:
            scene["lower_third_scale"] = max(0.5, min(1.8, int(lt_scale_match.group(1)) / 100))
            notes.append(f"Lower third scale set to {int(scene['lower_third_scale']*100)}%")
        elif "bigger lower third" in text or "larger lower third" in text:
            scene["lower_third_scale"] = 1.3
            notes.append("Lower third made larger")
        elif "smaller lower third" in text:
            scene["lower_third_scale"] = 0.75
            notes.append("Lower third made smaller")

    # ---- Outro (exit) animation -----------------------------------------------
    for outro_name, kws in OUTRO_KEYWORDS.items():
        if _contains_any(text, kws):
            scene["outro_animation"] = outro_name
            notes.append(f"Outro set to '{outro_name}'")
            break

    # ---- Background style / gradient / vignette ------------------------------
    if "gradient" in text:
        scene["background_style"] = "Gradient"
        scene["transparent_bg"] = False
        notes.append("Background style set to gradient")
    elif "solid background" in text or "flat background" in text:
        scene["background_style"] = "Solid"

    vignette_pct_match = re.search(r"vignette[^0-9]*(\d{1,3})\s*%?", text)
    if vignette_pct_match:
        scene["vignette"] = max(0, min(100, int(vignette_pct_match.group(1))))
        notes.append(f"Vignette set to {scene['vignette']}%")
    elif "vignette" in text or "moody edges" in text or "darkened edges" in text:
        scene["vignette"] = 50
        notes.append("Vignette added")
    elif "no vignette" in text or "remove vignette" in text:
        scene["vignette"] = 0

    if "circular vignette" in text or "spotlight" in text or "circle vignette" in text:
        scene["vignette_shape"] = "Circular"
        notes.append("Vignette shape set to circular")
    elif "rectangular vignette" in text or "frame vignette" in text or "box vignette" in text:
        scene["vignette_shape"] = "Rectangular"
        notes.append("Vignette shape set to rectangular")
    elif "elliptical vignette" in text or "oval vignette" in text:
        scene["vignette_shape"] = "Elliptical"
        notes.append("Vignette shape set to elliptical")

    # ---- Mood / preset ------------------------------------------------
    best_preset, best_hits = None, 0
    for preset_name, kws in MOOD_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw in text)
        if hits > best_hits:
            best_preset, best_hits = preset_name, hits
    if best_preset:
        preset = brand.PRESETS[best_preset]
        scene.update(preset)
        notes.append(f"Applied '{best_preset}' preset")

    # ---- Explicit colors (override preset if mentioned) ----------------
    for word, hexval in COLOR_KEYWORDS.items():
        if word in text:
            if "background" in text or "bg" in text:
                pass  # handled below with more context
    # background color
    bg_match = re.search(r"([a-z\- ]+?)\s+background", text)
    if bg_match:
        color_phrase = bg_match.group(1).strip()
        for word, hexval in sorted(COLOR_KEYWORDS.items(), key=lambda kv: -len(kv[0])):
            if word in color_phrase:
                scene["bg_color"] = hexval
                scene["transparent_bg"] = False
                notes.append(f"Background color set to {word}")
                break
    if "transparent background" in text or "no background" in text:
        scene["transparent_bg"] = True
        notes.append("Background set to transparent")

    # accent / text color
    accent_match = re.search(r"([a-z\- ]+?)\s+(?:accent|divider|line)", text)
    if accent_match:
        for word, hexval in sorted(COLOR_KEYWORDS.items(), key=lambda kv: -len(kv[0])):
            if word in accent_match.group(1):
                scene["accent_color"] = hexval
                notes.append(f"Accent color set to {word}")
                break

    text_match = re.search(r"([a-z\- ]+?)\s+(?:text|title|type)\b", text)
    if text_match:
        for word, hexval in sorted(COLOR_KEYWORDS.items(), key=lambda kv: -len(kv[0])):
            if word in text_match.group(1):
                scene["text_color"] = hexval
                notes.append(f"Text color set to {word}")
                break

    # ---- Animation style -------------------------------------------------
    for anim, kws in ANIMATION_KEYWORDS.items():
        if _contains_any(text, kws):
            scene["animation"] = anim
            notes.append(f"Animation set to '{anim}'")
            break

    # ---- Speed adjusts duration -------------------------------------------
    dur_match = DURATION_RE.search(text)
    if dur_match:
        scene["duration"] = float(dur_match.group(1))
        notes.append(f"Duration set to {scene['duration']}s")
    elif _contains_any(text, SPEED_KEYWORDS["slow"]):
        scene["duration"] = max(scene.get("duration", 3.0), 5.0)
        notes.append("Slowed the animation down")
    elif _contains_any(text, SPEED_KEYWORDS["fast"]):
        scene["duration"] = min(scene.get("duration", 3.0), 2.0)
        notes.append("Sped the animation up")

    # ---- Logo choice & placement -------------------------------------------
    if _contains_any(text, LOGO_KEYWORDS["no logo"]):
        scene["logo"] = "None"
        notes.append("Removed the logo")
    else:
        for logo_name, kws in LOGO_KEYWORDS.items():
            if logo_name == "no logo":
                continue
            if _contains_any(text, kws):
                scene["logo"] = logo_name
                notes.append(f"Logo set to {logo_name}")
                break

    for placement, kws in PLACEMENT_KEYWORDS.items():
        if _contains_any(text, kws):
            scene["logo_placement"] = placement
            notes.append(f"Logo placed at {placement}")
            break

    if "white logo" in text or "knockout" in text or "white seal" in text:
        scene["logo_color_mode"] = "white"
        notes.append("Logo recolored to white knockout")
    elif "original color logo" in text or "full color seal" in text or "blue seal" in text:
        scene["logo_color_mode"] = "original"

    # ---- Case ------------------------------------------------------------
    if "lowercase" in text or "sentence case" in text or "mixed case" in text:
        scene["uppercase_title"] = False
        notes.append("Title set to mixed case")
    elif "all caps" in text or "uppercase" in text or "all-caps" in text:
        scene["uppercase_title"] = True
        notes.append("Title set to all caps")

    # ---- Divider on/off ----------------------------------------------------
    if "no divider" in text or "without a line" in text or "no line" in text:
        scene["divider"] = False
    elif "divider" in text or "with a line" in text or "underline" in text:
        scene["divider"] = True

    if not notes:
        notes.append("No recognized design keywords found — try mentioning a "
                      "mood (elegant / upbeat / athletic), a color, an animation "
                      "(fade / slide / bounce / typewriter), or the seal's placement.")

    return scene, notes
