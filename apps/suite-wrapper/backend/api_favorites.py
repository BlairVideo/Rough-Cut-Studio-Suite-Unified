"""
api_favorites.py — FavoritesMixin: the Favorites feature (list/toggle/remove, add-to-cuts).

Split out of suite_api.py (contract A-1). This module defines ONE mixin
class; backend/suite_api.py composes every mixin with Rough Cut Studio's
Api into the single SuiteApi js_api object — the mixins are never
instantiated on their own. Cross-workspace attributes (self.window,
self.jobs, self._require_window, self._read_ivt_cache, RCS state like
self.sources/self.media_paths) resolve through the composed class at
runtime, exactly as they did pre-split.
"""

import os
import sys
import json
import time
import traceback
import subprocess

try:
    from . import (paths, handoff, brander_bridge, brander_gemini, sync_xml,
                   synced_audio_splice, favorites)
    from .jobs import get_job_manager
    from .api_shared import *  # noqa: F401,F403 — shared constants + helpers
except ImportError:  # pragma: no cover — direct script import in tests
    import paths
    import handoff
    import brander_bridge
    import brander_gemini
    import sync_xml
    import synced_audio_splice
    import favorites
    from jobs import get_job_manager
    from api_shared import *  # noqa: F401,F403

# api_shared put RCS's backend dir on sys.path (same bootstrap RCS's own
# main.py performs), so RCS backend modules are importable from here on.
import webview            # noqa: E402
import api as rcs_api     # noqa: E402  (Rough Cut Studio's backend api module)


