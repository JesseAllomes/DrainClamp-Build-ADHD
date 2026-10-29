"""G7 new-project intake: card pre-fill, skip, impact split, triggers, caps, m0, writes."""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import dc_state  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail).splitlines()[0][:100] if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcintake-"))
home = tmp / "home"


def make(name, charter=None, state=True) -> Path:
    root = tmp / name
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    if state:
        (root / ".agent" / "drainclamp-state.md").write_text(dc_state.template_text(), encoding="utf-8")
    if charter is not None:
        (root / ".agent" / "charter.json").write_text(
            json.dumps({"schema": 1, "created": "2026-01-01T00:00:00Z", "charter": charter}), encoding="utf-8")
    return root


def run(root, *args):
    proc = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_project.py"), "--root", str(root),
                           "--home", str(home), "intake", *args], capture_output=True, text=True, encoding="utf-8")
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def charter_of(root):
    return json.loads((root / ".agent" / "charter.json").read_text(encoding="utf-8"))["charter"]


FULL = {"name": "Demo", "purpose": "Reconcile the weekly export", "stakeholders": "Finance team",
        "milestones": "Parse the export", "out_of_scope": "Reporting", "data": ["export.csv"],
        "security": "Nothing leaves the machine", "success": "Totals match on the sample file",
        "risks": [{"risk": "bad export", "likelihood": "Low", "impact": "Low", "guardrail": "manual"}]}
THIN = {"name": "Thin", "purpose": "Tidy the shared folder"}

# 1. well-described charter: card all filled, no questions
r = make("full", {**FULL, "success": ""}, state=True)
rc, out = run(r, "--tier", "build")
check("card has header + 7 lines", rc == 0 and len(out.splitlines()) == 8, out)
r = make("full2", FULL, state=False)
rc, out = run(r)
lines = out.splitlines()
check("filled charter: nothing open, straight to confirm",
      "nothing blocking open" in lines[0] and all(l.endswith("filled") for l in lines[1:]), out)
rc, out = run(r, "next")
check("next on a full card shows the final card, no question", "final card" in out and "Q1" not in out, out)
rc, out = run(r, "ok")
check("ok on a full card has no decisions to record", rc == 0 and "no open questions" in out, out)
rc, out = run(r)
check("after ok the project counts as resumed", out == "INTAKE: skip (resumed project)", out)

# 2. resumed project skip
r = make("resumed", FULL)
rc, out = run(r)
check("resumed project skips intake with the exact line", out == "INTAKE: skip (resumed project)", out)
r = make("nocharter", None)
rc, out = run(r)
check("state file with no charter is resumed", out == "INTAKE: skip (resumed project)", out)
r = make("fresh", None, state=False)
rc, out = run(r)
check("no state file is a new project", out.startswith("INTAKE: card") and len(out.splitlines()) == 8, out)

# 3. board-created charter: thin charter pre-fills what it has and asks the rest
r = make("board", THIN)
rc, out = run(r)
card = out.splitlines()
check("board charter: problem filled, others open", card[1].endswith("filled") and card[2].startswith("2 User: ?"), out)
check("card shows blocking count and estimate", "3 blocking open" in card[0] and "~3" in card[0], card[0])

# 4. Blocking before Shaping, one per turn, cue recompute
rc, out = run(r, "rec", "4", "the shared export folder")
rc, out = run(r, "next")
q = out.splitlines()
check("rec overrides the recommended default", "1) the shared export folder (recommended)" in out, out)
check("first question is the first Blocking item (Inputs)", q[0].startswith("Q1 of ~3 · Inputs:"), out)
check("turn is at most 3 lines with a recommended option",
      len(q) <= 3 and "1) " in q[0] and "(recommended)" in q[0] and "3) other" in q[0], out)
rc, out = run(r, "answer", "4", "the weekly export file on the shared drive; access held")
check("answer moves to the next Blocking item", out.startswith("Q2 of ~3 · Limits:"), out)
rc, out = run(r, "flag", "personal-data")
check("trigger queues a Blocking question and raises the estimate", "blocking question" in out and "~4" in out, out)
rc, out = run(r, "answer", "5", "no data leaves the machine")
check("card-order Blocking item comes before the queued trigger", out.startswith("Q3 of ~4 · Success:"), out)
rc, out = run(r, "answer", "6", "totals match on the sample file")
check("then the queued trigger question", out.startswith("Q4 of ~4 · Personal data:"), out)
rc, out = run(r, "answer", "x1", "keep it local")
check("no Blocking open: Shaping is not asked past the room, final card shown",
      "final card" in out and "Q5" not in out, out)
check("shaping items become assumed", "2 User: assumed:" in out and "7 Risk: assumed:" in out, out)
rc, out = run(r, "ok")
dec = out.splitlines()[1:]
check("ok prints DC:DECISIONS lines with sources",
      any(d.endswith("(user)") for d in dec) and any(d.endswith("(assumed)") for d in dec), out)
c = charter_of(r)
check("charter written through the schema", c["purpose"] == "Tidy the shared folder" and c["success"]
      and c["data"] and c["security"] and c["risks"], c)
