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

def refuse_hook(root):
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho blocked by hook >&2\nexit 1\n", encoding="ascii", newline="\n")
    os.chmod(hook, 0o755)


# -- r93: a refused commit on a review-required repo returns to the start branch
r93 = make_repo("ac-r93", codeowners=True)
add_chunk(r93)
start(r93)
write(r93, "a.py", "x = 2\n")
refuse_hook(r93)
rc, out = commit(r93, "--verified")
check("(r93) hook refusal -> back on main, no work branch left",
      out.startswith("NOT COMMITTED: git commit refused")
      and git(r93, "branch", "--show-current") == "main"
      and git(r93, "branch", "--list", "dc/ac-r93-m1") == "", out)

# -- r92: an edit after the scan is never committed unscanned
import _dcio  # noqa: E402
r92 = make_repo("ac-r92")
write(r92, "a.py", "x = 2\n")
real_clean = _dcio._clean_tree


def racing_clean(top, prefix, commit, argv, scratch, sub="."):
    write(r92, "a.py", "x = 3  # edited after the scan\n")
    return None


_dcio._clean_tree = racing_clean
try:
    out92 = _dcio.auto_commit(r92, ["a.py"], "race", clean_argv=[PY, "-c", "pass"], home=str(home))
finally:
    _dcio._clean_tree = real_clean
check("(r92) an edit after the scan blocks the commit",
      out92[0].startswith("NOT COMMITTED: the step's files changed after the PII scan")
      and git(r92, "rev-list", "--count", "HEAD") == "1", out92)

# -- r108: an existing work branch at another commit is refused, main untouched
r108 = make_repo("ac-r108", codeowners=True)
git(r108, "checkout", "-q", "-b", "dc/ac-r108-m1")
write(r108, "b.py", "y = 5\n")
git(r108, "commit", "-q", "-am", "older branch work")
git(r108, "checkout", "-q", "main")
add_chunk(r108)
start(r108)
write(r108, "a.py", "x = 2\n")
m108 = head(r108)
rc, out = commit(r108, "--verified")
check("(r108) work branch at another commit -> no commit",
      out.startswith("NOT COMMITTED: work branch dc/ac-r108-m1 already exists")
      and head(r108) == m108 and git(r108, "branch", "--show-current") == "main", out)

# -- r105: a Windows backslash path in --verify-cmd survives
if os.name == "nt":
    r105 = make_repo("ac-r105", extra={"tools/chk.py": "import os, sys\nsys.exit(0 if os.path.isfile('a.py') else 1)\n"})
    add_chunk(r105)
    start(r105)
    write(r105, "a.py", "x = 2\n")
    proj(r105, "config", "--clean-tree", "on", "--verify-cmd", f'"{PY}" tools\\chk.py')
    rc, out = commit(r105, "--verified")
    check("(r105) backslash path in --verify-cmd runs as written", out.startswith("COMMITTED:"), out)
else:
    check("(r105) backslash path check is Windows-only (skipped)", True)

EMPTY_VERIFY = "```json\n[]\n```"


def set_verify(root, payload):
    path = root / ".agent" / "drainclamp-state.md"
    path.write_text(path.read_text(encoding="utf-8").replace(
        EMPTY_VERIFY, "```json\n" + json.dumps(payload) + "\n```"), encoding="utf-8")


# -- r110: the fast entry's cwd is kept and the clean-tree check runs there
import _dcio  # noqa: E402
import dc_project  # noqa: E402
r110 = make_repo("ac-r110", extra={"web/marker": "m\n"})
set_verify(r110, [{"id": "q", "argv": [PY, "-c", "print(1)"], "cwd": "web", "tier": "fast"}])
got110 = dc_project._fast_verify_argv(r110)
check("(r110) fast entry keeps its cwd", got110 == ([PY, "-c", "print(1)"], "web"), got110)
probe = [sys.executable, "-c", "exit(0 if __import__('os').path.isfile('marker') else 1)"]
why_web = _dcio._clean_tree(r110, "", head(r110), probe, Path(tempfile.mkdtemp(prefix="dcr110-")), "web")
why_top = _dcio._clean_tree(r110, "", head(r110), probe, Path(tempfile.mkdtemp(prefix="dcr110-")), ".")
check("(r110) clean-tree runs in the entry's cwd", why_web is None, why_web)
check("(r110) and not at the root", (why_top or "").startswith("clean-tree check failed"), why_top)

