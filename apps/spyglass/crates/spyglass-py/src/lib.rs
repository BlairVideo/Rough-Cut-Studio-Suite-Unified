//! PyO3 bindings exposing `spyglass-engine`/`spyglass-core` to Python --
//! the mechanism a future Rough Cut Studio Suite integration uses to link
//! Spyglass's Rust engine directly into the Suite's Python process,
//! in-process, rather than a subprocess/JSON-RPC sidecar.
//!
//! Phase 1 (see `ping`/`ping_after_delay`/`deliberately_panic` below) was a
//! deliberately minimal spike proving two things before any real surface
//! was bound: a `tokio::runtime::Runtime` started inside a `cdylib` loaded
//! into CPython's own process works correctly, and releasing the GIL
//! during a blocking wait (`py.allow_threads`) genuinely lets other Python
//! threads keep running instead of freezing the whole interpreter. Both
//! were empirically confirmed (a concurrent Python thread kept ticking
//! during a simulated slow call; a deliberate panic surfaced as a normal
//! `PanicException` rather than aborting the process) -- see the Phase 1
//! spike test for the exact proof.
//!
//! Phase 2 (this file's real content) binds the read-only search/browse
//! surface. One simplification worth recording: none of
//! `spyglass_core::search`/`facets` is actually `async` -- Tauri's own
//! commands.rs wrapped `search_shots`/`find_similar_shots` in
//! `tauri::async_runtime::spawn_blocking` purely to keep Tauri's single
//! cooperative-scheduling main thread unblocked, not because the
//! underlying logic does real async I/O (it's synchronous `Mutex` locks +
//! `rusqlite` queries + a blocking subprocess round trip to the embed
//! server). The direct Python equivalent of "don't block the one thread
//! everything else depends on" is simply `py.allow_threads(|| ...)` around
//! that synchronous call -- no `tokio::spawn`/oneshot-channel indirection
//! needed for these functions at all. That machinery stays reserved (and
//! already proven, per Phase 1) for `Engine`'s own background loops
//! (gap-fill worker, volume watcher, rescan scheduler), which are
//! genuinely `async` and run on the engine's own Tokio runtime regardless
//! of what calls into them.
//!
//! Note on the `::spyglass_core::` (leading `::`) paths throughout: this
//! crate's own Python-facing module is *also* named `spyglass_core` (the
//! `#[pymodule] fn spyglass_core` below -- its name has to match the
//! compiled extension's module name so `import spyglass_core` works in
//! Python), which collides with the `spyglass-core` dependency crate of
//! the same name in the module namespace. The leading `::` forces an
//! unambiguous crate-root reference to the dependency; a bare
//! `spyglass_core::` here is a compile error ("ambiguous name"), not a
//! style preference.

mod native_preview;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pythonize::{depythonize, pythonize};
use ::spyglass_core::consolidate::{CopyMode, FolderStructure};
use ::spyglass_core::facets::FacetFilters;
use ::spyglass_core::models::{AccessLevel, GapFillProgress, ShotSearchResult, TranscriptSearchResult, WatchedRoot};
use serde::Serialize;
use spyglass_engine::{Engine, EngineConfig, EngineState};
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};
use std::time::Duration;

/// The single running engine for this process. `OnceLock` (not
/// `Mutex<Option<...>>`) because there's exactly one legitimate lifetime
/// for this: started once by `init()`, torn down only when the hosting
/// Python process exits. A second `init()` call is a caller bug, not a
/// state transition to support.
static ENGINE: OnceLock<Engine> = OnceLock::new();

fn state() -> PyResult<Arc<EngineState>> {
    ENGINE.get().map(|e| e.state.clone()).ok_or_else(|| PyRuntimeError::new_err("call spyglass_core.init() first"))
}

fn to_py_err<E: std::fmt::Display>(e: E) -> PyErr {
    PyRuntimeError::new_err(e.to_string())
}

/// Starts the engine: opens (or creates) `spyglass_index.sqlite` under
/// `app_data_dir`, recovers any gap-fill jobs left `running` by a previous
/// process that didn't shut down cleanly (the same one-time recovery pass
/// the Tauri shell's own `setup()` does before handing `db` to
/// `Engine::start` -- host-lifecycle bootstrap, not something the engine
/// repeats on every call), then starts the gap-fill worker, volume
/// watcher, and rescan scheduler on the engine's own Tokio runtime.
#[pyfunction]
fn init(app_data_dir: String, sidecar_dir: String, max_concurrency: usize, min_idle_seconds: f64) -> PyResult<()> {
    let app_data_dir = PathBuf::from(app_data_dir);
    let db = ::spyglass_core::Db::open(&app_data_dir).map_err(to_py_err)?;
    {
        let conn = db.conn.lock().map_err(to_py_err)?;
        let _ = ::spyglass_core::db::reset_stale_running_jobs(&conn);
        let _ = ::spyglass_core::db::enqueue_pending_gap_fill_jobs(&conn);
    }

    let engine = Engine::start(
        db,
        EngineConfig {
            keyframe_cache_root: app_data_dir.join("keyframes"),
            sidecar_dir: PathBuf::from(sidecar_dir),
            max_concurrency,
            min_idle_seconds,
        },
    );
    ENGINE.set(engine).map_err(|_| PyRuntimeError::new_err("init() was already called once for this process"))
}

