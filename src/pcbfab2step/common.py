# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Logging, constants and small helpers shared by the readers and the builder."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Dict, List

EPS_Z = 1e-7          # z comparison tolerance (mm)
OZ_TO_MM = 0.035      # 1 oz/ft2 copper, nominal finished thickness
MIL = 0.0254          # mm per mil

# Built-in defaults, used where neither the command line, the config file
# nor the fab data give a value (mm).
DEFAULTS = dict(copper=0.035, mask=0.020, silk=0.020, finish=0.0, paste=0.120, plating=0.025,
                cap=0.015, board=1.6)
# Plausible ranges for thicknesses read from fab data; values outside are ignored.
VALID_RANGE = dict(mask=(0.005, 0.100), silk=(0.005, 0.100), finish=(0.00005, 0.050), paste=(0.050, 0.300),
                   copper=(0.001, 0.500), dielectric=(0.005, 10.0))


class Log:
    verbose = 1
    warnings: List[str] = []

    @classmethod
    def reset(cls, verbose: int = 1) -> None:
        cls.verbose = verbose
        cls.warnings = []


def info(msg: str, level: int = 1) -> None:
    if Log.verbose >= level:
        print(msg, flush=True)


def warn(msg: str) -> None:
    if msg not in Log.warnings:
        Log.warnings.append(msg)
        if Log.verbose >= 1:
            print("WARNING: " + msg, flush=True)


def name_tokens(name: str) -> set:
    """Lower-case alphanumeric words of a layer or file name."""
    return set(t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t)


def decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


class PointIndex:
    """Grid index for matching points (holes, counterholes, hints) by position."""

    def __init__(self, tol=0.005):
        self.tol = tol
        self.grid: Dict[tuple, list] = defaultdict(list)

    def add(self, x, y, obj):
        self.grid[(int(math.floor(x / self.tol)), int(math.floor(y / self.tol)))].append((x, y, obj))

    def find(self, x, y):
        gx, gy = int(math.floor(x / self.tol)), int(math.floor(y / self.tol))
        best, bd = None, self.tol
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for (px, py, o) in self.grid.get((gx + i, gy + j), ()):
                    d = math.hypot(px - x, py - y)
                    if d <= bd:
                        best, bd = o, d
        return best

    def find_all(self, x, y):
        gx, gy = int(math.floor(x / self.tol)), int(math.floor(y / self.tol))
        out = []
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for (px, py, o) in self.grid.get((gx + i, gy + j), ()):
                    if math.hypot(px - x, py - y) <= self.tol:
                        out.append(o)
        return out
