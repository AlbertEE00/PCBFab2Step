# PCBFab2Step

Convert PCB fabrication data to a coloured STEP model: the full stackup with
real thicknesses, copper of every layer, dielectrics, solder mask, silkscreen,
surface finish, solder paste, drilled and plated holes, blind, buried and
laser vias, counterbores, countersinks, back drills, dual-diameter holes,
rout cut-outs, via protection (IPC-4761 filling, capping, plugging, tenting)
and the components of the ECAD's own STEP export. Or export one layer of it
(paste, stencil, solder mask, silkscreen, copper, outline) as DXF, SVG or
STEP with the same origin.

| input | what it gives |
|---|---|
| ODB++ (job folder, `.tgz`, `.tar`, `.zip`) | the board; next to IPC-2581, the IPC-4761 via types |
| IPC-2581 rev B/C (`.cvg`, `.xml`, zipped) | the board, or next to ODB++ the counterhole types and angles |
| NC drill (Altium `.DRR` report and drill files) | exact counterhole depths and angles |
| STEP from the ECAD (Altium: board + parts) | the components |
| Gerber | found, not read yet |

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

## Usage

One call writes one output. Without `--export` it is the board as a STEP
model; with `--export TARGET:FORMAT` it is that one layer. The options act on
that output, and options that belong to the other one are refused.

```
pcbfab2step [INPUT ...] [options]                         the board as a STEP model
pcbfab2step [INPUT ...] --export TARGET:FORMAT [options]  one layer as DXF, SVG or STEP
pcbfab2step info [INPUT ...]                              what was found, stackup, holes, targets
pcbfab2step init [FOLDER]                                 config.toml with every setting
pcbfab2step settings                                      every setting and its default
```

```
pcbfab2step FAB                                       FAB/PCBFab2Step/<board>.step
pcbfab2step FAB --sanitize                            no inner layers, only exposed copper
pcbfab2step FAB --show paste --color mask=black
pcbfab2step FAB --export stencil-bot:step --thickness stencil=4mil --aperture-offset -1mil
pcbfab2step FAB --export paste-bot:dxf --no-outline
pcbfab2step FAB --export solder-bot:svg --invert      the mask openings (as in Gerber)
pcbfab2step FAB --export outline:step                 the plain PCB body
pcbfab2step board.cvg parts.step                      IPC-2581 board with the parts of a STEP
pcbfab2step FAB --sources ipc+drill+step              IPC-2581 as the board, the ODB++ not read
```

`uv run pcbfab2step` in a clone, or `python -m pcbfab2step`, works the same.
`pcbfab2step --help` has the details of every option.

Sizes are mm unless a unit is given: `0.1`, `0.1mm`, `100um`, `4mil`,
`0.004in`.

## Inputs and outputs

No layout is assumed. A folder given is searched with its subfolders (four
levels); a file given also brings the other outputs in its folder, and in the
parent folder when its folder is one of the usual output folders (`ODB++`,
`IPC-2581`, `NC Drill`, `Gerber`, `STEP`). So Altium's OutJob output folder
works as it is, and so does everything in one flat folder:

```
FAB/                                  or   FAB/
  ODB++/  (.tgz, .zip or odb/)               board.tgz
  IPC-2581/board.cvg                         board.cvg
  NC Drill/board.DRR, board.TXT ...          board.DRR, board.TXT ...
  STEP/board.step                            board.step
  config.toml                                config.toml
  PCBFab2Step/      <- written here          PCBFab2Step/
```

The results go to `PCBFab2Step/` in the folder given, or in the folder of the
file given (`-o FILE` writes elsewhere):

