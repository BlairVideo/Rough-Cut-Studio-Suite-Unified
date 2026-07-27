//! Read-only adapter for B-Roll Analyzer's per-folder `.broll_analyzer_cache.json`.
//!
//! Verified against the actual code: this cache stores only the
//! settings-independent raw per-frame samples (`sharpness`, `exposure`,
//! `motion_mag`, `motion_jitter`, `energy`) keyed by `(size, mtime)` --
//! *not* a precomputed technical-quality/best-window score, since those
//! depend on settings (window length, segments-per-clip, energy weight)
//! that can change between runs without invalidating the cache. B-Roll
//! Analyzer recomputes them on load via its own `rescore_clip`.
//!
//! Spyglass doesn't import or replicate that function. Instead this
//! adapter aggregates its own simple `technical_quality_score`/
//! `energy_score` per shot directly from the raw samples that fall inside
//! that shot's time range -- a numeric facet (Section 3/5), not a tag, and
//! not required to match B-Roll Analyzer's own UI numbers exactly. The
//! per-component weights below mirror B-Roll Analyzer's own published
//! constants purely so "technical quality" means the same thing in both
//! apps' vocabularies; nothing here calls into or depends on its code.

use super::{AdapterError, BROLL_CACHE_FILENAME};
use serde::Deserialize;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

const WEIGHT_SHARPNESS: f64 = 0.40;
const WEIGHT_EXPOSURE: f64 = 0.25;
const WEIGHT_STABILITY: f64 = 0.35;

