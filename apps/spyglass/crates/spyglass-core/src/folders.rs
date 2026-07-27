//! Folder tree (Search workspace left panel): lets a user narrow a search
//! to a specific watched folder, or a subfolder within it, without typing
//! a path. There's no folder table in the schema -- `watched_roots` is
//! just an allowlist of top-level scan roots, and everything under them
//! is only known indirectly, via `clips.file_path` strings. Rather than
//! adding a folder table (a real schema migration + backfill for what's
//! fundamentally a display concern), this derives the tree on demand by
//! grouping `clips.file_path` on its path components below a given
//! prefix -- the same "cheap enough to call on demand" philosophy
//! `list_facet_options` already uses (see that function's doc comment).
//!
//! Lazy/one-level-at-a-time by design: `list_folder_children(None)`
//! returns the watched roots themselves as top-level nodes;
//! `list_folder_children(Some(path))` returns that path's immediate
//! child directories (one level deeper, not the whole subtree). A UI
//! tree view expands nodes on click, so there's no need to materialize
//! more than one level per call.

use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FolderNode {
    pub name: String,
    /// Absolute path this node represents -- what a caller passes back as
    /// `folder_path` (via `FacetFilters`) or as this function's own
    /// `parent_path` to expand one level further.
    pub path: String,
    /// `Some(id)` only for a top-level watched-root node, so the UI can
    /// offer root-level actions (scan/pause/remove) without a second
    /// lookup. `None` for every derived subfolder node.
    pub root_id: Option<i64>,
    /// Shots anywhere under this folder (recursive), not just directly
    /// in it -- matches how `folder_path` filtering itself works.
    pub shot_count: i64,
    pub has_children: bool,
}

/// `path` with exactly one trailing `/` -- the shared prefix shape every
/// `LIKE`/`SUBSTR` comparison in this module and in `facets::matching_shot_ids`
/// relies on (a bare prefix without the separator would let
/// `/Volumes/Archive2` wrongly match a `/Volumes/Archive` filter).
pub(crate) fn normalize_prefix(path: &str) -> String {
    if path.ends_with('/') {
        path.to_string()
    } else {
        format!("{path}/")
    }
}

fn folder_stats(conn: &Connection, folder_path: &str) -> rusqlite::Result<(i64, bool)> {
    let prefix = normalize_prefix(folder_path);
    let shot_count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM shots s JOIN clips c ON c.id = s.clip_id
         WHERE c.file_path = ?1 OR c.file_path LIKE ?2",
        params![folder_path, format!("{prefix}%")],
        |r| r.get(0),
    )?;
    let has_children: bool = conn.query_row(
        "SELECT EXISTS(
            SELECT 1 FROM clips
            WHERE file_path LIKE ?1
              AND INSTR(SUBSTR(file_path, LENGTH(?2) + 1), '/') > 0
         )",
        params![format!("{prefix}%"), prefix],
        |r| r.get::<_, i64>(0),
    )? != 0;
    Ok((shot_count, has_children))
}

