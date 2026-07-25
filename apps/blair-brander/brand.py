"""
brand.py — Blair Academy brand constants, sourced from 2025_Style_Guide_final.pdf

This is the single source of truth for on-brand colors and type choices.
Keeping it separate makes it easy for Blair's communications office to
update if the style guide changes, without touching the app logic.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "fonts")
ASSET_DIR = os.path.join(BASE_DIR, "assets")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# ---------------------------------------------------------------------------
# COLOR PALETTE (Style Guide pp. 13-14)
# ---------------------------------------------------------------------------
PRIMARY_COLORS = {
    "Blair Blue":     "#004b8d",   # PMS 288 — core brand color
    "Dark Blue":      "#093266",   # PMS 534
    "Cool Grey":      "#72808a",   # PMS 430
    "Warm Grey":      "#99928a",   # PMS 402
    "Athletic Blue":  "#002244",   # PMS 289
}

SECONDARY_COLORS = {
    "Sky":          "#5f8fb4",   # PMS 7454
    "Orange":       "#f15d22",   # PMS 1665
    "Red":          "#da1a32",   # PMS 186
    "Yellow":       "#dd971a",   # PMS 131
    "Blue-Purple":  "#44477a",   # PMS 5275
    "Teal":         "#00b2ba",   # PMS 7466
    "Burnt Orange": "#c6671d",   # PMS 153
    "Eggplant":     "#770055",   # PMS 242
    "Green":        "#74a333",   # PMS 377
    "Web Grey 1":   "#e9e7e5",
    "Web Grey 2":   "#f1f3f4",
}

WHITE = "#ffffff"
BLACK = "#000000"

ALL_COLORS = {**PRIMARY_COLORS, **SECONDARY_COLORS, "White": WHITE, "Black": BLACK}


def hex_to_rgb(hex_color):
    """Parse a '#rrggbb' string into an (r, g, b) int tuple. Single source
    of truth for hex parsing — used by renderer.py, assets.py, timeline.py."""
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def relative_luminance(hex_color):
    """WCAG relative luminance of a '#rrggbb' color, in [0, 1]."""
    def _linearize(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (_linearize(c) for c in hex_to_rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a, hex_b):
    """WCAG contrast ratio between two '#rrggbb' colors (1.0 to 21.0)."""
    l_a = relative_luminance(hex_a)
    l_b = relative_luminance(hex_b)
    lighter, darker = max(l_a, l_b), min(l_a, l_b)
    return (lighter + 0.05) / (darker + 0.05)


# WCAG AA's "large text" contrast threshold (18pt+, or 14pt+ bold) rather
# than the stricter 4.5:1 normal-text threshold — title/subtitle text in
# this app is always rendered large and typically bold, so 3.0:1 is the
# bar that actually matches how the exported text will read.
MIN_TEXT_CONTRAST = 3.0

# ---------------------------------------------------------------------------
# TYPEFACES
# ---------------------------------------------------------------------------
# Blair's real brand fonts (Avenir Next LT Pro, Adobe Garamond Pro, Archer,
# Trajan Pro, Haettenschweiler, etc.) are commercial licenses. Where the
# genuine files are present in fonts/ (dropped in by Blair's design team),
# we use them directly. Where they're not available in the needed weight,
# we fall back to a free, open-license (SIL OFL) visual substitute so
# exported titles still read as on-brand. Bree Serif needs no substitute —
# it's already an approved Blair font AND freely licensed.
#
# Adobe Garamond Pro is present in fonts/ but only as Bold/Semibold/Italic
# (no true Regular weight was supplied), so "Garamond (elegant serif)"
# stays on the EB Garamond substitute rather than swap in a heavier weight
# under the "regular" label. Trajan Pro has no file in fonts/ at all, so
# "Trajan-style" stays on Cinzel.

FONTS = {
    "Garamond (elegant serif)":      {"regular": "EBGaramond-Regular.ttf", "italic": "EBGaramond-Italic.ttf",
                                       "brand_match": "Adobe Garamond Pro (no Regular weight supplied — using substitute)"},
    "Trajan-style (formal caps)":    {"regular": "Cinzel-Regular.ttf", "italic": "Cinzel-Regular.ttf",
                                       "brand_match": "Trajan Pro (not supplied — using substitute)"},
    "Bree Serif (friendly display)": {"regular": "BreeSerif-Regular.ttf", "italic": "BreeSerif-Regular.ttf",
                                       "brand_match": "Bree Serif (exact brand font)"},
    "Bold Impact (pop display)":     {"regular": "Haettenschweiler-Regular.ttf", "italic": "Haettenschweiler-Regular.ttf",
                                       "brand_match": "Haettenschweiler (genuine brand font)"},
    "Modern Sans (clean body)":      {"regular": "AvenirNextLTPro-Regular.otf", "italic": "AvenirNextLTPro-It.otf",
                                       "brand_match": "Avenir Next LT Pro (genuine brand font)"},
    "Modern Sans SemiBold":          {"regular": "AvenirNextLTPro-Demi.otf", "italic": "AvenirNextLTPro-Demi.otf",
                                       "brand_match": "Avenir Next LT Pro Demi (genuine brand font)"},
    "Modern Sans Bold":              {"regular": "AvenirNextLTPro-Bold.otf", "italic": "AvenirNextLTPro-BoldIt.otf",
                                       "brand_match": "Avenir Next LT Pro Bold (genuine brand font)"},
    "Condensed Sans (banner)":       {"regular": "AvenirNextLTPro-Cn.otf", "italic": "AvenirNextLTPro-Cn.otf",
                                       "brand_match": "Avenir Next Condensed (genuine brand font)"},
}


def font_path(font_key, style="regular"):
    entry = FONTS[font_key]
    return os.path.join(FONT_DIR, entry.get(style, entry["regular"]))


# ---------------------------------------------------------------------------
# LOGO / SEAL ASSETS
# ---------------------------------------------------------------------------
# NOTE: several of the source files shipped in the project (blair_seal_white.png,
# blair_seal_ribbon_white.png, Blair_B_white.png, and the *_KO.png files) were
# supplied as flattened white-on-white images with no alpha channel, so they
# render blank. Rather than use those broken files, this app derives clean
# transparent + white-knockout versions programmatically from the working
# colored artwork at runtime (see assets.py). This is more reliable and means
# any color variant can be generated on demand without extra source files.

LOGO_SOURCES = {
    "Blair Seal (crest)":            "blair_seal_Blue.png",
    "Blair Seal + Ribbon":           "blair_seal_ribbon_blue.png",
    "Blair Seal (horizontal lockup)": "blair_seal_horizontal.png",
    "Blair Seal (vertical lockup)":  "blair_seal_vertical.png",
    "Blair \"B\" Monogram":          "BlairB_PMS_289.png",
}

TRADEMARK_NOTICE = (
    "Official Blair Academy asset — this app is sanctioned by Blair Academy "
    "Communications for internal use. All seals and logo lockups are cleared "
    "for use in this tool."
)

# ---------------------------------------------------------------------------
# CANVAS / ASPECT RATIO PRESETS
# ---------------------------------------------------------------------------
CANVAS_PRESETS = {
    "16:9 Landscape (video)":              (1920, 1080),
    "9:16 Vertical (Stories / Reels / TikTok)": (1080, 1920),
    "1:1 Square (Instagram feed)":         (1080, 1080),
    "4:5 Portrait (Instagram feed)":       (1080, 1350),
}
DEFAULT_CANVAS_PRESET = "16:9 Landscape (video)"

# ---------------------------------------------------------------------------
# LAYOUTS
# ---------------------------------------------------------------------------
LAYOUTS = ["Full Title Card", "Lower Third"]
DEFAULT_LAYOUT = "Full Title Card"

BACKGROUND_STYLES = ["Solid", "Gradient"]

ANIMATIONS = ["fade", "slide", "zoom", "bounce", "wipe", "stagger", "typewriter", "none"]
OUTRO_ANIMATIONS = ["fade", "slide", "zoom", "wipe", "none"]
DEFAULT_OUTRO = "fade"

LOWER_THIRD_POSITIONS = ["Bottom Left", "Bottom Center", "Bottom Right",
                          "Top Left", "Top Center", "Top Right"]
DEFAULT_LOWER_THIRD_POSITION = "Bottom Left"

VIGNETTE_SHAPES = ["Elliptical", "Circular", "Rectangular"]
DEFAULT_VIGNETTE_SHAPE = "Elliptical"

# ---------------------------------------------------------------------------
# DARK UI THEME (app chrome only — does not affect rendered output)
# ---------------------------------------------------------------------------
UI_DARK_BG = "#1e1f22"
UI_PANEL_BG = "#26272b"
UI_PANEL_BG_ALT = "#2e3035"
UI_BORDER = "#3f4045"
UI_TEXT = "#e6e6e6"
UI_TEXT_DIM = "#9a9a9a"
UI_ACCENT = "#5f8fb4"   # Blair Sky, used for focus/selection accents in the UI chrome
UI_ENTRY_BG = "#333438"

# ---------------------------------------------------------------------------
# STYLE PRESETS — map a mood to on-brand color + type + motion defaults
# ---------------------------------------------------------------------------
PRESETS = {
    "Clean & Elegant": {
        "bg_color": "#004b8d",        # Blair Blue
        "accent_color": "#dd971a",    # Yellow, used sparingly as a rule/divider
        "text_color": "#ffffff",
        "title_font": "Trajan-style (formal caps)",
        "subtitle_font": "Garamond (elegant serif)",
        "uppercase_title": True,
        "letter_spacing": 6,
        "divider": True,
        "animation": "fade",
        "duration": 4.0,
        "logo": "Blair Seal (crest)",
        "logo_placement": "bottom-center",
        "logo_color_mode": "white",   # seal's own blue would vanish on Blair Blue
    },
    "Pop & Upbeat": {
        "bg_color": "#f15d22",        # Orange
        "accent_color": "#dd971a",    # Yellow
        "text_color": "#ffffff",
        "title_font": "Bold Impact (pop display)",
        "subtitle_font": "Modern Sans Bold",
        "uppercase_title": True,
        "letter_spacing": 1,
        "divider": False,
        "animation": "bounce",
        "duration": 3.0,
        "logo": "Blair \"B\" Monogram",
        "logo_placement": "top-right",
        "logo_color_mode": "original",  # navy on orange already reads clearly
    },
    "Traditional / Formal": {
        "bg_color": "#093266",        # Dark Blue
        "accent_color": "#72808a",    # Cool Grey
        "text_color": "#ffffff",
        "title_font": "Trajan-style (formal caps)",
        "subtitle_font": "Garamond (elegant serif)",
        "uppercase_title": True,
        "letter_spacing": 8,
        "divider": True,
        "animation": "fade",
        "duration": 5.0,
        "logo": "Blair Seal + Ribbon",
        "logo_placement": "bottom-center",
        "logo_color_mode": "white",   # avoid navy-on-navy
    },
    "Athletics / High Energy": {
        "bg_color": "#002244",        # Athletic Blue
        "accent_color": "#da1a32",    # Red
        "text_color": "#ffffff",
        "title_font": "Bold Impact (pop display)",
        "subtitle_font": "Condensed Sans (banner)",
        "uppercase_title": True,
        "letter_spacing": 2,
        "divider": False,
        "animation": "slide",
        "duration": 2.5,
        "logo": "Blair \"B\" Monogram",
        "logo_placement": "top-left",
        "logo_color_mode": "white",   # avoid navy-on-navy
    },
    "Warm / Community": {
        "bg_color": "#c6671d",        # Burnt Orange
        "accent_color": "#74a333",    # Green
        "text_color": "#ffffff",
        "title_font": "Bree Serif (friendly display)",
        "subtitle_font": "Modern Sans (clean body)",
        "uppercase_title": False,
        "letter_spacing": 0,
        "divider": True,
        "animation": "fade",
        "duration": 4.0,
        "logo": "Blair Seal (crest)",
        "logo_placement": "bottom-center",
        "logo_color_mode": "original",  # navy seal reads fine on burnt orange
    },
}

DEFAULT_PRESET = "Clean & Elegant"

CANVAS_SIZE = CANVAS_PRESETS[DEFAULT_CANVAS_PRESET]   # legacy default, prefer scene["canvas_size"]
FPS = 30