# -- r103: dot-leading targets keep their dot
r103 = make_repo("ac-r103", extra={".github/workflows/ci.yml": "on: push\n"})
add_chunk(r103, ".github/workflows/ci.yml;./a.py")
start(r103)
write(r103, ".github/workflows/ci.yml", "on: [push]\n")
write(r103, "a.py", "x = 2\n")
rc, out = commit(r103, "--verified")
check("(r103) dot-leading and ./ targets are committed",
      out.startswith("COMMITTED:") and "LEFT" not in out
      and head_files(r103) == [".github/workflows/ci.yml", "a.py"], out)

# -- r109: a staged rename commits both names
r109 = make_repo("ac-r109", extra={"src/old.py": "o = 1\n"})
add_chunk(r109, "src")
start(r109)
git(r109, "mv", "src/old.py", "src/new.py")
rc, out = commit(r109, "--verified")
check("(r109) rename commits the deletion too",
      out.startswith("COMMITTED:") and head_files(r109) == ["src/new.py", "src/old.py"]
      and git(r109, "status", "--porcelain") == "", out)

# -- r95: clean-tree on with no command
r95 = make_repo("ac-r95")
proj(r95, "config", "--clean-tree", "on")
add_chunk(r95)
start(r95)
write(r95, "a.py", "x = 2\n")
h95 = head(r95)
rc, out = commit(r95, "--verified")
check("(r95) clean-tree on with no command -> no commit",
      out.startswith("NOT COMMITTED: clean-tree check is on but there is no command")
      and head(r95) == h95, out)

# -- r91: a sidecar-only clean-tree command never runs; a non-allowlisted fast entry neither
r91 = make_repo("ac-r91")
add_chunk(r91)
start(r91)
write(r91, "a.py", "x = 2\n")
sp = r91 / ".agent" / "drainclamp-project.json"
sd = json.loads(sp.read_text(encoding="utf-8"))
sd["autocommit"] = {"enabled": True, "clean_tree": True, "review_required": False,
                    "verify_cmd": f'"{PY}" -c "import a"'}
sp.write_text(json.dumps(sd), encoding="utf-8")
rc, out = commit(r91, "--verified")
check("(r91) sidecar-only command refused", out.startswith("NOT COMMITTED: the clean-tree command was not set"), out)
proj(r91, "config", "--verify-cmd", f'"{PY}" -c "import a"')
rc, out = commit(r91, "--verified")
check("(r91) same command set with config commits", out.startswith("COMMITTED:"), out)
r91b = make_repo("ac-r91b")
set_verify(r91b, [{"id": "q", "argv": [PY, "-c", "import a"], "cwd": ".", "tier": "fast"}])
proj(r91b, "config", "--clean-tree", "on")
add_chunk(r91b)
start(r91b)
write(r91b, "a.py", "x = 2\n")
rc, out = commit(r91b, "--verified")
check("(r91) non-allowlisted fast entry refused", "not on the runner allowlist" in out, out)

# -- r96: clean-tree not-found, timeout and worktree failure
import _dcio  # noqa: E402
r96 = make_repo("ac-r96")
add_chunk(r96)
start(r96)
write(r96, "a.py", "x = 2\n")
proj(r96, "config", "--clean-tree", "on", "--verify-cmd", "dc-no-such-tool-xyz --check")
rc, out = commit(r96, "--verified")
check("(r96) missing clean-tree command -> no commit",
      out.startswith("NOT COMMITTED: clean-tree command not found: dc-no-such-tool-xyz"), out)
saved_timeout = _dcio.CLEAN_TREE_TIMEOUT
_dcio.CLEAN_TREE_TIMEOUT = 1
try:
    why = _dcio._clean_tree(r96, "", head(r96), [sys.executable, "-c", "__import__('time').sleep(10)"],
                            Path(tempfile.mkdtemp(prefix="dcr96-")))
finally:
    _dcio.CLEAN_TREE_TIMEOUT = saved_timeout
check("(r96) slow clean-tree check times out", why == "clean-tree check timed out", why)
why = _dcio._clean_tree(r96, "", "0" * 40, [sys.executable, "-c", "pass"],
                        Path(tempfile.mkdtemp(prefix="dcr96-")))
check("(r96) bad commit -> worktree failure reported", (why or "").startswith("clean-tree worktree failed"), why)
check("(r96) no worktree left behind", len(git(r96, "worktree", "list").splitlines()) == 1,
      git(r96, "worktree", "list"))

