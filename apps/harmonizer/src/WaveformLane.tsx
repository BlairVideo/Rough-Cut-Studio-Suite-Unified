import { useEffect, useRef, useState } from "react";

export interface LanePoint {
  ref_time: number;
  take_time: number;
  confidence: number | null;
  editable: boolean;
}

interface FlaggedRange {
  start: number;
  end: number;
}

interface WaveformLaneProps {
  label: string;
  peaks: number[];
  duration: number;
  points?: LanePoint[];
  flaggedRanges?: FlaggedRange[];
  onCommit?: (points: LanePoint[]) => void;
}

const VIEW_WIDTH = 1000;
const VIEW_HEIGHT = 80;

function confidenceColor(confidence: number | null): string {
  if (confidence === null) return "#38bdf8"; // neutral blue for boundaries/inserted points
  if (confidence < 0.3) return "#f87171"; // red
  if (confidence < 0.5) return "#fbbf24"; // amber
  return "#4ade80"; // green
}

function waveformPath(peaks: number[]): string {
  if (peaks.length === 0) return "";
  const step = VIEW_WIDTH / (peaks.length - 1 || 1);
  const mid = VIEW_HEIGHT / 2;
  const top = peaks.map((p, i) => `${i === 0 ? "M" : "L"} ${(i * step).toFixed(2)} ${(mid - p * mid).toFixed(2)}`);
  const bottom = peaks
    .map((p, i) => `L ${(i * step).toFixed(2)} ${(mid + p * mid).toFixed(2)}`)
    .reverse();
  return [...top, ...bottom, "Z"].join(" ");
}

export default function WaveformLane({
  label,
  peaks,
  duration,
  points,
  flaggedRanges,
  onCommit,
}: WaveformLaneProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [localPoints, setLocalPoints] = useState<LanePoint[]>(points ?? []);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  // Resync from the parent's authoritative state (e.g. after a backend
  // recompute confirms it) whenever we're not mid-drag ourselves.
  useEffect(() => {
    if (dragIndex === null && points) setLocalPoints(points);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points]);

  const path = waveformPath(peaks);
  const timeToX = (t: number) => (t / duration) * VIEW_WIDTH;
  const xToTime = (x: number) => (x / VIEW_WIDTH) * duration;

  function clientXToViewX(clientX: number): number {
    const rect = svgRef.current!.getBoundingClientRect();
    return ((clientX - rect.left) / rect.width) * VIEW_WIDTH;
  }

  function handlePointerMove(e: React.PointerEvent<SVGSVGElement>) {
    if (dragIndex === null) return;
    const x = clientXToViewX(e.clientX);
    const newTime = Math.max(0, Math.min(duration, xToTime(x)));
    setLocalPoints((prev) => prev.map((p, i) => (i === dragIndex ? { ...p, take_time: newTime } : p)));
  }

  function handlePointerUp() {
    if (dragIndex !== null) {
      setDragIndex(null);
      onCommit?.(localPoints);
    }
  }

  function deleteSelected() {
    if (selected === null) return;
    const updated = localPoints.filter((_, i) => i !== selected);
    setLocalPoints(updated);
    setSelected(null);
    onCommit?.(updated);
  }

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-neutral-400">{label}</span>
        {selected !== null && localPoints[selected]?.editable && (
          <button
            onClick={deleteSelected}
            className="rounded bg-red-900/60 px-2 py-0.5 text-xs text-red-200 hover:bg-red-800"
          >
            Delete anchor
          </button>
        )}
      </div>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
        preserveAspectRatio="none"
        className="h-20 w-full cursor-default rounded bg-neutral-900"
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerLeave={handlePointerUp}
      >
        {flaggedRanges?.map((r, i) => (
          <rect
            key={i}
            x={timeToX(r.start)}
            y={0}
            width={Math.max(0, timeToX(r.end) - timeToX(r.start))}
            height={VIEW_HEIGHT}
            fill="#7f1d1d"
            opacity={0.35}
          />
        ))}
        <path d={path} fill="#38bdf8" opacity={0.6} />
        {localPoints.map((p, i) => {
          const x = timeToX(p.take_time);
          const isSelected = selected === i;
          return (
            <g key={i}>
              <line
                x1={x}
                x2={x}
                y1={0}
                y2={VIEW_HEIGHT}
                stroke={confidenceColor(p.confidence)}
                strokeWidth={isSelected ? 2.5 : 1.5}
              />
              {p.editable && (
                <circle
                  cx={x}
                  cy={VIEW_HEIGHT - 6}
                  r={5}
                  fill={confidenceColor(p.confidence)}
                  stroke={isSelected ? "#ffffff" : "none"}
                  strokeWidth={1.5}
                  className="cursor-ew-resize"
                  onPointerDown={(e) => {
                    e.stopPropagation();
                    setSelected(i);
                    setDragIndex(i);
                  }}
                />
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
