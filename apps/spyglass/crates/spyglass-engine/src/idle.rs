//! macOS system idle-time detection, used to gate background gap-fill work
//! (Section 7: "pause automatically when the machine is under active
//! editing load and resume at idle").
//!
//! This reads HIDIdleTime -- time since the last keyboard/mouse event --
//! which is a reasonable proxy for "not actively sitting at the computer,"
//! not a true CPU/disk-contention signal. Someone scrubbing a timeline with
//! a mouse would show near-zero idle time even though the actual resource
//! contention that matters (disk I/O, CPU) is what Section 7 is really
//! about. Documented simplification: this is the cheap, portable half of
//! that requirement, not the whole thing.

use crate::state::EngineState;
use std::process::Command;
use std::sync::atomic::Ordering;

/// The single gate every background loop (gap-fill worker, rescan
/// scheduler) checks before starting new work: the manual pause toggle
/// wins outright, otherwise a "process now" override (`QueueControl::
/// force_active` -- see its doc comment) lets work proceed regardless of
/// idle state, otherwise the machine must have been idle (Section 7) for
/// at least `min_idle_seconds`. Shared here so the two loops can't drift
/// into checking this two different ways.
pub fn background_work_allowed(state: &EngineState, min_idle_seconds: f64) -> bool {
    let paused = state.queue_control.paused.load(Ordering::Relaxed);
    if paused {
        return false;
    }
    if state.queue_control.force_active.load(Ordering::Relaxed) {
        return true;
    }
    system_idle_seconds()
        .map(|secs| secs >= min_idle_seconds)
        .unwrap_or(true) // unknown idle state: don't block progress indefinitely
}

/// Seconds since the last HID (keyboard/mouse) event, via `ioreg`. Returns
/// `None` if the command fails or its output can't be parsed -- callers
/// should treat that as "unknown, assume active" rather than "idle".
pub fn system_idle_seconds() -> Option<f64> {
    let output = Command::new("ioreg").args(["-c", "IOHIDSystem", "-d", "4"]).output().ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let line = text.lines().find(|l| l.contains("HIDIdleTime"))?;
    let ns_str = line.split('=').nth(1)?.trim();
    let nanoseconds: f64 = ns_str.parse().ok()?;
    Some(nanoseconds / 1_000_000_000.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use spyglass_core::Db;
    use std::sync::atomic::{AtomicU64, Ordering as StdOrdering};

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn scratch_state() -> EngineState {
        let n = TMP_COUNTER.fetch_add(1, StdOrdering::SeqCst);
        let db_path = std::env::temp_dir().join(format!("spyglass_idle_test_{n}.sqlite"));
        std::fs::remove_file(&db_path).ok();
        let db = Db::open_at(&db_path).unwrap();
        EngineState::new(db, std::env::temp_dir())
    }

    #[test]
    fn system_idle_seconds_returns_a_plausible_value_on_this_machine() {
        // Can't assert an exact value (depends on real HID activity at test
        // time), but on any macOS dev machine this should resolve to
        // *something* non-negative, proving the ioreg parsing itself works.
        let idle = system_idle_seconds();
        assert!(idle.is_some());
        assert!(idle.unwrap() >= 0.0);
    }

    #[test]
    fn force_active_bypasses_an_otherwise_unreachable_idle_threshold() {
        let state = scratch_state();
        // A threshold no real machine will ever be idle long enough to
        // clear on its own -- proves the override actually bypasses the
        // idle check itself, not just a low one that happened to pass.
        let unreachable_min_idle = f64::MAX;

        assert!(
            !background_work_allowed(&state, unreachable_min_idle),
            "without the override, an unreachable idle threshold must block work"
        );

        state.queue_control.force_active.store(true, Ordering::Relaxed);
        assert!(
            background_work_allowed(&state, unreachable_min_idle),
            "the \"process now\" override must bypass the idle check entirely"
        );
    }

    #[test]
    fn manual_pause_wins_over_the_force_active_override() {
        let state = scratch_state();
        state.queue_control.force_active.store(true, Ordering::Relaxed);
        state.queue_control.paused.store(true, Ordering::Relaxed);

        assert!(
            !background_work_allowed(&state, 0.0),
            "a manual pause must still block work even while a \"process now\" override is active"
        );
    }
}
