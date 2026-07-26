# tools/

Home for cross-app scripts currently scattered across the source repo.

- `run_tests.sh` — runs every `uv` workspace member's `pytest` suite, each in its
  own subprocess (`uv run --package <name> pytest`), rather than one combined
  `uv run pytest` from the repo root. Needed because several apps
  (`blair-brander`, `broll-analyzer`, `interview-transcriber`) each name their
  real entrypoint `app.py` — collected into one shared Python process, whichever
  app's `app.py` imports first silently wins every other app's `import app` for
  the rest of that session via Python's global `sys.modules` cache. See the
  script's own header comment and the root `CLAUDE.md`'s "Python tests" line.

Still not fully populated — migrated during Phase 1/2 (see `../migration-plan.md`).
Other candidates:

- BRAW proxy build (`Studio Suite/tools/braw/build.sh`) — compiles against the
  proprietary Blackmagic RAW SDK, never vendored into git.
- Packaging scripts (`B-Roll Analyzer/build_app.sh` + `PLATYPUS_PACKAGING.md`,
  `Studio Suite/launch_studio_suite.sh`) — the source repo currently has three
  different, unconsolidated packaging approaches; see migration-plan.md §4.
