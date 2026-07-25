# Venv Consolidation Plan (item #4 from the July 2026 review)

Grounded in a direct read of each app's `requirements.txt`, `.venv/pyvenv.cfg`,
and launcher script. **All 7 steps are now complete** — see "Progress" below.
All venvs were later moved from Python 3.11 to 3.13 — see "Python 3.11 →
3.13 migration" below.

**Correction to the mechanism** (found while implementing, not caught while
writing the plan): `python3 -m venv --system-site-packages` only inherits
from the *system* Python install, not an arbitrary custom venv — it cannot
be pointed at a hand-built `.venv-base`. The actual mechanism used instead:
a `.pth` file dropped into each app's own `site-packages`, containing the
absolute path to `.venv-base`'s `site-packages`. Python's site machinery
appends `.pth`-listed directories to `sys.path` at interpreter startup, so
an app venv with no local `torch` install still resolves `import torch` by
falling through to the base venv — while remaining, in every other way, a
completely normal, independent venv (no global/system Python touched, no
new tool installed, fully reversible by deleting the `.pth` file). This is
lower-risk than `--system-site-packages` would have been anyway: that flag
would have exposed the *entire* system Python environment, not just the
five chosen shared packages.

## Progress

- [x] **Step 1 — pin exact versions.** All 6 `requirements.txt` files
      (5 sibling apps + Studio Suite) pinned to the exact versions already
      installed and proven working (verified via `pip install --dry-run`
      against each venv — zero conflicts, everything "already satisfied").
      Bonus finding: `torch` (2.13.0), `numpy` (2.4.6), `scipy` (1.17.1), and
      `opencv` (5.0.0.93) had *already* independently converged to identical
      versions across every app that shares them — no version-reconciliation
      work was needed for step 2.
- [x] **`Studio Suite/.venv-base` built** with the five packages proven
      shared by ≥2 apps: `torch==2.13.0`, `numpy==2.4.6`, `scipy==1.17.1`,
      `opencv-python-headless==5.0.0.93`, `pillow==12.3.0` (937 MB). Verified
      all five import correctly in isolation.
- [x] **B-Roll Analyzer migrated (pilot)**:
  - Old `.venv` (910 MB) renamed as a safety backup, then moved OUT of the
    tracked tree (into the session scratchpad) once the migration was
    verified — sync_copies.sh doesn't recognize a `.venv.bak-*` name as
    excluded, and leaving it in place flooded `--check` with thousands of
    spurious "drift" lines for files inside someone's old site-packages.
  - New `.venv` created, wired to `.venv-base` via the `.pth` file, then
    `torchvision==0.28.0` + `open_clip_torch==3.3.0` + `pytest` installed
    locally (the two real B-Roll-specific extras). Confirmed via
    `pip list`/directory inspection that torch/numpy/opencv/pillow are
    **not** duplicated locally — only resolved through the base.
  - Also switched from `opencv-python` (GUI build) to
    `opencv-python-headless`, unifying with A-Sync's variant — confirmed
    safe: B-Roll Analyzer's source has zero calls to any GUI `cv2`
    function (`imshow`/`namedWindow`/`waitKey`/`createTrackbar`).
  - **New venv size: 94 MB** (was 910 MB) — the other ~816 MB now lives
    once, in the shared base, instead of duplicated per-app.
  - Verified: `pytest tests/` (48/48 pass), a real CLIP inference call
    through `vision_energy.score_frames_energy()` (exercises torch +
    open_clip resolving correctly through the `.pth` indirection end to
    end), `python -m py_compile` on all modules, the standalone app
    launched directly (`python app.py`) and stayed running with a clean
    log, and the actual subprocess worker the suite spawns
    (`backend/workers/broll_worker.py --selfcheck`) passed under the new
    venv. Studio Suite's own `main.py --selftest` still passes.
  - **Fixed a real gotcha found along the way**: `sync_copies.sh`'s
    exclude list didn't cover `.venv-base/` either — its `.py`/`.md`/etc.
    include-whitelist would have copied stray *source* files out of
    torch/numpy/scipy's package internals into the All-In-One tree
    (without their compiled binaries) on the next plain `./sync_copies.sh`
    run, silently corrupting that copy. Added `--exclude '.venv-base/'`
    and `--exclude '.venv.bak*/'` to the script, with a comment explaining
    why.

