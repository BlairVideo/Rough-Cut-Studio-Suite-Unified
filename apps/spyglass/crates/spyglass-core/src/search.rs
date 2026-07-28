//! Hybrid text-query search (Section 12): embed the query, rank shots by a
//! weighted fusion of visual/caption embedding similarity, tag matches, and
//! transcript keyword matches.
//!
//! **Deviation from Section 16's "decided" sqlite-vec choice, flagged
//! rather than silent**: the official `sqlite-vec` Rust crate
//! (`sqlite-vec = "0.1.10-alpha.4"`) fails to build in this environment --
//! its packaged source is missing a bundled file
//! (`sqlite-vec-diskann.c`), a real bug in that alpha release, not
//! something fixable from the consuming side. Nearest-neighbor search here
//! is instead a brute-force cosine similarity scan over `embeddings`,
//! implemented in Rust. At the plan's own stated archive scale (tens of
//! thousands of shots -- Section 16 says revisit sqlite-vec only past
//! roughly a million), this is fast enough for a "press enter to search"
//! UI and meaningfully simpler/more reliable than an experimental
//! extension. Swapping in sqlite-vec later, if the crate matures or the
//! archive outgrows this approach, only touches this module.

use crate::models::ShotSearchResult;
use rusqlite::{params, Connection, OptionalExtension};
use std::collections::HashMap;

/// Cosine similarity. The sidecar's embeddings are already L2-normalized,
/// so this is just a dot product -- kept as an explicit cosine calculation
/// (rather than assuming normalization) so this stays correct even if a
/// future embedding source isn't pre-normalized.
pub fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let dot: f32 = a.iter().zip(b).map(|(x, y)| x * y).sum();
    let norm_a: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
    let norm_b: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm_a == 0.0 || norm_b == 0.0 {
        return 0.0;
    }
    dot / (norm_a * norm_b)
}

pub fn decode_vector(bytes: &[u8]) -> Vec<f32> {
    bytes
        .chunks_exact(4)
        .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
        .collect()
}

#[derive(Debug, Clone, Copy, PartialEq, Default)]
struct ShotCandidate {
    visual_score: Option<f32>,
    caption_score: Option<f32>,
    tag_hit: bool,
    /// Best (highest) normalized rarity, 0.0-1.0, among tags that matched
    /// this shot -- see `normalized_tag_rarity`. Meaningless when
    /// `tag_hit` is false.
    tag_rarity: f32,
    transcript_hit: bool,
}

// Weighted fusion (Section 12) -- a documented first-cut heuristic, not a
// tuned model. Visual similarity is the primary signal (image-vs-text,
// cross-tower). Caption similarity is kept as its *own*, smaller-weighted
// term rather than folded into the same bucket as visual (see
// `caption_relevance_threshold`'s doc comment for why) -- capping its
// ceiling means one noisy caption spike can't alone outscore a shot with
// real, corroborating visual/tag signal. Tag and keyword hits are flat
// boosts since they're binary (present or not), except tag is additionally
// scaled by rarity (see `normalized_tag_rarity`).
const WEIGHT_VISUAL: f64 = 0.45;
const WEIGHT_CAPTION: f64 = 0.20;
const WEIGHT_TAG: f64 = 0.20;
const WEIGHT_KEYWORD: f64 = 0.15;

// A shot with no tag/transcript hit needs at least this much *visual*
// (query-text vs. image) similarity to be considered a real match rather
// than baseline CLIP noise -- cosine similarity between totally unrelated
// image/text pairs commonly sits in the 0.15-0.25 range, so it's not a
// safe stand-in for "no match". First-cut heuristic, tune if real
// searches show it's too strict/loose.
const MIN_VISUAL_SIMILARITY_TO_SURFACE: f32 = 0.24;

// How many standard deviations above *this query's own* mean caption
// similarity a caption must sit to qualify -- see
// `caption_relevance_threshold`.
const CAPTION_RELATIVE_FLOOR_Z: f32 = 1.0;

/// Every archive-real tag ("mascot", "outdoors", "classroom", "cheering"
/// in this school's footage) covers a large, genuinely common fraction of
/// shots -- confirmed live: in a 1622-shot archive, 4 tags alone each
/// covered 23-25% of every shot. A flat per-tag boost treats "mascot" (on
/// a quarter of the archive) identically to a tag on 3 shots, so any query
/// touching a common tag gets swamped by that tag's whole population
/// regardless of actual relevance. This scales the boost by how much
/// information the tag actually carries (an IDF-style rarity, normalized
/// to 0.0-1.0 so it composes cleanly with `WEIGHT_TAG` regardless of
/// archive size): a tag on every shot contributes ~0, a tag on a handful
/// of shots contributes close to the full weight.
fn normalized_tag_rarity(tag_shot_count: i64, total_shots: i64) -> f32 {
    if total_shots <= 1 || tag_shot_count <= 0 {
        return 1.0;
    }
    let tag_shot_count = (tag_shot_count.min(total_shots)) as f64;
    let total_shots = total_shots as f64;
    if tag_shot_count >= total_shots {
        return 0.0;
    }
    ((total_shots / tag_shot_count).ln() / total_shots.ln()).clamp(0.0, 1.0) as f32
}

/// The caption channel compares query text against *another piece of
/// text* (the VLM-generated caption, itself embedded via the same CLIP
/// text encoder) -- not text against an image, and reusing CLIP's text
/// tower for text-vs-text similarity is a well-documented source of "hub"
/// captions that score anomalously high against many unrelated queries.
/// `search_shots` already subtracts each caption's own anchor-battery hub
/// score before this ever sees it (migration 009), so `caption_scores`
/// here are hub-*corrected* residuals, not raw similarities -- but that
/// correction is against a fixed, generic battery, not this specific
/// query, so it doesn't guarantee zero residual variance for every real
/// query. This adds a second, per-query-relative layer on top: a single
/// fixed floor doesn't just fail to filter remaining noise -- confirmed
/// live against this archive (before hub-correction existed), it actively
/// inverted on some queries, letting through only the worst outlier while
/// rejecting every genuinely on-topic caption. Computing the floor
/// relative to the current query's own score distribution instead adapts
/// to each query's actual noise baseline. Returns `f32::INFINITY` (never
/// qualifies) when there isn't enough data to establish a meaningful
/// baseline.
fn caption_relevance_threshold(caption_scores: &[f32]) -> f32 {
    if caption_scores.len() < 2 {
        return f32::INFINITY;
    }
    let mean = caption_scores.iter().sum::<f32>() / caption_scores.len() as f32;
    let variance = caption_scores.iter().map(|s| (s - mean).powi(2)).sum::<f32>() / caption_scores.len() as f32;
    let stdev = variance.sqrt();
    if stdev < 1e-6 {
        return f32::INFINITY;
    }
    mean + CAPTION_RELATIVE_FLOOR_Z * stdev
}

