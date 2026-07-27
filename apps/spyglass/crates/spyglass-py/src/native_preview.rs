//! Native `AVPlayerView`-based video preview, ported from Spyglass's own
//! `src-tauri/src/native_video_preview.rs` to run inside a pywebview
//! window instead of a Tauri/wry one. The AVFoundation/AppKit logic
//! itself is essentially unchanged -- only how the host `NSView` pointer
//! is obtained differs: Tauri's `WebviewWindow::with_webview` handed it
//! over directly (and guaranteed main-thread execution); here, Python
//! obtains it via PyObjC (`objc.pyobjc_id(webview_obj)`) and passes it in
//! as a plain integer, and Python is responsible for dispatching the
//! actual call onto the main thread itself (via `PyObjCTools.AppHelper.
//! callAfter`) before calling in -- `MainThreadMarker::new()` below
//! returns `None` and this rejects the call outright if that dispatch
//! didn't happen, rather than silently touching AppKit off the main
//! thread.
//!
//! Do NOT assume the original implementation's coordinate-space fix
//! (measure `window.innerHeight` in JS rather than trusting the
//! webview's own `frame().size.height` from Rust) carries over unchanged
//! -- that fix was empirically discovered for Tauri/wry's specific
//! webview, and pywebview's cocoa backend manages its own window chrome
//! differently. `debug_webview_frame` exists specifically to re-measure
//! this from scratch (see the Phase 4 spike test) before trusting any
//! coordinate conversion here.

use objc2::rc::Retained;
use objc2::MainThreadMarker;
use objc2_app_kit::{NSBitmapImageFileType, NSView};
use objc2_av_foundation::AVPlayer;
use objc2_av_kit::AVPlayerView;
use objc2_core_foundation::{CGPoint, CGRect, CGSize};
use objc2_core_media::CMTime;
use objc2_foundation::NSDictionary;
use objc2_foundation::NSURL;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use std::sync::Mutex;

struct ActivePreview {
    view: Retained<AVPlayerView>,
    player: Retained<AVPlayer>,
}

// Safety: only ever constructed, read, or dropped from a call that already
// required a `MainThreadMarker` to obtain (i.e. the main thread). See
// module doc comment -- the Python caller is responsible for actually
// being on the main thread when it calls into these functions at all.
unsafe impl Send for ActivePreview {}

static ACTIVE_PREVIEW: Mutex<Option<ActivePreview>> = Mutex::new(None);

fn main_thread_marker() -> PyResult<MainThreadMarker> {
    MainThreadMarker::new().ok_or_else(|| {
        PyRuntimeError::new_err(
            "not running on the main thread -- the Python caller must dispatch this call via \
             PyObjCTools.AppHelper.callAfter before calling in, since pywebview's own js_api \
             calls are dispatched on a worker thread",
        )
    })
}

/// Diagnostic only (Phase 4 spike): reads the given view's AppKit frame
/// (bottom-left-origin `(x, y, width, height)`), to be compared against
/// the same webview's JS-measured `window.innerWidth`/`innerHeight` --
/// proving or disproving whether the original Tauri/wry coordinate-space
/// quirk (the embedded webview's own `frame()` reporting full window
/// height rather than usable content height) also applies to pywebview's
/// cocoa backend, before any real coordinate-conversion code is written
/// against an assumption either way.
#[pyfunction]
fn debug_webview_frame(view_ptr: usize) -> PyResult<(f64, f64, f64, f64)> {
    let _mtm = main_thread_marker()?;
    if view_ptr == 0 {
        return Err(PyRuntimeError::new_err("view_ptr is null"));
    }
    let view: &NSView = unsafe { &*(view_ptr as *const NSView) };
    let frame = view.frame();
    Ok((frame.origin.x, frame.origin.y, frame.size.width, frame.size.height))
}

