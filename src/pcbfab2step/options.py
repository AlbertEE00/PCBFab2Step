# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""The GUI's options, and how each becomes a command line flag.

The GUI keeps no settings of its own: its state is the inputs, the output
and a dict of option values, and build_argv() turns that into the command
line it runs (and shows). parse_argv() reads a command line back into that
state, so `pcbfab2step gui FAB --show paste` opens with paste on. Every option
here is a flag of the command line or a config.toml setting given with --set.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Dict, List, Optional

from .config import NAMED_COLORS, SCHEMA

SETTING = {attr: (sec, key, kind, builtin, tmpl, help_) for sec, key, attr, kind, builtin, tmpl, help_ in SCHEMA}

SHOW = ("mask", "silk", "finish", "paste", "components")            # --show / --hide
COLOR_ITEM = {"board_color": "board", "copper_color": "copper", "mask_color": "mask", "silk_color": "silk",
              "finish_color": "finish", "plating_color": "plating", "fill_color": "fill", "cap_color": "cap",
              "paste_color": "paste"}                                 # --color ITEM=COLOR
THICK_ITEM = {"board_thickness": "board", "copper_thickness": "copper", "mask_thickness": "mask",
              "silk_thickness": "silk", "finish_thickness": "finish", "paste_thickness": "paste",
              "plating_thickness": "plating", "via_plating_thickness": "via-plating", "cap_thickness": "cap",
              "stencil_thickness": "stencil"}                         # --thickness ITEM=SIZE
COUNTERHOLE_FLAG = {"counterhole": "--counterhole", "counterhole_top": "--counterhole-top",
                    "counterhole_bottom": "--counterhole-bottom"}
EXPORT_ONLY = ("invert", "outline", "aperture_offset")                # only with --export
MODEL_ONLY = SHOW                                                     # --show / --hide: not with --export

# Options that are flags, not config.toml settings: (type, label, help, choices)
FLAGS = {
    "vias_drilled": ("choice", "Drill the vias", "undrilled: the vias are left out, their pads stay (--vias "
                     "undrilled); drilled: drill them even with --sanitize", ("drilled", "undrilled")),
    "counterhole": ("counterhole", "Both sides", "counterholes without type, angle or depth in the data "
                    "(--counterhole)", ()),
    "counterhole_top": ("counterhole", "Top side", "wins over both sides (--counterhole-top)", ()),
    "counterhole_bottom": ("counterhole", "Bottom side", "wins over both sides (--counterhole-bottom)", ()),
    "layer_thickness": ("lines", "Layers", "a layer of the fab data: NAME=SIZE, one per line (--thickness)", ()),
    "config": ("path", "Settings file", "default: config.toml in the folder given (--config)", ()),
    "no_config": ("check", "Ignore config.toml", "--no-config", ()),
    "invert": ("check", "Invert", "the rest of the outline instead: mask openings, the paste of a stencil, the "
               "clearances in the copper (--invert)", ()),
    "outline": ("choice", "Board outline", "DXF and SVG have it unless left out; a STEP layer only with it (the "
                "PCB body) (--outline / --no-outline)", ("with", "without")),
    "format": ("choice", "Format", "the model as STEP or as a mesh file (--format)",
               ("step", "glb", "gltf", "obj", "fbx", "stl", "ply", "3mf")),
}

LABELS = {
    "sanitize": "Level", "mask": "Solder mask", "silk": "Silkscreen", "finish": "Surface finish",
    "paste": "Solder paste", "components": "Components", "mask_color": "Colour", "mask_thickness": "Thickness",
    "drill_through_mask": "Vias cut the mask", "silk_color": "Colour", "silk_thickness": "Thickness",
    "clip_silk": "Clip to the mask openings", "finish_type": "Type", "finish_color": "Colour",
    "finish_thickness": "Thickness", "paste_color": "Colour", "paste_thickness": "Thickness",
    "copper_color": "Copper colour", "copper_thickness": "Copper thickness", "board_color": "Laminate colour",
    "board_thickness": "Board thickness", "plating_thickness": "Plating", "via_plating_thickness": "Via plating",
    "plating_color": "Colour", "via_fill": "Fill", "via_cap": "Cap", "via_tent": "Tent", "via_plug": "Plug",
    "via_max_diameter": "Up to diameter", "via_protection": "IPC-4761 rules", "fill_material": "Fill material",
    "plug_depth": "Plug depth", "cap_thickness": "Cap thickness", "fill_color": "Fill colour",
    "cap_color": "Cap colour", "counterhole_depth_mode": "Depth without data", "counterbore_rules": "Counterbores by size",
    "countersink_rules": "Countersinks by size", "backdrill_stub": "Stub", "use_sources": "Use",
    "odb_step": "ODB++ step", "schema": "STEP protocol", "svg_dir": "Region SVGs to", "rout": "Rout layers",
    "arc_tolerance": "Arc tolerance", "arc_fit": "Arc fit", "max_holes_per_face": "Holes per face",
    "aperture_offset": "Aperture offset", "stencil_thickness": "Stencil thickness",
    "silk_simplify": "Simplify", "silk_body": "Body", "simplify": "Simplify", "hide_bodies": "Bodies",
    "mesh_from": "Meshes from",
}

