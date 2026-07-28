use spyglass_core::adapters::{broll_analyzer, card_eater, transcriber};
use spyglass_core::models::{
    AccessLevel, GapFillProgress, NewClip, NewWatchedRoot, SourceApp, WatchedRoot,
};
use spyglass_core::{db, scanner};
use spyglass_engine::EngineState;
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use tauri::{AppHandle, Manager, State};

fn default_extensions() -> Vec<String> {
    scanner::DEFAULT_EXTENSIONS.iter().map(|s| s.to_string()).collect()
}

// ---------------------------------------------------------------------------
// Watched roots (Section 8 allowlist)
// ---------------------------------------------------------------------------

/// A watched root plus the runtime status the settings panel needs:
/// whether its path currently resolves at all (Section 9's offline badge)
/// and its gap-fill progress counts (Section 7's per-root status panel).
/// Bundled into one call so the panel isn't doing an N+1 round trip per
/// root on every refresh.
#[derive(Debug, Clone, serde::Serialize)]
pub struct WatchedRootStatus {
    #[serde(flatten)]
    pub root: WatchedRoot,
    pub is_online: bool,
    pub progress: GapFillProgress,
}

#[tauri::command]
pub fn list_watched_roots(state: State<Arc<EngineState>>) -> Result<Vec<WatchedRootStatus>, String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    let roots = db::list_visible_watched_roots(&conn).map_err(|e| e.to_string())?;
    roots
        .into_iter()
        .map(|root| {
            let progress = db::gap_fill_progress_for_root(&conn, &root.path).map_err(|e| e.to_string())?;
            let is_online = Path::new(&root.path).exists();
            Ok(WatchedRootStatus { root, is_online, progress })
        })
        .collect()
}

#[tauri::command]
pub fn add_watched_root(
    state: State<Arc<EngineState>>,
    label: String,
    path: String,
    volume_id: Option<String>,
    approved_by: Option<String>,
) -> Result<WatchedRoot, String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    db::add_watched_root(
        &conn,
        &NewWatchedRoot {
            label,
            path,
            volume_id,
            approved_by,
        },
    )
    .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn set_watched_root_access_level(
    state: State<Arc<EngineState>>,
    id: i64,
    access_level: String,
) -> Result<(), String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    db::set_watched_root_access_level(&conn, id, AccessLevel::from_str(&access_level))
        .map_err(|e| e.to_string())
}

/// Destructive: purges every clip (and cascading shots/tags/embeddings/
/// transcript segments) registered under this root's path. The frontend
/// is responsible for confirming with the user before calling this.
#[tauri::command]
pub fn remove_watched_root(state: State<Arc<EngineState>>, id: i64) -> Result<(), String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    db::remove_watched_root(&conn, id).map_err(|e| e.to_string())
}

/// "Start fresh" for one watched root without touching any other folder's
/// index -- see `spyglass_core::db::reset_watched_root`'s doc comment for
/// the full rationale (the `TAGS_PROMPT` prompt-echo bug this exists for:
/// literal example tags the VLM parroted back regardless of a shot's real
/// content, baked into every clip already scanned before the prompt was
/// fixed). Unlike `remove_watched_root`, the root itself is left alone
/// (still active/paused, not tombstoned) -- only its clips are purged and
/// `last_scanned_at` cleared, so the frontend's expected follow-up
/// (`scan_watched_root` against the same id) re-registers and re-analyzes
/// every file under it from scratch. Also deletes each purged clip's
/// cached keyframe directory, which isn't a database row and so doesn't
/// cascade with the rest. Destructive -- the frontend must confirm with
/// the user before calling this, same contract as `remove_watched_root`.
/// Returns the number of clips purged.
#[tauri::command]
pub fn reset_watched_root(state: State<Arc<EngineState>>, id: i64) -> Result<usize, String> {
    let result = {
        let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
        db::reset_watched_root(&conn, id).map_err(|e| e.to_string())?
    };
    if let Some(keyframe_root) = state.db.path.parent().map(|p| p.join("keyframes")) {
        for clip_id in &result.removed_clip_ids {
            let _ = std::fs::remove_dir_all(keyframe_root.join(clip_id.to_string()));
        }
    }
    Ok(result.clips_removed)
}

/// Repoints an existing root at a new location on disk -- the fix for an
/// annual working-drive-to-archive-drive migration where the *folder
/// itself* moved, not a single file already covered by some other,
/// still-active root's ordinary rescan. Only updates the `watched_roots`
/// row; the frontend is expected to immediately follow this with
/// `scan_watched_root` against the same id, which is what actually walks
/// the new path and relinks each clip still pointing at the (now-gone) old
/// path by checksum.
#[tauri::command]
pub fn relink_watched_root(state: State<Arc<EngineState>>, id: i64, new_path: String) -> Result<WatchedRoot, String> {
    if !Path::new(&new_path).is_dir() {
        return Err(format!("{new_path} does not exist or is not a folder"));
    }
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    db::relink_watched_root_path(&conn, id, &new_path).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Scanning (Phase 1: registration + transcript import only)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, serde::Serialize)]
pub struct ScanResult {
    pub discovered: u64,
    pub registered: u64,
    pub already_registered: u64,
    /// Skipped because the file falls under a watched root the user
    /// explicitly removed -- see `scanner::is_under_a_removed_root`.
    pub excluded_removed: u64,
    /// Recognized as content already indexed under a now-gone path (an
    /// archive-drive move) and repointed to its new location in place,
    /// rather than registered as a new clip -- see
    /// `db::find_clip_by_checksum`.
    pub relinked: u64,
    pub errors: Vec<String>,
}