/// Default page size for `search_shots`/`browse_shots` when the caller
/// doesn't ask for a specific `limit` -- matches the Tauri commands'
/// previously-hardcoded value, so omitting `limit` behaves exactly as
/// before this became a parameter.
const DEFAULT_RESULT_LIMIT: i64 = 60;

/// Natural-language shot search (mirrors `commands::search_shots`):
/// embeds `query` via the persistent CLIP text-embedding server (started
/// lazily on first use) and ranks shots with `spyglass_core::search`'s
/// hybrid scoring. `filters` is a plain Python dict shaped like
/// `FacetFilters` (`{"tags": [...], "source_app": ..., "date_from": ...,
/// "date_to": ..., "favorites_only": ..., "folder_path": ...}`); `{}`
/// means no filters. `limit` defaults to `DEFAULT_RESULT_LIMIT`; the
/// Suite's "View more results" affordance re-issues the same search with
/// a larger `limit` rather than a true offset -- see api_spyglass.py.
#[pyfunction]
#[pyo3(signature = (query, filters, limit=None))]
fn search_shots(py: Python<'_>, query: String, filters: Bound<'_, PyAny>, limit: Option<i64>) -> PyResult<PyObject> {
    let filters: FacetFilters = depythonize(&filters)?;
    let limit = limit.unwrap_or(DEFAULT_RESULT_LIMIT);
    let state = state()?;

    let results: Vec<ShotSearchResult> = py
        .allow_threads(|| -> Result<Vec<ShotSearchResult>, String> {
            let embedding = {
                let mut guard = state.embed_server.lock().map_err(|e| e.to_string())?;
                spyglass_engine::embed_server::embed_with_restart(&mut guard, &state.sidecar_dir, &query)?
            };
            let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
            ::spyglass_core::search::search_shots(&conn, &query, &embedding, &filters, limit).map_err(|e| e.to_string())
        })
        .map_err(PyRuntimeError::new_err)?;

    pythonize(py, &results).map_err(to_py_err).map(|b| b.into())
}

/// "Find shots like this" (mirrors `commands::find_similar_shots`):
/// visual-similarity search seeded from a reference shot already in view.
#[pyfunction]
fn find_similar_shots(py: Python<'_>, shot_id: i64) -> PyResult<PyObject> {
    let state = state()?;
    let results: Vec<ShotSearchResult> = py
        .allow_threads(|| -> Result<Vec<ShotSearchResult>, String> {
            let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
            ::spyglass_core::search::find_similar_shots(&conn, shot_id, 60).map_err(|e| e.to_string())
        })
        .map_err(PyRuntimeError::new_err)?;
    pythonize(py, &results).map_err(to_py_err).map(|b| b.into())
}

/// Facet-only browsing (mirrors `commands::browse_shots`) -- no embed-
/// server round trip needed since there's no text query. `limit` defaults
/// to `DEFAULT_RESULT_LIMIT`, same convention as `search_shots`.
#[pyfunction]
#[pyo3(signature = (filters, limit=None))]
fn browse_shots(py: Python<'_>, filters: Bound<'_, PyAny>, limit: Option<i64>) -> PyResult<PyObject> {
    let filters: FacetFilters = depythonize(&filters)?;
    let limit = limit.unwrap_or(DEFAULT_RESULT_LIMIT);
    let state = state()?;
    let results: Vec<ShotSearchResult> = py
        .allow_threads(|| -> Result<Vec<ShotSearchResult>, String> {
            let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
            ::spyglass_core::facets::browse_shots(&conn, &filters, limit).map_err(|e| e.to_string())
        })
        .map_err(PyRuntimeError::new_err)?;
    pythonize(py, &results).map_err(to_py_err).map(|b| b.into())
}

/// Populates the facet sidebar (mirrors `commands::list_facet_options`):
/// every tag/source value with its shot count, plus the archive's
/// ingested-date bounds.
#[pyfunction]
fn list_facet_options(py: Python<'_>) -> PyResult<PyObject> {
    let state = state()?;
    let options: ::spyglass_core::facets::FacetOptions = py
        .allow_threads(|| -> Result<_, String> {
            let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
            ::spyglass_core::facets::list_facet_options(&conn).map_err(|e| e.to_string())
        })
        .map_err(PyRuntimeError::new_err)?;
    pythonize(py, &options).map_err(to_py_err).map(|b| b.into())
}

