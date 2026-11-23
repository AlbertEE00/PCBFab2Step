# PCBFab2Step

Convert PCB fabrication data to a coloured STEP model: the full stackup with
real thicknesses, copper of every layer, dielectrics, solder mask, silkscreen,
surface finish, solder paste, drilled and plated holes, blind, buried and
laser vias, counterbores, countersinks, back drills, dual-diameter holes,
rout cut-outs and via protection (IPC-4761 filling, capping, plugging,
tenting).

Inputs:

| format | role |
|---|---|
| ODB++ (folder, `.tgz`, `.tar`, `.zip`) | geometry (primary source); next to IPC-2581 in a fab folder, the IPC-4761 via types |
| IPC-2581 rev B/C (`.cvg`, `.xml`, zipped) | geometry, or next to ODB++ the counterhole types and angles |
| NC drill (Altium `.DRR` report and drill files) | counterhole depths and angles (secondary: read in a fab folder, else with `--ncdrill`) |
| Gerber | not read yet |

Every reader turns its files into one shared board description
(`board.py`); the builder turns that description into solids, so all
formats get the same stackup, hole and coating logic.

## Install

Python 3.11 to 3.14 (the OpenCascade bindings, `cadquery-ocp`, have no wheels
for newer versions yet).

```
uv sync                      # in a clone; creates .venv with the dependencies
uv run pcbfab2step --help
```

or `pip install .` (optionally `pip install .[plot]` for the TrueType text
fallback and `tools/section_plot.py`).

## Quick start

```
pcbfab2step board.tgz                       # ODB++ -> board.step next to it
pcbfab2step board.tgz board.cvg             # ODB++ geometry, IPC-2581 counterhole data
pcbfab2step board.cvg                       # IPC-2581 only
pcbfab2step info board.tgz                  # stackup and hole report, no STEP
pcbfab2step init FAB                        # create a fab folder with config.toml
pcbfab2step FAB                             # convert what is in the fab folder
```

`python -m pcbfab2step` works the same.

## Fab folder

`pcbfab2step init FAB` creates:

```
FAB/
  ODB++/        ODB++ product model
  IPC2581/      IPC-2581 file
  NCDRILL/      NC drill output (.DRR report and drill files)
  GERBER/       (not read yet)
  config.toml   settings, populated with the defaults
```

Nothing depends on this layout: a single file always works, a plain folder is
searched for ODB++ and IPC-2581 files, and a file given from a fab folder's
subfolder (`FAB/ODB++/board.tgz`) still uses that fab folder's `config.toml`,
`IPC2581/` and `NCDRILL/`. Altium's own output folder names (`ODB++`,
`IPC-2581`, `NC Drill`, `Gerber`) are recognised as well, so an OutJob's
output folder can be given as it is. With several candidates of one kind the
newest is used and the report says so.

A fab folder is read as a whole: the primary source gives the geometry and
the other sources fill in what it lacks, matched by position (IPC-2581:
counterhole types and angles; NC drill: exact counterhole depths; ODB++ next
to an IPC-2581 primary: the IPC-4761 via types). Where two sources describe
the same hole the report says whether they agree.

Which source gives the geometry: ODB++ when present (`--source ipc2581`, or
`[sources] primary`, to use the IPC-2581 geometry instead). With both present,
IPC-2581 then supplies the counterhole data. An IPC-2581 file named like the
ODB++ input beside it is picked up automatically (`--ipc2581 off` leaves it
out, `--ipc2581 PATH` names another).

## Settings and precedence

```
command line  >  config.toml  >  fab data  >  built-in defaults
```

In `config.toml` a value of `"default"` keeps the fab data where it has the
value, else the built-in default named in the comment next to the key. Any
other value overrides the fab data, and a command line option overrides the
config file. The report prints the source of every thickness and of every
counterhole type, angle and depth, so each number can be traced.

Main sections: `[stackup]` (board and copper thickness, `[stackup.layers]`
per layer), `[soldermask]`, `[silkscreen]`, `[finish]`, `[paste]` (enable,
colour, thickness, finish type), `[plating]` (hole wall and via wall plating),
`[vias]` (fill, cap, tent, plug every via, IPC-4761 rules), `[counterholes]`
(with `[counterholes.top]` and `[counterholes.bottom]`), `[backdrill]`,
`[geometry]`, `[sources]`, `[output]`. Unknown keys are reported, so a typo
does not pass silently.

## Counterholes

