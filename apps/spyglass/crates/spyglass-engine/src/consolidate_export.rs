//! Runs the Consolidate & Copy export (Section 15) as a background task.
//! This app has no push-event plumbing yet (every other long-running piece
//! -- the gap-fill queue -- is polled from the frontend via a status
//! command, not pushed), so a shared status slot in `AppState` plus a
//! start/poll command pair matches that existing pattern rather than
//! introducing event emission just for this one feature.

use spyglass_core::consolidate::{self, CopyMode, ExportPlanEntry, ManifestEntry};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

#[derive(Debug, Clone, serde::Serialize)]
pub struct ConsolidateExportStatus {
    pub completed: usize,
    pub total: usize,
    pub current_file: String,
    pub finished: bool,
    pub error: Option<String>,
    /// Populated once `finished` -- lets the frontend offer the paired
    /// "XML pointing at copied files" export (Section 15) right after a
    /// successful run without a second round trip to re-read the manifest
    /// off disk.
    pub manifest: Option<Vec<ManifestEntry>>,
}

pub type ConsolidateExportSlot = Arc<Mutex<Option<ConsolidateExportStatus>>>;

/// Starts the export on a plain OS thread -- deliberately not tied to the
/// Tokio runtime, since `consolidate::run_consolidate_export` is a
/// synchronous, blocking loop with no `.await` points and no need to share
/// the DB connection (the plan is already fully resolved before this is
/// called). A second call while one is still running just replaces the
/// tracked status; the frontend is responsible for disabling the export
/// action while `finished` is false.
pub fn run_in_background(status: ConsolidateExportSlot, destination_root: PathBuf, plan: Vec<ExportPlanEntry>, copy_mode: CopyMode) {
    let total = plan.len();
    {
        let mut s = status.lock().unwrap();
        *s = Some(ConsolidateExportStatus { completed: 0, total, current_file: String::new(), finished: false, error: None, manifest: None });
    }

    std::thread::spawn(move || {
        let status_for_progress = status.clone();
        let result = consolidate::run_consolidate_export(&destination_root, &plan, &copy_mode, move |p| {
            let mut s = status_for_progress.lock().unwrap();
            *s = Some(ConsolidateExportStatus {
                completed: p.completed,
                total: p.total,
                current_file: p.current_file.clone(),
                finished: false,
                error: None,
                manifest: None,
            });
        });

        let mut s = status.lock().unwrap();
        *s = Some(match result {
            Ok(manifest) => {
                ConsolidateExportStatus { completed: total, total, current_file: String::new(), finished: true, error: None, manifest: Some(manifest) }
            }
            Err(err) => {
                ConsolidateExportStatus { completed: 0, total, current_file: String::new(), finished: true, error: Some(err.to_string()), manifest: None }
            }
        });
    });
}
