# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Arc fitting: which polygon runs are rebuilt as exact arcs."""

import math

import numpy as np

from pcbfab2step.brep import _lsq_circle, _uneven
from pcbfab2step.geometry import Tess


def _arc(r, a0, a1, n):
    a = np.linspace(math.radians(a0), math.radians(a1), n)
    return np.column_stack((r * np.cos(a), r * np.sin(a)))


def test_straight_edge_between_short_chords_is_no_arc():
    """A slot side (5.08 mm) between the last and first short chords of its end
    arcs: the four points are co-circular (an isosceles trapezoid), on a
    71 mm circle. Rebuilt as an arc it bowed the side by 45 um."""
    pts = np.array([(25.39822, 29.61594), (25.4, 29.616), (30.48, 29.616), (30.48178, 29.61594)])
    fit = _lsq_circle(pts)
    assert 50 < fit[2] < 100
    assert _uneven(pts, fit, Tess.tol + 5e-4)


def test_real_arcs_are_kept():
    tol = Tess.tol + 5e-4
    fine = _arc(2.0, 0, 30, 25)                 # our tessellation: chords bow well under the tolerance
    assert not _uneven(fine, _lsq_circle(fine), tol)
    coarse = _arc(39.0, 0, 45, 20)              # Altium writes outline arcs as segments bowing ~8 um
    assert not _uneven(coarse, _lsq_circle(coarse), tol)
