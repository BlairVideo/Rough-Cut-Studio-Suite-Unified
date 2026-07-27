//! Manages a persistent `analyze_clip.py --serve` child process (mirrors
//! `embed_server.rs`'s Section 12 pattern, applied to gap-fill instead of
//! search): keeps CLIP + moondream2 loaded in memory across many clips
//! instead of the old one-shot-process-per-clip approach, which reloaded
//! both models from disk before analyzing even a single clip. For an
//! archive with thousands of clips queued for gap-fill, that reload cost
//! -- not the analysis itself -- was the dominant cost in queue throughput.
//!
//! One `AnalyzeWorker` maps to one gap-fill worker slot (`gap_fill_worker`'s
//! `MAX_CONCURRENCY` loop tasks each own one), so the number of resident
//! Python processes stays exactly what it was before: one per concurrent
//! slot, just reused across every clip that slot claims instead of
//! discarded after each one.

use spyglass_core::pipeline::{resolve_venv_python, CancelToken, PipelineError, SidecarOutput};
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::Ordering;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

/// Bound on the one-time model-load wait in `start`. Loading CLIP +
/// moondream2 from a cold weights cache can take a while, especially under
/// concurrent load from other slots doing the same thing at app launch --
/// mirrors `embed_server::READY_TIMEOUT`'s reasoning exactly.
const READY_TIMEOUT: Duration = Duration::from_secs(5 * 60);

/// How often `analyze` re-checks the cancel token and elapsed time while
/// waiting for a response, instead of blocking for the full remaining
/// timeout in one call -- the same cadence `pipeline::run_sidecar`'s poll
/// loop used, so a volume-disconnect cancel still lands promptly.
const POLL_INTERVAL: Duration = Duration::from_millis(200);

pub struct AnalyzeWorker {
    child: Child,
    stdin: ChildStdin,
    responses: Receiver<String>,
}

impl AnalyzeWorker {
    /// Spawns `analyze_clip.py --serve` and blocks until it reports
    /// readiness (both models finished loading) or `READY_TIMEOUT` elapses.
    pub fn start(sidecar_dir: &Path) -> Result<Self, String> {
        Self::start_with_timeout(sidecar_dir, READY_TIMEOUT)
    }

