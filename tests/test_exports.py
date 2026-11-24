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


def test_aperture_offset_ignores_rounding_steps():
    """Where an arc meets a line Altium's coordinates can be a rounding step
    (1e-7 inch) apart: a 2.54 nm edge. A mitre offset must not turn it into a spike."""
    import shapely
    from shapely.geometry import Polygon
    from pcbfab2step import exports as ex
    pts = [(-1.0, -1.0), (0.0, 0.0), (0.3, 1.0), (-1.0, 1.0)]       # convex corner of 120 deg at (0, 0)
    stepped = pts[:2] + [(2.54e-6, 0.0)] + pts[2:]                    # the same corner with a rounding step
    a, b = ex.offset(Polygon(pts), 0.1), ex.offset(Polygon(stepped), 0.1)
    assert shapely.hausdorff_distance(a, b) < 1e-4
    assert abs(a.area - b.area) < 1e-5


def test_export_step_with_outline(tmp_path, ipc_file):
    pytest.importorskip("OCP")
    src = _src(tmp_path, ipc_file)
    assert main([str(src), "--export", "Bottom Solder:step", "--outline", "-q"]) == 0
    text = next((tmp_path / "PCBFab2Step").glob("*Bottom Solder.step")).read_text(errors="ignore")
    assert "PCB (" in text and "'Bottom Solder'" in text


def test_solder_export_is_the_mask(ipc_file, opts):
    """The solder target is the mask as on the board (the outline minus the
    openings, minus the holes through it), not the artwork; inverted: the openings."""
    from pcbfab2step.geometry import gdiff
    m = Model(ipc2581.read(ipc_file, opts), opts)
    m.prepare()
    t = exports.find_target(m, "solder-bot")
    art, sheet = t.art(), t.material()
    assert art.area > 0.5 and sheet.intersection(art).area < 1e-9
    assert m.B.area - art.area - 15 < sheet.area < m.B.area - art.area    # less the holes through it
    assert art.difference(gdiff(m.B, sheet)).area < 1e-9                  # --invert: the openings


def test_invert(tmp_path, ipc_file):
    src = _src(tmp_path, ipc_file)
    assert main([str(src), "--export", "solder-bot:dxf", "--invert", "-q"]) == 0
    assert list((tmp_path / "PCBFab2Step").glob("*solder-bot_inverted.dxf"))
    with pytest.raises(SystemExit, match="only with --export"):
        main([str(src), "--invert", "-q"])
    with pytest.raises(SystemExit, match="nothing around it"):
        main([str(src), "--export", "outline:dxf", "--invert", "-q"])


def test_sanitize_keeps_visible_caps(ipc_file, opts):
    """Undrilled vias (--sanitize): the core stays solid; a cap that shows keeps
    its stub within the outer copper, a cap under the solder mask is left out.
    Via_1 is capped both sides: covered by the top mask, in a bottom opening."""
    opts.external_only, opts.vias, opts.outer_copper = True, None, "all"
    m = Model(ipc2581.read(ipc_file, opts), opts)
    m.prepare()
    st = m.stk.stack
    stubs = [h for h in m.holes if h.stub]
    assert not any(h.kind == "via" and not h.stub for h in m.holes)      # no via drilled
    assert len(stubs) == 1
    s = stubs[0]
    assert (round(s.x, 3), round(s.y, 3)) == (15.0, 10.0)
    assert s.cap_bot > 0 and s.cap_top == 0 and s.top_idx == s.bot_idx == len(st) - 1
    assert (s.z_lo, s.z_hi) == (st[-1].z0, st[-1].z1)                  # within the bottom copper
    assert all(c.z_hi <= st[-1].z1 + 1e-9 for c in m.cuts if c.hole is s)
    note = next(n for n in m.hole_notes if n.startswith("vias not drilled"))
    assert "caps kept on the surface: 1 on bottom" in note and "1 cap(s) under the solder mask" in note


def test_external_only(ipc_file, opts):
    pytest.importorskip("OCP")
    opts.external_only, opts.vias, opts.outer_copper = True, None, "pads"
    m = Model(ipc2581.read(ipc_file, opts), opts)
    m.prepare()
    assert not any(h.kind == "via" and not h.stub for h in m.holes)  # vias not drilled by default
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


def test_edge_slivers_dropped():
    """A layer drawn to the true board edge leaves crescents between it and the
    profile's chords: hairlines along the outline, gone from the export; a real
    feature at the edge stays."""
    import shapely
    from pcbfab2step.geometry import gdiff
    B = shapely.box(0, 0, 10, 10)
    g = gdiff(B, shapely.box(0.005, 0, 10, 10))                              # 5 um strip along the edge
    g = shapely.union_all([g, shapely.box(2, 2, 4, 4), shapely.box(9.8, 3, 10, 4)])
    out = exports.drop_edge_slivers(g, B)
    assert out.area == pytest.approx(4.0 + 0.2, abs=1e-4)
