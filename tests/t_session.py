"""Registry and session-menu checks for dc_registry.py / dc_session.py."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import dc_registry  # noqa: E402
import dc_session  # noqa: E402
import dc_state  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcsession-"))
home = tmp / "home"


def make_repo(name, roadmap="") -> Path:
    """A repository with just enough state to be a registrable project."""
    root = tmp / name
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    text = dc_state.template_text()
    if roadmap:
        text = text.replace(
            "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
            f"<!-- DC:ROADMAP -->\n{roadmap}\n")
    (root / ".agent" / "drainclamp-state.md").write_text(text, encoding="utf-8")
    return root


def session(*args):
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_session.py"), "--home", str(home), *args],
        capture_output=True, text=True, encoding="utf-8")
    return proc.returncode, proc.stdout + proc.stderr


# 1. an empty registry is a menu, not a crash
rc, out = session()
check("empty registry lists no active projects", rc == 0 and "(no active projects)" in out)
check("empty registry still offers a new project", "(1) - a new project" in out)

# 2. registration, then listing
alpha = make_repo("alpha")
beta = make_repo("beta", "| m1 | goal | a.py | done |\n| m2 | goal | b.py | pending |")
dc_registry.touch(alpha, home)
dc_registry.touch(beta, home)
rows = dc_registry.listing("active", home)
check("both repositories registered", len(rows) == 2, f"got {len(rows)}")

rc, out = session()
check("menu names both projects", "alpha" in out and "beta" in out)
check("menu shows roadmap progress", "1/2 done" in out, out.strip().replace("\n", " | "))
check("menu offers new/complete", "- a new project" in out and "mark a project complete" in out)

# An entry can remain visible while its state becomes unreadable to a more
# restricted host (for example, a Windows sandbox ACL). Progress is optional;
# the session menu itself must remain available.
read_text = dc_session._dcio.read_text
try:
    def denied(_path):
        raise PermissionError(13, "permission denied")

    dc_session._dcio.read_text = denied
    check("unreadable state suppresses progress", dc_session.progress(str(alpha)) == "")
finally:
    dc_session._dcio.read_text = read_text

# 3. touch never resurrects a completed project
dc_registry.set_status(beta, "complete", home)
dc_registry.touch(beta, home)
entry = dc_registry.find(dc_registry.load(home), beta)
check("touch does not reopen a completed project", entry["status"] == "complete",
      entry["status"])

rc, out = session()
check("completed project leaves the active menu", "beta" not in out)
check("menu offers reopen when a completed project exists",
      "reopen a completed project" in out)

# 4. numbering resolves the same order the menu printed
rc, out = session("--list-complete")
check("completed listing is numbered", "(1) - beta" in out, out.strip())
rc, out = session("--reopen", "1")
check("reopen by menu number works", rc == 0 and "reopened" in out, out.strip())
check("reopened project is active again",
      dc_registry.find(dc_registry.load(home), beta)["status"] == "active")

# 5. an out-of-range number is refused, not silently clamped
rc, out = session("--complete", "99")
check("out-of-range selection fails loudly", rc != 0 and "no active project numbered 99" in out)

# 6. a repository whose state file has gone is pruned on read
gone = make_repo("gone")
dc_registry.touch(gone, home)
(gone / ".agent" / "drainclamp-state.md").unlink()
names = [e["name"] for e in dc_registry.listing("active", home)]
check("missing state file prunes the entry", "gone" not in names, names)
check("pruning is persisted", "gone" not in json.dumps(dc_registry.load(home)))

# 7. a corrupt registry is treated as empty rather than trusted
dc_registry.registry_path(home).write_text("{not json", encoding="utf-8")
check("corrupt registry reads as empty", dc_registry.load(home)["projects"] == [])

# 8. an unknown schema is not half-read
dc_registry.registry_path(home).write_text(
    json.dumps({"schema": 99, "projects": [{"root": "x", "status": "active"}]}),
    encoding="utf-8")
check("unknown schema reads as empty", dc_registry.load(home)["projects"] == [])

# 9. entries missing required fields are dropped, not defaulted
dc_registry.registry_path(home).write_text(
    json.dumps({"schema": 1, "projects": [
        {"root": str(alpha), "status": "active", "name": "alpha"},
        {"root": str(alpha), "status": "bogus"},
        {"status": "active"},
    ]}), encoding="utf-8")
check("malformed entries are dropped", len(dc_registry.load(home)["projects"]) == 1)

# 10. Gate 5 reports a finished roadmap as COMPLETE, not HOLD
done_repo = make_repo("finished",
                      "| m1 | goal | a.py | done |\n| m2 | goal | b.py | done |")
state = dc_state.load(done_repo / ".agent" / "drainclamp-state.md")
verdict, human = dc_state.purge_check(state, done_repo, context_high=False)
check("all-done roadmap yields COMPLETE", verdict.startswith("COMPLETE"), verdict)
check("COMPLETE counts the milestones", "2/2" in verdict, verdict)
check("COMPLETE does not claim a purge happened", "purge" not in human.lower(), human)

# 11. a part-done roadmap is still an overlap question
state = dc_state.load(beta / ".agent" / "drainclamp-state.md")
verdict, _ = dc_state.purge_check(state, beta, context_high=False)
check("part-done roadmap is not COMPLETE", not verdict.startswith("COMPLETE"), verdict)

# 12. an empty roadmap is not a completed one
state = dc_state.load(alpha / ".agent" / "drainclamp-state.md")
verdict, _ = dc_state.purge_check(state, alpha, context_high=False)
check("empty roadmap is not COMPLETE", not verdict.startswith("COMPLETE"), verdict)

# 13. a long list is never capped, and its action numbers clear every project.
bulk_home = tmp / "bulk-home"
for i in range(14):
    dc_registry.touch(make_repo(f"bulk{i:02d}"), bulk_home)


def bulk(*args):
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_session.py"),
         "--home", str(bulk_home), *args],
        capture_output=True, text=True, encoding="utf-8")
    return proc.returncode, proc.stdout + proc.stderr


rc, out = bulk()
check("long menu lists every project", out.count("(last worked") == 14, out.count("(last worked"))
check("an uncapped menu declares no SHOWING", "SHOWING" not in out)
check("action numbers clear the full count", "(15) - a new project" in out,
      [l for l in out.splitlines() if "new project" in l])
rc, out2 = bulk("--all")
check("--all is accepted and changes nothing", rc == 0 and out2 == out)

# 13b. each row names the project's full path, so same-named checkouts differ
bulk03_root = dc_registry.find(dc_registry.load(bulk_home), tmp / "bulk03")["root"]
check("menu row prints the full path", f"bulk03 — {bulk03_root} (" in out,
      [l for l in out.splitlines() if "bulk03" in l])
twin_home = tmp / "twin-home"
for side in ("left", "right"):
    twin = make_repo(f"{side}/same")
    dc_registry.touch(twin, twin_home)
rows = [dc_session.row(i, e) for i, e in enumerate(dc_registry.listing("active", twin_home), 1)]
check("same-named projects are told apart by path",
      len(rows) == 2 and rows[0].split(" (last")[0][4:] != rows[1].split(" (last")[0][4:], rows)

# 13c. Gate 6 findings that block a project are flagged in its row
flagged = make_repo("flagged", "| m1 | goal | a.py | active |")
sidecar = flagged / ".agent" / "drainclamp-project.json"
sidecar.write_text(json.dumps({"review": {"findings": [
    {"id": "r1", "status": "open"}, {"id": "r2", "status": "fixing"},
    {"id": "r3", "status": "fixed"}, {"id": "r4", "status": "waived"}]}}), encoding="utf-8")
entry = {"root": str(flagged), "name": "flagged", "last": "2026-10-08"}
line = dc_session.row(1, entry)
check("blocking findings are flagged with their count", line.endswith(", review: 2 blocking)"), line)
sidecar.write_text(json.dumps({"review": {"findings": [{"id": "r1", "status": "fixed"}]}}),
                   encoding="utf-8")
check("a settled review adds no flag", "review" not in dc_session.row(1, entry),
      dc_session.row(1, entry))
sidecar.write_text("{damaged", encoding="utf-8")
check("a damaged sidecar adds no flag and no crash", "review" not in dc_session.row(1, entry))
try:
    def denied(_path):
        raise PermissionError(13, "permission denied")

    dc_session._dcio.read_text = denied
    check("an unreadable sidecar adds no flag", dc_session.review(str(flagged)) == "")
finally:
    dc_session._dcio.read_text = read_text

# 14. forgetting drops the index entry and nothing else
victim = tmp / "bulk00"
rc, out = bulk("--forget", str(victim))
check("forget exits clean", rc == 0 and "dropped from the index" in out, out.strip())
check("forgotten project leaves the registry",
      dc_registry.find(dc_registry.load(bulk_home), victim) is None)
check("forget leaves the state file alone",
      (victim / ".agent" / "drainclamp-state.md").is_file())
check("a forgotten project can be re-registered",
      dc_registry.touch(victim, bulk_home)["status"] == "active")

# 15. a throwaway repository in the temp directory never reaches a lasting registry
fake_temp = tmp / "fake-temp"
fake_temp.mkdir()
lasting_home = tmp / "lasting-home"
saved_tempdir = tempfile.tempdir
try:
    tempfile.tempdir = str(fake_temp)
    throwaway = fake_temp / "dcstress-x"
    (throwaway / ".agent").mkdir(parents=True)
    (throwaway / ".agent" / "drainclamp-state.md").write_text(dc_state.template_text(),
                                                               encoding="utf-8")
    dc_registry.touch(throwaway, lasting_home)
    check("a temp-dir repo is not indexed by a lasting registry",
          dc_registry.find(dc_registry.load(lasting_home), throwaway) is None)
    kept = make_repo("lasting")
    dc_registry.touch(kept, lasting_home)
    check("a repo outside the temp dir is still indexed",
          dc_registry.find(dc_registry.load(lasting_home), kept) is not None)
    temp_home = fake_temp / "dcreg-test"
    dc_registry.touch(throwaway, temp_home)
    check("a temp-dir registry (a test's) still indexes a temp-dir repo",
          dc_registry.find(dc_registry.load(temp_home), throwaway) is not None)
    polluted = dc_registry.load(lasting_home)
    polluted["projects"].append({"root": str(throwaway).replace("\\", "/"), "name": "dcstress-x",
                                 "status": "active", "last": "2026-10-08T00:00:00+00:00"})
    dc_registry.save(polluted, lasting_home)
    names = [e["name"] for e in dc_registry.listing("active", lasting_home)]
    check("listing drops temp-dir entries already in a lasting registry", names == ["lasting"], names)
    check("and saves the cleaned registry",
          dc_registry.find(dc_registry.load(lasting_home), throwaway) is None)
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_session.py"), "--home", str(lasting_home),
         "--register", str(throwaway)],
        capture_output=True, text=True, encoding="utf-8", env={**os.environ, "TMP": str(fake_temp),
                                                                "TEMP": str(fake_temp),
                                                                "TMPDIR": str(fake_temp)})
    check("--register says it skipped a temp-dir repo",
          proc.returncode == 0 and "not registered" in proc.stdout, proc.stdout + proc.stderr)
finally:
    tempfile.tempdir = saved_tempdir

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
