"""Planning gate and mandatory fan-out rule: dc_plan.py through dc_project.py plan / chunk start."""
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
import dc_state  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcplan-"))
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


def plan(root, op, *extra):
    return proj(root, "plan", op, "--milestone", "m1", *extra)


def start(root, cid, *extra):
    return proj(root, "chunk", "start", "--milestone", "m1", "--id", cid, *extra)


def side(root):
    return json.loads((root / ".agent" / "drainclamp-project.json").read_text(encoding="utf-8"))


def fill(root, replace):
    path = root / ".agent" / "plans" / "m1.md"
    text = path.read_text(encoding="utf-8")
    for old, new in replace:
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    return path


# -- small sequential milestone: plan optional, start passes, no-size warning -------------------
small = make_repo("small")
rc, out = add(small, "one step", "--targets", "a.py", "--est", "20")
check("chunk add warns when a chunk has no size", "no --size" in out, out)
rc, out = plan(small, "need")
check("a small milestone needs no plan", rc == 0 and out.startswith("PLAN OPTIONAL m1"), out)
rc, out = start(small, "c1")
check("chunk start passes with no plan when none is required",
      rc == 0 and "step m1/c1 started" in out and "FAN-OUT m1: SEQUENTIAL" in out, out)
check("the fan-out verdict is recorded", side(small)["gates"]["m1"]["fanout"].startswith("SEQUENTIAL"),
      side(small).get("gates"))
rc, out = start(small, "c1")
check("the same verdict is not printed twice", "FAN-OUT" not in out, out)

# -- parallel milestone: plan required, refused until approved, fan-out enforced ----------------
par = make_repo("parallel")
add(par, "parser", "--targets", "a.py::parse", "--size", "medium", "--est", "25",
    "--parallel", "yes", "--why", "own file")
add(par, "writer", "--targets", "b.py", "--size", "medium", "--est", "25",
    "--parallel", "yes", "--why", "own file")
rc, out = plan(par, "need")
check("PARALLEL makes the plan required", out.startswith("PLAN REQUIRED m1: fan-out PARALLEL c1, c2")
      and "plan status: missing" in out, out)
rc, out = proj(par, "next")
check("the capsule says the plan is waiting", "Plan: required (fan-out PARALLEL c1, c2), missing" in out, out)
rc, out = start(par, "c1")
check("chunk start refused without a plan, nothing written",
      rc != 0 and "PLAN REQUIRED m1" in out and "start" not in side(par)["chunks"]["m1"][0], out)
rc, out = plan(par, "check")
check("check without a plan points at init", rc == 2 and "plan init" in out, out)
rc, out = plan(par, "init")
path = par / ".agent" / "plans" / "m1.md"
skel = path.read_text(encoding="utf-8")
check("init pre-fills chunks with their files",
      rc == 0 and "### c1 - parser" in skel and "Files: a.py" in skel and "Files: b.py" in skel, skel)
rc, out = plan(par, "init")
check("init never overwrites a plan", rc != 0 and "exists" in out, out)
rc, out = plan(par, "check")
check("the skeleton fails check on every TODO line", rc == 2 and "'TODO:' line(s) left" in out, out)
rc, out = plan(par, "approve")
check("an incomplete plan cannot be approved", rc != 0 and "gap(s)" in out, out)
fill(par, [
    ("TODO: how the milestone is built, in a few lines", "Parser and writer meet at a list of rows."),
    ("TODO: the contract between parallel chunks: signatures, file formats, flags, who owns which shared name",
     "none"),
    ("Test: TODO: test file and what it proves", "Test: tests/test_parse.py proves rows parse"),
    ("Test: TODO: test file and what it proves", ""),
    ("TODO: inputs and failure modes, and how each is handled", "Empty file: zero rows."),
    ("TODO: what this milestone does not do", "No CSV dialects."),
])
rc, out = plan(par, "check")
check("PARALLEL needs real Interfaces; a chunk needs a Test line",
      rc == 2 and "Interfaces must state the contract" in out and "c2 has no Test line" in out, out)
text = path.read_text(encoding="utf-8").replace("## Interfaces\nnone", "## Interfaces\nparse(text) -> list[Row]; writer reads Row")
text = text.replace("Files: b.py\n\n", "Files: b.py\nTest: tests/test_write.py proves rows write\n\n")
text = text.replace("Files: a.py", "Files: b.py")   # both chunks now own b.py; c1 loses its target
path.write_text(text, encoding="utf-8")
rc, out = plan(par, "check")
check("a chunk target missing from its Files, and a shared file, are gaps",
      "c1 target a.py is not in its Files" in out and "parallel chunks c1 and c2 both own b.py" in out, out)
