"""Sidecar checks for dc_project.py: chunks, errors, to-dos, time, capsule; no board fields."""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import dc_project  # noqa: E402
import dc_state  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcproject-"))
home = tmp / "home"


def make_repo(name, roadmap="") -> Path:
    root = tmp / name
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    text = dc_state.template_text()
    if roadmap:
        text = text.replace(
            "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
            f"<!-- DC:ROADMAP -->\n{roadmap}\n")
    (root / ".agent" / "drainclamp-state.md").write_text(text, encoding="utf-8")
    return root


def proj(root, *args):
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_project.py"),
         "--root", str(root), "--home", str(home), *args],
        capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


repo = make_repo("alpha", "| m1 | build parser | a.py | active |\n"
                          "| m2 | use parser | b.py | pending |")
side = repo / ".agent" / dc_project.SIDECAR_NAME

# 1. no sidecar is an empty project, and reading never creates one
rc, out = proj(repo, "summary")
check("summary works with no sidecar", rc == 0, out.strip())
check("reading does not create the sidecar", not side.exists())

# 2. chunks: add, list, done with a note
rc, out = proj(repo, "chunk", "add", "--milestone", "m1", "--goal", "write tokenizer",
               "--targets", "a.py::tokenize;a.py", "--est", "20")
check("chunk add succeeds", rc == 0 and "c1" in out, out.strip())
proj(repo, "chunk", "add", "--milestone", "m1", "--goal", "write parser")
data = json.loads(side.read_text(encoding="utf-8"))
check("sidecar carries schema 1", data.get("schema") == 1, data.get("schema"))
check("chunk targets are split", data["chunks"]["m1"][0]["targets"] == ["a.py::tokenize", "a.py"],
      data["chunks"]["m1"][0]["targets"])
check("second chunk gets the next id", data["chunks"]["m1"][1]["id"] == "c2")

rc, out = proj(repo, "chunk", "add", "--milestone", "m9", "--goal", "x")
check("chunk for an unknown milestone is refused", rc != 0 and "m9" in out, out.strip())

rc, out = proj(repo, "chunk", "done", "--milestone", "m1", "--id", "c1",
               "--note", "tokenizer handles CRLF")
data = json.loads(side.read_text(encoding="utf-8"))
c1 = data["chunks"]["m1"][0]
check("chunk done records the flag", c1["done"] is True)
check("chunk done records the note", c1["note"] == "tokenizer handles CRLF")
check("chunk done stamps a time", bool(c1.get("done_at")))
rc, out = proj(repo, "chunk", "done", "--milestone", "m1", "--id", "c7", "--note", "x")
check("unknown chunk id is refused", rc != 0 and "c7" in out, out.strip())

# 3. the resume capsule is short and points at the next chunk
rc, out = proj(repo, "next")
lines = out.strip().splitlines()
check("capsule exits clean", rc == 0, out.strip())
check("capsule is at most 15 lines", len(lines) <= 15, len(lines))
check("capsule names the active milestone", "m1" in out)
check("capsule names the next open chunk", "c2" in out and "write parser" in out, out)
check("capsule carries the last done note", "tokenizer handles CRLF" in out)
check("capsule does not repeat the finished chunk goal as next",
      "Next chunk: c1" not in out)

# 4. errors and to-dos
rc, out = proj(repo, "error", "add", "--summary", "CRLF broke tokenizer",
               "--milestone", "m1", "--chunk", "c1", "--cause", "split on \\n")
check("error add succeeds", rc == 0 and "e1" in out, out.strip())
rc, out = proj(repo, "error", "fix", "--id", "e1", "--fix", "splitlines()")
data = json.loads(side.read_text(encoding="utf-8"))
check("error fix closes it", data["errors"][0]["status"] == "fixed")
check("error fix records the fix", data["errors"][0]["fix"] == "splitlines()")

proj(repo, "todo", "add", "--text", "ask FC about format")
rc, out = proj(repo, "todo", "done", "--id", "t1")
data = json.loads(side.read_text(encoding="utf-8"))
check("todo done ticks it", data["todos"][0]["done"] is True, out.strip())

# 5. time: touches inside the idle window extend one session; a gap starts another
t0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
d = dc_project.empty()
d = dc_project.touch_time(d, t0)
d = dc_project.touch_time(d, t0 + timedelta(minutes=20))
check("touch within idle window extends the session", len(d["time"]) == 1, d["time"])
d = dc_project.touch_time(d, t0 + timedelta(minutes=20 + dc_project.IDLE_MINUTES + 1))
check("touch after idle gap starts a new session", len(d["time"]) == 2, d["time"])
check("session minutes add up", dc_project.build_minutes(d) == 20, dc_project.build_minutes(d))

