# apps/

Empty until later migration phases populate it (see `../migration-plan.md`):

- **Phase 1** (Python/pywebview apps, copied from the source monolith repo):
  `suite-wrapper/`, `rough-cut-studio/`, `a-sync/`, `broll-analyzer/`,
  `blair-brander/`, `interview-transcriber/`
- **Phase 2** (Tauri/React apps):
  `harmonizer/` (with `harmonizer/backend/` — the real alignment/FCPXML/Resolve
  engine, not a placeholder), `card-eater/`