/// Folder-tree panel (Suite Search workspace): `parent_path=None` returns
/// the watched roots themselves; `parent_path=Some(path)` returns that
/// path's immediate child directories one level deeper. See
/// `spyglass_core::folders` for why this is derived on demand rather than
/// stored.
#[pyfunction]
#[pyo3(signature = (parent_path=None))]
fn list_folder_children(py: Python<'_>, parent_path: Option<String>) -> PyResult<PyObject> {
    let state = state()?;
    let nodes: Vec<::spyglass_core::folders::FolderNode> = py
        .allow_threads(|| -> Result<_, String> {
            let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
            ::spyglass_core::folders::list_folder_children(&conn, parent_path.as_deref()).map_err(|e| e.to_string())
        })
        .map_err(PyRuntimeError::new_err)?;
    pythonize(py, &nodes).map_err(to_py_err).map(|b| b.into())
}

#[pyfunction]
fn search_transcripts(py: Python<'_>, query: String) -> PyResult<PyObject> {
    let state = state()?;
    let results: Vec<TranscriptSearchResult> = py
        .allow_threads(|| -> Result<Vec<TranscriptSearchResult>, String> {
            let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
            ::spyglass_core::db::search_transcripts(&conn, &query, 50).map_err(|e| e.to_string())
        })
        .map_err(PyRuntimeError::new_err)?;
    pythonize(py, &results).map_err(to_py_err).map(|b| b.into())
}

#[pyfunction]
fn list_favorite_shots(py: Python<'_>) -> PyResult<PyObject> {
    let state = state()?;
    let results: Vec<ShotSearchResult> = py
        .allow_threads(|| -> Result<Vec<ShotSearchResult>, String> {
            let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
            ::spyglass_core::search::list_favorite_shots(&conn).map_err(|e| e.to_string())
        })
        .map_err(PyRuntimeError::new_err)?;
    pythonize(py, &results).map_err(to_py_err).map(|b| b.into())
}

// ---------------------------------------------------------------------------
// Phase 3: tag/favorite mutations, pool tray CRUD, watched-root management,
// gap-fill queue control, and consolidate/XMEML export -- mirrors the
// remaining surface of `commands.rs` not already bound in Phase 2. A few
// small structs below (`ScanResultPy`, `WatchedRootStatus`,
// `BackgroundWorkStatus`, `ConsolidateEstimate`) mirror commands.rs's own
// locally-defined (not `spyglass_core`-exported) result shapes, since
// those were never part of spyglass_core's own public API either -- the
// Tauri shell defined them itself, and this binding does the same.
// ---------------------------------------------------------------------------

#[derive(Serialize)]
struct ScanResultPy {
    discovered: u64,
    registered: u64,
    already_registered: u64,
    excluded_removed: u64,
    relinked: u64,
    errors: Vec<String>,
}

impl From<::spyglass_core::scanner::ScanStats> for ScanResultPy {
    fn from(s: ::spyglass_core::scanner::ScanStats) -> Self {
        ScanResultPy {
            discovered: s.discovered,
            registered: s.registered,
            already_registered: s.already_registered,
            excluded_removed: s.excluded_removed,
            relinked: s.relinked,
            errors: Vec::new(),
        }
    }
}

#[derive(Serialize)]
struct WatchedRootStatusPy {
    #[serde(flatten)]
    root: WatchedRoot,
    is_online: bool,
    progress: GapFillProgress,
}

#[derive(Serialize)]
struct BackgroundWorkStatusPy {
    manually_paused: bool,
    idle_seconds: Option<f64>,
    min_idle_seconds: f64,
    force_active: bool,
}

#[derive(Serialize)]
struct ConsolidateEstimatePy {
    file_count: usize,
    total_bytes: u64,
    available_bytes: u64,
    destination_has_existing_files: bool,
}

// ---------------- Tag correction / favoriting ----------------

#[pyfunction]
fn add_tag(shot_id: i64, label: String) -> PyResult<()> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    ::spyglass_core::db::add_human_tag(&conn, shot_id, label.trim()).map_err(to_py_err)
}

#[pyfunction]
fn remove_tag(shot_id: i64, label: String) -> PyResult<()> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    ::spyglass_core::db::remove_tag(&conn, shot_id, &label).map_err(to_py_err)
}

#[pyfunction]
fn set_shot_favorite(shot_id: i64, favorite: bool) -> PyResult<()> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    ::spyglass_core::db::set_shot_favorite(&conn, shot_id, favorite).map_err(to_py_err)
}

