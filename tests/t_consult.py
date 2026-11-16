"""Advisor gate checks: consult briefs, counsel replies, the trial log, problem triggers and the
DC:DECISIONS write gate."""
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
import dc_state  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcconsult-"))
ROADMAP = ("| id | goal | files | status | deps |\n|---|---|---|---|---|\n"
           "| m1 | add the parser | a.py | active | |")
A_PY = "".join(f"LINE_{n} = {n}\n" for n in range(1, 101))


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def make_repo(name) -> Path:
    root = tmp / name
    (root / ".agent").mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    text = dc_state.template_text().replace(
        "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
        f"<!-- DC:ROADMAP -->\n{ROADMAP}\n")
    text = text.replace("<!-- DC:DECISIONS -->\n", "<!-- DC:DECISIONS -->\n"
                        "- Parser output format -> totals printed as JSON lines (user)\n")
    (root / ".agent" / "drainclamp-state.md").write_text(text, encoding="utf-8")
    (root / "a.py").write_text(A_PY, encoding="utf-8", newline="\n")
    return root


def rv(root, *args, stdin=None):
    p = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_review.py"), "--root", str(root), *args],
                       capture_output=True, text=True, input=stdin)
    return p.returncode, p.stdout + p.stderr


def consults(root):
    return (dc_project.load(root / ".agent").get("review") or {}).get("consults") or []


def direction(root, *extra):
    return rv(root, "consult", "--kind", "direction", "--question", "Which parser output format to use?",
              "--option", "JSON lines", "--option", "CSV rows", *extra)


# -- c1: consult briefs --------------------------------------------------------------
root = make_repo("brief")
rc, out = direction(root, "--lines", "a.py:3-5")
check("direction consult runs with review mode off", rc == 0 and "CONSULT V1 direction m1/milestone (1/2)" in out, out)
brief = (root / ".agent" / "review" / "V1" / "brief.md").read_text(encoding="utf-8")
check("options listed neutrally as O1/O2", "O1: JSON lines" in brief and "O2: CSV rows" in brief
      and "not a preference" in brief, brief)
check("related standing decision shown", "D1: - Parser output format" in brief, brief)
check("code range numbered, only the range", "    3 | LINE_3 = 3" in brief and "LINE_6" not in brief, brief)
check("brief carries its own output contract", '{"pick": "O<n>"' in brief and "NOT a Gate 6" in brief, brief)
check("spawn line names dca-advisor and counsel", "spawn dca-advisor once" in out and "counsel --consult V1" in out, out)
rec = consults(root)[0]
check("record stored with nothing answered yet", rec["kind"] == "direction" and rec["reply"] is None
      and rec["options"] == ["JSON lines", "CSV rows"], rec)

rc, out = direction(root, "--lines", "a.py")
check("whole file refused, range required", rc != 0 and "never a whole file" in out, out)
rc, out = rv(root, "consult", "--kind", "direction", "--question", "Which one?", "--option", "a", "--lines", "a.py:1-80")
check("one option refused", rc != 0 and "2-6 --option" in out, out)
rc, out = direction(root, "--lines", "a.py:1-80")
check("range over the cap refused", rc != 0 and "cap 60" in out, out)

rc, out = direction(root)
check("second consult in the bucket allowed", rc == 0 and "(2/2)" in out, out)
rc, out = direction(root)
check("third consult refused without the user", rc != 0 and "ask the user" in out, out)
rc, out = direction(root, "--user-ok", "user said go ahead")
check("third consult runs with --user-ok", rc == 0 and "CONSULT V3" in out, out)
check("user_ok recorded", consults(root)[-1]["user_ok"] == "user said go ahead", consults(root)[-1])

dc_project.mutate(root / ".agent", lambda d: (dc_project.apply_op(
    d, root, "chunk.add", {"milestone": "m1", "goal": "build the parser"}), d)[1])
rc, out = direction(root)
check("bucket follows the current chunk", rc == 0 and "m1/c1 (1/2)" in out, out)

rc, out = direction(root, "--no-subagents")
check("no-subagent host prints SKIP and records nothing", rc == 0 and out.startswith("CONSULT SKIP")
      and len(consults(root)) == 4, out)

(root / ".agent" / "verify.log").write_text(
    "# dc_verify milestone x\n\n== unit [FAIL] ['pytest'] (cwd .)\n   test_a failed\nE assert 1 == 2\n\n"
    "== lint [PASS] ['ruff'] (cwd .)\n", encoding="utf-8")
rc, out = rv(root, "consult", "--kind", "problem", "--check", "unit", "--attempt", "fixed the import: same failure",
             "--attempt", "pinned the version: same failure")
pb = (root / ".agent" / "review" / "V5" / "brief.md").read_text(encoding="utf-8") if rc == 0 else ""
check("problem brief has the failing section only", rc == 0 and "E assert 1 == 2" in pb and "ruff" not in pb, out + pb)
check("problem brief lists every attempt", "A1: fixed the import: same failure" in pb
      and "A2: pinned the version" in pb, pb)
