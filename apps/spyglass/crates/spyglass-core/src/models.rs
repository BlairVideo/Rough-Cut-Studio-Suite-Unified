use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceApp {
    CardEater,
    SpyglassScan,
}

impl SourceApp {
    pub fn as_str(self) -> &'static str {
        match self {
            SourceApp::CardEater => "card_eater",
            SourceApp::SpyglassScan => "spyglass_scan",
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s {
            "card_eater" => SourceApp::CardEater,
            _ => SourceApp::SpyglassScan,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Clip {
    pub id: i64,
    pub file_path: String,
    pub source_app: SourceApp,
    pub checksum: Option<String>,
    pub size_bytes: Option<i64>,
    pub duration_sec: Option<f64>,
    pub frame_rate: Option<f64>,
    pub ingested_at: String,
}

/// A clip row not yet assigned an id (insert input).
#[derive(Debug, Clone, PartialEq)]
pub struct NewClip {
    pub file_path: String,
    pub source_app: SourceApp,
    pub checksum: Option<String>,
    pub size_bytes: Option<i64>,
    pub duration_sec: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Shot {
    pub id: i64,
    pub clip_id: i64,
    pub start_tc: f64,
    pub end_tc: f64,
    pub keyframe_path: Option<String>,
    pub technical_quality_score: Option<f64>,
    pub energy_score: Option<f64>,
    pub caption: Option<String>,
    pub is_favorite: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct NewTranscriptSegment {
    pub clip_id: i64,
    pub start_tc: f64,
    pub end_tc: f64,
    pub speaker: Option<String>,
    pub text: String,
    pub avg_logprob: Option<f64>,
    pub no_speech_prob: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TranscriptSegment {
    pub id: i64,
    pub clip_id: i64,
    pub start_tc: f64,
    pub end_tc: f64,
    pub speaker: Option<String>,
    pub text: String,
    pub avg_logprob: Option<f64>,
    pub no_speech_prob: Option<f64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TagSource {
    Human,
    SpyglassVlm,
}

impl TagSource {
    pub fn as_str(self) -> &'static str {
        match self {
            TagSource::Human => "human",
            TagSource::SpyglassVlm => "spyglass_vlm",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Tag {
    pub id: i64,
    pub shot_id: i64,
    pub label: String,
    pub source: String,
    pub confidence: Option<f64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AccessLevel {
    Active,
    Paused,
    Removed,
}

impl AccessLevel {
    pub fn as_str(self) -> &'static str {
        match self {
            AccessLevel::Active => "active",
            AccessLevel::Paused => "paused",
            AccessLevel::Removed => "removed",
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s {
            "paused" => AccessLevel::Paused,
            "removed" => AccessLevel::Removed,
            _ => AccessLevel::Active,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WatchedRoot {
    pub id: i64,
    pub label: String,
    pub path: String,
    pub volume_id: Option<String>,
    pub access_level: String,
    pub approved_by: Option<String>,
    pub approved_at: String,
    pub last_scanned_at: Option<String>,
    pub sidecar_cache_enabled: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct NewWatchedRoot {
    pub label: String,
    pub path: String,
    pub volume_id: Option<String>,
    pub approved_by: Option<String>,
}

/// A transcript-keyword search hit -- a segment plus enough of its parent
/// clip to jump to source.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TranscriptSearchResult {
    pub segment: TranscriptSegment,
    pub clip_file_path: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JobStatus {
    Pending,
    Running,
    Done,
    Failed,
    AwaitingReconnect,
}

impl JobStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            JobStatus::Pending => "pending",
            JobStatus::Running => "running",
            JobStatus::Done => "done",
            JobStatus::Failed => "failed",
            JobStatus::AwaitingReconnect => "awaiting_reconnect",
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s {
            "running" => JobStatus::Running,
            "done" => JobStatus::Done,
            "failed" => JobStatus::Failed,
            "awaiting_reconnect" => JobStatus::AwaitingReconnect,
            _ => JobStatus::Pending,
        }
    }
}

/// The persisted gap-fill queue's per-clip row (Section 7).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GapFillJob {
    pub id: i64,
    pub clip_id: i64,
    pub status: JobStatus,
    pub attempts: i64,
    pub last_error: Option<String>,
    pub queued_at: String,
    pub updated_at: String,
}

/// Per-root status panel numbers (Section 7): "14,820 / 15,000 indexed,
/// 180 queued, 12 failed."
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct GapFillProgress {
    pub discovered: i64,
    pub indexed: i64,
    pub queued: i64,
    pub failed: i64,
    pub awaiting_reconnect: i64,
}

/// Result of `db::reset_watched_root` -- how many clips were purged and
/// which ids, so the caller (the Tauri/PyO3 layer, which owns the
/// filesystem path to the keyframe cache -- this crate has no concept of
/// it) can also delete each purged clip's cached keyframe JPEGs, which
/// aren't a `clips` foreign key and don't cascade on their own.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResetWatchedRootResult {
    pub clips_removed: usize,
    pub removed_clip_ids: Vec<i64>,
}

/// One ranked hit from `search::search_shots` (Section 12) -- a shot plus
/// enough of its parent clip and gap-fill output to render a result card
/// and jump to source.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ShotSearchResult {
    pub shot_id: i64,
    pub clip_id: i64,
    pub clip_file_path: String,
    pub clip_frame_rate: Option<f64>,
    pub start_tc: f64,
    pub end_tc: f64,
    pub keyframe_path: Option<String>,
    pub technical_quality_score: Option<f64>,
    pub energy_score: Option<f64>,
    pub caption: Option<String>,
    pub tags: Vec<String>,
    pub score: f64,
    pub is_favorite: bool,
}

/// The pool tray's backing store (Section 13/14) -- an ordered list of
/// shot ids staged for export. `shot_ids` is stored as a JSON array in the
/// `shot_ids` TEXT column; this struct holds it already decoded.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Collection {
    pub id: i64,
    pub name: String,
    pub shot_ids: Vec<i64>,
    pub created_at: String,
}
