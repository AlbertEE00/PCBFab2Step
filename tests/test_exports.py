# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Layer exports (DXF, SVG, STEP) and the model content options."""

import os

import pytest

from pcbfab2step import exports, ipc2581
from pcbfab2step.builder import Model
from pcbfab2step.pcbfab2step import main


def test_parse_spec():
    r = exports.parse_spec("Bottom Paste:step,dxf:invert")
    assert (r.layer, r.formats, r.invert) == ("Bottom Paste", ["step", "dxf"], True)
    assert exports.parse_spec("outline").formats == ["dxf", "svg"]
    with pytest.raises(ValueError):
        exports.parse_spec("outline:pdf")


def test_export_2d(tmp_path, ipc_file):
    src = tmp_path / "board.cvg"
    src.write_bytes(open(ipc_file, "rb").read())
    assert main([str(src), "--no-step", "--export", "top solder:dxf,svg", "outline:dxf",
                 "--export", "Top Solder:svg:invert", "-q"]) == 0
    out = tmp_path / "exports"
    names = sorted(os.listdir(out))
    assert any(n.endswith("Top_Solder.dxf") for n in names) and any(n.endswith("outline.dxf") for n in names)
    inv = [n for n in names if n.endswith("Top_Solder_inverted.svg")]
    assert inv and "evenodd" in (out / inv[0]).read_text()
    dxf = next(out / n for n in names if n.endswith("Top_Solder.dxf")).read_text()
    assert "OUTLINE" in dxf and "TOP_SOLDER" in dxf and "AC1009" in dxf
    assert not (tmp_path / "board.step").exists()                 # --no-step


def test_generic_names_and_errors(tmp_path, ipc_file):
    src = tmp_path / "board.cvg"
    src.write_bytes(open(ipc_file, "rb").read())
    assert main([str(src), "--no-step", "--use-generic-names", "--export", "solder-bot:dxf", "-q"]) == 0
    assert any(n.endswith("solder-bot.dxf") for n in os.listdir(tmp_path / "exports"))
    with pytest.raises(SystemExit, match="generic names need --use-generic-names"):
        main([str(src), "--no-step", "--export", "solder-bot", "-q"])


def test_export_step_with_outline(tmp_path, ipc_file):
    pytest.importorskip("OCP")
    src = tmp_path / "board.cvg"
    src.write_bytes(open(ipc_file, "rb").read())
    assert main([str(src), "--no-step", "--export", "Bottom Solder:step:invert", "--outline",
                 "--stencil-thickness", "0.1", "-q"]) == 0
    p = next((tmp_path / "exports").glob("*Bottom_Solder_inverted.step"))
    text = p.read_text(errors="ignore")
    assert "Board outline" in text and "Bottom Solder inverted (100 um)" in text


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
