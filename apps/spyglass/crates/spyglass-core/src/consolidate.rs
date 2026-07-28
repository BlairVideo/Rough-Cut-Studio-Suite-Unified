//! Consolidate & Copy export (Section 15): a second pool export mode,
//! alongside the Premiere XML handoff (`xmeml.rs`), that physically copies
//! -- optionally trimmed -- everything in the pool into one self-contained
//! destination folder. Covers what the XML export doesn't: archiving a
//! finished project's source material, handing footage to an outside
//! editor without the whole archive, or building an offline copy that
//! doesn't depend on the archive drives staying mounted.
//!
//! Same "no SQL dependency of its own" shape as `xmeml.rs`'s `XmemlClip` --
//! this module takes already-resolved plain data and is easy to unit-test
//! in isolation; the Tauri command layer resolves pool shots into
//! `ConsolidateClip`s.

use crate::ffprobe::AudioFormat;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fmt::Write as _;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::Command;
use thiserror::Error;

/// One pool shot as the exporter needs it, already resolved from the
/// database.
#[derive(Debug, Clone, PartialEq)]
pub struct ConsolidateClip {
    pub shot_id: i64,
    pub file_path: String,
    pub size_bytes: Option<i64>,
    pub duration_sec: Option<f64>,
    pub frame_rate: Option<f64>,
    /// This clip's real probed audio format (Section 15), carried through
    /// to the manifest and the paired copied-files XMEML export so both
    /// share one probe rather than each re-deriving it.
    pub audio_format: Option<AudioFormat>,
    pub in_seconds: f64,
    pub out_seconds: f64,
    pub tags: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TrimPrecision {
    /// `ffmpeg -c copy` -- fast and lossless, but snaps to the nearest
    /// keyframe, so the actual start point can drift from the requested
    /// in-point depending on the source's GOP structure (Section 15).
    StreamCopy,
    /// Frame-accurate (decodes and re-encodes) but slower and
    /// re-compresses the footage.
    ReEncode,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case")]
pub enum CopyMode {
    /// Copies the entire original file untouched -- no re-encode, no
    /// frame-accuracy risk, preserves all original metadata/audio.
    FullSource,
    /// Copies only the selected in/out range plus a handle on each side.
    /// Given the pool likely mixes codecs from years of different
    /// cameras, stream-copy-with-handle is the more scalable default, but
    /// this stays a visible per-export setting rather than a silent
    /// decision (Section 15) -- it matters whether footage got
    /// recompressed.
    Trimmed { handle_seconds: f64, precision: TrimPrecision },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FolderStructure {
    Flat,
    /// One subfolder per shot's first tag (falling back to "untagged"),
    /// for exports that feed straight into a labeled delivery. Phase 5
    /// scope: a shot with several tags is copied once, under its first
    /// tag, not duplicated into every tag's subfolder -- duplicating a
    /// full-source copy across tags would multiply disk usage for
    /// heavily-tagged footage with no clear "primary" destination anyway.
    SubfolderPerTag,
}

/// One planned copy: a resolved clip plus the collision-safe destination
/// path it will land at.
#[derive(Debug, Clone, PartialEq)]
pub struct ExportPlanEntry {
    pub clip: ConsolidateClip,
    pub destination_path: PathBuf,
}

fn sanitize_path_component(s: &str) -> String {
    s.replace(['/', '\0'], "_")
}

/// Builds the copy plan: every pool shot's original filename is repeated
/// constantly across a multi-year archive (`C0001.MP4` from a dozen
/// different shoots -- Section 15), so copied files are renamed on the way
/// out as `{pool_name}_{sequence}_{original_basename}` rather than relying
/// on original names staying unique at the destination. `sequence` is the
/// shot's 1-based position in the pool's own order, zero-padded to 3
/// digits, so a large pool still sorts naturally at the destination.
pub fn build_export_plan(
    pool_name: &str,
    clips: &[ConsolidateClip],
    destination_root: &Path,
    folder_structure: FolderStructure,
) -> Vec<ExportPlanEntry> {
    let safe_pool_name = sanitize_path_component(pool_name);
    clips
        .iter()
        .enumerate()
        .map(|(i, clip)| {
            let sequence = i + 1;
            let original_basename = Path::new(&clip.file_path)
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_else(|| "clip".to_string());
            let filename = format!("{safe_pool_name}_{sequence:03}_{original_basename}");

            let subdir = match folder_structure {
                FolderStructure::Flat => PathBuf::new(),
                FolderStructure::SubfolderPerTag => {
                    let tag = clip.tags.first().map(|t| sanitize_path_component(t)).unwrap_or_else(|| "untagged".to_string());
                    PathBuf::from(tag)
                }
            };

            ExportPlanEntry { clip: clip.clone(), destination_path: destination_root.join(subdir).join(filename) }
        })
        .collect()
}

/// Total planned copy footprint, for the pre-export size/free-space check
/// (Section 15's "warn rather than fail partway through a multi-hundred-
/// gigabyte copy").
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct SizeEstimate {
    pub file_count: usize,
    pub total_bytes: u64,
}

fn estimate_clip_bytes(clip: &ConsolidateClip, copy_mode: &CopyMode) -> u64 {
    let full_size = clip.size_bytes.unwrap_or(0).max(0) as u64;
    match copy_mode {
        CopyMode::FullSource => full_size,
        CopyMode::Trimmed { handle_seconds, .. } => {
            let Some(duration) = clip.duration_sec.filter(|d| *d > 0.0) else {
                // Can't estimate a selection ratio without the clip's total
                // duration -- fall back to the full size, a safe
                // overestimate rather than under-warning on free space.
                return full_size;
            };
            let selected = (clip.out_seconds - clip.in_seconds + 2.0 * handle_seconds).max(0.0);
            let ratio = (selected / duration).min(1.0);
            ((full_size as f64) * ratio).round() as u64
        }
    }
}

pub fn estimate_export_size(clips: &[ConsolidateClip], copy_mode: &CopyMode) -> SizeEstimate {
    SizeEstimate {
        file_count: clips.len(),
        total_bytes: clips.iter().map(|c| estimate_clip_bytes(c, copy_mode)).sum(),
    }
}

/// Free space available at (or above, if it doesn't exist yet) `path`, via
/// the system `df` -- no new dependency for something the OS already
/// exposes, matching this module's local-binary-first philosophy.
pub fn available_bytes(path: &Path) -> io::Result<u64> {
    let mut probe = path;
    while !probe.exists() {
        match probe.parent() {
            Some(parent) => probe = parent,
            None => break,
        }
    }
    let output = Command::new("df").arg("-Pk").arg(probe).output()?;
    let text = String::from_utf8_lossy(&output.stdout);
    let last_line = text
        .lines()
        .last()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "df produced no output"))?;
    let available_kb: u64 = last_line
        .split_whitespace()
        .nth(3)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, format!("could not parse df output: {last_line}")))?;
    Ok(available_kb * 1024)
}