/// Retroactive cleanup (mirrors `commands::purge_bad_tags`): deletes every
/// unreviewed VLM-generated tag containing a digit (jersey numbers,
/// scoreboard scores/clocks, years on a sign), an onscreen-text-shaped tag
/// (quoted, UI-role-suffixed like `"united" logo`, sentence-punctuated, or
/// implausibly long), or a whole-word gender/headcount term ("boy"/"girl"/
/// "two boys") -- see `spyglass_core::db::purge_onscreen_text_tags`,
/// `purge_ui_text_tags`, and `purge_gender_tags` for the full rationale and
/// their scope limits (doesn't touch human tags or catch a digit-free,
/// unpunctuated, un-quoted bare-word name/text leak). Returns the total
/// number removed across all three passes.
#[pyfunction]
fn purge_onscreen_text_tags() -> PyResult<usize> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    let onscreen_text = ::spyglass_core::db::purge_onscreen_text_tags(&conn).map_err(to_py_err)?;
    let ui_text = ::spyglass_core::db::purge_ui_text_tags(&conn).map_err(to_py_err)?;
    let gender = ::spyglass_core::db::purge_gender_tags(&conn).map_err(to_py_err)?;
    Ok(onscreen_text + ui_text + gender)
}

// ---------------- Pool tray ----------------

#[pyfunction]
fn get_pool(py: Python<'_>) -> PyResult<PyObject> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    let pool = ::spyglass_core::pool::get_or_create_default_pool(&conn).map_err(to_py_err)?;
    let shots: Vec<ShotSearchResult> = ::spyglass_core::pool::list_shots(&conn, pool.id).map_err(to_py_err)?;
    pythonize(py, &shots).map_err(to_py_err).map(|b| b.into())
}

#[pyfunction]
fn add_shot_to_pool(shot_id: i64) -> PyResult<()> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    let pool = ::spyglass_core::pool::get_or_create_default_pool(&conn).map_err(to_py_err)?;
    ::spyglass_core::pool::add_shot(&conn, pool.id, shot_id).map_err(to_py_err)
}

#[pyfunction]
fn remove_shot_from_pool(shot_id: i64) -> PyResult<()> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    let pool = ::spyglass_core::pool::get_or_create_default_pool(&conn).map_err(to_py_err)?;
    ::spyglass_core::pool::remove_shot(&conn, pool.id, shot_id).map_err(to_py_err)
}

#[pyfunction]
fn reorder_pool(shot_ids: Vec<i64>) -> PyResult<()> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    let pool = ::spyglass_core::pool::get_or_create_default_pool(&conn).map_err(to_py_err)?;
    ::spyglass_core::pool::reorder(&conn, pool.id, &shot_ids).map_err(to_py_err)
}

#[pyfunction]
fn clear_pool() -> PyResult<()> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    let pool = ::spyglass_core::pool::get_or_create_default_pool(&conn).map_err(to_py_err)?;
    ::spyglass_core::pool::clear(&conn, pool.id).map_err(to_py_err)
}

/// Exports the pool tray, in its current order, as a Premiere Pro XMEML
/// sequence at `destination_path`. Mirrors `commands::export_pool_to_premiere_xml`.
#[pyfunction]
fn export_pool_to_premiere_xml(destination_path: String, sequence_name: String) -> PyResult<String> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    let pool = ::spyglass_core::pool::get_or_create_default_pool(&conn).map_err(to_py_err)?;
    let shots: Vec<ShotSearchResult> = ::spyglass_core::pool::list_shots(&conn, pool.id).map_err(to_py_err)?;
    if shots.is_empty() {
        return Err(PyRuntimeError::new_err("the pool is empty -- add shots before exporting"));
    }

    let clips: Vec<::spyglass_core::xmeml::XmemlClip> = shots
        .iter()
        .map(|shot| ::spyglass_core::xmeml::XmemlClip {
            file_path: shot.clip_file_path.clone(),
            name: Path::new(&shot.clip_file_path)
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_else(|| shot.clip_file_path.clone()),
            frame_rate: shot.clip_frame_rate,
            in_seconds: shot.start_tc,
            out_seconds: shot.end_tc,
            audio_format: ::spyglass_core::ffprobe::probe_audio_format(Path::new(&shot.clip_file_path)).ok().flatten(),
        })
        .collect();

    let options = ::spyglass_core::xmeml::XmemlOptions { sequence_name, ..Default::default() };
    let xml = ::spyglass_core::xmeml::build_sequence_xml(&clips, &options);
    std::fs::write(&destination_path, xml).map_err(to_py_err)?;
    Ok(destination_path)
}

// ---------------- Watched roots ----------------

