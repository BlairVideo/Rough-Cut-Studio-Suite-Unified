use crate::models::{
    AccessLevel, Clip, GapFillJob, GapFillProgress, JobStatus, NewClip, NewTranscriptSegment,
    NewWatchedRoot, ResetWatchedRootResult, ShotReanalysisResult, SourceApp, TranscriptSearchResult,
    TranscriptSegment, WatchedRoot,
};
use rusqlite::{params, Connection, OptionalExtension};
use rusqlite_migration::{Migrations, M};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub struct Db {
    pub conn: Mutex<Connection>,
    /// The file this connection was opened against -- needed by the backup/
    /// restore tooling (Section 17/18) to locate the live index file without
    /// re-deriving it from the app-data dir a second time.
    pub path: PathBuf,
}

fn migrations() -> Migrations<'static> {
    Migrations::new(vec![
        M::up(include_str!("../migrations/001_initial.sql")),
        M::up(include_str!("../migrations/002_gap_fill_jobs.sql")),
        M::up(include_str!("../migrations/003_add_shot_caption.sql")),
        M::up(include_str!("../migrations/004_add_clip_frame_rate.sql")),
        M::up(include_str!("../migrations/005_unique_tag_per_shot.sql")),
        M::up(include_str!("../migrations/006_add_shot_scrub_frames.sql")),
        M::up(include_str!("../migrations/007_unique_watched_root_path.sql")),
        M::up(include_str!("../migrations/008_drop_shot_scrub_frames.sql")),
        M::up(include_str!("../migrations/009_add_caption_hub_score.sql")),
        M::up(include_str!("../migrations/010_add_shot_favorite.sql")),
        M::up(include_str!("../migrations/011_add_alias_links.sql")),
    ])
}

impl Db {
    /// Opens (creating if necessary) `spyglass_index.sqlite` in the given
    /// app-data directory, running migrations.
    pub fn open(app_data_dir: &Path) -> rusqlite::Result<Self> {
        std::fs::create_dir_all(app_data_dir).ok();
        let db_path = app_data_dir.join("spyglass_index.sqlite");
        Self::open_at(&db_path)
    }

    /// Opens a database at an exact file path, running migrations. Split
    /// out from `open` so tests can point at a throwaway file directly.
    pub fn open_at(db_path: &Path) -> rusqlite::Result<Self> {
        let mut conn = Connection::open(db_path)?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.pragma_update(None, "busy_timeout", 2000)?;
        // WAL instead of the default rollback journal: every gap-fill
        // commit and scan insert previously did two fsyncs (journal write +
        // main file write), WAL collapses that to one. `synchronous =
        // NORMAL` is the pairing SQLite's own docs recommend with WAL --
        // safe against app crashes (only a hard power-loss mid-checkpoint
        // could lose the last few commits, and this runs on a single local
        // machine, not a server that gets kill -9'd routinely).
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        migrations()
            .to_latest(&mut conn)
            .expect("failed to run database migrations");
        Ok(Self {
            conn: Mutex::new(conn),
            path: db_path.to_path_buf(),
        })
    }
}

// ---------------------------------------------------------------------------
// Clips
// ---------------------------------------------------------------------------

fn row_to_clip(row: &rusqlite::Row) -> rusqlite::Result<Clip> {
    let source_app_str: String = row.get(2)?;
    Ok(Clip {
        id: row.get(0)?,
        file_path: row.get(1)?,
        source_app: SourceApp::from_str(&source_app_str),
        checksum: row.get(3)?,
        size_bytes: row.get(4)?,
        duration_sec: row.get(5)?,
        ingested_at: row.get(6)?,
        frame_rate: row.get(7)?,
    })
}

const CLIP_COLUMNS: &str =
    "id, file_path, source_app, checksum, size_bytes, duration_sec, ingested_at, frame_rate";

/// Inserts a clip if `file_path` isn't already registered; otherwise leaves
/// the existing row untouched and returns it. This is the dedup contract
/// from Section 7/9: registration never duplicates or clobbers a row an
/// adapter or the scanner has already created for the same path.
pub fn upsert_clip(conn: &Connection, new_clip: &NewClip) -> rusqlite::Result<Clip> {
    conn.execute(
        "INSERT INTO clips (file_path, source_app, checksum, size_bytes, duration_sec)
         VALUES (?1, ?2, ?3, ?4, ?5)
         ON CONFLICT(file_path) DO NOTHING",
        params![
            new_clip.file_path,
            new_clip.source_app.as_str(),
            new_clip.checksum,
            new_clip.size_bytes,
            new_clip.duration_sec,
        ],
    )?;
    let sql = format!("SELECT {CLIP_COLUMNS} FROM clips WHERE file_path = ?1");
    conn.query_row(&sql, params![new_clip.file_path], row_to_clip)
}

pub fn find_clip_by_path(conn: &Connection, file_path: &str) -> rusqlite::Result<Option<Clip>> {
    let sql = format!("SELECT {CLIP_COLUMNS} FROM clips WHERE file_path = ?1");
    conn.query_row(&sql, params![file_path], row_to_clip)
        .optional()
}

pub fn find_clip_by_id(conn: &Connection, id: i64) -> rusqlite::Result<Option<Clip>> {
    let sql = format!("SELECT {CLIP_COLUMNS} FROM clips WHERE id = ?1");
    conn.query_row(&sql, params![id], row_to_clip).optional()
}

/// The oldest clip row (by id) with this exact BLAKE3 checksum, if any --
/// used by the scanner to recognize the same content reappearing at a new
/// path (an archive-drive move) rather than registering it as an unrelated
/// new clip and losing every shot/embedding/tag already built on it.
pub fn find_clip_by_checksum(conn: &Connection, checksum: &str) -> rusqlite::Result<Option<Clip>> {
    let sql = format!("SELECT {CLIP_COLUMNS} FROM clips WHERE checksum = ?1 ORDER BY id ASC LIMIT 1");
    conn.query_row(&sql, params![checksum], row_to_clip).optional()
}

/// Repoints an existing clip at a new on-disk path -- e.g. after an annual
/// working-drive-to-archive-drive move -- preserving its id and everything
/// keyed to it (shots, embeddings, tags, pool membership) instead of
/// starting gap-fill over from an unrelated new clip row.
pub fn relink_clip_path(conn: &Connection, clip_id: i64, new_path: &str) -> rusqlite::Result<()> {
    conn.execute("UPDATE clips SET file_path = ?1 WHERE id = ?2", params![new_path, clip_id])?;
    Ok(())
}

/// Every currently-registered clip path, loaded in one query so a scan's
/// "already registered?" check is an in-memory set lookup instead of one
/// query (and one connection-lock acquisition) per discovered file. In
/// steady state -- a periodic rescan that finds nothing new -- this is the
/// dominant per-file cost across a watched root with thousands of clips,
/// since it runs on every file on every rescan, not just newly discovered
/// ones (see `scanner::scan_and_register`).
pub fn registered_clip_paths(conn: &Connection) -> rusqlite::Result<std::collections::HashSet<String>> {
    let mut stmt = conn.prepare("SELECT file_path FROM clips")?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
    rows.collect()
}

pub fn list_clips(conn: &Connection) -> rusqlite::Result<Vec<Clip>> {
    let sql = format!("SELECT {CLIP_COLUMNS} FROM clips ORDER BY id ASC");
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map([], row_to_clip)?;
    rows.collect()
}

// ---------------------------------------------------------------------------
// Tags (Section 13's inline tag-correction affordance -- human edits land
// here with source='human', feeding back into search quality over time)
// ---------------------------------------------------------------------------

/// Adds a human-corrected tag to a shot. A no-op if that exact label
/// already exists on the shot (from the VLM pass or an earlier
/// correction) -- `tags(shot_id, label)` is a unique pair.
pub fn add_human_tag(conn: &Connection, shot_id: i64, label: &str) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT OR IGNORE INTO tags (shot_id, label, source, confidence) VALUES (?1, ?2, 'human', NULL)",
        params![shot_id, label],
    )?;
    Ok(())
}

/// Removes a tag from a shot by its exact label, regardless of source --
/// a human correcting a wrong VLM-generated tag needs to be able to
/// remove it, not just add a competing one.
pub fn remove_tag(conn: &Connection, shot_id: i64, label: &str) -> rusqlite::Result<()> {
    conn.execute("DELETE FROM tags WHERE shot_id = ?1 AND label = ?2", params![shot_id, label])?;
    Ok(())
}

/// Retroactive cleanup for tags the VLM pass generated before `TAGS_PROMPT`
/// (apps/spyglass/sidecar/analyze_clip.py) told it not to transcribe
/// on-screen text: deletes every `source = 'spyglass_vlm'` tag whose label
/// contains a digit. A digit in a short subject-matter keyword tag
/// ("mascot", "cheering") essentially never occurs on its own -- it almost
/// always means the model read a jersey number, a scoreboard score/clock,
/// or a year off a sign/lower-third, which is exactly the on-screen-text
/// leak this suite's private-school data-privacy mandate rules out. Uses
/// the identical predicate as the sidecar's own `_parse_tags` filter (any
/// digit anywhere in the label), so this purges exactly what the updated
/// pipeline would now refuse to write in the first place.
///
/// Deliberately scoped to `source = 'spyglass_vlm'` only -- a human who
/// deliberately typed a tag via `add_human_tag` (e.g. a game score they
/// actually want searchable) made that choice consciously; this cleanup
/// only reverses tags the model volunteered without review.
///
/// This does NOT catch a name or other on-screen text that happens to
/// contain no digits (e.g. a lower-third reading a person's name) -- there
/// is no reliable local heuristic to distinguish a proper name from an
/// ordinary subject-matter word after the label has already been
/// lowercased and stored, and guessing would risk deleting legitimate tags
/// ("art class", "chess club"). That class of tag needs either a human
/// pass over the tag list or a future name-detection step; it's out of
/// scope for this deterministic pass.
///
/// A digit-in-label predicate can't be expressed as a plain SQL `LIKE`
/// (would need leading-wildcard `%[0-9]%`, which SQLite doesn't support
/// without a GLOB/REGEXP extension), so this filters candidate rows in
/// Rust and deletes by id -- fine at this table's expected size (tens of
/// thousands of shots at most, per the architecture plan's own scale
/// note), same tradeoff `idx_tags_label` already accepts for other
/// wildcard lookups.
pub fn purge_onscreen_text_tags(conn: &Connection) -> rusqlite::Result<usize> {
    let ids: Vec<i64> = {
        let mut stmt = conn.prepare("SELECT id, label FROM tags WHERE source = 'spyglass_vlm'")?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?;
        rows.filter_map(|row| row.ok())
            .filter(|(_, label)| label.chars().any(|c| c.is_ascii_digit()))
            .map(|(id, _)| id)
            .collect()
    };
    let removed = ids.len();
    for id in ids {
        conn.execute("DELETE FROM tags WHERE id = ?1", params![id])?;
    }
    Ok(removed)
}

