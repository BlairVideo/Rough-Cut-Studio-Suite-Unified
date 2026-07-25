# `braw_proxy_tool` — the BRAW proxy helper

`backend/braw_bridge.py` shells out to a small compiled binary expected
at `paths.BRAW_TOOL_BIN` (`tools/braw/braw_proxy_tool`). Source
(`braw_proxy_tool.mm`) is checked into this repo and builds clean; the
compiled **binary itself is gitignored** (a machine-specific build
artifact, like every `.venv/`) and is not present until you run
`build.sh` on a machine with the Blackmagic RAW SDK installed. Until
then, `braw_bridge.braw_available()` correctly reports `False` and every
BRAW-dependent code path degrades gracefully — that is a normal,
fully-supported state for most checkouts of this repo, not a bug.

The SDK itself (headers, dispatch glue, sample projects) is Blackmagic's
own proprietary, non-open-source download and is **never vendored** into
this repo — `build.sh` compiles against wherever it's actually installed
on the machine doing the build.

## Building it

```
./build.sh
```

Auto-detects the SDK's `Mac/Include` directory at its default install
location (`/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac/Include`
— the standalone free "Blackmagic RAW" installer, which also drops the
Player app and Speed Test app alongside it under `/Applications/
Blackmagic RAW/`). Override with `BRAW_SDK_INCLUDE=/path ./build.sh` if
it's installed somewhere else, or if the SDK ever ships a different
layout. Output lands at `tools/braw/braw_proxy_tool`, exactly where
`paths.BRAW_TOOL_BIN` expects it — no suite-side configuration needed
after a build.

