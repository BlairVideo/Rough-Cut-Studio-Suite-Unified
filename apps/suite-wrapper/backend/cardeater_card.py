"""
cardeater_card.py — card detection/scanning for the Card Eater workspace.

Python port of Card Eater's own card_detect.rs.
"""

import os

try:
    from . import cardeater_metadata as metadata
except ImportError:  # pragma: no cover — direct script import in tests
    import cardeater_metadata as metadata

JUNK_ENTRIES = {".DS_Store", ".Trashes", ".fseventsd", ".Spotlight-V100"}

# Recognizable camera output formats -- used only as a fallback signal for
# cards that don't use the conventional DCIM/PRIVATE layout (see
# looks_like_camera_card's docstring). Not the same list as
# api_cardeater._PREVIEWABLE_EXTENSIONS, which is about in-app preview
# support, not card detection.
CAMERA_MEDIA_EXTENSIONS = {
    # video
    "mov", "mp4", "m4v", "mxf", "avi", "braw", "r3d", "ari",
    # photo / raw
    "jpg", "jpeg", "heic", "raw", "cr2", "cr3", "nef", "arw", "dng", "raf", "rw2", "orf",
    # audio recorders sometimes ingested the same way
    "wav",
}


def _is_junk(name):
    return name.startswith(".") or name in JUNK_ENTRIES


def _has_top_level_dir_named(path, name):
    try:
        with os.scandir(path) as it:
            return any(e.is_dir() and e.name.lower() == name.lower() for e in it)
    except OSError:
        return False


def has_dcim(path):
    """Whether `path` has a top-level DCIM subfolder (case-insensitive)."""
    return _has_top_level_dir_named(path, "DCIM")


def _looks_like_all_media(path, max_depth=2):
    """Shallow scan (the root plus up to `max_depth` levels of subfolders)
    confirming EVERY non-junk entry is either a recognizable camera media
    file or a subfolder that is itself all-media -- covers cards that
    don't use either conventional layout (clips sitting directly in the
    root, or under a crew-assigned folder name like "A001" rather than
    DCIM/PRIVATE) without a full recursive walk on every volume-watcher
    tick (this only runs once per newly-mounted volume, not on every poll
    -- see cardeater_volume_watcher._run_once).

    Deliberately a universal ("all entries qualify") rather than an
    existential ("some entry qualifies") check: a general-purpose external
    drive very commonly has *some* video or photo file buried in it
    somewhere (a Movies folder, an old export, a Photos library) without
    being a camera card, and that used to be enough to false-positive it
    into auto-activating as one. Requiring the whole (shallow) volume to
    be homogeneously camera media -- as a real card's contents are --
    rules those drives out while still catching non-standard card
    layouts."""
    try:
        with os.scandir(path) as it:
            entries = [e for e in it if not _is_junk(e.name)]
    except OSError:
        return False
    if not entries:
        return False
    saw_media = False
    for entry in entries:
        if entry.is_file():
            ext = os.path.splitext(entry.name)[1].lstrip(".").lower()
            if ext not in CAMERA_MEDIA_EXTENSIONS:
                return False
            saw_media = True
        elif entry.is_dir():
            if max_depth <= 0 or not _looks_like_all_media(entry.path, max_depth - 1):
                return False
            saw_media = True
        else:
            return False
    return saw_media


def looks_like_camera_card(path):
    """Whether `path` looks like a camera card: a top-level DCIM folder
    (most consumer/prosumer cameras), a top-level PRIVATE folder
    (Sony-style XDCAM/XAVC cards, which store clips under
    PRIVATE/M4ROOT/CLIP rather than DCIM), or -- for cards that use
    neither conventional layout (clips directly in the root, or under a
    differently-named folder, e.g. a crew's own "A001"/"B001" camera
    labels) -- a volume that is, within two levels of the root, made up
    entirely of recognizable camera media (see _looks_like_all_media).
    That last check is intentionally strict: it's a fallback for
    non-standard *cards*, not a general "does this volume contain any
    media" test, so it won't fire for an ordinary external drive that
    merely has some unrelated video or photo content on it somewhere."""
    return (_has_top_level_dir_named(path, "DCIM")
            or _has_top_level_dir_named(path, "PRIVATE")
            or _looks_like_all_media(path))


def walk_stats(card_path):
    """Returns (total_files, total_bytes), skipping junk entries."""
    total_files = 0
    total_bytes = 0
    for root, dirs, files in os.walk(card_path):
        dirs[:] = [d for d in dirs if not _is_junk(d)]
        for name in files:
            if _is_junk(name):
                continue
            try:
                total_bytes += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            total_files += 1
    return total_files, total_bytes


def scan_card_files(card_path):
    """Walks `card_path` and returns a list of FileEntry dicts: path,
    relative_folder, name, ext, size_bytes, created_at, created_at_source."""
    if not os.path.exists(card_path):
        raise FileNotFoundError("Card path does not exist")

    found = []
    for root, dirs, files in os.walk(card_path):
        dirs[:] = [d for d in dirs if not _is_junk(d)]
        for name in files:
            if _is_junk(name):
                continue
            path = os.path.join(root, name)
            try:
                size_bytes = os.path.getsize(path)
            except OSError:
                size_bytes = 0
            relative_folder = os.path.relpath(root, card_path)
            if relative_folder == ".":
                relative_folder = ""
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            found.append({
                "path": path,
                "relative_folder": relative_folder,
                "name": name,
                "ext": ext,
                "size_bytes": size_bytes,
            })

    resolved = metadata.resolve_created_at_batch([f["path"] for f in found])

    entries = []
    for f in found:
        created_at, created_at_source = resolved.get(f["path"], (None, "unavailable"))
        entries.append({
            **f,
            "created_at": created_at,
            "created_at_source": created_at_source,
        })
    return entries


def build_card_info(mount_path, is_dev_fallback):
    label = os.path.basename(mount_path.rstrip(os.sep)) or mount_path
    total_files, total_bytes = walk_stats(mount_path)
    return {
        "id": mount_path,
        "label": label,
        "mount_path": mount_path,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "has_dcim": has_dcim(mount_path),
        "is_dev_fallback": is_dev_fallback,
    }
