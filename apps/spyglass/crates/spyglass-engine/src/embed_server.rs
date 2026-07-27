//! Manages the persistent `embed_text_server.py` child process (Section
//! 12): keeps CLIP loaded in memory across searches so query-time
//! embedding is milliseconds, not the several seconds a fresh process
//! would spend reloading the model on every search.

use spyglass_core::pipeline::resolve_venv_python;
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::Mutex;
use std::time::Duration;

/// Bound on how long a single query is allowed to wait for its embedding.
/// The model is supposed to already be resident in memory by this point
/// (Section 12: "query-time embedding is milliseconds"), so this is
/// generous headroom for CPU contention from concurrent gap-fill sidecar
/// processes, not an expected steady-state latency. Without a bound here,
/// a wedged or crashed-without-closing-stdout child would hang the caller
/// forever -- unlike the gap-fill sidecar (`pipeline::DEFAULT_SIDECAR_
/// TIMEOUT`), which was always time-bounded.
const EMBED_TIMEOUT: Duration = Duration::from_secs(60);

/// Bound on the one-time model-load wait in `start`. Loading CLIP from a
/// cold cache under heavy concurrent gap-fill load can genuinely take
/// longer than the "several seconds" steady-state case, so this is set
/// well above that, not tuned to it.
const READY_TIMEOUT: Duration = Duration::from_secs(5 * 60);

pub struct EmbedServer {
    child: Mutex<Child>,
    stdin: Mutex<ChildStdin>,
    /// Fed by a dedicated reader thread (spawned in `start`) rather than
    /// read directly in `embed` -- that's what lets `embed` wait on a
    /// `recv_timeout` instead of an unboundable `BufRead::read_line`.
    responses: Mutex<Receiver<String>>,
}

impl EmbedServer {
    /// Spawns the server and blocks until it reports readiness (the model
    /// finished loading) or `READY_TIMEOUT` elapses. Callers should do this
    /// once, lazily, off the critical path of app launch -- not at every
    /// search.
    pub fn start(sidecar_dir: &Path) -> Result<Self, String> {
        Self::start_with_timeout(sidecar_dir, READY_TIMEOUT)
    }

    /// Split out from `start` so tests can pass a short timeout instead of
    /// waiting out the real `READY_TIMEOUT` to prove it fires.
    fn start_with_timeout(sidecar_dir: &Path, ready_timeout: Duration) -> Result<Self, String> {
        let program = resolve_venv_python(sidecar_dir);
        let script = sidecar_dir.join("embed_text_server.py");

        let mut child = Command::new(program)
            .arg(script)
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
            // First line is the readiness marker; everything after just
            // gets drained so a later warning/error line never fills the
            // pipe buffer and blocks the child (same concern as the
            // pipe-draining in pipeline.rs).
            if reader.read_line(&mut buf).unwrap_or(0) > 0 {
                let _ = ready_tx.send(());
            }
            buf.clear();
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
                    "embed server did not report readiness within {}s -- killed it",
                    ready_timeout.as_secs()
                ));
            }
        }

        // Responses are read on their own thread and handed over a
        // channel so `embed` can wait on `recv_timeout` instead of a
        // plain blocking `read_line` that has no way to time out.
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

        Ok(EmbedServer {
            child: Mutex::new(child),
            stdin: Mutex::new(stdin),
            responses: Mutex::new(rx),
        })
    }

    /// Sends one query and waits for its embedding, up to `EMBED_TIMEOUT`.
    /// Requests are answered strictly one at a time (a single-user desktop
    /// search UI never needs more than that); the stdin/responses mutexes
    /// serialize concurrent callers rather than interleaving requests.
    pub fn embed(&self, text: &str) -> Result<Vec<f32>, String> {
        self.embed_with_timeout(text, EMBED_TIMEOUT)
    }

    /// Split out from `embed` so tests can pass a short timeout instead of
    /// waiting out the real `EMBED_TIMEOUT` to prove it fires.
    fn embed_with_timeout(&self, text: &str, timeout: Duration) -> Result<Vec<f32>, String> {
        let request = serde_json::json!({ "text": text }).to_string();

        {
            let mut stdin = self.stdin.lock().map_err(|e| e.to_string())?;
            writeln!(stdin, "{request}").map_err(|e| e.to_string())?;
            stdin.flush().map_err(|e| e.to_string())?;
        }

        let line = {
            let responses = self.responses.lock().map_err(|e| e.to_string())?;
            responses.recv_timeout(timeout).map_err(|err| match err {
                RecvTimeoutError::Timeout => {
                    format!("embed server did not answer within {}s", timeout.as_secs())
                }
                RecvTimeoutError::Disconnected => "embed server closed its output stream unexpectedly".to_string(),
            })?
        };

        let response: serde_json::Value = serde_json::from_str(&line).map_err(|e| e.to_string())?;
        if let Some(err) = response.get("error").and_then(|v| v.as_str()) {
            return Err(err.to_string());
        }
        let embedding = response
            .get("embedding")
            .and_then(|v| v.as_array())
            .ok_or_else(|| "embed server response missing 'embedding'".to_string())?
            .iter()
            .map(|v| v.as_f64().unwrap_or(0.0) as f32)
            .collect();
        Ok(embedding)
    }
}

