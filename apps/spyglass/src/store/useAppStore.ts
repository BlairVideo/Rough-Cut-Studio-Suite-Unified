import { create } from "zustand";
import { api } from "../lib/api";
import type { BackgroundWorkStatus, FacetFilters, FacetOptions, ShotSearchResult, SortBy, SourceApp, WatchedRootStatus } from "../types";

function updateShotTags(shots: ShotSearchResult[], shotId: number, updater: (tags: string[]) => string[]) {
  return shots.map((s) => (s.shot_id === shotId ? { ...s, tags: updater(s.tags) } : s));
}

/// Synthetic `searchQuery` value while viewing the favorites list -- same
/// trick `findSimilarTo` uses to repurpose the results grid for a
/// non-text-search view without a second results component.
const FAVORITES_QUERY_LABEL = "Favorites";

const EMPTY_FACET_FILTERS: FacetFilters = { tags: [], source_app: null, date_from: null, date_to: null, sort_by: "relevance" };

export function hasActiveFacetFilters(filters: FacetFilters): boolean {
  return filters.tags.length > 0 || filters.source_app !== null || filters.date_from !== null || filters.date_to !== null;
}

interface AppState {
  watchedRoots: WatchedRootStatus[];
  searchQuery: string;
  searchResults: ShotSearchResult[];
  searching: boolean;
  searchError: string | null;
  settingsOpen: boolean;
  statusMessage: string | null;
  queuePaused: boolean;
  backgroundWorkStatus: BackgroundWorkStatus | null;
  pool: ShotSearchResult[];
  poolOpen: boolean;
  facetOptions: FacetOptions | null;
  facetFilters: FacetFilters;
  facetSidebarOpen: boolean;

  setSettingsOpen: (open: boolean) => void;
  setPoolOpen: (open: boolean) => void;
  setSearchQuery: (query: string) => void;
  runSearch: () => Promise<void>;
  setFacetSidebarOpen: (open: boolean) => void;
  refreshFacetOptions: () => Promise<void>;
  toggleTagFilter: (label: string) => void;
  setSourceAppFilter: (sourceApp: SourceApp | null) => void;
  setDateRange: (from: string | null, to: string | null) => void;
  setSortBy: (sortBy: SortBy) => void;
  clearFacetFilters: () => void;
  findSimilarTo: (shot: ShotSearchResult) => Promise<void>;
  showFavorites: () => Promise<void>;
  toggleFavorite: (shotId: number, favorite: boolean) => Promise<void>;
  refreshWatchedRoots: () => Promise<void>;
  refreshQueuePaused: () => Promise<void>;
  toggleQueuePaused: () => Promise<void>;
  refreshBackgroundWorkStatus: () => Promise<void>;
  setStatusMessage: (message: string | null) => void;

  refreshPool: () => Promise<void>;
  addToPool: (shotId: number) => Promise<void>;
  removeFromPool: (shotId: number) => Promise<void>;
  reorderPool: (shotIds: number[]) => Promise<void>;
  clearPool: () => Promise<void>;
  exportPool: (destinationPath: string, sequenceName: string) => Promise<void>;

  addTag: (shotId: number, label: string) => Promise<void>;
  removeTag: (shotId: number, label: string) => Promise<void>;
}

