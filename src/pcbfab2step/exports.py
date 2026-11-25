# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Layer export: one layer of the board as DXF, SVG or STEP, in the board's
own coordinates (the same origin as the STEP model; bottom layers seen from
the top, as in Gerber).

    --export TARGET:FORMAT[,FORMAT]

TARGET is a generic name or the layer's name in the fab data. A layer is
exported as it is on the board (as the model builds it), not as the artwork:

    outline                    DXF/SVG: the board outline; STEP: the plain PCB body
    paste-top    paste-bot     solder paste deposits
    stencil-top  stencil-bot   the outline minus the paste openings; in STEP a plate of
                               the stencil thickness lying on the board surface
    solder-top   solder-bot    the solder mask: the outline minus its openings, tented
                               vias closed, holes through the mask cut
    silk-top     silk-bot      silkscreen (clipped to the solder mask)
    copper-top   copper-bot    outer copper, holes cut; copper-N: the Nth copper layer from the top

Without -top/-bot the side that has data is taken. --invert exports the
rest of the outline instead (the solder mask openings, the stencil's paste,
the clearances in the copper). DXF and SVG include the outline unless
--no-outline; a STEP layer is the layer alone unless --outline, which adds
the PCB body. --aperture-offset grows (+) or shrinks (-) the artwork before
the layer is made: paste and stencil apertures, solder mask openings,
copper and silkscreen features.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

import numpy as np
import shapely

from .common import info
from .geometry import clean, gdiff, ginter, iter_polys, simplify

FORMATS = ("dxf", "svg", "step")
STENCIL_GREY = (0.72, 0.74, 0.76)


@dataclass
class Target:
    generic: str                 # outline, paste-bot, stencil-bot, copper-3 ...
    fab: str                     # name in the fab data
    kind: str                    # outline, paste, stencil, mask, silk, copper
    side: str                    # top, bottom, "" (outline, inner copper)
    art_fn: object               # () -> the artwork in the fab data (2D, board coordinates, mm)
    z0: float                    # where the layer sits in the model
    z1: float
    color: tuple = (0.6, 0.6, 0.6)
    aliases: Set[str] = field(default_factory=set)
    material_fn: object = None   # (artwork, offset) -> the layer as it is on the board
    _art: object = None

    def art(self):
        if self._art is None:
            self._art = clean(self.art_fn())
        return self._art

    def material(self, art=None, grow: float = 0.0):
        art = self.art() if art is None else art
        return clean(self.material_fn(art, grow)) if self.material_fn is not None else art

    def has_data(self) -> bool:
        return not self.art().is_empty


def canon(name: str) -> str:
    return re.sub(r"[\s_\-]+", "", name.strip().lower())


def outer_surfaces(model):
    """z of the board's outer faces, solder mask included (where a stencil lies)."""
    st, o = model.stk, model.opts
    t = {side: (0.0 if o.no_mask else max((L.t for L in st.coats[side].get("mask", [])), default=0.0))
         for side in ("top", "bottom")}
    return st.z_bot - t["bottom"], st.z_top + t["top"]