/// Embeds `text` using `*slot`, starting the server first if `*slot` is
/// `None`, and retrying once against a freshly restarted server if the
/// first attempt fails. Without this, a persistent server that dies
/// mid-session (crash, OOM) left `*slot` holding a defunct child forever --
/// every search after that point would fail with "closed its output stream
/// unexpectedly" until the whole app was restarted, since nothing ever
/// re-checked whether the server was still alive.
pub fn embed_with_restart(slot: &mut Option<EmbedServer>, sidecar_dir: &Path, text: &str) -> Result<Vec<f32>, String> {
    if slot.is_none() {
        *slot = Some(EmbedServer::start(sidecar_dir)?);
    }

    match slot.as_ref().unwrap().embed(text) {
        Ok(embedding) => Ok(embedding),
        Err(first_err) => {
            *slot = None; // the old server is presumed dead -- drop it before replacing it
            let server = EmbedServer::start(sidecar_dir)
                .map_err(|restart_err| format!("embed failed ({first_err}); restart also failed: {restart_err}"))?;
            let embedding = server
                .embed(text)
                .map_err(|retry_err| format!("embed failed after restart: {retry_err}"))?;
            *slot = Some(server);
            Ok(embedding)
        }
    }
}

impl Drop for EmbedServer {
    fn drop(&mut self) {
        // `Child` doesn't kill its process on drop -- without this, the
        // model-loaded Python process would linger after Spyglass quits.
        if let Ok(mut child) = self.child.lock() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::Instant;

    fn sidecar_dir() -> std::path::PathBuf {
        // Two levels up from this crate's own manifest dir
        // (`crates/spyglass-engine/Cargo.toml`), not one, now that this file
        // lives in `crates/spyglass-engine` rather than `src-tauri` itself.
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().join("sidecar")
    }

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    /// A scratch dir standing in for `sidecar_dir` -- no `.venv` inside it,
    /// so `resolve_venv_python` falls back to bare `python3` (fine for
    /// these stdlib-only fixture scripts, same convention `pipeline.rs`'s
    /// tests use).
    fn scratch_dir(tag: &str) -> std::path::PathBuf {
        let n = TMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("spyglass_embed_server_test_{tag}_{n}"));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn start_kills_and_reports_a_server_that_never_signals_ready() {
        let dir = scratch_dir("never_ready");
        // Never writes anything to stderr, so the "ready" wait can only
        // resolve via the timeout, not a real signal.
        std::fs::write(dir.join("embed_text_server.py"), "import time\ntime.sleep(30)\n").unwrap();

        let started = Instant::now();
        let result = EmbedServer::start_with_timeout(&dir, Duration::from_secs(1));
        assert!(started.elapsed() < Duration::from_secs(10), "must not wait out the full 30s sleep");
        assert!(result.is_err(), "expected a timeout error, got Ok(EmbedServer)");

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn embed_times_out_promptly_when_the_server_never_answers() {
        let dir = scratch_dir("never_answers");
        // Signals ready immediately, then hangs on every request without
        // ever writing a response line.
        std::fs::write(
            dir.join("embed_text_server.py"),
            "import sys, time\nprint('ready', file=sys.stderr, flush=True)\nfor line in sys.stdin:\n    time.sleep(30)\n",
        )
        .unwrap();

        let server = EmbedServer::start_with_timeout(&dir, Duration::from_secs(10)).expect("server should report ready");

        let started = Instant::now();
        let result = server.embed_with_timeout("mascot", Duration::from_secs(1));
        assert!(started.elapsed() < Duration::from_secs(10), "must not wait out the full 30s sleep");
        assert!(result.is_err(), "expected a timeout error, got {result:?}");
        assert!(result.unwrap_err().contains("did not answer"));

        std::fs::remove_dir_all(&dir).ok();
    }

    /// A stdlib-only fixture answering every request with a fixed vector --
    /// enough to exercise `embed_with_restart`'s recovery path without
    /// needing torch/CLIP.
    fn write_echo_fixture(dir: &std::path::Path) {
        std::fs::write(
            dir.join("embed_text_server.py"),
            "import json, sys\nprint('ready', file=sys.stderr, flush=True)\nfor line in sys.stdin:\n    print(json.dumps({'embedding': [1.0, 2.0, 3.0]}), flush=True)\n",
        )
        .unwrap();
    }

    #[test]
    fn embed_with_restart_starts_a_server_into_an_empty_slot() {
        let dir = scratch_dir("restart_cold_start");
        write_echo_fixture(&dir);

        let mut slot: Option<EmbedServer> = None;
        let embedding = embed_with_restart(&mut slot, &dir, "mascot").unwrap();
        assert_eq!(embedding, vec![1.0, 2.0, 3.0]);
        assert!(slot.is_some());

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn embed_with_restart_recovers_when_the_resident_server_has_died() {
        let dir = scratch_dir("restart_recovers");
        write_echo_fixture(&dir);

        let server = EmbedServer::start(&dir).expect("server should start");
        // Simulate the server crashing (OOM, force-quit) independently of
        // any request -- without `embed_with_restart`, every future call
        // against this same slot would keep failing with "closed its
        // output stream unexpectedly" forever.
        server.child.lock().unwrap().kill().unwrap();
        server.child.lock().unwrap().wait().ok();

        let mut slot = Some(server);
        let embedding = embed_with_restart(&mut slot, &dir, "mascot").expect("must recover via a fresh server");
        assert_eq!(embedding, vec![1.0, 2.0, 3.0]);
        assert!(slot.is_some(), "a healthy replacement server must be left in the slot");

        // The replacement must actually be usable for a second call, not
        // just capable of answering the one retry inside `embed_with_restart`.
        let second = slot.as_ref().unwrap().embed("another query").expect("replacement server must stay usable");
        assert_eq!(second, vec![1.0, 2.0, 3.0]);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn embed_with_restart_reports_both_errors_when_the_restart_itself_fails() {
        let dir = scratch_dir("restart_fails");
        write_echo_fixture(&dir);

        let server = EmbedServer::start(&dir).expect("server should start");
        server.child.lock().unwrap().kill().unwrap();
        server.child.lock().unwrap().wait().ok();
        let mut slot = Some(server);

        // Break the fixture so the restart attempt also fails, instead of
        // just the original dead server -- proves the error message covers
        // both failures rather than only ever reporting the first. Exits
        // immediately rather than hanging, so the ready-wait fails via a
        // near-instant stderr EOF/disconnect instead of waiting out
        // `EmbedServer::start`'s full (non-test-shortened) `READY_TIMEOUT`.
        std::fs::write(dir.join("embed_text_server.py"), "import sys\nsys.exit(1)\n").unwrap();

        let err = embed_with_restart(&mut slot, &dir, "mascot").unwrap_err();
        assert!(err.contains("embed failed"), "should mention the original failure: {err}");
        assert!(err.contains("restart also failed"), "should mention the restart failure: {err}");

        std::fs::remove_dir_all(&dir).ok();
    }

    /// Exercises the real subprocess lifecycle -- spawn, ready-wait, two
    /// sequential queries, and cleanup on drop -- against the actual
    /// `embed_text_server.py` and its CLIP checkpoint. Ignored by default
    /// (slow, needs the sidecar venv); run explicitly with
    /// `cargo test -- --ignored embed_server`.
    #[test]
    #[ignore]
    fn embed_server_answers_sequential_queries_with_normalized_vectors() {
        let server = EmbedServer::start(&sidecar_dir()).expect("embed server must start");

        let a = server.embed("mascot cheering at a football game").unwrap();
        assert_eq!(a.len(), 512);
        let norm: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
        assert!((norm - 1.0).abs() < 0.01, "CLIP embeddings should be L2-normalized, got norm {norm}");

        let b = server.embed("students studying in the library").unwrap();
        assert_eq!(b.len(), 512);
        assert_ne!(a, b, "different queries should not produce identical embeddings");

        drop(server);
    }
}
