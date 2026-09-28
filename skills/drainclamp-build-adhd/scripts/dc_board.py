#!/usr/bin/env python3
"""Projects board: a localhost dashboard over every registered project.

    python dc_board.py serve [--port 8765] [--open]

Reads the registry, each project's state file and its sidecar, and serves them
as JSON plus one page (`board.html`). Edits made on the page go through
`dc_project.apply_op`, so the board and the CLI share one set of rules.

It is deliberately narrow:
- binds 127.0.0.1 only and refuses any request whose Host is not loopback, so
  another machine or a DNS-rebinding page cannot reach it;
- every API call carries a per-run random token, printed once in the URL, so
  another local page cannot read or write through it;
- it writes the sidecar, a new project's scaffold, and registry status -- never
  the state file. Milestone status stays with dc_state.py. An existing project
  is addressed by registry id, never by a path from the request; a new one is
  created only inside a `project_roots` folder from config.json, under a
  slug-only name that must not already exist.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import socket
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import _dcio
import dc_project
import dc_registry
import dc_state
from _dcio import DcError

DEFAULT_PORT = 8765
MAX_BODY = 64 * 1024
PAGE = Path(__file__).resolve().parent / "board.html"
LOOPBACK = ("127.0.0.1", "localhost")

FALLBACK_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>DrainClamp board</title></head><body>
<p>board.html is missing next to dc_board.py. The API is running.</p></body></html>"""


def project_id(root: str) -> str:
    return hashlib.sha1(dc_registry._key(root).encode("utf-8")).hexdigest()[:10]


def _entries(home: str | None) -> list[dict]:
    return dc_registry.listing("active", home) + dc_registry.listing("complete", home)


def _state(root: Path):
    text = _dcio.read_text(root / _dcio.AGENT_DIR_NAME / _dcio.STATE_NAME)
    return dc_state.State.parse(text) if text is not None else None


def _log_lines(state) -> list[str]:
    if state is None:
        return []
    return [l[2:].strip() for l in state.sections.get("LOG", "").splitlines()
            if l.startswith("- ")]


def overview(entry: dict, home: str | None) -> dict:
    root = Path(entry["root"])
    item = {"id": project_id(entry["root"]), "name": entry.get("name") or root.name,
            "root": entry["root"], "status": entry["status"], "last": entry.get("last"),
            "roadmap": {"done": 0, "total": 0}, "milestone": None, "summary": None,
            "error": None}
    try:
        rows = dc_project.roadmap(root)
        item["roadmap"] = {"done": sum(1 for r in rows if r["status"] == "done"),
                           "total": len(rows)}
        ms = dc_project.current_milestone(rows)
        item["milestone"] = ms["id"] if ms else None
        item["milestone_goal"] = ms["goal"] if ms else None
        item["summary"] = dc_project.summary(dc_project.load(root / _dcio.AGENT_DIR_NAME), home)
    except (DcError, KeyError, ValueError) as exc:
        item["error"] = str(exc)  # one broken project must not blank the board
    return item


def detail(entry: dict, home: str | None) -> dict:
    root = Path(entry["root"])
    agent = root / _dcio.AGENT_DIR_NAME
    state = _state(root)
    rows = dc_state.parse_roadmap(state.sections["ROADMAP"]) if state else []
    data = dc_project.load(agent)
    ms = dc_project.current_milestone(rows)
    return {"id": project_id(entry["root"]), "name": entry.get("name") or root.name,
            "root": entry["root"], "status": entry["status"], "last": entry.get("last"),
            "roadmap": rows, "milestone": ms["id"] if ms else None, "data": data,
            "summary": dc_project.summary(data, home), "log": _log_lines(state),
            "charter": dc_project.load_charter(agent), "ops": list(dc_project.OPS)}


