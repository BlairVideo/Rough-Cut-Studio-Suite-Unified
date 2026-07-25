# Packaging the Full-Capability Distributable with Platypus

This produces a double-clickable "B-Roll Analyzer.app" that runs the real,
unfrozen `build_env` venv -- the exact environment where
`torchvision.ops.nms` already tested clean. Nothing is frozen, so this
sidesteps the py2app + PyTorch op-registration incompatibility entirely.
Every feature, including energy detection, works out of the box for
whoever you hand this to.

Use this alongside, not instead of, the lighter py2app build -- the
py2app `.app` is smaller and simpler for people who don't need energy
detection; this one is for full capability.

## 1. Stage the distributable folder

From your project folder (the one with a working `build_env/`):

```bash
chmod +x build_full_distributable.sh
./build_full_distributable.sh
```

This creates:

```
dist_full/B-Roll Analyzer (Full)/
  ├── runtime/   (relocatable copy of build_env, torch and all)
  └── src/       (app.py, analyzer.py, vision_energy.py, etc.)
```

The script verifies `torchvision.ops.nms` resolves in the copied venv
before finishing, so you'll know immediately if something didn't copy
cleanly.

## 2. Install Platypus

Free, from the developer: https://sveinbjorn.org/platypus
(Or `brew install --cask platypus`.)

## 3. Create the app

Open Platypus and set:

| Field | Value |
|---|---|
| App Name | `B-Roll Analyzer` |
| Script Type | `bash` |
| Script Path | `launch_broll_analyzer.sh` (the one from this handoff) |
| Interface | **None** (the script only shows dialogs on error; the real UI is Tkinter's own window) |
| Icon | `AppIcon.icns` from `src/` |
| Interpreter | `/bin/bash` (default) |

Leave "Bundled Files" empty -- deliberately. The venv and source stay
*outside* the .app bundle as siblings (`runtime/` and `src/`), not
embedded inside it. This keeps them easy to inspect or swap without
regenerating the app, and avoids Gatekeeper/signing complications that
come with embedding a huge venv inside a bundle's Resources folder.

Under **Settings**, leave "Accepts dropped items," "Run in background,"
etc. at their defaults (unchecked) -- this is a plain double-click launch.

Click **Create App**, and save it directly into:

```
dist_full/B-Roll Analyzer (Full)/B-Roll Analyzer.app
```

so it ends up as a sibling of `runtime/` and `src/`, matching what
`launch_broll_analyzer.sh` expects.

## 4. Test it

Double-click `B-Roll Analyzer.app`. It should launch exactly like running
`python3 app.py` from an activated `build_env` -- including the Energy
checkbox being available immediately, no separate `pip install` needed.

If something's wrong, check the log instead of guessing:

```bash
tail -50 ~/Library/Logs/B-Roll\ Analyzer/app.log
```

## 5. Share it

Zip the whole folder (not just the `.app` -- `runtime/` and `src/` have
to travel with it):

```bash
cd dist_full
zip -r "B-Roll Analyzer (Full).zip" "B-Roll Analyzer (Full)"
```

Whoever receives it unzips it anywhere and double-clicks the `.app`
inside. No install step, no Terminal, no separate `pip install`.

**Size/distribution note:** this zip will be large (roughly 1-2 GB,
mostly torch) since it's carrying a full Python environment rather than
a stripped, frozen one. That's an inherent tradeoff of this approach --
fine for AirDrop, a shared drive, or Drive upload; not something you'd
want to email.

**Gatekeeper note:** since this isn't signed/notarized (that requires an
Apple Developer account and would need to happen through the same
process as the py2app build), first launch on another Mac will need a
right-click → Open once, same as any unsigned app. After that it opens
normally.

## Updating later

Because `runtime/` and `src/` are plain folders, not baked into the
`.app`, updating either later is simple:

- **Code changes:** just replace the files in `src/`.
- **Dependency changes:** re-run `build_full_distributable.sh` to refresh
  `runtime/` from a rebuilt `build_env`.

The `.app` itself never needs to be regenerated for either kind of
update.
