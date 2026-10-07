"""Gate 6 end to end, replayed from a live run.

On 2026-10-07 Gate 6 ran for real against this fixture: two Sonnet critics, a Haiku
checker, a Sonnet refuter, a Sonnet fixer and a Sonnet re-check, all working from the
agent definitions in agents/. The fixture plants three defects (an off-by-one in
`window`, path traversal in `read_note`, an unchecked None in `admin_address`) and one
decoy (`chunks`, which only looks suspicious). Every agent output below is verbatim from
that run, so this suite re-proves the deterministic half without a model:

- every live citation survives the mechanical check (nothing hallucinated got through),
- the three planted defects are confirmed and the decoy is refuted,
- duplicates across critics merge (the live run is where the evidence-based merge rule
  came from: the critics labelled one defect "correctness" and "error-handling"),
- the fixer's real edits stay in scope, pass the fixture checks and clear the milestone,
- one stray edit is still caught as a SCOPE-BREACH.

Refuter verdicts are keyed by file:line rather than id, because ids depend on the merge
rule; the verdicts themselves are the live run's.
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
import dc_project  # noqa: E402
import dc_review  # noqa: E402
import dc_state  # noqa: E402
import dc_verify  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + str(detail) if detail else ''}")
    if not cond:
        fails.append(name)


TOOL = '''"""Small helpers for the notes service."""
import os

NOTES_DIR = "notes"


def window(items, size):
    """Every run of `size` consecutive items, in order."""
    out = []
    for i in range(len(items) - size):
        out.append(items[i:i + size])
    return out


def chunks(items, n):
    """Split into n-sized chunks; the last one may be shorter."""
    return [items[i:i + n] for i in range(0, len(items), n)]


def read_note(base, name):
    """Read the note called `name` from the notes folder under `base`.

    `name` comes straight from the HTTP query string.
    """
    path = os.path.join(base, NOTES_DIR, name)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def parse_port(text):
    """The port number in `text`, or None when it is not a number."""
    try:
        return int(text)
    except ValueError:
        return None


def admin_address(cfg):
    """The admin endpoint listens one port above the main service."""
    port = parse_port(cfg.get("port", ""))
    return ("localhost", port + 1)
'''
CHECKS = '''"""Fixture checks for tool.py."""
from tool import chunks, window

assert window([1, 2, 3, 4], 2)[0] == [1, 2]
assert chunks([1, 2, 3], 2) == [[1, 2], [3]]
print("check_tool: 2 passed")
'''

# -- verbatim agent outputs from the live run ------------------------------------------------
CRITIC_A = r'''{"severity": "high", "category": "correctness", "file": "tool.py", "line": 10, "symbol": "window", "claim": "range bound is off by one, so the last window is always dropped", "scenario": "window([1, 2, 3, 4], 2) returns [[1, 2], [2, 3]]; expected [[1, 2], [2, 3], [3, 4]]. window([1, 2], 2) returns [] instead of [[1, 2]]. The fixture check only asserts element [0], so it does not catch this.", "evidence": "    for i in range(len(items) - size):", "confidence": 0.95, "decision": ""}
{"severity": "high", "category": "correctness", "file": "tool.py", "line": 41, "symbol": "admin_address", "claim": "parse_port returns None for a missing or non-numeric port and admin_address adds 1 to it without checking, raising TypeError", "scenario": "admin_address({}) uses the default \"\", so parse_port returns None and None + 1 raises TypeError. admin_address({\"port\": \"abc\"}) fails the same way. The parse_port docstring promises None for a non-number, but this caller ignores that contract.", "evidence": "    return (\"localhost\", port + 1)", "confidence": 0.85, "decision": ""}
{"severity": "low", "category": "correctness", "file": "tool.py", "line": 17, "symbol": "chunks", "claim": "n <= 0 is not handled: range() with step 0 raises ValueError, and a negative step silently returns an empty list", "scenario": "chunks([1, 2, 3], 0) raises ValueError: range() arg 3 must not be zero. chunks([1, 2, 3], -1) returns [] and drops all the data.", "evidence": "    return [items[i:i + n] for i in range(0, len(items), n)]", "confidence": 0.3, "decision": ""}
{"severity": "low", "category": "correctness", "file": "tool.py", "line": 33, "symbol": "parse_port", "claim": "only ValueError is caught, and the result is not range-checked, so a non-string config value escapes and out-of-range numbers pass through as ports", "scenario": "parse_port(None) raises TypeError rather than returning None, which can happen when cfg has \"port\": null. parse_port(\"-5\") returns -5 and parse_port(\"70000\") returns 70000, so admin_address returns an invalid port.", "evidence": "        return int(text)", "confidence": 0.3, "decision": ""}'''
CRITIC_B = r'''{"severity": "critical", "category": "path-traversal", "file": "tool.py", "line": 25, "symbol": "read_note", "claim": "name from the HTTP query string is joined to the notes folder with no containment check, so '../' segments or an absolute path read any file the process can open; os.path.join discards the base entirely when name is absolute", "scenario": "read_note('/srv/app', '../../etc/passwd') opens /etc/passwd. read_note('/srv/app', '/etc/passwd') (or 'C:\\Windows\\win.ini' on Windows) ignores base and NOTES_DIR and reads that file. The contents are returned to the HTTP caller. D1 says notes live under <base>/notes, and this lets reads escape that folder.", "evidence": "path = os.path.join(base, NOTES_DIR, name)", "confidence": 0.9, "decision": "D1"}
{"severity": "medium", "category": "error-handling", "file": "tool.py", "line": 41, "symbol": "admin_address", "claim": "parse_port returns None for a missing or non-numeric port, and admin_address adds 1 to it without checking, raising an unhandled TypeError. A malformed or absent config value crashes the caller instead of being rejected. Port range is also unchecked: 65535 gives the invalid port 65536.", "scenario": "admin_address({}) calls parse_port(''), gets None, and evaluates None + 1, raising TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'. admin_address({'port': '65535'}) returns ('localhost', 65536).", "evidence": "return (\"localhost\", port + 1)", "confidence": 0.7, "decision": ""}'''
CHECKER = r'''{"severity": "high", "category": "conformance", "file": "tool.py", "line": 25, "symbol": "read_note", "claim": "read_note does not confine notes to <base>/notes per D1; name parameter lacks validation", "scenario": "D1 specifies 'notes live under <base>/notes' but implementation allows path traversal via unsanitized name from HTTP query string", "evidence": "path = os.path.join(base, NOTES_DIR, name)", "confidence": 0.95, "decision": "D1"}
{"severity": "medium", "category": "test-gap", "file": "tool.py", "line": 35, "symbol": "parse_port", "claim": "except ValueError branch has no test", "scenario": "parse_port handles ValueError by returning None, but check_tool.py does not test this error path with non-numeric input", "evidence": "return None", "confidence": 0.95, "decision": ""}
{"severity": "medium", "category": "test-gap", "file": "tool.py", "line": 41, "symbol": "admin_address", "claim": "admin_address is completely untested; implicit error path when port is None", "scenario": "admin_address not imported or called by check_tool.py; when parse_port returns None, port + 1 at line 41 raises TypeError", "evidence": "return (\"localhost\", port + 1)", "confidence": 0.95, "decision": ""}'''
REFUTER = {  # (file, line) -> (verdict, note), from the live refuter
    ("tool.py", 10): ("CONFIRMED", "tool.py:10 uses range(len(items) - size), so window([1,2,3,4],2) yields i=0,1 only and drops [3,4]"),
    ("tool.py", 41): ("CONFIRMED", "tool.py:40 gets None from parse_port(\"\") and tool.py:41 evaluates None + 1"),
    ("tool.py", 17): ("REFUTED", "The only caller (check_tool.py:5) passes n=2; no reachable input breaks the contract"),
    ("tool.py", 33): ("REFUTED", "parse_port is documented at tool.py:31 as taking text; the claimed failures are not traced"),
    ("tool.py", 25): ("CONFIRMED", "tool.py:25 passes the raw query-string name to os.path.join with no containment check"),
    ("tool.py", 35): ("CONFIRMED", "check_tool.py imports only chunks and window, so the except ValueError branch is untested"),
}
RECHECK_RESOLVED = ("tool.py", 10), ("tool.py", 41), ("tool.py", 25), ("tool.py", 35)

# The fixer's edits, verbatim.
TOOL_FIXED = TOOL.replace("    for i in range(len(items) - size):", "    for i in range(len(items) - size + 1):").replace(
    "    path = os.path.join(base, NOTES_DIR, name)\n",
    "    folder = os.path.realpath(os.path.join(base, NOTES_DIR))\n"
    "    path = os.path.realpath(os.path.join(folder, name))\n"
    "    if os.path.commonpath([folder, path]) != folder:\n"
    "        raise ValueError(\"note name escapes the notes folder\")\n").replace(
    "    port = parse_port(cfg.get(\"port\", \"\"))\n",
    "    port = parse_port(cfg.get(\"port\", \"\"))\n"
    "    if port is None:\n"
    "        raise ValueError(\"config 'port' must be a number\")\n")
CHECKS_FIXED = '''"""Fixture checks for tool.py."""
from tool import admin_address, chunks, parse_port, read_note, window

assert window([1, 2, 3, 4], 2)[0] == [1, 2]
assert chunks([1, 2, 3], 2) == [[1, 2], [3]]
try:
    admin_address({})
except ValueError:
    pass
else:
    raise AssertionError("admin_address({}) must raise ValueError")
assert admin_address({"port": "8080"}) == ("localhost", 8081)
for bad in ("../secret.txt", "/etc/passwd"):
    try:
        read_note(".", bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"read_note accepted {bad!r}")
assert window([1, 2, 3, 4], 2) == [[1, 2], [2, 3], [3, 4]]
assert window([1, 2], 2) == [[1, 2]]
assert parse_port("abc") is None
assert parse_port("8080") == 8080
print("check_tool: 2 passed")
'''

# -- the fixture repository ----------------------------------------------------------------
root = Path(tempfile.mkdtemp(prefix="dce2e-")) / "repo"
(root / ".agent").mkdir(parents=True)
for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
(root / "README.md").write_text("Fixture for Gate 6 e2e.\n", encoding="utf-8")
(root / ".gitignore").write_text("__pycache__/\n.agent/\n", encoding="utf-8")
subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True)
(root / "tool.py").write_text(TOOL, encoding="utf-8", newline="\n")
(root / "check_tool.py").write_text(CHECKS, encoding="utf-8", newline="\n")
state = dc_state.template_text().replace(
    "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
    "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n"
    "| m1 | notes helpers: window, chunks, read_note, admin_address | tool.py;check_tool.py | active |\n")
state = state.replace("<!-- DC:DECISIONS -->\n", "<!-- DC:DECISIONS -->\n"
                      "- Notes storage -> notes live under <base>/notes; names are user-supplied (user)\n")
(root / ".agent" / "drainclamp-state.md").write_text(state, encoding="utf-8")


def green():
    dc_verify.save_record(root / ".agent", dc_verify.TIER_RECORD_PREFIX + "milestone", "tier", "pass",
                          "verify.log", dc_verify.tree_fingerprint(root))


def rv(*args, stdin=None):
    p = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_review.py"), "--root", str(root), *args],
                       capture_output=True, text=True, input=stdin)
    return p.returncode, p.stdout + p.stderr


def findings():
    return dc_review.review_of(dc_project.load(root / ".agent"))["findings"]


def at(file, line):
    return next(f for f in findings() if f["file"] == file and f["line"] == line)


rv("config", "--mode", "milestone")
green()
rc, out = rv("packet", "--milestone", "m1", "--depth", "final")
check("final packet built over the fixture", rc == 0 and "R1" in out and "callers 3" in out, out)
for role, model in (("critic-a", "sonnet/high"), ("critic-b", "sonnet/high"), ("checker", "haiku/medium"),
                    ("refuter", "sonnet/high")):
    rv("run", "--round", "R1", "--role", role, "--model", model)

outs = {}
for role, text in (("critic-a", CRITIC_A), ("critic-b", CRITIC_B), ("checker", CHECKER)):
    outs[role] = rv("ingest", "--round", "R1", "--role", role, "--file", "-", stdin=text)[1]
check("every live citation passes the mechanical check", all("0 rejected" in o for o in outs.values()), outs)
check("critic B's admin_address finding merges into critic A's", "1 merged" in outs["critic-b"], outs["critic-b"])
check("checker: read_note and admin_address merge, the test gap is new",
      "2 merged" in outs["checker"] and "1 new" in outs["checker"], outs["checker"])
check("six distinct candidates from nine raw findings", len(findings()) == 6, len(findings()))
check("the merged admin_address finding names all three finders",
      sorted(at("tool.py", 41)["found_by"]) == ["checker", "critic-a", "critic-b"], at("tool.py", 41)["found_by"])
check("read_note is flagged against decision D1", "vs D1" in at("tool.py", 25)["flags"], at("tool.py", 25)["flags"])

verdicts = "\n".join(json.dumps({"id": f["id"], "verdict": REFUTER[(f["file"], f["line"])][0],
                                 "note": REFUTER[(f["file"], f["line"])][1]}) for f in findings())
rc, out = rv("adjudicate", "--round", "R1", "--file", "-", stdin=verdicts)
planted = [at("tool.py", 10), at("tool.py", 25), at("tool.py", 41)]
check("all three planted defects confirmed", all(f["status"] == "open" for f in planted), [f["status"] for f in planted])
check("the decoy (chunks) is refuted", at("tool.py", 17)["status"] == "refuted")
code, out = rv("check", "--milestone", "m1")
check("the milestone is blocked by four open findings", code == 2 and "FINDINGS 4" in out, out)

ids = {k: at(*k)["id"] for k in RECHECK_RESOLVED}
rv("decide", *sum((["--set", f"{i}=fix"] for i in ids.values()), []))
for (file, line), fid in ids.items():
    allow = "check_tool.py" if line == 35 else "tool.py;check_tool.py"
    rv("ticket", "--round", "R1", "--id", fid, "--allow", allow, "--file", "-", stdin=f"TICKET {fid}\n")
rv("run", "--round", "R1", "--role", "fixer", "--model", "sonnet/medium")
(root / "tool.py").write_text(TOOL_FIXED, encoding="utf-8", newline="\n")
(root / "check_tool.py").write_text(CHECKS_FIXED, encoding="utf-8", newline="\n")

(root / "README.md").write_text("Fixture for Gate 6 e2e.\nstray\n", encoding="utf-8")
rc, out = rv("scope", "--round", "R1")
check("a stray edit beside the real fix is a SCOPE-BREACH", rc == 4 and "README.md" in out, out)
(root / "README.md").write_text("Fixture for Gate 6 e2e.\n", encoding="utf-8")
rc, out = rv("scope", "--round", "R1")
check("the fixer's real edits are in scope", rc == 0 and "2 file(s) changed, all inside tickets" in out, out)

p = subprocess.run([sys.executable, "-B", "check_tool.py"], cwd=root, capture_output=True, text=True)
check("the fixed fixture passes its checks, including the new ones", p.returncode == 0, p.stdout + p.stderr)

rv("run", "--round", "R1", "--role", "recheck", "--model", "sonnet/high")
rc, out = rv("run", "--round", "R1", "--role", "recheck", "--model", "sonnet/high")
check("the seventh run is refused", rc == 2 and "REVIEW CAP" in out, out)
for fid in ids.values():
    rv("resolve", "--id", fid, "--fixed", "--note", "re-check RESOLVED")
rc, out = rv("finish", "--round", "R1")
check("the round finishes as REVIEW-PASS", rc == 0 and "REVIEW-PASS" in out, out)

rm = Path(tempfile.mkdtemp()) / "rm.md"
rm.write_text("| id | goal | files | status |\n|---|---|---|---|\n"
              "| m1 | notes helpers: window, chunks, read_note, admin_address | tool.py;check_tool.py | done |\n",
              encoding="utf-8")
p = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_state.py"), "--root", str(root),
                    "--set", "ROADMAP", "--file", str(rm)], capture_output=True, text=True)
check("the milestone closes once the review passes", p.returncode == 0, p.stdout + p.stderr)
prec = dc_review.precision(dc_review.review_of(dc_project.load(root / ".agent")))
check("precision is logged per role", prec.get("critic-a") == {"found": 4, "upheld": 2}
      and prec.get("critic-b") == {"found": 2, "upheld": 2}, prec)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