- the model: `<board>.step`, or `<board>_sanitized-<level>.step`;
- an export: `<board>_<target>.<format>` (the target as given: generic name
  or the layer's name in the fab data).

`PCBFab2Step/` is never searched, and a STEP written by PCBFab2Step is never
taken as an input.

Which sources are used: everything found. ODB++ gives the board before
IPC-2581; the others add what it lacks, matched by position: IPC-2581 the
counterhole types and angles, NC drill reports the exact counterhole depths,
ODB++ next to an IPC-2581 board the IPC-4761 via types, the ECAD's STEP the
components. Where two sources describe the same hole the report says whether
they agree. A geometry file given as an input is the board. `--sources`
limits and orders them: `--sources ipc+drill+step` takes IPC-2581 as the
board, reads the NC drill output and the STEP, and leaves the ODB++ out
(`odb`, `ipc`, `drill`, `step`, joined with `+` or `,`).

With several files of one kind the one named like the board is used (for the
board itself: the newest); if several are found and none is named like the
board, a secondary source is left out and the report says so. `info` lists
what was found and used.

## Settings and precedence

```
command line  >  config.toml  >  fab data  >  built-in defaults
```

`pcbfab2step init FOLDER` writes `config.toml` with every setting, its
default and a comment; `pcbfab2step settings` prints the same. In
`config.toml` a value of `"default"` keeps the fab data where it has the
value, else the built-in default named in the comment. Any other value
overrides the fab data. On the command line `--set SECTION.KEY=VALUE` sets
any of them (`--set finish.type=ENIG`, `--set backdrill.stub=0.05`,
`--set 'vias.rules=["7:max=0.3"]'`); the common ones have their own options:

| option | what | config.toml |
|---|---|---|
| `--thickness ITEM=SIZE` | `board` (copper to copper; the dielectrics scale), `copper`, `mask`, `silk`, `finish`, `paste`, `plating`, `via-plating`, `cap`, `stencil`, or a layer name; comma list | `[stackup]`, `[soldermask] thickness`, ..., `[stackup.layers]` |
| `--color ITEM=COLOR` | `board`, `copper`, `mask`, `silk`, `finish`, `plating`, `fill`, `cap`, `paste`; a name, `#RRGGBB` or `R,G,B` | `[soldermask] color`, ... |
| `--show LIST`, `--hide LIST` | model layers: `mask`, `silk`, `finish`, `paste` (off by default), `components` | `enabled` keys, `[model] components` |
| `--sanitize [LEVEL]` | `outer`, `exposed` (default), `pads` | `[model] sanitize` |
| `--vias RULE` | IPC-4761 rule, `none`, `undrilled`, `drilled` | `[vias]` |
| `--counterhole[-top\|-bottom] SPEC` | counterhole type, angle, depth | `[counterholes]` |
| `--invert` | export the rest of the outline instead of the layer | |
| `--aperture-offset SIZE` | grow or shrink the artwork of an exported layer | `[export] aperture_offset` |
| `--sources LIST` | sources to use | `[sources] use` |

The report prints the source of every thickness and of every counterhole
type, angle and depth, so each number can be traced. Unknown keys in
`config.toml` and unknown `--set` keys are reported (with the closest names).

## Board model

One named, coloured body per layer and material (copper layers, dielectrics
with material, Dk and Df in the name, mask, silkscreen, finish, paste, hole
plating, via fill, plugs and caps), written as STEP AP214 (`[output] schema`
for AP242 or AP203). Arcs and circles of the data are rebuilt as exact
cylinders, cones and circular edges. With components the tree is

```
<board>
  PCB                      the layer bodies
  Components top
  Components bottom
    P1                     a part with one body
    R1                     a part with several: one body each, own colour
      body
      terminals
      logo
```

### Components

The ECAD's STEP export (Altium: its parts on a simplified board body) adds
its parts to the model; `--hide components` leaves them out, and a STEP
given as an input is used whatever its name.

- The ECAD's board body is left out (named `Board`, else the body whose size
  matches the outline). Every part keeps its name, colours and XY placement;
  if the board body is off the outline the parts are shifted with it.
- In Z each part is put on the model's own outer surface (solder mask
  included) on its side: parts above the ECAD board on the top, parts below
  on the bottom, parts through it by the top. A different board thickness in
  the ECAD export therefore never leaves parts floating or sunk in; the
  report gives both thicknesses.

### Hiding design detail

`--sanitize LEVEL` keeps what can be seen from outside:

- `outer`: only the outer copper layers; the inner copper and all
  dielectrics become one core body, like Altium's STEP. Vias are not drilled
  (`--vias drilled` drills them); their pads stay, and so does what shows on
  the surface: a capped via keeps its cap where the solder mask leaves it in
  view (with the plating and fill under it, within the outer copper, as in
  the full model), so a filled and capped back drill bore is closed as on the
  board. A cap under the mask cannot be seen and is left out. Tenting closes
  the vias' own mask openings as in the full model.
- `exposed` (the default for a bare `--sanitize`): also the outer copper is
  cut to the pads plus the copper the solder mask leaves open; a side without
  solder mask keeps all its copper.
- `pads`: the outer copper is pads only.

