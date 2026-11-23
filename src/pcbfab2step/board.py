"""The shared, format-neutral board description ("virtual objects").

Every reader (ODB++, IPC-2581, ...) turns its fab data into a Board; the
builder turns a Board into solids. Coordinates are millimetres in the board
frame of the fab data; layers are listed top first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .geometry import EMPTY, circle

COPPER, DIELECTRIC = "copper", "dielectric"
COATINGS = ("mask", "silk", "finish", "paste")


@dataclass
class Layer:
    """One layer of the fab data. kind: copper, dielectric, mask, silk, finish,
    paste, drill, rout or doc."""
    row: int
    name: str
    kind: str
    polarity: str = "POSITIVE"
    context: str = "BOARD"
    type: str = ""                   # layer type of the source format
    add_type: str = ""               # drill subtype: BACKDRILL, COUNTERHOLE, COUNTERBORE, COUNTERSINK, ...
    start: str = ""                  # drill / rout span: first and last layer
    end: str = ""
    dielectric_type: str = ""        # CORE / PREPREG
    dielectric_name: str = ""
    side_hint: str = ""              # top / bottom when the data says
    t_data: Optional[float] = None   # thickness from the fab data (mm), None if not given
    t_data_src: str = ""
    material: str = ""
    dk: Optional[float] = None
    df: Optional[float] = None
    empty: bool = False              # the layer has no features
    attrs: Dict[str, object] = field(default_factory=dict)
    image_fn: Optional[Callable[[], object]] = None   # raw 2D image (not clipped)
    points_fn: Optional[Callable[[], list]] = None    # [(x, y, size)] of the features (document layers)
    points_note: str = ""            # what points_fn used (report)
    implicit: bool = False
    # set by the builder
    side: str = ""
    t: float = 0.0
    t_src: str = ""
    z0: float = 0.0
    z1: float = 0.0
    _img: object = None

    @property
    def key(self) -> str:
        return self.name.lower()

    def raw_image(self):
        if self._img is None:
            self._img = self.image_fn() if self.image_fn is not None else EMPTY
        return self._img


@dataclass
class Hole:
    """A drilled (or lasered) hole. Readers fill the first block; the builder
    the rest (drilled radius, stack indices, z range)."""
    x: float
    y: float
    r_fin: float
    plated: bool
    kind: str                        # via | pth | npth
    layer: str                       # drill layer name
    start: str = ""                  # span: first and last layer
    end: str = ""
    laser: bool = False
    dir_hint: str = ""               # top / bottom drill direction, if the data says
    r_drill_data: Optional[float] = None   # drilled radius from the data (tool DRILL_SIZE)
    geom_fin: object = None          # non-round holes (slots)
    attrs: Optional[dict] = None
    # via protection (IPC-4761)
    fill: str = ""
    plug_top: float = 0.0
    plug_bot: float = 0.0
    cap_top: float = 0.0             # cap radius; -1 = take it from the copper land
    cap_bot: float = 0.0
    tent_top: bool = False
    tent_bot: bool = False
    prot_src: str = ""
    # set by the builder
    r_drill: float = 0.0
    t_plating: float = 0.0
    geom_drill: object = None
    top_idx: int = 0
    bot_idx: int = 0
    z_lo: float = 0.0
    z_hi: float = 0.0
    entry: str = "top"
    z_lo_eff: float = 0.0
    z_hi_eff: float = 0.0
    pierce: bool = True
    dual: Optional[tuple] = None     # (r_dd, z_end, side)
    # side -> (radius, z_end) of a back drill bore that the via fill closes
    bores: Dict[str, tuple] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def region_drill(self):
        return self.geom_drill if self.geom_drill is not None else circle(self.x, self.y, self.r_drill)

    def region_fin(self):
        return self.geom_fin if self.geom_fin is not None else circle(self.x, self.y, self.r_fin)

    @property
    def round(self) -> bool:
        return self.geom_drill is None and self.geom_fin is None


@dataclass
class Counterhole:
    """A counterbore or countersink at the surface of the board. type, angle
    and depth are None/empty when the fab data does not say; src tells where
    they came from."""
    x: float
    y: float
    d: float                         # diameter at the surface
    layer: str                       # source layer (report)
    start: str = ""                  # span (ODB++: through the copper layer at or below the bottom)
    end: str = ""
    dir_hint: str = ""
    geom: object = None              # non-round outline
    type: str = ""                   # counterbore | countersink
    angle: Optional[float] = None    # included angle, degrees
    depth: Optional[float] = None    # mm from the outer copper surface
    floor_layer: str = ""            # counterbore floor on the near face of this layer
    depth_range: Optional[Tuple[float, float]] = None   # depth the data allows (report)
    src: str = ""
    side: str = ""                   # top / bottom if the data says; set by the builder


@dataclass
class Backdrill:
    x: float
    y: float
    d: float
    layer: str
    start: str = ""
    end: str = ""
    dir_hint: str = ""
    depth: Optional[float] = None    # mm from the surface, if the data gives it
    stub: Optional[float] = None     # stub left above the must-not-cut layer, mm
    mnc: str = ""                    # must-not-cut layer, if the data names it
    src: str = ""


@dataclass
class DualDrill:
    x: float
    y: float
    d: float
    layer: str
    start: str = ""
    end: str = ""
    dir_hint: str = ""


@dataclass
class RoutCut:
    region: object                   # shapely geometry, or a callable returning it (lazy)
    layer: str
    start: str = ""
    end: str = ""
    count: int = 1


@dataclass
class CounterholeHint:
    """Counterhole type, angle or depth from a second source (IPC-2581
    padstacks, an NC drill report), matched to the board's counterholes by
    position, or by diameter and side when it has no positions."""
    src: str
    side: Optional[str]
    d: float                          # diameter at the surface
    type: str = ""
    angle: Optional[float] = None
    depth: Optional[float] = None     # counterbore depth from the outer copper surface, mm
    floor_layer: str = ""             # counterbore floor on the near face of this layer
    depth_range: Optional[Tuple[float, float]] = None   # counterbore depth the data allows (report)
    tip_depth: Optional[float] = None  # countersink depth to the cone tip, as the data gives it
    family: str = ""                  # source family for alignment, e.g. "NC drill", "IPC-2581"
    pts: Optional[List[Tuple[float, float]]] = None
    count: Optional[int] = None
    used: int = 0


@dataclass
class Board:
    """Everything a reader knows about the board."""
    name: str
    source: str                      # e.g. "ODB++ 8.1 (Altium Designer 26.1)"
    layers: List[Layer]              # top first; all layers of the data
    outline: object = EMPTY          # board outline incl. cut-outs (shapely)
    board_thickness: Optional[float] = None
    board_thickness_src: str = ""
    holes: List[Hole] = field(default_factory=list)
    counterholes: List[Counterhole] = field(default_factory=list)
    backdrills: List[Backdrill] = field(default_factory=list)
    duals: List[DualDrill] = field(default_factory=list)
    routs: List[RoutCut] = field(default_factory=list)
    hints: List[CounterholeHint] = field(default_factory=list)
    hint_notes: List[str] = field(default_factory=list)      # what the secondary sources gave
    counter_flags: Tuple[Optional[bool], Optional[bool]] = (None, None)   # (counterbores, countersinks)
    units: str = "MM"
    step: str = ""
    other_steps: List[str] = field(default_factory=list)
    instances: int = 1               # step-and-repeat instances
    notes: List[str] = field(default_factory=list)          # report lines
    prot_notes: List[str] = field(default_factory=list)     # via protection report lines
    report_extra: Optional[Callable[[], None]] = None       # reader-specific lines at the end of the report
    after_build: Optional[Callable[[], None]] = None        # reader-specific report after the build


# --------------------------------------------------------------------------
# IPC-4761 via protection
# --------------------------------------------------------------------------

FILL_TOKENS = {"fill", "filling", "filled"}
CAP_TOKENS = {"cap", "capping", "capped", "caps"}
PLUG_TOKENS = {"plug", "plugging", "plugged"}
TENT_TOKENS = {"tent", "tenting", "tented", "cover", "covering", "covered"}
COUNTER_TOKENS = {"counterhole", "counterholes", "counterbore", "counterbores", "countersink", "countersinks",
                  "cbore", "csink", "csk"}

IPC_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7}


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
