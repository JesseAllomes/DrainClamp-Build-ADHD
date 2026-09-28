"""Stale writers: an agent that left and came back must not erase what landed.

The lock serialises writers, so two processes never corrupt the file. It does
not preserve intent. An operator who instructs one agent, switches to another,
and returns leaves a window minutes long in which the first agent's picture of
the plan is stale -- and the losing write is silent. Both agents are told they
succeeded and one plan simply ceases to exist.

--expect-generation closes that window by comparing against the generation the
caller *reasoned from* rather than the one on disk at write time. These checks
pin the refusal, the survival of the committed work, the retry path, and the
unchanged default.
"""
import os
import re
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


def generation(repo) -> int:
    out = state(repo, "--show").stdout
    m = re.search(r"generation=(\d+)", out)
    return int(m.group(1)) if m else -1


def roadmap_rows(repo) -> list[str]:
    body = (repo / ".agent" / "drainclamp-state.md").read_text(encoding="utf-8")
    section = body.split("DC:ROADMAP")[1]
    return [ln for ln in section.splitlines() if ln.startswith("|")]


def write_roadmap(repo, text, *extra):
    src = repo / "_roadmap.md"
    src.write_text(text, encoding="utf-8")
    return state(repo, "--set", "ROADMAP", "--file", str(src), *extra)


tmp = Path(tempfile.mkdtemp(prefix="dcstale-"))
repo = tmp / "repo"
repo.mkdir()
subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)

# T1: agent A reads the plan and begins work
write_roadmap(repo, "| a | component A | a.py | active |\n")
dec = repo / "_dec.md"
dec.write_text("no open questions\n", encoding="utf-8")
state(repo, "--set", "DECISIONS", "--file", str(dec))
gen_a_read = generation(repo)
check("agent A observes a generation", gen_a_read > 0, gen_a_read)

# T2: the operator switches, and agent B commits its own work
r = write_roadmap(repo, "| a | component A | a.py | active |\n"
                        "| b | component B | b.py | done |\n")
check("agent B commits while A is away", r.returncode == 0, r.stderr[:60])
check("the generation advanced", generation(repo) == gen_a_read + 1)

# T3: agent A returns and writes what it knew at T1
r = write_roadmap(repo, "| a | component A | a.py | done |\n",
                  "--expect-generation", str(gen_a_read))
check("a stale write is refused", r.returncode != 0, f"exit {r.returncode}")
check("the refusal reuses EXIT_CHECK_FAILED", r.returncode == 2, f"exit {r.returncode}")
check("the refusal names both generations",
      str(gen_a_read) in r.stderr and "mismatch" in r.stderr.lower(), r.stderr[:80])

rows = roadmap_rows(repo)
check("agent B's milestone survived the refusal",
      any("component B" in ln for ln in rows), str(rows))
check("agent B's row was not silently reverted",
      any("component A" in ln and "active" in ln for ln in rows), str(rows))

# A re-reads and re-applies on top of what it found
gen_now = generation(repo)
r = write_roadmap(repo, "| a | component A | a.py | done |\n"
                        "| b | component B | b.py | done |\n",
                  "--expect-generation", str(gen_now))
check("the retry succeeds once A has re-read", r.returncode == 0, r.stderr[:60])
rows = roadmap_rows(repo)
check("both agents' work is present after the retry",
      any("component A" in ln and "done" in ln for ln in rows)
      and any("component B" in ln for ln in rows), str(rows))

# The flag is opt-in: without it, last write still wins
r = write_roadmap(repo, "| z | replaced wholesale | z.py | pending |\n")
check("without the flag the default is unchanged", r.returncode == 0, r.stderr[:60])
check("last write still wins when no expectation is given",
      any("replaced wholesale" in ln for ln in roadmap_rows(repo)))

# An expectation that matches is a no-op
gen_now = generation(repo)
r = write_roadmap(repo, "| z | replaced wholesale | z.py | done |\n",
                  "--expect-generation", str(gen_now))
check("a current expectation does not obstruct", r.returncode == 0, r.stderr[:60])

# The log path takes the same guard
r = state(repo, "--append-log", "stale note", "--expect-generation", str(gen_now))
check("--append-log honours the expectation too", r.returncode == 2, f"exit {r.returncode}")

print()
print(f"FAILURES: {fails if fails else 'none'}")
sys.exit(1 if fails else 0)
