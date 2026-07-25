# tools/

Home for cross-app scripts currently scattered across the source repo. Not yet
populated — migrated during Phase 1/2 (see `../migration-plan.md`). Candidates:

- BRAW proxy build (`Studio Suite/tools/braw/build.sh`) — compiles against the
  proprietary Blackmagic RAW SDK, never vendored into git.
- Packaging scripts (`B-Roll Analyzer/build_app.sh` + `PLATYPUS_PACKAGING.md`,
  `Studio Suite/launch_studio_suite.sh`) — the source repo currently has three
  different, unconsolidated packaging approaches; see migration-plan.md §4.
