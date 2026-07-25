"""
api_cardeater.py — CardEaterMixin: the Card Eater workspace (card ingest —
copy camera-card footage to one or more destinations, renamed per a naming
convention, verified byte-for-byte with BLAKE3).

Split out of suite_api.py (contract A-1), same convention as every other
workspace mixin. This is a full Python port of the standalone "Card Eater"
Tauri/Rust+React app (see CardEater/ at the repo root, and its own
CLAUDE.md/card-ingest-app-plan.md) rather than a subprocess/in-process
bridge to it — Card Eater has no Python component at all to shell out to,
unlike the other four sibling apps. The behavioral contract (naming engine,
copy/verify engine, DB schema) is ported line-for-line from its Rust
source — see cardeater_naming.py / cardeater_copy.py / cardeater_verify.py /
cardeater_db.py's own module docstrings for exactly what each mirrors.

Error contract matches every other workspace: every public method returns
a dict with at least {"ok": bool} and {"error": str} when not ok.
"""

import traceback

try:
    from . import paths
    from . import cardeater_db as db
    from . import cardeater_naming as naming
    from . import cardeater_copy as copy_engine
    from . import cardeater_card as card_detect
    from . import cardeater_metadata as metadata
    from . import cardeater_volume_watcher as volume_watcher
except ImportError:  # pragma: no cover — direct script import in tests
    import paths
    import cardeater_db as db
    import cardeater_naming as naming
    import cardeater_copy as copy_engine
    import cardeater_card as card_detect
    import cardeater_metadata as metadata
    import cardeater_volume_watcher as volume_watcher

import os
import subprocess
import sys
import threading

import webview  # noqa: E402

# Mirrors the original Card Eater React app's FilePreviewModal.tsx — only
# formats the (embedded) webview can plausibly decode natively; everything
# else (RAW formats, sidecar files) has no in-app preview path and shows a
# "preview unavailable" placeholder client-side instead.
_PREVIEWABLE_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif",
    "mp4", "mov", "m4v", "webm",
    "wav", "mp3", "m4a", "aac", "aiff",
}


class CardEaterState:
    """Everything CardEaterMixin needs beyond the composed Api's own
    self.window: one SQLite connection, the active-card registry the
    background volume watcher updates, and per-destination job control /
    live-progress state for in-flight copy jobs. One instance per SuiteApi
    (see suite_api.py's __init__)."""

    def __init__(self, db_path):
        self.db = db.Db(db_path)
        self.lock = threading.RLock()
        self.job_controls = {}  # dest_id -> cardeater_copy.JobControl
        self.live = {}          # dest_id -> {mb_per_sec, eta_secs, current_file_name, error_message}
        self.card = volume_watcher.CardRegistry()

    def start_watcher(self):
        volume_watcher.start(self.card)


