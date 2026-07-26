"""
tests/test_waveform_view.py

Unit tests for waveform_view.py's compute_peaks -- the pure-numpy
min/max downsampling used to draw the waveform canvas. Does not touch
the Tkinter Canvas widget itself.
"""

import numpy as np
import pytest

from waveform_view import compute_peaks


def test_compute_peaks_shape():
    samples = np.linspace(-1, 1, 1000, dtype=np.float32)
    peaks = compute_peaks(samples, num_buckets=10)
    assert peaks.shape == (10, 2)


def test_compute_peaks_min_max_per_bucket():
    # A monotonically increasing ramp within [-1, 1] -- each bucket's min
    # should be its first (smallest) sample, max its last (largest).
    samples = np.linspace(-1, 1, 40, dtype=np.float32)
    peaks = compute_peaks(samples, num_buckets=4)
    assert peaks.shape == (4, 2)
    # monotonic ramp -> each bucket's min/max should itself increase bucket-over-bucket
    assert np.all(np.diff(peaks[:, 0]) > 0)
    assert np.all(np.diff(peaks[:, 1]) > 0)
    assert peaks[0, 0] == pytest.approx(-1.0)
    assert peaks[-1, 1] == pytest.approx(1.0)


def test_compute_peaks_clips_to_unit_range():
    samples = np.array([-5.0, 5.0], dtype=np.float32)
    peaks = compute_peaks(samples, num_buckets=1)
    assert peaks[0, 0] == pytest.approx(-1.0)
    assert peaks[0, 1] == pytest.approx(1.0)


def test_compute_peaks_multichannel_averaged_to_mono():
    # Stereo where L and R are mirror images -- averaging should collapse
    # both channels toward zero rather than reflecting either channel alone.
    left = np.full(100, 0.8, dtype=np.float32)
    right = np.full(100, -0.8, dtype=np.float32)
    stereo = np.stack([left, right], axis=1)
    peaks = compute_peaks(stereo, num_buckets=1)
    assert peaks[0, 0] == pytest.approx(0.0, abs=1e-6)
    assert peaks[0, 1] == pytest.approx(0.0, abs=1e-6)


def test_compute_peaks_empty_input_returns_zeros():
    peaks = compute_peaks(np.array([], dtype=np.float32), num_buckets=5)
    assert peaks.shape == (5, 2)
    assert np.all(peaks == 0.0)


def test_compute_peaks_none_input_returns_zeros():
    peaks = compute_peaks(None, num_buckets=3)
    assert peaks.shape == (3, 2)
    assert np.all(peaks == 0.0)


def test_compute_peaks_zero_buckets_returns_empty():
    samples = np.linspace(-1, 1, 100, dtype=np.float32)
    peaks = compute_peaks(samples, num_buckets=0)
    assert peaks.shape == (0, 2)


def test_compute_peaks_pads_when_not_evenly_divisible():
    # 10 samples into 3 buckets -> bucket_size=4, padded to 12 with zeros.
    samples = np.arange(1, 11, dtype=np.float32)  # 1..10
    peaks = compute_peaks(samples, num_buckets=3)
    assert peaks.shape == (3, 2)
    # last bucket is [9, 10, 0, 0] (padding) -> min is 0, not 9
    assert peaks[2, 0] == pytest.approx(0.0)
