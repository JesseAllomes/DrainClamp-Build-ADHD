"""Project lifecycle: create from a charter, complete, reopen -- functions and the CLI other tools call."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
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
check("sidecar holds build data only (no board fields seeded)", side.get("created") and
      not any(k in side for k in ("savings", "profile", "health", "runs")), sorted(side))
check("the charter keeps its value figures as the brief", charter["value"]["baseline_min"] == 60)
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

# 7. the command line other tools (project-board) call: create, complete, reopen
import re  # noqa: E402

CLI = [sys.executable, "-B", str(SCRIPTS / "dc_project.py")]


def cli(*args):
    return subprocess.run([*CLI, "--home", H, *args], capture_output=True, text=True)


cf = tmp / "beta.json"
cf.write_text(json.dumps({"name": "Beta", "purpose": "Try it"}), encoding="utf-8")
out = cli("create", "--charter", str(cf), "--folder", "../beta")
check("cli create refuses a traversal folder", out.returncode != 0 and not (tmp / "beta").exists(), out.stderr)
out = cli("create", "--charter", str(cf), "--location", str(other))
check("cli create refuses a location outside roots", out.returncode != 0, out.stderr)
out = cli("create", "--charter", str(cf))
m = re.search(r"created Beta at (.+?) \(git repo", out.stdout)
check("cli create prints the root the board parses", out.returncode == 0 and m and Path(m.group(1)) == (roots / "beta").resolve(),
      out.stdout + out.stderr)
check("cli create writes the charter", (roots / "beta" / ".agent" / "charter.json").is_file())
cf.write_text(json.dumps({"name": "Gamma", "purpose": "p", "kpis": ["k1"]}), encoding="utf-8")
out = cli("create", "--charter", str(cf))
check("unknown charter fields are refused (KPIs belong to the KPI board)", out.returncode != 0 and "unknown charter fields" in out.stderr, out.stderr)
out = subprocess.run([*CLI, "--home", H, "--root", str(roots / "beta"), "complete", "--note", "done here"], capture_output=True, text=True)
check("cli complete", out.returncode == 0 and dc_registry.find(dc_registry.load(H), roots / "beta")["status"] == "complete", out.stderr)
check("Gate S menu agrees", "Beta" in [e["name"] for e in dc_registry.listing("complete", H)])
out = subprocess.run([*CLI, "--home", H, "--root", str(roots / "beta"), "reopen"], capture_output=True, text=True)
check("cli reopen", out.returncode == 0 and dc_registry.find(dc_registry.load(H), roots / "beta")["status"] == "active", out.stderr)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
