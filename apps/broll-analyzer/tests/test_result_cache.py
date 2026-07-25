"""
tests/test_result_cache.py
Unit tests for result_cache.py's cache-hit logic: load/save round trips,
file fingerprinting, ClipResult<->cache-entry (de)serialization, the
is_entry_usable reuse decision, and the on-demand thumbnail patch.

All I/O happens inside pytest's `tmp_path` fixture, so nothing here
touches the real project folder or leaves anything behind.
"""

import base64
import json
import os

import pytest

import result_cache
from analyzer import ClipResult, FrameSample


def make_clip_result(**overrides):
    defaults = dict(
        path="/videos/clip.mp4", filename="clip.mp4", duration=12.5, fps=29.97,
        width=1920, height=1080,
        samples=[
            FrameSample(time_sec=0.0, sharpness=80.0, exposure=70.0,
                        motion_mag=1.0, motion_jitter=0.1, energy=55.0),
            FrameSample(time_sec=0.5, sharpness=82.0, exposure=71.0,
                        motion_mag=1.1, motion_jitter=0.2, energy=60.0),
        ],
        energy_enabled=True, mean_energy_score=57.5,
        audio_channels=2, audio_samplerate=48000, audio_bit_depth=16,
        audio_channel_layout="Stereo", audio_format_probed=True,
        thumbnail_jpeg=b"\xff\xd8fakejpegbytes", thumbnail_time=6.0,
    )
    defaults.update(overrides)
    return ClipResult(**defaults)


# ---------------------------------------------------------------------
# cache_path_for_folder
# ---------------------------------------------------------------------

def test_cache_path_is_inside_the_given_folder(tmp_path):
    path = result_cache.cache_path_for_folder(str(tmp_path))
    assert path == os.path.join(str(tmp_path), result_cache.CACHE_FILENAME)


# ---------------------------------------------------------------------
# load_cache -- best-effort loading
# ---------------------------------------------------------------------

class TestLoadCache:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert result_cache.load_cache(str(tmp_path)) == {}

    def test_corrupt_json_returns_empty_dict(self, tmp_path):
        cache_file = tmp_path / result_cache.CACHE_FILENAME
        cache_file.write_text("{not valid json")
        assert result_cache.load_cache(str(tmp_path)) == {}

    def test_wrong_version_returns_empty_dict(self, tmp_path):
        cache_file = tmp_path / result_cache.CACHE_FILENAME
        cache_file.write_text(json.dumps({"version": 999, "clips": {"a": {}}}))
        assert result_cache.load_cache(str(tmp_path)) == {}

    def test_non_dict_clips_value_returns_empty_dict(self, tmp_path):
        cache_file = tmp_path / result_cache.CACHE_FILENAME
        cache_file.write_text(json.dumps(
            {"version": result_cache.CACHE_VERSION, "clips": ["not", "a", "dict"]}))
        assert result_cache.load_cache(str(tmp_path)) == {}

    def test_non_dict_top_level_returns_empty_dict(self, tmp_path):
        cache_file = tmp_path / result_cache.CACHE_FILENAME
        cache_file.write_text(json.dumps(["not", "a", "dict"]))
        assert result_cache.load_cache(str(tmp_path)) == {}


# ---------------------------------------------------------------------
# save_cache / load_cache round trip
# ---------------------------------------------------------------------

class TestSaveCacheRoundTrip:
    def test_round_trip_preserves_entries(self, tmp_path):
        entries = {"clip_a.mp4": {"size": 100, "mtime": 1.0},
                   "sub/clip_b.mov": {"size": 200, "mtime": 2.0}}
        result_cache.save_cache(str(tmp_path), entries)
        assert result_cache.load_cache(str(tmp_path)) == entries

    def test_save_leaves_no_stray_tmp_file(self, tmp_path):
        result_cache.save_cache(str(tmp_path), {"a": {"size": 1, "mtime": 1.0}})
        cache_file = result_cache.cache_path_for_folder(str(tmp_path))
        assert os.path.exists(cache_file)
        assert not os.path.exists(cache_file + ".tmp")

    def test_on_disk_payload_shape(self, tmp_path):
        result_cache.save_cache(str(tmp_path), {"a": {"size": 1, "mtime": 1.0}})
        with open(result_cache.cache_path_for_folder(str(tmp_path))) as f:
            raw = json.load(f)
        assert raw["version"] == result_cache.CACHE_VERSION
        assert raw["clips"] == {"a": {"size": 1, "mtime": 1.0}}