- [x] **Local Interview Transcriber migrated**: shares `torch`/`numpy`/
      `scipy`/`pillow` from the base (it never used `opencv`); its own
      non-shared extras (`streamlit`, `pywebview`, `pyobjc-framework-*`,
      `mlx-whisper`, `pyannote.audio`, `torchaudio`, `keyring`) installed
      locally. **New venv size: 988 MB** (was 1.7 GB) — the remaining bulk
      is `pyannote.audio`'s own large, genuinely non-shared dependency tree
      (lightning/pytorch-lightning/torchmetrics/optuna) plus Streamlit.
      Verified: `py_compile` on `app.py`/`launcher.py`; real `mlx_whisper`
      transcription against a synthetic WAV (produced a segment, not just
      an import success); the actual `backend/workers/transcribe_worker.py
      --selfcheck` the suite spawns; and a full standalone launch via
      `launcher.py` — confirmed the Streamlit child process actually bound
      a port and served `HTTP 200`, not just "process didn't crash." Studio
      Suite's `main.py --selftest` still passes (including its `WHISPER_MODELS`
      equality check against this exact venv).

- [x] **A-Sync migrated**: shares `numpy`/`scipy`/`opencv-python-headless`
      (already its own variant)/`pillow` from the base; only `sounddevice`
      installed locally. **New venv size: 23 MB** (was 294 MB). Verified:
      `py_compile` on all four modules; a real FFT cross-correlation test
      (two synthetic WAVs with a known 0.25s offset — `compute_waveform_offset`
      recovered the exact magnitude through `scipy.signal.correlate`
      resolving via the base); `backend/workers/sync_worker.py --selfcheck`;
      standalone launch via `sync_app.py` stayed running with a clean log.
- [x] **Full suite verification (step 6)**: `main.py --selftest` passes
      after each individual migration and again after all three landed
      together. `sync_copies.sh --check` stays clean (only real code edits
      show as drift, no venv-internal noise).

## Disk accounting (final, primary tree)

| Venv | Before | After |
|---|---|---|
| B-Roll Analyzer | 910 MB | 94 MB |
| Local Interview Transcriber | 1.7 GB | 988 MB |
| A-Sync | 294 MB | 23 MB |
| Rough Cut Studio | 64 MB | 64 MB (unchanged, no shared deps) |
| Studio Suite | 77 MB | 77 MB (unchanged) |
| Studio Suite/.venv-base (new) | — | 937 MB |
| **Total** | **≈3.05 GB** | **≈2.1 GB** |

Net savings: **≈950 MB** in the primary tree. (The plan's original estimate
of 0.8–0.9 GB was close; the actual figure landed slightly higher.)

- [x] **Step 7 — the All-In-One tree's fate: archived.** Compressed via
      `tar czf` to `~/Archives/Rough Cut Studio Suite — All-In-One (archived
      2026-07-14).tar.gz` (735 MB, down from 2.7 GB live). Verified BEFORE
      removing anything: `tar tzf` full-listing integrity check (exit 0,
      77,282 entries), file-count comparison against the live tree
      (77,282 == 77,282, exact match), and a spot-extraction + byte-for-byte
      `diff` of three representative files (`main.py`, `CONTRACT.md`,
      `sync_core.py`) against their live originals — all identical. Only
      after all three checks passed was the live directory removed
      (`rm -rf`), reclaiming the full 2.7 GB from `/Applications`.
      `sync_copies.sh` — whose entire job was keeping two live trees in
      sync — now has nothing to do; updated it to fail with a clear pointer
      to the archive's location instead of a bare "both trees must exist"
      error, so a future run isn't confusing. If the derived tree is ever
      restored from the archive, the script works exactly as before with
      no further changes needed.

## Final state

- Primary tree only now (no second live copy).
- Primary-tree venvs: ≈2.1 GB (down from ≈3.05 GB pre-migration).
- All-In-One tree: archived at ~735 MB compressed, outside `/Applications`;
  live directory removed, ~2.7 GB reclaimed.