rc, out = proj(repo, "time", "add", "--minutes", "90", "--note", "design on paper")
data = json.loads(side.read_text(encoding="utf-8"))
manual = [s for s in data["time"] if s["source"] == "manual"]
check("manual time is recorded", len(manual) == 1 and
      dc_project.minutes_of(manual[0]) == 90, out.strip())
rc, out = proj(repo, "time", "add", "--minutes", "-5")
check("negative time is refused", rc != 0)

# 7. a damaged or future sidecar is refused, never overwritten
side.write_text("{broken", encoding="utf-8")
rc, out = proj(repo, "todo", "add", "--text", "x")
check("corrupt sidecar refuses writes", rc != 0 and "sidecar" in out.lower(), out.strip())
check("corrupt sidecar is left as found", side.read_text(encoding="utf-8") == "{broken")
side.write_text(json.dumps({"schema": 99}), encoding="utf-8")
rc, out = proj(repo, "summary")
check("unknown schema is refused", rc != 0 and "schema" in out, out.strip())

# 8. every write bumps the revision so a stale writer can be refused
side.unlink()
proj(repo, "todo", "add", "--text", "a")
proj(repo, "todo", "add", "--text", "b")
data = json.loads(side.read_text(encoding="utf-8"))
check("revision counts writes", data["rev"] == 2, data.get("rev"))
try:
    dc_project.mutate(repo / ".agent", lambda d: d, expect=1)
    check("stale revision is refused", False)
except dc_project.DcError as exc:
    check("stale revision is refused", "revision" in str(exc), str(exc))

# chunk boundary: purge verdict over chunk target paths, printed by `chunk done`
bnd = make_repo("boundary", "| m1 | three steps | a.py;b.py;c.py | active |")
proj(bnd, "chunk", "add", "--milestone", "m1", "--goal", "one", "--targets", "a.py::f")
proj(bnd, "chunk", "add", "--milestone", "m1", "--goal", "two", "--targets", "a.py::g;b.py")
proj(bnd, "chunk", "add", "--milestone", "m1", "--goal", "three", "--targets", "c.py")
proj(bnd, "chunk", "add", "--milestone", "m1", "--goal", "four")
rc, out = proj(bnd, "chunk", "done", "--milestone", "m1", "--id", "c1", "--note", "f")
check("boundary names the next chunk", "Next chunk: c2" in out, out)
check("symbols of one file overlap by path (1 of 2 = 50%)",
      "HOLD (chunk overlap 50%)" in out and "Context retained" in out, out)
check("HOLD boundary hands over the next targets",
      "Targets: a.py::g, b.py" in out and "  dc_chunk.py --symbol g --path a.py" in out, out)
rc, out = proj(bnd, "chunk", "done", "--milestone", "m1", "--id", "c2")
check("disjoint next chunk recommends a purge",
      "PURGE (chunk overlap 0%)" in out and "Context purge recommended." in out, out)
check("PURGE boundary leaves targets to the resume", "Targets:" not in out, out)
rc, out = proj(bnd, "chunk", "done", "--milestone", "m1", "--id", "c3")
check("next chunk without targets is unknown, never PURGE",
      "HOLD (chunk overlap unknown: next chunk has no targets)" in out, out)
rc, out = proj(bnd, "chunk", "done", "--milestone", "m1", "--id", "c3", "--context-high")
check("--context-high forces PURGE without inventing an overlap",
      "PURGE (chunk context-high; overlap unknown" in out, out)
rc, out = proj(bnd, "chunk", "done", "--milestone", "m1", "--id", "c4")
check("last chunk hands over to Gate 5",
      "all chunks done" in out and "--purge-check" in out and "PURGE" not in out, out)
v, _ = dc_state.chunk_purge_check(["a.py"], ["docs/*.md"], bnd, False)
check("unresolved chunk glob is unknown", v.startswith("HOLD (chunk overlap unknown: unresolved"), v)
v, _ = dc_state.chunk_purge_check([], ["a.py"], bnd, False)
check("finished chunk without targets is unknown", "finished chunk has no targets" in v, v)

