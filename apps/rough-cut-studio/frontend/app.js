// app.js — talks to window.pywebview.api (see backend/api.py)

const state = {
  sources: {},       // source_id -> {segment_count, media_path, auto_linked}
  lastResult: null,
  editSegments: [],  // mutable working copy of resolved_segments for the Cuts tab
  previewingCid: null,  // _cid of whichever cut is loaded in the preview player, if any
  undoStack: [],      // snapshots of editSegments taken before each structural change
  redoStack: [],      // snapshots to restore via redo, populated by undo and cleared by new changes
  sequences: [],      // named sequence summaries for this project
  compareSelected: [], // up to 2 history indices checked for comparison
  currentProjectPath: null, // set once a project has been saved/loaded, enables Ctrl/Cmd+S quick-save
  transcriptModalSourceId: null, // source_id backing the currently-open transcript modal
  transcriptModalSegments: [],   // raw segments for the currently-open transcript modal, indexed to match data-idx
  exportFormat: "xmeml",   // which sub-tab is active in the consolidated Export tab: xmeml | fcpxml | otio
  exportPreviews: { xmeml: "", fcpxml: "", otio: "" },
  llamaModels: [],  // model names last fetched from the local Ollama install
  thumbnailCache: new Map(),  // "source_id::media_path::in_seconds" -> data URI, avoids re-fetching on every re-render
  cutsFilter: { source: "all", track: "all" },  // purely-visual Cuts-table filter; never touches editSegments
};

const MAX_UNDO_DEPTH = 20;

const el = (id) => document.getElementById(id);

function whenApiReady() {
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api) return resolve();
    window.addEventListener("pywebviewready", () => resolve());
  });
}

// ---------- ruler / ticking timecode ----------

let rulerFps = 25;
let rulerFrames = 0;
let rulerInterval = null;

function startRulerTicking() {
  el("ruler").classList.add("is-busy");
  rulerFrames = 0;
  clearInterval(rulerInterval);
  rulerInterval = setInterval(() => {
    rulerFrames++;
    el("rulerTc").textContent = framesToTc(rulerFrames, rulerFps);
  }, 1000 / rulerFps);
}

function stopRulerTicking(freezeAt = "00:00:00:00") {
  clearInterval(rulerInterval);
  el("ruler").classList.remove("is-busy");
  el("rulerTc").textContent = freezeAt;
}

function framesToTc(totalFrames, fps) {
  const fpsInt = Math.round(fps);
  const f = totalFrames % fpsInt;
  const totalSecs = Math.floor(totalFrames / fpsInt);
  const s = totalSecs % 60;
  const m = Math.floor(totalSecs / 60) % 60;
  const h = Math.floor(totalSecs / 3600);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)}:${pad(f)}`;
}

// ---------- status helper ----------

function setStatus(msg, kind) {
  const s = el("status");
  s.textContent = msg || "";
  s.className = "status" + (kind ? ` is-${kind}` : "");
}

// ---------- sources ----------

function renderSources() {
  const list = el("sourceList");
  list.innerHTML = "";
  const ids = Object.keys(state.sources);
  if (ids.length === 0) {
    const li = document.createElement("li");
    li.className = "block__hint";
    li.textContent = "No transcripts added yet.";
    list.appendChild(li);
    return;
  }
  ids.forEach((sourceId) => {
    const src = state.sources[sourceId];
    const li = document.createElement("li");
    li.className = "source-item";
    const linked = !!src.media_path;
    const linkLabel = linked
      ? (src.auto_linked ? "✓ auto-linked" : "✓ media linked")
      : "link media…";
    // sourceId/src.error are derived from user-picked filenames and
    // backend-reported text respectively -- escape them like every other
    // piece of user/file-derived text in this file (transcript content,
    // notes, sequence names, history labels) rather than trusting them not
    // to contain HTML-significant characters.
    const safeId = escapeHtml(sourceId);
    li.innerHTML = `
      <div class="source-item__row">
        <span class="source-item__name" title="${safeId}">${safeId}</span>
        <div class="source-item__actions">
          <button class="icon-btn" data-action="view" data-id="${safeId}">view</button>
          <button class="icon-btn ${linked ? "is-linked" : ""}" data-action="link" data-id="${safeId}">
            ${linkLabel}
          </button>
          <button class="icon-btn" data-action="remove" data-id="${safeId}">remove</button>
        </div>
      </div>
      <div class="source-item__meta">${src.segment_count} segments${src.error ? " · " + escapeHtml(src.error) : ""}</div>
    `;
    list.appendChild(li);
  });
}

async function addTranscripts() {
  try {
    const res = await window.pywebview.api.pick_transcript_files();
    if (!res || !res.ok) return;
    let autoLinkedCount = 0;
    (res.sources || []).forEach((s) => {
      state.sources[s.source_id] = s;
      if (s.auto_linked) autoLinkedCount++;
    });
    renderSources();
    if (autoLinkedCount > 0) {
      setStatus(`Added ${res.sources.length} transcript(s) — auto-linked media for ${autoLinkedCount} of them.`, "ok");
    }
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
  }
}

async function viewTranscript(sourceId) {
  let res;
  try {
    res = await window.pywebview.api.get_transcript_view(sourceId);
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
    return;
  }
  if (!res || !res.ok) {
    setStatus((res && res.error) || "Couldn't load that transcript.", "error");
    return;
  }
  el("transcriptModalTitle").textContent = `${sourceId} \u00b7 ${res.segment_count} segments`;
  state.transcriptModalSourceId = sourceId;
  state.transcriptModalSegments = res.segments;
  const body = el("transcriptModalBody");
  body.innerHTML = "";
  res.segments.forEach((seg) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${seg.index + 1}</td>
      <td>${escapeHtml(seg.start_tc)}</td>
      <td>${escapeHtml(seg.end_tc)}</td>
      <td>${seg.speaker ? escapeHtml(seg.speaker) : "\u2014"}</td>
      <td>${escapeHtml(seg.text)}</td>
      <td class="transcript-table__add-cell">
        <button class="row-btn" data-act="add-to-cuts" data-idx="${seg.index}" title="Add this segment to the bottom of the cut list">+ Add</button>
      </td>
    `;
    body.appendChild(tr);
  });
  openTranscriptModal();
}

// The element focused before the modal opened, so closing it restores focus
// instead of dropping it back to <body> (a keyboard/screen-reader trap).
let transcriptModalOpener = null;

function openTranscriptModal() {
  transcriptModalOpener = document.activeElement;
  el("transcriptModal").hidden = false;
  el("btnCloseTranscriptModal").focus();
}

function closeTranscriptModal() {
  el("transcriptModal").hidden = true;
  if (transcriptModalOpener && document.contains(transcriptModalOpener)) {
    transcriptModalOpener.focus();
  }
  transcriptModalOpener = null;
}

function getFocusableInTranscriptModal() {
  return Array.from(
    el("transcriptModal").querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
  ).filter((elm) => !elm.disabled && elm.offsetParent !== null);
}

el("transcriptModalBody").addEventListener("click", (e) => {
  const btn = e.target.closest('button[data-act="add-to-cuts"]');
  if (!btn) return;
  const seg = state.transcriptModalSegments[Number(btn.dataset.idx)];
  if (!seg) return;
  readEditTableIntoState(); // don't lose in-progress edits in other rows
  pushUndoSnapshot();
  const newSeg = {
    track: "main",
    source_id: state.transcriptModalSourceId,
    in_tc: seg.start_tc,
    out_tc: seg.end_tc,
    note: "",
    on_screen_text: "",
    source_text: seg.text || "",
    timeline_start_tc: "00:00:00:00",
  };
  state.editSegments.push(newSeg);
  appendCutRow(newSeg);
  setStatus(`Added segment ${seg.index + 1} from ${state.transcriptModalSourceId} to the bottom of the cut list.`, "ok");
});

el("btnCloseTranscriptModal").addEventListener("click", closeTranscriptModal);
el("transcriptModal").addEventListener("click", (e) => {
  if (e.target.id === "transcriptModal") closeTranscriptModal(); // click on the backdrop
});
document.addEventListener("keydown", (e) => {
  if (el("transcriptModal").hidden) return;
  if (e.key === "Escape") {
    closeTranscriptModal();
    return;
  }
  if (e.key !== "Tab") return;
  // Focus trap: keep Tab/Shift+Tab cycling within the modal instead of
  // escaping to controls behind the overlay.
  const focusable = getFocusableInTranscriptModal();
  if (focusable.length === 0) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
});

async function batchRelink() {
  let res;
  try {
    res = await window.pywebview.api.batch_relink_media();
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
    return;
  }
  if (!res) return;
  if (!res.ok) {
    if (!res.cancelled) setStatus(res.error || "Batch relink failed.", "error");
    return;
  }
  (res.sources || []).forEach((s) => {
    if (state.sources[s.source_id]) {
      state.sources[s.source_id].media_path = s.media_path;
      state.sources[s.source_id].auto_linked = false;
    }
  });
  renderSources();
  if (res.message) {
    setStatus(res.message, "ok");
  } else {
    const remaining = (res.still_unlinked || []).length;
    const msg = `Linked ${res.linked_count} source(s) from that folder` +
      (remaining ? `; ${remaining} still unlinked.` : ".");
    setStatus(msg, res.linked_count > 0 ? "ok" : "error");
  }
}

el("btnBatchRelink").addEventListener("click", batchRelink);

async function linkMedia(sourceId) {
  try {
    const res = await window.pywebview.api.link_media_file(sourceId);
    if (res && res.ok) {
      state.sources[sourceId].media_path = res.media_path;
      state.sources[sourceId].auto_linked = false;
      renderSources();
    } else if (res && !res.ok && !res.cancelled) {
      setStatus(res.error || "Couldn't link that media file.", "error");
    }
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
  }
}

async function removeSource(sourceId) {
  try {
    await window.pywebview.api.remove_source(sourceId);
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
    return;
  }
  delete state.sources[sourceId];
  renderSources();
}

el("sourceList").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const id = btn.dataset.id;
  if (btn.dataset.action === "link") linkMedia(id);
  if (btn.dataset.action === "remove") removeSource(id);
  if (btn.dataset.action === "view") viewTranscript(id);
});

el("btnAddTranscript").addEventListener("click", addTranscripts);

function updateDropFrameVisibility(dropFrameAvailable) {
  const row = el("dropFrameRow");
  row.hidden = !dropFrameAvailable;
  if (!dropFrameAvailable) {
    el("dropFrame").checked = false;
  }
}

el("fps").addEventListener("change", async (e) => {
  const fps = parseFloat(e.target.value);
  rulerFps = fps;
  const res = await window.pywebview.api.set_fps(fps);
  const available = res && res.drop_frame_available;
  updateDropFrameVisibility(available);
  if (!available) {
    await window.pywebview.api.set_drop_frame(false);
  }
});

el("dropFrame").addEventListener("change", async (e) => {
  await window.pywebview.api.set_drop_frame(e.target.checked);
});

// ---------- edit cuts table ----------

// Cuts don't have a stable id from the backend, but the preview player needs
// one to track "which row is this" across reorders, edits, and re-renders —
// an array index alone would silently point at the wrong cut the moment
// something moves. Assigned once when a cut enters state.editSegments and
// carried forward by object spread on every subsequent edit.
let _cidCounter = 0;
function newCid() {
  return "c" + (++_cidCounter) + "_" + Math.random().toString(36).slice(2, 7);
}

