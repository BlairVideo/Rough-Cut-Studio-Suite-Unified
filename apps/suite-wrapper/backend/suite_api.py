"""
suite_api.py — the single js_api object for the Studio Suite window.

SuiteApi SUBCLASSES Rough Cut Studio's Api (imported from the sibling
app's backend, which is inserted onto sys.path here) so the untouched RCS
frontend keeps calling window.pywebview.api.* exactly as it always has;
everything suite-specific is added under suite_/transcriber_/broll_/
brander_/sync_ prefixes, which cannot collide with RCS's method names.

Error contract (matches RCS's own style): every public method returns a
dict with at least {"ok": bool} and {"error": str} when not ok — nothing
raises across the JS bridge. pywebview dispatches each call on a worker
thread, so all cross-call state lives in the thread-safe JobManager (or in
RCS's own lock-guarded state).
"""

import os
import sys
import json
import time
import traceback
import subprocess

try:
    from . import (paths, handoff, brander_bridge, brander_gemini, sync_xml,
                   synced_audio_splice, favorites, braw_bridge)
    from .jobs import get_job_manager
    from .api_shared import *  # noqa: F401,F403 — re-exported: pre-split
    #   callers (main.py --selftest, verify scripts) read constants like
    #   WHISPER_MODELS / IVT_CACHE_SUFFIX off THIS module's namespace.
    from .api_security import SecurityMixin
    from .api_transcriber import TranscriberMixin
    from .api_broll import BrollMixin
    from .api_favorites import FavoritesMixin
    from .api_sync import SyncMixin
    from .api_brander import BranderMixin
    from .api_cardeater import CardEaterMixin, CardEaterState
    from .api_harmonize import HarmonizeMixin
    from .api_pipeline import PipelineMixin
    from .api_colorize import ColorizeMixin
    from .api_spyglass import SpyglassMixin
    from . import cardeater_copy, notify, spyglass_bridge
except ImportError:  # pragma: no cover — direct script import in tests
    import paths
    import handoff
    import brander_bridge
    import brander_gemini
    import sync_xml
    import synced_audio_splice
    import favorites
    import braw_bridge
    from jobs import get_job_manager
    from api_shared import *  # noqa: F401,F403
    from api_security import SecurityMixin
    from api_transcriber import TranscriberMixin
    from api_broll import BrollMixin
    from api_favorites import FavoritesMixin
    from api_sync import SyncMixin
    from api_brander import BranderMixin
    from api_cardeater import CardEaterMixin, CardEaterState
    from api_harmonize import HarmonizeMixin
    from api_pipeline import PipelineMixin
    from api_colorize import ColorizeMixin
    from api_spyglass import SpyglassMixin
    import cardeater_copy
    import notify
    import spyglass_bridge

# api_shared already put RCS's backend dir on sys.path; keep the guard for
# direct-import edge cases.
if paths.RCS_BACKEND_DIR not in sys.path:
    sys.path.insert(0, paths.RCS_BACKEND_DIR)

import webview            # noqa: E402
import api as rcs_api     # noqa: E402  (Rough Cut Studio's backend api module)
from api import Api       # noqa: E402  (Rough Cut Studio's Api)


