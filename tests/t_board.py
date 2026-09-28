"""Server checks for dc_board.py: listing, detail, guarded writes, refusals."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import dc_board  # noqa: E402
import dc_project  # noqa: E402
import dc_registry  # noqa: E402
import dc_state  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcboard-"))
home = tmp / "home"


def make_repo(name, roadmap) -> Path:
    root = tmp / name
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    text = dc_state.template_text().replace(
        "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
        f"<!-- DC:ROADMAP -->\n{roadmap}\n")
    (root / ".agent" / "drainclamp-state.md").write_text(text, encoding="utf-8")
    dc_registry.touch(root, home)
    return root


repo = make_repo("alpha", "| m1 | build | a.py | active |\n| m2 | ship | b.py | pending |")
make_repo("beta", "| m1 | only | c.py | done |")

TOKEN = "test-token-123"
server = dc_board.make_server(home=str(home), port=0, token=TOKEN)
threading.Thread(target=server.serve_forever, daemon=True).start()
host, port = server.server_address
BASE = f"http://127.0.0.1:{port}"

check("server binds loopback only", host == "127.0.0.1", host)


def call(path, body=None, token=TOKEN, headers=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data is not None else "GET")
    if token:
        req.add_header("X-DC-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(raw) if "json" in ctype else raw.decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw.decode(errors="replace")


# 1. the page itself loads without a token (it carries none of the data)
code, page = call("/", token=None)
check("index page is served", code == 200 and "<html" in page.lower(), code)

# 2. every API call needs the token and a loopback Host
code, _ = call("/api/projects", token=None)
check("API refuses a missing token", code == 403, code)
code, _ = call("/api/projects", token="wrong")
check("API refuses a wrong token", code == 403, code)
code, _ = call("/api/projects", headers={"Host": "evil.example:80"})
check("API refuses a foreign Host header", code == 403, code)

# 3. listing covers every registered project with progress
code, body = call("/api/projects")
names = {p["name"]: p for p in body.get("projects", [])} if code == 200 else {}
check("listing returns both projects", set(names) == {"alpha", "beta"}, list(names))
check("listing carries roadmap progress", names.get("alpha", {}).get("roadmap") ==
      {"done": 0, "total": 2}, names.get("alpha"))
check("listing names the current milestone",
      names.get("alpha", {}).get("milestone") == "m1", names.get("alpha"))
pid = names["alpha"]["id"]

# 4. detail for one project
code, body = call(f"/api/project?id={pid}")
check("detail loads", code == 200, code)
check("detail carries roadmap rows", [r["id"] for r in body.get("roadmap", [])] == ["m1", "m2"])
check("detail carries the sidecar", body.get("data", {}).get("schema") == 1)
code, _ = call("/api/project?id=nope")
check("unknown project id is 404", code == 404, code)

# 5. a write goes through dc_project and bumps the revision
code, body = call("/api/op", {"id": pid, "rev": 0, "op": "chunk.add",
                              "args": {"milestone": "m1", "goal": "first step",
                                       "targets": "a.py::go"}})
check("chunk.add via the board succeeds", code == 200 and body.get("rev") == 1, body)
side = json.loads((repo / ".agent" / dc_project.SIDECAR_NAME).read_text(encoding="utf-8"))
check("the write landed in the repo's sidecar", side["chunks"]["m1"][0]["goal"] == "first step")

# 6. a stale revision is a conflict, not an overwrite
code, body = call("/api/op", {"id": pid, "rev": 0, "op": "todo.add", "args": {"text": "x"}})
check("stale revision is 409", code == 409, (code, body))
side = json.loads((repo / ".agent" / dc_project.SIDECAR_NAME).read_text(encoding="utf-8"))
check("stale write changed nothing", side["todos"] == [] and side["rev"] == 1)

# 7. writes need the token too, and bad input is refused cleanly
code, _ = call("/api/op", {"id": pid, "rev": 1, "op": "todo.add", "args": {"text": "x"}},
               token=None)
check("write without token is 403", code == 403, code)
code, body = call("/api/op", {"id": pid, "rev": 1, "op": "rm.rf", "args": {}})
check("unknown operation is 400", code == 400, (code, body))
code, _ = call("/api/op", b"{not json")
check("malformed JSON is 400", code == 400, code)
code, _ = call("/api/op", b"x" * (dc_board.MAX_BODY + 1))
check("oversized body is 413", code == 413, code)
code, body = call("/api/op", {"id": pid, "rev": 1, "op": "chunk.add",
                              "args": {"milestone": "m9", "goal": "g"}})
check("dc_project validation surfaces as 400", code == 400 and "m9" in body.get("error", ""),
      (code, body))

# 8. the board never writes the state file
before = (repo / ".agent" / "drainclamp-state.md").read_text(encoding="utf-8")
call("/api/op", {"id": pid, "rev": 1, "op": "todo.add", "args": {"text": "y"}})
after = (repo / ".agent" / "drainclamp-state.md").read_text(encoding="utf-8")
check("state file untouched by board writes", before == after)

# 9. the page matches the API it drives: every op it sends exists, and its copies
#    of dc_project's constants have not drifted
import re  # noqa: E402

html = dc_board.PAGE.read_text(encoding="utf-8")
check("board.html is served, not the fallback", page == html)
sent = set(re.findall(r'"([a-z]+\.[a-z]+)"', html))  # every quoted "area.verb" is an op name
check("page sends only known operations", sent and sent <= set(dc_project.OPS),
      sorted(sent - set(dc_project.OPS)))
for path in ("/api/projects", "/api/project?id=", "/api/op", "/api/create", "/api/status"):
    check(f"page calls {path}", f'"{path}' in html)


def js_list(name):
    m = re.search(rf"const {name} = \[(.*?)\];", html, re.S)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else None


check("AREAS mirror matches dc_project", js_list("AREAS") == list(dc_project.AREAS))
check("CHARTER_TEXT mirror matches dc_project",
      js_list("CHARTER_TEXT") == list(dc_project.CHARTER_TEXT))
check("MoSCoW mirror matches the charter enum",
      js_list("MOSCOW") == list(dc_project.CHARTER_ENUMS["priority"]))
check("risk levels mirror the charter enum",
      js_list("LEVELS") == list(dc_project.CHARTER_ENUMS["likelihood"]))
m = re.search(r"const HEALTH = \{(.*?)\};", html)
check("HEALTH keys mirror dc_project",
      m is not None and re.findall(r"(\w+):", m.group(1)) == list(dc_project.HEALTH))
check("page loads no script from anywhere else", "<script src" not in html)
import dc_tokens  # noqa: E402
check("token cells read the fields dc_tokens stores",
      all(f"c.{k}" in html for k in dc_tokens.PROJECT_FIELDS)
      and "d.data.tokens.chunks" in html and "d.data.tokens.milestones" in html)

# 10. a port another process holds is refused, never shared
try:
    twin = dc_board.make_server(home=str(home), port=port, token=TOKEN)
except OSError:
    check("second server on a taken port is refused", True)
else:
    twin.server_close()
    check("second server on a taken port is refused", False, f"bound {port} twice")

server.shutdown()
server.server_close()

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