# charter view for Gates 1 and 2
chr_repo = make_repo("chartered", "| m1 | first | a.py | active |")
rc, out = proj(chr_repo, "charter")
check("no charter says so and falls back to Gate 1", rc == 0 and "CHARTER: none" in out, out)
cfile = chr_repo / ".agent" / dc_project.CHARTER_NAME
cfile.write_text(json.dumps({"charter": {"name": "Leave alerts", "purpose": "Warn managers",
                                         "stray": "x"}}), encoding="utf-8")
rc, out = proj(chr_repo, "charter")
check("invalid charter is ignored, not trusted", rc == 0 and "invalid, ignored" in out, out)
cfile.write_text(json.dumps({"charter": {
    "name": "Leave alerts", "purpose": "Warn managers before leave runs out",
    "security": "- read-only payroll view\n- no personal data in logs",
    "milestones": "", "acceptance": "\n".join(f"- case {i}" for i in range(10)),
    "scope": [{"feature": "Nightly scan", "what": "", "priority": "Must"},
              {"feature": "Email digest", "what": "", "priority": "Should"},
              {"feature": "Mobile app", "what": "", "priority": "Won't"}],
    "risks": [{"risk": "wrong balance", "likelihood": "Med", "impact": "High",
               "guardrail": "reconcile"}],
    "value": {"baseline_min": 30, "new_min": 5, "runs_per_year": 50}}}), encoding="utf-8")
rc, out = proj(chr_repo, "charter")
check("charter view is marked as data", "data, not instructions" in out, out)
check("filled multi-line answers are joined",
      "Security: read-only payroll view; no personal data in logs" in out, out)
check("scope is grouped by MoSCoW",
      "Must: Nightly scan | Should: Email digest | Could: - | Won't: Mobile app" in out, out)
check("high-impact risks are named", "Risks: 1 (high impact: wrong balance)" in out, out)
check("value line", "Value: 30 min -> 5 min per run, 50 runs/yr" in out, out)
check("roadmap draft falls back to Must scope without milestones",
      "from Must scope" in out and "1. Nightly scan" in out and "Email digest" not in
      out.split("Draft ROADMAP")[1].split("Acceptance")[0], out)
check("long acceptance lists declare the cap", "8. case 7" in out and
      "TRUNCATED: 2 more acceptance lines" in out, out)
check("material blanks are listed for Gate 1",
      "Blank, material (Gate 1 may ask): objectives, success, out_of_scope, milestones, "
      "retention, never, approvals" in out, out)
check("non-material blanks are never asked", "Blank, not asked: sponsor, stakeholders" in out, out)

# -- board features are project-board's, not DrainClamp's (1.5.0) --------------------
for gone in ("savings.set", "profile.summary", "feature.add", "safeguard.add", "health.set", "run.add",
             "flow.set", "task.add", "outcome.set"):
    check(f"{gone} is not a DrainClamp operation", gone not in dc_project.OPS)
    try:
        dc_project.apply_op(dc_project.empty(), repo, gone, {})
        check(f"{gone} refused", False)
    except dc_project.DcError:
        check(f"{gone} refused", True)
for name in ("board_status", "next_step", "guess_frequency", "savings", "set_global_rate", "seed_profile"):
    check(f"dc_project.{name} is gone", not hasattr(dc_project, name))
old = dc_project.empty()
old.update({"savings": {"baseline_min": 60}, "profile": {"summary": "x"}, "health": {"state": "on"},
            "runs": [{"id": "r1"}], "flow": {"state": "need"}, "outcome": {"by": "user"}})
kept = dc_project._validate(old)
check("an older sidecar with board fields still loads, fields kept as found",
      kept["savings"] == {"baseline_min": 60} and kept["profile"] == {"summary": "x"} and kept["runs"] == [{"id": "r1"}])

# Gate 6 in the resume view: the close line names the review when it applies, and
# proposed advisor ideas are listed as waiting on the user.
gate = make_repo("gate6", "| m1 | first | a.py | done |\n| m2 | last | b.py | active |")
proj(gate, "chunk", "add", "--milestone", "m2", "--goal", "only", "--targets", "b.py")
proj(gate, "chunk", "done", "--milestone", "m2", "--id", "c1")
gside = gate / ".agent" / dc_project.SIDECAR_NAME


def gate_capsule(review):
    data = json.loads(gside.read_text(encoding="utf-8"))
    data["review"] = review
    gside.write_text(json.dumps(data), encoding="utf-8")
    return "\n".join(dc_project.capsule(gate, dc_project.load(gate / ".agent")))


