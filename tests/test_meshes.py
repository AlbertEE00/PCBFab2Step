# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Mesh files (glTF, OBJ, FBX, STL, PLY, 3MF), bodies left out by name,
component exports and the B-rep ring cleanup."""

import json
import struct
import zipfile
import zlib

import numpy as np
import pytest

from pcbfab2step.cli import main


def _src(tmp_path, ipc_file):
    src = tmp_path / "board.cvg"
    src.write_bytes(open(ipc_file, "rb").read())
    return src


def _glb(path):
    data = open(path, "rb").read()
    magic, version, total = struct.unpack_from("<III", data, 0)
    assert magic == 0x46546C67 and version == 2 and total == len(data)
    n, kind = struct.unpack_from("<II", data, 12)
    assert kind == 0x4E4F534A
    doc = json.loads(data[20:20 + n])
    return doc, data


def _fbx_nodes(path):
    """A small reader for the FBX the writer makes: {node name: count}."""
    data = open(path, "rb").read()
    assert data[:23] == b"Kaydara FBX Binary  \x00\x1a\x00"
    assert struct.unpack_from("<I", data, 23)[0] == 7400
    counts = {}

    def prop(o):
        t = chr(data[o])
        o += 1
        if t in "CY":
            return o + (1 if t == "C" else 2)
        if t in "IF":
            return o + 4
        if t in "DL":
            return o + 8
        if t in "SR":
            return o + 4 + struct.unpack_from("<I", data, o)[0]
        n, enc, ln = struct.unpack_from("<III", data, o)
        raw = data[o + 12:o + 12 + ln]
        size = {"d": 8, "f": 4, "i": 4, "l": 8, "b": 1}[t]
        assert len(zlib.decompress(raw) if enc else raw) == n * size
        return o + 12 + ln

    def node(o):
        end, nprop, plen, nlen = struct.unpack_from("<IIIB", data, o)
        if end == 0:
            return None
        name = data[o + 13:o + 13 + nlen].decode()
        counts[name] = counts.get(name, 0) + 1
        p = o + 13 + nlen
        for _ in range(nprop):
            p = prop(p)
        assert p == o + 13 + nlen + plen
        while p < end - 13:
            q = node(p)
            assert q is not None
            p = q
        return end
    o = 27
    while True:
        q = node(o)
        if q is None:
            break
        o = q
    return counts


@pytest.mark.parametrize("fmt", ["glb", "gltf", "obj", "fbx", "stl", "ply", "3mf"])
def test_model_as_mesh(tmp_path, ipc_file, fmt):
    src = _src(tmp_path, ipc_file)
    assert main([str(src), "--format", fmt, "-q"]) == 0
    out = tmp_path / "PCBFab2Step" / f"board.{fmt}"
    assert out.exists() and out.stat().st_size > 1000
    if fmt == "glb":
        doc, _ = _glb(out)
        assert len(doc["meshes"]) >= 8 and doc["nodes"][0]["name"]
        lo = np.min([a["min"] for a in doc["accessors"] if "min" in a], axis=0)
        hi = np.max([a["max"] for a in doc["accessors"] if "max" in a], axis=0)
        assert hi[0] - lo[0] == pytest.approx(0.040, abs=1e-6)            # metres
        assert hi[2] - lo[2] == pytest.approx(0.030, abs=1e-6)            # +Y up: the board's y is -z
    elif fmt == "gltf":
        doc = json.load(open(out))
        assert (tmp_path / "PCBFab2Step" / doc["buffers"][0]["uri"]).stat().st_size == doc["buffers"][0]["byteLength"]
    elif fmt == "obj":
        text = out.read_text()
        assert text.count("\no ") >= 8 and "mtllib board.mtl" in text
        mtl = (tmp_path / "PCBFab2Step" / "board.mtl").read_text()
        assert mtl.count("newmtl") >= 4 and "Pm 0.850" in mtl                 # copper is metal
    elif fmt == "fbx":
        counts = _fbx_nodes(out)
        assert counts["Geometry"] == counts["Model"] >= 8 and counts["Material"] >= 4
    elif fmt == "stl":
        data = out.read_bytes()
        n = struct.unpack_from("<I", data, 80)[0]
        assert len(data) == 84 + 50 * n
    elif fmt == "ply":
        head = out.read_bytes()[:400].decode("ascii", "replace")
        assert head.startswith("ply\nformat binary_little_endian 1.0") and "property uchar red" in head
    elif fmt == "3mf":
        with zipfile.ZipFile(out) as z:
            model = z.read("3D/3dmodel.model").decode()
        assert 'unit="millimeter"' in model and model.count("<object ") >= 8


def test_layer_and_hidden_bodies(tmp_path, ipc_file):
    src = _src(tmp_path, ipc_file)
    assert main([str(src), "--export", "solder-bot:glb,fbx", "-q"]) == 0
    doc, _ = _glb(tmp_path / "PCBFab2Step" / "board_solder-bot.glb")
    assert len(doc["meshes"]) == 1
    out = tmp_path / "x.glb"
    assert main([str(src), "-o", str(out), "--hide", "solder-top,copper-2,PP 1", "-q"]) == 0
    names = {m["name"] for m in _glb(out)[0]["meshes"]}
    assert not any(n.startswith(("Top Solder", "MID", "PP 1")) for n in names)
    assert any(n.startswith("Bottom Solder") for n in names)


def test_hide_names_in_step(tmp_path, ipc_file, capsys):
    pytest.importorskip("OCP")
    src = _src(tmp_path, ipc_file)
    out = tmp_path / "m.step"
    assert main([str(src), "-o", str(out), "--hide", "silk-top,mask-bot,nosuchlayer"]) == 0
    text = out.read_text(errors="ignore")
    assert "'Top Solder" in text and "'Bottom Solder" not in text
    assert "--hide nosuchlayer: no body of that name" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="both shown and hidden"):
        main([str(src), "--hide", "silk-top", "--show", "silk-top"])


def test_mesh_bodies_in_step(tmp_path, ipc_file, capsys):
    """--mesh writes bodies as triangle meshes (AP242 TRIANGULATED_SURFACE_SET);
    the rest stay exact solids, and a CAD reader gets both back."""
    pytest.importorskip("OCP")
    src = _src(tmp_path, ipc_file)
    out = tmp_path / "m.step"
    assert main([str(src), "-o", str(out), "--mesh", "solder-top,copper-bot,nosuchbody"]) == 0
    log = capsys.readouterr().out
    assert "the STEP is AP242" in log and "--mesh nosuchbody: no body of that name" in log
    text = out.read_text(errors="ignore")
    assert "AP242" in text.split("ENDSEC;")[0]
    assert text.count("TRIANGULATED_SURFACE_SET") >= 2 and "ADVANCED_FACE" in text
    from OCP.BRep import BRep_Tool
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    r = STEPControl_Reader()
    r.ReadFile(str(out))
    r.TransferRoots()
    ex = TopExp_Explorer(r.OneShape(), TopAbs_FACE)
    mesh = exact = 0
    while ex.More():
        f = TopoDS.Face(ex.Current())
        if BRep_Tool.Surface_s(f) is None:
            mesh += 1
            assert BRep_Tool.Triangulation_s(f, TopLoc_Location()).NbTriangles() > 0
        else:
            exact += 1
        ex.Next()
    assert mesh >= 2 and exact > 50


def test_layers_stay_whole():
    """A cut that ends inside a layer (back drill, the drill under a countersink,
    a counterbore floor) is a blind hole in that layer, not a split of the
    whole layer at its depth: exact and fast alike."""
    pytest.importorskip("OCP")
    import pathlib
    import tempfile

    from conftest import IPC_XML
    from pcbfab2step import cli, scene
    from pcbfab2step.brep import Brep, bbox
    from pcbfab2step.builder import Cut
    from pcbfab2step.geometry import circle
    from pcbfab2step.pipeline import Job, Session
    p = pathlib.Path(tempfile.mkdtemp()) / "board.cvg"
    p.write_text(IPC_XML, encoding="utf-8")
    m = Job(cli.build_parser().parse_args([str(p), "-q"]), "model", Session()).prepare()
    L = next(L for L in m.stk.stack if L.name == "CORE")
    mid = 0.5 * (L.z0 + L.z1)
    blind = [Cut(circle(20, 15, 0.4), mid, L.z1 + 0.1, "backdrill", False, False, cid=9001),
             Cut(circle(25, 15, 0.3), L.z0 - 0.1, L.z1 + 0.1, "hole", False, False, cid=9002)]
    m.geo = Brep
    solids = m.slab(m.B, L.z0, L.z1, {}, cuts=blind)
    assert len(solids) == 1                                    # one body, the blind hole cut into it
    b = bbox(solids[0])
    assert abs(b[2] - L.z0) < 1e-6 and abs(b[5] - L.z1) < 1e-6
    vol = scene.shape_mesh(solids)
    m.geo = scene.MeshGeo
    fast = scene.realize([m.slab(m.B, L.z0, L.z1, {}, cuts=blind)])[0]

    def v(mm):
        a, b2, c = (mm.pos[mm.tri[:, k]] for k in range(3))
        return float(np.einsum("ij,ij->i", a, np.cross(b2, c)).sum() / 6.0)
    assert v(fast) == pytest.approx(v(vol), rel=1e-3)
    full = m.B.area * (L.z1 - L.z0)
    assert v(fast) == pytest.approx(full - circle(20, 15, 0.4).area * (L.z1 - mid)
                                    - circle(25, 15, 0.3).area * (L.z1 - L.z0), rel=1e-4)


def test_body_names():
    from pcbfab2step.pipeline import body_names, components_side
    names = body_names(dict(kind="mask", side="bottom", layer="Bottom Solder"))
    assert {"mask", "solderbot", "maskbottom", "bottomsolder"} <= set(names)
    assert "copper2" in body_names(dict(kind="copper", side="", layer="MID"), {"MID": 2})
    part = body_names(dict(name="body", path=("Components bottom", "U7")))
    assert {"componentsbot", "u7", "components"} <= set(part)
    assert components_side("components-top") == "top" and components_side("Components_Bottom") == "bottom"
    assert components_side("copper-top") is None


def test_components_export(tmp_path, ipc_file):
    pytest.importorskip("OCP")
    from pcbfab2step.brep import OCC, Brep, write_step
    from shapely.geometry import box
    OCC.load()
    fab = tmp_path / "FAB"
    main(["init", str(fab)])
    (fab / "IPC2581" / "board.cvg").write_bytes(open(ipc_file, "rb").read())
    b = Brep.prisms(box(0, 0, 40, 30), -1.6, 0.0)
    top = Brep.prisms(box(5, 5, 8, 7), 0.0, 1.0)
    bot = Brep.prisms(box(5, 5, 8, 7), -2.6, -1.6)
    write_step(str(fab / "STEP" / "board.step"), "PCB",
               [{"name": "Board", "color": (0, 0.5, 0), "solids": b},
                {"name": "U1", "color": (0.1, 0.1, 0.1), "solids": top},
                {"name": "U2", "color": (0.1, 0.1, 0.1), "solids": bot}])
    ecad = fab / "STEP" / "board.step"
    ecad.write_text(ecad.read_text(encoding="latin-1").replace("by pcbfab2step", "by Altium"), encoding="latin-1")
    assert main([str(fab), "--export", "components-bot:step,glb", "-q"]) == 0
    out = fab / "PCBFab2Step"
    text = next(out.glob("*components-bot.step")).read_text(errors="ignore")
    assert "'U2'" in text and "'U1'" not in text
    doc, _ = _glb(next(out.glob("*components-bot.glb")))
    assert len(doc["meshes"]) == 1
    with pytest.raises(SystemExit, match="STEP or a mesh format"):
        main([str(fab), "--export", "components-top:svg", "-q"])


def test_ring_spike_is_cleaned():
    """A ring that runs out and straight back along itself (GEOS: simple) makes
    an invalid B-rep face; the tip goes, the face is valid."""
    pytest.importorskip("OCP")
    from shapely.geometry import Polygon
    from pcbfab2step.brep import OCC, Brep
    OCC.load()
    from OCP.BRepCheck import BRepCheck_Analyzer
    pts = [(0, 0), (10, 0), (10, 5), (5.0, 5.0), (5.1, 3.0), (4.9998, 5.000004), (0, 5)]
    poly = Polygon(pts)
    assert poly.is_valid
    f = Brep._poly_face(poly, 0.0)
    assert f is not None and BRepCheck_Analyzer(f).IsValid()
    P = Brep.clean_ring(np.asarray(pts, dtype=float))
    assert len(P) == len(pts) - 1


def test_silk_simplify(ipc_file):
    from shapely.geometry import Point
    from pcbfab2step.geometry import simplify
    g = Point(0, 0).buffer(1.0, quad_segs=256)            # 1024 points, 0.006 mm apart
    s = simplify(g, 0.002)
    assert len(s.exterior.coords) < 200 and abs(s.area - g.area) < 0.01
    assert simplify(g, 0) is g
