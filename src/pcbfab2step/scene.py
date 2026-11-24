# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Meshes for the viewer.

Fast: the builder runs with MeshGeo in place of the B-rep: every slab is
triangulated straight from the 2D region the STEP solid would be made of
(constrained Delaunay caps, walls along the rings), holes and vias as
cylinders, countersinks as stepped discs. Exact: the B-rep solids are built
and tessellated (what the STEP contains).

A Scene is a list of parts (one per body of the STEP model), each with its
own triangles and edges, packed into one binary buffer and a JSON manifest
for the three.js viewer.
"""

from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import shapely
from shapely.geometry.polygon import orient

from .geometry import Tess, iter_polys

SCENE_VERSION = 1
EDGE_TURN = math.radians(35.0)      # a vertical edge is drawn where the outline turns more than this


@dataclass
class Mesh:
    pos: np.ndarray                 # (n, 3) float
    tri: np.ndarray                 # (m, 3) int, counter-clockwise seen from outside
    edges: np.ndarray               # (k, 2) int, line segments to draw

    @staticmethod
    def join(meshes: List["Mesh"]) -> "Mesh":
        meshes = [m for m in meshes if m is not None and len(m.tri)]
        if not meshes:
            return _empty()
        pos, tri, edg, off = [], [], [], 0
        for m in meshes:
            pos.append(m.pos)
            tri.append(m.tri + off)
            edg.append(m.edges + off)
            off += len(m.pos)
        return Mesh(np.vstack(pos), np.vstack(tri), np.vstack(edg))


# --------------------------------------------------------------------------
# meshes from 2D regions
# --------------------------------------------------------------------------

def _cap_triangles(poly, V: np.ndarray) -> Optional[np.ndarray]:
    """Triangles of a polygon as indices into its ring vertices V
    (counter-clockwise), or None. The constrained Delaunay triangulation adds
    no points: its corners are V's own coordinates, found by a sorted search."""
    tris = shapely.constrained_delaunay_triangles(poly)
    if tris.is_empty:
        return None
    T = shapely.get_coordinates(tris).reshape(-1, 4, 2)[:, :3, :]
    vc = V[:, 0] + 1j * V[:, 1]
    order = np.argsort(vc, kind="stable")
    sv = vc[order]
    tc = (T[..., 0] + 1j * T[..., 1]).reshape(-1)
    at = np.clip(np.searchsorted(sv, tc), 0, len(sv) - 1)
    ok = sv[at] == tc
    idx = order[at].reshape(-1, 3)
    idx = idx[ok.reshape(-1, 3).all(axis=1)]
    if not len(idx):
        return None
    a, b, c = V[idx[:, 0]], V[idx[:, 1]], V[idx[:, 2]]
    cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    cw = cross < 0
    idx[cw] = idx[cw][:, [0, 2, 1]]
    return idx[np.abs(cross) > 1e-14]


def _rings(poly) -> List[np.ndarray]:
    out = []
    for r in [poly.exterior, *poly.interiors]:
        c = np.asarray(r.coords)[:-1, :2]
        if len(c) >= 3:
            out.append(c)
    return out


_PLAN: dict = {}            # polygon WKB -> (V, caps, rings), most recently used last
_PLAN_BUDGET = 4_000_000    # ring vertices kept: the stack's layers and the next build share their regions
_plan_size = [0]


def _make_plan(poly):
    p = orient(poly, 1.0)                           # exterior CCW, holes CW: walls face out
    rings = _rings(p)
    V = np.vstack(rings) if rings else None
    caps = _cap_triangles(p, V) if rings else None
    return V, caps, rings


def _keep(key, got):
    _PLAN.pop(key, None)
    _PLAN[key] = got
    _plan_size[0] += 0 if got[0] is None else len(got[0])
    while len(_PLAN) > 1 and _plan_size[0] > _PLAN_BUDGET:
        old = _PLAN.pop(next(iter(_PLAN)))
        _plan_size[0] -= 0 if old[0] is None else len(old[0])


