# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Surface finish: ENIG on the exposed copper when the fab data has none (as
Altium's exports), the copper's colour by default, a body when it has a
thickness, and the copper whole again when the finish is left out."""

import numpy as np
import pytest

from pcbfab2step import cli, common, scene
from pcbfab2step.pipeline import Job, Session


def _scene(path, *extra):
    args = cli.build_parser().parse_args([str(path), "-q", *extra])
    return scene.model_scene(Job(args, "model", Session()))


def _volume(m) -> float:
    a, b, c = (m.pos[m.tri[:, k]] for k in range(3))
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def _parts(sc, kind, side=None):
    return [p for p in sc.parts if p.kind == kind and (side is None or p.side == side)]


def _vol(sc, kind, side):
    return sum(_volume(p.mesh) for p in _parts(sc, kind, side))


def test_default_finish_colours_the_exposed_copper(ipc_file):
    sc = _scene(ipc_file)
    for side in ("top", "bottom"):
        (f,) = _parts(sc, "finish", side)
        assert "ENIG" in f.name and "copper" in f.name
        assert "finishtop" in f.names or "finishbot" in f.names
    whole = _scene(ipc_file, "--hide", "finish")
    for side in ("top", "bottom"):
        # the finish is the copper under it: together they are the whole copper layer
        assert _vol(sc, "copper", side) + _vol(sc, "finish", side) == pytest.approx(_vol(whole, "copper", side), rel=1e-6)
        assert _vol(sc, "finish", side) < _vol(whole, "copper", side)        # the mask covers the rest
    assert not _parts(whole, "finish")


def test_hiding_one_side_keeps_its_copper_whole(ipc_file):
    common.Log.warnings.clear()
    sc = _scene(ipc_file, "--hide", "finish-top")
    whole = _scene(ipc_file, "--hide", "finish")
    assert not _parts(sc, "finish", "top") and _parts(sc, "finish", "bottom")
    assert _vol(sc, "copper", "top") == pytest.approx(_vol(whole, "copper", "top"), rel=1e-9)
    assert not any("finish" in w for w in common.Log.warnings)


def test_solid_finish_and_settings(ipc_file):
    sc = _scene(ipc_file, "--set", "finish.body=solid")
    whole = _scene(ipc_file, "--hide", "finish")
    (f,) = _parts(sc, "finish", "top")
    assert "4.00 um" in f.name
    assert _vol(sc, "copper", "top") == pytest.approx(_vol(whole, "copper", "top"), rel=1e-9)   # on the copper
    z = f.mesh.pos[:, 2]
    assert z.max() - z.min() == pytest.approx(0.004, abs=1e-9)
    hasl = _scene(ipc_file, "--set", "finish.type=HASL", "--set", "finish.area=all")
    (t,) = _parts(hasl, "finish", "top")
    assert "HASL" in t.name and not _parts(hasl, "copper", "top")      # all of the top copper
    assert tuple(t.color) != tuple(f.color)                             # silver, not gold
