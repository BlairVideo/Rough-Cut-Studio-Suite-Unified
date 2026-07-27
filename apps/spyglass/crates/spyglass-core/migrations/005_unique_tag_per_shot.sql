-- Backs the tag-correction UI (Section 13): adding a tag that already
-- exists on a shot should be a no-op, not a duplicate row, whether it
-- came from the VLM pass or a human correction.
CREATE UNIQUE INDEX idx_tags_shot_id_label ON tags(shot_id, label);
