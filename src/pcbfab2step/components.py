# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""Components from an ECAD STEP export (Altium: a simplified board body plus
the component models) added to the model.

The board body is left out (by name 'Board', else the body whose outline
matches the board); every other part keeps its name, colours and placement.
XY is the ECAD origin, which is the fab data's origin when the board body
lines up with the outline (checked; shifted to the outline centre if not).
In Z the ECAD board body rarely matches the stackup exactly, so each part is
moved onto the model's own surface on its side: parts above the ECAD board on
the top surface, parts below it on the bottom surface, parts through it by
the top surface.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

from .common import info, warn

GREY = (0.6, 0.6, 0.6)


def _occ():
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_Label
    from OCP.TopLoc import TopLoc_Location
    from OCP.Quantity import Quantity_Color
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.IFSelect import IFSelect_ReturnStatus
    try:
        from OCP.collections import Sequence_TDF_Label as Seq
    except ImportError:                                  # older OCP
        from OCP.TDF import TDF_LabelSequence as Seq
    return dict(
        STEPCAFControl_Reader=STEPCAFControl_Reader, TDocStd_Document=TDocStd_Document,
        TCollection_ExtendedString=TCollection_ExtendedString, XCAFDoc_DocumentTool=XCAFDoc_DocumentTool,
        XCAFDoc_ColorType=XCAFDoc_ColorType, TDataStd_Name=TDataStd_Name, TDF_Label=TDF_Label,
        TopLoc_Location=TopLoc_Location, Quantity_Color=Quantity_Color, Bnd_Box=Bnd_Box, BRepBndLib=BRepBndLib,
        gp_Trsf=gp_Trsf, gp_Vec=gp_Vec, IFSelect_ReturnStatus=IFSelect_ReturnStatus, Seq=Seq)


def _bbox(o, shape) -> Optional[Tuple[float, ...]]:
    b = o["Bnd_Box"]()
    o["BRepBndLib"].Add_s(shape, b)
    if b.IsVoid():
        return None
    p0, p1 = b.CornerMin(), b.CornerMax()
    return (p0.X(), p0.Y(), p0.Z(), p1.X(), p1.Y(), p1.Z())


