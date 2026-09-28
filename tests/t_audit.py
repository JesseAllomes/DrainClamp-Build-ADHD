"""dc_audit.py checks: the freshness test, rollups, stack, dead-code candidates.

The freshness test is the whole value of the cache, so most of this file is
about what must invalidate it. HEAD plus a TTL would pass a naive test while
missing every uncommitted edit — which is the state a working session lives in.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import dc_selftest  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def audit(root, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_audit.py"), str(root), *args],
        capture_output=True, text=True,
    )


def dc_state(root, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_state.py"), "--root", str(root), *args],
        capture_output=True, text=True,
    )


def git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def refreshed(out):
    return out.stdout.startswith("AUDIT: refreshed")


fixtures = Path(os.environ.get("DC_FIXTURES") or tempfile.mkdtemp(prefix="dcfix-"))
if not (fixtures / "MANIFEST.txt").exists():
    dc_selftest.materialise(fixtures)
repo = fixtures / "repo_calc"

# A clean baseline: the fixture leaves one file untracked on purpose, and an
# untracked file is exactly what must invalidate a cache.
first = audit(repo)
check("first run scans", refreshed(first), first.stdout.splitlines()[0] if first.stdout else first.stderr)
check("bounded output", len(first.stdout.splitlines()) <= 15,
      f"{len(first.stdout.splitlines())} lines")
check("audit.md written", (repo / ".agent/audit.md").is_file())
check("audit.json written", (repo / ".agent/audit.json").is_file())
check("COVERAGE stated on the totals line", "COVERAGE: partial" in first.stdout)

second = audit(repo)
check("clean re-run reuses the cache", second.stdout.startswith("CACHE: fresh"),
      second.stdout.splitlines()[0])

# --- what must invalidate --------------------------------------------------
(repo / "src" / "extra.py").write_text("def extra():\n    return 1\n", encoding="utf-8")
out = audit(repo)
check("a new untracked file invalidates", refreshed(out), out.stdout.splitlines()[0])
check("reason names dirty/untracked files", "dirty or untracked" in out.stdout.splitlines()[0])
(repo / "src" / "extra.py").unlink()
audit(repo)

original = (repo / "src" / "calc.py").read_text(encoding="utf-8")
(repo / "src" / "calc.py").write_text(original + "\n\ndef divide(a, b):\n    return a / b\n",
                                      encoding="utf-8")
out = audit(repo)
check("a dirty tracked file invalidates", refreshed(out), out.stdout.splitlines()[0])
(repo / "src" / "calc.py").write_text(original, encoding="utf-8")
audit(repo)
out = audit(repo)
check("a byte-identical revert does not invalidate", out.stdout.startswith("CACHE: fresh"),
      out.stdout.splitlines()[0])

git(repo, "add", "-A")
git(repo, "commit", "-qm", "commit the fixture leftovers")
audit(repo)
out = audit(repo)
check("committing settles the cache again", out.stdout.startswith("CACHE: fresh"),
      out.stdout.splitlines()[0])

before_head = audit(repo)
(repo / "src" / "calc.py").write_text(original + "\n\ndef modulo(a, b):\n    return a % b\n",
                                      encoding="utf-8")
git(repo, "add", "-A")
git(repo, "commit", "-qm", "second commit")
out = audit(repo)
check("a new HEAD invalidates", refreshed(out) and "HEAD" in out.stdout.splitlines()[0],
      out.stdout.splitlines()[0])
_ = before_head

git(repo, "mv", "src/calc.py", "src/calculator.py")
out = audit(repo)
check("a rename invalidates", refreshed(out), out.stdout.splitlines()[0])
git(repo, "mv", "src/calculator.py", "src/calc.py")
git(repo, "commit", "-qam", "restore name")
audit(repo)

# --- every verification-config family -------------------------------------
CONFIG_FAMILIES = {
    "package.json": '{"name": "x", "scripts": {"test": "jest"}}\n',
    "pytest.ini": "[pytest]\n",
    "setup.cfg": "[tool:pytest]\n",
    "tox.ini": "[pytest]\n",
    "go.mod": "module example.com/x\n",
    "Cargo.toml": "[package]\nname = \"x\"\n",
    "ruff.toml": "line-length = 100\n",
    ".ruff.toml": "line-length = 99\n",
    "eslint.config.js": "export default [];\n",
    ".eslintrc.json": "{}\n",
    "tsconfig.json": "{}\n",
    "jest.config.js": "module.exports = {};\n",
    "vitest.config.ts": "export default {};\n",
}
config_misses = []
for name, body in CONFIG_FAMILIES.items():
    (repo / name).write_text(body, encoding="utf-8")
    out = audit(repo)
    if not (refreshed(out) and "verification config" in out.stdout.splitlines()[0]):
        config_misses.append(f"create:{name}")
    if not audit(repo).stdout.startswith("CACHE: fresh"):
        config_misses.append(f"settle:{name}")
check("creating any verification-config family invalidates", not config_misses,
      ", ".join(config_misses))

# pyproject.toml already exists in the fixture: byte change, rename, delete.
pyproject = repo / "pyproject.toml"
body = pyproject.read_text(encoding="utf-8")
pyproject.write_text(body + "\n# changed\n", encoding="utf-8")
out = audit(repo)
check("a byte change in a config invalidates",
      refreshed(out) and "verification config" in out.stdout.splitlines()[0],
      out.stdout.splitlines()[0])
audit(repo)

pyproject.rename(repo / "pyproject.renamed.toml")
out = audit(repo)
check("a config rename invalidates", refreshed(out), out.stdout.splitlines()[0])
(repo / "pyproject.renamed.toml").rename(pyproject)
audit(repo)

(repo / "ruff.toml").unlink()
out = audit(repo)
check("a config deletion invalidates",
      refreshed(out) and "verification config" in out.stdout.splitlines()[0],
      out.stdout.splitlines()[0])
audit(repo)

# A lockfile churns on every install without changing what the repo declares.
(repo / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
out = audit(repo)
check("a lockfile is not a verification-config input",
      "verification config" not in out.stdout.splitlines()[0], out.stdout.splitlines()[0])
audit(repo)

# --- TTL --------------------------------------------------------------------
cache_file = repo / ".agent" / "audit.json"
cached = json.loads(cache_file.read_text(encoding="utf-8"))
cached["generated"] = (datetime.now(timezone.utc) - timedelta(hours=48)) \
    .replace(microsecond=0).isoformat()
cache_file.write_text(json.dumps(cached, indent=2), encoding="utf-8")
out = audit(repo)
check("an expired TTL invalidates", refreshed(out) and "TTL" in out.stdout.splitlines()[0],
      out.stdout.splitlines()[0])

# --- Gate 3 signal ----------------------------------------------------------
out = audit(repo)
check("empty DC:VERIFY requires Gate 3",
      "GATE3: required (DC:VERIFY is empty)" in out.stdout,
      next((ln for ln in out.stdout.splitlines() if ln.startswith("GATE3")), ""))

verify_file = repo / ".agent" / "ver.json"
verify_file.write_text(json.dumps([
    {"id": "pytest-unit", "argv": ["pytest", "tests/"], "cwd": ".", "tier": "milestone"}
]), encoding="utf-8")
dc_state(repo, "--set", "VERIFY", "--file", str(verify_file))
audit(repo, "--force")
out = audit(repo)
gate3 = next((ln for ln in out.stdout.splitlines() if ln.startswith("GATE3")), "")
check("a registered DC:VERIFY lets Gate 3 skip", gate3.startswith("GATE3: skip"), gate3)

verify_file.write_text(json.dumps([
    {"id": "pytest-unit", "argv": ["pytest", "tests/", "-q"], "cwd": ".", "tier": "milestone"}
]), encoding="utf-8")
dc_state(repo, "--set", "VERIFY", "--file", str(verify_file))
out = audit(repo)
gate3 = next((ln for ln in out.stdout.splitlines() if ln.startswith("GATE3")), "")
check("a changed DC:VERIFY requires Gate 3 again",
      gate3.startswith("GATE3: required") and "DC:VERIFY changed" in gate3, gate3)

# --- scan shape -------------------------------------------------------------
deep = Path(tempfile.mkdtemp(prefix="dcaudit-deep-"))
dc_selftest.materialise(deep / "tree")
tree = deep / "tree"
subprocess.run(["git", "init", "-q"], cwd=tree, capture_output=True)
out = audit(tree, "--force")
report = json.loads((tree / ".agent/audit.json").read_text(encoding="utf-8"))
depth2 = {row["path"]: row for row in report["scan"]["depth2"]}
check("deeply nested code appears in a depth-2 bucket",
      any(row["files"] >= 1 for path, row in depth2.items() if path.startswith("py_pkg")),
      ", ".join(sorted(depth2)[:6]))
buried_counted = sum(row["files"] for path, row in depth2.items() if path.startswith("py_pkg"))
check("depth-2 rollup counts files below depth 2", buried_counted >= 7, f"{buried_counted} files")
check("linked directory skipped and reported",
      any("linked_pkg" in item for item in report["scan"]["skipped_links"])
      or "SKIPPED" in (tree / "MANIFEST.txt").read_text(encoding="utf-8"),
      ", ".join(report["scan"]["skipped_links"]))
check("no manifest is reported as unknown, not guessed",
      report["stack"] == ["unknown(no recognised manifest)"], "; ".join(report["stack"]))
calc_report = json.loads((repo / ".agent/audit.json").read_text(encoding="utf-8"))
check("stack detected from a manifest, direct deps only",
      any(item.startswith("python(pyproject") for item in calc_report["stack"]),
      "; ".join(calc_report["stack"]))
check("a lockfile never becomes a stack input",
      not any("lock" in item for item in calc_report["stack"]),
      "; ".join(calc_report["stack"]))
check("dead code is reported as candidates, never as fact",
      "candidates (unverified)" in out.stdout or not report["dead_code_candidates"],
      out.stdout.splitlines()[0])
check("an unreferenced helper is a candidate",
      any("orphan_helper" in item for item in report["dead_code_candidates"]),
      ", ".join(report["dead_code_candidates"][:4]))
check("a referenced helper is not a candidate",
      not any(item.endswith("::helper") for item in report["dead_code_candidates"]),
      ", ".join(report["dead_code_candidates"][:4]))

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
