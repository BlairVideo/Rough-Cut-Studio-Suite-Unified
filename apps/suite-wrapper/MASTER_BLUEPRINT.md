# Master Blueprint — Rough Cut Studio Suite

Audit and refactoring guide for the security / performance / architecture pass.
Grounded in a read-only code audit (three parallel agents, July 2026). Every
finding cites `file:line` against the primary copy, originally at
`/Applications/ Claude Apps/ Rough Cut Studio Suite/` and moved 2026-07-20 to
`/Users/cj/Developer/Blair/Rough Cut Studio Suite/`. A second byte-identical
copy exists at `…/Rough Cut Studio Suite — All-In-One/` — see A-0 below.

Legend: **[C]** critical · **[H]** high · **[M]** medium · **[L]** low ·
**[✓]** already done well (don't regress it).

---

## 0. Executive summary

The suite is a pywebview desktop shell (`SuiteApi extends` Rough Cut Studio's
`Api`) that wraps five standalone apps through three isolation mechanisms:
in-process subclassing (RCS), an in-process pure-Pillow bridge (Blair Brander),
and JSON-over-stdout subprocess workers in each sibling's own venv (Transcriber,
B-Roll, A-Sync). The architecture is sound and the highest-risk surfaces — the
preview HTTP server, the JS↔Python bridge, subprocess argv, XML export, LLM
prompt-injection — are **already handled correctly**. The work ahead is
consolidation and hardening, not a rewrite.

Three items dominate the execution phase:

1. **[H] Secrets at rest** — a live Gemini key is in plaintext `.env`; migrate
   it to Keychain like the other two secrets already are. (SEC-1)
2. **[H] CLIP energy scoring** — runs one frame at a time, on CPU, with N model
   copies; this is the single biggest perf win. (PERF-1)
3. **[H] Two hand-synced full copies of the tree** — the largest
   maintainability liability; every edit must be double-applied by hand. (A-0)

---

## 1. High-Level Architectural Analysis

### 1.1 Integration mechanisms (the three ways the shell reaches a sibling)

`backend/paths.py:15-40` is the single source of truth for sibling dirs and venv
interpreters, all derived from `__file__` (never hardcoded — the parent folders
contain leading spaces). **[✓] Keep this. Never hardcode a sibling path.**

| Sibling | Mechanism | Entry point | Isolation |
|---|---|---|---|
| Rough Cut Studio | Subclass + frontend reuse (in-process) | `SuiteApi(Api)` `suite_api.py:141`; page recomposed at launch `main.py:112-149` | None — shares the suite process & GIL |
| Blair Brander | In-process bridge (pure Pillow, Tkinter-free modules only) | `backend/brander_bridge.py:43-48` | None — runs in suite venv |
| Local Interview Transcriber | Subprocess worker, JSON-line stdout | `backend/workers/transcribe_worker.py`, `IVT_PYTHON` | Own venv/process |
| B-Roll Analyzer | Subprocess worker (+ nested `ProcessPoolExecutor`) | `backend/workers/broll_worker.py`, `BROLL_PYTHON` | Own venv/process |
| A-Sync | Subprocess worker + in-process XML builder | `backend/workers/sync_worker.py`, `ASYNC_PYTHON`; `backend/sync_xml.py` | Own venv/process |

The worker protocol: params as a single JSON string in `argv[1]`; stdout is one
JSON object per line (`{"type":"progress|result|error",...}`) — `jobs.py:289-379`.
**[✓]** Streaming is incremental with a separate stderr-drain thread.

### 1.2 Data flow & the implicit shared-file contracts

RCS ingests **transcripts, not media** — all media handoff funnels through
generated WebVTT files whose `NOTE Source video:`/`NOTE Source audio:` headers
auto-link media. `source_id` is always the VTT filename stem. Durable truth
lives in sidecar files next to the media; in-memory state (`JobManager`
singleton, RCS `Api` instance state) is rebuilt empty each launch.

