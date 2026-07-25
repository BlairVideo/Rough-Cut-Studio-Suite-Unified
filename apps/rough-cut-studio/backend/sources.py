"""
sources.py

Owns the transcript/source-management state that used to live directly on
`Api` in api.py: parsed transcript sources, linked media file paths, the
project's fps/drop-frame settings, and the storyboard thumbnail cache.
Extracted because this slice of state has little coupling to the
LLM-generation pipeline (`generate`/`revise`/`_resolve_segments`/
`_finalize_outputs`, etc.), which continues to live in `Api`.

`Api` still exposes `sources`/`media_paths`/`fps`/`drop_frame`/
`_thumbnail_cache` as plain attributes via `@property` proxies (see api.py,
right after `__init__`) that read/write the `SourceManager` instance held
as `self._sources_mgr`. That is what lets every other place in api.py that
reads/writes those names directly (`generate()`, `_resolve_segments()`,
`_finalize_outputs()`, `_apply_loaded_project_unsafe()`,
`_build_project_dict()`, `new_project()`, ...) keep working completely
unchanged.

`SourceManager` takes a reference to the owning `Api` instance (`api`)
rather than a `window` argument, because `main.py` assigns `api.window`
only *after* `webview.create_window(...)` returns -- well after `Api()`
(and therefore `SourceManager()`) has already been constructed. Taking a
frozen `window` argument at construction time would capture `None`
forever; instead `SourceManager.window` is a property that reads
`self._api.window` lazily, by which time main.py has set it. The same
applies to `preview_server`, which lives on `Api` too.
"""

import os
import traceback
from collections import OrderedDict

import webview

from transcript_parser import (
    parse_transcript,
    parse_transcript_file,
    seconds_to_smpte,
    detect_linked_media,
    is_drop_frame_capable,
    VIDEO_EXTENSIONS,
)
from thumbnails import extract_thumbnail_data_uri, ffmpeg_available

# Cap on the in-memory storyboard thumbnail cache -- without one, a long
# scrubbing/reordering session on a large project keeps every extracted
# frame in memory for the life of the process. 500 entries is generous for
# a single session's worth of Cuts-tab thumbnails while keeping the cache's
# memory footprint bounded.
MAX_THUMBNAIL_CACHE_ENTRIES = 500


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


