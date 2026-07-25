"""
braw_proxy_cache.py — on-disk index of decoded BRAW proxy files.

Unlike the B-Roll Analyzer's per-FOLDER cache (one .broll_analyzer_cache
.json living inside the analyzed folder) or the transcriber's per-file
sidecar (<video>.ivt-cache.json next to the source), BRAW source files
routinely live on read-only or removable media (camera cards, archived
raw drives) where writing any file next to them — let alone a whole
transcoded proxy — isn't reliable. So proxies are centralized under this
suite's OWN assets/proxies/ dir (paths.PROXIES_DIR), and this module
keeps a single JSON index there mapping each source file's absolute path
to its cached proxy, keyed by a content-free hash of that path (never the
path itself, since it may contain characters unsafe for a filename).

Invalidation follows the same (size, mtime) fingerprint convention as
B-Roll's result_cache.py, including its epsilon-tolerant mtime comparison
(see is_current's docstring) — a source file that's been replaced or
re-exported is treated as uncached and queued for a fresh proxy; a
source file that's merely been copied/moved (same size+mtime) is not.

assets/proxies/ is also capped at a total on-disk size (addendum v49,
proxy_cache_max_bytes/enforce_cache_cap) — register_proxy calls
enforce_cache_cap after every fresh proxy, deleting the oldest cached
proxy files (oldest first, by their own mtime) until back under budget.
Nothing prunes proactively before that: the cap is enforced lazily, on
the next proxy registered, not on a timer or at launch.
"""

import os
import json
import time
import hashlib

try:
    from . import paths
except ImportError:  # pragma: no cover — direct script import in tests
    import paths

INDEX_FILENAME = "index.json"
INDEX_VERSION = 1

# Tolerance for stat().st_mtime comparisons — matches result_cache.py's
# is_entry_usable: floats round-tripped through JSON, or read back from
# filesystems that quantize timestamps slightly differently between
# stat calls, can differ in the last few bits without the file having
# actually changed.
_MTIME_TOLERANCE_SECONDS = 1e-6

# Soft on-disk budget for assets/proxies/ (addendum v49): once a freshly
# registered proxy would leave the folder over this, the oldest proxies
# (by their own file mtime — i.e. generation order, NOT the source
# .braw's mtime, which is unrelated) are deleted to make room, oldest
# first, until back under budget or nothing further is safe to evict.
# Override via STUDIO_SUITE_PROXY_CACHE_MAX_BYTES for a machine with
# more or less spare disk than this default assumes (real 6K clips
# measured ~85-90MB per proxy after the 1920px downscale — see
# braw_proxy_tool.mm — so 25GiB covers roughly a day's worth of BRAW
# footage at that rate).
_DEFAULT_PROXY_CACHE_MAX_BYTES = 25 * 1024 ** 3
_PROXY_CACHE_MAX_BYTES_ENV = "STUDIO_SUITE_PROXY_CACHE_MAX_BYTES"


def _index_path():
    return os.path.join(paths.PROXIES_DIR, INDEX_FILENAME)


def _proxy_key(source_path):
    """Stable, filesystem-safe key for a source path — content of the
    path itself never becomes part of a filename (BRAW filenames/paths
    are arbitrary user data)."""
    return hashlib.sha1(os.path.abspath(source_path).encode("utf-8")).hexdigest()


def proxy_output_path(source_path):
    """Where a freshly-generated proxy for `source_path` should be
    written. Callers write here BEFORE calling register_proxy."""
    return os.path.join(paths.PROXIES_DIR, _proxy_key(source_path) + ".mov")


def file_fingerprint(path):
    """(size, mtime) for change detection, or None if the file can't be
    stat'd (e.g. removable media was ejected)."""
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime
    except OSError:
        return None


