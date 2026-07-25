export interface Segment {
  ref_start: number;
  ref_end: number;
  take_start: number;
  take_end: number;
  speed_factor: number;
  flagged: boolean;
}

export interface Anchor {
  ref_time: number;
  take_times: Record<string, number | null>;
  confidence: Record<string, number | null>;
}

export interface SyncReport {
  reference: string;
  takes: string[];
  ref_duration: number;
  take_durations: Record<string, number>;
  coarse_offsets_sec: Record<string, number>;
  coarse_offset_confidence: Record<string, number>;
  anchors: Anchor[];
  segments: Record<string, Segment[]>;
  skipped_anchors: Record<string, number>;
  excluded_leadin_ref_sec: Record<string, number>;
  waveforms: Record<string, number[]>;
  merge_tolerance: number;
  flag_speed_min: number;
  flag_speed_max: number;
}

export interface AlignResult {
  report_path: string;
  report: SyncReport;
}

export interface ImportResult {
  project: string;
  timeline: string;
  fcpxml_path: string;
}
