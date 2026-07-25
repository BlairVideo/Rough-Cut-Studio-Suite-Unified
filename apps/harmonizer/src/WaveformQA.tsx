import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import WaveformLane, { type LanePoint } from "./WaveformLane";
import type { Segment, SyncReport } from "./types";

interface WaveformQAProps {
  report: SyncReport;
  reportPath: string;
  segmentsByTake: Record<string, Segment[]>;
  onSegmentsChange: (take: string, segments: Segment[]) => void;
}

function pointsForTake(take: string, segments: Segment[], report: SyncReport): LanePoint[] {
  if (segments.length === 0) return [];
  const confidenceByRefTime = new Map(report.anchors.map((a) => [a.ref_time, a.confidence[take] ?? null]));
  const raw = [
    { ref_time: segments[0].ref_start, take_time: segments[0].take_start },
    ...segments.map((s) => ({ ref_time: s.ref_end, take_time: s.take_end })),
  ];
  return raw.map((p, i) => ({
    ref_time: p.ref_time,
    take_time: p.take_time,
    confidence: confidenceByRefTime.get(p.ref_time) ?? null,
    editable: i !== 0 && i !== raw.length - 1,
  }));
}

export default function WaveformQA({ report, reportPath, segmentsByTake, onSegmentsChange }: WaveformQAProps) {
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  async function commit(take: string, points: LanePoint[]) {
    setPending(take);
    setError(null);
    try {
      const segments = await invoke<Segment[]>("run_recompute_segments", {
        reportPath,
        take,
        points: points.map((p) => ({ ref_time: p.ref_time, take_time: p.take_time })),
      });
      onSegmentsChange(take, segments);
    } catch (e) {
      setError(String(e));
    } finally {
      setPending(null);
    }
  }

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-medium">Waveform QA</h2>
        <p className="text-xs text-neutral-500">
          Drag an anchor to nudge it, click then "Delete anchor" to merge across it. Red/amber lines are
          low-confidence matches worth checking.
        </p>
      </div>

      <WaveformLane label="Reference" peaks={report.waveforms.reference} duration={report.ref_duration} />

      {report.takes.map((take) => {
        const segments = segmentsByTake[take] ?? [];
        return (
          <div key={take} className="relative">
            <WaveformLane
              label={take}
              peaks={report.waveforms[take] ?? []}
              duration={report.take_durations[take]}
              points={pointsForTake(take, segments, report)}
              flaggedRanges={segments
                .filter((s) => s.flagged)
                .map((s) => ({ start: s.take_start, end: s.take_end }))}
              onCommit={(points) => commit(take, points)}
            />
            {pending === take && (
              <span className="absolute right-2 top-0 text-xs text-sky-400">Recomputing…</span>
            )}
          </div>
        );
      })}

      {error && (
        <pre className="whitespace-pre-wrap rounded-md border border-red-900 bg-red-950/40 p-3 text-xs text-red-300">
          {error}
        </pre>
      )}
    </section>
  );
}