check("problem contract: hypotheses with checks", '"hypothesis"' in pb and "at most 3" in pb, pb)
rc, out = rv(root, "consult", "--kind", "problem", "--check", "unit")
check("problem without attempts refused", rc != 0 and "every attempt" in out, out)
rc, out = rv(root, "consult", "--kind", "problem", "--error", "e9", "--attempt", "x: y")
check("unknown error id refused", rc != 0 and "no error e9" in out, out)

rc, out = rv(root, "config", "--consult", "off")
check("config --consult off", rc == 0 and "consult off" in out, out)
rc, out = direction(root)
check("consult off prints OFF", rc == 0 and out.startswith("CONSULT OFF"), out)
rv(root, "config", "--consult", "on")

# -- c2: counsel ---------------------------------------------------------------------
rc, out = rv(root, "counsel", "--consult", "V1", "--file", "-", stdin='{"pick": "O3", "reason": "because so"}\n')
check("pick outside the options refused", rc != 0 and "not O1-O2" in out, out)
rc, out = rv(root, "counsel", "--consult", "V1", "--file", "-", "--tokens", "21000",
             stdin='{"pick": "o1", "reason": "D1 already says JSON lines"}\n{"pick": "O2", "reason": "or this"}\n')
check("direction pick recorded with option text", rc == 0 and "PICK O1 (JSON lines)" in out
      and "1 line(s) after the pick ignored" in out, out)
v1 = consults(root)[0]
check("tokens stored from the notification", v1["tokens"] == {"subagent_tokens": 21000, "total": 21000}, v1)
rc, out = rv(root, "counsel", "--consult", "V1", "--file", "-", stdin='{"pick": "O1", "reason": "again now"}\n')
check("second reply refused", rc != 0 and "one run per consult" in out, out)
rc, out = rv(root, "counsel", "--consult", "V1", "--accepted", "yes", "--file", "-", stdin="")
check("reply and verdict in one call refused", rc != 0 and "one call each" in out, out)
rc, out = rv(root, "counsel", "--consult", "V2", "--accepted", "yes", "--changed", "no")
check("verdict before reply refused", rc != 0 and "no reply yet" in out, out)
rc, out = rv(root, "counsel", "--consult", "V1", "--accepted", "yes", "--changed", "yes", "--passed", "yes")
check("--passed refused on a direction", rc != 0 and "problem consult" in out, out)
rc, out = rv(root, "counsel", "--consult", "V1", "--accepted", "yes", "--changed", "yes")
check("verdict recorded with a trial line", rc == 0 and "TRIAL 1/10: accepted 1/1, changed 1/1" in out
      and "tokens/run 21000" in out, out)

hyps = "".join(json.dumps({"hypothesis": f"cause number {i}", "check": f"run check {i}"}) + "\n" for i in range(1, 5))
rc, out = rv(root, "counsel", "--consult", "V5", "--file", "-", stdin=hyps + '{"hypothesis": "no check given"}\n')
check("4th hypothesis cut, unchecked one rejected", rc == 0 and "H3: cause number 3" in out
      and "H4" not in out and "cut over the cap of 3" in out and "discriminating check" in out, out)
rc, out = rv(root, "counsel", "--consult", "V5", "--accepted", "yes", "--changed", "yes", "--passed", "no")
check("one failed advised attempt is not yet stuck", rc == 0 and "STUCK" not in out, out)
rc, out = rv(root, "consult", "--kind", "problem", "--check", "unit", "--attempt", "H1 from V5: still fails",
             "--user-ok", "user: try once more")
rc2, _ = rv(root, "counsel", "--consult", "V6", "--file", "-", stdin=hyps)
rc, out = rv(root, "counsel", "--consult", "V6", "--accepted", "yes", "--changed", "no", "--passed", "no")
check("two failed advised attempts say go to the user", rc == 0 and rc2 == 0
      and "STUCK: 2 advised attempts" in out, out)

trial = make_repo("trial")
for i in range(10):
    rv(trial, "consult", "--kind", "direction", "--question", f"Pick number {i}?", "--option", "a", "--option", "b",
       "--user-ok", "trial fixture")
    vid = f"V{i + 1}"
    rv(trial, "counsel", "--consult", vid, "--file", "-", "--tokens", "90000", stdin='{"pick": "O1", "reason": "fixture"}\n')
    rc, out = rv(trial, "counsel", "--consult", vid, "--accepted", "yes" if i == 0 else "no", "--changed", "no")
check("trial done warns under 1 in 5 accepted and high tokens", "TRIAL 10/10: accepted 1/10" in out
      and "TRIAL DONE: switch the gate off or tighten it" in out and "90000 tokens per run" in out, out)

# -- c3: problem triggers ------------------------------------------------------------
import dc_verify  # noqa: E402

trig = make_repo("triggers")
ag = trig / ".agent"
fail = [{"id": "unit", "status": "FAIL", "detail": "E assert 1 == 2"}]
check("first failure: no advise", dc_verify.update_streaks(ag, fail, "tree1") == [])
check("rerun on the same tree is not counted", dc_verify.update_streaks(ag, fail, "tree1") == [])
got = dc_verify.update_streaks(ag, fail, "tree2")
check("second failure on a changed tree advises", len(got) == 1 and "ADVISE problem: check unit failed 2 times"
      in got[0] and "--check unit" in got[0], got)