// Builds one <tr> for the Cuts table. Used both by the full-rebuild path
// (renderEditTable, needed for undo/redo, bulk actions, and applying a fresh
// generation/revision result -- all of which can legitimately touch many
// rows at once) and by the targeted single-row update paths below
// (delete/reorder/track-toggle/add), which do DOM surgery instead of a full
// rebuild so a single-row edit on a large cut list doesn't destroy and
// recreate every input/select in the table.
function buildRowElement(seg, i, total) {
  if (!seg._cid) seg._cid = newCid();
  const sourceIds = Object.keys(state.sources);
  const tr = document.createElement("tr");
  tr.dataset.idx = i;
  tr.dataset.cid = seg._cid;
  const isBroll = seg.track === "broll";
  const audioMode = seg.audio_mode || "silent";
  const options = sourceIds
    .map((id) => `<option value="${escapeHtml(id)}" ${id === seg.source_id ? "selected" : ""}>${escapeHtml(id)}</option>`)
    .join("");
  const startValue = isBroll
    ? (seg.timeline_start_tc || "00:00:00:00")
    : (seg.timeline_start_tc || "auto");

  const rowLabel = `cut ${i + 1}${seg.source_id ? " from " + escapeHtml(seg.source_id) : ""}`;
  tr.innerHTML = `
    <td class="select-cell"><input type="checkbox" class="row-select" aria-label="Select ${rowLabel}" ${seg._selected ? "checked" : ""}></td>
    <td class="reorder-cell">
      <div class="reorder-cell__inner">
        <span class="drag-handle" draggable="true" title="Drag to reorder (or use the ▲▼ buttons, or Alt+↑/↓ while focused in this row)">\u283f</span>
        <button class="row-btn" data-act="up" ${i === 0 ? "disabled" : ""} title="Move up" aria-label="Move ${rowLabel} up">▲</button>
        <button class="row-btn" data-act="down" ${i === total - 1 ? "disabled" : ""} title="Move down" aria-label="Move ${rowLabel} down">▼</button>
      </div>
    </td>
    <td class="thumb-cell"><div class="thumb-placeholder" data-thumb role="button" tabindex="0" aria-label="Preview ${rowLabel}"></div></td>
    <td>
      <select class="track-select" data-field="track">
        <option value="main" ${!isBroll ? "selected" : ""}>Main (V1)</option>
        <option value="broll" ${isBroll ? "selected" : ""}>B-Roll (V2)</option>
      </select>
    </td>
    <td><select class="src-select" data-field="source_id">${options}</select></td>
    <td><input type="text" class="tc-input" data-field="in_tc" value="${escapeHtml(seg.in_tc || "00:00:00:00")}"></td>
    <td><input type="text" class="tc-input" data-field="out_tc" value="${escapeHtml(seg.out_tc || "00:00:01:00")}"></td>
    <td>
      <input type="text" class="tc-input" data-field="timeline_start_tc" value="${escapeHtml(startValue)}" ${isBroll ? "" : "disabled"}>
      <span class="tc-conflict-msg" id="conflict-msg-${seg._cid}"></span>
    </td>
    <td class="audio-cell">
      <select data-field="audio_mode" ${isBroll ? "" : "disabled"}>
        <option value="silent" ${audioMode === "silent" ? "selected" : ""}>Silent</option>
        <option value="full" ${audioMode === "full" ? "selected" : ""}>Full Volume</option>
        <option value="duck_main" ${audioMode === "duck_main" ? "selected" : ""}>Duck Main</option>
      </select>
      <input type="number" class="duck-db-input" data-field="duck_db" step="1" max="0" min="-60"
             value="${seg.duck_db ?? -12}" title="How much to reduce the main track's volume, in dB"
             ${isBroll && audioMode === "duck_main" ? "" : "hidden"}>
    </td>
    <td class="script-text-cell" title="${escapeHtml(seg.source_text || "")}">${escapeHtml(seg.source_text || "")}</td>
    <td><input type="text" data-field="note" value="${escapeHtml(seg.note || "")}"></td>
    <td><input type="text" data-field="on_screen_text" value="${escapeHtml(seg.on_screen_text || "")}"></td>
    <td>
      <div class="reorder-cell__inner">
        <button class="row-btn" data-act="dup" title="Duplicate cut" aria-label="Duplicate ${rowLabel}">⧉</button>
        <button class="row-btn row-btn--danger" data-act="del" title="Delete cut" aria-label="Delete ${rowLabel}">✕</button>
      </div>
    </td>
  `;
  return tr;
}