check("trigger answers go to assumptions", "keep it local" in c["assumptions"], c.get("assumptions"))

# 5. default and rest
r = make("dflt", THIN)
run(r, "next")
rc, out = run(r, "default")
check("default records the recommended answer as assumed and moves on", out.startswith("Q2"), out)
rc, out = run(r, "rest")
check("rest accepts every remaining default and shows the final card",
      "final card" in out and "?" not in "\n".join(out.splitlines()[1:]), out)
rc, out = run(r, "ok")
check("rest answers are all assumed decisions", all(d.endswith("(assumed)") for d in out.splitlines()[1:]), out)
check("rest left the charter valid and complete", charter_of(r)["success"] and charter_of(r)["data"])

# 6. vague answer: re-asked once as pick-one, then default
r = make("vague", THIN)
run(r, "next")
rc, out = run(r, "answer", "4", "not sure")
check("vague Blocking answer is re-asked as a pick-one", out.startswith("Pick one") and "(recommended)" in out, out)
rc, out = run(r, "answer", "4", "idk")
check("second vague answer falls to an assumed default", out.startswith("Q2"), out)
rc, out = run(r, "rest")
rc, out = run(r, "ok")
check("vague item is recorded as assumed", any("Inputs" in d and d.endswith("(assumed)") for d in out.splitlines()), out)

# 7. cosmetic items are never asked
r = make("cos", THIN)
rc, out = run(r, "add", "which naming style for the helpers?", "--class", "cosmetic")
check("cosmetic is settled without a question", "not asked" in out and "repo" in out, out)
rc, out = run(r, "next")
check("cosmetic never becomes a question", "naming" not in out, out)

# 8. caps: 8 for build, with discovery m0 for unresolved Blocking and assumed Shaping
r = make("cap", THIN)
for i in range(1, 10):
    run(r, "add", f"extra blocking decision {i}", "--class", "blocking")
rc, out = run(r, "next")
n = 1
while out.startswith("Q") and n < 12:
    n = int(re.match(r"Q(\d+)", out).group(1))
    rc, out = run(r, "default")
check("build tier stops at 8 questions", n == 8 and not out.startswith("Q"), f"n={n}")
check("cap output names the m0 discovery milestone", "m0 discovery" in out and "cap 8" in out, out)
rc, out = run(r, "ok")
c = charter_of(r)
check("m0 discovery is the first milestone line", c["milestones"].startswith("m0 discovery"), c["milestones"])
check("shaping left over is recorded as assumed decisions", "(assumed)" in out, out)

# 9. quick tier: only Problem, MVP | Out, Success; cap 3
r = make("quick", None, state=False)
rc, out = run(r, "--tier", "quick")
check("quick card skips the other items", out.count("skipped (quick)") == 4, out)
rc, out = run(r, "next")
check("quick asks Problem first", out.startswith("Q1 of ~3 · Problem:"), out)
rc, out = run(r, "answer", "1", "convert the monthly list into one sheet")
check("quick asks MVP | Out next", out.startswith("Q2 of ~3 · MVP:"), out)
rc, out = run(r, "answer", "3", "one sheet | charts")
rc, out = run(r, "answer", "6", "the sheet opens and totals add up")
check("quick ends after 3 answers", "final card" in out, out)
rc, out = run(r, "ok")
c = charter_of(r)
check("quick writes problem, MVP, out of scope and success",
      c["purpose"] and c["milestones"] == "one sheet" and c["out_of_scope"] == "charts" and c["success"], c)

# 10. live-write trigger on build adds the pre-mortem as the last question, folded into risk
r = make("live", {**FULL, "success": ""})
rc, out = run(r, "flag", "live-write")
check("live-write trigger queues a Blocking question", "blocking question" in out, out)
rc, out = run(r, "next")
check("card-order Success is asked first", out.startswith("Q1") and "Success:" in out, out)
rc, out = run(r, "answer", "6", "totals match on the sample file")
check("trigger question next", "Live write:" in out, out)
rc, out = run(r, "answer", "x1", "test copy first")
check("pre-mortem line is the last question", "most likely caused it" in out and out.startswith("Q3"), out)
rc, out = run(r, "ok")
check("ok refused while the pre-mortem is open", rc != 0 and "still open" in out, out)
rc, out = run(r, "answer", "pm", "stale export data")
rc, out = run(r, "ok")
check("pre-mortem answer lands in risks", any(x["risk"] == "stale export data" for x in charter_of(r)["risks"]))

# 11. ok refused while Blocking items are open; existing charter command still works
r = make("open", THIN)
rc, out = run(r, "ok")
check("ok refused with Blocking open", rc != 0 and "still open" in out, out)
proc = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_project.py"), "--root", str(r),
                       "--home", str(home), "charter"], capture_output=True, text=True)
check("charter command unchanged", proc.stdout.startswith("CHARTER: Thin"), proc.stdout)

# 12. line discipline
check("question turn stays within 3 lines", len(run(make("lines", THIN), "next")[1].splitlines()) <= 3)

print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)