#[pyfunction]
fn list_watched_roots(py: Python<'_>) -> PyResult<PyObject> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    let roots = ::spyglass_core::db::list_visible_watched_roots(&conn).map_err(to_py_err)?;
    let statuses: Vec<WatchedRootStatusPy> = roots
        .into_iter()
        .map(|root| {
            let progress = ::spyglass_core::db::gap_fill_progress_for_root(&conn, &root.path).unwrap_or_default();
            let is_online = Path::new(&root.path).exists();
            WatchedRootStatusPy { root, is_online, progress }
        })
        .collect();
    pythonize(py, &statuses).map_err(to_py_err).map(|b| b.into())
}

#[pyfunction]
#[pyo3(signature = (label, path, volume_id=None, approved_by=None))]
fn add_watched_root(
    py: Python<'_>,
    label: String,
    path: String,
    volume_id: Option<String>,
    approved_by: Option<String>,
) -> PyResult<PyObject> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    let root = ::spyglass_core::db::add_watched_root(
        &conn,
        &::spyglass_core::models::NewWatchedRoot { label, path, volume_id, approved_by },
    )
    .map_err(to_py_err)?;
    pythonize(py, &root).map_err(to_py_err).map(|b| b.into())
}

#[pyfunction]
fn set_watched_root_access_level(id: i64, access_level: String) -> PyResult<()> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    ::spyglass_core::db::set_watched_root_access_level(&conn, id, AccessLevel::from_str(&access_level)).map_err(to_py_err)
}

/// Destructive: purges every clip (and cascading shots/tags/embeddings/
/// transcript segments) registered under this root's path. The caller is
/// responsible for confirming with the user before calling this.
#[pyfunction]
fn remove_watched_root(id: i64) -> PyResult<()> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    ::spyglass_core::db::remove_watched_root(&conn, id).map_err(to_py_err)
}

/// "Start fresh" for one watched root without touching any other folder's
/// index -- see `spyglass_core::db::reset_watched_root`'s doc comment for
/// the full rationale (the `TAGS_PROMPT` prompt-echo bug this exists for:
/// literal example tags the VLM parroted back regardless of a shot's real
/// content). Also deletes each purged clip's cached keyframe directory,
/// which isn't a database row and so doesn't cascade with the rest.
/// Destructive -- the caller is responsible for confirming with the user
/// before calling this, same contract as `remove_watched_root`. Returns
/// the number of clips purged.
#[pyfunction]
fn reset_watched_root(id: i64) -> PyResult<usize> {
    let state = state()?;
    let result = {
        let conn = state.db.conn.lock().map_err(to_py_err)?;
        ::spyglass_core::db::reset_watched_root(&conn, id).map_err(to_py_err)?
    };
    if let Some(keyframe_root) = state.db.path.parent().map(|p| p.join("keyframes")) {
        for clip_id in &result.removed_clip_ids {
            let _ = std::fs::remove_dir_all(keyframe_root.join(clip_id.to_string()));
        }
    }
    Ok(result.clips_removed)
}

/// Retroactive repair (mirrors `commands::requeue_short_shot_clips`) for
/// clips indexed before `sidecar/analyze_clip.py`'s scene-cut detector
/// sensitivity fix: finds every clip with at least one shot shorter than
/// `spyglass_core::db::MIN_SHOT_DURATION_SEC` (fast pans, camera flashes,
/// and quick highlight-reel cuts used to register as their own spurious
/// sub-second "shots"), wipes just those clips' shots/tags/embeddings, and
/// requeues them for a fresh gap-fill pass -- clips that never had the
/// problem are left untouched, unlike `reset_watched_root`'s whole-folder
/// wipe. Also deletes each affected clip's cached keyframe directory,
/// since re-analysis producing fewer (merged) shots than before would
/// otherwise leave the old higher-numbered keyframe JPEGs behind as
/// orphaned files. Destructive -- the caller is responsible for
/// confirming with the user before calling this. Returns the number of
/// clips requeued; the actual re-analysis happens asynchronously via the
/// normal gap-fill worker queue.
#[pyfunction]
fn requeue_short_shot_clips() -> PyResult<usize> {
    let state = state()?;
    let result = {
        let conn = state.db.conn.lock().map_err(to_py_err)?;
        let clip_ids = ::spyglass_core::db::find_clips_with_short_shots(&conn, ::spyglass_core::db::MIN_SHOT_DURATION_SEC)
            .map_err(to_py_err)?;
        ::spyglass_core::db::requeue_clips_with_short_shots(&conn, &clip_ids).map_err(to_py_err)?
    };
    if let Some(keyframe_root) = state.db.path.parent().map(|p| p.join("keyframes")) {
        for clip_id in &result.requeued_clip_ids {
            let _ = std::fs::remove_dir_all(keyframe_root.join(clip_id.to_string()));
        }
    }
    Ok(result.clips_requeued)
}