/// Section 15's "a collision check if the destination folder isn't
/// empty" -- warns before writing into a folder that may already hold
/// unrelated content, distinct from the always-on collision-safe renaming
/// `build_export_plan` already does for the copied files themselves.
pub fn destination_has_existing_files(destination_root: &Path) -> bool {
    destination_root.exists()
        && std::fs::read_dir(destination_root).map(|mut it| it.next().is_some()).unwrap_or(false)
}

/// BLAKE3 checksum of a file's contents, streamed rather than loaded
/// whole into memory (Section 2's "safe handling of huge assets").
pub fn checksum_file(path: &Path) -> io::Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = blake3::Hasher::new();
    let mut buf = [0u8; 1 << 16];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher.finalize().to_hex().to_string())
}

#[derive(Debug, Error)]
pub enum ConsolidateError {
    #[error("could not copy {path}: {source}")]
    Io { path: String, source: io::Error },
    #[error("ffmpeg failed to trim {path}: {stderr}")]
    FfmpegFailed { path: String, stderr: String },
}

fn copy_full(src: &Path, dest: &Path) -> Result<(), ConsolidateError> {
    std::fs::copy(src, dest).map_err(|e| ConsolidateError::Io { path: dest.to_string_lossy().into_owned(), source: e })?;
    Ok(())
}

