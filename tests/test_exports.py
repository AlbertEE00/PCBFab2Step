# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Layer exports (DXF, SVG, STEP) and the model content options."""


import os

import pytest

from pcbfab2step import exports, ipc2581
from pcbfab2step.builder import Model
from pcbfab2step.cli import main


def test_parse_export():
    assert exports.parse_export("Bottom Paste:step,dxf", None) == ("Bottom Paste", ["step", "dxf"])
    assert exports.parse_export("outline", "x/board.svg") == ("outline", ["svg"])
    with pytest.raises(SystemExit, match="add the format"):
        exports.parse_export("outline", None)
    with pytest.raises(SystemExit, match="unknown format"):
        exports.parse_export("outline:pdf", None)


def _src(tmp_path, ipc_file):
    src = tmp_path / "board.cvg"
    src.write_bytes(open(ipc_file, "rb").read())
    return src


def test_export_2d(tmp_path, ipc_file):
    src = _src(tmp_path, ipc_file)
    assert main([str(src), "--export", "top solder:dxf,svg", "-q"]) == 0
    out = tmp_path / "PCBFab2Step"
    dxf = next(out.glob("*Top Solder.dxf")).read_text()
    assert "OUTLINE" in dxf and "TOP_SOLDER" in dxf and "AC1009" in dxf
    assert main([str(src), "--export", "solder-bot:dxf", "--no-outline", "-q"]) == 0
    assert "OUTLINE" not in next(out.glob("*solder-bot.dxf")).read_text()
    assert not list(out.glob("*.step"))                                    # one call, one output


def test_target_errors(tmp_path, ipc_file):
    src = _src(tmp_path, ipc_file)
    with pytest.raises(SystemExit, match="no target 'nothing'"):
        main([str(src), "--export", "nothing:dxf", "-q"])


def test_aperture_offset():
    """Stencil aperture pull back: square corners stay square; an aperture can vanish."""
    import shapely
    from pcbfab2step import exports as ex
    g = shapely.box(0, 0, 2, 1)
    assert ex.offset(g, -0.1).area == pytest.approx(1.8 * 0.8)            # square corners stay square
    assert ex.offset(g, 0.1).area == pytest.approx(2.2 * 1.2)
    assert ex.offset(g, -0.6).is_empty


def test_export_step_with_outline(tmp_path, ipc_file):
    pytest.importorskip("OCP")
    src = _src(tmp_path, ipc_file)
    assert main([str(src), "--export", "Bottom Solder:step", "--outline", "-q"]) == 0
    text = next((tmp_path / "PCBFab2Step").glob("*Bottom Solder.step")).read_text(errors="ignore")
    assert "PCB (" in text and "'Bottom Solder'" in text


def test_external_only(ipc_file, opts):
    pytest.importorskip("OCP")
    opts.external_only, opts.vias, opts.outer_copper = True, None, "pads"
    m = Model(ipc2581.read(ipc_file, opts), opts)
    m.prepare()
    assert not any(h.kind == "via" for h in m.holes)               # vias not drilled by default
    assert any(n.startswith("vias not drilled") for n in m.hole_notes)
    m.build()
    names = [c["name"] for c in m.components]
    assert any(n.startswith("Core (") for n in names)
    assert not any(n.startswith(("MID", "CORE (", "PP 1")) for n in names)    # inner layers merged


def test_components_from_ecad_step(tmp_path, ipc_file):
    """An ECAD STEP (board body + a part under it) in the fab folder's STEP/:
    the part is added on the model's bottom surface, the board body left out."""
    pytest.importorskip("OCP")
    from pcbfab2step.brep import OCC, Brep, write_step
    from shapely.geometry import box
    OCC.load()
    fab = tmp_path / "FAB"
    main(["init", str(fab)])
    (fab / "IPC2581" / "board.cvg").write_bytes(open(ipc_file, "rb").read())
    (fab / "STEP").mkdir(exist_ok=True)               # init creates it
    b = Brep.prisms(box(0, 0, 40, 30), -1.6, 0.0)            # ECAD frame: top at z=0
    part = Brep.prisms(box(5, 5, 8, 7), -2.6, -1.6)           # under the board
    write_step(str(fab / "STEP" / "board.step"), "PCB",
               [{"name": "Board", "color": (0, 0.5, 0), "solids": b},
                {"name": "U1", "color": (0.1, 0.1, 0.1), "solids": part}])
    ecad = fab / "STEP" / "board.step"                       # as if an ECAD wrote it
    ecad.write_text(ecad.read_text(encoding="latin-1").replace("by pcbfab2step", "by Altium"), encoding="latin-1")
    assert main([str(fab), "-q"]) == 0
    out = next((fab / "PCBFab2Step").glob("*.step"))
    text = out.read_text(errors="ignore")
    assert "'U1'" in text and "'Board'" not in text and "'Components bottom'" in text and "'PCB'" in text


def test_stencil(tmp_path, odb_job):
    """A paste layer (after the last copper row: the bottom side) -> 'stencil' finds stencil-bot: the
    outline minus the (offset) apertures; STEP: a plate on the bottom surface."""
    pytest.importorskip("OCP")
    with open(os.path.join(odb_job, "matrix", "matrix"), "a") as f:
        f.write("LAYER {\n   ROW=7\n   CONTEXT=BOARD\n   TYPE=SOLDER_PASTE\n   NAME=TOP_PASTE\n"
                "   POLARITY=POSITIVE\n   START_NAME=\n   END_NAME=\n   OLD_NAME=\n   ADD_TYPE=\n}\n")
    d = os.path.join(odb_job, "steps", "pcb", "layers", "top_paste")
    os.makedirs(d)
    with open(os.path.join(d, "features"), "w") as f:
        f.write("UNITS=MM\n$0 rect2000x1000\nP 5 15 0 P 0 0\n")
    assert main([odb_job, "--export", "stencil:svg,step", "--thickness", "stencil=4mil", "--aperture-offset",
                 "-0.1mm", "-q"]) == 0
    out = os.path.join(os.path.dirname(odb_job), "PCBFab2Step")
    svg = [n for n in os.listdir(out) if n.endswith("stencil-bot.svg")]
    stp = [n for n in os.listdir(out) if n.endswith("stencil-bot.step")]
    assert svg and stp
    text = open(os.path.join(out, stp[0]), errors="ignore").read()
    assert "stencil-bot (102 um)" in text
