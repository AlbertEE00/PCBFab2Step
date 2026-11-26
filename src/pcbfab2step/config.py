# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Settings: colours, the config.toml format, and the merge of command line,
config file and built-in defaults into one options object.

Precedence, highest first:

    command line  >  config file  >  fab data  >  built-in defaults

A config value of "default" (or a key left out) does not override: the fab
data is used where it has the value, else the built-in default. Any other
value overrides the fab data. The command line overrides the config file.
"""

from __future__ import annotations

import os
import re
import sys
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:                                    # pragma: no cover
    tomllib = None

CONFIG_NAME = "config.toml"
FAB_SUBDIRS = ("ODB++", "IPC2581", "GERBER", "NCDRILL", "STEP")

NAMED_COLORS = {
    "green": "#1b6e2d", "matte-green": "#2e5e33", "dark-green": "#0f4a1e", "blue": "#1f4e9c",
    "red": "#b02a2a", "black": "#141414", "matte-black": "#262626", "white": "#f2f2f2",
    "yellow": "#e8c51a", "purple": "#5e2b7e", "orange": "#d9661f", "grey": "#808080", "gray": "#808080",
    "clear": "#c8d8c0", "copper": "#c87533", "gold": "#e6be5a", "silver": "#c8c8c8", "tin": "#c8c8c8",
    "nickel": "#b4b4aa", "fr4": "#c9b97e", "laminate": "#cdbe91", "cream": "#e3d9be", "tan": "#cdbe91",
    "epoxy": "#3b3b33", "brown": "#6b4a2b",
}


class ConfigError(ValueError):
    pass


def parse_color(s) -> Tuple[float, float, float]:
    if isinstance(s, (list, tuple)) and len(s) == 3:
        v = [float(x) for x in s]
    else:
        t = str(s).strip().lower()
        t = NAMED_COLORS.get(t, t)
        m = re.match(r"^#?([0-9a-f]{6})$", t)
        if m:
            h = m.group(1)
            return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        parts = [p for p in re.split(r"[,\s]+", t) if p]
        if len(parts) != 3:
            raise ConfigError(f"bad colour '{s}' (use a name, #RRGGBB or R,G,B)")
        try:
            v = [float(p) for p in parts]
        except ValueError:
            raise ConfigError(f"bad colour '{s}' (use a name, #RRGGBB or R,G,B)")
    if max(v) > 1.0:
        v = [x / 255.0 for x in v]
    return tuple(min(1.0, max(0.0, x)) for x in v)


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
# Schema: one entry per setting
# --------------------------------------------------------------------------
# (section, key, attribute, kind, built-in default, template value, comment)
#   kind: "mm" (float >= 0), "float", "int", "bool", "str", "color", "color_auto",
#         a tuple of choices, "list" (list of strings)
#   template value: written by 'init'; "default" defers to the fab data.

SCHEMA = [
    ("output", "file", "output", "str", None, "default",
     "STEP model to write (default: PCBFab2Step/<board>.step in the folder given)"),
    ("output", "schema", "schema", ("AP214", "AP242", "AP203"), "AP214", "AP214", "STEP application protocol"),
    ("output", "svg_dir", "svg_dir", "str", None, "default", "also write the 2D layer regions as SVG files here"),
    ("output", "mesh", "mesh_from", ("preview", "exact"), "preview", "preview",
     "mesh files (glb, obj, fbx, stl, ...): the preview's meshes (quick) or the STEP's own solids, tessellated"),

    ("sources", "use", "use_sources", "str", None, "auto",
     "which of the found sources to use, the first geometry source as the board: odb, ipc, drill, step, "
     "e.g. \"ipc+drill+step\" (auto: all found, ODB++ before IPC-2581)"),
    ("sources", "odb_step", "odb_step", "str", None, "default", "ODB++ step to convert (default: pcb)"),

    ("model", "sanitize", "sanitize", ("none", "outer", "exposed", "pads"), "none", "none",
     "hide design detail: outer (inner layers merged into one core, vias not drilled), exposed (also "
     "outer copper cut to pads and the copper the solder mask leaves open), pads (outer copper: pads only)"),
    ("model", "components", "components", "bool", True, True,
     "add the components of the ECAD STEP export when one is found"),
    ("model", "hide", "hide_bodies", "list", [], [],
     "bodies left out of the model: silk-bot, solder-top, finish-bot, paste-top, copper-3, components-top, a "
     "designator (U1) or a layer name of the fab data; --hide adds to these, --show takes them back"),
    ("model", "mesh", "mesh_bodies", "list", [], [],
     "bodies written into the STEP as triangle meshes, not exact solids (names as for model.hide, or 'all'); "
     "a STEP with meshes is AP242"),
    ("model", "mesh_tolerance", "mesh_tolerance", "mm", 0.005, 0.005,
     "largest gap between a mesh and the exact surface, mm"),

    ("export", "stencil_thickness", "stencil_thickness", "mm", 0.1, 0.1,
     "stencil plate thickness for --export stencil-...:step, mm"),
    ("export", "aperture_offset", "aperture_offset", "length", 0.0, 0.0,
     "grow (+) or shrink (-) the features of an exported layer, e.g. stencil apertures: -0.05 mm"),

    ("stackup", "board_thickness", "board_thickness", "mm", None, "default",
     "copper-to-copper thickness, mm; scales the dielectrics to match (default: fab data)"),
    ("stackup", "copper_thickness", "copper_thickness", "mm", None, "default",
     "every copper layer, mm (default: fab data, else 0.035)"),

    ("copper", "color", "copper_color", "color", "copper", "copper", "copper layer colour"),
    ("dielectric", "color", "board_color", "color", "laminate", "laminate", "laminate colour"),

    ("soldermask", "enabled", "mask", "bool", True, True, "build the solder mask"),
    ("soldermask", "color", "mask_color", "color", "green", "green",
     "name (green, matte-green, blue, red, black, white, ...), #RRGGBB or R,G,B"),
    ("soldermask", "thickness", "mask_thickness", "mm", None, "default",
     "over copper, mm (default: fab data, else 0.020)"),

    ("silkscreen", "enabled", "silk", "bool", True, True, "build the silkscreen"),
    ("silkscreen", "color", "silk_color", "color", "white", "white", "silkscreen colour"),
    ("silkscreen", "thickness", "silk_thickness", "mm", None, "default", "mm (default: fab data, else 0.020)"),
    ("silkscreen", "clip_to_mask", "clip_silk", "bool", True, True, "remove silkscreen over mask openings"),
    ("silkscreen", "simplify", "silk_simplify", "mm", 0.002, 0.002,
     "leave out detail smaller than this from the silkscreen outline (text, logos are drawn with points a few "
     "um apart): a STEP several times smaller; 0 keeps every point"),
    ("silkscreen", "body", "silk_body", ("solid", "surface"), "solid", "solid",
     "solid: a body as thick as the silkscreen; surface: only its top face (about 4x smaller STEP)"),

    ("finish", "enabled", "finish", "bool", True, True, "build the surface finish"),
    ("finish", "type", "finish_type", "str", None, "default",
     "ENIG, ENEPIG, HASL, ImAg, ImSn, OSP, ... (sets the colour; default: fab data, else ENIG)"),
    ("finish", "color", "finish_color", "color_auto", "auto", "auto", "auto: from the finish type"),
    ("finish", "thickness", "finish_thickness", "mm", None, "default",
     "mm (default: fab data; Altium's ODB++ and IPC-2581 leave it out)"),
    ("finish", "body", "finish_body", ("auto", "solid", "color"), "auto", "auto",
     "solid: a body as thick as the finish on the copper; color: the copper under the finish takes its "
     "colour (no added thickness); auto: solid when there is a thickness, else color"),
    ("finish", "area", "finish_area", ("exposed", "all"), "exposed", "exposed",
     "where the finish goes when the fab data has no finish artwork: exposed (copper not under the solder "
     "mask) or all (all outer copper)"),
    ("finish", "layers", "finish_layers", "list", [], [],
     "fab layers whose features mark where the finish goes (instead of finish.area), names or wildcards; "
     "a layer of one side applies to that side, others to both"),

    ("paste", "enabled", "paste", "bool", False, False, "build solder paste deposits"),
    ("paste", "color", "paste_color", "color", "#a6a6a6", "#a6a6a6", "solder paste colour"),
    ("paste", "thickness", "paste_thickness", "mm", None, "default", "mm (default: fab data, else 0.120)"),

    ("plating", "thickness", "plating_thickness", "mm", None, "default",
     "hole wall plating, mm (default: the drill tool sizes of the fab data, else 0.025)"),
    ("plating", "via_thickness", "via_plating_thickness", "mm", None, "default",
     "via wall plating, mm (default: as thickness)"),
    ("plating", "color", "plating_color", "color", "copper", "copper", "hole wall colour"),

    ("vias", "fill", "via_fill", ("none", "epoxy", "copper", "silver"), None, "default",
     "fill every via: none, epoxy, copper or silver (default: fab data)"),
    ("vias", "cap", "via_cap", "bool", None, "default",
     "cap every via with plating, IPC-4761 type VII; implies fill (default: fab data)"),
    ("vias", "tent", "via_tent", ("none", "top", "bottom", "both"), None, "default",
     "solder mask over every via: none, top, bottom or both (default: fab data)"),
    ("vias", "plug", "via_plug", ("none", "top", "bottom", "both"), None, "default",
     "plug every via: none, top, bottom or both (default: fab data)"),
    ("vias", "max_diameter", "via_max_diameter", "mm", None, "default",
     "the four settings above only for vias up to this finished diameter, mm (default: all vias)"),
    ("vias", "rules", "via_protection", "list", [], [],
     "IPC-4761 rules, e.g. [\"7:max=0.3\", \"1b:layer=drill*\"] (see --help)"),
    ("vias", "fill_material", "fill_material", ("epoxy", "copper", "silver"), "epoxy", "epoxy",
     "fill material where the fab data or a rule fills without naming one"),
    ("vias", "plug_depth", "plug_depth", "float", 0.3, 0.3, "plug depth as a fraction of the hole length"),
    ("vias", "cap_thickness", "cap_thickness", "mm", 0.015, 0.015, "via cap plating, mm"),
    ("vias", "drill_through_mask", "drill_through_mask", "bool", False, False,
     "vias cut the solder mask even where the mask layer covers them"),
    ("vias", "fill_color", "fill_color", "color_auto", "auto", "auto", "auto: by fill material"),
    ("vias", "cap_color", "cap_color", "color", "#d8894a", "#d8894a", "via cap colour"),

    ("counterholes", "depth_mode", "counterhole_depth_mode", ("expose", "through"), "expose", "expose",
     "counterbores without a depth in the data end on the far layer of their span: expose leaves "
     "that copper layer, through cuts it"),
    ("counterholes", "counterbore_rules", "counterbore_rules", "list", [], [],
     "add counterbores by hole size: [\"HOLE:DIA:DEPTH[:top|bottom|both]\", ...]"),
    ("counterholes", "countersink_rules", "countersink_rules", "list", [], [],
     "add countersinks by hole size: [\"HOLE:DIA:ANGLE[:top|bottom|both]\", ...]"),

    ("backdrill", "stub", "backdrill_stub", "mm", 0.0, 0.0,
     "dielectric left above the must-not-cut layer, mm (0: Altium's drill depth, up to the layer); "
     "stub data in the fab data takes precedence"),

    ("geometry", "rout", "rout", ("auto", "cut", "ignore"), "auto", "auto",
     "rout layers: auto cuts internal rout paths only, cut all, or ignore"),
    ("geometry", "arc_tolerance", "arc_tolerance", "float", 0.001, 0.001, "tessellation chord error, mm"),
    ("geometry", "simplify", "simplify", "mm", 0.0, 0.0,
     "leave out detail smaller than this from every layer's outline (0: off); smaller STEP files, outlines "
     "move by up to this much"),
    ("geometry", "arc_fit", "arc_fit", ("loose", "strict", "off"), "loose", "loose",
     "rebuild true arcs and circles in the STEP"),
    ("geometry", "max_holes_per_face", "max_holes_per_face", "int", 1000, 1000,
     "split larger regions into tiles (0 = never); export time grows with the square of this"),
]

COUNTER_FIELDS = ("type", "angle", "depth")
TYPE_WORDS = {"bore": "counterbore", "cbore": "counterbore", "counterbore": "counterbore",
              "sink": "countersink", "csink": "countersink", "csk": "countersink", "countersink": "countersink"}


def _norm_type(v) -> str:
    t = TYPE_WORDS.get(str(v).strip().lower())
    if t is None:
        raise ConfigError(f"counterhole type '{v}': use bore (counterbore) or sink (countersink)")
    return t


def _counter_value(key: str, v):
    if key == "type":
        return _norm_type(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"counterhole {key} '{v}': a number is expected")
    if key == "angle" and not 0.0 < f < 180.0:
        raise ConfigError(f"counterhole angle {f:g}: 0 < angle < 180 degrees")
    if key == "depth" and f <= 0.0:
        raise ConfigError(f"counterhole depth {f:g}: must be > 0 mm")
    return f


def parse_counterhole_spec(spec: str) -> Dict[str, object]:
    """'type=sink,angle=82' / 'sink angle=82' / 'bore depth=1.0' -> {field: value}."""
    out: Dict[str, object] = {}
    for tok in [t for t in re.split(r"[,\s;]+", spec or "") if t]:
        if "=" in tok:
            k, v = [x.strip() for x in tok.split("=", 1)]
            k = k.lower()
            if k not in COUNTER_FIELDS:
                raise ConfigError(f"counterhole option '{k}': use type=, angle= or depth=")
            out[k] = _counter_value(k, v)
        elif tok.lower() in TYPE_WORDS:
            out["type"] = _norm_type(tok)
        else:
            raise ConfigError(f"counterhole option '{tok}': use type=bore|sink, angle=DEG, depth=MM")
    return out


def is_counterhole_token(tok: str) -> bool:
    t = tok.strip().lower()
    if t in TYPE_WORDS:
        return True
    return bool(re.match(r"^(type|angle|depth)=", t)) or all(
        re.match(r"^((type|angle|depth)=[^,;]+|bore|sink|cbore|csink|csk|counterbore|countersink)$", p)
        for p in re.split(r"[,;]+", t) if p) and ("," in t or ";" in t)


# --------------------------------------------------------------------------
# Config files
# --------------------------------------------------------------------------


def load_config(path: str) -> dict:
    if tomllib is None:                  # pragma: no cover
        raise ConfigError("reading config.toml needs Python 3.11 or newer")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}")
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}")


def _is_default(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in ("default", "auto-default", ""))


UNITS = {"mm": 1.0, "um": 0.001, "µm": 0.001, "mil": 0.0254, "mils": 0.0254, "thou": 0.0254, "in": 25.4,
         "inch": 25.4, "inches": 25.4, '"': 25.4, "cm": 10.0}


def parse_length(v, where: str = "") -> float:
    """'0.1', '0.1mm', '4mil', '-2 mil', '0.004in', '100um' -> mm."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.fullmatch(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)\s*([a-zµ\"]*)\s*", str(v), re.I)
    if not m or m.group(2).lower() not in UNITS and m.group(2) != "":
        raise ConfigError(f"{where + ': ' if where else ''}'{v}' is not a length (mm, um, mil, in)")
    return float(m.group(1)) * UNITS.get(m.group(2).lower() or "mm", 1.0)


