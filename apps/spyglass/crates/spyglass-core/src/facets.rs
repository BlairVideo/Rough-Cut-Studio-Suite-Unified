//! Facet browsing (Section 12/13): filter/browse shots by tag, date range,
//! and source app without a text query, and narrow a text search down to a
//! selected facet set.
//!
//! Scope note: Section 13's original facet list was "tags, date range,
//! source event, shot type." Only `tags.label`, `clips.ingested_at`, and
//! `clips.source_app` have real columns behind them -- there's no
//! shot-type classification anywhere in the pipeline, and no "source
//! event" grouping (the closest real concept, `watched_roots`, groups
//! folders a human chose to watch, not discrete capture events). This
//! builds the three facets the schema actually supports; "source event"
//! could later mean grouping by `watched_root_id` if that turns out to be
//! what's actually wanted, and "shot type" needs a new column and a
//! classifier before it's buildable at all.

use crate::search::fetch_shot_search_result;
use crate::models::ShotSearchResult;
use rusqlite::{params, params_from_iter, Connection};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

/// `#[serde(default)]` on the struct (not just per-field) so a caller can
/// omit any subset of fields -- including all of them (`{}`, which
/// `is_empty()` below and `Default` already treat as "no filters set").
/// Tauri's own frontend always sent a fully-populated object, so this gap
/// went unnoticed until the Suite integration's Python bridge, where an
/// empty dict for "no filters" is the natural idiom.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", default)]
pub struct FacetFilters {
    /// OR within this list (any selected tag matches) -- selecting
    /// "mascot" and "cheering" broadens results, it doesn't narrow them to
    /// shots tagged with both.
    pub tags: Vec<String>,
    pub source_app: Option<String>,
    /// Inclusive, `YYYY-MM-DD`, compared against `date(clips.ingested_at)`.
    pub date_from: Option<String>,
    pub date_to: Option<String>,
    /// Restrict to shots with `is_favorite = 1`.
    pub favorites_only: bool,
    /// Restrict to shots whose clip lives at or under this absolute path
    /// -- a watched root's own path, or one of its subfolders, from the
    /// folder-tree panel (see `crate::folders`).
    pub folder_path: Option<String>,
    /// Result ordering (Section 12/13's "sort by" control) -- orthogonal to
    /// the fields above, which only ever narrow *which* shots qualify.
    /// Bundled onto `FacetFilters` rather than threaded as its own
    /// parameter so it rides through the existing filters plumbing
    /// (Tauri command, PyO3 dict, the Python bridge, both frontends)
    /// unchanged.
    pub sort_by: SortBy,
}

/// How to order `search_shots`/`browse_shots` results. `Relevance` only
/// carries real meaning for a text search (`search_shots`) -- `browse_shots`
/// has no relevance signal without a query, so it treats `Relevance` the
/// same as `NewestFirst` (its own long-standing default, Section 13).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SortBy {
    #[default]
    Relevance,
    NewestFirst,
    OldestFirst,
    /// By `shots.technical_quality_score` descending; a shot with no score
    /// yet (gap-fill hasn't attached B-Roll Analyzer data for it) sorts
    /// after every scored shot rather than being dropped.
    HighestQuality,
    /// By `shots.energy_score` descending, same "unscored sorts last"
    /// convention as `HighestQuality`.
    MostEnergy,
}

