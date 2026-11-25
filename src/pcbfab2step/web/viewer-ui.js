// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Albert van Zyl
//
// The viewer's chrome: caption, tool bar, bodies drawer, picked body.
// Display only: nothing here changes what is exported.

const EYE = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z" opacity=".45"/><path d="M2.5 13.5l11-11"/></svg>';

const GROUPS = [
  ["Board", ["core", "dielectric", "copper", "mask", "silk", "finish", "paste"]],
  ["Holes and vias", ["plating", "fill", "plug", "cap"]],
  ["Components", ["component"]],
  ["Export", ["layer", "board", "outline"]],
];
const KIND_NAMES = {
  core: "core", dielectric: "dielectric", copper: "copper", mask: "solder mask", silk: "silkscreen",
  finish: "surface finish", paste: "solder paste", plating: "hole plating", fill: "via fill", plug: "via plug",
  cap: "via cap", component: "component", layer: "layer", board: "board", outline: "outline",
};

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "html") el.innerHTML = v;
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) el.append(k);
  return el;
}

function css(c) {
  return `rgb(${c.map((v) => Math.round(v * 255)).join(",")})`;
}

export function describe(manifest) {
  const n = (manifest.triangles || 0).toLocaleString("en-US");
  if (manifest.mode === "layer") return `Layer export as it will be written, ${n} triangles`;
  if (manifest.mode === "exact") return `The STEP model's own solids, ${n} triangles`;
  return `Preview of the STEP model, ${n} triangles`;
}