class SuiteApi(SecurityMixin, TranscriberMixin, BrollMixin, FavoritesMixin,
               SyncMixin, BranderMixin, CardEaterMixin, HarmonizeMixin,
               PipelineMixin, ColorizeMixin, SpyglassMixin, Api):
    """Composed js_api (contract A-1): each workspace's methods live in
    its own mixin module (api_security / api_transcriber / api_broll /
    api_favorites / api_sync / api_brander / api_cardeater /
    api_harmonize / api_pipeline / api_colorize / api_spyglass). RCS's Api
    is LAST in the MRO, so every mixin override of an inherited method
    (SecurityMixin's key/autosave/transcript-path overrides, this class's
    save_xml) wins, and their super() calls fall through to RCS unchanged.
    What stays in this file: __init__, the Jobs surface (incl. the shared
    _require_window/_finished_job helpers), and the save_xml splice
    override (Edit-workspace integration)."""

    def __init__(self):
        super().__init__()
        paths.ensure_suite_dirs()
        self.jobs = get_job_manager()
        self.favorites = favorites.load()
        # Spyglass: unlike every other mixin here, this starts a real
        # engine (background gap-fill/rescan/volume-watch loops on its own
        # Tokio runtime, linked in-process via the compiled spyglass_core
        # extension) rather than just initializing plain Python state --
        # mirrors CardEaterState's eager start_watcher() below and
        # Spyglass's own standalone Tauri `setup()`, both of which start
        # their background loops unconditionally at launch. Best-effort:
        # a not-yet-built extension logs a traceback and leaves the Search
        # tab non-functional rather than failing suite startup entirely.
        spyglass_bridge.try_eager_init()
        # SEC-3: RCS's crash-recovery autosave (inherited _autosave_path)
        # otherwise lands at a fixed, world-predictable name in the shared
        # temp dir and contains verbatim transcript text (PII). Relocate it
        # to a private per-user support dir and lock the file down to 0600
        # (see _autosave / autosave_working_state overrides below).
        self._autosave_path = self._suite_autosave_path()
        # Card Eater workspace: one SQLite connection + active-card registry,
        # fed by a background /Volumes poller (see cardeater_volume_watcher.py).
        self._cardeater = CardEaterState(paths.CARDEATER_DB)
        self._cardeater.start_watcher()


    # =====================================================================
    # Jobs
    # =====================================================================

    def suite_list_jobs(self):
        """Merges Card Eater's copy destinations (kind "cardeater_copy",
        one entry per destination) in with jobs.py's own subprocess/thread
        jobs, so the Jobs drawer is the single place background work of
        ANY kind shows up — the Copy workspace has no queue panel of its
        own (see cardeater_copy.list_as_generic_jobs's own docstring)."""
        try:
            cardeater_jobs = cardeater_copy.list_as_generic_jobs(self._cardeater)
            return {"ok": True, "jobs": cardeater_jobs + self.jobs.list_jobs()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cancel_job(self, job_id):
        try:
            return self.jobs.cancel(job_id)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_clear_finished_jobs(self):
        try:
            self.jobs.clear_finished()
            cardeater_copy.clear_finished(self._cardeater)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_braw_status(self):
        """Whether BRAW (.braw) files can be decoded on this machine at
        all — cross-workspace, not B-Roll-specific, so it lives here
        rather than in any one workspace mixin. A future frontend can use
        this to show a "BRAW not available" hint up front instead of only
        surfacing it per-clip after a folder is analyzed."""
        try:
            return {"ok": True, **braw_bridge.status()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_native_notify(self, title, message):
        """Best-effort native (Notification Center) alert for a finished
        background job — the frontend only calls this when the window
        isn't focused (suite.js maybeNativeNotify), so this never
        duplicates the in-app toast the user is already looking at."""
        try:
            notify.send_native_notification(title, message)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_proxy_cache_info(self):
        """assets/proxies/ usage for the Settings panel's cache display —
        cross-workspace (every BRAW-touching workspace shares this one
        cache), so it lives here rather than in any one mixin."""
        try:
            return {"ok": True, **braw_bridge.cache_usage()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_clear_proxy_cache(self):
        try:
            removed = braw_bridge.clear_cache()
            return {"ok": True, "removed": removed}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # ---------- shared helpers ----------

    def _require_window(self):
        """Native dialogs need the pywebview window, which main.py assigns
        only after create_window returns — same guard RCS uses."""
        if not self.window:
            return {"ok": False, "error": "The app window isn't ready yet — try again in a moment."}
        return None

    def _finished_job(self, job_id, kind):
        """Fetch a job that must exist, match `kind`, be done, and carry a
        result. Returns (job, None) or (None, error_dict)."""
        job = self.jobs.get_job(job_id)
        if job is None:
            return None, {"ok": False, "error": f"Unknown job id: {job_id}"}
        if job.kind != kind:
            return None, {"ok": False, "error": f"Job {job_id} is a '{job.kind}' job, not '{kind}'."}
        if job.status != "done" or not isinstance(job.result, dict):
            return None, {"ok": False, "error": "That job hasn't finished successfully yet."}
        return job, None


    # =====================================================================
    # Edit-workspace export override: splice synced external audio
    # =====================================================================

    def save_xml(self):
        """Override Rough Cut Studio's Premiere XMEML export so that any
        main cut sourced from a synced clip (Sync workspace → transcribe →
        Send to Edit) also carries its separately-recorded external audio,
        offset-aligned, as its own non-merged audio track(s).

        If nothing in the current cut is synced, this is a no-op passthrough
        to RCS's stock export (identical file + dialog), so normal projects
        are completely unaffected. A splice FAILURE also falls back to the
        stock export rather than blocking the user — but is surfaced in the
        returned dict (A-4: a silent fallback would silently drop synced
        audio from the export with no user-visible signal)."""
        if not self.last_result:
            return super().save_xml()
        try:
            base_xml = self.last_result.get("xml")
            resolved = self.last_result.get("resolved_segments") or []
            # Addendum v5: let discovery fall back to each source's own
            # ingested transcript file when neither the sync sidecar nor
            # the transcription cache survive next to the video.
            source_paths = {
                sid: (self.sources.get(sid) or {}).get("path")
                for sid in {seg.get("source_id") for seg in resolved}
            }
            new_xml, warnings = synced_audio_splice.splice_external_audio(
                base_xml, resolved, dict(self.media_paths), float(self.fps),
                source_paths=source_paths)
            if not new_xml:
                # No synced sources in this cut — behave exactly like RCS.
                return super().save_xml()
            res = self._save_text(
                new_xml,
                default_name=f"{self._safe_name(self.last_result['sequence_name'])}_premiere.xml",
                file_types=("Premiere XML (*.xml)", "All files (*.*)"),
            )
            if isinstance(res, dict) and res.get("ok") and warnings:
                res["warnings"] = warnings
            return res
        except Exception as e:
            traceback.print_exc()
            res = super().save_xml()
            # A-4: tell the user the export happened WITHOUT the synced
            # audio instead of pretending nothing went wrong. The export
            # button's handler only reads ok/path/error, so a "warnings"
            # key alone would be invisible — push the message through
            # RCS's own status bar via evaluate_js, the same
            # backend->frontend pattern RCS's _notify_retry uses.
            if isinstance(res, dict) and res.get("ok"):
                msg = ("Saved WITHOUT synced external audio — the splice "
                       f"failed ({e.__class__.__name__}) and the stock "
                       "export was used. See the terminal log.")
                res.setdefault("warnings", []).append(msg)
                if self.window:
                    try:
                        payload = json.dumps(msg)
                        # Deferred: the export button's own handler writes a
                        # "saved to ..." success status as soon as this call
                        # returns, which would immediately overwrite an
                        # instant push. 600ms lands the warning after it.
                        self.window.evaluate_js(
                            "setTimeout(function () { "
                            f"window.setStatus && window.setStatus({payload}, \"error\"); "
                            "}, 600)"
                        )
                    except Exception:
                        traceback.print_exc()  # visible in console, never blocks the save
            return res

    # =====================================================================
    # Edit-workspace BRAW substitution (Phase 2, remainder)
    # =====================================================================
    # RCS's own Api/SourceManager never learn BRAW exists -- every method
    # below reuses RCS's real logic via super(), just briefly pointing
    # self.media_paths at a cached proxy for the duration of the call, the
    # same "resolve the decode path, do the real work, put the original
    # back" idea broll_worker.py's run_analyze already uses (there via a
    # local variable; here via the shared media_paths dict, since that's
    # what RCS's own SourceManager reads internally and it's a plain
    # mutable dict — sources.py's SourceManager.__init__ + api.py's
    # media_paths @property, no RCS file needs touching to swap it).
    #
    # Metadata-only sites that reference media_paths for DISPLAY or for
    # paths external tools resolve themselves (_display_clip_name,
    # _build_project_dict/_apply_loaded_project_unsafe, _finalize_outputs'
    # XML/OTIO export) are deliberately NOT overridden here -- those must
    # keep referencing the ORIGINAL .braw file (this suite's "export
    # always references original media" convention), and a real NLE with
    # a BRAW plugin can decode it natively where Studio Suite's own
    # embedded ffmpeg pipeline can't.

    def _with_braw_proxy_substituted(self, source_id, call):
        """Run `call` (a zero-arg callable wrapping an RCS method call for
        one `source_id`) with self.media_paths[source_id] pointed at its
        cached BRAW proxy, if that source is a .braw file — restoring the
        original mapping afterward regardless of outcome. A non-.braw
        source (or one with no media linked at all) passes through
        completely unaffected."""
        original = self.media_paths.get(source_id)
        if not original or os.path.splitext(original)[1].lower() != braw_bridge.BRAW_EXTENSION:
            return call()
        proxy_path = braw_bridge.find_cached_proxy(original)
        if proxy_path is None:
            return {"ok": False, "error":
                    "This BRAW clip's proxy hasn't finished generating yet — "
                    "check the Jobs drawer."}
        self.media_paths[source_id] = proxy_path
        try:
            return call()
        finally:
            self.media_paths[source_id] = original

    def get_thumbnail(self, source_id, in_seconds):
        return self._with_braw_proxy_substituted(
            source_id, lambda: super(SuiteApi, self).get_thumbnail(source_id, in_seconds))

    def get_preview_url(self, source_id):
        return self._with_braw_proxy_substituted(
            source_id, lambda: super(SuiteApi, self).get_preview_url(source_id))

    def export_video_preview(self):
        """Like _with_braw_proxy_substituted, but export_video_preview
        (unlike get_thumbnail/get_preview_url) decodes EVERY main-track
        source in one ffmpeg run (api.py's build_preview_export), so every
        .braw source in the current cut needs substituting at once — bail
        out with one clear error if any of them lack a cached proxy yet,
        rather than a partial/confusing per-clip failure deep in ffmpeg."""
        swapped = {}
        try:
            for source_id, path in list(self.media_paths.items()):
                if path and os.path.splitext(path)[1].lower() == braw_bridge.BRAW_EXTENSION:
                    proxy_path = braw_bridge.find_cached_proxy(path)
                    if proxy_path is None:
                        return {"ok": False, "error":
                                "One or more BRAW clips' proxies haven't finished "
                                "generating yet — check the Jobs drawer."}
                    swapped[source_id] = path
                    self.media_paths[source_id] = proxy_path
            return super().export_video_preview()
        finally:
            for source_id, original in swapped.items():
                self.media_paths[source_id] = original

    def link_media_file(self, source_id):
        """Like B-Roll/Sync's own start actions, eagerly queue proxy
        generation the moment a .braw file is manually linked in Edit —
        fire-and-forget, so it's more likely to already be cached by the
        time a preview/thumbnail/export is first requested."""
        result = super().link_media_file(source_id)
        if isinstance(result, dict) and result.get("ok") and result.get("media_path"):
            braw_bridge.queue_missing_proxies(self.jobs, [result["media_path"]])
        return result

    def batch_relink_media(self):
        """Same eager-queue idea as link_media_file, for every .braw file
        RCS's own folder-scan stem-matching just linked (VIDEO_EXTENSIONS
        now includes .braw — Rough Cut Studio/backend/transcript_parser.py
        — so RCS's own batch_relink_media already matches them itself, no
        other change needed there)."""
        result = super().batch_relink_media()
        braw_bridge.queue_missing_proxies(self.jobs, self.media_paths.values())
        return result