# (id, title, switch option or None, options): the order of the GUI and of the command line
GROUPS = [
    ("sanitize", "Sanitize", "sanitize", ["sanitize"]),
    ("mask", "Solder mask", "mask", ["mask_color", "mask_thickness", "drill_through_mask"]),
    ("silk", "Silkscreen", "silk", ["silk_color", "silk_thickness", "clip_silk", "silk_simplify", "silk_body"]),
    ("finish", "Surface finish", "finish", ["finish_type", "finish_color", "finish_thickness"]),
    ("paste", "Solder paste", "paste", ["paste_color", "paste_thickness"]),
    ("components", "Components", "components", []),
    ("bodies", "Bodies left out", None, ["hide_bodies"]),
    ("stack", "Copper and laminate", None, ["copper_color", "copper_thickness", "board_color", "board_thickness",
                                            "layer_thickness"]),
    ("vias", "Vias", None, ["vias_drilled", "via_fill", "via_cap", "via_tent", "via_plug", "via_max_diameter",
                            "via_protection", "fill_material", "plug_depth", "cap_thickness", "fill_color",
                            "cap_color"]),
    ("plating", "Hole plating", None, ["plating_thickness", "via_plating_thickness", "plating_color"]),
    ("counterholes", "Counterholes", None, ["counterhole", "counterhole_top", "counterhole_bottom",
                                            "counterhole_depth_mode", "counterbore_rules", "countersink_rules"]),
    ("backdrill", "Back drills", None, ["backdrill_stub"]),
    ("sources", "Sources", None, ["use_sources", "odb_step", "config", "no_config"]),
    ("file", "Output file", None, ["schema", "mesh_from", "svg_dir"]),
    ("geometry", "Geometry", None, ["simplify", "rout", "arc_tolerance", "arc_fit", "max_holes_per_face"]),
]
EXPORT_GROUP = ["invert", "outline", "aperture_offset", "stencil_thickness"]


def _gui_type(attr: str):
    if attr in FLAGS:
        return FLAGS[attr][0], FLAGS[attr][3]
    sec, key, kind, builtin, tmpl, help_ = SETTING[attr]
    if attr == "sanitize":
        return "choice", ("outer", "exposed", "pads")
    if attr in SHOW:
        return "switch", ()
    if kind == "bool":
        return ("tristate", ("true", "false")) if builtin is None else ("check", ())
    if isinstance(kind, tuple):
        return "choice", kind
    return {"mm": "size", "length": "size", "float": "number", "int": "number", "color": "color",
            "color_auto": "color", "list": "lines", "str": "text"}[kind], ()


def _default_text(attr: str) -> str:
    if attr in FLAGS:
        return {"vias_drilled": "as the fab data and --sanitize say", "outline": "DXF, SVG: with; STEP: without",
                "config": "config.toml in the folder"}.get(attr, "")
    sec, key, kind, builtin, tmpl, help_ = SETTING[attr]
    if attr == "sanitize":
        return "exposed"
    if builtin is None or tmpl == "default":
        return "from the fab data"
    if kind == "bool":
        return "on" if builtin else "off"
    if kind in ("mm",):
        return f"{builtin:g} mm"
    if kind == "list":
        return ""
    return str(builtin)


def flag_hint(attr: str) -> str:
    """The command line form of an option, for the GUI's tooltips."""
    if attr in COLOR_ITEM:
        return f"--color {COLOR_ITEM[attr]}=COLOR"
    if attr in THICK_ITEM:
        return f"--thickness {THICK_ITEM[attr]}=SIZE"
    if attr in SHOW:
        return f"--show {attr} / --hide {attr}"
    if attr in COUNTERHOLE_FLAG:
        return f"{COUNTERHOLE_FLAG[attr]} type=bore|sink,angle=DEG,depth=SIZE"
    hints = {"sanitize": "--sanitize outer|exposed|pads", "use_sources": "--sources LIST", "config": "--config FILE",
             "format": "--format step|glb|gltf|obj|fbx|stl|ply|3mf",
             "hide_bodies": "--hide silk-bot,solder-top,copper-2,components-top,U1,...",
             "no_config": "--no-config", "vias_drilled": "--vias drilled|undrilled", "via_protection": "--vias RULE",
             "layer_thickness": "--thickness LAYER=SIZE", "aperture_offset": "--aperture-offset SIZE",
             "invert": "--invert", "outline": "--outline / --no-outline"}
    if attr in hints:
        return hints[attr]
    sec, key, *_ = SETTING[attr]
    return f"--set {sec}.{key}=VALUE"


