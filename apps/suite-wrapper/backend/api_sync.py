"""
api_sync.py — SyncMixin: the Sync workspace (probe/detect jobs, offset persistence with the one-sidecar-per-video merge, send-to-transcriber, XMEML).

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


class SyncMixin:
    # =====================================================================
    # Sync (A-Sync integration — contract addendum v3)
    # =====================================================================

    def sync_pick_video(self):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=SYNC_VIDEO_DIALOG_TYPES,
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file picker: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": path}

    def sync_pick_audio(self):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=SYNC_AUDIO_DIALOG_TYPES,
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file picker: {e}"}
        selected = _all_paths(result)
        if not selected:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "paths": selected}

    @staticmethod
    def _async_venv_error():
        if not os.path.isfile(paths.ASYNC_PYTHON):
            return {"ok": False, "error":
                    "A-Sync's virtual environment was not found at "
                    f"{paths.ASYNC_PYTHON} — set that app up first."}
        return None

    def sync_probe(self, paths_list):
        """Short-lived probe worker subprocess (A-Sync venv) for the info
        lines under the pickers — same parse-the-protocol-lines pattern as
        broll_export_xml."""
        try:
            wanted = [p for p in (paths_list or []) if isinstance(p, str) and p]
            if not wanted:
                return {"ok": False, "error": "No files were given to probe."}
            missing = [p for p in wanted if not os.path.isfile(p)]
            if missing:
                return {"ok": False, "error": f"File not found: {missing[0]}"}
            err = self._async_venv_error()
            if err:
                return err

            # BRAW pre-flight (suite-side only, mirrors broll_start): a
            # freshly-picked .braw file has no proxy yet, so this first
            # probe call will report it as "not ready" per sync_worker.py's
            # own resolve_decode_path gate — queue generation now so it's
            # more likely ready by the time the user retries/starts detect.
            braw_proxy_jobs = braw_bridge.queue_missing_proxies(self.jobs, wanted)

            params = {"mode": "probe", "paths": wanted}
            proc = subprocess.run(
                [paths.ASYNC_PYTHON, paths.SYNC_WORKER, json.dumps(params)],
                cwd=paths.ASYNC_DIR,
                capture_output=True,
                text=True,
                timeout=SYNC_PROBE_TIMEOUT_SECONDS,
            )
            data = None
            worker_error = None
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if msg.get("type") == "result":
                    data = msg.get("data")
                elif msg.get("type") == "error":
                    worker_error = msg.get("message")
            if worker_error:
                return {"ok": False, "error": worker_error}
            if not isinstance(data, dict):
                tail = (proc.stderr or "").strip().splitlines()[-10:]
                return {"ok": False, "error": "\n".join(tail) or
                        f"Probe worker exited with code {proc.returncode}."}
            return {"ok": True, "probes": data.get("probes") or {}, "braw_proxy_jobs": braw_proxy_jobs}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Probing took too long and was aborted."}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def sync_peaks(self, paths_list):
        """Downsampled waveform peaks (min/max per bucket) for the Sync
        workspace's waveform visual — the browser-based counterpart to
        the standalone A-Sync app's own WaveformCanvas. Same short-lived
        worker-subprocess pattern as sync_probe (A-Sync venv, one call
        per batch of files), just a heavier decode+downsample instead of
        a plain ffprobe metadata read, hence its own longer timeout.
        Per-file failures are recorded on that file's own entry
        (`{"error": str}`) rather than failing the whole batch — same
        policy as sync_probe."""
        try:
            wanted = [p for p in (paths_list or []) if isinstance(p, str) and p]
            if not wanted:
                return {"ok": False, "error": "No files were given."}
            missing = [p for p in wanted if not os.path.isfile(p)]
            if missing:
                return {"ok": False, "error": f"File not found: {missing[0]}"}
            err = self._async_venv_error()
            if err:
                return err

            # BRAW pre-flight — same rationale as sync_probe above.
            braw_proxy_jobs = braw_bridge.queue_missing_proxies(self.jobs, wanted)

            params = {"mode": "peaks", "paths": wanted}
            proc = subprocess.run(
                [paths.ASYNC_PYTHON, paths.SYNC_WORKER, json.dumps(params)],
                cwd=paths.ASYNC_DIR,
                capture_output=True,
                text=True,
                timeout=SYNC_PEAKS_TIMEOUT_SECONDS,
            )
            data = None
            worker_error = None
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if msg.get("type") == "result":
                    data = msg.get("data")
                elif msg.get("type") == "error":
                    worker_error = msg.get("message")
            if worker_error:
                return {"ok": False, "error": worker_error}
            if not isinstance(data, dict):
                tail = (proc.stderr or "").strip().splitlines()[-10:]
                return {"ok": False, "error": "\n".join(tail) or
                        f"Peaks worker exited with code {proc.returncode}."}
            return {"ok": True, "peaks": data.get("peaks") or {}, "braw_proxy_jobs": braw_proxy_jobs}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Loading the waveform took too long and was aborted."}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def sync_start(self, video_path, audio_paths, method="waveform"):
        """Offset detection as a background job (kind "sync" — no per-kind
        throttle registered, so it runs immediately) in A-Sync's venv."""
        try:
            if not video_path or not os.path.isfile(video_path):
                return {"ok": False, "error": f"Video file not found: {video_path}"}
            audio_list = [p for p in (audio_paths or []) if isinstance(p, str) and p]
            if not audio_list:
                return {"ok": False, "error": "No audio files were given."}
            missing = [p for p in audio_list if not os.path.isfile(p)]
            if missing:
                return {"ok": False, "error": f"File not found: {missing[0]}"}
            method = method or "waveform"
            if method not in SYNC_METHODS:
                return {"ok": False, "error": f"Unknown sync method: {method}"}
            err = self._async_venv_error()
            if err:
                return err

            # BRAW pre-flight (suite-side only, mirrors broll_start): only
            # video_path is realistically ever .braw, but queue_missing_
            # proxies is a no-op for anything else, so it's simplest to
            # just pass the whole set. Fire-and-forget — a .braw video
            # whose proxy isn't ready yet by the time the "sync" job below
            # reaches it surfaces sync_worker.py's own per-run error
            # instead (see that module's resolve_decode_path usage).
            braw_proxy_jobs = braw_bridge.queue_missing_proxies(
                self.jobs, [video_path] + audio_list)

            params = {
                "mode": "detect",
                "video_path": video_path,
                "audio_paths": audio_list,
                "method": method,
            }
            job_id = self.jobs.start_subprocess_job(
                kind="sync",
                label=os.path.basename(video_path),
                interpreter=paths.ASYNC_PYTHON,
                script=paths.SYNC_WORKER,
                params=params,
                cwd=paths.ASYNC_DIR,
            )
            return {"ok": True, "job_id": job_id, "braw_proxy_jobs": braw_proxy_jobs}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # ---------- offsets sidecar (<video>.sync-offsets.json) ----------

    @staticmethod
    def _sync_offsets_path(video_path):
        """Addendum v55: a .braw video_path redirects to a centralized
        fallback (braw_bridge.sync_offsets_path) instead of writing next
        to the — routinely read-only/removable — source."""
        return braw_bridge.sync_offsets_path(video_path)

    def sync_save_offsets(self, video_path, tracks, method="waveform"):
        """Persist the current (possibly nudged) offsets/routing for this
        video. Addendum v9 (one sidecar per video, not two): if a valid
        transcription cache already exists, the routing is written INTO it
        (`sync_tracks`/`sync_method`/`sync_updated_at`) instead of the
        separate sidecar, and any lingering sidecar file is removed — so a
        video transcribed before being synced never gets a second file at
        all. Otherwise, unchanged: writes the suite-owned
        `<video>.sync-offsets.json` sidecar (the video hasn't been
        transcribed yet, so there's nowhere else to put this)."""
        try:
            if not video_path or not isinstance(video_path, str):
                return {"ok": False, "error": "No video path was given."}
            clean_tracks = []
            for t in tracks or []:
                if not isinstance(t, dict) or not t.get("path"):
                    continue
                try:
                    offset = float(t.get("offset_seconds") or 0.0)
                except (TypeError, ValueError):
                    offset = 0.0
                clean = {"path": str(t["path"]), "offset_seconds": offset}
                # Routing fields (addendum v4) — persisted only when the
                # incoming dict carries them, so pre-v4 sidecars keep their
                # exact old shape (absent == enabled True / all channels).
                if "enabled" in t:
                    clean["enabled"] = bool(t["enabled"])
                if "channels" in t:
                    ch = t["channels"]
                    if ch is None:
                        clean["channels"] = None
                    elif isinstance(ch, (list, tuple)):
                        sel = []
                        for c in ch:
                            try:
                                sel.append(int(c))
                            except (TypeError, ValueError):
                                continue
                        clean["channels"] = sel
                clean_tracks.append(clean)

            method = str(method or "waveform")
            updated_at = time.time()

            cache = self._read_ivt_cache(video_path)
            if cache is not None:
                cache["sync_tracks"] = clean_tracks
                cache["sync_method"] = method
                cache["sync_updated_at"] = updated_at
                cache_path = self._ivt_cache_path(video_path)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
                sidecar = self._sync_offsets_path(video_path)
                if os.path.exists(sidecar):
                    try:
                        os.remove(sidecar)
                    except OSError:
                        pass  # cosmetic cleanup only -- the cache already has the data
                return {"ok": True, "path": cache_path}

            data = {
                "video_path": video_path,
                "method": method,
                "tracks": clean_tracks,
                "updated_at": updated_at,
            }
            sidecar = self._sync_offsets_path(video_path)
            with open(sidecar, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return {"ok": True, "path": sidecar}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def sync_load_offsets(self, video_path):
        """Restore saved offsets/routing. Addendum v9: checks the
        transcription cache's `sync_tracks` first (present there once a
        video has been both synced and transcribed); falls back to the
        standalone sidecar otherwise. If BOTH exist — a video synced and
        transcribed before this addendum shipped — the sidecar is folded
        into the cache and removed right here, on first touch, rather than
        needing a separate migration step. Response shape is identical
        regardless of which file backed it. Best-effort like the IVT
        cache: a missing or unreadable sidecar/cache is simply
        found:false, never an error."""
        try:
            if not video_path or not isinstance(video_path, str):
                return {"ok": False, "error": "No video path was given."}

            cache = self._read_ivt_cache(video_path)
            if isinstance(cache, dict) and "sync_tracks" in cache:
                return {
                    "ok": True,
                    "found": True,
                    "video_path": video_path,
                    "method": cache.get("sync_method") or "waveform",
                    "tracks": list(cache.get("sync_tracks") or []),
                    "updated_at": cache.get("sync_updated_at"),
                }

            sidecar = self._sync_offsets_path(video_path)
            if not os.path.exists(sidecar):
                return {"ok": True, "found": False}
            try:
                with open(sidecar, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                return {"ok": True, "found": False}
            if not isinstance(data, dict):
                return {"ok": True, "found": False}

            tracks = list(data.get("tracks") or [])
            method = data.get("method") or "waveform"
            updated_at = data.get("updated_at")

            # Legacy dual-file state: a valid cache exists but never got the
            # sync_tracks key (predates this addendum). Consolidate now.
            if isinstance(cache, dict):
                try:
                    cache["sync_tracks"] = tracks
                    cache["sync_method"] = method
                    cache["sync_updated_at"] = updated_at
                    with open(self._ivt_cache_path(video_path), "w", encoding="utf-8") as f:
                        json.dump(cache, f)
                    os.remove(sidecar)
                except Exception:
                    pass  # best-effort -- the sidecar-sourced data below is still returned

            return {
                "ok": True,
                "found": True,
                "video_path": data.get("video_path") or video_path,
                "method": method,
                "tracks": tracks,
                "updated_at": updated_at,
            }
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # ---------- handoffs ----------

    def sync_send_to_transcriber(self, video_path, audio_path, offset_seconds,
                                 model_label, enable_diarization, channel=None):
        """Proxy-free synced transcription: a normal "transcribe" job (the
        per-kind throttle applies) whose params also carry the external
        audio_path + offset_seconds — the worker feeds whisper the external
        audio and shifts the segments onto the video timeline. The cache
        lands next to the VIDEO, so the finished job behaves exactly like
        any other transcription downstream."""
        try:
            if not video_path or not os.path.isfile(video_path):
                return {"ok": False, "error": f"Video file not found: {video_path}"}
            if not audio_path or not os.path.isfile(audio_path):
                return {"ok": False, "error": f"Audio file not found: {audio_path}"}
            try:
                offset = float(offset_seconds or 0.0)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"Invalid offset: {offset_seconds!r}"}
            # Optional single-channel transcription (addendum v4 E): a
            # 1-based SOURCE channel, or None for the current whole-file mono
            # downmix. Passed through as the job param `audio_channel`.
            audio_channel = None
            if channel is not None:
                try:
                    audio_channel = int(channel)
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"Invalid channel: {channel!r}"}
                if audio_channel < 1:
                    return {"ok": False, "error": f"Invalid channel: {channel!r}"}
            repo = WHISPER_MODELS.get(model_label)
            if repo is None:
                return {"ok": False, "error": f"Unknown model: {model_label}"}
            if not os.path.isfile(paths.IVT_PYTHON):
                return {"ok": False, "error":
                        "The Local Interview Transcriber's virtual environment was not "
                        f"found at {paths.IVT_PYTHON} — set that app up first."}

            params = {
                "video_path": video_path,
                "model_repo": repo,
                "model_label": model_label,
                "enable_diarization": bool(enable_diarization),
                "app_dir": paths.IVT_DIR,
                "audio_path": audio_path,
                "offset_seconds": offset,
                "audio_channel": audio_channel,
            }
            label = (f"{os.path.basename(video_path)} ⟵ "
                     f"{os.path.basename(audio_path)}")
            job_id = self.jobs.start_subprocess_job(
                kind="transcribe",
                label=label,
                interpreter=paths.IVT_PYTHON,
                script=paths.TRANSCRIBE_WORKER,
                params=params,
                cwd=paths.IVT_DIR,
            )
            return {"ok": True, "job_id": job_id}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def sync_export_xml(self, payload):
        """Non-merged Premiere XML for the synced clips. payload =
        {video: {path, probe}, tracks: [{path, offset_seconds, probe}],
        include_camera_audio: bool, sequence_name: str} — probes come from
        the sync job result / sync_probe (the frontend passes them through)
        so no re-probing happens here. Built in-process by sync_xml.py."""
        try:
            if not isinstance(payload, dict):
                return {"ok": False, "error": "No export payload was given."}
            video = payload.get("video") or {}
            video_path = video.get("path")
            if not video_path:
                return {"ok": False, "error": "The export payload has no video path."}
            tracks = payload.get("tracks")
            if not isinstance(tracks, list) or not tracks:
                return {"ok": False, "error": "No synced audio tracks to export."}
            include_camera_audio = bool(payload.get("include_camera_audio"))
            stem = os.path.splitext(os.path.basename(video_path))[0]
            sequence_name = str(payload.get("sequence_name") or f"{stem} synced")

            err = self._require_window()
            if err:
                return err
            try:
                result = self.window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename=f"{stem} synced.xml",
                    file_types=("Premiere XML (*.xml)", "All files (*.*)"),
                )
            except Exception as e:
                traceback.print_exc()
                return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
            output_path = _first_path(result)
            if not output_path:
                return {"ok": False, "cancelled": True}

            xml_string, warnings = sync_xml.build_sync_xml(
                video, tracks, include_camera_audio, sequence_name)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(xml_string)
            return {"ok": True, "path": output_path, "warnings": warnings}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}
