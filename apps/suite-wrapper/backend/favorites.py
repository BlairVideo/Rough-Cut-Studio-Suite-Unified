"""favorites.py — persistent store for favorited transcript lines, and
(reusing the same store as kind="broll") segments sent to the B-Roll tab
from the B-Roll workspace (contract addenda v6, v15, v16).

Entries are keyed by (vtt_path, index), not just `source_id`: RCS's
`self.sources` is rebuilt empty every launch and only repopulated when a
transcript is actually loaded into Edit that session, but the generated
VTT under assets/transcripts/ is permanent. Storing the VTT path lets
"add to Cuts" re-ingest the source on demand (see SuiteApi.
suite_favorite_add_to_cuts) instead of an entry silently going stale
whenever its source isn't currently loaded.

One store, two `kind`s (v15): B-Roll tab entries reuse this exact same
schema/matching rather than a parallel identity scheme — see `build()`'s
docstring for why that's safe. Unlike transcript favorites, B-Roll
entries have no on/off toggle — BrollMixin.broll_send_to_edit appends
them directly when segments are checked and sent.
"""

import json
import os
import uuid
from datetime import datetime, timezone

try:  # package import (suite runtime) vs. direct script import (tests)
    from . import paths
except ImportError:  # pragma: no cover
    import paths


def load():
    """The full favorites list, newest-created first. Any read failure
    (missing file, corrupt JSON) returns [] — same fail-open policy as
    every other sidecar store in this project."""
    try:
        with open(paths.FAVORITES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return data
    except Exception:
        return []


def save(favorites):
    paths.ensure_suite_dirs()
    with open(paths.FAVORITES_FILE, "w", encoding="utf-8") as f:
        json.dump(favorites, f, indent=2)


def new_id():
    return "f_" + uuid.uuid4().hex[:8]


def find(favorites, vtt_path, start_seconds, end_seconds, tol=0.05):
    """The existing favorite covering the same (vtt_path, start, end)
    range, or None. Matching is by TIME RANGE (contract addendum v7,
    replacing the original index-based match) so a cut whose in/out
    doesn't align with any parsed transcript segment — a manually added
    or edited Cuts row, a B-roll clip — can still be favorited directly
    from the Cuts table or the preview window, not just the transcript
    modal."""
    vtt_path = os.path.abspath(vtt_path)
    for fav in favorites:
        if os.path.abspath(fav.get("vtt_path", "")) != vtt_path:
            continue
        if abs(float(fav.get("start_seconds", 0.0) or 0.0) - start_seconds) <= tol and \
           abs(float(fav.get("end_seconds", 0.0) or 0.0) - end_seconds) <= tol:
            return fav
    return None


def build(vtt_path, source_id, start_seconds, end_seconds, start_tc="", end_tc="",
          speaker="", text="", index=None, kind="transcript", clip_path=None, score=None):
    """New favorite entry. `index` (the segment's position within a parsed
    transcript) is optional display metadata only — never part of the
    matching key (see `find`) — and is None for a favorite made from a
    Cuts row or the preview window, where there is no parsed segment.

    `kind` distinguishes which suite.js tab an entry belongs to:
    "transcript" (the original Favorites tab — a transcript line, a Cuts
    row, or the preview window) or "broll" (the B-Roll tab — a scored
    segment from the B-Roll Analyzer, checked and "sent" there before
    it's ever placed on Cuts). It plays NO role in `find()`'s matching,
    which stays purely vtt_path + seconds — a B-Roll entry's vtt_path is
    the SAME synthetic single-cue VTT `handoff.ensure_broll_source`
    already writes at send time, so the two kinds can never collide
    there. `clip_path`/`score` are B-Roll-only display metadata, None for
    a transcript favorite. `note` (v16) is a free-text editorial note the
    user can attach after the fact via suite_update_favorite_note — always
    starts empty here; set separately so toggling a favorite off/on never
    has a note to preserve or lose."""
    return {
        "id": new_id(),
        "kind": kind,
        "vtt_path": vtt_path,
        "source_id": source_id,
        "index": index,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "start_tc": start_tc,
        "end_tc": end_tc,
        "speaker": speaker or "",
        "text": text or "",
        "clip_path": clip_path,
        "score": score,
        "note": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