def option_model() -> dict:
    """What the GUI draws: groups of options with their type, label, help,
    default and command line form. A group's switch turns its feature on or
    off (show/hide, or --sanitize with its level inside the group)."""
    def opt(attr):
        typ, choices = _gui_type(attr)
        help_ = FLAGS[attr][2] if attr in FLAGS else SETTING[attr][5]
        label = FLAGS[attr][1] if attr in FLAGS else LABELS.get(attr, attr.replace("_", " ").capitalize())
        return dict(id=attr, type=typ, label=label, help=help_, choices=list(choices), default=_default_text(attr),
                    flag=flag_hint(attr))
    groups = []
    for gid, title, switch, attrs in GROUPS:
        sw = None
        if switch == "sanitize":
            sw = dict(id="sanitize", on="exposed", off=None, default=False, flag=flag_hint("sanitize"),
                      help=SETTING["sanitize"][5])
        elif switch:
            sw = dict(id=switch, on=True, off=False, default=bool(SETTING[switch][3]), flag=flag_hint(switch),
                      help=SETTING[switch][5])
        groups.append(dict(id=gid, title=title, switch=sw, options=[opt(a) for a in attrs if a != switch or
                                                                    a == "sanitize"]))
    from .config import parse_color
    hexes = {n: "#" + "".join(f"{round(c * 255):02x}" for c in parse_color(v)) for n, v in NAMED_COLORS.items()}
    return dict(groups=groups, export=[opt(a) for a in EXPORT_GROUP], colors=sorted(NAMED_COLORS),
                color_hex=hexes, show=list(SHOW))


# --------------------------------------------------------------------------
# state -> command line
# --------------------------------------------------------------------------

def _toml(kind, v) -> str:
    if kind == "list":
        items = [s.strip() for s in str(v).splitlines() if s.strip()] if isinstance(v, str) else list(v)
        return "[" + ", ".join('"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"' for s in items) + "]"
    if kind == "bool":
        return "true" if v in (True, "true") else "false"
    return str(v).strip()


def _lines(v) -> List[str]:
    return [s.strip() for s in (v.splitlines() if isinstance(v, str) else v) if str(s).strip()]


def _flags(attr: str, v, export: bool) -> List[str]:
    """The flags of one option value (show/hide are joined in build_argv)."""
    if attr in EXPORT_ONLY and not export:
        return []
    if attr in COLOR_ITEM:
        return ["--color", f"{COLOR_ITEM[attr]}={v}"]
    if attr in THICK_ITEM:
        return ["--thickness", f"{THICK_ITEM[attr]}={v}"]
    if attr == "sanitize":
        if v == "none":                           # off, over a config.toml that turns it on
            return ["--set", "model.sanitize=none"]
        return ["--sanitize", str(v)] if v else []
    if attr == "use_sources":
        return ["--sources", str(v)]
    if attr == "config":
        return ["--config", str(v)]
    if attr == "no_config":
        return ["--no-config"] if v else []
    if attr == "vias_drilled":
        return ["--vias", str(v)]
    if attr == "via_protection":
        return [x for r in _lines(v) for x in ("--vias", r)]
    if attr == "layer_thickness":
        return [x for r in _lines(v) for x in ("--thickness", r)]
    if attr in COUNTERHOLE_FLAG:
        return [COUNTERHOLE_FLAG[attr], str(v)] if str(v).strip() else []
    if attr == "aperture_offset":
        return [f"--aperture-offset={v}"]
    if attr == "invert":
        return ["--invert"] if v else []
    if attr == "outline":
        return {"with": ["--outline"], "without": ["--no-outline"]}.get(v, [])
    sec, key, kind, *_ = SETTING[attr]
    return ["--set", f"{sec}.{key}={_toml(kind, v)}"]


def ordered_attrs() -> List[str]:
    out = []
    for _gid, _title, switch, attrs in GROUPS:
        for a in ([switch] if switch else []) + attrs:
            if a not in out:
                out.append(a)
    return out + [a for a in EXPORT_GROUP if a not in out]


