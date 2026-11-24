# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Layer exports: any layer's 2D image as DXF, SVG or STEP, in the board's own
coordinates (same origin as the STEP model).

The builder draws every layer as a 2D image before it becomes solids; an
export takes that image on its own. A layer is named by its name in the fab
data (case-insensitive) or, with --use-generic-names, by a generic name:

    outline
    paste-top    paste-bot        solder paste
    solder-top   solder-bot       solder mask (the openings, as in the data)
    silk-top     silk-bot         silkscreen
    copper-top   copper-bot   copper-<n>   copper, n counted from the top (1..)

Inverted, a layer is the outline minus its image (a paste stencil plate).
DXF and SVG include the board outline unless --no-outline; STEP adds the
board as a plain slab only with --outline. A STEP layer sits where it is in
the model (an inverted one on the board surface, --stencil-thickness thick).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import numpy as np

from .common import info, warn
from .geometry import EMPTY, clean, gdiff, iter_polys

FORMATS = ("dxf", "svg", "step")
DEFAULT_FORMATS = ("dxf", "svg")
KINDS = ("outline", "paste", "solder", "silk", "copper")


@dataclass
class ExportLayer:
    generic: str                 # outline, paste-top, copper-3 ...
    fab: str                     # name in the fab data
    kind: str                    # outline, paste, mask, silk, copper
    side: str                    # top, bottom, "" (outline, inner copper)
    image: object                # 2D region (board coordinates, mm)
    z0: float                    # where the layer sits in the model
    z1: float
    color: tuple = (0.6, 0.6, 0.6)
    aliases: Set[str] = field(default_factory=set)


@dataclass
class ExportRequest:
    layer: str                   # as given (fab or generic name, 'all', or kind with both sides)
    formats: List[str]
    invert: bool = False
    generic: bool = False        # from a short alias (--paste-dxf ...): generic names


def canon(name: str) -> str:
    return re.sub(r"[\s_\-]+", "", name.strip().lower())


def parse_spec(spec: str) -> ExportRequest:
    """'LAYER[:fmt[,fmt...]][:invert]' -> ExportRequest."""
    parts = spec.split(":")
    layer, fmts, inv = parts[0].strip(), [], False
    for p in parts[1:]:
        for t in (x.strip().lower() for x in p.split(",")):
            if not t:
                continue
            if t in ("invert", "inverted", "inv"):
                inv = True
            elif t in FORMATS:
                fmts.append(t)
            else:
                raise ValueError(f"--export {spec}: unknown format or option '{t}' (dxf, svg, step, invert)")
    if not layer:
        raise ValueError(f"--export {spec}: no layer named")
    return ExportRequest(layer, fmts or list(DEFAULT_FORMATS), inv)


def collect_requests(args) -> List[ExportRequest]:
    reqs: List[ExportRequest] = []
    for group in getattr(args, "export", None) or []:
        for spec in group:
            reqs.append(parse_spec(spec))
    for kind in ("outline", "paste", "solder", "silk"):
        for fmt in FORMATS:
            if getattr(args, f"{kind}_{fmt}", False):
                reqs.append(ExportRequest(kind, [fmt], generic=True))
    inv = getattr(args, "invert", None)
    if inv is not None:
        names = {canon(x) for x in inv}
        for r in reqs:
            if not names or canon(r.layer) in names:
                r.invert = True
    return reqs


def available(model) -> List[ExportLayer]:
    """Every exportable layer of a prepared model."""
    st = model.stk
    o = model.opts
    out = [ExportLayer("outline", "outline", "outline", "", model.B, st.z_bot, st.z_top, o.board_color)]
    cu = [L for L in st.stack if L.kind == "copper"]
    for n, L in enumerate(cu, 1):
        side = "top" if L is st.top_cu else "bottom" if L is st.bot_cu else ""
        g = {"top": "copper-top", "bottom": "copper-bot"}.get(side, f"copper-{n}")
        e = ExportLayer(g, L.name, "copper", side, None, L.z0, L.z1, o.copper_color, {f"copper-{n}"})
        e.image = (lambda L=L: model.copper_image(L))
        out.append(e)
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
                za, zb = surf + sg * base, surf + sg * (base + L.t)
                e = ExportLayer(g, L.name, kind, side, None, min(za, zb), max(za, zb), color)
                e.image = (lambda L=L: model.image(L))
                out.append(e)
    return out


