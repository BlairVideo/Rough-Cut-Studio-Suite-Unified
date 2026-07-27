//! Spyglass's engine: background workers (gap-fill, rescan, volume-watch),
//! sidecar process management (the ML analyze worker, the CLIP text-embed
//! server), and the shared app state they all operate on -- everything
//! that used to live in `src-tauri` but has no real dependency on Tauri
//! itself, split out so a future host (a PyO3-linked process embedding
//! Spyglass into Rough Cut Studio Suite) can start the same engine the
//! Tauri shell does, rather than this logic needing a rewrite to be
//! reachable from anywhere but a Tauri command.

pub mod analyze_worker;
pub mod consolidate_export;
pub mod embed_server;
pub mod engine;
pub mod gap_fill_worker;
pub mod idle;
pub mod rescan_scheduler;
pub mod state;
pub mod volume_watcher;

pub use engine::{Engine, EngineConfig};
pub use state::EngineState;
