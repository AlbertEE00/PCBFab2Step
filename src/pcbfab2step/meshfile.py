# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Mesh files of the model or of one layer: glTF (.glb, .gltf), OBJ (+ .mtl),
FBX (binary 7.4), STL, PLY and 3MF.

The meshes are the viewer's (scene.py): one mesh per body of the STEP model,
with its name, colour and a material by kind (copper is metal, laminate and
solder mask are not). Units and axes follow each format's convention:

    glb, gltf   metres, +Y up (the glTF standard)
    obj         millimetres, +Y up (what most importers expect)
    fbx         millimetres (UnitScaleFactor 0.1), +Z up declared in the file
    stl, ply    millimetres, +Z up (3D printing)
    3mf         millimetres, +Z up (unit="millimeter")

Faces are flat-shaded (no normals: glTF and OBJ importers then shade each
triangle flat; STL and FBX carry face normals).
"""

from __future__ import annotations

import io
import json
import os
import re
import struct
import zipfile
import zlib
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import __version__

FORMATS = ("glb", "gltf", "obj", "fbx", "stl", "ply", "3mf")
LABELS = {"glb": "glTF binary", "gltf": "glTF", "obj": "OBJ + MTL", "fbx": "FBX", "stl": "STL", "ply": "PLY",
          "3mf": "3MF"}

# metalness, roughness by kind (as the viewer draws them)
LOOK = {"copper": (0.85, 0.34), "plating": (0.85, 0.34), "cap": (0.85, 0.34), "finish": (0.95, 0.22),
        "paste": (0.6, 0.5), "mask": (0.0, 0.48), "silk": (0.0, 0.75), "dielectric": (0.0, 0.85),
        "core": (0.0, 0.85), "board": (0.0, 0.85), "fill": (0.0, 0.6), "plug": (0.0, 0.6), "layer": (0.0, 0.7),
        "component": (0.1, 0.6)}


def format_of(path: str, fmt: Optional[str] = None) -> str:
    f = (fmt or os.path.splitext(path)[1].lstrip(".")).lower()
    if f not in FORMATS:
        raise SystemExit(f"{path}: not a mesh format ({', '.join(FORMATS)})")
    return f


def write(path: str, parts: Sequence, title: str, fmt: Optional[str] = None) -> List[str]:
    """Write scene parts (scene.Part: name, kind, color, mesh, path) as a mesh
    file; returns the files written (OBJ and glTF write two)."""
    fmt = format_of(path, fmt)
    parts = [p for p in parts if p.mesh is not None and len(p.mesh.tri)]
    if not parts:
        raise SystemExit("nothing to write: no bodies")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    return {"glb": _glb, "gltf": _gltf, "obj": _obj, "fbx": _fbx, "stl": _stl, "ply": _ply, "3mf": _3mf}[fmt](
        path, parts, title)


# --------------------------------------------------------------------------
# shared
# --------------------------------------------------------------------------

def _unique_names(parts) -> List[str]:
    seen: Dict[str, int] = {}
    out = []
    for p in parts:
        base = p.name or p.kind or "body"
        k = seen.get(base, 0)
        seen[base] = k + 1
        out.append(base if k == 0 else f"{base} ({k + 1})")
    return out


def _materials(parts) -> Tuple[List[dict], List[int]]:
    """One material per kind and colour: [{name, color, metal, rough, opacity}], index per part."""
    mats: List[dict] = []
    keys: Dict[tuple, int] = {}
    idx = []
    for p in parts:
        c = tuple(round(float(v), 4) for v in p.color)
        key = (p.kind, c, round(float(getattr(p, "opacity", 1.0)), 3))
        if key not in keys:
            metal, rough = LOOK.get(p.kind, LOOK["component"])
            name = re.sub(r"[^A-Za-z0-9_]+", "_", f"{p.kind or 'body'}_{'%02x%02x%02x' % tuple(round(v * 255) for v in c)}")
            keys[key] = len(mats)
            mats.append(dict(name=name, color=c, metal=metal, rough=rough, opacity=key[2]))
        idx.append(keys[key])
    return mats, idx


def _y_up(pos: np.ndarray) -> np.ndarray:
    """(x, y, z) z-up -> (x, z, -y) y-up."""
    return np.column_stack([pos[:, 0], pos[:, 2], -pos[:, 1]])


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _face_normals(pos: np.ndarray, tri: np.ndarray) -> np.ndarray:
    a, b, c = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
    n = np.cross(b - a, c - a)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    return n / np.where(ln > 0, ln, 1.0)


# --------------------------------------------------------------------------
# glTF 2.0
# --------------------------------------------------------------------------

def _gltf_doc(parts, title) -> Tuple[dict, bytes]:
    mats, midx = _materials(parts)
    names = _unique_names(parts)
    blob = io.BytesIO()
    views, accessors, meshes, nodes = [], [], [], []

    def add_view(data: bytes, target: int) -> int:
        while blob.tell() % 4:
            blob.write(b"\0")
        views.append(dict(buffer=0, byteOffset=blob.tell(), byteLength=len(data), target=target))
        blob.write(data)
        return len(views) - 1
    for p, name, mi in zip(parts, names, midx):
        pos = (_y_up(p.mesh.pos) * 0.001).astype("<f4")
        tri = p.mesh.tri.astype("<u4")
        pv = add_view(pos.tobytes(), 34962)
        iv = add_view(tri.tobytes(), 34963)
        accessors.append(dict(bufferView=pv, componentType=5126, count=len(pos), type="VEC3",
                              min=[float(v) for v in pos.min(axis=0)], max=[float(v) for v in pos.max(axis=0)]))
        accessors.append(dict(bufferView=iv, componentType=5125, count=int(tri.size), type="SCALAR"))
        meshes.append(dict(name=name, primitives=[dict(attributes=dict(POSITION=len(accessors) - 2),
                                                        indices=len(accessors) - 1, material=mi)]))
        nodes.append(dict(name=name, mesh=len(meshes) - 1))
    # the assemblies of the STEP model as nodes: root > PCB / Components top > R1 > body
    groups: Dict[tuple, int] = {}
    root = dict(name=title, children=[])
    nodes_all = [root] + nodes

    def group(path: tuple) -> dict:
        if not path:
            return root
        if path not in groups:
            parent = group(path[:-1])
            nodes_all.append(dict(name=path[-1], children=[]))
            groups[path] = len(nodes_all) - 1
            parent.setdefault("children", []).append(groups[path])
        return nodes_all[groups[path]]
    for k, p in enumerate(parts):
        group(tuple(getattr(p, "path", ()) or ())).setdefault("children", []).append(k + 1)
    materials = []
    for m in mats:
        rgb = [_srgb_to_linear(v) for v in m["color"]]
        mat = dict(name=m["name"], pbrMetallicRoughness=dict(baseColorFactor=rgb + [m["opacity"]],
                                                             metallicFactor=m["metal"], roughnessFactor=m["rough"]))
        if m["opacity"] < 1:
            mat["alphaMode"] = "BLEND"
        materials.append(mat)
    data = blob.getvalue()
    doc = dict(asset=dict(version="2.0", generator=f"pcbfab2step {__version__}"), scene=0,
               scenes=[dict(name=title, nodes=[0])], nodes=nodes_all, meshes=meshes, materials=materials,
               accessors=accessors, bufferViews=views, buffers=[dict(byteLength=len(data))])
    return doc, data


def _glb(path, parts, title) -> List[str]:
    doc, data = _gltf_doc(parts, title)
    js = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    data += b"\0" * (-len(data) % 4)
    total = 12 + 8 + len(js) + 8 + len(data)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(data), 0x004E4942) + data)
    return [path]


def _gltf(path, parts, title) -> List[str]:
    doc, data = _gltf_doc(parts, title)
    bin_path = os.path.splitext(path)[0] + ".bin"
    doc["buffers"][0]["uri"] = os.path.basename(bin_path)
    with open(bin_path, "wb") as f:
        f.write(data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    return [path, bin_path]


# --------------------------------------------------------------------------
# OBJ + MTL
# --------------------------------------------------------------------------

def _obj(path, parts, title) -> List[str]:
    mats, midx = _materials(parts)
    names = _unique_names(parts)
    mtl_path = os.path.splitext(path)[0] + ".mtl"
    with open(mtl_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}: materials, pcbfab2step {__version__}\n")
        for m in mats:
            r, g, b = m["color"]
            spec = [v * 0.6 for v in (r, g, b)] if m["metal"] > 0.5 else [0.04, 0.04, 0.04]
            ns = max(2.0, (1.0 - m["rough"]) ** 2 * 900.0)
            f.write(f"\nnewmtl {m['name']}\nKa 0 0 0\nKd {r:.4f} {g:.4f} {b:.4f}\n"
                    f"Ks {spec[0]:.4f} {spec[1]:.4f} {spec[2]:.4f}\nNs {ns:.1f}\nd {m['opacity']:.3f}\nillum 2\n"
                    f"Pr {m['rough']:.3f}\nPm {m['metal']:.3f}\n")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# {title}: millimetres, +Y up; pcbfab2step {__version__}\nmtllib {os.path.basename(mtl_path)}\n")
        off = 1
        for p, name, mi in zip(parts, names, midx):
            f.write(f"o {_obj_name(name)}\nusemtl {mats[mi]['name']}\ns off\n")
            pos = _y_up(p.mesh.pos)
            buf = io.StringIO()
            np.savetxt(buf, pos, fmt="v %.6f %.6f %.6f")
            np.savetxt(buf, p.mesh.tri + off, fmt="f %d %d %d")
            f.write(buf.getvalue())
            off += len(pos)
    return [path, mtl_path]


def _obj_name(name: str) -> str:
    return re.sub(r"\s+", "_", name.strip()) or "body"


# --------------------------------------------------------------------------
# STL (binary, all bodies in one) and PLY (binary, per-vertex colours)
# --------------------------------------------------------------------------

def _stl(path, parts, title) -> List[str]:
    tris = sum(len(p.mesh.tri) for p in parts)
    rec = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    with open(path, "wb") as f:
        head = f"{title} (mm) pcbfab2step {__version__}".encode("ascii", "replace")[:80]
        f.write(head + b" " * (80 - len(head)) + struct.pack("<I", tris))
        for p in parts:
            pos, tri = p.mesh.pos, p.mesh.tri
            out = np.zeros(len(tri), dtype=rec)
            out["n"] = _face_normals(pos, tri)
            out["v"] = pos[tri]
            f.write(out.tobytes())
    return [path]


def _ply(path, parts, title) -> List[str]:
    nv = sum(len(p.mesh.pos) for p in parts)
    nf = sum(len(p.mesh.tri) for p in parts)
    head = ("ply\nformat binary_little_endian 1.0\n"
            f"comment {title} (mm, +Z up), pcbfab2step {__version__}\n"
            f"element vertex {nv}\nproperty float x\nproperty float y\nproperty float z\n"
            "property uchar red\nproperty uchar green\nproperty uchar blue\n"
            f"element face {nf}\nproperty list uchar int vertex_indices\nend_header\n")
    vrec = np.dtype([("p", "<f4", 3), ("c", "u1", 3)])
    frec = np.dtype([("n", "u1"), ("i", "<i4", 3)])
    with open(path, "wb") as f:
        f.write(head.encode("ascii", "replace"))
        for p in parts:
            v = np.zeros(len(p.mesh.pos), dtype=vrec)
            v["p"] = p.mesh.pos
            v["c"] = [round(c * 255) for c in p.color]
            f.write(v.tobytes())
        off = 0
        for p in parts:
            fa = np.zeros(len(p.mesh.tri), dtype=frec)
            fa["n"] = 3
            fa["i"] = p.mesh.tri + off
            f.write(fa.tobytes())
            off += len(p.mesh.pos)
    return [path]


# --------------------------------------------------------------------------
# 3MF
# --------------------------------------------------------------------------

def _xml(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _3mf(path, parts, title) -> List[str]:
    mats, midx = _materials(parts)
    names = _unique_names(parts)
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
              'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
              f'<metadata name="Title">{_xml(title)}</metadata>\n'
              f'<metadata name="Application">pcbfab2step {__version__}</metadata>\n<resources>\n'
              '<basematerials id="1">\n')
    for m in mats:
        hexc = "#%02X%02X%02X%02X" % (tuple(round(v * 255) for v in m["color"]) + (round(m["opacity"] * 255),))
        out.write(f'<base name="{_xml(m["name"])}" displaycolor="{hexc}"/>\n')
    out.write("</basematerials>\n")
    for k, (p, name, mi) in enumerate(zip(parts, names, midx)):
        out.write(f'<object id="{k + 2}" type="model" name="{_xml(name)}" pid="1" pindex="{mi}">\n<mesh>\n<vertices>\n')
        buf = io.StringIO()
        np.savetxt(buf, p.mesh.pos, fmt='<vertex x="%.6f" y="%.6f" z="%.6f"/>')
        out.write(buf.getvalue() + "</vertices>\n<triangles>\n")
        buf = io.StringIO()
        np.savetxt(buf, p.mesh.tri, fmt='<triangle v1="%d" v2="%d" v3="%d"/>')
        out.write(buf.getvalue() + "</triangles>\n</mesh>\n</object>\n")
    out.write("</resources>\n<build>\n")
    for k in range(len(parts)):
        out.write(f'<item objectid="{k + 2}"/>\n')
    out.write("</build>\n</model>\n")
    types = ('<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
             'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
             'relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.'
             '3dmanufacturing-3dmodel+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.'
            'com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", out.getvalue())
    return [path]


# --------------------------------------------------------------------------
# FBX 7.4 binary
# --------------------------------------------------------------------------

_FBX_VERSION = 7400
_FBX_FILE_ID = b"\x28\xb3\x2a\xeb\xb6\x24\xcc\xc2\xbf\xc8\xb0\x2a\xa9\x2b\xfc\xf1"
_FBX_FOOT_ID = b"\xfa\xbc\xab\x09\xd0\xc8\xd4\x66\xb1\x76\xfb\x83\x1c\xf7\x26\x7e"
_FBX_TIME = "1970-01-01 10:00:00:000"


class _Node:
    """A node record: name, properties [(type code, value)], children."""
    __slots__ = ("name", "props", "kids")

    def __init__(self, name: str, *props, kids=None):
        self.name, self.props, self.kids = name, list(props), list(kids or [])

    def add(self, name: str, *props, kids=None) -> "_Node":
        n = _Node(name, *props, kids=kids)
        self.kids.append(n)
        return n


def _fbx_prop(t: str, v) -> bytes:
    if t == "C":
        return b"C" + struct.pack("<B", 1 if v else 0)
    if t == "I":
        return b"I" + struct.pack("<i", v)
    if t == "L":
        return b"L" + struct.pack("<q", v)
    if t == "D":
        return b"D" + struct.pack("<d", v)
    if t == "F":
        return b"F" + struct.pack("<f", v)
    if t in ("S", "R"):
        b = v.encode("utf-8") if isinstance(v, str) else bytes(v)
        return t.encode() + struct.pack("<I", len(b)) + b
    if t in ("d", "f", "i", "l", "b"):
        dt = {"d": "<f8", "f": "<f4", "i": "<i4", "l": "<i8", "b": "u1"}[t]
        a = np.ascontiguousarray(np.asarray(v, dtype=dt)).tobytes()
        if len(a) > 256:
            z = zlib.compress(a, 6)
            return t.encode() + struct.pack("<III", len(v), 1, len(z)) + z
        return t.encode() + struct.pack("<III", len(v), 0, len(a)) + a
    raise ValueError(t)


def _fbx_node(n: _Node, offset: int) -> bytes:
    props = b"".join(_fbx_prop(t, v) for t, v in n.props)
    name = n.name.encode("ascii")
    head_len = 13 + len(name)
    body = b""
    pos = offset + head_len + len(props)
    if n.kids:
        parts = []
        for k in n.kids:
            b = _fbx_node(k, pos)
            parts.append(b)
            pos += len(b)
        body = b"".join(parts) + b"\0" * 13
    elif not n.props:
        body = b"\0" * 13
    end = offset + head_len + len(props) + len(body)
    return struct.pack("<IIIB", end, len(n.props), len(props), len(name)) + name + props + body


def _p70(*items) -> _Node:
    """Properties70 { P: name, type, label, flags, values... }"""
    n = _Node("Properties70")
    for it in items:
        n.add("P", *it)
    return n


def _P(name, typ, label, flags, *vals) -> tuple:
    return (("S", name), ("S", typ), ("S", label), ("S", flags)) + tuple(vals)


def _fbx(path, parts, title) -> List[str]:
    mats, midx = _materials(parts)
    names = _unique_names(parts)
    nid = [1000000]

    def new_id() -> int:
        nid[0] += 1
        return nid[0]
    objects = _Node("Objects")
    conns = _Node("Connections")
    mat_ids = []
    for m in mats:
        i = new_id()
        mat_ids.append(i)
        r, g, b = m["color"]
        mo = objects.add("Material", ("L", i), ("S", f"{m['name']}\x00\x01Material"), ("S", ""))
        mo.add("Version", ("I", 102))
        mo.add("ShadingModel", ("S", "phong"))
        mo.add("MultiLayer", ("I", 0))
        spec = 0.6 if m["metal"] > 0.5 else 0.04
        mo.kids.append(_p70(
            _P("DiffuseColor", "Color", "", "A", ("D", r), ("D", g), ("D", b)),
            _P("DiffuseFactor", "Number", "", "A", ("D", 1.0)),
            _P("SpecularColor", "Color", "", "A", ("D", spec), ("D", spec), ("D", spec)),
            _P("Shininess", "Number", "", "A", ("D", max(2.0, (1.0 - m["rough"]) ** 2 * 100.0))),
            _P("Opacity", "Number", "", "A", ("D", m["opacity"])),
            _P("TransparencyFactor", "Number", "", "A", ("D", 1.0 - m["opacity"]))))
    groups: Dict[tuple, int] = {}

    def group(pth: tuple) -> int:
        if not pth:
            return 0
        if pth not in groups:
            parent = group(pth[:-1])
            gid = new_id()
            groups[pth] = gid
            mo = objects.add("Model", ("L", gid), ("S", f"{pth[-1]}\x00\x01Model"), ("S", "Null"))
            mo.add("Version", ("I", 232))
            mo.kids.append(_p70())
            mo.add("Shading", ("C", True))
            mo.add("Culling", ("S", "CullingOff"))
            conns.add("C", ("S", "OO"), ("L", gid), ("L", parent))
        return groups[pth]
    for p, name, mi in zip(parts, names, midx):
        pos, tri = p.mesh.pos, p.mesh.tri
        gid, mid = new_id(), new_id()
        geo = objects.add("Geometry", ("L", gid), ("S", "\x00\x01Geometry"), ("S", "Mesh"))
        geo.kids.append(_p70())
        geo.add("GeometryVersion", ("I", 124))
        geo.add("Vertices", ("d", pos.astype("<f8").ravel()))
        pvi = tri.astype("<i4").copy()
        pvi[:, 2] = -pvi[:, 2] - 1                       # the last index of a polygon, negated minus one
        geo.add("PolygonVertexIndex", ("i", pvi.ravel()))
        le = geo.add("LayerElementNormal", ("I", 0))
        le.add("Version", ("I", 101))
        le.add("Name", ("S", ""))
        le.add("MappingInformationType", ("S", "ByPolygonVertex"))
        le.add("ReferenceInformationType", ("S", "Direct"))
        le.add("Normals", ("d", np.repeat(_face_normals(pos, tri), 3, axis=0).astype("<f8").ravel()))
        lm = geo.add("LayerElementMaterial", ("I", 0))
        lm.add("Version", ("I", 101))
        lm.add("Name", ("S", ""))
        lm.add("MappingInformationType", ("S", "AllSame"))
        lm.add("ReferenceInformationType", ("S", "IndexToDirect"))
        lm.add("Materials", ("i", np.zeros(1, dtype="<i4")))
        lay = geo.add("Layer", ("I", 0))
        lay.add("Version", ("I", 100))
        for typ in ("LayerElementNormal", "LayerElementMaterial"):
            e = lay.add("LayerElement")
            e.add("Type", ("S", typ))
            e.add("TypedIndex", ("I", 0))
        mo = objects.add("Model", ("L", mid), ("S", f"{name}\x00\x01Model"), ("S", "Mesh"))
        mo.add("Version", ("I", 232))
        mo.kids.append(_p70())
        mo.add("Shading", ("C", True))
        mo.add("Culling", ("S", "CullingOff"))
        conns.add("C", ("S", "OO"), ("L", mid), ("L", group(tuple(getattr(p, "path", ()) or ()))))
        conns.add("C", ("S", "OO"), ("L", gid), ("L", mid))
        conns.add("C", ("S", "OO"), ("L", mat_ids[mi]), ("L", mid))
    n_geo, n_mod, n_mat = len(parts), len(parts) + len(groups), len(mats)
    header = _Node("FBXHeaderExtension")
    header.add("FBXHeaderVersion", ("I", 1003))
    header.add("FBXVersion", ("I", _FBX_VERSION))
    header.add("EncryptionType", ("I", 0))
    ts = header.add("CreationTimeStamp")
    for k, v in (("Version", 1000), ("Year", 1970), ("Month", 1), ("Day", 1), ("Hour", 10), ("Minute", 0),
                 ("Second", 0), ("Millisecond", 0)):
        ts.add(k, ("I", v))
    header.add("Creator", ("S", f"pcbfab2step {__version__}"))
    gs = _Node("GlobalSettings")
    gs.add("Version", ("I", 1000))
    gs.kids.append(_p70(
        _P("UpAxis", "int", "Integer", "", ("I", 2)), _P("UpAxisSign", "int", "Integer", "", ("I", 1)),
        _P("FrontAxis", "int", "Integer", "", ("I", 1)), _P("FrontAxisSign", "int", "Integer", "", ("I", -1)),
        _P("CoordAxis", "int", "Integer", "", ("I", 0)), _P("CoordAxisSign", "int", "Integer", "", ("I", 1)),
        _P("OriginalUpAxis", "int", "Integer", "", ("I", 2)),
        _P("OriginalUpAxisSign", "int", "Integer", "", ("I", 1)),
        _P("UnitScaleFactor", "double", "Number", "", ("D", 0.1)),
        _P("OriginalUnitScaleFactor", "double", "Number", "", ("D", 0.1))))
    docs = _Node("Documents")
    docs.add("Count", ("I", 1))
    d = docs.add("Document", ("L", 999999), ("S", ""), ("S", "Scene"))
    d.kids.append(_p70(_P("SourceObject", "object", "", ""),
                       _P("ActiveAnimStackName", "KString", "", "", ("S", ""))))
    d.add("RootNode", ("L", 0))
    defs = _Node("Definitions")
    defs.add("Version", ("I", 100))
    defs.add("Count", ("I", 3 + 1))
    defs.add("ObjectType", ("S", "GlobalSettings")).add("Count", ("I", 1))
    for typ, cnt in (("Model", n_mod), ("Geometry", n_geo), ("Material", n_mat)):
        defs.add("ObjectType", ("S", typ)).add("Count", ("I", cnt))
    top = [header, _Node("FileId", ("R", _FBX_FILE_ID)), _Node("CreationTime", ("S", _FBX_TIME)),
           _Node("Creator", ("S", f"pcbfab2step {__version__}")), gs, docs, _Node("References"), defs, objects,
           conns]
    out = io.BytesIO()
    out.write(b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", _FBX_VERSION))
    for n in top:
        out.write(_fbx_node(n, out.tell()))
    out.write(b"\0" * 13)                                # the end of the top level
    out.write(_FBX_FOOT_ID + b"\0" * 4)
    pad = (-out.tell()) % 16 or 16
    out.write(b"\0" * pad + struct.pack("<I", _FBX_VERSION) + b"\0" * 120 +
              b"\xf8\x5a\x8c\x6a\xde\xf5\xd9\x7e\xec\xe9\x0c\xe3\x75\x8f\x29\x0b")
    with open(path, "wb") as f:
        f.write(out.getvalue())
    return [path]


def bounds(parts) -> Tuple[np.ndarray, np.ndarray]:
    a = np.vstack([p.mesh.pos for p in parts])
    return a.min(axis=0), a.max(axis=0)