**Verified working** (this repo, against a real SDK install): built
clean on the first attempt, and a real run against the SDK's own
`Media/sample.braw` sample clip produced a genuinely playable `.mov` —
confirmed via `ffmpeg`/`ffprobe` (zero decode errors, `probe_score=100`)
and OpenCV (`cv2.VideoCapture` opens it and reads a correctly-colored,
correctly-oriented frame at the source's native 4608×2592 resolution).
See CONTRACT.md addendum v29 for the full verification record.

## Invocation

```
braw_proxy_tool <source.braw> <output.mov>
```

Two positional arguments, both absolute paths. No flags, no stdin.

## stdout — one JSON object per line

Identical wire format to every Python subprocess worker in this suite
(`backend/workers/worker_protocol.py`), so the same parser
(`worker_protocol.parse_line`) already used to read Python workers'
stdout also reads this tool's:

```
{"type": "progress", "progress": <float 0-100>, "detail": "<str>"}
{"type": "result",   "data": {}}
{"type": "error",    "message": "<str>"}
```

- Emits `progress` lines throughout decode (per-frame during video,
  per ~1s chunk during audio, plus setup/finalize bookends) for the Jobs
  drawer's progress bar.
- On success: writes a real, playable H.264 (video) + linear PCM (audio,
  when the clip has an audio track) `.mov` at `<output.mov>` — anything
  `ffprobe`/`cv2`/an HTML5 `<video>` element can already open, since
  that's the entire point of the proxy — then emits exactly one `result`
  line. `data` is empty; `braw_bridge.py` already knows the output path.
- On failure: emits an `error` line with a human-readable `message`
  before exiting non-zero, and removes any partial `<output.mov>` it had
  started writing (never leaves a corrupt file behind).
- Exit code 0 only on genuine success. `braw_bridge._run_proxy_tool`
  additionally verifies `<output.mov>` actually exists on disk before
  treating a `result` line as trustworthy.

## Cancellation

`braw_bridge.py` cancels a running job by sending `SIGTERM` (via
`Popen.terminate()`), escalating to `SIGKILL` after a grace period
(same policy `jobs.py` already uses for every subprocess job). A
`SIGTERM`/`SIGINT` handler flips an atomic flag the frame/audio loops
poll between iterations, so the tool unwinds and deletes its partial
output promptly rather than needing the `SIGKILL` escalation in the
common case.

## Implementation notes (braw_proxy_tool.mm)

- **Decode is fully sequential** — one frame read+decoded+processed at a
  time via a `dispatch_semaphore`, not pipelined like the SDK's own
  `ProcessClipCPU` sample (`s_maxJobsInFlight`). A proxy transcode isn't
  latency-sensitive, and sequential-by-construction means frames never
  need reordering before reaching the muxer. Revisit with that sample's
  pattern (plus a small reorder buffer) if generation time ever becomes
  a real complaint.
- **Writing is asynchronous**, via `-[AVAssetWriterInput
  requestMediaDataWhenReadyOnQueue:usingBlock:]` — one serial GCD queue
  and callback block per track (video, audio), each re-invoked by
  AVFoundation whenever that specific track wants more data. This
  replaced an earlier design that manually polled
  `videoInput.readyForMoreMediaData` in a spin loop — a documented-
  supported alternative for `expectsMediaDataInRealTime = NO`, but one
  that a real production clip proved could permanently stall (a live
  stack sample showed the entire encode+mux pipeline fully idle while
  the polled property never reported ready again — a lost wakeup, not a
  capacity problem). See CONTRACT.md addendum v45 for the full
  investigation. Because video and audio now genuinely run concurrently
  on separate queues, `EmitLine` (this tool's only stdout output) takes
  a `std::mutex` to keep JSON-lines output from interleaving mid-line.
- **Video is downscaled to a 1920px-longest-edge preview resolution**
  (`vImageScale_ARGB8888`, Accelerate framework) rather than encoded at
  the source's native resolution — this tool is strictly a scrubbing/
  preview proxy, never the export path, so full sensor resolution was
  always more than the job needs.
- **Hardware H.264 encoding** (VideoToolbox's own default) is used —
  an earlier round of the same investigation forced software encoding
  on the (wrong, later disproven) suspicion that the hardware encoder
  itself was wedging; removed once the real cause and fix
  (`requestMediaDataWhenReadyOnQueue:`, above) were confirmed to also
  work correctly with hardware encoding re-enabled. See CONTRACT.md
  addendum v46.
- **Pixel format**: frames are requested from the SDK as
  `blackmagicRawResourceFormatBGRAU8` specifically because that's
  `CVPixelBuffer`'s native `kCVPixelFormatType_32BGRA` — zero channel
  reordering needed between the SDK's decode buffer and the muxer's
  pixel buffer, just a straight resize via `vImageScale_ARGB8888` (still
  needed even at matching dimensions because the two buffers' row
  strides aren't guaranteed equal).
- **Video timestamps** use a fixed 600000 timescale (highly divisible;
  keeps 23.976/29.97-style rates within a fraction of a millisecond of
  drift per frame) rather than an exact rational frame duration, since
  `IBlackmagicRawClip::GetFrameRate` only exposes a plain `float`. This
  is fine for a scrubbing/preview proxy — the actual Premiere XML export
  always references the **original** `.braw` path, never this proxy (see
  CONTRACT.md's "non-merged, original media" philosophy), so proxy
  timing doesn't need frame-accuracy.
- **Embedded starting timecode** (CONTRACT.md addendum v56): a single
  QuickTime `tmcd` sample spanning the whole clip, giving the reader a
  starting timecode to count forward from — the standard convention, not
  one sample per frame. Sourced from `IBlackmagicRawClip::
  GetTimecodeForFrame(0, ...)` (a cheap metadata lookup, not a frame
  decode) and `IBlackmagicRawClipEx::QueryTimecodeInfo` (drop-frame
  flag), converted to a frame count via the standard SMPTE 12M-1
  drop-frame formula. Written synchronously, immediately after the
  session starts and before either async video/audio block is even
  registered — deliberately not using
  `requestMediaDataWhenReadyOnQueue:`/`expectsMediaDataInRealTime` at all
  for this one track, so it can't reintroduce anything resembling the
  async stall addendum v45 fixed. Best-effort like the audio track: an
  unparseable/missing timecode just skips the track, never fails proxy
  generation. This is what lets `A-Sync/sync_core.py`'s existing
  `codec_tag_string == "tmcd"` detection (unmodified) work for a `.braw`
  source's "timecode" sync method, previously always reporting "no
  embedded timecode found."
- **SDK runtime discovery** (`kFrameworkParentCandidates`) is a
  hand-maintained duplicate of `backend/braw_bridge.py`'s
  `_SDK_RUNTIME_CANDIDATES` list, adjusted from "the framework bundle's
  own path" to "the directory containing it" (what
  `CreateBlackmagicRawFactoryInstanceFromPath` expects — it appends
  `BlackmagicRawAPI.framework` itself). If you add a new install
  location to one list, add it to the other.
- `BlackmagicRawAPIDispatch.cpp` (from the SDK's own `Mac/Include/`) is
  compiled alongside `braw_proxy_tool.mm` by `build.sh` — it's the glue
  that dynamically loads the framework bundle via `CFBundle` at runtime
  (dlopen-style, not link-time linking), so the tool works against
  whichever real install it finds without being built against a
  specific `BlackmagicRawAPI.framework` copy.
- **The whole proxy-building body is wrapped in `@try`/`@catch`** around
  an uncaught `NSException`. AVFoundation reports invalid/unsupported
  output-settings combinations (see below) by *throwing*, not by
  returning `nil`/an `NSError` — without this, such a case crashes the
  whole process (`libc++abi: terminating due to an uncaught exception`)
  instead of the clean JSON `error` line the tool's contract promises.
  Two concrete triggers are guarded explicitly (odd width/height;
  non-mono/stereo audio without an explicit channel layout — see
  CONTRACT.md addendum v33), but AVFoundation's validation surface is
  broader than any fixed set of guards, so the `@try`/`@catch` stays as
  the backstop for whatever's next. **Run `./test_av_settings.sh`**
  after touching the video/audio settings dictionaries — it exercises
  every dimension/channel/bit-depth combination known to matter (real
  Blackmagic sensor resolutions, multi-channel audio, every AVFoundation-
  valid bit depth) against AVFoundation directly, no SDK or real `.braw`
  media required, and fails loudly if any combination throws.

## What `braw_bridge.py` does NOT need from this tool

- No frame-accurate seeking/thumbnail extraction (that's a separate,
  cheaper future code path — see the BRAW compatibility handoff doc's
  "Where proxies get frame-level access for thumbnails" open item).
- No metadata/probe-only mode — `ffprobe`-equivalent info is read from
  the generated proxy itself once it exists.
- No config beyond the two paths above; quality/codec choices are the
  tool's own fixed policy, not something the suite currently exposes.
