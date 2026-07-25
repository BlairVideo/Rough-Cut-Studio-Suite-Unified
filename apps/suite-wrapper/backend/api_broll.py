"""
api_broll.py — BrollMixin: the B-Roll workspace (folder analysis, previews, send-to-edit, ranked-bin XML export).

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
from thumbnails import extract_thumbnail_data_uri, ffmpeg_available  # noqa: E402


def _resolve_playable_path(path, allowed_extensions):
    """Resolve `path` to something the inherited RCS PreviewServer can
    actually stream: a .braw path becomes its cached proxy (a plain .mov
    — braw_bridge.py's module docstring, Phase 2; RCS's PreviewServer
    itself never learns BRAW exists), anything else must already match
    `allowed_extensions`. Shared by broll_preview_url and
    sync_preview_url, whose only difference is which extensions they
    otherwise allow. Returns (playable_path, None) or (None, error_dict)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == braw_bridge.BRAW_EXTENSION:
        proxy_path = braw_bridge.find_cached_proxy(path)
        if proxy_path is None:
            return None, {"ok": False, "error":
                          "This BRAW clip's proxy hasn't finished generating yet — "
                          "check the Jobs drawer."}
        return proxy_path, None
    if ext not in allowed_extensions:
        return None, {"ok": False, "error":
                      "That file type can't be previewed (allowed: "
                      + ", ".join(allowed_extensions) + ")."}
    return path, None