/// Runs the watched-root scanner against one approved root: discovers
/// media files and registers a bare `clips` row for anything not already
/// indexed (Section 7/17). Also updates `last_scanned_at`.
///
/// Walking a large archive and BLAKE3-checksumming every newly discovered
/// file is genuinely slow -- easily minutes on an external drive with
/// thousands of clips. Tauri commands that aren't declared `async` run on
/// the main thread by default, so doing that work directly here would
/// freeze the whole UI (not just this button) for the duration. Running it
/// inside `spawn_blocking` keeps it off both the main thread and the async
/// runtime's worker threads -- and `scanner::rescan_root` itself only
/// takes the database's lock for brief per-file operations rather than
/// the whole scan, so a long scan doesn't also freeze every *other*
/// command that needs the database (search, the gap-fill worker, pool)
/// for its duration.
#[tauri::command]
pub async fn scan_watched_root(app: AppHandle, root_id: i64) -> Result<ScanResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<Arc<EngineState>>();

        // The periodic rescan scheduler treats a never-scanned root as
        // always "due" -- without this guard, it can start walking (and
        // redundantly re-checksumming, possibly tens of GB per file) the
        // exact same root a manual click just started on.
        let _scan_guard = state
            .try_start_scan(root_id)
            .ok_or_else(|| "this folder is already being scanned in the background -- try again shortly".to_string())?;

        let root = {
            let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
            db::list_watched_roots(&conn)
                .map_err(|e| e.to_string())?
                .into_iter()
                .find(|r| r.id == root_id)
                .ok_or_else(|| format!("watched root {root_id} not found"))?
        };

        let extensions = default_extensions();
        let stats = scanner::rescan_root(&state.db, &root, &extensions).map_err(|e| e.to_string())?;

        Ok(ScanResult {
            discovered: stats.discovered,
            registered: stats.registered,
            already_registered: stats.already_registered,
            excluded_removed: stats.excluded_removed,
            relinked: stats.relinked,
            errors: Vec::new(),
        })
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Opens Card Eater's own `card-eater.sqlite3` read-only (default macOS
/// app-data location for `edu.blair.cardeater`, or an explicit path) and
/// registers a `clips` row for every completed, verified copy -- Section 3's
/// adapter, following the busy-timeout/retry/fresh-connection contract in
/// Section 19.3.
#[tauri::command]
pub async fn scan_card_eater(app: AppHandle, db_path: Option<String>) -> Result<ScanResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let resolved_path = db_path
            .map(PathBuf::from)
            .or_else(default_card_eater_db_path)
            .ok_or_else(|| "could not resolve Card Eater's database path".to_string())?;

        let files = card_eater::scan_completed_copies(&resolved_path).map_err(|e| e.to_string())?;
        let Some(files) = files else {
            return Err("card-eater.sqlite3 stayed busy through every retry -- try again shortly".to_string());
        };

        let state = app.state::<Arc<EngineState>>();
        let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
        let removed_roots = db::effectively_removed_watched_root_paths(&conn).map_err(|e| e.to_string())?;
        let mut result = ScanResult {
            discovered: files.len() as u64,
            registered: 0,
            already_registered: 0,
            excluded_removed: 0,
            relinked: 0,
            errors: Vec::new(),
        };
        for file in files {
            let new_clip: NewClip = file.into();
            if scanner::is_under_a_removed_root(&new_clip.file_path, &removed_roots) {
                result.excluded_removed += 1;
                continue;
            }
            let already_existed = db::find_clip_by_path(&conn, &new_clip.file_path)
                .map_err(|e| e.to_string())?
                .is_some();
            db::upsert_clip(&conn, &new_clip).map_err(|e| e.to_string())?;
            if already_existed {
                result.already_registered += 1;
            } else {
                result.registered += 1;
            }
        }
        db::enqueue_pending_gap_fill_jobs(&conn).map_err(|e| e.to_string())?;
        Ok(result)
    })
    .await
    .map_err(|e| e.to_string())?
}

fn default_card_eater_db_path() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    Some(
        PathBuf::from(home)
            .join("Library/Application Support/edu.blair.cardeater/card-eater.sqlite3"),
    )
}