def targets(model) -> List[Target]:
    st, o = model.stk, model.opts
    zb, zt = outer_surfaces(model)
    out = [Target("outline", "outline", "outline", "", lambda: model.B, zb, zt, o.board_color, {"board", "pcb"})]
    cu = [L for L in st.stack if L.kind == "copper"]
    for n, L in enumerate(cu, 1):
        side = "top" if L is st.top_cu else "bottom" if L is st.bot_cu else ""
        g = {"top": "copper-top", "bottom": "copper-bot"}.get(side, f"copper-{n}")
        out.append(Target(g, L.name, "copper", side, (lambda L=L: model.copper_image(L)), L.z0, L.z1,
                          o.copper_color, {f"copper-{n}"},
                          material_fn=lambda art, grow, L=L: model.copper_sheet(L, art)))
    for side in ("top", "bottom"):
        sg = 1.0 if side == "top" else -1.0
        surf = model._surface(side)
        coats = st.coats[side]
        t_m = max((L.t for L in coats.get("mask", [])), default=0.0)
        t_f = max((L.t for L in coats.get("finish", [])), default=0.0)
        short = "top" if side == "top" else "bot"
        for kind, gen, color, base in (("mask", "solder", o.mask_color, 0.0), ("silk", "silk", o.silk_color, t_m),
                                       ("paste", "paste", o.paste_color, t_f)):
            Ls = coats.get(kind, [])
            for k, L in enumerate(Ls):
                g = f"{gen}-{short}" + (f"-{k + 1}" if len(Ls) > 1 else "")
                za, zb2 = surf + sg * base, surf + sg * (base + L.t)
                al = {f"{gen}-{side}" + (f"-{k + 1}" if len(Ls) > 1 else "")}
                if kind == "mask":
                    al |= {f"mask-{short}", f"mask-{side}"}
                out.append(Target(g, L.name, kind, side, (lambda L=L: model.image(L)), min(za, zb2), max(za, zb2),
                                  color, al,
                                  material_fn=lambda art, grow, k=kind, sd=side: model.coat_sheet(k, sd, art, grow)))
                if kind == "paste":
                    sg_ = "stencil" + g[len("paste"):]
                    out.append(Target(sg_, f"{L.name} stencil", "stencil", side, (lambda L=L: model.image(L)),
                                      0.0, 0.0, STENCIL_GREY, {"stencil" + a[len("paste"):] for a in al},
                                      material_fn=lambda art, grow: gdiff(model.B, art)))
    return out


def find_target(model, name: str) -> Target:
    ts = targets(model)
    c = canon(name)
    for t in ts:
        if c in {canon(t.generic)} | {canon(a) for a in t.aliases}:
            return t
    for t in ts:
        if c == canon(t.fab):
            return t
    # a kind without a side: the side that has data
    kinds = {"paste": "paste", "stencil": "stencil", "solder": "mask", "mask": "mask", "soldermask": "mask",
             "silk": "silk", "silkscreen": "silk"}
    if c in kinds:
        hits = [t for t in ts if t.kind == kinds[c] and t.has_data()]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise SystemExit(f"--export {name}: both sides have data; give {hits[0].generic} or {hits[1].generic}")
        raise SystemExit(f"--export {name}: the fab data has no {name} layer with data")
    names = ", ".join(f"{t.generic} ({t.fab})" if t.fab != t.generic else t.generic for t in ts)
    raise SystemExit(f"--export: no target '{name}'. Targets: {names}")


def parse_export(spec: str, output: Optional[str]):
    """'stencil-bot:step' -> ('stencil-bot', ['step']); the format may come from -o."""
    name, _, fmts = spec.rpartition(":") if ":" in spec else (spec, "", "")
    if not name:
        name, fmts = spec, ""
    formats = [f.strip().lower() for f in fmts.split(",") if f.strip()]
    if not formats and output:
        ext = os.path.splitext(output)[1].lower().lstrip(".")
        formats = ["step" if ext == "stp" else ext] if ext else []
    if not formats:
        raise SystemExit(f"--export {spec}: add the format, e.g. {name}:dxf, {name}:svg or {name}:step")
    from .meshfile import FORMATS as MESH
    bad = [f for f in formats if f not in FORMATS and f not in MESH]
    if bad:
        raise SystemExit(f"--export {spec}: unknown format '{bad[0]}' (dxf, svg, step, {', '.join(MESH)})")
    return name.strip(), formats


# Shorter edges than this are rounding steps in the data (Altium writes 1e-7 inch,
# 2.54 nm, where an arc meets a line): a mitre offset would turn one into a spike.
MICRO_EDGE = 1e-4   # mm


def offset(geom, d: float):
    """Grow (+) or shrink (-) every feature by d mm; square corners stay square."""
    if not d or geom.is_empty:
        return geom
    geom = shapely.remove_repeated_points(geom, MICRO_EDGE)
    return clean(geom.buffer(d, join_style="mitre", mitre_limit=2.0))


# Altium writes the board profile's arcs as chords outside the arc (vertices
# 8 um beyond it), the layers' own arcs much finer: a layer drawn to the board
# edge leaves a crescent up to that wide along the outline. Thinner than this:
EDGE_SLIVER = 0.02  # mm


