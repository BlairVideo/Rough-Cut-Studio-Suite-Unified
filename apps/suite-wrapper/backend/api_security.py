"""
api_security.py — SecurityMixin: the SEC-1/2/3 hardening overrides of inherited RCS Api methods (Keychain-backed Gemini key, transcript media-path gate, private autosave).

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
                   synced_audio_splice, favorites, braw_bridge)
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
    import braw_bridge
    from jobs import get_job_manager
    from api_shared import *  # noqa: F401,F403

# api_shared put RCS's backend dir on sys.path (same bootstrap RCS's own
# main.py performs), so RCS backend modules are importable from here on.
import webview            # noqa: E402
import api as rcs_api     # noqa: E402  (Rough Cut Studio's backend api module)


class SecurityMixin:
    # =====================================================================
    # Security hardening (MASTER_BLUEPRINT.md §3) — all suite-side; no
    # sibling-app files are modified.
    # =====================================================================

    def _suite_autosave_path(self):
        """A private, non-predictable location for RCS's autosave snapshot,
        replacing the inherited tempfile.gettempdir() default. Falls back to
        the inherited path if the support dir can't be created."""
        try:
            base = os.path.join(
                os.path.expanduser("~/Library/Application Support"),
                "RoughCutStudioSuite",
            )
            os.makedirs(base, exist_ok=True)
            try:
                os.chmod(base, 0o700)
            except OSError:
                pass
            return os.path.join(base, "autosave.json")
        except Exception:
            # Keep whatever the parent constructor already set.
            return getattr(self, "_autosave_path", None)

    def _lock_autosave_perms(self):
        try:
            if self._autosave_path and os.path.exists(self._autosave_path):
                os.chmod(self._autosave_path, 0o600)
        except OSError:
            pass

    def _autosave(self):
        result = super()._autosave()
        self._lock_autosave_perms()
        return result

    def autosave_working_state(self, payload):
        result = super().autosave_working_state(payload)
        self._lock_autosave_perms()
        return result

    # ---------- SEC-1: Gemini key in the system keychain, not plaintext ----

    def load_saved_api_key(self):
        """Prefer the macOS Keychain (the same secure store already used for
        the HF token and Blair Brander's key). Fall back to RCS's legacy
        plaintext .env for existing installs — and on that first read, move
        the key into the Keychain and scrub the plaintext copy so the secret
        stops living on disk. The key value is never logged."""
        try:
            import keyring
            key = (keyring.get_password(RCS_KEYRING_SERVICE,
                                        RCS_KEYRING_GEMINI_KEY) or "").strip()
            if key:
                return {"ok": True, "api_key": key}
        except Exception:
            pass  # keychain unavailable — fall through to legacy read
        legacy = super().load_saved_api_key()
        legacy_key = ""
        if isinstance(legacy, dict):
            legacy_key = (legacy.get("api_key") or "").strip()
        if legacy_key:
            try:
                import keyring
                keyring.set_password(RCS_KEYRING_SERVICE,
                                     RCS_KEYRING_GEMINI_KEY, legacy_key)
                self._scrub_env_gemini_key()
            except Exception:
                pass  # best-effort migration; still return the key so the UI works
        return {"ok": True, "api_key": legacy_key}

    def save_api_key_to_disk(self, api_key):
        """Store the key in the system keychain instead of a plaintext .env.
        The method name is kept because RCS's own frontend calls it by name
        for its "remember key on this machine" toggle. An empty string
        clears the stored key."""
        key = (api_key or "").strip()
        try:
            import keyring
            if key:
                keyring.set_password(RCS_KEYRING_SERVICE,
                                     RCS_KEYRING_GEMINI_KEY, key)
            else:
                try:
                    keyring.delete_password(RCS_KEYRING_SERVICE,
                                            RCS_KEYRING_GEMINI_KEY)
                except keyring.errors.PasswordDeleteError:
                    pass
            self._scrub_env_gemini_key()
            return {"ok": True, "path": "system keychain"}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't update the keychain: {e}"}

    def _scrub_env_gemini_key(self):
        """Remove any GEMINI_API_KEY line from RCS's legacy .env, preserving
        any other lines. Deletes the file if it ends up empty. Best-effort —
        a failure here never breaks key save/load."""
        env_path = getattr(rcs_api, "ENV_PATH", None)
        if not env_path or not os.path.exists(env_path):
            return
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            kept = [ln for ln in lines
                    if not ln.strip().startswith("GEMINI_API_KEY=")]
            if kept == lines:
                return  # nothing to scrub
            remaining = "".join(kept).strip()
            if remaining:
                tmp = env_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(remaining + "\n")
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                os.replace(tmp, env_path)
            else:
                os.remove(env_path)
        except OSError:
            pass

    # ---------- SEC-2: gate transcript-embedded media paths ----------

    def _suite_prune_disallowed_media(self):
        """A transcript's embedded `NOTE Source video:` path is read from
        file content and auto-linked by RCS with only an existence check —
        unlike the project-load path, which gates it through
        _is_allowed_media_path. Re-apply that same gate here so a hostile
        transcript can't point a source's media at an arbitrary file (which
        would then be fed to ffmpeg and served over the local preview HTTP
        server). Legitimately linked media are real video files and pass the
        gate unchanged; only disallowed entries are dropped."""
        try:
            for sid, mp in list(self.media_paths.items()):
                if mp and not rcs_api._is_allowed_media_path(mp):
                    self.media_paths.pop(sid, None)
                    try:
                        self.preview_server.forget(mp)
                    except Exception:
                        pass
        except Exception:
            traceback.print_exc()

    def _queue_braw_proxies_for_linked_media(self):
        """Eagerly start proxy generation (fire-and-forget, idempotent —
        see queue_missing_proxies's own docstring) for any .braw file
        that just became linked media, e.g. via an embedded
        `NOTE Source video:` auto-link surviving the prune above now that
        VIDEO_EXTENSIONS includes .braw. Best-effort: a failure here must
        never break transcript ingestion itself."""
        try:
            braw_bridge.queue_missing_proxies(self.jobs, self.media_paths.values())
        except Exception:
            traceback.print_exc()

    def pick_transcript_files(self):
        result = super().pick_transcript_files()
        self._suite_prune_disallowed_media()
        self._queue_braw_proxies_for_linked_media()
        return result

    def _add_transcript(self, path):
        result = super()._add_transcript(path)
        self._suite_prune_disallowed_media()
        self._queue_braw_proxies_for_linked_media()
        # The returned dict may advertise a media_path we just pruned; keep
        # it honest so the frontend doesn't show a link that isn't there.
        try:
            if isinstance(result, dict):
                sid = result.get("source_id")
                if sid is not None and sid not in self.media_paths:
                    if result.get("media_path"):
                        result["media_path"] = None
                        result["auto_linked"] = False
        except Exception:
            pass
        return result
