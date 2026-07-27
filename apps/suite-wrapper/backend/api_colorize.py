"""
api_colorize.py — ColorizeMixin: the Grade workspace (color correction
and grading, LUT import/apply, in/out trimming, single + batch export).

Split out of suite_api.py (contract A-1), same shape as every other
workspace mixin: this module defines ONE mixin class; backend/suite_api.py
composes every mixin with Rough Cut Studio's Api into the single SuiteApi
js_api object — mixins are never instantiated on their own.
Cross-workspace attributes (self.window, self.jobs, self._require_window)
resolve through the composed class at runtime.

Every public method returns a dict with at least {"ok": bool} — nothing
raises across the JS bridge, matching every other mixin's error contract.
"""

import os
import time
import traceback

try:
    from . import paths, colorize_bridge
    from .api_shared import *  # noqa: F401,F403 — _first_path/_all_paths
except ImportError:  # pragma: no cover — direct script import in tests
    import paths
    import colorize_bridge
    from api_shared import *  # noqa: F401,F403

import webview  # noqa: E402

# Referenced via the module object (never a bare `from colorize_bridge
# import ...`) so this works whether the try/except above resolved
# colorize_bridge as a package-relative submodule or a top-level one.
GradeState = colorize_bridge.GradeState
LutParseError = colorize_bridge.LutParseError
ColorizeClip = colorize_bridge.ColorizeClip
ColorizeProject = colorize_bridge.ColorizeProject
GradePreset = colorize_bridge.GradePreset
OUTPUT_PRESETS = colorize_bridge.OUTPUT_PRESETS

COLORIZE_VIDEO_DIALOG_TYPES = (
    "Video files (*.mp4;*.mov;*.mxf;*.mkv;*.avi;*.braw)",
    "All files (*.*)",
)
COLORIZE_LUT_DIALOG_TYPES = (
    "LUT files (*.cube;*.3dl)",
    "All files (*.*)",
)

COLORIZE_EXPORT_JOB_KIND = "colorize_export"
COLORIZE_EXPORT_CONCURRENCY = 2  # ffmpeg re-encodes are CPU-heavy; cap batch export concurrency