# ---------------------------------------------------------------------
# file_fingerprint
# ---------------------------------------------------------------------

class TestFileFingerprint:
    def test_existing_file_returns_size_and_mtime(self, tmp_path):
        f = tmp_path / "clip.mp4"
        f.write_bytes(b"0123456789")
        fp = result_cache.file_fingerprint(str(f))
        st = os.stat(str(f))
        assert fp == (st.st_size, st.st_mtime)

    def test_missing_file_returns_none(self, tmp_path):
        assert result_cache.file_fingerprint(str(tmp_path / "nope.mp4")) is None


# ---------------------------------------------------------------------
# entry_from_result / result_from_entry -- ClipResult <-> cache entry
# ---------------------------------------------------------------------

class TestEntryRoundTrip:
    def test_round_trip_preserves_fields(self):
        result = make_clip_result()
        fp = (54321, 9999.5)
        entry = result_cache.entry_from_result(result, fp)

        assert entry["size"] == 54321
        assert entry["mtime"] == 9999.5
        assert entry["filename"] == "clip.mp4"
        assert entry["duration"] == 12.5
        assert entry["fps"] == 29.97
        assert len(entry["samples"]) == 2
        assert entry["thumbnail_jpeg_b64"] == base64.b64encode(result.thumbnail_jpeg).decode("ascii")
        assert entry["thumbnail_time"] == 6.0

        restored = result_cache.result_from_entry(result.path, entry)
        assert restored.filename == result.filename
        assert restored.duration == result.duration
        assert restored.fps == result.fps
        assert restored.width == result.width
        assert restored.height == result.height
        assert restored.energy_enabled == result.energy_enabled
        assert restored.audio_channels == result.audio_channels
        assert restored.audio_samplerate == result.audio_samplerate
        assert restored.audio_bit_depth == result.audio_bit_depth
        assert restored.audio_channel_layout == result.audio_channel_layout
        assert restored.thumbnail_jpeg == result.thumbnail_jpeg
        assert restored.thumbnail_time == result.thumbnail_time
        assert len(restored.samples) == 2
        for original, restored_sample in zip(result.samples, restored.samples):
            assert restored_sample.time_sec == original.time_sec
            assert restored_sample.sharpness == original.sharpness
            assert restored_sample.exposure == original.exposure
            assert restored_sample.motion_mag == original.motion_mag
            assert restored_sample.motion_jitter == original.motion_jitter
            assert restored_sample.energy == original.energy

    def test_no_thumbnail_round_trips_to_none(self):
        result = make_clip_result(thumbnail_jpeg=None, thumbnail_time=None)
        entry = result_cache.entry_from_result(result, (1, 1.0))
        assert entry["thumbnail_jpeg_b64"] is None
        restored = result_cache.result_from_entry(result.path, entry)
        assert restored.thumbnail_jpeg is None

    def test_missing_keys_fall_back_to_documented_defaults(self):
        # An empty entry is what you'd get from a hand-edited or
        # partially-written cache file -- every field must have a safe
        # fallback rather than raising a KeyError.
        restored = result_cache.result_from_entry("/videos/other.mov", {})
        assert restored.filename == "other.mov"  # from os.path.basename(path)
        assert restored.duration == 0.0
        assert restored.samples == []
        assert restored.audio_channels == 2
        assert restored.audio_samplerate == 48000
        assert restored.audio_bit_depth == 16
        assert restored.audio_channel_layout == "Stereo"
        assert restored.thumbnail_jpeg is None

    def test_corrupt_base64_thumbnail_degrades_to_none(self):
        # "abc" is not validly padded base64 -- base64.b64decode raises
        # binascii.Error (a ValueError subclass) on it.
        restored = result_cache.result_from_entry(
            "/videos/x.mp4", {"thumbnail_jpeg_b64": "abc"})
        assert restored.thumbnail_jpeg is None


# ---------------------------------------------------------------------
# is_entry_usable -- the actual cache-hit decision
# ---------------------------------------------------------------------