function renderEditTable() {
  const body = el("editTableBody");
  body.innerHTML = "";

  if (state.editSegments.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="13" class="block__hint">No cuts yet — generate a script or add one manually.</td>`;
    body.appendChild(tr);
    updateBulkUI();
    refreshBrollOverlapWarnings();
    return;
  }

  state.editSegments.forEach((seg, i) => {
    const tr = buildRowElement(seg, i, state.editSegments.length);
    body.appendChild(tr);
    applyRowFilter(tr, seg);
    enqueueThumbnail(tr, seg);
  });

  highlightPreviewRow(state.previewingCid);
  updateBulkUI();
  refreshBrollOverlapWarnings();
}

// Walks the currently-rendered rows after a targeted (non-full-rebuild) edit
// and fixes up the two things that depend on row position: each row's
// data-idx (used everywhere to map a <tr> back to its state.editSegments
// entry) and the up/down buttons' disabled state at the list boundaries.
// Cheap even for large tables since it only touches attributes, not markup.
function renumberRows() {
  const rows = Array.from(el("editTableBody").querySelectorAll("tr[data-idx]"));
  rows.forEach((tr, i) => {
    tr.dataset.idx = i;
    const upBtn = tr.querySelector('button[data-act="up"]');
    const downBtn = tr.querySelector('button[data-act="down"]');
    if (upBtn) upBtn.disabled = i === 0;
    if (downBtn) downBtn.disabled = i === rows.length - 1;
  });
}

// Rebuilds a single row in place (used when a field change -- track or
// audio_mode -- alters that row's markup, e.g. Timeline Start's
// disabled/value or the duck-dB input's visibility) instead of tearing down
// and rebuilding the whole table.
function replaceRowElement(oldTr) {
  if (!oldTr) return;
  const idx = Number(oldTr.dataset.idx);
  const seg = state.editSegments[idx];
  if (!seg) return;
  const newTr = buildRowElement(seg, idx, state.editSegments.length);
  oldTr.replaceWith(newTr);
  applyRowFilter(newTr, seg);
  if (state.previewingCid != null) {
    newTr.classList.toggle("is-playing", newTr.dataset.cid === state.previewingCid);
  }
  enqueueThumbnail(newTr, seg);
}

// Appends exactly one new row (manual "+ Add Cut" and the transcript
// viewer's "+ Add") without touching any existing row's DOM node. Falls
// back to a full render when the table was previously empty, since that
// case has to replace the "No cuts yet" placeholder row anyway.
function appendCutRow(seg) {
  if (state.editSegments.length <= 1) {
    renderEditTable();
    return;
  }
  const body = el("editTableBody");
  const tr = buildRowElement(seg, state.editSegments.length - 1, state.editSegments.length);
  body.appendChild(tr);
  applyRowFilter(tr, seg);
  renumberRows(); // the previous last row's "down" button needs re-enabling
  enqueueThumbnail(tr, seg);
  updateBulkUI();
}

function highlightPreviewRow(cid) {
  document.querySelectorAll("#editTableBody tr[data-cid]").forEach((tr) => {
    tr.classList.toggle("is-playing", cid != null && tr.dataset.cid === cid);
  });
}

// ---------- cuts table filter + aggregate stats ----------
//
// The Source/Track filter above the Cuts table is purely visual: it only
// ever toggles a <tr>'s `.hidden`, never touches state.editSegments' order
// or contents, never reassigns a data-idx, and never affects the up/down
// boundary logic in renumberRows()/moveRow() (which operate on true array
// position, not visual position -- a filtered-out row sitting at true index
// 0 still correctly blocks the first *visible* row from moving further up;
// that's an accepted trade-off of a purely-visual filter, not a bug).

function rowMatchesFilter(seg) {
  const f = state.cutsFilter;
  if (f.source !== "all" && seg.source_id !== f.source) return false;
  if (f.track !== "all") {
    const bucket = seg.track === "broll" ? "broll" : "main";
    if (bucket !== f.track) return false;
  }
  return true;
}

// Called after building or updating any row (renderEditTable's loop,
// appendCutRow, replaceRowElement, the duplicate-row insertion) so a freshly
// (re)built <tr> starts out correctly hidden/shown for the current filter.
function applyRowFilter(tr, seg) {
  tr.hidden = !rowMatchesFilter(seg);
}

// Re-applies the current filter to every already-rendered row without
// rebuilding anything -- used by the filter <select>s' own change handlers.
function applyFilterToAllRows() {
  document.querySelectorAll("#editTableBody tr[data-idx]").forEach((tr) => {
    const seg = state.editSegments[Number(tr.dataset.idx)];
    if (seg) applyRowFilter(tr, seg);
  });
}

el("cutsFilterSource").addEventListener("change", (e) => {
  state.cutsFilter.source = e.target.value;
  applyFilterToAllRows();
});

el("cutsFilterTrack").addEventListener("change", (e) => {
  state.cutsFilter.track = e.target.value;
  applyFilterToAllRows();
});

// No existing "Xm YYs" helper elsewhere in this file to reuse (framesToTc
// formats a running frame-accurate timecode, not a plain duration summary).
function formatShortDuration(totalSeconds) {
  const secs = Math.max(0, Math.round(totalSeconds || 0));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

// Summarizes the *full* cut list (independent of the current filter
// selection, so it always shows what's available to filter by) into
// #cutsStats, and refreshes #cutsFilterSource's options from the distinct
// source_ids actually present in state.editSegments (not state.sources, so
// the dropdown reflects what's actually in the cut list). Called from
// updateBulkUI(), which already runs on essentially every mutation path in
// this file (delete, duplicate, add, render, bulk actions).
function updateCutsStats() {
  const statsEl = el("cutsStats");
  const sourceSelect = el("cutsFilterSource");

  const perSource = new Map(); // source_id -> { count, seconds }
  state.editSegments.forEach((seg) => {
    if (!seg.source_id) return;
    const entry = perSource.get(seg.source_id) || { count: 0, seconds: 0 };
    entry.count++;
    // in_seconds/out_seconds can be stale/absent for manually-added or
    // duplicated rows that haven't been through Apply yet -- this is an
    // informational display, not authoritative, so just skip those rather
    // than erroring or showing a bogus negative duration.
    if (typeof seg.in_seconds === "number" && typeof seg.out_seconds === "number") {
      const d = seg.out_seconds - seg.in_seconds;
      if (d > 0) entry.seconds += d;
    }
    perSource.set(seg.source_id, entry);
  });

  // Repopulate the source filter dropdown, preserving the current selection
  // if it's still valid, resetting to "all" if that source has no cuts left.
  sourceSelect.innerHTML = '<option value="all">All sources</option>';
  perSource.forEach((_entry, sourceId) => {
    const opt = document.createElement("option");
    opt.value = sourceId;
    opt.textContent = sourceId;
    sourceSelect.appendChild(opt);
  });
  if (state.cutsFilter.source !== "all" && !perSource.has(state.cutsFilter.source)) {
    state.cutsFilter.source = "all";
  }
  sourceSelect.value = state.cutsFilter.source;

  if (state.editSegments.length === 0) {
    statsEl.textContent = "";
    return;
  }

  const totalCount = state.editSegments.length;
  let totalSeconds = 0;
  perSource.forEach((e) => { totalSeconds += e.seconds; });
  const perSourceLabel = Array.from(perSource.entries())
    .map(([sourceId, e]) => `${escapeHtml(sourceId)}: ${e.count} cuts, ${formatShortDuration(e.seconds)}`)
    .join(" · ");
  statsEl.innerHTML = `${totalCount} cuts · ${formatShortDuration(totalSeconds)} total — ${perSourceLabel}`;
}

// ---------- inline B-roll overlap warnings ----------

// Approximate, warning-only timecode-to-seconds conversion. Deliberately NOT
// the real thing: it ignores drop-frame semantics entirely (a plain
// H*3600+M*60+S+F/fps count), because this only feeds a live "heads up,
// these two B-roll clips probably overlap" indicator while editing -- the
// backend does the authoritative drop-frame-aware conversion on Apply, and
// that's the math that actually decides lane placement for export. Do not
// upgrade this to replicate that logic; it doesn't need to be exact, just
// close enough to flag overlaps as they're created.
function approxTcToSeconds(tc, fps) {
  if (!tc) return null;
  const match = /^(\d+):(\d+):(\d+)[:;](\d+)$/.exec(tc.trim());
  if (!match) return null;
  const h = Number(match[1]), m = Number(match[2]), s = Number(match[3]), f = Number(match[4]);
  return h * 3600 + m * 60 + s + f / fps;
}

// Recomputes which B-roll cuts overlap another B-roll cut and flags them on
// their Timeline Start input. Deliberately re-derives start/duration from
// timeline_start_tc/in_tc/out_tc via approxTcToSeconds() rather than trusting
// timeline_start_seconds/in_seconds/out_seconds -- those only get recomputed
// by the backend on Apply, so they can be stale after a manual, unapplied
// timecode edit. Idempotent: clears the flag from any row no longer in
// conflict, since edits can resolve overlaps as easily as create them.
function refreshBrollOverlapWarnings() {
  const ranges = []; // { cid, start, end }
  state.editSegments.forEach((seg) => {
    if (seg.track !== "broll") return;
    const start = approxTcToSeconds(seg.timeline_start_tc, rulerFps);
    const inSec = approxTcToSeconds(seg.in_tc, rulerFps);
    const outSec = approxTcToSeconds(seg.out_tc, rulerFps);
    if (start == null || inSec == null || outSec == null) return;
    const duration = outSec - inSec;
    if (duration <= 0) return;
    if (!seg._cid) seg._cid = newCid();
    ranges.push({ cid: seg._cid, start, end: start + duration });
  });

  // Plain O(n^2) pairwise comparison -- B-roll counts are small in practice
  // (mirrors what the backend's own lane-assignment pass already does).
  const conflicted = new Set();
  for (let i = 0; i < ranges.length; i++) {
    for (let j = i + 1; j < ranges.length; j++) {
      const a = ranges[i], b = ranges[j];
      if (a.start < b.end && b.start < a.end) {
        conflicted.add(a.cid);
        conflicted.add(b.cid);
      }
    }
  }

  const CONFLICT_TITLE = "Overlaps another B-roll clip — will be placed on an additional track/lane after Apply.";
  const CONFLICT_MSG = "⚠ overlaps another B-roll clip";
  document.querySelectorAll("#editTableBody tr[data-cid]").forEach((tr) => {
    const input = tr.querySelector('.tc-input[data-field="timeline_start_tc"]');
    if (!input) return;
    const hasConflict = conflicted.has(tr.dataset.cid);
    input.classList.toggle("tc-input--conflict", hasConflict);
    const msgEl = tr.querySelector(".tc-conflict-msg");
    if (hasConflict) {
      input.title = CONFLICT_TITLE;
      input.setAttribute("aria-invalid", "true");
      if (msgEl) {
        input.setAttribute("aria-describedby", msgEl.id);
        msgEl.textContent = CONFLICT_MSG;
      }
    } else {
      if (input.title === CONFLICT_TITLE) input.title = "";
      input.removeAttribute("aria-invalid");
      input.removeAttribute("aria-describedby");
      if (msgEl) msgEl.textContent = "";
    }
  });
}

// ---------- undo/redo (structural changes, plus committed timecode-field
// edits — free-text fields like note/on-screen-text still rely purely on
// the browser's own native undo inside the input, so we don't shadow that
// for them. Timecode fields are different: a retime is normally a full-value
// replacement typed and then committed on blur, not incremental typing, so
// there's no useful native-undo trail to preserve there the way there is for
// prose — see the focusin/change listeners on editTableBody below for how a
// pre-edit snapshot is captured at focus time and pushed at commit time) ----------

// Snapshot of state.editSegments captured when a .tc-input gains focus, so a
// committed edit (native "change" on blur) can push an undo step representing
// the state *before* the edit. There's no other way to get a "before" value
// for a text input, since the DOM already holds the new value by the time
// "change" fires. Null whenever no tc-input edit is in progress.
let tcEditSnapshot = null;

// Pushes any pending tc-input pre-edit snapshot onto the undo stack as its
// own, separate, earlier entry, then clears it. Needed by any handler that
// mutates state.editSegments and pushes its own undo snapshot (via
// pushUndoSnapshot()) while a .tc-input could still be focused with an
// uncommitted edit — e.g. Alt+Up/Alt+Down reordering the row a timecode
// field is being edited in without blurring it first. Without this, the
// tc-input's own "change" listener would push tcEditSnapshot *after* the
// other handler's snapshot once the field is eventually blurred, even
// though tcEditSnapshot represents an *older* state — corrupting the
// undo stack's chronological order. Calling this first restores correct
// ordering: [..., tcEditSnapshot (older), the other handler's snapshot
// (newer)]. A no-op if no tc-input edit is in progress.
function flushTcEditSnapshot() {
  if (!tcEditSnapshot) return;
  state.undoStack.push(tcEditSnapshot);
  if (state.undoStack.length > MAX_UNDO_DEPTH) state.undoStack.shift();
  state.redoStack = [];
  updateUndoRedoUI();
  tcEditSnapshot = null;
}

function pushUndoSnapshot() {
  state.undoStack.push(JSON.parse(JSON.stringify(state.editSegments)));
  if (state.undoStack.length > MAX_UNDO_DEPTH) state.undoStack.shift();
  state.redoStack = []; // a new change starts a new branch — the old "future" is gone
  updateUndoRedoUI();
}

function updateUndoRedoUI() {
  el("btnUndo").disabled = state.undoStack.length === 0;
  el("btnRedo").disabled = state.redoStack.length === 0;
}

function undoLastChange() {
  if (state.undoStack.length === 0) {
    setStatus("Nothing to undo.", "error");
    return;
  }
  state.redoStack.push(JSON.parse(JSON.stringify(state.editSegments)));
  if (state.redoStack.length > MAX_UNDO_DEPTH) state.redoStack.shift();
  state.editSegments = state.undoStack.pop();
  renderEditTable();
  updateUndoRedoUI();
  setStatus("Undid last change.", "ok");
}

function redoLastChange() {
  if (state.redoStack.length === 0) {
    setStatus("Nothing to redo.", "error");
    return;
  }
  state.undoStack.push(JSON.parse(JSON.stringify(state.editSegments)));
  if (state.undoStack.length > MAX_UNDO_DEPTH) state.undoStack.shift();
  state.editSegments = state.redoStack.pop();
  renderEditTable();
  updateUndoRedoUI();
  setStatus("Redid last change.", "ok");
}

el("btnUndo").addEventListener("click", undoLastChange);
el("btnRedo").addEventListener("click", redoLastChange);

document.addEventListener("keydown", (e) => {
  const key = e.key.toLowerCase();
  const cmdOrCtrl = e.metaKey || e.ctrlKey;
  const isUndo = cmdOrCtrl && !e.shiftKey && key === "z";
  const isRedo = (cmdOrCtrl && e.shiftKey && key === "z") || (e.ctrlKey && !e.metaKey && key === "y");
  if (!isUndo && !isRedo) return;

  const active = document.activeElement;
  const tag = active && active.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return; // let native undo handle text fields
  const editTab = el("tabEdit");
  if (!editTab || !editTab.classList.contains("is-active")) return;

  e.preventDefault();
  if (isRedo) redoLastChange();
  else undoLastChange();
});

// Writes a value into a specific cut's field both in state and, if that
// row is currently rendered, its live input — used by the preview note
// sync and by Set In/Set Out, so a change made outside the table (preview
// player) never requires a full re-render (which would re-fetch every
// thumbnail) just to stay consistent with the table.
function updateTableRowField(idx, field, value) {
  if (state.editSegments[idx]) state.editSegments[idx][field] = value;
  const input = document.querySelector(`#editTableBody tr[data-idx="${idx}"] [data-field="${field}"]`);
  if (input) input.value = value;
}

// ---------- bulk row selection ----------

function updateBulkUI() {
  const selected = state.editSegments.filter((s) => s._selected);
  const bar = el("bulkActions");
  bar.hidden = selected.length === 0;
  el("bulkCount").textContent = `${selected.length} selected`;

  // The header checkbox's own checked/indeterminate state is scoped to
  // currently-*visible* rows only (tr.hidden === false under the
  // Source/Track filter), not the full state.editSegments array -- same
  // convention as a header checkbox next to any filtered spreadsheet: it
  // should reflect what clicking "Select All" would actually do right now.
  // This must stay in sync with the #selectAllRows change handler below,
  // which is scoped the same way. The "N selected" count above intentionally
  // stays scoped to the *full* list, not just visible rows -- a row selected
  // while visible and then hidden by a later filter change should keep
  // counting as selected rather than silently losing its _selected flag.
  const visibleSegs = Array.from(document.querySelectorAll("#editTableBody tr[data-idx]"))
    .filter((tr) => !tr.hidden)
    .map((tr) => state.editSegments[Number(tr.dataset.idx)])
    .filter(Boolean);
  const visibleSelectedCount = visibleSegs.filter((s) => s._selected).length;

  const selectAll = el("selectAllRows");
  if (visibleSegs.length === 0) {
    selectAll.checked = false;
    selectAll.indeterminate = false;
  } else {
    selectAll.checked = visibleSelectedCount === visibleSegs.length;
    selectAll.indeterminate = visibleSelectedCount > 0 && visibleSelectedCount < visibleSegs.length;
  }

  updateCutsStats();
}

// Timecode fields (in/out/timeline-start) get their own app-level undo step
// on commit, unlike other free-text inputs (note/on-screen-text) — see the
// comment above tcEditSnapshot's declaration for why. "focusin" (unlike
// "focus") bubbles, so a single delegated listener on editTableBody catches
// focus entering any .tc-input in any row, even ones added/rebuilt later.
el("editTableBody").addEventListener("focusin", (e) => {
  if (!e.target.classList || !e.target.classList.contains("tc-input")) return;
  // Sync any other in-progress edits first, so the captured "before" state
  // is accurate — same rule every other mutating handler in this file
  // follows (see the undo/redo section comment).
  readEditTableIntoState();
  tcEditSnapshot = JSON.parse(JSON.stringify(state.editSegments));
});

// A plain text input's native "change" event only fires on blur/commit if
// the value actually changed, so this only fires for real retimes, never a
// focus-then-blur with no edit — no manual before/after comparison needed.
el("editTableBody").addEventListener("change", (e) => {
  if (!e.target.classList.contains("tc-input")) return;
  if (tcEditSnapshot) {
    state.undoStack.push(tcEditSnapshot); // the pre-edit snapshot, not a fresh one
    if (state.undoStack.length > MAX_UNDO_DEPTH) state.undoStack.shift();
    state.redoStack = [];
    updateUndoRedoUI();
    tcEditSnapshot = null;
  }
  readEditTableIntoState();
  refreshBrollOverlapWarnings(); // a direct timeline-start (or in/out) edit can change overlap membership
});

el("editTableBody").addEventListener("change", (e) => {
  if (!e.target.classList.contains("row-select")) return;
  const tr = e.target.closest("tr[data-idx]");
  const idx = Number(tr.dataset.idx);
  if (state.editSegments[idx]) state.editSegments[idx]._selected = e.target.checked;
  updateBulkUI();
});

// Scoped to currently-*visible* rows only (tr.hidden === false under the
// Source/Track filter). The filter never removes rows from
// state.editSegments (see the comment above rowMatchesFilter/
// applyRowFilter) -- it only hides their <tr>s -- so an unscoped "select
// all" here would silently select rows the user filtered out and never
// saw, which is a real data-loss risk once combined with Bulk Delete.
// Don't "simplify" this back to iterating state.editSegments directly.
el("selectAllRows").addEventListener("change", (e) => {
  const checked = e.target.checked;
  document.querySelectorAll("#editTableBody tr[data-idx]").forEach((tr) => {
    if (tr.hidden) return;
    const seg = state.editSegments[Number(tr.dataset.idx)];
    if (seg) seg._selected = checked;
    const cb = tr.querySelector(".row-select");
    if (cb) cb.checked = checked;
  });
  updateBulkUI();
});

el("btnBulkDelete").addEventListener("click", () => {
  readEditTableIntoState();
  const remaining = state.editSegments.filter((s) => !s._selected);
  const removedCount = state.editSegments.length - remaining.length;
  if (removedCount === 0) return;
  pushUndoSnapshot();
  state.editSegments = remaining;
  renderEditTable();
  setStatus(`Deleted ${removedCount} cut(s).`, "ok");
});

el("btnBulkSetTrack").addEventListener("click", () => {
  readEditTableIntoState();
  const newTrack = el("bulkTrackSelect").value;
  const affected = state.editSegments.filter((s) => s._selected);
  if (affected.length === 0) return;
  pushUndoSnapshot();
  affected.forEach((s) => {
    s.track = newTrack;
    if (newTrack === "broll" && !s.timeline_start_tc) s.timeline_start_tc = "00:00:00:00";
  });
  renderEditTable();
  setStatus(`Set ${affected.length} cut(s) to ${newTrack === "broll" ? "B-Roll" : "Main"}.`, "ok");
});

// ---------- drag-and-drop reordering ----------

let dragSrcIndex = null;

el("editTableBody").addEventListener("dragstart", (e) => {
  const tr = e.target.closest("tr[data-idx]");
  if (!tr) return;
  dragSrcIndex = Number(tr.dataset.idx);
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", String(dragSrcIndex));
  tr.classList.add("is-dragging");
});

el("editTableBody").addEventListener("dragend", () => {
  document.querySelectorAll("#editTableBody tr").forEach((tr) => {
    tr.classList.remove("is-dragging", "drag-over-top", "drag-over-bottom");
  });
  dragSrcIndex = null;
});

el("editTableBody").addEventListener("dragover", (e) => {
  if (dragSrcIndex === null) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
  const tr = e.target.closest("tr[data-idx]");
  if (!tr) return;
  document.querySelectorAll("#editTableBody tr").forEach((r) => {
    if (r !== tr) r.classList.remove("drag-over-top", "drag-over-bottom");
  });
  const rect = tr.getBoundingClientRect();
  const isAfter = (e.clientY - rect.top) > rect.height / 2;
  tr.classList.toggle("drag-over-bottom", isAfter);
  tr.classList.toggle("drag-over-top", !isAfter);
});

el("editTableBody").addEventListener("drop", (e) => {
  e.preventDefault();
  const tr = e.target.closest("tr[data-idx]");
  document.querySelectorAll("#editTableBody tr").forEach((r) => {
    r.classList.remove("drag-over-top", "drag-over-bottom");
  });
  if (!tr || dragSrcIndex === null) return;

  const targetIdx = Number(tr.dataset.idx);
  const rect = tr.getBoundingClientRect();
  const isAfter = (e.clientY - rect.top) > rect.height / 2;
  let insertIdx = isAfter ? targetIdx + 1 : targetIdx;
  if (insertIdx === dragSrcIndex || insertIdx === dragSrcIndex + 1) {
    dragSrcIndex = null;
    return; // dropped back where it started — no-op
  }

  readEditTableIntoState(); // capture any in-progress text edits before we mutate the array
  pushUndoSnapshot();
  const body = el("editTableBody");
  const srcTr = body.querySelector(`tr[data-idx="${dragSrcIndex}"]`);
  const arr = state.editSegments;
  const [moved] = arr.splice(dragSrcIndex, 1);
  if (dragSrcIndex < insertIdx) insertIdx--; // shift target left to account for the removal
  arr.splice(insertIdx, 0, moved);
  dragSrcIndex = null;

  // Move the actual dragged <tr> node to its new DOM position rather than
  // rebuilding the table — preserves every other row's element identity
  // (and, next pass, focus during a keyboard-driven reorder).
  if (srcTr) {
    if (isAfter) {
      tr.insertAdjacentElement("afterend", srcTr);
    } else {
      tr.insertAdjacentElement("beforebegin", srcTr);
    }
  }
  renumberRows();
  refreshBrollOverlapWarnings(); // reordering alone doesn't change overlap membership, but keep this consistent
});

// Thumbnail loads go through a small concurrency-limited queue rather than
// firing one js_api call per row unconditionally. Each call is its own
// pywebview dispatch that the backend serves by shelling out to ffmpeg
// synchronously (see backend/thumbnails.py) — on a large cut list (100-500+
// rows), rendering the table without a cap would fire that many concurrent
// ffmpeg processes at once. Every call site that wants a row's thumbnail
// loaded (the full-render loop and the incremental insert/replace paths
// above) should go through enqueueThumbnail() instead of calling
// loadRowThumbnail() directly.
const THUMBNAIL_CONCURRENCY = 4;
const thumbnailQueue = [];
let activeThumbnailLoads = 0;

function enqueueThumbnail(tr, seg) {
  thumbnailQueue.push({ tr, seg });
  pumpThumbnailQueue();
}

function pumpThumbnailQueue() {
  while (activeThumbnailLoads < THUMBNAIL_CONCURRENCY && thumbnailQueue.length > 0) {
    const { tr, seg } = thumbnailQueue.shift();
    // The row may have been removed by a later edit while it sat in the
    // queue — skip it rather than spending an in-flight backend call on a
    // row that's no longer in the table. (loadRowThumbnail already guards
    // against swapping into a detached placeholder; this is just an
    // up-front check to avoid wasting the call in the first place.)
    if (!tr.isConnected) continue;
    activeThumbnailLoads++;
    loadRowThumbnail(tr, seg).finally(() => {
      activeThumbnailLoads--;
      pumpThumbnailQueue();
    });
  }
}

function makeThumbImg(dataUri, holder) {
  const img = document.createElement("img");
  img.className = "thumb-img";
  img.src = dataUri;
  // Carry the placeholder's accessible name/role/focusability forward —
  // replaceWith() swaps the element entirely, so these aren't inherited.
  img.setAttribute("role", "button");
  img.setAttribute("tabindex", "0");
  const label = holder.getAttribute("aria-label");
  if (label) img.setAttribute("aria-label", label);
  return img;
}

async function loadRowThumbnail(tr, seg) {
  const holder = tr.querySelector("[data-thumb]");
  if (!holder || !seg.source_id || seg.in_seconds == null) return;

  // Includes media_path so relinking a source's media invalidates any
  // thumbnail cached under the old file instead of showing a stale frame.
  const mediaPath = state.sources[seg.source_id]?.media_path || "";
  const cacheKey = `${seg.source_id}::${mediaPath}::${seg.in_seconds}`;
  const cached = state.thumbnailCache.get(cacheKey);
  if (cached) {
    holder.replaceWith(makeThumbImg(cached, holder));
    return;
  }

  try {
    const res = await window.pywebview.api.get_thumbnail(seg.source_id, seg.in_seconds);
    if (res && res.ok && res.data_uri) {
      state.thumbnailCache.set(cacheKey, res.data_uri);
      // The row may have re-rendered while this call was in flight — only
      // swap if this exact placeholder element is still attached.
      if (holder.isConnected) holder.replaceWith(makeThumbImg(res.data_uri, holder));
    }
    // On failure, leave the placeholder — no media linked / no ffmpeg / etc.
    // is not worth interrupting the editor with an error for every row.
  } catch (e) {
    // Thumbnail failures are cosmetic; never let them break the table.
  }
}

// A monotonically increasing session id. Any in-flight async step from an
// older session checks this before acting, so clicking a new thumbnail (or
// closing the player) cleanly cancels whatever was playing/queued before —
// no stray timeupdate listeners or delayed loads from a previous preview.
let previewSession = 0;

function previewCut(seg) {
  if (!seg.source_id) return;
  if (!seg._cid) seg._cid = newCid();
  playPreviewQueue([seg._cid]);
}

function previewScript() {
  const cids = state.editSegments
    .filter((s) => (s.track || "main") !== "broll")
    .slice()
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
    .map((s) => {
      if (!s._cid) s._cid = newCid();
      return s._cid;
    });
  if (cids.length === 0) {
    setStatus("No main cuts to preview yet.", "error");
    return;
  }
  setStatus(`Previewing ${cids.length} cut(s) in order…`);
  playPreviewQueue(cids);
}

// playQueueStep attaches a fresh `timeupdate` (and sometimes `loadedmetadata`)
// listener to the single shared <video> element on every cut. Each handler
// checks `mySession` and self-removes -- but only the next time its event
// actually fires. If a session is superseded/closed and that event never
// fires again (e.g. metadata never loads, or the player is closed instead of
// advancing), the stale closure stays bound to the element indefinitely
// (video.load() resets playback state but does NOT clear addEventListener
// listeners). These two trackers let each new step -- and closePreview --
// proactively remove whatever the previous step attached, so at most one of
// each listener is ever live on the element.
let _activeTimeUpdateHandler = null;
let _activeLoadedMetadataHandler = null;

function _clearPreviewVideoListeners(video) {
  if (_activeTimeUpdateHandler) {
    video.removeEventListener("timeupdate", _activeTimeUpdateHandler);
    _activeTimeUpdateHandler = null;
  }
  if (_activeLoadedMetadataHandler) {
    video.removeEventListener("loadedmetadata", _activeLoadedMetadataHandler);
    _activeLoadedMetadataHandler = null;
  }
}

function closePreview() {
  previewSession++; // invalidate anything in flight
  const video = el("previewVideo");
  _clearPreviewVideoListeners(video);
  video.pause();
  video.removeAttribute("src");
  video.load();
  el("previewPlayer").hidden = true;
  state.previewingCid = null;
  highlightPreviewRow(null);
}

function playPreviewQueue(cids) {
  const mySession = ++previewSession;
  playQueueStep(cids, 0, mySession);
}

async function playQueueStep(cids, pos, mySession) {
  if (mySession !== previewSession) return; // superseded by a newer preview
  const video = el("previewVideo");
  const player = el("previewPlayer");

  if (pos >= cids.length) {
    if (cids.length > 1) setStatus("Preview finished.", "ok");
    return;
  }

  const cid = cids[pos];
  const idx = state.editSegments.findIndex((s) => s._cid === cid);
  if (idx === -1) {
    // The cut was deleted mid-playback — skip it rather than stalling.
    playQueueStep(cids, pos + 1, mySession);
    return;
  }
  const seg = state.editSegments[idx];

  const res = await window.pywebview.api.get_preview_url(seg.source_id);
  if (mySession !== previewSession) return; // a newer preview started while we awaited

  if (!res || !res.ok) {
    setStatus(res && res.error ? res.error : "Couldn't load a preview for this cut.", "error");
    return;
  }

  player.hidden = false;
  state.previewingCid = cid;
  highlightPreviewRow(cid);
  populatePreviewInfo(seg, cids.length > 1 ? pos + 1 : null, cids.length);

  // Whatever the previous step (or a previous, now-superseded session)
  // attached is no longer relevant — drop it before wiring up this step's
  // own listeners so at most one of each is ever live on the element.
  _clearPreviewVideoListeners(video);

  const advance = () => {
    video.removeEventListener("timeupdate", onTimeUpdate);
    _activeTimeUpdateHandler = null;
    if (pos + 1 >= cids.length) {
      // Last (or only) cut in the queue — actually stop at its out-point
      // instead of falling through to playQueueStep, which would just
      // return via the `pos >= cids.length` guard above without ever
      // pausing, letting playback run on into whatever follows in the
      // source file.
      video.pause();
      if (cids.length > 1) setStatus("Preview finished.", "ok");
      return;
    }
    playQueueStep(cids, pos + 1, mySession);
  };

  const onTimeUpdate = () => {
    if (mySession !== previewSession) {
      video.removeEventListener("timeupdate", onTimeUpdate);
      _activeTimeUpdateHandler = null;
      return;
    }
    if (video.currentTime >= seg.out_seconds) advance();
  };

  const startPlayback = () => {
    _activeLoadedMetadataHandler = null; // this one just fired (or wasn't needed); nothing left to clear
    if (mySession !== previewSession) return;
    video.currentTime = seg.in_seconds;
    video.play().catch(() => {}); // autoplay can be blocked; controls still work either way
    video.addEventListener("timeupdate", onTimeUpdate);
    _activeTimeUpdateHandler = onTimeUpdate;
  };

  if (video.src !== res.url) {
    video.src = res.url;
    video.addEventListener("loadedmetadata", startPlayback, { once: true });
    _activeLoadedMetadataHandler = startPlayback;
  } else {
    startPlayback();
  }

  if (pos === 0) player.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function populatePreviewInfo(seg, position, total) {
  const title = document.querySelector(".preview-player__title");
  if (title) {
    title.textContent = position ? `Preview \u00b7 Cut ${position}/${total}` : "Preview";
  }
  el("previewSource").textContent = seg.source_name || seg.source_id || "";
  el("previewInOut").textContent = `${seg.in_tc || ""} \u2192 ${seg.out_tc || ""}`;

  const trackRow = el("previewTrackRow");
  if (seg.track === "broll") {
    trackRow.hidden = false;
    el("previewTrack").textContent = `B-Roll (V2) \u00b7 starts ${seg.timeline_start_tc || ""}`;
  } else {
    trackRow.hidden = true;
  }

  el("previewNote").value = seg.note || "";
}

// Editing the note here writes straight back into state.editSegments (by
// _cid, looked up fresh so it's correct even if the cut moved rows since
// the preview loaded) and mirrors the change into the table's own note
// field live, without a full re-render — a full render would spam the
// thumbnail API on every keystroke.
el("previewNote").addEventListener("input", (e) => {
  if (!state.previewingCid) return;
  const idx = state.editSegments.findIndex((s) => s._cid === state.previewingCid);
  if (idx === -1) return;
  updateTableRowField(idx, "note", e.target.value);
});

async function setInOutFromPlayhead(field) {
  if (!state.previewingCid) {
    setStatus("Preview a cut first, then Set In/Out from its playhead.", "error");
    return;
  }
  const idx = state.editSegments.findIndex((s) => s._cid === state.previewingCid);
  if (idx === -1) return;

  const seconds = el("previewVideo").currentTime;
  const res = await window.pywebview.api.format_timecode(seconds);
  if (!res || !res.ok) {
    setStatus(res && res.error ? res.error : "Couldn't read the playhead position.", "error");
    return;
  }

  // Flush any in-progress tc-input edit's pre-edit snapshot as its own,
  // earlier undo entry first — same reasoning as the Alt+Up/Alt+Down
  // handler (see flushTcEditSnapshot()'s comment): Set In/Out mutates
  // state.editSegments and pushes its own snapshot while an unrelated
  // timecode field could still be focused mid-edit, so without this the
  // two entries would land on the undo stack out of chronological order.
  flushTcEditSnapshot();
  readEditTableIntoState(); // don't lose in-progress edits in other rows
  pushUndoSnapshot();
  const tcField = field === "in" ? "in_tc" : "out_tc";
  const secField = field === "in" ? "in_seconds" : "out_seconds";
  updateTableRowField(idx, tcField, res.tc);
  state.editSegments[idx][secField] = res.seconds; // keep in sync for immediate re-preview/thumbnails
  el("previewInOut").textContent =
    `${state.editSegments[idx].in_tc || ""} \u2192 ${state.editSegments[idx].out_tc || ""}`;
  setStatus(`Set ${field === "in" ? "in" : "out"} point to ${res.tc}. Apply Changes to rebuild from it.`, "ok");
  // updateTableRowField() sets the tc-input's .value directly, which does not
  // fire a native "change" event -- so the tc-input change listener's own
  // refreshBrollOverlapWarnings() call never runs for this path. This can
  // change a B-roll cut's duration (in/out), so call it explicitly here too.
  refreshBrollOverlapWarnings();
}

el("btnSetIn").addEventListener("click", () => setInOutFromPlayhead("in"));
el("btnSetOut").addEventListener("click", () => setInOutFromPlayhead("out"));

el("btnClosePreview").addEventListener("click", closePreview);
el("btnPreviewScript").addEventListener("click", () => {
  readEditTableIntoState();
  previewScript();
});

function readEditTableIntoState() {
  const rows = el("editTableBody").querySelectorAll("tr[data-idx]");
  const updated = [];
  rows.forEach((tr) => {
    const get = (field) => tr.querySelector(`[data-field="${field}"]`)?.value ?? "";
    const original = state.editSegments[Number(tr.dataset.idx)] || {};
    const track = get("track") || "main";
    updated.push({
      ...original,
      track,
      source_id: get("source_id"),
      in_tc: get("in_tc"),
      out_tc: get("out_tc"),
      note: get("note"),
      on_screen_text: get("on_screen_text"),
      timeline_start_tc: track === "broll" ? get("timeline_start_tc") : original.timeline_start_tc,
      audio_mode: track === "broll" ? (get("audio_mode") || "silent") : original.audio_mode,
      duck_db: track === "broll" ? (parseFloat(get("duck_db")) || -12) : original.duck_db,
    });
  });
  state.editSegments = updated;
}

// Swaps a row with its previous/next sibling in both state.editSegments and
// the live DOM, without ever rebuilding either <tr> node, so the rows' own
// inputs/selects/buttons keep their identity — and, critically, keep focus
// — across the move. Shared by the ▲/▼ button clicks below and the
// Alt+Up/Alt+Down keyboard shortcut so the swap logic isn't duplicated.
// Callers are expected to have already checked list boundaries.
//
// Note: in both directions we move the *other* row via insertBefore, never
// `tr` itself. Moving `tr`'s own node to a position *before* an earlier
// sibling (e.g. `insertBefore(tr, prev)`) measurably blurs a focused
// descendant in this app's target webview even though `tr` stays connected
// throughout — so instead we always reposition the untouched sibling around
// the stationary `tr`, which reliably preserves focus.
function moveRow(tr, direction) {
  const idx = Number(tr.dataset.idx);
  if (direction === "up") {
    if (idx <= 0) return false;
    [state.editSegments[idx - 1], state.editSegments[idx]] = [state.editSegments[idx], state.editSegments[idx - 1]];
    const prev = tr.previousElementSibling;
    if (prev) tr.parentNode.insertBefore(prev, tr.nextSibling);
  } else {
    if (idx >= state.editSegments.length - 1) return false;
    [state.editSegments[idx + 1], state.editSegments[idx]] = [state.editSegments[idx], state.editSegments[idx + 1]];
    const next = tr.nextElementSibling;
    if (next) tr.parentNode.insertBefore(next, tr);
  }
  renumberRows();
  return true;
}

el("editTableBody").addEventListener("click", (e) => {
  const thumb = e.target.closest(".thumb-img, .thumb-placeholder");
  if (thumb) {
    const idx = Number(thumb.closest("tr").dataset.idx);
    const seg = state.editSegments[idx];
    if (seg) previewCut(seg);
    return;
  }

  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  readEditTableIntoState(); // don't lose in-progress edits in other rows
  const tr = btn.closest("tr[data-idx]");
  const idx = Number(tr.dataset.idx);
  const canDel = btn.dataset.act === "del";
  const canDup = btn.dataset.act === "dup";
  const canUp = btn.dataset.act === "up" && idx > 0;
  const canDown = btn.dataset.act === "down" && idx < state.editSegments.length - 1;
  if (!canDel && !canDup && !canUp && !canDown) return;
  pushUndoSnapshot();
  // Targeted DOM surgery instead of a full renderEditTable() — deleting,
  // duplicating, or reordering one row shouldn't rebuild (and re-fetch
  // thumbnails for) every other row in a list that can run into the hundreds.
  if (canDel) {
    state.editSegments.splice(idx, 1);
    if (state.editSegments.length === 0) {
      renderEditTable(); // switches back to the "No cuts yet" placeholder row
      return;
    }
    tr.remove();
    renumberRows();
    updateBulkUI(); // deleting a selected row can change the bulk-selection count
  } else if (canDup) {
    // Fresh _cid is required — the app relies on it for per-row identity
    // (preview highlighting, live note sync); everything else (including
    // any edits already made to note/on_screen_text/source_text) carries
    // over via the spread.
    const clone = { ...state.editSegments[idx], _cid: newCid() };
    state.editSegments.splice(idx + 1, 0, clone);
    const newTr = buildRowElement(clone, idx + 1, state.editSegments.length);
    tr.insertAdjacentElement("afterend", newTr);
    applyRowFilter(newTr, clone);
    renumberRows();
    enqueueThumbnail(newTr, clone);
    updateBulkUI(); // clone inherits _selected via the spread, so the bulk count can change
  } else if (canUp) {
    moveRow(tr, "up");
  } else if (canDown) {
    moveRow(tr, "down");
  }
  refreshBrollOverlapWarnings(); // delete can resolve an overlap; duplicate can introduce one
});

// Enter/Space activates the thumbnail preview control the same way a click
// does, since it's exposed as role="button" for keyboard/screen-reader use.
el("editTableBody").addEventListener("keydown", (e) => {
  // Alt+Up/Alt+Down reorders the row focus is currently inside (an input,
  // select, or button), mirroring the ▲/▼ buttons — moveRow() moves the
  // actual DOM node via insertBefore rather than rebuilding it, so the
  // focused element keeps focus across the move for free. preventDefault()
  // unconditionally (even at a list boundary) so the browser never does
  // anything native with Alt+Arrow inside the table.
  if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
    const tr = e.target.closest("tr[data-idx]");
    if (!tr) return;
    e.preventDefault();
    const idx = Number(tr.dataset.idx);
    const direction = e.key === "ArrowUp" ? "up" : "down";
    const atBoundary = direction === "up" ? idx === 0 : idx === state.editSegments.length - 1;
    if (atBoundary) return; // no-op, same as the disabled ▲/▼ button
    // Flush any in-progress tc-input edit's pre-edit snapshot as its own,
    // earlier undo entry first — see flushTcEditSnapshot()'s comment. This
    // must happen before pushUndoSnapshot() below so the two entries land
    // on the stack in correct chronological order.
    flushTcEditSnapshot();
    readEditTableIntoState(); // don't lose in-progress edits in other rows
    pushUndoSnapshot();
    moveRow(tr, direction);
    refreshBrollOverlapWarnings(); // reordering alone doesn't change overlap membership, but keep this consistent
    return;
  }

  if (e.key !== "Enter" && e.key !== " ") return;
  const thumb = e.target.closest(".thumb-img, .thumb-placeholder");
  if (!thumb) return;
  e.preventDefault();
  const idx = Number(thumb.closest("tr").dataset.idx);
  const seg = state.editSegments[idx];
  if (seg) previewCut(seg);
});

// Rebuild just this row on Track change so the Timeline Start field's
// enabled state (and its "auto" vs editable value) stays in sync
// immediately. Also rebuild on Audio Mode change so the duck-dB input
// shows/hides — that one's cosmetic enough not to warrant its own undo
// snapshot. A single row's markup changes here, so replaceRowElement()
// (build a fresh <tr> via buildRowElement() and swap it in) replaces what
// used to be a full renderEditTable() — no need to touch any other row.
el("editTableBody").addEventListener("change", (e) => {
  const field = e.target.dataset.field;
  if (field !== "track" && field !== "audio_mode") return;

  if (field !== "track") {
    readEditTableIntoState();
    replaceRowElement(e.target.closest("tr[data-idx]"));
    refreshBrollOverlapWarnings(); // audio_mode doesn't affect overlap, but keep this consistent
    return;
  }

  // Every other mutating handler calls readEditTableIntoState() *before*
  // pushUndoSnapshot(), so the snapshot captures the true pre-action state
  // including any in-progress edits in other rows. Snapshotting first (the
  // previous behavior here) captured whatever was last synced instead —
  // stale data from before this change — so an Undo could silently discard
  // edits made in other fields just before touching this dropdown. Fixed
  // by syncing everything first, then taking the snapshot with just this
  // row's track value reverted to what it was before the change.
  const tr = e.target.closest("tr[data-idx]");
  const idx = tr ? Number(tr.dataset.idx) : -1;
  const previousValue = idx !== -1 ? state.editSegments[idx]?.track : undefined;

  readEditTableIntoState();

  if (idx !== -1 && previousValue !== undefined) {
    const snapshotSegments = state.editSegments.map((s, i) =>
      i === idx ? { ...s, track: previousValue } : s
    );
    state.undoStack.push(JSON.parse(JSON.stringify(snapshotSegments)));
    if (state.undoStack.length > MAX_UNDO_DEPTH) state.undoStack.shift();
    state.redoStack = [];
    updateUndoRedoUI();
  }

  replaceRowElement(tr);
  refreshBrollOverlapWarnings(); // flipping a cut to/from B-roll changes overlap membership
});

el("btnAddCutRow").addEventListener("click", () => {
  readEditTableIntoState();
  pushUndoSnapshot();
  const firstSource = Object.keys(state.sources)[0] || "";
  const newSeg = {
    track: "main",
    source_id: firstSource,
    in_tc: "00:00:00:00",
    out_tc: "00:00:01:00",
    note: "",
    on_screen_text: "",
    timeline_start_tc: "00:00:00:00",
  };
  state.editSegments.push(newSeg);
  appendCutRow(newSeg);
});

el("btnApplyEdits").addEventListener("click", async () => {
  readEditTableIntoState();
  if (state.editSegments.length === 0) {
    setStatus("Add at least one cut first.", "error");
    return;
  }
  const sequenceName = el("sequenceName").value.trim() ||
    (state.lastResult ? state.lastResult.sequence_name : "Sequence");
  const targetDuration = el("targetDuration").value.trim();

  setStatus("Rebuilding script and XML from your edits…");
  try {
    const res = await window.pywebview.api.rebuild_outputs({
      sequence_name: sequenceName,
      target_duration: targetDuration || null,
      segments: state.editSegments,
    });
    if (!res.ok) {
      setStatus(res.error || "Couldn't rebuild from those edits.", "error");
      return;
    }
    applyGenerationResult(res, { keepEditTab: true });
    setStatus(`Rebuilt ${res.resolved_segments.length} cuts.`, "ok");
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
  }
});

el("btnRevise").addEventListener("click", async () => {
  const provider = getProvider();
  const instruction = el("reviseInstruction").value.trim();
  const apiKey = el("apiKey").value.trim();
  const ollamaHost = el("ollamaHost").value.trim() || "http://localhost:11434";
  const sequenceName = el("sequenceName").value.trim() ||
    (state.lastResult ? state.lastResult.sequence_name : "Sequence");
  const targetDuration = el("targetDuration").value.trim();
  const model = getSelectedModel();

  if (!state.lastResult) {
    setStatus("Generate a script first, then you can ask for a revision.", "error");
    return;
  }
  if (!instruction) { setStatus("Describe what you'd like to change first.", "error"); return; }
  if (provider === "gemini" && !apiKey) { setStatus("Enter your Gemini API key first.", "error"); return; }
  if (provider === "llama" && !model) { setStatus("Choose a Llama model first (Refresh if the list is empty).", "error"); return; }

  el("btnRevise").disabled = true;
  setStatus(provider === "llama" ? "Asking local Llama to revise the cut…" : "Asking Gemini to revise the cut…");
  startRulerTicking();

  try {
    const res = await window.pywebview.api.revise({
      provider,
      api_key: apiKey,
      ollama_host: ollamaHost,
      instruction,
      prompt: el("prompt").value,
      sequence_name: sequenceName,
      model,
      target_duration: targetDuration || null,
    });

    if (!res.ok) {
      setStatus(res.error || "Revision failed.", "error");
      stopRulerTicking();
      return;
    }

    applyGenerationResult(res, { keepEditTab: true });
    setStatus(`Revised — ${res.resolved_segments.length} cuts now.`, "ok");
    stopRulerTicking(framesToTc(0, rulerFps));
    el("reviseInstruction").value = "";
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
    stopRulerTicking();
  } finally {
    el("btnRevise").disabled = false;
  }
});

// ---------- generation ----------

window.onGenerationRetry = function (info) {
  // Called from Python (see backend/api.py::_notify_retry) while a
  // transient error (Gemini overload/rate-limit, or an Ollama connection
  // hiccup) is being retried in the background.
  setStatus(
    `Retrying (${info.reason}) — attempt ${info.attempt}/${info.max_attempts} in ${info.wait_seconds}s…`
  );
};

window.onGenerationStart = function (info) {
  // Called from Python (see backend/api.py::_notify_generation_start) once,
  // right as a Llama/Ollama request is actually sent — surfaces the context
  // window size that was chosen, since that's the main thing driving how
  // slow a local call will feel and is otherwise invisible.
  setStatus(`Running ${info.model} locally (context: ${info.num_ctx.toLocaleString()} tokens)…`);
};

function escapeHtml(str) {
  return (str || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function renderScriptMarkdown(md) {
  // Minimal, purpose-built markdown -> HTML for our own generated structure
  // (headers, a table, bold, and paragraphs). Not a general-purpose parser.
  const lines = md.split("\n");
  let html = "";
  let inTable = false;
  for (let raw of lines) {
    const line = raw;
    if (line.startsWith("# ")) { html += `<h1>${escapeHtml(line.slice(2))}</h1>`; continue; }
    if (line.startsWith("## ")) {
      if (inTable) { html += "</table>"; inTable = false; }
      html += `<h2>${escapeHtml(line.slice(3))}</h2>`;
      continue;
    }
    if (line.startsWith("|")) {
      const cells = line.split("|").slice(1, -1).map((c) => c.trim());
      if (cells.every((c) => /^-+$/.test(c))) continue; // separator row
      if (!inTable) { html += "<table>"; inTable = true; }
      const tag = html.endsWith("<table>") ? "th" : "td";
      html += "<tr>" + cells.map((c) => `<${tag}>${escapeHtml(c)}</${tag}>`).join("") + "</tr>";
      continue;
    }
    if (inTable) { html += "</table>"; inTable = false; }
    if (line.startsWith("_") && line.endsWith("_")) {
      html += `<p><em>${escapeHtml(line.slice(1, -1))}</em></p>`;
      continue;
    }
    if (line.trim() === "") continue;
    const bolded = escapeHtml(line).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html += `<p>${bolded}</p>`;
  }
  if (inTable) html += "</table>";
  return html;
}

function applyGenerationResult(res, opts = {}) {
  closePreview(); // the whole cut list is being replaced; any active preview is now stale
  state.undoStack = []; // undo/redo history doesn't carry across a full generate/rebuild/revise/restore
  state.redoStack = [];
  updateUndoRedoUI();
  state.lastResult = res;
  state.editSegments = (res.resolved_segments || []).map((s) => ({ ...s }));
  el("outputBlock").hidden = false;
  el("scriptPreview").innerHTML = renderScriptMarkdown(res.script_markdown);
  state.exportPreviews = {
    xmeml: res.xml_preview || "",
    fcpxml: res.fcpxml_preview || "",
    otio: res.otio_preview || "",
  };
  renderExportPreview();
  renderEditTable();
  renderDurationMeter(res.duration);
  if (res.history) renderHistory(res.history);

  const warnEl = el("warnings");
  warnEl.innerHTML = "";
  (res.warnings || []).forEach((w) => {
    const p = document.createElement("div");
    p.textContent = "⚠ " + w;
    warnEl.appendChild(p);
  });

  if (!opts.keepEditTab) {
    activateTab("script");
  }
}

function renderHistory(historyList) {
  const list = el("historyList");
  list.innerHTML = "";
  state.compareSelected = state.compareSelected.filter((idx) =>
    (historyList || []).some((h) => h.index === idx)
  );
  updateCompareButton();

  if (!historyList || historyList.length === 0) {
    list.innerHTML = `<li class="block__hint">Nothing yet — generate a script to start building history.</li>`;
    return;
  }
  historyList.forEach((h) => {
    const li = document.createElement("li");
    li.className = "history-item";
    const when = new Date(h.timestamp).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
    const brollLabel = h.broll_count ? `, ${h.broll_count} b-roll` : "";
    const checked = state.compareSelected.includes(h.index);
    li.innerHTML = `
      <div class="history-item__info">
        <span class="history-item__label">${escapeHtml(h.label)}</span>
        <span class="history-item__meta">${when} \u00b7 ${h.cut_count} cuts${brollLabel} \u00b7 ${h.runtime_label}</span>
      </div>
      <div class="history-item__actions">
        <label class="history-item__compare">
          <input type="checkbox" data-compare="${h.index}" ${checked ? "checked" : ""}> compare
        </label>
        <button class="btn btn--ghost" data-restore="${h.index}">Restore</button>
      </div>
    `;
    list.appendChild(li);
  });
}

function updateCompareButton() {
  const btn = el("btnCompareHistory");
  const n = state.compareSelected.length;
  btn.textContent = `Compare Selected (${n}/2)`;
  btn.disabled = n !== 2;
}

el("historyList").addEventListener("change", (e) => {
  const cb = e.target.closest("input[data-compare]");
  if (!cb) return;
  const index = Number(cb.dataset.compare);
  if (cb.checked) {
    if (state.compareSelected.length >= 2) {
      // Only ever keep the two most recently checked — drop the oldest.
      const dropped = state.compareSelected.shift();
      const oldCb = document.querySelector(`input[data-compare="${dropped}"]`);
      if (oldCb) oldCb.checked = false;
    }
    state.compareSelected.push(index);
  } else {
    state.compareSelected = state.compareSelected.filter((i) => i !== index);
  }
  updateCompareButton();
});

el("btnCompareHistory").addEventListener("click", async () => {
  if (state.compareSelected.length !== 2) return;
  const [a, b] = state.compareSelected;
  setStatus("Comparing versions…");
  try {
    const res = await window.pywebview.api.compare_history_entries(a, b);
    if (!res.ok) {
      setStatus(res.error || "Couldn't compare those versions.", "error");
      return;
    }
    renderHistoryCompare(res);
    setStatus("Comparison ready.", "ok");
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
  }
});

function renderHistoryCompare(res) {
  const panel = el("historyCompare");
  panel.hidden = false;
  el("compareLabelA").textContent = `${res.a.label} \u00b7 ${res.a.cut_count} cuts \u00b7 ${res.a.runtime_label}`;
  el("compareLabelB").textContent = `${res.b.label} \u00b7 ${res.b.cut_count} cuts \u00b7 ${res.b.runtime_label}`;

  const rowsEl = el("compareRows");
  rowsEl.innerHTML = "";
  const markFor = { same: "=", added: "+", removed: "\u2212", changed: "\u2260" };
  const cutText = (cut) => cut ? `${escapeHtml(cut.source_name || "")} <em>${escapeHtml(cut.in_tc || "")}\u2192${escapeHtml(cut.out_tc || "")}</em>${cut.note ? " \u00b7 " + escapeHtml(cut.note) : ""}` : "<em>\u2014</em>";

  res.rows.forEach((row) => {
    const div = document.createElement("div");
    div.className = `compare-row is-${row.type}`;
    div.innerHTML = `
      <span class="compare-row__mark">${markFor[row.type] || ""}</span>
      <span class="compare-row__cut">${cutText(row.a)}</span>
      <span class="compare-row__cut">${cutText(row.b)}</span>
    `;
    rowsEl.appendChild(div);
  });
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

el("btnCloseCompare").addEventListener("click", () => {
  el("historyCompare").hidden = true;
});

// ---------- named sequences ----------

function renderSequenceList(sequences) {
  state.sequences = sequences || [];
  const list = el("sequenceList");
  list.innerHTML = "";
  if (state.sequences.length === 0) {
    list.innerHTML = `<li class="block__hint">No saved sequences yet.</li>`;
    return;
  }
  state.sequences.forEach((s) => {
    const li = document.createElement("li");
    li.className = "sequence-item";
    const brollLabel = s.broll_count ? `, ${s.broll_count} b-roll` : "";
    li.innerHTML = `
      <div class="sequence-item__info">
        <span class="sequence-item__name" title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</span>
        <span class="sequence-item__meta">${s.cut_count} cuts${brollLabel}</span>
      </div>
      <div class="sequence-item__actions">
        <button class="icon-btn" data-seq-load="${escapeHtml(s.name)}">load</button>
        <button class="icon-btn" data-seq-delete="${escapeHtml(s.name)}">remove</button>
      </div>
    `;
    list.appendChild(li);
  });
}

el("btnSaveSequence").addEventListener("click", async () => {
  const nameInput = el("sequenceSaveName");
  const name = nameInput.value.trim();
  if (!name) {
    setStatus("Give the sequence a name first.", "error");
    return;
  }
  try {
    const res = await window.pywebview.api.save_sequence(name);
    if (!res.ok) {
      setStatus(res.error || "Couldn't save that sequence.", "error");
      return;
    }
    renderSequenceList(res.sequences);
    nameInput.value = "";
    setStatus(`Saved sequence "${name}".`, "ok");
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
  }
});

el("sequenceList").addEventListener("click", async (e) => {
  const loadBtn = e.target.closest("button[data-seq-load]");
  const delBtn = e.target.closest("button[data-seq-delete]");
  if (loadBtn) {
    const name = loadBtn.dataset.seqLoad;
    setStatus(`Loading sequence "${name}"…`);
    try {
      const res = await window.pywebview.api.load_sequence(name);
      if (!res.ok) {
        setStatus(res.error || "Couldn't load that sequence.", "error");
        return;
      }
      applyGenerationResult(res);
      if (res.sequences) renderSequenceList(res.sequences);
      setStatus(`Loaded sequence "${name}".`, "ok");
    } catch (err) {
      setStatus("Unexpected error: " + err, "error");
    }
  } else if (delBtn) {
    const name = delBtn.dataset.seqDelete;
    try {
      const res = await window.pywebview.api.delete_sequence(name);
      if (res.ok) renderSequenceList(res.sequences);
    } catch (err) {
      setStatus("Unexpected error: " + err, "error");
    }
  }
});

el("historyList").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-restore]");
  if (!btn) return;
  const index = btn.dataset.restore;
  setStatus("Restoring that version…");
  try {
    const res = await window.pywebview.api.restore_history_entry(index);
    if (!res.ok) {
      setStatus(res.error || "Couldn't restore that version.", "error");
      return;
    }
    applyGenerationResult(res);
    setStatus(`Restored: ${res.sequence_name}`, "ok");
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
  }
});

function renderDurationMeter(duration) {
  const meter = el("durationMeter");
  if (!duration) { meter.hidden = true; return; }

  meter.hidden = false;
  if (!duration.target_seconds) {
    meter.className = "duration-meter";
    meter.textContent = `Runtime: ${duration.main_runtime_label} (no target set)`;
    return;
  }

  const diff = duration.diff_seconds || 0;
  const diffLabel = diff === 0 ? "on target" : `${diff > 0 ? "+" : ""}${Math.round(diff)}s vs target`;
  meter.className = "duration-meter " + (duration.over_target ? "is-offtarget" : "is-ontarget");
  meter.textContent = `Runtime: ${duration.main_runtime_label} · Target: ${duration.target_label} · ${diffLabel}`;
}

// ---------- LLM provider (Gemini / local Llama via Ollama) ----------
//
// The provider and model are read fresh from these fields at the moment
// Generate or Revise is clicked (see generate() and the btnRevise handler)
// — there's no "locked in" provider for a session, so switching providers
// or models mid-session, including between a Generate and a later Revise,
// just works.

function getProvider() {
  return el("provider").value;
}

function getSelectedModel() {
  return getProvider() === "llama" ? el("llamaModel").value : el("model").value;
}

function updateProviderVisibility() {
  const isLlama = getProvider() === "llama";
  el("geminiSettings").hidden = isLlama;
  el("llamaSettings").hidden = !isLlama;
}

el("provider").addEventListener("change", () => {
  updateProviderVisibility();
  // First time someone switches to Llama in a session, try to populate the
  // model list automatically so they're not staring at an empty dropdown —
  // silently, since they may not have Ollama installed/running yet and a
  // loud error the moment they touch the dropdown would be unwelcome.
  if (getProvider() === "llama" && state.llamaModels.length === 0) {
    refreshLlamaModels({ silent: true });
  }
});

function renderLlamaModelOptions() {
  const select = el("llamaModel");
  const previousValue = select.value;
  select.innerHTML = "";
  if (state.llamaModels.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No models found — click Refresh";
    select.appendChild(opt);
    return;
  }
  state.llamaModels.forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    select.appendChild(opt);
  });
  if (state.llamaModels.includes(previousValue)) select.value = previousValue;
}

