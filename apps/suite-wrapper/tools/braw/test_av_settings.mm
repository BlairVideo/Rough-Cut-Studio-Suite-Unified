// test_av_settings.mm — regression coverage for AVFoundation output-
// settings combinations that previously crashed braw_proxy_tool with an
// UNCAUGHT NSException (libc++abi: terminating due to an uncaught
// exception), rather than the clean JSON error the tool's contract
// promises.
//
// Why this exists as its OWN small harness instead of only relying on
// the real .braw integration tests in Studio Suite/tests/: those only
// ever exercise the SDK's own sample.braw clip (2-channel 24-bit audio,
// even dimensions) — real customer footage from actual Blackmagic
// cameras (which commonly record 4+ discrete audio channels from
// multiple mic/XLR inputs) hits AVFoundation validation paths that
// sample clip never does, and there was no multi-channel .braw sample
// available to reproduce the real bug report with. This harness tests
// the exact settings-building logic (mirrored from braw_proxy_tool.mm,
// not merely restated) against every dimension/channel/bit-depth
// combination that's realistic for BRAW sources, independent of having
// real media on hand.
//
// Run: ./test_av_settings.sh (compiles and runs this file)
// A non-zero exit / any "FAIL" line means braw_proxy_tool.mm's actual
// settings-building code (the block this mirrors) needs the same fix.

#import <Foundation/Foundation.h>
#import <AVFoundation/AVFoundation.h>

static int g_failures = 0;

// Mirrors braw_proxy_tool.mm's own even-dimension guard exactly.
static void CheckVideoSettings(uint32_t width, uint32_t height, const char* label)
{
    const uint32_t encodeWidth = (width % 2 == 0) ? width : width - 1;
    const uint32_t encodeHeight = (height % 2 == 0) ? height : height - 1;
    @try
    {
        NSDictionary* videoSettings = @{
            AVVideoCodecKey: AVVideoCodecTypeH264,
            AVVideoWidthKey: @(encodeWidth),
            AVVideoHeightKey: @(encodeHeight),
            AVVideoCompressionPropertiesKey: @{
                AVVideoAverageBitRateKey: @((long long)encodeWidth * encodeHeight * 12),
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
            },
        };
        AVAssetWriterInput* input = [AVAssetWriterInput assetWriterInputWithMediaType:AVMediaTypeVideo
                                                                         outputSettings:videoSettings];
        (void)input;
        NSLog(@"PASS  video %s (%ux%u -> encode %ux%u)", label, width, height, encodeWidth, encodeHeight);
    }
    @catch (NSException* e)
    {
        NSLog(@"FAIL  video %s (%ux%u): %@ / %@", label, width, height, e.name, e.reason);
        g_failures++;
    }
}

// Mirrors braw_proxy_tool.mm's own audio-settings block exactly,
// including the AVChannelLayoutKey fix and the bit-depth guard.
static void CheckAudioSettings(uint32_t sampleRate, uint32_t channels, uint32_t bitDepth, const char* label)
{
    const bool validBitDepth = bitDepth == 8 || bitDepth == 16 || bitDepth == 24 || bitDepth == 32;
    if (!validBitDepth)
    {
        NSLog(@"SKIP  audio %s (bits=%u not in {8,16,24,32} -- braw_proxy_tool treats this clip as having no usable audio, by design)", label, bitDepth);
        return;
    }

    AudioChannelLayout channelLayout = {0};
    channelLayout.mChannelLayoutTag = kAudioChannelLayoutTag_DiscreteInOrder | channels;
    NSData* channelLayoutData = [NSData dataWithBytes:&channelLayout length:sizeof(AudioChannelLayout)];

    @try
    {
        NSDictionary* audioSettings = @{
            AVFormatIDKey: @(kAudioFormatLinearPCM),
            AVSampleRateKey: @(sampleRate),
            AVNumberOfChannelsKey: @(channels),
            AVLinearPCMBitDepthKey: @(bitDepth),
            AVLinearPCMIsFloatKey: @NO,
            AVLinearPCMIsBigEndianKey: @NO,
            AVLinearPCMIsNonInterleaved: @NO,
            AVChannelLayoutKey: channelLayoutData,
        };
        AVAssetWriterInput* input = [AVAssetWriterInput assetWriterInputWithMediaType:AVMediaTypeAudio
                                                                         outputSettings:audioSettings];
        (void)input;
        NSLog(@"PASS  audio %s (sr=%u ch=%u bits=%u)", label, sampleRate, channels, bitDepth);
    }
    @catch (NSException* e)
    {
        NSLog(@"FAIL  audio %s (sr=%u ch=%u bits=%u): %@ / %@", label, sampleRate, channels, bitDepth, e.name, e.reason);
        g_failures++;
    }
}

int main()
{
    // Video: the reported crash's suspected (and ruled-out) cause, plus
    // every real Blackmagic sensor resolution and a couple of degenerate
    // edge cases.
    CheckVideoSettings(4607, 2592, "odd width (originally suspected root cause)");
    CheckVideoSettings(4608, 2591, "odd height");
    CheckVideoSettings(12288, 6480, "Ursa 12K");
    CheckVideoSettings(8192, 4320, "8K");
    CheckVideoSettings(6144, 3456, "6K");
    CheckVideoSettings(4608, 2592, "4.6K (SDK sample clip)");
    CheckVideoSettings(1920, 1080, "1080p");

    // Audio: the ACTUAL root cause of the reported crash — any channel
    // count beyond mono/stereo throws without AVChannelLayoutKey, which
    // several real Blackmagic cameras' multi-mic/XLR recording modes hit
    // in practice (the SDK sample clip is only ever 2-channel, so this
    // path had never been exercised against real hardware output).
    CheckAudioSettings(48000, 1, 24, "mono");
    CheckAudioSettings(48000, 2, 24, "stereo (SDK sample clip)");
    CheckAudioSettings(48000, 4, 24, "4ch (multi-mic/XLR)");
    CheckAudioSettings(48000, 6, 24, "6ch");
    CheckAudioSettings(48000, 8, 24, "8ch");
    CheckAudioSettings(48000, 2, 16, "16-bit stereo");
    CheckAudioSettings(48000, 2, 32, "32-bit stereo");
    CheckAudioSettings(96000, 2, 24, "96kHz stereo");
    CheckAudioSettings(48000, 2, 20, "20-bit (unsupported -- must be skipped, not crash)");

    if (g_failures > 0)
    {
        NSLog(@"%d combination(s) failed -- braw_proxy_tool.mm needs the same fix applied here.", g_failures);
        return 1;
    }
    NSLog(@"All combinations passed.");
    return 0;
}
