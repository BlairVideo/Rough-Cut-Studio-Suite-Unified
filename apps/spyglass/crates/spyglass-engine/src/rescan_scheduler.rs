//! Periodic re-walk of watched roots (Section 7: "watched roots are
//! periodically re-walked... to catch newly added footage") -- the manual
//! "Scan now" button already exists (`commands::scan_watched_root`); this
//! loop calls the exact same `scanner::rescan_root` sequence on a timer so
//! a scheduled rescan can never behave differently from the button.
//!
//! Gated by the same shared idle/pause check the gap-fill worker uses
//! (`idle::background_work_allowed`) -- re-walking a large archive tree is
//! disk I/O too, and Section 7's "never block an active edit" applies to
//! discovery just as much as to gap-fill.

use crate::state::EngineState;
use spyglass_core::{db, scanner};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

/// How often the scheduler wakes to check which roots are due. Cheap
/// (a `list_watched_roots` query plus a pure filter), so this can be much
/// more frequent than `rescan_interval` itself without real cost.
pub const DEFAULT_POLL_INTERVAL: Duration = Duration::from_secs(5 * 60);

/// How long since a root's last scan before it's due for another
/// re-walk -- not yet exposed as a user setting, matching the same
/// "fixed conservative default for now" precedent as `MAX_CONCURRENCY`/
/// `MIN_IDLE_SECONDS` in the engine's own configuration.
pub const DEFAULT_RESCAN_INTERVAL: Duration = Duration::from_secs(6 * 60 * 60);

pub async fn run(state: Arc<EngineState>, poll_interval: Duration, rescan_interval: Duration, min_idle_seconds: f64) {
    let mut interval = tokio::time::interval(poll_interval);
    loop {
        interval.tick().await;

        if !crate::idle::background_work_allowed(&state, min_idle_seconds) {
            continue;
        }

        let extensions: Vec<String> = scanner::DEFAULT_EXTENSIONS.iter().map(|s| s.to_string()).collect();

        let due_ids: Vec<i64> = {
            let conn = state.db.conn.lock().unwrap();
            let roots = db::list_watched_roots(&conn).unwrap_or_default();
            scanner::roots_due_for_rescan(&roots, chrono::Utc::now(), rescan_interval)
                .into_iter()
                .filter(|root| Path::new(&root.path).exists())
                .map(|root| root.id)
                .collect()
        };

        // Walking a due root and checksumming its newly discovered files is
        // real, potentially multi-minute disk I/O. Running it directly in
        // this async loop (as opposed to `spawn_blocking`) would tie up a
        // tokio worker thread for that whole time, and holding the single
        // shared `Connection`'s lock across every due root in one go would
        // additionally stall any other command needing the database (a
        // manual "Scan now", search, the gap-fill worker) until the entire
        // batch finished. Each root gets its own short-lived lock instead.
        let state_for_blocking = state.clone();
        let extensions_for_blocking = extensions.clone();
        let _ = tokio::task::spawn_blocking(move || {
            for root_id in due_ids {
                // A manual "Scan now" click may already be walking this
                // exact root -- skip it this tick rather than race it
                // (see `EngineState::try_start_scan`'s doc comment). It'll
                // be picked up again on the next poll if it's still due.
                let Some(_scan_guard) = state_for_blocking.try_start_scan(root_id) else {
                    continue;
                };

                let root = {
                    let conn = state_for_blocking.db.conn.lock().unwrap();
                    db::list_watched_roots(&conn).unwrap_or_default().into_iter().find(|r| r.id == root_id)
                };
                let Some(root) = root else {
                    continue;
                };
                let _ = scanner::rescan_root(&state_for_blocking.db, &root, &extensions_for_blocking);
            }
        })
        .await;
    }
}