class CardEaterMixin:
    # =====================================================================
    # Card detection / scanning
    # =====================================================================

    def suite_cardeater_get_active_card(self):
        try:
            with self._cardeater.card.lock:
                return {"ok": True, "card": self._cardeater.card.active}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_open_folder_as_card(self):
        """Dev/manual fallback for environments with no physical card
        reader: opens a native folder picker and treats the chosen folder
        as a "card" — mirrors the original's DevCardFallback / real-card
        watcher path exactly (both funnel into the same active-card
        registry)."""
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the folder picker: {e}"}
        path = (result[0] if result else None) if isinstance(result, (list, tuple)) else result
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            info = card_detect.build_card_info(path, is_dev_fallback=True)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}
        with self._cardeater.card.lock:
            self._cardeater.card.active = info
        return {"ok": True, "card": info}

    def suite_cardeater_open_folder(self, path):
        """Opens an arbitrary folder in the Finder -- backs both the "Open
        Source" button next to the active card's label (opens the card's
        own root) and the Jobs drawer's per-destination "Open Folder"
        button on a finished/in-progress cardeater_copy job (opens its
        resolved destination folder). macOS-only (`open`), same convention
        as BrollMixin.suite_reveal_broll_media's `open -R` — reimplemented
        here rather than shared since sibling-app files are never imported
        as a runtime dependency. `-R` isn't used here since the target is
        the folder itself, not a file to reveal/select inside a parent."""
        try:
            if not path or not isinstance(path, str) or not os.path.isdir(path):
                return {"ok": False, "error": f"Folder not found on disk: {path}"}
            if sys.platform != "darwin":
                return {"ok": False, "error": "Opening folders in a file manager is only supported on macOS."}
            subprocess.run(["open", path], check=False)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_pick_destination(self):
        """Opens a native folder picker for adding one or more copy
        destinations — mirrors SyncMixin's sync_pick_video/sync_pick_audio
        dialog pattern (a plain OS folder picker, unrelated to card
        detection)."""
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=True)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the folder picker: {e}"}
        if not result:
            return {"ok": False, "cancelled": True}
        paths = list(result) if isinstance(result, (list, tuple)) else [result]
        return {"ok": True, "paths": paths}

    def suite_cardeater_scan_card_files(self, card_path):
        try:
            files = card_detect.scan_card_files(card_path)
            return {"ok": True, "files": files}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_preview_url(self, path):
        """Local-HTTP preview URL for the file-preview modal (image/video/
        audio only — RAW/proprietary formats show a placeholder client
        side, same scope carve-out as the original app's own
        FilePreviewModal). Reuses RCS's own PreviewServer (self.preview_server,
        inherited via Api.__init__), the same byte-range-capable local
        server the Sync/B-Roll workspaces already use — not a new server."""
        try:
            ext = os.path.splitext(path)[1].lstrip(".").lower()
            if ext not in _PREVIEWABLE_EXTENSIONS:
                return {"ok": False, "error": "unsupported"}
            if not os.path.isfile(path):
                return {"ok": False, "error": "File not found."}
            return {"ok": True, "url": self.preview_server.url_for(path)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_file_metadata(self, path):
        """On-demand technical metadata (dimensions/duration/frame rate/
        camera make+model) for the viewer panel — fetched per-file when a
        file is focused there, not batched at scan time like the file
        list's created_at/created_at_source (see cardeater_metadata.py's
        module docstring for why)."""
        try:
            if not os.path.isfile(path):
                return {"ok": False, "error": "File not found."}
            return {"ok": True, "metadata": metadata.resolve_extended_metadata(path)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Favorites (destination bookmarks)
    # =====================================================================

    def suite_cardeater_list_favorites(self):
        try:
            with self._cardeater.db.lock:
                return {"ok": True, "favorites": db.list_favorites(self._cardeater.db.conn)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_add_favorite(self, label, path):
        try:
            with self._cardeater.db.lock:
                fav = db.add_favorite(self._cardeater.db.conn, label, path)
            return {"ok": True, "favorite": fav}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_remove_favorite(self, fav_id):
        try:
            with self._cardeater.db.lock:
                db.remove_favorite(self._cardeater.db.conn, fav_id)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Naming templates + engine
    # =====================================================================

    def suite_cardeater_list_naming_templates(self):
        try:
            with self._cardeater.db.lock:
                return {"ok": True, "templates": db.list_naming_templates(self._cardeater.db.conn)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_save_naming_template(self, tpl):
        try:
            naming.validate_template(tpl)
            with self._cardeater.db.lock:
                saved = db.save_naming_template(self._cardeater.db.conn, tpl)
            return {"ok": True, "template": saved}
        except naming.NamingError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_delete_naming_template(self, tpl_id):
        try:
            with self._cardeater.db.lock:
                db.delete_naming_template(self._cardeater.db.conn, tpl_id)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_preview_names(self, req):
        try:
            return {"ok": True, **naming.preview_names(req)}
        except naming.NamingError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_check_folder_collision(self, dest_path, folder_name):
        try:
            return {"ok": True, **naming.check_folder_collision(dest_path, folder_name)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_list_destination_files(self, dest_path, folder_name=None):
        """"Preview" button on a chosen destination: lists what's already
        sitting in it (or its resolved per-job subfolder, if the naming
        template uses one) so the user can see what's there before
        copying -- same resolved-path rule _run_destination uses for
        `target_dir` (cardeater_copy.py), just read-only and non-recursive."""
        try:
            target_dir = os.path.join(dest_path, folder_name) if folder_name else dest_path
            if not os.path.isdir(target_dir):
                return {"ok": True, "resolved_path": target_dir, "exists": False, "entries": []}
            entries = []
            with os.scandir(target_dir) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        is_dir = entry.is_dir()
                        size_bytes = None if is_dir else entry.stat().st_size
                    except OSError:
                        is_dir, size_bytes = False, None
                    entries.append({"name": entry.name, "is_dir": is_dir, "size_bytes": size_bytes})
            entries.sort(key=lambda e: e["name"].lower())
            return {"ok": True, "resolved_path": target_dir, "exists": True, "entries": entries}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Copy queue / verification
    # =====================================================================

    def suite_cardeater_check_disk_space(self, dest_paths, bytes_needed):
        try:
            return {"ok": True, "checks": copy_engine.check_disk_space(dest_paths, bytes_needed)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_start_job(self, req):
        try:
            handle = copy_engine.start_job(self._cardeater, req)
            return {"ok": True, **handle}
        except (ValueError, naming.NamingError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_pause_job(self, job_id):
        return self._cardeater_set_job_control(job_id, copy_engine.JobControl.PAUSED)

    def suite_cardeater_resume_job(self, job_id):
        return self._cardeater_set_job_control(job_id, copy_engine.JobControl.RUNNING)

    def suite_cardeater_cancel_job(self, job_id):
        return self._cardeater_set_job_control(job_id, copy_engine.JobControl.CANCELLED)

    def _cardeater_set_job_control(self, job_id, value):
        try:
            copy_engine.set_job_control(self._cardeater, job_id, value)
            return {"ok": True}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_get_job_status(self, job_id):
        try:
            return {"ok": True, **copy_engine.get_job_status(self._cardeater, job_id)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_list_jobs(self, card_id=None):
        try:
            with self._cardeater.db.lock:
                rows = db.list_job_summaries(self._cardeater.db.conn, card_id)
            return {"ok": True, "jobs": rows}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_export_job_history_csv(self, card_id=None):
        err = self._require_window()
        if err:
            return err
        try:
            with self._cardeater.db.lock:
                rows = db.list_job_summaries(self._cardeater.db.conn, card_id)
            csv_text = db.job_summaries_to_csv(rows)
            return self._save_text(
                csv_text,
                default_name="card-eater-job-history.csv",
                file_types=("CSV (*.csv)", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_cardeater_mark_card_safe_check(self, card_path):
        try:
            return {"ok": True, "safe": copy_engine.mark_card_safe_check(self._cardeater, card_path)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}