def read(path: str, outline_bounds, z_top: float, z_bot: float) -> List[dict]:
    """-> bodies for write_step, grouped Components top/bottom > part > body.
    z_top/z_bot: the model's outer surfaces (solder mask included)."""
    o = _occ()
    doc = o["TDocStd_Document"](o["TCollection_ExtendedString"]("XmlOcaf"))
    r = o["STEPCAFControl_Reader"]()
    r.SetNameMode(True)
    r.SetColorMode(True)
    if r.ReadFile(path) != o["IFSelect_ReturnStatus"].IFSelect_RetDone or not r.Transfer(doc):
        warn(f"components: cannot read {path}")
        return []
    st = o["XCAFDoc_DocumentTool"].ShapeTool_s(doc.Main())
    ct = o["XCAFDoc_DocumentTool"].ColorTool_s(doc.Main())
    Seq, Label = o["Seq"], o["TDF_Label"]

    def name(lab):
        a = o["TDataStd_Name"]()
        return a.Get().ToExtString() if lab.FindAttribute(o["TDataStd_Name"].GetID_s(), a) else ""

    get = getattr(type(ct), "GetColor_s", None) or ct.GetColor      # label form is static in OCP 7.8+

    def color(lab, inherited):
        c = o["Quantity_Color"]()
        for typ in (o["XCAFDoc_ColorType"].XCAFDoc_ColorSurf, o["XCAFDoc_ColorType"].XCAFDoc_ColorGen):
            if get(lab, typ, c):
                return (c.Red(), c.Green(), c.Blue())
        subs = Seq()
        st.GetSubShapes_s(lab, subs)
        for i in range(1, subs.Length() + 1):
            for typ in (o["XCAFDoc_ColorType"].XCAFDoc_ColorSurf, o["XCAFDoc_ColorType"].XCAFDoc_ColorGen):
                if get(subs.Value(i), typ, c):
                    return (c.Red(), c.Green(), c.Blue())
        return inherited

    def leaves(lab, loc, inh, out):
        """(product name, located shape, colour) of every body below a label."""
        ref = Label()
        nm = name(lab)
        if st.IsReference_s(lab) and st.GetReferredShape_s(lab, ref):
            loc = loc.Multiplied(st.GetLocation_s(lab))
            inh = color(lab, inh)
            nm = name(ref) or nm               # the product's name (body), not the instance's (body:1)
            lab = ref
        inh = color(lab, inh)
        if st.IsAssembly_s(lab):
            kids = Seq()
            st.GetComponents_s(lab, kids)
            for i in range(1, kids.Length() + 1):
                leaves(kids.Value(i), loc, inh, out)
        else:
            out.append((re.sub(r":\d+$", "", nm) or "body", st.GetShape_s(lab).Moved(loc), inh))

    free = Seq()
    st.GetFreeShapes(free)
    tops = []                                   # (name, label) of the parts below the root
    for i in range(1, free.Length() + 1):
        root = free.Value(i)
        if st.IsAssembly_s(root):
            kids = Seq()
            st.GetComponents_s(root, kids)
            tops += [kids.Value(j) for j in range(1, kids.Length() + 1)]
        else:
            tops.append(root)
    TopLoc = o["TopLoc_Location"]
    parts = []
    for lab in tops:
        ref = Label()
        nm = name(lab)
        if st.IsReference_s(lab) and st.GetReferredShape_s(lab, ref):
            nm = name(ref) or nm
        shape = st.GetShape_s(lab)
        parts.append((nm, lab, _bbox(o, shape)))
    x0, y0, x1, y1 = outline_bounds
    board = next((p for p in parts if p[0].strip().lower() in ("board", "pcb")), None)
    if board is None:
        board = next((p for p in parts if p[2] and abs((p[2][3] - p[2][0]) - (x1 - x0)) < 1.0
                      and abs((p[2][4] - p[2][1]) - (y1 - y0)) < 1.0), None)
    notes = []
    if board is None or board[2] is None:
        warn(f"components: no board body found in {os.path.basename(path)}; parts placed as they are")
        bz0, bz1, dx, dy = z_bot, z_top, 0.0, 0.0
    else:
        bb = board[2]
        bz0, bz1 = bb[2], bb[5]
        dx = (x0 + x1) / 2 - (bb[0] + bb[3]) / 2
        dy = (y0 + y1) / 2 - (bb[1] + bb[4]) / 2
        if abs(dx) < 0.05 and abs(dy) < 0.05:
            dx = dy = 0.0
        else:
            notes.append(f"board body is off the outline by ({dx:.3f}, {dy:.3f}) mm: parts shifted")
        t_ecad, t_model = bz1 - bz0, z_top - z_bot
        notes.append(f"board body {board[0]!r} left out ({t_ecad:.4f} mm thick; model {t_model:.4f} mm"
                     + (")" if abs(t_ecad - t_model) < 0.005 else "; parts follow the model's surfaces)"))
    comps = []
    sides = {"top": 0, "bottom": 0}
    for nm, lab, bb in parts:
        if board is not None and lab.IsEqual(board[1]) or bb is None:
            continue
        eps = 0.05
        if bb[2] >= bz1 - eps:
            side, dz = "top", z_top - bz1
        elif bb[5] <= bz0 + eps:
            side, dz = "bottom", z_bot - bz0
        else:
            side, dz = "top", z_top - bz1                # through the board: by the top surface
        sides[side] += 1
        tr = o["gp_Trsf"]()
        tr.SetTranslation(o["gp_Vec"](dx, dy, dz))
        out = []
        leaves(lab, TopLoc(), GREY, out)
        group = "Components top" if side == "top" else "Components bottom"
        layer = f"COMPONENTS_{side.upper()}"
        if len(out) == 1:
            _, shape, col = out[0]
            comps.append({"name": nm, "color": col, "solids": [shape.Moved(TopLoc(tr))], "layer": layer,
                          "path": (group,)})
            continue
        used = {}
        for sub, shape, col in out:
            used[sub] = used.get(sub, 0) + 1
            leaf = sub if used[sub] == 1 else f"{sub} {used[sub]}"
            comps.append({"name": leaf, "color": col, "solids": [shape.Moved(TopLoc(tr))], "layer": layer,
                          "path": (group, nm)})
    notes.insert(0, f"{len(parts) - (board is not None)} part(s) from {os.path.basename(path)}: "
                    f"{sides['top']} on top, {sides['bottom']} on the bottom")
    for n in notes:
        info("  " + n)
    return comps
