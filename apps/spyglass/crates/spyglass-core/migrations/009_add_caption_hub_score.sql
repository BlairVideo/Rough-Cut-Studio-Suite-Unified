-- Confirmed live: some VLM captions' text embeddings are "hubs" in CLIP's
-- text-embedding space -- structurally similar to almost any query
-- regardless of actual content (one caption averaged 0.648 cosine
-- similarity across 20 completely unrelated test queries, versus 0.40 for
-- an ordinary caption). This stores each shot's caption's own baseline
-- similarity against a fixed, diverse anchor-phrase battery, computed once
-- at gap-fill time; search subtracts it from a caption's raw similarity to
-- a real query so the caption's structural closeness to *everything*
-- cancels out, leaving only what's actually specific to that query.
ALTER TABLE shots ADD COLUMN caption_hub_score REAL;
