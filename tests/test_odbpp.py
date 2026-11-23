"""ODB++ reader on a synthetic job and the counterhole precedence chain."""

import pytest

from pcbfab2step import odbpp
from pcbfab2step.board import CounterholeHint
from pcbfab2step.builder import Model


def test_read(odb_job, opts):
    b = odbpp.read(odb_job, opts)
    assert b.name == "testjob" and b.step == "pcb"
    assert b.board_thickness == pytest.approx(1.07)
    L = {L.name: L for L in b.layers}
    assert L["TOP"].t_data == pytest.approx(0.035) and L["CORE"].t_data == pytest.approx(1.0)
    vias = [h for h in b.holes if h.kind == "via"]
    assert len(vias) == 1 and vias[0].r_drill_data == pytest.approx(0.175)
    assert [h.kind for h in b.holes if not h.plated] == ["npth"]
    assert len(b.counterholes) == 1 and b.counterholes[0].type == ""     # ODB++ has no type


def test_default_counterbore_by_span(odb_job, opts):
    m = Model(odbpp.read(odb_job, opts), opts)
    m.prepare()
    st = m.stk
    cb = [c for c in m.cuts if c.kind == "counterbore"]
    assert len(cb) == 1
    assert cb[0].z_lo == pytest.approx(st.stack[-1].z1)          # expose: stops on BOT
    assert any("counterbore assumed" in n for n in m.hole_notes)


def test_setting_beats_hint_beats_default(odb_job, opts):
    b = odbpp.read(odb_job, opts)
    b.hints = [CounterholeHint(src="test hint", side="top", d=2.0, type="countersink", angle=82.0,
                               family="test", pts=[(10.0, 10.0)])]
    m = Model(b, opts)
    m.prepare()
    assert len(m.cones) == 1                                     # hint: countersink 82 deg
    c = m.cones[0]
    assert 2 * __import__("math").degrees(__import__("math").atan((c.r_wide - c.r_narrow) /
                                                                  abs(c.z_wide - c.z_narrow))) == pytest.approx(82.0)
    b = odbpp.read(odb_job, opts)
    b.hints = [CounterholeHint(src="test hint", side="top", d=2.0, type="countersink", angle=82.0,
                               family="test", pts=[(10.0, 10.0)])]
    opts.counterhole["top"]["type"] = ("counterbore", "--counterhole")
    m = Model(b, opts)
    m.prepare()
    assert not m.cones and any(c.kind == "counterbore" for c in m.cuts)


def test_via_settings(odb_job, opts):
    opts.via_fill, opts.via_cap, opts.via_tent = "copper", True, "both"
    opts.sources = {"via_fill": "--via-fill"}
    m = Model(odbpp.read(odb_job, opts), opts)
    m.prepare()
    via = [h for h in m.holes if h.kind == "via"][0]
    assert via.fill == "copper" and via.cap_top > 0 and via.tent_top and via.tent_bot
    npth = [h for h in m.holes if not h.plated][0]
    assert not npth.fill


def test_floor_layer_from_other_format(odb_job, opts):
    from pcbfab2step.builder import canon
    assert canon("Blank 2") == canon("BLANK_2") == canon("_L02__BLANK_2") == "blank2"
    b = odbpp.read(odb_job, opts)
    # an IPC-2581 hint names the floor layer the IPC way ("Core"), ODB++ calls it CORE
    b.hints = [CounterholeHint(src="test hint", side="top", d=2.0, type="counterbore", floor_layer="Co re",
                               family="test", pts=[(10.0, 10.0)])]
    m = Model(b, opts)
    m.prepare()
    cb = [c for c in m.cuts if c.kind == "counterbore"][0]
    assert cb.z_lo == pytest.approx(m.stk.stack[1].z1)            # on CORE's upper face
