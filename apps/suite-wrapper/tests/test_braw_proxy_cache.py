"""braw_proxy_cache.py -- fingerprint-keyed index of cached BRAW proxies.
Mirrors B-Roll Analyzer's result_cache.py test style: pure filesystem/JSON
logic, no real BRAW file or SDK involved (a plain stand-in .braw file with
arbitrary bytes is enough, since only its size/mtime ever matter here)."""

import os
import time

from backend import paths, braw_proxy_cache


def _redirect_proxies_dir(monkeypatch, tmp_path):
    proxies_dir = tmp_path / "proxies"
    monkeypatch.setattr(paths, "PROXIES_DIR", str(proxies_dir))


def make_source(tmp_path, name="clip.braw", data=b"\x00" * 32):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_find_cached_proxy_returns_none_when_nothing_cached(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    source = make_source(tmp_path)
    assert braw_proxy_cache.find_cached_proxy(source) is None


def test_register_then_find_returns_the_proxy_path(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    source = make_source(tmp_path)
    proxy_path = braw_proxy_cache.proxy_output_path(source)
    os.makedirs(os.path.dirname(proxy_path), exist_ok=True)
    with open(proxy_path, "wb") as f:
        f.write(b"fake proxy bytes")

    braw_proxy_cache.register_proxy(source, proxy_path)

    assert braw_proxy_cache.find_cached_proxy(source) == proxy_path


def test_stale_size_invalidates_the_cache_entry(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    source = make_source(tmp_path)
    proxy_path = braw_proxy_cache.proxy_output_path(source)
    os.makedirs(os.path.dirname(proxy_path), exist_ok=True)
    open(proxy_path, "wb").write(b"fake")
    braw_proxy_cache.register_proxy(source, proxy_path)

    # Source re-exported at a different size -- must be treated as uncached.
    with open(source, "wb") as f:
        f.write(b"\x00" * 999)

    assert braw_proxy_cache.find_cached_proxy(source) is None


def test_stale_mtime_invalidates_the_cache_entry(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    source = make_source(tmp_path)
    proxy_path = braw_proxy_cache.proxy_output_path(source)
    os.makedirs(os.path.dirname(proxy_path), exist_ok=True)
    open(proxy_path, "wb").write(b"fake")
    braw_proxy_cache.register_proxy(source, proxy_path)

    # Same size, but a materially different mtime (re-written in place) --
    # must be treated as uncached, unlike the sub-microsecond float noise
    # the tolerance in is_current exists to absorb.
    new_mtime = os.stat(source).st_mtime + 5.0
    os.utime(source, (new_mtime, new_mtime))

    assert braw_proxy_cache.find_cached_proxy(source) is None


def test_tiny_mtime_float_noise_is_still_a_cache_hit(monkeypatch, tmp_path):
    """Same intent as result_cache.py's is_entry_usable tolerance test:
    floats round-tripped through JSON, or re-stat'd on filesystems that
    quantize timestamps slightly differently, must not cause a phantom
    cache miss."""
    _redirect_proxies_dir(monkeypatch, tmp_path)
    source = make_source(tmp_path)
    proxy_path = braw_proxy_cache.proxy_output_path(source)
    os.makedirs(os.path.dirname(proxy_path), exist_ok=True)
    open(proxy_path, "wb").write(b"fake")
    braw_proxy_cache.register_proxy(source, proxy_path)

    index = braw_proxy_cache.load_index()
    key = braw_proxy_cache._proxy_key(source)
    index["entries"][key]["mtime"] += 1e-9  # far below the 1e-6s tolerance
    braw_proxy_cache.save_index(index)

    assert braw_proxy_cache.find_cached_proxy(source) == proxy_path


def test_missing_proxy_file_invalidates_even_with_matching_fingerprint(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    source = make_source(tmp_path)
    proxy_path = braw_proxy_cache.proxy_output_path(source)
    os.makedirs(os.path.dirname(proxy_path), exist_ok=True)
    open(proxy_path, "wb").write(b"fake")
    braw_proxy_cache.register_proxy(source, proxy_path)

    os.remove(proxy_path)  # e.g. user manually cleared assets/proxies/

    assert braw_proxy_cache.find_cached_proxy(source) is None


def test_forget_proxy_drops_the_entry_without_touching_the_file(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    source = make_source(tmp_path)
    proxy_path = braw_proxy_cache.proxy_output_path(source)
    os.makedirs(os.path.dirname(proxy_path), exist_ok=True)
    open(proxy_path, "wb").write(b"fake")
    braw_proxy_cache.register_proxy(source, proxy_path)

    braw_proxy_cache.forget_proxy(source)

    assert braw_proxy_cache.find_cached_proxy(source) is None
    assert os.path.isfile(proxy_path)  # the file itself is left alone


def test_load_index_is_best_effort_against_a_corrupt_file(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    os.makedirs(str(tmp_path / "proxies"), exist_ok=True)
    with open(braw_proxy_cache._index_path(), "w", encoding="utf-8") as f:
        f.write("{not valid json")

    index = braw_proxy_cache.load_index()
    assert index == {"version": braw_proxy_cache.INDEX_VERSION, "entries": {}, "failures": {}}


def test_two_different_source_paths_never_collide(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    a = make_source(tmp_path, "a.braw")
    b = make_source(tmp_path, "b.braw")
    assert braw_proxy_cache.proxy_output_path(a) != braw_proxy_cache.proxy_output_path(b)


def _register(tmp_path, monkeypatch, name, nbytes, age_offset=0.0):
    """Make a source + a same-named proxy of `nbytes`, register it, and
    backdate the PROXY FILE's own mtime by `age_offset` seconds (older =
    larger offset) so cap-enforcement's age ordering is deterministic
    without relying on real wall-clock gaps between fast test calls."""
    source = make_source(tmp_path, name, data=b"\x00" * 8)
    proxy_path = braw_proxy_cache.proxy_output_path(source)
    os.makedirs(os.path.dirname(proxy_path), exist_ok=True)
    with open(proxy_path, "wb") as f:
        f.write(b"\x00" * nbytes)
    if age_offset:
        t = time.time() - age_offset
        os.utime(proxy_path, (t, t))
    braw_proxy_cache.register_proxy(source, proxy_path)
    return source, proxy_path


def test_register_proxy_evicts_oldest_when_over_cap(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("STUDIO_SUITE_PROXY_CACHE_MAX_BYTES", "1000")

    _, oldest_proxy = _register(tmp_path, monkeypatch, "old.braw", 400, age_offset=100.0)
    _, middle_proxy = _register(tmp_path, monkeypatch, "mid.braw", 400, age_offset=50.0)
    # Registering this one pushes the folder to 1200 bytes, over the
    # 1000-byte cap -- the oldest file (old.braw's proxy) must go first.
    newest_source, newest_proxy = _register(tmp_path, monkeypatch, "new.braw", 400)

    assert not os.path.isfile(oldest_proxy)
    assert os.path.isfile(middle_proxy)
    assert os.path.isfile(newest_proxy)
    assert braw_proxy_cache.find_cached_proxy(newest_source) == newest_proxy


def test_eviction_drops_the_index_entry_too(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("STUDIO_SUITE_PROXY_CACHE_MAX_BYTES", "1000")

    old_source, _ = _register(tmp_path, monkeypatch, "old.braw", 400, age_offset=100.0)
    _register(tmp_path, monkeypatch, "mid.braw", 400, age_offset=50.0)
    _register(tmp_path, monkeypatch, "new.braw", 400)

    # Evicted -- a subsequent lookup must be a clean miss, not a stale
    # index entry pointing at a now-deleted file.
    assert braw_proxy_cache.find_cached_proxy(old_source) is None


def test_freshly_registered_proxy_is_never_evicted_by_its_own_pass(monkeypatch, tmp_path):
    """Even a single proxy bigger than the whole cap must survive its own
    registration -- the cap is a soft cleanup target, not a hard reject."""
    _redirect_proxies_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("STUDIO_SUITE_PROXY_CACHE_MAX_BYTES", "100")

    source, proxy_path = _register(tmp_path, monkeypatch, "huge.braw", 5000)

    assert os.path.isfile(proxy_path)
    assert braw_proxy_cache.find_cached_proxy(source) == proxy_path


def test_under_cap_evicts_nothing(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("STUDIO_SUITE_PROXY_CACHE_MAX_BYTES", "1000000")

    _, first_proxy = _register(tmp_path, monkeypatch, "a.braw", 400, age_offset=10.0)
    _, second_proxy = _register(tmp_path, monkeypatch, "b.braw", 400)

    assert os.path.isfile(first_proxy)
    assert os.path.isfile(second_proxy)


def test_enforce_cache_cap_counts_orphaned_files_not_in_the_index(monkeypatch, tmp_path):
    """A proxy the index lost track of (e.g. a crashed run's leftover)
    must still count against the budget and still be evictable -- cap
    enforcement scans the directory itself, not just the index."""
    _redirect_proxies_dir(monkeypatch, tmp_path)
    proxies_dir = tmp_path / "proxies"
    os.makedirs(str(proxies_dir), exist_ok=True)
    orphan = proxies_dir / "orphan.mov"
    orphan.write_bytes(b"\x00" * 400)
    old_time = time.time() - 100.0
    os.utime(str(orphan), (old_time, old_time))

    monkeypatch.setenv("STUDIO_SUITE_PROXY_CACHE_MAX_BYTES", "1000")
    _, newest_proxy = _register(tmp_path, monkeypatch, "new.braw", 800)

    assert not os.path.isfile(str(orphan))
    assert os.path.isfile(newest_proxy)


# ---------------------------------------------------------------------------
# cache_usage / clear_cache (Settings-panel cache display + manual clear)
# ---------------------------------------------------------------------------

def test_cache_usage_on_empty_cache(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    usage = braw_proxy_cache.cache_usage()
    assert usage == {
        "bytes_used": 0, "file_count": 0,
        "bytes_cap": braw_proxy_cache.proxy_cache_max_bytes(),
    }


def test_cache_usage_counts_registered_and_orphaned_proxies(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    _register(tmp_path, monkeypatch, "a.braw", 400)
    proxies_dir = tmp_path / "proxies"
    (proxies_dir / "orphan.mov").write_bytes(b"\x00" * 100)

    usage = braw_proxy_cache.cache_usage()

    assert usage["file_count"] == 2
    assert usage["bytes_used"] == 500


def test_clear_cache_removes_every_proxy_and_resets_the_index(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    source, proxy_path = _register(tmp_path, monkeypatch, "a.braw", 400)

    removed = braw_proxy_cache.clear_cache()

    assert removed == 1
    assert not os.path.isfile(proxy_path)
    assert braw_proxy_cache.find_cached_proxy(source) is None
    assert braw_proxy_cache.cache_usage() == {
        "bytes_used": 0, "file_count": 0,
        "bytes_cap": braw_proxy_cache.proxy_cache_max_bytes(),
    }


def test_clear_cache_on_empty_cache_is_a_no_op(monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    assert braw_proxy_cache.clear_cache() == 0
