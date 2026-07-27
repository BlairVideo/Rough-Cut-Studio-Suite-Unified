"""
paths.py — single source of truth for every directory/interpreter path the
suite touches.

Post-monorepo-migration layout: every sibling app lives under apps/, one
level up from this app's own directory (apps/suite-wrapper). Every path is
still built with os.path.join from this file's own location (never
hardcoded strings), which keeps everything working no matter where the
whole repo is moved.
"""

import os

# backend/paths.py -> backend -> apps/suite-wrapper
SUITE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT_DIR = os.path.dirname(SUITE_DIR)  # apps/
REPO_ROOT = os.path.dirname(PARENT_DIR)

# Sibling apps (read-only from the suite's point of view — never modified).
RCS_DIR = os.path.join(PARENT_DIR, "rough-cut-studio")
IVT_DIR = os.path.join(PARENT_DIR, "interview-transcriber")
BROLL_DIR = os.path.join(PARENT_DIR, "broll-analyzer")
BRANDER_DIR = os.path.join(PARENT_DIR, "blair-brander")
ASYNC_DIR = os.path.join(PARENT_DIR, "a-sync")
COLORIZE_DIR = os.path.join(PARENT_DIR, "colorize")
HARMONIZER_DIR = os.path.join(PARENT_DIR, "harmonizer")
# Renamed from HARMONIZER_PROTOTYPE_DIR ("prototype/") during the Phase 2
# migration — this is the real alignment/FCPXML engine, not scratch code.
HARMONIZER_BACKEND_DIR = os.path.join(HARMONIZER_DIR, "backend")
# Spyglass (content-aware shot search). Unlike every other sibling app
# here, Spyglass's engine is Rust, not Python — there's no subprocess
# worker script to point at. What this suite actually consumes is the
# compiled `spyglass_core` PyO3 extension (apps/spyglass/crates/spyglass-py,
# built via `maturin develop` into this venv's site-packages — see
# spyglass_bridge.py) plus the Python ML sidecar Spyglass's own engine
# shells out to for CLIP/VLM analysis (unrelated to how the engine itself
# is reached from Python — see spyglass_bridge.py's module docstring).
# SPYGLASS_APP_DATA_DIR (needs ASSETS_DIR) is defined further down, next
# to CARDEATER_DB, the closest analog.
SPYGLASS_DIR = os.path.join(PARENT_DIR, "spyglass")
SPYGLASS_SIDECAR_DIR = os.path.join(SPYGLASS_DIR, "sidecar")

RCS_BACKEND_DIR = os.path.join(RCS_DIR, "backend")
RCS_FRONTEND_DIR = os.path.join(RCS_DIR, "frontend")

# One shared `uv` workspace venv for every Python app in the suite, at the
# repo root — replaces the old per-app .venv + requirements.txt +
# .venv-base/.pth-file sharing trick (see VENV_CONSOLIDATION_PLAN.md for
# that mechanism's history; it's superseded, not extended, by this). The
# suite previously ran each heavy app's worker as a subprocess under that
# app's OWN separate interpreter specifically for dependency isolation —
# deliberately dropped in favor of one shared, uv-locked environment now
# that every shared package was already confirmed at identical pinned
# versions with zero conflicts (same doc). The subprocess-per-worker
# pattern itself (crash/memory isolation) is unchanged; only which
# interpreter each worker runs under changed.
SHARED_VENV_PYTHON = os.path.join(REPO_ROOT, ".venv", "bin", "python")
IVT_PYTHON = SHARED_VENV_PYTHON
BROLL_PYTHON = SHARED_VENV_PYTHON
ASYNC_PYTHON = SHARED_VENV_PYTHON
# Only meaningful once Phase 2 adds apps/harmonizer/backend's own deps
# (numpy, scipy, librosa, soundfile) to the shared workspace.
HARMONIZER_PYTHON = SHARED_VENV_PYTHON

# Suite-owned directories.
BACKEND_DIR = os.path.join(SUITE_DIR, "backend")
WORKERS_DIR = os.path.join(BACKEND_DIR, "workers")
TRANSCRIBE_WORKER = os.path.join(WORKERS_DIR, "transcribe_worker.py")
BROLL_WORKER = os.path.join(WORKERS_DIR, "broll_worker.py")
SYNC_WORKER = os.path.join(WORKERS_DIR, "sync_worker.py")
HARMONIZE_WORKER = os.path.join(WORKERS_DIR, "harmonize_worker.py")