/// Trims `src` to `[in_seconds - handle, out_seconds + handle]` via
/// ffmpeg. `-ss` placement differs by precision, matching each mode's own
/// real trade-off: before `-i` (fast input-seek) for stream copy, since a
/// copy can't start except at a keyframe regardless of seek placement, so
/// there's nothing to gain from the slower seek; after `-i` (decodes from
/// the true start, discarding frames before it) for re-encode, since only
/// then does the extra decode cost buy real frame accuracy.
fn copy_trimmed(
    src: &Path,
    dest: &Path,
    in_seconds: f64,
    out_seconds: f64,
    handle_seconds: f64,
    precision: TrimPrecision,
) -> Result<(), ConsolidateError> {
    let start = (in_seconds - handle_seconds).max(0.0);
    let duration = (out_seconds - in_seconds + 2.0 * handle_seconds).max(0.0);

    let mut cmd = Command::new(crate::ffmpeg_paths::ffmpeg_path());
    cmd.arg("-y");
    if precision == TrimPrecision::StreamCopy {
        cmd.args(["-ss", &start.to_string()]);
    }
    cmd.arg("-i").arg(src);
    if precision == TrimPrecision::ReEncode {
        cmd.args(["-ss", &start.to_string()]);
    }
    cmd.args(["-t", &duration.to_string()]);
    match precision {
        TrimPrecision::StreamCopy => {
            cmd.args(["-c", "copy"]);
        }
        TrimPrecision::ReEncode => {
            cmd.args(["-c:v", "libx264", "-c:a", "aac"]);
        }
    }
    cmd.arg(dest);

    let output = cmd
        .output()
        .map_err(|e| ConsolidateError::Io { path: src.to_string_lossy().into_owned(), source: e })?;
    if !output.status.success() {
        return Err(ConsolidateError::FfmpegFailed {
            path: src.to_string_lossy().into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).trim().to_string(),
        });
    }
    Ok(())
}

/// Every export writes this companion record alongside the copied media
/// (Section 15) -- what makes a folder handed to an outside vendor
/// self-documenting on its own.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ManifestEntry {
    pub original_path: String,
    pub copied_path: String,
    pub in_seconds: f64,
    pub out_seconds: f64,
    pub frame_rate: Option<f64>,
    pub audio_format: Option<AudioFormat>,
    pub tags: Vec<String>,
    /// BLAKE3 of the *copied* file, always recorded (useful for later
    /// re-verifying a delivered folder hasn't silently corrupted since).
    pub checksum: String,
    /// For full-source copies: whether `checksum` matches the source
    /// file's own BLAKE3 (Section 15's "cheap insurance against a flaky
    /// cable or NAS connection corrupting a file mid-transfer"). Trimmed
    /// copies are genuinely different bytes than their source, so there's
    /// no source/copy match to check there -- this stays `true` for a
    /// trim that completed without ffmpeg erroring, since "verified"
    /// means something different for that mode.
    pub checksum_verified: bool,
}

pub const MANIFEST_JSON_FILENAME: &str = "spyglass_export_manifest.json";
pub const MANIFEST_CSV_FILENAME: &str = "spyglass_export_manifest.csv";

fn csv_escape(s: &str) -> String {
    if s.contains(',') || s.contains('"') || s.contains('\n') {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_string()
    }
}

