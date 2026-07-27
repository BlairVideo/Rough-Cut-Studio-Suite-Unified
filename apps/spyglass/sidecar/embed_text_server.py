#!/usr/bin/env python3
"""Persistent CLIP text-embedding server for interactive search-query
latency (Section 12). Spawning a fresh `analyze_clip.py`-style process per
search would reload the CLIP checkpoint on every keystroke's worth of
search -- multiple seconds of dead time before a query even runs. This
process loads the model once and stays alive, answering one query at a
time over a line-delimited JSON protocol on stdin/stdout.

Protocol: one JSON object per line.
    request:  {"text": "mascot cheering at a football game"}
    response: {"embedding": [0.0123, -0.045, ...]}   (512 floats, L2-normalized)
    on error: {"error": "message"}

Uses the exact same model/preprocessing as `analyze_clip.py`'s
`embed_text` (imported directly, not reimplemented) so query-time and
index-time embeddings always land in the same joint space.

Prints "ready" to stderr once the model has finished loading -- the
caller should wait for that line before sending the first request.
"""
import json
import sys

from analyze_clip import embed_text
from parent_watchdog import exit_if_parent_dies


def main() -> int:
    # This process outlives any single request and stays resident with a
    # loaded CLIP checkpoint for as long as the app runs -- exactly the
    # kind of long-lived child that must not survive an unclean exit of
    # its Rust host (force-quit, crash, SIGKILL).
    exit_if_parent_dies()

    # Trigger model load eagerly rather than on first request, so the
    # caller's first real query isn't the one that pays the load latency.
    embed_text("warmup")
    print("ready", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            text = request["text"]
            embedding = embed_text(text)
            print(json.dumps({"embedding": embedding}), flush=True)
        except Exception as exc:  # noqa: BLE001 -- one bad request must not kill the server
            print(json.dumps({"error": str(exc)}), flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