class FavoritesMixin:
    # =====================================================================
    # Favorites (contract addendum v6; range-based matching v7; B-Roll tab
    # entries reuse this same store as kind="broll" — see
    # BrollMixin.broll_send_to_edit, which populates them directly rather
    # than through a favorite toggle; cleared on new/load project,
    # editorial notes v16; saved/loaded WITH the project file v17)
    # =====================================================================

    def suite_list_favorites(self):
        try:
            favs = sorted(self.favorites, key=lambda f: f.get("created_at", ""), reverse=True)
            return {"ok": True, "favorites": favs}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def _toggle_favorite_range(self, vtt_path, source_id, start_seconds, end_seconds,
                                start_tc, end_tc, speaker, text, index,
                                kind="transcript", clip_path=None, score=None):
        """Shared toggle body for suite_toggle_favorite (transcript modal,
        has a segment index) and suite_toggle_favorite_range (Cuts row /
        preview window, no segment index) — matching is always by time
        range (favorites.find), never by index or kind. B-Roll tab entries
        (kind="broll") are NOT created through this toggle — see
        BrollMixin.broll_send_to_edit, which appends them directly since
        "Send to Edit" has no on/off toggle semantic."""
        existing = favorites.find(self.favorites, vtt_path, start_seconds, end_seconds)
        if existing:
            self.favorites = [f for f in self.favorites if f is not existing]
            favorites.save(self.favorites)
            return {"ok": True, "favorited": False, "favorite": None}
        fav = favorites.build(vtt_path, source_id, start_seconds, end_seconds,
                               start_tc, end_tc, speaker, text, index,
                               kind=kind, clip_path=clip_path, score=score)
        self.favorites.append(fav)
        favorites.save(self.favorites)
        return {"ok": True, "favorited": True, "favorite": fav}

    def suite_toggle_favorite(self, source_id, index):
        """Favorite/unfavorite one transcript line. `source_id` must be a
        currently loaded source — favoriting only ever happens from an
        already-open transcript modal, so the lazy re-ingest path
        (suite_favorite_add_to_cuts) doesn't apply here."""
        try:
            entry = self.sources.get(source_id)
            if not entry:
                return {"ok": False, "error": "Unknown source."}
            vtt_path = entry["path"]
            segment = next((s for s in entry["segments"] if s.index == index), None)
            if segment is None:
                return {"ok": False, "error": "That segment no longer exists."}
            return self._toggle_favorite_range(
                vtt_path, source_id, segment.start_seconds, segment.end_seconds,
                segment.start_tc, segment.end_tc, segment.speaker, segment.text, index)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_toggle_favorite_range(self, source_id, start_seconds, end_seconds,
                                     text="", speaker=""):
        """Favorite/unfavorite an arbitrary time range on a currently
        loaded source — used by the Cuts-table star and the preview-window
        star, since a Cuts row's in/out may not align with any parsed
        transcript segment (manually added/edited rows, B-roll clips)."""
        try:
            entry = self.sources.get(source_id)
            if not entry:
                return {"ok": False, "error": "Unknown source."}
            vtt_path = entry["path"]
            start_seconds = float(start_seconds)
            end_seconds = float(end_seconds)

            def tc(seconds):
                res = self.format_timecode(seconds)
                return res["tc"] if isinstance(res, dict) and res.get("ok") else ""

            return self._toggle_favorite_range(
                vtt_path, source_id, start_seconds, end_seconds,
                tc(start_seconds), tc(end_seconds), speaker, text, None)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_update_favorite_note(self, favorite_id, note):
        """Editorial note attached to a favorite (addendum v16), stored
        kind-neutrally on the favorite dict itself — currently only
        surfaced in the B-Roll tab's cards, but not restricted to
        kind:"broll" since there's no reason a transcript favorite
        couldn't grow the same UI later."""
        try:
            fav = next((f for f in self.favorites if f.get("id") == favorite_id), None)
            if not fav:
                return {"ok": False, "error": "That favorite no longer exists."}
            fav["note"] = str(note or "")
            favorites.save(self.favorites)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_remove_favorite(self, favorite_id):
        try:
            self.favorites = [f for f in self.favorites if f.get("id") != favorite_id]
            favorites.save(self.favorites)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def _build_project_dict(self, meta=None):
        """Override RCS's own project-file serialization (addendum v17) so
        Favorites (both kinds — transcript AND B-Roll, they share one list)
        travel WITH the project file itself, not just the suite-wide
        favorites.json. RCS's save_project/save_project_to_path/_autosave/
        autosave_working_state all build the saved dict through this one
        method, so this is the single place that needs to change — no
        other override for "save" is needed."""
        project = super()._build_project_dict(meta)
        project["favorites"] = self.favorites
        return project

    def new_project(self, *args, **kwargs):
        """Override RCS's own new_project so Favorites are reset along with
        everything else (addendum v16). A blank slate has no project file
        to carry favorites in, so this always clears to []. Only clears on
        success (a generation-lock conflict returns {"ok": False} and must
        leave favorites untouched)."""
        result = super().new_project(*args, **kwargs)
        if isinstance(result, dict) and result.get("ok"):
            self.favorites = []
            favorites.save(self.favorites)
        return result

    def load_project(self, *args, **kwargs):
        """Loads the JUST-OPENED project's own favorites (addendum v17),
        replacing whatever was in memory — fixing the reported bug where
        opening a different project kept showing the previous project's
        favorited lines/clips. Re-reads the same file RCS's own
        load_project just parsed (by path, returned in its result) rather
        than hooking the shared _apply_loaded_project_unsafe: that method
        is also used by restore_autosave (crash recovery of the SAME
        session, which must never touch favorites), and the two callers
        aren't otherwise distinguishable at that layer. An older project
        file saved before this feature existed has no "favorites" key —
        treated the same as an explicitly empty list, not "leave
        unchanged", so switching to it still clears out the previous
        project's favorites as expected. Only touches favorites on
        success — a cancelled file dialog or a rejected/corrupt project
        file returns {"ok": False} (or {"cancelled": True}) and must leave
        the current favorites alone."""
        result = super().load_project(*args, **kwargs)
        if isinstance(result, dict) and result.get("ok"):
            self.favorites = self._read_favorites_from_project_file(result.get("path"))
            favorites.save(self.favorites)
        return result

    @staticmethod
    def _read_favorites_from_project_file(path):
        if not path:
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                project = json.load(f)
        except Exception:
            return []
        raw = project.get("favorites") if isinstance(project, dict) else None
        if not isinstance(raw, list):
            return []
        return [f for f in raw if isinstance(f, dict)]

    def suite_favorite_add_to_cuts(self, favorite_id):
        """Push a favorited line/segment (or a B-Roll tab entry) into the
        Cuts table, re-ingesting its backing VTT first if that source
        isn't currently loaded (it may have been made/sent in an earlier
        session). A "broll"-kind entry becomes a broll-track cut — same
        VTT re-ingest path works unchanged for it, since
        BrollMixin.broll_send_to_edit already wrote a real (synthetic,
        single-cue) VTT via handoff.ensure_broll_source at send time, not
        just at add-to-cuts time."""
        try:
            fav = next((f for f in self.favorites if f.get("id") == favorite_id), None)
            if not fav:
                return {"ok": False, "error": "That favorite no longer exists."}
            vtt_path = fav["vtt_path"]
            source_id = fav["source_id"]
            if source_id not in self.sources:
                if not os.path.isfile(vtt_path):
                    return {"ok": False, "error": "That favorite's source file is missing — can't add it to Cuts."}
                info = handoff.reingest_source(self, vtt_path)
                source_id = info["source_id"]
            track = "broll" if fav.get("kind") == "broll" else "main"
            cut = handoff.build_cut_spec(
                self, source_id, fav["start_seconds"], fav["end_seconds"], track=track)
            cut["source_text"] = fav.get("text") or ""
            return {"ok": True, "cut": cut}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}
