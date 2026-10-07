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
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
