"""Subprocess sandbox and output normalisation checks for _dcio.py."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import _dcio  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


root = Path(tempfile.mkdtemp(prefix="dcsbx-")).resolve()
(root / "pkg").mkdir()
outside = Path(tempfile.mkdtemp(prefix="dcout-")).resolve()

PY = sys.executable

# --- repo-supplied argv: strict ------------------------------------------
# A metacharacter in a stored entry means the author expected a shell. They will
# not get one, so the entry would run as something other than it reads as.
for bad, label in [
    (["pytest", "tests/ && rm -rf /"], "&&"),
    (["sh", "-c", "echo hi | grep hi"], "pipe"),
    (["echo", "a; echo b"], "semicolon"),
    (["cat", "f > out.txt"], "redirect"),
    (["echo", "`whoami`"], "backtick"),
    (["echo", "$(id)"], "substitution"),
]:
    try:
        _dcio.check_repo_argv(bad)
        ok = False
    except _dcio.DcError as exc:
        ok = exc.code == _dcio.EXIT_UNSAFE_COMMAND
    check(f"repo argv with {label} rejected as unsafe", ok)

check("clean repo argv accepted", _dcio.check_repo_argv(["pytest", "tests/", "-q"]) is None)

# --- our own argv: structural only ---------------------------------------
# Without a shell these characters are literal, so blocking them would break
# legitimate commands while buying no safety at all.
check("our own argv may contain a semicolon in an argument",
      _dcio.check_argv([PY, "-c", "import sys; sys.exit(0)"]) is None)
check("our own argv may contain a dollar sign",
      _dcio.check_argv(["grep", "-E", "^foo$", "file.txt"]) is None)
try:
    _dcio.check_argv(["py;thon", "-c", "pass"])
    ok = False
except _dcio.DcError:
    ok = True
check("metacharacter in the executable name is still rejected", ok)

for bad, label in [([], "empty"), ("pytest tests/", "string not array"), ([1, 2], "non-string")]:
    try:
        _dcio.check_argv(bad)
        ok = False
    except _dcio.DcError:
        ok = True
    check(f"{label} argv rejected", ok)

# --- cwd containment ------------------------------------------------------
check("cwd at root accepted", _dcio.resolve_cwd(".", root) == root)
check("cwd in a subpackage accepted (monorepo)",
      _dcio.resolve_cwd("pkg", root) == root / "pkg")
for bad, label in [(str(outside), "absolute path outside"), ("..", "parent traversal")]:
    try:
        _dcio.resolve_cwd(bad, root)
        ok = False
    except _dcio.DcError as exc:
        ok = exc.code == _dcio.EXIT_UNSAFE_COMMAND
    check(f"cwd via {label} rejected", ok)

# symlink escape (skip if unprivileged on Windows)
link = root / "escape"
try:
    link.symlink_to(outside, target_is_directory=True)
    made = True
except OSError:
    made = False
if made:
    try:
        _dcio.resolve_cwd("escape", root)
        ok = False
    except _dcio.DcError:
        ok = True
    check("cwd escaping via symlink rejected", ok)
else:
    print("SKIP  cwd escaping via symlink (needs symlink privilege)")

# --- execution ------------------------------------------------------------
r = _dcio.run_sandboxed([PY, "-c", "print('hello')"], root)
check("clean run captures stdout", r.returncode == 0 and "hello" in r.stdout, r.stdout)
check("run is not a shell", "*" in _dcio.run_sandboxed([PY, "-c", "print('*')"], root).stdout)

r = _dcio.run_sandboxed([PY, "-c", "import sys; sys.exit(3)"], root)
check("non-zero exit surfaced", r.returncode == 3)

r = _dcio.run_sandboxed(["definitely-not-a-real-binary-xyz"], root)
check("missing binary reported, not raised", r.missing and r.returncode is None)

r = _dcio.run_sandboxed([PY, "-c", "import os; print(os.getcwd())"], root, cwd="pkg")
check("cwd honoured", "pkg" in r.stdout, r.stdout)

# stdin is closed, so a reader gets EOF instead of hanging
r = _dcio.run_sandboxed([PY, "-c", "print(len(__import__('sys').stdin.read()))"], root, timeout=10)
check("stdin is devnull, no hang", r.returncode == 0 and r.stdout.strip() == "0", r.stdout)

# --- timeout + process tree ----------------------------------------------
t0 = time.monotonic()
r = _dcio.run_sandboxed([PY, "-c", "import time; time.sleep(30)"], root, timeout=2)
elapsed = time.monotonic() - t0
check("timeout fires", r.timed_out and r.returncode is None)
check("timeout does not wait for the full sleep", elapsed < 15, f"{elapsed:.1f}s")

# A grandchild that outlives its parent must also die. Killing only the parent
# leaves orphaned runners holding file handles, which on Windows then blocks the
# next atomic write.
started = root / "child_started.txt"
leaked = root / "child_leaked.txt"
(root / "child.py").write_text(
    "import pathlib, sys, time\n"
    "pathlib.Path(sys.argv[1]).write_text('started')\n"
    "time.sleep(25)\n"
    "pathlib.Path(sys.argv[2]).write_text('leaked')\n",
    encoding="utf-8")
(root / "spawner.py").write_text(
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, 'child.py', sys.argv[1], sys.argv[2]])\n"
    "time.sleep(25)\n",
    encoding="utf-8")
r = _dcio.run_sandboxed(
    [PY, "spawner.py", str(started), str(leaked)], root, timeout=4)
deadline = time.monotonic() + 6
while time.monotonic() < deadline and not started.exists():
    time.sleep(0.1)
check("grandchild actually started (test is meaningful)", started.exists())
time.sleep(2)
check("process tree terminated, grandchild did not survive",
      r.timed_out and not leaked.exists(),
      f"timed_out={r.timed_out} leaked={leaked.exists()}")

# --- output normalisation -------------------------------------------------
r = _dcio.run_sandboxed(
    [PY, "-c", r"print('\x1b[31mRED\x1b[0m and \x1b[1mBOLD\x1b[0m')"], root)
check("ANSI stripped from captured output",
      "RED and BOLD" in r.stdout and "\x1b" not in r.stdout, repr(r.stdout))

r = _dcio.run_sandboxed([PY, "-c", r"print('a\x00b\x07c')"], root)
check("control characters stripped", "abc" in r.stdout, repr(r.stdout))

r = _dcio.run_sandboxed(
    [PY, "-c", r"import sys; sys.stdout.write('10%\r50%\r100% done\n')"], root)
check("carriage-return progress resolves to final state",
      "100% done" in r.stdout and "10%" not in r.stdout, repr(r.stdout))

r = _dcio.run_sandboxed([PY, "-c", "print('x' * 5000)"], root)
line = [ln for ln in r.stdout.split("\n") if ln][0]
check("single huge line capped with a marker",
      len(line) < 700 and "TRUNCATED 5000 chars" in line, f"{len(line)} chars")

r = _dcio.run_sandboxed([PY, "-c", "print('y' * 400_000)"], root, max_bytes=8192)
check("byte cap applied and marked", r.truncated and "[TRUNCATED at 8192 bytes]" in r.stdout)
check("capped output stays small", len(r.stdout) < 20_000, f"{len(r.stdout)} chars")

r = _dcio.run_sandboxed([PY, "-c", r"import sys; sys.stdout.buffer.write(b'\xff\xfe ok\n')"], root)
check("invalid utf-8 decoded with replacement, no crash", "ok" in r.stdout, repr(r.stdout))

# --- summary helpers ------------------------------------------------------
pytest_like = (
    "============ FAILURES ============\n"
    "____ test_add ____\n"
    "\n"
    "    def test_add():\n"
    ">       assert add(2, 3) == 6\n"
    "E       assert 5 == 6\n"
    "\n"
    "tests/test_calc.py:6: AssertionError\n"
)
check("first_failure_line picks the assertion",
      "assert 5 == 6" in _dcio.first_failure_line(pytest_like),
      _dcio.first_failure_line(pytest_like))
check("collapse flattens to one line",
      "\n" not in _dcio.collapse("a\n\n  b   c\n"), _dcio.collapse("a\n\n  b   c\n"))
check("collapse caps length", len(_dcio.collapse("z" * 5000)) < 600)
check("first_failure_line on empty input is empty", _dcio.first_failure_line("") == "")

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