export const useAppStore = create<AppState>((set, get) => ({
  watchedRoots: [],
  searchQuery: "",
  searchResults: [],
  searching: false,
  searchError: null,
  settingsOpen: false,
  statusMessage: null,
  queuePaused: false,
  backgroundWorkStatus: null,
  pool: [],
  poolOpen: false,
  facetOptions: null,
  facetFilters: EMPTY_FACET_FILTERS,
  facetSidebarOpen: true,

  setSettingsOpen: (open) => set({ settingsOpen: open }),
  setPoolOpen: (open) => set({ poolOpen: open }),
  setSearchQuery: (query) => set({ searchQuery: query }),
  setStatusMessage: (message) => set({ statusMessage: message }),
  setFacetSidebarOpen: (open) => set({ facetSidebarOpen: open }),

  // Facet-driven search (Section 12/13): a text query is ranked and then
  // narrowed by the selected facets (`api.searchShots`); facets alone with
  // no text query fall back to a plain newest-first browse
  // (`api.browseShots`), since relevance ranking has nothing to rank
  // against without query text.
  runSearch: async () => {
    const query = get().searchQuery.trim();
    const filters = get().facetFilters;
    if (!query && !hasActiveFacetFilters(filters)) {
      set({ searchResults: [], searchError: null });
      return;
    }
    set({ searching: true, searchError: null });
    try {
      const results = query ? await api.searchShots(query, filters) : await api.browseShots(filters);
      set({ searchResults: results, searchError: null });
    } catch (err) {
      // Clear stale results on failure -- otherwise a failed search (e.g.
      // an embed-server timeout under load) silently leaves the *previous*
      // query's results on screen, which looks identical to "search isn't
      // doing anything" rather than the actual error it is.
      set({ searchResults: [], statusMessage: `Search failed: ${String(err)}`, searchError: String(err) });
    } finally {
      set({ searching: false });
    }
  },

  refreshFacetOptions: async () => {
    try {
      const facetOptions = await api.listFacetOptions();
      set({ facetOptions });
    } catch (err) {
      set({ statusMessage: `Could not load filters: ${String(err)}` });
    }
  },

  toggleTagFilter: (label) => {
    set((state) => {
      const tags = state.facetFilters.tags.includes(label)
        ? state.facetFilters.tags.filter((t) => t !== label)
        : [...state.facetFilters.tags, label];
      return { facetFilters: { ...state.facetFilters, tags } };
    });
    void get().runSearch();
  },

  setSourceAppFilter: (sourceApp) => {
    set((state) => ({ facetFilters: { ...state.facetFilters, source_app: sourceApp } }));
    void get().runSearch();
  },

  setDateRange: (from, to) => {
    set((state) => ({ facetFilters: { ...state.facetFilters, date_from: from, date_to: to } }));
    void get().runSearch();
  },

  setSortBy: (sortBy) => {
    set((state) => ({ facetFilters: { ...state.facetFilters, sort_by: sortBy } }));
    void get().runSearch();
  },

  // Sort order isn't itself a "filter" (hasActiveFacetFilters ignores it,
  // same reasoning) -- "Clear filters" resets which shots qualify, not the
  // order the producer chose to view them in.
  clearFacetFilters: () => {
    set((state) => ({ facetFilters: { ...EMPTY_FACET_FILTERS, sort_by: state.facetFilters.sort_by } }));
    void get().runSearch();
  },

  findSimilarTo: async (shot) => {
    set({ searching: true, searchError: null, searchQuery: `Similar to shot in ${shot.clip_file_path.split("/").pop()}` });
    try {
      const results = await api.findSimilarShots(shot.shot_id);
      set({ searchResults: results, searchError: null });
    } catch (err) {
      set({ searchResults: [], statusMessage: `Similarity search failed: ${String(err)}`, searchError: String(err) });
    } finally {
      set({ searching: false });
    }
  },

  showFavorites: async () => {
    set({ searching: true, searchError: null, searchQuery: FAVORITES_QUERY_LABEL });
    try {
      const results = await api.listFavoriteShots();
      set({ searchResults: results, searchError: null });
    } catch (err) {
      set({ searchResults: [], statusMessage: `Could not load favorites: ${String(err)}`, searchError: String(err) });
    } finally {
      set({ searching: false });
    }
  },

  toggleFavorite: async (shotId, favorite) => {
    try {
      await api.setShotFavorite(shotId, favorite);
      set((state) => ({
        // Viewing the favorites list itself: an un-favorited shot drops out
        // of view immediately rather than lingering with an unfilled star.
        searchResults:
          state.searchQuery === FAVORITES_QUERY_LABEL && !favorite
            ? state.searchResults.filter((s) => s.shot_id !== shotId)
            : state.searchResults.map((s) => (s.shot_id === shotId ? { ...s, is_favorite: favorite } : s)),
        pool: state.pool.map((s) => (s.shot_id === shotId ? { ...s, is_favorite: favorite } : s)),
      }));
    } catch (err) {
      set({ statusMessage: `Could not update favorite: ${String(err)}` });
    }
  },

  refreshWatchedRoots: async () => {
    try {
      const roots = await api.listWatchedRoots();
      set({ watchedRoots: roots });
    } catch (err) {
      set({ statusMessage: `Could not load watched roots: ${String(err)}` });
    }
  },

  refreshQueuePaused: async () => {
    try {
      const paused = await api.getQueuePaused();
      set({ queuePaused: paused });
    } catch {
      // Non-critical status read; leave last-known value in place.
    }
  },

  refreshBackgroundWorkStatus: async () => {
    try {
      const status = await api.getBackgroundWorkStatus();
      set({ backgroundWorkStatus: status });
    } catch {
      // Non-critical status read; leave last-known value in place.
    }
  },

  toggleQueuePaused: async () => {
    const next = !get().queuePaused;
    try {
      await api.setQueuePaused(next);
      set({ queuePaused: next });
    } catch (err) {
      set({ statusMessage: `Could not update the background queue: ${String(err)}` });
    }
  },

  refreshPool: async () => {
    try {
      const pool = await api.getPool();
      set({ pool });
    } catch (err) {
      set({ statusMessage: `Could not load the pool: ${String(err)}` });
    }
  },

  addToPool: async (shotId) => {
    try {
      await api.addShotToPool(shotId);
      await get().refreshPool();
    } catch (err) {
      set({ statusMessage: `Could not add to pool: ${String(err)}` });
    }
  },

  removeFromPool: async (shotId) => {
    try {
      await api.removeShotFromPool(shotId);
      set({ pool: get().pool.filter((s) => s.shot_id !== shotId) });
    } catch (err) {
      set({ statusMessage: `Could not remove from pool: ${String(err)}` });
    }
  },

  reorderPool: async (shotIds) => {
    const byId = new Map(get().pool.map((s) => [s.shot_id, s]));
    const reordered = shotIds.map((id) => byId.get(id)).filter((s): s is ShotSearchResult => Boolean(s));
    set({ pool: reordered }); // optimistic -- drag feedback shouldn't wait on a round trip
    try {
      await api.reorderPool(shotIds);
    } catch (err) {
      set({ statusMessage: `Could not save pool order: ${String(err)}` });
      void get().refreshPool();
    }
  },

  clearPool: async () => {
    try {
      await api.clearPool();
      set({ pool: [] });
    } catch (err) {
      set({ statusMessage: `Could not clear the pool: ${String(err)}` });
    }
  },

  exportPool: async (destinationPath, sequenceName) => {
    try {
      const path = await api.exportPoolToPremiereXml(destinationPath, sequenceName);
      set({ statusMessage: `Exported Premiere Pro sequence to ${path}` });
    } catch (err) {
      set({ statusMessage: `Export failed: ${String(err)}` });
    }
  },

  addTag: async (shotId, label) => {
    const trimmed = label.trim();
    if (!trimmed) return;
    try {
      await api.addTag(shotId, trimmed);
      set((state) => ({
        searchResults: updateShotTags(state.searchResults, shotId, (tags) =>
          tags.includes(trimmed) ? tags : [...tags, trimmed].sort(),
        ),
        pool: updateShotTags(state.pool, shotId, (tags) => (tags.includes(trimmed) ? tags : [...tags, trimmed].sort())),
      }));
      void get().refreshFacetOptions();
    } catch (err) {
      set({ statusMessage: `Could not add tag: ${String(err)}` });
    }
  },

  removeTag: async (shotId, label) => {
    try {
      await api.removeTag(shotId, label);
      set((state) => ({
        searchResults: updateShotTags(state.searchResults, shotId, (tags) => tags.filter((t) => t !== label)),
        pool: updateShotTags(state.pool, shotId, (tags) => tags.filter((t) => t !== label)),
      }));
      void get().refreshFacetOptions();
    } catch (err) {
      set({ statusMessage: `Could not remove tag: ${String(err)}` });
    }
  },
}));
