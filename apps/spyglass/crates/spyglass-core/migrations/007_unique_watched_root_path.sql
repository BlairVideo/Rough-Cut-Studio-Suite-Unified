-- Prevents a removed root from ever accumulating a duplicate row: without
-- this, re-adding the exact same path after removal created a second,
-- separate `watched_roots` row (the first stayed 'removed' forever, dead
-- weight in the list) instead of reactivating the original.
CREATE UNIQUE INDEX idx_watched_roots_path ON watched_roots(path);