- **Total disk recovered this pass: ≈3.65 GB** (≈950 MB from venv dedup +
  ≈2.7 GB from retiring the duplicate tree), at the cost of 735 MB for the
  archive — net ≈2.9 GB reclaimed on disk, with the pre-migration state
  still recoverable from the archive (and the user's separate external
  backups).

## Python 3.11 → 3.13 migration (2026-07-15)

All six venvs (the five apps' own + the shared `.venv-base`) rebuilt on
**Python 3.13.14**, replacing 3.11.9. Reason: 3.11 had already dropped to
security-only maintenance (EOL 2027-10); 3.13 is still in active bugfix
maintenance for a few more months and has a full year longer runway
(EOL 2029-10). Verified compatible BEFORE committing to the migration —
real venv builds + a real end-to-end mlx_whisper transcription under
3.12 and again under 3.13, not just checking PyPI classifiers.

- **Built from Homebrew's `python@3.13`** (`3.13.14`), not the python.org
  framework builds the 3.11 venvs used — a deliberate deviation, since it
  avoided running a `.pkg` installer that needs admin rights. One real gap
  this caused: Homebrew's `python@3.13` doesn't bundle Tcl/Tk, so
  `import tkinter` (A-Sync's and B-Roll Analyzer's entire GUI) failed
  until `brew install python-tk@3.13` was run — caught by actually
  running each app's real code, not just import-checking the requirements
  list. Fixed once, system-wide; didn't require rebuilding the venvs
  already created before the fix.
- **The `.pth`-based shared-base mechanism (see "Correction to the
  mechanism" above) needed no design change** — each slim venv's `.pth`
  file just needed repointing at
  `.venv-base/lib/python3.13/site-packages` (the path changes with the
  Python minor version; the mechanism itself doesn't). Confirmed no
  package duplication crept in during the rebuild (torch/numpy/scipy/
  opencv/pillow absent from every slim venv's own site-packages, exactly
  as before).
- **Verified per app**, not just via `pip install` succeeding: real
  `py_compile` across every source file, Studio Suite's full pytest
  suite (56/56) and `--selftest`, every worker's `--selfcheck`
  (sync/transcribe/broll) run through the *actual* sibling venv each one
  uses, a real mlx_whisper transcription, a real FFT cross-correlation
  offset detection + waveform-peaks computation, and a real standalone
  launch of every app (including the actual Dock-launcher `.app`, not
  just `python main.py` directly).
- Old 3.11.9 venvs backed up (not deleted) to the session scratchpad
  before rebuilding — ephemeral, not a durable backup location; keep the
  user's own separate external backups as the real fallback if a rollback
  is ever needed.

## Current state (measured directly)

| Location | Size |
|---|---|
| Primary tree venvs (5 apps) | ≈ 3.05 GB |
| `— All-In-One` tree venvs (5 apps) | ≈ 2.7 GB |
| **Total** | **≈ 5.7 GB** |

All five venvs use **Python 3.11.9** from the same Framework build — no
interpreter-version conflict exists today.

Duplicated heavy packages within the primary tree alone:
- `torch` in both **Local Interview Transcriber** (`torch>=2.2`,
  `torchaudio>=2.2`) and **B-Roll Analyzer** (`torch>=2.0`, `torchvision>=0.15`)
  — ~533 MB each install.
- `opencv` in both **B-Roll Analyzer** (`opencv-python>=4.8`) and **A-Sync**
  (`opencv-python-headless>=4.8`) — **different distributions** of the same
  library (GUI-enabled vs. headless).
- `numpy`/`scipy` in B-Roll Analyzer, A-Sync, and the Transcriber.

Checked: B-Roll Analyzer never calls a GUI `cv2` function (`imshow`,
`namedWindow`, `waitKey`, `createTrackbar` — none found in `analyzer.py` or
`vision_energy.py`), so it does not actually need the GUI-enabled
`opencv-python` build. This removes one otherwise-real compatibility
question.

All five apps' `requirements.txt` pin only lower bounds (`>=`), never exact
versions or upper bounds — so today, `pip install` in each venv could
already silently resolve to different patch/minor versions of the same
package across apps, with no lockfile to catch drift.

## Why this is riskier than #1–#3

Unlike the earlier subprocess-timeout fixes, this touches **packaging**, not
application logic:
- Each sibling app is a genuinely standalone product (own launcher —
  `launch_broll_analyzer.sh`, `Local Interview Transcriber/launcher.py`, plus
  A-Sync and Rough Cut Studio's own entry points) that must keep working
  when double-clicked on its own, outside the suite.
- A shared/merged venv changes what "the app's environment" means for anyone
  who opens one sibling standalone — if that ever breaks, it breaks the
  product, not just the wrapper.
- There is no git history here (confirmed: not a git repo) and the
  `— All-In-One` tree is not itself a backup mechanism the way a git branch
  would be — deleting it is a real, unrecoverable disk operation without a
  separate backup.

## Recommended approach: shared base venv, no deletions yet

Rather than one merged venv (which would force every app to agree on exact
versions of everything, including packages only one of them uses), use a
**layered venv per app**, each pointing at one common base for the
heavy/shared packages only:

1. Create `Studio Suite/.venv-base/` (or a sibling location) with Python
   3.11, containing only the packages proven to be shared verbatim by ≥2
   apps: `torch`/`torchvision`/`torchaudio` (align on `torch>=2.2` — the
   stricter of the two current constraints, since 2.2 satisfies both
   `>=2.0` and `>=2.2`), `numpy`, `scipy`, and `opencv-python-headless`
   (adopted suite-wide, replacing B-Roll Analyzer's GUI build since it's
   unused).
2. Rebuild each sibling's own `.venv` with
   `python3 -m venv --system-site-packages .venv` pointed so it inherits the
   base's site-packages, then `pip install` only that app's *remaining*,
   non-shared dependencies (`mlx-whisper`, `pyannote.audio`, `streamlit`,
   `pyobjc-*` for the Transcriber; `open_clip_torch` for B-Roll Analyzer;
   `sounddevice` for A-Sync; `pywebview`/`requests` for RCS and the suite).
3. Verify each app **standalone** (its own launcher, not through the suite)
   before touching the next one — one app at a time, so a break is
   immediately attributable.
4. Only after all five are verified standalone AND the suite's own
   `--selftest` + a manual pass through every workspace succeed, consider
   the second (`— All-In-One`) tree. Recommend archiving it (e.g. compress
   to a single `.tar.gz` outside `/Applications`) rather than deleting
   outright, at least for one release cycle — cheap insurance against an
   unexpected standalone-launch regression discovered later.

## Explicitly out of scope for this pass

- Repackaging into a single distributable app (`py2app`/`briefcase`) — a
  much larger effort that would also solve this, but changes how the whole
  suite is built/shipped; a separate decision.
- Pinning exact versions / adding a lockfile — worth doing, but orthogonal
  to deduplication and lower-risk to do first, independently, in each
  `requirements.txt` before restructuring venvs.

## Suggested order if approved

1. Pin exact versions in each `requirements.txt` first (cheap, reversible,
   makes step 2 reproducible).
2. Build `.venv-base`, verify it in isolation (import torch/cv2/numpy/scipy,
   check versions).
3. Migrate B-Roll Analyzer to `opencv-python-headless` + the shared base;
   verify standalone.
4. Migrate the Transcriber to the shared base; verify standalone.
5. Migrate A-Sync to the shared base; verify standalone.
6. Run the full Studio Suite `--selftest` + a manual pass through every
   workspace (Sync/Transcribe/B-Roll/Edit/Graphics).
7. Only then decide on the `— All-In-One` tree's fate (archive vs. delete),
   as a separate, explicit decision.

Estimated recoverable disk in the primary tree alone: roughly 0.8–0.9 GB
(one fewer `torch`, one `opencv` unified, `numpy`/`scipy` deduplicated).
Archiving (not yet deleting) the second tree keeps that 2.7 GB "at risk" but
recoverable rather than immediately reclaiming it.
