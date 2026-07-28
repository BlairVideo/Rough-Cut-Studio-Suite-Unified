use crate::consolidate_export::ConsolidateExportSlot;
use crate::embed_server::EmbedServer;
use crate::gap_fill_worker::InFlightRegistry;
use spyglass_core::Db;
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};

/// Manual pause/resume toggle for the gap-fill worker loop, checked
/// alongside the idle-time gate (Section 7). Shared via `Arc` since each
/// worker task and the volume watcher all read it independently.
#[derive(Default)]
pub struct QueueControl {
    pub paused: AtomicBool,
    /// "Process now" override: bypasses the idle-time gate (but not
    /// `paused`) until the pending queue actually drains, at which point
    /// `gap_fill_worker`'s loop clears it on its own -- see that module's
    /// doc comment. Exists because the idle gate alone means a scan run
    /// while the machine is in active use just piles up an ever-growing
    /// backlog of unanalyzed clips with no way to work through it short of
    /// walking away from the keyboard for `min_idle_seconds`.
    pub force_active: AtomicBool,
}

pub struct EngineState {
    pub db: Db,
    pub queue_control: Arc<QueueControl>,
    pub in_flight: Arc<InFlightRegistry>,
    /// Lazily started on first search (model load takes a few seconds --
    /// not worth paying at every app launch if the user never searches).
    pub embed_server: Mutex<Option<EmbedServer>>,
    /// Progress/result of the most recent Consolidate & Copy export
    /// (Section 15), polled by the frontend the same way gap-fill progress
    /// is polled -- see `consolidate_export.rs`.
    pub consolidate_export: ConsolidateExportSlot,
    /// Resolved once at startup by the host (dev-tree path in debug
    /// builds, bundled resource dir in release for the Tauri shell) --
    /// so command handlers never need to re-derive it per call.
    pub sidecar_dir: PathBuf,
    /// Watched-root ids with a scan currently in flight. A manual "Scan
    /// now" click and the periodic `rescan_scheduler` tick both call the
    /// same `scanner::rescan_root` independently -- without this, a root
    /// that's never been scanned (always "due") can end up walked by both
    /// at once, each redundantly re-checksumming whatever tens-of-GB
    /// files the walk hasn't reached yet, roughly doubling I/O load on
    /// what might already be a slow external/network drive.
    pub scanning_roots: Mutex<HashSet<i64>>,
}

impl EngineState {
    pub fn new(db: Db, sidecar_dir: PathBuf) -> Self {
        Self {
            db,
            queue_control: Arc::new(QueueControl::default()),
            in_flight: Arc::new(InFlightRegistry::default()),
            embed_server: Mutex::new(None),
            consolidate_export: Arc::new(Mutex::new(None)),
            sidecar_dir,
            scanning_roots: Mutex::new(HashSet::new()),
        }
    }

    /// Claims `root_id` for scanning, returning `None` if it's already
    /// being scanned elsewhere. The returned guard releases the claim on
    /// drop -- including on an early return via `?` or a panic unwind --
    /// so a failed scan can never leave a root stuck permanently
    /// unscannable.
    pub fn try_start_scan(&self, root_id: i64) -> Option<ScanGuard<'_>> {
        let mut scanning = self.scanning_roots.lock().unwrap();
        if scanning.insert(root_id) {
            Some(ScanGuard { state: self, root_id })
        } else {
            None
        }
    }
}

pub struct ScanGuard<'a> {
    state: &'a EngineState,
    root_id: i64,
}

impl Drop for ScanGuard<'_> {
    fn drop(&mut self) {
        self.state.scanning_roots.lock().unwrap().remove(&self.root_id);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn scratch_state() -> EngineState {
        let n = TMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let db_path = std::env::temp_dir().join(format!("spyglass_state_test_{n}.sqlite"));
        std::fs::remove_file(&db_path).ok();
        let db = Db::open_at(&db_path).unwrap();
        EngineState::new(db, std::env::temp_dir())
    }

    #[test]
    fn try_start_scan_rejects_a_second_claim_on_the_same_root() {
        let state = scratch_state();

        let first = state.try_start_scan(1);
        assert!(first.is_some(), "the first claim on an unclaimed root must succeed");

        let second = state.try_start_scan(1);
        assert!(second.is_none(), "a second concurrent claim on the same root must be rejected");

        // A different root is unaffected by root 1 being claimed.
        let other_root = state.try_start_scan(2);
        assert!(other_root.is_some(), "claiming a different root must not be blocked");
    }

    #[test]
    fn try_start_scan_releases_its_claim_when_the_guard_drops() {
        let state = scratch_state();

        {
            let _guard = state.try_start_scan(1).expect("first claim must succeed");
        } // guard drops here

        let reclaimed = state.try_start_scan(1);
        assert!(reclaimed.is_some(), "dropping the guard must release the root for a later scan");
    }
}