/// Walks `root_path` for `*.ivt-cache.json` sidecars, registers each
/// described video as a clip, and imports its transcript segments
/// (respecting `excluded_speakers`/`speaker_labels`). Skips clips that
/// already have transcript segments imported, so a re-scan doesn't
/// duplicate rows.
#[tauri::command]
pub async fn scan_transcriber_sidecars(app: AppHandle, root_path: String) -> Result<ScanResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = Path::new(&root_path);
        let sidecar_paths = transcriber::discover_sidecars(root);

        let state = app.state::<Arc<EngineState>>();
        let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
        let removed_roots = db::effectively_removed_watched_root_paths(&conn).map_err(|e| e.to_string())?;
        let mut result = ScanResult {
            discovered: sidecar_paths.len() as u64,
            registered: 0,
            already_registered: 0,
            excluded_removed: 0,
            relinked: 0,
            errors: Vec::new(),
        };

        for sidecar_path in sidecar_paths {
            let sidecar = match transcriber::parse_sidecar(&sidecar_path) {
                Ok(s) => s,
                Err(e) => {
                    result.errors.push(e.to_string());
                    continue;
                }
            };
            let video_path = transcriber::source_video_path(&sidecar).to_string();
            if scanner::is_under_a_removed_root(&video_path, &removed_roots) {
                result.excluded_removed += 1;
                continue;
            }
            let already_existed = db::find_clip_by_path(&conn, &video_path)
                .map_err(|e| e.to_string())?
                .is_some();
            let clip = db::upsert_clip(
                &conn,
                &NewClip {
                    file_path: video_path,
                    source_app: SourceApp::SpyglassScan,
                    checksum: None,
                    size_bytes: sidecar.video_size,
                    duration_sec: None,
                },
            )
            .map_err(|e| e.to_string())?;

            if already_existed {
                result.already_registered += 1;
                continue;
            }
            result.registered += 1;

            for seg in transcriber::to_transcript_segments(&sidecar, clip.id) {
                db::insert_transcript_segment(&conn, &seg).map_err(|e| e.to_string())?;
            }
        }

        db::enqueue_pending_gap_fill_jobs(&conn).map_err(|e| e.to_string())?;
        Ok(result)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Walks `root_path` for `.broll_analyzer_cache.json` files and registers
/// each clip they describe. Attaching technical-quality/energy facets to
/// shots happens once shot detection exists (Phase 2) -- this Phase 1 pass
/// only registers the clip rows.
#[tauri::command]
pub async fn scan_broll_cache(app: AppHandle, root_path: String) -> Result<ScanResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = Path::new(&root_path);
        let cache_paths = broll_analyzer::discover_cache_files(root);

        let state = app.state::<Arc<EngineState>>();
        let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
        let removed_roots = db::effectively_removed_watched_root_paths(&conn).map_err(|e| e.to_string())?;
        let mut result = ScanResult {
            discovered: 0,
            registered: 0,
            already_registered: 0,
            excluded_removed: 0,
            relinked: 0,
            errors: Vec::new(),
        };

        for cache_path in cache_paths {
            let cache = match broll_analyzer::parse_cache(&cache_path) {
                Ok(c) => c,
                Err(e) => {
                    result.errors.push(e.to_string());
                    continue;
                }
            };
            for rel_path in cache.clips.keys() {
                result.discovered += 1;
                let abs_path = broll_analyzer::clip_absolute_path(&cache_path, rel_path);
                let path_str = abs_path.to_string_lossy().into_owned();
                if scanner::is_under_a_removed_root(&path_str, &removed_roots) {
                    result.excluded_removed += 1;
                    continue;
                }
                let already_existed = db::find_clip_by_path(&conn, &path_str)
                    .map_err(|e| e.to_string())?
                    .is_some();
                let size_bytes = std::fs::metadata(&abs_path).ok().map(|m| m.len() as i64);
                let checksum = scanner::compute_checksum(&abs_path).ok();
                db::upsert_clip(
                    &conn,
                    &NewClip {
                        file_path: path_str,
                        source_app: SourceApp::SpyglassScan,
                        checksum,
                        size_bytes,
                        duration_sec: None,
                    },
                )
                .map_err(|e| e.to_string())?;
                if already_existed {
                    result.already_registered += 1;
                } else {
                    result.registered += 1;
                }
            }
        }

        db::enqueue_pending_gap_fill_jobs(&conn).map_err(|e| e.to_string())?;
        Ok(result)
    })
    .await
    .map_err(|e| e.to_string())?
}

// ---------------------------------------------------------------------------
// Gap-fill queue (Phase 2: progress, retry, pause/resume -- Section 7)
// ---------------------------------------------------------------------------

