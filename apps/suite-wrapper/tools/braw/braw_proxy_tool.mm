// braw_proxy_tool.mm — decodes a .braw clip via the Blackmagic RAW SDK and
// muxes it to an ordinary QuickTime .mov (H.264 video + linear PCM audio)
// that every existing ffmpeg/ffprobe/cv2/<video> code path in this suite
// can already play. Implements the contract documented in README.md next
// to this file; braw_bridge.py (backend/braw_bridge.py) is already
// written against that contract, so this binary is a drop-in once built
// — no suite-side code changes needed.
//
// Not vendored: BlackmagicRawAPI.h/BlackmagicRawAPIDispatch.cpp are
// Blackmagic's own proprietary SDK files and are never copied into this
// repo. build.sh compiles against wherever the SDK is actually installed
// on the machine doing the build (see that script for the search list).
//
// Decode strategy: fully sequential, one frame in flight at a time (read
// -> decode+process -> copy into a CVPixelBuffer -> append) via a
// dispatch_semaphore the callback signals. The SDK's own ProcessClipCPU
// sample instead pipelines several frames concurrently for throughput —
// deliberately not done here: a proxy transcode isn't latency-sensitive,
// and sequential-by-construction means frames can never need reordering
// before they reach the muxer. If proxy generation time ever becomes a
// real complaint, revisit with that sample's s_maxJobsInFlight pattern
// plus a small reorder buffer keyed by frame index.

#include "BlackmagicRawAPI.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <csignal>
#include <limits>
#include <mutex>
#include <string>
#include <vector>

#import <Foundation/Foundation.h>
#import <AVFoundation/AVFoundation.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <CoreAudio/CoreAudioTypes.h>
#import <Accelerate/Accelerate.h>

#ifdef DEBUG
    #include <cassert>
    #define VERIFY(condition) assert(SUCCEEDED(condition))
#else
    #define VERIFY(condition) condition
#endif

// ---------------------------------------------------------------------------
// worker_protocol JSON-lines helpers — must stay in exact sync with the
// contract backend/workers/worker_protocol.py defines and braw_bridge.py
// parses. NSJSONSerialization (not hand-rolled string concatenation) is
// used so a detail/message containing quotes or unicode still round-trips
// as valid JSON.
// ---------------------------------------------------------------------------

// Video and audio now run on their own independent GCD queues (see the
// requestMediaDataWhenReadyOnQueue: rewrite, CONTRACT.md addendum v45),
// so both can legitimately call EmitProgress/EmitError at the same time
// -- without this lock their stdout writes could interleave mid-line
// and corrupt the JSON-lines protocol.
static std::mutex g_stdoutMutex;

static void EmitLine(NSDictionary* message)
{
    std::lock_guard<std::mutex> lock(g_stdoutMutex);
    @autoreleasepool {
        NSData* data = [NSJSONSerialization dataWithJSONObject:message options:0 error:nil];
        fwrite(data.bytes, 1, data.length, stdout);
        fputc('\n', stdout);
        fflush(stdout);
    }
}

static void EmitProgress(double percent, const std::string& detail)
{
    EmitLine(@{
        @"type": @"progress",
        @"progress": @(percent),
        @"detail": [NSString stringWithUTF8String:detail.c_str()],
    });
}

static void EmitResult()
{
    EmitLine(@{ @"type": @"result", @"data": @{} });
}

static void EmitError(const std::string& message)
{
    EmitLine(@{
        @"type": @"error",
        @"message": [NSString stringWithUTF8String:message.c_str()],
    });
}

// ---------------------------------------------------------------------------
// Cancellation: braw_bridge.py's _run_proxy_tool sends SIGTERM (escalating
// to SIGKILL after a grace period, same as every subprocess job in
// jobs.py). A signal handler may only touch async-signal-safe state, so
// it does nothing but flip this flag; the frame loop below polls it.
// ---------------------------------------------------------------------------

static std::atomic<bool> g_cancelled{false};

static void HandleTerminationSignal(int)
{
    g_cancelled.store(true);
}

// ---------------------------------------------------------------------------
// SDK runtime discovery — kept in sync BY HAND with braw_bridge.py's
// _SDK_RUNTIME_CANDIDATES (backend/braw_bridge.py). That list holds the
// FRAMEWORK BUNDLE's own path (checked with a plain existence test);
// CreateBlackmagicRawFactoryInstanceFromPath instead wants the DIRECTORY
// CONTAINING the bundle (it appends "BlackmagicRawAPI.framework" itself
// — see BlackmagicRawAPIDispatch.cpp), so each entry here is that same
// real install location with the trailing "/BlackmagicRawAPI.framework"
// stripped off.
// ---------------------------------------------------------------------------

static const char* const kFrameworkParentCandidates[] = {
    "/Applications/Blackmagic RAW/Blackmagic RAW Player.app/Contents/Frameworks",
    "/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac/Libraries",
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Frameworks",
    "/Applications/DaVinci Resolve Studio/DaVinci Resolve Studio.app/Contents/Frameworks",
    "/Library/Frameworks",
};

static IBlackmagicRawFactory* CreateFactory()
{
    for (const char* dir : kFrameworkParentCandidates)
    {
        CFStringRef dirStr = CFStringCreateWithCString(kCFAllocatorDefault, dir, kCFStringEncodingUTF8);
        IBlackmagicRawFactory* factory = CreateBlackmagicRawFactoryInstanceFromPath(dirStr);
        CFRelease(dirStr);
        if (factory != nullptr)
            return factory;
    }
    return nullptr;
}

// ---------------------------------------------------------------------------
// Decode callback: one frame in flight. WaitForFrame blocks the main
// thread until ProcessComplete (success) or a Submit()/SetResourceFormat
// failure surfaced from ReadComplete signals the semaphore.
// ---------------------------------------------------------------------------