/// `None` -> the watched roots themselves (top-level tree nodes).
/// `Some(parent)` -> `parent`'s immediate child directories, derived from
/// every clip file path one level below it. Removed/tombstoned watched
/// roots are excluded at the top level, same as the Settings panel's own
/// `list_visible_watched_roots`.
pub fn list_folder_children(conn: &Connection, parent_path: Option<&str>) -> rusqlite::Result<Vec<FolderNode>> {
    match parent_path {
        None => {
            let roots = crate::db::list_visible_watched_roots(conn)?;
            let mut nodes = Vec::with_capacity(roots.len());
            for root in roots {
                let (shot_count, has_children) = folder_stats(conn, &root.path)?;
                nodes.push(FolderNode {
                    name: root.label,
                    path: root.path,
                    root_id: Some(root.id),
                    shot_count,
                    has_children,
                });
            }
            Ok(nodes)
        }
        Some(parent) => {
            let prefix = normalize_prefix(parent);
            let mut stmt = conn.prepare(
                "SELECT SUBSTR(SUBSTR(file_path, LENGTH(?1) + 1), 1,
                        INSTR(SUBSTR(file_path, LENGTH(?1) + 1), '/') - 1) AS child_name
                 FROM clips
                 WHERE file_path LIKE ?2
                   AND INSTR(SUBSTR(file_path, LENGTH(?1) + 1), '/') > 0
                 GROUP BY child_name
                 ORDER BY child_name ASC",
            )?;
            let names: Vec<String> = stmt
                .query_map(params![prefix, format!("{prefix}%")], |r| r.get::<_, String>(0))?
                .collect::<Result<_, _>>()?;

            let mut nodes = Vec::with_capacity(names.len());
            for name in names {
                let child_path = format!("{prefix}{name}");
                let (shot_count, has_children) = folder_stats(conn, &child_path)?;
                nodes.push(FolderNode { name, path: child_path, root_id: None, shot_count, has_children });
            }
            Ok(nodes)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{self, Db};
    use crate::models::{AccessLevel, NewClip, NewWatchedRoot, SourceApp};
    use std::sync::atomic::{AtomicU64, Ordering};

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn open_scratch_db(tag: &str) -> (Db, std::path::PathBuf) {
        let n = TMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!("spyglass_folders_test_{tag}_{n}.sqlite3"));
        std::fs::remove_file(&path).ok();
        let db = Db::open_at(&path).expect("open scratch db");
        (db, path)
    }

    fn insert_clip(conn: &Connection, path: &str) -> i64 {
        db::upsert_clip(
            conn,
            &NewClip {
                file_path: path.to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap()
        .id
    }

    fn insert_shot(conn: &Connection, clip_id: i64, start_tc: f64, end_tc: f64) -> i64 {
        conn.execute("INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, ?2, ?3)", params![clip_id, start_tc, end_tc])
            .unwrap();
        conn.last_insert_rowid()
    }

    #[test]
    fn top_level_lists_visible_watched_roots_with_recursive_shot_counts() {
        let (db, path) = open_scratch_db("top_level");
        let conn = db.conn.lock().unwrap();
        let root = db::add_watched_root(
            &conn,
            &NewWatchedRoot { label: "Archive".into(), path: "/Volumes/Archive".into(), volume_id: None, approved_by: None },
        )
        .unwrap();
        let clip_a = insert_clip(&conn, "/Volumes/Archive/2025/game.mov");
        let clip_b = insert_clip(&conn, "/Volumes/Archive/loose.mov");
        insert_shot(&conn, clip_a, 0.0, 4.0);
        insert_shot(&conn, clip_b, 0.0, 4.0);

        let nodes = list_folder_children(&conn, None).unwrap();
        assert_eq!(nodes.len(), 1);
        assert_eq!(nodes[0].root_id, Some(root.id));
        assert_eq!(nodes[0].path, "/Volumes/Archive");
        assert_eq!(nodes[0].shot_count, 2, "counts shots in subfolders and directly in the root");
        assert!(nodes[0].has_children, "has a 2025/ subfolder");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn removed_watched_roots_are_excluded_from_the_top_level() {
        let (db, path) = open_scratch_db("removed_root");
        let conn = db.conn.lock().unwrap();
        let root = db::add_watched_root(
            &conn,
            &NewWatchedRoot { label: "Old".into(), path: "/Volumes/Old".into(), volume_id: None, approved_by: None },
        )
        .unwrap();
        db::set_watched_root_access_level(&conn, root.id, AccessLevel::Removed).unwrap();

        let nodes = list_folder_children(&conn, None).unwrap();
        assert!(nodes.is_empty());

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn expanding_a_parent_lists_only_its_immediate_subfolders() {
        let (db, path) = open_scratch_db("expand");
        let conn = db.conn.lock().unwrap();
        let clip_2024 = insert_clip(&conn, "/Volumes/Archive/2024/fall/game.mov");
        let clip_2025 = insert_clip(&conn, "/Volumes/Archive/2025/game.mov");
        let clip_loose = insert_clip(&conn, "/Volumes/Archive/loose.mov");
        insert_shot(&conn, clip_2024, 0.0, 4.0);
        insert_shot(&conn, clip_2025, 0.0, 4.0);
        insert_shot(&conn, clip_loose, 0.0, 4.0);

        let nodes = list_folder_children(&conn, Some("/Volumes/Archive")).unwrap();
        let names: Vec<&str> = nodes.iter().map(|n| n.name.as_str()).collect();
        assert_eq!(names, vec!["2024", "2025"], "a loose file directly in the folder is not itself a child folder");

        let node_2024 = nodes.iter().find(|n| n.name == "2024").unwrap();
        assert_eq!(node_2024.path, "/Volumes/Archive/2024");
        assert!(node_2024.has_children, "2024 has a fall/ subfolder one level deeper");
        assert_eq!(node_2024.shot_count, 1);

        let node_2025 = nodes.iter().find(|n| n.name == "2025").unwrap();
        assert!(!node_2025.has_children, "2025/game.mov is a leaf file, not a subfolder");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }
}
