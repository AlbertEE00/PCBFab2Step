# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Command line. One call writes one output:

    pcbfab2step [INPUT ...] [options]                        the board as a STEP model
    pcbfab2step [INPUT ...] --export TARGET:FORMAT [options]  one layer as DXF, SVG or STEP
    pcbfab2step info [INPUT ...]                              what was found, stackup, holes, targets
    pcbfab2step init [FOLDER]                                 config.toml with every setting
    pcbfab2step settings                                      every setting and its default

The options act on that output. Settings merge as command line > config.toml
> fab data > built-in defaults; every config.toml setting can also be given
with --set SECTION.KEY=VALUE.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import time
from types import SimpleNamespace
from typing import List, Optional

from . import __version__
from .board import parse_via_rule
from .common import Log, info, warn
from .config import (NAMED_COLORS, SCHEMA, ConfigError, check_config, config_template, init_fab_folder,
                     is_counterhole_token, load_config, resolve)

COUNTERHOLE_FLAGS = ("--counterhole", "--counterhole-top", "--counterhole-bottom", "--counterhole-bot")

USAGE = """
  pcbfab2step [INPUT ...] [options]                         the board as a STEP model
  pcbfab2step [INPUT ...] --export TARGET:FORMAT [options]  one layer as DXF, SVG or STEP
  pcbfab2step info [INPUT ...]                              what was found, stackup, holes, targets
  pcbfab2step init [FOLDER]                                 config.toml with every setting
  pcbfab2step settings                                      every setting and its default"""

DESCRIPTION = """\
PCB fab data (ODB++, IPC-2581, NC drill, the ECAD's STEP) to a coloured STEP
model, or one layer of it as DXF, SVG or STEP. One call writes one output, to
PCBFab2Step/ in the folder given (or in the folder of the file given)."""

EPILOG = """examples:
  pcbfab2step FAB                                       FAB/PCBFab2Step/<board>.step
  pcbfab2step FAB --sanitize                            no inner layers, only exposed copper
  pcbfab2step FAB --show paste --color mask=black
  pcbfab2step FAB --export stencil-bot:step --thickness stencil=4mil --aperture-offset -1mil
  pcbfab2step FAB --export paste-bot:dxf --no-outline
  pcbfab2step FAB --export solder-bot:svg --invert      the mask openings (as in Gerber)
  pcbfab2step FAB --export outline:step                 the plain PCB body
  pcbfab2step board.cvg parts.step                      IPC-2581 board with the parts of a STEP
  pcbfab2step FAB --sources ipc+drill+step              IPC-2581 as the board, the ODB++ not read

inputs:
  A folder is searched with its subfolders; a file also brings the other outputs in its folder,
  and in the parent folder when its folder is an output folder (ODB++, IPC-2581, NC Drill,
  Gerber, STEP). Found: ODB++ (job folder or archive), IPC-2581 (.cvg/.xml), NC drill output
  (.DRR report with its drill files), the ECAD's STEP export (its components are added, the
  ECAD's simplified board left out), config.toml. ODB++ gives the board before IPC-2581; the
  other sources add what it lacks (IPC-2581: counterhole types; NC drill: counterhole depths;
  ODB++ next to IPC-2581: via types). Gerber is found but not read yet.

export targets (--export TARGET:FORMAT, FORMAT dxf, svg or step; several: dxf,svg):
  A layer is exported as it is on the board (as in the model), not as the artwork.
  outline                      DXF/SVG: the board outline; STEP: the plain PCB body
  paste-top   paste-bot        solder paste deposits
  stencil-top stencil-bot      the stencil: outline minus the paste openings; STEP: a plate,
                               --thickness stencil=SIZE (0.1 mm), on the board surface
  solder-top  solder-bot       the solder mask: outline minus its openings, tented vias closed
  silk-top    silk-bot         silkscreen, clipped to the solder mask
  copper-top  copper-bot  copper-N   copper layer (holes cut), N counted from the top
  or a layer's name in the fab data ('pcbfab2step info' lists them). Without -top/-bot the
  side with data is taken. --invert: the rest of the outline instead (mask openings, the paste
  of a stencil, the clearances in the copper). DXF and SVG include the outline (--no-outline
  to leave it out); a STEP layer is the layer alone (--outline adds the PCB body). --vias,
  --counterhole and --sanitize shape the exported layer as they shape the model.

sizes: mm unless a unit is given: 0.1, 0.1mm, 100um, 4mil, 0.004in

via rules (--vias, repeatable; over the fab data):
  TYPE[:key=value...]  IPC-4761 type 1a 1b 2a 2b 3a 3b 4a 4b 5 6a 6b 7 (VII, VI-b, ... accepted)
                       or none (bare vias); keys min=SIZE max=SIZE (finished hole), layer=GLOB
                       (drill layer), kind=via|pth|any, side=top|bottom ('a' types: top)
  undrilled | drilled  leave the vias out (their pads stay) or drill them (with --sanitize)
  e.g. --vias 7:max=0.3   fill and cap every via up to 0.3 mm

counterholes (ODB++ has no type, angle or depth for them):
  --counterhole type=sink angle=82          both sides
  --counterhole-top type=bore depth=0.8     top side only (wins over --counterhole)
  Without these: NC drill and IPC-2581 data, else counterbore, 90 degrees, depth from the layer
  span. By hole size: --set 'counterholes.counterbore_rules=["3.2:6.0:1.5:bottom"]'

precedence: command line > config.toml > fab data > built-in defaults.
Options can be kept in a text file, one per line, and given as @file.
"""

