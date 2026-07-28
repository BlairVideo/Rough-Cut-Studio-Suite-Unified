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
use objc2_app_kit::NSView;
use objc2_av_foundation::AVPlayer;
use objc2_av_kit::AVPlayerView;
use objc2_core_foundation::{CGPoint, CGRect, CGSize};
use objc2_core_media::CMTime;
use objc2_foundation::NSURL;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use std::sync::atomic::Ordering;
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

/// Forward-buffered cushion given to the preview player -- see
/// `src-tauri/src/native_video_preview.rs`'s copy of this same const for
/// the full rationale (watched-root footage on a slow archival/external
/// volume needs more headroom than AVFoundation's network-streaming-tuned
/// default of 0).
const PREFERRED_FORWARD_BUFFER_SEC: f64 = 8.0;

/// The gap-fill queue's manual-pause flag as it was *before* a preview
/// session auto-paused it -- `None` while no preview is open. Mirrors
/// `NativePreviewState::paused_before_preview` in the Tauri shell (a
/// `static` here rather than an `AppHandle`-managed field, since this
/// module has no equivalent app-state container to hang it off of).
static PAUSED_BEFORE_PREVIEW: Mutex<Option<bool>> = Mutex::new(None);

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

    // Pause background gap-fill for the duration of the preview, same
    // rationale as the Tauri shell's copy of this logic -- those jobs
    // read source clip files too, and contending with them for the same
    // slow external/archival drive is a common source of preview stutter
    // unrelated to the preview file itself. Only capture+pause on the
    // *first* open of a session (a shot-switch that replaces an
    // already-active preview must not re-capture "already paused" as the
    // value to restore to). Best-effort: if the engine isn't initialized
    // yet, skip the pause rather than failing the whole preview open --
    // this module has no hard dependency on the engine otherwise.
    if let Ok(engine_state) = crate::state() {
        let mut prior = PAUSED_BEFORE_PREVIEW.lock().unwrap();
        if prior.is_none() {
            *prior = Some(engine_state.queue_control.paused.load(Ordering::Relaxed));
            engine_state.queue_control.paused.store(true, Ordering::Relaxed);
        }
    }

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

        if let Some(item) = player.currentItem() {
            item.setPreferredForwardBufferDuration(PREFERRED_FORWARD_BUFFER_SEC);
        }

        player.seekToTime(CMTime::with_seconds(start_tc, 600));
        player.play();

        *ACTIVE_PREVIEW.lock().unwrap() = Some(ActivePreview { view: player_view, player });
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

    let prior_paused = PAUSED_BEFORE_PREVIEW.lock().unwrap().take();
    if let Some(prior_paused) = prior_paused {
        if let Ok(engine_state) = crate::state() {
            engine_state.queue_control.paused.store(prior_paused, Ordering::Relaxed);
        }
    }
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(debug_webview_frame, m)?)?;
    m.add_function(wrap_pyfunction!(open_native_video_preview, m)?)?;
    m.add_function(wrap_pyfunction!(close_native_video_preview, m)?)?;
    Ok(())
}
