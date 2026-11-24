# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Command line, input discovery and (with OpenCascade) a full conversion."""

import os
import shutil

import pytest

from pcbfab2step.cli import main
from pcbfab2step.sources import OUTPUT_DIR, choose, discover, kind_of

DRR = ("CounterHole : Counterholes Top\nASCII RoundHoles File : testjob.TX2\n\n"
       "Tool       Hole Size     Hole Tolerance   Hole Type   Hole Count   Plated   Tool Travel     Depth     Angle\n"
       "T1      2mm (79mil)                       Round       1            NPTH     0mm (0inch)     "
       "0.3mm (11.811mil)   0\n")
TX2 = "M48\n;FILE_FORMAT=4:4\nMETRIC,LZ\nT01F00S00C2.0000\n%\nT01\nX0010Y0010\nM30\n"


def altium_outputs(root, ipc_file, odb_job, drill=True):
    """An Altium OutJob output folder: ODB++/odb, IPC-2581/, NC Drill/."""
    shutil.copytree(odb_job, root / "ODB++" / "odb")
    (root / "IPC-2581").mkdir()
    shutil.copy(ipc_file, root / "IPC-2581" / "board.cvg")
    if drill:
        (root / "NC Drill").mkdir()
        (root / "NC Drill" / "testjob.DRR").write_text(DRR)
        (root / "NC Drill" / "testjob.TX2").write_text(TX2)
    return root


def test_info_ipc(ipc_file, capsys):
    assert main(["info", ipc_file]) == 0
    out = capsys.readouterr().out
    assert "countersink 4.000 mm from top" in out and "IPC-2581 rev B" in out and "Export targets" in out


def test_info_odb_counterhole(odb_job, capsys):
    assert main(["info", odb_job, "--counterhole-top", "type=sink", "angle=100"]) == 0
    out = capsys.readouterr().out
    assert "countersink 2.000 mm from top" in out and "100 deg" in out


def test_discovery_any_layout(tmp_path, ipc_file, odb_job):
    fab = altium_outputs(tmp_path / "Outputs", ipc_file, odb_job)
    s = choose(discover([str(fab)]), None)
    assert s.primary == "odb" and s.primary_path.endswith("odb") and s.ipc_hints.endswith("board.cvg")
    assert s.drill and s.drill[0].endswith("NC Drill")
    assert s.out_dir == str(fab / OUTPUT_DIR)
    s = choose(discover([str(fab)]), "ipc+drill")
    assert s.primary == "ipc" and s.ipc_hints is None and s.odb_types is None and s.drill
    s = choose(discover([str(fab)]), "ipc+odb")
    assert s.primary == "ipc" and s.odb_types.endswith("odb") and not s.drill
    with pytest.raises(SystemExit, match="no STEP found"):
        choose(discover([str(fab)]), "odb+step")


def test_file_brings_its_siblings(tmp_path, ipc_file, odb_job):
    """A file in an output folder also finds the outputs beside that folder; results go beside the file."""
    fab = altium_outputs(tmp_path / "FAB", ipc_file, odb_job)
    s = choose(discover([str(fab / "IPC-2581" / "board.cvg")]), None)
    assert s.primary == "ipc" and s.odb_types and s.drill                  # the IPC-2581 file given decides
    assert s.out_dir == str(fab / "IPC-2581" / OUTPUT_DIR)


def test_flat_folder_and_own_output_skipped(tmp_path, ipc_file):
    flat = tmp_path / "flat"
    flat.mkdir()
    shutil.copy(ipc_file, flat / "board.cvg")
    (flat / OUTPUT_DIR).mkdir()
    (flat / OUTPUT_DIR / "board.step").write_text("ISO-10303-21;")       # our own output: never an input
    s = choose(discover([str(flat)]), None)
    assert s.primary == "ipc" and s.step is None


def test_odb_job_folder_kinds(tmp_path, odb_job):
    assert kind_of(odb_job) == "odb"
    wrapped = tmp_path / "ODB-1" / "odb"
    shutil.copytree(odb_job, wrapped)
    assert kind_of(str(tmp_path / "ODB-1")) == "folder"
    s = choose(discover([str(tmp_path / "ODB-1")]), None)              # found below the folder given
    assert s.primary == "odb" and s.primary_path.endswith("odb")


def test_nc_drill_found_and_left_out(tmp_path, ipc_file, odb_job, capsys):
    fab = altium_outputs(tmp_path / "FAB", ipc_file, odb_job)
    assert main(["info", str(fab)]) == 0
    out = capsys.readouterr().out
    assert "counterbore 2.000 mm from top, depth 0.3000 mm" in out
    assert main(["info", str(fab), "--sources", "odb+ipc"]) == 0
    assert "depth 0.3000 mm" not in capsys.readouterr().out


def test_convert_step(tmp_path, ipc_file):
    pytest.importorskip("OCP")
    out = tmp_path / "board.step"
    assert main([ipc_file, "-o", str(out), "-q"]) == 0
    text = out.read_text(encoding="latin-1")
    assert "from IPC-2581 by pcbfab2step" in text
    for name in ("TOP (copper 35.0 um)", "CORE (FR4", "Bottom Solder (solder mask 25.0 um", "Via fill (epoxy)"):
        assert name in text


def test_default_output_folder(tmp_path, ipc_file):
    pytest.importorskip("OCP")
    (tmp_path / "in").mkdir()
    src = tmp_path / "in" / "board.cvg"
    shutil.copy(ipc_file, src)
    assert main([str(src), "-q", "--sanitize"]) == 0
    assert any(n.endswith("_sanitized-exposed.step") for n in os.listdir(tmp_path / "in" / OUTPUT_DIR))


def test_one_output_per_call(ipc_file):
    with pytest.raises(SystemExit, match="acts on the board model"):
        main([ipc_file, "--export", "outline:dxf", "--show", "paste", "-q"])
    with pytest.raises(SystemExit, match="only with --export"):
        main([ipc_file, "--no-outline", "-q"])


def test_settings_from_the_command_line(ipc_file, capsys):
    assert main(["info", ipc_file, "--thickness", "mask=1mil,TOP=0.07", "--color", "mask=black",
                 "--set", "finish.type=ENIG", "--hide", "silk"]) == 0
    out = capsys.readouterr().out
    assert "0.0254" in out or "25.4 um" in out
    with pytest.raises(SystemExit, match="did you mean"):
        main(["info", ipc_file, "--set", "soldermask.colour=black"])


def test_settings_command(capsys):
    assert main(["settings"]) == 0
    out = capsys.readouterr().out
    assert "[export]" in out and "stencil_thickness" in out and "[model]" in out
