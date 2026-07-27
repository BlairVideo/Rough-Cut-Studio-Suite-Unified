pub mod broll_analyzer;
pub mod card_eater;
pub mod transcriber;

use std::path::PathBuf;
use thiserror::Error;

/// The two known sidecar filename patterns from Section 3/7 of the plan --
/// the watched-root scanner excludes these from being treated as media
/// files itself, since they're adapter input, not footage.
pub const IVT_CACHE_SUFFIX: &str = ".ivt-cache.json";
pub const BROLL_CACHE_FILENAME: &str = ".broll_analyzer_cache.json";

#[derive(Debug, Error)]
pub enum AdapterError {
    #[error("io error reading {path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("invalid JSON in {path}: {source}")]
    Json {
        path: PathBuf,
        source: serde_json::Error,
    },
}