FRONTEND_DIR = os.path.join(SUITE_DIR, "frontend")
GENERATED_DIR = os.path.join(FRONTEND_DIR, "_generated")

ASSETS_DIR = os.path.join(SUITE_DIR, "assets")
TRANSCRIPTS_DIR = os.path.join(ASSETS_DIR, "transcripts")
GRAPHICS_DIR = os.path.join(ASSETS_DIR, "graphics")
LOGOS_DIR = os.path.join(ASSETS_DIR, "logos")
PROXIES_DIR = os.path.join(ASSETS_DIR, "proxies")
FAVORITES_FILE = os.path.join(ASSETS_DIR, "favorites.json")
CARDEATER_DB = os.path.join(ASSETS_DIR, "cardeater.sqlite3")
# Spyglass's own SQLite index + keyframe cache + backups live under here
# (spyglass_core::Db::open joins "spyglass_index.sqlite" onto whatever
# app_data_dir it's given -- same convention as the standalone Tauri app's
# own OS-provided app-data directory, just relocated under this suite's
# own assets/ tree).
SPYGLASS_APP_DATA_DIR = os.path.join(ASSETS_DIR, "spyglass")

# Colorize workspace: JSON sidecars, matching every other workspace's
# storage convention (no shared SQLite/settings system exists in this
# suite outside CardEater's own DB). LUTS_DIR holds both the original
# imported .cube/.3dl file and its cached WebGL-preview JSON per LUT id.
COLORIZE_ASSETS_DIR = os.path.join(ASSETS_DIR, "colorize")
COLORIZE_PROJECTS_DIR = os.path.join(COLORIZE_ASSETS_DIR, "projects")
COLORIZE_PRESETS_DIR = os.path.join(COLORIZE_ASSETS_DIR, "presets")
COLORIZE_LUTS_DIR = os.path.join(COLORIZE_ASSETS_DIR, "luts")
COLORIZE_EXPORTS_TMP_DIR = os.path.join(COLORIZE_ASSETS_DIR, "tmp")

# pywebview's WKWebView/BottleServer data store (localStorage, etc.) --
# passed to webview.start(storage_path=...) so per-workspace settings
# (suite.js's saveTranscriberSettings/saveBrollSettings/saveRcsSettings,
# column widths) survive a relaunch instead of living in pywebview's
# default private/ephemeral store, which is wiped every launch.
WEBVIEW_STORAGE_DIR = os.path.join(ASSETS_DIR, "webview_storage")

# Centralized fallback for a .braw video's .ivt-cache.json/.sync-offsets.json
# sidecars (braw_bridge.py's ivt_cache_path/sync_offsets_path) -- those
# normally live next to the video, but a .braw source routinely sits on
# read-only/removable camera media where that write isn't reliable, same
# reasoning as PROXIES_DIR above.
IVT_CACHE_DIR = os.path.join(ASSETS_DIR, "ivt_cache")

# BRAW compatibility (see braw_bridge.py). Unlike RCS/IVT/BROLL/ASYNC,
# there is no sibling app here — Blackmagic's RAW SDK is a proprietary,
# non-open-source dependency, so it is never vendored into this repo.
# BRAW_TOOL_BIN is where a small compiled helper (built once against the
# SDK, following the same "own process" isolation as every other heavy
# dependency in this suite) is expected to live if/when it exists; its
# absence is a normal, gracefully-degraded state, not an error.
BRAW_TOOL_DIR = os.path.join(SUITE_DIR, "tools", "braw")
BRAW_TOOL_BIN = os.path.join(BRAW_TOOL_DIR, "braw_proxy_tool")


def ensure_suite_dirs():
    """Create the suite's writable output dirs if missing. Called at
    startup and defensively before each write — the assets folders hold
    generated handoff VTTs, exported graphics, imported custom logos, and
    cached BRAW proxies, and _generated holds the composed index.html
    rebuilt at every launch."""
    for d in (GENERATED_DIR, TRANSCRIPTS_DIR, GRAPHICS_DIR, LOGOS_DIR, PROXIES_DIR, IVT_CACHE_DIR,
              WEBVIEW_STORAGE_DIR, COLORIZE_PROJECTS_DIR, COLORIZE_PRESETS_DIR,
              COLORIZE_LUTS_DIR, COLORIZE_EXPORTS_TMP_DIR, SPYGLASS_APP_DATA_DIR):
        os.makedirs(d, exist_ok=True)
