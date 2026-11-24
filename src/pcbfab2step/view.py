# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""`--view`: show what a command would write, and write nothing.

With pywebview (pip install pcbfab2step[gui]) the scene opens in a window of
its own; without it, as a self-contained page in the browser. The pages and
the GUI are served to the window by a small WSGI app on 127.0.0.1 with a
random key in the URL, so no other program on the machine can use it.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs

from .common import info, warn
from .scene import MODULES, WEB, to_html

Response = Tuple[str, list, bytes]


def have_webview() -> bool:
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


def page(title: str, body: str, boot: str, css=("viewer.css",)) -> str:
    """An HTML page of the web app: the viewer modules by URL."""
    import html
    imports = {k: "./web/" + v for k, v in MODULES.items()}
    links = "".join(f"<link rel='stylesheet' href='./web/{c}'>" for c in css)
    return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(title)}</title>{links}"
            f"<script type='importmap'>{json.dumps({'imports': imports})}</script></head>"
            f"{body}<script type='module' src='./web/{boot}'></script></html>")


class WebApp:
    """A WSGI app: static files of web/, and routes. Every request but the
    static files needs the key (?t=KEY or the X-Key header)."""

    def __init__(self):
        self.key = secrets.token_urlsafe(18)
        self.routes: Dict[Tuple[str, str], Callable] = {}

    def route(self, method: str, path: str):
        def deco(fn):
            self.routes[(method, path)] = fn
            return fn
        return deco

    @staticmethod
    def json(obj, status="200 OK") -> Response:
        return status, [("Content-Type", "application/json; charset=utf-8")], json.dumps(obj).encode("utf-8")

    @staticmethod
    def error(msg: str, status="400 Bad Request") -> Response:
        return WebApp.json({"error": msg}, status)

    def static(self, rel: str) -> Response:
        path = os.path.normpath(os.path.join(WEB, rel))
        if not path.startswith(os.path.normpath(WEB) + os.sep) or not os.path.isfile(path):
            return "404 Not Found", [("Content-Type", "text/plain")], b"not found"
        ctype = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml"}.get(
            os.path.splitext(path)[1], mimetypes.guess_type(path)[0] or "application/octet-stream")
        with open(path, "rb") as f:
            return "200 OK", [("Content-Type", ctype), ("Cache-Control", "no-cache")], f.read()

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/") or "/"
        q = {k: v[-1] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        try:
            if path.startswith("/web/"):
                status, headers, body = self.static(path[5:])
            elif path == "/favicon.ico":
                status, headers, body = "204 No Content", [], b""
            elif q.get("t") != self.key and environ.get("HTTP_X_KEY") != self.key:
                status, headers, body = "403 Forbidden", [("Content-Type", "text/plain")], b"no key"
            else:
                fn = self.routes.get((method, path))
                if fn is None:
                    status, headers, body = "404 Not Found", [("Content-Type", "text/plain")], b"not found"
                else:
                    size = int(environ.get("CONTENT_LENGTH") or 0)
                    raw = environ["wsgi.input"].read(size) if size else b""
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                    status, headers, body = fn(req, q)
        except SystemExit as exc:                  # the command line's own messages
            status, headers, body = self.error(str(exc.code if exc.code is not None else exc))
        except Exception as exc:                    # pragma: no cover - shown in the GUI
            import traceback
            traceback.print_exc()
            status, headers, body = self.error(f"{type(exc).__name__}: {exc}", "500 Internal Server Error")
        start_response(status, headers + [("Content-Length", str(len(body)))])
        return [body]


def serve(app) -> Tuple[object, int]:
    """Serve a WSGI app on 127.0.0.1 (a free port) in a thread."""
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

    class Quiet(WSGIRequestHandler):
        def log_message(self, *a):
            pass

    class Threaded(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    srv = make_server("127.0.0.1", 0, app, server_class=Threaded, handler_class=Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def window(title: str, url: str, js_api=None, on_loaded=None, on_created=None, size=(1480, 920)):
    """A pywebview window on the URL; returns when it is closed."""
    import webview
    dark = _windows_dark()
    w = webview.create_window(title, url, js_api=js_api, width=size[0], height=size[1], min_size=(960, 600),
                              background_color="#1d2126" if dark else "#e9ecef", text_select=True)
    if on_created:
        on_created(w)
    if on_loaded:
        w.events.loaded += lambda: on_loaded(w)
    webview.start(private_mode=True)
    return w


def _windows_dark() -> bool:
    if os.name != "nt":
        return True
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return True


# --------------------------------------------------------------------------
# --view
# --------------------------------------------------------------------------

class ViewApp(WebApp):
    """One scene in a window."""

    def __init__(self, manifest: dict, blob: bytes):
        super().__init__()
        self.manifest, self.blob = manifest, blob

        @self.route("GET", "/")
        def index(req, q):
            body = "<body class='viewer-only'><main id='viewer' class='viewport'></main></body>"
            return "200 OK", [("Content-Type", "text/html; charset=utf-8")], \
                page(manifest.get("title", "PCBFab2Step"), body, "view.js").encode("utf-8")

        @self.route("GET", "/scene.json")
        def scene_json(req, q):
            return self.json(self.manifest)

        @self.route("GET", "/scene.bin")
        def scene_bin(req, q):
            return "200 OK", [("Content-Type", "application/octet-stream")], self.blob


def show(manifest: dict, blob: bytes, browser: Optional[bool] = None) -> str:
    """Open a scene: a pywebview window, else the browser. Returns how."""
    title = manifest.get("title", "PCBFab2Step")
    if browser is None:
        browser = not have_webview()
    if not browser:
        app = ViewApp(manifest, blob)
        srv, port = serve(app)
        try:
            window(f"{title} - PCBFab2Step", f"http://127.0.0.1:{port}/?t={app.key}")
        finally:
            srv.shutdown()
        return "window"
    fd, path = tempfile.mkstemp(prefix="pcbfab2step-view-", suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(to_html(manifest, blob))
    webbrowser.open("file:///" + path.replace(os.sep, "/").lstrip("/"))
    info(f"Opened {path} in the browser (pip install pcbfab2step[gui] shows it in a window of its own)")
    return path


def run_view(args, mode: str) -> int:
    """`--view [exact]`: the model (or with --export the layer) as it would be
    written; nothing is written."""
    from . import scene
    from .pipeline import Job
    t0 = time.time()
    job = Job(args, mode)
    for line in job.input_lines():
        info(line)
    if mode == "export":
        sc = scene.layer_scene(job, args.export)
    else:
        if args.view == "exact":
            info("Building the exact solids (as in the STEP) ...")
        sc = scene.model_scene(job, exact=args.view == "exact")
    for line in sc.info:
        info("  " + line)
    manifest, blob = sc.pack()
    info(f"Scene: {len(sc.parts)} bodies, {manifest['triangles']:,} triangles in {time.time() - t0:.1f} s; "
         "nothing written")
    if os.environ.get("PCBFAB2STEP_NO_WINDOW"):          # tests
        return 0
    try:
        show(manifest, blob)
    except Exception as exc:                             # no GUI backend: the browser
        warn(f"no window ({exc}); showing it in the browser")
        show(manifest, blob, browser=True)
    return 0
