# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""IPC-2581 reader (revision B and C, as Altium writes it): one XML file
(.cvg / .xml, optionally zipped) -> Board.

As the primary source it gives the stackup (thickness and material of every
layer), the outline (Step Profile), copper, mask, silkscreen and paste images
from the layer features and the padstack pads, drill holes with their spans
and plating, back drills (Spec Backdrill) and counterholes.

Next to ODB++ it is a secondary source: counterhole hints (type, angle and
floor layer per hole) read from the padstack hole diameters per layer
(PadStack/LayerHole). Altium gives the hole diameter at the face of each
layer nearest the counterhole's side: a countersink shows a diameter that
shrinks linearly with depth, a counterbore a constant diameter that drops to
the drill diameter below its floor.

Coordinates are converted to mm and taken relative to the Step Datum, which
puts them in the frame of the ODB++ export of the same design."""

from __future__ import annotations

import math
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np
from shapely import affinity
from shapely.geometry import Polygon

from .board import Backdrill, Board, Counterhole, CounterholeHint, Hole, Layer, RoutCut
from .common import PointIndex, name_tokens, warn
from .geometry import (EMPTY, _ellipse, _oval, _rect_shape, _thermal, arc_centerline, circle, gdiff, iter_polys,
                       make_valid, polys_only, square_line, stadium, thick_arc, union_all)

UNIT_MM = {"MILLIMETER": 1.0, "MM": 1.0, "MICRON": 0.001, "INCH": 25.4, "MILS": 0.0254, "MIL": 0.0254}

FUNCTION_KIND = {
    "SIGNAL": "copper", "PLANE": "copper", "MIXED": "copper", "CONDUCTOR": "copper", "CONDFOIL": "copper",
    "CONDFILM": "copper", "POWER_GROUND": "copper",
    "DIELCORE": "dielectric", "DIELPREG": "dielectric", "DIELBASE": "dielectric", "DIELADHV": "dielectric",
    "DIELECTRIC": "dielectric",
    "SOLDERMASK": "mask", "COVERLAY": "mask",
    "LEGEND": "silk", "SILKSCREEN": "silk",
    "PASTEMASK": "paste", "SOLDERPASTE": "paste",
    "COATINGCOND": "finish",
    "DRILL": "drill",
    "ROUTE": "rout", "V_CUT": "rout",
}
PREPREG_WORDS = {"pp", "prepreg", "preg", "bondply", "bond", "bondfilm"}


def is_ipc2581(path: str) -> bool:
    """True if the file (or the only XML in a zip) has an IPC-2581 root element."""
    try:
        head = _read_bytes(path)[:4096]
    except (OSError, ValueError):
        return False
    return re.search(rb"<\s*IPC-2581\b", head) is not None


def _read_bytes(path: str) -> bytes:
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith((".cvg", ".xml"))]
            if len(names) != 1:
                raise ValueError(f"{path}: expected one .cvg/.xml inside, found {len(names)}")
            return z.read(names[0])
    with open(path, "rb") as f:
        return f.read()


def _f(e, key, default=0.0) -> float:
    v = e.get(key) if e is not None else None
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _b(e, key, default=True) -> bool:
    v = e.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("true", "1", "yes")


class IpcDoc:
    """The parsed XML with namespaces stripped, units, dictionaries and the step."""

    def __init__(self, path: str):
        self.path = path
        data = _read_bytes(path)
        root = ET.fromstring(data)
        for el in root.iter():
            if isinstance(el.tag, str) and "}" in el.tag:
                el.tag = el.tag.split("}", 1)[1]
        self.root = root
        self.revision = root.get("revision", "?")
        hdr = root.find("Ecad/CadHeader")
        self.units = (hdr.get("units") if hdr is not None else None) or "MILLIMETER"
        self.k = UNIT_MM.get(self.units.upper(), 1.0)
        self.cad = root.find("Ecad/CadData")
        if self.cad is None:
            raise SystemExit(f"{path}: no Ecad/CadData section (not an IPC-2581 design file?)")
        steps = self.cad.findall("Step")
        if not steps:
            raise SystemExit(f"{path}: no Step in CadData")
        self.step = steps[0]
        self.other_steps = [s.get("name", "") for s in steps[1:]]
        dat = self.step.find("Datum")
        self.dx = _f(dat, "x") if dat is not None else 0.0
        self.dy = _f(dat, "y") if dat is not None else 0.0
        # dictionaries (each may have its own units)
        self.std: Dict[str, Tuple[ET.Element, float]] = {}
        self.user: Dict[str, Tuple[ET.Element, float]] = {}
        self.line: Dict[str, Tuple[ET.Element, float]] = {}
        self.fill: Dict[str, ET.Element] = {}
        content = root.find("Content")
        if content is not None:
            for dname, entry, store in (("DictionaryStandard", "EntryStandard", self.std),
                                        ("DictionaryUser", "EntryUser", self.user),
                                        ("DictionaryLineDesc", "EntryLineDesc", self.line)):
                for d in content.findall(dname):
                    k = UNIT_MM.get((d.get("units") or self.units).upper(), self.k)
                    for e in d.findall(entry):
                        kids = list(e)
                        if kids:
                            store[e.get("id", "")] = (kids[0], k)
            for d in content.findall("DictionaryFillDesc"):
                for e in d.findall("EntryFillDesc"):
                    kids = list(e)
                    if kids:
                        self.fill[e.get("id", "")] = kids[0]
        self.specs = {s.get("name", ""): s for s in (hdr.findall("Spec") if hdr is not None else [])}
        self.unsupported = Counter()

    def pt(self, x, y) -> Tuple[float, float]:
        """Board coordinates in mm, relative to the Datum."""
        return ((x - self.dx) * self.k, (y - self.dy) * self.k)


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


class Geo:
    """IPC-2581 feature elements -> shapely geometry (mm, board frame)."""

    def __init__(self, doc: IpcDoc):
        self.doc = doc

    # polygons ..............................................................
    def ring(self, poly_el, k: float, local: bool) -> np.ndarray:
        """Points of a Polygon / Cutout / Polyline: PolyBegin then
        PolyStepSegment / PolyStepCurve. local: primitive coordinates
        (no datum shift)."""
        pts: List[np.ndarray] = []
        cur = None

        def P(x, y):
            return (x * k, y * k) if local else self.doc.pt(x, y)
        for e in poly_el:
            t = e.tag
            if t == "PolyBegin":
                cur = P(_f(e, "x"), _f(e, "y"))
                pts.append(np.array([cur]))
            elif t == "PolyStepSegment" and cur is not None:
                cur = P(_f(e, "x"), _f(e, "y"))
                pts.append(np.array([cur]))
            elif t == "PolyStepCurve" and cur is not None:
                nxt = P(_f(e, "x"), _f(e, "y"))
                cx, cy = P(_f(e, "centerX"), _f(e, "centerY"))
                arc = arc_centerline(cur[0], cur[1], nxt[0], nxt[1], cx, cy, _b(e, "clockwise", False))
                pts.append(arc[1:])
                cur = nxt
        if not pts:
            return np.zeros((0, 2))
        return np.vstack(pts)

    def polygon(self, poly_el, k, local, cutouts=()):
        P = self.ring(poly_el, k, local)
        if len(P) < 3:
            return EMPTY
        g = make_valid(Polygon(P))
        for c in cutouts:
            C = self.ring(c, k, local)
            if len(C) >= 3:
                g = gdiff(g, make_valid(Polygon(C)))
        return polys_only(g)

    # line descriptions ..................................................
    def line_desc(self, el, k) -> Tuple[float, str]:
        ld = el.find("LineDesc")
        if ld is not None:
            return _f(ld, "lineWidth") * k, (ld.get("lineEnd") or "ROUND").upper()
        ref = el.find("LineDescRef")
        if ref is not None and ref.get("id") in self.doc.line:
            ld, kk = self.doc.line[ref.get("id")]
            return _f(ld, "lineWidth") * kk, (ld.get("lineEnd") or "ROUND").upper()
        return 0.0, "ROUND"

    def stroke_pts(self, P: np.ndarray, w: float, end: str):
        """A polyline drawn with width w; round, square or flat (NONE) ends."""
        if w <= 0 or len(P) < 2:
            return EMPTY
        if end == "SQUARE":
            parts = [square_line(P[i, 0], P[i, 1], P[i + 1, 0], P[i + 1, 1], w) for i in range(len(P) - 1)]
        else:
            parts = [stadium(P[i, 0], P[i, 1], P[i + 1, 0], P[i + 1, 1], w / 2.0) for i in range(len(P) - 1)]
        g = union_all(parts)
        if end in ("NONE", "FLAT"):
            # flat ends: trim the round caps at both ends of the path
            for (a, b) in ((P[0], P[1]), (P[-1], P[-2])):
                d = np.asarray(a) - np.asarray(b)
                L = float(np.hypot(*d))
                if L < 1e-12:
                    continue
                u = d / L
                n = np.array([-u[1], u[0]])
                c = np.asarray(a)
                q = [c + n * w, c + n * w + u * w, c - n * w + u * w, c - n * w]
                g = gdiff(g, Polygon(q))
        return g

    # primitives in local coordinates (mm) ...............................
    def primitive(self, el, k):
        t = el.tag
        a = lambda key, d=0.0: _f(el, key, d) * k   # noqa: E731
        if t == "Circle":
            return circle(0.0, 0.0, a("diameter") / 2.0)
        if t == "RectCenter":
            return _rect_shape(a("width"), a("height"))
        if t in ("RectRound", "RectCham"):
            corners = "".join(c for c, key in (("1", "upperRight"), ("2", "upperLeft"), ("3", "lowerLeft"),
                                               ("4", "lowerRight")) if _b(el, key, True))
            rad = a("radius") if t == "RectRound" else a("chamfer")
            return _rect_shape(a("width"), a("height"), rad, corners, chamfer=(t == "RectCham"))
        if t == "RectCorner":
            x0, y0, x1, y1 = a("lowerLeftX"), a("lowerLeftY"), a("upperRightX"), a("upperRightY")
            return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        if t == "Oval":
            return _oval(a("width"), a("height"))
        if t == "Ellipse":
            return _ellipse(a("width"), a("height"))
        if t == "Diamond":
            w, h = a("width") / 2.0, a("height") / 2.0
            return Polygon([(w, 0), (0, h), (-w, 0), (0, -h)])
        if t in ("Hexagon", "Octagon"):
            n = 6 if t == "Hexagon" else 8
            r = a("length") / 2.0
            off = 0.0 if n == 6 else math.pi / 8
            return Polygon([(r * math.cos(off + 2 * math.pi * i / n), r * math.sin(off + 2 * math.pi * i / n))
                            for i in range(n)])
        if t == "Triangle":
            b, h = a("base"), a("height")
            return Polygon([(-b / 2, -h / 2), (b / 2, -h / 2), (0, h / 2)])
        if t in ("Donut", "Thermal"):
            shape = (el.get("shape") or "ROUND").upper()
            od, idm = a("outerDiameter"), a("innerDiameter")
            outer = self._shape(shape, od)
            ring = gdiff(outer, self._shape(shape, idm))
            if t == "Donut":
                return ring
            return _thermal(ring, od / 2.0, _f(el, "spokeStartAngle", 0.0), _f(el, "spokeCount", 4),
                            a("gap"))
        if t == "Butterfly":
            shape = (el.get("shape") or "ROUND").upper()
            s = a("diameter") or a("side")
            full = self._shape(shape, s)
            q1 = Polygon([(0, 0), (s, 0), (s, s), (0, s)])
            q3 = Polygon([(0, 0), (-s, 0), (-s, -s), (0, -s)])
            return union_all([full.intersection(q1), full.intersection(q3)])
        if t == "Moire":
            self.doc.unsupported["Moire (outer ring only)"] += 1
            return circle(0.0, 0.0, a("diameter") / 2.0)
        if t == "Contour":
            pe = el.find("Polygon")
            if pe is None:
                return EMPTY
            return self.polygon(pe, k, True, el.findall("Cutout"))
        if t in ("Line", "Arc", "Polyline", "Outline", "UserSpecial"):
            return self.user_feature(el, k, local=True)
        self.doc.unsupported[t] += 1
        return EMPTY

    def _shape(self, shape, d):
        if d <= 0:
            return EMPTY
        if shape == "SQUARE":
            return _rect_shape(d, d)
        if shape in ("HEXAGON", "OCTAGON"):
            n = 6 if shape == "HEXAGON" else 8
            r = d / 2.0
            return Polygon([(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)) for i in range(n)])
        return circle(0.0, 0.0, d / 2.0)

    def user_feature(self, el, k, local=False):
        """Line, Arc, Polyline, Outline, UserSpecial (a group of these)."""
        t = el.tag
        P = (lambda x, y: (x * k, y * k)) if local else self.doc.pt
        if t == "UserSpecial":
            return union_all(self.any_feature(c, k, local) for c in el)
        if t == "Line":
            w, end = self.line_desc(el, k)
            x0, y0 = P(_f(el, "startX"), _f(el, "startY"))
            x1, y1 = P(_f(el, "endX"), _f(el, "endY"))
            return self.stroke_pts(np.array([[x0, y0], [x1, y1]]), w, end)
        if t == "Arc":
            w, end = self.line_desc(el, k)
            sx, sy = P(_f(el, "startX"), _f(el, "startY"))
            ex, ey = P(_f(el, "endX"), _f(el, "endY"))
            cx, cy = P(_f(el, "centerX"), _f(el, "centerY"))
            cw = _b(el, "clockwise", False)
            if w <= 0:
                return EMPTY
            if end == "SQUARE" or end in ("NONE", "FLAT"):
                return self.stroke_pts(arc_centerline(sx, sy, ex, ey, cx, cy, cw), w, end)
            return thick_arc(sx, sy, ex, ey, cx, cy, cw, w / 2.0)
        if t == "Polyline":
            w, end = self.line_desc(el, k)
            return self.stroke_pts(self.ring(el, k, local), w, end)
        if t == "Outline":
            pe = el.find("Polygon")
            if pe is None:
                return EMPTY
            w, end = self.line_desc(el, k)
            P2 = self.ring(pe, k, local)
            if len(P2) < 3:
                return EMPTY
            return union_all([make_valid(Polygon(P2)), self.stroke_pts(P2, w, end)])
        return self.any_feature(el, k, local)

    def any_feature(self, el, k, local=False):
        t = el.tag
        if t in ("Line", "Arc", "Polyline", "Outline", "UserSpecial"):
            return self.user_feature(el, k, local)
        if t == "Contour":
            pe = el.find("Polygon")
            if pe is None:
                return EMPTY
            return self.polygon(pe, k, local, el.findall("Cutout"))
        if t == "Text":
            self.doc.unsupported["Text (left out)"] += 1
            return EMPTY
        if t in ("StandardPrimitiveRef", "UserPrimitiveRef"):
            store = self.doc.std if t == "StandardPrimitiveRef" else self.doc.user
            ent = store.get(el.get("id", ""))
            if ent is None:
                self.doc.unsupported[f"{t} {el.get('id', '')} (not in the dictionary)"] += 1
                return EMPTY
            prim, kk = ent
            return self.primitive(prim, kk)
        if t in self.META or t in ("LineDesc", "LineDescRef"):
            return None
        return self.primitive(el, k)

    PRIMITIVES = {"Circle", "RectCenter", "RectRound", "RectCham", "RectCorner", "Oval", "Ellipse", "Diamond",
                  "Hexagon", "Octagon", "Triangle", "Donut", "Thermal", "Butterfly", "Moire",
                  "StandardPrimitiveRef", "UserPrimitiveRef"}
    META = {"Xform", "Location", "PinRef", "NonstandardAttribute", "SpecRef", "ColorRef", "FillDesc",
            "FillDescRef"}

    def placed(self, parent):
        """The shape(s) of a Features, LayerPad or SlotCavity element. With a
        Location, shapes are in primitive coordinates: Xform (scale, mirror,
        rotation, offset) is applied, then the shape moves to the Location.
        Without one, contours and lines are in board coordinates."""
        k = self.doc.k
        xf = parent.find("Xform")
        loc = parent.find("Location")
        lx, ly = (_f(loc, "x"), _f(loc, "y")) if loc is not None else (0.0, 0.0)
        out = []
        for c in parent:
            if c.tag in self.META:
                continue
            local = loc is not None or c.tag in self.PRIMITIVES
            g = self.any_feature(c, k, local=local)
            if g is None or g.is_empty:
                continue
            if local:
                g = self.transform(g, xf, lx, ly)
            out.append(g)
        return union_all(out)

    def transform(self, g, xf, lx, ly):
        """Primitive -> board: scale, mirror (x -> -x), rotate counterclockwise,
        offset, then move to the Location (lx, ly in file units)."""
        if xf is not None:
            s = _f(xf, "scale", 1.0) or 1.0
            if s != 1.0:
                g = affinity.scale(g, s, s, origin=(0, 0))
            if _b(xf, "mirror", False):
                g = affinity.scale(g, -1.0, 1.0, origin=(0, 0))
            rot = _f(xf, "rotation", 0.0)
            if rot:
                g = affinity.rotate(g, rot, origin=(0, 0))
            ox, oy = _f(xf, "xOffset") * self.doc.k, _f(xf, "yOffset") * self.doc.k
            if ox or oy:
                g = affinity.translate(g, ox, oy)
        x, y = self.doc.pt(lx, ly)
        return affinity.translate(g, x, y)

    def set_geometry(self, s):
        """All feature geometry of a Set."""
        parts = []
        for fe in s.findall("Features"):
            g = self.placed(fe)
            if g is not None and not g.is_empty:
                parts.append(g)
        return parts

    def points(self, s) -> List[Tuple[float, float, float]]:
        """(x, y, size) of each primitive of a Set (document layers): arcs by
        their centre, lines by their middle, shapes by their centroid. Group
        features (UserSpecial) are taken apart, so markers drawn next to each
        other are not merged."""
        out = []
        k = self.doc.k
        for fe in s.findall("Features"):
            loc = fe.find("Location")
            for c in fe:
                if c.tag in self.META:
                    continue
                if loc is None and c.tag in ("Arc", "Line", "UserSpecial"):
                    out.extend(self._prim_points(c, k))
                    continue
                g = self.placed_one(fe, c)
                for p in iter_polys(g):
                    cc = p.centroid
                    x0, y0, x1, y1 = p.bounds
                    out.append((cc.x, cc.y, min(x1 - x0, y1 - y0)))
        return out

    def _prim_points(self, el, k):
        out = []
        if el.tag == "UserSpecial":
            for c in el:
                out.extend(self._prim_points(c, k))
            return out
        w, _ = self.line_desc(el, k)
        if el.tag == "Arc":
            cx, cy = self.doc.pt(_f(el, "centerX"), _f(el, "centerY"))
            sx, sy = self.doc.pt(_f(el, "startX"), _f(el, "startY"))
            out.append((cx, cy, 2.0 * math.hypot(sx - cx, sy - cy) + w))
        elif el.tag == "Line":
            x0, y0 = self.doc.pt(_f(el, "startX"), _f(el, "startY"))
            x1, y1 = self.doc.pt(_f(el, "endX"), _f(el, "endY"))
            out.append(((x0 + x1) / 2.0, (y0 + y1) / 2.0, max(math.hypot(x1 - x0, y1 - y0), w)))
        else:
            g = self.any_feature(el, k)
            for p in iter_polys(g):
                cc = p.centroid
                x0, y0, x1, y1 = p.bounds
                out.append((cc.x, cc.y, min(x1 - x0, y1 - y0)))
        return out

    def placed_one(self, parent, child):
        """One shape of a Features element, placed like placed() does."""
        loc = parent.find("Location")
        local = loc is not None or child.tag in self.PRIMITIVES
        g = self.any_feature(child, self.doc.k, local=local)
        if g is None or g.is_empty or not local:
            return g
        lx, ly = (_f(loc, "x"), _f(loc, "y")) if loc is not None else (0.0, 0.0)
        return self.transform(g, parent.find("Xform"), lx, ly)


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------


class IpcReader:
    def __init__(self, path: str, opts=None):
        self.opts = opts
        self.doc = IpcDoc(path)
        self.geo = Geo(self.doc)
        d = self.doc
        self.layer_el = {L.get("name", ""): L for L in d.cad.findall("Layer")}
        self.features: Dict[str, List[ET.Element]] = defaultdict(list)
        for lf in d.step.findall("LayerFeature"):
            self.features[lf.get("layerRef", "")].append(lf)
        self.pads: Dict[str, List[ET.Element]] = defaultdict(list)
        # via padstacks (a LayerHole with platingStatus VIA, no component pin): their pads on
        # a solder mask layer are the vias' own openings (a via tented in the data has none)
        self._via_lp = set()
        self._n_via_ps = 0
        for ps in d.step.findall("PadStack"):
            via = any((lh.get("platingStatus") or "").upper() == "VIA" for lh in ps.findall("LayerHole")) \
                and ps.find(".//PinRef") is None
            self._n_via_ps += via
            for lp in ps.findall("LayerPad"):
                self.pads[lp.get("layerRef", "")].append(lp)
                if via:
                    self._via_lp.add(id(lp))
        self._via_sets = any(_is_via_set(st) for lfs in self.features.values() for lf in lfs
                             for st in lf.findall("Set"))
        self._sets_img: Dict[tuple, object] = {}
        self._stackup()

    # .. stackup ............................................................
    def _stackup(self):
        d = self.doc
        seq = []
        self.overall, self.where = None, ""
        st = d.cad.find("Stackup")
        if st is not None:
            if st.get("overallThickness"):
                self.overall = _f(st, "overallThickness") * d.k
            self.where = (st.get("whereMeasured") or "").upper()
            for grp in st.findall("StackupGroup"):
                for sl in grp.findall("StackupLayer"):
                    ref = sl.get("layerOrGroupRef", "")
                    spec = sl.find("SpecRef")
                    seq.append((_f(sl, "sequence", len(seq) + 1), ref, sl.get("thickness"),
                                spec.get("id") if spec is not None else ref))
        seq.sort(key=lambda t: t[0])
        self.seq = seq
        self.thick = {ref: (float(t) * d.k if t not in (None, "") else None) for _, ref, t, _ in seq}
        self.spec_of = {ref: sid for _, ref, _, sid in seq}

    def _spec_props(self, name) -> Tuple[str, Optional[float], Optional[float]]:
        """(material, Dk, Df) from the Spec of a layer."""
        sp = self.doc.specs.get(name)
        if sp is None:
            return "", None, None
        mat, dk, df = "", None, None
        for g in sp:
            typ = (g.get("type") or "").upper()
            props = g.findall("Property")
            if g.tag == "General" and typ == "MATERIAL" and props:
                mat = props[0].get("text") or props[0].get("value") or ""
            elif g.tag == "Dielectric" and props:
                v = props[0].get("value")
                try:
                    if typ == "DIELECTRIC_CONSTANT":
                        dk = float(v)
                    elif typ == "LOSS_TANGENT":
                        df = float(v)
                except (TypeError, ValueError):
                    pass
        if mat.strip().lower() == "undefined":
            mat = ""
        return mat, dk, df

    def layers(self) -> List[Layer]:
        order = [ref for _, ref, _, _ in self.seq if ref in self.layer_el]
        order += [n for n in self.layer_el if n not in order]
        out = []
        for row, name in enumerate(order):
            el = self.layer_el[name]
            func = (el.get("layerFunction") or "").upper()
            kind = FUNCTION_KIND.get(func, "doc")
            side = (el.get("side") or "").upper()
            L = Layer(row=row + 1, name=name, kind=kind, type=func,
                      polarity=(el.get("polarity") or "POSITIVE").upper(), context="BOARD")
            L.side_hint = {"TOP": "top", "BOTTOM": "bottom"}.get(side, "")
            sp = el.find("SpanType")
            if sp is not None:
                L.start, L.end = sp.get("fromLayer", ""), sp.get("toLayer", "")
            t = self.thick.get(name)
            mat, dk, df = self._spec_props(self.spec_of.get(name, name))
            L.material, L.dk, L.df = mat, dk, df
            if kind == "copper":
                if t is not None:
                    L.t_data, L.t_data_src = t, "IPC-2581 stackup"
                L.material = ""
            elif kind == "dielectric":
                if t:
                    L.t_data, L.t_data_src = t, "IPC-2581 stackup"
                toks = name_tokens(name)
                if func == "DIELPREG" or toks & PREPREG_WORDS or re.search(r"prepreg|bond\s*ply", name.lower()):
                    L.dielectric_type = "PREPREG"
                else:
                    L.dielectric_type = "CORE"
                L.dielectric_name = mat
            elif kind in ("mask", "silk", "finish", "paste"):
                if t:
                    L.t_data, L.t_data_src = t, "IPC-2581 stackup"
            if kind in ("copper", "mask", "silk", "finish", "paste", "rout", "doc"):
                L.image_fn = (lambda nm=name: self.image(nm))
                L.empty = not self._has_geometry(name)
            if kind == "doc":
                L.points_fn = (lambda L=L: self.doc_points(L))
            if kind == "mask":
                L.mask_split_fn = (lambda nm=name: self.mask_split(nm))
            out.append(L)
        return out

    def _has_geometry(self, name) -> bool:
        if self.pads.get(name):
            return True
        for lf in self.features.get(name, ()):
            for s in lf.findall("Set"):
                if s.find("Features") is not None:
                    return True
        return False

    # .. images ..............................................................
    def _sets_image(self, name: str, skip_vias: bool = False):
        """The feature sets of a layer in order (NEGATIVE sets subtract);
        skip_vias leaves out the sets with padUsage VIA."""
        key = (name, skip_vias)
        acc = self._sets_img.get(key)
        if acc is not None:
            return acc
        acc = EMPTY
        batch, pol = [], "P"
        for lf in self.features.get(name, ()):
            for s in lf.findall("Set"):
                if skip_vias and _is_via_set(s):
                    continue
                p = "N" if (s.get("polarity") or "POSITIVE").upper().startswith("NEG") else "P"
                parts = self.geo.set_geometry(s)
                if not parts:
                    continue
                if p != pol and batch:
                    acc = _apply(acc, batch, pol)
                    batch = []
                pol = p
                batch.extend(parts)
        if batch:
            acc = _apply(acc, batch, pol)
        self._sets_img[key] = acc
        return acc

    def image(self, name: str):
        """Layer image: the feature sets, then the padstack pads on this layer."""
        acc = self._sets_image(name)
        pads = [self.geo.placed(lp) for lp in self.pads.get(name, ())]
        if pads:
            acc = union_all([acc] + pads)
        return acc

    def mask_split(self, name: str):
        """Solder mask layer: (openings of everything but the vias, [(x, y, opening)]
        of the vias' own openings: the pads of the via padstacks and the sets with
        padUsage VIA). None when the file has neither, so it does not say which
        openings are vias."""
        if not (self._n_via_ps or self._via_sets):
            return None
        other, own = [self._sets_image(name, skip_vias=True)], []
        for lf in self.features.get(name, ()):
            for s in lf.findall("Set"):
                if _is_via_set(s):
                    for g in self.geo.set_geometry(s) or ():
                        if g is not None and not g.is_empty:
                            c = g.centroid
                            own.append((c.x, c.y, g))
        for lp in self.pads.get(name, ()):
            g = self.geo.placed(lp)
            if g is None or g.is_empty:
                continue
            if id(lp) in self._via_lp:
                c = g.centroid
                own.append((c.x, c.y, g))
            else:
                other.append(g)
        return union_all(other), own

    def doc_points(self, L: Layer):
        """Markers of a document layer (Filling, Capping, ...). Altium writes a
        via's IPC-4761 features as pads of their own padstacks on these layers;
        when the layer has such pads only they count, since the drawn features
        of the layer are artwork (footprint drawings, outlines). Without pads,
        every drawn primitive is a marker."""
        name = L.name
        pads = self.pads.get(name, ())
        if pads:
            out = []
            for lp in pads:
                g = self.geo.placed(lp)
                if g is None or g.is_empty:
                    continue
                c = g.centroid
                x0, y0, x1, y1 = g.bounds
                out.append((c.x, c.y, min(x1 - x0, y1 - y0)))
            drawn = sum(len(s.findall("Features")) for lf in self.features.get(name, ()) for s in lf.findall("Set"))
            L.points_note = f"{len(out)} via markers from the padstacks" + (
                f"; {drawn} drawn feature group(s) on the layer ignored" if drawn else "")
            return out
        out = []
        for lf in self.features.get(name, ()):
            for s in lf.findall("Set"):
                out.extend(self.geo.points(s))
        L.points_note = f"{len(out)} drawn features" if out else ""
        return out

    def outline(self):
        prof = self.doc.step.find("Profile")
        if prof is None:
            return EMPTY
        parts = []
        for pe in prof.findall("Polygon"):
            parts.append(self.geo.polygon(pe, self.doc.k, False, prof.findall("Cutout")))
        return union_all(parts)

    # .. holes ..............................................................
    def _drill_layers(self):
        return [n for n, el in self.layer_el.items() if (el.get("layerFunction") or "").upper() == "DRILL"]

    def _is_backdrill_layer(self, name: str) -> bool:
        toks = name_tokens(name)
        return bool(toks & {"bd", "backdrill", "backdrills"}) or "[bd]" in name.lower() or \
            "back drill" in name.lower()

    def _backdrill_specs(self) -> Dict[frozenset, dict]:
        out = {}
        for nm, sp in self.doc.specs.items():
            bd = {}
            for g in sp.findall("Backdrill"):
                typ = (g.get("type") or "").upper()
                p = g.find("Property")
                if p is None:
                    continue
                if typ in ("START_LAYER", "MUST_NOT_CUT_LAYER"):
                    bd[typ] = p.get("layerOrGroupRef") or p.get("text") or ""
                elif typ in ("MAX_STUB_LENGTH", "MAX_DEPTH", "DEPTH"):
                    v = p.get("value")
                    try:
                        bd[typ] = float(v) * self.doc.k if v not in (None, "") else None
                    except ValueError:
                        bd[typ] = None
            if bd.get("START_LAYER") and bd.get("MUST_NOT_CUT_LAYER"):
                bd["name"] = nm
                out[frozenset((bd["START_LAYER"].lower(), bd["MUST_NOT_CUT_LAYER"].lower()))] = bd
        return out

    def _z_faces(self):
        """Copper/dielectric layers top-down with their top and bottom z
        (from the IPC-2581 thicknesses; z = 0 at the bottom)."""
        stack = [L for L in self._layers if L.kind in ("copper", "dielectric")]
        z = sum((L.t_data or 0.0) for L in stack)
        top = z
        out = {}
        for L in stack:
            t = L.t_data or 0.0
            out[L.key] = (z, z - t)
            z -= t
        return out, top, [L for L in stack]

    def read(self) -> Board:
        d = self.doc
        self._layers = self.layers()
        board = Board(name=os.path.splitext(os.path.basename(d.path))[0], source=self._source_label(),
                      layers=self._layers, units="MM")
        board.step = d.step.get("name", "")
        board.other_steps = d.other_steps
        board.outline = self.outline()
        if self.overall:
            body = sum(self.thick.get(L.name) or 0.0 for L in self._layers if L.kind in ("copper", "dielectric"))
            allsum = sum(v or 0.0 for v in self.thick.values())
            if self.where == "METAL":
                board.board_thickness = self.overall
                board.board_thickness_src = "IPC-2581 overallThickness (METAL)"
            else:
                what = ("the sum of all stackup layers including coatings" if abs(allsum - self.overall) < 0.0005
                        else "copper to copper" if abs(body - self.overall) < 0.0005 else "not the stack sum")
                board.notes.append(f"IPC-2581 Stackup overallThickness {self.overall:.4f} mm "
                                   f"(whereMeasured {self.where or '?'}) is {what}")
        self._holes(board)
        board.counterholes = self.counterholes()
        for L in self._layers:
            if L.kind == "rout":
                board.routs.append(RoutCut(region=(lambda nm=L.name: self.image(nm)), layer=L.name,
                                           start=L.start, end=L.end))
        board.report_extra = self._report_unsupported
        if d.dx or d.dy:
            board.notes.append(f"coordinates relative to the IPC-2581 Datum ({d.dx:g}, {d.dy:g})")
        return board

    def _source_label(self) -> str:
        d = self.doc
        return f"IPC-2581 rev {d.revision} ({os.path.basename(d.path)}, units {d.units})"

    def _holes(self, board: Board):
        d = self.doc
        specs = self._backdrill_specs()
        spec_by_hole = {}
        bds = []
        n_drill = 0
        for lname in self._drill_layers():
            el = self.layer_el[lname]
            sp = el.find("SpanType")
            start, end = (sp.get("fromLayer", ""), sp.get("toLayer", "")) if sp is not None else ("", "")
            is_bd = self._is_backdrill_layer(lname)
            for lf in self.features.get(lname, ()):
                for s in lf.findall("Set"):
                    for h in s.findall("Hole"):
                        x, y = d.pt(_f(h, "x"), _f(h, "y"))
                        dia = _f(h, "diameter") * d.k
                        status = (h.get("platingStatus") or "PLATED").upper()
                        if is_bd:
                            bds.append((x, y, dia, lname, start, end))
                            continue
                        plated = status != "NONPLATED"
                        kind = "via" if status == "VIA" else ("pth" if plated else "npth")
                        hole = Hole(x=x, y=y, r_fin=dia / 2.0, plated=plated, kind=kind, layer=lname,
                                    start=start, end=end)
                        sref = h.find("SpecRef")
                        if sref is not None and sref.get("id") in d.specs:
                            spec_by_hole[(round(x, 3), round(y, 3))] = sref.get("id")
                        board.holes.append(hole)
                        n_drill += 1
                    for sc in s.findall("SlotCavity"):
                        g = self.geo.placed(sc)
                        if g is None or g.is_empty:
                            continue
                        status = (sc.get("platingStatus") or "PLATED").upper()
                        plated = status != "NONPLATED"
                        c = g.centroid
                        x0, y0, x1, y1 = g.bounds
                        hole = Hole(x=c.x, y=c.y, r_fin=min(x1 - x0, y1 - y0) / 2.0, plated=plated,
                                    kind="pth" if plated else "npth", layer=lname, start=start, end=end,
                                    geom_fin=g)
                        board.holes.append(hole)
                        n_drill += 1
        if n_drill == 0:
            self._padstack_holes(board)
        for (x, y, dia, lname, start, end) in bds:
            b = Backdrill(x=x, y=y, d=dia, layer=lname, start=start, end=end, src=f"IPC-2581 layer {lname}")
            spec = specs.get(frozenset((start.lower(), end.lower())))
            if spec is not None:
                self._apply_bd_spec(b, spec)
            board.backdrills.append(b)
        if bds and specs:
            used = {frozenset((b.start.lower(), b.end.lower())) for b in board.backdrills}
            for key, sp in specs.items():
                if key not in used:
                    board.notes.append(f"IPC-2581 back drill spec '{sp['name']}' has no back drill layer")

    def _apply_bd_spec(self, b: Backdrill, spec: dict):
        faces, top, stack = self._z_faces()
        start, mnc = spec["START_LAYER"], spec["MUST_NOT_CUT_LAYER"]
        b.mnc = mnc
        b.src = f"IPC-2581 spec '{spec['name']}'"
        names = [L.key for L in stack]
        if start.lower() in names and mnc.lower() in names:
            si, mi = names.index(start.lower()), names.index(mnc.lower())
            b.dir_hint = "top" if si < mi else "bottom"
            s_face = faces[start.lower()][0] if si < mi else faces[start.lower()][1]
            m_face = faces[mnc.lower()][0] if si < mi else faces[mnc.lower()][1]
            to_mnc = abs(s_face - m_face)
            v = spec.get("MAX_STUB_LENGTH")
            if v is not None and v > 0:
                if abs(v - to_mnc) <= 0.005:
                    # Altium writes the drill depth (start face to the must-not-cut face) here
                    pass
                elif v < to_mnc:
                    b.stub = v
                    b.src += " MAX_STUB_LENGTH"

    def _padstack_holes(self, board: Board):
        """No drill layers: holes from the padstacks (LayerHole spans)."""
        d = self.doc
        seen = PointIndex(0.002)
        for ps in d.step.findall("PadStack"):
            for lh in ps.findall("LayerHole"):
                sp = lh.find("Span")
                start, end = (sp.get("fromLayer", ""), sp.get("toLayer", "")) if sp is not None else ("", "")
                if start and start == end:
                    continue                   # per-layer entries of a counterhole profile
                x, y = d.pt(_f(lh, "x"), _f(lh, "y"))
                dia = _f(lh, "diameter") * d.k
                if any(abs(2 * o.r_fin - dia) < 1e-4 for o in seen.find_all(x, y)):
                    continue
                status = (lh.get("platingStatus") or "PLATED").upper()
                plated = status != "NONPLATED"
                h = Hole(x=x, y=y, r_fin=dia / 2.0, plated=plated,
                         kind="via" if status == "VIA" else ("pth" if plated else "npth"),
                         layer="padstacks", start=start, end=end)
                seen.add(x, y, h)
                board.holes.append(h)
        if board.holes:
            board.notes.append(f"no IPC-2581 drill layer: {len(board.holes)} holes taken from the padstacks")

    # .. counterholes from padstack hole profiles .........................
    def _profiles(self):
        """Per padstack: (name, x, y, [(layer key, diameter)]) for padstacks
        that list one hole entry per layer."""
        d = self.doc
        for ps in d.step.findall("PadStack"):
            ent = []
            for lh in ps.findall("LayerHole"):
                sp = lh.find("Span")
                if sp is None or sp.get("fromLayer") != sp.get("toLayer"):
                    continue
                ent.append(lh)
            if len(ent) < 2:
                continue
            by_pos = defaultdict(list)
            for lh in ent:
                x, y = d.pt(_f(lh, "x"), _f(lh, "y"))
                by_pos[(round(x, 4), round(y, 4))].append((lh.find("Span").get("fromLayer", "").lower(),
                                                           _f(lh, "diameter") * d.k, lh.get("name", "")))
            for (x, y), rows in by_pos.items():
                nm = re.sub(r"\s*counter\s*hole.*$", "", rows[0][2], flags=re.I).strip() or rows[0][2]
                yield nm, x, y, rows

    def counter_analysis(self):
        """[(name, x, y, side, D, info)] with info: type, angle, floor_layer,
        depth_range, d_drill. Diameters are taken at the face of each layer
        nearest the counterhole's side."""
        faces, top, stack = self._z_faces()
        order = {L.key: i for i, L in enumerate(stack)}
        tol = 0.01
        out = []
        for nm, x, y, rows in self._profiles():
            rows = sorted((r for r in rows if r[0] in order), key=lambda r: order[r[0]])
            if len(rows) < 2:
                continue
            dmin = min(r[1] for r in rows)
            for side in ("top", "bottom"):
                seq = rows if side == "top" else rows[::-1]
                D = seq[0][1]
                if D <= dmin + tol:
                    continue
                surf = top if side == "top" else 0.0

                def depth(key):
                    z_top, z_bot = faces[key]
                    return (surf - z_top) if side == "top" else (z_bot - surf)
                inside = [(depth(k), dia, k) for k, dia, _ in seq if dia > dmin + tol]
                uncut = next(((depth(k), k) for k, dia, _ in seq if dia <= dmin + tol), None)
                mids = [(dep, dia) for dep, dia, _ in inside if dia < D - tol and dep > 1e-6]
                inf = dict(d_drill=dmin, floor_layer="", depth_range=None, type="", angle=None, spread=0.0)
                if mids:
                    th = [(D - dia) / (2.0 * dep) for dep, dia in mids]
                    angs = [2.0 * math.degrees(math.atan(t)) for t in th]
                    inf["type"] = "countersink"
                    inf["angle"] = round(sum(angs) / len(angs), 3)
                    inf["spread"] = max(angs) - min(angs)
                else:
                    last_cut = max(dep for dep, _, _ in inside)
                    if uncut is not None:
                        inf["floor_layer"] = uncut[1]
                        inf["depth_range"] = (last_cut, uncut[0])
                    if len(inside) >= 2 and any(dep > 1e-6 for dep, _, _ in inside):
                        inf["type"] = "counterbore"
                out.append((nm, x, y, side, D, inf))
        return out

    def counterholes(self) -> List[Counterhole]:
        out = []
        names = {L.key: L.name for L in self._layers}
        for nm, x, y, side, D, inf in self.counter_analysis():
            c = Counterhole(x=x, y=y, d=D, layer="IPC-2581 padstacks", side=side, type=inf["type"],
                            angle=inf["angle"], src="IPC-2581 padstack hole diameters")
            if inf["floor_layer"]:
                c.floor_layer = names.get(inf["floor_layer"], inf["floor_layer"])
                c.depth_range = inf["depth_range"]
            out.append(c)
        return out

    def hints(self) -> Tuple[List[CounterholeHint], List[str]]:
        """Counterhole hints (secondary mode), one per size, side and shape."""
        groups: Dict[tuple, CounterholeHint] = {}
        holes: Dict[tuple, set] = defaultdict(set)
        names = {L.key: L.name for L in self._layers}
        for nm, x, y, side, D, inf in self.counter_analysis():
            key = (round(D, 3), side, inf["type"], inf["angle"], inf["floor_layer"])
            q = groups.get(key)
            if q is None:
                q = CounterholeHint(src="IPC-2581 padstack hole diameters", side=side, d=D, type=inf["type"],
                                    angle=inf["angle"], family="IPC-2581", pts=[])
                if inf["floor_layer"]:
                    q.floor_layer = names.get(inf["floor_layer"], inf["floor_layer"])
                    q.depth_range = inf["depth_range"]
                groups[key] = q
            q.pts.append((x, y))
            holes[key].add(nm)
        hints = list(groups.values())
        notes = []
        for key, q in groups.items():
            q.count = len(q.pts)
            if q.type == "countersink":
                what = f"countersink {q.d:.3f} mm, {q.angle:g} deg"
            elif q.type == "counterbore":
                what = (f"counterbore {q.d:.3f} mm, floor on {q.floor_layer} "
                        f"({q.depth_range[0]:.4f}..{q.depth_range[1]:.4f} mm deep)" if q.depth_range else
                        f"counterbore {q.d:.3f} mm")
            else:
                what = (f"counterhole {q.d:.3f} mm of unknown type (only the outer layer is cut)")
            nm = sorted(holes[key])
            notes.append(f"{q.count} x {what} from {q.side} ({', '.join(nm[:6])}{', ...' if len(nm) > 6 else ''})")
        return hints, notes

    def _report_unsupported(self):
        if self.doc.unsupported:
            warn("IPC-2581 features left out or approximated: " +
                 ", ".join(f"{k} x{n}" for k, n in sorted(self.doc.unsupported.items())))


def _is_via_set(s) -> bool:
    """A feature set of via pads (padUsage VIA), positive."""
    return (s.get("padUsage") or "").upper() == "VIA" and \
        not (s.get("polarity") or "POSITIVE").upper().startswith("NEG")


def _apply(acc, batch, pol):
    u = union_all(batch)
    if pol == "P":
        return union_all([acc, u])
    return gdiff(acc, u)


def read(path: str, opts=None) -> Board:
    """IPC-2581 file -> Board (primary source)."""
    return IpcReader(path, opts).read()


def read_hints(path: str) -> Tuple[List[CounterholeHint], List[str]]:
    """IPC-2581 file -> counterhole hints (secondary source next to ODB++)."""
    r = IpcReader(path)
    r._layers = r.layers()
    hints, notes = r.hints()
    label = os.path.basename(path)
    if not hints:
        notes = [f"IPC-2581 {label}: no counterhole profiles in the padstacks"]
    else:
        notes = [f"IPC-2581 {label}:"] + ["  " + n for n in notes]
    return hints, notes
