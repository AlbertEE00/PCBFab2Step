"""Synthetic fab data for the tests (no proprietary designs in the repository)."""

import os
import textwrap

import pytest

# A 40 x 30 mm, three-copper-layer board in IPC-2581 rev B as Altium writes it:
# file coordinates offset by the Datum (10, 10), per-layer hole diameters in the
# padstacks for a top countersink and a bottom counterbore, a back drill layer
# with its Backdrill spec, and "Filling" / "Capping" document layers: the via
# markers are pads of their own padstacks (as Altium writes IPC-4761 via
# features), the drawn circle on "Filling" at (35, 30) is artwork, and the
# "Plugging Bottom" layer has drawn markers only (an arc around (35, 30)).
IPC_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <IPC-2581 revision="B" xmlns="http://webstds.ipc.org/2581">
      <Content roleRef="Owner">
        <FunctionMode mode="USERDEF" level="1"/>
        <StepRef name="test board"/>
        <DictionaryStandard units="MILLIMETER">
          <EntryStandard id="CIRCLE_1"><Circle diameter="1.0"/></EntryStandard>
          <EntryStandard id="RECT_1"><RectCenter width="1.0" height="2.0"/></EntryStandard>
          <EntryStandard id="CIRCLE_2"><Circle diameter="0.7"/></EntryStandard>
        </DictionaryStandard>
      </Content>
      <Ecad name="test">
        <CadHeader units="MILLIMETER">
          <Spec name="TOP"><General type="MATERIAL"><Property text="Copper"/></General></Spec>
          <Spec name="CORE"><General type="MATERIAL"><Property text="FR4"/></General>
            <Dielectric type="DIELECTRIC_CONSTANT"><Property value="4.3"/></Dielectric></Spec>
          <Spec name="PP 1"><General type="MATERIAL"><Property text="FR4 prepreg"/></General></Spec>
          <Spec name="Back Drill (TOP - MID, 0.835)">
            <Backdrill type="START_LAYER"><Property layerOrGroupRef="TOP"/></Backdrill>
            <Backdrill type="MUST_NOT_CUT_LAYER"><Property layerOrGroupRef="MID"/></Backdrill>
            <Backdrill type="MAX_STUB_LENGTH"><Property value="0.835"/></Backdrill>
          </Spec>
        </CadHeader>
        <CadData>
          <Layer name="TOP" layerFunction="SIGNAL" side="TOP" polarity="POSITIVE"/>
          <Layer name="CORE" layerFunction="DIELCORE" side="NONE" polarity="POSITIVE"/>
          <Layer name="MID" layerFunction="SIGNAL" side="INTERNAL" polarity="POSITIVE"/>
          <Layer name="PP 1" layerFunction="DIELCORE" side="NONE" polarity="POSITIVE"/>
          <Layer name="BOT" layerFunction="SIGNAL" side="BOTTOM" polarity="POSITIVE"/>
          <Layer name="Bottom Solder" layerFunction="SOLDERMASK" side="BOTTOM" polarity="POSITIVE"/>
          <Layer name="Top Solder" layerFunction="SOLDERMASK" side="TOP" polarity="POSITIVE"/>
          <Layer name="Top Overlay" layerFunction="LEGEND" side="TOP" polarity="POSITIVE"/>
          <Layer name="Filling" layerFunction="DOCUMENT" side="INTERNAL" polarity="POSITIVE"/>
          <Layer name="Capping" layerFunction="DOCUMENT" side="INTERNAL" polarity="POSITIVE"/>
          <Layer name="Plugging Bottom" layerFunction="DOCUMENT" side="INTERNAL" polarity="POSITIVE"/>
          <Layer name="Drill Guide (TOP - BOT)" layerFunction="DRILL" side="INTERNAL" polarity="POSITIVE"/>
          <Layer name="Drill Guide ([BD] TOP - MID)" layerFunction="DRILL" side="INTERNAL" polarity="POSITIVE">
            <SpanType fromLayer="TOP" toLayer="MID"/>
          </Layer>
          <Stackup name="Stackup" overallThickness="1.35" tolPlus="0" tolMinus="0" whereMeasured="OTHER">
            <StackupGroup name="all" thickness="1.35" tolPlus="0" tolMinus="0">
              <StackupLayer layerOrGroupRef="TOP" thickness="0.035" sequence="1"><SpecRef id="TOP"/></StackupLayer>
              <StackupLayer layerOrGroupRef="CORE" thickness="0.8" sequence="2"><SpecRef id="CORE"/></StackupLayer>
              <StackupLayer layerOrGroupRef="MID" thickness="0.035" sequence="3"><SpecRef id="MID"/></StackupLayer>
              <StackupLayer layerOrGroupRef="PP 1" thickness="0.4" sequence="4"><SpecRef id="PP 1"/></StackupLayer>
              <StackupLayer layerOrGroupRef="BOT" thickness="0.035" sequence="5"><SpecRef id="BOT"/></StackupLayer>
              <StackupLayer layerOrGroupRef="Bottom Solder" thickness="0.025" sequence="6"/>
              <StackupLayer layerOrGroupRef="Top Solder" thickness="0.02" sequence="7"/>
              <StackupLayer layerOrGroupRef="Top Overlay" thickness="0" sequence="8"/>
            </StackupGroup>
          </Stackup>
          <Step name="test board">
            <Datum x="10" y="10"/>
            <Profile>
              <Polygon>
                <PolyBegin x="10" y="10"/>
                <PolyStepSegment x="50" y="10"/>
                <PolyStepSegment x="50" y="40"/>
                <PolyStepSegment x="10" y="40"/>
                <PolyStepSegment x="10" y="10"/>
              </Polygon>
            </Profile>
            <PadStack net="No Net">
              <LayerHole name="MH1 CounterHole TOP" diameter="4.0" platingStatus="NONPLATED" x="30" y="25">
                <Span fromLayer="TOP" toLayer="TOP"/></LayerHole>
              <LayerHole name="MH1 CounterHole MID" diameter="2.33" platingStatus="NONPLATED" x="30" y="25">
                <Span fromLayer="MID" toLayer="MID"/></LayerHole>
              <LayerHole name="MH1 CounterHole BOT" diameter="2.0" platingStatus="NONPLATED" x="30" y="25">
                <Span fromLayer="BOT" toLayer="BOT"/></LayerHole>
            </PadStack>
            <PadStack net="No Net">
              <LayerHole name="MH2 CounterHole TOP" diameter="2.0" platingStatus="NONPLATED" x="40" y="25">
                <Span fromLayer="TOP" toLayer="TOP"/></LayerHole>
              <LayerHole name="MH2 CounterHole MID" diameter="3.0" platingStatus="NONPLATED" x="40" y="25">
                <Span fromLayer="MID" toLayer="MID"/></LayerHole>
              <LayerHole name="MH2 CounterHole BOT" diameter="3.0" platingStatus="NONPLATED" x="40" y="25">
                <Span fromLayer="BOT" toLayer="BOT"/></LayerHole>
            </PadStack>
            <PadStack net="GND">
              <LayerHole name="Via_1" diameter="0.3" platingStatus="VIA" x="25" y="20">
                <Span fromLayer="TOP" toLayer="BOT"/></LayerHole>
              <LayerPad layerRef="TOP"><Location x="25" y="20"/><StandardPrimitiveRef id="CIRCLE_1"/></LayerPad>
              <LayerPad layerRef="BOT"><Location x="25" y="20"/><StandardPrimitiveRef id="CIRCLE_1"/></LayerPad>
            </PadStack>
            <PadStack net="No Net">
              <LayerPad layerRef="Filling"><Xform/><Location x="25" y="20"/>
                <StandardPrimitiveRef id="CIRCLE_1"/></LayerPad>
            </PadStack>
            <PadStack net="No Net">
              <LayerPad layerRef="Capping"><Xform/><Location x="25" y="20"/>
                <StandardPrimitiveRef id="CIRCLE_2"/></LayerPad>
            </PadStack>
            <PadStack net="GND">
              <LayerHole name="Via_2" diameter="0.3" platingStatus="VIA" x="35" y="30">
                <Span fromLayer="TOP" toLayer="BOT"/></LayerHole>
              <LayerPad layerRef="Bottom Solder"><Location x="35" y="30"/><StandardPrimitiveRef id="CIRCLE_2"/></LayerPad>
            </PadStack>
            <PadStack net="GND">
              <LayerPad layerRef="TOP"><Xform rotation="90"/><Location x="20" y="15"/>
                <StandardPrimitiveRef id="RECT_1"/></LayerPad>
              <LayerPad layerRef="Top Solder"><Xform rotation="90"/><Location x="20" y="15"/>
                <StandardPrimitiveRef id="RECT_1"/></LayerPad>
            </PadStack>
            <LayerFeature layerRef="TOP">
              <Set net="GND"><Features><Contour><Polygon>
                <PolyBegin x="12" y="30"/><PolyStepSegment x="48" y="30"/><PolyStepSegment x="48" y="38"/>
                <PolyStepSegment x="12" y="38"/><PolyStepSegment x="12" y="30"/></Polygon></Contour></Features></Set>
              <Set net="SIG"><Features><Line startX="12" startY="15" endX="18" endY="15">
                <LineDesc lineEnd="ROUND" lineWidth="0.2" lineProperty="SOLID"/></Line></Features></Set>
            </LayerFeature>
            <LayerFeature layerRef="MID">
              <Set net="GND"><Features><Contour><Polygon>
                <PolyBegin x="12" y="12"/><PolyStepSegment x="48" y="12"/><PolyStepSegment x="48" y="38"/>
                <PolyStepSegment x="12" y="38"/><PolyStepSegment x="12" y="12"/></Polygon>
                <Cutout><PolyBegin x="20" y="20"/>
                  <PolyStepCurve x="20" y="20" centerX="25" centerY="20" clockwise="false"/></Cutout>
                </Contour></Features></Set>
            </LayerFeature>
            <LayerFeature layerRef="BOT">
              <Set net="GND"><Features><Contour><Polygon>
                <PolyBegin x="12" y="12"/><PolyStepSegment x="48" y="12"/><PolyStepSegment x="48" y="20"/>
                <PolyStepSegment x="12" y="20"/><PolyStepSegment x="12" y="12"/></Polygon></Contour></Features></Set>
            </LayerFeature>
            <LayerFeature layerRef="Bottom Solder">
              <Set><Features><Location x="25" y="20"/><StandardPrimitiveRef id="CIRCLE_1"/></Features></Set>
            </LayerFeature>
            <LayerFeature layerRef="Filling">
              <Set><Features><Location x="35" y="30"/><StandardPrimitiveRef id="CIRCLE_1"/></Features></Set>
            </LayerFeature>
            <LayerFeature layerRef="Plugging Bottom">
              <Set><Features><UserSpecial>
                <Arc startX="35.3" startY="30" endX="35.3" endY="30" centerX="35" centerY="30" clockwise="false">
                  <LineDesc lineEnd="ROUND" lineWidth="0.05" lineProperty="SOLID"/></Arc>
                <Line startX="10" startY="10" endX="50" endY="40">
                  <LineDesc lineEnd="ROUND" lineWidth="0.1" lineProperty="SOLID"/></Line>
              </UserSpecial></Features></Set>
            </LayerFeature>
            <LayerFeature layerRef="Drill Guide (TOP - BOT)">
              <Set><Hole name="MH1" diameter="2.0" platingStatus="NONPLATED" x="30" y="25"/></Set>
              <Set><Hole name="MH2" diameter="2.0" platingStatus="NONPLATED" x="40" y="25"/></Set>
              <Set><Hole name="Via 1" diameter="0.3" platingStatus="VIA" plusTol="0" minusTol="0" x="25" y="20">
                <SpecRef id="Back Drill (TOP - MID, 0.835)"/></Hole></Set>
              <Set><Hole name="Via 2" diameter="0.3" platingStatus="VIA" plusTol="0" minusTol="0" x="35" y="30"/></Set>
            </LayerFeature>
            <LayerFeature layerRef="Drill Guide ([BD] TOP - MID)">
              <Set><Hole name="Via 1" diameter="0.5" platingStatus="VIA" plusTol="0" minusTol="0" x="25" y="20"/></Set>
            </LayerFeature>
          </Step>
        </CadData>
      </Ecad>
    </IPC-2581>
    """)


def write_odb(root: str) -> str:
    """A 30 x 20 mm two-layer ODB++ job folder (MM units) with a plated via,
    an NPTH and a top counterhole layer (Altium style ADD_TYPE=COUNTERHOLE)."""
    job = os.path.join(root, "testjob")

    def w(rel, text):
        p = os.path.join(job, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(textwrap.dedent(text))

    w("misc/info", "UNITS=MM\nODB_VERSION_MAJOR=8\nODB_VERSION_MINOR=1\nODB_SOURCE=pytest\n")
    w("misc/attrlist", "UNITS=MM\n.board_thickness=1.07\n")
    layers = [
        ("TOP_SOLDER", "SOLDER_MASK", "", "", ""),
        ("TOP", "SIGNAL", "", "", ""),
        ("CORE", "DIELECTRIC", "", "", ""),
        ("BOT", "SIGNAL", "", "", ""),
        ("DRILL", "DRILL", "TOP", "BOT", ""),
        ("COUNTERHOLES_TOP", "DRILL", "TOP", "BOT", "COUNTERHOLE"),
    ]
    m = "STEP {\n   COL=1\n   NAME=PCB\n}\n"
    for i, (n, t, s, e, add) in enumerate(layers, 1):
        m += (f"LAYER {{\n   ROW={i}\n   CONTEXT=BOARD\n   TYPE={t}\n   NAME={n}\n   POLARITY=POSITIVE\n"
              f"   START_NAME={s}\n   END_NAME={e}\n   OLD_NAME=\n   ADD_TYPE={add}\n")
        if t == "DIELECTRIC":
            m += "   DIELECTRIC_TYPE=CORE\n   DIELECTRIC_NAME=FR4\n"
        m += "}\n"
    w("matrix/matrix", m)
    S = "steps/pcb"
    w(f"{S}/stephdr", "UNITS=MM\nX_DATUM=0\nY_DATUM=0\n")
    w(f"{S}/attrlist", "UNITS=MM\n")
    w(f"{S}/profile", "UNITS=MM\nS P 0\nOB 0 0 I\nOS 30 0\nOS 30 20\nOS 0 20\nOS 0 0\nOE\nSE\n")
    w(f"{S}/layers/top/attrlist", "UNITS=MM\n.copper_weight=35\n")
    w(f"{S}/layers/bot/attrlist", "UNITS=MM\n.copper_weight=35\n")
    w(f"{S}/layers/core/attrlist", "UNITS=MM\n.layer_dielectric=1.0\n")
    surf = "S P 0\nOB 1 1 I\nOS 29 1\nOS 29 19\nOS 1 19\nOS 1 1\nOE\nSE\n"
    w(f"{S}/layers/top/features", "UNITS=MM\n$0 r800\n" + surf + "P 20 10 0 P 0 0\n")
    w(f"{S}/layers/bot/features", "UNITS=MM\n" + surf)
    w(f"{S}/layers/top_solder/features", "UNITS=MM\n$0 r1000\nP 20 10 0 P 0 0\n")
    w(f"{S}/layers/drill/features", "UNITS=MM\n$0 r300\n$1 r1000\nP 20 10 0 P 0 0\nP 10 10 1 P 0 0\n")
    w(f"{S}/layers/drill/tools", "UNITS=MM\nTOOLS {\nNUM=1\nTYPE=VIA\nFINISH_SIZE=300\nDRILL_SIZE=350\n}\n"
      "TOOLS {\nNUM=2\nTYPE=NON_PLATED\nFINISH_SIZE=1000\nDRILL_SIZE=1000\n}\n")
    w(f"{S}/layers/counterholes_top/features", "UNITS=MM\n$0 r2000\nP 10 10 0 P 0 0\n")
    for n, *_ in layers:
        p = os.path.join(job, S, "layers", n.lower(), "attrlist")
        if not os.path.exists(p):
            w(f"{S}/layers/{n.lower()}/attrlist", "UNITS=MM\n")
    return job


@pytest.fixture
def ipc_file(tmp_path):
    p = tmp_path / "board.cvg"
    p.write_text(IPC_XML, encoding="utf-8")
    return str(p)


@pytest.fixture
def odb_job(tmp_path):
    return write_odb(str(tmp_path))


@pytest.fixture
def opts():
    from types import SimpleNamespace

    from pcbfab2step.config import resolve
    o = resolve(SimpleNamespace(), None)
    o.via_rules = []
    return o
