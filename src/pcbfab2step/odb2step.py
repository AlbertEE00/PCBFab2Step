#!/usr/bin/env python3
"""
odb2step.py  -  convert an ODB++ product model into a coloured, named 3D STEP model.

    pip install numpy shapely cadquery-ocp          (Python 3.9+)
    python odb2step.py board.tgz                     -> board.step next to the input
    python odb2step.py board.zip -o out.step --mask-color blue --silk-color yellow
    python odb2step.py odb_folder --list             stackup + hole report only
    python odb2step.py -h                            all options

Input: an ODB++ folder, .tgz/.tar.gz/.tar or .zip (nested archives and .Z/.gz
compressed members are handled). Output: STEP AP214 (or AP242/AP203), mm.
One named, coloured body per layer or feature class, e.g.
"DIELECTRIC_4 (RO4350B, core, 0.5080 mm, Dk 3.66, Df 0.004)".

GEOMETRY
  Board      step profile (island + cut-outs); internal rout paths (--rout).
  Copper     all feature records: lines, arcs, pads (every standard symbol,
             user symbols, resize, any rotation, mirror), surfaces with holes
             and arc segments, stroke text (job font), Code 39 barcodes,
             negative features in drawing order, negative layers (planes).
             Clipped to the board outline.
  Surfaces   each hole is cut from the island that contains it (by position,
             not by record order), so multi-glyph TrueType surfaces keep their
             counters; nested contours with a repeated I/H flag follow their
             winding (non-zero rule).
  Dielectric one body per stackup layer; the resin between the copper of an
             inner layer is added to the adjacent prepreg (else the layer below).
  Mask       conformal: fills between outer copper and covers it by the mask
             thickness; openings from the mask layer.
  Silk       on top of the mask, clipped to mask openings (--no-clip-silk).
  Finish     finish layers (Altium writes CONDUCTIVE_PASTE "surface finish")
             with their own thickness; empty finish layers mean "all exposed
             copper". Colour follows the material (gold, silver, copper).
  Paste      optional (--paste).
  Arcs are rebuilt as exact circles/arcs (cylinder faces), including arcs that
  CAD tools export as polylines (--arc-fit). Every face is checked against its
  2D polygon (area, self and ring intersections) and falls back to straight
  edges where a fitted arc would not be exact.

HOLES (ODB++ drill layers; START_NAME..END_NAME are the drilled layers)
  Through, blind and buried holes span the full thickness of both end layers.
  Laser vias (tool TYPE2=LASER or .via_type=laser) stop on the target land and
  get a plated cup. Slots (line/arc features) and non-round holes are supported.
  Plating: finished size from the tool/symbol; drilled size from DRILL_SIZE
  when larger, else finished + 2 x --plating-thickness. NPTH are unplated.
  Backdrill   ADD_TYPE=BACKDRILL (or name). Drills through the listed layers and
              on into the dielectric until --backdrill-stub short of the next
              copper layer (the must-not-cut layer), unless the job gives
              .backdrill_max_depth / .backdrill_max_stub_drill /
              .backdrill_penetrate_stop_layer. Barrel, fill and cap are removed
              in the drilled part.
  Counterhole ADD_TYPE=COUNTERHOLE (Altium), COUNTERBORE, COUNTERSINK or layer
              names with counterbore/countersink/cbore/csink. Side from
              .drill_layer_direction or the span. ODB++ has no field for the
              type, depth or angle (Altium writes all counterholes of a side to
              one layer with only the diameter), so in this order:
              1. --counterbore / --countersink rules by hole size;
              2. Altium NC drill output: the .DRR report lists depth and angle
                 per counterhole tool (an angle = countersink, its depth is to
                 the cone tip; a depth alone = counterbore) and its Excellon
                 files give the positions. Read from --nc-drill, else from
                 inside the ODB++ input, else beside it (report named like
                 the input, or the only one there);
              3. the layer type or name, then the board flags counter_bore /
                 countersink of ODB++ metadata.xml;
              4. --counterhole-type (counterbore by default) with
                 --countersink-angle (90) or --counterhole-depth; without a
                 depth a counterbore stops ON the last copper layer of the
                 span (--counterhole-depth-mode).
              A countersink cone runs from the counterhole diameter at the
              surface down to the drilled hole.
  Dual dia.   ADD_TYPE=DUAL_DIAMETER: larger plated step, partway.
  Protection  IPC-4761 from .ipc_via_type_top/_bottom, .via_in_pad, .is_capped,
              .cu_base (copper fill), and from document layers whose name says
              fill/cap/plug/tent/cover (Altium FILLING, CAPPING, ...), plus
              --via-protection rules. Fill occupies the finished bore; plugs fill
              --plug-depth of it from the plugged side; caps close the drilled
              hole flush with the land (the land copper thickness already
              includes the cap plating); tenting closes the mask over the via.
              Vias follow the mask layer data (tented if the mask has no
              opening); other holes always cut mask and silk.

THICKNESS (first match wins)
  command line -> matrix/stackup.xml -> .copper_weight (oz if <= 6, else um)
  and .layer_dielectric -> missing dielectrics from .board_thickness
  (copper-to-copper); empty copper layers without .copper_weight are 0 um
  when that makes the stack match .board_thickness (Altium placeholder
  layers) -> defaults: copper 35 um, mask 20 um, silk 20 um,
  plating 25 um, cap 15 um, board 1.6 mm. Mask, silk, finish and paste values
  outside a plausible range (Altium writes 0.00254 mm when a layer has no
  thickness) are replaced by the defaults; the report says which source was used.

CONVENTIONS
  z = 0 at the bottom face of the lowest copper layer; the copper-to-copper
  board spans 0 .. board thickness, bottom mask/silk lie below z = 0.
  ODB++ rotations are clockwise; mirroring flips x after rotation. Step-and-
  repeat ANGLE is applied clockwise and MIRROR flips x; FLIP is not supported.
  Bodies touch but do not overlap. Stacked pieces of one layer (e.g. above and
  below a counterbore floor) are separate touching solids of the same body;
  very large regions are split into touching tiles
  (--max-holes-per-face) because OCCT's STEP writer slows down quadratically
  with the number of holes in one face.

LIMITS
  Flex/rigid-flex areas, coverlay and stiffener layers, components and 3D
  models are not modelled. Rare stencil symbols (home plates, cross, dogbone,
  dpack, d-shape), butterflies, moire and open-corner thermals are drawn from
  their parameter descriptions and flagged in the report. Very large boards
  (tens of thousands of pads and vias) take minutes and several GB of RAM,
  mostly inside the STEP writer.
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import io
import math
import os
import re
import sys
import tarfile
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

try:
    import shapely
    from shapely import affinity
    from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box
    from shapely.geometry.polygon import orient
    from shapely.ops import unary_union
except ImportError:  # pragma: no cover
    sys.exit("shapely >= 2.0 is required:  pip install shapely")

__version__ = "1.1.0"

EPS_Z = 1e-7          # z comparison tolerance (mm)
OZ_TO_MM = 0.035      # 1 oz/ft2 copper, nominal finished thickness
MIL = 0.0254          # mm per mil

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


class Log:
    verbose = 1
    warnings: List[str] = []


def info(msg: str, level: int = 1) -> None:
    if Log.verbose >= level:
        print(msg, flush=True)


def warn(msg: str) -> None:
    if msg not in Log.warnings:
        Log.warnings.append(msg)
        if Log.verbose >= 1:
            print("WARNING: " + msg, flush=True)


# --------------------------------------------------------------------------
# Shapely helpers
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Tessellation (arcs are later recovered as exact circles in the B-rep)
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Affine transforms (2x3 matrices as tuples a, b, d, e, xoff, yoff)
# x' = a*x + b*y + xoff ;  y' = d*x + e*y + yoff   (shapely convention)
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# LZW (.Z, Unix compress) decoder, used by some ODB++ writers
# --------------------------------------------------------------------------


def unlzw(data: bytes) -> bytes:
    if len(data) < 3 or data[0] != 0x1F or data[1] != 0x9D:
        raise ValueError("not a .Z (compress) stream")
    flags = data[2]
    maxbits = flags & 0x1F
    block = bool(flags & 0x80)
    if maxbits < 9 or maxbits > 16:
        raise ValueError("unsupported .Z code size")
    n = len(data)
    pos = mark = 3
    bits, mask = 9, 0x1FF
    end = 256 if block else 255
    prefix = [0] * 65536
    suffix = [0] * 65536
    if pos >= n:
        return b""
    buf = data[pos]
    pos += 1
    if pos >= n:
        raise ValueError("truncated .Z stream")
    buf += data[pos] << 8
    pos += 1
    final = prev = buf & mask
    buf >>= bits
    left = 16 - bits
    if prev > 255:
        raise ValueError("invalid .Z stream")
    out = bytearray([final])
    while True:
        if end >= mask and bits < maxbits:
            rem = (pos - mark) % bits
            if rem:
                pos += bits - rem
            if pos > n:
                break
            buf = left = 0
            mark = pos
            bits += 1
            mask = (mask << 1) | 1
        if pos >= n:
            break
        buf += data[pos] << left
        pos += 1
        left += 8
        if left < bits:
            if pos >= n:
                break
            buf += data[pos] << left
            pos += 1
            left += 8
        code = buf & mask
        buf >>= bits
        left -= bits
        if code == 256 and block:
            rem = (pos - mark) % bits
            if rem:
                pos += bits - rem
            buf = left = 0
            mark = pos
            bits, mask, end = 9, 0x1FF, 255
            continue
        temp = code
        stack = []
        if code > end:
            if code != end + 1 or prev > end:
                raise ValueError("invalid .Z code")
            stack.append(final)
            code = prev
        while code >= 256:
            stack.append(suffix[code])
            code = prefix[code]
        stack.append(code)
        final = code
        if end < mask:
            end += 1
            prefix[end] = prev
            suffix[end] = final
        prev = temp
        stack.reverse()
        out.extend(stack)
    return bytes(out)


# --------------------------------------------------------------------------
# ODB++ product model access (folder, .tgz/.tar, .zip; nested archives)
# --------------------------------------------------------------------------


class OdbJob:
    def __init__(self, path: str):
        self.path = path
        self.files: Dict[str, bytes] = {}      # lower-case rel path -> data (archives)
        self.dir_index: Dict[str, str] = {}    # lower-case rel path -> real path (folders)
        self.root = ""
        if os.path.isdir(path):
            for base, _dirs, fnames in os.walk(path):
                for fn in fnames:
                    full = os.path.join(base, fn)
                    rel = os.path.relpath(full, path).replace(os.sep, "/")
                    self.dir_index[rel.lower()] = full
        else:
            self._load_archive(path, open(path, "rb").read(), depth=0)
        self._find_root()
        self.name = self._job_name()

    # .. archive handling ...................................................
    def _load_archive(self, name: str, data: bytes, depth: int):
        low = name.lower()
        members: Dict[str, bytes] = {}
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for zi in z.infolist():
                    if zi.is_dir():
                        continue
                    members[zi.filename.replace("\\", "/")] = z.read(zi)
        else:
            try:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
                    for m in t.getmembers():
                        if not m.isfile():
                            continue
                        f = t.extractfile(m)
                        if f is not None:
                            members[m.name.replace("\\", "/")] = f.read()
            except tarfile.TarError:
                if low.endswith(".gz") or data[:2] == b"\x1f\x8b":
                    return self._load_archive(name[:-3], gzip.decompress(data), depth + 1)
                raise SystemExit(f"cannot read '{name}': not a folder, zip or tar archive")
        has_matrix = any(re.search(r"(^|/)matrix/matrix(\.z|\.gz)?$", k.lower()) for k in members)
        if not has_matrix and depth < 3:
            inner = [k for k in members if re.search(r"\.(tgz|tar|tar\.gz|zip|gz)$", k.lower())]
            for k in inner:
                try:
                    self._load_archive(k, members[k], depth + 1)
                except SystemExit:
                    continue
                if self.files:
                    return
        for k, v in members.items():
            k = k.lstrip("./").lstrip("/")
            self.files[k.lower()] = v

    def _all_paths(self) -> List[str]:
        return list(self.files.keys()) if self.files else list(self.dir_index.keys())

    def _find_root(self):
        cands = []
        for p in self._all_paths():
            m = re.match(r"^(.*?)(?:^|/)?matrix/matrix(\.z|\.gz)?$", p)
            if m and p.endswith(("matrix/matrix", "matrix/matrix.z", "matrix/matrix.gz")):
                root = p[: p.rfind("matrix/matrix")]
                cands.append(root)
        if not cands:
            raise SystemExit("no ODB++ product model found (matrix/matrix is missing)")
        self.root = min(cands, key=len)

    def _job_name(self) -> str:
        base = os.path.basename(os.path.normpath(self.path))
        for ext in (".tgz", ".tar.gz", ".tar", ".zip", ".gz"):
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        base = re.sub(r"^[0-9a-f]{8}-", "", base)  # upload prefixes
        return base or "odb"

    # .. file access ..........................................................
    def raw(self, rel: str) -> Optional[bytes]:
        rel = (self.root + rel).lower()
        for suffix in ("", ".z", ".gz"):
            key = rel + suffix
            data = None
            if self.files:
                data = self.files.get(key)
            elif key in self.dir_index:
                with open(self.dir_index[key], "rb") as f:
                    data = f.read()
            if data is None:
                continue
            if suffix == ".z" or data[:2] == b"\x1f\x9d":
                data = unlzw(data)
            elif suffix == ".gz" or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data
        return None

    def text(self, rel: str) -> Optional[str]:
        data = self.raw(rel)
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")

    def exists(self, rel: str) -> bool:
        rel = (self.root + rel).lower()
        keys = self.files if self.files else self.dir_index
        return any((rel + s) in keys for s in ("", ".z", ".gz"))

    def listdir(self, rel: str) -> List[str]:
        rel = (self.root + rel).lower().rstrip("/") + "/"
        out = set()
        for p in self._all_paths():
            if p.startswith(rel):
                rest = p[len(rel):]
                if rest:
                    out.add(rest.split("/")[0])
        return sorted(out)


# --------------------------------------------------------------------------
# Structured text, attrlist and feature file parsers
# --------------------------------------------------------------------------


def parse_structured(text: str):
    """KEY=VALUE lines and NAME { ... } blocks -> (top dict, [(NAME, dict)])."""
    top: Dict[str, str] = {}
    arrays: List[Tuple[str, Dict[str, str]]] = []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("{"):
            cur = (line[:-1].strip().upper(), {})
            continue
        if line.startswith("}"):
            if cur is not None:
                arrays.append(cur)
            cur = None
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            (cur[1] if cur is not None else top)[k.strip().upper()] = v.strip()
    return top, arrays


def parse_attrlist(text: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not text:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().lower()
        v = v.strip().strip('"')
        if k == "units":
            out["units"] = v.upper()
            continue
        if v or k not in out:
            out[k] = v
    return out


def attr_float(attrs: Dict[str, str], key: str) -> Optional[float]:
    v = attrs.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ODB++ option attributes are stored in feature files as the option index
OPTION_ATTRS = {
    ".drill": ["plated", "non_plated", "via"],
    ".pad_usage": ["toeprint", "via", "g_fiducial", "l_fiducial", "tooling_hole", "bond_finger"],
    ".via_type": ["drilled", "laser", "photo"],
    ".plated_type": ["standard", "press_fit"],
    ".comp": ["none", "right", "left"],
    ".drill_layer_direction": ["top2bottom", "bottom2top", "any"],
}
TEXT_ATTRS = {".geometry", ".ipc_via_type_top", ".ipc_via_type_bottom", ".bit", ".string",
              ".net_name", ".comp_name", ".pin_name", ".layer_name", ".feature_type"}


@dataclass
class Rec:
    kind: str                 # L P A T B S
    pol: str = "P"
    data: tuple = ()
    attrs: Optional[Dict[str, object]] = None
    polys: Optional[list] = None   # surfaces: [(is_island, [(op, args)...]), ...]


@dataclass
class FeatureFile:
    units: str = "INCH"
    symbols: Dict[int, Tuple[str, Optional[str]]] = field(default_factory=dict)
    attr_names: Dict[int, str] = field(default_factory=dict)
    attr_texts: Dict[int, str] = field(default_factory=dict)
    records: List[Rec] = field(default_factory=list)

    @property
    def cs(self) -> float:          # coordinate scale to mm
        return 25.4 if self.units.startswith("INCH") else 1.0

    def ss(self, sym_unit: Optional[str]) -> float:   # symbol size scale to mm
        if sym_unit == "I":
            return MIL
        if sym_unit == "M":
            return 0.001
        return MIL if self.units.startswith("INCH") else 0.001


_TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\S+")


def _parse_feature_attrs(s: str, ff: FeatureFile) -> Dict[str, object]:
    out: Dict[str, object] = {}
    s = s.strip()
    if not s:
        return out
    for part in s.split(";"):
        for tok in part.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if tok.upper().startswith("ID="):
                continue
            if "=" in tok:
                k, v = tok.split("=", 1)
            else:
                k, v = tok, None
            try:
                name = ff.attr_names.get(int(k), k)
            except ValueError:
                name = k
            name = name.lower()
            if v is None:
                out[name] = True
            elif name in OPTION_ATTRS:
                try:
                    out[name] = OPTION_ATTRS[name][int(v)]
                except (ValueError, IndexError):
                    out[name] = v.lower()
            elif name in TEXT_ATTRS:
                try:
                    out[name] = ff.attr_texts.get(int(v), v)
                except ValueError:
                    out[name] = v
            else:
                out[name] = v
    return out


def parse_features(text: Optional[str], default_units: str = "INCH") -> FeatureFile:
    ff = FeatureFile(units=default_units)
    if not text:
        return ff
    surf: Optional[Rec] = None
    cur_poly = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        c0 = line[0]
        if c0 == "$":
            parts = line[1:].split()
            if len(parts) >= 2:
                unit = parts[2].upper() if len(parts) >= 3 and parts[2].upper() in ("I", "M") else None
                ff.symbols[int(parts[0])] = (parts[1], unit)
            continue
        if c0 == "@":
            parts = line[1:].split(None, 1)
            if len(parts) == 2:
                ff.attr_names[int(parts[0])] = parts[1].strip()
            continue
        if c0 == "&":
            parts = line[1:].split(None, 1)
            ff.attr_texts[int(parts[0])] = parts[1].strip() if len(parts) == 2 else ""
            continue
        up = line.upper()
        if up.startswith("UNITS="):
            ff.units = "MM" if "MM" in up else "INCH"
            continue
        if up.startswith("U ") and len(line) < 12:
            ff.units = "MM" if "MM" in up else "INCH"
            continue
        if up.startswith("ID=") or up.startswith("F "):
            continue
        geo, _, atr = line.partition(";")
        tok = geo.split()
        if not tok:
            continue
        k = tok[0].upper()
        try:
            if k == "OB" and surf is not None:
                cur_poly = (tok[3].upper() != "H" if len(tok) > 3 else True,
                            [("S", (float(tok[1]), float(tok[2])))])
                surf.polys.append(cur_poly)
            elif k == "OS" and cur_poly is not None:
                cur_poly[1].append(("L", (float(tok[1]), float(tok[2]))))
            elif k == "OC" and cur_poly is not None:
                cw = tok[5].upper().startswith("Y") if len(tok) > 5 else True
                cur_poly[1].append(("A", (float(tok[1]), float(tok[2]), float(tok[3]), float(tok[4]), cw)))
            elif k == "OE":
                cur_poly = None
            elif k == "SE":
                if surf is not None:
                    ff.records.append(surf)
                surf = None
                cur_poly = None
            elif k == "S":
                surf = Rec("S", tok[1].upper() if len(tok) > 1 else "P", (), _parse_feature_attrs(atr, ff), [])
            elif k == "L":
                ff.records.append(Rec("L", tok[6].upper(),
                                      (float(tok[1]), float(tok[2]), float(tok[3]), float(tok[4]), int(tok[5])),
                                      _parse_feature_attrs(atr, ff)))
            elif k == "A":
                cw = tok[10].upper().startswith("Y") if len(tok) > 10 else True
                ff.records.append(Rec("A", tok[8].upper(),
                                      (float(tok[1]), float(tok[2]), float(tok[3]), float(tok[4]),
                                       float(tok[5]), float(tok[6]), int(tok[7]), cw),
                                      _parse_feature_attrs(atr, ff)))
            elif k == "P":
                if tok[3] == "-1":
                    sym, resize = int(tok[4]), float(tok[5])
                    i = 6
                else:
                    sym, resize = int(tok[3]), 0.0
                    i = 4
                pol = tok[i].upper()
                orient_def = int(tok[i + 2]) if len(tok) > i + 2 else 0
                angle = float(tok[i + 3]) if orient_def in (8, 9) and len(tok) > i + 3 else 0.0
                ff.records.append(Rec("P", pol, (float(tok[1]), float(tok[2]), sym, resize, orient_def, angle),
                                      _parse_feature_attrs(atr, ff)))
            elif k == "T":
                t = _TOKEN_RE.findall(geo)
                x, y, font, pol, od = float(t[1]), float(t[2]), t[3], t[4].upper(), int(t[5])
                i = 6
                angle = 0.0
                if od in (8, 9):
                    angle = float(t[6])
                    i = 7
                xs, ys, wf = float(t[i]), float(t[i + 1]), float(t[i + 2])
                txt = t[i + 3]
                if txt[:1] in "'\"":
                    txt = txt[1:-1]
                ver = int(t[i + 4]) if len(t) > i + 4 and t[i + 4].isdigit() else 0
                ff.records.append(Rec("T", pol, (x, y, font, od, angle, xs, ys, wf, txt, ver),
                                      _parse_feature_attrs(atr, ff)))
            elif k == "B":
                t = _TOKEN_RE.findall(geo)
                x, y, bc, font, pol, od = float(t[1]), float(t[2]), t[3], t[4], t[5].upper(), int(t[6])
                i = 7
                angle = 0.0
                if od in (8, 9):
                    angle = float(t[7])
                    i = 8
                if t[i].upper() == "E":
                    i += 1
                w, h = float(t[i]), float(t[i + 1])
                fasc, cs, bg, astr, apos = (t[i + 2].upper(), t[i + 3].upper(), t[i + 4].upper(),
                                            t[i + 5].upper(), t[i + 6].upper())
                txt = " ".join(t[i + 7:]) if len(t) > i + 7 else ""
                txt = txt.strip("'\"")
                ff.records.append(Rec("B", pol, (x, y, bc, font, od, angle, w, h, fasc, cs, bg, astr, apos, txt),
                                      _parse_feature_attrs(atr, ff)))
        except (IndexError, ValueError) as exc:
            warn(f"skipped unreadable feature record '{line[:60]}' ({exc})")
    if surf is not None:
        ff.records.append(surf)
    return ff


# --------------------------------------------------------------------------
# Standard symbols (geometry centred on the origin, millimetres)
# --------------------------------------------------------------------------

_N = r"(\d+(?:\.\d*)?|\.\d+)"
_C = r"(?:x(\d+))?"      # optional corner list


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


APPROX_SYMBOLS = {"bfr", "bfs", "oval_h", "s_tho", "rc_tho", "moire", "radhplate", "dshape",
                  "cross", "dogbone", "dpack", "hplate", "rhplate", "fhplate"}


def std_symbol(name: str, k: float):
    """Geometry of an ODB++ standard symbol, or None if the name is not a
    standard symbol. k converts symbol units (mil or micron) to mm."""
    n = name.lower()

    def m(p):
        return re.match("^" + p + "$", n)

    def f(*vals):
        return [float(v) * k for v in vals]

    r = m(rf"r{_N}")
    if r:
        return circle(0.0, 0.0, float(r.group(1)) * k / 2.0)
    r = m(rf"s{_N}")
    if r:
        s, = f(r.group(1))
        return box(-s / 2, -s / 2, s / 2, s / 2)
    r = m(rf"rect{_N}x{_N}xr{_N}{_C}")
    if r:
        w, h, rad = f(r.group(1), r.group(2), r.group(3))
        return _rect_shape(w, h, rad, r.group(4) or "1234")
    r = m(rf"rect{_N}x{_N}xc{_N}{_C}")
    if r:
        w, h, rad = f(r.group(1), r.group(2), r.group(3))
        return _rect_shape(w, h, rad, r.group(4) or "1234", chamfer=True)
    r = m(rf"rect{_N}x{_N}")
    if r:
        w, h = f(r.group(1), r.group(2))
        return box(-w / 2, -h / 2, w / 2, h / 2)
    r = m(rf"oval{_N}x{_N}")
    if r:
        return _oval(*f(r.group(1), r.group(2)))
    r = m(rf"di{_N}x{_N}")
    if r:
        w, h = f(r.group(1), r.group(2))
        return _poly([(w / 2, 0), (0, h / 2), (-w / 2, 0), (0, -h / 2)])
    r = m(rf"oct{_N}x{_N}x{_N}")
    if r:
        w, h, c = f(r.group(1), r.group(2), r.group(3))
        return _rect_shape(w, h, c, "1234", chamfer=True)
    r = m(rf"donut_r{_N}x{_N}")
    if r:
        od, idd = f(r.group(1), r.group(2))
        return gdiff(circle(0, 0, od / 2), circle(0, 0, idd / 2))
    r = m(rf"donut_s{_N}x{_N}xr{_N}{_C}")
    if r:
        od, idd, rad = f(r.group(1), r.group(2), r.group(3))
        c = r.group(4) or "1234"
        return gdiff(_rect_shape(od, od, rad, c), _rect_shape(idd, idd, max(0.0, rad - (od - idd) / 2), c))
    r = m(rf"donut_sr{_N}x{_N}")
    if r:
        od, idd = f(r.group(1), r.group(2))
        return gdiff(box(-od / 2, -od / 2, od / 2, od / 2), circle(0, 0, idd / 2))
    r = m(rf"donut_s{_N}x{_N}")
    if r:
        od, idd = f(r.group(1), r.group(2))
        return gdiff(box(-od / 2, -od / 2, od / 2, od / 2), box(-idd / 2, -idd / 2, idd / 2, idd / 2))
    r = m(rf"donut_rc{_N}x{_N}x{_N}xr{_N}{_C}")
    if r:
        ow, oh, lw, rad = f(r.group(1), r.group(2), r.group(3), r.group(4))
        c = r.group(5) or "1234"
        return gdiff(_rect_shape(ow, oh, rad, c), _rect_shape(ow - 2 * lw, oh - 2 * lw, max(0.0, rad - lw), c))
    r = m(rf"donut_rc{_N}x{_N}x{_N}")
    if r:
        ow, oh, lw = f(r.group(1), r.group(2), r.group(3))
        return gdiff(_rect_shape(ow, oh), _rect_shape(ow - 2 * lw, oh - 2 * lw))
    r = m(rf"donut_o{_N}x{_N}x{_N}")
    if r:
        ow, oh, lw = f(r.group(1), r.group(2), r.group(3))
        return gdiff(_oval(ow, oh), _oval(ow - 2 * lw, oh - 2 * lw))
    r = m(rf"hex_l{_N}x{_N}x{_N}")
    if r:
        w, h, c = f(r.group(1), r.group(2), r.group(3))
        return _poly([(-w / 2, 0), (-w / 2 + c, -h / 2), (w / 2 - c, -h / 2), (w / 2, 0),
                      (w / 2 - c, h / 2), (-w / 2 + c, h / 2)])
    r = m(rf"hex_s{_N}x{_N}x{_N}")
    if r:
        w, h, c = f(r.group(1), r.group(2), r.group(3))
        return _poly([(0, -h / 2), (w / 2, -h / 2 + c), (w / 2, h / 2 - c), (0, h / 2),
                      (-w / 2, h / 2 - c), (-w / 2, -h / 2 + c)])
    r = m(rf"bfr{_N}")
    if r:
        d, = f(r.group(1))
        q = union_all([box(-d, 0, 0, d), box(0, -d, d, 0)])     # quadrants 2 and 4 (assumed)
        return ginter(circle(0, 0, d / 2), q)
    r = m(rf"bfs{_N}")
    if r:
        s, = f(r.group(1))
        return union_all([box(-s / 2, 0, 0, s / 2), box(0, -s / 2, s / 2, 0)])
    r = m(rf"tri{_N}x{_N}")
    if r:
        b, h = f(r.group(1), r.group(2))
        return _poly([(-b / 2, -h / 2), (b / 2, -h / 2), (0, h / 2)])
    r = m(rf"oval_h{_N}x{_N}")
    if r:
        w, h = f(r.group(1), r.group(2))
        if w >= h:
            return union_all([box(-w / 2, -h / 2, w / 2 - h / 2, h / 2), circle(w / 2 - h / 2, 0, h / 2)])
        return union_all([box(-w / 2, -h / 2, w / 2, h / 2 - w / 2), circle(0, h / 2 - w / 2, w / 2)])
    r = m(rf"(thr|ths){_N}x{_N}x{_N}x{_N}x{_N}")
    if r:
        od, idd, ang, ns, gap = float(r.group(2)) * k, float(r.group(3)) * k, float(r.group(4)), \
            float(r.group(5)), float(r.group(6)) * k
        ring = gdiff(circle(0, 0, od / 2), circle(0, 0, idd / 2))
        return _thermal(ring, od / 2, ang, ns, gap, rounded=(r.group(1) == "thr"), lw=(od - idd) / 2)
    r = m(rf"(s_ths|s_tho|s_thr){_N}x{_N}x{_N}x{_N}x{_N}(?:xr{_N}{_C})?")
    if r:
        os_, is_, ang, ns, gap = float(r.group(2)) * k, float(r.group(3)) * k, float(r.group(4)), \
            float(r.group(5)), float(r.group(6)) * k
        rad = float(r.group(7)) * k if r.group(7) else 0.0
        c = r.group(8) or "1234"
        ring = gdiff(_rect_shape(os_, os_, rad, c), _rect_shape(is_, is_, max(0.0, rad - (os_ - is_) / 2), c))
        return _thermal(ring, os_ * 0.75, ang, ns, gap, rounded=(r.group(1) == "s_thr"), lw=(os_ - is_) / 2)
    r = m(rf"sr_ths{_N}x{_N}x{_N}x{_N}x{_N}")
    if r:
        os_, idd, ang, ns, gap = float(r.group(1)) * k, float(r.group(2)) * k, float(r.group(3)), \
            float(r.group(4)), float(r.group(5)) * k
        ring = gdiff(box(-os_ / 2, -os_ / 2, os_ / 2, os_ / 2), circle(0, 0, idd / 2))
        return _thermal(ring, os_ * 0.75, ang, ns, gap)
    r = m(rf"(rc_ths|rc_tho){_N}x{_N}x{_N}x{_N}x{_N}x{_N}(?:xr{_N}{_C})?")
    if r:
        w, h = float(r.group(2)) * k, float(r.group(3)) * k
        ang, ns, gap, ag = float(r.group(4)), float(r.group(5)), float(r.group(6)) * k, float(r.group(7)) * k
        rad = float(r.group(8)) * k if r.group(8) else 0.0
        c = r.group(9) or "1234"
        ring = gdiff(_rect_shape(w, h, rad, c), _rect_shape(w - 2 * ag, h - 2 * ag, max(0.0, rad - ag), c))
        return _thermal(ring, max(w, h) * 0.75, ang, ns, gap)
    r = m(rf"o_ths{_N}x{_N}x{_N}x{_N}x{_N}x{_N}")
    if r:
        ow, oh = float(r.group(1)) * k, float(r.group(2)) * k
        ang, ns, gap, lw = float(r.group(3)), float(r.group(4)), float(r.group(5)) * k, float(r.group(6)) * k
        ring = gdiff(_oval(ow, oh), _oval(ow - 2 * lw, oh - 2 * lw))
        return _thermal(ring, max(ow, oh) * 0.75, ang, ns, gap)
    r = m(rf"oblong_ths{_N}x{_N}x{_N}x{_N}x{_N}x{_N}x([rs])")
    if r:
        ow, oh = float(r.group(1)) * k, float(r.group(2)) * k
        ang, ns, gap, lw = float(r.group(3)), float(r.group(4)), float(r.group(5)) * k, float(r.group(6)) * k
        ring = gdiff(_oval(ow, oh), _oval(ow - 2 * lw, oh - 2 * lw))
        return _thermal(ring, max(ow, oh) * 0.75, ang, ns, gap, rounded=(r.group(7) == "r"), lw=lw)
    r = m(rf"el{_N}x{_N}")
    if r:
        return _ellipse(*f(r.group(1), r.group(2)))
    r = m(rf"moire{_N}x{_N}x{_N}x{_N}x{_N}x{_N}")
    if r:
        rw, rg = float(r.group(1)) * k, float(r.group(2)) * k
        nr, lw, ll, la = int(float(r.group(3))), float(r.group(4)) * k, float(r.group(5)) * k, float(r.group(6))
        parts = []
        for i in range(nr):
            rin = rg + i * (rw + rg)
            parts.append(gdiff(circle(0, 0, rin + rw), circle(0, 0, rin)))
        for a in (la, la + 90.0):
            t = math.radians(a)
            dx, dy = math.cos(t) * ll / 2, math.sin(t) * ll / 2
            parts.append(stadium(-dx, -dy, dx, dy, lw / 2))
        return union_all(parts)
    r = m(rf"hole{_N}x([pnv])x{_N}x{_N}")
    if r:
        return circle(0, 0, float(r.group(1)) * k / 2)
    if m(r"null\d*"):
        return EMPTY
    r = m(rf"hplate{_N}x{_N}x{_N}(?:x{_N}x{_N})?")
    if r:
        w, h, c = f(r.group(1), r.group(2), r.group(3))
        return _poly([(-w / 2, -h / 2), (w / 2 - c, -h / 2), (w / 2, 0), (w / 2 - c, h / 2), (-w / 2, h / 2)])
    r = m(rf"rhplate{_N}x{_N}x{_N}(?:x{_N}x{_N})?")
    if r:
        w, h, c = f(r.group(1), r.group(2), r.group(3))
        return _poly([(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2 - c, 0), (w / 2, h / 2), (-w / 2, h / 2)])
    r = m(rf"fhplate{_N}x{_N}x{_N}x{_N}(?:x{_N}x{_N})?")
    if r:
        w, h, vc, hc = f(r.group(1), r.group(2), r.group(3), r.group(4))
        return _poly([(-w / 2, -h / 2), (w / 2 - hc, -h / 2), (w / 2, -h / 2 + vc), (w / 2, h / 2 - vc),
                      (w / 2 - hc, h / 2), (-w / 2, h / 2)])
    r = m(rf"radhplate{_N}x{_N}x{_N}(?:x{_N})?")
    if r:
        w, h, ms = f(r.group(1), r.group(2), r.group(3))
        sag = max(1e-6, (w - ms) / 2.0)
        R = (sag ** 2 + (h / 2) ** 2) / (2 * sag)
        cx = w / 2 + R - sag
        a = math.asin(min(1.0, (h / 2) / R))
        arc = arc_pts(cx, 0.0, R, math.pi - a, math.pi + a)[::-1]
        return _poly([(-w / 2, -h / 2)] + [tuple(p) for p in arc] + [(-w / 2, h / 2)])
    r = m(rf"dshape{_N}x{_N}x{_N}(?:x{_N})?")
    if r:
        w, h, rel = f(r.group(1), r.group(2), r.group(3))
        rel = max(1e-6, min(rel, w))
        R = (rel ** 2 + (h / 2) ** 2) / (2 * rel)
        cx = w / 2 - R
        a = math.asin(min(1.0, (h / 2) / R))
        arc = arc_pts(cx, 0.0, R, -a, a)
        return _poly([(-w / 2, -h / 2)] + [tuple(p) for p in arc] + [(-w / 2, h / 2)])
    r = m(rf"cross{_N}x{_N}x{_N}x{_N}x{_N}x{_N}x?([rs])(?:x?{_N})?")
    if r:
        w, h, hs, vs = f(r.group(1), r.group(2), r.group(3), r.group(4))
        hc, vc = float(r.group(5)), float(r.group(6))
        yc = -h / 2 + h * vc / 100.0
        xc = -w / 2 + w * hc / 100.0
        if r.group(7) == "r":
            return union_all([stadium(-w / 2 + hs / 2, yc, w / 2 - hs / 2, yc, hs / 2),
                              stadium(xc, -h / 2 + vs / 2, xc, h / 2 - vs / 2, vs / 2)])
        return union_all([box(-w / 2, yc - hs / 2, w / 2, yc + hs / 2), box(xc - vs / 2, -h / 2, xc + vs / 2, h / 2)])
    r = m(rf"dogbone{_N}x{_N}x{_N}x{_N}x{_N}x?([rs])(?:x{_N})?")
    if r:
        w, h, hs, vs = f(r.group(1), r.group(2), r.group(3), r.group(4))
        return union_all([box(-w / 2, -hs / 2, w / 2, hs / 2), box(-w / 2, -h / 2, -w / 2 + vs, h / 2),
                          box(w / 2 - vs, -h / 2, w / 2, h / 2)])
    r = m(rf"dpack{_N}x{_N}x{_N}x{_N}x(\d+)x(\d+)(?:x{_N})?")
    if r:
        w, h, hg, vg = f(r.group(1), r.group(2), r.group(3), r.group(4))
        rows, cols = max(1, int(r.group(5))), max(1, int(r.group(6)))
        ra = float(r.group(7)) * k if r.group(7) else 0.0
        pw = (w - (cols - 1) * vg) / cols
        ph = (h - (rows - 1) * hg) / rows
        parts = []
        for i in range(rows):
            for j in range(cols):
                cx = -w / 2 + pw / 2 + j * (pw + vg)
                cy = -h / 2 + ph / 2 + i * (ph + hg)
                parts.append(affinity.translate(_rect_shape(pw, ph, ra), cx, cy))
        return union_all(parts)
    return None


def symbol_family(name: str) -> str:
    m = re.match(r"^([a-z_]+?)(?=\d|\.)", name.lower())
    return m.group(1) if m else name.lower()


# --------------------------------------------------------------------------
# Stroke font and barcodes
# --------------------------------------------------------------------------


class StrokeFont:
    def __init__(self, text: str):
        self.xsize = self.ysize = 0.302
        self.offset = 0.0
        self.chars: Dict[str, list] = {}
        cur = None
        for raw in text.splitlines():
            t = raw.split()
            if not t:
                continue
            key = t[0].upper()
            if key == "XSIZE":
                self.xsize = float(t[1])
            elif key == "YSIZE":
                self.ysize = float(t[1])
            elif key == "OFFSET":
                self.offset = float(t[1])
            elif key == "CHAR":
                ch = raw.strip()[5:6] if len(raw.strip()) > 5 else " "
                cur = []
                self.chars[ch] = cur
            elif key == "ECHAR":
                cur = None
            elif key == "LINE" and cur is not None and len(t) >= 8:
                cur.append((float(t[1]), float(t[2]), float(t[3]), float(t[4]), t[5].upper(), t[6].upper(),
                            float(t[7])))
        xs = [v for lines in self.chars.values() for ln in lines for v in (ln[0], ln[2])]
        ys = [v for lines in self.chars.values() for ln in lines for v in (ln[1], ln[3])]
        self.minx = min(xs) if xs else 0.0
        self.miny = min(ys) if ys else 0.0


CODE39 = {
    "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn", "4": "nnnwwnnnw",
    "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw", "8": "wnnwnnwnn", "9": "nnwwnnwnn",
    "A": "wnnnnwnnw", "B": "nnwnnwnnw", "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn",
    "F": "nnwnwwnnn", "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww", "O": "wnnnwnnwn",
    "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn", "S": "nnwnnnwwn", "T": "nnnnwnwwn",
    "U": "wwnnnnnnw", "V": "nwwnnnnnw", "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn",
    "Z": "nwwnwnnnn", "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn", "*": "nwnnwnwnn",
    "$": "nwnwnwnnn", "/": "nwnwnnnwn", "+": "nwnnnwnwn", "%": "nnnwnwnwn",
}
CODE39_ORDER = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%"


# --------------------------------------------------------------------------
# Feature renderer
# --------------------------------------------------------------------------


class Renderer:
    def __init__(self, job: OdbJob, default_units: str):
        self.job = job
        self.units = default_units
        self.user_syms: Dict[str, object] = {}
        self.fonts: Dict[str, Optional[StrokeFont]] = {}
        self.sym_cache: Dict[tuple, object] = {}
        self.approx_used = set()
        self.surf_stats: Counter = Counter()
        self._busy = set()

    # .. symbols ...............................................................
    def symbol(self, name: str, unit: Optional[str], ff: FeatureFile):
        k = ff.ss(unit)
        key = (name, k)
        g = self.sym_cache.get(key)
        if g is not None:
            return g
        g = std_symbol(name, k)
        if g is not None:
            fam = symbol_family(name)
            if fam in APPROX_SYMBOLS:
                self.approx_used.add(fam)
        else:
            g = self.user_symbol(name)
            if g is None:
                warn(f"unknown symbol '{name}' ignored")
                g = EMPTY
        self.sym_cache[key] = g
        return g

    def user_symbol(self, name: str):
        key = name.lower()
        if key in self.user_syms:
            return self.user_syms[key]
        if key in self._busy:
            warn(f"recursive user symbol '{name}'")
            return EMPTY
        txt = self.job.text(f"symbols/{key}/features")
        if txt is None:
            self.user_syms[key] = None
            return None
        self._busy.add(key)
        g = self.render(parse_features(txt, self.units))
        self._busy.discard(key)
        self.user_syms[key] = g
        return g

    def font(self, name: str) -> Optional[StrokeFont]:
        key = (name or "standard").lower()
        if key not in self.fonts:
            txt = self.job.text(f"fonts/{key}") or (self.job.text("fonts/standard") if key != "standard" else None)
            self.fonts[key] = StrokeFont(txt) if txt else None
        return self.fonts[key]

    # .. strokes ...............................................................
    @staticmethod
    def _stroke(x0, y0, x1, y1, w, shape="R"):
        if shape == "S":
            return square_line(x0, y0, x1, y1, w)
        return stadium(x0, y0, x1, y1, w / 2.0)

    def _sym_kind(self, ff: FeatureFile, sym: int):
        name, unit = ff.symbols.get(sym, ("", None))
        n = name.lower()
        r = re.match(rf"^r{_N}$", n)
        if r:
            return "r", float(r.group(1)) * ff.ss(unit), name, unit
        r = re.match(rf"^s{_N}$", n)
        if r:
            return "s", float(r.group(1)) * ff.ss(unit), name, unit
        return "x", 0.0, name, unit

    def _sweep_symbol(self, pts: np.ndarray, ff, name, unit):
        """Line or arc drawn with a non-round symbol: union of convex hulls of the
        symbol placed at consecutive points (exact for convex symbols)."""
        g = self.symbol(name, unit, ff)
        if g.is_empty:
            return EMPTY
        parts = []
        hull = g.convex_hull
        for i in range(len(pts) - 1):
            a = affinity.translate(hull, pts[i][0], pts[i][1])
            b = affinity.translate(hull, pts[i + 1][0], pts[i + 1][1])
            parts.append(union_all([a, b]).convex_hull)
        if not parts:
            parts.append(affinity.translate(g, pts[0][0], pts[0][1]))
        if abs(hull.area - g.area) > 1e-6 * max(hull.area, 1e-12):
            warn(f"line/arc drawn with non-convex symbol '{name}' approximated by its convex hull")
        return union_all(parts)

    # .. feature geometry ........................................................
    def feature_geom(self, rec: Rec, ff: FeatureFile):
        cs = ff.cs
        k = rec.kind
        if k == "P":
            x, y, sym, resize, od, ang = rec.data
            x, y = x * cs, y * cs
            kind, size, name, unit = self._sym_kind(ff, sym)
            rs = resize * ff.ss(unit) if resize else 0.0
            if kind == "r":
                return circle(x, y, (size + rs) / 2.0)
            g = self.symbol(name, unit, ff)
            if g.is_empty:
                return g
            if rs:
                g = polys_only(g.buffer(rs / 2.0, quad_segs=16))
            M = m_compose(m_translate(x, y), orient_matrix(od, ang))
            return m_apply(M, g)
        if k == "L":
            xs, ys, xe, ye, sym = rec.data
            xs, ys, xe, ye = xs * cs, ys * cs, xe * cs, ye * cs
            kind, size, name, unit = self._sym_kind(ff, sym)
            if kind == "r":
                return stadium(xs, ys, xe, ye, size / 2.0)
            if kind == "s":
                return square_line(xs, ys, xe, ye, size)
            return self._sweep_symbol(np.array([[xs, ys], [xe, ye]]), ff, name, unit)
        if k == "A":
            xs, ys, xe, ye, xc, yc, sym, cw = rec.data
            xs, ys, xe, ye, xc, yc = xs * cs, ys * cs, xe * cs, ye * cs, xc * cs, yc * cs
            kind, size, name, unit = self._sym_kind(ff, sym)
            if kind == "r":
                return thick_arc(xs, ys, xe, ye, xc, yc, cw, size / 2.0)
            cl = arc_centerline(xs, ys, xe, ye, xc, yc, cw)
            if kind == "s":
                return union_all([square_line(cl[i][0], cl[i][1], cl[i + 1][0], cl[i + 1][1], size)
                                  for i in range(len(cl) - 1)])
            return self._sweep_symbol(cl, ff, name, unit)
        if k == "S":
            return self.surface_geom(rec, cs, self.surf_stats)
        if k == "T":
            return self.text_geom(rec, ff)
        if k == "B":
            return self.barcode_geom(rec, ff)
        return EMPTY

    @staticmethod
    def contour_points(ops, cs: float) -> List[tuple]:
        x0, y0 = ops[0][1]
        cur = (x0 * cs, y0 * cs)
        pts = [cur]
        for op, a in ops[1:]:
            if op == "L":
                cur = (a[0] * cs, a[1] * cs)
                pts.append(cur)
            else:
                xe, ye, xc, yc, cw = a
                arc = arc_centerline(cur[0], cur[1], xe * cs, ye * cs, xc * cs, yc * cs, cw)
                pts.extend(map(tuple, arc[1:]))
                cur = (xe * cs, ye * cs)
        return pts

    @staticmethod
    def surface_geom(rec: Rec, cs: float, stats: Optional[Counter] = None):
        """Islands minus holes. Each hole is cut from the island that contains
        it, found by position rather than by record order: writers do not all
        list a hole straight after its island (Altium writes a TrueType string
        as one surface with every glyph outline first and every counter after
        them, so pairing by order filled the counters of all glyphs but the
        last). A contour nested directly in one of the same type but wound the
        other way is read as the opposite type (non-zero rule; ODB++ islands
        are clockwise, holes counter-clockwise)."""
        polys, flags, cw = [], [], []
        for is_island, ops in rec.polys or []:
            if not ops:
                continue
            pts = Renderer.contour_points(ops, cs)
            if len(pts) < 3:
                continue
            poly = make_valid(Polygon(pts))
            if poly.is_empty:
                continue
            P = np.asarray(pts)
            sa = float(np.dot(P[:, 0], np.roll(P[:, 1], -1)) - np.dot(np.roll(P[:, 0], -1), P[:, 1]))
            polys.append(poly)
            flags.append(bool(is_island))
            cw.append(sa < 0.0)
        n = len(polys)
        if n == 0:
            return EMPTY
        if n == 1 and flags[0]:
            return polys[0]
        parent, area = contour_parents(polys)
        # effective type, parents first (a parent always has the larger area)
        eff = list(flags)
        for i in sorted(range(n), key=lambda k: -area[k]):
            p = parent[i]
            if p >= 0 and flags[i] == eff[p] and cw[i] != cw[p]:
                eff[i] = not eff[p]
                if stats is not None:
                    stats["contours read by winding"] += 1
        cuts: Dict[int, list] = defaultdict(list)
        orphans = []
        last_island = -1            # what pairing by record order would have used
        for i in range(n):
            if flags[i]:
                last_island = i
            if eff[i]:
                continue
            p = parent[i]
            while p >= 0 and not eff[p]:
                p = parent[p]
            if p >= 0:
                cuts[p].append(polys[i])
            else:
                orphans.append(polys[i])
            if stats is not None and not flags[i] and 0 <= last_island != p and p >= 0:
                stats["holes placed by position"] += 1
        islands = [i for i in range(n) if eff[i]]
        if not islands:
            if stats is not None:
                stats["hole-only surfaces ignored"] += 1
            return EMPTY
        g = union_all(gdiff(polys[i], union_all(cuts[i])) if i in cuts else polys[i] for i in islands)
        if orphans:
            if stats is not None:
                stats["holes outside every island"] += len(orphans)
            g = gdiff(g, union_all(orphans))
        return g

    def text_geom(self, rec: Rec, ff: FeatureFile):
        x, y, font, od, ang, xs, ys, wf, txt, ver = rec.data
        cs = ff.cs
        F = self.font(font)
        xs_mm, ys_mm = xs * cs, ys * cs
        width = wf * 12 * MIL if wf > 0 else 0.0
        if F is None:
            g = _text_fallback(txt, ys_mm)
            if g is None:
                warn("text features skipped: no stroke font in the job and matplotlib unavailable")
                return EMPTY
            warn("job has no fonts/standard; text rendered with a fallback outline font")
        else:
            sx = xs_mm / F.xsize if F.xsize else 1.0
            sy = ys_mm / F.ysize if F.ysize else 1.0
            adv = xs_mm + F.offset * sx
            parts_p, parts_n = [], []
            for i, ch in enumerate(txt):
                lines = F.chars.get(ch)
                if lines is None:
                    lines = F.chars.get(ch.upper(), [])
                ox = i * adv
                for (gx0, gy0, gx1, gy1, pol, shape, gw) in lines:
                    w = width if width > 0 else gw * sy
                    seg = self._stroke(ox + (gx0 - F.minx) * sx, (gy0 - F.miny) * sy,
                                       ox + (gx1 - F.minx) * sx, (gy1 - F.miny) * sy, w, shape)
                    (parts_n if pol == "N" else parts_p).append(seg)
            g = gdiff(union_all(parts_p), union_all(parts_n))
        M = m_compose(m_translate(x * cs, y * cs), orient_matrix(od, ang))
        return m_apply(M, g)

    def barcode_geom(self, rec: Rec, ff: FeatureFile):
        x, y, bc, font, od, ang, w, h, fasc, csum, bg, astr, apos, txt = rec.data
        cs = ff.cs
        w_mm, h_mm = w * cs, h * cs
        if w_mm <= 0 or h_mm <= 0:
            return EMPTY
        if bc.upper() not in ("UPC39", "CODE39", "C39"):
            warn(f"barcode type '{bc}' drawn as Code 39")
        data = txt.upper()
        if csum.startswith("Y"):
            tot = sum(CODE39_ORDER.index(c) for c in data if c in CODE39_ORDER)
            data += CODE39_ORDER[tot % 43]
        seq = "*" + "".join(c for c in data if c in CODE39) + "*"
        wide = 3.0 * w_mm
        bars = []
        cx = 0.0
        for c in seq:
            pat = CODE39[c]
            for j, e in enumerate(pat):
                ew = wide if e == "w" else w_mm
                if j % 2 == 0:
                    bars.append(box(cx, 0.0, cx + ew, h_mm))
                cx += ew
            cx += w_mm  # inter-character gap
        g = union_all(bars)
        total_w = cx - w_mm
        if bg.startswith("Y"):
            q = 10 * w_mm
            g = gdiff(box(-q, -q, total_w + q, h_mm + q), g)
        if astr.startswith("Y") and txt:
            th = max(h_mm * 0.15, 0.5)
            wf = th * 0.12 / (12 * MIL)
            fake = Rec("T", "P", (0.0, 0.0, font, 0, 0.0, th * 0.8 / cs, th / cs, wf, txt, 0))
            tg = self.text_geom(fake, ff)
            dy = (h_mm + 0.3 * th) if apos.startswith("T") else -(1.3 * th)
            g = union_all([g, affinity.translate(tg, 0.0, dy)])
        self.approx_used.add("barcode")
        M = m_compose(m_translate(x * cs, y * cs), orient_matrix(od, ang))
        return m_apply(M, g)

    # .. whole feature files ....................................................
    def render(self, ff: FeatureFile, select=None):
        """Render records honouring polarity order (later negative features
        erase earlier positive ones)."""
        acc = EMPTY
        batch: list = []
        bpol = None
        for rec in ff.records:
            if select is not None and not select(rec):
                continue
            g = self.feature_geom(rec, ff)
            if g is None or g.is_empty:
                continue
            pol = "N" if rec.pol.startswith("N") else "P"
            if bpol is not None and pol != bpol:
                acc = _apply_batch(acc, batch, bpol)
                batch = []
            batch.append(g)
            bpol = pol
        if batch:
            acc = _apply_batch(acc, batch, bpol)
        return acc


def report_surfaces(stats: Counter) -> None:
    n = stats.get("holes placed by position", 0)
    if n:
        info(f"Surfaces: {n} hole contour(s) listed after a different island were cut from the island "
             f"that contains them (e.g. Altium TrueType strings)")
    n = stats.get("contours read by winding", 0)
    if n:
        info(f"Surfaces: {n} nested contour(s) whose I/H flag repeats the enclosing contour's type "
             f"were read by winding direction")
    n = stats.get("hole-only surfaces ignored", 0)
    if n:
        warn(f"{n} surface feature(s) contain only hole contours and were ignored")
    n = stats.get("holes outside every island", 0)
    if n:
        warn(f"{n} hole contour(s) lie outside every island of their surface (subtracted from the surface)")


def _apply_batch(acc, batch, pol):
    u = union_all(batch)
    if pol == "P":
        return union_all([acc, u])
    return gdiff(acc, u)


def _text_fallback(txt: str, height: float):
    try:
        from matplotlib.textpath import TextPath
        from matplotlib.font_manager import FontProperties
    except Exception:
        return None
    try:
        tp = TextPath((0, 0), txt, size=height * 1.4, prop=FontProperties(family="DejaVu Sans"))
        polys = [Polygon(p) for p in tp.to_polygons() if len(p) >= 3]
        acc = EMPTY
        for p in sorted(polys, key=lambda q: -q.area):
            p = make_valid(p)
            acc = gdiff(acc, p) if acc.contains(p) else union_all([acc, p])
        return acc
    except Exception:
        return None


# --------------------------------------------------------------------------
# Matrix, steps, step-and-repeat
# --------------------------------------------------------------------------

COPPER_TYPES = {"SIGNAL", "POWER_GROUND", "MIXED"}


@dataclass
class MLayer:
    row: int
    name: str
    type: str
    context: str
    polarity: str
    add_type: str
    start: str
    end: str
    dielectric_type: str
    dielectric_name: str
    attrs: Dict[str, str] = field(default_factory=dict)
    kind: str = ""
    side: str = ""
    t: float = 0.0
    t_src: str = ""
    z0: float = 0.0
    z1: float = 0.0
    material: str = ""
    dk: Optional[float] = None
    df: Optional[float] = None
    implicit: bool = False

    @property
    def key(self) -> str:
        return self.name.lower()


def layer_kind(L: MLayer) -> str:
    t = L.type
    if t in COPPER_TYPES:
        return "copper"
    return {"DIELECTRIC": "dielectric", "SOLDER_MASK": "mask", "SILK_SCREEN": "silk",
            "SOLDER_PASTE": "paste", "CONDUCTIVE_PASTE": "finish", "DRILL": "drill",
            "ROUT": "rout"}.get(t, "doc")


def name_tokens(name: str) -> set:
    return set(t for t in re.split(r"[^a-z0-9]+", name.lower()) if t)


def load_matrix(job: OdbJob):
    txt = job.text("matrix/matrix")
    if txt is None:
        raise SystemExit("matrix/matrix not readable")
    _top, arrays = parse_structured(txt)
    steps = [d.get("NAME", "") for n, d in arrays if n == "STEP"]
    layers = []
    for n, d in arrays:
        if n != "LAYER":
            continue
        try:
            row = int(float(d.get("ROW", "0") or 0))
        except ValueError:
            row = 0
        L = MLayer(row=row, name=d.get("NAME", ""), type=d.get("TYPE", "").upper(),
                   context=d.get("CONTEXT", "BOARD").upper(), polarity=d.get("POLARITY", "POSITIVE").upper(),
                   add_type=d.get("ADD_TYPE", "").upper(), start=d.get("START_NAME", ""), end=d.get("END_NAME", ""),
                   dielectric_type=d.get("DIELECTRIC_TYPE", "").upper(), dielectric_name=d.get("DIELECTRIC_NAME", ""))
        L.kind = layer_kind(L)
        layers.append(L)
    layers.sort(key=lambda L: L.row)
    return steps, layers


def step_instances(job: OdbJob, step: str, M=IDENT, depth=0, units="INCH"):
    """All (step, transform) instances reachable from 'step' via step-and-repeat.
    ANGLE is applied clockwise (ODB++ convention); MIRROR flips x -> -x."""
    out = [(step.lower(), M)]
    if depth > 8:
        warn("step-and-repeat nesting deeper than 8 levels ignored")
        return out
    txt = job.text(f"steps/{step.lower()}/stephdr")
    if not txt:
        return out
    top, arrays = parse_structured(txt)
    su = top.get("UNITS", units).upper()
    sc = 25.4 if su.startswith("INCH") else 1.0
    for n, d in arrays:
        if n != "STEP-REPEAT":
            continue
        child = d.get("NAME", "").lower()
        if not child:
            continue
        ctxt = job.text(f"steps/{child}/stephdr") or ""
        ctop, _ = parse_structured(ctxt)
        csc = 25.4 if ctop.get("UNITS", su).upper().startswith("INCH") else 1.0
        dx0 = float(ctop.get("X_DATUM", 0) or 0) * csc
        dy0 = float(ctop.get("Y_DATUM", 0) or 0) * csc
        X, Y = float(d.get("X", 0) or 0) * sc, float(d.get("Y", 0) or 0) * sc
        DX, DY = float(d.get("DX", 0) or 0) * sc, float(d.get("DY", 0) or 0) * sc
        NX, NY = int(float(d.get("NX", 1) or 1)), int(float(d.get("NY", 1) or 1))
        ang = float(d.get("ANGLE", 0) or 0)
        mirror = d.get("MIRROR", "NO").upper().startswith("Y")
        if d.get("FLIP", "NO").upper().startswith("Y"):
            warn(f"step-repeat FLIP of '{child}' (layer order reversal) is not supported; placed unflipped")
        for i in range(max(1, NX)):
            for j in range(max(1, NY)):
                loc = m_translate(-dx0, -dy0)
                if mirror:
                    loc = m_compose(M_MIRROR_X, loc)
                loc = m_compose(m_rot_cw(ang), loc)
                loc = m_compose(m_translate(X + i * DX, Y + j * DY), loc)
                out += step_instances(job, child, m_compose(M, loc), depth + 1, units)
    return out


# --------------------------------------------------------------------------
# Thickness helpers
# --------------------------------------------------------------------------


def _len_mm(v: float, units: str, kind: str) -> float:
    """Convert attribute values to mm. kind 'inch_mm' (inch or mm) or
    'mil_micron' (mil or micron)."""
    u = (units or "MM").upper()
    if kind == "inch_mm":
        if u.startswith("MM"):
            return v / 1000.0 if v > 20.0 else v
        return v * MIL if v > 1.0 else v * 25.4
    if u.startswith("MM"):
        return v / 1000.0
    return v * MIL


def copper_from_attrs(attrs) -> Tuple[Optional[float], str]:
    v = attr_float(attrs, ".copper_weight")
    if v is None or v <= 0:
        return None, ""
    if v <= 6.0:
        return v * OZ_TO_MM, f".copper_weight={v:g} (oz)"
    return v / 1000.0, f".copper_weight={v:g} (um)"


def layer_dielectric_from_attrs(attrs) -> Optional[float]:
    v = attr_float(attrs, ".layer_dielectric")
    if v is None or v <= 0:
        return None
    return _len_mm(v, attrs.get("units", "MM"), "inch_mm")


def parse_stackup_xml(text: str) -> Dict[str, float]:
    """matrix/stackup.xml (ODB++ 8.1): layer name -> thickness (mm)."""
    out: Dict[str, float] = {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        warn("matrix/stackup.xml could not be parsed")
        return out
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]

    def to_mm(val, unit):
        u = (unit or root.get("DefaultUnits") or "MIL").upper()
        v = float(val)
        return {"MIL": v * MIL, "INCH": v * 25.4, "MM": v, "MICRON": v / 1000.0, "UM": v / 1000.0}.get(u, v * MIL)

    for section in [s for s in root if s.tag in ("EdaData", "SupplierData")][:1] or [root]:
        specs: Dict[str, float] = {}
        for spec in section.iter("Spec"):
            t = None
            for prop in spec.iter("Property"):
                val = prop.get("Finished_Thickness") or prop.get("Thickness")
                if val:
                    try:
                        t = to_mm(val, prop.get("Units_Thickness"))
                        break
                    except ValueError:
                        pass
            if t is None:
                for dt in spec.iter("Default_Thickness"):
                    val = dt.get("Finished_Thickness") or dt.get("Thickness")
                    if val:
                        try:
                            t = to_mm(val, dt.get("Units"))
                            break
                        except ValueError:
                            pass
            if t is not None:
                specs[spec.get("SpecName", "")] = t
        for lay in section.iter("Layer"):
            nm = lay.get("LayerName")
            if not nm:
                continue
            for sr in lay.iter("SpecRef"):
                t = specs.get(sr.get("MaterialSpecName", ""))
                if t:
                    out[nm.lower()] = t
                    break
    return out


DEFAULTS = dict(copper=0.035, mask=0.020, silk=0.020, finish=0.0, paste=0.120, plating=0.025,
                cap=0.015, board=1.6)
VALID_RANGE = dict(mask=(0.005, 0.100), silk=(0.005, 0.100), finish=(0.00005, 0.050), paste=(0.050, 0.300),
                   copper=(0.001, 0.500), dielectric=(0.005, 10.0))


# --------------------------------------------------------------------------
# Stackup
# --------------------------------------------------------------------------


class Stackup:
    def __init__(self, layers: List[MLayer], job_attrs, step_attrs, xml_t: Dict[str, float], opts,
                 empty_cu: Optional[set] = None):
        self.opts = opts
        board = [L for L in layers if L.context == "BOARD"]
        self.stack: List[MLayer] = []
        self.coats: Dict[str, Dict[str, List[MLayer]]] = {"top": defaultdict(list), "bottom": defaultdict(list)}
        self.notes: List[str] = []
        over = {k.lower(): v for k, v in (opts.layer_thickness or {}).items()}

        core = [L for L in board if L.kind in ("copper", "dielectric")]
        coppers = [L for L in core if L.kind == "copper"]
        if not coppers and not core:
            raise SystemExit("matrix has no copper or dielectric board layers")

        # implicit dielectrics between adjacent copper layers
        stack: List[MLayer] = []
        for L in core:
            if stack and stack[-1].kind == "copper" and L.kind == "copper":
                D = MLayer(row=L.row, name=f"DIELECTRIC_{stack[-1].name}_{L.name}", type="DIELECTRIC",
                           context="BOARD", polarity="POSITIVE", add_type="", start="", end="",
                           dielectric_type="", dielectric_name="", kind="dielectric", implicit=True)
                stack.append(D)
            stack.append(L)
        if len([L for L in stack if L.kind == "dielectric"]) == 0:
            only = stack[0]
            below = not ("bot" in name_tokens(only.name) or "bottom" in name_tokens(only.name))
            D = MLayer(row=only.row + (1 if below else -1), name="DIELECTRIC_CORE", type="DIELECTRIC",
                       context="BOARD", polarity="POSITIVE", add_type="", start="", end="",
                       dielectric_type="", dielectric_name="", kind="dielectric", implicit=True)
            stack = stack + [D] if below else [D] + stack
        self.stack = stack

        # thickness of copper / dielectric layers
        unknown = []
        for L in stack:
            if L.key in over:
                L.t, L.t_src = over[L.key], "command line"
                continue
            if L.key in xml_t:
                L.t, L.t_src = xml_t[L.key], "stackup.xml"
                continue
            if L.kind == "copper":
                t, src = copper_from_attrs(L.attrs)
                if t is None:
                    t, src = opts.copper_thickness or DEFAULTS["copper"], "default"
                L.t, L.t_src = t, src
            else:
                t = layer_dielectric_from_attrs(L.attrs)
                lo, hi = VALID_RANGE["dielectric"]
                if t is not None and lo <= t <= hi:
                    L.t, L.t_src = t, ".layer_dielectric"
                else:
                    unknown.append(L)
            if L.kind == "dielectric":
                L.dk = attr_float(L.attrs, ".dielectric_constant")
                L.df = attr_float(L.attrs, ".loss_tangent")
                L.material = L.dielectric_name or L.attrs.get(".comment", "")

        # board thickness: command line > attributes > default
        t_attr, t_attr_src = None, ""
        for src_name, attrs in (("job", job_attrs), ("step", step_attrs)):
            v = attr_float(attrs, ".board_thickness")
            if v and v > 0:
                t_attr = _len_mm(v, attrs.get("units", "MM"), "inch_mm")
                t_attr_src = f".board_thickness ({src_name})"
                break
        self.board_attr = t_attr
        target = opts.board_thickness or t_attr
        # Copper layers with no thickness data and no features are placeholders
        # when leaving them out (t = 0) makes the stack match the board
        # thickness (Altium omits .copper_weight for zero-thickness layers).
        zero = [L for L in stack if L.kind == "copper" and L.t_src == "default" and L.key in (empty_cu or ())]
        if zero and target and not unknown:
            full = sum(L.t for L in stack)
            cut = sum(L.t for L in zero)
            tol = max(0.005, 0.002 * target)
            if abs(full - cut - target) <= tol < abs(full - target):
                ref = "--board-thickness" if opts.board_thickness else t_attr_src
                each = zero[0].t
                for L in zero:
                    L.t, L.t_src = 0.0, f"empty, no .copper_weight (0 matches {ref})"
                self.notes.append(f"{len(zero)} empty copper layer(s) without .copper_weight taken as 0 um "
                                  f"(at {each * 1000:g} um each the stack would be {full:.4f} mm, "
                                  f"not {target:.4f} mm)")
        known = sum(L.t for L in stack if L not in unknown)
        if unknown:
            tgt = target if target else DEFAULTS["board"]
            rem = tgt - known
            if rem <= 0.01 * len(unknown):
                warn(f"board thickness {tgt:.4f} mm leaves no room for {len(unknown)} dielectric(s); using 0.1 mm each")
                each = 0.1
            else:
                each = rem / len(unknown)
            src = ("inferred from " + ("--board-thickness" if opts.board_thickness else t_attr_src)
                   if target else f"inferred from default {DEFAULTS['board']} mm board")
            for L in unknown:
                L.t, L.t_src = each, src
        elif opts.board_thickness:
            diel = [L for L in stack if L.kind == "dielectric" and L.t_src != "command line"]
            cu = sum(L.t for L in stack if L not in diel)
            dsum = sum(L.t for L in diel)
            if diel and dsum > 0 and opts.board_thickness > cu:
                f = (opts.board_thickness - cu) / dsum
                for L in diel:
                    L.t *= f
                    L.t_src += f" (scaled x{f:.4f} to --board-thickness)"
                self.notes.append(f"dielectrics scaled by {f:.4f} to match --board-thickness")

        # z coordinates, bottom-up
        z = 0.0
        for L in reversed(stack):
            L.z0, L.z1 = z, z + L.t
            z = L.z1
        self.total = z
        if t_attr and abs(t_attr - self.total) > 0.0005 and not unknown:
            self.notes.append(f"stack sum {self.total:.4f} mm differs from {t_attr_src} {t_attr:.4f} mm")

        # coating layers (mask, silk, finish, paste) and their side
        rows = [L.row for L in coppers] or [L.row for L in core]
        first, last = min(rows), max(rows)
        for L in board:
            if L.kind not in ("mask", "silk", "finish", "paste"):
                continue
            tk = name_tokens(L.name)
            if L.row < first:
                side = "top"
            elif L.row > last:
                side = "bottom"
            elif tk & {"bot", "bottom", "b"}:
                side = "bottom"
            else:
                side = "top"
            L.side = side
            self._coat_thickness(L, over)
            self.coats[side][L.kind].append(L)

        n = len(stack)
        self.top_cu = stack[0] if stack[0].kind == "copper" else None
        self.bot_cu = stack[-1] if stack[-1].kind == "copper" else None
        self.z_top = stack[0].z1
        self.z_bot = stack[-1].z0
        for i, L in enumerate(stack):
            L.side = "top" if i == 0 else ("bottom" if i == n - 1 else "inner")

    def _coat_thickness(self, L: MLayer, over):
        kind = L.kind
        if L.key in over:
            L.t, L.t_src = over[L.key], "command line"
            return
        cli = getattr(self.opts, f"{kind}_thickness", None)
        v = layer_dielectric_from_attrs(L.attrs)
        lo, hi = VALID_RANGE[kind]
        if v is not None and lo <= v <= hi:
            L.t, L.t_src = v, ".layer_dielectric"
        elif cli is not None:
            L.t, L.t_src = cli, "command line default" if v is None else f"command line (ODB value {v:g} mm ignored)"
        else:
            L.t = DEFAULTS[kind]
            L.t_src = "standard default" if v is None else f"standard default (ODB value {v * 1000:g} um implausible)"
        L.material = L.attrs.get(".comment", "")
        L.dk = attr_float(L.attrs, ".dielectric_constant")
        L.df = attr_float(L.attrs, ".loss_tangent")

    def index(self, name: str) -> Optional[int]:
        key = (name or "").lower()
        for i, L in enumerate(self.stack):
            if L.key == key:
                return i
        return None

    def span(self, L: MLayer, all_layers: List[MLayer]) -> Tuple[int, int]:
        """Stack index range (top, bottom) of a drill/rout layer."""
        n = len(self.stack)

        def resolve(name, default):
            if not name:
                return default
            i = self.index(name)
            if i is not None:
                return i
            # a non-stack board layer: map by row to the nearest stack layer
            row = next((X.row for X in all_layers if X.key == name.lower()), None)
            if row is None:
                warn(f"drill span layer '{name}' not found; using full stack")
                return default
            best = min(range(n), key=lambda j: abs(self.stack[j].row - row))
            return best
        a = resolve(L.start, 0)
        b = resolve(L.end, n - 1)
        return (min(a, b), max(a, b))


# --------------------------------------------------------------------------
# Holes, counterholes, backdrills, via protection
# --------------------------------------------------------------------------


@dataclass
class Hole:
    x: float
    y: float
    r_fin: float
    r_drill: float
    plated: bool
    kind: str                       # via | pth | npth
    layer: str
    top_idx: int
    bot_idx: int
    z_lo: float
    z_hi: float
    laser: bool = False
    entry: str = "top"
    geom_fin: object = None         # non-round holes (slots)
    geom_drill: object = None
    attrs: Optional[dict] = None
    z_lo_eff: float = 0.0
    z_hi_eff: float = 0.0
    fill: str = ""
    plug_top: float = 0.0
    plug_bot: float = 0.0
    cap_top: float = 0.0
    cap_bot: float = 0.0
    tent_top: bool = False
    tent_bot: bool = False
    pierce: bool = True
    prot_src: str = ""
    dual: Optional[tuple] = None    # (r_dd, z_end, side)
    notes: List[str] = field(default_factory=list)

    def region_drill(self):
        return self.geom_drill if self.geom_drill is not None else circle(self.x, self.y, self.r_drill)

    def region_fin(self):
        return self.geom_fin if self.geom_fin is not None else circle(self.x, self.y, self.r_fin)

    @property
    def round(self) -> bool:
        return self.geom_drill is None


@dataclass
class Cut:
    region: object
    z_lo: float
    z_hi: float
    kind: str
    pierce_top: bool
    pierce_bot: bool
    hole: object = None
    cid: int = -1


@dataclass
class Cone:
    x: float
    y: float
    r_wide: float
    r_narrow: float
    z_wide: float
    z_narrow: float
    side: str


class PointIndex:
    def __init__(self, tol=0.005):
        self.tol = tol
        self.grid: Dict[tuple, list] = defaultdict(list)

    def add(self, x, y, obj):
        self.grid[(int(math.floor(x / self.tol)), int(math.floor(y / self.tol)))].append((x, y, obj))

    def find(self, x, y):
        gx, gy = int(math.floor(x / self.tol)), int(math.floor(y / self.tol))
        best, bd = None, self.tol
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for (px, py, o) in self.grid.get((gx + i, gy + j), ()):
                    d = math.hypot(px - x, py - y)
                    if d <= bd:
                        best, bd = o, d
        return best

    def find_all(self, x, y):
        gx, gy = int(math.floor(x / self.tol)), int(math.floor(y / self.tol))
        out = []
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for (px, py, o) in self.grid.get((gx + i, gy + j), ()):
                    if math.hypot(px - x, py - y) <= self.tol:
                        out.append(o)
        return out


def parse_tools(text: Optional[str]):
    tools = []
    if not text:
        return tools
    top, arrays = parse_structured(text)
    units = top.get("UNITS", "INCH").upper()
    for n, d in arrays:
        if n != "TOOLS":
            continue

        def size(key):
            try:
                v = float(d.get(key, "-1") or -1)
            except ValueError:
                return None
            if v <= 0:
                return None
            if units.startswith("MM"):
                return v if v < 20.0 else v / 1000.0
            return v * 25.4 if v < 1.0 else v * MIL
        tools.append(dict(num=d.get("NUM", ""), type=d.get("TYPE", "").upper(), type2=d.get("TYPE2", "").upper(),
                          finish=size("FINISH_SIZE"), drill=size("DRILL_SIZE")))
    return tools


def match_tool(tools, d_mm, drill_attr):
    cands = [t for t in tools if t["finish"] and abs(t["finish"] - d_mm) < 0.0015]
    if not cands:
        cands = [t for t in tools if t["drill"] and abs(t["drill"] - d_mm) < 0.0015]
    if len(cands) > 1 and drill_attr:
        want = {"plated": "PLATED", "non_plated": "NON_PLATED", "via": "VIA"}.get(drill_attr)
        c2 = [t for t in cands if t["type"] == want]
        if c2:
            cands = c2
    return cands[0] if cands else None


IPC_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7}


# --------------------------------------------------------------------------
# Counterhole type, depth and angle from outside the ODB++ feature data.
# ODB++ has no field for them (drill subtypes are only BACKDRILL and
# DUAL_DIAMETER); Altium's ADD_TYPE=COUNTERHOLE layer carries only the
# diameter and the layer span. Altium's NC drill report (.DRR) lists depth
# and angle per counterhole tool, and its Excellon files give the positions.
# --------------------------------------------------------------------------

_MEAS_RE = re.compile(r"([-+]?(?:\d+\.?\d*|\.\d+))\s*(mm|mils?|inch|in|um)\b(\s*\([^)]*\))?", re.I)
_UNIT_MM = {"mm": 1.0, "mil": 0.0254, "mils": 0.0254, "inch": 25.4, "in": 25.4, "um": 0.001}
NC_ARCHIVES = (".tgz", ".tar.gz", ".tar", ".zip")


@dataclass
class NcCounter:
    """One counterhole tool of an NC drill report."""
    report: str
    section: str
    side: Optional[str]
    tool: int
    d: float
    depth: Optional[float]
    angle: Optional[float]
    count: Optional[int]
    drill_file: str
    pts: Optional[List[Tuple[float, float]]] = None
    used: int = 0

    def label(self) -> str:
        bits = [f"T{self.tool} {self.d:g} mm"]
        if self.depth is not None:
            bits.append(f"depth {self.depth:g} mm")
        if self.angle:
            bits.append(f"angle {self.angle:g} deg")
        return f"{self.report}, {self.section}: " + ", ".join(bits)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse_drr(text: str, report: str) -> List[NcCounter]:
    """Counterhole tools of an Altium NC drill report. A tool line reads
    'T2  3.2mm (126mil)  [tolerance]  Round  12  NPTH  <travel>  1.6mm (..)  90°';
    the depth and angle columns follow the tool travel. An angle marks a
    countersink (Altium gives its depth to the cone tip), a depth without
    angle a counterbore."""
    out: List[NcCounter] = []
    section, counter, dfile = "", False, ""
    has_depth = has_angle = False
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"(layer\s*pair|counter\s*holes?|back\s*drill\w*)\s*:\s*(.*)$", s, re.I)
        if m:
            counter = m.group(1).lower().replace(" ", "").startswith("counterhole")
            section, dfile, has_depth, has_angle = m.group(2).strip(), "", False, False
            continue
        m = re.search(r"\bfile\s*:\s*(\S.*?)\s*$", s, re.I)
        if m:
            dfile = m.group(1)
            continue
        if re.match(r"tool\s+hole\s+size\b", s, re.I):
            has_depth = re.search(r"\bdepth\b", s, re.I) is not None
            has_angle = re.search(r"\bangle\b", s, re.I) is not None
            continue
        m = re.match(r"T(\d+)\s+(.+)$", s)
        if not (counter and m):
            continue
        rest = m.group(2)
        ms = _MEAS_RE.search(rest)
        mp = re.search(r"\b(N?PTH)\b", rest, re.I)
        if ms is None or mp is None or mp.start() < ms.end():
            continue
        d = float(ms.group(1)) * _UNIT_MM[ms.group(2).lower()]
        mc = re.search(r"(\d+)\s*$", rest[:mp.start()])
        tail = rest[mp.end():]
        meas = list(_MEAS_RE.finditer(tail))
        depth = angle = None
        if has_depth and len(meas) >= 2:
            depth = float(meas[1].group(1)) * _UNIT_MM[meas[1].group(2).lower()]
        if has_angle:
            ma = re.search(r"([-+]?\d+(?:\.\d*)?)", tail[meas[-1].end():] if meas else tail)
            if ma and 0.0 < float(ma.group(1)) < 180.0:
                angle = float(ma.group(1))
        toks = name_tokens(section)
        side = "top" if "top" in toks else ("bottom" if toks & {"bottom", "bot"} else None)
        out.append(NcCounter(report, section, side, int(m.group(1)), d, depth, angle,
                             int(mc.group(1)) if mc else None, dfile))
    return out


def parse_excellon(text: str) -> Tuple[Dict[int, float], Dict[int, List[Tuple[float, float]]]]:
    """Tool diameters and drill hits (mm) of an Excellon file (absolute
    coordinates; LZ/TZ and ;FILE_FORMAT=i:d honoured; routed slots skipped)."""
    metric, lz, fmt = False, True, None
    diam: Dict[int, float] = {}
    hits: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
    cur, x, y = None, 0.0, 0.0

    def num(v: str) -> float:
        if "." in v:
            val = float(v)
        else:
            sign = -1.0 if v.startswith("-") else 1.0
            dig = v.lstrip("+-")
            a, b = fmt or ((3, 3) if metric else (2, 4))
            val = sign * int(dig.ljust(a + b, "0") if lz else dig) / 10.0 ** b
        return val if metric else val * 25.4

    for raw in text.splitlines():
        s = raw.strip().upper()
        if not s:
            continue
        if s.startswith(";"):
            m = re.search(r"FILE_FORMAT\s*=\s*(\d+)\s*:\s*(\d+)", s)
            if m:
                fmt = (int(m.group(1)), int(m.group(2)))
            continue
        if s.startswith(("METRIC", "INCH")):
            metric = s.startswith("METRIC")
            lz = False if "TZ" in s else (True if "LZ" in s else lz)
            continue
        if s in ("M71", "M72"):
            metric = s == "M71"
            continue
        m = re.match(r"T(\d+)(.*)$", s)
        if m:
            cur = int(m.group(1))
            mc = re.search(r"C([\d.]+)", m.group(2))
            if mc:
                diam[cur] = float(mc.group(1)) * (1.0 if metric else 25.4)
            continue
        if cur is None or "G85" in s or s.startswith(("G00", "G01", "G02", "G03", "M15", "M16")):
            continue
        mx = re.search(r"X([-+]?[\d.]+)", s)
        my = re.search(r"Y([-+]?[\d.]+)", s)
        if mx is None and my is None:
            continue
        if mx:
            x = num(mx.group(1))
        if my:
            y = num(my.group(1))
        hits[cur].append((x, y))
    return diam, hits


class _NcPool:
    """Files that may hold NC drill output: one folder or one archive."""

    def __init__(self, label: str):
        self.label = label
        self.items: Dict[str, Tuple[str, object]] = {}   # lower basename -> (name, path or bytes)

    def add(self, name: str, src):
        self.items.setdefault(os.path.basename(name).lower(), (os.path.basename(name), src))

    def read(self, key: str) -> Optional[str]:
        it = self.items.get(key.lower())
        if it is None:
            return None
        src = it[1]
        if isinstance(src, (bytes, bytearray)):
            return _decode(bytes(src))
        try:
            with open(src, "rb") as f:
                return _decode(f.read())
        except OSError:
            return None

    def reports(self) -> List[str]:
        return sorted(k for k in self.items if k.endswith(".drr"))


def _pool_from_path(path: str) -> Optional[_NcPool]:
    if os.path.isdir(path):
        pool = _NcPool(path)
        for fn in os.listdir(path):
            full = os.path.join(path, fn)
            if os.path.isfile(full):
                pool.add(fn, full)
        return pool
    if os.path.isfile(path) and path.lower().endswith(NC_ARCHIVES):
        pool = _NcPool(path)
        data = open(path, "rb").read()
        try:
            if zipfile.is_zipfile(io.BytesIO(data)):
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for zi in z.infolist():
                        if not zi.is_dir():
                            pool.add(zi.filename.replace("\\", "/"), z.read(zi))
            else:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
                    for m in t.getmembers():
                        f = t.extractfile(m) if m.isfile() else None
                        if f is not None:
                            pool.add(m.name, f.read())
        except (tarfile.TarError, zipfile.BadZipFile, OSError):
            return None
        return pool
    return None


def _stem(path: str) -> str:
    base = os.path.basename(os.path.normpath(path)).lower()
    for ext in (".tar.gz",) + tuple(e for e in NC_ARCHIVES if e != ".tar.gz") + (".drr",):
        if base.endswith(ext):
            return base[: -len(ext)]
    return os.path.splitext(base)[0]


def load_nc_drill(job: OdbJob, input_path: str, extra: List[str]) -> Tuple[List[NcCounter], List[str]]:
    """Counterhole tools from NC drill reports given with --nc-drill, inside
    the ODB++ archive or folder, or beside it. Beside the input, a report
    named like the input is used; otherwise the only report there."""
    notes: List[str] = []
    chosen: List[Tuple[_NcPool, str]] = []
    for p in extra:
        if os.path.isfile(p) and p.lower().endswith(".drr"):
            pool = _pool_from_path(os.path.dirname(os.path.abspath(p)))
            if pool is not None:
                chosen.append((pool, os.path.basename(p).lower()))
            continue
        pool = _pool_from_path(p)
        if pool is None:
            warn(f"--nc-drill '{p}': not a .DRR report, folder or archive")
            continue
        if not pool.reports():
            warn(f"--nc-drill '{p}': no .DRR report found")
        chosen += [(pool, r) for r in pool.reports()]
    if not extra:
        inside = _NcPool("inside the ODB++ input")
        if job.files:
            for k, v in job.files.items():
                inside.add(k, v)
        else:
            for k, v in job.dir_index.items():
                inside.add(k, v)
        chosen += [(inside, r) for r in inside.reports()]
        if not chosen:
            here = os.path.abspath(input_path.rstrip("/\\"))
            beside = _pool_from_path(os.path.dirname(here))
            reps = beside.reports() if beside is not None else []
            stem = _stem(here)
            pick = [r for r in reps if _stem(r) == stem] or [r for r in reps if stem and stem in r]
            if not pick and len(reps) == 1:
                pick = reps
            if reps and not pick:
                notes.append("NC drill reports beside the input but none named like it ("
                             + ", ".join(reps[:5]) + "); use --nc-drill")
            chosen += [(beside, r) for r in pick]
    out: List[NcCounter] = []
    for pool, rep in chosen:
        text = pool.read(rep)
        if text is None:
            continue
        entries = parse_drr(text, pool.items[rep][0])
        found = []
        for e in entries:
            dtext = pool.read(e.drill_file) if e.drill_file else None
            if dtext is None:
                continue
            diam, hits = parse_excellon(dtext)
            if e.tool in hits:
                e.pts = hits[e.tool]
                if e.tool in diam and abs(diam[e.tool] - e.d) < 0.05:
                    e.d = diam[e.tool]
                found.append(e.drill_file)
        out += entries
        notes.append(f"NC drill report {pool.items[rep][0]} ({pool.label}): {len(entries)} counterhole tool(s)"
                     + (f", positions from {', '.join(sorted(set(found)))}" if found else
                        (", drill files not found: matched by size and side only" if entries else "")))
    return out, notes


def odb_counter_flags(job: OdbJob) -> Tuple[Optional[bool], Optional[bool]]:
    """Board-level counterbore / countersink flags of ODB++ metadata.xml
    (counter_bore, countersink), if an exporter wrote them."""
    for p in job._all_paths():
        if not p.endswith("metadata.xml"):
            continue
        data = job.files.get(p) if job.files else open(job.dir_index[p], "rb").read()
        txt = _decode(data)

        def flag(pat):
            m = re.search(rf"<({pat})\b[^>]*>\s*(\w+)\s*</\1\s*>", txt, re.I)
            v = m.group(2) if m else None
            if v is None:
                m = re.search(rf"\b(?:{pat})\s*=\s*[\"'](\w+)[\"']", txt, re.I)
                v = m.group(1) if m else None
            return None if v is None else v.lower() in ("true", "yes", "y", "1")
        return flag(r"counter_?bore"), flag(r"counter_?sink")
    return None, None


def parse_ipc_type(s: str):
    """'7', '1b', 'VI-a', 'IV-B', 'none' -> (top_code, bottom_code)."""
    s = s.strip().lower().replace("type", "").replace("_", "-").strip()
    if s in ("", "0", "none", "no"):
        return (0, 0)
    m = re.match(r"^(\d|[iv]+)\s*-?\s*([ab])?$", s)
    if not m:
        raise ValueError(f"unknown IPC-4761 type '{s}'")
    code = int(m.group(1)) if m.group(1).isdigit() else IPC_ROMAN.get(m.group(1), 0)
    if not 1 <= code <= 7:
        raise ValueError(f"IPC-4761 type out of range: '{s}'")
    if m.group(2) == "a":
        return (code, 0)
    return (code, code)


@dataclass
class ViaRule:
    top: int
    bot: int
    dmin: float = 0.0
    dmax: float = 1e9
    layer: str = "*"
    kind: str = "via"
    side: str = ""
    text: str = ""


def parse_via_rule(spec: str) -> ViaRule:
    parts = [p for p in re.split(r"[:,]", spec) if p.strip()]
    if not parts:
        raise ValueError("empty --via-protection rule")
    top, bot = parse_ipc_type(parts[0])
    r = ViaRule(top, bot, text=spec)
    for p in parts[1:]:
        if "=" not in p:
            raise ValueError(f"bad key in --via-protection '{spec}' (use key=value)")
        k, v = [x.strip().lower() for x in p.split("=", 1)]
        if k == "min":
            r.dmin = float(v)
        elif k == "max":
            r.dmax = float(v)
        elif k == "layer":
            r.layer = v
        elif k == "kind":
            r.kind = v
        elif k == "side":
            r.side = v
        else:
            raise ValueError(f"unknown key '{k}' in --via-protection '{spec}'")
    if r.side in ("bottom", "bot") and r.top and not r.bot:
        r.top, r.bot = 0, r.top
    return r


def apply_ipc(h: Hole, top: int, bot: int, fill_material: str, plug_depth: float, src: str):
    h.tent_top = top in (1, 2, 4, 6)
    h.tent_bot = bot in (1, 2, 4, 6)
    h.plug_top = plug_depth if top in (3, 4) else 0.0
    h.plug_bot = plug_depth if bot in (3, 4) else 0.0
    h.fill = fill_material if (top in (5, 6, 7) or bot in (5, 6, 7)) else ""
    h.cap_top = -1.0 if top == 7 else 0.0       # -1: radius decided later
    h.cap_bot = -1.0 if bot == 7 else 0.0
    h.prot_src = src


def ipc_label(h: Hole) -> str:
    parts = []
    if h.fill:
        parts.append(f"filled ({h.fill})")
    if h.plug_top or h.plug_bot:
        parts.append("plugged " + "/".join(s for s, v in (("top", h.plug_top), ("bottom", h.plug_bot)) if v))
    caps = [s for s, v in (("top", h.cap_top), ("bottom", h.cap_bot)) if v]
    if caps:
        parts.append("capped " + "/".join(caps))
    tents = [s for s, v in (("top", h.tent_top), ("bottom", h.tent_bot)) if v]
    if tents:
        parts.append("tented " + "/".join(tents))
    return ", ".join(parts) if parts else "open"


# --------------------------------------------------------------------------
# OpenCascade: B-rep construction with arc recovery
# --------------------------------------------------------------------------


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
            if len(ids) >= 4 and sweep >= math.radians(1.0):
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
    def ring_spec(coords):
        """-> (P, full_circle, arcs). P is the cleaned vertex array."""
        P = np.asarray(coords, dtype=float)[:, :2]
        if len(P) > 1 and abs(P[0, 0] - P[-1, 0]) < 1e-12 and abs(P[0, 1] - P[-1, 1]) < 1e-12:
            P = P[:-1]
        for _ in range(2):
            if len(P) >= 3:
                dd = np.hypot(*(np.roll(P, -1, axis=0) - P).T)
                keep = dd > 5e-5
                if keep.sum() >= 3 and not keep.all():
                    P = P[keep]
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
        plain = [(np.asarray(r.coords)[:-1, :2], None, []) for r in rings]
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
                f = cls._build_face(specs, z) if changed else None
                if f is None or abs(cls._area(f) - poly.area) > lim:
                    ArcFit.stats["fallbacks"] += 1
                    f = cls._build_face(plain, z)
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
                if f is not None:
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


def cut_shapes(shape, tools):
    if not tools:
        return [shape]
    try:
        c = OCC.BA.BRepAlgoAPI_Cut()
        c.SetArguments(OCC.shape_list([shape]))
        c.SetTools(OCC.shape_list(tools))
        c.SetRunParallel(True)
        c.Build()
        if not c.IsDone() or _has_errors(c):
            warn("a countersink cut failed; body left uncut")
            return [shape]
        return solids_of(c.Shape())
    except Exception:
        warn("a countersink cut failed; body left uncut")
        return [shape]


# --------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------

NAMED_COLORS = {
    "green": "#1b6e2d", "matte-green": "#2e5e33", "dark-green": "#0f4a1e", "blue": "#1f4e9c",
    "red": "#b02a2a", "black": "#141414", "matte-black": "#262626", "white": "#f2f2f2",
    "yellow": "#e8c51a", "purple": "#5e2b7e", "orange": "#d9661f", "grey": "#808080", "gray": "#808080",
    "clear": "#c8d8c0", "copper": "#c87533", "gold": "#e6be5a", "silver": "#c8c8c8", "tin": "#c8c8c8",
    "nickel": "#b4b4aa", "fr4": "#c9b97e", "laminate": "#cdbe91", "cream": "#e3d9be", "tan": "#cdbe91",
    "epoxy": "#3b3b33", "brown": "#6b4a2b",
}


def parse_color(s: str) -> Tuple[float, float, float]:
    t = s.strip().lower()
    t = NAMED_COLORS.get(t, t)
    m = re.match(r"^#?([0-9a-f]{6})$", t)
    if m:
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    parts = [p for p in re.split(r"[,\s]+", t) if p]
    if len(parts) == 3:
        v = [float(p) for p in parts]
        if max(v) > 1.0:
            v = [x / 255.0 for x in v]
        return tuple(min(1.0, max(0.0, x)) for x in v)
    raise argparse.ArgumentTypeError(f"bad colour '{s}' (use a name, #RRGGBB or R,G,B)")


def finish_color_for(material: str) -> str:
    m = (material or "").lower()
    if any(k in m for k in ("gold", "enig", "enepig", "au")):
        return "gold"
    if any(k in m for k in ("silver", "hasl", "tin", "lead", "ag", "sn")):
        return "silver"
    if "osp" in m or "copper" in m:
        return "copper"
    return "gold"


# --------------------------------------------------------------------------
# STEP writer (XCAF: names and colours)
# --------------------------------------------------------------------------


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


def write_step(path: str, root_name: str, components, schema: str = "AP214"):
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
    asm = st.NewShape()
    TDataStd_Name.Set_s(asm, TCollection_ExtendedString(root_name))
    for comp in components:
        if not comp["solids"]:
            continue
        cmp = OCC.TopoDS.TopoDS_Compound()
        bld = OCC.BRep.BRep_Builder()
        bld.MakeCompound(cmp)
        for s in comp["solids"]:
            bld.Add(cmp, s)
        lab = st.AddShape(cmp, False)
        TDataStd_Name.Set_s(lab, TCollection_ExtendedString(comp["name"]))
        r, g, b = comp["color"]
        ct.SetColor(lab, Quantity_Color(r, g, b, Quantity_TypeOfColor.Quantity_TOC_sRGB),
                    XCAFDoc_ColorType.XCAFDoc_ColorSurf)
        st.AddComponent(asm, lab, TopLoc_Location())
        if comp.get("layer"):
            lt.SetLayer(lab, TCollection_ExtendedString(comp["layer"]))
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
    _stamp_header(path, root_name)


def _stamp_header(path: str, root_name: str):
    """Record the converter and its version in the STEP header."""
    with open(path, "r", encoding="latin-1") as f:
        text = f.read()
    head_end = text.find("ENDSEC;")
    head, body = text[:head_end], text[head_end:]
    src = f"odb2step {__version__}"
    head = re.sub(r"FILE_DESCRIPTION\(\(.*?\),", lambda m: f"FILE_DESCRIPTION(('{root_name} from ODB++ by {src}'),",
                  head, count=1, flags=re.S)
    head = re.sub(r"FILE_NAME\('(.*?)',", lambda m: f"FILE_NAME('{root_name}',", head, count=1, flags=re.S)
    head = head.replace("'Open CASCADE STEP processor", f"'{src} / Open CASCADE STEP processor", 1)
    with open(path, "w", encoding="latin-1") as f:
        f.write(head + body)


# --------------------------------------------------------------------------
# Model: 2D layer regions -> holes -> cuts -> solids
# --------------------------------------------------------------------------

FILL_TOKENS = {"fill", "filling", "filled"}
CAP_TOKENS = {"cap", "capping", "capped", "caps"}
PLUG_TOKENS = {"plug", "plugging", "plugged"}
TENT_TOKENS = {"tent", "tenting", "tented", "cover", "covering", "covered"}
COUNTER_TOKENS = {"counterhole", "counterholes", "counterbore", "counterbores", "countersink", "countersinks",
                  "cbore", "csink", "csk"}


def _rule_floats(spec: str, name: str):
    parts = [p.strip() for p in spec.split(":")]
    if len(parts) < 3:
        raise SystemExit(f"--{name} expects HOLE:DIA:{'DEPTH' if name == 'counterbore' else 'ANGLE'}[:top|bottom]")
    side = parts[3].lower() if len(parts) > 3 else "top"
    if side not in ("top", "bottom", "bot", "both"):
        raise SystemExit(f"--{name}: side must be top, bottom or both")
    return float(parts[0]), float(parts[1]), float(parts[2]), ("bottom" if side == "bot" else side)


class Model:
    def __init__(self, job: OdbJob, opts):
        self.job = job
        self.opts = opts
        top, _ = parse_structured(job.text("misc/info") or "")
        self.info = top
        self.units = top.get("UNITS", "INCH").upper()
        self.steps, self.layers = load_matrix(job)
        self.step = self._pick_step()
        for L in self.layers:
            L.attrs = parse_attrlist(job.text(f"steps/{self.step}/layers/{L.key}/attrlist"))
        self.job_attrs = parse_attrlist(job.text("misc/attrlist"))
        self.step_attrs = parse_attrlist(job.text(f"steps/{self.step}/attrlist"))
        xml = job.text("matrix/stackup.xml")
        self.xml_t = parse_stackup_xml(xml) if xml else {}
        self.instances = step_instances(job, self.step, units=self.units)
        self._ff: Dict[tuple, FeatureFile] = {}
        steps_used = sorted({s for s, _ in self.instances})
        empty_cu = {L.key for L in self.layers
                    if L.kind == "copper" and not L.polarity.upper().startswith("NEG")
                    and not any(self.ff(s, L.key).records for s in steps_used)}
        self.stk = Stackup(self.layers, self.job_attrs, self.step_attrs, self.xml_t, opts, empty_cu)
        self.R = Renderer(job, self.units)
        self.meta_counter = odb_counter_flags(job)
        self.nc, self.nc_notes = ([], []) if opts.no_nc_drill else load_nc_drill(job, job.path, opts.nc_drill)
        self._own: Dict[tuple, object] = {}
        self._img: Dict[str, object] = {}
        self._tools: Dict[tuple, list] = {}
        self.holes: List[Hole] = []
        self.counters, self.backdrills, self.duals = [], [], []
        self.cuts: List[Cut] = []
        self.cones: List[Cone] = []
        self.prot_notes: List[str] = []
        self.hole_notes: List[str] = []
        self.components: List[dict] = []
        self.svg: Dict[str, object] = {}

    # .. inputs ................................................................
    def _pick_step(self) -> str:
        names = [s.lower() for s in self.steps] or self.job.listdir("steps")
        if not names:
            raise SystemExit("the product model has no steps")
        if self.opts.step:
            want = self.opts.step.lower()
            if want not in names:
                raise SystemExit(f"step '{self.opts.step}' not found; available: {', '.join(names)}")
            return want
        if "pcb" in names:
            return "pcb"
        for s in names:
            if "STEP-REPEAT" not in (self.job.text(f"steps/{s}/stephdr") or "").upper():
                return s
        return names[0]

    def ff(self, step: str, layer: str) -> FeatureFile:
        key = (step, layer)
        if key not in self._ff:
            self._ff[key] = parse_features(self.job.text(f"steps/{step}/layers/{layer}/features"), self.units)
        return self._ff[key]

    def tools(self, step: str, layer: str):
        key = (step, layer)
        if key not in self._tools:
            self._tools[key] = parse_tools(self.job.text(f"steps/{step}/layers/{layer}/tools"))
        return self._tools[key]

    def raw_image(self, L: MLayer):
        if L.key in self._img:
            return self._img[L.key]
        parts = []
        for s, M in self.instances:
            key = (s, L.key)
            if key not in self._own:
                ff = self.ff(s, L.key)
                self._own[key] = self.R.render(ff) if ff.records else EMPTY
            g = self._own[key]
            if not g.is_empty:
                parts.append(m_apply(M, g))
        g = union_all(parts)
        self._img[L.key] = g
        return g

    def image(self, L: MLayer):
        g = self.raw_image(L)
        if L.polarity.startswith("NEG"):
            g = gdiff(self.B, g)
        return clean(ginter(g, self.B))

    # .. preparation ..........................................................
    def prepare(self):
        ff = parse_features(self.job.text(f"steps/{self.step}/profile"), self.units)
        B = self.R.render(ff)
        if B.is_empty:
            cu = union_all(self.raw_image(L) for L in self.stk.stack if L.kind == "copper")
            if cu.is_empty:
                raise SystemExit("no step profile and no copper: cannot determine the board outline")
            warn("step profile missing or empty; using the bounding box of the copper as board outline")
            x0, y0, x1, y1 = cu.bounds
            B = box(x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5)
        self.B = clean(B)
        self.svg["profile"] = self.B
        self._build_holes()
        self._via_protection()
        self._counter_and_backdrills()
        self._resolve_caps()
        self._build_cuts()

    def _entry_side(self, L: MLayer, top_i: int, bot_i: int) -> str:
        d = L.attrs.get(".drill_layer_direction", "").lower()
        if d.startswith("top2"):
            return "top"
        if d.startswith("bottom2") or d.startswith("bot2"):
            return "bottom"
        n = len(self.stk.stack)
        tt, tb = top_i == 0, bot_i == n - 1
        if tt and not tb:
            return "top"
        if tb and not tt:
            return "bottom"
        toks = name_tokens(L.name)
        if toks & {"bottom", "bot"} and "top" not in toks:
            return "bottom"
        return "top"

    def _drill_role(self, L: MLayer) -> str:
        toks = name_tokens(L.name)
        add = L.add_type
        if add == "BACKDRILL" or toks & {"backdrill", "backdrills", "bd"}:
            return "backdrill"
        if add in ("COUNTERHOLE", "COUNTERBORE", "COUNTERSINK") or toks & COUNTER_TOKENS:
            return "counter"
        if add == "DUAL_DIAMETER":
            return "dual"
        return "hole"

    def _build_holes(self):
        st = self.stk.stack
        tp = self.opts.plating_thickness
        for L in self.layers:
            if L.context != "BOARD" or L.kind != "drill":
                continue
            top_i, bot_i = self.stk.span(L, self.layers)
            role = self._drill_role(L)
            side = self._entry_side(L, top_i, bot_i)
            toks = name_tokens(L.name)
            npth_name = bool(toks & {"npth", "nonplated", "unplated", "np"}) or ("non" in toks and "plated" in toks)
            count = 0
            for s, M in self.instances:
                ff = self.ff(s, L.key)
                tools = self.tools(s, L.key)
                for rec in ff.records:
                    item = self._drill_item(rec, ff, M)
                    if item is None:
                        continue
                    px, py, d, geom = item
                    count += 1
                    attrs = rec.attrs or {}
                    if role != "hole":
                        spec = dict(x=px, y=py, d=d, geom=geom, layer=L, side=side, top=top_i, bot=bot_i,
                                    attrs=attrs)
                        {"counter": self.counters, "backdrill": self.backdrills, "dual": self.duals}[role].append(spec)
                        continue
                    drill_attr = attrs.get(".drill")
                    tool = match_tool(tools, d, drill_attr) if d > 0 else None
                    ttype = tool["type"] if tool else ""
                    if drill_attr in ("plated", "via"):
                        plated = True
                    elif drill_attr == "non_plated":
                        plated = False
                    elif ttype:
                        plated = ttype != "NON_PLATED"
                    else:
                        plated = not npth_name
                    kind = "via" if (drill_attr == "via" or ttype == "VIA") and plated else ("pth" if plated else "npth")
                    laser = bool(tool and tool["type2"] == "LASER") or str(attrs.get(".via_type", "")).lower() == "laser"
                    r_fin = max(0.0, d / 2.0)
                    if plated:
                        if tool and tool["drill"] and tool["finish"] and tool["drill"] > tool["finish"] + 0.001:
                            r_drill = tool["drill"] / 2.0
                        else:
                            r_drill = r_fin + tp
                    else:
                        r_drill = r_fin
                    if laser:
                        entry = side
                        if entry == "top":
                            z_hi = st[top_i].z1
                            tgt = st[bot_i]
                            z_lo = tgt.z1 if (tgt.kind == "copper" and bot_i != top_i) else tgt.z0
                        else:
                            z_lo = st[bot_i].z0
                            tgt = st[top_i]
                            z_hi = tgt.z0 if (tgt.kind == "copper" and bot_i != top_i) else tgt.z1
                    else:
                        entry = side
                        z_lo, z_hi = st[bot_i].z0, st[top_i].z1
                    h = Hole(x=px, y=py, r_fin=r_fin, r_drill=r_drill, plated=plated, kind=kind, layer=L.name,
                             top_idx=top_i, bot_idx=bot_i, z_lo=z_lo, z_hi=z_hi, laser=laser, entry=entry,
                             attrs=attrs, z_lo_eff=z_lo, z_hi_eff=z_hi)
                    if geom is not None:
                        h.geom_fin = geom
                        h.geom_drill = clean(geom.buffer(r_drill - r_fin, quad_segs=32)) if r_drill > r_fin else geom
                    if L.attrs.get(".cu_base", "no").lower() in ("yes", "y", "true", "1") and plated:
                        h.fill, h.prot_src = "copper", ".cu_base"
                    self.holes.append(h)
            if count == 0:
                continue

    def _drill_item(self, rec: Rec, ff: FeatureFile, M):
        """-> (x, y, diameter, geometry or None for round holes)."""
        cs = ff.cs
        if rec.kind == "P":
            x, y, sym, _resize, _od, _ang = rec.data
            kind, size, _name, _unit = self.R._sym_kind(ff, sym)
            px, py = m_apply_pt(M, x * cs, y * cs)
            if kind == "r":
                return px, py, size, None
            g = m_apply(M, self.R.feature_geom(rec, ff))
            if g.is_empty:
                return None
            x0, y0, x1, y1 = g.bounds
            return px, py, min(x1 - x0, y1 - y0), g
        if rec.kind in ("L", "A", "S"):
            g = m_apply(M, self.R.feature_geom(rec, ff))
            if g.is_empty:
                return None
            c = g.centroid
            if rec.kind == "S":
                x0, y0, x1, y1 = g.bounds
                d = min(x1 - x0, y1 - y0)
            else:
                kind, size, _n, _u = self.R._sym_kind(ff, rec.data[4] if rec.kind == "L" else rec.data[6])
                d = size
            return c.x, c.y, d, g
        return None

    def _hole_index(self, plated_only=False):
        idx = PointIndex(0.01)
        for h in self.holes:
            if plated_only and not h.plated:
                continue
            idx.add(h.x, h.y, h)
        return idx

    # .. via protection ......................................................
    def _doc_points(self, L: MLayer):
        pts = []
        for s, M in self.instances:
            ff = self.ff(s, L.key)
            for rec in ff.records:
                item = self._drill_item(rec, ff, M)
                if item:
                    pts.append(item[:3])
        return pts

    def _via_protection(self):
        fm = self.opts.fill_material
        pd = self.opts.plug_depth
        plated = [h for h in self.holes if h.plated]
        if not plated:
            return
        n_attr = 0
        for h in plated:
            a = h.attrs or {}
            t, b = a.get(".ipc_via_type_top"), a.get(".ipc_via_type_bottom")
            if t or b:
                def code(v):
                    m = re.search(r"\d", str(v or ""))
                    return int(m.group(0)) if m else 0
                apply_ipc(h, code(t), code(b), fm if h.fill != "copper" else "copper", pd, "attributes .ipc_via_type")
                n_attr += 1
            elif a.get(".via_in_pad") is True:
                apply_ipc(h, 7, 7, fm, pd, "attribute .via_in_pad")
                n_attr += 1
            if a.get(".is_capped") is True and not (h.cap_top or h.cap_bot):
                h.cap_top = h.cap_bot = -1.0
                h.fill = h.fill or fm
                h.prot_src = h.prot_src or "attribute .is_capped"
                n_attr += 1
        if n_attr:
            self.prot_notes.append(f"{n_attr} holes: protection from ODB++ feature attributes")
        idx = PointIndex(0.01)
        for h in plated:
            idx.add(h.x, h.y, h)
        for L in self.layers:
            if L.kind in ("copper", "dielectric", "drill", "rout", "silk", "paste"):
                continue
            toks = name_tokens(L.name)
            role = ("fill" if toks & FILL_TOKENS else "cap" if toks & CAP_TOKENS else
                    "plug" if toks & PLUG_TOKENS else "tent" if toks & TENT_TOKENS else None)
            if role is None:
                continue
            sides = ("top",) if "top" in toks else ("bottom",) if toks & {"bottom", "bot"} else ("top", "bottom")
            hit = 0
            for (x, y, d) in self._doc_points(L):
                for h in idx.find_all(x, y):
                    hit += 1
                    if role == "fill":
                        h.fill = "copper" if toks & {"copper", "conductive", "cu"} else (h.fill or fm)
                    elif role == "cap":
                        for sd in sides:
                            setattr(h, f"cap_{'top' if sd == 'top' else 'bot'}", d / 2.0 if d > 0 else -1.0)
                        h.fill = h.fill or fm
                    elif role == "plug":
                        for sd in sides:
                            setattr(h, f"plug_{'top' if sd == 'top' else 'bot'}", pd)
                    else:
                        for sd in sides:
                            setattr(h, f"tent_{'top' if sd == 'top' else 'bot'}", True)
                    h.prot_src = (h.prot_src + ", " if h.prot_src else "") + f"layer {L.name}"
            if hit:
                self.prot_notes.append(f"layer {L.name}: {role} applied to {hit} hole(s) ({'/'.join(sides)})")
            else:
                warn(f"via-protection layer {L.name} ({role}) matched no plated hole")
        for spec in self.opts.via_rules:
            hit = 0
            for h in plated:
                if spec.kind == "via" and h.kind != "via":
                    continue
                if spec.kind == "pth" and h.kind != "pth":
                    continue
                d = 2 * h.r_fin
                if not (spec.dmin - 1e-6 <= d <= spec.dmax + 1e-6):
                    continue
                if not fnmatch.fnmatch(h.layer.lower(), spec.layer.lower()):
                    continue
                apply_ipc(h, spec.top, spec.bot, fm, pd, f"rule '{spec.text}'")
                hit += 1
            self.prot_notes.append(f"rule '{spec.text}': applied to {hit} hole(s)")

    # .. counterholes, backdrills, dual diameter ..........................
    def _surface(self, side):
        return self.stk.z_top if side == "top" else self.stk.z_bot

    def _counter_z(self, side, top_i, bot_i, mode):
        st = self.stk.stack
        n = len(st)
        if self.opts.counterhole_depth:
            d = self.opts.counterhole_depth
            return self.stk.z_top - d if side == "top" else self.stk.z_bot + d
        if side == "bottom":
            deep = st[top_i]
            if mode == "expose" and deep.kind == "copper" and top_i != n - 1:
                return deep.z0
            return deep.z1
        deep = st[bot_i]
        if mode == "expose" and deep.kind == "copper" and bot_i != 0:
            return deep.z1
        return deep.z0

    def _trim_hole(self, h: Hole, side: str, z_end: float):
        if side == "top":
            h.z_hi_eff = min(h.z_hi_eff, z_end)
            h.cap_top, h.plug_top, h.tent_top = 0.0, 0.0, False
        else:
            h.z_lo_eff = max(h.z_lo_eff, z_end)
            h.cap_bot, h.plug_bot, h.tent_bot = 0.0, 0.0, False

    def _counter_type(self, L: MLayer) -> Tuple[str, str]:
        """(type, source) for a counterhole without NC drill data: the layer
        type or name, else the board flags of metadata.xml, else
        --counterhole-type."""
        toks = name_tokens(L.name)
        if L.add_type == "COUNTERSINK" or toks & {"countersink", "countersinks", "csink", "csk"}:
            return "countersink", "layer"
        if L.add_type == "COUNTERBORE" or toks & {"counterbore", "counterbores", "cbore"}:
            return "counterbore", "layer"
        cb, cs = self.meta_counter
        if cs and not cb:
            return "countersink", "ODB++ metadata.xml: the board has countersinks only"
        if cb and not cs:
            return "counterbore", "ODB++ metadata.xml: the board has counterbores only"
        return self.opts.counterhole_type, "default"

    def _nc_index(self):
        """NC drill counterhole hits indexed in ODB++ coordinates. Altium can
        write NC drill files against another origin: if the hits do not line
        up as they are, the shift that lines up most of them is used."""
        pts = [(p, e) for e in self.nc if e.pts for p in e.pts]
        if not pts or not self.counters:
            return None
        odb = PointIndex(tol=0.02)
        for c in self.counters:
            odb.add(c["x"], c["y"], c)
        sample = pts[:: max(1, len(pts) // 50)]

        def score(dx, dy, sel):
            return sum(1 for (x, y), _ in sel if odb.find(x + dx, y + dy) is not None)
        off = (0.0, 0.0)
        if score(0.0, 0.0, sample) < len(sample):
            best = score(0.0, 0.0, sample)
            for c in self.counters[:2]:
                for (x, y), _ in pts:
                    sc = score(c["x"] - x, c["y"] - y, sample)
                    if sc > best:
                        best, off = sc, (c["x"] - x, c["y"] - y)
        if score(off[0], off[1], pts) < max(1, (len(pts) + 1) // 2):
            self.hole_notes.append("NC drill hits do not line up with the ODB++ counterholes, also not after a "
                                   "shift: matched by size and side only")
            return None
        if off != (0.0, 0.0):
            self.hole_notes.append(f"NC drill coordinates shifted by ({off[0]:+.4f}, {off[1]:+.4f}) mm "
                                   f"to line up with the ODB++")
        idx = PointIndex(tol=0.02)
        for (x, y), e in pts:
            idx.add(x + off[0], y + off[1], e)
        return idx

    def _nc_entry(self, c, side, nc_idx) -> Optional[NcCounter]:
        def fits(q):
            return abs(q.d - c["d"]) <= 0.02 and q.side in (None, side)
        if nc_idx is not None:
            hit = [q for q in nc_idx.find_all(c["x"], c["y"]) if fits(q)]
            if hit:
                return hit[0]
        pool = [q for q in self.nc if fits(q) and (q.pts is None or nc_idx is None)]
        if pool and len({(q.depth, q.angle) for q in pool}) == 1:
            return pool[0]
        return None

    def _add_counterbore(self, x, y, r, side, z_end, h, src, geom=None):
        surf = self._surface(side)
        region = geom if geom is not None else circle(x, y, r)
        lo, hi = (z_end, surf) if side == "top" else (surf, z_end)
        self.cuts.append(Cut(region, lo, hi, "counterbore", side == "top", side == "bottom"))
        if h is not None:
            self._trim_hole(h, side, z_end)
        return abs(surf - z_end)

    def _add_countersink(self, x, y, r_wide, angle, side, h):
        surf = self._surface(side)
        r_n = h.r_drill if h is not None else max(0.01, 0.05 * r_wide)
        r_n = min(r_n, r_wide * 0.999)
        depth = (r_wide - r_n) / math.tan(math.radians(angle) / 2.0)
        depth = min(depth, self.stk.total * 0.999)
        z_n = surf - depth if side == "top" else surf + depth
        self.cones.append(Cone(x, y, r_wide, r_n, surf, z_n, side))
        if h is not None:
            self._trim_hole(h, side, z_n)
        return depth

    def _counter_and_backdrills(self):
        st = self.stk.stack
        n = len(st)
        idx = self._hole_index()
        rules = []
        for spec in self.opts.counterbore or []:
            d1, d2, depth, side = _rule_floats(spec, "counterbore")
            rules.append(("counterbore", d1, d2, depth, side))
        for spec in self.opts.countersink or []:
            d1, d2, ang, side = _rule_floats(spec, "countersink")
            rules.append(("countersink", d1, d2, ang, side))
        ruled = set()
        for (kind, d1, d2, val, side) in rules:
            hit = 0
            for h in self.holes:
                if abs(2 * h.r_fin - d1) > 0.005:
                    continue
                for sd in (("top", "bottom") if side == "both" else (side,)):
                    if kind == "counterbore":
                        z_end = self.stk.z_top - val if sd == "top" else self.stk.z_bot + val
                        self._add_counterbore(h.x, h.y, d2 / 2.0, sd, z_end, h, "rule")
                    else:
                        self._add_countersink(h.x, h.y, d2 / 2.0, val, sd, h)
                    ruled.add((id(h), sd))
                    hit += 1
            self.hole_notes.append(f"--{kind} {d1:g}:{d2:g}:{val:g}:{side}: {hit} hole side(s)")
        groups = Counter()
        assumed = False
        self.hole_notes.extend(self.nc_notes)
        nc_idx = self._nc_index() if self.nc else None
        for c in self.counters:
            h = idx.find(c["x"], c["y"])
            side = c["side"]
            if h is not None and (id(h), side) in ruled:
                continue
            r = c["d"] / 2.0
            ang = self.opts.countersink_angle
            src = ""
            e = self._nc_entry(c, side, nc_idx) if self.nc else None
            if e is not None and (e.angle or e.depth is not None):
                e.used += 1
                ctype = "countersink" if e.angle else "counterbore"
                src = "from NC drill report " + e.label()
                if e.angle:
                    ang = e.angle
                    tip = r / math.tan(math.radians(ang) / 2.0)
                    if e.depth is not None:
                        src += (" (its depth is to the cone tip)" if abs(e.depth - tip) <= 0.02 else
                                f" (its depth does not match a {ang:g} deg cone: {tip:.3f} mm to the tip)")
            else:
                ctype, why = self._counter_type(c["layer"])
                if why == "default":
                    assumed = True
                elif why != "layer":
                    src = why
            if ctype == "countersink":
                depth = self._add_countersink(c["x"], c["y"], r, ang, side, h)
            else:
                if e is not None and e.depth is not None and not e.angle:
                    surf = self._surface(side)
                    dep = min(max(e.depth, 0.0), self.stk.total)
                    z_end = surf - dep if side == "top" else surf + dep
                else:
                    z_end = self._counter_z(side, c["top"], c["bot"], self.opts.counterhole_depth_mode)
                depth = self._add_counterbore(c["x"], c["y"], r, side, z_end, h, "odb", c["geom"])
            on = f"on {2 * h.r_fin:.3f} mm {h.kind.upper()}" if h else "(no hole underneath)"
            key = (c["layer"].name, ctype, round(c["d"], 4), side, round(depth, 4),
                   ang if ctype == "countersink" else 0.0, on, src)
            groups[key] += 1
        for (lname, ctype, d, side, depth, ang, on, src), k in sorted(groups.items()):
            extra = f", {ang:g} deg" if ctype == "countersink" else ""
            self.hole_notes.append(f"{lname}: {k} x {ctype} {d:.3f} mm from {side}, depth {depth:.4f} mm{extra} {on}"
                                   + (f"\n    {src}" if src else ""))
        for e in self.nc:
            if e.used == 0:
                self.hole_notes.append(f"NC drill {e.label()}: not found among the ODB++ counterholes")
            elif e.count and e.used != e.count:
                self.hole_notes.append(f"NC drill {e.label()}: report lists {e.count} hole(s), {e.used} matched")
        if assumed:
            self.hole_notes.append("counterhole type, depth and angle are not in Altium's ODB++: "
                                   f"{self.opts.counterhole_type} assumed (--counterhole-type, --counterhole-depth, "
                                   "--countersink-angle). Exact data: put the NC drill output (.DRR report and "
                                   "drill files) beside the archive, or use --nc-drill")
        bgroups = Counter()
        for c in self.backdrills:
            L = c["layer"]
            side = c["side"]
            surf = self._surface(side)
            h = idx.find(c["x"], c["y"])
            dattr = attr_float(L.attrs, ".backdrill_max_depth")
            stub = self.opts.backdrill_stub
            sattr = attr_float(c["attrs"], ".backdrill_max_stub_drill")
            if sattr and sattr > 0:
                stub = _len_mm(sattr, L.attrs.get("units", self.units), "mil_micron")
            if dattr and dattr > 0:
                depth = _len_mm(dattr, L.attrs.get("units", self.units), "mil_micron")
                z_end = surf - depth if side == "top" else surf + depth
            else:
                # the span lists the drilled layers; the next copper layer is the
                # must-not-cut layer. Drill on until 'stub' short of it.
                if side == "top":
                    z_far = st[c["bot"]].z0
                    k = c["bot"] + 1
                    while k < n and st[k].kind != "copper":
                        k += 1
                    z_end = min(z_far, st[k].z1 + stub) if k < n else z_far
                else:
                    z_far = st[c["top"]].z1
                    k = c["top"] - 1
                    while k >= 0 and st[k].kind != "copper":
                        k -= 1
                    z_end = max(z_far, st[k].z0 - stub) if k >= 0 else z_far
            depth = self._add_counterbore(c["x"], c["y"], c["d"] / 2.0, side, z_end, h, "backdrill")
            self.cuts[-1].kind = "backdrill"
            bgroups[(L.name, round(c["d"], 4), side, round(depth, 4), h is not None)] += 1
        for (lname, d, side, depth, ok), k in sorted(bgroups.items()):
            self.hole_notes.append(f"{lname}: {k} x backdrill {d:.3f} mm from {side}, depth {depth:.4f} mm"
                                   + ("" if ok else " (no matching PTH)"))
        for c in self.duals:
            side = c["side"]
            h = idx.find(c["x"], c["y"])
            z_end = self._counter_z(side, c["top"], c["bot"], "through")
            self._add_counterbore(c["x"], c["y"], c["d"] / 2.0, side, z_end, None, "dual")
            self.cuts[-1].kind = "dual"
            if h is not None and h.plated:
                h.dual = (c["d"] / 2.0, z_end, side)
                if side == "top":
                    h.z_hi_eff = min(h.z_hi_eff, z_end)
                    h.cap_top = 0.0
                else:
                    h.z_lo_eff = max(h.z_lo_eff, z_end)
                    h.cap_bot = 0.0
        if self.duals:
            self.hole_notes.append(f"{len(self.duals)} dual-diameter (plated step) holes")

    def _resolve_caps(self):
        st = self.stk.stack
        cu_cache = {}
        for h in self.holes:
            for side in ("top", "bot"):
                r = getattr(h, f"cap_{side}")
                if not r:
                    continue
                li = h.top_idx if side == "top" else h.bot_idx
                L = st[li]
                if L.kind != "copper":
                    setattr(h, f"cap_{side}", 0.0)
                    continue
                if r < 0:
                    if L.key not in cu_cache:
                        cu_cache[L.key] = self.image(L)
                    cu = cu_cache[L.key]
                    p = Point(h.x, h.y)
                    land = 0.0
                    for poly in iter_polys(cu):
                        if poly.contains(p):
                            land = poly.exterior.distance(p)
                            break
                    r = min(max(land, h.r_drill + 0.02), h.r_drill + 0.15) if land else h.r_drill + 0.05
                setattr(h, f"cap_{side}", max(r, h.r_drill))

    def _cap_t(self, h: Hole, side: str) -> float:
        L = self.stk.stack[h.top_idx if side == "top" else h.bot_idx]
        return min(self.opts.cap_thickness, L.t)

    def _build_cuts(self):
        thru = self.opts.drill_through_mask
        for h in self.holes:
            base_p = (h.kind != "via") or thru
            self.cuts.append(Cut(h.region_drill(), h.z_lo, h.z_hi, "hole",
                                 base_p and not h.tent_top, base_p and not h.tent_bot, hole=h))
        st = self.stk
        for L in self.layers:
            if L.context != "BOARD" or L.kind != "rout" or self.opts.rout == "ignore":
                continue
            g = self.raw_image(L)
            if g.is_empty:
                continue
            top_i, bot_i = st.span(L, self.layers)
            keep = []
            edge = self.B.boundary
            for p in iter_polys(g):
                if self.opts.rout == "auto" and p.intersects(edge):
                    continue
                keep.append(p)
            reg = union_all(keep)
            if reg.is_empty:
                continue
            self.cuts.append(Cut(reg, st.stack[bot_i].z0, st.stack[top_i].z1, "rout", True, True))
            self.hole_notes.append(f"rout layer {L.name}: {len(keep)} internal cut-out(s) applied")
        for i, c in enumerate(self.cuts):
            c.cid = i

    # .. 3D construction .......................................................
    def _cut_union(self, cuts, cache):
        key = tuple(c.cid for c in cuts)
        g = cache.get(key)
        if g is None:
            g = union_all(c.region for c in cuts)
            cache[key] = g
        return g

    def slab(self, base, z0, z1, cache, cuts=None, label=None):
        if base is None or base.is_empty:
            return []
        cuts = [c for c in (self.cuts if cuts is None else cuts) if c.z_lo < z1 - EPS_Z and c.z_hi > z0 + EPS_Z]
        ev = sorted({z0, z1} | {z for c in cuts for z in (c.z_lo, c.z_hi) if z0 + 1e-6 < z < z1 - 1e-6})
        merged = [ev[0]]
        for z in ev[1:]:
            if z - merged[-1] > 1e-6:
                merged.append(z)
        merged[-1] = z1
        out = []
        for a, b in zip(merged[:-1], merged[1:]):
            act = [c for c in cuts if c.z_lo <= a + 1e-6 and c.z_hi >= b - 1e-6]
            reg = clean(gdiff(base, self._cut_union(act, cache)))
            if label and a == merged[0]:
                self.svg[label] = reg
            out.extend(Brep.prisms(reg, a, b))
        return out

    def _coat_cuts(self, side):
        """Regions that remove coatings on one side: all holes reaching the
        surface, the subset that pierces mask/silk, and the holes not capped."""
        surf = self._surface(side)
        holes, pierce, holes_open = [], [], []
        for c in self.cuts:
            if c.kind == "cap":
                continue
            reach = (c.z_hi >= surf - EPS_Z) if side == "top" else (c.z_lo <= surf + EPS_Z)
            if not reach:
                continue
            holes.append(c.region)
            h = c.hole
            capped = h is not None and ((side == "top" and h.cap_top > 0) or (side == "bottom" and h.cap_bot > 0))
            if not capped:
                holes_open.append(c.region)
            if c.pierce_top if side == "top" else c.pierce_bot:
                pierce.append(c.region)
        for cn in self.cones:
            if cn.side == side:
                g = circle(cn.x, cn.y, cn.r_wide)
                pierce.append(g)
                holes.append(g)
                holes_open.append(g)
        return union_all(holes), union_all(pierce), union_all(holes_open)

    def _tent_regions(self, side, openings):
        hs = [h for h in self.holes if (h.tent_top if side == "top" else h.tent_bot)]
        if not hs:
            return EMPTY
        polys = list(iter_polys(openings))
        tree = shapely.STRtree(polys) if polys else None
        out = []
        for h in hs:
            p = Point(h.x, h.y)
            done = False
            if tree is not None:
                for i in tree.query(p):
                    poly = polys[int(i)]
                    if poly.contains(p) and poly.area <= 30 * math.pi * h.r_drill ** 2:
                        out.append(Polygon(poly.exterior))
                        done = True
                        break
            if not done:
                out.append(circle(h.x, h.y, h.r_drill + 0.05))
        return union_all(out)

    def _comp(self, name, color, solids, layer=None):
        if solids:
            self.components.append(dict(name=name, color=color, solids=solids, layer=layer or name))

    def build(self):
        OCC.load()
        o = self.opts
        st = self.stk.stack
        n = len(st)
        cache: Dict[tuple, object] = {}
        t0 = time.time()
        cu_img = {L.key: self.image(L) for L in st if L.kind == "copper"}
        for k, g in cu_img.items():
            self.svg[f"copper_{k}"] = g
        diel_extra: Dict[int, list] = defaultdict(list)
        stack_comps = []
        for i, L in enumerate(st):
            info(f"  building {L.name} ...", 2)
            if L.kind == "copper":
                solids = self.slab(cu_img[L.key], L.z0, L.z1, cache)
                stack_comps.append((i, L, solids))
                if 0 < i < n - 1:
                    fill = gdiff(self.B, cu_img[L.key])
                    tgt = self._fill_target(i)
                    if tgt is not None:
                        diel_extra[tgt].extend(self.slab(fill, L.z0, L.z1, cache))
            else:
                solids = self.slab(self.B, L.z0, L.z1, cache, label=f"dielectric_{L.key}")
                stack_comps.append((i, L, solids))
        for i, L, solids in stack_comps:
            if L.kind == "dielectric" and diel_extra.get(i):
                solids = solids + diel_extra[i]
            if L.kind == "copper":
                name = f"{L.name} (copper {L.t * 1000:.1f} um)"
                self._comp(name, o.copper_color, solids, L.name)
            else:
                bits = [b for b in (L.material, L.dielectric_type.lower() if L.dielectric_type else "") if b]
                bits.append(f"{L.t:.4f} mm")
                if L.dk:
                    bits.append(f"Dk {L.dk:g}")
                if L.df:
                    bits.append(f"Df {L.df:g}")
                self._comp(f"{L.name} ({', '.join(bits)})", o.board_color, solids, L.name)
        info(f"  stackup bodies built ({time.time() - t0:.1f} s)", 2)
        for side in ("top", "bottom"):
            self._build_coatings(side, cu_img, cache)
        self._build_hole_bodies()
        if self.cones:
            self._apply_cones()

    def _fill_target(self, i):
        st = self.stk.stack
        up = i - 1 if i - 1 >= 0 and st[i - 1].kind == "dielectric" else None
        dn = i + 1 if i + 1 < len(st) and st[i + 1].kind == "dielectric" else None
        for j in (dn, up):
            if j is not None and st[j].dielectric_type == "PREPREG":
                return j
        return dn if dn is not None else up

    def _build_coatings(self, side, cu_img, cache):
        o = self.opts
        st = self.stk
        coats = st.coats[side]
        sg = 1.0 if side == "top" else -1.0
        cu = st.top_cu if side == "top" else st.bot_cu
        surf = self._surface(side)
        if cu is not None:
            z_cu = cu.z1 if side == "top" else cu.z0
            z_lam = cu.z0 if side == "top" else cu.z1
            C = cu_img[cu.key]
        else:
            z_cu = z_lam = surf
            C = EMPTY
        holes, pierce, holes_open = self._coat_cuts(side)

        def zr(a, b):
            return (min(a, b), max(a, b))

        # solder mask
        masks = coats.get("mask", []) if not o.no_mask else []
        M = None
        if masks:
            openings = union_all(self.image(L) for L in masks)
            M = gdiff(self.B, openings)
            tents = self._tent_regions(side, openings)
            if not tents.is_empty:
                M = union_all([M, ginter(tents, self.B)])
            t_m = max(L.t for L in masks)
            lower = clean(gdiff(gdiff(M, C), holes))
            upper = clean(gdiff(M, pierce))
            self.svg[f"mask_{side}"] = upper
            solids = []
            if cu is not None:
                solids += Brep.prisms(lower, *zr(z_lam, z_cu))
            solids += Brep.prisms(upper, *zr(z_cu, z_cu + sg * t_m))
            L0 = masks[0]
            nm = f"{L0.name} (solder mask {t_m * 1000:.1f} um" + (f", Dk {L0.dk:g}" if L0.dk else "") + ")"
            self._comp(nm, o.mask_color, solids, L0.name)
            z_mask_top = z_cu + sg * t_m
        else:
            z_mask_top = None
        # surface finish
        fins = coats.get("finish", []) if not o.no_finish else []
        F = EMPTY
        t_f = 0.0
        fin_name, fin_mat = None, ""
        if fins:
            t_f = max(L.t for L in fins)
            fin_name = fins[0].name
            fin_mat = fins[0].material
            img = union_all(self.image(L) for L in fins)
            if img.is_empty:
                img = C if M is None else gdiff(C, M)
            F = ginter(img, C)
        elif o.finish_thickness and cu is not None:
            t_f = o.finish_thickness
            fin_name = f"{side.upper()}_SURFACE_FINISH"
            F = C if M is None else gdiff(C, M)
        if t_f > 0 and not F.is_empty:
            F = clean(gdiff(gdiff(F, holes_open), pierce))
            self.svg[f"finish_{side}"] = F
            solids = Brep.prisms(F, *zr(z_cu, z_cu + sg * t_f))
            col = o.finish_color if o.finish_color != "auto" else parse_color(finish_color_for(fin_mat))
            nm = f"{fin_name} (surface finish{', ' + fin_mat if fin_mat else ''}, {t_f * 1000:.2f} um)"
            self._comp(nm, col, solids, fin_name)
        # silkscreen
        silks = coats.get("silk", []) if not o.no_silk else []
        if silks:
            S = union_all(self.image(L) for L in silks)
            t_s = max(L.t for L in silks)
            S = gdiff(S, pierce)
            solids = []
            if M is not None:
                if not o.no_clip_silk:
                    S = ginter(S, M)
                S = clean(S)
                solids = Brep.prisms(S, *zr(z_mask_top, z_mask_top + sg * t_s))
            else:
                S = clean(gdiff(S, holes))
                lower = clean(gdiff(S, C))
                upper = clean(gdiff(S, F)) if t_f > 0 else S
                if cu is not None:
                    solids += Brep.prisms(lower, *zr(z_lam, z_cu))
                solids += Brep.prisms(upper, *zr(z_cu, z_cu + sg * t_s))
            self.svg[f"silk_{side}"] = S
            self._comp(f"{silks[0].name} (silkscreen {t_s * 1000:.1f} um)", o.silk_color, solids, silks[0].name)
        # solder paste
        pastes = coats.get("paste", []) if o.paste else []
        if pastes:
            P = clean(gdiff(union_all(self.image(L) for L in pastes), holes))
            t_p = max(L.t for L in pastes)
            z0 = z_cu + sg * t_f
            solids = Brep.prisms(P, *zr(z0, z0 + sg * t_p))
            self._comp(f"{pastes[0].name} (solder paste {t_p * 1000:.0f} um)", o.paste_color, solids, pastes[0].name)

    def _build_hole_bodies(self):
        o = self.opts
        tp = o.plating_thickness
        plating, fills, plugs, caps = [], [], [], []
        fill_mats = Counter()
        for h in self.holes:
            if not h.plated:
                continue
            ct = self._cap_t(h, "top") if h.cap_top > 0 else 0.0
            cb = self._cap_t(h, "bot") if h.cap_bot > 0 else 0.0
            zlo = h.z_lo_eff + cb
            zhi = h.z_hi_eff - ct
            bottom_cup = None
            if h.laser:
                # plated cup: barrel plus plating on the target land
                if h.entry == "top" and h.z_lo_eff == h.z_lo:
                    bottom_cup = (h.z_lo_eff, h.z_lo_eff + tp)
                elif h.entry == "bottom" and h.z_hi_eff == h.z_hi:
                    bottom_cup = (h.z_hi_eff - tp, h.z_hi_eff)
            if zhi - zlo <= 1e-6:
                continue
            if h.round:
                s = Brep.disc_prism(h.x, h.y, h.r_drill, h.r_fin, zlo, zhi)
                if s is not None:
                    plating.append(s)
                if bottom_cup and h.r_fin > 0:
                    s = Brep.disc_prism(h.x, h.y, h.r_fin, 0.0, *bottom_cup)
                    if s is not None:
                        plating.append(s)
            else:
                ring = clean(gdiff(h.geom_drill, h.geom_fin))
                plating += Brep.prisms(ring, zlo, zhi)
            if h.dual:
                r_dd, z_end, side = h.dual
                surf = self._surface(side)
                a, b = (z_end, surf) if side == "top" else (surf, z_end)
                s = Brep.disc_prism(h.x, h.y, r_dd, max(r_dd - tp, h.r_fin), a, b)
                if s is not None:
                    plating.append(s)
                fa, fb = (z_end, z_end + tp) if side == "top" else (z_end - tp, z_end)
                s = Brep.disc_prism(h.x, h.y, max(r_dd - tp, h.r_fin), h.r_fin, fa, fb)
                if s is not None:
                    plating.append(s)
            # fill / plugs inside the finished bore
            f_lo, f_hi = zlo, zhi
            if bottom_cup:
                if h.entry == "top":
                    f_lo = bottom_cup[1]
                else:
                    f_hi = bottom_cup[0]
            if h.r_fin > 1e-6 and f_hi - f_lo > 1e-6:
                if h.fill:
                    fill_mats[h.fill] += 1
                    if h.round:
                        s = Brep.disc_prism(h.x, h.y, h.r_fin, 0.0, f_lo, f_hi)
                        fills.append((h.fill, [s] if s is not None else []))
                    else:
                        fills.append((h.fill, Brep.prisms(h.geom_fin, f_lo, f_hi)))
                else:
                    Ltot = f_hi - f_lo
                    for frac, a, b in ((h.plug_top, f_hi - h.plug_top * Ltot, f_hi),
                                       (h.plug_bot, f_lo, f_lo + h.plug_bot * Ltot)):
                        if frac > 0:
                            if h.round:
                                s = Brep.disc_prism(h.x, h.y, h.r_fin, 0.0, a, b)
                                if s is not None:
                                    plugs.append(s)
                            else:
                                plugs += Brep.prisms(h.geom_fin, a, b)
            # caps close the drilled hole flush with the land surface (the land
            # copper thickness already includes the cap plating)
            for r, a, b in ((h.cap_top, h.z_hi - ct, h.z_hi), (h.cap_bot, h.z_lo, h.z_lo + cb)):
                if r <= 0 or b - a <= 1e-9:
                    continue
                if h.round:
                    s = Brep.disc_prism(h.x, h.y, h.r_drill, 0.0, a, b)
                    if s is not None:
                        caps.append(s)
                else:
                    caps += Brep.prisms(h.geom_drill, a, b)
        self._comp(f"Hole plating ({tp * 1000:.0f} um)", o.plating_color, plating, "PLATING")
        by_mat = defaultdict(list)
        for mat, ss in fills:
            by_mat[mat].extend(ss)
        for mat, ss in by_mat.items():
            col = o.fill_color if o.fill_color != "auto" else parse_color(
                {"copper": "copper", "silver": "silver"}.get(mat, "epoxy"))
            self._comp(f"Via fill ({mat})", col, ss, "VIA_FILL")
        col = o.fill_color if o.fill_color != "auto" else parse_color("epoxy")
        self._comp("Via plug (epoxy)", col, plugs, "VIA_PLUG")
        self._comp(f"Via caps ({o.cap_thickness * 1000:.0f} um)", o.cap_color, caps, "VIA_CAP")

    def _apply_cones(self):
        gp, BP = OCC.gp, OCC.BP
        tools = []
        for c in self.cones:
            sg = 1.0 if c.side == "top" else -1.0
            h = abs(c.z_wide - c.z_narrow)
            ax = gp.gp_Ax2(gp.gp_Pnt(c.x, c.y, c.z_narrow), gp.gp_Dir(0, 0, sg))
            cone = BP.BRepPrimAPI_MakeCone(ax, c.r_narrow, c.r_wide, h).Shape()
            ax2 = gp.gp_Ax2(gp.gp_Pnt(c.x, c.y, c.z_wide), gp.gp_Dir(0, 0, sg))
            cyl = BP.BRepPrimAPI_MakeCylinder(ax2, c.r_wide, 1.0).Shape()
            tb = bbox(cone)
            tools.append((cone, cyl, (c.x - c.r_wide, c.y - c.r_wide, c.x + c.r_wide, c.y + c.r_wide), tb))
        for comp in self.components:
            new = []
            for s in comp["solids"]:
                b = bbox(s)
                if b is None:
                    new.append(s)
                    continue
                x0, y0, z0, x1, y1, z1 = b
                hit = []
                for cone, cyl, (a0, b0, a1, b1), _ in tools:
                    if a1 < x0 or a0 > x1 or b1 < y0 or b0 > y1:
                        continue
                    hit += [cone, cyl]
                new.extend(cut_shapes(s, hit) if hit else [s])
            comp["solids"] = new

    # .. reporting ..............................................................
    def report(self):
        st = self.stk
        o = self.opts
        app = self.info.get("SAVE_APP") or self.info.get("ODB_SOURCE", "unknown source")
        ver = f"{self.info.get('ODB_VERSION_MAJOR', '?')}.{self.info.get('ODB_VERSION_MINOR', '?')}"
        info(f"\nodb2step {__version__}")
        info(f"Job      : {self.job.name}   ({app}, ODB++ {ver}, units {self.units})")
        others = [s for s in self.steps if s.lower() != self.step]
        info(f"Step     : {self.step}" + (f"   (other steps: {', '.join(others)})" if others else "")
             + (f"   [{len(self.instances)} step-repeat instances]" if len(self.instances) > 1 else ""))
        x0, y0, x1, y1 = self.B.bounds
        nh = sum(len(p.interiors) for p in iter_polys(self.B))
        info(f"Outline  : {x1 - x0:.3f} x {y1 - y0:.3f} mm, {len(list(iter_polys(self.B)))} island(s), {nh} cut-out(s)")
        info("\nStackup (z = 0 at the bottom of the lowest copper layer):")
        info(f"  {'layer':26s} {'kind':11s} {'thickness':>12s}  {'z range (mm)':>19s}  source")

        def row(L, kind, z0, z1):
            t = abs(z1 - z0)
            tt = f"{t * 1000:.2f} um" if t < 0.1 else f"{t:.4f} mm"
            note = ""
            if L.kind in ("mask", "silk", "finish", "paste") and not L.implicit:
                if self.raw_image(L).is_empty:
                    note = ("  [no features: finish on exposed copper]" if L.kind == "finish"
                            else "  [no features]" if L.kind != "mask" else "  [no openings]")
            info(f"  {L.name[:26]:26s} {kind:11s} {tt:>12s}  {min(z0, z1):9.4f}..{max(z0, z1):<9.4f}  {L.t_src}{note}")

        def coat_rows(side):
            cu = st.top_cu if side == "top" else st.bot_cu
            sg = 1 if side == "top" else -1
            surf = self._surface(side)
            z_cu = (cu.z1 if side == "top" else cu.z0) if cu else surf
            rows = []
            m = st.coats[side].get("mask", [])
            f = st.coats[side].get("finish", [])
            s = st.coats[side].get("silk", [])
            p = st.coats[side].get("paste", [])
            for L in f:
                rows.append((L, "finish", z_cu, z_cu + sg * L.t))
            zt = z_cu
            for L in m:
                rows.append((L, "mask", z_cu, z_cu + sg * L.t))
                zt = z_cu + sg * L.t
            for L in s:
                rows.append((L, "silk", zt, zt + sg * L.t))
            if o.paste:
                for L in p:
                    rows.append((L, "paste", z_cu, z_cu + sg * L.t))
            return rows
        for r in reversed(coat_rows("top")):
            row(*r)
        for L in st.stack:
            kind = L.kind + (" (impl.)" if L.implicit else "")
            row(L, kind, L.z0, L.z1)
            if L.kind == "dielectric" and (L.material or L.dk):
                extra = ", ".join(b for b in (L.material, L.dielectric_type.lower(),
                                              f"Dk {L.dk:g}" if L.dk else "", f"Df {L.df:g}" if L.df else "") if b)
                info(f"  {'':26s} {'':11s} {'':12s}  {'':19s}  [{extra}]")
        for r in coat_rows("bottom"):
            row(*r)
        tline = f"  board thickness, copper to copper: {st.total:.4f} mm"
        if st.board_attr:
            diff = st.total - st.board_attr
            tline += f"   (ODB .board_thickness {st.board_attr:.4f} mm, difference {diff * 1000:+.1f} um)"
        info(tline)
        for nt in st.notes:
            info("  note: " + nt)
        info("\nHoles:")
        groups = Counter()
        for h in self.holes:
            sp = f"{st.stack[h.top_idx].name}..{st.stack[h.bot_idx].name}"
            shape = "slot" if not h.round else "round"
            groups[(h.layer, sp, h.kind, shape, round(2 * h.r_fin, 4), round(2 * h.r_drill, 4), h.laser,
                    ipc_label(h) if h.plated else "")] += 1
        if not groups:
            info("  (no drill layers)")
        for (layer, sp, kind, shape, dfin, ddr, laser, prot), k in sorted(groups.items()):
            dr = f" (drill {ddr:.3f})" if ddr != dfin else ""
            info(f"  {layer}: {k} x {dfin:.3f} mm{dr} {kind.upper()}{' laser' if laser else ''}"
                 f"{' slot' if shape == 'slot' else ''} span {sp}{'; ' + prot if prot else ''}")
        for nt in self.hole_notes:
            info("  " + nt)
        for nt in self.prot_notes:
            info("  via protection: " + nt)
        if self.R.approx_used:
            warn("approximated symbol types (verify visually): " + ", ".join(sorted(self.R.approx_used)))


# --------------------------------------------------------------------------
# SVG dump (2D regions, for checking)
# --------------------------------------------------------------------------


def write_svgs(folder: str, regions: Dict[str, object], B):
    os.makedirs(folder, exist_ok=True)
    x0, y0, x1, y1 = B.bounds
    w, h = x1 - x0, y1 - y0
    for name, g in regions.items():
        if g is None or g.is_empty:
            continue
        paths = []
        for p in iter_polys(g):
            d = ""
            for ring in [p.exterior] + list(p.interiors):
                c = np.asarray(ring.coords)
                d += "M" + " L".join(f"{x:.5f},{y:.5f}" for x, y in c) + " Z "
            paths.append(f'<path d="{d}" fill-rule="evenodd"/>')
        fn = os.path.join(folder, re.sub(r"[^A-Za-z0-9_.-]+", "_", name) + ".svg")
        with open(fn, "w") as f:
            f.write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0 - 1} {-y1 - 1} {w + 2} {h + 2}" '
                    f'width="{(w + 2) * 10:.0f}" height="{(h + 2) * 10:.0f}">'
                    f'<g transform="scale(1,-1)" fill="#2a6" stroke="#000" stroke-width="0.01">'
                    + "".join(paths) + "</g></svg>")


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="odb2step",
        fromfile_prefix_chars="@",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Convert an ODB++ product model (folder, .tgz/.tar or .zip) to a coloured STEP model.",
        epilog="""via protection rules (--via-protection, repeatable, applied after the ODB++ data):
  TYPE[:key=value...]   TYPE = IPC-4761 type 1a 1b 2a 2b 3a 3b 4a 4b 5 6a 6b 7 or none
                        (roman numerals accepted: VII, VI-b, ...); 'a' types act on the top side
                        unless side=bottom is given
  keys: min=MM max=MM (finished hole diameter), layer=GLOB (drill layer name),
        kind=via|pth|any (default via), side=top|bottom
  e.g.  --via-protection 7:max=0.3     fill and cap every via up to 0.3 mm
        --via-protection 1b            tent all vias on both sides