    /// Split out from `start` so tests can pass a short timeout instead of
    /// waiting out the real `READY_TIMEOUT` to prove it fires.
    fn start_with_timeout(sidecar_dir: &Path, ready_timeout: Duration) -> Result<Self, String> {
        let program = resolve_venv_python(sidecar_dir);
        let script = sidecar_dir.join("analyze_clip.py");

        let mut child = Command::new(program)
            .arg(script)
            .arg("--serve")
            .current_dir(sidecar_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| e.to_string())?;

        let stdin = child.stdin.take().expect("stdin was piped");
        let stdout = child.stdout.take().expect("stdout was piped");
        let stderr = child.stderr.take().expect("stderr was piped");

        // The "ready" marker arrives as stderr's first line -- wait for it
        // on a background thread so this can be time-bounded via
        // `recv_timeout` rather than a plain blocking `read_line`.
        let (ready_tx, ready_rx) = mpsc::channel::<()>();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stderr);
            let mut buf = String::new();
            if reader.read_line(&mut buf).unwrap_or(0) > 0 {
                let _ = ready_tx.send(());
            }
            buf.clear();
            // Drain everything after the ready line so a later warning
            // from the model libraries never fills the pipe buffer and
            // blocks the child (same concern as pipeline.rs's stderr
            // draining for the one-shot sidecar).
            while reader.read_line(&mut buf).unwrap_or(0) > 0 {
                buf.clear();
            }
        });

        match ready_rx.recv_timeout(ready_timeout) {
            Ok(()) => {}
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "analyze worker did not report readiness within {}s -- killed it",
                    ready_timeout.as_secs()
                ));
            }
        }

        let (tx, rx) = mpsc::channel::<String>();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = String::new();
            loop {
                line.clear();
                match reader.read_line(&mut line) {
                    Ok(0) | Err(_) => break, // child exited or pipe broke
                    Ok(_) => {
                        if tx.send(line.clone()).is_err() {
                            break; // no one's listening anymore
                        }
                    }
                }
            }
        });

        Ok(AnalyzeWorker { child, stdin, responses: rx })
    }

    /// Analyzes one clip, waiting up to `timeout` and checking `cancel`
    /// every `POLL_INTERVAL`. On timeout, cancellation, or any protocol/
    /// process error, the underlying process is killed and this worker is
    /// no longer usable -- callers must drop it and `start` a fresh one for
    /// the next clip. That mirrors the old kill-on-timeout/cancel semantics
    /// from `pipeline::run_sidecar` exactly; the only change is that a
    /// clean, successful analysis no longer pays a respawn+reload cost
    /// afterward the way every single clip used to.
    pub fn analyze(
        &mut self,
        video_path: &Path,
        keyframe_dir: &Path,
        timeout: Duration,
        cancel: Option<&CancelToken>,
    ) -> Result<SidecarOutput, PipelineError> {
        let request = serde_json::json!({
            "video_path": video_path.to_string_lossy(),
            "keyframe_dir": keyframe_dir.to_string_lossy(),
        })
        .to_string();

        if let Err(e) = writeln!(self.stdin, "{request}").and_then(|_| self.stdin.flush()) {
            self.kill();
            return Err(PipelineError::SidecarFailed {
                status: -1,
                stderr: format!("failed to send request to analyze worker: {e}"),
            });
        }

        let start = Instant::now();
        let line = loop {
            if cancel.is_some_and(|c| c.load(Ordering::Relaxed)) {
                self.kill();
                return Err(PipelineError::Cancelled);
            }
            if start.elapsed() > timeout {
                self.kill();
                return Err(PipelineError::Timeout);
            }
            match self.responses.recv_timeout(POLL_INTERVAL) {
                Ok(line) => break line,
                Err(RecvTimeoutError::Timeout) => continue,
                Err(RecvTimeoutError::Disconnected) => {
                    self.kill();
                    return Err(PipelineError::SidecarFailed {
                        status: -1,
                        stderr: "analyze worker closed its output stream unexpectedly".to_string(),
                    });
                }
            }
        };

        let response: serde_json::Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(e) => {
                self.kill();
                return Err(PipelineError::InvalidOutput(e));
            }
        };
        if let Some(err) = response.get("error").and_then(|v| v.as_str()) {
            // A per-clip failure inside the sidecar (corrupt file, bad
            // codec) -- the worker itself is still healthy and answered
            // the protocol correctly, so it stays alive for the next clip.
            return Err(PipelineError::SidecarFailed { status: 1, stderr: err.to_string() });
        }
        serde_json::from_value(response).map_err(PipelineError::InvalidOutput)
    }

    fn kill(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for AnalyzeWorker {
    fn drop(&mut self) {
        // `Child` doesn't kill its process on drop -- without this, a
        // model-loaded Python process would linger after Spyglass quits.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// The sidecar directory during development: a sibling of `src-tauri` at
/// the workspace root -- two levels up from this crate's own manifest dir
/// (`crates/spyglass-engine/Cargo.toml`), not one, now that this file lives
/// in `crates/spyglass-engine` rather than `src-tauri` itself. Shared by
/// tests here and by `gap_fill_worker`'s own `dev_sidecar_dir` -- kept as a
/// private duplicate rather than a shared export since it's test/dev-only
/// plumbing.
#[cfg(test)]
fn sidecar_dir() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().join("sidecar")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering as AtomicOrdering;
    use std::sync::atomic::{AtomicU64, Ordering as AU64Ordering};
    use std::sync::Arc;

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    /// A scratch dir standing in for `sidecar_dir` -- no `.venv` inside it,
    /// so `resolve_venv_python` falls back to bare `python3` (fine for
    /// these stdlib-only fixture scripts).
    fn scratch_dir(tag: &str) -> PathBuf {
        let n = TMP_COUNTER.fetch_add(1, AU64Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("spyglass_analyze_worker_test_{tag}_{n}"));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn start_kills_and_reports_a_worker_that_never_signals_ready() {
        let dir = scratch_dir("never_ready");
        std::fs::write(dir.join("analyze_clip.py"), "import time\ntime.sleep(30)\n").unwrap();

        let started = Instant::now();
        let result = AnalyzeWorker::start_with_timeout(&dir, Duration::from_secs(1));
        assert!(started.elapsed() < Duration::from_secs(10), "must not wait out the full 30s sleep");
        assert!(result.is_err(), "expected a startup error, got Ok(AnalyzeWorker)");

        std::fs::remove_dir_all(&dir).ok();
    }

    /// Verifies the request/response protocol and per-clip error handling
    /// against a stdlib-only fake `--serve` script -- no torch/CLIP needed.
    #[test]
    fn analyze_round_trips_a_request_and_survives_a_per_clip_error() {
        let dir = scratch_dir("fake_serve");
        std::fs::write(
            dir.join("analyze_clip.py"),
            r#"
import json, sys
print("ready", file=sys.stderr, flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if "bad" in req["video_path"]:
        print(json.dumps({"error": "boom"}), flush=True)
    else:
        print(json.dumps({"duration_sec": 12.0, "frame_rate": 30.0, "shots": []}), flush=True)
"#,
        )
        .unwrap();

        let mut worker = AnalyzeWorker::start(&dir).expect("worker should report ready");

        let good = worker.analyze(Path::new("/tmp/good.mov"), Path::new("/tmp/kf"), Duration::from_secs(5), None);
        let output = good.expect("first request should succeed");
        assert_eq!(output.duration_sec, 12.0);

        // A per-clip error must not kill the worker -- it should still be
        // able to answer a subsequent request.
        let bad = worker.analyze(Path::new("/tmp/bad.mov"), Path::new("/tmp/kf"), Duration::from_secs(5), None);
        assert!(bad.is_err());

        let good_again = worker.analyze(Path::new("/tmp/good2.mov"), Path::new("/tmp/kf"), Duration::from_secs(5), None);
        assert!(good_again.is_ok(), "worker must survive a prior per-clip error");

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn analyze_times_out_promptly_when_the_worker_never_answers() {
        let dir = scratch_dir("never_answers");
        std::fs::write(
            dir.join("analyze_clip.py"),
            "import sys, time\nprint('ready', file=sys.stderr, flush=True)\nfor line in sys.stdin:\n    time.sleep(30)\n",
        )
        .unwrap();

        let mut worker = AnalyzeWorker::start(&dir).expect("worker should report ready");

        let started = Instant::now();
        let result = worker.analyze(Path::new("/tmp/x.mov"), Path::new("/tmp/kf"), Duration::from_secs(1), None);
        assert!(started.elapsed() < Duration::from_secs(10), "must not wait out the full 30s sleep");
        assert!(matches!(result, Err(PipelineError::Timeout)));

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn analyze_cancels_promptly_when_the_token_is_set_mid_wait() {
        let dir = scratch_dir("cancel");
        std::fs::write(
            dir.join("analyze_clip.py"),
            "import sys, time\nprint('ready', file=sys.stderr, flush=True)\nfor line in sys.stdin:\n    time.sleep(30)\n",
        )
        .unwrap();

        let mut worker = AnalyzeWorker::start(&dir).expect("worker should report ready");
        let cancel: CancelToken = Arc::new(AtomicBool::new(false));
        let cancel_setter = cancel.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            cancel_setter.store(true, AtomicOrdering::Relaxed);
        });

        let started = Instant::now();
        let result = worker.analyze(Path::new("/tmp/x.mov"), Path::new("/tmp/kf"), Duration::from_secs(30), Some(&cancel));
        assert!(started.elapsed() < Duration::from_secs(5), "must not wait out the full 30s timeout");
        assert!(matches!(result, Err(PipelineError::Cancelled)));

        std::fs::remove_dir_all(&dir).ok();
    }

    /// Exercises the real subprocess lifecycle against the actual
    /// `analyze_clip.py --serve` and its ML checkpoints. Ignored by
    /// default (slow, needs the sidecar venv + a real video file); run
    /// explicitly with `cargo test -- --ignored analyze_worker`.
    #[test]
    #[ignore]
    fn real_worker_starts_against_the_actual_sidecar() {
        let result = AnalyzeWorker::start(&sidecar_dir());
        assert!(result.is_ok(), "real analyze worker must start: {:?}", result.err());
    }
}