def _plans(polys) -> dict:
    """Plans of many polygons by WKB. A geometry change in the viewer
    rebuilds the slabs, but most regions come back unchanged; the new ones
    are triangulated in parallel (GEOS releases the GIL)."""
    out, todo = {}, {}
    for poly in polys:
        key = poly.wkb
        if key in out or key in todo:
            continue
        got = _PLAN.pop(key, None)
        if got is None:
            todo[key] = poly
        else:
            _PLAN[key] = got                        # most recently used last
            out[key] = got
    if todo:
        keys = sorted(todo, key=lambda k: -len(k))  # the largest first: the pool stays busy
        workers = min(len(keys), os.cpu_count() or 1, 16)
        if workers > 1:
            with ThreadPoolExecutor(workers) as ex:
                made = list(ex.map(lambda k: _make_plan(todo[k]), keys))
        else:
            made = [_make_plan(todo[k]) for k in keys]
        for k, got in zip(keys, made):
            out[k] = got
            _keep(k, got)
    return out


def _plan(poly):
    return _plans([poly])[poly.wkb]


def prism_mesh(poly, z0: float, z1: float, plan=None) -> Optional[Mesh]:
    """A prism of a polygon (with holes) between z0 and z1."""
    if poly.is_empty or poly.area < 1e-12 or z1 - z0 <= 1e-9:
        return None
    V, caps, rings = plan if plan is not None else _plan(poly)
    if caps is None:
        return None
    n = len(V)
    nxt = np.empty(n, dtype=np.int64)
    start, turns = 0, []
    for r in rings:
        k = len(r)
        nxt[start:start + k] = np.roll(np.arange(start, start + k), -1)
        prv = np.roll(r, 1, axis=0)
        nx = np.roll(r, -1, axis=0)
        v1, v2 = r - prv, nx - r
        turns.append(np.abs(np.arctan2(v1[:, 0] * v2[:, 1] - v1[:, 1] * v2[:, 0], (v1 * v2).sum(axis=1))))
        start += k
    a = np.arange(n)
    b = nxt
    walls = np.vstack([np.column_stack([a, b, b + n]), np.column_stack([a, b + n, a + n])])
    tri = np.vstack([caps[:, ::-1], caps + n, walls])     # bottom faces down, top faces up
    sharp = np.nonzero(np.concatenate(turns) > EDGE_TURN)[0]
    edges = np.vstack([np.column_stack([a, b]), np.column_stack([a + n, b + n]), np.column_stack([sharp, sharp + n])])
    pos = np.empty((2 * n, 3))
    pos[:n, :2], pos[n:, :2] = V, V
    pos[:n, 2], pos[n:, 2] = z0, z1
    return Mesh(pos, tri, edges)


def flat_mesh(geom, z: float = 0.0) -> Optional[Mesh]:
    """A 2D region as a flat, upward facing mesh with its outlines (layer view)."""
    polys = [p for p in iter_polys(geom) if p.area >= 1e-12]
    plans = _plans(polys)
    parts = []
    for poly in polys:
        V, caps, rings = plans[poly.wkb]
        if caps is None:
            continue
        n = len(V)
        nxt = np.empty(n, dtype=np.int64)
        start = 0
        for r in rings:
            k = len(r)
            nxt[start:start + k] = np.roll(np.arange(start, start + k), -1)
            start += k
        pos = np.column_stack([V, np.full(n, z)])
        parts.append(Mesh(pos, caps, np.column_stack([np.arange(n), nxt])))
    return Mesh.join(parts) if parts else None


