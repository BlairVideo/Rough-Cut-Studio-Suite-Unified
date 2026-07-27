//! Index backup/restore/integrity tooling (Section 17/18: "index backup/
//! rebuild tooling once the archive size makes that a real concern").
//! Section 9's sidecar-mirror rehydration (`sidecar_cache_enabled`) was
//! never built past its schema flag, so "rebuild" here means repairing or
//! restoring the SQLite index itself -- integrity check, FTS rebuild,
//! backup/restore -- rather than regenerating shots/embeddings from source
//! media. Re-deriving from scratch is already possible today via the
//! existing remove-root/re-add-root flow and isn't new Phase 6 scope.

use rusqlite::backup::{Backup, Progress};
use rusqlite::{Connection, OpenFlags};
use std::path::{Path, PathBuf};
use std::time::Duration;

/// Filename prefix backups are written under -- used both to name new
/// backups and to recognize/sort existing ones for retention pruning.
pub const BACKUP_FILE_PREFIX: &str = "spyglass_index_backup_";

/// Snapshots `conn`'s database into a fresh file at `dest_path`, using
/// SQLite's own online backup API rather than a raw file copy -- a plain
/// `fs::copy` of a live file risks copying an inconsistent snapshot (more
/// so now that the live connection runs in WAL mode: `dest` is a bare
/// `Connection::open`, not `Db::open_at`, so it stays in the default
/// rollback-journal mode and is fully self-contained the moment this
/// function returns and `dest` closes); the backup API is the correct
/// primitive and is safe to run against a connection that's still open.
pub fn backup_database(conn: &Connection, dest_path: &Path) -> rusqlite::Result<()> {
    let mut dest = Connection::open(dest_path)?;
    let backup = Backup::new(conn, &mut dest)?;
    let progress: Option<fn(Progress)> = None;
    backup.run_to_completion(5, Duration::from_millis(50), progress)
}

/// Deletes all but the newest `retain` backup files in `backup_dir` (sorted
/// by filename, which sorts chronologically since backups are named with a
/// zero-padded UTC timestamp). Returns the paths removed.
pub fn prune_old_backups(backup_dir: &Path, retain: usize) -> std::io::Result<Vec<PathBuf>> {
    let mut backups: Vec<PathBuf> = std::fs::read_dir(backup_dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with(BACKUP_FILE_PREFIX))
                .unwrap_or(false)
        })
        .collect();
    backups.sort();

    let mut removed = Vec::new();
    if backups.len() > retain {
        let cutoff = backups.len() - retain;
        for path in &backups[..cutoff] {
            std::fs::remove_file(path)?;
            removed.push(path.clone());
        }
    }
    Ok(removed)
}