impl FacetFilters {
    pub fn is_empty(&self) -> bool {
        self.tags.is_empty()
            && self.source_app.is_none()
            && self.date_from.is_none()
            && self.date_to.is_none()
            && !self.favorites_only
            && self.folder_path.is_none()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TagFacet {
    pub label: String,
    pub shot_count: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceFacet {
    pub source_app: String,
    pub shot_count: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FacetOptions {
    pub tags: Vec<TagFacet>,
    pub sources: Vec<SourceFacet>,
    /// `date(clips.ingested_at)` bounds across every clip with at least
    /// one shot -- lets the UI clamp its date pickers to a range that can
    /// actually return results instead of a blank calendar.
    pub earliest_date: Option<String>,
    pub latest_date: Option<String>,
}

/// Every facet's available values and counts, for populating the sidebar.
/// Cheap enough (a handful of `GROUP BY` scans) to call on demand rather
/// than caching -- see Section 16's archive-scale note (tens of thousands
/// of shots).
pub fn list_facet_options(conn: &Connection) -> rusqlite::Result<FacetOptions> {
    let tags = {
        let mut stmt = conn.prepare(
            "SELECT label, COUNT(DISTINCT shot_id) as cnt FROM tags GROUP BY label ORDER BY cnt DESC, label ASC",
        )?;
        let rows = stmt.query_map([], |r| Ok(TagFacet { label: r.get(0)?, shot_count: r.get(1)? }))?;
        rows.collect::<Result<Vec<_>, _>>()?
    };

    let sources = {
        let mut stmt = conn.prepare(
            "SELECT c.source_app, COUNT(*) as cnt FROM shots s JOIN clips c ON c.id = s.clip_id GROUP BY c.source_app",
        )?;
        let rows = stmt.query_map([], |r| Ok(SourceFacet { source_app: r.get(0)?, shot_count: r.get(1)? }))?;
        rows.collect::<Result<Vec<_>, _>>()?
    };

    let (earliest_date, latest_date): (Option<String>, Option<String>) = conn.query_row(
        "SELECT MIN(date(c.ingested_at)), MAX(date(c.ingested_at)) FROM shots s JOIN clips c ON c.id = s.clip_id",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;

    Ok(FacetOptions { tags, sources, earliest_date, latest_date })
}

/// Shot ids satisfying every selected facet -- AND across the tag/source/
/// date categories, OR within the tag list itself. `None` means "no
/// filters set, don't restrict anything" -- kept distinct from
/// `Some(empty set)`, which would (incorrectly) zero out every result.
pub fn matching_shot_ids(conn: &Connection, filters: &FacetFilters) -> rusqlite::Result<Option<HashSet<i64>>> {
    if filters.is_empty() {
        return Ok(None);
    }

    let mut allowed: Option<HashSet<i64>> = None;
    let mut intersect = |ids: HashSet<i64>| {
        allowed = Some(match allowed.take() {
            Some(existing) => existing.intersection(&ids).copied().collect(),
            None => ids,
        });
    };

    if !filters.tags.is_empty() {
        let placeholders = filters.tags.iter().map(|_| "?").collect::<Vec<_>>().join(",");
        let sql = format!("SELECT DISTINCT shot_id FROM tags WHERE label IN ({placeholders})");
        let mut stmt = conn.prepare(&sql)?;
        let ids: HashSet<i64> =
            stmt.query_map(params_from_iter(filters.tags.iter()), |r| r.get::<_, i64>(0))?.collect::<Result<_, _>>()?;
        intersect(ids);
    }

    if let Some(source_app) = &filters.source_app {
        let mut stmt =
            conn.prepare("SELECT s.id FROM shots s JOIN clips c ON c.id = s.clip_id WHERE c.source_app = ?1")?;
        let ids: HashSet<i64> =
            stmt.query_map(params![source_app], |r| r.get::<_, i64>(0))?.collect::<Result<_, _>>()?;
        intersect(ids);
    }

    if filters.date_from.is_some() || filters.date_to.is_some() {
        let mut stmt = conn.prepare(
            "SELECT s.id FROM shots s JOIN clips c ON c.id = s.clip_id
             WHERE (?1 IS NULL OR date(c.ingested_at) >= ?1)
               AND (?2 IS NULL OR date(c.ingested_at) <= ?2)",
        )?;
        let ids: HashSet<i64> = stmt
            .query_map(params![filters.date_from, filters.date_to], |r| r.get::<_, i64>(0))?
            .collect::<Result<_, _>>()?;
        intersect(ids);
    }

    if filters.favorites_only {
        let mut stmt = conn.prepare("SELECT id FROM shots WHERE is_favorite = 1")?;
        let ids: HashSet<i64> = stmt.query_map([], |r| r.get::<_, i64>(0))?.collect::<Result<_, _>>()?;
        intersect(ids);
    }

    if let Some(folder_path) = &filters.folder_path {
        // `folder_path` is an apparent path (a watched root's own path, or
        // a folder-tree node's path handed back by `folders::
        // list_folder_children`) and matches "anywhere under this folder"
        // (same recursive semantics as `FolderNode::shot_count`) -- so
        // this needs every real prefix reachable from it, not just a
        // single translated one, or a folder reached through a Finder
        // alias (see `folders::real_prefixes_for`) would show a nonzero
        // count in the tree yet filter down to zero results here, and a
        // folder merely *containing* an aliased subfolder further down
        // would silently drop that subfolder's shots from its own total.
        let mut ids: HashSet<i64> = HashSet::new();
        for real_path in crate::folders::real_prefixes_for(conn, folder_path)? {
            let prefix = crate::folders::normalize_prefix(&real_path);
            let mut stmt = conn.prepare(
                "SELECT s.id FROM shots s JOIN clips c ON c.id = s.clip_id
                 WHERE c.file_path = ?1 OR c.file_path LIKE ?2",
            )?;
            let found: HashSet<i64> = stmt
                .query_map(params![real_path, format!("{prefix}%")], |r| r.get::<_, i64>(0))?
                .collect::<Result<_, _>>()?;
            ids.extend(found);
        }
        intersect(ids);
    }

    Ok(allowed)
}

/// The `ORDER BY` clause for a given `SortBy` -- `Relevance` has no
/// meaning without a text query, so `browse_shots` maps it onto
/// `NewestFirst`, its own long-standing default (Section 13). Every branch
/// is a fixed, closed-enum literal (never interpolated user input), so
/// building the clause by string match is safe.
fn browse_order_by_clause(sort_by: SortBy) -> &'static str {
    match sort_by {
        SortBy::Relevance | SortBy::NewestFirst => "c.ingested_at DESC, s.id DESC",
        SortBy::OldestFirst => "c.ingested_at ASC, s.id ASC",
        SortBy::HighestQuality => "s.technical_quality_score IS NULL, s.technical_quality_score DESC, s.id DESC",
        SortBy::MostEnergy => "s.energy_score IS NULL, s.energy_score DESC, s.id DESC",
    }
}

/// Facet-only browsing (Section 13: "browsing by tag/date/source without
/// typing a query") -- with no explicit `sort_by`, results are newest-
/// ingested-first (there's no relevance signal without a text query), same
/// convention as `list_favorite_shots`. With no filters set this doubles
/// as a plain "browse the whole archive" view in whatever order was
/// chosen.
pub fn browse_shots(conn: &Connection, filters: &FacetFilters, limit: i64) -> rusqlite::Result<Vec<ShotSearchResult>> {
    let allowed = matching_shot_ids(conn, filters)?;

    let shot_ids: Vec<i64> = {
        let sql = format!(
            "SELECT s.id FROM shots s JOIN clips c ON c.id = s.clip_id ORDER BY {}",
            browse_order_by_clause(filters.sort_by)
        );
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map([], |r| r.get::<_, i64>(0))?;
        rows.collect::<Result<Vec<_>, _>>()?
    };

    let mut results = Vec::new();
    for shot_id in shot_ids {
        if let Some(allowed) = &allowed {
            if !allowed.contains(&shot_id) {
                continue;
            }
        }
        if let Some(result) = fetch_shot_search_result(conn, shot_id, 0.0)? {
            results.push(result);
        }
        if results.len() as i64 >= limit {
            break;
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
        let path = std::env::temp_dir().join(format!("spyglass_facets_test_{tag}_{n}.sqlite3"));
        std::fs::remove_file(&path).ok();
        let db = Db::open_at(&path).expect("open scratch db");
        (db, path)
    }

    fn insert_clip(conn: &Connection, path: &str, source_app: SourceApp) -> i64 {
        db::upsert_clip(
            conn,
            &NewClip { file_path: path.to_string(), source_app, checksum: None, size_bytes: None, duration_sec: None },
        )
        .unwrap()
        .id
    }

    fn insert_shot(conn: &Connection, clip_id: i64, start_tc: f64, end_tc: f64) -> i64 {
        conn.execute("INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, ?2, ?3)", params![clip_id, start_tc, end_tc])
            .unwrap();
        conn.last_insert_rowid()
    }

    fn set_ingested_at(conn: &Connection, clip_id: i64, date: &str) {
        conn.execute("UPDATE clips SET ingested_at = ?1 WHERE id = ?2", params![format!("{date}T00:00:00.000Z"), clip_id])
            .unwrap();
    }

    fn tag_shot(conn: &Connection, shot_id: i64, label: &str) {
        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, ?2, 'human')", params![shot_id, label]).unwrap();
    }

    #[test]
    fn empty_filters_mean_no_restriction() {
        let (db, path) = open_scratch_db("empty");
        let conn = db.conn.lock().unwrap();
        assert_eq!(matching_shot_ids(&conn, &FacetFilters::default()).unwrap(), None);
        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn tag_filter_is_or_within_the_list() {
        let (db, path) = open_scratch_db("tag_or");
        let conn = db.conn.lock().unwrap();
        let clip = insert_clip(&conn, "/Volumes/Archive/a.mov", SourceApp::SpyglassScan);
        let mascot_shot = insert_shot(&conn, clip, 0.0, 4.0);
        let cheering_shot = insert_shot(&conn, clip, 4.0, 8.0);
        let neither_shot = insert_shot(&conn, clip, 8.0, 12.0);
        tag_shot(&conn, mascot_shot, "mascot");
        tag_shot(&conn, cheering_shot, "cheering");

        let filters = FacetFilters { tags: vec!["mascot".into(), "cheering".into()], ..Default::default() };
        let allowed = matching_shot_ids(&conn, &filters).unwrap().unwrap();
        assert!(allowed.contains(&mascot_shot));
        assert!(allowed.contains(&cheering_shot));
        assert!(!allowed.contains(&neither_shot));

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn source_and_tag_filters_combine_with_and() {
        let (db, path) = open_scratch_db("source_and_tag");
        let conn = db.conn.lock().unwrap();
        let card_eater_clip = insert_clip(&conn, "/Volumes/Archive/b.mov", SourceApp::CardEater);
        let scan_clip = insert_clip(&conn, "/Volumes/Archive/c.mov", SourceApp::SpyglassScan);
        let matching_shot = insert_shot(&conn, card_eater_clip, 0.0, 4.0);
        let wrong_source_shot = insert_shot(&conn, scan_clip, 0.0, 4.0);
        let wrong_tag_shot = insert_shot(&conn, card_eater_clip, 4.0, 8.0);
        tag_shot(&conn, matching_shot, "mascot");
        tag_shot(&conn, wrong_source_shot, "mascot");

        let filters =
            FacetFilters { tags: vec!["mascot".into()], source_app: Some("card_eater".into()), ..Default::default() };
        let allowed = matching_shot_ids(&conn, &filters).unwrap().unwrap();
        assert!(allowed.contains(&matching_shot));
        assert!(!allowed.contains(&wrong_source_shot), "wrong source_app must be excluded even with a matching tag");
        assert!(!allowed.contains(&wrong_tag_shot), "wrong tag must be excluded even with a matching source_app");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn date_range_filter_is_inclusive_on_both_ends() {
        let (db, path) = open_scratch_db("date_range");
        let conn = db.conn.lock().unwrap();
        let early_clip = insert_clip(&conn, "/Volumes/Archive/early.mov", SourceApp::SpyglassScan);
        let mid_clip = insert_clip(&conn, "/Volumes/Archive/mid.mov", SourceApp::SpyglassScan);
        let late_clip = insert_clip(&conn, "/Volumes/Archive/late.mov", SourceApp::SpyglassScan);
        set_ingested_at(&conn, early_clip, "2025-09-01");
        set_ingested_at(&conn, mid_clip, "2025-10-15");
        set_ingested_at(&conn, late_clip, "2025-12-01");
        let early_shot = insert_shot(&conn, early_clip, 0.0, 4.0);
        let mid_shot = insert_shot(&conn, mid_clip, 0.0, 4.0);
        let late_shot = insert_shot(&conn, late_clip, 0.0, 4.0);

        let filters =
            FacetFilters { date_from: Some("2025-10-01".into()), date_to: Some("2025-10-31".into()), ..Default::default() };
        let allowed = matching_shot_ids(&conn, &filters).unwrap().unwrap();
        assert!(!allowed.contains(&early_shot));
        assert!(allowed.contains(&mid_shot));
        assert!(!allowed.contains(&late_shot));

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn favorites_only_filter_excludes_unfavorited_shots() {
        let (db, path) = open_scratch_db("favorites_only");
        let conn = db.conn.lock().unwrap();
        let clip = insert_clip(&conn, "/Volumes/Archive/fav.mov", SourceApp::SpyglassScan);
        let favorited = insert_shot(&conn, clip, 0.0, 4.0);
        let unfavorited = insert_shot(&conn, clip, 4.0, 8.0);
        db::set_shot_favorite(&conn, favorited, true).unwrap();

        let filters = FacetFilters { favorites_only: true, ..Default::default() };
        let allowed = matching_shot_ids(&conn, &filters).unwrap().unwrap();
        assert!(allowed.contains(&favorited));
        assert!(!allowed.contains(&unfavorited));

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn folder_path_filter_matches_the_folder_and_every_subfolder_but_not_siblings() {
        let (db, path) = open_scratch_db("folder_path");
        let conn = db.conn.lock().unwrap();
        let direct_clip = insert_clip(&conn, "/Volumes/Archive/2025/game.mov", SourceApp::SpyglassScan);
        let nested_clip = insert_clip(&conn, "/Volumes/Archive/2025/fall/practice.mov", SourceApp::SpyglassScan);
        let sibling_clip = insert_clip(&conn, "/Volumes/Archive/2024/game.mov", SourceApp::SpyglassScan);
        let prefix_collision_clip = insert_clip(&conn, "/Volumes/Archive2/game.mov", SourceApp::SpyglassScan);
        let direct_shot = insert_shot(&conn, direct_clip, 0.0, 4.0);
        let nested_shot = insert_shot(&conn, nested_clip, 0.0, 4.0);
        let sibling_shot = insert_shot(&conn, sibling_clip, 0.0, 4.0);
        let collision_shot = insert_shot(&conn, prefix_collision_clip, 0.0, 4.0);

        let filters = FacetFilters { folder_path: Some("/Volumes/Archive/2025".into()), ..Default::default() };
        let allowed = matching_shot_ids(&conn, &filters).unwrap().unwrap();
        assert!(allowed.contains(&direct_shot));
        assert!(allowed.contains(&nested_shot), "subfolders of the selected folder must match too");
        assert!(!allowed.contains(&sibling_shot));
        assert!(!allowed.contains(&collision_shot), "a same-prefix sibling root must not false-match");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn folder_path_filter_matches_through_a_finder_alias_to_a_different_volume() {
        // The other half of the Athletics bug: `folders::list_folder_children`
        // now surfaces an aliased folder as a node, but selecting it has to
        // actually filter down to its clips too, even though they live on
        // a volume with no path relationship to the watched root at all.
        let (db, path) = open_scratch_db("folder_path_alias");
        let conn = db.conn.lock().unwrap();
        crate::db::upsert_alias_link(&conn, "/Volumes/Root/Athletics", "/Volumes/OtherDrive/Athletics").unwrap();
        let aliased_clip = insert_clip(&conn, "/Volumes/OtherDrive/Athletics/Fall/game.mov", SourceApp::SpyglassScan);
        let sibling_clip = insert_clip(&conn, "/Volumes/Root/Academics/lecture.mov", SourceApp::SpyglassScan);
        let aliased_shot = insert_shot(&conn, aliased_clip, 0.0, 4.0);
        let sibling_shot = insert_shot(&conn, sibling_clip, 0.0, 4.0);

        let filters = FacetFilters { folder_path: Some("/Volumes/Root/Athletics".into()), ..Default::default() };
        let allowed = matching_shot_ids(&conn, &filters).unwrap().unwrap();
        assert!(allowed.contains(&aliased_shot), "the apparent folder_path must resolve through the alias link");
        assert!(!allowed.contains(&sibling_shot));

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn browse_shots_orders_newest_first_and_respects_filters_and_limit() {
        let (db, path) = open_scratch_db("browse");
        let conn = db.conn.lock().unwrap();
        let clip_a = insert_clip(&conn, "/Volumes/Archive/browse_a.mov", SourceApp::SpyglassScan);
        let clip_b = insert_clip(&conn, "/Volumes/Archive/browse_b.mov", SourceApp::SpyglassScan);
        set_ingested_at(&conn, clip_a, "2025-09-01");
        set_ingested_at(&conn, clip_b, "2025-11-01");
        let older_shot = insert_shot(&conn, clip_a, 0.0, 4.0);
        let newer_shot = insert_shot(&conn, clip_b, 0.0, 4.0);
        tag_shot(&conn, newer_shot, "mascot");

        let all = browse_shots(&conn, &FacetFilters::default(), 10).unwrap();
        assert_eq!(all.len(), 2);
        assert_eq!(all[0].shot_id, newer_shot, "newest-ingested clip's shot must sort first");
        assert_eq!(all[1].shot_id, older_shot);

        let filtered = browse_shots(&conn, &FacetFilters { tags: vec!["mascot".into()], ..Default::default() }, 10).unwrap();
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].shot_id, newer_shot);

        let limited = browse_shots(&conn, &FacetFilters::default(), 1).unwrap();
        assert_eq!(limited.len(), 1);
        assert_eq!(limited[0].shot_id, newer_shot);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn browse_shots_oldest_first_reverses_the_default_order() {
        let (db, path) = open_scratch_db("browse_oldest");
        let conn = db.conn.lock().unwrap();
        let clip_a = insert_clip(&conn, "/Volumes/Archive/browse_oldest_a.mov", SourceApp::SpyglassScan);
        let clip_b = insert_clip(&conn, "/Volumes/Archive/browse_oldest_b.mov", SourceApp::SpyglassScan);
        set_ingested_at(&conn, clip_a, "2025-09-01");
        set_ingested_at(&conn, clip_b, "2025-11-01");
        let older_shot = insert_shot(&conn, clip_a, 0.0, 4.0);
        let newer_shot = insert_shot(&conn, clip_b, 0.0, 4.0);

        let results = browse_shots(&conn, &FacetFilters { sort_by: SortBy::OldestFirst, ..Default::default() }, 10).unwrap();
        assert_eq!(results[0].shot_id, older_shot, "oldest-ingested clip's shot must sort first");
        assert_eq!(results[1].shot_id, newer_shot);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn browse_shots_highest_quality_sorts_scored_shots_first_and_unscored_last() {
        let (db, path) = open_scratch_db("browse_quality");
        let conn = db.conn.lock().unwrap();
        let clip = insert_clip(&conn, "/Volumes/Archive/browse_quality.mov", SourceApp::SpyglassScan);
        let low = insert_shot(&conn, clip, 0.0, 4.0);
        let high = insert_shot(&conn, clip, 4.0, 8.0);
        let unscored = insert_shot(&conn, clip, 8.0, 12.0);
        conn.execute("UPDATE shots SET technical_quality_score = 0.3 WHERE id = ?1", params![low]).unwrap();
        conn.execute("UPDATE shots SET technical_quality_score = 0.9 WHERE id = ?1", params![high]).unwrap();

        let results =
            browse_shots(&conn, &FacetFilters { sort_by: SortBy::HighestQuality, ..Default::default() }, 10).unwrap();
        assert_eq!(results[0].shot_id, high, "the higher technical_quality_score must sort first");
        assert_eq!(results[1].shot_id, low);
        assert_eq!(results[2].shot_id, unscored, "a shot with no score yet must sort last, not be dropped");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn list_facet_options_reports_counts_and_date_bounds() {
        let (db, path) = open_scratch_db("options");
        let conn = db.conn.lock().unwrap();
        let clip_a = insert_clip(&conn, "/Volumes/Archive/opts_a.mov", SourceApp::CardEater);
        let clip_b = insert_clip(&conn, "/Volumes/Archive/opts_b.mov", SourceApp::SpyglassScan);
        set_ingested_at(&conn, clip_a, "2025-09-01");
        set_ingested_at(&conn, clip_b, "2025-11-01");
        let shot_a = insert_shot(&conn, clip_a, 0.0, 4.0);
        let shot_b = insert_shot(&conn, clip_b, 0.0, 4.0);
        tag_shot(&conn, shot_a, "mascot");
        tag_shot(&conn, shot_b, "mascot");
        tag_shot(&conn, shot_b, "cheering");

        let options = list_facet_options(&conn).unwrap();
        let mascot = options.tags.iter().find(|t| t.label == "mascot").unwrap();
        assert_eq!(mascot.shot_count, 2);
        let cheering = options.tags.iter().find(|t| t.label == "cheering").unwrap();
        assert_eq!(cheering.shot_count, 1);
        assert_eq!(options.sources.len(), 2);
        assert_eq!(options.earliest_date.as_deref(), Some("2025-09-01"));
        assert_eq!(options.latest_date.as_deref(), Some("2025-11-01"));

        drop(conn);
        std::fs::remove_file(&path).ok();
    }
}
