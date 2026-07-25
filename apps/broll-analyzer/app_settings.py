"""
app_settings.py
Persists the app's option controls (segment length, energy weight,
worker count, last-used folder, etc.) between runs, so re-opening the
app doesn't reset everything to defaults.

Stored as a single plain JSON file in the user's home directory:
"~/.broll_analyzer_settings.json". This is the same trust model as the
existing per-folder result cache (result_cache.py) -- a local,
human-readable file containing only the app's own UI option values
(numbers, short strings, one folder path). Nothing here is a
credential, nothing is executed, and nothing is ever transmitted
anywhere; loading and saving are both best-effort so a missing,
corrupt, or unwritable file can never stop the app from starting or
running.
"""

import os
import json

SETTINGS_FILENAME = ".broll_analyzer_settings.json"
SETTINGS_VERSION = 1

# Only these keys are ever read from or written to the settings file.
# Keeping an explicit allow-list means a hand-edited or unexpectedly
# structured settings file can't inject unexpected keys/values into
# the app -- unrecognized keys are simply ignored on load.
ALLOWED_KEYS = {
    "folder_path",
    "window_sec",
    "max_segments",
    "top_mode",
    "top_n",
    "min_score",
    "max_workers",
    "enable_energy",
    "energy_weight_pct",
    "sequence_order",
}


def settings_path() -> str:
    return os.path.join(os.path.expanduser("~"), SETTINGS_FILENAME)


def load_settings() -> dict:
    """Best-effort load: any problem (missing file, corrupt JSON, wrong
    version, permissions) just means starting with defaults, exactly as
    if this feature didn't exist."""
    path = settings_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("version") != SETTINGS_VERSION:
            return {}
        values = data.get("values", {})
        if not isinstance(values, dict):
            return {}
        return {k: v for k, v in values.items() if k in ALLOWED_KEYS}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}


def save_settings(values: dict) -> None:
    """Best-effort save via a temp file + atomic replace (same pattern
    as result_cache.save_cache), so a crash mid-write can't leave a
    corrupt settings file behind. Any failure (read-only home dir, disk
    full) is swallowed -- losing settings just means defaults next
    launch, never a broken app."""
    path = settings_path()
    tmp_path = path + ".tmp"
    payload = {
        "version": SETTINGS_VERSION,
        "values": {k: v for k, v in values.items() if k in ALLOWED_KEYS},
    }
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, path)
    except OSError:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
