"""
api.py

The bridge object exposed to the frontend via pywebview's `js_api`. Every
method here is callable from JavaScript as `window.pywebview.api.<method>(...)`.

Design goals relevant to safety/security:
  * The Gemini API key never touches disk unless the user explicitly clicks
    "Remember key on this machine" (writes to a local .env next to the app,
    with a clear warning in the UI). By default it lives only in memory for
    the running session.
  * All file reads/writes go through native OS dialogs, so the app never
    silently reads or writes files outside what the user picks.
  * Every segment the model chooses is re-validated against the real parsed
    transcript (correct source_id, in-range segment_index, sane trim
    amounts) before it can influence the exported script or XML. A bad or
    hallucinated Gemini response fails validation instead of producing a
    corrupted edit.

Threading note: pywebview calls every js_api method (the methods on this
class) on a worker thread, not the main thread that owns the GUI event
loop. Tkinter -- and most native GUI toolkits -- must only be touched from
the main thread; creating a Tk() root here would crash the app (reliably
on macOS, intermittently elsewhere). File dialogs therefore go through
`window.create_file_dialog(...)`, which pywebview implements to be safe
to call from this worker thread.
"""

import os
import json
import uuid
import difflib
import tempfile
import threading
import traceback
from collections import OrderedDict
from datetime import datetime