def drop_edge_slivers(geom, B, w: float = EDGE_SLIVER):
    """Remove the parts of a region along the outline that are narrower than w
    (the gap between the profile's chords and a layer drawn to the true edge)."""
    if geom.is_empty:
        return geom
    h = w / 2.0
    opened = geom.buffer(-h, join_style="mitre", mitre_limit=2.0).buffer(h, join_style="mitre", mitre_limit=2.0)
    thin = [p for p in iter_polys(gdiff(geom, opened)) if p.distance(B.boundary) <= w]
    if not thin:
        return geom
    return clean(gdiff(geom, shapely.union_all(thin).buffer(1e-6)))


def layer_geometry(model, spec: str, opts, output: Optional[str] = None):
    """What --export TARGET:FORMAT writes: (target, 2D region, label, formats)."""
    name, formats = parse_export(spec, output)
    t = find_target(model, name)
    B = model.B
    d = float(getattr(opts, "aperture_offset", 0.0) or 0.0)
    inv = bool(getattr(opts, "export_invert", False))
    if t.kind == "outline":
        if inv:
            raise SystemExit("--invert: the outline has nothing around it to export")
        geom = B
    else:
        art = t.art()
        if d:
            n0 = len(list(iter_polys(art)))
            art = offset(art, d)
            n1 = len(list(iter_polys(art)))
            what = "mask openings" if t.kind == "mask" else "apertures" if t.kind in ("paste", "stencil") \
                else "features"
            info(f"  {what} offset by {d:+.4f} mm" + (f"; {n0 - n1} vanished" if n1 < n0 else ""))
        geom = clean(ginter(t.material(art, d), B))
        if inv:
            geom = clean(gdiff(B, geom))
        geom = drop_edge_slivers(geom, B)
        if t.kind == "silk":
            geom = simplify(geom, getattr(opts, "silk_simplify", 0.0))
        geom = simplify(geom, getattr(opts, "simplify", 0.0))
    if t.kind != "outline" and geom.is_empty:
        raise SystemExit(f"--export {name}{' --invert' if inv else ''}: the layer is empty")
    label = (t.fab if canon(name) == canon(t.fab) else t.generic) + ("_inverted" if inv else "")
    return t, geom, label, formats


def export(model, spec: str, opts, out_dir: str, base: str, output: Optional[str]) -> List[str]:
    t, geom, label, formats = layer_geometry(model, spec, opts, output)
    B = model.B
    with_outline = getattr(opts, "export_outline", None)
    written = []
    for fmt in formats:
        if output and len(formats) == 1:
            path = output
        elif output:
            path = os.path.splitext(output)[0] + "." + fmt
        else:
            path = os.path.join(out_dir, f"{base}_{label}.{fmt}")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if fmt in ("dxf", "svg"):
            outl = None if (t.kind == "outline" or with_outline is False) else B
            if fmt == "dxf":
                write_dxf(path, geom, outl, label)
            else:
                write_svg(path, geom, outl, t.color)
        elif fmt == "step":
            write_layer_step(path, model, t, geom, bool(with_outline), label, opts)
        else:
            written += write_layer_mesh(path, model, t, geom, bool(with_outline), label, opts, fmt)
            continue
        written.append(path)
    return written


def write_layer_mesh(path, model, t: Target, geom, with_board, label, opts, fmt) -> List[str]:
    """A layer as a mesh file: the bodies of the layer STEP, meshed."""
    from . import meshfile
    from .scene import MeshGeo, Part, realize
    parts = []
    for name, kind, color, g, z0, z1 in layer_step_bodies(model, t, geom, with_board, label, opts):
        mesh = realize([MeshGeo.prisms(g, z0, z1)])[0]
        if len(mesh.tri):
            parts.append(Part(name, kind, tuple(color), mesh, ("PCB",) if kind == "board" else ()))
    return meshfile.write(path, parts, label, fmt)


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------


def _rings(g):
    for p in iter_polys(g):
        for ring in [p.exterior] + list(p.interiors):
            c = np.asarray(ring.coords)
            if len(c) >= 3:
                yield c


