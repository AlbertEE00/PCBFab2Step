# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""The GUI's command line (options.py), its back end (gui.py) and --view."""

import io
import json
import os

import pytest

from pcbfab2step import options as O
from pcbfab2step.cli import main
from pcbfab2step.config import SCHEMA


def test_every_setting_has_a_control():
    m = O.option_model()
    ids = {o["id"] for g in m["groups"] for o in g["options"]} | {g["switch"]["id"] for g in m["groups"]
                                                                   if g["switch"]} | {o["id"] for o in m["export"]}
    missing = {attr for _s, _k, attr, *_r in SCHEMA} - ids - {"output"}       # output: the STEP model field
    assert not missing


VALUES = {"mask": False, "paste": True, "mask_color": "black", "via_fill": "epoxy", "via_cap": "true",
          "sanitize": "outer", "via_protection": "7:max=0.3\n1b:layer=drill*", "counterhole_top": "type=sink,angle=82",
          "aperture_offset": "-0.05mm", "invert": True, "outline": "without", "layer_thickness": "CORE=0.5",
          "counterbore_rules": "3.2:6.0:1.5:bottom", "finish_type": "ENIG", "vias_drilled": "undrilled",
          "arc_tolerance": "0.002", "no_config": True, "clip_silk": False, "use_sources": "odb+drill+step",
          "stencil_thickness": "4mil", "mask_thickness": "0.02", "board_color": "#123456"}


@pytest.mark.parametrize("export", [None, "solder-bot:svg"])
def test_argv_round_trip(export):
    argv = O.build_argv(["C:/x y/FAB"], "C:/out/b.step", VALUES, export)
    back = O.parse_argv(argv)
    want = {k: v for k, v in VALUES.items() if (export or k not in O.EXPORT_ONLY) and not (export and k in O.SHOW)}
    assert back["values"] == want
    assert back["export"] == export and back["inputs"] == ["C:/x y/FAB"]
    assert back["output"] == (None if export else "C:/out/b.step")
    if export:
        assert "--show" not in argv and "--hide" not in argv          # not with --export
    else:
        assert "--invert" not in argv and not any(a.startswith("--aperture") for a in argv)


def test_sanitize_off_over_config():
    assert O.build_argv(["FAB"], None, {"sanitize": "none"}) == ["FAB", "--set", "model.sanitize=none"]
    assert O.parse_argv(["FAB", "--set", "model.sanitize=none"])["values"] == {"sanitize": "none"}


def test_command_line_quoting():
    argv = ["C:/My Boards/FAB", "--vias", "1b:layer=drill*", "--set", 'x=["a"]', "--color", "mask=black"]
    win = O.command_line(argv, windows=True)
    assert win == 'pcbfab2step "C:/My Boards/FAB" --vias "1b:layer=drill*" --set \'x=["a"]\' --color mask=black'
    assert O.command_line(argv, windows=False).startswith("pcbfab2step 'C:/My Boards/FAB'")


# --------------------------------------------------------------------------
# the back end, called as WSGI