#[derive(Debug, Clone, Deserialize)]
pub struct FrameSample {
    pub time_sec: f64,
    pub sharpness: f64,
    pub exposure: f64,
    #[allow(dead_code)]
    pub motion_mag: f64,
    pub motion_jitter: f64,
    #[serde(default)]
    pub energy: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct BrollClipEntry {
    #[allow(dead_code)]
    pub size: u64,
    #[allow(dead_code)]
    pub mtime: f64,
    pub samples: Vec<FrameSample>,
    #[serde(default)]
    pub energy_enabled: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct BrollCache {
    #[allow(dead_code)]
    pub version: u32,
    pub clips: HashMap<String, BrollClipEntry>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct ShotScores {
    pub technical_quality: Option<f64>,
    pub energy: Option<f64>,
}

/// Recursively finds every `.broll_analyzer_cache.json` file under `root`
/// (one per analyzed folder, not one per clip).
pub fn discover_cache_files(root: &Path) -> Vec<PathBuf> {
    WalkDir::new(root)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file())
        .filter(|e| e.file_name() == BROLL_CACHE_FILENAME)
        .map(|e| e.into_path())
        .collect()
}

pub fn parse_cache(path: &Path) -> Result<BrollCache, AdapterError> {
    let bytes = std::fs::read(path).map_err(|source| AdapterError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    serde_json::from_slice(&bytes).map_err(|source| AdapterError::Json {
        path: path.to_path_buf(),
        source,
    })
}

/// Resolves a cache entry's relative path key into an absolute path,
/// relative to the folder the cache file itself lives in.
pub fn clip_absolute_path(cache_file_path: &Path, rel_path: &str) -> PathBuf {
    cache_file_path
        .parent()
        .map(|dir| dir.join(rel_path))
        .unwrap_or_else(|| PathBuf::from(rel_path))
}

/// Looks for a `.broll_analyzer_cache.json` next to `clip_path` and returns
/// its entry for this specific clip, if any -- the runtime counterpart to
/// Section 6 step 3 ("where a B-Roll Analyzer best-segment overlaps a
/// detected shot, its scores get attached"). Returns `None` on any failure
/// (no cache in that folder, clip not in it, unreadable/invalid JSON) --
/// the gap-fill pipeline treats a missing B-Roll Analyzer facet as normal,
/// not an error.
pub fn find_entry_for_clip_path(clip_path: &Path) -> Option<BrollClipEntry> {
    let parent = clip_path.parent()?;
    let cache_path = parent.join(BROLL_CACHE_FILENAME);
    let cache = parse_cache(&cache_path).ok()?;
    let file_name = clip_path.file_name()?.to_string_lossy().into_owned();
    cache.clips.get(&file_name).cloned()
}

fn normalize(value: f64, lo: f64, hi: f64) -> f64 {
    if hi <= lo {
        return 0.0;
    }
    ((value - lo) / (hi - lo)).clamp(0.0, 1.0) * 100.0
}

/// Aggregates a shot's technical-quality and energy facets from whichever
/// raw samples fall within `[start_tc, end_tc)`. The stability component's
/// jitter ceiling is taken from the whole clip's samples (not just this
/// shot's window) so a shot's score stays comparable to its siblings in
/// the same clip, mirroring how B-Roll Analyzer's own per-clip rescoring
/// works.
pub fn aggregate_shot_scores(entry: &BrollClipEntry, start_tc: f64, end_tc: f64) -> ShotScores {
    let jitter_ceiling = entry
        .samples
        .iter()
        .map(|s| s.motion_jitter)
        .fold(0.0_f64, f64::max)
        .max(1e-6);

    let in_range: Vec<&FrameSample> = entry
        .samples
        .iter()
        .filter(|s| s.time_sec >= start_tc && s.time_sec < end_tc)
        .collect();

    if in_range.is_empty() {
        return ShotScores::default();
    }

    let technical_quality = in_range
        .iter()
        .map(|s| {
            let stability = 100.0 - normalize(s.motion_jitter, 0.0, jitter_ceiling);
            s.sharpness * WEIGHT_SHARPNESS + s.exposure * WEIGHT_EXPOSURE + stability * WEIGHT_STABILITY
        })
        .sum::<f64>()
        / in_range.len() as f64;

    let energy = if entry.energy_enabled {
        Some(in_range.iter().map(|s| s.energy).sum::<f64>() / in_range.len() as f64)
    } else {
        None
    };

    ShotScores {
        technical_quality: Some(technical_quality),
        energy,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_json() -> &'static str {
        r#"{
            "version": 1,
            "clips": {
                "C0001.MP4": {
                    "size": 50000,
                    "mtime": 1732000000.0,
                    "energy_enabled": true,
                    "samples": [
                        {"time_sec": 0.0, "sharpness": 80.0, "exposure": 90.0, "motion_mag": 1.0, "motion_jitter": 2.0, "energy": 30.0},
                        {"time_sec": 0.5, "sharpness": 85.0, "exposure": 92.0, "motion_mag": 1.2, "motion_jitter": 3.0, "energy": 40.0},
                        {"time_sec": 5.0, "sharpness": 20.0, "exposure": 40.0, "motion_mag": 8.0, "motion_jitter": 9.0, "energy": 10.0}
                    ]
                }
            }
        }"#
    }

    #[test]
    fn parses_exact_schema_and_ignores_unknown_fields() {
        let cache: BrollCache = serde_json::from_str(sample_json()).unwrap();
        assert_eq!(cache.version, 1);
        let entry = cache.clips.get("C0001.MP4").unwrap();
        assert_eq!(entry.samples.len(), 3);
        assert!(entry.energy_enabled);
    }

    #[test]
    fn aggregates_only_samples_within_shot_range() {
        let cache: BrollCache = serde_json::from_str(sample_json()).unwrap();
        let entry = cache.clips.get("C0001.MP4").unwrap();

        // Shot covering the first two (good-quality, low-jitter) samples only.
        let scores = aggregate_shot_scores(entry, 0.0, 1.0);
        assert!(scores.technical_quality.unwrap() > 70.0);
        assert!((scores.energy.unwrap() - 35.0).abs() < 0.01);

        // Shot covering only the poor-quality, high-jitter sample.
        let poor = aggregate_shot_scores(entry, 4.0, 6.0);
        assert!(poor.technical_quality.unwrap() < scores.technical_quality.unwrap());
    }

    #[test]
    fn returns_none_scores_when_no_samples_fall_in_range() {
        let cache: BrollCache = serde_json::from_str(sample_json()).unwrap();
        let entry = cache.clips.get("C0001.MP4").unwrap();
        let scores = aggregate_shot_scores(entry, 100.0, 110.0);
        assert_eq!(scores, ShotScores::default());
    }

    #[test]
    fn energy_is_none_when_energy_analysis_was_disabled() {
        let mut cache: BrollCache = serde_json::from_str(sample_json()).unwrap();
        cache.clips.get_mut("C0001.MP4").unwrap().energy_enabled = false;
        let entry = cache.clips.get("C0001.MP4").unwrap();
        let scores = aggregate_shot_scores(entry, 0.0, 1.0);
        assert!(scores.technical_quality.is_some());
        assert!(scores.energy.is_none());
    }

    #[test]
    fn clip_absolute_path_resolves_relative_to_cache_folder() {
        let cache_path = Path::new("/Volumes/Archive/Fall2025/.broll_analyzer_cache.json");
        let abs = clip_absolute_path(cache_path, "C0001.MP4");
        assert_eq!(abs, PathBuf::from("/Volumes/Archive/Fall2025/C0001.MP4"));
    }

    #[test]
    fn find_entry_for_clip_path_locates_sibling_cache_and_matching_entry() {
        let dir = std::env::temp_dir().join(format!(
            "spyglass_broll_find_entry_{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(BROLL_CACHE_FILENAME), sample_json()).unwrap();

        let clip_path = dir.join("C0001.MP4");
        let entry = find_entry_for_clip_path(&clip_path);
        assert!(entry.is_some());
        assert_eq!(entry.unwrap().samples.len(), 3);

        let missing = find_entry_for_clip_path(&dir.join("C9999.MP4"));
        assert!(missing.is_none());

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn find_entry_for_clip_path_returns_none_when_no_cache_in_folder() {
        let dir = std::env::temp_dir().join(format!(
            "spyglass_broll_no_cache_{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let entry = find_entry_for_clip_path(&dir.join("C0001.MP4"));
        assert!(entry.is_none());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn discover_cache_files_finds_per_folder_caches() {
        let dir = std::env::temp_dir().join(format!(
            "spyglass_broll_discover_{}",
            std::process::id()
        ));
        std::fs::create_dir_all(dir.join("sub")).unwrap();
        std::fs::write(dir.join(BROLL_CACHE_FILENAME), sample_json()).unwrap();
        std::fs::write(dir.join("sub").join(BROLL_CACHE_FILENAME), sample_json()).unwrap();

        let found = discover_cache_files(&dir);
        assert_eq!(found.len(), 2);

        std::fs::remove_dir_all(&dir).ok();
    }
}
