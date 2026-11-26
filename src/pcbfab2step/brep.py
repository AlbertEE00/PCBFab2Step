# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""B-rep construction with OpenCascade (cadquery-ocp): exact arcs recovered
from tessellated rings, faces and prisms from shapely polygons, booleans, and
the coloured, named STEP writer."""

from __future__ import annotations

import math
import os
import re
import sys
from collections import Counter
from typing import Tuple

import numpy as np
import shapely
from shapely.geometry import Polygon, box
from shapely.geometry.polygon import orient

from . import __version__
from .common import Log, warn
from .geometry import Tess, clean, gdiff, ginter, iter_polys, polys_only, union_all


class OCC:
    loaded = False

    @classmethod
    def load(cls):
        if cls.loaded:
            return
        try:
            from OCP import gp, GC, BRepBuilderAPI, BRepPrimAPI, BRepAlgoAPI, ShapeUpgrade, TopExp, TopAbs, \
                TopoDS, BRep, Bnd, BRepBndLib, GProp, BRepGProp, ShapeFix
        except ImportError:
            sys.exit("OpenCascade bindings are required:  pip install cadquery-ocp")
        cls.gp, cls.GC = gp, GC
        cls.BB, cls.BP, cls.BA = BRepBuilderAPI, BRepPrimAPI, BRepAlgoAPI
        cls.SU, cls.TopExp, cls.TopAbs, cls.TopoDS, cls.BRep = ShapeUpgrade, TopExp, TopAbs, TopoDS, BRep
        cls.Bnd, cls.BRepBndLib, cls.GProp, cls.BRepGProp, cls.ShapeFix = Bnd, BRepBndLib, GProp, BRepGProp, ShapeFix
        cls.loaded = True

    @staticmethod
    def shape_list(shapes):
        try:
            from OCP.TopTools import TopTools_ListOfShape as L
        except ImportError:
            from OCP.collections import List_TopoDS_Shape as L
        out = L()
        for s in shapes:
            out.Append(s)
        return out


class ArcFit:
    enabled = True
    strict = False
    max_turn = math.radians(20.0)
    stats = Counter()


def _lsq_circle(P: np.ndarray):
    m = P.mean(axis=0)
    Q = P - m
    A = np.column_stack((Q[:, 0], Q[:, 1], np.ones(len(Q))))
    b = -(Q ** 2).sum(axis=1)
    try:
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    cx, cy = -sol[0] / 2.0, -sol[1] / 2.0
    r2 = cx * cx + cy * cy - sol[2]
    if r2 <= 0:
        return None
    return cx + m[0], cy + m[1], math.sqrt(r2)


def _circ3(p1, p2, p3):
    ax, ay = p1[0] - p2[0], p1[1] - p2[1]
    cx, cy = p3[0] - p2[0], p3[1] - p2[1]
    d = 2.0 * (ax * cy - ay * cx)
    if abs(d) < 1e-18:
        return None
    la, lc = ax * ax + ay * ay, cx * cx + cy * cy
    ux = (cy * la - ay * lc) / d
    uy = (ax * lc - cx * la) / d
    return p2[0] + ux, p2[1] + uy, math.hypot(ux, uy)


class _Kasa:
    """Incremental algebraic (Kasa) circle fit, coordinates relative to an origin."""

    def __init__(self, ox, oy):
        self.ox, self.oy = ox, oy
        self.s = [0.0] * 9   # n, Sx, Sy, Sxx, Syy, Sxy, Sz, Sxz, Syz

    def _upd(self, p, k):
        x, y = p[0] - self.ox, p[1] - self.oy
        z = x * x + y * y
        s = self.s
        s[0] += k
        s[1] += k * x
        s[2] += k * y
        s[3] += k * x * x
        s[4] += k * y * y
        s[5] += k * x * y
        s[6] += k * z
        s[7] += k * x * z
        s[8] += k * y * z

    def add(self, p):
        self._upd(p, 1.0)

    def remove(self, p):
        self._upd(p, -1.0)

    def solve(self):
        n, sx, sy, sxx, syy, sxy, sz, sxz, syz = self.s
        a11, a12, a13, a22, a23, a33 = sxx, sxy, sx, syy, sy, n
        b1, b2, b3 = -sxz, -syz, -sz
        c11 = a22 * a33 - a23 * a23
        c12 = a13 * a23 - a12 * a33
        c13 = a12 * a23 - a13 * a22
        det = a11 * c11 + a12 * c12 + a13 * c13
        scale = max(abs(a11 * a22 * a33), 1e-300)
        if abs(det) <= 1e-14 * scale:
            return None
        c22 = a11 * a33 - a13 * a13
        c23 = a12 * a13 - a11 * a23
        c33 = a11 * a22 - a12 * a12
        D = (c11 * b1 + c12 * b2 + c13 * b3) / det
        E = (c12 * b1 + c22 * b2 + c23 * b3) / det
        F = (c13 * b1 + c23 * b2 + c33 * b3) / det
        cx, cy = -D / 2.0, -E / 2.0
        r2 = cx * cx + cy * cy - F
        if r2 <= 0:
            return None
        return cx + self.ox, cy + self.oy, math.sqrt(r2)


def drop_collinear(P: np.ndarray, eps: float = 1e-4) -> np.ndarray:
    """Remove vertices lying on the segment between their neighbours (within
    eps mm). CAD tools insert such points mid-chord; they hide arcs."""
    for _ in range(4):
        n = len(P)
        if n <= 4:
            break
        a = np.roll(P, 1, axis=0)
        b = np.roll(P, -1, axis=0)
        ab = b - a
        ap = P - a
        L2 = (ab ** 2).sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            d = np.abs(ab[:, 0] * ap[:, 1] - ab[:, 1] * ap[:, 0]) / np.sqrt(L2)
            t = (ap * ab).sum(axis=1) / L2
        rm = (d < eps) & (t > 0.0) & (t < 1.0)
        rm &= ~np.roll(rm, 1)
        if not rm.any() or n - rm.sum() < 3:
            break
        P = P[~rm]
    return P


def _uneven(pts: np.ndarray, fit, tol: float) -> bool:
    """A tessellated arc bows alike over all its chords (a chord over a
    dropped vertex bows 4 times as much): a chord bowing over ten times the
    median, and beyond the tolerance, is a straight edge between short
    chords that are co-circular with it by chance (a slot side between its
    end arcs), not part of an arc."""
    if fit is None:
        return True
    r = fit[2]
    c = np.hypot(*(pts[1:] - pts[:-1]).T)
    sag = r - np.sqrt(np.maximum(r * r - 0.25 * c * c, 0.0))
    return float(sag.max()) > max(tol, 10.0 * float(np.median(sag)))


def find_arcs(P: np.ndarray):
    """Find runs of consecutive vertices that lie on common circles (within
    the tessellation tolerance) by greedy growth with an incremental circle
    fit. Returns (full_circle or None, [(s, e, m)]): arc from P[s] through P[m]
    to P[e], indices increasing cyclically."""
    n = len(P)
    if n < 4 or not ArcFit.enabled:
        return None, []
    nxt = np.roll(P, -1, axis=0)
    prv = np.roll(P, 1, axis=0)
    v1, v2 = P - prv, nxt - P
    turn = np.arctan2(v1[:, 0] * v2[:, 1] - v1[:, 1] * v2[:, 0], (v1 * v2).sum(axis=1))
    seg = np.hypot(v2[:, 0], v2[:, 1])
    mt = ArcFit.max_turn
    tol = Tess.tol + 5e-4
    tiny = math.radians(0.02)
    corners = np.nonzero(np.abs(turn) > mt)[0]
    start = int(corners[0]) if len(corners) else 0
    Pl = P.tolist()
    turnl = turn.tolist()
    segl = seg.tolist()

    def ix(o):
        return (start + o) % n

    runs = []
    o = 0
    while o < n - 2:
        F = _Kasa(*Pl[ix(o)])
        F.add(Pl[ix(o)])
        F.add(Pl[ix(o + 1)])
        sign = 0
        e = o + 1
        good = None
        while e < n:          # never run past the start vertex (no overlapping arcs)
            t = turnl[ix(e)]
            if abs(t) > mt:
                break
            if abs(t) > tiny:
                sg = 1 if t > 0 else -1
                if sign == 0:
                    sign = sg
                elif sg != sign:
                    break
            q = Pl[ix(e + 1)]
            F.add(q)
            e += 1
            if e - o + 1 < 4:
                continue
            c = F.solve()
            if c is None or c[2] > 1e4 or c[2] < 1e-4:
                F.remove(q)
                e -= 1
                if c is None and good is None:
                    pass
                break
            cx, cy, r = c
            if abs(math.hypot(q[0] - cx, q[1] - cy) - r) > tol:
                F.remove(q)
                e -= 1
                break
            Lc = segl[ix(e - 1)]
            ang = 2.0 * math.asin(min(1.0, Lc / (2.0 * r)))
            lim = mt * 1.2
            if ArcFit.strict:
                lim = min(lim, 2 * math.pi / Tess.nseg(r) * 1.05)
            if ang > lim:
                F.remove(q)
                e -= 1
                break
            # on a true arc every interior turn equals the mean central angle of
            # its two chords; three clusters of points are always co-circular,
            # this rejects such false arcs
            bad = False
            for k in (range(o + 1, e) if good is None else (e - 1,)):
                a1 = 2.0 * math.asin(min(1.0, segl[ix(k - 1)] / (2.0 * r)))
                a2 = 2.0 * math.asin(min(1.0, segl[ix(k)] / (2.0 * r)))
                expv = 0.5 * (a1 + a2)
                if abs(abs(turnl[ix(k)]) - expv) > 0.2 * expv + 1e-3:
                    bad = True
                    break
            if bad:
                F.remove(q)
                e -= 1
                break
            good = e
        if good is not None:
            ids = [ix(k) for k in range(o, good + 1)]
            while len(ids) >= 4:
                pts = P[ids]
                fit = _lsq_circle(pts)
                if fit is None:
                    ids = []
                    break
                res = np.abs(np.hypot(pts[:, 0] - fit[0], pts[:, 1] - fit[1]) - fit[2])
                if res.max() <= tol:
                    break
                ids.pop()
            sweep = abs(sum(turnl[k] for k in ids[1:-1])) if len(ids) >= 4 else 0.0
            if len(ids) >= 4 and sweep >= math.radians(1.0) and not _uneven(P[ids], fit, tol):
                if len(ids) >= n:          # whole ring on one circle
                    fit = _lsq_circle(P)
                    if fit is not None:
                        ArcFit.stats["circles"] += 1
                        return fit, []
                runs.append(ids)
                o = o + len(ids) - 1
                continue
        o += 1
    arcs = []
    for idx in runs:
        pieces = [idx]
        if len(idx) >= 5:
            p0, pm, p1 = P[idx[0]], P[idx[len(idx) // 2]], P[idx[-1]]
            if np.dot(p0 - pm, p1 - pm) > 0 or len(idx) > 600 or idx[0] == idx[-1]:
                h = len(idx) // 2
                pieces = [idx[:h + 1], idx[h:]]
        for pc in pieces:
            if len(pc) >= 3 and pc[0] != pc[-1]:
                arcs.append((pc[0], pc[-1], pc[len(pc) // 2]))
                ArcFit.stats["arcs"] += 1
    return None, arcs


class Brep:
    """Turns shapely polygons into OCC faces/prisms with exact arcs."""

    @staticmethod
    def _pnt(p, z):
        return OCC.gp.gp_Pnt(float(p[0]), float(p[1]), z)

    # .. ring analysis (pure numpy/shapely) ...................................
    @staticmethod
    def clean_ring(P: np.ndarray) -> np.ndarray:
        """Drop what boolean operations leave and OpenCascade rejects: points
        within 50 nm of the next one (a zero-length edge fails the whole wire)
        and the tips of spikes, where the ring runs out and straight back
        along itself (GEOS takes such a ring as simple, OpenCascade as
        self-intersecting, and the face is invalid)."""
        for _ in range(6):
            if len(P) < 4:
                break
            nxt = np.roll(P, -1, axis=0)
            dd = np.hypot(*(nxt - P).T)
            keep = dd > 5e-5
            a, b = P - np.roll(P, 1, axis=0), nxt - P
            la, lb = np.hypot(*a.T), np.hypot(*b.T)
            cross = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
            dot = (a * b).sum(axis=1)
            spike = (dot < 0) & (np.abs(cross) <= 1e-3 * la * lb) & keep
            keep &= ~spike
            if keep.all() or keep.sum() < 3:
                break
            P = P[keep]
        return P

    @staticmethod
    def ring_spec(coords):
        """-> (P, full_circle, arcs). P is the cleaned vertex array."""
        P = np.asarray(coords, dtype=float)[:, :2]
        if len(P) > 1 and abs(P[0, 0] - P[-1, 0]) < 1e-12 and abs(P[0, 1] - P[-1, 1]) < 1e-12:
            P = P[:-1]
        P = Brep.clean_ring(P)
        if not ArcFit.enabled or len(P) < 4:
            return P, None, []
        # fast path: the whole ring on one circle (pads, holes, vias)
        if len(P) >= 8:
            fit = _lsq_circle(P)
            if fit is not None:
                res = np.abs(np.hypot(P[:, 0] - fit[0], P[:, 1] - fit[1]) - fit[2])
                if res.max() <= Tess.tol + 5e-4 and fit[2] > 1e-4:
                    turn = np.diff(np.unwrap(np.arctan2(P[:, 1] - fit[1], P[:, 0] - fit[0])))
                    if np.all(turn > 0) or np.all(turn < 0):
                        ArcFit.stats["circles"] += 1
                        return P, fit, []
        P = drop_collinear(P)
        full, arcs = find_arcs(P)
        return P, full, arcs

    @staticmethod
    def spec_ring(spec):
        """Shapely ring following the fitted edges (arcs densely sampled), used
        to check that fitted rings do not touch or cross each other."""
        P, full, arcs = spec
        if full is not None:
            cx, cy, r = full
            a = np.linspace(0, 2 * math.pi, 73)[:-1]
            return shapely.LinearRing(np.column_stack((cx + r * np.cos(a), cy + r * np.sin(a))))
        if not arcs:
            return None
        n = len(P)
        arc_at = {s_: (e_, m_) for (s_, e_, m_) in arcs}
        pts = []
        i = arcs[0][0]
        start = i
        guard = 0
        while True:
            guard += 1
            if guard > 2 * n + 5:
                return None
            if i in arc_at:
                e_, m_ = arc_at[i]
                c = _circ3(P[i], P[m_], P[e_])
                if c is None:
                    pts.append(P[i])
                else:
                    a0 = math.atan2(P[i][1] - c[1], P[i][0] - c[0])
                    am = math.atan2(P[m_][1] - c[1], P[m_][0] - c[0])
                    a1 = math.atan2(P[e_][1] - c[1], P[e_][0] - c[0])
                    d1 = (am - a0) % (2 * math.pi)
                    d2 = (a1 - a0) % (2 * math.pi)
                    sweep = d2 if d1 <= d2 else d2 - 2 * math.pi
                    t = np.linspace(0.0, 1.0, max(3, int(abs(sweep) / 0.05) + 2))[:-1]
                    ang = a0 + sweep * t
                    pts.extend(np.column_stack((c[0] + c[2] * np.cos(ang), c[1] + c[2] * np.sin(ang))))
                i = e_
            else:
                pts.append(P[i])
                i = (i + 1) % n
            if i == start:
                break
        if len(pts) < 3:
            return None
        return shapely.LinearRing(np.asarray(pts))

    # .. OCC wires ...............................................................
    @classmethod
    def spec_wire(cls, spec, z, hole: bool):
        P, full, arcs = spec
        n = len(P)
        if n < 3 and full is None:
            return None
        BB = OCC.BB
        if full is not None:
            cx, cy, r = full
            d = OCC.gp.gp_Dir(0, 0, -1 if hole else 1)
            circ = OCC.gp.gp_Circ(OCC.gp.gp_Ax2(OCC.gp.gp_Pnt(cx, cy, z), d), r)
            e = BB.BRepBuilderAPI_MakeEdge(circ).Edge()
            return BB.BRepBuilderAPI_MakeWire(e).Wire()
        arc_at = {s_: (e_, m_) for (s_, e_, m_) in arcs}
        inside = set()
        for (s_, e_, m_) in arcs:
            k = (s_ + 1) % n
            while k != e_:
                inside.add(k)
                k = (k + 1) % n
        start = next((i for i in range(n) if i not in inside), None)
        if start is None:
            return None
        segs = []
        i = start
        guard = 0
        while True:
            guard += 1
            if guard > 2 * n + 5:
                return None
            if i in arc_at:
                e_, m_ = arc_at[i]
                segs.append(("A", i, e_, m_))
                i = e_
            else:
                j = (i + 1) % n
                segs.append(("L", i, j))
                i = j
            if i == start:
                break
        merged = []
        for sg in segs:
            if sg[0] == "L" and merged and merged[-1][0] == "L":
                a0, a1 = merged[-1][1], merged[-1][2]
                b1 = sg[2]
                u = P[a1] - P[a0]
                v = P[b1] - P[a1]
                lu, lv = math.hypot(*u), math.hypot(*v)
                if lu > 0 and lv > 0 and abs(u[0] * v[1] - u[1] * v[0]) <= 1e-10 * lu * lv \
                        and u[0] * v[0] + u[1] * v[1] > 0:
                    merged[-1] = ("L", a0, b1)
                    continue
            merged.append(sg)
        wm = BB.BRepBuilderAPI_MakeWire()
        verts = {}

        def vtx(k):
            v = verts.get(k)
            if v is None:
                v = BB.BRepBuilderAPI_MakeVertex(cls._pnt(P[k], z)).Vertex()
                verts[k] = v
            return v
        for sg in merged:
            if sg[0] == "L":
                if sg[1] == sg[2]:
                    continue
                e = BB.BRepBuilderAPI_MakeEdge(vtx(sg[1]), vtx(sg[2]))
            else:
                _, s_, e_, m_ = sg
                mk = OCC.GC.GC_MakeArcOfCircle(cls._pnt(P[s_], z), cls._pnt(P[m_], z), cls._pnt(P[e_], z))
                if not mk.IsDone():
                    for k0, k1 in ((s_, m_), (m_, e_)):
                        wm.Add(BB.BRepBuilderAPI_MakeEdge(vtx(k0), vtx(k1)).Edge())
                    continue
                e = BB.BRepBuilderAPI_MakeEdge(mk.Value(), vtx(s_), vtx(e_))
            if not e.IsDone():
                return None
            wm.Add(e.Edge())
        return wm.Wire() if wm.IsDone() else None

    @classmethod
    def ring_wire(cls, coords, z, hole: bool):
        return cls.spec_wire(cls.ring_spec(coords), z, hole)

    @classmethod
    def polygon_wire(cls, coords, z):
        mp = OCC.BB.BRepBuilderAPI_MakePolygon()
        P = np.asarray(coords, dtype=float)[:, :2]
        if len(P) > 1 and np.allclose(P[0], P[-1]):
            P = P[:-1]
        for p in P:
            mp.Add(cls._pnt(p, z))
        mp.Close()
        return mp.Wire() if mp.IsDone() else None

    # .. faces ...................................................................
    @staticmethod
    def _touching(rings):
        tree = shapely.STRtree(rings)
        for i, r in enumerate(rings):
            for j in tree.query(r, predicate="dwithin", distance=1e-6):
                if int(j) != i:
                    return True
        return False

    @staticmethod
    def _separate(poly, d=2e-4):
        """Rings that touch at a point are valid for GEOS but not for a B-rep
        face: shrink just those rings by d (0.2 um)."""
        rings = [poly.exterior] + list(poly.interiors)
        tree = shapely.STRtree(rings)
        touch = set()
        where = (poly.centroid.x, poly.centroid.y)
        for i, r in enumerate(rings):
            for j in tree.query(r, predicate="dwithin", distance=1e-6):
                j = int(j)
                if j != i:
                    touch.update((i, j))
                    p = r.intersection(rings[j])
                    if not p.is_empty:
                        where = (p.bounds[0], p.bounds[1])
        ext = Polygon(poly.exterior)
        if 0 in touch:
            ext = polys_only(ext.buffer(-d, join_style="mitre"))
        holes = []
        for k, r in enumerate(poly.interiors, 1):
            hp = Polygon(r)
            holes.append(polys_only(hp.buffer(-d, join_style="mitre")) if k in touch else hp)
        return gdiff(ext, union_all(holes)), where

    @staticmethod
    def _area(face) -> float:
        p = OCC.GProp.GProp_GProps()
        OCC.BRepGProp.BRepGProp.SurfaceProperties_s(face, p)
        return p.Mass()

    @staticmethod
    def fit_area(spec) -> Tuple[float, float]:
        """(area inside the fitted ring, area of its vertex polygon)."""
        P, full, arcs = spec
        n = len(P)
        x, y = P[:, 0], P[:, 1]
        a_poly = 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y)))
        if full is not None:
            return math.pi * full[2] ** 2, a_poly
        if not arcs:
            return a_poly, a_poly
        arc_at = {s_: (e_, m_) for (s_, e_, m_) in arcs}
        inside = set()
        for (s_, e_, m_) in arcs:
            k = (s_ + 1) % n
            while k != e_:
                inside.add(k)
                k = (k + 1) % n
        start = next((i for i in range(n) if i not in inside), None)
        if start is None:
            return a_poly, a_poly
        signed = 0.0
        i = start
        for _ in range(2 * n + 5):
            if i in arc_at:
                e_, m_ = arc_at[i]
                c = _circ3(P[i], P[m_], P[e_])
                if c is None:
                    for k0, k1 in ((i, m_), (m_, e_)):
                        signed += 0.5 * (P[k0, 0] * P[k1, 1] - P[k1, 0] * P[k0, 1])
                else:
                    cx, cy, r = c
                    a0 = math.atan2(P[i, 1] - cy, P[i, 0] - cx)
                    am = math.atan2(P[m_, 1] - cy, P[m_, 0] - cx)
                    a1 = math.atan2(P[e_, 1] - cy, P[e_, 0] - cx)
                    d1 = (am - a0) % (2 * math.pi)
                    d2 = (a1 - a0) % (2 * math.pi)
                    sw = d2 if d1 <= d2 else d2 - 2 * math.pi
                    signed += 0.5 * (cx * r * (math.sin(a0 + sw) - math.sin(a0))
                                     - cy * r * (math.cos(a0 + sw) - math.cos(a0)) + r * r * sw)
                i = e_
            else:
                j = (i + 1) % n
                signed += 0.5 * (P[i, 0] * P[j, 1] - P[j, 0] * P[i, 1])
                i = j
            if i == start:
                break
        return abs(signed), a_poly

    @staticmethod
    def _valid(face) -> bool:
        from OCP.BRepCheck import BRepCheck_Analyzer
        return face is not None and BRepCheck_Analyzer(face).IsValid()

    @classmethod
    def _build_face(cls, specs, z):
        ow = cls.spec_wire(specs[0], z, False)
        if ow is None:
            return None
        fm = OCC.BB.BRepBuilderAPI_MakeFace(ow, True)
        if not fm.IsDone():
            return None
        for sp in specs[1:]:
            iw = cls.spec_wire(sp, z, True)
            if iw is not None:
                fm.Add(iw)
        return fm.Face() if fm.IsDone() else None

    @classmethod
    def _poly_face(cls, poly, z):
        rings = [poly.exterior] + list(poly.interiors)
        plain = [(cls.clean_ring(np.asarray(r.coords)[:-1, :2]), None, []) for r in rings]
        specs = [cls.ring_spec(r.coords) for r in rings] if ArcFit.enabled else plain
        if ArcFit.enabled:
            # fitted rings must stay simple and disjoint (checked on a dense
            # shapely approximation, O(n log n)); offenders keep plain edges
            for _ in range(3):
                approx = [cls.spec_ring(sp) if (sp[1] is not None or sp[2]) else None for sp in specs]
                bad = set(k for k, a in enumerate(approx) if a is not None and not a.is_simple)
                geoms = [approx[k] if (approx[k] is not None and k not in bad) else rings[k]
                         for k in range(len(rings))]
                if len(geoms) > 1:
                    tree = shapely.STRtree(geoms)
                    for k, g in enumerate(geoms):
                        if approx[k] is None:
                            continue
                        for j in tree.query(g, predicate="intersects"):
                            j = int(j)
                            if j != k:
                                bad.add(k)
                                if approx[j] is not None:
                                    bad.add(j)
                if not bad:
                    break
                ArcFit.stats["unfitted_rings"] += len(bad)
                for k in bad:
                    specs[k] = plain[k]
        f = cls._build_face(specs, z)
        if f is not None and ArcFit.enabled:
            tol = Tess.tol + 5e-4
            lim = 2.0 * tol * poly.length + 1e-6 * poly.area + 1e-9
            if abs(cls._area(f) - poly.area) > lim:
                # keep plain edges only on the rings whose fit changes their own
                # polygon's area beyond the tolerance along their length (a
                # wrong arc, or a coarse polygon such as a round outline written
                # as a 128-gon), not on every ring of the face
                changed = False
                for k, sp in enumerate(specs):
                    if sp[1] is None and not sp[2]:
                        continue
                    a_fit, a_poly = cls.fit_area(sp)
                    if abs(a_fit - a_poly) > 2.0 * tol * rings[k].length + 1e-9:
                        ArcFit.stats["unfitted_rings"] += 1
                        specs[k] = plain[k]
                        changed = True
                f2 = cls._build_face(specs, z) if changed else None
                if f2 is None or abs(cls._area(f2) - poly.area) > lim:
                    ArcFit.stats["fallbacks"] += 1
                    f2 = cls._build_face(plain, z)
                f = f2 if f2 is not None else f       # never trade a face for none
        if f is not None and len(rings) <= 50 and not cls._valid(f):
            f2 = cls._build_face(plain, z)
            if cls._valid(f2):
                f = f2
        return f

    max_holes = 1000

    @classmethod
    def split_many_holes(cls, poly, depth=0):
        """OCCT's STEP translation is quadratic in the number of holes of a
        face. Split huge regions at a gap between holes (never through one)
        into tiles; the tiles stay touching solids of the same body."""
        k = cls.max_holes
        if k <= 0 or len(poly.interiors) <= k or depth > 12:
            return [poly]
        x0, y0, x1, y1 = poly.bounds
        vertical = (x1 - x0) >= (y1 - y0)
        rb = np.array([r.bounds for r in poly.interiors])
        lo = rb[:, 0] if vertical else rb[:, 1]
        hi = rb[:, 2] if vertical else rb[:, 3]
        mid = float(np.median(0.5 * (lo + hi)))
        cands = np.unique(np.concatenate((hi + 1e-3, lo - 1e-3)))
        cands = cands[np.argsort(np.abs(cands - mid))][:400]
        cut = mid
        for c in cands:
            if not np.any((lo < c) & (hi > c)):
                cut = float(c)
                break
        if vertical:
            a, b = box(x0 - 1, y0 - 1, cut, y1 + 1), box(cut, y0 - 1, x1 + 1, y1 + 1)
        else:
            a, b = box(x0 - 1, y0 - 1, x1 + 1, cut), box(x0 - 1, cut, x1 + 1, y1 + 1)
        out = []
        for half in (a, b):
            for q in iter_polys(clean(ginter(poly, half))):
                out.extend(cls.split_many_holes(orient(q, 1.0), depth + 1))
        return out

    @classmethod
    def faces(cls, geom, z: float):
        out = []
        polys = []
        for poly in iter_polys(geom):
            if poly.area >= 1e-9:
                polys.extend(cls.split_many_holes(orient(poly, 1.0)))
        for poly in polys:
            if poly.area < 1e-9:
                continue
            poly = orient(poly, 1.0)
            rings = [poly.exterior] + list(poly.interiors)
            pieces = [poly]
            if len(rings) > 1 and cls._touching(rings):
                sep, where = cls._separate(poly)
                pieces = [orient(q, 1.0) for q in iter_polys(sep)]
                if not pieces:
                    warn(f"could not separate touching boundaries near ({where[0]:.3f}, {where[1]:.3f}) mm")
                    pieces = [poly]
            for q in pieces:
                f = cls._poly_face(q, z)
                if f is None:                          # last resort: GEOS rebuilds the rings
                    for r in iter_polys(clean(q.buffer(0))):
                        f = cls._poly_face(orient(r, 1.0), z)
                        if f is not None:
                            out.append(f)
                        else:
                            c = r.representative_point()
                            warn(f"a region of {r.area:.3f} mm2 at z {z:.3f} near ({c.x:.3f}, {c.y:.3f}) mm "
                                 "could not be made a B-rep face: left out of the STEP")
                    continue
                out.append(f)
        return out

    @classmethod
    def prisms(cls, geom, z0: float, z1: float):
        h = z1 - z0
        if h <= 1e-9 or geom is None or geom.is_empty:
            return []
        out = []
        vec = OCC.gp.gp_Vec(0, 0, h)
        for f in cls.faces(geom, z0):
            pm = OCC.BP.BRepPrimAPI_MakePrism(f, vec)
            if pm.IsDone():
                out.append(pm.Shape())
        return out

    @classmethod
    def sheets(cls, geom, z: float, sg: float = 1.0):
        """Faces only (a surface body), at z; facing up on the top side (sg 1),
        down on the bottom (sg -1)."""
        if geom is None or geom.is_empty:
            return []
        out = cls.faces(geom, z)
        return out if sg > 0 else [f.Reversed() for f in out]

    @staticmethod
    def disc_prism(x, y, r_out, r_in, z0, z1):
        """Exact cylinder / tube between z0 and z1."""
        if z1 - z0 <= 1e-9 or r_out <= 1e-9:
            return None
        gp, BB = OCC.gp, OCC.BB
        ow = BB.BRepBuilderAPI_MakeWire(BB.BRepBuilderAPI_MakeEdge(
            gp.gp_Circ(gp.gp_Ax2(gp.gp_Pnt(x, y, z0), gp.gp_Dir(0, 0, 1)), r_out)).Edge()).Wire()
        fm = BB.BRepBuilderAPI_MakeFace(ow, True)
        if r_in > 1e-6:
            iw = BB.BRepBuilderAPI_MakeWire(BB.BRepBuilderAPI_MakeEdge(
                gp.gp_Circ(gp.gp_Ax2(gp.gp_Pnt(x, y, z0), gp.gp_Dir(0, 0, -1)), r_in)).Edge()).Wire()
            fm.Add(iw)
        return OCC.BP.BRepPrimAPI_MakePrism(fm.Face(), gp.gp_Vec(0, 0, z1 - z0)).Shape()


def solids_of(shape):
    out = []
    ex = OCC.TopExp.TopExp_Explorer(shape, OCC.TopAbs.TopAbs_SOLID)
    while ex.More():
        out.append(ex.Current())
        ex.Next()
    return out


def _has_errors(op) -> bool:
    """BOPAlgo error flag; not exposed by every OCP build."""
    fn = getattr(op, "HasErrors", None)
    try:
        return bool(fn()) if fn else False
    except Exception:
        return False


def face_count(shape) -> int:
    k = 0
    ex = OCC.TopExp.TopExp_Explorer(shape, OCC.TopAbs.TopAbs_FACE)
    while ex.More():
        k += 1
        ex.Next()
    return k


def bbox(shape):
    b = OCC.Bnd.Bnd_Box()
    OCC.BRepBndLib.BRepBndLib.Add_s(shape, b)
    if b.IsVoid():
        return None
    p0, p1 = b.CornerMin(), b.CornerMax()
    return (p0.X(), p0.Y(), p0.Z(), p1.X(), p1.Y(), p1.Z())


def volume(shape) -> float:
    p = OCC.GProp.GProp_GProps()
    OCC.BRepGProp.BRepGProp.VolumeProperties_s(shape, p)
    return p.Mass()


def cut_shapes(shape, tools, what: str = "a countersink cut"):
    if not tools:
        return [shape]
    try:
        c = OCC.BA.BRepAlgoAPI_Cut()
        c.SetArguments(OCC.shape_list([shape]))
        c.SetTools(OCC.shape_list(tools))
        c.SetRunParallel(True)
        c.Build()
        if not c.IsDone() or _has_errors(c):
            warn(f"{what} failed; body left uncut")
            return [shape]
        return solids_of(c.Shape())
    except Exception:
        warn(f"{what} failed; body left uncut")
        return [shape]


class _Quiet:
    """Silence OpenCascade's stdout chatter."""

    def __init__(self, on=True):
        self.on = on

    def __enter__(self):
        if self.on:
            sys.stdout.flush()
            self.fd = os.dup(1)
            self.null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(self.null, 1)
        return self

    def __exit__(self, *a):
        if self.on:
            sys.stdout.flush()
            os.dup2(self.fd, 1)
            os.close(self.fd)
            os.close(self.null)


