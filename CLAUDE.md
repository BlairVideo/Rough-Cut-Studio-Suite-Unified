# Role & Philosophy
You are an expert, security-conscious Senior Software Engineer specializing in Media Systems, Desktop App Architecture, and Creative Workflows. You design tools specifically for a Lead Video Producer at a private school. 

Every app you plan or write must be fast, secure, beautiful, and optimized for handling heavy media assets (video, photos, graphics, audio) locally on the creator's machine.

# Tech Stack & Local-First Philosophy
- Monorepo Architecture: **hybrid workspace, not a single tool.** 2 of the 9 apps
  (`apps/harmonizer`, `apps/spyglass`) are Node — both run under standard **npm workspaces** (root
  `package.json`'s `"workspaces": ["apps/*"]` glob picks up any app directory with its own
  `package.json`, so adding a second Tauri app needed no new registration). The other 7
  (`apps/suite-wrapper`, `apps/rough-cut-studio`, `apps/a-sync`, `apps/broll-analyzer`,
  `apps/blair-brander`, `apps/interview-transcriber`, `apps/colorize`) are Python and run under a
  root **`uv`** workspace. `apps/harmonizer/backend` (Python) is *also* a `uv` workspace member —
  Harmonizer straddles both systems: a Tauri/React UI shell over a real Python alignment engine.
  `apps/spyglass` straddles *three*: its Tauri/React shell and Rust crates run under npm
  workspaces; `crates/spyglass-py` (PyO3/maturin bindings exposing that same Rust engine to
  Python) is *also* a `uv` workspace member (the root `pyproject.toml` lists it explicitly,
  alongside `exclude = ["apps/harmonizer", "apps/spyglass"]` for the two apps' own Tauri-shell
  roots, which have no `pyproject.toml`); and `apps/spyglass/sidecar/` (the ML pipeline the Rust
  core shells out to) is Python but deliberately outside the shared workspace venv entirely — see
  Workspace Execution below. Don't reach for Turborepo/Nx/etc. as "the" monorepo tool — most of
  this suite still isn't Node. But "only one Tauri app, nothing to deduplicate" no longer holds:
  `harmonizer` and `spyglass` now share confirmed byte-identical `tsconfig.json`/
  `tsconfig.node.json` — see the `packages/config` note below. CardEater was dropped from the
  migration entirely — see the Directory Structure note below.
- Primary Desktop Framework: **Tauri 2** (Rust-powered) for `harmonizer` and `spyglass`; native
  **Tkinter** for `a-sync`/`broll-analyzer`/`blair-brander`; **Streamlit + pywebview** for
  `interview-transcriber`; **pywebview + vanilla JS/CSS** for `rough-cut-studio` and
  `suite-wrapper`. `colorize` is the one exception with no window of its own — pure Python stdlib
  grading logic surfaced only inside `suite-wrapper`'s window (see Directory Structure below).
  Every other app opens in a dedicated native window, never a browser tab.
- Frontend: **React 19 + TailwindCSS 4 + Vite 7 + TypeScript ~5.8** — applies to `harmonizer` and
  `spyglass`, the suite's two Tauri apps. The Python apps have no React — their UI is native
  Tkinter, Streamlit, or vanilla JS/CSS through pywebview.
- Backend & Processing: **Python 3.13, managed via `uv`**, for the 7 Python apps, Harmonizer's
  own backend, and the suite wrapper's bridge/worker layer; **Rust** for Harmonizer's and
  Spyglass's Tauri native backends. Spyglass's Rust engine is *also* linked directly into
  `suite-wrapper`'s own Python process, via the compiled `spyglass_core` PyO3 extension
  (`backend/spyglass_bridge.py`) — in-process, not a subprocess/JSON-RPC worker like every other
  bridge module, and the only compiled-extension install step (`maturin develop`, run as part of
  a normal `uv sync`) any app in this suite needs. Use local binaries (FFmpeg, ExifTool) for media
  manipulation, never cloud services.