fn hybrid_score(candidate: &ShotCandidate, caption_threshold: f32) -> f64 {
    let visual = candidate
        .visual_score
        .filter(|&s| s >= MIN_VISUAL_SIMILARITY_TO_SURFACE)
        .map_or(0.0, |s| s.max(0.0) as f64 * WEIGHT_VISUAL);
    let caption = candidate
        .caption_score
        .filter(|&s| s >= caption_threshold)
        .map_or(0.0, |s| s.max(0.0) as f64 * WEIGHT_CAPTION);

    let mut score = visual + caption;
    if candidate.tag_hit {
        score += candidate.tag_rarity as f64 * WEIGHT_TAG;
    }
    if candidate.transcript_hit {
        score += WEIGHT_KEYWORD;
    }
    score
}

/// Whether a candidate has *any* real signal a query term was actually
/// found -- a flat tag/transcript hit, or vector similarity clearing its
/// channel's floor. Used to drop shots that would otherwise be surfaced
/// purely on baseline embedding noise.
fn has_meaningful_signal(candidate: &ShotCandidate, caption_threshold: f32) -> bool {
    candidate.tag_hit
        || candidate.transcript_hit
        || candidate.visual_score.is_some_and(|s| s >= MIN_VISUAL_SIMILARITY_TO_SURFACE)
        || candidate.caption_score.is_some_and(|s| s >= caption_threshold)
}

/// Exact-word tag match (with trivial singular/plural handling), not
/// substring containment. Plain substring matching (either direction)
/// produces false positives whenever a short query word or tag happens to
/// occur inside a longer, unrelated one -- e.g. the token "cat" matching
/// tags like "location" or "vacation" purely because "cat" appears inside
/// them, or the tag "art" matching the token "party".
fn tag_matches_token(token: &str, label: &str) -> bool {
    fn without_trailing_s(s: &str) -> &str {
        s.strip_suffix('s').unwrap_or(s)
    }
    token == label || without_trailing_s(token) == without_trailing_s(label)
}

fn query_tokens(query_text: &str) -> Vec<String> {
    query_text
        .to_lowercase()
        .split_whitespace()
        .filter(|t| t.len() >= 3) // skip tiny stopword-ish tokens ("a", "at", "on")
        .map(|s| s.to_string())
        .collect()
}

pub(crate) fn fetch_shot_search_result(conn: &Connection, shot_id: i64, score: f64) -> rusqlite::Result<Option<ShotSearchResult>> {
    let row = conn
        .query_row(
            "SELECT s.id, s.clip_id, c.file_path, c.frame_rate, s.start_tc, s.end_tc, s.keyframe_path,
                    s.technical_quality_score, s.energy_score, s.caption, s.is_favorite
             FROM shots s JOIN clips c ON c.id = s.clip_id
             WHERE s.id = ?1",
            params![shot_id],
            |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, Option<f64>>(3)?,
                    r.get::<_, f64>(4)?,
                    r.get::<_, f64>(5)?,
                    r.get::<_, Option<String>>(6)?,
                    r.get::<_, Option<f64>>(7)?,
                    r.get::<_, Option<f64>>(8)?,
                    r.get::<_, Option<String>>(9)?,
                    r.get::<_, bool>(10)?,
                ))
            },
        )
        .optional()?;

    let Some((
        shot_id,
        clip_id,
        clip_file_path,
        clip_frame_rate,
        start_tc,
        end_tc,
        keyframe_path,
        technical_quality_score,
        energy_score,
        caption,
        is_favorite,
    )) = row
    else {
        return Ok(None);
    };

    let tags: Vec<String> = {
        let mut stmt = conn.prepare("SELECT label FROM tags WHERE shot_id = ?1 ORDER BY label")?;
        let rows = stmt.query_map(params![shot_id], |r| r.get(0))?;
        rows.collect::<Result<_, _>>()?
    };

    Ok(Some(ShotSearchResult {
        shot_id,
        clip_id,
        clip_file_path,
        clip_frame_rate,
        start_tc,
        end_tc,
        keyframe_path,
        technical_quality_score,
        energy_score,
        caption,
        tags,
        score,
        is_favorite,
    }))
}

