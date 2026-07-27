// colorize.js — Grade workspace: WebGL2 live color-grading preview, 3-way
// wheels, curves, HSL secondary bands, LUT import/apply, in/out trimming,
// scopes, and single/batch export.
//
// A separate top-level <script> from suite.js (not a section appended to
// it) — its own IIFE, with zero access to suite.js's internal `call`/`$`/
// `toast`/`switchWs` closures. That's a deliberate scope call (flagged in
// the integration plan): the WebGL shader pipeline, curve editor, and
// scope rendering here are substantial enough to warrant their own file
// rather than growing suite.js's already-7000-line single file further.
// Coupling to the rest of the suite chrome is intentionally loose: DOM
// ids defined in shell.html, a CustomEvent ("suite:workspace-changed")
// for visibility, and the same window.pywebview.api bridge every other
// workspace calls.
//
// Grade math note: apply_grade_to_rgb in apps/colorize/grade.py is the
// SOURCE OF TRUTH (it's what export bakes into a .cube LUT — see that
// module's docstring). The GLSL fragment shader below is a hand-kept
// translation of that exact same pipeline, stage-for-stage, so live
// preview matches exported output. If one changes, the other must too.
(function () {
  "use strict";

  // ==========================================================================
  // tiny utils (intentionally NOT shared with suite.js — see file header)
  // ==========================================================================

  const $ = (id) => document.getElementById(id);

  function esc(str) {
    return String(str == null ? "" : str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function basename(p) {
    return String(p || "").split(/[\\/]/).pop();
  }

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  function newLocalId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "");
    return Math.random().toString(16).slice(2) + Date.now().toString(16);
  }

  function suiteApi() {
    return (window.pywebview && window.pywebview.api) || null;
  }

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

  function toastIfError(res, fallback) {
    if (res.ok || res.cancelled) return;
    toast(res.error || fallback || "Something went wrong.", "error");
  }

  function formatTimecode(seconds) {
    seconds = Math.max(0, seconds || 0);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    const ms = Math.floor((seconds - Math.floor(seconds)) * 1000);
    const pad = (n, w = 2) => String(n).padStart(w, "0");
    return `${pad(h)}:${pad(m)}:${pad(s)}.${pad(ms, 3)}`;
  }

  // ==========================================================================
  // grade model (mirrors apps/colorize/grade.py's GradeState field-for-field
  // so JSON round-trips with the backend without any translation layer)
  // ==========================================================================

  const HSL_BANDS = [
    { key: "red", color: "#e0433c" },
    { key: "orange", color: "#e0a23c" },
    { key: "yellow", color: "#d9e03c" },
    { key: "green", color: "#63e03c" },
    { key: "aqua", color: "#3ce0b0" },
    { key: "blue", color: "#3c8fe0" },
    { key: "purple", color: "#6b3ce0" },
    { key: "magenta", color: "#c93ce0" },
  ];
  const IDENTITY_CURVE = [[0, 0], [1, 1]];

  function defaultGrade() {
    const hsl = {};
    HSL_BANDS.forEach((b) => { hsl[b.key] = { hue: 0, sat: 0, lum: 0 }; });
    return {
      exposure: 0, contrast: 0, temperature: 0, tint: 0, saturation: 0, vibrance: 0,
      highlights: 0, shadows: 0, whites: 0, blacks: 0,
      lift: [0, 0, 0], gamma: [0, 0, 0], gain: [0, 0, 0],
      hsl,
      curve_master: IDENTITY_CURVE.map((p) => p.slice()),
      curve_r: IDENTITY_CURVE.map((p) => p.slice()),
      curve_g: IDENTITY_CURVE.map((p) => p.slice()),
      curve_b: IDENTITY_CURVE.map((p) => p.slice()),
      lut_id: null,
      lut_intensity: 100,
    };
  }

  function cloneGrade(g) { return JSON.parse(JSON.stringify(g)); }

  // ==========================================================================
  // undo / redo history -- per-clip stacks of full grade snapshots, keyed by
  // clip id so switching the active clip never cross-contaminates history.
  // Interactive controls push exactly ONE snapshot per gesture (a whole
  // mouse drag or a single keypress), not per intermediate "input" event,
  // via the beginStroke()/endStroke() pair a stroke guard exposes.
  // ==========================================================================

  const GRADE_HISTORY = new Map(); // clip id -> { undo: [snapshot,...], redo: [...] }
  const HISTORY_LIMIT = 60;

  function historyFor(clipId) {
    let h = GRADE_HISTORY.get(clipId);
    if (!h) { h = { undo: [], redo: [] }; GRADE_HISTORY.set(clipId, h); }
    return h;
  }

  function snapshotClip(clip) {
    return { grade: cloneGrade(clip.grade), lut_id: clip.lut_id };
  }

  function pushUndoSnapshot() {
    const clip = activeClip();
    if (!clip) return;
    const h = historyFor(clip.id);
    h.undo.push(snapshotClip(clip));
    if (h.undo.length > HISTORY_LIMIT) h.undo.shift();
    h.redo.length = 0;
    updateHistoryButtons();
  }

  function makeStrokeGuard() {
    let active = false;
    return {
      begin() { if (!active) { pushUndoSnapshot(); active = true; } },
      end() { active = false; },
    };
  }

  function applyHistorySnapshot(snap) {
    const clip = activeClip();
    if (!clip || !snap) return;
    clip.grade = cloneGrade(snap.grade);
    clip.lut_id = snap.lut_id;
    buildCurveLUT();
    applyLutTexture(clip.lut_id);
    renderBasicPanel(); renderWheelsPanel(); renderCurvesPanel(); renderHslPanel(); renderLutPanel();
    renderMediaBin();
    requestRender();
  }

  function undo() {
    const clip = activeClip();
    if (!clip) return;
    const h = historyFor(clip.id);
    if (!h.undo.length) return;
    h.redo.push(snapshotClip(clip));
    applyHistorySnapshot(h.undo.pop());
    updateHistoryButtons();
  }

  function redo() {
    const clip = activeClip();
    if (!clip) return;
    const h = historyFor(clip.id);
    if (!h.redo.length) return;
    h.undo.push(snapshotClip(clip));
    applyHistorySnapshot(h.redo.pop());
    updateHistoryButtons();
  }

  function updateHistoryButtons() {
    const clip = activeClip();
    const h = clip ? historyFor(clip.id) : { undo: [], redo: [] };
    const undoBtn = $("czUndoBtn"), redoBtn = $("czRedoBtn");
    if (undoBtn) undoBtn.disabled = !h.undo.length;
    if (redoBtn) redoBtn.disabled = !h.redo.length;
  }

  function wireHistoryControls() {
    $("czUndoBtn").addEventListener("click", undo);
    $("czRedoBtn").addEventListener("click", redo);
    window.addEventListener("keydown", (e) => {
      if (!CZ.visible) return;
      if (!(e.metaKey || e.ctrlKey)) return;
      const key = e.key.toLowerCase();
      if (key !== "z" && key !== "y") return;
      const el = document.activeElement;
      const tag = (el && el.tagName) || "";
      const type = (el && el.type) || "";
      // Range sliders keep focus after a drag -- don't let the generic
      // "typing in a field" guard swallow Cmd+Z while one is focused.
      if ((tag === "INPUT" && type !== "range") || tag === "TEXTAREA" || tag === "SELECT") return;
      e.preventDefault();
      if (key === "y" || (key === "z" && e.shiftKey)) redo(); else undo();
    });
  }

  function newClip(sourcePath, probeInfo) {
    return {
      id: newLocalId(),
      source_path: sourcePath,
      in_seconds: 0,
      out_seconds: (probeInfo && probeInfo.duration_seconds) || null,
      grade: defaultGrade(),
      lut_id: null,
      order: 0,
      _probe: probeInfo || null,
    };
  }

  // Catmull-Rom spline through sorted control points, clamped to [0,1] --
  // a faithful JS port of grade.py's _eval_curve (keep both in step).
  function evalCurve(points, x) {
    if (!points || points.length < 2) return clamp(x, 0, 1);
    const pts = points.slice().sort((a, b) => a[0] - b[0]);
    const xs = pts.map((p) => p[0]);
    x = clamp(x, xs[0], xs[xs.length - 1]);
    let i = 0;
    while (i < xs.length - 1 && xs[i + 1] < x) i++;
    i = clamp(i, 0, pts.length - 2);
    const p0 = i - 1 >= 0 ? pts[i - 1] : pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = i + 2 < pts.length ? pts[i + 2] : pts[i + 1];
    const [x1, y1] = p1, [x2, y2] = p2;
    if (x2 === x1) return clamp(y1, 0, 1);
    const t = (x - x1) / (x2 - x1);
    const t2 = t * t, t3 = t2 * t;
    const y0 = p0[1], y3 = p3[1];
    const y = 0.5 * (
      (2 * y1) + (-y0 + y2) * t + (2 * y0 - 5 * y1 + 4 * y2 - y3) * t2 +
      (-y0 + 3 * y1 - 3 * y2 + y3) * t3
    );
    return clamp(y, 0, 1);
  }

  // 3-way wheel <-> RGB offset basis (zero-sum: r+g+b === 0 always), so a
  // wheel puck can only represent color BALANCE, never uniform brightness
  // -- that's what each wheel's own "Luminance" slider is for.
  const SQRT3_2 = 0.8660254037844386;
  function wheelToRgb(nx, ny) {
    const dist = Math.hypot(nx, ny);
    if (dist > 1) { nx /= dist; ny /= dist; }
    return [nx, -0.5 * nx + SQRT3_2 * ny, -0.5 * nx - SQRT3_2 * ny];
  }
  function rgbToWheel(rgb) {
    const mean = (rgb[0] + rgb[1] + rgb[2]) / 3;
    const r0 = rgb[0] - mean, g0 = rgb[1] - mean, b0 = rgb[2] - mean;
    const nx = r0;
    const ny = (g0 - b0) / (2 * SQRT3_2);
    return { nx: clamp(nx, -1, 1), ny: clamp(ny, -1, 1), luminance: mean };
  }

  // ==========================================================================
  // suite state
  // ==========================================================================

  const CZ = {
    visible: false,
    project: { id: null, name: "Untitled Project", clips: [] },
    activeClipIndex: -1,
    selected: new Set(),   // clip ids checked in the media bin, for batch ops
    luts: [],              // [{id, name, size, ...}]
    lutPreviewCache: new Map(), // lut_id -> {size, data}
    presets: [],
    activeTab: "basic",
    activeCurveChannel: "master",
    activeHslBand: "red",
    playing: false,
    scrubDragging: null,   // "playhead" | "in" | "out" | null
    // gl state, populated by initGL()
    gl: null, program: null, uniforms: {}, videoTex: null, curveTex: null, lutTex: null, hasLut: false,
    needsRender: true,
    rafHandle: null,
    lastScopeAt: 0,
    // Before/After compare: both on = split screen (left ungraded, right
    // graded); Before only = fully ungraded; After only (default) or
    // neither = fully graded, i.e. normal playback.
    showBefore: false,
    showAfter: true,
  };

  function activeClip() {
    return CZ.activeClipIndex >= 0 ? CZ.project.clips[CZ.activeClipIndex] : null;
  }

  // ==========================================================================
  // WebGL2 pipeline
  // ==========================================================================

  const VERTEX_SRC = `#version 300 es
in vec2 a_pos;
out vec2 v_uv;
void main() {
  v_uv = a_pos * 0.5 + 0.5;
  gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

  const FRAGMENT_SRC = `#version 300 es
precision highp float;
precision highp sampler3D;

in vec2 v_uv;
out vec4 outColor;

uniform sampler2D u_video;
uniform sampler2D u_curveLUT;
uniform sampler3D u_lut;
uniform bool u_useLut;
uniform float u_lutIntensity;

uniform float u_exposure;
uniform float u_contrast;
uniform float u_temperature;
uniform float u_tint;
uniform float u_saturation;
uniform float u_vibrance;
uniform vec3 u_lift;
uniform vec3 u_gamma;
uniform vec3 u_gain;
uniform float u_highlights;
uniform float u_shadows;
uniform float u_whites;
uniform float u_blacks;
uniform vec3 u_hsl[8]; // x=hue(-100..100), y=sat(-100..100), z=lum(-100..100)

uniform bool u_showBefore;
uniform bool u_showAfter;

const float PI = 3.14159265359;
const float HSL_BAND_WIDTH = 45.0;
const float HSL_CENTERS[8] = float[8](0.0, 30.0, 60.0, 120.0, 180.0, 240.0, 285.0, 315.0);

vec3 rgb2hsl(vec3 c) {
  float mx = max(c.r, max(c.g, c.b));
  float mn = min(c.r, min(c.g, c.b));
  float l = (mx + mn) * 0.5;
  float h = 0.0;
  float s = 0.0;
  if (mx != mn) {
    float d = mx - mn;
    s = l > 0.5 ? d / (2.0 - mx - mn) : d / (mx + mn);
    if (mx == c.r) h = mod((c.g - c.b) / d + (c.g < c.b ? 6.0 : 0.0), 6.0);
    else if (mx == c.g) h = (c.b - c.r) / d + 2.0;
    else h = (c.r - c.g) / d + 4.0;
    h *= 60.0;
  }
  return vec3(h, s, l);
}

float hueToRgbF(float p, float q, float t) {
  t = mod(t, 1.0);
  if (t < 1.0 / 6.0) return p + (q - p) * 6.0 * t;
  if (t < 0.5) return q;
  if (t < 2.0 / 3.0) return p + (q - p) * (2.0 / 3.0 - t) * 6.0;
  return p;
}

vec3 hsl2rgb(vec3 hsl) {
  float h = hsl.x, s = hsl.y, l = hsl.z;
  if (s <= 0.0) return vec3(l);
  float q = l < 0.5 ? l * (1.0 + s) : l + s - l * s;
  float p = 2.0 * l - q;
  float hn = mod(h, 360.0) / 360.0;
  return vec3(hueToRgbF(p, q, hn + 1.0 / 3.0), hueToRgbF(p, q, hn), hueToRgbF(p, q, hn - 1.0 / 3.0));
}

float hslBandWeight(float hueDeg, int i) {
  float center = HSL_CENTERS[i];
  float d = abs(mod((hueDeg - center) + 180.0, 360.0) - 180.0);
  if (d >= HSL_BAND_WIDTH) return 0.0;
  return 0.5 * (1.0 + cos(PI * d / HSL_BAND_WIDTH));
}

void main() {
  vec3 original = texture(u_video, v_uv).rgb;
  vec3 c = original;

  // 1. exposure
  c *= pow(2.0, u_exposure);

  // 2. white balance (linear channel-tilt approximation, matching grade.py)
  float t = u_temperature / 100.0;
  float tn = u_tint / 100.0;
  c.r *= (1.0 + 0.30 * t);
  c.b *= (1.0 - 0.30 * t);
  c.g *= (1.0 + 0.20 * tn);
  c.r *= (1.0 - 0.10 * tn);
  c.b *= (1.0 - 0.10 * tn);

  // 3. lift / gamma / gain
  c = (c + u_lift) * (1.0 + u_gain);
  c.r = c.r > 0.0 ? pow(c.r, 1.0 / max(0.05, 1.0 + u_gamma.r)) : c.r;
  c.g = c.g > 0.0 ? pow(c.g, 1.0 / max(0.05, 1.0 + u_gamma.g)) : c.g;
  c.b = c.b > 0.0 ? pow(c.b, 1.0 / max(0.05, 1.0 + u_gamma.b)) : c.b;

  // 4. contrast, pivoted at 18% grey
  float cc = 1.0 + u_contrast / 100.0;
  c = (c - 0.18) * cc + 0.18;

  c = clamp(c, 0.0, 1.0);

  // 5. Tone range (highlights/shadows/whites/blacks) -- see grade.py's
  // apply_grade_to_rgb stage 5 for the reference implementation this
  // mirrors exactly, including the smoothstep mask shapes.
  if (u_highlights != 0.0 || u_shadows != 0.0 || u_whites != 0.0 || u_blacks != 0.0) {
    float luma = 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b;
    float wHighlights = smoothstep(0.35, 1.0, luma);
    float wShadows = 1.0 - smoothstep(0.0, 0.65, luma);
    float wWhites = pow(smoothstep(0.6, 1.0, luma), 2.0);
    float wBlacks = pow(1.0 - smoothstep(0.0, 0.4, luma), 2.0);
    float delta = 0.5 * (
      (u_highlights / 100.0) * wHighlights +
      (u_shadows / 100.0) * wShadows +
      (u_whites / 100.0) * wWhites +
      (u_blacks / 100.0) * wBlacks
    );
    c += delta;
    c = clamp(c, 0.0, 1.0);
  }

  // 6. HSL secondary bands, then master saturation/vibrance
  vec3 hsl = rgb2hsl(c);
  float dh = 0.0, ds = 0.0, dl = 0.0;
  for (int i = 0; i < 8; i++) {
    float w = hslBandWeight(hsl.x, i);
    if (w > 0.0) {
      dh += w * (u_hsl[i].x / 100.0) * 30.0;
      ds += w * (u_hsl[i].y / 100.0);
      dl += w * (u_hsl[i].z / 100.0);
    }
  }
  hsl.x = mod(hsl.x + dh, 360.0);
  hsl.y = clamp(hsl.y + ds, 0.0, 1.0);
  hsl.z = clamp(hsl.z + dl, 0.0, 1.0);
  hsl.y = clamp(hsl.y * (1.0 + u_saturation / 100.0), 0.0, 1.0);
  hsl.y = clamp(hsl.y + (u_vibrance / 100.0) * (1.0 - hsl.y), 0.0, 1.0);
  c = hsl2rgb(hsl);

  c = clamp(c, 0.0, 1.0);

  // 7. curves -- one shared 256x1 LUT texture, each channel sampled with
  // its own pre-curve value as the lookup coordinate (see buildCurveLUT).
  c = vec3(
    texture(u_curveLUT, vec2(c.r, 0.5)).r,
    texture(u_curveLUT, vec2(c.g, 0.5)).g,
    texture(u_curveLUT, vec2(c.b, 0.5)).b
  );

  // 8. creative 3D LUT
  if (u_useLut) {
    vec3 lutC = texture(u_lut, c).rgb;
    c = mix(c, lutC, u_lutIntensity);
  }

  // 9. before/after compare -- both checked splits the frame (left
  // ungraded, right graded, thin divider line); Before only shows fully
  // ungraded; After only (default) or neither shows the normal graded
  // result untouched.
  vec3 result = c;
  if (u_showBefore && u_showAfter) {
    float divider = 0.0015;
    if (abs(v_uv.x - 0.5) < divider) result = vec3(1.0);
    else if (v_uv.x < 0.5) result = original;
    else result = c;
  } else if (u_showBefore) {
    result = original;
  }

  outColor = vec4(clamp(result, 0.0, 1.0), 1.0);
}`;

  function compileShader(gl, type, src) {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(sh);
      gl.deleteShader(sh);
      throw new Error("Shader compile error: " + log);
    }
    return sh;
  }

  function initGL(canvas) {
    const gl = canvas.getContext("webgl2", { antialias: false, preserveDrawingBuffer: true });
    if (!gl) return null;

    // Every RGB upload below (curve LUT, creative 3D LUT) is a tightly-packed
    // buffer with no row padding. The default UNPACK_ALIGNMENT of 4 makes the
    // GL assume each row is padded to a 4-byte boundary -- true for the 256x1
    // curve LUT (256*3=768) but false for most real creative LUT sizes
    // (17/33/65 -> size*3 isn't a multiple of 4), which made texImage3D throw
    // INVALID_OPERATION and left the LUT texture incomplete. Sampling an
    // incomplete texture returns solid black, which at the default 100%
    // lut_intensity blends the whole preview to black. Alignment 1 matches
    // our buffers exactly regardless of size.
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);

    const vs = compileShader(gl, gl.VERTEX_SHADER, VERTEX_SRC);
    const fs = compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SRC);
    const program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error("Program link error: " + gl.getProgramInfoLog(program));
    }

    const quad = new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, quad, gl.STATIC_DRAW);
    const posLoc = gl.getAttribLocation(program, "a_pos");
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

    const uniforms = {};
    ["u_video", "u_curveLUT", "u_lut", "u_useLut", "u_lutIntensity", "u_exposure", "u_contrast",
      "u_temperature", "u_tint", "u_saturation", "u_vibrance", "u_lift", "u_gamma", "u_gain",
      "u_highlights", "u_shadows", "u_whites", "u_blacks", "u_hsl", "u_showBefore", "u_showAfter"]
      .forEach((n) => { uniforms[n] = gl.getUniformLocation(program, n); });

    const videoTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, videoTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    const curveTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, curveTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    const lutTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_3D, lutTex);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);

    CZ.gl = gl; CZ.program = program; CZ.uniforms = uniforms; CZ.vao = vao;
    CZ.videoTex = videoTex; CZ.curveTex = curveTex; CZ.lutTex = lutTex;
    buildCurveLUT();
    return gl;
  }

  function buildCurveLUT() {
    const gl = CZ.gl;
    if (!gl) return;
    const grade = (activeClip() || { grade: defaultGrade() }).grade;
    const data = new Uint8Array(256 * 3);
    for (let i = 0; i < 256; i++) {
      const x = i / 255;
      const r = evalCurve(grade.curve_master, evalCurve(grade.curve_r, x));
      const g = evalCurve(grade.curve_master, evalCurve(grade.curve_g, x));
      const b = evalCurve(grade.curve_master, evalCurve(grade.curve_b, x));
      data[i * 3] = Math.round(r * 255);
      data[i * 3 + 1] = Math.round(g * 255);
      data[i * 3 + 2] = Math.round(b * 255);
    }
    gl.bindTexture(gl.TEXTURE_2D, CZ.curveTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB8, 256, 1, 0, gl.RGB, gl.UNSIGNED_BYTE, data);
  }

  async function applyLutTexture(lutId) {
    const gl = CZ.gl;
    if (!gl) return;
    if (!lutId) { CZ.hasLut = false; requestRender(); return; }
    let payload = CZ.lutPreviewCache.get(lutId);
    if (!payload) {
      const res = await call("colorize_get_lut_preview", lutId);
      if (!res.ok) { toastIfError(res, "Couldn't load that LUT."); CZ.hasLut = false; requestRender(); return; }
      payload = res.lut;
      CZ.lutPreviewCache.set(lutId, payload);
    }
    const size = payload.size;
    const floatData = payload.data; // flat [r,g,b, r,g,b, ...] in [0,1], R-fastest
    const data = new Uint8Array(size * size * size * 3);
    for (let i = 0; i < data.length; i++) data[i] = Math.round(clamp(floatData[i], 0, 1) * 255);
    gl.bindTexture(gl.TEXTURE_3D, CZ.lutTex);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGB8, size, size, size, 0, gl.RGB, gl.UNSIGNED_BYTE, data);
    CZ.hasLut = true;
    requestRender();
  }

  function uploadVideoFrame() {
    const gl = CZ.gl;
    const video = $("czVideo");
    if (!gl || !video || video.readyState < 2) return false;
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.bindTexture(gl.TEXTURE_2D, CZ.videoTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    return true;
  }

  function drawFrame() {
    const gl = CZ.gl;
    const canvas = $("czCanvas");
    if (!gl || !canvas) return;
    if (!uploadVideoFrame()) return;

    const clip = activeClip();
    const grade = (clip || { grade: defaultGrade() }).grade;

    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.useProgram(CZ.program);
    gl.bindVertexArray(CZ.vao);

    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, CZ.videoTex); gl.uniform1i(CZ.uniforms.u_video, 0);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, CZ.curveTex); gl.uniform1i(CZ.uniforms.u_curveLUT, 1);
    gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_3D, CZ.lutTex); gl.uniform1i(CZ.uniforms.u_lut, 2);

    gl.uniform1i(CZ.uniforms.u_useLut, CZ.hasLut ? 1 : 0);
    gl.uniform1f(CZ.uniforms.u_lutIntensity, clamp((grade.lut_intensity || 0) / 100, 0, 1));
    gl.uniform1f(CZ.uniforms.u_exposure, grade.exposure || 0);
    gl.uniform1f(CZ.uniforms.u_contrast, grade.contrast || 0);
    gl.uniform1f(CZ.uniforms.u_temperature, grade.temperature || 0);
    gl.uniform1f(CZ.uniforms.u_tint, grade.tint || 0);
    gl.uniform1f(CZ.uniforms.u_saturation, grade.saturation || 0);
    gl.uniform1f(CZ.uniforms.u_vibrance, grade.vibrance || 0);
    gl.uniform1f(CZ.uniforms.u_highlights, grade.highlights || 0);
    gl.uniform1f(CZ.uniforms.u_shadows, grade.shadows || 0);
    gl.uniform1f(CZ.uniforms.u_whites, grade.whites || 0);
    gl.uniform1f(CZ.uniforms.u_blacks, grade.blacks || 0);
    gl.uniform3fv(CZ.uniforms.u_lift, grade.lift || [0, 0, 0]);
    gl.uniform3fv(CZ.uniforms.u_gamma, grade.gamma || [0, 0, 0]);
    gl.uniform3fv(CZ.uniforms.u_gain, grade.gain || [0, 0, 0]);
    const hslFlat = new Float32Array(24);
    HSL_BANDS.forEach((b, i) => {
      const e = (grade.hsl && grade.hsl[b.key]) || { hue: 0, sat: 0, lum: 0 };
      hslFlat[i * 3] = e.hue || 0; hslFlat[i * 3 + 1] = e.sat || 0; hslFlat[i * 3 + 2] = e.lum || 0;
    });
    gl.uniform3fv(CZ.uniforms.u_hsl, hslFlat);
    gl.uniform1i(CZ.uniforms.u_showBefore, CZ.showBefore ? 1 : 0);
    gl.uniform1i(CZ.uniforms.u_showAfter, CZ.showAfter ? 1 : 0);

    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  function requestRender() { CZ.needsRender = true; }

  let _renderErrorShown = false;
  function renderLoop() {
    if (!CZ.visible) { CZ.rafHandle = null; return; }
    // A throw anywhere in here (e.g. a cross-origin-tainted video failing
    // texImage2D) must never break the requestAnimationFrame chain below --
    // that would silently freeze the preview on whatever frame was last
    // drawn (or a blank canvas) with no visible sign anything went wrong.
    try {
      if (CZ.playing || CZ.needsRender) {
        drawFrame();
        CZ.needsRender = false;
        maybeUpdateScopes();
      }
    } catch (err) {
      if (!_renderErrorShown) {
        _renderErrorShown = true;
        toast("Live preview couldn't render this clip's video: " + (err && err.message ? err.message : err), "error", 8000);
      }
    }
    CZ.rafHandle = requestAnimationFrame(renderLoop);
  }

  function startRenderLoop() {
    if (CZ.rafHandle == null) CZ.rafHandle = requestAnimationFrame(renderLoop);
  }

  // ==========================================================================
  // scopes -- histogram / waveform / vectorscope from real graded pixels,
  // read back from the canvas on a throttle (not every frame) to keep
  // playback smooth.
  // ==========================================================================

  function maybeUpdateScopes() {
    const now = performance.now();
    if (now - CZ.lastScopeAt < 180) return;
    CZ.lastScopeAt = now;
    const gl = CZ.gl, canvas = $("czCanvas");
    if (!gl || !canvas || !canvas.width) return;
    const w = canvas.width, h = canvas.height;
    const pixels = new Uint8Array(w * h * 4);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    drawHistogram(pixels, w, h);
    drawWaveform(pixels, w, h);
    drawVectorscope(pixels, w, h);
  }

  function drawHistogram(pixels, w, h) {
    const canvas = $("czHistogram");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const bins = { r: new Uint32Array(256), g: new Uint32Array(256), b: new Uint32Array(256) };
    const stride = 4 * Math.max(1, Math.floor((w * h) / 20000)); // sample cap for perf
    for (let i = 0; i < pixels.length; i += stride) {
      bins.r[pixels[i]]++; bins.g[pixels[i + 1]]++; bins.b[pixels[i + 2]]++;
    }
    const maxCount = Math.max(1, ...bins.r, ...bins.g, ...bins.b);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.globalCompositeOperation = "lighter";
    [["r", "rgba(224,67,60,.85)"], ["g", "rgba(99,224,60,.85)"], ["b", "rgba(60,143,224,.85)"]].forEach(([ch, color]) => {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(0, canvas.height);
      for (let i = 0; i < 256; i++) {
        const x = (i / 255) * canvas.width;
        const y = canvas.height - (bins[ch][i] / maxCount) * canvas.height;
        ctx.lineTo(x, y);
      }
      ctx.lineTo(canvas.width, canvas.height);
      ctx.closePath();
      ctx.fill();
    });
    ctx.globalCompositeOperation = "source-over";
  }

  function drawWaveform(pixels, w, h) {
    const canvas = $("czWaveform");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "rgba(16,17,22,1)";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "rgba(232,163,61,.55)";
    const strideX = Math.max(1, Math.floor(w / canvas.width));
    const strideY = Math.max(1, Math.floor(h / 48));
    for (let y = 0; y < h; y += strideY) {
      for (let x = 0; x < w; x += strideX) {
        const idx = (y * w + x) * 4;
        const luma = 0.2126 * pixels[idx] + 0.7152 * pixels[idx + 1] + 0.0722 * pixels[idx + 2];
        const px = (x / w) * canvas.width;
        const py = canvas.height - (luma / 255) * canvas.height;
        ctx.fillRect(px, py, 1, 1);
      }
    }
  }

  function drawVectorscope(pixels, w, h) {
    const canvas = $("czVectorscope");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "rgba(16,17,22,1)";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    const cx = canvas.width / 2, cy = canvas.height / 2, scale = canvas.width / 2.4;
    ctx.strokeStyle = "rgba(255,255,255,.12)";
    ctx.beginPath(); ctx.arc(cx, cy, scale, 0, Math.PI * 2); ctx.stroke();
    ctx.fillStyle = "rgba(79,176,174,.55)";
    const stride = 4 * Math.max(1, Math.floor((w * h) / 12000));
    for (let i = 0; i < pixels.length; i += stride) {
      const r = pixels[i] / 255, g = pixels[i + 1] / 255, b = pixels[i + 2] / 255;
      const u = -0.169 * r - 0.331 * g + 0.5 * b;
      const v = 0.5 * r - 0.419 * g - 0.081 * b;
      ctx.fillRect(cx + u * scale * 2, cy - v * scale * 2, 1, 1);
    }
  }

  // ==========================================================================
  // transport (playback, scrub bar, in/out points)
  // ==========================================================================

  function resizeCanvasToVideo() {
    const video = $("czVideo"), canvas = $("czCanvas"), wrap = $("czPreviewWrap");
    if (!video.videoWidth || !wrap) return;
    // Backing-store (actual pixel) resolution is capped for render/scope
    // -readback cost, not display quality -- the DISPLAY size is handled
    // entirely by CSS (#czCanvas's width:auto;height:auto;max-width:100%;
    // max-height:100%, centered by .cz-preview-wrap's flex centering),
    // which scales it down to fit whatever space is available while
    // preserving aspect ratio, no manual pixel math needed here.
    const maxDim = 1280;
    const scale = Math.min(1, maxDim / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    requestRender();
  }

  function updateScrubUI() {
    const clip = activeClip();
    const video = $("czVideo");
    const duration = (video && video.duration) || (clip && clip._probe && clip._probe.duration_seconds) || 0;
    if (!duration) return;
    const inSec = (clip && clip.in_seconds) || 0;
    const outSec = clip && clip.out_seconds != null ? clip.out_seconds : duration;
    const range = $("czScrubRange"), inH = $("czInHandle"), outH = $("czOutHandle"), head = $("czScrubPlayhead");
    const pct = (s) => clamp((s / duration) * 100, 0, 100);
    if (range) { range.style.left = pct(inSec) + "%"; range.style.width = Math.max(0, pct(outSec) - pct(inSec)) + "%"; }
    if (inH) inH.style.left = pct(inSec) + "%";
    if (outH) outH.style.left = pct(outSec) + "%";
    if (head && video) head.style.left = pct(video.currentTime) + "%";
    const tc = $("czTimecode");
    if (tc && video) tc.textContent = formatTimecode(video.currentTime);
  }

  function setupTransport() {
    const video = $("czVideo");
    video.addEventListener("loadedmetadata", () => {
      resizeCanvasToVideo();
      const clip = activeClip();
      if (clip) {
        if (clip.out_seconds == null) clip.out_seconds = video.duration;
        video.currentTime = clip.in_seconds || 0;
      }
      updateScrubUI();
    });
    video.addEventListener("timeupdate", () => {
      const clip = activeClip();
      if (clip && clip.out_seconds != null && video.currentTime >= clip.out_seconds) {
        video.pause(); CZ.playing = false; $("czPlayBtn").textContent = "▶";
        video.currentTime = clip.out_seconds;
      }
      updateScrubUI();
      requestRender();
    });
    video.addEventListener("seeked", requestRender);
    video.addEventListener("play", () => { CZ.playing = true; $("czPlayBtn").textContent = "❚❚"; });
    video.addEventListener("pause", () => { CZ.playing = false; $("czPlayBtn").textContent = "▶"; });

    $("czPlayBtn").addEventListener("click", () => {
      const clip = activeClip();
      if (!clip) return;
      if (video.paused) {
        if (clip.out_seconds != null && video.currentTime >= clip.out_seconds) video.currentTime = clip.in_seconds || 0;
        video.play();
      } else {
        video.pause();
      }
    });

    const scrub = $("czScrub");
    function xToSeconds(clientX) {
      const rect = scrub.getBoundingClientRect();
      const frac = clamp((clientX - rect.left) / rect.width, 0, 1);
      return frac * (video.duration || 0);
    }
    scrub.addEventListener("pointerdown", (e) => {
      const target = e.target.closest(".cz-scrub__handle");
      CZ.scrubDragging = target ? (target.id === "czInHandle" ? "in" : "out") : "playhead";
      scrub.setPointerCapture(e.pointerId);
      handleScrubMove(e);
    });
    scrub.addEventListener("pointermove", (e) => { if (CZ.scrubDragging) handleScrubMove(e); });
    scrub.addEventListener("pointerup", () => { CZ.scrubDragging = null; });
    function handleScrubMove(e) {
      const clip = activeClip();
      if (!clip) return;
      const seconds = xToSeconds(e.clientX);
      if (CZ.scrubDragging === "in") {
        clip.in_seconds = clamp(seconds, 0, (clip.out_seconds != null ? clip.out_seconds : video.duration) - 0.02);
      } else if (CZ.scrubDragging === "out") {
        clip.out_seconds = clamp(seconds, clip.in_seconds + 0.02, video.duration || seconds);
      } else {
        video.currentTime = seconds;
      }
      updateScrubUI();
      renderMediaBin();
    }

    $("czSetInBtn").addEventListener("click", () => {
      const clip = activeClip(); if (!clip) return;
      clip.in_seconds = clamp(video.currentTime, 0, (clip.out_seconds || video.duration) - 0.02);
      updateScrubUI(); renderMediaBin();
    });
    $("czSetOutBtn").addEventListener("click", () => {
      const clip = activeClip(); if (!clip) return;
      clip.out_seconds = clamp(video.currentTime, clip.in_seconds + 0.02, video.duration);
      updateScrubUI(); renderMediaBin();
    });

    window.addEventListener("keydown", (e) => {
      if (!CZ.visible) return;
      const tag = (document.activeElement && document.activeElement.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.key === "[") $("czSetInBtn").click();
      else if (e.key === "]") $("czSetOutBtn").click();
      else if (e.key === " ") { e.preventDefault(); $("czPlayBtn").click(); }
    });

    window.addEventListener("resize", () => { resizeCanvasToVideo(); resizeCurveCanvas(); });

    $("czShowBeforeChk").addEventListener("change", (e) => {
      CZ.showBefore = e.target.checked;
      requestRender();
    });
    $("czShowAfterChk").addEventListener("change", (e) => {
      CZ.showAfter = e.target.checked;
      requestRender();
    });
  }

  // ==========================================================================
  // media bin
  // ==========================================================================

  async function loadClipIntoPreview(index) {
    CZ.activeClipIndex = index;
    const clip = activeClip();
    const video = $("czVideo");
    if (!clip) { video.removeAttribute("src"); return; }
    const res = await call("colorize_get_preview_url", clip.source_path);
    if (!res.ok) { toastIfError(res, "Couldn't load that clip for preview."); return; }
    video.pause();
    video.src = res.url;
    buildCurveLUT();
    applyLutTexture(clip.lut_id);
    renderBasicPanel();
    renderWheelsPanel();
    renderCurvesPanel();
    renderHslPanel();
    renderLutPanel();
    renderMediaBin();
    updateHistoryButtons();
    requestRender();
  }

  function renderMediaBin() {
    const list = $("czMediaList");
    if (!list) return;
    if (!CZ.project.clips.length) {
      list.innerHTML = '<li class="suite-empty">No clips imported yet.</li>';
      return;
    }
    list.innerHTML = CZ.project.clips.map((clip, i) => {
      const dur = clip._probe ? clip._probe.duration_seconds : null;
      const trimmed = clip.out_seconds != null ? (clip.out_seconds - clip.in_seconds) : dur;
      const meta = trimmed != null ? formatTimecode(trimmed).slice(0, 8) : "";
      const lutBadge = clip.lut_id ? '<span class="cz-media-item__lut-badge">LUT</span>' : "";
      return `<li class="cz-media-item${i === CZ.activeClipIndex ? " is-active" : ""}${CZ.selected.has(clip.id) ? " is-selected" : ""}" data-index="${i}">
        <input type="checkbox" class="cz-media-item__check" data-id="${esc(clip.id)}" ${CZ.selected.has(clip.id) ? "checked" : ""}>
        <span class="cz-media-item__name" title="${esc(clip.source_path)}">${esc(basename(clip.source_path))}</span>
        ${lutBadge}
        <span class="cz-media-item__meta">${esc(meta)}</span>
        <button class="cz-media-item__remove" data-id="${esc(clip.id)}" title="Remove clip">✕</button>
      </li>`;
    }).join("");
    list.querySelectorAll(".cz-media-item").forEach((li) => {
      li.addEventListener("click", (e) => {
        if (e.target.classList.contains("cz-media-item__check") || e.target.classList.contains("cz-media-item__remove")) return;
        loadClipIntoPreview(parseInt(li.dataset.index, 10));
      });
    });
    list.querySelectorAll(".cz-media-item__check").forEach((cb) => {
      cb.addEventListener("click", (e) => {
        e.stopPropagation();
        if (cb.checked) CZ.selected.add(cb.dataset.id); else CZ.selected.delete(cb.dataset.id);
        renderMediaBin();
      });
    });
    list.querySelectorAll(".cz-media-item__remove").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        removeClip(btn.dataset.id);
      });
    });
  }

  // Clears WebGL/panel state back to "nothing loaded" -- shared by
  // removeClip (when the active clip was the one removed) and
  // clearAllClips, both of which can leave the bin empty.
  function resetPreviewToEmpty() {
    CZ.activeClipIndex = -1;
    const video = $("czVideo");
    if (video) video.removeAttribute("src");
    buildCurveLUT();
    applyLutTexture(null);
    renderBasicPanel(); renderWheelsPanel(); renderCurvesPanel(); renderHslPanel(); renderLutPanel();
    updateHistoryButtons();
    requestRender();
  }

  function removeClip(id) {
    const idx = CZ.project.clips.findIndex((c) => c.id === id);
    if (idx < 0) return;
    CZ.project.clips.splice(idx, 1);
    CZ.selected.delete(id);
    GRADE_HISTORY.delete(id);
    CZ.project.clips.forEach((c, i) => { c.order = i; });
    if (CZ.activeClipIndex === idx) {
      const nextIndex = CZ.project.clips.length ? Math.min(idx, CZ.project.clips.length - 1) : -1;
      if (nextIndex >= 0) loadClipIntoPreview(nextIndex); else resetPreviewToEmpty();
    } else if (CZ.activeClipIndex > idx) {
      CZ.activeClipIndex -= 1;
    }
    renderMediaBin();
  }

  function clearAllClips() {
    if (!CZ.project.clips.length) return;
    if (!confirm(`Remove all ${CZ.project.clips.length} clip(s) from this project?`)) return;
    CZ.project.clips.forEach((c) => GRADE_HISTORY.delete(c.id));
    CZ.project.clips = [];
    CZ.selected.clear();
    resetPreviewToEmpty();
    renderMediaBin();
  }

  async function pickClips() {
    const res = await call("colorize_pick_clips");
    if (!res.ok) { toastIfError(res, "Couldn't import clips."); return; }
    addProbedClips(res.clips);
  }

  // Shared by pickClips (dialog-sourced paths) and addClipsByPath
  // (paths supplied by another workspace's hand-off, e.g. Spyglass's
  // "Send to Colorize") -- both just need each probe result pushed into
  // the bin the same way.
  function addProbedClips(probedClips) {
    (probedClips || []).forEach((probe) => {
      if (probe.error) { toast(`${basename(probe.path)}: ${probe.error}`, "error"); return; }
      const clip = newClip(probe.path, probe);
      clip.order = CZ.project.clips.length;
      CZ.project.clips.push(clip);
    });
    renderMediaBin();
    if (CZ.activeClipIndex < 0 && CZ.project.clips.length) loadClipIntoPreview(0);
  }

  // Cross-workspace hand-off (Search workspace's "Send to Colorize"): the
  // caller already knows the file paths, so this skips the file dialog
  // colorize_pick_clips would otherwise open.
  async function addClipsByPath(paths) {
    if (!paths || !paths.length) return;
    const res = await call("colorize_probe_clips", paths);
    if (!res.ok) { toastIfError(res, "Couldn't import clips."); return; }
    addProbedClips(res.clips);
    toast(`Sent ${paths.length} clip${paths.length === 1 ? "" : "s"} to Colorize.`, "ok");
  }

  // ==========================================================================
  // control tabs
  // ==========================================================================

  function setActiveTab(tab) {
    CZ.activeTab = tab;
    document.querySelectorAll(".cz-tab").forEach((b) => b.classList.toggle("is-active", b.dataset.tab === tab));
    ["basic", "color", "lut"].forEach((t) => {
      const panel = $("czPanel" + t[0].toUpperCase() + t.slice(1));
      if (panel) panel.hidden = t !== tab;
    });
    // The curve canvas can't be measured (clientWidth/Height are 0) while
    // its tab is hidden -- re-measure and redraw at the correct backing
    // -store resolution now that it's visible again.
    if (tab === "basic") resizeCurveCanvas();
  }

  // ---- basic ----

  const BASIC_FIELDS = [
    { key: "exposure", label: "Exposure", min: -4, max: 4, step: 0.01 },
    { key: "contrast", label: "Contrast", min: -100, max: 100, step: 1 },
    { key: "highlights", label: "Highlights", min: -100, max: 100, step: 1 },
    { key: "shadows", label: "Shadows", min: -100, max: 100, step: 1 },
    { key: "whites", label: "Whites", min: -100, max: 100, step: 1 },
    { key: "blacks", label: "Blacks", min: -100, max: 100, step: 1 },
    { key: "temperature", label: "Temperature", min: -100, max: 100, step: 1 },
    { key: "tint", label: "Tint", min: -100, max: 100, step: 1 },
    { key: "saturation", label: "Saturation", min: -100, max: 100, step: 1 },
    { key: "vibrance", label: "Vibrance", min: -100, max: 100, step: 1 },
  ];

  function renderBasicPanel() {
    const panel = $("czBasicSliders");
    if (!panel) return;
    const grade = (activeClip() || { grade: defaultGrade() }).grade;
    panel.innerHTML = BASIC_FIELDS.map((f) => sliderRowHtml(f.key, f.label, grade[f.key] || 0, f.min, f.max, f.step)).join("");
    BASIC_FIELDS.forEach((f) => wireSliderRow(panel, f.key, f.min, f.max, (v) => {
      const g = (activeClip() || {}).grade; if (!g) return;
      g[f.key] = v; requestRender();
    }, 0));
  }

  function sliderRowHtml(key, label, value, min, max, step) {
    return `<div class="cz-slider-row" data-key="${key}">
      <div class="cz-slider-row__head">
        <span>${esc(label)}</span>
        <span><span class="cz-slider-row__value" data-role="value">${Number(value).toFixed(step < 1 ? 2 : 0)}</span>
        <button class="cz-slider-row__reset" data-role="reset" title="Reset">↺</button></span>
      </div>
      <input type="range" min="${min}" max="${max}" step="${step}" value="${value}" data-role="input">
    </div>`;
  }

  function wireSliderRow(container, key, min, max, onChange, defaultValue) {
    const row = container.querySelector(`.cz-slider-row[data-key="${key}"]`);
    if (!row) return;
    const input = row.querySelector('[data-role="input"]');
    const valueEl = row.querySelector('[data-role="value"]');
    const resetBtn = row.querySelector('[data-role="reset"]');
    const stroke = makeStrokeGuard();
    input.addEventListener("input", () => {
      stroke.begin();
      const v = parseFloat(input.value);
      valueEl.textContent = v.toFixed(parseFloat(input.step) < 1 ? 2 : 0);
      onChange(v);
    });
    input.addEventListener("change", () => stroke.end());
    resetBtn.addEventListener("click", () => {
      pushUndoSnapshot();
      input.value = defaultValue;
      valueEl.textContent = Number(defaultValue).toFixed(parseFloat(input.step) < 1 ? 2 : 0);
      onChange(defaultValue);
    });
  }

  // ---- wheels ----

  const WHEEL_DEFS = [{ key: "lift", label: "Lift (Shadows)" }, { key: "gamma", label: "Gamma (Mids)" }, { key: "gain", label: "Gain (Highlights)" }];

  function renderWheelsPanel() {
    const panel = $("czPanelWheels");
    if (!panel) return;
    panel.innerHTML = `<div class="cz-wheels">${WHEEL_DEFS.map((w) => `
      <div class="cz-wheel-block" data-wheel="${w.key}">
        <div class="cz-wheel-block__head">
          <span class="cz-wheel-block__label">${esc(w.label)}</span>
          <button class="cz-wheel-block__reset" data-role="reset" title="Reset">↺</button>
        </div>
        <div class="cz-wheel" data-wheel-canvas="${w.key}"><div class="cz-wheel__puck"></div></div>
        <input type="range" min="-0.5" max="0.5" step="0.005" value="0" data-role="lum" title="Luminance">
      </div>`).join("")}</div>`;

    WHEEL_DEFS.forEach((w) => {
      const block = panel.querySelector(`.cz-wheel-block[data-wheel="${w.key}"]`);
      const wheel = block.querySelector(".cz-wheel");
      const puck = block.querySelector(".cz-wheel__puck");
      const lumInput = block.querySelector('[data-role="lum"]');
      const resetBtn = block.querySelector('[data-role="reset"]');
      const stroke = makeStrokeGuard();

      function currentGrade() { const c = activeClip(); return c ? c.grade[w.key] : [0, 0, 0]; }
      function syncPuckFromGrade() {
        const { nx, ny, luminance } = rgbToWheel(currentGrade());
        const r = wheel.clientWidth / 2;
        puck.style.left = `${r + nx * r}px`;
        puck.style.top = `${r - ny * r}px`;
        lumInput.value = luminance;
      }
      syncPuckFromGrade();

      function applyFromPuck(nx, ny) {
        const clip = activeClip(); if (!clip) return;
        const rgb = wheelToRgb(nx, ny);
        const lum = parseFloat(lumInput.value) || 0;
        clip.grade[w.key] = [rgb[0] + lum, rgb[1] + lum, rgb[2] + lum];
        requestRender();
      }

      let dragging = false;
      wheel.addEventListener("pointerdown", (e) => { stroke.begin(); dragging = true; wheel.setPointerCapture(e.pointerId); movePuck(e); });
      wheel.addEventListener("pointermove", (e) => { if (dragging) movePuck(e); });
      wheel.addEventListener("pointerup", () => { dragging = false; stroke.end(); });
      function movePuck(e) {
        const rect = wheel.getBoundingClientRect();
        const r = rect.width / 2;
        const nx = clamp((e.clientX - rect.left - r) / r, -1, 1);
        const ny = clamp(-(e.clientY - rect.top - r) / r, -1, 1);
        const dist = Math.min(1, Math.hypot(nx, ny));
        const angle = Math.atan2(ny, nx);
        const cx = r + Math.cos(angle) * dist * r, cy = r - Math.sin(angle) * dist * r;
        puck.style.left = `${cx}px`; puck.style.top = `${cy}px`;
        applyFromPuck(Math.cos(angle) * dist, Math.sin(angle) * dist);
      }
      lumInput.addEventListener("input", () => {
        stroke.begin();
        const { nx, ny } = rgbToWheel(currentGrade());
        applyFromPuck(nx, ny);
      });
      lumInput.addEventListener("change", () => stroke.end());
      resetBtn.addEventListener("click", () => {
        const clip = activeClip(); if (!clip) return;
        pushUndoSnapshot();
        clip.grade[w.key] = [0, 0, 0];
        syncPuckFromGrade();
        requestRender();
      });
    });
  }

  // ---- curves ----

  function renderCurvesPanel() {
    const panel = $("czPanelCurves");
    if (!panel) return;
    panel.innerHTML = `
      <div class="cz-curve-channels">
        <button data-ch="master" class="is-active">Master</button>
        <button data-ch="r">R</button>
        <button data-ch="g">G</button>
        <button data-ch="b">B</button>
        <button class="cz-curve-channels__reset" data-role="reset-curve" title="Reset this channel's curve">↺</button>
      </div>
      <canvas id="czCurveCanvas" width="320" height="170"></canvas>
      <p class="suite-hint suite-hint--tight">Click to add a point, drag to move, double-click a point to remove it.</p>`;
    panel.querySelectorAll(".cz-curve-channels button[data-ch]").forEach((b) => {
      b.addEventListener("click", () => {
        CZ.activeCurveChannel = b.dataset.ch;
        panel.querySelectorAll(".cz-curve-channels button[data-ch]").forEach((x) => x.classList.toggle("is-active", x === b));
        drawCurveEditor();
      });
    });
    panel.querySelector('[data-role="reset-curve"]').addEventListener("click", () => {
      const clip = activeClip(); if (!clip) return;
      pushUndoSnapshot();
      clip.grade["curve_" + CZ.activeCurveChannel] = IDENTITY_CURVE.map((p) => p.slice());
      buildCurveLUT(); requestRender(); drawCurveEditor();
    });
    wireCurveEditor();
    resizeCurveCanvas();
  }

  // The canvas's `width`/`height` attributes (its backing-store pixel
  // buffer) are hardcoded to 320x170 in the markup above, but CSS
  // stretches it to fill whatever width .cz-basic-curves is actually
  // given (often well over 320 CSS px) -- that upscale of a low-res
  // bitmap is what reads as a blurry/low-res curve chart. Re-measure the
  // canvas's on-screen CSS size and size the backing store to match, at
  // devicePixelRatio, so it's always drawn crisp at its real display
  // size. clientWidth/clientHeight are 0 while the Basic & Curves tab is
  // hidden (display:none), so this is a no-op until setActiveTab makes
  // it visible again and calls this itself.
  function resizeCurveCanvas() {
    const canvas = $("czCurveCanvas");
    if (!canvas || !canvas.clientWidth || !canvas.clientHeight) return;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.round(canvas.clientWidth * dpr);
    const h = Math.round(canvas.clientHeight * dpr);
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;
    drawCurveEditor();
  }

  function curvePointsForActiveChannel() {
    const clip = activeClip(); if (!clip) return IDENTITY_CURVE;
    return clip.grade["curve_" + CZ.activeCurveChannel];
  }

  function drawCurveEditor() {
    const canvas = $("czCurveCanvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    // Draw in CSS-pixel space (canvas.clientWidth/Height), not raw backing
    // -store pixels -- resizeCurveCanvas sizes the backing store to
    // clientWidth/Height * devicePixelRatio for crispness, but drawing
    // directly in that larger pixel space would also shrink lineWidth/
    // point-radius relative to the visible canvas at high DPR. Resetting
    // the transform to the current DPR scale (rather than accumulating
    // ctx.scale calls across repeated draws) keeps this idempotent no
    // matter how many times drawCurveEditor runs.
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = canvas.clientWidth || canvas.width / dpr;
    const H = canvas.clientHeight || canvas.height / dpr;
    ctx.clearRect(0, 0, W, H);
    ctx.strokeStyle = "rgba(255,255,255,.08)";
    for (let i = 1; i < 4; i++) {
      ctx.beginPath(); ctx.moveTo((i / 4) * W, 0); ctx.lineTo((i / 4) * W, H); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, (i / 4) * H); ctx.lineTo(W, (i / 4) * H); ctx.stroke();
    }
    const points = curvePointsForActiveChannel();
    const colors = { master: "#EDEDF2", r: "#e0433c", g: "#63e03c", b: "#3c8fe0" };
    ctx.strokeStyle = colors[CZ.activeCurveChannel] || "#EDEDF2";
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i <= 100; i++) {
      const x = i / 100;
      const y = evalCurve(points, x);
      const px = x * W, py = H - y * H;
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();
    ctx.fillStyle = colors[CZ.activeCurveChannel] || "#EDEDF2";
    points.forEach(([x, y]) => {
      ctx.beginPath(); ctx.arc(x * W, H - y * H, 4, 0, Math.PI * 2); ctx.fill();
    });
  }

  function wireCurveEditor() {
    const canvas = $("czCurveCanvas");
    if (!canvas || canvas._czWired) return;
    canvas._czWired = true;
    let dragIndex = -1;
    const stroke = makeStrokeGuard();

    function eventToUnit(e) {
      const rect = canvas.getBoundingClientRect();
      return {
        x: clamp((e.clientX - rect.left) / rect.width, 0, 1),
        y: clamp(1 - (e.clientY - rect.top) / rect.height, 0, 1),
      };
    }
    function nearestPointIndex(unitX, unitY) {
      const points = curvePointsForActiveChannel();
      let best = -1, bestDist = 0.05;
      points.forEach((p, i) => {
        const d = Math.hypot(p[0] - unitX, p[1] - unitY);
        if (d < bestDist) { bestDist = d; best = i; }
      });
      return best;
    }
    canvas.addEventListener("pointerdown", (e) => {
      const clip = activeClip(); if (!clip) return;
      stroke.begin();
      const { x, y } = eventToUnit(e);
      const points = curvePointsForActiveChannel();
      const idx = nearestPointIndex(x, y);
      if (idx >= 0) {
        dragIndex = idx;
      } else {
        points.push([x, y]);
        points.sort((a, b) => a[0] - b[0]);
        dragIndex = points.findIndex((p) => p[0] === x && p[1] === y);
      }
      canvas.setPointerCapture(e.pointerId);
      buildCurveLUT(); requestRender(); drawCurveEditor();
    });
    canvas.addEventListener("pointermove", (e) => {
      if (dragIndex < 0) return;
      const clip = activeClip(); if (!clip) return;
      const points = curvePointsForActiveChannel();
      const { x, y } = eventToUnit(e);
      const isEndpoint = dragIndex === 0 || dragIndex === points.length - 1;
      points[dragIndex][1] = y;
      if (!isEndpoint) {
        const lo = points[dragIndex - 1][0] + 0.001, hi = points[dragIndex + 1][0] - 0.001;
        points[dragIndex][0] = clamp(x, lo, hi);
      }
      buildCurveLUT(); requestRender(); drawCurveEditor();
    });
    canvas.addEventListener("pointerup", () => { dragIndex = -1; stroke.end(); });
    canvas.addEventListener("dblclick", (e) => {
      const clip = activeClip(); if (!clip) return;
      const { x, y } = eventToUnit(e);
      const points = curvePointsForActiveChannel();
      const idx = nearestPointIndex(x, y);
      if (idx > 0 && idx < points.length - 1) {
        pushUndoSnapshot();
        points.splice(idx, 1);
        buildCurveLUT(); requestRender(); drawCurveEditor();
      }
    });
  }

  // ---- HSL ----

  function renderHslPanel() {
    const panel = $("czPanelHsl");
    if (!panel) return;
    panel.innerHTML = `
      <div class="cz-hsl-bands">${HSL_BANDS.map((b) => `<button class="cz-hsl-band-btn${b.key === CZ.activeHslBand ? " is-active" : ""}" style="background:${b.color}" data-band="${b.key}" title="${b.key}"></button>`).join("")}</div>
      <div id="czHslSliders"></div>`;
    panel.querySelectorAll(".cz-hsl-band-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        CZ.activeHslBand = btn.dataset.band;
        panel.querySelectorAll(".cz-hsl-band-btn").forEach((b) => b.classList.toggle("is-active", b === btn));
        renderHslSliders();
      });
    });
    renderHslSliders();
  }

  function renderHslSliders() {
    const host = $("czHslSliders");
    if (!host) return;
    const clip = activeClip();
    const band = (clip && clip.grade.hsl[CZ.activeHslBand]) || { hue: 0, sat: 0, lum: 0 };
    host.innerHTML = [
      sliderRowHtml("hue", "Hue", band.hue, -100, 100, 1),
      sliderRowHtml("sat", "Saturation", band.sat, -100, 100, 1),
      sliderRowHtml("lum", "Luminance", band.lum, -100, 100, 1),
    ].join("");
    ["hue", "sat", "lum"].forEach((k) => wireSliderRow(host, k, -100, 100, (v) => {
      const c = activeClip(); if (!c) return;
      c.grade.hsl[CZ.activeHslBand][k] = v; requestRender();
    }, 0));
  }

  // ---- LUT ----

  function renderLutPanel() {
    const panel = $("czPanelLut");
    if (!panel) return;
    const clip = activeClip();
    panel.innerHTML = `
      <button class="suite-btn suite-btn--secondary suite-btn--small" id="czImportLutBtn">Import LUT (.cube / .3dl)…</button>
      <ul class="cz-lut-list" id="czLutList"></ul>
      ${sliderRowHtml("lut_intensity", "Intensity", clip ? clip.grade.lut_intensity : 100, 0, 100, 1)}`;
    panel.querySelector("#czImportLutBtn").addEventListener("click", async () => {
      const res = await call("colorize_pick_and_import_lut");
      if (!res.ok) { toastIfError(res, "Couldn't import that LUT."); return; }
      toast(`Imported LUT "${res.lut.name}"`, "ok");
      await refreshLuts();
    });
    wireSliderRow(panel, "lut_intensity", 0, 100, (v) => {
      const c = activeClip(); if (!c) return;
      c.grade.lut_intensity = v; requestRender();
    }, 100);
    renderLutList();
  }

  function renderLutList() {
    const list = $("czLutList");
    if (!list) return;
    const clip = activeClip();
    if (!CZ.luts.length) {
      list.innerHTML = '<li class="suite-empty">No LUTs imported yet.</li>';
      return;
    }
    list.innerHTML = CZ.luts.map((l) => `
      <li class="cz-lut-item${clip && clip.lut_id === l.id ? " is-active" : ""}" data-id="${esc(l.id)}">
        <span class="cz-lut-item__name">${esc(l.name)}</span>
        <button class="cz-lut-item__remove" data-role="remove" title="Delete">✕</button>
      </li>`).join("");
    list.querySelectorAll(".cz-lut-item").forEach((li) => {
      li.addEventListener("click", async (e) => {
        if (e.target.dataset.role === "remove") return;
        const c = activeClip(); if (!c) return;
        pushUndoSnapshot();
        c.lut_id = li.dataset.id;
        c.grade.lut_id = li.dataset.id;
        await applyLutTexture(li.dataset.id);
        renderLutList(); renderMediaBin();
      });
      li.querySelector('[data-role="remove"]').addEventListener("click", async (e) => {
        e.stopPropagation();
        const res = await call("colorize_delete_lut", li.dataset.id);
        if (!res.ok) { toastIfError(res, "Couldn't delete that LUT."); return; }
        await refreshLuts();
      });
    });
  }

  async function refreshLuts() {
    const res = await call("colorize_list_luts");
    if (res.ok) CZ.luts = res.luts || [];
    renderLutList();
  }

  // ==========================================================================
  // batch operations
  // ==========================================================================

  function selectedClips() {
    return CZ.project.clips.filter((c) => CZ.selected.has(c.id));
  }

  function wireBatchButtons() {
    $("czCopyGradeBtn").addEventListener("click", () => {
      const source = activeClip();
      const targets = selectedClips();
      if (!source) { toast("Select a clip to copy the grade from first.", "error"); return; }
      if (!targets.length) { toast("Check one or more clips in the media bin first.", "error"); return; }
      targets.forEach((c) => { if (c.id !== source.id) c.grade = cloneGrade(source.grade); });
      toast(`Copied grade to ${targets.length} clip(s).`, "ok");
      renderMediaBin();
    });
    $("czApplyLutSelectedBtn").addEventListener("click", () => {
      const source = activeClip();
      const targets = selectedClips();
      if (!source || !source.lut_id) { toast("Apply a LUT to the active clip first.", "error"); return; }
      if (!targets.length) { toast("Check one or more clips in the media bin first.", "error"); return; }
      targets.forEach((c) => { c.lut_id = source.lut_id; c.grade.lut_id = source.lut_id; c.grade.lut_intensity = source.grade.lut_intensity; });
      toast(`Applied LUT to ${targets.length} clip(s).`, "ok");
      renderMediaBin();
    });
  }

  // ==========================================================================
  // project / presets
  // ==========================================================================

  function clipToDict(clip) {
    return {
      id: clip.id, source_path: clip.source_path, in_seconds: clip.in_seconds,
      out_seconds: clip.out_seconds, grade: clip.grade, lut_id: clip.lut_id, order: clip.order,
    };
  }

  async function saveProject() {
    const dict = {
      id: CZ.project.id, name: CZ.project.name,
      clips: CZ.project.clips.map((c, i) => ({ ...clipToDict(c), order: i })),
    };
    const res = await call("colorize_save_project", dict);
    if (!res.ok) { toastIfError(res, "Couldn't save the project."); return; }
    CZ.project.id = res.project_id;
    toast("Project saved.", "ok");
    refreshProjectList();
  }

  async function refreshProjectList() {
    const res = await call("colorize_list_projects");
    const select = $("czProjectSelect");
    if (!res.ok || !select) return;
    const current = CZ.project.id;
    select.innerHTML = '<option value="">New project…</option>' +
      (res.projects || []).map((p) => `<option value="${esc(p.id)}" ${p.id === current ? "selected" : ""}>${esc(p.name)} (${p.clip_count})</option>`).join("");
  }

  async function loadProject(id) {
    if (!id) {
      CZ.project = { id: null, name: "Untitled Project", clips: [] };
      CZ.activeClipIndex = -1; CZ.selected.clear();
      renderMediaBin();
      return;
    }
    const res = await call("colorize_load_project", id);
    if (!res.ok) { toastIfError(res, "Couldn't load that project."); return; }
    CZ.project = {
      id: res.project.id, name: res.project.name,
      clips: res.project.clips.map((c) => ({ ...c, _probe: null })),
    };
    CZ.selected.clear();
    CZ.activeClipIndex = -1;
    renderMediaBin();
    // Probe every clip in the background so media-bin durations populate
    // without blocking the project load itself.
    CZ.project.clips.forEach(async (c) => {
      const p = await call("colorize_probe_clip", c.source_path);
      if (p.ok) { c._probe = p.clip; renderMediaBin(); }
    });
    if (CZ.project.clips.length) loadClipIntoPreview(0);
  }

  async function refreshPresets() {
    const res = await call("colorize_list_presets");
    const select = $("czPresetSelect");
    if (!res.ok || !select) return;
    CZ.presets = res.presets || [];
    select.innerHTML = '<option value="">Load preset…</option>' +
      CZ.presets.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  }

  function wireProjectAndPresetControls() {
    $("czSaveProjectBtn").addEventListener("click", saveProject);
    $("czProjectSelect").addEventListener("change", (e) => loadProject(e.target.value));
    $("czResetGradeBtn").addEventListener("click", () => {
      const c = activeClip(); if (!c) return;
      pushUndoSnapshot();
      c.grade = defaultGrade(); c.lut_id = null;
      buildCurveLUT(); applyLutTexture(null);
      renderBasicPanel(); renderWheelsPanel(); renderCurvesPanel(); renderHslPanel(); renderLutPanel();
      requestRender();
    });
    $("czSavePresetBtn").addEventListener("click", async () => {
      const c = activeClip(); if (!c) { toast("Load a clip first.", "error"); return; }
      const name = $("czPresetName").value.trim();
      if (!name) { toast("Name the preset first.", "error"); return; }
      const res = await call("colorize_save_preset", name, c.grade);
      if (!res.ok) { toastIfError(res, "Couldn't save the preset."); return; }
      $("czPresetName").value = "";
      toast(`Preset "${name}" saved.`, "ok");
      refreshPresets();
    });
    $("czPresetSelect").addEventListener("change", (e) => {
      const id = e.target.value;
      if (!id) return;
      const preset = CZ.presets.find((p) => p.id === id);
      const c = activeClip();
      if (!preset || !c) return;
      pushUndoSnapshot();
      c.grade = cloneGrade(preset.grade);
      c.lut_id = c.grade.lut_id;
      buildCurveLUT(); applyLutTexture(c.lut_id);
      renderBasicPanel(); renderWheelsPanel(); renderCurvesPanel(); renderHslPanel(); renderLutPanel();
      requestRender();
    });
  }

  // ==========================================================================
  // export
  // ==========================================================================

  const trackedExportJobs = new Set();
  let exportPollTimer = null;

  function startExportPolling() {
    if (exportPollTimer) return;
    exportPollTimer = setInterval(async () => {
      if (!CZ.visible) return;
      const res = await call("suite_list_jobs");
      if (!res.ok) return;
      const jobs = (res.jobs || []).filter((j) => j.kind === "colorize_export" && (trackedExportJobs.has(j.id) || j.status === "running" || j.status === "queued"));
      jobs.forEach((j) => trackedExportJobs.add(j.id));
      renderExportJobs(jobs);
    }, 1000);
  }

  function renderExportJobs(jobs) {
    const host = $("czExportJobs");
    if (!host) return;
    if (!jobs.length) { host.innerHTML = ""; return; }
    host.innerHTML = jobs.map((j) => {
      const pct = Math.round(j.progress || 0);
      const statusCls = j.status === "running" ? "is-running" : j.status === "done" ? "is-done" : j.status === "error" ? "is-error" : j.status === "cancelled" ? "is-cancelled" : "is-queued";
      return `<div class="suite-job ${statusCls}">
        <div class="suite-job__head">
          <span class="suite-job__label" title="${esc(j.label)}">${esc(j.label)}</span>
          <span class="suite-job__status ${statusCls}">${esc(j.status)}</span>
        </div>
        <div class="suite-job__bar"><i style="width:${pct}%"></i></div>
        ${j.status === "error" ? `<div class="suite-job__error">${esc(j.error || "Export failed.")}</div>` : ""}
      </div>`;
    }).join("");
  }

  function wireExportButtons() {
    $("czExportSelectedBtn").addEventListener("click", async () => {
      const clips = selectedClips();
      if (!clips.length) { toast("Check one or more clips in the media bin first.", "error"); return; }
      await runBatchExport(clips);
    });
    $("czExportAllBtn").addEventListener("click", async () => {
      if (!CZ.project.clips.length) { toast("Import some clips first.", "error"); return; }
      await runBatchExport(CZ.project.clips);
    });
  }

  async function runBatchExport(clips) {
    const folderRes = await call("colorize_pick_export_folder");
    if (!folderRes.ok) { toastIfError(folderRes, "Couldn't choose an export folder."); return; }
    const preset = $("czExportPreset").value;
    const res = await call("colorize_export_batch", clips.map(clipToDict), folderRes.folder, preset);
    if (!res.ok) { toastIfError(res, "Couldn't start the export."); return; }
    (res.queued || []).forEach((q) => { if (q.job_id) trackedExportJobs.add(q.job_id); });
    const failed = (res.queued || []).filter((q) => q.error);
    if (failed.length) toast(`${failed.length} clip(s) couldn't be queued.`, "error");
    toast(`Queued ${(res.queued || []).length - failed.length} export(s).`, "ok");
    startExportPolling();
  }

  // ==========================================================================
  // init / visibility
  // ==========================================================================

  let initialized = false;

  function init() {
    if (initialized) return;
    initialized = true;
    const canvas = $("czCanvas");
    try {
      if (!initGL(canvas)) throw new Error("WebGL2 unavailable");
    } catch (e) {
      toast("This Mac's browser engine doesn't support WebGL2 — live grading preview is unavailable.", "error", 8000);
    }
    setupTransport();
    setActiveTab("basic");
    document.querySelectorAll(".cz-tab").forEach((b) => b.addEventListener("click", () => setActiveTab(b.dataset.tab)));
    renderBasicPanel(); renderWheelsPanel(); renderCurvesPanel(); renderHslPanel(); renderLutPanel();
    renderMediaBin();
    wireBatchButtons();
    wireProjectAndPresetControls();
    wireExportButtons();
    wireHistoryControls();
    $("czPickClipsBtn").addEventListener("click", pickClips);
    $("czClearClipsBtn").addEventListener("click", clearAllClips);
    refreshLuts();
    refreshProjectList();
    refreshPresets();
    startExportPolling();
  }

  // Cross-workspace hand-off event -- same loose-coupling contract as
  // "suite:workspace-changed" (see file header). The sender is expected
  // to switchWs("colorize") first, which fires that event synchronously
  // and calls init(); the defensive init() call here just covers a
  // caller that dispatches this before this workspace has ever been
  // shown (init() itself is idempotent, guarded by `initialized`).
  document.addEventListener("suite:send-to-colorize", (e) => {
    const paths = (e.detail && e.detail.paths) || [];
    if (!paths.length) return;
    init();
    addClipsByPath(paths);
  });

  document.addEventListener("suite:workspace-changed", (e) => {
    const active = e.detail && e.detail.ws === "colorize";
    CZ.visible = active;
    if (active) {
      init();
      resizeCanvasToVideo();
      requestRender();
      startRenderLoop();
    } else {
      const video = $("czVideo");
      if (video && !video.paused) video.pause();
    }
  });
})();