def line_mesh(geom, z: float = 0.0) -> Optional[Mesh]:
    """Only the rings of a region as lines (the board outline in a 2D view)."""
    pos, edges, off = [], [], 0
    for poly in iter_polys(geom):
        for r in _rings(poly):
            k = len(r)
            pos.append(np.column_stack([r, np.full(k, z)]))
            idx = np.arange(off, off + k)
            edges.append(np.column_stack([idx, np.roll(idx, -1)]))
            off += k
    if not pos:
        return None
    return Mesh(np.vstack(pos), np.zeros((0, 3), np.int64), np.vstack(edges))


def disc_mesh(x: float, y: float, r_out: float, r_in: float, z0: float, z1: float) -> Optional[Mesh]:
    """A cylinder (r_in = 0) or a tube between z0 and z1."""
    if r_out <= 0 or z1 - z0 <= 1e-9 or r_in >= r_out:
        return None
    k = max(12, min(Tess.nseg(r_out), 96))
    t = np.arange(k) * (2 * math.pi / k)
    c, s = np.cos(t), np.sin(t)
    i = np.arange(k)
    j = (i + 1) % k
    if r_in <= 1e-9:
        ring = np.column_stack([x + r_out * c, y + r_out * s])
        pos = np.empty((2 * k + 2, 3))
        pos[:k, :2], pos[k:2 * k, :2] = ring, ring
        pos[:k, 2], pos[k:2 * k, 2] = z0, z1
        pos[2 * k] = (x, y, z0)
        pos[2 * k + 1] = (x, y, z1)
        cb, ct = 2 * k, 2 * k + 1
        tri = np.vstack([np.column_stack([np.full(k, cb), j, i]), np.column_stack([np.full(k, ct), i + k, j + k]),
                         np.column_stack([i, j, j + k]), np.column_stack([i, j + k, i + k])])
        edges = np.vstack([np.column_stack([i, j]), np.column_stack([i + k, j + k])])
        return Mesh(pos, tri, edges)
    outer = np.column_stack([x + r_out * c, y + r_out * s])
    inner = np.column_stack([x + r_in * c, y + r_in * s])
    pos = np.empty((4 * k, 3))
    pos[:k, :2], pos[k:2 * k, :2] = outer, outer
    pos[2 * k:3 * k, :2], pos[3 * k:, :2] = inner, inner
    pos[:k, 2], pos[k:2 * k, 2], pos[2 * k:3 * k, 2], pos[3 * k:, 2] = z0, z1, z0, z1
    ob, ot, ib, it = 0, k, 2 * k, 3 * k
    tri = np.vstack([
        np.column_stack([ob + i, ob + j, ot + j]), np.column_stack([ob + i, ot + j, ot + i]),       # outer wall
        np.column_stack([ib + j, ib + i, it + i]), np.column_stack([ib + j, it + i, it + j]),       # inner wall
        np.column_stack([ot + i, ot + j, it + j]), np.column_stack([ot + i, it + j, it + i]),       # top
        np.column_stack([ob + j, ob + i, ib + i]), np.column_stack([ob + j, ib + i, ib + j]),       # bottom
    ])
    edges = np.vstack([np.column_stack([ob + i, ob + j]), np.column_stack([ot + i, ot + j]),
                       np.column_stack([ib + i, ib + j]), np.column_stack([it + i, it + j])])
    return Mesh(pos, tri, edges)


@dataclass
class Prism:
    """A prism waiting for its cap triangles (made for all at once, see realize)."""
    poly: object
    z0: float
    z1: float


class MeshGeo:
    """The builder's geometry backend for the viewer (see Model.geo)."""

    @staticmethod
    def prisms(geom, z0: float, z1: float) -> List[Prism]:
        if geom is None or geom.is_empty or z1 - z0 <= 1e-9:
            return []
        return [Prism(p, z0, z1) for p in iter_polys(geom) if p.area >= 1e-12]

    @staticmethod
    def disc_prism(x, y, r_out, r_in, z0, z1):
        return disc_mesh(x, y, r_out, r_in, z0, z1)