class BrollMixin:
    # =====================================================================
    # B-Roll (favorite-card thumbnail + reveal-in-Finder: addendum v16)
    # =====================================================================

    def broll_pick_folder(self):
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

    def broll_start(self, folder, options=None):
        try:
            if not folder or not os.path.isdir(folder):
                return {"ok": False, "error": f"Folder not found: {folder}"}
            if not os.path.isfile(paths.BROLL_PYTHON):
                return {"ok": False, "error":
                        "The B-Roll Analyzer's virtual environment was not found at "
                        f"{paths.BROLL_PYTHON} — set that app up first."}

            # BRAW pre-flight (suite-side only — B-Roll Analyzer's own
            # analyzer.py never learns .braw exists, see braw_bridge.py's
            # module docstring): queue a "braw_proxy" job for any .braw
            # file in the folder that doesn't already have a cached proxy,
            # BEFORE starting the "broll" job below, so a proxy generated
            # while a previous Analyze run was showing "not ready yet" for
            # that clip is more likely to already be done by the time this
            # run's worker reaches it. Fire-and-forget: this call does not
            # wait on those jobs, and a .braw clip whose proxy isn't ready
            # yet by the time the worker gets to it simply reports its own
            # per-clip "not ready yet, re-run Analyze" error rather than
            # blocking the rest of the folder.
            braw_proxy_jobs = braw_bridge.queue_missing_proxies(
                self.jobs, braw_bridge.find_braw_files(folder))

            params = dict(options or {})
            params["mode"] = "analyze"
            params["folder"] = folder
            job_id = self.jobs.start_subprocess_job(
                kind="broll",
                label=os.path.basename(folder.rstrip(os.sep)) or folder,
                interpreter=paths.BROLL_PYTHON,
                script=paths.BROLL_WORKER,
                params=params,
                cwd=paths.BROLL_DIR,
            )
            return {"ok": True, "job_id": job_id, "braw_proxy_jobs": braw_proxy_jobs}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def broll_preview_url(self, path):
        """Serve a clip through the inherited RCS PreviewServer (lazy-
        started, per-file token URLs, Range support) so the B-Roll cards
        can play segments in-place. Same gate as RCS's
        _is_allowed_media_path: a real file with an allowed video
        container extension, nothing else ever reaches the server —
        EXCEPT a .braw path, transparently resolved to its cached proxy
        by _resolve_playable_path (braw_bridge.py's module docstring,
        Phase 2). RCS's PreviewServer itself never learns BRAW exists."""
        try:
            if not path or not isinstance(path, str) or not os.path.isfile(path):
                return {"ok": False, "error": f"Clip not found on disk: {path}"}
            playable_path, err = _resolve_playable_path(path, PREVIEW_VIDEO_EXTENSIONS)
            if err is not None:
                return err
            return {"ok": True, "url": self.preview_server.url_for(playable_path)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def sync_preview_url(self, path):
        """Like broll_preview_url, but the Sync workspace's proxy-free
        preview player also plays external AUDIO files (one <audio> per
        enabled track) alongside the muted picture. So the gate accepts BOTH
        the video containers AND the sync audio extensions; the same real-
        file + extension check keeps anything else from reaching the
        inherited RCS PreviewServer (which already handles mimetype + byte-
        range for <audio> and <video>). A .braw path resolves to its
        cached proxy exactly like broll_preview_url — audio-only sync
        sources are never .braw, so this only ever matters for the video
        side of a sync pair."""
        try:
            if not path or not isinstance(path, str) or not os.path.isfile(path):
                return {"ok": False, "error": f"File not found on disk: {path}"}
            allowed = PREVIEW_VIDEO_EXTENSIONS + SYNC_PREVIEW_AUDIO_EXTENSIONS
            playable_path, err = _resolve_playable_path(path, allowed)
            if err is not None:
                return err
            return {"ok": True, "url": self.preview_server.url_for(playable_path)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _broll_clip_duration(path, fallback=0.0, _memo=None):
        """Best-effort clip duration for the synthetic VTT's single cue:
        walk up from the clip's own folder looking for the analyzer's cache
        file whose entry (keyed by path relative to the cache's folder)
        covers this clip. Falls back to `fallback` — the VTT cue length is
        cosmetic (source duration display) so a rough value is acceptable.

        `_memo` (PERF-5): a per-call dict of cache_file -> parsed JSON (or
        None for unreadable). A multi-clip Send to Edit hits the SAME
        folder cache once per clip; the memo makes that one open+parse
        instead of N. Callers pass one dict per user action — never a
        long-lived cache, so edits to the cache file are always picked up
        by the next action."""
        if _memo is None:
            _memo = {}
        clip_dir = os.path.dirname(os.path.abspath(path))
        current = clip_dir
        for _ in range(8):  # bounded walk — never scan to filesystem root forever
            cache_file = os.path.join(current, BROLL_CACHE_FILENAME)
            if cache_file not in _memo:
                data = None
                if os.path.isfile(cache_file):
                    try:
                        with open(cache_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        data = None
                _memo[cache_file] = data
            data = _memo[cache_file]
            if data is not None:
                try:
                    rel = os.path.relpath(os.path.abspath(path), current)
                    entry = (data.get("clips") or {}).get(rel)
                    if entry and entry.get("duration"):
                        return float(entry["duration"])
                except Exception:
                    pass
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return fallback

    def broll_send_to_edit(self, selections):
        """Register each checkmarked segment as a "sent to the B-Roll tab"
        entry (kind="broll" in the shared favorites store — see
        favorites.py) instead of inserting Cuts-table rows directly. The
        B-Roll tab (in the Edit workspace) is the staging area; from
        there the user adds a specific card to Cuts via its own
        "+ Add to Cuts" button (suite_favorite_add_to_cuts, already
        generic over any favorite kind). Sending the same segment twice
        is idempotent — favorites.find() dedups by (vtt_path, start,
        end), so a re-send never creates a duplicate card."""
        try:
            if not selections:
                return {"ok": False, "error": "No segments were selected."}
            sources_added = []
            source_by_path = {}
            added = []
            cache_memo = {}  # one parse per folder cache for this whole send (PERF-5)

            def tc(seconds):
                res = self.format_timecode(seconds)
                return res["tc"] if isinstance(res, dict) and res.get("ok") else ""

            for sel in selections:
                path = (sel or {}).get("path")
                if not path or not os.path.isfile(path):
                    return {"ok": False, "error": f"Clip not found on disk: {path}"}
                try:
                    start = float(sel.get("start", 0.0))
                    end = float(sel.get("end", 0.0))
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"Invalid segment times for {os.path.basename(path)}."}
                try:
                    score = float(sel["score"]) if sel.get("score") is not None else None
                except (TypeError, ValueError):
                    score = None
                if path not in source_by_path:
                    duration = self._broll_clip_duration(
                        path, fallback=max(end, start) or 1.0, _memo=cache_memo)
                    info = handoff.ensure_broll_source(self, path, duration)
                    source_by_path[path] = info
                    sources_added.append(info["source_id"])
                info = source_by_path[path]
                if favorites.find(self.favorites, info["vtt_path"], start, end):
                    continue  # already sent — idempotent, not a duplicate card
                fav = favorites.build(
                    info["vtt_path"], info["source_id"], start, end,
                    tc(start), tc(end), speaker="", text=os.path.basename(path),
                    index=None, kind="broll", clip_path=path, score=score)
                self.favorites.append(fav)
                added.append(fav)
            if added:
                favorites.save(self.favorites)
            return {"ok": True, "added": added, "sources_added": sources_added}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def suite_broll_favorite_thumbnail(self, path, start_seconds):
        """A storyboard-style frame for a B-Roll tab card, pulled straight
        from the clip file via ffmpeg — the same extractor RCS's own
        Cuts-row thumbnails use (sources.py get_thumbnail). Deliberately
        bypasses get_thumbnail/self.media_paths entirely: a B-Roll tab
        entry already carries its own `clip_path` (see favorites.py), so
        unlike RCS's source_id-keyed thumbnails this needs no source to
        be currently loaded/linked this session — the card can show a
        thumbnail even for an entry sent in an earlier launch.

        A .braw `clip_path` is resolved to its cached proxy before ffmpeg
        ever sees it — RCS's thumbnails.py stays BRAW-unaware, same
        substitution-at-the-call-site pattern as broll_preview_url."""
        if not ffmpeg_available():
            return {"ok": False, "error": "ffmpeg not found on this machine — thumbnails need it."}
        if not path or not isinstance(path, str) or not os.path.isfile(path):
            return {"ok": False, "error": f"Clip not found on disk: {path}"}
        try:
            start_seconds = float(start_seconds)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid timestamp."}
        decode_path = path
        if os.path.splitext(path)[1].lower() == braw_bridge.BRAW_EXTENSION:
            proxy_path = braw_bridge.find_cached_proxy(path)
            if proxy_path is None:
                return {"ok": False, "error":
                        "This BRAW clip's proxy hasn't finished generating yet — "
                        "check the Jobs drawer."}
            decode_path = proxy_path
        data_uri = extract_thumbnail_data_uri(decode_path, start_seconds)
        if not data_uri:
            return {"ok": False, "error": "Couldn't extract a frame from that file/timestamp."}
        return {"ok": True, "data_uri": data_uri}

    def suite_reveal_broll_media(self, path):
        """The "media link" affordance on a B-Roll tab card (addendum
        v16): reveal the favorited segment's source clip in the Finder.
        macOS-only (`open -R`), matching this suite's target platform —
        same command Local Interview Transcriber's own reveal_in_finder
        uses, reimplemented here rather than imported since sibling-app
        files are never modified NOR imported as a runtime dependency."""
        try:
            if not path or not isinstance(path, str) or not os.path.isfile(path):
                return {"ok": False, "error": f"File not found on disk: {path}"}
            if sys.platform != "darwin":
                return {"ok": False, "error": "Revealing files in a file manager is only supported on macOS."}
            subprocess.run(["open", "-R", path], check=False)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def broll_export_xml(self, job_id, selected_paths=None):
        """Export the analyzer's own Premiere XML for a finished analysis
        job. Runs through a short-lived broll worker subprocess because
        rebuilding ClipResults needs cv2/numpy (analyzer imports), which
        the suite venv intentionally doesn't have."""
        try:
            job, err = self._finished_job(job_id, "broll")
            if err:
                return err
            folder = job.result.get("folder")
            options = dict(job.result.get("options") or {})
            if not folder or not os.path.isdir(folder):
                return {"ok": False, "error": f"The analyzed folder is no longer available: {folder}"}

            win_err = self._require_window()
            if win_err:
                return win_err
            try:
                result = self.window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename="Best B-Roll Selects.xml",
                    file_types=("Premiere XML (*.xml)", "All files (*.*)"),
                )
            except Exception as e:
                traceback.print_exc()
                return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
            output_path = _first_path(result)
            if not output_path:
                return {"ok": False, "cancelled": True}

            params = dict(options)
            params.update({
                "mode": "export_xml",
                "folder": folder,
                "output_path": output_path,
                "selected_paths": list(selected_paths) if selected_paths else None,
            })
            proc = subprocess.run(
                [paths.BROLL_PYTHON, paths.BROLL_WORKER, json.dumps(params)],
                cwd=paths.BROLL_DIR,
                capture_output=True,
                text=True,
                timeout=EXPORT_XML_TIMEOUT_SECONDS,
            )
            worker_error = None
            got_result = False
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if msg.get("type") == "result":
                    got_result = True
                elif msg.get("type") == "error":
                    worker_error = msg.get("message")
            if worker_error:
                return {"ok": False, "error": worker_error}
            if not got_result:
                tail = (proc.stderr or "").strip().splitlines()[-10:]
                return {"ok": False, "error": "\n".join(tail) or
                        f"XML export worker exited with code {proc.returncode}."}
            return {"ok": True, "path": output_path}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "The XML export took too long and was aborted."}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}
