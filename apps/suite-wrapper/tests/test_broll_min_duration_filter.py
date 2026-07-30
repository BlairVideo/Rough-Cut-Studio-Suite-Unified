"""Very-short-clip filtering in the B-Roll workspace (addendum): a clip
whose own full duration is below broll_worker.MIN_USABLE_DURATION_SEC
(e.g. an accidental "record then immediately stop" take) gets shown as
an error-style card instead of a normal result whose "best segment" is
really just the whole unusable clip -- see broll_worker.py's own
docstring on the constant for why this is a suite-side display/export
filter rather than a change to analyzer.py itself (analyzer.py reports
these durations accurately; the clip genuinely is that short)."""

import os
import sys

# broll_worker.py redirects stdout to stderr as a MODULE-LEVEL side
# effect (protocol-stream setup for its real life as a subprocess worker)
# -- save/restore both the Python sys.stdout object and the real fd 1
# around the import, same pattern as test_sidecar_consolidation.py uses
# for transcribe_worker, so this doesn't corrupt pytest's own output
# capture.
_saved_stdout_obj = sys.stdout
_saved_fd1 = os.dup(1)
try:
    from backend.workers import broll_worker
finally:
    sys.stdout = _saved_stdout_obj
    os.dup2(_saved_fd1, 1)
    os.close(_saved_fd1)

from analyzer import ClipResult


def make_result(duration, error=None):
    return ClipResult(path="fake.mp4", filename="fake.mp4", duration=duration,
                       fps=60.0, width=3840, height=2160, overall_score=50.0,
                       error=error)


class TestClipPayloadMinDuration:
    def test_clip_shorter_than_threshold_gets_an_error_message_not_segments(self):
        result = make_result(duration=0.5)
        payload = broll_worker.clip_payload(result)
        assert payload["error"] is not None
        assert "0.5" in payload["error"]

    def test_clip_at_or_above_threshold_is_unaffected(self):
        result = make_result(duration=broll_worker.MIN_USABLE_DURATION_SEC)
        payload = broll_worker.clip_payload(result)
        assert payload["error"] is None

    def test_a_real_decode_error_is_not_overwritten_by_the_duration_message(self):
        # duration defaults to 0 on a genuine decode failure -- the
        # original error must win, not get replaced by the short-clip
        # message.
        result = make_result(duration=0.0, error="Could not open file")
        payload = broll_worker.clip_payload(result)
        assert payload["error"] == "Could not open file"


class TestRebuildFromCacheMinDuration:
    def _write_cache(self, folder, rel_path, duration):
        from analyzer import FrameSample
        from result_cache import entry_from_result, save_cache

        clip_path = os.path.join(folder, rel_path)
        with open(clip_path, "wb") as f:
            f.write(b"\x00" * 16)
        result = ClipResult(
            path=clip_path, filename=rel_path, duration=duration, fps=60.0,
            width=3840, height=2160,
            samples=[FrameSample(time_sec=0.0, sharpness=50.0, exposure=50.0,
                                  motion_mag=0.0, motion_jitter=0.0)])
        fp = os.stat(clip_path).st_size, os.stat(clip_path).st_mtime
        entries = {rel_path: entry_from_result(result, fp)}
        save_cache(folder, entries)
        return clip_path

    def test_too_short_clip_excluded_from_export_everything(self, tmp_path):
        folder = str(tmp_path)
        self._write_cache(folder, "short.mp4", duration=0.5)
        options = dict(broll_worker.DEFAULT_OPTIONS)
        results = broll_worker.rebuild_from_cache(folder, options, paths_filter=None)
        assert results == []

    def test_too_short_clip_still_included_if_explicitly_selected(self, tmp_path):
        folder = str(tmp_path)
        clip_path = self._write_cache(folder, "short.mp4", duration=0.5)
        options = dict(broll_worker.DEFAULT_OPTIONS)
        results = broll_worker.rebuild_from_cache(
            folder, options, paths_filter=[clip_path])
        assert len(results) == 1

    def test_normal_length_clip_is_unaffected(self, tmp_path):
        folder = str(tmp_path)
        self._write_cache(folder, "normal.mp4", duration=10.0)
        options = dict(broll_worker.DEFAULT_OPTIONS)
        results = broll_worker.rebuild_from_cache(folder, options, paths_filter=None)
        assert len(results) == 1
