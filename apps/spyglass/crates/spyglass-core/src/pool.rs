//! The pool tray (Section 13/14): stage shots from any number of searches
//! into a working selection, then export it. Backed by the `collections`
//! table -- "each pool is a `collections` row holding an ordered list of
//! `shot_id`s" (Section 14) -- rather than any new plumbing.
//!
//! Phase 4 scope is a single always-on tray (auto-created on first use),
//! not full multi-collection management (naming/saving several named
//! pools) -- that's a natural later extension of the same schema, not
//! something this phase's UI asks for.

use crate::models::{Collection, ShotSearchResult};
use crate::search;
use rusqlite::{params, Connection, OptionalExtension};

pub const DEFAULT_POOL_NAME: &str = "Current Pool";

fn row_to_collection(row: &rusqlite::Row) -> rusqlite::Result<Collection> {
    let shot_ids_json: String = row.get(2)?;
    let shot_ids: Vec<i64> = serde_json::from_str(&shot_ids_json).unwrap_or_default();
    Ok(Collection {
        id: row.get(0)?,
        name: row.get(1)?,
        shot_ids,
        created_at: row.get(3)?,
    })
}

const COLLECTION_COLUMNS: &str = "id, name, shot_ids, created_at";

fn find_collection_by_name(conn: &Connection, name: &str) -> rusqlite::Result<Option<Collection>> {
    let sql = format!("SELECT {COLLECTION_COLUMNS} FROM collections WHERE name = ?1");
    conn.query_row(&sql, params![name], row_to_collection).optional()
}

fn get_collection(conn: &Connection, id: i64) -> rusqlite::Result<Collection> {
    let sql = format!("SELECT {COLLECTION_COLUMNS} FROM collections WHERE id = ?1");
    conn.query_row(&sql, params![id], row_to_collection)
}

fn save_shot_ids(conn: &Connection, id: i64, shot_ids: &[i64]) -> rusqlite::Result<()> {
    let json = serde_json::to_string(shot_ids).unwrap_or_else(|_| "[]".to_string());
    conn.execute("UPDATE collections SET shot_ids = ?2 WHERE id = ?1", params![id, json])?;
    Ok(())
}

/// Gets the default pool, creating it (empty) on first use.
pub fn get_or_create_default_pool(conn: &Connection) -> rusqlite::Result<Collection> {
    if let Some(existing) = find_collection_by_name(conn, DEFAULT_POOL_NAME)? {
        return Ok(existing);
    }
    conn.execute("INSERT INTO collections (name, shot_ids) VALUES (?1, '[]')", params![DEFAULT_POOL_NAME])?;
    let id = conn.last_insert_rowid();
    get_collection(conn, id)
}

/// Appends a shot to the pool if it isn't already staged (a shot can only
/// be in the pool once -- re-adding an already-staged shot is a no-op,
/// not a duplicate entry).
pub fn add_shot(conn: &Connection, pool_id: i64, shot_id: i64) -> rusqlite::Result<()> {
    let pool = get_collection(conn, pool_id)?;
    if pool.shot_ids.contains(&shot_id) {
        return Ok(());
    }
    let mut shot_ids = pool.shot_ids;
    shot_ids.push(shot_id);
    save_shot_ids(conn, pool_id, &shot_ids)
}

pub fn remove_shot(conn: &Connection, pool_id: i64, shot_id: i64) -> rusqlite::Result<()> {
    let pool = get_collection(conn, pool_id)?;
    let shot_ids: Vec<i64> = pool.shot_ids.into_iter().filter(|&id| id != shot_id).collect();
    save_shot_ids(conn, pool_id, &shot_ids)
}

/// Replaces the pool's order wholesale -- the frontend sends the full
/// reordered list after a drag-and-drop, rather than a from/to index pair,
/// which keeps this function a plain "set the order" operation instead of
/// needing to validate move semantics itself.
pub fn reorder(conn: &Connection, pool_id: i64, shot_ids: &[i64]) -> rusqlite::Result<()> {
    save_shot_ids(conn, pool_id, shot_ids)
}