THICKNESS_KEYS = {"board": "board_thickness", "copper": "copper_thickness", "mask": "mask_thickness",
                  "soldermask": "mask_thickness", "solder": "mask_thickness", "silk": "silk_thickness",
                  "silkscreen": "silk_thickness", "finish": "finish_thickness", "paste": "paste_thickness",
                  "plating": "plating_thickness", "viaplating": "via_plating_thickness",
                  "cap": "cap_thickness", "stencil": "stencil_thickness"}
COLOR_KEYS = {"board": "board_color", "laminate": "board_color", "dielectric": "board_color",
              "copper": "copper_color", "mask": "mask_color", "soldermask": "mask_color", "solder": "mask_color",
              "silk": "silk_color", "silkscreen": "silk_color", "finish": "finish_color",
              "plating": "plating_color", "fill": "fill_color", "cap": "cap_color", "paste": "paste_color"}
SHOW_KEYS = {"mask": "mask", "soldermask": "mask", "silk": "silk", "silkscreen": "silk", "finish": "finish",
             "paste": "paste", "components": "components", "parts": "components"}


def _key(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", s.strip().lower())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pcbfab2step", usage=USAGE, description=DESCRIPTION, epilog=EPILOG,
                                fromfile_prefix_chars="@", formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="*", metavar="INPUT",
                   help="folder with the fab outputs, or files (default: this folder)")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="output file (default: PCBFab2Step/<board>.step, or <board>_<target>.<format> with "
                        "--export, in the folder given)")
    p.add_argument("--version", action="version", version=f"pcbfab2step {__version__}")

    s = p.add_argument_group("sources")
    s.add_argument("--sources", metavar="LIST",
                   help="use only these of what was found, the first geometry source as the board: odb, ipc, "
                        "drill, step, e.g. ipc+drill+step (default: all, ODB++ before IPC-2581)")
    s.add_argument("--config", metavar="FILE", help="settings file (default: config.toml in the folder given)")
    s.add_argument("--no-config", action="store_true", help="ignore config.toml")

    m = p.add_argument_group("board model (the default output)")
    m.add_argument("--sanitize", nargs="?", const="exposed", choices=["outer", "exposed", "pads"],
                   metavar="LEVEL",
                   help="hide design detail: outer (inner layers merged into one core, vias not drilled), "
                        "exposed (also outer copper cut to pads and what the solder mask leaves open; the "
                        "default), pads (outer copper: pads only)")
    m.add_argument("--vias", action="append", default=[], metavar="RULE",
                   help="via protection over the fab data, or undrilled / drilled (repeatable; see below)")
    m.add_argument("--show", action="append", default=[], metavar="LIST",
                   help="build these too: paste (off by default), or mask, silk, finish, components")
    m.add_argument("--hide", action="append", default=[], metavar="LIST",
                   help="leave out: mask, silk, finish, paste, components")
    m.add_argument("--counterhole", action="append", default=[], metavar="SPEC",
                   help="both sides: type=bore|sink angle=DEG depth=SIZE (see below)")
    m.add_argument("--counterhole-top", action="append", default=[], metavar="SPEC", help="top side")
    m.add_argument("--counterhole-bottom", "--counterhole-bot", dest="counterhole_bottom", action="append",
                   default=[], metavar="SPEC", help="bottom side")
    # odb2step 1.1.0 spellings
    m.add_argument("--counterhole-type", choices=["counterbore", "countersink"], default=None,
                   help=argparse.SUPPRESS)
    m.add_argument("--countersink-angle", type=float, default=None, help=argparse.SUPPRESS)
    m.add_argument("--counterhole-depth", type=float, default=None, help=argparse.SUPPRESS)

    x = p.add_argument_group("layer export (instead of the board model)")
    x.add_argument("--export", metavar="TARGET:FORMAT",
                   help="write one layer: TARGET as listed below, FORMAT dxf, svg or step")
    x.add_argument("--outline", action=argparse.BooleanOptionalAction, default=None,
                   help="with the board outline: DXF and SVG have it unless --no-outline; STEP only with "
                        "--outline (the PCB body)")
    x.add_argument("--invert", action="store_true",
                   help="the rest of the outline instead of the layer: the solder mask openings, the paste of "
                        "a stencil, the clearances in the copper")
    x.add_argument("--aperture-offset", metavar="SIZE",
                   help="grow (+) or shrink (-) the artwork before the layer is made: stencil and paste "
                        "apertures, mask openings, features; e.g. -0.05mm, -2mil")

    a = p.add_argument_group("sizes and colours (board model and exports)")
    a.add_argument("--thickness", action="append", default=[], metavar="ITEM=SIZE",
                   help="board (copper to copper; the dielectrics scale), copper, mask, silk, finish, paste, "
                        "plating, via-plating, cap, stencil, or a layer name; e.g. mask=0.02,stencil=4mil")
    a.add_argument("--color", "--colour", action="append", default=[], metavar="ITEM=COLOR",
                   help="board, copper, mask, silk, finish, plating, fill, cap, paste = a name (" +
                        ", ".join(sorted(NAMED_COLORS)) + "), #RRGGBB or R,G,B")
    a.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="any config.toml setting, e.g. --set finish.type=ENIG ('pcbfab2step settings' "
                        "lists them)")

    g = p.add_argument_group("messages")
    g.add_argument("-v", "--verbose", action="count", default=1, help="more detail")
    g.add_argument("-q", "--quiet", action="store_true", help="errors and warnings only")
    return p


