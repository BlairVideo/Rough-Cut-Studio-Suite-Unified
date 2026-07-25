#!/bin/bash
# build.sh — compiles braw_proxy_tool against a locally-installed
# Blackmagic RAW SDK. The SDK itself is never vendored into this repo
# (proprietary, not open-source) — this script locates BlackmagicRawAPI.h
# / BlackmagicRawAPIDispatch.cpp wherever the SDK is actually installed
# on the machine running this script and compiles against that.
#
# Usage:
#   ./build.sh                      # auto-detect the SDK's Include dir
#   BRAW_SDK_INCLUDE=/path ./build.sh   # override, if auto-detect fails
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT="$SCRIPT_DIR/braw_proxy_tool"

# Kept in sync BY HAND with the runtime candidates in
# backend/braw_bridge.py and braw_proxy_tool.mm's kFrameworkParentCandidates
# — this is the SDK's own Mac/Include dir specifically (headers + the
# dispatch glue), a different thing from the installed runtime framework.
# An array (not a plain string) so install paths containing spaces (every
# entry here does) aren't word-split.
CANDIDATES=(
    "/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac/Include"
)

if [ -n "$BRAW_SDK_INCLUDE" ]; then
    SDK_INCLUDE="$BRAW_SDK_INCLUDE"
else
    SDK_INCLUDE=""
    for candidate in "${CANDIDATES[@]}"; do
        if [ -f "$candidate/BlackmagicRawAPI.h" ]; then
            SDK_INCLUDE="$candidate"
            break
        fi
    done
fi

if [ -z "$SDK_INCLUDE" ] || [ ! -f "$SDK_INCLUDE/BlackmagicRawAPI.h" ]; then
    echo "Couldn't find the Blackmagic RAW SDK's Include directory." >&2
    echo "Install the free Blackmagic RAW SDK, or pass its Mac/Include path explicitly:" >&2
    echo "  BRAW_SDK_INCLUDE=/path/to/Blackmagic\\ RAW\\ SDK/Mac/Include ./build.sh" >&2
    exit 1
fi

echo "Using SDK headers from: $SDK_INCLUDE"

clang++ \
    -std=gnu++17 \
    -stdlib=libc++ \
    -fobjc-arc \
    -mmacosx-version-min=10.15 \
    -I "$SDK_INCLUDE" \
    -framework Foundation \
    -framework AVFoundation \
    -framework CoreMedia \
    -framework CoreVideo \
    -framework CoreAudio \
    -framework CoreFoundation \
    -framework Accelerate \
    -o "$OUTPUT" \
    "$SCRIPT_DIR/braw_proxy_tool.mm" \
    "$SDK_INCLUDE/BlackmagicRawAPIDispatch.cpp"

chmod +x "$OUTPUT"
echo "Built $OUTPUT"
