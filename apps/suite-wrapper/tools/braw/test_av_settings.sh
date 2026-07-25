#!/bin/bash
# test_av_settings.sh — compiles and runs test_av_settings.mm. Unlike
# build.sh, this needs no Blackmagic RAW SDK at all (pure AVFoundation,
# no BlackmagicRawAPI.h) — run it any time braw_proxy_tool.mm's video/
# audio settings-building logic changes, to catch an AVFoundation
# validation regression before it ships as an uncaught-exception crash.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BINARY="$SCRIPT_DIR/test_av_settings"

clang++ \
    -std=gnu++17 \
    -stdlib=libc++ \
    -fobjc-arc \
    -mmacosx-version-min=10.15 \
    -framework Foundation \
    -framework AVFoundation \
    -framework CoreAudio \
    -o "$BINARY" \
    "$SCRIPT_DIR/test_av_settings.mm"

"$BINARY"
STATUS=$?
rm -f "$BINARY"
exit $STATUS
