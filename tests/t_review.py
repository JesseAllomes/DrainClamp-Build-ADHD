"""Gate 6 checks for dc_review.py: store, packet, ingest, scope, guard, verdicts."""
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
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcreview-"))
ROADMAP = ("| id | goal | files | status | deps |\n|---|---|---|---|---|\n"
           "| m1 | add the parser | a.py | pending | |\n"
           "| m2 | later work | b.py | pending | m1 |")
A_PY = ("def parse(items):\n"
        "    total = 0\n"
        "    for i in range(len(items) - 1):\n"
        "        total += items[i]\n"
        "    return total\n"
        "\n"
        "\n"
        "def caller():\n"
        "    return parse([1, 2, 3])\n")


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def make_repo(name) -> Path:
    root = tmp / name
    (root / ".agent").mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "b.py").write_text("X = 1\n", encoding="utf-8")
    git(root, "add", "b.py")
    git(root, "commit", "-qm", "base")
    text = dc_state.template_text().replace(
        "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
        f"<!-- DC:ROADMAP -->\n{ROADMAP}\n")
    text = text.replace("<!-- DC:DECISIONS -->\n", "<!-- DC:DECISIONS -->\n"
                        "- Parser totals -> the parser deliberately skips trailing items of the list (user)\n")
    (root / ".agent" / "drainclamp-state.md").write_text(text, encoding="utf-8")
    (root / "a.py").write_text(A_PY, encoding="utf-8", newline="\n")
    return root


def rv(root, *args, stdin=None):
    p = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_review.py"), "--root", str(root), *args],
                       capture_output=True, text=True, input=stdin)
    return p.returncode, p.stdout + p.stderr


def st(root, *args):
    p = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_state.py"), "--root", str(root), *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def green(root):
    """A passing Gate 3 record for the current tree, as dc_verify writes it."""
    dc_verify.save_record(root / ".agent", dc_verify.TIER_RECORD_PREFIX + "milestone", "tier", "pass",
                          "verify.log", dc_verify.tree_fingerprint(root))


def set_roadmap(root, status_m1):
    f = tmp / "rm.md"
    f.write_text(ROADMAP.replace("| a.py | pending |", f"| a.py | {status_m1} |"), encoding="utf-8")
    return st(root, "--set", "ROADMAP", "--file", str(f))


def side(root):
    return dc_review.review_of(dc_project.load(root / ".agent"))


def finding(cl, ev, line=3, sev="high", cat="correctness", file="a.py", decision=""):
    return json.dumps({"severity": sev, "category": cat, "file": file, "line": line, "symbol": "parse",
                       "claim": cl, "scenario": "parse([1,2,3]) -> 3, expected 6", "evidence": ev,
                       "confidence": 0.8, "decision": decision})


# -- config and the off default -------------------------------------------------------------------
root = make_repo("main")
rc, out = rv(root, "config")
check("config defaults to mode off", rc == 0 and "mode off" in out, out)
check("config lists the Codex and Grok rosters by default",
      "models codex: " in out and "checker=gpt-6-luna/medium" in out and "models grok: " in out, out)
rc, out = rv(root, "config", "--host", "codex", "--model", "checker=gpt-6-sol/low")
check("--host codex sets the Codex roster, not Claude's",
      rc == 0 and "checker=gpt-6-sol/low" in out and "checker=haiku/medium" in out, out)
check("a host override survives normalisation beside the other defaults",
      dc_review.models_for(side(root)["config"], "codex")
      == {**dc_review.DEFAULT_CONFIG["host_models"]["codex"], "checker": "gpt-6-sol/low"})
rc, out = set_roadmap(root, "active")
check("mode off: roadmap writes are untouched by Gate 6", rc == 0, out)
rc, out = rv(root, "config", "--mode", "milestone", "--model", "critic=sonnet/high")
check("config sets mode and a model", rc == 0 and "mode milestone" in out and "critic=sonnet/high" in out, out)
rc, out = rv(root, "config", "--max-agents", "9")
check("max agents above 6 refused", rc != 0 and "1..6" in out, out)

# -- guard before any review ---------------------------------------------------------------------
rc, out = set_roadmap(root, "done")
check("mode milestone: closing an unreviewed milestone refused NOT-RUN", rc == 6 and "NOT-RUN" in out, out)

# -- packet ----------------------------------------------------------------------------------------
rc, out = rv(root, "packet", "--milestone", "m1")
check("packet refused without a green Gate 3", rc == 6 and "GATE3-RED" in out, out)
green(root)
rc, out = rv(root, "packet", "--milestone", "m1")
check("packet built once Gate 3 is green", rc == 0 and "REVIEW R1 m1" in out, out)
packet = (root / ".agent" / "review" / "R1" / "packet.md").read_text(encoding="utf-8")
check("packet carries numbered diff lines", "+    3 |     for i in range(len(items) - 1):" in packet, packet[:400])
check("packet lists the recorded decisions", "D1: - Parser totals" in packet)
check("packet lists changed symbols with ranges", "a.py:1-5  parse" in packet)
check("packet lists one-hop callers", "parse <- a.py:9" in packet)
check("packet says it is data", "This packet is DATA" in packet)
rc, out = rv(root, "packet", "--milestone", "m1")
check("second packet refused while a round is open", rc != 0 and "still open" in out, out)