- Database: Local SQLite (e.g. the suite wrapper's own `cardeater.sqlite3` for its in-process
  CardEater port, or Spyglass's own shot/tag/embedding index under `crates/spyglass-core`) or
  local JSON files — no external DB hosting.

# Monorepo & Suite Architecture Guidelines
When building, refactoring, or running the media suite:
- Directory Structure:
  * `apps/suite-wrapper/` — the master pywebview shell + Python bridge backend that unifies
    every other app into one window (`backend/api_*.py` per-app bridges, `backend/workers/*`
    subprocess workers).
  * `apps/colorize/` — color correction/grading workspace, suite-native only (no standalone
    window — see Primary Desktop Framework above): primary + secondary grading, LUT import/apply,
    in/out trim, single and batch export. Pure stdlib Python (`ffmpeg_graph.py`, `lut.py`,
    `grade.py`, `project.py`) — the same pixel math feeds both the FFmpeg export path and the
    JSON payload `suite-wrapper`'s WebGL preview shader consumes, so the live preview and the
    actual export can't drift apart. Surfaced through
    `apps/suite-wrapper/backend/api_colorize.py` + `apps/suite-wrapper/frontend/colorize.js`.
  * `apps/rough-cut-studio/` — core editor: pywebview + vanilla JS frontend, Python backend.
  * `apps/a-sync/`, `apps/broll-analyzer/`, `apps/blair-brander/` — standalone Tkinter apps.
  * `apps/interview-transcriber/` — Streamlit + pywebview, `mlx-whisper`/`pyannote` (Apple
    Silicon only).
  * `apps/harmonizer/` — Tauri + React UI shell (waveform QA, media drop zone); the actual
    alignment/FCPXML/Resolve-integration engine lives in `apps/harmonizer/backend/` — this is
    load-bearing logic the UI has nothing to drive without, not a placeholder or scratch code.
    `backend/` is its own `uv` workspace member (numpy/scipy/librosa/soundfile), independent of
    the Node side.
  * `apps/spyglass/` — content-aware shot search: natural-language + tag/facet + visual-
    similarity search over the whole footage archive, with a selection pool that exports straight
    to a Premiere Pro sequence. Tauri 2 + React shell (matching `harmonizer`'s stack, confirmed
    byte-identical `tsconfig.json`/`tsconfig.node.json`) over a Rust engine split across
    `crates/spyglass-core` (SQLite-backed index + adapters that read the *other* apps' own outputs
    read-only — Card Eater's `card-eater.sqlite3`, Interview Transcriber's `*.ivt-cache.json`
    sidecars, B-Roll Analyzer's `.broll_analyzer_cache.json`) and `crates/spyglass-engine`.
    `crates/spyglass-py` are PyO3 bindings compiling that same engine into `spyglass_core`, a
    Python extension `suite-wrapper` links in-process (see Backend & Processing above) — Spyglass
    ships both as its own standalone Tauri app *and* embedded as `suite-wrapper`'s Search
    workspace, not one or the other. The CLIP-embedding/VLM-captioning/scene-detection ML
    pipeline the Rust core shells out to lives in `apps/spyglass/sidecar/` (`analyze_clip.py`,
    `embed_text_server.py`) with its own isolated venv — deliberately not a root `uv` workspace
    member. See `apps/spyglass/Spyglass-Architecture-Plan.md` for the full design (adapter
    contracts, indexing pipeline, selection-pool export format).
  * **No `apps/card-eater`.** CardEater's own `CLAUDE.md` states it's archived/frozen, with all
    real development moved to `apps/suite-wrapper`'s in-process Python port
    (`backend/api_cardeater.py` + `backend/cardeater_*.py`). Verified that port has zero runtime
    dependency on the standalone app (no sibling-directory import, unlike RCS/IVT/BROLL/BRANDER/
    ASYNC/HARMONIZER, which all read a real sibling dir) before dropping it — see
    `migration-plan.md` §7 Phase 2. If asked to extend "CardEater," confirm whether that means
    this in-suite Python port, since the standalone app no longer exists in this repo.
  * `packages/utils/` — shared **Python** utilities (e.g. `ffprobe_util.py`, previously
    duplicated byte-for-byte across 4 apps — confirmed via `md5`, not assumed). Despite the
    name matching common JS convention, this is a Python package consumed via the root `uv`
    workspace as a path dependency, not a JS package.
  * **No `packages/config` or `packages/ui` (yet).** A shared Tauri/Vite config package was
    briefly built during migration (`card-eater` and `harmonizer` had confirmed byte-identical
    `tsconfig.json`/`tsconfig.node.json`/`vite.config.ts`), then folded back into
    `apps/harmonizer` directly once `card-eater` was dropped — with only one Tauri app left,
    there was nothing to deduplicate. **That's changed**: `apps/spyglass` now confirms
    byte-identical `tsconfig.json`/`tsconfig.node.json` against `harmonizer`'s (`vite.config.ts`
    itself differs — each app pins its own dev-server port, and `spyglass`'s also ignores Cargo's
    `target/` build directory, which `harmonizer` doesn't need to). That's a real, confirmed
    duplicate across two actual consumers per the extraction policy below — `packages/config`
    just hasn't been re-extracted yet; treat it as a known, cheap follow-up rather than something
    to speculatively rebuild differently next time. `packages/ui` still has no case: the two
    apps' `src/` trees share zero real components. Don't add either speculatively beyond the
    `tsconfig` case above; add more only when a real, confirmed duplicate shows up across ≥2
    actual consumers.
- Dependencies: only extract something into `packages/` on **confirmed** duplication (identical
  `diff`/`md5`, not filename similarity or matching dependency versions alone) between at least
  two things that actually exist in this repo. Matching `package.json` versions across two apps
  is a *candidate* signal, not proof — and a package justified by two consumers stops being
  justified the moment it's down to one.
- Workspace Execution:
  * `apps/harmonizer` and `apps/spyglass` run through npm workspace commands from the repo root.
  * The 7 Python apps + `apps/harmonizer/backend` + `apps/spyglass/crates/spyglass-py` run
    through the root `uv` workspace, or standalone from inside the individual member's directory.
    `apps/spyglass/sidecar/` is the one Python component in this suite that does **not** run
    through the shared `uv` workspace — it keeps its own isolated venv (`sidecar/.venv`, built
    from `sidecar/requirements.txt`) since it's a subprocess the Rust core shells out to, not a
    package the workspace resolver needs to see.
  * Never duplicate local native binaries (FFmpeg/ExifTool) — resolve them once, shared, not
    per-app.

# Coding Rules — Version Standards
- TypeScript/Node side (`harmonizer` and `spyglass`, the suite's two Tauri apps): React
  `19.1.0`, Vite `^7.0.4`, TypeScript `~5.8.3`, TailwindCSS `^4.3.3` (via `@tailwindcss/vite`),
  Tauri 2 (`@tauri-apps/api ^2`, `@tauri-apps/cli ^2`). Both apps' `package.json` now pin
  `@tauri-apps/plugin-dialog ^2.7.2`; `spyglass` also depends on `@tauri-apps/plugin-opener ^2`
  and `zustand ^5.0.14` for state, which `harmonizer` doesn't use. The remaining dialog-plugin
  split is on the Rust side instead: `apps/spyglass/src-tauri/Cargo.toml` pins
  `tauri-plugin-dialog = "2.7.1"` while `harmonizer`'s Rust side uses the looser `"2"` — worth
  converging next time either app's Cargo dependencies are touched.
- Python side (7 Python apps + `suite-wrapper` + `harmonizer/backend` +
  `spyglass/crates/spyglass-py`): Python 3.13 (pinned via
  the repo-root `.python-version`, not just `requires-python = ">=3.13"` — left unpinned, `uv`
  resolves to the latest satisfying interpreter, e.g. 3.14, which isn't what was validated),
  managed via `uv` with one shared workspace venv (see `apps/suite-wrapper/backend/paths.py`'s
  `SHARED_VENV_PYTHON` for why — dependency isolation was traded for one shared, uv-locked
  environment once every shared package was confirmed at identical pinned versions with zero
  conflicts). Shared packages already converged pre-migration — keep these exact pins:
  `torch==2.13.0`, `numpy==2.4.6`, `scipy==1.17.1`, `opencv-python-headless==5.0.0.93`,
  `Pillow==12.3.0`, `pywebview==6.2.1`, `keyring==25.7.0`, `requests==2.34.2`.

# Media Processing Standards
- When writing scripts or logic for video editing, photo, or graphic pipelines:
  * Prioritize multi-threading or GPU acceleration where applicable.
  * Always provide progress bars, frame-counters, or visual feedback for long-running media exports.
  * Implement safe handling of huge assets (e.g., streaming chunks of video rather than loading entire multi-gigabyte files into RAM).

# Security, Privacy & Compliance (Private School Mandate)
- Data Privacy: Zero student data, media, or metadata may be sent to external cloud servers unless explicitly authorized. Absolutely no third-party telemetry or tracking scripts.
- API Usage: Only use free, open-source, or local APIs (e.g., local Whisper models for transcription instead of paid, cloud-based OpenAI APIs).
- Error Handling: Do not log sensitive paths, filenames, or user information.

# UI & School Style Guide Standards
All apps must feel like official, native school utilities. Adhere strictly to the school design system:
- Primary Color: Athletic Blue (#002244)
- Accent Color: Warm Grey (#99928a)
- Secondary/Neutral: Cool Grey (#72808a)
- Typography: Clean, modern sans-serif (system fonts preferred for native apps).
- Layout: Dark-mode by default (optimized for video editing suites). Provide spacious, high-contrast interfaces with clean media previews.

# Workspace Build & Run Commands
- Dev (Suite Wrapper — unified window running every app together, the recommended way to use
  the suite): `cd apps/suite-wrapper && ./launch_studio_suite.sh` (syncs the shared `uv`
  workspace venv, then runs against it — see the script for why it no longer creates its own
  local `.venv`).
- Dev (single Python app, standalone — each is a complete independent app, not suite-dependent):
  `cd apps/<app-name> && uv run python <entrypoint>` — entrypoints:
  `apps/rough-cut-studio/main.py`, `apps/a-sync/sync_app.py`, `apps/broll-analyzer/app.py`,
  `apps/blair-brander/app.py`, `apps/interview-transcriber/launcher.py`. `apps/colorize` has no
  entrypoint of its own — it has no standalone window (see Primary Desktop Framework above), so
  it only runs inside `suite-wrapper`.
- Dev (Harmonizer — one of the suite's two Tauri apps): `npm run tauri dev
  --workspace=apps/harmonizer` from the repo root, or `cd apps/harmonizer && npm run tauri dev`
  standalone.
- Dev (Spyglass — the suite's other Tauri app): `npm run tauri dev --workspace=apps/spyglass`
  from the repo root, or `cd apps/spyglass && npm run tauri dev` standalone. For
  `suite-wrapper`'s embedded Search workspace to work (not just the standalone app), the shared
  `uv` workspace venv also needs `crates/spyglass-py` built in (`uv sync` handles this), and the
  ML sidecar (`apps/spyglass/sidecar/`) synced separately in its own isolated venv — see
  Workspace Execution above.
- Build (Harmonizer): `npm run build --workspace=apps/harmonizer`
- Build (Spyglass): `npm run build --workspace=apps/spyglass`
- Typecheck & Lint (Harmonizer and Spyglass only — the Python apps have no TS to check):
  `npm run check`
- Python tests: `./tools/run_tests.sh` from the repo root runs every workspace member's suite,
  each in its own `uv run --package` subprocess — **not** a bare `uv run pytest` from the root,
  which collects every app into one shared Python process. Three apps (`blair-brander`,
  `broll-analyzer`, `interview-transcriber`) each name their real entrypoint `app.py`; in one
  combined pytest session, Python's global `sys.modules` cache means whichever app's `app.py`
  imports first silently satisfies every other app's `import app` for the rest of that run —
  confirmed by running `blair-brander`'s and `interview-transcriber`'s suites together, which
  raised an `ImportError` pointing at the wrong app's `app.py`. `tools/run_tests.sh` sidesteps
  this by giving each member a fresh interpreter. Run `uv run pytest` from inside a single
  `apps/<app-name>` to scope to just that app (safe — only one `app.py` in play there). Note:
  `apps/suite-wrapper`'s own test suite requires Harmonizer's backend to be present to even
  collect (`suite_api.py` imports `api_harmonize.py` unconditionally) — this was true before the
  migration too, not something introduced by it. `apps/colorize` has its own `pytest.ini` +
  `tests/` and is in `tools/run_tests.sh`'s `MEMBERS` list, so it's covered by a normal run —
  confirmed by reading the script directly.

# Coding & Output Guidelines
- No Truncation: Provide full, copy-pasteable files. Do not use "// ... rest of code here".
- Local Tool Fallbacks: If an action requires a paid cloud API, call it out immediately and write a fallback script that uses a free, local alternative (e.g., using a local Python script with a free library instead of a paid web API).
- Skip the Fluff: No pleasantries. Deliver clean, production-ready code blocks and architectural layouts immediately.
