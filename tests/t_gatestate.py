"""Gate-state precondition: verification refuses to run against no plan.

The scripts already refuse to let an absence read as a success. An entry that
never executed is NO-CHECKS-RUN; a runner that is not installed is
MISSING-REQUIRED. The gates had no equivalent, so a session could skip Gate 2
entirely, claim decisions were recorded, and every check would still run and
still go green against a roadmap that did not exist.

These checks pin the refusal: no state means nothing executes, the exit code
is the existing NO-CHECKS code rather than a new one, discovery still works
before Gate 2 has run, and the escape hatch is explicit.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
if not SCRIPTS.is_dir():
    SCRIPTS = ROOT / "skills" / "drainclamp-build" / "scripts"

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def dc(script, cwd, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        cwd=str(cwd), capture_output=True, text=True)


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


tmp = Path(tempfile.mkdtemp(prefix="dcgate-"))
repo = tmp / "repo"
repo.mkdir()
git(repo, "init")
(repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
git(repo, "add", "-A")
git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")

# --------------------------------------------------------------------------
# No state: nothing runs
# --------------------------------------------------------------------------

r = dc("dc_verify.py", repo, "--tier", "milestone")
check("tier refuses when Gate 2 has not run", "NO-STATE" in r.stdout, r.stdout[:80])
check("refusal is never green", r.returncode != 0, f"exit {r.returncode}")
check("refusal reuses the NO-CHECKS exit code, not a new one",
      r.returncode == 6, f"exit {r.returncode}")
check("refusal names the file it expected",
      "drainclamp-state.md" in r.stdout)
check("refusal says nothing was executed",
      "Nothing was executed" in r.stdout)
check("refusal points at the gate, not just the file",
      "Gate 2" in r.stdout)

# The verdict line must not claim a pass by omission.
check("no PASS anywhere in a stateless run", "PASS" not in r.stdout)

# --------------------------------------------------------------------------
# Discovery runs before Gate 2 by design, so it must not be gated
# --------------------------------------------------------------------------

r = dc("dc_verify.py", repo, "--discover")
check("--discover still works with no state", r.returncode == 0, r.stderr[:60])
check("--discover returns an array", r.stdout.strip().startswith("["), r.stdout[:40])

# --------------------------------------------------------------------------
# Escape hatch is explicit, never implied
# --------------------------------------------------------------------------

r = dc("dc_verify.py", repo, "--tier", "milestone", "--allow-no-state")
# Not a string-absence test: the bypass NOTE mentions NO-STATE by design. The
# refusal block is what must be gone.
check("--allow-no-state bypasses the refusal",
      "Gate 2 did not run" not in r.stdout, r.stdout[:80])

# --------------------------------------------------------------------------
# Presence is not the question: a template parses cleanly and plans nothing
# --------------------------------------------------------------------------

agent = repo / ".agent"
agent.mkdir(exist_ok=True)
state = agent / "drainclamp-state.md"
template = SCRIPTS.parent / "assets" / "state.template.md"
check("state template ships with the skill", template.is_file(), str(template))

state.write_text("", encoding="utf-8")
r = dc("dc_verify.py", repo, "--tier", "milestone")
check("an empty file at the expected path is not state", r.returncode != 0,
      f"exit {r.returncode}")

state.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
r = dc("dc_verify.py", repo, "--tier", "milestone")
check("a template with no roadmap rows is refused",
      "NO-STATE" in r.stdout and r.returncode == 6, f"exit {r.returncode}")
check("the refusal names the roadmap as the gap",
      "ROADMAP" in r.stdout, r.stdout[:100])

# A roadmap alone is still not Gate 2: decisions are what survive a purge.
def write_sections(roadmap: str, decisions: str) -> None:
    body = template.read_text(encoding="utf-8")
    body = body.replace("<!-- DC:ROADMAP -->\n", f"<!-- DC:ROADMAP -->\n{roadmap}\n")
    body = body.replace("<!-- DC:DECISIONS -->\n", f"<!-- DC:DECISIONS -->\n{decisions}\n")
    state.write_text(body, encoding="utf-8")


write_sections("| m1 | do the thing | a.py | pending |", "")
r = dc("dc_verify.py", repo, "--tier", "milestone")
check("a roadmap with no decisions is still refused",
      "NO-STATE" in r.stdout and "DECISIONS" in r.stdout, r.stdout[:100])

write_sections("| m1 | do the thing | a.py | pending |", "no open questions")
r = dc("dc_verify.py", repo, "--tier", "milestone")
check("a real plan passes the precondition", "NO-STATE" not in r.stdout, r.stdout[:100])

# --------------------------------------------------------------------------
# The bypass is never silent
# --------------------------------------------------------------------------

state.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
r = dc("dc_verify.py", repo, "--tier", "milestone", "--allow-no-state")
check("--allow-no-state announces itself in the report",
      "allow-no-state" in r.stdout and "NOTE" in r.stdout, r.stdout[:120])
log = (agent / "verify.log")
check("the bypass reaches the log too",
      log.is_file() and "allow-no-state" in log.read_text(encoding="utf-8"))

print()
print(f"FAILURES: {fails if fails else 'none'}")
sys.exit(1 if fails else 0)
