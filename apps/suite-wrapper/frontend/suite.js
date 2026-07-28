// suite.js — Studio Suite chrome: workspace switching, background jobs,
// Transcribe / B-Roll / Graphics workspaces, and the glue that feeds results
// into the untouched Rough Cut Studio frontend (app.js, loaded before this
// file as a sibling classic script — its top-level state/functions are
// visible here; every use is typeof-guarded anyway).
(function () {
  "use strict";

  // ---------------- tiny utils ----------------

  const $ = (id) => document.getElementById(id);

  function esc(str) {
    return String(str == null ? "" : str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function basename(p) {
    return String(p || "").split(/[\\/]/).pop();
  }

  function dirname(p) {
    const s = String(p || "");
    const i = s.lastIndexOf("/");
    return i >= 0 ? s.slice(0, i) : "";
  }

  function mmss(sec) {
    const s = Math.max(0, Math.floor(sec || 0));
    return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }

  function suiteApi() {
    return (window.pywebview && window.pywebview.api) || null;
  }

  // Uniform call wrapper: never throws, always resolves to a {ok, ...} dict.
  async function call(method, ...args) {
    const a = suiteApi();
    if (!a || typeof a[method] !== "function") {
      return { ok: false, error: `Backend method "${method}" is unavailable.` };
    }
    try {
      const res = await a[method](...args);
      return res == null ? { ok: false, error: "Empty response from backend." } : res;
    } catch (err) {
      return { ok: false, error: String(err) };
    }
  }

  // A dialog the user cancelled shouldn't produce an error toast.
  function toastIfError(res, fallback) {
    if (res.ok || res.cancelled) return;
    toast(res.error || fallback || "Something went wrong.", "error");
  }

  function whenSuiteApiReady() {
    return new Promise((resolve) => {
      if (window.pywebview && window.pywebview.api) return resolve();
      window.addEventListener("pywebviewready", () => resolve());
    });
  }

  // ---------------- suite state ----------------

  const S = {
    ws: "transcribe",
    jobs: [],
    prevStatus: {},      // job id -> last seen status, for transition detection
    pollTimer: null,
    drawerOpen: false,
    drawerSig: "",
    tQueue: [],          // video paths queued for transcription
    tExpanded: new Set(),// transcribe job ids with the segment preview open
    tSent: new Set(),    // transcribe job ids already sent to Edit
    tResultsSig: "",
    tParallel: 1,
    hfPresent: false,
    gxGeminiKeyPresent: false,
    tLoaded: new Map(),  // video_path -> {name, segmentCount, speakerCount} cache-loaded (no job) transcriptions
    tEd: null,           // open transcript editor: {videoPath, name, jobId|null, segments, speakers, speaker_labels, excluded_speakers, dirty, committed}
    broll: null,         // { jobId, folder, clips } from the last finished analysis
    brollSel: new Map(), // "path::start::end" -> {path, start, end}
    brollSelCommitted: "[]", // JSON of the last committed selection, for the b-roll undo domain
    spyglass: {          // Search workspace (Spyglass integration)
      query: "",
      results: [],
      activeTags: new Set(),   // tag facet filter, OR'd together (matches FacetFilters.tags)
      allTags: [],             // [{label, shot_count}] from spyglass_list_facets
      roots: [],               // [{...WatchedRootStatus}] from spyglass_list_watched_roots
      queueStatus: null,        // {manually_paused, idle_seconds, min_idle_seconds, force_active} from spyglass_background_work_status
      pool: [],                // [{...ShotSearchResult}] from spyglass_pool_get, in pool order
      consolidateDest: "",
      consolidateEstimate: null,
      consolidatePolling: null, // interval id while an export is in flight
      rootsPolling: null,       // interval id while the watched-roots panel is visible (see updateSpyglassRootsPolling)
      dateFrom: "",            // FacetFilters.date_from, "" means unset
      dateTo: "",               // FacetFilters.date_to, "" means unset
      favoritesOnly: false,     // FacetFilters.favorites_only
      folderPath: null,         // FacetFilters.folder_path -- selected folder-tree node, or null for "All folders"
      folderNodes: new Map(),   // parent path (null = top level) -> [{...FolderNode}] from spyglass_list_folder_children, lazily fetched per expand
      folderExpanded: new Set(), // paths currently expanded in the folder tree
      resultLimit: 60,          // current page size passed as `limit`; grows via "View more results"
      hasMore: false,           // true when the last response came back exactly at resultLimit (more may exist)
      sortBy: "relevance",      // FacetFilters.sort_by
      tagFilterOpen: true,      // "Filter by tag" section collapse state
    },
    gx: { scene: null, options: null, aiMode: "local" },
    tlCollapsed: false,  // #suiteTimeline collapsed state
    favorites: [],       // favorited transcript lines (addendum v6), backend is the source of truth
    sync: {              // Sync workspace (addendum v3)
      video: null,       // {path, probe, probed} -- the ACTIVE project (addendum v21)
      audios: [],        // [{path, probe, probed}]
      tracks: null,      // detect result / restored sidecar: [{path, filename, offset_seconds, probe, error}]
      method: "waveform",
      sidecar: null,     // sync_load_offsets result (found:true) for the picked video
      confirmIdx: null,  // track index with the transcribe confirm strip open
      zoom: 1,           // waveform zoom factor, 1 = fit to width (addendum v21)
      projects: new Map(), // video path -> snapshot of the fields above, for every OTHER loaded video
      order: [],           // video paths in load order, for the project-tab strip
    },
    harmonize: {          // Harmonize workspace (Harmonizer integration)
      ref: null,          // {path}
      takes: [],          // [{path}]
      report: null,       // last align result (harmonize_start job result, or a restored sidecar)
      jobId: null,
      zoom: 1,            // waveform zoom factor, 1 = fit to width
      exportNote: null,   // reference_note from the last successful harmonize_export_xml
    },
    cardEater: {
      card: null,             // active CardInfo or null
      cardMountedAt: null,    // client-stamped ISO timestamp, the {YYYYMMDD} card_insert source
      prevCardId: undefined,  // undefined = never polled yet; used to detect mount/unmount transitions
      files: [],
      selectedPaths: new Set(),    // checked for the copy job
      highlightedPaths: new Set(), // Finder-style click/shift-click row highlight -- separate from selectedPaths; a checkbox click acts on this set when it has more than one member
      selectAnchorPath: null,      // anchor for shift-click range highlighting
      filters: { extensions: new Set(), dateFrom: null, dateTo: null },
      collapsedGroups: new Set(),

      templates: [],
      selectedTemplateId: null,
      draft: null,            // set to ceBlankTemplateDraft() at init
      eventName: "",
      manualDate: null,
      folderMode: "date_name",
      prevFileTemplate: "",
      preview: null,
      previewError: null,

      favorites: [],
      destinations: [],
      collisions: {},

      // Copy-job progress/pause/resume/cancel lives in the suite-wide Jobs
      // drawer (S.jobs, kind "cardeater_copy") — no separate job state here.
      safeToRemove: false,

      historyOpen: false,
      historyJobs: [],
      summary: null,

      focusedPath: null,     // path of the file currently shown in the viewer panel, or null
      focusedMeta: null,     // cached suite_cardeater_file_metadata() result for focusedPath
      viewerUrl: null,       // {path, markup} cached suite_cardeater_preview_url() render for focusedPath
    },
    settings: {              // suite-wide preferences (persisted, SUITE_SETTINGS_STORAGE_KEY)
      notifyOnJobDone: true,  // native OS notification when a job finishes and the window isn't focused
    },
    pipeline: {               // "Run Pipeline" modal (Sync/Transcribe/B-Roll queued together for one folder)
      folder: null,
      videos: [],             // last suite_pipeline_list_videos() result for `folder`
      stages: { sync: false, transcribe: true, broll: true, edit: false }, // persisted defaults, PIPELINE_SETTINGS_STORAGE_KEY
      syncAudios: [],         // shared audio pool, tried against every discovered video when the sync stage runs
      syncMethod: "waveform",
      // Runtime-only (not persisted): job ids this run queued with the Edit
      // stage checked, so onJobDone can auto-forward them once they finish —
      // a transcribe job per queued video, at most one broll job (broll_start
      // analyzes the whole folder in one job).
      autoEditTranscribeJobIds: new Set(),
      autoEditBrollJobId: null,
    },
  };

  const JOB_ICONS = { transcribe: "◉", broll: "▦", brander_video: "▶", brander_send: "✦", sync: "∿", cardeater_copy: "⧉", braw_proxy: "◈", harmonize: "♪" };
  const JOB_KIND_LABELS = { transcribe: "Transcribe", broll: "B-Roll", brander_video: "Video Export", brander_send: "Send to Edit", sync: "Sync Detect", cardeater_copy: "Copy", braw_proxy: "BRAW Proxy", harmonize: "Harmonize" };

  // ---------------- toasts ----------------

  function toast(msg, kind = "info", ms = 4200) {
    const host = $("suiteToasts");
    if (!host) return;
    const t = document.createElement("div");
    t.className = `suite-toast suite-toast--${kind}`;
    t.textContent = msg;
    host.appendChild(t);
    setTimeout(() => {
      t.classList.add("is-leaving");
      setTimeout(() => t.remove(), 350);
    }, ms);
    while (host.children.length > 5) host.firstChild.remove();
  }

  // ---------------- suite-wide settings (persisted) ----------------

  const SUITE_SETTINGS_STORAGE_KEY = "suiteSettings.v1";

  function saveSuiteSettings() {
    try {
      localStorage.setItem(SUITE_SETTINGS_STORAGE_KEY, JSON.stringify(S.settings));
    } catch (e) {
      // localStorage disabled/full — setting just won't persist this run.
    }
  }

  function restoreSuiteSettings() {
    let saved;
    try {
      saved = JSON.parse(localStorage.getItem(SUITE_SETTINGS_STORAGE_KEY) || "null");
    } catch (e) {
      return;
    }
    if (!saved || typeof saved !== "object") return;
    if (typeof saved.notifyOnJobDone === "boolean") S.settings.notifyOnJobDone = saved.notifyOnJobDone;
  }

  // Native (Notification Center) alert for a job event, IN ADDITION to the
  // in-app toast already shown alongside every call site below — never a
  // replacement for it, so the app looks and sounds exactly as it always
  // has when the window has focus. Only fires when the user has actually
  // tabbed away (document.hidden), which is the one case a toast can't
  // reach them; best-effort throughout (notify.py never raises either).
  function maybeNativeNotify(title, body) {
    if (!S.settings.notifyOnJobDone || !document.hidden) return;
    call("suite_native_notify", title, body);
  }

  // ---------------- SuiteUndo — bounded per-domain undo/redo stacks ----------------
  //
  // Domains (addendum E): "graphics" (Brander scene), "transcribe::<video>"
  // (one per open transcript), "broll-sel" (segment selection toggles).
  // Snapshots are JSON strings. Callers keep a "last committed" snapshot and
  // push it right before adopting a new committed state, so the stack always
  // holds pre-change states in chronological order. The Edit workspace is
  // NOT a domain here — RCS's own undo stack owns it (the suite timeline
  // routes drags through pushUndoSnapshot()).

  const SuiteUndo = {
    MAX: 50,
    stacks: new Map(), // domain -> {undo: [str], redo: [str]}
    _get(domain) {
      let s = this.stacks.get(domain);
      if (!s) { s = { undo: [], redo: [] }; this.stacks.set(domain, s); }
      return s;
    },
    push(domain, snapshot) {
      const s = this._get(domain);
      s.undo.push(snapshot);
      if (s.undo.length > this.MAX) s.undo.shift();
      s.redo.length = 0; // a new change starts a new branch
    },
    undo(domain, current) {
      const s = this._get(domain);
      if (s.undo.length === 0) return null;
      s.redo.push(current);
      if (s.redo.length > this.MAX) s.redo.shift();
      return s.undo.pop();
    },
    redo(domain, current) {
      const s = this._get(domain);
      if (s.redo.length === 0) return null;
      s.undo.push(current);
      if (s.undo.length > this.MAX) s.undo.shift();
      return s.redo.pop();
    },
    canUndo(domain) { return this._get(domain).undo.length > 0; },
    canRedo(domain) { return this._get(domain).redo.length > 0; },
    reset(domain) { this.stacks.delete(domain); },
  };

  // cmd/ctrl+Z (+shift = redo) routed to the ACTIVE workspace's domain.
  // Registered in the CAPTURE phase so that when a non-Edit workspace is
  // active we can stopPropagation() before RCS app.js's own document-level
  // (bubble) undo handler sees the event — its guard only checks its Cuts
  // tab, which can be "active" while hidden under another suite workspace.
  // NEVER intercepted when the Edit workspace is active or the event target
  // sits inside #workspace-edit (RCS owns those); text fields keep their
  // native undo (our editor fields commit on change, so an uncommitted edit
  // is exactly what native undo should operate on). Colorize is the same
  // deal as Edit: colorize.js owns a real per-clip undo/redo stack via its
  // own window-level (bubble) keydown listener -- this router must stay out
  // of its way entirely, not just skip a branch for it, since colorize.js
  // isn't one of the S.ws-routed domains below and swallowing the keystroke
  // here (preventDefault + stopPropagation, then nothing) is a straight
  // dead end for a Colorize slider drag with no undo domain to hand it to.
  function wireSuiteUndoKeys() {
    document.addEventListener("keydown", (e) => {
      const cmdOrCtrl = e.metaKey || e.ctrlKey;
      if (!cmdOrCtrl || e.key.toLowerCase() !== "z") return;
      if (S.ws === "edit" || S.ws === "colorize") return; // RCS / colorize.js own undo there
      const t = e.target;
      if (t && t.closest && t.closest("#workspace-edit")) return;
      const tag = t && t.tagName;
      if (tag === "TEXTAREA") return; // native undo
      if (tag === "INPUT" && /^(text|password|search|email|url|number)$/.test(t.type || "text")) return;
      e.preventDefault();
      e.stopPropagation(); // keep RCS's document-level handler out of it
      const redo = e.shiftKey;
      if (S.ws === "graphics") { if (redo) gxRedo(); else gxUndo(); }
      else if (S.ws === "transcribe") { if (S.tEd) { if (redo) tEdRedo(); else tEdUndo(); } }
      else if (S.ws === "broll") { if (redo) brollSelRedo(); else brollSelUndo(); }
      else if (S.ws === "sync") { if (redo) syncRedo(); else syncUndo(); }
    }, true);
  }

  // ---------------- workspace switching ----------------

  function switchWs(name) {
    S.ws = name;
    document.querySelectorAll("#suiteTopbar .suite-ws-tab").forEach((b) => {
      const active = b.dataset.ws === name;
      b.classList.toggle("is-active", active);
      b.setAttribute("aria-selected", active ? "true" : "false");
    });
    ["cardeater", "sync", "transcribe", "broll", "spyglass", "edit", "harmonize", "colorize", "graphics"].forEach((ws) => {
      const c = $("workspace-" + ws);
      if (c) c.hidden = ws !== name; // display toggle only — #workspace-edit is never unmounted
    });
    if (name === "edit") {
      renderSuiteTimeline(); // refresh on every switch to Edit (addendum D)
      // Favorites/B-Roll content can exist with no script generated yet (a
      // starred transcript line, segments sent from B-Roll, or the Run
      // Pipeline "Edit" stage) — make sure those tabs are reachable here too.
      if (S.favorites.length > 0) revealOutputBlockIfHidden();
    }
    // The waveform canvas is sized from its (hidden) host's clientWidth,
    // which is 0 while the workspace itself is hidden — redraw once it
    // becomes visible so it isn't stuck at a stale/zero width.
    if (name === "spyglass") { loadSpyglassFacets(); loadSpyglassRoots(); loadSpyglassQueueStatus(); loadSpyglassPool(); loadSpyglassFolderTree(); }
    updateSpyglassRootsPolling();
    if (name === "sync") scheduleSyncWaveformRedraw();
    if (name === "harmonize") scheduleHarmonizeWaveformRedraw();
    if (name !== "broll") stopBrollPreview();
    if (name !== "spyglass" && !$("sgPreviewModal").hidden) closeSpyglassPreview();
    if (name !== "graphics") gxStopPlayback();
    if (name !== "sync") teardownSyncPlayer();
    if (name !== "transcribe") closeTedPlayer();
    // colorize.js is a separate top-level script (its WebGL shader
    // pipeline/curve editor/scopes warranted their own file rather than
    // growing this one further — see CONTRACT.md's Colorize section), so
    // it can't reach this closure's internals. A DOM event is the loosest
    // coupling that still lets it start/stop its render loop and pause
    // video playback exactly when every other workspace pauses its own.
    document.dispatchEvent(new CustomEvent("suite:workspace-changed", { detail: { ws: name } }));
  }

  // ---------------- jobs: polling, badge, drawer ----------------

  function ensurePolling() {
    if (S.pollTimer) return;
    S.pollTimer = setInterval(pollJobs, 1000);
    pollJobs();
  }

  function maybeStopPolling() {
    const active = S.jobs.some((j) => j.status === "queued" || j.status === "running");
    if (!active && !S.drawerOpen && S.pollTimer) {
      clearInterval(S.pollTimer);
      S.pollTimer = null;
    }
  }

  async function pollJobs() {
    const res = await call("suite_list_jobs");
    if (!res.ok) return;
    const jobs = res.jobs || [];
    jobs.forEach((j) => {
      const prev = S.prevStatus[j.id];
      if (prev !== j.status) {
        const isTransition = prev !== undefined; // undefined = first sighting (e.g. already done at boot)
        if (j.status === "done") onJobDone(j, isTransition);
        else if (j.status === "error" && isTransition) {
          toast(`${j.label}: ${j.error || "failed"}`, "error");
          maybeNativeNotify(j.label, j.error || "Failed");
        } else if (j.status === "cancelled" && isTransition) {
          toast(`Cancelled — ${j.label}`, "info");
          maybeNativeNotify(j.label, "Cancelled");
        }
        // Card Eater copy jobs additionally get a detailed summary modal
        // (file counts, verification result) on top of the plain toast
        // above — same "one summary per finished destination" behavior
        // the Copy workspace's own queue used to show before it merged
        // into this drawer.
        if (j.kind === "cardeater_copy" && isTransition && (j.status === "done" || j.status === "error")) {
          ceShowSummaryForJob(j);
          maybeNativeNotify(j.label, j.status === "done" ? "Copy complete" : (j.error || "Copy failed"));
        }
      }
      S.prevStatus[j.id] = j.status;
    });
    S.jobs = jobs;
    renderBadge();
    renderDrawer();
    renderTranscribeResults();
    maybeStopPolling();
  }

  function onJobDone(job, isTransition) {
    if (job.kind === "broll" && job.result) {
      // Always adopt the latest finished analysis so the grid is populated
      // even if the job finished before this page first polled.
      S.broll = { jobId: job.id, folder: job.result.folder, clips: job.result.clips || [] };
      S.brollSel.clear();
      S.brollSelCommitted = "[]";
      SuiteUndo.reset("broll-sel");
      renderBrollResults();
      if (isTransition) {
        const msg = `${(job.result.clips || []).length} clip(s) scored.`;
        toast(`B-roll analysis complete — ${msg}`, "ok");
        maybeNativeNotify(job.label, `Analysis complete — ${msg}`);
      }
      if (S.pipeline.autoEditBrollJobId === job.id) {
        S.pipeline.autoEditBrollJobId = null;
        sendAllBrollToEdit(job.result.clips || []);
      }
    } else if (job.kind === "sync" && job.result) {
      // Adopt the latest finished detection even if it completed before this
      // page first polled (same rule as b-roll above); offsets are persisted
      // to the sidecar only on a live transition.
      adoptSyncResult(job, isTransition);
    } else if (job.kind === "harmonize" && job.result) {
      // Adopt the latest finished alignment even if it completed before this
      // page first polled (same rule as sync/broll above).
      adoptHarmonizeResult(job, isTransition);
    } else if (job.kind === "transcribe" && isTransition) {
      toast(`Transcription finished — ${job.label}`, "ok");
      maybeNativeNotify(job.label, "Transcription finished");
      if (S.pipeline.autoEditTranscribeJobIds.has(job.id)) {
        S.pipeline.autoEditTranscribeJobIds.delete(job.id);
        sendTranscribeToEdit(job.id);
      }
    } else if (job.kind === "brander_video" && isTransition) {
      toast(`Video exported: ${(job.result && job.result.path) || "done"}`, "ok");
      maybeNativeNotify(job.label, "Video exported");
    } else if (job.kind === "brander_send" && isTransition && job.result) {
      handleBranderSendDone(job);
    }
  }

  async function handleBranderSendDone(job) {
    await refreshRcsSources();
    const inserted = insertBrollCuts(job.result.cut ? [job.result.cut] : []);
    if (inserted) {
      toast(`Graphic added to the Edit timeline as B-roll (${job.result.source_id || job.label}).`, "ok");
      maybeNativeNotify(job.label, "Graphic added to the Edit timeline");
      switchWs("edit");
    }
  }

  function renderBadge() {
    const badge = $("suiteJobsBadge");
    const n = S.jobs.filter((j) => j.status === "queued" || j.status === "running").length;
    badge.textContent = String(n);
    badge.hidden = n === 0;
  }

  function openDrawer() {
    S.drawerOpen = true;
    const d = $("suiteJobsDrawer");
    d.classList.add("is-open");
    d.setAttribute("aria-hidden", "false");
    ensurePolling();
    renderDrawer(true);
  }

  function closeDrawer() {
    S.drawerOpen = false;
    const d = $("suiteJobsDrawer");
    d.classList.remove("is-open");
    d.setAttribute("aria-hidden", "true");
    maybeStopPolling();
  }

  function renderDrawer(force) {
    if (!S.drawerOpen && !force) return;
    const sig = S.jobs.map((j) =>
      `${j.id}:${j.status}:${Math.round(j.progress || 0)}:${j.detail || ""}:${S.tSent.has(j.id)}`
    ).join("|");
    if (!force && sig === S.drawerSig) return;
    S.drawerSig = sig;

    const list = $("suiteJobsList");
    if (S.jobs.length === 0) {
      list.innerHTML = `<div class="suite-drawer-empty">No background jobs yet.<br>Transcriptions, B-roll analyses and graphic renders will appear here.</div>`;
      return;
    }
    list.innerHTML = S.jobs.map((j) => {
      const isCardEaterCopy = j.kind === "cardeater_copy";
      // The generic cancel-✕ (top-right of the card) covers every OTHER
      // kind; cardeater_copy gets its own Pause/Resume/Cancel trio below
      // instead (it's the only kind that supports pausing, and its cancel
      // must call a different backend endpoint — see the click delegation).
      const running = !isCardEaterCopy && (j.status === "queued" || j.status === "running");
      const pct = Math.max(0, Math.min(100, j.status === "done" ? 100 : (j.progress || 0)));
      const statusCls = j.status === "running" ? "is-running" : j.status === "done" ? "is-done"
        : j.status === "error" ? "is-error" : j.status === "paused" ? "is-paused"
        : j.status === "queued" ? "is-queued" : "";
      let extras = "";
      if (j.status === "error" && j.error) {
        extras += `<div class="suite-job__error">${esc(j.error)}</div>`;
      }
      if (j.status === "done") {
        if (j.kind === "transcribe") {
          const sent = S.tSent.has(j.id);
          extras += `<div class="suite-job__actions">
            <button class="suite-btn suite-btn--ghost suite-btn--small" data-action="t-preview" data-id="${esc(j.id)}">Preview</button>
            <button class="suite-btn suite-btn--secondary suite-btn--small" data-action="t-send" data-id="${esc(j.id)}" ${sent ? "disabled" : ""}>${sent ? "Sent to Edit ✓" : "Send to Edit"}</button>
          </div>`;
        } else if (j.kind === "broll") {
          extras += `<div class="suite-job__actions">
            <button class="suite-btn suite-btn--secondary suite-btn--small" data-action="b-view" data-id="${esc(j.id)}">View Results</button>
          </div>`;
        } else if (j.kind === "sync") {
          extras += `<div class="suite-job__actions">
            <button class="suite-btn suite-btn--secondary suite-btn--small" data-action="sy-view" data-id="${esc(j.id)}">View Offsets</button>
          </div>`;
        } else if (j.kind === "harmonize") {
          extras += `<div class="suite-job__actions">
            <button class="suite-btn suite-btn--secondary suite-btn--small" data-action="hz-view" data-id="${esc(j.id)}">View Alignment</button>
          </div>`;
        } else if (j.kind === "brander_video" && j.result && j.result.path) {
          extras += `<div class="suite-job__result-path">${esc(j.result.path)}</div>`;
        } else if (j.kind === "brander_send") {
          extras += `<div class="suite-job__detail">Added to Edit as a B-roll source.</div>`;
        }
      }
      if (isCardEaterCopy) {
        const buttons = [];
        if (j.status === "running") buttons.push(`<button class="suite-btn suite-btn--ghost suite-btn--small" data-action="cc-pause" data-id="${esc(j.id)}">Pause</button>`);
        if (j.status === "paused") buttons.push(`<button class="suite-btn suite-btn--secondary suite-btn--small" data-action="cc-resume" data-id="${esc(j.id)}">Resume</button>`);
        if (["queued", "running", "paused"].includes(j.status)) {
          buttons.push(`<button class="suite-btn suite-btn--ghost suite-btn--small" data-action="cc-cancel" data-id="${esc(j.id)}">Cancel</button>`);
        }
        if (j.result && j.result.resolved_path) {
          buttons.push(`<button class="suite-btn suite-btn--ghost suite-btn--small" data-action="cc-open-folder" data-path="${esc(j.result.resolved_path)}">Open Folder</button>`);
          if (j.status === "done") {
            buttons.push(`<button class="suite-btn suite-btn--secondary suite-btn--small" data-action="cc-send-broll" data-path="${esc(j.result.resolved_path)}">Send to B-Roll Analyzer</button>`);
          }
        }
        if (buttons.length) extras += `<div class="suite-job__actions">${buttons.join("")}</div>`;
      }
      return `<div class="suite-job is-${esc(j.status)}">
        <div class="suite-job__head">
          <span class="suite-job__icon">${JOB_ICONS[j.kind] || "●"}</span>
          <span class="suite-job__label" title="${esc(j.label)}">${esc(j.label)}</span>
          <span class="suite-job__status ${statusCls}">${esc(j.status)}</span>
          ${running ? `<button class="suite-job__cancel" data-action="cancel" data-id="${esc(j.id)}" title="Cancel job" aria-label="Cancel ${esc(j.label)}">✕</button>` : ""}
        </div>
        <div class="suite-job__bar"><i style="width:${pct}%"></i></div>
        <div class="suite-job__detail" title="${esc(j.detail || "")}">${esc(JOB_KIND_LABELS[j.kind] || j.kind)}${j.detail ? " · " + esc(j.detail) : ""}</div>
        ${extras}
      </div>`;
    }).join("");
  }

  // ---------------- Transcribe workspace ----------------

  function renderTranscribeQueue() {
    const list = $("tQueueList");
    if (S.tQueue.length === 0) {
      list.innerHTML = `<li style="border-style:dashed;color:var(--text-faint);justify-content:center;">No videos queued</li>`;
      return;
    }
    list.innerHTML = S.tQueue.map((p, i) =>
      `<li><span title="${esc(p)}">${esc(basename(p))}</span><button data-action="t-remove" data-idx="${i}" title="Remove" aria-label="Remove ${esc(basename(p))}">✕</button></li>`
    ).join("");
  }

  async function refreshTokenStatus() {
    const res = await call("transcriber_hf_token_status");
    S.hfPresent = !!(res.ok && res.present);
    const status = $("tTokenStatus");
    const edit = $("tTokenEdit");
    if (S.hfPresent) {
      status.className = "suite-token-status";
      status.innerHTML = `✓ Hugging Face token saved <button data-action="t-token-change">change</button>`;
      edit.hidden = true;
    } else {
      status.className = "suite-token-status is-missing";
      status.textContent = "No Hugging Face token saved — needed for diarization.";
      edit.hidden = false;
    }
    const hint = $("tTokenHint");
    if (hint) hint.hidden = !$("tDiarize").checked || S.hfPresent;
  }

  async function refreshBranderGeminiKeyStatus() {
    const res = await call("brander_gemini_key_status");
    S.gxGeminiKeyPresent = !!(res.ok && res.present);
    const status = $("gxGeminiKeyStatus");
    const edit = $("gxGeminiKeyEdit");
    if (S.gxGeminiKeyPresent) {
      status.className = "suite-token-status";
      status.innerHTML = `✓ Gemini API key saved <button data-action="gx-gemini-key-change">change</button>`;
      edit.hidden = true;
    } else {
      status.className = "suite-token-status is-missing";
      status.textContent = "No Gemini API key saved — needed for Gemini mode.";
      edit.hidden = false;
    }
    const hint = $("gxGeminiKeyHint");
    if (hint) hint.hidden = S.gx.aiMode !== "gemini" || S.gxGeminiKeyPresent;
  }

  function renderTranscribeResults(force) {
    const jobs = S.jobs.filter((j) => j.kind === "transcribe");
    const sig = jobs.map((j) =>
      `${j.id}:${j.status}:${Math.round(j.progress || 0)}:${S.tExpanded.has(j.id)}:${S.tSent.has(j.id)}`
    ).join("|") + "||" + Array.from(S.tLoaded.keys()).join("|");
    if (!force && sig === S.tResultsSig) return;
    S.tResultsSig = sig;

    const host = $("tResults");
    if (jobs.length === 0 && S.tLoaded.size === 0) {
      host.innerHTML = `<p class="suite-empty">No transcriptions yet — queue videos on the left and start, or open an existing transcription.</p>`;
      return;
    }
    const loadedCards = Array.from(S.tLoaded.entries()).map(([path, meta]) => `
      <div class="suite-tjob suite-tjob--loaded">
        <div class="suite-tjob__row">
          <span class="suite-tjob__name" title="${esc(path)}">${esc(meta.name)}</span>
          <span class="suite-tjob__meta">opened from cache</span>
        </div>
        <div class="suite-tjob__row">
          <span class="suite-tjob__meta">${meta.segmentCount} segments${meta.speakerCount ? ` · ${meta.speakerCount} speaker(s)` : ""}</span>
          <div class="suite-tjob__actions">
            <button class="suite-btn suite-btn--ghost suite-btn--small" data-action="t-edit-cache" data-path="${esc(path)}">Edit Transcript</button>
            <button class="suite-btn suite-btn--secondary suite-btn--small" data-action="t-send-cache" data-path="${esc(path)}">Send to Edit</button>
          </div>
        </div>
      </div>`).join("");
    host.innerHTML = loadedCards + jobs.map((j) => {
      const pct = Math.max(0, Math.min(100, j.status === "done" ? 100 : (j.progress || 0)));
      let body = "";
      if (j.status === "queued" || j.status === "running") {
        body = `<div class="suite-job__bar"><i style="width:${pct}%"></i></div>
          <div class="suite-tjob__meta">${esc(j.status)}${j.detail ? " · " + esc(j.detail) : ""}</div>`;
      } else if (j.status === "error") {
        body = `<div class="suite-job__error">${esc(j.error || "Transcription failed.")}</div>`;
      } else if (j.status === "cancelled") {
        body = `<div class="suite-tjob__meta">Cancelled.</div>`;
      } else if (j.status === "done") {
        const segs = (j.result && j.result.segments) || [];
        const speakers = (j.result && j.result.speakers) || [];
        const expanded = S.tExpanded.has(j.id);
        const sent = S.tSent.has(j.id);
        body = `<div class="suite-tjob__row">
          <span class="suite-tjob__meta">${segs.length} segments${speakers.length ? ` · ${speakers.length} speaker(s)` : ""}</span>
          <div class="suite-tjob__actions">
            <button class="suite-btn suite-btn--ghost suite-btn--small" data-action="t-preview" data-id="${esc(j.id)}">${expanded ? "Hide Preview" : "Preview"}</button>
            <button class="suite-btn suite-btn--ghost suite-btn--small" data-action="t-edit" data-id="${esc(j.id)}">Edit Transcript</button>
            <button class="suite-btn suite-btn--secondary suite-btn--small" data-action="t-send" data-id="${esc(j.id)}" ${sent ? "disabled" : ""}>${sent ? "Sent to Edit ✓" : "Send to Edit"}</button>
          </div>
        </div>`;
        if (expanded) {
          const rows = segs.slice(0, 25).map((s) => {
            const spIdx = s.speaker ? Math.max(0, speakers.indexOf(s.speaker)) % 6 : null;
            const sp = s.speaker ? `<span class="suite-tseg__speaker suite-speaker-${spIdx}">${esc(s.speaker)}:</span> ` : "";
            return `<div class="suite-tseg"><span class="suite-tseg__tc">${mmss(s.start)}</span><span>${sp}<span class="suite-tseg__text">${esc(s.text)}</span></span></div>`;
          }).join("");
          const more = segs.length > 25 ? `<div class="suite-tjob__meta">… ${segs.length - 25} more segments (view the full transcript in Edit after sending)</div>` : "";
          body += `<div class="suite-tjob__segments">${rows}${more}</div>`;
        }
      }
      return `<div class="suite-tjob">
        <div class="suite-tjob__row">
          <span class="suite-tjob__name" title="${esc(j.label)}">${esc(j.label)}</span>
          <span class="suite-tjob__meta">${esc(j.status)}</span>
        </div>
        ${body}
      </div>`;
    }).join("");
  }

  async function sendTranscribeToEdit(jobId) {
    const res = await call("transcriber_send_to_edit", jobId);
    if (!res.ok) {
      toast(res.error || "Couldn't send that transcript to Edit.", "error");
      return;
    }
    S.tSent.add(jobId);
    const refreshed = await refreshRcsSources();
    toast(refreshed
      ? `Added "${res.source_id}" to the Edit sources.`
      : `Transcript saved (${res.source_id}) — open Edit to see it.`, "ok");
    renderTranscribeResults(true);
    renderDrawer(true);
    switchWs("edit");
  }

  // ---------------- Transcript editor (addendum A) ----------------
  //
  // The editable truth is the video's .ivt-cache.json: opening an editor
  // loads it (falling back to the in-memory job result when absent), edits
  // accumulate locally with per-video undo, "Save Edits" writes it back via
  // transcriber_update_transcript, and "Send to Edit" always goes through
  // the saved cache state (the backend re-reads it).

  const TED_CHUNK = 250; // chunked segment rendering — keeps >500-segment lists responsive
  let tEdRenderToken = 0;

  function tEdDomain() {
    return S.tEd ? "transcribe::" + S.tEd.videoPath : null;
  }

  function speakerIdx(raw) {
    const ed = S.tEd;
    if (!ed || !raw) return 0;
    return Math.max(0, ed.speakers.indexOf(raw)) % 6;
  }

  function speakerDisplay(raw) {
    const ed = S.tEd;
    if (!raw) return "";
    return (ed && ed.speaker_labels && ed.speaker_labels[raw]) || raw;
  }

  function tEdStateStr() {
    const ed = S.tEd;
    return JSON.stringify({
      segments: ed.segments,
      speakers: ed.speakers,
      speaker_labels: ed.speaker_labels,
      excluded_speakers: ed.excluded_speakers,
    });
  }

  // "Last committed" pattern: push the previous committed snapshot the
  // moment a newer state is committed, so undo entries are always the
  // pre-change states, in order.
  function tEdCommit() {
    const ed = S.tEd;
    if (!ed) return;
    const now = tEdStateStr();
    if (ed.committed != null && now !== ed.committed) {
      SuiteUndo.push(tEdDomain(), ed.committed);
      ed.dirty = true;
    }
    ed.committed = now;
    updateTEdHeader();
  }

  function tEdApplySnapshot(str) {
    const ed = S.tEd;
    if (!ed) return;
    const snap = JSON.parse(str);
    ed.segments = snap.segments;
    ed.speakers = snap.speakers;
    ed.speaker_labels = snap.speaker_labels;
    ed.excluded_speakers = snap.excluded_speakers;
    ed.committed = str;
    ed.dirty = true;
    renderTranscriptEditor(true);
  }

  function tEdUndo() {
    const ed = S.tEd;
    if (!ed) return;
    const prev = SuiteUndo.undo(tEdDomain(), tEdStateStr());
    if (prev == null) return;
    tEdApplySnapshot(prev);
  }

  function tEdRedo() {
    const ed = S.tEd;
    if (!ed) return;
    const next = SuiteUndo.redo(tEdDomain(), tEdStateStr());
    if (next == null) return;
    tEdApplySnapshot(next);
  }

  function updateTEdHeader() {
    const ed = S.tEd;
    if (!ed) return;
    const saveBtn = document.querySelector('#tEditor [data-tact="ed-save"]');
    if (saveBtn) saveBtn.disabled = !ed.dirty;
    const dirtyEl = document.querySelector("#tEditor .suite-teditor__dirty");
    if (dirtyEl) dirtyEl.hidden = !ed.dirty;
    const u = document.querySelector("#tEditor #tEdUndoBtn");
    const r = document.querySelector("#tEditor #tEdRedoBtn");
    if (u) u.disabled = !SuiteUndo.canUndo(tEdDomain());
    if (r) r.disabled = !SuiteUndo.canRedo(tEdDomain());
  }

  function openTranscriptEditor(data) {
    closeTedPlayer(); // a different transcript/job is opening — drop any open preview
    S.tEd = {
      videoPath: data.videoPath,
      name: data.name,
      jobId: data.jobId || null,
      segments: JSON.parse(JSON.stringify(data.segments || [])),
      speakers: (data.speakers || []).slice(),
      speaker_labels: Object.assign({}, data.speaker_labels || {}),
      excluded_speakers: (data.excluded_speakers || []).slice(),
      dirty: false,
      committed: null,
    };
    S.tEd.committed = tEdStateStr();
    $("tResultsHead").hidden = true;
    $("tResults").hidden = true;
    $("tEditor").hidden = false;
    renderTranscriptEditor();
    switchWs("transcribe");
  }

  function closeTranscriptEditor() {
    closeTedPlayer();
    S.tEd = null;
    $("tEditor").hidden = true;
    $("tEditor").innerHTML = "";
    $("tResultsHead").hidden = false;
    $("tResults").hidden = false;
    renderTranscribeResults(true);
  }

  async function openTranscriptEditorForJob(job) {
    const vp = job.result && job.result.video_path;
    let data = null;
    if (vp) {
      const res = await call("transcriber_load_cache", vp);
      if (res.ok && res.found) {
        data = {
          segments: res.segments || [],
          speakers: res.speakers || [],
          speaker_labels: res.speaker_labels || {},
          excluded_speakers: res.excluded_speakers || [],
        };
      }
    }
    if (!data && job.result && job.result.segments) {
      data = {
        segments: job.result.segments,
        speakers: job.result.speakers || [],
        speaker_labels: {},
        excluded_speakers: [],
      };
    }
    if (!data) {
      toast("No transcript data available for that job yet.", "error");
      return;
    }
    openTranscriptEditor(Object.assign({ videoPath: vp || job.label, name: job.label, jobId: job.id }, data));
  }

  async function openTranscriptEditorForPath(videoPath) {
    const res = await call("transcriber_load_cache", videoPath);
    if (!res.ok) { toast(res.error || "Couldn't load that transcription.", "error"); return false; }
    if (!res.found) {
      toast(`No saved transcription found next to ${basename(videoPath)} (or it's stale).`, "error", 5600);
      return false;
    }
    S.tLoaded.set(videoPath, {
      name: basename(videoPath),
      segmentCount: (res.segments || []).length,
      speakerCount: (res.speakers || []).length,
    });
    openTranscriptEditor({
      videoPath,
      name: basename(videoPath),
      jobId: null,
      segments: res.segments || [],
      speakers: res.speakers || [],
      speaker_labels: res.speaker_labels || {},
      excluded_speakers: res.excluded_speakers || [],
    });
    return true;
  }

  function renderTranscriptEditor(preserveScroll) {
    const ed = S.tEd;
    const host = $("tEditor");
    if (!ed) { host.innerHTML = ""; return; }
    const prevSegsEl = host.querySelector(".suite-teditor__segments");
    const scrollTop = preserveScroll && prevSegsEl ? prevSegsEl.scrollTop : 0;

    const speakerRows = ed.speakers.map((raw) => {
      const included = !ed.excluded_speakers.includes(raw);
      const others = ed.speakers.filter((s) => s !== raw);
      const mergeOpts = others.map((s) =>
        `<option value="${esc(s)}">${esc(speakerDisplay(s))}</option>`).join("");
      return `<div class="suite-tspk ${included ? "" : "is-excluded"}" data-raw="${esc(raw)}">
        <i class="suite-tspk__dot suite-speaker-${speakerIdx(raw)}"></i>
        <input type="text" class="suite-tspk__label" data-tact="spk-label" data-raw="${esc(raw)}"
               value="${esc(speakerDisplay(raw))}" title="Rename speaker (raw name: ${esc(raw)})"
               aria-label="Display name for ${esc(raw)}" />
        <label class="suite-tspk__inc" title="Excluded speakers are dropped when sending to Edit">
          <input type="checkbox" data-tact="spk-include" data-raw="${esc(raw)}" ${included ? "checked" : ""} />
          <span>include</span>
        </label>
        <select class="suite-tspk__merge" data-tact="spk-merge" data-raw="${esc(raw)}"
                aria-label="Merge ${esc(speakerDisplay(raw))} into another speaker" ${others.length ? "" : "disabled"}>
          <option value="">Merge into…</option>${mergeOpts}
        </select>
      </div>`;
    }).join("");

    host.innerHTML = `
      <div class="suite-teditor__head">
        <button class="suite-btn suite-btn--ghost suite-btn--small" data-tact="ed-close">‹ Back</button>
        <span class="suite-teditor__name" title="${esc(ed.videoPath)}">${esc(ed.name)}</span>
        <span class="suite-teditor__dirty" ${ed.dirty ? "" : "hidden"}>unsaved edits</span>
        <div class="suite-undo-pair">
          <button class="suite-undo-btn" id="tEdUndoBtn" data-tact="ed-undo" title="Undo (Cmd/Ctrl+Z)" aria-label="Undo transcript edit" disabled>↩</button>
          <button class="suite-undo-btn" id="tEdRedoBtn" data-tact="ed-redo" title="Redo (Shift+Cmd/Ctrl+Z)" aria-label="Redo transcript edit" disabled>↪</button>
        </div>
        <button class="suite-btn suite-btn--secondary suite-btn--small" id="tEdShowVideoBtn" data-tact="ted-toggle-video"
                aria-label="Show or hide the reference video" ${tedVideoAvailable(ed.videoPath) ? "" : "hidden"}>${tEdPlayer ? "Hide Video" : "Show Video"}</button>
        <div class="suite-teditor__head-actions">
          <button class="suite-btn suite-btn--secondary suite-btn--small" data-tact="ed-save" ${ed.dirty ? "" : "disabled"}>Save Edits</button>
          <button class="suite-btn suite-btn--primary suite-btn--small" data-tact="ed-send">Send to Edit</button>
        </div>
      </div>
      ${ed.speakers.length ? `<div class="suite-teditor__speakers">
        <h4 class="suite-teditor__subtitle">Speakers</h4>${speakerRows}
      </div>` : ""}
      <div class="suite-teditor__segments"></div>`;

    renderTEdSegments(scrollTop);
    updateTEdHeader();
  }

  function tEdSegmentRow(seg, i) {
    const ed = S.tEd;
    const excluded = seg.speaker && ed.excluded_speakers.includes(seg.speaker);
    let speakerCtl = "";
    if (ed.speakers.length || seg.speaker) {
      const opts = ed.speakers.map((s) =>
        `<option value="${esc(s)}" ${s === seg.speaker ? "selected" : ""}>${esc(speakerDisplay(s))}</option>`).join("");
      speakerCtl = `<select class="suite-tseg-edit__speaker suite-speaker-${speakerIdx(seg.speaker)}"
        data-tact="seg-speaker" data-i="${i}" aria-label="Speaker for segment ${i + 1}">
        ${opts}<option value="__new__">New speaker…</option></select>`;
    }
    const jumpBtn = tedVideoAvailable(ed.videoPath)
      ? `<button class="suite-tseg-jump" data-tact="seg-jump" data-i="${i}"
           title="Play from ${mmss(seg.start)}" aria-label="Jump to ${mmss(seg.start)} in the reference video">▶</button>`
      : "";
    return `<div class="suite-tseg-edit ${excluded ? "is-excluded" : ""}" data-i="${i}">
      <span class="suite-tseg__tc">${mmss(seg.start)}–${mmss(seg.end)}</span>
      ${jumpBtn}
      ${speakerCtl}
      <input type="text" class="suite-tseg-edit__text" data-tact="seg-text" data-i="${i}"
             value="${esc(seg.text)}" aria-label="Text of segment ${i + 1}" />
    </div>`;
  }

  // Chunked render: append TED_CHUNK rows per animation frame so a
  // 1000-segment transcript never blocks the main thread in one go.
  function renderTEdSegments(restoreScrollTop) {
    const ed = S.tEd;
    const segsHost = document.querySelector("#tEditor .suite-teditor__segments");
    if (!ed || !segsHost) return;
    const token = ++tEdRenderToken;
    segsHost.innerHTML = "";
    let i = 0;
    const step = () => {
      if (token !== tEdRenderToken || !S.tEd) return; // superseded / editor closed
      const end = Math.min(i + TED_CHUNK, ed.segments.length);
      let html = "";
      for (; i < end; i++) html += tEdSegmentRow(ed.segments[i], i);
      segsHost.insertAdjacentHTML("beforeend", html);
      if (i < ed.segments.length) {
        // setTimeout, not requestAnimationFrame: rAF is suspended entirely
        // while the document is hidden, which would strand the remaining
        // chunks un-rendered.
        setTimeout(step, 16);
      } else if (restoreScrollTop) {
        segsHost.scrollTop = restoreScrollTop;
      }
    };
    step();
  }

  // Swap a segment's speaker <select> for a one-shot text input ("New
  // speaker…"): Enter/blur commits, Escape cancels.
  function tEdBeginNewSpeaker(selectEl, segIdx) {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "suite-tseg-edit__newspeaker";
    input.placeholder = "New speaker name";
    input.setAttribute("aria-label", "New speaker name");
    let done = false;
    const commit = () => {
      if (done) return;
      done = true;
      const ed = S.tEd;
      const name = input.value.trim();
      if (!ed || !name) { renderTranscriptEditor(true); return; }
      if (!ed.speakers.includes(name)) ed.speakers.push(name);
      if (ed.segments[segIdx]) ed.segments[segIdx].speaker = name;
      tEdCommit();
      renderTranscriptEditor(true);
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      else if (e.key === "Escape") { done = true; renderTranscriptEditor(true); }
    });
    input.addEventListener("blur", commit);
    selectEl.replaceWith(input);
    input.focus();
  }

  function tEdMergeSpeaker(fromRaw, intoRaw) {
    const ed = S.tEd;
    if (!ed || !fromRaw || !intoRaw || fromRaw === intoRaw) return;
    ed.segments.forEach((seg) => { if (seg.speaker === fromRaw) seg.speaker = intoRaw; });
    ed.speakers = ed.speakers.filter((s) => s !== fromRaw);
    delete ed.speaker_labels[fromRaw];
    ed.excluded_speakers = ed.excluded_speakers.filter((s) => s !== fromRaw);
    tEdCommit();
    renderTranscriptEditor(true);
    toast(`Merged "${speakerDisplay(intoRaw) === intoRaw ? fromRaw : fromRaw}" into "${speakerDisplay(intoRaw)}".`, "ok");
  }

  async function saveTranscriptEdits() {
    const ed = S.tEd;
    if (!ed) return false;
    const res = await call("transcriber_update_transcript",
      ed.videoPath, ed.segments, ed.speakers, ed.speaker_labels, ed.excluded_speakers);
    if (!res.ok) {
      toast(res.error || "Couldn't save the transcript edits.", "error");
      return false;
    }
    ed.dirty = false;
    updateTEdHeader();
    toast("Transcript edits saved.", "ok");
    return true;
  }

  async function sendEditorToEdit() {
    const ed = S.tEd;
    if (!ed) return;
    if (ed.dirty) {
      const saved = await saveTranscriptEdits();
      if (!saved) return;
    }
    if (ed.jobId) {
      await sendTranscribeToEdit(ed.jobId); // backend re-reads the saved cache
      return;
    }
    const res = await call("transcriber_send_cache_to_edit", ed.videoPath);
    if (!res.ok) {
      toast(res.error || "Couldn't send that transcript to Edit.", "error");
      return;
    }
    const refreshed = await refreshRcsSources();
    toast(refreshed
      ? `Added "${res.source_id}" to the Edit sources.`
      : `Transcript saved (${res.source_id}) — open Edit to see it.`, "ok");
    switchWs("edit");
  }

  // ---------------- transcript editor reference video (addendum v5 §C) ----------------
  //
  // A STATIC player (#tEdPlayer in shell.html) — a sibling of #tEditor, NOT
  // inside it, since renderTranscriptEditor rebuilds #tEditor's innerHTML
  // wholesale on most edits (see that function). Modeled directly on
  // syPlayer/openSyncPlayer, but simpler: one plain, unmuted <video> (it's
  // the only audio source here — no synced audio tracks to lock to it).

  // Mirrors backend suite_api.PREVIEW_VIDEO_EXTENSIONS, PLUS .braw (Phase 3,
  // addendum v51) since broll_preview_url resolves a .braw source through
  // its cached proxy before this client ever sees a URL — see
  // _resolve_playable_path in api_broll.py. Client-side only, to hide the
  // toggle/jump controls for files broll_preview_url would reject anyway —
  // no new backend method is added; the existing generic preview server
  // (already used by B-Roll's own segment preview) still does the real
  // validation and serving.
  const TED_VIDEO_EXTENSIONS = [".mp4", ".mov", ".mxf", ".avi", ".mkv", ".m4v", ".braw"];
  function tedVideoAvailable(path) {
    if (!path) return false;
    const m = /\.[^./\\]+$/.exec(String(path));
    return !!m && TED_VIDEO_EXTENSIONS.includes(m[0].toLowerCase());
  }

  const tEdUrlCache = new Map(); // video path -> preview URL (fetched once), brollUrlCache's pattern
  let tEdPlayer = null;          // { videoEl, playing } while open; null when closed

  function updateTEdPlayerTc(vt) {
    if (!tEdPlayer) return;
    const v = tEdPlayer.videoEl;
    if (vt == null) vt = Number(v.currentTime) || 0;
    let dur = Number(v.duration);
    if (!isFinite(dur)) dur = 0;
    $("tEdPlayerTc").textContent = `${mmss(vt)} / ${mmss(dur)}`;
  }

  // Reflect the video's time into the scrubber + timecode (unless the user
  // is dragging the scrubber right now).
  function tEdPlayerUiTick() {
    if (!tEdPlayer) return;
    const vt = Number(tEdPlayer.videoEl.currentTime) || 0;
    const scrub = $("tEdPlayerScrub");
    if (scrub && document.activeElement !== scrub) scrub.value = String(vt);
    updateTEdPlayerTc(vt);
  }

  function pauseTedPlayer() {
    if (!tEdPlayer) return;
    tEdPlayer.playing = false;
    try { tEdPlayer.videoEl.pause(); } catch (e) { /* detached */ }
    const b = $("tEdPlayerPlay");
    if (b) { b.textContent = "▶"; b.classList.remove("is-active"); }
  }

  function playTedPlayer() {
    if (!tEdPlayer) return;
    tEdPlayer.playing = true;
    const b = $("tEdPlayerPlay");
    if (b) { b.textContent = "■"; b.classList.add("is-active"); }
    tEdPlayer.videoEl.play().catch(() => {}); // autoplay may be blocked
  }

  function toggleTedPlay() {
    if (!tEdPlayer) return;
    if (tEdPlayer.playing) { pauseTedPlayer(); return; }
    playTedPlayer();
  }

  function tedPlayerSeek(sec) {
    if (!tEdPlayer) return;
    try { tEdPlayer.videoEl.currentTime = sec; } catch (e) { /* metadata not ready */ }
    tEdPlayerUiTick();
  }

  // Fetch (once) and cache the preview URL, reusing broll_preview_url
  // exactly as B-Roll's own segment preview does (playBrollSegment above) —
  // it already validates+serves any allowed video file generically.
  async function tedPreviewUrl(path) {
    let url = tEdUrlCache.get(path);
    if (url) return url;
    const res = await call("broll_preview_url", path);
    if (!res.ok || !res.url) {
      toast(res.error || "Couldn't load a preview for that video.", "error");
      return null;
    }
    url = res.url;
    tEdUrlCache.set(path, url);
    return url;
  }

  // The header's toggle button is rebuilt by renderTranscriptEditor (which
  // reads `tEdPlayer` at render time), but opening/closing the player itself
  // does NOT trigger that render — so reflect the label change directly,
  // the same way updateTEdHeader() patches the undo/redo buttons in place.
  function updateTedShowVideoBtn() {
    const btn = document.querySelector('#tEditor [data-tact="ted-toggle-video"]');
    if (btn) btn.textContent = tEdPlayer ? "Hide Video" : "Show Video";
  }

  async function openTedPlayer() {
    const ed = S.tEd;
    if (!ed || !ed.videoPath) return;
    const url = await tedPreviewUrl(ed.videoPath);
    if (!url) return;
    if (!S.tEd || S.tEd !== ed) return; // editor closed/replaced while awaiting
    const panel = $("tEdPlayer");
    const videoEl = $("tEdPlayerVideo");
    videoEl.src = url;
    try { videoEl.load(); } catch (e) { /* no media in stub pane */ }
    tEdPlayer = { videoEl, playing: false };
    panel.hidden = false;
    const scrub = $("tEdPlayerScrub");
    scrub.max = "1";
    scrub.value = "0";
    updateTEdPlayerTc(0);
    updateTedShowVideoBtn();
  }

  function closeTedPlayer() {
    if (!tEdPlayer) return;
    pauseTedPlayer();
    const vid = tEdPlayer.videoEl;
    try { vid.pause(); } catch (e) { /* detached */ }
    vid.removeAttribute("src");
    try { vid.load(); } catch (e) { /* no media */ }
    tEdPlayer = null;
    const panel = $("tEdPlayer");
    if (panel) panel.hidden = true;
    updateTedShowVideoBtn();
  }

  function toggleTedPlayer() {
    if (tEdPlayer) { closeTedPlayer(); return; }
    openTedPlayer();
  }

  // Per-segment "jump to time": opens the player if not already open
  // (awaiting the URL fetch when needed), seeks to the segment's start, and
  // plays.
  async function tedJumpToSegment(i) {
    const ed = S.tEd;
    if (!ed || !ed.segments[i]) return;
    const start = Number(ed.segments[i].start) || 0;
    if (!tEdPlayer) {
      await openTedPlayer();
      if (!S.tEd || S.tEd !== ed || !tEdPlayer) return; // fetch failed, or editor moved on
    }
    tedPlayerSeek(start);
    playTedPlayer();
  }

  function wireTranscriptEditor() {
    const host = $("tEditor");

    host.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-tact]");
      if (!btn) return;
      const act = btn.dataset.tact;
      if (act === "ed-close") closeTranscriptEditor();
      else if (act === "ed-save") saveTranscriptEdits();
      else if (act === "ed-send") sendEditorToEdit();
      else if (act === "ed-undo") tEdUndo();
      else if (act === "ed-redo") tEdRedo();
      else if (act === "ted-toggle-video") toggleTedPlayer();
      else if (act === "seg-jump") tedJumpToSegment(Number(btn.dataset.i));
    });

    host.addEventListener("change", (e) => {
      const ed = S.tEd;
      const t = e.target;
      const act = t.dataset && t.dataset.tact;
      if (!ed || !act) return;
      if (act === "seg-text") {
        const i = Number(t.dataset.i);
        if (ed.segments[i]) {
          ed.segments[i].text = t.value;
          tEdCommit();
        }
      } else if (act === "seg-speaker") {
        const i = Number(t.dataset.i);
        if (t.value === "__new__") { tEdBeginNewSpeaker(t, i); return; }
        if (ed.segments[i]) {
          ed.segments[i].speaker = t.value;
          tEdCommit();
          renderTranscriptEditor(true);
        }
      } else if (act === "spk-label") {
        const raw = t.dataset.raw;
        const label = t.value.trim();
        if (label && label !== raw) ed.speaker_labels[raw] = label;
        else delete ed.speaker_labels[raw];
        tEdCommit();
        renderTranscriptEditor(true);
      } else if (act === "spk-include") {
        const raw = t.dataset.raw;
        if (t.checked) ed.excluded_speakers = ed.excluded_speakers.filter((s) => s !== raw);
        else if (!ed.excluded_speakers.includes(raw)) ed.excluded_speakers.push(raw);
        tEdCommit();
        renderTranscriptEditor(true);
      } else if (act === "spk-merge") {
        const into = t.value;
        t.value = "";
        if (into) tEdMergeSpeaker(t.dataset.raw, into);
      }
    });

    // reference video player transport (addendum v5 §C) — static elements,
    // wired once here (mirrors wireSync's one-time syPlayer wiring).
    $("tEdPlayerPlay").addEventListener("click", toggleTedPlay);
    $("tEdPlayerClose").addEventListener("click", closeTedPlayer);
    $("tEdPlayerScrub").addEventListener("input", (e) => tedPlayerSeek(parseFloat(e.target.value) || 0));
    $("tEdPlayerVideo").addEventListener("timeupdate", () => { if (tEdPlayer) tEdPlayerUiTick(); });
    $("tEdPlayerVideo").addEventListener("loadedmetadata", () => {
      if (!tEdPlayer) return;
      const scrub = $("tEdPlayerScrub");
      const dur = Number(tEdPlayer.videoEl.duration);
      if (scrub) scrub.max = isFinite(dur) && dur > 0 ? String(dur) : "1";
      updateTEdPlayerTc();
    });
    $("tEdPlayerVideo").addEventListener("ended", () => { if (tEdPlayer) pauseTedPlayer(); });
  }

  // ============================================================
  // Interview Transcriber: persisted settings (v18). Whisper model,
  // diarization, and the parallel-transcriptions limit reset to their
  // hardcoded HTML defaults on every launch — a per-USER preference, not
  // project data, so localStorage is the right store (same idiom as the
  // Cuts table's COL_RESIZE_STORAGE_KEY). Saved on every change; restored
  // once at boot AFTER #tModel's options are filled in (transcriber_models
  // is async — restoring before that would set a value the <select>
  // doesn't have an <option> for yet, silently no-oping).
  // ============================================================
  const TRANSCRIBER_SETTINGS_STORAGE_KEY = "suiteTranscriberSettings.v1";

  function saveTranscriberSettings() {
    try {
      localStorage.setItem(TRANSCRIBER_SETTINGS_STORAGE_KEY, JSON.stringify({
        model: $("tModel") ? $("tModel").value : undefined,
        diarize: $("tDiarize") ? $("tDiarize").checked : false,
        parallel: S.tParallel,
      }));
    } catch (e) {
      // localStorage disabled/full — settings just won't persist this run.
    }
  }

  function restoreTranscriberSettings() {
    let saved;
    try {
      saved = JSON.parse(localStorage.getItem(TRANSCRIBER_SETTINGS_STORAGE_KEY) || "null");
    } catch (e) {
      return;
    }
    if (!saved || typeof saved !== "object") return;
    const modelEl = $("tModel");
    if (modelEl && saved.model && Array.from(modelEl.options).some((o) => o.value === saved.model)) {
      modelEl.value = saved.model;
    }
    const diarizeEl = $("tDiarize");
    if (diarizeEl && saved.diarize) {
      diarizeEl.checked = true;
      diarizeEl.dispatchEvent(new Event("change")); // reveals the HF-token hint, same as a real click
    }
    if (typeof saved.parallel === "number") {
      S.tParallel = Math.max(1, Math.min(4, saved.parallel));
      if ($("tParValue")) $("tParValue").textContent = String(S.tParallel);
      call("transcriber_set_parallel", S.tParallel); // sync the backend worker pool, fire-and-forget like setParallel's own call
    }
  }

  function wireTranscribe() {
    $("tAddVideos").addEventListener("click", async () => {
      const res = await call("transcriber_pick_videos");
      if (!res.ok) { toastIfError(res, "Couldn't open the file dialog."); return; }
      (res.paths || []).forEach((p) => { if (!S.tQueue.includes(p)) S.tQueue.push(p); });
      renderTranscribeQueue();
    });

    $("tQueueList").addEventListener("click", (e) => {
      const btn = e.target.closest('button[data-action="t-remove"]');
      if (!btn) return;
      S.tQueue.splice(Number(btn.dataset.idx), 1);
      renderTranscribeQueue();
    });

    $("tModel").addEventListener("change", saveTranscriberSettings);

    $("tDiarize").addEventListener("change", (e) => {
      $("tTokenHint").hidden = !e.target.checked || S.hfPresent;
      if (e.target.checked) refreshTokenStatus();
      saveTranscriberSettings();
    });

    $("tTokenStatus").addEventListener("click", (e) => {
      if (!e.target.closest('button[data-action="t-token-change"]')) return;
      $("tTokenEdit").hidden = false;
      $("tTokenInput").focus();
    });

    $("tTokenSave").addEventListener("click", async () => {
      const token = $("tTokenInput").value.trim();
      const res = await call("transcriber_save_hf_token", token);
      if (!res.ok) { toast(res.error || "Couldn't save the token.", "error"); return; }
      $("tTokenInput").value = "";
      toast(token ? "Hugging Face token saved to the keychain." : "Hugging Face token removed.", "ok");
      refreshTokenStatus();
    });

    const setParallel = async (n) => {
      S.tParallel = Math.max(1, Math.min(4, n));
      $("tParValue").textContent = String(S.tParallel);
      const res = await call("transcriber_set_parallel", S.tParallel);
      if (!res.ok) toast(res.error || "Couldn't change the parallel limit.", "error");
      saveTranscriberSettings();
    };
    $("tParDown").addEventListener("click", () => setParallel(S.tParallel - 1));
    $("tParUp").addEventListener("click", () => setParallel(S.tParallel + 1));

    $("tStart").addEventListener("click", async () => {
      if (S.tQueue.length === 0) {
        toast("Add at least one video first.", "error");
        return;
      }
      const modelLabel = $("tModel").value;
      const diarize = $("tDiarize").checked;
      if (diarize && !S.hfPresent) {
        toast("Diarization needs a Hugging Face token — set one in Suite Settings (⚙) → Transcribe (or it will fail).", "info");
      }
      const btn = $("tStart");
      btn.disabled = true;
      const res = await call("transcriber_start", S.tQueue.slice(), modelLabel, diarize);
      btn.disabled = false;
      if (!res.ok) { toast(res.error || "Couldn't start transcription.", "error"); return; }
      toast(`Queued ${(res.job_ids || []).length} transcription job(s).`, "ok");
      S.tQueue = [];
      renderTranscribeQueue();
      ensurePolling();
      openDrawer();
    });

    $("tOpenExisting").addEventListener("click", async () => {
      const res = await call("transcriber_pick_videos");
      if (!res.ok) { toastIfError(res, "Couldn't open the file dialog."); return; }
      const paths = res.paths || [];
      for (const p of paths) {
        const opened = await openTranscriptEditorForPath(p);
        if (opened) {
          if (paths.length > 1) toast(`Opened ${basename(p)} — the other picked files are listed under Transcriptions.`, "info");
          // register the rest as loaded cards without opening them
          for (const q of paths.slice(paths.indexOf(p) + 1)) {
            const r = await call("transcriber_load_cache", q);
            if (r.ok && r.found) {
              S.tLoaded.set(q, {
                name: basename(q),
                segmentCount: (r.segments || []).length,
                speakerCount: (r.speakers || []).length,
              });
            } else {
              toast(`No saved transcription found for ${basename(q)}.`, "info");
            }
          }
          renderTranscribeResults(true);
          return;
        }
      }
    });

    // delegated actions in the results column
    $("tResults").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      const action = btn.dataset.action;
      if (action === "t-preview") {
        const id = btn.dataset.id;
        if (S.tExpanded.has(id)) S.tExpanded.delete(id); else S.tExpanded.add(id);
        renderTranscribeResults(true);
      } else if (action === "t-send") {
        sendTranscribeToEdit(btn.dataset.id);
      } else if (action === "t-edit") {
        const job = S.jobs.find((j) => j.id === btn.dataset.id);
        if (job) openTranscriptEditorForJob(job);
      } else if (action === "t-edit-cache") {
        openTranscriptEditorForPath(btn.dataset.path);
      } else if (action === "t-send-cache") {
        (async () => {
          const res = await call("transcriber_send_cache_to_edit", btn.dataset.path);
          if (!res.ok) { toast(res.error || "Couldn't send that transcript to Edit.", "error"); return; }
          const refreshed = await refreshRcsSources();
          toast(refreshed
            ? `Added "${res.source_id}" to the Edit sources.`
            : `Transcript saved (${res.source_id}) — open Edit to see it.`, "ok");
          switchWs("edit");
        })();
      }
    });

    wireTranscriptEditor();
    renderTranscribeQueue();
  }

  // ---------------- B-Roll workspace ----------------

  function brollSelKey(path, start, end) {
    return `${path}::${start}::${end}`;
  }

  function updateBrollSendButton() {
    const n = S.brollSel.size;
    const btn = $("bSendSelected");
    btn.textContent = `Send ${n} segment${n === 1 ? "" : "s"} to Edit`;
    btn.disabled = n === 0;
  }

  function renderBrollResults() {
    const grid = $("bGrid");
    $("bExportXml").disabled = !S.broll;
    if (!S.broll) {
      grid.innerHTML = `<p class="suite-empty">No analysis yet — choose a folder and click Analyze.</p>`;
      updateBrollSendButton();
      return;
    }
    $("bResultsTitle").textContent = `Results — ${basename(S.broll.folder) || S.broll.folder} (${S.broll.clips.length} clips)`;
    if (S.broll.clips.length === 0) {
      grid.innerHTML = `<p class="suite-empty">No video clips found in that folder.</p>`;
      updateBrollSendButton();
      return;
    }
    grid.innerHTML = S.broll.clips.map((c) => {
      const thumb = c.thumbnail_data_uri
        ? `<img class="suite-clip__thumb" src="${esc(c.thumbnail_data_uri)}" alt="Thumbnail of ${esc(c.filename)}" />`
        : `<div class="suite-clip__thumb suite-clip__thumb--placeholder">no preview</div>`;
      const score = Math.max(0, Math.min(100, Math.round(c.overall_score || 0)));
      if (c.error) {
        return `<div class="suite-clip">
        <div class="suite-clip__stage">${thumb}</div>
        <div class="suite-clip__body">
          <div class="suite-clip__name-row"><span class="suite-clip__name" title="${esc(c.path)}">${esc(c.filename)}</span></div>
          <div class="suite-clip__error">${esc(c.error)}</div>
        </div></div>`;
      }
      const chips = (c.segments || []).map((s) => {
        const key = brollSelKey(c.path, s.start, s.end);
        const checked = S.brollSel.has(key);
        // SEC-4: coerce numeric bounds to real numbers before they touch
        // markup, so a non-numeric value can never break out of an attribute.
        const st = Number(s.start) || 0;
        const en = Number(s.end) || 0;
        const scoreVal = Math.round(s.score || 0);
        return `<span class="suite-seg-chipwrap">
          <button type="button" class="suite-seg-play" data-path="${esc(c.path)}" data-start="${st}" data-end="${en}"
                  title="Preview ${mmss(st)}–${mmss(en)} (loops)" aria-label="Preview ${mmss(st)} to ${mmss(en)} of ${esc(c.filename)}">▶</button>
          <label class="suite-seg-chip ${checked ? "is-checked" : ""}">
            <input type="checkbox" data-path="${esc(c.path)}" data-start="${st}" data-end="${en}" data-score="${scoreVal}" ${checked ? "checked" : ""}
                   aria-label="Select ${mmss(st)} to ${mmss(en)} of ${esc(c.filename)}" />
            ${mmss(st)}–${mmss(en)} <span class="star">★</span>${scoreVal}
          </label>
        </span>`;
      }).join("");
      return `<div class="suite-clip" data-clip-path="${esc(c.path)}">
      <div class="suite-clip__stage" data-stage>${thumb}<div class="suite-clip__preview" data-preview></div></div>
      <div class="suite-clip__body">
        <div class="suite-clip__name-row">
          <span class="suite-clip__name" title="${esc(c.path)}">${esc(c.filename)}</span>
          <span class="suite-clip__duration">${mmss(c.duration)}</span>
        </div>
        <div class="suite-clip__scorebar"><i class="${score >= 70 ? "is-high" : ""}" style="width:${score}%"></i></div>
        <span class="suite-clip__score-label">score ${score}/100 · ${c.width || "?"}×${c.height || "?"} · ${(c.fps || 0).toFixed ? (c.fps || 0).toFixed(2) : c.fps} fps</span>
        <div class="suite-clip__segments">${chips}</div>
      </div></div>`;
    }).join("");
    updateBrollSendButton();
  }

  // Phase 2 of the Spyglass integration: read-only search/browse over the
  // whole archive. Reuses .suite-broll-grid/.suite-clip's card layout
  // (closest existing structural analog — an async result set rendered as
  // a thumbnail grid) rather than introducing a new CSS pattern for what
  // is, at this phase, a materially similar shape: one card per shot with
  // a keyframe thumbnail, caption, and tags. Facets/pool tray/tag editing/
  // native preview are Phase 3+, not this pass.
  const SG_RESULT_PAGE_SIZE = 60; // must match spyglass-py's DEFAULT_RESULT_LIMIT

  function renderSpyglassResults() {
    const grid = $("sgGrid");
    const results = S.spyglass.results;
    const loadMoreRow = $("sgLoadMoreRow");
    if (!results || results.length === 0) {
      $("sgResultsTitle").textContent = "Results";
      grid.innerHTML = `<p class="suite-empty">No search yet — type a query and press Search, or Browse whole archive.</p>`;
      if (loadMoreRow) loadMoreRow.hidden = true;
      return;
    }
    const poolIds = new Set(S.spyglass.pool.map((s) => s.shot_id));
    $("sgResultsTitle").textContent = `Results (${results.length}${S.spyglass.hasMore ? "+" : ""})`;
    if (loadMoreRow) loadMoreRow.hidden = !S.spyglass.hasMore;
    grid.innerHTML = results.map((s) => {
      const thumb = s.keyframe_data_uri
        ? `<img class="suite-clip__thumb" src="${esc(s.keyframe_data_uri)}" alt="Keyframe for ${esc(basename(s.clip_file_path))}" />`
        : `<div class="suite-clip__thumb suite-clip__thumb--placeholder">no preview</div>`;
      const tags = (s.tags || []).map((t) =>
        `<span class="suite-seg-chipwrap"><span class="suite-seg-chip">${esc(t)}
          <button type="button" class="sg-tag-remove" data-shot-id="${s.shot_id}" data-tag="${esc(t)}" title="Remove tag" aria-label="Remove tag ${esc(t)}">×</button>
        </span></span>`).join("");
      const inPool = poolIds.has(s.shot_id);
      return `<div class="suite-clip" data-shot-id="${s.shot_id}">
      <div class="suite-clip__stage sg-preview-trigger" data-shot-id="${s.shot_id}" role="button" tabindex="0" title="Click to preview ${esc(basename(s.clip_file_path))}" aria-label="Preview ${esc(basename(s.clip_file_path))}">${thumb}</div>
      <div class="suite-clip__body">
        <div class="suite-clip__name-row">
          <span class="suite-clip__name" title="${esc(s.clip_file_path)}">${esc(basename(s.clip_file_path))}</span>
          <span class="suite-clip__duration">${mmss(s.start_tc)}–${mmss(s.end_tc)}</span>
        </div>
        ${s.caption ? `<p class="suite-hint">${esc(s.caption)}</p>` : ""}
        <div class="suite-clip__segments">${tags}</div>
        <div class="suite-folder-row">
          <button type="button" class="suite-btn suite-btn--ghost sg-favorite" data-shot-id="${s.shot_id}" data-favorite="${s.is_favorite ? "1" : "0"}">${s.is_favorite ? "★ Favorited" : "☆ Favorite"}</button>
          <button type="button" class="suite-btn suite-btn--ghost sg-pool-toggle" data-shot-id="${s.shot_id}" data-in-pool="${inPool ? "1" : "0"}">${inPool ? "− Pool" : "+ Pool"}</button>
        </div>
        <div class="suite-folder-row">
          <input type="text" class="sg-add-tag" data-shot-id="${s.shot_id}" placeholder="add tag, press Enter" aria-label="Add tag to ${esc(basename(s.clip_file_path))}" />
        </div>
      </div></div>`;
    }).join("");
  }

  async function loadSpyglassFacets() {
    const res = await call("spyglass_list_facets");
    if (!res.ok) return;
    S.spyglass.allTags = (res.facets && res.facets.tags) || [];
    renderSpyglassTagFacets();
  }

  function renderSpyglassTagFacets() {
    const box = $("sgTagFacets");
    if (!S.spyglass.allTags.length) {
      box.innerHTML = `<p class="suite-hint suite-hint--tight">Run a search or Browse once to populate tags.</p>`;
      return;
    }
    box.innerHTML = S.spyglass.allTags.map((t) => {
      const active = S.spyglass.activeTags.has(t.label);
      return `<button type="button" class="suite-tag-chip ${active ? "is-active" : ""}" data-tag="${esc(t.label)}">${esc(t.label)} <span class="suite-clip__duration">${t.shot_count}</span></button>`;
    }).join("");
  }

  // "Filter by tag" is its own collapsible section (independent of the rest
  // of the Filters block) -- the tag list can get long once an archive has
  // real VLM-generated tags, so hiding it shouldn't also hide date/
  // favorites filters.
  function renderSpyglassTagFilterCollapse() {
    const open = S.spyglass.tagFilterOpen;
    $("sgTagFacets").hidden = !open;
    $("sgTagFilterChevron").textContent = open ? "▾" : "▸";
    $("sgTagFilterToggle").setAttribute("aria-expanded", String(open));
  }

  function currentSpyglassFilters() {
    const filters = { tags: Array.from(S.spyglass.activeTags), sort_by: S.spyglass.sortBy };
    if (S.spyglass.dateFrom) filters.date_from = S.spyglass.dateFrom;
    if (S.spyglass.dateTo) filters.date_to = S.spyglass.dateTo;
    if (S.spyglass.favoritesOnly) filters.favorites_only = true;
    if (S.spyglass.folderPath) filters.folder_path = S.spyglass.folderPath;
    return filters;
  }

  async function runSpyglassSearchOrBrowse() {
    const query = $("sgQuery").value.trim();
    const filters = currentSpyglassFilters();
    const limit = S.spyglass.resultLimit;
    const res = query
      ? await call("spyglass_search", query, filters, limit)
      : await call("spyglass_browse", filters, limit);
    if (!res.ok) { toast(res.error || "Search failed.", "error"); return; }
    S.spyglass.results = res.results || [];
    S.spyglass.hasMore = S.spyglass.results.length >= limit;
    renderSpyglassResults();
  }

  // Any filter change (tag/date/favorites/folder) starts back at the
  // first page -- only the "View more results" button itself grows
  // resultLimit and keeps the current filters.
  async function resetSpyglassResultsAndSearch() {
    S.spyglass.resultLimit = SG_RESULT_PAGE_SIZE;
    await runSpyglassSearchOrBrowse();
  }

  // ---- Folder tree (Search workspace left panel) ----
  //
  // There's no folder table in the schema -- watched_roots is just an
  // allowlist of top-level scan roots, and everything below is only known
  // via clips.file_path strings. spyglass_list_folder_children derives one
  // level of the tree on demand (see spyglass_core::folders), so this
  // fetches lazily per expand rather than loading the whole subtree.

  function renderSpyglassFolderNodes(nodes, depth) {
    return nodes.map((n) => {
      const isExpanded = S.spyglass.folderExpanded.has(n.path);
      const isSelected = S.spyglass.folderPath === n.path;
      const children = S.spyglass.folderNodes.get(n.path);
      const toggle = n.has_children
        ? `<button type="button" class="sg-folder-toggle" data-path="${esc(n.path)}" aria-label="${isExpanded ? "Collapse" : "Expand"} ${esc(n.name)}">${isExpanded ? "▾" : "▸"}</button>`
        : `<span class="sg-folder-toggle sg-folder-toggle--leaf"></span>`;
      const row = `<div class="suite-folder-tree-row ${isSelected ? "is-selected" : ""}" style="--sg-tree-depth:${depth}">
        ${toggle}
        <button type="button" class="sg-folder-select" data-path="${esc(n.path)}" title="${esc(n.path)}">
          <span class="sg-folder-name">${esc(n.name)}</span><span class="suite-list-row__meta">${n.shot_count}</span>
        </button>
      </div>`;
      const childrenHtml = (isExpanded && children) ? renderSpyglassFolderNodes(children, depth + 1) : "";
      return row + childrenHtml;
    }).join("");
  }

  function renderSpyglassFolderTree() {
    const box = $("sgFolderTree");
    const topLevel = S.spyglass.folderNodes.get(null);
    if (!topLevel || !topLevel.length) {
      box.innerHTML = `<p class="suite-hint suite-hint--tight">No watched folders yet — add one in Settings → Search.</p>`;
      return;
    }
    const allRow = `<div class="suite-folder-tree-row ${S.spyglass.folderPath === null ? "is-selected" : ""}">
      <span class="sg-folder-toggle sg-folder-toggle--leaf"></span>
      <button type="button" class="sg-folder-select" data-path="">All folders</button>
    </div>`;
    box.innerHTML = allRow + renderSpyglassFolderNodes(topLevel, 0);
  }

  async function loadSpyglassFolderChildren(parentPath) {
    const res = await call("spyglass_list_folder_children", parentPath);
    if (!res.ok) { toast(res.error || "Couldn't load folders.", "error"); return null; }
    const nodes = res.nodes || [];
    S.spyglass.folderNodes.set(parentPath, nodes);
    return nodes;
  }

  async function loadSpyglassFolderTree() {
    S.spyglass.folderNodes = new Map();
    S.spyglass.folderExpanded = new Set();
    await loadSpyglassFolderChildren(null);
    renderSpyglassFolderTree();
  }

  async function loadSpyglassRoots() {
    const res = await call("spyglass_list_watched_roots");
    if (!res.ok) return;
    S.spyglass.roots = res.roots || [];
    renderSpyglassRoots();
  }

  // Idle-gated background indexing (Section 7: pauses automatically while
  // the machine is in active use, resumes after min_idle_seconds) means a
  // folder that was just scanned can sit with clips registered but zero
  // shots for as long as someone's actually using this computer -- and a
  // clip with no shots yet is invisible in Search/Browse (both join
  // through `shots`), which looks exactly like "some clips are missing."
  // This surfaces that state and the "Process now" override that bypasses
  // it for one queue-drain pass -- see spyglass_force_gap_fill_now.
  async function loadSpyglassQueueStatus() {
    const res = await call("spyglass_background_work_status");
    if (!res.ok) return;
    S.spyglass.queueStatus = res.status;
    renderSpyglassQueueStatus();
  }

  function renderSpyglassQueueStatus() {
    const status = S.spyglass.queueStatus;
    const statusEl = $("sgQueueStatus");
    const toggleBtn = $("sgQueuePauseToggle");
    const forceBtn = $("sgQueueForceNow");
    if (!status) { statusEl.textContent = "Loading…"; return; }

    toggleBtn.textContent = status.manually_paused ? "Resume" : "Pause";

    const idleGated =
      !status.manually_paused &&
      !status.force_active &&
      status.idle_seconds != null &&
      status.idle_seconds < status.min_idle_seconds;

    if (status.manually_paused) {
      statusEl.textContent = "Paused.";
    } else if (status.force_active) {
      statusEl.textContent = "Processing now (forced), regardless of idle state.";
    } else if (idleGated) {
      const secondsLeft = Math.ceil(status.min_idle_seconds - status.idle_seconds);
      statusEl.textContent = `Waiting for idle (~${secondsLeft}s) — pauses automatically while this machine is in active use.`;
    } else {
      statusEl.textContent = "Running.";
    }

    forceBtn.disabled = status.manually_paused || status.force_active;
  }

  // Gap-fill indexing runs continuously in the background (in-process, via
  // spyglass_core), completely independent of this panel being open --
  // unlike the Sync/Transcribe/B-Roll/Harmonize job system, it never
  // notifies the frontend when a clip finishes. Without this, "indexed/
  // queued" froze at whatever it was the moment the panel was opened,
  // looking stuck (and inaccurate) even while indexing kept moving
  // underneath. Polls only while the panel could actually be on screen —
  // the Search workspace, or the Settings modal's Search tab.
  function spyglassRootsPanelVisible() {
    const settingsOpen = !$("suiteSettingsModal").hidden;
    return S.ws === "spyglass" || (settingsOpen && S.settingsTab === "search");
  }

  function updateSpyglassRootsPolling() {
    if (spyglassRootsPanelVisible()) {
      if (!S.spyglass.rootsPolling) {
        S.spyglass.rootsPolling = setInterval(() => {
          loadSpyglassRoots();
          loadSpyglassQueueStatus();
        }, 4000);
      }
    } else if (S.spyglass.rootsPolling) {
      clearInterval(S.spyglass.rootsPolling);
      S.spyglass.rootsPolling = null;
    }
  }

  function renderSpyglassRoots() {
    const box = $("sgRootsList");
    if (!S.spyglass.roots.length) {
      box.innerHTML = `<p class="suite-hint suite-hint--tight">No watched roots yet.</p>`;
      return;
    }
    box.innerHTML = S.spyglass.roots.map((r) => {
      const p = r.progress || {};
      const status = r.is_online ? "online" : "offline";
      const meta = `${status} · ${p.indexed || 0}/${p.discovered || 0} indexed${p.queued ? `, ${p.queued} queued` : ""}${p.failed ? `, ${p.failed} failed` : ""}`;
      return `<div class="suite-list-row" data-root-id="${r.id}">
        <span class="suite-list-row__label" title="${esc(r.path)}">${esc(r.label)} <span class="suite-list-row__meta">${meta}</span></span>
        <button type="button" class="sg-root-scan" data-root-id="${r.id}">Scan</button>
        <button type="button" class="sg-root-toggle" data-root-id="${r.id}" data-access-level="${r.access_level}">${r.access_level === "paused" ? "Resume" : "Pause"}</button>
        <button type="button" class="sg-root-reset" data-root-id="${r.id}" title="Wipe this folder's indexed clips/tags/captions and rescan it from scratch -- use after a tagging pipeline fix to re-tag just this folder">Reset &amp; rescan</button>
        <button type="button" class="sg-root-remove suite-list-row__danger" data-root-id="${r.id}">Remove</button>
      </div>`;
    }).join("");
  }

  async function loadSpyglassPool() {
    const res = await call("spyglass_pool_get");
    if (!res.ok) return;
    S.spyglass.pool = res.results || [];
    renderSpyglassPool();
    renderSpyglassResults(); // refresh "+ Pool"/"− Pool" button state on any visible cards
  }

  function renderSpyglassPool() {
    const box = $("sgPoolList");
    $("sgPoolCount").textContent = S.spyglass.pool.length;
    if (!S.spyglass.pool.length) {
      box.innerHTML = `<p class="suite-hint suite-hint--tight">Nothing pooled yet — click "+ Pool" on a result.</p>`;
      return;
    }
    box.innerHTML = S.spyglass.pool.map((s, i) => `<div class="suite-list-row" data-shot-id="${s.shot_id}">
      <span class="suite-list-row__label" title="${esc(s.clip_file_path)}">${esc(basename(s.clip_file_path))} <span class="suite-list-row__meta">${mmss(s.start_tc)}–${mmss(s.end_tc)}</span></span>
      <button type="button" class="sg-pool-up" data-index="${i}" ${i === 0 ? "disabled" : ""} title="Move up">↑</button>
      <button type="button" class="sg-pool-down" data-index="${i}" ${i === S.spyglass.pool.length - 1 ? "disabled" : ""} title="Move down">↓</button>
      <button type="button" class="sg-pool-remove suite-list-row__danger" data-shot-id="${s.shot_id}">Remove</button>
    </div>`).join("");
  }

  // ---- pool send-to: B-Roll Analyzer / Edit's B-Roll pool / Colorize ----
  //
  // Three hand-offs off the same pool tray, each reusing the target
  // workspace's own existing entry point rather than inventing a new
  // backend contract:
  //  - B-Roll Analyzer only analyzes a whole FOLDER (broll_start), same
  //    as Card Eater's own ceSendToBroll -- a pooled shot has no
  //    per-clip analyze call to hand off to, so this kicks one analysis
  //    job per unique parent folder among the pooled shots.
  //  - Edit's B-Roll pool is broll_send_to_edit's favorites-store target
  //    (kind="broll") -- the exact same call the B-Roll workspace's own
  //    "Send to Edit" button makes.
  //  - Colorize has no direct API call for "add this known path" from
  //    outside its own file; per colorize.js's file header, cross-
  //    workspace coupling to it is a DOM CustomEvent, not a shared JS
  //    closure.

  async function sgSendPoolToBroll() {
    const pool = S.spyglass.pool;
    if (!pool.length) { toast("The pool is empty — add shots before sending.", "error"); return; }
    const folders = Array.from(new Set(pool.map((s) => dirname(s.clip_file_path)).filter(Boolean)));
    if (!folders.length) { toast("Couldn't resolve a folder to analyze for these shots.", "error"); return; }
    let started = 0;
    for (const folder of folders) {
      const res = await call("broll_start", folder);
      if (res.ok) started++;
      else toastIfError(res, `Couldn't start B-Roll analysis for ${basename(folder)}.`);
    }
    if (!started) return;
    const folderInput = $("bFolder");
    if (folderInput) folderInput.value = folders[folders.length - 1];
    switchWs("broll");
    ensurePolling();
    openDrawer();
    toast(`Sent to B-Roll Analyzer — analyzing ${started} folder${started === 1 ? "" : "s"} `
      + `(${pool.length} pooled shot${pool.length === 1 ? "" : "s"}).`, "ok");
  }

  async function sgSendPoolToEditBroll() {
    const pool = S.spyglass.pool;
    if (!pool.length) { toast("The pool is empty — add shots before sending.", "error"); return; }
    const selections = pool.map((s) => ({ path: s.clip_file_path, start: s.start_tc, end: s.end_tc }));
    const res = await call("broll_send_to_edit", selections);
    if (!res.ok) { toastIfError(res, "Couldn't send the pool to Edit."); return; }
    const added = res.added || [];
    S.favorites.push(...added);
    renderBrollFavoritesPanel();
    refreshCutsRowFavoriteMarkers();
    refreshPreviewFavoriteStar();
    revealOutputBlockIfHidden();
    toast(`Sent ${added.length} shot${added.length === 1 ? "" : "s"} to Rough Cut Studio's B-Roll pool.`, "ok");
    switchWs("edit");
    if (typeof activateTab === "function") activateTab("broll");
  }

  function sgSendPoolToColorize() {
    const pool = S.spyglass.pool;
    if (!pool.length) { toast("The pool is empty — add shots before sending.", "error"); return; }
    const paths = Array.from(new Set(pool.map((s) => s.clip_file_path).filter(Boolean)));
    switchWs("colorize");
    document.dispatchEvent(new CustomEvent("suite:send-to-colorize", { detail: { paths } }));
  }

  // Click-to-play preview: ports Spyglass's own ShotPreviewPlayer.tsx
  // almost exactly (same placeholder-measure-and-Y-flip approach) --
  // the one thing NOT assumed to carry over unchanged from the original
  // Tauri/wry implementation is the coordinate math itself. A live Phase
  // 4 spike measured pywebview's cocoa webview's own AppKit frame side
  // by side against this exact `window.innerHeight` value and found them
  // identical (no ~32pt titlebar-overlap discrepancy the way wry had) --
  // so the flip below is confirmed correct for pywebview, not carried
  // over on faith.
  async function openSpyglassPreview(shot) {
    $("sgPreviewPath").textContent = shot.clip_file_path;
    $("sgPreviewPath").title = shot.clip_file_path;
    $("sgPreviewModal").hidden = false;

    const stage = $("sgPreviewStage");
    const rect = stage.getBoundingClientRect();
    const res = await call(
      "spyglass_open_preview", shot.clip_file_path, shot.start_tc,
      rect.left, window.innerHeight - rect.top - rect.height, rect.width, rect.height);
    if (!res.ok) {
      toast(res.error || "Couldn't open the preview.", "error");
      $("sgPreviewModal").hidden = true;
    }
  }

  async function closeSpyglassPreview() {
    $("sgPreviewModal").hidden = true;
    await call("spyglass_close_preview");
  }

  // RCS's own #outputBlock (Script/Cuts/Export/History, plus the
  // suite-injected Favorites/B-Roll tabs — see injectFavoritesTab/
  // injectBrollFavoritesTab) starts `hidden` and RCS only unhides it once a
  // script has been generated. Favorites and B-Roll content can now arrive
  // (a favorited transcript line, segments sent from the B-Roll workspace,
  // or the Run Pipeline "Edit" stage below) with no script in sight, so the
  // block is unhidden here too — a suite-side reveal, RCS's own file is
  // never touched. Safe to call unconditionally; a no-op once RCS has
  // already shown it.
  function revealOutputBlockIfHidden() {
    const outputBlock = document.getElementById("outputBlock");
    if (outputBlock && outputBlock.hidden) outputBlock.hidden = false;
  }

  // Run Pipeline's "Edit" stage (addendum): once the pipeline's own B-Roll
  // job finishes, forward every discovered segment straight to the Edit
  // workspace's B-Roll tab — the same backend call and favorites bookkeeping
  // as manually checking every chip in the B-Roll workspace and clicking
  // "Send to Edit", just without that manual step.
  async function sendAllBrollToEdit(clips) {
    const selections = [];
    (clips || []).forEach((c) => {
      (c.segments || []).forEach((s) => {
        selections.push({ path: c.path, start: s.start, end: s.end, score: s.score != null ? s.score : null });
      });
    });
    if (selections.length === 0) return;
    const res = await call("broll_send_to_edit", selections);
    if (!res.ok) { toastIfError(res, "Pipeline: couldn't send B-Roll segments to Edit."); return; }
    const added = res.added || [];
    S.favorites.push(...added);
    renderBrollFavoritesPanel();
    refreshCutsRowFavoriteMarkers();
    refreshPreviewFavoriteStar();
    revealOutputBlockIfHidden();
    toast(`Pipeline: sent ${added.length} B-roll segment${added.length === 1 ? "" : "s"} to the Edit workspace.`, "ok");
    switchWs("edit");
    if (typeof activateTab === "function") activateTab("broll");
  }

  // ---------------- B-roll segment preview (addendum B) ----------------
  //
  // One per-card <video> created lazily on first play, fed by
  // broll_preview_url (RCS's token preview server, Range-capable). A chip's
  // ▶ seeks to its start and loops [start, end) via timeupdate. Only one
  // preview is ever active; starting another (or re-clicking the active
  // chip, or leaving the workspace) stops it. The video plays IN PLACE of
  // the thumbnail: both share one `.suite-clip__stage` box (fixed 16:9,
  // via CSS) and only one of the two is ever visible at a time, toggled by
  // the stage's `is-previewing` class — the card never grows/shifts.
  // (The preview box is only ever display:none while nothing is playing
  // in it, never while a video is active — reintroducing that would hit
  // the same WebKit hidden-media-doesn't-play bug fixed elsewhere in Sync.)

  const brollUrlCache = new Map(); // clip path -> preview URL (fetched once)
  let bPreview = null;             // {video, stage, btn, start, end, onTime}

  function stopBrollPreview() {
    if (!bPreview) return;
    const p = bPreview;
    bPreview = null;
    try { p.video.pause(); } catch (err) { /* already detached */ }
    p.video.removeEventListener("timeupdate", p.onTime);
    if (p.btn && p.btn.isConnected) {
      p.btn.textContent = "▶";
      p.btn.classList.remove("is-active");
    }
    if (p.stage && p.stage.isConnected) p.stage.classList.remove("is-previewing");
  }

  async function playBrollSegment(btn) {
    if (bPreview && bPreview.btn === btn) { stopBrollPreview(); return; } // toggle off
    stopBrollPreview();

    const path = btn.dataset.path;
    const start = parseFloat(btn.dataset.start) || 0;
    const end = parseFloat(btn.dataset.end) || (start + 1);

    let url = brollUrlCache.get(path);
    if (!url) {
      const res = await call("broll_preview_url", path);
      if (!res.ok || !res.url) {
        toast(res.error || "Couldn't load a preview for that clip.", "error");
        return;
      }
      url = res.url;
      brollUrlCache.set(path, url);
    }

    const card = btn.closest(".suite-clip");
    const stage = card && card.querySelector("[data-stage]");
    const holder = stage && stage.querySelector("[data-preview]");
    if (!stage || !holder || !btn.isConnected) return; // grid re-rendered while awaiting

    let video = holder.querySelector("video");
    if (!video) {
      video = document.createElement("video");
      video.playsInline = true;
      video.muted = true;
      video.preload = "auto";
      holder.appendChild(video);
    }
    stage.classList.add("is-previewing"); // hides the thumbnail, shows the video

    const onTime = () => {
      // loop [start, end): jump back the moment the playhead leaves the segment
      if (video.currentTime >= end || video.currentTime < start - 0.25) {
        video.currentTime = start;
      }
    };
    const begin = () => {
      if (!bPreview || bPreview.video !== video || bPreview.btn !== btn) return; // superseded
      try { video.currentTime = start; } catch (err) { /* metadata not ready */ }
      video.play().catch(() => {}); // autoplay can be blocked; loop still arms
    };

    bPreview = { video, stage, btn, start, end, onTime };
    video.addEventListener("timeupdate", onTime);
    btn.textContent = "■";
    btn.classList.add("is-active");

    if (video.getAttribute("src") !== url) {
      video.src = url;
      video.addEventListener("loadedmetadata", begin, { once: true });
      video.load();
    } else {
      begin();
    }
  }

  // ---------------- B-roll selection undo (addendum E) ----------------

  function brollSelStateStr() {
    return JSON.stringify(Array.from(S.brollSel.values()));
  }

  function brollSelCommit() {
    const now = brollSelStateStr();
    if (now !== S.brollSelCommitted) {
      SuiteUndo.push("broll-sel", S.brollSelCommitted);
      S.brollSelCommitted = now;
    }
  }

  function applyBrollSelectionToDom() {
    document.querySelectorAll("#bGrid input[type=checkbox][data-path]").forEach((cb) => {
      const key = brollSelKey(cb.dataset.path, parseFloat(cb.dataset.start), parseFloat(cb.dataset.end));
      const on = S.brollSel.has(key);
      cb.checked = on;
      const chip = cb.closest(".suite-seg-chip");
      if (chip) chip.classList.toggle("is-checked", on);
    });
    updateBrollSendButton();
  }

  function brollSelApply(str) {
    const sels = JSON.parse(str);
    S.brollSel = new Map(sels.map((sel) => [brollSelKey(sel.path, sel.start, sel.end), sel]));
    S.brollSelCommitted = str;
    applyBrollSelectionToDom();
  }

  function brollSelUndo() {
    const prev = SuiteUndo.undo("broll-sel", brollSelStateStr());
    if (prev != null) brollSelApply(prev);
  }

  function brollSelRedo() {
    const next = SuiteUndo.redo("broll-sel", brollSelStateStr());
    if (next != null) brollSelApply(next);
  }

  // ============================================================
  // B-Roll Analyzer: persisted settings (v18). The six analysis
  // parameters (window/segments/gap/energy/energy-weight/workers) reset
  // to their hardcoded HTML defaults on every launch — same per-USER
  // preference reasoning as the Transcriber settings above. The folder
  // path itself is deliberately NOT persisted here — picking a folder is
  // part of each analysis run, not a standing preference.
  // ============================================================
  const BROLL_SETTINGS_STORAGE_KEY = "suiteBrollSettings.v1";
  const BROLL_SETTINGS_FIELD_IDS = ["bWindowSec", "bMaxSegments", "bMinGap", "bEnergyWeight", "bWorkers"];

  function saveBrollSettings() {
    try {
      const values = {};
      BROLL_SETTINGS_FIELD_IDS.forEach((id) => { if ($(id)) values[id] = $(id).value; });
      values.bEnergy = $("bEnergy") ? $("bEnergy").checked : false;
      localStorage.setItem(BROLL_SETTINGS_STORAGE_KEY, JSON.stringify(values));
    } catch (e) {
      // localStorage disabled/full — settings just won't persist this run.
    }
  }

  function restoreBrollSettings() {
    let saved;
    try {
      saved = JSON.parse(localStorage.getItem(BROLL_SETTINGS_STORAGE_KEY) || "null");
    } catch (e) {
      return;
    }
    if (!saved || typeof saved !== "object") return;
    BROLL_SETTINGS_FIELD_IDS.forEach((id) => {
      if ($(id) && saved[id] != null) $(id).value = saved[id];
    });
    if ($("bEnergy") && saved.bEnergy) {
      $("bEnergy").checked = true;
      $("bEnergyWeight").disabled = false; // mirrors the bEnergy change handler below
    }
  }

  function wireBroll() {
    $("bPickFolder").addEventListener("click", async () => {
      const res = await call("broll_pick_folder");
      if (!res.ok) { toastIfError(res, "Couldn't open the folder dialog."); return; }
      if (res.path) $("bFolder").value = res.path;
    });

    BROLL_SETTINGS_FIELD_IDS.forEach((id) => {
      if ($(id)) $(id).addEventListener("change", saveBrollSettings);
    });

    $("bEnergy").addEventListener("change", (e) => {
      $("bEnergyWeight").disabled = !e.target.checked;
      saveBrollSettings();
    });

    $("bAnalyze").addEventListener("click", async () => {
      const folder = $("bFolder").value.trim();
      if (!folder) { toast("Choose a folder of clips first.", "error"); return; }
      const options = {
        window_sec: parseFloat($("bWindowSec").value) || 4.0,
        max_segments: parseInt($("bMaxSegments").value, 10) || 1,
        min_segment_gap_sec: parseFloat($("bMinGap").value) || 1.0,
        enable_energy: $("bEnergy").checked,
        energy_weight: parseFloat($("bEnergyWeight").value) || 0.35,
        max_workers: parseInt($("bWorkers").value, 10) || 3,
      };
      const btn = $("bAnalyze");
      btn.disabled = true;
      const res = await call("broll_start", folder, options);
      btn.disabled = false;
      if (!res.ok) { toast(res.error || "Couldn't start the analysis.", "error"); return; }
      toast(`Analyzing "${basename(folder)}"…`, "ok");
      ensurePolling();
      openDrawer();
    });

    $("bGrid").addEventListener("change", (e) => {
      const cb = e.target.closest("input[type=checkbox][data-path]");
      if (!cb) return;
      const sel = { path: cb.dataset.path, start: parseFloat(cb.dataset.start), end: parseFloat(cb.dataset.end),
                    score: cb.dataset.score ? parseFloat(cb.dataset.score) : null };
      const key = brollSelKey(sel.path, sel.start, sel.end);
      if (cb.checked) S.brollSel.set(key, sel); else S.brollSel.delete(key);
      const chip = cb.closest(".suite-seg-chip");
      if (chip) chip.classList.toggle("is-checked", cb.checked);
      updateBrollSendButton();
      brollSelCommit();
    });

    $("bGrid").addEventListener("click", async (e) => {
      const play = e.target.closest(".suite-seg-play");
      if (play) {
        e.preventDefault();
        playBrollSegment(play);
      }
    });

    $("bSendSelected").addEventListener("click", async () => {
      const selections = Array.from(S.brollSel.values());
      if (selections.length === 0) return;
      const btn = $("bSendSelected");
      btn.disabled = true;
      const res = await call("broll_send_to_edit", selections);
      btn.disabled = false;
      if (!res.ok) { toast(res.error || "Couldn't send those segments to Edit.", "error"); return; }
      const added = res.added || [];
      S.favorites.push(...added);
      renderBrollFavoritesPanel();
      refreshCutsRowFavoriteMarkers();
      refreshPreviewFavoriteStar();
      revealOutputBlockIfHidden(); // no script needed to see the B-Roll tab
      const skipped = selections.length - added.length;
      toast(`Sent ${added.length} segment${added.length === 1 ? "" : "s"} to the B-Roll tab` +
        (skipped ? ` (${skipped} already there)` : "") + ".", "ok");
      switchWs("edit");
      if (typeof activateTab === "function") activateTab("broll");
    });

    $("bExportXml").addEventListener("click", async () => {
      if (!S.broll) return;
      const paths = Array.from(new Set(Array.from(S.brollSel.values()).map((s) => s.path)));
      const res = await call("broll_export_xml", S.broll.jobId, paths.length ? paths : null);
      if (!res.ok) { toastIfError(res, "Couldn't export the Premiere XML."); return; }
      toast(`Premiere XML saved to ${res.path}`, "ok");
    });
  }

  // ============================================================
  // Pool & Export panel: user-adjustable width, dragged via #sgPoolResize
  // (the grid column between results and the pool aside — see
  // .suite-ws--spyglass's `var(--sg-pool-w, 300px)` track). Persisted in
  // localStorage, same pattern as CardEater's viewer-width resizer.
  // ============================================================

  const SG_POOL_WIDTH_MIN = 260;
  const SG_POOL_WIDTH_MAX = 640;
  const SG_POOL_WIDTH_STORAGE_KEY = "suiteSgPoolWidth.v1";

  function sgGetPoolWidthPx() {
    const ws = $("workspace-spyglass");
    const raw = parseFloat(getComputedStyle(ws).getPropertyValue("--sg-pool-w"));
    return isNaN(raw) ? 300 : raw;
  }

  function sgSetPoolWidthPx(px) {
    const clamped = Math.max(SG_POOL_WIDTH_MIN, Math.min(SG_POOL_WIDTH_MAX, px));
    $("workspace-spyglass").style.setProperty("--sg-pool-w", clamped + "px");
    return clamped;
  }

  function sgLoadSavedPoolWidth() {
    let saved;
    try {
      saved = parseFloat(localStorage.getItem(SG_POOL_WIDTH_STORAGE_KEY));
    } catch (e) {
      return;
    }
    if (!isNaN(saved) && saved > 0) sgSetPoolWidthPx(saved);
  }

  function sgWirePoolResize() {
    const handle = $("sgPoolResize");
    if (!handle) return;

    handle.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const startWidth = sgGetPoolWidthPx();

      document.body.classList.add("suite-sg-pool-resizing");
      handle.classList.add("is-dragging");

      function onMove(ev) {
        // Pool panel sits to the RIGHT of the handle, so dragging left
        // (negative delta) widens it and dragging right narrows it.
        const delta = ev.clientX - startX;
        sgSetPoolWidthPx(startWidth - delta);
      }
      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.classList.remove("suite-sg-pool-resizing");
        handle.classList.remove("is-dragging");
        try {
          localStorage.setItem(SG_POOL_WIDTH_STORAGE_KEY, String(sgGetPoolWidthPx()));
        } catch (e) {
          // Private-browsing quota or localStorage disabled -- resizing
          // still works for the rest of this session, it just won't persist.
        }
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });

    // Keyboard nudge, since the handle is a focusable separator (arrow
    // keys are the conventional way to resize a native splitter).
    handle.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft") { e.preventDefault(); sgSetPoolWidthPx(sgGetPoolWidthPx() + 16); }
      else if (e.key === "ArrowRight") { e.preventDefault(); sgSetPoolWidthPx(sgGetPoolWidthPx() - 16); }
      else return;
      try {
        localStorage.setItem(SG_POOL_WIDTH_STORAGE_KEY, String(sgGetPoolWidthPx()));
      } catch (err) { /* see onUp above */ }
    });
  }

  function wireSpyglass() {
    sgLoadSavedPoolWidth();
    sgWirePoolResize();

    async function runSearchOrBrowse() {
      const btn = $("sgSearch");
      btn.disabled = true;
      await resetSpyglassResultsAndSearch();
      btn.disabled = false;
      loadSpyglassFacets(); // tag counts depend on what's actually in the archive, cheap to refresh
    }

    $("sgSearch").addEventListener("click", runSearchOrBrowse);
    $("sgQuery").addEventListener("keydown", (e) => {
      if (e.key === "Enter") runSearchOrBrowse();
    });

    $("sgBrowseAll").addEventListener("click", async () => {
      $("sgQuery").value = "";
      await runSearchOrBrowse();
    });

    $("sgTagFacets").addEventListener("click", async (e) => {
      const chip = e.target.closest(".suite-tag-chip");
      if (!chip) return;
      const tag = chip.dataset.tag;
      if (S.spyglass.activeTags.has(tag)) S.spyglass.activeTags.delete(tag);
      else S.spyglass.activeTags.add(tag);
      renderSpyglassTagFacets();
      await resetSpyglassResultsAndSearch();
    });

    // ---- date range / favorites-only filters ----

    $("sgDateFrom").addEventListener("change", async () => {
      S.spyglass.dateFrom = $("sgDateFrom").value;
      await resetSpyglassResultsAndSearch();
    });
    $("sgDateTo").addEventListener("change", async () => {
      S.spyglass.dateTo = $("sgDateTo").value;
      await resetSpyglassResultsAndSearch();
    });
    $("sgFavoritesOnly").addEventListener("change", async () => {
      S.spyglass.favoritesOnly = $("sgFavoritesOnly").checked;
      await resetSpyglassResultsAndSearch();
    });

    // ---- sort ----

    $("sgSortBy").addEventListener("change", async () => {
      S.spyglass.sortBy = $("sgSortBy").value;
      await resetSpyglassResultsAndSearch();
    });

    // ---- filter-by-tag collapse ----

    $("sgTagFilterToggle").addEventListener("click", () => {
      S.spyglass.tagFilterOpen = !S.spyglass.tagFilterOpen;
      renderSpyglassTagFilterCollapse();
    });
    renderSpyglassTagFilterCollapse();

    // ---- folder tree ----

    $("sgFolderTree").addEventListener("click", async (e) => {
      const toggleBtn = e.target.closest(".sg-folder-toggle");
      if (toggleBtn && !toggleBtn.classList.contains("sg-folder-toggle--leaf")) {
        const path = toggleBtn.dataset.path;
        if (S.spyglass.folderExpanded.has(path)) {
          S.spyglass.folderExpanded.delete(path);
        } else {
          S.spyglass.folderExpanded.add(path);
          if (!S.spyglass.folderNodes.has(path)) await loadSpyglassFolderChildren(path);
        }
        renderSpyglassFolderTree();
        return;
      }
      const selectBtn = e.target.closest(".sg-folder-select");
      if (selectBtn) {
        S.spyglass.folderPath = selectBtn.dataset.path || null;
        renderSpyglassFolderTree();
        await resetSpyglassResultsAndSearch();
      }
    });

    // ---- view more results ----

    $("sgLoadMore").addEventListener("click", async () => {
      const btn = $("sgLoadMore");
      btn.disabled = true;
      S.spyglass.resultLimit += SG_RESULT_PAGE_SIZE;
      await runSpyglassSearchOrBrowse();
      btn.disabled = false;
    });

    // ---- result-card actions (favorite / pool / tags) ----

    $("sgGrid").addEventListener("click", async (e) => {
      const favBtn = e.target.closest(".sg-favorite");
      if (favBtn) {
        const shotId = parseInt(favBtn.dataset.shotId, 10);
        const nowFavorite = favBtn.dataset.favorite !== "1";
        const res = await call("spyglass_set_favorite", shotId, nowFavorite);
        if (!res.ok) { toast(res.error || "Couldn't update favorite.", "error"); return; }
        const shot = S.spyglass.results.find((s) => s.shot_id === shotId);
        if (shot) shot.is_favorite = nowFavorite;
        renderSpyglassResults();
        return;
      }
      const poolBtn = e.target.closest(".sg-pool-toggle");
      if (poolBtn) {
        const shotId = parseInt(poolBtn.dataset.shotId, 10);
        const inPool = poolBtn.dataset.inPool === "1";
        const res = await call(inPool ? "spyglass_pool_remove" : "spyglass_pool_add", shotId);
        if (!res.ok) { toast(res.error || "Couldn't update the pool.", "error"); return; }
        await loadSpyglassPool();
        return;
      }
      const previewTrigger = e.target.closest(".sg-preview-trigger");
      if (previewTrigger) {
        const shotId = parseInt(previewTrigger.dataset.shotId, 10);
        const shot = S.spyglass.results.find((s) => s.shot_id === shotId);
        if (shot) await openSpyglassPreview(shot);
        return;
      }
      const tagRemoveBtn = e.target.closest(".sg-tag-remove");
      if (tagRemoveBtn) {
        const shotId = parseInt(tagRemoveBtn.dataset.shotId, 10);
        const res = await call("spyglass_remove_tag", shotId, tagRemoveBtn.dataset.tag);
        if (!res.ok) { toast(res.error || "Couldn't remove tag.", "error"); return; }
        const shot = S.spyglass.results.find((s) => s.shot_id === shotId);
        if (shot) shot.tags = (shot.tags || []).filter((t) => t !== tagRemoveBtn.dataset.tag);
        renderSpyglassResults();
      }
    });

    $("sgGrid").addEventListener("keydown", async (e) => {
      const trigger = e.target.closest(".sg-preview-trigger");
      if (trigger && (e.key === "Enter" || e.key === " ")) {
        e.preventDefault();
        const shotId = parseInt(trigger.dataset.shotId, 10);
        const shot = S.spyglass.results.find((s) => s.shot_id === shotId);
        if (shot) await openSpyglassPreview(shot);
        return;
      }
      const input = e.target.closest(".sg-add-tag");
      if (!input || e.key !== "Enter") return;
      const label = input.value.trim();
      if (!label) return;
      const shotId = parseInt(input.dataset.shotId, 10);
      const res = await call("spyglass_add_tag", shotId, label);
      if (!res.ok) { toast(res.error || "Couldn't add tag.", "error"); return; }
      const shot = S.spyglass.results.find((s) => s.shot_id === shotId);
      if (shot) { shot.tags = shot.tags || []; if (!shot.tags.includes(label)) shot.tags.push(label); }
      renderSpyglassResults();
    });

    // ---- background indexing queue ----

    $("sgQueuePauseToggle").addEventListener("click", async () => {
      const nextPaused = !(S.spyglass.queueStatus && S.spyglass.queueStatus.manually_paused);
      const res = await call("spyglass_set_queue_paused", nextPaused);
      if (!res.ok) { toast(res.error || "Couldn't update background indexing.", "error"); return; }
      await loadSpyglassQueueStatus();
    });

    $("sgQueueForceNow").addEventListener("click", async () => {
      const res = await call("spyglass_force_gap_fill_now");
      if (!res.ok) { toast(res.error || "Couldn't start processing now.", "error"); return; }
      toast("Processing the analysis queue now, regardless of idle state.", "ok");
      await loadSpyglassQueueStatus();
    });

    // ---- watched roots ----

    $("sgAddRoot").addEventListener("click", async () => {
      const picked = await call("spyglass_pick_watched_root_folder");
      if (!picked.ok) { toastIfError(picked, "Couldn't open the folder dialog."); return; }
      if (!picked.path) return;
      const res = await call("spyglass_add_watched_root", basename(picked.path), picked.path);
      if (!res.ok) { toast(res.error || "Couldn't add that folder.", "error"); return; }
      toast(`Added watched root "${basename(picked.path)}".`, "ok");
      await loadSpyglassRoots();
    });

    $("sgRootsList").addEventListener("click", async (e) => {
      const scanBtn = e.target.closest(".sg-root-scan");
      if (scanBtn) {
        const rootId = parseInt(scanBtn.dataset.rootId, 10);
        const root = S.spyglass.roots.find((r) => r.id === rootId);
        const res = await call("spyglass_scan_watched_root", rootId, root ? root.label : null);
        if (!res.ok) { toast(res.error || "Couldn't start the scan.", "error"); return; }
        toast(`Scanning "${root ? root.label : rootId}"…`, "ok");
        ensurePolling();
        openDrawer();
        return;
      }
      const toggleBtn = e.target.closest(".sg-root-toggle");
      if (toggleBtn) {
        const rootId = parseInt(toggleBtn.dataset.rootId, 10);
        const nextLevel = toggleBtn.dataset.accessLevel === "paused" ? "active" : "paused";
        const res = await call("spyglass_set_watched_root_access_level", rootId, nextLevel);
        if (!res.ok) { toast(res.error || "Couldn't update that root.", "error"); return; }
        await loadSpyglassRoots();
        return;
      }
      const resetBtn = e.target.closest(".sg-root-reset");
      if (resetBtn) {
        const rootId = parseInt(resetBtn.dataset.rootId, 10);
        const root = S.spyglass.roots.find((r) => r.id === rootId);
        const label = root ? root.label : rootId;
        if (!confirm(`Reset "${label}"? This clears every indexed clip/tag/caption under this folder and rescans it from scratch with the current pipeline. Other watched folders aren't affected.`)) return;
        const res = await call("spyglass_reset_watched_root", rootId);
        if (!res.ok) { toast(res.error || "Couldn't reset that root.", "error"); return; }
        toast(`Cleared ${res.removed} clip(s) from "${label}" — rescanning…`, "ok");
        const scanRes = await call("spyglass_scan_watched_root", rootId, root ? root.label : null);
        if (!scanRes.ok) { toast(scanRes.error || "Reset done, but couldn't start the rescan.", "error"); await loadSpyglassRoots(); return; }
        ensurePolling();
        openDrawer();
        await loadSpyglassRoots();
        return;
      }
      const removeBtn = e.target.closest(".sg-root-remove");
      if (removeBtn) {
        const rootId = parseInt(removeBtn.dataset.rootId, 10);
        if (!confirm("Remove this watched root? This purges every indexed clip under its path.")) return;
        const res = await call("spyglass_remove_watched_root", rootId);
        if (!res.ok) { toast(res.error || "Couldn't remove that root.", "error"); return; }
        await loadSpyglassRoots();
      }
    });

    // ---- tag maintenance ----

    $("sgPurgeOnscreenTextTags").addEventListener("click", async () => {
      if (!confirm("Purge on-screen text tags and gender/headcount tags? This removes every auto-generated tag containing a digit (jersey numbers, scoreboard scores/clocks, signs) or a boy/girl gender or headcount word across the whole archive. Tags you added yourself are not affected. This can't be undone.")) return;
      const btn = $("sgPurgeOnscreenTextTags");
      const resultBox = $("sgPurgeOnscreenTextTagsResult");
      btn.disabled = true;
      const res = await call("spyglass_purge_onscreen_text_tags");
      btn.disabled = false;
      if (!res.ok) { toast(res.error || "Couldn't purge tags.", "error"); return; }
      resultBox.textContent = `Removed ${res.removed} tag${res.removed === 1 ? "" : "s"}.`;
      toast(`Removed ${res.removed} on-screen-text/gender tag${res.removed === 1 ? "" : "s"}.`, "ok");
      loadSpyglassFacets(); // tag counts/options changed archive-wide
      // Strip the same digit-containing and gender/headcount tags from
      // already-loaded result cards in place -- matches the backend's own
      // purge predicates, so the visible chips reflect the purge
      // immediately without needing to re-run the last search/browse.
      const SG_GENDER_WORDS = new Set(["boy", "boys", "girl", "girls", "male", "female", "males", "females"]);
      S.spyglass.results.forEach((s) => {
        s.tags = (s.tags || []).filter((t) => !/\d/.test(t) && !t.split(" ").some((w) => SG_GENDER_WORDS.has(w)));
      });
      renderSpyglassResults();
    });

    // ---- shot maintenance ----

    $("sgRequeueShortShots").addEventListener("click", async () => {
      if (!confirm("Re-index every clip with a very short shot fragment (under a second, from fast pans, camera flashes, or quick cuts the old scene detector over-split)? Only affected clips are wiped and requeued for re-analysis — everything else in the index is left alone. Re-analysis runs in the background afterward.")) return;
      const btn = $("sgRequeueShortShots");
      const resultBox = $("sgRequeueShortShotsResult");
      btn.disabled = true;
      const res = await call("spyglass_requeue_short_shot_clips");
      btn.disabled = false;
      if (!res.ok) { toast(res.error || "Couldn't requeue those clips.", "error"); return; }
      resultBox.textContent = res.requeued === 0
        ? "No clips with short fragments found."
        : `Requeued ${res.requeued} clip${res.requeued === 1 ? "" : "s"} for re-analysis.`;
      toast(
        res.requeued === 0
          ? "No clips with short fragments found."
          : `Requeued ${res.requeued} clip${res.requeued === 1 ? "" : "s"} with short fragments.`,
        "ok",
      );
      if (res.requeued > 0) { ensurePolling(); openDrawer(); }
    });

    // ---- pool tray ----

    $("sgPoolList").addEventListener("click", async (e) => {
      const removeBtn = e.target.closest(".sg-pool-remove");
      if (removeBtn) {
        const res = await call("spyglass_pool_remove", parseInt(removeBtn.dataset.shotId, 10));
        if (!res.ok) { toast(res.error || "Couldn't remove from the pool.", "error"); return; }
        await loadSpyglassPool();
        return;
      }
      const upBtn = e.target.closest(".sg-pool-up");
      const downBtn = e.target.closest(".sg-pool-down");
      if (upBtn || downBtn) {
        const i = parseInt((upBtn || downBtn).dataset.index, 10);
        const j = upBtn ? i - 1 : i + 1;
        const ids = S.spyglass.pool.map((s) => s.shot_id);
        [ids[i], ids[j]] = [ids[j], ids[i]];
        const res = await call("spyglass_pool_reorder", ids);
        if (!res.ok) { toast(res.error || "Couldn't reorder the pool.", "error"); return; }
        await loadSpyglassPool();
      }
    });

    $("sgPoolClear").addEventListener("click", async () => {
      if (S.spyglass.pool.length && !confirm("Clear the whole pool?")) return;
      const res = await call("spyglass_pool_clear");
      if (!res.ok) { toast(res.error || "Couldn't clear the pool.", "error"); return; }
      await loadSpyglassPool();
    });

    $("sgPoolExportXml").addEventListener("click", async () => {
      if (!S.spyglass.pool.length) { toast("The pool is empty — add shots before exporting.", "error"); return; }
      const sequenceName = `Spyglass Pool ${new Date().toISOString().slice(0, 10)}`;
      const res = await call("spyglass_export_pool_xml", sequenceName);
      if (!res.ok) { toastIfError(res, "Couldn't export the Premiere XML."); return; }
      toast(`Premiere XML saved to ${res.path}`, "ok");
    });

    $("sgSendBroll").addEventListener("click", sgSendPoolToBroll);
    $("sgSendEditBroll").addEventListener("click", sgSendPoolToEditBroll);
    $("sgSendColorize").addEventListener("click", sgSendPoolToColorize);

    // ---- consolidate & copy export ----

    function selectedCopyMode() {
      const mode = $("sgCopyMode").value;
      if (mode === "trimmed") return { mode: "trimmed", handle_seconds: 1.0, precision: "stream_copy" };
      return { mode: "full_source" };
    }

    async function refreshConsolidateEstimate() {
      if (!S.spyglass.consolidateDest) return;
      const res = await call("spyglass_estimate_consolidate_export", S.spyglass.consolidateDest, selectedCopyMode());
      if (!res.ok) {
        $("sgConsolidateEstimate").textContent = res.error || "Couldn't estimate this export.";
        $("sgConsolidateStart").disabled = true;
        return;
      }
      const e = res.estimate;
      const gb = (e.total_bytes / 1e9).toFixed(2);
      const availGb = (e.available_bytes / 1e9).toFixed(1);
      $("sgConsolidateEstimate").textContent =
        `${e.file_count} file(s), ~${gb} GB — ${availGb} GB available at destination` +
        (e.destination_has_existing_files ? " (destination already has files)" : "");
      $("sgConsolidateStart").disabled = false;
    }

    $("sgPickConsolidateDest").addEventListener("click", async () => {
      const picked = await call("spyglass_pick_consolidate_destination");
      if (!picked.ok) { toastIfError(picked, "Couldn't open the folder dialog."); return; }
      if (!picked.path) return;
      S.spyglass.consolidateDest = picked.path;
      $("sgConsolidateDest").value = picked.path;
      await refreshConsolidateEstimate();
    });

    $("sgCopyMode").addEventListener("change", refreshConsolidateEstimate);

    $("sgConsolidateStart").addEventListener("click", async () => {
      if (!S.spyglass.consolidateDest) { toast("Choose a destination folder first.", "error"); return; }
      const btn = $("sgConsolidateStart");
      btn.disabled = true;
      const poolName = `spyglass_${new Date().toISOString().slice(0, 10)}`;
      const res = await call(
        "spyglass_start_consolidate_export", S.spyglass.consolidateDest, poolName,
        selectedCopyMode(), $("sgFolderStructure").value);
      if (!res.ok) { toast(res.error || "Couldn't start the export.", "error"); btn.disabled = false; return; }
      toast("Consolidate export started…", "ok");
      $("sgConsolidateProgress").textContent = "Running…";
      if (S.spyglass.consolidatePolling) clearInterval(S.spyglass.consolidatePolling);
      S.spyglass.consolidatePolling = setInterval(async () => {
        const statusRes = await call("spyglass_consolidate_export_status");
        if (!statusRes.ok || !statusRes.status) return;
        const st = statusRes.status;
        if (!st.finished) {
          $("sgConsolidateProgress").textContent = `${st.completed}/${st.total} — ${st.current_file || "…"}`;
          return;
        }
        clearInterval(S.spyglass.consolidatePolling);
        S.spyglass.consolidatePolling = null;
        btn.disabled = false;
        if (st.error) {
          $("sgConsolidateProgress").textContent = `Failed: ${st.error}`;
          toast(`Consolidate export failed: ${st.error}`, "error");
        } else {
          $("sgConsolidateProgress").textContent = `Done — ${st.completed}/${st.total} file(s) copied.`;
          toast("Consolidate export complete.", "ok");
          $("sgExportCopiedXml").disabled = false;
        }
      }, 500);
    });

    $("sgExportCopiedXml").addEventListener("click", async () => {
      const sequenceName = `Spyglass Copied ${new Date().toISOString().slice(0, 10)}`;
      const res = await call("spyglass_export_copied_files_xml", sequenceName);
      if (!res.ok) { toastIfError(res, "Couldn't export the Premiere XML."); return; }
      toast(`Premiere XML saved to ${res.path}`, "ok");
    });

    // ---- native preview modal ----

    $("sgPreviewClose").addEventListener("click", closeSpyglassPreview);
    $("sgPreviewModal").addEventListener("click", (e) => {
      if (e.target.id === "sgPreviewModal") closeSpyglassPreview(); // backdrop click
    });
    $("sgPreviewReveal").addEventListener("click", () => {
      const path = $("sgPreviewPath").textContent;
      if (path) call("suite_reveal_broll_media", path); // generic path-reveal helper, not actually B-Roll-specific
    });
  }

  // ============================================================
  // Sync workspace (addendum v3) — A-Sync integration
  //
  // Offset semantics (binding, from sync_core.waveform_offset): the offset
  // is how many seconds the EXTERNAL AUDIO must be DELAYED to line up with
  // the video; negative = the audio starts before the video. Readouts are
  // always formatted `+1.234 s (+1234 ms)` — sign shown, 3-decimal seconds,
  // whole milliseconds. Every committed offset change debounces a
  // sync_save_offsets sidecar write (~500ms) and pushes a "sync" SuiteUndo
  // entry (same "last committed" pattern as the other domains).
  // ============================================================

  let syCommitted = null;  // JSON of the last committed offsets state
  let sySaveTimer = null;  // ~500ms debounce for sync_save_offsets

  function formatOffset(sec) {
    const ms = Math.round((sec || 0) * 1000);
    const sign = ms < 0 ? "-" : "+";
    const abs = Math.abs(ms);
    return `${sign}${(abs / 1000).toFixed(3)} s (${sign}${abs} ms)`;
  }

  function fileStem(p) {
    return basename(p).replace(/\.[^.]+$/, "");
  }

  // "12:34 · 23.98 fps · 3840×2160 · PCM 16-bit 48 kHz stereo · TC 01:02:03:04"
  function syncProbeLine(p) {
    if (!p) return "no media info";
    const parts = [];
    if (p.duration != null) parts.push(mmss(p.duration));
    const fps = Number(p.fps);
    if (fps) parts.push(`${fps.toFixed(2).replace(/\.?0+$/, "")} fps`);
    if (p.width && p.height) parts.push(`${p.width}×${p.height}`);
    if (p.audio_format_label) parts.push(p.audio_format_label);
    if (p.timecode_tag) parts.push(`TC ${p.timecode_tag}`);
    return parts.join(" · ") || "no media info";
  }

  // ---------------- sync undo (one domain PER video, addendum v21) ----------------
  //
  // Undo used to be one shared "sync" domain for the whole workspace; now
  // that multiple videos' sync state can be loaded at once (addendum v21),
  // each gets its own domain so undoing in one project can never affect
  // another's history.

  function syncDomain() {
    return "sync::" + (S.sync.video ? S.sync.video.path : "none");
  }

  function syncStateStr() {
    return JSON.stringify((S.sync.tracks || []).map((t) => ({
      path: t.path, offset_seconds: t.offset_seconds,
      enabled: t.enabled !== false,
      channels: Array.isArray(t.channels) ? t.channels : null,
    })));
  }

  function syncCommit() {
    const now = syncStateStr();
    if (syCommitted != null && now !== syCommitted) {
      SuiteUndo.push(syncDomain(), syCommitted);
    }
    syCommitted = now;
    updateSyncUndoButtons();
  }

  function updateSyncUndoButtons() {
    $("syUndoBtn").disabled = !SuiteUndo.canUndo(syncDomain());
    $("syRedoBtn").disabled = !SuiteUndo.canRedo(syncDomain());
  }

  function syncApplySnapshot(str) {
    const byPath = new Map(JSON.parse(str).map((t) => [t.path, t]));
    (S.sync.tracks || []).forEach((t) => {
      const s = byPath.get(t.path);
      if (!s) return;
      t.offset_seconds = s.offset_seconds;
      t.enabled = s.enabled !== false;
      t.channels = Array.isArray(s.channels) ? s.channels : null;
    });
    syCommitted = str;
    renderSyncResults();
    scheduleSyncSave(); // the sidecar should reflect the restored offsets/routing too
  }

  function syncUndo() {
    if (!S.sync.tracks) return;
    const prev = SuiteUndo.undo(syncDomain(), syncStateStr());
    if (prev != null) syncApplySnapshot(prev);
  }

  function syncRedo() {
    if (!S.sync.tracks) return;
    const next = SuiteUndo.redo(syncDomain(), syncStateStr());
    if (next != null) syncApplySnapshot(next);
  }

  // ---------------- offsets: mutation + persistence ----------------

  function scheduleSyncSave() {
    clearTimeout(sySaveTimer);
    sySaveTimer = setTimeout(syncSaveOffsetsNow, 500);
  }

  // Generalized over an explicit video path (rather than always S.sync.video)
  // so a background sync-detect job that finished for a project the user
  // has since switched away from can still persist its own sidecar.
  async function saveOffsetsForPath(videoPath, tracks) {
    const payload = (tracks || [])
      .filter((t) => !t.error && t.offset_seconds != null)
      .map((t) => ({
        path: t.path, offset_seconds: t.offset_seconds,
        enabled: t.enabled !== false,
        channels: Array.isArray(t.channels) ? t.channels : null,
      }));
    if (!videoPath || payload.length === 0) return;
    const res = await call("sync_save_offsets", videoPath, payload);
    if (!res.ok) toast(res.error || "Couldn't save the sync offsets sidecar.", "error");
  }

  async function syncSaveOffsetsNow() {
    clearTimeout(sySaveTimer);
    sySaveTimer = null;
    if (!S.sync.video || !S.sync.tracks) return;
    await saveOffsetsForPath(S.sync.video.path, S.sync.tracks);
  }

  // Commit a new offset (whole milliseconds) for track i: update the row in
  // place (no full re-render, so the ms input keeps focus), push one undo
  // entry, debounce the sidecar write.
  function syncSetOffsetMs(i, ms) {
    const t = (S.sync.tracks || [])[i];
    if (!t || t.error) return;
    const rounded = Math.round(Number(ms));
    if (!isFinite(rounded)) { renderSyncResults(); return; } // bad input — reset the row
    t.offset_seconds = rounded / 1000;
    const row = document.querySelector(`#syResults .suite-sync-track[data-i="${i}"]`);
    if (row) {
      const readout = row.querySelector("[data-offset-readout]");
      if (readout) readout.textContent = formatOffset(t.offset_seconds);
      const input = row.querySelector(".suite-sync-msinput");
      if (input) input.value = String(rounded);
    }
    renderSyncWaveform(); // reposition only -- peaks are already cached, no refetch
    syncCommit();
    scheduleSyncSave();
  }

  // Commit a routing change (enabled / channels) for track i: mutate, re-render
  // (dims the row, toggles the channel select + transcribe button), push one
  // undo entry, debounce the sidecar write. renderSyncResults reconciles the
  // live preview (add/remove/mute audio elements) if a player is open.
  function syncSetRouting(i, patch) {
    const t = (S.sync.tracks || [])[i];
    if (!t || t.error) return;
    Object.assign(t, patch);
    if (t.enabled === false) { t.previewSolo = false; t.previewMuted = false; }
    renderSyncResults();
    renderSyncWaveform(); // enabling/disabling changes which rows should show
    syncCommit();
    scheduleSyncSave();
  }

  // Preview-only mute/solo toggle for track i — never persisted, never an undo
  // entry. Just re-renders (for the button state) and re-applies preview gains.
  function syncTogglePreviewFlag(i, flag) {
    const t = (S.sync.tracks || [])[i];
    if (!t || t.error || t.enabled === false) return;
    t[flag] = !t[flag];
    renderSyncResults();
  }

  // ============================================================
  // Synced preview player (addendum v4 §B) — proxy-free, browser-mixed
  //
  // A single MUTED <video> = the picture; one <audio> per ENABLED track =
  // the recorders. On every tick (video `timeupdate` + a rAF loop while
  // playing) each audio is locked to the picture per the v3 sign convention:
  //   audio.currentTime = video.currentTime - offset
  // A track whose audio hasn't started yet (video.currentTime < offset →
  // target < 0) is paused/silent and resumes once the picture passes it.
  // We only hard-correct an element when it drifts > ~40 ms from its target
  // (the standalone MixPlayer's "seek once, then let it run" lesson). Mute /
  // solo are applied via volume; solo mutes every other track. One player at
  // a time; teardown stops every element when leaving the workspace, closing
  // the player, or re-detecting. Preview URLs are fetched once and cached on
  // the track (t._previewUrl) / on S.sync.video.
  // ============================================================

  const SYNC_DRIFT_TOLERANCE = 0.04; // seconds
  // Each correction is a real HTTP byte-range seek against the local
  // preview server (the audio is streamed, not a file:// src) — issuing
  // one every animation-frame tick (~60/s) doesn't keep two media elements
  // in sync, it starves the audio element of a chance to ever finish a
  // fetch and resume: a short buffered snippet plays, then it stalls
  // silently forever while the video (never reseeked) keeps running. This
  // is exactly the "seek once, then let it run" lesson the standalone
  // A-Sync app's own MixPlayer already learned (see its module notes) —
  // corrections must be rare, not continuous.
  const SYNC_CORRECTION_INTERVAL_MS = 1000;
  let syPlayer = null; // { videoEl, audios: Map(path -> <audio>), raf, playing }

  function trackByPath(p) {
    return (S.sync.tracks || []).find((t) => t.path === p) || null;
  }

  // Fetch (once) and cache the preview URL for a path onto `holder`.
  async function syncPreviewUrl(path, holder) {
    if (holder && holder._previewUrl) return holder._previewUrl;
    const res = await call("sync_preview_url", path);
    if (!res.ok || !res.url) {
      toast(res.error || `Couldn't load a preview for ${basename(path)}.`, "error");
      return null;
    }
    if (holder) holder._previewUrl = res.url;
    return res.url;
  }

  function anyPreviewSolo() {
    return (S.sync.tracks || []).some((t) => t.previewSolo && t.enabled !== false && !t.error);
  }

  // Reconcile the set of <audio> elements with the currently enabled tracks:
  // create one per newly-enabled track, drop those no longer enabled.
  async function syncPlayerReconcile() {
    if (!syPlayer) return;
    const host = $("syPlayerAudios");
    const enabled = (S.sync.tracks || []).filter(
      (t) => !t.error && t.enabled !== false && t.offset_seconds != null);
    const want = new Set(enabled.map((t) => t.path));
    Array.from(syPlayer.audios.keys()).forEach((path) => {
      if (want.has(path)) return;
      const el = syPlayer.audios.get(path);
      try { el.pause(); } catch (e) { /* detached */ }
      el.removeAttribute("src");
      try { el.load(); } catch (e) { /* no media */ }
      el.remove();
      syPlayer.audios.delete(path);
    });
    for (const t of enabled) {
      if (syPlayer.audios.has(t.path)) continue;
      const url = await syncPreviewUrl(t.path, t);
      if (!syPlayer) return;      // torn down while awaiting
      if (!url) continue;
      if (syPlayer.audios.has(t.path)) continue; // added by a concurrent reconcile
      const el = document.createElement("audio");
      el.preload = "auto";
      el.dataset.path = t.path;
      el.src = url;
      if (host) host.appendChild(el);
      syPlayer.audios.set(t.path, el);
    }
  }

  // Apply mute / solo / enabled → volume for every audio element.
  function syncPlayerApplyGains() {
    if (!syPlayer) return;
    const solo = anyPreviewSolo();
    syPlayer.audios.forEach((el, path) => {
      const t = trackByPath(path);
      const silence = !t || t.enabled === false || t.previewMuted || (solo && !t.previewSolo);
      el.volume = silence ? 0 : 1;
    });
  }

  // Lock every audio element to the picture (the offset math). Called every
  // rAF tick during playback, so corrections are throttled (see
  // SYNC_CORRECTION_INTERVAL_MS above) and never overlap an in-flight seek —
  // EXCEPT when `force` is true, for the handful of deliberate, one-time
  // repositioning moments (pressing Play, scrubbing, opening the player, an
  // offset/routing change) where waiting out the throttle would make the
  // preview feel unresponsive.
  function syncPlayerLock(force) {
    if (!syPlayer) return;
    const vt = Number(syPlayer.videoEl.currentTime) || 0;
    const now = (window.performance && performance.now()) || Date.now();
    syPlayer.audios.forEach((el, path) => {
      const t = trackByPath(path);
      if (!t) return;
      const target = vt - (t.offset_seconds || 0);
      if (target < 0) {
        // The recorder hasn't started at this picture time — silence it.
        if (!el.paused) { try { el.pause(); } catch (e) { /* detached */ } }
        return;
      }
      if (force) el._syLastCorrection = 0;
      if (!el.seeking) {
        const last = el._syLastCorrection || 0;
        if (now - last >= SYNC_CORRECTION_INTERVAL_MS &&
            Math.abs((Number(el.currentTime) || 0) - target) > SYNC_DRIFT_TOLERANCE) {
          try { el.currentTime = target; el._syLastCorrection = now; }
          catch (e) { /* metadata not ready */ }
        }
      }
      if (syPlayer.playing && el.paused && !el.seeking) el.play().catch(() => {});
    });
  }

  function updateSyncPlayerTc(vt) {
    if (!syPlayer) return;
    const v = S.sync.video;
    if (vt == null) vt = Number(syPlayer.videoEl.currentTime) || 0;
    let dur = (v && v.probe && Number(v.probe.duration)) || 0;
    if (!dur) dur = Number(syPlayer.videoEl.duration) || 0;
    if (!isFinite(dur)) dur = 0;
    $("syPlayerTc").textContent = `${mmss(vt)} / ${mmss(dur)}`;
  }

  // Reflect the picture time into the scrubber + timecode (unless the user is
  // dragging the scrubber right now).
  function syncPlayerUiTick() {
    if (!syPlayer) return;
    const vt = Number(syPlayer.videoEl.currentTime) || 0;
    const scrub = $("syPlayerScrub");
    if (scrub && document.activeElement !== scrub) scrub.value = String(vt);
    updateSyncPlayerTc(vt);
  }

  function startSyncPlayerRaf() {
    if (!syPlayer) return;
    const loop = () => {
      if (!syPlayer || !syPlayer.playing) return;
      syncPlayerLock();
      syncPlayerUiTick();
      syPlayer.raf = requestAnimationFrame(loop);
    };
    syPlayer.raf = requestAnimationFrame(loop);
  }

  function pauseSyncPlayer() {
    if (!syPlayer) return;
    syPlayer.playing = false;
    if (syPlayer.raf != null) { cancelAnimationFrame(syPlayer.raf); syPlayer.raf = null; }
    try { syPlayer.videoEl.pause(); } catch (e) { /* detached */ }
    syPlayer.audios.forEach((el) => { try { el.pause(); } catch (e) { /* detached */ } });
    const b = $("syPlayerPlay");
    if (b) { b.textContent = "▶"; b.classList.remove("is-active"); }
  }

  function toggleSyncPlay() {
    if (!syPlayer) return;
    if (syPlayer.playing) { pauseSyncPlayer(); return; }
    syPlayer.playing = true;
    const b = $("syPlayerPlay");
    if (b) { b.textContent = "■"; b.classList.add("is-active"); }
    syncPlayerApplyGains();
    syncPlayerLock(true);
    syPlayer.videoEl.play().catch(() => {}); // autoplay may be blocked; lock still runs
    startSyncPlayerRaf();
  }

  function syncPlayerSeek(sec) {
    if (!syPlayer) return;
    try { syPlayer.videoEl.currentTime = sec; } catch (e) { /* metadata not ready */ }
    syncPlayerLock(true);
    syncPlayerUiTick();
  }

  async function openSyncPlayer() {
    const v = S.sync.video;
    if (!v) return;
    const url = await syncPreviewUrl(v.path, v);
    if (!url) return;
    const panel = $("syPlayer");
    const videoEl = $("syPlayerVideo");
    videoEl.muted = true;
    videoEl.src = url;
    try { videoEl.load(); } catch (e) { /* no media in stub pane */ }
    syPlayer = { videoEl, audios: new Map(), raf: null, playing: false };
    panel.hidden = false;
    const tog = $("syPreviewToggle");
    if (tog) { tog.classList.add("is-active"); tog.textContent = "Hide Preview"; }
    const scrub = $("syPlayerScrub");
    const dur = (v.probe && Number(v.probe.duration)) || 0;
    scrub.max = dur > 0 ? String(dur) : "1";
    scrub.value = "0";
    updateSyncPlayerTc(0);
    await syncPlayerReconcile();
    if (!syPlayer) return; // torn down while awaiting
    syncPlayerApplyGains();
    syncPlayerLock(true);
  }

  function teardownSyncPlayer() {
    if (!syPlayer) return;
    pauseSyncPlayer();
    syPlayer.audios.forEach((el) => {
      try { el.pause(); } catch (e) { /* detached */ }
      el.removeAttribute("src");
      try { el.load(); } catch (e) { /* no media */ }
      el.remove();
    });
    syPlayer.audios.clear();
    const vid = syPlayer.videoEl;
    try { vid.pause(); } catch (e) { /* detached */ }
    vid.removeAttribute("src");
    try { vid.load(); } catch (e) { /* no media */ }
    syPlayer = null;
    const host = $("syPlayerAudios");
    if (host) host.innerHTML = "";
    const panel = $("syPlayer");
    if (panel) panel.hidden = true;
    const tog = $("syPreviewToggle");
    if (tog) { tog.classList.remove("is-active"); tog.textContent = "Preview Sync"; }
  }

  function toggleSyncPlayer() {
    if (syPlayer) { teardownSyncPlayer(); return; }
    openSyncPlayer();
  }

  // ---------------- rendering ----------------

  function renderSyncRail() {
    const v = S.sync.video;
    $("syVideoRow").hidden = !v;
    if (v) {
      const name = $("syVideoName");
      name.textContent = basename(v.path);
      name.title = v.path;
      $("syVideoInfo").textContent = v.probed ? syncProbeLine(v.probe) : "probing…";
    }

    const sc = S.sync.sidecar;
    const showSaved = !!(v && sc && sc.found);
    $("sySavedStrip").hidden = !showSaved;
    if (showSaved) {
      const n = (sc.tracks || []).length;
      $("sySavedLabel").textContent = `Saved offsets found for this video (${n} track${n === 1 ? "" : "s"}).`;
    }

    const list = $("syAudioList");
    if (S.sync.audios.length === 0) {
      list.innerHTML = `<li style="border-style:dashed;color:var(--text-faint);align-items:center;">No audio files</li>`;
    } else {
      list.innerHTML = S.sync.audios.map((a, i) => `<li>
        <div class="suite-sync-audio__row">
          <span title="${esc(a.path)}">${esc(basename(a.path))}</span>
          <button data-action="sy-remove" data-idx="${i}" title="Remove" aria-label="Remove ${esc(basename(a.path))}">✕</button>
        </div>
        <span class="suite-sync-file__info">${a.probed ? esc(syncProbeLine(a.probe)) : "probing…"}</span>
      </li>`).join("");
    }

    $("syDetect").disabled = !(v && S.sync.audios.length > 0);
    renderSyncProjectTabs();
  }

  // Inline confirm strip: reads the model + diarization CURRENTLY selected
  // in the Transcribe workspace's live controls at open time.
  function syncConfirmStrip(i) {
    const model = $("tModel") ? $("tModel").value : "";
    const diarize = $("tDiarize") ? $("tDiarize").checked : false;
    return `<div class="suite-sync-confirm">
      <span class="suite-sync-confirm__txt">Transcribe onto the video timeline with
        <strong>${esc(model || "(no model)")}</strong> · diarization <strong>${diarize ? "on" : "off"}</strong>
        (current Transcribe settings) — no proxy file is rendered.</span>
      <div class="suite-sync-confirm__actions">
        <button class="suite-btn suite-btn--secondary suite-btn--small" data-action="sy-confirm" data-i="${i}">Confirm</button>
        <button class="suite-btn suite-btn--ghost suite-btn--small" data-action="sy-cancel" data-i="${i}">Cancel</button>
      </div>
    </div>`;
  }

  // ---------------- routing (addendum v4 §C) ----------------
  //
  // Each track carries `enabled` (bool, default true — a disabled track is
  // excluded from preview, export and transcription) and `channels`
  // (list[int] of 1-based SOURCE channels, or null = all). The channel
  // select maps: "All channels"→null, "Channel k"→[k], "Downmix to mono"→
  // [0] (0 = whole-file mixdown, a single clipitem with no <sourcetrack> —
  // the exporters interpret it that way). `previewMuted`/`previewSolo` are
  // transient preview-only flags (never persisted, never in the undo state).

  function normalizeTrackRouting(t) {
    t.enabled = t.enabled !== false;
    t.channels = Array.isArray(t.channels) ? t.channels : null;
    return t;
  }

  function selectValueFromChannels(channels) {
    if (!Array.isArray(channels) || channels.length === 0) return "all";
    if (channels.length === 1 && channels[0] === 0) return "mono";
    if (channels.length === 1 && channels[0] >= 1) return "ch-" + channels[0];
    return "all";
  }

  function channelsFromSelectValue(val) {
    if (val === "mono") return [0];
    const m = /^ch-(\d+)$/.exec(val || "");
    if (m) return [Number(m[1])];
    return null; // "all" (and any unknown) → all channels
  }

  function channelSelectHtml(t, i) {
    const n = Math.max(1, (t.probe && Number(t.probe.audio_channels)) || 1);
    const cur = selectValueFromChannels(t.channels);
    const opt = (val, label) =>
      `<option value="${val}"${val === cur ? " selected" : ""}>${esc(label)}</option>`;
    let opts = opt("all", "All channels");
    for (let k = 1; k <= n; k++) opts += opt("ch-" + k, "Channel " + k);
    if (n >= 2) opts += opt("mono", "Downmix to mono");
    const dis = t.enabled === false ? " disabled" : "";
    return `<label class="suite-sync-chanfield">
      <span>Channels</span>
      <select class="suite-sync-chanselect" data-action="sy-channel" data-i="${i}"${dis}
              aria-label="Source channels of ${esc(t.filename || basename(t.path))}">${opts}</select>
    </label>`;
  }

  function syncTrackRow(t, i) {
    const fmt = t.probe && t.probe.audio_format_label
      ? `<span class="suite-sync-track__fmt">${esc(t.probe.audio_format_label)}</span>` : "";
    const head = `<div class="suite-sync-track__head">
      <span class="suite-sync-track__name" title="${esc(t.path)}">${esc(t.filename || basename(t.path))}</span>
      ${fmt}
    </div>`;
    if (t.error) {
      return `<div class="suite-sync-track is-error" data-i="${i}">${head}
        <div class="suite-sync-track__error">Detection failed — ${esc(t.error)}</div>
      </div>`;
    }
    const disabled = t.enabled === false;
    const ms = Math.round((t.offset_seconds || 0) * 1000);
    const nudges = [-100, -10, 10, 100].map((d) =>
      `<button class="suite-sync-nudge" data-action="sy-nudge" data-i="${i}" data-ms="${d}"
        title="Nudge ${d > 0 ? "+" : "−"}${Math.abs(d)}ms" aria-label="Nudge ${esc(t.filename || basename(t.path))} by ${d} milliseconds">${d > 0 ? "+" : "−"}${Math.abs(d)}ms</button>`).join("");
    const soloDis = disabled ? " disabled" : "";
    const previewCtl = `<div class="suite-sync-previewctl" role="group" aria-label="Preview controls for ${esc(t.filename || basename(t.path))}">
        <button class="suite-sync-ptoggle${t.previewMuted ? " is-active" : ""}" data-action="sy-mute" data-i="${i}"${soloDis}
          title="Mute this track in the preview" aria-pressed="${t.previewMuted ? "true" : "false"}">Mute</button>
        <button class="suite-sync-ptoggle${t.previewSolo ? " is-active" : ""}" data-action="sy-solo" data-i="${i}"${soloDis}
          title="Solo this track in the preview (mutes the others)" aria-pressed="${t.previewSolo ? "true" : "false"}">Solo</button>
      </div>`;
    return `<div class="suite-sync-track${disabled ? " is-disabled" : ""}" data-i="${i}">
      ${head}
      <div class="suite-sync-offsetrow">
        <span class="suite-sync-offset" data-offset-readout title="Delay applied to this recording so it lines up with the video">${esc(formatOffset(t.offset_seconds))}</span>
        <div class="suite-sync-nudges" role="group" aria-label="Nudge offset of ${esc(t.filename || basename(t.path))}">${nudges}</div>
        <label class="suite-sync-msfield">
          <input type="number" class="suite-sync-msinput" data-i="${i}" step="1" value="${ms}"
                 aria-label="Offset of ${esc(t.filename || basename(t.path))} in milliseconds" />
          <span>ms</span>
        </label>
      </div>
      <div class="suite-sync-routing">
        <label class="suite-sync-enable">
          <input type="checkbox" data-action="sy-enable" data-i="${i}" ${disabled ? "" : "checked"}
                 aria-label="Include ${esc(t.filename || basename(t.path))}" />
          <span>Enabled</span>
        </label>
        ${channelSelectHtml(t, i)}
        ${previewCtl}
      </div>
      ${disabled ? "" : `<div class="suite-sync-track__actions">
        <button class="suite-btn suite-btn--ghost suite-btn--small" data-action="sy-transcribe" data-i="${i}">Transcribe this track (no proxy)</button>
      </div>`}
      ${!disabled && S.sync.confirmIdx === i ? syncConfirmStrip(i) : ""}
    </div>`;
  }

  function renderSyncResults() {
    const host = $("syResults");
    const tracks = S.sync.tracks;
    updateSyncUndoButtons();
    const enabledTracks = (tracks || []).filter((t) => !t.error && t.enabled !== false && t.offset_seconds != null);
    $("syExportXml").disabled = !(S.sync.video && tracks && enabledTracks.length > 0);
    if ($("syPreviewToggle")) {
      $("syPreviewToggle").disabled = !(S.sync.video && enabledTracks.length > 0);
    }
    $("syResultsTitle").textContent = S.sync.video && tracks
      ? `Offsets — ${basename(S.sync.video.path)}` : "Offsets";
    if (!tracks) {
      host.innerHTML = `<p class="suite-empty">No sync results yet — choose a video and audio files on the left, then run Detect Sync.</p>`;
    } else if (tracks.length === 0) {
      host.innerHTML = `<p class="suite-empty">The sync job returned no tracks.</p>`;
    } else {
      host.innerHTML =
        `<p class="suite-hint suite-hint--tight suite-sync-lede">Offset = how much each recording is delayed to line up with the video (negative = it starts before the video). Enable/disable and pick source channels per track — changes are saved next to the video automatically.</p>` +
        tracks.map((t, i) => syncTrackRow(t, i)).join("");
    }
    // Keep the open player's audio elements in step with the enabled set and
    // re-apply mute/solo gains + the offset lock (live routing changes).
    if (syPlayer) {
      syncPlayerReconcile().then(() => { if (syPlayer) { syncPlayerApplyGains(); syncPlayerLock(true); } });
    }
  }

  // ---------------- waveform visual (addendum v20) ----------------
  //
  // Read-only multitrack waveform view: the video's own audio as a
  // reference row, plus one row per enabled external track, each drawn
  // at its current (possibly nudged) offset — the browser-based
  // counterpart to the standalone A-Sync app's own WaveformCanvas.
  // Peaks are fetched ONCE per detect/restore (sync_peaks — a real
  // decode+downsample in A-Sync's own venv) and cached directly on
  // S.sync.video/S.sync.tracks; a nudge or routing change only
  // repositions the already-fetched data (renderSyncWaveform), never
  // refetches (loadSyncWaveform).

  const SYNC_WAVE_ROW_H = 56;
  const SYNC_WAVE_ROW_GAP = 6;
  const SYNC_WAVE_TRACK_COLORS = ["#00b2ba", "#f15d22", "#5f8fb4", "#dd971a", "#74a333", "#770055"];

  let syncWaveRedrawTimer = null;
  function scheduleSyncWaveformRedraw() {
    clearTimeout(syncWaveRedrawTimer);
    syncWaveRedrawTimer = setTimeout(renderSyncWaveform, 120);
  }

  // ---------------- waveform zoom (addendum v21) ----------------
  //
  // zoom=1 renders the whole timeline fit to the host's width (the original
  // behavior). Zooming in widens the canvas beyond that and #syWaveScroll
  // (overflow-x: auto) picks up a native horizontal scrollbar/trackpad-pan —
  // simpler and more robust than hand-rolled panning logic.

  const SYNC_ZOOM_STEP = 1.6;
  const SYNC_ZOOM_MAX = 20;

  function updateSyncZoomLabel() {
    const label = $("syWaveZoomLabel");
    if (label) label.textContent = S.sync.zoom <= 1.01 ? "Fit" : `${S.sync.zoom.toFixed(1)}×`;
  }

  function setSyncZoom(factor) {
    S.sync.zoom = Math.max(1, Math.min(SYNC_ZOOM_MAX, factor));
    updateSyncZoomLabel();
    renderSyncWaveform();
  }

  function zoomSyncIn() { setSyncZoom(S.sync.zoom * SYNC_ZOOM_STEP); }
  function zoomSyncOut() { setSyncZoom(S.sync.zoom / SYNC_ZOOM_STEP); }
  function resetSyncZoom() { setSyncZoom(1); }

  async function loadSyncWaveform() {
    const v = S.sync.video;
    const tracks = (S.sync.tracks || []).filter((t) => !t.error);
    if (!v || tracks.length === 0) { renderSyncWaveform(); return; }
    const paths = [v.path, ...tracks.map((t) => t.path)];
    const res = await call("sync_peaks", paths);
    if (!S.sync.video || S.sync.video.path !== v.path) return; // superseded
    const peaks = (res.ok && res.peaks) || {};
    const vp = peaks[v.path];
    v.peaks = (vp && !vp.error) ? vp : null;
    tracks.forEach((t) => {
      const tp = peaks[t.path];
      t.peaks = (tp && !tp.error) ? tp : null;
    });
    renderSyncWaveform();
  }

  function renderSyncWaveform() {
    const host = $("syWaveform");
    const canvas = $("syWaveCanvas");
    if (!host || !canvas) return;
    const v = S.sync.video;
    const tracks = (S.sync.tracks || []).filter((t) => !t.error && t.enabled !== false && t.peaks);
    if (!v || !v.peaks || tracks.length === 0) { host.hidden = true; return; }
    host.hidden = false;
    const zoomToolbar = $("syWaveZoomToolbar");
    if (zoomToolbar) zoomToolbar.hidden = false;
    updateSyncZoomLabel();

    const rows = [{
      label: "Camera (reference)", peaks: v.peaks.peaks, duration: v.peaks.duration,
      offset: 0, color: "#8a97a6",
    }].concat(tracks.map((t, i) => ({
      label: t.filename || basename(t.path), peaks: t.peaks.peaks, duration: t.peaks.duration,
      offset: t.offset_seconds || 0, color: SYNC_WAVE_TRACK_COLORS[i % SYNC_WAVE_TRACK_COLORS.length],
    })));

    const viewDuration = Math.max(0.5, ...rows.map((r) => r.duration + Math.max(0, r.offset)));
    // host.clientWidth is the OUTER (non-scrolling) container's width -- a
    // stable "fit" reference that doesn't itself grow as the canvas widens
    // with zoom (unlike the scrolling wrapper's clientWidth would).
    const fitWidth = host.clientWidth || 600;
    const cssWidth = Math.round(fitWidth * S.sync.zoom);
    const cssHeight = rows.length * SYNC_WAVE_ROW_H + (rows.length - 1) * SYNC_WAVE_ROW_GAP;
    const dpr = window.devicePixelRatio || 1;
    canvas.style.width = `${cssWidth}px`;
    canvas.style.height = `${cssHeight}px`;
    canvas.width = Math.round(cssWidth * dpr);
    canvas.height = Math.round(cssHeight * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    const pxPerSecond = cssWidth / viewDuration;
    rows.forEach((row, i) => {
      const top = i * (SYNC_WAVE_ROW_H + SYNC_WAVE_ROW_GAP);
      const centerY = top + SYNC_WAVE_ROW_H / 2;
      const halfH = SYNC_WAVE_ROW_H / 2 - 6;

      ctx.fillStyle = "rgba(255,255,255,0.03)";
      ctx.fillRect(0, top, cssWidth, SYNC_WAVE_ROW_H);

      const startX = row.offset * pxPerSecond;
      const trackWidthPx = row.duration * pxPerSecond;
      const numBuckets = row.peaks.length;
      if (numBuckets > 0 && trackWidthPx > 0) {
        ctx.strokeStyle = row.color;
        ctx.lineWidth = Math.max(1, trackWidthPx / numBuckets);
        ctx.beginPath();
        for (let b = 0; b < numBuckets; b++) {
          const x = startX + (b / numBuckets) * trackWidthPx;
          if (x < -2 || x > cssWidth + 2) continue; // off-canvas — skip drawing it
          const mn = row.peaks[b][0], mx = row.peaks[b][1];
          ctx.moveTo(x, centerY - mx * halfH);
          ctx.lineTo(x, centerY - mn * halfH);
        }
        ctx.stroke();
      }

      ctx.fillStyle = "#c7ccd4";
      ctx.font = "11px -apple-system, system-ui, sans-serif";
      ctx.textBaseline = "top";
      ctx.fillText(i === 0 ? row.label : `${row.label}  (${formatOffset(row.offset)})`, 4, top + 3);
    });
  }

  // ---------------- multi-project registry (addendum v21) ----------------
  //
  // Multiple videos' sync work can be loaded at once. S.sync.{video, audios,
  // tracks, method, sidecar, confirmIdx} (plus the module-level syCommitted)
  // always describe the ACTIVE project; every other loaded video's fields
  // live in S.sync.projects (path -> snapshot), in S.sync.order's load
  // order. Objects are stored by REFERENCE, not cloned — while a project is
  // active, its map entry and its flat fields are literally the same
  // objects, so in-place mutation (e.g. probe results arriving) is visible
  // from both without extra bookkeeping. A snapshot is only "frozen" the
  // moment you switch away from it (captureActiveSyncProject).

  function captureActiveSyncProject() {
    if (!S.sync.video) return;
    S.sync.projects.set(S.sync.video.path, {
      video: S.sync.video, audios: S.sync.audios, tracks: S.sync.tracks,
      method: S.sync.method, sidecar: S.sync.sidecar, confirmIdx: S.sync.confirmIdx,
      committed: syCommitted,
    });
    if (!S.sync.order.includes(S.sync.video.path)) S.sync.order.push(S.sync.video.path);
  }

  function applySyncProjectSnapshot(snap) {
    S.sync.video = snap.video;
    S.sync.audios = snap.audios;
    S.sync.tracks = snap.tracks;
    S.sync.method = snap.method || "waveform";
    S.sync.sidecar = snap.sidecar;
    S.sync.confirmIdx = snap.confirmIdx;
    syCommitted = snap.committed;
    if ($("syMethod")) $("syMethod").value = S.sync.method;
  }

  function switchSyncProject(path) {
    if (S.sync.video && S.sync.video.path === path) return; // already active
    const snap = S.sync.projects.get(path);
    if (!snap) return;
    teardownSyncPlayer();
    captureActiveSyncProject();
    applySyncProjectSnapshot(snap);
    updateSyncUndoButtons();
    renderSyncRail();
    renderSyncResults();
    renderSyncWaveform(); // peaks travel with the restored objects -- no refetch needed
  }

  function renderSyncProjectTabs() {
    const host = $("syProjectTabs");
    if (!host) return;
    if (S.sync.order.length < 2) { host.hidden = true; host.innerHTML = ""; return; }
    host.hidden = false;
    host.innerHTML = S.sync.order.map((path) => {
      const active = !!(S.sync.video && S.sync.video.path === path);
      const snapTracks = active ? S.sync.tracks : (S.sync.projects.get(path) || {}).tracks;
      const n = (snapTracks || []).length;
      return `<button class="suite-sync-projtab${active ? " is-active" : ""}" data-path="${esc(path)}"
        title="${esc(path)}" aria-current="${active ? "true" : "false"}">${esc(basename(path))}${n ? ` <span>${n}</span>` : ""}</button>`;
    }).join("");
  }

  // ---------------- job completion / sidecar restore ----------------

  function adoptSyncResult(job, isTransition) {
    const r = job.result;
    const videoPath = r.video && r.video.path;
    if (!videoPath) return;
    const isActive = !!(S.sync.video && S.sync.video.path === videoPath);
    const tracks = (r.tracks || []).map((t) => normalizeTrackRouting(Object.assign({}, t)));
    const audios = tracks.map((t) => ({ path: t.path, probe: t.probe || null, probed: true }));
    const method = r.method || "waveform";
    const videoObj = { path: videoPath, probe: r.video.probe || null, probed: true };
    const committed = JSON.stringify(tracks.map((t) => ({
      path: t.path, offset_seconds: t.offset_seconds,
      enabled: t.enabled !== false,
      channels: Array.isArray(t.channels) ? t.channels : null,
    })));

    if (isActive) {
      teardownSyncPlayer(); // fresh detection → drop any open preview
      S.sync.video = videoObj;
      S.sync.method = method;
      if ($("syMethod")) $("syMethod").value = S.sync.method;
      S.sync.tracks = tracks;
      S.sync.audios = audios;
      S.sync.confirmIdx = null;
      S.sync.sidecar = null;
      SuiteUndo.reset(syncDomain());
      syCommitted = committed; // detection itself is the undo baseline
      captureActiveSyncProject(); // keep its registry entry current too
      renderSyncRail();
      renderSyncResults();
      loadSyncWaveform();
    } else {
      // Detection finished for a project the user has switched away from (or
      // never opened this session) — update its stored snapshot directly,
      // without touching whatever project is on screen right now.
      S.sync.projects.set(videoPath, {
        video: videoObj, audios, tracks, method, sidecar: null, confirmIdx: null, committed,
      });
      if (!S.sync.order.includes(videoPath)) S.sync.order.push(videoPath);
      SuiteUndo.reset("sync::" + videoPath);
      renderSyncProjectTabs();
    }
    if (isTransition) {
      const failed = tracks.filter((t) => t.error).length;
      const summary = `${tracks.length - failed} track(s) aligned` + (failed ? `, ${failed} failed.` : ".");
      toast(`Sync detected — ${summary}` + (isActive ? "" : ` (${basename(videoPath)})`),
        failed ? "info" : "ok");
      maybeNativeNotify(job.label, summary);
      saveOffsetsForPath(videoPath, tracks); // persist the detected values right away (addendum v3)
    }
  }

  async function loadSavedOffsets() {
    const v = S.sync.video;
    const sc = S.sync.sidecar;
    if (!v || !sc || !sc.found) return;
    const scTracks = (sc.tracks || []).filter((t) => t && t.path);
    if (scTracks.length === 0) { toast("The saved sidecar has no tracks.", "info"); return; }
    const btn = $("syLoadSaved");
    btn.disabled = true;
    const probeRes = await call("sync_probe", scTracks.map((t) => t.path));
    btn.disabled = false;
    const probes = (probeRes.ok && probeRes.probes) || {};
    teardownSyncPlayer();
    S.sync.audios = scTracks.map((t) => ({ path: t.path, probe: probes[t.path] || null, probed: true }));
    S.sync.tracks = scTracks.map((t) => normalizeTrackRouting({
      path: t.path,
      filename: basename(t.path),
      offset_seconds: t.offset_seconds,
      enabled: t.enabled,
      channels: t.channels,
      probe: probes[t.path] || null,
      error: null,
    }));
    if (sc.method) {
      S.sync.method = sc.method;
      $("syMethod").value = sc.method;
    }
    S.sync.confirmIdx = null;
    S.sync.sidecar = null; // strip consumed
    SuiteUndo.reset(syncDomain());
    syCommitted = syncStateStr();
    captureActiveSyncProject();
    renderSyncRail();
    renderSyncResults();
    loadSyncWaveform();
    toast(`Restored ${S.sync.tracks.length} saved offset(s) — no re-detection needed.`, "ok");
  }

  // ---------------- actions ----------------

  async function pickSyncVideo() {
    const res = await call("sync_pick_video");
    if (!res.ok) { toastIfError(res, "Couldn't open the file dialog."); return; }
    if (!res.path) return; // dialog cancelled

    if (S.sync.video && S.sync.video.path === res.path) return; // already the active project
    if (S.sync.projects.has(res.path)) {
      switchSyncProject(res.path);
      toast(`${basename(res.path)} is already loaded — switched to it.`, "info");
      return;
    }

    captureActiveSyncProject(); // stash whatever was active -- don't boot it out
    teardownSyncPlayer(); // the picture just changed
    S.sync.video = { path: res.path, probe: null, probed: false };
    S.sync.sidecar = null;
    S.sync.audios = []; // a fresh project starts with no external audio of its own
    S.sync.tracks = null;
    S.sync.confirmIdx = null;
    S.sync.method = "waveform";
    if ($("syMethod")) $("syMethod").value = "waveform";
    SuiteUndo.reset(syncDomain());
    syCommitted = null;
    captureActiveSyncProject(); // register the new project right away so its tab appears immediately
    renderSyncRail();
    renderSyncResults();
    renderSyncWaveform();

    const [probeRes, sidecarRes] = await Promise.all([
      call("sync_probe", [res.path]),
      call("sync_load_offsets", res.path),
    ]);
    // Look the project up by path rather than trusting S.sync.video, since the
    // user may have switched to a different loaded project while awaiting —
    // mutating through the registry entry (same object reference as the flat
    // fields were, while this was active) keeps both in sync either way.
    const proj = S.sync.projects.get(res.path);
    if (!proj) return; // removed while awaiting
    proj.video.probed = true;
    if (probeRes.ok && probeRes.probes && probeRes.probes[res.path]) {
      proj.video.probe = probeRes.probes[res.path];
    } else if (!probeRes.ok) {
      toast(probeRes.error || "Couldn't probe that video.", "error");
    }
    if (sidecarRes.ok && sidecarRes.found) proj.sidecar = sidecarRes;

    const stillActive = S.sync.video && S.sync.video.path === res.path;
    if (stillActive) {
      S.sync.sidecar = proj.sidecar;
      if (proj.sidecar) {
        await loadSavedOffsets(); // auto-load the timeline the moment saved offsets are found
      } else {
        renderSyncRail();
      }
    }
  }

  async function addSyncAudio() {
    const res = await call("sync_pick_audio");
    if (!res.ok) { toastIfError(res, "Couldn't open the file dialog."); return; }
    const fresh = (res.paths || []).filter((p) => !S.sync.audios.some((a) => a.path === p));
    if (fresh.length === 0) return;
    fresh.forEach((p) => S.sync.audios.push({ path: p, probe: null, probed: false }));
    renderSyncRail();
    const probeRes = await call("sync_probe", fresh); // ONE batched probe for all new files
    const probes = (probeRes.ok && probeRes.probes) || {};
    S.sync.audios.forEach((a) => {
      if (fresh.includes(a.path)) { a.probed = true; a.probe = probes[a.path] || null; }
    });
    if (!probeRes.ok) toast(probeRes.error || "Couldn't probe those audio files.", "error");
    renderSyncRail();
  }

  async function startSyncDetect() {
    const v = S.sync.video;
    if (!v || S.sync.audios.length === 0) return;
    const btn = $("syDetect");
    btn.disabled = true;
    const res = await call("sync_start", v.path, S.sync.audios.map((a) => a.path), S.sync.method);
    btn.disabled = false;
    renderSyncRail(); // restore the real disabled state
    if (!res.ok) { toast(res.error || "Couldn't start sync detection.", "error"); return; }
    toast(`Detecting sync for ${basename(v.path)} against ${S.sync.audios.length} audio file(s)…`, "ok");
    ensurePolling();
    openDrawer();
  }

  async function confirmSyncTranscribe(i) {
    const v = S.sync.video;
    const t = (S.sync.tracks || [])[i];
    if (!v || !t || t.error || t.enabled === false) return; // disabled tracks aren't offered
    const modelLabel = $("tModel") ? $("tModel").value : "";
    const diarize = $("tDiarize") ? $("tDiarize").checked : false;
    // A single selected source channel (channels == [k], k >= 1) is passed to
    // the backend; "all" (null) and "downmix to mono" ([0]) omit it (None →
    // the existing whole-file mono downmix). Addendum v4 §E.
    const channel = (Array.isArray(t.channels) && t.channels.length === 1 && t.channels[0] >= 1)
      ? t.channels[0] : null;
    // offset_seconds is the CURRENT (possibly nudged) value
    const res = await call("sync_send_to_transcriber", v.path, t.path, t.offset_seconds, modelLabel, diarize, channel);
    if (!res.ok) { toast(res.error || "Couldn't queue that transcription.", "error"); return; }
    S.sync.confirmIdx = null;
    renderSyncResults();
    toast(`Transcribing ${basename(t.path)} onto ${basename(v.path)}'s timeline (offset ${formatOffset(t.offset_seconds)}) — no proxy rendered.`, "ok", 5600);
    ensurePolling();
    openDrawer();
  }

  async function exportSyncXml() {
    const v = S.sync.video;
    const tracks = (S.sync.tracks || []).filter((t) => !t.error && t.offset_seconds != null);
    if (!v || tracks.length === 0) return;
    const payload = {
      video: { path: v.path, probe: v.probe || null },
      // Forward per-track routing; the builder skips enabled == false and
      // emits clipitems only for the selected channels (null = all). §D.
      tracks: tracks.map((t) => ({
        path: t.path,
        offset_seconds: t.offset_seconds,
        probe: t.probe || null,
        enabled: t.enabled !== false,
        channels: Array.isArray(t.channels) ? t.channels : null,
      })),
      include_camera_audio: $("syIncludeCam").checked,
      sequence_name: `${fileStem(v.path)} synced`,
    };
    const btn = $("syExportXml");
    btn.disabled = true;
    const res = await call("sync_export_xml", payload);
    btn.disabled = false;
    renderSyncResults(); // restore the real disabled state
    if (!res.ok) { toastIfError(res, "Couldn't export the Premiere XML."); return; }
    toast(`Premiere XML saved to ${res.path}`, "ok");
    (res.warnings || []).forEach((w) => toast(w, "info", 6400));
  }

  function wireSync() {
    $("syPickVideo").addEventListener("click", pickSyncVideo);

    $("syVideoRemove").addEventListener("click", () => {
      if (!S.sync.video) return;
      const removedPath = S.sync.video.path;
      teardownSyncPlayer();
      S.sync.projects.delete(removedPath);
      S.sync.order = S.sync.order.filter((p) => p !== removedPath);
      SuiteUndo.reset("sync::" + removedPath);
      const nextPath = S.sync.order[S.sync.order.length - 1];
      if (nextPath) {
        applySyncProjectSnapshot(S.sync.projects.get(nextPath));
      } else {
        S.sync.video = null;
        S.sync.sidecar = null;
        S.sync.tracks = null;
        S.sync.confirmIdx = null;
        syCommitted = null;
        // S.sync.audios intentionally left alone -- lets you pick a
        // different video and redetect against the same mic files.
      }
      renderSyncRail();
      renderSyncResults();
      renderSyncWaveform();
    });

    $("syProjectTabs").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-path]");
      if (!btn) return;
      switchSyncProject(btn.dataset.path);
    });

    $("syAddAudio").addEventListener("click", addSyncAudio);

    $("syAudioList").addEventListener("click", (e) => {
      const btn = e.target.closest('button[data-action="sy-remove"]');
      if (!btn) return;
      S.sync.audios.splice(Number(btn.dataset.idx), 1);
      renderSyncRail();
    });

    $("syMethod").addEventListener("change", (e) => { S.sync.method = e.target.value; });
    $("syDetect").addEventListener("click", startSyncDetect);
    $("syLoadSaved").addEventListener("click", loadSavedOffsets);
    $("syUndoBtn").addEventListener("click", syncUndo);
    $("syRedoBtn").addEventListener("click", syncRedo);
    $("syExportXml").addEventListener("click", exportSyncXml);
    $("syWaveZoomIn").addEventListener("click", zoomSyncIn);
    $("syWaveZoomOut").addEventListener("click", zoomSyncOut);
    $("syWaveZoomReset").addEventListener("click", resetSyncZoom);

    // synced preview player transport (addendum v4 §B)
    $("syPreviewToggle").addEventListener("click", toggleSyncPlayer);
    $("syPlayerPlay").addEventListener("click", toggleSyncPlay);
    $("syPlayerClose").addEventListener("click", teardownSyncPlayer);
    $("syPlayerScrub").addEventListener("input", (e) => syncPlayerSeek(parseFloat(e.target.value) || 0));
    // the picture drives the lock on every timeupdate (covers paused seeks too)
    $("syPlayerVideo").addEventListener("timeupdate", () => {
      if (syPlayer) { syncPlayerLock(); syncPlayerUiTick(); }
    });
    $("syPlayerVideo").addEventListener("ended", () => { if (syPlayer) pauseSyncPlayer(); });

    // delegated per-track actions in the results panel
    $("syResults").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      const i = Number(btn.dataset.i);
      const action = btn.dataset.action;
      if (action === "sy-nudge") {
        const t = (S.sync.tracks || [])[i];
        if (!t || t.error) return;
        syncSetOffsetMs(i, Math.round((t.offset_seconds || 0) * 1000) + Number(btn.dataset.ms));
      } else if (action === "sy-transcribe") {
        S.sync.confirmIdx = i;
        renderSyncResults();
      } else if (action === "sy-cancel") {
        S.sync.confirmIdx = null;
        renderSyncResults();
      } else if (action === "sy-confirm") {
        confirmSyncTranscribe(i);
      } else if (action === "sy-mute") {
        syncTogglePreviewFlag(i, "previewMuted");
      } else if (action === "sy-solo") {
        syncTogglePreviewFlag(i, "previewSolo");
      }
    });

    // direct ms input + routing controls: commit on change
    $("syResults").addEventListener("change", (e) => {
      const input = e.target.closest(".suite-sync-msinput");
      if (input) { syncSetOffsetMs(Number(input.dataset.i), parseFloat(input.value)); return; }
      const enable = e.target.closest('input[data-action="sy-enable"]');
      if (enable) { syncSetRouting(Number(enable.dataset.i), { enabled: enable.checked }); return; }
      const chan = e.target.closest('select[data-action="sy-channel"]');
      if (chan) { syncSetRouting(Number(chan.dataset.i), { channels: channelsFromSelectValue(chan.value) }); return; }
    });
    $("syResults").addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const input = e.target.closest(".suite-sync-msinput");
      if (!input) return;
      e.preventDefault();
      syncSetOffsetMs(Number(input.dataset.i), parseFloat(input.value));
    });

    renderSyncRail();
  }

  // ---------------- Harmonize workspace (Harmonizer integration) ----------------
  //
  // v1 scope: reference + N takes -> background alignment job (harmonize_start,
  // in Harmonizer's own venv) -> READ-ONLY waveform + summary -> Send to
  // Resolve / Export FCPXML. No anchor editing (drag/nudge/merge/split) yet
  // -- see Harmonizer_App_Plan.md. Unlike Sync, the report already carries
  // its own waveform data (align.py's report.waveforms) -- no separate
  // peaks-fetch call is needed.

  const HZ_WAVE_ROW_H = 56;
  const HZ_WAVE_ROW_GAP = 6;
  const HZ_WAVE_TRACK_COLORS = ["#00b2ba", "#f15d22", "#5f8fb4", "#dd971a", "#74a333", "#770055"];
  const HZ_ZOOM_STEP = 1.6;
  const HZ_ZOOM_MAX = 20;

  function harmonizeHasBraw() {
    return S.harmonize.takes.some((t) => /\.braw$/i.test(t.path));
  }

  function updateHarmonizeExportButtons() {
    const hasReport = !!S.harmonize.report;
    const braw = harmonizeHasBraw();
    const disabled = !hasReport || braw;
    const title = braw ? "BRAW export isn't supported yet — see the take list." : "";
    const sendBtn = $("hzSendResolve");
    const xmlBtn = $("hzExportXml");
    sendBtn.disabled = disabled;
    xmlBtn.disabled = disabled;
    sendBtn.title = title;
    xmlBtn.title = title;
  }

  function renderHarmonizeRail() {
    const ref = S.harmonize.ref;
    const refRow = $("hzRefRow");
    if (ref) {
      refRow.hidden = false;
      const nameEl = $("hzRefName");
      nameEl.textContent = basename(ref.path);
      nameEl.title = ref.path;
    } else {
      refRow.hidden = true;
    }
    const list = $("hzTakeList");
    if (S.harmonize.takes.length === 0) {
      list.innerHTML = `<li style="border-style:dashed;color:var(--text-faint);justify-content:center;">No takes added</li>`;
    } else {
      list.innerHTML = S.harmonize.takes.map((t, i) =>
        `<li>
          <label class="suite-hz-take" title="Same source as reference — skip retiming">
            <input type="checkbox" data-action="hz-no-retime" data-idx="${i}" ${t.noRetime ? "checked" : ""} aria-label="${esc(basename(t.path))}: same source as reference, skip retiming" />
            <span title="${esc(t.path)}">${esc(basename(t.path))}</span>
          </label>
          <button data-action="hz-remove-take" data-idx="${i}" title="Remove" aria-label="Remove ${esc(basename(t.path))}">✕</button>
        </li>`
      ).join("");
    }
    $("hzAnalyze").disabled = !ref || S.harmonize.takes.length === 0;
    updateHarmonizeExportButtons();
  }

  function renderHarmonizeSummary() {
    const host = $("hzSummary");
    const report = S.harmonize.report;
    if (!report) {
      host.innerHTML = `<p class="suite-empty">No alignment yet — choose a reference and takes on the left, then click Analyze.</p>`;
      return;
    }
    const takeNames = report.takes || [];
    if (takeNames.length === 0) {
      host.innerHTML = `<p class="suite-empty">No takes in this report.</p>`;
      return;
    }
    const noRetimeTakes = new Set(report.no_retime_takes || []);
    host.innerHTML = takeNames.map((name) => {
      const offset = (report.coarse_offsets_sec || {})[name];
      const conf = (report.coarse_offset_confidence || {})[name];
      const segs = (report.segments || {})[name] || [];
      const flagged = segs.filter((s) => s.flagged).length;
      const leadin = (report.excluded_leadin_ref_sec || {})[name] || 0;
      const skipped = (report.skipped_anchors || {})[name] || 0;
      const noRetime = noRetimeTakes.has(name);
      return `<div class="suite-sync-track">
        <div class="suite-sync-track__head">
          <span class="suite-sync-track__name" title="${esc(name)}">${esc(name)}${noRetime ? " · same source" : ""}</span>
          <span class="suite-sync-offset">${offset != null ? offset.toFixed(2) + "s" : "—"}</span>
        </div>
        <div class="suite-hint suite-hint--tight">${noRetime
          ? "not retimed — placed at the coarse offset only (same source as reference)"
          : `confidence ${conf != null ? conf.toFixed(1) : "—"}
          · excluded lead-in ${leadin.toFixed(2)}s
          · ${flagged}/${segs.length} segment(s) flagged
          · ${skipped} anchor(s) skipped`}</div>
      </div>`;
    }).join("");
  }

  function updateHarmonizeZoomLabel() {
    const label = $("hzWaveZoomLabel");
    if (label) label.textContent = S.harmonize.zoom <= 1.01 ? "Fit" : `${S.harmonize.zoom.toFixed(1)}×`;
  }

  function setHarmonizeZoom(factor) {
    S.harmonize.zoom = Math.max(1, Math.min(HZ_ZOOM_MAX, factor));
    updateHarmonizeZoomLabel();
    renderHarmonizeWaveform();
  }
  function zoomHarmonizeIn() { setHarmonizeZoom(S.harmonize.zoom * HZ_ZOOM_STEP); }
  function zoomHarmonizeOut() { setHarmonizeZoom(S.harmonize.zoom / HZ_ZOOM_STEP); }
  function resetHarmonizeZoom() { setHarmonizeZoom(1); }

  let hzWaveRedrawTimer = null;
  function scheduleHarmonizeWaveformRedraw() {
    clearTimeout(hzWaveRedrawTimer);
    hzWaveRedrawTimer = setTimeout(renderHarmonizeWaveform, 120);
  }

  function hzConfidenceColor(conf) {
    if (conf == null) return "#5a5f6b";
    if (conf >= 0.8) return "#5fb86a";
    if (conf >= 0.5) return "#d9b23c";
    return "#d9503c";
  }

  function renderHarmonizeWaveform() {
    const host = $("hzWaveform");
    const canvas = $("hzWaveCanvas");
    if (!host || !canvas) return;
    const report = S.harmonize.report;
    if (!report || !report.waveforms) { host.hidden = true; return; }
    host.hidden = false;
    const zoomToolbar = $("hzWaveZoomToolbar");
    if (zoomToolbar) zoomToolbar.hidden = false;
    updateHarmonizeZoomLabel();

    const takeNames = report.takes || [];
    // A take's own-clock time T corresponds to reference-clock time
    // T - coarse_offset (align.py build_segments: offset >= 0 means the
    // take already has `offset` extra seconds of pre-roll before ref time
    // 0). Each take row is shifted LEFT by its own coarse offset so its
    // waveform lines up under the reference's shared time axis -- the
    // opposite sign from Sync's `video_time = audio_time + offset`, since
    // Sync anchors to a video track and Harmonize anchors everything to
    // the reference track.
    const rows = [{
      name: null, label: "Reference", peaks: (report.waveforms.reference || []),
      duration: report.ref_duration || 0, offset: 0, color: "#8a97a6",
    }].concat(takeNames.map((name, i) => ({
      name, label: name, peaks: (report.waveforms[name] || []),
      duration: (report.take_durations || {})[name] || 0,
      offset: -((report.coarse_offsets_sec || {})[name] || 0),
      color: HZ_WAVE_TRACK_COLORS[i % HZ_WAVE_TRACK_COLORS.length],
    })));

    const viewDuration = Math.max(0.5, ...rows.map((r) => r.duration + Math.max(0, r.offset)));
    const fitWidth = host.clientWidth || 600;
    const cssWidth = Math.round(fitWidth * S.harmonize.zoom);
    const cssHeight = rows.length * HZ_WAVE_ROW_H + (rows.length - 1) * HZ_WAVE_ROW_GAP;
    const dpr = window.devicePixelRatio || 1;
    canvas.style.width = `${cssWidth}px`;
    canvas.style.height = `${cssHeight}px`;
    canvas.width = Math.round(cssWidth * dpr);
    canvas.height = Math.round(cssHeight * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    const pxPerSecond = cssWidth / viewDuration;
    rows.forEach((row, i) => {
      const top = i * (HZ_WAVE_ROW_H + HZ_WAVE_ROW_GAP);
      const centerY = top + HZ_WAVE_ROW_H / 2;
      const halfH = HZ_WAVE_ROW_H / 2 - 6;

      ctx.fillStyle = "rgba(255,255,255,0.03)";
      ctx.fillRect(0, top, cssWidth, HZ_WAVE_ROW_H);

      const startX = row.offset * pxPerSecond;
      const trackWidthPx = row.duration * pxPerSecond;
      const numBuckets = row.peaks.length;
      if (numBuckets > 0 && trackWidthPx > 0) {
        // align.py's waveform_peaks() returns a flat max-abs-amplitude list
        // (one float per bucket), not Sync's [min,max] pairs -- draw a
        // symmetric stroke around centerY instead of an asymmetric one.
        ctx.strokeStyle = row.color;
        ctx.lineWidth = Math.max(1, trackWidthPx / numBuckets);
        ctx.beginPath();
        for (let b = 0; b < numBuckets; b++) {
          const x = startX + (b / numBuckets) * trackWidthPx;
          if (x < -2 || x > cssWidth + 2) continue;
          const amp = row.peaks[b];
          ctx.moveTo(x, centerY - amp * halfH);
          ctx.lineTo(x, centerY + amp * halfH);
        }
        ctx.stroke();
      }

      // Anchor markers, confidence-colored -- the one thing Sync's own
      // waveform draws nothing like (it has no anchor concept). A row's
      // anchor tick lands at the SAME x a waveform sample at that time
      // would use: (time + row.offset) * pxPerSecond for a take row (time
      // is in the take's own clock, matching how its peaks are positioned
      // above), or ref_time * pxPerSecond for the reference row.
      if (row.name === null) {
        (report.anchors || []).forEach((a) => {
          const x = a.ref_time * pxPerSecond;
          if (x < -2 || x > cssWidth + 2) return;
          ctx.fillStyle = "#8a97a6";
          ctx.fillRect(x - 0.5, top + 4, 1, HZ_WAVE_ROW_H - 8);
        });
      } else {
        (report.anchors || []).forEach((a) => {
          const t = (a.take_times || {})[row.name];
          if (t == null) return;
          const x = (t + row.offset) * pxPerSecond;
          if (x < -2 || x > cssWidth + 2) return;
          ctx.fillStyle = hzConfidenceColor((a.confidence || {})[row.name]);
          ctx.beginPath();
          ctx.arc(x, top + 8, 3, 0, Math.PI * 2);
          ctx.fill();
        });
      }

      ctx.fillStyle = "#c7ccd4";
      ctx.font = "11px -apple-system, system-ui, sans-serif";
      ctx.textBaseline = "top";
      ctx.fillText(row.label, 4, top + HZ_WAVE_ROW_H - 14);
    });
  }

  // ---------------- job completion / sidecar restore ----------------

  function harmonizeNoRetimeNames() {
    return S.harmonize.takes.filter((t) => t.noRetime).map((t) => basename(t.path));
  }

  function adoptHarmonizeResult(job, isTransition) {
    const report = job.result && job.result.report;
    if (!report) return;
    S.harmonize.report = report;
    const note = $("hzExportNote");
    if (note) note.hidden = true;
    renderHarmonizeSummary();
    renderHarmonizeWaveform();
    updateHarmonizeExportButtons();
    if (isTransition) {
      const flagged = (report.takes || []).reduce(
        (n, name) => n + ((report.segments || {})[name] || []).filter((s) => s.flagged).length, 0);
      const summary = `${(report.takes || []).length} take(s) analyzed` +
        (flagged ? `, ${flagged} segment(s) flagged for review.` : ".");
      toast(`Alignment complete — ${summary}`, "ok");
      maybeNativeNotify(job.label, summary);
      const ref = S.harmonize.ref;
      if (ref) {
        call("harmonize_save_report", ref.path, S.harmonize.takes.map((t) => t.path), report,
          harmonizeNoRetimeNames());
      }
    }
  }

  // ---------------- actions ----------------

  async function pickHarmonizeRef() {
    const res = await call("harmonize_pick_reference");
    if (!res.ok) { toastIfError(res, "Couldn't open the file dialog."); return; }
    if (!res.path) return;
    S.harmonize.ref = { path: res.path };
    S.harmonize.report = null;
    renderHarmonizeRail();
    renderHarmonizeSummary();
    renderHarmonizeWaveform();

    const loadRes = await call("harmonize_load_report", res.path);
    if (!(S.harmonize.ref && S.harmonize.ref.path === res.path)) return; // superseded while awaiting
    if (loadRes.ok && loadRes.found) {
      S.harmonize.report = loadRes.report;
      if (Array.isArray(loadRes.take_paths) && loadRes.take_paths.length && S.harmonize.takes.length === 0) {
        const noRetimeSet = new Set(loadRes.no_retime || []);
        S.harmonize.takes = loadRes.take_paths.map((p) => ({ path: p, noRetime: noRetimeSet.has(basename(p)) }));
      }
      renderHarmonizeRail();
      renderHarmonizeSummary();
      renderHarmonizeWaveform();
      toast(`Restored a saved alignment for ${basename(res.path)}.`, "info");
    }
  }

  async function addHarmonizeTakes() {
    const res = await call("harmonize_pick_takes");
    if (!res.ok) { toastIfError(res, "Couldn't open the file dialog."); return; }
    const fresh = (res.paths || []).filter((p) => !S.harmonize.takes.some((t) => t.path === p));
    if (fresh.length === 0) return;
    fresh.forEach((p) => S.harmonize.takes.push({ path: p, noRetime: false }));
    renderHarmonizeRail();
  }

  async function startHarmonizeAnalyze() {
    const ref = S.harmonize.ref;
    if (!ref || S.harmonize.takes.length === 0) return;
    const btn = $("hzAnalyze");
    btn.disabled = true;
    const res = await call("harmonize_start", ref.path, S.harmonize.takes.map((t) => t.path),
      harmonizeNoRetimeNames());
    renderHarmonizeRail(); // restores the real disabled state
    if (!res.ok) { toast(res.error || "Couldn't start alignment.", "error"); return; }
    toast(`Analyzing ${basename(ref.path)} against ${S.harmonize.takes.length} take(s)…`, "ok");
    ensurePolling();
    openDrawer();
  }

  function harmonizeTimelineName() {
    const input = $("hzTimelineName");
    const v = input ? input.value.trim() : "";
    return v || null;
  }

  async function sendHarmonizeToResolve() {
    const ref = S.harmonize.ref;
    const report = S.harmonize.report;
    if (!ref || !report) return;
    const btn = $("hzSendResolve");
    btn.disabled = true;
    const res = await call("harmonize_send_to_resolve", ref.path, S.harmonize.takes.map((t) => t.path), report,
      null, harmonizeTimelineName());
    updateHarmonizeExportButtons();
    if (!res.ok) { toast(res.error || "Couldn't send to Resolve.", "error"); return; }
    toast(`Sent to Resolve — "${res.timeline}" in project "${res.project}".`, "ok", 6000);
  }

  async function exportHarmonizeXml() {
    const ref = S.harmonize.ref;
    const report = S.harmonize.report;
    if (!ref || !report) return;
    const res = await call("harmonize_export_xml", ref.path, S.harmonize.takes.map((t) => t.path), report,
      harmonizeTimelineName());
    if (!res.ok) { toastIfError(res, "Couldn't export FCPXML."); return; }
    toast(`Exported FCPXML to ${res.path}`, "ok");
    const note = $("hzExportNote");
    if (note && res.reference_note) {
      note.hidden = false;
      note.textContent = res.reference_note;
    }
  }

  function wireHarmonize() {
    $("hzPickRef").addEventListener("click", pickHarmonizeRef);

    $("hzRefRemove").addEventListener("click", () => {
      S.harmonize.ref = null;
      S.harmonize.report = null;
      const note = $("hzExportNote");
      if (note) note.hidden = true;
      renderHarmonizeRail();
      renderHarmonizeSummary();
      renderHarmonizeWaveform();
    });

    $("hzAddTakes").addEventListener("click", addHarmonizeTakes);

    $("hzTakeList").addEventListener("click", (e) => {
      const btn = e.target.closest('button[data-action="hz-remove-take"]');
      if (!btn) return;
      S.harmonize.takes.splice(Number(btn.dataset.idx), 1);
      renderHarmonizeRail();
    });

    $("hzTakeList").addEventListener("change", (e) => {
      const cb = e.target.closest('input[data-action="hz-no-retime"]');
      if (!cb) return;
      const t = S.harmonize.takes[Number(cb.dataset.idx)];
      if (t) t.noRetime = cb.checked;
    });

    $("hzAnalyze").addEventListener("click", startHarmonizeAnalyze);
    $("hzSendResolve").addEventListener("click", sendHarmonizeToResolve);
    $("hzExportXml").addEventListener("click", exportHarmonizeXml);
    $("hzWaveZoomIn").addEventListener("click", zoomHarmonizeIn);
    $("hzWaveZoomOut").addEventListener("click", zoomHarmonizeOut);
    $("hzWaveZoomReset").addEventListener("click", resetHarmonizeZoom);

    renderHarmonizeRail();
    renderHarmonizeSummary();
  }

  // ---------------- Graphics workspace (Blair Brander) ----------------

  let gxPreviewSeq = 0;      // request counter — a stale response never overwrites a newer frame
  let gxStillTimer = null;   // 200ms debounce for still previews
  let gxScrubTimer = null;   // ~100ms throttle for scrub previews
  let gxScrubPending = null;

  function gxScene() { return S.gx.scene; }

  function schedulePreview() {
    if (!gxScene()) return;
    clearTimeout(gxStillTimer);
    gxStillTimer = setTimeout(requestStillPreview, 200);
  }

  async function requestStillPreview() {
    const scene = gxScene();
    if (!scene) return;
    const seq = ++gxPreviewSeq;
    const res = await call("brander_still_preview", scene, 960);
    if (seq !== gxPreviewSeq) return; // superseded
    if (res.ok && res.data_uri) $("gxPreviewImg").src = res.data_uri;
  }

  // #gxScrub's own value is (and always was, per its timecode label —
  // `${(t*total).toFixed(2)}s` where total = duration+hold_seconds — a
  // FRACTION OF THE WHOLE CLIP LIFETIME, hold tail included, not just
  // duration. The backend's `t` means something narrower (see
  // renderer.render_frame's docstring: fraction of duration ALONE,
  // clamped to 1.0 for the entire hold tail) — converting scrubber
  // fraction -> (t, elapsed_seconds) here, once, is what lets the
  // scrubber/Play-button preview actually match what export produces
  // during the hold tail (previously it silently didn't: the raw
  // fraction was forwarded as `t` unconverted, so the preview kept
  // easing toward "settled" through what export renders as an already-
  // frozen plateau).
  function gxScrubFracToTimes(scene, frac) {
    const duration = Math.max(0.1, (scene && scene.duration) || 4);
    const hold = Math.max(0, (scene && scene.hold_seconds) || 0);
    const elapsed = Math.max(0, Math.min(1, frac)) * (duration + hold);
    return { t: Math.max(0, Math.min(1, elapsed / duration)), elapsed };
  }

  function requestScrubPreview(frac) {
    gxScrubPending = frac;
    if (gxScrubTimer) return;
    gxScrubTimer = setTimeout(async () => {
      gxScrubTimer = null;
      const scene = gxScene();
      if (!scene || gxScrubPending == null) return;
      const frac2 = gxScrubPending;
      gxScrubPending = null;
      const { t, elapsed } = gxScrubFracToTimes(scene, frac2);
      const seq = ++gxPreviewSeq;
      const res = await call("brander_preview", scene, t, 960, elapsed);
      if (seq !== gxPreviewSeq) return;
      if (res.ok && res.data_uri) $("gxPreviewImg").src = res.data_uri;
    }, 100);
  }

  function fillSelect(sel, values, selected) {
    sel.innerHTML = "";
    (values || []).forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      sel.appendChild(opt);
    });
    if (selected != null && (values || []).includes(selected)) sel.value = selected;
  }

  function buildSwatches(host, colors, sceneKey) {
    host.innerHTML = "";
    Object.entries(colors).forEach(([name, hex]) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "suite-swatch";
      b.style.background = hex;
      b.title = `${name} (${hex})`;
      b.setAttribute("aria-label", `${name} for ${sceneKey.replace(/_/g, " ")}`);
      b.dataset.hex = hex;
      b.dataset.sceneKey = sceneKey;
      host.appendChild(b);
    });
  }

  function refreshSwatchSelection() {
    const scene = gxScene();
    if (!scene) return;
    document.querySelectorAll("#workspace-graphics .suite-swatch").forEach((b) => {
      const cur = (scene[b.dataset.sceneKey] || "").toLowerCase();
      b.classList.toggle("is-selected", cur === b.dataset.hex.toLowerCase());
    });
  }

  function gxToggleConditionalRows() {
    const scene = gxScene();
    if (!scene) return;
    $("gxLowerThirdOpts").hidden = scene.layout !== "Lower Third";
    $("gxGradientRow").hidden = !(scene.background_style === "Gradient" && !scene.transparent_bg);
    $("gxLogoCustomRow").hidden = scene.logo_color_mode !== "custom";
    $("gxShadowControls").hidden = !scene.shadow_enabled;
    // addendum v19: an AI-generated background (scene.ai_background_path)
    // overrides background_style entirely (see renderer.render_background) —
    // surface that plainly rather than leaving the style dropdown silently
    // ignored.
    $("gxAiBgRow").hidden = !scene.ai_background_path;
    // "Remove custom logo" only makes sense for an imported logo — built-
    // ins (and "None") aren't removable (brander_remove_custom_logo
    // refuses them server-side too, this is just the UI mirroring that).
    $("gxRemoveLogo").hidden = !String(scene.logo || "").startsWith("Custom: ");
  }

  // Single render path used by boot / preset apply / AI interpret / Load Project.
  function renderFormFromScene(scene) {
    if (!scene) return;
    $("gxTitle").value = scene.title || "";
    $("gxSubtitle").value = scene.subtitle || "";
    const o = S.gx.options || {};
    if (o.layouts) $("gxLayout").value = scene.layout || o.layouts[0];
    if (o.canvas_presets && scene.canvas_preset_name && o.canvas_presets[scene.canvas_preset_name]) {
      $("gxCanvas").value = scene.canvas_preset_name;
    }
    if (o.background_styles) $("gxBgStyle").value = scene.background_style || "Solid";
    if (o.animations) $("gxAnimation").value = scene.animation || o.animations[0];
    if (o.outro_animations) $("gxOutro").value = scene.outro_animation || "none";
    if (o.lower_third_positions) $("gxLtPosition").value = scene.lower_third_position || o.lower_third_positions[0];
    if (o.vignette_shapes) $("gxVignetteShape").value = scene.vignette_shape || o.vignette_shapes[0];
    if (o.fonts) {
      $("gxTitleFont").value = scene.title_font || o.fonts[0];
      $("gxSubtitleFont").value = scene.subtitle_font || o.fonts[0];
    }
    // Falls back to "None" (not just left on whatever the select last
    // showed) for a missing/falsy scene.logo OR one that's no longer in
    // the options list (e.g. a deleted custom logo) — mirrors the
    // standalone app's own `s.get("logo") or "None"` (app.py).
    if (o.logos) $("gxLogo").value = (scene.logo && o.logos.includes(scene.logo)) ? scene.logo : "None";
    if (o.logo_placements) $("gxLogoPlacement").value = scene.logo_placement || "bottom-center";
    $("gxLogoColorMode").value = scene.logo_color_mode || "original";
    $("gxLogoCustomColor").value = /^#[0-9a-fA-F]{6}$/.test(scene.logo_custom_color || "") ? scene.logo_custom_color : "#ffffff";
    $("gxTransparent").checked = !!scene.transparent_bg;
    $("gxDivider").checked = !!scene.divider;
    $("gxLogoGrow").checked = !!scene.logo_grow;
    $("gxShadowEnabled").checked = !!scene.shadow_enabled;
    $("gxShadowColor").value = /^#[0-9a-fA-F]{6}$/.test(scene.shadow_color || "") ? scene.shadow_color : "#000000";

    const setSlider = (id, valId, value, suffix) => {
      $(id).value = value;
      $(valId).textContent = `${Math.round(value)}${suffix}`;
    };
    setSlider("gxTitleSize", "gxTitleSizeVal", scene.title_size || 130, "px");
    setSlider("gxSubtitleSize", "gxSubtitleSizeVal", scene.subtitle_size || 46, "px");
    setSlider("gxLogoHeight", "gxLogoHeightVal", scene.logo_height || 160, "px");
    setSlider("gxLogoOpacity", "gxLogoOpacityVal", scene.logo_opacity != null ? scene.logo_opacity : 100, "%");
    setSlider("gxVignette", "gxVignetteVal", scene.vignette || 0, "%");
    setSlider("gxShadowOpacity", "gxShadowOpacityVal", scene.shadow_opacity != null ? scene.shadow_opacity : 60, "%");
    setSlider("gxShadowBlur", "gxShadowBlurVal", scene.shadow_blur != null ? scene.shadow_blur : 8, "px");
    setSlider("gxShadowOffsetX", "gxShadowOffsetXVal", scene.shadow_offset_x != null ? scene.shadow_offset_x : 4, "px");
    setSlider("gxShadowOffsetY", "gxShadowOffsetYVal", scene.shadow_offset_y != null ? scene.shadow_offset_y : 4, "px");
    setSlider("gxLtScale", "gxLtScaleVal", Math.round((scene.lower_third_scale || 1.0) * 100), "%");
    setSlider("gxTextOffsetX", "gxTextOffsetXVal", scene.text_offset_x || 0, "px");
    setSlider("gxTextOffsetY", "gxTextOffsetYVal", scene.text_offset_y || 0, "px");

    $("gxDuration").value = scene.duration != null ? scene.duration : 4;
    $("gxHold").value = scene.hold_seconds != null ? scene.hold_seconds : 1;

    refreshSwatchSelection();
    gxToggleConditionalRows();
    renderGxTimeline();
  }

  // Renderer-side defaults for the twelve animation-timing floats (mirrors
  // Blair Brander's renderer.py fallbacks) — filled into the scene once so
  // the timeline bars always have concrete values to drag.
  const GX_TIMING_DEFAULTS = {
    title_in_start: 0.0, title_in_end: 0.45,
    subtitle_in_start: 0.30, subtitle_in_end: 0.70,
    logo_in_start: 0.55, logo_in_end: 0.95,
    title_out_start: 0.80, title_out_end: 1.0,
    subtitle_out_start: 0.78, subtitle_out_end: 0.98,
    logo_out_start: 0.82, logo_out_end: 1.0,
  };

  function gxEnsureTimingDefaults(scene) {
    if (!scene) return;
    Object.entries(GX_TIMING_DEFAULTS).forEach(([k, v]) => {
      if (typeof scene[k] !== "number") scene[k] = v;
    });
  }

  function populateGraphicsOptions(defaults) {
    S.gx.scene = defaults.scene;
    S.gx.options = defaults.options || {};
    gxEnsureTimingDefaults(S.gx.scene);
    const o = S.gx.options;
    fillSelect($("gxPreset"), o.presets, undefined);
    fillSelect($("gxLayout"), o.layouts);
    fillSelect($("gxCanvas"), Object.keys(o.canvas_presets || {}));
    fillSelect($("gxBgStyle"), o.background_styles);
    fillSelect($("gxAnimation"), o.animations);
    fillSelect($("gxOutro"), o.outro_animations);
    fillSelect($("gxLtPosition"), o.lower_third_positions);
    fillSelect($("gxVignetteShape"), o.vignette_shapes);
    fillSelect($("gxTitleFont"), o.fonts);
    fillSelect($("gxSubtitleFont"), o.fonts);
    fillSelect($("gxLogo"), o.logos);
    fillSelect($("gxLogoPlacement"), o.logo_placements);

    const palette = Object.assign({}, o.primary_colors || {}, o.secondary_colors || {}, { White: "#ffffff", Black: "#000000" });
    buildSwatches($("gxSwBg"), palette, "bg_color");
    buildSwatches($("gxSwAccent"), palette, "accent_color");
    buildSwatches($("gxSwText"), palette, "text_color");
    buildSwatches($("gxSwGradient"), palette, "bg_gradient_color");

    renderFormFromScene(S.gx.scene);
    gxCommitted = JSON.stringify(S.gx.scene); // undo baseline — boot state isn't itself undoable
    updateGxUndoButtons();
  }

  // ---------------- Graphics undo (addendum E, domain "graphics") ----------------
  //
  // Same "last committed" pattern as the transcript editor: every committed
  // scene change (field commit, swatch, preset, AI apply, timeline drag end;
  // sliders on release, not per input tick) pushes the previous committed
  // snapshot. Live slider/drag movement only mutates the scene + preview.

  let gxCommitted = null; // JSON of the last committed scene

  function gxCommit() {
    const scene = gxScene();
    if (!scene) return;
    const now = JSON.stringify(scene);
    if (gxCommitted != null && now !== gxCommitted) {
      SuiteUndo.push("graphics", gxCommitted);
    }
    gxCommitted = now;
    updateGxUndoButtons();
  }

  function updateGxUndoButtons() {
    $("gxUndoBtn").disabled = !SuiteUndo.canUndo("graphics");
    $("gxRedoBtn").disabled = !SuiteUndo.canRedo("graphics");
  }

  function gxApplySnapshot(str) {
    S.gx.scene = JSON.parse(str);
    gxCommitted = str;
    renderFormFromScene(S.gx.scene);
    schedulePreview();
    updateGxUndoButtons();
  }

  function gxUndo() {
    const scene = gxScene();
    if (!scene) return;
    const prev = SuiteUndo.undo("graphics", JSON.stringify(scene));
    if (prev == null) return;
    gxApplySnapshot(prev);
  }

  function gxRedo() {
    const scene = gxScene();
    if (!scene) return;
    const next = SuiteUndo.redo("graphics", JSON.stringify(scene));
    if (next == null) return;
    gxApplySnapshot(next);
  }

  // ---------------- Graphics animation timeline (addendum C5) ----------------
  //
  // Rows Title / Subtitle / Logo; per row an IN bar ({el}_in_start.._in_end,
  // amber) and — unless outro_animation === "none" — an OUT bar (teal).
  // Bars drag as a whole, each end drags individually. Clamps: 0..1, bar
  // min-width 0.02, IN must end ≥0.02 before OUT starts (the standalone
  // timeline.py CROSS_GAP behavior). Positions snap to 1/(fps*duration).
  // Pointer events with setPointerCapture; no external libs.

  const GX_TL_ELEMENTS = [
    { el: "title", label: "Title" },
    { el: "subtitle", label: "Subtitle" },
    { el: "logo", label: "Logo" },
  ];
  const GX_TL_MINW = 0.02;
  const GX_TL_GAP = 0.02;
  let gxTlDrag = null; // active drag: {el, kind, mode, startX, trackW, s0, e0}

  function gxTlSnapStep() {
    const scene = gxScene();
    const fps = (S.gx.options && S.gx.options.fps) || 30;
    const duration = (scene && scene.duration) || 4;
    return 1 / Math.max(1, fps * duration);
  }

  function gxTlSnap(v) {
    const step = gxTlSnapStep();
    return Math.round(v / step) * step;
  }

  function gxTlVals(el, kind) {
    const scene = gxScene();
    return [scene[`${el}_${kind}_start`], scene[`${el}_${kind}_end`]];
  }

  function gxTlSetVals(el, kind, s, e) {
    const scene = gxScene();
    scene[`${el}_${kind}_start`] = s;
    scene[`${el}_${kind}_end`] = e;
  }

  function gxTlPositionBar(bar, s, e) {
    bar.style.left = `${(s * 100).toFixed(3)}%`;
    bar.style.width = `${(Math.max(GX_TL_MINW, e - s) * 100).toFixed(3)}%`;
  }

  function gxTlUpdateRowReadout(rowEl) {
    const scene = gxScene();
    if (!scene) return;
    const el = rowEl.dataset.el;
    const vals = rowEl.querySelector("[data-vals]");
    if (!vals) return;
    const outVisible = scene.outro_animation !== "none";
    const fmt = (v) => (Math.round(v * 100) / 100).toFixed(2);
    let txt = `in ${fmt(scene[`${el}_in_start`])}–${fmt(scene[`${el}_in_end`])}`;
    if (outVisible) txt += ` · out ${fmt(scene[`${el}_out_start`])}–${fmt(scene[`${el}_out_end`])}`;
    vals.textContent = txt;
  }

  function renderGxTimeline() {
    const scene = gxScene();
    const host = $("gxTlRows");
    if (!host) return;
    if (!scene) { host.innerHTML = ""; return; }
    gxEnsureTimingDefaults(scene);
    const outVisible = scene.outro_animation !== "none";
    host.innerHTML = GX_TL_ELEMENTS.map(({ el, label }) => `
      <div class="suite-gx-tl-row" data-el="${el}">
        <span class="suite-gx-tl-label">${label}</span>
        <div class="suite-gx-tl-track" data-track>
          <div class="suite-gx-tl-bar suite-gx-tl-bar--in" data-bar="in" title="${label} in animation" aria-label="${label} in animation bar">
            <i class="suite-gx-tl-handle suite-gx-tl-handle--l" data-h="l"></i>
            <i class="suite-gx-tl-handle suite-gx-tl-handle--r" data-h="r"></i>
          </div>
          ${outVisible ? `<div class="suite-gx-tl-bar suite-gx-tl-bar--out" data-bar="out" title="${label} out animation" aria-label="${label} out animation bar">
            <i class="suite-gx-tl-handle suite-gx-tl-handle--l" data-h="l"></i>
            <i class="suite-gx-tl-handle suite-gx-tl-handle--r" data-h="r"></i>
          </div>` : ""}
        </div>
        <span class="suite-gx-tl-vals" data-vals></span>
      </div>`).join("");
    host.querySelectorAll(".suite-gx-tl-row").forEach((rowEl) => {
      const el = rowEl.dataset.el;
      rowEl.querySelectorAll(".suite-gx-tl-bar").forEach((bar) => {
        const [s, e] = gxTlVals(el, bar.dataset.bar);
        gxTlPositionBar(bar, s, e);
      });
      gxTlUpdateRowReadout(rowEl);
    });
  }

  function gxTlApplyDrag(dFrac) {
    const scene = gxScene();
    const d = gxTlDrag;
    if (!scene || !d) return;
    const outVisible = scene.outro_animation !== "none";
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    // per-bar movement bounds enforcing the ≥0.02 cross-gap to the other bar
    let low = 0, high = 1;
    if (d.kind === "in" && outVisible) high = scene[`${d.el}_out_start`] - GX_TL_GAP;
    if (d.kind === "out") low = scene[`${d.el}_in_end`] + GX_TL_GAP;
    if (high < low + GX_TL_MINW) return; // no room — bar is pinned

    let s = d.s0, e = d.e0;
    if (d.mode === "move") {
      const w = d.e0 - d.s0;
      s = clamp(gxTlSnap(d.s0 + dFrac), low, high - w);
      e = s + w;
    } else if (d.mode === "l") {
      s = clamp(gxTlSnap(d.s0 + dFrac), low, d.e0 - GX_TL_MINW);
    } else { // "r"
      e = clamp(gxTlSnap(d.e0 + dFrac), d.s0 + GX_TL_MINW, high);
    }
    gxTlSetVals(d.el, d.kind, s, e);
    gxTlPositionBar(d.barEl, s, e);
    gxTlUpdateRowReadout(d.rowEl);
  }

  function wireGxTimeline() {
    const host = $("gxTlRows");

    host.addEventListener("pointerdown", (e) => {
      const bar = e.target.closest(".suite-gx-tl-bar");
      if (!bar || !gxScene()) return;
      const rowEl = bar.closest(".suite-gx-tl-row");
      const track = rowEl.querySelector("[data-track]");
      const handle = e.target.closest(".suite-gx-tl-handle");
      const kind = bar.dataset.bar;
      const [s0, e0] = gxTlVals(rowEl.dataset.el, kind);
      gxTlDrag = {
        el: rowEl.dataset.el,
        kind,
        mode: handle ? handle.dataset.h : "move",
        startX: e.clientX,
        trackW: Math.max(1, track.getBoundingClientRect().width),
        s0, e0,
        barEl: bar,
        rowEl,
      };
      try { bar.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
      bar.classList.add("is-dragging");
      e.preventDefault();
    });

    host.addEventListener("pointermove", (e) => {
      if (!gxTlDrag) return;
      gxTlApplyDrag((e.clientX - gxTlDrag.startX) / gxTlDrag.trackW);
    });

    const endDrag = (e) => {
      if (!gxTlDrag) return;
      const bar = gxTlDrag.barEl;
      gxTlDrag = null;
      bar.classList.remove("is-dragging");
      try { bar.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
      gxCommit();          // one undo entry per drag (addendum E)
      schedulePreview();
    };
    host.addEventListener("pointerup", endDrag);
    host.addEventListener("pointercancel", endDrag);
  }

  // ---------------- Graphics playback (Play button + scrubber) ----------------

  let gxPlayTimer = null; // setInterval, not rAF — rAF suspends while the document is hidden

  function gxStopPlayback() {
    if (gxPlayTimer != null) clearInterval(gxPlayTimer);
    gxPlayTimer = null;
    const btn = $("gxPlay");
    if (btn) { btn.textContent = "▶"; btn.classList.remove("is-active"); }
  }

  function gxTogglePlay() {
    if (gxPlayTimer != null) { gxStopPlayback(); return; }
    const scene = gxScene();
    if (!scene) return;
    const total = Math.max(0.1, (scene.duration || 0) + (scene.hold_seconds || 0));
    const startT = parseFloat($("gxScrub").value) >= 0.999 ? 0 : parseFloat($("gxScrub").value) || 0;
    const t0 = performance.now() - startT * total * 1000;
    const btn = $("gxPlay");
    btn.textContent = "■";
    btn.classList.add("is-active");
    gxPlayTimer = setInterval(() => {
      const t = Math.min(1, (performance.now() - t0) / 1000 / total);
      $("gxScrub").value = String(t);
      $("gxScrubTc").textContent = `${(t * total).toFixed(2)}s`;
      requestScrubPreview(t); // existing ~100ms throttle (addendum C5)
      if (t >= 1) gxStopPlayback();
    }, 33);
  }

  function wireGraphics() {
    // "change"-bound controls commit an undo entry immediately; "input"-bound
    // ones (typing, color picker) update the scene/preview live and commit
    // once on their native change (blur / picker close) — addendum E.
    const bind = (id, fn, evt = "change") => {
      $(id).addEventListener(evt, (e) => {
        const scene = gxScene();
        if (!scene) return;
        fn(scene, e);
        gxToggleConditionalRows();
        schedulePreview();
        if (evt === "change") gxCommit();
      });
      if (evt !== "change") $(id).addEventListener("change", () => gxCommit());
    };

    bind("gxPreset", (scene, e) => {
      const vals = (S.gx.options && S.gx.options.preset_values) || {};
      const preset = vals[e.target.value];
      if (preset) Object.assign(scene, JSON.parse(JSON.stringify(preset)));
      renderFormFromScene(scene);
    });

    bind("gxTitle", (scene, e) => { scene.title = e.target.value; }, "input");
    bind("gxSubtitle", (scene, e) => { scene.subtitle = e.target.value; }, "input");
    bind("gxLayout", (scene, e) => { scene.layout = e.target.value; });
    bind("gxCanvas", (scene, e) => {
      const dims = (S.gx.options && S.gx.options.canvas_presets || {})[e.target.value];
      if (dims) {
        scene.canvas_size = dims.slice();
        scene.canvas_preset_name = e.target.value;
      }
    });
    bind("gxBgStyle", (scene, e) => { scene.background_style = e.target.value; });
    bind("gxAnimation", (scene, e) => { scene.animation = e.target.value; });
    bind("gxOutro", (scene, e) => {
      scene.outro_animation = e.target.value;
      renderGxTimeline(); // OUT bars appear/disappear with the outro
    });
    bind("gxLtPosition", (scene, e) => { scene.lower_third_position = e.target.value; });
    bind("gxVignetteShape", (scene, e) => { scene.vignette_shape = e.target.value; });
    bind("gxTitleFont", (scene, e) => { scene.title_font = e.target.value; });
    bind("gxSubtitleFont", (scene, e) => { scene.subtitle_font = e.target.value; });
    bind("gxLogo", (scene, e) => { scene.logo = e.target.value; });
    bind("gxLogoPlacement", (scene, e) => { scene.logo_placement = e.target.value; });
    bind("gxLogoColorMode", (scene, e) => { scene.logo_color_mode = e.target.value; });
    bind("gxLogoCustomColor", (scene, e) => { scene.logo_custom_color = e.target.value; }, "input");
    bind("gxLogoGrow", (scene, e) => { scene.logo_grow = e.target.checked; });
    bind("gxTransparent", (scene, e) => { scene.transparent_bg = e.target.checked; });
    bind("gxDivider", (scene, e) => { scene.divider = e.target.checked; });
    bind("gxShadowEnabled", (scene, e) => { scene.shadow_enabled = e.target.checked; });
    bind("gxShadowColor", (scene, e) => { scene.shadow_color = e.target.value; }, "input");
    bind("gxDuration", (scene, e) => { scene.duration = parseFloat(e.target.value) || 4; });
    bind("gxHold", (scene, e) => { scene.hold_seconds = Math.max(0, parseFloat(e.target.value) || 0); });

    // Sliders: live scene+preview on input, ONE undo entry on release/commit.
    const bindSlider = (id, valId, suffix, apply) => {
      $(id).addEventListener("input", (e) => {
        const scene = gxScene();
        if (!scene) return;
        const v = Number(e.target.value);
        $(valId).textContent = `${Math.round(v)}${suffix}`;
        apply(scene, v);
        schedulePreview();
      });
      $(id).addEventListener("change", () => gxCommit());
    };
    bindSlider("gxTitleSize", "gxTitleSizeVal", "px", (s, v) => { s.title_size = v; });
    bindSlider("gxSubtitleSize", "gxSubtitleSizeVal", "px", (s, v) => { s.subtitle_size = v; });
    bindSlider("gxTextOffsetX", "gxTextOffsetXVal", "px", (s, v) => { s.text_offset_x = v; });
    bindSlider("gxTextOffsetY", "gxTextOffsetYVal", "px", (s, v) => { s.text_offset_y = v; });
    bindSlider("gxLogoHeight", "gxLogoHeightVal", "px", (s, v) => { s.logo_height = v; });
    bindSlider("gxLogoOpacity", "gxLogoOpacityVal", "%", (s, v) => { s.logo_opacity = v; });
    bindSlider("gxVignette", "gxVignetteVal", "%", (s, v) => { s.vignette = v; });
    bindSlider("gxShadowOpacity", "gxShadowOpacityVal", "%", (s, v) => { s.shadow_opacity = v; });
    bindSlider("gxShadowBlur", "gxShadowBlurVal", "px", (s, v) => { s.shadow_blur = v; });
    bindSlider("gxShadowOffsetX", "gxShadowOffsetXVal", "px", (s, v) => { s.shadow_offset_x = v; });
    bindSlider("gxShadowOffsetY", "gxShadowOffsetYVal", "px", (s, v) => { s.shadow_offset_y = v; });
    bindSlider("gxLtScale", "gxLtScaleVal", "%", (s, v) => { s.lower_third_scale = Math.round(v) / 100; });

    // color swatches (delegated per row container)
    ["gxSwBg", "gxSwAccent", "gxSwText", "gxSwGradient"].forEach((hostId) => {
      $(hostId).addEventListener("click", (e) => {
        const b = e.target.closest(".suite-swatch");
        const scene = gxScene();
        if (!b || !scene) return;
        scene[b.dataset.sceneKey] = b.dataset.hex;
        refreshSwatchSelection();
        schedulePreview();
        gxCommit();
      });
    });

    // undo/redo buttons + animation timeline + playback
    $("gxUndoBtn").addEventListener("click", gxUndo);
    $("gxRedoBtn").addEventListener("click", gxRedo);
    wireGxTimeline();
    $("gxPlay").addEventListener("click", gxTogglePlay);
    $("gxScrub").addEventListener("pointerdown", gxStopPlayback); // grabbing the scrubber stops playback

    // time scrubber — live animated preview while dragging
    $("gxScrub").addEventListener("input", (e) => {
      const t = parseFloat(e.target.value) || 0;
      const scene = gxScene();
      const total = scene ? ((scene.duration || 0) + (scene.hold_seconds || 0)) : 0;
      $("gxScrubTc").textContent = total ? `${(t * total).toFixed(2)}s` : `${Math.round(t * 100)}%`;
      requestScrubPreview(t);
    });

    // AI prompt bar — Local (prompt_ai) | Gemini (brander_ai_generate).
    // Blair Brander's Gemini key lives only in the system keychain (see
    // brander_gemini_key_status/brander_save_gemini_key) — entirely
    // separate from Rough Cut Studio's shared .env-based key, and never
    // logged, echoed, or written to any file from here.
    // Persistent Gemini call status (addendum v19) -- distinct from the
    // key-configured indicator (gxGeminiKeyStatus, unaffected by this):
    // this one reflects the last CALL (Apply or Generate Graphic) —
    // idle, in-flight, or the last result — so the outcome stays visible
    // in the UI rather than only flashing by as a toast.
    function setGeminiCallStatus(kind, message) {
      const el = $("gxGeminiCallStatus");
      if (!el) return;
      if (!kind) { el.hidden = true; el.className = "suite-gemini-status"; return; }
      el.hidden = false;
      el.className = `suite-gemini-status is-${kind}`;
      el.textContent = message;
    }

    const setAiMode = (mode) => {
      S.gx.aiMode = mode;
      ["gxAiModeLocal", "gxAiModeGemini"].forEach((id) => {
        const b = $(id);
        const active = b.dataset.mode === mode;
        b.classList.toggle("is-active", active);
        b.setAttribute("aria-pressed", active ? "true" : "false");
      });
      $("gxGenerateGraphic").hidden = mode !== "gemini";
      if (mode === "gemini") refreshBranderGeminiKeyStatus();
      else { setGeminiCallStatus(null); $("gxGeminiKeyHint").hidden = true; }
      $("gxPrompt").placeholder = mode === "gemini"
        ? 'Describe it for Gemini, e.g. "moody documentary title, navy, serif"'
        : 'Describe it, e.g. "pop upbeat, square, red gradient"';
    };
    $("gxAiModeLocal").addEventListener("click", () => setAiMode("local"));
    $("gxAiModeGemini").addEventListener("click", () => setAiMode("gemini"));

    function handleNoApiKey() {
      $("gxGeminiKeyHint").hidden = false;
      openSuiteSettings("graphics");
      $("gxGeminiKeyEdit").hidden = false;
      $("gxGeminiKeyInput").focus();
      toast("Gemini needs an API key — set Blair Brander's own key in Suite Settings → Graphics.", "info", 6000);
      setGeminiCallStatus("error", "No Gemini API key saved yet.");
    }

    const interpret = async () => {
      const text = $("gxPrompt").value.trim();
      const scene = gxScene();
      if (!text || !scene) return;
      const btn = $("gxInterpret");
      btn.disabled = true;
      if (S.gx.aiMode === "gemini") {
        btn.textContent = "Applying…";
        setGeminiCallStatus("loading", "Gemini is thinking…");
      }
      const res = S.gx.aiMode === "gemini"
        ? await call("brander_ai_generate", text, scene)
        : await call("brander_interpret", text, scene);
      btn.disabled = false;
      btn.textContent = "Apply";
      if (!res.ok) {
        if (res.error === "no_api_key") {
          handleNoApiKey();
        } else {
          toast(res.error || "Couldn't interpret that prompt.", "error");
          if (S.gx.aiMode === "gemini") setGeminiCallStatus("error", res.error || "Gemini request failed.");
        }
        return;
      }
      if (S.gx.aiMode === "gemini") setGeminiCallStatus("ok", "Gemini applied your last request.");
      S.gx.scene = res.scene;
      gxEnsureTimingDefaults(S.gx.scene);
      renderFormFromScene(S.gx.scene);
      schedulePreview();
      gxCommit(); // AI apply is one undoable step
      (res.notes || []).forEach((n) => toast(n, "info", 5200));
      $("gxPrompt").value = "";
    };

    // "Generate Graphic" (addendum v19) — a completely custom, AI-generated
    // background image via Gemini image generation (brander_ai_generate_graphic),
    // distinct from Apply's text-only scene tweaks (brander_ai_generate).
    // Shares the same status indicators and no-key handling as Apply.
    const generateGraphic = async () => {
      const scene = gxScene();
      if (!scene) return;
      const text = $("gxPrompt").value.trim();
      const btn = $("gxGenerateGraphic");
      btn.disabled = true;
      btn.textContent = "Generating…";
      setGeminiCallStatus("loading", "Gemini is generating a graphic — this can take a bit longer than a text edit…");
      const res = await call("brander_ai_generate_graphic", text, scene);
      btn.disabled = false;
      btn.textContent = "Generate Graphic";
      if (!res.ok) {
        if (res.error === "no_api_key") {
          handleNoApiKey();
        } else {
          toast(res.error || "Couldn't generate that graphic.", "error");
          setGeminiCallStatus("error", res.error || "Gemini image generation failed.");
        }
        return;
      }
      setGeminiCallStatus("ok", "Gemini generated a new background graphic.");
      S.gx.scene = res.scene;
      gxEnsureTimingDefaults(S.gx.scene);
      renderFormFromScene(S.gx.scene);
      // brander_ai_generate_graphic already rendered a preview against the
      // NEW scene server-side (res.data_uri) — use it directly instead of
      // the usual debounced schedulePreview() round-trip, so the freshly
      // generated image appears immediately.
      if (res.data_uri) $("gxPreviewImg").src = res.data_uri;
      gxCommit();
      toast("Generated a new background graphic.", "ok");
    };
    $("gxGenerateGraphic").addEventListener("click", generateGraphic);

    $("gxAiBgClear").addEventListener("click", async () => {
      const scene = gxScene();
      if (!scene) return;
      const res = await call("brander_clear_ai_background", scene);
      if (!res.ok) { toastIfError(res, "Couldn't remove that graphic."); return; }
      S.gx.scene = res.scene;
      gxToggleConditionalRows();
      schedulePreview();
      gxCommit();
      toast("Removed the AI graphic — back to the regular background style.", "ok");
    });
    $("gxInterpret").addEventListener("click", interpret);
    // Enter inserts a newline now that the prompt is a multi-line textarea;
    // Cmd/Ctrl+Enter submits instead (same "submit" shortcut as chat inputs).
    $("gxPrompt").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); interpret(); }
    });

    $("gxGeminiKeyStatus").addEventListener("click", (e) => {
      if (!e.target.closest('button[data-action="gx-gemini-key-change"]')) return;
      $("gxGeminiKeyEdit").hidden = false;
      $("gxGeminiKeyInput").focus();
    });

    $("gxGeminiKeySave").addEventListener("click", async () => {
      const key = $("gxGeminiKeyInput").value.trim();
      const res = await call("brander_save_gemini_key", key);
      if (!res.ok) { toast(res.error || "Couldn't save the Gemini API key.", "error"); return; }
      $("gxGeminiKeyInput").value = "";
      toast(key ? "Gemini API key saved to the keychain." : "Gemini API key removed.", "ok");
      refreshBranderGeminiKeyStatus();
    });

    // Logo import (addendum C2)
    $("gxImportLogo").addEventListener("click", async () => {
      const res = await call("brander_import_logo");
      if (!res.ok) { toastIfError(res, "Couldn't import that logo."); return; }
      if (!res.selected) return; // dialog cancelled
      if (S.gx.options) S.gx.options.logos = res.logos || S.gx.options.logos;
      fillSelect($("gxLogo"), (res.logos || []), res.selected);
      const scene = gxScene();
      if (scene) {
        scene.logo = res.selected;
        schedulePreview();
        gxCommit();
      }
      toast(`Imported logo "${res.selected}" — white backgrounds are keyed to transparency.`, "ok", 5600);
    });

    $("gxRemoveLogo").addEventListener("click", async () => {
      const name = $("gxLogo").value;
      const res = await call("brander_remove_custom_logo", name);
      if (!res.ok) { toastIfError(res, "Couldn't remove that logo."); return; }
      if (S.gx.options) S.gx.options.logos = res.logos || S.gx.options.logos;
      const scene = gxScene();
      const wasSelected = scene && scene.logo === name;
      fillSelect($("gxLogo"), (res.logos || []), wasSelected ? "None" : (scene && scene.logo));
      if (scene && wasSelected) {
        scene.logo = "None";
        schedulePreview();
        gxCommit();
      }
      gxToggleConditionalRows();
      toast(`Removed "${name}".`, "ok");
    });

    // action buttons
    $("gxExportPng").addEventListener("click", async () => {
      const scene = gxScene();
      if (!scene) return;
      const res = await call("brander_export_png", scene);
      if (!res.ok) { toastIfError(res, "Couldn't export the PNG."); return; }
      toast(`PNG saved to ${res.path}`, "ok");
    });

    $("gxExportVideo").addEventListener("click", async () => {
      const scene = gxScene();
      if (!scene) return;
      const res = await call("brander_export_video", scene, $("gxCodec").value);
      if (!res.ok) { toastIfError(res, "Couldn't start the video export."); return; }
      toast("Rendering video in the background…", "ok");
      ensurePolling();
      openDrawer();
    });

    $("gxSaveProject").addEventListener("click", async () => {
      const scene = gxScene();
      if (!scene) return;
      const res = await call("brander_save_project", scene);
      if (!res.ok) { toastIfError(res, "Couldn't save the project."); return; }
      toast(`Project saved to ${res.path}`, "ok");
    });

    $("gxLoadProject").addEventListener("click", async () => {
      const res = await call("brander_load_project");
      if (!res.ok) { toastIfError(res, "Couldn't load that project."); return; }
      if (!res.scene) return;
      S.gx.scene = res.scene;
      gxEnsureTimingDefaults(S.gx.scene);
      renderFormFromScene(S.gx.scene);
      schedulePreview();
      gxCommit(); // loading a project is one undoable step back to the previous scene
      toast(`Loaded ${res.path || "project"}.`, "ok");
    });

    $("gxSendToEdit").addEventListener("click", async () => {
      const scene = gxScene();
      if (!scene) return;
      const res = await call("brander_send_to_edit", scene);
      if (!res.ok) { toast(res.error || "Couldn't start the render.", "error"); return; }
      toast("Rendering graphic with alpha — it will be added to Edit when done.", "ok");
      ensurePolling();
      openDrawer();
    });
  }

  // ---------------- suite chrome wiring ----------------

  function wireChrome() {
    document.querySelectorAll("#suiteTopbar .suite-ws-tab").forEach((btn) => {
      btn.addEventListener("click", () => switchWs(btn.dataset.ws));
    });

    $("suiteJobsBtn").addEventListener("click", () => {
      if (S.drawerOpen) closeDrawer(); else openDrawer();
    });
    $("suiteJobsClose").addEventListener("click", closeDrawer);

    $("suiteClearFinished").addEventListener("click", async () => {
      const res = await call("suite_clear_finished_jobs");
      if (!res.ok) { toast(res.error || "Couldn't clear finished jobs.", "error"); return; }
      // Drop transition bookkeeping for jobs that no longer exist next poll.
      await pollJobs();
      renderDrawer(true);
    });

    // Escape closes the jobs drawer (only when the RCS transcript modal isn't
    // the thing being closed — its own handler checks `hidden` itself).
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || !S.drawerOpen) return;
      const rcsModal = document.getElementById("transcriptModal");
      if (rcsModal && !rcsModal.hidden) return; // let RCS close its modal first
      closeDrawer();
    });

    // delegated jobs-drawer actions
    $("suiteJobsList").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      const id = btn.dataset.id;
      if (btn.dataset.action === "cancel") {
        call("suite_cancel_job", id).then((res) => {
          if (!res.ok) toast(res.error || "Couldn't cancel that job.", "error");
          pollJobs();
        });
      } else if (btn.dataset.action === "t-send") {
        sendTranscribeToEdit(id);
      } else if (btn.dataset.action === "t-preview") {
        if (S.tExpanded.has(id)) S.tExpanded.delete(id); else S.tExpanded.add(id);
        switchWs("transcribe");
        renderTranscribeResults(true);
        const card = $("tResults");
        if (card) card.scrollTop = 0;
      } else if (btn.dataset.action === "b-view") {
        switchWs("broll");
      } else if (btn.dataset.action === "sy-view") {
        switchWs("sync");
      } else if (btn.dataset.action === "hz-view") {
        switchWs("harmonize");
      } else if (btn.dataset.action === "cc-pause" || btn.dataset.action === "cc-resume" || btn.dataset.action === "cc-cancel") {
        // These act on the whole Card Eater job (every destination), same
        // as clicking any one destination's Pause/Resume/Cancel did in the
        // original app's own queue — pause/resume/cancel are job-level,
        // not per-destination, in the backend (cardeater_copy.py).
        const job = S.jobs.find((j) => j.id === id);
        if (!job || job.cardeater_job_id == null) return;
        const method = btn.dataset.action === "cc-pause" ? "suite_cardeater_pause_job"
          : btn.dataset.action === "cc-resume" ? "suite_cardeater_resume_job"
          : "suite_cardeater_cancel_job";
        call(method, job.cardeater_job_id).then((res) => {
          if (!res.ok) toast(res.error || "Couldn't update that copy job.", "error");
          pollJobs();
        });
      } else if (btn.dataset.action === "cc-open-folder") {
        call("suite_cardeater_open_folder", btn.dataset.path).then((res) => {
          toastIfError(res, "Couldn't open that folder");
        });
      } else if (btn.dataset.action === "cc-send-broll") {
        ceSendToBroll(btn.dataset.path);
      }
    });

    $("suiteSettingsBtn").addEventListener("click", () => openSuiteSettings());
    $("suiteSettingsClose").addEventListener("click", closeSuiteSettings);
    $("suiteSettingsModal").addEventListener("click", (e) => {
      if (e.target.id === "suiteSettingsModal") closeSuiteSettings();
    });
    document.querySelectorAll("#suiteSettingsModal .suite-settings-tab").forEach((btn) => {
      btn.addEventListener("click", () => switchSettingsTab(btn.dataset.settingsTab));
    });
    $("stgNotify").addEventListener("change", (e) => {
      S.settings.notifyOnJobDone = e.target.checked;
      saveSuiteSettings();
    });
    $("stgClearCache").addEventListener("click", async () => {
      const res = await call("suite_clear_proxy_cache");
      if (!res.ok) { toast(res.error || "Couldn't clear the cache.", "error"); return; }
      toast(`Cleared ${res.removed} cached proxy file(s).`, "ok");
      refreshSuiteSettingsCacheInfo();
    });

    $("suitePipelineBtn").addEventListener("click", () => openSuitePipeline(null));
    $("suitePipelineClose").addEventListener("click", closeSuitePipeline);
    $("suitePipelineModal").addEventListener("click", (e) => {
      if (e.target.id === "suitePipelineModal") closeSuitePipeline();
    });
  }

  // ---------------- suite settings modal ----------------

  function bytesToHuman(n) {
    n = Number(n) || 0;
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  const SETTINGS_TABS = ["general", "copy", "search", "transcribe", "edit", "graphics"];

  function openSuiteSettings(tab) {
    $("suiteSettingsModal").hidden = false;
    switchSettingsTab(tab || S.settingsTab || "general");
  }

  function closeSuiteSettings() { $("suiteSettingsModal").hidden = true; updateSpyglassRootsPolling(); }

  // Settings panes are static markup (see shell.html), not rebuilt on every
  // open -- the Edit pane hosts a live node relocated out of RCS's own DOM
  // (relocateLlmProviderSettings()), and innerHTML-replacing it here would
  // silently destroy that node instead of just re-rendering a template.
  function switchSettingsTab(tab) {
    if (!SETTINGS_TABS.includes(tab)) tab = "general";
    S.settingsTab = tab;
    document.querySelectorAll("#suiteSettingsModal .suite-settings-tab").forEach((btn) => {
      const active = btn.dataset.settingsTab === tab;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll("#suiteSettingsModal .suite-settings-pane").forEach((pane) => {
      pane.hidden = pane.dataset.settingsPane !== tab;
    });
    if (tab === "general") {
      $("stgNotify").checked = !!S.settings.notifyOnJobDone;
      refreshSuiteSettingsCacheInfo();
    } else if (tab === "copy") {
      ceRenderTemplateSelect();
      ceApplyDraftToForm();
    } else if (tab === "search") {
      loadSpyglassRoots();
      loadSpyglassQueueStatus();
    } else if (tab === "transcribe") {
      refreshTokenStatus();
    } else if (tab === "graphics") {
      refreshBranderGeminiKeyStatus();
    }
    updateSpyglassRootsPolling();
  }

  async function refreshSuiteSettingsCacheInfo() {
    const el = $("stgCacheInfo");
    if (!el) return;
    el.textContent = "Reading cache usage…";
    const info = await call("suite_proxy_cache_info");
    if (!el.isConnected) return; // modal closed before this resolved
    if (!info.ok) { el.textContent = info.error || "Couldn't read cache usage."; return; }
    el.textContent = `${bytesToHuman(info.bytes_used)} used of ${bytesToHuman(info.bytes_cap)} cap `
      + `(${info.file_count} cached proxy file${info.file_count === 1 ? "" : "s"}).`;
  }

  // ---------------- pipeline modal ----------------
  //
  // "Run Pipeline" queues Sync/Transcribe/B-Roll jobs together for one
  // folder instead of visiting each workspace by hand. It does NOT wait
  // for one stage to finish before starting the next -- Sync and
  // Transcribe are order-independent with each other and with B-Roll
  // (CLAUDE.md, "Sync workspace specifics": their sidecar merges however
  // they arrive), so every checked stage's job(s) fire together, exactly
  // the concurrency jobs.py already gives every job kind.
  //
  // The Sync stage has one real limitation, called out in its own hint
  // text: sync_start pairs ONE video against a list of candidate audio
  // files, and there is no reliable way to guess that pairing from a
  // folder alone (that's the whole reason A-Sync uses cross-correlation
  // instead of name matching). So the pipeline offers a single shared
  // audio pool tried against every discovered video -- the right shape
  // for a common single-recorder/multi-camera shoot, not a substitute
  // for the Sync workspace's own per-video pairing on a dual-system job.

  const PIPELINE_SETTINGS_STORAGE_KEY = "suitePipelineSettings.v1";

  function saveSuitePipelineSettings() {
    try {
      localStorage.setItem(PIPELINE_SETTINGS_STORAGE_KEY, JSON.stringify({
        stages: S.pipeline.stages,
        syncMethod: S.pipeline.syncMethod,
      }));
    } catch (e) {
      // localStorage disabled/full — settings just won't persist this run.
    }
  }

  function restoreSuitePipelineSettings() {
    let saved;
    try {
      saved = JSON.parse(localStorage.getItem(PIPELINE_SETTINGS_STORAGE_KEY) || "null");
    } catch (e) {
      return;
    }
    if (!saved || typeof saved !== "object") return;
    if (saved.stages && typeof saved.stages === "object") {
      Object.assign(S.pipeline.stages, saved.stages);
    }
    if (typeof saved.syncMethod === "string") S.pipeline.syncMethod = saved.syncMethod;
  }

  function openSuitePipeline(folder) {
    S.pipeline.folder = folder || null;
    S.pipeline.videos = [];
    S.pipeline.syncAudios = [];
    $("suitePipelineModal").hidden = false;
    renderSuitePipeline();
    if (S.pipeline.folder) suitePipelineScan();
  }

  function closeSuitePipeline() { $("suitePipelineModal").hidden = true; }

  async function suitePipelineScan() {
    const body = $("suitePipelineBody");
    if (body) {
      const countEl = body.querySelector("#pplVideoCount");
      if (countEl) countEl.textContent = "Scanning…";
    }
    const res = await call("pipeline_list_videos", S.pipeline.folder);
    if (!res.ok) { toast(res.error || "Couldn't scan that folder.", "error"); S.pipeline.videos = []; }
    else S.pipeline.videos = res.videos || [];
    renderSuitePipeline();
  }

  function renderSuitePipeline() {
    const p = S.pipeline;
    const body = $("suitePipelineBody");
    body.innerHTML = `
      <div class="suite-folder-row">
        <input type="text" id="pplFolder" placeholder="/path/to/folder" value="${esc(p.folder || "")}" readonly />
        <button class="suite-btn suite-btn--small" id="pplChooseFolder">Choose Folder…</button>
      </div>
      <p class="suite-hint suite-hint--tight" id="pplVideoCount">
        ${p.folder ? `${p.videos.length} video file(s) found.` : "Choose a folder to scan for video files."}
      </p>

      <div class="suite-block">
        <h3 class="suite-block-title">Stages</h3>
        <label class="suite-hint"><input type="checkbox" id="pplStageSync" ${p.stages.sync ? "checked" : ""} /> Sync</label>
        <label class="suite-hint"><input type="checkbox" id="pplStageTranscribe" ${p.stages.transcribe ? "checked" : ""} /> Transcribe</label>
        <label class="suite-hint"><input type="checkbox" id="pplStageBroll" ${p.stages.broll ? "checked" : ""} /> B-Roll</label>
        <label class="suite-hint"><input type="checkbox" id="pplStageEdit" ${p.stages.edit ? "checked" : ""} /> Edit</label>
        <p class="suite-hint suite-hint--tight">Transcribe and B-Roll use your saved settings from those workspaces. Edit automatically forwards each stage's results into the Edit workspace as it finishes — Transcribe into Sources, B-Roll into the B-Roll tab — no generated script needed.</p>
      </div>

      <div class="suite-block" id="pplSyncBlock" ${p.stages.sync ? "" : "hidden"}>
        <h3 class="suite-block-title">Sync — Shared Audio</h3>
        <p class="suite-hint suite-hint--tight">These audio file(s) are tried against EVERY video found above — best for one shared recorder (e.g. a boom mic) covering several cameras. For dual-system audio paired per clip, use the Sync workspace directly instead.</p>
        <ul class="suite-file-list" id="pplSyncAudioList">
          ${p.syncAudios.length === 0
            ? `<li style="border-style:dashed;color:var(--text-faint);justify-content:center;">No audio files added</li>`
            : p.syncAudios.map((path, i) => `<li>${esc(basename(path))}<button class="suite-btn suite-btn--ghost suite-btn--small" data-action="ppl-remove-audio" data-i="${i}">✕</button></li>`).join("")}
        </ul>
        <div class="suite-folder-row">
          <button class="suite-btn suite-btn--small" id="pplAddAudio">Add Audio…</button>
          <select id="pplSyncMethod" aria-label="Sync detection method">
            <option value="waveform" ${p.syncMethod === "waveform" ? "selected" : ""}>Waveform analysis</option>
            <option value="timecode" ${p.syncMethod === "timecode" ? "selected" : ""}>Embedded timecode</option>
          </select>
        </div>
      </div>

      <button class="suite-btn suite-btn--primary" id="pplRun" ${p.folder ? "" : "disabled"}>Run Pipeline</button>
    `;

    $("pplChooseFolder").addEventListener("click", async () => {
      const res = await call("broll_pick_folder");
      if (!res.ok) { toastIfError(res, "Couldn't open the folder dialog."); return; }
      if (!res.path) return;
      S.pipeline.folder = res.path;
      S.pipeline.videos = [];
      renderSuitePipeline();
      suitePipelineScan();
    });

    $("pplStageSync").addEventListener("change", (e) => { p.stages.sync = e.target.checked; saveSuitePipelineSettings(); renderSuitePipeline(); });
    $("pplStageTranscribe").addEventListener("change", (e) => { p.stages.transcribe = e.target.checked; saveSuitePipelineSettings(); });
    $("pplStageBroll").addEventListener("change", (e) => { p.stages.broll = e.target.checked; saveSuitePipelineSettings(); });
    $("pplStageEdit").addEventListener("change", (e) => { p.stages.edit = e.target.checked; saveSuitePipelineSettings(); });

    const addAudioBtn = $("pplAddAudio");
    if (addAudioBtn) {
      addAudioBtn.addEventListener("click", async () => {
        const res = await call("sync_pick_audio");
        if (!res.ok) { toastIfError(res, "Couldn't open the audio file dialog."); return; }
        (res.paths || []).forEach((path) => { if (!p.syncAudios.includes(path)) p.syncAudios.push(path); });
        renderSuitePipeline();
      });
    }
    const methodSel = $("pplSyncMethod");
    if (methodSel) methodSel.addEventListener("change", (e) => { p.syncMethod = e.target.value; saveSuitePipelineSettings(); });

    body.querySelectorAll('[data-action="ppl-remove-audio"]').forEach((btn) => {
      btn.addEventListener("click", () => {
        p.syncAudios.splice(Number(btn.dataset.i), 1);
        renderSuitePipeline();
      });
    });

    $("pplRun").addEventListener("click", suitePipelineRun);
  }

  async function suitePipelineRun() {
    const p = S.pipeline;
    if (!p.folder) return;
    const needsVideos = p.stages.sync || p.stages.transcribe;
    if (needsVideos && p.videos.length === 0) {
      toast("No video files found in that folder.", "info");
      return;
    }
    if (p.stages.sync && p.syncAudios.length === 0) {
      toast("Add at least one audio file for the Sync stage, or uncheck it.", "error");
      return;
    }
    let queued = 0;
    if (p.stages.sync) {
      for (const video of p.videos) {
        const res = await call("sync_start", video, p.syncAudios.slice(), p.syncMethod);
        if (res.ok) queued++;
        else toastIfError(res, `Couldn't start Sync for ${basename(video)}.`);
      }
    }
    if (p.stages.transcribe) {
      let saved;
      try { saved = JSON.parse(localStorage.getItem(TRANSCRIBER_SETTINGS_STORAGE_KEY) || "null"); } catch (e) { saved = null; }
      const model = (saved && saved.model) || $("tModel").value;
      const diarize = !!(saved && saved.diarize);
      const res = await call("transcriber_start", p.videos.slice(), model, diarize);
      if (res.ok) {
        const ids = res.job_ids || [];
        queued += ids.length;
        if (p.stages.edit) ids.forEach((id) => p.autoEditTranscribeJobIds.add(id));
      } else {
        toastIfError(res, "Couldn't start transcription.");
      }
    }
    if (p.stages.broll) {
      let options = null;
      try {
        const saved = JSON.parse(localStorage.getItem(BROLL_SETTINGS_STORAGE_KEY) || "null");
        if (saved) {
          options = {
            window_sec: parseFloat(saved.bWindowSec) || 4.0,
            max_segments: parseInt(saved.bMaxSegments, 10) || undefined,
            min_segment_gap_sec: parseFloat(saved.bMinGap) || undefined,
            enable_energy: !!saved.bEnergy,
            energy_weight: parseFloat(saved.bEnergyWeight) || undefined,
            max_workers: parseInt(saved.bWorkers, 10) || undefined,
          };
        }
      } catch (e) { options = null; }
      const res = await call("broll_start", p.folder, options);
      if (res.ok) {
        queued++;
        if (p.stages.edit) p.autoEditBrollJobId = res.job_id;
      } else {
        toastIfError(res, "Couldn't start B-Roll analysis.");
      }
    }
    if (queued > 0) {
      toast(`Pipeline started — ${queued} job(s) queued.`, "ok");
      closeSuitePipeline();
      ensurePolling();
      openDrawer();
    }
  }

  // ============================================================
  // RCS frontend integration
  //
  // Rough Cut Studio's app.js is a sibling classic script loaded before this
  // one, so its top-level declarations are visible here:
  //   state (L3, incl. state.editSegments / state.sources)
  //   renderSources (L76), newCid (L342), appendCutRow (L478),
  //   renderEditTable (L415), readEditTableIntoState (L1292),
  //   pushUndoSnapshot (L721), flushTcEditSnapshot (L712),
  //   refreshBrollOverlapWarnings (L633), activateTab (L2028), rulerFps (L35).
  // Every symbol is typeof-guarded — if RCS changes, the suite degrades to a
  // toast telling the user where to find the ingested source.
  // ============================================================

  function rcsEditReady() {
    return typeof state !== "undefined" && state && Array.isArray(state.editSegments) &&
      typeof appendCutRow === "function" &&
      typeof readEditTableIntoState === "function" &&
      typeof pushUndoSnapshot === "function";
  }

  function suiteTcToSeconds(tc, fps) {
    const m = /^(\d+):(\d+):(\d+)[:;](\d+)$/.exec(String(tc || "").trim());
    if (!m) return null;
    return Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]) + Number(m[4]) / fps;
  }

  function suiteSecondsToTc(sec, fps) {
    const total = Math.max(0, sec || 0);
    const whole = Math.floor(total);
    const f = Math.min(Math.round(fps) - 1, Math.round((total - whole) * fps));
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(Math.floor(whole / 3600))}:${pad(Math.floor(whole / 60) % 60)}:${pad(whole % 60)}:${pad(Math.max(0, f))}`;
  }

  // Re-render the RCS source list from the backend's authoritative view —
  // same approach as RCS's own autosave-restore path (list_sources returns a
  // plain array of source dicts).
  async function refreshRcsSources() {
    const a = suiteApi();
    if (!a || typeof a.list_sources !== "function") return false;
    let list;
    try {
      list = await a.list_sources();
    } catch (err) {
      return false;
    }
    const arr = Array.isArray(list) ? list : (list && list.sources) || [];
    if (typeof state === "undefined" || !state || typeof renderSources !== "function") return false;
    state.sources = {};
    arr.forEach((s) => { if (s && s.source_id) state.sources[s.source_id] = s; });
    renderSources();
    return true;
  }

  // Insert CutSpecs ({source_id, start_seconds, end_seconds, in_tc, out_tc,
  // track:"broll"}) as B-roll rows in the RCS Cuts table, following RCS's own
  // mutation protocol: flush any pending timecode-edit snapshot, sync DOM
  // edits into state, push ONE undo snapshot, then append rows via
  // appendCutRow (which assigns _cid through buildRowElement). New clips are
  // laid end-to-end on the timeline after any existing B-roll.
  function insertBrollCuts(cutSpecs) {
    if (!cutSpecs || cutSpecs.length === 0) return false;
    if (!rcsEditReady()) {
      toast("Added source(s) — open the Cuts tab in Edit to place them.", "info");
      return false;
    }
    const fps = (typeof rulerFps === "number" && rulerFps > 0) ? rulerFps : 25;

    if (typeof flushTcEditSnapshot === "function") flushTcEditSnapshot();
    readEditTableIntoState();
    pushUndoSnapshot();

    // Start placing after the last existing B-roll clip on the timeline.
    let cursor = 0;
    state.editSegments.forEach((seg) => {
      if (seg.track !== "broll") return;
      const st = suiteTcToSeconds(seg.timeline_start_tc, fps);
      const i = suiteTcToSeconds(seg.in_tc, fps);
      const o = suiteTcToSeconds(seg.out_tc, fps);
      if (st != null && i != null && o != null && o > i) cursor = Math.max(cursor, st + (o - i));
    });

    cutSpecs.forEach((spec) => {
      const dur = Math.max(0, (spec.end_seconds || 0) - (spec.start_seconds || 0));
      const seg = {
        track: "broll",
        source_id: spec.source_id,
        in_tc: spec.in_tc || suiteSecondsToTc(spec.start_seconds, fps),
        out_tc: spec.out_tc || suiteSecondsToTc(spec.end_seconds, fps),
        in_seconds: spec.start_seconds,
        out_seconds: spec.end_seconds,
        note: "",
        on_screen_text: "",
        source_text: "",
        timeline_start_tc: suiteSecondsToTc(cursor, fps),
        audio_mode: "silent",
        duck_db: -12,
      };
      cursor += dur;
      state.editSegments.push(seg);
      appendCutRow(seg);
    });

    if (typeof refreshBrollOverlapWarnings === "function") refreshBrollOverlapWarnings();

    // Before the first generation #outputBlock (and the Cuts table in it) is
    // hidden — reveal it so the inserted rows are actually visible, and jump
    // to the Cuts tab.
    const outputBlock = document.getElementById("outputBlock");
    if (outputBlock) outputBlock.hidden = false;
    if (typeof activateTab === "function") activateTab("edit");

    const hasMain = state.editSegments.some((s) => (s.track || "main") !== "broll");
    if (!hasMain) {
      toast("B-roll placed — the timeline needs at least one main-track cut before export.", "info", 6000);
    }
    scheduleSuiteTimelineRefresh(); // send-to-edit insertions refresh the suite timeline
    return true;
  }

  // ============================================================
  // Favorites (addendum v6) — favorite a transcript line in RCS's own
  // transcript-viewer modal, a 5th "Favorites" tab listing them all, and a
  // matching badge on any Cuts row that came from one. S.favorites is the
  // in-memory cache; every mutation updates it locally (no refetch) and the
  // backend file is the durable source of truth across launches.
  // ============================================================

  async function loadFavorites() {
    const res = await call("suite_list_favorites");
    if (res.ok) S.favorites = res.favorites || [];
  }

  // Re-fetch S.favorites from the backend and re-paint every surface that
  // reads it (addendum v17). The backend already clears/reloads
  // self.favorites correctly on a successful New Project / Load Project
  // (see FavoritesMixin.new_project/load_project) — the reported "still
  // not clearing out" bug was purely this: S.favorites is an in-memory
  // CACHE (see the block comment above loadFavorites), only ever
  // populated once at boot(), so the Favorites/B-Roll tabs and every star
  // kept showing the discarded project's stale data until relaunch.
  async function refreshFavoritesAfterProjectChange() {
    await loadFavorites();
    renderFavoritesPanel();
    if (typeof renderBrollFavoritesPanel === "function") renderBrollFavoritesPanel();
    refreshTranscriptFavoriteStars();
    refreshCutsRowFavoriteMarkers();
    refreshPreviewFavoriteStar();
  }

  // RCS's own "New Project"/"Load Project" buttons (app.js, never
  // modified) call window.pywebview.api.new_project()/load_project()
  // directly at click time — there's no hook to attach a suite-side
  // "afterward" step to from that file. Wrapping the two methods on the
  // pywebview.api OBJECT itself (not RCS's JS) is transparent to app.js:
  // its `await window.pywebview.api.new_project()` call reaches this
  // wrapper without any change on its side, and the wrapper's own return
  // value is indistinguishable from the original (same shape, just
  // awaited one layer deeper).
  function wrapProjectLifecycleApiMethods() {
    const api = suiteApi();
    if (!api || api.__suiteLifecycleWrapped) return;
    api.__suiteLifecycleWrapped = true;
    ["new_project", "load_project"].forEach((name) => {
      const orig = api[name];
      if (typeof orig !== "function") return;
      api[name] = async (...args) => {
        const res = await orig.apply(api, args);
        if (res && res.ok) await refreshFavoritesAfterProjectChange();
        return res;
      };
    });
  }

  // Write-guarded star state — critical for the Cuts-table star
  // (refreshCutsRowFavoriteMarkers runs from injectSuiteTimeline's
  // {childList:true, subtree:true} MutationObserver on #editTableBody): an
  // UNCONDITIONAL `btn.textContent = ...` is itself a childList mutation
  // even when the glyph doesn't change, which would re-trigger that same
  // observer forever. Only touch the DOM when a value actually changes.
  function setFavStarState(btn, isFav) {
    const glyph = isFav ? "★" : "☆";
    if (btn.textContent !== glyph) btn.textContent = glyph;
    if (btn.classList.contains("is-fav") !== isFav) btn.classList.toggle("is-fav", isFav);
    const label = isFav ? "Remove from Favorites" : "Add to Favorites";
    if (btn.title !== label) {
      btn.title = label;
      btn.setAttribute("aria-label", label);
    }
  }

  // Same tolerance backend/favorites.py's find() uses by default — keep
  // these in sync.
  const FAV_TOLERANCE_SECONDS = 0.05;

  // The one true "is this range favorited" check, shared by every star
  // surface. Matches on NUMERIC seconds (with the backend's own
  // tolerance), never on timecode STRINGS — a Cuts row/preview star used
  // to compare `start_tc`/`end_tc` text directly, which looked consistent
  // surface-to-surface but could still silently mismatch the backend's
  // own idea of "favorited": the backend stores start_tc/end_tc as
  // whatever `format_timecode()` produced from the seconds at favoriting
  // time, which isn't guaranteed to be byte-identical to whatever string
  // happens to be sitting in a row's in/out input right now (a different
  // rulerFps read, or a fresh round-trip through timecode math, can shift
  // the string by a frame's worth of rounding even when the underlying
  // range is the same to well within the backend's own 0.05s tolerance).
  // Comparing the same seconds+tolerance the backend already uses
  // eliminates that class of drift entirely.
  function isFavoritedRange(sourceId, startSeconds, endSeconds) {
    if (sourceId == null || startSeconds == null || endSeconds == null) return false;
    return S.favorites.some((f) =>
      f.source_id === sourceId &&
      Math.abs((f.start_seconds || 0) - startSeconds) <= FAV_TOLERANCE_SECONDS &&
      Math.abs((f.end_seconds || 0) - endSeconds) <= FAV_TOLERANCE_SECONDS);
  }

  // Inject a star toggle into each row of RCS's transcript-viewer modal
  // (#transcriptModalBody), inside the existing "+ Add" cell — no new
  // column/header needed. viewTranscript() fully rebuilds the tbody
  // (innerHTML="" + appendChild) every time it opens a transcript, so the
  // stars are (re)injected from a MutationObserver, same idiom as
  // injectTranscriptSearch's search-reset observer on the same element.
  function injectTranscriptFavoriteStars() {
    const tbody = document.getElementById("transcriptModalBody");
    if (!tbody || tbody.dataset.suiteFavWired) return;
    tbody.dataset.suiteFavWired = "1";

    function renderStars() {
      tbody.querySelectorAll('button[data-act="add-to-cuts"]').forEach((addBtn) => {
        const cell = addBtn.parentElement;
        if (!cell || cell.querySelector(".suite-fav-btn")) return;
        const star = document.createElement("button");
        star.type = "button";
        star.className = "row-btn suite-fav-btn";
        star.dataset.tact = "fav-toggle";
        star.dataset.idx = addBtn.dataset.idx;
        cell.insertBefore(star, addBtn);
      });
      refreshTranscriptFavoriteStars();
    }

    new MutationObserver(renderStars).observe(tbody, { childList: true });
    renderStars();

    // Coexists with RCS's own click listener on the same element (that one
    // filters on data-act="add-to-cuts"; this one filters on a different
    // attribute, data-tact="fav-toggle" — never collides).
    tbody.addEventListener("click", async (e) => {
      const btn = e.target.closest('button[data-tact="fav-toggle"]');
      if (!btn) return;
      const idx = Number(btn.dataset.idx);
      const sourceId = (typeof state !== "undefined" && state) ? state.transcriptModalSourceId : null;
      if (sourceId == null) return;
      const res = await call("suite_toggle_favorite", sourceId, idx);
      if (!res.ok) { toastIfError(res, "Couldn't update that favorite."); return; }
      if (res.favorited) {
        S.favorites.push(res.favorite);
        revealOutputBlockIfHidden(); // no script needed to see the Favorites tab
      } else {
        S.favorites = S.favorites.filter((f) => !(f.source_id === sourceId && f.index === idx));
      }
      refreshTranscriptFavoriteStars();
      refreshCutsRowFavoriteMarkers();
      renderFavoritesPanel();
    });
  }

  function refreshTranscriptFavoriteStars() {
    const tbody = document.getElementById("transcriptModalBody");
    if (!tbody || typeof state === "undefined" || !state) return;
    const sourceId = state.transcriptModalSourceId;
    const segments = Array.isArray(state.transcriptModalSegments) ? state.transcriptModalSegments : [];
    tbody.querySelectorAll('button[data-tact="fav-toggle"]').forEach((star) => {
      const idx = Number(star.dataset.idx);
      const seg = segments.find((s) => s.index === idx);
      // Match on the segment's own start_seconds/end_seconds (real numbers
      // straight from the parsed transcript, no tc-string round-trip at
      // all) — the same key the Cuts-row and preview-window stars use, and
      // the only key the backend ever stores (favorites.py's
      // build()/find() treat `index` as optional display metadata, never
      // part of the match). Matching on `index` here left every favorite
      // made from a Cuts row or the preview window — both stored with
      // index:null — permanently unstarred in this modal, even though the
      // backend considered them favorited.
      const isFav = !!seg && isFavoritedRange(sourceId, seg.start_seconds, seg.end_seconds);
      setFavStarState(star, isFav);
    });
  }

  // Cuts rows carry no segment identity once inserted (just source_id +
  // in_tc/out_tc), so "is this row favorited" is matched on those three
  // fields — good enough since two distinct favorited lines never share an
  // identical in/out on the same source. Reuses the same cells
  // injectSuiteTimeline's ruler already reads. The star is a real button
  // (addendum v7) — clicking it favorites/unfavorites that row directly,
  // via suite_toggle_favorite_range, even if the row has no matching
  // transcript segment (manually added, edited, or a B-roll clip).
  function refreshCutsRowFavoriteMarkers() {
    const tbody = document.getElementById("editTableBody");
    if (!tbody) return;
    const fps = (typeof rulerFps === "number" && rulerFps > 0) ? rulerFps : 25;
    tbody.querySelectorAll("tr").forEach((tr) => {
      const srcSel = tr.querySelector('[data-field="source_id"]');
      const inInput = tr.querySelector('[data-field="in_tc"]');
      const outInput = tr.querySelector('[data-field="out_tc"]');
      if (!srcSel || !inInput || !outInput) return;
      let btn = tr.querySelector(".suite-cut-fav-btn");
      if (!btn) {
        btn = document.createElement("button");
        btn.type = "button";
        btn.className = "row-btn suite-fav-btn suite-cut-fav-btn";
        btn.dataset.tact = "cut-fav-toggle";
        // Overlaid on the row's Preview thumbnail (thumb-cell), the same
        // corner-badge treatment the preview-window star uses on
        // #previewVideo — not the last (actions) cell, which is a full
        // row away from Preview and easy to miss next to dup/delete.
        const thumbCell = tr.querySelector(".thumb-cell");
        if (thumbCell) thumbCell.appendChild(btn);
      }
      const start = suiteTcToSeconds(inInput.value, fps);
      const end = suiteTcToSeconds(outInput.value, fps);
      const isFav = isFavoritedRange(srcSel.value, start, end);
      setFavStarState(btn, isFav);
    });
  }

  // PERF-5: coalesce bursts of #editTableBody mutations (a full
  // renderEditTable() rebuild fires one childList mutation per row) into
  // ONE star re-scan per animation frame, instead of a full
  // querySelectorAll scan per mutation. requestAnimationFrame rather than
  // a leading-edge timer so the TRAILING state is always the one scanned —
  // the callback runs after the frame's mutations have settled, so stars
  // are never left stale after the burst's last row lands. (rAF pauses
  // while the window is hidden, which is fine here: this drives a visual
  // refresh, not time-based state — it simply runs on next paint.)
  // setFavStarState's write-guard keeps the refresh itself from
  // re-triggering the observer that schedules this.
  let favMarkersRafPending = false;
  function scheduleCutsRowFavoriteMarkers() {
    if (favMarkersRafPending) return;
    favMarkersRafPending = true;
    requestAnimationFrame(() => {
      favMarkersRafPending = false;
      refreshCutsRowFavoriteMarkers();
    });
  }

  // Delegated click handler for the Cuts-row star (wired once at boot).
  // Reads the row's live field values (same cells the marker refresh
  // above reads), converts timecodes to seconds with the existing
  // suiteTcToSeconds helper, and calls the same backend endpoint the
  // preview-window star uses.
  function wireCutsRowFavorites() {
    const tbody = document.getElementById("editTableBody");
    if (!tbody || tbody.dataset.suiteFavWired) return;
    tbody.dataset.suiteFavWired = "1";
    tbody.addEventListener("click", async (e) => {
      const btn = e.target.closest('[data-tact="cut-fav-toggle"]');
      if (!btn) return;
      const tr = btn.closest("tr");
      const srcSel = tr && tr.querySelector('[data-field="source_id"]');
      const inInput = tr && tr.querySelector('[data-field="in_tc"]');
      const outInput = tr && tr.querySelector('[data-field="out_tc"]');
      if (!srcSel || !inInput || !outInput) return;
      const fps = (typeof rulerFps === "number" && rulerFps > 0) ? rulerFps : 25;
      const start = suiteTcToSeconds(inInput.value, fps);
      const end = suiteTcToSeconds(outInput.value, fps);
      if (start == null || end == null) return;
      const textCell = tr.querySelector(".script-text-cell");
      const text = textCell ? textCell.textContent.trim() : "";
      const sourceId = srcSel.value;
      const res = await call("suite_toggle_favorite_range", sourceId, start, end, text);
      if (!res.ok) { toastIfError(res, "Couldn't update that favorite."); return; }
      if (res.favorited) {
        S.favorites.push(res.favorite);
      } else {
        // Same numeric-seconds+tolerance match as isFavoritedRange, not a
        // tc-string compare — this is the local cache staying in sync
        // with what the backend just removed, so it needs the identical
        // key the backend's own favorites.find() used to locate it.
        S.favorites = S.favorites.filter((f) =>
          !(f.source_id === sourceId &&
            Math.abs((f.start_seconds || 0) - start) <= FAV_TOLERANCE_SECONDS &&
            Math.abs((f.end_seconds || 0) - end) <= FAV_TOLERANCE_SECONDS));
      }
      refreshCutsRowFavoriteMarkers();
      refreshTranscriptFavoriteStars();
      refreshPreviewFavoriteStar();
      renderFavoritesPanel();
    });
  }

  // The 5th "Favorites" tab. activateTab() (RCS's own app.js) re-queries
  // .tab/.tab-panel live on every call, so a tab injected anywhere in the
  // DOM works with zero changes there — only the injected button's own
  // click listener has to be wired manually (RCS's once-at-parse-time
  // listener loop never sees a node added after it ran).
  function injectFavoritesTab() {
    const tabs = document.querySelector(".tabs");
    const historyPanel = document.getElementById("tabHistory");
    if (!tabs || !historyPanel || document.getElementById("tabBtnFavorites")) return;

    tabs.insertAdjacentHTML("beforeend",
      `<button class="tab" id="tabBtnFavorites" data-tab="favorites" role="tab" aria-selected="false" aria-controls="tabFavorites">Favorites</button>`);
    historyPanel.insertAdjacentHTML("afterend",
      `<div class="tab-panel" id="tabFavorites" role="tabpanel" aria-labelledby="tabBtnFavorites">
        <div class="suite-fav-list" id="suiteFavList"></div>
      </div>`);

    document.getElementById("tabBtnFavorites").addEventListener("click", () => {
      if (typeof activateTab === "function") activateTab("favorites");
      renderFavoritesPanel();
    });
    document.getElementById("suiteFavList").addEventListener("click", (e) => {
      const addBtn = e.target.closest('[data-tact="fav-add-cuts"]');
      const rmBtn = e.target.closest('[data-tact="fav-remove"]');
      if (addBtn) addFavoriteToCuts(addBtn.dataset.fid);
      else if (rmBtn) removeFavorite(rmBtn.dataset.fid);
    });
  }

  function renderFavoritesPanel() {
    const list = document.getElementById("suiteFavList");
    if (!list) return;
    // kind:"broll" favorites live in the separate B-Roll tab (addendum
    // v15) — excluded here so a segment doesn't show up in both.
    const favs = S.favorites.filter((f) => f.kind !== "broll");
    if (!favs.length) {
      list.innerHTML = `<p class="suite-fav-empty">No favorites yet — star a line in a transcript to see it here.</p>`;
      return;
    }
    const sorted = favs.slice().sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
    list.innerHTML = sorted.map((f) => `
      <div class="suite-fav-card">
        <div class="suite-fav-card__meta">
          <span class="suite-fav-card__source">${esc(f.source_id)}</span>
          <span class="suite-fav-card__tc">${esc(f.start_tc)}–${esc(f.end_tc)}</span>
          ${f.speaker ? `<span class="suite-fav-card__speaker">${esc(f.speaker)}</span>` : ""}
        </div>
        <p class="suite-fav-card__text">${esc(f.text)}</p>
        <div class="suite-fav-card__actions">
          <button class="row-btn" data-tact="fav-add-cuts" data-fid="${esc(f.id)}">+ Add to Cuts</button>
          <button class="row-btn suite-fav-btn is-fav" data-tact="fav-remove" data-fid="${esc(f.id)}" title="Remove from Favorites">★</button>
        </div>
      </div>
    `).join("");
  }

  async function addFavoriteToCuts(favoriteId) {
    const res = await call("suite_favorite_add_to_cuts", favoriteId);
    if (!res.ok) { toastIfError(res, "Couldn't add that favorite to Cuts."); return; }
    const cut = res.cut;

    if (cut.track === "broll") {
      // A favorited B-Roll segment (addendum v15) — same insertion path
      // broll_send_to_edit's own caller uses. ensure_broll_source may have
      // just registered a brand-new source on the backend (a favorite
      // made from the grid, never sent to Cuts before), so RCS's own
      // state.sources needs a refresh before the row can show a valid
      // Source dropdown value — insertBrollCuts itself doesn't do this,
      // it assumes the caller already has.
      await refreshRcsSources();
      const inserted = insertBrollCuts([cut]);
      refreshCutsRowFavoriteMarkers();
      refreshPreviewFavoriteStar();
      toast(inserted ? "Added to the Edit timeline." : "Added — open Cuts to place it.", "ok");
      return;
    }

    if (!rcsEditReady()) {
      toast("Added source — open the Cuts tab in Edit to place it.", "info");
      return;
    }
    const fps = (typeof rulerFps === "number" && rulerFps > 0) ? rulerFps : 25;

    if (typeof flushTcEditSnapshot === "function") flushTcEditSnapshot();
    readEditTableIntoState();
    pushUndoSnapshot();

    // Matches RCS's own transcript-modal "+ Add" shape exactly (track:
    // "main", source_text filled in, no timeline_start_tc — main cuts
    // don't carry one until Apply).
    const seg = {
      track: "main",
      source_id: cut.source_id,
      in_tc: cut.in_tc || suiteSecondsToTc(cut.start_seconds, fps),
      out_tc: cut.out_tc || suiteSecondsToTc(cut.end_seconds, fps),
      note: "",
      on_screen_text: "",
      source_text: cut.source_text || "",
    };
    state.editSegments.push(seg);
    appendCutRow(seg);

    const outputBlock = document.getElementById("outputBlock");
    if (outputBlock) outputBlock.hidden = false;
    if (typeof activateTab === "function") activateTab("edit");

    refreshCutsRowFavoriteMarkers();
    refreshPreviewFavoriteStar();
    scheduleSuiteTimelineRefresh();
    toast("Added to Cuts.", "ok");
  }

  async function removeFavorite(favoriteId) {
    const res = await call("suite_remove_favorite", favoriteId);
    if (!res.ok) { toastIfError(res, "Couldn't remove that favorite."); return; }
    S.favorites = S.favorites.filter((f) => f.id !== favoriteId);
    renderFavoritesPanel();
    refreshTranscriptFavoriteStars();
    refreshCutsRowFavoriteMarkers();
    refreshPreviewFavoriteStar();
    // Generic over both kinds — a no-op for a transcript favorite (the
    // B-Roll tab never held it), correct for a B-Roll tab entry removed
    // from this tab's own ✕ button.
    if (typeof renderBrollFavoritesPanel === "function") renderBrollFavoritesPanel();
  }

  // A 6th "B-Roll" tab (addendum v15), alongside RCS's native
  // Script/Cuts/Export/History and the suite-injected Favorites — lists
  // B-Roll Analyzer segments checked and "sent to Edit" (kind:"broll"
  // entries in the shared favorites store) so they can be reviewed/added
  // to Cuts from the Edit workspace without switching back to the B-Roll
  // workspace. Same injection pattern as injectFavoritesTab (activateTab()
  // live-queries .tab/.tab-panel on every call, so a tab added anywhere in
  // the DOM at any time just works — see CONTRACT.md v6/v15), anchored
  // right after Favorites' own panel to keep tab order Script/Cuts/Export/
  // History/Favorites/B-Roll. Reuses addFavoriteToCuts/removeFavorite
  // verbatim — both are already generic over any favorite id/kind.
  function injectBrollFavoritesTab() {
    const tabs = document.querySelector(".tabs");
    const favPanel = document.getElementById("tabFavorites");
    if (!tabs || !favPanel || document.getElementById("tabBtnBroll")) return;

    tabs.insertAdjacentHTML("beforeend",
      `<button class="tab" id="tabBtnBroll" data-tab="broll" role="tab" aria-selected="false" aria-controls="tabBroll">B-Roll</button>`);
    favPanel.insertAdjacentHTML("afterend",
      `<div class="tab-panel" id="tabBroll" role="tabpanel" aria-labelledby="tabBtnBroll">
        <div class="suite-fav-list suite-broll-fav-grid" id="suiteBrollFavList"></div>
      </div>`);

    document.getElementById("tabBtnBroll").addEventListener("click", () => {
      if (typeof activateTab === "function") activateTab("broll");
      renderBrollFavoritesPanel();
    });
    document.getElementById("suiteBrollFavList").addEventListener("click", (e) => {
      const addBtn = e.target.closest('[data-tact="fav-add-cuts"]');
      const rmBtn = e.target.closest('[data-tact="fav-remove"]');
      const playBtn = e.target.closest(".suite-seg-play");
      const revealBtn = e.target.closest('[data-tact="fav-reveal"]');
      if (addBtn) addFavoriteToCuts(addBtn.dataset.fid);
      else if (rmBtn) removeFavorite(rmBtn.dataset.fid);
      else if (playBtn) { e.preventDefault(); playBrollSegment(playBtn); }
      else if (revealBtn) revealBrollFavoriteMedia(revealBtn.dataset.path);
    });
    // Editorial note (v16): saved on blur/Enter, not per-keystroke — same
    // "commit on change, not on input" behavior as RCS's own Cuts-table
    // free-text fields (note/on-screen-text).
    document.getElementById("suiteBrollFavList").addEventListener("change", (e) => {
      const noteEl = e.target.closest('[data-tact="broll-note"]');
      if (noteEl) saveBrollFavoriteNote(noteEl.dataset.fid, noteEl.value);
    });
  }

  function renderBrollFavoritesPanel() {
    const list = document.getElementById("suiteBrollFavList");
    if (!list) return;
    const favs = S.favorites.filter((f) => f.kind === "broll");
    if (!favs.length) {
      list.innerHTML = `<p class="suite-fav-empty">Nothing sent yet — check some segments in the B-Roll workspace and click "Send to Edit" to see them here.</p>`;
      return;
    }
    const sorted = favs.slice().sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
    list.innerHTML = sorted.map((f) => {
      const st = Number(f.start_seconds) || 0;
      const en = Number(f.end_seconds) || 0;
      const path = f.clip_path || "";
      return `
      <div class="suite-fav-card suite-fav-card--broll suite-clip" data-clip-path="${esc(path)}">
        <div class="suite-fav-card__stage">
          <div class="suite-clip__stage" data-stage>
            <div class="suite-clip__thumb suite-clip__thumb--placeholder" data-thumb-placeholder>no preview</div>
            <div class="suite-clip__preview" data-preview></div>
            ${path ? `<button type="button" class="suite-seg-play suite-seg-play--overlay" data-path="${esc(path)}" data-start="${st}" data-end="${en}"
                    title="Preview ${esc(f.start_tc)}–${esc(f.end_tc)} (loops)" aria-label="Preview ${esc(f.start_tc)} to ${esc(f.end_tc)} of ${esc(basename(path))}">▶</button>` : ""}
          </div>
        </div>
        <div class="suite-fav-card__body">
          <div class="suite-fav-card__meta">
            <span class="suite-fav-card__source" title="${esc(path)}">${esc(basename(path || f.text || ""))}</span>
            <span class="suite-fav-card__tc">${esc(f.start_tc)}–${esc(f.end_tc)}</span>
            ${f.score != null ? `<span class="suite-fav-card__speaker">score ${Math.round(f.score)}</span>` : ""}
          </div>
          <label class="suite-fav-card__notelabel">
            Editorial note
            <textarea class="suite-fav-card__note" data-tact="broll-note" data-fid="${esc(f.id)}"
                      placeholder="Add a note…">${esc(f.note || "")}</textarea>
          </label>
          ${path ? `<button class="row-btn suite-fav-card__link" data-tact="fav-reveal" data-path="${esc(path)}"
                  title="${esc(path)}">🔗 ${esc(basename(path))}</button>` : ""}
          <div class="suite-fav-card__actions">
            <button class="row-btn" data-tact="fav-add-cuts" data-fid="${esc(f.id)}">+ Add to Cuts</button>
            <button class="row-btn" data-tact="fav-remove" data-fid="${esc(f.id)}" title="Remove from the B-Roll tab">✕ Remove</button>
          </div>
        </div>
      </div>
    `;
    }).join("");
    list.querySelectorAll(".suite-fav-card--broll[data-clip-path]").forEach((card) => {
      const path = card.dataset.clipPath;
      if (!path) return;
      const btn = card.querySelector(".suite-seg-play");
      const start = btn ? parseFloat(btn.dataset.start) || 0 : 0;
      enqueueBrollFavThumbnail(card, path, start);
    });
  }

  // Lazy, concurrency-limited thumbnail loader for the B-Roll tab's cards
  // (v16) — mirrors RCS's own enqueueThumbnail/THUMBNAIL_CONCURRENCY idea
  // (app.js) for the Cuts table, kept as its own small queue here since
  // that one is tightly coupled to <tr>/cut-segment rows, not generic.
  const BROLL_FAV_THUMB_CONCURRENCY = 3;
  let brollFavThumbActive = 0;
  const brollFavThumbQueue = [];
  const brollFavThumbCache = new Map(); // "path@start" -> data URI or null (failed)

  function enqueueBrollFavThumbnail(card, path, start) {
    brollFavThumbQueue.push({ card, path, start });
    pumpBrollFavThumbQueue();
  }

  function pumpBrollFavThumbQueue() {
    while (brollFavThumbActive < BROLL_FAV_THUMB_CONCURRENCY && brollFavThumbQueue.length > 0) {
      const { card, path, start } = brollFavThumbQueue.shift();
      brollFavThumbActive++;
      loadBrollFavThumbnail(card, path, start).finally(() => {
        brollFavThumbActive--;
        pumpBrollFavThumbQueue();
      });
    }
  }

  async function loadBrollFavThumbnail(card, path, start) {
    const key = `${path}@${start}`;
    let uri = brollFavThumbCache.get(key);
    if (uri === undefined) {
      let res;
      try {
        res = await call("suite_broll_favorite_thumbnail", path, start);
      } catch (err) {
        res = { ok: false };
      }
      uri = (res && res.ok && res.data_uri) ? res.data_uri : null;
      brollFavThumbCache.set(key, uri);
    }
    if (!uri || !card.isConnected) return;
    const ph = card.querySelector("[data-thumb-placeholder]");
    if (!ph) return;
    const img = document.createElement("img");
    img.className = "suite-clip__thumb";
    img.alt = "";
    img.src = uri;
    ph.replaceWith(img);
  }

  async function revealBrollFavoriteMedia(path) {
    if (!path) return;
    const res = await call("suite_reveal_broll_media", path);
    if (!res.ok) toastIfError(res, "Couldn't reveal that file.");
  }

  async function saveBrollFavoriteNote(favoriteId, note) {
    const fav = S.favorites.find((f) => f.id === favoriteId);
    if (fav) fav.note = note;
    const res = await call("suite_update_favorite_note", favoriteId, note);
    if (!res.ok) toastIfError(res, "Couldn't save that note.");
  }


  // Favorite star in the preview window's header (addendum v7). RCS's own
  // populatePreviewInfo() (app.js) isn't hooked directly — a MutationObserver
  // on #previewSource re-evaluates the star every time RCS itself changes
  // which cut is being previewed (including automatically, mid-queue,
  // during "▶ Preview Script"), since .textContent = replacing its one text
  // node is a childList mutation on that element.
  // The star used to sit in .preview-player__header, next to the "Preview"
  // title and the ✕ close button — visually far from the actual video
  // thumbnail one row below. Wrapping #previewVideo in its own relatively-
  // positioned container lets the star sit as an overlay directly on the
  // thumbnail's corner instead (same "absolute-positioned overlay on a
  // thumbnail" pattern the B-Roll segment cards already use for their own
  // play-button overlay). #previewVideo keeps its id/attributes/listeners
  // — it's MOVED into the wrapper, not cloned, so RCS's own el("previewVideo")
  // lookups are unaffected by the new parent.
  function injectPreviewFavoriteStar() {
    const video = document.getElementById("previewVideo");
    if (!video || document.getElementById("suitePreviewFavBtn")) return;

    let wrap = video.parentElement && video.parentElement.classList.contains("suite-preview-thumb-wrap")
      ? video.parentElement : null;
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.className = "suite-preview-thumb-wrap";
      video.parentElement.insertBefore(wrap, video);
      wrap.appendChild(video);
    }

    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "suitePreviewFavBtn";
    btn.className = "row-btn suite-fav-btn suite-preview-fav-btn";
    btn.title = "Add to Favorites";
    btn.textContent = "☆";
    wrap.appendChild(btn);

    btn.addEventListener("click", async () => {
      const seg = currentPreviewSegment();
      if (!seg) return;
      const fps = (typeof rulerFps === "number" && rulerFps > 0) ? rulerFps : 25;
      const start = suiteTcToSeconds(seg.in_tc, fps);
      const end = suiteTcToSeconds(seg.out_tc, fps);
      if (start == null || end == null) return;
      const res = await call("suite_toggle_favorite_range", seg.source_id, start, end, seg.source_text || "");
      if (!res.ok) { toastIfError(res, "Couldn't update that favorite."); return; }
      if (res.favorited) {
        S.favorites.push(res.favorite);
      } else {
        // Numeric-seconds+tolerance match, same key the backend's own
        // favorites.find() used to locate and remove this entry.
        S.favorites = S.favorites.filter((f) =>
          !(f.source_id === seg.source_id &&
            Math.abs((f.start_seconds || 0) - start) <= FAV_TOLERANCE_SECONDS &&
            Math.abs((f.end_seconds || 0) - end) <= FAV_TOLERANCE_SECONDS));
      }
      refreshPreviewFavoriteStar();
      refreshCutsRowFavoriteMarkers();
      refreshTranscriptFavoriteStars();
      renderFavoritesPanel();
    });

    new MutationObserver(refreshPreviewFavoriteStar)
      .observe(document.getElementById("previewSource"), { childList: true, characterData: true, subtree: true });
  }

  // The Cuts-row currently loaded in the preview player, read LIVE from its
  // own DOM cells — not from the state.editSegments cache. state.editSegments
  // only picks up an in/out edit on blur/change (readEditTableIntoState), so
  // right after typing a new timecode the cache and the live row disagree;
  // in that window the Cuts-row star (which always read the live DOM) and
  // the preview star (which read the stale cache) could show DIFFERENT
  // favorited states for the exact same cut — the "inconsistent between
  // the Cuts page and the preview window" bug. Reading both from the same
  // live source makes them agree by construction.
  function currentPreviewSegment() {
    if (typeof state === "undefined" || !state || state.previewingCid == null) return null;
    const tbody = document.getElementById("editTableBody");
    if (!tbody) return null;
    const tr = Array.from(tbody.querySelectorAll("tr[data-cid]"))
      .find((r) => r.dataset.cid === state.previewingCid);
    if (!tr) return null;
    const srcSel = tr.querySelector('[data-field="source_id"]');
    const inInput = tr.querySelector('[data-field="in_tc"]');
    const outInput = tr.querySelector('[data-field="out_tc"]');
    if (!srcSel || !inInput || !outInput) return null;
    const textCell = tr.querySelector(".script-text-cell");
    return {
      source_id: srcSel.value,
      in_tc: inInput.value,
      out_tc: outInput.value,
      source_text: textCell ? textCell.title : "",
    };
  }

  function refreshPreviewFavoriteStar() {
    const btn = document.getElementById("suitePreviewFavBtn");
    if (!btn) return;
    const seg = currentPreviewSegment();
    if (!seg) {
      setFavStarState(btn, false);
      return;
    }
    const fps = (typeof rulerFps === "number" && rulerFps > 0) ? rulerFps : 25;
    const start = suiteTcToSeconds(seg.in_tc, fps);
    const end = suiteTcToSeconds(seg.out_tc, fps);
    setFavStarState(btn, isFavoritedRange(seg.source_id, start, end));
  }

  // ============================================================
  // Edit workspace — suite timeline panel (#suiteTimeline, addendum D)
  //
  // Collapsible ~150px panel at the bottom of #workspace-edit. V1 lane =
  // main cuts laid end-to-end; b-roll cuts are greedily packed into V2+
  // lanes at their timeline_start_tc. B-roll blocks drag horizontally
  // (frame-snapped at rulerFps, clamped ≥ 0); a drop runs RCS's mandated
  // mutation order and updates only that row's timeline-start input. Main
  // blocks aren't draggable — their order lives in the Cuts table.
  // ============================================================

  let tlDrag = null;         // active b-roll block drag
  let tlTrim = null;         // active in/out edge-handle drag (any block)
  let tlRefreshTimer = null; // throttle handle (≥500ms between renders)
  let tlLastRefresh = 0;

  function tlFps() {
    return (typeof rulerFps === "number" && rulerFps > 0) ? rulerFps : 25;
  }

  function scheduleSuiteTimelineRefresh() {
    if (tlRefreshTimer) return;
    const since = Date.now() - tlLastRefresh;
    tlRefreshTimer = setTimeout(() => {
      tlRefreshTimer = null;
      renderSuiteTimeline();
    }, Math.max(50, 500 - since));
  }

  // Make the transcript modal's segments searchable. RCS's own
  // viewTranscript() (app.js) only ever rewrites #transcriptModalBody's
  // innerHTML — it never touches .modal__header/.modal__body themselves —
  // so a search bar inserted between them, once, at boot, survives every
  // future open/reload of the modal untouched. A MutationObserver on the
  // tbody (not a hook into viewTranscript, which we must not modify) resets
  // the filter whenever RCS repopulates it, so switching to a DIFFERENT
  // transcript never leaves a stale filter hiding all of its rows.
  function injectTranscriptSearch() {
    const tbody = document.getElementById("transcriptModalBody");
    const modalBody = tbody && tbody.closest(".modal__body");
    if (!tbody || !modalBody || document.getElementById("suiteTranscriptSearch")) return;

    const bar = document.createElement("div");
    bar.className = "suite-transcript-search";
    bar.innerHTML =
      `<input type="text" id="suiteTranscriptSearch" placeholder="Search transcript…" aria-label="Search transcript" />` +
      `<span class="suite-transcript-search__count" id="suiteTranscriptSearchCount"></span>`;
    modalBody.parentElement.insertBefore(bar, modalBody);

    const input = bar.querySelector("#suiteTranscriptSearch");
    const countEl = bar.querySelector("#suiteTranscriptSearchCount");

    function applyTranscriptFilter() {
      const q = input.value.trim().toLowerCase();
      let shown = 0, total = 0;
      tbody.querySelectorAll("tr").forEach((tr) => {
        total++;
        const matches = !q || tr.textContent.toLowerCase().includes(q);
        tr.classList.toggle("suite-row-hidden", !matches);
        if (matches) shown++;
      });
      countEl.textContent = q ? `${shown} / ${total}` : "";
    }
    input.addEventListener("input", applyTranscriptFilter);

    // viewTranscript() repopulates the tbody via body.innerHTML="" then
    // appendChild — a childList mutation, not an attribute change, so this
    // never re-fires from applyTranscriptFilter()'s own classList.toggle
    // calls above.
    const observer = new MutationObserver(() => {
      input.value = "";
      applyTranscriptFilter();
    });
    observer.observe(tbody, { childList: true });
  }

  function injectSuiteTimeline() {
    const ws = $("workspace-edit");
    if (!ws || $("suiteTimeline")) return;
    const panel = document.createElement("section");
    panel.id = "suiteTimeline";
    panel.setAttribute("aria-label", "Suite timeline");
    panel.innerHTML = `
      <div class="suite-tl-head" id="suiteTlHead" role="button" tabindex="0" aria-expanded="true" title="Collapse or expand the timeline">
        <span class="suite-tl-chevron">▾</span>
        <span class="suite-tl-title">Timeline</span>
        <span class="suite-tl-meta" id="suiteTlMeta"></span>
      </div>
      <div class="suite-tl-body" id="suiteTlBody">
        <div class="suite-tl-ruler" id="suiteTlRuler"></div>
        <div class="suite-tl-lanes" id="suiteTlLanes"></div>
        <div class="suite-tl-note" id="suiteTlNote" hidden></div>
      </div>`;
    ws.appendChild(panel);

    const head = panel.querySelector("#suiteTlHead");
    const toggle = () => {
      S.tlCollapsed = !S.tlCollapsed;
      panel.classList.toggle("is-collapsed", S.tlCollapsed);
      head.setAttribute("aria-expanded", S.tlCollapsed ? "false" : "true");
      head.querySelector(".suite-tl-chevron").textContent = S.tlCollapsed ? "▸" : "▾";
      if (!S.tlCollapsed) renderSuiteTimeline();
    };
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });

    wireSuiteTimelineTrim(panel);
    wireSuiteTimelineDrag(panel);

    // RCS-side edits (undo, Apply, reorder, add/delete rows) re-render the
    // timeline via a throttled MutationObserver on #editTableBody. Plain
    // value edits don't mutate the DOM, so also listen for committed input
    // changes bubbling out of the table — this is also the only signal
    // that an in/out timecode was just retyped, so it must refresh the
    // favorite stars too (a stale star was the other half of the
    // inconsistent-highlighting bug: editing in/out on a favorited row
    // left its star lit until some UNRELATED childList mutation happened
    // to re-scan it).
    const tbody = document.getElementById("editTableBody");
    if (tbody) {
      new MutationObserver(() => { scheduleSuiteTimelineRefresh(); scheduleCutsRowFavoriteMarkers(); })
        .observe(tbody, { childList: true, subtree: true });
      tbody.addEventListener("change", () => {
        scheduleSuiteTimelineRefresh();
        scheduleCutsRowFavoriteMarkers();
        refreshPreviewFavoriteStar();
      });
    }
    window.addEventListener("resize", () => scheduleSuiteTimelineRefresh());
    window.addEventListener("resize", scheduleSyncWaveformRedraw);
  }

  // Narrows the "B-Roll Audio" column (addendum v7). RCS's own index.html
  // ALREADY ships a <colgroup> with explicit per-column pixel widths (a
  // plain th/td:nth-child CSS width rule was tried first and silently did
  // nothing — a <colgroup> is authoritative over per-cell widths under
  // table-layout:fixed, so RCS's own colgroup was winning every time).
  // Since the <col> elements are static markup present from initial page
  // load, this needs no measurement/visibility timing at all — just edit
  // the existing inline widths directly, once, at boot.
  function fixEditTableColumnWidths() {
    const cols = document.querySelectorAll(".edit-table colgroup col");
    if (cols.length < 11 || cols[8].dataset.suiteNarrowed) return;
    cols[8].dataset.suiteNarrowed = "1"; // idempotency guard
    const AUDIO_COL = 8, TEXT_COL = 9, NOTE_COL = 10; // 0-based
    cols[AUDIO_COL].style.width = "92px"; // was 150px — see suite.css's own comment on the select's width
    // "Editorial note" (col 10) is the ONE <col> RCS's own index.html ships
    // with no width at all — under table-layout:fixed, the column with no
    // explicit width absorbs 100% of whatever's left over after every
    // other column's fixed width is subtracted, which is why it renders
    // far wider than a short annotation field needs. Cap it to a sane
    // size and make Script Text (the column that actually benefits from
    // extra room) the new unconstrained absorber in its place, by
    // clearing ITS width instead — same "one column stays flexible"
    // scheme RCS's own colgroup already relied on, just relocated to the
    // column that should have it.
    cols[NOTE_COL].style.width = "200px";
    cols[TEXT_COL].style.width = "";
  }

  // ============================================================
  // Cuts table: user-adjustable column widths. Drag handles on the
  // header's column borders resize the SAME <colgroup> <col> elements
  // fixEditTableColumnWidths edits, and persist per-column widths in
  // localStorage so they survive relaunches (a per-USER display
  // preference, not project data — never written into a project file or
  // any backend store). Handles only exist between columns 2 ("Preview")
  // and 11 ("On-screen text"): the checkbox/spacer columns (0, 1) and the
  // tiny actions column (12) aren't worth resizing, and letting actions
  // shrink further would make its icon buttons unusable.
  // ============================================================

  const COL_RESIZE_MIN = 40;    // px — never let a column shrink to unusable
  const COL_RESIZE_FIRST = 2;   // "Preview" — first column with a right-edge handle
  const COL_RESIZE_LAST = 11;   // "On-screen text" — last column with a right-edge handle
  const COL_RESIZE_STORAGE_KEY = "suiteEditTableColWidths.v1";

  // cols[i]'s current width in px — from its own inline style if it has
  // one, else measured from its live rendered header-cell width (true
  // for exactly one column at a time: whichever fixEditTableColumnWidths
  // left unconstrained, currently Script Text). A drag freezes whatever
  // it measures into an explicit width (see injectColumnResizeHandles),
  // so this fallback only ever matters for that column's FIRST drag.
  function getColWidthPx(cols, i) {
    const w = parseFloat(cols[i].style.width);
    if (!isNaN(w)) return w;
    const th = document.querySelectorAll(".edit-table thead th")[i];
    return th ? th.getBoundingClientRect().width : 100;
  }

  function saveColWidths(cols) {
    try {
      const widths = Array.from(cols).map((c) => parseFloat(c.style.width) || null);
      localStorage.setItem(COL_RESIZE_STORAGE_KEY, JSON.stringify(widths));
    } catch (e) {
      // Private-browsing quota or localStorage disabled — resizing still
      // works for the rest of this session, it just won't persist.
    }
  }

  // Applied once at boot, AFTER fixEditTableColumnWidths has set its own
  // defaults — a saved width (the user's own deliberate resize) always
  // wins over those defaults. Length-checked against the CURRENT column
  // count so a stale save from a different table shape is never applied.
  function loadSavedColWidths(cols) {
    let saved;
    try {
      saved = JSON.parse(localStorage.getItem(COL_RESIZE_STORAGE_KEY) || "null");
    } catch (e) {
      return;
    }
    if (!Array.isArray(saved) || saved.length !== cols.length) return;
    saved.forEach((w, i) => {
      if (typeof w === "number" && w > 0) cols[i].style.width = w + "px";
    });
  }

  function injectColumnResizeHandles() {
    const headerRow = document.querySelector(".edit-table thead tr");
    const cols = document.querySelectorAll(".edit-table colgroup col");
    if (!headerRow || cols.length < 13 || headerRow.dataset.suiteResizeWired) return;
    headerRow.dataset.suiteResizeWired = "1";

    loadSavedColWidths(cols);

    const ths = headerRow.querySelectorAll("th");
    for (let i = COL_RESIZE_FIRST; i <= COL_RESIZE_LAST; i++) {
      const th = ths[i];
      if (!th) continue;
      const handle = document.createElement("div");
      handle.className = "suite-col-resize-handle";
      th.appendChild(handle);

      handle.addEventListener("mousedown", (e) => {
        e.preventDefault();
        e.stopPropagation(); // don't let RCS's own header click handlers (if any) see this
        const startX = e.clientX;
        const leftIdx = i, rightIdx = i + 1;
        const leftStart = getColWidthPx(cols, leftIdx);
        const rightStart = getColWidthPx(cols, rightIdx);
        // Freeze BOTH sides to explicit pixel widths up front — this is
        // what lets a currently-unconstrained column (Script Text)
        // participate in a drag at all. From this point on it behaves
        // like any other fixed column; fixEditTableColumnWidths's own
        // guard only ever runs once at first boot, so this never fights
        // with it.
        cols[leftIdx].style.width = leftStart + "px";
        cols[rightIdx].style.width = rightStart + "px";

        document.body.classList.add("suite-col-resizing");
        handle.classList.add("is-dragging");

        function onMove(ev) {
          const rawDelta = ev.clientX - startX;
          // Clamp so NEITHER side can go below the minimum — this keeps
          // their SUM exactly constant, so the table's overall width
          // never changes regardless of which pair of columns trades
          // space (a plain "borrow from the neighbor" resize, the same
          // scheme spreadsheet/data-grid column resizers use).
          const delta = Math.max(
            COL_RESIZE_MIN - leftStart,
            Math.min(rightStart - COL_RESIZE_MIN, rawDelta)
          );
          cols[leftIdx].style.width = (leftStart + delta) + "px";
          cols[rightIdx].style.width = (rightStart - delta) + "px";
        }
        function onUp() {
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
          document.body.classList.remove("suite-col-resizing");
          handle.classList.remove("is-dragging");
          saveColWidths(cols);
        }
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      });
    }
  }

  // Moves #durationMeter (the "Runtime: …" pill RCS fills in via
  // renderDurationMeter) to be a literal DOM sibling of the Target
  // Duration input, inside the same .field--inline label — so it always
  // renders immediately next to it, in the same flex row, instead of in
  // a separate grid column clear across the panel from it (see suite.css
  // Creative Brief comment). renderDurationMeter() only ever looks the
  // element up by id and sets hidden/className/textContent, so moving it
  // in the DOM has no effect on RCS's own code. Idempotent (checked via
  // parentElement) so re-running it after a re-render is a no-op.
  function relocateRuntimeMeter() {
    const meter = document.getElementById("durationMeter");
    const fieldRow = document.querySelector("#workspace-edit .block--compact > .field--inline");
    if (!meter || !fieldRow || meter.parentElement === fieldRow) return;
    fieldRow.appendChild(meter);
  }

  // ---------------- Rough Cut Studio: persisted settings (v18) -----------
  //
  // Frame rate, drop-frame, LLM provider/model, and the Ollama host reset
  // to their hardcoded index.html defaults on every launch UNLESS a
  // project is loaded/restored — a per-USER preference, not project data
  // (RCS's own new_project/load_project/restore_autosave already own
  // resetting/restoring these for their own cases; this only fills in
  // the gap for a plain fresh launch with nothing to load). Mirrors
  // RCS's own load_project handler's restoration logic (app.js) exactly,
  // since that's already a proven path for setting provider/model/
  // ollamaHost/llamaModel/fps/dropFrame correctly together — just
  // replaying it with a saved preference instead of a loaded project's
  // fields. Reaches into RCS's DOM/globals (updateProviderVisibility,
  // updateDropFrameVisibility, refreshLlamaModels, rulerFps) the same way
  // every other suite.js integration point already does; RCS's own files
  // are never modified.
  const RCS_SETTINGS_STORAGE_KEY = "suiteRcsSettings.v1";
  const RCS_SETTINGS_FIELD_IDS = ["fps", "provider", "model", "ollamaHost", "llamaModel"];

  function saveRcsSettings() {
    try {
      const values = {};
      RCS_SETTINGS_FIELD_IDS.forEach((id) => {
        const el = document.getElementById(id);
        if (el) values[id] = el.value;
      });
      const dropFrameEl = document.getElementById("dropFrame");
      values.dropFrame = dropFrameEl ? dropFrameEl.checked : false;
      localStorage.setItem(RCS_SETTINGS_STORAGE_KEY, JSON.stringify(values));
    } catch (e) {
      // localStorage disabled/full — settings just won't persist this run.
    }
  }

  function restoreRcsSettings() {
    let saved;
    try {
      saved = JSON.parse(localStorage.getItem(RCS_SETTINGS_STORAGE_KEY) || "null");
    } catch (e) {
      return;
    }
    if (!saved || typeof saved !== "object") return;
    const fpsEl = document.getElementById("fps");
    const providerEl = document.getElementById("provider");
    const modelEl = document.getElementById("model");
    const ollamaHostEl = document.getElementById("ollamaHost");
    const dropFrameEl = document.getElementById("dropFrame");
    if (!fpsEl || !providerEl) return; // RCS hook missing — degrade quietly, like assertRcsHooks

    if (saved.fps) {
      fpsEl.value = saved.fps;
      const parsed = parseFloat(saved.fps);
      if (typeof rulerFps !== "undefined" && !isNaN(parsed)) rulerFps = parsed;
    }
    const dfAvailable = saved.fps === "29.97" || saved.fps === "59.94";
    if (typeof updateDropFrameVisibility === "function") updateDropFrameVisibility(dfAvailable);
    if (dropFrameEl) dropFrameEl.checked = dfAvailable && !!saved.dropFrame;

    if (saved.provider) providerEl.value = saved.provider;
    if (typeof updateProviderVisibility === "function") updateProviderVisibility();
    if (ollamaHostEl && saved.ollamaHost) ollamaHostEl.value = saved.ollamaHost;

    if (saved.provider === "llama") {
      if (typeof refreshLlamaModels === "function") {
        refreshLlamaModels({ silent: true }).then(() => {
          const llamaModelEl = document.getElementById("llamaModel");
          if (!llamaModelEl || !saved.llamaModel) return;
          if (typeof state !== "undefined" && Array.isArray(state.llamaModels) &&
              !state.llamaModels.includes(saved.llamaModel)) {
            const opt = document.createElement("option");
            opt.value = saved.llamaModel;
            opt.textContent = `${saved.llamaModel} (not pulled — run \`ollama pull ${saved.llamaModel}\`)`;
            llamaModelEl.appendChild(opt);
          }
          llamaModelEl.value = saved.llamaModel;
        });
      }
    } else if (saved.model && modelEl) {
      modelEl.value = saved.model;
    }
  }

  function wireRcsSettingsPersistence() {
    RCS_SETTINGS_FIELD_IDS.forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("change", saveRcsSettings);
    });
    const dropFrameEl = document.getElementById("dropFrame");
    if (dropFrameEl) dropFrameEl.addEventListener("change", saveRcsSettings);
  }

  // RCS's own "Remember key on this machine (stored in plain text in a
  // local .env file)" checkbox is stale inside the suite: api_security.py's
  // SecurityMixin already overrides save_api_key_to_disk to write to the
  // system keychain instead (never plaintext), so the opt-in/disclosure the
  // checkbox offers no longer describes what actually happens. RCS's own
  // app.js still gates the save call on `el("rememberKey").checked`, so
  // rather than touching RCS's files (which would also change the
  // plaintext-.env behavior standalone RCS still correctly uses), just hide
  // the control here and leave it permanently checked — the save call fires
  // unchanged, only now it always lands in the keychain.
  function suppressRememberKeyCheckbox() {
    const cb = document.getElementById("rememberKey");
    if (!cb) return;
    const label = cb.closest("label.checkbox");
    if (label) label.hidden = true;
    cb.checked = true;
  }

  // Relocates RCS's own "04 -- LLM Provider" block (frontend/index.html) out
  // of the Edit workspace sidebar and into Suite Settings' Edit pane. This
  // moves the actual composed DOM node (appendChild), never RCS's own
  // index.html -- every id inside it (provider/apiKey/model/ollamaHost/
  // llamaModel/etc.) is unchanged, so RCS's own app.js, restoreRcsSettings()/
  // wireRcsSettingsPersistence() above, and suppressRememberKeyCheckbox()
  // all keep working from wherever the node now lives in the DOM.
  function relocateLlmProviderSettings() {
    const providerEl = document.getElementById("provider");
    const pane = $("settingsPaneEdit");
    if (!providerEl || !pane) return; // RCS hook missing -- degrade quietly, like assertRcsHooks
    const section = providerEl.closest("section.block");
    if (!section) return;
    const title = section.querySelector(".block__title");
    if (title) title.textContent = "LLM Provider"; // drop the "04 --" sidebar numbering, orphaned once moved out
    pane.appendChild(section);
  }

  // ---------------- Sources panel: hide B-Roll synthetic sources (v16) ---
  //
  // Favoriting/sending a B-Roll Analyzer segment registers its clip as a
  // REAL RCS source (handoff.ensure_broll_source), named "<clip stem> —
  // broll" so it can carry timecodes/media-link like any other source —
  // but it was never meant to clutter the Sources list a user builds by
  // adding their own transcripts. renderSources() (app.js) can't be
  // touched, so this hides the matching <li> elements after every render
  // via a MutationObserver on #sourceList — same idiom as every other
  // "reach into RCS's DOM after RCS's own JS rebuilds it" feature here.
  const BROLL_SOURCE_NAME_MARKER = " — broll";

  function isBrollSourceName(name) {
    return typeof name === "string" && name.includes(BROLL_SOURCE_NAME_MARKER);
  }

  function hideBrollSourceItems() {
    const list = document.getElementById("sourceList");
    if (!list) return;
    let anyVisible = false;
    list.querySelectorAll("li.source-item").forEach((li) => {
      const nameEl = li.querySelector(".source-item__name");
      const isBroll = isBrollSourceName(nameEl && nameEl.textContent);
      li.style.display = isBroll ? "none" : "";
      if (!isBroll) anyVisible = true;
    });
    // If EVERY real source got hidden, RCS's own "No transcripts added
    // yet." hint never renders (it only appears when state.sources is
    // completely empty) — inject an equivalent hint so the panel doesn't
    // just look broken/blank. Removed again the moment a non-B-roll
    // source is visible, or if RCS's own hint is present (nothing to add
    // to, ever — that only shows when state.sources is truly empty).
    const nativeHint = list.querySelector("li.block__hint:not(.suite-broll-hint)");
    let ownHint = list.querySelector("li.suite-broll-hint");
    if (!anyVisible && !nativeHint) {
      if (!ownHint) {
        ownHint = document.createElement("li");
        ownHint.className = "block__hint suite-broll-hint";
        ownHint.textContent = "No transcripts added yet.";
        list.appendChild(ownHint);
      }
    } else if (ownHint) {
      ownHint.remove();
    }
  }

  function injectBrollSourceFilter() {
    const list = document.getElementById("sourceList");
    if (!list || list.dataset.suiteBrollFilterWired) return;
    list.dataset.suiteBrollFilterWired = "1";
    new MutationObserver(hideBrollSourceItems).observe(list, { childList: true });
    hideBrollSourceItems();
  }

  function renderSuiteTimeline() {
    tlLastRefresh = Date.now();
    const panel = $("suiteTimeline");
    if (!panel || S.tlCollapsed || tlDrag) return;
    const lanesEl = $("suiteTlLanes");
    const rulerEl = $("suiteTlRuler");
    const noteEl = $("suiteTlNote");
    const metaEl = $("suiteTlMeta");
    const tbody = document.getElementById("editTableBody");

    if (!rcsEditReady() || !tbody) {
      // Graceful degradation when the RCS globals are missing (addendum D).
      noteEl.hidden = false;
      noteEl.textContent = "Timeline unavailable — Rough Cut Studio's editor didn't finish loading.";
      lanesEl.innerHTML = "";
      rulerEl.innerHTML = "";
      metaEl.textContent = "";
      return;
    }
    noteEl.hidden = true;
    const fps = tlFps();
    readEditTableIntoState(); // pick up pending DOM edits first (addendum D)

    const main = [];
    const broll = [];
    state.editSegments.forEach((seg) => {
      if (typeof newCid === "function" && !seg._cid) seg._cid = newCid();
      const i = suiteTcToSeconds(seg.in_tc, fps);
      const o = suiteTcToSeconds(seg.out_tc, fps);
      const dur = (i != null && o != null && o > i) ? (o - i) : 0;
      if (seg.track === "broll") {
        const st = suiteTcToSeconds(seg.timeline_start_tc, fps);
        broll.push({ seg, start: st == null ? 0 : st, dur: Math.max(dur, 1 / fps) });
      } else {
        main.push({ seg, dur: Math.max(dur, 1 / fps) });
      }
    });

    if (main.length === 0 && broll.length === 0) {
      lanesEl.innerHTML = `<div class="suite-tl-empty">No cuts yet — generate a script, add cuts, or send b-roll here.</div>`;
      rulerEl.innerHTML = "";
      metaEl.textContent = "";
      return;
    }

    // V1: main cuts end-to-end in table order
    let cursor = 0;
    main.forEach((b) => { b.start = cursor; cursor += b.dur; });
    const mainEnd = cursor;

    // b-roll: greedy interval scheduling into as few lanes as possible
    broll.sort((a, b) => (a.start - b.start) || (a.dur - b.dur));
    const laneEnds = [];
    broll.forEach((b) => {
      let lane = laneEnds.findIndex((end) => end <= b.start + 1e-6);
      if (lane === -1) { lane = laneEnds.length; laneEnds.push(0); }
      laneEnds[lane] = b.start + b.dur;
      b.lane = lane;
    });

    const total = Math.max(mainEnd, ...broll.map((b) => b.start + b.dur), 1);
    panel.dataset.total = String(total);

    // ruler ticks at a "nice" interval (~≤12 labels)
    const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
    const step = steps.find((s) => total / s <= 12) || 600;
    let ticks = "";
    for (let t = 0; t <= total + 1e-6; t += step) {
      ticks += `<span class="suite-tl-tick" style="left:${(t / total) * 100}%">${mmss(t)}</span>`;
    }
    rulerEl.innerHTML = ticks;

    const blockHtml = (b, cls, draggable) => {
      const name = b.seg.source_id || "cut";
      const cid = esc(b.seg._cid || "");
      const dragHint = draggable ? " — drag to retime, drag an edge to trim" : " — drag an edge to trim";
      return `<div class="suite-tl-block ${cls}" data-cid="${cid}"${draggable ? ' data-draggable="1"' : ""}
        style="left:${(b.start / total) * 100}%;width:${Math.max(0.4, (b.dur / total) * 100)}%"
        title="${esc(name)} · ${b.dur.toFixed(1)}s${dragHint}, click to reveal in the Cuts table">
        <div class="suite-tl-handle suite-tl-handle--in" data-cid="${cid}" data-edge="in" title="Drag to adjust the in point"></div>
        <span class="suite-tl-block__name">${esc(name)}</span><span class="suite-tl-block__dur">${b.dur.toFixed(1)}s</span>
        <div class="suite-tl-handle suite-tl-handle--out" data-cid="${cid}" data-edge="out" title="Drag to adjust the out point"></div>
      </div>`;
    };

    let html = "";
    for (let l = laneEnds.length - 1; l >= 0; l--) {
      html += `<div class="suite-tl-lane suite-tl-lane--broll">
        <span class="suite-tl-lane__label">V${l + 2}</span>
        <div class="suite-tl-lane__track">${broll.filter((b) => b.lane === l).map((b) => blockHtml(b, "suite-tl-block--broll", true)).join("")}</div>
      </div>`;
    }
    html += `<div class="suite-tl-lane suite-tl-lane--main">
      <span class="suite-tl-lane__label">V1</span>
      <div class="suite-tl-lane__track">${main.map((b) => blockHtml(b, "suite-tl-block--main", false)).join("")}</div>
    </div>`;
    lanesEl.innerHTML = html;
    metaEl.textContent = `${main.length} main · ${broll.length} b-roll · ${mmss(total)}`;
  }

  // Scroll the cut's row into view in the Cuts table and flash it.
  function flashCutsRow(cid) {
    if (!cid) return;
    const outputBlock = document.getElementById("outputBlock");
    if (outputBlock) outputBlock.hidden = false;
    if (typeof activateTab === "function") activateTab("edit");
    const tr = document.querySelector(`#editTableBody tr[data-cid="${cid}"]`);
    if (!tr) return;
    tr.scrollIntoView({ behavior: "smooth", block: "center" });
    tr.classList.remove("suite-row-flash");
    void tr.offsetWidth; // restart the animation if it was already flashing
    tr.classList.add("suite-row-flash");
    setTimeout(() => tr.classList.remove("suite-row-flash"), 1800);
  }

  // Drop → RCS's mandated order: flushTcEditSnapshot(), readEditTableIntoState(),
  // pushUndoSnapshot(), mutate the cut, update ONLY that row's timeline-start
  // input (no full renderEditTable), refreshBrollOverlapWarnings().
  // SMPTE formatting is local non-drop-frame; drop-frame projects (the RCS
  // #dropFrame checkbox) defer to the authoritative backend format_timecode.
  async function commitTimelineDrag(cid, newStartSeconds) {
    if (!rcsEditReady()) { renderSuiteTimeline(); return; }
    const fps = tlFps();

    let tc = null;
    const df = document.getElementById("dropFrame");
    if (df && df.checked) {
      const res = await call("format_timecode", newStartSeconds);
      if (res.ok && res.tc) tc = res.tc;
    }
    if (!tc) tc = suiteSecondsToTc(newStartSeconds, fps);

    if (typeof flushTcEditSnapshot === "function") flushTcEditSnapshot();
    readEditTableIntoState();
    pushUndoSnapshot();
    const seg = state.editSegments.find((s) => s._cid === cid);
    if (!seg) { renderSuiteTimeline(); return; }
    seg.timeline_start_tc = tc;
    const input = document.querySelector(
      `#editTableBody tr[data-cid="${cid}"] .tc-input[data-field="timeline_start_tc"]`);
    if (input) input.value = tc;
    if (typeof refreshBrollOverlapWarnings === "function") refreshBrollOverlapWarnings();
    renderSuiteTimeline(); // refresh after our own drag (addendum D)
  }

  // Same RCS-mandated mutation order as commitTimelineDrag, but for a genuine
  // retime (in_tc/out_tc) rather than a reposition (timeline_start_tc) — so
  // this also refreshes B-roll overlap warnings, since trimming either edge
  // changes a cut's duration and can change its overlap membership.
  async function commitTimelineTrim(cid, edge, newSeconds) {
    if (!rcsEditReady()) { renderSuiteTimeline(); return; }
    const fps = tlFps();

    let tc = null;
    const df = document.getElementById("dropFrame");
    if (df && df.checked) {
      const res = await call("format_timecode", newSeconds);
      if (res.ok && res.tc) tc = res.tc;
    }
    if (!tc) tc = suiteSecondsToTc(newSeconds, fps);

    if (typeof flushTcEditSnapshot === "function") flushTcEditSnapshot();
    readEditTableIntoState();
    pushUndoSnapshot();
    const seg = state.editSegments.find((s) => s._cid === cid);
    if (!seg) { renderSuiteTimeline(); return; }
    const field = edge === "in" ? "in_tc" : "out_tc";
    seg[field] = tc;
    const input = document.querySelector(
      `#editTableBody tr[data-cid="${cid}"] .tc-input[data-field="${field}"]`);
    if (input) input.value = tc;
    if (typeof refreshBrollOverlapWarnings === "function") refreshBrollOverlapWarnings();
    renderSuiteTimeline();
  }

  // Left/right edge handles on every block (main and b-roll alike) trim
  // in_tc/out_tc — distinct from wireSuiteTimelineDrag's whole-block
  // reposition (b-roll only, mutates timeline_start_tc). Wired first and
  // uses stopImmediatePropagation so a handle press never ALSO triggers the
  // whole-block drag on a b-roll block whose ancestor carries
  // data-draggable="1" (e.'target.closest would otherwise match the block
  // itself, since a handle is nested inside it).
  function wireSuiteTimelineTrim(panel) {
    const lanesEl = panel.querySelector("#suiteTlLanes");

    lanesEl.addEventListener("pointerdown", (e) => {
      const handle = e.target.closest(".suite-tl-handle");
      if (!handle) return;
      e.stopImmediatePropagation();
      e.preventDefault();
      const block = handle.closest(".suite-tl-block");
      const track = block.parentElement;
      const total = parseFloat(panel.dataset.total) || 1;
      const rect = track.getBoundingClientRect();
      const fps = tlFps();
      const seg = (typeof state !== "undefined" && Array.isArray(state.editSegments))
        ? state.editSegments.find((s) => s._cid === handle.dataset.cid) : null;
      if (!seg) return;
      const inSeconds = suiteTcToSeconds(seg.in_tc, fps);
      const outSeconds = suiteTcToSeconds(seg.out_tc, fps);
      if (inSeconds == null || outSeconds == null) return; // nothing sane to trim from
      tlTrim = {
        block, handle, cid: handle.dataset.cid, edge: handle.dataset.edge,
        startX: e.clientX,
        pxPerSec: Math.max(1e-6, rect.width / total),
        total, inSeconds, outSeconds, minDur: 1 / fps,
        startLeftPct: parseFloat(block.style.left) || 0,
        startWidthPct: parseFloat(block.style.width) || 0,
        curSeconds: null, moved: false,
      };
      try { handle.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
      handle.classList.add("is-dragging");
    });

    lanesEl.addEventListener("pointermove", (e) => {
      if (!tlTrim) return;
      const dx = e.clientX - tlTrim.startX;
      if (Math.abs(dx) > 3) tlTrim.moved = true;
      if (!tlTrim.moved) return;
      const fps = tlFps();
      const deltaSeconds = Math.round((dx / tlTrim.pxPerSec) * fps) / fps; // snap to whole frames
      if (tlTrim.edge === "in") {
        const newIn = Math.max(0, Math.min(tlTrim.outSeconds - tlTrim.minDur, tlTrim.inSeconds + deltaSeconds));
        tlTrim.curSeconds = newIn;
        const shiftPct = ((newIn - tlTrim.inSeconds) / tlTrim.total) * 100;
        tlTrim.block.style.left = `${tlTrim.startLeftPct + shiftPct}%`;
        tlTrim.block.style.width = `${Math.max(0.4, tlTrim.startWidthPct - shiftPct)}%`;
      } else {
        const newOut = Math.max(tlTrim.inSeconds + tlTrim.minDur, tlTrim.outSeconds + deltaSeconds);
        tlTrim.curSeconds = newOut;
        const growthPct = ((newOut - tlTrim.outSeconds) / tlTrim.total) * 100;
        tlTrim.block.style.width = `${Math.max(0.4, tlTrim.startWidthPct + growthPct)}%`;
      }
    });

    lanesEl.addEventListener("pointerup", (e) => {
      if (!tlTrim) return;
      const t = tlTrim;
      tlTrim = null;
      t.handle.classList.remove("is-dragging");
      try { t.handle.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
      if (t.moved && t.curSeconds != null) commitTimelineTrim(t.cid, t.edge, t.curSeconds);
      else renderSuiteTimeline(); // a no-move press snaps back to the committed value
    });

    lanesEl.addEventListener("pointercancel", () => {
      if (!tlTrim) return;
      tlTrim = null;
      renderSuiteTimeline(); // discard the aborted trim
    });
  }

  function wireSuiteTimelineDrag(panel) {
    const lanesEl = panel.querySelector("#suiteTlLanes");

    lanesEl.addEventListener("pointerdown", (e) => {
      const block = e.target.closest('.suite-tl-block[data-draggable="1"]');
      if (!block) return;
      const track = block.parentElement;
      const total = parseFloat(panel.dataset.total) || 1;
      const rect = track.getBoundingClientRect();
      const startLeftPct = parseFloat(block.style.left) || 0;
      tlDrag = {
        block,
        cid: block.dataset.cid,
        startX: e.clientX,
        pxPerSec: Math.max(1e-6, rect.width / total),
        total,
        startSeconds: (startLeftPct / 100) * total,
        curSeconds: null,
        moved: false,
      };
      try { block.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
      block.classList.add("is-dragging");
      e.preventDefault();
    });

    lanesEl.addEventListener("pointermove", (e) => {
      if (!tlDrag) return;
      const dx = e.clientX - tlDrag.startX;
      if (Math.abs(dx) > 3) tlDrag.moved = true;
      if (!tlDrag.moved) return;
      const fps = tlFps();
      let s = tlDrag.startSeconds + dx / tlDrag.pxPerSec;
      s = Math.max(0, Math.round(s * fps) / fps); // snap to whole frames, clamp ≥ 0
      tlDrag.curSeconds = s;
      tlDrag.block.style.left = `${(s / tlDrag.total) * 100}%`;
    });

    lanesEl.addEventListener("pointerup", (e) => {
      if (!tlDrag) return;
      const d = tlDrag;
      tlDrag = null;
      d.block.classList.remove("is-dragging");
      try { d.block.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
      if (d.moved && d.curSeconds != null) commitTimelineDrag(d.cid, d.curSeconds);
      else flashCutsRow(d.cid); // a no-move press is a click
    });

    lanesEl.addEventListener("pointercancel", () => {
      if (!tlDrag) return;
      const d = tlDrag;
      tlDrag = null;
      d.block.classList.remove("is-dragging");
      renderSuiteTimeline(); // discard the aborted drag
    });

    // main-track blocks aren't whole-block draggable — a plain click reveals
    // their row. Skip a click whose target was a trim handle: the native
    // "click" that follows a pointerdown/up pair isn't suppressed by that
    // handler's stopImmediatePropagation (a different event type), and
    // wireSuiteTimelineTrim's own pointerup already decided what a
    // handle press means (trim vs. re-render).
    lanesEl.addEventListener("click", (e) => {
      if (e.target.closest(".suite-tl-handle")) return;
      const block = e.target.closest(".suite-tl-block");
      if (!block || block.dataset.draggable) return;
      flashCutsRow(block.dataset.cid);
    });
  }

  // ---------------- boot ----------------

  // A-5: one boot-time inventory of every RCS app.js symbol / index.html
  // anchor this overlay reaches by name (the coupling surface CONTRACT.md
  // documents). Each use site is already typeof-guarded, so a missing hook
  // only degrades its own feature — but silently. This makes the breakage
  // diagnosable: one console.warn per missing hook plus a summary, never a
  // throw (a missing hook must not take down the rest of the overlay).
  // Every anchor listed here is STATIC in RCS's index.html (verified) —
  // nothing lazily-created is asserted at boot; lazily-populated CONTENT
  // (e.g. transcript rows) is handled by each feature's own observers.
  function assertRcsHooks() {
    const missing = [];
    [["appendCutRow", "Cuts-table insertion (Send to Edit, Favorites)"],
     ["readEditTableIntoState", "undo-safe cut edits"],
     ["pushUndoSnapshot", "undo integration"],
     ["flushTcEditSnapshot", "timecode-edit undo ordering"],
     ["activateTab", "workspace tab switching"],
     ["renderSources", "source-list refresh"],
     ["refreshBrollOverlapWarnings", "B-roll overlap warnings"],
     ["newCid", "row identity for inserted cuts"],
    ].forEach(([name, why]) => {
      if (typeof window[name] !== "function") missing.push(`${name} (${why})`);
    });
    if (typeof state === "undefined" || !state || !Array.isArray(state.editSegments)) {
      missing.push("state.editSegments (RCS edit state)");
    }
    [["editTableBody", "Cuts table"], ["transcriptModalBody", "transcript modal"],
     ["previewSource", "preview window"], ["btnClosePreview", "preview header"],
    ].forEach(([id, why]) => {
      if (!document.getElementById(id)) missing.push(`#${id} (${why})`);
    });
    const colgroup = document.querySelector(".edit-table colgroup");
    if (!colgroup || colgroup.querySelectorAll("col").length < 10) {
      missing.push(".edit-table colgroup with >=10 cols (Audio-column width fix)");
    }
    missing.forEach((m) => console.warn(`[suite] RCS hook missing: ${m}`));
    if (missing.length) {
      console.warn(`[suite] ${missing.length} RCS hook(s) missing — the ` +
        "affected overlay features are disabled; the rest run normally. " +
        "An RCS frontend update likely renamed something CONTRACT.md relies on.");
    }
    return missing;
  }

  // ---------------- Card Eater workspace ----------------
  //
  // Vanilla-JS port of the standalone Card Eater app's React frontend (see
  // CardEater/src/ at the repo root) onto Studio Suite's own conventions —
  // S.cardEater holds the state each of its zustand stores held, call()
  // replaces its lib/tauri.ts wrappers, and polling replaces its Tauri
  // event subscriptions (useTauriEvents.ts): there's no event bus here, so
  // active-card changes and job progress are both detected by diffing
  // against the last poll, same effect as the original's
  // card-mounted/card-unmounted/copy-progress/verify-progress/job-complete
  // events.

  function ceBlankTemplateDraft() {
    return {
      name: "", folder_template: "{Name} {YYYY}",
      file_template: "{YYYYMMDD}_{Name}_{Seq}.{ext}", date_source: "file_metadata",
      seq_start: null, seq_padding: 3, no_subfolder: false,
      use_source_filename: false, no_sequence: false,
    };
  }

  const CE_FOLDER_PRESETS = [
    { mode: "name_year", label: "Name + Year", value: "{Name} {YYYY}" },
    { mode: "date_name", label: "Date + Name", value: "{YYYYMMDD}_{Name}" },
  ];
  function ceModeForFolderTemplate(tpl) {
    const preset = CE_FOLDER_PRESETS.find((p) => p.value === tpl);
    return preset ? preset.mode : "custom";
  }

  function ceFormatBytes(bytes) {
    if (bytes == null || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return `${(bytes / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function ceFormatCreatedAt(file) {
    if (!file.created_at || file.created_at_source === "unavailable") return "—";
    const d = new Date(file.created_at);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString();
  }

  function ceFormatEta(etaSecs) {
    if (etaSecs == null || !Number.isFinite(etaSecs) || etaSecs < 0) return "—";
    const total = Math.round(etaSecs);
    const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  }

  const CE_STATUS_LABEL = { queued: "Queued", running: "Running", paused: "Paused", complete: "Complete", failed: "Failed", cancelled: "Cancelled" };
  const CE_STATUS_CLASS = { queued: "", running: "is-running", paused: "is-paused", complete: "is-done", failed: "is-error", cancelled: "is-cancelled" };

  // ---------------- active card polling ----------------

  async function ceActivateCard(card) {
    const ce = S.cardEater;
    ce.card = card;
    ce.cardMountedAt = new Date().toISOString();
    ce.selectedPaths = new Set();
    ce.highlightedPaths = new Set();
    ce.selectAnchorPath = null;
    ce.files = [];
    ce.focusedPath = null;
    ce.focusedMeta = null;
    ceRenderCardHeader();
    ceRenderFileList();
    ceRenderViewer();
    const res = await call("suite_cardeater_scan_card_files", card.mount_path);
    if (ce.card !== card) return; // card changed again while awaiting
    if (!res.ok) { toast(res.error || "Failed to scan card files", "error"); return; }
    ce.files = res.files || [];

    // Populate the file list and every group, fully expanded, with nothing
    // pre-selected -- the user picks what to copy explicitly (checkbox, or
    // a checkbox click on a click/shift-click-highlighted range) rather
    // than starting from an auto-picked subset that's easy to miss and
    // accidentally copy past.
    ce.selectedPaths = new Set();
    ce.collapsedGroups = new Set();
    ceRenderFileList();
    ceRenderExtFilters();
    ceSchedulePreview();
  }

  function ceDeactivateCard() {
    const ce = S.cardEater;
    ce.card = null;
    ce.cardMountedAt = null;
    ce.files = [];
    ce.selectedPaths = new Set();
    ce.highlightedPaths = new Set();
    ce.selectAnchorPath = null;
    ceRenderCardHeader();
    ceRenderFileList();
    ceRenderExtFilters();
  }

  async function cePollActiveCard() {
    const res = await call("suite_cardeater_get_active_card");
    if (!res.ok) return;
    const ce = S.cardEater;
    const card = res.card;
    const nextId = card ? card.id : null;
    if (ce.prevCardId === undefined) {
      // First poll: adopt silently (e.g. a card already active before this
      // page ever polled), no transition toast.
      ce.prevCardId = nextId;
      if (card) await ceActivateCard(card);
      return;
    }
    if (nextId !== ce.prevCardId) {
      ce.prevCardId = nextId;
      if (card) {
        await ceActivateCard(card);
        toast(`Card detected: ${card.label}`, "ok");
      } else {
        ceDeactivateCard();
      }
    }
  }

  function ceStartActiveCardPolling() {
    cePollActiveCard();
    setInterval(cePollActiveCard, 1500);
  }

  // ---------------- file selector ----------------

  function ceVisibleFiles() {
    const ce = S.cardEater;
    const f = ce.filters;
    return ce.files.filter((file) => {
      if (f.extensions.size > 0 && !f.extensions.has(file.ext.toLowerCase())) return false;
      if (f.dateFrom && file.created_at && file.created_at < f.dateFrom) return false;
      if (f.dateTo && file.created_at && file.created_at > f.dateTo) return false;
      return true;
    });
  }

  function ceGroupFiles(visible) {
    const groups = new Map();
    for (const file of visible) {
      const key = file.relative_folder || "";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(file);
    }
    const result = Array.from(groups.entries()).map(([folder, files]) => ({
      folder, label: folder === "" ? "Root" : folder,
      files: [...files].sort((a, b) => a.name.localeCompare(b.name)),
    }));
    result.sort((a, b) => a.label.localeCompare(b.label));
    return result;
  }

  function ceRenderCardHeader() {
    const ce = S.cardEater;
    const info = $("ceCardInfo");
    const noCardHint = $("ceNoCardHint");
    if (ce.card) {
      info.hidden = false;
      noCardHint.hidden = true;
      $("ceCardLabel").textContent = ce.card.label;
      $("ceCardPath").textContent = ce.card.mount_path;
    } else {
      info.hidden = true;
      noCardHint.hidden = false;
    }
  }

  function ceRenderExtFilters() {
    const ce = S.cardEater;
    const host = $("ceExtFilters");
    const exts = Array.from(new Set(ce.files.map((f) => f.ext.toLowerCase()))).sort();
    host.innerHTML = exts.map((ext) => {
      const active = ce.filters.extensions.has(ext);
      return `<button type="button" class="${active ? "is-active" : ""}" data-ext="${esc(ext)}">.${esc(ext)}</button>`;
    }).join("");
    $("ceToolbar").hidden = ce.files.length === 0;
  }

  function ceRenderFileList() {
    const ce = S.cardEater;
    const host = $("ceFileListScroll");
    if (!ce.card) {
      host.innerHTML = '<p class="suite-empty">No card detected — insert a memory card, or open a folder to treat it as a card, to get started.</p>';
      ceRenderSelectionSummary();
      return;
    }
    const visible = ceVisibleFiles();
    const groups = ceGroupFiles(visible);
    if (groups.length === 0) {
      host.innerHTML = '<p class="suite-empty">No files match the current filters.</p>';
      ceRenderSelectionSummary();
      return;
    }
    host.innerHTML = groups.map((g) => {
      const collapsed = ce.collapsedGroups.has(g.folder);
      const paths = g.files.map((f) => f.path);
      const selectedCount = paths.filter((p) => ce.selectedPaths.has(p)).length;
      const allSelected = paths.length > 0 && selectedCount === paths.length;
      const rows = collapsed ? "" : g.files.map((file) => {
        const unavailable = file.created_at_source === "unavailable" || !file.created_at;
        const focused = ce.focusedPath === file.path;
        const highlighted = ce.highlightedPaths.has(file.path);
        return `<div class="suite-ce-file ${unavailable ? "is-unavailable" : ""} ${highlighted ? "is-highlighted" : ""} ${focused ? "is-focused" : ""}" data-path="${esc(file.path)}">
          <input type="checkbox" ${ce.selectedPaths.has(file.path) ? "checked" : ""} data-role="ce-file-check" />
          <span class="suite-ce-file__name" title="${esc(file.name)}">${esc(file.name)}</span>
          <span class="suite-ce-file__ext">${esc(file.ext || "?")}</span>
          <span class="suite-ce-file__size">${esc(ceFormatBytes(file.size_bytes))}</span>
          <span class="suite-ce-file__date">${esc(ceFormatCreatedAt(file))}</span>
        </div>`;
      }).join("");
      return `<div class="suite-ce-group" data-folder="${esc(g.folder)}">
        <div class="suite-ce-group__head" data-role="ce-group-head">
          <input type="checkbox" data-role="ce-group-check" ${allSelected ? "checked" : ""} />
          <span class="suite-ce-group__caret">${collapsed ? "▸" : "▾"}</span>
          ${esc(g.label)} <span class="suite-ce-group__count">(${g.files.length})</span>
        </div>
        ${rows}
      </div>`;
    }).join("");
    ceRenderSelectionSummary();
  }

  function ceRenderSelectionSummary() {
    const ce = S.cardEater;
    $("ceSelectionSummary").textContent = ce.card ? `${ce.selectedPaths.size} of ${ce.files.length} selected` : "";
    ceRenderLaunchBar();
  }

  // Toggles a single file's checked-for-copy state. Used directly by the
  // checkbox "change" listener whenever there's no multi-file row highlight
  // in play (see ceApplyCheckboxToHighlight for the batch case).
  function ceToggleFileSelection(path) {
    const ce = S.cardEater;
    if (ce.selectedPaths.has(path)) ce.selectedPaths.delete(path); else ce.selectedPaths.add(path);
    ceRenderFileList();
    ceSchedulePreview();
  }

  // Flattened path order matching what's actually rendered/clickable right
  // now (current filters, current group order, collapsed groups excluded --
  // their rows aren't in the DOM, so a shift-click can't span into them).
  // Used only for shift-click range highlighting.
  function ceVisibleOrderedPaths() {
    const ce = S.cardEater;
    const paths = [];
    for (const g of ceGroupFiles(ceVisibleFiles())) {
      if (ce.collapsedGroups.has(g.folder)) continue;
      for (const f of g.files) paths.push(f.path);
    }
    return paths;
  }

  // Finder-style click/shift-click row highlighting -- a visual "which
  // rows am I working with right now" selection, entirely separate from
  // ce.selectedPaths (which files are checked for the copy job). A plain
  // click highlights just that one row; shift-click grows/shrinks the
  // highlighted range from the fixed anchor (ce.selectAnchorPath, only
  // moved by a plain click) to the newly clicked row, replacing whatever
  // was highlighted before -- same as Finder's own shift-click behavior.
  function ceHighlightOnly(path) {
    const ce = S.cardEater;
    ce.highlightedPaths = new Set([path]);
    ce.selectAnchorPath = path;
  }

  function ceHighlightRange(toPath) {
    const ce = S.cardEater;
    const anchor = ce.selectAnchorPath || toPath;
    const order = ceVisibleOrderedPaths();
    const i = order.indexOf(anchor);
    const j = order.indexOf(toPath);
    if (i === -1 || j === -1) { ce.highlightedPaths = new Set([toPath]); return; }
    const [lo, hi] = i < j ? [i, j] : [j, i];
    ce.highlightedPaths = new Set(order.slice(lo, hi + 1));
  }

  // Applies a checkbox click on `path` to the copy-job selection: if `path`
  // belongs to a multi-row highlight, the click acts on the whole
  // highlighted group -- checking (or unchecking) every highlighted file
  // to match the direction this one just moved in -- rather than toggling
  // just the single row the click happened to land on. With no highlight
  // (or a single-row one) it's a plain single-file toggle.
  function ceApplyCheckboxToHighlight(path) {
    const ce = S.cardEater;
    if (ce.highlightedPaths.size > 1 && ce.highlightedPaths.has(path)) {
      const willSelect = !ce.selectedPaths.has(path); // pre-click state: was it checked already?
      ce.highlightedPaths.forEach((p) => willSelect ? ce.selectedPaths.add(p) : ce.selectedPaths.delete(p));
      ceRenderFileList();
      ceSchedulePreview();
    } else {
      ceToggleFileSelection(path);
    }
  }

  // ---------------- naming template editor ----------------

  function ceApplyDraftToForm() {
    const ce = S.cardEater;
    const d = ce.draft;
    $("ceFolderModeNameYear").checked = ce.folderMode === "name_year";
    $("ceFolderModeDateName").checked = ce.folderMode === "date_name";
    $("ceFolderModeCustom").checked = ce.folderMode === "custom";
    const folderInput = $("ceFolderTemplate");
    folderInput.value = d.folder_template;
    folderInput.hidden = ce.folderMode !== "custom";
    folderInput.disabled = d.no_subfolder;
    $("ceFileTemplate").value = d.file_template;
    $("ceFileTemplate").disabled = d.use_source_filename;
    $("ceEventName").value = ce.eventName;
    $("ceDateCardInsert").checked = d.date_source === "card_insert";
    $("ceDateFileMeta").checked = d.date_source === "file_metadata";
    $("ceDateManual").checked = d.date_source === "manual";
    $("ceManualDate").hidden = d.date_source !== "manual";
    $("ceManualDate").value = ce.manualDate ? ce.manualDate.slice(0, 10) : "";
    $("ceSeqStart").value = d.seq_start == null ? "" : String(d.seq_start);
    $("ceSeqPadding").value = String(d.seq_padding);
    $("ceNoSubfolder").checked = d.no_subfolder;
    $("ceUseSourceFilename").checked = d.use_source_filename;
    $("ceTemplateName").value = d.name;
    $("ceTemplateDelete").disabled = ce.selectedTemplateId == null;
    const select = $("ceTemplateSelect");
    select.value = ce.selectedTemplateId == null ? "" : String(ce.selectedTemplateId);
  }

  function ceRenderTemplateSelect() {
    const ce = S.cardEater;
    const select = $("ceTemplateSelect");
    select.innerHTML = '<option value="">-- Select saved template --</option>' +
      ce.templates.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
    select.value = ce.selectedTemplateId == null ? "" : String(ce.selectedTemplateId);
  }

  async function ceRefreshTemplates() {
    const res = await call("suite_cardeater_list_naming_templates");
    if (!res.ok) return;
    S.cardEater.templates = res.templates || [];
    ceRenderTemplateSelect();
  }

  function ceHandleSelectTemplate(idStr) {
    const ce = S.cardEater;
    if (!idStr) { ce.selectedTemplateId = null; ceApplyDraftToForm(); return; }
    const id = Number(idStr);
    const tpl = ce.templates.find((t) => t.id === id);
    if (!tpl) return;
    ce.selectedTemplateId = tpl.id;
    ce.draft = {
      name: tpl.name, folder_template: tpl.folder_template, file_template: tpl.file_template,
      date_source: tpl.date_source, seq_start: tpl.seq_start, seq_padding: tpl.seq_padding,
      no_subfolder: tpl.no_subfolder, use_source_filename: tpl.use_source_filename, no_sequence: tpl.no_sequence,
    };
    ce.prevFileTemplate = tpl.file_template;
    ce.folderMode = ceModeForFolderTemplate(tpl.folder_template);
    ceApplyDraftToForm();
    ceSchedulePreview();
  }

  function ceHandleNewTemplate() {
    const ce = S.cardEater;
    ce.draft = ceBlankTemplateDraft();
    ce.selectedTemplateId = null;
    ce.prevFileTemplate = ce.draft.file_template;
    ce.folderMode = ceModeForFolderTemplate(ce.draft.folder_template);
    $("ceTemplateError").hidden = true;
    ceApplyDraftToForm();
    ceSchedulePreview();
  }

  async function ceHandleSaveTemplate() {
    const ce = S.cardEater;
    const name = ce.draft.name.trim();
    if (!name) {
      const err = $("ceTemplateError");
      err.hidden = false; err.textContent = "Enter a template name before saving.";
      return;
    }
    const res = await call("suite_cardeater_save_naming_template", { ...ce.draft, name });
    if (!res.ok) {
      const err = $("ceTemplateError");
      err.hidden = false; err.textContent = res.error || "Failed to save template";
      return;
    }
    $("ceTemplateError").hidden = true;
    await ceRefreshTemplates();
    ce.selectedTemplateId = res.template.id;
    ceApplyDraftToForm();
    toast(`Saved template "${res.template.name}"`, "ok");
  }

  async function ceHandleDeleteTemplate() {
    const ce = S.cardEater;
    if (ce.selectedTemplateId == null) return;
    const res = await call("suite_cardeater_delete_naming_template", ce.selectedTemplateId);
    if (!res.ok) { toastIfError(res, "Failed to delete template"); return; }
    await ceRefreshTemplates();
    ce.selectedTemplateId = null;
    ce.draft = ceBlankTemplateDraft();
    ce.folderMode = ceModeForFolderTemplate(ce.draft.folder_template);
    ceApplyDraftToForm();
    toast("Template deleted", "ok");
  }

  function ceHandleFolderModeChange(mode) {
    const ce = S.cardEater;
    ce.folderMode = mode;
    if (mode !== "custom") {
      const preset = CE_FOLDER_PRESETS.find((p) => p.mode === mode);
      if (preset) ce.draft.folder_template = preset.value;
    }
    ceApplyDraftToForm();
    ceSchedulePreview();
  }

  function ceHandleUseSourceFilenameToggle(checked) {
    const ce = S.cardEater;
    if (checked) {
      if (!ce.draft.use_source_filename) ce.prevFileTemplate = ce.draft.file_template;
      ce.draft.use_source_filename = true;
      ce.draft.file_template = "{OriginalName}.{ext}";
    } else {
      ce.draft.use_source_filename = false;
      ce.draft.file_template = ce.prevFileTemplate || ceBlankTemplateDraft().file_template;
    }
    ceApplyDraftToForm();
    ceSchedulePreview();
  }

  function ceRenderPreview() {
    const ce = S.cardEater;
    const host = $("cePreview");
    if (ce.previewError) {
      host.innerHTML = `<p class="suite-ce-error">${esc(ce.previewError)}</p>`;
      return;
    }
    if (!ce.preview) {
      host.innerHTML = '<p class="suite-hint suite-hint--tight">Select files on a card to see a naming preview.</p>';
      return;
    }
    const parts = [];
    if (!ce.draft.no_subfolder) {
      parts.push(`<p class="suite-ce-preview__folder">Folder: <b>${esc(ce.preview.folder_name || "(none)")}</b></p>`);
    }
    for (const name of ce.preview.sample_file_names) {
      parts.push(`<p class="suite-ce-preview__file">${esc(name)}</p>`);
    }
    for (const w of ce.preview.warnings || []) {
      parts.push(`<p class="suite-ce-preview__warning">⚠ ${esc(w)}</p>`);
    }
    host.innerHTML = parts.join("");
  }

  let cePreviewTimer = null;
  function ceSchedulePreview() {
    clearTimeout(cePreviewTimer);
    cePreviewTimer = setTimeout(ceRunPreview, 300);
    ceScheduleCollisionCheck();
  }

  async function ceRunPreview() {
    const ce = S.cardEater;
    const files = ce.files.filter((f) => ce.selectedPaths.has(f.path));
    if (files.length === 0) { ce.preview = null; ce.previewError = null; ceRenderPreview(); return; }
    const req = {
      card_insert_date: ce.cardMountedAt || new Date().toISOString(),
      event_name: ce.eventName,
      manual_date: ce.draft.date_source === "manual" ? ce.manualDate : null,
      template: ce.draft,
      files,
      dest_path: ce.destinations.length > 0 ? ce.destinations[0] : null,
    };
    const res = await call("suite_cardeater_preview_names", req);
    if (res.ok) { ce.preview = res; ce.previewError = null; } else { ce.preview = null; ce.previewError = res.error || "Preview failed"; }
    ceRenderPreview();
  }

  // ---------------- destinations + favorites ----------------

  function ceRenderFavorites() {
    const ce = S.cardEater;
    const host = $("ceFavorites");
    if (ce.favorites.length === 0) { host.innerHTML = '<p class="suite-hint suite-hint--tight">No favorite destinations yet.</p>'; return; }
    host.innerHTML = ce.favorites.map((fav) => {
      const active = ce.destinations.includes(fav.path);
      return `<span class="suite-ce-fav ${active ? "is-active" : ""}">
        <button type="button" data-role="ce-fav-add" data-path="${esc(fav.path)}" title="${esc(fav.path)}">${active ? "✓ " : ""}${esc(fav.label)}</button>
        <button type="button" data-role="ce-fav-remove" data-id="${fav.id}" aria-label="Remove favorite ${esc(fav.label)}">×</button>
      </span>`;
    }).join("");
  }

  async function ceRefreshFavorites() {
    const res = await call("suite_cardeater_list_favorites");
    if (!res.ok) return;
    S.cardEater.favorites = res.favorites || [];
    ceRenderFavorites();
  }

  function ceRenderDestinations() {
    const ce = S.cardEater;
    const host = $("ceDestList");
    if (ce.destinations.length === 0) {
      host.innerHTML = '<li class="suite-hint suite-hint--tight" style="border:none;background:none;padding:2px 0">No destinations selected yet.</li>';
      ceRenderFavorites();
      ceRenderLaunchBar();
      return;
    }
    host.innerHTML = ce.destinations.map((path) => {
      const c = ce.collisions[path];
      let badge = "";
      if (c && c.status === "exists_empty") badge = '<span class="suite-ce-collision suite-ce-collision--empty">folder exists, empty</span>';
      else if (c && c.status === "exists_non_empty") badge = '<span class="suite-ce-collision suite-ce-collision--full">⚠ folder exists, has files</span>';
      return `<li><span title="${esc(path)}">${esc(path)}</span>${badge}<button type="button" class="suite-btn suite-btn--secondary suite-btn--small" data-role="ce-dest-preview" data-path="${esc(path)}">Preview</button><button type="button" data-role="ce-dest-remove" data-path="${esc(path)}" aria-label="Remove destination">✕</button></li>`;
    }).join("");
    ceRenderFavorites();
    ceRenderLaunchBar();
  }

  let ceCollisionTimer = null;
  function ceScheduleCollisionCheck() {
    clearTimeout(ceCollisionTimer);
    const ce = S.cardEater;
    const folderName = ce.preview && !ce.draft.no_subfolder ? ce.preview.folder_name : null;
    if (!folderName || ce.destinations.length === 0) { ce.collisions = {}; ceRenderDestinations(); return; }
    ceCollisionTimer = setTimeout(async () => {
      const entries = await Promise.all(ce.destinations.map(async (destPath) => {
        const res = await call("suite_cardeater_check_folder_collision", destPath, folderName);
        return [destPath, res.ok ? res : null];
      }));
      ce.collisions = Object.fromEntries(entries.filter(([, v]) => v));
      ceRenderDestinations();
    }, 300);
  }

  async function ceHandleAddDestination() {
    const res = await call("suite_cardeater_pick_destination");
    if (!res.ok) { if (!res.cancelled) toastIfError(res, "Failed to open folder picker"); return; }
    const ce = S.cardEater;
    for (const p of res.paths || []) if (p && !ce.destinations.includes(p)) ce.destinations.push(p);
    ceRenderDestinations();
    ceScheduleCollisionCheck();
  }

  // "Preview" button on a destination row: shows what's already sitting in
  // it (or its resolved per-job subfolder, once a naming preview exists)
  // before the user commits to copying into it.
  async function ceOpenDestPreview(path) {
    const ce = S.cardEater;
    $("ceDestPreviewModal").hidden = false;
    $("ceDestPreviewBody").innerHTML = '<p class="suite-empty">Loading…</p>';
    const folderName = (!ce.draft.no_subfolder && ce.preview) ? ce.preview.folder_name : null;
    const res = await call("suite_cardeater_list_destination_files", path, folderName);
    if (!res.ok) {
      $("ceDestPreviewBody").innerHTML = `<p class="suite-ce-error">${esc(res.error || "Failed to list destination files")}</p>`;
      return;
    }
    ceRenderDestPreview(res);
  }

  function ceRenderDestPreview(res) {
    const header = `<p class="suite-hint suite-hint--tight" style="word-break:break-all">${esc(res.resolved_path)}</p>`;
    if (!res.exists) {
      $("ceDestPreviewBody").innerHTML = header +
        '<p class="suite-empty">This folder doesn\'t exist yet — nothing here to collide with.</p>';
      return;
    }
    if (res.entries.length === 0) {
      $("ceDestPreviewBody").innerHTML = header + '<p class="suite-empty">Empty — no files here yet.</p>';
      return;
    }
    const rows = res.entries.map((e) => `<div class="suite-ce-destpreview__row">
      <span class="suite-ce-destpreview__name">${e.is_dir ? "📁 " : ""}${esc(e.name)}</span>
      <span class="suite-ce-destpreview__size">${e.is_dir ? "" : esc(ceFormatBytes(e.size_bytes))}</span>
    </div>`).join("");
    $("ceDestPreviewBody").innerHTML = header + rows;
  }

  // ---------------- job launcher ----------------

  function ceRenderLaunchBar() {
    const ce = S.cardEater;
    const selected = ce.files.filter((f) => ce.selectedPaths.has(f.path));
    const totalBytes = selected.reduce((sum, f) => sum + f.size_bytes, 0);
    $("ceLaunchSummary").textContent =
      `${selected.length} file(s) selected · ${ceFormatBytes(totalBytes)} · ${ce.destinations.length} destination(s)`;
    const canStart = !!ce.card && selected.length > 0 && ce.destinations.length > 0 &&
      (ce.draft.date_source !== "manual" || !!ce.manualDate);
    $("ceStartJob").disabled = !canStart;
  }

  async function ceHandleStartJob() {
    const ce = S.cardEater;
    if (!ce.card || !ce.cardMountedAt) return;
    const selected = ce.files.filter((f) => ce.selectedPaths.has(f.path));
    const totalBytes = selected.reduce((sum, f) => sum + f.size_bytes, 0);
    const btn = $("ceStartJob");
    btn.disabled = true;
    $("ceSpaceWarnings").hidden = true;
    try {
      const spaceRes = await call("suite_cardeater_check_disk_space", ce.destinations, totalBytes);
      if (spaceRes.ok) {
        const failing = spaceRes.checks.filter((c) => !c.ok);
        if (failing.length > 0) {
          const warn = $("ceSpaceWarnings");
          warn.hidden = false;
          warn.innerHTML = "<b>Not enough free space:</b><br>" + failing.map((c) =>
            `${esc(c.dest_path)}: needs ${ceFormatBytes(c.required_bytes)}, only ${ceFormatBytes(c.available_bytes)} available`
          ).join("<br>");
          return;
        }
      }
      const res = await call("suite_cardeater_start_job", {
        source_card_label: ce.card.label, source_path: ce.card.mount_path,
        card_insert_date: ce.cardMountedAt, event_name: ce.eventName,
        manual_date: ce.draft.date_source === "manual" ? ce.manualDate : null,
        template: ce.draft, files: selected, destinations: ce.destinations,
      });
      if (!res.ok) { toastIfError(res, "Failed to start job"); return; }
      toast(`Copy #${res.job_id} started — ${selected.length} file(s) to ${ce.destinations.length} destination(s)`, "ok");
      // Progress now lives in the suite-wide Jobs drawer (kind
      // "cardeater_copy", one entry per destination) rather than a
      // separate Copy Queue panel — same drawer/poll loop every other
      // workspace's background work already uses.
      ensurePolling();
      openDrawer();
    } finally {
      ceRenderLaunchBar();
    }
  }

  // ---------------- "safe to remove card" (still Copy-workspace-local — it's
  // about the physical card, not any one job) ----------------

  async function ceCheckSafeToRemove() {
    const ce = S.cardEater;
    const safeEl = $("ceSafeToRemove");
    if (!ce.card) { safeEl.hidden = true; return; }
    const res = await call("suite_cardeater_mark_card_safe_check", ce.card.mount_path);
    if (!res.ok) return;
    ce.safeToRemove = res.safe;
    safeEl.hidden = false;
    safeEl.className = "suite-ce-queue__safe " + (res.safe ? "is-safe" : "is-waiting");
    safeEl.textContent = res.safe ? "✓ Safe to remove card" : "⏳ Copying/verifying — do not remove card";
  }

  // ---------------- summary / history / preview modals ----------------

  // Called from pollJobs() on a cardeater_copy job's done/error transition
  // — `job` is one entry from the merged Jobs-drawer list (suite_list_jobs),
  // not the old per-job/per-destination shape the Copy Queue used to poll.
  function ceShowSummaryForJob(job) {
    const r = job.result || {};
    S.cardEater.summary = {
      files_total: r.files_total || 0,
      bytes_total: r.bytes_total || 0,
      files_verified: r.files_verified || 0,
      dest_path: r.resolved_path || r.dest_path || job.label,
      status: job.status === "error" ? "failed" : "complete",
      error_message: job.error || null,
    };
    ceRenderSummary();
    $("ceSummaryModal").hidden = false;
  }

  function ceRenderSummary() {
    const s = S.cardEater.summary;
    if (!s) return;
    const failed = s.status === "failed";
    $("ceSummaryBody").innerHTML = `
      <div class="suite-ce-summary-grid">
        <div class="suite-ce-summary-stat"><b>${s.files_total}</b><span>Total Files</span></div>
        <div class="suite-ce-summary-stat"><b>${esc(ceFormatBytes(s.bytes_total))}</b><span>Total Size</span></div>
        <div class="suite-ce-summary-stat"><b>${s.files_verified}</b><span>Verified</span></div>
      </div>
      ${failed
        ? `<div class="suite-ce-summary-fail">Some files failed to copy/verify at <span style="word-break:break-all">${esc(s.dest_path)}</span>.${s.error_message ? `<br>${esc(s.error_message)}` : ""}</div>`
        : `<p class="suite-ce-summary-ok">✓ All files verified successfully at ${esc(s.dest_path)}</p>`}
      ${!failed && s.dest_path ? `<div class="suite-job__actions">
        <button class="suite-btn suite-btn--secondary suite-btn--small" data-action="cc-send-broll" data-path="${esc(s.dest_path)}">Send to B-Roll Analyzer</button>
        <button class="suite-btn suite-btn--secondary suite-btn--small" data-action="cc-run-pipeline" data-path="${esc(s.dest_path)}">Run Pipeline…</button>
      </div>` : ""}
    `;
  }

  // Kicks off a B-Roll analysis of `folderPath` (a Copy job's resolved
  // destination folder) and switches to that workspace to watch it run --
  // same effect as clicking Browse… then Analyze on the B-Roll workspace's
  // own folder field, just skipping the folder picker since the path is
  // already known.
  async function ceSendToBroll(folderPath) {
    if (!folderPath) return;
    const res = await call("broll_start", folderPath);
    if (!res.ok) { toastIfError(res, "Couldn't start B-Roll analysis."); return; }
    const folderInput = $("bFolder");
    if (folderInput) folderInput.value = folderPath;
    switchWs("broll");
    ensurePolling();
    openDrawer();
    toast(`Sent to B-Roll Analyzer — analyzing ${basename(folderPath)}`, "ok");
  }

  async function ceOpenHistory() {
    $("ceHistoryModal").hidden = false;
    $("ceHistoryBody").innerHTML = '<p class="suite-empty">Loading…</p>';
    const res = await call("suite_cardeater_list_jobs", null);
    if (!res.ok) { $("ceHistoryBody").innerHTML = `<p class="suite-ce-error">${esc(res.error || "Failed to load job history")}</p>`; return; }
    S.cardEater.historyJobs = res.jobs || [];
    ceRenderHistory();
  }

  function ceRenderHistory() {
    const jobs = S.cardEater.historyJobs;
    if (jobs.length === 0) { $("ceHistoryBody").innerHTML = '<p class="suite-empty">No jobs yet</p>'; return; }
    $("ceHistoryBody").innerHTML = `<table class="suite-ce-history-table">
      <thead><tr><th>Card</th><th>Started</th><th>Status</th><th>Destinations</th><th>Files</th><th>Size</th></tr></thead>
      <tbody>${jobs.map((j) => `<tr>
        <td>${esc(j.source_card_label)}</td>
        <td>${j.started_at ? esc(new Date(j.started_at).toLocaleString()) : "—"}</td>
        <td><span class="suite-job__status ${CE_STATUS_CLASS[j.status] || ""}">${CE_STATUS_LABEL[j.status] || j.status}</span></td>
        <td title="${esc(j.destination_paths)}">${j.destination_count}</td>
        <td>${j.file_count}</td>
        <td>${esc(ceFormatBytes(j.bytes_total))}</td>
      </tr>`).join("")}</tbody>
    </table>`;
  }

  async function ceExportHistoryCsv() {
    const res = await call("suite_cardeater_export_job_history_csv", null);
    if (!res.ok) { toastIfError(res, "Failed to export job history"); return; }
    if (res.path) toast(`Job history exported to ${res.path}`, "ok");
  }

  // ---------------- viewer panel (preview + metadata for the focused file) ----------------
  //
  // Clicking anywhere on a file's row "focuses" that file: the panel fetches
  // the preview URL (RCS's own range-capable preview server, same one Sync/
  // B-Roll use) and, separately, on-demand extended metadata (dimensions/
  // duration/frame rate/camera) via a one-off exiftool call the backend
  // doesn't run at scan time (see cardeater_metadata.resolve_extended_metadata's
  // docstring — batching that for every file on a large card would slow
  // scanning down for a field only shown once a file is actually viewed).
  // Clicking the same file's row again unfocuses it (closes the panel).

  function ceSetFocusedFile(path) {
    const ce = S.cardEater;
    ce.focusedPath = path;
    ce.focusedMeta = null;
    ce.viewerUrl = null;
    ceRenderFileList();
    ceRenderViewer();
    if (path) {
      const file = ce.files.find((f) => f.path === path);
      ceLoadViewerPreview(path, file && file.ext);
      ceLoadViewerMetadata(path);
    }
  }

  function ceFocusFile(path) {
    const ce = S.cardEater;
    ceSetFocusedFile(ce.focusedPath === path ? null : path);
  }

  function ceFormatDuration(secs) {
    if (secs == null || !Number.isFinite(secs) || secs <= 0) return null;
    const total = Math.round(secs);
    const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
    const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
    const ss = String(s).padStart(2, "0");
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  }

  async function ceLoadViewerPreview(path, ext) {
    const ce = S.cardEater;
    const res = await call("suite_cardeater_preview_url", path);
    if (ce.focusedPath !== path) return; // user focused a different file before this resolved
    const e = (ext || "").toLowerCase();
    let markup;
    if (!res.ok || !res.url) {
      markup = `<p class="suite-hint">No preview available for .${esc(e || "?")} files.</p>`;
    } else if (["jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif"].includes(e)) {
      markup = `<img src="${esc(res.url)}" alt="${esc(basename(path))}" />`;
    } else if (["mp4", "mov", "m4v", "webm"].includes(e)) {
      markup = `<video src="${esc(res.url)}" controls></video>`;
    } else if (["wav", "mp3", "m4a", "aac", "aiff"].includes(e)) {
      markup = `<audio src="${esc(res.url)}" controls></audio>`;
    } else {
      markup = `<p class="suite-hint">No preview available for .${esc(e)} files.</p>`;
    }
    ce.viewerUrl = { path, markup };
    ceRenderViewer();
  }

  async function ceLoadViewerMetadata(path) {
    const ce = S.cardEater;
    const res = await call("suite_cardeater_file_metadata", path);
    if (ce.focusedPath !== path) return;
    ce.focusedMeta = res.ok ? res.metadata : { available: false };
    ceRenderViewer();
  }

  function ceRenderViewer() {
    const ce = S.cardEater;
    const host = $("ceViewerBody");
    const file = ce.files.find((f) => f.path === ce.focusedPath);
    if (!file) {
      host.innerHTML = '<p class="suite-hint suite-hint--tight">Click "View" on a file to preview it and see its metadata here.</p>';
      return;
    }

    const media = (ce.viewerUrl && ce.viewerUrl.path === file.path) ? ce.viewerUrl.markup : '<p class="suite-empty">Loading…</p>';

    const dateSourceLabel = { exif: "Metadata", filesystem: "Filesystem", unavailable: "Unavailable" }[file.created_at_source] || "—";
    const rows = [
      ["Size", ceFormatBytes(file.size_bytes)],
      ["Extension", `.${file.ext || "?"}`],
      ["Created", ceFormatCreatedAt(file)],
      ["Date source", dateSourceLabel],
    ];
    const m = ce.focusedMeta;
    if (m && m.available) {
      if (m.width && m.height) rows.push(["Dimensions", `${m.width} × ${m.height}`]);
      const dur = ceFormatDuration(m.duration_secs);
      if (dur) rows.push(["Duration", dur]);
      if (m.frame_rate) rows.push(["Frame rate", `${Number(m.frame_rate).toFixed(2)} fps`]);
      const camera = [m.camera_make, m.camera_model].filter(Boolean).join(" ");
      if (camera) rows.push(["Camera", camera]);
      if (m.file_type) rows.push(["File type", m.file_type]);
    }

    host.innerHTML = `
      <div class="suite-ce-viewer__media">${media}</div>
      <div>
        <p class="suite-ce-viewer__filename" title="${esc(file.name)}">${esc(file.name)}</p>
        <p class="suite-ce-viewer__path" title="${esc(file.path)}">${esc(file.path)}</p>
      </div>
      <div class="suite-ce-viewer__meta">
        ${rows.map(([label, value]) => `<div class="suite-ce-viewer__row"><span>${esc(label)}</span><span>${esc(value)}</span></div>`).join("")}
        ${m && !m.available ? '<p class="suite-hint suite-hint--tight">Extended metadata (dimensions/duration/camera) unavailable for this file.</p>' : ""}
      </div>
    `;
  }

  // ============================================================
  // Viewer panel: user-adjustable width, dragged via #ceViewerResize
  // (the grid column between the file list and the viewer aside — see
  // #workspace-cardeater's `var(--ce-viewer-w, 390px)` track). Persisted
  // in localStorage as a per-user display preference, same pattern as
  // the Cuts table's own column-width persistence above.
  // ============================================================

  const CE_VIEWER_WIDTH_MIN = 280;
  const CE_VIEWER_WIDTH_MAX = 640;
  const CE_VIEWER_WIDTH_STORAGE_KEY = "suiteCeViewerWidth.v1";

  function ceGetViewerWidthPx() {
    const ws = $("workspace-cardeater");
    const raw = parseFloat(getComputedStyle(ws).getPropertyValue("--ce-viewer-w"));
    return isNaN(raw) ? 390 : raw;
  }

  function ceSetViewerWidthPx(px) {
    const clamped = Math.max(CE_VIEWER_WIDTH_MIN, Math.min(CE_VIEWER_WIDTH_MAX, px));
    $("workspace-cardeater").style.setProperty("--ce-viewer-w", clamped + "px");
    return clamped;
  }

  function ceLoadSavedViewerWidth() {
    let saved;
    try {
      saved = parseFloat(localStorage.getItem(CE_VIEWER_WIDTH_STORAGE_KEY));
    } catch (e) {
      return;
    }
    if (!isNaN(saved) && saved > 0) ceSetViewerWidthPx(saved);
  }

  function ceWireViewerResize() {
    const handle = $("ceViewerResize");
    if (!handle) return;

    handle.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const startWidth = ceGetViewerWidthPx();

      document.body.classList.add("suite-ce-viewer-resizing");
      handle.classList.add("is-dragging");

      function onMove(ev) {
        // Viewer sits to the RIGHT of the handle, so dragging left
        // (negative delta) widens it and dragging right narrows it.
        const delta = ev.clientX - startX;
        ceSetViewerWidthPx(startWidth - delta);
      }
      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.classList.remove("suite-ce-viewer-resizing");
        handle.classList.remove("is-dragging");
        try {
          localStorage.setItem(CE_VIEWER_WIDTH_STORAGE_KEY, String(ceGetViewerWidthPx()));
        } catch (e) {
          // Private-browsing quota or localStorage disabled -- resizing
          // still works for the rest of this session, it just won't persist.
        }
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });

    // Keyboard nudge, since the handle is a focusable separator (arrow
    // keys are the conventional way to resize a native splitter).
    handle.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft") { e.preventDefault(); ceSetViewerWidthPx(ceGetViewerWidthPx() + 16); }
      else if (e.key === "ArrowRight") { e.preventDefault(); ceSetViewerWidthPx(ceGetViewerWidthPx() - 16); }
      else return;
      try {
        localStorage.setItem(CE_VIEWER_WIDTH_STORAGE_KEY, String(ceGetViewerWidthPx()));
      } catch (err) { /* see onUp above */ }
    });
  }

  // The file list's columns (name/ext/size/date) are laid out with flexbox
  // at fixed widths for everything but the name, which is what happens to
  // get squeezed as the results panel narrows (viewer widened, or a small
  // window). A ResizeObserver — rather than a CSS media/container query —
  // is what's needed here: the results panel's width isn't derivable from
  // the viewport (it's a `1fr` grid track competing with a user-resizable
  // viewer column), so only measuring the live element works.
  const CE_RESULTS_COMPACT_WIDTH = 480; // below this, drop the date column
  const CE_RESULTS_MINI_WIDTH = 360;    // below this, also drop the ext column

  function ceWireResultsResizeObserver() {
    const panel = $("ceResultsPanel");
    if (!panel || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const width = entries[0].contentRect.width;
      panel.classList.toggle("is-ce-compact", width < CE_RESULTS_COMPACT_WIDTH);
      panel.classList.toggle("is-ce-mini", width < CE_RESULTS_MINI_WIDTH);
    });
    ro.observe(panel);
  }

  // ---------------- wiring ----------------

  function wireCardEater() {
    const ce = S.cardEater;
    ce.draft = ceBlankTemplateDraft();
    ce.folderMode = ceModeForFolderTemplate(ce.draft.folder_template);
    ce.prevFileTemplate = ce.draft.file_template;

    ceLoadSavedViewerWidth();
    ceWireViewerResize();
    ceWireResultsResizeObserver();

    $("ceOpenSource").addEventListener("click", async () => {
      const ce = S.cardEater;
      if (!ce.card) return;
      const res = await call("suite_cardeater_open_folder", ce.card.mount_path);
      toastIfError(res, "Couldn't open that folder");
    });

    $("ceOpenFolderAsCard").addEventListener("click", async () => {
      const res = await call("suite_cardeater_open_folder_as_card");
      if (!res.ok) { if (!res.cancelled) toastIfError(res, "Failed to open folder"); return; }
      S.cardEater.prevCardId = res.card.id;
      await ceActivateCard(res.card);
    });

    $("ceFileListScroll").addEventListener("click", (e) => {
      const groupHead = e.target.closest('[data-role="ce-group-head"]');
      if (groupHead && !e.target.closest('[data-role="ce-group-check"]')) {
        const folder = groupHead.closest(".suite-ce-group").dataset.folder;
        const cg = S.cardEater.collapsedGroups;
        if (cg.has(folder)) cg.delete(folder); else cg.add(folder);
        ceRenderFileList();
        return;
      }
      // Finder-style click/shift-click row highlighting: works from
      // anywhere on a file's row, not just its own tiny checkbox. This only
      // highlights rows (ce.highlightedPaths) and focuses the viewer -- it
      // never checks/unchecks a file for the copy job by itself. The
      // checkbox (native "change" listener below) is what marks files for
      // copy; when the checkbox click lands on a file that's part of a
      // multi-row highlight, it acts on the whole highlighted group (see
      // ceApplyCheckboxToHighlight) rather than just that one row.
      const row = e.target.closest(".suite-ce-file");
      if (!row) return;
      const path = row.dataset.path;
      const onCheckbox = !!e.target.closest('[data-role="ce-file-check"]');

      if (e.shiftKey) {
        // preventDefault stops the checkbox's own native toggle (when the
        // shift-click happened to land on it) so the "change" listener
        // below never fires for this click.
        e.preventDefault();
        ceHighlightRange(path);
        ceSetFocusedFile(path); // also re-renders the file list, picking up the new highlight
        return;
      }
      if (!onCheckbox) {
        ceHighlightOnly(path);
        ceFocusFile(path); // also re-renders the file list, picking up the new highlight
      }
    });
    $("ceFileListScroll").addEventListener("change", (e) => {
      const groupCheck = e.target.closest('[data-role="ce-group-check"]');
      if (groupCheck) {
        // Read the group's member paths off the data model rather than
        // querying rendered .suite-ce-file rows -- a collapsed group
        // renders zero rows (see ceRenderFileList), so a DOM query here
        // would silently find nothing and do nothing while closed.
        const ce = S.cardEater;
        const folder = groupCheck.closest(".suite-ce-group").dataset.folder;
        const paths = ceVisibleFiles()
          .filter((f) => (f.relative_folder || "") === folder)
          .map((f) => f.path);
        const allSelected = paths.length > 0 && paths.every((p) => ce.selectedPaths.has(p));
        paths.forEach((p) => allSelected ? ce.selectedPaths.delete(p) : ce.selectedPaths.add(p));
        ceRenderFileList();
        ceSchedulePreview();
        return;
      }
      const fileCheck = e.target.closest('[data-role="ce-file-check"]');
      if (fileCheck) {
        const path = fileCheck.closest(".suite-ce-file").dataset.path;
        // Acts on the whole row highlight (if this file is part of one with
        // more than one member) rather than just this single checkbox --
        // see ceApplyCheckboxToHighlight.
        ceApplyCheckboxToHighlight(path);
        // Populate the viewer with whichever file's checkbox was just
        // touched, so batch-checking a run of clips lets you quickly
        // confirm what you're adding without a separate "View" click.
        ceSetFocusedFile(path);
      }
    });

    $("ceExtFilters").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-ext]");
      if (!btn) return;
      const ext = btn.dataset.ext;
      const exts = S.cardEater.filters.extensions;
      if (exts.has(ext)) exts.delete(ext); else exts.add(ext);
      ceRenderExtFilters();
      ceRenderFileList();
    });
    $("ceDateFrom").addEventListener("change", (e) => {
      S.cardEater.filters.dateFrom = e.target.value ? `${e.target.value}T00:00:00Z` : null;
      ceRenderFileList();
    });
    $("ceDateTo").addEventListener("change", (e) => {
      S.cardEater.filters.dateTo = e.target.value ? `${e.target.value}T23:59:59Z` : null;
      ceRenderFileList();
    });
    $("ceSelectAll").addEventListener("click", () => {
      S.cardEater.selectedPaths = new Set(S.cardEater.files.map((f) => f.path));
      ceRenderFileList();
      ceSchedulePreview();
    });
    $("ceSelectNone").addEventListener("click", () => {
      S.cardEater.selectedPaths = new Set();
      ceRenderFileList();
      ceSchedulePreview();
    });

    // naming template editor
    $("ceTemplateSelect").addEventListener("change", (e) => ceHandleSelectTemplate(e.target.value));
    $("ceTemplateNew").addEventListener("click", ceHandleNewTemplate);
    $("ceTemplateName").addEventListener("input", (e) => { S.cardEater.draft.name = e.target.value; });
    $("ceTemplateSave").addEventListener("click", ceHandleSaveTemplate);
    $("ceTemplateDelete").addEventListener("click", ceHandleDeleteTemplate);

    $("ceFolderModeNameYear").addEventListener("change", () => ceHandleFolderModeChange("name_year"));
    $("ceFolderModeDateName").addEventListener("change", () => ceHandleFolderModeChange("date_name"));
    $("ceFolderModeCustom").addEventListener("change", () => ceHandleFolderModeChange("custom"));
    $("ceFolderTemplate").addEventListener("input", (e) => { S.cardEater.draft.folder_template = e.target.value; ceSchedulePreview(); });
    $("ceFileTemplate").addEventListener("input", (e) => { S.cardEater.draft.file_template = e.target.value; ceSchedulePreview(); });
    $("ceEventName").addEventListener("input", (e) => { S.cardEater.eventName = e.target.value; ceSchedulePreview(); });

    $("ceDateCardInsert").addEventListener("change", () => { S.cardEater.draft.date_source = "card_insert"; ceApplyDraftToForm(); ceSchedulePreview(); });
    $("ceDateFileMeta").addEventListener("change", () => { S.cardEater.draft.date_source = "file_metadata"; ceApplyDraftToForm(); ceSchedulePreview(); });
    $("ceDateManual").addEventListener("change", () => { S.cardEater.draft.date_source = "manual"; ceApplyDraftToForm(); ceSchedulePreview(); });
    $("ceManualDate").addEventListener("change", (e) => { S.cardEater.manualDate = e.target.value ? `${e.target.value}T00:00:00Z` : null; ceSchedulePreview(); ceRenderLaunchBar(); });

    $("ceSeqStart").addEventListener("input", (e) => {
      S.cardEater.draft.seq_start = e.target.value.trim() === "" ? null : Number(e.target.value);
      ceSchedulePreview();
    });
    $("ceSeqPadding").addEventListener("input", (e) => {
      const n = Number(e.target.value);
      S.cardEater.draft.seq_padding = Number.isNaN(n) ? 3 : n;
      ceSchedulePreview();
    });

    $("ceNoSubfolder").addEventListener("change", (e) => {
      S.cardEater.draft.no_subfolder = e.target.checked;
      ceApplyDraftToForm();
      ceSchedulePreview();
    });
    $("ceUseSourceFilename").addEventListener("change", (e) => ceHandleUseSourceFilenameToggle(e.target.checked));

    // destinations + favorites
    $("ceAddDestination").addEventListener("click", ceHandleAddDestination);
    $("ceAddFavorite").addEventListener("click", async () => {
      const label = $("ceNewFavLabel").value.trim();
      const path = $("ceNewFavPath").value.trim();
      if (!label || !path) return;
      const res = await call("suite_cardeater_add_favorite", label, path);
      if (!res.ok) { toastIfError(res, "Failed to add favorite"); return; }
      $("ceNewFavLabel").value = ""; $("ceNewFavPath").value = "";
      await ceRefreshFavorites();
    });
    $("ceFavorites").addEventListener("click", async (e) => {
      const add = e.target.closest('[data-role="ce-fav-add"]');
      if (add) {
        const path = add.dataset.path;
        if (!S.cardEater.destinations.includes(path)) S.cardEater.destinations.push(path);
        ceRenderDestinations();
        ceScheduleCollisionCheck();
        return;
      }
      const remove = e.target.closest('[data-role="ce-fav-remove"]');
      if (remove) {
        const res = await call("suite_cardeater_remove_favorite", Number(remove.dataset.id));
        if (res.ok) await ceRefreshFavorites();
      }
    });
    $("ceDestList").addEventListener("click", (e) => {
      const preview = e.target.closest('[data-role="ce-dest-preview"]');
      if (preview) { ceOpenDestPreview(preview.dataset.path); return; }
      const btn = e.target.closest('[data-role="ce-dest-remove"]');
      if (!btn) return;
      S.cardEater.destinations = S.cardEater.destinations.filter((p) => p !== btn.dataset.path);
      ceRenderDestinations();
      ceScheduleCollisionCheck();
    });

    // job launcher (progress/pause/resume/cancel now live in the Jobs drawer)
    $("ceStartJob").addEventListener("click", ceHandleStartJob);
    $("ceHistoryBtn").addEventListener("click", ceOpenHistory);

    // modals
    $("ceSummaryModal").addEventListener("click", (e) => {
      if (e.target.id === "ceSummaryModal") { $("ceSummaryModal").hidden = true; return; }
      const sendBtn = e.target.closest('[data-action="cc-send-broll"]');
      if (sendBtn) {
        $("ceSummaryModal").hidden = true;
        ceSendToBroll(sendBtn.dataset.path);
        return;
      }
      const pipelineBtn = e.target.closest('[data-action="cc-run-pipeline"]');
      if (pipelineBtn) {
        $("ceSummaryModal").hidden = true;
        openSuitePipeline(pipelineBtn.dataset.path);
      }
    });
    $("ceSummaryClose").addEventListener("click", () => { $("ceSummaryModal").hidden = true; });
    $("ceHistoryModal").addEventListener("click", (e) => { if (e.target.id === "ceHistoryModal") $("ceHistoryModal").hidden = true; });
    $("ceHistoryClose").addEventListener("click", () => { $("ceHistoryModal").hidden = true; });
    $("ceHistoryExport").addEventListener("click", ceExportHistoryCsv);
    $("ceDestPreviewModal").addEventListener("click", (e) => { if (e.target.id === "ceDestPreviewModal") $("ceDestPreviewModal").hidden = true; });
    $("ceDestPreviewClose").addEventListener("click", () => { $("ceDestPreviewModal").hidden = true; });

    ceApplyDraftToForm();
    ceRenderCardHeader();
    ceRenderViewer();
    ceRenderFileList();
    ceRenderDestinations();
    ceRenderPreview();
    ceStartActiveCardPolling();
    setInterval(ceCheckSafeToRemove, 3000);
    ceRefreshTemplates();
    ceRefreshFavorites();
  }

  async function boot() {
    wrapProjectLifecycleApiMethods();
    wireChrome();
    assertRcsHooks();
    wireCardEater();
    wireSync();
    wireTranscribe();
    wireBroll();
    wireSpyglass();
    wireHarmonize();
    wireGraphics();
    wireSuiteUndoKeys();
    injectSuiteTimeline();
    injectTranscriptSearch();
    injectTranscriptFavoriteStars();
    injectFavoritesTab();
    injectBrollFavoritesTab();
    wireCutsRowFavorites();
    injectPreviewFavoriteStar();
    fixEditTableColumnWidths();
    injectColumnResizeHandles();
    relocateRuntimeMeter();
    injectBrollSourceFilter();
    wireRcsSettingsPersistence();
    suppressRememberKeyCheckbox();
    relocateLlmProviderSettings();
    restoreSuiteSettings();
    restoreSuitePipelineSettings();
    switchWs("spyglass");

    await loadFavorites();
    refreshCutsRowFavoriteMarkers();

    const [models, defaults] = await Promise.all([
      call("transcriber_models"),
      call("brander_defaults"),
    ]);

    if (models.ok) {
      fillSelect($("tModel"), (models.models || []).map((m) => m.label), models.default_label);
    } else {
      fillSelect($("tModel"), ["(models unavailable)"]);
    }
    restoreTranscriberSettings();
    restoreBrollSettings();
    restoreRcsSettings();

    if (defaults.ok && defaults.scene) {
      populateGraphicsOptions(defaults);
      requestStillPreview();
    } else if (defaults.error) {
      toast(`Graphics engine unavailable: ${defaults.error}`, "error", 6000);
    }

    refreshTokenStatus();
    refreshBranderGeminiKeyStatus();

    // Default workspace: Edit if RCS has restorable state (its recovery
    // banner will be showing), otherwise Search.
    const a = suiteApi();
    if (a && typeof a.check_autosave === "function") {
      try {
        const rec = await a.check_autosave();
        if (rec && rec.ok && rec.available) switchWs("edit");
      } catch (err) { /* stay on Transcribe */ }
    }

    ensurePolling(); // one initial poll; stops by itself while idle
  }

  whenSuiteApiReady().then(boot);
})();
