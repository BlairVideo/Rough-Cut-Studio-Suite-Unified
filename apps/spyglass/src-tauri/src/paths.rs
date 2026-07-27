//! Resolves where the Python sidecar and the bundled ffmpeg/ffprobe pair
//! live, branching once on debug-vs-release rather than scattering that
//! branch across every call site (Phase 7 packaging).
//!
//! Debug builds keep today's dev-tree-relative behavior completely
//! unchanged -- `cargo tauri dev`/`cargo test` never touch the release
//! branch below. Release builds resolve against the app bundle's own
//! `Resources` directory, where `packaging/build_sidecar_runtime.sh` and
//! `packaging/build_ffmpeg_bin.sh`'s output land via `tauri.conf.json`'s
//! `bundle.resources`.

use std::path::PathBuf;
use tauri::{path::BaseDirectory, AppHandle, Manager};

/// Dev build -> unchanged workspace-relative `sidecar/` dir. Release build
/// -> the bundled `sidecar-runtime` resource, laid out identically
/// (`.venv/` + `analyze_clip.py` + `embed_text_server.py` as siblings) so
/// `resolve_venv_python`/`SidecarCommand::real`/`EmbedServer::start` (all
/// already take a `sidecar_dir: &Path` and derive everything via
/// `.join(...)`) need no changes at all to work against either tree.
pub fn resolve_sidecar_dir(app: &AppHandle) -> PathBuf {
    if cfg!(debug_assertions) {
        spyglass_engine::gap_fill_worker::dev_sidecar_dir()
    } else {
        app.path()
            .resolve("sidecar-runtime", BaseDirectory::Resource)
            .expect("bundled sidecar-runtime resource must exist")
    }
}

/// Dev build -> `None` (today's bare `ffmpeg`/`ffprobe` PATH lookup,
/// unchanged). Release build -> the bundled `ffmpeg-bin` resource dir,
/// handed to `spyglass_core::ffmpeg_paths::set_ffmpeg_bin_dir`.
pub fn resolve_ffmpeg_bin_dir(app: &AppHandle) -> Option<PathBuf> {
    if cfg!(debug_assertions) {
        None
    } else {
        Some(
            app.path()
                .resolve("ffmpeg-bin", BaseDirectory::Resource)
                .expect("bundled ffmpeg-bin resource must exist"),
        )
    }
}
