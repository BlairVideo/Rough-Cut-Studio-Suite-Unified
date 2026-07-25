"""
api_harmonize.py — HarmonizeMixin: the Harmonize workspace (Harmonizer
integration — piano multicam sync: reference + N takes -> alignment job ->
Send to Resolve / Export FCPXML).

Split out the same way every other workspace mixin is (contract A-1).
This module defines ONE mixin class; backend/suite_api.py composes every
mixin with Rough Cut Studio's Api into the single SuiteApi js_api object.
Cross-workspace attributes (self.window, self.jobs, self._require_window)
resolve through the composed class at runtime.

Analysis (harmonize_start) runs as a background subprocess job in
Harmonizer's own venv (numpy/scipy/librosa live there) — mirrors
api_sync.py's sync_start exactly. Export (harmonize_send_to_resolve /
harmonize_export_xml) runs in-process via harmonizer_bridge.py, since
make_fcpxml.py/import_to_resolve.py are pure stdlib — mirrors
sync_export_xml's in-process pattern.
"""

import os
import json
import time
import traceback

try:
    from . import paths, harmonizer_bridge
    from .api_shared import *  # noqa: F401,F403 — shared constants + helpers
except ImportError:  # pragma: no cover — direct script import in tests
    import paths
    import harmonizer_bridge
    from api_shared import *  # noqa: F401,F403

import webview  # noqa: E402


class HarmonizeMixin:
    # =====================================================================
    # Harmonize (Harmonizer integration)
    # =====================================================================

    def harmonize_pick_reference(self):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=HARMONIZE_REF_DIALOG_TYPES,
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file picker: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": path}

    def harmonize_pick_takes(self):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=HARMONIZE_TAKE_DIALOG_TYPES,
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file picker: {e}"}
        selected = _all_paths(result)
        if not selected:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "paths": selected}

    @staticmethod
    def _harmonizer_venv_error():
        if not os.path.isfile(paths.HARMONIZER_PYTHON):
            return {"ok": False, "error":
                     "Harmonizer's virtual environment was not found at "
                     f"{paths.HARMONIZER_PYTHON} — set that app up first."}
        return None

    def harmonize_start(self, ref_path, take_paths, no_retime=None):
        try:
            take_paths = [p for p in (take_paths or []) if isinstance(p, str) and p]
            if not ref_path or not isinstance(ref_path, str):
                return {"ok": False, "error": "No reference file was given."}
            if not os.path.isfile(ref_path):
                return {"ok": False, "error": f"Reference file not found: {ref_path}"}
            if not take_paths:
                return {"ok": False, "error": "No takes were given."}
            missing = [p for p in take_paths if not os.path.isfile(p)]
            if missing:
                return {"ok": False, "error": f"Take file(s) not found: {', '.join(missing)}"}

            # Basenames of takes sharing the reference's own audio source
            # (e.g. fed from the same recorder) -- align.py's own
            # --no-retime concept (build_segments' no_retime_takes param).
            # The worker re-validates these against the actual take
            # basenames; no need to duplicate that check here.
            no_retime = [n for n in (no_retime or []) if isinstance(n, str) and n]

            err = self._harmonizer_venv_error()
            if err:
                return err

            job_id = self.jobs.start_subprocess_job(
                kind="harmonize",
                label=os.path.basename(ref_path),
                interpreter=paths.HARMONIZER_PYTHON,
                script=paths.HARMONIZE_WORKER,
                params={"mode": "align", "ref": ref_path, "takes": take_paths, "no_retime": no_retime},
                cwd=paths.HARMONIZER_BACKEND_DIR,
            )
            return {"ok": True, "job_id": job_id}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _harmonize_report_path(ref_path):
        ref_dir = os.path.dirname(ref_path)
        stem = os.path.splitext(os.path.basename(ref_path))[0]
        return os.path.join(ref_dir, f"{stem}{HARMONIZE_REPORT_SUFFIX}")

    def harmonize_save_report(self, ref_path, take_paths, report, no_retime=None):
        try:
            if not ref_path or not isinstance(report, dict):
                return {"ok": False, "error": "Nothing to save."}
            sidecar_path = self._harmonize_report_path(ref_path)
            payload = {
                "ref_path": ref_path,
                "take_paths": take_paths or [],
                "no_retime": [n for n in (no_retime or []) if isinstance(n, str) and n],
                "report": report,
                "saved_at": time.time(),
            }
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            return {"ok": True, "path": sidecar_path}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def harmonize_load_report(self, ref_path):
        try:
            if not ref_path:
                return {"ok": True, "found": False}
            sidecar_path = self._harmonize_report_path(ref_path)
            if not os.path.isfile(sidecar_path):
                return {"ok": True, "found": False}
            with open(sidecar_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return {
                "ok": True, "found": True,
                "take_paths": payload.get("take_paths") or [],
                "no_retime": payload.get("no_retime") or [],
                "report": payload.get("report"),
                "saved_at": payload.get("saved_at"),
            }
        except Exception as e:
            traceback.print_exc()
            return {"ok": True, "found": False, "error": str(e)}

    def harmonize_send_to_resolve(self, ref_path, take_paths, report, project_name=None, timeline_name=None):
        try:
            if not isinstance(report, dict):
                return {"ok": False, "error": "No sync report to export — run Analyze first."}
            take_paths = [p for p in (take_paths or []) if isinstance(p, str) and p]
            braw = harmonizer_bridge.braw_takes(take_paths)
            if braw:
                return {"ok": False, "error":
                         "BRAW export isn't supported yet — Resolve has no tested path for a "
                         "retimed BRAW clip (see Harmonizer_App_Plan.md). Affected take(s): "
                         + ", ".join(os.path.basename(p) for p in braw)}
            return harmonizer_bridge.send_to_resolve(
                ref_path, take_paths, report, project_name=project_name, timeline_name=timeline_name)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def harmonize_export_xml(self, ref_path, take_paths, report, timeline_name=None):
        try:
            if not isinstance(report, dict):
                return {"ok": False, "error": "No sync report to export — run Analyze first."}
            take_paths = [p for p in (take_paths or []) if isinstance(p, str) and p]
            braw = harmonizer_bridge.braw_takes(take_paths)
            if braw:
                return {"ok": False, "error":
                         "BRAW export isn't supported yet — Resolve has no tested path for a "
                         "retimed BRAW clip (see Harmonizer_App_Plan.md). Affected take(s): "
                         + ", ".join(os.path.basename(p) for p in braw)}
            timeline_name = (timeline_name or "").strip() or None

            err = self._require_window()
            if err:
                return err
            stem = timeline_name or os.path.splitext(os.path.basename(ref_path or "harmonized"))[0] + " harmonized"
            try:
                result = self.window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename=f"{stem}.fcpxml",
                    file_types=("FCPXML (*.fcpxml)", "All files (*.*)"),
                )
            except Exception as e:
                traceback.print_exc()
                return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
            output_path = _first_path(result)
            if not output_path:
                return {"ok": False, "cancelled": True}

            harmonizer_bridge.export_fcpxml(report, take_paths, output_path, sequence_name=timeline_name)
            return {
                "ok": True, "path": output_path,
                "reference_note": (
                    "Reference audio isn't included — Resolve can't link an audio-only "
                    "FCPXML asset. Add it manually after import, or run "
                    "Harmonizer/prototype/add_reference_audio.py."
                ),
            }
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}