/// Retroactive cleanup for tags generated before `TAGS_PROMPT`
/// (sidecar/analyze_clip.py) told the VLM not to describe or count
/// subjects' gender -- deletes every unreviewed `spyglass_vlm` tag
/// containing a whole-word gender/headcount term (a bare "boy"/"girl"/
/// "male"/"female" tag, or a headcount phrase like "two boys"). This
/// suite's private-school data-privacy mandate rules out classifying or
/// filtering shots by the sex of the students in them, regardless of
/// whether the tag also happens to leak on-screen text.
///
/// Word-matched against `label.split_whitespace()`, not a substring check
/// -- a tag like "cowboy hat" or "girlfriend" must survive, same rationale
/// as the sidecar's own `_parse_tags` filter this mirrors. Deliberately
/// scoped to `source = 'spyglass_vlm'` only, same as
/// `purge_onscreen_text_tags`: a human who deliberately typed a tag via
/// `add_human_tag` made that choice consciously.
pub fn purge_gender_tags(conn: &Connection) -> rusqlite::Result<usize> {
    const GENDER_WORDS: [&str; 8] =
        ["boy", "boys", "girl", "girls", "male", "female", "males", "females"];
    let ids: Vec<i64> = {
        let mut stmt = conn.prepare("SELECT id, label FROM tags WHERE source = 'spyglass_vlm'")?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?;
        rows.filter_map(|row| row.ok())
            .filter(|(_, label)| label.split_whitespace().any(|w| GENDER_WORDS.contains(&w)))
            .map(|(id, _)| id)
            .collect()
    };
    let removed = ids.len();
    for id in ids {
        conn.execute("DELETE FROM tags WHERE id = ?1", params![id])?;
    }
    Ok(removed)
}

/// Retroactive cleanup for on-screen-text tags made of ordinary words --
/// the digit filter `purge_onscreen_text_tags` only catches text WITH a
/// digit (jersey numbers, scores); it does nothing for a UI string read off
/// screen-recording footage and quoted back verbatim, e.g. `"boarding now"
/// button` or `"united" logo` (confirmed live, see
/// `sidecar/analyze_clip.py`'s `_looks_like_onscreen_text`, which this
/// mirrors exactly so a tag written before that fix and one written after
/// it are judged by the identical rule). A legitimate short keyword tag
/// never needs a quote character, sentence punctuation, a trailing UI-role
/// word (button/logo/icon/...), or more than `MAX_TAG_WORDS` words -- any
/// of those is treated as the model transcribing the screen rather than
/// describing the scene.
///
/// Deliberately scoped to `source = 'spyglass_vlm'` only, same as the other
/// two purges: a human-typed tag was a conscious choice. Does NOT catch a
/// bare-word on-screen-text tag with none of this structure (e.g. a menu
/// heading transcribed as a plain one-word tag) -- that class needs the
/// sidecar's OCR-token match (`_matches_onscreen_tokens`) run against the
/// keyframe itself at generation time; there is no keyframe access from
/// this retroactive DB-only pass to re-derive it after the fact.
pub fn purge_ui_text_tags(conn: &Connection) -> rusqlite::Result<usize> {
    const UI_ROLE_WORDS: [&str; 15] = [
        "button", "logo", "icon", "banner", "menu", "tab", "link", "label", "header", "heading",
        "screen", "app", "interface", "ad", "sign",
    ];
    const MAX_TAG_WORDS: usize = 4;

    fn looks_like_onscreen_text(label: &str) -> bool {
        if label.contains('"') || label.contains('\u{201c}') || label.contains('\u{201d}') {
            return true;
        }
        if label.contains('!') || label.contains('?') || label.contains("...") {
            return true;
        }
        let words: Vec<&str> = label.split_whitespace().collect();
        if words.len() > MAX_TAG_WORDS {
            return true;
        }
        words.last().is_some_and(|w| UI_ROLE_WORDS.contains(w))
    }

    let ids: Vec<i64> = {
        let mut stmt = conn.prepare("SELECT id, label FROM tags WHERE source = 'spyglass_vlm'")?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?;
        rows.filter_map(|row| row.ok())
            .filter(|(_, label)| looks_like_onscreen_text(label))
            .map(|(id, _)| id)
            .collect()
    };
    let removed = ids.len();
    for id in ids {
        conn.execute("DELETE FROM tags WHERE id = ?1", params![id])?;
    }
    Ok(removed)
}

/// Runs all three retroactive bad-tag purges (`purge_onscreen_text_tags`,
/// `purge_ui_text_tags`, `purge_gender_tags`) and sums the total removed --
/// hoisted here because both hosts (`src-tauri/src/commands.rs`'s
/// `purge_bad_tags` and `crates/spyglass-py/src/lib.rs`'s
/// `purge_onscreen_text_tags` pyfunction) were independently composing the
/// exact same three-call sequence under two different names. See each
/// individual purge function's own doc comment for its specific rationale
/// and scope limits.
pub fn purge_bad_tags(conn: &Connection) -> rusqlite::Result<usize> {
    let onscreen_text = purge_onscreen_text_tags(conn)?;
    let ui_text = purge_ui_text_tags(conn)?;
    let gender = purge_gender_tags(conn)?;
    Ok(onscreen_text + ui_text + gender)
}

// ---------------------------------------------------------------------------
// Clip favoriting -- a quick bookmark distinct from the pool tray
// ---------------------------------------------------------------------------

pub fn set_shot_favorite(conn: &Connection, shot_id: i64, favorite: bool) -> rusqlite::Result<()> {
    conn.execute("UPDATE shots SET is_favorite = ?2 WHERE id = ?1", params![shot_id, favorite])?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Transcript segments (+ FTS5 keyword search)
// ---------------------------------------------------------------------------

pub fn insert_transcript_segment(
    conn: &Connection,
    seg: &NewTranscriptSegment,
) -> rusqlite::Result<i64> {
    conn.execute(
        "INSERT INTO transcript_segments
            (clip_id, start_tc, end_tc, speaker, text, avg_logprob, no_speech_prob)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            seg.clip_id,
            seg.start_tc,
            seg.end_tc,
            seg.speaker,
            seg.text,
            seg.avg_logprob,
            seg.no_speech_prob,
        ],
    )?;
    Ok(conn.last_insert_rowid())
}

/// FTS5 keyword search over transcript text, joined back to each segment's
/// clip so a hit can jump straight to source (Phase 1's "transcript-keyword
/// search over existing metadata" -- Section 17).
pub fn search_transcripts(
    conn: &Connection,
    query: &str,
    limit: i64,
) -> rusqlite::Result<Vec<TranscriptSearchResult>> {
    let mut stmt = conn.prepare(
        "SELECT ts.id, ts.clip_id, ts.start_tc, ts.end_tc, ts.speaker, ts.text,
                ts.avg_logprob, ts.no_speech_prob, c.file_path
         FROM transcript_segments_fts f
         JOIN transcript_segments ts ON ts.id = f.rowid
         JOIN clips c ON c.id = ts.clip_id
         WHERE f.text MATCH ?1
         ORDER BY rank
         LIMIT ?2",
    )?;
    let rows = stmt.query_map(params![query, limit], |row| {
        Ok(TranscriptSearchResult {
            segment: TranscriptSegment {
                id: row.get(0)?,
                clip_id: row.get(1)?,
                start_tc: row.get(2)?,
                end_tc: row.get(3)?,
                speaker: row.get(4)?,
                text: row.get(5)?,
                avg_logprob: row.get(6)?,
                no_speech_prob: row.get(7)?,
            },
            clip_file_path: row.get(8)?,
        })
    })?;
    rows.collect()
}

// ---------------------------------------------------------------------------
// Watched roots (Section 8 allowlist)
// ---------------------------------------------------------------------------

fn row_to_watched_root(row: &rusqlite::Row) -> rusqlite::Result<WatchedRoot> {
    Ok(WatchedRoot {
        id: row.get(0)?,
        label: row.get(1)?,
        path: row.get(2)?,
        volume_id: row.get(3)?,
        access_level: row.get(4)?,
        approved_by: row.get(5)?,
        approved_at: row.get(6)?,
        last_scanned_at: row.get(7)?,
        sidecar_cache_enabled: row.get::<_, i64>(8)? != 0,
    })
}

const WATCHED_ROOT_COLUMNS: &str = "id, label, path, volume_id, access_level, approved_by, approved_at, last_scanned_at, sidecar_cache_enabled";

/// Adding a path that already has a row (typically one the user previously
/// removed -- see `idx_watched_roots_path`) reactivates that same row
/// instead of inserting a second one alongside it. Without this, removing
/// and later re-adding the same folder left the old `removed` row behind
/// forever as dead weight in the list, in addition to the new one.
pub fn add_watched_root(conn: &Connection, root: &NewWatchedRoot) -> rusqlite::Result<WatchedRoot> {
    conn.execute(
        "INSERT INTO watched_roots (label, path, volume_id, approved_by)
         VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(path) DO UPDATE SET
             label = excluded.label,
             volume_id = excluded.volume_id,
             approved_by = excluded.approved_by,
             access_level = 'active'",
        params![root.label, root.path, root.volume_id, root.approved_by],
    )?;
    let sql = format!("SELECT {WATCHED_ROOT_COLUMNS} FROM watched_roots WHERE path = ?1");
    conn.query_row(&sql, params![root.path], row_to_watched_root)
}

