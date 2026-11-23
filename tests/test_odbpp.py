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
    assert via.fill == "copper" and via.cap_top > 0 and via.tent_top
    assert not via.tent_bot                                       # no bottom solder mask to tent with
    assert any(n.startswith("tenting bottom") and "no solder mask on this side" in n for n in m.prot_notes)
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


def _write(job, rel, text):
    import os
    with open(os.path.join(job, "steps", "pcb", rel), "w") as f:
        f.write(text)


def test_tent_keeps_pad_openings(odb_job, opts):
    """Tenting closes a via's own mask opening (.pad_usage=via), never a pad's."""
    _write(odb_job, "layers/top_solder/features", "UNITS=MM\n$0 r1000\n$1 rect3000x2000\n@0 .pad_usage\n"
           "P 20 10 0 P 0 0 ;0=1\nP 5 15 1 P 0 0 ;0=0\n")
    _write(odb_job, "layers/drill/features", "UNITS=MM\n$0 r300\n$1 r1000\n"
           "P 20 10 0 P 0 0\nP 10 10 1 P 0 0\nP 5 15 0 P 0 0\n")
    opts.via_tent = "top"
    m = Model(odbpp.read(odb_job, opts), opts)
    m.prepare()
    own = min(m.holes, key=lambda h: (h.x - 20) ** 2 + (h.y - 10) ** 2)
    vip = min(m.holes, key=lambda h: (h.x - 5) ** 2 + (h.y - 15) ** 2)
    assert own.tent_top and not vip.tent_top                     # via in pad stays open
    t = m._tent_region["top"]
    assert t.contains(__import__("shapely").Point(20, 10)) and t.area == pytest.approx(0.785, abs=0.01)
    assert not t.intersects(__import__("shapely").Point(5, 15).buffer(1.0))
    note = [n for n in m.prot_notes if n.startswith("tenting top")][0]
    assert "1 via opening(s) closed" in note and "1 inside the opening of a pad" in note
    assert "from the fab data" in note


def test_tent_by_shape_without_pad_usage(odb_job, opts):
    """No .pad_usage: only a small round opening around one via is the via's own."""
    _write(odb_job, "layers/top_solder/features", "UNITS=MM\n$0 r1000\n$1 rect3000x2000\n$2 r2000\n"
           "P 20 10 0 P 0 0\nP 5 15 1 P 0 0\nP 25 5 2 P 0 0\n")
    _write(odb_job, "layers/drill/features", "UNITS=MM\n$0 r300\n$1 r1000\n"
           "P 20 10 0 P 0 0\nP 10 10 1 P 0 0\nP 5 15 0 P 0 0\nP 25 5 0 P 0 0\n")
    opts.via_tent = "both"
    m = Model(odbpp.read(odb_job, opts), opts)
    m.prepare()
    tented = sorted((h.x, h.y) for h in m.holes if h.plated and h.tent_top)
    assert tented == [(20.0, 10.0)]                              # pad and exposed-copper disc stay open
    assert not any(h.tent_bot for h in m.holes if h.plated)      # no bottom mask layer: nothing to tent
    note = [n for n in m.prot_notes if n.startswith("tenting top")][0]
    assert "1 via opening(s) closed" in note and "2 inside the opening" in note and "by shape" in note


def _add_layer(job, row, name, start, end, add, features, tools=None):
    import os
    p = os.path.join(job, "matrix", "matrix")
    with open(p, "a") as f:
        f.write(f"LAYER {{\n   ROW={row}\n   CONTEXT=BOARD\n   TYPE=DRILL\n   NAME={name}\n   POLARITY=POSITIVE\n"
                f"   START_NAME={start}\n   END_NAME={end}\n   OLD_NAME=\n   ADD_TYPE={add}\n}}\n")
    d = os.path.join(job, "steps", "pcb", "layers", name.lower())
    os.makedirs(d, exist_ok=True)
    for fn, text in (("features", features), ("attrlist", "UNITS=MM\n"), ("tools", tools)):
        if text is not None:
            with open(os.path.join(d, fn), "w") as f:
                f.write(text)


def test_via_type_layer(odb_job, opts):
    """Altium's DRILL_TYPE_7 repeats the type VII vias: their type, no second hole."""
    _add_layer(odb_job, 7, "DRILL_TYPE_7", "TOP", "BOT", "", "UNITS=MM\n$0 r300\nP 20 10 0 P 0 0\nP 25 15 0 P 0 0\n",
               "UNITS=MM\nTOOLS {\nNUM=1\nTYPE=VIA\nFINISH_SIZE=300\nDRILL_SIZE=350\n}\n")
    b = odbpp.read(odb_job, opts)
    vias = sorted((h.x, h.y) for h in b.holes if h.plated)
    assert vias == [(20.0, 10.0), (25.0, 15.0)]                  # (20, 10) once
    assert all(h.fill and h.cap_top and h.cap_bot for h in b.holes if h.plated)
    assert any("type VII (filled and capped) for 2 hole(s), 1 of them only on this layer" in n for n in b.prot_notes)


def test_backdrill_must_not_cut_from_layer_name(odb_job, opts):
    """Altium 26: span TOP..TOP stops short of the must-not-cut layer the name gives (BOT)."""
    _add_layer(odb_job, 7, "DRILL_BACKDRILLS_TOP-BOT", "TOP", "TOP", "BACKDRILL",
               "UNITS=MM\n$0 r500\nP 20 10 0 P 0 0\n",
               "UNITS=MM\nTOOLS {\nNUM=1\nTYPE=NON_PLATED\nFINISH_SIZE=500\nDRILL_SIZE=500\n}\n")
    m = Model(odbpp.read(odb_job, opts), opts)
    m.prepare()
    bd = [c for c in m.cuts if c.kind == "backdrill"]
    assert len(bd) == 1 and bd[0].z_lo == pytest.approx(m.stk.stack[-1].z1)   # up to BOT's upper face
    assert any("must-not-cut layer BOT (named by the layer; its span ends at TOP)" in n for n in m.hole_notes)
