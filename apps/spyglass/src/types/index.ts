export type SourceApp = "card_eater" | "spyglass_scan";

export interface Clip {
  id: number;
  file_path: string;
  source_app: SourceApp;
  checksum: string | null;
  size_bytes: number | null;
  duration_sec: number | null;
  ingested_at: string;
}

export interface TranscriptSegment {
  id: number;
  clip_id: number;
  start_tc: number;
  end_tc: number;
  speaker: string | null;
  text: string;
  avg_logprob: number | null;
  no_speech_prob: number | null;
}

export interface TranscriptSearchResult {
  segment: TranscriptSegment;
  clip_file_path: string;
}

export type AccessLevel = "active" | "paused" | "removed";

export interface WatchedRoot {
  id: number;
  label: string;
  path: string;
  volume_id: string | null;
  access_level: AccessLevel;
  approved_by: string | null;
  approved_at: string;
  last_scanned_at: string | null;
  sidecar_cache_enabled: boolean;
}

export interface BackgroundWorkStatus {
  manually_paused: boolean;
  idle_seconds: number | null;
  min_idle_seconds: number;
  force_active: boolean;
}

export interface GapFillProgress {
  discovered: number;
  indexed: number;
  queued: number;
  failed: number;
  awaiting_reconnect: number;
}

export interface WatchedRootStatus extends WatchedRoot {
  is_online: boolean;
  progress: GapFillProgress;
}

export interface ScanResult {
  discovered: number;
  registered: number;
  already_registered: number;
  excluded_removed: number;
  relinked: number;
  errors: string[];
}

export interface ShotSearchResult {
  shot_id: number;
  clip_id: number;
  clip_file_path: string;
  clip_frame_rate: number | null;
  start_tc: number;
  end_tc: number;
  keyframe_path: string | null;
  technical_quality_score: number | null;
  energy_score: number | null;
  caption: string | null;
  tags: string[];
  score: number;
  is_favorite: boolean;
}

// Consolidate & Copy export (Section 15).

export type TrimPrecision = "stream_copy" | "re_encode";

export type CopyMode = { mode: "full_source" } | { mode: "trimmed"; handle_seconds: number; precision: TrimPrecision };

export type FolderStructure = "flat" | "subfolder_per_tag";

export interface ConsolidateEstimate {
  file_count: number;
  total_bytes: number;
  available_bytes: number;
  destination_has_existing_files: boolean;
}

export interface ManifestEntry {
  original_path: string;
  copied_path: string;
  in_seconds: number;
  out_seconds: number;
  frame_rate: number | null;
  tags: string[];
  checksum: string;
  checksum_verified: boolean;
}

export interface ConsolidateExportStatus {
  completed: number;
  total: number;
  current_file: string;
  finished: boolean;
  error: string | null;
  manifest: ManifestEntry[] | null;
}

// Index maintenance (Section 17/18: backup, restore, integrity, rebuild).

export interface BackupInfo {
  file_name: string;
  path: string;
  size_bytes: number;
  created_at: string;
}

// Facet browsing (Section 12/13).

export type SortBy = "relevance" | "newest_first" | "oldest_first" | "highest_quality" | "most_energy";

export interface FacetFilters {
  tags: string[];
  source_app: SourceApp | null;
  date_from: string | null;
  date_to: string | null;
  sort_by: SortBy;
}

export interface TagFacet {
  label: string;
  shot_count: number;
}

export interface SourceFacet {
  source_app: SourceApp;
  shot_count: number;
}

export interface FacetOptions {
  tags: TagFacet[];
  sources: SourceFacet[];
  earliest_date: string | null;
  latest_date: string | null;
}