ODB++ (as Altium writes it) marks counterholes but has no type, angle or
depth for them. The type, angle and depth of each counterhole are resolved in
this order:

1. command line: `--counterhole-top` / `--counterhole-bottom`, then `--counterhole` (both sides)
2. config: `[counterholes.top]` / `[counterholes.bottom]`, then `[counterholes]`
3. NC drill report (read in a fab folder, else with `--ncdrill`): exact depth, or angle for countersinks
4. IPC-2581 padstacks: type and angle (see below)
5. the counterhole layer itself (ODB++ `ADD_TYPE=COUNTERSINK/COUNTERBORE` or its name)
6. defaults: counterbore, 90 degrees, depth from the layer span

```
--counterhole type=sink angle=82          both sides
--counterhole-top bore depth=0.8          top only; wins over --counterhole
--counterhole-bottom type=sink            (--counterhole-bot also works)
```

Values: `type=bore|sink` (also `counterbore`, `countersink`), `angle=DEG`
(included angle), `depth=MM` (counterbore depth from the outer copper). The
words may be separated by spaces or commas.

IPC-2581: Altium writes, for every counterhole, the hole diameter at each
copper layer (`PadStack/LayerHole`), taken at the face of the layer nearest
the counterhole's side. A diameter that shrinks linearly with depth is a
countersink and gives the angle exactly (for example 3.2 mm at the surface,
2.794 mm at 0.2032 mm depth: 90 degrees). A constant diameter that drops to
the drill diameter is a counterbore; its floor lies between the last layer
cut and the first layer not cut, and is put on the near face of the first
layer not cut (the report gives the range the data allows). If only the
outer layer is cut the type is not decided by the data and the chain above
continues.

Counterholes by hole size, for boards without counterhole layers:
`--counterbore HOLE:DIA:DEPTH[:SIDE]`, `--countersink HOLE:DIA:ANGLE[:SIDE]`.

## Back drills

Altium defines a back drill by its layer pair and drills "up to, but not
touching, the last layer": the last layer of the pair is the must-not-cut
layer. IPC-2581 names it in the back drill spec. In ODB++ the back drill
layer is named after the pair (`DRILL_BACKDRILLS_SLOT-GND2`), and that name
decides: Altium 26 writes the layer's START/END span one layer short of the
must-not-cut layer (`SLOT..BLANK3`), older exports up to it. Without a pair
in the name the last layer of the span is taken. The drill stops on the
must-not-cut layer's face; `--backdrill-stub MM` (`[backdrill] stub`) leaves dielectric
above it instead. Stub or depth values in the fab data take precedence
(ODB++ `.backdrill_max_depth`, `.backdrill_max_stub_drill`; IPC-2581
`MAX_STUB_LENGTH` when it is smaller than the drill depth: Altium writes the
drill depth itself there, which is recognised and not taken as a stub).

On a filled via the fill closes the back drill bore up to the surface (vias
are filled after back drilling), and a capped via is capped over the bore;
the coatings then cover it. An open bore cuts the mask and ends any cap, plug
or tenting on that side.

A back drill not larger than the plated hole it would drill out does not
remove plating and is ignored (reported). A plated hole that repeats a back
drill's position, size and span over a smaller plated hole is taken as that
back drill (Altium lists bottom back drills in the drill pair as well).

## Via protection

Applied in this order: fab data (ODB++ `.ipc_via_type_*`, `.via_in_pad`,
`.is_capped`, `.cu_base`; Altium's per-type via drill layers such as
`DRILL_TYPE_7`; document layers named Filling, Capping, Plugging or Tenting
in ODB++ and IPC-2581), then `--via-fill`, `--via-cap`, `--via-tent`,
`--via-plug` (optionally limited by `--via-max-diameter`), then
`--via-protection` rules such as `7:max=0.3` (fill and cap vias up to 0.3 mm)
or `1b` (tent all vias both sides).

On the document layers, Altium's via features (IPC-4761 fill, cap, ...) are
pads at the via positions: pads in ODB++, pads of padstacks of their own in
IPC-2581. When a layer has such pads only they mark vias; lines, arcs and
contours on the layer are taken as artwork (footprint drawings, outlines).
A layer without pads is read primitive by primitive (arc centres, line
middles, shape centroids). The report says which was used.

Altium's ODB++ also lists the vias of each IPC-4761 type on a drill layer of
their own (`DRILL_TYPE_7`: type VII, filled and capped), besides the regular
drill layer. Such a layer gives the type of the holes at its positions and
adds no second hole. Fab data adds up: a via is filled or capped when any of
it says so, and the report lists holes one source marks and another does not
(Altium 26 leaves back drilled type VII vias off the Capping layer).

