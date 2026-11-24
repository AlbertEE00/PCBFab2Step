// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Albert van Zyl
//
// The PCBFab2Step GUI. Its whole state is a command line: inputs, output and
// option values go to the back end, which answers with the command line,
// what it finds and the scene to show. Nothing here exports anything by
// itself: the Export buttons run the command line that is shown.

import { Viewer } from "pcbfab2step/viewer";
import { mountChrome } from "pcbfab2step/viewer-ui";

const KEY = new URLSearchParams(location.search).get("t") || "";
const CHEVRON = '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4.5 2.5L8 6l-3.5 3.5"/></svg>';
const FORMATS = ["dxf", "svg", "step"];

// ---------------------------------------------------------------------------
// helpers

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "html") el.innerHTML = v;
    else if (k === "class") el.className = v;
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c);
  return el;
}

async function api(path, body) {
  const url = `${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(KEY)}`;
  const res = await fetch(url, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const j = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) throw new Error(j.error || res.statusText);
  return j;
}

const rgb = (c) => (c ? `rgb(${c.map((v) => Math.round(v * 255)).join(",")})` : "transparent");
const base = (p) => (p || "").split(/[\\/]/).filter(Boolean).pop() || p;
const dir = (p) => { const i = Math.max((p || "").lastIndexOf("/"), (p || "").lastIndexOf("\\")); return i > 0 ? p.slice(0, i) : ""; };
function thick(mm) {
  if (!mm) return "";
  return mm < 1 ? `${+(mm * 1000).toFixed(mm * 1000 < 10 ? 1 : 0)} µm` : `${+mm.toFixed(3)} mm`;
}

// ---------------------------------------------------------------------------
// state

const S = { inputs: [], output: "", values: {}, layer: null, exact: false };
let M = null;          // the option model
let ST = null;         // the last state from the back end
let inApp = false;     // a pywebview window (native dialogs, dropped paths)
let windows = true;

function request(extra = {}) {
  const spec = S.layer ? `${S.layer.target}:${S.layer.fmt}` : null;
  return { inputs: S.inputs, output: S.output || null, values: S.values, export: spec, ...extra };
}

// ---------------------------------------------------------------------------
// layout

document.body.append(
  h("aside", { class: "side", "aria-label": "Inputs and options" }),
  h("main", { class: "viewport", id: "viewer" }),
  h("aside", { class: "layers", "aria-label": "Layers" }),
  h("footer", { class: "cmd" }),
);
const side = document.querySelector(".side");
const vp = document.getElementById("viewer");
const layersEl = document.querySelector(".layers");
const cmdEl = document.querySelector(".cmd");

const viewer = new Viewer(vp);
const chrome = mountChrome(vp, viewer);
window.pcbViewer = viewer;

const emptyEl = h("div", { class: "empty" });
const statusEl = h("span", { class: "status", role: "status" });
const previewBtn = h("button", { class: "tb", "aria-pressed": "true", title: "Meshes from the 2D regions: quick" }, "Preview");
const exactBtn = h("button", { class: "tb", "aria-pressed": "false", title: "Build the STEP's own solids and show those (slower)" }, "Exact");
const modebar = h("div", { class: "modebar" }, statusEl, h("div", { class: "seg" }, previewBtn, exactBtn));
const layerTitle = h("span");
const backBtn = h("button", { class: "btn small primary" }, "Back to the model");
const layerbar = h("div", { class: "layerbar", hidden: true }, layerTitle, backBtn);
vp.append(emptyEl, modebar, layerbar);

previewBtn.addEventListener("click", () => { if (S.exact) { S.exact = false; syncMode(); requestScene(); } });
let exactFor = null;            // the settings the exact view was asked for: a change goes back to the preview
exactBtn.addEventListener("click", () => { if (!S.exact) { S.exact = true; exactFor = ST && ST.geometry; syncMode(); requestScene(); } });
backBtn.addEventListener("click", () => setLayer(null));

function syncMode() {
  previewBtn.setAttribute("aria-pressed", String(!S.exact));
  exactBtn.setAttribute("aria-pressed", String(S.exact));
  previewBtn.disabled = exactBtn.disabled = !!S.layer;
  layerbar.hidden = !S.layer;
  layerTitle.textContent = "Layer view";
}

