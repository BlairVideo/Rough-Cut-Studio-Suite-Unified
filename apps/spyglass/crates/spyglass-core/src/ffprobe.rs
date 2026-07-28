//! ffprobe-based per-clip audio format probing (Section 15): both the
//! XMEML exporter and the consolidate export's manifest need a file's
//! *real* channel count / sample rate / bit depth rather than the fixed
//! stereo/48kHz/16-bit assumption Phase 4's XMEML export deliberately
//! deferred (Section 14's build note) -- built once here and shared by
//! both, per Section 15's "worth building once and sharing" call.
//!
//! Follows the same local-binary philosophy as the rest of the Suite
//! (Card Eater's own conventions, this repo's `CLAUDE.md`): shells out to
//! the system `ffprobe` rather than pulling in an FFI/decoder dependency.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::process::Command;
use thiserror::Error;

/// A clip's real audio format, as probed from its first audio stream.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct AudioFormat {
    pub channels: u32,
    pub sample_rate: u32,
    /// Bit depth if the container/codec exposes one -- many compressed
    /// formats (AAC, etc.) don't report `bits_per_raw_sample` at all, so
    /// this stays `None` rather than a guessed value; call sites needing a
    /// concrete number (XMEML's `<samplecharacteristics><depth>`) fall
    /// back to 16, matching the broadcast-default this replaces.
    pub bit_depth: Option<u32>,
}

impl AudioFormat {
    /// The fixed assumption Phase 4's XMEML export used everywhere --
    /// still the right fallback when a clip has no probeable audio stream
    /// at all (silent footage) or ffprobe itself isn't available.
    pub const FALLBACK: AudioFormat = AudioFormat { channels: 2, sample_rate: 48000, bit_depth: Some(16) };
}

#[derive(Debug, Error, PartialEq)]
pub enum ProbeError {
    #[error("ffprobe failed to start: {0}")]
    Spawn(String),
    #[error("ffprobe exited with status {status}: {stderr}")]
    Failed { status: i32, stderr: String },
    #[error("could not parse ffprobe output: {0}")]
    InvalidOutput(String),
}

#[derive(Debug, Deserialize)]
struct FfprobeStream {
    #[serde(default)]
    channels: Option<u32>,
    #[serde(default)]
    sample_rate: Option<String>,
    /// Populated for compressed codecs that decode to a wider depth than
    /// their raw sample size (e.g. some AAC profiles) -- empty for PCM,
    /// where `bits_per_sample` (below) already holds the real depth.
    #[serde(default)]
    bits_per_raw_sample: Option<String>,
    /// What ffprobe actually reports for uncompressed PCM streams --
    /// confirmed against a real synthesized pcm_s16le file, where
    /// `bits_per_raw_sample` comes back empty but this field is `16`.
    #[serde(default)]
    bits_per_sample: Option<u32>,
}

#[derive(Debug, Deserialize)]
struct FfprobeOutput {
    #[serde(default)]
    streams: Vec<FfprobeStream>,
}

/// Parses `ffprobe -show_entries stream=... -of json`'s stdout into an
/// `AudioFormat`, or `Ok(None)` when the file has no audio stream at all --
/// split out from the subprocess call itself so this (the part with real
/// logic) is unit-testable against fixture JSON without spawning ffprobe.
fn parse_ffprobe_json(stdout: &[u8]) -> Result<Option<AudioFormat>, ProbeError> {
    let parsed: FfprobeOutput =
        serde_json::from_slice(stdout).map_err(|e| ProbeError::InvalidOutput(e.to_string()))?;
    let Some(stream) = parsed.streams.first() else {
        return Ok(None);
    };

    let channels = stream.channels.unwrap_or(AudioFormat::FALLBACK.channels);
    let sample_rate = stream
        .sample_rate
        .as_deref()
        .and_then(|s| s.parse().ok())
        .unwrap_or(AudioFormat::FALLBACK.sample_rate);
    let bit_depth = stream
        .bits_per_raw_sample
        .as_deref()
        .and_then(|s| s.parse().ok())
        .or(stream.bits_per_sample)
        .filter(|&d| d > 0);

    Ok(Some(AudioFormat { channels, sample_rate, bit_depth }))
}

/// Probes `path`'s first audio stream via ffprobe. `Ok(None)` (not an
/// error) means the file genuinely has no audio stream -- callers should
/// fall back to `AudioFormat::FALLBACK` in that case, same as an error.
pub fn probe_audio_format(path: &Path) -> Result<Option<AudioFormat>, ProbeError> {
    let output = Command::new(crate::ffmpeg_paths::ffprobe_path())
        .args([
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels,sample_rate,bits_per_raw_sample,bits_per_sample",
            "-of",
            "json",
        ])
        .arg(path)
        .output()
        .map_err(|e| ProbeError::Spawn(e.to_string()))?;

    if !output.status.success() {
        return Err(ProbeError::Failed {
            status: output.status.code().unwrap_or(-1),
            stderr: String::from_utf8_lossy(&output.stderr).trim().to_string(),
        });
    }

    parse_ffprobe_json(&output.stdout)
}

