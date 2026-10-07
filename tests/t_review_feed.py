"""The review block project-board reads: field names and types are a contract.

project-board renders the "Adversarial review" section straight from the sidecar's
`review` key, read-only. A renamed field there fails silently in a browser, so the
shape is pinned here, on the writing side.
"""
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
import dc_verify  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + str(detail) if detail else ''}")
    if not cond:
        fails.append(name)


root = Path(tempfile.mkdtemp(prefix="dcfeed-")) / "repo"
(root / ".agent").mkdir(parents=True)
for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
state = dc_state.template_text().replace(
    "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
    "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n| m1 | g | a.py | active |\n")
(root / ".agent" / "drainclamp-state.md").write_text(state, encoding="utf-8")
(root / "a.py").write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
dc_verify.save_record(root / ".agent", dc_verify.TIER_RECORD_PREFIX + "milestone", "tier", "pass",
                      "verify.log", dc_verify.tree_fingerprint(root))


def rv(*args, stdin=None):
    p = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_review.py"), "--root", str(root), *args],
                       capture_output=True, text=True, input=stdin)
    return p.returncode, p.stdout + p.stderr


rv("config", "--mode", "milestone")
rv("packet", "--milestone", "m1")
rv("run", "--round", "R1", "--role", "critic-a", "--model", "sonnet/high")
rv("run", "--round", "R1", "--role", "refuter", "--model", "sonnet/high")
rv("ingest", "--round", "R1", "--role", "critic-a", "--file", "-", stdin=json.dumps(
    {"severity": "medium", "category": "correctness", "file": "a.py", "line": 2, "symbol": "f",
     "claim": "no overflow guard", "scenario": "f(huge) -> wraps", "evidence": "return x + 1"}))
rv("adjudicate", "--round", "R1", "--file", "-", stdin=json.dumps({"id": "r1", "verdict": "CONFIRMED", "note": "a.py:2"}))
rv("decide", "--set", "r1=waive:python ints do not overflow; accepted")
rc, out = rv("finish", "--round", "R1")
check("the feed fixture reaches a finished round", rc == 0, out)

side = json.loads((root / ".agent" / "drainclamp-project.json").read_text(encoding="utf-8"))
review = side.get("review")
check("sidecar carries a review block", isinstance(review, dict))
review = review or {}
check("config.mode is a string the board can show", isinstance((review.get("config") or {}).get("mode"), str))
ROUND = {"id": str, "milestone": str, "depth": str, "verdict": str, "coverage": str, "finished": str,
         "runs": list}
FINDING = {"id": str, "round": str, "milestone": str, "severity": str, "category": str, "file": str,
           "line": int, "claim": str, "scenario": str, "status": str, "reason": str, "found_by": list,
           "flags": list}
for r in review.get("rounds", []):
    for key, kind in ROUND.items():
        check(f"round field {key} is {kind.__name__}", isinstance(r.get(key), kind), r.get(key))
for f in review.get("findings", []):
    for key, kind in FINDING.items():
        check(f"finding field {key} is {kind.__name__}", isinstance(f.get(key), kind), f.get(key))
check("statuses are from the documented set",
      all(f["status"] in ("candidate", "open", "fixing", "fixed", "waived", "dismissed", "refuted", "withdrawn")
          for f in review.get("findings", [])))

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