def realize(solid_lists: List[list]) -> List[Mesh]:
    """One mesh per list of MeshGeo solids (Prism or Mesh)."""
    plans = _plans([s.poly for ss in solid_lists for s in ss if isinstance(s, Prism)])
    out = []
    for ss in solid_lists:
        ms = []
        for s in ss:
            if isinstance(s, Prism):
                s = prism_mesh(s.poly, s.z0, s.z1, plans[s.poly.wkb])
            if s is not None:
                ms.append(s)
        out.append(Mesh.join(ms))
    return out


# --------------------------------------------------------------------------
# meshes from B-rep solids (exact view, ECAD components)
# --------------------------------------------------------------------------

FEATURE_ANGLE = math.radians(30.0)     # an exact mesh draws the edges where the faces meet sharper than this
_STL = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])


def _empty() -> Mesh:
    return Mesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 2), np.int64))


def _stl_mesh(path: str) -> Mesh:
    """A binary STL as an indexed mesh (equal corners joined) with its sharp edges."""
    raw = open(path, "rb").read()
    if len(raw) < 84:
        return _empty()
    n = min(int.from_bytes(raw[80:84], "little"), (len(raw) - 84) // _STL.itemsize)
    v = np.frombuffer(raw, _STL, count=n, offset=84)["v"].reshape(-1, 3)
    if not len(v):
        return _empty()
    key = np.ascontiguousarray(v).view(np.dtype((np.void, 12))).ravel()
    _, first, inv = np.unique(key, return_index=True, return_inverse=True)
    pos = v[first].astype(np.float64)
    tri = inv.reshape(-1, 3).astype(np.int64)
    return Mesh(pos, tri, feature_edges(pos, tri))


def feature_edges(pos: np.ndarray, tri: np.ndarray, angle: float = FEATURE_ANGLE) -> np.ndarray:
    """Edges on one triangle only, or between triangles that meet sharper than angle."""
    if not len(tri):
        return np.zeros((0, 2), np.int64)
    a, b, c = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
    nrm = np.cross(b - a, c - a)
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-30)
    e = np.sort(np.vstack([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]]), axis=1)
    f = np.tile(np.arange(len(tri)), 3)
    ek = e[:, 0] * (len(pos) + 1) + e[:, 1]
    o = np.argsort(ek, kind="stable")
    ek, e, f = ek[o], e[o], f[o]
    first = np.nonzero(np.r_[True, ek[1:] != ek[:-1]])[0]
    cnt = np.diff(np.r_[first, len(ek)])
    sharp = cnt != 2
    two = np.nonzero(cnt == 2)[0]
    sharp[two] = (nrm[f[first[two]]] * nrm[f[first[two] + 1]]).sum(axis=1) < math.cos(angle)
    return e[first[sharp]]


def shape_meshes(groups: List[list], deflection: float = 0.01, angle: float = 0.5) -> List[Mesh]:
    """Tessellate groups of OCC solids (all at once, BRepMesh in parallel),
    one mesh per group. The triangles come out through binary STL, which is
    much faster than reading them node by node."""
    import tempfile
    from .brep import OCC
    OCC.load()
    from OCP.BRep import BRep_Builder
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.StlAPI import StlAPI_Writer
    from OCP.TopoDS import TopoDS_Compound

    def compound(shapes):
        b = BRep_Builder()
        comp = TopoDS_Compound()
        b.MakeCompound(comp)
        for sh in shapes:
            b.Add(comp, sh)
        return comp
    if not any(groups):
        return [_empty() for _ in groups]
    BRepMesh_IncrementalMesh(compound([sh for g in groups for sh in g]), deflection, False, angle, True)
    out = []
    with tempfile.TemporaryDirectory(prefix="pcbfab2step-") as d:
        for i, g in enumerate(groups):
            path = os.path.join(d, f"{i}.stl")
            w = StlAPI_Writer()
            w.ASCIIMode = False
            out.append(_stl_mesh(path) if g and w.Write(compound(g), path) else _empty())
    return out