class TestIsEntryUsable:
    def test_none_entry_is_never_usable(self):
        assert result_cache.is_entry_usable(None, (1, 1.0), need_energy=False) is False

    def test_none_fingerprint_is_never_usable(self):
        assert result_cache.is_entry_usable({"size": 1, "mtime": 1.0}, None,
                                             need_energy=False) is False

    def test_mismatched_size_is_a_cache_miss(self):
        entry = {"size": 100, "mtime": 1.0, "energy_enabled": False}
        assert result_cache.is_entry_usable(entry, (999, 1.0), need_energy=False) is False

    def test_mismatched_mtime_is_a_cache_miss(self):
        entry = {"size": 100, "mtime": 1.0, "energy_enabled": False}
        assert result_cache.is_entry_usable(entry, (100, 999.0), need_energy=False) is False

    def test_matching_fingerprint_without_needing_energy_is_usable(self):
        entry = {"size": 100, "mtime": 1.0, "energy_enabled": False}
        assert result_cache.is_entry_usable(entry, (100, 1.0), need_energy=False) is True

    def test_needing_energy_but_entry_lacks_it_is_a_cache_miss(self):
        entry = {"size": 100, "mtime": 1.0, "energy_enabled": False}
        assert result_cache.is_entry_usable(entry, (100, 1.0), need_energy=True) is False

    def test_needing_energy_and_entry_has_it_is_usable(self):
        entry = {"size": 100, "mtime": 1.0, "energy_enabled": True}
        assert result_cache.is_entry_usable(entry, (100, 1.0), need_energy=True) is True

    def test_entry_with_energy_but_not_needing_it_is_still_usable(self):
        # A folder previously analyzed with energy scoring on, being
        # re-run with energy scoring off -- the extra data doesn't
        # disqualify the cache hit.
        entry = {"size": 100, "mtime": 1.0, "energy_enabled": True}
        assert result_cache.is_entry_usable(entry, (100, 1.0), need_energy=False) is True


# ---------------------------------------------------------------------
# update_thumbnail -- on-demand patch of a single cache entry
# ---------------------------------------------------------------------

class TestUpdateThumbnail:
    def test_updates_existing_entry_in_place(self, tmp_path):
        result_cache.save_cache(str(tmp_path), {
            "clip.mp4": {"size": 1, "mtime": 1.0, "thumbnail_jpeg_b64": None,
                         "thumbnail_time": None},
            "other.mp4": {"size": 2, "mtime": 2.0, "thumbnail_jpeg_b64": None,
                          "thumbnail_time": None},
        })
        result_cache.update_thumbnail(str(tmp_path), "clip.mp4", b"newjpegbytes", 3.5)

        entries = result_cache.load_cache(str(tmp_path))
        assert entries["clip.mp4"]["thumbnail_jpeg_b64"] == base64.b64encode(b"newjpegbytes").decode("ascii")
        assert entries["clip.mp4"]["thumbnail_time"] == 3.5
        # The other entry must be untouched.
        assert entries["other.mp4"]["thumbnail_jpeg_b64"] is None

    def test_clearing_thumbnail_with_none(self, tmp_path):
        result_cache.save_cache(str(tmp_path), {
            "clip.mp4": {"size": 1, "mtime": 1.0,
                         "thumbnail_jpeg_b64": base64.b64encode(b"old").decode("ascii"),
                         "thumbnail_time": 1.0},
        })
        result_cache.update_thumbnail(str(tmp_path), "clip.mp4", None, None)
        entries = result_cache.load_cache(str(tmp_path))
        assert entries["clip.mp4"]["thumbnail_jpeg_b64"] is None
        assert entries["clip.mp4"]["thumbnail_time"] is None

    def test_unknown_rel_path_is_a_silent_no_op(self, tmp_path):
        result_cache.save_cache(str(tmp_path), {
            "clip.mp4": {"size": 1, "mtime": 1.0, "thumbnail_jpeg_b64": None},
        })
        cache_file = result_cache.cache_path_for_folder(str(tmp_path))
        before = cache_file and open(cache_file).read()

        result_cache.update_thumbnail(str(tmp_path), "does_not_exist.mp4", b"x", 1.0)

        after = open(cache_file).read()
        assert before == after

    def test_no_cache_file_at_all_is_a_silent_no_op(self, tmp_path):
        # Must not raise, and must not create a cache file just from a
        # thumbnail-refresh patch attempt.
        result_cache.update_thumbnail(str(tmp_path), "clip.mp4", b"x", 1.0)
        assert not os.path.exists(result_cache.cache_path_for_folder(str(tmp_path)))
