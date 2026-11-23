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
| ODB++ (folder, `.tgz`, `.tar`, `.zip`) | geometry (primary source) |
| IPC-2581 rev B/C (`.cvg`, `.xml`, zipped) | geometry, or next to ODB++ the counterhole types and angles |
| NC drill (Altium `.DRR` report and drill files) | counterhole depths and angles (secondary, off by default) |
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
`IPC2581/` and `NCDRILL/`. With several candidates of one kind the newest is
used and the report says so.

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
3. NC drill report (with `--ncdrill`): exact depth, or angle for countersinks
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

Altium defines a back drill by its first and last layer and drills "up to,
but not touching, the last layer": the last layer of the back drill span is
the must-not-cut layer, in ODB++ and IPC-2581 alike. The drill stops on that
layer's face; `--backdrill-stub MM` (`[backdrill] stub`) leaves dielectric
above it instead. Stub or depth values in the fab data take precedence
(ODB++ `.backdrill_max_depth`, `.backdrill_max_stub_drill`; IPC-2581
`MAX_STUB_LENGTH` when it is smaller than the drill depth: Altium writes the
drill depth itself there, which is recognised and not taken as a stub).

A back drill not larger than the plated hole it would drill out does not
remove plating and is ignored (reported). A plated hole that repeats a back
drill's position, size and span over a smaller plated hole is taken as that
back drill (Altium lists bottom back drills in the drill pair as well).

## Via protection

Applied in this order: fab data (ODB++ `.ipc_via_type_*`, `.via_in_pad`,
`.is_capped`, `.cu_base`; document layers named Filling, Capping, Plugging or
Tenting in ODB++ and IPC-2581), then `--via-fill`, `--via-cap`, `--via-tent`,
`--via-plug` (optionally limited by `--via-max-diameter`), then
`--via-protection` rules such as `7:max=0.3` (fill and cap vias up to 0.3 mm)
or `1b` (tent all vias both sides).

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
  NC drill data or `depth=` for the exact depth.

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
