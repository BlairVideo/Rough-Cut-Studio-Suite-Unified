//! Where to find `ffmpeg`/`ffprobe` at runtime. Dev builds and `cargo test`
//! rely on Homebrew's copies being on `PATH` (unchanged, existing
//! behavior); a packaged `.app` bundles its own de-Homebrewed copies
//! (Phase 7) and must point here at exactly that pair instead, resolved
//! once at startup by `src-tauri` (the only caller allowed to know about
//! Tauri's resource-bundling paths -- this crate stays dependency-free of
//! `tauri`, per the plan's "embeddable outside Tauri" decision).

use std::path::PathBuf;
use std::sync::OnceLock;

static FFMPEG_BIN_DIR: OnceLock<PathBuf> = OnceLock::new();

/// Call once, early, from the packaged app's startup. A second call is a
/// harmless no-op (`OnceLock::set`'s own semantics) rather than a panic --
/// there's only one process-wide value to set.
pub fn set_ffmpeg_bin_dir(dir: PathBuf) {
    let _ = FFMPEG_BIN_DIR.set(dir);
}

fn resolve(bin_name: &str) -> PathBuf {
    match FFMPEG_BIN_DIR.get() {
        Some(dir) => dir.join(bin_name),
        None => PathBuf::from(bin_name), // unset -> bare name, PATH lookup
    }
}

pub fn ffmpeg_path() -> PathBuf {
    resolve("ffmpeg")
}

pub fn ffprobe_path() -> PathBuf {
    resolve("ffprobe")
}
