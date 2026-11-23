"""Cross-section plot of a STEP file.
Usage: python section_plot.py model.step POS FROM TO out.png [x|y] [zmax]
  x: cut at x=POS, plot y from FROM to TO;  y: cut at y=POS, plot x.
Requires cadquery-ocp, numpy, matplotlib."""
import sys, os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MPoly

from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
from OCP.TDataStd import TDataStd_Name
from OCP.Quantity import Quantity_Color, Quantity_TypeOfColor
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_SOLID, TopAbs_FACE
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRep import BRep_Tool
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
try:
    from OCP.TDF import TDF_LabelSequence
except ImportError:
    from OCP.collections import Sequence_TDF_Label as TDF_LabelSequence
from OCP.TDF import TDF_Label

path, X0, Y0, Y1, out = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
AXIS = sys.argv[6] if len(sys.argv) > 6 else 'x'
ZMAX = float(sys.argv[7]) if len(sys.argv) > 7 else 1.80
app = XCAFApp_Application.GetApplication_s()
doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF")); app.InitDocument(doc)
rd = STEPCAFControl_Reader(); rd.SetColorMode(True); rd.SetNameMode(True)
fd = os.dup(1); nul = os.open(os.devnull, os.O_WRONLY); os.dup2(nul, 1)
rd.ReadFile(path); rd.Transfer(doc); os.dup2(fd, 1)
stool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main()); ctool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
labels = TDF_LabelSequence(); stool.GetFreeShapes(labels)
bodies = []
def walk(lab):
    if stool.IsAssembly_s(lab):
        comps = TDF_LabelSequence(); stool.GetComponents_s(lab, comps)
        for i in range(1, comps.Length() + 1):
            r = TDF_Label(); c = comps.Value(i)
            walk(r if stool.GetReferredShape_s(c, r) else c)
    else:
        na = TDataStd_Name(); nm = na.Get().ToExtString() if lab.FindAttribute(TDataStd_Name.GetID_s(), na) else ''
        shp = stool.GetShape_s(lab)
        ex = TopExp_Explorer(shp, TopAbs_SOLID); col = None
        if ex.More():
            c = Quantity_Color()
            if ctool.GetColor(ex.Current(), XCAFDoc_ColorType.XCAFDoc_ColorSurf, c):
                col = c.Values(Quantity_TypeOfColor.Quantity_TOC_sRGB)
        bodies.append((nm, col, shp))
for i in range(1, labels.Length() + 1): walk(labels.Value(i))
# thin slab around the section plane, then project its triangles onto (y, z)
fig, ax = plt.subplots(figsize=(13, 4.6))
w = 0.004
box = (BRepPrimAPI_MakeBox(gp_Pnt(X0 - w / 2, Y0, -1), gp_Pnt(X0 + w / 2, Y1, 3)) if AXIS == 'x' else BRepPrimAPI_MakeBox(gp_Pnt(Y0, X0 - w / 2, -1), gp_Pnt(Y1, X0 + w / 2, 3))).Shape()
legend = {}
for nm, col, shp in bodies:
    cm = BRepAlgoAPI_Common(shp, box).Shape()
    BRepMesh_IncrementalMesh(cm, 0.002, False, 0.2, True)
    ex = TopExp_Explorer(cm, TopAbs_FACE)
    while ex.More():
        f = TopoDS.Face(ex.Current()); loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(f, loc)
        if tri is not None:
            pts = np.array([[(tri.Node(i).Y() if AXIS == 'x' else tri.Node(i).X()), tri.Node(i).Z()] for i in range(1, tri.NbNodes() + 1)])
            for k in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(k).Get()
                ax.add_patch(MPoly(pts[[a - 1, b - 1, c - 1]], closed=True, facecolor=col or (0.5, 0.5, 0.5), edgecolor='none'))
        ex.Next()
    if col:
        legend[nm.split(' (')[0]] = col
ax.set_xlim(Y0, Y1); ax.set_ylim(-0.08, ZMAX)
ax.set_xlabel((f'y (mm) at x = {X0} mm') if AXIS == 'x' else (f'x (mm) at y = {X0} mm')); ax.set_ylabel('z (mm)')
handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in legend.values()]
ax.legend(handles, legend.keys(), fontsize=7, loc='upper left', bbox_to_anchor=(1.01, 1.0))
ax.set_title(os.path.basename(path) + ': section (z exaggerated)')
plt.tight_layout(); plt.savefig(out, dpi=150)
print('saved', out)
