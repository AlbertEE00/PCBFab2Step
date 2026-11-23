"""PCBFab2Step command line: fab data (ODB++, IPC-2581, NC drill) -> coloured STEP.

    pcbfab2step INPUT... [options]          convert (same as 'pcbfab2step convert ...')
    pcbfab2step info INPUT... [options]     stackup and hole report only
    pcbfab2step init [FOLDER] [--force]     create a fab folder with config.toml

The coordinator finds the inputs, merges the settings (command line > config
file > fab data > defaults), lets the reader of the primary source build the
shared board description, adds what the secondary sources know, and hands the
board to the builder and the STEP writer."""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

from . import __version__
from .board import parse_via_rule
from .common import Log, info, warn
from .config import (NAMED_COLORS, ConfigError, check_config, init_fab_folder, is_counterhole_token, load_config,
                     resolve)

COUNTERHOLE_FLAGS = ("--counterhole", "--counterhole-top", "--counterhole-bottom", "--counterhole-bot")

EPILOG = """inputs:
  INPUT may be a fab folder, an ODB++ product model (.tgz/.tar/.zip or folder), an IPC-2581 file
  (.cvg/.xml) or a config.toml; several may be given (e.g. board.tgz board.cvg). A fab folder
  (see 'pcbfab2step init') holds ODB++/, IPC2581/, NCDRILL/, GERBER/ and config.toml; a plain
  folder is searched for ODB++ and IPC-2581 files. With ODB++ and IPC-2581 both present, ODB++
  gives the geometry and IPC-2581 the counterhole types and angles (--source ipc2581 to use the
  IPC-2581 geometry instead). An IPC-2581 file named like the ODB++ input beside it is used
  automatically (--ipc2581 off to leave it out).

precedence: command line > config.toml > fab data > built-in defaults

counterholes (ODB++ has no type, angle or depth for them):
  --counterhole type=sink angle=82          both sides
  --counterhole-top type=bore depth=0.8     top side only (wins over --counterhole)
  --counterhole-bottom sink                 bare words: bore, sink, counterbore, countersink
  Without these, IPC-2581 padstacks or NC drill data (--ncdrill) give the values, else counterbore,
  90 degrees, and a depth from the counterhole layer span (--counterhole-depth-mode).
  By hole size (repeatable; they replace counterholes of the fab data on the same holes):
  --counterbore 3.2:6.0:1.5:bottom          3.2 mm holes get a 6.0 mm counterbore, 1.5 mm deep
  --countersink 3.4:6.5:90:top              3.4 mm holes get a 6.5 mm, 90 degree countersink

via protection rules (--via-protection, repeatable, applied after the fab data and the --via-* settings):
  TYPE[:key=value...]   TYPE = IPC-4761 type 1a 1b 2a 2b 3a 3b 4a 4b 5 6a 6b 7 or none
                        (roman numerals accepted: VII, VI-b, ...); 'a' types act on the top side
                        unless side=bottom is given
  keys: min=MM max=MM (finished hole diameter), layer=GLOB (drill layer name),
        kind=via|pth|any (default via), side=top|bottom
  e.g.  --via-protection 7:max=0.3     fill and cap every via up to 0.3 mm
        --via-protection 1b            tent all vias on both sides

options can be kept in a text file, one per line, and passed as @file:  pcbfab2step board.tgz @board.args
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pcbfab2step", fromfile_prefix_chars="@", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Convert PCB fab data (ODB++, IPC-2581; NC drill as extra data) to a coloured STEP model.",
        epilog=EPILOG)
    p.add_argument("--version", action="version", version=f"pcbfab2step {__version__}")
    p.add_argument("inputs", nargs="+", metavar="INPUT", help="fab folder, ODB++, IPC-2581 or config.toml")
    p.add_argument("-o", "--output", default=None, help="STEP file (default: <board>.step next to the input)")
    p.add_argument("--list", action="store_true", help="print the stackup and hole report only, no STEP")

    s = p.add_argument_group("sources")
    s.add_argument("--source", "--type", dest="source", choices=["odb", "ipc2581"], default=None,
                   help="primary (geometry) source when both are present (default: odb)")
    s.add_argument("--ipc2581", default=None, metavar="PATH|off",
                   help="IPC-2581 file for counterhole data next to ODB++ (default: from the fab folder or "
                        "named like the ODB++ input beside it; off: none)")
    s.add_argument("--ncdrill", "--nc-drill", dest="ncdrill", nargs="?", const="", default=None, metavar="PATH",
                   help="read NC drill output (counterhole depths and angles); off by default. Without PATH: "
                        "the fab folder's NCDRILL/, inside the ODB++ input, else a report named like the input "
                        "beside it. PATH: a .DRR report, a folder or an archive")
    s.add_argument("--no-ncdrill", "--no-nc-drill", dest="ncdrill", action="store_const", const=False,
                   help="do not read NC drill output (overrides config.toml)")
    s.add_argument("--config", default=None, metavar="FILE",
                   help="config file (default: config.toml of the fab folder)")
    s.add_argument("--no-config", action="store_true", help="ignore config files")
    s.add_argument("--step", default=None, help="ODB++ step (default: 'pcb', else a step without step-and-repeat)")

    g = p.add_argument_group("thickness, mm (override the fab data)")
    g.add_argument("--board-thickness", type=float, help="copper-to-copper thickness; scales the dielectrics to match")
    g.add_argument("--copper-thickness", type=float, help="every copper layer (default: fab data, else 0.035)")
    g.add_argument("--mask-thickness", type=float, help="solder mask over copper (default: fab data, else 0.020)")
    g.add_argument("--silk-thickness", type=float, help="silkscreen (default: fab data, else 0.020)")
    g.add_argument("--finish-thickness", type=float,
                   help="surface finish; without a finish layer this adds finish on exposed copper")
    g.add_argument("--paste-thickness", type=float, help="solder paste with --paste (default 0.120)")
    g.add_argument("--plating-thickness", type=float, help="hole wall plating (default: tool sizes, else 0.025)")
    g.add_argument("--via-plating-thickness", type=float, help="via wall plating (default: as --plating-thickness)")
    g.add_argument("--cap-thickness", type=float, help="via cap plating (0.015)")
    g.add_argument("--layer-thickness", action="append", default=[], metavar="LAYER=MM",
                   help="thickness of one layer (repeatable)")

    lg = p.add_argument_group("layers")
    lg.add_argument("--mask", action=argparse.BooleanOptionalAction, default=None, help="solder mask (on)")
    lg.add_argument("--silk", action=argparse.BooleanOptionalAction, default=None, help="silkscreen (on)")
    lg.add_argument("--finish", action=argparse.BooleanOptionalAction, default=None, help="surface finish (on)")
    lg.add_argument("--paste", action=argparse.BooleanOptionalAction, default=None, help="solder paste (off)")
    lg.add_argument("--clip-silk", action=argparse.BooleanOptionalAction, default=None,
                    help="remove silkscreen over mask openings (on)")
    lg.add_argument("--finish-type", default=None, help="ENIG, HASL, OSP, ... (sets the finish colour)")
    lg.add_argument("--rout", choices=["auto", "cut", "ignore"], default=None,
                    help="rout layers: auto cuts internal rout paths only (default), cut all, or ignore")

    hg = p.add_argument_group("counterholes and back drills")
    hg.add_argument("--counterhole", action="append", default=[], metavar="SPEC",
                    help="both sides: type=bore|sink angle=DEG depth=MM (see below)")
    hg.add_argument("--counterhole-top", action="append", default=[], metavar="SPEC", help="top side")
    hg.add_argument("--counterhole-bottom", "--counterhole-bot", dest="counterhole_bottom", action="append",
                    default=[], metavar="SPEC", help="bottom side")
    hg.add_argument("--counterhole-depth-mode", choices=["expose", "through"], default=None,
                    help="counterbores without a depth: stop on the far copper layer of the span (expose, "
                         "default) or cut it (through)")
    hg.add_argument("--counterbore", dest="counterbore_rules", action="append", default=[],
                    metavar="HOLE:DIA:DEPTH[:SIDE]")
    hg.add_argument("--countersink", dest="countersink_rules", action="append", default=[],
                    metavar="HOLE:DIA:ANGLE[:SIDE]")
    hg.add_argument("--backdrill-stub", type=float, default=None,
                    help="dielectric left above the must-not-cut layer, mm (0 = Altium's depth: up to the "
                         "layer); stub data in the fab data takes precedence")
    # odb2step 1.1.0 spellings
    hg.add_argument("--counterhole-type", choices=["counterbore", "countersink"], default=None,
                    help=argparse.SUPPRESS)
    hg.add_argument("--countersink-angle", type=float, default=None, help=argparse.SUPPRESS)
    hg.add_argument("--counterhole-depth", type=float, default=None, help=argparse.SUPPRESS)

    vg = p.add_argument_group("vias (override the fab data; --via-max-diameter limits them)")
    vg.add_argument("--via-fill", choices=["none", "epoxy", "copper", "silver"], default=None, help="fill every via")
    vg.add_argument("--via-cap", action=argparse.BooleanOptionalAction, default=None,
                    help="cap every via (IPC-4761 type VII; implies fill)")
    vg.add_argument("--via-tent", choices=["none", "top", "bottom", "both"], default=None,
                    help="solder mask over every via")
    vg.add_argument("--via-plug", choices=["none", "top", "bottom", "both"], default=None, help="plug every via")
    vg.add_argument("--via-max-diameter", type=float, default=None, help="only vias up to this finished diameter")
    vg.add_argument("--via-protection", action="append", default=[], metavar="RULE", help="IPC-4761 rule, see below")
    vg.add_argument("--fill-material", choices=["epoxy", "copper", "silver"], default=None,
                    help="fill material where none is named (epoxy)")
    vg.add_argument("--plug-depth", type=float, default=None, help="plug depth, fraction of the hole length (0.3)")
    vg.add_argument("--drill-through-mask", action=argparse.BooleanOptionalAction, default=None,
                    help="vias cut the solder mask even where the mask layer covers them")

    cg = p.add_argument_group("colours (name, #RRGGBB or R,G,B); names: " + ", ".join(sorted(NAMED_COLORS)))
    for key, hlp in (("board", "laminate"), ("copper", "copper"), ("mask", "green"), ("silk", "white"),
                     ("finish", "auto: from the finish type"), ("plating", "hole walls, copper"),
                     ("fill", "auto: by fill material"), ("cap", "#d8894a"), ("paste", "#a6a6a6")):
        cg.add_argument(f"--{key}-color", default=None, help=hlp)

    og = p.add_argument_group("geometry and output")
    og.add_argument("--arc-tolerance", type=float, default=None, help="tessellation chord error, mm (0.001)")
    og.add_argument("--arc-fit", choices=["loose", "strict", "off"], default=None,
                    help="rebuild true arcs and circles: loose also fits arcs exported as polylines (default)")
    og.add_argument("--max-holes-per-face", type=int, default=None,
                    help="split larger regions into tiles; STEP export time grows with the square of the "
                         "holes per face (1000, 0 = never)")
    og.add_argument("--schema", choices=["AP214", "AP242", "AP203"], default=None)
    og.add_argument("--svg-dir", default=None, help="also write the 2D layer regions as SVG files")
    og.add_argument("-v", "--verbose", action="count", default=1)
    og.add_argument("-q", "--quiet", action="store_true")
    return p


def join_counterhole_args(argv: List[str]) -> List[str]:
    """'--counterhole-top type=sink angle=82' -> '--counterhole-top type=sink,angle=82',
    so the option takes several words without swallowing the inputs."""
    out: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
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


def _legacy(args):
    """odb2step 1.1.0 options -> --counterhole."""
    bits = []
    if args.counterhole_type:
        bits.append(f"type={args.counterhole_type}")
    if args.countersink_angle:
        bits.append(f"angle={args.countersink_angle}")
    if args.counterhole_depth:
        bits.append(f"depth={args.counterhole_depth}")
    if bits:
        args.counterhole = [",".join(bits)] + list(args.counterhole)


def default_output(primary: str, name: str, fab: Optional[str]) -> str:
    folder = fab or os.path.dirname(os.path.abspath(primary.rstrip("/\\")))
    if os.access(folder, os.W_OK):
        return os.path.join(folder, name + ".step")
    return os.path.join(os.getcwd(), name + ".step")


def convert(args) -> int:
    from . import ipc2581, ncdrill, odbpp
    from .brep import ArcFit, Brep, write_step
    from .builder import Model, parse_size_rule, write_svgs
    from .geometry import Tess
    from .sources import LABEL, choose, discover

    Log.reset(0 if args.quiet else args.verbose)
    t0 = time.time()
    _legacy(args)
    srcs = discover(args.inputs, args.config, args.no_config)
    cfg, label = None, "config.toml"
    if srcs.config:
        cfg = load_config(srcs.config)
        label = os.path.basename(srcs.config)
        for w in check_config(cfg, label):
            warn(w)
    try:
        opts = resolve(args, cfg, label)
        opts.via_rules = [parse_via_rule(s) for s in opts.via_protection]
    except (ConfigError, ValueError) as exc:
        raise SystemExit(str(exc))
    for spec in opts.counterbore_rules:
        parse_size_rule(spec, "counterbore")
    for spec in opts.countersink_rules:
        parse_size_rule(spec, "countersink")
    if args.list:
        opts.list = True
    Tess.tol = max(1e-5, opts.arc_tolerance)
    ArcFit.enabled = opts.arc_fit != "off"
    ArcFit.strict = opts.arc_fit == "strict"
    Brep.max_holes = opts.max_holes_per_face

    choose(srcs, opts.source, args.ipc2581)
    info(f"Input    : {LABEL[srcs.primary]} {srcs.primary_path}" + (f"   (fab folder {srcs.fab})" if srcs.fab else ""))
    if srcs.config:
        info(f"Config   : {srcs.config}")
    for nt in srcs.notes:
        info("  " + nt)
    if srcs.primary == "odb":
        reader = odbpp.OdbReader(srcs.primary_path, opts)
        board = reader.read()
    else:
        reader = ipc2581.IpcReader(srcs.primary_path, opts)
        board = reader.read()
    if srcs.secondary_ipc:
        hints, notes = ipc2581.read_hints(srcs.secondary_ipc)
        board.hints += hints
        board.hint_notes += notes
    if opts.ncdrill is not None:
        inside = None
        if srcs.primary == "odb":
            inside = ncdrill.pool_from_files("inside the ODB++ input", reader.inside_files())
        explicit = [opts.ncdrill] if opts.ncdrill else []
        beside = [os.path.dirname(os.path.abspath(srcs.primary_path.rstrip("/\\")))]
        stem = ncdrill._stem(srcs.primary_path)
        chosen, notes = ncdrill.find_reports(explicit, inside, beside, stem, srcs.ncdrill_dirs)
        hints, notes2 = ncdrill.read_hints(chosen)
        board.hints = hints + board.hints          # NC drill first: it has exact counterbore depths
        board.hint_notes += notes + notes2

    model = Model(board, opts)
    model.prepare()
    model.report()
    if args.list:
        return 0
    info("\nBuilding solids ...")
    model.build()
    if board.after_build is not None:
        board.after_build()
    if opts.svg_dir:
        write_svgs(opts.svg_dir, model.svg, model.B)
    out = opts.output
    if out and "output" in opts.sources and not opts.sources["output"].startswith("--") and srcs.config:
        out = os.path.join(os.path.dirname(os.path.abspath(srcs.config)), out)
    out = out or default_output(srcs.primary_path, board.name, srcs.fab)
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
                                description="Create a fab folder: ODB++/, IPC2581/, GERBER/, NCDRILL/ and a "
                                            "config.toml populated with the defaults.")
    p.add_argument("folder", nargs="?", default=".", help="fab folder to create or complete (default: here)")
    p.add_argument("--force", action="store_true", help="overwrite an existing config.toml")
    a = p.parse_args(argv)
    for line in init_fab_folder(a.folder, __version__, a.force):
        print(line)
    print("Put the fab data in the subfolders, edit config.toml, then run:  pcbfab2step " + a.folder)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "init":
        return init(argv[1:])
    listing = False
    if argv and argv[0] in ("convert", "info"):
        listing = argv[0] == "info"
        argv = argv[1:]
    args = build_parser().parse_args(join_counterhole_args(argv))
    if listing:
        args.list = True
    try:
        return convert(args)
    except ConfigError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    sys.exit(main())
