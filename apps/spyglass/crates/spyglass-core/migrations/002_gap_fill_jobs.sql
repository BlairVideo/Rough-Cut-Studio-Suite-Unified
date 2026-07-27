-- The persisted background job queue Section 7 requires: one row per clip
-- that still needs shot detection/keyframes/embeddings. A clip either needs
-- gap-fill or it doesn't, so one job row per clip is enough -- retry state
-- (attempts/last_error) lives on that same row rather than a separate
-- per-attempt log.
CREATE TABLE gap_fill_jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id    INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'done', 'failed', 'awaiting_reconnect')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    queued_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE UNIQUE INDEX idx_gap_fill_jobs_clip_id ON gap_fill_jobs(clip_id);
CREATE INDEX idx_gap_fill_jobs_status ON gap_fill_jobs(status);
