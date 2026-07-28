//! Native `AVPlayerView`-based video preview (macOS only).
//!
//! The web `<video>` element's decode path is hardware-only (VideoToolbox),
//! which doesn't support the 10-bit 4:2:2 H.264 this archive's Sony FX3
//! actually records in -- confirmed by comparing against QuickTime Player
//! (which, like this, decodes through AVFoundation) playing the exact same
//! file cleanly at the exact same timestamp where the web player corrupted.
//! Rather than proxy/transcode the file, this embeds a real `AVPlayerView`
//! as a native `NSView` subview, positioned over a placeholder in the web
//! content, so playback goes through the same AVFoundation stack Finder
//! and QuickTime already use.
//!
//! `Retained<T>` objects here are never `Send`/`Sync` on their own -- every
//! touch of `AVPlayerView`/`AVPlayer` happens inside `WebviewWindow::
//! with_webview`, which Tauri guarantees runs on the main thread, so the
//! `unsafe impl Send` below is sound only as long as that invariant holds.

use objc2::rc::Retained;
use objc2::MainThreadMarker;
use objc2_app_kit::NSView;
use objc2_av_foundation::AVPlayer;
use objc2_av_kit::AVPlayerView;
use objc2_core_foundation::{CGPoint, CGRect, CGSize};
use objc2_core_media::CMTime;
use objc2_foundation::NSURL;
use spyglass_engine::EngineState;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use tauri::{Manager, State, WebviewWindow};

struct ActivePreview {
    view: Retained<AVPlayerView>,
    player: Retained<AVPlayer>,
}

// Safety: only ever constructed, read, or dropped from inside a
// `with_webview` callback (main thread). See module doc comment.
unsafe impl Send for ActivePreview {}

/// Forward-buffered cushion given to the preview player, well above
/// AVFoundation's own "automatic" default (0, which tunes for typical
/// network-streaming assumptions). Watched-root footage often lives on a
/// secondary/archival volume (an external drive, or one reached through a
/// Finder alias) with far higher read latency and lower sustained
/// throughput than the machine's own boot volume -- a bigger cushion here
/// absorbs a slow read or a momentary stall on that volume before it
/// turns into a visibly dropped frame.
const PREFERRED_FORWARD_BUFFER_SEC: f64 = 8.0;

#[derive(Default)]
pub struct NativePreviewState {
    inner: Mutex<Option<ActivePreview>>,
    /// The gap-fill queue's manual-pause flag as it was *before* a preview
    /// session auto-paused it (see below) -- `None` while no preview is
    /// open. Captured once per open/close session (not per shot switch,
    /// since `open_native_video_preview` replacing an already-active
    /// preview must not overwrite this with "already paused"), so closing
    /// restores exactly what the user had, rather than always resuming.
    paused_before_preview: Mutex<Option<bool>>,
}

#[tauri::command]
pub fn open_native_video_preview(
    window: WebviewWindow,
    engine_state: State<Arc<EngineState>>,
    path: String,
    start_tc: f64,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> Result<(), String> {
    let app = window.app_handle().clone();
    let engine_state = engine_state.inner().clone();
    window
        .with_webview(move |webview| {
            let mtm = MainThreadMarker::new().expect("with_webview runs on the main thread");
            let state = app.state::<NativePreviewState>();

            // Pause background gap-fill (CLIP embedding/VLM captioning/
            // keyframe extraction) for the duration of the preview -- those
            // jobs read source clip files too, and contending with them for
            // the same slow external/archival drive is a common source of
            // stutter that has nothing to do with the preview file itself.
            // Only capture+pause on the *first* open of a session; a
            // shot-switch that replaces an already-active preview must
            // leave the queue paused without re-capturing "paused" as the
            // value to restore to.
            {
                let mut prior = state.paused_before_preview.lock().unwrap();
                if prior.is_none() {
                    *prior = Some(engine_state.queue_control.paused.load(Ordering::Relaxed));
                    engine_state.queue_control.paused.store(true, Ordering::Relaxed);
                }
            }

            unsafe {
                // The webview itself, not the window's contentView -- `x`/`y`
                // (already converted to AppKit's bottom-left origin by the
                // caller, see `ShotPreviewPlayer.tsx`) are relative to the
                // webview's own viewport.
                let webview_view: &NSView = &*webview.inner().cast();

                // Replace whatever preview (if any) was already showing.
                if let Some(previous) = state.inner.lock().unwrap().take() {
                    previous.player.pause();
                    previous.view.removeFromSuperview();
                }

                let Some(url) = NSURL::from_file_path(&path) else {
                    return;
                };
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

                *state.inner.lock().unwrap() = Some(ActivePreview { view: player_view, player });
            }
        })
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn close_native_video_preview(window: WebviewWindow, engine_state: State<Arc<EngineState>>) -> Result<(), String> {
    let app = window.app_handle().clone();
    let engine_state = engine_state.inner().clone();
    window
        .with_webview(move |_webview| {
            let state = app.state::<NativePreviewState>();

            let taken = state.inner.lock().unwrap().take();
            if let Some(active) = taken {
                unsafe {
                    active.player.pause();
                    active.view.removeFromSuperview();
                }
            }

            let prior_paused = state.paused_before_preview.lock().unwrap().take();
            if let Some(prior_paused) = prior_paused {
                engine_state.queue_control.paused.store(prior_paused, Ordering::Relaxed);
            }
        })
        .map_err(|e| e.to_string())
}
