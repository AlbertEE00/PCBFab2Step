# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Input discovery: what was given on the command line (files, a fab folder,
a plain folder) -> the primary source, the secondary sources and the config
file. Nothing is hard-coded to one layout: a single ODB++ archive or IPC-2581
file always works, and a folder is searched in its standard subfolders
(ODB++/, IPC2581/, NCDRILL/, GERBER/) and in itself."""

from __future__ import annotations

import os
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import CONFIG_NAME
from .ipc2581 import is_ipc2581

ODB_ARCHIVES = (".tgz", ".tar.gz", ".tar", ".zip", ".gz")
IPC_FILES = (".cvg", ".xml")
SUBDIRS: Dict[str, tuple] = {
    "odb": ("odb++", "odb", "odbpp", "odb_pp"),
    "ipc2581": ("ipc2581", "ipc-2581", "ipc_2581", "ipc"),
    "ncdrill": ("ncdrill", "nc_drill", "nc-drill", "nc drill", "nc", "drill"),
    "gerber": ("gerber", "gerbers"),
    "step": ("step", "3d", "mcad"),                   # Altium's STEP export: its components are added
    "export": ("export", "exports"),                  # our own output
}
ALL_SUBDIRS = {n for v in SUBDIRS.values() for n in v}
LABEL = {"odb": "ODB++", "ipc2581": "IPC-2581"}


@dataclass
class Sources:
    odb: List[str] = field(default_factory=list)       # ODB++ candidates
    ipc: List[str] = field(default_factory=list)       # IPC-2581 candidates
    explicit: Dict[str, List[str]] = field(default_factory=dict)
    fab: Optional[str] = None                          # fab folder
    config: Optional[str] = None
    ncdrill_dirs: List[str] = field(default_factory=list)
    gerber: bool = False
    step_files: List[str] = field(default_factory=list)   # ECAD STEP exports (STEP/), newest first
    notes: List[str] = field(default_factory=list)
    primary: str = ""
    primary_path: str = ""
    secondary_ipc: Optional[str] = None


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
        return "ipc2581"
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


def _odb_job_dir(p: str) -> bool:
    """p holds an ODB++ job: matrix/matrix in it, or in its only subfolder
    (an extracted archive with a wrapper folder such as 'odb')."""
    if _is_odb_root(p):
        return True
    try:
        kids = [os.path.join(p, k) for k in os.listdir(p) if os.path.isdir(os.path.join(p, k))]
    except OSError:
        return False
    return len(kids) == 1 and _is_odb_root(kids[0])


def kind_of(path: str) -> Optional[str]:
    """odb, ipc2581, ncdrill, config, folder or None."""
    if os.path.isdir(path):
        if _is_odb_root(path):
            return "odb"
        if os.path.isfile(os.path.join(path, CONFIG_NAME)):
            return "folder"
        if _odb_job_dir(path) and not any(_subdir(path, k) for k in ("ipc2581", "ncdrill", "gerber")):
            return "odb"
        return "folder"
    if not os.path.isfile(path):
        return None
    low = path.lower()
    if low.endswith(".toml"):
        return "config"
    if low.endswith(".drr"):
        return "ncdrill"
    if low.endswith(IPC_FILES):
        return "ipc2581" if is_ipc2581(path) else None
    if low.endswith(".zip"):
        return _zip_kind(path)
    if low.endswith(ODB_ARCHIVES):
        return _tar_kind(path)
    return None


def _subdir(folder: str, kind: str) -> Optional[str]:
    try:
        entries = os.listdir(folder)
    except OSError:
        return None
    for e in entries:
        if e.lower() in SUBDIRS[kind] and os.path.isdir(os.path.join(folder, e)):
            return os.path.join(folder, e)
    return None


def _scan(folder: str, kind: str, inner: bool = False) -> List[str]:
    """Inputs of one kind directly in a folder (inner: the folder is the fab
    folder's subfolder for that kind, where an extracted job may be called 'odb')."""
    out = []
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return out
    for e in entries:
        p = os.path.join(folder, e)
        if os.path.isdir(p):
            # fab subfolders are searched on their own
            if kind == "odb" and (inner or e.lower() not in ALL_SUBDIRS) and _odb_job_dir(p):
                out.append(p)
            continue
        low = e.lower()
        if kind == "odb" and low.endswith(ODB_ARCHIVES) and kind_of(p) == "odb":
            out.append(p)
        elif kind == "ipc2581" and (low.endswith(IPC_FILES) or low.endswith(".zip")) and kind_of(p) == "ipc2581":
            out.append(p)
    return out


def _config_near(path: str) -> Optional[str]:
    """config.toml of the fab folder an input file sits in (the folder itself,
    or its parent when the file sits in a standard subfolder)."""
    d = os.path.dirname(os.path.abspath(path))
    c = os.path.join(d, CONFIG_NAME)
    if os.path.isfile(c):
        return c
    if os.path.basename(d).lower() in ALL_SUBDIRS:
        c = os.path.join(os.path.dirname(d), CONFIG_NAME)
        if os.path.isfile(c):
            return c
    return None


def discover(inputs: List[str], config: Optional[str] = None, no_config: bool = False) -> Sources:
    """Classify the inputs, find the fab folder and the config file."""
    s = Sources(explicit={"odb": [], "ipc2581": [], "ncdrill": []})
    folders = []
    for p in inputs:
        k = kind_of(p)
        if k is None:
            if not os.path.exists(p):
                raise SystemExit(f"input '{p}' not found")
            raise SystemExit(f"input '{p}': not an ODB++ product model, IPC-2581 file, fab folder or config.toml")
        if k == "folder":
            folders.append(p)
        elif k == "config":
            s.config = s.config or p
            folders.append(os.path.dirname(os.path.abspath(p)))
        else:
            s.explicit[k].append(p)
    if len(folders) > 1:
        raise SystemExit("give one fab folder (or files)")
    if not folders:
        # a file inside a fab folder's subfolder (FAB/ODB++/board.tgz): use that fab folder too
        first = (s.explicit["odb"] + s.explicit["ipc2581"])[:1]
        if first:
            d = os.path.dirname(os.path.abspath(first[0].rstrip("/\\")))
            parent = os.path.dirname(d)
            if os.path.basename(d).lower() in ALL_SUBDIRS and os.path.isfile(os.path.join(parent, CONFIG_NAME)):
                folders.append(parent)
    if folders:
        f = folders[0]
        s.fab = f
        for kind in ("odb", "ipc2581"):
            sub = _subdir(f, kind)
            if sub and kind == "odb" and _is_odb_root(sub):
                found = [sub]                     # job extracted straight into ODB++/
            else:
                found = (_scan(sub, kind, inner=True) if sub else []) + _scan(f, kind)
            if kind == "odb":
                s.odb = found
            else:
                s.ipc = found
        nd = _subdir(f, "ncdrill")
        if nd:
            s.ncdrill_dirs.append(nd)
        gd = _subdir(f, "gerber")
        s.gerber = bool(gd and os.listdir(gd))
        sd = _subdir(f, "step")
        if sd:
            s.step_files = sorted((os.path.join(sd, e) for e in os.listdir(sd)
                                   if e.lower().endswith((".step", ".stp"))), key=os.path.getmtime, reverse=True)
        if s.config is None and os.path.isfile(os.path.join(f, CONFIG_NAME)):
            s.config = os.path.join(f, CONFIG_NAME)
    s.odb = s.explicit["odb"] + [p for p in s.odb if p not in s.explicit["odb"]]
    s.ipc = s.explicit["ipc2581"] + [p for p in s.ipc if p not in s.explicit["ipc2581"]]
    if s.config is None and not folders:
        first = (s.explicit["odb"] + s.explicit["ipc2581"])[:1]
        if first:
            s.config = _config_near(first[0])
    if config:
        s.config = config
    if no_config:
        s.config = None
    return s


def _newest(paths: List[str]) -> str:
    return max(paths, key=lambda p: os.path.getmtime(p))


def choose(s: Sources, want: Optional[str], ipc_opt: Optional[str]) -> Sources:
    """Pick the primary source (want: odb / ipc2581 / None) and the IPC-2581
    file used as secondary data next to ODB++ (ipc_opt: a path, 'off' or None
    for automatic)."""
    if want == "odb" and not s.odb:
        raise SystemExit("--source odb: no ODB++ input found" + (f" in {s.fab}" if s.fab else ""))
    if want == "ipc2581" and not s.ipc:
        raise SystemExit("--source ipc2581: no IPC-2581 input found" + (f" in {s.fab}" if s.fab else ""))
    # explicit inputs decide before anything found in folders
    ex_odb, ex_ipc = s.explicit["odb"], s.explicit["ipc2581"]
    if want:
        kind = want
    elif ex_odb or ex_ipc:
        kind = "odb" if ex_odb else "ipc2581"
    else:
        kind = "odb" if s.odb else "ipc2581" if s.ipc else None
    if kind is None:
        where = f" in {s.fab}" if s.fab else ""
        if s.gerber:
            raise SystemExit(f"only Gerber data{where}: Gerber input is not supported yet (use ODB++ or IPC-2581)")
        raise SystemExit(f"no ODB++ or IPC-2581 input found{where}")
    cands = s.odb if kind == "odb" else s.ipc
    explicit = s.explicit["odb" if kind == "odb" else "ipc2581"]
    if len(explicit) > 1:
        raise SystemExit(f"several {LABEL[kind]} inputs given: give one")
    if explicit:
        s.primary_path = explicit[0]
    elif len(cands) > 1:
        s.primary_path = _newest(cands)
        s.notes.append(f"{len(cands)} {LABEL[kind]} inputs found; using the newest, "
                       f"{os.path.basename(s.primary_path)}")
    else:
        s.primary_path = cands[0]
    s.primary = kind
    if kind == "odb":
        if ipc_opt and ipc_opt.lower() in ("off", "none", "no"):
            return s
        if ipc_opt:
            if kind_of(ipc_opt) != "ipc2581":
                raise SystemExit(f"--ipc2581 '{ipc_opt}': not an IPC-2581 file")
            s.secondary_ipc = ipc_opt
        elif s.explicit["ipc2581"]:
            s.secondary_ipc = s.explicit["ipc2581"][0]
        elif s.fab and s.ipc:
            s.secondary_ipc = _newest(s.ipc) if len(s.ipc) > 1 else s.ipc[0]
        else:
            stem = _stem(s.primary_path)
            folder = os.path.dirname(os.path.abspath(s.primary_path))
            same = [p for p in _scan(folder, "ipc2581") if _stem(p) == stem]
            if same:
                s.secondary_ipc = same[0]
                s.notes.append(f"IPC-2581 {os.path.basename(same[0])} found beside the ODB++ input (same name); "
                               "--ipc2581 off to leave it out")
    return s


def _stem(path: str) -> str:
    base = os.path.basename(os.path.normpath(path)).lower()
    for ext in (".tar.gz",) + ODB_ARCHIVES + IPC_FILES:
        if base.endswith(ext):
            return base[: -len(ext)]
    return os.path.splitext(base)[0]
