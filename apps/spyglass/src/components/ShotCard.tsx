import { useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import { useAppStore } from "../store/useAppStore";
import type { ShotSearchResult } from "../types";
import { ShotPreviewPlayer } from "./ShotPreviewPlayer";

function formatTimecode(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function ScoreBadge({ label, value }: { label: string; value: number }) {
  return (
    <span className="rounded bg-surface-inset px-1.5 py-0.5 text-xs text-warm-grey">
      {label} {Math.round(value)}
    </span>
  );
}

/// Inline tag-correction affordance (Section 13): remove a wrong tag,
/// add a missing one -- feeds straight back into the `tags` table.
function TagEditor({ result }: { result: ShotSearchResult }) {
  const addTag = useAppStore((s) => s.addTag);
  const removeTag = useAppStore((s) => s.removeTag);
  const [draft, setDraft] = useState("");

  const submit = () => {
    if (!draft.trim()) return;
    void addTag(result.shot_id, draft);
    setDraft("");
  };

  return (
    <div className="mt-2 flex flex-wrap items-center gap-1">
      {result.tags.map((tag) => (
        <span
          key={tag}
          className="group flex items-center gap-1 rounded bg-athletic-blue-light px-1.5 py-0.5 text-xs text-white"
        >
          {tag}
          <button
            type="button"
            onClick={() => void removeTag(result.shot_id, tag)}
            className="text-white/60 hover:text-white"
            title={`Remove "${tag}"`}
          >
            &times;
          </button>
        </span>
      ))}
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          }
        }}
        placeholder="+ tag"
        className="w-14 rounded border border-border-subtle bg-surface-inset px-1.5 py-0.5 text-xs text-white placeholder-cool-grey outline-none focus:border-athletic-blue-light focus:w-24"
      />
    </div>
  );
}

/// Static keyframe thumbnail with a click-to-play overlay. Previously this
/// cycled between pre-extracted scrub frames on hover (Finder icon-view
/// style); that was removed in favor of a single static thumbnail per shot.
function ShotPreview({ result, onOpenPlayer }: { result: ShotSearchResult; onOpenPlayer: () => void }) {
  const [hovering, setHovering] = useState(false);

  return (
    <div
      className="relative aspect-video w-full cursor-pointer overflow-hidden bg-surface-inset"
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
      onClick={onOpenPlayer}
      title="Click to play"
    >
      {result.keyframe_path ? (
        <img
          src={convertFileSrc(result.keyframe_path)}
          alt={result.caption ?? "Shot keyframe"}
          className="absolute inset-0 h-full w-full object-cover"
        />
      ) : (
        <div className="flex h-full items-center justify-center text-xs text-cool-grey">No thumbnail yet</div>
      )}
      <div
        className={`absolute inset-0 flex items-center justify-center bg-black/20 transition-opacity duration-150 ${
          hovering ? "opacity-100" : "opacity-0"
        }`}
      >
        <span className="flex h-10 w-10 items-center justify-center rounded-full bg-black/50 text-lg text-white">
          &#9654;
        </span>
      </div>
    </div>
  );
}

export function ShotCard({ result, inPool = false }: { result: ShotSearchResult; inPool?: boolean }) {
  const addToPool = useAppStore((s) => s.addToPool);
  const removeFromPool = useAppStore((s) => s.removeFromPool);
  const findSimilarTo = useAppStore((s) => s.findSimilarTo);
  const toggleFavorite = useAppStore((s) => s.toggleFavorite);
  const pool = useAppStore((s) => s.pool);
  const isStaged = inPool || pool.some((s) => s.shot_id === result.shot_id);
  const [playerOpen, setPlayerOpen] = useState(false);

  const reveal = () => {
    void revealItemInDir(result.clip_file_path).catch(() => {
      // Most likely the source drive is offline -- non-fatal, just a no-op from the user's perspective.
    });
  };

  return (
    <div className="overflow-hidden rounded-lg border border-border-subtle bg-surface-raised">
      <div className="relative">
        <ShotPreview result={result} onOpenPlayer={() => setPlayerOpen(true)} />
        <div className="absolute left-2 top-2 flex items-center gap-1">
          <button
            type="button"
            onClick={() => void toggleFavorite(result.shot_id, !result.is_favorite)}
            className={`flex h-7 w-7 items-center justify-center rounded bg-surface/90 text-sm shadow hover:bg-athletic-blue-light ${
              result.is_favorite ? "text-amber-400" : "text-white"
            }`}
            title={result.is_favorite ? "Remove from favorites" : "Add to favorites"}
          >
            {result.is_favorite ? "★" : "☆"}
          </button>
          <button
            type="button"
            onClick={reveal}
            className="rounded bg-surface/90 px-2 py-1 text-xs font-medium text-white shadow hover:bg-athletic-blue-light"
            title="Reveal source file in Finder"
          >
            Reveal
          </button>
        </div>
        <button
          type="button"
          onClick={() => void (isStaged ? removeFromPool(result.shot_id) : addToPool(result.shot_id))}
          className={`absolute right-2 top-2 rounded px-2 py-1 text-xs font-medium shadow ${
            isStaged ? "bg-athletic-blue-light text-white" : "bg-surface/90 text-white hover:bg-athletic-blue-light"
          }`}
          title={isStaged ? "Remove from pool" : "Add to pool"}
        >
          {isStaged ? "In pool" : "+ Pool"}
        </button>
      </div>
      <div className="p-3">
        <div className="mb-1.5 flex items-center justify-between text-xs text-warm-grey">
          <span>
            {formatTimecode(result.start_tc)}-{formatTimecode(result.end_tc)}
          </span>
          <div className="flex gap-1">
            {result.technical_quality_score != null && (
              <ScoreBadge label="Quality" value={result.technical_quality_score} />
            )}
            {result.energy_score != null && <ScoreBadge label="Energy" value={result.energy_score} />}
          </div>
        </div>
        {result.caption && <p className="text-sm text-white">{result.caption}</p>}
        <TagEditor result={result} />
        <div className="mt-2 flex items-center justify-between gap-2">
          <p className="truncate text-xs text-cool-grey" title={result.clip_file_path}>
            {result.clip_file_path}
          </p>
          <button
            type="button"
            onClick={() => void findSimilarTo(result)}
            className="shrink-0 text-xs text-cool-grey hover:text-athletic-blue-light"
            title="Find shots that look like this one"
          >
            Find similar
          </button>
        </div>
      </div>
      {playerOpen && <ShotPreviewPlayer result={result} onClose={() => setPlayerOpen(false)} />}
    </div>
  );
}