SIGNED = ("--aperture-offset",)


def join_counterhole_args(argv: List[str]) -> List[str]:
    """'--counterhole-top type=sink angle=82' -> '--counterhole-top type=sink,angle=82',
    so the option takes several words without swallowing the inputs; and
    '--aperture-offset -1mil' -> '--aperture-offset=-1mil' (argparse would take
    the negative size for an option)."""
    out: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in SIGNED and i + 1 < len(argv) and re.match(r"-[\d.]", argv[i + 1]):
            out.append(f"{a}={argv[i + 1]}")
            i += 2
            continue
        out.append(a)
        if a in COUNTERHOLE_FLAGS:
            vals = []
            j = i + 1
            while j < len(argv) and is_counterhole_token(argv[j]):
                vals.append(argv[j])
                j += 1
            if vals:
                out.append(",".join(vals))
            i = j
            continue
        i += 1
    return out


# --------------------------------------------------------------------------
# command line -> setting values (the names of config.SCHEMA)
# --------------------------------------------------------------------------


def _items(values: List[str]) -> List[str]:
    return [t.strip() for v in values for t in v.split(",") if t.strip()]


def _parse_set_value(v: str):
    try:
        import tomllib
    except ImportError:                                   # Python 3.10
        import tomli as tomllib                            # type: ignore
    try:
        return tomllib.loads(f"v = {v}")["v"]
    except Exception:
        return v


