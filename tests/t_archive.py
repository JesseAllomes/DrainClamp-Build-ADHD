"""DC:LOG rollover and --promote-to-agents checks for dc_state.py."""
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import _dcio  # noqa: E402
import dc_state  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def run(root, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_state.py"), "--root", str(root), *args],
        capture_output=True, text=True,
    )


def newrepo():
    d = Path(tempfile.mkdtemp(prefix="dcarc-")) / "repo"
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=d, capture_output=True)
    return d


def active(root):
    text = (root / ".agent/drainclamp-state.md").read_text(encoding="utf-8")
    body = text.split("<!-- DC:LOG -->")[1].split("<!-- /DC:LOG -->")[0]
    return [ln for ln in body.splitlines() if ln.strip()]


def archived(root):
    p = root / ".agent" / dc_state.ARCHIVE_NAME
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines()
            if dc_state.LOG_ENTRY_RE.match(ln.strip())]


# --- cap and rollover -----------------------------------------------------
repo = newrepo()
for i in range(20):
    run(repo, "--append-log", f"entry {i}")
check("no archive before the cap is exceeded", not archived(repo) and len(active(repo)) == 20)

r = run(repo, "--append-log", "entry 20")
check("21st entry triggers archiving", "1 archived" in r.stdout, r.stdout.strip())
check("active log stays at the cap", len(active(repo)) == 20, str(len(active(repo))))
check("oldest entry moved to the archive",
      len(archived(repo)) == 1 and "entry 0" in archived(repo)[0], str(archived(repo)))

for i in range(21, 40):
    run(repo, "--append-log", f"entry {i}")
check("active log never exceeds the cap", len(active(repo)) == 20, str(len(active(repo))))
check("archive holds every overflowed entry", len(archived(repo)) == 20,
      str(len(archived(repo))))

all_ids = [dc_state.entry_id(l) for l in active(repo) + archived(repo)]
check("no entry lost across the rollover", len(all_ids) == 40)
check("no duplicate ids anywhere", len(set(all_ids)) == 40,
      f"{len(set(all_ids))} unique of {len(all_ids)}")
texts = [dc_state.LOG_ENTRY_RE.match(l.strip()).group(3) for l in archived(repo) + active(repo)]
check("all 40 sentences survive in order",
      texts == [f"entry {i}" for i in range(40)], str(texts[:3]))

# --- crash recovery -------------------------------------------------------
# Simulate a crash *between* the archive write and the state replacement: the
# entries are durable in the archive but still present in the active log.
repo2 = newrepo()
for i in range(20):
    run(repo2, "--append-log", f"c{i}")
agent = repo2 / ".agent"
stranded = active(repo2)[:3]
dc_state.archive_append(agent, stranded)
check("archive write is durable on its own", len(archived(repo2)) == 3)
check("active log still holds the duplicates (crash state)",
      len(active(repo2)) == 20)

r = run(repo2, "--append-log", "after crash")
check("next mutation reconciles the duplicates",
      len(active(repo2)) == 18, f"{len(active(repo2))} active")
ids2 = [dc_state.entry_id(l) for l in active(repo2)]
check("reconciled entries are exactly the archived ones",
      not (set(ids2) & set(dc_state.archived_ids(agent))))
check("nothing lost by reconciliation",
      len(set(ids2) | dc_state.archived_ids(agent)) == 21,
      str(len(set(ids2) | dc_state.archived_ids(agent))))

# --- idempotent archive ---------------------------------------------------
before = len(archived(repo2))
written = dc_state.archive_append(agent, stranded)
check("re-archiving the same entries writes nothing", written == 0)
check("archive length unchanged", len(archived(repo2)) == before)

# --- archive is not part of resume ---------------------------------------
r = run(repo2, "--show")
check("--show does not read the archive", "archive" not in r.stdout.lower())
check("archive header says it is never auto-loaded",
      "never auto-loaded" in (agent / dc_state.ARCHIVE_NAME).read_text(encoding="utf-8").lower())