/// `probe_audio_format`, but never fails -- ffprobe missing from PATH, a
/// corrupt/unreadable file, or a clip with no audio stream should degrade
/// to the broadcast-default assumption rather than abort an export (XMEML
/// or consolidate) over one bad clip's audio probe.
pub fn probe_audio_format_or_fallback(path: &Path) -> AudioFormat {
    probe_audio_format(path).ok().flatten().unwrap_or(AudioFormat::FALLBACK)
}

/// The format-level string ISO 8601 timestamp columns in this schema share
/// (`clips.ingested_at`, and now `clips.recorded_at`) all use: millisecond
/// precision, `Z` suffix -- matters because sort-by-date compares these as
/// plain strings (facets.rs/search.rs), so every writer needs the same
/// width or lexicographic ordering breaks.
fn format_timestamp(dt: DateTime<Utc>) -> String {
    dt.format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string()
}

#[derive(Debug, Deserialize)]
struct FfprobeFormatTags {
    #[serde(default)]
    creation_time: Option<String>,
}

#[derive(Debug, Deserialize)]
struct FfprobeFormat {
    #[serde(default)]
    tags: Option<FfprobeFormatTags>,
}

#[derive(Debug, Deserialize)]
struct FfprobeFormatOutput {
    #[serde(default)]
    format: Option<FfprobeFormat>,
}

/// Parses `ffprobe -show_entries format_tags=creation_time -of json`'s
/// stdout, normalizing the tag's own timestamp format to this schema's
/// shared string-sortable one (see `format_timestamp`). `Ok(None)` covers
/// both "no such tag" (common for re-muxed/converted files) and an
/// unparseable value -- split out for unit testing without spawning
/// ffprobe, same convention as `parse_ffprobe_json`.
fn parse_creation_time_json(stdout: &[u8]) -> Result<Option<String>, ProbeError> {
    let parsed: FfprobeFormatOutput =
        serde_json::from_slice(stdout).map_err(|e| ProbeError::InvalidOutput(e.to_string()))?;
    let Some(raw) = parsed.format.and_then(|f| f.tags).and_then(|t| t.creation_time) else {
        return Ok(None);
    };
    Ok(DateTime::parse_from_rfc3339(&raw).ok().map(|dt| format_timestamp(dt.with_timezone(&Utc))))
}

/// The camera/editing-software-embedded `creation_time` tag -- the closest
/// thing to a real capture date this pipeline can read, independent of
/// when Spyglass happened to scan the file (`clips.ingested_at`). `Ok(None)`
/// (not an error) covers both "no such tag" and an unparseable timestamp --
/// both mean "fall back to mtime", same non-fatal contract as
/// `probe_audio_format`.
pub fn probe_creation_time(path: &Path) -> Result<Option<String>, ProbeError> {
    let output = Command::new(crate::ffmpeg_paths::ffprobe_path())
        .args(["-v", "error", "-show_entries", "format_tags=creation_time", "-of", "json"])
        .arg(path)
        .output()
        .map_err(|e| ProbeError::Spawn(e.to_string()))?;

    if !output.status.success() {
        return Err(ProbeError::Failed {
            status: output.status.code().unwrap_or(-1),
            stderr: String::from_utf8_lossy(&output.stderr).trim().to_string(),
        });
    }

    parse_creation_time_json(&output.stdout)
}