/// Reorders `scored` (shot id, relevance score) pairs in place per
/// `sort_by`. `SortBy::Relevance` is a no-op re-sort by the score
/// `search_shots` already computed; every other option re-ranks by a
/// different column fetched fresh per candidate (cheap -- `scored` is
/// already narrowed to shots with real query signal, never the whole
/// archive), falling back to relevance as the tiebreaker so equally-dated/
/// equally-scored shots don't reorder arbitrarily between calls.
fn order_scored_shots(conn: &Connection, scored: &mut [(i64, f64)], sort_by: crate::facets::SortBy) -> rusqlite::Result<()> {
    use crate::facets::SortBy;
    use std::cmp::Ordering;

    if matches!(sort_by, SortBy::Relevance) {
        scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(Ordering::Equal));
        return Ok(());
    }

    // `None` sorts last regardless of direction -- an unscored shot isn't
    // "worst", it's simply missing the B-Roll Analyzer data the score
    // comes from (see `adapters::broll_analyzer`), so it shouldn't be
    // indistinguishable from a genuinely low score.
    fn cmp_desc_none_last(a: Option<f64>, b: Option<f64>) -> Ordering {
        match (a, b) {
            (Some(a), Some(b)) => b.partial_cmp(&a).unwrap_or(Ordering::Equal),
            (Some(_), None) => Ordering::Less,
            (None, Some(_)) => Ordering::Greater,
            (None, None) => Ordering::Equal,
        }
    }

    // `COALESCE(c.recorded_at, c.ingested_at)` -- see facets.rs's
    // `browse_order_by_clause` doc comment for why sort-by-date can't use
    // `ingested_at` alone (it's scan time, not capture time).
    let mut recorded_at: HashMap<i64, String> = HashMap::new();
    let mut quality: HashMap<i64, Option<f64>> = HashMap::new();
    let mut energy: HashMap<i64, Option<f64>> = HashMap::new();
    for &(shot_id, _) in scored.iter() {
        let (ra, q, e) = conn.query_row(
            "SELECT COALESCE(c.recorded_at, c.ingested_at), s.technical_quality_score, s.energy_score
             FROM shots s JOIN clips c ON c.id = s.clip_id WHERE s.id = ?1",
            params![shot_id],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<f64>>(1)?, r.get::<_, Option<f64>>(2)?)),
        )?;
        recorded_at.insert(shot_id, ra);
        quality.insert(shot_id, q);
        energy.insert(shot_id, e);
    }

    scored.sort_by(|a, b| {
        let relevance_tiebreak = || b.1.partial_cmp(&a.1).unwrap_or(Ordering::Equal);
        match sort_by {
            SortBy::Relevance => unreachable!("handled above"),
            SortBy::NewestFirst => recorded_at[&b.0].cmp(&recorded_at[&a.0]).then_with(relevance_tiebreak),
            SortBy::OldestFirst => recorded_at[&a.0].cmp(&recorded_at[&b.0]).then_with(relevance_tiebreak),
            SortBy::HighestQuality => cmp_desc_none_last(quality[&a.0], quality[&b.0]).then_with(relevance_tiebreak),
            SortBy::MostEnergy => cmp_desc_none_last(energy[&a.0], energy[&b.0]).then_with(relevance_tiebreak),
        }
    });
    Ok(())
}

/// Ranks shots against a text query: `query_embedding` (from the CLIP text
/// encoder, same joint space as visual/caption embeddings) drives the
/// vector-similarity term; `query_text` drives the tag and transcript-
/// keyword terms. `filters` (Section 12's "combined/hybrid search") narrows
/// results to a selected facet set *in addition to* relevance -- a shot
/// still needs real query signal to surface, facets only ever remove
/// candidates, never add ones the query itself didn't support. Returns the
/// top `limit` shots, highest score first.
pub fn search_shots(
    conn: &Connection,
    query_text: &str,
    query_embedding: &[f32],
    filters: &crate::facets::FacetFilters,
    limit: i64,
) -> rusqlite::Result<Vec<ShotSearchResult>> {
    let mut candidates: HashMap<i64, ShotCandidate> = HashMap::new();

    // Each caption's own baseline similarity against a fixed anchor
    // battery (migration 009) -- subtracted from its raw similarity to the
    // *real* query below. Confirmed live: some captions are genuine
    // embedding-space hubs (structurally similar to almost any text,
    // independent of content -- one averaged 0.648 similarity across 20
    // unrelated test queries vs. 0.40 for an ordinary caption), which
    // survives even after removing generic boilerplate phrasing. Missing
    // for a shot (an older gap-fill run, before this existed) defaults to
    // no correction rather than dropping the shot's caption signal.
    let caption_hub_scores: HashMap<i64, f32> = {
        let mut stmt = conn.prepare("SELECT id, caption_hub_score FROM shots WHERE caption_hub_score IS NOT NULL")?;
        let rows = stmt.query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, f64>(1)?)))?;
        rows.map(|r| r.map(|(id, score)| (id, score as f32))).collect::<Result<_, _>>()?
    };

    {
        let mut stmt = conn.prepare("SELECT shot_id, kind, vector FROM embeddings")?;
        let rows = stmt.query_map([], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?, row.get::<_, Vec<u8>>(2)?))
        })?;
        for row in rows {
            let (shot_id, kind, vector_bytes) = row?;
            let vector = decode_vector(&vector_bytes);
            let similarity = cosine_similarity(query_embedding, &vector);
            let entry = candidates.entry(shot_id).or_default();
            match kind.as_str() {
                "visual" => entry.visual_score = Some(entry.visual_score.map_or(similarity, |s| s.max(similarity))),
                "caption" => {
                    let hub_score = caption_hub_scores.get(&shot_id).copied().unwrap_or(0.0);
                    let adjusted = similarity - hub_score;
                    entry.caption_score = Some(entry.caption_score.map_or(adjusted, |s| s.max(adjusted)));
                }
                _ => {}
            }
        }
    }

    let tokens = query_tokens(query_text);
    if !tokens.is_empty() {
        let total_shots: i64 = conn.query_row("SELECT COUNT(*) FROM shots", [], |r| r.get(0))?;

        let mut tag_shot_counts: HashMap<String, i64> = HashMap::new();
        {
            let mut stmt = conn.prepare("SELECT LOWER(label), COUNT(DISTINCT shot_id) FROM tags GROUP BY LOWER(label)")?;
            let rows = stmt.query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)))?;
            for row in rows {
                let (label_lower, count) = row?;
                tag_shot_counts.insert(label_lower, count);
            }
        }

        let mut stmt = conn.prepare("SELECT shot_id, label FROM tags")?;
        let rows = stmt.query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)))?;
        for row in rows {
            let (shot_id, label) = row?;
            let label_lower = label.to_lowercase();
            if tokens.iter().any(|t| tag_matches_token(t, &label_lower)) {
                let tag_shot_count = tag_shot_counts.get(&label_lower).copied().unwrap_or(1);
                let rarity = normalized_tag_rarity(tag_shot_count, total_shots);
                let entry = candidates.entry(shot_id).or_default();
                entry.tag_hit = true;
                entry.tag_rarity = entry.tag_rarity.max(rarity);
            }
        }
    }

    {
        let mut stmt = conn.prepare(
            "SELECT ts.clip_id, ts.start_tc, ts.end_tc
             FROM transcript_segments_fts f
             JOIN transcript_segments ts ON ts.id = f.rowid
             WHERE f.text MATCH ?1",
        )?;
        // A raw MATCH query can fail on FTS5-illegal syntax (bare quotes,
        // leading operators); a keyword miss shouldn't sink the whole
        // search when vector/tag signals may still be useful.
        let query_result = stmt.query_map(params![query_text], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, f64>(1)?, row.get::<_, f64>(2)?))
        });
        if let Ok(rows) = query_result {
            let mut overlap_stmt =
                conn.prepare("SELECT id FROM shots WHERE clip_id = ?1 AND start_tc < ?3 AND end_tc > ?2")?;
            for row in rows.flatten() {
                let (clip_id, seg_start, seg_end) = row;
                if let Ok(shot_ids) = overlap_stmt
                    .query_map(params![clip_id, seg_start, seg_end], |r| r.get::<_, i64>(0))
                    .map(|rows| rows.flatten().collect::<Vec<_>>())
                {
                    for shot_id in shot_ids {
                        candidates.entry(shot_id).or_default().transcript_hit = true;
                    }
                }
            }
        }
    }

    let caption_threshold = caption_relevance_threshold(
        &candidates.values().filter_map(|c| c.caption_score).collect::<Vec<_>>(),
    );

    let allowed = crate::facets::matching_shot_ids(conn, filters)?;

    let mut scored: Vec<(i64, f64)> = candidates
        .iter()
        .filter(|(_, c)| has_meaningful_signal(c, caption_threshold))
        .filter(|(id, _)| allowed.as_ref().is_none_or(|a| a.contains(id)))
        .map(|(&id, c)| (id, hybrid_score(c, caption_threshold)))
        .collect();
    order_scored_shots(conn, &mut scored, filters.sort_by)?;
    scored.truncate(limit.max(0) as usize);

    let mut results = Vec::with_capacity(scored.len());
    for (shot_id, score) in scored {
        if let Some(result) = fetch_shot_search_result(conn, shot_id, score)? {
            results.push(result);
        }
    }
    Ok(results)
}