other = [{"id": "unit", "status": "FAIL", "detail": "E KeyError: 'x'"}]
check("a different failure line starts a new streak", dc_verify.update_streaks(ag, other, "tree3") == [])
dc_verify.update_streaks(ag, other, "tree4")
got = dc_verify.update_streaks(ag, [{"id": "unit", "status": "PASS"}], "tree5")
check("a pass clears and points at counsel --passed", len(got) == 1 and "STREAK CLEARED unit after 2" in got[0], got)
check("streak gone after the pass", dc_verify.update_streaks(ag, fail, "tree6") == []
      and json.loads((ag / "verify-streaks.json").read_text())["unit"]["count"] == 1)
check("skip rows leave the streak alone", dc_verify.update_streaks(ag, [{"id": "unit", "status": "SKIP"}], "t7") == []
      and json.loads((ag / "verify-streaks.json").read_text())["unit"]["count"] == 1)


def proj(root, *args):
    p = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_project.py"), "--root", str(root), *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


rc, out = proj(trig, "error", "add", "--summary", "Import fails on blank row")
check("first error: no advise", rc == 0 and "ADVISE" not in out, out)
rc, out = proj(trig, "error", "add", "--summary", "import fails  on BLANK row")
check("same error again advises a problem consult", rc == 0 and "ADVISE problem: e2 repeats e1" in out
      and "--error e2" in out, out)

import dc_state as _ds  # noqa: E402
check("resume marker only for Claude", _ds.resume_host("codex") != "claude" and _ds.resume_host("claude") == "claude")
slow = ag.parent / "slow"; slow.mkdir(); (slow / "x").write_text("")
dc_verify.update_streaks(slow, [{"id": "it", "status": "TIMEOUT", "detail": "timed out after 60s"}], "s1")
got = dc_verify.update_streaks(slow, [{"id": "it", "status": "TIMEOUT", "detail": "timed out after 61s"}], "s2")
check("repeat timeouts with different durations advise", len(got) == 1 and "failed 2 times" in got[0], got)
check("no fingerprint: a repeat failure is not counted", dc_verify.update_streaks(ag, fail, None) == []
      and dc_verify.update_streaks(ag, fail, None) == [])
# -- c4: DC:DECISIONS write gate -----------------------------------------------------
def st(root, *args):
    p = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_state.py"), "--root", str(root), *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def set_decisions(root, *lines):
    text = dc_state.State.parse((root / ".agent" / "drainclamp-state.md").read_text(encoding="utf-8"))
    body = text.sections["DECISIONS"].rstrip("\n") + "\n" + "".join(ln + "\n" for ln in lines)
    f = root / "_dec.md"
    f.write_text(body, encoding="utf-8")
    return st(root, "--set", "DECISIONS", "--file", str(f))


gate = make_repo("gate")
rc, out = set_decisions(gate)
check("unchanged standing lines pass", rc == 0, out)
rc, out = set_decisions(gate, "- Cache size -> 64 entries")
check("untagged added line refused", rc != 0 and "no source" in out, out)
rc, out = set_decisions(gate, "- Cache size -> 64 entries (Claude default)")
check("Claude-only line without advice refused", rc != 0 and "without advice" in out, out)
rc, out = set_decisions(gate, "- Cache size -> 64 entries (Claude default, advice V1)")
check("unknown advice id refused", rc != 0 and "V1 is not an answered direction consult" in out, out)
rv(gate, "consult", "--kind", "direction", "--question", "Which cache size?", "--option", "64", "--option", "256")
rc, out = set_decisions(gate, "- Cache size -> 64 entries (Claude default, advice V1)")
check("advice id without a reply refused", rc != 0 and "not an answered" in out, out)
rv(gate, "counsel", "--consult", "V1", "--file", "-", stdin='{"pick": "O1", "reason": "small working set"}\n')
rc, out = set_decisions(gate, "- Cache size -> 64 entries (Claude default, advice V1)")
check("answered advice id written", rc == 0, out)
rc, out = set_decisions(gate, "- Log level -> info (Claude default, advice skip: no subagents on this host)")
check("advice skip with a reason written", rc == 0, out)
rc, out = set_decisions(gate, "- Retry count -> 3 (user + Claude)", "- Port -> 8080 (repo convention)",
                        "- Theme (charter) -> dark", "- Timeout -> 30 s (assumed)", "no open questions")
check("user, repo, charter, assumed and mixed sources pass", rc == 0, out)
rv(gate, "config", "--consult", "off")
rc, out = set_decisions(gate, "- Batch size -> 50 (Claude)")
check("consult off: Claude-only line passes", rc == 0, out)
rc, out = set_decisions(gate, "- Batch window -> 5 min")
check("consult off: a source is still required", rc != 0 and "no source" in out, out)

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
