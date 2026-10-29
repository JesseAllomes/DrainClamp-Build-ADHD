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
      "c2 -> branch dc/fan-out-demo-m1-c2  worktree " in lines[1]
      and lines[1].endswith(".worktrees/fan-out-demo-m1-c2"), lines[1])

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
check("merge order lists the branch", out.splitlines()[0] == "c1 dc/order-m1-c1", out)
rc, out = proj(r7, "merge-order", "--milestone", "m1", "--chunks", "c4,c3")
check("merge order of a subset", [l.split()[0] for l in out.splitlines()] == ["c3", "c4"], out)
side = r7 / ".agent" / "drainclamp-project.json"
data = json.loads(side.read_text(encoding="utf-8"))
data["chunks"]["m1"][0]["deps"] = ["c2"]
side.write_text(json.dumps(data), encoding="utf-8")
rc, out = proj(r7, "merge-order", "--milestone", "m1", "--chunks", "c1,c2")
check("cycle is an error", rc != 0 and "cycle" in out, out)

# r43: the default Verify line falls back to a pointer when DC:VERIFY has no usable milestone runner
EMPTY_VERIFY = "```json\n[]\n```"


def set_verify(root, payload):
    path = root / ".agent" / "drainclamp-state.md"
    path.write_text(path.read_text(encoding="utf-8").replace(
        EMPTY_VERIFY, "```json\n" + json.dumps(payload) + "\n```"), encoding="utf-8")


def verify_line(root):
    rc, out = proj(root, "brief", "--milestone", "m1", "--chunk", "c1")
    return rc, next((ln for ln in out.splitlines() if ln.startswith("Verify:")), out)


NO_RUNNER = "Verify: (no DC:VERIFY milestone runner: ask the conductor)"
v1 = make_repo("verify-none")
add(v1, "one", "--targets", "a.py")
set_verify(v1, [{"id": "quick", "argv": ["pytest", "-q"], "cwd": ".", "tier": "fast"}])
rc, line = verify_line(v1)
check("brief falls back when DC:VERIFY has no milestone entry", rc == 0 and line == NO_RUNNER, line)
v2 = make_repo("verify-bad")
add(v2, "one", "--targets", "a.py")
set_verify(v2, [{"id": "broken"}])
rc, line = verify_line(v2)
check("brief falls back when DC:VERIFY is malformed", rc == 0 and line == NO_RUNNER, line)
v3 = make_repo("verify-ok")
add(v3, "one", "--targets", "a.py")
set_verify(v3, [{"id": "unit", "argv": ["python", "-m", "pytest", "-q"], "cwd": ".", "tier": "milestone"}])
rc, line = verify_line(v3)
check("brief uses the milestone runner's argv", rc == 0 and line == "Verify: python -m pytest -q", line)

# r46: one open chunk is not a fan-out
r9 = make_repo("single-chunk")
add(r9, "only", "--targets", "a.py", "--size", "medium", "--parallel", "yes", "--why", "x")
rc, out = fan(r9)
check("one ready chunk is sequential", rc == 0 and out == "SEQUENTIAL: 1 ready chunk(s), need 2", out)

# r60: same-named project folders in one repo get distinct branches
multi = tmp / "multi"
multi.mkdir()
subprocess.run(["git", "init", "-q", str(multi)], check=True)
state_text = dc_state.template_text().replace(
    "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
    "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n| m1 | build | a.py | active |\n")
leaves = {}
for folder in ("a", "b"):
    leaf = multi / folder / "leave"
    (leaf / ".agent").mkdir(parents=True)
    (leaf / ".drainclamp-project").write_text("", encoding="utf-8")
    (leaf / ".agent" / "drainclamp-state.md").write_text(state_text, encoding="utf-8")
    add(leaf, "one", "--targets", "x.py")
    leaves[folder] = leaf
for folder, leaf in leaves.items():
    rc, out = proj(leaf, "brief", "--milestone", "m1", "--chunk", "c1", "--verify", "x")
    check(f"group project {folder}/leave names its own branch",
          rc == 0 and any(ln.startswith(f"Branch: dc/{folder}-leave-") and ln.endswith("-m1-c1")
                          for ln in out.splitlines()), out)

# r61: a folder target clashes with a file inside it; chunks with no targets never fan out
r10 = make_repo("folder-clash")
add(r10, "one", "--targets", "src/", "--size", "medium", "--parallel", "yes", "--why", "x")
add(r10, "two", "--targets", "src/a.py", "--size", "medium", "--parallel", "yes", "--why", "x")
rc, out = fan(r10)
check("a folder target and a file inside it share a path",
      rc == 0 and out.startswith("SEQUENTIAL: c1 and c2 share "), out)
r11 = make_repo("no-targets")
add(r11, "one", "--size", "medium", "--parallel", "yes", "--why", "x")
add(r11, "two", "--size", "medium", "--parallel", "yes", "--why", "x")
rc, out = fan(r11)
check("chunks with no targets are sequential", rc == 0 and out == "SEQUENTIAL: c1 has no targets", out)

# r74: whole-tree and ./-prefixed targets clash with files inside them
for n, (label, t1, t2) in enumerate((("a '.' target", ".", "src/a.py"),
                                     ("a './src' target", "./src", "src/a.py"),
                                     ("a './a.py' target", "./a.py", "a.py"))):
    rr = make_repo(f"dot-clash-{n}")
    add(rr, "one", "--targets", t1, "--size", "medium", "--parallel", "yes", "--why", "x")
    add(rr, "two", "--targets", t2, "--size", "medium", "--parallel", "yes", "--why", "x")
    rc, out = fan(rr)
    check(f"{label} clashes with {t2}", rc == 0 and out.startswith("SEQUENTIAL: c1 and c2 share "), out)

# r77: projects at x/y and x-y in one repo get distinct branches
branches = {}
for folder in ("x/y", "x-y"):
    leaf = multi / "xy" / folder
    (leaf / ".agent").mkdir(parents=True)
    (leaf / ".drainclamp-project").write_text("", encoding="utf-8")
    (leaf / ".agent" / "drainclamp-state.md").write_text(state_text, encoding="utf-8")
    add(leaf, "one", "--targets", "x.py")
    rc, out = proj(leaf, "brief", "--milestone", "m1", "--chunk", "c1", "--verify", "x")
    branches[folder] = next((ln for ln in out.splitlines() if ln.startswith("Branch: ")), out)
check("x/y and x-y get different builder branches",
      all(b.startswith("Branch: ") for b in branches.values())
      and len(set(branches.values())) == 2, branches)

print(f"\n{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
