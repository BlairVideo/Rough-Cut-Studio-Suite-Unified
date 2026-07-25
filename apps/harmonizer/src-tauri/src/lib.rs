use std::path::PathBuf;
use std::process::Command;

use serde::{Deserialize, Serialize};
use serde_json::Value;

fn prototype_dir() -> PathBuf {
    // CARGO_MANIFEST_DIR is .../Harmonizer/app/src-tauri at build time.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../prototype")
        .canonicalize()
        .expect("prototype/ directory not found next to app/")
}

fn venv_python() -> PathBuf {
    prototype_dir().join(".venv/bin/python3")
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
    let proto = prototype_dir();
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
    let proto = prototype_dir();

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
    let proto = prototype_dir();
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
