import { useEffect, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { api } from "../lib/api";
import { useAppStore } from "../store/useAppStore";
import type { BackupInfo, WatchedRoot, WatchedRootStatus } from "../types";

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

function BackupRow({ backup, onRestoring }: { backup: BackupInfo; onRestoring: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const setStatusMessage = useAppStore((s) => s.setStatusMessage);

  const restore = async () => {
    setBusy(true);
    try {
      await api.restoreBackup(backup.path);
      onRestoring();
    } catch (err) {
      setStatusMessage(`Restore failed: ${String(err)}`);
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <div className="flex items-center justify-between gap-2 rounded border border-border-subtle bg-surface-inset px-2 py-1.5 text-xs">
      <div className="min-w-0">
        <p className="truncate text-white" title={backup.file_name}>
          {backup.file_name}
        </p>
        <p className="text-cool-grey">
          {formatBytes(backup.size_bytes)} -- {backup.created_at}
        </p>
      </div>
      {confirming ? (
        <div className="flex shrink-0 items-center gap-1">
          <span className="text-warm-grey">Overwrite the live index and restart?</span>
          <button
            type="button"
            onClick={() => void restore()}
            disabled={busy}
            className="rounded bg-red-900 px-2 py-1 text-white hover:bg-red-800"
          >
            {busy ? "Restoring..." : "Confirm"}
          </button>
          <button
            type="button"
            onClick={() => setConfirming(false)}
            disabled={busy}
            className="rounded border border-border-subtle px-2 py-1 text-white"
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setConfirming(true)}
          className="shrink-0 rounded border border-border-subtle px-2 py-1 text-white hover:border-athletic-blue-light"
        >
          Restore
        </button>
      )}
    </div>
  );
}

function MaintenanceSection() {
  const setStatusMessage = useAppStore((s) => s.setStatusMessage);
  const [backups, setBackups] = useState<BackupInfo[]>([]);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [confirmingRebuild, setConfirmingRebuild] = useState(false);
  const [confirmingPurgeTags, setConfirmingPurgeTags] = useState(false);
  const [restoring, setRestoring] = useState(false);

  const refreshBackups = async () => {
    try {
      setBackups(await api.listBackups());
    } catch {
      // Backups directory may not exist yet on a brand-new install -- fine.
      setBackups([]);
    }
  };

  useEffect(() => {
    void refreshBackups();
  }, []);

  const backupNow = async () => {
    setBusyAction("backup");
    try {
      const info = await api.backupIndexNow();
      setStatusMessage(`Index backed up: ${info.file_name} (${formatBytes(info.size_bytes)}).`);
      await refreshBackups();
    } catch (err) {
      setStatusMessage(`Backup failed: ${String(err)}`);
    } finally {
      setBusyAction(null);
    }
  };

  const checkIntegrity = async () => {
    setBusyAction("integrity");
    try {
      const issues = await api.checkIndexIntegrity();
      setStatusMessage(
        issues.length === 1 && issues[0] === "ok"
          ? "Index integrity check: clean, no issues found."
          : `Index integrity check found ${issues.length} issue(s): ${issues.join("; ")}`,
      );
    } catch (err) {
      setStatusMessage(`Integrity check failed: ${String(err)}`);
    } finally {
      setBusyAction(null);
    }
  };

  const rebuildSearchIndex = async () => {
    setBusyAction("rebuild");
    try {
      await api.rebuildSearchIndex();
      setStatusMessage("Search index rebuilt.");
    } catch (err) {
      setStatusMessage(`Rebuild failed: ${String(err)}`);
    } finally {
      setBusyAction(null);
      setConfirmingRebuild(false);
    }
  };

  // Retroactive cleanup for tags the VLM pass generated before TAGS_PROMPT
  // (sidecar/analyze_clip.py) told it not to transcribe on-screen text or
  // describe/count subjects' gender -- see
  // spyglass_core::db::purge_onscreen_text_tags, purge_ui_text_tags, and
  // purge_gender_tags's doc comments for the full rationale and scope
  // limits (digit-containing, onscreen-text-shaped (quoted/UI-role-suffixed/
  // sentence-punctuated), or gender/headcount spyglass_vlm tags only;
  // doesn't touch human tags or catch a digit-free, unpunctuated, un-quoted
  // bare-word name/text leak).
  const purgeBadTags = async () => {
    setBusyAction("purgeTags");
    try {
      const removed = await api.purgeBadTags();
      setStatusMessage(`Removed ${removed} on-screen-text/gender tag${removed === 1 ? "" : "s"}.`);
    } catch (err) {
      setStatusMessage(`Purge failed: ${String(err)}`);
    } finally {
      setBusyAction(null);
      setConfirmingPurgeTags(false);
    }
  };

  if (restoring) {
    return (
      <div className="mt-4 border-t border-border-subtle pt-3">
        <p className="text-xs text-warm-grey">Restarting Spyglass to finish restoring the index...</p>
      </div>
    );
  }

  return (
    <div className="mt-4 border-t border-border-subtle pt-3">
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-warm-grey">Index Maintenance</p>

      <div className="mb-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void backupNow()}
          disabled={busyAction !== null}
          className="rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-athletic-blue-light disabled:opacity-40"
        >
          {busyAction === "backup" ? "Backing up..." : "Back up index now"}
        </button>
        <button
          type="button"
          onClick={() => void checkIntegrity()}
          disabled={busyAction !== null}
          className="rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-athletic-blue-light disabled:opacity-40"
        >
          {busyAction === "integrity" ? "Checking..." : "Check integrity"}
        </button>
        {confirmingRebuild ? (
          <>
            <span className="text-xs text-warm-grey">Rebuild search index now?</span>
            <button
              type="button"
              onClick={() => void rebuildSearchIndex()}
              disabled={busyAction !== null}
              className="rounded bg-athletic-blue px-2 py-1 text-xs text-white hover:bg-athletic-blue-light"
            >
              {busyAction === "rebuild" ? "Rebuilding..." : "Confirm"}
            </button>
            <button
              type="button"
              onClick={() => setConfirmingRebuild(false)}
              className="rounded border border-border-subtle px-2 py-1 text-xs text-white"
            >
              Cancel
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmingRebuild(true)}
            disabled={busyAction !== null}
            className="rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-athletic-blue-light disabled:opacity-40"
          >
            Rebuild search index
          </button>
        )}
        {confirmingPurgeTags ? (
          <>
            <span className="text-xs text-warm-grey">
              Purge on-screen-text tags (jersey numbers, scores, signs, quoted UI text like buttons/logos) and
              gender/headcount tags (boy/girl counts) archive-wide? Your own tags aren&apos;t affected.
            </span>
            <button
              type="button"
              onClick={() => void purgeBadTags()}
              disabled={busyAction !== null}
              className="rounded bg-athletic-blue px-2 py-1 text-xs text-white hover:bg-athletic-blue-light"
            >
              {busyAction === "purgeTags" ? "Purging..." : "Confirm"}
            </button>
            <button
              type="button"
              onClick={() => setConfirmingPurgeTags(false)}
              className="rounded border border-border-subtle px-2 py-1 text-xs text-white"
            >
              Cancel
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmingPurgeTags(true)}
            disabled={busyAction !== null}
            className="rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-athletic-blue-light disabled:opacity-40"
          >
            Purge on-screen text tags
          </button>
        )}
      </div>

      {backups.length > 0 && (
        <div className="space-y-1.5">
          {backups.map((backup) => (
            <BackupRow key={backup.path} backup={backup} onRestoring={() => setRestoring(true)} />
          ))}
        </div>
      )}
    </div>
  );
}