def _convert(kind, v, where: str):
    """Config / command line value -> Python value of the setting."""
    try:
        if kind == "mm":
            f = parse_length(v, where)
            if f < 0:
                raise ConfigError(f"{where}: {v} must be >= 0")
            return f
        if kind == "length":
            return parse_length(v, where)
        if kind == "float":
            return float(v)
        if kind == "int":
            return int(v)
        if kind == "bool":
            if isinstance(v, bool):
                return v
            t = str(v).strip().lower()
            if t in ("true", "yes", "on", "1"):
                return True
            if t in ("false", "no", "off", "0"):
                return False
            raise ConfigError(f"{where}: '{v}' is not true or false")
        if kind == "str":
            return str(v)
        if kind == "color":
            return parse_color(v)
        if kind == "color_auto":
            return "auto" if str(v).strip().lower() == "auto" else parse_color(v)
        if kind == "list":
            if isinstance(v, str):
                return [v]
            return [str(x) for x in v]
        if isinstance(kind, tuple):
            t = str(v).strip()
            for c in kind:
                if t.lower() == c.lower():
                    return c
            raise ConfigError(f"{where}: '{v}' is not one of {', '.join(kind)}")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"{where}: bad value '{v}'")
    raise ConfigError(f"{where}: unknown setting kind {kind}")