/// The best available real-world capture timestamp for `path`: the
/// embedded `creation_time` tag when ffprobe can read one, otherwise the
/// filesystem mtime. `None` only when both fail (ffprobe missing/errored
/// with no tag, and the file's own metadata is unreadable -- e.g. its
/// watched-root drive is offline) -- callers treat that the same as "not
/// yet known" (`clips.recorded_at` stays NULL, sort-by-date falls back to
/// `ingested_at` via SQL `COALESCE`).
pub fn recorded_at_for_file(path: &Path) -> Option<String> {
    if let Ok(Some(creation_time)) = probe_creation_time(path) {
        return Some(creation_time);
    }
    let modified = std::fs::metadata(path).ok()?.modified().ok()?;
    Some(format_timestamp(DateTime::<Utc>::from(modified)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_stereo_pcm_stream_using_bits_per_sample() {
        // Confirmed against a real synthesized pcm_s16le file (Section
        // 15): ffprobe reports PCM depth via `bits_per_sample`, not
        // `bits_per_raw_sample` (which comes back empty for PCM).
        let json = br#"{"streams":[{"channels":2,"sample_rate":"48000","bits_per_sample":16}]}"#;
        let format = parse_ffprobe_json(json).unwrap().unwrap();
        assert_eq!(format, AudioFormat { channels: 2, sample_rate: 48000, bit_depth: Some(16) });
    }

    #[test]
    fn parses_mono_stream_from_a_field_camera_mic() {
        let json = br#"{"streams":[{"channels":1,"sample_rate":"44100","bits_per_sample":16}]}"#;
        let format = parse_ffprobe_json(json).unwrap().unwrap();
        assert_eq!(format, AudioFormat { channels: 1, sample_rate: 44100, bit_depth: Some(16) });
    }

    #[test]
    fn prefers_bits_per_raw_sample_over_bits_per_sample_when_both_present() {
        let json = br#"{"streams":[{"channels":2,"sample_rate":"48000","bits_per_raw_sample":"24","bits_per_sample":16}]}"#;
        let format = parse_ffprobe_json(json).unwrap().unwrap();
        assert_eq!(format.bit_depth, Some(24));
    }

    #[test]
    fn missing_both_bit_depth_fields_is_none_not_a_guess() {
        // Real case: compressed codecs like AAC report neither field.
        let json = br#"{"streams":[{"channels":2,"sample_rate":"48000"}]}"#;
        let format = parse_ffprobe_json(json).unwrap().unwrap();
        assert_eq!(format.bit_depth, None);
    }

    #[test]
    fn bits_per_sample_of_zero_is_treated_as_unknown() {
        let json = br#"{"streams":[{"channels":2,"sample_rate":"48000","bits_per_sample":0}]}"#;
        let format = parse_ffprobe_json(json).unwrap().unwrap();
        assert_eq!(format.bit_depth, None);
    }

    #[test]
    fn no_audio_stream_returns_none_not_an_error() {
        let json = br#"{"streams":[]}"#;
        assert_eq!(parse_ffprobe_json(json).unwrap(), None);
    }

    #[test]
    fn malformed_json_is_an_invalid_output_error() {
        let result = parse_ffprobe_json(b"not json");
        assert!(matches!(result, Err(ProbeError::InvalidOutput(_))));
    }

    #[test]
    fn or_fallback_never_panics_on_a_nonexistent_path() {
        let format = probe_audio_format_or_fallback(Path::new("/nonexistent/path/does-not-exist.mov"));
        assert_eq!(format, AudioFormat::FALLBACK);
    }

    /// Exercises the *real* ffprobe binary against real ffmpeg-synthesized
    /// clips -- ignored by default since it depends on local machine setup
    /// (ffmpeg/ffprobe on PATH), same convention as `pipeline.rs`'s
    /// `real_sidecar_*` test. Run explicitly with
    /// `cargo test -- --ignored real_ffprobe`.
    #[test]
    #[ignore]
    fn real_ffprobe_probes_mono_and_stereo_clips_correctly() {
        let dir = std::env::temp_dir().join(format!(
            "spyglass_ffprobe_test_{}",
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();

        let stereo_path = dir.join("stereo.mov");
        let status = Command::new("ffmpeg")
            .args([
                "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
            ])
            .arg(&stereo_path)
            .status()
            .expect("ffmpeg must be on PATH for this test");
        assert!(status.success());

        let mono_path = dir.join("mono.mov");
        let status = Command::new("ffmpeg")
            .args([
                "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le",
            ])
            .arg(&mono_path)
            .status()
            .expect("ffmpeg must be on PATH for this test");
        assert!(status.success());

        let stereo_format = probe_audio_format(&stereo_path).unwrap().unwrap();
        assert_eq!(stereo_format.channels, 2);
        assert_eq!(stereo_format.sample_rate, 48000);
        assert_eq!(stereo_format.bit_depth, Some(16));

        let mono_format = probe_audio_format(&mono_path).unwrap().unwrap();
        assert_eq!(mono_format.channels, 1);
        assert_eq!(mono_format.sample_rate, 44100);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn parses_creation_time_and_normalizes_microsecond_precision_to_millis() {
        // Real shape confirmed against an actual camera file (Section
        // 12/13's sort-by-date bug): ffprobe reports 6-digit microseconds,
        // but `clips.ingested_at` (and every other timestamp column sort-
        // by-date compares against) only ever has 3.
        let json = br#"{"format":{"tags":{"creation_time":"2024-10-22T17:13:32.000000Z"}}}"#;
        assert_eq!(parse_creation_time_json(json).unwrap(), Some("2024-10-22T17:13:32.000Z".to_string()));
    }

    #[test]
    fn missing_creation_time_tag_is_none_not_an_error() {
        // Common for re-muxed/converted files that dropped the original
        // metadata -- callers fall back to mtime, this must not be
        // mistaken for a probe failure.
        let json = br#"{"format":{"tags":{}}}"#;
        assert_eq!(parse_creation_time_json(json).unwrap(), None);
    }

    #[test]
    fn missing_tags_object_entirely_is_none() {
        let json = br#"{"format":{}}"#;
        assert_eq!(parse_creation_time_json(json).unwrap(), None);
    }

    #[test]
    fn unparseable_creation_time_value_is_none_not_an_error() {
        let json = br#"{"format":{"tags":{"creation_time":"not-a-timestamp"}}}"#;
        assert_eq!(parse_creation_time_json(json).unwrap(), None);
    }

    #[test]
    fn recorded_at_for_file_falls_back_to_mtime_when_ffprobe_finds_no_file() {
        // No real video file, and no ffprobe process can succeed against a
        // nonexistent path -- `std::fs::metadata` also fails, so this
        // exercises the full "both signals absent" -> None path.
        assert_eq!(recorded_at_for_file(Path::new("/nonexistent/path/does-not-exist.mov")), None);
    }
}