async function refreshLlamaModels(opts = {}) {
  const host = el("ollamaHost").value.trim() || "http://localhost:11434";
  const res = await window.pywebview.api.list_ollama_models(host);
  if (!res || !res.ok) {
    if (!opts.silent) {
      setStatus((res && res.error) || "Couldn't reach Ollama.", "error");
    }
    return;
  }
  state.llamaModels = res.models || [];
  renderLlamaModelOptions();
  if (!opts.silent) {
    setStatus(
      state.llamaModels.length > 0
        ? `Found ${state.llamaModels.length} local model(s).`
        : "Ollama is reachable, but no models are pulled yet — run `ollama pull llama3.1` in a terminal.",
      state.llamaModels.length > 0 ? "ok" : "error"
    );
  }
}

el("btnRefreshLlamaModels").addEventListener("click", () => refreshLlamaModels());

async function generate() {
  const provider = getProvider();
  const prompt = el("prompt").value.trim();
  const apiKey = el("apiKey").value.trim();
  const ollamaHost = el("ollamaHost").value.trim() || "http://localhost:11434";
  const sequenceName = el("sequenceName").value.trim();
  const model = getSelectedModel();
  const targetDuration = el("targetDuration").value.trim();

  if (Object.keys(state.sources).length === 0) {
    setStatus("Add at least one transcript first.", "error");
    return;
  }
  if (!prompt) { setStatus("Write a creative brief first.", "error"); return; }
  if (provider === "gemini" && !apiKey) { setStatus("Enter your Gemini API key first.", "error"); return; }
  if (provider === "llama" && !model) { setStatus("Choose a Llama model first (Refresh if the list is empty).", "error"); return; }

  el("btnGenerate").disabled = true;
  setStatus(provider === "llama" ? "Calling local Llama and building the cut…" : "Calling Gemini and building the cut…");
  startRulerTicking();

  if (provider === "gemini" && el("rememberKey").checked) {
    window.pywebview.api.save_api_key_to_disk(apiKey);
  }

  try {
    const res = await window.pywebview.api.generate({
      provider,
      api_key: apiKey,
      ollama_host: ollamaHost,
      prompt,
      sequence_name: sequenceName,
      model,
      target_duration: targetDuration || null,
    });

    if (!res.ok) {
      setStatus(res.error || "Generation failed.", "error");
      stopRulerTicking();
      return;
    }

    applyGenerationResult(res);
    setStatus(`Generated ${res.resolved_segments.length} cuts.`, "ok");
    stopRulerTicking(framesToTc(0, rulerFps));
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
    stopRulerTicking();
  } finally {
    el("btnGenerate").disabled = false;
  }
}