fn load_existing_manifest(destination_root: &Path) -> Vec<ManifestEntry> {
    std::fs::read_to_string(destination_root.join(MANIFEST_JSON_FILENAME))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

/// Writes both manifest formats (Section 15 calls for "a companion
/// CSV/JSON") -- JSON for round-tripping resumability, CSV for a human (or
/// a vendor) opening it in a spreadsheet.
fn write_manifest(destination_root: &Path, entries: &[ManifestEntry]) -> io::Result<()> {
    let json = serde_json::to_string_pretty(entries).unwrap_or_else(|_| "[]".to_string());
    std::fs::write(destination_root.join(MANIFEST_JSON_FILENAME), json)?;

    let mut csv = String::from("original_path,copied_path,in_seconds,out_seconds,tags,checksum,checksum_verified\n");
    for e in entries {
        let _ = writeln!(
            csv,
            "{},{},{},{},{},{},{}",
            csv_escape(&e.original_path),
            csv_escape(&e.copied_path),
            e.in_seconds,
            e.out_seconds,
            csv_escape(&e.tags.join("|")),
            e.checksum,
            e.checksum_verified,
        );
    }
    std::fs::write(destination_root.join(MANIFEST_CSV_FILENAME), csv)?;
    Ok(())
}

/// Progress callback payload (Section 15: "same pattern as indexing --
/// visible per-file and overall progress").
#[derive(Debug, Clone, Serialize)]
pub struct ExportProgress {
    pub completed: usize,
    pub total: usize,
    pub current_file: String,
}

/// Runs the plan: copies (or trims) each entry, checksums the result, and
/// checkpoints the manifest to disk after *every* file -- not just at the
/// end -- so an export interrupted partway through leaves a manifest an
/// interrupted-then-resumed run can read back (Section 15's resumability
/// requirement, mirroring the same checkpoint-after-each-unit pattern the
/// indexing queue uses in Section 7).
pub fn run_consolidate_export(
    destination_root: &Path,
    plan: &[ExportPlanEntry],
    copy_mode: &CopyMode,
    mut on_progress: impl FnMut(&ExportProgress),
) -> Result<Vec<ManifestEntry>, ConsolidateError> {
    std::fs::create_dir_all(destination_root)
        .map_err(|e| ConsolidateError::Io { path: destination_root.to_string_lossy().into_owned(), source: e })?;

    let existing = load_existing_manifest(destination_root);
    let existing_by_dest: HashMap<&str, &ManifestEntry> =
        existing.iter().map(|e| (e.copied_path.as_str(), e)).collect();

    let total = plan.len();
    let mut manifest = Vec::with_capacity(total);

    for (i, entry) in plan.iter().enumerate() {
        let dest_str = entry.destination_path.to_string_lossy().into_owned();
        on_progress(&ExportProgress { completed: i, total, current_file: dest_str.clone() });

        // Resumability: if a prior (possibly interrupted) run already
        // copied and verified this exact destination file, and it's still
        // there with a matching checksum, skip re-copying it rather than
        // restarting the whole export from file one.
        if let Some(prior) = existing_by_dest.get(dest_str.as_str()) {
            let still_matches = entry.destination_path.exists()
                && prior.checksum_verified
                && checksum_file(&entry.destination_path).map(|c| c == prior.checksum).unwrap_or(false);
            if still_matches {
                manifest.push((*prior).clone());
                continue;
            }
        }

        if let Some(parent) = entry.destination_path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| ConsolidateError::Io { path: parent.to_string_lossy().into_owned(), source: e })?;
        }

        let checksum_verified = match copy_mode {
            CopyMode::FullSource => {
                copy_full(Path::new(&entry.clip.file_path), &entry.destination_path)?;
                let source_checksum = checksum_file(Path::new(&entry.clip.file_path))
                    .map_err(|e| ConsolidateError::Io { path: entry.clip.file_path.clone(), source: e })?;
                let copy_checksum = checksum_file(&entry.destination_path)
                    .map_err(|e| ConsolidateError::Io { path: dest_str.clone(), source: e })?;
                let verified = source_checksum == copy_checksum;
                manifest.push(ManifestEntry {
                    original_path: entry.clip.file_path.clone(),
                    copied_path: dest_str.clone(),
                    in_seconds: entry.clip.in_seconds,
                    out_seconds: entry.clip.out_seconds,
                    frame_rate: entry.clip.frame_rate,
                    audio_format: entry.clip.audio_format,
                    tags: entry.clip.tags.clone(),
                    checksum: copy_checksum,
                    checksum_verified: verified,
                });
                write_manifest(destination_root, &manifest)
                    .map_err(|e| ConsolidateError::Io { path: destination_root.to_string_lossy().into_owned(), source: e })?;
                continue;
            }
            CopyMode::Trimmed { handle_seconds, precision } => {
                copy_trimmed(
                    Path::new(&entry.clip.file_path),
                    &entry.destination_path,
                    entry.clip.in_seconds,
                    entry.clip.out_seconds,
                    *handle_seconds,
                    *precision,
                )?;
                true
            }
        };

        let copy_checksum = checksum_file(&entry.destination_path)
            .map_err(|e| ConsolidateError::Io { path: dest_str.clone(), source: e })?;
        manifest.push(ManifestEntry {
            original_path: entry.clip.file_path.clone(),
            copied_path: dest_str,
            in_seconds: entry.clip.in_seconds,
            out_seconds: entry.clip.out_seconds,
            frame_rate: entry.clip.frame_rate,
            audio_format: entry.clip.audio_format,
            tags: entry.clip.tags.clone(),
            checksum: copy_checksum,
            checksum_verified,
        });
        write_manifest(destination_root, &manifest)
            .map_err(|e| ConsolidateError::Io { path: destination_root.to_string_lossy().into_owned(), source: e })?;
    }

    on_progress(&ExportProgress { completed: total, total, current_file: String::new() });
    Ok(manifest)
}

