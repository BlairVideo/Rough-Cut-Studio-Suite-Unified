-- The VLM gap-fill pass (Section 6 step 5) produces a caption per shot,
-- which up to now was only ever embedded (via CLIP's text encoder) and
-- never itself persisted as readable text. Storing it directly on the
-- shot row -- alongside technical_quality_score/energy_score, which are
-- similarly denormalized there for convenience -- is what lets search
-- results show *why* a shot matched, and lets a user sanity-check an
-- auto-generated tag against the caption that produced it.
ALTER TABLE shots ADD COLUMN caption TEXT;