/// Resets `failed` jobs back to `pending`, optionally scoped to one root
/// (Section 7's "retry failed" action). Returns how many were requeued.
#[tauri::command]
pub fn retry_failed_jobs(state: State<Arc<EngineState>>, root_id: Option<i64>) -> Result<usize, String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    let root_path = match root_id {
        Some(id) => Some(
            db::list_watched_roots(&conn)
                .map_err(|e| e.to_string())?
                .into_iter()
                .find(|r| r.id == id)
                .ok_or_else(|| format!("watched root {id} not found"))?
                .path,
        ),
        None => None,
    };
    db::retry_failed_jobs(&conn, root_path.as_deref()).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn set_queue_paused(state: State<Arc<EngineState>>, paused: bool) -> Result<(), String> {
    state.queue_control.paused.store(paused, Ordering::Relaxed);
    Ok(())
}

#[tauri::command]
pub fn get_queue_paused(state: State<Arc<EngineState>>) -> Result<bool, String> {
    Ok(state.queue_control.paused.load(Ordering::Relaxed))
}

/// "Process now" override (Section 7's idle gate otherwise means a scan
/// run while the machine is in active use just piles up an ever-growing
/// backlog with no way to work through it short of walking away from the
/// keyboard): bypasses `idle::background_work_allowed`'s idle check --
/// but not a manual pause, which still wins outright -- until the pending
/// queue drains on its own, at which point `gap_fill_worker`'s loop clears
/// the flag itself. A no-op if the queue is currently paused; the caller
/// is expected to also surface that in the UI rather than silently doing
/// nothing.
#[tauri::command]
pub fn force_gap_fill_now(state: State<Arc<EngineState>>) -> Result<(), String> {
    state.queue_control.force_active.store(true, Ordering::Relaxed);
    Ok(())
}

/// What `get_queue_paused`'s plain boolean can't tell the panel: the queue
/// can look "Running" (not manually paused) while still doing nothing at
/// all, because the idle gate (Section 7: pause during active editing,
/// resume after `min_idle_seconds`) is currently blocking it. Without this,
/// there was no way to distinguish "actually indexing" from "waiting for
/// you to stop touching the mouse" -- both looked identical in the UI,
/// which is exactly what made stalled progress look like a hang.
#[derive(Debug, Clone, serde::Serialize)]
pub struct BackgroundWorkStatus {
    pub manually_paused: bool,
    /// `None` if the idle check itself failed (treated as "assume active,
    /// don't block" by `idle::background_work_allowed`, same as here).
    pub idle_seconds: Option<f64>,
    pub min_idle_seconds: f64,
    /// Whether a "process now" override (`force_gap_fill_now`) is
    /// currently bypassing the idle check. Cleared automatically once the
    /// queue drains -- see that command's doc comment.
    pub force_active: bool,
}

#[tauri::command]
pub fn get_background_work_status(state: State<Arc<EngineState>>) -> BackgroundWorkStatus {
    BackgroundWorkStatus {
        manually_paused: state.queue_control.paused.load(Ordering::Relaxed),
        idle_seconds: spyglass_engine::idle::system_idle_seconds(),
        min_idle_seconds: crate::MIN_IDLE_SECONDS,
        force_active: state.queue_control.force_active.load(Ordering::Relaxed),
    }
}

// ---------------------------------------------------------------------------
// Search (Phase 3: hybrid shot search)
// ---------------------------------------------------------------------------

/// Natural-language shot search (Section 12): embeds `query` via the
/// persistent CLIP text-embedding server (started lazily on first use, and
/// transparently restarted by `embed_with_restart` if it's died since the
/// last search) and ranks shots with `spyglass_core::search`'s hybrid
/// scoring.
///
/// Starting the embed server for the first time waits on a real model
/// load, and every query is a round trip to that subprocess -- both can
/// take a while under CPU contention from concurrent gap-fill sidecar
/// processes (`EMBED_TIMEOUT`/`READY_TIMEOUT` in `embed_server.rs` bound
/// how long, but even a bounded wait of up to a minute would freeze the
/// whole UI if it ran on Tauri's main thread, same class of bug the scan
/// commands had). Run inside `spawn_blocking` for the same reason.
#[tauri::command]
pub async fn search_shots(
    app: AppHandle,
    query: String,
    filters: spyglass_core::facets::FacetFilters,
) -> Result<Vec<spyglass_core::models::ShotSearchResult>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<Arc<EngineState>>();
        let embedding = {
            let mut guard = state.embed_server.lock().map_err(|e| e.to_string())?;
            spyglass_engine::embed_server::embed_with_restart(&mut guard, &state.sidecar_dir, &query)?
        };

        let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
        spyglass_core::search::search_shots(&conn, &query, &embedding, &filters, 60).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// "Find shots like this" (Section 12): visual-similarity search seeded
/// from a reference shot already in view, rather than a text query.
#[tauri::command]
pub async fn find_similar_shots(app: AppHandle, shot_id: i64) -> Result<Vec<spyglass_core::models::ShotSearchResult>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<Arc<EngineState>>();
        let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
        spyglass_core::search::find_similar_shots(&conn, shot_id, 60).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

// ---------------------------------------------------------------------------
// Tag correction (Section 13's inline fix affordance)
// ---------------------------------------------------------------------------