pub fn list_watched_roots(conn: &Connection) -> rusqlite::Result<Vec<WatchedRoot>> {
    let sql = format!("SELECT {WATCHED_ROOT_COLUMNS} FROM watched_roots ORDER BY id ASC");
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map([], row_to_watched_root)?;
    rows.collect()
}

/// Watched roots the Settings panel should actually list and let the user
/// act on (Scan/Pause/Remove) -- every row except a `removed` tombstone.
/// `remove_watched_root` deliberately never deletes its row (see that
/// function's doc comment): it has to persist so
/// `effectively_removed_watched_root_paths` can keep excluding its path
/// from a scan until some broader *active* root reclaims it. That
/// persistence is a scanning-time implementation detail, not something the
/// user should keep seeing in the watched-folder list after they've
/// already removed it -- `list_watched_roots` stays the raw accessor other
/// internal callers need (rescan scheduling, "Scan now" by id, re-add
/// reactivation, tests); this is the display-facing view over it.
pub fn list_visible_watched_roots(conn: &Connection) -> rusqlite::Result<Vec<WatchedRoot>> {
    Ok(list_watched_roots(conn)?
        .into_iter()
        .filter(|r| r.access_level != "removed")
        .collect())
}

/// Paths of every root a user has explicitly removed (Section 8), with no
/// regard for any other root's current state. Prefer
/// `effectively_removed_watched_root_paths` for actually gating clip
/// registration -- this raw list is the primitive that function is defined
/// against, not something any host currently calls directly.
pub(crate) fn removed_watched_root_paths(conn: &Connection) -> rusqlite::Result<Vec<String>> {
    let mut stmt = conn.prepare("SELECT path FROM watched_roots WHERE access_level = 'removed'")?;
    let rows = stmt.query_map([], |row| row.get(0))?;
    rows.collect()
}

/// Removed-root paths that should still exclude a file from registration --
/// i.e. `removed_watched_root_paths` with anything now covered by a
/// currently-*active* root filtered back out.
///
/// A removed root is meant to keep excluding its path indefinitely (Section
/// 8) so a later, broader/overlapping watched root doesn't silently walk
/// back over content the user deliberately purged. But confirmed live
/// against a real archive, that assumption breaks for a different, equally
/// real workflow: an annual working-drive-to-archive-drive move, where the
/// user adds a new *active* root over a location that happens to contain a
/// narrower root they'd previously removed for an unrelated reason (or
/// simply reorganized). A removed "Campus Photoshoot 2025" root kept
/// excluding its files from every scan of a newer, broader, active
/// "Activities and Events" root that now legitimately covers it -- files
/// the user very much wanted indexed stayed invisible indefinitely. An
/// active root's current coverage is a stronger, more recent signal of
/// intent than a stale removal, so it wins.
pub fn effectively_removed_watched_root_paths(conn: &Connection) -> rusqlite::Result<Vec<String>> {
    let removed = removed_watched_root_paths(conn)?;
    if removed.is_empty() {
        return Ok(removed);
    }
    let mut stmt = conn.prepare("SELECT path FROM watched_roots WHERE access_level = 'active'")?;
    let active: Vec<String> = stmt.query_map([], |row| row.get(0))?.collect::<Result<_, _>>()?;
    Ok(removed
        .into_iter()
        .filter(|r| !crate::scanner::path_is_under_any(r, &active))
        .collect())
}

pub fn set_watched_root_access_level(
    conn: &Connection,
    id: i64,
    level: AccessLevel,
) -> rusqlite::Result<()> {
    conn.execute(
        "UPDATE watched_roots SET access_level = ?1 WHERE id = ?2",
        params![level.as_str(), id],
    )?;
    Ok(())
}

/// Repoints an existing watched root at a new filesystem location without
/// disturbing its id, label, access level, or any clip already registered
/// under its old path -- the counterpart to the checksum-based per-clip
/// relink (`find_clip_by_checksum`/`relink_clip_path`) for the case where
/// the *whole folder* moved (e.g. a working-drive folder relocated onto an
/// archive drive) rather than being discovered incidentally under some
/// other, already-active root. Clears `last_scanned_at`, since that
/// timestamp described a scan of the old path and would otherwise falsely
/// suggest the new path has already been scanned.
///
/// This only updates the root's own path; it does not touch any clip row.
/// The caller is expected to trigger a rescan of the new path immediately
/// after (the normal "Scan now" flow), which is what actually repoints each
/// clip still pointing at the now-unreachable old path, one checksum match
/// at a time.
pub fn relink_watched_root_path(
    conn: &Connection,
    id: i64,
    new_path: &str,
) -> rusqlite::Result<WatchedRoot> {
    conn.execute(
        "UPDATE watched_roots SET path = ?1, last_scanned_at = NULL WHERE id = ?2",
        params![new_path, id],
    )?;
    let sql = format!("SELECT {WATCHED_ROOT_COLUMNS} FROM watched_roots WHERE id = ?1");
    conn.query_row(&sql, params![id], row_to_watched_root)
}

pub fn touch_watched_root_scanned_at(conn: &Connection, id: i64) -> rusqlite::Result<()> {
    conn.execute(
        "UPDATE watched_roots SET last_scanned_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?1",
        params![id],
    )?;
    Ok(())
}

/// Splits `root_path` into (exact-match value, escaped child-prefix LIKE
/// pattern) for boundary-safe "under this root" queries. A bare
/// `LIKE 'path%'` would let `/Volumes/Archive` swallow a sibling like
/// `/Volumes/Archive2` -- see `scanner::path_is_under_any`'s doc comment for
/// the same hazard at the in-memory layer. Escapes literal `%`/`_`/`\` in
/// the path itself so those characters in a real filesystem path aren't
/// reinterpreted as LIKE wildcards. Pair with `ESCAPE '\'` at each call
/// site: `file_path = ?1 OR file_path LIKE ?2 ESCAPE '\'`.
fn root_path_match_params(root_path: &str) -> (String, String) {
    let root = root_path.trim_end_matches('/');
    let escaped = root
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_");
    (root.to_string(), format!("{escaped}/%"))
}

/// Marks a root `removed` and purges every clip registered under its path
/// (shots/tags/embeddings/transcript_segments cascade via foreign keys) --
/// the destructive half of Section 8's "pause vs. remove, treated
/// differently" policy. Callers are responsible for confirming with the
/// user first; this function performs the purge unconditionally.
pub fn remove_watched_root(conn: &Connection, id: i64) -> rusqlite::Result<()> {
    let path: String = conn.query_row(
        "SELECT path FROM watched_roots WHERE id = ?1",
        params![id],
        |row| row.get(0),
    )?;
    let (exact, child_pattern) = root_path_match_params(&path);
    conn.execute(
        "DELETE FROM clips WHERE file_path = ?1 OR file_path LIKE ?2 ESCAPE '\\'",
        params![exact, child_pattern],
    )?;
    conn.execute(
        "UPDATE watched_roots SET access_level = 'removed' WHERE id = ?1",
        params![id],
    )?;
    Ok(())
}

/// "Start fresh" for a single watched root, without touching any other
/// root's index: purges every clip registered under `id`'s path (shots/
/// tags/embeddings/transcript_segments/gap_fill_jobs all cascade via
/// foreign keys, same as `remove_watched_root`) and clears
/// `last_scanned_at` so the next "Scan now" on this root re-discovers and
/// re-analyzes every file as if seeing it for the first time. Unlike
/// `remove_watched_root`, the root's own row is left alone (still
/// `active`/`paused`, not tombstoned to `removed`) -- this is "re-scan
/// this one folder from scratch," not "stop watching it."
///
/// Real motivating case: a VLM prompt bug (`TAGS_PROMPT`'s literal "for
/// example: mascot, cheering, classroom, outdoors" -- see
/// `sidecar/analyze_clip.py`) baked wrong tags into every clip already
/// scanned before the prompt was fixed. Unlike the digit/gender tag bugs
/// (`purge_onscreen_text_tags`/`purge_gender_tags`), those four words are
/// legitimate tag vocabulary -- a real mascot shot should keep that tag --
/// so there's no safe way to purge just the wrong instances by label
/// archive-wide. The only correct fix is re-running the corrected pipeline
/// over the affected folder(s), which needs those clips wiped first so the
/// scanner treats them as new again instead of "already registered."
///
/// Returns the ids of every removed clip so the caller (the Tauri/PyO3
/// layer) can also delete that clip's cached keyframe directory -- see
/// `ResetWatchedRootResult`'s doc comment for why that's not handled here.
pub fn reset_watched_root(conn: &Connection, id: i64) -> rusqlite::Result<ResetWatchedRootResult> {
    let path: String = conn.query_row(
        "SELECT path FROM watched_roots WHERE id = ?1",
        params![id],
        |row| row.get(0),
    )?;
    let (exact, child_pattern) = root_path_match_params(&path);
    let removed_clip_ids: Vec<i64> = {
        let mut stmt =
            conn.prepare("SELECT id FROM clips WHERE file_path = ?1 OR file_path LIKE ?2 ESCAPE '\\'")?;
        let rows = stmt.query_map(params![exact.clone(), child_pattern.clone()], |r| r.get(0))?;
        rows.collect::<Result<_, _>>()?
    };
    conn.execute(
        "DELETE FROM clips WHERE file_path = ?1 OR file_path LIKE ?2 ESCAPE '\\'",
        params![exact, child_pattern],
    )?;
    conn.execute(
        "UPDATE watched_roots SET last_scanned_at = NULL WHERE id = ?1",
        params![id],
    )?;
    Ok(ResetWatchedRootResult { clips_removed: removed_clip_ids.len(), removed_clip_ids })
}

/// Matches `sidecar/analyze_clip.py`'s `MIN_SHOT_DURATION_SEC` -- the
/// scene-cut detector's own minimum shot length. Duplicated here (rather
/// than shared across the Rust/Python process boundary) because this only
/// runs against already-indexed rows on the Rust side; keep the two in
/// sync by hand if that constant ever changes.
pub const MIN_SHOT_DURATION_SEC: f64 = 1.0;

