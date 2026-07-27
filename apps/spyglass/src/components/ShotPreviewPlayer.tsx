import { useEffect, useRef } from "react";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import { api } from "../lib/api";
import type { ShotSearchResult } from "../types";

/// Click-to-play preview, in place of proxying/transcoding: plays the
/// shot's real source file directly, no generated media involved.
///
/// This does NOT use a web `<video>` element. WebKit's `<video>` only
/// decodes through hardware (VideoToolbox), which doesn't support the
/// 10-bit 4:2:2 H.264 this archive's Sony FX3 actually records in --
/// confirmed by comparing against QuickTime Player (AVFoundation-based,
/// like this) playing the exact same file cleanly at a timestamp where
/// the web player produced corrupted frames. Instead, the native side
/// embeds a real `AVPlayerView` (see `native_video_preview.rs`) as an
/// NSView subview positioned directly over the placeholder below, so
/// playback goes through the same AVFoundation stack Finder/QuickTime
/// use, with its own native transport controls (no custom play/pause UI
/// needed here). `open`/`close` are commands, not props, because the
/// player lives entirely on the native side -- this component only tells
/// it where to sit and when to stop.
///
/// The Rust side needs an AppKit (bottom-left-origin) rect, but the only
/// reliably correct height to flip against is `window.innerHeight` --
/// the wry-embedded WKWebView's own `NSView.frame().size.height` does not
/// match it (confirmed live: using the webview's reported frame height
/// consistently rendered the native view ~32pt too high, since that frame
/// includes space the titlebar overlaps that `innerHeight` correctly
/// excludes). Flipping here, with a value that's guaranteed to describe
/// the same viewport `getBoundingClientRect` measured against, avoids
/// re-deriving (and re-guessing) that height on the Rust side.
///
/// Known gap: the native view's position is captured once on open, not
/// re-measured on window resize -- fine for a modal that doesn't reflow
/// while open, but would drift if the window resizes while the preview
/// is showing.
export function ShotPreviewPlayer({ result, onClose }: { result: ShotSearchResult; onClose: () => void }) {
  const placeholderRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = placeholderRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    void api.openNativeVideoPreview(result.clip_file_path, result.start_tc, {
      x: rect.left,
      y: window.innerHeight - rect.top - rect.height,
      width: rect.width,
      height: rect.height,
    });

    return () => {
      void api.closeNativeVideoPreview();
    };
  }, [result.clip_file_path, result.start_tc]);

  const reveal = () => {
    void revealItemInDir(result.clip_file_path).catch(() => {
      // Most likely the source drive is offline -- non-fatal, just a no-op from the user's perspective.
    });
  };

  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="w-full max-w-3xl rounded border border-border-subtle bg-surface-raised p-4 text-sm text-white"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between gap-2">
          <p className="truncate text-xs text-cool-grey" title={result.clip_file_path}>
            {result.clip_file_path}
          </p>
          <div className="flex shrink-0 items-center gap-3">
            <button type="button" onClick={reveal} className="text-cool-grey hover:text-white">
              Reveal in Finder
            </button>
            <button type="button" onClick={onClose} className="text-cool-grey hover:text-white">
              Close
            </button>
          </div>
        </div>

        {/* The native AVPlayerView is positioned directly over this box from the Rust side. */}
        <div ref={placeholderRef} className="aspect-video w-full bg-black" />
      </div>
    </div>
  );
}
