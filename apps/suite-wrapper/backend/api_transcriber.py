"""
api_transcriber.py — TranscriberMixin: the Transcribe workspace (models, HF token, jobs, transcript cache read/edit, send-to-edit).

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


class TranscriberMixin:
    # =====================================================================
    # Transcriber
    # =====================================================================

    def transcriber_models(self):
        try:
            return {
                "ok": True,
                "models": [{"label": label, "repo": repo} for label, repo in WHISPER_MODELS.items()],
                "default_label": DEFAULT_WHISPER_LABEL,
            }
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def transcriber_pick_videos(self):
        err = self._require_window()
        if err:
            return err
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=VIDEO_DIALOG_TYPES,
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file picker: {e}"}
        selected = _all_paths(result)
        if not selected:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "paths": selected}

    def transcriber_hf_token_status(self):
        try:
            import keyring
            token = keyring.get_password(KEYRING_SERVICE, KEYRING_HF_TOKEN_KEY)
            return {"ok": True, "present": bool(token)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't read the keychain: {e}"}

    def transcriber_save_hf_token(self, token):
        """Store (or, for an empty string, delete) the HF token in the
        system keychain — the exact slot the standalone transcriber reads,
        so both apps share one token. Never written to any file."""
        try:
            import keyring
            token = (token or "").strip()
            if token:
                keyring.set_password(KEYRING_SERVICE, KEYRING_HF_TOKEN_KEY, token)
            else:
                try:
                    keyring.delete_password(KEYRING_SERVICE, KEYRING_HF_TOKEN_KEY)
                except keyring.errors.PasswordDeleteError:
                    pass  # nothing stored — deleting nothing is fine
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't update the keychain: {e}"}

    def transcriber_set_parallel(self, n):
        try:
            self.jobs.set_kind_limit("transcribe", n)
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def transcriber_start(self, paths_list, model_label, enable_diarization):
        """One subprocess job per video, run with the transcriber's own
        venv python. Jobs beyond the per-kind limit queue automatically."""
        try:
            if not paths_list:
                return {"ok": False, "error": "No video files were given."}
            repo = WHISPER_MODELS.get(model_label)
            if repo is None:
                return {"ok": False, "error": f"Unknown model: {model_label}"}
            if not os.path.isfile(paths.IVT_PYTHON):
                return {"ok": False, "error":
                        "The Local Interview Transcriber's virtual environment was not "
                        f"found at {paths.IVT_PYTHON} — set that app up first."}
            missing = [p for p in paths_list if not os.path.isfile(p)]
            if missing:
                return {"ok": False, "error": f"File not found: {missing[0]}"}

            # BRAW pre-flight (suite-side only, same fire-and-forget
            # pattern as api_broll.py's broll_start): queue a proxy job
            # for any .braw path here that doesn't already have a cached
            # proxy, BEFORE starting the per-file "transcribe" jobs below.
            # transcribe_worker.py's own wait_for_decode_path call closes
            # the race for whichever proxy isn't ready by the time its
            # job actually runs.
            braw_proxy_jobs = braw_bridge.queue_missing_proxies(self.jobs, paths_list)

            job_ids = []
            for video_path in paths_list:
                params = {
                    "video_path": video_path,
                    "model_repo": repo,
                    "model_label": model_label,
                    "enable_diarization": bool(enable_diarization),
                    "app_dir": paths.IVT_DIR,
                }
                job_ids.append(self.jobs.start_subprocess_job(
                    kind="transcribe",
                    label=os.path.basename(video_path),
                    interpreter=paths.IVT_PYTHON,
                    script=paths.TRANSCRIBE_WORKER,
                    params=params,
                    cwd=paths.IVT_DIR,
                ))
            return {"ok": True, "job_ids": job_ids, "braw_proxy_jobs": braw_proxy_jobs}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # ---------- transcript cache (the shared .ivt-cache.json) ----------

    @staticmethod
    def _ivt_cache_path(video_path):
        """Cache file lives right next to the video, e.g.
        'interview.mp4' -> 'interview.mp4.ivt-cache.json' — the exact file
        the standalone transcriber reads and writes. A .braw source is
        the one exception (addendum v55) — braw_bridge.ivt_cache_path
        redirects it to a centralized fallback, since .braw media
        routinely lives on read-only/removable camera cards where this
        write isn't reliable. This one change also covers api_sync.py's
        sync_save_offsets/sync_load_offsets, which read/write the
        transcription cache via this same method (inherited through the
        composed SuiteApi)."""
        return braw_bridge.ivt_cache_path(video_path)

    @classmethod
    def _read_ivt_cache(cls, video_path):
        """Read the cache for a video, or None when it's absent, unreadable,
        or stale. Validity mirrors the standalone app's load_cache() exactly:
        stored video_size == st_size AND video_mtime == int(st_mtime); if the
        video itself can't be statted the cache is still honored (e.g. the
        source volume is unplugged but the edit state should survive)."""
        cache_path = cls._ivt_cache_path(video_path)
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        try:
            stat = os.stat(video_path)
            if data.get("video_size") != stat.st_size or \
               data.get("video_mtime") != int(stat.st_mtime):
                return None  # video changed since this cache was written
        except OSError:
            pass
        return data if isinstance(data, dict) else None

    @staticmethod
    def _visible_cache_segments(cache):
        """Apply the cache's exclusions and display labels, mirroring the
        standalone app's _visible_segments(): segments whose RAW speaker is
        excluded are dropped, and each surviving segment's speaker becomes
        its display label (falling back to the raw name)."""
        excluded = set(cache.get("excluded_speakers") or [])
        labels = cache.get("speaker_labels") or {}
        visible = []
        for seg in cache.get("segments") or []:
            raw = str(seg.get("speaker", "") or "")
            if raw in excluded:
                continue
            seg = dict(seg)
            seg["speaker"] = (labels.get(raw) or "").strip() or raw
            visible.append(seg)
        return visible

    def transcriber_load_cache(self, video_path):
        """Reopen a past transcription without re-transcribing: returns the
        cache's editable state, or found:false when there's no (valid)
        cache for that video."""
        try:
            if not video_path or not isinstance(video_path, str):
                return {"ok": False, "error": "No video path was given."}
            data = self._read_ivt_cache(video_path)
            if data is None:
                return {"ok": True, "found": False, "segments": [],
                        "speakers": [], "speaker_labels": {},
                        "excluded_speakers": []}
            return {
                "ok": True,
                "found": True,
                "segments": list(data.get("segments") or []),
                "speakers": list(data.get("speakers") or []),
                "speaker_labels": dict(data.get("speaker_labels") or {}),
                "excluded_speakers": list(data.get("excluded_speakers") or []),
            }
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def transcriber_update_transcript(self, video_path, segments, speakers,
                                      speaker_labels, excluded_speakers):
        """Rewrite the video's .ivt-cache.json with the edited state
        (schema identical to the transcribe worker's write_ivt_cache, but
        honoring the passed labels/exclusions). video_size/mtime are
        recomputed fresh at write time so the file stays valid for both
        this suite and the standalone app."""
        try:
            if not video_path or not isinstance(video_path, str):
                return {"ok": False, "error": "No video path was given."}
            if not isinstance(segments, list):
                return {"ok": False, "error": "segments must be a list."}

            clean_segments = []
            for i, seg in enumerate(segments):
                if not isinstance(seg, dict):
                    return {"ok": False, "error": f"Segment {i + 1} is not an object."}
                start, end = seg.get("start"), seg.get("end")
                if not isinstance(start, (int, float)) or isinstance(start, bool) or \
                   not isinstance(end, (int, float)) or isinstance(end, bool):
                    return {"ok": False,
                            "error": f"Segment {i + 1} needs numeric start and end times."}
                text, speaker = seg.get("text"), seg.get("speaker")
                if not isinstance(text, str) or not isinstance(speaker, str):
                    return {"ok": False,
                            "error": f"Segment {i + 1} needs string text and speaker."}
                clean_segments.append({
                    "start": float(start),
                    "end": float(end),
                    "text": text,
                    "speaker": speaker,
                    # Preserved when the frontend echoes them back; a
                    # hand-added segment simply gets neutral confidences.
                    "avg_logprob": float(seg["avg_logprob"])
                    if isinstance(seg.get("avg_logprob"), (int, float))
                    and not isinstance(seg.get("avg_logprob"), bool) else 0.0,
                    "no_speech_prob": float(seg["no_speech_prob"])
                    if isinstance(seg.get("no_speech_prob"), (int, float))
                    and not isinstance(seg.get("no_speech_prob"), bool) else 0.0,
                })

            clean_speakers = [str(s) for s in (speakers or []) if str(s).strip()]
            if not clean_speakers:
                seen = []
                for seg in clean_segments:
                    if seg["speaker"] and seg["speaker"] not in seen:
                        seen.append(seg["speaker"])
                clean_speakers = seen

            clean_labels = {str(k): str(v) for k, v in (speaker_labels or {}).items()}
            clean_excluded = [str(x) for x in (excluded_speakers or [])]

            # Fresh size/mtime at write time — same best-effort fallback as
            # the standalone save_cache() when the video can't be statted.
            try:
                stat = os.stat(video_path)
                video_size, video_mtime = stat.st_size, int(stat.st_mtime)
            except OSError:
                video_size, video_mtime = None, None

            data = {
                "path": video_path,
                "name": os.path.basename(video_path),
                "video_size": video_size,
                "video_mtime": video_mtime,
                "speakers": clean_speakers,
                "segments": clean_segments,
                "speaker_labels": clean_labels,
                "excluded_speakers": clean_excluded,
            }
            # Carry forward the sync-related extra keys (addendum v3/v9) from
            # whatever the cache held before this edit — a raw read, not
            # gated on the staleness check, since this write makes the
            # fingerprint valid again regardless. Without this, the very
            # first transcript edit after a sync+transcribe merge would
            # silently drop sync_tracks (the sidecar it replaced is already
            # gone by then, so the association would be lost outright, not
            # just recomputed).
            cache_path = self._ivt_cache_path(video_path)
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
            except Exception:
                old_data = None
            if isinstance(old_data, dict):
                for key in ("audio_source", "sync_offset_seconds",
                            "sync_tracks", "sync_method", "sync_updated_at"):
                    if key in old_data:
                        data[key] = old_data[key]
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return {"ok": True, "cache_path": cache_path}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def transcriber_send_to_edit(self, job_id):
        try:
            job, err = self._finished_job(job_id, "transcribe")
            if err:
                return err
            video_path = job.result.get("video_path")
            if not video_path:
                return {"ok": False, "error": "That job produced no transcript segments."}
            # The cache is the editable truth: if a (still-valid) cache
            # exists it may carry edits/labels/exclusions newer than the
            # job's in-memory result, so prefer it.
            cache = self._read_ivt_cache(video_path)
            if cache is not None:
                segments = self._visible_cache_segments(cache)
                if not segments:
                    return {"ok": False, "error":
                            "No transcript segments are left to send — every "
                            "speaker is excluded (or the transcript is empty)."}
            else:
                segments = job.result.get("segments") or []
                if not segments:
                    return {"ok": False, "error": "That job produced no transcript segments."}
            return handoff.send_transcript_to_edit(self, video_path, segments)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def transcriber_send_cache_to_edit(self, video_path):
        """Send a cache-loaded transcription (no job this session) to the
        Edit workspace, honoring its labels/exclusions."""
        try:
            if not video_path or not isinstance(video_path, str):
                return {"ok": False, "error": "No video path was given."}
            cache = self._read_ivt_cache(video_path)
            if cache is None:
                return {"ok": False, "error":
                        f"No saved transcription was found for "
                        f"{os.path.basename(video_path)} — transcribe it first."}
            segments = self._visible_cache_segments(cache)
            if not segments:
                return {"ok": False, "error":
                        "No transcript segments are left to send — every "
                        "speaker is excluded (or the transcript is empty)."}
            return handoff.send_transcript_to_edit(self, video_path, segments)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}