# -- r97: a held index.lock
r97 = make_repo("ac-r97")
add_chunk(r97)
start(r97)
write(r97, "a.py", "x = 2\n")
h97 = head(r97)
lock = r97 / ".git" / "index.lock"
lock.write_text("", encoding="ascii")
try:
    rc, out = commit(r97, "--verified")
finally:
    lock.unlink()
check("(r97) held index.lock -> no commit",
      out.startswith("NOT COMMITTED: the git index is locked") and head(r97) == h97, out)

# -- r98: a hook refusal is reported and leaves nothing of ours staged
r98 = make_repo("ac-r98")
add_chunk(r98)
start(r98)
write(r98, "a.py", "x = 2\n")
write(r98, "new.py", "z = 1\n")
refuse_hook(r98)
h98 = head(r98)
rc, out = commit(r98, "--verified")
check("(r98) hook refusal -> NOT COMMITTED, new file unstaged",
      out.startswith("NOT COMMITTED: git commit refused") and head(r98) == h98
      and "new.py" not in git(r98, "diff", "--cached", "--name-only"), out)

# -- r99: chunk start refuses an unknown milestone
r99 = make_repo("ac-r99")
rc, out = proj(r99, "chunk", "start", "--milestone", "m99")
check("(r99) unknown milestone -> refused", rc != 0 and "m99 is not in DC:ROADMAP" in out, out)

# -- r100: the first step of a repo with no commit yet
r100 = tmp / "ac-r100"
(r100 / ".agent").mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "init", "-q", "-b", "main", str(r100)], check=True)
git(r100, "config", "user.name", "Builder")
git(r100, "config", "user.email", "builder@example.com")
git(r100, "config", "core.autocrlf", "false")
(r100 / ".agent" / "drainclamp-state.md").write_text(dc_state.template_text().replace(
    "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
    "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n"
    "| m1 | build | a.py | active |\n"), encoding="utf-8")
write(r100, ".gitignore", ".agent/\n")
add_chunk(r100)
start(r100)
write(r100, "a.py", "x = 1\n")
write(r100, "new.py", "z = 1\n")
rc, out = commit(r100, "--verified")
check("(r100) no-HEAD repo: first step is the root commit",
      out.startswith("COMMITTED:") and git(r100, "rev-list", "--count", "HEAD") == "1"
      and sorted(git(r100, "show", "--name-only", "--format=", "HEAD").splitlines()) == ["a.py", "new.py"],
      out)

# -- r101: an existing work branch at HEAD is reused
r101 = make_repo("ac-r101", codeowners=True)
git(r101, "branch", "dc/ac-r101-m1")
add_chunk(r101)
start(r101)
write(r101, "a.py", "x = 2\n")
tip101 = head(r101)
rc, out = commit(r101, "--verified")
check("(r101) existing work branch at HEAD is checked out and committed to",
      out.startswith("COMMITTED:") and git(r101, "branch", "--show-current") == "dc/ac-r101-m1"
      and git(r101, "rev-parse", "main") == tip101, out)

# -- t18: a passing tier record for the current tree counts as verified
import dc_verify  # noqa: E402


def record(root, status, tier="fast"):
    dc_verify.save_record(root / ".agent", dc_verify.TIER_RECORD_PREFIX + tier, dc_verify.verify_digest(root), status,
                          "verify.log", dc_verify.tree_fingerprint(root))


r18 = make_repo("ac-t18")
add_chunk(r18)
start(r18)
write(r18, "a.py", "x = 2\n")
h18 = head(r18)
rc, out = commit(r18)
check("(t18) no record: not committed, says so", "not verified: no record" in out and head(r18) == h18, out)
record(r18, "fail")
rc, out = commit(r18)
check("(t18) failed record: not committed, says so", "not verified: it failed" in out and head(r18) == h18, out)
record(r18, "pass")
write(r18, "a.py", "x = 3\n")
rc, out = commit(r18)
check("(t18) pass for an older tree: not committed, says so",
      "not verified: files changed since the check" in out and head(r18) == h18, out)
record(r18, "pass", tier="milestone")
rc, out = commit(r18)
check("(t18) pass for the current tree: committed without --verified", out.startswith("COMMITTED:"), out)

