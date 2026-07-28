//! Gap-fill pipeline orchestration (Section 6): for a clip lacking shots,
//! shell out to the local Python sidecar for scene-cut detection, keyframe
//! extraction, and CLIP visual embeddings, then write the results into
//! `shots`/`embeddings`. Where a B-Roll Analyzer cache entry overlaps a
//! detected shot, its technical-quality/energy facets get attached too.
//!
//! The Rust core never runs the ML itself (Section 2/19: that stays in a
//! local Python sidecar process) -- this module only shells out to it and
//! persists whatever comes back.

use crate::adapters::broll_analyzer::{self, BrollClipEntry};
use crate::models::Clip;
use rusqlite::{params, Connection};
use serde::Deserialize;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use thiserror::Error;

/// A shared flag a caller can set to request early termination of an
/// in-flight sidecar subprocess -- e.g. the volume watcher (Section 9)
/// noticing the clip's drive just disappeared, rather than waiting out the
/// full timeout for what would otherwise be a hung file handle.
pub type CancelToken = Arc<AtomicBool>;

/// Generous ceiling for one clip's analysis -- long enough for a big file
/// on a slow drive, short enough that a genuinely stuck subprocess (Section
/// 9's "some filesystem drivers hang on I/O for a stale handle instead of
/// erroring immediately") doesn't pin a worker slot forever.
pub const DEFAULT_SIDECAR_TIMEOUT: Duration = Duration::from_secs(15 * 60);

#[derive(Debug, Clone, Deserialize)]
pub struct SidecarShot {
    pub start_tc: f64,
    pub end_tc: f64,
    pub keyframe_filename: String,
    pub embedding: Vec<f32>,
    #[serde(default)]
    pub caption: Option<String>,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub caption_embedding: Option<Vec<f32>>,
    /// This caption's own baseline similarity against a fixed anchor
    /// battery -- see migration 009's doc comment. Absent whenever
    /// `caption_embedding` is, for the same reason (VLM step failed).
    #[serde(default)]
    pub caption_hub_score: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SidecarOutput {
    pub duration_sec: f64,
    #[serde(default)]
    pub frame_rate: Option<f64>,
    pub shots: Vec<SidecarShot>,
}

#[derive(Debug, Error)]
pub enum PipelineError {
    #[error("sidecar process failed to start: {0}")]
    Spawn(std::io::Error),
    #[error("sidecar exited with status {status}: {stderr}")]
    SidecarFailed { status: i32, stderr: String },
    #[error("could not parse sidecar output: {0}")]
    InvalidOutput(serde_json::Error),
    #[error("sidecar exceeded its time budget and was killed")]
    Timeout,
    #[error("sidecar was cancelled (source drive disconnected)")]
    Cancelled,
    #[error("database error: {0}")]
    Db(#[from] rusqlite::Error),
}

/// How to invoke the sidecar -- injectable so tests can point at a cheap
/// stdlib-only fixture script instead of the real `analyze_clip.py` (which
/// needs torch/open_clip and a real video file to do anything useful).
#[derive(Debug, Clone)]
pub struct SidecarCommand {
    pub program: String,
    pub leading_args: Vec<String>,
}

/// Prefers the sidecar's own venv interpreter (with torch/open_clip/etc.
/// already installed) over a bare `python3`, which almost certainly lacks
/// those packages. Shared by every sidecar script this crate shells out
/// to, not just `analyze_clip.py`.
pub fn resolve_venv_python(sidecar_dir: &Path) -> String {
    let venv_python = sidecar_dir.join(".venv/bin/python");
    if venv_python.exists() {
        venv_python.to_string_lossy().into_owned()
    } else {
        "python3".to_string()
    }
}

impl SidecarCommand {
    /// The real sidecar: `<sidecar_dir>/.venv/bin/python <sidecar_dir>/analyze_clip.py`.
    pub fn real(sidecar_dir: &Path) -> Self {
        SidecarCommand {
            program: resolve_venv_python(sidecar_dir),
            leading_args: vec![sidecar_dir.join("analyze_clip.py").to_string_lossy().into_owned()],
        }
    }
}

/// Spawns the sidecar and waits for it with a hard time budget, draining
/// stdout/stderr on background threads the whole time. Piping without
/// draining concurrently would deadlock on any clip with enough shots to
/// fill the OS pipe buffer (a real case here, not a hypothetical -- CLIP
/// embeddings for dozens of shots easily exceed the typical 64KB pipe
/// buffer) since the child would block on write() while we're blocked
/// waiting for it to exit.
#[derive(Debug, PartialEq)]
enum StopReason {
    Timeout,
    Cancelled,
}

fn run_sidecar(
    cmd: &SidecarCommand,
    video_path: &Path,
    keyframe_dir: &Path,
    timeout: Duration,
    cancel: Option<&CancelToken>,
) -> Result<SidecarOutput, PipelineError> {
    let mut child = Command::new(&cmd.program)
        .args(&cmd.leading_args)
        .arg(video_path)
        .arg(keyframe_dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(PipelineError::Spawn)?;

    let mut stdout_pipe = child.stdout.take().expect("stdout was piped");
    let mut stderr_pipe = child.stderr.take().expect("stderr was piped");
    let stdout_thread = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = stdout_pipe.read_to_end(&mut buf);
        buf
    });
    let stderr_thread = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = stderr_pipe.read_to_end(&mut buf);
        buf
    });

    let start = Instant::now();
    let mut stop_reason = None;
    let status = loop {
        match child.try_wait().map_err(PipelineError::Spawn)? {
            Some(status) => break Some(status),
            None => {
                if cancel.is_some_and(|c| c.load(Ordering::Relaxed)) {
                    let _ = child.kill();
                    let _ = child.wait();
                    stop_reason = Some(StopReason::Cancelled);
                    break None;
                }
                if start.elapsed() > timeout {
                    let _ = child.kill();
                    let _ = child.wait();
                    stop_reason = Some(StopReason::Timeout);
                    break None;
                }
                std::thread::sleep(Duration::from_millis(200));
            }
        }
    };

    let stdout = stdout_thread.join().unwrap_or_default();
    let stderr = stderr_thread.join().unwrap_or_default();

    let Some(status) = status else {
        return Err(match stop_reason {
            Some(StopReason::Cancelled) => PipelineError::Cancelled,
            _ => PipelineError::Timeout,
        });
    };

    if !status.success() {
        return Err(PipelineError::SidecarFailed {
            status: status.code().unwrap_or(-1),
            stderr: String::from_utf8_lossy(&stderr).trim().to_string(),
        });
    }

    serde_json::from_slice(&stdout).map_err(PipelineError::InvalidOutput)
}

