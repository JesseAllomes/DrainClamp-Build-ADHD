"""Milestone dependencies: telling the calculus which pending row is next.

Without an ordering, several pending milestones are indistinguishable and the
purge calculus can only answer `multiple eligible next milestones` -- holding a
context it might have been able to release. The fifth column supplies the
ordering, and the same roadmap that held now purges.

The column is optional. The row parser already tolerated extra cells and
ignored them, so a four-column table means exactly what it always did and no
state file needs migrating.
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


def state(repo, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_state.py"), "--root", str(repo), *args],
        capture_output=True, text=True)


def roadmap(repo, text):
    src = repo / "_r.md"
    src.write_text(text, encoding="utf-8")
    return state(repo, "--set", "ROADMAP", "--file", str(src))


def verdict(repo) -> str:
    return state(repo, "--purge-check").stdout.splitlines()[0]


tmp = Path(tempfile.mkdtemp(prefix="dcdeps-"))
repo = tmp / "repo"
(repo / "a").mkdir(parents=True)
(repo / "b").mkdir()
(repo / "c").mkdir()
for name in ("a/a.py", "b/b.py", "c/c.py"):
    (repo / name).write_text("x = 1\n", encoding="utf-8")
subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
dec = repo / "_d.md"
dec.write_text("no open questions\n", encoding="utf-8")
state(repo, "--set", "DECISIONS", "--file", str(dec))

FOUR_COL = ("| m1 | one | a/a.py | done |\n"
            "| m2 | two | b/b.py | pending |\n"
            "| m3 | three | c/c.py | pending |\n")

# --------------------------------------------------------------------------
# Backward compatibility: the old shape still means the old thing
# --------------------------------------------------------------------------

r = roadmap(repo, FOUR_COL)
check("a four-column roadmap still parses", r.returncode == 0, r.stderr[:60])
check("without deps, two pending rows are indistinguishable",
      "multiple eligible next milestones" in verdict(repo), verdict(repo))

# --------------------------------------------------------------------------
# The same roadmap, ordered
# --------------------------------------------------------------------------

r = roadmap(repo, "| m1 | one | a/a.py | done | |\n"
                  "| m2 | two | b/b.py | pending | m1 |\n"
                  "| m3 | three | c/c.py | pending | m2 |\n")
check("a five-column roadmap parses", r.returncode == 0, r.stderr[:60])
check("deps resolve the ambiguity into a decision",
      verdict(repo).startswith("PURGE"), verdict(repo))

# An empty fifth cell is not a dependency on nothing -- it is no dependency.
r = roadmap(repo, "| m1 | one | a/a.py | done | |\n"
                  "| m2 | two | b/b.py | pending | |\n")
check("an empty deps cell is treated as no dependency", r.returncode == 0, r.stderr[:60])

# --------------------------------------------------------------------------
# A graph that cannot be satisfied is refused, never repaired
# --------------------------------------------------------------------------

r = roadmap(repo, "| m1 | one | a/a.py | pending | m2 |\n"
                  "| m2 | two | b/b.py | pending | m1 |\n")
check("a cycle is rejected", r.returncode != 0 and "cycle" in r.stderr, r.stderr[:70])
check("the cycle error names the milestones involved",
      "m1" in r.stderr and "m2" in r.stderr, r.stderr[:70])

r = roadmap(repo, "| m1 | one | a/a.py | pending | ghost |\n")
check("a dependency on a milestone that does not exist is rejected",
      r.returncode != 0 and "ghost" in r.stderr, r.stderr[:70])

r = roadmap(repo, "| m1 | one | a/a.py | pending | m1 |\n")
check("a self-dependency is rejected",
      r.returncode != 0 and "itself" in r.stderr, r.stderr[:70])

# A longer cycle is still a cycle.
r = roadmap(repo, "| m1 | one | a/a.py | pending | m3 |\n"
                  "| m2 | two | b/b.py | pending | m1 |\n"
                  "| m3 | three | c/c.py | pending | m2 |\n")
check("an indirect cycle is rejected", r.returncode != 0 and "cycle" in r.stderr,
      r.stderr[:70])

# --------------------------------------------------------------------------
# Concurrent agents: several milestones in flight is a real state, not a gap
# --------------------------------------------------------------------------

r = roadmap(repo, "| m1 | one | a/a.py | active | |\n"
                  "| m2 | two | b/b.py | active | |\n"
                  "| m3 | three | c/c.py | pending | m1 |\n")
check("deps do not override multiple active milestones",
      "HOLD" in verdict(repo) and "active" in verdict(repo), verdict(repo))

# --------------------------------------------------------------------------
# Blocked rows are excluded from the eligible set, not counted as candidates
# --------------------------------------------------------------------------

r = roadmap(repo, "| m1 | one | a/a.py | done | |\n"
                  "| m2 | two | b/b.py | pending | m1 |\n"
                  "| m3 | three | c/c.py | pending | m2 |\n"
                  "| m4 | four | c/c.py | pending | m3 |\n")
check("a chain leaves exactly one startable milestone",
      verdict(repo).startswith("PURGE"), verdict(repo))

print()
print(f"FAILURES: {fails if fails else 'none'}")
sys.exit(1 if fails else 0)