#[pyfunction]
fn relink_watched_root(py: Python<'_>, id: i64, new_path: String) -> PyResult<PyObject> {
    if !Path::new(&new_path).is_dir() {
        return Err(PyRuntimeError::new_err(format!("{new_path} does not exist or is not a folder")));
    }
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    let root = ::spyglass_core::db::relink_watched_root_path(&conn, id, &new_path).map_err(to_py_err)?;
    pythonize(py, &root).map_err(to_py_err).map(|b| b.into())
}

/// Walks `root_id`'s path and registers/relinks discovered media, exactly
/// as `commands::scan_watched_root` does -- a genuinely slow (potentially
/// multi-minute) filesystem walk + checksum pass, hence `py.allow_threads`
/// here specifically. The Suite's own caller wraps this in a background
/// thread job for progress/cancel UX (see spyglass_bridge.py); this
/// function itself is a plain, synchronous, blocking call.
#[pyfunction]
fn scan_watched_root(py: Python<'_>, root_id: i64) -> PyResult<PyObject> {
    let state = state()?;
    let result: ScanResultPy = py
        .allow_threads(|| -> Result<ScanResultPy, String> {
            let _scan_guard = state
                .try_start_scan(root_id)
                .ok_or_else(|| "this folder is already being scanned in the background -- try again shortly".to_string())?;

            let root = {
                let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
                ::spyglass_core::db::list_watched_roots(&conn)
                    .map_err(|e| e.to_string())?
                    .into_iter()
                    .find(|r| r.id == root_id)
                    .ok_or_else(|| format!("watched root {root_id} not found"))?
            };

            let extensions: Vec<String> = ::spyglass_core::scanner::DEFAULT_EXTENSIONS.iter().map(|s| s.to_string()).collect();
            let stats = ::spyglass_core::scanner::rescan_root(&state.db, &root, &extensions).map_err(|e| e.to_string())?;
            Ok(stats.into())
        })
        .map_err(PyRuntimeError::new_err)?;
    pythonize(py, &result).map_err(to_py_err).map(|b| b.into())
}

// ---------------- Gap-fill queue ----------------

#[pyfunction]
fn enqueue_gap_fill() -> PyResult<usize> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    ::spyglass_core::db::enqueue_pending_gap_fill_jobs(&conn).map_err(to_py_err)
}

#[pyfunction]
#[pyo3(signature = (root_id=None))]
fn retry_failed_jobs(root_id: Option<i64>) -> PyResult<usize> {
    let state = state()?;
    let conn = state.db.conn.lock().map_err(to_py_err)?;
    let root_path = match root_id {
        Some(id) => Some(
            ::spyglass_core::db::list_watched_roots(&conn)
                .map_err(to_py_err)?
                .into_iter()
                .find(|r| r.id == id)
                .ok_or_else(|| PyRuntimeError::new_err(format!("watched root {id} not found")))?
                .path,
        ),
        None => None,
    };
    ::spyglass_core::db::retry_failed_jobs(&conn, root_path.as_deref()).map_err(to_py_err)
}

#[pyfunction]
fn set_queue_paused(paused: bool) -> PyResult<()> {
    let state = state()?;
    state.queue_control.paused.store(paused, std::sync::atomic::Ordering::Relaxed);
    Ok(())
}

#[pyfunction]
fn get_queue_paused() -> PyResult<bool> {
    let state = state()?;
    Ok(state.queue_control.paused.load(std::sync::atomic::Ordering::Relaxed))
}

/// "Process now" override -- see `spyglass_engine::idle`'s doc comment and
/// the Tauri shell's `commands::force_gap_fill_now`, which this mirrors.
/// Bypasses the idle-time gate (but not a manual pause) until the pending
/// queue drains, at which point the gap-fill worker clears it on its own.
#[pyfunction]
fn force_gap_fill_now() -> PyResult<()> {
    let state = state()?;
    state.queue_control.force_active.store(true, std::sync::atomic::Ordering::Relaxed);
    Ok(())
}

#[pyfunction]
fn get_background_work_status(py: Python<'_>) -> PyResult<PyObject> {
    let state = state()?;
    let status = BackgroundWorkStatusPy {
        manually_paused: state.queue_control.paused.load(std::sync::atomic::Ordering::Relaxed),
        idle_seconds: spyglass_engine::idle::system_idle_seconds(),
        min_idle_seconds: 20.0,
        force_active: state.queue_control.force_active.load(std::sync::atomic::Ordering::Relaxed),
    };
    pythonize(py, &status).map_err(to_py_err).map(|b| b.into())
}

// ---------------- Consolidate & Copy export ----------------

