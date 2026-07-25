"""
notify.py — best-effort native macOS notifications for background job
completion. This suite has no other cross-platform ambitions (the
transcriber is already Apple Silicon-only, see its own CLAUDE.md), so
`osascript` is the whole implementation rather than an abstraction over
several backends.

Never raises: a notification is a courtesy, not a correctness
requirement, and a failure here (osascript missing, Notification Center
disabled, permission not granted) must never surface as a suite error.
"""

import subprocess


def _escape(text):
    """`display notification` takes a double-quoted AppleScript string
    literal — escape backslashes first (else a literal backslash would
    escape the following escaped quote) and double quotes."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def send_native_notification(title, message):
    script = (
        f'display notification "{_escape(message)}" with title "{_escape(title)}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
