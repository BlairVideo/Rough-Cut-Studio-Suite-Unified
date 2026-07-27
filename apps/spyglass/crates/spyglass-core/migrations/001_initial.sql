PRAGMA foreign_keys = ON;

-- One row per physical media file. `checksum` is Card Eater's own BLAKE3
-- `hash_source` when the file came through it, or freshly computed by
-- Spyglass (same algorithm) otherwise -- see the Card Eater adapter.
CREATE TABLE clips (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL UNIQUE,
    source_app    TEXT NOT NULL CHECK (source_app IN ('card_eater', 'spyglass_scan')),
    checksum      TEXT,
    size_bytes    INTEGER,
    duration_sec  REAL,
    ingested_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX idx_clips_checksum ON clips(checksum);

-- Sub-segments within a clip (scene-cut level) -- the actual unit search
-- returns. technical_quality_score/energy_score are nullable and only
-- populated when a B-Roll Analyzer cache overlaps the shot's time range.
CREATE TABLE shots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id                 INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    start_tc                REAL NOT NULL,
    end_tc                  REAL NOT NULL,
    keyframe_path           TEXT,
    technical_quality_score REAL,
    energy_score            REAL
);
CREATE INDEX idx_shots_clip_id ON shots(clip_id);

-- Sourced from the Transcriber's .ivt-cache.json sidecars. avg_logprob/
-- no_speech_prob are Whisper's own per-segment confidence scores, carried
-- through unchanged.
CREATE TABLE transcript_segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id        INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    start_tc       REAL NOT NULL,
    end_tc         REAL NOT NULL,
    speaker        TEXT,
    text           TEXT NOT NULL,
    avg_logprob    REAL,
    no_speech_prob REAL
);
CREATE INDEX idx_transcript_segments_clip_id ON transcript_segments(clip_id);

-- FTS5 keyword index over transcript text, kept in sync via triggers so
-- callers never have to remember to update it separately.
CREATE VIRTUAL TABLE transcript_segments_fts USING fts5(
    text,
    content = 'transcript_segments',
    content_rowid = 'id'
);

CREATE TRIGGER transcript_segments_ai AFTER INSERT ON transcript_segments BEGIN
    INSERT INTO transcript_segments_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER transcript_segments_ad AFTER DELETE ON transcript_segments BEGIN
    INSERT INTO transcript_segments_fts(transcript_segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER transcript_segments_au AFTER UPDATE ON transcript_segments BEGIN
    INSERT INTO transcript_segments_fts(transcript_segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO transcript_segments_fts(rowid, text) VALUES (new.id, new.text);
END;

-- Entirely Spyglass's own doing -- human corrections or the gap-fill VLM
-- pass. None of the three upstream apps produce subject-matter tags.
CREATE TABLE tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id    INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('human', 'spyglass_vlm')),
    confidence REAL
);
CREATE INDEX idx_tags_shot_id ON tags(shot_id);
CREATE INDEX idx_tags_label ON tags(label);

-- Visual/caption embedding vectors. Stored as plain BLOBs for Phase 1;
-- Phase 3 wires this through the sqlite-vec extension for NN search
-- without changing this table's shape.
CREATE TABLE embeddings (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL CHECK (kind IN ('visual', 'caption')),
    vector  BLOB NOT NULL
);
CREATE INDEX idx_embeddings_shot_id ON embeddings(shot_id);

-- User-created shortlists / the pool tray's backing store (Section 13/14).
-- shot_ids is an ordered JSON array of integers.
CREATE TABLE collections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    shot_ids   TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- The allowlist of drives/folders Spyglass is permitted to scan and index.
-- Every disk access the app makes is checked against this table at the I/O
-- layer -- see the watched-root scanner.
CREATE TABLE watched_roots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    label                 TEXT NOT NULL,
    path                  TEXT NOT NULL,
    volume_id             TEXT,
    access_level          TEXT NOT NULL DEFAULT 'active' CHECK (access_level IN ('active', 'paused', 'removed')),
    approved_by           TEXT,
    approved_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_scanned_at       TEXT,
    sidecar_cache_enabled INTEGER NOT NULL DEFAULT 0
);