function setStatus(text, busy = false) {
  statusEl.textContent = text;
  statusEl.classList.toggle("busy", busy);
}

function showEmpty(title, text, isError = false) {
  emptyEl.hidden = !title;
  vp.classList.toggle("blank", !!title && !viewer.manifest);
  emptyEl.replaceChildren();
  if (title) emptyEl.append(h("div", {}, h("b", {}, title), h("div", { class: isError ? "err" : "" }, text)));
}

// ---------------------------------------------------------------------------
// left: inputs

const inputsEl = h("section", { class: "block", "aria-label": "Inputs" });
const outputEl = h("section", { class: "block", "aria-label": "Output" });
const groupsEl = h("section", { class: "groups", "aria-label": "Options" });

async function pick(mode, path = "", types = []) {
  if (!inApp) return [];
  try {
    return (await api("api/pick", { mode, path, types })).paths || [];
  } catch (e) {
    setStatus(e.message);
    return [];
  }
}

function setInputs(paths, add = false) {
  const list = add ? [...S.inputs] : [];
  for (const p of paths) if (p && !list.includes(p)) list.push(p);
  if (!add) { S.output = ""; S.layer = null; S.exact = false; syncMode(); }
  S.inputs = list;
  renderInputs();
  changed();
}

const FAB_TYPES = ["Fab data (*.tgz;*.tar;*.zip;*.gz;*.cvg;*.xml;*.DRR;*.step;*.stp)", "All files (*.*)"];

function renderInputs() {
  inputsEl.replaceChildren(h("h2", {}, "Inputs"));
  if (!S.inputs.length) {
    inputsEl.append(h("div", { class: "drop big", "data-drop": "replace" },
      h("h3", {}, "Drop the fab outputs here"),
      h("p", {}, "A folder, an ODB++ job or archive, or an IPC-2581 file. NC drill output and the ECAD's STEP are found next to them."),
      inApp ? h("div", { class: "row" },
        h("button", { class: "btn primary", on: { click: async () => setInputs(await pick("folder")) } }, "Choose folder"),
        h("button", { class: "btn", on: { click: async () => setInputs(await pick("file", "", FAB_TYPES)) } }, "Choose file")) : null),
      addPathRow());
    return;
  }
  const src = ST && ST.sources;
  const rows = [];
  const row = (k, path, extra) => rows.push(h("div", { class: "src" }, h("span", { class: "k" }, k),
    h("div", { class: "v", title: path || "" }, path ? [h("div", { class: "file" }, base(path)), h("div", { class: "dir" }, h("bdi", {}, dir(path)))] : extra), h("span")));
  const slot = (k, text, mode, types) => rows.push(h("div", { class: "src" }, h("span", { class: "k" }, k),
    h("div", { class: "v" }, h("div", { class: "slot", "data-drop": "add" }, h("span", {}, text),
      inApp ? h("button", { class: "btn small", on: { click: async () => setInputs(await pick(mode, S.inputs[0] || "", types), true) } }, "Choose") : null)), h("span")));
  if (src && src.primary) {
    const label = { odb: "ODB++", ipc: "IPC-2581" }[src.primary];
    row(label, src.path);
    if (src.primary === "odb") {
      if (src.ipc_hints) row("IPC-2581", src.ipc_hints);
      else slot("IPC-2581", "Optional: counterhole types", "file", ["IPC-2581 (*.cvg;*.xml)"]);
    }
    if (src.odb_types) row("ODB++", src.odb_types);
    if (src.drill && src.drill.length) for (const d of src.drill) row("NC drill", d);
    else slot("NC drill", "Not found: drop its folder here", "folder");
    if (src.step) row("STEP", src.step);
    else slot("STEP", "Not found: the ECAD's STEP adds the components", "file", ["STEP (*.step;*.stp)"]);
    if (src.gerber) rows.push(h("div", { class: "src" }, h("span", { class: "k" }, "Gerber"), h("div", { class: "v muted" }, `${src.gerber} file(s), not read yet`), h("span")));
    rows.push(h("div", { class: "src" }, h("span", { class: "k" }, "Settings"),
      h("div", { class: "v", title: src.config || "" }, src.config ? [h("div", { class: "file" }, base(src.config)), h("div", { class: "dir" }, h("bdi", {}, dir(src.config)))] : h("span", { class: "missing" }, "No config.toml: the defaults")), h("span")));
  }
  inputsEl.append(...rows);
  const given = h("div", { class: "src" }, h("span", { class: "k" }, "Given"),
    h("div", { class: "v" }, ...S.inputs.map((p, i) => h("div", { class: "file", title: p, style: "display:flex;gap:6px;align-items:center" },
      h("span", { style: "overflow:hidden;text-overflow:ellipsis" }, base(p)),
      h("button", { class: "btn quiet small", title: `Remove ${p}`, "aria-label": `Remove ${p}`, on: { click: () => { S.inputs.splice(i, 1); if (!S.inputs.length) setInputs([]); else { renderInputs(); changed(); } } } }, "×")))), h("span"));
  inputsEl.append(given);
  if (src && src.notes && src.notes.length) inputsEl.append(h("ul", { class: "notes" }, ...src.notes.map((n) => h("li", {}, n))));
  if (ST && ST.warnings && ST.warnings.length) inputsEl.append(h("ul", { class: "notes" }, ...ST.warnings.filter((w) => !(src && src.notes || []).includes(w)).map((n) => h("li", {}, n))));
  inputsEl.append(addPathRow(true));
  if (inApp) inputsEl.append(h("div", { class: "row", style: "display:flex;gap:6px;margin-top:8px" },
    h("button", { class: "btn small", on: { click: async () => setInputs(await pick("folder", S.inputs[0] || "")) } }, "Another board"),
    h("button", { class: "btn small", on: { click: async () => setInputs(await pick("file", S.inputs[0] || "", FAB_TYPES), true) } }, "Add a file")));
}

