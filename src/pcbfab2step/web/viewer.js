// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Albert van Zyl
//
// The PCBFab2Step 3D viewer: draws a packed scene (scene.py) with three.js.
// Used by `pcbfab2step --view` (a page of its own) and by the GUI.

import * as THREE from "three";
import { OrbitControls, RoomEnvironment } from "three";

// How each kind of body looks; the colour comes from the scene (the STEP colour).
const LOOK = {
  copper: { metal: 0.85, rough: 0.34 },
  plating: { metal: 0.85, rough: 0.34 },
  cap: { metal: 0.85, rough: 0.34 },
  finish: { metal: 0.95, rough: 0.22 },
  paste: { metal: 0.6, rough: 0.5 },
  mask: { metal: 0.0, rough: 0.36 },
  silk: { metal: 0.0, rough: 0.75 },
  dielectric: { metal: 0.0, rough: 0.85 },
  core: { metal: 0.0, rough: 0.85 },
  board: { metal: 0.0, rough: 0.85 },
  fill: { metal: 0.0, rough: 0.6 },
  plug: { metal: 0.0, rough: 0.6 },
  component: { metal: 0.1, rough: 0.6 },
};
const UNLIT = new Set(["layer"]);

const VIEWS = {
  iso: [1, -1.15, 0.95],
  top: [0, -1e-4, 1],
  bottom: [0, 1e-4, -1],
  front: [0, -1, 1e-4],
  back: [0, 1, 1e-4],
  left: [-1, 0, 1e-4],
  right: [1, 0, 1e-4],
};

function srgb(c) {
  return new THREE.Color().setRGB(c[0], c[1], c[2], THREE.SRGBColorSpace);
}

// Edges a little darker than the body (lighter on very dark bodies).
function edgeColor(c) {
  const col = srgb(c);
  const hsl = {};
  col.getHSL(hsl);
  return hsl.l > 0.22 ? col.clone().lerp(new THREE.Color(0, 0, 0), 0.55) : col.clone().lerp(new THREE.Color(1, 1, 1), 0.28);
}

