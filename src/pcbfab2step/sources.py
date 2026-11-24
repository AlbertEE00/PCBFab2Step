# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Input discovery: the paths given on the command line -> the fab outputs
to read and where the results go.

No layout is assumed. A folder is searched with its subfolders; a file brings
the other outputs in its folder, and in the parent folder when its folder is
one of the usual output folders (ODB++, IPC-2581, NC Drill, Gerber, STEP),
so FAB/IPC-2581/board.cvg also finds FAB/STEP/board.step. Found are:

    odb      ODB++ product model (job folder or .tgz/.tar/.zip)
    ipc      IPC-2581 file (.cvg/.xml, also zipped)
    drill    NC drill output (.DRR report and its drill files)
    gerber   Gerber files (found, not read yet)
    step     the ECAD's STEP export, whose components are added

The first geometry source (ODB++ before IPC-2581) gives the board; the others
add what it lacks. Results go to PCBFab2Step/ in the folder given, or in the
folder of the file given; that folder is never searched.
"""

from __future__ import annotations

import os
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import CONFIG_NAME
from .ipc2581 import is_ipc2581

OUTPUT_DIR = "PCBFab2Step"
ODB_ARCHIVES = (".tgz", ".tar.gz", ".tar", ".zip", ".gz")
IPC_FILES = (".cvg", ".xml")
STEP_FILES = (".step", ".stp")
GERBER_FILES = (".gbr", ".ger", ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gtp", ".gbp", ".gko", ".gm1",
                ".gml", ".pho", ".art")
# folders an ECAD writes its outputs to: a file in one of them brings its siblings too
OUTPUT_FOLDERS = {"odb++", "odb", "odbpp", "odb_pp", "ipc2581", "ipc-2581", "ipc_2581", "ipc", "ncdrill", "nc drill",
                  "nc_drill", "nc-drill", "nc", "drill", "gerber", "gerbers", "step", "3d", "mcad"}
SKIP = {OUTPUT_DIR.lower(), "export", "exports", "history", "__previews", "__pycache__", ".git", ".svn", ".venv",
        "venv", "node_modules"}
KINDS = ("odb", "ipc", "gerber", "drill", "step")
GEOMETRY = ("odb", "ipc", "gerber")
ALIASES = {"odb": "odb", "odb++": "odb", "odbpp": "odb", "ipc": "ipc", "ipc2581": "ipc", "ipc-2581": "ipc",
           "gerber": "gerber", "gerbers": "gerber", "drill": "drill", "ncdrill": "drill", "nc": "drill",
           "nc-drill": "drill", "step": "step", "components": "step"}
LABEL = {"odb": "ODB++", "ipc": "IPC-2581", "gerber": "Gerber", "drill": "NC drill", "step": "STEP"}
MAX_DEPTH = 4


@dataclass
class Sources:
    found: Dict[str, List[str]] = field(default_factory=lambda: {k: [] for k in KINDS})
    explicit: Dict[str, List[str]] = field(default_factory=lambda: {k: [] for k in KINDS})
    roots: List[str] = field(default_factory=list)
    out_dir: str = ""
    config: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    # chosen
    use: List[str] = field(default_factory=list)
    primary: str = ""
    primary_path: str = ""
    ipc_hints: Optional[str] = None        # IPC-2581 next to an ODB++ primary: counterhole types
    odb_types: Optional[str] = None        # ODB++ next to an IPC-2581 primary: via types
    drill: List[str] = field(default_factory=list)
    step: Optional[str] = None


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def _zip_kind(path: str) -> Optional[str]:
    try:
        with zipfile.ZipFile(path) as z:
            names = [n.replace("\\", "/").lower() for n in z.namelist()]
    except (zipfile.BadZipFile, OSError):
        return None
    if any(re.search(r"(^|/)matrix/matrix(\.z|\.gz)?$", n) for n in names):
        return "odb"
    if any(n.endswith((".tgz", ".tar.gz", ".tar")) for n in names):
        return "odb"
    ipcs = [n for n in names if n.endswith(IPC_FILES)]
    if len(ipcs) == 1 and is_ipc2581(path):
        return "ipc"
    return None


def _tar_kind(path: str) -> Optional[str]:
    try:
        with tarfile.open(path, "r:*") as t:
            for m in t:
                n = m.name.replace("\\", "/").lower()
                if re.search(r"(^|/)matrix/matrix(\.z|\.gz)?$", n):
                    return "odb"
    except (tarfile.TarError, OSError, EOFError):
        return None
    return None


def _is_odb_root(p: str) -> bool:
    return os.path.isfile(os.path.join(p, "matrix", "matrix"))


def kind_of(path: str) -> Optional[str]:
    """odb, ipc, drill, gerber, step, config, folder, or None."""
    if os.path.isdir(path):
        return "odb" if _is_odb_root(path) else "folder"
    if not os.path.isfile(path):
        return None
    low = path.lower()
    if low.endswith(".toml"):
        return "config"
    if low.endswith(".drr"):
        return "drill"
    if low.endswith(STEP_FILES):
        return "step"
    if low.endswith(GERBER_FILES):
        return "gerber"
    if low.endswith(IPC_FILES):
        return "ipc" if is_ipc2581(path) else None
    if low.endswith(".zip"):
        return _zip_kind(path)
    if low.endswith(ODB_ARCHIVES):
        return _tar_kind(path)
    return None


def _ours(path: str) -> bool:
    """A STEP written by pcbfab2step (never an input)."""
    try:
        with open(path, "rb") as f:
            return b"by pcbfab2step" in f.read(4096)
    except OSError:
        return False


def _walk(root: str, found: Dict[str, List[str]]):
    """Everything below root (MAX_DEPTH levels), our own output folder skipped."""
    root = os.path.abspath(root)
    base = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if _is_odb_root(dirpath):
            found["odb"].append(dirpath)
            dirnames[:] = []
            continue
        if dirpath.count(os.sep) - base >= MAX_DEPTH:
            dirnames[:] = []
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP and not d.startswith("."))
        drr = False
        for f in sorted(filenames):
            p = os.path.join(dirpath, f)
            low = f.lower()
            if low.endswith(".drr"):
                drr = True
            elif low.endswith(STEP_FILES):
                if not _ours(p):
                    found["step"].append(p)
            elif low.endswith(GERBER_FILES):
                found["gerber"].append(p)
            elif low.endswith(IPC_FILES + ODB_ARCHIVES):
                k = kind_of(p)
                if k in ("odb", "ipc"):
                    found[k].append(p)
        if drr:
            found["drill"].append(dirpath)


def discover(inputs: List[str], config: Optional[str] = None, no_config: bool = False) -> Sources:
    s = Sources()
    inputs = inputs or ["."]
    for p in inputs:
        k = kind_of(p)
        if k is None:
            if not os.path.exists(p):
                raise SystemExit(f"input '{p}' not found")
            raise SystemExit(f"input '{p}': not fab data (ODB++, IPC-2581, NC drill report, STEP, config.toml) "
                             "or a folder")
        ap = os.path.abspath(p)
        if k == "config":
            config = config or ap
            continue
        if k == "folder" or (k == "odb" and os.path.isdir(ap)):
            if k == "odb":
                s.explicit["odb"].append(ap)
            root = ap
        else:
            s.explicit[k].append(ap if k != "drill" else ap)
            root = os.path.dirname(ap)
        if not s.out_dir:
            s.out_dir = os.path.join(root if (k == "folder" or not os.path.isdir(ap)) else os.path.dirname(ap),
                                     OUTPUT_DIR)
        if os.path.basename(root).lower() in OUTPUT_FOLDERS:
            root = os.path.dirname(root)              # the usual output folders: their siblings count
        if root not in s.roots:
            s.roots.append(root)
    for r in s.roots:
        _walk(r, s.found)
    for k in KINDS:
        seen, lst = set(), []
        for p in s.explicit[k] + s.found[k]:
            if p not in seen:
                seen.add(p)
                lst.append(p)
        s.found[k] = lst
    if not no_config:
        if config:
            s.config = config
        else:
            for r in s.roots:
                c = os.path.join(r, CONFIG_NAME)
                if os.path.isfile(c):
                    s.config = c
                    break
    return s


# --------------------------------------------------------------------------
# choice
# --------------------------------------------------------------------------


def parse_use(spec: Optional[str]) -> Optional[List[str]]:
    """'odb+drill+step' / 'ipc,step' -> kinds in order; None or 'auto' -> None."""
    if spec is None or str(spec).strip().lower() in ("", "auto", "all", "default"):
        return None
    out = []
    for t in re.split(r"[+,\s]+", str(spec).strip().lower()):
        if not t:
            continue
        if t not in ALIASES:
            raise SystemExit(f"--sources: unknown source '{t}' (odb, ipc, gerber, drill, step)")
        if ALIASES[t] not in out:
            out.append(ALIASES[t])
    return out


def _stem(path: str) -> str:
    base = os.path.basename(os.path.normpath(path)).lower()
    for ext in (".tar.gz",) + ODB_ARCHIVES + IPC_FILES + STEP_FILES:
        if base.endswith(ext):
            return base[: -len(ext)]
    return os.path.splitext(base)[0]


def _pick(s: Sources, kind: str, stem: str = "") -> Optional[str]:
    """One input of a kind: the one given, else the only one found, else the
    one named like the primary (stem), else for the primary the newest;
    ODB++ prefers the extracted job folder to its archives. With several
    candidates none named like the board, a secondary source is not used."""
    if s.explicit[kind]:
        if len(s.explicit[kind]) > 1 and kind in GEOMETRY + ("step",):
            raise SystemExit(f"several {LABEL[kind]} inputs given: give one")
        return s.explicit[kind][0]
    cands = s.found[kind]
    if len(cands) == 1:
        return cands[0]
    pool = cands
    if stem:
        same = [p for p in cands if _stem(p) == stem]
        if not same:
            s.notes.append(f"{len(cands)} {LABEL[kind]} files found, none named like the board ({stem}); "
                           f"not used (give one as an input)")
            return None
        pool = same
    if kind == "odb":
        folders = [p for p in pool if os.path.isdir(p)]
        pool = folders or pool
    pick = max(pool, key=lambda p: os.path.getmtime(p))
    s.notes.append(f"{len(cands)} {LABEL[kind]} inputs found; using {os.path.relpath(pick, s.roots[0])}")
    return pick


def choose(s: Sources, use_spec: Optional[str]) -> Sources:
    want = parse_use(use_spec)
    where = f" in {s.roots[0]}" if s.roots else ""
    if want is None:
        s.use = [k for k in KINDS if s.found[k] and k != "gerber"]
        geo = [k for k in GEOMETRY if s.found[k]]
        # a geometry file given explicitly decides the primary
        exp = [k for k in GEOMETRY if s.explicit[k]]
        primary = exp[0] if exp else (geo[0] if geo else None)
    else:
        for k in want:
            if not s.found[k]:
                raise SystemExit(f"--sources {k}: no {LABEL[k]} found{where}")
        s.use = want
        primary = next((k for k in want if k in GEOMETRY), None)
    if primary is None:
        if s.found["gerber"]:
            raise SystemExit(f"only Gerber data{where}: reading Gerber is not supported yet (use ODB++ or IPC-2581)")
        raise SystemExit(f"no ODB++ or IPC-2581 found{where}")
    if primary == "gerber":
        raise SystemExit("--sources gerber: reading Gerber is not supported yet (use ODB++ or IPC-2581)")
    s.primary = primary
    s.primary_path = _pick(s, primary)
    stem = _stem(s.primary_path)
    if primary == "odb" and "ipc" in s.use:
        s.ipc_hints = _pick(s, "ipc", stem)
    if primary == "ipc" and "odb" in s.use:
        s.odb_types = _pick(s, "odb", stem)
    if "drill" in s.use:
        s.drill = list(s.found["drill"])
    if "step" in s.use:
        s.step = _pick(s, "step", stem)
    if s.found["gerber"] and "gerber" not in s.use:
        s.notes.append(f"{len(s.found['gerber'])} Gerber file(s) found; not read (Gerber is not supported yet)")
    return s