el("btnGenerate").addEventListener("click", generate);

// ---------- tabs ----------

// Single source of truth for switching the Script/Cuts/Export/History tab,
// used by the click handler below plus every other code path that jumps to
// a specific tab programmatically (applyGenerationResult, the load-project
// history fallback) -- keeps aria-selected in sync with the is-active class
// everywhere the active tab can change, not just on direct user clicks.
function activateTab(tabName) {
  document.querySelectorAll(".tab").forEach((t) => {
    const isActive = t.dataset.tab === tabName;
    t.classList.toggle("is-active", isActive);
    t.setAttribute("aria-selected", isActive ? "true" : "false");
  });
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("is-active"));
  el("tab" + tabName.charAt(0).toUpperCase() + tabName.slice(1)).classList.add("is-active");
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
});

// ---------- exports ----------

el("btnSaveScript").addEventListener("click", async () => {
  try {
    const res = await window.pywebview.api.save_script();
    if (res && res.ok) {
      setStatus("Script saved to " + res.path, "ok");
    } else if (res && !res.ok && !res.cancelled) {
      setStatus(res.error || "Couldn't save the script.", "error");
    }
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
  }
});

el("btnExportVideoPreview").addEventListener("click", async () => {
  const btn = el("btnExportVideoPreview");
  btn.disabled = true;
  setStatus("Rendering video preview — this can take a little while for longer cuts…");
  startRulerTicking();
  try {
    const res = await window.pywebview.api.export_video_preview();
    if (!res) return;
    if (!res.ok) {
      if (!res.cancelled) setStatus(res.error || "Couldn't export a video preview.", "error");
      return;
    }
    setStatus(`Exported ${res.cut_count} cut(s) to ${res.path}`, "ok");
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
  } finally {
    stopRulerTicking(framesToTc(0, rulerFps));
    btn.disabled = false;
  }
});

