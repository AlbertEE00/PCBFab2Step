# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""2D geometry: shapely helpers, phase-locked tessellation, primitive shapes and
2D transforms. Units are millimetres; arcs are tessellated finely here and
recovered as exact circles when the B-rep is built (brep.py)."""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Tuple

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box
from shapely.ops import unary_union


EMPTY = Polygon()


def polys_only(g):
    """Areal part of a geometry as Polygon/MultiPolygon (drops lines/points)."""
    if g is None or g.is_empty:
        return EMPTY
    if isinstance(g, (Polygon, MultiPolygon)):
        return g
    if isinstance(g, GeometryCollection):
        parts = [p for p in g.geoms if isinstance(p, (Polygon, MultiPolygon)) and not p.is_empty]
        return unary_union(parts) if parts else EMPTY
    return EMPTY


def union_all(geoms: Iterable) -> Polygon:
    geoms = [g for g in geoms if g is not None and not g.is_empty]
    if not geoms:
        return EMPTY
    if len(geoms) == 1:
        return polys_only(geoms[0])
    return polys_only(unary_union(geoms))


def gdiff(a, b):
    if a is None or a.is_empty:
        return EMPTY
    if b is None or b.is_empty:
        return a
    return polys_only(a.difference(b))


def ginter(a, b):
    if a is None or b is None or a.is_empty or b.is_empty:
        return EMPTY
    return polys_only(a.intersection(b))


def iter_polys(g):
    if g is None or g.is_empty:
        return
    if isinstance(g, Polygon):
        yield g
    elif isinstance(g, (MultiPolygon, GeometryCollection)):
        for p in g.geoms:
            yield from iter_polys(p)


SLIVER_WIDTH = 0.004   # mm; polygons/holes thinner than this on average are artefacts


def _thin(p, min_area):
    a = p.area
    return a < min_area or 2.0 * a / max(p.length, 1e-12) < SLIVER_WIDTH / 2.0


def clean(g, min_area: float = 1e-7):
    """Drop sliver polygons and sliver holes left by boolean operations
    (e.g. a pour edge and the board outline tessellated differently)."""
    out = []
    for p in iter_polys(g):
        if _thin(Polygon(p.exterior), min_area):
            continue
        holes = [r for r in p.interiors if not _thin(Polygon(r), min_area)]
        if len(holes) != len(p.interiors):
            p = Polygon(p.exterior, holes)
        # rings: a pad minus a hole a fraction of a micron smaller (rounded export sizes)
        if p.interiors and 2.0 * p.area / max(p.length, 1e-12) < SLIVER_WIDTH / 2.0:
            continue
        out.append(p)
    if not out:
        return EMPTY
    return out[0] if len(out) == 1 else MultiPolygon(out)


def make_valid(g):
    if g is None or g.is_empty:
        return EMPTY
    if g.is_valid:
        return g
    return polys_only(shapely.make_valid(g))


def contour_parents(polys: List[object]) -> Tuple[np.ndarray, np.ndarray]:
    """For each filled contour, the index of the smallest other contour that
    contains it (-1 if none), tested with an interior point of the contour.
    Valid ODB++ contours do not cross, so one interior point decides nesting.
    Returns (parent, area)."""
    arr = np.empty(len(polys), dtype=object)
    arr[:] = polys
    area = shapely.area(arr)
    parent = np.full(len(polys), -1, dtype=int)
    if len(polys) < 2:
        return parent, area
    pts = shapely.point_on_surface(arr)
    # containers are the query side so each one is prepared once (large pours)
    cont, inner = shapely.STRtree(pts).query(arr, predicate="contains")
    keep = (cont != inner) & (area[cont] > area[inner] * (1.0 + 1e-9))
    cont, inner = cont[keep], inner[keep]
    if len(inner):
        order = np.lexsort((area[cont], inner))
        cont, inner = cont[order], inner[order]
        first = np.ones(len(inner), dtype=bool)
        first[1:] = inner[1:] != inner[:-1]
        parent[inner[first]] = cont[first]
    return parent, area


class Tess:
    tol = 0.001       # max chord deviation (mm)
    min_seg = 16      # min segments for a full circle
    max_seg = 4096

    @staticmethod
    def nseg(r: float) -> int:
        """Segments for a full circle of radius r (phase-locked angular grid)."""
        if r <= Tess.tol:
            return Tess.min_seg
        step = 2.0 * math.acos(max(-1.0, 1.0 - Tess.tol / r))
        n = int(math.ceil(2.0 * math.pi / max(step, 1e-9)))
        n = max(Tess.min_seg, min(Tess.max_seg, n))
        return n + (n % 2)  # even, so 180 degrees is on the grid


def _grid_angles(a0: float, a1: float, r: float) -> np.ndarray:
    """Angles from a0 to a1 (a1 > a0, CCW) including both ends, with interior
    vertices locked to the global grid k*2pi/n(r). Identical arcs therefore get
    identical vertices, which keeps boolean results clean."""
    n = Tess.nseg(r)
    d = 2.0 * math.pi / n
    k0 = math.floor(a0 / d + 1e-9) + 1
    k1 = math.ceil(a1 / d - 1e-9) - 1
    inner = np.arange(k0, k1 + 1, dtype=float) * d if k1 >= k0 else np.zeros(0)
    # avoid near-duplicate vertices next to the ends
    if inner.size:
        keep = (inner - a0 > 0.05 * d) & (a1 - inner > 0.05 * d)
        inner = inner[keep]
    return np.concatenate(([a0], inner, [a1]))


_circle_cache: Dict[tuple, Polygon] = {}


def circle(cx: float, cy: float, r: float) -> Polygon:
    if r <= 0:
        return EMPTY
    key = (round(cx, 9), round(cy, 9), round(r, 9))
    g = _circle_cache.get(key)
    if g is None:
        n = Tess.nseg(r)
        a = np.arange(n) * (2.0 * math.pi / n)
        g = Polygon(np.column_stack((cx + r * np.cos(a), cy + r * np.sin(a))))
        if len(_circle_cache) < 400000:
            _circle_cache[key] = g
    return g


def arc_pts(cx, cy, r, a0, a1) -> np.ndarray:
    """Points on a CCW arc a0->a1 (radians, a1 >= a0)."""
    ang = _grid_angles(a0, a1, r)
    return np.column_stack((cx + r * np.cos(ang), cy + r * np.sin(ang)))


def arc_centerline(sx, sy, ex, ey, cx, cy, cw: bool) -> np.ndarray:
    """Centre line points of an ODB++ arc (start, end, centre, direction).
    Start == end gives a full circle. Radius is interpolated linearly when the
    start and end radii differ (as Gerber/ODB viewers do)."""
    r0 = math.hypot(sx - cx, sy - cy)
    r1 = math.hypot(ex - cx, ey - cy)
    if r0 < 1e-9 and r1 < 1e-9:
        return np.array([[sx, sy], [ex, ey]])
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    same = abs(sx - ex) < 1e-9 and abs(sy - ey) < 1e-9
    sweep = (a0 - a1) if cw else (a1 - a0)
    sweep %= 2.0 * math.pi
    if same or sweep < 1e-12:
        sweep = 2.0 * math.pi if same else sweep
    if sweep < 1e-12:
        return np.array([[sx, sy], [ex, ey]])
    if abs(r0 - r1) < 1e-6:
        r = 0.5 * (r0 + r1)
        if cw:
            # walk CCW from end to start, then reverse
            b0 = a1
            pts = arc_pts(cx, cy, r, b0, b0 + sweep)[::-1]
        else:
            pts = arc_pts(cx, cy, r, a0, a0 + sweep)
    else:
        n = max(2, int(math.ceil(sweep / (2 * math.pi / Tess.nseg(max(r0, r1))))))
        t = np.linspace(0.0, 1.0, n + 1)
        ang = a0 + (-1.0 if cw else 1.0) * sweep * t
        rr = r0 + (r1 - r0) * t
        pts = np.column_stack((cx + rr * np.cos(ang), cy + rr * np.sin(ang)))
    pts[0] = (sx, sy)
    pts[-1] = (ex, ey)
    return pts


def stadium(x0, y0, x1, y1, r) -> Polygon:
    """Line with round caps (Minkowski sum of a segment and a disc)."""
    if r <= 0:
        return EMPTY
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return circle(x0, y0, r)
    ang = math.atan2(dy, dx)
    # cap at end: from ang-90 to ang+90 (CCW); cap at start: ang+90 .. ang+270
    c1 = arc_pts(x1, y1, r, ang - math.pi / 2, ang + math.pi / 2)
    c0 = arc_pts(x0, y0, r, ang + math.pi / 2, ang + 3 * math.pi / 2)
    return Polygon(np.vstack((c1, c0)))


def square_line(x0, y0, x1, y1, s) -> Polygon:
    """Line drawn with a square symbol: hull of the squares at both ends."""
    h = s / 2.0
    pts = [(x0 - h, y0 - h), (x0 + h, y0 - h), (x0 + h, y0 + h), (x0 - h, y0 + h),
           (x1 - h, y1 - h), (x1 + h, y1 - h), (x1 + h, y1 + h), (x1 - h, y1 + h)]
    return shapely.MultiPoint(pts).convex_hull


def thick_arc(sx, sy, ex, ey, cx, cy, cw, r) -> Polygon:
    """Arc drawn with a round symbol of radius r (round ends)."""
    if r <= 0:
        return EMPTY
    R0 = math.hypot(sx - cx, sy - cy)
    R1 = math.hypot(ex - cx, ey - cy)
    same = abs(sx - ex) < 1e-9 and abs(sy - ey) < 1e-9
    R = 0.5 * (R0 + R1)
    if abs(R0 - R1) > 1e-6 or R <= r * 1.0001:
        cl = arc_centerline(sx, sy, ex, ey, cx, cy, cw)
        if len(cl) < 2:
            return circle(sx, sy, r)
        parts = [stadium(cl[i, 0], cl[i, 1], cl[i + 1, 0], cl[i + 1, 1], r) for i in range(len(cl) - 1)]
        return union_all(parts)
    if same:
        return gdiff(circle(cx, cy, R + r), circle(cx, cy, R - r))
    if cw:  # draw as CCW from end to start
        sx, sy, ex, ey = ex, ey, sx, sy
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    sweep = (a1 - a0) % (2 * math.pi)
    if sweep < 1e-12:
        return union_all([circle(sx, sy, r), circle(ex, ey, r)])
    a1 = a0 + sweep
    outer = arc_pts(cx, cy, R + r, a0, a1)
    cap_e = arc_pts(ex, ey, r, a1, a1 + math.pi)
    inner = arc_pts(cx, cy, R - r, a0, a1)[::-1]
    cap_s = arc_pts(sx, sy, r, a0 + math.pi, a0 + 2 * math.pi)
    poly = Polygon(np.vstack((outer, cap_e, inner, cap_s)))
    if not poly.is_valid:
        poly = make_valid(poly)
        # overlapping caps on nearly closed arcs: add caps explicitly
        poly = union_all([poly, circle(sx, sy, r), circle(ex, ey, r)])
    return poly


IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def m_compose(m2, m1):
    """Return m2 o m1 (apply m1 first)."""
    a1, b1, d1, e1, x1, y1 = m1
    a2, b2, d2, e2, x2, y2 = m2
    return (a2 * a1 + b2 * d1, a2 * b1 + b2 * e1,
            d2 * a1 + e2 * d1, d2 * b1 + e2 * e1,
            a2 * x1 + b2 * y1 + x2, d2 * x1 + e2 * y1 + y2)


def m_rot_cw(deg):
    t = math.radians(-deg)
    c, s = math.cos(t), math.sin(t)
    if abs(c) < 1e-15:
        c = 0.0
    if abs(s) < 1e-15:
        s = 0.0
    return (c, -s, s, c, 0.0, 0.0)


M_MIRROR_X = (-1.0, 0.0, 0.0, 1.0, 0.0, 0.0)   # x -> -x


def m_translate(x, y):
    return (1.0, 0.0, 0.0, 1.0, x, y)


def m_apply_pt(m, x, y):
    a, b, d, e, xo, yo = m
    return a * x + b * y + xo, d * x + e * y + yo


def m_apply(m, g):
    if g is None or g.is_empty or m == IDENT:
        return g
    a, b, d, e, xo, yo = m
    return affinity.affine_transform(g, [a, b, d, e, xo, yo])


def m_is_mirror(m) -> bool:
    a, b, d, e, _, _ = m
    return a * e - b * d < 0


def orient_matrix(orient_def: int, angle: float, mirror_x_axis: bool = True):
    """Pad/text orientation. ODB++: rotation is clockwise; when both rotated and
    mirrored the pad is rotated first, then mirrored (x -> -x)."""
    if orient_def in (0, 1, 2, 3, 4, 5, 6, 7):
        ang = 90.0 * (orient_def % 4)
        mir = orient_def >= 4
    elif orient_def == 8:
        ang, mir = angle, False
    elif orient_def == 9:
        ang, mir = angle, True
    else:
        ang, mir = 0.0, False
    m = m_rot_cw(ang)
    if mir:
        m = m_compose(M_MIRROR_X, m)
    return m


def _rect_shape(w, h, rad=0.0, corners="1234", chamfer=False):
    """Rectangle with rounded or chamfered corners. ODB++ corner numbering is
    counter-clockwise from the top right: 1=TR, 2=TL, 3=BL, 4=BR."""
    if w <= 0 or h <= 0:
        return EMPTY
    hw, hh = w / 2.0, h / 2.0
    rad = max(0.0, min(rad, hw, hh))
    if rad <= 1e-12 or not corners:
        return box(-hw, -hh, hw, hh)
    spec = {"1": (hw, hh, 0.0), "2": (-hw, hh, 0.5 * math.pi),
            "3": (-hw, -hh, math.pi), "4": (hw, -hh, 1.5 * math.pi)}
    pts: List[Tuple[float, float]] = []
    for c in "1234":
        x, y, a0 = spec[c]
        if c in corners:
            cx = x - math.copysign(rad, x)
            cy = y - math.copysign(rad, y)
            if chamfer:
                pts.append((cx + rad * math.cos(a0), cy + rad * math.sin(a0)))
                pts.append((cx + rad * math.cos(a0 + math.pi / 2), cy + rad * math.sin(a0 + math.pi / 2)))
            else:
                pts.extend(map(tuple, arc_pts(cx, cy, rad, a0, a0 + math.pi / 2)))
        else:
            pts.append((x, y))
    return make_valid(Polygon(pts))


def _oval(w, h):
    if w <= 0 or h <= 0:
        return EMPTY
    if w >= h:
        d = (w - h) / 2.0
        return stadium(-d, 0.0, d, 0.0, h / 2.0)
    d = (h - w) / 2.0
    return stadium(0.0, -d, 0.0, d, w / 2.0)


def _ellipse(w, h):
    r = max(w, h) / 2.0
    n = Tess.nseg(r)
    a = np.arange(n) * (2 * math.pi / n)
    return Polygon(np.column_stack((w / 2.0 * np.cos(a), h / 2.0 * np.sin(a))))


def _spoke_cuts(angle_deg, n, gap, extent):
    """Spoke openings of a thermal: strips of width 'gap' from the centre
    outwards. Angle is counter-clockwise from +x (ODB++ angle convention)."""
    n = int(round(n))
    if n <= 0 or gap <= 0:
        return EMPTY
    cuts = []
    for k in range(n):
        a = math.radians(angle_deg + k * 360.0 / n)
        c, s = math.cos(a), math.sin(a)
        nx, ny = -s * gap / 2.0, c * gap / 2.0
        E = extent
        cuts.append(Polygon([(nx, ny), (nx + c * E, ny + s * E), (-nx + c * E, -ny + s * E), (-nx, -ny)]))
    return union_all(cuts)


def _round_ends(g, lw):
    if lw <= 0 or g.is_empty:
        return g
    rho = 0.49 * lw
    return polys_only(g.buffer(-rho, quad_segs=16).buffer(rho, quad_segs=16))


def _thermal(ring, outer_extent, angle, n, gap, rounded=False, lw=0.0):
    g = gdiff(ring, _spoke_cuts(angle, n, gap, outer_extent * 1.5 + 1.0))
    if rounded:
        g = _round_ends(g, lw)
    return g


def _poly(pts):
    return make_valid(Polygon(pts))


def simplify(g, tol: float):
    """Douglas-Peucker on every ring (topology kept): drops points that are
    within tol of the line through their neighbours."""
    if not tol or tol <= 0 or g is None or g.is_empty:
        return g
    import shapely
    return clean(shapely.simplify(g, tol, preserve_topology=True))