pub fn write_gap_fill_result(
    conn: &Connection,
    clip: &Clip,
    keyframe_dir: &Path,
    output: &SidecarOutput,
    broll_entry: Option<&BrollClipEntry>,
) -> rusqlite::Result<usize> {
    // One transaction for the whole clip instead of one autocommit per
    // statement: a clip with N shots was previously doing 2-3 fsync'd
    // commits per shot (shot row, visual embedding, optional caption
    // embedding, tags) plus the trailing clip UPDATE. Wrapping it here also
    // makes a mid-write failure atomic -- a shot never ends up half
    // persisted (row present, embedding missing) for a caller to retry into.
    let tx = conn.unchecked_transaction()?;
    let mut shot_count = 0;
    for shot in &output.shots {
        let scores = broll_entry
            .map(|entry| broll_analyzer::aggregate_shot_scores(entry, shot.start_tc, shot.end_tc))
            .unwrap_or_default();
        let keyframe_path = keyframe_dir
            .join(&shot.keyframe_filename)
            .to_string_lossy()
            .into_owned();

        tx.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc, keyframe_path, technical_quality_score, energy_score, caption, caption_hub_score)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                clip.id,
                shot.start_tc,
                shot.end_tc,
                keyframe_path,
                scores.technical_quality,
                scores.energy,
                shot.caption,
                shot.caption_hub_score,
            ],
        )?;
        let shot_id = tx.last_insert_rowid();

        let vector_bytes: Vec<u8> = shot.embedding.iter().flat_map(|f| f.to_le_bytes()).collect();
        tx.execute(
            "INSERT INTO embeddings (shot_id, kind, vector) VALUES (?1, 'visual', ?2)",
            params![shot_id, vector_bytes],
        )?;

        // The gap-fill VLM pass (Section 6 step 5) -- the only source of
        // subject-matter tags anywhere in the pipeline. Absent whenever the
        // VLM step itself failed on this shot; the visual embedding alone
        // still makes it searchable either way.
        if let Some(caption_embedding) = &shot.caption_embedding {
            let caption_bytes: Vec<u8> = caption_embedding.iter().flat_map(|f| f.to_le_bytes()).collect();
            tx.execute(
                "INSERT INTO embeddings (shot_id, kind, vector) VALUES (?1, 'caption', ?2)",
                params![shot_id, caption_bytes],
            )?;
        }
        for tag in &shot.tags {
            tx.execute(
                "INSERT OR IGNORE INTO tags (shot_id, label, source, confidence) VALUES (?1, ?2, 'spyglass_vlm', NULL)",
                params![shot_id, tag],
            )?;
        }

        shot_count += 1;
    }

    tx.execute(
        "UPDATE clips SET duration_sec = ?2, frame_rate = ?3 WHERE id = ?1",
        params![clip.id, output.duration_sec, output.frame_rate],
    )?;

    tx.commit()?;
    Ok(shot_count)
}

