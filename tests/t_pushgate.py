"""Machine-wide push gate (dc_pushgate.py): real `git push` through the installed hook.

Global git config is redirected to a temp file (GIT_CONFIG_GLOBAL) and DRAINCLAMP_HOME
to a temp folder, so the machine's own hooksPath is never touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
tmp = Path(tempfile.mkdtemp(prefix="dcgate-"))
home = tmp / "dchome"
home.mkdir()
(tmp / "none.txt").write_text("", encoding="utf-8")
(home / "config.json").write_text(json.dumps({"pii_names_file": str(tmp / "none.txt")}),
                                  encoding="utf-8")
os.environ["DRAINCLAMP_HOME"] = str(home)
os.environ["GIT_CONFIG_GLOBAL"] = str(tmp / "gitconfig")
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
(tmp / "gitconfig").write_text("[user]\n\tname = t\n\temail = t@t\n", encoding="utf-8")
sys.path.insert(0, str(SCRIPTS))
import dc_pushgate  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def J(*parts):
    return "".join(parts)


def gate(*args, root=None):
    cmd = [sys.executable, "-B", str(SCRIPTS / "dc_pushgate.py")]
    if root:
        cmd += ["--root", str(root)]
    proc = subprocess.run(cmd + list(args), capture_output=True, text=True, encoding="utf-8")
    return proc.returncode, proc.stdout + proc.stderr


def git(repo, *args, check_rc=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8")
    if check_rc and proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return proc.returncode, proc.stdout + proc.stderr


def commit(repo, rel, text, msg="change"):
    (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


remote = tmp / "remote.git"
subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
repo = tmp / "repo"
subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)
commit(repo, "README.md", "hello\n", "start")

# 1. not installed: status says so, and Gate P holds
rc, out = gate("status")
check("status before install says not installed", rc != 0 and "not installed" in out, out)
rc, out = gate("check", root=repo)
check("check holds a clean push when the hook is not installed", rc != 0
      and "PUSH: hold (push gate not installed" in out, out)

# 2. a foreign global hooksPath is never replaced without --force
git(repo, "config", "--global", "core.hooksPath", str(tmp / "theirs"))
rc, out = gate("install")
check("install refuses to replace another global hooksPath", rc != 0 and "refusing" in out, out)
git(repo, "config", "--global", "--unset", "core.hooksPath")

# 3. install: hooksPath set, scanner copied, status active
rc, out = gate("install")
hooks = home / "git-hooks"
check("install sets the global hooksPath", rc == 0 and
      dc_pushgate._same(git(repo, "config", "--global", "core.hooksPath")[1].strip(), hooks), out)
check("install copies the scanner and its word list",
      (home / "pii-gate" / "scripts" / "dc_pii.py").is_file()
      and (home / "pii-gate" / "assets" / "given-names.txt").is_file())
check("no dispatcher for the hooks git fires on every command",
      not (hooks / "reference-transaction").exists() and (hooks / "post-commit").is_file())
rc, out = gate("status", root=repo)
check("status is active in a repository", rc == 0 and "active" in out, out)

# 4. a real push: clean goes, PII is refused, the value is never printed
rc, out = git(repo, "push", "-q", "origin", "HEAD:main", check_rc=False)
check("a clean push goes through the hook", rc == 0, out)
email = J("jane", "@", "acme.com")
commit(repo, "owner.txt", J("owner ", email, "\n"), "owner")
rc, out = git(repo, "push", "-q", "origin", "HEAD:main", check_rc=False)
check("a push carrying PII is refused", rc != 0 and "push refused" in out and "EMAIL" in out, out)
check("the refusal never prints the value", email not in out, out)
rc, _ = git(repo, "ls-remote", "origin", "main")
remote_head = _.split()[0] if _.strip() else ""
check("the remote did not move", remote_head != git(repo, "rev-parse", "HEAD")[1].strip())
git(repo, "reset", "-q", "--hard", "HEAD~1")

# 5. the repository's own hooks still run: pre-push after the scan, others through dispatchers
hookdir = repo / ".git" / "hooks"
(hookdir / "pre-push").write_text("#!/bin/sh\ncat > \"$(git rev-parse --git-dir)/prepush-in\"\n"
                                  "exit 3\n", encoding="utf-8")
(hookdir / "post-commit").write_text("#!/bin/sh\ntouch \"$(git rev-parse --git-dir)/post-ran\"\n",
                                     encoding="utf-8")
for h in ("pre-push", "post-commit"):
    (hookdir / h).chmod(0o755)
commit(repo, "b.txt", "fine\n", "fine")
check("a repository post-commit hook still runs", (repo / ".git" / "post-ran").exists())
rc, out = git(repo, "push", "-q", "origin", "HEAD:main", check_rc=False)
seen = (repo / ".git" / "prepush-in").read_text(encoding="utf-8") if \
    (repo / ".git" / "prepush-in").exists() else ""
check("the repository pre-push runs after a clean scan and can still block",
      rc != 0 and "refs/heads/main" in seen, (rc, out, seen))
(hookdir / "pre-push").unlink()

# 6. fail closed: no scanner, no push
(home / "pii-gate" / "scripts" / "dc_pii.py").unlink()
rc, out = git(repo, "push", "-q", "origin", "HEAD:main", check_rc=False)
check("a missing scanner refuses the push", rc != 0, out)
rc, out = gate("status", root=repo)
check("status reports the stale install", rc != 0 and "rerun dc_pushgate.py install" in out, out)
gate("install")

# 7. a local hooksPath bypasses the global gate, and status says so
git(repo, "config", "core.hooksPath", ".husky")
rc, out = gate("status", root=repo)
check("status flags a repository that bypasses the gate", rc != 0 and "INACTIVE" in out, out)
git(repo, "config", "--unset", "core.hooksPath")

# 8. Gate P check: scans the outgoing commits with the source scanner
commit(repo, "c.txt", J("ring 0412", " 345", " 678\n"), "phone")
rc, out = gate("check", root=repo)
check("check refuses outgoing PII and reports the hook", rc != 0 and "PHONE" in out
      and "PUSH GATE: active" in out and out.strip().endswith("PUSH: hold (PII found: remove it or allow it, see above)"), out)
git(repo, "reset", "-q", "--hard", "HEAD~1")
rc, out = gate("check", root=repo)
check("check passes a clean branch", rc == 0 and "clean" in out and out.strip().endswith("PUSH: go"), out)
git(repo, "config", "core.hooksPath", ".husky")
rc, out = gate("check", root=repo)
check("a repo with its own hooksPath may push but is told", rc == 0
      and out.strip().endswith("PUSH: go (manual pushes from this repo are not covered by the hook)"), out)
git(repo, "config", "--unset", "core.hooksPath")
(home / "pii-gate" / "assets" / "given-names.txt").write_text("changed", encoding="utf-8")
rc, out = gate("check", root=repo)
check("a stale installed scanner holds the push", rc != 0 and "PUSH: hold (installed scanner is out of date" in out, out)
gate("install")

# 9. uninstall: our hooksPath unset, our files gone, a foreign file kept
(hooks / "notes.txt").write_text("mine", encoding="utf-8")
rc, out = gate("uninstall")
rc2, cfg = git(repo, "config", "--global", "--get", "core.hooksPath", check_rc=False)
check("uninstall unsets our hooksPath", rc == 0 and not cfg.strip(), (out, cfg))
check("uninstall removes only its own files", not (hooks / "pre-push").exists()
      and (hooks / "notes.txt").exists() and not (home / "pii-gate" / "scripts").exists())

# 10. the pre-push dispatcher hands the destination remote ($1) to the scanner
disp = dc_pushgate.dispatcher("pre-push", "/usr/bin/python3", tmp / "gate")
check("the pre-push dispatcher passes the remote to the scanner", 'prepush --remote "$1"' in disp, disp)

# 11. check() scans against the branch's own remote, origin for a detached HEAD
rrepo = tmp / "remrepo"
subprocess.run(["git", "init", "-q", "-b", "main", str(rrepo)], check=True, capture_output=True)
commit(rrepo, "README.md", "hello\n", "start")
git(rrepo, "config", "branch.main.remote", "upstream2")
real_run, real_status = dc_pushgate.subprocess.run, dc_pushgate.status
real_verdict = dc_pushgate.push_verdict
recorded = []


def recorder(argv, *a, **k):
    if argv and argv[0] == "git":
        return real_run(argv, *a, **k)  # repository lookups stay real
    recorded.append(list(argv))
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def prepush_remote():
    del recorded[:]
    dc_pushgate.check(rrepo, None)
    argv = next((c for c in recorded if "prepush" in c), [])
    return argv[argv.index("--remote") + 1] if "--remote" in argv else None


try:
    dc_pushgate.subprocess.run = recorder
    dc_pushgate.status = lambda root, home: (True, [])
    check("check scans against the branch's own remote", prepush_remote() == "upstream2",
          recorded)
    git(rrepo, "checkout", "-q", "--detach")
    check("a detached HEAD is scanned against origin", prepush_remote() == "origin", recorded)
finally:
    dc_pushgate.subprocess.run = real_run
    dc_pushgate.status = real_status
    dc_pushgate.push_verdict = real_verdict

shutil.rmtree(tmp, ignore_errors=True)
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)
