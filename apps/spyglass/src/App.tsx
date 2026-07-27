import { useEffect } from "react";
import { SearchBar } from "./components/SearchBar";
import { ResultsList } from "./components/ResultsList";
import { FacetSidebar } from "./components/FacetSidebar";
import { WatchedRootsPanel } from "./components/WatchedRootsPanel";
import { PoolTray } from "./components/PoolTray";
import { useAppStore } from "./store/useAppStore";

function App() {
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const setSettingsOpen = useAppStore((s) => s.setSettingsOpen);
  const poolOpen = useAppStore((s) => s.poolOpen);
  const setPoolOpen = useAppStore((s) => s.setPoolOpen);
  const poolCount = useAppStore((s) => s.pool.length);
  const statusMessage = useAppStore((s) => s.statusMessage);
  const setStatusMessage = useAppStore((s) => s.setStatusMessage);
  const refreshWatchedRoots = useAppStore((s) => s.refreshWatchedRoots);
  const refreshPool = useAppStore((s) => s.refreshPool);
  const showFavorites = useAppStore((s) => s.showFavorites);

  useEffect(() => {
    void refreshWatchedRoots();
    void refreshPool();
  }, [refreshWatchedRoots, refreshPool]);

  useEffect(() => {
    if (!statusMessage) return;
    const timer = setTimeout(() => setStatusMessage(null), 6000);
    return () => clearTimeout(timer);
  }, [statusMessage, setStatusMessage]);

  return (
    <div className="flex h-screen flex-col bg-surface">
      <div className="flex items-center gap-2 border-b border-border-subtle bg-surface-raised">
        <SearchBar />
        <button
          type="button"
          onClick={() => void showFavorites()}
          className="rounded border border-border-subtle px-3 py-1.5 text-xs text-white hover:border-athletic-blue-light"
        >
          ★ Favorites
        </button>
        <button
          type="button"
          onClick={() => setPoolOpen(!poolOpen)}
          className="rounded border border-border-subtle px-3 py-1.5 text-xs text-white hover:border-athletic-blue-light"
        >
          Pool ({poolCount})
        </button>
        <button
          type="button"
          onClick={() => setSettingsOpen(!settingsOpen)}
          className="mr-6 rounded border border-border-subtle px-3 py-1.5 text-xs text-white hover:border-athletic-blue-light"
        >
          Watched Folders
        </button>
      </div>

      <div className={poolOpen ? "flex flex-1 overflow-hidden pb-40" : "flex flex-1 overflow-hidden"}>
        <FacetSidebar />
        <ResultsList />
      </div>

      {statusMessage && (
        <div className="fixed bottom-4 left-1/2 z-20 -translate-x-1/2 rounded-md border border-border-subtle bg-surface-raised px-4 py-2 text-sm text-white shadow-lg">
          {statusMessage}
        </div>
      )}

      {settingsOpen && <WatchedRootsPanel />}
      <PoolTray />
    </div>
  );
}

export default App;
