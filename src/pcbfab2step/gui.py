# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""`pcbfab2step gui [INPUT ...] [options]`: the command line in a window.

The GUI is a face on the command line, not a second program: its state is a
command line (options.build_argv), shown at the bottom of the window and run
as it is shown. The 3D view is the --view scene of that command line, rebuilt
when it changes (the fab data is read once and kept, see pipeline.Session).
Colours and the bodies the settings leave out change in the view at once.

Back end: a WSGI app on 127.0.0.1 with a random key (view.WebApp), in a
pywebview window (Edge WebView2 on Windows): native file dialogs, files
dropped on the window come with their full paths.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict
from typing import Dict, List, Optional

from . import __version__
from . import options as O
from .common import Log
from .pipeline import Job, Session
from .view import WebApp, have_webview, page, serve, window

KIND_COLOR = {"board_color": ("core", "dielectric", "board"), "copper_color": ("copper",), "mask_color": ("mask",),
              "silk_color": ("silk",), "paste_color": ("paste",), "plating_color": ("plating",),
              "cap_color": ("cap",), "finish_color": ("finish",), "fill_color": ("fill", "plug")}
# what only the view changes: no new scene for these
VIEW_ONLY = {"paste", "components"} | set(KIND_COLOR)


class Gui(WebApp):
    def __init__(self, start: dict):
        super().__init__()
        self.start = start                      # {inputs, output, values, export}
        self.session = Session(keep=2)
        self.work = threading.Lock()            # one model job at a time (the builder has globals)
        self.scenes: "OrderedDict[str, tuple]" = OrderedDict()   # id -> (manifest, blob, extra)
        self.scene_keys: Dict[tuple, str] = {}
        self.runs: Dict[str, dict] = {}
        self.window = None
        self._n = 0

        @self.route("GET", "/")
        def index(req, q):
            return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page(
                "PCBFab2Step", "<body class='app'></body>", "app.js", ("viewer.css", "app.css")).encode("utf-8")

        @self.route("GET", "/api/init")
        def init(req, q):
            return self.json(dict(model=O.option_model(), start=self.start, version=__version__,
                                  window=self.window is not None, windows=os.name == "nt"))

        @self.route("POST", "/api/state")
        def state(req, q):
            return self.json(self.state(req))

        @self.route("POST", "/api/scene")
        def scene(req, q):
            return self.json(self.scene(req))

        @self.route("GET", "/api/blob")
        def blob(req, q):
            got = self.scenes.get(q.get("id", ""))
            if got is None:
                return self.error("that scene is gone; update the view", "404 Not Found")
            return "200 OK", [("Content-Type", "application/octet-stream")], got[1]

        @self.route("POST", "/api/run")
        def run(req, q):
            return self.json(self.run(req))

        @self.route("GET", "/api/run")
        def run_status(req, q):
            r = self.runs.get(q.get("id", ""))
            if r is None:
                return self.error("no such run", "404 Not Found")
            n = int(q.get("since", 0))
            return self.json(dict(lines=r["lines"][n:], n=len(r["lines"]), done=r["done"], ok=r["ok"],
                                  outputs=r["outputs"], seconds=round(time.time() - r["t0"], 1)))

        @self.route("POST", "/api/pick")
        def pick(req, q):
            return self.json(dict(paths=self.pick(req.get("mode", "folder"), req.get("path") or "",
                                                  req.get("types") or [])))

        @self.route("POST", "/api/open")
        def open_(req, q):
            return self.json(dict(ok=open_path(req.get("path", ""))))

    # ------------------------------------------------------------------
    # state

    def _argv(self, req, export=None) -> List[str]:
        return O.build_argv(req.get("inputs") or [], req.get("output") or None, req.get("values") or {}, export)

    def _job(self, argv: List[str], mode: str) -> Job:
        from .cli import _check_scope, build_parser, join_counterhole_args
        args = build_parser().parse_args(join_counterhole_args(argv))
        _check_scope(args)
        return Job(args, mode, self.session)

    def state(self, req) -> dict:
        """The command line of the GUI state and what it finds: inputs, output,
        the colours the view can change at once. Errors come back as text."""
        export = req.get("export") or None
        argv = self._argv(req, export)
        out = dict(argv=argv, command=O.command_line(argv), error=None, sources=None, output=None, colors={},
                   hidden=[], geometry=None, effective={}, origins={}, switches={}, warnings=[])
        if not req.get("inputs"):
            out["error"] = "Drop the fab outputs on the window, or choose a folder or file."
            return out
        try:
            job = self._job(argv, "export" if export else "model")
        except SystemExit as exc:
            out["error"] = str(exc.code if exc.code is not None else exc)
            out["sources"] = self._found(req.get("inputs") or [])
            return out
        out["sources"] = self._sources(job)
        out["output"] = job.model_path() if not export else None
        o = job.opts
        for attr, kinds in KIND_COLOR.items():
            c = getattr(o, attr, None)
            if isinstance(c, tuple):
                for k in kinds:
                    out["colors"][k] = [round(v, 4) for v in c]
        out["hidden"] = ([] if o.paste else ["paste"]) + ([] if o.components else ["component"])
        out["effective"] = {attr: O.display_value(kind, getattr(o, attr, None))
                            for attr, (sec, key, kind, *_r) in O.SETTING.items()}
        out["origins"] = dict(getattr(o, "sources", {}) or {})
        out["switches"] = {a: bool(getattr(o, a, False)) for a in O.SHOW}
        out["switches"]["sanitize"] = o.sanitize != "none"
        out["geometry"] = self._geometry_key(req, export)
        out["warnings"] = list(job.srcs.notes)
        return out

    @staticmethod
    def _geometry_key(req, export=None) -> str:
        """What the scene depends on: not the colours or what the view hides,
        not the export settings for the model."""
        skip = VIEW_ONLY if export else VIEW_ONLY | set(O.EXPORT_ONLY) | {"stencil_thickness"}
        values = {k: v for k, v in (req.get("values") or {}).items() if k not in skip}
        return json.dumps([req.get("inputs") or [], values, export], sort_keys=True)

    @staticmethod
    def _found(inputs) -> Optional[dict]:
        from .sources import discover
        try:
            s = discover(list(inputs))
        except SystemExit:
            return None
        return dict(primary=None, path=None, found={k: v for k, v in s.found.items()}, out_dir=s.out_dir,
                    config=s.config, notes=s.notes)

    @staticmethod
    def _sources(job) -> dict:
        s = job.srcs
        return dict(primary=s.primary, path=s.primary_path, ipc_hints=s.ipc_hints, odb_types=s.odb_types,
                    drill=list(s.drill), step=s.step, gerber=len(s.found.get("gerber", [])), config=s.config,
                    found={k: v for k, v in s.found.items()}, out_dir=s.out_dir, notes=list(s.notes), use=s.use)

    # ------------------------------------------------------------------
    # scenes

    def _keep(self, manifest, blob, extra) -> str:
        self._n += 1
        sid = f"s{self._n}"
        self.scenes[sid] = (manifest, blob, extra)
        while len(self.scenes) > 6:
            old, _ = self.scenes.popitem(last=False)
            self.scene_keys = {k: v for k, v in self.scene_keys.items() if v != old}
        return sid

    def scene(self, req) -> dict:
        """The model's scene (fast, or exact), or with spec one layer's export."""
        spec = req.get("spec")
        exact = bool(req.get("exact"))
        argv = self._argv(req, spec)
        with self.work:
            return self._scene(req, spec, exact, argv)

    def _scene(self, req, spec, exact, argv) -> dict:
        from . import scene as S
        messages: List[str] = []
        Log.reset(1)
        Log.sink = messages.append
        try:
            t0 = time.time()
            job = self._job(argv, "export" if spec else "model")
            key = (job.read_key(), self._geometry_key(req, spec), exact)
            sid = self.scene_keys.get(key)
            if sid in self.scenes:
                m, _blob, extra = self.scenes[sid]
                return dict(id=sid, manifest=m, cached=True, **extra)
            if spec:
                sc = S.layer_scene(job, spec)
                model = job.prepare()
            else:
                sc = S.model_scene(job, exact=exact)
                model = sc.model
            manifest, blob = sc.pack()
            extra = dict(stack=stack_rows(model), messages=messages + sc.info, seconds=round(time.time() - t0, 2),
                         board=job.read().name)
            sid = self._keep(manifest, blob, extra)
            self.scene_keys[key] = sid
            return dict(id=sid, manifest=manifest, cached=False, **extra)
        finally:
            Log.sink = None

    # ------------------------------------------------------------------
    # running the command line

    def run(self, req) -> dict:
        export = req.get("export") or None
        argv = self._argv(req, export)
        self._n += 1
        rid = f"r{self._n}"
        r = dict(argv=argv, lines=[], done=False, ok=False, outputs=[], t0=time.time())
        self.runs[rid] = r

        def go():
            from .cli import main as cli_main
            with self.work:
                Log.sink = r["lines"].append
                r["lines"].append(O.command_line(argv))
                try:
                    r["ok"] = cli_main(list(argv)) == 0
                except SystemExit as exc:
                    r["lines"].append(f"ERROR: {exc.code if exc.code is not None else exc}")
                except Exception as exc:          # pragma: no cover - shown in the GUI
                    r["lines"].append(f"ERROR: {type(exc).__name__}: {exc}")
                    r["lines"].extend(traceback.format_exc().splitlines()[-6:])
                finally:
                    Log.sink = None
                    r["outputs"] = written(r["lines"])
                    r["done"] = True
        threading.Thread(target=go, daemon=True).start()
        return dict(id=rid, command=O.command_line(argv))

    # ------------------------------------------------------------------
    # the window

    def pick(self, mode: str, path: str, types: List[str]) -> List[str]:
        if self.window is None:
            return []
        import webview
        kind = {"folder": webview.FileDialog.FOLDER, "file": webview.FileDialog.OPEN,
                "save": webview.FileDialog.SAVE}[mode]
        start = path if os.path.isdir(path) else os.path.dirname(path) if path else ""
        kw = dict(directory=start)
        if mode == "save":
            kw["save_filename"] = os.path.basename(path) if path and not os.path.isdir(path) else ""
        if types:
            kw["file_types"] = tuple(types)
        got = self.window.create_file_dialog(kind, **kw)
        if not got:
            return []
        return [got] if isinstance(got, str) else list(got)

    def created(self, w):
        self.window = w

    def on_loaded(self, w):
        """Files dropped on the window arrive here with their full paths."""
        self.window = w
        try:
            from webview.dom import DOMEventHandler
        except ImportError:                     # pragma: no cover - an old pywebview
            return

        def dropped(e):
            files = (e.get("dataTransfer") or {}).get("files") or []
            paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
            if paths:
                w.evaluate_js(f"window.pcbApp && window.pcbApp.dropped({json.dumps(paths)})")
        try:
            w.dom.document.events.drop += DOMEventHandler(dropped, True, True)
        except Exception as exc:                # pragma: no cover
            print(f"drag and drop: {exc}", file=sys.stderr)