function addPathRow(add = false) {
  const inp = h("input", { type: "text", placeholder: add ? "Add a path: paste it, press Enter" : "Or paste a path, press Enter", "aria-label": "Path" });
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && inp.value.trim()) setInputs([inp.value.trim().replace(/^"|"$/g, "")], add);
  });
  return h("div", { class: "addpath" }, inp);
}

function renderOutput() {
  const inp = h("input", { type: "text", value: S.output, placeholder: (ST && ST.output) || "PCBFab2Step/<board>.step in the folder", "aria-label": "Output file" });
  inp.addEventListener("change", () => { S.output = inp.value.trim().replace(/^"|"$/g, ""); changed(); });
  outputEl.replaceChildren(h("h2", {}, "STEP model"), h("div", { class: "out" }, inp,
    inApp ? h("button", { class: "btn", on: { click: async () => {
      const got = await pick("save", S.output || (ST && ST.output) || "", ["STEP (*.step;*.stp)"]);
      if (got.length) { S.output = got[0]; inp.value = got[0]; changed(); }
    } } }, "Browse") : null));
  outputEl.querySelector("input").title = "Where the STEP model goes (-o)";
}

// ---------------------------------------------------------------------------
// left: options (built once; sync() writes values into them)

const controls = new Map();     // option id -> {row, get, set, setPlaceholder}

function setValue(id, v) {
  if (v === undefined || v === null || v === "") delete S.values[id];
  else S.values[id] = v;
  syncOptions();
  changed();
}