Pads come from the data: ODB++ pad features (P records, vias by
`.pad_usage=via`), IPC-2581 padstack pads. `--vias undrilled` leaves the
vias undrilled in any model. With `exposed` and `pads` the pads of undrilled
vias go too (a tented via's opening is not exposed copper), and their caps
with them.

The via settings work as in the full model, e.g. fill, cap and tent every
via: `--set vias.fill=epoxy --set vias.cap=true --set vias.tent=both`.

## Counterholes

ODB++ (as Altium writes it) marks counterholes but has no type, angle or
depth for them. The type, angle and depth of each counterhole are resolved in
this order:

1. command line: `--counterhole-top` / `--counterhole-bottom`, then `--counterhole` (both sides)
2. config: `[counterholes.top]` / `[counterholes.bottom]`, then `[counterholes]`
3. NC drill report (when found): exact depth, or angle for countersinks
4. IPC-2581 padstacks: type and angle (see below)
5. the counterhole layer itself (ODB++ `ADD_TYPE=COUNTERSINK/COUNTERBORE` or its name)
6. defaults: counterbore, 90 degrees, depth from the layer span

```
--counterhole type=sink angle=82          both sides
--counterhole-top bore depth=0.8          top only; wins over --counterhole
--counterhole-bottom type=sink            (--counterhole-bot also works)
```

Values: `type=bore|sink` (also `counterbore`, `countersink`), `angle=DEG`
(included angle), `depth=SIZE` (counterbore depth from the outer copper). The
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

Counterholes by hole size, for boards without counterhole layers, in
`[counterholes]`: `counterbore_rules = ["HOLE:DIA:DEPTH[:SIDE]"]`,
`countersink_rules = ["HOLE:DIA:ANGLE[:SIDE]"]` (or with `--set`).

## Back drills

Altium defines a back drill by its layer pair and drills "up to, but not
touching, the last layer": the last layer of the pair is the must-not-cut
layer. IPC-2581 names it in the back drill spec. In ODB++ the back drill
layer is named after the pair (`DRILL_BACKDRILLS_SLOT-GND2`), and that name
decides: Altium 26 writes the layer's START/END span one layer short of the
must-not-cut layer (`SLOT..BLANK3`), older exports up to it. Without a pair
in the name the last layer of the span is taken. The drill stops on the
must-not-cut layer's face; `[backdrill] stub` (`--set backdrill.stub=0.1`) leaves dielectric
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
in ODB++ and IPC-2581), then the `[vias]` settings (fill, cap, tent, plug
every via, optionally up to `max_diameter`), then the rules of `[vias] rules`
and `--vias`, such as `7:max=0.3` (fill and cap vias up to 0.3 mm) or `1b`
(tent all vias on both sides). `--vias undrilled` leaves the vias undrilled
(their pads and visible caps stay, see Hiding design detail).

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

## Layer export

```
pcbfab2step FAB --export TARGET:FORMAT [--invert] [--outline | --no-outline] [--aperture-offset SIZE]
```

One layer of the board in the board's own coordinates: the same origin as
the STEP model, bottom layers seen from the top (as in Gerber). FORMAT is
`dxf`, `svg` or `step` (or a comma list, `dxf,svg`), or comes from `-o`.

A layer is exported as it is on the board, the same region as its body in
the model, not as its artwork. The solder mask layer of Gerber and ODB++ is
drawn as its openings; the export is the mask itself.

