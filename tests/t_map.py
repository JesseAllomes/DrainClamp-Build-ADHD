"""dc_map.py checks (skeleton step 4): ast mapping, cache reuse, verdicts."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import dc_map  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def run(root, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_map.py"), str(root), *args],
        capture_output=True, text=True,
    )


repo = Path(tempfile.mkdtemp(prefix="dcmap-"))
subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
(repo / "src").mkdir()
(repo / "src/app.py").write_text(
    "import os\n\n\nclass Widget:\n    def build(self):\n        return 1\n\n"
    "    def teardown(self):\n        return 2\n\n\ndef helper(x):\n    return x\n",
    encoding="utf-8")
(repo / "src/util.js").write_text(
    "export function jsThing() {\n  return 1;\n}\n\n"
    "class Panel {\n  render() {\n    return 2;\n  }\n}\n\n"
    "const arrowThing = (a, b) => a + b;\n", encoding="utf-8")
(repo / "src/legacy.rb").write_text("def legacy_entry\n  1\nend\n", encoding="utf-8")
(repo / "src/broken.py").write_text("def oops(:\n", encoding="utf-8")
(repo / ".venv/Lib").mkdir(parents=True)
(repo / ".venv/Lib/vendor.py").write_text("def vendored(): pass\n", encoding="utf-8")

out = run(repo)
lines = out.stdout.splitlines()
check("COVERAGE: partial in response", "COVERAGE: partial" in lines)
check("class and nested methods indexed",
      any("Widget.build" in l for l in lines) and any("Widget.teardown" in l for l in lines))
check("module function indexed", any("\thelper" in l for l in lines))
check("end_lineno recorded as a range", any("src/app.py:4-9\tWidget" in l for l in lines),
      next((l for l in lines if l.endswith("\tWidget")), ""))
check("method range is narrower than its class",
      any("src/app.py:5-6\tWidget.build" in l for l in lines),
      next((l for l in lines if l.endswith("Widget.build")), ""))
check(".venv excluded", not any(".venv" in l for l in lines))
check("JS function indexed by the regex scanner",
      any("src/util.js:1-3\tjsThing" in l for l in lines),
      next((l for l in lines if l.endswith("\tjsThing")), ""))
check("JS class method indexed as Class.method",
      any("Panel.render" in l for l in lines))
check("JS arrow assignment indexed",
      any("\tarrowThing" in l for l in lines))
check("an extension with no grammar is UNSUPPORTED, not zero symbols",
      any("src/legacy.rb" in l and "UNSUPPORTED" in l for l in lines))
check("malformed Python reported PARSE-FAILED",
      any("src/broken.py" in l and "PARSE-FAILED" in l for l in lines))
check("UNSUPPORTED and PARSE-FAILED are distinct verdicts",
      not any("src/legacy.rb" in l and "PARSE-FAILED" in l for l in lines))

# cache reuse: unchanged files are not reparsed
_, before = dc_map.load_cache(dc_map.cache_path(repo))
calls = {"n": 0}
real = dc_map.classify


def counting(path):
    calls["n"] += 1
    return real(path)


dc_map.classify = counting
dc_map.refresh(repo)
check("unchanged files are not reparsed", calls["n"] == 0, f"{calls['n']} reparse(s)")

time.sleep(0.02)
(repo / "src/app.py").write_text(
    "def helper(x):\n    return x\n\n\ndef added():\n    return 3\n", encoding="utf-8")
calls["n"] = 0
dc_map.refresh(repo)
dc_map.classify = real
check("changed file is reparsed exactly once", calls["n"] == 1, f"{calls['n']}")

out = run(repo, "--symbol", "added")
check("new symbol appears after refresh", any("\tadded" in l for l in out.stdout.splitlines()))
check("stale symbol is gone", not any("Widget" in l for l in out.stdout.splitlines()))

# deleted file drops out
(repo / "src/broken.py").unlink()
out = run(repo)
check("deleted file drops from the index", "src/broken.py" not in out.stdout)

# a cache written by another grammar is not reinterpreted
cache = dc_map.cache_path(repo)
stale = cache.read_text(encoding="utf-8").replace(
    f"# schema={dc_map.MAP_SCHEMA}", "# schema=1", 1)
cache.write_text(stale, encoding="utf-8")
_, records = dc_map.load_cache(cache)
check("a cache from an older map schema is discarded", records == {},
      f"{len(records)} record(s) kept")
dc_map.refresh(repo)

# --no-refresh
out = run(repo, "--no-refresh")
check("--no-refresh prints CACHE: unvalidated", "CACHE: unvalidated" in out.stdout)
check("--no-refresh still prints COVERAGE: partial", "COVERAGE: partial" in out.stdout)

# paging + SHOWING n/N, and COVERAGE on a slice
big = Path(tempfile.mkdtemp(prefix="dcmapbig-"))
subprocess.run(["git", "init", "-q"], cwd=big, capture_output=True)
(big / "many.py").write_text(
    "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(150)), encoding="utf-8")
p1 = run(big)
p2 = run(big, "--page", "2")
check("page 1 caps at 120 with SHOWING n/N", "SHOWING 120/150 (page 1)" in p1.stdout)
check("page 2 returns the remainder", "SHOWING 30/150 (page 2)" in p2.stdout)
check("COVERAGE: partial repeats on a slice", "COVERAGE: partial" in p2.stdout)
check("page 2 contains the tail symbol", any("\tf149" in l for l in p2.stdout.splitlines()))

# --path filter
out = run(repo, "--path", "src")
check("--path filters to a subtree", all(
    l.startswith("src/") for l in out.stdout.splitlines()
    if ":" in l and "\t" in l and not l.startswith(("COVERAGE", "SHOWING", "NOT", "CACHE"))))

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