struct DecodedFrame
{
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t sizeBytes = 0;
    std::vector<uint8_t> pixels;   // copied out before the SDK releases its own buffer
};

class ToolCallback : public IBlackmagicRawCallback
{
public:
    ToolCallback()
    {
        m_semaphore = dispatch_semaphore_create(0);
    }

    void ResetForNextFrame()
    {
        m_result = S_OK;
        m_frame.pixels.clear();
    }

    // Returns the ProcessComplete/ReadComplete result for the frame most
    // recently submitted via CreateJobReadFrame + Submit().
    HRESULT WaitForFrame(DecodedFrame* outFrame)
    {
        dispatch_semaphore_wait(m_semaphore, DISPATCH_TIME_FOREVER);
        if (outFrame != nullptr)
            *outFrame = m_frame;
        return m_result;
    }

    virtual void ReadComplete(IBlackmagicRawJob* readJob, HRESULT result, IBlackmagicRawFrame* frame)
    {
        IBlackmagicRawJob* processJob = nullptr;

        if (result == S_OK)
            result = frame->SetResourceFormat(blackmagicRawResourceFormatBGRAU8);   // matches kCVPixelFormatType_32BGRA below — no channel-swap needed

        if (result == S_OK)
            result = frame->CreateJobDecodeAndProcessFrame(nullptr, nullptr, &processJob);

        if (result == S_OK)
            result = processJob->Submit();

        if (result != S_OK)
        {
            if (processJob != nullptr)
                processJob->Release();
            m_result = result;
            dispatch_semaphore_signal(m_semaphore);   // ProcessComplete will never fire for this frame — unblock now
        }

        readJob->Release();
    }

    virtual void ProcessComplete(IBlackmagicRawJob* job, HRESULT result, IBlackmagicRawProcessedImage* processedImage)
    {
        uint32_t width = 0, height = 0, sizeBytes = 0;
        void* imageData = nullptr;

        if (result == S_OK) result = processedImage->GetWidth(&width);
        if (result == S_OK) result = processedImage->GetHeight(&height);
        if (result == S_OK) result = processedImage->GetResourceSizeBytes(&sizeBytes);
        if (result == S_OK) result = processedImage->GetResource(&imageData);

        if (result == S_OK)
        {
            m_frame.width = width;
            m_frame.height = height;
            m_frame.sizeBytes = sizeBytes;
            const uint8_t* src = static_cast<const uint8_t*>(imageData);
            m_frame.pixels.assign(src, src + sizeBytes);   // SDK owns imageData only until job->Release() below
        }

        m_result = result;
        job->Release();
        dispatch_semaphore_signal(m_semaphore);
    }

    virtual void DecodeComplete(IBlackmagicRawJob*, HRESULT) {}
    virtual void TrimProgress(IBlackmagicRawJob*, float) {}
    virtual void TrimComplete(IBlackmagicRawJob*, HRESULT) {}
    virtual void SidecarMetadataParseWarning(IBlackmagicRawClip*, CFStringRef, uint32_t, CFStringRef) {}
    virtual void SidecarMetadataParseError(IBlackmagicRawClip*, CFStringRef, uint32_t, CFStringRef) {}
    virtual void PreparePipelineComplete(void*, HRESULT) {}

    virtual HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, LPVOID*) { return E_NOTIMPL; }
    virtual ULONG STDMETHODCALLTYPE AddRef(void) { return 0; }
    virtual ULONG STDMETHODCALLTYPE Release(void) { return 0; }

private:
    dispatch_semaphore_t m_semaphore;
    HRESULT m_result = S_OK;
    DecodedFrame m_frame;
};

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

// Fixed, highly-divisible timescale for video presentation times — a
// per-clip exact rational frame duration isn't available from
// IBlackmagicRawClip::GetFrameRate (a plain float), so the nearest tick
// at this timescale is used instead (well under a millisecond of drift
// per frame even for 23.976/29.97 rates). This is a scrubbing/preview
// proxy, never the export path (Premiere XML export always references
// the ORIGINAL .braw path, never this proxy — see CONTRACT.md addendum
// v28), so exact frame-accurate timing isn't required here.
static const int32_t kVideoTimescale = 600000;

static bool RemoveExistingFile(NSString* path)
{
    NSFileManager* fm = [NSFileManager defaultManager];
    if (![fm fileExistsAtPath:path])
        return true;
    NSError* error = nil;
    return [fm removeItemAtPath:path error:&error] == YES;
}

// Parses a BRAW SDK timecode string ("HH:MM:SS:FF", or "HH:MM:SS;FF" for
// drop-frame -- mirrors A-Sync's sync_core.py .replace(";", ":") parsing
// convention for consistency, not because any code is shared between the
// two) into the frame count a CMTimeCodeFormatType_TimeCode32 sample
// expects, per the standard SMPTE 12M-1 drop-frame formula. Returns
// false (leaving *outFrameNumber untouched) for anything unparseable --
// embedded timecode is a nice-to-have, never worth failing proxy
// generation over.
static bool ParseTimecodeToFrameNumber(const std::string& timecodeStr, float frameRate,
                                       bool isDropFrame, int32_t* outFrameNumber)
{
    std::string normalized = timecodeStr;
    std::replace(normalized.begin(), normalized.end(), ';', ':');

    int hh = 0, mm = 0, ss = 0, ff = 0;
    if (sscanf(normalized.c_str(), "%d:%d:%d:%d", &hh, &mm, &ss, &ff) != 4)
        return false;

    const int64_t fpsRounded = llround((double)frameRate);
    if (fpsRounded <= 0)
        return false;

    int64_t frameNumber;
    if (isDropFrame)
    {
        // The only two real drop-frame rates: 2 dropped frame NUMBERS
        // per minute at 29.97fps, 4 at 59.94fps.
        const int64_t dropFramesPerMinute = fpsRounded >= 50 ? 4 : 2;
        const int64_t totalMinutes = 60 * hh + mm;
        frameNumber = (fpsRounded * 3600 * hh)
                    + (fpsRounded * 60 * mm)
                    + (fpsRounded * ss)
                    + ff
                    - (dropFramesPerMinute * (totalMinutes - totalMinutes / 10));
    }
    else
    {
        frameNumber = fpsRounded * (int64_t)(hh * 3600 + mm * 60 + ss) + ff;
    }

    if (frameNumber < 0 || frameNumber > std::numeric_limits<int32_t>::max())
        return false;
    *outFrameNumber = (int32_t)frameNumber;
    return true;
}

