//! Read-only adapter for Card Eater's own `card-eater.sqlite3`.
//!
//! Card Eater is a sibling app with its own private database -- Spyglass
//! never writes to it and never holds a connection open across a scan (see
//! Section 19.3 of the architecture plan). Concretely:
//!
//! - open read-only, `PRAGMA busy_timeout` set on Spyglass's own connection
//!   so a brief write in progress is waited out rather than failing on
//!   first contact,
//! - retry once or twice with a short backoff if a query still comes back
//!   `SQLITE_BUSY` after the timeout, then give up on this scan pass and
//!   let the next periodic scan pick the file up,
//! - open -> query -> close per scan, never held open for the adapter's
//!   lifetime (a long-held read transaction under WAL would block
//!   checkpointing -- moot here since Card Eater's db is verified to run
//!   the default rollback-journal mode, not WAL, but the discipline is
//!   worth keeping regardless).

use crate::models::{NewClip, SourceApp};
use rusqlite::{Connection, Error as SqliteError, ErrorCode, OpenFlags};
use std::path::{Path, PathBuf};
use std::thread::sleep;
use std::time::Duration;

const BUSY_TIMEOUT_MS: u32 = 2000;
const MAX_RETRIES: u32 = 2;
const RETRY_BACKOFF_MS: u64 = 250;

fn is_busy(err: &SqliteError) -> bool {
    matches!(
        err,
        SqliteError::SqliteFailure(e, _) if e.code == ErrorCode::DatabaseBusy
    )
}

