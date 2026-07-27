//! Spyglass core: index schema, read-only source adapters, watched-root
//! scanner, and (later phases) the search/query API -- kept as its own
//! crate, separate from the Tauri shell binary, per Section 19.2 of the
//! architecture plan so a future embed into Studio Suite is a wiring
//! change rather than a rewrite.

pub mod adapters;
pub mod consolidate;
pub mod db;
pub mod facets;
pub mod ffmpeg_paths;
pub mod ffprobe;
pub mod folders;
pub mod maintenance;
pub mod models;
pub mod pipeline;
pub mod pool;
pub mod scanner;
pub mod search;
pub mod xmeml;

pub use db::Db;