def written(lines: List[str]) -> List[str]:
    """The files a run wrote, from its messages ('Wrote X', 'Writing X ...')."""
    out = []
    for ln in lines:
        ln = ln.strip()
        if ln.startswith("Wrote "):
            p = ln[len("Wrote "):].strip()
        elif ln.startswith("Writing ") and ln.endswith(" ..."):
            p = ln[len("Writing "):-len(" ...")].strip()
        else:
            continue
        if p not in out:
            out.append(p)
    return out


def stack_rows(m) -> List[dict]:
    """The board from top to bottom for the layers panel: every layer that can
    be exported (with its targets), the dielectrics between them."""
    from . import exports
    st, o = m.stk, m.opts
    names = {L.name: L for L in m.board.layers}

    def rgb(c):
        return [round(float(v), 4) for v in c] if isinstance(c, (tuple, list)) else None
    rows = []
    for t in exports.targets(m):
        if t.kind == "outline":
            continue
        L = names.get(t.fab) or names.get(t.fab.replace(" stencil", ""))
        has = not (L is not None and getattr(L, "empty", False))
        rows.append(dict(kind=t.kind, side=t.side, name=t.fab, generic=t.generic, z=(t.z0 + t.z1) / 2,
                         t=round(t.z1 - t.z0, 5), color=rgb(t.color), data=has))
    for L in st.stack:
        if L.kind != "copper":
            rows.append(dict(kind="dielectric", side="", name=L.name, generic=None, z=(L.z0 + L.z1) / 2,
                             t=round(L.t, 5), color=rgb(o.board_color), data=True, material=L.material or ""))
    paste_z = {r["side"]: r["z"] for r in rows if r["kind"] == "paste"}
    for r in rows:
        if r["kind"] == "stencil":
            r["z"] = paste_z.get(r["side"], 0.0) + (1e-6 if r["side"] == "top" else -1e-6)
            r["t"] = round(o.stencil_thickness, 5)
    rows.sort(key=lambda r: -r["z"])
    return [dict(kind="outline", side="", name="Board outline", generic="outline", z=0, t=0,
                 color=rgb(o.board_color), data=True)] + rows


