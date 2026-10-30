"""G9 auto-commit at a completed step: chunk start snapshot, guards, PII scan, work branch,
clean-tree check, opt-out. Temp git repos only; reserved example data."""
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


tmp = Path(tempfile.mkdtemp(prefix="dcautocommit-"))
home = tmp / "home"
PY = sys.executable.replace("\\", "/")


def git(root, *args, check_rc=True):
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check_rc and proc.returncode != 0:
        raise RuntimeError(f"git {args}: {proc.stderr}")
    return (proc.stdout + proc.stderr).strip()


def write(root, rel, text, mode="w"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        with open(path, mode, encoding="utf-8", newline="") as handle:
            handle.write(text)


def make_repo(name, codeowners=False, extra=None):
    root = tmp / name
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    git(root, "config", "user.name", "Builder")
    git(root, "config", "user.email", "builder@example.com")
    git(root, "config", "core.autocrlf", "false")
    text = dc_state.template_text().replace(
        "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
        "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n"
        "| m1 | build | a.py | active |\n")
    (root / ".agent" / "drainclamp-state.md").write_text(text, encoding="utf-8")
    base = {"a.py": "x = 1\n", "b.py": "y = 1\n", "user.txt": "u\n", "pre.txt": "p\n",
            ".gitignore": ".agent/\n"}
    base.update(extra or {})
    if codeowners:
        base["CODEOWNERS"] = "* @example-owner\n"
    for rel, body in base.items():
        write(root, rel, body)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial")
    return root


def proj(root, *args):
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_project.py"),
         "--root", str(root), "--home", str(home), *args],
        capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def add_chunk(root, targets="a.py;new.py"):
    return proj(root, "chunk", "add", "--milestone", "m1", "--goal", "wire the parser",
                "--targets", targets)


def start(root):
    return proj(root, "chunk", "start", "--milestone", "m1", "--id", "c1")


def commit(root, *flags):
    return proj(root, "commit", "--milestone", "m1", "--chunk", "c1", *flags)


def head(root):
    return git(root, "rev-parse", "HEAD")


def head_files(root):
    return sorted(git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())


# -- (a) only the chunk's own paths, with sibling, user and pre-dirty edits around ----------
ra = make_repo("ac-a")
add_chunk(ra)
write(ra, "pre.txt", "p edited before the step\n")
rc, out = start(ra)
check("chunk start snapshots pre-dirty files", rc == 0 and "1 file(s) already edited" in out, out)
write(ra, "a.py", "x = 2\n")
write(ra, "new.py", "z = 1\n")
write(ra, "b.py", "y = 2\n")            # sibling edit, not a target
write(ra, "user.txt", "u edited\n")     # the user's own edit during the step
write(ra, "pre.txt", "p edited again\n")
rc, out = commit(ra, "--verified")
lines = out.splitlines()
check("(a) COMMITTED line first", rc == 0 and lines[0].startswith("COMMITTED: ")
      and lines[0].endswith("(2 file(s))"), out)
check("(a) subject names the step", "Complete m1/c1: wire the parser" in lines[0], out)
check("(a) only the chunk's paths are in the commit", head_files(ra) == ["a.py", "new.py"],
      head_files(ra))
status = git(ra, "status", "--porcelain")
check("(a) sibling, user and pre-dirty edits stay uncommitted",
      all(x in status for x in ("b.py", "user.txt", "pre.txt")), status)
check("(a) pre-dirty count reported",
      "NOT COMMITTED: 1 file(s) had edits before this step" in out, out)
check("(a) outside-target files are named as left", "LEFT: 2 changed file(s) outside" in out, out)
side = json.loads((ra / ".agent" / "drainclamp-project.json").read_text(encoding="utf-8"))
check("(a) commit hash is kept on the chunk", side["chunks"]["m1"][0].get("commit"), side)
check("(a) no trailer unless the caller passes one",
      "Co-Authored-By" not in git(ra, "log", "-1", "--format=%B"))