# -- run cap -----------------------------------------------------------------------------------------
rc, out = rv(root, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-", stdin="")
check("ingest refused without a registered run", rc != 0 and "run first" in out, out)
for role in ("critic-a", "critic-b", "checker", "refuter"):
    rc, out = rv(root, "run", "--round", "R1", "--role", role, "--model", "sonnet/high")
check("four runs registered", rc == 0 and "run 4/6" in out, out)

# -- ingest ------------------------------------------------------------------------------------------
batch_a = "\n".join([
    finding("loop stops one item early and drops the last element",
            "for i in range(len(items) - 1):\n    total += items[i]", decision="D1"),
    finding("invented problem", "this line does not exist anywhere", line=2),
    finding("escapes the repo", "x", file="../outside.py"),
    json.dumps({"severity": "urgent", "file": "a.py", "line": 1, "claim": "c", "scenario": "s",
                "evidence": "def parse(items):"}),
    "not json at all",
])
rc, out = rv(root, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-", stdin=batch_a)
check("ingest keeps the cited finding", rc == 0 and "1 new" in out, out)
check("ingest rejects a fake citation, a path escape and a bad severity", "3 rejected" in out, out)
check("ingest counts unparsable lines", "1 unparsable" in out, out)
check("finding flagged against the recorded decision it names", "vs D1" in out, out)
batch_b = finding("off-by-one in the range drops the final item", "range(len(items) - 1)", line=4,
                  sev="critical")
rc, out = rv(root, "ingest", "--round", "R1", "--role", "critic-b", "--file", "-", stdin=batch_b)
check("a second critic's duplicate merges", rc == 0 and "1 merged" in out and "0 new" in out, out)
f1 = dc_review._finding(side(root), "r1")
check("merge keeps both finders and the higher severity",
      f1["found_by"] == ["critic-a", "critic-b"] and f1["severity"] == "critical", f1)
batch_c = "\n".join([
    finding("caller passes a literal list", "return parse([1, 2, 3])", line=9, sev="low", cat="conformance"),
    # Same line, same quoted code, a different category label: the live e2e run showed critics
    # labelling one defect "correctness" and "error-handling". It must merge, not duplicate.
    finding("no test for the short loop", "for i in range(len(items) - 1):", line=3, sev="medium",
            cat="test-gap"),
])
rc, out = rv(root, "ingest", "--round", "R1", "--role", "checker", "--file", "-", stdin=batch_c)
check("checker finding kept as r2", "r2" in out, out)
check("same quoted code under another category merges", "1 merged" in out and "1 new" in out, out)
cands = (root / ".agent/review/R1/candidates.jsonl").read_text(encoding="utf-8").splitlines()
check("candidates hand-off file lists both candidates for the refuter",
      [json.loads(c)["id"] for c in cands] == ["r1", "r2"], cands)

# -- blocking -------------------------------------------------------------------------------------
rc, out = set_roadmap(root, "done")
check("candidates block the milestone (exit 2)", rc == 2 and "REVIEW FINDINGS 2" in out, out)
code, out = rv(root, "check", "--milestone", "m1")
check("check exits 2 while findings block", code == 2 and "FINDINGS 2" in out, out)
nxt = "\n".join(dc_project.capsule(root, dc_project.load(root / ".agent")))
check("next capsule shows the review hold", "Review: 2 blocking (r1 r2)" in nxt, nxt)
verdict_line, _ = dc_state.purge_check(dc_state.load(root / ".agent" / "drainclamp-state.md"), root, False)
check("purge check holds while findings block", verdict_line.startswith("HOLD (review open"), verdict_line)
rc, out = rv(root, "finish", "--round", "R1")
check("finish refused while candidates are unadjudicated", rc != 0 and "not adjudicated" in out, out)

# -- adjudicate and decide -----------------------------------------------------------------------
verdicts = "\n".join([json.dumps({"id": "r1", "verdict": "CONFIRMED", "note": "range stops at n-1"}),
                      json.dumps({"id": "r2", "verdict": "REFUTED", "note": "literal is fine"})])
rc, out = rv(root, "adjudicate", "--round", "R1", "--file", "-", stdin=verdicts)
check("refuter verdicts recorded", rc == 0 and "r1 [critical] open" in out and "refuted" in out, out)
rc, out = rv(root, "decide", "--set", "r1=waive")
check("waive without a reason refused", rc != 0 and "needs a reason" in out, out)
rc, out = rv(root, "decide", "--set", "r2=fix")
check("a refuted finding takes no decision", rc != 0 and "only adjudicated" in out, out)
rc, out = rv(root, "decide", "--set", "r1=fix")
check("fix decision moves r1 to fixing", rc == 0 and "r1 -> fixing" in out, out)

# -- ticket and scope -----------------------------------------------------------------------------
rc, out = rv(root, "resolve", "--id", "r1", "--fixed")
check("resolve refused without a ticket", rc != 0 and "no ticket" in out, out)
rc, out = rv(root, "ticket", "--round", "R1", "--id", "r1", "--allow", "a.py", "--file", "-",
             stdin="TICKET r1\nCHANGE: range(len(items))\n")
check("ticket stored with its allow list", rc == 0 and "fixer may change: a.py" in out, out)
check("tickets hand-off file holds the ticket for the fixer",
      "## r1\nTICKET r1" in (root / ".agent/review/R1/tickets.md").read_text(encoding="utf-8"))
(root / "a.py").write_text(A_PY.replace("range(len(items) - 1)", "range(len(items))"), encoding="utf-8")
(root / "b.py").write_text("X = 2\n", encoding="utf-8")
rc, out = rv(root, "scope", "--round", "R1")
check("fixer touching an unlisted file is a SCOPE-BREACH (exit 4)", rc == 4 and "b.py" in out, out)
rc, out = rv(root, "resolve", "--id", "r1", "--fixed")
check("resolve refused after a breach", rc != 0 and "scope" in out, out)
(root / "b.py").write_text("X = 1\n", encoding="utf-8")
rc, out = rv(root, "scope", "--round", "R1")
check("scope ok once the stray edit is restored", rc == 0 and "scope ok" in out, out)
rc, out = rv(root, "resolve", "--id", "r1", "--fixed", "--note", "range fixed, test added")
check("resolve marks r1 fixed", rc == 0 and "r1 fixed" in out, out)

# -- finish, guard, stale -------------------------------------------------------------------------
rc, out = rv(root, "finish", "--round", "R1")
check("finish stamps and passes", rc == 0 and "REVIEW-PASS" in out, out)
rc, out = rv(root, "run", "--round", "R1", "--role", "recheck", "--model", "sonnet")
check("a finished round takes no more runs", rc != 0 and "closed" in out, out)
rc, out = set_roadmap(root, "done")
check("roadmap accepts done after the review passes", rc == 0, out)
(root / "a.py").write_text(A_PY + "\n# later edit\n", encoding="utf-8")
code, out = rv(root, "check", "--milestone", "m1")
check("an edit after the review makes it STALE (exit 5)", code == 5 and "STALE" in out, out)

# -- suppression across rounds ------------------------------------------------------------------
green(root)
rc, out = rv(root, "packet", "--milestone", "m1", "--depth", "quick")
check("quick packet starts R2 with no call-site section", rc == 0 and "R2" in out
      and "Call sites" not in (root / ".agent/review/R2/packet.md").read_text(encoding="utf-8"), out)
rv(root, "run", "--round", "R2", "--role", "critic-a", "--model", "sonnet")
rv(root, "run", "--round", "R2", "--role", "refuter", "--model", "sonnet")
noise = finding("caller hard-codes its input", "return parse([1, 2, 3])", line=9, sev="low", cat="style")
rv(root, "ingest", "--round", "R2", "--role", "critic-a", "--file", "-", stdin=noise)
rv(root, "adjudicate", "--round", "R2", "--file", "-",
   stdin=json.dumps({"id": "r3", "verdict": "UNCERTAIN", "note": "taste"}))
rc, out = rv(root, "decide", "--set", "r3=dismiss:fixture data, not a defect")
check("dismiss with a reason accepted", rc == 0 and "r3 -> dismissed" in out, out)
rc, out = rv(root, "ingest", "--round", "R2", "--role", "critic-a", "--file", "-", stdin=noise)
check("a dismissed finding does not resurface", "1 suppressed" in out and "0 new" in out, out)
rc, out = rv(root, "finish", "--round", "R2")
check("R2 finishes clean", rc == 0 and "REVIEW-PASS" in out, out)
green(root)
rv(root, "packet", "--milestone", "m1")
check("the next packet lists the dismissed finding",
      "r3 [dismissed]" in (root / ".agent/review/R3/packet.md").read_text(encoding="utf-8"))

# -- the cap ----------------------------------------------------------------------------------------
cap = make_repo("cap")
rv(cap, "config", "--mode", "milestone", "--max-agents", "2")
green(cap)
rv(cap, "packet", "--milestone", "m1")
rv(cap, "run", "--round", "R1", "--role", "critic-a", "--model", "inline")
rc, out = rv(cap, "run", "--round", "R1", "--role", "refuter", "--model", "inline")
rc, out = rv(cap, "run", "--round", "R1", "--role", "fixer", "--model", "sonnet")
check("a run past max agents refused (exit 2)", rc == 2 and "REVIEW CAP" in out, out)
check("an inline run marks the round partial", "inline" in side(cap)["rounds"][0]["coverage"])
rc, out = rv(cap, "finish", "--round", "R1")
check("finish refused while a registered finder was never ingested", rc == 6 and "never ingested: critic-a" in out, out)
rv(cap, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-", stdin="NO FINDINGS\n")
rc, out = rv(cap, "finish", "--round", "R1")
check("a clean inline round passes, tagged partial", rc == 0 and "COVERAGE: partial" in out, out)

# -- abort, and binary files in the change set ------------------------------------------------
ab = make_repo("abort")
rv(ab, "config", "--mode", "milestone")
(ab / "blob.bin").write_bytes(bytes(range(256)))
green(ab)
rc, out = rv(ab, "packet", "--milestone", "m1")
check("an untracked binary file does not break the packet", rc == 0 and "R1" in out, out)
check("the binary file is listed without a diff",
      "### FILE blob.bin  (no textual diff" in (ab / ".agent/review/R1/packet.md").read_text(encoding="utf-8"))
rv(ab, "run", "--round", "R1", "--role", "critic-a", "--model", "sonnet")
rv(ab, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-",
   stdin=finding("loop stops one item early", "for i in range(len(items) - 1):"))
rc, out = rv(ab, "abort", "--round", "R1", "--reason", "packet was wrong")
check("abort closes the round as not run (exit 6) and withdraws its candidates",
      rc == 6 and "1 candidate(s) withdrawn" in out and side(ab)["findings"][0]["status"] == "withdrawn", out)
code, out = rv(ab, "check", "--milestone", "m1")
check("an aborted round is never a pass", code == 6 and "NOT-RUN" in out, out)
rc, out = rv(ab, "packet", "--milestone", "m1")
check("a new round may start after an abort", rc == 0 and "R2" in out, out)

# -- big files: a new one is pointed at, a clipped modified one is partial coverage --------------
big = make_repo("big")
(big / "long.py").write_text("".join(f"V{i} = {i}\n" for i in range(300)), encoding="utf-8")
git(big, "add", "long.py")
git(big, "commit", "-qm", "long")
(big / "long.py").write_text("".join(f"V{i} = {i + 1}\n" for i in range(300)), encoding="utf-8")
(big / "fresh.py").write_text("".join(f"def f{i}():\n    return {i}\n" for i in range(160)), encoding="utf-8")
green(big)
rc, out = rv(big, "packet", "--milestone", "m1", "--depth", "final")
pk = (big / ".agent/review/R1/packet.md").read_text(encoding="utf-8")
check("a large new file is pointed at, not half inlined",
      "### FILE fresh.py  NEW, 320 lines: not inlined" in pk and "def f159" not in pk, pk[:200])
check("its symbols are listed so agents can read it", "fresh.py:319-320  f159" in pk)
check("a clipped modified file makes coverage partial",
      "COVERAGE: partial" in out and "diff cut at 250 lines in long.py" in pk, out)

# -- mode final only guards the last close ----------------------------------------------------
fin = make_repo("final")
rv(fin, "config", "--mode", "final")
rc, out = set_roadmap(fin, "done")
check("mode final: an intermediate milestone closes without review", rc == 0, out)

# -- the advisor ----------------------------------------------------------------------------------
adv = make_repo("advise")
(adv / ".agent" / "charter.json").write_text(json.dumps({"charter": {
    "name": "adv", "purpose": "Sum parser totals for the payroll export",
    "out_of_scope": "Anything beyond parsing totals"}}), encoding="utf-8")
rv(adv, "config", "--mode", "milestone")
green(adv)
rv(adv, "packet", "--milestone", "m1")
rc, out = rv(adv, "brief", "--milestone", "m1")
check("brief refused before the review passes", rc == 6 and "ADVICE NOT-RUN" in out, out)
rv(adv, "run", "--round", "R1", "--role", "critic-a", "--model", "sonnet")
rv(adv, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-", stdin="NO FINDINGS\n")
rv(adv, "finish", "--round", "R1")
rc, out = rv(adv, "brief", "--milestone", "m1")
check("brief built after a passing review", rc == 0 and "ADVICE A1" in out and "purpose lines 2" in out, out)
brief = (adv / ".agent/review/A1/brief.md").read_text(encoding="utf-8")
check("brief carries charter purpose and out-of-scope as P lines",
      "P1: [charter purpose] Sum parser totals" in brief and "P2: [charter out_of_scope]" in brief, brief[:300])


def idea(title, size="S", purpose="P1", file="a.py", line=1, evidence="def parse(items):", kind="improve"):
    return json.dumps({"title": title, "kind": kind, "size": size, "purpose": purpose,
                       "value": "the payroll officer gets correct totals", "detail": "d",
                       "file": file, "line": line, "evidence": evidence})


ideas = "\n".join([
    idea("Sum every item, not all but the last"),
    idea("Validate the totals against the export header", size="M", file="",
         evidence="Sum parser totals for the payroll export"),
    idea("Serves nothing listed", purpose="P9"),
    idea("Grounded in nothing", evidence="this quote is nowhere"),
    idea("Report which item overflowed", file="", evidence="Sum parser totals for the payroll export"),
    idea("Cache parsed totals between runs", size="L", kind="expand", file="",
         evidence="Sum parser totals for the payroll export"),
    idea("Expose totals to the KPI feed", size="M", kind="expand", file="",
         evidence="Sum parser totals for the payroll export"),
    idea("Sixth valid idea over the cap", file="", evidence="Sum parser totals for the payroll export"),
])
rc, out = rv(adv, "suggest", "--advice", "A1", "--model", "opus/high", "--file", "-", stdin=ideas)
check("advisor ideas: grounded ones kept, ungrounded rejected, five at most",
      rc == 0 and "5 new" in out and "2 rejected" in out and "TRUNCATED 1" in out, out)
check("a purpose that is not in the brief is rejected", "is not a P<n> in the brief" in out, out)
rc, out = rv(adv, "suggest", "--advice", "A1", "--file", "-", stdin=idea("Again"))
check("one advisor run per brief", rc != 0 and "one run per brief" in out, out)
rc, out = rv(adv, "triage", "--set", "i2=deny")
check("deny needs a reason", rc != 0 and "deny needs a reason" in out, out)
rc, out = rv(adv, "triage", "--set", "i1=accept", "--set", "i2=accept", "--set", "i3=shelve",
             "--set", "i4=deny:out of scope for a parser")
check("accepting an S idea adds a to-do; an M idea asks for a roadmap row",
      rc == 0 and "to-do t1 added" in out and "i2 is size M: add a roadmap row" in out, out)
check("the to-do names the idea", any(t["text"].startswith("[i1]") for t in dc_project.load(adv / ".agent")["todos"]))
rc, out = rv(adv, "brief", "--milestone", "m1")
brief2 = (adv / ".agent/review/A2/brief.md").read_text(encoding="utf-8")
check("the next brief lists decided and shelved ideas",
      "i4 [denied] Cache parsed totals" in brief2 and "i3 Report which item overflowed" in brief2, brief2[-600:])
again = "\n".join([idea("Report which item overflowed", file="", evidence="Sum parser totals for the payroll export"),
                   idea("Cache parsed totals between runs", size="L", kind="expand", file="",
                        evidence="Sum parser totals for the payroll export")])
rc, out = rv(adv, "suggest", "--advice", "A2", "--model", "opus/high", "--file", "-", stdin=again)
check("a shelved idea comes back with its id; a denied one never does",
      "1 revived" in out and "1 already decided" in out and "i3" in out, out)
code, out = rv(adv, "status", "--milestone", "m1")
check("status shows the advisor's ideas", "advisor final: ideas" in out and "proposed" in out, out)

# -- dc_verify writes the tier record Gate 6 reads ------------------------------------------------
ver = make_repo("verify")
blk = tmp / "verify.json"
blk.write_text(json.dumps([{"id": "ok", "argv": [sys.executable, "-c", "pass"], "cwd": ".",
                            "tier": "milestone", "timeout_s": 60}]), encoding="utf-8")
st(ver, "--set", "VERIFY", "--file", str(blk))
p = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_verify.py"), "--root", str(ver),
                    "--tier", "milestone"], capture_output=True, text=True)
rec = dc_verify.load_records(ver / ".agent").get("tier:milestone") or {}
check("dc_verify records the tier outcome and tree",
      rec.get("tree") == dc_verify.tree_fingerprint(ver) and rec.get("status") in ("pass", "fail"),
      f"rc={p.returncode} rec={rec}")
if p.returncode == 0:
    check("a passing tier record satisfies Gate 6's Gate 3 check", dc_review.gate3_status(ver) is not None)


def fixing_repo(name, *batch):
    """A repo whose round R1 holds critic-a's findings, confirmed and decided `fix`."""
    root = make_repo(name)
    rv(root, "config", "--mode", "milestone")
    green(root)
    rv(root, "packet", "--milestone", "m1")
    rv(root, "run", "--round", "R1", "--role", "critic-a", "--model", "sonnet")
    rv(root, "run", "--round", "R1", "--role", "refuter", "--model", "sonnet")
    rv(root, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-", stdin="\n".join(batch))
    ids = [f["id"] for f in side(root)["findings"]]
    rv(root, "adjudicate", "--round", "R1", "--file", "-",
       stdin="\n".join(json.dumps({"id": i, "verdict": "CONFIRMED", "note": "fixture"}) for i in ids))
    rv(root, "decide", *[a for i in ids for a in ("--set", f"{i}=fix")])
    return root


# -- replies read straight from a Claude Code subagent transcript (t2) ------------------------------
def transcript(name, agent_type, *messages, folder=None, usage=None):
    """A subagent transcript as Claude Code writes it: JSON lines plus a .meta.json."""
    folder = folder or tmp / "transcripts"
    folder.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "message": {"role": "user", "content": "Gate 6 review."}}]
    for n, content in enumerate(messages):
        for block in content:   # Claude Code writes one line per content block of a message
            rows.append({"type": "assistant", "uuid": f"u{n}-{len(rows)}",
                         "message": {"id": f"msg{n}", "role": "assistant", "content": [block]}})
            if usage:   # every streamed line of a call repeats that call's usage
                rows[-1]["message"]["usage"] = dict(usage)
    path = folder / f"agent-{name}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (folder / f"agent-{name}.meta.json").write_text(
        json.dumps({"agentType": f"drainclamp-build-adhd:{agent_type}"}), encoding="utf-8")
    return path


