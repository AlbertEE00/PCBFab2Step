# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""NC drill reader (secondary source): Altium's .DRR drill report gives the
counterhole depth and angle per tool, the Excellon drill files the hit
positions. ODB++ has no field for counterhole type, depth or angle; the NC
drill output is one place where Altium writes them (IPC-2581 is another).

The result is a list of CounterholeHint objects for the builder; NC drill is
not a primary source (no outline, no copper)."""

from __future__ import annotations

import io
import os
import re
import tarfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .board import CounterholeHint
from .common import decode_text, name_tokens, warn


_MEAS_RE = re.compile(r"([-+]?(?:\d+\.?\d*|\.\d+))\s*(mm|mils?|inch|in|um)\b(\s*\([^)]*\))?", re.I)
_UNIT_MM = {"mm": 1.0, "mil": 0.0254, "mils": 0.0254, "inch": 25.4, "in": 25.4, "um": 0.001}
NC_ARCHIVES = (".tgz", ".tar.gz", ".tar", ".zip")


@dataclass
class NcCounter:
    """One counterhole tool of an NC drill report."""
    report: str
    section: str
    side: Optional[str]
    tool: int
    d: float
    depth: Optional[float]
    angle: Optional[float]
    count: Optional[int]
    drill_file: str
    pts: Optional[List[Tuple[float, float]]] = None
    used: int = 0

    def label(self) -> str:
        bits = [f"T{self.tool} {self.d:g} mm"]
        if self.depth is not None:
            bits.append(f"depth {self.depth:g} mm")
        if self.angle:
            bits.append(f"angle {self.angle:g} deg")
        return f"{self.report}, {self.section}: " + ", ".join(bits)


def parse_drr(text: str, report: str) -> List[NcCounter]:
    """Counterhole tools of an Altium NC drill report. A tool line reads
    'T2  3.2mm (126mil)  [tolerance]  Round  12  NPTH  <travel>  1.6mm (..)  90°';
    the depth and angle columns follow the tool travel. An angle marks a
    countersink (Altium gives its depth to the cone tip), a depth without
    angle a counterbore."""
    out: List[NcCounter] = []
    section, counter, dfile = "", False, ""
    has_depth = has_angle = False
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"(layer\s*pair|counter\s*holes?|back\s*drill\w*)\s*:\s*(.*)$", s, re.I)
        if m:
            counter = m.group(1).lower().replace(" ", "").startswith("counterhole")
            section, dfile, has_depth, has_angle = m.group(2).strip(), "", False, False
            continue
        m = re.search(r"\bfile\s*:\s*(\S.*?)\s*$", s, re.I)
        if m:
            dfile = m.group(1)
            continue
        if re.match(r"tool\s+hole\s+size\b", s, re.I):
            has_depth = re.search(r"\bdepth\b", s, re.I) is not None
            has_angle = re.search(r"\bangle\b", s, re.I) is not None
            continue
        m = re.match(r"T(\d+)\s+(.+)$", s)
        if not (counter and m):
            continue
        rest = m.group(2)
        ms = _MEAS_RE.search(rest)
        mp = re.search(r"\b(N?PTH)\b", rest, re.I)
        if ms is None or mp is None or mp.start() < ms.end():
            continue
        d = float(ms.group(1)) * _UNIT_MM[ms.group(2).lower()]
        mc = re.search(r"(\d+)\s*$", rest[:mp.start()])
        tail = rest[mp.end():]
        meas = list(_MEAS_RE.finditer(tail))
        depth = angle = None
        if has_depth and len(meas) >= 2:
            depth = float(meas[1].group(1)) * _UNIT_MM[meas[1].group(2).lower()]
        if has_angle:
            ma = re.search(r"([-+]?\d+(?:\.\d*)?)", tail[meas[-1].end():] if meas else tail)
            if ma and 0.0 < float(ma.group(1)) < 180.0:
                angle = float(ma.group(1))
        toks = name_tokens(section)
        side = "top" if "top" in toks else ("bottom" if toks & {"bottom", "bot"} else None)
        out.append(NcCounter(report, section, side, int(m.group(1)), d, depth, angle,
                             int(mc.group(1)) if mc else None, dfile))
    return out


def parse_excellon(text: str) -> Tuple[Dict[int, float], Dict[int, List[Tuple[float, float]]]]:
    """Tool diameters and drill hits (mm) of an Excellon file (absolute
    coordinates; LZ/TZ and ;FILE_FORMAT=i:d honoured; routed slots skipped)."""
    metric, lz, fmt = False, True, None
    diam: Dict[int, float] = {}
    hits: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
    cur, x, y = None, 0.0, 0.0

    def num(v: str) -> float:
        if "." in v:
            val = float(v)
        else:
            sign = -1.0 if v.startswith("-") else 1.0
            dig = v.lstrip("+-")
            a, b = fmt or ((3, 3) if metric else (2, 4))
            val = sign * int(dig.ljust(a + b, "0") if lz else dig) / 10.0 ** b
        return val if metric else val * 25.4

    for raw in text.splitlines():
        s = raw.strip().upper()
        if not s:
            continue
        if s.startswith(";"):
            m = re.search(r"FILE_FORMAT\s*=\s*(\d+)\s*:\s*(\d+)", s)
            if m:
                fmt = (int(m.group(1)), int(m.group(2)))
            continue
        if s.startswith(("METRIC", "INCH")):
            metric = s.startswith("METRIC")
            lz = False if "TZ" in s else (True if "LZ" in s else lz)
            continue
        if s in ("M71", "M72"):
            metric = s == "M71"
            continue
        m = re.match(r"T(\d+)(.*)$", s)
        if m:
            cur = int(m.group(1))
            mc = re.search(r"C([\d.]+)", m.group(2))
            if mc:
                diam[cur] = float(mc.group(1)) * (1.0 if metric else 25.4)
            continue
        if cur is None or "G85" in s or s.startswith(("G00", "G01", "G02", "G03", "M15", "M16")):
            continue
        mx = re.search(r"X([-+]?[\d.]+)", s)
        my = re.search(r"Y([-+]?[\d.]+)", s)
        if mx is None and my is None:
            continue
        if mx:
            x = num(mx.group(1))
        if my:
            y = num(my.group(1))
        hits[cur].append((x, y))
    return diam, hits


class _NcPool:
    """Files that may hold NC drill output: one folder or one archive."""

    def __init__(self, label: str):
        self.label = label
        self.items: Dict[str, Tuple[str, object]] = {}   # lower basename -> (name, path or bytes)

    def add(self, name: str, src):
        self.items.setdefault(os.path.basename(name).lower(), (os.path.basename(name), src))

    def read(self, key: str) -> Optional[str]:
        it = self.items.get(key.lower())
        if it is None:
            return None
        src = it[1]
        if isinstance(src, (bytes, bytearray)):
            return decode_text(bytes(src))
        try:
            with open(src, "rb") as f:
                return decode_text(f.read())
        except OSError:
            return None

    def reports(self) -> List[str]:
        return sorted(k for k in self.items if k.endswith(".drr"))


def _pool_from_path(path: str) -> Optional[_NcPool]:
    if os.path.isdir(path):
        pool = _NcPool(path)
        for fn in os.listdir(path):
            full = os.path.join(path, fn)
            if os.path.isfile(full):
                pool.add(fn, full)
        return pool
    if os.path.isfile(path) and path.lower().endswith(NC_ARCHIVES):
        pool = _NcPool(path)
        data = open(path, "rb").read()
        try:
            if zipfile.is_zipfile(io.BytesIO(data)):
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for zi in z.infolist():
                        if not zi.is_dir():
                            pool.add(zi.filename.replace("\\", "/"), z.read(zi))
            else:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
                    for m in t.getmembers():
                        f = t.extractfile(m) if m.isfile() else None
                        if f is not None:
                            pool.add(m.name, f.read())
        except (tarfile.TarError, zipfile.BadZipFile, OSError):
            return None
        return pool
    return None


def _stem(path: str) -> str:
    base = os.path.basename(os.path.normpath(path)).lower()
    for ext in (".tar.gz",) + tuple(e for e in NC_ARCHIVES if e != ".tar.gz") + (".drr",):
        if base.endswith(ext):
            return base[: -len(ext)]
    return os.path.splitext(base)[0]


def pool_from_files(label: str, files: Dict[str, object]) -> _NcPool:
    """A pool from {name: path or bytes}, e.g. the files inside an ODB++ archive."""
    pool = _NcPool(label)
    for k, v in files.items():
        pool.add(k, v)
    return pool


def find_reports(explicit: List[str], inside: Optional[_NcPool], beside: List[str], stem: str,
                 dedicated: List[str]) -> Tuple[List[Tuple[_NcPool, str]], List[str]]:
    """Choose the NC drill reports to read.

    explicit   paths given with --ncdrill PATH (a .DRR, a folder or an archive): all their reports
    inside     files inside the primary input (an ODB++ archive or folder): all reports
    beside     folders next to the input: the report named like the input, else the only one there
    dedicated  NC drill folders of a fab folder (FAB/NCDRILL): all reports
    """
    notes: List[str] = []
    chosen: List[Tuple[_NcPool, str]] = []
    for p in explicit:
        if os.path.isfile(p) and p.lower().endswith(".drr"):
            pool = _pool_from_path(os.path.dirname(os.path.abspath(p)))
            if pool is not None:
                chosen.append((pool, os.path.basename(p).lower()))
            continue
        pool = _pool_from_path(p)
        if pool is None:
            warn(f"--ncdrill '{p}': not a .DRR report, folder or archive")
            continue
        if not pool.reports():
            warn(f"--ncdrill '{p}': no .DRR report found")
        chosen += [(pool, r) for r in pool.reports()]
    if explicit:
        return chosen, notes
    for d in dedicated:
        pool = _pool_from_path(d)
        if pool is not None:
            chosen += [(pool, r) for r in pool.reports()]
    if inside is not None:
        chosen += [(inside, r) for r in inside.reports()]
    if chosen:
        return chosen, notes
    for d in beside:
        pool = _pool_from_path(d)
        reps = pool.reports() if pool is not None else []
        pick = [r for r in reps if _stem(r) == stem] or [r for r in reps if stem and stem in r]
        if not pick and len(reps) == 1:
            pick = reps
        if reps and not pick:
            notes.append(f"NC drill reports in {d} but none named like the input ("
                         + ", ".join(reps[:5]) + "); give one with --ncdrill PATH")
        chosen += [(pool, r) for r in pick]
        if pick:
            break
    if not chosen:
        notes.append("--ncdrill: no NC drill report (.DRR) found")
    return chosen, notes


def read_hints(chosen: List[Tuple[_NcPool, str]]) -> Tuple[List[CounterholeHint], List[str]]:
    """Counterhole hints from the chosen reports, with hit positions from the
    drill files the reports name (when present)."""
    notes: List[str] = []
    out: List[CounterholeHint] = []
    seen = set()
    for pool, rep in chosen:
        key = (pool.label, rep)
        if key in seen:
            continue
        seen.add(key)
        text = pool.read(rep)
        if text is None:
            continue
        entries = parse_drr(text, pool.items[rep][0])
        found = []
        for e in entries:
            dtext = pool.read(e.drill_file) if e.drill_file else None
            if dtext is None:
                continue
            diam, hits = parse_excellon(dtext)
            if e.tool in hits:
                e.pts = hits[e.tool]
                if e.tool in diam and abs(diam[e.tool] - e.d) < 0.05:
                    e.d = diam[e.tool]
                found.append(e.drill_file)
        for e in entries:
            ctype = "countersink" if e.angle else ("counterbore" if e.depth is not None else "")
            out.append(CounterholeHint(src="NC drill report " + e.label(), side=e.side, d=e.d, type=ctype,
                                       angle=e.angle, depth=None if e.angle else e.depth,
                                       tip_depth=e.depth if e.angle else None, pts=e.pts, count=e.count))
        notes.append(f"NC drill report {pool.items[rep][0]} ({pool.label}): {len(entries)} counterhole tool(s)"
                     + (f", positions from {', '.join(sorted(set(found)))}" if found else
                        (", drill files not found: matched by size and side only" if entries else "")))
    return out, notes
