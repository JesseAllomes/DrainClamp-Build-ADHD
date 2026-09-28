"""Project lifecycle: create from a charter, complete, reopen -- CLI functions and board."""
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
from _dcio import DcError  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def refused(fn, needle=""):
    try:
        fn()
    except DcError as exc:
        return needle.lower() in str(exc).lower()
    return False


tmp = Path(tempfile.mkdtemp(prefix="dclife-"))
home = tmp / "home"
home.mkdir()
roots = tmp / "projects"
roots.mkdir()
other = tmp / "elsewhere"
other.mkdir()
H = str(home)

CHARTER = {"name": "KPI's FY26-27", "purpose": "Track KPIs in one place",
           "scope": [{"feature": "Dashboard", "what": "shows KPIs", "priority": "Must"},
                     {"feature": "", "what": "", "priority": "Must"}],
           "risks": [{"risk": "stale data", "likelihood": "Med", "impact": "High",
                      "guardrail": "show last refresh"}],
           "data": ["Personal information"],
           "value": {"baseline_min": 60, "new_min": 10, "runs_per_year": 12,
                     "hourly_rate": 80}}

# 1. no project_roots configured -> refused, nothing created
check("create refused without project_roots",
      refused(lambda: dc_project.create_project(CHARTER, H), "project_roots"))
check("nothing created without roots", not any(roots.iterdir()))

(home / "config.json").write_text(json.dumps({"project_roots": [str(roots)]}), encoding="utf-8")

# 2. charter validation
check("name is required", refused(lambda: dc_project.clean_charter({"purpose": "p"}), "name"))
check("purpose is required", refused(lambda: dc_project.clean_charter({"name": "n"}), "purpose"))
check("unknown charter field refused",
      refused(lambda: dc_project.clean_charter({"name": "n", "purpose": "p", "shell": "x"}),
              "unknown"))
check("bad MoSCoW value refused", refused(lambda: dc_project.clean_charter(
    {"name": "n", "purpose": "p", "scope": [{"feature": "f", "priority": "Maybe"}]}), "priority"))
check("negative value refused", refused(lambda: dc_project.clean_charter(
    {"name": "n", "purpose": "p", "value": {"new_min": -1}}), "negative"))
check("oversized text refused", refused(lambda: dc_project.clean_charter(
    {"name": "n", "purpose": "x" * (dc_project.TEXT_CAP + 1)}), "over"))

# 3. folder names: slug only, no traversal, no reserved names
check("slugify drops apostrophes", dc_project.slugify("KPI's FY26-27") == "kpis-fy26-27")
for bad in ("../evil", "a/b", "Upper", "-x", "con", "a b", ""):
    check(f"folder {bad!r} refused", refused(
        lambda b=bad: dc_project.create_project(CHARTER, H, folder=b or "!!!")))
check("location outside project_roots refused",
      refused(lambda: dc_project.create_project(CHARTER, H, location=str(other)), "not one of"))
check("nothing written by refused creates", not any(roots.iterdir()) and not any(other.iterdir()))

# 4. a good create builds the whole scaffold and registers it
made = dc_project.create_project(CHARTER, H)
target = roots / "kpis-fy26-27"
agent = target / ".agent"
check("folder created from the name", made["folder"] == "kpis-fy26-27" and target.is_dir(), made)
check("own git repository", (target / ".git").exists())
top = subprocess.run(["git", "-C", str(target), "rev-parse", "--show-toplevel"],
                     capture_output=True, text=True).stdout.strip()
check("git toplevel is the new folder", Path(top).resolve() == target.resolve(), top)
state = (agent / "drainclamp-state.md").read_text(encoding="utf-8")
check("state file is the template", "generation=1" in state and "<!-- DC:ROADMAP -->" in state)
charter = dc_project.load_charter(agent)
check("charter saved", charter and charter["name"] == "KPI's FY26-27", charter)
check("blank scope rows dropped", charter and len(charter["scope"]) == 1)
side = dc_project.load(agent)
check("sidecar seeded with savings", side["savings"].get("baseline_min") == 60 and
      side["savings"].get("hourly_rate") == 80, side["savings"])
check("profile seeded from the charter", side["profile"]["summary"] == "Track KPIs in one place"
      and [f["name"] for f in side["profile"]["features"]] == ["Dashboard"] and
      [g["area"] for g in side["profile"]["safeguards"]] == ["Guardrails"], side["profile"])
entry = dc_registry.find(dc_registry.load(H), target)
check("registered active under the charter name",
      entry and entry["status"] == "active" and entry["name"] == "KPI's FY26-27", entry)
check("listed in the Gate S menu",
      [e["name"] for e in dc_registry.listing("active", H)] == ["KPI's FY26-27"])