def check_config(cfg: dict, label: str) -> List[str]:
    """Unknown sections and keys (typos) -> warnings."""
    known = {}
    for sec, key, *_ in SCHEMA:
        known.setdefault(sec, set()).add(key)
    known.setdefault("stackup", set()).add("layers")
    known.setdefault("counterholes", set()).update(COUNTER_FIELDS + ("top", "bottom"))
    out = []
    for sec, val in cfg.items():
        if sec not in known:
            out.append(f"{label}: unknown section [{sec}]")
            continue
        if not isinstance(val, dict):
            out.append(f"{label}: [{sec}] should be a table")
            continue
        for k in val:
            if k not in known[sec]:
                out.append(f"{label}: unknown key '{k}' in [{sec}]")
    return out


# --------------------------------------------------------------------------
# Merge: command line > config > (fab data, in the builder) > built-in defaults
# --------------------------------------------------------------------------


def resolve(cli, cfg: Optional[dict], cfg_label: str = "config.toml") -> SimpleNamespace:
    """Options for the builder. 'cli' is the argparse namespace (None = not
    given), 'cfg' the parsed config file (or None). Every value records its
    origin in opts.sources for the report."""
    cfg = cfg or {}
    o = SimpleNamespace()
    sources: Dict[str, str] = {}
    labels = getattr(cli, "_labels", {}) or {}
    for sec, key, attr, kind, builtin, _tmpl, _c in SCHEMA:
        v = getattr(cli, attr, None)
        flag = labels.get(attr, "--" + attr.replace("_", "-"))
        if v is not None and not (kind == "list" and v == []):
            val = _convert(kind, v, flag) if kind != "list" else list(v)
            sources[attr] = flag
        else:
            cv = cfg.get(sec, {}).get(key) if isinstance(cfg.get(sec), dict) else None
            if not _is_default(cv) and not (kind == "list" and cv == []):
                val = _convert(kind, cv, f"{cfg_label} [{sec}] {key}")
                sources[attr] = f"{cfg_label} [{sec}] {key}"
            else:
                val = builtin
                if kind in ("color",) and builtin is not None:
                    val = parse_color(builtin)
        setattr(o, attr, val)
    # list settings add up: config rules first, then command line rules
    for attr, sec, key in (("via_protection", "vias", "rules"),
                           ("counterbore_rules", "counterholes", "counterbore_rules"),
                           ("countersink_rules", "counterholes", "countersink_rules"),
                           ("hide_bodies", "model", "hide"), ("mesh_bodies", "model", "mesh")):
        cv = cfg.get(sec, {}).get(key) if isinstance(cfg.get(sec), dict) else None
        lst = _convert("list", cv, key) if cv else []
        lst += list(getattr(cli, attr, None) or [])
        setattr(o, attr, lst)
    # booleans stored inverted for the builder
    o.no_mask, o.no_silk, o.no_finish, o.no_clip_silk = not o.mask, not o.silk, not o.finish, not o.clip_silk
    # per-layer thickness: config table, then command line LAYER=MM
    lt: Dict[str, float] = {}
    tab = cfg.get("stackup", {}).get("layers", {}) if isinstance(cfg.get("stackup"), dict) else {}
    for k, v in (tab or {}).items():
        if not _is_default(v):
            lt[str(k)] = _convert("mm", v, f"{cfg_label} [stackup.layers] {k}")
    if lt:
        sources["layer_thickness"] = f"{cfg_label} [stackup.layers]"
    cli_lt = {}
    for item in getattr(cli, "layer_thickness", None) or []:
        if "=" not in item:
            raise ConfigError(f"--thickness expects LAYER=SIZE, got '{item}'")
        k, v = item.split("=", 1)
        cli_lt[k.strip()] = _convert("mm", v, f"--thickness {k.strip()}")
    if cli_lt:
        lt.update(cli_lt)
        sources["layer_thickness"] = "--thickness" if not tab else f"--thickness / {cfg_label}"
    o.layer_thickness = lt
    # counterholes per side
    o.counterhole = resolve_counterholes(cli, cfg, cfg_label)
    o.sources = sources
    return o