#[tauri::command]
pub fn add_tag(state: State<Arc<EngineState>>, shot_id: i64, label: String) -> Result<(), String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    db::add_human_tag(&conn, shot_id, label.trim()).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn remove_tag(state: State<Arc<EngineState>>, shot_id: i64, label: String) -> Result<(), String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    db::remove_tag(&conn, shot_id, &label).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Clip favoriting
// ---------------------------------------------------------------------------

#[tauri::command]
pub fn set_shot_favorite(state: State<Arc<EngineState>>, shot_id: i64, favorite: bool) -> Result<(), String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    db::set_shot_favorite(&conn, shot_id, favorite).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn list_favorite_shots(state: State<Arc<EngineState>>) -> Result<Vec<spyglass_core::models::ShotSearchResult>, String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    spyglass_core::search::list_favorite_shots(&conn).map_err(|e| e.to_string())
}

/// Populates the facet sidebar (Section 13): every tag/source value with
/// its shot count, plus the archive's ingested-date bounds for clamping
/// the date pickers.
#[tauri::command]
pub fn list_facet_options(state: State<Arc<EngineState>>) -> Result<spyglass_core::facets::FacetOptions, String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    spyglass_core::facets::list_facet_options(&conn).map_err(|e| e.to_string())
}

/// Facet-only browsing (Section 13: "browsing by tag/date/source without
/// typing a query") -- no embed-server round trip needed since there's no
/// text query, so unlike `search_shots` this runs synchronously.
#[tauri::command]
pub fn browse_shots(
    state: State<Arc<EngineState>>,
    filters: spyglass_core::facets::FacetFilters,
) -> Result<Vec<spyglass_core::models::ShotSearchResult>, String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    spyglass_core::facets::browse_shots(&conn, &filters, 60).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Pool tray (Section 13/14)
// ---------------------------------------------------------------------------

#[tauri::command]
pub fn get_pool(state: State<Arc<EngineState>>) -> Result<Vec<spyglass_core::models::ShotSearchResult>, String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    let pool = spyglass_core::pool::get_or_create_default_pool(&conn).map_err(|e| e.to_string())?;
    spyglass_core::pool::list_shots(&conn, pool.id).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn add_shot_to_pool(state: State<Arc<EngineState>>, shot_id: i64) -> Result<(), String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    let pool = spyglass_core::pool::get_or_create_default_pool(&conn).map_err(|e| e.to_string())?;
    spyglass_core::pool::add_shot(&conn, pool.id, shot_id).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn remove_shot_from_pool(state: State<Arc<EngineState>>, shot_id: i64) -> Result<(), String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    let pool = spyglass_core::pool::get_or_create_default_pool(&conn).map_err(|e| e.to_string())?;
    spyglass_core::pool::remove_shot(&conn, pool.id, shot_id).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn reorder_pool(state: State<Arc<EngineState>>, shot_ids: Vec<i64>) -> Result<(), String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    let pool = spyglass_core::pool::get_or_create_default_pool(&conn).map_err(|e| e.to_string())?;
    spyglass_core::pool::reorder(&conn, pool.id, &shot_ids).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn clear_pool(state: State<Arc<EngineState>>) -> Result<(), String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    let pool = spyglass_core::pool::get_or_create_default_pool(&conn).map_err(|e| e.to_string())?;
    spyglass_core::pool::clear(&conn, pool.id).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Premiere Pro XMEML export (Section 14)
// ---------------------------------------------------------------------------

/// Exports the pool tray, in its current order, as a Premiere Pro-
/// importable XMEML sequence at `destination_path`. Returns the written
/// file's path. The frontend is responsible for confirming the
/// destination with the user before calling this (it's a genuine "writes
/// new data" action).
#[tauri::command]
pub fn export_pool_to_premiere_xml(
    state: State<Arc<EngineState>>,
    destination_path: String,
    sequence_name: String,
) -> Result<String, String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    let pool = spyglass_core::pool::get_or_create_default_pool(&conn).map_err(|e| e.to_string())?;
    let shots = spyglass_core::pool::list_shots(&conn, pool.id).map_err(|e| e.to_string())?;
    if shots.is_empty() {
        return Err("the pool is empty -- add shots before exporting".to_string());
    }

    let clips: Vec<spyglass_core::xmeml::XmemlClip> = shots
        .iter()
        .map(|shot| spyglass_core::xmeml::XmemlClip {
            file_path: shot.clip_file_path.clone(),
            name: std::path::Path::new(&shot.clip_file_path)
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_else(|| shot.clip_file_path.clone()),
            frame_rate: shot.clip_frame_rate,
            in_seconds: shot.start_tc,
            out_seconds: shot.end_tc,
            // Real per-clip audio probing (Section 15), replacing Phase
            // 4's fixed stereo/48kHz/16-bit assumption -- not persisted in
            // the index, so probed fresh at export time via ffprobe.
            audio_format: spyglass_core::ffprobe::probe_audio_format(Path::new(&shot.clip_file_path)).ok().flatten(),
        })
        .collect();

    let options = spyglass_core::xmeml::XmemlOptions { sequence_name, ..Default::default() };
    let xml = spyglass_core::xmeml::build_sequence_xml(&clips, &options);

    std::fs::write(&destination_path, xml).map_err(|e| e.to_string())?;
    Ok(destination_path)
}