# -- i14: one end-of-step call, chunk done --commit
r14 = make_repo("ac-i14")
add_chunk(r14)
proj(r14, "chunk", "add", "--milestone", "m1", "--goal", "second step", "--targets", "a.py;new.py")
start(r14)
write(r14, "a.py", "x = 2\n")
h14 = head(r14)
rc, out = proj(r14, "chunk", "done", "--milestone", "m1", "--id", "c1", "--commit", "--note", "parser")
side14 = json.loads((r14 / ".agent" / "drainclamp-project.json").read_text(encoding="utf-8"))
c14 = side14["chunks"]["m1"][0]
check("(i14) refused commit: chunk not ticked, reason shown",
      out.startswith("NOT COMMITTED: the step's DC:VERIFY tier is not verified")
      and "not ticked" in out and not c14.get("done") and head(r14) == h14, out)
rc, out = proj(r14, "chunk", "done", "--milestone", "m1", "--id", "c1", "--commit", "--verified",
               "--note", "parser", "--trailer", "Co-Authored-By: Example <a@example.com>")
lines14 = out.splitlines()
side14 = json.loads((r14 / ".agent" / "drainclamp-project.json").read_text(encoding="utf-8"))
c14 = side14["chunks"]["m1"][0]
short14 = lines14[0].split()[1] if lines14 and lines14[0].startswith("COMMITTED:") else "?"
check("(i14) COMMITTED first, then the tick", lines14[0].startswith("COMMITTED:")
      and lines14[1] == "DRAINCLAMP: chunk m1/c1 done" and c14.get("done"), out)
check("(i14) hash lands in the chunk note", c14.get("note") == f"{short14}: parser", c14)
check("(i14) trailer passes through", "Co-Authored-By: Example" in git(r14, "log", "-1", "--format=%B"))
check("(i14) boundary printed", any(ln.startswith("Next chunk: c2") for ln in lines14), out)
check("(i14) same targets: HOLD, then the compact line last",
      any(ln.startswith("HOLD") for ln in lines14) and lines14[-1].startswith("Compact: "), out)
proj(r14, "chunk", "start", "--milestone", "m1", "--id", "c2")
write(r14, "new.py", "z = 1\n")
rc, out = proj(r14, "chunk", "done", "--milestone", "m1", "--id", "c2", "--commit", "--verified")
check("(i14) last chunk: close verdict, no compact line",
      out.startswith("COMMITTED:") and "all chunks done" in out and "Compact: " not in out, out)
r14b = make_repo("ac-i14b")
add_chunk(r14b)
start(r14b)
proj(r14b, "config", "--autocommit", "off")
rc, out = proj(r14b, "chunk", "done", "--milestone", "m1", "--id", "c1", "--commit")
check("(i14) auto-commit off: reported, chunk still ticked",
      "auto-commit is off" in out and "DRAINCLAMP: chunk m1/c1 done" in out, out)

# -- r112: a step with nothing to commit still ticks; a refusal names the way out
r112 = make_repo("ac-r112")
add_chunk(r112)
start(r112)
rc, out = proj(r112, "chunk", "done", "--milestone", "m1", "--id", "c1", "--commit", "--verified")
check("(r112) nothing to commit: reported and ticked",
      "no files changed by this step" in out and "DRAINCLAMP: chunk m1/c1 done" in out, out)
r112b = make_repo("ac-r112b")
add_chunk(r112b)
start(r112b)
write(r112b, "a.py", "x = 2\n")
rc, out = proj(r112b, "chunk", "done", "--milestone", "m1", "--id", "c1", "--commit")
check("(r112) refusal names plain chunk done", "not ticked" in out
      and "tick it without a commit: chunk done --milestone m1 --id c1" in out, out)

# -- r113: a pass is for the DC:VERIFY config it ran, not a later one
r113 = make_repo("ac-r113")
add_chunk(r113)
start(r113)
write(r113, "a.py", "x = 2\n")
h113 = head(r113)
record(r113, "pass", tier="milestone")
set_verify(r113, [{"id": "q", "argv": [PY, "-c", "print(1)"], "cwd": ".", "tier": "fast"}])
rc, out = commit(r113)
check("(r113) DC:VERIFY edited after the pass: not committed",
      "not verified: DC:VERIFY changed since the check" in out and head(r113) == h113, out)
record(r113, "pass", tier="milestone")
rc, out = commit(r113)
check("(r113) pass recorded under the new config: committed", out.startswith("COMMITTED:"), out)

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