function control(o) {
  const tip = `${o.help}\n${o.flag}`;
  const reset = h("button", { class: "reset", title: "Back to the default", "aria-label": `Reset ${o.label}`, html: "&times;" });
  reset.addEventListener("click", () => setValue(o.id, null));
  let el, get, set, ph = () => {};
  const commit = () => setValue(o.id, get());
  if (o.type === "choice" || o.type === "tristate") {
    const names = o.type === "tristate" ? { true: "Yes", false: "No" } : {};
    el = h("select", { "aria-label": o.label }, h("option", { value: "" }, `Default`), ...o.choices.map((c) => h("option", { value: c }, names[c] || c)));
    get = () => el.value;
    set = (v) => (el.value = v == null ? "" : String(v));
    ph = (eff) => (el.options[0].textContent = eff ? `Default (${eff})` : `Default (${o.default || "fab data"})`);
    el.addEventListener("change", commit);
  } else if (o.type === "check") {
    const box = h("input", { type: "checkbox", "aria-label": o.label });
    el = h("div", { class: "check" }, h("label", { class: "switch" }, box, h("i")));
    let dflt = false;
    get = () => box.checked;
    set = (v) => (box.checked = v == null ? dflt : !!v);
    ph = (eff) => { dflt = eff === "on" || (eff === "" && o.default === "on"); };
    box.addEventListener("change", commit);
  } else if (o.type === "color") {
    const sel = h("select", { "aria-label": o.label }, h("option", { value: "" }, "Default"), ...M.colors.map((c) => h("option", { value: c }, c)), h("option", { value: "#custom" }, "Custom"));
    const pickIn = h("input", { type: "color", "aria-label": `${o.label}: custom` });
    const sw = h("span", { class: "sw" }, pickIn);
    el = h("div", { class: "colorpick" }, sw, sel);
    get = () => (sel.value === "#custom" ? pickIn.value : sel.value);
    set = (v) => {
      if (v && String(v).startsWith("#")) {
        if (![...sel.options].some((x) => x.value === v)) sel.value = "#custom";
        pickIn.value = v;
      } else sel.value = v || "";
    };
    ph = (eff) => (sel.options[0].textContent = `Default (${eff || o.default})`);
    sel.addEventListener("change", () => { if (sel.value === "#custom") pickIn.click(); else commit(); });
    pickIn.addEventListener("change", () => { sel.value = "#custom"; commit(); });
    el._swatch = sw;
  } else if (o.type === "lines") {
    el = h("textarea", { rows: 2, "aria-label": o.label, placeholder: o.flag });
    get = () => el.value.trim();
    set = (v) => (el.value = v || "");
    el.addEventListener("change", commit);
  } else if (o.type === "counterhole") {
    const typ = h("select", { "aria-label": `${o.label}: type` }, h("option", { value: "" }, "Type"), h("option", { value: "bore" }, "bore"), h("option", { value: "sink" }, "sink"));
    const ang = h("input", { type: "text", placeholder: "angle °", "aria-label": `${o.label}: angle` });
    const dep = h("input", { type: "text", placeholder: "depth", "aria-label": `${o.label}: depth` });
    el = h("div", { class: "chole" }, typ, ang, dep);
    get = () => [typ.value && `type=${typ.value}`, ang.value.trim() && `angle=${ang.value.trim()}`, dep.value.trim() && `depth=${dep.value.trim()}`].filter(Boolean).join(",");
    set = (v) => {
      const f = Object.fromEntries(String(v || "").split(/[,;\s]+/).filter(Boolean).map((t) => t.includes("=") ? t.split("=") : ["type", t]));
      typ.value = (f.type || "").replace(/^counter/, "").replace("bore", "bore").replace("sink", "sink");
      ang.value = f.angle || "";
      dep.value = f.depth || "";
    };
    for (const x of [typ, ang, dep]) x.addEventListener("change", commit);
  } else {
    el = h("input", { type: "text", "aria-label": o.label });
    get = () => el.value.trim();
    set = (v) => (el.value = v == null ? "" : String(v));
    ph = (eff) => (el.placeholder = eff || o.default || "");
    el.addEventListener("change", commit);
    if (o.type === "path" && inApp) {
      const b = h("button", { class: "btn small", on: { click: async () => { const got = await pick("file", el.value); if (got.length) setValue(o.id, got[0]); } } }, "Browse");
      el = h("div", { class: "out" }, el, b);
    }
  }
  const wide = o.type === "lines" || o.type === "counterhole";
  const row = h("div", { class: `opt${wide ? " wide" : ""}`, "data-id": o.id, title: tip }, h("label", {}, o.label), el, reset);
  if (wide) row.insertBefore(reset, row.children[1]);
  controls.set(o.id, { row, get, set, ph, el, o });
  return row;
}

const groupEls = [];

