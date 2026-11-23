"""Usage: python verify_step.py model.step [--overlap]
Re-import a STEP written by pcbfab2step and check it: names, colours, validity,
volumes, face types, pairwise overlaps between bodies."""
import sys, time, itertools
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
try:
    from OCP.TDF import TDF_LabelSequence
except ImportError:
    from OCP.collections import Sequence_TDF_Label as TDF_LabelSequence
from OCP.TDataStd import TDataStd_Name
from OCP.Quantity import Quantity_Color, Quantity_TypeOfColor
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.GProp import GProp_GProps
from OCP.BRepGProp import BRepGProp
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_SOLID, TopAbs_FACE
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.TopoDS import TopoDS
import os

path = sys.argv[1]
do_overlap = '--overlap' in sys.argv
app = XCAFApp_Application.GetApplication_s()
doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF")); app.InitDocument(doc)
rd = STEPCAFControl_Reader(); rd.SetColorMode(True); rd.SetNameMode(True)
fd = os.dup(1); nul = os.open(os.devnull, os.O_WRONLY); os.dup2(nul, 1)
st = rd.ReadFile(path); rd.Transfer(doc)
os.dup2(fd, 1)
stool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main()); ctool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
labels = TDF_LabelSequence(); stool.GetFreeShapes(labels)
bodies = []
def walk(lab):
    comps = TDF_LabelSequence()
    if stool.IsAssembly_s(lab):
        stool.GetComponents_s(lab, comps)
        for i in range(1, comps.Length() + 1):
            c = comps.Value(i)
            ref = c
            from OCP.TDF import TDF_Label
            r = TDF_Label()
            if stool.GetReferredShape_s(c, r):
                ref = r
            walk(ref)
    else:
        name_attr = TDataStd_Name()
        nm = ''
        if lab.FindAttribute(TDataStd_Name.GetID_s(), name_attr):
            nm = name_attr.Get().ToExtString()
        col = Quantity_Color()
        from OCP.XCAFDoc import XCAFDoc_ColorTool
        has = XCAFDoc_ColorTool.GetColor_s(lab, XCAFDoc_ColorType.XCAFDoc_ColorSurf, col)
        rgb = col.Values(Quantity_TypeOfColor.Quantity_TOC_sRGB) if has else None
        bodies.append((nm, rgb, stool.GetShape_s(lab)))
for i in range(1, labels.Length() + 1):
    walk(labels.Value(i))

def solids(s):
    out = []; ex = TopExp_Explorer(s, TopAbs_SOLID)
    while ex.More(): out.append(ex.Current()); ex.Next()
    return out
def faces_by_type(s):
    cnt = {}
    ex = TopExp_Explorer(s, TopAbs_FACE)
    while ex.More():
        t = BRepAdaptor_Surface(TopoDS.Face(ex.Current())).GetType()
        k = str(t).split('_')[-1]
        cnt[k] = cnt.get(k, 0) + 1; ex.Next()
    return cnt
def vol(s):
    p = GProp_GProps(); BRepGProp.VolumeProperties_s(s, p); return p.Mass()
def bb(s):
    b = Bnd_Box(); BRepBndLib.Add_s(s, b); p0, p1 = b.CornerMin(), b.CornerMax(); return (p0.X(), p0.Y(), p0.Z(), p1.X(), p1.Y(), p1.Z())

print(f"{len(bodies)} bodies in {os.path.getsize(path)/1e6:.1f} MB")
tot_bad = 0
info = []
for nm, rgb, shp in bodies:
    sols = solids(shp)
    bad = sum(0 if BRepCheck_Analyzer(x).IsValid() else 1 for x in sols)
    tot_bad += bad
    v = sum(vol(x) for x in sols)
    ft = faces_by_type(shp)
    if not rgb and sols:
        c2 = Quantity_Color()
        if ctool.GetColor(sols[0], XCAFDoc_ColorType.XCAFDoc_ColorSurf, c2):
            rgb = c2.Values(Quantity_TypeOfColor.Quantity_TOC_sRGB)
    col = '#%02x%02x%02x' % tuple(round(c * 255) for c in rgb) if rgb else 'none'
    z0, z1 = bb(shp)[2], bb(shp)[5]
    print(f"  {nm[:70]:70s} {col} solids={len(sols):4d} invalid={bad} vol={v:10.5f} mm3 z={z0:.4f}..{z1:.4f} faces={ft}")
    info.append((nm, sols))
print('invalid solids total:', tot_bad)
if do_overlap:
    t = time.time()
    worst = []
    allsol = [(nm, s, bb(s)) for nm, sols in info for s in sols]
    pairs = 0; overl = 0.0
    for (n1, s1, b1), (n2, s2, b2) in itertools.combinations(allsol, 2):
        if n1 == n2: continue
        if b1[3] < b2[0] or b2[3] < b1[0] or b1[4] < b2[1] or b2[4] < b1[1] or b1[5] <= b2[2] + 2e-6 or b2[5] <= b1[2] + 2e-6:
            continue
        pairs += 1
        c = BRepAlgoAPI_Common(s1, s2)
        v = vol(c.Shape()) if c.IsDone() else float('nan')
        if v > 1e-9 or v != v:
            worst.append((v, n1, n2))
            overl += v
    worst.sort(reverse=True)
    print(f"overlap check: {pairs} solid pairs with overlapping boxes, total overlap volume {overl:.3e} mm3 ({time.time()-t:.0f} s)")
    for w in worst[:10]:
        print('   ', w)
