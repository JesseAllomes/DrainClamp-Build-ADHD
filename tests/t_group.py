"""Group repos: several DrainClamp projects in sub-folders of one git repo."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
HOME = tempfile.mkdtemp(prefix="dcreg-")
os.environ["DRAINCLAMP_HOME"] = HOME  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import _dcio  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcgroup-")).resolve()


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def run(script, *args, cwd=None):
    proc = subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *args],
                          cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def make_group(name, projects=("a", "b")):
    """A git repo (`.agent/` ignored) with one committed source file per sub-folder project."""
    repo = tmp / name
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text(".agent/\n", encoding="utf-8")
    for p in projects:
        (repo / p / "src").mkdir(parents=True)
        (repo / p / "src" / f"{p}.py").write_text(f"def {p}_fn():\n    return 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def log(root, text, *extra, cwd=None):
    args = ["--append-log", text, *extra]
    if root is not None:
        args = ["--root", str(root), *args]
    return run("dc_state.py", *args, cwd=cwd)


# ---- G1: project root ------------------------------------------------------
grp = make_group("grp")
a, b = grp / "a", grp / "b"
rc, out = log(a, "first for a")
check("G1 state write in a/ ok", rc == 0, out.strip())
rc, out = log(b, "first for b")
check("G1 state write in b/ ok", rc == 0, out.strip())
sa, sb = a / ".agent" / "drainclamp-state.md", b / ".agent" / "drainclamp-state.md"
check("G1 two separate state files", sa.is_file() and sb.is_file())
check("G1 nothing written at the top level", not (grp / ".agent").exists())
check("G1 marker written below the top level",
      (a / ".drainclamp-project").is_file() and (b / ".drainclamp-project").is_file())
check("G1 state files do not share content",
      "first for a" in sa.read_text(encoding="utf-8") and "first for b" not in sa.read_text(encoding="utf-8"))

check("G1 project_root(--root a) is a", _dcio.project_root(a) == a)
check("G1 project_root from a/src is a", _dcio.project_root(a / "src") == a)
check("G1 repo_root stays the git top level", _dcio.repo_root(a) == grp)
prev = os.getcwd()
os.chdir(a / "src")
try:
    check("G1 no start, cwd a/src resolves to a", _dcio.project_root() == a)
finally:
    os.chdir(prev)
rc, out = log(None, "from inside src", cwd=a / "src")
check("G1 run from a/src with no --root writes a's state",
      rc == 0 and "from inside src" in sa.read_text(encoding="utf-8")
      and not (a / "src" / ".agent").exists(), out.strip())

# marker only: a project with no state yet is still found from inside it
grp2 = make_group("grp2", ("c",))
(grp2 / "c" / ".drainclamp-project").write_text("", encoding="utf-8")
check("G1 marker alone marks the project root", _dcio.project_root(grp2 / "c" / "src") == grp2 / "c")
bare = make_group("bare", ("d",))
check("G1 explicit sub-folder, no marker, is honoured", _dcio.project_root(bare / "d") == bare / "d")
os.chdir(bare / "d")
try:
    check("G1 no start, no marker, falls back to top level", _dcio.project_root() == bare)
finally:
    os.chdir(prev)

# single-project repo is unchanged: state at top, no marker, sub-folder resolves to top
single = tmp / "single"
single.mkdir()
git(single, "init", "-q")
(single / "src").mkdir()
rc, out = log(single, "solo")
check("G1 single project writes at the top level",
      rc == 0 and (single / ".agent" / "drainclamp-state.md").is_file())
check("G1 single project gets no marker", not (single / ".drainclamp-project").exists())
check("G1 single project: sub-folder root resolves to the top level",
      _dcio.project_root(single / "src") == single)

# outside git: unchanged
loose = tmp / "loose"
(loose / "x").mkdir(parents=True)
check("G1 outside git returns start unchanged", _dcio.project_root(loose / "x") == loose / "x")

# --expect-generation stays per project
rc, out = log(a, "stale write", "--expect-generation", "0")
check("G1 expect-generation guard refuses a stale write in a", rc != 0, out.strip()[:80])

# ---- G2: every git query scoped to the project ------------------------------
import json  # noqa: E402
import dc_audit  # noqa: E402
import dc_map  # noqa: E402
import dc_review  # noqa: E402
import dc_verify  # noqa: E402

g2 = make_group("g2")
a2, b2 = g2 / "a", g2 / "b"
(a2 / "src" / "extra.py").write_text("def extra():\n    return 2\n", encoding="utf-8")
git(g2, "add", "-A")
git(g2, "commit", "-q", "-m", "more")
rc, out = run("dc_audit.py", "--root", str(a2))
check("G2 audit of a runs", rc == 0, out.strip()[:120])
cache = json.loads((a2 / ".agent" / "audit.json").read_text(encoding="utf-8"))
check("G2 audit cache records the prefix", cache.get("prefix") == "a/", cache.get("prefix"))
check("G2 audit counts only a's files", cache["scan"]["files"] == 2, cache["scan"]["files"])
head_a = cache["head"]

# uncommitted work in b/ is invisible from a/
(b2 / "src" / "b.py").write_text("def b_fn():\n    return 99\n", encoding="utf-8")
(b2 / "src" / "new_b.py").write_text("x = 1\n", encoding="utf-8")
rc, out = run("dc_audit.py", "--root", str(a2))
check("G2 sibling edits leave a's audit cache fresh", "CACHE: fresh" in out, out.strip()[:120])
rc, out = run("dc_audit.py", "--root", str(b2))
check("G2 b's own audit does see its edits", rc == 0 and "AUDIT: refreshed" in out, out.strip()[:120])
check("G2 dirty fingerprint of a ignores b",
      dc_audit.dirty_fingerprint(a2)[1] == 0 and dc_audit.dirty_fingerprint(b2)[1] == 2)
check("G2 changed_files of a is empty", dc_verify.changed_files(a2, None) == ([], "working tree"),
      dc_verify.changed_files(a2, None))
changed_b, _how = dc_verify.changed_files(b2, None)
check("G2 changed_files of b is project-relative", changed_b == ["src/b.py", "src/new_b.py"], changed_b)
check("G2 review change set of a is empty", dc_review._changed(a2) == [])
inv = dc_review._inventory(a2)
check("G2 review inventory of a holds only a", "src/a.py" in inv and "src/b.py" not in inv, inv)
tree_a = dc_verify.tree_fingerprint(a2)
check("G2 tree fingerprint of a is stable", tree_a is not None and tree_a == dc_verify.tree_fingerprint(a2))
check("G2 tree fingerprints differ across projects", tree_a != dc_verify.tree_fingerprint(b2))

# a sibling commit moves repo HEAD but not a's own head
git(g2, "add", "-A")
git(g2, "commit", "-q", "-m", "b work")
check("G2 sibling commit does not move a's head", dc_audit.head_commit(a2) == head_a)
check("G2 sibling commit leaves a's tree fingerprint alone", dc_verify.tree_fingerprint(a2) == tree_a)
rc, out = run("dc_audit.py", "--root", str(a2))
check("G2 a's audit still fresh after the sibling commit", "CACHE: fresh" in out, out.strip()[:120])

# branch-relative change set is scoped and project-relative
base = git(g2, "rev-parse", "HEAD").stdout.strip()
(a2 / "src" / "a.py").write_text("def a_fn():\n    return 5\n", encoding="utf-8")
(b2 / "src" / "b.py").write_text("def b_fn():\n    return 6\n", encoding="utf-8")
git(g2, "add", "-A")
git(g2, "commit", "-q", "-m", "both")
check("G2 --base diff of a lists only a, project-relative",
      dc_verify.changed_files(a2, base + "~1")[0] == ["src/a.py"], dc_verify.changed_files(a2, base + "~1"))

# map lists only the project's files
records = dc_map.refresh(a2)
check("G2 map of a lists only a's files", set(records) == {"src/a.py", "src/extra.py"}, sorted(records))
check("G2 top-level scan is larger than a project's", dc_audit.scan(g2)["files"] > dc_audit.scan(a2)["files"])

# single-project repo: no prefix, same arguments as before
check("G2 top level has no prefix", _dcio.git_prefix(g2) == "" and _dcio.scope_args("") == [])
check("G2 prefix uses forward slashes", _dcio.git_prefix(a2 / "src") == "a/src/")

print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)
