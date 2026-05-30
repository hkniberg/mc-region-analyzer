"use strict";

const TICKS_PER_HOUR = 72000;

const state = {
  dim: "overworld",
  view: "world",                // 'world' | 'region'
  regionPos: null,              // {rx, rz}
  colorMode: "status",
  worldRows: [],
  worldByKey: new Map(),        // "rx,rz" -> row
  regionPayload: null,          // {region, chunks}
  chunkByKey: new Map(),        // "cx,cz" -> chunk row (for region view)
  hideEmpty: false,
  minInhHours: 0,
  camera: { x: 0, y: 0, zoom: 8 }, // (x,y) = world coords at canvas center
  selected: null,               // {kind: 'region'|'chunk', key: string}
  worldStats: null,             // min/max precomputed for current dim
};

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const coordReadout = document.getElementById("coord-readout");
const viewLabel = document.getElementById("view-label");
const infoBody = document.getElementById("info-body");
const statsBody = document.getElementById("stats-body");
const legendBody = document.getElementById("legend-body");
let tooltip = null;
let tooltipTarget = null;

// ----- Setup -----

function resizeCanvas() {
  const wrap = document.getElementById("canvas-wrap");
  const dpr = window.devicePixelRatio || 1;
  canvas.width = wrap.clientWidth * dpr;
  canvas.height = wrap.clientHeight * dpr;
  canvas.style.width = wrap.clientWidth + "px";
  canvas.style.height = wrap.clientHeight + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  render();
}
window.addEventListener("resize", resizeCanvas);

// ----- Data load -----

async function loadWorld(dim) {
  const res = await fetch(`/api/world?dim=${dim}`);
  if (!res.ok) throw new Error("api/world failed");
  state.worldRows = await res.json();
  state.worldByKey.clear();
  for (const r of state.worldRows) {
    state.worldByKey.set(`${r.rx},${r.rz}`, r);
  }
  computeWorldStats();
  fitToPopulated();
  updateStats();
  updateLegend();
  render();
}

async function loadRegion(dim, rx, rz) {
  const res = await fetch(`/api/region/${dim}/${rx}/${rz}`);
  if (!res.ok) throw new Error("api/region failed");
  state.regionPayload = await res.json();
  state.chunkByKey.clear();
  for (const c of state.regionPayload.chunks) {
    state.chunkByKey.set(`${c.cx},${c.cz}`, c);
  }
  render();
}

function computeWorldStats() {
  // Min/max for normalization in non-status color modes
  let minInhLog = Infinity, maxInhLog = -Infinity;
  let minBeLog = Infinity, maxBeLog = -Infinity;
  let minMod = Infinity, maxMod = -Infinity;
  let minRx = Infinity, maxRx = -Infinity, minRz = Infinity, maxRz = -Infinity;
  for (const r of state.worldRows) {
    if (r.rx < minRx) minRx = r.rx;
    if (r.rx > maxRx) maxRx = r.rx;
    if (r.rz < minRz) minRz = r.rz;
    if (r.rz > maxRz) maxRz = r.rz;
    if (r.max_inh && r.max_inh > 0) {
      const v = Math.log10(r.max_inh);
      if (v < minInhLog) minInhLog = v;
      if (v > maxInhLog) maxInhLog = v;
    }
    if (r.sum_be && r.sum_be > 0) {
      const v = Math.log10(r.sum_be);
      if (v < minBeLog) minBeLog = v;
      if (v > maxBeLog) maxBeLog = v;
    }
    if (r.max_modified) {
      if (r.max_modified < minMod) minMod = r.max_modified;
      if (r.max_modified > maxMod) maxMod = r.max_modified;
    }
  }
  state.worldStats = {
    minInhLog, maxInhLog, minBeLog, maxBeLog, minMod, maxMod,
    minRx, maxRx, minRz, maxRz,
  };
}

// ----- Color modes -----

