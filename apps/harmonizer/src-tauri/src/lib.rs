use std::path::PathBuf;
use std::process::Command;

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Dev-tree-relative resolution only (mirrors `apps/spyglass/src-tauri/src/paths.rs`'s
/// `debug_assertions` branch) -- a packaged build needs `bundle.resources` +
/// `AppHandle` threading here, which is deferred, so this is not yet safe to
/// call from a release binary.
fn backend_dir() -> Result<PathBuf, String> {
    // CARGO_MANIFEST_DIR is apps/harmonizer/src-tauri at build time; the
    // engine lives at apps/harmonizer/backend (renamed from prototype/
    // during the Phase 2 migration -- see migration-plan.md §6.4).
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../backend")
        .canonicalize()
        .map_err(|e| format!("apps/harmonizer/backend/ not found next to src-tauri/: {e}"))
}

/// Prefers the shared root `.venv` (see `apps/suite-wrapper/backend/paths.py`'s
/// `SHARED_VENV_PYTHON`) over a bare `python3`, which almost certainly lacks
/// numpy/scipy/librosa/soundfile. Falls back to `python3` rather than
/// erroring so a missing venv still yields a readable "failed to launch"
/// error instead of a panic.
fn venv_python() -> PathBuf {
    // src-tauri -> harmonizer -> apps -> repo root.
    let shared_venv_python = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../.venv/bin/python");
    if shared_venv_python.exists() {
        shared_venv_python
    } else {
        PathBuf::from("python3")
    }
}

#[derive(Serialize)]
pub struct AlignResult {
    report_path: String,
    report: Value,
}

#[tauri::command]
fn run_align(
    ref_path: String,
    take_paths: Vec<String>,
    no_retime_takes: Vec<String>,
) -> Result<AlignResult, String> {
    let proto = backend_dir()?;
    let report_path = std::env::temp_dir().join(format!("harmonizer_report_{}.json", std::process::id()));

    let mut cmd = Command::new(venv_python());
    cmd.arg(proto.join("align.py"))
        .arg("--ref").arg(&ref_path)
        .arg("--takes").args(&take_paths)
        .arg("--out").arg(&report_path);
    if !no_retime_takes.is_empty() {
        cmd.arg("--no-retime").args(&no_retime_takes);
    }

    let output = cmd.output().map_err(|e| format!("failed to launch align.py: {e}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).to_string());
    }

    let report_text = std::fs::read_to_string(&report_path)
        .map_err(|e| format!("align.py ran but report wasn't written: {e}"))?;
    let report: Value = serde_json::from_str(&report_text)
        .map_err(|e| format!("failed to parse report.json: {e}"))?;

    Ok(AlignResult {
        report_path: report_path.to_string_lossy().to_string(),
        report,
    })
}

#[derive(Serialize, Deserialize)]
pub struct ImportResult {
    project: String,
    timeline: String,
    fcpxml_path: String,
}

#[tauri::command]
fn run_import_to_resolve(
    report_path: String,
    ref_media: String,
    take_media: Vec<String>,
    project_name: Option<String>,
    timeline_name: Option<String>,
) -> Result<ImportResult, String> {
    let proto = backend_dir()?;

    let mut cmd = Command::new(venv_python());
    cmd.arg(proto.join("import_to_resolve.py"))
        .arg("--report").arg(&report_path)
        .arg("--ref-media").arg(&ref_media)
        .arg("--take-media").args(&take_media);
    if let Some(name) = project_name.filter(|n| !n.trim().is_empty()) {
        cmd.arg("--project").arg(name);
    }
    if let Some(name) = timeline_name.filter(|n| !n.trim().is_empty()) {
        cmd.arg("--timeline-name").arg(name);
    }

    let output = cmd
        .output()
        .map_err(|e| format!("failed to launch import_to_resolve.py: {e}"))?;

    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).to_string());
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(stdout.trim()).map_err(|e| format!("failed to parse import result: {e}"))
}

#[derive(Serialize, Deserialize)]
pub struct AnchorPoint {
    ref_time: f64,
    take_time: f64,
}

#[tauri::command]
fn run_recompute_segments(
    report_path: String,
    take: String,
    points: Vec<AnchorPoint>,
) -> Result<Value, String> {
    let proto = backend_dir()?;
    let points_path = std::env::temp_dir().join(format!("harmonizer_points_{}.json", std::process::id()));
    let points_json = serde_json::to_string(&points).map_err(|e| e.to_string())?;
    std::fs::write(&points_path, points_json).map_err(|e| format!("failed to write points file: {e}"))?;

    let output = Command::new(venv_python())
        .arg(proto.join("recompute_segments.py"))
        .arg("--report").arg(&report_path)
        .arg("--take").arg(&take)
        .arg("--points").arg(&points_path)
        .output()
        .map_err(|e| format!("failed to launch recompute_segments.py: {e}"))?;

    let _ = std::fs::remove_file(&points_path);

    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).to_string());
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(stdout.trim()).map_err(|e| format!("failed to parse recompute result: {e}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            run_align,
            run_import_to_resolve,
            run_recompute_segments
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    // Regression test for the panic this repo shipped for a while:
    // `backend_dir()` used to point at `apps/prototype` (a pre-migration
    // path the Phase 2 migration renamed to `apps/harmonizer/backend`),
    // which doesn't exist, so `.canonicalize().expect(...)` crashed every
    // one of this crate's three Tauri commands on first use.
    #[test]
    fn backend_dir_resolves_to_the_real_engine_directory() {
        let dir = backend_dir().expect("apps/harmonizer/backend/ should resolve from src-tauri/");
        assert!(
            dir.join("align.py").is_file(),
            "expected {dir:?} to contain align.py -- backend_dir() is pointing at the wrong directory"
        );
    }

    #[test]
    fn venv_python_is_either_the_shared_venv_or_a_python3_fallback() {
        let python = venv_python();
        let is_shared_venv = python
            .to_string_lossy()
            .ends_with(".venv/bin/python");
        let is_fallback = python == PathBuf::from("python3");
        assert!(
            is_shared_venv || is_fallback,
            "venv_python() returned {python:?}, expected the shared root .venv or a python3 fallback"
        );
    }
}
