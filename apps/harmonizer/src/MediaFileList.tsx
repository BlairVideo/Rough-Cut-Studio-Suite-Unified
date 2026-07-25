export interface MediaFile {
  path: string;
  role: "reference" | "take";
  noRetime: boolean;
}

interface MediaFileListProps {
  files: MediaFile[];
  onSetRole: (index: number, role: "reference" | "take") => void;
  onToggleNoRetime: (index: number, value: boolean) => void;
  onRemove: (index: number) => void;
}

function basename(path: string): string {
  return path.split("/").pop() ?? path;
}

export default function MediaFileList({ files, onSetRole, onToggleNoRetime, onRemove }: MediaFileListProps) {
  if (files.length === 0) return null;

  return (
    <div className="overflow-hidden rounded-lg border border-neutral-800">
      <table className="w-full text-sm">
        <thead className="bg-neutral-900 text-neutral-400">
          <tr>
            <th className="px-3 py-2 text-left font-medium">File</th>
            <th className="px-3 py-2 text-left font-medium">Role</th>
            <th className="px-3 py-2 text-left font-medium">Skip retiming</th>
            <th className="px-3 py-2" />
          </tr>
        </thead>
        <tbody>
          {files.map((f, i) => (
            <tr key={f.path} className="border-t border-neutral-800">
              <td className="max-w-xs truncate px-3 py-2 text-neutral-100" title={f.path}>
                {basename(f.path)}
              </td>
              <td className="px-3 py-2">
                <div className="flex gap-3 text-xs text-neutral-300">
                  <label className="flex items-center gap-1.5">
                    <input
                      type="radio"
                      name={`role-${i}`}
                      checked={f.role === "reference"}
                      onChange={() => onSetRole(i, "reference")}
                      className="h-3.5 w-3.5 accent-sky-500"
                    />
                    Reference
                  </label>
                  <label className="flex items-center gap-1.5">
                    <input
                      type="radio"
                      name={`role-${i}`}
                      checked={f.role === "take"}
                      onChange={() => onSetRole(i, "take")}
                      className="h-3.5 w-3.5 accent-sky-500"
                    />
                    Take
                  </label>
                </div>
              </td>
              <td className="px-3 py-2">
                {f.role === "take" && (
                  <input
                    type="checkbox"
                    checked={f.noRetime}
                    onChange={(e) => onToggleNoRetime(i, e.target.checked)}
                    className="h-4 w-4 rounded border-neutral-600 bg-neutral-900 accent-sky-500"
                  />
                )}
              </td>
              <td className="px-3 py-2 text-right">
                <button onClick={() => onRemove(i)} className="text-xs text-neutral-500 hover:text-red-400">
                  Remove
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
