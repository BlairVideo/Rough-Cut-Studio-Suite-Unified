import { hasActiveFacetFilters, useAppStore } from "../store/useAppStore";
import type { SortBy } from "../types";
import { ShotCard } from "./ShotCard";

const SORT_LABELS: Record<SortBy, string> = {
  relevance: "Relevance",
  newest_first: "Newest first",
  oldest_first: "Oldest first",
  highest_quality: "Highest quality",
  most_energy: "Most energy",
};

export function ResultsList() {
  const results = useAppStore((s) => s.searchResults);
  const searchQuery = useAppStore((s) => s.searchQuery);
  const facetFilters = useAppStore((s) => s.facetFilters);
  const setSortBy = useAppStore((s) => s.setSortBy);
  const searching = useAppStore((s) => s.searching);
  const searchError = useAppStore((s) => s.searchError);

  if (searching) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-cool-grey">
        Searching -- the first search after launch also warms up the local search model,
        which can take a few extra seconds.
      </div>
    );
  }

  // Facet filters alone (no typed query) still produce a real newest-first
  // browse (Section 13) -- only show the empty-state hint when there's
  // neither a query nor an active filter to have produced results at all.
  if (!searchQuery.trim() && !hasActiveFacetFilters(facetFilters)) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-cool-grey">
        Search for what's happening in a shot -- e.g. "mascot cheering" or "students studying" -- or browse by filter
        on the left.
      </div>
    );
  }

  // Distinct from "no matches" -- a failed search (timeout, sidecar hiccup)
  // must not look identical to a real empty result set, and must not leave
  // the previous query's results on screen looking unchanged.
  if (searchError) {
    return (
      <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-red-300">
        Search failed: {searchError}
      </div>
    );
  }

  if (results.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-cool-grey">
        No matching shots.
      </div>
    );
  }

  return (
    <div className="flex flex-1 flex-col overflow-y-auto p-6">
      <div className="mb-3 flex shrink-0 items-center justify-between">
        <span className="text-xs text-cool-grey">
          {results.length} result{results.length === 1 ? "" : "s"}
        </span>
        <label className="flex items-center gap-1.5 text-xs text-cool-grey">
          Sort by
          <select
            value={facetFilters.sort_by}
            onChange={(e) => setSortBy(e.target.value as SortBy)}
            className="rounded border border-border-subtle bg-surface-inset px-1.5 py-1 text-xs text-white outline-none focus:border-athletic-blue-light"
          >
            {Object.entries(SORT_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {results.map((r) => (
          <ShotCard key={r.shot_id} result={r} />
        ))}
      </div>
    </div>
  );
}