# --paths adds a file the chunk did not name; trailers pass through
write(ra, "a.py", "x = 3\n")
write(ra, "b.py", "y = 3\n")
rc, out = proj(ra, "commit", "--milestone", "m1", "--chunk", "c1", "--verified",
               "--paths", "b.py", "--trailer", "Co-Authored-By: Example <a@example.com>",
               "--subject", "Tidy the parser")
check("(a) --paths and --trailer", rc == 0 and out.startswith("COMMITTED:")
      and head_files(ra) == ["a.py", "b.py"], out)
check("(a) trailer lands in the message",
      "Co-Authored-By: Example" in git(ra, "log", "-1", "--format=%B"))

# no snapshot -> refuse rather than guess
rb0 = make_repo("ac-nosnap")
add_chunk(rb0)
write(rb0, "a.py", "x = 9\n")
before = head(rb0)
rc, out = commit(rb0, "--verified")
check("no snapshot -> no commit", out.startswith("NOT COMMITTED: no start snapshot")
      and head(rb0) == before, out)

# -- (b) another session's staged file never rides along ---------------------------------------
rb = make_repo("ac-b")
add_chunk(rb)
start(rb)
write(rb, "a.py", "x = 2\n")
write(rb, "other.py", "o = 1\n")
git(rb, "add", "other.py")       # staged by another session after the snapshot
rc, out = commit(rb, "--verified")
check("(b) commit made", out.startswith("COMMITTED:"), out)
check("(b) staged file of another session is not in the commit", head_files(rb) == ["a.py"],
      head_files(rb))
check("(b) it is still staged afterwards",
      "other.py" in git(rb, "diff", "--cached", "--name-only"))

# -- (c) refusals: verify, PII, merge, detached HEAD, size, binary --------------------------------
rc_ = make_repo("ac-c")
add_chunk(rc_)
start(rc_)
write(rc_, "a.py", "x = 2\n")
h0 = head(rc_)
rc, out = commit(rc_)
check("(c) no --verified -> no commit", out.startswith("NOT COMMITTED: the step's DC:VERIFY")
      and head(rc_) == h0, out)

secret_mail = "jo" + ".q" + "@" + "corp" + "-mail" + ".net"   # built at run time, not a literal
write(rc_, "a.py", f"contact = '{secret_mail}'\n")
rc, out = commit(rc_, "--verified")
check("(c) PII hit -> no commit, kinds and paths only",
      out.startswith("NOT COMMITTED: PII/secret scan hit") and "a.py" in out
      and secret_mail not in out and head(rc_) == h0, out)
write(rc_, "a.py", "x = 2\n")

(rc_ / ".git" / "MERGE_HEAD").write_text(h0 + "\n", encoding="ascii")
rc, out = commit(rc_, "--verified")
check("(c) merge in progress -> no commit", out.startswith("NOT COMMITTED: a merge is in progress")
      and head(rc_) == h0, out)
(rc_ / ".git" / "MERGE_HEAD").unlink()

git(rc_, "checkout", "-q", "--detach")
rc, out = commit(rc_, "--verified")
check("(c) detached HEAD -> no commit", out.startswith("NOT COMMITTED: HEAD is detached"), out)
git(rc_, "checkout", "-q", "main")

write(rc_, "new.py", "q = 1\n" * 1_000_000, "w")     # about 6 MB of text
rc, out = commit(rc_, "--verified")
check("(c) file over 5 MB -> needs the user's go",
      out.startswith("NOT COMMITTED:") and "over 5 MB" in out and head(rc_) == h0, out)
(rc_ / "new.py").unlink()
write(rc_, "new.py", b"\x00\x01binary\x00")
rc, out = commit(rc_, "--verified")
check("(c) new binary -> needs the user's go",
      out.startswith("NOT COMMITTED:") and "new binary" in out and head(rc_) == h0, out)
(rc_ / "new.py").unlink()
rc, out = commit(rc_, "--verified")
check("(c) clean again -> commits", out.startswith("COMMITTED:"), out)

