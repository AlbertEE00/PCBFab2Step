# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""IPC-2581 reader on a synthetic board."""

import math

import pytest

from pcbfab2step import ipc2581
from pcbfab2step.builder import Model


def test_layers_and_stackup(ipc_file, opts):
    b = ipc2581.read(ipc_file, opts)
    kinds = {L.name: L.kind for L in b.layers}
    assert kinds["TOP"] == "copper" and kinds["CORE"] == "dielectric" and kinds["Top Solder"] == "mask"
    assert kinds["Top Overlay"] == "silk" and kinds["Drill Guide (TOP - BOT)"] == "drill"
    L = {L.name: L for L in b.layers}
    assert L["CORE"].t_data == pytest.approx(0.8) and L["CORE"].material == "FR4" and L["CORE"].dk == 4.3
    assert L["PP 1"].dielectric_type == "PREPREG" and L["CORE"].dielectric_type == "CORE"
    assert L["Top Solder"].side_hint == "top" and L["Bottom Solder"].side_hint == "bottom"
    assert L["Top Overlay"].t_data is None           # 0 in the stackup: not given
    assert b.board_thickness is None                  # overallThickness includes the mask (OTHER)
    assert b.outline.area == pytest.approx(1200.0)    # 40 x 30, datum removed
    assert b.outline.bounds == pytest.approx((0.0, 0.0, 40.0, 30.0))


def test_holes_backdrill_counterholes(ipc_file, opts):
    b = ipc2581.read(ipc_file, opts)
    assert sorted((h.kind, round(2 * h.r_fin, 3)) for h in b.holes) == [
        ("npth", 2.0), ("npth", 2.0), ("via", 0.3), ("via", 0.3)]
    assert len(b.backdrills) == 1
    bd = b.backdrills[0]
    assert (bd.mnc, bd.dir_hint, bd.stub) == ("MID", "top", None)   # MAX_STUB_LENGTH is Altium's depth
    assert (bd.x, bd.y) == pytest.approx((15.0, 10.0))
    cs = {c.side: c for c in b.counterholes}
    assert cs["top"].type == "countersink" and cs["top"].angle == pytest.approx(90.0, abs=0.05)
    assert cs["top"].d == pytest.approx(4.0)
    assert cs["bottom"].type == "counterbore" and cs["bottom"].floor_layer == "TOP"
    assert cs["bottom"].depth_range == pytest.approx((0.435, 1.27))


def test_hints(ipc_file):
    hints, notes = ipc2581.read_hints(ipc_file)
    assert {(q.side, q.type) for q in hints} == {("top", "countersink"), ("bottom", "counterbore")}
    assert all(q.family == "IPC-2581" and len(q.pts) == 1 for q in hints)
    assert any("countersink" in n for n in notes)


def test_images_rotation_and_cutouts(ipc_file, opts):
    b = ipc2581.read(ipc_file, opts)
    L = {L.name: L for L in b.layers}
    top = L["TOP"].raw_image()
    # pour 36 x 8 + round-ended 6 x 0.2 trace + via pad (d 1) + 1 x 2 pad turned 90 degrees
    pad = [p for p in top.geoms if p.centroid.distance(__import__("shapely").geometry.Point(10, 5)) < 0.01][0] \
        if top.geom_type == "MultiPolygon" else None
    assert pad is not None
    x0, y0, x1, y1 = pad.bounds
    assert (x1 - x0, y1 - y0) == pytest.approx((2.0, 1.0), abs=1e-6)   # rotated counterclockwise by 90
    mid = L["MID"].raw_image()
    assert mid.area == pytest.approx(36 * 26 - math.pi * 25, rel=1e-3)
    assert L["Top Overlay"].empty