// ---------------------------------------------------------------------------
// Consolidate & Copy export (Section 15)
// ---------------------------------------------------------------------------

/// Resolves the current pool's shots into `ConsolidateClip`s -- joining
/// back to each shot's parent `clips` row for the size/duration the
/// consolidate module needs (fields `ShotSearchResult` doesn't carry).
/// `with_audio_format` is skipped for a plain size estimate (an ffprobe
/// call per pool clip is unnecessary work just to show a byte count) and
/// only turned on for an actual export run, which needs it for the
/// optional copied-files XMEML.
fn resolve_consolidate_clips(
    conn: &rusqlite::Connection,
    with_audio_format: bool,
) -> Result<Vec<spyglass_core::consolidate::ConsolidateClip>, String> {
    let pool = spyglass_core::pool::get_or_create_default_pool(conn).map_err(|e| e.to_string())?;
    let shots = spyglass_core::pool::list_shots(conn, pool.id).map_err(|e| e.to_string())?;
    if shots.is_empty() {
        return Err("the pool is empty -- add shots before exporting".to_string());
    }

    shots
        .into_iter()
        .map(|shot| {
            let clip = db::find_clip_by_id(conn, shot.clip_id)
                .map_err(|e| e.to_string())?
                .ok_or_else(|| format!("clip {} no longer exists", shot.clip_id))?;
            let audio_format = if with_audio_format {
                spyglass_core::ffprobe::probe_audio_format(Path::new(&clip.file_path)).ok().flatten()
            } else {
                None
            };
            Ok(spyglass_core::consolidate::ConsolidateClip {
                shot_id: shot.shot_id,
                file_path: shot.clip_file_path,
                size_bytes: clip.size_bytes,
                duration_sec: clip.duration_sec,
                frame_rate: shot.clip_frame_rate,
                audio_format,
                in_seconds: shot.start_tc,
                out_seconds: shot.end_tc,
                tags: shot.tags,
            })
        })
        .collect()
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct ConsolidateEstimate {
    pub file_count: usize,
    pub total_bytes: u64,
    pub available_bytes: u64,
    pub destination_has_existing_files: bool,
}

/// Section 15's pre-export safety check: file count/total size (for the
/// confirmation dialog), free space at the destination, and whether the
/// destination folder already holds unrelated content.
#[tauri::command]
pub fn estimate_consolidate_export(
    state: State<Arc<EngineState>>,
    destination_path: String,
    copy_mode: spyglass_core::consolidate::CopyMode,
) -> Result<ConsolidateEstimate, String> {
    let clips = {
        let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
        resolve_consolidate_clips(&conn, false)?
    };

    let size = spyglass_core::consolidate::estimate_export_size(&clips, &copy_mode);
    let destination = Path::new(&destination_path);
    let available_bytes = spyglass_core::consolidate::available_bytes(destination).map_err(|e| e.to_string())?;
    let destination_has_existing_files = spyglass_core::consolidate::destination_has_existing_files(destination);

    Ok(ConsolidateEstimate {
        file_count: size.file_count,
        total_bytes: size.total_bytes,
        available_bytes,
        destination_has_existing_files,
    })
}

/// Kicks off the export in the background (`consolidate_export.rs`); the
/// frontend polls `get_consolidate_export_status` for progress. The
/// frontend is responsible for confirming file count/size/destination
/// with the user before calling this -- it's a genuine "writes new data"
/// action (Section 15).
#[tauri::command]
pub fn start_consolidate_export(
    state: State<Arc<EngineState>>,
    destination_path: String,
    pool_name: String,
    copy_mode: spyglass_core::consolidate::CopyMode,
    folder_structure: spyglass_core::consolidate::FolderStructure,
) -> Result<(), String> {
    let clips = {
        let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
        resolve_consolidate_clips(&conn, true)?
    };

    let destination_root = PathBuf::from(&destination_path);
    let plan = spyglass_core::consolidate::build_export_plan(&pool_name, &clips, &destination_root, folder_structure);

    spyglass_engine::consolidate_export::run_in_background(state.consolidate_export.clone(), destination_root, plan, copy_mode);
    Ok(())
}

#[tauri::command]
pub fn get_consolidate_export_status(
    state: State<Arc<EngineState>>,
) -> Result<Option<spyglass_engine::consolidate_export::ConsolidateExportStatus>, String> {
    state.consolidate_export.lock().map_err(|e| e.to_string()).map(|s| s.clone())
}

/// The paired "XML pointing at copied files" export (Section 15) --
/// builds a second Premiere XMEML sequence referencing the just-copied
/// files instead of the original archive paths, from the most recently
/// completed consolidate export's own manifest. Only callable after that
/// export finished successfully.
#[tauri::command]
pub fn export_copied_files_to_premiere_xml(
    state: State<Arc<EngineState>>,
    destination_path: String,
    sequence_name: String,
) -> Result<String, String> {
    let manifest = {
        let status = state.consolidate_export.lock().map_err(|e| e.to_string())?;
        status
            .as_ref()
            .and_then(|s| s.manifest.clone())
            .ok_or_else(|| "no completed consolidate export to build a copied-files XML from".to_string())?
    };

    let clips = spyglass_core::consolidate::manifest_to_xmeml_clips(&manifest);
    let options = spyglass_core::xmeml::XmemlOptions { sequence_name, ..Default::default() };
    let xml = spyglass_core::xmeml::build_sequence_xml(&clips, &options);

    std::fs::write(&destination_path, xml).map_err(|e| e.to_string())?;
    Ok(destination_path)
}

// ---------------------------------------------------------------------------
// Index maintenance (Section 17/18: backup, restore, integrity, rebuild)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, serde::Serialize)]
pub struct BackupInfo {
    pub file_name: String,
    pub path: String,
    pub size_bytes: u64,
    pub created_at: String,
}

/// Backups older than the newest 5 are pruned automatically (Section 10's
/// general posture against letting the app's own footprint grow
/// unbounded) -- not yet exposed as a setting, matching the same "fixed
/// conservative default for now" precedent as `MAX_CONCURRENCY`.
const BACKUP_RETENTION_COUNT: usize = 5;

fn backups_dir(state: &EngineState) -> Result<PathBuf, String> {
    let dir = state
        .db
        .path
        .parent()
        .ok_or_else(|| "database path has no parent directory".to_string())?
        .join("backups");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir)
}