from transcript_parser import (
    parse_transcript_file,
    seconds_to_smpte,
    timecode_to_seconds,
    parse_duration_string,
    seconds_to_duration_label,
    TRANSCRIPT_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from gemini_client import generate_script as gemini_generate_script, GeminiError, DEFAULT_MODEL as GEMINI_DEFAULT_MODEL
from llama_client import (
    generate_script as llama_generate_script,
    list_models as llama_list_models,
    LlamaError,
    DEFAULT_MODEL as LLAMA_DEFAULT_MODEL,
    DEFAULT_HOST as LLAMA_DEFAULT_HOST,
)
from xml_builder import build_premiere_xml
from fcpxml_builder import build_fcpxml
from otio_builder import build_otio
from script_writer import build_script_markdown
from thumbnails import ffmpeg_available
from video_export import build_preview_export
from preview_server import PreviewServer
from sources import SourceManager

import webview

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
PROJECT_FILE_VERSION = 1

# Two deliberately different minimum-clip-length floors, named explicitly so
# the difference reads as intentional rather than as two magic numbers that
# drifted apart:
#   - MIN_MODEL_TRIM_SECONDS guards the *model's* chosen in/out trims in
#     _resolve_segments. It matches the 0.3s floor gemini_client.py's
#     SYSTEM_INSTRUCTION explicitly tells the model to respect; if a
#     trimmed segment comes back shorter than that, it's treated as an
#     invalid trim and the full untrimmed segment is used instead.
#   - MIN_MANUAL_CLIP_SECONDS guards *hand-edited* cuts on the Cuts tab in
#     rebuild_outputs. It's intentionally much smaller (a couple of frames
#     at most common frame rates) since an editor may deliberately want a
#     very short manual cut (a quick flash frame, a whip-pan cutaway) that
#     the model would never be asked to produce on its own.
MIN_MODEL_TRIM_SECONDS = 0.3
MIN_MANUAL_CLIP_SECONDS = 0.1

# Note: MAX_THUMBNAIL_CACHE_ENTRIES now lives in sources.py, next to the
# _thumbnail_cache it bounds (moved there along with get_thumbnail() as
# part of the SourceManager extraction).


def _first_path(result):
    """create_file_dialog returns None, a string, or a tuple/list of strings
    depending on platform and dialog type -- normalize to a single path."""
    if not result:
        return None
    if isinstance(result, (list, tuple)):
        return result[0] if result else None
    return result


def _all_paths(result):
    if not result:
        return []
    if isinstance(result, (list, tuple)):
        return list(result)
    return [result]


def _coerce_int(value):
    """Best-effort int conversion for model-provided fields. Gemini's
    schema-enforced output always gives real JSON integers, but Ollama's
    JSON *mode* only guarantees syntactically valid JSON, not a specific
    shape — a smaller/local model can and does sometimes write numbers as
    strings (e.g. "2" or "2.0"). Returns None if the value genuinely isn't
    a number, so the caller can still reject it, just not because of a
    formatting quirk that isn't really wrong."""
    if isinstance(value, bool):
        return None  # bool is technically an int subclass; never treat True/False as 0/1 here
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(s)
        except ValueError:
            try:
                return int(round(float(s)))
            except ValueError:
                return None
    return None


def _coerce_float(value, default=0.0):
    """Same idea as _coerce_int but for the offset-seconds fields."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _is_allowed_transcript_path(path):
    """Restricts a project-file-supplied transcript `path` to the same file
    types the transcript picker itself allows (see
    SourceManager.pick_transcript_files()'s dialog filter and
    TRANSCRIPT_EXTENSIONS in transcript_parser.py). A `.rcstudio.json`
    project file is just hand-editable JSON text -- without this check, a
    malicious one could point `path` at an arbitrary file (e.g. `~/.ssh/
    id_rsa`), have it "parsed" as a lenient transcript format, and have its
    contents sent to an LLM provider as a normal-looking source. Requires
    the path to actually be a regular file, not a directory or special
    file (os.path.exists() alone would accept those too)."""
    if not path or not isinstance(path, str):
        return False
    if not os.path.isfile(path):
        return False
    return os.path.splitext(path)[1].lower() in TRANSCRIPT_EXTENSIONS


def _is_allowed_media_path(path):
    """Same idea as _is_allowed_transcript_path but for a project-file-
    supplied `media_path` -- restricted to the same video containers
    link_media_file()'s dialog filter and batch_relink_media()'s folder
    scan already allow (VIDEO_EXTENSIONS in transcript_parser.py). Without
    this, an untrusted media_path becomes valid input to ffmpeg (thumbnails
    /preview export) and gets served byte-for-byte over the local preview
    HTTP server."""
    if not path or not isinstance(path, str):
        return False
    if not os.path.isfile(path):
        return False
    return os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS


MAX_HISTORY_ENTRIES = 30


class Api:
    def __init__(self):
        # Transcript/source state (sources, media_paths, fps, drop_frame,
        # _thumbnail_cache) lives on this SourceManager -- see sources.py --
        # and is exposed as plain attributes on Api via the @property
        # proxies right after __init__, so every other place in this class
        # that reads/writes those names keeps working unchanged.
        # SourceManager takes a reference to this Api instance (not a
        # frozen `window` argument) because main.py assigns `self.window`
        # only *after* webview.create_window(...) returns, i.e. after this
        # constructor has already finished.
        self._sources_mgr = SourceManager(self)
        self.last_result = None  # cache of last successful generation, for export
        self.window = None       # set by main.py right after webview.create_window(...)
        self.history = []        # in-session record of every generate/edit/revise, newest last
        self.sequences = {}      # name -> saved snapshot; lets one project hold multiple named cuts
        self.preview_server = PreviewServer()  # lazily started on first preview request
        self._current_project_path = None  # set once a project has been saved/loaded, for Ctrl/Cmd+S quick-save
        self._last_meta = {}     # most recent {sequence_name, prompt, model, target_duration}, for autosave
        self._autosave_path = os.path.join(tempfile.gettempdir(), "rough_cut_studio_autosave.json")
        # generate()/revise() make long, blocking network/LLM calls and then
        # mutate shared state (self.last_result, self.history, self._last_meta,
        # autosave). pywebview dispatches each js_api call on a worker thread,
        # so two calls fired close together (e.g. a double-clicked button, or
        # Revise clicked while a Generate is still in flight) could otherwise
        # interleave and corrupt that state or the autosave snapshot. This
        # lock makes a second call fail fast with a clear message instead of
        # silently racing the first one.
        self._generation_lock = threading.Lock()

    # ---------- source-state proxies ----------
    #
    # sources/media_paths/fps/drop_frame/_thumbnail_cache all physically
    # live on self._sources_mgr (see sources.py) now, but are exposed here
    # as plain attributes so every other method in this class that reads or
    # writes e.g. `self.sources` -- generate()/_generate_locked(),
    # _resolve_segments(), _finalize_outputs(),
    # _apply_loaded_project_unsafe(), _build_project_dict(), new_project(),
    # etc. -- keeps working completely unchanged.

    @property
    def sources(self):
        return self._sources_mgr.sources

    @sources.setter
    def sources(self, value):
        self._sources_mgr.sources = value

    @property
    def media_paths(self):
        return self._sources_mgr.media_paths

    @media_paths.setter
    def media_paths(self, value):
        self._sources_mgr.media_paths = value

    @property
    def fps(self):
        return self._sources_mgr.fps

    @fps.setter
    def fps(self, value):
        self._sources_mgr.fps = value

    @property
    def drop_frame(self):
        return self._sources_mgr.drop_frame

    @drop_frame.setter
    def drop_frame(self, value):
        self._sources_mgr.drop_frame = value

    @property
    def _thumbnail_cache(self):
        return self._sources_mgr._thumbnail_cache

    @_thumbnail_cache.setter
    def _thumbnail_cache(self, value):
        self._sources_mgr._thumbnail_cache = value

    # ---------- transcript management ----------
    #
    # Actual logic lives in SourceManager (backend/sources.py) now; these
    # are thin one-line delegators. Kept as real methods (not removed) with
    # unchanged names/signatures because js_api exposes them directly to
    # the frontend by name.

    def pick_transcript_files(self):
        return self._sources_mgr.pick_transcript_files()

    def _add_transcript(self, path):
        return self._sources_mgr._add_transcript(path)

    def remove_source(self, source_id):
        return self._sources_mgr.remove_source(source_id)

    def set_fps(self, fps):
        return self._sources_mgr.set_fps(fps)

    def set_drop_frame(self, enabled):
        return self._sources_mgr.set_drop_frame(enabled)

    def format_timecode(self, seconds):
        return self._sources_mgr.format_timecode(seconds)

    def link_media_file(self, source_id):
        return self._sources_mgr.link_media_file(source_id)

    def batch_relink_media(self):
        return self._sources_mgr.batch_relink_media()

    def list_sources(self):
        return self._sources_mgr.list_sources()

    def get_transcript_view(self, source_id):
        return self._sources_mgr.get_transcript_view(source_id)

    # ---------- storyboard thumbnails ----------

    def get_thumbnail(self, source_id, in_seconds):
        return self._sources_mgr.get_thumbnail(source_id, in_seconds)

    # ---------- video preview ----------

    def get_preview_url(self, source_id):
        return self._sources_mgr.get_preview_url(source_id)

    # ---------- generation ----------

    def _call_llm(self, provider, model, prompt, sources, api_key=None, host=None, target_seconds=None):
        """Dispatches a script-generation request to whichever provider is
        selected. Returns the raw parsed dict on success; raises GeminiError
        or LlamaError on failure, which generate()/revise() catch and turn
        into a normal {"ok": False, "error": ...} response — the rest of
        the pipeline (_resolve_segments, _finalize_outputs) doesn't care
        which provider produced the raw segments."""
        if provider == "llama":
            return llama_generate_script(
                prompt=prompt,
                sources=sources,
                model=model or LLAMA_DEFAULT_MODEL,
                host=host or LLAMA_DEFAULT_HOST,
                on_retry=self._notify_retry,
                on_start=self._notify_generation_start,
                target_seconds=target_seconds,
            )
        return gemini_generate_script(
            api_key=api_key,
            prompt=prompt,
            sources=sources,
            model=model or GEMINI_DEFAULT_MODEL,
            on_retry=self._notify_retry,
            target_seconds=target_seconds,
        )

    def list_ollama_models(self, host=None):
        """Lists models already pulled into the local Ollama install, for
        the Llama model dropdown. A connection failure here (Ollama not
        running, wrong host) is reported back as a normal error, not an
        exception — the person may not have switched to Llama yet."""
        try:
            models = llama_list_models(host or LLAMA_DEFAULT_HOST)
            return {"ok": True, "models": models}
        except LlamaError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Unexpected error contacting Ollama: {e}"}

    def generate(self, params):
        """Public entry point — serializes against revise() via
        _generation_lock (see __init__) before doing any real work."""
        if not self._generation_lock.acquire(blocking=False):
            return {
                "ok": False,
                "error": "Another generation or revision is already in progress — wait for it to finish first.",
            }
        try:
            return self._generate_locked(params)
        finally:
            self._generation_lock.release()

    def _generate_locked(self, params):
        """
        params: {
            "provider": str ("gemini" or "llama", default "gemini"),
            "api_key": str (required for provider="gemini"),
            "ollama_host": str (optional, provider="llama" only),
            "prompt": str,
            "sequence_name": str,
            "model": str (optional),
            "target_duration": str (optional, e.g. "60", "90s", "1:30"),
        }
        """
        params = params or {}
        provider = params.get("provider") or "gemini"
        api_key = params.get("api_key", "").strip()
        ollama_host = params.get("ollama_host") or LLAMA_DEFAULT_HOST
        prompt = params.get("prompt", "").strip()
        sequence_name = params.get("sequence_name") or "Generated Sequence"
        model = params.get("model") or (LLAMA_DEFAULT_MODEL if provider == "llama" else GEMINI_DEFAULT_MODEL)

        if not self.sources:
            return {"ok": False, "error": "Add at least one transcript before generating."}
        if not prompt:
            return {"ok": False, "error": "Enter a creative brief / prompt first."}
        if provider == "gemini" and not api_key:
            return {"ok": False, "error": "Enter your Gemini API key first."}
        if provider == "llama" and not model:
            return {"ok": False, "error": "Choose a Llama model first."}

        try:
            target_seconds = parse_duration_string(params.get("target_duration"))
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        self._last_meta = {
            "sequence_name": sequence_name,
            "prompt": prompt,
            "provider": provider,
            "model": model,
            "ollama_host": ollama_host,
            "target_duration": params.get("target_duration"),
        }  # never includes the API key -- autosave/project files must not carry secrets

        gemini_sources = []
        for source_id, entry in self.sources.items():
            gemini_sources.append(
                {
                    "source_id": source_id,
                    "segments": [s.to_dict() for s in entry["segments"]],
                }
            )

        try:
            raw = self._call_llm(
                provider, model, prompt, gemini_sources,
                api_key=api_key, host=ollama_host, target_seconds=target_seconds,
            )
        except (GeminiError, LlamaError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Unexpected error calling {'Llama' if provider == 'llama' else 'Gemini'}: {e}"}

        resolved, problems = self._resolve_segments(raw)
        if not resolved:
            return {
                "ok": False,
                "error": self._no_segments_error(provider, problems),
                "details": problems,
            }

        final_sequence_name = raw.get("sequence_name") or sequence_name
        narrative_summary = raw.get("narrative_summary", "")

        outputs = self._finalize_outputs(final_sequence_name, narrative_summary, resolved,
                                          target_seconds=target_seconds, history_label="Generated")
        outputs["warnings"] = problems + outputs.get("warnings", [])
        return outputs

    def revise(self, params):
        """Public entry point — serializes against generate() via
        _generation_lock (see __init__) before doing any real work."""
        if not self._generation_lock.acquire(blocking=False):
            return {
                "ok": False,
                "error": "Another generation or revision is already in progress — wait for it to finish first.",
            }
        try:
            return self._revise_locked(params)
        finally:
            self._generation_lock.release()

    def _revise_locked(self, params):
        """
        Asks the selected LLM (Gemini or local Llama via Ollama) to revise
        the current main cut based on a short instruction (e.g. "make the
        opening punchier", "trim to fit the target better", "cut the
        section about the budget") instead of starting over from the
        original brief. Reuses the same generation path as generate() with
        an augmented prompt — no separate flow or schema per provider.
        The provider/model used here doesn't have to match whatever
        produced the cut being revised — switching providers mid-session
        is fully supported.

        Only the main (V1) track is revised; any B-roll clips you've placed
        manually on the Cuts tab are carried over unchanged, since those
        are editorial decisions this app never asks the model to make.

        params: {
            "provider": str ("gemini" or "llama", default "gemini"),
            "api_key": str (required for provider="gemini"),
            "ollama_host": str (optional, provider="llama" only),
            "instruction": str,       # what to change
            "prompt": str,            # the original creative brief, for context
            "sequence_name": str (optional),
            "model": str (optional),
            "target_duration": str (optional),
        }
        """
        if not self.last_result:
            return {"ok": False, "error": "Generate a script first, then you can ask for a revision."}

        params = params or {}
        provider = params.get("provider") or "gemini"
        api_key = params.get("api_key", "").strip()
        ollama_host = params.get("ollama_host") or LLAMA_DEFAULT_HOST
        instruction = params.get("instruction", "").strip()
        original_prompt = params.get("prompt", "").strip()
        model = params.get("model") or (LLAMA_DEFAULT_MODEL if provider == "llama" else GEMINI_DEFAULT_MODEL)
        sequence_name = params.get("sequence_name") or self.last_result["sequence_name"]

        if provider == "gemini" and not api_key:
            return {"ok": False, "error": "Enter your Gemini API key first."}
        if provider == "llama" and not model:
            return {"ok": False, "error": "Choose a Llama model first."}
        if not instruction:
            return {"ok": False, "error": "Describe what you'd like to change."}

        try:
            target_seconds = parse_duration_string(params.get("target_duration"))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if target_seconds is None:
            target_seconds = self.last_result.get("target_seconds")

        self._last_meta = {
            "sequence_name": sequence_name,
            "prompt": original_prompt,
            "provider": provider,
            "model": model,
            "ollama_host": ollama_host,
            "target_duration": params.get("target_duration"),
        }

        current_main = [
            s for s in self.last_result.get("resolved_segments", [])
            if s.get("track", "main") != "broll"
        ]
        current_main.sort(key=lambda s: s["order"])
        if current_main:
            cut_lines = [
                f"- {s['source_id']} [{s['in_tc']} \u2192 {s['out_tc']}]: {s.get('note', '')}"
                for s in current_main
            ]
            current_cut_summary = "\n".join(cut_lines)
        else:
            current_cut_summary = "(no cuts yet)"

        combined_prompt = (
            f"{original_prompt}\n\n"
            "--- REVISION REQUEST ---\n"
            "This is the CURRENT cut, already in place as a starting point:\n"
            f"{current_cut_summary}\n\n"
            f"Requested change: {instruction}\n\n"
            "Apply this change and return a complete revised cut list (not just "
            "the parts that changed), still following all the rules above."
        )

        gemini_sources = []
        for source_id, entry in self.sources.items():
            gemini_sources.append(
                {
                    "source_id": source_id,
                    "segments": [s.to_dict() for s in entry["segments"]],
                }
            )

        try:
            raw = self._call_llm(
                provider, model, combined_prompt, gemini_sources,
                api_key=api_key, host=ollama_host, target_seconds=target_seconds,
            )
        except (GeminiError, LlamaError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Unexpected error calling {'Llama' if provider == 'llama' else 'Gemini'}: {e}"}

        resolved, problems = self._resolve_segments(raw)
        if not resolved:
            return {
                "ok": False,
                "error": self._no_segments_error(provider, problems),
                "details": problems,
            }

        # Carry forward existing B-roll overlays untouched — Gemini never
        # sees or edits them; they're a Cuts-tab-only editorial decision.
        existing_broll = [
            dict(s) for s in self.last_result.get("resolved_segments", [])
            if s.get("track") == "broll"
        ]
        combined = resolved + existing_broll

        final_sequence_name = raw.get("sequence_name") or sequence_name
        narrative_summary = raw.get("narrative_summary", "")

        outputs = self._finalize_outputs(final_sequence_name, narrative_summary, combined,
                                          target_seconds=target_seconds,
                                          history_label=f"Revised: {instruction[:60]}")
        outputs["warnings"] = problems + outputs.get("warnings", [])
        return outputs

    def rebuild_outputs(self, payload):
        """Public entry point — also serializes via _generation_lock (see
        __init__) since it mutates the same shared state (self.last_result,
        self.history, self._last_meta) as generate()/revise(), even though
        it doesn't itself make a network call."""
        if not self._generation_lock.acquire(blocking=False):
            return {
                "ok": False,
                "error": "Another generation or revision is already in progress — wait for it to finish first.",
            }
        try:
            return self._rebuild_outputs_locked(payload)
        finally:
            self._generation_lock.release()

    def _rebuild_outputs_locked(self, payload):
        """
        Rebuilds the script + XML from an edited cut list, without calling
        Gemini again. Used by the Cuts tab's Apply Changes button.

        payload: {
            "sequence_name": str,
            "narrative_summary": str (optional, carried over from the last
                generation if omitted),
            "target_duration": str (optional, e.g. "60", "1:30"; carried
                over from the last generation if omitted),
            "segments": [
                {"source_id": str, "in_tc": str, "out_tc": str,
                 "note": str, "on_screen_text": str,
                 "track": "main" | "broll" (default "main"),
                 "timeline_start_tc": str (required if track is "broll")},
                ...
            ]  # list order is the desired cut order for "main" segments;
               # "broll" segments are placed independently at their own
               # timeline_start_tc regardless of their position in this list
        }
        """
        payload = payload or {}
        items = payload.get("segments") or []
        if not items:
            return {"ok": False, "error": "There are no cuts to build — add at least one."}

        sequence_name = payload.get("sequence_name") or (
            self.last_result["sequence_name"] if self.last_result else "Sequence"
        )
        narrative_summary = payload.get("narrative_summary")
        if narrative_summary is None:
            narrative_summary = self.last_result.get("narrative_summary", "") if self.last_result else ""

        target_duration = payload.get("target_duration")
        if target_duration is None and self.last_result:
            target_duration = self.last_result.get("target_seconds")
        try:
            target_seconds = parse_duration_string(target_duration)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        resolved = []
        problems = []
        for i, item in enumerate(items):
            source_id = item.get("source_id")
            if source_id not in self.sources:
                problems.append(f"Cut {i + 1}: unknown source '{source_id}' — skipped.")
                continue

            track = item.get("track") or "main"
            if track not in ("main", "broll"):
                track = "main"

            try:
                in_seconds = timecode_to_seconds(item.get("in_tc") or "00:00:00:00", self.fps, self.drop_frame)
                out_seconds = timecode_to_seconds(item.get("out_tc") or "00:00:00:00", self.fps, self.drop_frame)
            except ValueError:
                problems.append(f"Cut {i + 1}: couldn't read the in/out timecode — skipped.")
                continue
            if out_seconds - in_seconds < MIN_MANUAL_CLIP_SECONDS:
                problems.append(f"Cut {i + 1}: out point must come after the in point — skipped.")
                continue

            timeline_start_seconds = None
            audio_mode = "silent"
            duck_db = -12.0
            if track == "broll":
                try:
                    timeline_start_seconds = timecode_to_seconds(
                        item.get("timeline_start_tc") or "00:00:00:00", self.fps, self.drop_frame
                    )
                except ValueError:
                    problems.append(f"Cut {i + 1}: couldn't read the B-roll timeline start — placed at 00:00.")
                    timeline_start_seconds = 0.0
                audio_mode = item.get("audio_mode") or "silent"
                if audio_mode not in ("silent", "full", "duck_main"):
                    audio_mode = "silent"
                try:
                    duck_db = float(item.get("duck_db", -12.0))
                except (TypeError, ValueError):
                    duck_db = -12.0
                duck_db = max(-60.0, min(0.0, duck_db))  # keep it in a sane, audible range

            # Start from a copy of the incoming item so any field the
            # frontend attaches that this backend doesn't know about (e.g.
            # the client-side `_cid` used for row identity across
            # re-renders — see HANDOFF.md) survives the round-trip instead
            # of being silently dropped. The explicit fields below are the
            # authoritative, freshly-computed values and always win over
            # whatever was in `item`.
            resolved.append(
                {
                    **item,
                    "order": i,
                    "track": track,
                    "source_id": source_id,
                    "source_name": self._display_clip_name(source_id),
                    "in_seconds": in_seconds,
                    "out_seconds": out_seconds,
                    "in_tc": seconds_to_smpte(in_seconds, self.fps, self.drop_frame),
                    "out_tc": seconds_to_smpte(out_seconds, self.fps, self.drop_frame),
                    "note": item.get("note", ""),
                    "on_screen_text": item.get("on_screen_text", ""),
                    "source_text": item.get("source_text", ""),
                    "timeline_start_seconds": timeline_start_seconds,
                    "timeline_start_tc": (
                        seconds_to_smpte(timeline_start_seconds, self.fps, self.drop_frame)
                        if timeline_start_seconds is not None else None
                    ),
                    "audio_mode": audio_mode,
                    "duck_db": duck_db,
                }
            )

        if not resolved:
            return {"ok": False, "error": "None of the cuts were valid.", "details": problems}

        outputs = self._finalize_outputs(sequence_name, narrative_summary, resolved,
                                          target_seconds=target_seconds,
                                          history_label=f"Edited ({len(resolved)} cuts)")
        outputs["warnings"] = problems + outputs.get("warnings", [])
        return outputs

    def _finalize_outputs(self, sequence_name: str, narrative_summary: str, resolved: list,
                           target_seconds=None, history_label: str = "Updated"):
        """Builds the script + XML from a resolved segment list and caches
        the result for export. Shared by generate(), rebuild_outputs(),
        revise(), and project loading, so every path produces identical,
        valid output — and every call is recorded to history (see
        _record_history) so past iterations stay reachable within the app.

        `resolved` may mix "main" cuts (placed sequentially on V1/A1/A2)
        and "broll" cuts (placed on V2 at their own explicit timeline
        position, silently). Main cuts are renumbered 0..n-1 in their
        given relative order; broll cuts keep their own order for display
        but are positioned by timeline_start_seconds, not list order.
        """
        main_list = [dict(s) for s in resolved if s.get("track", "main") != "broll"]
        broll_list = [dict(s) for s in resolved if s.get("track") == "broll"]

        if not main_list:
            return {
                "ok": False,
                "error": "At least one cut must be on the main track (V1) — mark at least one cut "
                         "as \"Main\" instead of B-Roll before building.",
            }

        for i, s in enumerate(main_list):
            s["order"] = i
        for i, s in enumerate(broll_list):
            s["order"] = i

        # Compute each main cut's own timeline position for display (the
        # Cuts tab shows this, read-only, so you know where to park B-roll
        # relative to it). This mirrors the sequential frame math the XML
        # builder does independently for the actual export.
        running = 0.0
        for s in main_list:
            s["timeline_start_seconds"] = running
            s["timeline_start_tc"] = seconds_to_smpte(running, self.fps, self.drop_frame)
            running += (s["out_seconds"] - s["in_seconds"])
        main_runtime_seconds = running

        script_md = build_script_markdown(
            sequence_name, narrative_summary, main_list, self.fps,
            broll_segments=broll_list, target_seconds=target_seconds,
        )

        xml_segments = []
        missing_media = set()
        for seg in main_list:
            media_path = self.media_paths.get(seg["source_id"])
            if not media_path:
                missing_media.add(seg["source_id"])
                media_path = seg["source_name"]  # placeholder path; still produces valid XML
            xml_segments.append(
                {
                    "order": seg["order"],
                    "source_path": media_path,
                    "source_name": seg["source_name"],
                    "in_seconds": seg["in_seconds"],
                    "out_seconds": seg["out_seconds"],
                    "note": seg.get("note"),
                }
            )

        xml_broll_segments = []
        for seg in broll_list:
            media_path = self.media_paths.get(seg["source_id"])
            if not media_path:
                missing_media.add(seg["source_id"])
                media_path = seg["source_name"]
            xml_broll_segments.append(
                {
                    "source_path": media_path,
                    "source_name": seg["source_name"],
                    "in_seconds": seg["in_seconds"],
                    "out_seconds": seg["out_seconds"],
                    "note": seg.get("note"),
                    "timeline_start_seconds": seg.get("timeline_start_seconds") or 0.0,
                    "audio_mode": seg.get("audio_mode", "silent"),
                }
            )

        # For every "duck_main" B-roll clip, find which main clip(s) it
        # overlaps in time and flatten-attenuate those for their whole
        # duration (see xml_builder's module docstring for why this is a
        # whole-clip reduction rather than a frame-precise fade around
        # just the overlap). If more than one B-roll duck the same main
        # clip, the strongest (most negative) reduction wins.
        main_duck_db = {}
        for seg in broll_list:
            if seg.get("audio_mode") != "duck_main":
                continue
            duck_db = seg.get("duck_db", -12.0)
            b_start = seg.get("timeline_start_seconds") or 0.0
            b_end = b_start + max(0.0, seg["out_seconds"] - seg["in_seconds"])
            for m in main_list:
                m_start = m["timeline_start_seconds"]
                m_end = m_start + (m["out_seconds"] - m["in_seconds"])
                if m_start < b_end and m_end > b_start:  # time ranges overlap
                    existing = main_duck_db.get(m["order"])
                    main_duck_db[m["order"]] = duck_db if existing is None else min(existing, duck_db)

        xml_str, xmeml_warnings = build_premiere_xml(sequence_name, self.fps, xml_segments,
                                                      broll_segments=xml_broll_segments, main_duck_db=main_duck_db)
        fcpxml_str, fcpxml_warnings = build_fcpxml(sequence_name, self.fps, xml_segments,
                                                    broll_segments=xml_broll_segments, main_duck_db=main_duck_db)
        otio_str, otio_warnings = build_otio(sequence_name, self.fps, xml_segments,
                                              broll_segments=xml_broll_segments)

        combined_resolved = main_list + broll_list

        self.last_result = {
            "sequence_name": sequence_name,
            "narrative_summary": narrative_summary,
            "script_markdown": script_md,
            "xml": xml_str,
            "fcpxml": fcpxml_str,
            "otio": otio_str,
            "resolved_segments": combined_resolved,
            "target_seconds": target_seconds,
        }

        duration_info = {
            "main_runtime_seconds": main_runtime_seconds,
            "main_runtime_label": seconds_to_duration_label(main_runtime_seconds),
            "target_seconds": target_seconds,
            "target_label": seconds_to_duration_label(target_seconds) if target_seconds else None,
        }
        if target_seconds:
            diff = main_runtime_seconds - target_seconds
            duration_info["diff_seconds"] = diff
            duration_info["over_target"] = abs(diff) > max(5.0, target_seconds * 0.15)

        warnings = xmeml_warnings + fcpxml_warnings + otio_warnings + [
            f"No media file linked for source '{s}' — XML will reference a placeholder path."
            for s in sorted(missing_media)
        ]
        if duration_info.get("over_target"):
            direction = "over" if duration_info["diff_seconds"] > 0 else "under"
            warnings.append(
                f"Runtime is {seconds_to_duration_label(abs(duration_info['diff_seconds']))} {direction} "
                f"your {duration_info['target_label']} target."
            )

        self._record_history(
            label=history_label,
            sequence_name=sequence_name,
            narrative_summary=narrative_summary,
            script_markdown=script_md,
            xml=xml_str,
            fcpxml=fcpxml_str,
            resolved_segments=combined_resolved,
            duration=duration_info,
        )

        self._last_meta["sequence_name"] = sequence_name
        self._last_meta["target_duration"] = target_seconds
        self._autosave()

        return {
            "ok": True,
            "sequence_name": sequence_name,
            "narrative_summary": narrative_summary,
            "resolved_segments": combined_resolved,
            "script_markdown": script_md,
            "xml_preview": xml_str,
            "fcpxml_preview": fcpxml_str,
            "otio_preview": otio_str,
            "duration": duration_info,
            "warnings": warnings,
            "history": self._history_summary(),
        }

    def _record_history(self, label, sequence_name, narrative_summary, script_markdown,
                         xml, fcpxml, resolved_segments, duration):
        entry = {
            "id": uuid.uuid4().hex,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "label": label,
            "sequence_name": sequence_name,
            "narrative_summary": narrative_summary,
            "script_markdown": script_markdown,
            "xml": xml,
            "fcpxml": fcpxml,
            "resolved_segments": [dict(s) for s in resolved_segments],
            "duration": duration,
        }
        self.history.append(entry)
        if len(self.history) > MAX_HISTORY_ENTRIES:
            self.history = self.history[-MAX_HISTORY_ENTRIES:]

    def _history_summary(self):
        """Compact list for the History tab — newest first. Full script/XML
        text is only sent when a specific entry is restored, to keep this
        summary call cheap even with many iterations."""
        summary = []
        for i, entry in enumerate(self.history):
            cut_count = len([s for s in entry["resolved_segments"] if s.get("track", "main") != "broll"])
            broll_count = len([s for s in entry["resolved_segments"] if s.get("track") == "broll"])
            summary.append(
                {
                    "index": i,
                    "timestamp": entry["timestamp"],
                    "label": entry["label"],
                    "cut_count": cut_count,
                    "broll_count": broll_count,
                    "runtime_label": entry["duration"].get("main_runtime_label", ""),
                }
            )
        return list(reversed(summary))

    def list_history(self):
        return {"ok": True, "history": self._history_summary()}

    def get_history_entry(self, index):
        try:
            index = int(index)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid history entry."}
        if index < 0 or index >= len(self.history):
            return {"ok": False, "error": "That history entry no longer exists."}
        entry = self.history[index]
        return {
            "ok": True,
            "index": index,
            "label": entry["label"],
            "timestamp": entry["timestamp"],
            "sequence_name": entry["sequence_name"],
            "narrative_summary": entry["narrative_summary"],
            "script_markdown": entry["script_markdown"],
            "xml": entry["xml"],
            "fcpxml": entry["fcpxml"],
            "resolved_segments": entry["resolved_segments"],
            "duration": entry["duration"],
        }

    def compare_history_entries(self, index_a, index_b):
        """A cut-by-cut diff of two history entries' MAIN tracks (B-roll is
        summarized as a count, not diffed in detail, to keep this readable).
        Cuts are matched by content — same source + in/out + note — using
        Python's own difflib, the same technique behind most text diffs, so
        a cut that just moved position shows as unchanged rather than as a
        spurious remove+add pair."""
        try:
            index_a = int(index_a)
            index_b = int(index_b)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid history entries."}
        if not (0 <= index_a < len(self.history)) or not (0 <= index_b < len(self.history)):
            return {"ok": False, "error": "One of those history entries no longer exists."}

        entry_a = self.history[index_a]
        entry_b = self.history[index_b]

        def main_cuts(entry):
            cuts = [s for s in entry["resolved_segments"] if s.get("track", "main") != "broll"]
            return sorted(cuts, key=lambda s: s["order"])

        def signature(seg):
            return (seg.get("source_id"), seg.get("in_tc"), seg.get("out_tc"), seg.get("note", ""))

        def brief(seg):
            return {
                "source_name": seg.get("source_name"),
                "in_tc": seg.get("in_tc"),
                "out_tc": seg.get("out_tc"),
                "note": seg.get("note", ""),
            }

        a_cuts = main_cuts(entry_a)
        b_cuts = main_cuts(entry_b)
        a_sigs = [signature(s) for s in a_cuts]
        b_sigs = [signature(s) for s in b_cuts]

        sm = difflib.SequenceMatcher(a=a_sigs, b=b_sigs, autojunk=False)
        rows = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    rows.append({"type": "same", "a": brief(a_cuts[i1 + k]), "b": brief(b_cuts[j1 + k])})
            elif tag == "replace":
                span = max(i2 - i1, j2 - j1)
                for k in range(span):
                    a_seg = brief(a_cuts[i1 + k]) if i1 + k < i2 else None
                    b_seg = brief(b_cuts[j1 + k]) if j1 + k < j2 else None
                    rows.append({"type": "changed", "a": a_seg, "b": b_seg})
            elif tag == "delete":
                for k in range(i1, i2):
                    rows.append({"type": "removed", "a": brief(a_cuts[k]), "b": None})
            elif tag == "insert":
                for k in range(j1, j2):
                    rows.append({"type": "added", "a": None, "b": brief(b_cuts[k])})

        def side_summary(entry, cuts):
            broll_count = len([s for s in entry["resolved_segments"] if s.get("track") == "broll"])
            return {
                "label": entry["label"],
                "timestamp": entry["timestamp"],
                "sequence_name": entry["sequence_name"],
                "cut_count": len(cuts),
                "broll_count": broll_count,
                "runtime_label": entry["duration"].get("main_runtime_label", ""),
            }

        return {
            "ok": True,
            "a": side_summary(entry_a, a_cuts),
            "b": side_summary(entry_b, b_cuts),
            "rows": rows,
        }

    def restore_history_entry(self, index):
        """Public entry point — also serializes via _generation_lock (see
        __init__) since it mutates the same shared state (self.last_result,
        self.history, self._last_meta) as generate()/revise()/
        rebuild_outputs()."""
        if not self._generation_lock.acquire(blocking=False):
            return {
                "ok": False,
                "error": "Another generation or revision is already in progress — wait for it to finish first.",
            }
        try:
            try:
                index = int(index)
            except (TypeError, ValueError):
                return {"ok": False, "error": "Invalid history entry."}
            if index < 0 or index >= len(self.history):
                return {"ok": False, "error": "That history entry no longer exists."}

            entry = self.history[index]
            resolved = [dict(s) for s in entry["resolved_segments"]]
            for seg in resolved:
                # Re-derive the display name in case media links changed since
                # this snapshot was recorded.
                seg["source_name"] = self._display_clip_name(seg["source_id"])

            return self._finalize_outputs(
                entry["sequence_name"],
                entry["narrative_summary"],
                resolved,
                target_seconds=entry["duration"].get("target_seconds"),
                history_label=f"Restored: {entry['label']}",
            )
        finally:
            self._generation_lock.release()

    # ---------- named sequences (multiple cuts sharing one project) ----------
    #
    # History is an automatic, chronological log of everything that's
    # happened. Sequences are the opposite: an explicit, user-named save
    # point — "Social Cut", "Broadcast Cut" — that lives alongside History
    # rather than replacing it, so you can keep a few deliberately-named
    # versions of the edit around, all sharing the same sources and media
    # links, all saved in the same project file.

    def save_sequence(self, name):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "Give the sequence a name first."}
        if not self.last_result:
            return {"ok": False, "error": "Generate or edit a cut before saving it as a sequence."}

        self.sequences[name] = {
            "sequence_name": self.last_result["sequence_name"],
            "narrative_summary": self.last_result.get("narrative_summary", ""),
            "resolved_segments": [dict(s) for s in self.last_result.get("resolved_segments", [])],
            "target_seconds": self.last_result.get("target_seconds"),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        return {"ok": True, "sequences": self._sequence_summary()}

    def _sequence_summary(self):
        summary = []
        for name, snap in self.sequences.items():
            cut_count = len([s for s in snap["resolved_segments"] if s.get("track", "main") != "broll"])
            broll_count = len([s for s in snap["resolved_segments"] if s.get("track") == "broll"])
            summary.append(
                {
                    "name": name,
                    "sequence_name": snap["sequence_name"],
                    "saved_at": snap["saved_at"],
                    "cut_count": cut_count,
                    "broll_count": broll_count,
                }
            )
        summary.sort(key=lambda s: s["saved_at"], reverse=True)
        return summary

    def list_sequences(self):
        return {"ok": True, "sequences": self._sequence_summary()}

    def load_sequence(self, name):
        """Public entry point — also serializes via _generation_lock (see
        __init__) since it mutates the same shared state (self.last_result,
        self.history, self._last_meta) as generate()/revise()/
        rebuild_outputs()."""
        if not self._generation_lock.acquire(blocking=False):
            return {
                "ok": False,
                "error": "Another generation or revision is already in progress — wait for it to finish first.",
            }
        try:
            snap = self.sequences.get(name)
            if not snap:
                return {"ok": False, "error": f"No sequence named '{name}'."}

            resolved = [dict(s) for s in snap["resolved_segments"]]
            for seg in resolved:
                seg["source_name"] = self._display_clip_name(seg["source_id"])

            outputs = self._finalize_outputs(
                snap["sequence_name"],
                snap["narrative_summary"],
                resolved,
                target_seconds=snap.get("target_seconds"),
                history_label=f"Loaded sequence '{name}'",
            )
            outputs["sequences"] = self._sequence_summary()
            return outputs
        finally:
            self._generation_lock.release()

    def delete_sequence(self, name):
        if name in self.sequences:
            del self.sequences[name]
        return {"ok": True, "sequences": self._sequence_summary()}

    def _notify_retry(self, attempt, max_attempts, wait_seconds, reason):
        if not self.window:
            return
        try:
            payload = json.dumps(
                {"attempt": attempt, "max_attempts": max_attempts,
                 "wait_seconds": round(wait_seconds, 1), "reason": reason}
            )
            self.window.evaluate_js(
                f"window.onGenerationRetry && window.onGenerationRetry({payload})"
            )
        except Exception:
            # A UI push failing should never break the retry loop, but it
            # should still be visible in the console rather than silently
            # invisible -- this print is the only trace a real bug here
            # would leave.
            traceback.print_exc()

    def _notify_generation_start(self, info):
        """Called once from llama_client right before the Ollama request is
        sent, so the context size actually chosen (the main lever behind
        how slow a local call will be) is visible instead of a silent
        implementation detail — useful for the person to judge whether a
        given transcript set is going to be a fast or slow call before it
        finishes."""
        if not self.window:
            return
        try:
            payload = json.dumps(info)
            self.window.evaluate_js(
                f"window.onGenerationStart && window.onGenerationStart({payload})"
            )
        except Exception:
            traceback.print_exc()  # same reasoning as _notify_retry above

    def _no_segments_error(self, provider, problems):
        """Builds the error shown when _resolve_segments rejected every
        segment. Provider-neutral (this used to hardcode "Gemini's
        response...", which was actively misleading once Llama/Ollama was
        added as a second provider), and includes the actual per-segment
        rejection reasons inline — the frontend only surfaces `error` as a
        status message, not the separate `details` list, so folding the
        specifics in here is what makes this diagnosable instead of a dead
        end, which matters a lot more now that a local model can produce
        a wider variety of malformed shapes than Gemini's schema-enforced
        output ever does."""
        who = "Llama" if provider == "llama" else "Gemini"
        msg = f"{who}'s response didn't reference any valid transcript segments."
        if problems:
            shown = problems[:5]
            msg += " " + "; ".join(shown)
            if len(problems) > len(shown):
                msg += f" (+{len(problems) - len(shown)} more)"
        if provider == "llama":
            msg += (
                " Smaller local models sometimes struggle to follow the exact JSON shape asked for — "
                "trying again, or trying a larger/more capable model, often helps."
            )
        return msg

    def _resolve_segments(self, raw: dict):
        """Validate every segment the model chose against the real, parsed
        transcripts. Works identically regardless of which provider
        produced `raw` — Gemini's schema-enforced output and Ollama's
        looser JSON mode both funnel through here, which is also why
        numeric fields are coerced rather than strictly type-checked (see
        _coerce_int/_coerce_float)."""
        problems = []
        resolved = []
        if not isinstance(raw, dict):
            problems.append(f"Expected a JSON object in the response, got {type(raw).__name__}: {str(raw)[:200]}")
            return resolved, problems

        segs = raw.get("script_segments")
        if segs is None:
            # The model returned valid JSON, but not shaped the way we
            # asked — surface what it actually sent instead of a bare
            # "nothing worked", which is nearly undiagnosable otherwise.
            preview = ", ".join(f"{k}: {str(v)[:60]!r}" for k, v in list(raw.items())[:4])
            problems.append(f"The response had no 'script_segments' array. Got instead: {preview}")
            segs = []
        elif isinstance(segs, list) and len(segs) == 0:
            # Distinct from the above: the shape was right, the model just
            # chose to return nothing. Worth calling out separately since
            # the fix is different (retry / rephrase the brief) from a
            # shape mismatch (which usually means switching models).
            problems.append("The response's script_segments array was present but empty — the model didn't choose any segments.")

        for i, item in enumerate(segs):
            if not isinstance(item, dict):
                problems.append(f"Segment {i}: expected an object, got {type(item).__name__} — skipped.")
                continue

            source_id = item.get("source_id")
            entry = self.sources.get(source_id)
            if entry is None:
                problems.append(f"Segment {i}: unknown source_id '{source_id}' — skipped.")
                continue

            raw_idx = item.get("segment_index")
            idx = _coerce_int(raw_idx)
            if idx is None or idx < 0 or idx >= len(entry["segments"]):
                problems.append(
                    f"Segment {i}: segment_index {raw_idx!r} isn't a valid index for '{source_id}' "
                    f"(has {len(entry['segments'])} segments) — skipped."
                )
                continue

            seg = entry["segments"][idx]
            in_off = max(0.0, _coerce_float(item.get("in_offset_seconds"), 0.0))
            out_off = max(0.0, _coerce_float(item.get("out_offset_seconds"), 0.0))

            in_seconds = seg.start_seconds + in_off
            out_seconds = seg.end_seconds - out_off
            if out_seconds - in_seconds < MIN_MODEL_TRIM_SECONDS:
                # trims were invalid/too aggressive; fall back to the full segment
                in_seconds = seg.start_seconds
                out_seconds = seg.end_seconds
                problems.append(f"Segment {i}: trim made the clip too short — used the full segment instead.")

            order = _coerce_int(item.get("order"))
            # Preserve any field the model's raw JSON carried that we don't
            # explicitly recognize (see the matching note in rebuild_outputs)
            # — the explicit fields below are the authoritative values and
            # always win over same-named keys in `item`.
            resolved.append(
                {
                    **item,
                    "order": order if order is not None else i,
                    "track": "main",
                    "source_id": source_id,
                    "source_name": self._display_clip_name(source_id),
                    "in_seconds": in_seconds,
                    "out_seconds": out_seconds,
                    "in_tc": seconds_to_smpte(in_seconds, self.fps, self.drop_frame),
                    "out_tc": seconds_to_smpte(out_seconds, self.fps, self.drop_frame),
                    "note": item.get("editorial_note", ""),
                    "on_screen_text": item.get("on_screen_text", ""),
                    "source_text": seg.text,
                }
            )

        resolved.sort(key=lambda s: s["order"])
        for i, s in enumerate(resolved):
            s["order"] = i
        return resolved, problems

    def _display_clip_name(self, source_id):
        """The name shown for a clip in the script and written into the XML.
        This must reflect the actual video file, never the transcript file —
        a transcript is just an editing aid, not the media being cut."""
        media_path = self.media_paths.get(source_id)
        if media_path:
            return os.path.basename(media_path)
        # No media linked yet: use the source id with a placeholder .mp4
        # extension (the most common editorial delivery container) rather
        # than the transcript's own filename/extension. The editor still
        # needs to relink real media in Premiere either way.
        return f"{source_id}.mp4"

    # ---------- project save/resume ----------

    def _build_project_dict(self, meta=None):
        """Builds the full project dict — sources, media links, fps,
        prompt/model/sequence name, target duration, current cut list,
        history, and named sequences — as a plain JSON-serializable dict.
        Shared by the dialog-based Save Project, the Ctrl/Cmd+S quick-save,
        and the background crash-recovery autosave, so all three always
        produce byte-for-byte the same shape of file."""
        meta = meta or {}

        target_seconds = None
        if meta.get("target_duration"):
            try:
                target_seconds = parse_duration_string(meta.get("target_duration"))
            except ValueError:
                target_seconds = None
        if target_seconds is None and self.last_result:
            target_seconds = self.last_result.get("target_seconds")

        return {
            "version": PROJECT_FILE_VERSION,
            "fps": self.fps,
            "drop_frame": self.drop_frame,
            "sequence_name": meta.get("sequence_name", ""),
            "prompt": meta.get("prompt", ""),
            "provider": meta.get("provider", "gemini"),
            "model": meta.get("model", GEMINI_DEFAULT_MODEL),
            "ollama_host": meta.get("ollama_host", LLAMA_DEFAULT_HOST),
            "target_seconds": target_seconds,
            "sources": [
                {
                    "source_id": source_id,
                    "path": entry["path"],
                    "media_path": self.media_paths.get(source_id),
                }
                for source_id, entry in self.sources.items()
            ],
            "narrative_summary": self.last_result.get("narrative_summary", "") if self.last_result else "",
            "resolved_segments": self.last_result.get("resolved_segments") if self.last_result else None,
            "history": self.history,
            "sequences": self.sequences,
        }

    def new_project(self):
        """Resets all in-memory session state back to a blank slate, as if
        the app had just been launched — sources, media links, the current
        cut list, history, saved sequences, fps/drop-frame, and the
        remembered project path/meta. Also discards the crash-recovery
        autosave snapshot: this is a deliberate reset, so the next launch
        shouldn't offer to restore the project that was just cleared.
        Doesn't touch the Gemini API key (memory-only or in .env) — that's
        a machine-level setting, not part of a project.

        Also serializes via _generation_lock (see __init__) since it
        mutates the same shared state as generate()/revise()/
        rebuild_outputs()."""
        if not self._generation_lock.acquire(blocking=False):
            return {
                "ok": False,
                "error": "Another generation or revision is already in progress — wait for it to finish first.",
            }
        try:
            self.sources = {}
            self.media_paths = {}
            self.last_result = None
            self._thumbnail_cache = OrderedDict()
            self.history = []
            self.sequences = {}
            self._current_project_path = None
            self._last_meta = {}
            self.fps = 25.0
            self.drop_frame = False
            self.discard_autosave()
            return {"ok": True}
        finally:
            self._generation_lock.release()

    def save_project(self, meta=None):
        """Saves everything needed to resume: sources, media links, fps,
        the last prompt/model/sequence name, target duration, the current
        cut list (if any), and the full session history — as plain local
        JSON. No transcript or video content is embedded, only file paths,
        so most of the file stays small; history is the exception, since it
        carries a full script/XML/FCPXML/OTIO snapshot per entry (capped at
        the 30 most recent, same as the in-memory limit)."""
        meta = meta or {}
        self._last_meta = meta  # remembered so autosave keeps using the current prompt/model/etc.
        project = self._build_project_dict(meta)

        if not self.window:
            return {"ok": False, "error": "The app window isn't ready yet — try again in a moment."}
        default_name = f"{self._safe_name(project['sequence_name'] or 'project')}.rcstudio.json"
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_name,
                file_types=("Rough Cut Studio project (*.json;*.rcstudio.json)", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(project, f, indent=2)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't save to {path}: {e}"}
        self._current_project_path = path
        return {"ok": True, "path": path}

    def save_project_to_path(self, path, meta=None):
        """Writes straight to an already-known path, no dialog — the
        Ctrl/Cmd+S 'quick save' once a project has been saved or loaded
        once this session. Falls back to the normal dialog flow from the
        frontend if this reports failure (e.g. the file was moved)."""
        if not path:
            return {"ok": False, "error": "No known project path yet."}
        meta = meta or {}
        self._last_meta = meta
        project = self._build_project_dict(meta)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(project, f, indent=2)
        except Exception as e:
            return {"ok": False, "error": f"Couldn't save to {path}: {e}"}
        self._current_project_path = path
        return {"ok": True, "path": path}

    def load_project(self):
        if not self.window:
            return {"ok": False, "error": "The app window isn't ready yet — try again in a moment."}
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("Rough Cut Studio project (*.json;*.rcstudio.json)", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file picker: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}

        try:
            with open(path, "r", encoding="utf-8") as f:
                project = json.load(f)
        except Exception as e:
            return {"ok": False, "error": f"Couldn't read that project file: {e}"}

        result = self._apply_loaded_project(project, history_label="Loaded from project")
        if result.get("ok"):
            self._current_project_path = path
            result["path"] = path
        return result

    def _apply_loaded_project(self, project, history_label="Loaded from project"):
        """Applies a parsed project dict to self.* state — shared by
        load_project (file picked via dialog) and restore_autosave
        (fixed recovery path, no dialog), so both go through identical
        logic and can't drift apart.

        Wrapped in a single top-level try/except: a hand-edited or
        partially-written project/autosave file can be malformed in ways
        the per-field guards below don't anticipate, and every other public
        method in this class fails gracefully with {"ok": False, "error":
        ...} rather than letting an exception escape to the caller.

        Also serializes via _generation_lock (see __init__) since it
        mutates the same shared state as generate()/revise()/
        rebuild_outputs()."""
        if not self._generation_lock.acquire(blocking=False):
            return {
                "ok": False,
                "error": "Another generation or revision is already in progress — wait for it to finish first.",
            }
        try:
            return self._apply_loaded_project_unsafe(project, history_label=history_label)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"That project file looks corrupted or unreadable: {e}"}
        finally:
            self._generation_lock.release()

    def _apply_loaded_project_unsafe(self, project, history_label="Loaded from project"):
        if not isinstance(project, dict):
            return {"ok": False, "error": "That file doesn't look like a Rough Cut Studio project."}
        if project.get("version") != PROJECT_FILE_VERSION:
            return {"ok": False, "error": "That project file was made by an incompatible version of the app."}

        self.sources = {}
        self.media_paths = {}
        self.last_result = None
        self.fps = float(project.get("fps") or 25.0)
        self.drop_frame = bool(project.get("drop_frame", False))

        try:
            raw_history = project.get("history") or []
            self.history = raw_history[-MAX_HISTORY_ENTRIES:] if isinstance(raw_history, list) else []
        except Exception:
            self.history = []

        try:
            raw_sequences = project.get("sequences") or {}
            self.sequences = raw_sequences if isinstance(raw_sequences, dict) else {}
        except Exception:
            self.sequences = {}

        source_summaries = []
        missing_files = []
        media_warnings = []
        for s in project.get("sources", []):
            if not isinstance(s, dict):
                continue
            source_id = s.get("source_id")
            src_path = s.get("path")
            if not source_id or not src_path:
                missing_files.append(src_path or source_id or "unknown")
                continue
            if not os.path.isfile(src_path):
                missing_files.append(src_path)
                continue
            if not _is_allowed_transcript_path(src_path):
                # A project file is just hand-editable JSON -- a `path`
                # with a disallowed extension is treated the same as a
                # missing file (skipped, not loaded) rather than silently
                # parsed anyway. See _is_allowed_transcript_path().
                missing_files.append(
                    f"{src_path} (unsupported file type — expected one of "
                    f"{', '.join(TRANSCRIPT_EXTENSIONS)})"
                )
                continue
            try:
                segments = parse_transcript_file(src_path, fps=self.fps)
            except Exception:
                missing_files.append(src_path)
                continue
            self.sources[source_id] = {"path": src_path, "segments": segments}
            media_path = s.get("media_path")
            if media_path:
                if _is_allowed_media_path(media_path):
                    self.media_paths[source_id] = media_path
                else:
                    # Don't fail the whole source over a bad media link --
                    # same as the "no media linked" case elsewhere in this
                    # app, just note why it wasn't linked.
                    media_warnings.append(
                        f"Media file for '{source_id}' ({media_path}) was "
                        f"skipped — not a file or not an allowed video type "
                        f"(expected one of {', '.join(VIDEO_EXTENSIONS)})."
                    )
            source_summaries.append(
                {
                    "source_id": source_id,
                    "path": src_path,
                    "segment_count": len(segments),
                    "media_path": self.media_paths.get(source_id),
                    "auto_linked": False,
                }
            )

        try:
            history_summary = self._history_summary()
        except Exception:
            self.history = []
            history_summary = []

        self._last_meta = {
            "sequence_name": project.get("sequence_name", ""),
            "prompt": project.get("prompt", ""),
            "provider": project.get("provider", "gemini"),
            "model": project.get("model", GEMINI_DEFAULT_MODEL),
            "ollama_host": project.get("ollama_host", LLAMA_DEFAULT_HOST),
            "target_duration": project.get("target_seconds"),
        }

        result = {
            "ok": True,
            "fps": self.fps,
            "drop_frame": self.drop_frame,
            "sequence_name": project.get("sequence_name", ""),
            "prompt": project.get("prompt", ""),
            "provider": project.get("provider", "gemini"),
            "model": project.get("model", GEMINI_DEFAULT_MODEL),
            "ollama_host": project.get("ollama_host", LLAMA_DEFAULT_HOST),
            "target_seconds": project.get("target_seconds"),
            "sources": source_summaries,
            "missing_files": missing_files,
            "history": history_summary,
            "sequences": self._sequence_summary(),
        }
        if media_warnings:
            result["warnings"] = media_warnings

        resolved = project.get("resolved_segments")
        if resolved:
            resolved = [seg for seg in resolved if isinstance(seg, dict) and seg.get("source_id")]
            for seg in resolved:
                # Re-derive the display name in case media links changed
                # since this project was saved.
                seg["source_name"] = self._display_clip_name(seg["source_id"])
            outputs = self._finalize_outputs(
                project.get("sequence_name") or "Loaded Sequence",
                project.get("narrative_summary", ""),
                resolved,
                target_seconds=project.get("target_seconds"),
                history_label=history_label,
            )
            result.update(outputs)  # includes a fresh "history" with the new entry appended

        return result

    # ---------- auto-save / crash recovery ----------
    #
    # A single fixed-path recovery file (not tied to any particular saved
    # project) that's silently rewritten after anything meaningful happens,
    # so a crash or accidental quit doesn't lose work that was never
    # explicitly saved. This is separate from Save Project: that writes a
    # file you name and choose the location for; this writes to one
    # well-known temp path you never interact with directly, purely so
    # there's something to offer to restore next time the app opens.

    def _autosave(self):
        """Best-effort silent snapshot to the fixed recovery path. Never
        raises — an autosave failure must never interrupt the actual
        action (generate/edit/revise/etc.) that triggered it."""
        try:
            project = self._build_project_dict(self._last_meta)
            tmp_path = self._autosave_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(project, f)
            os.replace(tmp_path, self._autosave_path)  # atomic on both POSIX and Windows
        except Exception:
            # Never raise -- see docstring -- but still leave a trace, since
            # a silent `pass` here means a real, recurring autosave bug
            # would be invisible until someone actually lost work.
            traceback.print_exc()

    def autosave_working_state(self, payload):
        """Called periodically by the frontend with whatever's currently in
        the Cuts tab — including edits that haven't been Applied yet — so a
        crash mid-edit still has something better than the last Apply to
        recover. Doesn't touch self.last_result or history; purely a
        recovery snapshot living outside the normal generate/edit flow."""
        try:
            payload = payload or {}
            meta = {
                "sequence_name": payload.get("sequence_name", ""),
                "prompt": payload.get("prompt", ""),
                "provider": payload.get("provider", "gemini"),
                "model": payload.get("model", GEMINI_DEFAULT_MODEL),
                "ollama_host": payload.get("ollama_host", LLAMA_DEFAULT_HOST),
                "target_duration": payload.get("target_duration"),
            }
            project = self._build_project_dict(meta)
            if payload.get("segments") is not None:
                project["resolved_segments"] = payload["segments"]
                project["unapplied"] = True  # flag: this reflects edits beyond the last Apply/Generate
            tmp_path = self._autosave_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(project, f)
            os.replace(tmp_path, self._autosave_path)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def check_autosave(self):
        """Called once at startup. Reports whether a recovery snapshot
        exists without loading it — restoring is a separate, explicit
        action so a stale snapshot never silently overrides a fresh
        session."""
        if not os.path.exists(self._autosave_path):
            return {"ok": True, "available": False}
        try:
            mtime = os.path.getmtime(self._autosave_path)
            with open(self._autosave_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            resolved = data.get("resolved_segments") or []
            cut_count = len([s for s in resolved if s.get("track", "main") != "broll"])
            return {
                "ok": True,
                "available": True,
                "sequence_name": data.get("sequence_name", ""),
                "saved_at": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                "cut_count": cut_count,
                "unapplied": bool(data.get("unapplied")),
            }
        except Exception:
            # Treat an unreadable/corrupted snapshot as "nothing to offer"
            # rather than surfacing an error at startup -- but still log it,
            # since silently treating a real bug the same as "no snapshot
            # yet" would make it impossible to notice.
            traceback.print_exc()
            return {"ok": True, "available": False}

    def restore_autosave(self):
        if not os.path.exists(self._autosave_path):
            return {"ok": False, "error": "No recovery snapshot found."}
        try:
            with open(self._autosave_path, "r", encoding="utf-8") as f:
                project = json.load(f)
        except Exception as e:
            return {"ok": False, "error": f"Couldn't read the recovery snapshot: {e}"}
        return self._apply_loaded_project(project, history_label="Recovered from autosave")

    def discard_autosave(self):
        try:
            if os.path.exists(self._autosave_path):
                os.remove(self._autosave_path)
        except Exception:
            traceback.print_exc()  # e.g. a permissions error -- still worth a trace
        return {"ok": True}

    # ---------- export ----------

    def save_script(self):
        if not self.last_result:
            return {"ok": False, "error": "Nothing generated yet."}
        return self._save_text(
            self.last_result["script_markdown"],
            default_name=f"{self._safe_name(self.last_result['sequence_name'])}_script.md",
            file_types=("Markdown (*.md)", "Text (*.txt)", "All files (*.*)"),
        )

    def save_xml(self):
        if not self.last_result:
            return {"ok": False, "error": "Nothing generated yet."}
        return self._save_text(
            self.last_result["xml"],
            default_name=f"{self._safe_name(self.last_result['sequence_name'])}_premiere.xml",
            file_types=("Premiere XML (*.xml)", "All files (*.*)"),
        )

    def save_fcpxml(self):
        if not self.last_result:
            return {"ok": False, "error": "Nothing generated yet."}
        return self._save_text(
            self.last_result["fcpxml"],
            default_name=f"{self._safe_name(self.last_result['sequence_name'])}.fcpxml",
            file_types=("Final Cut Pro XML (*.fcpxml)", "All files (*.*)"),
        )

    def save_otio(self):
        if not self.last_result:
            return {"ok": False, "error": "Nothing generated yet."}
        return self._save_text(
            self.last_result["otio"],
            default_name=f"{self._safe_name(self.last_result['sequence_name'])}.otio",
            file_types=("OpenTimelineIO (*.otio)", "All files (*.*)"),
        )

    def export_video_preview(self):
        """Renders the current MAIN TRACK cut list into a single .mp4 via
        ffmpeg — an actual file you can share or watch outside the app,
        unlike the in-app player. B-roll isn't composited in; see
        video_export.py's module docstring for why that's a deliberate
        scope decision, not an oversight."""
        if not self.last_result:
            return {"ok": False, "error": "Generate a script first."}
        if not ffmpeg_available():
            return {"ok": False, "error": "ffmpeg not found on this machine — video preview export needs it."}

        main_cuts = [
            s for s in self.last_result.get("resolved_segments", [])
            if s.get("track", "main") != "broll"
        ]
        if not main_cuts:
            return {"ok": False, "error": "There are no main cuts to export."}
        main_cuts.sort(key=lambda s: s["order"])

        missing = sorted({
            s["source_id"] for s in main_cuts
            if not (self.media_paths.get(s["source_id"]) and os.path.exists(self.media_paths[s["source_id"]]))
        })
        if missing:
            return {
                "ok": False,
                "error": "Link media for these sources before exporting a preview: " + ", ".join(missing),
            }

        if not self.window:
            return {"ok": False, "error": "The app window isn't ready yet — try again in a moment."}
        default_name = f"{self._safe_name(self.last_result['sequence_name'])}_preview.mp4"
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_name,
                file_types=("MP4 video (*.mp4)", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}

        clip_specs = [
            {
                "source_path": self.media_paths[s["source_id"]],
                "in_seconds": s["in_seconds"],
                "out_seconds": s["out_seconds"],
            }
            for s in main_cuts
        ]

        ok, error = build_preview_export(clip_specs, path, self.fps)
        if not ok:
            return {"ok": False, "error": error}
        return {"ok": True, "path": path, "cut_count": len(clip_specs)}

    def _save_text(self, content, default_name, file_types):
        if not self.window:
            return {"ok": False, "error": "The app window isn't ready yet — try again in a moment."}
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_name,
                file_types=file_types,
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the save dialog: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't save to {path}: {e}"}
        return {"ok": True, "path": path}

    @staticmethod
    def _safe_name(name):
        return "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip().replace(" ", "_") or "sequence"

    # ---------- API key persistence (opt-in only) ----------

    def load_saved_api_key(self):
        if os.path.exists(ENV_PATH):
            try:
                with open(ENV_PATH, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("GEMINI_API_KEY="):
                            return {"ok": True, "api_key": line.split("=", 1)[1].strip()}
            except Exception:
                pass
        env_key = os.environ.get("GEMINI_API_KEY", "")
        return {"ok": True, "api_key": env_key}

    def save_api_key_to_disk(self, api_key):
        """Only called if the user explicitly opts in from the UI."""
        try:
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write(f"GEMINI_API_KEY={api_key.strip()}\n")
            try:
                # Best-effort: restrict the key file to the current user only,
                # so it isn't world/group-readable on a shared machine. Not
                # fully meaningful on Windows (no POSIX permission bits), but
                # harmless there -- wrapped separately so a chmod failure
                # never turns a successful save into a reported error.
                os.chmod(ENV_PATH, 0o600)
            except OSError:
                pass
            return {"ok": True, "path": ENV_PATH}
        except Exception as e:
            return {"ok": False, "error": str(e)}
