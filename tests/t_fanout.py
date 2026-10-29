"""Conductor build checks for dc_project.py: chunk flags, fanout, brief, merge-order."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import dc_state  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcfanout-"))
home = tmp / "home"


def make_repo(name) -> Path:
    root = tmp / name
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    text = dc_state.template_text().replace(
        "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
        "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n"
        "| m1 | build | a.py | active |\n")
    (root / ".agent" / "drainclamp-state.md").write_text(text, encoding="utf-8")
    return root


def proj(root, *args):
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_project.py"),
         "--root", str(root), "--home", str(home), *args],
        capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def add(root, goal, *flags):
    return proj(root, "chunk", "add", "--milestone", "m1", "--goal", goal, *flags)


def fan(root, *extra):
    return proj(root, "fanout", "--milestone", "m1", *extra)


# independent medium chunks -> parallel
r1 = make_repo("Fan Out Demo")
add(r1, "contract: interfaces", "--targets", "api.py", "--size", "small")
add(r1, "parser", "--targets", "a.py::parse", "--size", "medium",
    "--parallel", "yes", "--why", "own file", "--deps", "c1")
add(r1, "writer", "--targets", "B.py", "--size", "large",
    "--parallel", "yes", "--why", "own file", "--deps", "c1")
rc, out = fan(r1)
check("chain head blocks fan-out", rc == 0 and out == "SEQUENTIAL: c2 needs c1", out)
proj(r1, "chunk", "done", "--milestone", "m1", "--id", "c1", "--note", "api: parse(text)->list")
rc, out = fan(r1)
lines = out.splitlines()
check("independent medium chunks go parallel",
      rc == 0 and lines[0] == "PARALLEL: c2, c3 (independent, medium+)" and len(lines) == 3, out)
check("branch and worktree naming",
      "c2 -> branch dc/fan-out-demo-c2  worktree " in lines[1]
      and lines[1].endswith(".worktrees/fan-out-demo-c2"), lines[1])

rc, out = fan(r1, "--no-subagents")
check("host without subagents", out == "SEQUENTIAL: host has no subagents", out)
rc, out = fan(r1, "--critical", "c3")
check("--critical forces sequential", out == "SEQUENTIAL: c3 is critical", out)
rc, out = fan(r1, "--critical", "c9")
check("--critical unknown chunk is an error", rc != 0 and "c9" in out, out)

# brief
rc, out = proj(r1, "brief", "--milestone", "m1", "--chunk", "c2", "--budget", "30000",
               "--verify", "python -m unittest")
bl = out.splitlines()
check("brief is short", rc == 0 and len(bl) <= 30, len(bl))
check("brief names files, contract, verify, budget, rules",
      "  a.py::parse" in bl and any("api: parse(text)->list" in l for l in bl)
      and "Verify: python -m unittest" in bl and "Token budget: 30000" in bl
      and any("never grind" in l for l in bl) and any("never push" in l for l in bl)
      and any(l.startswith("Report:") for l in bl), out)
rc, out = proj(r1, "brief", "--milestone", "m1", "--chunk", "c8")
check("brief for unknown chunk is an error", rc != 0, out)

# shared file (case-insensitive, symbol stripped) and small chunk
r2 = make_repo("shared")
add(r2, "one", "--targets", "src/A.py::f", "--size", "medium", "--parallel", "yes", "--why", "x")
add(r2, "two", "--targets", "SRC/a.py::g", "--size", "medium", "--parallel", "yes", "--why", "x")
rc, out = fan(r2)
check("shared file is sequential", out == "SEQUENTIAL: c1 and c2 share src/a.py", out)

r3 = make_repo("small")
add(r3, "one", "--targets", "a.py", "--size", "medium", "--parallel", "yes", "--why", "x")
add(r3, "two", "--targets", "b.py", "--size", "small", "--parallel", "yes", "--why", "x")
rc, out = fan(r3)
check("small chunk is sequential", out == "SEQUENTIAL: c2 is small", out)

r4 = make_repo("deploy")
add(r4, "one", "--targets", "a.py", "--size", "medium", "--parallel", "yes", "--why", "x")
add(r4, "ship", "--targets", "b.py", "--size", "large", "--parallel", "yes", "--why", "x",
    "--critical", "release")
rc, out = fan(r4)
check("critical chunk is sequential", out == "SEQUENTIAL: c2 is critical (release)", out)

# unflagged legacy chunks, parallel needs --why, unknown deps, cap of four
r5 = make_repo("legacy")
add(r5, "one", "--targets", "a.py")
add(r5, "two", "--targets", "b.py")
rc, out = fan(r5)
check("unflagged chunks stay sequential", rc == 0 and out.startswith("SEQUENTIAL: c1"), out)
rc, out = add(r5, "x", "--parallel", "yes")
check("--parallel requires --why", rc != 0 and "--why" in out, out)
rc, out = add(r5, "x", "--deps", "c7")
check("unknown dep is an error", rc != 0 and "c7" in out, out)

r6 = make_repo("many")
for n in range(6):
    add(r6, f"p{n}", "--targets", f"f{n}.py", "--size", "medium", "--parallel", "yes", "--why", "x")
rc, out = fan(r6)
check("at most four builders, the rest after the merge",
      out.splitlines()[0].startswith("PARALLEL: c1, c2, c3, c4 ") and len(out.splitlines()) == 6
      and out.splitlines()[-1] == "  then in order after the merge: c5, c6", out)

r8 = make_repo("mixed")
add(r8, "one", "--targets", "a.py", "--size", "medium", "--parallel", "yes", "--why", "x")
add(r8, "tiny", "--targets", "c.py", "--size", "small", "--parallel", "yes", "--why", "x")
add(r8, "two", "--targets", "b.py", "--size", "large", "--parallel", "yes", "--why", "x")
rc, out = fan(r8)
check("qualifying chunks fan out past a small one, which waits",
      out.splitlines()[0] == "PARALLEL: c1, c3 (independent, medium+)"
      and out.splitlines()[-1] == "  then in order after the merge: c2", out)

# merge order: dependency order, ties by id, cycle is an error
r7 = make_repo("order")
add(r7, "a", "--size", "medium")
add(r7, "b", "--deps", "c1")
add(r7, "c")
add(r7, "d", "--deps", "c2,c3")
rc, out = proj(r7, "merge-order", "--milestone", "m1")
order = [l.split()[0] for l in out.splitlines()]
check("merge order follows deps", rc == 0 and order == ["c1", "c2", "c3", "c4"], out)
check("merge order lists the branch", out.splitlines()[0] == "c1 dc/order-c1", out)
rc, out = proj(r7, "merge-order", "--milestone", "m1", "--chunks", "c4,c3")
check("merge order of a subset", [l.split()[0] for l in out.splitlines()] == ["c3", "c4"], out)
side = r7 / ".agent" / "drainclamp-project.json"
data = json.loads(side.read_text(encoding="utf-8"))
data["chunks"]["m1"][0]["deps"] = ["c2"]
side.write_text(json.dumps(data), encoding="utf-8")
rc, out = proj(r7, "merge-order", "--milestone", "m1", "--chunks", "c1,c2")
check("cycle is an error", rc != 0 and "cycle" in out, out)

print(f"\n{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