def mesh_face(shape, tol: float = 0.005):
    """A solid as one face that holds only its triangles (no surface): STEP
    AP242 writes it as a TRIANGULATED_SURFACE_SET. The corners of each face
    of the solid stay its own, so their normals keep the edges sharp."""
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.Poly import Poly_Triangle, Poly_Triangulation
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    BRepMesh_IncrementalMesh(shape, tol, False, 0.35, True)
    pts, tris = [], []
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        f = TopoDS.Face(ex.Current())
        loc = TopLoc_Location()
        tri = OCC.BRep.BRep_Tool.Triangulation_s(f, loc)
        if tri is not None:
            tr, off = loc.Transformation(), len(pts)
            for i in range(1, tri.NbNodes() + 1):
                q = tri.Node(i).Transformed(tr)
                pts.append((q.X(), q.Y(), q.Z()))
            rev = f.Orientation() == TopAbs_REVERSED
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                tris.append((off + a, off + c, off + b) if rev else (off + a, off + b, off + c))
        ex.Next()
    if not tris:
        return None
    T = Poly_Triangulation(len(pts), len(tris), False)
    for i, (x, y, z) in enumerate(pts, 1):
        T.SetNode(i, OCC.gp.gp_Pnt(x, y, z))
    for i, (a, b, c) in enumerate(tris, 1):
        T.SetTriangle(i, Poly_Triangle(a, b, c))
    face = OCC.TopoDS.TopoDS_Face()
    OCC.BRep.BRep_Builder().MakeFace(face, T)
    return face