int main(int argc, const char* argv[])
{
    if (argc != 3)
    {
        EmitError("Usage: braw_proxy_tool <source.braw> <output.mov>");
        return 1;
    }

    signal(SIGTERM, HandleTerminationSignal);
    signal(SIGINT, HandleTerminationSignal);

    const std::string sourcePath = argv[1];
    const std::string outputPath = argv[2];

    @autoreleasepool
    {
    NSString* nsOutputPath = [NSString stringWithUTF8String:outputPath.c_str()];
    if (!RemoveExistingFile(nsOutputPath))
    {
        EmitError("Couldn't remove existing output file: " + outputPath);
        return 1;
    }

    IBlackmagicRawFactory* factory = nullptr;
    IBlackmagicRaw* codec = nullptr;
    IBlackmagicRawClip* clip = nullptr;
    IBlackmagicRawClipAudio* audio = nullptr;
    ToolCallback callback;

    int exitCode = 0;
    bool wroteAnyOutput = false;

    // Everything below touches AVFoundation, which reports programmer-
    // error-shaped problems (an invalid/unsupported output-settings
    // combination — the specific trigger seen in production was an odd
    // width/height, now guarded above, but AVFoundation's own validation
    // surface is broader than any one guard) as an UNCAUGHT
    // NSException, not an NSError/nil return — without this @try, that
    // crashes the whole process (libc++abi: terminating due to an
    // uncaught exception) instead of reporting a clean JSON error this
    // tool's contract promises. `break` still works normally for the
    // existing early-exit paths below; only an actual thrown exception
    // reaches @catch.
    @try
    {
    do
    {
        factory = CreateFactory();
        if (factory == nullptr)
        {
            EmitError("Blackmagic RAW runtime not found (checked the usual DaVinci Resolve / "
                      "Blackmagic RAW install locations) — this shouldn't happen if "
                      "braw_bridge.braw_available() reported true.");
            exitCode = 1;
            break;
        }

        HRESULT result = factory->CreateCodec(&codec);
        if (result != S_OK) { EmitError("Failed to create a Blackmagic RAW codec instance."); exitCode = 1; break; }

        CFStringRef sourcePathCF = CFStringCreateWithCString(kCFAllocatorDefault, sourcePath.c_str(), kCFStringEncodingUTF8);
        result = codec->OpenClip(sourcePathCF, &clip);
        CFRelease(sourcePathCF);
        if (result != S_OK) { EmitError("Failed to open clip: " + sourcePath); exitCode = 1; break; }

        result = codec->SetCallback(&callback);
        if (result != S_OK) { EmitError("Failed to set the decode callback."); exitCode = 1; break; }

        uint32_t width = 0, height = 0;
        uint64_t frameCount = 0;
        float frameRate = 0.0f;
        VERIFY(clip->GetWidth(&width));
        VERIFY(clip->GetHeight(&height));
        VERIFY(clip->GetFrameRate(&frameRate));
        VERIFY(clip->GetFrameCount(&frameCount));
        if (width == 0 || height == 0 || frameRate <= 0.0f || frameCount == 0)
        {
            EmitError("Clip reported invalid dimensions/frame rate/frame count.");
            exitCode = 1;
            break;
        }

        // Embedded starting timecode (CONTRACT.md addendum v56): a cheap
        // metadata lookup against the CLIP (never a per-frame decode --
        // GetTimecodeForFrame doesn't read/process frame 0's pixels).
        // Missing or unparseable timecode is deliberately NOT a hard
        // failure -- the proxy is still fully usable without it, so any
        // problem here just skips adding the track below, same
        // degrade-gracefully posture as the optional audio track.
        CFStringRef startTimecodeCF = nullptr;
        const bool haveTimecode = clip->GetTimecodeForFrame(0, &startTimecodeCF) == S_OK
                                   && startTimecodeCF != nullptr;
        bool timecodeIsDropFrame = false;
        if (haveTimecode)
        {
            IBlackmagicRawClipEx* clipEx = nullptr;
            if (clip->QueryInterface(IID_IBlackmagicRawClipEx, (void**)&clipEx) == S_OK)
            {
                uint32_t baseFrameIndex = 0;
                clipEx->QueryTimecodeInfo(&baseFrameIndex, &timecodeIsDropFrame);
                clipEx->Release();
            }
        }

        // Scrubbing/preview proxies never need source resolution -- this
        // tool's own contract already treats the proxy as strictly a
        // preview artifact (Premiere XML export always references the
        // ORIGINAL .braw, never this file). A real stall this tool once
        // hit at 6K was suspected to be a hardware-encoder resolution
        // ceiling (CONTRACT.md addendum v40) -- that turned out to be
        // wrong (addendum v45/v46: the real cause was a lost wakeup on
        // readyForMoreMediaData, fixed independently of resolution), but
        // downscaling stayed anyway on its own merits: smaller, faster-
        // to-generate proxies for a use case that never needed source
        // resolution to begin with. The longest edge is capped well
        // under typical hardware H.264 encode limits and forced even
        // (H.264 4:2:0 requires it).
        static const uint32_t kMaxProxyDimension = 1920;
        const uint32_t longestEdge = std::max(width, height);
        const double downscale = longestEdge > kMaxProxyDimension
            ? (double)kMaxProxyDimension / (double)longestEdge : 1.0;
        uint32_t encodeWidth = (uint32_t)llround((double)width * downscale);
        uint32_t encodeHeight = (uint32_t)llround((double)height * downscale);
        if (encodeWidth % 2 != 0) encodeWidth -= 1;
        if (encodeHeight % 2 != 0) encodeHeight -= 1;
        encodeWidth = std::max(encodeWidth, (uint32_t)2);
        encodeHeight = std::max(encodeHeight, (uint32_t)2);

        const bool hasAudio = clip->QueryInterface(IID_IBlackmagicRawClipAudio, (void**)&audio) == S_OK;
        uint32_t audioBitDepth = 0, audioChannelCount = 0, audioSampleRate = 0;
        uint64_t audioSampleCount = 0;
        if (hasAudio)
        {
            audio->GetAudioBitDepth(&audioBitDepth);
            audio->GetAudioChannelCount(&audioChannelCount);
            audio->GetAudioSampleRate(&audioSampleRate);
            audio->GetAudioSampleCount(&audioSampleCount);
            // AVLinearPCMBitDepthKey only accepts 8/16/24/32 (AVFoundation
            // throws an uncaught NSException at AVAssetWriterInput
            // creation for anything else) — reinterpreting the SDK's own
            // packed samples at a DIFFERENT bit depth than it actually
            // reports would corrupt the audio (misaligned sample
            // boundaries), so an unsupported depth is treated as no
            // usable audio rather than guessed at.
            const bool validBitDepth = audioBitDepth == 8 || audioBitDepth == 16
                                     || audioBitDepth == 24 || audioBitDepth == 32;
            if (!validBitDepth || audioChannelCount == 0 || audioSampleRate == 0 || audioSampleCount == 0)
            {
                audio->Release();
                audio = nullptr;   // treat as no usable audio rather than failing the whole proxy
            }
        }

        EmitProgress(1.0, "Preparing proxy");

        NSError* error = nil;
        AVAssetWriter* writer = [AVAssetWriter assetWriterWithURL:[NSURL fileURLWithPath:nsOutputPath]
                                                          fileType:AVFileTypeQuickTimeMovie
                                                             error:&error];
        if (writer == nil)
        {
            EmitError("Failed to create the output file: " + std::string(error.localizedDescription.UTF8String));
            exitCode = 1;
            break;
        }

        // Bits-per-pixel-PER-FRAME budget for a good-quality (not
        // master-grade) H.264 scrubbing/editing proxy. The bitrate scales
        // with frame rate — a flat width*height*constant asks for the
        // same bits/second whether the clip is 24fps or 60fps, which is
        // nonsensical: at 24fps each frame must carry proportionally
        // more data to hit the same bits/second target. (A real stall
        // this tool once hit was suspected to be caused by the old flat
        // formula's bitrate being unsustainable -- CONTRACT.md addendum
        // v36 -- but that turned out to be unrelated; see addendum v45
        // for the actual cause. The frame-rate-aware formula stayed
        // anyway since it's simply more correct.) 0.1 bits/pixel/frame is
        // a well-established, generous quality target for H.264 at this
        // tier of use; the min/max clamp is a second line of defense
        // against any degenerate resolution/frame-rate combination
        // producing an unreasonable number.
        static const double kBitsPerPixelPerFrame = 0.1;
        static const int64_t kMinBitsPerSecond = 2000000;      // 2 Mbps floor
        static const int64_t kMaxBitsPerSecond = 100000000;    // 100 Mbps ceiling
        int64_t targetBitsPerSecond = (int64_t)((double)encodeWidth * (double)encodeHeight
                                                 * (double)frameRate * kBitsPerPixelPerFrame);
        targetBitsPerSecond = std::max(kMinBitsPerSecond, std::min(targetBitsPerSecond, kMaxBitsPerSecond));

        // NOTE: an earlier round of this investigation (CONTRACT.md
        // addendum v44) forced software H.264 encoding here
        // (AVVideoEncoderSpecificationKey /
        // kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder:
        // NO), suspecting a wedged hardware encoder session. The actual
        // root cause turned out to be unrelated (a lost wakeup in the
        // manually-polled readyForMoreMediaData property, fixed by the
        // requestMediaDataWhenReadyOnQueue: rewrite -- addendum v45).
        // Once that real fix was in place, hardware encoding was
        // re-tested directly against the real clip and worked
        // correctly (addendum v46) -- the hardware encoder was never
        // actually broken. Forcing software encoding was removed since
        // it's strictly slower with no remaining upside; VideoToolbox's
        // own default (prefer hardware when available) is used as-is.
        NSDictionary* videoSettings = @{
            AVVideoCodecKey: AVVideoCodecTypeH264,
            AVVideoWidthKey: @(encodeWidth),
            AVVideoHeightKey: @(encodeHeight),
            AVVideoCompressionPropertiesKey: @{
                AVVideoAverageBitRateKey: @(targetBitsPerSecond),
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
                // High profile lets VideoToolbox enable B-frame reordering
                // by default. A real stall this tool once hit was
                // suspected to be caused by the encoder's reorder
                // lookahead buffer (CONTRACT.md addendum v38) -- that
                // turned out to be unrelated (see addendum v45 for the
                // actual cause), but disabling reordering stayed anyway:
                // this is a scrubbing proxy with no need for B-frames'
                // extra compression efficiency, and submission-order
                // output is simpler to reason about.
                AVVideoAllowFrameReorderingKey: @NO,
            },
        };
        AVAssetWriterInput* videoInput = [AVAssetWriterInput assetWriterInputWithMediaType:AVMediaTypeVideo
                                                                             outputSettings:videoSettings];
        videoInput.expectsMediaDataInRealTime = NO;

        NSDictionary* pixelBufferAttrs = @{
            (NSString*)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_32BGRA),
            (NSString*)kCVPixelBufferWidthKey: @(encodeWidth),
            (NSString*)kCVPixelBufferHeightKey: @(encodeHeight),
        };
        AVAssetWriterInputPixelBufferAdaptor* adaptor =
            [AVAssetWriterInputPixelBufferAdaptor assetWriterInputPixelBufferAdaptorWithAssetWriterInput:videoInput
                                                                               sourcePixelBufferAttributes:pixelBufferAttrs];

        if (![writer canAddInput:videoInput])
        {
            EmitError("The output file can't accept an H.264 video track.");
            exitCode = 1;
            break;
        }
        [writer addInput:videoInput];

        AVAssetWriterInput* audioInput = nil;
        if (hasAudio)
        {
            // AVFoundation throws an uncaught NSException at
            // AVAssetWriterInput creation for any channel count beyond
            // mono/stereo unless AVChannelLayoutKey is present (confirmed
            // empirically — this is the real crash reported in
            // production, from a real multi-channel-audio BRAW source;
            // several Blackmagic cameras record up to 4+ discrete audio
            // channels). kAudioChannelLayoutTag_DiscreteInOrder is the
            // correct, honest tag here — these are independent recorded
            // channels (camera mic + XLR inputs, etc.), not a real
            // spatial layout like 5.1, and the tag says exactly that.
            AudioChannelLayout channelLayout = {0};
            channelLayout.mChannelLayoutTag = kAudioChannelLayoutTag_DiscreteInOrder | audioChannelCount;
            NSData* channelLayoutData = [NSData dataWithBytes:&channelLayout length:sizeof(AudioChannelLayout)];

            NSDictionary* audioSettings = @{
                AVFormatIDKey: @(kAudioFormatLinearPCM),
                AVSampleRateKey: @(audioSampleRate),
                AVNumberOfChannelsKey: @(audioChannelCount),
                AVLinearPCMBitDepthKey: @(audioBitDepth),
                AVLinearPCMIsFloatKey: @NO,
                AVLinearPCMIsBigEndianKey: @NO,
                AVLinearPCMIsNonInterleaved: @NO,
                AVChannelLayoutKey: channelLayoutData,
            };
            audioInput = [AVAssetWriterInput assetWriterInputWithMediaType:AVMediaTypeAudio outputSettings:audioSettings];
            audioInput.expectsMediaDataInRealTime = NO;
            if ([writer canAddInput:audioInput])
                [writer addInput:audioInput];
            else
                audioInput = nil;   // proceed video-only rather than failing the whole proxy
        }

        const int64_t frameDurationTicks = llround(kVideoTimescale / (double)frameRate);

        // Embedded timecode track (CONTRACT.md addendum v56): a single
        // sample spanning the whole clip, giving the STARTING timecode —
        // the standard QuickTime convention (a reader/NLE derives every
        // other frame's timecode by counting forward from it at the
        // track's own frame rate). Written once, synchronously, right
        // after the session starts and before either async block below
        // is even registered — deliberately NOT using
        // requestMediaDataWhenReadyOnQueue:/expectsMediaDataInRealTime at
        // all, so this can't reintroduce anything resembling the
        // video/audio async stall this tool was so hard-won to fix
        // (addendum v45/v46). Best-effort: any failure along the way
        // (unparseable timecode, format description creation failure,
        // writer rejecting the input) just skips the track — the proxy
        // is still fully usable without it.
        AVAssetWriterInput* timecodeInput = nil;
        int32_t timecodeStartFrameNumber = 0;
        CMTimeCodeFormatDescriptionRef timecodeFormatDesc = NULL;
        if (haveTimecode)
        {
            std::string timecodeStr;
            const char* fastPtr = CFStringGetCStringPtr(startTimecodeCF, kCFStringEncodingUTF8);
            if (fastPtr != nullptr)
            {
                timecodeStr = fastPtr;
            }
            else
            {
                char buffer[64];
                if (CFStringGetCString(startTimecodeCF, buffer, sizeof(buffer), kCFStringEncodingUTF8))
                    timecodeStr = buffer;
            }

            if (!timecodeStr.empty() &&
                ParseTimecodeToFrameNumber(timecodeStr, frameRate, timecodeIsDropFrame, &timecodeStartFrameNumber))
            {
                OSStatus tcStatus = CMTimeCodeFormatDescriptionCreate(
                    kCFAllocatorDefault,
                    kCMTimeCodeFormatType_TimeCode32,
                    CMTimeMake(frameDurationTicks, kVideoTimescale),
                    (uint32_t)llround((double)frameRate),
                    timecodeIsDropFrame ? kCMTimeCodeFlag_DropFrame : 0,
                    NULL,
                    &timecodeFormatDesc);

                if (tcStatus == noErr && timecodeFormatDesc != NULL)
                {
                    AVAssetWriterInput* candidate =
                        [AVAssetWriterInput assetWriterInputWithMediaType:AVMediaTypeTimecode
                                                            outputSettings:nil
                                                          sourceFormatHint:(CMFormatDescriptionRef)timecodeFormatDesc];
                    if ([writer canAddInput:candidate])
                    {
                        [writer addInput:candidate];
                        [videoInput addTrackAssociationWithTrackOfInput:candidate
                                                                    type:AVTrackAssociationTypeTimecode];
                        timecodeInput = candidate;
                    }
                    else
                    {
                        CFRelease(timecodeFormatDesc);
                        timecodeFormatDesc = NULL;
                    }
                }
            }
        }
        if (startTimecodeCF != nullptr)
        {
            CFRelease(startTimecodeCF);
            startTimecodeCF = nullptr;
        }

        if (![writer startWriting])
        {
            EmitError("Failed to start writing the output file: " +
                      std::string(writer.error.localizedDescription.UTF8String));
            exitCode = 1;
            break;
        }
        [writer startSessionAtSourceTime:kCMTimeZero];
        wroteAnyOutput = true;   // from here on, a failure leaves a partial file worth cleaning up

        if (timecodeInput != nil && timecodeFormatDesc != NULL)
        {
            // One sample, one write, done immediately, entirely on the
            // main thread — no requestMediaDataWhenReadyOnQueue:/
            // expectsMediaDataInRealTime involvement at all (unlike
            // video/audio below), so this can't reintroduce anything
            // resembling the async stall this tool was so hard-won to
            // fix (addendum v45/v46). A freshly-added input's
            // readyForMoreMediaData starts YES until real backpressure
            // builds up, which one 4-byte sample never will — checked
            // anyway as cheap, consistent defensiveness with the rest of
            // this tool.
            const int32_t sampleValueBigEndian = (int32_t)CFSwapInt32HostToBig((uint32_t)timecodeStartFrameNumber);
            CMBlockBufferRef tcBlockBuffer = NULL;
            CMBlockBufferCreateWithMemoryBlock(kCFAllocatorDefault, NULL, sizeof(sampleValueBigEndian),
                                                kCFAllocatorDefault, NULL, 0, sizeof(sampleValueBigEndian), 0, &tcBlockBuffer);
            if (tcBlockBuffer != NULL)
            {
                CMBlockBufferReplaceDataBytes(&sampleValueBigEndian, tcBlockBuffer, 0, sizeof(sampleValueBigEndian));

                // One sample spanning the WHOLE clip's duration -- the
                // standard QuickTime timecode-track convention: a reader
                // derives every other frame's timecode by counting
                // forward from this single starting value at the
                // track's own frame rate, not from one sample per frame.
                const CMTime wholeClipDuration = CMTimeMake((int64_t)frameCount * frameDurationTicks, kVideoTimescale);
                CMSampleTimingInfo tcTiming = { wholeClipDuration, kCMTimeZero, kCMTimeInvalid };
                size_t tcSampleSize = sizeof(sampleValueBigEndian);
                CMSampleBufferRef tcSampleBuffer = NULL;
                CMSampleBufferCreate(kCFAllocatorDefault, tcBlockBuffer, true, NULL, NULL,
                                      (CMFormatDescriptionRef)timecodeFormatDesc,
                                      1, 1, &tcTiming, 1, &tcSampleSize, &tcSampleBuffer);
                if (tcSampleBuffer != NULL)
                {
                    if (timecodeInput.readyForMoreMediaData)
                        [timecodeInput appendSampleBuffer:tcSampleBuffer];
                    CFRelease(tcSampleBuffer);
                }
                CFRelease(tcBlockBuffer);
            }
            [timecodeInput markAsFinished];
        }
        if (timecodeFormatDesc != NULL)
            CFRelease(timecodeFormatDesc);

        // ---- video + audio, via AVFoundation's own async request
        // pattern (requestMediaDataWhenReadyOnQueue:usingBlock:) ----
        //
        // Every previous approach here (through v44) manually polled
        // videoInput.readyForMoreMediaData in a spin loop -- a
        // documented-supported pattern for expectsMediaDataInRealTime =
        // NO, and one that worked for every real sample available in
        // this environment, but a real production 6K clip reproducibly
        // stalled inside that poll at a small, early frame no matter
        // what was changed: bitrate (v36), run-loop pumping (v35),
        // B-frame reordering (v38), resolution (v40), audio/video
        // presentation-time lockstep (v41-v43), even forcing software
        // encoding (v44). A stack sample taken from the live, stalled
        // process (CONTRACT.md addendum v45) settled why: EVERY internal
        // AVFoundation/CoreMedia worker thread --
        // com.apple.coremedia.mediaprocessor.videocompression,
        // .audiocompression, and com.apple.coremedia.formatwriter.qtmovie
        // -- was 100% idle, parked on its own condition variable, for the
        // entire multi-second stall. The actual encode+mux pipeline had
        // fully drained everything appended so far and had nothing left
        // to do; it was never overloaded. videoInput.readyForMoreMediaData
        // simply never flipped back to YES to reflect that drained
        // state -- a lost wakeup between the (idle) pipeline and the
        // property this tool was manually polling, not a capacity
        // problem any setting could fix.
        //
        // Switching to Apple's own documented asynchronous pattern lets
        // AVFoundation itself decide when to re-invoke each track's
        // block, rather than this tool polling a property that wasn't
        // updating correctly. Video and audio each get their own serial
        // queue and their own callback block; AVFoundation calls each
        // block whenever that track wants more data, and the block loops
        // producing samples for as long as it's told it's ready.
        __block uint64_t nextFrameIndex = 0;
        __block bool anyFailed = false;
        __block bool videoDone = false;
        __block bool audioDoneFlag = false;   // "Flag" suffix avoids shadowing the audioDone local used in the wait loop below

        // A block literal captures a plain (non-__block) C++ object like
        // `callback` by making its own const COPY -- but the BRAW SDK was
        // handed &callback via codec->SetCallback() above and will only
        // ever signal THAT object's semaphore, never a block-local copy's.
        // Capturing a pointer instead (trivially copyable) keeps the
        // block operating on the exact same instance the SDK calls back
        // into.
        ToolCallback* callbackPtr = &callback;

        dispatch_queue_t videoQueue = dispatch_queue_create("studio_suite.braw_proxy_tool.video", DISPATCH_QUEUE_SERIAL);
        dispatch_semaphore_t videoDoneSemaphore = dispatch_semaphore_create(0);

        [videoInput requestMediaDataWhenReadyOnQueue:videoQueue usingBlock:^{
            while (videoInput.readyForMoreMediaData)
            {
                if (g_cancelled.load() || anyFailed || nextFrameIndex >= frameCount)
                {
                    if (!videoDone)
                    {
                        videoDone = true;
                        [videoInput markAsFinished];
                        dispatch_semaphore_signal(videoDoneSemaphore);
                    }
                    return;
                }

                const uint64_t frameIndex = nextFrameIndex++;
                callbackPtr->ResetForNextFrame();

                IBlackmagicRawJob* readJob = nullptr;
                HRESULT frameResult = clip->CreateJobReadFrame(frameIndex, &readJob);
                if (frameResult == S_OK)
                    frameResult = readJob->Submit();
                if (frameResult != S_OK)
                {
                    if (readJob != nullptr) readJob->Release();
                    EmitError("Failed to submit a read job for frame " + std::to_string(frameIndex) + ".");
                    anyFailed = true;
                    videoDone = true;
                    [videoInput markAsFinished];
                    dispatch_semaphore_signal(videoDoneSemaphore);
                    return;
                }

                DecodedFrame decoded;
                frameResult = callbackPtr->WaitForFrame(&decoded);
                if (frameResult != S_OK)
                {
                    EmitError("Failed to decode/process frame " + std::to_string(frameIndex) + ".");
                    anyFailed = true;
                    videoDone = true;
                    [videoInput markAsFinished];
                    dispatch_semaphore_signal(videoDoneSemaphore);
                    return;
                }

                CVPixelBufferRef pixelBuffer = NULL;
                CVReturn cvResult = CVPixelBufferPoolCreatePixelBuffer(NULL, adaptor.pixelBufferPool, &pixelBuffer);
                if (cvResult != kCVReturnSuccess || pixelBuffer == NULL)
                {
                    EmitError("Failed to allocate a pixel buffer for frame " + std::to_string(frameIndex) + ".");
                    anyFailed = true;
                    videoDone = true;
                    [videoInput markAsFinished];
                    dispatch_semaphore_signal(videoDoneSemaphore);
                    return;
                }

                CVPixelBufferLockBaseAddress(pixelBuffer, 0);
                uint8_t* dst = static_cast<uint8_t*>(CVPixelBufferGetBaseAddress(pixelBuffer));
                const size_t dstStride = CVPixelBufferGetBytesPerRow(pixelBuffer);
                const size_t srcStride = decoded.height > 0 ? decoded.sizeBytes / decoded.height : 0;

                // Actual resize (not just a crop) from the SDK's native-
                // resolution decoded frame straight into the proxy's
                // smaller pixel buffer. vImageScale_ARGB8888 is channel-
                // order-agnostic -- it interpolates four independent
                // 8-bit planes per pixel, which is exactly correct for
                // BGRA too, it just doesn't care what the four channels
                // mean. A NULL tempBuffer lets vImage manage its own
                // scratch space; this isn't a latency-sensitive per-
                // frame path so the extra alloc/free per frame is an
                // acceptable tradeoff for simplicity.
                vImage_Buffer srcBuf = { decoded.pixels.data(), decoded.height, decoded.width, srcStride };
                vImage_Buffer dstBuf = { dst, encodeHeight, encodeWidth, dstStride };
                vImage_Error scaleResult = vImageScale_ARGB8888(&srcBuf, &dstBuf, NULL, kvImageNoFlags);
                CVPixelBufferUnlockBaseAddress(pixelBuffer, 0);

                if (scaleResult != kvImageNoError)
                {
                    CVPixelBufferRelease(pixelBuffer);
                    EmitError("Failed to scale frame " + std::to_string(frameIndex) + " for the proxy.");
                    anyFailed = true;
                    videoDone = true;
                    [videoInput markAsFinished];
                    dispatch_semaphore_signal(videoDoneSemaphore);
                    return;
                }

                CMTime pts = CMTimeMake(frameIndex * frameDurationTicks, kVideoTimescale);
                [adaptor appendPixelBuffer:pixelBuffer withPresentationTime:pts];
                CVPixelBufferRelease(pixelBuffer);

                const double pct = 5.0 + (double)(frameIndex + 1) / (double)frameCount * (hasAudio && audioInput != nil ? 85.0 : 94.0);
                EmitProgress(pct, "Decoding frame " + std::to_string(frameIndex + 1) + " of " + std::to_string(frameCount));

                if (frameIndex + 1 >= frameCount)
                {
                    videoDone = true;
                    [videoInput markAsFinished];
                    dispatch_semaphore_signal(videoDoneSemaphore);
                    return;
                }
            }
        }];

        dispatch_queue_t audioQueue = nil;
        dispatch_semaphore_t audioDoneSemaphore = dispatch_semaphore_create(0);
        CMAudioFormatDescriptionRef audioFormatDesc = NULL;

        if (audioInput != nil)
        {
            AudioStreamBasicDescription asbd = {0};
            asbd.mSampleRate = audioSampleRate;
            asbd.mFormatID = kAudioFormatLinearPCM;
            asbd.mFormatFlags = kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked;
            asbd.mBitsPerChannel = audioBitDepth;
            asbd.mChannelsPerFrame = audioChannelCount;
            asbd.mFramesPerPacket = 1;
            asbd.mBytesPerFrame = (audioBitDepth / 8) * audioChannelCount;
            asbd.mBytesPerPacket = asbd.mBytesPerFrame * asbd.mFramesPerPacket;

            OSStatus status = CMAudioFormatDescriptionCreate(kCFAllocatorDefault, &asbd, 0, NULL, 0, NULL, NULL, &audioFormatDesc);
            if (status != noErr)
            {
                EmitError("Failed to describe the audio format for the output file.");
                exitCode = 1;
                break;
            }

            const uint32_t chunkSampleFrames = audioSampleRate;   // ~1 second per chunk, mirrors ExtractAudio.cpp's maxSampleCount
            const uint32_t bufferSize = chunkSampleFrames * asbd.mBytesPerFrame;
            __block std::vector<uint8_t> audioBuffer(bufferSize);
            __block uint64_t audioSampleIndex = 0;

            audioQueue = dispatch_queue_create("studio_suite.braw_proxy_tool.audio", DISPATCH_QUEUE_SERIAL);
            [audioInput requestMediaDataWhenReadyOnQueue:audioQueue usingBlock:^{
                while (audioInput.readyForMoreMediaData)
                {
                    if (g_cancelled.load() || anyFailed || audioSampleIndex >= audioSampleCount)
                    {
                        if (!audioDoneFlag)
                        {
                            audioDoneFlag = true;
                            [audioInput markAsFinished];
                            dispatch_semaphore_signal(audioDoneSemaphore);
                        }
                        return;
                    }

                    const uint64_t samplesRemaining = audioSampleCount - audioSampleIndex;
                    const uint32_t requestSampleFrames = (uint32_t)std::min((uint64_t)chunkSampleFrames, samplesRemaining);

                    uint32_t samplesRead = 0, bytesRead = 0;
                    HRESULT audioResult = audio->GetAudioSamples((int64_t)audioSampleIndex, audioBuffer.data(), bufferSize,
                                                                  requestSampleFrames, &samplesRead, &bytesRead);
                    if (audioResult != S_OK || samplesRead == 0)
                    {
                        audioDoneFlag = true;
                        [audioInput markAsFinished];
                        dispatch_semaphore_signal(audioDoneSemaphore);
                        return;
                    }

                    CMBlockBufferRef blockBuffer = NULL;
                    CMBlockBufferCreateWithMemoryBlock(kCFAllocatorDefault, NULL, bytesRead, kCFAllocatorDefault,
                                                        NULL, 0, bytesRead, 0, &blockBuffer);
                    CMBlockBufferReplaceDataBytes(audioBuffer.data(), blockBuffer, 0, bytesRead);

                    CMSampleTimingInfo timing = { CMTimeMake(1, (int32_t)audioSampleRate),
                                                   CMTimeMake((int64_t)audioSampleIndex, (int32_t)audioSampleRate),
                                                   kCMTimeInvalid };
                    CMSampleBufferRef sampleBuffer = NULL;
                    CMSampleBufferCreate(kCFAllocatorDefault, blockBuffer, true, NULL, NULL, audioFormatDesc,
                                          samplesRead, 1, &timing, 0, NULL, &sampleBuffer);

                    [audioInput appendSampleBuffer:sampleBuffer];

                    CFRelease(sampleBuffer);
                    CFRelease(blockBuffer);

                    audioSampleIndex += samplesRead;

                    if (audioSampleIndex >= audioSampleCount)
                    {
                        audioDoneFlag = true;
                        [audioInput markAsFinished];
                        dispatch_semaphore_signal(audioDoneSemaphore);
                        return;
                    }
                }
            }];
        }
        else
        {
            dispatch_semaphore_signal(audioDoneSemaphore);   // nothing to wait for
        }

        // Supervisory wait on the main thread: polls both completion
        // semaphores with a short timeout rather than waiting forever,
        // specifically so a writer that enters AVAssetWriterStatusFailed
        // (which stops AVFoundation from ever re-invoking either block
        // again) doesn't leave this tool hung forever waiting for a
        // signal that will now never come.
        bool videoWaitDone = false, audioWaitDone = false;
        bool writerFailedDuringWait = false;
        while (!videoWaitDone || !audioWaitDone)
        {
            if (!videoWaitDone && dispatch_semaphore_wait(videoDoneSemaphore, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(100 * NSEC_PER_MSEC))) == 0)
                videoWaitDone = true;
            if (!audioWaitDone && dispatch_semaphore_wait(audioDoneSemaphore, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(100 * NSEC_PER_MSEC))) == 0)
                audioWaitDone = true;
            if ((!videoWaitDone || !audioWaitDone) && writer.status == AVAssetWriterStatusFailed)
            {
                writerFailedDuringWait = true;
                anyFailed = true;
                break;
            }
        }

        if (audioFormatDesc != NULL)
            CFRelease(audioFormatDesc);

        if (writerFailedDuringWait)
        {
            NSString* desc = writer.error != nil ? writer.error.localizedDescription : @"unknown error";
            EmitError("The output writer failed: " + std::string(desc.UTF8String));
            exitCode = 1;
            break;
        }
        if (anyFailed) { exitCode = 1; break; }
        if (g_cancelled.load()) { exitCode = 1; break; }

        if (audioInput != nil)
            EmitProgress(95.0, "Writing remaining audio");

        // ---- finalize ----

        EmitProgress(97.0, "Finalizing");
        dispatch_semaphore_t finishSemaphore = dispatch_semaphore_create(0);
        [writer finishWritingWithCompletionHandler:^{
            dispatch_semaphore_signal(finishSemaphore);
        }];
        dispatch_semaphore_wait(finishSemaphore, DISPATCH_TIME_FOREVER);

        if (writer.status != AVAssetWriterStatusCompleted)
        {
            NSString* desc = writer.error != nil ? writer.error.localizedDescription : @"unknown error";
            EmitError("Failed to finalize the output file: " + std::string(desc.UTF8String));
            exitCode = 1;
            break;
        }

        EmitProgress(100.0, "Done");
        EmitResult();

    } while (0);
    }
    @catch (NSException* exception)
    {
        EmitError("Unexpected error while building the proxy: " +
                  std::string(exception.reason != nil ? exception.reason.UTF8String
                                                        : exception.name.UTF8String));
        exitCode = 1;
    }

    if (audio != nullptr) audio->Release();
    if (clip != nullptr) clip->Release();
    if (codec != nullptr) codec->Release();
    if (factory != nullptr) factory->Release();

    if (exitCode != 0 && wroteAnyOutput)
        RemoveExistingFile(nsOutputPath);   // never leave a corrupt/partial proxy behind on failure

    return exitCode;

    }   // @autoreleasepool
}