/// Finds every clip with at least one shot shorter than `min_duration_sec`
/// -- the fallout of a since-fixed scene-cut detector sensitivity bug
/// (`sidecar/analyze_clip.py`'s `ContentDetector` used to run with its
/// stock ~0.5s `min_scene_len`, short enough that fast pans, camera
/// flashes, and quick highlight-reel cuts in real event footage routinely
/// registered as their own spurious "shots," each paying the full
/// keyframe/CLIP-embedding/VLM-caption cost for a span too brief to ever
/// be a useful search result or export unit). Returns clip ids only --
/// `requeue_clips_with_short_shots` does the actual repair.
pub fn find_clips_with_short_shots(conn: &Connection, min_duration_sec: f64) -> rusqlite::Result<Vec<i64>> {
    let mut stmt = conn.prepare(
        "SELECT DISTINCT clip_id FROM shots WHERE (end_tc - start_tc) < ?1 ORDER BY clip_id ASC",
    )?;
    let rows = stmt.query_map(params![min_duration_sec], |r| r.get(0))?;
    rows.collect()
}

/// Wipes every shot (and cascading tags/embeddings, both `ON DELETE
/// CASCADE` from `shots`) belonging to each clip in `clip_ids`, clears any
/// stale gap-fill job for that clip, and queues a fresh one -- the same
/// "wipe so the pipeline treats it as needing analysis again" pattern
/// `reset_watched_root` uses for the `TAGS_PROMPT` prompt-echo bug, but
/// scoped to individual clips instead of a whole watched root, since a
/// short-shot clip can be sitting in an otherwise-healthy folder alongside
/// clips that never had the problem. One transaction for the whole batch
/// so a mid-run failure can't leave some clips wiped without a fresh job
/// queued behind them. `clip_id`s already `DELETE FROM gap_fill_jobs`
/// first rather than relying on `enqueue_gap_fill_job_for_clip`'s dedup
/// guard, since a clip whose short-shot job already ran to `done` would
/// otherwise never get a new one queued. Returns which clips were touched
/// so the caller (Tauri layer) can also delete their now-stale cached
/// keyframe directories -- see `ShotReanalysisResult`'s doc comment.
pub fn requeue_clips_with_short_shots(
    conn: &Connection,
    clip_ids: &[i64],
) -> rusqlite::Result<ShotReanalysisResult> {
    if clip_ids.is_empty() {
        return Ok(ShotReanalysisResult::default());
    }

    let tx = conn.unchecked_transaction()?;
    for &clip_id in clip_ids {
        tx.execute("DELETE FROM shots WHERE clip_id = ?1", params![clip_id])?;
        tx.execute("DELETE FROM gap_fill_jobs WHERE clip_id = ?1", params![clip_id])?;
        tx.execute("INSERT INTO gap_fill_jobs (clip_id) VALUES (?1)", params![clip_id])?;
    }
    tx.commit()?;

    Ok(ShotReanalysisResult {
        clips_requeued: clip_ids.len(),
        requeued_clip_ids: clip_ids.to_vec(),
    })
}

// ---------------------------------------------------------------------------
// Alias links (Finder-alias redirection recorded during a scan, so the
// folder tree/folder_path filter can browse an aliased subtree without
// needing its volume mounted or its watched root's path to relate to it)
// ---------------------------------------------------------------------------

/// Records (or updates, if the alias was re-scanned and now points
/// somewhere new) one Finder-alias boundary crossing. Called once per
/// resolved directory alias by `scanner::scan_and_register` -- see
/// `scanner::AliasLink`.
pub fn upsert_alias_link(conn: &Connection, apparent_path: &str, real_path: &str) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT INTO alias_links (apparent_path, real_path, updated_at)
         VALUES (?1, ?2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
         ON CONFLICT(apparent_path) DO UPDATE SET
             real_path = excluded.real_path,
             updated_at = excluded.updated_at",
        params![apparent_path, real_path],
    )?;
    Ok(())
}

/// Every recorded alias boundary crossing. Small table (one row per
/// Finder alias ever resolved by a scan, not per file), so `folders`'s
/// translation helpers just load all of it and match in Rust rather than
/// fighting SQL `LIKE`'s wildcard characters showing up literally inside
/// a real folder name.
pub fn list_alias_links(conn: &Connection) -> rusqlite::Result<Vec<(String, String)>> {
    let mut stmt = conn.prepare("SELECT apparent_path, real_path FROM alias_links")?;
    let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
    rows.collect()
}

// ---------------------------------------------------------------------------
// Gap-fill job queue (Section 7's persisted background queue)
// ---------------------------------------------------------------------------

fn row_to_job(row: &rusqlite::Row) -> rusqlite::Result<GapFillJob> {
    let status_str: String = row.get(2)?;
    Ok(GapFillJob {
        id: row.get(0)?,
        clip_id: row.get(1)?,
        status: JobStatus::from_str(&status_str),
        attempts: row.get(3)?,
        last_error: row.get(4)?,
        queued_at: row.get(5)?,
        updated_at: row.get(6)?,
    })
}

const JOB_COLUMNS: &str = "id, clip_id, status, attempts, last_error, queued_at, updated_at";

/// Inserts a pending job for every clip that has no shots yet and isn't
/// already queued/tracked -- the dedup contract from Section 7 ("no shots
/// yet? run shot detection") applied at enqueue time rather than re-checked
/// per-worker-tick. Returns how many jobs were newly queued.
pub fn enqueue_pending_gap_fill_jobs(conn: &Connection) -> rusqlite::Result<usize> {
    conn.execute(
        "INSERT INTO gap_fill_jobs (clip_id)
         SELECT c.id FROM clips c
         WHERE NOT EXISTS (SELECT 1 FROM shots s WHERE s.clip_id = c.id)
           AND NOT EXISTS (SELECT 1 FROM gap_fill_jobs j WHERE j.clip_id = c.id)",
        [],
    )
}

/// The single-clip counterpart to `enqueue_pending_gap_fill_jobs`'s full-
/// table sweep -- called right after one clip is registered during a scan
/// so it can start gap-filling immediately, rather than waiting for the
/// *entire* scan to finish. A watched root can contain a handful of
/// small files alongside a few genuinely huge ones (a multi-hour, multi-
/// camera event master can be tens of GB); since `scan_and_register`
/// discovers files one at a time and only the caller's final sweep used
/// to queue anything, one slow file anywhere in the walk held up gap-fill
/// for every other file the scan had already found, however long that
/// scan still had left to run.
pub fn enqueue_gap_fill_job_for_clip(conn: &Connection, clip_id: i64) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT INTO gap_fill_jobs (clip_id)
         SELECT ?1 WHERE NOT EXISTS (SELECT 1 FROM shots WHERE clip_id = ?1)
           AND NOT EXISTS (SELECT 1 FROM gap_fill_jobs WHERE clip_id = ?1)",
        params![clip_id],
    )?;
    Ok(())
}

pub fn get_job(conn: &Connection, id: i64) -> rusqlite::Result<GapFillJob> {
    let sql = format!("SELECT {JOB_COLUMNS} FROM gap_fill_jobs WHERE id = ?1");
    conn.query_row(&sql, params![id], row_to_job)
}

/// Atomically claims the smallest-by-file-size pending job for processing
/// (falling back to FIFO order within same-size/unknown-size clips). Safe
/// under a single shared connection (the caller holds `Db.conn`'s mutex for
/// the duration) -- the worker loop's concurrency limit governs how many
/// sidecar subprocesses run in parallel, not contention on this query.
///
/// Smallest-first rather than strict `queued_at` FIFO: a multi-hour/multi-GB
/// camera-native master (e.g. a locked-off event recording) can take the
/// *entire* sidecar timeout just to decode for scene detection, without ever
/// producing a shot. With only `MAX_CONCURRENCY` worker slots, a queue
/// ordered by arrival time lets one or two such files occupy every slot for
/// the full timeout, over and over, while hundreds of much-faster ordinary
/// clips sit behind them untouched -- indistinguishable from the whole queue
/// being stuck. Clips with an unknown size (`size_bytes IS NULL`) sort after
/// every known size, not before, so a clip whose size hasn't been recorded
/// yet can't jump ahead of clips already known to be small.
pub fn claim_next_pending_job(conn: &Connection) -> rusqlite::Result<Option<GapFillJob>> {
    let id: Option<i64> = conn
        .query_row(
            "SELECT j.id FROM gap_fill_jobs j JOIN clips c ON c.id = j.clip_id
             WHERE j.status = 'pending'
             ORDER BY (c.size_bytes IS NULL) ASC, c.size_bytes ASC, j.queued_at ASC
             LIMIT 1",
            [],
            |row| row.get(0),
        )
        .optional()?;
    let Some(id) = id else { return Ok(None) };
    conn.execute(
        "UPDATE gap_fill_jobs SET status = 'running', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?1",
        params![id],
    )?;
    get_job(conn, id).map(Some)
}

/// Marks a job done -- but only if it's still `running`. A disconnect can
/// flip a job to `awaiting_reconnect` while its sidecar subprocess is still
/// finishing up in the background (Section 9); when that stale result
/// eventually comes back, it must not clobber the reassigned status.
/// Returns whether the update actually applied.
pub fn mark_job_done(conn: &Connection, job_id: i64) -> rusqlite::Result<bool> {
    let rows = conn.execute(
        "UPDATE gap_fill_jobs SET status = 'done', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
         WHERE id = ?1 AND status = 'running'",
        params![job_id],
    )?;
    Ok(rows > 0)
}

/// Same "only if still running" guard as `mark_job_done`. Returns whether
/// the update actually applied.
pub fn mark_job_failed(conn: &Connection, job_id: i64, error: &str) -> rusqlite::Result<bool> {
    let rows = conn.execute(
        "UPDATE gap_fill_jobs SET status = 'failed', attempts = attempts + 1, last_error = ?2,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?1 AND status = 'running'",
        params![job_id, error],
    )?;
    Ok(rows > 0)
}

/// Resets `failed` jobs back to `pending` (Section 7's "retry failed"
/// action), optionally scoped to clips under one root's path. Returns the
/// number of jobs requeued.
pub fn retry_failed_jobs(conn: &Connection, root_path: Option<&str>) -> rusqlite::Result<usize> {
    match root_path {
        Some(path) => {
            let (exact, child_pattern) = root_path_match_params(path);
            conn.execute(
                "UPDATE gap_fill_jobs SET status = 'pending', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                 WHERE status = 'failed' AND clip_id IN (SELECT id FROM clips WHERE file_path = ?1 OR file_path LIKE ?2 ESCAPE '\\')",
                params![exact, child_pattern],
            )
        }
        None => conn.execute(
            "UPDATE gap_fill_jobs SET status = 'pending', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
             WHERE status = 'failed'",
            [],
        ),
    }
}

