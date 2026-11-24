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

  const views = h("div", { class: "seg", role: "group", "aria-label": "View" },
    ...[["iso", "Iso"], ["top", "Top"], ["bottom", "Bottom"], ["front", "Front"], ["right", "Side"]].map(([v, label]) =>
      h("button", { class: "tb", title: `${label} view`, on: { click: () => viewer.view(v) } }, label)),
    h("button", { class: "tb", title: "Fit the board in the window (F)", on: { click: () => viewer.fit() } }, "Fit"));

  const axes = ["x", "y", "z"].map((a) => h("button", { class: "tb", "aria-pressed": "false", title: `Section across ${a.toUpperCase()}` }, a.toUpperCase()));
  const slider = h("input", { type: "range", min: "0", max: "1000", value: "500", disabled: true, "aria-label": "Section position" });
  const flip = h("button", { class: "tb", disabled: true, title: "Keep the other side of the cut" }, "Flip");
  let axis = null, flipped = false;
  const applySection = () => {
    axes.forEach((b, i) => press(b, axis === "xyz"[i]));
    slider.disabled = flip.disabled = !axis;
    viewer.setSection(axis, slider.value / 1000, flipped);
  };
  axes.forEach((b, i) => b.addEventListener("click", () => {
    axis = axis === "xyz"[i] ? null : "xyz"[i];
    applySection();
  }));
  slider.addEventListener("input", applySection);
  flip.addEventListener("click", () => { flipped = !flipped; press(flip, flipped); applySection(); });
  const section = h("div", { class: "seg", role: "group", "aria-label": "Section" }, h("label", {}, "Section "), ...axes, slider, flip);

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
  viewport.append(picked, bodies, toolbar);

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