The real inter-app API is a set of **shared JSON schemas** — currently enforced
only by comments. Treat these as contracts under test:

| File | Owner / shared with | Notes |
|---|---|---|
| `.ivt-cache.json` | shared w/ standalone Transcriber | Existence alone marks a video "transcribed" for the standalone. Now also carries suite-only keys (`audio_source`, `sync_offset_seconds`, `sync_tracks`, `sync_method`, `sync_updated_at`) — relies on the standalone loader ignoring unknown keys. |
| `.sync-offsets.json` | suite-owned | Folded into `.ivt-cache.json` and deleted once both synced+transcribed (v9). |
| `.broll_analyzer_cache.json` | shared w/ B-Roll standalone | Per-frame samples + fingerprint. |
| `favorites.json`, `custom_logos.json` | suite-owned | — |
| Handoff VTTs | suite → RCS | `NOTE Source video/audio:` are the linking contract. |

### 1.3 Concurrency model

- One process-wide `JobManager` singleton (`jobs.py:422-429`), all state under a
  single `RLock`, frontend gets snapshots not live refs. **[✓]**
- Per-kind throttle `_kind_limits = {"transcribe": 1}` (`jobs.py:95`) — one
  transcription at a time due to Metal/MPS contention; excess FIFO-queues.
  Runtime-adjustable via `transcriber_set_parallel`. **[✓]**
- Heavy CPU/GPU work runs in **separate venvs/processes** → true OS parallelism
  outside the suite GIL. **[✓]**
- **[M]** Two paths run synchronous `subprocess.run` on a pywebview worker
  thread: `broll_export_xml` (300s timeout, `suite_api.py:853`) and `sync_probe`
  (60s, `suite_api.py:951`). Fine at desktop scale; convert to jobs if they ever
  block the UI.

### 1.4 Structural risks (target for refactor)

- **A-0 [H] — Two hand-maintained byte-identical copies of the whole tree.**
  ✅ **Done (v10):** `Studio Suite/sync_copies.sh` — one-way, code-files-only
  rsync (never assets/.env/venvs) with a `--check` drift-report mode. Copies
  stay real runnable apps; syncing is one command.
- **A-1 [M] — `suite_api.py` is a ~1,600-line god-object** mixing jobs,
  transcriber, b-roll, favorites, sync, and brander. ✅ **Done (v10):** split into `api_shared.py` + six mixin modules
  (`api_security/transcriber/broll/favorites/sync/brander.py`) composed onto
  `SuiteApi(…, Api)`; suite_api.py is ~200 lines. Method inventory verified
  identical pre/post; selftest green.
- **A-2 [M] — Stringly-typed cross-process contract.** No shared schema between
  `SuiteApi` and workers; drift is caught only at runtime. ✅ **Done (v10):** `backend/workers/worker_protocol.py` — make_*/parse_line
  used by all three workers (build side) and jobs.py (receive side);
  stdlib-only, importable from both the sibling venvs and the suite.
- **A-3 [M] — Duplicated constants hand-synced with sibling source**:
  `WHISPER_MODELS` (`suite_api.py:71`), `default_scene()` (`brander_bridge.py:152`),
  `LOGO_PLACEMENTS` (`brander_bridge.py:56`). Worker `--selfcheck`s assert the
  sibling attributes *exist* but not that they *match*. ✅ **Done (v10):** transcribe_worker --selfcheck dumps the sibling's values
  and `main.py --selftest` asserts equality; Brander's default_scene/
  LOGO_PLACEMENTS are ast-compared against the sibling source in the same
  selftest.
- **A-4 [M] — Silent-fallback masking.** `save_xml` wraps the whole audio splice
  in `try/except` and silently falls back to stock RCS export on any failure
  (`suite_api.py:235-237`) — a splice bug would silently drop synced audio.
  ✅ **Done (v10):** still falls back to stock export, but appends a `warnings`
  entry and pushes a deferred setStatus(...,'error') via evaluate_js.