class Handler(BaseHTTPRequestHandler):
    server_version = "dc_board"
    home: str | None = None
    token: str = ""

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        raw = body if isinstance(body, bytes) else (
            json.dumps(body).encode("utf-8") if ctype == "application/json"
            else body.encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in LOOPBACK

    def _token_ok(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-DC-Token", ""), self.token)

    def _guard(self) -> bool:
        if not self._host_ok() or not self._token_ok():
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def _entry(self, pid: str) -> dict | None:
        return next((e for e in _entries(self.home) if project_id(e["root"]) == pid), None)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            if not self._host_ok():
                self._send(403, {"error": "forbidden"})
                return
            page = _dcio.read_text(PAGE) or FALLBACK_PAGE
            self._send(200, page, "text/html")
            return
        if not url.path.startswith("/api/"):
            self._send(404, {"error": "not found"})
            return
        if not self._guard():
            return
        try:
            if url.path == "/api/projects":
                self._send(200, {"projects": [overview(e, self.home) for e in _entries(self.home)],
                                 "hourly_rate": dc_project.global_rate(self.home),
                                 "project_roots": [str(r) for r in
                                                   dc_project.project_roots(self.home)]})
                return
            if url.path == "/api/project":
                pid = (parse_qs(url.query).get("id") or [""])[0]
                entry = self._entry(pid)
                if entry is None:
                    self._send(404, {"error": f"no project {pid}"})
                    return
                self._send(200, detail(entry, self.home))
                return
        except DcError as exc:
            self._send(500, {"error": str(exc)})
            return
        self._send(404, {"error": "not found"})

    def _body(self) -> dict | None:
        """The JSON object sent, or None after an error response has gone out."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._send(413, {"error": f"body over {MAX_BODY} bytes"})
            self.close_connection = True
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "body is not JSON"})
            return None
        if not isinstance(body, dict):
            self._send(400, {"error": "expected a JSON object"})
            return None
        return body

    def _project(self, body: dict) -> tuple[dict, int] | None:
        """(registry entry, expected rev) for an edit, or None after an error."""
        entry = self._entry(str(body.get("id", "")))
        if entry is None:
            self._send(404, {"error": "no such project"})
            return None
        rev = body.get("rev")
        if not isinstance(rev, int) or isinstance(rev, bool):
            self._send(400, {"error": "rev (integer) is required"})
            return None
        return entry, rev

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        routes = {"/api/op": self._op, "/api/create": self._create,
                  "/api/status": self._status}
        if path not in routes:
            self._send(404, {"error": "not found"})
            return
        if not self._guard():
            return
        body = self._body()
        if body is not None:
            routes[path](body)

    def _op(self, body: dict) -> None:
        if not isinstance(body.get("args", {}), dict):
            self._send(400, {"error": "expected {id, rev, op, args}"})
            return
        op = body.get("op")
        if op not in dc_project.OPS:
            self._send(400, {"error": f"unknown operation: {op}"})
            return
        found = self._project(body)
        if found is None:
            return
        entry, rev = found
        root = Path(entry["root"])
        agent = root / _dcio.AGENT_DIR_NAME
        result = []

        def apply(data: dict) -> dict:
            result.append(dc_project.apply_op(data, root, op, body.get("args") or {}))
            return data

        try:
            current = dc_project.load(agent)["rev"]
            if current != rev:
                self._send(409, {"error": f"project changed (revision {current}); reload",
                                 "rev": current})
                return
            data = dc_project.mutate(agent, apply, expect=rev)
        except DcError as exc:
            code = 409 if "revision" in str(exc) else 400
            self._send(code, {"error": str(exc)})
            return
        self._send(200, {"ok": True, "message": result[0], "rev": data["rev"]})

    def _create(self, body: dict) -> None:
        folder, location = body.get("folder"), body.get("location")
        if (folder is not None and not isinstance(folder, str)) or                 (location is not None and not isinstance(location, str)):
            self._send(400, {"error": "folder and location must be strings"})
            return
        try:
            made = dc_project.create_project(body.get("charter"), self.home, location, folder)
        except DcError as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(200, {"ok": True, "id": project_id(made["root"]), **made})

    def _status(self, body: dict) -> None:
        status = body.get("status")
        if status not in dc_registry.STATUSES:
            self._send(400, {"error": f"status must be one of {', '.join(dc_registry.STATUSES)}"})
            return
        found = self._project(body)
        if found is None:
            return
        entry, rev = found
        note = body.get("note") or ""
        if not isinstance(note, str):
            self._send(400, {"error": "note must be text"})
            return
        try:
            data = dc_project.set_lifecycle(Path(entry["root"]), status, self.home, note,
                                            expect=rev)
        except DcError as exc:
            code = 409 if "revision" in str(exc) else 400
            self._send(code, {"error": str(exc)})
            return
        word = "marked complete" if status == "complete" else "reopened"
        self._send(200, {"ok": True, "message": f"{entry.get('name')} {word}",
                         "rev": data["rev"]})


class ExclusiveServer(ThreadingHTTPServer):
    """A port another process holds is refused, never shared.

    HTTPServer sets SO_REUSEADDR, which on Windows lets a second socket bind a
    port that is already listening -- so the board could sit beside some other
    server and neither would own the traffic. Windows gets SO_EXCLUSIVEADDRUSE
    instead; elsewhere SO_REUSEADDR only skips TIME_WAIT and is kept.
    """

    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(home: str | None, port: int, token: str) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"home": home, "token": token})
    server = ExclusiveServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_board.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cmd", choices=["serve"])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--home", default=None, help="registry/config directory override")
    parser.add_argument("--open", action="store_true", help="open the board in a browser")
    args = parser.parse_args()

    token = secrets.token_urlsafe(18)
    try:
        server = make_server(args.home, args.port, token)
    except OSError as exc:
        raise DcError(f"cannot listen on 127.0.0.1:{args.port} ({exc}); try --port") from None
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    print(f"DRAINCLAMP: board running at {url}")
    print("Ctrl+C stops it. The link works only while this process runs.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return _dcio.EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DcError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(_dcio.EXIT_INTERNAL)
