"""
cardeater_verify.py — BLAKE3-based source/destination integrity verification.

Python port of Card Eater's own verify.rs. Consumed by cardeater_copy.py's
per-destination verify-worker pool (verification overlaps subsequent file
copies rather than blocking them). Uses the `blake3` PyPI package (Rust
bindings, so hashing speed matches the original Tauri app's).
"""

import blake3

# 1MB streaming read buffer -- large files are hashed incrementally so a
# multi-GB source/destination pair never sits fully in memory.
HASH_BUF_SIZE = 1024 * 1024


def hash_file(path):
    """Streams `path` through a BLAKE3 hasher and returns its hex digest."""
    hasher = blake3.blake3()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(HASH_BUF_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
    except OSError as e:
        raise OSError(f"failed to read file while hashing: {e}") from e
    return hasher.hexdigest()


def verify_pair(source, dest):
    """Hashes both `source` and `dest` and reports whether they match.
    Returns {"hash_source", "hash_dest", "matched"}."""
    hash_source = hash_file(source)
    hash_dest = hash_file(dest)
    return {
        "hash_source": hash_source,
        "hash_dest": hash_dest,
        "matched": hash_source == hash_dest,
    }