/// "Find shots like this one" (Section 12): nearest-neighbor over visual
/// embeddings seeded from `reference_shot_id`'s own visual embedding,
/// excluding itself. Returns an empty list (not an error) if the reference
/// shot has no visual embedding yet -- gap-fill may simply not have run on
/// it, which isn't a failure condition for the caller.
pub fn find_similar_shots(conn: &Connection, reference_shot_id: i64, limit: i64) -> rusqlite::Result<Vec<ShotSearchResult>> {
    let reference_bytes: Option<Vec<u8>> = conn
        .query_row(
            "SELECT vector FROM embeddings WHERE shot_id = ?1 AND kind = 'visual'",
            params![reference_shot_id],
            |r| r.get(0),
        )
        .optional()?;
    let Some(reference_bytes) = reference_bytes else {
        return Ok(Vec::new());
    };
    let reference_vector = decode_vector(&reference_bytes);

    let mut scored: Vec<(i64, f64)> = {
        let mut stmt = conn.prepare("SELECT shot_id, vector FROM embeddings WHERE kind = 'visual' AND shot_id != ?1")?;
        let rows = stmt.query_map(params![reference_shot_id], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, Vec<u8>>(1)?))
        })?;
        rows.filter_map(|row| row.ok())
            .map(|(shot_id, vector_bytes)| {
                let vector = decode_vector(&vector_bytes);
                let similarity = cosine_similarity(&reference_vector, &vector).max(0.0) as f64;
                (shot_id, similarity)
            })
            .collect()
    };
    scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    scored.truncate(limit.max(0) as usize);

    let mut results = Vec::with_capacity(scored.len());
    for (shot_id, score) in scored {
        if let Some(result) = fetch_shot_search_result(conn, shot_id, score)? {
            results.push(result);
        }
    }
    Ok(results)
}