def load_index():
    """Best-effort load: any problem (missing, corrupt, wrong version)
    just yields an empty index — every source is treated as uncached,
    exactly as if this cache didn't exist yet. Caching here is purely a
    speed optimization (skip a slow decode), never a correctness
    requirement.

    `failures` (addendum v54) is a second, independent top-level dict —
    added here so an old index.json written before it existed still
    loads fine (defaults to {}), no INDEX_VERSION bump needed for a
    purely additive schema change."""
    try:
        with open(_index_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            return {"version": INDEX_VERSION, "entries": {}, "failures": {}}
        entries = data.get("entries", {})
        failures = data.get("failures", {})
        return {"version": INDEX_VERSION,
                "entries": entries if isinstance(entries, dict) else {},
                "failures": failures if isinstance(failures, dict) else {}}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {"version": INDEX_VERSION, "entries": {}, "failures": {}}


def save_index(index):
    """Best-effort save via temp file + atomic replace (same convention
    as result_cache.py's save_cache) — a failed write only costs the
    next lookup a re-decode, never the running job's own result."""
    paths.ensure_suite_dirs()
    index_path = _index_path()
    tmp_path = index_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(index, f)
        os.replace(tmp_path, index_path)
    except OSError:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def is_current(entry, fingerprint):
    """Whether a cache entry still matches the source file's current
    (size, mtime) AND its proxy file still exists on disk (assets/
    proxies/ is suite-owned but not immune to a user manually clearing
    it out)."""
    if entry is None or fingerprint is None:
        return False
    size, mtime = fingerprint
    if entry.get("size") != size:
        return False
    cached_mtime = entry.get("mtime")
    if not isinstance(cached_mtime, (int, float)) \
            or abs(cached_mtime - mtime) > _MTIME_TOLERANCE_SECONDS:
        return False
    proxy_path = entry.get("proxy_path")
    return bool(proxy_path) and os.path.isfile(proxy_path)


def find_cached_proxy(source_path):
    """Return the cached proxy's path for `source_path` if one exists
    and is still current, else None."""
    fingerprint = file_fingerprint(source_path)
    if fingerprint is None:
        return None
    index = load_index()
    entry = index["entries"].get(_proxy_key(source_path))
    if not is_current(entry, fingerprint):
        return None
    return entry["proxy_path"]


def register_proxy(source_path, proxy_path):
    """Record a freshly-generated proxy in the index. No-op (rather than
    raising) if `source_path` can no longer be stat'd — a proxy for a
    since-vanished source is still usable this session but not worth
    persisting as a future cache hit that can never be validated."""
    fingerprint = file_fingerprint(source_path)
    if fingerprint is None:
        return
    size, mtime = fingerprint
    index = load_index()
    index["entries"][_proxy_key(source_path)] = {
        "source_path": source_path,
        "proxy_path": proxy_path,
        "size": size,
        "mtime": mtime,
    }
    save_index(index)
    enforce_cache_cap(protect_path=proxy_path)


def proxy_cache_max_bytes():
    """The configured disk budget for assets/proxies/, in bytes."""
    override = os.environ.get(_PROXY_CACHE_MAX_BYTES_ENV)
    if override:
        try:
            return max(0, int(override))
        except ValueError:
            pass  # malformed override -- fall through to the default
    return _DEFAULT_PROXY_CACHE_MAX_BYTES


def _proxy_files_by_age():
    """Every *.mov directly under PROXIES_DIR (index.json/.tmp excluded),
    oldest first by the FILE's own mtime. Scans the directory itself
    rather than trusting the index, so a proxy the index lost track of
    (e.g. left behind by a crashed/cancelled run — like the pre-existing
    orphans this suite already has on disk) still counts against the
    budget and is still evictable, not just the ones a source file can
    still be resolved for."""
    try:
        names = os.listdir(paths.PROXIES_DIR)
    except OSError:
        return []
    files = []
    for name in names:
        if not name.endswith(".mov"):
            continue
        full = os.path.join(paths.PROXIES_DIR, name)
        try:
            st = os.stat(full)
        except OSError:
            continue  # removed mid-scan by another process -- skip it
        files.append((st.st_mtime, st.st_size, full))
    files.sort(key=lambda entry: entry[0])
    return files


def enforce_cache_cap(protect_path=None):
    """Delete the oldest cached proxies until assets/proxies/ is back
    under proxy_cache_max_bytes(), or nothing is left that's safe to
    evict. `protect_path` (the proxy a caller just finished writing) is
    never evicted by its own cap-enforcement pass, even if it alone
    exceeds the budget — this is a soft cleanup pass, not a hard
    "reject new work" limit. Best-effort throughout: a file that can't
    be removed (e.g. mid-flight from another process) is skipped rather
    than raised, matching every other method in this module."""
    cap = proxy_cache_max_bytes()
    files = _proxy_files_by_age()
    total = sum(size for _, size, _ in files)
    if total <= cap:
        return

    protect_abs = os.path.abspath(protect_path) if protect_path else None
    index = None
    changed = False
    for _, size, full in files:
        if total <= cap:
            break
        if protect_abs is not None and os.path.abspath(full) == protect_abs:
            continue
        try:
            os.remove(full)
        except OSError:
            continue
        total -= size
        if index is None:
            index = load_index()
        for key, entry in list(index["entries"].items()):
            if entry.get("proxy_path") == full:
                del index["entries"][key]
                changed = True
    if changed:
        save_index(index)


def cache_usage():
    """Current on-disk usage of assets/proxies/, for a settings-panel
    display: total bytes, file count, and the configured cap. Scans the
    directory itself (same `_proxy_files_by_age` the cap-enforcement pass
    uses), so this reflects real disk usage even for proxies the index
    lost track of."""
    files = _proxy_files_by_age()
    return {
        "bytes_used": sum(size for _, size, _ in files),
        "file_count": len(files),
        "bytes_cap": proxy_cache_max_bytes(),
    }


def clear_cache():
    """Delete every cached proxy and reset the index — a user-initiated
    "free up this disk space" action, unlike enforce_cache_cap's lazy
    oldest-first eviction. Best-effort per file, matching the rest of
    this module: a file that can't be removed is skipped, not raised."""
    removed = 0
    for _, _, full in _proxy_files_by_age():
        try:
            os.remove(full)
            removed += 1
        except OSError:
            continue
    index = load_index()
    index["entries"] = {}
    save_index(index)
    return removed


def forget_proxy(source_path):
    """Drop `source_path`'s entry (if any) without deleting its proxy
    file — used when a proxy is known to be bad and a regeneration
    should be forced next time. Best-effort, matches the rest of this
    module."""
    index = load_index()
    if index["entries"].pop(_proxy_key(source_path), None) is not None:
        save_index(index)


# ---------------------------------------------------------------------------
# Addendum v54 — proxy job failure signal
# ---------------------------------------------------------------------------
# wait_for_decode_path (braw_bridge.py) runs in a DIFFERENT venv/process
# than the one generating the proxy, with no visibility into the suite's
# own JobManager — it can only poll the filesystem, so before this it had
# no way to tell "still legitimately queued" apart from "the proxy tool
# actually crashed", and would wait out its full (now effectively
# unbounded, addendum v48) timeout either way. These three functions let
# request_proxy record a definitive failure the moment one happens, so a
# waiter elsewhere can bail out immediately instead of hanging.

def record_proxy_failure(source_path, error_message):
    """Record that the most recent proxy generation attempt for
    `source_path` failed. Overwrites any previous failure for the same
    source — only the latest attempt's outcome matters."""
    index = load_index()
    index["failures"][_proxy_key(source_path)] = {
        "source_path": source_path,
        "error": str(error_message),
        "recorded_at": time.time(),
    }
    save_index(index)


def find_proxy_failure(source_path):
    """The recorded error message for `source_path`'s most recent failed
    attempt, or None if there isn't one (never failed, or cleared by a
    fresh attempt via clear_proxy_failure)."""
    index = load_index()
    entry = index["failures"].get(_proxy_key(source_path))
    return entry["error"] if entry else None


def clear_proxy_failure(source_path):
    """Drop any recorded failure for `source_path` — called right before
    a fresh generation attempt starts, so a retry isn't shadowed by a
    stale failure from a previous attempt."""
    index = load_index()
    if index["failures"].pop(_proxy_key(source_path), None) is not None:
        save_index(index)