def resolve(reqs: List[ExportRequest], layers: List[ExportLayer], generic: bool):
    """-> [(ExportLayer, ExportRequest)]; unknown names raise with the list of names."""
    by_fab = {canon(e.fab): e for e in layers}
    by_gen: Dict[str, ExportLayer] = {}
    for e in layers:
        for g in {e.generic} | e.aliases:
            by_gen[canon(g)] = e
    out = []
    for r in reqs:
        c = canon(r.layer)
        use_gen = generic or r.generic
        if c == "all":
            hits = layers
        elif r.generic and c in ("paste", "solder", "silk"):        # --paste-dxf: both sides
            hits = [e for e in layers if e.generic.startswith(r.layer + "-")]
            if not hits:
                warn(f"export {r.layer}: the fab data has no {r.layer} layer")
        elif c == "outline":
            hits = [layers[0]]
        else:
            e = (by_gen if use_gen else by_fab).get(c)
            if e is None:
                names = ", ".join((e.generic if use_gen else e.fab) for e in layers)
                hint = "" if use_gen else " (generic names need --use-generic-names)"
                raise SystemExit(f"--export: no layer '{r.layer}'{hint}. Layers: {names}")
            hits = [e]
        for e in hits:
            out.append((e, r))
    return out


def export_dir(fab: Optional[str], primary: str, output: Optional[str]) -> str:
    if fab:
        return os.path.join(fab, "EXPORT")
    base = os.path.dirname(os.path.abspath(output)) if output else \
        os.path.dirname(os.path.abspath(primary.rstrip("/\\")))
    return os.path.join(base, "exports")


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_")


def run(model, reqs, opts, folder: str, board_name: str, generic: bool) -> List[str]:
    if not reqs:
        return []
    layers = available(model)
    todo = resolve(reqs, layers, generic)
    os.makedirs(folder, exist_ok=True)
    written = []
    B = model.B
    with_outline = getattr(opts, "export_outline", None)
    for e, r in todo:
        img = e.image() if callable(e.image) else e.image
        img = clean(img) if img is not None else EMPTY
        inv = r.invert and e.kind != "outline"
        geom = clean(gdiff(B, img)) if inv else img
        label = (e.generic if (generic or r.generic) else e.fab)
        stem = os.path.join(folder, _safe(f"{board_name}_{label}" + ("_inverted" if inv else "")))
        for fmt in r.formats:
            path = f"{stem}.{fmt}"
            outline = (with_outline is not False) if fmt != "step" else bool(with_outline)
            if fmt == "dxf":
                write_dxf(path, geom, B if outline and e.kind != "outline" else None, label)
            elif fmt == "svg":
                write_svg(path, geom, B if outline and e.kind != "outline" else None, e.color, inv)
            else:
                write_layer_step(path, model, e, geom, inv, outline and e.kind != "outline", label, opts)
            written.append(path)
    for p in written:
        info(f"  exported {p}")
    return written


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
    """DXF R12 (AC1009): closed polylines, mm; layer OUTLINE and the layer's name."""
    lay = re.sub(r"[^A-Za-z0-9_-]+", "_", layer).upper() or "LAYER"
    out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "9", "$INSUNITS", "70", "4",
           "0", "ENDSEC", "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER", "70", "2"]
    for name, col in (("OUTLINE", 7), (lay, 3)):
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


def write_svg(path: str, geom, outline, color, inverted: bool):
    """SVG in mm (1 user unit = 1 mm), y up as in the board; the layer filled,
    the outline as a line."""
    ref = outline if outline is not None else geom
    if ref is None or ref.is_empty:
        ref = geom
    x0, y0, x1, y1 = ref.bounds if not ref.is_empty else (0, 0, 1, 1)
    m = 1.0
    w, h = x1 - x0 + 2 * m, y1 - y0 + 2 * m

    def d(g):
        return " ".join("M" + " L".join(f"{x:.4f},{y:.4f}" for x, y in c) + " Z" for c in _rings(g))
    body = ""
    if geom is not None and not geom.is_empty:
        body += f'<path d="{d(geom)}" fill="{_hex(color)}" fill-rule="evenodd" stroke="none"/>'
    if outline is not None:
        body += f'<path d="{d(outline)}" fill="none" stroke="#000" stroke-width="0.05"/>'
    with open(path, "w") as f:
        f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.3f}mm" height="{h:.3f}mm" '
                f'viewBox="{x0 - m:.4f} {-y1 - m:.4f} {w:.4f} {h:.4f}">'
                f'<g transform="scale(1,-1)">{body}</g></svg>\n')


def write_layer_step(path, model, e: ExportLayer, geom, inverted, with_board, label, opts):
    from .brep import OCC, Brep, write_step
    OCC.load()
    st = model.stk
    comps = []
    if with_board:
        comps.append({"name": f"Board outline ({st.z_top - st.z_bot:.4f} mm)", "color": opts.board_color,
                      "solids": Brep.prisms(model.B, st.z_bot, st.z_top), "layer": "OUTLINE"})
    if inverted:
        t = opts.stencil_thickness
        if e.side == "bottom":
            z0, z1 = st.z_bot - t, st.z_bot
        else:
            z0, z1 = st.z_top, st.z_top + t
        name = f"{label} inverted ({t * 1000:.0f} um)"
    else:
        z0, z1 = e.z0, e.z1
        if z1 - z0 < 1e-6:
            z1 = z0 + 0.001
        name = label
    comps.append({"name": name, "color": e.color, "solids": Brep.prisms(geom, z0, z1), "layer": label})
    write_step(path, label, comps, opts.schema)