/// Builds a second XMEML sequence (Section 15's "paired XML pointing at
/// copied files") whose clips reference the freshly copied files instead
/// of the original archive paths -- useful for building an edit that
/// doesn't require the archive drives to stay mounted. Reuses the same
/// `xmeml` module the Premiere export already relies on rather than a
/// second XML writer.
pub fn manifest_to_xmeml_clips(manifest: &[ManifestEntry]) -> Vec<crate::xmeml::XmemlClip> {
    manifest
        .iter()
        .map(|entry| crate::xmeml::XmemlClip {
            file_path: entry.copied_path.clone(),
            name: Path::new(&entry.copied_path)
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_else(|| entry.copied_path.clone()),
            frame_rate: entry.frame_rate,
            audio_format: entry.audio_format,
            in_seconds: 0.0,
            out_seconds: entry.out_seconds - entry.in_seconds,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_clip(path: &str, size_bytes: i64, duration_sec: f64, in_s: f64, out_s: f64, tags: &[&str]) -> ConsolidateClip {
        ConsolidateClip {
            shot_id: 1,
            file_path: path.to_string(),
            size_bytes: Some(size_bytes),
            duration_sec: Some(duration_sec),
            frame_rate: Some(29.97),
            audio_format: None,
            in_seconds: in_s,
            out_seconds: out_s,
            tags: tags.iter().map(|s| s.to_string()).collect(),
        }
    }

    #[test]
    fn build_export_plan_renames_with_pool_name_sequence_and_original_basename() {
        let clips = vec![
            sample_clip("/Volumes/Archive/shoot1/C0001.MP4", 1000, 10.0, 0.0, 4.0, &[]),
            sample_clip("/Volumes/Archive/shoot2/C0001.MP4", 1000, 10.0, 0.0, 4.0, &[]),
        ];
        let plan = build_export_plan("Fall Promo", &clips, Path::new("/tmp/out"), FolderStructure::Flat);

        assert_eq!(plan[0].destination_path, Path::new("/tmp/out/Fall Promo_001_C0001.MP4"));
        assert_eq!(plan[1].destination_path, Path::new("/tmp/out/Fall Promo_002_C0001.MP4"));
        assert_ne!(plan[0].destination_path, plan[1].destination_path, "repeated camera filenames must not collide");
    }

    #[test]
    fn build_export_plan_subfolder_per_tag_uses_first_tag_or_untagged() {
        let clips = vec![
            sample_clip("/Volumes/Archive/a.mov", 100, 5.0, 0.0, 1.0, &["mascot", "cheering"]),
            sample_clip("/Volumes/Archive/b.mov", 100, 5.0, 0.0, 1.0, &[]),
        ];
        let plan = build_export_plan("Pool", &clips, Path::new("/tmp/out"), FolderStructure::SubfolderPerTag);

        assert_eq!(plan[0].destination_path, Path::new("/tmp/out/mascot/Pool_001_a.mov"));
        assert_eq!(plan[1].destination_path, Path::new("/tmp/out/untagged/Pool_002_b.mov"));
    }

    #[test]
    fn estimate_export_size_full_source_sums_whole_file_sizes() {
        let clips = vec![
            sample_clip("/a.mov", 1_000_000, 60.0, 0.0, 4.0, &[]),
            sample_clip("/b.mov", 2_000_000, 60.0, 0.0, 4.0, &[]),
        ];
        let estimate = estimate_export_size(&clips, &CopyMode::FullSource);
        assert_eq!(estimate, SizeEstimate { file_count: 2, total_bytes: 3_000_000 });
    }

    #[test]
    fn estimate_export_size_trimmed_scales_by_selected_duration_ratio() {
        // 100MB file, 100s total, selecting 4s + 1s handle each side = 6s
        // out of 100s -> ~6% of the file's size.
        let clips = vec![sample_clip("/a.mov", 100_000_000, 100.0, 10.0, 14.0, &[])];
        let estimate = estimate_export_size(
            &clips,
            &CopyMode::Trimmed { handle_seconds: 1.0, precision: TrimPrecision::StreamCopy },
        );
        assert_eq!(estimate.total_bytes, 6_000_000);
    }

    #[test]
    fn estimate_export_size_falls_back_to_full_size_without_known_duration() {
        let clip = ConsolidateClip {
            shot_id: 1,
            file_path: "/a.mov".to_string(),
            size_bytes: Some(500),
            duration_sec: None,
            frame_rate: None,
            audio_format: None,
            in_seconds: 0.0,
            out_seconds: 1.0,
            tags: vec![],
        };
        let estimate = estimate_export_size(
            &[clip],
            &CopyMode::Trimmed { handle_seconds: 1.0, precision: TrimPrecision::StreamCopy },
        );
        assert_eq!(estimate.total_bytes, 500);
    }

    #[test]
    fn destination_has_existing_files_distinguishes_empty_missing_and_populated() {
        let dir = std::env::temp_dir().join(format!(
            "spyglass_consolidate_test_collision_{}",
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
        ));

        assert!(!destination_has_existing_files(&dir), "a folder that doesn't exist yet has no existing files");

        std::fs::create_dir_all(&dir).unwrap();
        assert!(!destination_has_existing_files(&dir), "an empty folder has no existing files");

        std::fs::write(dir.join("preexisting.txt"), b"hello").unwrap();
        assert!(destination_has_existing_files(&dir));

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn checksum_file_matches_for_identical_content_and_differs_for_different_content() {
        let dir = std::env::temp_dir().join(format!(
            "spyglass_consolidate_test_checksum_{}",
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();

        let a = dir.join("a.bin");
        let b = dir.join("b.bin");
        let c = dir.join("c.bin");
        std::fs::write(&a, b"some clip bytes").unwrap();
        std::fs::write(&b, b"some clip bytes").unwrap();
        std::fs::write(&c, b"different bytes").unwrap();

        assert_eq!(checksum_file(&a).unwrap(), checksum_file(&b).unwrap());
        assert_ne!(checksum_file(&a).unwrap(), checksum_file(&c).unwrap());

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn available_bytes_returns_a_plausible_value_for_the_temp_dir() {
        let bytes = available_bytes(&std::env::temp_dir()).unwrap();
        assert!(bytes > 0, "the temp filesystem should report some free space");
    }

    fn scratch_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "spyglass_consolidate_test_{tag}_{}",
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn run_consolidate_export_full_source_copies_checksums_and_writes_manifest() {
        let dir = scratch_dir("full_source");
        let source_dir = dir.join("source");
        std::fs::create_dir_all(&source_dir).unwrap();
        let source_path = source_dir.join("C0001.MP4");
        std::fs::write(&source_path, b"fake video bytes").unwrap();

        let dest_dir = dir.join("dest");
        let clip = ConsolidateClip {
            shot_id: 1,
            file_path: source_path.to_string_lossy().into_owned(),
            size_bytes: Some(17),
            duration_sec: Some(10.0),
            frame_rate: Some(29.97),
            audio_format: None,
            in_seconds: 1.0,
            out_seconds: 4.0,
            tags: vec!["mascot".to_string()],
        };
        let plan = build_export_plan("Pool", &[clip], &dest_dir, FolderStructure::Flat);

        let mut progress_calls = 0;
        let manifest = run_consolidate_export(&dest_dir, &plan, &CopyMode::FullSource, |_p| progress_calls += 1).unwrap();

        assert_eq!(manifest.len(), 1);
        assert!(manifest[0].checksum_verified);
        assert_eq!(manifest[0].tags, vec!["mascot"]);
        assert!(progress_calls >= 2, "expects at least a start and final progress call");

        let copied_bytes = std::fs::read(&plan[0].destination_path).unwrap();
        assert_eq!(copied_bytes, b"fake video bytes");

        assert!(dest_dir.join(MANIFEST_JSON_FILENAME).exists());
        assert!(dest_dir.join(MANIFEST_CSV_FILENAME).exists());
        let csv = std::fs::read_to_string(dest_dir.join(MANIFEST_CSV_FILENAME)).unwrap();
        assert!(csv.contains("mascot"));

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn run_consolidate_export_resumes_without_recopying_an_already_verified_file() {
        let dir = scratch_dir("resume");
        let source_dir = dir.join("source");
        std::fs::create_dir_all(&source_dir).unwrap();
        let source_path = source_dir.join("clip.mov");
        std::fs::write(&source_path, b"original content").unwrap();

        let dest_dir = dir.join("dest");
        let clip = ConsolidateClip {
            shot_id: 1,
            file_path: source_path.to_string_lossy().into_owned(),
            size_bytes: Some(16),
            duration_sec: Some(10.0),
            frame_rate: None,
            audio_format: None,
            in_seconds: 0.0,
            out_seconds: 2.0,
            tags: vec![],
        };
        let plan = build_export_plan("Pool", &[clip], &dest_dir, FolderStructure::Flat);

        run_consolidate_export(&dest_dir, &plan, &CopyMode::FullSource, |_| {}).unwrap();

        // Simulate the source having changed on disk after the first
        // (interrupted) run -- if the second run re-copied unconditionally
        // this would show up in the destination file's contents.
        std::fs::write(&source_path, b"CHANGED - should not be recopied").unwrap();

        let second_manifest = run_consolidate_export(&dest_dir, &plan, &CopyMode::FullSource, |_| {}).unwrap();
        let copied_bytes = std::fs::read(&plan[0].destination_path).unwrap();
        assert_eq!(copied_bytes, b"original content", "an already-verified destination file must not be recopied");
        assert_eq!(second_manifest.len(), 1);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn run_consolidate_export_recopies_when_the_destination_file_was_removed() {
        let dir = scratch_dir("recopy_after_removal");
        let source_dir = dir.join("source");
        std::fs::create_dir_all(&source_dir).unwrap();
        let source_path = source_dir.join("clip.mov");
        std::fs::write(&source_path, b"content v1").unwrap();

        let dest_dir = dir.join("dest");
        let clip = ConsolidateClip {
            shot_id: 1,
            file_path: source_path.to_string_lossy().into_owned(),
            size_bytes: Some(10),
            duration_sec: Some(10.0),
            frame_rate: None,
            audio_format: None,
            in_seconds: 0.0,
            out_seconds: 2.0,
            tags: vec![],
        };
        let plan = build_export_plan("Pool", &[clip], &dest_dir, FolderStructure::Flat);

        run_consolidate_export(&dest_dir, &plan, &CopyMode::FullSource, |_| {}).unwrap();
        std::fs::remove_file(&plan[0].destination_path).unwrap();
        std::fs::write(&source_path, b"content v2").unwrap();

        run_consolidate_export(&dest_dir, &plan, &CopyMode::FullSource, |_| {}).unwrap();
        let copied_bytes = std::fs::read(&plan[0].destination_path).unwrap();
        assert_eq!(copied_bytes, b"content v2", "a missing destination file must be recopied from the current source");

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn manifest_to_xmeml_clips_points_at_copied_paths_with_zero_based_in_out() {
        let manifest = vec![ManifestEntry {
            original_path: "/Volumes/Archive/original.mov".to_string(),
            copied_path: "/Users/editor/Delivery/Pool_001_original.mov".to_string(),
            in_seconds: 10.0,
            out_seconds: 14.0,
            frame_rate: Some(29.97),
            audio_format: None,
            tags: vec![],
            checksum: "abc".to_string(),
            checksum_verified: true,
        }];
        let clips = manifest_to_xmeml_clips(&manifest);
        assert_eq!(clips.len(), 1);
        assert_eq!(clips[0].file_path, "/Users/editor/Delivery/Pool_001_original.mov");
        assert_eq!(clips[0].in_seconds, 0.0);
        assert_eq!(clips[0].out_seconds, 4.0);
    }

    /// Exercises the real ffmpeg stream-copy trim path -- ignored by
    /// default since it depends on local machine setup (ffmpeg on PATH),
    /// same convention as `pipeline.rs`'s `real_sidecar_*` test. Run
    /// explicitly with `cargo test -- --ignored real_ffmpeg_trim`.
    #[test]
    #[ignore]
    fn real_ffmpeg_trim_produces_a_shorter_playable_file() {
        let dir = scratch_dir("real_trim");
        let source_path = dir.join("source.mp4");
        let status = Command::new("ffmpeg")
            .args(["-y", "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=15", "-c:v", "libx264", "-pix_fmt", "yuv420p"])
            .arg(&source_path)
            .status()
            .expect("ffmpeg must be on PATH for this test");
        assert!(status.success());

        let dest_dir = dir.join("dest");
        let clip = ConsolidateClip {
            shot_id: 1,
            file_path: source_path.to_string_lossy().into_owned(),
            size_bytes: std::fs::metadata(&source_path).ok().map(|m| m.len() as i64),
            duration_sec: Some(6.0),
            frame_rate: Some(15.0),
            audio_format: None,
            in_seconds: 2.0,
            out_seconds: 4.0,
            tags: vec![],
        };
        let plan = build_export_plan("Pool", &[clip], &dest_dir, FolderStructure::Flat);

        let manifest = run_consolidate_export(
            &dest_dir,
            &plan,
            &CopyMode::Trimmed { handle_seconds: 0.5, precision: TrimPrecision::StreamCopy },
            |_| {},
        )
        .unwrap();

        assert_eq!(manifest.len(), 1);
        assert!(plan[0].destination_path.exists());
        let trimmed_size = std::fs::metadata(&plan[0].destination_path).unwrap().len();
        let original_size = std::fs::metadata(&source_path).unwrap().len();
        assert!(trimmed_size < original_size, "a ~3s trim of a 6s clip should produce a visibly smaller file");

        std::fs::remove_dir_all(&dir).ok();
    }
}