path.write_text(text.replace("### c1 - parser\nFiles: b.py", "### c1 - parser\nFiles: a.py")
                .replace("## Edge cases", "### c9 - ghost\nFiles: z.py\nTest: none\n\n## Edge cases"),
                encoding="utf-8")
rc, out = plan(par, "check")
check("an entry for a chunk that does not exist is a gap", "### c9 is not a chunk of m1" in out, out)
path.write_text(text.replace("### c1 - parser\nFiles: b.py", "### c1 - parser\nFiles: a.py"), encoding="utf-8")
rc, out = plan(par, "check")
check("a complete plan passes check", rc == 0 and "complete" in out, out)
rc, out = plan(par, "approve")
check("approve records the hash", rc == 0 and "approved" in out
      and side(par)["plans"]["m1"]["status"] == "approved", out)
rc, out = start(par, "c1")
check("an approved plan lets the gate reach the fan-out rule, which refuses a sequential start",
      rc != 0 and "FAN-OUT m1: PARALLEL: c1, c2" in out and "--sequential" in out, out)
rc, out = proj(par, "brief", "--milestone", "m1", "--chunk", "c1")
check("the builder brief carries the plan and the chunk's test",
      "Plan: .agent/plans/m1.md" in out and "Test to write or pass: tests/test_parse.py proves rows parse" in out, out)
rc, out = start(par, "c1", "--sequential", "host has no subagents")
check("--sequential records the reason and starts",
      rc == 0 and "sequential by choice (host has no subagents)" in out
      and side(par)["gates"]["m1"]["sequential"]["reason"] == "host has no subagents", out)
rc, out = start(par, "c2")
check("once the milestone is sequential by choice, the next picked chunk starts", rc == 0, out)
path.write_text(path.read_text(encoding="utf-8") + "\nLate edit.\n", encoding="utf-8")
rc, out = start(par, "c2")
check("an approved plan edited later blocks the next start", rc != 0 and "PLAN CHANGED m1" in out, out)
rc, out = proj(par, "next")
check("the capsule names the stale plan", "edited after approval" in out, out)
rc, out = plan(par, "approve")
rc, out = start(par, "c2")
check("re-approval clears it", rc == 0, out)
import dc_review  # noqa: E402
block = dc_review._planned_block(par, "m1")
check("the Gate 6 packet lists the planned tests for the checker",
      block[:1] == ["## Planned tests (from the approved plan; a missing one is a checker finding)"]
      and "c1: tests/test_parse.py proves rows parse" in block and dc_review._planned_block(small, "m1") == [], block)

# -- large chunk or long estimate: required; waive needs a reason; a begun milestone is not stalled
big = make_repo("big")
add(big, "everything", "--targets", "a.py", "--size", "large", "--est", "25")
rc, out = plan(big, "need")
check("a large chunk makes the plan required", "PLAN REQUIRED m1: large chunk c1" in out, out)
rc, out = plan(big, "waive")
check("waive without a reason is refused", rc != 0 and "reason" in out, out)
rc, out = plan(big, "waive", "--reason", "user: one-file rename, no design")
check("waive with the user's reason lets the chunk start",
      "waived" in out and start(big, "c1")[0] == 0, out)

long = make_repo("long")
for n in range(4):
    add(long, f"step {n}", "--targets", f"f{n}.py", "--size", "small", "--est", "25")
add(long, "release", "--targets", "VERSION", "--size", "small", "--est", "25", "--critical", "release")
rc, out = plan(long, "need")
check("estimates of 90+ min (critical chunks not counted) make the plan required",
      "estimates total 100 min (>= 90)" in out, out)
data = dc_project.load(long / ".agent")
data["chunks"]["m1"][0]["done"] = True
(long / ".agent" / "drainclamp-project.json").write_text(json.dumps(data), encoding="utf-8")
rc, out = start(long, "c2")
check("a milestone already under way is not stalled by a missing plan", rc == 0, out)

rc, out = proj(long, "plan", "need", "--milestone", "m9")
check("plan on a milestone not in the roadmap is refused", rc != 0 and "not in DC:ROADMAP" in out, out)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