def write_dxf(path: str, geom, outline, layer: str):
    """DXF R12 (AC1009): closed polylines, mm; layers OUTLINE and the layer's name."""
    lay = re.sub(r"[^A-Za-z0-9_-]+", "_", layer).upper() or "LAYER"
    layers = ([("OUTLINE", 7)] if outline is not None else []) + [(lay, 3)]
    out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "9", "$INSUNITS", "70", "4",
           "0", "ENDSEC", "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER", "70", str(len(layers))]
    for name, col in layers:
        out += ["0", "LAYER", "2", name, "70", "0", "62", str(col), "6", "CONTINUOUS"]
    out += ["0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]

    def poly(c, name):
        if np.allclose(c[0], c[-1]):
            c = c[:-1]
        out.extend(["0", "POLYLINE", "8", name, "66", "1", "70", "1"])
        for x, y in c:
            out.extend(["0", "VERTEX", "8", name, "10", f"{x:.6f}", "20", f"{y:.6f}"])
        out.extend(["0", "SEQEND", "8", name])
    if outline is not None:
        for c in _rings(outline):
            poly(c, "OUTLINE")
    for c in _rings(geom):
        poly(c, lay)
    out += ["0", "ENDSEC", "0", "EOF"]
    with open(path, "w", newline="\r\n") as f:
        f.write("\n".join(out) + "\n")


def _hex(c):
    return "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in c)


def write_svg(path: str, geom, outline, color):
    """SVG in mm (1 user unit = 1 mm), y up as on the board; the layer filled,
    the outline as a line."""
    ref = outline if outline is not None else geom
    x0, y0, x1, y1 = ref.bounds if not ref.is_empty else (0, 0, 1, 1)
    m = 1.0
    w, h = x1 - x0 + 2 * m, y1 - y0 + 2 * m

    def d(g):
        return " ".join("M" + " L".join(f"{x:.4f},{y:.4f}" for x, y in c) + " Z" for c in _rings(g))
    body = ""
    if not geom.is_empty:
        body += f'<path d="{d(geom)}" fill="{_hex(color)}" fill-rule="evenodd" stroke="none"/>'
    if outline is not None:
        body += f'<path d="{d(outline)}" fill="none" stroke="#000" stroke-width="0.05"/>'
    with open(path, "w") as f:
        f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.3f}mm" height="{h:.3f}mm" '
                f'viewBox="{x0 - m:.4f} {-y1 - m:.4f} {w:.4f} {h:.4f}">'
                f'<g transform="scale(1,-1)">{body}</g></svg>\n')


def layer_step_bodies(model, t: Target, geom, with_board, label, opts):
    """The bodies of a layer STEP: (name, kind, colour, 2D region, z0, z1)."""
    zb, zt = outer_surfaces(model)

    def pcb(z0=None, z1=None):
        """The plain PCB body (solder mask included), without the z range of the
        exported layer, so the two never overlap."""
        if z0 is None:
            parts = [(zb, zt)]
        else:
            parts = []
            if z0 > zb + 1e-6:
                parts.append((zb, min(z0, zt)))
            if z1 < zt - 1e-6:
                parts.append((max(z1, zb), zt))
        return [(f"PCB ({zt - zb:.4f} mm)", "board", opts.board_color, model.B, a, b) for a, b in parts if b - a > 1e-6]
    if t.kind == "outline":
        return pcb()
    if t.kind == "stencil":
        th = opts.stencil_thickness
        z0, z1 = (zb - th, zb) if t.side == "bottom" else (zt, zt + th)
        name = f"{label} ({th * 1000:.0f} um)"
    else:
        z0, z1 = t.z0, max(t.z1, t.z0 + 0.001)
        name = label
    return (pcb(z0, z1) if with_board else []) + [(name, "layer", t.color, geom, z0, z1)]


def write_layer_step(path, model, t: Target, geom, with_board, label, opts):
    from .brep import OCC, Brep, write_step
    OCC.load()
    comps = {}
    for name, kind, color, g, z0, z1 in layer_step_bodies(model, t, geom, with_board, label, opts):
        c = comps.setdefault(name, {"name": name, "color": color, "solids": [], "layer": "PCB" if kind == "board"
                                    else label})
        c["solids"] += Brep.prisms(g, z0, z1)
    write_step(path, label, list(comps.values()), opts.schema)