counterhole rules by hole size (repeatable; they replace ODB++ counterholes on the same holes):
  --counterbore 3.2:6.0:1.5:bottom    3.2 mm holes get a 6.0 mm counterbore, 1.5 mm deep, from the bottom
  --countersink 3.4:6.5:90:top        3.4 mm holes get a 6.5 mm, 90 degree countersink from the top

options can be kept per board in a text file, one per line (e.g. --counterhole-type=countersink),
and passed as @file:  python odb2step.py board.tgz @board.args
""")
    p.add_argument("--version", action="version", version=f"odb2step {__version__}")
    p.add_argument("input", help="ODB++ product model: folder, .tgz/.tar.gz/.tar or .zip")
    p.add_argument("-o", "--output", help="output STEP file (default: <job>.step next to the input)")
    p.add_argument("--step", help="ODB++ step to export (default: 'pcb', else a step without step-and-repeat)")
    p.add_argument("--list", action="store_true", help="print stackup and hole report only, no STEP output")

    g = p.add_argument_group("thickness (mm); defaults apply only where the ODB++ data has no usable value")
    g.add_argument("--board-thickness", type=float, help="copper-to-copper thickness; scales dielectrics to match")
    g.add_argument("--copper-thickness", type=float, help="copper layers without .copper_weight (default 0.035)")
    g.add_argument("--mask-thickness", type=float, help="solder mask over copper (default 0.020)")
    g.add_argument("--silk-thickness", type=float, help="silkscreen (default 0.020)")
    g.add_argument("--finish-thickness", type=float,
                   help="finish thickness when the job gives none; without a finish layer this adds a "
                        "finish body on exposed copper (default: none)")
    g.add_argument("--paste-thickness", type=float, help="solder paste with --paste (default 0.120)")
    g.add_argument("--plating-thickness", type=float, default=DEFAULTS["plating"], help="hole wall plating (0.025)")
    g.add_argument("--cap-thickness", type=float, default=DEFAULTS["cap"], help="via cap plating (0.015)")
    g.add_argument("--layer-thickness", action="append", default=[], metavar="LAYER=MM",
                   help="force the thickness of one matrix layer (repeatable)")

    lg = p.add_argument_group("layers")
    lg.add_argument("--no-mask", action="store_true", help="omit solder mask")
    lg.add_argument("--no-silk", action="store_true", help="omit silkscreen")
    lg.add_argument("--no-finish", action="store_true", help="omit surface finish")
    lg.add_argument("--paste", action="store_true", help="include solder paste deposits")
    lg.add_argument("--no-clip-silk", action="store_true", help="keep silkscreen over mask openings")
    lg.add_argument("--rout", choices=["auto", "cut", "ignore"], default="auto",
                    help="rout layers: auto cuts internal rout paths only (default), cut all, or ignore")

    hg = p.add_argument_group("holes")
    hg.add_argument("--counterhole-type", choices=["counterbore", "countersink"], default="counterbore",
                    help="type of counterholes that neither NC drill data nor the ODB++ layer specify "
                         "(Altium ADD_TYPE=COUNTERHOLE): counterbore (default) or countersink")
    hg.add_argument("--countersink-angle", type=float, default=90.0,
                    help="included angle of such countersinks, degrees (90)")
    hg.add_argument("--counterhole-depth", type=float,
                    help="depth of such counterbores, mm from the outer copper (default: --counterhole-depth-mode)")
    hg.add_argument("--counterhole-depth-mode", choices=["expose", "through"], default="expose",
                    help="counterbores ending on an inner copper layer: stop on it (expose, default) "
                         "or cut through it")
    hg.add_argument("--nc-drill", action="append", default=[], metavar="PATH",
                    help="Altium NC drill output with counterhole type, depth and angle: a .DRR report (drill "
                         "files beside it), a folder or an archive. Default: a .DRR inside the ODB++ input, "
                         "else beside it named like the input (or the only one there)")
    hg.add_argument("--no-nc-drill", action="store_true", help="ignore NC drill output")
    hg.add_argument("--counterbore", action="append", default=[], metavar="HOLE:DIA:DEPTH[:SIDE]")
    hg.add_argument("--countersink", action="append", default=[], metavar="HOLE:DIA:ANGLE[:SIDE]")
    hg.add_argument("--backdrill-stub", type=float, default=0.2,
                    help="dielectric left between a backdrill and the must-not-cut layer, mm (0.2); "
                         "ODB .backdrill_max_stub_drill / .backdrill_max_depth take precedence")
    hg.add_argument("--via-protection", action="append", default=[], metavar="RULE", help="see below")
    hg.add_argument("--fill-material", choices=["epoxy", "copper", "silver"], default="epoxy")
    hg.add_argument("--plug-depth", type=float, default=0.3, help="plug depth as a fraction of the hole length")
    hg.add_argument("--drill-through-mask", action="store_true",
                    help="vias cut the solder mask even where the mask layer covers them (no tenting)")

    cg = p.add_argument_group("colours (name, #RRGGBB or R,G,B); names: " + ", ".join(sorted(NAMED_COLORS)))
    for key, dflt, hlp in (("board", "laminate", "dielectric"), ("copper", "copper", ""), ("mask", "green", ""),
                           ("silk", "white", ""), ("finish", "auto", "auto: from the finish material"),
                           ("plating", "copper", "hole walls"), ("fill", "auto", "auto: by fill material"),
                           ("cap", "#d8894a", ""), ("paste", "#a6a6a6", "")):
        cg.add_argument(f"--{key}-color", default=dflt, help=hlp or None)

    og = p.add_argument_group("geometry and output")
    og.add_argument("--arc-tolerance", type=float, default=0.001, help="tessellation chord error in mm (0.001)")
    og.add_argument("--arc-fit", choices=["loose", "strict", "off"], default="loose",
                    help="rebuild true arcs/circles: loose also fits arcs the CAD exported as polylines (default)")
    og.add_argument("--max-holes-per-face", type=int, default=1000,
                    help="split larger regions into touching tiles; STEP export time grows with the "
                         "square of the holes per face (default 1000, 0 = never split)")
    og.add_argument("--schema", choices=["AP214", "AP242", "AP203"], default="AP214")
    og.add_argument("--svg-dir", help="also write the 2D layer regions as SVG files for checking")
    og.add_argument("-v", "--verbose", action="count", default=1)
    og.add_argument("-q", "--quiet", action="store_true")
    return p


def finish_args(a):
    lt = {}
    for item in a.layer_thickness:
        if "=" not in item:
            raise SystemExit(f"--layer-thickness expects LAYER=MM, got '{item}'")
        k, v = item.split("=", 1)
        lt[k.strip()] = float(v)
    a.layer_thickness = lt
    try:
        a.via_rules = [parse_via_rule(s) for s in a.via_protection]
    except ValueError as exc:
        raise SystemExit(str(exc))
    for key in ("board", "copper", "mask", "silk", "plating", "cap", "paste"):
        setattr(a, f"{key}_color", parse_color(getattr(a, f"{key}_color")))
    for key in ("finish", "fill"):
        v = getattr(a, f"{key}_color")
        setattr(a, f"{key}_color", v if v == "auto" else parse_color(v))
    for spec in a.counterbore:
        _rule_floats(spec, "counterbore")
    for spec in a.countersink:
        _rule_floats(spec, "countersink")
    return a


def default_output(inp: str, job_name: str) -> str:
    folder = os.path.dirname(os.path.abspath(inp.rstrip("/\\")))
    cand = os.path.join(folder, job_name + ".step")
    if os.access(folder, os.W_OK):
        return cand
    return os.path.join(os.getcwd(), job_name + ".step")


def main(argv=None) -> int:
    args = finish_args(build_parser().parse_args(argv))
    Log.verbose = 0 if args.quiet else args.verbose
    Tess.tol = max(1e-5, args.arc_tolerance)
    ArcFit.enabled = args.arc_fit != "off"
    ArcFit.strict = args.arc_fit == "strict"
    Brep.max_holes = args.max_holes_per_face
    t0 = time.time()
    job = OdbJob(args.input)
    model = Model(job, args)
    model.prepare()
    model.report()
    if args.list:
        return 0
    info("\nBuilding solids ...")
    model.build()
    report_surfaces(model.R.surf_stats)
    if args.svg_dir:
        write_svgs(args.svg_dir, model.svg, model.B)
    out = args.output or default_output(args.input, job.name)
    info(f"Writing {out} ...")
    write_step(out, f"{job.name} {model.step}", model.components, args.schema)
    nsol = sum(len(c["solids"]) for c in model.components)
    info(f"\nDone in {time.time() - t0:.1f} s: {len(model.components)} bodies, {nsol} solids, "
         f"{ArcFit.stats['circles']} circles and {ArcFit.stats['arcs']} arcs rebuilt exactly, "
         f"{os.path.getsize(out) / 1e6:.1f} MB")
    for c in model.components:
        info(f"  {c['name']}: {len(c['solids'])} solid(s)", 2)
    if Log.warnings:
        info(f"{len(Log.warnings)} warning(s), see above")
    return 0


if __name__ == "__main__":
    sys.exit(main())