fn backup_info_for(path: &Path) -> std::io::Result<BackupInfo> {
    let meta = std::fs::metadata(path)?;
    let created_at: chrono::DateTime<chrono::Utc> = meta.modified()?.into();
    Ok(BackupInfo {
        file_name: path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
        path: path.to_string_lossy().into_owned(),
        size_bytes: meta.len(),
        created_at: created_at.to_rfc3339(),
    })
}

/// Snapshots the live index into a timestamped file via SQLite's own
/// online backup API (`maintenance::backup_database`), then prunes down to
/// the newest `BACKUP_RETENTION_COUNT` backups.
#[tauri::command]
pub fn backup_index_now(state: State<Arc<EngineState>>) -> Result<BackupInfo, String> {
    let dir = backups_dir(&state)?;
    let timestamp = chrono::Utc::now().format("%Y-%m-%dT%H-%M-%S%.3fZ");
    let dest = dir.join(format!(
        "{}{timestamp}.sqlite",
        spyglass_core::maintenance::BACKUP_FILE_PREFIX
    ));

    {
        let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
        spyglass_core::maintenance::backup_database(&conn, &dest).map_err(|e| e.to_string())?;
    }
    spyglass_core::maintenance::prune_old_backups(&dir, BACKUP_RETENTION_COUNT).map_err(|e| e.to_string())?;

    backup_info_for(&dest).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn list_backups(state: State<Arc<EngineState>>) -> Result<Vec<BackupInfo>, String> {
    let dir = backups_dir(&state)?;
    let mut backups: Vec<BackupInfo> = std::fs::read_dir(&dir)
        .map_err(|e| e.to_string())?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with(spyglass_core::maintenance::BACKUP_FILE_PREFIX))
                .unwrap_or(false)
        })
        .filter_map(|p| backup_info_for(&p).ok())
        .collect();
    backups.sort_by(|a, b| b.created_at.cmp(&a.created_at));
    Ok(backups)
}

/// Restores the live index from a backup file. Validates the candidate is
/// a healthy SQLite database first (never stages a corrupt file over the
/// live index), stages it into the app-data dir, atomically renames it
/// over the live db path, clears any now-stale journal/WAL sidecar files
/// left by the *previous* live database (a leftover journal next to the
/// swapped-in file could otherwise make SQLite think there's an
/// interrupted transaction to recover), then restarts the app so it
/// reopens cleanly against the restored file rather than trying to
/// hot-swap the connection every other part of the running app still
/// holds a reference to. Destructive -- the frontend must confirm with the
/// user before calling this.
#[tauri::command]
pub fn restore_backup(app: AppHandle, state: State<Arc<EngineState>>, backup_path: String) -> Result<(), String> {
    let source = Path::new(&backup_path);
    let live_path = state.db.path.clone();

    // A brief synchronization point, not a real guarantee against every
    // in-flight query -- the background loops re-acquire this lock on
    // every iteration anyway, so this just avoids swapping the file out
    // from under a query that happens to be running at this exact instant.
    drop(state.db.conn.lock().map_err(|e| e.to_string())?);

    spyglass_core::maintenance::restore_database_file(source, &live_path).map_err(|e| e.to_string())?;

    app.request_restart();
    Ok(())
}