function colorForRegion(r) {
  if (!regionPassesFilter(r)) return null; // hidden
  switch (state.colorMode) {
    case "status":     return statusColorRegion(r);
    case "inhabited":  return logColor(r.max_inh, state.worldStats.minInhLog, state.worldStats.maxInhLog, [240,240,255], [10,20,140]);
    case "be":         return logColor(r.sum_be,  state.worldStats.minBeLog,  state.worldStats.maxBeLog,  [255,250,235], [200,90,0]);
    case "modified":   return linearColor(r.max_modified, state.worldStats.minMod, state.worldStats.maxMod, [240,235,255], [90,30,150]);
    case "complete":   return completeColor(r);
  }
  return "#333";
}

function statusColorRegion(r) {
  if (r.scan_status === "error") return "#d33";
  if (r.scan_status === "empty") return "#3a3a3a"; // dark gray = empty stub
  // ok
  if ((r.chunks_visited || 0) > 0) return "#3aa05a";   // green = visited
  if ((r.chunks_full || 0) > 0)    return "#bda44a";   // yellow = generated, unvisited
  return "#555";                                       // partial-gen-only
}

function logColor(v, minLog, maxLog, lowRgb, highRgb) {
  if (!v || v <= 0) return "#1a1a1d";
  if (!isFinite(minLog) || !isFinite(maxLog) || maxLog === minLog) return rgbToCss(highRgb);
  const t = clamp((Math.log10(v) - minLog) / (maxLog - minLog), 0, 1);
  return rgbToCss(lerpRgb(lowRgb, highRgb, t));
}

function linearColor(v, lo, hi, lowRgb, highRgb) {
  if (v == null) return "#1a1a1d";
  if (lo === hi) return rgbToCss(highRgb);
  const t = clamp((v - lo) / (hi - lo), 0, 1);
  return rgbToCss(lerpRgb(lowRgb, highRgb, t));
}

function completeColor(r) {
  if (r.scan_status === "empty") return "#3a3a3a";
  if (r.scan_status === "error") return "#d33";
  const present = r.chunks_present || 0;
  if (!present) return "#1a1a1d";
  const frac = (r.chunks_full || 0) / present;
  return rgbToCss(lerpRgb([200,60,60], [60,180,90], frac));
}

function lerpRgb(a, b, t) {
  return [Math.round(a[0]+(b[0]-a[0])*t), Math.round(a[1]+(b[1]-a[1])*t), Math.round(a[2]+(b[2]-a[2])*t)];
}
function rgbToCss([r,g,b]) { return `rgb(${r},${g},${b})`; }
function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

// ----- Filters -----

function regionPassesFilter(r) {
  if (state.hideEmpty && r.scan_status === "empty") return false;
  if (state.minInhHours > 0) {
    const hours = (r.max_inh || 0) / TICKS_PER_HOUR;
    if (hours < state.minInhHours) return false;
  }
  return true;
}

// ----- Chunk colors (region view) -----

function colorForChunk(c) {
  switch (state.colorMode) {
    case "status":    return statusColorChunk(c);
    case "inhabited": return logColor(c.inhabited_ticks, 0, Math.log10(72000*1000), [240,240,255], [10,20,140]);
    case "be":        return logColor(c.block_entities_count, 0, 3, [255,250,235], [200,90,0]);
    case "modified": {
      const ts = state.regionPayload.chunks.map(x => x.last_modified).filter(Boolean);
      if (!ts.length) return "#1a1a1d";
      const lo = Math.min(...ts), hi = Math.max(...ts);
      return linearColor(c.last_modified, lo, hi, [240,235,255], [90,30,150]);
    }
    case "complete":  return c.status === "minecraft:full" ? "#3aa05a" : (c.status ? "#bda44a" : "#1a1a1d");
  }
  return "#333";
}

function statusColorChunk(c) {
  if (c.error) return "#d33";
  if ((c.inhabited_ticks || 0) > 0) return "#3aa05a";
  if (c.status === "minecraft:full") return "#bda44a";
  if (c.status) return "#555";
  return "#1a1a1d";
}