/** Caption, tool bar and bodies drawer over a viewer. opts.caption: false to leave the caption out. */
export function mountChrome(viewport, viewer, opts = {}) {
  const caption = h("div", { class: "caption" }, h("h1"), h("p"));
  const picked = h("div", { class: "picked", hidden: true });
  const press = (b, on) => b.setAttribute("aria-pressed", on ? "true" : "false");

  // popovers above the tool bar
  const pops = [];
  function popover(btn, panel) {
    panel.classList.add("pop");
    panel.hidden = true;
    pops.push([btn, panel]);
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = panel.hidden;
      for (const [b, p] of pops) { p.hidden = true; if (b !== btn) b.classList.remove("open"); }
      panel.hidden = !open;
      btn.classList.toggle("open", open);
      if (open) place(btn, panel);
    });
    panel.addEventListener("click", (e) => e.stopPropagation());
  }
  function place(btn, panel) {
    const vr = viewport.getBoundingClientRect(), br = btn.getBoundingClientRect();
    panel.style.left = `${Math.max(8, Math.min(br.left - vr.left, vr.width - panel.offsetWidth - 8))}px`;
  }
  document.addEventListener("click", () => { for (const [b, p] of pops) { p.hidden = true; b.classList.remove("open"); } });

  const ISOS = [["Front right", "iso"], ["Front left", "iso-front-left"], ["Back right", "iso-back-right"], ["Back left", "iso-back-left"]];
  const isoBtn = h("button", { class: "tb", title: "Isometric views: from the corners above and below" }, "Iso ▾");
  const isoPanel = h("div", { class: "isopanel", role: "menu", "aria-label": "Isometric views" },
    h("span", { class: "lbl" }, "From above"), ...ISOS.map(([label, v]) => h("button", { class: "tb", role: "menuitem", on: { click: () => { viewer.view(v); isoPanel.hidden = true; isoBtn.classList.remove("open"); } } }, label)),
    h("span", { class: "lbl" }, "From below"), ...ISOS.map(([label, v]) => h("button", { class: "tb", role: "menuitem", on: { click: () => { viewer.view(v === "iso" ? "iso-below-front-right" : v.replace("iso-", "iso-below-")); isoPanel.hidden = true; isoBtn.classList.remove("open"); } } }, label)));
  popover(isoBtn, isoPanel);

  const views = h("div", { class: "seg", role: "group", "aria-label": "View" }, isoBtn,
    ...[["top", "Top"], ["bottom", "Bottom"], ["front", "Front"], ["right", "Side"]].map(([v, label]) =>
      h("button", { class: "tb", title: `${label} view`, on: { click: () => viewer.view(v) } }, label)),
    h("button", { class: "tb", title: "Fit the board in the window (F)", on: { click: () => viewer.fit() } }, "Fit"));

  // section: axis, position, turn about the board's normal, tilt out of its plane
  const sec = { axis: null, t: 0.5, flip: false, turn: 0, tilt: 0, last: "x" };
  const secBtn = h("button", { class: "tb", "aria-pressed": "false", title: "Cut the board open" }, "Section ▾");
  const axes = ["x", "y", "z"].map((a) => h("button", { class: "tb", "aria-pressed": "false", title: `Cut across ${a.toUpperCase()}` }, a.toUpperCase()));
  const flip = h("button", { class: "tb", "aria-pressed": "false", title: "Keep the other side of the cut" }, "Flip");
  const off = h("button", { class: "tb", title: "No section" }, "Off");
  const slider = (min, max, val, label) => h("input", { type: "range", min: String(min), max: String(max), value: String(val), "aria-label": label });
  const pos = slider(0, 1000, 500, "Section position");
  const turn = slider(-90, 90, 0, "Turn the cut");
  const tilt = slider(-80, 80, 0, "Tilt the cut");
  const turnVal = h("span", { class: "val" }, "0°"), tiltVal = h("span", { class: "val" }, "0°");
  const applySection = () => {
    sec.t = pos.value / 1000;
    sec.turn = +turn.value;
    sec.tilt = +tilt.value;
    turnVal.textContent = `${sec.turn}°`;
    tiltVal.textContent = `${sec.tilt}°`;
    axes.forEach((b, i) => press(b, sec.axis === "xyz"[i]));
    press(secBtn, !!sec.axis);
    press(flip, sec.flip);
    viewer.setSection(sec.axis, sec.t, sec.flip, sec.turn, sec.tilt);
  };
  axes.forEach((b, i) => b.addEventListener("click", () => { sec.axis = sec.last = "xyz"[i]; applySection(); }));
  flip.addEventListener("click", () => { sec.flip = !sec.flip; if (!sec.axis) sec.axis = sec.last; applySection(); });
  off.addEventListener("click", () => { sec.axis = null; applySection(); });
  for (const r of [pos, turn, tilt]) r.addEventListener("input", () => { if (!sec.axis) sec.axis = sec.last; applySection(); });
  for (const [r, v] of [[turn, turnVal], [tilt, tiltVal]]) r.addEventListener("dblclick", () => { r.value = "0"; applySection(); });
  const secPanel = h("div", { class: "secpanel", "aria-label": "Section" },
    h("div", { class: "row" }, h("span", { class: "lbl" }, "Across"), h("div", { class: "seg" }, ...axes), h("span", { class: "grow" }), flip, off),
    h("label", { class: "row" }, h("span", { class: "lbl" }, "Position"), pos, h("span", { class: "val" })),
    h("label", { class: "row", title: "Turn the cut about the board's normal (double-click: 0)" }, h("span", { class: "lbl" }, "Turn"), turn, turnVal),
    h("label", { class: "row", title: "Tilt the cut out of its plane: an oblique cut (double-click: 0)" }, h("span", { class: "lbl" }, "Tilt"), tilt, tiltVal));
  popover(secBtn, secPanel);
  secBtn.addEventListener("click", () => { if (!sec.axis && !secPanel.hidden) { sec.axis = sec.last; applySection(); } });
  const section = h("div", { class: "seg", role: "group", "aria-label": "Section" }, secBtn);

  const edges = h("button", { class: "tb", "aria-pressed": "true", title: "Draw the edges (E)" }, "Edges");
  edges.addEventListener("click", () => { viewer.setEdges(!viewer.edges); press(edges, viewer.edges); });
  const mask = h("button", { class: "tb", "aria-pressed": "false", title: "See the copper through the solder mask (view only)" }, "See-through mask");
  mask.addEventListener("click", () => { viewer.setSeeThroughMask(!viewer.seeThroughMask); press(mask, viewer.seeThroughMask); });
  const bodiesBtn = h("button", { class: "tb", "aria-pressed": "false", title: "List the bodies" }, "Bodies");

  const toolbar = h("div", { class: "toolbar" }, views, h("div", { class: "sep" }), section, h("div", { class: "sep" }), edges, mask, bodiesBtn);

  const list = h("ul");
  const bodies = h("aside", { class: "bodies", hidden: true, "aria-label": "Bodies" },
    h("header", {}, h("h2", {}, "Bodies"), h("button", { class: "tb", title: "Close", on: { click: () => toggleBodies(false) } }, "Close")), list);
  const toggleBodies = (on) => { bodies.hidden = !on; press(bodiesBtn, on); };
  bodiesBtn.addEventListener("click", () => toggleBodies(bodies.hidden));

  if (opts.caption !== false) viewport.append(caption);
  viewport.append(picked, bodies, toolbar, isoPanel, secPanel);

  const rows = new Map();
  function fill() {
    const m = viewer.manifest;
    if (!m) return;
    caption.querySelector("h1").textContent = opts.title || m.title;
    caption.querySelector("p").textContent = opts.subtitle || describe(m);
    mask.hidden = !viewer.parts.some((p) => p.rec.kind === "mask");
    list.replaceChildren();
    rows.clear();
    for (const [label, kinds] of GROUPS) {
      const ps = viewer.parts.filter((p) => kinds.includes(p.rec.kind));
      if (!ps.length) continue;
      list.append(h("li", { class: "group", role: "presentation" }, label));
      for (const p of ps) {
        const eye = h("button", { class: "eye", title: "Show or hide (view only)" });
        const li = h("li", { title: `${p.rec.name} (${KIND_NAMES[p.rec.kind] || p.rec.kind})` },
          h("span", { class: "sw", style: `background:${css(p.rec.color)}` }), h("span", { class: "nm" }, p.rec.name), eye);
        eye.addEventListener("click", (e) => {
          e.stopPropagation();
          const kindOff = viewer.hiddenKinds.has(p.rec.kind);
          if (kindOff) {                      // left out by the settings: show the whole kind to look at it
            const ks = new Set(viewer.hiddenKinds);
            ks.delete(p.rec.kind);
            viewer.setHiddenKinds([...ks]);
          } else if (viewer._hiddenByName(p)) {   // left out by name: show it to look at it
            viewer.setHiddenNames([...viewer.hideNames].filter((n) => !(p.rec.names || []).includes(n)));
          } else viewer.setPartHidden(p.rec.id, !p.viewHidden);
          sync();
        });
        li.addEventListener("click", () => viewer.select(p));
        rows.set(p, { li, eye });
        list.append(li);
      }
    }
    sync();
  }
  function sync() {
    for (const [p, { li, eye }] of rows) {
      const on = viewer.isShown(p);
      li.classList.toggle("off", !on);
      li.classList.toggle("sel", viewer.selected === p);
      eye.innerHTML = on ? EYE : EYE_OFF;
      eye.setAttribute("aria-label", (on ? "Hide " : "Show ") + p.rec.name);
    }
  }

  const prevPick = viewer.opts.onPick;
  viewer.opts.onPick = (p) => {
    picked.hidden = !p;
    if (p) {
      picked.replaceChildren(h("b", {}, p.rec.name), h("span", {}, [KIND_NAMES[p.rec.kind] || p.rec.kind, p.rec.layer && p.rec.layer !== p.rec.name ? p.rec.layer : ""].filter(Boolean).join(", ")));
    }
    sync();
    prevPick && prevPick(p);
  };

  window.addEventListener("keydown", (e) => {
    if (e.target.closest && e.target.closest("input, select, textarea")) return;
    if (e.key === "f" || e.key === "F") viewer.fit();
    else if (e.key === "e" || e.key === "E") edges.click();
    else if (e.key === "Home") viewer.view("iso");
  });

  return { refresh: fill, sync, toolbar, caption, bodies: toggleBodies };
}
