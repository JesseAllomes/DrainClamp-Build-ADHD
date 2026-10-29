"""Codex as a named external agent (dc_codex.py): argv, prompt, run outcomes with a fake codex."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))
sys.path.insert(0, str(SCRIPTS))
import dc_codex  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dccodex-"))
root, result = tmp / "repo", tmp / "out" / "codex.jsonl"
root.mkdir()

# 1. argv: read-only critic, workspace-write builder, prompt on stdin
argv = dc_codex.critic_argv("codex", root, result)
check("critic argv is read-only, rooted, writes the result file, prompt on stdin",
      argv[:2] == ["codex", "exec"] and argv[argv.index("-C") + 1] == str(root)
      and argv[argv.index("-s") + 1] == "read-only" and argv[argv.index("-o") + 1] == str(result)
      and argv[-1] == "-", argv)
argv = dc_codex.builder_argv("codex", root, result)
check("builder argv is workspace-write in its worktree",
      argv[argv.index("-s") + 1] == "workspace-write" and argv[argv.index("-C") + 1] == str(root), argv)

# 2. the prompt carries the packet, both lenses and the Claude critic's own method
prompt = dc_codex.critic_prompt(".agent/review/R3/packet.md")
check("prompt names the packet and the A+B lens", ".agent/review/R3/packet.md" in prompt
      and "A+B" in prompt and "read-only" in prompt)
check("prompt reuses dca-critic's instructions without frontmatter",
      "JSON" in prompt and "NO FINDINGS" in prompt and "\nname: dca-critic" not in prompt)

# 3. availability: off switch and PATH lookup
os.environ[dc_codex.OFF_ENV] = "1"
check("the off switch hides codex", dc_codex.executable() is None)
del os.environ[dc_codex.OFF_ENV]

# 4. run outcomes with a fake codex (a Python script behind a .cmd/.sh shim)
fake = tmp / "fake_codex.py"
fake.write_text(
    "import sys, time\n"
    "args = sys.argv[1:]\n"
    "mode = open(sys.argv[0] + '.mode').read().strip()\n"
    "prompt = sys.stdin.read()\n"
    "out = args[args.index('-o') + 1]\n"
    "if mode == 'ok': open(out, 'w').write('NO FINDINGS\\n')\n"
    "if mode == 'fail': sys.stderr.write('auth expired\\n'); sys.exit(3)\n"
    "if mode == 'slow': time.sleep(5)\n"
    "if mode == 'empty': open(out, 'w').write('')\n", encoding="utf-8")
bindir = tmp / "bin"
bindir.mkdir()
if os.name == "nt":
    (bindir / "codex.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
else:
    shim = bindir / "codex"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
os.environ["PATH"] = str(bindir) + os.pathsep + os.environ["PATH"]
exe = dc_codex.executable()
check("a codex on PATH is found", exe is not None and Path(exe).parent == bindir, exe)


def attempt(mode, timeout=60):
    (tmp / "fake_codex.py.mode").write_text(mode, encoding="utf-8")
    return dc_codex.run(dc_codex.critic_argv(exe, root, result), "prompt", result, timeout)


ok, why = attempt("ok")
check("a clean run returns the result file", ok and result.read_text().strip() == "NO FINDINGS", why)
ok, why = attempt("fail")
check("a failing codex is one line with its exit code", not ok and "exited 3" in why
      and "auth expired" in why, why)
ok, why = attempt("empty")
check("an empty result counts as no result", not ok and "no result" in why, why)
ok, why = attempt("slow", timeout=1)
check("a slow codex times out instead of blocking", not ok and "timed out" in why, why)
check("a failed run leaves no stale result behind", not result.exists() or not result.read_text().strip())

# 5. a codex inside the current directory (the reviewed repo) is never run
import subprocess  # noqa: E402

cwd_dir, empty_dir = tmp / "cwdrepo", tmp / "emptybin"
cwd_dir.mkdir()
empty_dir.mkdir()
if os.name == "nt":
    (cwd_dir / "codex.cmd").write_text("@echo off\r\n", encoding="utf-8")
else:
    (cwd_dir / "codex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (cwd_dir / "codex").chmod(0o755)
probe = ("import sys; sys.path.insert(0, sys.argv[1]); import dc_codex; "
         "print(dc_codex.executable())")


def probe_executable(path_value):
    env = {**os.environ, "PATH": path_value}
    env.pop(dc_codex.OFF_ENV, None)
    proc = subprocess.run([sys.executable, "-c", probe, str(SCRIPTS)], cwd=str(cwd_dir), env=env,
                          capture_output=True, text=True)
    return proc.stdout.strip(), proc.stderr


got, err = probe_executable(str(empty_dir))
check("a codex only in the current directory is not found", got == "None", (got, err))
got, err = probe_executable(str(empty_dir) + os.pathsep + ".")
check("a '.' PATH entry does not make the current directory searchable", got == "None", (got, err))
got, err = probe_executable(str(empty_dir) + os.pathsep + str(cwd_dir))
check("the current directory as an absolute PATH entry is still skipped", got == "None", (got, err))
other = tmp / "elsewhere"
other.mkdir()
if os.name == "nt":
    (other / "codex.cmd").write_text("@echo off\r\n", encoding="utf-8")
else:
    (other / "codex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (other / "codex").chmod(0o755)
got, err = probe_executable(str(empty_dir) + os.pathsep + str(other))
check("a codex in an absolute PATH entry is found",
      got != "None" and Path(got).resolve().parent == other.resolve(), (got, err))

# a symlinked codex on PATH (POSIX) is accepted
if os.name == "nt":
    check("symlinked codex on PATH (skipped on Windows)", True)
else:
    real_dir, link_dir = tmp / "real", tmp / "linkbin"
    real_dir.mkdir()
    link_dir.mkdir()
    (real_dir / "codex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (real_dir / "codex").chmod(0o755)
    os.symlink(real_dir / "codex", link_dir / "codex")
    got, err = probe_executable(str(link_dir))
    check("a codex symlinked into a PATH folder is found",
          got != "None" and Path(got).parent.resolve() == link_dir.resolve(), (got, err))

shutil.rmtree(tmp, ignore_errors=True)
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)
