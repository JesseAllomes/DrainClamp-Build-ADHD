"""dc_selftest.py checks: every fixture family exists, and gaps are declared.

A fixture that quietly fails to materialise turns a real assertion into a
vacuous one, so the manifest has to say what was skipped and why.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "drainclamp-build-adhd" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import dc_map  # noqa: E402
import dc_selftest  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


dest = Path(tempfile.mkdtemp(prefix="dcselftest-")) / "fixtures"
manifest = dc_selftest.materialise(dest)

EXPECTED = {
    "py_pkg/core.py", "py_pkg/duplicate_a.py", "py_pkg/duplicate_b.py",
    "py_pkg/malformed.py", "py_pkg/crlf_module.py",
    "py_pkg/deep/deeper/deepest/buried.py", "js_pkg/app.js",
    "unsupported/legacy.rb", "spaced dir/module with spaces.py",
    "state/roadmap_empty.md", "repo_calc/src/calc.py",
    "repo_calc/notes_untracked.py",
}
missing = sorted(rel for rel in EXPECTED if not (dest / rel).is_file())
check("every fixture family is on disk", not missing, ", ".join(missing))
check("the manifest is written", (dest / dc_selftest.MANIFEST_NAME).is_file())

unicode_files = [p for p in (dest / "py_pkg").iterdir() if "naïve" in p.name]
check("the Unicode filename exists or is declared SKIPPED",
      bool(unicode_files) or manifest["families"]["unicode filename"] == "SKIPPED",
      manifest["families"]["unicode filename"])

crlf = (dest / "py_pkg" / "crlf_module.py").read_bytes()
check("the CRLF fixture really has CRLF endings", b"\r\n" in crlf)
check("the CRLF fixture does not duplicate core.py symbols",
      b"orphan_helper" not in crlf)

link = dest / "links" / "linked_pkg"
declared_skip = any("directory link" in note for note in manifest["notes"])
check("the directory link exists or is declared SKIPPED",
      dc_map.is_link(link) or declared_skip,
      manifest["families"]["directory link"])

status = subprocess.run(["git", "status", "--porcelain"], cwd=str(dest / "repo_calc"),
                        capture_output=True, text=True)
check("the git fixture leaves exactly one untracked file",
      [ln.split()[-1] for ln in status.stdout.splitlines()] == ["notes_untracked.py"],
      status.stdout.replace("\n", " | ").strip())

head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(dest / "repo_calc"),
                      capture_output=True, text=True)
check("the git fixture has a HEAD to compare against",
      head.returncode == 0 and len(head.stdout.strip()) >= 7,
      head.stdout.strip()[:12])

status2 = dc_selftest.materialise(dest)
check("materialising twice is deterministic",
      status2["families"] == manifest["families"],
      f"{len(status2['families'])} families both runs")

# The index must see the fixtures as the suites assume: two `handle` symbols,
# one malformed file, one unsupported extension.
records = dc_map.refresh(dest)
handles = [rel for rel, (_key, status, symbols) in records.items()
           if any(name == "handle" for name, _s, _e in symbols)]
check("the duplicate-symbol fixture really is ambiguous", len(handles) == 2,
      ", ".join(sorted(handles)))
check("the malformed fixture is PARSE-FAILED",
      records.get("py_pkg/malformed.py", [None, None])[1] == dc_map.PARSE_FAILED,
      str(records.get("py_pkg/malformed.py", ["", "?"])[1]))
check("the unsupported fixture is UNSUPPORTED",
      records.get("unsupported/legacy.rb", [None, None])[1] == dc_map.UNSUPPORTED,
      str(records.get("unsupported/legacy.rb", ["", "?"])[1]))

out = subprocess.run(
    [sys.executable, "-B", str(SCRIPTS / "dc_selftest.py"), "--materialise",
     "--dest", str(dest)], capture_output=True, text=True)
check("the CLI reports what it wrote", out.returncode == 0 and "FIXTURES:" in out.stdout,
      out.stdout.strip().splitlines()[0] if out.stdout else out.stderr.strip())
check("anything skipped is printed, not swallowed",
      (not manifest["notes"]) or "SKIPPED" in out.stdout,
      "; ".join(manifest["notes"]) or "nothing skipped on this host")

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
