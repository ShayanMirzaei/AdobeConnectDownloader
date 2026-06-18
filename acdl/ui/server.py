"""Local web UI: a tiny stdlib HTTP server + JSON API over the JobManager.

No web framework — http.server only, so the app stays self-contained and cross-platform.
Routes:
  GET  /                      -> the UI
  GET  /app.js | /style.css   -> static assets
  GET  /api/jobs              -> list jobs with live progress
  POST /api/jobs {url}        -> start a download
  POST /api/jobs/<id>/pause|resume|remove
Run via the `acdl-ui` console script (opens the browser automatically).
"""
from __future__ import annotations
import argparse
import http.server
import json
import os
import re
import sys
import threading
import urllib.parse
import webbrowser

from .. import applog
from ..jobs.manager import JobManager


def _static_dir() -> str:
    # When frozen by PyInstaller, data files live under sys._MEIPASS (see packaging/*.spec).
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "acdl", "ui", "static")
    return os.path.join(os.path.dirname(__file__), "static")


STATIC = _static_dir()
_MANAGER: JobManager | None = None
_CTYPE = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
          ".css": "text/css; charset=utf-8"}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a):  # keep the console quiet
        pass

    def _json(self, code: int, body) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static(self, name: str) -> None:
        path = os.path.normpath(os.path.join(STATIC, name))
        if not path.startswith(STATIC) or not os.path.isfile(path):
            return self._json(404, {"error": "not found"})
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _CTYPE.get(os.path.splitext(path)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        p = urllib.parse.urlparse(self.path).path
        if p == "/":
            return self._static("index.html")
        if p in ("/app.js", "/style.css"):
            return self._static(p.lstrip("/"))
        if p == "/api/jobs":
            return self._json(200, _MANAGER.list())
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        p = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        if p == "/api/jobs":
            url = (body.get("url") or "").strip()
            if not url:
                return self._json(400, {"error": "missing url"})
            return self._json(200, {"id": _MANAGER.submit(url)})
        m = re.match(r"^/api/jobs/([A-Za-z0-9]+)/(pause|resume|remove)$", p)
        if m:
            getattr(_MANAGER, m.group(2))(m.group(1))
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})


def main() -> None:
    global _MANAGER
    ap = argparse.ArgumentParser(prog="acdl-ui", description="AdobeConnectDownloader web UI.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--downloads", default="downloads", help="folder for jobs + finished MP4s")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    applog.setup()
    log = applog.get("acdl.ui")
    _MANAGER = JobManager(root=args.downloads)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    log.info("AdobeConnectDownloader UI  →  %s   (Ctrl+C to stop)", url)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
