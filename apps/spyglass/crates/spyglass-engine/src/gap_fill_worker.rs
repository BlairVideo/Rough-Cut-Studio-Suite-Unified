//! Background job queue worker (Section 7): pulls pending `gap_fill_jobs`
//! rows and runs `spyglass_core::pipeline::run_gap_fill_for_clip` against
//! them, with a configurable concurrency limit and an idle/pause gate so it
//! never competes with an active edit for CPU/disk.
//!
//! The actual indexing logic (shelling out to the Python sidecar, writing
//! shots/embeddings) lives in `spyglass_core::pipeline` -- this module is
//! just the scheduling loop, mirroring how Card Eater keeps its own
//! app-lifecycle-bound async loops (`volume_watcher.rs`) in the Tauri shell
//! while the pure logic lives elsewhere.

use crate::analyze_worker::AnalyzeWorker;
use crate::state::EngineState;
use spyglass_core::adapters::broll_analyzer::{self, BrollClipEntry};
use spyglass_core::models::{Clip, GapFillJob};
use spyglass_core::pipeline::{CancelToken, PipelineError};
use spyglass_core::{db, pipeline};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

const POLL_INTERVAL: Duration = Duration::from_secs(2);

/// Cancel tokens for whatever clips are currently mid-analysis, keyed by
/// clip id. The volume watcher (Section 9) uses this to kill a specific
/// in-flight sidecar the moment its source drive disappears, rather than
/// waiting out the timeout for what would otherwise be a hung file handle.
#[derive(Default)]
pub struct InFlightRegistry {
    tokens: Mutex<HashMap<i64, CancelToken>>,
}

impl InFlightRegistry {
    fn register(&self, clip_id: i64) -> CancelToken {
        let token: CancelToken = Arc::new(AtomicBool::new(false));
        self.tokens.lock().unwrap().insert(clip_id, token.clone());
        token
    }

    fn unregister(&self, clip_id: i64) {
        self.tokens.lock().unwrap().remove(&clip_id);
    }

    /// Sets the cancel flag for every currently-tracked clip whose path
    /// falls under `root_path`. Returns how many were signalled.
    pub fn cancel_under_root(&self, root_path: &str, clip_paths: impl Fn(i64) -> Option<String>) -> usize {
        let like_prefix = root_path.trim_end_matches('/');
        let tokens = self.tokens.lock().unwrap();
        let mut signalled = 0;
        for (&clip_id, token) in tokens.iter() {
            if let Some(path) = clip_paths(clip_id) {
                if path.starts_with(like_prefix) {
                    token.store(true, Ordering::Relaxed);
                    signalled += 1;
                }
            }
        }
        signalled
    }
}

#[derive(Clone)]
pub struct WorkerConfig {
    pub sidecar_dir: PathBuf,
    pub keyframe_cache_root: PathBuf,
    pub max_concurrency: usize,
    /// Minimum seconds of HID (keyboard/mouse) inactivity before the
    /// worker will start new jobs -- Section 7's "resume at idle" policy.
    pub min_idle_seconds: f64,
}

/// The sidecar directory during development: a sibling of `src-tauri` at
/// the workspace root. Two levels up from this crate's own manifest dir
/// (`crates/spyglass-engine/Cargo.toml`), not one -- this used to live in
/// `src-tauri` itself (one level up from the workspace root), so moving it
/// into `crates/spyglass-engine` changed how many `.parent()` calls this
/// needs. Not yet resolved for a packaged/bundled build -- when this app is
/// actually distributed as a standalone .app, the sidecar needs to ship as
/// a bundled resource instead of being found next to the source tree.
/// Flagged here rather than silently assumed solved.
pub fn dev_sidecar_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates/spyglass-engine always has a parent directory")
        .parent()
        .expect("the workspace root is always the grandparent of crates/spyglass-engine")
        .join("sidecar")
}

/// Spawns `config.max_concurrency` worker loop tasks onto `handle`. Takes an
/// explicit runtime handle (rather than assuming an ambient Tokio context,
/// the way `tauri::async_runtime::spawn` could) since this is called from
/// `Engine::start`, which runs as plain synchronous setup code before the
/// engine's own runtime has necessarily been entered.
pub fn spawn(state: Arc<EngineState>, config: WorkerConfig, handle: &tokio::runtime::Handle) {
    let config = Arc::new(config);
    for _ in 0..config.max_concurrency.max(1) {
        let state = state.clone();
        let config = config.clone();
        handle.spawn(async move {
            worker_loop(state, config).await;
        });
    }
}

