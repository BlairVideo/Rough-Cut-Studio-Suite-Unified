"""
api_spyglass.py — SpyglassMixin: the Search workspace (Spyglass
integration — content-aware natural-language shot search over the whole
archive: type "students studying in the library" or "mascot cheering at a
football game" and get back the specific shots, cued to timecode).

Split out the same way every other workspace mixin is (contract A-1).
Unlike every sibling app's own mixin, Spyglass's engine is Rust, not
Python — there is no subprocess worker here, because the engine already
runs in-process (linked directly into this suite's Python process via the
compiled `spyglass_core` PyO3 extension — see spyglass_bridge.py). Every
method below is therefore a synchronous call into that extension, not a
`self.jobs.start_subprocess_job`/`start_thread_job` dispatch — Spyglass's
own commands.rs only wrapped its equivalent calls in Tauri's
`spawn_blocking` to keep Tauri's single UI thread unblocked; the direct
Python equivalent (releasing the GIL during the call) already happens
inside spyglass_core's own Rust implementation (`py.allow_threads`), so
there's nothing further this mixin needs to do to avoid blocking the rest
of the suite while a search or a scan runs.

Phase 3 adds tag/favorite mutations, pool tray CRUD, watched-root
management, gap-fill queue control, and consolidate/XMEML export.
`spyglass_scan_watched_root` is the one method here that DOES use
`self.jobs.start_thread_job` — not for GIL/blocking reasons (the PyO3
call already releases the GIL), but for the same UX reason `sync`/`broll`
jobs use it: a folder walk is genuinely slow, and routing it through the
Jobs drawer gives progress feedback and a way to see it's still running,
consistent with every other long-running action in this suite. Consolidate
export deliberately does NOT go through jobs.py — see spyglass_bridge.py's
`start_consolidate_export` docstring for why (Spyglass already owns a
complete, working progress-tracking mechanism of its own).

Phase 4 adds the native `AVPlayerView` click-to-play preview, ported from
Spyglass's own Tauri implementation. The one genuinely novel wrinkle:
pywebview dispatches every js_api call (these methods) on a worker
thread, but AppKit view manipulation is only safe on the main thread —
Tauri's `WebviewWindow::with_webview` guaranteed main-thread execution
for free, pywebview does not. `_run_on_main_thread` below bridges that
gap via `PyObjCTools.AppHelper.callAfter`, waiting on a `threading.Event`
so the call still resolves synchronously from the frontend's point of
view (an error opening a bad path surfaces as a real `{"ok": False}`,
not a silently-swallowed background failure).
"""

import datetime
import os
import threading
import traceback

try:
    from . import paths, spyglass_bridge
    from .api_shared import _first_path
except ImportError:  # pragma: no cover — direct script import in tests
    import paths
    import spyglass_bridge
    from api_shared import _first_path

import webview  # noqa: E402

# PyObjC/pywebview's cocoa backend -- this whole suite is macOS-only (see
# root CLAUDE.md), so no cross-platform fallback is needed; these imports
# are only ever missing in a broken/incomplete venv, which should surface
# loudly rather than degrade silently.
import objc  # noqa: E402
from PyObjCTools import AppHelper  # noqa: E402
from webview.platforms.cocoa import BrowserView  # noqa: E402

# Bound on how long a native-preview call waits for the main-thread
# dispatch to actually run (see `_run_on_main_thread`) before giving up
# and reporting a timeout -- generous, since this is just AppKit view
# setup, not real decode/analysis work.
_MAIN_THREAD_TIMEOUT_SECONDS = 5.0