/// Resets any job still marked `running` back to `pending`. Only ever
/// meaningful to call once, at startup, before any worker has claimed
/// anything in this process: a `running` row found at that point cannot
/// belong to a job actually in flight right now (nothing has started
/// claiming yet), so it can only be a leftover from a *previous* process
/// that exited (crash, force-quit) mid-analysis without reaching
/// `mark_job_done`/`mark_job_failed`. Without this, such a job stays
/// `running` forever -- `claim_next_pending_job` never looks at it again,
/// and `retry_failed_jobs` only recovers `failed` jobs -- so its clip
/// never gets indexed on any future run, silently capping how far the
/// gap-fill queue can ever get. Returns how many were reset.
pub fn reset_stale_running_jobs(conn: &Connection) -> rusqlite::Result<usize> {
    conn.execute(
        "UPDATE gap_fill_jobs SET status = 'pending', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
         WHERE status = 'running'",
        [],
    )
}

/// Cancels in-flight/pending work for clips under `root_path` when its
/// volume disappears (Section 9: "cancelled cleanly and re-queued as
/// pending, awaiting reconnect" rather than left half-written or retried
/// against a drive that's no longer there).
pub fn mark_jobs_awaiting_reconnect(conn: &Connection, root_path: &str) -> rusqlite::Result<usize> {
    let (exact, child_pattern) = root_path_match_params(root_path);
    conn.execute(
        "UPDATE gap_fill_jobs SET status = 'awaiting_reconnect', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
         WHERE status IN ('pending', 'running') AND clip_id IN (SELECT id FROM clips WHERE file_path = ?1 OR file_path LIKE ?2 ESCAPE '\\')",
        params![exact, child_pattern],
    )
}

/// The other half of Section 9's reconnect flow: when the volume reappears,
/// resume the queue where it left off.
pub fn requeue_jobs_on_reconnect(conn: &Connection, root_path: &str) -> rusqlite::Result<usize> {
    let (exact, child_pattern) = root_path_match_params(root_path);
    conn.execute(
        "UPDATE gap_fill_jobs SET status = 'pending', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
         WHERE status = 'awaiting_reconnect' AND clip_id IN (SELECT id FROM clips WHERE file_path = ?1 OR file_path LIKE ?2 ESCAPE '\\')",
        params![exact, child_pattern],
    )
}