async fn worker_loop(state: Arc<EngineState>, config: Arc<WorkerConfig>) {
    // One persistent `analyze_clip.py --serve` process for this slot's
    // entire lifetime, reused across every clip it claims -- instead of
    // the old one-shot-process-per-clip approach, which paid CLIP +
    // moondream2's full disk-load cost before analyzing even a single
    // clip. `None` means "not started yet" (first job claimed) or "just
    // died" (the previous clip's analysis killed it on timeout/cancel/
    // error) -- either way, the next iteration respawns it, paying the
    // reload cost again only in that comparatively rare case.
    let mut worker: Option<AnalyzeWorker> = None;

    loop {
        if !crate::idle::background_work_allowed(&state, config.min_idle_seconds) {
            tokio::time::sleep(POLL_INTERVAL).await;
            continue;
        }

        let claimed = claim_next(&state);
        let Some((job, clip, broll_entry)) = claimed else {
            // Nothing left to claim -- if a "process now" override was
            // driving the queue through the idle gate, its job is done;
            // clear it so the worker goes back to waiting for real idle
            // instead of continuing to bypass it (system-wide, since
            // `claim_next_pending_job` isn't scoped to one root) forever.
            state.queue_control.force_active.store(false, Ordering::Relaxed);
            tokio::time::sleep(POLL_INTERVAL).await;
            continue;
        };

        let in_flight = state.in_flight.clone();
        let cancel = in_flight.register(clip.id);

        let sidecar_dir = config.sidecar_dir.clone();
        let keyframe_root = config.keyframe_cache_root.clone();
        let job_id = job.id;
        let cancel_for_blocking = cancel.clone();
        let clip_for_blocking = clip.clone();
        let mut worker_for_blocking = worker.take();

        // Deliberately does NOT hold `state.db.conn`'s lock here -- the
        // sidecar this waits on can run for minutes (up to `DEFAULT_
        // SIDECAR_TIMEOUT`), and that lock is the same one every other
        // command (search, pool, and "Scan now") needs. Holding it for
        // the subprocess's whole lifetime serialized the entire app
        // behind whichever clip a worker happened to be analyzing.
        //
        // Returns the worker back alongside the result so the loop can
        // reuse it on the next iteration -- `None` only when this clip's
        // analysis killed it (timeout/cancel/protocol error) or it failed
        // to start in the first place.
        let blocking_result = tokio::task::spawn_blocking(move || {
            let mut w = match worker_for_blocking.take() {
                Some(w) => w,
                None => match AnalyzeWorker::start(&sidecar_dir) {
                    Ok(w) => w,
                    Err(e) => {
                        let io_err = std::io::Error::other(e);
                        return (None, Err(PipelineError::Spawn(io_err)));
                    }
                },
            };

            let keyframe_dir = keyframe_root.join(clip_for_blocking.id.to_string());
            let result = w.analyze(
                Path::new(&clip_for_blocking.file_path),
                &keyframe_dir,
                pipeline::DEFAULT_SIDECAR_TIMEOUT,
                Some(&cancel_for_blocking),
            );
            match result {
                Ok(output) => (Some(w), Ok((keyframe_dir, output))),
                Err(err) => (None, Err(err)), // `w` already killed itself internally on error
            }
        })
        .await;

        in_flight.unregister(job.clip_id);

        let sidecar_outcome = match blocking_result {
            Ok((worker_back, result)) => {
                worker = worker_back;
                result
            }
            Err(join_err) => {
                // The blocking closure (and the worker moved into it)
                // panicked -- both are gone; next iteration starts fresh.
                worker = None;
                Err(PipelineError::Spawn(std::io::Error::other(format!("worker task panicked: {join_err}"))))
            }
        };

        match sidecar_outcome {
            Ok((keyframe_dir, output)) => {
                let conn = state.db.conn.lock().unwrap();
                match pipeline::write_gap_fill_result(&conn, &clip, &keyframe_dir, &output, broll_entry.as_ref()) {
                    Ok(_shot_count) => {
                        let _ = db::mark_job_done(&conn, job_id);
                    }
                    Err(err) => {
                        let _ = db::mark_job_failed(&conn, job_id, &err.to_string());
                    }
                }
            }
            Err(PipelineError::Cancelled) => {
                // The volume watcher already flipped this job to
                // `awaiting_reconnect` (that's what set the cancel flag in
                // the first place) -- nothing further to record here.
            }
            Err(err) => {
                let conn = state.db.conn.lock().unwrap();
                let _ = db::mark_job_failed(&conn, job_id, &err.to_string());
            }
        }
    }
}

/// Claims one pending job and resolves its clip + any overlapping B-Roll
/// Analyzer cache entry, all under one connection lock (no `.await` inside
/// this scope -- the lock must not cross an await point).
fn claim_next(state: &EngineState) -> Option<(GapFillJob, Clip, Option<BrollClipEntry>)> {
    let conn = state.db.conn.lock().unwrap();

    let job = db::claim_next_pending_job(&conn).ok().flatten()?;
    match db::find_clip_by_id(&conn, job.clip_id).ok().flatten() {
        Some(clip) => {
            let broll_entry = broll_analyzer::find_entry_for_clip_path(Path::new(&clip.file_path));
            Some((job, clip, broll_entry))
        }
        None => {
            // The clip row is gone (shouldn't happen outside a race with
            // `remove_watched_root`) -- fail the job rather than spin on it.
            let _ = db::mark_job_failed(&conn, job.id, "clip row no longer exists");
            None
        }
    }
}
