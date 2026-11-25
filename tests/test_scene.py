# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Viewer scenes: meshes from the 2D regions (fast), from the B-rep (exact),
layer views, and the packed format the viewer reads."""

import json
import os

import numpy as np
import pytest
from shapely.geometry import Polygon, box

from pcbfab2step import cli, scene
from pcbfab2step.pipeline import Job, Session


def _job(path, *extra, session=None, mode="model"):
    args = cli.build_parser().parse_args([str(path), "-q", *extra])
    return Job(args, mode, session or Session())


def _volume(m: scene.Mesh) -> float:
    a, b, c = (m.pos[m.tri[:, k]] for k in range(3))
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def _closed(m: scene.Mesh) -> bool:
    """Every directed edge has its reverse: closed and consistently oriented."""
    e = np.vstack([m.tri[:, [0, 1]], m.tri[:, [1, 2]], m.tri[:, [2, 0]]])
    fwd = {tuple(x) for x in e.tolist()}
    return len(fwd) == len(e) and all((b, a) in fwd for a, b in fwd)


def test_prism_mesh_is_closed_and_outward():
    poly = box(0, 0, 10, 6).difference(Polygon([(2, 2), (4, 2), (3, 4)]))
    m = scene.prism_mesh(poly, 0.0, 1.5)
    assert _closed(m)
    assert _volume(m) == pytest.approx(poly.area * 1.5, rel=1e-9)        # positive: faces point out
    assert len(m.edges) == 2 * 7 + 7                                      # rings at both z, 7 vertical corners


def test_disc_mesh_volume():
    m = scene.disc_mesh(1.0, 2.0, 0.5, 0.2, -1.0, 1.0)
    assert _closed(m)
    k = len(m.pos) // 4
    ring = 0.5 * k * np.sin(2 * np.pi / k)                                 # polygon area / r^2
    assert _volume(m) == pytest.approx(ring * (0.5 ** 2 - 0.2 ** 2) * 2.0, rel=1e-9)


def test_fast_scene(ipc_file):
    job = _job(ipc_file)
    sc = scene.model_scene(job)
    kinds = {p.kind for p in sc.parts}
    assert {"copper", "dielectric", "mask", "plating", "fill", "plug", "cap"} <= kinds
    for p in sc.parts:
        assert len(p.mesh.tri) and p.mesh.tri.max() < len(p.mesh.pos)
    man, blob = sc.pack()
    json.dumps(man)
    assert man["triangles"] == sum(len(p.mesh.tri) for p in sc.parts)
    end = 0
    for rec, p in zip(man["parts"], sc.parts):
        off, n = rec["pos"]
        pos = np.frombuffer(blob, "<f4", count=n, offset=off).reshape(-1, 3)
        assert np.allclose(pos, p.mesh.pos, atol=1e-5)
        off, n = rec["tri"]
        assert np.array_equal(np.frombuffer(blob, "<u4", count=n, offset=off).reshape(-1, 3), p.mesh.tri)
        end = max(end, rec["edges"][0] + 4 * rec["edges"][1])
    assert end == len(blob)
    lo, hi = np.array(man["bounds"][:3]), np.array(man["bounds"][3:])
    assert np.all(hi - lo > 0) and hi[0] - lo[0] == pytest.approx(40.0, abs=1e-3)


def _add_paste(odb_job):
    with open(os.path.join(odb_job, "matrix", "matrix"), "a") as f:
        f.write("LAYER {\n   ROW=7\n   CONTEXT=BOARD\n   TYPE=SOLDER_PASTE\n   NAME=TOP_PASTE\n"
                "   POLARITY=POSITIVE\n   START_NAME=\n   END_NAME=\n   OLD_NAME=\n   ADD_TYPE=\n}\n")
    d = os.path.join(odb_job, "steps", "pcb", "layers", "top_paste")
    os.makedirs(d)
    with open(os.path.join(d, "features"), "w") as f:
        f.write("UNITS=MM\n$0 rect2000x1000\nP 5 15 0 P 0 0\n")


def test_paste_always_in_the_scene(odb_job):
    _add_paste(odb_job)
    for extra, hidden in (((), True), (("--show", "paste"), False)):
        sc = scene.model_scene(_job(odb_job, *extra))
        assert "paste" in {p.kind for p in sc.parts}           # built either way, a draw to show
        assert ("paste" in sc.hidden) == hidden


def test_fast_scene_reuses_triangulations(ipc_file, monkeypatch):
    sess = Session()
    scene.model_scene(_job(ipc_file, session=sess))
    calls = []
    real = scene.shapely.constrained_delaunay_triangles
    monkeypatch.setattr(scene.shapely, "constrained_delaunay_triangles", lambda g: calls.append(1) or real(g))
    scene.model_scene(_job(ipc_file, "--color", "mask=black", session=sess))
    assert not calls                                           # same regions: nothing triangulated again


def test_fast_and_exact_agree(ipc_file):
    """The fast meshes hold the volume of the exact solids (the countersink is
    stepped in the fast view, so a little difference there)."""
    pytest.importorskip("OCP")
    sess = Session()
    fast = scene.model_scene(_job(ipc_file, session=sess))
    exact = scene.model_scene(_job(ipc_file, session=sess), exact=True)
    assert exact.mode == "exact" and [p.name for p in fast.parts] == [p.name for p in exact.parts]
    for f, e in zip(fast.parts, exact.parts):
        vf, ve = _volume(f.mesh), _volume(e.mesh)
        assert vf == pytest.approx(ve, rel=0.01), f.name


def test_feature_edges():
    pytest.importorskip("OCP")
    from pcbfab2step.brep import OCC, Brep
    OCC.load()
    m = scene.shape_mesh(Brep.prisms(box(0, 0, 2, 1), 0.0, 1.0))
    assert len(m.tri) == 12 and len(m.pos) == 8
    assert len(m.edges) == 12                                 # the box's edges, no face diagonals


def test_feature_edges_of_touching_bodies():
    """Two blocks stacked face to face (a solder mask's lower and upper part)
    share the triangles where they meet: that face and its seam are inside
    the material and draw no line."""
    def block(z0, z1):
        P = np.array([[x, y, z] for z in (z0, z1) for y in (0, 1) for x in (0, 1)], float)
        q = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
        return P, [(a, b, c) for a, b, c, d in q] + [(a, c, d) for a, b, c, d in q]
    (P1, T1), (P2, T2) = block(0, 1), block(1, 2)
    pos, inv = np.unique(np.vstack([P1, P2]), axis=0, return_inverse=True)
    tri = inv.ravel()[np.vstack([T1, np.array(T2) + 8])]
    e = scene.feature_edges(pos, tri)
    assert len(e) == 16                       # a 1 x 1 x 2 block: its upright edges in two pieces
    assert not (np.abs(pos[e].mean(axis=1)[:, 2] - 1) < 1e-9).any()


def test_layer_scenes(ipc_file):
    sess = Session()
    sc = scene.layer_scene(_job(ipc_file, "--export", "solder-bot:svg", session=sess, mode="export"), "solder-bot:svg")
    assert sc.flat and sc.view == "top" and [p.kind for p in sc.parts] == ["layer", "outline"]
    sc = scene.layer_scene(_job(ipc_file, "--export", "solder-bot:svg", "--no-outline", session=sess, mode="export"),
                           "solder-bot:svg")
    assert [p.kind for p in sc.parts] == ["layer"]
    sc = scene.layer_scene(_job(ipc_file, "--export", "solder-bot:step", "--outline", session=sess, mode="export"),
                           "solder-bot:step")
    assert not sc.flat and sorted(p.kind for p in sc.parts) == ["board", "layer"]
    assert all(_closed(p.mesh) for p in sc.parts)
