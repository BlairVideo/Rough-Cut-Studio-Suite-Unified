-- Finder-style hover-scrub preview (replaces playing the raw source file
-- live in the UI, which glitched on long-GOP camera-native footage
-- regardless of file size): a handful of low-res frames sampled across the
-- shot, generated once at index time, cycled by mouse position instead of
-- ever seeking a live <video> element. 0 means no scrub frames were
-- generated for this shot (e.g. it predates this feature, or was too short
-- to bother sampling) -- the UI falls back to the single static keyframe.
ALTER TABLE shots ADD COLUMN scrub_frame_count INTEGER NOT NULL DEFAULT 0;