def write_step(path: str, root_name: str, components, schema: str = "AP214", fmt: str = "ODB++",
               mesh_tol: float = 0.005):
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.Quantity import Quantity_Color, Quantity_TypeOfColor
    from OCP.TDataStd import TDataStd_Name
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_StepModelType
    from OCP.Interface import Interface_Static
    from OCP.TopLoc import TopLoc_Location
    from OCP.IFSelect import IFSelect_ReturnStatus

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    lt = XCAFDoc_DocumentTool.LayerTool_s(doc.Main())
    # bodies with a "path" go into nested assemblies (PCB, Components bottom > R1 > body ...);
    # the tree is built leaves first, then each assembly is added to its parent
    kids = {(): []}
    for comp in components:
        if not comp["solids"]:
            continue
        cmp = OCC.TopoDS.TopoDS_Compound()
        bld = OCC.BRep.BRep_Builder()
        bld.MakeCompound(cmp)
        for s in comp["solids"]:
            if comp.get("mesh"):
                s = mesh_face(s, mesh_tol)
                if s is None:
                    continue
            bld.Add(cmp, s)
        lab = st.AddShape(cmp, False)
        TDataStd_Name.Set_s(lab, TCollection_ExtendedString(comp["name"]))
        r, g, b = comp["color"]
        ct.SetColor(lab, Quantity_Color(r, g, b, Quantity_TypeOfColor.Quantity_TOC_sRGB),
                    XCAFDoc_ColorType.XCAFDoc_ColorSurf)
        if comp.get("layer"):
            lt.SetLayer(lab, TCollection_ExtendedString(comp["layer"]))
        grp = tuple(comp.get("path") or ())
        for i in range(len(grp) + 1):
            kids.setdefault(grp[:i], [])
            if i and grp[:i] not in kids[grp[:i - 1]]:
                kids[grp[:i - 1]].append(grp[:i])
        kids[grp].append(lab)

    def assemble(grp):
        asm = st.NewShape()
        TDataStd_Name.Set_s(asm, TCollection_ExtendedString(grp[-1] if grp else root_name))
        for k in kids[grp]:
            child = assemble(k) if isinstance(k, tuple) else k
            st.AddComponent(asm, child, TopLoc_Location())
        return asm
    assemble(())
    st.UpdateAssemblies()
    from OCP.STEPControl import STEPControl_Controller
    STEPControl_Controller.Init_s()     # registers the write.step.* parameters
    sch = {"AP214": "AP214IS", "AP203": "AP203", "AP242": "AP242DIS"}.get(schema.upper(), "AP214IS")
    ok_params = all((
        Interface_Static.SetCVal_s("write.step.schema", sch),
        Interface_Static.SetCVal_s("write.step.unit", "MM"),
        # no 2D parameter curves: much smaller files; CAD and EM importers rebuild them
        Interface_Static.SetIVal_s("write.surfacecurve.mode", 0)))
    if not ok_params:
        warn("some STEP writer parameters were not accepted by this OpenCascade version")
    w = STEPCAFControl_Writer()
    w.SetColorMode(True)
    w.SetNameMode(True)
    w.SetLayerMode(True)
    with _Quiet(Log.verbose < 4):
        ok = w.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
        status = w.Write(path) if ok else None
    if not ok or status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise SystemExit(f"STEP export failed ({status})")
    _stamp_header(path, root_name, fmt)


def _stamp_header(path: str, root_name: str, fmt: str = "ODB++"):
    """Record the converter, its version and the source format in the STEP header."""
    with open(path, "r", encoding="latin-1") as f:
        text = f.read()
    head_end = text.find("ENDSEC;")
    head, body = text[:head_end], text[head_end:]
    src = f"pcbfab2step {__version__}"
    head = re.sub(r"FILE_DESCRIPTION\(\(.*?\),", lambda m: f"FILE_DESCRIPTION(('{root_name} from {fmt} by {src}'),",
                  head, count=1, flags=re.S)
    head = re.sub(r"FILE_NAME\('(.*?)',", lambda m: f"FILE_NAME('{root_name}',", head, count=1, flags=re.S)
    head = head.replace("'Open CASCADE STEP processor", f"'{src} / Open CASCADE STEP processor", 1)
    with open(path, "w", encoding="latin-1") as f:
        f.write(head + body)