// ----- Rendering -----

function render() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.fillStyle = "#0e0e10";
  ctx.fillRect(0, 0, w, h);
  if (state.view === "world") renderWorld(w, h);
  else renderRegion(w, h);
  viewLabel.textContent = state.view === "world"
    ? `World — ${state.dim}`
    : `Region (${state.regionPos.rx}, ${state.regionPos.rz}) — ${state.dim}`;
}

function renderWorld(w, h) {
  const z = state.camera.zoom; // pixels per region
  const cx = w/2 - state.camera.x * z;
  const cy = h/2 - state.camera.y * z;
  // Determine visible region range
  const visMinRx = Math.floor((0 - cx) / z) - 1;
  const visMaxRx = Math.ceil((w - cx) / z) + 1;
  const visMinRz = Math.floor((0 - cy) / z) - 1;
  const visMaxRz = Math.ceil((h - cy) / z) + 1;

  for (const r of state.worldRows) {
    if (r.rx < visMinRx || r.rx > visMaxRx || r.rz < visMinRz || r.rz > visMaxRz) continue;
    const color = colorForRegion(r);
    if (!color) continue;
    const x = cx + r.rx * z;
    const y = cy + r.rz * z;
    ctx.fillStyle = color;
    ctx.fillRect(x, y, Math.max(1, z), Math.max(1, z));
  }

  if (z >= 6) {
    ctx.strokeStyle = "rgba(0,0,0,0.15)";
    ctx.lineWidth = 1;
    for (const r of state.worldRows) {
      if (r.rx < visMinRx || r.rx > visMaxRx || r.rz < visMinRz || r.rz > visMaxRz) continue;
      if (!regionPassesFilter(r)) continue;
      const x = Math.floor(cx + r.rx * z) + 0.5;
      const y = Math.floor(cy + r.rz * z) + 0.5;
      ctx.strokeRect(x, y, z, z);
    }
  }

  // Selection highlight
  if (state.selected && state.selected.kind === "region") {
    const [rx, rz] = state.selected.key.split(",").map(Number);
    const x = cx + rx * z, y = cy + rz * z;
    ctx.strokeStyle = "#ffb84d";
    ctx.lineWidth = 2;
    ctx.strokeRect(x - 0.5, y - 0.5, z + 1, z + 1);
  }

  // Origin marker
  if (z >= 3) {
    ctx.strokeStyle = "rgba(255,184,77,0.5)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx, 0); ctx.lineTo(cx, h);
    ctx.moveTo(0, cy); ctx.lineTo(w, cy);
    ctx.stroke();
  }
}

function renderRegion(w, h) {
  // 32×32 grid centered/fit in canvas. We render at fixed scale (computed once).
  const { rx, rz } = state.regionPos;
  const cellSize = Math.floor(Math.min(w, h) * 0.85 / 32);
  const gridSize = cellSize * 32;
  const offX = (w - gridSize) / 2;
  const offY = (h - gridSize) / 2;

  // Background panel
  ctx.fillStyle = "#16161a";
  ctx.fillRect(offX - 4, offY - 4, gridSize + 8, gridSize + 8);

  const baseCx = rx * 32, baseCz = rz * 32;
  for (let lz = 0; lz < 32; lz++) {
    for (let lx = 0; lx < 32; lx++) {
      const cx = baseCx + lx, cz = baseCz + lz;
      const c = state.chunkByKey.get(`${cx},${cz}`);
      const color = c ? colorForChunk(c) : "#0e0e10";
      ctx.fillStyle = color;
      ctx.fillRect(offX + lx * cellSize, offY + lz * cellSize, cellSize, cellSize);
    }
  }

  // Grid lines
  ctx.strokeStyle = "rgba(0,0,0,0.25)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 32; i++) {
    const p = i * cellSize;
    ctx.beginPath();
    ctx.moveTo(offX + p + 0.5, offY); ctx.lineTo(offX + p + 0.5, offY + gridSize);
    ctx.moveTo(offX, offY + p + 0.5); ctx.lineTo(offX + gridSize, offY + p + 0.5);
    ctx.stroke();
  }

  // Selection
  if (state.selected && state.selected.kind === "chunk") {
    const [cxs, czs] = state.selected.key.split(",").map(Number);
    const lx = cxs - baseCx, lz = czs - baseCz;
    if (lx >= 0 && lx < 32 && lz >= 0 && lz < 32) {
      ctx.strokeStyle = "#ffb84d";
      ctx.lineWidth = 2;
      ctx.strokeRect(offX + lx * cellSize - 1, offY + lz * cellSize - 1, cellSize + 2, cellSize + 2);
    }
  }

  // Store layout for pick
  state._regionLayout = { offX, offY, cellSize, baseCx, baseCz };
}