const EXPORT_FORMAT_META = {
  xmeml: { label: "Premiere Pro XML", saveLabel: "Save Premiere Pro XML…", saveFn: "save_xml", btnId: "exportFormatTabXmeml" },
  fcpxml: { label: "Final Cut Pro XML", saveLabel: "Save Final Cut Pro XML…", saveFn: "save_fcpxml", btnId: "exportFormatTabFcpxml" },
  otio: { label: "OTIO", saveLabel: "Save OTIO…", saveFn: "save_otio", btnId: "exportFormatTabOtio" },
};

// Unlike the Script/Cuts/Export/History tabs, the three export-format tabs
// all control the *same* single <pre id="exportPreview"> region rather than
// their own separate .tab-panel, so there's one shared tabpanel whose
// aria-labelledby has to be repointed at whichever tab is currently active
// instead of a fixed 1:1 tab->panel mapping.
function renderExportPreview() {
  const meta = EXPORT_FORMAT_META[state.exportFormat];
  const previewEl = el("exportPreview");
  previewEl.textContent = state.exportPreviews[state.exportFormat] || "";
  previewEl.setAttribute("aria-labelledby", meta.btnId);
  el("btnSaveExport").textContent = meta.saveLabel;
  document.querySelectorAll(".export-format-tab").forEach((btn) => {
    const isActive = btn.dataset.format === state.exportFormat;
    btn.classList.toggle("is-active", isActive);
    btn.setAttribute("aria-selected", isActive ? "true" : "false");
  });
}