/// Runs SQLite's own consistency checks (`integrity_check` +
/// `foreign_key_check`). Returns `["ok"]` when clean, or the list of
/// problems found.
pub fn integrity_check(conn: &Connection) -> rusqlite::Result<Vec<String>> {
    let raw: Vec<String> = conn
        .prepare("PRAGMA integrity_check")?
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut problems: Vec<String> = raw.into_iter().filter(|line| line != "ok").collect();

    let fk_problems: Vec<String> = conn
        .prepare("PRAGMA foreign_key_check")?
        .query_map([], |row| {
            let table: String = row.get(0)?;
            let rowid: Option<i64> = row.get(1)?;
            Ok(format!("foreign key violation in {table} (rowid {rowid:?})"))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    problems.extend(fk_problems);

    if problems.is_empty() {
        problems.push("ok".to_string());
    }
    Ok(problems)
}

/// Opens `path` read-only and runs `integrity_check` against it, never
/// touching the live index -- used to reject a corrupt candidate file
/// before it's ever staged as a restore.
pub fn validate_backup_file(path: &Path) -> rusqlite::Result<bool> {
    let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let problems = integrity_check(&conn)?;
    Ok(problems == ["ok".to_string()])
}

#[derive(Debug, thiserror::Error)]
pub enum RestoreError {
    #[error("backup file failed integrity check -- not restoring")]
    InvalidBackup,
    #[error("could not validate backup file: {0}")]
    Validate(#[source] rusqlite::Error),
    #[error("could not check-point the backup file before copying it: {0}")]
    Checkpoint(#[source] rusqlite::Error),
    #[error("restore file operation failed: {0}")]
    Io(#[from] std::io::Error),
}

/// Stages `backup_path` in over `live_path`: validates it's a healthy
/// SQLite database first (never stages a corrupt file over the live
/// index), check-points any WAL sidecar into `backup_path`'s own main file
/// (a raw `fs::copy` below only touches that one file -- if `backup_path`
/// happens to be a WAL-mode database with recent commits still sitting in
/// its `-wal` sidecar rather than the main file, copying just the main
/// file would silently drop them, exactly what `Db::open_at` staying open
/// against a source file used to do before this existed), copies it into
/// the same directory as `live_path`, atomically renames it into place,
/// then clears any now-stale journal/WAL sidecar files left by the
/// *previous* file at `live_path` -- a leftover journal next to the
/// swapped-in file could otherwise make SQLite think there's an
/// interrupted transaction to recover on next open. Pure filesystem logic,
/// kept free of any live `Connection` *to `live_path`* or app handle so
/// it's directly testable without a running Tauri app (the short-lived
/// connection this opens is to `backup_path`, purely to flush it, and is
/// closed before the copy).
pub fn restore_database_file(backup_path: &Path, live_path: &Path) -> Result<(), RestoreError> {
    let valid = validate_backup_file(backup_path).map_err(RestoreError::Validate)?;
    if !valid {
        return Err(RestoreError::InvalidBackup);
    }

    {
        let conn = Connection::open(backup_path).map_err(RestoreError::Checkpoint)?;
        // No-op if `backup_path` isn't in WAL mode to begin with (the
        // common case: `backup_database`'s output never is -- see its own
        // doc comment). TRUNCATE also removes the now-empty `-wal`/`-shm`
        // sidecars rather than leaving them zero-length next to the file.
        conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")
            .map_err(RestoreError::Checkpoint)?;
    }

    let staging_path = live_path.with_extension("sqlite.restoring");
    std::fs::copy(backup_path, &staging_path)?;
    std::fs::rename(&staging_path, live_path)?;

    for suffix in ["-journal", "-wal", "-shm"] {
        let sidecar = PathBuf::from(format!("{}{suffix}", live_path.to_string_lossy()));
        let _ = std::fs::remove_file(sidecar);
    }
    Ok(())
}

/// Repairs/refreshes the on-disk search structures: rebuilds the FTS5
/// virtual table from its source rows, reindexes, and reclaims space. A
/// maintenance action for corruption/staleness -- `transcript_segments_fts`
/// is normally kept in sync by its own triggers, so this isn't needed on
/// any routine path.
pub fn rebuild_search_index(conn: &Connection) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT INTO transcript_segments_fts(transcript_segments_fts) VALUES ('rebuild')",
        [],
    )?;
    conn.execute_batch("REINDEX; VACUUM;")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{insert_transcript_segment, search_transcripts, upsert_clip, Db};
    use crate::models::{NewClip, NewTranscriptSegment, SourceApp};
    use std::sync::atomic::{AtomicU64, Ordering};

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn scratch_dir(tag: &str) -> PathBuf {
        let n = TMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("spyglass_maintenance_test_{tag}_{n}"));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn seed_transcript(conn: &Connection) {
        let clip = upsert_clip(
            conn,
            &NewClip {
                file_path: "/Volumes/Archive/interview1.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap();
        insert_transcript_segment(
            conn,
            &NewTranscriptSegment {
                clip_id: clip.id,
                start_tc: 0.0,
                end_tc: 4.2,
                speaker: Some("Coach Smith".to_string()),
                text: "our mascot really got the crowd cheering tonight".to_string(),
                avg_logprob: Some(-0.2),
                no_speech_prob: Some(0.01),
            },
        )
        .unwrap();
    }

    #[test]
    fn backup_database_round_trips_real_rows_into_a_fresh_file() {
        let dir = scratch_dir("backup_roundtrip");
        let db = Db::open_at(&dir.join("live.sqlite")).unwrap();
        {
            let conn = db.conn.lock().unwrap();
            seed_transcript(&conn);
        }

        let dest = dir.join("backup1.sqlite");
        {
            let conn = db.conn.lock().unwrap();
            backup_database(&conn, &dest).unwrap();
        }

        let restored = Connection::open(&dest).unwrap();
        let results = search_transcripts(&restored, "mascot", 10).unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(integrity_check(&restored).unwrap(), vec!["ok".to_string()]);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn prune_old_backups_keeps_only_the_newest_n() {
        let dir = scratch_dir("prune");
        let names = [
            "spyglass_index_backup_2026-01-01T00-00-00Z.sqlite",
            "spyglass_index_backup_2026-01-02T00-00-00Z.sqlite",
            "spyglass_index_backup_2026-01-03T00-00-00Z.sqlite",
            "spyglass_index_backup_2026-01-04T00-00-00Z.sqlite",
            "not_a_backup.sqlite",
        ];
        for name in names {
            std::fs::write(dir.join(name), b"x").unwrap();
        }

        let removed = prune_old_backups(&dir, 2).unwrap();
        assert_eq!(removed.len(), 2);

        let remaining: Vec<String> = std::fs::read_dir(&dir)
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect();
        assert!(remaining.contains(&"spyglass_index_backup_2026-01-03T00-00-00Z.sqlite".to_string()));
        assert!(remaining.contains(&"spyglass_index_backup_2026-01-04T00-00-00Z.sqlite".to_string()));
        assert!(remaining.contains(&"not_a_backup.sqlite".to_string()));
        assert_eq!(remaining.len(), 3);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn prune_old_backups_is_a_no_op_when_under_the_retention_limit() {
        let dir = scratch_dir("prune_noop");
        std::fs::write(dir.join("spyglass_index_backup_2026-01-01T00-00-00Z.sqlite"), b"x").unwrap();

        let removed = prune_old_backups(&dir, 5).unwrap();
        assert!(removed.is_empty());

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn integrity_check_reports_ok_on_a_healthy_database() {
        let dir = scratch_dir("integrity_ok");
        let db = Db::open_at(&dir.join("live.sqlite")).unwrap();
        let conn = db.conn.lock().unwrap();
        assert_eq!(integrity_check(&conn).unwrap(), vec!["ok".to_string()]);

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn validate_backup_file_accepts_a_healthy_file_and_rejects_a_corrupt_one() {
        let dir = scratch_dir("validate");
        let db = Db::open_at(&dir.join("live.sqlite")).unwrap();
        let dest = dir.join("good_backup.sqlite");
        {
            let conn = db.conn.lock().unwrap();
            backup_database(&conn, &dest).unwrap();
        }
        assert!(validate_backup_file(&dest).unwrap());

        let corrupt = dir.join("corrupt.sqlite");
        std::fs::write(&corrupt, b"this is not a sqlite file").unwrap();
        assert!(validate_backup_file(&corrupt).is_err());

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn restore_database_file_swaps_the_live_file_for_the_backups_content() {
        let dir = scratch_dir("restore_swap");

        // The "live" db, with its own distinct clip registered.
        let live_path = dir.join("live.sqlite");
        let live_db = Db::open_at(&live_path).unwrap();
        {
            let conn = live_db.conn.lock().unwrap();
            upsert_clip(
                &conn,
                &NewClip {
                    file_path: "/Volumes/Archive/original_live_clip.mov".to_string(),
                    source_app: SourceApp::SpyglassScan,
                    checksum: None,
                    size_bytes: None,
                    duration_sec: None,
                },
            )
            .unwrap();
        }
        // Stray sidecar files left behind by the live connection, matching
        // what an interrupted rollback-journal-mode write can leave.
        std::fs::write(dir.join("live.sqlite-journal"), b"stale").unwrap();

        // A separate "backup" db with different content entirely.
        let backup_path = dir.join("backup.sqlite");
        let backup_db = Db::open_at(&backup_path).unwrap();
        {
            let conn = backup_db.conn.lock().unwrap();
            upsert_clip(
                &conn,
                &NewClip {
                    file_path: "/Volumes/Archive/restored_backup_clip.mov".to_string(),
                    source_app: SourceApp::SpyglassScan,
                    checksum: None,
                    size_bytes: None,
                    duration_sec: None,
                },
            )
            .unwrap();
        }

        restore_database_file(&backup_path, &live_path).unwrap();

        let restored = Connection::open(&live_path).unwrap();
        let clip_paths: Vec<String> = restored
            .prepare("SELECT file_path FROM clips")
            .unwrap()
            .query_map([], |row| row.get::<_, String>(0))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(clip_paths, vec!["/Volumes/Archive/restored_backup_clip.mov".to_string()]);
        assert!(!dir.join("live.sqlite-journal").exists());

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn restore_database_file_rejects_a_corrupt_backup_without_touching_the_live_file() {
        let dir = scratch_dir("restore_reject");
        let live_path = dir.join("live.sqlite");
        Db::open_at(&live_path).unwrap();
        let live_bytes_before = std::fs::read(&live_path).unwrap();

        let corrupt_backup = dir.join("corrupt_backup.sqlite");
        std::fs::write(&corrupt_backup, b"not a sqlite file").unwrap();

        let err = restore_database_file(&corrupt_backup, &live_path).unwrap_err();
        assert!(matches!(err, RestoreError::Validate(_)));
        assert_eq!(std::fs::read(&live_path).unwrap(), live_bytes_before);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn rebuild_search_index_preserves_existing_transcript_search_results() {
        let dir = scratch_dir("rebuild_fts");
        let db = Db::open_at(&dir.join("live.sqlite")).unwrap();
        let conn = db.conn.lock().unwrap();
        seed_transcript(&conn);

        assert_eq!(search_transcripts(&conn, "mascot", 10).unwrap().len(), 1);

        rebuild_search_index(&conn).unwrap();

        assert_eq!(search_transcripts(&conn, "mascot", 10).unwrap().len(), 1);
        assert_eq!(integrity_check(&conn).unwrap(), vec!["ok".to_string()]);

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }
}