def handback(text):
    return {"type": "tool_use", "name": "SubagentHandback", "input": {"message": text}}


tx = make_repo("transcript")
rv(tx, "config", "--mode", "milestone")
green(tx)
rv(tx, "packet", "--milestone", "m1")
rv(tx, "run", "--round", "R1", "--role", "critic-a", "--model", "sonnet")
rv(tx, "run", "--round", "R1", "--role", "refuter", "--model", "sonnet")
crit = transcript("a1111111111111111", "dca-critic",
                  [{"type": "text", "text": "Reading the packet."}, {"type": "tool_use", "name": "Read", "input": {}}],
                  [handback(finding("loop stops one item early", "for i in range(len(items) - 1):"))],
                  usage={"input_tokens": 100, "cache_creation_input_tokens": 10,
                         "cache_read_input_tokens": 1000, "output_tokens": 50})
rc, out = rv(tx, "ingest", "--round", "R1", "--role", "refuter", "--from-transcript", str(crit))
check("ingest takes only finder roles", rc != 0, out)
rc, out = rv(tx, "ingest", "--round", "R1", "--role", "critic-a", "--from-transcript", str(crit))
check("ingest reads the SubagentHandback reply from the transcript", rc == 0 and "1 new" in out, out)
rc, out = rv(tx, "adjudicate", "--round", "R1", "--from-transcript", str(crit))
check("a critic's transcript is refused as the refuter's", rc != 0 and "not dca-refuter" in out, out)
os.environ["CLAUDE_CONFIG_DIR"] = str(tmp / "claude-home")
transcript("a2222222222222222", "dca-refuter",
           [{"type": "text", "text": "Early narration, not the reply."}, {"type": "tool_use", "name": "Read", "input": {}}],
           [{"type": "text", "text": json.dumps({"id": "r1", "verdict": "CONFIRMED", "note": "n-1"})},
            {"type": "text", "text": "\n"}],
           folder=tmp / "claude-home" / "projects" / "proj" / "sess" / "subagents")