el("exportFormatTabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".export-format-tab");
  if (!btn) return;
  state.exportFormat = btn.dataset.format;
  renderExportPreview();
});

el("btnSaveExport").addEventListener("click", async () => {
  const meta = EXPORT_FORMAT_META[state.exportFormat];
  try {
    const res = await window.pywebview.api[meta.saveFn]();
    if (res && res.ok) {
      setStatus(`${meta.label} saved to ` + res.path, "ok");
    } else if (res && !res.ok && !res.cancelled) {
      setStatus(res.error || `Couldn't save the ${meta.label}.`, "error");
    }
  } catch (err) {
    setStatus("Unexpected error: " + err, "error");
  }
});

// ---------- project save/resume ----------

function gatherProjectMeta() {
  return {
    sequence_name: el("sequenceName").value.trim(),
    prompt: el("prompt").value,
    provider: getProvider(),
    model: getSelectedModel(),
    ollama_host: el("ollamaHost").value.trim() || "http://localhost:11434",
    target_duration: el("targetDuration").value.trim() || null,
  };
}

el("btnNewProject").addEventListener("click", async () => {
  const hasWork = Object.keys(state.sources).length > 0 || state.editSegments.length > 0 || !!state.lastResult;
  if (hasWork && !confirm(
    "Start a new project? This clears all sources, cuts, and history from the current session. " +
    "Anything not already saved to a project file will be lost."
  )) {
    return;
  }

  const res = await window.pywebview.api.new_project();
  if (!res || !res.ok) {
    setStatus((res && res.error) || "Couldn't start a new project.", "error");
    return;
  }

  closePreview();

  state.sources = {};
  renderSources();

  state.lastResult = null;
  state.editSegments = [];
  state.previewingCid = null;
  state.undoStack = [];
  state.redoStack = [];
  updateUndoRedoUI();
  state.sequences = [];
  renderSequenceList([]);
  state.compareSelected = [];
  el("historyCompare").hidden = true;
  renderHistory([]);
  state.currentProjectPath = null;
  state.transcriptModalSourceId = null;
  state.transcriptModalSegments = [];
  state.exportFormat = "xmeml";
  state.exportPreviews = { xmeml: "", fcpxml: "", otio: "" };
  renderExportPreview();

  el("sequenceName").value = "";
  el("prompt").value = "";
  el("targetDuration").value = "";
  el("fps").value = "25";
  rulerFps = 25;
  updateDropFrameVisibility(false);
  el("dropFrame").checked = false;

  el("outputBlock").hidden = true;
  el("warnings").innerHTML = "";
  renderDurationMeter(null);

  setStatus("Started a new project.", "ok");
});