def build_argv(inputs: List[str], output: Optional[str], values: Dict[str, object],
               export: Optional[str] = None) -> List[str]:
    """The command line of a GUI state: the board model, or with export
    (TARGET:FORMAT) one layer. Options that do not act on that output are
    left out (show/hide on an export, export settings on the model)."""
    argv = [str(p) for p in inputs]
    if output and not export:
        argv += ["-o", str(output)]
    fmt = values.get("format")
    if fmt and fmt != "step" and not export:
        argv += ["--format", str(fmt)]
    if export:
        argv += ["--export", export]
    else:
        shown = [a for a in SHOW if values.get(a) is True]
        hidden = [a for a in SHOW if values.get(a) is False] + _lines(str(values.get("hide_bodies") or "").replace(",", "\n"))
        if shown:
            argv += ["--show", ",".join(shown)]
        if hidden:
            argv += ["--hide", ",".join(hidden)]
    for attr in ordered_attrs():
        if attr in SHOW or attr not in values or attr == "hide_bodies":
            continue
        v = values[attr]
        if v is None or v == "" or (isinstance(v, (list, tuple)) and not v):
            continue
        argv += _flags(attr, v, bool(export))
    return argv


def command_line(argv: List[str], windows: Optional[bool] = None) -> str:
    """The command as a person would type it (PowerShell / cmd quoting on Windows)."""
    windows = os.name == "nt" if windows is None else windows
    if not windows:
        return "pcbfab2step " + " ".join(shlex.quote(a) for a in argv)

    def q(a):
        if a and not re.search(r'[\s"\'`$&|;<>()\[\]{},@#*?]', a):
            return a
        if '"' not in a:
            return f'"{a}"'
        return "'" + a.replace("'", "''") + "'"
    return "pcbfab2step " + " ".join(q(a) for a in argv)


def display_value(kind, v) -> str:
    """A resolved setting as the GUI shows it next to its field."""
    from .config import parse_color
    if v is None:
        return ""
    if isinstance(v, bool):
        return "on" if v else "off"
    if kind in ("color", "color_auto"):
        if isinstance(v, str):
            return v
        for name, val in NAMED_COLORS.items():
            try:
                if all(abs(a - b) < 0.002 for a, b in zip(parse_color(val), v)):
                    return name
            except Exception:
                continue
        return "#" + "".join(f"{round(c * 255):02x}" for c in v)
    if kind in ("mm", "length") and isinstance(v, (int, float)):
        return f"{v:g} mm"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


# --------------------------------------------------------------------------
# command line -> state
# --------------------------------------------------------------------------

def parse_argv(argv: List[str]) -> dict:
    """{inputs, output, values, export} of a command line (raises SystemExit
    with the message on a bad one, as the command line would)."""
    from .cli import build_parser, join_counterhole_args, settings_from_args
    args = build_parser().parse_args(join_counterhole_args(list(argv)))
    c = settings_from_args(args)
    values: Dict[str, object] = {}
    set_attrs = set(getattr(c, "_labels", {}))
    for attr, (sec, key, kind, builtin, tmpl, help_) in SETTING.items():
        if attr in ("output",) or attr not in vars(c):
            continue
        if attr == "hide_bodies":
            if c.hide_bodies:
                values[attr] = "\n".join(c.hide_bodies)
            continue
        v = getattr(c, attr)
        if attr == "use_sources":
            values[attr] = str(v)
        elif kind == "list":
            if v:
                values[attr] = "\n".join(str(x) for x in v)
        elif attr in SHOW or (kind == "bool" and builtin is not None):
            values[attr] = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "on", "1")
        elif kind == "bool":
            values[attr] = "true" if v in (True, "true", "yes", "on", "1") else "false"
        elif attr in set_attrs or v is not None:
            values[attr] = str(v)
    if c.vias_drilled is not None:
        values["vias_drilled"] = "drilled" if c.vias_drilled else "undrilled"
    if c.layer_thickness:
        values["layer_thickness"] = "\n".join(c.layer_thickness)
    for attr in COUNTERHOLE_FLAG:
        specs = getattr(c, attr)
        if specs:
            values[attr] = ",".join(specs)
    if args.config:
        values["config"] = args.config
    if args.no_config:
        values["no_config"] = True
    if args.invert:
        values["invert"] = True
    if args.outline is not None:
        values["outline"] = "with" if args.outline else "without"
    if getattr(args, "format", None):
        values["format"] = args.format
    return dict(inputs=list(args.inputs), output=args.output, values=values, export=args.export,
                view=getattr(args, "view", None))