rc, out = rv(tx, "adjudicate", "--round", "R1", "--from-transcript", "a2222222222222222")
os.environ.pop("CLAUDE_CONFIG_DIR")
check("an agent id finds its transcript; the last message's text is the reply",
      rc == 0 and "r1 [high] open" in out, out)

def run_tokens(root, role):
    rnd = next(r for r in side(root)["rounds"] if r["id"] == "R1")
    return next(r for r in rnd["runs"] if r["role"] == role).get("tokens")


tok = run_tokens(tx, "critic-a")
check("a transcript's usage is stored on its run, each streamed call once",
      tok and tok["calls"] == 2 and tok["total"] == 2320 and tok["output"] == 100, tok)
rv(tx, "ingest", "--round", "R1", "--role", "critic-a", "--from-transcript", str(crit))
check("ingesting the same transcript again does not double its tokens",
      (run_tokens(tx, "critic-a") or {}).get("total") == 2320, run_tokens(tx, "critic-a"))
check("a transcript without usage leaves its run uncounted", run_tokens(tx, "refuter") is None,
      run_tokens(tx, "refuter"))
rc, out = rv(tx, "status", "--milestone", "m1")
check("status prints each round's token total and says which runs it misses",
      "tokens R1: 2,320 total, 100 output (critic-a 2,320) COVERAGE: partial (1/2 runs counted)" in out, out)
rc, out = rv(tx, "status", "--milestone", "m1", "--json")
check("status --json carries the per-round token totals",
      rc == 0 and json.loads(out)["tokens"]["R1"]["total"] == 2320, out[-300:])
plain = tmp / "transcripts" / "codex-reply.txt"
plain.write_text(finding("x", "def parse(items):") + "\n", encoding="utf-8")
rc, out = rv(tx, "ingest", "--round", "R1", "--role", "critic-a", "--from-transcript", str(plain))
check("an unknown transcript format is refused, not guessed",
      rc != 0 and "not a Claude Code subagent transcript" in out, out)
cut = transcript("a3333333333333333", "dca-critic",
                 [{"type": "text", "text": "Looking."}, {"type": "tool_use", "name": "Read", "input": {}}])
rc, out = rv(tx, "ingest", "--round", "R1", "--role", "critic-a", "--from-transcript", str(cut))
check("a transcript that ends mid-work is refused, never read as a reply",
      rc != 0 and "ends without a final reply" in out, out)
rc, out = rv(tx, "ingest", "--round", "R1", "--role", "critic-a", "--from-transcript", str(crit),
             "--file", "-", stdin="")
check("--file and --from-transcript are exclusive", rc != 0 and "not allowed with" in out, out)

# -- replies read straight from a Codex spawned agent's rollout (i8) --------------------------------
def rollout(thread, role, *events, folder=None):
    """A Codex rollout as the CLI writes it: session_meta first, then event records."""
    folder = folder or tmp / "rollouts"
    folder.mkdir(parents=True, exist_ok=True)
    spawn = {"agent_nickname": "Ada", "agent_path": "/root/review", "agent_role": role, "depth": 1,
             "parent_thread_id": "01a10000-0000-7000-8000-000000000000"}
    rows = [{"type": "session_meta", "payload": {"id": thread, "source": {"subagent": {"thread_spawn": spawn}},
                                                 "base_instructions": {"text": "Gate 6 review."}}}]
    rows += [{"timestamp": "2026-10-08T01:00:00Z", "type": "event_msg", "payload": e} for e in events]
    path = folder / f"rollout-2026-10-08T10-00-00-{thread}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def done_msg(text):
    return {"type": "task_complete", "last_agent_message": text}


def tokens_msg(inp, cached, out, total):
    usage = {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
             "output_tokens": out, "reasoning_output_tokens": 0, "total_tokens": inp + out}
    return {"type": "token_count", "info": {"last_token_usage": usage,
                                            "total_token_usage": dict(usage, total_tokens=total)}}


cx = make_repo("codex-rollout")
rv(cx, "config", "--mode", "milestone")
green(cx)
rv(cx, "packet", "--milestone", "m1")
rv(cx, "run", "--round", "R1", "--role", "critic-a", "--model", "gpt-6.1-sol")
rv(cx, "run", "--round", "R1", "--role", "refuter", "--model", "gpt-6.1-sol")
crit_cx = rollout("01a10000-0000-7000-8000-00000000c0de", "dca-critic",
                  {"type": "task_started"}, tokens_msg(1000, 600, 40, 1040),
                  done_msg(finding("loop stops one item early", "for i in range(len(items) - 1):")))
rc, out = rv(cx, "ingest", "--round", "R1", "--role", "critic-a", "--from-transcript", str(crit_cx))
check("ingest reads a Codex agent's last task_complete message", rc == 0 and "1 new" in out, out)
tok = next(r for r in next(r for r in side(cx)["rounds"] if r["id"] == "R1")["runs"]
           if r["role"] == "critic-a").get("tokens")
check("a Codex rollout's usage is stored on its run, input counted once",
      tok and (tok["calls"], tok["input"], tok["cache_read"], tok["total"]) == (1, 400, 600, 1040), tok)
rc, out = rv(cx, "adjudicate", "--round", "R1", "--from-transcript", str(crit_cx))
check("a Codex critic's rollout is refused as the refuter's", rc != 0 and "not dca-refuter" in out, out)
guard = rollout("01a10000-0000-7000-8000-0000000000aa", None, done_msg("APPROVE"))
rc, out = rv(cx, "ingest", "--round", "R1", "--role", "critic-a", "--from-transcript", str(guard))
check("a Codex rollout with no agent role is refused for a reviewer role",
      rc != 0 and "dca-critic" in out and "nothing read" in out, out)
again = rollout("01a10000-0000-7000-8000-0000000000bb", "dca-critic",
                done_msg("NO FINDINGS"), {"type": "task_started"})
rc, out = rv(cx, "ingest", "--round", "R1", "--role", "critic-a", "--from-transcript", str(again))
check("a Codex rollout working on a later task is refused, not read as its old reply",
      rc != 0 and "ends without a final reply" in out, out)
os.environ["CODEX_HOME"] = str(tmp / "codex-home")
rollout("01a10000-0000-7000-8000-00000000f00d", "dca-refuter",
        {"type": "task_started"}, done_msg(json.dumps({"id": "r1", "verdict": "CONFIRMED", "note": "n-1"})),
        folder=tmp / "codex-home" / "sessions" / "2026" / "10" / "08")
rc, out = rv(cx, "adjudicate", "--round", "R1", "--from-transcript", "01a10000-0000-7000-8000-00000000f00d")
os.environ.pop("CODEX_HOME")
check("a Codex thread id finds its rollout under CODEX_HOME", rc == 0 and "r1 [high] open" in out, out)

# -- the user skips a small release's review --------------------------------------------------------
sk = make_repo("skip")
rv(sk, "config", "--mode", "milestone")
rc, out = rv(sk, "skip", "--milestone", "m1", "--reason", "x")
check("skip needs a real reason", rc != 0 and "reason" in out, out)
green(sk)
rv(sk, "packet", "--milestone", "m1")
rc, out = rv(sk, "skip", "--milestone", "m1", "--reason", "docs-only release")
check("skip refused while a round is open", rc != 0 and "still open" in out, out)
rv(sk, "abort", "--round", "R1", "--reason", "switching to a skip")
rc, out = rv(sk, "skip", "--milestone", "m1", "--reason", "docs-only release")
check("skip records a finished round with the user's reason", rc == 0 and "SKIPPED by the user" in out, out)
code, out = rv(sk, "check", "--milestone", "m1")
check("a skipped review lets the milestone close but never reads as a pass",
      code == 0 and "REVIEW-SKIPPED" in out and "REVIEW-PASS" not in out and "docs-only release" in out, out)
rc, out = set_roadmap(sk, "done")
check("the roadmap guard accepts a skipped review", rc == 0, out)
(sk / "a.py").write_text(A_PY + "\n# after the skip\n", encoding="utf-8")
code, out = rv(sk, "check", "--milestone", "m1")
check("an edit after the skip is STALE (exit 5)", code == 5 and "a.py" in out, out)
sc = make_repo("skip-clean")
git(sc, "add", "-A")
git(sc, "commit", "-qm", "release")
rc, out = rv(sc, "skip", "--milestone", "m1", "--reason", "docs-only release")
check("skip refuses an empty change set instead of stamping nothing",
      rc != 0 and "no changed files" in out and not side(sc)["rounds"], out)


