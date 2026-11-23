"""Command line, input discovery and (with OpenCascade) a full conversion."""

import os
import shutil

import pytest

from pcbfab2step.pcbfab2step import main
from pcbfab2step.sources import choose, discover


def test_list_ipc(ipc_file, capsys):
    assert main([ipc_file, "--list"]) == 0
    out = capsys.readouterr().out
    assert "countersink 4.000 mm from top" in out and "IPC-2581 rev B" in out


def test_info_odb(odb_job, capsys):
    assert main(["info", odb_job, "--counterhole-top", "type=sink", "angle=100"]) == 0
    out = capsys.readouterr().out
    assert "countersink 2.000 mm from top" in out and "100 deg" in out


def test_fab_folder_discovery(tmp_path, ipc_file, odb_job):
    fab = tmp_path / "FAB"
    assert main(["init", str(fab)]) == 0
    shutil.copytree(odb_job, fab / "ODB++" / "testjob")
    shutil.copy(ipc_file, fab / "IPC2581" / "board.cvg")
    s = choose(discover([str(fab)]), None, None)
    assert s.primary == "odb" and s.secondary_ipc.endswith("board.cvg")
    assert s.config and s.config.endswith("config.toml")
    s = choose(discover([str(fab)]), "ipc2581", None)
    assert s.primary == "ipc2581" and s.secondary_ipc is None
    s = choose(discover([str(fab)]), None, "off")
    assert s.secondary_ipc is None


def test_same_name_ipc_beside_odb(tmp_path, ipc_file, odb_job):
    shutil.copy(ipc_file, os.path.join(os.path.dirname(odb_job), "testjob.cvg"))
    s = choose(discover([odb_job]), None, None)
    assert s.secondary_ipc and s.secondary_ipc.endswith("testjob.cvg")


def test_convert_step(tmp_path, ipc_file, capsys):
    pytest.importorskip("OCP")
    out = tmp_path / "board.step"
    assert main([ipc_file, "-o", str(out), "-q"]) == 0
    text = out.read_text(encoding="latin-1")
    assert "from IPC-2581 by pcbfab2step" in text
    for name in ("TOP (copper 35.0 um)", "CORE (FR4", "Bottom Solder (solder mask 25.0 um", "Via fill (epoxy)"):
        assert name in text


def test_file_inside_fab_folder(tmp_path, ipc_file, odb_job):
    fab = tmp_path / "FAB"
    main(["init", str(fab)])
    shutil.copytree(odb_job, fab / "ODB++" / "testjob")
    shutil.copy(ipc_file, fab / "IPC2581" / "board.cvg")
    s = choose(discover([str(fab / "ODB++" / "testjob")]), None, None)
    assert s.primary == "odb" and s.secondary_ipc.endswith("board.cvg")
    assert s.config.endswith("config.toml") and s.ncdrill_dirs and s.ncdrill_dirs[0].endswith("NCDRILL")


def test_explicit_input_decides(tmp_path, ipc_file, odb_job):
    # an IPC-2581 file in a folder named like a fab subfolder, next to unrelated ODB++ jobs
    root = tmp_path / "work"
    (root / "ipc").mkdir(parents=True)
    shutil.copy(ipc_file, root / "ipc" / "b.cvg")
    shutil.copytree(odb_job, root / "other" / "testjob")
    s = choose(discover([str(root / "ipc" / "b.cvg")]), None, None)
    assert s.primary == "ipc2581" and s.primary_path.endswith("b.cvg") and s.fab is None


def test_odb_folder_kinds(tmp_path, odb_job):
    from pcbfab2step.sources import kind_of
    wrapped = tmp_path / "ODB-1" / "odb"
    shutil.copytree(odb_job, wrapped)
    assert kind_of(str(tmp_path / "ODB-1")) == "odb"           # archive extracted with a wrapper folder
    fab = tmp_path / "FAB"
    main(["init", str(fab)])
    shutil.copytree(odb_job, fab / "ODB++", dirs_exist_ok=True)  # job extracted straight into ODB++/
    assert kind_of(str(fab)) == "folder"
    s = choose(discover([str(fab)]), None, None)
    assert s.primary == "odb" and os.path.basename(s.primary_path) == "ODB++"


def test_fab_folder_reads_nc_drill(tmp_path, odb_job, capsys):
    """A fab folder with NC drill output: its .DRR depth is used without --ncdrill."""
    fab = tmp_path / "FAB"
    main(["init", str(fab)])
    shutil.copytree(odb_job, fab / "ODB++" / "testjob")
    (fab / "NCDRILL" / "testjob.DRR").write_text(
        "CounterHole : Counterholes Top\nASCII RoundHoles File : testjob.TX2\n\n"
        "Tool       Hole Size     Hole Tolerance   Hole Type   Hole Count   Plated   Tool Travel     Depth     Angle\n"
        "T1      2mm (79mil)                       Round       1            NPTH     0mm (0inch)     0.3mm (11.811mil)   0\n")
    (fab / "NCDRILL" / "testjob.TX2").write_text("M48\n;FILE_FORMAT=4:4\nMETRIC,LZ\nT01F00S00C2.0000\n%\nT01\n"
                                                 "X0010Y0010\nM30\n")
    assert main(["info", str(fab)]) == 0
    out = capsys.readouterr().out
    assert "NCDRILL/ of the fab folder read" in out and "counterbore 2.000 mm from top, depth 0.3000 mm" in out
    assert main(["info", str(fab), "--no-ncdrill"]) == 0
    out = capsys.readouterr().out
    assert "of the fab folder read" not in out and "depth 0.3000 mm" not in out


def test_altium_output_folder_names(tmp_path, ipc_file, odb_job):
    """Altium OutJob folders: 'ODB++/odb' (extracted job), 'IPC-2581', 'NC Drill'."""
    fab = tmp_path / "Outputs"
    shutil.copytree(odb_job, fab / "ODB++" / "odb")
    (fab / "IPC-2581").mkdir()
    shutil.copy(ipc_file, fab / "IPC-2581" / "board.cvg")
    (fab / "NC Drill").mkdir()
    s = choose(discover([str(fab)]), None, None)
    assert s.primary == "odb" and s.primary_path.endswith("odb") and s.secondary_ipc.endswith("board.cvg")
    assert s.ncdrill_dirs and s.ncdrill_dirs[0].endswith("NC Drill")
