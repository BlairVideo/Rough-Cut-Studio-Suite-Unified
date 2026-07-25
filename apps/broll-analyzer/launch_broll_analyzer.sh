#!/bin/bash
# launch_broll_analyzer.sh
#
# This script is meant to be wrapped by Platypus into a double-clickable
# "B-Roll Analyzer.app". Unlike the py2app build, nothing here is frozen --
# it just activates the real venv and runs app.py directly, which is why
# torch / torchvision / open_clip work reliably here (we confirmed the
# identical venv works perfectly unfrozen; the failure was specific to
# py2app's static freezing of PyTorch's native op registration).
#
# Expected folder layout (this app must stay a sibling of these two
# folders -- Platypus embeds this script inside the .app bundle, but the
# venv and source are kept OUTSIDE it so they're easy to inspect, update,
# or swap without regenerating the app):
#
#   B-Roll Analyzer (Full)/
#     ├── B-Roll Analyzer.app     <- the Platypus-wrapped launcher
#     ├── runtime/                <- full build_env venv (torch and all)
#     └── src/                    <- app.py, analyzer.py, vision_energy.py,
#                                    xml_export.py, result_cache.py,
#                                    app_settings.py, AppIcon.icns

set -uo pipefail

# --- Locate the distributable's root folder (parent of this .app) -----
# This makes the whole folder relocatable/renamable -- nothing here is
# a hardcoded absolute path.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR"
while [[ "$APP_DIR" != "/" && "$APP_DIR" != *.app ]]; do
    APP_DIR="$(dirname "$APP_DIR")"
done

if [[ "$APP_DIR" == "/" ]]; then
    osascript -e 'display alert "B-Roll Analyzer" message "Could not locate the .app bundle location on disk. This launcher must be run from inside its normal distributable folder, not copied out on its own." as critical'
    exit 1
fi

DIST_ROOT="$(dirname "$APP_DIR")"
VENV_DIR="$DIST_ROOT/runtime"
SRC_DIR="$DIST_ROOT/src"
PYTHON_BIN="$VENV_DIR/bin/python3"

LOG_DIR="$HOME/Library/Logs/B-Roll Analyzer"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/app.log"

# --- Sanity checks, with a real dialog instead of silently failing ----
# Double-clicking an .app gives no Terminal to read a bare error from,
# so anything that stops the app before its own window opens needs to
# surface as an alert, not just a nonzero exit code.
if [[ ! -x "$PYTHON_BIN" ]]; then
    osascript -e "display alert \"B-Roll Analyzer\" message \"Could not find the bundled Python environment at:\n\n$VENV_DIR\n\nMake sure the 'runtime' folder is sitting right next to this app -- it looks like it wasn't copied along with it.\" as critical"
    exit 1
fi

if [[ ! -f "$SRC_DIR/app.py" ]]; then
    osascript -e "display alert \"B-Roll Analyzer\" message \"Could not find app.py at:\n\n$SRC_DIR\n\nMake sure the 'src' folder is sitting right next to this app.\" as critical"
    exit 1
fi

# --- Run it -------------------------------------------------------------
cd "$SRC_DIR"
{
    echo "----- launch $(date) -----"
} >> "$LOG_FILE"

"$PYTHON_BIN" app.py >> "$LOG_FILE" 2>&1
STATUS=$?

if [[ $STATUS -ne 0 ]]; then
    osascript -e "display alert \"B-Roll Analyzer quit unexpectedly\" message \"Exit code $STATUS. Details were written to:\n\n$LOG_FILE\" as critical"
fi

exit $STATUS
