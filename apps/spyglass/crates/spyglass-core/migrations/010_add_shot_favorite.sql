-- Clip favoriting: a quick "come back to this shot" marker, distinct from
-- the pool tray (which is for staging an export selection, not bookmarking).
ALTER TABLE shots ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_shots_is_favorite ON shots(is_favorite) WHERE is_favorite = 1;