#[tauri::command]
pub fn check_index_integrity(state: State<Arc<EngineState>>) -> Result<Vec<String>, String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    spyglass_core::maintenance::integrity_check(&conn).map_err(|e| e.to_string())
}

/// Repairs/refreshes the on-disk search structures (Section 17/18's
/// "rebuild tooling") -- a maintenance action for corruption/staleness,
/// not a routine operation.
#[tauri::command]
pub fn rebuild_search_index_cmd(state: State<Arc<EngineState>>) -> Result<(), String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    spyglass_core::maintenance::rebuild_search_index(&conn).map_err(|e| e.to_string())
}

/// Retroactive cleanup for tags the VLM pass generated before `TAGS_PROMPT`
/// (sidecar/analyze_clip.py) told it not to transcribe on-screen text or
/// describe/count subjects' gender -- deletes every unreviewed
/// `spyglass_vlm` tag containing a digit (jersey numbers, scoreboard
/// scores/clocks, years off a sign), an onscreen-text-shaped tag (quoted,
/// UI-role-suffixed like `"united" logo`, sentence-punctuated, or
/// implausibly long), or a whole-word gender/headcount term ("boy"/"girl"/
/// "two boys") archive-wide, and never touches a human-added tag. See
/// `spyglass_core::db::purge_onscreen_text_tags`, `purge_ui_text_tags`, and
/// `purge_gender_tags`'s doc comments for the full rationale and their
/// scope limits (doesn't catch a digit-free, unpunctuated, un-quoted
/// bare-word name or text leak -- that class needs the sidecar's OCR-token
/// match at generation time, not a retroactive DB-only pass). Destructive
/// -- the frontend must confirm with the user before calling this, same
/// contract as `restore_backup`/`remove_watched_root`. Returns the total
/// number of tags removed across all three passes.
#[tauri::command]
pub fn purge_bad_tags(state: State<Arc<EngineState>>) -> Result<usize, String> {
    let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
    db::purge_bad_tags(&conn).map_err(|e| e.to_string())
}

/// Retroactive repair for clips indexed before `sidecar/analyze_clip.py`'s
/// scene-cut detector sensitivity fix (`min_scene_len` on `ContentDetector`
/// plus the `_merge_short_scenes` backstop): finds every clip with at
/// least one shot shorter than `spyglass_core::db::MIN_SHOT_DURATION_SEC`
/// (fast pans, camera flashes, and quick highlight-reel cuts used to
/// register as their own spurious sub-second "shots," each paying the
/// full keyframe/CLIP-embedding/VLM-caption cost), wipes just those clips'
/// shots/tags/embeddings, and requeues them for a fresh gap-fill pass --
/// clips that never had the problem are left completely untouched, unlike
/// `reset_watched_root`'s whole-folder wipe. Also deletes each affected
/// clip's cached keyframe directory, same as `reset_watched_root` does,
/// since re-analysis producing fewer (merged) shots than before would
/// otherwise leave the old higher-numbered keyframe JPEGs behind as
/// orphaned files. Destructive -- the frontend must confirm with the user
/// before calling this, same contract as `reset_watched_root`. Returns the
/// number of clips requeued; the actual re-analysis happens asynchronously
/// via the normal gap-fill worker queue, same as any other queued job.
#[tauri::command]
pub fn requeue_short_shot_clips(state: State<Arc<EngineState>>) -> Result<usize, String> {
    let result = {
        let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
        let clip_ids = db::find_clips_with_short_shots(&conn, db::MIN_SHOT_DURATION_SEC)
            .map_err(|e| e.to_string())?;
        db::requeue_clips_with_short_shots(&conn, &clip_ids).map_err(|e| e.to_string())?
    };
    if let Some(keyframe_root) = state.db.path.parent().map(|p| p.join("keyframes")) {
        for clip_id in &result.requeued_clip_ids {
            let _ = std::fs::remove_dir_all(keyframe_root.join(clip_id.to_string()));
        }
    }
    Ok(result.clips_requeued)
}

/// Best-effort read-ahead: touches the first few MB of `path` on a
/// background thread, fire-and-forget. Called from the frontend on shot
/// hover (before the user actually clicks to preview), so a sleeping
/// external/archival drive gets a head start waking up and the OS/SMB
/// layer gets a head start warming its cache, ahead of when
/// `open_native_video_preview` actually needs the file. Errors (file
/// gone, drive unmounted, etc.) are swallowed -- this is purely a latency
/// optimization for the real open, never something the frontend needs to
/// react to.
#[tauri::command]
pub fn prefetch_clip_file(path: String) {
    std::thread::spawn(move || {
        use std::io::Read;
        let Ok(mut file) = std::fs::File::open(&path) else { return };
        let mut buf = [0u8; 1024 * 1024];
        for _ in 0..4 {
            match file.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
        }
    });
}