class ColorizeMixin:
    # =====================================================================
    # Media bin
    # =====================================================================

    def colorize_pick_clips(self):
        """Opens a multi-select file dialog, probes every chosen clip via
        the shared ffprobe utility, and returns the probed list — the
        frontend decides whether/where to add each into the active
        project's clip list."""
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=COLORIZE_VIDEO_DIALOG_TYPES)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file dialog: {e}"}
        chosen = _all_paths(result)
        if not chosen:
            return {"ok": True, "clips": []}
        clips = []
        for path in chosen:
            try:
                clips.append(colorize_bridge.probe_clip(path))
            except Exception as e:
                traceback.print_exc()
                clips.append({"path": path, "error": str(e)})
        return {"ok": True, "clips": clips}

    def colorize_probe_clip(self, path):
        try:
            return {"ok": True, "clip": colorize_bridge.probe_clip(path)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def colorize_get_preview_url(self, path):
        """A loopback URL colorize.js's <video> element can load for a
        clip's ORIGINAL source file -- the preview always decodes the
        real source (Colorize has no proxy-generation step of its own),
        matching every other workspace's live-playback path."""
        try:
            url = colorize_bridge.get_preview_url(path)
            if url is None:
                return {"ok": False, "error": "Media file not found on disk."}
            return {"ok": True, "url": url}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # LUT library
    # =====================================================================

    def colorize_pick_and_import_lut(self):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=COLORIZE_LUT_DIALOG_TYPES)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file dialog: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            meta = colorize_bridge.import_lut(path)
            return {"ok": True, "lut": meta}
        except LutParseError as e:
            return {"ok": False, "error": f"Couldn't read that LUT: {e}"}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def colorize_list_luts(self):
        try:
            return {"ok": True, "luts": colorize_bridge.list_luts()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def colorize_get_lut_preview(self, lut_id):
        try:
            payload = colorize_bridge.get_lut_preview_json(lut_id)
            if payload is None:
                return {"ok": False, "error": f"Unknown LUT id: {lut_id}"}
            return {"ok": True, "lut": payload}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def colorize_delete_lut(self, lut_id):
        try:
            for suffix in (".json", ".preview.json"):
                p = os.path.join(paths.COLORIZE_LUTS_DIR, f"{lut_id}{suffix}")
                if os.path.isfile(p):
                    os.remove(p)
            stored = colorize_bridge.resolve_lut_original_path(lut_id)
            if stored and os.path.isfile(stored):
                os.remove(stored)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Projects
    # =====================================================================

    def colorize_new_project(self, name):
        try:
            project = ColorizeProject.new(name or "Untitled Project")
            colorize_bridge.save_project(project)
            return {"ok": True, "project": project.to_dict()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def colorize_save_project(self, project_dict):
        try:
            project = ColorizeProject.from_dict(project_dict)
            colorize_bridge.save_project(project)
            return {"ok": True, "project_id": project.id}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def colorize_load_project(self, project_id):
        try:
            project = colorize_bridge.load_project(project_id)
            if project is None:
                return {"ok": False, "error": f"Unknown project id: {project_id}"}
            return {"ok": True, "project": project.to_dict()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def colorize_list_projects(self):
        try:
            return {"ok": True, "projects": colorize_bridge.list_projects()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def colorize_delete_project(self, project_id):
        try:
            removed = colorize_bridge.delete_project(project_id)
            return {"ok": True, "removed": removed}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Grade presets
    # =====================================================================

    def colorize_save_preset(self, name, grade_dict):
        try:
            preset = GradePreset.new(name or "Untitled Preset", GradeState.from_dict(grade_dict))
            colorize_bridge.save_preset(preset)
            return {"ok": True, "preset": preset.to_dict()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def colorize_list_presets(self):
        try:
            return {"ok": True, "presets": colorize_bridge.list_presets()}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def colorize_delete_preset(self, preset_id):
        try:
            removed = colorize_bridge.delete_preset(preset_id)
            return {"ok": True, "removed": removed}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # =====================================================================
    # Export (single + batch) — both paths funnel through the same
    # per-clip job so both show up in the shared Jobs drawer identically.
    # =====================================================================

    def colorize_pick_export_folder(self):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the folder dialog: {e}"}
        folder = _first_path(result)
        if not folder:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "folder": folder}

    def _colorize_start_export_job(self, clip_dict, output_path, output_preset):
        clip = ColorizeClip.from_dict(clip_dict)

        def do_export(progress_cb, cancel_event):
            return colorize_bridge.export_clip(clip, output_path, output_preset, progress_cb, cancel_event)

        self.jobs.set_kind_limit(COLORIZE_EXPORT_JOB_KIND, COLORIZE_EXPORT_CONCURRENCY)
        return self.jobs.start_thread_job(
            kind=COLORIZE_EXPORT_JOB_KIND,
            label=os.path.basename(output_path),
            fn=do_export,
        )

    def colorize_export_clip(self, clip_dict, output_path, output_preset="share_h264"):
        if output_preset not in OUTPUT_PRESETS:
            return {"ok": False, "error": f"Unknown output preset: {output_preset}"}
        try:
            job_id = self._colorize_start_export_job(clip_dict, output_path, output_preset)
            return {"ok": True, "job_id": job_id}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def colorize_export_batch(self, clips, output_folder, output_preset="share_h264"):
        """`clips` is a list of clip dicts (project.ColorizeClip shape).
        Each becomes its own thread job under the shared
        "colorize_export" kind/concurrency limit, so the Jobs drawer
        shows per-clip AND overall batch progress with no extra UI —
        matches how B-Roll/BRAW proxy batches already queue."""
        if output_preset not in OUTPUT_PRESETS:
            return {"ok": False, "error": f"Unknown output preset: {output_preset}"}
        if not clips:
            return {"ok": False, "error": "No clips to export"}
        ext = ".mov" if output_preset == "archive_prores422" else ".mp4"
        queued = []
        for clip_dict in clips:
            try:
                base = os.path.splitext(os.path.basename(clip_dict["source_path"]))[0]
                output_path = os.path.join(output_folder, f"{base}_graded{ext}")
                job_id = self._colorize_start_export_job(clip_dict, output_path, output_preset)
                queued.append({"clip_id": clip_dict.get("id"), "output_path": output_path, "job_id": job_id})
            except Exception as e:
                traceback.print_exc()
                queued.append({"clip_id": clip_dict.get("id"), "error": str(e)})
        return {"ok": True, "queued": queued}
