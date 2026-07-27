//! Disconnect-safe handling for watched roots (Section 9). Polls each
//! *active* watched root's own path for reachability -- not a generic
//! `/Volumes` diff -- since Spyglass only cares about the specific roots
//! it's been approved to touch.
//!
//! Plain polling (not Disk Arbitration FFI) is a deliberate choice here,
//! same call Card Eater already made for its own card-mount detection
//! (`volume_watcher.rs` there): disconnect handling doesn't need
//! millisecond responsiveness, and this avoids a new dependency /
//! Objective-C bridge. What actually matters for Section 9's "don't hang
//! behind a stale file handle" concern is handled by the in-flight
//! cancellation registry, not by how fast the poll notices the unmount.

use crate::state::EngineState;
use spyglass_core::db;
use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

const POLL_INTERVAL: Duration = Duration::from_secs(2);

/// Diffs a previous/current online-state snapshot. Pure and
/// dependency-free so it's directly unit-testable, mirroring Card Eater's
/// own `diff_volumes` extraction.
pub(crate) fn diff_online_state(
    previous: &HashMap<i64, bool>,
    current: &HashMap<i64, bool>,
) -> (Vec<i64>, Vec<i64>) {
    let mut newly_offline = Vec::new();
    let mut newly_online = Vec::new();
    for (&root_id, &now_online) in current {
        match previous.get(&root_id) {
            Some(&was_online) if was_online != now_online => {
                if now_online {
                    newly_online.push(root_id);
                } else {
                    newly_offline.push(root_id);
                }
            }
            None if !now_online => newly_offline.push(root_id),
            _ => {}
        }
    }
    (newly_offline, newly_online)
}

fn is_reachable(path: &str) -> bool {
    Path::new(path).exists()
}

pub async fn run(state: Arc<EngineState>) {
    let mut known: HashMap<i64, bool> = HashMap::new();
    let mut interval = tokio::time::interval(POLL_INTERVAL);

    loop {
        interval.tick().await;

        let roots = {
            let conn = state.db.conn.lock().unwrap();
            db::list_watched_roots(&conn).unwrap_or_default()
        };

        let current: HashMap<i64, bool> = roots
            .iter()
            .filter(|r| r.access_level == "active")
            .map(|r| (r.id, is_reachable(&r.path)))
            .collect();

        let (newly_offline, newly_online) = diff_online_state(&known, &current);

        if !newly_offline.is_empty() || !newly_online.is_empty() {
            let conn = state.db.conn.lock().unwrap();
            for root_id in &newly_offline {
                let Some(root) = roots.iter().find(|r| r.id == *root_id) else { continue };
                let _ = db::mark_jobs_awaiting_reconnect(&conn, &root.path);
                let root_path = root.path.clone();
                state.in_flight.cancel_under_root(&root_path, |clip_id| {
                    db::find_clip_by_id(&conn, clip_id).ok().flatten().map(|c| c.file_path)
                });
            }
            for root_id in &newly_online {
                let Some(root) = roots.iter().find(|r| r.id == *root_id) else { continue };
                let _ = db::requeue_jobs_on_reconnect(&conn, &root.path);
            }
        }

        known = current;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn map(pairs: &[(i64, bool)]) -> HashMap<i64, bool> {
        pairs.iter().cloned().collect()
    }

    #[test]
    fn root_going_offline_is_detected() {
        let previous = map(&[(1, true)]);
        let current = map(&[(1, false)]);
        let (offline, online) = diff_online_state(&previous, &current);
        assert_eq!(offline, vec![1]);
        assert!(online.is_empty());
    }

    #[test]
    fn root_coming_back_online_is_detected() {
        let previous = map(&[(1, false)]);
        let current = map(&[(1, true)]);
        let (offline, online) = diff_online_state(&previous, &current);
        assert!(offline.is_empty());
        assert_eq!(online, vec![1]);
    }

    #[test]
    fn steady_state_reports_no_transitions() {
        let previous = map(&[(1, true), (2, false)]);
        let current = map(&[(1, true), (2, false)]);
        let (offline, online) = diff_online_state(&previous, &current);
        assert!(offline.is_empty());
        assert!(online.is_empty());
    }

    #[test]
    fn newly_added_offline_root_counts_as_offline_not_ignored() {
        // A root added to the allowlist while its drive is already
        // disconnected has no prior snapshot entry -- must still surface
        // as offline rather than being silently skipped.
        let previous = map(&[]);
        let current = map(&[(1, false)]);
        let (offline, online) = diff_online_state(&previous, &current);
        assert_eq!(offline, vec![1]);
        assert!(online.is_empty());
    }

    #[test]
    fn newly_added_online_root_is_not_reported_as_a_transition() {
        // A brand-new root that's already reachable isn't a "just
        // reconnected" event -- there's nothing to requeue for it yet.
        let previous = map(&[]);
        let current = map(&[(1, true)]);
        let (offline, online) = diff_online_state(&previous, &current);
        assert!(offline.is_empty());
        assert!(online.is_empty());
    }
}