function AccessBadge({ level }: { level: WatchedRoot["access_level"] }) {
  const styles: Record<WatchedRoot["access_level"], string> = {
    active: "bg-athletic-blue-light text-white",
    paused: "bg-warm-grey text-athletic-blue",
    removed: "bg-surface-inset text-cool-grey",
  };
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-medium ${styles[level]}`}>{level}</span>
  );
}

function OfflineBadge({ label }: { label: string }) {
  return (
    <span
      className="rounded bg-red-950 px-2 py-0.5 text-xs font-medium text-red-300"
      title={`Reconnect ${label} to resume indexing and open source clips`}
    >
      offline -- reconnect {label}
    </span>
  );
}

function AddRootForm({ onAdded }: { onAdded: () => void }) {
  const [label, setLabel] = useState("");
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const setStatusMessage = useAppStore((s) => s.setStatusMessage);

  const browse = async () => {
    const selected = await open({ directory: true, multiple: false });
    if (typeof selected === "string") {
      setPath(selected);
      if (!label) setLabel(selected.split("/").filter(Boolean).pop() ?? selected);
    }
  };

  const submit = async () => {
    if (!label.trim() || !path.trim()) return;
    setBusy(true);
    try {
      await api.addWatchedRoot(label.trim(), path.trim());
      setLabel("");
      setPath("");
      onAdded();
    } catch (err) {
      setStatusMessage(`Could not add watched root: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mb-4 rounded-lg border border-border-subtle bg-surface-inset p-3">
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-warm-grey">
        Add watched folder
      </p>
      <div className="flex flex-col gap-2">
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="Label (e.g. Fall 2025 Season -- Drive 4)"
          className="rounded border border-border-subtle bg-surface px-2 py-1.5 text-sm text-white placeholder-cool-grey outline-none focus:border-athletic-blue-light"
        />
        <div className="flex gap-2">
          <input
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/Volumes/..."
            className="flex-1 rounded border border-border-subtle bg-surface px-2 py-1.5 text-sm text-white placeholder-cool-grey outline-none focus:border-athletic-blue-light"
          />
          <button
            type="button"
            onClick={() => void browse()}
            className="rounded border border-border-subtle px-3 py-1.5 text-xs text-white hover:border-athletic-blue-light"
          >
            Browse...
          </button>
        </div>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy || !label.trim() || !path.trim()}
          className="rounded bg-athletic-blue px-3 py-1.5 text-xs font-medium text-white hover:bg-athletic-blue-light disabled:opacity-50"
        >
          Add to Spyglass
        </button>
      </div>
    </div>
  );
}