function buildGroups() {
  groupsEl.replaceChildren(h("div", { class: "block", style: "border:0;padding-bottom:0" }, h("h2", {}, "Options")));
  for (const g of M.groups) {
    const body = h("div", { class: "gbody" }, ...g.options.map(control));
    const count = h("span", { class: "count" });
    const open = h("button", { class: "open", "aria-expanded": "false", html: CHEVRON });
    open.append(h("span", {}, g.title), count);
    const head = h("div", { class: "ghead" }, open);
    let box = null;
    if (g.switch) {
      box = h("input", { type: "checkbox", "aria-label": g.title });
      head.append(h("label", { class: "switch", title: `${g.switch.help}\n${g.switch.flag}` }, box, h("i")));
      box.addEventListener("change", () => {
        const sw = g.switch;
        if (sw.id === "sanitize") {
          if (box.checked) setValue("sanitize", S.values.sanitize && S.values.sanitize !== "none" ? S.values.sanitize : "exposed");
          else setValue("sanitize", ST && ST.origins && ST.origins.sanitize && !String(ST.origins.sanitize).startsWith("--") ? "none" : null);
        } else setValue(sw.id, box.checked);
      });
    }
    const el = h("div", { class: "group" }, head, body);
    open.addEventListener("click", () => {
      el.classList.toggle("expanded");
      open.setAttribute("aria-expanded", String(el.classList.contains("expanded")));
    });
    if (!g.options.length) open.querySelector("svg").style.visibility = "hidden";
    groupsEl.append(el);
    groupEls.push({ g, el, count, box });
  }
}

function syncOptions() {
  const eff = (ST && ST.effective) || {};
  for (const [id, c] of controls) {
    c.ph(eff[id]);
    if (document.activeElement && c.row.contains(document.activeElement) && c.o.type !== "choice") {
      /* leave a field being typed in */
    } else c.set(S.values[id]);
    c.row.classList.toggle("set", id in S.values);
    if (c.el._swatch) {
      const v = S.values[id] || eff[id];
      c.el._swatch.style.background = v && v !== "auto" ? (String(v).startsWith("#") ? v : cssColor(v)) : "transparent";
    }
  }
  for (const { g, el, count, box } of groupEls) {
    const ids = new Set(g.options.map((o) => o.id));
    if (g.switch) ids.add(g.switch.id);
    const n = [...ids].filter((id) => id in S.values).length;
    count.textContent = n ? String(n) : "";
    if (box) {
      const sw = g.switch;
      const on = sw.id === "sanitize" ? (S.values.sanitize ? S.values.sanitize !== "none" : !!(ST && ST.switches && ST.switches.sanitize))
        : (sw.id in S.values ? !!S.values[sw.id] : ST && ST.switches && sw.id in ST.switches ? ST.switches[sw.id] : sw.default);
      box.checked = on;
      el.classList.toggle("off", !on);
    }
  }
}

let NAMED = {};
function cssColor(name) {
  return NAMED[name] || "transparent";
}

// ---------------------------------------------------------------------------
// right: the stackup column

let stackRows = [];
let selected = null;           // generic name of the expanded layer row
const fmtOf = {};              // generic -> chosen format

function setLayer(layer) {
  S.layer = layer;
  if (layer) S.exact = false;
  syncMode();
  renderLayers();
  changed();
}

const ROW_COLOR = { copper: "copper", mask: "mask", silk: "silk", paste: "paste", dielectric: "dielectric", outline: "dielectric" };

