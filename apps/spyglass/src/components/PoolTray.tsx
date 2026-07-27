import { useEffect, useRef, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { save } from "@tauri-apps/plugin-dialog";
import { useAppStore } from "../store/useAppStore";
import type { ShotSearchResult } from "../types";
import { ConsolidateExportModal } from "./ConsolidateExportModal";

function PoolChip({ shot, index, onReorder }: { shot: ShotSearchResult; index: number; onReorder: (from: number, to: number) => void }) {
  const removeFromPool = useAppStore((s) => s.removeFromPool);
  const dragIndex = useRef<number | null>(null);

  return (
    <div
      draggable
      onDragStart={() => {
        dragIndex.current = index;
      }}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        if (dragIndex.current !== null && dragIndex.current !== index) {
          onReorder(dragIndex.current, index);
        }
        dragIndex.current = null;
      }}
      className="group relative h-20 w-32 shrink-0 cursor-grab overflow-hidden rounded border border-border-subtle bg-surface-inset active:cursor-grabbing"
      title={shot.caption ?? shot.clip_file_path}
    >
      {shot.keyframe_path ? (
        <img src={convertFileSrc(shot.keyframe_path)} alt="" className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full items-center justify-center text-xs text-cool-grey">No thumbnail</div>
      )}
      <button
        type="button"
        onClick={() => void removeFromPool(shot.shot_id)}
        className="absolute right-1 top-1 rounded bg-surface/90 px-1 text-xs text-white opacity-0 group-hover:opacity-100"
        title="Remove from pool"
      >
        &times;
      </button>
    </div>
  );
}

export function PoolTray() {
  const pool = useAppStore((s) => s.pool);
  const poolOpen = useAppStore((s) => s.poolOpen);
  const setPoolOpen = useAppStore((s) => s.setPoolOpen);
  const refreshPool = useAppStore((s) => s.refreshPool);
  const reorderPool = useAppStore((s) => s.reorderPool);
  const clearPool = useAppStore((s) => s.clearPool);
  const exportPool = useAppStore((s) => s.exportPool);
  const [exporting, setExporting] = useState(false);
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [folderExportOpen, setFolderExportOpen] = useState(false);

  useEffect(() => {
    void refreshPool();
  }, [refreshPool]);

  if (!poolOpen) return null;

  const handleReorder = (from: number, to: number) => {
    const next = [...pool];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    void reorderPool(next.map((s) => s.shot_id));
  };

  const doExport = async () => {
    if (pool.length === 0) return;
    const destination = await save({
      title: "Export Premiere Pro Sequence",
      defaultPath: "Spyglass Pool.xml",
      filters: [{ name: "Final Cut Pro XML", extensions: ["xml"] }],
    });
    if (!destination) return;
    setExporting(true);
    try {
      await exportPool(destination, "Spyglass Pool");
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="fixed inset-x-0 bottom-0 z-10 border-t border-border-subtle bg-surface-raised">
      <div className="flex items-center justify-between px-4 py-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-warm-grey">
          Pool ({pool.length})
        </span>
        <div className="flex items-center gap-2">
          {confirmingClear ? (
            <>
              <span className="text-xs text-warm-grey">Clear the whole pool?</span>
              <button
                type="button"
                onClick={() => {
                  void clearPool();
                  setConfirmingClear(false);
                }}
                className="rounded bg-red-900 px-2 py-1 text-xs text-white hover:bg-red-800"
              >
                Confirm
              </button>
              <button
                type="button"
                onClick={() => setConfirmingClear(false)}
                className="rounded border border-border-subtle px-2 py-1 text-xs text-white"
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmingClear(true)}
              disabled={pool.length === 0}
              className="rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-red-700 disabled:opacity-40"
            >
              Clear
            </button>
          )}
          <button
            type="button"
            onClick={() => void doExport()}
            disabled={pool.length === 0 || exporting}
            className="rounded bg-athletic-blue px-3 py-1 text-xs font-medium text-white hover:bg-athletic-blue-light disabled:opacity-50"
          >
            {exporting ? "Exporting..." : "Export to Premiere Pro"}
          </button>
          <button
            type="button"
            onClick={() => setFolderExportOpen(true)}
            disabled={pool.length === 0}
            className="rounded border border-border-subtle px-3 py-1 text-xs font-medium text-white hover:border-athletic-blue-light disabled:opacity-50"
          >
            Export to Folder
          </button>
          <button type="button" onClick={() => setPoolOpen(false)} className="text-cool-grey hover:text-white">
            Close
          </button>
        </div>
      </div>
      <div className="flex gap-2 overflow-x-auto px-4 pb-3">
        {pool.length === 0 ? (
          <p className="py-6 text-xs text-cool-grey">
            Nothing staged yet -- use "+ Pool" on a search result to add it here.
          </p>
        ) : (
          pool.map((shot, i) => <PoolChip key={shot.shot_id} shot={shot} index={i} onReorder={handleReorder} />)
        )}
      </div>
      {folderExportOpen && <ConsolidateExportModal onClose={() => setFolderExportOpen(false)} />}
    </div>
  );
}