/// Opens (replacing any existing) native preview: embeds a real
/// `AVPlayerView` as an `NSView` subview of the webview at `view_ptr`,
/// positioned at `(x, y, width, height)` (already converted to AppKit's
/// bottom-left origin by the caller), seeked to `start_tc`, and playing.
/// See the module doc comment for the main-thread requirement.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn open_native_video_preview(view_ptr: usize, path: String, start_tc: f64, x: f64, y: f64, width: f64, height: f64) -> PyResult<()> {
    let mtm = main_thread_marker()?;
    if view_ptr == 0 {
        return Err(PyRuntimeError::new_err("view_ptr is null"));
    }
    let Some(url) = NSURL::from_file_path(&path) else {
        return Err(PyRuntimeError::new_err(format!("couldn't build a file URL from {path}")));
    };

    unsafe {
        let webview_view: &NSView = &*(view_ptr as *const NSView);

        if let Some(previous) = ACTIVE_PREVIEW.lock().unwrap().take() {
            previous.player.pause();
            previous.view.removeFromSuperview();
        }

        let player = AVPlayer::playerWithURL(&url, mtm);
        let player_view = AVPlayerView::new(mtm);
        player_view.setPlayer(Some(&player));
        player_view.setFrame(CGRect::new(CGPoint::new(x, y), CGSize::new(width, height)));
        webview_view.addSubview(&player_view);

        player.seekToTime(CMTime::with_seconds(start_tc, 600));
        player.play();

        *ACTIVE_PREVIEW.lock().unwrap() = Some(ActivePreview { view: player_view, player });
    }
    Ok(())
}

/// Diagnostic only (Phase 4 verification, not part of the real surface):
/// the active preview's real `AVPlayer` state -- `(rate, status_code,
/// has_error)`, where `status_code` is `AVPlayerStatus` (0=unknown,
/// 1=readyToPlay, 2=failed) and `rate > 0.0` means it is actually
/// playing, not just that `play()` was called without erroring.
#[pyfunction]
fn debug_player_status() -> PyResult<(f64, i64, bool)> {
    let _mtm = main_thread_marker()?;
    let guard = ACTIVE_PREVIEW.lock().unwrap();
    let Some(active) = guard.as_ref() else {
        return Err(PyRuntimeError::new_err("no active preview"));
    };
    unsafe {
        let rate = active.player.rate() as f64;
        let status = active.player.status().0 as i64;
        let has_error = active.player.error().is_some();
        Ok((rate, status, has_error))
    }
}

/// Diagnostic only (Phase 4 verification): captures the active preview
/// view's actual rendered pixels to a PNG file -- real visual evidence
/// of whether the embedded AVPlayerView is showing clean video or
/// corruption, rather than just "no error was raised."
#[pyfunction]
fn debug_capture_preview_png(output_path: String) -> PyResult<()> {
    let _mtm = main_thread_marker()?;
    let guard = ACTIVE_PREVIEW.lock().unwrap();
    let Some(active) = guard.as_ref() else {
        return Err(PyRuntimeError::new_err("no active preview to capture"));
    };
    unsafe {
        let view: &NSView = &active.view;
        let frame = view.frame();
        let Some(bitmap) = view.bitmapImageRepForCachingDisplayInRect(frame) else {
            return Err(PyRuntimeError::new_err("bitmapImageRepForCachingDisplayInRect returned None"));
        };
        view.cacheDisplayInRect_toBitmapImageRep(frame, &bitmap);
        let props = NSDictionary::new();
        let Some(data) = bitmap.representationUsingType_properties(NSBitmapImageFileType::PNG, &props) else {
            return Err(PyRuntimeError::new_err("representationUsingType_properties (PNG) returned None"));
        };
        let bytes = data.to_vec();
        std::fs::write(&output_path, bytes).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
    }
    Ok(())
}

#[pyfunction]
fn close_native_video_preview() -> PyResult<()> {
    let _mtm = main_thread_marker()?;
    let taken = ACTIVE_PREVIEW.lock().unwrap().take();
    if let Some(active) = taken {
        unsafe {
            active.player.pause();
            active.view.removeFromSuperview();
        }
    }
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(debug_webview_frame, m)?)?;
    m.add_function(wrap_pyfunction!(open_native_video_preview, m)?)?;
    m.add_function(wrap_pyfunction!(close_native_video_preview, m)?)?;
    m.add_function(wrap_pyfunction!(debug_player_status, m)?)?;
    m.add_function(wrap_pyfunction!(debug_capture_preview_png, m)?)?;
    Ok(())
}
