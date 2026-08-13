"""dc_chunk.py checks: ambiguity refusal and read sizing."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def run(root, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_chunk.py"), "--root", str(root), *args],
        capture_output=True, text=True, cwd=str(root),
    )


repo = Path(tempfile.mkdtemp(prefix="dcchk-"))
subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
(repo / "a").mkdir()
(repo / "b").mkdir()
# same symbol name in two modules
for pkg in ("a", "b"):
    (repo / pkg / "mod.py").write_text(
        f"def handle(x):\n    return '{pkg}'\n\n\ndef only_{pkg}():\n    return 1\n",
        encoding="utf-8")
# a big file: 300 lines, one 100-line function
big = ["def small():", "    return 0", "", ""]
big += ["def middle():"] + [f"    x{i} = {i}" for i in range(98)] + ["    return 1", "", ""]
big += [f"# filler {i}" for i in range(300 - len(big))]
(repo / "big.py").write_text("\n".join(big) + "\n", encoding="utf-8")
# a small file
(repo / "tiny.py").write_text("def t():\n    return 1\n", encoding="utf-8")

subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_map.py"), str(repo)],
               capture_output=True, text=True)

# ambiguity
out = run(repo, "--symbol", "handle")
check("ambiguous symbol refuses", out.returncode != 0 and "AMBIGUOUS: 2" in out.stdout)
check("candidates are listed", "a/mod.py::handle" in out.stdout and "b/mod.py::handle" in out.stdout)
check("refusal says it will not guess", "Refusing to guess" in out.stdout)
check("no file content leaked on refusal", "return 'a'" not in out.stdout)

out = run(repo, "--symbol", "handle", "--path", "a")
check("--path disambiguates", out.returncode == 0 and "return 'a'" in out.stdout)
out = run(repo, "--symbol", "handle", "--match", "b/mod.py::handle")
check("--match disambiguates", out.returncode == 0 and "return 'b'" in out.stdout)
out = run(repo, "--symbol", "only_a")
check("unique symbol resolves directly", out.returncode == 0 and "only_a" in out.stdout)

out = run(repo, "--symbol", "nonexistent")
check("missing symbol says the index is partial",
      out.returncode != 0 and "not proof" in out.stderr)

# read sizing
out = run(repo, "--symbol", "middle")
check("100-line span in a 300-line file reads as one range, no full-read advice",
      "ADVICE" not in out.stdout and "big.py:5-104" in out.stdout, out.stdout.splitlines()[0])
check("range emitted with a total for context",
      "of 300 lines" in out.stdout)

out = run(repo, "tiny.py", "1", "2")
check("file <=120 lines advises a full read", "read it in full" in out.stdout)

out = run(repo, "big.py", "1", "200")
check("span >=50% advises a full read", "read the file in full" in out.stdout)

out = run(repo, "big.py", "5", "104")
check("span <50% does not advise a full read", "ADVICE" not in out.stdout)

# grep
out = run(repo, "big.py", "--grep", "filler 3$", "--context", "1")
check("grep marks the hit line", ">" in out.stdout and "filler 3" in out.stdout)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