# --- promote-to-agents ----------------------------------------------------
repo3 = newrepo()
dec = repo3 / "dec.md"
dec.write_text(
    "- API must stay backward compatible -> yes (user)\n"
    "- destructive migration allowed -> no (user)\n"
    "- naming follows existing snake_case -> (repo convention)\n",
    encoding="utf-8")
run(repo3, "--set", "DECISIONS", "--file", str(dec))
rm = repo3 / "rm.md"
rm.write_text("| id | goal | files | status |\n|---|---|---|---|\n"
              "| m1 | x | a.py | active |\n", encoding="utf-8")
run(repo3, "--set", "ROADMAP", "--file", str(rm))
run(repo3, "--append-log", "did a thing")

r = run(repo3, "--promote-to-agents")
check("bare promote lists choices and writes nothing",
      "Nothing was written" in r.stdout and not (repo3 / "AGENTS.md").exists())
check("listing is numbered for selection", "1." in r.stdout and "3." in r.stdout)
check("listing states roadmap/verify/log are never promoted",
      "never promoted" in r.stdout)

r = run(repo3, "--promote-to-agents", "--select", "1,2")
check("selection without --confirm still writes nothing",
      not (repo3 / "AGENTS.md").exists() and "Nothing was written" in r.stdout)
check("proposed block is shown before writing",
      "backward compatible" in r.stdout and dc_state.PROMOTE_MARKER in r.stdout)

r = run(repo3, "--promote-to-agents", "--select", "1,2", "--confirm")
agents = (repo3 / "AGENTS.md").read_text(encoding="utf-8")
check("--confirm writes AGENTS.md", (repo3 / "AGENTS.md").exists())
check("only selected constraints written",
      "backward compatible" in agents and "snake_case" not in agents)
check("roadmap never promoted", "m1" not in agents and "| goal |" not in agents)
check("log never promoted", "did a thing" not in agents)
check("verify never promoted", "argv" not in agents)

# existing content preserved; re-promote replaces only our section
(repo3 / "AGENTS.md").write_text(
    "# House rules\n\nHand-written guidance that must survive.\n\n" + agents,
    encoding="utf-8")
r = run(repo3, "--promote-to-agents", "--select", "3", "--confirm")
agents2 = (repo3 / "AGENTS.md").read_text(encoding="utf-8")
check("hand-written content preserved", "must survive" in agents2)
check("promoted section replaced, not duplicated",
      agents2.count(f"<!-- {dc_state.PROMOTE_MARKER} -->") == 1)
check("new selection replaced the old", "snake_case" in agents2
      and "backward compatible" not in agents2)

# ambiguous destination
repo4 = newrepo()
run(repo4, "--set", "DECISIONS", "--file", str(dec))
(repo4 / "AGENTS.md").write_text("root\n", encoding="utf-8")
(repo4 / "sub").mkdir()
(repo4 / "sub/AGENTS.md").write_text("sub\n", encoding="utf-8")
r = run(repo4, "--promote-to-agents", "--select", "1", "--confirm")
check("several instruction files -> refuses and names them",
      r.returncode != 0 and "several instruction files" in r.stderr)
check("nothing written on the ambiguous path",
      (repo4 / "AGENTS.md").read_text(encoding="utf-8") == "root\n")
r = run(repo4, "--promote-to-agents", "--select", "1", "--dest", "sub/AGENTS.md", "--confirm")
check("--dest resolves the ambiguity",
      "backward compatible" in (repo4 / "sub/AGENTS.md").read_text(encoding="utf-8"))
check("the other instruction file untouched",
      (repo4 / "AGENTS.md").read_text(encoding="utf-8") == "root\n")

# CLAUDE.md is never created by any of this
check("CLAUDE.md never created",
      not (repo3 / "CLAUDE.md").exists() and not (repo4 / "CLAUDE.md").exists())

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