# -- (d) review-required default branch gets a work branch -----------------------------------
rd = make_repo("ac-d", codeowners=True)
main_tip = head(rd)
add_chunk(rd)
start(rd)
write(rd, "a.py", "x = 2\n")
rc, out = commit(rd, "--verified")
check("(d) committed to a work branch", out.startswith("COMMITTED:")
      and git(rd, "branch", "--show-current") == "dc/ac-d-m1", out)
check("(d) PR needed is said", "PR needed" in out and "dc/ac-d-m1" in out, out)
check("(d) the default branch did not move", git(rd, "rev-parse", "main") == main_tip)

# a repo without CODEOWNERS and no flag commits in place; the flag alone makes it review-required
rd2 = make_repo("ac-d2")
add_chunk(rd2)
start(rd2)
write(rd2, "a.py", "x = 2\n")
rc, out = commit(rd2, "--verified")
check("(d) no CODEOWNERS -> commits on the current branch",
      out.startswith("COMMITTED:") and git(rd2, "branch", "--show-current") == "main"
      and "PR needed" not in out, out)
rd3 = make_repo("ac-d3")
proj(rd3, "config", "--review-required", "on")
add_chunk(rd3)
start(rd3)
write(rd3, "a.py", "x = 2\n")
rc, out = commit(rd3, "--verified")
check("(d) config flag -> work branch", "PR needed" in out
      and git(rd3, "branch", "--show-current") == "dc/ac-d3-m1", out)

# -- (e) clean-tree check catches a pass that depended on an uncommitted file ----------------------
re_ = make_repo("ac-e", extra={"a.py": "x = 1\n"})
write(re_, "dep.py", "VALUE = 1\n")          # untracked, so pre-dirty and never committed
add_chunk(re_)
start(re_)
write(re_, "a.py", "import dep\nx = dep.VALUE\n")
rc, out = proj(re_, "config", "--clean-tree", "on", "--verify-cmd",
               f'"{PY}" -c "import a"')
check("(e) config prints the settings", "clean-tree on" in out, out)
e0 = head(re_)
rc, out = commit(re_, "--verified")
check("(e) clean-tree catches the missing dependency",
      out.startswith("NOT COMMITTED: clean-tree check failed") and head(re_) == e0, out)
check("(e) temporary worktree is gone", len(git(re_, "worktree", "list").splitlines()) == 1,
      git(re_, "worktree", "list"))
proj(re_, "config", "--clean-tree", "off")
rc, out = commit(re_, "--verified")
check("(e) without the check the same step would have committed", out.startswith("COMMITTED:"), out)

rf2 = make_repo("ac-e2")
add_chunk(rf2)
start(rf2)
write(rf2, "a.py", "x = 2\n")
proj(rf2, "config", "--clean-tree", "on", "--verify-cmd", f'"{PY}" -c "import a"')
rc, out = commit(rf2, "--verified")
check("(e) a self-contained step passes the clean-tree check and commits",
      out.startswith("COMMITTED:"), out)

# -- (f) opt-out ---------------------------------------------------------------------------------
rg = make_repo("ac-f")
rc, out = proj(rg, "config")
check("(f) default is on", "autocommit on" in out, out)
add_chunk(rg)
start(rg)
write(rg, "a.py", "x = 2\n")
rc, out = proj(rg, "config", "--autocommit", "off")
check("(f) opt-out is stored", "autocommit off" in out, out)
g0 = head(rg)
rc, out = commit(rg, "--verified")
check("(f) off -> no commit", out.startswith("NOT COMMITTED: auto-commit is off")
      and head(rg) == g0, out)
proj(rg, "config", "--autocommit", "on")
rc, out = commit(rg, "--verified")
check("(f) back on -> commits", out.startswith("COMMITTED:"), out)

# -- a milestone with no chunks: snapshot on the milestone -----------------------------------------
rh = make_repo("ac-h")
rc, out = proj(rh, "chunk", "start", "--milestone", "m1")
write(rh, "a.py", "x = 5\n")
rc, out = proj(rh, "commit", "--milestone", "m1", "--verified")
check("milestone without chunks commits", out.startswith("COMMITTED:")
      and "Complete m1: build" in out, out)

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