export class Viewer {
  /** el: the element to draw in. opts.onPick(part | null), opts.onChange() */
  constructor(el, opts = {}) {
    this.el = el;
    this.opts = opts;
    this.parts = [];          // {rec, group, mesh, lines, visible, viewHidden}
    this.hiddenKinds = new Set();
    this.edges = true;
    this.seeThroughMask = false;
    this.section = null;      // {axis, t, flip}
    this.bounds = new THREE.Box3(new THREE.Vector3(-1, -1, -1), new THREE.Vector3(1, 1, 1));

    const r = (this.renderer = new THREE.WebGLRenderer({ antialias: true, stencil: true, alpha: true }));
    r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    r.localClippingEnabled = true;
    r.toneMapping = THREE.NeutralToneMapping;
    r.toneMappingExposure = 1.0;
    r.setClearColor(0x000000, 0);
    r.domElement.className = "pcb-canvas";
    el.appendChild(r.domElement);

    this.scene = new THREE.Scene();
    const pmrem = new THREE.PMREMGenerator(r);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = 0.85;
    pmrem.dispose();
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x404850, 0.5));

    const cam = (this.camera = new THREE.PerspectiveCamera(28, 1, 0.1, 1000));
    cam.up.set(0, 0, 1);
    const head = new THREE.DirectionalLight(0xffffff, 1.6);   // follows the camera
    head.position.set(0.35, 0.6, 0);
    head.target.position.set(0, 0, -1);
    cam.add(head, head.target);
    this.scene.add(cam);

    this.root = new THREE.Group();
    this.scene.add(this.root);
    this.caps = new THREE.Group();
    this.scene.add(this.caps);

    const c = (this.controls = new OrbitControls(cam, r.domElement));
    c.enableDamping = false;
    c.zoomToCursor = true;
    c.screenSpacePanning = true;
    c.rotateSpeed = 0.9;
    c.addEventListener("change", () => this.render());

    this._down = null;
    r.domElement.addEventListener("pointerdown", (e) => (this._down = [e.clientX, e.clientY]));
    r.domElement.addEventListener("pointerup", (e) => {
      if (this._down && Math.hypot(e.clientX - this._down[0], e.clientY - this._down[1]) < 4 && e.button === 0) {
        const hit = this.pick(e);
        this.select(hit ? hit.part : null);
      }
      this._down = null;
    });
    r.domElement.addEventListener("dblclick", (e) => {
      const hit = this.pick(e);
      if (hit) {
        c.target.copy(hit.point);   // orbit around what was clicked
        c.update();
      }
    });

    this._ro = new ResizeObserver(() => this.resize());
    this._ro.observe(el);
    this.resize();
  }

  // ----------------------------------------------------------------------
  // scene

  /** Show a packed scene: manifest (JSON) and its ArrayBuffer. keepView: keep the camera. */
  load(manifest, buffer, keepView = false) {
    const first = !this.manifest;
    const flatChanged = this.manifest && this.manifest.flat !== manifest.flat;
    this.clear();
    this.manifest = manifest;
    this.hiddenKinds = new Set(manifest.hidden || []);
    for (const rec of manifest.parts) this._addPart(rec, buffer);
    const b = manifest.bounds;
    this.bounds = new THREE.Box3(new THREE.Vector3(b[0], b[1], b[2]), new THREE.Vector3(b[3], b[4], b[5]));
    this.controls.enableRotate = !manifest.flat;
    this._applyVisibility();
    if (this.section) this._buildCaps();
    if (first || flatChanged || !keepView) this.view(manifest.view || "iso");
    else this.render();
  }

  _addPart(rec, buffer) {
    const pos = new THREE.BufferAttribute(new Float32Array(buffer, rec.pos[0], rec.pos[1]), 3);
    const group = new THREE.Group();
    group.name = rec.name;
    const part = { rec, group, mesh: null, lines: null, viewHidden: false };
    if (rec.tri[1]) {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", pos);
      g.setIndex(new THREE.BufferAttribute(new Uint32Array(buffer, rec.tri[0], rec.tri[1]), 1));
      part.mesh = new THREE.Mesh(g, this._material(rec));
      part.mesh.userData.part = part;
      group.add(part.mesh);
    }
    if (rec.edges[1]) {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", pos);
      g.setIndex(new THREE.BufferAttribute(new Uint32Array(buffer, rec.edges[0], rec.edges[1]), 1));
      const light = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
      const col = rec.kind === "outline" ? (light ? new THREE.Color(0.25, 0.28, 0.32) : new THREE.Color(0.86, 0.88, 0.9)) : edgeColor(rec.color);
      part.lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: col, transparent: rec.kind !== "outline", opacity: 0.85 }));
      group.add(part.lines);
    }
    this.parts.push(part);
    this.root.add(group);
  }

  _material(rec) {
    const color = srgb(rec.color);
    if (UNLIT.has(rec.kind)) {
      return new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
    }
    const look = LOOK[rec.kind] || LOOK.component;
    const m = new THREE.MeshStandardMaterial({
      color, metalness: look.metal, roughness: look.rough, flatShading: true,
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
    });
    this._setOpacity(m, rec);
    return m;
  }

  _setOpacity(m, rec) {
    const op = rec.kind === "mask" && this.seeThroughMask ? 0.62 : rec.opacity ?? 1;
    m.transparent = op < 1;
    m.opacity = op;
    m.depthWrite = op >= 1;
    m.needsUpdate = true;
  }

  clear() {
    this._clearCaps();
    for (const p of this.parts) {
      for (const o of [p.mesh, p.lines]) {
        if (!o) continue;
        o.geometry.dispose();
        o.material.dispose();
      }
      this.root.remove(p.group);
    }
    this.parts = [];
    this.selected = null;
  }

  // ----------------------------------------------------------------------
  // what is shown

  _applyVisibility() {
    for (const p of this.parts) p.group.visible = !p.viewHidden && !this.hiddenKinds.has(p.rec.kind);
    for (const p of this.parts) if (p.lines) p.lines.visible = this.edges || p.rec.kind === "outline" || !p.mesh;
    if (this.section) this._buildCaps();
    this.render();
    this.opts.onChange && this.opts.onChange();
  }

  /** Kinds the settings leave out of the model (paste, component). */
  setHiddenKinds(kinds) {
    this.hiddenKinds = new Set(kinds);
    this._applyVisibility();
  }

  /** Hide a body in the view only (the model is not changed). */
  setPartHidden(id, hidden) {
    const p = this.parts.find((q) => q.rec.id === id);
    if (p) p.viewHidden = hidden;
    this._applyVisibility();
  }

  isShown(p) {
    return p.group.visible;
  }

  setEdges(on) {
    this.edges = on;
    this._applyVisibility();
  }

  setSeeThroughMask(on) {
    this.seeThroughMask = on;
    for (const p of this.parts) if (p.mesh && p.rec.kind === "mask") this._setOpacity(p.mesh.material, p.rec);
    if (this.section) this._buildCaps();
    this.render();
  }

  /** New colour for every body of a kind (the GUI's colour options, instantly). */
  setKindColor(kind, rgb) {
    for (const p of this.parts) {
      if (p.rec.kind !== kind) continue;
      p.rec.color = rgb;
      if (p.mesh) p.mesh.material.color.copy(srgb(rgb));
      if (p.lines) p.lines.material.color.copy(edgeColor(rgb));
    }
    if (this.section) this._buildCaps();
    this.render();
  }

  // ----------------------------------------------------------------------
  // camera

  resize() {
    const w = Math.max(1, this.el.clientWidth), h = Math.max(1, this.el.clientHeight);
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.render();
  }

  _visibleBox() {
    const box = new THREE.Box3();
    for (const p of this.parts) if (p.group.visible) box.expandByObject(p.group);
    return box.isEmpty() ? this.bounds.clone() : box;
  }

  /** Look from a named direction (iso, top, bottom, front, back, left, right) at the whole board. */
  view(name) {
    const d = new THREE.Vector3(...(VIEWS[name] || VIEWS.iso)).normalize();
    this._frame(this._visibleBox(), d);
  }

  /** Fit the board in the window, looking from where the camera is now. */
  fit() {
    const d = this.camera.position.clone().sub(this.controls.target).normalize();
    this._frame(this._visibleBox(), d);
  }

  _frame(box, dir) {
    // the distance at which every corner of the box is in the window, looking along -dir
    const center = box.getCenter(new THREE.Vector3());
    const cam = this.camera;
    const look = new THREE.Matrix4().lookAt(dir, new THREE.Vector3(), cam.up);
    const inv = look.clone().invert();
    const tv = Math.tan(THREE.MathUtils.degToRad(cam.fov) / 2), th = tv * cam.aspect;
    let dist = 1e-3;
    const v = new THREE.Vector3();
    for (let i = 0; i < 8; i++) {
      v.set(i & 1 ? box.max.x : box.min.x, i & 2 ? box.max.y : box.min.y, i & 4 ? box.max.z : box.min.z);
      v.sub(center).applyMatrix4(inv);             // camera axes: x right, y up, z towards the camera
      dist = Math.max(dist, v.z + Math.abs(v.x) / th, v.z + Math.abs(v.y) / tv);
    }
    dist *= 1.12;
    const r = box.getBoundingSphere(new THREE.Sphere()).radius || 1;
    this.controls.minDistance = r * 1e-4;
    this.controls.maxDistance = r * 60;
    this.controls.target.copy(center);
    cam.position.copy(center).addScaledVector(dir, dist);
    cam.lookAt(center);
    this.controls.update();
    this.render();
  }

  _nearFar() {
    const sphere = this.bounds.getBoundingSphere(new THREE.Sphere());
    const far = this.camera.position.distanceTo(sphere.center) + sphere.radius * 2 + 1;
    const near = Math.max(this.camera.position.distanceTo(this.controls.target) * 0.02, far * 1e-6, 1e-4);
    if (Math.abs(this.camera.near - near) / near > 0.01 || Math.abs(this.camera.far - far) / far > 0.01) {
      this.camera.near = near;
      this.camera.far = far;
      this.camera.updateProjectionMatrix();
    }
  }

  // ----------------------------------------------------------------------
  // section view: a clipping plane, the cut faces filled (stencil caps)

  /** axis: "x", "y", "z" or null; t: 0..1 across the board; flip: keep the other side. */
  setSection(axis, t = 0.5, flip = false) {
    this.section = axis ? { axis, t, flip } : null;
    const plane = this._plane();
    for (const p of this.parts) {
      for (const o of [p.mesh, p.lines]) {
        if (!o) continue;
        o.material.clippingPlanes = plane ? [plane] : null;
        o.material.needsUpdate = true;
      }
      // a cut body shows its inside where no cap covers it (an open mesh)
      if (p.mesh && !UNLIT.has(p.rec.kind)) p.mesh.material.side = plane ? THREE.DoubleSide : THREE.FrontSide;
    }
    if (plane) this._buildCaps();
    else this._clearCaps();
    this.render();
  }

  _plane() {
    const s = this.section;
    if (!s) return null;
    const i = { x: 0, y: 1, z: 2 }[s.axis];
    const lo = this.bounds.min.getComponent(i), hi = this.bounds.max.getComponent(i);
    const at = lo + (hi - lo) * s.t;
    const n = new THREE.Vector3();
    n.setComponent(i, s.flip ? 1 : -1);
    if (!this._planeObj) this._planeObj = new THREE.Plane();
    return this._planeObj.set(n, s.flip ? -at : at);
  }

  _clearCaps() {
    for (const o of [...this.caps.children]) {
      o.traverse((x) => {
        if (x.material) x.material.dispose();
        if (x.isMesh && x.userData.ownGeometry) x.geometry.dispose();
      });
      this.caps.remove(o);
    }
  }

  _buildCaps() {
    this._clearCaps();
    const plane = this._plane();
    if (!plane) return;
    const size = this.bounds.getSize(new THREE.Vector3()).length() * 4 + 10;
    const capGeo = new THREE.PlaneGeometry(size, size);
    let order = 1;
    for (const p of this.parts) {
      if (!p.mesh || !p.group.visible || UNLIT.has(p.rec.kind)) continue;
      const base = { depthWrite: false, depthTest: false, colorWrite: false, stencilWrite: true, stencilFunc: THREE.AlwaysStencilFunc, clippingPlanes: [plane] };
      const back = new THREE.MeshBasicMaterial({ ...base, side: THREE.BackSide, stencilFail: THREE.IncrementWrapStencilOp, stencilZFail: THREE.IncrementWrapStencilOp, stencilZPass: THREE.IncrementWrapStencilOp });
      const front = new THREE.MeshBasicMaterial({ ...base, side: THREE.FrontSide, stencilFail: THREE.DecrementWrapStencilOp, stencilZFail: THREE.DecrementWrapStencilOp, stencilZPass: THREE.DecrementWrapStencilOp });
      const look = LOOK[p.rec.kind] || LOOK.component;
      const capMat = new THREE.MeshStandardMaterial({
        color: srgb(p.rec.color).multiplyScalar(0.92), metalness: look.metal * 0.6, roughness: Math.min(1, look.rough + 0.2),
        stencilWrite: true, stencilRef: 0, stencilFunc: THREE.NotEqualStencilFunc,
        stencilFail: THREE.ReplaceStencilOp, stencilZFail: THREE.ReplaceStencilOp, stencilZPass: THREE.ReplaceStencilOp,
      });
      const g = new THREE.Group();
      const m0 = new THREE.Mesh(p.mesh.geometry, back);
      const m1 = new THREE.Mesh(p.mesh.geometry, front);
      const cap = new THREE.Mesh(capGeo, capMat);
      m0.renderOrder = m1.renderOrder = order;
      cap.renderOrder = order + 0.5;
      cap.userData.cap = true;
      cap.onAfterRender = (renderer) => renderer.clearStencil();
      order += 1;
      g.add(m0, m1, cap);
      this.caps.add(g);
    }
    if (this.caps.children.length) this.caps.children[0].children[2].userData.ownGeometry = true;
    for (const p of this.parts) p.group.renderOrder = order + 1;
  }

  _placeCaps() {
    const plane = this._plane();
    if (!plane) return;
    const at = plane.coplanarPoint(new THREE.Vector3());
    for (const g of this.caps.children) {
      const cap = g.children[2];
      cap.position.copy(at);
      cap.lookAt(at.x - plane.normal.x, at.y - plane.normal.y, at.z - plane.normal.z);
    }
  }

  // ----------------------------------------------------------------------
  // picking

  pick(event) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, this.camera);
    const meshes = this.parts.filter((p) => p.mesh && p.group.visible).map((p) => p.mesh);
    const plane = this._plane();
    for (const h of ray.intersectObjects(meshes, false)) {
      if (plane && plane.distanceToPoint(h.point) < 0) continue;
      return { part: h.object.userData.part, point: h.point };
    }
    return null;
  }

  select(part) {
    if (this.selected && this.selected.mesh) this.selected.mesh.material.emissive.setRGB(0, 0, 0);
    this.selected = part;
    if (part && part.mesh && part.mesh.material.emissive) part.mesh.material.emissive.setRGB(0.32, 0.24, 0.02);
    this.render();
    this.opts.onPick && this.opts.onPick(part);
  }

  // ----------------------------------------------------------------------

  render() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      this._nearFar();
      this._placeCaps();
      this.renderer.render(this.scene, this.camera);
    });
  }

  /** The current view as a PNG data URL. */
  snapshot() {
    this._nearFar();
    this._placeCaps();
    this.renderer.render(this.scene, this.camera);
    return this.renderer.domElement.toDataURL("image/png");
  }

  dispose() {
    this.clear();
    this._ro.disconnect();
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

/** Fetch a scene the app serves: base.json (the manifest) and base.bin. */
export async function fetchScene(base, key = "") {
  const q = key ? `?t=${encodeURIComponent(key)}` : "";
  const [m, b] = await Promise.all([fetch(`${base}.json${q}`), fetch(`${base}.bin${q}`)]);
  if (!m.ok) throw new Error((await m.json().catch(() => ({}))).error || m.statusText);
  if (!b.ok) throw new Error(b.statusText);
  return { manifest: await m.json(), buffer: await b.arrayBuffer() };
}

/** Decode a scene embedded in a page (base64), quickly. */
export async function decodeScene(embedded) {
  const res = await fetch("data:application/octet-stream;base64," + embedded.data);
  return { manifest: embedded.manifest, buffer: await res.arrayBuffer() };
}