function renderLayers() {
  const xopts = buildExportOptions();
  const list = h("ul", { class: "stack" });
  const live = (ST && ST.colors) || {};
  for (const r0 of stackRows) {
    const r = live[ROW_COLOR[r0.kind]] ? { ...r0, color: live[ROW_COLOR[r0.kind]] } : r0;
    if (r.kind === "dielectric") {
      const t = r.t || 0;
      const px = Math.max(6, Math.min(30, Math.round(5 + 9 * Math.log10(Math.max(t * 1000, 5) / 5))));
      list.append(h("li", { class: "diel", title: `${r.name} ${r.material || ""} ${thick(t)}` },
        h("span", { class: "band", style: `background:${rgb(r.color)};height:${px}px` }),
        h("span", { class: "nm", style: `line-height:${Math.max(px - 2, 8)}px;height:${px}px;overflow:hidden` }, px >= 14 ? [h("b", {}, r.name), h("span", {}, r.material || "")] : null),
        h("span", { class: "th" }, px >= 14 ? thick(t) : "")));
      continue;
    }
    const g = r.generic;
    const isSel = selected === g;
    const viewing = S.layer && S.layer.target === g;
    const li = h("li", {
      class: `layer${r.kind === "outline" ? " outline" : ""}${r.data ? "" : " nodata"}${viewing || isSel ? " sel" : ""}`,
      tabindex: "0", role: "button", "aria-expanded": String(isSel), title: r.data ? `${g} (${r.name})` : `${g}: no data in this layer`,
    },
      h("span", { class: "band", style: `background:${rgb(r.color)}` }),
      h("span", { class: "nm" }, h("b", {}, g), r.name && r.name !== g ? h("span", {}, r.name) : null),
      h("span", { class: "th" }, r.data ? thick(r.t) : "empty"));
    const toggle = () => { selected = isSel ? null : g; renderLayers(); };
    li.addEventListener("click", (e) => { if (!e.target.closest(".acts")) toggle(); });
    li.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && e.target === li) { e.preventDefault(); toggle(); } });
    if (isSel) {
      const fmt = fmtOf[g] || (S.layer && S.layer.target === g ? S.layer.fmt : r.kind === "stencil" ? "step" : "svg");
      fmtOf[g] = fmt;
      const seg = h("div", { class: "seg", role: "group", "aria-label": "Format" }, ...FORMATS.map((f) =>
        h("button", { "aria-pressed": String(f === fmt), on: { click: () => { fmtOf[g] = f; if (S.layer && S.layer.target === g) setLayer({ target: g, fmt: f }); else renderLayers(); } } }, f.toUpperCase())));
      const view = h("button", { class: "btn small", disabled: !r.data, on: { click: () => setLayer({ target: g, fmt }) } }, viewing ? "Viewing" : "View");
      const exp = h("button", { class: "btn small", disabled: !r.data, on: { click: () => runCommand(`${g}:${fmt}`) } }, "Export");
      li.append(h("div", { class: "acts" }, seg, h("span", { class: "grow" }), view, exp));
    }
    list.append(li);
  }
  layersEl.replaceChildren(
    h("header", {}, h("h2", {}, "Layers"), h("p", {}, "Top to bottom. Pick one to view it or export it as it is on the board.")),
    xopts, list);
}

let xoptsOpen = false;
function buildExportOptions() {
  const rows = M.export.map((o) => { const r = control(o); return r; });
  const open = h("button", { class: "open", html: CHEVRON, "aria-expanded": String(xoptsOpen) });
  open.append(h("span", {}, "Export settings"));
  const n = M.export.filter((o) => o.id in S.values).length;
  if (n) open.append(h("span", { class: "count", style: "min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:var(--gold);color:var(--gold-ink);font-size:11px;font-weight:600;display:grid;place-items:center" }, String(n)));
  const el = h("div", { class: `xopts${xoptsOpen ? " expanded" : ""}` }, open, h("div", { class: "xbody" }, ...rows));
  open.addEventListener("click", () => { xoptsOpen = !xoptsOpen; el.classList.toggle("expanded", xoptsOpen); open.setAttribute("aria-expanded", String(xoptsOpen)); });
  setTimeout(syncOptions);
  return el;
}

// ---------------------------------------------------------------------------
// bottom: the command line, running it

const cmdCode = h("code");
const cmdErr = h("div", { class: "cmderr err", role: "alert" });
const copyBtn = h("button", { class: "btn" }, "Copy");
const logBtn = h("button", { class: "btn quiet", "aria-pressed": "false" }, "Log");
const runBtn = h("button", { class: "btn primary" }, "Export STEP");
const logEl = h("div", { class: "log", "aria-live": "polite" });
const doneEl = h("div", { class: "done" });
cmdEl.append(cmdErr, logEl, doneEl, h("div", { class: "cmdrow" },
  h("div", { class: "cmdline", title: "The command line of these settings: the Export button runs it" }, h("span", { class: "ps" }, windows ? ">" : "$"), cmdCode),
  copyBtn, logBtn, runBtn));