# -- scope names a ticket whose files did not change -----------------------------------------------
bl = fixing_repo("blocked", finding("loop stops one item early", "for i in range(len(items) - 1):"))
rv(bl, "ticket", "--round", "R1", "--id", "r1", "--allow", "a.py", "--file", "-", stdin="TICKET r1\n")
rc, out = rv(bl, "scope", "--round", "R1")
check("scope flags a ticket whose files did not change",
      rc == 0 and "r1: none of its files changed (fixer BLOCKED?)" in out, out)

# -- mode final guards the last close ------------------------------------------------------------
fl = make_repo("final-last")
rv(fl, "config", "--mode", "final")
both = tmp / "rm-both.md"
both.write_text(ROADMAP.replace("| pending |", "| done |"), encoding="utf-8")
rc, out = st(fl, "--set", "ROADMAP", "--file", str(both))
check("mode final: closing the last milestone unreviewed is refused NOT-RUN",
      rc == 6 and "NOT-RUN" in out and "m2" in out, out)

# -- a damaged sidecar never holds the purge -------------------------------------------------------
dp = make_repo("damaged-purge")
(dp / ".agent" / "drainclamp-project.json").write_text("{not json", encoding="utf-8")
line, _ = dc_state.purge_check(dc_state.load(dp / ".agent" / "drainclamp-state.md"), dp, False)
check("a damaged sidecar counts as no open findings in the purge check",
      dc_state.review_blocking(dp) == 0 and not line.startswith("HOLD (review open"), line)

# -- resolve --reopen sends a failed re-check back to open ---------------------------------------
ro = fixing_repo("reopen", finding("loop stops one item early", "for i in range(len(items) - 1):"))
rc, out = rv(ro, "resolve", "--id", "r1", "--reopen", "--note", "re-check NOT-RESOLVED")
f1 = side(ro)["findings"][0]
check("resolve --reopen puts the finding back to open",
      rc == 0 and "r1 reopened" in out and f1["status"] == "open" and f1["resolved_at"] is None
      and "NOT-RESOLVED" in f1["refuter_note"], out)

# -- brief --anyway: the advisor without a passing review, recorded ------------------------------
an = make_repo("anyway")
rv(an, "config", "--mode", "milestone")
green(an)
rv(an, "packet", "--milestone", "m1")
rc, out = rv(an, "brief", "--milestone", "m1", "--anyway", "user wants ideas before the review ends")
adv_rec = side(an)["advice"]
check("brief --anyway builds the brief and records the reason",
      rc == 0 and "ADVICE A1" in out and bool(adv_rec)
      and adv_rec[0]["anyway"] == "user wants ideas before the review ends", out)
check("no purpose recorded is COVERAGE: partial, not README only",
      "none recorded: COVERAGE: partial" in out and "README only" not in out, out)
(an / "README.md").write_text("# anyway\n\nSums parser totals for an export.\n", encoding="utf-8")
rc, out = rv(an, "brief", "--milestone", "m1", "--anyway", "again, with a README")
check("a README-only purpose is flagged with the charter fix (t11)",
      rc == 0 and "PURPOSE: README only (no objectives, out_of_scope, never)" in out
      and "COVERAGE: partial" in out, out)
rc, out = rv(adv, "brief", "--milestone", "m1")
check("a charter purpose is not flagged", rc == 0 and "README only" not in out, out)

# -- --base never reaches git as an option (t4) ---------------------------------------------------
ob = make_repo("option-base")
rv(ob, "config", "--mode", "milestone")
green(ob)
for cmd in (["packet", "--milestone", "m1"], ["skip", "--milestone", "m1", "--reason", "small"]):
    rc, out = rv(ob, *cmd, "--base=--output=leak.txt")
    check(f"{cmd[0]} refuses a --base that is a git option (exit 4)",
          rc == 4 and "must name a git ref" in out and not (ob / "leak.txt").exists(), out)
p_ = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_verify.py"), "--root", str(ob), "--tier", "fast",
                     "--base=--output=leak.txt"], capture_output=True, text=True)
check("dc_verify refuses a --base that is a git option",
      p_.returncode == 4 and "must name a git ref" in p_.stdout + p_.stderr and not (ob / "leak.txt").exists(),
      p_.stdout + p_.stderr)