def resolve_counterholes(cli, cfg: dict, cfg_label: str) -> Dict[str, Dict[str, Optional[tuple]]]:
    """{side: {field: (value, origin) or None}}; command line side > command
    line both > config side > config both."""
    sec = cfg.get("counterholes", {}) if isinstance(cfg.get("counterholes"), dict) else {}
    cli_both = _merge_specs(getattr(cli, "counterhole", None) or [])
    cli_side = {"top": _merge_specs(getattr(cli, "counterhole_top", None) or []),
                "bottom": _merge_specs(getattr(cli, "counterhole_bottom", None) or [])}
    cfg_both = {k: _counter_value(k, v) for k, v in sec.items() if k in COUNTER_FIELDS and not _is_default(v)}
    cfg_side = {}
    for side in ("top", "bottom"):
        t = sec.get(side, {}) if isinstance(sec.get(side), dict) else {}
        cfg_side[side] = {k: _counter_value(k, v) for k, v in t.items() if k in COUNTER_FIELDS and not _is_default(v)}
    out = {}
    for side in ("top", "bottom"):
        res = {}
        for f in COUNTER_FIELDS:
            for vals, label in ((cli_side[side], f"--counterhole-{side}"), (cli_both, "--counterhole"),
                                (cfg_side[side], f"{cfg_label} [counterholes.{side}]"),
                                (cfg_both, f"{cfg_label} [counterholes]")):
                if f in vals:
                    res[f] = (vals[f], label)
                    break
            else:
                res[f] = None
        out[side] = res
    return out