def _call(app, method, path, body=None, key=True):
    raw = json.dumps(body).encode() if body is not None else b""
    q = path.split("?", 1)[1] if "?" in path else ""
    if key:
        q = (q + "&" if q else "") + f"t={app.key}"
    env = {"REQUEST_METHOD": method, "PATH_INFO": path.split("?")[0], "QUERY_STRING": q,
           "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
    got = {}

    def start(status, headers):
        got["status"], got["headers"] = status, dict(headers)
    body = b"".join(app(env, start))
    ctype = got["headers"].get("Content-Type", "")
    return got["status"], (json.loads(body) if "json" in ctype else body)


@pytest.fixture
def gui():
    from pcbfab2step.gui import Gui
    return Gui(dict(inputs=[], output=None, values={}, export=None))


def test_key_and_static(gui):
    assert _call(gui, "GET", "/api/init", key=False)[0].startswith("403")
    status, body = _call(gui, "GET", "/web/viewer.js", key=False)
    assert status.startswith("200") and b"class Viewer" in body
    assert _call(gui, "GET", "/web/../gui.py", key=False)[0].startswith("404")
    status, init = _call(gui, "GET", "/api/init")
    assert status.startswith("200") and init["model"]["groups"] and init["window"] is False


def test_state_scene_and_run(gui, ipc_file, tmp_path):
    req = dict(inputs=[ipc_file], output=None, values={"paste": True, "mask_color": "black"})
    status, st = _call(gui, "POST", "/api/state", req)
    assert st["error"] is None and st["sources"]["primary"] == "ipc"
    assert st["argv"] == [ipc_file, "--show", "paste", "--color", "mask=black"]
    assert st["colors"]["mask"] == [0.0, 0.0, 0.0] or max(st["colors"]["mask"]) < 0.2
    assert st["hidden"] == [] and st["output"].endswith(".step")

    status, sc = _call(gui, "POST", "/api/scene", req)
    assert status.startswith("200") and sc["manifest"]["parts"] and not sc["cached"]
    kinds = [r["kind"] for r in sc["stack"]]
    assert kinds[0] == "outline" and "dielectric" in kinds and kinds.count("copper") == 3
    blob = _call(gui, "GET", f"/api/blob?id={sc['id']}")[1]
    last = max(p["edges"][0] + 4 * p["edges"][1] for p in sc["manifest"]["parts"])
    assert len(blob) == last
    again = _call(gui, "POST", "/api/scene", dict(req, values={"paste": False, "mask_color": "red"}))[1]
    assert again["cached"] and again["id"] == sc["id"]          # colours and paste: the same scene
    fins = [r for r in sc["stack"] if r["kind"] == "finish"]
    assert [r["side"] for r in fins] == ["top", "bottom"] and all(r["material"] == "ENIG" for r in fins)
    hid = _call(gui, "POST", "/api/scene", dict(req, values=dict(req["values"], hide_bodies="silk-bot")))[1]
    assert hid["cached"]                                          # leaving out a body is a draw
    hid = _call(gui, "POST", "/api/scene", dict(req, values=dict(req["values"], hide_bodies="silk-bot,finish-top")))[1]
    assert not hid["cached"]                                      # the finish is in the copper: rebuilt
    top = [p for p in hid["manifest"]["parts"] if p.get("kind") == "finish"]
    assert top and all(p.get("side") != "top" for p in top)

    layer = _call(gui, "POST", "/api/scene", dict(req, spec="solder-bot:svg"))[1]
    assert layer["manifest"]["flat"] and layer["manifest"]["mode"] == "layer"

    out = str(tmp_path / "out" / "board.step")
    run = _call(gui, "POST", "/api/run", dict(req, output=out))[1]
    import time
    for _ in range(600):
        r = _call(gui, "GET", f"/api/run?id={run['id']}")[1]
        if r["done"]:
            break
        time.sleep(0.1)
    assert r["ok"] and r["outputs"] == [out] and os.path.getsize(out) > 1000


def test_state_errors(gui, tmp_path):
    st = _call(gui, "POST", "/api/state", dict(inputs=[str(tmp_path)], values={}))[1]
    assert "no ODB++ or IPC-2581" in st["error"]
    st = _call(gui, "POST", "/api/state", dict(inputs=[str(tmp_path)], values={"via_fill": "glue"}))[1]
    assert st["error"]
    assert _call(gui, "GET", "/api/blob?id=nope")[0].startswith("404")


def test_view_writes_nothing(tmp_path, ipc_file, monkeypatch):
    monkeypatch.setenv("PCBFAB2STEP_NO_WINDOW", "1")
    src = tmp_path / "board.cvg"
    src.write_bytes(open(ipc_file, "rb").read())
    assert main(["--view", str(src), "-q"]) == 0                    # --view before the input
    assert main([str(src), "--export", "solder-bot:svg", "--view", "-q"]) == 0
    assert not (tmp_path / "PCBFab2Step").exists()


def test_standalone_page(ipc_file):
    from pcbfab2step import scene
    from pcbfab2step.cli import build_parser
    from pcbfab2step.pipeline import Job
    job = Job(build_parser().parse_args([ipc_file, "-q"]), "model")
    man, blob = scene.model_scene(job).pack()
    page = scene.to_html(man, blob)
    assert page.startswith("<!DOCTYPE html>") and "type='importmap'" in page
    assert '"three":"data:text/javascript;base64,' in page and "window.PCB_SCENE" in page
    assert "</script>" not in json.dumps(man)                      # nothing closes the script early
