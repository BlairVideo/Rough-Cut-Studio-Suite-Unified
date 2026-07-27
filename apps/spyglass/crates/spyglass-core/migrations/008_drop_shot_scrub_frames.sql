-- Removes the hover-scrub preview column added in 006: the UI no longer
-- cycles between pre-extracted frames on mouse position, just shows the
-- static keyframe, so this count has nothing left to read it.
ALTER TABLE shots DROP COLUMN scrub_frame_count;