# 5. never over an existing folder
check("existing target refused", refused(lambda: dc_project.create_project(CHARTER, H),
                                         "already exists"))
(roots / "keep").mkdir()
(roots / "keep" / "mine.txt").write_text("x", encoding="utf-8")
check("existing non-project folder refused",
      refused(lambda: dc_project.create_project(CHARTER, H, folder="keep"), "already exists"))
check("existing folder left alone", (roots / "keep" / "mine.txt").read_text() == "x")

# 6. complete and reopen: registry status + sidecar stamp, state untouched
rev = dc_project.load(agent)["rev"]
check("stale revision refused", refused(
    lambda: dc_project.set_lifecycle(target, "complete", H, expect=rev + 5), "revision"))
check("stale refusal left status alone",
      dc_registry.find(dc_registry.load(H), target)["status"] == "active")
dc_project.set_lifecycle(target, "complete", H, note="shipped v1", expect=rev)
check("registry says complete", dc_registry.find(dc_registry.load(H), target)["status"] ==
      "complete")
check("gone from the active menu", dc_registry.listing("active", H) == [])
done = dc_project.load(agent).get("completed")
check("sidecar stamped with note", done and done["note"] == "shipped v1" and done["at"], done)
dc_project.set_lifecycle(target, "active", H)
check("reopen sets active", dc_registry.find(dc_registry.load(H), target)["status"] == "active")
check("reopen clears the stamp", dc_project.load(agent).get("completed") is None)
check("state untouched by lifecycle",
      (agent / "drainclamp-state.md").read_text(encoding="utf-8") == state)
(agent / "drainclamp-project.json").write_text("{broken", encoding="utf-8")
check("damaged sidecar stops completion", refused(
    lambda: dc_project.set_lifecycle(target, "complete", H)))
check("registry unchanged after damaged sidecar",
      dc_registry.find(dc_registry.load(H), target)["status"] == "active")
(agent / "drainclamp-project.json").unlink()

# 7. the board: create and status endpoints
TOKEN = "life-token"
server = dc_board.make_server(home=H, port=0, token=TOKEN)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"


def call(path, body=None, token=TOKEN):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data is not None else "GET")
    if token:
        req.add_header("X-DC-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


code, body = call("/api/projects")
check("listing offers project_roots",
      code == 200 and [Path(r) for r in body["project_roots"]] == [roots.resolve()], body)

code, _ = call("/api/create", {"charter": {"name": "Beta", "purpose": "p"}}, token=None)
check("create without token is 403", code == 403 and not (roots / "beta").exists(), code)
code, body = call("/api/create", {"charter": {"name": "Beta", "purpose": "p"},
                                  "folder": "../beta"})
check("board refuses a traversal folder", code == 400 and not (tmp / "beta").exists(),
      (code, body))
code, body = call("/api/create", {"charter": {"name": "Beta", "purpose": "p"},
                                  "location": str(other)})
check("board refuses a location outside roots", code == 400, (code, body))
code, body = call("/api/create", {"charter": {"name": "Beta", "purpose": "Try it"}})
check("board creates a project", code == 200 and body.get("folder") == "beta" and
      (roots / "beta" / ".agent" / "charter.json").is_file(), (code, body))
bid = body.get("id")
code, body = call(f"/api/project?id={bid}")
check("new project detail carries the charter",
      code == 200 and body.get("charter", {}).get("purpose") == "Try it", body)
rev = body.get("data", {}).get("rev")

code, _ = call("/api/status", {"id": bid, "rev": rev, "status": "complete"}, token=None)
check("status without token is 403", code == 403, code)
code, body = call("/api/status", {"id": bid, "rev": rev, "status": "deleted"})
check("unknown status is 400", code == 400, (code, body))
code, body = call("/api/status", {"id": bid, "rev": rev + 3, "status": "complete"})
check("stale status is 409", code == 409, (code, body))
code, body = call("/api/status", {"id": bid, "rev": rev, "status": "complete",
                                  "note": "done here"})
check("board marks complete", code == 200 and body.get("rev") == rev + 1, (code, body))
code, body = call("/api/projects")
beta = next((p for p in body.get("projects", []) if p["id"] == bid), {})
check("listing shows it complete", beta.get("status") == "complete", beta)
check("Gate S menu agrees",
      "Beta" not in [e["name"] for e in dc_registry.listing("active", H)] and
      "Beta" in [e["name"] for e in dc_registry.listing("complete", H)])
code, body = call("/api/status", {"id": bid, "rev": rev + 1, "status": "active"})
check("board reopens", code == 200 and
      dc_registry.find(dc_registry.load(H), roots / "beta")["status"] == "active", (code, body))

server.shutdown()
server.server_close()

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