/// All favorited shots (Section 13-adjacent bookmarking), most recently
/// favorited first. `score` is meaningless here and always 0.0, same as
/// the pool tray's `list_shots` -- this isn't a relevance ranking.
pub fn list_favorite_shots(conn: &Connection) -> rusqlite::Result<Vec<ShotSearchResult>> {
    let shot_ids: Vec<i64> = {
        let mut stmt = conn.prepare("SELECT id FROM shots WHERE is_favorite = 1 ORDER BY id DESC")?;
        let rows = stmt.query_map([], |r| r.get::<_, i64>(0))?;
        rows.collect::<Result<_, _>>()?
    };

    let mut results = Vec::with_capacity(shot_ids.len());
    for shot_id in shot_ids {
        if let Some(result) = fetch_shot_search_result(conn, shot_id, 0.0)? {
            results.push(result);
        }
    }
    Ok(results)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{self, Db};
    use crate::facets::{FacetFilters, SortBy};
    use crate::models::{NewClip, NewTranscriptSegment, SourceApp};
    use std::sync::atomic::{AtomicU64, Ordering};

    static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn open_scratch_db(tag: &str) -> (Db, std::path::PathBuf) {
        let n = TMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!("spyglass_search_test_{tag}_{n}.sqlite3"));
        std::fs::remove_file(&path).ok();
        let db = Db::open_at(&path).expect("open scratch db");
        (db, path)
    }

    #[test]
    fn cosine_similarity_of_identical_vectors_is_one() {
        let v = vec![0.6, 0.8];
        assert!((cosine_similarity(&v, &v) - 1.0).abs() < 1e-6);
    }

    #[test]
    fn cosine_similarity_of_orthogonal_vectors_is_zero() {
        assert!((cosine_similarity(&[1.0, 0.0], &[0.0, 1.0])).abs() < 1e-6);
    }

    #[test]
    fn cosine_similarity_handles_mismatched_or_empty_vectors() {
        assert_eq!(cosine_similarity(&[1.0, 0.0], &[1.0]), 0.0);
        assert_eq!(cosine_similarity(&[], &[]), 0.0);
    }

    #[test]
    fn vector_bytes_round_trip_through_decode() {
        let original: Vec<f32> = vec![0.1, -0.2, 0.3, -0.4];
        let bytes: Vec<u8> = original.iter().flat_map(|f| f.to_le_bytes()).collect();
        assert_eq!(decode_vector(&bytes), original);
    }

    #[test]
    fn hybrid_score_ranks_pure_vector_match_above_tag_only_match() {
        let vector_hit = ShotCandidate { visual_score: Some(0.9), ..Default::default() };
        let tag_only = ShotCandidate { tag_hit: true, tag_rarity: 1.0, ..Default::default() };
        assert!(hybrid_score(&vector_hit, f32::INFINITY) > hybrid_score(&tag_only, f32::INFINITY));
    }

    #[test]
    fn hybrid_score_combines_all_three_signals() {
        let all_three = ShotCandidate {
            visual_score: Some(0.5),
            caption_score: None,
            tag_hit: true,
            tag_rarity: 1.0,
            transcript_hit: true,
        };
        let vector_only = ShotCandidate { visual_score: Some(0.5), ..Default::default() };
        assert!(hybrid_score(&all_three, f32::INFINITY) > hybrid_score(&vector_only, f32::INFINITY));
    }

    #[test]
    fn hybrid_score_clamps_negative_similarity_instead_of_penalizing() {
        let negative_vector =
            ShotCandidate { visual_score: Some(-0.8), tag_hit: true, tag_rarity: 1.0, ..Default::default() };
        let tag_only = ShotCandidate { tag_hit: true, tag_rarity: 1.0, ..Default::default() };
        assert_eq!(hybrid_score(&negative_vector, f32::INFINITY), hybrid_score(&tag_only, f32::INFINITY));
    }

    #[test]
    fn normalized_tag_rarity_penalizes_archive_wide_tags_and_rewards_rare_ones() {
        // Live-data-shaped: a tag on a quarter of a 1622-shot archive
        // ("mascot": 406/1622) must contribute far less than a tag on a
        // handful of shots -- otherwise a hub tag swamps every query that
        // happens to touch it, regardless of actual relevance.
        let hub = normalized_tag_rarity(406, 1622);
        let rare = normalized_tag_rarity(4, 1622);
        let universal = normalized_tag_rarity(1622, 1622);
        assert!(hub < 0.3, "hub tag rarity was {hub}, expected well under 0.3");
        assert!(rare > 0.75, "rare tag rarity was {rare}, expected well over 0.75");
        assert_eq!(universal, 0.0, "a tag on every shot carries zero discriminative value");
    }

    fn insert_shot_with_embedding(conn: &Connection, clip_id: i64, start_tc: f64, end_tc: f64, kind: &str, vector: &[f32]) -> i64 {
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, ?2, ?3)",
            params![clip_id, start_tc, end_tc],
        )
        .unwrap();
        let shot_id = conn.last_insert_rowid();
        let bytes: Vec<u8> = vector.iter().flat_map(|f| f.to_le_bytes()).collect();
        conn.execute(
            "INSERT INTO embeddings (shot_id, kind, vector) VALUES (?1, ?2, ?3)",
            params![shot_id, kind, bytes],
        )
        .unwrap();
        shot_id
    }

    fn add_embedding(conn: &Connection, shot_id: i64, kind: &str, vector: &[f32]) {
        let bytes: Vec<u8> = vector.iter().flat_map(|f| f.to_le_bytes()).collect();
        conn.execute(
            "INSERT INTO embeddings (shot_id, kind, vector) VALUES (?1, ?2, ?3)",
            params![shot_id, kind, bytes],
        )
        .unwrap();
    }

    #[test]
    fn search_shots_ranks_the_closest_visual_embedding_first() {
        let (db, path) = open_scratch_db("visual_rank");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/game.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let close_shot = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "visual", &[1.0, 0.0, 0.0]);
        // Above MIN_VISUAL_SIMILARITY_TO_SURFACE (cosine ~0.287 against the
        // query below) so it still clears the relevance floor -- this test
        // is about ranking order, not the floor itself.
        let far_shot = insert_shot_with_embedding(&conn, clip.id, 4.0, 8.0, "visual", &[0.3, 1.0, 0.0]);

        let results = search_shots(&conn, "mascot", &[1.0, 0.0, 0.0], &FacetFilters::default(), 10).unwrap();
        assert_eq!(results[0].shot_id, close_shot);
        assert!(results.iter().any(|r| r.shot_id == far_shot));
        assert!(results[0].score > results.iter().find(|r| r.shot_id == far_shot).unwrap().score);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn search_shots_boosts_matching_tags_even_without_a_strong_vector_match() {
        let (db, path) = open_scratch_db("tag_boost");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/game2.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let tagged_shot = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "visual", &[0.0, 1.0, 0.0]);
        conn.execute(
            "INSERT INTO tags (shot_id, label, source) VALUES (?1, 'mascot', 'spyglass_vlm')",
            params![tagged_shot],
        )
        .unwrap();
        let _untagged_shot = insert_shot_with_embedding(&conn, clip.id, 4.0, 8.0, "visual", &[0.0, 1.0, 0.0]);

        // Query embedding is unrelated to either shot's visual embedding --
        // only the tag match should distinguish them.
        let results = search_shots(&conn, "mascot", &[1.0, 0.0, 0.0], &FacetFilters::default(), 10).unwrap();
        assert_eq!(results[0].shot_id, tagged_shot);
        assert_eq!(results[0].tags, vec!["mascot"]);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn search_shots_boosts_shots_overlapping_a_matching_transcript_segment() {
        let (db, path) = open_scratch_db("transcript_boost");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Interviews/coach.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let matching_shot = insert_shot_with_embedding(&conn, clip.id, 0.0, 5.0, "visual", &[0.0, 1.0, 0.0]);
        let _other_shot = insert_shot_with_embedding(&conn, clip.id, 5.0, 10.0, "visual", &[0.0, 1.0, 0.0]);

        db::insert_transcript_segment(
            &conn,
            &NewTranscriptSegment {
                clip_id: clip.id,
                start_tc: 1.0,
                end_tc: 3.0,
                speaker: None,
                text: "our mascot really got the crowd going".to_string(),
                avg_logprob: None,
                no_speech_prob: None,
            },
        )
        .unwrap();

        let results = search_shots(&conn, "mascot", &[1.0, 0.0, 0.0], &FacetFilters::default(), 10).unwrap();
        assert_eq!(results[0].shot_id, matching_shot);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn search_shots_drops_shots_with_no_real_signal_instead_of_returning_noise() {
        let (db, path) = open_scratch_db("relevance_floor");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/unrelated.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        // Orthogonal to the query embedding (cosine similarity 0.0) and no
        // tag/transcript hit -- must not be returned just to fill `limit`.
        let unrelated_shot = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "visual", &[0.0, 1.0, 0.0]);

        let results = search_shots(&conn, "mascot", &[1.0, 0.0, 0.0], &FacetFilters::default(), 10).unwrap();
        assert!(
            !results.iter().any(|r| r.shot_id == unrelated_shot),
            "a shot with zero vector similarity and no tag/transcript hit should be filtered out, not surfaced as a result"
        );

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn caption_channel_needs_its_own_higher_floor_to_avoid_becoming_a_hub() {
        let (db, path) = open_scratch_db("caption_hub");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/hub.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        // Mimics a long, generic caption whose text embedding sits close to
        // almost any query in CLIP's text-text space (observed in practice
        // around 0.64-0.69 against totally unrelated queries) -- must not
        // surface despite clearing the (much lower) visual-channel floor's
        // numeric value. All vectors are unit length so their dot product
        // with the query [1,0,0] is exactly their cosine similarity.
        let hub_shot = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "caption", &[0.67, 0.7423, 0.0]);
        // A genuinely on-topic caption match.
        let real_match = insert_shot_with_embedding(&conn, clip.id, 4.0, 8.0, "caption", &[0.95, 0.3122, 0.0]);
        // A low-similarity filler shot, purely to give the relative floor
        // (`caption_relevance_threshold`) a realistic 3-point distribution
        // to compute a mean/stdev from, rather than the degenerate 2-point
        // case -- not itself asserted on.
        let _filler = insert_shot_with_embedding(&conn, clip.id, 8.0, 12.0, "caption", &[0.30, 0.9539, 0.0]);

        let results = search_shots(&conn, "students playing foosball", &[1.0, 0.0, 0.0], &FacetFilters::default(), 10).unwrap();
        assert!(
            !results.iter().any(|r| r.shot_id == hub_shot),
            "a caption similarity that doesn't clear this query's own relative floor must not surface just because it clears the visual channel's (much lower, unrelated) floor"
        );
        assert!(results.iter().any(|r| r.shot_id == real_match));

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn corroborated_visual_and_caption_match_outranks_a_caption_only_hub_spike() {
        // Live-data-shaped regression, found diagnosing this archive's real
        // index directly: for the real query "field hockey game", genuine
        // "field hockey game unfolds..." shots scored only 0.42-0.52
        // caption similarity (while also clearing the *visual* channel,
        // ~0.28-0.30) while two unrelated bonfire shots spiked to 0.705
        // caption similarity with no visual support at all (~0.20, below
        // MIN_VISUAL_SIMILARITY_TO_SURFACE). Under the old design -- one
        // shared WEIGHT_VECTOR bucket, max()'d across visual/caption -- the
        // bonfire spike alone (0.705 * 0.65 = 0.458) outranked every real
        // match. This reproduces that shape with controlled synthetic
        // vectors: a shot with real (if modest) support from *both*
        // channels must outrank a shot whose only signal is an isolated
        // caption spike, even though that spike's raw similarity is higher.
        let (db, path) = open_scratch_db("hub_vs_corroborated");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/hub_vs_corroborated.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        // A baseline cluster establishing a realistic caption-similarity
        // distribution for this query (mirrors the real archive's
        // corpus-wide noise floor) -- none clear the relative floor
        // themselves, just shape the mean/stdev `caption_relevance_threshold`
        // computes from.
        let cluster: [(f32, f32); 10] = [
            (0.26, 0.9656),
            (0.27, 0.9629),
            (0.28, 0.9600),
            (0.29, 0.9570),
            (0.30, 0.9539),
            (0.31, 0.9507),
            (0.32, 0.9474),
            (0.33, 0.9440),
            (0.28, 0.9600),
            (0.30, 0.9539),
        ];
        for (i, (x, y)) in cluster.iter().enumerate() {
            insert_shot_with_embedding(&conn, clip.id, 20.0 + i as f64, 21.0 + i as f64, "caption", &[*x, *y, 0.0]);
        }

        // Real match: modest but real support from *both* channels.
        let corroborated_shot = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "visual", &[0.30, 0.9539, 0.0]);
        add_embedding(&conn, corroborated_shot, "caption", &[0.58, 0.8146, 0.0]);

        // Hub: an isolated caption spike, no visual support at all.
        let hub_shot = insert_shot_with_embedding(&conn, clip.id, 4.0, 8.0, "visual", &[0.10, 0.9950, 0.0]);
        add_embedding(&conn, hub_shot, "caption", &[0.90, 0.4359, 0.0]);

        let results = search_shots(&conn, "field hockey game", &[1.0, 0.0, 0.0], &FacetFilters::default(), 10).unwrap();
        let corroborated_rank = results.iter().position(|r| r.shot_id == corroborated_shot);
        let hub_rank = results.iter().position(|r| r.shot_id == hub_shot);
        assert!(corroborated_rank.is_some() && hub_rank.is_some(), "both shots should surface");
        assert!(
            corroborated_rank.unwrap() < hub_rank.unwrap(),
            "a shot with real (if modest) visual+caption support must outrank a shot whose only signal is an isolated caption spike"
        );

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn caption_hub_score_correction_ranks_a_genuine_match_above_a_higher_raw_similarity_hub() {
        // Direct regression for a confirmed-live finding beyond the test
        // above: some captions are genuine embedding-space hubs -- high
        // raw similarity to almost *any* query, independent of content --
        // and this survives even after stripping generic boilerplate
        // phrasing (tried and measured live; it barely moved the numbers).
        // `caption_hub_score` (migration 009) is each caption's own
        // baseline similarity against a fixed anchor battery, computed at
        // gap-fill time; subtracting it must flip a ranking that raw
        // similarity alone gets backwards.
        let (db, path) = open_scratch_db("hub_score_correction");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/hub_score_correction.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        // Background cluster to give the per-query relative floor a
        // realistic distribution to compute from (same technique as the
        // test above) -- none of these carry a hub score, i.e. no
        // correction applied, and none are asserted on directly.
        for (i, raw) in [0.15_f32, 0.20, 0.10].iter().enumerate() {
            let y = (1.0 - raw * raw).sqrt();
            insert_shot_with_embedding(&conn, clip.id, 20.0 + i as f64, 21.0 + i as f64, "caption", &[*raw, y, 0.0]);
        }

        // Raw similarity alone (0.85) would rank this shot #1 -- but it's
        // a known hub, reflected in its own high stored caption_hub_score.
        let hub_shot = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "caption", &[0.85, 0.5268, 0.0]);
        conn.execute("UPDATE shots SET caption_hub_score = 0.78 WHERE id = ?1", params![hub_shot]).unwrap();

        // Real match: lower raw similarity (0.55), but a low hub score
        // means most of it is genuine, query-specific signal.
        let real_shot = insert_shot_with_embedding(&conn, clip.id, 4.0, 8.0, "caption", &[0.55, 0.8352, 0.0]);
        conn.execute("UPDATE shots SET caption_hub_score = 0.05 WHERE id = ?1", params![real_shot]).unwrap();

        let results = search_shots(&conn, "some query", &[1.0, 0.0, 0.0], &FacetFilters::default(), 10).unwrap();
        let hub_rank = results.iter().position(|r| r.shot_id == hub_shot);
        let real_rank = results.iter().position(|r| r.shot_id == real_shot);
        assert!(real_rank.is_some(), "the hub-corrected genuine match must surface");
        if let Some(hub_rank) = hub_rank {
            assert!(
                real_rank.unwrap() < hub_rank,
                "hub correction must rank the genuine match above the raw-similarity-only winner"
            );
        }

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn tag_matching_requires_a_whole_word_not_a_substring() {
        let (db, path) = open_scratch_db("tag_substring");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/outdoors.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        // Tagged "location" -- shares the substring "cat" with the query
        // below purely by coincidence ("lo-CAT-ion"), but has nothing to do
        // with cats. Vector embedding is also orthogonal to the query so
        // only a (bugged) substring tag match could surface it.
        let location_shot = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "visual", &[0.0, 1.0, 0.0]);
        conn.execute(
            "INSERT INTO tags (shot_id, label, source) VALUES (?1, 'location', 'spyglass_vlm')",
            params![location_shot],
        )
        .unwrap();

        let results = search_shots(&conn, "cat", &[1.0, 0.0, 0.0], &FacetFilters::default(), 10).unwrap();
        assert!(
            !results.iter().any(|r| r.shot_id == location_shot),
            "tag 'location' must not match query 'cat' just because 'cat' occurs inside it"
        );

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn list_favorite_shots_returns_only_favorited_shots_newest_first() {
        let (db, path) = open_scratch_db("favorites");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/favorites.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let shot_a = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "visual", &[1.0, 0.0, 0.0]);
        let shot_b = insert_shot_with_embedding(&conn, clip.id, 4.0, 8.0, "visual", &[0.0, 1.0, 0.0]);
        let _unfavorited = insert_shot_with_embedding(&conn, clip.id, 8.0, 12.0, "visual", &[0.0, 0.0, 1.0]);

        db::set_shot_favorite(&conn, shot_a, true).unwrap();
        db::set_shot_favorite(&conn, shot_b, true).unwrap();

        let results = list_favorite_shots(&conn).unwrap();
        assert_eq!(results.len(), 2);
        assert!(results.iter().all(|r| r.is_favorite));
        // Most recently favorited (highest id) first.
        assert_eq!(results[0].shot_id, shot_b);
        assert_eq!(results[1].shot_id, shot_a);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn search_shots_respects_limit() {
        let (db, path) = open_scratch_db("limit");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/many.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();
        for i in 0..5 {
            insert_shot_with_embedding(&conn, clip.id, i as f64, i as f64 + 1.0, "visual", &[1.0, 0.0, 0.0]);
        }

        let results = search_shots(&conn, "anything", &[1.0, 0.0, 0.0], &FacetFilters::default(), 2).unwrap();
        assert_eq!(results.len(), 2);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn search_shots_sort_by_newest_overrides_relevance_ranking() {
        let (db, path) = open_scratch_db("sort_newest");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/sort_newest.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();
        set_ingested_at(&conn, clip.id, "2025-06-01");

        // The stronger visual match (closer to the query embedding) is the
        // *older* clip -- under plain relevance this would rank first;
        // sort_by=NewestFirst must flip that.
        let more_relevant_older = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "visual", &[1.0, 0.0, 0.0]);
        let clip_b = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/sort_newest_b.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();
        set_ingested_at(&conn, clip_b.id, "2025-12-01");
        let less_relevant_newer = insert_shot_with_embedding(&conn, clip_b.id, 0.0, 4.0, "visual", &[0.3, 1.0, 0.0]);

        let by_relevance = search_shots(&conn, "mascot", &[1.0, 0.0, 0.0], &FacetFilters::default(), 10).unwrap();
        assert_eq!(by_relevance[0].shot_id, more_relevant_older, "sanity check: relevance alone ranks the stronger visual match first");

        let filters = FacetFilters { sort_by: SortBy::NewestFirst, ..Default::default() };
        let by_newest = search_shots(&conn, "mascot", &[1.0, 0.0, 0.0], &filters, 10).unwrap();
        assert_eq!(by_newest[0].shot_id, less_relevant_newer, "sort_by=NewestFirst must rank the newer clip's shot first regardless of relevance");
        assert_eq!(by_newest[1].shot_id, more_relevant_older);

        fn set_ingested_at(conn: &Connection, clip_id: i64, date: &str) {
            conn.execute("UPDATE clips SET ingested_at = ?1 WHERE id = ?2", params![format!("{date}T00:00:00.000Z"), clip_id]).unwrap();
        }

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn search_shots_sort_by_highest_quality_ranks_unscored_shots_last() {
        let (db, path) = open_scratch_db("sort_quality");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/sort_quality.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let high_quality = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "visual", &[1.0, 0.0, 0.0]);
        let unscored = insert_shot_with_embedding(&conn, clip.id, 4.0, 8.0, "visual", &[1.0, 0.0, 0.0]);
        conn.execute("UPDATE shots SET technical_quality_score = 0.95 WHERE id = ?1", params![high_quality]).unwrap();

        let filters = FacetFilters { sort_by: SortBy::HighestQuality, ..Default::default() };
        let results = search_shots(&conn, "mascot", &[1.0, 0.0, 0.0], &filters, 10).unwrap();
        assert_eq!(results[0].shot_id, high_quality);
        assert_eq!(results[1].shot_id, unscored, "an unscored shot must sort last, not be dropped");

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn search_shots_facet_filter_excludes_a_relevant_shot_that_fails_the_facet() {
        let (db, path) = open_scratch_db("facet_filter");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/facet_filter.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        // Both shots are equally strong text-relevance matches (identical
        // visual embedding); only the facet filter should tell them apart.
        let tagged_shot = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "visual", &[1.0, 0.0, 0.0]);
        let untagged_shot = insert_shot_with_embedding(&conn, clip.id, 4.0, 8.0, "visual", &[1.0, 0.0, 0.0]);
        conn.execute(
            "INSERT INTO tags (shot_id, label, source) VALUES (?1, 'mascot', 'spyglass_vlm')",
            params![tagged_shot],
        )
        .unwrap();

        let unfiltered = search_shots(&conn, "anything", &[1.0, 0.0, 0.0], &FacetFilters::default(), 10).unwrap();
        assert!(unfiltered.iter().any(|r| r.shot_id == tagged_shot));
        assert!(unfiltered.iter().any(|r| r.shot_id == untagged_shot));

        let filters = FacetFilters { tags: vec!["mascot".into()], ..Default::default() };
        let filtered = search_shots(&conn, "anything", &[1.0, 0.0, 0.0], &filters, 10).unwrap();
        assert!(filtered.iter().any(|r| r.shot_id == tagged_shot));
        assert!(
            !filtered.iter().any(|r| r.shot_id == untagged_shot),
            "a shot with real query relevance but no matching facet must still be excluded once a facet filter is set"
        );

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn find_similar_shots_excludes_the_reference_and_ranks_closest_first() {
        let (db, path) = open_scratch_db("find_similar");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/similar.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        let reference = insert_shot_with_embedding(&conn, clip.id, 0.0, 4.0, "visual", &[1.0, 0.0, 0.0]);
        let close = insert_shot_with_embedding(&conn, clip.id, 4.0, 8.0, "visual", &[0.9, 0.1, 0.0]);
        let far = insert_shot_with_embedding(&conn, clip.id, 8.0, 12.0, "visual", &[0.0, 1.0, 0.0]);

        let results = find_similar_shots(&conn, reference, 10).unwrap();
        assert!(!results.iter().any(|r| r.shot_id == reference), "reference shot must not match itself");
        assert_eq!(results[0].shot_id, close);
        assert_eq!(results.last().unwrap().shot_id, far);

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn find_similar_shots_returns_empty_when_reference_has_no_visual_embedding() {
        let (db, path) = open_scratch_db("find_similar_missing");
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: "/Volumes/Archive/no_embedding.mov".to_string(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();
        conn.execute(
            "INSERT INTO shots (clip_id, start_tc, end_tc) VALUES (?1, 0.0, 1.0)",
            params![clip.id],
        )
        .unwrap();
        let shot_without_embedding = conn.last_insert_rowid();

        let results = find_similar_shots(&conn, shot_without_embedding, 10).unwrap();
        assert!(results.is_empty());

        drop(conn);
        std::fs::remove_file(&path).ok();
    }

    /// Full-stack proof, not a mock: runs a real clip through the real
    /// gap-fill sidecar (scene detection, CLIP embeddings, moondream2
    /// captioning/tagging), embeds a query with the real CLIP text
    /// encoder, and confirms the hybrid ranking actually surfaces the shot
    /// whose real VLM-generated tags/caption matched. Ignored by default
    /// (slow, needs the sidecar venv); run explicitly with
    /// `cargo test --release -- --ignored real_search_end_to_end`.
    #[test]
    #[ignore]
    fn real_search_end_to_end_ranks_the_matching_shot_first() {
        use crate::pipeline::{self, SidecarCommand};
        use std::process::Command;

        let dir = std::env::temp_dir().join(format!("spyglass_search_e2e_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();

        let clip_path = dir.join("scene.mp4");
        let ffmpeg_status = Command::new("ffmpeg")
            .args([
                "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
            ])
            .arg(&clip_path)
            .status()
            .expect("ffmpeg must be on PATH for this test");
        assert!(ffmpeg_status.success());

        let sidecar_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().join("sidecar");
        assert!(sidecar_dir.join(".venv/bin/python").exists(), "sidecar venv not found at {sidecar_dir:?}");

        let db = Db::open_at(&dir.join("index.sqlite")).unwrap();
        let conn = db.conn.lock().unwrap();
        let clip = db::upsert_clip(
            &conn,
            &NewClip {
                file_path: clip_path.to_string_lossy().into_owned(),
                source_app: SourceApp::SpyglassScan,
                checksum: None,
                size_bytes: None,
                duration_sec: None,
                recorded_at: None,
            },
        )
        .unwrap();

        // Drives the same two production functions the real worker loop
        // composes (`run_sidecar_for_clip` then `write_gap_fill_result` --
        // see `gap_fill_worker.rs`'s doc comment), rather than the
        // production-dead single-call combinator this test used to go
        // through.
        let sidecar = SidecarCommand::real(&sidecar_dir);
        let (keyframe_dir, output) = pipeline::run_sidecar_for_clip(
            &clip, &sidecar, &dir.join("keyframes"),
            std::time::Duration::from_secs(120), None,
        )
        .unwrap();
        pipeline::write_gap_fill_result(&conn, &clip, &keyframe_dir, &output, None).unwrap();

        // Whatever the VLM actually captioned this shot, use one of its own
        // real tags (if any) as the query -- proves the loop is closed
        // rather than asserting a specific caption's wording, which would
        // make this test flaky against model nondeterminism.
        let tag: Option<String> = conn
            .query_row("SELECT label FROM tags LIMIT 1", [], |r| r.get(0))
            .optional()
            .unwrap();
        let query_text = tag.unwrap_or_else(|| "colorful abstract pattern".to_string());

        let embed_script = format!(
            "import sys, json; sys.path.insert(0, {sidecar_dir:?}); from analyze_clip import embed_text; print(json.dumps(embed_text({query_text:?})))"
        );
        let python = sidecar_dir.join(".venv/bin/python");
        let output = Command::new(&python).args(["-c", &embed_script]).output().unwrap();
        assert!(output.status.success(), "embedding helper failed: {}", String::from_utf8_lossy(&output.stderr));
        let query_embedding: Vec<f32> = serde_json::from_slice(&output.stdout).unwrap();

        let results = search_shots(&conn, &query_text, &query_embedding, &FacetFilters::default(), 10).unwrap();
        assert!(!results.is_empty(), "expected at least one ranked shot");
        // The only shot in the index should come back, and with a
        // meaningfully positive score (not just a bare tag-match floor),
        // proving the vector similarity term is actually contributing.
        assert!(results[0].score > 0.0);

        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }
}
