import { invoke } from "@tauri-apps/api/core";
import type {
  ScanResult,
  ShotSearchResult,
  TranscriptSearchResult,
  WatchedRoot,
  WatchedRootStatus,
  AccessLevel,
  ConsolidateEstimate,
  ConsolidateExportStatus,
  CopyMode,
  FolderStructure,
  BackupInfo,
  BackgroundWorkStatus,
  FacetFilters,
  FacetOptions,
} from "../types";

export const api = {
  listWatchedRoots: () => invoke<WatchedRootStatus[]>("list_watched_roots"),

  addWatchedRoot: (label: string, path: string, volumeId?: string, approvedBy?: string) =>
    invoke<WatchedRoot>("add_watched_root", {
      label,
      path,
      volumeId: volumeId ?? null,
      approvedBy: approvedBy ?? null,
    }),

  setWatchedRootAccessLevel: (id: number, accessLevel: AccessLevel) =>
    invoke<void>("set_watched_root_access_level", { id, accessLevel }),

  removeWatchedRoot: (id: number) => invoke<void>("remove_watched_root", { id }),

  resetWatchedRoot: (id: number) => invoke<number>("reset_watched_root", { id }),

  relinkWatchedRoot: (id: number, newPath: string) =>
    invoke<WatchedRoot>("relink_watched_root", { id, newPath }),

  scanWatchedRoot: (rootId: number) => invoke<ScanResult>("scan_watched_root", { rootId }),

  scanCardEater: (dbPath?: string) => invoke<ScanResult>("scan_card_eater", { dbPath: dbPath ?? null }),

  scanTranscriberSidecars: (rootPath: string) =>
    invoke<ScanResult>("scan_transcriber_sidecars", { rootPath }),

  scanBrollCache: (rootPath: string) => invoke<ScanResult>("scan_broll_cache", { rootPath }),

  enqueueGapFill: () => invoke<number>("enqueue_gap_fill"),

  retryFailedJobs: (rootId?: number) => invoke<number>("retry_failed_jobs", { rootId: rootId ?? null }),

  setQueuePaused: (paused: boolean) => invoke<void>("set_queue_paused", { paused }),

  getQueuePaused: () => invoke<boolean>("get_queue_paused"),

  getBackgroundWorkStatus: () => invoke<BackgroundWorkStatus>("get_background_work_status"),

  searchTranscripts: (query: string) => invoke<TranscriptSearchResult[]>("search_transcripts", { query }),

  searchShots: (query: string, filters: FacetFilters) => invoke<ShotSearchResult[]>("search_shots", { query, filters }),

  findSimilarShots: (shotId: number) => invoke<ShotSearchResult[]>("find_similar_shots", { shotId }),

  listFacetOptions: () => invoke<FacetOptions>("list_facet_options"),

  browseShots: (filters: FacetFilters) => invoke<ShotSearchResult[]>("browse_shots", { filters }),

  addTag: (shotId: number, label: string) => invoke<void>("add_tag", { shotId, label }),

  removeTag: (shotId: number, label: string) => invoke<void>("remove_tag", { shotId, label }),

  setShotFavorite: (shotId: number, favorite: boolean) =>
    invoke<void>("set_shot_favorite", { shotId, favorite }),

  listFavoriteShots: () => invoke<ShotSearchResult[]>("list_favorite_shots"),

  getPool: () => invoke<ShotSearchResult[]>("get_pool"),

  addShotToPool: (shotId: number) => invoke<void>("add_shot_to_pool", { shotId }),

  removeShotFromPool: (shotId: number) => invoke<void>("remove_shot_from_pool", { shotId }),

  reorderPool: (shotIds: number[]) => invoke<void>("reorder_pool", { shotIds }),

  clearPool: () => invoke<void>("clear_pool"),

  exportPoolToPremiereXml: (destinationPath: string, sequenceName: string) =>
    invoke<string>("export_pool_to_premiere_xml", { destinationPath, sequenceName }),

  estimateConsolidateExport: (destinationPath: string, copyMode: CopyMode) =>
    invoke<ConsolidateEstimate>("estimate_consolidate_export", { destinationPath, copyMode }),

  startConsolidateExport: (destinationPath: string, poolName: string, copyMode: CopyMode, folderStructure: FolderStructure) =>
    invoke<void>("start_consolidate_export", { destinationPath, poolName, copyMode, folderStructure }),

  getConsolidateExportStatus: () => invoke<ConsolidateExportStatus | null>("get_consolidate_export_status"),

  exportCopiedFilesToPremiereXml: (destinationPath: string, sequenceName: string) =>
    invoke<string>("export_copied_files_to_premiere_xml", { destinationPath, sequenceName }),

  backupIndexNow: () => invoke<BackupInfo>("backup_index_now"),

  listBackups: () => invoke<BackupInfo[]>("list_backups"),

  restoreBackup: (backupPath: string) => invoke<void>("restore_backup", { backupPath }),

  checkIndexIntegrity: () => invoke<string[]>("check_index_integrity"),

  rebuildSearchIndex: () => invoke<void>("rebuild_search_index_cmd"),

  purgeBadTags: () => invoke<number>("purge_bad_tags"),

  requeueShortShotClips: () => invoke<number>("requeue_short_shot_clips"),

  /// `rect.y` must already be in AppKit's bottom-left-origin convention
  /// (distance from the bottom of the viewport), not the DOM's top-left --
  /// see the comment in `ShotPreviewPlayer.tsx` for why that conversion
  /// happens here rather than being re-derived on the Rust side.
  openNativeVideoPreview: (
    path: string,
    startTc: number,
    rect: { x: number; y: number; width: number; height: number },
  ) => invoke<void>("open_native_video_preview", { path, startTc, ...rect }),

  closeNativeVideoPreview: () => invoke<void>("close_native_video_preview"),

  /// Fire-and-forget read-ahead -- call on shot hover, well before the
  /// user actually clicks to preview, so a sleeping external/archival
  /// drive gets a head start waking up. See `prefetch_clip_file`'s doc
  /// comment for why this never needs to be awaited or handled.
  prefetchClipFile: (path: string) => invoke<void>("prefetch_clip_file", { path }),
};