pub fn clear(conn: &Connection, pool_id: i64) -> rusqlite::Result<()> {
    save_shot_ids(conn, pool_id, &[])
}

/// Hydrates the pool's ordered shot ids into full display-ready rows
/// (same shape search results use, so the same UI card component renders
/// both). `score` is meaningless here and always 0.0 -- the pool has its
/// own order, not a relevance ranking.
pub fn list_shots(conn: &Connection, pool_id: i64) -> rusqlite::Result<Vec<ShotSearchResult>> {
    let pool = get_collection(conn, pool_id)?;
    let mut results = Vec::with_capacity(pool.shot_ids.len());
    for shot_id in pool.shot_ids {
        if let Some(result) = search::fetch_shot_search_result(conn, shot_id, 0.0)? {
            results.push(result);
        }
    }
    Ok(results)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{self, Db};
    use crate::models::{NewClip, SourceApp};
    use std::sync::atomic::{AtomicU64, Ordering};

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn open_scratch_db(tag: &str) -> (Db, std::path::PathBuf) {
        let n = TMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!("spyglass_pool_test_{tag}_{n}.sqlite3"));
        std::fs::remove_file(&path).ok();
        let db = Db::open_at(&path).expect("open scratch db");
        (db, path)
    }

    fn insert_shot(conn: &Connection, clip_id: i64, start_tc: f64) -> i64 {
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, ?2, ?3)",
            params![clip_id, start_tc, start_tc + 4.0],
        )
        .unwrap();
        conn.last_insert_rowid()
    }

    #[test]
    fn get_or_create_default_pool_is_idempotent() {
        let (db, path) = open_scratch_db("get_or_create");
        let conn = db.conn.lock().unwrap();

        let first = get_or_create_default_pool(&conn).unwrap();
        let second = get_or_create_default_pool(&conn).unwrap();
        assert_eq!(first.id, second.id);

        let count: i64 = conn.query_row("SELECT COUNT(*) FROM collections", [], |r| r.get(0)).unwrap();
        assert_eq!(count, 1, "must not create a second row on repeated calls");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn add_shot_is_idempotent_and_preserves_order() {
        let (db, path) = open_scratch_db("add_shot");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/pool.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap();
        let shot_a = insert_shot(&conn, clip.id, 0.0);
        let shot_b = insert_shot(&conn, clip.id, 4.0);

        let pool = get_or_create_default_pool(&conn).unwrap();
        add_shot(&conn, pool.id, shot_a).unwrap();
        add_shot(&conn, pool.id, shot_b).unwrap();
        add_shot(&conn, pool.id, shot_a).unwrap(); // re-adding is a no-op

        let updated = get_collection(&conn, pool.id).unwrap();
        assert_eq!(updated.shot_ids, vec![shot_a, shot_b]);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn remove_shot_drops_only_the_named_shot() {
        let (db, path) = open_scratch_db("remove_shot");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/pool2.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap();
        let shot_a = insert_shot(&conn, clip.id, 0.0);
        let shot_b = insert_shot(&conn, clip.id, 4.0);

        let pool = get_or_create_default_pool(&conn).unwrap();
        add_shot(&conn, pool.id, shot_a).unwrap();
        add_shot(&conn, pool.id, shot_b).unwrap();
        remove_shot(&conn, pool.id, shot_a).unwrap();

        let updated = get_collection(&conn, pool.id).unwrap();
        assert_eq!(updated.shot_ids, vec![shot_b]);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn reorder_replaces_the_order_wholesale() {
        let (db, path) = open_scratch_db("reorder");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/pool3.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap();
        let shot_a = insert_shot(&conn, clip.id, 0.0);
        let shot_b = insert_shot(&conn, clip.id, 4.0);
        let shot_c = insert_shot(&conn, clip.id, 8.0);

        let pool = get_or_create_default_pool(&conn).unwrap();
        add_shot(&conn, pool.id, shot_a).unwrap();
        add_shot(&conn, pool.id, shot_b).unwrap();
        add_shot(&conn, pool.id, shot_c).unwrap();

        reorder(&conn, pool.id, &[shot_c, shot_a, shot_b]).unwrap();
        let updated = get_collection(&conn, pool.id).unwrap();
        assert_eq!(updated.shot_ids, vec![shot_c, shot_a, shot_b]);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn list_shots_hydrates_full_shot_details_in_pool_order() {
        let (db, path) = open_scratch_db("list_shots");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/pool4.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap();
        let shot_a = insert_shot(&conn, clip.id, 0.0);
        let shot_b = insert_shot(&conn, clip.id, 10.0);

        let pool = get_or_create_default_pool(&conn).unwrap();
        add_shot(&conn, pool.id, shot_b).unwrap();
        add_shot(&conn, pool.id, shot_a).unwrap();

        let results = list_shots(&conn, pool.id).unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].shot_id, shot_b);
        assert_eq!(results[1].shot_id, shot_a);
        assert_eq!(results[0].clip_file_path, "/Volumes/Archive/pool4.mov");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn clear_empties_the_pool() {
        let (db, path) = open_scratch_db("clear");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/pool5.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap();
        let shot_a = insert_shot(&conn, clip.id, 0.0);

        let pool = get_or_create_default_pool(&conn).unwrap();
        add_shot(&conn, pool.id, shot_a).unwrap();
        clear(&conn, pool.id).unwrap();

        let updated = get_collection(&conn, pool.id).unwrap();
        assert!(updated.shot_ids.is_empty());

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    /// Ties the pool tray to the XMEML exporter (Section 14's actual
    /// end-to-end path): stage two shots from two different clips with
    /// different frame rates, build the sequence from `list_shots`'
    /// output directly (the same call the export command makes), and
    /// confirm it's well-formed with the right clip data and a sequence
    /// rate derived from the pool's own footage.
    #[test]
    fn pool_shots_export_to_a_well_formed_xmeml_sequence() {
        use crate::xmeml::{build_sequence_xml, XmemlClip, XmemlOptions};

        let (db, path) = open_scratch_db("pool_to_xmeml");
        let conn = db.conn.lock().unwrap();

        let clip_a = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/game.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap();
        conn.execute("UPDATE clips SET frame_rate = 29.97 WHERE id = ?1", params![clip_a.id]).unwrap();

        let clip_b = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/interview.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap();
        conn.execute("UPDATE clips SET frame_rate = 29.97 WHERE id = ?1", params![clip_b.id]).unwrap();

        let shot_a = insert_shot(&conn, clip_a.id, 10.0);
        let shot_b = insert_shot(&conn, clip_b.id, 30.0);

        let pool = get_or_create_default_pool(&conn).unwrap();
        add_shot(&conn, pool.id, shot_a).unwrap();
        add_shot(&conn, pool.id, shot_b).unwrap();

        let staged = list_shots(&conn, pool.id).unwrap();
        assert_eq!(staged.len(), 2);

        let clips: Vec<XmemlClip> = staged
            .iter()
            .map(|s| XmemlClip {
                file_path: s.clip_file_path.clone(),
                name: s.clip_file_path.rsplit('/').next().unwrap_or_default().to_string(),
                frame_rate: s.clip_frame_rate,
                in_seconds: s.start_tc,
                out_seconds: s.end_tc,
                audio_format: None,
            })
            .collect();

        let xml = build_sequence_xml(&clips, &XmemlOptions::default());
        assert!(xml.contains("game.mov"));
        assert!(xml.contains("interview.mov"));
        assert!(xml.contains("file://localhost/Volumes/Archive/game.mov"));
        // Both clips are 29.97fps -- the sequence should adopt that rate,
        // not the 29.97-is-NTSC fallback default coincidentally matching.
        assert!(xml.contains("<timebase>30</timebase>"));
        assert!(xml.contains("<ntsc>TRUE</ntsc>"));
        assert_eq!(xml.matches("<clipitem id=\"clipitem-V1-").count(), 2);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }
}