def open_path(path: str) -> bool:
    """Show a written file in its folder (Explorer), or open a folder."""
    if not path or not os.path.exists(path):
        return False
    try:
        if os.name == "nt":
            if os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path)              # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if os.path.isfile(path) else ["open", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) if os.path.isfile(path) else path])
        return True
    except OSError:
        return False


def main(argv: Optional[List[str]] = None) -> int:
    """pcbfab2step gui [INPUT ...] [options]: the window starts with this command line."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in argv):
        print("usage: pcbfab2step gui [INPUT ...] [options]\n\nThe options of the command line in a window, with "
              "a 3D view of what it writes.\nThe window needs pywebview: pip install pcbfab2step[gui]. "
              "--browser: show it in the browser instead.")
        return 0
    browser = "--browser" in argv
    argv = [a for a in argv if a != "--browser"]
    start = O.parse_argv(argv) if argv else dict(inputs=[], output=None, values={}, export=None)
    start["inputs"] = [os.path.abspath(p) for p in start["inputs"]]
    app = Gui(start)
    srv, port = serve(app)
    url = f"http://127.0.0.1:{port}/?t={app.key}"
    try:
        if not browser and have_webview():
            window(f"PCBFab2Step {__version__}", url, on_loaded=app.on_loaded, on_created=app.created)
        else:
            import webbrowser
            if not browser:
                print("pywebview is not installed (pip install pcbfab2step[gui]); the GUI opens in the browser.")
            print(f"PCBFab2Step GUI: {url}\nStop with Ctrl+C.")
            webbrowser.open(url)
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
    return 0