// ----- Picking -----

function pickAt(px, py) {
  if (state.view === "world") {
    const z = state.camera.zoom;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    const cx = w/2 - state.camera.x * z;
    const cy = h/2 - state.camera.y * z;
    const rx = Math.floor((px - cx) / z);
    const rz = Math.floor((py - cy) / z);
    const r = state.worldByKey.get(`${rx},${rz}`);
    if (r && regionPassesFilter(r)) return { kind: "region", key: `${rx},${rz}`, data: r };
    return null;
  } else {
    const L = state._regionLayout;
    if (!L) return null;
    const lx = Math.floor((px - L.offX) / L.cellSize);
    const lz = Math.floor((py - L.offY) / L.cellSize);
    if (lx < 0 || lx > 31 || lz < 0 || lz > 31) return null;
    const cx = L.baseCx + lx, cz = L.baseCz + lz;
    const c = state.chunkByKey.get(`${cx},${cz}`);
    return { kind: "chunk", key: `${cx},${cz}`, data: c, cx, cz };
  }
}

// ----- Camera helpers -----

function fitToPopulated() {
  // Bounding box of the dense cluster — 5th-95th percentile of OK regions.
  // Far-out exploration trails (e.g. rx=58594) are excluded; use "Fit all" to see them.
  const xs = [], zs = [];
  for (const r of state.worldRows) {
    if (r.scan_status === "ok") { xs.push(r.rx); zs.push(r.rz); }
  }
  if (xs.length === 0) { fitAll(); return; }
  xs.sort((a, b) => a - b);
  zs.sort((a, b) => a - b);
  const pct = (arr, p) => arr[Math.min(arr.length - 1, Math.max(0, Math.floor(arr.length * p)))];
  const minRx = pct(xs, 0.025), maxRx = pct(xs, 0.975);
  const minRz = pct(zs, 0.025), maxRz = pct(zs, 0.975);
  fitToBounds(minRx, maxRx, minRz, maxRz, 10);
}

function fitAll() {
  const s = state.worldStats;
  if (!s) return;
  fitToBounds(s.minRx, s.maxRx, s.minRz, s.maxRz, 5);
}

function fitToBounds(minRx, maxRx, minRz, maxRz, padding) {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const rangeX = Math.max(1, maxRx - minRx + 1);
  const rangeZ = Math.max(1, maxRz - minRz + 1);
  const zoom = Math.max(0.02, Math.min((w - 40) / rangeX, (h - 40) / rangeZ));
  state.camera.zoom = zoom;
  state.camera.x = (minRx + maxRx + 1) / 2;
  state.camera.y = (minRz + maxRz + 1) / 2;
}

// ----- Tooltip -----

function showTooltip(html, evt) {
  if (!tooltip) {
    tooltip = document.createElement("div");
    tooltip.className = "tooltip";
    document.body.appendChild(tooltip);
  }
  tooltip.innerHTML = html;
  tooltip.style.display = "block";
  tooltip.style.left = (evt.clientX + 14) + "px";
  tooltip.style.top  = (evt.clientY + 14) + "px";
}
function hideTooltip() { if (tooltip) tooltip.style.display = "none"; }

