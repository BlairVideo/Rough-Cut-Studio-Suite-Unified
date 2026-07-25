#!/usr/bin/env python3
"""Harmonizer Phase 1 prototype: ref + take audio -> JSON sync report.

Usage:
    python align.py --ref ref.wav --takes take1.wav take2.wav take3.wav --out report.json
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import tempfile

import librosa
import numpy as np
from scipy.signal import correlate, find_peaks

SR = 16000
MIN_SEGMENT_DUR = 0.02

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BRAW_EXTRACT_AUDIO = os.path.join(_SCRIPT_DIR, "braw_sdk", "Samples", "ExtractAudio", "ExtractAudio")
_BRAW_SCRATCH_WAV = "/tmp/outputAudio.wav"  # hardcoded output path baked into Blackmagic's ExtractAudio sample


def extract_braw_audio(braw_path):
    """Runs Blackmagic's ExtractAudio SDK sample to pull the embedded audio
    out of a .braw clip as a native WAV. It resolves its bundled framework
    via a path relative to its own working directory (not the exe's own
    location), so it must be invoked with cwd set to its own folder."""
    if not os.path.exists(_BRAW_EXTRACT_AUDIO):
        raise FileNotFoundError(
            f"Blackmagic RAW SDK not found at {_BRAW_EXTRACT_AUDIO} -- "
            "see prototype/braw_sdk/README or copy it from an installed SDK."
        )
    if os.path.exists(_BRAW_SCRATCH_WAV):
        os.remove(_BRAW_SCRATCH_WAV)

    result = subprocess.run(
        [_BRAW_EXTRACT_AUDIO, os.path.abspath(braw_path)],
        cwd=os.path.dirname(_BRAW_EXTRACT_AUDIO),
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not os.path.exists(_BRAW_SCRATCH_WAV):
        raise RuntimeError(f"ExtractAudio failed for {braw_path}: {result.stderr.strip()}")

    out_fd, out_path = tempfile.mkstemp(suffix=".wav", prefix="braw_audio_")
    os.close(out_fd)
    shutil.move(_BRAW_SCRATCH_WAV, out_path)
    return out_path


def load_mono(path):
    wav_path = path
    braw_temp = None
    if os.path.splitext(path)[1].lower() == ".braw":
        braw_temp = extract_braw_audio(path)
        wav_path = braw_temp
    try:
        y, _ = librosa.load(wav_path, sr=SR, mono=True)
    finally:
        if braw_temp:
            os.remove(braw_temp)
    return y


def waveform_peaks(audio, num_buckets=2000):
    """Downsample audio to a per-bucket max-abs-amplitude envelope for the QA
    UI's waveform view -- a couple thousand points is plenty for on-screen
    rendering and keeps the report small (2000 floats vs. ~2M samples)."""
    if len(audio) == 0:
        return []
    bucket_size = max(1, len(audio) // num_buckets)
    trimmed = audio[: bucket_size * (len(audio) // bucket_size)]
    if trimmed.size == 0:
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        return [peak]
    buckets = trimmed.reshape(-1, bucket_size)
    peaks = np.max(np.abs(buckets), axis=1)
    return [round(float(p), 4) for p in peaks]


def gcc_phat(sig, ref, fs, max_tau=None, interp=16):
    n = sig.shape[0] + ref.shape[0]
    n_fft = 1 << (n - 1).bit_length()

    SIG = np.fft.rfft(sig, n=n_fft)
    REF = np.fft.rfft(ref, n=n_fft)
    R = SIG * np.conj(REF)

    denom = np.abs(R)
    denom[denom < 1e-15] = 1e-15
    cc = np.fft.irfft(R / denom, n=n_fft * interp)

    max_shift = int(n_fft * interp / 2)
    if max_tau:
        max_shift = min(int(interp * fs * max_tau), max_shift)

    cc = np.concatenate((cc[-max_shift:], cc[: max_shift + 1]))

    peak_idx = int(np.argmax(np.abs(cc)))
    peak_val = np.abs(cc[peak_idx])
    confidence = float(peak_val / (np.mean(np.abs(cc)) + 1e-12))

    shift = peak_idx - max_shift
    tau = shift / float(interp * fs)
    return tau, confidence


def coarse_offset(take_audio, ref_audio, max_lead_in=60.0):
    """Whole-file GCC-PHAT is otherwise free to pick a peak anywhere across
    the entire recording -- confirmed on real data where an unbounded search
    locked onto a shift almost exactly equal to negative the full reference
    duration (a spurious/edge peak, not a real match), silently producing a
    take with zero usable anchors. Bounding max_tau to a plausible lead-in/
    lead-out window keeps the search from ever considering a shift no real
    multi-camera take would have."""
    tau, confidence = gcc_phat(take_audio, ref_audio, SR, max_tau=max_lead_in, interp=8)
    return tau, confidence


def detect_onsets(ref_audio):
    times = librosa.onset.onset_detect(
        y=ref_audio, sr=SR, units="time", backtrack=True
    )
    return times.tolist()


def local_match_candidates(
    ref_audio, take_audio, ref_time, guess_take_time,
    window_radius, search_radius, k, min_peak_separation,
):
    """Return up to k candidate (take_time, confidence) matches in the search
    window, instead of just the single best one -- ambiguous passages (a
    repeated phrase, an ornament) often have more than one plausible match,
    and the global path solver needs alternatives to pick a coherent one."""
    ref_start = max(0, int((ref_time - window_radius) * SR))
    ref_end = min(len(ref_audio), int((ref_time + window_radius) * SR))
    ref_window = ref_audio[ref_start:ref_end]
    if ref_window.size == 0:
        return []

    search_start = max(0, int((guess_take_time - window_radius - search_radius) * SR))
    search_end = max(0, min(len(take_audio), int((guess_take_time + window_radius + search_radius) * SR)))
    if search_end <= search_start:
        return []
    search_region = take_audio[search_start:search_end]
    if search_region.size < ref_window.size:
        return []

    ref_norm = ref_window - ref_window.mean()
    ref_norm_energy = np.sqrt(np.sum(ref_norm ** 2)) + 1e-12

    cc = correlate(search_region - search_region.mean(), ref_norm, mode="valid")
    window_energies = np.sqrt(
        np.convolve(search_region ** 2, np.ones(ref_window.size), mode="valid")
    ) + 1e-12
    normalized_cc = cc / (window_energies * ref_norm_energy)
    if normalized_cc.size == 0:
        return []

    distance_samples = max(1, int(min_peak_separation * SR))
    peak_indices, _ = find_peaks(normalized_cc, distance=distance_samples)
    if peak_indices.size == 0:
        peak_indices = np.array([int(np.argmax(normalized_cc))])

    order = np.argsort(normalized_cc[peak_indices])[::-1][:k]
    top_indices = peak_indices[order]

    candidates = []
    for idx in top_indices:
        match_start_sample = search_start + int(idx)
        match_center_time = (match_start_sample + ref_window.size / 2) / SR
        candidates.append((match_center_time, float(normalized_cc[idx])))
    candidates.sort(key=lambda c: -c[1])
    return candidates


def build_anchors(
    ref_audio, takes_audio, take_names, coarse_offsets,
    window_radius, search_radius, candidates_per_anchor, min_peak_separation,
):
    ref_onsets = detect_onsets(ref_audio)
    anchors = []
    for ref_time in ref_onsets:
        candidates_by_take = {}
        for name, audio in zip(take_names, takes_audio):
            guess = ref_time + coarse_offsets[name][0]
            candidates_by_take[name] = local_match_candidates(
                ref_audio, audio, ref_time, guess,
                window_radius, search_radius, candidates_per_anchor, min_peak_separation,
            )
        anchors.append({"ref_time": ref_time, "candidates": candidates_by_take})
    return anchors


class _Node:
    __slots__ = ("ref_time", "take_time", "confidence", "anchor_index")

    def __init__(self, ref_time, take_time, confidence, anchor_index):
        self.ref_time = ref_time
        self.take_time = take_time
        self.confidence = confidence
        self.anchor_index = anchor_index


def solve_take_path(usable_anchors, start_point, end_point, speed_penalty_weight, confidence_weight):
    """Dynamic-programming pass over every candidate match for this take,
    finding the single monotonic (in both ref_time and take_time) path from
    start to end that minimizes cumulative cost. Cost per step trades off
    match confidence against how far the resulting speed factor strays from
    1.0, so a slightly-less-confident-but-plausible match beats a
    high-confidence match on the wrong note that would otherwise produce an
    implausible speed factor. An anchor with no candidate on the winning path
    is simply skipped -- the segment bridges straight across it."""
    nodes = [_Node(start_point[0], start_point[1], 1.0, None)]
    for anchor_index, ref_time, candidates in usable_anchors:
        for take_time, conf in candidates:
            nodes.append(_Node(ref_time, take_time, conf, anchor_index))
    nodes.append(_Node(end_point[0], end_point[1], 1.0, None))

    n = len(nodes)
    cost = [math.inf] * n
    prev = [-1] * n
    cost[0] = 0.0

    for i in range(1, n):
        node = nodes[i]
        best_cost, best_prev = math.inf, -1
        for j in range(i):
            pj = nodes[j]
            if node.anchor_index is not None and pj.anchor_index == node.anchor_index:
                continue
            take_dur = node.take_time - pj.take_time
            if take_dur <= MIN_SEGMENT_DUR:
                continue
            ref_dur = node.ref_time - pj.ref_time
            if ref_dur <= 0:
                continue
            speed = ref_dur / take_dur
            # Confidence is a reward (negative cost), not a penalty: a path
            # that includes more plausible, reasonably-confident anchors must
            # come out cheaper than skipping straight to the end, otherwise
            # the DP always prefers one giant unanchored segment.
            step_cost = abs(math.log(speed)) * speed_penalty_weight - node.confidence * confidence_weight
            total = cost[j] + step_cost
            if total < best_cost:
                best_cost, best_prev = total, j
        cost[i], prev[i] = best_cost, best_prev

    if cost[n - 1] == math.inf:
        path = [
            {"ref_time": start_point[0], "take_time": start_point[1], "confidence": 1.0, "anchor_index": None},
            {"ref_time": end_point[0], "take_time": end_point[1], "confidence": 1.0, "anchor_index": None},
        ]
        return path, set()

    path_indices = []
    cur = n - 1
    while cur != -1:
        path_indices.append(cur)
        cur = prev[cur]
    path_indices.reverse()

    path = [
        {
            "ref_time": nodes[i].ref_time,
            "take_time": nodes[i].take_time,
            "confidence": nodes[i].confidence,
            "anchor_index": nodes[i].anchor_index,
        }
        for i in path_indices
    ]
    accepted_indices = {p["anchor_index"] for p in path if p["anchor_index"] is not None}
    return path, accepted_indices


def segments_from_path(path, merge_tolerance, flag_speed_min, flag_speed_max):
    """Turn an ordered list of {"ref_time", "take_time"} points (a solved DP
    path, or a manually-edited anchor list from the QA UI) into merged,
    flagged segments. Shared by the automatic pipeline and by recomputation
    after a user nudges/deletes/inserts an anchor."""
    raw_segments = []
    for a, b in zip(path, path[1:]):
        ref_dur = b["ref_time"] - a["ref_time"]
        take_dur = b["take_time"] - a["take_time"]
        raw_segments.append(
            {
                "ref_start": a["ref_time"],
                "ref_end": b["ref_time"],
                "take_start": a["take_time"],
                "take_end": b["take_time"],
                "speed_factor": ref_dur / take_dur,
            }
        )

    merged = []
    for seg in raw_segments:
        if merged and abs(merged[-1]["speed_factor"] - 1.0) <= merge_tolerance and abs(
            seg["speed_factor"] - 1.0
        ) <= merge_tolerance:
            prev = merged[-1]
            prev["ref_end"] = seg["ref_end"]
            prev["take_end"] = seg["take_end"]
            prev["speed_factor"] = (prev["ref_end"] - prev["ref_start"]) / (
                prev["take_end"] - prev["take_start"]
            )
        else:
            merged.append(dict(seg))

    for seg in merged:
        seg["flagged"] = not (flag_speed_min <= seg["speed_factor"] <= flag_speed_max)
    return merged


def build_segments(
    anchors, ref_duration, take_durations, take_names, coarse_offsets, merge_tolerance,
    flag_speed_min, flag_speed_max, speed_penalty_weight, confidence_weight,
    no_retime_takes=frozenset(),
):
    indexed_anchors = sorted(enumerate(anchors), key=lambda pair: pair[1]["ref_time"])

    segments_by_take = {}
    skipped_anchor_counts = {}
    leadin_ref_sec = {}
    anchor_take_times = {i: {} for i in range(len(anchors))}
    anchor_confidence = {i: {} for i in range(len(anchors))}

    for name in take_names:
        offset = coarse_offsets[name][0]
        take_duration = take_durations[name]

        # A nonzero coarse offset means the take and reference didn't start
        # rolling in sync. If offset < 0, the reference started first, so the
        # take has no footage for the first -offset seconds of ref time --
        # that span must be excluded, not forced into a segment against take
        # time 0. If offset >= 0, the take already has content at ref_time 0.
        if offset >= 0:
            start_ref_time, start_take_time = 0.0, offset
        else:
            start_ref_time, start_take_time = -offset, 0.0
        leadin_ref_sec[name] = start_ref_time

        if name in no_retime_takes:
            # This take's audio is known to be the same source as the
            # reference (e.g. fed from the same recorder), so any per-segment
            # speed variation the matcher would compute is analysis noise,
            # not real drift -- confirmed on real data where a same-source
            # take still showed segments swinging 0.86x-1.37x despite a
            # correct 1.0002 median. Skip matching entirely and emit one
            # straight segment positioned by the coarse offset alone.
            path = [
                {"ref_time": start_ref_time, "take_time": start_take_time, "anchor_index": None},
                {"ref_time": ref_duration, "take_time": take_duration, "anchor_index": None},
            ]
            skipped_anchor_counts[name] = sum(
                1 for _, a in indexed_anchors if a["ref_time"] > start_ref_time and a["candidates"][name]
            )
        else:
            usable = [
                (idx, a["ref_time"], a["candidates"][name])
                for idx, a in indexed_anchors
                if a["ref_time"] > start_ref_time and a["candidates"][name]
            ]

            path, accepted_indices = solve_take_path(
                usable, (start_ref_time, start_take_time), (ref_duration, take_duration),
                speed_penalty_weight, confidence_weight,
            )
            skipped_anchor_counts[name] = len(usable) - len(accepted_indices)

        for p in path:
            if p["anchor_index"] is not None:
                anchor_take_times[p["anchor_index"]][name] = p["take_time"]
                anchor_confidence[p["anchor_index"]][name] = p["confidence"]

        segments_by_take[name] = segments_from_path(
            path, merge_tolerance, flag_speed_min, flag_speed_max
        )

    anchors_report = [
        {
            "ref_time": anchors[i]["ref_time"],
            "take_times": {n: anchor_take_times[i].get(n) for n in take_names},
            "confidence": {n: anchor_confidence[i].get(n) for n in take_names},
        }
        for i in range(len(anchors))
    ]

    return segments_by_take, skipped_anchor_counts, leadin_ref_sec, anchors_report


def main():
    parser = argparse.ArgumentParser(description="Phase 1 sync-alignment prototype")
    parser.add_argument("--ref", required=True)
    parser.add_argument("--takes", required=True, nargs="+")
    parser.add_argument("--out", required=True)
    parser.add_argument("--window-radius", type=float, default=0.15)
    parser.add_argument("--search-radius", type=float, default=0.5)
    parser.add_argument("--merge-tolerance", type=float, default=0.02)
    parser.add_argument("--flag-speed-min", type=float, default=0.5)
    parser.add_argument("--flag-speed-max", type=float, default=2.0)
    parser.add_argument("--candidates-per-anchor", type=int, default=3)
    parser.add_argument("--min-peak-separation", type=float, default=0.05)
    parser.add_argument("--speed-penalty-weight", type=float, default=3.0)
    parser.add_argument("--confidence-weight", type=float, default=1.0)
    parser.add_argument(
        "--max-lead-in", type=float, default=60.0,
        help="bounds the coarse whole-file offset search to +/- this many seconds -- "
             "prevents a spurious cross-correlation peak from landing on an implausible "
             "shift (e.g. a repeated phrase matching across most of the recording)",
    )
    parser.add_argument(
        "--no-retime", nargs="*", default=[],
        help="basenames of takes known to share the reference's audio source "
             "(e.g. fed from the same recorder) -- skip matching entirely and "
             "emit a single straight segment positioned by the coarse offset",
    )
    args = parser.parse_args()

    ref_audio = load_mono(args.ref)
    take_names = [os.path.basename(p) for p in args.takes]
    takes_audio = [load_mono(p) for p in args.takes]

    coarse_offsets = {}
    for name, audio in zip(take_names, takes_audio):
        tau, conf = coarse_offset(audio, ref_audio, args.max_lead_in)
        coarse_offsets[name] = (tau, conf)

    anchors = build_anchors(
        ref_audio, takes_audio, take_names, coarse_offsets,
        args.window_radius, args.search_radius,
        args.candidates_per_anchor, args.min_peak_separation,
    )

    ref_duration = len(ref_audio) / SR
    take_durations = {n: len(a) / SR for n, a in zip(take_names, takes_audio)}

    no_retime_takes = set(args.no_retime)
    unknown = no_retime_takes - set(take_names)
    if unknown:
        raise SystemExit(f"--no-retime name(s) not among takes: {sorted(unknown)}")

    segments, skipped_anchor_counts, leadin_ref_sec, anchors_report = build_segments(
        anchors, ref_duration, take_durations, take_names, coarse_offsets, args.merge_tolerance,
        args.flag_speed_min, args.flag_speed_max, args.speed_penalty_weight, args.confidence_weight,
        no_retime_takes,
    )

    waveforms = {"reference": waveform_peaks(ref_audio)}
    for name, audio in zip(take_names, takes_audio):
        waveforms[name] = waveform_peaks(audio)

    report = {
        "reference": os.path.basename(args.ref),
        "takes": take_names,
        "ref_duration": ref_duration,
        "take_durations": take_durations,
        "coarse_offsets_sec": {n: v[0] for n, v in coarse_offsets.items()},
        "coarse_offset_confidence": {n: v[1] for n, v in coarse_offsets.items()},
        "anchors": anchors_report,
        "segments": segments,
        "skipped_anchors": skipped_anchor_counts,
        "excluded_leadin_ref_sec": leadin_ref_sec,
        "waveforms": waveforms,
        "merge_tolerance": args.merge_tolerance,
        "flag_speed_min": args.flag_speed_min,
        "flag_speed_max": args.flag_speed_max,
    }

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)

    print(f"Wrote {len(anchors)} anchors, segments for {len(take_names)} takes -> {args.out}")
    for name in take_names:
        flagged = sum(1 for s in segments[name] if s["flagged"])
        print(
            f"  {name}: excluded {leadin_ref_sec[name]:.3f}s ref lead-in, "
            f"skipped {skipped_anchor_counts[name]} anchor(s) in DP path, "
            f"flagged {flagged}/{len(segments[name])} segment(s)"
        )


if __name__ == "__main__":
    main()