def test_model_prepare(ipc_file, opts):
    b = ipc2581.read(ipc_file, opts)
    m = Model(b, opts)
    m.prepare()
    st = m.stk
    assert st.total == pytest.approx(1.305)
    assert [L.name for L in st.stack] == ["TOP", "CORE", "MID", "PP 1", "BOT"]
    bd = [c for c in m.cuts if c.kind == "backdrill"]
    assert len(bd) == 1 and bd[0].z_lo == pytest.approx(st.stack[2].z1)    # up to, not into, MID
    via = min(m.holes, key=lambda h: (h.x - 15) ** 2 + (h.y - 10) ** 2)
    assert via.z_hi_eff == pytest.approx(st.stack[2].z1)                    # plating ends at MID
    cb = [c for c in m.cuts if c.kind == "counterbore"]
    assert len(cb) == 1 and cb[0].z_hi == pytest.approx(st.stack[0].z0)   # floor on TOP's lower face
    assert len(m.cones) == 1 and m.cones[0].side == "top"
    cone = m.cones[0]
    assert (cone.r_wide - cone.r_narrow) / abs(cone.z_wide - cone.z_narrow) == pytest.approx(1.0)
    filled = [h for h in m.holes if h.fill]
    assert len(filled) == 1 and (filled[0].x, filled[0].y) == pytest.approx((15.0, 10.0))   # padstack marker
    v = filled[0]
    assert v.cap_top > 0 and v.cap_bot > 0                       # capped both sides, back drill or not
    assert v.bores["top"] == pytest.approx((0.25, st.stack[2].z1))  # fill closes the back drill bore
    assert not bd[0].pierce_top and bd[0].hole is v              # coatings cover the filled bore
    L = {L.name: L for L in b.layers}
    assert "padstacks" in L["Filling"].points_note and "ignored" in L["Filling"].points_note


def test_settings_override_fab_data(ipc_file, opts):
    opts.counterhole["top"]["type"] = ("counterbore", "--counterhole-top")
    opts.counterhole["top"]["depth"] = (0.3, "--counterhole-top")
    b = ipc2581.read(ipc_file, opts)
    m = Model(b, opts)
    m.prepare()
    assert not m.cones
    cb = sorted((c for c in m.cuts if c.kind == "counterbore"), key=lambda c: c.z_lo)
    top = [c for c in cb if c.pierce_top][0]
    assert top.z_hi - top.z_lo == pytest.approx(0.3)
    assert any("from --counterhole-top" in n for n in m.hole_notes)


def test_open_backdrill_without_fill(ipc_file, opts):
    opts.via_fill = "none"                       # --via-fill none overrides the markers
    opts.sources = {"via_fill": "--via-fill"}
    m = Model(ipc2581.read(ipc_file, opts), opts)
    m.prepare()
    v = min(m.holes, key=lambda h: (h.x - 15) ** 2 + (h.y - 10) ** 2)
    bd = [c for c in m.cuts if c.kind == "backdrill"][0]
    assert not v.fill and not v.bores and v.cap_top == 0 and v.cap_bot > 0   # open bore ends the top cap
    assert bd.pierce_top


def test_fill_and_caps_in_step(ipc_file, tmp_path):
    pytest.importorskip("OCP")
    from pcbfab2step.pcbfab2step import main
    out = tmp_path / "b.step"
    assert main([ipc_file, "-o", str(out), "-q"]) == 0
    text = out.read_text(encoding="latin-1")
    assert "Via fill (epoxy)" in text and "Via caps (15 um)" in text


def test_drawn_markers_without_pads(ipc_file, opts):
    m = Model(ipc2581.read(ipc_file, opts), opts)
    m.prepare()
    v = min(m.holes, key=lambda h: (h.x - 25) ** 2 + (h.y - 20) ** 2)     # file (35, 30)
    assert v.plug_bot > 0 and v.plug_top == 0 and not v.fill
    assert any("Plugging Bottom: plug applied to 1 hole(s) (bottom)" in n for n in m.prot_notes)


def test_tent_closes_only_via_openings(ipc_file, opts):
    """Via padstack pads on the mask are the vias' own openings; a via inside a
    drawn opening stays open (the artwork asks for no mask there)."""
    from shapely import Point
    opts.via_tent = "both"
    m = Model(ipc2581.read(ipc_file, opts), opts)
    m.prepare()
    v1 = min(m.holes, key=lambda h: (h.x - 15) ** 2 + (h.y - 10) ** 2)    # file (25, 20): drawn opening
    v2 = min(m.holes, key=lambda h: (h.x - 25) ** 2 + (h.y - 20) ** 2)    # file (35, 30): via padstack pad
    assert not v1.tent_bot and v2.tent_bot and v1.tent_top and v2.tent_top
    t = m._tent_region["bottom"]
    assert t.contains(Point(25, 20)) and not t.intersects(Point(15, 10).buffer(0.5))
    assert t.area == pytest.approx(0.385, abs=0.01)                          # the 0.7 mm via opening
    notes = {n.split(":")[0]: n for n in m.prot_notes if n.startswith("tenting ")}
    assert "1 via opening(s) closed" in notes["tenting bottom"] and "1 inside the opening" in notes["tenting bottom"]
    assert "2 already covered by the mask data" in notes["tenting top"]
