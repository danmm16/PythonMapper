"""Local web UI: a tiny stdlib HTTP server bound to 127.0.0.1 only."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import discover, manage

UI_FILE = Path(__file__).with_name("ui.html")
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")


def render_page(data=None, token=None) -> str:
    """The UI page. With data and no token it is a self-contained snapshot."""
    boot = json.dumps({"token": token, "data": data}).replace("</", "<\\/")
    return UI_FILE.read_text(encoding="utf-8").replace("/*__BOOT__*/", f"window.BOOT = {boot};", 1)


def open_folder(path: str):
    if discover.WINDOWS:
        os.startfile(path)  # noqa: S606 - path comes from the scan, not the request
    else:
        subprocess.Popen(["open" if discover.MACOS else "xdg-open", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Job:
    """A plan running in a background thread; the UI polls its output."""

    def __init__(self, plan: manage.Plan):
        self.id = secrets.token_hex(8)
        self.title = plan.title
        self.output = []
        self.done = False
        self.ok = None


class App:
    def __init__(self, scan_options: dict):
        self.options = scan_options
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.data = None
        self.hosts = set()
        self.job = None
        self.job_lock = threading.Lock()

    def scan(self, refresh=False) -> dict:
        with self.lock:
            if refresh or self.data is None:
                data = discover.scan(**self.options)
                data["tools"] = manage.find_tools(data)
                self.data = data
            return self.data

    def find(self, body: dict) -> dict:
        data = self.scan()
        for item in data["environments"] + data["kernels"]:
            if item["id"] == body.get("id"):
                return item
        raise LookupError("Unknown item; try rescanning.")

    def size(self, body):
        item = self.find(body)
        item["size"] = discover.dir_size(item["path"])
        return {"id": item["id"], "size": item["size"]}

    def open(self, body):
        path = self.find(body)["path"]
        if not os.path.isdir(path):
            raise ValueError("That folder no longer exists.")
        open_folder(path)
        return {"ok": True}

    def _plan(self, body) -> manage.Plan:
        data = self.scan()
        try:
            return manage.make_plan(data, str(body.get("op")), body.get("params") or {}, data["tools"])
        except manage.ManageError as exc:
            raise ValueError(str(exc))

    def plan(self, body):
        return self._plan(body).to_dict()

    def run(self, body):
        plan = self._plan(body)
        with self.job_lock:
            if self.job and not self.job.done:
                raise ValueError("Another operation is still running.")
            job = self.job = Job(plan)

        def work():
            try:
                job.ok = manage.run_plan(plan, job.output.append)
            except Exception as exc:  # keep the server alive, report in the log
                job.output.append(f"\nError: {type(exc).__name__}: {exc}\n")
                job.ok = False
            try:
                self.scan(refresh=True)
            finally:
                job.done = True

        threading.Thread(target=work, daemon=True).start()
        return {"job": job.id, "title": job.title}

    def job_status(self, job_id: str, since: int):
        job = self.job
        if not job or job.id != job_id:
            raise LookupError("Unknown job.")
        out = job.output[since:]
        reply = {"title": job.title, "output": "".join(out), "next": since + len(out), "done": job.done, "ok": job.ok}
        if job.done:
            reply["data"] = self.scan()
        return reply


class Handler(BaseHTTPRequestHandler):
    app: App = None  # set on a subclass by serve()
    server_version = "PythonMapper"

    def log_message(self, *args):
        pass

    def send(self, code: int, body, ctype="application/json"):
        raw = (json.dumps(body) if ctype == "application/json" else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype == "text/html":
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(raw)

    def host_ok(self) -> bool:  # blocks DNS-rebinding attacks from web pages
        return self.headers.get("Host", "") in self.app.hosts

    def token_ok(self) -> bool:
        return self.host_ok() and secrets.compare_digest(self.headers.get("X-Token", ""), self.app.token)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self.host_ok():
            return self.send(403, {"error": "Forbidden"})
        if url.path == "/":
            return self.send(200, render_page(token=self.app.token), "text/html")
        if not self.token_ok():
            return self.send(403, {"error": "Forbidden"})
        if url.path == "/api/scan":
            return self.send(200, self.app.scan(query.get("refresh") == ["1"]))
        if url.path == "/api/job":
            try:
                since = int(query.get("since", ["0"])[0])
                return self.send(200, self.app.job_status(query.get("id", [""])[0], since))
            except (LookupError, ValueError) as exc:
                return self.send(404, {"error": str(exc)})
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        if not self.token_ok():
            return self.send(403, {"error": "Forbidden"})
        route = {"/api/size": self.app.size, "/api/open": self.app.open,
                 "/api/plan": self.app.plan, "/api/run": self.app.run}.get(urlparse(self.path).path)
        if route is None:
            return self.send(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise ValueError
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {"error": "Bad request"})
        try:
            self.send(200, route(body))
        except LookupError as exc:
            self.send(404, {"error": str(exc)})
        except ValueError as exc:
            self.send(400, {"error": str(exc)})
        except Exception as exc:  # report, keep serving
            self.send(500, {"error": f"{type(exc).__name__}: {exc}"})


def serve(scan_options: dict, port: int = 0, open_browser: bool = True):
    app = App(scan_options)
    print("Scanning for Python installations...", file=sys.stderr)
    data = app.scan()
    print(f"Found {len(data['environments'])} interpreters/environments and "
          f"{len(data['kernels'])} Jupyter kernels in {data['duration']}s.", file=sys.stderr)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    port = httpd.server_address[1]
    app.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    url = f"http://127.0.0.1:{port}/"
    print(f"PythonMapper is running at {url}  (press Ctrl+C to stop)", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        httpd.server_close()