def settings_from_args(args) -> SimpleNamespace:
    """The option values the merge (config.resolve) reads, from the command line."""
    c = SimpleNamespace(layer_thickness=[], via_protection=[], counterhole=list(args.counterhole),
                        counterhole_top=list(args.counterhole_top),
                        counterhole_bottom=list(args.counterhole_bottom), counterbore_rules=[],
                        countersink_rules=[], vias_drilled=None, _labels={})
    lab = c._labels
    by_key = {f"{sec}.{key}": (attr, kind) for sec, key, attr, kind, *_ in SCHEMA}
    for item in args.set:
        if "=" not in item:
            raise SystemExit(f"--set expects SECTION.KEY=VALUE, got '{item}'")
        k, v = (t.strip() for t in item.split("=", 1))
        kl = k.lower()
        if kl.startswith("stackup.layers."):
            c.layer_thickness.append(f"{k.split('.', 2)[2]}={v}")
            continue
        m = re.fullmatch(r"counterholes(?:\.(top|bottom))?\.(type|angle|depth)", kl)
        if m:
            getattr(c, "counterhole" + (f"_{m.group(1)}" if m.group(1) else "")).append(f"{m.group(2)}={v}")
            continue
        if kl not in by_key:
            near = difflib.get_close_matches(kl, list(by_key), n=3)
            raise SystemExit(f"--set {k}: no such setting" + (f" (did you mean {', '.join(near)}?)" if near else
                                                                "; 'pcbfab2step settings' lists them"))
        attr, kind = by_key[kl]
        val = _parse_set_value(v)
        if kind == "list":
            getattr(c, attr).extend(val if isinstance(val, list) else [val])
        else:
            setattr(c, attr, val)
        lab[attr] = f"--set {k}"
    if args.sources:
        c.use_sources, lab["use_sources"] = args.sources, "--sources"
    if args.sanitize:
        c.sanitize, lab["sanitize"] = args.sanitize, "--sanitize"
    for item in _items(args.thickness):
        if "=" not in item:
            raise SystemExit(f"--thickness expects ITEM=SIZE, got '{item}'")
        k, v = (t.strip() for t in item.split("=", 1))
        attr = THICKNESS_KEYS.get(_key(k))
        if attr:
            setattr(c, attr, v)
            lab[attr] = f"--thickness {k}"
        else:
            c.layer_thickness.append(f"{k}={v}")
    for item in _items(args.color):
        if "=" not in item:
            raise SystemExit(f"--color expects ITEM=COLOR, got '{item}'")
        k, v = (t.strip() for t in item.split("=", 1))
        attr = COLOR_KEYS.get(_key(k))
        if not attr:
            raise SystemExit(f"--color {k}: no such item (board, copper, mask, silk, finish, plating, fill, cap, "
                             "paste)")
        setattr(c, attr, v)
        lab[attr] = f"--color {k}"
    shown = {}
    for flag, vals, on in (("--show", args.show, True), ("--hide", args.hide, False)):
        for item in _items(vals):
            attr = SHOW_KEYS.get(_key(item))
            if not attr:
                raise SystemExit(f"{flag} {item}: no such item (mask, silk, finish, paste, components)")
            if attr in shown and shown[attr] != on:
                raise SystemExit(f"{item} is both shown and hidden")
            shown[attr] = on
            setattr(c, attr, on)
            lab[attr] = f"{flag} {item}"
    for item in args.vias:
        t = item.strip().lower()
        if t in ("undrilled", "nodrill", "no-drill", "off"):
            c.vias_drilled = False
        elif t in ("drilled", "drill", "on"):
            c.vias_drilled = True
        else:
            c.via_protection.append(item)
    if args.aperture_offset is not None:
        c.aperture_offset, lab["aperture_offset"] = args.aperture_offset, "--aperture-offset"
    if args.output:
        c.output, lab["output"] = args.output, "-o"
    # odb2step 1.1.0 spellings
    bits = [f"type={args.counterhole_type}"] if args.counterhole_type else []
    if args.countersink_angle:
        bits.append(f"angle={args.countersink_angle}")
    if args.counterhole_depth:
        bits.append(f"depth={args.counterhole_depth}")
    if bits:
        c.counterhole.insert(0, ",".join(bits))
    return c


