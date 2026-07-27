//! Read-only adapter for Local Interview Transcriber's `.ivt-cache.json`
//! sidecars, written next to each source video. No content hash is present
//! in this schema (its own staleness check is `(video_size, video_mtime)`),
//! so Spyglass computes its own BLAKE3 for change-detection/dedup rather
//! than relying on anything from this file.

use super::{AdapterError, IVT_CACHE_SUFFIX};
use crate::models::NewTranscriptSegment;
use serde::Deserialize;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

#[derive(Debug, Clone, Deserialize)]
pub struct IvtSegment {
    pub start: f64,
    pub end: f64,
    pub text: String,
    pub speaker: String,
    pub avg_logprob: Option<f64>,
    pub no_speech_prob: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct IvtCacheSidecar {
    pub path: String,
    #[allow(dead_code)]
    pub name: Option<String>,
    #[allow(dead_code)]
    pub video_size: Option<i64>,
    #[allow(dead_code)]
    pub video_mtime: Option<f64>,
    #[allow(dead_code)]
    pub speakers: Option<Vec<String>>,
    pub segments: Vec<IvtSegment>,
    #[serde(default)]
    pub speaker_labels: HashMap<String, String>,
    #[serde(default)]
    pub excluded_speakers: Vec<String>,
}

/// Recursively finds every `*.ivt-cache.json` sidecar under `root`.
pub fn discover_sidecars(root: &Path) -> Vec<PathBuf> {
    WalkDir::new(root)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file())
        .filter(|e| {
            e.file_name()
                .to_string_lossy()
                .ends_with(IVT_CACHE_SUFFIX)
        })
        .map(|e| e.into_path())
        .collect()
}

pub fn parse_sidecar(path: &Path) -> Result<IvtCacheSidecar, AdapterError> {
    let bytes = std::fs::read(path).map_err(|source| AdapterError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    serde_json::from_slice(&bytes).map_err(|source| AdapterError::Json {
        path: path.to_path_buf(),
        source,
    })
}

/// The source video path a sidecar describes -- the sidecar's own `path`
/// field is authoritative rather than re-deriving it by stripping the
/// suffix off the sidecar's own filename.
pub fn source_video_path(sidecar: &IvtCacheSidecar) -> &str {
    &sidecar.path
}

/// Maps `segments` into `NewTranscriptSegment` rows, respecting
/// `excluded_speakers` (don't import segments for a speaker the editor
/// explicitly hid) and applying `speaker_labels` (use the display name,
/// not the raw diarization id) -- per Section 3's adapter implication.
pub fn to_transcript_segments(sidecar: &IvtCacheSidecar, clip_id: i64) -> Vec<NewTranscriptSegment> {
    sidecar
        .segments
        .iter()
        .filter(|seg| !sidecar.excluded_speakers.contains(&seg.speaker))
        .map(|seg| NewTranscriptSegment {
            clip_id,
            start_tc: seg.start,
            end_tc: seg.end,
            speaker: Some(
                sidecar
                    .speaker_labels
                    .get(&seg.speaker)
                    .cloned()
                    .unwrap_or_else(|| seg.speaker.clone()),
            ),
            text: seg.text.clone(),
            avg_logprob: seg.avg_logprob,
            no_speech_prob: seg.no_speech_prob,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_json() -> &'static str {
        r#"{
            "path": "/Volumes/Interviews/coach_smith.mov",
            "name": "coach_smith.mov",
            "video_size": 123456,
            "video_mtime": 1732000000.0,
            "speakers": ["SPEAKER_00", "SPEAKER_01"],
            "segments": [
                {"start": 0.0, "end": 4.2, "text": "our mascot got the crowd cheering", "speaker": "SPEAKER_00", "avg_logprob": -0.2, "no_speech_prob": 0.01},
                {"start": 4.2, "end": 9.0, "text": "off the record comment", "speaker": "SPEAKER_01", "avg_logprob": -0.4, "no_speech_prob": 0.05}
            ],
            "speaker_labels": {"SPEAKER_00": "Coach Smith"},
            "excluded_speakers": ["SPEAKER_01"]
        }"#
    }

    #[test]
    fn parses_exact_schema_fields() {
        let sidecar: IvtCacheSidecar = serde_json::from_str(sample_json()).unwrap();
        assert_eq!(sidecar.path, "/Volumes/Interviews/coach_smith.mov");
        assert_eq!(sidecar.segments.len(), 2);
        assert_eq!(sidecar.speaker_labels.get("SPEAKER_00").unwrap(), "Coach Smith");
        assert_eq!(sidecar.excluded_speakers, vec!["SPEAKER_01"]);
    }

    #[test]
    fn excluded_speakers_are_dropped_and_labels_are_applied() {
        let sidecar: IvtCacheSidecar = serde_json::from_str(sample_json()).unwrap();
        let rows = to_transcript_segments(&sidecar, 42);

        assert_eq!(rows.len(), 1, "the SPEAKER_01 segment must be excluded");
        assert_eq!(rows[0].clip_id, 42);
        assert_eq!(rows[0].speaker.as_deref(), Some("Coach Smith"));
        assert_eq!(rows[0].text, "our mascot got the crowd cheering");
        assert_eq!(rows[0].avg_logprob, Some(-0.2));
    }

    #[test]
    fn falls_back_to_raw_speaker_id_when_no_label_assigned() {
        let mut sidecar: IvtCacheSidecar = serde_json::from_str(sample_json()).unwrap();
        sidecar.excluded_speakers.clear();
        let rows = to_transcript_segments(&sidecar, 1);
        let unlabeled = rows.iter().find(|r| r.text == "off the record comment").unwrap();
        assert_eq!(unlabeled.speaker.as_deref(), Some("SPEAKER_01"));
    }

    #[test]
    fn discover_sidecars_finds_nested_ivt_cache_files() {
        let dir = std::env::temp_dir().join(format!(
            "spyglass_ivt_discover_{}",
            std::process::id()
        ));
        std::fs::create_dir_all(dir.join("sub")).unwrap();
        std::fs::write(dir.join("clip.mov.ivt-cache.json"), sample_json()).unwrap();
        std::fs::write(dir.join("sub").join("clip2.mp4.ivt-cache.json"), sample_json()).unwrap();
        std::fs::write(dir.join("clip.mov"), b"not json").unwrap();

        let found = discover_sidecars(&dir);
        assert_eq!(found.len(), 2);

        std::fs::remove_dir_all(&dir).ok();
    }
}