- **A-5 [L] — Brittle frontend coupling.** `main.py` regex-scrapes RCS's
  `index.html`; `suite.js` reaches into RCS `app.js` internals and `index.html`
  structure by name (the 13-`<col>` colgroup, `appendCutRow`, `state.editSegments`,
  …). Any RCS refactor silently breaks the overlay. ✅ **Done (v10):** `assertRcsHooks()` at boot inventories all 8 functions,
  state.editSegments, 4 DOM anchors, and the colgroup — console.warn per
  missing hook, never throws. Verified zero warnings against current RCS.

---

## 2. Performance & Optimization Checklist

Ordered by impact. Each item is a concrete, testable target.

### PERF-1 [H] — CLIP energy scoring: batch, use MPS, stop N× model copies
`B-Roll Analyzer/vision_energy.py:120-144` scores a **single** image per call
(batch=1) inside the per-frame decode loop (`analyzer.py:427`); device pick is
`"cuda" else "cpu"` (`vision_energy.py:94`) so on Apple Silicon it **always runs
on CPU**; and each of the 3 pool processes (`broll_worker.py:233`) loads its own
ViT-B-32 with **uncapped torch threads** (cv2 threads are capped, torch is not).
Targets:
- [x] Accumulate sampled frames and run CLIP in batches via the new
      `score_frames_energy` (BATCH_SIZE=32). *Parity vs single-frame: 1.1e-5.*
- [x] Add MPS to the device pick. *Verified live: device == "mps".*
- [x] `torch.set_num_threads(1)` per pool child in broll_worker `_pool_init`
      (guarded import), mirroring the cv2 cap.

### PERF-2 [H] — Blair Brander per-frame renderer: cache constants, dedupe holds
`renderer.py`/`export.py` recompute per-frame what's constant across the clip.
Targets:
- [x] `load_font` lru_cached on `(font_key, style, int(size))`.
- [x] `fit_font_size` delegates to an lru_cached pure helper (fit results
      verified identical).
- [x] Hold frames run-length encoded in export_video — rendered once, bytes
      written N times. *Byte-identical output verified (same decoded SHA);
      ~29% faster on a 2s+1s export.*
- [x] `_vignette_mask` small-grid+bilinear (max delta 2/255; 680→25ms cold),
      `_gradient_layer` per-anti-diagonal bytes (bit-identical; 669→7ms cold).
      Pillow-only — no numpy dependency added.

### PERF-3 [M] — B-roll grid frontend re-render — ❌ FALSE POSITIVE (v10)

**Closed without change:** `renderBrollResults()` has exactly one caller —
new analysis results arriving in `onJobDone`. Selection toggles and
undo/redo already update chips/checkboxes in place and never rebuild the
grid. The audit's premise was wrong. Original finding kept below for the
record.

#### (original finding)
`renderBrollResults` (`suite.js:1203-1255`) rebuilds `grid.innerHTML` from all
clips with inline base64 thumbnails on every re-render. Targets:
- [ ] Diff cards / toggle selection via CSS class in place instead of full
      innerHTML rebuild.
- [ ] Serve thumbnails via the preview server as `<img src=token-url>` rather
      than inlining base64.

### PERF-4 [M] — Cache mtime keying inconsistency
`result_cache.py:84-204` stores float `st_mtime` and compares with `!=`; the
transcriber cache stores `int(st.st_mtime)` (`transcribe_worker.py:127`). Exact
float `!=` on mtime is fragile across filesystems with different sub-second
granularity (folder copy → false cache miss). Target:
- [x] Epsilon compare (1e-6s) on the read side; stored format unchanged
      (shared with older app copies).

### PERF-5 [L] — Redundant probes / scans
- [x] Video timecode read once per batch in sync_worker's detect loop.
- [x] Folder cache parsed once per send via `_broll_clip_duration`'s per-call
      `_memo`.
