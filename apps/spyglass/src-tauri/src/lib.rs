mod commands;
mod native_video_preview;
mod paths;

use spyglass_core::Db;
use spyglass_engine::{Engine, EngineConfig, EngineState};
use std::sync::Arc;
use tauri::Manager;

/// Background work runs on up to this many sidecar subprocesses in
/// parallel (Section 7: "configurable concurrency limit respecting CPU/GPU
/// headroom"). Not yet exposed as a user setting -- a fixed, conservative
/// default for Phase 2.
const MAX_CONCURRENCY: usize = 2;

/// How long the machine must have seen no keyboard/mouse activity before
/// the queue starts new work (Section 7's idle-resume policy).
pub(crate) const MIN_IDLE_SECONDS: f64 = 20.0;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_process::init())
        .setup(|app| {
            let app_data_dir = app
                .path()
                .app_data_dir()
                .expect("failed to resolve app data dir");
            let db = Db::open(&app_data_dir).expect("failed to open database");

            // Anything registered but never gap-filled in a previous run
            // (including work interrupted by quitting mid-index -- Section
            // 7's resumability requirement) picks back up here. A job the
            // previous run left `running` (it was killed or crashed mid-
            // analysis, since a clean shutdown never leaves one in that
            // state) is reset to `pending` first -- otherwise it would sit
            // forever, never reclaimed by any worker in this or any future
            // run, silently capping how far gap-fill can ever progress.
            //
            // This is host-lifecycle setup (a fresh launch's one-time
            // recovery pass), not something `Engine::start` does on every
            // call -- it happens here, before the engine takes ownership
            // of `db`, same as it always has.
            {
                let conn = db.conn.lock().unwrap();
                let _ = spyglass_core::db::reset_stale_running_jobs(&conn);
                let _ = spyglass_core::db::enqueue_pending_gap_fill_jobs(&conn);
            }

            let sidecar_dir = paths::resolve_sidecar_dir(app.handle());
            if let Some(ffmpeg_bin_dir) = paths::resolve_ffmpeg_bin_dir(app.handle()) {
                spyglass_core::ffmpeg_paths::set_ffmpeg_bin_dir(ffmpeg_bin_dir);
            }

            let engine = Engine::start(
                db,
                EngineConfig {
                    sidecar_dir,
                    keyframe_cache_root: app_data_dir.join("keyframes"),
                    max_concurrency: MAX_CONCURRENCY,
                    min_idle_seconds: MIN_IDLE_SECONDS,
                },
            );
            // Command handlers extract `Arc<EngineState>` (not `Engine`
            // itself) via Tauri's managed state -- the same `Arc` the
            // engine's own background loops hold, so both sides share one
            // `EngineState` instance rather than two independent copies.
            let state: Arc<EngineState> = engine.state.clone();
            app.manage(state);
            // Keeping `engine` alive for the app's lifetime keeps its
            // Tokio runtime (and therefore the background loops spawned
            // on it) running; dropping it would shut the runtime down.
            app.manage(engine);
            app.manage(native_video_preview::NativePreviewState::default());

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::list_watched_roots,
            commands::add_watched_root,
            commands::set_watched_root_access_level,
            commands::remove_watched_root,
            commands::reset_watched_root,
            commands::relink_watched_root,
            commands::scan_watched_root,
            commands::scan_card_eater,
            commands::scan_transcriber_sidecars,
            commands::scan_broll_cache,
            commands::enqueue_gap_fill,
            commands::retry_failed_jobs,
            commands::set_queue_paused,
            commands::get_queue_paused,
            commands::force_gap_fill_now,
            commands::get_background_work_status,
            commands::search_transcripts,
            commands::search_shots,
            commands::find_similar_shots,
            commands::add_tag,
            commands::remove_tag,
            commands::set_shot_favorite,
            commands::list_favorite_shots,
            commands::list_facet_options,
            commands::browse_shots,
            commands::get_pool,
            commands::add_shot_to_pool,
            commands::remove_shot_from_pool,
            commands::reorder_pool,
            commands::clear_pool,
            commands::export_pool_to_premiere_xml,
            commands::estimate_consolidate_export,
            commands::start_consolidate_export,
            commands::get_consolidate_export_status,
            commands::export_copied_files_to_premiere_xml,
            commands::backup_index_now,
            commands::list_backups,
            commands::restore_backup,
            commands::check_index_integrity,
            commands::rebuild_search_index_cmd,
            commands::purge_bad_tags,
            commands::requeue_short_shot_clips,
            commands::prefetch_clip_file,
            native_video_preview::open_native_video_preview,
            native_video_preview::close_native_video_preview,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
