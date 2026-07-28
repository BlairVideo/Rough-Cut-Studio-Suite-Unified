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

/// An "apparent" path -- a watched root's own path, or a folder-tree
/// node's path, or a `folder_path` search filter value, all of which are
/// built by appending real subfolder names onto a watched root's own
/// stored path -- and the real filesystem-path prefix its clips are
/// actually registered under (`clips.file_path`) are identical for
/// ordinary content: a watched root's path is a real directory, and
/// everything under it keeps the same path in `file_path`. They diverge
/// the moment a Finder alias inside the tree resolves to a real location
/// with no path relationship to where the alias sits (`scanner::AliasLink`,
/// recorded by `db::upsert_alias_link` at the exact point `scanner::
/// resolve_finder_alias` resolves it) -- e.g. a shortcut to footage on a
/// second, unrelated volume. Plain string-prefix matching against
/// `clips.file_path` can never bridge that gap on its own, so every
/// caller that walks the tree by path prefix (`folder_stats`,
/// `list_folder_children`, and `facets::matching_shot_ids`'s `folder_path`
/// filter) has to translate through the two helpers below first.
///
/// Both load the whole (small -- one row per Finder alias ever resolved
/// by a scan, not per file) `alias_links` table and match in Rust rather
/// than via SQL `LIKE`, since an alias's `apparent_path` is arbitrary
/// user-chosen folder-name data that could itself contain `%`/`_`
/// wildcard characters.

/// One hop: the longest recorded `alias_links` entry whose `apparent_path`
/// is `path` itself or an ancestor of it, with that matched prefix
/// substituted for its `real_path` (everything past it kept as-is).
/// `path` unchanged if nothing matches.
fn translate_once(links: &[(String, String)], path: &str) -> String {
    let current = path.trim_end_matches('/');
    let best = links
        .iter()
        .map(|(apparent, real)| (apparent.trim_end_matches('/'), real.trim_end_matches('/')))
        .filter(|(apparent, _)| current == *apparent || current.starts_with(&format!("{apparent}/")))
        .max_by_key(|(apparent, _)| apparent.len());

    match best {
        Some((apparent, real)) => format!("{real}{}", &current[apparent.len()..]),
        None => current.to_string(),
    }
}

/// Loops `translate_once` to follow a chain of nested aliases (an alias
/// whose own resolved target itself contains a further alias, recorded as
/// its own `AliasLink` when the scanner recurses into it). Bounded --
/// real chains are never more than a couple of hops deep, so this only
/// guards against a corrupt/cyclic table.
fn translate_chain(links: &[(String, String)], path: &str) -> String {
    let mut current = path.trim_end_matches('/').to_string();
    for _ in 0..8 {
        let next = translate_once(links, &current);
        if next == current {
            break;
        }
        current = next;
    }
    current
}

/// Every real path prefix whose clips count as being at or under
/// `apparent_path`, recursively: the translated base itself, plus -- for
/// every alias boundary nested anywhere below it, in whatever coordinate
/// space each hop lands in -- each further resolved target, chained/
/// nested arbitrarily deep. `translate_chain` alone (a single-hop-chased
/// base, no descent) is what `list_folder_children`/`folder_stats` use
/// where a caller needs to match/derive exactly one level, not a whole
/// recursive subtree; this is for recursive aggregation (`folder_stats`'s
/// `shot_count`, and `facets::
/// matching_shot_ids`'s `folder_path` filter, both documented as matching
/// "anywhere under this folder"): a folder several levels *above* a
/// Finder alias needs its own total to include everything the alias
/// redirects to, even though that content shares no path prefix with the
/// folder's own real location at all.
fn real_prefixes_recursive(links: &[(String, String)], base: &str, depth: usize) -> Vec<String> {
    let mut out = vec![base.to_string()];
    if depth >= 8 {
        return out;
    }
    let boundary = format!("{base}/");
    for (apparent, real) in links {
        let apparent = apparent.trim_end_matches('/');
        if apparent == base || apparent.starts_with(&boundary) {
            out.extend(real_prefixes_recursive(links, real.trim_end_matches('/'), depth + 1));
        }
    }
    out
}

pub(crate) fn real_prefixes_for(conn: &Connection, apparent_path: &str) -> rusqlite::Result<Vec<String>> {
    let links = crate::db::list_alias_links(conn)?;
    let base = translate_chain(&links, apparent_path);
    Ok(real_prefixes_recursive(&links, &base, 0))
}

/// Names of aliases whose own apparent path sits immediately inside
/// `apparent_parent` (not deeper) -- the alias-derived half of a folder's
/// immediate children, alongside the real-subfolder half `list_folder_children`
/// derives from `clips.file_path`. A deeper nested alias isn't included
/// here -- lazy one-level-at-a-time, matching how this module's tree
/// expansion already works; it surfaces once the user expands down to it.
fn immediate_alias_child_names(links: &[(String, String)], apparent_parent: &str) -> Vec<String> {
    let boundary = format!("{}/", apparent_parent.trim_end_matches('/'));
    links
        .iter()
        .filter_map(|(apparent, _)| {
            let rest = apparent.trim_end_matches('/').strip_prefix(boundary.as_str())?;
            if rest.is_empty() || rest.contains('/') {
                None
            } else {
                Some(rest.to_string())
            }
        })
        .collect()
}