copyBtn.addEventListener("click", async () => {
  const text = cmdCode.textContent;
  try { await navigator.clipboard.writeText(text); } catch {
    const t = h("textarea", {}, text); document.body.append(t); t.select(); document.execCommand("copy"); t.remove();
  }
  copyBtn.textContent = "Copied";
  setTimeout(() => (copyBtn.textContent = "Copy"), 1200);
});
logBtn.addEventListener("click", () => { const on = !logEl.classList.contains("open"); logEl.classList.toggle("open", on); logBtn.setAttribute("aria-pressed", String(on)); });
runBtn.addEventListener("click", () => runCommand(S.layer ? `${S.layer.target}:${S.layer.fmt}` : null));

function renderCmd() {
  cmdCode.textContent = ST ? ST.command : "pcbfab2step";
  cmdErr.textContent = ST && ST.error && S.inputs.length ? ST.error : "";
  runBtn.textContent = S.layer ? `Export ${S.layer.target}.${S.layer.fmt}` : "Export STEP";
  runBtn.disabled = !ST || !!ST.error || running;
}

let running = false;
function logLine(line) {
  const cls = /^WARNING/.test(line) ? "w" : /^ERROR/.test(line) ? "e" : /^Wrote |^Done /.test(line) ? "ok" : "";
  logEl.append(h("div", { class: cls }, line));
  logEl.scrollTop = logEl.scrollHeight;
}

async function runCommand(spec) {
  if (running || !ST || ST.error) return;
  running = true;
  renderCmd();
  logEl.replaceChildren();
  doneEl.replaceChildren();
  logEl.classList.add("open");
  logBtn.setAttribute("aria-pressed", "true");
  setStatus(spec ? `Exporting ${spec}` : "Writing the STEP model", true);
  try {
    const r = await api("api/run", request({ export: spec }));
    let n = 0;
    for (;;) {
      await new Promise((ok) => setTimeout(ok, 350));
      const st = await api(`api/run?id=${r.id}&since=${n}`);
      st.lines.forEach(logLine);
      n = st.n;
      if (st.done) {
        setStatus(st.ok ? `Done in ${st.seconds} s` : "The command failed: see the log");
        for (const p of st.outputs) doneEl.append(h("span", { class: "muted" }, "Wrote"), h("span", {}, p),
          inApp ? h("button", { class: "btn small", on: { click: () => api("api/open", { path: p }) } }, "Show in folder") : null);
        break;
      }
    }
  } catch (e) {
    logLine(`ERROR: ${e.message}`);
    setStatus("The command failed: see the log");
  } finally {
    running = false;
    renderCmd();
  }
}

// ---------------------------------------------------------------------------
// talking to the back end

let stateTimer = 0, stateSeq = 0;
function changed() {
  clearTimeout(stateTimer);
  stateTimer = setTimeout(refreshState, 90);
}

async function refreshState() {
  const seq = ++stateSeq;
  let st;
  try {
    st = await api("api/state", request());
  } catch (e) {
    st = { error: e.message, command: "pcbfab2step" };
  }
  if (seq !== stateSeq) return;
  ST = st;
  renderInputs();
  renderOutput();
  syncOptions();
  renderCmd();
  if (!S.inputs.length) {
    showEmpty("No board yet", "Drop the fab outputs on the window, or choose a folder on the left.");
    setStatus("");
    return;
  }
  if (st.error) {
    showEmpty("Nothing to show", st.error, true);
    setStatus("");
    return;
  }
  if (S.exact && st.geometry !== exactFor) { S.exact = false; syncMode(); }
  if (!S.layer) {                       // instant: colours and what the settings leave out
    for (const [k, c] of Object.entries(st.colors || {})) viewer.setKindColor(k, c);
    viewer.setHiddenKinds(st.hidden || []);
    chrome.refresh();
  }
  if (stackRows.length) renderLayers();
  if (sceneKey() !== shownKey) requestScene();
}

