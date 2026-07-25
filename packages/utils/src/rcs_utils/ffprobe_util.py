"""ffprobe_util.py — shared ffprobe metadata-read helpers.

Consumed via the rcs_utils package (packages/utils) by every app that needs
it: suite-wrapper, a-sync, broll-analyzer, interview-transcriber. Previously
vendored as four byte-identical copies before the monorepo migration —
see migration-plan.md §4/§6.2 for why that changed.

Read-only metadata inspection only (no transcode/export commands here —
those stay app-specific, since each app's render/extract pipeline has
genuinely different needs).
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional


def _ensure_common_ffmpeg_dirs_on_path() -> None:
    """A process launched via Finder/Dock/LaunchServices (as opposed to a
    Terminal shell) gets a minimal default PATH that doesn't include
    Homebrew's or MacPorts' bin directories — those are normally added by
    .zshrc/.zprofile, which only load for interactive shells. Without
    this, a machine with ffmpeg/ffprobe genuinely installed still fails
    with a raw "[Errno 2] No such file or directory: 'ffprobe'" the
    moment the app is launched by double-clicking (or via a Dock icon)
    rather than from a terminal, even though the exact same code works
    fine run directly from a shell.

    Runs once at import time (this module is imported early by every
    app's entry point) and only ever APPENDS known install locations
    that exist and aren't already present — never removes or reorders
    anything already on PATH, so it can't mask a different, deliberate
    ffmpeg install earlier in PATH."""
    candidates = ("/opt/homebrew/bin", "/opt/homebrew/sbin",
                  "/usr/local/bin", "/opt/local/bin")
    existing_raw = os.environ.get("PATH") or ""
    existing = existing_raw.split(os.pathsep) if existing_raw else []
    to_add = [p for p in candidates if os.path.isdir(p) and p not in existing]
    if to_add:
        os.environ["PATH"] = os.pathsep.join(existing + to_add)


_ensure_common_ffmpeg_dirs_on_path()


def probe_json(path: str, timeout: float, select_streams: Optional[str] = None,
                show_entries: Optional[str] = None, show_format: bool = False,
                show_streams: bool = False) -> dict:
    """Run ffprobe with -of json and return the parsed dict.

    Raises RuntimeError (never a raw TimeoutExpired) on a timeout or a
    non-zero exit; lets FileNotFoundError propagate as-is (ffprobe not on
    PATH) so callers that want to distinguish "not installed" from "failed"
    still can. Callers decide their own fail-open/fail-closed policy at the
    call site — this just removes the boilerplate of invoking ffprobe
    consistently.

    `show_format`/`show_streams` are the full-section dumps (`-show_format`/
    `-show_streams`); `show_entries` is ffprobe's narrower field-selection
    syntax. Combine `select_streams` + `show_entries` for a targeted query,
    or `show_format` + `show_streams` for a full probe.
    """
    cmd = ["ffprobe", "-v", "error"]
    if select_streams:
        cmd += ["-select_streams", select_streams]
    if show_format:
        cmd += ["-show_format"]
    if show_streams:
        cmd += ["-show_streams"]
    if show_entries:
        cmd += ["-show_entries", show_entries]
    cmd += ["-of", "json", str(path)]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffprobe timed out after {timeout}s on {path}")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr.decode(errors='replace')}")
    return json.loads(proc.stdout.decode(errors="replace") or "{}")


def probe_duration_seconds(path: str, timeout: float = 15) -> Optional[float]:
    """Container duration in seconds, or None if unavailable for any reason
    (ffprobe missing, timeout, no duration field) — fail-open, matching
    every existing call site's behavior."""
    try:
        data = probe_json(path, timeout, show_entries="format=duration")
        dur = (data.get("format") or {}).get("duration")
        return float(dur) if dur else None
    except Exception:
        return None


def probe_video_fps(path: str, timeout: float = 15) -> Optional[float]:
    """Average video frame rate from stream 0 (preferring avg_frame_rate
    over the nominal r_frame_rate for VFR content), or None if
    unavailable."""
    try:
        data = probe_json(path, timeout, select_streams="v:0",
                           show_entries="stream=avg_frame_rate,r_frame_rate")
        streams = data.get("streams") or []
        if not streams:
            return None
        stream = streams[0]
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = stream.get(key)
            if not raw or raw == "0/0":
                continue
            num, _, den = raw.partition("/")
            den = den or "1"
            try:
                num_f, den_f = float(num), float(den)
                if den_f > 0 and num_f > 0:
                    return num_f / den_f
            except ValueError:
                continue
        return None
    except Exception:
        return None


def probe_audio_format(path: str, timeout: float = 15) -> Optional[dict]:
    """{'channels', 'sample_rate', 'sample_fmt', 'channel_layout'} for the
    first audio stream (a:0), or None if there's no audio stream or ffprobe
    is unavailable/times out. Values are raw ffprobe types (int/str); a
    zero/missing numeric field is normalized to None. Callers apply their
    own defaults and channel-layout labels."""
    try:
        data = probe_json(path, timeout, select_streams="a:0",
                           show_entries="stream=channels,sample_rate,sample_fmt,channel_layout")
        streams = data.get("streams") or []
        if not streams:
            return None
        s = streams[0]
        channels = int(s.get("channels") or 0)
        sample_rate = int(s.get("sample_rate") or 0)
        return {
            "channels": channels or None,
            "sample_rate": sample_rate or None,
            "sample_fmt": s.get("sample_fmt"),
            "channel_layout": s.get("channel_layout"),
        }
    except Exception:
        return None
