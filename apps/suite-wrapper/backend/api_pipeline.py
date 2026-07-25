"""
api_pipeline.py — PipelineMixin: the "Run Pipeline" cross-workspace
shortcut (Sync + Transcribe + B-Roll queued together for one folder of
freshly-copied footage, instead of visiting each workspace by hand).

Split out of suite_api.py (contract A-1), same pattern as every other
workspace mixin. Unlike those, this one starts no job of its own and
owns no state — orchestration (which stage's job to start, in what
order, with which options) lives in the frontend exactly the way the
existing Card Eater -> B-Roll hand-off (suite.js's ceSendToBroll) already
works, calling each workspace's own sync_start/transcriber_start/
broll_start in turn. All this mixin adds is the one thing the frontend
genuinely cannot do itself: listing the video files inside a folder.

Sync and Transcribe are order-independent with each other and with
B-Roll (Sync workspace specifics, CLAUDE.md) — their jobs fold into
whichever sidecar already exists rather than depending on run order —
so the frontend fires all selected stages' jobs together rather than
waiting for one to finish before starting the next.
"""

import os
import traceback

try:
    from .api_shared import *  # noqa: F401,F403 — shared constants + helpers
    from . import braw_bridge
except ImportError:  # pragma: no cover — direct script import in tests
    from api_shared import *  # noqa: F401,F403
    import braw_bridge

# Same container set the Sync/Transcribe file dialogs already accept
# (api_shared.PREVIEW_VIDEO_EXTENSIONS + .braw, Phase 3 addendum v51).
_PIPELINE_VIDEO_EXTENSIONS = tuple(PREVIEW_VIDEO_EXTENSIONS) + (braw_bridge.BRAW_EXTENSION,)


class PipelineMixin:
    def pipeline_list_videos(self, folder):
        """Every video file under `folder` (recursive — Copy-workspace
        destinations commonly nest by date/event), dotfiles/dot-dirs
        skipped, sorted for a stable, predictable order in the UI."""
        try:
            if not folder or not os.path.isdir(folder):
                return {"ok": False, "error": f"Folder not found: {folder}"}
            found = []
            for root, dirnames, filenames in os.walk(folder):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if name.startswith("."):
                        continue
                    if os.path.splitext(name)[1].lower() in _PIPELINE_VIDEO_EXTENSIONS:
                        found.append(os.path.join(root, name))
            found.sort()
            return {"ok": True, "videos": found}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}