function sceneKey() {
  return JSON.stringify([ST && ST.geometry, S.layer, S.layer ? false : S.exact, S.layer ? [S.values.invert, S.values.outline, S.values.aperture_offset, S.values.stencil_thickness] : null]);
}

let shownKey = null, sceneBusy = false, sceneAgain = false, shownLayer = undefined;

async function requestScene() {
  if (sceneBusy) { sceneAgain = true; return; }
  if (!ST || ST.error || !S.inputs.length) return;
  sceneBusy = true;
  const key = sceneKey();
  const exact = S.exact && !S.layer;
  const spec = S.layer ? `${S.layer.target}:${S.layer.fmt}` : null;
  if (!viewer.manifest) showEmpty("Reading the fab data", "The first view takes a few seconds; changes after that are quicker.");
  setStatus(exact ? "Building the exact solids" : spec ? `Making ${spec}` : "Updating the view", true);
  try {
    const res = await api("api/scene", request({ spec, exact }));
    const buf = await (await fetch(`api/blob?id=${res.id}&t=${encodeURIComponent(KEY)}`)).arrayBuffer();
    const sameThing = shownLayer !== undefined && JSON.stringify(shownLayer) === JSON.stringify(S.layer);
    viewer.load(res.manifest, buf, sameThing);
    shownLayer = S.layer ? { ...S.layer } : null;
    if (!S.layer && ST) {
      for (const [k, c] of Object.entries(ST.colors || {})) viewer.setKindColor(k, c);
      viewer.setHiddenKinds(ST.hidden || []);
    }
    chrome.refresh();
    showEmpty(null);
    if (res.stack && res.stack.length && (!S.layer || !stackRows.length)) stackRows = res.stack;
    renderLayers();
    shownKey = key;
    const warn = (res.messages || []).filter((m) => /^WARNING/.test(m)).length;
    setStatus(`${res.cached ? "Shown" : exact ? "Exact solids" : "Updated"} in ${res.seconds} s${warn ? `, ${warn} warning${warn > 1 ? "s" : ""} in the log` : ""}`);
    if (res.messages && res.messages.length && !running) { logEl.replaceChildren(); res.messages.forEach(logLine); }
  } catch (e) {
    setStatus("");
    showEmpty("Could not make the view", e.message, true);
    shownKey = key;
  } finally {
    sceneBusy = false;
    if (sceneAgain || sceneKey() !== shownKey) { sceneAgain = false; requestScene(); }
  }
}

// ---------------------------------------------------------------------------
// files dropped on the window

let dropZone = "replace";
document.addEventListener("dragover", (e) => { e.preventDefault(); document.body.classList.add("dragging"); });
document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) document.body.classList.remove("dragging"); });
document.addEventListener("drop", (e) => {
  e.preventDefault();
  document.body.classList.remove("dragging");
  const z = e.target.closest && e.target.closest("[data-drop]");
  dropZone = z ? z.dataset.drop : "replace";
  if (!inApp) setStatus("In the browser a dropped file has no path: paste the path instead.");
});

window.pcbApp = {
  dropped(paths) { setInputs(paths, dropZone === "add" && S.inputs.length > 0); },
};

// ---------------------------------------------------------------------------
// start

(async function start() {
  const init = await api("api/init");
  M = init.model;
  inApp = init.window;
  windows = init.windows;
  cmdEl.querySelector(".ps").textContent = windows ? ">" : "$";
  NAMED = M.color_hex || {};
  side.append(h("div", { class: "brand" }, h("b", {}, "PCBFab2Step"), h("span", {}, init.version)), inputsEl, outputEl, groupsEl);
  buildGroups();
  const st = init.start || {};
  S.inputs = st.inputs || [];
  S.output = st.output || "";
  S.values = st.values || {};
  if (st.export) {
    const [target, fmt] = st.export.split(":");
    S.layer = { target, fmt: (fmt || "svg").split(",")[0] };
  }
  syncMode();
  renderInputs();
  renderOutput();
  renderLayers();
  renderCmd();
  await refreshState();
})();
