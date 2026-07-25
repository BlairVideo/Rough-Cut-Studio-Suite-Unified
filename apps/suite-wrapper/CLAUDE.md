# Studio Suite — the unified wrapper app

One native `pywebview` window (no browser) that combines five workspaces
— Sync, Transcribe, B-Roll, Graphics, Edit — backed by the four sibling
apps plus Rough Cut Studio itself, none of which are modified to make
this work. See the root [`CLAUDE.md`](../CLAUDE.md) first for the
suite-wide "never touch a sibling app's files without asking" rule — it
applies here more than anywhere, since this wrapper's whole job is to
reach into those apps without changing them.

`CONTRACT.md` in this directory is the full backend/frontend contract and
addendum-by-addendum history (every feature added, every bug fixed, with
root causes) — read it before assuming something isn't documented.

## Run it

```bash
./launch_studio_suite.sh      # first run creates .venv + installs requirements
.venv/bin/python main.py --selftest   # headless: composes the page + runs the full pytest suite
```

**Gotcha (the single most common cause of "I fixed it but it's still
broken"):** pywebview loads Python **once**, at launch. A backend edit
never takes effect in an already-open window — always quit and relaunch
after touching anything under `backend/`.

## Architecture

- **`backend/suite_api.py`** composes `SuiteApi` from per-workspace
  mixins — `api_sync.py`, `api_transcriber.py`, `api_broll.py`,
  `api_brander.py`, `api_favorites.py`, `api_security.py` — with Rough
  Cut Studio's own `Api` last in the MRO, so the embedded Edit workspace
  keeps working unchanged while the other four get their own `suite_*`/
  workspace-specific methods. `backend/api_shared.py` holds constants
  shared across mixins and bootstraps RCS's backend dir onto `sys.path`.
- **Heavy work runs as background jobs** (`backend/jobs.py`), each a
  *subprocess* using that sibling app's own venv interpreter —
  `backend/workers/{sync,transcribe,broll}_worker.py`, JSON-lines over a
  dup'd stdout fd (protocol shared via `backend/workers/worker_protocol.py`).
  This is why each sibling app needs its own `.venv`: the worker
  literally runs inside it, not inside Studio Suite's own venv.
- **Blair Brander is the one exception** — no Tkinter, pure Pillow, so
  it's imported **in-process** via `backend/brander_bridge.py` rather
  than run as a subprocess. `brander_bridge.default_scene()` replicates
  Blair Brander's own `default_scene()` verbatim (keep the two in sync if
  the original ever changes) — see that module's docstring for the
  `sys.path` bootstrap order (must happen before any `multiprocessing`
  pool is created, since export.py's pool workers re-import it).
- **The composed page is rebuilt at every launch.** `main.py`'s
  `compose_page()` reads Rough Cut Studio's own `frontend/index.html`,
  pulls its `<head>` links and `<body>` (rewriting local asset hrefs to
  copies in `frontend/_generated/`), and stitches them into
  `frontend/shell.html`'s placeholders alongside `suite.css`/`suite.js`.
  Never hand-edit anything under `frontend/_generated/` — it's
  regenerated from scratch every time and any edit is silently lost.
- **Shared venv packages**: `Studio Suite/.venv-base` holds heavy ML deps
  (torch, numpy, scipy, opencv, pillow) that three sibling apps need —
  A-Sync, B-Roll Analyzer, Local Interview Transcriber. Each of *those*
  apps' own slim `.venv` (not Studio Suite's own `.venv`, which doesn't
  use this) gets a `_shared_base_venv.pth` file in its site-packages
  pointing at `.venv-base`'s site-packages, so `pip install` sees those
  packages as already satisfied without duplicating multi-GB installs
  per app. See `VENV_CONSOLIDATION_PLAN.md` for the full history,
  including the Python 3.11→3.13 migration notes.

## Sync workspace specifics

Offset sign convention (binding, `sync_xml.py`'s own contract comment:
`video_time = audio_time + offset`): a positive offset means the audio
must be delayed (starts later) to line up with the video; negative means
the audio starts before the video (its head gets trimmed instead). A
video that's both synced and transcribed gets exactly one sidecar file,
not two — offsets fold into the existing `<video>.ivt-cache.json` rather
than a separate `.sync-offsets.json` once a transcript exists for that
video.

## Testing

`pytest` (see `tests/`) covers the suite's own backend logic with fast,
mocked-dependency unit tests; a handful (`test_sync_peaks.py`, etc.) spin
up a real sibling-app subprocess and `pytest.skip` if that app's venv or
ffmpeg isn't present, rather than failing the whole suite on a missing
optional dependency. Run `.venv/bin/python main.py --selftest` to compose
the real page (the exact code path a launch depends on) and run the full
pytest suite, without opening a window.

## GUI-launch gotchas (only reproduce via a real launch, not `python main.py`)

Processes launched via Finder/Dock/`open` get a different environment
than a Terminal-launched shell — three real bugs only showed up this way:
Rosetta translation even on Apple Silicon (fixed with `arch -arm64` in
the `.app`'s launcher script), a missing Homebrew `/opt/homebrew/bin` on
`PATH` (fixed in the shared, four-times-vendored `ffprobe_util.py`), and
Homebrew Python's Tcl/Tk not being bundled (needs `python-tk` installed
separately). If something behaves differently in the real double-clicked
app than when run from a terminal, suspect the launch environment first.