function regionTooltipHtml(r) {
  const maxH = ((r.max_inh || 0) / TICKS_PER_HOUR).toFixed(1);
  const sumH = ((r.sum_inh || 0) / TICKS_PER_HOUR).toFixed(0);
  return `
    <b>region (${r.rx}, ${r.rz})</b><br>
    status: ${r.scan_status}<br>
    chunks: ${r.chunks_present || 0} (full: ${r.chunks_full || 0}, visited: ${r.chunks_visited || 0})<br>
    max inh: ${maxH} h &nbsp; sum: ${sumH} h<br>
    block entities: ${r.sum_be || 0}<br>
    file: ${formatBytes(r.file_size)}
  `;
}
function chunkTooltipHtml(c) {
  if (!c) return `<b>chunk</b><br>(not generated)`;
  const h = ((c.inhabited_ticks || 0) / TICKS_PER_HOUR).toFixed(2);
  return `
    <b>chunk (${c.cx}, ${c.cz})</b><br>
    inhabited: ${h} h<br>
    status: ${c.status || "(none)"}<br>
    block entities: ${c.block_entities_count ?? 0}
  `;
}

// ----- Info panel -----

function showRegionInfo(r) {
  const maxH = ((r.max_inh || 0) / TICKS_PER_HOUR).toFixed(2);
  const sumH = ((r.sum_inh || 0) / TICKS_PER_HOUR).toFixed(1);
  const rows = [
    ["region",         `(${r.rx}, ${r.rz})`],
    ["dim",            state.dim],
    ["scan status",    r.scan_status],
    ["file size",      formatBytes(r.file_size)],
    ["chunks present", r.chunks_present ?? "—"],
    ["chunks full",    r.chunks_full ?? "—"],
    ["chunks visited", r.chunks_visited ?? "—"],
    ["max inhabited",  `${maxH} h`],
    ["sum inhabited",  `${sumH} h`],
    ["block entities", r.sum_be ?? 0],
    ["last modified",  r.max_modified ? new Date(r.max_modified*1000).toISOString().replace("T"," ").slice(0,19) : "—"],
  ];
  if (r.error) rows.push(["error", r.error]);
  let html = rows.map(([k,v]) => `<div class="info-row"><span class="k">${k}</span><span class="v">${escapeHtml(String(v))}</span></div>`).join("");
  html += `<div style="margin-top:8px"><button id="drill-in" class="full">Drill into region →</button></div>`;
  infoBody.innerHTML = html;
  infoBody.classList.remove("placeholder");
  document.getElementById("drill-in").addEventListener("click", () => enterRegionView(r.rx, r.rz));
}

function showChunkInfo(c, cx, cz) {
  if (!c) {
    infoBody.innerHTML = `<div class="info-row"><span class="k">chunk</span><span class="v">(${cx}, ${cz})</span></div><div style="color:#888;margin-top:6px">Not generated.</div>`;
    infoBody.classList.remove("placeholder");
    return;
  }
  const ticks = c.inhabited_ticks || 0;
  const hours = (ticks / TICKS_PER_HOUR);
  const blockX = c.cx * 16, blockZ = c.cz * 16;
  const rows = [
    ["chunk",          `(${c.cx}, ${c.cz})`],
    ["block coords",   `(${blockX}, ${blockZ}) → (${blockX+15}, ${blockZ+15})`],
    ["dim",            state.dim],
    ["status",         c.status || "—"],
    ["inhabited",      `${ticks.toLocaleString()} ticks (${hours.toFixed(2)} h)`],
    ["block entities", c.block_entities_count ?? 0],
    ["last modified",  c.last_modified ? new Date(c.last_modified*1000).toISOString().replace("T"," ").slice(0,19) : "—"],
  ];
  if (c.error) rows.push(["error", c.error]);
  infoBody.innerHTML = rows.map(([k,v]) => `<div class="info-row"><span class="k">${k}</span><span class="v">${escapeHtml(String(v))}</span></div>`).join("");
  infoBody.classList.remove("placeholder");
}