def shape_mesh(shapes, deflection: float = 0.01, angle: float = 0.5) -> Mesh:
    return shape_meshes([list(shapes)], deflection, angle)[0]


# --------------------------------------------------------------------------
# scene
# --------------------------------------------------------------------------

@dataclass
class Part:
    name: str
    kind: str                       # copper, dielectric, core, mask, finish, silk, paste, plating, fill, plug,
    color: Tuple[float, float, float]   # cap, component, layer, outline
    mesh: Mesh
    path: Tuple[str, ...] = ()      # the STEP assembly it sits in
    layer: str = ""
    opacity: float = 1.0


@dataclass
class Scene:
    title: str
    mode: str                       # fast, exact, layer
    parts: List[Part] = field(default_factory=list)
    view: str = "iso"               # iso, top
    flat: bool = False              # a 2D layer view
    info: List[str] = field(default_factory=list)
    hidden: List[str] = field(default_factory=list)     # kinds the settings hide (shown on request)

    def bounds(self):
        ps = [p.mesh.pos for p in self.parts if len(p.mesh.pos)]
        if not ps:
            return [0, 0, 0, 1, 1, 1]
        a = np.vstack(ps)
        return [float(v) for v in (*a.min(axis=0), *a.max(axis=0))]

    def pack(self) -> Tuple[dict, bytes]:
        """Manifest (JSON-able) and one binary buffer: per part float32
        positions, uint32 triangles, uint32 edge pairs, each 4-byte aligned."""
        chunks, off, parts = [], 0, []
        for i, p in enumerate(self.parts):
            m = p.mesh
            pos = np.ascontiguousarray(m.pos, dtype="<f4")
            tri = np.ascontiguousarray(m.tri, dtype="<u4")
            edg = np.ascontiguousarray(m.edges, dtype="<u4")
            rec = dict(id=i, name=p.name, kind=p.kind, path=list(p.path), layer=p.layer,
                       color=[round(float(c), 4) for c in p.color], opacity=p.opacity)
            for key, arr in (("pos", pos), ("tri", tri), ("edges", edg)):
                rec[key] = [off, int(arr.size)]
                chunks.append(arr.tobytes())
                off += arr.nbytes
            parts.append(rec)
        manifest = dict(version=SCENE_VERSION, title=self.title, mode=self.mode, view=self.view, flat=self.flat,
                        units="mm", bounds=self.bounds(), parts=parts, info=self.info, hidden=self.hidden,
                        triangles=int(sum(len(p.mesh.tri) for p in self.parts)))
        return manifest, b"".join(chunks)


def _parts_from_components(comps) -> List[Part]:
    """Viewer parts of the model's components: MeshGeo solids are joined,
    B-rep solids tessellated."""
    out = []
    lazy = [c for c in comps if c["solids"] and isinstance(c["solids"][0], (Mesh, Prism))]
    brep = [c for c in comps if not any(c is x for x in lazy)]
    meshes = dict(zip(map(id, lazy), realize([c["solids"] for c in lazy])))
    meshes.update(zip(map(id, brep), shape_meshes([list(c["solids"]) for c in brep])))
    for c in comps:
        mesh = meshes[id(c)]
        if not len(mesh.tri):
            continue
        kind = c.get("kind") or ("component" if c.get("path") and c["path"][0].startswith("Components") else "")
        out.append(Part(c["name"], kind, tuple(c["color"]), mesh, tuple(c.get("path") or ()), c.get("layer", "")))
    return out