- [x] rAF-coalesced via `scheduleCutsRowFavoriteMarkers` (trailing-edge safe).

### [✓] Already well-tuned (measure before touching)
Job dispatch/throttle model · streaming worker protocol · Range-based
proxy-free preview server · decode-minimizing `grab()`/`retrieve()` loop
(`analyzer.py:397`) · single-decode-of-reference sync batch · vectorized
waveform peaks + single-polygon canvas redraw (`waveform_view.py`) ·
signature-guarded frontend renderers (`renderDrawer`/`renderTranscribeResults`) ·
debounced/throttled preview scrub with stale-response guards.

---

## 3. Defensive Coding & Security Spec

Threat model: single-user local desktop app processing **sensitive imagery of
people** (interview faces/audio), with opt-in outbound calls (Gemini, one-time
HF/model-weight downloads) and a loopback preview server. This is **not** a
multi-tenant web app, so classic OWASP auth/session items mostly don't apply;
the real surfaces are secrets-at-rest, local IPC, path handling, and what PII
leaves the machine.

### SEC-1 [H] — Migrate the plaintext Gemini key to Keychain
`Rough Cut Studio/.env:1` holds a **live** `GEMINI_API_KEY` in cleartext
(written `api.py:1870`, read `api.py:1858`; file is mode 0600 — good, but Time
Machine / Spotlight-indexed backups / cloud-sync of `/Applications` still see
it). The suite already moved the HF token (`suite_api.py:281`) and Brander's
Gemini key (`suite_api.py:1363`) to Keychain via `keyring` — RCS's key is the
odd one out. Targets:
- [ ] **Rotate the key now** — treat the value currently in the file as
      compromised (it left the machine for this audit). *(USER ACTION — must be
      done in the Google Cloud console; not something the tooling can do.)*
- [x] Migrate RCS's key to the same `keyring`/Keychain store; treat `.env` as
      legacy-read-only. *Done: `SuiteApi.load_saved_api_key`/`save_api_key_to_disk`
      overrides store the key in Keychain (service `RoughCutStudio`), read `.env`
      only as a legacy fallback, and scrub the plaintext copy via
      `_scrub_env_gemini_key` on first read/save. The existing `.env` migrates
      automatically the next time the suite launches (or when a key is next
      saved).*
- [x] Confirm `.env` is in backup-exclusion / `.gitignore`. *N/A — not a git
      repo; the auto-scrub removes the plaintext copy entirely, so nothing to
      exclude. The live `.env` is still present until the suite next runs.*

### SEC-2 [M] — Gate transcript-embedded media paths through the allowlist
`sources.py:126-131` (`_add_transcript`) assigns the `NOTE Source video:` path
from **file content** into `media_paths[source_id]` with only `os.path.exists()`
— no extension check. The project-load path routes the identical field through
`_is_allowed_media_path` (`api.py:167-179`) precisely because an untrusted path
"becomes valid input to ffmpeg … and gets served byte-for-byte over the local
preview HTTP server." A hostile transcript could point the source at any
existing file (e.g. `~/.ssh/id_rsa`); the random token + loopback binding stop
remote exploitation, so this is defense-in-depth, not a live read primitive.
Targets:
- [x] Gate the `detect_linked_media` result through `_is_allowed_media_path`
      before assigning in `_add_transcript`. *Done suite-side without editing
      RCS: `SuiteApi._add_transcript`/`pick_transcript_files` overrides call
      super() then `_suite_prune_disallowed_media`, which re-applies RCS's own
      `_is_allowed_media_path` gate and drops (and `preview_server.forget`s) any
      media link that fails it. Legit video links pass unchanged.*
- [x] Apply the same extension gate inside `sources.py get_preview_url` for
      symmetry. *Covered by the pruning above — a disallowed path never remains
      in `media_paths`, so it never reaches `get_preview_url`. RCS's
      `get_preview_url` itself was left untouched (sibling file).*