function updateStats() {
  let visible = 0, totalBytes = 0, withActivity = 0;
  for (const r of state.worldRows) {
    if (!regionPassesFilter(r)) continue;
    visible++;
    totalBytes += r.file_size || 0;
    if ((r.chunks_visited || 0) > 0) withActivity++;
  }
  statsBody.innerHTML = `
    <div class="info-row"><span class="k">regions total</span><span class="v">${state.worldRows.length}</span></div>
    <div class="info-row"><span class="k">visible</span><span class="v">${visible}</span></div>
    <div class="info-row"><span class="k">with activity</span><span class="v">${withActivity}</span></div>
    <div class="info-row"><span class="k">total size</span><span class="v">${formatBytes(totalBytes)}</span></div>
  `;
}

function updateLegend() {
  let html = "";
  if (state.colorMode === "status") {
    html = [
      ["#3aa05a", "visited (inhabited > 0)"],
      ["#bda44a", "generated, unvisited"],
      ["#555",    "partial-gen only"],
      ["#3a3a3a", "empty stub"],
      ["#d33",    "error"],
    ].map(([c,l]) => `<div class="legend-item"><span class="legend-swatch" style="background:${c}"></span>${l}</div>`).join("");
  } else if (state.colorMode === "inhabited") {
    html = `<div>log scale of <code>max_inh</code><br>light = low, dark blue = high</div>`;
  } else if (state.colorMode === "be") {
    html = `<div>log scale of <code>sum_be</code><br>light = low, orange = high</div>`;
  } else if (state.colorMode === "modified") {
    html = `<div>linear over file mtime<br>light = older, purple = newer</div>`;
  } else if (state.colorMode === "complete") {
    html = `<div>fraction of chunks with <code>status='minecraft:full'</code><br>red = low, green = high</div>`;
  }
  legendBody.innerHTML = html;
}

function formatBytes(b) {
  if (!b) return "—";
  if (b < 1024) return b + " B";
  if (b < 1024*1024) return (b/1024).toFixed(1) + " KB";
  if (b < 1024*1024*1024) return (b/(1024*1024)).toFixed(1) + " MB";
  return (b/(1024*1024*1024)).toFixed(2) + " GB";
}

function escapeHtml(s) { return s.replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch])); }

// ----- View transitions -----

function enterRegionView(rx, rz) {
  state.view = "region";
  state.regionPos = { rx, rz };
  state.selected = { kind: "region", key: `${rx},${rz}` };
  document.getElementById("back-to-world").style.display = "block";
  document.getElementById("fit-populated").style.display = "none";
  document.getElementById("fit-all").style.display = "none";
  loadRegion(state.dim, rx, rz);
}

function exitToWorld() {
  state.view = "world";
  state.regionPos = null;
  document.getElementById("back-to-world").style.display = "none";
  document.getElementById("fit-populated").style.display = "block";
  document.getElementById("fit-all").style.display = "block";
  render();
}

// ----- Mouse interaction -----

let drag = null;

