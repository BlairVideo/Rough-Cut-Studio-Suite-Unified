"""
cardeater_volume_watcher.py — background /Volumes polling for real hardware
card detection.

Python port of Card Eater's own volume_watcher.rs. Plain polling (not
FSEvents/DiskArbitration) is a deliberate choice carried over from the
original: card-insert detection doesn't need millisecond responsiveness.

Difference from the original Tauri app: there's no frontend event bus here
(Tauri's `app.emit`), so this just maintains a thread-safe `CardRegistry`
that the API layer's `suite_cardeater_get_active_card` reads on each poll —
the frontend detects mount/unmount transitions itself by diffing the
returned card's `id` against what it last saw (same effect as the
original's "card-mounted"/"card-unmounted" events, via polling instead of
push).
"""

import os
import threading
import time

try:
    from . import cardeater_card as card_detect
except ImportError:  # pragma: no cover — direct script import in tests
    import cardeater_card as card_detect

POLL_INTERVAL_SECONDS = 1.5


class CardRegistry:
    def __init__(self):
        self.lock = threading.RLock()
        self.active = None  # CardInfo dict or None


def _list_current_volume_paths():
    try:
        boot_canonical = os.path.realpath("/")
    except OSError:
        boot_canonical = None

    try:
        names = os.listdir("/Volumes")
    except OSError:
        return set()

    paths = set()
    for name in names:
        path = os.path.join("/Volumes", name)
        try:
            canonical = os.path.realpath(path)
        except OSError:
            continue
        if boot_canonical is not None and canonical == boot_canonical:
            continue  # the boot volume itself, symlinked into /Volumes
        paths.add(path)
    return paths


def diff_volumes(previous, current):
    """Returns (appeared, disappeared) -- pure set diff, split out for
    direct testability without a live filesystem."""
    appeared = sorted(current - previous)
    disappeared = sorted(previous - current)
    return appeared, disappeared


def _run_once(registry, known):
    current = _list_current_volume_paths()
    appeared, disappeared = diff_volumes(known, current)

    # Process disappearances first: load-bearing for the same-tick "card
    # ejected, different card inserted" case, so the active card is cleared
    # before `appeared` is evaluated against it.
    for path in disappeared:
        with registry.lock:
            if registry.active and registry.active["mount_path"] == path:
                registry.active = None

    for path in appeared:
        with registry.lock:
            already_active = registry.active is not None
        if already_active:
            # A card is already being worked on; a second inserted card is
            # silently ignored for Phase 1 (no simultaneous multi-card
            # ingest yet -- see architecture doc section 3.1).
            continue
        if not card_detect.looks_like_camera_card(path):
            # Doesn't look like a card (no DCIM or PRIVATE folder) -- most
            # likely a disk image, installer volume, or network share.
            # Don't auto-activate; "Open Folder as Card" still covers
            # non-standard cards.
            continue
        try:
            info = card_detect.build_card_info(path, is_dev_fallback=False)
        except OSError:
            continue
        with registry.lock:
            registry.active = info

    return current


def run(registry):
    """Runs for the lifetime of the process on a daemon thread."""
    known = set()
    while True:
        try:
            known = _run_once(registry, known)
        except Exception:
            pass  # never let a transient FS hiccup kill the watcher thread
        time.sleep(POLL_INTERVAL_SECONDS)


def start(registry):
    thread = threading.Thread(target=run, args=(registry,), daemon=True, name="cardeater-volume-watcher")
    thread.start()
    return thread