def _check_scope(args):
    """One call, one output: flags of the other output are mistakes."""
    model_only = [f for f, v in (("--show", args.show), ("--hide", args.hide)) if v]
    if args.export and model_only:
        raise SystemExit(f"{', '.join(model_only)}: acts on the board model, not on --export")
    export_only = [f for f, v in (("--outline/--no-outline", args.outline is not None),
                                  ("--invert", args.invert),
                                  ("--aperture-offset", args.aperture_offset is not None)) if v]
    if not args.export and export_only:
        raise SystemExit(f"{', '.join(export_only)}: only with --export")


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", s).strip()


def run(args, mode: str) -> int:
    """mode: model, export or info."""
    from . import exports, ipc2581, ncdrill, odbpp
    from .brep import ArcFit, Brep, write_step
    from .builder import Model, parse_size_rule, write_svgs
    from .geometry import Tess
    from .sources import LABEL, choose, discover

    Log.reset(0 if args.quiet else args.verbose)
    t0 = time.time()
    if mode != "info":
        _check_scope(args)
    srcs = discover(args.inputs, args.config, args.no_config)
    cfg, label = None, "config.toml"
    if srcs.config:
        cfg = load_config(srcs.config)
        label = os.path.basename(srcs.config)
        for w in check_config(cfg, label):
            warn(w)
    cli = settings_from_args(args)
    try:
        opts = resolve(cli, cfg, label)
        opts.via_rules = [parse_via_rule(s) for s in opts.via_protection]
    except (ConfigError, ValueError) as exc:
        raise SystemExit(str(exc))
    for spec in opts.counterbore_rules:
        parse_size_rule(spec, "counterbore")
    for spec in opts.countersink_rules:
        parse_size_rule(spec, "countersink")
    opts.list = mode == "info"
    opts.external_only = opts.sanitize != "none"
    opts.outer_copper = {"exposed": "exposed", "pads": "pads"}.get(opts.sanitize, "all")
    opts.vias = cli.vias_drilled
    opts.export_outline = args.outline
    opts.export_invert = args.invert
    Tess.tol = max(1e-5, opts.arc_tolerance)
    ArcFit.enabled = opts.arc_fit != "off"
    ArcFit.strict = opts.arc_fit == "strict"
    Brep.max_holes = opts.max_holes_per_face

    choose(srcs, opts.use_sources)
    info(f"Input    : {LABEL[srcs.primary]} {srcs.primary_path}")
    for k, path in (("ipc", srcs.ipc_hints), ("odb", srcs.odb_types), ("step", srcs.step)):
        if path:
            info(f"           {LABEL[k]} {path}")
    for d in srcs.drill:
        info(f"           NC drill {d}")
    if srcs.config:
        info(f"Config   : {srcs.config}")
    for nt in srcs.notes:
        info("  " + nt)

    if srcs.primary == "odb":
        reader = odbpp.OdbReader(srcs.primary_path, opts)
    else:
        reader = ipc2581.IpcReader(srcs.primary_path, opts)
    board = reader.read()
    if srcs.ipc_hints:
        hints, notes = ipc2581.read_hints(srcs.ipc_hints)
        board.hints += hints
        board.hint_notes += notes
    if srcs.odb_types:
        board.prot_notes += odbpp.apply_via_types(srcs.odb_types, opts, board)
    if "drill" in srcs.use:
        inside = ncdrill.pool_from_files("inside the ODB++ input", reader.inside_files()) \
            if srcs.primary == "odb" else None
        explicit = [p for p in srcs.explicit["drill"]]
        chosen, notes = ncdrill.find_reports(explicit, inside, [], ncdrill._stem(srcs.primary_path), srcs.drill)
        hints, notes2 = ncdrill.read_hints(chosen)
        board.hints = hints + board.hints          # NC drill first: it has exact counterbore depths
        board.hint_notes += notes + notes2

    model = Model(board, opts)
    model.prepare()
    if mode == "info":
        model.report()
        info("\nExport targets (generic / name in the fab data):")
        for e in exports.targets(model):
            info(f"  {e.generic:<14} {e.fab}" + ("" if e.has_data() else "   (empty)"))
        return 0
    base = _safe_name(board.name)
    os.makedirs(srcs.out_dir, exist_ok=True)

    if mode == "export":
        written = exports.export(model, args.export, opts, srcs.out_dir, base, args.output)
        for p in written:
            info(f"Wrote {p}")
        info(f"Done in {time.time() - t0:.1f} s")
        return 0

    model.report()
    info("\nBuilding solids ...")
    model.build()
    if board.after_build is not None:
        board.after_build()
    if opts.svg_dir:
        write_svgs(opts.svg_dir, model.svg, model.B)
    out = opts.output
    if out and opts.sources.get("output", "").startswith(label) and srcs.config:
        out = os.path.join(os.path.dirname(os.path.abspath(srcs.config)), out)
    if not out:
        suffix = f"_sanitized-{opts.sanitize}" if opts.sanitize != "none" else ""
        out = os.path.join(srcs.out_dir, f"{base}{suffix}.step")
    if srcs.step and opts.components:
        from . import components
        st = model.stk
        t_mask = {side: (0.0 if opts.no_mask else max((L.t for L in st.coats[side].get("mask", [])), default=0.0))
                  for side in ("top", "bottom")}
        info("\nComponents:")
        parts = components.read(srcs.step, model.B.bounds, st.z_top + t_mask["top"], st.z_bot - t_mask["bottom"])
        if parts:
            for c in model.components:
                c["path"] = ("PCB",)
            model.components += parts
    info(f"Writing {out} ...")
    root = f"{board.name} {board.step}".strip()
    write_step(out, root, model.components, opts.schema, fmt=LABEL[srcs.primary])
    nsol = sum(len(c["solids"]) for c in model.components)
    info(f"\nDone in {time.time() - t0:.1f} s: {len(model.components)} bodies, {nsol} solids, "
         f"{ArcFit.stats['circles']} circles and {ArcFit.stats['arcs']} arcs rebuilt exactly, "
         f"{os.path.getsize(out) / 1e6:.1f} MB")
    for c in model.components:
        info(f"  {c['name']}: {len(c['solids'])} solid(s)", 2)
    if Log.warnings:
        info(f"{len(Log.warnings)} warning(s), see above")
    return 0


def init(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="pcbfab2step init",
                                description="Write config.toml with every setting and its default into a folder "
                                            "(and the usual output subfolders, if missing).")
    p.add_argument("folder", nargs="?", default=".", help="folder (default: here)")
    p.add_argument("--force", action="store_true", help="overwrite an existing config.toml")
    a = p.parse_args(argv)
    for line in init_fab_folder(a.folder, __version__, a.force):
        print(line)
    print("Put the fab outputs there (any layout), edit config.toml, then run:  pcbfab2step " + a.folder)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "init":
        return init(argv[1:])
    if argv and argv[0] == "settings":
        sys.stdout.write(config_template(__version__))
        return 0
    mode = "model"
    if argv and argv[0] == "info":
        mode, argv = "info", argv[1:]
    args = build_parser().parse_args(join_counterhole_args(argv))
    if args.export and mode == "model":
        mode = "export"
    try:
        return run(args, mode)
    except ConfigError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    sys.exit(main())
