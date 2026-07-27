import { useEffect, useRef, useState } from "react";
import { open, save } from "@tauri-apps/plugin-dialog";
import { api } from "../lib/api";
import type { ConsolidateEstimate, ConsolidateExportStatus, CopyMode, FolderStructure, TrimPrecision } from "../types";

function formatBytes(bytes: number): string {
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / Math.pow(1024, exponent);
  return `${value.toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`;
}

type Step = "options" | "confirm" | "exporting" | "done" | "error";

export function ConsolidateExportModal({ onClose }: { onClose: () => void }) {
  const [step, setStep] = useState<Step>("options");
  const [destinationPath, setDestinationPath] = useState<string | null>(null);
  const [poolName, setPoolName] = useState("Spyglass Pool");
  const [copyModeKind, setCopyModeKind] = useState<"full_source" | "trimmed">("full_source");
  const [handleSeconds, setHandleSeconds] = useState(1);
  const [precision, setPrecision] = useState<TrimPrecision>("stream_copy");
  const [folderStructure, setFolderStructure] = useState<FolderStructure>("flat");
  const [estimate, setEstimate] = useState<ConsolidateEstimate | null>(null);
  const [status, setStatus] = useState<ConsolidateExportStatus | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [xmlExporting, setXmlExporting] = useState(false);
  const [xmlExportedTo, setXmlExportedTo] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const buildCopyMode = (): CopyMode =>
    copyModeKind === "full_source" ? { mode: "full_source" } : { mode: "trimmed", handle_seconds: handleSeconds, precision };

  const chooseDestination = async () => {
    const chosen = await open({ directory: true, title: "Choose Export Destination Folder" });
    if (typeof chosen === "string") setDestinationPath(chosen);
  };

  const reviewExport = async () => {
    if (!destinationPath) return;
    setBusy(true);
    setErrorMessage(null);
    try {
      const result = await api.estimateConsolidateExport(destinationPath, buildCopyMode());
      setEstimate(result);
      setStep("confirm");
    } catch (err) {
      setErrorMessage(String(err));
    } finally {
      setBusy(false);
    }
  };

  const startExport = async () => {
    if (!destinationPath) return;
    setBusy(true);
    setErrorMessage(null);
    try {
      await api.startConsolidateExport(destinationPath, poolName, buildCopyMode(), folderStructure);
      setStep("exporting");
      pollRef.current = setInterval(async () => {
        try {
          const s = await api.getConsolidateExportStatus();
          setStatus(s);
          if (s?.finished) {
            if (pollRef.current) clearInterval(pollRef.current);
            setStep(s.error ? "error" : "done");
            if (s.error) setErrorMessage(s.error);
          }
        } catch {
          // Transient poll failure -- next tick tries again.
        }
      }, 500);
    } catch (err) {
      setErrorMessage(String(err));
      setStep("error");
    } finally {
      setBusy(false);
    }
  };

  const exportCopiedXml = async () => {
    const path = await save({
      title: "Export Premiere Pro Sequence (Copied Files)",
      defaultPath: `${poolName}.xml`,
      filters: [{ name: "Final Cut Pro XML", extensions: ["xml"] }],
    });
    if (!path) return;
    setXmlExporting(true);
    try {
      const written = await api.exportCopiedFilesToPremiereXml(path, poolName);
      setXmlExportedTo(written);
    } catch (err) {
      setErrorMessage(String(err));
    } finally {
      setXmlExporting(false);
    }
  };

  const sizeWarning =
    estimate && estimate.total_bytes > estimate.available_bytes
      ? `Warning: the estimated export size (${formatBytes(estimate.total_bytes)}) exceeds the ${formatBytes(estimate.available_bytes)} free at the destination.`
      : null;

  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-lg rounded border border-border-subtle bg-surface-raised p-5 text-sm text-white">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold">Export to Folder</h2>
          <button type="button" onClick={onClose} className="text-cool-grey hover:text-white">
            &times;
          </button>
        </div>

        {step === "options" && (
          <div className="space-y-4">
            <div>
              <label className="mb-1 block text-xs uppercase tracking-wide text-warm-grey">Destination Folder</label>
              <div className="flex items-center gap-2">
                <span className="flex-1 truncate rounded border border-border-subtle bg-surface-inset px-2 py-1 text-xs text-cool-grey">
                  {destinationPath ?? "No folder chosen"}
                </span>
                <button
                  type="button"
                  onClick={() => void chooseDestination()}
                  className="rounded border border-border-subtle px-2 py-1 text-xs hover:border-athletic-blue-light"
                >
                  Choose...
                </button>
              </div>
            </div>

            <div>
              <label className="mb-1 block text-xs uppercase tracking-wide text-warm-grey">Export Name</label>
              <input
                type="text"
                value={poolName}
                onChange={(e) => setPoolName(e.target.value)}
                className="w-full rounded border border-border-subtle bg-surface-inset px-2 py-1 text-xs text-white"
              />
              <p className="mt-1 text-xs text-cool-grey">Used as a prefix on every copied filename.</p>
            </div>

            <div>
              <label className="mb-1 block text-xs uppercase tracking-wide text-warm-grey">Copy Mode</label>
              <div className="space-y-2">
                <label className="flex items-start gap-2">
                  <input
                    type="radio"
                    checked={copyModeKind === "full_source"}
                    onChange={() => setCopyModeKind("full_source")}
                    className="mt-1"
                  />
                  <span>
                    <span className="block">Full source clip</span>
                    <span className="block text-xs text-cool-grey">Copies the entire original file, untouched.</span>
                  </span>
                </label>
                <label className="flex items-start gap-2">
                  <input
                    type="radio"
                    checked={copyModeKind === "trimmed"}
                    onChange={() => setCopyModeKind("trimmed")}
                    className="mt-1"
                  />
                  <span>
                    <span className="block">Trimmed to shot (+ handle)</span>
                    <span className="block text-xs text-cool-grey">Copies only the selected range plus a retrim buffer.</span>
                  </span>
                </label>
              </div>
            </div>

            {copyModeKind === "trimmed" && (
              <div className="rounded border border-border-subtle bg-surface-inset p-3 space-y-3">
                <div>
                  <label className="mb-1 block text-xs uppercase tracking-wide text-warm-grey">Handle (seconds)</label>
                  <input
                    type="number"
                    min={0}
                    step={0.5}
                    value={handleSeconds}
                    onChange={(e) => setHandleSeconds(Math.max(0, Number(e.target.value)))}
                    className="w-24 rounded border border-border-subtle bg-surface px-2 py-1 text-xs text-white"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs uppercase tracking-wide text-warm-grey">Precision</label>
                  <div className="space-y-1">
                    <label className="flex items-start gap-2">
                      <input
                        type="radio"
                        checked={precision === "stream_copy"}
                        onChange={() => setPrecision("stream_copy")}
                        className="mt-1"
                      />
                      <span>
                        <span className="block">Stream copy (fast, may snap to nearest keyframe)</span>
                      </span>
                    </label>
                    <label className="flex items-start gap-2">
                      <input
                        type="radio"
                        checked={precision === "re_encode"}
                        onChange={() => setPrecision("re_encode")}
                        className="mt-1"
                      />
                      <span>
                        <span className="block">Re-encode (frame-accurate, slower, recompresses)</span>
                      </span>
                    </label>
                  </div>
                </div>
              </div>
            )}

            <div>
              <label className="mb-1 block text-xs uppercase tracking-wide text-warm-grey">Folder Structure</label>
              <div className="space-y-1">
                <label className="flex items-center gap-2">
                  <input type="radio" checked={folderStructure === "flat"} onChange={() => setFolderStructure("flat")} />
                  <span>Flat</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    checked={folderStructure === "subfolder_per_tag"}
                    onChange={() => setFolderStructure("subfolder_per_tag")}
                  />
                  <span>Subfolder per tag</span>
                </label>
              </div>
            </div>

            {errorMessage && <p className="text-xs text-red-400">{errorMessage}</p>}

            <div className="flex justify-end gap-2 pt-2">
              <button type="button" onClick={onClose} className="rounded border border-border-subtle px-3 py-1 text-xs">
                Cancel
              </button>
              <button
                type="button"
                disabled={!destinationPath || busy}
                onClick={() => void reviewExport()}
                className="rounded bg-athletic-blue px-3 py-1 text-xs font-medium hover:bg-athletic-blue-light disabled:opacity-50"
              >
                {busy ? "Checking..." : "Review Export"}
              </button>
            </div>
          </div>
        )}

        {step === "confirm" && estimate && (
          <div className="space-y-4">
            <div className="rounded border border-border-subtle bg-surface-inset p-3 text-xs">
              <p>
                <span className="text-warm-grey">Files: </span>
                {estimate.file_count}
              </p>
              <p>
                <span className="text-warm-grey">Estimated size: </span>
                {formatBytes(estimate.total_bytes)}
              </p>
              <p>
                <span className="text-warm-grey">Free space at destination: </span>
                {formatBytes(estimate.available_bytes)}
              </p>
              <p className="mt-2 break-all">
                <span className="text-warm-grey">Destination: </span>
                {destinationPath}
              </p>
            </div>

            {sizeWarning && <p className="text-xs text-red-400">{sizeWarning}</p>}
            {estimate.destination_has_existing_files && (
              <p className="text-xs text-red-400">Warning: this folder already contains files.</p>
            )}

            {errorMessage && <p className="text-xs text-red-400">{errorMessage}</p>}

            <div className="flex justify-end gap-2 pt-2">
              <button type="button" onClick={() => setStep("options")} className="rounded border border-border-subtle px-3 py-1 text-xs">
                Back
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void startExport()}
                className="rounded bg-athletic-blue px-3 py-1 text-xs font-medium hover:bg-athletic-blue-light disabled:opacity-50"
              >
                {busy ? "Starting..." : "Start Export"}
              </button>
            </div>
          </div>
        )}

        {step === "exporting" && (
          <div className="space-y-3">
            <p className="text-xs text-warm-grey">
              Copying {status ? `${status.completed} / ${status.total}` : "..."}
            </p>
            <div className="h-2 w-full overflow-hidden rounded bg-surface-inset">
              <div
                className="h-full bg-athletic-blue-light transition-all"
                style={{ width: status && status.total > 0 ? `${(100 * status.completed) / status.total}%` : "0%" }}
              />
            </div>
            {status?.current_file && <p className="truncate text-xs text-cool-grey">{status.current_file}</p>}
          </div>
        )}

        {step === "done" && status?.manifest && (
          <div className="space-y-4">
            <p className="text-xs text-white">
              Exported {status.manifest.length} file{status.manifest.length === 1 ? "" : "s"} to {destinationPath}.
            </p>
            <p className="text-xs text-cool-grey">
              A manifest (spyglass_export_manifest.csv / .json) was written alongside the copied media.
            </p>
            {status.manifest.some((m) => !m.checksum_verified) && (
              <p className="text-xs text-red-400">
                Warning: one or more files failed checksum verification -- see the manifest for details.
              </p>
            )}
            {xmlExportedTo ? (
              <p className="text-xs text-white">Wrote copied-files sequence to {xmlExportedTo}.</p>
            ) : (
              <button
                type="button"
                disabled={xmlExporting}
                onClick={() => void exportCopiedXml()}
                className="rounded border border-border-subtle px-3 py-1 text-xs hover:border-athletic-blue-light disabled:opacity-50"
              >
                {xmlExporting ? "Exporting..." : "Also Export Premiere XML (Copied Files)"}
              </button>
            )}
            <div className="flex justify-end pt-2">
              <button type="button" onClick={onClose} className="rounded bg-athletic-blue px-3 py-1 text-xs font-medium hover:bg-athletic-blue-light">
                Done
              </button>
            </div>
          </div>
        )}

        {step === "error" && (
          <div className="space-y-4">
            <p className="text-xs text-red-400">{errorMessage ?? "The export failed."}</p>
            <div className="flex justify-end gap-2 pt-2">
              <button type="button" onClick={onClose} className="rounded border border-border-subtle px-3 py-1 text-xs">
                Close
              </button>
              <button
                type="button"
                onClick={() => setStep("options")}
                className="rounded bg-athletic-blue px-3 py-1 text-xs font-medium hover:bg-athletic-blue-light"
              >
                Try Again
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