el("btnSaveProject").addEventListener("click", async () => {
  const res = await window.pywebview.api.save_project(gatherProjectMeta());
  if (res && res.ok) {
    state.currentProjectPath = res.path;
    setStatus("Project saved to " + res.path, "ok");
  } else if (res && res.error) {
    setStatus(res.error, "error");
  }
});

// Ctrl/Cmd+S: quick-save to the last-used path if we have one (no dialog),
// otherwise falls back to the normal Save Project dialog and remembers
// whatever path the user picks for next time.
async function quickSaveProject() {
  const meta = gatherProjectMeta();
  if (state.currentProjectPath) {
    const res = await window.pywebview.api.save_project_to_path(state.currentProjectPath, meta);
    if (res && res.ok) {
      setStatus("Saved to " + res.path, "ok");
      return;
    }
    // The remembered path stopped working (e.g. the file/folder moved) — fall
    // back to a fresh dialog rather than just failing silently.
  }
  const res = await window.pywebview.api.save_project(meta);
  if (res && res.ok) {
    state.currentProjectPath = res.path;
    setStatus("Project saved to " + res.path, "ok");
  } else if (res && res.error) {
    setStatus(res.error, "error");
  }
}

document.addEventListener("keydown", (e) => {
  const cmdOrCtrl = e.metaKey || e.ctrlKey;
  if (cmdOrCtrl && !e.shiftKey && e.key.toLowerCase() === "s") {
    e.preventDefault();
    quickSaveProject();
  }
});

el("btnLoadProject").addEventListener("click", async () => {
  const res = await window.pywebview.api.load_project();
  if (!res) return;
  if (!res.ok) {
    if (!res.cancelled) setStatus(res.error || "Couldn't load that project.", "error");
    return;
  }
  state.currentProjectPath = res.path || null;

  state.sources = {};
  (res.sources || []).forEach((s) => { state.sources[s.source_id] = s; });
  renderSources();

  state.compareSelected = [];
  el("historyCompare").hidden = true;
  renderSequenceList(res.sequences || []);

  el("sequenceName").value = res.sequence_name || "";
  el("prompt").value = res.prompt || "";
  el("provider").value = res.provider || "gemini";
  updateProviderVisibility();
  el("ollamaHost").value = res.ollama_host || "http://localhost:11434";
  if (res.provider === "llama") {
    refreshLlamaModels({ silent: true }).then(() => {
      if (!res.model) return;
      if (!state.llamaModels.includes(res.model)) {
        const opt = document.createElement("option");
        opt.value = res.model;
        opt.textContent = `${res.model} (not pulled — run \`ollama pull ${res.model}\`)`;
        el("llamaModel").appendChild(opt);
      }
      el("llamaModel").value = res.model;
    });
  } else if (res.model) {
    el("model").value = res.model;
  }
  if (res.fps) {
    el("fps").value = String(res.fps);
    rulerFps = res.fps;
  }
  const dfAvailable = res.fps === 29.97 || res.fps === 59.94;
  updateDropFrameVisibility(dfAvailable);
  el("dropFrame").checked = dfAvailable && !!res.drop_frame;
  el("targetDuration").value = res.target_seconds ? String(Math.round(res.target_seconds)) : "";

  if (res.resolved_segments) {
    applyGenerationResult(res);
  } else if (res.history && res.history.length > 0) {
    // Unusual, but possible: history exists without a "current" cut list.
    // Surface it via the History tab rather than hiding it entirely.
    el("outputBlock").hidden = false;
    state.editSegments = [];
    renderEditTable();
    renderHistory(res.history);
    activateTab("history");
  } else {
    el("outputBlock").hidden = true;
    state.editSegments = [];
    renderHistory([]);
  }

  let msg = `Loaded project with ${(res.sources || []).length} source(s).`;
  if (res.missing_files && res.missing_files.length) {
    msg += ` ${res.missing_files.length} file(s) from the project couldn't be found and were skipped.`;
  }
  setStatus(msg, res.missing_files && res.missing_files.length ? "error" : "ok");
});

// ---------- init ----------

whenApiReady().then(async () => {
  const saved = await window.pywebview.api.load_saved_api_key();
  if (saved && saved.ok && saved.api_key) {
    el("apiKey").value = saved.api_key;
  }
  renderSources();
  renderEditTable();
  renderHistory([]);
  renderSequenceList([]);
  renderExportPreview();
  updateProviderVisibility();
  renderLlamaModelOptions();
  updateUndoRedoUI();
  checkForRecovery();
  startAutosaveTimer();
});

// ---------- crash recovery ----------

async function checkForRecovery() {
  const res = await window.pywebview.api.check_autosave();
  if (!res || !res.ok || !res.available) return;
  const when = new Date(res.saved_at).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
  const unappliedNote = res.unapplied ? " (includes edits you hadn't applied yet)" : "";
  el("recoveryBannerText").textContent =
    `Found an unsaved session from ${when} — "${res.sequence_name || "Untitled"}", ${res.cut_count} cut(s)${unappliedNote}.`;
  el("recoveryBanner").hidden = false;
}

el("btnRestoreAutosave").addEventListener("click", async () => {
  const res = await window.pywebview.api.restore_autosave();
  el("recoveryBanner").hidden = true;
  if (!res || !res.ok) {
    setStatus((res && res.error) || "Couldn't restore that session.", "error");
    return;
  }
  state.sources = {};
  // restore_autosave doesn't re-send a source list the way load_project does
  // (it reuses _apply_loaded_project, which does populate self.sources
  // server-side) -- refresh the sidebar from the server's own view of it.
  const sources = await window.pywebview.api.list_sources();
  (sources || []).forEach((s) => { state.sources[s.source_id] = s; });
  renderSources();

  el("sequenceName").value = res.sequence_name || "";
  el("prompt").value = res.prompt || "";
  el("provider").value = res.provider || "gemini";
  updateProviderVisibility();
  el("ollamaHost").value = res.ollama_host || "http://localhost:11434";
  if (res.provider === "llama") {
    refreshLlamaModels({ silent: true }).then(() => {
      if (!res.model) return;
      if (!state.llamaModels.includes(res.model)) {
        const opt = document.createElement("option");
        opt.value = res.model;
        opt.textContent = `${res.model} (not pulled — run \`ollama pull ${res.model}\`)`;
        el("llamaModel").appendChild(opt);
      }
      el("llamaModel").value = res.model;
    });
  } else if (res.model) {
    el("model").value = res.model;
  }
  el("targetDuration").value = res.target_seconds ? String(Math.round(res.target_seconds)) : "";
  renderSequenceList(res.sequences || []);

  if (res.resolved_segments) {
    applyGenerationResult(res);
  } else {
    renderHistory(res.history || []);
  }
  setStatus("Session restored.", "ok");
});

el("btnDiscardAutosave").addEventListener("click", async () => {
  await window.pywebview.api.discard_autosave();
  el("recoveryBanner").hidden = true;
});

// ---------- periodic autosave of in-progress (unapplied) edits ----------

const AUTOSAVE_INTERVAL_MS = 45000;

function startAutosaveTimer() {
  setInterval(() => {
    // Nothing worth protecting yet — skip rather than write an empty snapshot.
    if (Object.keys(state.sources).length === 0 && state.editSegments.length === 0) return;
    readEditTableIntoState();
    window.pywebview.api.autosave_working_state({
      sequence_name: el("sequenceName").value.trim(),
      prompt: el("prompt").value,
      provider: getProvider(),
      model: getSelectedModel(),
      ollama_host: el("ollamaHost").value.trim() || "http://localhost:11434",
      target_duration: el("targetDuration").value.trim() || null,
      segments: state.editSegments,
    });
  }, AUTOSAVE_INTERVAL_MS);
}
