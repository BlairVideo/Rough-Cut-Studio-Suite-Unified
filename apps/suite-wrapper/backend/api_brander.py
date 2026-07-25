"""
api_brander.py — BranderMixin: the Graphics workspace (Blair Brander bridge: previews, AI titles incl. the dedicated Gemini key, logo import, exports).

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
import uuid
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


class BranderMixin:
    # =====================================================================
    # Brander (in-process — pure Pillow)
    # =====================================================================

    def brander_defaults(self):
        try:
            return {
                "ok": True,
                "scene": brander_bridge.scene_to_json(brander_bridge.default_scene()),
                "options": brander_bridge.options_dict(),
            }
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def brander_preview(self, scene, t, max_width=960, elapsed_seconds=None):
        try:
            data_uri, w, h = brander_bridge.render_preview(
                scene, t=t, max_width=max_width, elapsed_seconds=elapsed_seconds)
            return {"ok": True, "data_uri": data_uri, "width": w, "height": h}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Preview render failed: {e}"}

    def brander_still_preview(self, scene, max_width=960):
        try:
            data_uri, w, h = brander_bridge.render_still_preview(scene, max_width=max_width)
            return {"ok": True, "data_uri": data_uri, "width": w, "height": h}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Preview render failed: {e}"}

    def brander_interpret(self, prompt_text, scene):
        """Local, keyword-based prompt interpreter (prompt_ai.py) — no
        network involved despite the name."""
        try:
            base = brander_bridge.normalize_scene(scene)
            new_scene, notes = brander_bridge.prompt_ai.interpret(prompt_text or "", base)
            return {"ok": True,
                    "scene": brander_bridge.scene_to_json(new_scene),
                    "notes": list(notes or [])}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def brander_import_logo(self):
        """Import a custom logo: copy the picked PNG/JPEG into
        assets/logos/, register it in brand.LOGO_SOURCES under a unique
        'Custom: <stem>' name, and persist the registry so it survives
        restarts. The standard white-background keying (assets.
        load_transparent) applies to imports too — logos supplied on a
        white background are auto-keyed to transparency."""
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=LOGO_DIALOG_TYPES,
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file picker: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        # The "All files" filter lets anything through the dialog, and only
        # PNG/JPEG survive the white-key pipeline — enforce here.
        if os.path.splitext(path)[1].lower() not in LOGO_IMPORT_EXTENSIONS:
            return {"ok": False, "error": "Logos must be PNG or JPEG images."}
        try:
            name = brander_bridge.register_custom_logo(path)
            return {"ok": True,
                    "logos": brander_bridge.options_dict()["logos"],
                    "selected": name}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't import the logo: {e}"}

    def brander_remove_custom_logo(self, name):
        """Delete a previously-imported custom logo (file + registry
        entry). Refuses to remove a built-in logo — see
        brander_bridge.remove_custom_logo."""
        try:
            ok, error = brander_bridge.remove_custom_logo(name)
            if not ok:
                return {"ok": False, "error": error}
            return {"ok": True, "logos": brander_bridge.options_dict()["logos"]}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't remove that logo: {e}"}

    def brander_gemini_key_status(self):
        """Status for Blair Brander's OWN Gemini key, stored in the system
        keychain under BRANDER_KEYRING_SERVICE/BRANDER_KEYRING_GEMINI_KEY —
        entirely separate from Rough Cut Studio's shared .env-based key."""
        try:
            import keyring
            key = keyring.get_password(BRANDER_KEYRING_SERVICE, BRANDER_KEYRING_GEMINI_KEY)
            return {"ok": True, "present": bool(key)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't read the keychain: {e}"}

    def brander_save_gemini_key(self, key):
        """Store (or, for an empty string, delete) Blair Brander's own
        Gemini API key in the system keychain. Never written to any file,
        and never shared with Rough Cut Studio's Edit-workspace key."""
        try:
            import keyring
            key = (key or "").strip()
            if key:
                keyring.set_password(BRANDER_KEYRING_SERVICE, BRANDER_KEYRING_GEMINI_KEY, key)
            else:
                try:
                    keyring.delete_password(BRANDER_KEYRING_SERVICE, BRANDER_KEYRING_GEMINI_KEY)
                except keyring.errors.PasswordDeleteError:
                    pass  # nothing stored — deleting nothing is fine
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't update the keychain: {e}"}

    def brander_ai_generate(self, prompt_text, scene):
        """Gemini-backed AI mode for the Graphics form. Uses Blair
        Brander's OWN dedicated Gemini key from the system keychain
        (BRANDER_KEYRING_SERVICE/BRANDER_KEYRING_GEMINI_KEY) — never RCS's
        shared .env-based key. No key -> error "no_api_key" so the UI can
        prompt instead of showing a failure toast. The model's answer is
        whitelist-validated by brander_gemini.validate_update before ANY
        field touches the scene; this endpoint call is the suite's only
        new network traffic."""
        try:
            try:
                import keyring
                key = (keyring.get_password(BRANDER_KEYRING_SERVICE, BRANDER_KEYRING_GEMINI_KEY) or "").strip()
            except Exception:
                key = ""
            if not key:
                return {"ok": False, "error": "no_api_key",
                        "message": "No Gemini API key is set for Blair Brander — "
                                   "add one to use Gemini mode."}

            options = brander_bridge.options_dict()
            base = brander_bridge.normalize_scene(scene)
            scene_json = brander_bridge.scene_to_json(base)

            try:
                update = brander_gemini.generate_scene_update(
                    key, prompt_text or "", scene_json, options)
            except brander_gemini.BranderGeminiError as e:
                return {"ok": False, "error": str(e)}

            clean, notes = brander_gemini.validate_update(update, scene_json, options)

            new_scene = dict(base)
            new_scene.update(clean)
            # canvas_size always follows canvas_preset_name (the scene's
            # invariant everywhere else in the app).
            preset_name = clean.get("canvas_preset_name")
            if preset_name:
                size = options["canvas_presets"].get(preset_name)
                if size:
                    new_scene["canvas_size"] = (int(size[0]), int(size[1]))
            if not clean and not notes:
                notes = ["The AI suggested no changes."]

            return {"ok": True,
                    "scene": brander_bridge.scene_to_json(new_scene),
                    "notes": notes,
                    "provider": "gemini"}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def brander_ai_generate_graphic(self, prompt_text, scene):
        """Gemini image generation for a fully custom background graphic
        (as opposed to brander_ai_generate's text-only scene tweaks).
        Same dedicated Blair Brander keychain key as brander_ai_generate —
        no separate key needed. The generated PNG is saved under
        assets/graphics/ and set as scene["ai_background_path"], which
        renderer.render_background() (Blair Brander's own file — the ONE
        place its render pipeline had to change for this feature) treats
        as an image to cover-fit behind the usual title/logo layers,
        taking priority over background_style whenever a valid file is
        set. Brand-guideline compliance is enforced at the prompt level
        (see brander_gemini.IMAGE_SYSTEM_INSTRUCTION) — there is no
        pixel-level verification that the model actually stayed in
        palette, so a human glance at the preview this returns is still
        worthwhile before exporting."""
        try:
            try:
                import keyring
                key = (keyring.get_password(BRANDER_KEYRING_SERVICE, BRANDER_KEYRING_GEMINI_KEY) or "").strip()
            except Exception:
                key = ""
            if not key:
                return {"ok": False, "error": "no_api_key",
                        "message": "No Gemini API key is set for Blair Brander — "
                                   "add one to use Gemini mode."}

            options = brander_bridge.options_dict()
            base = brander_bridge.normalize_scene(scene)

            try:
                image_bytes, mime_type = brander_gemini.generate_graphic_image(
                    key, prompt_text or "", options)
            except brander_gemini.BranderGeminiError as e:
                return {"ok": False, "error": str(e)}

            ext = ".png" if "png" in (mime_type or "") else ".jpg"
            paths.ensure_suite_dirs()
            filename = f"ai-bg-{uuid.uuid4().hex[:12]}{ext}"
            out_path = os.path.join(paths.GRAPHICS_DIR, filename)
            with open(out_path, "wb") as f:
                f.write(image_bytes)

            new_scene = dict(base)
            new_scene["ai_background_path"] = out_path
            new_scene["transparent_bg"] = False  # a background image implies opaque output

            scene_json = brander_bridge.scene_to_json(new_scene)
            data_uri, w, h = brander_bridge.render_still_preview(scene_json, max_width=960)

            return {"ok": True,
                    "scene": scene_json,
                    "data_uri": data_uri,
                    "width": w,
                    "height": h,
                    "provider": "gemini"}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def brander_clear_ai_background(self, scene):
        """Drop scene["ai_background_path"], reverting to the normal
        Solid/Gradient background_style. Does NOT delete the generated
        file on disk (it's cheap, and a project file or undo step may
        still reference it by path)."""
        try:
            new_scene = dict(brander_bridge.normalize_scene(scene))
            new_scene.pop("ai_background_path", None)
            return {"ok": True, "scene": brander_bridge.scene_to_json(new_scene)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def _brander_slug(self, scene):
        title = str((scene or {}).get("title") or "graphic")
        return self._safe_name(title)[:60] or "graphic"

    def brander_export_png(self, scene):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=self._brander_slug(scene) + ".png",
                file_types=("PNG image (*.png)", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            normalized = brander_bridge.normalize_scene(scene)
            brander_bridge.export.export_png(normalized, path)
            return {"ok": True, "path": path}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"PNG export failed: {e}"}

    def brander_export_video(self, scene, codec="mov"):
        """Save dialog now, render in a background thread job. Note that
        export.export_video streams frames into ffmpeg via its own
        multiprocessing pool — once running it can't be interrupted
        mid-encode; cancelling only marks the job (see jobs.py docstring)."""
        if codec not in ("mov", "webm"):
            return {"ok": False, "error": f"Unknown codec: {codec}"}
        err = self._require_window()
        if err:
            return err
        ext = ".mov" if codec == "mov" else ".webm"
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=self._brander_slug(scene) + ext,
                file_types=(
                    ("QuickTime Animation with alpha (*.mov)", "All files (*.*)")
                    if codec == "mov" else
                    ("VP9 WebM with alpha (*.webm)", "All files (*.*)")
                ),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}

        try:
            normalized = brander_bridge.normalize_scene(scene)

            def do_export(progress_cb, cancel_event):
                progress_cb(5, f"Rendering {os.path.basename(path)}…")
                # ffmpeg + the render pool give no incremental callback;
                # the jump from 5 to done is honest rather than invented.
                brander_bridge.export.export_video(
                    normalized, path, codec=codec,
                    extra_logo_sources=brander_bridge.custom_logo_sources())
                progress_cb(100, "Export finished")
                return {"path": path}

            job_id = self.jobs.start_thread_job(
                kind="brander_video",
                label=os.path.basename(path),
                fn=do_export,
            )
            return {"ok": True, "job_id": job_id}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def brander_send_to_edit(self, scene):
        """Export a qtrle-alpha .mov into the suite's graphics folder, then
        register it as a b-roll source exactly like broll_send_to_edit —
        all inside one background job so the UI never blocks on ffmpeg."""
        try:
            normalized = brander_bridge.normalize_scene(scene)
            paths.ensure_suite_dirs()
            slug = self._brander_slug(normalized)
            out_path = os.path.join(paths.GRAPHICS_DIR, f"{slug}-{int(time.time())}.mov")
            duration = brander_bridge.scene_duration_seconds(normalized)
            title = str(normalized.get("title") or "Graphic")

            def do_send(progress_cb, cancel_event):
                progress_cb(5, "Rendering graphic with alpha…")
                brander_bridge.export.export_video(
                    normalized, out_path, codec="mov",
                    extra_logo_sources=brander_bridge.custom_logo_sources())
                if cancel_event.is_set():
                    raise RuntimeError("Cancelled")
                progress_cb(85, "Adding to Edit sources…")
                info = handoff.ensure_broll_source(
                    self, out_path, duration, cue_text=f"Graphic: {title}")
                cut = handoff.build_cut_spec(
                    self, info["source_id"], 0.0, duration, track="broll")
                progress_cb(100, "Ready in Edit")
                return {"media_path": out_path,
                        "source_id": info["source_id"],
                        "cut": cut}

            job_id = self.jobs.start_thread_job(
                kind="brander_send",
                label=f"Send to Edit: {title}",
                fn=do_send,
            )
            return {"ok": True, "job_id": job_id}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def brander_save_project(self, scene):
        err = self._require_window()
        if err:
            return err
        ext = brander_bridge.project_io.PROJECT_EXTENSION
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=self._brander_slug(scene) + ext,
                file_types=(f"Blair Brander project (*{ext})", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            brander_bridge.project_io.save_project(
                brander_bridge.normalize_scene(scene), path)
            return {"ok": True, "path": path}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't save the project: {e}"}

    def brander_load_project(self):
        err = self._require_window()
        if err:
            return err
        ext = brander_bridge.project_io.PROJECT_EXTENSION
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=(f"Blair Brander project (*{ext})", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file picker: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            scene = brander_bridge.project_io.load_project(path)
            return {"ok": True,
                    "scene": brander_bridge.scene_to_json(scene),
                    "path": path}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't load the project: {e}"}