/// Per-root status panel numbers (Section 7).
pub fn gap_fill_progress_for_root(conn: &Connection, root_path: &str) -> rusqlite::Result<GapFillProgress> {
    let (exact, child_pattern) = root_path_match_params(root_path);
    conn.query_row(
        "SELECT
            (SELECT COUNT(*) FROM clips WHERE file_path = ?1 OR file_path LIKE ?2 ESCAPE '\\') AS discovered,
            (SELECT COUNT(*) FROM clips c WHERE (c.file_path = ?1 OR c.file_path LIKE ?2 ESCAPE '\\')
                AND EXISTS (SELECT 1 FROM shots s WHERE s.clip_id = c.id)) AS indexed,
            (SELECT COUNT(*) FROM gap_fill_jobs j JOIN clips c ON c.id = j.clip_id
                WHERE (c.file_path = ?1 OR c.file_path LIKE ?2 ESCAPE '\\') AND j.status IN ('pending', 'running')) AS queued,
            (SELECT COUNT(*) FROM gap_fill_jobs j JOIN clips c ON c.id = j.clip_id
                WHERE (c.file_path = ?1 OR c.file_path LIKE ?2 ESCAPE '\\') AND j.status = 'failed') AS failed,
            (SELECT COUNT(*) FROM gap_fill_jobs j JOIN clips c ON c.id = j.clip_id
                WHERE (c.file_path = ?1 OR c.file_path LIKE ?2 ESCAPE '\\') AND j.status = 'awaiting_reconnect') AS awaiting_reconnect",
        params![exact, child_pattern],
        |row| {
            Ok(GapFillProgress {
                discovered: row.get(0)?,
                indexed: row.get(1)?,
                queued: row.get(2)?,
                failed: row.get(3)?,
                awaiting_reconnect: row.get(4)?,
            })
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn open_scratch_db(tag: &str) -> (Db, std::path::PathBuf) {
        let n = TMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!("spyglass_db_test_{tag}_{n}.sqlite3"));
        std::fs::remove_file(&path).ok();
        let db = Db::open_at(&path).expect("open scratch db");
        (db, path)
    }

    #[test]
    fn add_human_tag_is_idempotent_and_remove_tag_deletes_by_label() {
        let (db, path) = open_scratch_db("human_tags");
        let conn = db.conn.lock().unwrap();

        let clip = upsert_clip(&conn, &new_clip("/Volumes/Archive/tagtest.mov")).unwrap();
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 4.0)",
            params![clip.id],
        )
        .unwrap();
        let shot_id = conn.last_insert_rowid();

        add_human_tag(&conn, shot_id, "mascot").unwrap();
        add_human_tag(&conn, shot_id, "mascot").unwrap(); // re-adding the same label is a no-op

        let labels: Vec<String> = {
            let mut stmt = conn.prepare("SELECT label FROM tags WHERE shot_id = ?1").unwrap();
            stmt.query_map(params![shot_id], |r| r.get(0)).unwrap().map(|r| r.unwrap()).collect()
        };
        assert_eq!(labels, vec!["mascot"]);

        remove_tag(&conn, shot_id, "mascot").unwrap();
        let remaining: i64 = conn.query_row("SELECT COUNT(*) FROM tags WHERE shot_id = ?1", params![shot_id], |r| r.get(0)).unwrap();
        assert_eq!(remaining, 0);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn add_human_tag_does_not_collide_with_an_existing_vlm_tag_of_the_same_label() {
        let (db, path) = open_scratch_db("human_tag_vlm_collision");
        let conn = db.conn.lock().unwrap();

        let clip = upsert_clip(&conn, &new_clip("/Volumes/Archive/collision.mov")).unwrap();
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 4.0)",
            params![clip.id],
        )
        .unwrap();
        let shot_id = conn.last_insert_rowid();

        conn.execute(
            "INSERT INTO tags (shot_id, label, source) VALUES (?1, 'mascot', 'spyglass_vlm')",
            params![shot_id],
        )
        .unwrap();

        // A human confirming a tag the VLM already produced must not error
        // (unique index on shot_id+label) -- it's a no-op, same as re-adding
        // an identical human tag.
        add_human_tag(&conn, shot_id, "mascot").unwrap();
        let count: i64 = conn.query_row("SELECT COUNT(*) FROM tags WHERE shot_id = ?1", params![shot_id], |r| r.get(0)).unwrap();
        assert_eq!(count, 1);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn purge_onscreen_text_tags_removes_only_digit_containing_vlm_tags() {
        let (db, path) = open_scratch_db("purge_onscreen_text");
        let conn = db.conn.lock().unwrap();

        let clip = upsert_clip(&conn, &new_clip("/Volumes/Archive/purge_test.mov")).unwrap();
        conn.execute("INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 4.0)", params![clip.id]).unwrap();
        let shot_id = conn.last_insert_rowid();

        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, 'mascot', 'spyglass_vlm')", params![shot_id]).unwrap();
        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, 'no23', 'spyglass_vlm')", params![shot_id]).unwrap();
        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, '42-10', 'spyglass_vlm')", params![shot_id]).unwrap();
        // A human deliberately typing a score/number tag is a conscious
        // choice, not an unreviewed VLM leak -- must survive the purge.
        // (A different label than the VLM tags above: `tags(shot_id, label)`
        // is a unique pair regardless of source, so a human correction
        // sharing a VLM tag's exact label is a no-op onto that same row --
        // this test needs its own label to prove a human-sourced digit tag
        // specifically, not just re-hit that no-op path.)
        add_human_tag(&conn, shot_id, "class of 2030").unwrap();

        let removed = purge_onscreen_text_tags(&conn).unwrap();
        assert_eq!(removed, 2, "only the two digit-containing spyglass_vlm tags should be purged");

        let remaining: Vec<(String, String)> = {
            let mut stmt = conn.prepare("SELECT label, source FROM tags WHERE shot_id = ?1 ORDER BY label").unwrap();
            stmt.query_map(params![shot_id], |r| Ok((r.get(0)?, r.get(1)?))).unwrap().map(|r| r.unwrap()).collect()
        };
        assert_eq!(
            remaining,
            vec![("class of 2030".to_string(), "human".to_string()), ("mascot".to_string(), "spyglass_vlm".to_string())]
        );

        // Idempotent -- a second run finds nothing left to purge.
        assert_eq!(purge_onscreen_text_tags(&conn).unwrap(), 0);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn purge_gender_tags_removes_only_gender_and_headcount_vlm_tags() {
        let (db, path) = open_scratch_db("purge_gender");
        let conn = db.conn.lock().unwrap();

        let clip = upsert_clip(&conn, &new_clip("/Volumes/Archive/purge_gender_test.mov")).unwrap();
        conn.execute("INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 4.0)", params![clip.id]).unwrap();
        let shot_id = conn.last_insert_rowid();

        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, 'mascot', 'spyglass_vlm')", params![shot_id]).unwrap();
        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, 'boys', 'spyglass_vlm')", params![shot_id]).unwrap();
        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, 'two girls', 'spyglass_vlm')", params![shot_id]).unwrap();
        // Shares a substring with "boy"/"girl" but is a different word --
        // must survive (word-matched, not substring-matched).
        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, 'cowboy hat', 'spyglass_vlm')", params![shot_id]).unwrap();
        // A human deliberately tagging this is a conscious choice, not an
        // unreviewed VLM leak -- must survive the purge (own label so this
        // isn't a no-op onto an existing VLM-sourced row).
        add_human_tag(&conn, shot_id, "girls varsity").unwrap();

        let removed = purge_gender_tags(&conn).unwrap();
        assert_eq!(removed, 2, "only the two gender/headcount spyglass_vlm tags should be purged");

        let remaining: Vec<(String, String)> = {
            let mut stmt = conn.prepare("SELECT label, source FROM tags WHERE shot_id = ?1 ORDER BY label").unwrap();
            stmt.query_map(params![shot_id], |r| Ok((r.get(0)?, r.get(1)?))).unwrap().map(|r| r.unwrap()).collect()
        };
        assert_eq!(
            remaining,
            vec![
                ("cowboy hat".to_string(), "spyglass_vlm".to_string()),
                ("girls varsity".to_string(), "human".to_string()),
                ("mascot".to_string(), "spyglass_vlm".to_string()),
            ]
        );

        // Idempotent -- a second run finds nothing left to purge.
        assert_eq!(purge_gender_tags(&conn).unwrap(), 0);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn purge_ui_text_tags_removes_only_onscreen_text_shaped_vlm_tags() {
        let (db, path) = open_scratch_db("purge_ui_text");
        let conn = db.conn.lock().unwrap();

        let clip = upsert_clip(&conn, &new_clip("/Volumes/Archive/purge_ui_text_test.mov")).unwrap();
        conn.execute("INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 4.0)", params![clip.id]).unwrap();
        let shot_id = conn.last_insert_rowid();

        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, 'mascot', 'spyglass_vlm')", params![shot_id]).unwrap();
        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, '\"boarding now\" button', 'spyglass_vlm')", params![shot_id]).unwrap();
        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, '\"united\" logo', 'spyglass_vlm')", params![shot_id]).unwrap();
        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, 'watch for your group number', 'spyglass_vlm')", params![shot_id]).unwrap();
        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, 'focus on the process... not the result!', 'spyglass_vlm')", params![shot_id]).unwrap();
        // A short, unquoted, unpunctuated multi-word tag must survive.
        conn.execute("INSERT INTO tags (shot_id, label, source) VALUES (?1, 'vatican museums', 'spyglass_vlm')", params![shot_id]).unwrap();
        // A human deliberately typing a quoted tag is a conscious choice,
        // not an unreviewed VLM leak -- must survive the purge.
        add_human_tag(&conn, shot_id, "\"go eagles\" banner").unwrap();

        let removed = purge_ui_text_tags(&conn).unwrap();
        assert_eq!(removed, 4, "the four onscreen-text-shaped spyglass_vlm tags should be purged");

        let remaining: Vec<(String, String)> = {
            let mut stmt = conn.prepare("SELECT label, source FROM tags WHERE shot_id = ?1 ORDER BY label").unwrap();
            stmt.query_map(params![shot_id], |r| Ok((r.get(0)?, r.get(1)?))).unwrap().map(|r| r.unwrap()).collect()
        };
        assert_eq!(
            remaining,
            vec![
                ("\"go eagles\" banner".to_string(), "human".to_string()),
                ("mascot".to_string(), "spyglass_vlm".to_string()),
                ("vatican museums".to_string(), "spyglass_vlm".to_string()),
            ]
        );

        // Idempotent -- a second run finds nothing left to purge.
        assert_eq!(purge_ui_text_tags(&conn).unwrap(), 0);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn set_shot_favorite_toggles_and_defaults_to_unfavorited() {
        let (db, path) = open_scratch_db("shot_favorite");
        let conn = db.conn.lock().unwrap();

        let clip = upsert_clip(&conn, &new_clip("/Volumes/Archive/favtest.mov")).unwrap();
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 4.0)",
            params![clip.id],
        )
        .unwrap();
        let shot_id = conn.last_insert_rowid();

        let is_favorite = |conn: &Connection| -> bool {
            conn.query_row("SELECT is_favorite FROM shots WHERE id = ?1", params![shot_id], |r| r.get(0)).unwrap()
        };
        assert!(!is_favorite(&conn), "a shot must default to unfavorited");

        set_shot_favorite(&conn, shot_id, true).unwrap();
        assert!(is_favorite(&conn));

        set_shot_favorite(&conn, shot_id, false).unwrap();
        assert!(!is_favorite(&conn));

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn upsert_clip_is_idempotent_by_file_path() {
        let (db, path) = open_scratch_db("clip_upsert");
        let conn = db.conn.lock().unwrap();

        let new_clip = NewClip {
            file_path: "/Volumes/Archive/game1.mov".to_string(),
            source_app: SourceApp::SpyglassScan,
            checksum: Some("abc123".to_string()),
            size_bytes: Some(1000),
            duration_sec: None,
        };
        let first = upsert_clip(&conn, &new_clip).unwrap();
        let second = upsert_clip(&conn, &new_clip).unwrap();
        assert_eq!(first.id, second.id, "re-registering the same path must not duplicate");
        assert_eq!(list_clips(&conn).unwrap().len(), 1);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn transcript_fts_search_finds_matching_segments_and_stays_in_sync() {
        let (db, path) = open_scratch_db("transcript_fts");
        let conn = db.conn.lock().unwrap();

        let clip = upsert_clip(
            &conn,
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
            &conn,
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

        insert_transcript_segment(
            &conn,
            &NewTranscriptSegment {
                clip_id: clip.id,
                start_tc: 4.2,
                end_tc: 9.0,
                speaker: Some("Coach Smith".to_string()),
                text: "let's talk about next week's game plan".to_string(),
                avg_logprob: Some(-0.1),
                no_speech_prob: Some(0.02),
            },
        )
        .unwrap();

        let hits = search_transcripts(&conn, "mascot", 10).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].clip_file_path, "/Volumes/Archive/interview1.mov");
        assert!(hits[0].segment.text.contains("mascot"));

        let no_hits = search_transcripts(&conn, "touchdown", 10).unwrap();
        assert!(no_hits.is_empty());

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn remove_watched_root_purges_clips_under_its_path() {
        let (db, path) = open_scratch_db("remove_root");
        let conn = db.conn.lock().unwrap();

        let root = add_watched_root(
            &conn,
            &NewWatchedRoot {
                label: "Legacy Archive".to_string(),
                path: "/Volumes/OldArchive01".to_string(),
                volume_id: Some("VOL-1".to_string()),
                approved_by: Some("palanch@blair.edu".to_string()),
            },
        )
        .unwrap();

        upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/OldArchive01/clip1.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap();
        upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/OtherArchive/clip2.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap();

        remove_watched_root(&conn, root.id).unwrap();

        let remaining = list_clips(&conn).unwrap();
        assert_eq!(remaining.len(), 1);
        assert_eq!(remaining[0].file_path, "/Volumes/OtherArchive/clip2.mov");

        let roots = list_watched_roots(&conn).unwrap();
        assert_eq!(roots[0].access_level, "removed");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn reset_watched_root_purges_clips_under_its_path_but_keeps_the_root_active() {
        let (db, path) = open_scratch_db("reset_root");
        let conn = db.conn.lock().unwrap();

        let root = add_watched_root(&conn, &new_root("Archive", "/Volumes/Archive")).unwrap();

        let purged = upsert_clip(&conn, &new_clip("/Volumes/Archive/clip1.mov")).unwrap();
        upsert_clip(&conn, &new_clip("/Volumes/OtherArchive/clip2.mov")).unwrap();

        let result = reset_watched_root(&conn, root.id).unwrap();

        assert_eq!(result.clips_removed, 1);
        assert_eq!(result.removed_clip_ids, vec![purged.id]);

        let remaining = list_clips(&conn).unwrap();
        assert_eq!(remaining.len(), 1);
        assert_eq!(remaining[0].file_path, "/Volumes/OtherArchive/clip2.mov");

        // Unlike `remove_watched_root`, the root itself stays active (not
        // tombstoned to 'removed') and its scanned-at timestamp is cleared
        // so the next "Scan now" treats every file as new again.
        let roots = list_watched_roots(&conn).unwrap();
        assert_eq!(roots[0].access_level, "active");
        assert_eq!(roots[0].last_scanned_at, None);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn reset_watched_root_does_not_purge_a_sibling_sharing_its_path_prefix() {
        let (db, path) = open_scratch_db("reset_root_sibling_prefix");
        let conn = db.conn.lock().unwrap();

        let root = add_watched_root(&conn, &new_root("Archive", "/Volumes/Archive")).unwrap();
        add_watched_root(&conn, &new_root("Archive2", "/Volumes/Archive2")).unwrap();

        upsert_clip(&conn, &new_clip("/Volumes/Archive/clip.mov")).unwrap();
        upsert_clip(&conn, &new_clip("/Volumes/Archive2/clip.mov")).unwrap();

        reset_watched_root(&conn, root.id).unwrap();

        let remaining = list_clips(&conn).unwrap();
        assert_eq!(remaining.len(), 1, "the sibling's clip must survive");
        assert_eq!(remaining[0].file_path, "/Volumes/Archive2/clip.mov");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn reset_watched_root_cascades_to_shots_tags_and_gap_fill_jobs() {
        let (db, path) = open_scratch_db("reset_root_cascade");
        let conn = db.conn.lock().unwrap();

        let root = add_watched_root(&conn, &new_root("Archive", "/Volumes/Archive")).unwrap();
        let clip = upsert_clip(&conn, &new_clip("/Volumes/Archive/clip.mov")).unwrap();
        enqueue_pending_gap_fill_jobs(&conn).unwrap();
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 4.0)",
            params![clip.id],
        )
        .unwrap();
        let shot_id = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO tags (shot_id, label, source) VALUES (?1, 'mascot', 'spyglass_vlm')",
            params![shot_id],
        )
        .unwrap();

        reset_watched_root(&conn, root.id).unwrap();

        let shots_left: i64 = conn.query_row("SELECT COUNT(*) FROM shots", [], |r| r.get(0)).unwrap();
        let tags_left: i64 = conn.query_row("SELECT COUNT(*) FROM tags", [], |r| r.get(0)).unwrap();
        let jobs_left: i64 = conn.query_row("SELECT COUNT(*) FROM gap_fill_jobs", [], |r| r.get(0)).unwrap();
        assert_eq!(shots_left, 0, "shots must cascade-delete with their clip");
        assert_eq!(tags_left, 0, "tags must cascade-delete with their shot");
        assert_eq!(jobs_left, 0, "gap-fill jobs must cascade-delete with their clip");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn find_clips_with_short_shots_only_returns_clips_with_at_least_one_short_shot() {
        let (db, path) = open_scratch_db("find_short_shots");
        let conn = db.conn.lock().unwrap();

        let short = upsert_clip(&conn, &new_clip("/Volumes/Archive/short.mov")).unwrap();
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 5.0)",
            params![short.id],
        )
        .unwrap();
        conn.execute(
            // A 0.3s sliver -- under the 1.0s threshold below.
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 5.0, 5.3)",
            params![short.id],
        )
        .unwrap();

        let healthy = upsert_clip(&conn, &new_clip("/Volumes/Archive/healthy.mov")).unwrap();
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 5.0)",
            params![healthy.id],
        )
        .unwrap();

        let no_shots_yet = upsert_clip(&conn, &new_clip("/Volumes/Archive/pending.mov")).unwrap();
        let _ = no_shots_yet;

        let flagged = find_clips_with_short_shots(&conn, 1.0).unwrap();
        assert_eq!(flagged, vec![short.id]);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn requeue_clips_with_short_shots_wipes_shots_and_queues_a_fresh_job() {
        let (db, path) = open_scratch_db("requeue_short_shots");
        let conn = db.conn.lock().unwrap();

        let clip = upsert_clip(&conn, &new_clip("/Volumes/Archive/short.mov")).unwrap();
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 0.3)",
            params![clip.id],
        )
        .unwrap();
        let shot_id = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO tags (shot_id, label, source) VALUES (?1, 'mascot', 'spyglass_vlm')",
            params![shot_id],
        )
        .unwrap();
        // A prior job that already ran to completion -- must be cleared so
        // a fresh one gets queued rather than being silently skipped by
        // `enqueue_gap_fill_job_for_clip`'s "already tracked" dedup guard.
        conn.execute("INSERT INTO gap_fill_jobs (clip_id) VALUES (?1)", params![clip.id]).unwrap();
        conn.execute(
            "UPDATE gap_fill_jobs SET status = 'done' WHERE clip_id = ?1",
            params![clip.id],
        )
        .unwrap();

        let result = requeue_clips_with_short_shots(&conn, &[clip.id]).unwrap();
        assert_eq!(result.clips_requeued, 1);
        assert_eq!(result.requeued_clip_ids, vec![clip.id]);

        let shots_left: i64 = conn.query_row("SELECT COUNT(*) FROM shots", [], |r| r.get(0)).unwrap();
        let tags_left: i64 = conn.query_row("SELECT COUNT(*) FROM tags", [], |r| r.get(0)).unwrap();
        assert_eq!(shots_left, 0, "the short shot must be wiped");
        assert_eq!(tags_left, 0, "tags must cascade-delete with their shot");

        let jobs: Vec<String> = conn
            .prepare("SELECT status FROM gap_fill_jobs WHERE clip_id = ?1")
            .unwrap()
            .query_map(params![clip.id], |r| r.get::<_, String>(0))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap();
        assert_eq!(jobs, vec!["pending".to_string()], "exactly one fresh pending job, not the stale done one");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn requeue_clips_with_short_shots_is_a_no_op_for_an_empty_list() {
        let (db, path) = open_scratch_db("requeue_short_shots_empty");
        let conn = db.conn.lock().unwrap();

        let result = requeue_clips_with_short_shots(&conn, &[]).unwrap();
        assert_eq!(result, ShotReanalysisResult::default());

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn list_visible_watched_roots_hides_removed_tombstones_but_list_watched_roots_keeps_them() {
        let (db, path) = open_scratch_db("list_visible_watched_roots");
        let conn = db.conn.lock().unwrap();

        let active = add_watched_root(
            &conn,
            &NewWatchedRoot {
                label: "Fall 2025 Season".to_string(),
                path: "/Volumes/WorkingDrive/Fall2025".to_string(),
                volume_id: None,
                approved_by: None,
            },
        )
        .unwrap();
        let removed = add_watched_root(
            &conn,
            &NewWatchedRoot {
                label: "Old Working Drive".to_string(),
                path: "/Volumes/OldWorkingDrive".to_string(),
                volume_id: None,
                approved_by: None,
            },
        )
        .unwrap();
        remove_watched_root(&conn, removed.id).unwrap();

        let visible = list_visible_watched_roots(&conn).unwrap();
        assert_eq!(visible.len(), 1, "the removed tombstone must not show up in the display list");
        assert_eq!(visible[0].id, active.id);

        let raw = list_watched_roots(&conn).unwrap();
        assert_eq!(raw.len(), 2, "the raw accessor must still return the tombstone for internal callers");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn re_adding_a_removed_roots_path_reactivates_it_instead_of_duplicating_the_row() {
        let (db, path) = open_scratch_db("readd_removed_root");
        let conn = db.conn.lock().unwrap();

        let root = add_watched_root(
            &conn,
            &NewWatchedRoot {
                label: "Fall 2025 Season".to_string(),
                path: "/Volumes/WorkingDrive/Fall2025".to_string(),
                volume_id: Some("VOL-1".to_string()),
                approved_by: Some("palanch@blair.edu".to_string()),
            },
        )
        .unwrap();
        remove_watched_root(&conn, root.id).unwrap();
        assert_eq!(list_watched_roots(&conn).unwrap().len(), 1);

        let readded = add_watched_root(
            &conn,
            &NewWatchedRoot {
                label: "Fall 2025 Season (moved to archive)".to_string(),
                path: "/Volumes/WorkingDrive/Fall2025".to_string(),
                volume_id: Some("VOL-1".to_string()),
                approved_by: Some("palanch@blair.edu".to_string()),
            },
        )
        .unwrap();

        assert_eq!(readded.id, root.id, "the same row is reused, not a new one");
        assert_eq!(readded.access_level, "active");
        assert_eq!(readded.label, "Fall 2025 Season (moved to archive)");

        let roots = list_watched_roots(&conn).unwrap();
        assert_eq!(roots.len(), 1, "must not accumulate a second row for the same path");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn effectively_removed_excludes_only_paths_no_active_root_covers() {
        let (db, path) = open_scratch_db("effectively_removed");
        let conn = db.conn.lock().unwrap();

        let untouched_removed = add_watched_root(
            &conn,
            &NewWatchedRoot {
                label: "Untouched Removed Shoot".to_string(),
                path: "/Volumes/Archive/Untouched".to_string(),
                volume_id: None,
                approved_by: None,
            },
        )
        .unwrap();
        remove_watched_root(&conn, untouched_removed.id).unwrap();

        let reclaimed_removed = add_watched_root(
            &conn,
            &NewWatchedRoot {
                label: "Campus Photoshoot 2025".to_string(),
                path: "/Volumes/Archive/Activities/Campus Photoshoot 2025".to_string(),
                volume_id: None,
                approved_by: None,
            },
        )
        .unwrap();
        remove_watched_root(&conn, reclaimed_removed.id).unwrap();

        // A broader active root added *after* the removal, covering the
        // same path as `reclaimed_removed` but not `untouched_removed`.
        add_watched_root(
            &conn,
            &NewWatchedRoot {
                label: "Activities and Events".to_string(),
                path: "/Volumes/Archive/Activities".to_string(),
                volume_id: None,
                approved_by: None,
            },
        )
        .unwrap();

        let effective = effectively_removed_watched_root_paths(&conn).unwrap();
        assert!(
            effective.contains(&"/Volumes/Archive/Untouched".to_string()),
            "a removed root with no active root over it must keep excluding its path"
        );
        assert!(
            !effective.contains(&"/Volumes/Archive/Activities/Campus Photoshoot 2025".to_string()),
            "a removed root now covered by a broader active root must stop being excluded"
        );

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn find_clip_by_checksum_finds_it_and_relink_clip_path_repoints_it_in_place() {
        let (db, path) = open_scratch_db("checksum_relink");
        let conn = db.conn.lock().unwrap();

        let clip = upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/WorkingDrive/clip1.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: Some("blake3-abc123".to_string()),
                size_bytes: None,
                duration_sec: None,
            },
        )
        .unwrap();

        assert!(find_clip_by_checksum(&conn, "blake3-does-not-exist").unwrap().is_none());
        let found = find_clip_by_checksum(&conn, "blake3-abc123").unwrap().unwrap();
        assert_eq!(found.id, clip.id);

        relink_clip_path(&conn, clip.id, "/Volumes/ArchiveDrive/2025/clip1.mov").unwrap();
        let relinked = find_clip_by_id(&conn, clip.id).unwrap().unwrap();
        assert_eq!(relinked.id, clip.id, "relinking preserves the clip's id");
        assert_eq!(relinked.file_path, "/Volumes/ArchiveDrive/2025/clip1.mov");
        assert_eq!(relinked.checksum, Some("blake3-abc123".to_string()));

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn relink_watched_root_path_repoints_the_root_and_clears_last_scanned_at() {
        let (db, path) = open_scratch_db("relink_watched_root");
        let conn = db.conn.lock().unwrap();

        let root = add_watched_root(
            &conn,
            &NewWatchedRoot {
                label: "Fall Play Footage".to_string(),
                path: "/Volumes/WorkingDrive/Fall Play".to_string(),
                volume_id: None,
                approved_by: None,
            },
        )
        .unwrap();
        touch_watched_root_scanned_at(&conn, root.id).unwrap();
        assert!(list_watched_roots(&conn).unwrap()[0].last_scanned_at.is_some());

        let relinked = relink_watched_root_path(&conn, root.id, "/Volumes/ArchiveDrive/2025/Fall Play").unwrap();
        assert_eq!(relinked.id, root.id, "relinking preserves the root's id");
        assert_eq!(relinked.label, "Fall Play Footage", "relinking preserves the root's label");
        assert_eq!(relinked.path, "/Volumes/ArchiveDrive/2025/Fall Play");
        assert_eq!(
            relinked.last_scanned_at, None,
            "the old path's scan time must not be carried over to the new path"
        );

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    fn new_clip(path: &str) -> NewClip {
        NewClip {
            file_path: path.to_string(),
            source_app: SourceApp::SpyglassScan,
            checksum: None,
            size_bytes: None,
            duration_sec: None,
        }
    }

    #[test]
    fn enqueue_only_covers_clips_missing_shots_and_not_already_queued() {
        let (db, path) = open_scratch_db("enqueue_gap_fill");
        let conn = db.conn.lock().unwrap();

        let needs_gap_fill = upsert_clip(&conn, &new_clip("/Volumes/Archive/needs_shots.mov")).unwrap();
        let already_has_shots = upsert_clip(&conn, &new_clip("/Volumes/Archive/has_shots.mov")).unwrap();
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 4.0)",
            params![already_has_shots.id],
        )
        .unwrap();

        let queued_first_pass = enqueue_pending_gap_fill_jobs(&conn).unwrap();
        assert_eq!(queued_first_pass, 1, "only the clip lacking shots should be queued");

        let job = claim_next_pending_job(&conn).unwrap().unwrap();
        assert_eq!(job.clip_id, needs_gap_fill.id);
        assert_eq!(job.status, JobStatus::Running);

        // Re-running enqueue must not duplicate the already-tracked job.
        let queued_second_pass = enqueue_pending_gap_fill_jobs(&conn).unwrap();
        assert_eq!(queued_second_pass, 0);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn claim_next_pending_job_prefers_the_smaller_clip_even_when_queued_second() {
        let (db, path) = open_scratch_db("claim_smallest_first");
        let conn = db.conn.lock().unwrap();

        // Queued first but huge -- must not block the smaller clip queued
        // right after it, or a multi-GB master would monopolize a worker
        // slot for its whole timeout while quick clips wait behind it.
        let huge = upsert_clip(
            &conn,
            &NewClip { size_bytes: Some(20_000_000_000), ..new_clip("/Volumes/Archive/huge_event_master.mov") },
        )
        .unwrap();
        let small = upsert_clip(
            &conn,
            &NewClip { size_bytes: Some(5_000_000), ..new_clip("/Volumes/Archive/small_broll.mov") },
        )
        .unwrap();
        // Unknown size must sort after every known size, not jump the queue.
        let unknown_size = upsert_clip(&conn, &new_clip("/Volumes/Archive/unknown_size.mov")).unwrap();

        conn.execute("INSERT INTO gap_fill_jobs (clip_id) VALUES (?1)", params![huge.id]).unwrap();
        conn.execute("INSERT INTO gap_fill_jobs (clip_id) VALUES (?1)", params![small.id]).unwrap();
        conn.execute("INSERT INTO gap_fill_jobs (clip_id) VALUES (?1)", params![unknown_size.id]).unwrap();

        let first = claim_next_pending_job(&conn).unwrap().unwrap();
        assert_eq!(first.clip_id, small.id, "the smaller clip must be claimed first despite being queued later");

        let second = claim_next_pending_job(&conn).unwrap().unwrap();
        assert_eq!(second.clip_id, huge.id, "known size, however large, still beats unknown size");

        let third = claim_next_pending_job(&conn).unwrap().unwrap();
        assert_eq!(third.clip_id, unknown_size.id, "unknown size is claimed last");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn reset_stale_running_jobs_requeues_running_jobs_and_leaves_other_statuses_alone() {
        let (db, path) = open_scratch_db("reset_stale_running");
        let conn = db.conn.lock().unwrap();

        let stuck = upsert_clip(&conn, &new_clip("/Volumes/Archive/stuck_mid_analysis.mov")).unwrap();
        let already_done = upsert_clip(&conn, &new_clip("/Volumes/Archive/finished.mov")).unwrap();

        // Simulate a job that finished normally *before* the crash that
        // left `stuck` behind -- must not get touched by the reset. Shots
        // (and its own `done` job row) are added before the enqueue sweep
        // below so that sweep skips it, same as a real completed clip.
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 1.0)",
            params![already_done.id],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO gap_fill_jobs (clip_id, status) VALUES (?1, 'done')",
            params![already_done.id],
        )
        .unwrap();

        // `stuck` is the only clip lacking both shots and a job row right
        // now, so this enqueue sweep + claim can only pick it up --
        // avoids depending on tie-break ordering between two same-instant
        // pending jobs.
        enqueue_pending_gap_fill_jobs(&conn).unwrap();
        let claimed = claim_next_pending_job(&conn).unwrap().unwrap();
        assert_eq!(claimed.clip_id, stuck.id);
        assert_eq!(claimed.status, JobStatus::Running);

        // A clip that was never claimed at all -- added only now, so its
        // own pending job can't be confused with `stuck`'s in the reset
        // count below.
        let untouched = upsert_clip(&conn, &new_clip("/Volumes/Archive/never_claimed.mov")).unwrap();
        enqueue_pending_gap_fill_jobs(&conn).unwrap();

        let reset_count = reset_stale_running_jobs(&conn).unwrap();
        assert_eq!(reset_count, 1, "only the one running job should be reset");

        let stuck_job = get_job(&conn, claimed.id).unwrap();
        assert_eq!(stuck_job.status, JobStatus::Pending, "the crashed job must be reclaimable again");

        let untouched_status: String = conn
            .query_row("SELECT status FROM gap_fill_jobs WHERE clip_id = ?1", params![untouched.id], |r| r.get(0))
            .unwrap();
        assert_eq!(untouched_status, "pending", "a job that was never running must be untouched");

        // The clip that genuinely finished before the crash is unaffected
        // by the reset -- still indexed, not bounced back to pending.
        let progress = gap_fill_progress_for_root(&conn, "/Volumes/Archive").unwrap();
        assert_eq!(progress.indexed, 1, "the genuinely finished clip stays indexed");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn failed_job_can_be_retried_and_reconnect_flow_round_trips() {
        let (db, path) = open_scratch_db("job_lifecycle");
        let conn = db.conn.lock().unwrap();

        let clip = upsert_clip(&conn, &new_clip("/Volumes/Archive/clip1.mov")).unwrap();
        enqueue_pending_gap_fill_jobs(&conn).unwrap();
        let job = claim_next_pending_job(&conn).unwrap().unwrap();
        assert_eq!(job.clip_id, clip.id);

        mark_job_failed(&conn, job.id, "ffmpeg could not open file").unwrap();
        let failed = get_job(&conn, job.id).unwrap();
        assert_eq!(failed.status, JobStatus::Failed);
        assert_eq!(failed.attempts, 1);
        assert_eq!(failed.last_error.as_deref(), Some("ffmpeg could not open file"));

        let retried = retry_failed_jobs(&conn, Some("/Volumes/Archive")).unwrap();
        assert_eq!(retried, 1);
        assert_eq!(get_job(&conn, job.id).unwrap().status, JobStatus::Pending);

        // Simulate a disconnect while the job is running, then reconnect.
        let reclaimed = claim_next_pending_job(&conn).unwrap().unwrap();
        let disconnected = mark_jobs_awaiting_reconnect(&conn, "/Volumes/Archive").unwrap();
        assert_eq!(disconnected, 1);
        assert_eq!(get_job(&conn, reclaimed.id).unwrap().status, JobStatus::AwaitingReconnect);

        let reconnected = requeue_jobs_on_reconnect(&conn, "/Volumes/Archive").unwrap();
        assert_eq!(reconnected, 1);
        assert_eq!(get_job(&conn, reclaimed.id).unwrap().status, JobStatus::Pending);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn gap_fill_progress_counts_each_bucket_correctly() {
        let (db, path) = open_scratch_db("progress_counts");
        let conn = db.conn.lock().unwrap();

        let done = upsert_clip(&conn, &new_clip("/Volumes/Archive/done.mov")).unwrap();
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 4.0)",
            params![done.id],
        )
        .unwrap();
        upsert_clip(&conn, &new_clip("/Volumes/Archive/pending.mov")).unwrap();
        let failing = upsert_clip(&conn, &new_clip("/Volumes/Archive/failing.mov")).unwrap();
        upsert_clip(&conn, &new_clip("/Volumes/OtherArchive/unrelated.mov")).unwrap();

        enqueue_pending_gap_fill_jobs(&conn).unwrap();
        conn.execute(
            "UPDATE gap_fill_jobs SET status = 'failed' WHERE clip_id = ?1",
            params![failing.id],
        )
        .unwrap();

        let progress = gap_fill_progress_for_root(&conn, "/Volumes/Archive").unwrap();
        assert_eq!(progress.discovered, 3);
        assert_eq!(progress.indexed, 1);
        assert_eq!(progress.queued, 1);
        assert_eq!(progress.failed, 1);
        assert_eq!(progress.awaiting_reconnect, 0);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    fn new_root(label: &str, path: &str) -> NewWatchedRoot {
        NewWatchedRoot {
            label: label.to_string(),
            path: path.to_string(),
            volume_id: None,
            approved_by: None,
        }
    }

    #[test]
    fn remove_watched_root_does_not_purge_a_sibling_sharing_its_path_prefix() {
        let (db, path) = open_scratch_db("remove_root_sibling_prefix");
        let conn = db.conn.lock().unwrap();

        let root = add_watched_root(&conn, &new_root("Archive", "/Volumes/Archive")).unwrap();
        add_watched_root(&conn, &new_root("Archive2", "/Volumes/Archive2")).unwrap();

        upsert_clip(&conn, &new_clip("/Volumes/Archive/clip.mov")).unwrap();
        upsert_clip(&conn, &new_clip("/Volumes/Archive2/clip.mov")).unwrap();

        remove_watched_root(&conn, root.id).unwrap();

        let removed_root_clips: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM clips WHERE file_path = '/Volumes/Archive/clip.mov'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(removed_root_clips, 0, "the removed root's own clip must be purged");

        let sibling_clips: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM clips WHERE file_path = '/Volumes/Archive2/clip.mov'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(sibling_clips, 1, "a sibling root's clip must survive removal of /Volumes/Archive");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn gap_fill_progress_for_root_does_not_count_a_sibling_sharing_its_path_prefix() {
        let (db, path) = open_scratch_db("progress_sibling_prefix");
        let conn = db.conn.lock().unwrap();

        upsert_clip(&conn, &new_clip("/Volumes/Archive/clip.mov")).unwrap();
        upsert_clip(&conn, &new_clip("/Volumes/Archive2/clip.mov")).unwrap();
        enqueue_pending_gap_fill_jobs(&conn).unwrap();

        let progress = gap_fill_progress_for_root(&conn, "/Volumes/Archive").unwrap();
        assert_eq!(progress.discovered, 1, "must not count /Volumes/Archive2's clip");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }
}