fn folder_stats(conn: &Connection, apparent_folder_path: &str) -> rusqlite::Result<(i64, bool)> {
    let links = crate::db::list_alias_links(conn)?;

    let mut shot_count = 0i64;
    for real_prefix_base in real_prefixes_recursive(&links, &translate_chain(&links, apparent_folder_path), 0) {
        let prefix = normalize_prefix(&real_prefix_base);
        shot_count += conn.query_row(
            "SELECT COUNT(*) FROM shots s JOIN clips c ON c.id = s.clip_id
             WHERE c.file_path = ?1 OR c.file_path LIKE ?2",
            params![real_prefix_base, format!("{prefix}%")],
            |r| r.get::<_, i64>(0),
        )?;
    }

    // Deliberately the single-hop base, not the full recursive prefix set
    // above -- "does expanding this node one level show anything" is
    // answered by its own immediate real subfolders plus its own
    // immediate alias children, not by what a *further* nested alias two
    // levels down eventually resolves to.
    let base = translate_chain(&links, apparent_folder_path);
    let base_prefix = normalize_prefix(&base);
    let has_real_subfolder: bool = conn.query_row(
        "SELECT EXISTS(
            SELECT 1 FROM clips
            WHERE file_path LIKE ?1
              AND INSTR(SUBSTR(file_path, LENGTH(?2) + 1), '/') > 0
         )",
        params![format!("{base_prefix}%"), base_prefix],
        |r| r.get::<_, i64>(0),
    )? != 0;
    let has_alias_child = !immediate_alias_child_names(&links, apparent_folder_path).is_empty();

    Ok((shot_count, has_real_subfolder || has_alias_child))
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
            // Resolved against the real path children are actually
            // registered under (`translate_once`/`translate_chain`'s doc
            // comment), but every `FolderNode` handed back stays in
            // `parent`'s own apparent terms -- `child_name` is just the
            // next path segment, and appending it onto the *apparent*
            // parent (not the real one) is what keeps a further expand/
            // `folder_path` filter round-tripping through this same
            // translation correctly, including a further nested alias one
            // level down.
            let links = crate::db::list_alias_links(conn)?;
            let apparent_prefix = normalize_prefix(parent);
            let real_prefix = normalize_prefix(&translate_chain(&links, parent));
            let mut stmt = conn.prepare(
                "SELECT SUBSTR(SUBSTR(file_path, LENGTH(?1) + 1), 1,
                        INSTR(SUBSTR(file_path, LENGTH(?1) + 1), '/') - 1) AS child_name
                 FROM clips
                 WHERE file_path LIKE ?2
                   AND INSTR(SUBSTR(file_path, LENGTH(?1) + 1), '/') > 0
                 GROUP BY child_name
                 ORDER BY child_name ASC",
            )?;
            let mut names: Vec<String> = stmt
                .query_map(params![real_prefix, format!("{real_prefix}%")], |r| r.get::<_, String>(0))?
                .collect::<Result<_, _>>()?;

            // A folder reached only through a Finder alias -- like
            // Athletics itself -- has no real subfolder of its own under
            // `parent`'s real prefix at all (its content lives entirely on
            // a different volume), so the query above alone would never
            // surface it. Its name comes directly from the alias's own
            // recorded apparent path instead.
            for name in immediate_alias_child_names(&links, parent) {
                if !names.contains(&name) {
                    names.push(name);
                }
            }
            names.sort();

            let mut nodes = Vec::with_capacity(names.len());
            for name in names {
                let child_path = format!("{apparent_prefix}{name}");
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

    #[test]
    fn a_folder_reached_through_a_finder_alias_appears_as_a_child_and_can_be_expanded() {
        // The actual Athletics bug, reproduced at the db layer (no real
        // Finder alias needed here -- `scanner::discover_media_files`'s
        // own macOS-only test covers that the scanner records the
        // `AliasLink` correctly; this proves `folders.rs` can use one once
        // recorded). "Athletics" sits inside the "/Volumes/Root" watched
        // root only as a Finder alias -- every actual clip lives on a
        // completely different volume, sharing no path prefix with the
        // watched root at all.
        let (db, path) = open_scratch_db("alias_crossing");
        let conn = db.conn.lock().unwrap();
        let root = db::add_watched_root(
            &conn,
            &NewWatchedRoot { label: "Root".into(), path: "/Volumes/Root".into(), volume_id: None, approved_by: None },
        )
        .unwrap();
        db::upsert_alias_link(&conn, "/Volumes/Root/Athletics", "/Volumes/OtherDrive/Athletics").unwrap();
        let clip = insert_clip(&conn, "/Volumes/OtherDrive/Athletics/Fall/game.mov");
        insert_shot(&conn, clip, 0.0, 4.0);

        let top_level = list_folder_children(&conn, None).unwrap();
        assert_eq!(top_level[0].root_id, Some(root.id));
        assert_eq!(top_level[0].shot_count, 1, "the aliased clip counts toward the watched root's own total");
        assert!(top_level[0].has_children, "Athletics must appear as a child, not silently vanish");

        let root_children = list_folder_children(&conn, Some("/Volumes/Root")).unwrap();
        assert_eq!(root_children.len(), 1);
        let athletics = &root_children[0];
        assert_eq!(athletics.name, "Athletics");
        assert_eq!(
            athletics.path, "/Volumes/Root/Athletics",
            "the node's path stays in the watched root's own apparent terms, not the alias's real target"
        );
        assert_eq!(athletics.shot_count, 1);
        assert!(athletics.has_children, "Athletics/Fall/game.mov is one level deeper");

        let athletics_children = list_folder_children(&conn, Some("/Volumes/Root/Athletics")).unwrap();
        assert_eq!(athletics_children.len(), 1);
        assert_eq!(athletics_children[0].name, "Fall");
        assert_eq!(
            athletics_children[0].path, "/Volumes/Root/Athletics/Fall",
            "expanding one level further still stays in apparent terms"
        );
        assert!(!athletics_children[0].has_children, "Fall/game.mov is a leaf file");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }
}