canvas.addEventListener("mousedown", (e) => {
  drag = { x: e.clientX, y: e.clientY, camX: state.camera.x, camY: state.camera.y, moved: false };
});
window.addEventListener("mouseup", (e) => {
  if (!drag) return;
  if (!drag.moved) {
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left, py = e.clientY - rect.top;
    const hit = pickAt(px, py);
    if (hit) {
      state.selected = { kind: hit.kind, key: hit.key };
      if (hit.kind === "region") showRegionInfo(hit.data);
      else showChunkInfo(hit.data, hit.cx, hit.cz);
      render();
    }
  }
  drag = null;
});
canvas.addEventListener("mousemove", (e) => {
  const rect = canvas.getBoundingClientRect();
  const px = e.clientX - rect.left, py = e.clientY - rect.top;

  if (drag && state.view === "world") {
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) drag.moved = true;
    state.camera.x = drag.camX - dx / state.camera.zoom;
    state.camera.y = drag.camY - dy / state.camera.zoom;
    render();
  }

  if (state.view === "world") {
    const z = state.camera.zoom;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    const cx = w/2 - state.camera.x * z;
    const cy = h/2 - state.camera.y * z;
    const rx = Math.floor((px - cx) / z);
    const rz = Math.floor((py - cy) / z);
    coordReadout.textContent = `rx=${rx}  rz=${rz}  (block ${rx*512}, ${rz*512})`;
    const hit = pickAt(px, py);
    if (hit) {
      showTooltip(regionTooltipHtml(hit.data), e);
    } else {
      hideTooltip();
    }
  } else {
    const hit = pickAt(px, py);
    if (hit) {
      coordReadout.textContent = `cx=${hit.cx}  cz=${hit.cz}  (block ${hit.cx*16}, ${hit.cz*16})`;
      showTooltip(chunkTooltipHtml(hit.data), e);
    } else {
      coordReadout.textContent = `region (${state.regionPos.rx}, ${state.regionPos.rz})`;
      hideTooltip();
    }
  }
});
canvas.addEventListener("mouseleave", () => { hideTooltip(); drag = null; });

canvas.addEventListener("dblclick", (e) => {
  if (state.view !== "world") return;
  const rect = canvas.getBoundingClientRect();
  const hit = pickAt(e.clientX - rect.left, e.clientY - rect.top);
  if (hit && hit.kind === "region") {
    hideTooltip();
    enterRegionView(hit.data.rx, hit.data.rz);
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.view === "region") {
    exitToWorld();
  }
});

canvas.addEventListener("wheel", (e) => {
  if (state.view !== "world") return;
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const px = e.clientX - rect.left, py = e.clientY - rect.top;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  // World coord under cursor before zoom
  const z = state.camera.zoom;
  const worldX = state.camera.x + (px - w/2) / z;
  const worldY = state.camera.y + (py - h/2) / z;
  const factor = e.deltaY < 0 ? 1.15 : (1/1.15);
  state.camera.zoom = clamp(z * factor, 0.02, 64);
  // Reposition so world coord stays under cursor
  state.camera.x = worldX - (px - w/2) / state.camera.zoom;
  state.camera.y = worldY - (py - h/2) / state.camera.zoom;
  render();
}, { passive: false });

// ----- UI controls -----

document.querySelectorAll("#dim-buttons button").forEach(btn => {
  btn.addEventListener("click", () => {
    const dim = btn.dataset.dim;
    document.querySelectorAll("#dim-buttons button").forEach(b => b.classList.toggle("active", b === btn));
    state.dim = dim;
    if (state.view === "region") exitToWorld();
    state.selected = null;
    infoBody.classList.add("placeholder");
    infoBody.textContent = "Hover or click a region.";
    loadWorld(dim);
  });
});

document.querySelectorAll('#color-mode input[name="cm"]').forEach(radio => {
  radio.addEventListener("change", () => {
    state.colorMode = radio.value;
    updateLegend();
    render();
  });
});

document.getElementById("hide-empty").addEventListener("change", (e) => {
  state.hideEmpty = e.target.checked;
  updateStats();
  render();
});

const minInhSlider = document.getElementById("min-inh");
const minInhLabel = document.getElementById("min-inh-label");
minInhSlider.addEventListener("input", () => {
  state.minInhHours = Number(minInhSlider.value);
  minInhLabel.textContent = state.minInhHours;
  updateStats();
  render();
});

document.getElementById("fit-populated").addEventListener("click", () => { fitToPopulated(); render(); });
document.getElementById("fit-all").addEventListener("click", () => { fitAll(); render(); });
document.getElementById("back-to-world").addEventListener("click", exitToWorld);

// ----- Boot -----

resizeCanvas();
loadWorld(state.dim).catch(err => {
  ctx.fillStyle = "#d33";
  ctx.font = "14px sans-serif";
  ctx.fillText("Failed to load: " + err.message, 20, 30);
});