/// Runs `f` against a fresh read-only connection to `db_path`, retrying on
/// `SQLITE_BUSY` per the contract above. Returns `Ok(None)` if every retry
/// was exhausted while the database stayed busy -- callers should skip this
/// scan pass for the file/root in question rather than treat it as fatal.
fn with_retry<T>(
    db_path: &Path,
    f: impl Fn(&Connection) -> rusqlite::Result<T>,
) -> rusqlite::Result<Option<T>> {
    for attempt in 0..=MAX_RETRIES {
        let conn = Connection::open_with_flags(
            db_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        conn.busy_timeout(Duration::from_millis(BUSY_TIMEOUT_MS as u64))?;
        match f(&conn) {
            Ok(value) => return Ok(Some(value)),
            Err(e) if is_busy(&e) && attempt < MAX_RETRIES => {
                sleep(Duration::from_millis(RETRY_BACKOFF_MS));
                continue;
            }
            Err(e) if is_busy(&e) => return Ok(None),
            Err(e) => return Err(e),
        }
    }
    Ok(None)
}

/// One completed, verified copy as Card Eater recorded it -- the adapter's
/// output before it becomes a `NewClip` row in Spyglass's own index.
#[derive(Debug, Clone, PartialEq)]
pub struct CardEaterCopiedFile {
    pub resolved_file_path: String,
    pub checksum: Option<String>,
    pub size_bytes: Option<i64>,
}

fn resolve_file_path(dest_path: &str, resolved_path: Option<&str>, new_name: &str) -> String {
    let base = resolved_path.filter(|p| !p.is_empty()).unwrap_or(dest_path);
    PathBuf::from(base)
        .join(new_name)
        .to_string_lossy()
        .into_owned()
}

/// Queries `job_files`/`job_destinations`/`jobs` for files that completed a
/// verified copy (`job_files.verified = 1`, no `error`) -- the concrete join
/// described in Section 3/6 of the plan. Returns `None` if the database
/// stayed busy through every retry.
pub fn scan_completed_copies(
    db_path: &Path,
) -> rusqlite::Result<Option<Vec<CardEaterCopiedFile>>> {
    with_retry(db_path, |conn| {
        let mut stmt = conn.prepare(
            "SELECT jf.new_name, jf.size_bytes, jf.hash_source,
                    jd.dest_path, jd.resolved_path
             FROM job_files jf
             JOIN job_destinations jd ON jd.id = jf.job_destination_id
             JOIN jobs j ON j.id = jd.job_id
             WHERE jf.verified = 1 AND jf.error IS NULL",
        )?;
        let rows = stmt.query_map([], |row| {
            let new_name: String = row.get(0)?;
            let size_bytes: Option<i64> = row.get(1)?;
            let hash_source: Option<String> = row.get(2)?;
            let dest_path: String = row.get(3)?;
            let resolved_path: Option<String> = row.get(4)?;
            Ok(CardEaterCopiedFile {
                resolved_file_path: resolve_file_path(
                    &dest_path,
                    resolved_path.as_deref(),
                    &new_name,
                ),
                checksum: hash_source,
                size_bytes,
            })
        })?;
        rows.collect()
    })
}

impl From<CardEaterCopiedFile> for NewClip {
    fn from(f: CardEaterCopiedFile) -> Self {
        NewClip {
            file_path: f.resolved_file_path,
            source_app: SourceApp::CardEater,
            checksum: f.checksum,
            size_bytes: f.size_bytes,
            duration_sec: None,
            recorded_at: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    /// Builds a scratch `card-eater.sqlite3` with just enough schema to
    /// exercise the adapter's join, independent of Card Eater's own
    /// migrations (which this crate has no business depending on).
    fn build_fixture_db(tag: &str) -> (PathBuf, Connection) {
        let n = TMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!("card_eater_fixture_{tag}_{n}.sqlite3"));
        std::fs::remove_file(&path).ok();
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT);
             CREATE TABLE job_destinations (
                 id INTEGER PRIMARY KEY, job_id INTEGER, dest_path TEXT,
                 resolved_path TEXT, status TEXT
             );
             CREATE TABLE job_files (
                 id INTEGER PRIMARY KEY, job_destination_id INTEGER,
                 original_name TEXT, new_name TEXT, size_bytes INTEGER,
                 hash_source TEXT, hash_dest TEXT, verified INTEGER, error TEXT
             );",
        )
        .unwrap();
        (path, conn)
    }

    #[test]
    fn resolve_file_path_prefers_resolved_path_over_dest_path() {
        let p = resolve_file_path("/Volumes/Archive/raw", Some("/Volumes/Archive/raw/Fall 2025"), "clip_001.mov");
        assert_eq!(p, "/Volumes/Archive/raw/Fall 2025/clip_001.mov");
    }

    #[test]
    fn resolve_file_path_falls_back_to_dest_path_when_unresolved() {
        let p = resolve_file_path("/Volumes/Archive/raw", None, "clip_001.mov");
        assert_eq!(p, "/Volumes/Archive/raw/clip_001.mov");
    }

    #[test]
    fn scan_only_returns_verified_error_free_files() {
        let (path, conn) = build_fixture_db("verified_filter");
        conn.execute("INSERT INTO jobs (id, status) VALUES (1, 'complete')", [])
            .unwrap();
        conn.execute(
            "INSERT INTO job_destinations (id, job_id, dest_path, resolved_path, status)
             VALUES (1, 1, '/Volumes/Archive/raw', '/Volumes/Archive/raw/Fall2025', 'complete')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO job_files (job_destination_id, original_name, new_name, size_bytes, hash_source, verified, error)
             VALUES (1, 'C0001.MP4', 'Fall2025_001_C0001.MP4', 5000, 'blake3hash1', 1, NULL)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO job_files (job_destination_id, original_name, new_name, size_bytes, hash_source, verified, error)
             VALUES (1, 'C0002.MP4', 'Fall2025_002_C0002.MP4', 6000, NULL, 0, 'copy failed')",
            [],
        )
        .unwrap();
        drop(conn);

        let files = scan_completed_copies(&path).unwrap().unwrap();
        assert_eq!(files.len(), 1);
        assert_eq!(
            files[0].resolved_file_path,
            "/Volumes/Archive/raw/Fall2025/Fall2025_001_C0001.MP4"
        );
        assert_eq!(files[0].checksum.as_deref(), Some("blake3hash1"));
        assert_eq!(files[0].size_bytes, Some(5000));

        std::fs::remove_file(&path).ok();
    }
}