| target | |
|---|---|
| `outline` | DXF/SVG: the board outline; STEP: the plain PCB body (solder mask included, like the ECAD's simplified board) |
| `paste-top`, `paste-bot` | the solder paste deposits |
| `stencil-top`, `stencil-bot` | the stencil: the outline minus the paste openings; STEP: a plate of `--thickness stencil=SIZE` (0.1 mm) lying on the board surface |
| `solder-top`, `solder-bot` | the solder mask: the outline minus its openings, the tented via openings closed, the holes through the mask cut |
| `silk-top`, `silk-bot` | the silkscreen, clipped to the solder mask |
| `copper-top`, `copper-bot`, `copper-N` | a copper layer with its holes cut, N counted from the top |
| the layer's name in the fab data | that layer (`info` lists them) |

Without `-top`/`-bot` (`stencil`, `paste`, `solder`, `silk`) the side that
has data is taken. `--invert` exports the rest of the outline instead: the
solder mask openings (as in Gerber), the paste of a stencil, the clearances
in a copper layer; the file name gets `_inverted`. DXF and SVG include the
outline unless `--no-outline`; a STEP layer is the layer alone at its place
in the model, and `--outline` adds the PCB body (clear of the layer, so the
two never overlap). `--vias`, `--counterhole` and `--sanitize` shape an
exported layer as they shape the model (tenting, holes, copper).

`--aperture-offset SIZE` grows (`+`) or shrinks (`-`) the artwork before the
layer is made: paste and stencil apertures, solder mask openings (the mask
shrinks), copper and silkscreen features. A stencil aperture pull-back is
`--aperture-offset -1mil`. Square corners stay square; apertures that vanish
are counted in the report. Steps shorter than 0.1 um in the data (Altium
rounds coordinates to 1e-7 inch where an arc meets a line) are removed
first, so they cannot turn into spikes at a corner.

DXF is R12 with closed polylines in mm (layer `OUTLINE` and a layer named
after the target); arcs are polylines at the tessellation tolerance. SVG is
in mm, the layer filled and the outline as a line.

## Checking

`--set output.svg_dir=DIR` also writes the 2D region of every layer as SVG.
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
  cli.py           command line: model, export, info, init, settings
  sources.py       input discovery and source choice
  config.py        settings, config.toml format and merge, init
  board.py         the shared board description (layers, holes, counterholes, ...)
  odbpp.py         ODB++ reader
  ipc2581.py       IPC-2581 reader (primary, or counterhole hints)
  ncdrill.py       NC drill reader (counterhole hints)
  builder.py       stackup, holes, counterholes, back drills, coatings -> solids
  exports.py       layer export (DXF, SVG, STEP; stencil)
  components.py    components from the ECAD's STEP export
  geometry.py      2D geometry and tessellation
  brep.py          OpenCascade B-rep, exact arc recovery, STEP writer
tests/
tools/
```

## Version history

The version continues the single-file `odb2step` (1.0.0 to 1.1.0, ODB++ only).

- 2.1.0: layers are exported as they are on the board: `solder-*` is the
  solder mask (it was its openings), copper has its holes cut, silkscreen is
  clipped to the mask. `--invert` is back, for every layer (the mask
  openings, the stencil's paste). `--aperture-offset` no longer turns
  rounding steps in the data into spikes at corners. With `--sanitize` (vias
  undrilled) the vias keep what shows on the surface: visible caps, filled
  and capped back drill bores closed, tenting. `--vias` and `--counterhole`
  also act on exports; stencil DXF and SVG get the outline like the other
  layers (`--no-outline` leaves it out). An ODB++ job folder named `odb` takes
  the board's name from the files beside it. Arc fitting no longer bows a
  straight edge that sits between short chords (a slot side between its end
  arcs): such runs keep their polygon.
- 2.0.0: a new command line. One call writes one output: the board model, or
  with `--export TARGET:FORMAT` one layer (`stencil-bot` replaces the
  inverted paste, `--aperture-offset` for aperture pull-back, `--outline`
  adds the PCB body to a STEP layer). Inputs are any folder or files: the fab
  outputs are found below or beside them (`--sources` limits them), results
  go to `PCBFab2Step/` there. Grouped options: `--sanitize`, `--show`,
  `--hide`, `--vias`, `--thickness`, `--color`, `--set` for any config key,
  sizes with units; `info` and `settings` commands. Components are grouped
  Components top/bottom > part > body. Removed: `--no-step`, `--invert`,
  `--use-generic-names` (both names always work), `--paste-dxf` and the
  other short forms, `--external-only`, `--pads-only`, `--exposed-copper`,
  `--components`, `--source`, `--ipc2581`, `--ncdrill`, `--list`, the
  `--via-*`, `--*-thickness` and `--*-color` options.
- 1.5.0: components from the ECAD STEP export (fab folder `STEP/`,
  `--components`, `--no-components`) placed on the model's surfaces; the
  STEP model is written to `EXPORT/` (or `exports/`) by default.
- 1.4.0: layer exports (`--export`, short forms such as `--paste-dxf`,
  `--invert`, `--outline`, `--stencil-thickness`, `--use-generic-names`,
  `--no-step`): any layer as DXF, SVG or STEP with the model's origin, an
  inverted layer as a stencil plate. Hiding design detail:
  `--external-only` (inner layers merged into one core), `--no-vias`,
  `--pads-only`, `--exposed-copper`. `info` lists the export layer names.
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

This project is dual-licensed under the **GNU Affero General Public License v3.0 (AGPLv3)** and a **Commercial License**.

* **Open-Source License (AGPLv3):** You may use, modify, and distribute this software for free under the terms of the AGPLv3 (see the [LICENSE.txt](LICENSE.txt) file). If you distribute this software or run a modified version as a network service (SaaS), you must release your complete source code under the AGPLv3 and retain all copyright notices.
* **Commercial License:** If you wish to use this software in a proprietary or closed-source product without complying with the copyleft obligations of the AGPLv3, you must purchase a commercial license. Contact **albertvzsmail@gmail.com** for commercial licensing inquiries.

Every source file carries the notice
`SPDX-License-Identifier: AGPL-3.0-only`, Copyright (C) 2026 Albert van Zyl.
