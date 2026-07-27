import { useEffect, useState } from "react";
import { hasActiveFacetFilters, useAppStore } from "../store/useAppStore";
import type { SourceApp } from "../types";

const SOURCE_LABELS: Record<SourceApp, string> = {
  card_eater: "Card Eater",
  spyglass_scan: "Spyglass scan",
};

/// Collapsible facet sidebar (Section 12/13): browse or narrow results by
/// tag, source, and ingested-date range without typing a query. The
/// schema only backs these three facets -- there's no shot-type column or
/// discrete "source event" grouping to filter on yet (see the scope note
/// in `facets.rs`).
export function FacetSidebar() {
  const open = useAppStore((s) => s.facetSidebarOpen);
  const setOpen = useAppStore((s) => s.setFacetSidebarOpen);
  const options = useAppStore((s) => s.facetOptions);
  const filters = useAppStore((s) => s.facetFilters);
  const refreshFacetOptions = useAppStore((s) => s.refreshFacetOptions);
  const toggleTagFilter = useAppStore((s) => s.toggleTagFilter);
  const setSourceAppFilter = useAppStore((s) => s.setSourceAppFilter);
  const setDateRange = useAppStore((s) => s.setDateRange);
  const clearFacetFilters = useAppStore((s) => s.clearFacetFilters);
  const [tagsOpen, setTagsOpen] = useState(true);

  useEffect(() => {
    void refreshFacetOptions();
  }, [refreshFacetOptions]);

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="shrink-0 border-r border-border-subtle bg-surface-raised px-2 py-4 text-xs text-cool-grey hover:text-white"
        title="Show filters"
      >
        Filters ▸
      </button>
    );
  }

  return (
    <div className="flex w-56 shrink-0 flex-col overflow-y-auto border-r border-border-subtle bg-surface-raised p-3">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-warm-grey">Filters</span>
        <button type="button" onClick={() => setOpen(false)} className="text-xs text-cool-grey hover:text-white" title="Hide filters">
          ◂
        </button>
      </div>

      {hasActiveFacetFilters(filters) && (
        <button
          type="button"
          onClick={clearFacetFilters}
          className="mb-3 self-start rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-athletic-blue-light"
        >
          Clear filters
        </button>
      )}

      <div className="mb-4">
        <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-warm-grey">Date range</p>
        <div className="flex flex-col gap-1.5">
          <input
            type="date"
            value={filters.date_from ?? ""}
            min={options?.earliest_date ?? undefined}
            max={options?.latest_date ?? undefined}
            onChange={(e) => setDateRange(e.target.value || null, filters.date_to)}
            className="rounded border border-border-subtle bg-surface-inset px-1.5 py-1 text-xs text-white outline-none focus:border-athletic-blue-light"
          />
          <input
            type="date"
            value={filters.date_to ?? ""}
            min={options?.earliest_date ?? undefined}
            max={options?.latest_date ?? undefined}
            onChange={(e) => setDateRange(filters.date_from, e.target.value || null)}
            className="rounded border border-border-subtle bg-surface-inset px-1.5 py-1 text-xs text-white outline-none focus:border-athletic-blue-light"
          />
        </div>
      </div>

      <div className="mb-4">
        <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-warm-grey">Source</p>
        <div className="flex flex-col gap-1 text-xs text-white">
          <label className="flex items-center gap-1.5">
            <input type="radio" checked={filters.source_app === null} onChange={() => setSourceAppFilter(null)} />
            All
          </label>
          {(options?.sources ?? []).map((s) => (
            <label key={s.source_app} className="flex items-center gap-1.5">
              <input
                type="radio"
                checked={filters.source_app === s.source_app}
                onChange={() => setSourceAppFilter(s.source_app)}
              />
              {SOURCE_LABELS[s.source_app] ?? s.source_app}
              <span className="text-cool-grey">({s.shot_count})</span>
            </label>
          ))}
        </div>
      </div>

      <div>
        <button
          type="button"
          onClick={() => setTagsOpen(!tagsOpen)}
          className="mb-1.5 flex w-full items-center justify-between text-xs font-medium uppercase tracking-wide text-warm-grey hover:text-white"
          aria-expanded={tagsOpen}
        >
          <span>
            Tags
            {filters.tags.length > 0 && <span className="ml-1 normal-case text-cool-grey">({filters.tags.length} selected)</span>}
          </span>
          <span>{tagsOpen ? "▾" : "▸"}</span>
        </button>
        {tagsOpen && (
          <>
            {options && options.tags.length === 0 && <p className="text-xs text-cool-grey">No tags yet.</p>}
            <div className="flex flex-col gap-1 text-xs text-white">
              {(options?.tags ?? []).map((t) => (
                <label key={t.label} className="flex items-center gap-1.5">
                  <input type="checkbox" checked={filters.tags.includes(t.label)} onChange={() => toggleTagFilter(t.label)} />
                  <span className="truncate" title={t.label}>
                    {t.label}
                  </span>
                  <span className="text-cool-grey">({t.shot_count})</span>
                </label>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