/// Shells out to the sidecar and waits for its result -- the slow part of
/// gap-fill (up to `DEFAULT_SIDECAR_TIMEOUT`) -- without touching the
/// database at all. Kept separate from `write_gap_fill_result` so a caller
/// juggling the app's single shared `Connection` (Section 19: one process,
/// one writer, no pool) can run this *before* taking the lock, rather than
/// holding it for the subprocess's entire lifetime. Every other command
/// that needs the connection -- including a "Scan now" click and the next
/// worker's job claim -- would otherwise queue up behind whichever clip is
/// currently being analyzed. `gap_fill_worker.rs`'s worker loop follows
/// exactly this split (via a persistent `AnalyzeWorker` sidecar rather than
/// `SidecarCommand` directly, then a separate `write_gap_fill_result` call);
/// this crate's own tests exercise the same two-step shape via
/// `SidecarCommand`, which is what lets them inject cheap fixture scripts
/// instead of the real `analyze_clip.py`.
pub fn run_sidecar_for_clip(
    clip: &Clip,
    sidecar: &SidecarCommand,
    keyframe_cache_root: &Path,
    timeout: Duration,
    cancel: Option<&CancelToken>,
) -> Result<(PathBuf, SidecarOutput), PipelineError> {
    let keyframe_dir = keyframe_cache_root.join(clip.id.to_string());
    let output = run_sidecar(sidecar, Path::new(&clip.file_path), &keyframe_dir, timeout, cancel)?;
    Ok((keyframe_dir, output))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{self, Db};
    use crate::models::{NewClip, SourceApp};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    /// A stdlib-only fixture standing in for `analyze_clip.py` -- exercises
    /// the same subprocess/JSON contract without needing torch/open_clip
    /// installed just to run this crate's test suite.
    fn write_fixture_sidecar(dir: &Path, shots_json: &str) -> PathBuf {
        let script_path = dir.join("fake_sidecar.py");
        let body = format!(
            r#"import json, sys
from pathlib import Path
video_path, keyframe_dir = sys.argv[1], Path(sys.argv[2])
keyframe_dir.mkdir(parents=True, exist_ok=True)
shots = {shots_json}
for s in shots:
    (keyframe_dir / s["keyframe_filename"]).write_bytes(b"fake-jpeg-bytes")
print(json.dumps({{"duration_sec": 12.0, "frame_rate": 29.97, "shots": shots}}))
"#
        );
        std::fs::write(&script_path, body).unwrap();
        script_path
    }

    fn write_failing_sidecar(dir: &Path) -> PathBuf {
        let script_path = dir.join("failing_sidecar.py");
        std::fs::write(
            &script_path,
            "import sys\nprint('could not open file', file=sys.stderr)\nsys.exit(1)\n",
        )
        .unwrap();
        script_path
    }

    fn scratch_dir(tag: &str) -> PathBuf {
        let n = TMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("spyglass_pipeline_test_{tag}_{n}"));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn python3() -> String {
        "python3".to_string()
    }

    /// Test-only convenience: drives the same two production functions the
    /// real worker loop composes (`run_sidecar_for_clip` then
    /// `write_gap_fill_result` -- see `gap_fill_worker.rs`'s doc comment)
    /// as a single call, so these tests read like the old combined
    /// `run_gap_fill_for_clip` did without that combinator needing to
    /// exist as production API surface it no longer has a caller for.
    fn run_gap_fill_for_clip(
        conn: &Connection,
        clip: &Clip,
        sidecar: &SidecarCommand,
        keyframe_cache_root: &Path,
        broll_entry: Option<&BrollClipEntry>,
        timeout: Duration,
        cancel: Option<&CancelToken>,
    ) -> Result<usize, PipelineError> {
        let (keyframe_dir, output) = run_sidecar_for_clip(clip, sidecar, keyframe_cache_root, timeout, cancel)?;
        write_gap_fill_result(conn, clip, &keyframe_dir, &output, broll_entry).map_err(PipelineError::from)
    }

    #[test]
    fn run_gap_fill_writes_shots_and_embeddings_from_sidecar_output() {
        let dir = scratch_dir("happy_path");
        let script = write_fixture_sidecar(
            &dir,
            r#"[{"start_tc": 0.0, "end_tc": 4.0, "keyframe_filename": "shot_0000.jpg", "embedding": [0.1, 0.2, 0.3]}]"#,
        );

        let db = Db::open_at(&dir.join("index.sqlite")).unwrap();
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/game1.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let sidecar = SidecarCommand {
            program: python3(),
            leading_args: vec![script.to_string_lossy().into_owned()],
        };

        let count = run_gap_fill_for_clip(&conn, &clip, &sidecar, &dir.join("keyframes"), None, Duration::from_secs(10), None).unwrap();
        assert_eq!(count, 1);

        let shot_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM shots WHERE clip_id = ?1", params![clip.id], |r| r.get(0))
            .unwrap();
        assert_eq!(shot_count, 1);

        let embedding_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM embeddings", [], |r| r.get(0))
            .unwrap();
        assert_eq!(embedding_count, 1);

        let duration: Option<f64> = conn
            .query_row("SELECT duration_sec FROM clips WHERE id = ?1", params![clip.id], |r| r.get(0))
            .unwrap();
        assert_eq!(duration, Some(12.0));

        let frame_rate: Option<f64> = conn
            .query_row("SELECT frame_rate FROM clips WHERE id = ?1", params![clip.id], |r| r.get(0))
            .unwrap();
        assert_eq!(frame_rate, Some(29.97));

        let keyframe_path: String = conn
            .query_row("SELECT keyframe_path FROM shots WHERE clip_id = ?1", params![clip.id], |r| r.get(0))
            .unwrap();
        assert!(Path::new(&keyframe_path).exists(), "keyframe file should exist on disk");

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn run_gap_fill_writes_tags_and_caption_embedding_when_present() {
        let dir = scratch_dir("tags_and_caption");
        let script = write_fixture_sidecar(
            &dir,
            r#"[{
                "start_tc": 0.0, "end_tc": 4.0, "keyframe_filename": "shot_0000.jpg",
                "embedding": [0.1, 0.2, 0.3],
                "caption": "A mascot cheers on the sideline.",
                "tags": ["mascot", "cheering", "sideline"],
                "caption_embedding": [0.4, 0.5, 0.6]
            }]"#,
        );

        let db = Db::open_at(&dir.join("index.sqlite")).unwrap();
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/game3.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let sidecar = SidecarCommand {
            program: python3(),
            leading_args: vec![script.to_string_lossy().into_owned()],
        };

        run_gap_fill_for_clip(&conn, &clip, &sidecar, &dir.join("keyframes"), None, Duration::from_secs(10), None).unwrap();

        let tags: Vec<String> = {
            let mut stmt = conn
                .prepare(
                    "SELECT t.label FROM tags t
                     JOIN shots s ON s.id = t.shot_id
                     WHERE s.clip_id = ?1 AND t.source = 'spyglass_vlm'
                     ORDER BY t.label",
                )
                .unwrap();
            stmt.query_map(params![clip.id], |r| r.get(0)).unwrap().map(|r| r.unwrap()).collect()
        };
        assert_eq!(tags, vec!["cheering", "mascot", "sideline"]);

        let embedding_kinds: Vec<String> = {
            let mut stmt = conn
                .prepare(
                    "SELECT e.kind FROM embeddings e
                     JOIN shots s ON s.id = e.shot_id
                     WHERE s.clip_id = ?1 ORDER BY e.kind",
                )
                .unwrap();
            stmt.query_map(params![clip.id], |r| r.get(0)).unwrap().map(|r| r.unwrap()).collect()
        };
        assert_eq!(embedding_kinds, vec!["caption", "visual"]);

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn run_gap_fill_tolerates_missing_caption_and_tags() {
        // A shot where the VLM step itself failed (caption/tags/embedding
        // all absent) must still write a plain visual-only shot rather
        // than erroring the whole clip.
        let dir = scratch_dir("no_vlm_output");
        let script = write_fixture_sidecar(
            &dir,
            r#"[{"start_tc": 0.0, "end_tc": 4.0, "keyframe_filename": "shot_0000.jpg", "embedding": [0.1, 0.2, 0.3]}]"#,
        );

        let db = Db::open_at(&dir.join("index.sqlite")).unwrap();
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/game4.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let sidecar = SidecarCommand {
            program: python3(),
            leading_args: vec![script.to_string_lossy().into_owned()],
        };

        run_gap_fill_for_clip(&conn, &clip, &sidecar, &dir.join("keyframes"), None, Duration::from_secs(10), None).unwrap();

        let tag_count: i64 = conn.query_row("SELECT COUNT(*) FROM tags", [], |r| r.get(0)).unwrap();
        assert_eq!(tag_count, 0);
        let embedding_count: i64 = conn.query_row("SELECT COUNT(*) FROM embeddings", [], |r| r.get(0)).unwrap();
        assert_eq!(embedding_count, 1, "only the visual embedding, no caption embedding");

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn run_gap_fill_attaches_broll_facets_when_cache_overlaps_shot() {
        let dir = scratch_dir("broll_facets");
        let script = write_fixture_sidecar(
            &dir,
            r#"[{"start_tc": 0.0, "end_tc": 1.0, "keyframe_filename": "shot_0000.jpg", "embedding": [0.5]}]"#,
        );

        let db = Db::open_at(&dir.join("index.sqlite")).unwrap();
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/game2.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let broll_entry: BrollClipEntry = serde_json::from_str(
            r#"{"size": 1, "mtime": 1.0, "energy_enabled": true, "samples": [
                {"time_sec": 0.0, "sharpness": 90.0, "exposure": 90.0, "motion_mag": 1.0, "motion_jitter": 1.0, "energy": 50.0}
            ]}"#,
        )
        .unwrap();

        let sidecar = SidecarCommand {
            program: python3(),
            leading_args: vec![script.to_string_lossy().into_owned()],
        };

        run_gap_fill_for_clip(&conn, &clip, &sidecar, &dir.join("keyframes"), Some(&broll_entry), Duration::from_secs(10), None).unwrap();

        let (quality, energy): (Option<f64>, Option<f64>) = conn
            .query_row(
                "SELECT technical_quality_score, energy_score FROM shots WHERE clip_id = ?1",
                params![clip.id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert!(quality.is_some());
        assert!(energy.is_some());

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn run_gap_fill_surfaces_sidecar_failure_without_writing_partial_rows() {
        let dir = scratch_dir("sidecar_failure");
        let script = write_failing_sidecar(&dir);

        let db = Db::open_at(&dir.join("index.sqlite")).unwrap();
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/corrupt.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let sidecar = SidecarCommand {
            program: python3(),
            leading_args: vec![script.to_string_lossy().into_owned()],
        };

        let result = run_gap_fill_for_clip(&conn, &clip, &sidecar, &dir.join("keyframes"), None, Duration::from_secs(10), None);
        match result {
            Err(PipelineError::SidecarFailed { status, stderr }) => {
                assert_eq!(status, 1);
                assert!(stderr.contains("could not open file"));
            }
            other => panic!("expected SidecarFailed, got {other:?}"),
        }

        let shot_count: i64 = conn.query_row("SELECT COUNT(*) FROM shots", [], |r| r.get(0)).unwrap();
        assert_eq!(shot_count, 0);

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn run_gap_fill_kills_and_reports_a_sidecar_that_hangs_past_its_timeout() {
        let dir = scratch_dir("timeout");
        let script_path = dir.join("hanging_sidecar.py");
        // Simulates Section 9's "filesystem driver hangs on a stale handle"
        // case: a sidecar that never exits on its own. The real fix is the
        // timeout+kill in `run_sidecar`, not anything the script does.
        std::fs::write(&script_path, "import time\ntime.sleep(30)\n").unwrap();

        let db = Db::open_at(&dir.join("index.sqlite")).unwrap();
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/stuck.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let sidecar = SidecarCommand {
            program: python3(),
            leading_args: vec![script_path.to_string_lossy().into_owned()],
        };

        let started = Instant::now();
        let result = run_gap_fill_for_clip(
            &conn,
            &clip,
            &sidecar,
            &dir.join("keyframes"),
            None,
            Duration::from_secs(1),
            None,
        );
        assert!(started.elapsed() < Duration::from_secs(10), "must not wait out the full 30s sleep");
        assert!(matches!(result, Err(PipelineError::Timeout)), "expected Timeout, got {result:?}");

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn run_gap_fill_kills_promptly_when_cancel_token_is_set() {
        let dir = scratch_dir("cancel");
        let script_path = dir.join("hanging_sidecar.py");
        std::fs::write(&script_path, "import time\ntime.sleep(30)\n").unwrap();

        let db = Db::open_at(&dir.join("index.sqlite")).unwrap();
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/disconnecting.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let sidecar = SidecarCommand {
            program: python3(),
            leading_args: vec![script_path.to_string_lossy().into_owned()],
        };

        // A generous timeout that would NOT fire on its own within the
        // test's patience budget -- proves cancellation, not the timeout
        // path, is what stops the subprocess here.
        let cancel: CancelToken = Arc::new(AtomicBool::new(false));
        let cancel_for_setter = cancel.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            cancel_for_setter.store(true, Ordering::Relaxed);
        });

        let started = Instant::now();
        let result = run_gap_fill_for_clip(
            &conn,
            &clip,
            &sidecar,
            &dir.join("keyframes"),
            None,
            Duration::from_secs(120),
            Some(&cancel),
        );
        assert!(started.elapsed() < Duration::from_secs(10), "cancellation must pre-empt the 120s timeout");
        assert!(matches!(result, Err(PipelineError::Cancelled)), "expected Cancelled, got {result:?}");

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    /// Exercises the *real* sidecar (ffmpeg + PySceneDetect + torch/open_clip
    /// in `sidecar/.venv`), not the stdlib fixture -- proves the actual
    /// contract end-to-end rather than just the Rust side of it. Ignored by
    /// default since it's slow (loads a real CLIP model) and depends on
    /// local machine setup (ffmpeg on PATH, sidecar venv installed); run
    /// explicitly with `cargo test -- --ignored real_sidecar`.
    #[test]
    #[ignore]
    fn real_sidecar_detects_shots_and_produces_clip_embeddings() {
        let dir = scratch_dir("real_sidecar");

        let clip_path = dir.join("two_shots.mp4");
        let ffmpeg_status = std::process::Command::new("ffmpeg")
            .args([
                "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=15",
                "-f", "lavfi", "-i", "color=c=red:duration=3:size=320x240:rate=15",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            ])
            .arg(&clip_path)
            .status()
            .expect("ffmpeg must be on PATH for this test");
        assert!(ffmpeg_status.success(), "ffmpeg failed to synthesize the test clip");

        let sidecar_dir = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("sidecar");
        assert!(
            sidecar_dir.join(".venv/bin/python").exists(),
            "sidecar venv not found at {sidecar_dir:?} -- run `pip install -r requirements.txt` there first"
        );

        let db = Db::open_at(&dir.join("index.sqlite")).unwrap();
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: clip_path.to_string_lossy().into_owned(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let sidecar = SidecarCommand::real(&sidecar_dir);
        let count = run_gap_fill_for_clip(
            &conn,
            &clip,
            &sidecar,
            &dir.join("keyframes"),
            None,
            Duration::from_secs(120),
            None,
        )
        .unwrap();

        assert_eq!(count, 2, "the synthesized clip has one hard cut -- expect exactly two shots");

        let shots: Vec<(f64, f64, String)> = {
            let mut stmt = conn
                .prepare("SELECT start_tc, end_tc, keyframe_path FROM shots WHERE clip_id = ?1 ORDER BY start_tc")
                .unwrap();
            stmt.query_map(params![clip.id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
                .unwrap()
                .map(|r| r.unwrap())
                .collect()
        };
        assert_eq!(shots.len(), 2);
        for (_, _, keyframe_path) in &shots {
            assert!(Path::new(keyframe_path).exists(), "keyframe {keyframe_path} should exist on disk");
        }

        let embedding_len: usize = conn
            .query_row(
                "SELECT LENGTH(vector) FROM embeddings LIMIT 1",
                [],
                |r| r.get::<_, i64>(0),
            )
            .unwrap() as usize
            / 4; // f32 = 4 bytes each
        assert_eq!(embedding_len, 512, "ViT-B-32 embeddings should be 512-dimensional");

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }
}
