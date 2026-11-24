# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Builder: turns a Board (the format-neutral description every reader
produces) into solids. It places the stackup in z, resolves the thicknesses,
holes, via protection, counterholes and backdrills with one precedence chain

    command line > config file > fab data (secondary sources first) > built-in defaults

and builds named, coloured bodies for the STEP writer."""

from __future__ import annotations

import copy
import fnmatch
import math
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import shapely
from shapely.geometry import Point, Polygon, box

from . import __version__
from .board import (CAP_TOKENS, COATINGS, FILL_TOKENS, PLUG_TOKENS, TENT_TOKENS, Board, CounterholeHint, Hole,
                    Layer, apply_ipc, ipc_label)
from .brep import OCC, Brep, bbox, cut_shapes
from .common import DEFAULTS, EPS_Z, VALID_RANGE, PointIndex, info, name_tokens, warn
from .config import finish_color_for, parse_color
from .geometry import EMPTY, circle, clean, gdiff, ginter, iter_polys, union_all


MAX_MARKER = 6.0   # mm: larger features on via-protection document layers are not hole markers


def canon(name: str) -> str:
    """Layer name without case, separators and Altium's ODB++ '_L01__' prefix."""
    n = re.sub(r"^_*l\d+_+", "", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", n)


def _src(opts, key: str, fallback: str) -> str:
    """Where an option value came from (command line flag or config file key)."""
    return (getattr(opts, "sources", None) or {}).get(key, fallback)


# --------------------------------------------------------------------------
# Stackup
# --------------------------------------------------------------------------


class Stackup:
    """Copper and dielectric layers in order (top first) with thickness and z,
    and the coating layers (mask, silk, finish, paste) of each side."""

    def __init__(self, board: Board, opts):
        self.opts = opts
        layers = [L for L in board.layers if L.context == "BOARD"]
        self._rows = {L.key: L.row for L in board.layers}
        self.stack: List[Layer] = []
        self.coats: Dict[str, Dict[str, List[Layer]]] = {"top": defaultdict(list), "bottom": defaultdict(list)}
        self.notes: List[str] = []
        self._span_cache: Dict[tuple, Tuple[int, int]] = {}
        over = {k.lower(): v for k, v in (opts.layer_thickness or {}).items()}
        over_src = _src(opts, "layer_thickness", "layer thickness setting")

        core = [L for L in layers if L.kind in ("copper", "dielectric")]
        coppers = [L for L in core if L.kind == "copper"]
        if not core:
            raise SystemExit("the fab data has no copper or dielectric board layers")

        # implicit dielectrics between adjacent copper layers
        stack: List[Layer] = []
        for L in core:
            if stack and stack[-1].kind == "copper" and L.kind == "copper":
                stack.append(Layer(row=L.row, name=f"DIELECTRIC_{stack[-1].name}_{L.name}", kind="dielectric",
                                   type="DIELECTRIC", implicit=True))
            stack.append(L)
        if not any(L.kind == "dielectric" for L in stack):
            only = stack[0]
            below = not ({"bot", "bottom"} & name_tokens(only.name))
            D = Layer(row=only.row + (1 if below else -1), name="DIELECTRIC_CORE", kind="dielectric",
                      type="DIELECTRIC", implicit=True)
            stack = stack + [D] if below else [D] + stack
        self.stack = stack

        # thickness of copper and dielectric layers
        lo_d, hi_d = VALID_RANGE["dielectric"]
        unknown = []
        for L in stack:
            if L.key in over:
                L.t, L.t_src = over[L.key], over_src
            elif L.kind == "copper" and opts.copper_thickness is not None:
                L.t, L.t_src = opts.copper_thickness, _src(opts, "copper_thickness", "copper thickness setting")
            elif L.t_data is not None and (L.kind == "copper" or lo_d <= L.t_data <= hi_d):
                L.t, L.t_src = L.t_data, L.t_data_src
            elif L.kind == "copper":
                L.t, L.t_src = DEFAULTS["copper"], "default"
            else:
                unknown.append(L)

        # board thickness: setting > fab data > default
        t_fab, t_fab_src = board.board_thickness, board.board_thickness_src
        self.board_attr, self.board_attr_src = t_fab, t_fab_src
        target = opts.board_thickness or t_fab
        ref = _src(opts, "board_thickness", "board thickness setting") if opts.board_thickness else t_fab_src
        # Copper layers with no thickness data and no features are placeholders
        # when leaving them out (t = 0) makes the stack match the board
        # thickness (Altium omits .copper_weight for zero-thickness layers).
        zero = [L for L in stack if L.kind == "copper" and L.t_src == "default" and L.empty]
        if zero and target and not unknown:
            full = sum(L.t for L in stack)
            cut = sum(L.t for L in zero)
            tol = max(0.005, 0.002 * target)
            if abs(full - cut - target) <= tol < abs(full - target):
                each = zero[0].t
                for L in zero:
                    L.t, L.t_src = 0.0, f"empty, no thickness data (0 matches {ref})"
                self.notes.append(f"{len(zero)} empty copper layer(s) without thickness data taken as 0 um "
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
            src = (f"inferred from {ref}" if target else f"inferred from default {DEFAULTS['board']} mm board")
            for L in unknown:
                L.t, L.t_src = each, src
        elif opts.board_thickness:
            diel = [L for L in stack if L.kind == "dielectric" and L.key not in over]
            cu = sum(L.t for L in stack if L not in diel)
            dsum = sum(L.t for L in diel)
            if diel and dsum > 0 and opts.board_thickness > cu:
                f = (opts.board_thickness - cu) / dsum
                for L in diel:
                    L.t *= f
                    L.t_src += f" (scaled x{f:.4f} to {ref})"
                self.notes.append(f"dielectrics scaled by {f:.4f} to match {ref}")

        # z coordinates, bottom-up
        z = 0.0
        for L in reversed(stack):
            L.z0, L.z1 = z, z + L.t
            z = L.z1
        self.total = z
        if t_fab and abs(t_fab - self.total) > 0.0005 and not unknown:
            self.notes.append(f"stack sum {self.total:.4f} mm differs from {t_fab_src} {t_fab:.4f} mm")

        # coating layers (mask, silk, finish, paste) and their side
        rows = [L.row for L in coppers] or [L.row for L in core]
        first, last = min(rows), max(rows)
        for L in layers:
            if L.kind not in COATINGS:
                continue
            tk = name_tokens(L.name)
            if L.side_hint in ("top", "bottom"):
                side = L.side_hint
            elif L.row < first:
                side = "top"
            elif L.row > last:
                side = "bottom"
            elif tk & {"bot", "bottom", "b"}:
                side = "bottom"
            else:
                side = "top"
            L.side = side
            self._coat_thickness(L, over, over_src)
            self.coats[side][L.kind].append(L)

        n = len(stack)
        self.top_cu = stack[0] if stack[0].kind == "copper" else None
        self.bot_cu = stack[-1] if stack[-1].kind == "copper" else None
        self.z_top = stack[0].z1
        self.z_bot = stack[-1].z0
        for i, L in enumerate(stack):
            L.side = "top" if i == 0 else ("bottom" if i == n - 1 else "inner")

    def _coat_thickness(self, L: Layer, over, over_src):
        kind = L.kind
        if L.key in over:
            L.t, L.t_src = over[L.key], over_src
            return
        ov = getattr(self.opts, f"{kind}_thickness", None)
        if ov is not None:
            L.t, L.t_src = ov, _src(self.opts, f"{kind}_thickness", f"{kind} thickness setting")
            return
        v = L.t_data
        lo, hi = VALID_RANGE[kind]
        if v is not None and lo <= v <= hi:
            L.t, L.t_src = v, L.t_data_src
        else:
            L.t = DEFAULTS[kind]
            L.t_src = ("standard default" if v is None else
                       f"standard default ({L.t_data_src} value {v * 1000:g} um implausible)")

    def index(self, name: str) -> Optional[int]:
        """Stack index of a layer name; names from another format of the same
        design match too ('Blank 2' in IPC-2581 is 'BLANK_2' in ODB++)."""
        key = (name or "").lower()
        for i, L in enumerate(self.stack):
            if L.key == key:
                return i
        c = canon(name)
        hits = [i for i, L in enumerate(self.stack) if canon(L.name) == c]
        return hits[0] if len(hits) == 1 else None

    def span(self, start: str, end: str) -> Tuple[int, int]:
        """Stack index range (top, bottom) of a drill/rout span given by layer names."""
        key = ((start or "").lower(), (end or "").lower())
        if key in self._span_cache:
            return self._span_cache[key]
        n = len(self.stack)

        def resolve(name, default):
            if not name:
                return default
            i = self.index(name)
            if i is not None:
                return i
            # a non-stack board layer: map by row to the nearest stack layer
            row = self._rows.get(name.lower())
            if row is None:
                warn(f"span layer '{name}' not found; using the full stack")
                return default
            return min(range(n), key=lambda j: abs(self.stack[j].row - row))
        a = resolve(start, 0)
        b = resolve(end, n - 1)
        out = (min(a, b), max(a, b))
        self._span_cache[key] = out
        return out


# --------------------------------------------------------------------------
# Cuts
# --------------------------------------------------------------------------


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


CONE_OVERRUN = 0.010   # mm, see Model._apply_cones


@dataclass
class Cone:
    x: float
    y: float
    r_wide: float
    r_narrow: float
    z_wide: float
    z_narrow: float
    side: str


def parse_size_rule(spec: str, name: str):
    """--counterbore HOLE:DIA:DEPTH[:SIDE] / --countersink HOLE:DIA:ANGLE[:SIDE]."""
    parts = [p.strip() for p in spec.split(":")]
    if len(parts) < 3:
        raise SystemExit(f"--{name} expects HOLE:DIA:{'DEPTH' if name == 'counterbore' else 'ANGLE'}[:top|bottom|both]")
    side = parts[3].lower() if len(parts) > 3 else "top"
    if side not in ("top", "bottom", "bot", "both"):
        raise SystemExit(f"--{name}: side must be top, bottom or both")
    try:
        return float(parts[0]), float(parts[1]), float(parts[2]), ("bottom" if side == "bot" else side)
    except ValueError:
        raise SystemExit(f"--{name} '{spec}': numbers expected")


def _family(h: CounterholeHint) -> str:
    return h.family or h.src.split(":")[0]


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


class Model:
    def __init__(self, board: Board, opts):
        self.board = board
        self.opts = opts
        self.layers = board.layers
        self.stk = Stackup(board, opts)
        self.holes: List[Hole] = list(board.holes)
        self.cuts: List[Cut] = []
        self.cones: List[Cone] = []
        self.prot_notes: List[str] = list(board.prot_notes)
        self.hole_notes: List[str] = []
        self.components: List[dict] = []
        self.svg: Dict[str, object] = {}
        self.B = EMPTY
        self._unaligned = set()
        self._tent_region: Dict[str, object] = {}
        self.vias_drilled = True
        self.geo = Brep          # solids; scene.MeshGeo builds viewer meshes from the same regions instead

    # .. images ...............................................................
    def image(self, L: Layer):
        """2D image of a layer, clipped to the outline, negative layers inverted."""
        g = L.raw_image()
        if L.polarity.upper().startswith("NEG"):
            g = gdiff(self.B, g)
        return clean(ginter(g, self.B))

    def copper_image(self, L: Layer):
        """Copper image; with --sanitize pads/exposed the pads alone (and the
        exposed copper) on the outer layers, without the pads of undrilled vias."""
        st = self.stk.stack
        mode = getattr(self.opts, "outer_copper", "all")
        if mode == "all" or L not in (st[0], st[-1]) or L.pads_fn is None:
            return self.image(L)
        g = L.pads_fn(self.vias_drilled)
        if mode == "exposed":
            # plus the copper the solder mask leaves open (pours, exposed traces)
            side = "top" if L is st[0] else "bottom"
            masks = [] if self.opts.no_mask else self.stk.coats[side].get("mask", [])
            if not masks:
                return self.image(L)              # no solder mask: all of it is exposed
            open_ = gdiff(union_all(self.image(M) for M in masks), self._tent_region.get(side, EMPTY))
            g = union_all([g, ginter(self.image(L), open_)])
        return clean(ginter(g, self.B))

    def mask_cover(self, side: str, openings=None, grow: float = 0.0):
        """Where the solder mask covers one side: the outline minus the openings,
        plus the tented via openings (grown with the openings when they are
        offset). None without a solder mask on that side."""
        masks = [] if self.opts.no_mask else self.stk.coats[side].get("mask", [])
        if not masks:
            return None
        if openings is None:
            openings = union_all(self.image(L) for L in masks)
        M = gdiff(self.B, openings)
        tents = self._tent_region.get(side, EMPTY)
        if not tents.is_empty:
            if grow > 0:
                tents = tents.buffer(grow, join_style="mitre", mitre_limit=2.0)
            M = union_all([M, ginter(tents, self.B)])
        return M

    def coat_sheet(self, kind: str, side: str, art, grow: float = 0.0):
        """A coating as the model builds it on the surface, from its artwork
        (layer export): solder mask = the cover minus the holes through it;
        silkscreen clipped to the mask (or off the holes); paste off the open
        holes (a capped via in a pad is printed over)."""
        holes, pierce, holes_open = self._coat_cuts(side)
        if kind == "mask":
            M = self.mask_cover(side, art, grow)
            return EMPTY if M is None else clean(gdiff(M, pierce))
        if kind == "silk":
            S = gdiff(art, pierce)
            M = self.mask_cover(side)
            if M is not None:
                return clean(S) if self.opts.no_clip_silk else clean(ginter(S, M))
            return clean(gdiff(S, holes))
        if kind == "paste":
            return clean(gdiff(art, holes_open))
        return clean(art)

    def copper_sheet(self, L: Layer, art):
        """Copper as the model builds it (layer export): the image minus the
        holes, bores and counterholes that reach into the layer; on an outer
        layer the via caps close their holes (copper seen from outside)."""
        cuts = [c.region for c in self.cuts if c.z_lo < L.z1 - EPS_Z and c.z_hi > L.z0 + EPS_Z]
        side = "top" if L is self.stk.top_cu else "bottom" if L is self.stk.bot_cu else None
        cuts += [circle(cn.x, cn.y, cn.r_wide) for cn in self.cones if side and cn.side == side]
        g = gdiff(art, union_all(cuts)) if cuts else art
        if side:
            st, caps = self.stk.stack, []
            for h in self.holes:
                if (h.cap_top if side == "top" else h.cap_bot) <= 0 or \
                        st[h.top_idx if side == "top" else h.bot_idx] is not L:
                    continue
                bore = h.bores.get(side)
                # 2 um over the bore: its circle and a clearance drawn for it in
                # the copper are polygons of their own, which would leave a hairline
                caps.append((circle(h.x, h.y, bore[0]) if bore else h.region_drill()).buffer(0.002))
            if caps:
                g = union_all([g, ginter(union_all(caps), self.B)])
        return clean(g)

    # .. preparation ..........................................................
    def prepare(self):
        B = self.board.outline
        if B is None or B.is_empty:
            cu = union_all(L.raw_image() for L in self.stk.stack if L.kind == "copper")
            if cu.is_empty:
                raise SystemExit("no board outline and no copper: cannot determine the board outline")
            warn("board outline missing or empty; using the bounding box of the copper as board outline")
            x0, y0, x1, y1 = cu.bounds
            B = box(x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5)
        self.B = clean(B)
        self.svg["profile"] = self.B
        self._mark_undrilled()
        self._drop_backdrill_duplicates()
        self._place_holes()
        self._doc_layer_protection()
        self._via_settings()
        self._via_rules()
        self._counter_and_backdrills()
        self._resolve_caps()
        self._resolve_tents()
        self._surface_stubs()
        self._build_cuts()

    def _mark_undrilled(self):
        """--vias undrilled (the default with --sanitize): vias are not drilled
        and the core stays solid; their pads stay on the copper. The vias stay
        in the hole list until their protection (fill, caps, tenting) is
        resolved; _surface_stubs() then keeps what shows on the surface."""
        o = self.opts
        vias = getattr(o, "vias", None)
        if vias is None:
            vias = not getattr(o, "external_only", False)
        self.vias_drilled = bool(vias)
        if not vias:
            for h in self.holes:
                if h.kind == "via":
                    h.undrilled = True

    def _surface_stubs(self):
        """Undrilled vias leave the hole list. A cap that shows on the surface
        stays: the via keeps a stub within the outer copper (cap, and the plating
        and fill under it, or the filled back drill bore), as in the full model.
        A cap under the solder mask cannot be seen and is left out, and so is one
        where the copper (with --sanitize exposed/pads) has no land for it."""
        und = [h for h in self.holes if h.undrilled]
        if not und:
            return
        self.holes = [h for h in self.holes if not h.undrilled]
        st = self.stk.stack
        n = len(st)
        kept, hidden = Counter(), 0
        for side, li in (("top", 0), ("bottom", n - 1)):
            L = st[li]
            cap = "cap_top" if side == "top" else "cap_bot"
            cand = [h for h in und if getattr(h, cap) > 0 and (h.top_idx if side == "top" else h.bot_idx) == li]
            if not cand or L.kind != "copper":
                continue
            cover = self.mask_cover(side)
            land = union_all([Polygon(p.exterior) for p in iter_polys(self.copper_image(L))])
            for g in (cover, land):
                if g is not None:
                    shapely.prepare(g)
            for h in cand:
                if (cover is not None and shapely.contains_xy(cover, h.x, h.y)) or \
                        not shapely.contains_xy(land, h.x, h.y):
                    hidden += 1
                    continue
                s = copy.copy(h)
                s.undrilled, s.stub, s.laser, s.dual, s.notes = False, True, False, None, []
                s.top_idx = s.bot_idx = li
                s.z_lo = s.z_lo_eff = L.z0
                s.z_hi = s.z_hi_eff = L.z1
                s.plug_top = s.plug_bot = 0.0
                if side == "top":
                    s.cap_bot, s.tent_bot = 0.0, False
                else:
                    s.cap_top, s.tent_top = 0.0, False
                s.bores = {}
                bore = h.bores.get(side)
                if bore is not None:              # filled back drill bore: no plating left under the cap
                    r_b = bore[0]
                    if side == "top":
                        s.bores["top"] = (r_b, L.z0)
                        s.z_hi_eff = L.z0
                    else:
                        s.bores["bottom"] = (r_b, L.z1)
                        s.z_lo_eff = L.z1
                    self.cuts.append(Cut(circle(h.x, h.y, r_b), L.z0, L.z1, "backdrill", False, False, hole=s))
                self.holes.append(s)
                kept[side] += 1
        n_bd = sum(1 for h in und if h.bores)
        note = f"vias not drilled: {len(und)} via(s), the core stays solid and their pads stay"
        if n_bd:
            note += f" ({n_bd} of them back drilled)"
        if kept:
            note += "; caps kept on the surface: " + ", ".join(f"{k} on {sd}" for sd, k in kept.items()) + \
                " (with the plating and fill under them, within the outer copper)"
        if hidden:
            note += f"; {hidden} cap(s) under the solder mask or off the copper left out"
        self.hole_notes.append(note + " (--vias drilled to drill them)")

    def _surface(self, side):
        return self.stk.z_top if side == "top" else self.stk.z_bot

    def _entry_side(self, dir_hint: str, top_i: int, bot_i: int, name: str) -> str:
        if dir_hint in ("top", "bottom"):
            return dir_hint
        n = len(self.stk.stack)
        tt, tb = top_i == 0, bot_i == n - 1
        if tt and not tb:
            return "top"
        if tb and not tt:
            return "bottom"
        toks = name_tokens(name)
        if toks & {"bottom", "bot"} and "top" not in toks:
            return "bottom"
        return "top"

    def _plating_t(self, h: Hole) -> Tuple[float, bool]:
        """(wall plating thickness, set explicitly). A setting overrides the
        drill tool sizes of the fab data; the default does not."""
        o = self.opts
        if h.kind == "via" and o.via_plating_thickness is not None:
            return o.via_plating_thickness, True
        if o.plating_thickness is not None:
            return o.plating_thickness, True
        return DEFAULTS["plating"], False

    def _drop_backdrill_duplicates(self):
        """Altium also lists a bottom back drill in the drill pair with the same
        layers: a plated hole with the back drill's position, size and span on
        a hole that has a smaller plated hole is that duplicate."""
        if not self.board.backdrills:
            return
        bidx = PointIndex(0.01)
        for b in self.board.backdrills:
            bidx.add(b.x, b.y, b)
        hidx = PointIndex(0.01)
        for h in self.holes:
            hidx.add(h.x, h.y, h)
        drop = set()
        for h in self.holes:
            if not h.plated:
                continue
            for b in bidx.find_all(h.x, h.y):
                if abs(2 * h.r_fin - b.d) > 0.005:
                    continue
                if {h.start.lower(), h.end.lower()} != {b.start.lower(), b.end.lower()}:
                    continue
                if any(o is not h and o.plated and 2 * o.r_fin < b.d - 0.005 for o in hidx.find_all(h.x, h.y)):
                    drop.add(id(h))
                    break
        if drop:
            self.holes = [h for h in self.holes if id(h) not in drop]
            self.hole_notes.append(f"{len(drop)} plated hole(s) with the position, size and span of a back drill "
                                   "on a smaller hole taken as that back drill (not a separate hole)")

    def _place_holes(self):
        st = self.stk.stack
        for h in self.holes:
            top_i, bot_i = self.stk.span(h.start, h.end)
            side = self._entry_side(h.dir_hint, top_i, bot_i, h.layer)
            if h.plated:
                tp, explicit = self._plating_t(h)
                r_drill = h.r_drill_data if (h.r_drill_data and not explicit) else h.r_fin + tp
            else:
                r_drill = h.r_fin
            if h.laser:
                if side == "top":
                    z_hi = st[top_i].z1
                    tgt = st[bot_i]
                    z_lo = tgt.z1 if (tgt.kind == "copper" and bot_i != top_i) else tgt.z0
                else:
                    z_lo = st[bot_i].z0
                    tgt = st[top_i]
                    z_hi = tgt.z0 if (tgt.kind == "copper" and bot_i != top_i) else tgt.z1
            else:
                z_lo, z_hi = st[bot_i].z0, st[top_i].z1
            h.r_drill = r_drill
            h.t_plating = max(0.0, r_drill - h.r_fin) if h.plated else 0.0
            h.top_idx, h.bot_idx, h.entry = top_i, bot_i, side
            h.z_lo, h.z_hi, h.z_lo_eff, h.z_hi_eff = z_lo, z_hi, z_lo, z_hi
            if h.geom_fin is not None:
                h.geom_drill = clean(h.geom_fin.buffer(r_drill - h.r_fin, quad_segs=32)) if r_drill > h.r_fin \
                    else h.geom_fin

    def _hole_index(self, plated_only=False):
        idx = PointIndex(0.01)
        for h in self.holes:
            if plated_only and not h.plated:
                continue
            idx.add(h.x, h.y, h)
        return idx

    # .. via protection ......................................................
    def _doc_layer_protection(self):
        """Document layers named Filling, Capping, Plugging, Tenting (fab data)."""
        fm, pd = self.opts.fill_material, self.opts.plug_depth
        plated = [h for h in self.holes if h.plated]
        if not plated:
            return
        idx = PointIndex(0.01)
        for h in plated:
            idx.add(h.x, h.y, h)
        # what other data (via type layers, attributes) already says, to compare with the markers
        pre = {"fill": {id(h): h.prot_src for h in plated if h.fill},
               "cap": {id(h): h.prot_src for h in plated if h.cap_top or h.cap_bot}}
        for L in self.layers:
            if L.kind in ("copper", "dielectric", "drill", "rout", "silk", "paste") or L.points_fn is None:
                continue
            toks = name_tokens(L.name)
            role = ("fill" if toks & FILL_TOKENS else "cap" if toks & CAP_TOKENS else
                    "plug" if toks & PLUG_TOKENS else "tent" if toks & TENT_TOKENS else None)
            if role is None:
                continue
            sides = ("top",) if "top" in toks else ("bottom",) if toks & {"bottom", "bot"} else ("top", "bottom")
            hit = set()
            # hole markers only: drawings, outlines and text on the layer are larger
            pts = [p for p in L.points_fn() if p[2] <= MAX_MARKER]
            for (x, y, d) in pts:
                for h in idx.find_all(x, y):
                    hit.add(id(h))
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
                extra = ""
                if role in pre:
                    miss = [src for k, src in pre[role].items() if k not in hit]
                    if miss:
                        srcs = sorted({m for m in miss if m})
                        extra = (f"; {len(miss)} other hole(s) {'filled' if role == 'fill' else 'capped'} by "
                                 f"{', '.join(srcs) or 'other data'} have no marker here")
                self.prot_notes.append(f"layer {L.name}: {role} applied to {len(hit)} hole(s) ({'/'.join(sides)})"
                                       + (f" [{L.points_note}]" if L.points_note else "") + extra)
            elif pts:
                warn(f"via-protection layer {L.name} ({role}) matched no plated hole")

    def _via_settings(self):
        """fill / cap / tent / plug for every via from the command line or config."""
        o = self.opts
        fill, cap, tent, plug = o.via_fill, o.via_cap, o.via_tent, o.via_plug
        if fill is None and cap is None and tent is None and plug is None:
            return
        dmax = o.via_max_diameter
        vias = [h for h in self.holes if h.plated and h.kind == "via"
                and (dmax is None or 2 * h.r_fin <= dmax + 1e-6)]
        if cap and fill == "none":
            warn("vias set to capped but not filled: capped vias are filled with " + o.fill_material)
        what = []
        if fill is not None:
            what.append(f"fill {fill} ({_src(o, 'via_fill', 'via fill setting')})")
        if cap is not None:
            what.append(f"cap {'yes' if cap else 'no'} ({_src(o, 'via_cap', 'via cap setting')})")
        if tent is not None:
            what.append(f"tent {tent} ({_src(o, 'via_tent', 'via tent setting')})")
        if plug is not None:
            what.append(f"plug {plug} ({_src(o, 'via_plug', 'via plug setting')})")
        label = "via setting"
        for h in vias:
            if fill is not None:
                h.fill = "" if fill == "none" else fill
            if cap is not None:
                h.cap_top = h.cap_bot = -1.0 if cap else 0.0
                if cap and not h.fill:
                    h.fill = o.fill_material
            if tent is not None:
                h.tent_top, h.tent_bot = tent in ("top", "both"), tent in ("bottom", "both")
            if plug is not None:
                h.plug_top = o.plug_depth if plug in ("top", "both") else 0.0
                h.plug_bot = o.plug_depth if plug in ("bottom", "both") else 0.0
            h.prot_src = (h.prot_src + ", " if h.prot_src else "") + label
        lim = f" up to {dmax:g} mm" if dmax is not None else ""
        self.prot_notes.append(f"{', '.join(what)}: applied to {len(vias)} via(s){lim}")

    def _via_rules(self):
        fm, pd = self.opts.fill_material, self.opts.plug_depth
        plated = [h for h in self.holes if h.plated]
        for spec in self.opts.via_rules or []:
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
    def _counter_z(self, side, top_i, bot_i, mode):
        """Counterbore floor from the counterhole span: the far layer of the span
        is not cut (expose) or cut through (through)."""
        st = self.stk.stack
        n = len(st)
        if side == "bottom":
            deep = st[top_i]
            if mode == "expose" and deep.kind == "copper" and top_i != n - 1:
                return deep.z0
            return deep.z1
        deep = st[bot_i]
        if mode == "expose" and deep.kind == "copper" and bot_i != 0:
            return deep.z1
        return deep.z0

    def _layer_face(self, name: str, side: str) -> Optional[float]:
        i = self.stk.index(name)
        if i is None:
            return None
        L = self.stk.stack[i]
        return L.z1 if side == "top" else L.z0

    def _trim_hole(self, h: Hole, side: str, z_end: float):
        if side == "top":
            h.z_hi_eff = min(h.z_hi_eff, z_end)
            h.cap_top, h.plug_top, h.tent_top = 0.0, 0.0, False
        else:
            h.z_lo_eff = max(h.z_lo_eff, z_end)
            h.cap_bot, h.plug_bot, h.tent_bot = 0.0, 0.0, False

    def _hint_index(self):
        """Secondary-source hints with positions, in board coordinates. A source
        may use another origin: if its points do not line up as they are, the
        shift that lines up most of them is used."""
        hints = [q for q in self.board.hints if q.pts]
        if not hints or not self.board.counterholes:
            return None
        board_idx = PointIndex(tol=0.02)
        for c in self.board.counterholes:
            board_idx.add(c.x, c.y, c)
        fams: Dict[str, list] = defaultdict(list)
        for q in hints:
            fams[_family(q)].append(q)
        idx = PointIndex(tol=0.02)
        any_added = False
        for fam, qs in fams.items():
            pts = [(p, q) for q in qs for p in q.pts]
            sample = pts[:: max(1, len(pts) // 50)]

            def score(dx, dy, sel):
                return sum(1 for (x, y), _ in sel if board_idx.find(x + dx, y + dy) is not None)
            off = (0.0, 0.0)
            if score(0.0, 0.0, sample) < len(sample):
                best = score(0.0, 0.0, sample)
                for c in self.board.counterholes[:2]:
                    for (x, y), _ in pts:
                        sc = score(c.x - x, c.y - y, sample)
                        if sc > best:
                            best, off = sc, (c.x - x, c.y - y)
            if score(off[0], off[1], pts) < max(1, (len(pts) + 1) // 2):
                self.hole_notes.append(f"{fam} positions do not line up with the counterholes, also not after "
                                       "a shift: matched by size and side only")
                self._unaligned.update(id(q) for q in qs)
                continue
            if off != (0.0, 0.0):
                self.hole_notes.append(f"{fam} coordinates shifted by ({off[0]:+.4f}, {off[1]:+.4f}) mm "
                                       "to line up with the counterholes")
            for (x, y), q in pts:
                idx.add(x + off[0], y + off[1], q)
                any_added = True
        return idx if any_added else None

    def _match_hints(self, c, side, hint_idx) -> List[CounterholeHint]:
        """The hints for one counterhole, first source first (NC drill, then IPC-2581)."""
        order = {id(q): i for i, q in enumerate(self.board.hints)}

        def fits(q):
            return abs(q.d - c.d) <= 0.02 and q.side in (None, side)
        if hint_idx is not None:
            hit = sorted((q for q in hint_idx.find_all(c.x, c.y) if fits(q)), key=lambda q: order.get(id(q), 0))
            if hit:
                return hit
        pool = [q for q in self.board.hints if fits(q) and (q.pts is None or id(q) in self._unaligned)]
        if pool and len({(q.type, q.depth, q.angle, q.floor_layer) for q in pool}) == 1:
            return pool[:1]
        return []

    @staticmethod
    def _check_hint(q, ctype, ang, depth) -> str:
        """'' when a later source agrees with the result, else what it says."""
        if q.type and q.type != ctype:
            return f"{q.type}" + (f" {q.angle:g} deg" if q.type == "countersink" and q.angle else "")
        if ctype == "countersink" and q.angle and ang and abs(q.angle - ang) > 0.5:
            return f"angle {q.angle:g} deg"
        if ctype == "counterbore":
            if q.depth is not None and abs(q.depth - depth) > 0.01:
                return f"depth {q.depth:g} mm"
            if q.depth is None and q.depth_range and not (q.depth_range[0] - 0.01 <= depth <= q.depth_range[1] + 0.01):
                return f"depth {q.depth_range[0]:.4f}..{q.depth_range[1]:.4f} mm"
        return ""

    def _add_counterbore(self, x, y, r, side, z_end, holes, kind, geom=None):
        surf = self._surface(side)
        region = geom if geom is not None else circle(x, y, r)
        lo, hi = (z_end, surf) if side == "top" else (surf, z_end)
        self.cuts.append(Cut(region, lo, hi, kind, side == "top", side == "bottom"))
        for h in holes:
            self._trim_hole(h, side, z_end)
        return abs(surf - z_end)

    def _add_backdrill(self, x, y, r, side, z_end, holes):
        """A back drill bore. It removes the plating above (top) or below
        (bottom) z_end. On a filled via the fill closes the bore up to the
        surface (vias are filled after back drilling) and the via keeps its
        cap on that side; the coatings then cover it. An open bore cuts the
        coatings and ends any cap, plug or tenting on that side."""
        surf = self._surface(side)
        lo, hi = (z_end, surf) if side == "top" else (surf, z_end)
        filled = [h for h in holes if h.fill]
        pierce = not filled
        if not (holes and all(h.undrilled for h in holes)):     # an undrilled via is not back drilled either
            self.cuts.append(Cut(circle(x, y, r), lo, hi, "backdrill", pierce and side == "top",
                                 pierce and side == "bottom", hole=filled[0] if filled else None))
        for h in holes:
            if h.fill:
                if side == "top":
                    h.z_hi_eff = min(h.z_hi_eff, z_end)
                else:
                    h.z_lo_eff = max(h.z_lo_eff, z_end)
                h.bores[side] = (max(r, h.bores.get(side, (0.0,))[0]), z_end)
            else:
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

    def _resolve_counter(self, c, side, top_i, bot_i, hint):
        """Type, angle and counterbore floor of one counterhole:
        settings > secondary data (hint) > primary fab data > board flags > defaults."""
        o = self.opts
        S = (o.counterhole or {}).get(side, {}) or {}
        surf = self._surface(side)
        sg = -1.0 if side == "top" else 1.0
        why = {}
        if S.get("type"):
            ctype, why["type"] = S["type"]
        elif hint is not None and hint.type:
            ctype, why["type"] = hint.type, hint.src
        elif c.type:
            ctype, why["type"] = c.type, c.src
        else:
            cb, cs = self.board.counter_flags
            if cs and not cb:
                ctype, why["type"] = "countersink", "board flags: countersinks only"
            elif cb and not cs:
                ctype, why["type"] = "counterbore", "board flags: counterbores only"
            else:
                ctype, why["type"] = "counterbore", "default"
        ang = None
        z_end = None
        rng = None
        if ctype == "countersink":
            if S.get("angle"):
                ang, why["angle"] = S["angle"]
            elif hint is not None and hint.type == "countersink" and hint.angle:
                ang, why["angle"] = hint.angle, hint.src
            elif c.type == "countersink" and c.angle:
                ang, why["angle"] = c.angle, c.src
            else:
                ang, why["angle"] = 90.0, "default"
        else:
            hb = hint is not None and hint.type in ("", "counterbore")
            cbt = c.type in ("", "counterbore")
            if S.get("depth"):
                dep, why["depth"] = S["depth"]
                z_end = surf + sg * dep
            elif hb and hint.depth is not None:
                z_end, why["depth"] = surf + sg * hint.depth, hint.src
            elif hb and hint.floor_layer and self._layer_face(hint.floor_layer, side) is not None:
                z_end, why["depth"] = self._layer_face(hint.floor_layer, side), hint.src
                rng = hint.depth_range
            elif hb and hint.depth_range:
                z_end, why["depth"] = surf + sg * hint.depth_range[1], hint.src
                rng = hint.depth_range
            elif cbt and c.depth is not None:
                z_end, why["depth"] = surf + sg * c.depth, c.src
            elif cbt and c.floor_layer and self._layer_face(c.floor_layer, side) is not None:
                z_end, why["depth"] = self._layer_face(c.floor_layer, side), c.src
                rng = c.depth_range
            else:
                mode = o.counterhole_depth_mode
                z_end = self._counter_z(side, top_i, bot_i, mode)
                why["depth"] = f"span ({mode})"
            z_end = min(max(z_end, self.stk.z_bot), self.stk.z_top)
        return ctype, ang, z_end, why, rng

    def _counter_and_backdrills(self):
        o = self.opts
        idx = self._hole_index()
        # counterholes by hole size (command line / config rules)
        rules = []
        for spec in o.counterbore_rules or []:
            d1, d2, depth, side = parse_size_rule(spec, "counterbore")
            rules.append(("counterbore", d1, d2, depth, side, spec))
        for spec in o.countersink_rules or []:
            d1, d2, ang, side = parse_size_rule(spec, "countersink")
            rules.append(("countersink", d1, d2, ang, side, spec))
        ruled = set()
        for (kind, d1, d2, val, side, spec) in rules:
            hit = 0
            for h in self.holes:
                if h.undrilled or abs(2 * h.r_fin - d1) > 0.005:
                    continue
                for sd in (("top", "bottom") if side == "both" else (side,)):
                    if kind == "counterbore":
                        z_end = self.stk.z_top - val if sd == "top" else self.stk.z_bot + val
                        self._add_counterbore(h.x, h.y, d2 / 2.0, sd, z_end, [h], "counterbore")
                    else:
                        self._add_countersink(h.x, h.y, d2 / 2.0, val, sd, h)
                    ruled.add((id(h), sd))
                    hit += 1
            self.hole_notes.append(f"--{kind} {spec}: {hit} hole side(s)")

        groups = Counter()
        assumed = {"type": False, "angle": False}
        hint_idx = self._hint_index()
        for c in self.board.counterholes:
            top_i, bot_i = self.stk.span(c.start, c.end)
            side = c.side if c.side in ("top", "bottom") else self._entry_side(c.dir_hint, top_i, bot_i, c.layer)
            c.side = side
            h = idx.find(c.x, c.y)
            if h is not None and (id(h), side) in ruled:
                continue
            hints = self._match_hints(c, side, hint_idx)
            hint = hints[0] if hints else None
            if hint is not None:
                hint.used += 1
            ctype, ang, z_end, why, rng = self._resolve_counter(c, side, top_i, bot_i, hint)
            for q in hints[1:]:               # the other sources: cross-check
                if q.src == hint.src:
                    continue
                d_now = abs(self._surface(side) - z_end) if z_end is not None else 0.0
                bad = self._check_hint(q, ctype, ang, d_now)
                if bad:
                    q.conflicts.append(bad)
                else:
                    q.agreed += 1
            r = c.d / 2.0
            if ctype == "countersink":
                depth = self._add_countersink(c.x, c.y, r, ang, side, h)
                if hint is not None and hint.tip_depth is not None and why.get("angle") == hint.src:
                    tip = r / math.tan(math.radians(ang) / 2.0)
                    why["angle"] += (" (its depth is to the cone tip)" if abs(hint.tip_depth - tip) <= 0.02 else
                                     f" (its depth does not match a {ang:g} deg cone: {tip:.3f} mm to the tip)")
            else:
                depth = self._add_counterbore(c.x, c.y, r, side, z_end, [h] if h is not None else [],
                                              "counterbore", c.geom)
            for k in ("type", "angle"):
                if why.get(k) == "default":
                    assumed[k] = True
            on = f"on {2 * h.r_fin:.3f} mm {h.kind.upper()}" if h else "(no hole underneath)"
            by_src = defaultdict(list)
            for k in ("type", "angle", "depth"):
                if k in why and why[k] != "default":
                    by_src[why[k]].append(k)
            srcs = "; ".join(f"{', '.join(ks)} from {s}" for s, ks in by_src.items())
            if rng:
                srcs += f"; the data allows {rng[0]:.4f}..{rng[1]:.4f} mm, the floor is put on the deeper face"
            key = (c.layer, ctype, round(c.d, 4), side, round(depth, 4),
                   ang if ctype == "countersink" else 0.0, on, srcs)
            groups[key] += 1
        for (lname, ctype, d, side, depth, ang, on, srcs), k in sorted(groups.items()):
            extra = f", {ang:g} deg" if ctype == "countersink" else ""
            self.hole_notes.append(f"{lname}: {k} x {ctype} {d:.3f} mm from {side}, depth {depth:.4f} mm{extra} {on}"
                                   + (f"\n    {srcs}" if srcs else ""))
        for q in self.board.hints:
            what = f"{q.src} ({q.type or 'counterhole'} {q.d:.3f} mm" + (f" from {q.side})" if q.side else ")")
            if q.agreed:
                rng = (f", floor between {q.depth_range[0]:.4f} and {q.depth_range[1]:.4f} mm deep"
                       if q.depth_range and q.depth is None else "")
                self.hole_notes.append(f"{what}: agrees for {q.agreed} hole(s){rng}")
            if q.conflicts:
                c0 = Counter(q.conflicts).most_common(1)[0][0]
                self.hole_notes.append(f"{what}: disagrees for {len(q.conflicts)} hole(s) (it says {c0}); "
                                       "the source listed first decides")
            seen = q.used + q.agreed + len(q.conflicts)
            if seen == 0:
                self.hole_notes.append(f"{what}: not matched to a counterhole")
            elif q.count and seen != q.count:
                self.hole_notes.append(f"{what}: lists {q.count} hole(s), {seen} matched")
        if assumed["type"] or assumed["angle"]:
            what = []
            if assumed["type"]:
                what.append("type counterbore")
            if assumed["angle"]:
                what.append("countersink angle 90 deg")
            self.hole_notes.append("counterhole " + " and ".join(what) + " assumed (not in the fab data). Set "
                                   "them with --counterhole[-top|-bottom] type=bore|sink angle=DEG depth=MM or "
                                   "[counterholes] in config.toml, or provide IPC-2581 or --ncdrill data")
        self._backdrills(idx)
        self._duals(idx)

    def _backdrills(self, idx):
        """Back drills. Altium defines a back drill by its first and last layer
        and drills up to, not into, the last layer: the last layer of the span
        is the must-not-cut layer. The stub left above it comes from the data,
        else the backdrill stub setting."""
        o = self.opts
        st = self.stk.stack
        bgroups = Counter()
        skipped = 0
        for b in self.board.backdrills:
            top_i, bot_i = self.stk.span(b.start, b.end)
            side = self._entry_side(b.dir_hint, top_i, bot_i, b.layer)
            surf = self._surface(side)
            r = b.d / 2.0
            holes = [h for h in idx.find_all(b.x, b.y) if h.plated and h.r_drill < r - 1e-6]
            if not holes and any(h.plated for h in idx.find_all(b.x, b.y)):
                skipped += 1
                continue
            if b.stub is not None:
                stub, stub_src = b.stub, b.src
            else:
                stub, stub_src = o.backdrill_stub, _src(o, "backdrill_stub", "default")
            if b.depth is not None and b.depth > 0:
                z_end = surf - b.depth if side == "top" else surf + b.depth
                how = f"depth from {b.src}"
                mnc = ""
            else:
                mi = self.stk.index(b.mnc) if b.mnc else None
                named = ""
                if mi is None and b.pair:
                    # the layer's own name gives the pair: the layer farther from the drilled surface
                    ids = [i for i in (self.stk.index(n) for n in b.pair) if i is not None]
                    if len(ids) == 2:
                        mi = max(ids) if side == "top" else min(ids)
                        far = bot_i if side == "top" else top_i
                        named = " (named by the layer" + ("" if mi == far else
                                                          f"; its span ends at {st[far].name}") + ")"
                if mi is None:
                    mi = bot_i if side == "top" else top_i
                M = st[mi]
                mnc = M.name
                z_end = min(surf, M.z1 + stub) if side == "top" else max(surf, M.z0 - stub)
                how = (f"up to the must-not-cut layer {mnc}{named}" if stub <= 0 else
                       f"stopping {stub * 1000:g} um short of the must-not-cut layer {mnc}{named} "
                       f"(stub from {stub_src})")
            filled = [h for h in holes if h.fill]
            depth = self._add_backdrill(b.x, b.y, r, side, z_end, holes)
            if holes and all(h.undrilled for h in holes):
                how += "; not drilled (the via is not drilled)"
            elif filled:
                capped = any((h.cap_top if side == "top" else h.cap_bot) for h in filled)
                how += "; bore filled" + (" and capped" if capped else "") + " with the via"
            bgroups[(b.layer, round(b.d, 4), side, round(depth, 4), how, bool(holes))] += 1
        for (lname, d, side, depth, how, ok), k in sorted(bgroups.items()):
            self.hole_notes.append(f"{lname}: {k} x backdrill {d:.3f} mm from {side}, depth {depth:.4f} mm, {how}"
                                   + ("" if ok else " (no plated hole underneath)"))
        if skipped:
            self.hole_notes.append(f"{skipped} back drill(s) not larger than the plated hole they would drill out: "
                                   "ignored (they would not remove the plating)")

    def _duals(self, idx):
        for c in self.board.duals:
            top_i, bot_i = self.stk.span(c.start, c.end)
            side = self._entry_side(c.dir_hint, top_i, bot_i, c.layer)
            h = idx.find(c.x, c.y)
            z_end = self._counter_z(side, top_i, bot_i, "through")
            self._add_counterbore(c.x, c.y, c.d / 2.0, side, z_end, [], "dual")
            if h is not None and h.plated:
                h.dual = (c.d / 2.0, z_end, side)
                if side == "top":
                    h.z_hi_eff = min(h.z_hi_eff, z_end)
                    h.cap_top = 0.0
                else:
                    h.z_lo_eff = max(h.z_lo_eff, z_end)
                    h.cap_bot = 0.0
        if self.board.duals:
            self.hole_notes.append(f"{len(self.board.duals)} dual-diameter (plated step) holes")

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
                setattr(h, f"cap_{side}", min(max(r, h.r_drill), h.r_drill + 1.0))

    def _cap_t(self, h: Hole, side: str) -> float:
        L = self.stk.stack[h.top_idx if side == "top" else h.bot_idx]
        return min(self.opts.cap_thickness, L.t)

    def _build_cuts(self):
        o = self.opts
        thru = o.drill_through_mask
        for h in self.holes:
            base_p = (h.kind != "via") or thru
            self.cuts.append(Cut(h.region_drill(), h.z_lo, h.z_hi, "hole",
                                 base_p and not h.tent_top, base_p and not h.tent_bot, hole=h))
        st = self.stk
        edge = self.B.boundary
        for rc in self.board.routs:
            if o.rout == "ignore":
                break
            g = rc.region() if callable(rc.region) else rc.region
            if g is None or g.is_empty:
                continue
            top_i, bot_i = st.span(rc.start, rc.end)
            keep = [p for p in iter_polys(g) if not (o.rout == "auto" and p.intersects(edge))]
            reg = union_all(keep)
            if reg.is_empty:
                continue
            self.cuts.append(Cut(reg, st.stack[bot_i].z0, st.stack[top_i].z1, "rout", True, True))
            self.hole_notes.append(f"rout layer {rc.layer}: {len(keep)} internal cut-out(s) applied")
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
        if base is None or base.is_empty or z1 - z0 <= 1e-9:
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
            out.extend(self.geo.prisms(reg, a, b))
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

    def _resolve_tents(self):
        """Tenting closes a via's own opening in the solder mask. A via inside
        the opening of a pad or of exposed copper stays open: the mask layer has
        no mask there, and a fab puts no mask on a pad or into an opening the
        artwork asks for (it plugs, fills or caps such a via instead)."""
        self._tent_region = {}
        n = len(self.stk.stack)
        for side in ("top", "bottom"):
            flag = "tent_top" if side == "top" else "tent_bot"
            for h in self.holes:              # a side the hole does not reach has nothing to tent
                if getattr(h, flag) and (h.top_idx != 0 if side == "top" else h.bot_idx != n - 1):
                    setattr(h, flag, False)
            hs = [h for h in self.holes if h.plated and getattr(h, flag)]
            masks = [] if self.opts.no_mask else self.stk.coats[side].get("mask", [])
            if hs and not masks:
                for h in hs:
                    setattr(h, flag, False)
                self.prot_notes.append(f"tenting {side}: {len(hs)} tented hole(s) left open: no solder mask "
                                       "on this side")
            if not hs or not masks:
                continue
            splits = [None if (L.mask_split_fn is None or L.polarity.upper().startswith("NEG"))
                      else L.mask_split_fn() for L in masks]
            if all(sp is not None for sp in splits):
                region, left_open, closed, how = self._tents_from_data(hs, splits)
            else:
                region, left_open, closed, how = self._tents_by_shape(side, hs, masks)
            self._tent_region[side] = region
            for h in left_open:
                setattr(h, flag, False)
            covered = len(hs) - len(left_open) - closed
            parts = []
            if closed:
                parts.append(f"{closed} via opening(s) closed")
            if covered:
                parts.append(f"{covered} already covered by the mask data")
            if left_open:
                parts.append(f"{len(left_open)} inside the opening of a pad or exposed copper left open "
                             "(no mask there in the data)")
            self.prot_notes.append(f"tenting {side}: {len(hs)} tented hole(s): " + "; ".join(parts) + f" [{how}]")

    def _tents_from_data(self, hs, splits):
        """The data marks the vias' own openings: close those of the tented
        vias, minus every other opening (pads, exposed copper, untented vias)."""
        other = clean(ginter(union_all([sp[0] for sp in splits]), self.B))
        idx = self._hole_index(plated_only=True)
        tented = {id(h) for h in hs}
        closing, staying, closed = [], [], set()
        for _, own in splits:
            for x, y, g in own:
                h = idx.find(x, y)
                if h is not None and id(h) in tented:
                    closing.append(g)
                    closed.add(id(h))
                else:
                    staying.append(g)
        shapely.prepare(other)
        left_open = [h for h in hs if not other.is_empty and shapely.contains_xy(other, h.x, h.y)]
        region = clean(ginter(gdiff(union_all(closing), union_all([other] + staying)), self.B)) \
            if closing else EMPTY
        n_closed = len(closed - {id(h) for h in left_open})
        return region, left_open, n_closed, "via openings from the fab data"

    def _tents_by_shape(self, side, hs, masks):
        """The data does not mark via openings: an opening is the via's own when
        it is a disc around the via, at most 0.5 mm wider than the drill radius,
        with no other hole in it and no paste on it (a round pad around a via
        in pad has paste)."""
        openings = union_all(self.image(L) for L in masks)
        polys = list(iter_polys(openings))
        if not polys:
            return EMPTY, [], 0, "no openings in the mask data"
        tree = shapely.STRtree(polys)
        paste = union_all(self.image(L) for L in self.stk.coats[side].get("paste", []))
        pts = [h for h in self.holes if h.plated]
        ptree = shapely.STRtree([Point(h.x, h.y) for h in pts])
        out, left_open = [], []
        for h in hs:
            p = Point(h.x, h.y)
            P = next((polys[int(i)] for i in tree.query(p) if polys[int(i)].contains(p)), None)
            if P is None:
                continue
            r_max = max(math.hypot(x - h.x, y - h.y) for x, y in P.exterior.coords)
            own = (r_max <= h.r_drill + 0.5 and P.area >= 0.8 * math.pi * r_max ** 2
                   and len(ptree.query(P, predicate="contains")) <= 1
                   and (paste.is_empty or not paste.contains(p)))
            if own:
                out.append(Polygon(P.exterior))
            else:
                left_open.append(h)
        return union_all(out), left_open, len(out), "via openings by shape: round, around one via, no paste"

    def _comp(self, name, color, solids, layer=None, kind=""):
        if solids:
            self.components.append(dict(name=name, color=color, solids=solids, layer=layer or name, kind=kind))

    def build(self):
        exact = self.geo is Brep
        if exact:
            OCC.load()
        elif self.cones:
            self._cone_steps()
        o = self.opts
        st = self.stk.stack
        n = len(st)
        cache: Dict[tuple, object] = {}
        t0 = time.time()
        ext = getattr(o, "external_only", False)
        cu_img = {L.key: self.copper_image(L) for i, L in enumerate(st)
                  if L.kind == "copper" and not (ext and 0 < i < n - 1)}
        for k, g in cu_img.items():
            self.svg[f"copper_{k}"] = g
        diel_extra: Dict[int, list] = defaultdict(list)
        stack_comps = []
        if ext and n > 2:
            # outer copper only; everything between is one core body, like Altium's STEP
            z0, z1 = st[-1].z1, st[0].z0
            core = self.slab(self.B, z0, z1, cache, label="dielectric_core")
            self._comp(f"Core ({z1 - z0:.4f} mm, inner layers merged)", o.board_color, core, "CORE", "core")
        for i, L in enumerate(st):
            if ext and n > 2 and 0 < i < n - 1:
                continue
            info(f"  building {L.name} ...", 2)
            if L.kind == "copper":
                solids = self.slab(cu_img[L.key], L.z0, L.z1, cache)
                stack_comps.append((i, L, solids))
                if 0 < i < n - 1 and L.t > 1e-9:
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
                self._comp(name, o.copper_color, solids, L.name, "copper")
            else:
                bits = [b for b in (L.material, L.dielectric_type.lower() if L.dielectric_type else "") if b]
                bits.append(f"{L.t:.4f} mm")
                if L.dk:
                    bits.append(f"Dk {L.dk:g}")
                if L.df:
                    bits.append(f"Df {L.df:g}")
                self._comp(f"{L.name} ({', '.join(bits)})", o.board_color, solids, L.name, "dielectric")
        info(f"  stackup bodies built ({time.time() - t0:.1f} s)", 2)
        for side in ("top", "bottom"):
            self._build_coatings(side, cu_img, cache)
        self._build_hole_bodies()
        if self.cones and exact:
            self._apply_cones()

    CONE_STEPS = 8

    def _cone_steps(self):
        """Meshes for the viewer: a countersink as a stack of discs (the exact
        cone is cut from the solids)."""
        for cn in self.cones:
            n = self.CONE_STEPS
            for k in range(n):
                za = cn.z_wide + (cn.z_narrow - cn.z_wide) * k / n
                zb = cn.z_wide + (cn.z_narrow - cn.z_wide) * (k + 1) / n
                r = cn.r_wide + (cn.r_narrow - cn.r_wide) * (k + 0.5) / n
                self.cuts.append(Cut(circle(cn.x, cn.y, r), min(za, zb), max(za, zb), "cone", False, False,
                                     cid=len(self.cuts)))

    def _fill_target(self, i):
        st = self.stk.stack
        up = i - 1 if i - 1 >= 0 and st[i - 1].kind == "dielectric" else None
        dn = i + 1 if i + 1 < len(st) and st[i + 1].kind == "dielectric" else None
        for j in (dn, up):
            if j is not None and st[j].dielectric_type == "PREPREG":
                return j
        return dn if dn is not None else up

    def _finish_color(self, material: str):
        o = self.opts
        if o.finish_color != "auto":
            return o.finish_color
        return parse_color(finish_color_for(o.finish_type or material))

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
            M = self.mask_cover(side)
            t_m = max(L.t for L in masks)
            lower = clean(gdiff(gdiff(M, C), holes))
            upper = clean(gdiff(M, pierce))
            self.svg[f"mask_{side}"] = upper
            solids = []
            if cu is not None:
                solids += self.geo.prisms(lower, *zr(z_lam, z_cu))
            solids += self.geo.prisms(upper, *zr(z_cu, z_cu + sg * t_m))
            L0 = masks[0]
            nm = f"{L0.name} (solder mask {t_m * 1000:.1f} um" + (f", Dk {L0.dk:g}" if L0.dk else "") + ")"
            self._comp(nm, o.mask_color, solids, L0.name, "mask")
            z_mask_top = z_cu + sg * t_m
        else:
            z_mask_top = None
        # surface finish
        fins = coats.get("finish", []) if not o.no_finish else []
        F = EMPTY
        t_f = 0.0
        fin_name, fin_mat = None, o.finish_type or ""
        if fins:
            t_f = max(L.t for L in fins)
            fin_name = fins[0].name
            fin_mat = o.finish_type or fins[0].material
            img = union_all(self.image(L) for L in fins)
            if img.is_empty:
                img = C if M is None else gdiff(C, M)
            F = ginter(img, C)
        elif o.finish_thickness and cu is not None and not o.no_finish:
            t_f = o.finish_thickness
            fin_name = f"{side.upper()}_SURFACE_FINISH"
            F = C if M is None else gdiff(C, M)
        if t_f > 0 and not F.is_empty:
            F = clean(gdiff(gdiff(F, holes_open), pierce))
            self.svg[f"finish_{side}"] = F
            solids = self.geo.prisms(F, *zr(z_cu, z_cu + sg * t_f))
            nm = f"{fin_name} (surface finish{', ' + fin_mat if fin_mat else ''}, {t_f * 1000:.2f} um)"
            self._comp(nm, self._finish_color(fin_mat), solids, fin_name, "finish")
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
                solids = self.geo.prisms(S, *zr(z_mask_top, z_mask_top + sg * t_s))
            else:
                S = clean(gdiff(S, holes))
                lower = clean(gdiff(S, C))
                upper = clean(gdiff(S, F)) if t_f > 0 else S
                if cu is not None:
                    solids += self.geo.prisms(lower, *zr(z_lam, z_cu))
                solids += self.geo.prisms(upper, *zr(z_cu, z_cu + sg * t_s))
            self.svg[f"silk_{side}"] = S
            self._comp(f"{silks[0].name} (silkscreen {t_s * 1000:.1f} um)", o.silk_color, solids, silks[0].name,
                       "silk")
        # solder paste
        pastes = coats.get("paste", []) if o.paste else []
        if pastes:
            P = clean(gdiff(union_all(self.image(L) for L in pastes), holes_open))
            t_p = max(L.t for L in pastes)
            z0 = z_cu + sg * t_f
            solids = self.geo.prisms(P, *zr(z0, z0 + sg * t_p))
            self._comp(f"{pastes[0].name} (solder paste {t_p * 1000:.0f} um)", o.paste_color, solids, pastes[0].name,
                       "paste")

    def _build_hole_bodies(self):
        o = self.opts
        plating, fills, plugs, caps = [], [], [], []
        fill_mats = Counter()
        tps = Counter()
        for h in self.holes:
            if not h.plated:
                continue
            tp = h.t_plating
            tps[("via" if h.kind == "via" else "pth", round(tp * 1000, 1))] += 1
            ct = self._cap_t(h, "top") if h.cap_top > 0 else 0.0
            cb = self._cap_t(h, "bot") if h.cap_bot > 0 else 0.0
            bore_t, bore_b = h.bores.get("top"), h.bores.get("bottom")
            # the plated barrel; on a filled back drilled side it ends at the bore
            zlo = h.z_lo_eff + (0.0 if bore_b else cb)
            zhi = h.z_hi_eff - (0.0 if bore_t else ct)
            if h.fill:
                if bore_t:
                    r_b, z_e = bore_t
                    s = self.geo.disc_prism(h.x, h.y, r_b, 0.0, z_e, h.z_hi - ct)
                    fills.append((h.fill, [s] if s is not None else []))
                if bore_b:
                    r_b, z_e = bore_b
                    s = self.geo.disc_prism(h.x, h.y, r_b, 0.0, h.z_lo + cb, z_e)
                    fills.append((h.fill, [s] if s is not None else []))
            bottom_cup = None
            if h.laser:
                # plated cup: barrel plus plating on the target land
                if h.entry == "top" and h.z_lo_eff == h.z_lo:
                    bottom_cup = (h.z_lo_eff, h.z_lo_eff + tp)
                elif h.entry == "bottom" and h.z_hi_eff == h.z_hi:
                    bottom_cup = (h.z_hi_eff - tp, h.z_hi_eff)
            if zhi - zlo > 1e-6:      # barrel left (a via can be back drilled from both sides)
                if h.round:
                    s = self.geo.disc_prism(h.x, h.y, h.r_drill, h.r_fin, zlo, zhi)
                    if s is not None:
                        plating.append(s)
                    if bottom_cup and h.r_fin > 0:
                        s = self.geo.disc_prism(h.x, h.y, h.r_fin, 0.0, *bottom_cup)
                        if s is not None:
                            plating.append(s)
                else:
                    ring = clean(gdiff(h.geom_drill, h.geom_fin))
                    plating += self.geo.prisms(ring, zlo, zhi)
                if h.dual:
                    r_dd, z_end, side = h.dual
                    surf = self._surface(side)
                    a, b = (z_end, surf) if side == "top" else (surf, z_end)
                    s = self.geo.disc_prism(h.x, h.y, r_dd, max(r_dd - tp, h.r_fin), a, b)
                    if s is not None:
                        plating.append(s)
                    fa, fb = (z_end, z_end + tp) if side == "top" else (z_end - tp, z_end)
                    s = self.geo.disc_prism(h.x, h.y, max(r_dd - tp, h.r_fin), h.r_fin, fa, fb)
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
                            s = self.geo.disc_prism(h.x, h.y, h.r_fin, 0.0, f_lo, f_hi)
                            fills.append((h.fill, [s] if s is not None else []))
                        else:
                            fills.append((h.fill, self.geo.prisms(h.geom_fin, f_lo, f_hi)))
                    else:
                        Ltot = f_hi - f_lo
                        for frac, a, b in ((h.plug_top, f_hi - h.plug_top * Ltot, f_hi),
                                           (h.plug_bot, f_lo, f_lo + h.plug_bot * Ltot)):
                            if frac > 0:
                                if h.round:
                                    s = self.geo.disc_prism(h.x, h.y, h.r_fin, 0.0, a, b)
                                    if s is not None:
                                        plugs.append(s)
                                else:
                                    plugs += self.geo.prisms(h.geom_fin, a, b)
            # caps close the drilled hole (or the filled back drill bore) flush with
            # the land surface (the land copper thickness already includes the cap plating)
            for r, a, b, bore in ((h.cap_top, h.z_hi - ct, h.z_hi, bore_t), (h.cap_bot, h.z_lo, h.z_lo + cb, bore_b)):
                if r <= 0 or b - a <= 1e-9:
                    continue
                if bore:
                    s = self.geo.disc_prism(h.x, h.y, bore[0], 0.0, a, b)
                    if s is not None:
                        caps.append(s)
                elif h.round:
                    s = self.geo.disc_prism(h.x, h.y, h.r_drill, 0.0, a, b)
                    if s is not None:
                        caps.append(s)
                else:
                    caps += self.geo.prisms(h.geom_drill, a, b)
        um = sorted({t for (_, t) in tps})
        if len(um) <= 1:
            label = f"Hole plating ({(um[0] if um else (o.plating_thickness or DEFAULTS['plating']) * 1000):.0f} um)"
        else:
            parts = []
            for kind in ("pth", "via"):
                ts = sorted({t for (k, t) in tps if k == kind})
                if ts:
                    who = "PTH" if kind == "pth" else "vias"
                    parts.append(f"{who} " + "/".join(f"{t:g}" for t in ts) + " um")
            label = "Hole plating (" + ", ".join(parts) + ")"
        self._comp(label, o.plating_color, plating, "PLATING", "plating")
        by_mat = defaultdict(list)
        for mat, ss in fills:
            by_mat[mat].extend(ss)
        for mat, ss in by_mat.items():
            col = o.fill_color if o.fill_color != "auto" else parse_color(
                {"copper": "copper", "silver": "silver"}.get(mat, "epoxy"))
            self._comp(f"Via fill ({mat})", col, ss, "VIA_FILL", "fill")
        col = o.fill_color if o.fill_color != "auto" else parse_color("epoxy")
        self._comp("Via plug (epoxy)", col, plugs, "VIA_PLUG", "plug")
        self._comp(f"Via caps ({o.cap_thickness * 1000:.0f} um)", o.cap_color, caps, "VIA_CAP", "cap")

    def _apply_cones(self):
        gp, BP = OCC.gp, OCC.BP
        tools = []
        for c in self.cones:
            sg = 1.0 if c.side == "top" else -1.0
            h = abs(c.z_wide - c.z_narrow)
            # The cone ends on the hole wall. Running its tip on into the (empty)
            # hole by 10 um makes the cone cross the wall instead of touching it
            # with its end edge, which left zero-width faces after the STEP round trip.
            ext = min(CONE_OVERRUN, 0.5 * c.r_narrow)
            r_tip = c.r_narrow - ext
            h_tool = h * (c.r_wide - r_tip) / max(c.r_wide - c.r_narrow, 1e-12)
            z_tip = c.z_wide - sg * h_tool
            ax = gp.gp_Ax2(gp.gp_Pnt(c.x, c.y, z_tip), gp.gp_Dir(0, 0, sg))
            cone = BP.BRepPrimAPI_MakeCone(ax, r_tip, c.r_wide, h_tool).Shape()
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
        bd = self.board
        info(f"\npcbfab2step {__version__}")
        info(f"Source   : {bd.source}")
        others = [s for s in bd.other_steps if s.lower() != (bd.step or "").lower()]
        line = f"Board    : {bd.name}"
        if bd.step:
            line += f"   step {bd.step}" + (f" (other steps: {', '.join(others)})" if others else "")
        if bd.instances > 1:
            line += f"   [{bd.instances} step-repeat instances]"
        info(line)
        for nt in bd.notes:
            info("  " + nt)
        x0, y0, x1, y1 = self.B.bounds
        nh = sum(len(p.interiors) for p in iter_polys(self.B))
        info(f"Outline  : {x1 - x0:.3f} x {y1 - y0:.3f} mm, {len(list(iter_polys(self.B)))} island(s), {nh} cut-out(s)")
        if bd.hint_notes:
            info("Secondary data:")
            for nt in bd.hint_notes:
                info("  " + nt)
        info("\nStackup (z = 0 at the bottom of the lowest copper layer):")
        info(f"  {'layer':26s} {'kind':11s} {'thickness':>12s}  {'z range (mm)':>19s}  source")

        def row(L, kind, z0, z1):
            t = abs(z1 - z0)
            tt = f"{t * 1000:.2f} um" if t < 0.1 else f"{t:.4f} mm"
            note = ""
            if L.kind in COATINGS and not L.implicit:
                if L.empty or L.raw_image().is_empty:
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
            tline += f"   ({st.board_attr_src} {st.board_attr:.4f} mm, difference {diff * 1000:+.1f} um)"
        info(tline)
        for nt in st.notes:
            info("  note: " + nt)
        info("\nHoles:")
        groups = Counter()
        for h in self.holes:
            if h.stub:
                continue
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
        if bd.report_extra is not None:
            bd.report_extra()


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