### SEC-3 [L] — PII in predictable temp autosave
`api.py:204` writes crash-recovery snapshots to
`$TMPDIR/rough_cut_studio_autosave.json` containing verbatim transcript text
(`source_text`); correctly excludes the API key. Persists after exit. Targets:
- [x] Move into the app support dir with 0600; clear on clean exit. *Done:
      `SuiteApi.__init__` relocates `_autosave_path` to
      `~/Library/Application Support/RoughCutStudioSuite/autosave.json` (dir
      0700), and the `_autosave`/`autosave_working_state` overrides chmod the
      file 0600 after each write. Clearing still uses RCS's existing
      `discard_autosave`.*

### SEC-4 [L] — Coerce numeric attrs in frontend markup
`suite.js:1235-1239` interpolates `s.start`/`s.end` into `data-*` unescaped.
They're floats from the worker (not free-form), so theoretical only — text,
speakers, filenames are all correctly `esc()`d. Target:
- [x] `Number(...)` / `esc(...)` for uniformity. *Done: `renderBrollResults`
      coerces `s.start`/`s.end` via `Number(...) || 0` before they touch markup.*

### Standing rules for the execution phase (enforce in review)
- **[✓] No `shell=True` / `os.system` / `eval` / `pickle` / `yaml.load`** exist
  today — keep it that way. All subprocesses stay list-form argv with
  interpreter/script from `paths.*`.
- **[✓] Preview server** binds `127.0.0.1:0` only, GET+Range, files addressed by
  opaque `uuid4` token (no path in URL → no traversal). Do not add a
  path-in-URL endpoint or bind `0.0.0.0`.
- **[✓] XML export** sets all text via ElementTree `.text=` (auto-escapes) — never
  build XMEML/FCPXML by string concatenation.
- **[✓] LLM prompt-injection is neutralized by server-side re-validation**: RCS
  accepts only real `source_id`+`segment_index`+clamped offsets
  (`api.py:1215-1300`); Brander whitelists/clamps every field
  (`brander_gemini.py:110-209`). Any new LLM-driven action MUST re-validate the
  model's output against a server-side allowlist before it touches state/files.
- **[✓] Secrets in transit**: keys go in the `x-goog-api-key` header only (never
  URL/query), are scrubbed from error text, never logged, endpoints hardcoded
  `https://`, no `verify=False`. Preserve all of this.
- **Path handling**: any new method that takes a path from JS or file content
  must validate it (`_is_allowed_media_path` / `os.path.isfile` + extension)
  before ffmpeg, preview-server registration, read, write, or delete.

### PII / network posture (document for users)
- With the **Gemini** provider, full interview transcript text is POSTed to
  Google under the user's own key over TLS (`gemini_client.py:165`) — the
  **local Ollama** provider keeps everything on-device.
- Transcription/diarization (mlx-whisper/pyannote), B-roll (OpenCV/CLIP), and
  all graphics rendering are fully local; the only other outbound traffic is
  one-time model-weight downloads. Brander's Gemini call sends only title text,
  not interview PII.

---

## 4. Suggested execution order

1. **SEC-1** rotate + migrate the live key (do first; it's exposed now).
2. **A-0** resolve the two-copy problem, or you'll double-apply everything below.
3. **PERF-1** CLIP batching/MPS — biggest measurable win.
4. **SEC-2**, **A-4** (silent splice fallback), **A-2** (shared schema).
5. **PERF-2** Brander renderer caching; **PERF-3** b-roll grid.
6. **A-1** split the `suite_api.py` god-object (mechanical, low-risk, do
   incrementally alongside the above).
7. Lower-priority: PERF-4/5, SEC-3/4, A-3/A-5.

Verify each change with `python3 main.py --selftest` in **both** copies (until
A-0 lands) and drive the affected workspace end-to-end.
