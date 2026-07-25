import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import MediaDropZone from "./MediaDropZone";
import MediaFileList, { type MediaFile } from "./MediaFileList";
import WaveformQA from "./WaveformQA";
import type { AlignResult, ImportResult, Segment, SyncReport } from "./types";

type Status = "idle" | "running" | "done" | "error";

function speedStats(segments: Segment[]) {
  if (segments.length === 0) return null;
  const speeds = segments.map((s) => s.speed_factor).sort((a, b) => a - b);
  const flagged = segments.filter((s) => s.flagged).length;
  return {
    count: segments.length,
    flagged,
    min: speeds[0],
    median: speeds[Math.floor(speeds.length / 2)],
    max: speeds[speeds.length - 1],
  };
}

export default function App() {
  const [files, setFiles] = useState<MediaFile[]>([]);
  const [dropActive, setDropActive] = useState(false);

  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [align, setAlign] = useState<AlignResult | null>(null);
  const [segmentsByTake, setSegmentsByTake] = useState<Record<string, Segment[]>>({});
  const [useCurrentProject, setUseCurrentProject] = useState(true);
  const [projectName, setProjectName] = useState("Harmonizer Sync");
  const [timelineName, setTimelineName] = useState("");
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);

  const refPath = files.find((f) => f.role === "reference")?.path ?? null;
  const takeFiles = files.filter((f) => f.role === "take");
  const takePaths = takeFiles.map((f) => f.path);
  const allFilesSet = refPath !== null && takePaths.length > 0;

  useEffect(() => {
    // Only present inside the real Tauri shell -- absent when previewing the
    // raw Vite page in an ordinary browser tab during development.
    if (!("__TAURI_INTERNALS__" in window)) return;

    let unlisten: (() => void) | undefined;
    getCurrentWindow()
      .onDragDropEvent((event) => {
        if (event.payload.type === "over") {
          const [x, y] = [event.payload.position.x, event.payload.position.y];
          const el = document.elementFromPoint(x, y)?.closest("[data-zone-id='media']");
          setDropActive(Boolean(el));
        } else if (event.payload.type === "drop") {
          const [x, y] = [event.payload.position.x, event.payload.position.y];
          const el = document.elementFromPoint(x, y)?.closest("[data-zone-id='media']");
          if (el) addFiles(event.payload.paths);
          setDropActive(false);
        } else {
          setDropActive(false);
        }
      })
      .then((fn) => (unlisten = fn));
    return () => unlisten?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function addFiles(paths: string[]) {
    setFiles((prev) => {
      const existing = new Set(prev.map((f) => f.path));
      let hasReference = prev.some((f) => f.role === "reference");
      const additions: MediaFile[] = paths
        .filter((p) => !existing.has(p))
        .map((path) => {
          const role: MediaFile["role"] = hasReference ? "take" : "reference";
          hasReference = true;
          return { path, role, noRetime: false };
        });
      return [...prev, ...additions];
    });
  }

  function setRole(index: number, role: MediaFile["role"]) {
    setFiles((prev) =>
      prev.map((f, i) => {
        if (i === index) return { ...f, role };
        if (role === "reference" && f.role === "reference") return { ...f, role: "take" };
        return f;
      })
    );
  }

  async function runAnalysis() {
    if (!allFilesSet) return;
    setStatus("running");
    setError(null);
    setAlign(null);
    setImportResult(null);
    try {
      const noRetimeTakes = takeFiles
        .filter((f) => f.noRetime)
        .map((f) => f.path.split("/").pop() ?? f.path);
      const result = await invoke<AlignResult>("run_align", {
        refPath,
        takePaths,
        noRetimeTakes,
      });
      setAlign(result);
      setSegmentsByTake(result.report.segments);
      setStatus("done");
    } catch (e) {
      setError(String(e));
      setStatus("error");
    }
  }

  function clearAll() {
    setFiles([]);
    setStatus("idle");
    setError(null);
    setAlign(null);
    setSegmentsByTake({});
    setUseCurrentProject(true);
    setProjectName("Harmonizer Sync");
    setTimelineName("");
    setImportResult(null);
  }

  async function importToResolve() {
    if (!align || !refPath) return;
    setImporting(true);
    setError(null);
    setImportResult(null);
    try {
      const result = await invoke<ImportResult>("run_import_to_resolve", {
        reportPath: align.report_path,
        refMedia: refPath,
        takeMedia: takePaths,
        projectName: useCurrentProject ? null : projectName,
        timelineName: timelineName.trim() || null,
      });
      setImportResult(result);
    } catch (e) {
      setError(String(e));
    } finally {
      setImporting(false);
    }
  }

  const report: SyncReport | null = align?.report ?? null;

  return (
    <main className="min-h-screen bg-neutral-950 p-8 text-neutral-100">
      <div className="mx-auto max-w-5xl space-y-8">
        <header>
          <h1 className="text-2xl font-semibold">Harmonizer</h1>
          <p className="text-sm text-neutral-400">
            Sync a clean reference recording against your piano takes, then import the synced timeline straight into DaVinci Resolve.
          </p>
        </header>

        <section className="space-y-3">
          <MediaDropZone active={dropActive} onPick={addFiles} />
          <MediaFileList
            files={files}
            onSetRole={setRole}
            onToggleNoRetime={(index, value) =>
              setFiles((prev) => prev.map((f, i) => (i === index ? { ...f, noRetime: value } : f)))
            }
            onRemove={(index) => setFiles((prev) => prev.filter((_, i) => i !== index))}
          />
        </section>

        <section className="flex items-center gap-3">
          <button
            onClick={runAnalysis}
            disabled={!allFilesSet || status === "running"}
            className="rounded-md bg-sky-500 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-400 disabled:cursor-not-allowed disabled:bg-neutral-700 disabled:text-neutral-400"
          >
            {status === "running" ? "Analyzing…" : "Run Sync Analysis"}
          </button>
          <button
            onClick={clearAll}
            disabled={status === "running" || importing}
            className="rounded-md border border-neutral-700 px-4 py-2 text-sm font-medium text-neutral-300 transition-colors hover:border-neutral-500 hover:text-neutral-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Clear All
          </button>
          {status === "running" && (
            <span className="text-sm text-neutral-400">Extracting audio, matching anchors…</span>
          )}
        </section>

        {error && (
          <pre className="whitespace-pre-wrap rounded-md border border-red-900 bg-red-950/40 p-4 text-xs text-red-300">
            {error}
          </pre>
        )}

        {report && (
          <section className="space-y-3">
            <h2 className="text-lg font-medium">Results</h2>
            <div className="overflow-hidden rounded-lg border border-neutral-800">
              <table className="w-full text-sm">
                <thead className="bg-neutral-900 text-neutral-400">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">Take</th>
                    <th className="px-3 py-2 text-right font-medium">Lead-in (s)</th>
                    <th className="px-3 py-2 text-right font-medium">Segments</th>
                    <th className="px-3 py-2 text-right font-medium">Flagged</th>
                    <th className="px-3 py-2 text-right font-medium">Skipped anchors</th>
                    <th className="px-3 py-2 text-right font-medium">Speed (min/med/max)</th>
                  </tr>
                </thead>
                <tbody>
                  {report.takes.map((name) => {
                    const stats = speedStats(segmentsByTake[name] ?? []);
                    return (
                      <tr key={name} className="border-t border-neutral-800">
                        <td className="px-3 py-2 font-mono text-xs">{name}</td>
                        <td className="px-3 py-2 text-right">
                          {report.excluded_leadin_ref_sec[name]?.toFixed(3)}
                        </td>
                        <td className="px-3 py-2 text-right">{stats?.count ?? 0}</td>
                        <td className={`px-3 py-2 text-right ${stats && stats.flagged > 0 ? "text-amber-400" : ""}`}>
                          {stats?.flagged ?? 0}
                        </td>
                        <td className="px-3 py-2 text-right">{report.skipped_anchors[name]}</td>
                        <td className="px-3 py-2 text-right font-mono text-xs">
                          {stats ? `${stats.min.toFixed(3)} / ${stats.median.toFixed(3)} / ${stats.max.toFixed(3)}` : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <WaveformQA
              report={report}
              reportPath={align!.report_path}
              segmentsByTake={segmentsByTake}
              onSegmentsChange={(take, segments) =>
                setSegmentsByTake((prev) => ({ ...prev, [take]: segments }))
              }
            />

            <div className="space-y-2 pt-2">
              <label className="flex items-center gap-2 text-sm text-neutral-300">
                <input
                  type="checkbox"
                  checked={useCurrentProject}
                  onChange={(e) => setUseCurrentProject(e.target.checked)}
                  className="h-4 w-4 rounded border-neutral-600 bg-neutral-900 accent-sky-500"
                />
                Import into currently open Resolve project
              </label>
              <input
                type="text"
                value={timelineName}
                onChange={(e) => setTimelineName(e.target.value)}
                placeholder="Timeline name (leave blank to auto-generate)"
                className="w-full rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-500 focus:border-sky-400 focus:outline-none"
              />
              <div className="flex items-center gap-3">
                {!useCurrentProject && (
                  <input
                    type="text"
                    value={projectName}
                    onChange={(e) => setProjectName(e.target.value)}
                    placeholder="Resolve project name"
                    className="rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-500 focus:border-sky-400 focus:outline-none"
                  />
                )}
                <button
                  onClick={importToResolve}
                  disabled={importing || (!useCurrentProject && !projectName.trim())}
                  className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-emerald-500 disabled:cursor-not-allowed disabled:bg-neutral-700 disabled:text-neutral-400"
                >
                  {importing ? "Importing…" : "Import to Resolve"}
                </button>
              </div>
            </div>
            {importing && (
              <p className="text-sm text-neutral-400">
                Generating FCPXML, importing into Resolve, adding reference audio…
              </p>
            )}
            {importResult && (
              <p className="text-sm text-emerald-400">
                Imported into project "{importResult.project}", timeline "{importResult.timeline}".
              </p>
            )}
          </section>
        )}
      </div>
    </main>
  );
}