/// Resolves the current pool's shots into `ConsolidateClip`s, mirroring
/// `commands::resolve_consolidate_clips` exactly (including the same
/// `with_audio_format` skip-for-a-plain-estimate optimization).
fn resolve_consolidate_clips(
    conn: &rusqlite::Connection,
    with_audio_format: bool,
) -> Result<Vec<::spyglass_core::consolidate::ConsolidateClip>, String> {
    let pool = ::spyglass_core::pool::get_or_create_default_pool(conn).map_err(|e| e.to_string())?;
    let shots: Vec<ShotSearchResult> = ::spyglass_core::pool::list_shots(conn, pool.id).map_err(|e| e.to_string())?;
    if shots.is_empty() {
        return Err("the pool is empty -- add shots before exporting".to_string());
    }
    shots
        .into_iter()
        .map(|shot| {
            let clip = ::spyglass_core::db::find_clip_by_id(conn, shot.clip_id)
                .map_err(|e| e.to_string())?
                .ok_or_else(|| format!("clip {} no longer exists", shot.clip_id))?;
            let audio_format = if with_audio_format {
                ::spyglass_core::ffprobe::probe_audio_format(Path::new(&clip.file_path)).ok().flatten()
            } else {
                None
            };
            Ok(::spyglass_core::consolidate::ConsolidateClip {
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

#[pyfunction]
fn estimate_consolidate_export(py: Python<'_>, destination_path: String, copy_mode: Bound<'_, PyAny>) -> PyResult<PyObject> {
    let copy_mode: CopyMode = depythonize(&copy_mode)?;
    let state = state()?;
    let estimate: ConsolidateEstimatePy = py
        .allow_threads(|| -> Result<ConsolidateEstimatePy, String> {
            let clips = {
                let conn = state.db.conn.lock().map_err(|e| e.to_string())?;
                resolve_consolidate_clips(&conn, false)?
            };
            let size = ::spyglass_core::consolidate::estimate_export_size(&clips, &copy_mode);
            let destination = Path::new(&destination_path);
            let available_bytes = ::spyglass_core::consolidate::available_bytes(destination).map_err(|e| e.to_string())?;
            let destination_has_existing_files = ::spyglass_core::consolidate::destination_has_existing_files(destination);
            Ok(ConsolidateEstimatePy {
                file_count: size.file_count,
                total_bytes: size.total_bytes,
                available_bytes,
                destination_has_existing_files,
            })
        })
        .map_err(PyRuntimeError::new_err)?;
    pythonize(py, &estimate).map_err(to_py_err).map(|b| b.into())
}

/// Kicks off the export on spyglass-engine's own background OS thread
/// (`consolidate_export::run_in_background`) and returns immediately --
/// the caller polls `get_consolidate_export_status` for progress, exactly
/// as the standalone Tauri app's own frontend does. Deliberately NOT
/// wrapped in the Suite's `jobs.start_thread_job`: Spyglass already owns a
/// complete, working progress-tracking mechanism for this
/// (`ConsolidateExportSlot` in `EngineState`), and routing it through a
/// second, parallel job-tracking system would mean keeping two sources of
/// truth for the same operation in sync for no real benefit.
#[pyfunction]
fn start_consolidate_export(
    destination_path: String,
    pool_name: String,
    copy_mode: Bound<'_, PyAny>,
    folder_structure: Bound<'_, PyAny>,
) -> PyResult<()> {
    let copy_mode: CopyMode = depythonize(&copy_mode)?;
    let folder_structure: FolderStructure = depythonize(&folder_structure)?;
    let state = state()?;
    let clips = {
        let conn = state.db.conn.lock().map_err(to_py_err)?;
        resolve_consolidate_clips(&conn, true).map_err(PyRuntimeError::new_err)?
    };
    let destination_root = PathBuf::from(&destination_path);
    let plan = ::spyglass_core::consolidate::build_export_plan(&pool_name, &clips, &destination_root, folder_structure);
    spyglass_engine::consolidate_export::run_in_background(state.consolidate_export.clone(), destination_root, plan, copy_mode);
    Ok(())
}

#[pyfunction]
fn get_consolidate_export_status(py: Python<'_>) -> PyResult<PyObject> {
    let state = state()?;
    let status = state.consolidate_export.lock().map_err(to_py_err)?.clone();
    pythonize(py, &status).map_err(to_py_err).map(|b| b.into())
}

/// The paired "XML pointing at copied files" export -- builds a second
/// Premiere XMEML sequence referencing the just-copied files instead of
/// the original archive paths, from the most recently completed
/// consolidate export's own manifest.
#[pyfunction]
fn export_copied_files_to_premiere_xml(destination_path: String, sequence_name: String) -> PyResult<String> {
    let state = state()?;
    let manifest = {
        let status = state.consolidate_export.lock().map_err(to_py_err)?;
        status
            .as_ref()
            .and_then(|s| s.manifest.clone())
            .ok_or_else(|| PyRuntimeError::new_err("no completed consolidate export to build a copied-files XML from"))?
    };
    let clips = ::spyglass_core::consolidate::manifest_to_xmeml_clips(&manifest);
    let options = ::spyglass_core::xmeml::XmemlOptions { sequence_name, ..Default::default() };
    let xml = ::spyglass_core::xmeml::build_sequence_xml(&clips, &options);
    std::fs::write(&destination_path, xml).map_err(to_py_err)?;
    Ok(destination_path)
}

// ---------------------------------------------------------------------------
// Phase 1 spike functions -- kept as cheap, dependency-free sanity checks
// (does the module still import and run correctly after later changes),
// not part of the real Spyglass surface.
// ---------------------------------------------------------------------------

#[pyfunction]
fn ping(n: i64) -> i64 {
    n + 1
}

#[pyfunction]
fn ping_after_delay(py: Python<'_>, ms: u64) -> PyResult<i64> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    ENGINE
        .get()
        .ok_or_else(|| PyRuntimeError::new_err("call spyglass_core.init() first"))?
        .runtime_handle()
        .spawn(async move {
            tokio::time::sleep(Duration::from_millis(ms)).await;
            let _ = tx.send(42i64);
        });
    py.allow_threads(|| rx.blocking_recv()).map_err(|_| PyRuntimeError::new_err("engine task dropped its response sender"))
}

/// Deliberately panics -- proves a panicking `#[pyfunction]` surfaces as a
/// normal Python exception rather than aborting the process. See Phase 1's
/// module doc comment for why this property matters.
#[pyfunction]
fn deliberately_panic() -> PyResult<()> {
    panic!("spyglass_core.deliberately_panic: this panic must surface as a Python exception, not abort the process");
}

#[pymodule]
fn spyglass_core(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(init, m)?)?;
    m.add_function(wrap_pyfunction!(search_shots, m)?)?;
    m.add_function(wrap_pyfunction!(find_similar_shots, m)?)?;
    m.add_function(wrap_pyfunction!(browse_shots, m)?)?;
    m.add_function(wrap_pyfunction!(list_facet_options, m)?)?;
    m.add_function(wrap_pyfunction!(list_folder_children, m)?)?;
    m.add_function(wrap_pyfunction!(search_transcripts, m)?)?;
    m.add_function(wrap_pyfunction!(list_favorite_shots, m)?)?;
    m.add_function(wrap_pyfunction!(add_tag, m)?)?;
    m.add_function(wrap_pyfunction!(remove_tag, m)?)?;
    m.add_function(wrap_pyfunction!(set_shot_favorite, m)?)?;
    m.add_function(wrap_pyfunction!(purge_onscreen_text_tags, m)?)?;
    m.add_function(wrap_pyfunction!(get_pool, m)?)?;
    m.add_function(wrap_pyfunction!(add_shot_to_pool, m)?)?;
    m.add_function(wrap_pyfunction!(remove_shot_from_pool, m)?)?;
    m.add_function(wrap_pyfunction!(reorder_pool, m)?)?;
    m.add_function(wrap_pyfunction!(clear_pool, m)?)?;
    m.add_function(wrap_pyfunction!(export_pool_to_premiere_xml, m)?)?;
    m.add_function(wrap_pyfunction!(list_watched_roots, m)?)?;
    m.add_function(wrap_pyfunction!(add_watched_root, m)?)?;
    m.add_function(wrap_pyfunction!(set_watched_root_access_level, m)?)?;
    m.add_function(wrap_pyfunction!(remove_watched_root, m)?)?;
    m.add_function(wrap_pyfunction!(reset_watched_root, m)?)?;
    m.add_function(wrap_pyfunction!(requeue_short_shot_clips, m)?)?;
    m.add_function(wrap_pyfunction!(relink_watched_root, m)?)?;
    m.add_function(wrap_pyfunction!(scan_watched_root, m)?)?;
    m.add_function(wrap_pyfunction!(enqueue_gap_fill, m)?)?;
    m.add_function(wrap_pyfunction!(retry_failed_jobs, m)?)?;
    m.add_function(wrap_pyfunction!(set_queue_paused, m)?)?;
    m.add_function(wrap_pyfunction!(get_queue_paused, m)?)?;
    m.add_function(wrap_pyfunction!(force_gap_fill_now, m)?)?;
    m.add_function(wrap_pyfunction!(get_background_work_status, m)?)?;
    m.add_function(wrap_pyfunction!(estimate_consolidate_export, m)?)?;
    m.add_function(wrap_pyfunction!(start_consolidate_export, m)?)?;
    m.add_function(wrap_pyfunction!(get_consolidate_export_status, m)?)?;
    m.add_function(wrap_pyfunction!(export_copied_files_to_premiere_xml, m)?)?;
    native_preview::register(m)?;
    m.add_function(wrap_pyfunction!(ping, m)?)?;
    m.add_function(wrap_pyfunction!(ping_after_delay, m)?)?;
    m.add_function(wrap_pyfunction!(deliberately_panic, m)?)?;
    Ok(())
}