def _merge_specs(specs: List[str]) -> Dict[str, object]:
    out: Dict[str, object] = {}
    for s in specs:
        out.update(parse_counterhole_spec(s))
    return out


# --------------------------------------------------------------------------
# init: fab folder with config.toml
# --------------------------------------------------------------------------


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_template(version: str) -> str:
    """config.toml populated with the defaults, every key commented."""
    lines = [
        f"# PCBFab2Step fab folder configuration (written by pcbfab2step {version})",
        "#",
        "# Precedence: command line > this file > fab data (ODB++, IPC-2581, NC drill) > built-in defaults.",
        '# "default" keeps the fab data where it has the value, else the built-in default given in the',
        "# comment. Any other value overrides the fab data. Sizes are mm unless a unit is given",
        '# ("4mil", "0.004in", "100um"). On the command line: --set SECTION.KEY=VALUE.',
        "#",
        "# The fab outputs may sit anywhere below this folder (Altium's ODB++/, IPC-2581/, NC Drill/,",
        "# STEP/ or flat): ODB++ or IPC-2581 gives the board, NC drill reports the counterhole depths,",
        "# the ECAD's STEP export its components. Results go to PCBFab2Step/ in this folder.",
    ]
    sections: List[str] = []
    for sec, *_ in SCHEMA:
        if sec not in sections:
            sections.append(sec)
    for sec in sections:
        lines += ["", f"[{sec}]"]
        if sec == "counterholes":
            lines += [
                "# For counterholes the fab data does not describe fully: ODB++ has no type, angle or depth;",
                "# IPC-2581 padstacks give the type and angle, NC drill reports give the depths.",
                _kv("type", "default", "bore (counterbore) or sink (countersink), both sides "
                                       "(default: fab data, else bore)"),
                _kv("angle", "default", "countersink included angle, degrees (default: fab data, else 90)"),
                _kv("depth", "default", "counterbore depth from the outer copper, mm "
                                        "(default: fab data, else depth_mode)"),
            ]
        for s2, key, attr, kind, builtin, tmpl, comment in SCHEMA:
            if s2 == sec:
                lines.append(_kv(key, tmpl, comment))
        if sec == "stackup":
            lines += ["", "[stackup.layers]".ljust(34) + "# thickness of single layers by layer name, mm",
                      '# "L2_GND" = 0.018']
        if sec == "counterholes":
            for side in ("top", "bottom"):
                lines += ["", f"[counterholes.{side}]".ljust(34) +
                          f"# the same keys for the {side} side only; they win over [counterholes]"]
                lines += [_kv(k, "default", "") for k in COUNTER_FIELDS]
    return "\n".join(lines) + "\n"


def _kv(key: str, val, comment: str) -> str:
    s = f"{key} = {_toml_value(val)}"
    return (s.ljust(34) + "# " + comment) if comment else s


def init_fab_folder(folder: str, version: str, force: bool = False) -> List[str]:
    """Create a fab folder: ODB++/, IPC2581/, GERBER/, NCDRILL/, STEP/ and config.toml."""
    done = []
    os.makedirs(folder, exist_ok=True)
    for d in FAB_SUBDIRS:
        p = os.path.join(folder, d)
        if not os.path.isdir(p):
            os.makedirs(p)
            done.append(f"created {p}{os.sep}")
    cfg = os.path.join(folder, CONFIG_NAME)
    if os.path.exists(cfg) and not force:
        done.append(f"kept {cfg} (exists; --force to overwrite)")
    else:
        with open(cfg, "w", encoding="utf-8", newline="\n") as f:
            f.write(config_template(version))
        done.append(f"wrote {cfg}")
    return done
