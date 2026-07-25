import { open } from "@tauri-apps/plugin-dialog";

interface MediaDropZoneProps {
  active: boolean;
  onPick: (paths: string[]) => void;
}

export default function MediaDropZone({ active, onPick }: MediaDropZoneProps) {
  async function browse() {
    const selected = await open({
      multiple: true,
      filters: [{ name: "Media", extensions: ["wav", "mov", "mp4", "braw", "m4a", "aiff"] }],
    });
    if (Array.isArray(selected)) onPick(selected);
    else if (typeof selected === "string") onPick([selected]);
  }

  return (
    <div
      data-zone-id="media"
      onClick={browse}
      className={`flex flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed p-10 text-center transition-colors cursor-pointer select-none
        ${active ? "border-sky-400 bg-sky-400/10" : "border-neutral-700 bg-neutral-900/40 hover:border-neutral-500"}`}
    >
      <span className="text-sm font-medium text-neutral-300">
        Drop the reference audio and all takes here
      </span>
      <span className="text-xs text-neutral-500">or click to browse (select multiple files)</span>
    </div>
  );
}
