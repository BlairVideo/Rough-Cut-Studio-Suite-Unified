//! Owns the engine's Tokio runtime and starts the three background loops
//! (gap-fill worker, volume watcher, rescan scheduler) on it. This is the
//! one entry point both the Tauri shell and any future non-Tauri host (a
//! PyO3-linked process, per the Suite integration plan) start the engine
//! through, so neither host can drift into wiring the background loops up
//! differently from the other.

use crate::gap_fill_worker::{self, WorkerConfig};
use crate::rescan_scheduler;
use crate::state::EngineState;
use crate::volume_watcher;
use spyglass_core::Db;
use std::path::PathBuf;
use std::sync::Arc;

pub struct EngineConfig {
    pub sidecar_dir: PathBuf,
    pub keyframe_cache_root: PathBuf,
    /// Background work runs on up to this many sidecar subprocesses in
    /// parallel (Section 7: "configurable concurrency limit respecting
    /// CPU/GPU headroom"). Not yet exposed as a user setting -- a fixed,
    /// conservative default chosen by the host.
    pub max_concurrency: usize,
    /// How long the machine must have seen no keyboard/mouse activity
    /// before the queue starts new work (Section 7's idle-resume policy).
    pub min_idle_seconds: f64,
}

/// A running engine: its shared state (handed to command handlers) plus
/// the Tokio runtime the three background loops live on. Dropping this
/// drops the runtime, which stops polling for new work -- callers that
/// need the engine to keep running for the process's lifetime (both hosts
/// today) must hold onto it, not let it fall out of scope right after
/// `start`.
pub struct Engine {
    pub state: Arc<EngineState>,
    runtime: tokio::runtime::Runtime,
}

impl Engine {
    /// Builds the engine's own multi-thread Tokio runtime and starts the
    /// gap-fill worker, volume watcher, and rescan scheduler on it. `db`
    /// must already be open and have had `reset_stale_running_jobs`/
    /// `enqueue_pending_gap_fill_jobs` run against it if the host wants
    /// resumability across restarts -- that's host-lifecycle setup, not
    /// something the engine does on every `start`.
    pub fn start(db: Db, config: EngineConfig) -> Self {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("failed to build spyglass-engine's tokio runtime");

        let state = Arc::new(EngineState::new(db, config.sidecar_dir.clone()));
        let handle = runtime.handle().clone();

        let worker_config = WorkerConfig {
            sidecar_dir: config.sidecar_dir,
            keyframe_cache_root: config.keyframe_cache_root,
            max_concurrency: config.max_concurrency,
            min_idle_seconds: config.min_idle_seconds,
        };
        gap_fill_worker::spawn(state.clone(), worker_config, &handle);

        let watcher_state = state.clone();
        handle.spawn(async move {
            volume_watcher::run(watcher_state).await;
        });

        let rescan_state = state.clone();
        let min_idle_seconds = config.min_idle_seconds;
        handle.spawn(async move {
            rescan_scheduler::run(
                rescan_state,
                rescan_scheduler::DEFAULT_POLL_INTERVAL,
                rescan_scheduler::DEFAULT_RESCAN_INTERVAL,
                min_idle_seconds,
            )
            .await;
        });

        Engine { state, runtime }
    }

    /// A handle onto the engine's own runtime, for a host that needs to
    /// spawn additional work bound to the same runtime (e.g. the Tauri
    /// shell's async command handlers, or a future PyO3 host's blocking-
    /// call bridge).
    pub fn runtime_handle(&self) -> tokio::runtime::Handle {
        self.runtime.handle().clone()
    }
}