# -- the per-agent findings cap --------------------------------------------------------------------
pc = make_repo("per-agent-cap")
rv(pc, "config", "--mode", "milestone")
green(pc)
rv(pc, "packet", "--milestone", "m1")
rv(pc, "run", "--round", "R1", "--role", "critic-a", "--model", "sonnet")
nine = "\n".join(finding(f"distinct claim number {i}", "for i in range(len(items) - 1):") for i in range(9))
rc, out = rv(pc, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-", stdin=nine)
check("a batch over max_findings_per_agent is cut and says so",
      rc == 0 and "9 in" in out and "TRUNCATED 1 over the per-agent cap" in out, out)

# -- an unreadable finder reply is not a clean review ----------------------------------------------
up = make_repo("unparsable")
rv(up, "config", "--mode", "milestone")
green(up)
rv(up, "packet", "--milestone", "m1")
rv(up, "run", "--round", "R1", "--role", "critic-a", "--model", "sonnet")
rc, out = rv(up, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-",
             stdin="I looked through the packet.\nOverall it seems fine.\n")
check("a reply with no parsable finding is refused (exit 6)", rc == 6 and "NOT-INGESTED" in out, out)
rc, out = rv(up, "finish", "--round", "R1")
check("the refused reply leaves the run un-ingested", rc == 6 and "never ingested: critic-a" in out, out)
one = json.loads(finding("loop stops one item early", "for i in range(len(items) - 1):"))
fenced = "```json\n[\n" + json.dumps(one, indent=2) + "\n]\n```\n"
rc, out = rv(up, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-", stdin=fenced)
check("a fenced, pretty-printed JSON array is parsed", rc == 0 and "1 new" in out, out)

# -- a reopened finding cannot resolve on its old ticket -----------------------------------------
rt = fixing_repo("reticket", finding("loop stops one item early", "for i in range(len(items) - 1):"))
rv(rt, "ticket", "--round", "R1", "--id", "r1", "--allow", "a.py", "--file", "-", stdin="TICKET r1\n")
(rt / "a.py").write_text(A_PY.replace("range(len(items) - 1)", "range(len(items))"), encoding="utf-8")
rv(rt, "scope", "--round", "R1")
rv(rt, "resolve", "--id", "r1", "--reopen", "--note", "re-check NOT-RESOLVED")
check("reopen clears the old ticket", side(rt)["findings"][0]["ticket"] == "", side(rt)["findings"][0])
rv(rt, "decide", "--set", "r1=fix")
rc, out = rv(rt, "resolve", "--id", "r1", "--fixed")
check("resolve --fixed after a reopen needs a new ticket", rc != 0 and "no ticket" in out, out)

# -- the fixer may not touch .agent/ ---------------------------------------------------------------
gd = fixing_repo("guard", finding("loop stops one item early", "for i in range(len(items) - 1):"))
rv(gd, "ticket", "--round", "R1", "--id", "r1", "--allow", "a.py", "--file", "-", stdin="TICKET r1\n")
(gd / "a.py").write_text(A_PY.replace("range(len(items) - 1)", "range(len(items))"), encoding="utf-8")
car = gd / ".agent" / "drainclamp-project.json"
raw = json.loads(car.read_text(encoding="utf-8"))
raw["review"]["config"]["mode"] = "off"
car.write_text(json.dumps(raw), encoding="utf-8")
rc, out = rv(gd, "scope", "--round", "R1")
check("a fixer edit to the review store is a SCOPE-BREACH (exit 4)", rc == 4 and ".agent/" in out, out)

# -- verify records: a real run after the fixer is no breach, a forged record is ---------------
FIX = A_PY.replace("range(len(items) - 1)", "range(len(items))")
vr = fixing_repo("records-rerun", finding("loop stops one item early", "for i in range(len(items) - 1):"))
dc_verify.save_record(vr / ".agent", "selftest", "sha256:abc", "pass", "s.log", dc_verify.tree_fingerprint(vr))
rv(vr, "ticket", "--round", "R1", "--id", "r1", "--allow", "a.py", "--file", "-", stdin="TICKET r1\n")
(vr / "a.py").write_text(FIX, encoding="utf-8")
rc, out = rv(vr, "scope", "--round", "R1")
check("scope ok after the fix", rc == 0 and "scope ok" in out, out)
for tier in ("fast", "milestone"):
    dc_verify.save_record(vr / ".agent", dc_verify.TIER_RECORD_PREFIX + tier, "tier", "pass", "verify.log",
                          dc_verify.tree_fingerprint(vr))
dc_verify.save_record(vr / ".agent", "selftest", "sha256:abc", "pass", "s.log", dc_verify.tree_fingerprint(vr))
rc, out = rv(vr, "scope", "--round", "R1")
check("a second scope after real dc_verify runs is not a breach (t3)", rc == 0 and "scope ok" in out, out)

fr = fixing_repo("records-forged", finding("loop stops one item early", "for i in range(len(items) - 1):"))
dc_verify.save_record(fr / ".agent", dc_verify.TIER_RECORD_PREFIX + "milestone", "tier", "fail", "verify.log",
                      dc_verify.tree_fingerprint(fr))
rv(fr, "ticket", "--round", "R1", "--id", "r1", "--allow", "a.py", "--file", "-", stdin="TICKET r1\n")
recs = json.loads((fr / ".agent" / dc_verify.RECORDS_NAME).read_text(encoding="utf-8"))
recs["tier:milestone"]["status"] = "pass"
(fr / ".agent" / dc_verify.RECORDS_NAME).write_text(json.dumps(recs), encoding="utf-8")
rc, out = rv(fr, "scope", "--round", "R1")
check("a record flipped to pass on an unchanged tree is a breach",
      rc == 4 and "verify-records.json (tier:milestone)" in out, out)
recs["tier:milestone"]["status"] = "fail"
(fr / ".agent" / dc_verify.RECORDS_NAME).write_text(json.dumps(recs), encoding="utf-8")
(fr / "a.py").write_text(FIX, encoding="utf-8")
recs["selftest"] = {"digest": "sha256:abc", "tree": recs["tier:milestone"]["tree"], "status": "pass",
                    "when": "2026-10-08T00:00:00+00:00", "log": ""}
(fr / ".agent" / dc_verify.RECORDS_NAME).write_text(json.dumps(recs), encoding="utf-8")
rc, out = rv(fr, "scope", "--round", "R1")
check("a record added for a tree that is not the current one is a breach",
      rc == 4 and "(selftest)" in out and "tier:milestone" not in out, out)

# -- gitignored writes are seen (t6) ---------------------------------------------------------------
ig = fixing_repo("ignored", finding("loop stops one item early", "for i in range(len(items) - 1):"))
(ig / ".gitignore").write_text("secrets/\n__pycache__/\n.coverage\nbuild/\n*.egg-info/\n", encoding="utf-8")
(ig / "secrets").mkdir()
(ig / "secrets" / "keep.env").write_text("A=1\n", encoding="utf-8")
(ig / "secrets" / "allowed.env").write_text("B=1\n", encoding="utf-8")
(ig / ".coverage").write_bytes(b"c1")
(ig / "build").mkdir()
(ig / "build" / "out.txt").write_text("1\n", encoding="utf-8")
(ig / "pkg.egg-info").mkdir()
(ig / "pkg.egg-info" / "PKG-INFO").write_text("1\n", encoding="utf-8")
rv(ig, "ticket", "--round", "R1", "--id", "r1", "--allow", "a.py;secrets/allowed.env", "--file", "-",
   stdin="TICKET r1\n")
check("the snapshot pins gitignored files outside .agent/",
      sorted((side(ig)["rounds"][0]["fix"].get("ignored") or {})) == ["secrets/allowed.env", "secrets/keep.env"],
      side(ig)["rounds"][0]["fix"].get("ignored"))
(ig / "a.py").write_text(FIX, encoding="utf-8")
(ig / "secrets" / "allowed.env").write_text("B=22\n", encoding="utf-8")
(ig / "__pycache__").mkdir()
(ig / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"cache")
(ig / ".coverage").write_bytes(b"coverage-run-2")
(ig / "build" / "out.txt").write_text("changed by a build\n", encoding="utf-8")
(ig / "pkg.egg-info" / "PKG-INFO").write_text("changed\n", encoding="utf-8")
rc, out = rv(ig, "scope", "--round", "R1")
check("an allowed gitignored file, test caches and build output are no breach", rc == 0 and "scope ok: 2 file(s)" in out, out)
rv(ig, "ticket", "--round", "R1", "--id", "r1", "--allow", "a.py", "--file", "-", stdin="TICKET r1\n")
(ig / "secrets" / "keep.env").write_text("A=changed\n", encoding="utf-8")
(ig / "secrets" / "new.env").write_text("C=1\n", encoding="utf-8")
rc, out = rv(ig, "scope", "--round", "R1")
check("a write into a gitignored path outside the ticket is a SCOPE-BREACH",
      rc == 4 and "secrets/keep.env" in out and "secrets/new.env" in out, out)

# -- a ticket stored after a breach does not hide it ---------------------------------------------
br = fixing_repo("breach-kept", finding("loop stops one item early", "for i in range(len(items) - 1):"),
                 finding("caller passes a literal list", "return parse([1, 2, 3])", line=9, sev="low",
                         cat="conformance"))
rv(br, "ticket", "--round", "R1", "--id", "r1", "--allow", "a.py", "--file", "-", stdin="TICKET r1\n")
(br / "a.py").write_text(A_PY.replace("range(len(items) - 1)", "range(len(items))"), encoding="utf-8")
(br / "b.py").write_text("X = 2\n", encoding="utf-8")
rc, out = rv(br, "scope", "--round", "R1")
check("a stray edit is a breach", rc == 4 and "b.py" in out, out)
rv(br, "ticket", "--round", "R1", "--id", "r2", "--allow", "a.py", "--file", "-", stdin="TICKET r2\n")
rc, out = rv(br, "scope", "--round", "R1")
check("a ticket stored after a breach keeps the breach", rc == 4 and "b.py" in out, out)

# -- an untracked cache the snapshot pinned is rewritten by the tests: no breach -------------------
pc = fixing_repo("pyc-pinned", finding("loop stops one item early", "for i in range(len(items) - 1):"))
(pc / "__pycache__").mkdir()
(pc / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"before")
rv(pc, "ticket", "--round", "R1", "--id", "r1", "--allow", "a.py", "--file", "-", stdin="TICKET r1\n")
check("the snapshot holds the untracked cache (not gitignored)",
      "__pycache__/a.cpython-312.pyc" in side(pc)["rounds"][0]["fix"]["files"], side(pc)["rounds"][0]["fix"]["files"])
(pc / "a.py").write_text(A_PY.replace("range(len(items) - 1)", "range(len(items))"), encoding="utf-8")
(pc / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"rebuilt by the tests")
rc, out = rv(pc, "scope", "--round", "R1")
check("a pinned untracked cache rewritten after the fix is no breach", rc == 0 and "scope ok: 1 file(s)" in out, out)
tb = fixing_repo("tracked-build", finding("loop stops one item early", "for i in range(len(items) - 1):"))
(tb / "build").mkdir()
(tb / "build" / "gen.py").write_text("G = 1\n", encoding="utf-8")
git(tb, "add", "build/gen.py"); git(tb, "commit", "-qm", "tracked build script")
rv(tb, "ticket", "--round", "R1", "--id", "r1", "--allow", "a.py", "--file", "-", stdin="TICKET r1\n")
(tb / "a.py").write_text(A_PY.replace("range(len(items) - 1)", "range(len(items))"), encoding="utf-8")
(tb / "build" / "gen.py").write_text("G = 2\n", encoding="utf-8")
rc, out = rv(tb, "scope", "--round", "R1")
check("a stray edit to a TRACKED file under build/ is a SCOPE-BREACH", rc == 4 and "build/gen.py" in out, out)

# -- files left out at the packet cap are not reviewed -------------------------------------------
lo = make_repo("left-out")
for n in range(4):
    (lo / f"f{n}.py").write_text("".join(f"V{i} = {i}\n" for i in range(200)), encoding="utf-8")
rv(lo, "config", "--mode", "milestone")
green(lo)
rv(lo, "packet", "--milestone", "m1", "--depth", "quick")
missed = side(lo)["rounds"][0].get("left_out") or []
check("the packet records the files it left out at the cap", len(missed) >= 1, side(lo)["rounds"][0])
rv(lo, "run", "--round", "R1", "--role", "critic-a", "--model", "sonnet")
rv(lo, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-", stdin="NO FINDINGS\n")
rc, out = rv(lo, "finish", "--round", "R1")
check("a round that left files out is not a pass (exit 6)", rc == 6 and "left out at the packet cap" in out, out)
green(lo)
rv(lo, "packet", "--milestone", "m1", "--depth", "quick", "--only", ";".join(missed))
rv(lo, "run", "--round", "R2", "--role", "critic-a", "--model", "sonnet")
rv(lo, "ingest", "--round", "R2", "--role", "critic-a", "--file", "-", stdin="NO FINDINGS\n")
rc, out = rv(lo, "finish", "--round", "R2")
check("a later round over the left-out files clears them", rc == 0 and "REVIEW-PASS" in out, out)

# -- two defects a line apart stay two findings ----------------------------------------------------
tw = make_repo("two-defects")
rv(tw, "config", "--mode", "milestone")
green(tw)
rv(tw, "packet", "--milestone", "m1")
rv(tw, "run", "--round", "R1", "--role", "critic-a", "--model", "sonnet")
pair = "\n".join([finding("loop stops one item early", "for i in range(len(items) - 1):"),
                  finding("total is not reset between calls", "total += items[i]", line=4)])
rc, out = rv(tw, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-", stdin=pair)
check("same category a line apart with different code stays two findings", rc == 0 and "2 new" in out, out)

# -- an untracked symlink is never inlined into the packet -----------------------------------------
sl = make_repo("symlink")
secret = tmp / "outside-secret.txt"
secret.write_text("TOP-SECRET-VALUE\n", encoding="utf-8")
try:
    os.symlink(secret, sl / "link.txt")
    linked = True
except (OSError, NotImplementedError):
    linked = False
if linked:
    green(sl)
    rc, out = rv(sl, "packet", "--milestone", "m1")
    pk = (sl / ".agent/review/R1/packet.md").read_text(encoding="utf-8")
    check("an untracked symlink's target is not inlined", rc == 0 and "TOP-SECRET-VALUE" not in pk, out)
else:
    print("SKIPPED: symlink check, this host cannot create symlinks")

# -- review off: a damaged sidecar does not block roadmap writes ---------------------------------
dg = make_repo("damaged-guard")
(dg / ".agent" / "drainclamp-project.json").write_text("{not json", encoding="utf-8")
rc, out = set_roadmap(dg, "done")
check("review never turned on: a damaged sidecar does not block a roadmap write", rc == 0, out)

# -- staleness counts every finished round, not only the last ------------------------------------
sr = make_repo("stale-rounds")
(sr / "c.py").write_text("C = 1\n", encoding="utf-8")
rv(sr, "config", "--mode", "milestone")
green(sr)
rv(sr, "packet", "--milestone", "m1")
rv(sr, "run", "--round", "R1", "--role", "critic-a", "--model", "sonnet")
rv(sr, "ingest", "--round", "R1", "--role", "critic-a", "--file", "-", stdin="NO FINDINGS\n")
rv(sr, "finish", "--round", "R1")
green(sr)
rv(sr, "packet", "--milestone", "m1", "--only", "a.py")
rv(sr, "run", "--round", "R2", "--role", "critic-a", "--model", "sonnet")
rv(sr, "ingest", "--round", "R2", "--role", "critic-a", "--file", "-", stdin="NO FINDINGS\n")
rv(sr, "finish", "--round", "R2")
(sr / "c.py").write_text("C = 2\n", encoding="utf-8")
code, out = rv(sr, "check", "--milestone", "m1")
check("an edit to a file only an earlier round reviewed is STALE (exit 5)", code == 5 and "c.py" in out, out)

print()
# Packet-time evidence and complete filename inventory define review freshness.
from unittest.mock import patch

def clean_round(name):
    fresh = make_repo(name)
    rv(fresh, 'config', '--mode', 'milestone')
    green(fresh)
    rc, out = rv(fresh, 'packet', '--milestone', 'm1')
    check(name + ' packet created', rc == 0, out)
    rv(fresh, 'run', '--round', 'R1', '--role', 'critic-a', '--model', 'test')
    rv(fresh, 'ingest', '--round', 'R1', '--role', 'critic-a', '--file', '-', stdin='NO FINDINGS\n')
    return fresh

inventory_root = clean_round('inventory-new')
rc, out = rv(inventory_root, 'finish', '--round', 'R1')
check('fresh round finishes', rc == 0, out)
(inventory_root / '.agent' / 'cache').mkdir()
(inventory_root / '.agent' / 'cache' / 'noise').write_bytes(b'noise')
rc, out = rv(inventory_root, 'check', '--milestone', 'm1')
check('agent cache noise does not stale review', rc == 0, out)
(inventory_root / '__pycache__').mkdir()
(inventory_root / '__pycache__' / 'a.cpython-312.pyc').write_bytes(b'built by a test run')
rc, out = rv(inventory_root, 'check', '--milestone', 'm1')
check('an untracked cache appearing after review does not stale it', rc == 0, out)
(inventory_root / "build").mkdir()
(inventory_root / "build" / "gen.py").write_text("G = 1\n", encoding="utf-8")
git(inventory_root, "add", "build/gen.py")
rc, out = rv(inventory_root, "check", "--milestone", "m1")
check("a new tracked file under build/ after review is STALE", rc == 5 and "build/gen.py" in out, out)
git(inventory_root, "rm", "-q", "--cached", "build/gen.py")
(inventory_root / "build" / "gen.py").unlink(); (inventory_root / "build").rmdir()
new_source = inventory_root / 'nueva_漢.py'
new_source.write_text('X = 1\n', encoding='utf-8')
rc, out = rv(inventory_root, 'check', '--milestone', 'm1')
check('new Unicode filename after review is STALE', rc == 5 and 'STALE' in out, out)
rc, out = set_roadmap(inventory_root, 'done')
check('new filename holds roadmap close', rc == 5 and 'STALE' in out, out)
new_source.unlink()
(inventory_root / 'b.py').unlink()
rc, out = rv(inventory_root, 'check', '--milestone', 'm1')
check('deleted tracked filename outside packet is STALE', rc == 5 and 'STALE' in out, out)

for mutation in ('existing', 'addition', 'deletion'):
    during = clean_round('during-' + mutation)
    if mutation == 'existing':
        (during / 'a.py').write_text(A_PY + '# changed after packet\n', encoding='utf-8')
    elif mutation == 'addition':
        (during / 'nueva_漢.py').write_text('X = 2\n', encoding='utf-8')
    else:
        (during / 'b.py').unlink()
    rc, out = rv(during, 'finish', '--round', 'R1')
    check('finish rejects unticketed ' + mutation + ' after packet',
          rc == 5 and 'STALE' in out and not side(during)['rounds'][0]['finished'], out)

for after_scope in (False, True):
    scoped = fixing_repo('fresh-scoped-' + str(after_scope),
                         finding('loop stops one item early', 'for i in range(len(items) - 1):'))
    rv(scoped, 'ticket', '--round', 'R1', '--id', 'r1', '--allow', 'a.py', '--file', '-',
       stdin='TICKET r1\n')
    (scoped / 'a.py').write_text(FIX, encoding='utf-8')
    rc, out = rv(scoped, 'scope', '--round', 'R1')
    check('genuine fix scope succeeds ' + str(after_scope), rc == 0, out)
    check('scope pins the accepted bytes ' + str(after_scope),
          (side(scoped)['rounds'][0]['fix'].get('checked_hashes') or {}).get('a.py')
          == dc_review._hash(scoped, 'a.py'))
    rv(scoped, 'resolve', '--id', 'r1', '--fixed')
    if after_scope:
        (scoped / 'a.py').write_text(FIX + '# edit after scope\n', encoding='utf-8')
    rc, out = rv(scoped, 'finish', '--round', 'R1')
    check('finish validates scoped bytes ' + str(after_scope),
          (rc == 5 and 'STALE' in out) if after_scope else (rc == 0 and 'REVIEW-PASS' in out), out)

legacy = clean_round('legacy-unfinished')
legacy_path = legacy / '.agent' / 'drainclamp-project.json'
legacy_data = json.loads(legacy_path.read_text(encoding='utf-8'))
legacy_data['review']['rounds'][0].pop('packet_stamp', None)
legacy_path.write_text(json.dumps(legacy_data), encoding='utf-8')
rc, out = rv(legacy, 'finish', '--round', 'R1')
check('legacy round without packet hashes refuses finish', rc == 5 and 'STALE' in out, out)
finished_legacy = clean_round('legacy-finished')
rv(finished_legacy, 'finish', '--round', 'R1')
legacy_path = finished_legacy / '.agent' / 'drainclamp-project.json'
legacy_data = json.loads(legacy_path.read_text(encoding='utf-8'))
legacy_data['review']['rounds'][0].pop('inventory', None)
legacy_path.write_text(json.dumps(legacy_data), encoding='utf-8')
rc, out = rv(finished_legacy, 'check', '--milestone', 'm1')
check('legacy finished inventory cannot falsely pass', rc in (5, 6) and 'REVIEW-PASS' not in out, out)
with patch.object(dc_review.subprocess, 'run', side_effect=OSError('git unavailable')):
    try:
        dc_review._inventory(finished_legacy)
        inventory_closed = False
    except dc_review.DcError:
        inventory_closed = True
check('inventory fails closed when Git unavailable', inventory_closed)

# A scoped round cannot absorb a newly introduced, unread source filename.
coverage_root = clean_round('filename-anchor')
rv(coverage_root, 'finish', '--round', 'R1')
(coverage_root / 'new.py').write_text('N = 1\n', encoding='utf-8')
green(coverage_root)
rv(coverage_root, 'packet', '--milestone', 'm1', '--only', 'a.py')
rv(coverage_root, 'run', '--round', 'R2', '--role', 'critic-a', '--model', 'test')
rv(coverage_root, 'ingest', '--round', 'R2', '--role', 'critic-a', '--file', '-', stdin='NO FINDINGS\n')
rc, out = rv(coverage_root, 'finish', '--round', 'R2')
check('scoped round does not absorb unread new filename', rc in (5, 6) and 'REVIEW-PASS' not in out, out)
rc, out = rv(coverage_root, 'check', '--milestone', 'm1')
check('unread new filename still holds check', rc in (5, 6) and 'REVIEW-PASS' not in out, out)
if not side(coverage_root)['rounds'][-1].get('finished'):
    rv(coverage_root, 'abort', '--round', 'R2', '--reason', 'review omitted new file')
green(coverage_root)
rv(coverage_root, 'packet', '--milestone', 'm1', '--only', 'new.py')
rv(coverage_root, 'run', '--round', 'R3', '--role', 'critic-a', '--model', 'test')
rv(coverage_root, 'ingest', '--round', 'R3', '--role', 'critic-a', '--file', '-', stdin='NO FINDINGS\n')
rc, out = rv(coverage_root, 'finish', '--round', 'R3')
check('round actually reviewing new file clears coverage', rc == 0 and 'REVIEW-PASS' in out, out)
(coverage_root / 'new.py').write_text('N = 2\n', encoding='utf-8')
rc, out = rv(coverage_root, 'check', '--milestone', 'm1')
check('reviewed new filename remains content-stamped', rc == 5 and 'STALE' in out, out)

# Each successful fixer batch remains evidence when a later ticket starts.
for edit_earlier in (False, True):
    batches = fixing_repo('accepted-batches-' + str(edit_earlier),
                          finding('loop stops early', 'for i in range(len(items) - 1):'),
                          finding('constant value is wrong', 'X = 1', file='b.py', line=1))
    rv(batches, 'ticket', '--round', 'R1', '--id', 'r1', '--allow', 'a.py', '--file', '-',
       stdin='TICKET r1\n')
    (batches / 'a.py').write_text(FIX, encoding='utf-8')
    rv(batches, 'scope', '--round', 'R1')
    rv(batches, 'resolve', '--id', 'r1', '--fixed')
    rv(batches, 'ticket', '--round', 'R1', '--id', 'r2', '--allow', 'b.py', '--file', '-',
       stdin='TICKET r2\n')
    (batches / 'b.py').write_text('X = 2\n', encoding='utf-8')
    rc, out = rv(batches, 'scope', '--round', 'R1')
    check('second independent fix scope succeeds ' + str(edit_earlier), rc == 0, out)
    rv(batches, 'resolve', '--id', 'r2', '--fixed')
    if edit_earlier:
        (batches / 'a.py').write_text(FIX + '# unchecked later edit\n', encoding='utf-8')
    rc, out = rv(batches, 'finish', '--round', 'R1')
    check('finish honors cumulative checked batches ' + str(edit_earlier),
          (rc == 5 and 'STALE' in out) if edit_earlier else (rc == 0 and 'REVIEW-PASS' in out), out)

# BLOCKED fixes may close by a user disposition when their bytes never changed.
for disposition in ('waive', 'dismiss'):
    blocked_ticket = fixing_repo('closed-ticket-' + disposition,
                                finding('loop stops early', 'for i in range(len(items) - 1):'))
    rv(blocked_ticket, 'ticket', '--round', 'R1', '--id', 'r1', '--allow', 'a.py', '--file', '-',
       stdin='TICKET r1\n')
    rv(blocked_ticket, 'scope', '--round', 'R1')
    rv(blocked_ticket, 'decide', '--set', 'r1=' + disposition + ':user accepts unchanged code')
    rc, out = rv(blocked_ticket, 'finish', '--round', 'R1')
    check('unchanged ticket can finish after user ' + disposition,
          rc == 0 and 'REVIEW-PASS' in out, out)
waived_change = fixing_repo('changed-waived-ticket',
                           finding('loop stops early', 'for i in range(len(items) - 1):'))
rv(waived_change, 'ticket', '--round', 'R1', '--id', 'r1', '--allow', 'a.py', '--file', '-',
   stdin='TICKET r1\n')
(waived_change / 'a.py').write_text(FIX, encoding='utf-8')
rv(waived_change, 'scope', '--round', 'R1')
rv(waived_change, 'decide', '--set', 'r1=waive:user accepts original behavior')
rc, out = rv(waived_change, 'finish', '--round', 'R1')
check('changed waived-only path is never blessed', rc == 5 and 'STALE' in out, out)

for unrelated in (False, True):
    addition = fixing_repo('fixed-addition-' + str(unrelated),
                          finding('loop stops early', 'for i in range(len(items) - 1):'))
    rv(addition, 'ticket', '--round', 'R1', '--id', 'r1', '--allow', 'a.py;new_test.py',
       '--file', '-', stdin='TICKET r1\n')
    (addition / 'a.py').write_text(FIX, encoding='utf-8')
    (addition / 'new_test.py').write_text('def test_sum():\n    assert 1 + 2 == 3\n', encoding='utf-8')
    rc, out = rv(addition, 'scope', '--round', 'R1')
    check('fixed-file addition passes scope ' + str(unrelated), rc == 0, out)
    rv(addition, 'resolve', '--id', 'r1', '--fixed')
    if unrelated:
        (addition / 'unrelated.py').write_text('U = 1\n', encoding='utf-8')
    rc, out = rv(addition, 'finish', '--round', 'R1')
    check('finish accepts only checked fixed additions ' + str(unrelated),
          (rc == 5 and 'STALE' in out) if unrelated else (rc == 0 and 'REVIEW-PASS' in out), out)

# A later checked batch for the same path supersedes its earlier checked hash.
shared_batch = fixing_repo('same-path-batches',
                          finding('loop stops early', 'for i in range(len(items) - 1):'),
                          finding('caller uses incomplete example', 'return parse([1, 2, 3])', line=9))
for fid, body in (('r1', FIX), ('r2', FIX.replace('parse([1, 2, 3])', 'parse([1, 2, 3, 4])'))):
    rv(shared_batch, 'ticket', '--round', 'R1', '--id', fid, '--allow', 'a.py', '--file', '-',
       stdin='TICKET ' + fid + '\n')
    (shared_batch / 'a.py').write_text(body, encoding='utf-8')
    rv(shared_batch, 'scope', '--round', 'R1')
    rv(shared_batch, 'resolve', '--id', fid, '--fixed')
archived = side(shared_batch)['rounds'][0].get('accepted_fix_batches') or []
check('earlier scope evidence is archived independently of current batch',
      len(archived) == 1 and archived[0].get('allow') == {'r1': ['a.py']}
      and archived[0].get('modified') == ['a.py']
      and archived[0].get('checked_hashes', {}).get('a.py') != dc_review._hash(shared_batch, 'a.py'))
rc, out = rv(shared_batch, 'finish', '--round', 'R1')
check('latest validated hash wins for a shared fixed path', rc == 0 and 'REVIEW-PASS' in out, out)

# Archived evidence cannot be rewritten after it is accepted by the latest scope.
archive_guard = fixing_repo('archive-guard',
                           finding('loop stops early', 'for i in range(len(items) - 1):'),
                           finding('constant value is wrong', 'X = 1', file='b.py', line=1))
for fid, filename, body in (('r1', 'a.py', FIX), ('r2', 'b.py', 'X = 2\n')):
    rv(archive_guard, 'ticket', '--round', 'R1', '--id', fid, '--allow', filename, '--file', '-',
       stdin='TICKET ' + fid + '\n')
    (archive_guard / filename).write_text(body, encoding='utf-8')
    rv(archive_guard, 'scope', '--round', 'R1')
    rv(archive_guard, 'resolve', '--id', fid, '--fixed')
archive_path = archive_guard / '.agent' / 'drainclamp-project.json'
archive_data = json.loads(archive_path.read_text(encoding='utf-8'))
archive_batches = archive_data['review']['rounds'][0].get('accepted_fix_batches') or []
if archive_batches:
    archive_batches[0]['checked_hashes']['a.py'] = 'forged archived hash'
    archive_path.write_text(json.dumps(archive_data), encoding='utf-8')
    rc, out = rv(archive_guard, 'finish', '--round', 'R1')
    archive_refused = rc == 5 and 'STALE' in out
else:
    archive_refused = False
    out = 'no archived evidence'
check('finish refuses archived evidence changed after scope', archive_refused, out)

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
