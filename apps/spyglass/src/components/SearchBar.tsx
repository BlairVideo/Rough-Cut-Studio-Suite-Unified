import { useAppStore } from "../store/useAppStore";

export function SearchBar() {
  const searchQuery = useAppStore((s) => s.searchQuery);
  const setSearchQuery = useAppStore((s) => s.setSearchQuery);
  const runSearch = useAppStore((s) => s.runSearch);
  const searching = useAppStore((s) => s.searching);

  return (
    <form
      className="flex flex-1 items-center gap-3 px-6 py-4"
      onSubmit={(e) => {
        e.preventDefault();
        void runSearch();
      }}
    >
      <span className="text-lg font-semibold tracking-tight text-white">Spyglass</span>
      <input
        type="text"
        value={searchQuery}
        onChange={(e) => setSearchQuery(e.target.value)}
        placeholder='Search shots -- e.g. "mascot cheering" or "students studying"'
        className="flex-1 rounded-md border border-border-subtle bg-surface-inset px-3 py-2 text-sm text-white placeholder-cool-grey outline-none focus:border-athletic-blue-light"
      />
      <button
        type="submit"
        disabled={searching}
        className="rounded-md bg-athletic-blue px-4 py-2 text-sm font-medium text-white transition hover:bg-athletic-blue-light disabled:opacity-50"
      >
        {searching ? "Searching..." : "Search"}
      </button>
    </form>
  );
}
