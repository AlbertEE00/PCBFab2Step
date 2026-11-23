# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""ODB++ reader: product model (folder, .tgz/.tar or .zip) -> Board.

Parses the matrix, layer attributes, feature files (all standard symbols,
user symbols, surfaces, text, barcodes), step-and-repeat, drill tools,
counterhole / back drill / dual-diameter drill layers and the via-protection
attributes. ODB++ has no counterhole type, depth or angle; the builder takes
them from the settings, secondary sources (IPC-2581, NC drill) or defaults."""

from __future__ import annotations

import gzip
import io
import math
import os
import re
import tarfile
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from shapely import affinity
from shapely.geometry import Polygon, box

from .board import COUNTER_TOKENS, Backdrill, Board, Counterhole, DualDrill, Hole, Layer, RoutCut, apply_ipc
from .common import MIL, OZ_TO_MM, decode_text, info, name_tokens, warn
from .geometry import (EMPTY, IDENT, M_MIRROR_X, _ellipse, _oval, _poly, _rect_shape, _thermal, arc_centerline,
                       arc_pts, circle, contour_parents, gdiff, ginter, m_apply, m_apply_pt, m_compose, m_rot_cw,
                       m_translate, make_valid, orient_matrix, polys_only, square_line, stadium, thick_arc,
                       union_all)


def unlzw(data: bytes) -> bytes:
    if len(data) < 3 or data[0] != 0x1F or data[1] != 0x9D:
        raise ValueError("not a .Z (compress) stream")
    flags = data[2]
    maxbits = flags & 0x1F
    block = bool(flags & 0x80)
    if maxbits < 9 or maxbits > 16:
        raise ValueError("unsupported .Z code size")
    n = len(data)
    pos = mark = 3
    bits, mask = 9, 0x1FF
    end = 256 if block else 255
    prefix = [0] * 65536
    suffix = [0] * 65536
    if pos >= n:
        return b""
    buf = data[pos]
    pos += 1
    if pos >= n:
        raise ValueError("truncated .Z stream")
    buf += data[pos] << 8
    pos += 1
    final = prev = buf & mask
    buf >>= bits
    left = 16 - bits
    if prev > 255:
        raise ValueError("invalid .Z stream")
    out = bytearray([final])
    while True:
        if end >= mask and bits < maxbits:
            rem = (pos - mark) % bits
            if rem:
                pos += bits - rem
            if pos > n:
                break
            buf = left = 0
            mark = pos
            bits += 1
            mask = (mask << 1) | 1
        if pos >= n:
            break
        buf += data[pos] << left
        pos += 1
        left += 8
        if left < bits:
            if pos >= n:
                break
            buf += data[pos] << left
            pos += 1
            left += 8
        code = buf & mask
        buf >>= bits
        left -= bits
        if code == 256 and block:
            rem = (pos - mark) % bits
            if rem:
                pos += bits - rem
            buf = left = 0
            mark = pos
            bits, mask, end = 9, 0x1FF, 255
            continue
        temp = code
        stack = []
        if code > end:
            if code != end + 1 or prev > end:
                raise ValueError("invalid .Z code")
            stack.append(final)
            code = prev
        while code >= 256:
            stack.append(suffix[code])
            code = prefix[code]
        stack.append(code)
        final = code
        if end < mask:
            end += 1
            prefix[end] = prev
            suffix[end] = final
        prev = temp
        stack.reverse()
        out.extend(stack)
    return bytes(out)


class OdbJob:
    def __init__(self, path: str):
        self.path = path
        self.files: Dict[str, bytes] = {}      # lower-case rel path -> data (archives)
        self.dir_index: Dict[str, str] = {}    # lower-case rel path -> real path (folders)
        self.root = ""
        if os.path.isdir(path):
            for base, _dirs, fnames in os.walk(path):
                for fn in fnames:
                    full = os.path.join(base, fn)
                    rel = os.path.relpath(full, path).replace(os.sep, "/")
                    self.dir_index[rel.lower()] = full
        else:
            self._load_archive(path, open(path, "rb").read(), depth=0)
        self._find_root()
        self.name = self._job_name()

    # .. archive handling ...................................................
    def _load_archive(self, name: str, data: bytes, depth: int):
        low = name.lower()
        members: Dict[str, bytes] = {}
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for zi in z.infolist():
                    if zi.is_dir():
                        continue
                    members[zi.filename.replace("\\", "/")] = z.read(zi)
        else:
            try:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
                    for m in t.getmembers():
                        if not m.isfile():
                            continue
                        f = t.extractfile(m)
                        if f is not None:
                            members[m.name.replace("\\", "/")] = f.read()
            except tarfile.TarError:
                if low.endswith(".gz") or data[:2] == b"\x1f\x8b":
                    return self._load_archive(name[:-3], gzip.decompress(data), depth + 1)
                raise SystemExit(f"cannot read '{name}': not a folder, zip or tar archive")
        has_matrix = any(re.search(r"(^|/)matrix/matrix(\.z|\.gz)?$", k.lower()) for k in members)
        if not has_matrix and depth < 3:
            inner = [k for k in members if re.search(r"\.(tgz|tar|tar\.gz|zip|gz)$", k.lower())]
            for k in inner:
                try:
                    self._load_archive(k, members[k], depth + 1)
                except SystemExit:
                    continue
                if self.files:
                    return
        for k, v in members.items():
            k = k.lstrip("./").lstrip("/")
            self.files[k.lower()] = v

    def _all_paths(self) -> List[str]:
        return list(self.files.keys()) if self.files else list(self.dir_index.keys())

    def _find_root(self):
        cands = []
        for p in self._all_paths():
            m = re.match(r"^(.*?)(?:^|/)?matrix/matrix(\.z|\.gz)?$", p)
            if m and p.endswith(("matrix/matrix", "matrix/matrix.z", "matrix/matrix.gz")):
                root = p[: p.rfind("matrix/matrix")]
                cands.append(root)
        if not cands:
            raise SystemExit("no ODB++ product model found (matrix/matrix is missing)")
        self.root = min(cands, key=len)

    def _job_name(self) -> str:
        base = os.path.basename(os.path.normpath(self.path))
        for ext in (".tgz", ".tar.gz", ".tar", ".zip", ".gz"):
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        base = re.sub(r"^[0-9a-f]{8}-", "", base)  # upload prefixes
        return base or "odb"

    # .. file access ..........................................................
    def raw(self, rel: str) -> Optional[bytes]:
        rel = (self.root + rel).lower()
        for suffix in ("", ".z", ".gz"):
            key = rel + suffix
            data = None
            if self.files:
                data = self.files.get(key)
            elif key in self.dir_index:
                with open(self.dir_index[key], "rb") as f:
                    data = f.read()
            if data is None:
                continue
            if suffix == ".z" or data[:2] == b"\x1f\x9d":
                data = unlzw(data)
            elif suffix == ".gz" or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data
        return None

    def text(self, rel: str) -> Optional[str]:
        data = self.raw(rel)
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")

    def exists(self, rel: str) -> bool:
        rel = (self.root + rel).lower()
        keys = self.files if self.files else self.dir_index
        return any((rel + s) in keys for s in ("", ".z", ".gz"))

    def listdir(self, rel: str) -> List[str]:
        rel = (self.root + rel).lower().rstrip("/") + "/"
        out = set()
        for p in self._all_paths():
            if p.startswith(rel):
                rest = p[len(rel):]
                if rest:
                    out.add(rest.split("/")[0])
        return sorted(out)


def parse_structured(text: str):
    """KEY=VALUE lines and NAME { ... } blocks -> (top dict, [(NAME, dict)])."""
    top: Dict[str, str] = {}
    arrays: List[Tuple[str, Dict[str, str]]] = []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("{"):
            cur = (line[:-1].strip().upper(), {})
            continue
        if line.startswith("}"):
            if cur is not None:
                arrays.append(cur)
            cur = None
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            (cur[1] if cur is not None else top)[k.strip().upper()] = v.strip()
    return top, arrays


def parse_attrlist(text: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not text:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().lower()
        v = v.strip().strip('"')
        if k == "units":
            out["units"] = v.upper()
            continue
        if v or k not in out:
            out[k] = v
    return out


def attr_float(attrs: Dict[str, str], key: str) -> Optional[float]:
    v = attrs.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


# Altium writes the vias of each IPC-4761 type to a drill layer of their own too
# (DRILL_TYPE_7: the type VII vias, also on the regular drill layer)
_VIA_TYPE_RE = re.compile(r"^(?:drill|via)s?_?type_?([1-7]|iv|v?i{1,3}|v)([ab])?$", re.I)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7}
IPC_TYPE_TEXT = {1: "tented", 2: "tented and covered", 3: "plugged", 4: "plugged and covered", 5: "filled",
                 6: "filled and covered", 7: "filled and capped"}


def via_type_layer(name: str) -> Optional[Tuple[int, str]]:
    """'DRILL_TYPE_7' -> (7, ''), 'drill_type_1b' -> (1, 'b'), else None."""
    m = _VIA_TYPE_RE.match(name.strip())
    if not m:
        return None
    t = m.group(1).lower()
    return (int(t) if t.isdigit() else _ROMAN[t]), (m.group(2) or "").lower()


def _roman(n: int) -> str:
    return {v: k for k, v in _ROMAN.items()}[n].upper()


# ODB++ option attributes are stored in feature files as the option index
OPTION_ATTRS = {
    ".drill": ["plated", "non_plated", "via"],
    ".pad_usage": ["toeprint", "via", "g_fiducial", "l_fiducial", "tooling_hole", "bond_finger"],
    ".via_type": ["drilled", "laser", "photo"],
    ".plated_type": ["standard", "press_fit"],
    ".comp": ["none", "right", "left"],
    ".drill_layer_direction": ["top2bottom", "bottom2top", "any"],
}
TEXT_ATTRS = {".geometry", ".ipc_via_type_top", ".ipc_via_type_bottom", ".bit", ".string",
              ".net_name", ".comp_name", ".pin_name", ".layer_name", ".feature_type"}


@dataclass
class Rec:
    kind: str                 # L P A T B S
    pol: str = "P"
    data: tuple = ()
    attrs: Optional[Dict[str, object]] = None
    polys: Optional[list] = None   # surfaces: [(is_island, [(op, args)...]), ...]


@dataclass
class FeatureFile:
    units: str = "INCH"
    symbols: Dict[int, Tuple[str, Optional[str]]] = field(default_factory=dict)
    attr_names: Dict[int, str] = field(default_factory=dict)
    attr_texts: Dict[int, str] = field(default_factory=dict)
    records: List[Rec] = field(default_factory=list)

    @property
    def cs(self) -> float:          # coordinate scale to mm
        return 25.4 if self.units.startswith("INCH") else 1.0

    def ss(self, sym_unit: Optional[str]) -> float:   # symbol size scale to mm
        if sym_unit == "I":
            return MIL
        if sym_unit == "M":
            return 0.001
        return MIL if self.units.startswith("INCH") else 0.001


_TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\S+")


def _parse_feature_attrs(s: str, ff: FeatureFile) -> Dict[str, object]:
    out: Dict[str, object] = {}
    s = s.strip()
    if not s:
        return out
    for part in s.split(";"):
        for tok in part.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if tok.upper().startswith("ID="):
                continue
            if "=" in tok:
                k, v = tok.split("=", 1)
            else:
                k, v = tok, None
            try:
                name = ff.attr_names.get(int(k), k)
            except ValueError:
                name = k
            name = name.lower()
            if v is None:
                out[name] = True
            elif name in OPTION_ATTRS:
                try:
                    out[name] = OPTION_ATTRS[name][int(v)]
                except (ValueError, IndexError):
                    out[name] = v.lower()
            elif name in TEXT_ATTRS:
                try:
                    out[name] = ff.attr_texts.get(int(v), v)
                except ValueError:
                    out[name] = v
            else:
                out[name] = v
    return out


def parse_features(text: Optional[str], default_units: str = "INCH") -> FeatureFile:
    ff = FeatureFile(units=default_units)
    if not text:
        return ff
    surf: Optional[Rec] = None
    cur_poly = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        c0 = line[0]
        if c0 == "$":
            parts = line[1:].split()
            if len(parts) >= 2:
                unit = parts[2].upper() if len(parts) >= 3 and parts[2].upper() in ("I", "M") else None
                ff.symbols[int(parts[0])] = (parts[1], unit)
            continue
        if c0 == "@":
            parts = line[1:].split(None, 1)
            if len(parts) == 2:
                ff.attr_names[int(parts[0])] = parts[1].strip()
            continue
        if c0 == "&":
            parts = line[1:].split(None, 1)
            ff.attr_texts[int(parts[0])] = parts[1].strip() if len(parts) == 2 else ""
            continue
        up = line.upper()
        if up.startswith("UNITS="):
            ff.units = "MM" if "MM" in up else "INCH"
            continue
        if up.startswith("U ") and len(line) < 12:
            ff.units = "MM" if "MM" in up else "INCH"
            continue
        if up.startswith("ID=") or up.startswith("F "):
            continue
        geo, _, atr = line.partition(";")
        tok = geo.split()
        if not tok:
            continue
        k = tok[0].upper()
        try:
            if k == "OB" and surf is not None:
                cur_poly = (tok[3].upper() != "H" if len(tok) > 3 else True,
                            [("S", (float(tok[1]), float(tok[2])))])
                surf.polys.append(cur_poly)
            elif k == "OS" and cur_poly is not None:
                cur_poly[1].append(("L", (float(tok[1]), float(tok[2]))))
            elif k == "OC" and cur_poly is not None:
                cw = tok[5].upper().startswith("Y") if len(tok) > 5 else True
                cur_poly[1].append(("A", (float(tok[1]), float(tok[2]), float(tok[3]), float(tok[4]), cw)))
            elif k == "OE":
                cur_poly = None
            elif k == "SE":
                if surf is not None:
                    ff.records.append(surf)
                surf = None
                cur_poly = None
            elif k == "S":
                surf = Rec("S", tok[1].upper() if len(tok) > 1 else "P", (), _parse_feature_attrs(atr, ff), [])
            elif k == "L":
                ff.records.append(Rec("L", tok[6].upper(),
                                      (float(tok[1]), float(tok[2]), float(tok[3]), float(tok[4]), int(tok[5])),
                                      _parse_feature_attrs(atr, ff)))
            elif k == "A":
                cw = tok[10].upper().startswith("Y") if len(tok) > 10 else True
                ff.records.append(Rec("A", tok[8].upper(),
                                      (float(tok[1]), float(tok[2]), float(tok[3]), float(tok[4]),
                                       float(tok[5]), float(tok[6]), int(tok[7]), cw),
                                      _parse_feature_attrs(atr, ff)))
            elif k == "P":
                if tok[3] == "-1":
                    sym, resize = int(tok[4]), float(tok[5])
                    i = 6
                else:
                    sym, resize = int(tok[3]), 0.0
                    i = 4
                pol = tok[i].upper()
                orient_def = int(tok[i + 2]) if len(tok) > i + 2 else 0
                angle = float(tok[i + 3]) if orient_def in (8, 9) and len(tok) > i + 3 else 0.0
                ff.records.append(Rec("P", pol, (float(tok[1]), float(tok[2]), sym, resize, orient_def, angle),
                                      _parse_feature_attrs(atr, ff)))
            elif k == "T":
                t = _TOKEN_RE.findall(geo)
                x, y, font, pol, od = float(t[1]), float(t[2]), t[3], t[4].upper(), int(t[5])
                i = 6
                angle = 0.0
                if od in (8, 9):
                    angle = float(t[6])
                    i = 7
                xs, ys, wf = float(t[i]), float(t[i + 1]), float(t[i + 2])
                txt = t[i + 3]
                if txt[:1] in "'\"":
                    txt = txt[1:-1]
                ver = int(t[i + 4]) if len(t) > i + 4 and t[i + 4].isdigit() else 0
                ff.records.append(Rec("T", pol, (x, y, font, od, angle, xs, ys, wf, txt, ver),
                                      _parse_feature_attrs(atr, ff)))
            elif k == "B":
                t = _TOKEN_RE.findall(geo)
                x, y, bc, font, pol, od = float(t[1]), float(t[2]), t[3], t[4], t[5].upper(), int(t[6])
                i = 7
                angle = 0.0
                if od in (8, 9):
                    angle = float(t[7])
                    i = 8
                if t[i].upper() == "E":
                    i += 1
                w, h = float(t[i]), float(t[i + 1])
                fasc, cs, bg, astr, apos = (t[i + 2].upper(), t[i + 3].upper(), t[i + 4].upper(),
                                            t[i + 5].upper(), t[i + 6].upper())
                txt = " ".join(t[i + 7:]) if len(t) > i + 7 else ""
                txt = txt.strip("'\"")
                ff.records.append(Rec("B", pol, (x, y, bc, font, od, angle, w, h, fasc, cs, bg, astr, apos, txt),
                                      _parse_feature_attrs(atr, ff)))
        except (IndexError, ValueError) as exc:
            warn(f"skipped unreadable feature record '{line[:60]}' ({exc})")
    if surf is not None:
        ff.records.append(surf)
    return ff


_N = r"(\d+(?:\.\d*)?|\.\d+)"
_C = r"(?:x(\d+))?"      # optional corner list

APPROX_SYMBOLS = {"bfr", "bfs", "oval_h", "s_tho", "rc_tho", "moire", "radhplate", "dshape",
                  "cross", "dogbone", "dpack", "hplate", "rhplate", "fhplate"}


def std_symbol(name: str, k: float):
    """Geometry of an ODB++ standard symbol, or None if the name is not a
    standard symbol. k converts symbol units (mil or micron) to mm."""
    n = name.lower()

    def m(p):
        return re.match("^" + p + "$", n)

    def f(*vals):
        return [float(v) * k for v in vals]

    r = m(rf"r{_N}")
    if r:
        return circle(0.0, 0.0, float(r.group(1)) * k / 2.0)
    r = m(rf"s{_N}")
    if r:
        s, = f(r.group(1))
        return box(-s / 2, -s / 2, s / 2, s / 2)
    r = m(rf"rect{_N}x{_N}xr{_N}{_C}")
    if r:
        w, h, rad = f(r.group(1), r.group(2), r.group(3))
        return _rect_shape(w, h, rad, r.group(4) or "1234")
    r = m(rf"rect{_N}x{_N}xc{_N}{_C}")
    if r:
        w, h, rad = f(r.group(1), r.group(2), r.group(3))
        return _rect_shape(w, h, rad, r.group(4) or "1234", chamfer=True)
    r = m(rf"rect{_N}x{_N}")
    if r:
        w, h = f(r.group(1), r.group(2))
        return box(-w / 2, -h / 2, w / 2, h / 2)
    r = m(rf"oval{_N}x{_N}")
    if r:
        return _oval(*f(r.group(1), r.group(2)))
    r = m(rf"di{_N}x{_N}")
    if r:
        w, h = f(r.group(1), r.group(2))
        return _poly([(w / 2, 0), (0, h / 2), (-w / 2, 0), (0, -h / 2)])
    r = m(rf"oct{_N}x{_N}x{_N}")
    if r:
        w, h, c = f(r.group(1), r.group(2), r.group(3))
        return _rect_shape(w, h, c, "1234", chamfer=True)
    r = m(rf"donut_r{_N}x{_N}")
    if r:
        od, idd = f(r.group(1), r.group(2))
        return gdiff(circle(0, 0, od / 2), circle(0, 0, idd / 2))
    r = m(rf"donut_s{_N}x{_N}xr{_N}{_C}")
    if r:
        od, idd, rad = f(r.group(1), r.group(2), r.group(3))
        c = r.group(4) or "1234"
        return gdiff(_rect_shape(od, od, rad, c), _rect_shape(idd, idd, max(0.0, rad - (od - idd) / 2), c))
    r = m(rf"donut_sr{_N}x{_N}")
    if r:
        od, idd = f(r.group(1), r.group(2))
        return gdiff(box(-od / 2, -od / 2, od / 2, od / 2), circle(0, 0, idd / 2))
    r = m(rf"donut_s{_N}x{_N}")
    if r:
        od, idd = f(r.group(1), r.group(2))
        return gdiff(box(-od / 2, -od / 2, od / 2, od / 2), box(-idd / 2, -idd / 2, idd / 2, idd / 2))
    r = m(rf"donut_rc{_N}x{_N}x{_N}xr{_N}{_C}")
    if r:
        ow, oh, lw, rad = f(r.group(1), r.group(2), r.group(3), r.group(4))
        c = r.group(5) or "1234"
        return gdiff(_rect_shape(ow, oh, rad, c), _rect_shape(ow - 2 * lw, oh - 2 * lw, max(0.0, rad - lw), c))
    r = m(rf"donut_rc{_N}x{_N}x{_N}")
    if r:
        ow, oh, lw = f(r.group(1), r.group(2), r.group(3))
        return gdiff(_rect_shape(ow, oh), _rect_shape(ow - 2 * lw, oh - 2 * lw))
    r = m(rf"donut_o{_N}x{_N}x{_N}")
    if r:
        ow, oh, lw = f(r.group(1), r.group(2), r.group(3))
        return gdiff(_oval(ow, oh), _oval(ow - 2 * lw, oh - 2 * lw))
    r = m(rf"hex_l{_N}x{_N}x{_N}")
    if r:
        w, h, c = f(r.group(1), r.group(2), r.group(3))
        return _poly([(-w / 2, 0), (-w / 2 + c, -h / 2), (w / 2 - c, -h / 2), (w / 2, 0),
                      (w / 2 - c, h / 2), (-w / 2 + c, h / 2)])
    r = m(rf"hex_s{_N}x{_N}x{_N}")
    if r:
        w, h, c = f(r.group(1), r.group(2), r.group(3))
        return _poly([(0, -h / 2), (w / 2, -h / 2 + c), (w / 2, h / 2 - c), (0, h / 2),
                      (-w / 2, h / 2 - c), (-w / 2, -h / 2 + c)])
    r = m(rf"bfr{_N}")
    if r:
        d, = f(r.group(1))
        q = union_all([box(-d, 0, 0, d), box(0, -d, d, 0)])     # quadrants 2 and 4 (assumed)
        return ginter(circle(0, 0, d / 2), q)
    r = m(rf"bfs{_N}")
    if r:
        s, = f(r.group(1))
        return union_all([box(-s / 2, 0, 0, s / 2), box(0, -s / 2, s / 2, 0)])
    r = m(rf"tri{_N}x{_N}")
    if r:
        b, h = f(r.group(1), r.group(2))
        return _poly([(-b / 2, -h / 2), (b / 2, -h / 2), (0, h / 2)])
    r = m(rf"oval_h{_N}x{_N}")
    if r:
        w, h = f(r.group(1), r.group(2))
        if w >= h:
            return union_all([box(-w / 2, -h / 2, w / 2 - h / 2, h / 2), circle(w / 2 - h / 2, 0, h / 2)])
        return union_all([box(-w / 2, -h / 2, w / 2, h / 2 - w / 2), circle(0, h / 2 - w / 2, w / 2)])
    r = m(rf"(thr|ths){_N}x{_N}x{_N}x{_N}x{_N}")
    if r:
        od, idd, ang, ns, gap = float(r.group(2)) * k, float(r.group(3)) * k, float(r.group(4)), \
            float(r.group(5)), float(r.group(6)) * k
        ring = gdiff(circle(0, 0, od / 2), circle(0, 0, idd / 2))
        return _thermal(ring, od / 2, ang, ns, gap, rounded=(r.group(1) == "thr"), lw=(od - idd) / 2)
    r = m(rf"(s_ths|s_tho|s_thr){_N}x{_N}x{_N}x{_N}x{_N}(?:xr{_N}{_C})?")
    if r:
        os_, is_, ang, ns, gap = float(r.group(2)) * k, float(r.group(3)) * k, float(r.group(4)), \
            float(r.group(5)), float(r.group(6)) * k
        rad = float(r.group(7)) * k if r.group(7) else 0.0
        c = r.group(8) or "1234"
        ring = gdiff(_rect_shape(os_, os_, rad, c), _rect_shape(is_, is_, max(0.0, rad - (os_ - is_) / 2), c))
        return _thermal(ring, os_ * 0.75, ang, ns, gap, rounded=(r.group(1) == "s_thr"), lw=(os_ - is_) / 2)
    r = m(rf"sr_ths{_N}x{_N}x{_N}x{_N}x{_N}")
    if r:
        os_, idd, ang, ns, gap = float(r.group(1)) * k, float(r.group(2)) * k, float(r.group(3)), \
            float(r.group(4)), float(r.group(5)) * k
        ring = gdiff(box(-os_ / 2, -os_ / 2, os_ / 2, os_ / 2), circle(0, 0, idd / 2))
        return _thermal(ring, os_ * 0.75, ang, ns, gap)
    r = m(rf"(rc_ths|rc_tho){_N}x{_N}x{_N}x{_N}x{_N}x{_N}(?:xr{_N}{_C})?")
    if r:
        w, h = float(r.group(2)) * k, float(r.group(3)) * k
        ang, ns, gap, ag = float(r.group(4)), float(r.group(5)), float(r.group(6)) * k, float(r.group(7)) * k
        rad = float(r.group(8)) * k if r.group(8) else 0.0
        c = r.group(9) or "1234"
        ring = gdiff(_rect_shape(w, h, rad, c), _rect_shape(w - 2 * ag, h - 2 * ag, max(0.0, rad - ag), c))
        return _thermal(ring, max(w, h) * 0.75, ang, ns, gap)
    r = m(rf"o_ths{_N}x{_N}x{_N}x{_N}x{_N}x{_N}")
    if r:
        ow, oh = float(r.group(1)) * k, float(r.group(2)) * k
        ang, ns, gap, lw = float(r.group(3)), float(r.group(4)), float(r.group(5)) * k, float(r.group(6)) * k
        ring = gdiff(_oval(ow, oh), _oval(ow - 2 * lw, oh - 2 * lw))
        return _thermal(ring, max(ow, oh) * 0.75, ang, ns, gap)
    r = m(rf"oblong_ths{_N}x{_N}x{_N}x{_N}x{_N}x{_N}x([rs])")
    if r:
        ow, oh = float(r.group(1)) * k, float(r.group(2)) * k
        ang, ns, gap, lw = float(r.group(3)), float(r.group(4)), float(r.group(5)) * k, float(r.group(6)) * k
        ring = gdiff(_oval(ow, oh), _oval(ow - 2 * lw, oh - 2 * lw))
        return _thermal(ring, max(ow, oh) * 0.75, ang, ns, gap, rounded=(r.group(7) == "r"), lw=lw)
    r = m(rf"el{_N}x{_N}")
    if r:
        return _ellipse(*f(r.group(1), r.group(2)))
    r = m(rf"moire{_N}x{_N}x{_N}x{_N}x{_N}x{_N}")
    if r:
        rw, rg = float(r.group(1)) * k, float(r.group(2)) * k
        nr, lw, ll, la = int(float(r.group(3))), float(r.group(4)) * k, float(r.group(5)) * k, float(r.group(6))
        parts = []
        for i in range(nr):
            rin = rg + i * (rw + rg)
            parts.append(gdiff(circle(0, 0, rin + rw), circle(0, 0, rin)))
        for a in (la, la + 90.0):
            t = math.radians(a)
            dx, dy = math.cos(t) * ll / 2, math.sin(t) * ll / 2
            parts.append(stadium(-dx, -dy, dx, dy, lw / 2))
        return union_all(parts)
    r = m(rf"hole{_N}x([pnv])x{_N}x{_N}")
    if r:
        return circle(0, 0, float(r.group(1)) * k / 2)
    if m(r"null\d*"):
        return EMPTY
    r = m(rf"hplate{_N}x{_N}x{_N}(?:x{_N}x{_N})?")
    if r:
        w, h, c = f(r.group(1), r.group(2), r.group(3))
        return _poly([(-w / 2, -h / 2), (w / 2 - c, -h / 2), (w / 2, 0), (w / 2 - c, h / 2), (-w / 2, h / 2)])
    r = m(rf"rhplate{_N}x{_N}x{_N}(?:x{_N}x{_N})?")
    if r:
        w, h, c = f(r.group(1), r.group(2), r.group(3))
        return _poly([(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2 - c, 0), (w / 2, h / 2), (-w / 2, h / 2)])
    r = m(rf"fhplate{_N}x{_N}x{_N}x{_N}(?:x{_N}x{_N})?")
    if r:
        w, h, vc, hc = f(r.group(1), r.group(2), r.group(3), r.group(4))
        return _poly([(-w / 2, -h / 2), (w / 2 - hc, -h / 2), (w / 2, -h / 2 + vc), (w / 2, h / 2 - vc),
                      (w / 2 - hc, h / 2), (-w / 2, h / 2)])
    r = m(rf"radhplate{_N}x{_N}x{_N}(?:x{_N})?")
    if r:
        w, h, ms = f(r.group(1), r.group(2), r.group(3))
        sag = max(1e-6, (w - ms) / 2.0)
        R = (sag ** 2 + (h / 2) ** 2) / (2 * sag)
        cx = w / 2 + R - sag
        a = math.asin(min(1.0, (h / 2) / R))
        arc = arc_pts(cx, 0.0, R, math.pi - a, math.pi + a)[::-1]
        return _poly([(-w / 2, -h / 2)] + [tuple(p) for p in arc] + [(-w / 2, h / 2)])
    r = m(rf"dshape{_N}x{_N}x{_N}(?:x{_N})?")
    if r:
        w, h, rel = f(r.group(1), r.group(2), r.group(3))
        rel = max(1e-6, min(rel, w))
        R = (rel ** 2 + (h / 2) ** 2) / (2 * rel)
        cx = w / 2 - R
        a = math.asin(min(1.0, (h / 2) / R))
        arc = arc_pts(cx, 0.0, R, -a, a)
        return _poly([(-w / 2, -h / 2)] + [tuple(p) for p in arc] + [(-w / 2, h / 2)])
    r = m(rf"cross{_N}x{_N}x{_N}x{_N}x{_N}x{_N}x?([rs])(?:x?{_N})?")
    if r:
        w, h, hs, vs = f(r.group(1), r.group(2), r.group(3), r.group(4))
        hc, vc = float(r.group(5)), float(r.group(6))
        yc = -h / 2 + h * vc / 100.0
        xc = -w / 2 + w * hc / 100.0
        if r.group(7) == "r":
            return union_all([stadium(-w / 2 + hs / 2, yc, w / 2 - hs / 2, yc, hs / 2),
                              stadium(xc, -h / 2 + vs / 2, xc, h / 2 - vs / 2, vs / 2)])
        return union_all([box(-w / 2, yc - hs / 2, w / 2, yc + hs / 2), box(xc - vs / 2, -h / 2, xc + vs / 2, h / 2)])
    r = m(rf"dogbone{_N}x{_N}x{_N}x{_N}x{_N}x?([rs])(?:x{_N})?")
    if r:
        w, h, hs, vs = f(r.group(1), r.group(2), r.group(3), r.group(4))
        return union_all([box(-w / 2, -hs / 2, w / 2, hs / 2), box(-w / 2, -h / 2, -w / 2 + vs, h / 2),
                          box(w / 2 - vs, -h / 2, w / 2, h / 2)])
    r = m(rf"dpack{_N}x{_N}x{_N}x{_N}x(\d+)x(\d+)(?:x{_N})?")
    if r:
        w, h, hg, vg = f(r.group(1), r.group(2), r.group(3), r.group(4))
        rows, cols = max(1, int(r.group(5))), max(1, int(r.group(6)))
        ra = float(r.group(7)) * k if r.group(7) else 0.0
        pw = (w - (cols - 1) * vg) / cols
        ph = (h - (rows - 1) * hg) / rows
        parts = []
        for i in range(rows):
            for j in range(cols):
                cx = -w / 2 + pw / 2 + j * (pw + vg)
                cy = -h / 2 + ph / 2 + i * (ph + hg)
                parts.append(affinity.translate(_rect_shape(pw, ph, ra), cx, cy))
        return union_all(parts)
    return None


def symbol_family(name: str) -> str:
    m = re.match(r"^([a-z_]+?)(?=\d|\.)", name.lower())
    return m.group(1) if m else name.lower()


class StrokeFont:
    def __init__(self, text: str):
        self.xsize = self.ysize = 0.302
        self.offset = 0.0
        self.chars: Dict[str, list] = {}
        cur = None
        for raw in text.splitlines():
            t = raw.split()
            if not t:
                continue
            key = t[0].upper()
            if key == "XSIZE":
                self.xsize = float(t[1])
            elif key == "YSIZE":
                self.ysize = float(t[1])
            elif key == "OFFSET":
                self.offset = float(t[1])
            elif key == "CHAR":
                ch = raw.strip()[5:6] if len(raw.strip()) > 5 else " "
                cur = []
                self.chars[ch] = cur
            elif key == "ECHAR":
                cur = None
            elif key == "LINE" and cur is not None and len(t) >= 8:
                cur.append((float(t[1]), float(t[2]), float(t[3]), float(t[4]), t[5].upper(), t[6].upper(),
                            float(t[7])))
        xs = [v for lines in self.chars.values() for ln in lines for v in (ln[0], ln[2])]
        ys = [v for lines in self.chars.values() for ln in lines for v in (ln[1], ln[3])]
        self.minx = min(xs) if xs else 0.0
        self.miny = min(ys) if ys else 0.0


CODE39 = {
    "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn", "4": "nnnwwnnnw",
    "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw", "8": "wnnwnnwnn", "9": "nnwwnnwnn",
    "A": "wnnnnwnnw", "B": "nnwnnwnnw", "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn",
    "F": "nnwnwwnnn", "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww", "O": "wnnnwnnwn",
    "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn", "S": "nnwnnnwwn", "T": "nnnnwnwwn",
    "U": "wwnnnnnnw", "V": "nwwnnnnnw", "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn",
    "Z": "nwwnwnnnn", "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn", "*": "nwnnwnwnn",
    "$": "nwnwnwnnn", "/": "nwnwnnnwn", "+": "nwnnnwnwn", "%": "nnnwnwnwn",
}
CODE39_ORDER = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%"


class Renderer:
    def __init__(self, job: OdbJob, default_units: str):
        self.job = job
        self.units = default_units
        self.user_syms: Dict[str, object] = {}
        self.fonts: Dict[str, Optional[StrokeFont]] = {}
        self.sym_cache: Dict[tuple, object] = {}
        self.approx_used = set()
        self.surf_stats: Counter = Counter()
        self._busy = set()

    # .. symbols ...............................................................
    def symbol(self, name: str, unit: Optional[str], ff: FeatureFile):
        k = ff.ss(unit)
        key = (name, k)
        g = self.sym_cache.get(key)
        if g is not None:
            return g
        g = std_symbol(name, k)
        if g is not None:
            fam = symbol_family(name)
            if fam in APPROX_SYMBOLS:
                self.approx_used.add(fam)
        else:
            g = self.user_symbol(name)
            if g is None:
                warn(f"unknown symbol '{name}' ignored")
                g = EMPTY
        self.sym_cache[key] = g
        return g

    def user_symbol(self, name: str):
        key = name.lower()
        if key in self.user_syms:
            return self.user_syms[key]
        if key in self._busy:
            warn(f"recursive user symbol '{name}'")
            return EMPTY
        txt = self.job.text(f"symbols/{key}/features")
        if txt is None:
            self.user_syms[key] = None
            return None
        self._busy.add(key)
        g = self.render(parse_features(txt, self.units))
        self._busy.discard(key)
        self.user_syms[key] = g
        return g

    def font(self, name: str) -> Optional[StrokeFont]:
        key = (name or "standard").lower()
        if key not in self.fonts:
            txt = self.job.text(f"fonts/{key}") or (self.job.text("fonts/standard") if key != "standard" else None)
            self.fonts[key] = StrokeFont(txt) if txt else None
        return self.fonts[key]

    # .. strokes ...............................................................
    @staticmethod
    def _stroke(x0, y0, x1, y1, w, shape="R"):
        if shape == "S":
            return square_line(x0, y0, x1, y1, w)
        return stadium(x0, y0, x1, y1, w / 2.0)

    def _sym_kind(self, ff: FeatureFile, sym: int):
        name, unit = ff.symbols.get(sym, ("", None))
        n = name.lower()
        r = re.match(rf"^r{_N}$", n)
        if r:
            return "r", float(r.group(1)) * ff.ss(unit), name, unit
        r = re.match(rf"^s{_N}$", n)
        if r:
            return "s", float(r.group(1)) * ff.ss(unit), name, unit
        return "x", 0.0, name, unit

    def _sweep_symbol(self, pts: np.ndarray, ff, name, unit):
        """Line or arc drawn with a non-round symbol: union of convex hulls of the
        symbol placed at consecutive points (exact for convex symbols)."""
        g = self.symbol(name, unit, ff)
        if g.is_empty:
            return EMPTY
        parts = []
        hull = g.convex_hull
        for i in range(len(pts) - 1):
            a = affinity.translate(hull, pts[i][0], pts[i][1])
            b = affinity.translate(hull, pts[i + 1][0], pts[i + 1][1])
            parts.append(union_all([a, b]).convex_hull)
        if not parts:
            parts.append(affinity.translate(g, pts[0][0], pts[0][1]))
        if abs(hull.area - g.area) > 1e-6 * max(hull.area, 1e-12):
            warn(f"line/arc drawn with non-convex symbol '{name}' approximated by its convex hull")
        return union_all(parts)

    # .. feature geometry ........................................................
    def feature_geom(self, rec: Rec, ff: FeatureFile):
        cs = ff.cs
        k = rec.kind
        if k == "P":
            x, y, sym, resize, od, ang = rec.data
            x, y = x * cs, y * cs
            kind, size, name, unit = self._sym_kind(ff, sym)
            rs = resize * ff.ss(unit) if resize else 0.0
            if kind == "r":
                return circle(x, y, (size + rs) / 2.0)
            g = self.symbol(name, unit, ff)
            if g.is_empty:
                return g
            if rs:
                g = polys_only(g.buffer(rs / 2.0, quad_segs=16))
            M = m_compose(m_translate(x, y), orient_matrix(od, ang))
            return m_apply(M, g)
        if k == "L":
            xs, ys, xe, ye, sym = rec.data
            xs, ys, xe, ye = xs * cs, ys * cs, xe * cs, ye * cs
            kind, size, name, unit = self._sym_kind(ff, sym)
            if kind == "r":
                return stadium(xs, ys, xe, ye, size / 2.0)
            if kind == "s":
                return square_line(xs, ys, xe, ye, size)
            return self._sweep_symbol(np.array([[xs, ys], [xe, ye]]), ff, name, unit)
        if k == "A":
            xs, ys, xe, ye, xc, yc, sym, cw = rec.data
            xs, ys, xe, ye, xc, yc = xs * cs, ys * cs, xe * cs, ye * cs, xc * cs, yc * cs
            kind, size, name, unit = self._sym_kind(ff, sym)
            if kind == "r":
                return thick_arc(xs, ys, xe, ye, xc, yc, cw, size / 2.0)
            cl = arc_centerline(xs, ys, xe, ye, xc, yc, cw)
            if kind == "s":
                return union_all([square_line(cl[i][0], cl[i][1], cl[i + 1][0], cl[i + 1][1], size)
                                  for i in range(len(cl) - 1)])
            return self._sweep_symbol(cl, ff, name, unit)
        if k == "S":
            return self.surface_geom(rec, cs, self.surf_stats)
        if k == "T":
            return self.text_geom(rec, ff)
        if k == "B":
            return self.barcode_geom(rec, ff)
        return EMPTY

    @staticmethod
    def contour_points(ops, cs: float) -> List[tuple]:
        x0, y0 = ops[0][1]
        cur = (x0 * cs, y0 * cs)
        pts = [cur]
        for op, a in ops[1:]:
            if op == "L":
                cur = (a[0] * cs, a[1] * cs)
                pts.append(cur)
            else:
                xe, ye, xc, yc, cw = a
                arc = arc_centerline(cur[0], cur[1], xe * cs, ye * cs, xc * cs, yc * cs, cw)
                pts.extend(map(tuple, arc[1:]))
                cur = (xe * cs, ye * cs)
        return pts

    @staticmethod
    def surface_geom(rec: Rec, cs: float, stats: Optional[Counter] = None):
        """Islands minus holes. Each hole is cut from the island that contains
        it, found by position rather than by record order: writers do not all
        list a hole straight after its island (Altium writes a TrueType string
        as one surface with every glyph outline first and every counter after
        them, so pairing by order filled the counters of all glyphs but the
        last). A contour nested directly in one of the same type but wound the
        other way is read as the opposite type (non-zero rule; ODB++ islands
        are clockwise, holes counter-clockwise)."""
        polys, flags, cw = [], [], []
        for is_island, ops in rec.polys or []:
            if not ops:
                continue
            pts = Renderer.contour_points(ops, cs)
            if len(pts) < 3:
                continue
            poly = make_valid(Polygon(pts))
            if poly.is_empty:
                continue
            P = np.asarray(pts)
            sa = float(np.dot(P[:, 0], np.roll(P[:, 1], -1)) - np.dot(np.roll(P[:, 0], -1), P[:, 1]))
            polys.append(poly)
            flags.append(bool(is_island))
            cw.append(sa < 0.0)
        n = len(polys)
        if n == 0:
            return EMPTY
        if n == 1 and flags[0]:
            return polys[0]
        parent, area = contour_parents(polys)
        # effective type, parents first (a parent always has the larger area)
        eff = list(flags)
        for i in sorted(range(n), key=lambda k: -area[k]):
            p = parent[i]
            if p >= 0 and flags[i] == eff[p] and cw[i] != cw[p]:
                eff[i] = not eff[p]
                if stats is not None:
                    stats["contours read by winding"] += 1
        cuts: Dict[int, list] = defaultdict(list)
        orphans = []
        last_island = -1            # what pairing by record order would have used
        for i in range(n):
            if flags[i]:
                last_island = i
            if eff[i]:
                continue
            p = parent[i]
            while p >= 0 and not eff[p]:
                p = parent[p]
            if p >= 0:
                cuts[p].append(polys[i])
            else:
                orphans.append(polys[i])
            if stats is not None and not flags[i] and 0 <= last_island != p and p >= 0:
                stats["holes placed by position"] += 1
        islands = [i for i in range(n) if eff[i]]
        if not islands:
            if stats is not None:
                stats["hole-only surfaces ignored"] += 1
            return EMPTY
        g = union_all(gdiff(polys[i], union_all(cuts[i])) if i in cuts else polys[i] for i in islands)
        if orphans:
            if stats is not None:
                stats["holes outside every island"] += len(orphans)
            g = gdiff(g, union_all(orphans))
        return g

    def text_geom(self, rec: Rec, ff: FeatureFile):
        x, y, font, od, ang, xs, ys, wf, txt, ver = rec.data
        cs = ff.cs
        F = self.font(font)
        xs_mm, ys_mm = xs * cs, ys * cs
        width = wf * 12 * MIL if wf > 0 else 0.0
        if F is None:
            g = _text_fallback(txt, ys_mm)
            if g is None:
                warn("text features skipped: no stroke font in the job and matplotlib unavailable")
                return EMPTY
            warn("job has no fonts/standard; text rendered with a fallback outline font")
        else:
            sx = xs_mm / F.xsize if F.xsize else 1.0
            sy = ys_mm / F.ysize if F.ysize else 1.0
            adv = xs_mm + F.offset * sx
            parts_p, parts_n = [], []
            for i, ch in enumerate(txt):
                lines = F.chars.get(ch)
                if lines is None:
                    lines = F.chars.get(ch.upper(), [])
                ox = i * adv
                for (gx0, gy0, gx1, gy1, pol, shape, gw) in lines:
                    w = width if width > 0 else gw * sy
                    seg = self._stroke(ox + (gx0 - F.minx) * sx, (gy0 - F.miny) * sy,
                                       ox + (gx1 - F.minx) * sx, (gy1 - F.miny) * sy, w, shape)
                    (parts_n if pol == "N" else parts_p).append(seg)
            g = gdiff(union_all(parts_p), union_all(parts_n))
        M = m_compose(m_translate(x * cs, y * cs), orient_matrix(od, ang))
        return m_apply(M, g)

    def barcode_geom(self, rec: Rec, ff: FeatureFile):
        x, y, bc, font, od, ang, w, h, fasc, csum, bg, astr, apos, txt = rec.data
        cs = ff.cs
        w_mm, h_mm = w * cs, h * cs
        if w_mm <= 0 or h_mm <= 0:
            return EMPTY
        if bc.upper() not in ("UPC39", "CODE39", "C39"):
            warn(f"barcode type '{bc}' drawn as Code 39")
        data = txt.upper()
        if csum.startswith("Y"):
            tot = sum(CODE39_ORDER.index(c) for c in data if c in CODE39_ORDER)
            data += CODE39_ORDER[tot % 43]
        seq = "*" + "".join(c for c in data if c in CODE39) + "*"
        wide = 3.0 * w_mm
        bars = []
        cx = 0.0
        for c in seq:
            pat = CODE39[c]
            for j, e in enumerate(pat):
                ew = wide if e == "w" else w_mm
                if j % 2 == 0:
                    bars.append(box(cx, 0.0, cx + ew, h_mm))
                cx += ew
            cx += w_mm  # inter-character gap
        g = union_all(bars)
        total_w = cx - w_mm
        if bg.startswith("Y"):
            q = 10 * w_mm
            g = gdiff(box(-q, -q, total_w + q, h_mm + q), g)
        if astr.startswith("Y") and txt:
            th = max(h_mm * 0.15, 0.5)
            wf = th * 0.12 / (12 * MIL)
            fake = Rec("T", "P", (0.0, 0.0, font, 0, 0.0, th * 0.8 / cs, th / cs, wf, txt, 0))
            tg = self.text_geom(fake, ff)
            dy = (h_mm + 0.3 * th) if apos.startswith("T") else -(1.3 * th)
            g = union_all([g, affinity.translate(tg, 0.0, dy)])
        self.approx_used.add("barcode")
        M = m_compose(m_translate(x * cs, y * cs), orient_matrix(od, ang))
        return m_apply(M, g)

    # .. whole feature files ....................................................
    def render(self, ff: FeatureFile, select=None):
        """Render records honouring polarity order (later negative features
        erase earlier positive ones)."""
        acc = EMPTY
        batch: list = []
        bpol = None
        for rec in ff.records:
            if select is not None and not select(rec):
                continue
            g = self.feature_geom(rec, ff)
            if g is None or g.is_empty:
                continue
            pol = "N" if rec.pol.startswith("N") else "P"
            if bpol is not None and pol != bpol:
                acc = _apply_batch(acc, batch, bpol)
                batch = []
            batch.append(g)
            bpol = pol
        if batch:
            acc = _apply_batch(acc, batch, bpol)
        return acc


def report_surfaces(stats: Counter) -> None:
    n = stats.get("holes placed by position", 0)
    if n:
        info(f"Surfaces: {n} hole contour(s) listed after a different island were cut from the island "
             f"that contains them (e.g. Altium TrueType strings)")
    n = stats.get("contours read by winding", 0)
    if n:
        info(f"Surfaces: {n} nested contour(s) whose I/H flag repeats the enclosing contour's type "
             f"were read by winding direction")
    n = stats.get("hole-only surfaces ignored", 0)
    if n:
        warn(f"{n} surface feature(s) contain only hole contours and were ignored")
    n = stats.get("holes outside every island", 0)
    if n:
        warn(f"{n} hole contour(s) lie outside every island of their surface (subtracted from the surface)")


def _apply_batch(acc, batch, pol):
    u = union_all(batch)
    if pol == "P":
        return union_all([acc, u])
    return gdiff(acc, u)


def _text_fallback(txt: str, height: float):
    try:
        from matplotlib.textpath import TextPath
        from matplotlib.font_manager import FontProperties
    except Exception:
        return None
    try:
        tp = TextPath((0, 0), txt, size=height * 1.4, prop=FontProperties(family="DejaVu Sans"))
        polys = [Polygon(p) for p in tp.to_polygons() if len(p) >= 3]
        acc = EMPTY
        for p in sorted(polys, key=lambda q: -q.area):
            p = make_valid(p)
            acc = gdiff(acc, p) if acc.contains(p) else union_all([acc, p])
        return acc
    except Exception:
        return None


COPPER_TYPES = {"SIGNAL", "POWER_GROUND", "MIXED"}


def layer_kind(L: Layer) -> str:
    t = L.type
    if t in COPPER_TYPES:
        return "copper"
    return {"DIELECTRIC": "dielectric", "SOLDER_MASK": "mask", "SILK_SCREEN": "silk",
            "SOLDER_PASTE": "paste", "CONDUCTIVE_PASTE": "finish", "DRILL": "drill",
            "ROUT": "rout"}.get(t, "doc")


def load_matrix(job: OdbJob):
    txt = job.text("matrix/matrix")
    if txt is None:
        raise SystemExit("matrix/matrix not readable")
    _top, arrays = parse_structured(txt)
    steps = [d.get("NAME", "") for n, d in arrays if n == "STEP"]
    layers = []
    for n, d in arrays:
        if n != "LAYER":
            continue
        try:
            row = int(float(d.get("ROW", "0") or 0))
        except ValueError:
            row = 0
        L = Layer(row=row, kind="", name=d.get("NAME", ""), type=d.get("TYPE", "").upper(),
                   context=d.get("CONTEXT", "BOARD").upper(), polarity=d.get("POLARITY", "POSITIVE").upper(),
                   add_type=d.get("ADD_TYPE", "").upper(), start=d.get("START_NAME", ""), end=d.get("END_NAME", ""),
                   dielectric_type=d.get("DIELECTRIC_TYPE", "").upper(), dielectric_name=d.get("DIELECTRIC_NAME", ""))
        L.kind = layer_kind(L)
        layers.append(L)
    layers.sort(key=lambda L: L.row)
    return steps, layers


def step_instances(job: OdbJob, step: str, M=IDENT, depth=0, units="INCH"):
    """All (step, transform) instances reachable from 'step' via step-and-repeat.
    ANGLE is applied clockwise (ODB++ convention); MIRROR flips x -> -x."""
    out = [(step.lower(), M)]
    if depth > 8:
        warn("step-and-repeat nesting deeper than 8 levels ignored")
        return out
    txt = job.text(f"steps/{step.lower()}/stephdr")
    if not txt:
        return out
    top, arrays = parse_structured(txt)
    su = top.get("UNITS", units).upper()
    sc = 25.4 if su.startswith("INCH") else 1.0
    for n, d in arrays:
        if n != "STEP-REPEAT":
            continue
        child = d.get("NAME", "").lower()
        if not child:
            continue
        ctxt = job.text(f"steps/{child}/stephdr") or ""
        ctop, _ = parse_structured(ctxt)
        csc = 25.4 if ctop.get("UNITS", su).upper().startswith("INCH") else 1.0
        dx0 = float(ctop.get("X_DATUM", 0) or 0) * csc
        dy0 = float(ctop.get("Y_DATUM", 0) or 0) * csc
        X, Y = float(d.get("X", 0) or 0) * sc, float(d.get("Y", 0) or 0) * sc
        DX, DY = float(d.get("DX", 0) or 0) * sc, float(d.get("DY", 0) or 0) * sc
        NX, NY = int(float(d.get("NX", 1) or 1)), int(float(d.get("NY", 1) or 1))
        ang = float(d.get("ANGLE", 0) or 0)
        mirror = d.get("MIRROR", "NO").upper().startswith("Y")
        if d.get("FLIP", "NO").upper().startswith("Y"):
            warn(f"step-repeat FLIP of '{child}' (layer order reversal) is not supported; placed unflipped")
        for i in range(max(1, NX)):
            for j in range(max(1, NY)):
                loc = m_translate(-dx0, -dy0)
                if mirror:
                    loc = m_compose(M_MIRROR_X, loc)
                loc = m_compose(m_rot_cw(ang), loc)
                loc = m_compose(m_translate(X + i * DX, Y + j * DY), loc)
                out += step_instances(job, child, m_compose(M, loc), depth + 1, units)
    return out


def _len_mm(v: float, units: str, kind: str) -> float:
    """Convert attribute values to mm. kind 'inch_mm' (inch or mm) or
    'mil_micron' (mil or micron)."""
    u = (units or "MM").upper()
    if kind == "inch_mm":
        if u.startswith("MM"):
            return v / 1000.0 if v > 20.0 else v
        return v * MIL if v > 1.0 else v * 25.4
    if u.startswith("MM"):
        return v / 1000.0
    return v * MIL


def copper_from_attrs(attrs) -> Tuple[Optional[float], str]:
    v = attr_float(attrs, ".copper_weight")
    if v is None or v <= 0:
        return None, ""
    if v <= 6.0:
        return v * OZ_TO_MM, f".copper_weight={v:g} (oz)"
    return v / 1000.0, f".copper_weight={v:g} (um)"


def layer_dielectric_from_attrs(attrs) -> Optional[float]:
    v = attr_float(attrs, ".layer_dielectric")
    if v is None or v <= 0:
        return None
    return _len_mm(v, attrs.get("units", "MM"), "inch_mm")


def parse_stackup_xml(text: str) -> Dict[str, float]:
    """matrix/stackup.xml (ODB++ 8.1): layer name -> thickness (mm)."""
    out: Dict[str, float] = {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        warn("matrix/stackup.xml could not be parsed")
        return out
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]

    def to_mm(val, unit):
        u = (unit or root.get("DefaultUnits") or "MIL").upper()
        v = float(val)
        return {"MIL": v * MIL, "INCH": v * 25.4, "MM": v, "MICRON": v / 1000.0, "UM": v / 1000.0}.get(u, v * MIL)

    for section in [s for s in root if s.tag in ("EdaData", "SupplierData")][:1] or [root]:
        specs: Dict[str, float] = {}
        for spec in section.iter("Spec"):
            t = None
            for prop in spec.iter("Property"):
                val = prop.get("Finished_Thickness") or prop.get("Thickness")
                if val:
                    try:
                        t = to_mm(val, prop.get("Units_Thickness"))
                        break
                    except ValueError:
                        pass
            if t is None:
                for dt in spec.iter("Default_Thickness"):
                    val = dt.get("Finished_Thickness") or dt.get("Thickness")
                    if val:
                        try:
                            t = to_mm(val, dt.get("Units"))
                            break
                        except ValueError:
                            pass
            if t is not None:
                specs[spec.get("SpecName", "")] = t
        for lay in section.iter("Layer"):
            nm = lay.get("LayerName")
            if not nm:
                continue
            for sr in lay.iter("SpecRef"):
                t = specs.get(sr.get("MaterialSpecName", ""))
                if t:
                    out[nm.lower()] = t
                    break
    return out


def parse_tools(text: Optional[str]):
    tools = []
    if not text:
        return tools
    top, arrays = parse_structured(text)
    units = top.get("UNITS", "INCH").upper()
    for n, d in arrays:
        if n != "TOOLS":
            continue

        def size(key):
            try:
                v = float(d.get(key, "-1") or -1)
            except ValueError:
                return None
            if v <= 0:
                return None
            if units.startswith("MM"):
                return v if v < 20.0 else v / 1000.0
            return v * 25.4 if v < 1.0 else v * MIL
        tools.append(dict(num=d.get("NUM", ""), type=d.get("TYPE", "").upper(), type2=d.get("TYPE2", "").upper(),
                          finish=size("FINISH_SIZE"), drill=size("DRILL_SIZE")))
    return tools


def match_tool(tools, d_mm, drill_attr):
    cands = [t for t in tools if t["finish"] and abs(t["finish"] - d_mm) < 0.0015]
    if not cands:
        cands = [t for t in tools if t["drill"] and abs(t["drill"] - d_mm) < 0.0015]
    if len(cands) > 1 and drill_attr:
        want = {"plated": "PLATED", "non_plated": "NON_PLATED", "via": "VIA"}.get(drill_attr)
        c2 = [t for t in cands if t["type"] == want]
        if c2:
            cands = c2
    return cands[0] if cands else None


def odb_counter_flags(job: OdbJob) -> Tuple[Optional[bool], Optional[bool]]:
    """Board-level counterbore / countersink flags of ODB++ metadata.xml
    (counter_bore, countersink), if an exporter wrote them."""
    for p in job._all_paths():
        if not p.endswith("metadata.xml"):
            continue
        data = job.files.get(p) if job.files else open(job.dir_index[p], "rb").read()
        txt = decode_text(data)

        def flag(pat):
            m = re.search(rf"<({pat})\b[^>]*>\s*(\w+)\s*</\1\s*>", txt, re.I)
            v = m.group(2) if m else None
            if v is None:
                m = re.search(rf"\b(?:{pat})\s*=\s*[\"'](\w+)[\"']", txt, re.I)
                v = m.group(1) if m else None
            return None if v is None else v.lower() in ("true", "yes", "y", "1")
        return flag(r"counter_?bore"), flag(r"counter_?sink")
    return None, None


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------


class OdbReader:
    """ODB++ product model -> Board."""

    def __init__(self, path: str, opts):
        self.path = path
        self.opts = opts
        self.job = job = OdbJob(path)
        top, _ = parse_structured(job.text("misc/info") or "")
        self.info = top
        self.units = top.get("UNITS", "INCH").upper()
        self.steps, self.layers = load_matrix(job)
        self.step = self._pick_step()
        for L in self.layers:
            L.attrs = parse_attrlist(job.text(f"steps/{self.step}/layers/{L.key}/attrlist"))
        self.job_attrs = parse_attrlist(job.text("misc/attrlist"))
        self.step_attrs = parse_attrlist(job.text(f"steps/{self.step}/attrlist"))
        xml = job.text("matrix/stackup.xml")
        self.xml_t = parse_stackup_xml(xml) if xml else {}
        self.instances = step_instances(job, self.step, units=self.units)
        self.R = Renderer(job, self.units)
        self._ff: Dict[tuple, FeatureFile] = {}
        self._own: Dict[tuple, object] = {}
        self._type_holes: list = []          # (hole, layer) of the per-type via drill layers
        self._tools: Dict[tuple, list] = {}

    # .. inputs ................................................................
    def _pick_step(self) -> str:
        names = [s.lower() for s in self.steps] or self.job.listdir("steps")
        if not names:
            raise SystemExit("the product model has no steps")
        want = getattr(self.opts, "step", None)
        if want:
            if want.lower() not in names:
                raise SystemExit(f"step '{want}' not found; available: {', '.join(names)}")
            return want.lower()
        if "pcb" in names:
            return "pcb"
        for s in names:
            if "STEP-REPEAT" not in (self.job.text(f"steps/{s}/stephdr") or "").upper():
                return s
        return names[0]

    def ff(self, step: str, layer: str) -> FeatureFile:
        key = (step, layer)
        if key not in self._ff:
            self._ff[key] = parse_features(self.job.text(f"steps/{step}/layers/{layer}/features"), self.units)
        return self._ff[key]

    def tools(self, step: str, layer: str):
        key = (step, layer)
        if key not in self._tools:
            self._tools[key] = parse_tools(self.job.text(f"steps/{step}/layers/{layer}/tools"))
        return self._tools[key]

    def raw_image(self, L: Layer):
        parts = []
        for s, M in self.instances:
            key = (s, L.key)
            if key not in self._own:
                ff = self.ff(s, L.key)
                self._own[key] = self.R.render(ff) if ff.records else EMPTY
            g = self._own[key]
            if not g.is_empty:
                parts.append(m_apply(M, g))
        return union_all(parts)

    def mask_split(self, L: Layer):
        """Solder mask layer: (openings of everything but the vias, [(x, y, opening)]
        of the vias' own openings, the pads with .pad_usage=via). None when the
        layer's pads carry no .pad_usage, so the data does not say which are vias."""
        def is_via(rec):
            return rec.kind == "P" and not rec.pol.startswith("N") and (rec.attrs or {}).get(".pad_usage") == "via"
        known = False
        other, own = [], []
        for s, M in self.instances:
            ff = self.ff(s, L.key)
            if not ff.records:
                continue
            known = known or any(r.kind == "P" and ".pad_usage" in (r.attrs or {}) for r in ff.records)
            g = self.R.render(ff, select=lambda r: not is_via(r))
            if not g.is_empty:
                other.append(m_apply(M, g))
            for rec in ff.records:
                if not is_via(rec):
                    continue
                gv = self.R.feature_geom(rec, ff)
                if gv is None or gv.is_empty:
                    continue
                gv = m_apply(M, gv)
                c = gv.centroid
                own.append((c.x, c.y, gv))
        if not known:
            return None
        return union_all(other), own

    def inside_files(self) -> Dict[str, object]:
        """Every file inside the ODB++ input: {name: bytes or path} (NC drill search)."""
        return dict(self.job.files) if self.job.files else dict(self.job.dir_index)

    # .. layers ...............................................................
    def _setup_layers(self):
        steps_used = sorted({s for s, _ in self.instances})
        for L in self.layers:
            L.image_fn = (lambda L=L: self.raw_image(L))
            has = any(self.ff(s, L.key).records for s in steps_used)
            L.empty = not has and not L.polarity.upper().startswith("NEG")
            if L.kind == "copper":
                if L.key in self.xml_t:
                    L.t_data, L.t_data_src = self.xml_t[L.key], "stackup.xml"
                else:
                    t, src = copper_from_attrs(L.attrs)
                    if t is not None:
                        L.t_data, L.t_data_src = t, src
            elif L.kind == "dielectric":
                if L.key in self.xml_t:
                    L.t_data, L.t_data_src = self.xml_t[L.key], "stackup.xml"
                else:
                    t = layer_dielectric_from_attrs(L.attrs)
                    if t is not None:
                        L.t_data, L.t_data_src = t, ".layer_dielectric"
                L.dk = attr_float(L.attrs, ".dielectric_constant")
                L.df = attr_float(L.attrs, ".loss_tangent")
                L.material = L.dielectric_name or L.attrs.get(".comment", "")
            elif L.kind in ("mask", "silk", "finish", "paste"):
                if L.kind == "mask":
                    L.mask_split_fn = (lambda L=L: self.mask_split(L))
                v = layer_dielectric_from_attrs(L.attrs)
                if v is not None:
                    L.t_data, L.t_data_src = v, ".layer_dielectric"
                L.material = L.attrs.get(".comment", "")
                L.dk = attr_float(L.attrs, ".dielectric_constant")
                L.df = attr_float(L.attrs, ".loss_tangent")
            if L.kind not in ("copper", "dielectric", "drill", "rout", "silk", "paste"):
                L.points_fn = (lambda L=L: self._doc_points(L))

    # .. drills ...............................................................
    @staticmethod
    def _dir_hint(L: Layer) -> str:
        d = str(L.attrs.get(".drill_layer_direction", "")).lower()
        if d.startswith("top2"):
            return "top"
        if d.startswith("bottom2") or d.startswith("bot2"):
            return "bottom"
        return ""

    @staticmethod
    def _drill_role(L: Layer) -> str:
        toks = name_tokens(L.name)
        add = L.add_type
        if not add and via_type_layer(L.name):
            return "viatype"
        if add == "BACKDRILL" or toks & {"backdrill", "backdrills", "bd"}:
            return "backdrill"
        if add in ("COUNTERHOLE", "COUNTERBORE", "COUNTERSINK") or toks & COUNTER_TOKENS:
            return "counter"
        if add == "DUAL_DIAMETER":
            return "dual"
        return "hole"

    @staticmethod
    def _layer_counter_type(L: Layer) -> Tuple[str, str]:
        toks = name_tokens(L.name)
        if L.add_type == "COUNTERSINK":
            return "countersink", f"ODB++ layer {L.name} (ADD_TYPE=COUNTERSINK)"
        if L.add_type == "COUNTERBORE":
            return "counterbore", f"ODB++ layer {L.name} (ADD_TYPE=COUNTERBORE)"
        if toks & {"countersink", "countersinks", "csink", "csk"}:
            return "countersink", f"ODB++ layer name {L.name}"
        if toks & {"counterbore", "counterbores", "cbore"}:
            return "counterbore", f"ODB++ layer name {L.name}"
        return "", ""

    def _drill_item(self, rec: Rec, ff: FeatureFile, M):
        """-> (x, y, diameter, geometry or None for round holes)."""
        cs = ff.cs
        if rec.kind == "P":
            x, y, sym, _resize, _od, _ang = rec.data
            kind, size, _name, _unit = self.R._sym_kind(ff, sym)
            px, py = m_apply_pt(M, x * cs, y * cs)
            if kind == "r":
                return px, py, size, None
            g = m_apply(M, self.R.feature_geom(rec, ff))
            if g.is_empty:
                return None
            x0, y0, x1, y1 = g.bounds
            return px, py, min(x1 - x0, y1 - y0), g
        if rec.kind in ("L", "A", "S"):
            g = m_apply(M, self.R.feature_geom(rec, ff))
            if g.is_empty:
                return None
            c = g.centroid
            if rec.kind == "S":
                x0, y0, x1, y1 = g.bounds
                d = min(x1 - x0, y1 - y0)
            else:
                kind, size, _n, _u = self.R._sym_kind(ff, rec.data[4] if rec.kind == "L" else rec.data[6])
                d = size
            return c.x, c.y, d, g
        return None

    def _doc_points(self, L: Layer):
        """Markers of a document layer (Filling, Capping, ...). Altium writes a
        via's IPC-4761 features there as pads; when the layer has pads only
        they count (lines, arcs and surfaces are artwork), else every feature."""
        pads, other = [], []
        for s, M in self.instances:
            ff = self.ff(s, L.key)
            for rec in ff.records:
                item = self._drill_item(rec, ff, M)
                if item:
                    (pads if rec.kind == "P" else other).append(item[:3])
        if pads:
            L.points_note = f"{len(pads)} pads" + (f"; {len(other)} drawn features ignored" if other else "")
            return pads
        L.points_note = f"{len(other)} drawn features" if other else ""
        return other

    def _drills(self, board: Board):
        units = self.units
        for L in self.layers:
            if L.context != "BOARD" or L.kind != "drill":
                continue
            role = self._drill_role(L)
            dh = self._dir_hint(L)
            toks = name_tokens(L.name)
            npth_name = bool(toks & {"npth", "nonplated", "unplated", "np"}) or ("non" in toks and "plated" in toks)
            ctype, csrc = self._layer_counter_type(L) if role == "counter" else ("", "")
            dattr = attr_float(L.attrs, ".backdrill_max_depth") if role == "backdrill" else None
            pair = self._layer_pair(L) if role == "backdrill" else None
            for s, M in self.instances:
                ff = self.ff(s, L.key)
                tools = self.tools(s, L.key)
                for rec in ff.records:
                    item = self._drill_item(rec, ff, M)
                    if item is None:
                        continue
                    px, py, d, geom = item
                    attrs = rec.attrs or {}
                    if role == "counter":
                        board.counterholes.append(Counterhole(x=px, y=py, d=d, layer=L.name, start=L.start,
                                                              end=L.end, dir_hint=dh, geom=geom, type=ctype,
                                                              src=csrc))
                        continue
                    if role == "backdrill":
                        b = Backdrill(x=px, y=py, d=d, layer=L.name, start=L.start, end=L.end, dir_hint=dh,
                                      src=f"ODB++ layer {L.name}", pair=pair)
                        if dattr and dattr > 0:
                            b.depth = _len_mm(dattr, L.attrs.get("units", units), "mil_micron")
                            b.src = f"ODB++ {L.name} .backdrill_max_depth"
                        sattr = attr_float(attrs, ".backdrill_max_stub_drill")
                        if sattr and sattr > 0:
                            b.stub = _len_mm(sattr, L.attrs.get("units", units), "mil_micron")
                            b.src = f"ODB++ {L.name} .backdrill_max_stub_drill"
                        board.backdrills.append(b)
                        continue
                    if role == "dual":
                        board.duals.append(DualDrill(x=px, y=py, d=d, layer=L.name, start=L.start, end=L.end,
                                                     dir_hint=dh))
                        continue
                    drill_attr = attrs.get(".drill")
                    tool = match_tool(tools, d, drill_attr) if d > 0 else None
                    ttype = tool["type"] if tool else ""
                    if drill_attr in ("plated", "via"):
                        plated = True
                    elif drill_attr == "non_plated":
                        plated = False
                    elif ttype:
                        plated = ttype != "NON_PLATED"
                    else:
                        plated = not npth_name
                    is_via = (drill_attr == "via" or ttype == "VIA") and plated
                    kind = "via" if is_via else ("pth" if plated else "npth")
                    laser = (bool(tool and tool["type2"] == "LASER")
                             or str(attrs.get(".via_type", "")).lower() == "laser")
                    r_fin = max(0.0, d / 2.0)
                    r_dd = None
                    if plated and tool and tool["drill"] and tool["finish"] and tool["drill"] > tool["finish"] + 0.001:
                        r_dd = tool["drill"] / 2.0
                    h = Hole(x=px, y=py, r_fin=r_fin, plated=plated, kind=kind, layer=L.name, start=L.start,
                             end=L.end, laser=laser, dir_hint=dh, r_drill_data=r_dd, geom_fin=geom, attrs=attrs)
                    if str(L.attrs.get(".cu_base", "no")).lower() in ("yes", "y", "true", "1") and plated:
                        h.fill, h.prot_src = "copper", ".cu_base"
                    if role == "viatype":
                        self._type_holes.append((h, L.name))
                        continue
                    board.holes.append(h)

    def _layer_pair(self, L: Layer) -> Optional[Tuple[str, str]]:
        """Altium names a back drill layer after its layer pair, the drilled
        surface layer and the must-not-cut layer (DRILL_BACKDRILLS_SLOT-GND2),
        while its START/END span may stop one layer short of the must-not-cut
        layer (Altium 26). -> the two copper layer names, or None."""
        cu = [x.name for x in self.layers if x.kind == "copper" and x.context == "BOARD"]
        up = L.name.upper()
        best = None
        for a in cu:
            for b in cu:
                if a == b or not up.endswith(f"_{a}-{b}".upper()):
                    continue
                if best is None or len(a + b) > len(best[0] + best[1]):
                    best = (a, b)
        return best

    def _via_type_layers(self, board: Board):
        """IPC-4761 via types from Altium's per-type drill layers (DRILL_TYPE_7):
        the type goes to the plated hole of the other drill layers at each
        position; a hole that is only on such a layer is added. 'a' types are
        single-sided and taken as the top side (the layer does not say)."""
        if not self._type_holes:
            return
        fm, pd = self.opts.fill_material, self.opts.plug_depth
        idx = {}
        for h in board.holes:
            if h.plated:
                idx.setdefault((round(h.x, 2), round(h.y, 2)), []).append(h)
        stats: Dict[str, list] = {}
        for h_new, lname in self._type_holes:
            n, ab = via_type_layer(lname)
            top, bot = (n, n) if n in (5, 7) or ab != "a" else (n, 5 if n == 6 else 0)
            found = [h for k in ((round(h_new.x, 2), round(h_new.y, 2)),) for h in idx.get(k, ())
                     if math.hypot(h.x - h_new.x, h.y - h_new.y) <= 0.01 and abs(h.r_fin - h_new.r_fin) <= 0.005]
            st = stats.setdefault(lname, [0, 0, n, ab])
            if not found:
                board.holes.append(h_new)
                found = [h_new]
                st[1] += 1
            for h in found:
                apply_ipc(h, top, bot, "copper" if h.fill == "copper" else fm, pd,
                          f"ODB++ layer {lname} (IPC-4761 type {_roman(n)}{ab})")
            st[0] += 1
        for lname, (k, added, n, ab) in stats.items():
            board.prot_notes.append(
                f"layer {lname}: IPC-4761 type {_roman(n)}{ab} ({IPC_TYPE_TEXT[n]}"
                + (", single-sided, taken as top" if ab == "a" and n not in (5, 7) else "") + f") for {k} hole(s)"
                + (f", {added} of them only on this layer" if added else ""))

    def _attr_protection(self, board: Board):
        """IPC-4761 via protection from ODB++ feature attributes."""
        fm, pd = self.opts.fill_material, self.opts.plug_depth
        n_attr = 0

        def code(v):
            m = re.search(r"\d", str(v or ""))
            return int(m.group(0)) if m else 0
        for h in board.holes:
            if not h.plated:
                continue
            a = h.attrs or {}
            t, b = a.get(".ipc_via_type_top"), a.get(".ipc_via_type_bottom")
            if t or b:
                apply_ipc(h, code(t), code(b), fm if h.fill != "copper" else "copper", pd, "attributes .ipc_via_type")
                n_attr += 1
            elif a.get(".via_in_pad") is True:
                apply_ipc(h, 7, 7, fm, pd, "attribute .via_in_pad")
                n_attr += 1
            if a.get(".is_capped") is True and not (h.cap_top or h.cap_bot):
                h.cap_top = h.cap_bot = -1.0
                h.fill = h.fill or fm
                h.prot_src = h.prot_src or "attribute .is_capped"
                n_attr += 1
        if n_attr:
            board.prot_notes.append(f"{n_attr} holes: protection from ODB++ feature attributes")

    # .. board ................................................................
    def read(self) -> Board:
        self._setup_layers()
        app = self.info.get("SAVE_APP") or self.info.get("ODB_SOURCE", "unknown source")
        ver = f"{self.info.get('ODB_VERSION_MAJOR', '?')}.{self.info.get('ODB_VERSION_MINOR', '?')}"
        board = Board(name=self.job.name, source=f"ODB++ {ver} ({app}), units {self.units}", layers=self.layers,
                      units=self.units)
        board.step = self.step
        board.other_steps = list(self.steps)
        board.instances = len(self.instances)
        ff = parse_features(self.job.text(f"steps/{self.step}/profile"), self.units)
        board.outline = self.R.render(ff)
        for src_name, attrs in (("job", self.job_attrs), ("step", self.step_attrs)):
            v = attr_float(attrs, ".board_thickness")
            if v and v > 0:
                board.board_thickness = _len_mm(v, attrs.get("units", "MM"), "inch_mm")
                board.board_thickness_src = f"ODB .board_thickness ({src_name})"
                break
        self._drills(board)
        self._attr_protection(board)
        self._via_type_layers(board)
        for L in self.layers:
            if L.context == "BOARD" and L.kind == "rout":
                board.routs.append(RoutCut(region=(lambda L=L: self.raw_image(L)), layer=L.name,
                                           start=L.start, end=L.end))
        board.counter_flags = odb_counter_flags(self.job)
        board.after_build = lambda: report_surfaces(self.R.surf_stats)
        board.report_extra = self._report_extra
        return board

    def _report_extra(self):
        if self.R.approx_used:
            warn("approximated symbol types (verify visually): " + ", ".join(sorted(self.R.approx_used)))


def read(path: str, opts) -> Board:
    """ODB++ product model (folder, .tgz/.tar or .zip) -> Board."""
    return OdbReader(path, opts).read()


def apply_via_types(path: str, opts, board: Board) -> List[str]:
    """ODB++ next to an IPC-2581 primary: the IPC-4761 via types of Altium's
    per-type drill layers (DRILL_TYPE_7) for the board's plated holes at the
    same positions. IPC-2581 has only the Filling/Capping markers."""
    r = OdbReader(path, opts)
    r.read()
    if not r._type_holes:
        return []
    fm, pd = opts.fill_material, opts.plug_depth
    holes = [h for h in board.holes if h.plated]
    idx: Dict[tuple, list] = {}
    for h in holes:
        idx.setdefault((round(h.x, 1), round(h.y, 1)), []).append(h)

    def find(x, y):
        out = []
        for i in (-0.1, 0.0, 0.1):
            for j in (-0.1, 0.0, 0.1):
                for h in idx.get((round(x + i, 1), round(y + j, 1)), ()):
                    if math.hypot(h.x - x, h.y - y) <= 0.01 and h not in out:
                        out.append(h)
        return out
    stats: Dict[str, list] = {}
    for hn, lname in r._type_holes:
        n, ab = via_type_layer(lname)
        top, bot = (n, n) if n in (5, 7) or ab != "a" else (n, 5 if n == 6 else 0)
        st = stats.setdefault(lname, [0, 0, n, ab])
        found = [h for h in find(hn.x, hn.y) if abs(h.r_fin - hn.r_fin) <= 0.005]
        if not found:
            st[1] += 1
        for h in found:
            apply_ipc(h, top, bot, "copper" if h.fill == "copper" else fm, pd,
                      f"ODB++ layer {lname} (IPC-4761 type {_roman(n)}{ab})")
            st[0] += 1
    label = os.path.basename(path.rstrip("/\\"))
    return [f"ODB++ {label}, layer {lname}: IPC-4761 type {_roman(n)}{ab} ({IPC_TYPE_TEXT[n]}) for {k} hole(s)"
            + (f"; {miss} position(s) without a plated hole here" if miss else "")
            for lname, (k, miss, n, ab) in stats.items()]