class SpyglassMixin:
    # =====================================================================
    # Search (Spyglass integration)
    # =====================================================================

    def spyglass_search(self, query, filters=None, limit=None):
        """Natural-language shot search: embeds `query` via Spyglass's own
        persistent CLIP text-embedding server and ranks shots with its
        hybrid (vector + tag + transcript-keyword) scoring. `filters` is a
        plain dict shaped like Spyglass's `FacetFilters` (`{"tags": [...],
        "source_app": ..., "date_from": ..., "date_to": ...,
        "favorites_only": ..., "folder_path": ...}`); omit or pass `{}`
        for no filters. `limit` defaults to 60 (Rust-side default) —
        the frontend's "View more results" button re-issues the same
        search with a larger `limit` rather than a true offset, since the
        underlying engine re-ranks from scratch on every call anyway."""
        try:
            results = spyglass_bridge.search_shots(query or "", filters, limit)
            return {"ok": True, "results": results}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_browse(self, filters=None, limit=None):
        """Facet-only browsing (no text query) — also doubles as "browse
        the whole archive, most recent first" when `filters` is empty.
        `limit` follows the same "View more results" convention as
        `spyglass_search`."""
        try:
            results = spyglass_bridge.browse_shots(filters, limit)
            return {"ok": True, "results": results}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_find_similar(self, shot_id):
        """"Find shots like this" — visual-similarity search seeded from a
        reference shot already in view."""
        try:
            results = spyglass_bridge.find_similar_shots(shot_id)
            return {"ok": True, "results": results}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_list_facets(self):
        """Populates the facet sidebar: every tag/source value with its
        shot count, plus the archive's ingested-date bounds."""
        try:
            facets = spyglass_bridge.list_facet_options()
            return {"ok": True, "facets": facets}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_list_folder_children(self, parent_path=None):
        """Folder-tree panel: `parent_path=None` returns the watched roots
        themselves as top-level nodes; `parent_path=<a node's own path>`
        lazily expands one level deeper. See spyglass_bridge.py /
        spyglass_core::folders for why this is derived on demand rather
        than a stored tree."""
        try:
            nodes = spyglass_bridge.list_folder_children(parent_path)
            return {"ok": True, "nodes": nodes}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_list_favorites(self):
        try:
            results = spyglass_bridge.list_favorite_shots()
            return {"ok": True, "results": results}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Tag correction / favoriting
    # =====================================================================

    def spyglass_add_tag(self, shot_id, label):
        try:
            spyglass_bridge.add_tag(shot_id, (label or "").strip())
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_remove_tag(self, shot_id, label):
        try:
            spyglass_bridge.remove_tag(shot_id, label)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_set_favorite(self, shot_id, favorite):
        try:
            spyglass_bridge.set_shot_favorite(shot_id, favorite)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_purge_onscreen_text_tags(self):
        """Retroactive cleanup for tags the VLM pass generated before
        TAGS_PROMPT (sidecar/analyze_clip.py) told it not to transcribe
        on-screen text or describe/count subjects' gender: deletes every
        unreviewed spyglass_vlm tag containing a digit (jersey numbers,
        scoreboard scores/clocks, years off a sign) or a whole-word
        gender/headcount term ("boy"/"girl"/"two boys") archive-wide,
        never touching human-added tags. Destructive (can't be undone
        short of re-running the VLM pass) -- the frontend is responsible
        for confirming with the user before calling this, same contract as
        spyglass_remove_watched_root. Doesn't catch a digit-free/gender-
        free name or text leak -- see
        spyglass_core::db::purge_onscreen_text_tags and purge_gender_tags's
        doc comments for why that class needs a human pass instead."""
        try:
            removed = spyglass_bridge.purge_onscreen_text_tags()
            return {"ok": True, "removed": removed}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_backfill_recorded_at(self, label=None):
        """One-off repair for clips registered before `recorded_at` existed:
        re-probes every clip still missing a real capture date (ffprobe
        `creation_time`, or mtime) and fills it in, so "Newest first"/
        "Oldest first" reflect actual footage dates instead of the scan-time
        `ingested_at` every bulk-imported clip otherwise shares -- see
        spyglass_bridge.backfill_recorded_at's docstring. Routed through the
        Jobs drawer (thread job) rather than a direct blocking call: an
        archive-wide pass shells out to ffprobe once per still-missing clip,
        which for a few thousand clips is genuinely slow, same UX reasoning
        as spyglass_scan_watched_root. Safe to re-run any time (e.g. after
        reconnecting a drive that was offline during an earlier pass)."""
        try:
            def run(progress_cb, cancel_event):
                progress_cb(0, "Backfilling capture dates…")
                result = spyglass_bridge.backfill_recorded_at()
                progress_cb(100, "Done")
                return result

            job_id = self.jobs.start_thread_job(
                kind="spyglass_backfill_recorded_at", label=label or "Backfill capture dates", fn=run
            )
            return {"ok": True, "job_id": job_id}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Pool tray
    # =====================================================================

    def spyglass_pool_get(self):
        try:
            return {"ok": True, "results": spyglass_bridge.get_pool()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_pool_add(self, shot_id):
        try:
            spyglass_bridge.add_shot_to_pool(shot_id)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_pool_remove(self, shot_id):
        try:
            spyglass_bridge.remove_shot_from_pool(shot_id)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_pool_reorder(self, shot_ids):
        try:
            spyglass_bridge.reorder_pool(shot_ids or [])
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_pool_clear(self):
        try:
            spyglass_bridge.clear_pool()
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_export_pool_xml(self, sequence_name):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"{(sequence_name or 'spyglass_pool').strip()}.xml",
                file_types=("Premiere XML (*.xml)", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
        output_path = _first_path(result)
        if not output_path:
            return {"ok": False, "cancelled": True}
        try:
            path = spyglass_bridge.export_pool_to_premiere_xml(output_path, sequence_name or "Spyglass Pool")
            return {"ok": True, "path": path}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Watched roots
    # =====================================================================

    def spyglass_pick_watched_root_folder(self):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the folder picker: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": path}

    def spyglass_list_watched_roots(self):
        try:
            return {"ok": True, "roots": spyglass_bridge.list_watched_roots()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_add_watched_root(self, label, path, volume_id=None, approved_by=None):
        try:
            if not path or not os.path.isdir(path):
                return {"ok": False, "error": f"Folder not found: {path}"}
            root = spyglass_bridge.add_watched_root(label or os.path.basename(path.rstrip(os.sep)) or path,
                                                      path, volume_id, approved_by)
            return {"ok": True, "root": root}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_set_watched_root_access_level(self, root_id, access_level):
        try:
            spyglass_bridge.set_watched_root_access_level(root_id, access_level)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_remove_watched_root(self, root_id):
        """Destructive (purges every clip registered under this root's
        path) — the frontend is responsible for confirming with the user
        before calling this, same contract as Spyglass's own Tauri command."""
        try:
            spyglass_bridge.remove_watched_root(root_id)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_reset_watched_root(self, root_id):
        """"Start fresh" for just this one watched root, without touching
        any other folder's index: purges every clip registered under its
        path (shots/tags/embeddings/gap-fill jobs cascade, plus each
        purged clip's cached keyframe directory on disk) and clears its
        last-scanned timestamp, but — unlike spyglass_remove_watched_root —
        leaves the root itself active rather than tombstoning it. Real
        motivating case: TAGS_PROMPT's prompt-echo bug (sidecar/
        analyze_clip.py) baked wrong tags ("mascot"/"cheering"/"classroom"/
        "outdoors") into every clip scanned before the prompt was fixed.
        Those are legitimate tag vocabulary, not something purge_
        onscreen_text_tags-style label matching can safely strip archive-
        wide, so the only correct fix is re-running the corrected pipeline
        over the affected folder. Destructive — the frontend is
        responsible for confirming with the user before calling this, and
        for triggering a fresh spyglass_scan_watched_root against the same
        root_id right after (this method only clears the index; it
        doesn't rescan)."""
        try:
            removed = spyglass_bridge.reset_watched_root(root_id)
            return {"ok": True, "removed": removed}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_requeue_short_shot_clips(self):
        """Retroactive repair for clips indexed before sidecar/
        analyze_clip.py's scene-cut detector sensitivity fix
        (ContentDetector's min_scene_len + the _merge_short_scenes
        backstop): finds every clip with at least one shot under
        MIN_SHOT_DURATION_SEC (fast pans, camera flashes, and quick
        highlight-reel cuts used to register as their own spurious
        sub-second "shots," each paying the full keyframe/CLIP-embedding/
        VLM-caption cost) and wipes + requeues just those clips for a
        fresh gap-fill pass, deleting their now-stale cached keyframe
        directories too. Clips that never had the problem are left
        completely untouched, unlike spyglass_reset_watched_root's whole-
        folder wipe. Destructive — the frontend is responsible for
        confirming with the user before calling this. Re-analysis then
        runs asynchronously through the normal gap-fill queue, same as any
        newly scanned clip -- no separate rescan trigger needed."""
        try:
            requeued = spyglass_bridge.requeue_short_shot_clips()
            return {"ok": True, "requeued": requeued}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_relink_watched_root(self, root_id, new_path):
        try:
            if not new_path or not os.path.isdir(new_path):
                return {"ok": False, "error": f"{new_path} does not exist or is not a folder"}
            root = spyglass_bridge.relink_watched_root(root_id, new_path)
            return {"ok": True, "root": root}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_scan_watched_root(self, root_id, label=None):
        """Walks the root's path and registers/relinks discovered media —
        a genuinely slow filesystem walk + checksum pass, so this runs as
        a suite-tracked thread job (Jobs drawer entry, progress affordance)
        rather than a direct blocking call, even though the PyO3 call
        itself already releases the GIL and wouldn't freeze anything else
        while it runs."""
        try:
            def run(progress_cb, cancel_event):
                progress_cb(0, "Scanning…")
                result = spyglass_bridge.scan_watched_root(root_id)
                progress_cb(100, "Done")
                return result

            job_id = self.jobs.start_thread_job(kind="spyglass_scan", label=label or f"Scan root {root_id}", fn=run)
            return {"ok": True, "job_id": job_id}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Index backup / restore
    # =====================================================================

    def spyglass_backup_index(self):
        """Snapshots the live Spyglass SQLite index to a file the user
        picks via a save dialog, using SQLite's own online backup API
        (spyglass_core::maintenance::backup_database) — safe to run while
        the background gap-fill worker is still writing to the live
        WAL-mode connection, unlike a raw file copy of the index file."""
        err = self._require_window()
        if err:
            return err
        default_name = "spyglass_index_backup_{}.sqlite".format(
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        )
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_name,
                file_types=("SQLite database (*.sqlite)", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
        dest_path = _first_path(result)
        if not dest_path:
            return {"ok": False, "cancelled": True}
        try:
            spyglass_bridge.backup_index(dest_path)
            return {"ok": True, "path": dest_path}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_restore_index(self):
        """Restores the live index from a backup file the user picks via
        an open dialog. Destructive — replaces every clip/tag/pool/
        gap-fill row currently in the index with the backup's contents.
        The Rust side (spyglass_core::maintenance::restore_database_file)
        validates the backup's integrity first and rejects a corrupt file
        without touching the live index at all; the frontend is
        responsible for confirming with the user before calling this,
        same contract as spyglass_remove_watched_root/
        spyglass_reset_watched_root."""
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("SQLite database (*.sqlite)", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file picker: {e}"}
        backup_path = _first_path(result)
        if not backup_path:
            return {"ok": False, "cancelled": True}
        try:
            spyglass_bridge.restore_index(backup_path)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Gap-fill queue
    # =====================================================================

    def spyglass_retry_failed_jobs(self, root_id=None):
        try:
            return {"ok": True, "count": spyglass_bridge.retry_failed_jobs(root_id)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_set_queue_paused(self, paused):
        try:
            spyglass_bridge.set_queue_paused(paused)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_force_gap_fill_now(self):
        """"Process now" override -- bypasses the idle-time gate (Section 7)
        that otherwise leaves recently-scanned clips with no shots yet, and
        therefore invisible in Search/Browse (both join through `shots`),
        for as long as the machine stays in active use. Auto-clears itself
        once the pending queue drains -- see spyglass_bridge.force_gap_fill_now
        and the engine's own force_active doc comment."""
        try:
            spyglass_bridge.force_gap_fill_now()
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_background_work_status(self):
        try:
            return {"ok": True, "status": spyglass_bridge.get_background_work_status()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Consolidate & Copy export
    # =====================================================================

    def spyglass_pick_consolidate_destination(self):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the folder picker: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": path}

    def spyglass_estimate_consolidate_export(self, destination_path, copy_mode):
        try:
            estimate = spyglass_bridge.estimate_consolidate_export(destination_path, copy_mode)
            return {"ok": True, "estimate": estimate}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_start_consolidate_export(self, destination_path, pool_name, copy_mode, folder_structure):
        try:
            spyglass_bridge.start_consolidate_export(destination_path, pool_name, copy_mode, folder_structure)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_consolidate_export_status(self):
        try:
            return {"ok": True, "status": spyglass_bridge.get_consolidate_export_status()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def spyglass_export_copied_files_xml(self, sequence_name):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"{(sequence_name or 'spyglass_copied').strip()}.xml",
                file_types=("Premiere XML (*.xml)", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
        output_path = _first_path(result)
        if not output_path:
            return {"ok": False, "cancelled": True}
        try:
            path = spyglass_bridge.export_copied_files_to_premiere_xml(output_path, sequence_name or "Spyglass Copied Files")
            return {"ok": True, "path": path}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Native video preview (Phase 4)
    # =====================================================================

    def _run_on_main_thread(self, fn):
        """Runs `fn()` on the main thread via AppHelper.callAfter, blocking
        THIS (worker) thread until it finishes -- so a js_api call still
        resolves synchronously with a real result/error, even though the
        actual AppKit work happens elsewhere. `fn` must return a dict
        shaped like the usual `{"ok": ...}` contract; any exception it
        raises is caught here and converted to `{"ok": False, "error":
        ...}` rather than propagating into PyObjCTools' own run-loop
        callback machinery (which would just print and swallow it)."""
        result_holder = {}
        done = threading.Event()

        def run():
            try:
                result_holder.update(fn())
            except Exception as e:
                traceback.print_exc()
                result_holder.update({"ok": False, "error": str(e)})
            finally:
                done.set()

        AppHelper.callAfter(run)
        if not done.wait(timeout=_MAIN_THREAD_TIMEOUT_SECONDS):
            return {"ok": False, "error": "Timed out waiting for the main thread to open the preview."}
        return result_holder

    def _webview_native_ptr(self):
        """The live WKWebView's raw pointer (via PyObjC's `objc.pyobjc_id`),
        for handing to the Rust side -- see spyglass_bridge.py's native-
        preview functions' module note. Raises if the window isn't ready
        yet or isn't tracked by pywebview's cocoa backend (should not
        happen once `create_window` has returned, per main.py's own
        bootstrap order)."""
        browser_view = BrowserView.instances[self.window.uid]
        return objc.pyobjc_id(browser_view.webview)

    def spyglass_open_preview(self, path, start_tc, x, y, width, height):
        """Embeds a native AVPlayerView over the placeholder the frontend
        already measured (`getBoundingClientRect`, Y-flipped against
        `window.innerHeight` — see suite.js's `openSpyglassPreview`).
        Mirrors Spyglass's own `ShotPreviewPlayer.tsx`/
        `native_video_preview.rs` almost exactly; only the coordinate
        space was re-verified from scratch for pywebview rather than
        assumed to match Tauri/wry (it does, empirically — see the Phase
        4 spike test; no Y-flip correction beyond the ordinary DOM-to-
        AppKit flip was needed)."""
        err = self._require_window()
        if err:
            return err
        try:
            ptr = self._webview_native_ptr()
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't resolve the native webview: {e}"}

        def do_open():
            # Raises on failure (caught by _run_on_main_thread); returns
            # None on success, per PyO3's `PyResult<()>` -> Python mapping.
            spyglass_bridge.open_native_video_preview(ptr, path, float(start_tc), float(x), float(y), float(width), float(height))
            return {"ok": True}

        return self._run_on_main_thread(do_open)

    def spyglass_close_preview(self):
        def do_close():
            spyglass_bridge.close_native_video_preview()
            return {"ok": True}

        return self._run_on_main_thread(do_close)