class SourceManager:
    """Owns `sources` / `media_paths` / `fps` / `drop_frame` /
    `_thumbnail_cache` on behalf of `Api`. See module docstring for why it
    holds a reference to the owning `Api` instance instead of a frozen
    `window` argument."""

    def __init__(self, api):
        self._api = api
        self.sources = {}       # source_id -> {"path": str, "segments": [Segment,...]}
        self.media_paths = {}   # source_id -> linked media file path (for XML pathurl)
        self.fps = 25.0
        self.drop_frame = False  # only meaningful at 29.97/59.94; ignored otherwise
        self._thumbnail_cache = OrderedDict()  # (media_path, rounded_seconds) -> data URI; LRU-capped, see MAX_THUMBNAIL_CACHE_ENTRIES

    @property
    def window(self):
        # Not available at __init__ time -- main.py assigns this on the
        # owning Api instance right after webview.create_window(...).
        return self._api.window

    @property
    def preview_server(self):
        return self._api.preview_server

    # ---------- transcript management ----------

    def pick_transcript_files(self):
        if not self.window:
            return {"ok": False, "error": "The app window isn't ready yet — try again in a moment."}
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=("Transcripts (*.srt;*.vtt;*.txt)", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file picker: {e}"}
        paths = _all_paths(result)
        if not paths:
            return {"ok": False, "cancelled": True}
        results = [self._add_transcript(path) for path in paths]
        return {"ok": True, "sources": results}

    def _add_transcript(self, path):
        source_id = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            segments = parse_transcript(content, fps=self.fps)
        except Exception as e:
            return {"source_id": source_id, "path": path, "error": str(e), "segment_count": 0}
        self.sources[source_id] = {"path": path, "segments": segments}

        auto_linked = False
        if source_id not in self.media_paths:
            found = detect_linked_media(path, content)
            if found:
                self.media_paths[source_id] = found
                auto_linked = True

        return {
            "source_id": source_id,
            "path": path,
            "segment_count": len(segments),
            "preview": [s.to_dict() for s in segments[:5]],
            "duration_tc": segments[-1].end_tc if segments else "00:00:00:00",
            "media_path": self.media_paths.get(source_id),
            "auto_linked": auto_linked,
        }

    def remove_source(self, source_id):
        self.sources.pop(source_id, None)
        media_path = self.media_paths.pop(source_id, None)
        if media_path:
            self.preview_server.forget(media_path)
        return {"ok": True}

    def set_fps(self, fps):
        try:
            self.fps = float(fps)
        except (TypeError, ValueError):
            self.fps = 25.0
        # Re-parse existing sources at the new frame rate. Wrapped per-source
        # (like _add_transcript) rather than left to raise -- a transcript
        # file that was moved/deleted since being added shouldn't take down
        # the whole fps change; it's reported back as a warning instead.
        problems = []
        for source_id, entry in list(self.sources.items()):
            try:
                entry["segments"] = parse_transcript_file(entry["path"], fps=self.fps)
            except Exception as e:
                problems.append(f"Couldn't re-parse '{source_id}' at the new frame rate: {e}")
        result = {
            "ok": True,
            "fps": self.fps,
            "drop_frame_available": is_drop_frame_capable(self.fps),
        }
        if problems:
            result["warnings"] = problems
        return result

    def set_drop_frame(self, enabled):
        self.drop_frame = bool(enabled)
        return {
            "ok": True,
            "drop_frame": self.drop_frame,
            "effective": self.drop_frame and is_drop_frame_capable(self.fps),
        }

    def format_timecode(self, seconds):
        """Formats a raw seconds value (e.g. the preview player's current
        playhead position) as a timecode using the project's current fps
        and drop-frame setting — so 'Set In/Out from Playhead' produces the
        exact same format as every other timecode in the app. Read-only,
        no side effects."""
        try:
            seconds = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid time value."}
        return {"ok": True, "tc": seconds_to_smpte(seconds, self.fps, self.drop_frame), "seconds": seconds}

    def link_media_file(self, source_id):
        if not self.window:
            return {"ok": False, "error": "The app window isn't ready yet — try again in a moment."}
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("Video files (*.mp4;*.mov;*.mxf;*.avi;*.mkv;*.m4v;*.braw)", "All files (*.*)"),
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the file picker: {e}"}
        path = _first_path(result)
        if not path:
            return {"ok": False, "cancelled": True}
        self.media_paths[source_id] = path
        return {"ok": True, "source_id": source_id, "media_path": path}

    def batch_relink_media(self):
        """Points at a folder once and auto-matches every currently-unlinked
        source by filename (same-stem, any known video extension) —
        recursively, so it works whether the footage is all in one folder
        or organized into subfolders. Only fills in sources that don't
        already have a valid link; never overwrites an existing one."""
        unlinked = [
            source_id for source_id in self.sources
            if not (self.media_paths.get(source_id) and os.path.exists(self.media_paths[source_id]))
        ]
        if not unlinked:
            return {"ok": True, "linked_count": 0, "message": "Every source is already linked.", "sources": self.list_sources()}

        if not self.window:
            return {"ok": False, "error": "The app window isn't ready yet — try again in a moment."}

        try:
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't open the folder picker: {e}"}
        folder = _first_path(result)
        if not folder:
            return {"ok": False, "cancelled": True}

        # Build a stem -> path map for every video file under the folder.
        # Bounded so a huge or deeply nested drive can't hang the app.
        stem_map = {}
        files_scanned = 0
        MAX_FILES_SCANNED = 20000
        for root, _dirs, files in os.walk(folder):
            for fname in files:
                files_scanned += 1
                if files_scanned > MAX_FILES_SCANNED:
                    break
                stem, ext = os.path.splitext(fname)
                if ext.lower() in VIDEO_EXTENSIONS and stem not in stem_map:
                    stem_map[stem] = os.path.join(root, fname)
            if files_scanned > MAX_FILES_SCANNED:
                break

        linked_count = 0
        still_unlinked = []
        for source_id in unlinked:
            match = stem_map.get(source_id)
            if match:
                self.media_paths[source_id] = match
                linked_count += 1
            else:
                still_unlinked.append(source_id)

        return {
            "ok": True,
            "linked_count": linked_count,
            "still_unlinked": still_unlinked,
            "folder": folder,
            "sources": self.list_sources(),
        }

    def list_sources(self):
        out = []
        for source_id, entry in self.sources.items():
            out.append(
                {
                    "source_id": source_id,
                    "path": entry["path"],
                    "segment_count": len(entry["segments"]),
                    "media_path": self.media_paths.get(source_id),
                }
            )
        return out

    def get_transcript_view(self, source_id):
        """Returns every parsed segment for a source, for the transcript
        viewer — read-only, just reflects what's already been parsed from
        disk. Useful for confirming a transcript looks right (or spotting
        a parsing mismatch) without leaving the app."""
        entry = self.sources.get(source_id)
        if not entry:
            return {"ok": False, "error": "Unknown source."}
        segments = [s.to_dict() for s in entry["segments"]]
        return {
            "ok": True,
            "source_id": source_id,
            "path": entry["path"],
            "media_path": self.media_paths.get(source_id),
            "segment_count": len(segments),
            "segments": segments,
        }

    # ---------- storyboard thumbnails ----------

    def get_thumbnail(self, source_id, in_seconds):
        """Returns a small base64 data URI for the frame at `in_seconds`
        into the source's linked media file, for the Cuts tab storyboard.
        Cached per (file, ~1/4-second bucket) so re-rendering the table
        after a reorder doesn't re-extract frames that were already fetched.
        Never raises: missing ffmpeg, missing media, or a bad timestamp all
        just come back as ok: False with a message, not an exception."""
        if not ffmpeg_available():
            return {"ok": False, "error": "ffmpeg not found on this machine — thumbnails need it."}

        media_path = self.media_paths.get(source_id)
        if not media_path:
            return {"ok": False, "error": "No media linked for this source yet."}
        if not os.path.exists(media_path):
            return {"ok": False, "error": "Linked media file not found on disk."}

        try:
            in_seconds = float(in_seconds)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid timestamp."}

        cache_key = (media_path, round(in_seconds * 4))  # ~250ms buckets
        cached = self._thumbnail_cache.get(cache_key)
        if cached:
            self._thumbnail_cache.move_to_end(cache_key)  # mark as most-recently-used
            return {"ok": True, "data_uri": cached}

        data_uri = extract_thumbnail_data_uri(media_path, in_seconds)
        if not data_uri:
            return {"ok": False, "error": "Couldn't extract a frame from that file/timestamp."}

        self._thumbnail_cache[cache_key] = data_uri
        while len(self._thumbnail_cache) > MAX_THUMBNAIL_CACHE_ENTRIES:
            self._thumbnail_cache.popitem(last=False)  # evict least-recently-used
        return {"ok": True, "data_uri": data_uri}

    # ---------- video preview ----------

    def get_preview_url(self, source_id):
        """Returns a local (127.0.0.1-only) URL the Cuts tab's <video>
        player can load for this source, starting a small background HTTP
        server on first use. Only ever serves files already linked as
        source media — never an arbitrary path."""
        media_path = self.media_paths.get(source_id)
        if not media_path:
            return {"ok": False, "error": "No media linked for this source yet."}
        if not os.path.exists(media_path):
            return {"ok": False, "error": "Linked media file not found on disk."}
        try:
            url = self.preview_server.url_for(media_path)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"Couldn't start the local preview server: {e}"}
        return {"ok": True, "url": url}
