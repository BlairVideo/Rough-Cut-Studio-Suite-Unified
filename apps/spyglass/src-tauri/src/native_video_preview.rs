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
use std::sync::Mutex;
use tauri::{Manager, WebviewWindow};

struct ActivePreview {
    view: Retained<AVPlayerView>,
    player: Retained<AVPlayer>,
}

// Safety: only ever constructed, read, or dropped from inside a
// `with_webview` callback (main thread). See module doc comment.
unsafe impl Send for ActivePreview {}

#[derive(Default)]
pub struct NativePreviewState {
    inner: Mutex<Option<ActivePreview>>,
}

#[tauri::command]
pub fn open_native_video_preview(
    window: WebviewWindow,
    path: String,
    start_tc: f64,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> Result<(), String> {
    let app = window.app_handle().clone();
    window
        .with_webview(move |webview| {
            let mtm = MainThreadMarker::new().expect("with_webview runs on the main thread");
            let state = app.state::<NativePreviewState>();

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

                player.seekToTime(CMTime::with_seconds(start_tc, 600));
                player.play();

                *state.inner.lock().unwrap() = Some(ActivePreview { view: player_view, player });
            }
        })
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn close_native_video_preview(window: WebviewWindow) -> Result<(), String> {
    let app = window.app_handle().clone();
    window
        .with_webview(move |_webview| {
            let taken = app.state::<NativePreviewState>().inner.lock().unwrap().take();
            if let Some(active) = taken {
                unsafe {
                    active.player.pause();
                    active.view.removeFromSuperview();
                }
            }
        })
        .map_err(|e| e.to_string())
}