Tenting closes a via's own opening in the solder mask and nothing else. A
via inside the opening of a pad or of exposed copper stays open, since the
mask layer has no mask there and a fab puts no mask on a pad or into an
opening the artwork asks for (via in pad is plugged, or filled and capped,
instead). The via openings come from the data (IPC-2581 via padstack pads,
ODB++ pads with `.pad_usage=via`); without that, an opening counts as the
via's own when it is round around one via, at most 0.5 mm wider than the
drill radius, and has no paste.

## Output

One named, coloured body per layer and material (copper layers, dielectrics
with material, Dk and Df in the name, mask, silkscreen, finish, paste, hole
plating, via fill, plugs and caps), written as STEP AP214 (`--schema AP242`
or `AP203`). Arcs and circles of the data are rebuilt as exact cylinders,
cones and circular edges. `--svg-dir` also writes the 2D region of every
layer for checking.

`tools/verify_step.py model.step [--overlap]` re-imports a STEP and prints
per-body validity, volume, z range and face types (and overlaps between
bodies); `tools/section_plot.py` draws a cross-section.

## Limitations

- Gerber input is not read yet.
- ODB++ step-and-repeat FLIP is not supported (placed unflipped, with a warning).
- IPC-2581 Text features are left out (Altium writes text as contours) and
  Moire primitives are approximated; both are reported.
- Counterbore floors from IPC-2581 are only known to the layer spacing; use
  NC drill data (a fab folder with the NC drill output) or `depth=` for the
  exact depth.

## Development

```
uv run pytest
```

The tests build small synthetic ODB++ and IPC-2581 boards; no design data is
kept in the repository.

Layout:

```
src/pcbfab2step/
  pcbfab2step.py   command line coordinator (convert, info, init)
  sources.py       input and fab folder discovery
  config.py        settings, config.toml format and merge, init
  board.py         the shared board description (layers, holes, counterholes, ...)
  odbpp.py         ODB++ reader
  ipc2581.py       IPC-2581 reader (primary, or counterhole hints)
  ncdrill.py       NC drill reader (counterhole hints)
  builder.py       stackup, holes, counterholes, back drills, coatings -> solids
  geometry.py      2D geometry and tessellation
  brep.py          OpenCascade B-rep, exact arc recovery, STEP writer
tests/
tools/
```

## Version history

The version continues the single-file `odb2step` (1.0.0 to 1.1.0, ODB++ only).

- 1.3.0: a fab folder is read as a whole: its NC drill output by default
  (exact counterbore depths, also for an IPC-2581 primary), and next to an
  IPC-2581 primary the ODB++ via types; sources describing the same
  counterhole are cross-checked in the report. Altium's own output folder
  names are recognised. ODB++ from Altium 26: back drills take the
  must-not-cut layer from the layer name (the START/END span stops one layer
  short of it, which gave zero-depth back drills), and the per-type via drill
  layers (`DRILL_TYPE_7`) give the IPC-4761 type instead of a second copy of
  every via. Tenting closes only a via's own mask opening; a via inside a pad
  or exposed copper opening stays open.
- 1.2.1: IPC-2581 via features: the Filling, Capping (and other via
  protection) markers Altium writes as padstack pads are read, so filled and
  capped vias come from the fab data without flags; drawn artwork on those
  layers no longer marks vias. ODB++ uses the pads of such layers the same
  way. The fill of a back drilled via closes the bore up to the surface and
  the via keeps its cap there.
- 1.2.0: package `pcbfab2step` with a shared board description, an
  IPC-2581 reader (geometry, and counterhole types and angles from the
  padstack hole diameters), NC drill off by default (`--ncdrill`),
  `--counterhole[-top|-bottom]` options, `config.toml` with the precedence
  above, `init` for fab folders, `--via-fill/cap/tent/plug`, separate via
  plating thickness. Fixes: back drills stop on the must-not-cut layer (the
  last layer of the span) instead of cutting through it, and a back drill now
  trims every smaller plated hole at its position; countersink cones no longer
  leave zero-width faces where they meet the hole wall; ring slivers left by
  rounded export sizes are removed; `--no-finish` also stops the finish body
  added by `--finish-thickness`. The ODB++ results are otherwise unchanged
  from 1.1.0 (same bodies and volumes on the regression boards).

## License

No license has been chosen yet; until a LICENSE file is added, the default
copyright applies.