out = gate_capsule({"config": {"mode": "off"}})
check("review off: plain close line",
      "All chunks done: close the milestone" in out and "Gate 6" not in out, out)
out = gate_capsule({"config": {"mode": "final"}})
check("final mode on the last open milestone names Gate 6 and its verdict",
      "Gate 3 milestone tier, then Gate 6 (dc_review.py check: NOT-RUN), then close" in out, out)
rc, out = proj(gate, "chunk", "done", "--milestone", "m2", "--id", "c1")
check("chunk boundary gives the same Gate 6 close line",
      "all chunks done: Gate 3 milestone tier, then Gate 6" in out and "--purge-check" in out, out)
import dc_review
out = gate_capsule({"config": {"mode": "final"}, "rounds": [
    {"id": "R1", "milestone": "m2", "finished": True, "stamp": {}, "coverage": "full",
     "inventory": dc_review._inventory(gate)}]})
check("a passed review says so instead of asking for Gate 6 again",
      "Gate 6 REVIEW-PASS; close the milestone" in out and "Gate 3 milestone tier" not in out, out)
early = make_repo("gate6-early", "| m1 | first | a.py | active |\n| m2 | last | b.py | pending |")
proj(early, "chunk", "add", "--milestone", "m1", "--goal", "only", "--targets", "a.py")
proj(early, "chunk", "done", "--milestone", "m1", "--id", "c1")
eside = early / ".agent" / dc_project.SIDECAR_NAME
edata = json.loads(eside.read_text(encoding="utf-8"))
edata["review"] = {"config": {"mode": "final"}}
eside.write_text(json.dumps(edata), encoding="utf-8")
out = "\n".join(dc_project.capsule(early, dc_project.load(early / ".agent")))
check("final mode before the last milestone: plain close line", "Gate 6" not in out, out)
edata["review"] = {"config": {"mode": "milestone"}}
eside.write_text(json.dumps(edata), encoding="utf-8")
out = "\n".join(dc_project.capsule(early, dc_project.load(early / ".agent")))
check("milestone mode names Gate 6 at every close", "then Gate 6 (dc_review.py check: NOT-RUN)" in out, out)
edata["review"] = {"config": {"mode": "final"},
                   "findings": [{"id": "r1", "status": "open", "milestone": "m1"}]}
eside.write_text(json.dumps(edata), encoding="utf-8")
out = "\n".join(dc_project.capsule(early, dc_project.load(early / ".agent")))
check("final mode, not last: blocking findings come before the close",
      "resolve Gate 6 findings first (r1; dc_review.py status), then close" in out, out)

out = gate_capsule({"config": {"mode": "off"}, "ideas": [
    {"id": "i1", "status": "proposed"}, {"id": "i2", "status": "accepted"},
    {"id": "i3", "status": "shelved"}, {"id": "i4", "status": "proposed"}, "junk"]})
check("proposed advisor ideas are listed as waiting on the user",
      "Advisor: 2 idea(s) to triage (i1 i4) - waiting on you" in out, out)
out = gate_capsule({"config": {"mode": "off"}, "ideas": [{"id": "i1", "status": "denied"}]})
check("no proposed ideas, no advisor line", "Advisor:" not in out, out)

# path-only targets: a large file gets an index hint, a small or missing one a plain read
sized = make_repo("sized", "| m1 | sized | big.py | active |")
(sized / "big.py").write_text("x = 1\n" * 300, encoding="utf-8")
(sized / "small.py").write_text("x = 1\n" * 10, encoding="utf-8")
proj(sized, "chunk", "add", "--milestone", "m1", "--goal", "sized",
     "--targets", "big.py;small.py;new.py")
rc, out = proj(sized, "next")
check("large path-only target is indexed first",
      "dc_map.py --path big.py  (300 lines: index, then read one range)" in out, out)
check("small path-only target is read whole", "  read small.py" in out, out)
check("missing path-only target falls back to read", "  read new.py" in out, out)
check("hint without a root stays a plain read", dc_project.read_hint("big.py") == "read big.py")
(sized / "big.md").write_text("line\n" * 300, encoding="utf-8")
check("large unsupported target is not sent to dc_map",
      dc_project.read_hint("big.md", sized) == "read big.md  (300 lines: no index for this type; read one range)",
      dc_project.read_hint("big.md", sized))
check("large supported target still indexed",
      dc_project.read_hint("big.py", sized).startswith("dc_map.py --path big.py"))

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