def model_scene(job, exact: bool = False) -> Scene:
    """The board model as the STEP would contain it. Fast: meshes straight from
    the 2D regions; exact: the B-rep solids, tessellated. Paste and the ECAD
    parts are always in the scene; when the settings leave them out they are
    listed in Scene.hidden, so showing them is only a draw."""
    import copy
    from .builder import Model
    o = job.opts
    hidden = [] if o.paste else ["paste"]
    if not o.components:
        hidden.append("component")
    board = job.read().fresh()
    job.apply_globals()
    so = copy.copy(o)
    so.paste = True
    m = Model(board, so)
    if not exact:
        m.geo = MeshGeo
    m.prepare()
    m.build()
    comps = m.components
    if job.srcs.step:
        for c in comps:
            c["path"] = ("PCB",)
        comps = comps + job_components(job, m)
    parts = _parts_from_components(comps)
    sc = Scene(job.read().name + (" (exact)" if exact else ""), "exact" if exact else "fast", parts)
    sc.hidden = hidden
    return sc


def job_components(job, m) -> list:
    """The ECAD STEP's parts on the model's surfaces (as Job.build adds them)."""
    from . import components
    o = job.opts
    st = m.stk
    t_mask = {side: (0.0 if o.no_mask else max((L.t for L in st.coats[side].get("mask", [])), default=0.0))
              for side in ("top", "bottom")}
    key = (job.srcs.step, round(st.z_top + t_mask["top"], 6), round(st.z_bot - t_mask["bottom"], 6), m.B.bounds)
    cache = job.session.__dict__.setdefault("components", {})
    if key not in cache:
        parts = components.read(job.srcs.step, m.B.bounds, st.z_top + t_mask["top"], st.z_bot - t_mask["bottom"]) or []
        cache.clear()
        meshes = shape_meshes([list(p["solids"]) for p in parts])
        cache[key] = [dict(p, solids=[mh]) for p, mh in zip(parts, meshes)]
    return cache[key]


def layer_scene(job, spec: str) -> Scene:
    """One --export layer as it would be written: 2D formats flat from the top
    (with the outline unless --no-outline), STEP in 3D (with the PCB body if
    --outline)."""
    from . import exports
    m = job.prepare()
    t, geom, label, formats = exports.layer_geometry(m, spec, job.opts, job.args.output)
    fmt = formats[0]
    with_outline = getattr(job.opts, "export_outline", None)
    parts: List[Part] = []
    if fmt in ("dxf", "svg"):
        mesh = flat_mesh(geom)
        if mesh is not None:
            parts.append(Part(label, "layer", tuple(t.color), mesh))
        if t.kind != "outline" and with_outline is not False:
            ol = line_mesh(m.B, 0.001)
            if ol is not None:
                parts.append(Part("outline", "outline", (0.85, 0.87, 0.9), ol))
        sc = Scene(f"{label}.{fmt}", "layer", parts, view="top", flat=True)
    else:
        for name, kind, color, g, z0, z1 in exports.layer_step_bodies(m, t, geom, bool(with_outline), label, job.opts):
            mesh = realize([MeshGeo.prisms(g, z0, z1)])[0]
            if len(mesh.tri):
                parts.append(Part(name, kind, tuple(color), mesh))
        sc = Scene(f"{label}.step", "layer", parts)
    sc.info.append(f"{len(list(iter_polys(geom)))} region(s), {geom.area:.2f} mm2")
    return sc


def to_html(manifest: dict, blob: bytes, viewer_js: str, three_js: str, controls_js: str, css: str) -> str:
    """A self-contained viewer page (the CLI's --view without pywebview)."""
    import base64
    payload = base64.b64encode(blob).decode("ascii")

    def mod(src):
        return "data:text/javascript;base64," + base64.b64encode(src.encode()).decode("ascii")
    imports = {"three": mod(three_js), "three/addons/controls/OrbitControls.js": mod(controls_js)}
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>" + manifest.get("title", "PCBFab2Step") +
            "</title><style>" + css + "</style><script type='importmap'>" + json.dumps({"imports": imports}) +
            "</script></head><body class='viewer-only'><div id='viewer'></div>"
            "<script>window.PCB_SCENE={manifest:" + json.dumps(manifest) + ",data:'" + payload + "'};</script>"
            "<script type='module'>" + viewer_js + "</script></body></html>")