function RootRow({ root, onChanged }: { root: WatchedRootStatus; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const [confirmingReset, setConfirmingReset] = useState(false);
  const setStatusMessage = useAppStore((s) => s.setStatusMessage);
  const { discovered, indexed, queued, failed, awaiting_reconnect: awaitingReconnect } = root.progress;

  const scanNow = async () => {
    setBusy(true);
    try {
      const scan = await api.scanWatchedRoot(root.id);
      const transcripts = await api.scanTranscriberSidecars(root.path);
      const broll = await api.scanBrollCache(root.path);
      const excludedRemoved = scan.excluded_removed + transcripts.excluded_removed + broll.excluded_removed;
      setStatusMessage(
        `${root.label}: ${scan.registered} new file(s), ${transcripts.registered} interview transcript(s), ${broll.registered} b-roll-analyzed clip(s) registered` +
          (scan.relinked > 0 ? `, ${scan.relinked} relinked from a previous location` : "") +
          (excludedRemoved > 0
            ? `, ${excludedRemoved} skipped (previously removed).`
            : "."),
      );
      onChanged();
    } catch (err) {
      setStatusMessage(`Scan failed for ${root.label}: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const relink = async () => {
    const selected = await open({ directory: true, multiple: false });
    if (typeof selected !== "string") return;
    setBusy(true);
    try {
      await api.relinkWatchedRoot(root.id, selected);
      // Scan the freshly-chosen folder directly rather than reusing
      // `root.path` -- this component's props won't reflect the new path
      // until `onChanged()` triggers a re-fetch below, so `root.path` here
      // is still the just-relinked-away-from old location.
      const scan = await api.scanWatchedRoot(root.id);
      const transcripts = await api.scanTranscriberSidecars(selected);
      const broll = await api.scanBrollCache(selected);
      setStatusMessage(
        `${root.label}: relinked to ${selected} -- ${scan.relinked} clip(s) reconnected` +
          (scan.registered > 0 || transcripts.registered > 0 || broll.registered > 0
            ? `, ${scan.registered} new file(s), ${transcripts.registered} interview transcript(s), ${broll.registered} b-roll-analyzed clip(s) registered.`
            : "."),
      );
      onChanged();
    } catch (err) {
      setStatusMessage(`Could not relink ${root.label}: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const retryFailed = async () => {
    setBusy(true);
    try {
      const retried = await api.retryFailedJobs(root.id);
      setStatusMessage(`${root.label}: ${retried} failed item(s) queued for another attempt.`);
      onChanged();
    } catch (err) {
      setStatusMessage(`Could not retry failed items for ${root.label}: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const togglePause = async () => {
    setBusy(true);
    try {
      await api.setWatchedRootAccessLevel(root.id, root.access_level === "active" ? "paused" : "active");
      onChanged();
    } catch (err) {
      setStatusMessage(`Could not update ${root.label}: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await api.removeWatchedRoot(root.id);
      onChanged();
    } catch (err) {
      setStatusMessage(`Could not remove ${root.label}: ${String(err)}`);
    } finally {
      setBusy(false);
      setConfirmingRemove(false);
    }
  };

  // "Start fresh" for just this folder: wipes every clip/shot/tag/caption
  // indexed under it (see spyglass_core::db::reset_watched_root's doc
  // comment -- the TAGS_PROMPT prompt-echo bug this exists for), then
  // immediately rescans so it's re-registered and re-analyzed with the
  // current pipeline, same "reset, then scan" shape as `relink` above.
  // Unlike `remove`, the root itself stays active -- only its index resets.
  const reset = async () => {
    setBusy(true);
    try {
      const removed = await api.resetWatchedRoot(root.id);
      const scan = await api.scanWatchedRoot(root.id);
      const transcripts = await api.scanTranscriberSidecars(root.path);
      const broll = await api.scanBrollCache(root.path);
      setStatusMessage(
        `${root.label}: cleared ${removed} clip(s), rescanned -- ` +
          `${scan.registered} file(s), ${transcripts.registered} interview transcript(s), ` +
          `${broll.registered} b-roll-analyzed clip(s) re-registered.`,
      );
      onChanged();
    } catch (err) {
      setStatusMessage(`Could not reset ${root.label}: ${String(err)}`);
    } finally {
      setBusy(false);
      setConfirmingReset(false);
    }
  };

  return (
    <div className="rounded-lg border border-border-subtle bg-surface-raised p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-medium text-white">{root.label}</span>
            <AccessBadge level={root.access_level} />
            {!root.is_online && <OfflineBadge label={root.label} />}
          </div>
          <p className="truncate text-xs text-cool-grey" title={root.path}>
            {root.path}
          </p>
          <p className="text-xs text-cool-grey">
            {root.last_scanned_at ? `Last scanned ${root.last_scanned_at}` : "Never scanned"}
          </p>
          {discovered > 0 && (
            <p className="mt-1 text-xs text-warm-grey">
              {indexed.toLocaleString()} / {discovered.toLocaleString()} indexed
              {queued > 0 && `, ${queued.toLocaleString()} queued`}
              {failed > 0 && `, ${failed.toLocaleString()} failed`}
              {awaitingReconnect > 0 && `, ${awaitingReconnect.toLocaleString()} awaiting reconnect`}
            </p>
          )}
        </div>
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void scanNow()}
          disabled={busy || root.access_level !== "active"}
          className="rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-athletic-blue-light disabled:opacity-40"
        >
          Scan now
        </button>
        <button
          type="button"
          onClick={() => void togglePause()}
          disabled={busy || root.access_level === "removed"}
          className="rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-athletic-blue-light disabled:opacity-40"
        >
          {root.access_level === "active" ? "Pause" : "Resume"}
        </button>
        <button
          type="button"
          onClick={() => void relink()}
          disabled={busy || root.access_level === "removed"}
          title="Point this root at a new folder (e.g. after moving it to an archive drive) and reconnect its clips by content match"
          className="rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-athletic-blue-light disabled:opacity-40"
        >
          Relink...
        </button>
        {failed > 0 && (
          <button
            type="button"
            onClick={() => void retryFailed()}
            disabled={busy}
            className="rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-athletic-blue-light disabled:opacity-40"
          >
            Retry {failed} failed
          </button>
        )}
        {confirmingReset ? (
          <>
            <span className="text-xs text-warm-grey">
              Clear this folder's index and rescan it from scratch with the current pipeline?
            </span>
            <button
              type="button"
              onClick={() => void reset()}
              disabled={busy}
              className="rounded bg-red-900 px-2 py-1 text-xs text-white hover:bg-red-800"
            >
              {busy ? "Resetting..." : "Confirm reset"}
            </button>
            <button
              type="button"
              onClick={() => setConfirmingReset(false)}
              className="rounded border border-border-subtle px-2 py-1 text-xs text-white"
            >
              Cancel
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmingReset(true)}
            disabled={busy || root.access_level === "removed"}
            title="Wipe this folder's indexed clips/tags/captions and rescan it from scratch -- use after a tagging pipeline fix to re-tag just this folder, without touching any other watched folder"
            className="rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-athletic-blue-light disabled:opacity-40"
          >
            Reset &amp; rescan
          </button>
        )}
        {confirmingRemove ? (
          <>
            <span className="text-xs text-warm-grey">Purge all indexed content from this root?</span>
            <button
              type="button"
              onClick={() => void remove()}
              disabled={busy}
              className="rounded bg-red-900 px-2 py-1 text-xs text-white hover:bg-red-800"
            >
              Confirm remove
            </button>
            <button
              type="button"
              onClick={() => setConfirmingRemove(false)}
              className="rounded border border-border-subtle px-2 py-1 text-xs text-white"
            >
              Cancel
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmingRemove(true)}
            disabled={busy || root.access_level === "removed"}
            className="rounded border border-border-subtle px-2 py-1 text-xs text-white hover:border-red-700 disabled:opacity-40"
          >
            Remove
          </button>
        )}
      </div>
    </div>
  );
}

export function WatchedRootsPanel() {
  const watchedRoots = useAppStore((s) => s.watchedRoots);
  // "Removed" rows persist server-side purely so their path stays excluded
  // from future re-scans (see `scanner::is_under_a_removed_root`) -- they
  // have no reason to keep showing up here once the user has removed them.
  const visibleRoots = watchedRoots.filter((root) => root.access_level !== "removed");
  const refreshWatchedRoots = useAppStore((s) => s.refreshWatchedRoots);
  const setSettingsOpen = useAppStore((s) => s.setSettingsOpen);
  const setStatusMessage = useAppStore((s) => s.setStatusMessage);
  const queuePaused = useAppStore((s) => s.queuePaused);
  const refreshQueuePaused = useAppStore((s) => s.refreshQueuePaused);
  const toggleQueuePaused = useAppStore((s) => s.toggleQueuePaused);
  const backgroundWorkStatus = useAppStore((s) => s.backgroundWorkStatus);
  const refreshBackgroundWorkStatus = useAppStore((s) => s.refreshBackgroundWorkStatus);
  const [syncingCardEater, setSyncingCardEater] = useState(false);

  useEffect(() => {
    void refreshWatchedRoots();
    void refreshQueuePaused();
    void refreshBackgroundWorkStatus();
    // Idle-gated status changes second-to-second as the user touches (or
    // stops touching) the keyboard/mouse -- a one-shot fetch on mount
    // would go stale the moment it did, showing "Running" or "Paused"
    // long after the real gate flipped the other way.
    const interval = setInterval(() => void refreshBackgroundWorkStatus(), 3000);
    return () => clearInterval(interval);
  }, [refreshWatchedRoots, refreshQueuePaused, refreshBackgroundWorkStatus]);

  const idleGated =
    !queuePaused &&
    backgroundWorkStatus != null &&
    backgroundWorkStatus.idle_seconds != null &&
    backgroundWorkStatus.idle_seconds < backgroundWorkStatus.min_idle_seconds;
  const secondsUntilIdle = idleGated
    ? Math.ceil(backgroundWorkStatus!.min_idle_seconds - backgroundWorkStatus!.idle_seconds!)
    : 0;

  const syncCardEater = async () => {
    setSyncingCardEater(true);
    try {
      const result = await api.scanCardEater();
      setStatusMessage(`Card Eater sync: ${result.registered} new clip(s) registered.`);
      void refreshWatchedRoots();
    } catch (err) {
      setStatusMessage(`Card Eater sync failed: ${String(err)}`);
    } finally {
      setSyncingCardEater(false);
    }
  };

  return (
    <div className="fixed inset-y-0 right-0 z-10 flex w-96 flex-col border-l border-border-subtle bg-surface p-4 shadow-xl">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-warm-grey">
          Watched Folders
        </h2>
        <button
          type="button"
          onClick={() => setSettingsOpen(false)}
          className="text-cool-grey hover:text-white"
        >
          Close
        </button>
      </div>

      <button
        type="button"
        onClick={() => void toggleQueuePaused()}
        className="mb-2 flex items-center justify-between rounded border border-border-subtle px-3 py-2 text-xs text-white hover:border-athletic-blue-light"
        title={
          idleGated
            ? "Indexing pauses automatically while you're actively using this machine -- it'll resume once you've been idle for a bit"
            : "Background indexing also pauses automatically while you're actively using this machine, and resumes at idle"
        }
      >
        <span>Background indexing</span>
        <span className={queuePaused ? "text-warm-grey" : idleGated ? "text-warm-grey" : "text-athletic-blue-light"}>
          {queuePaused
            ? "Paused (tap to resume)"
            : idleGated
              ? `Waiting for idle (~${secondsUntilIdle}s) -- tap to pause`
              : "Running -- tap to pause"}
        </span>
      </button>

      <button
        type="button"
        onClick={() => void syncCardEater()}
        disabled={syncingCardEater}
        className="mb-4 rounded border border-border-subtle px-3 py-2 text-xs text-white hover:border-athletic-blue-light disabled:opacity-50"
      >
        {syncingCardEater ? "Syncing..." : "Sync from Card Eater"}
      </button>

      <AddRootForm onAdded={() => void refreshWatchedRoots()} />

      <div className="flex-1 space-y-2 overflow-y-auto">
        {visibleRoots.length === 0 ? (
          <p className="text-xs text-cool-grey">
            No folders approved yet. By default Spyglass has zero filesystem visibility --
            add a folder above to start indexing it.
          </p>
        ) : (
          visibleRoots.map((root) => (
            <RootRow key={root.id} root={root} onChanged={() => void refreshWatchedRoots()} />
          ))
        )}
      </div>

      <MaintenanceSection />
    </div>
  );
}
