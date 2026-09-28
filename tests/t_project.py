"""Sidecar checks for dc_project.py: chunks, errors, to-dos, time, savings, capsule."""
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

# 6. savings: formula and payback, rate falls back to the global config
home.mkdir(parents=True, exist_ok=True)
(home / "config.json").write_text(json.dumps({"hourly_rate": 50}), encoding="utf-8")
rc, out = proj(repo, "savings", "set", "--baseline-min", "60", "--new-min", "15",
               "--runs-per-year", "26")
check("savings set succeeds", rc == 0, out.strip())
rc, out = proj(repo, "summary", "--json")
s = json.loads(out)
check("annual hours saved = (60-15)*26/60", abs(s["savings"]["annual_hours"] - 19.5) < 1e-9,
      s["savings"])
check("annual cost saved uses the global rate", abs(s["savings"]["annual_cost"] - 975) < 1e-9,
      s["savings"])
check("build hours include manual time", s["build_minutes"] >= 90, s["build_minutes"])
check("payback is build cost over annual saving", s["savings"]["payback_years"] is not None)
check("summary counts chunks", s["chunks"] == {"done": 1, "total": 2}, s["chunks"])
check("summary counts open errors", s["errors_open"] == 0, s["errors_open"])

proj(repo, "savings", "set", "--baseline-min", "60", "--new-min", "15",
     "--runs-per-year", "26", "--rate", "100")
s = json.loads(proj(repo, "summary", "--json")[1])
check("per-project rate overrides the global one",
      abs(s["savings"]["annual_cost"] - 1950) < 1e-9, s["savings"])

# 7. a damaged or future sidecar is refused, never overwritten
side.write_text("{broken", encoding="utf-8")
rc, out = proj(repo, "todo", "add", "--text", "x")
check("corrupt sidecar refuses writes", rc != 0 and "sidecar" in out.lower(), out.strip())
check("corrupt sidecar is left as found", side.read_text(encoding="utf-8") == "{broken")
side.write_text(json.dumps({"schema": 99}), encoding="utf-8")
rc, out = proj(repo, "summary")
check("unknown schema is refused", rc != 0 and "schema" in out, out.strip())

# 8. every write bumps the revision so the board can detect a stale edit
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

# 9. profile: summary, features, safeguards by area
rc, out = proj(repo, "profile", "summary", "--text", "One place for every KPI")
check("summary saved", rc == 0, out.strip())
proj(repo, "feature", "add", "--name", "Dashboard", "--what", "shows KPIs")
rc, out = proj(repo, "feature", "edit", "--id", "f1", "--what", "shows every KPI")
data = json.loads(side.read_text(encoding="utf-8"))
check("feature added and edited", data["profile"]["features"] ==
      [{"id": "f1", "name": "Dashboard", "what": "shows every KPI", "priority": ""}],
      data["profile"])
rc, out = proj(repo, "safeguard", "add", "--area", "Security", "--control", "binds 127.0.0.1",
               "--prevents", "remote access", "--status", "in")
check("safeguard added", rc == 0, out.strip())
rc, out = proj(repo, "safeguard", "add", "--area", "Nope", "--control", "x")
check("unknown safeguard area refused", rc != 0)
proj(repo, "safeguard", "add", "--area", "Testing", "--control", "selftest on close")
s = json.loads(proj(repo, "summary", "--json")[1])
check("summary counts safeguards in and planned", s["safeguards"] == {"in": 1, "plan": 1},
      s["safeguards"])
proj(repo, "safeguard", "edit", "--id", "g2", "--status", "in")
data = json.loads(side.read_text(encoding="utf-8"))
check("safeguard edit changes only what was given",
      data["profile"]["safeguards"][1]["status"] == "in" and
      data["profile"]["safeguards"][1]["control"] == "selftest on close")
proj(repo, "feature", "delete", "--id", "f1")
check("feature deleted", json.loads(side.read_text(encoding="utf-8"))["profile"]["features"] == [])

# 10. health with a dated update, kept as history
rc, out = proj(repo, "health", "set", "--state", "risk", "--update", "waiting on data")
proj(repo, "health", "set", "--state", "on", "--update", "data arrived")
data = json.loads(side.read_text(encoding="utf-8"))
check("health holds the latest update", data["health"]["state"] == "on" and
      data["health"]["update"] == "data arrived" and data["health"]["at"], data["health"])
check("updates keep the history", [u["state"] for u in data["updates"]] == ["risk", "on"])
rc, out = proj(repo, "health", "set", "--state", "green")
check("unknown health refused", rc != 0)

# 11. runs: realised savings from real use
proj(repo, "savings", "set", "--baseline-min", "60", "--new-min", "15",
     "--runs-per-year", "26", "--rate", "100")
proj(repo, "run", "add", "--count", "4")
proj(repo, "run", "add", "--minutes", "30", "--note", "slow week")
s = json.loads(proj(repo, "summary", "--json")[1])["savings"]
check("realised hours = (4x45 + 1x30)/60", abs(s["realised_hours"] - 3.5) < 1e-9, s)
check("realised cost uses the rate", abs(s["realised_cost"] - 350) < 1e-9, s)
check("realised runs counted", s["realised_runs"] == 5, s)
rc, out = proj(repo, "run", "add", "--count", "0")
check("zero runs refused", rc != 0)

# 12. seeding from a charter fills blanks only and marks safeguards planned
d = dc_project.empty()
charter = {"purpose": "Track KPIs",
           "scope": [{"feature": "Board", "what": "view", "priority": "Must"},
                     {"feature": "Mobile", "what": "app", "priority": "Won't"}],
           "security": "- local only\n- token per run", "retention": "logs kept 30 days",
           "risks": [{"risk": "stale data", "guardrail": "show refresh time"}],
           "never": "email anyone", "approvals": "before sending", "acceptance": "totals match"}
n = dc_project.seed_profile(d, charter)
areas = [g["area"] for g in d["profile"]["safeguards"]]
check("seed summary from purpose", d["profile"]["summary"] == "Track KPIs")
check("seed skips Won't features", [f["name"] for f in d["profile"]["features"]] == ["Board"])
check("seed safeguards by area", areas == ["Security", "Security", "Data retention",
                                           "Guardrails", "Guardrails", "Confirmation checks",
                                           "Testing"], areas)
check("seeded safeguards start planned",
      all(g["status"] == "plan" for g in d["profile"]["safeguards"]) and n == 8, n)
d["profile"]["summary"] = "hand written"
check("reseed adds nothing over hand edits",
      dc_project.seed_profile(d, charter) == 0 and d["profile"]["summary"] == "hand written")

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
rc, out = proj(bnd, "chunk", "done", "--milestone", "m1", "--id", "c2")
check("disjoint next chunk recommends a purge",
      "PURGE (chunk overlap 0%)" in out and "Context purge recommended." in out, out)
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

# -- board status, tasks, outcome, rate (m11) --------------------------------------
flowrepo = make_repo("flowrepo", "| m1 | Wait for the user to run a live test | a.py | active |")
done_repo = make_repo("donerepo", "| m1 | ship | a.py | done |")
d = dc_project.empty()
rows = dc_project.roadmap(flowrepo)
st = dc_project.board_status(rows, d, "active")
check("wording on the current step is only a guess", st == {"state": "need", "guessed": True}, st)
dc_project.apply_op(d, flowrepo, "flow.set", {"state": "run"})
st = dc_project.board_status(rows, d, "active")
check("an explicit flow beats the guess", st["state"] == "run" and not st["guessed"], st)
dc_project.apply_op(d, flowrepo, "flow.set", {"state": "auto"})
check("flow auto hands it back to the guess", d["flow"] is None)
try:
    dc_project.apply_op(d, flowrepo, "flow.set", {"state": "later"})
    check("unknown flow state refused", False)
except dc_project.DcError:
    check("unknown flow state refused", True)
check("registry complete wins over everything",
      dc_project.board_status(rows, d, "complete")["state"] == "done")
dd = dc_project.empty()
drows = dc_project.roadmap(done_repo)
check("finished roadmap with no open task is ready to close",
      dc_project.board_status(drows, dd, "active") == {"state": "close", "guessed": False})
msg = dc_project.apply_op(dd, done_repo, "task.add", {"text": "add CSV export", "owner": "you"})
check("task.add records owner and sets flow", dd["todos"][-1]["owner"] == "you" and
      dd["flow"]["state"] == "need", (msg, dd["todos"], dd["flow"]))
check("a finished roadmap with an open task is back in progress",
      dc_project.board_status(drows, dd, "active")["state"] == "need")
check("next step falls back to the first open task", dc_project.next_step(drows, dd) == "add CSV export")
dc_project.apply_op(dd, done_repo, "todo.done", {"id": dd["todos"][-1]["id"]})
check("ticking your last task clears the status the task set", dd["flow"] is None and
      dc_project.board_status(drows, dd, "active")["state"] == "close", dd["flow"])
dc_project.apply_op(dd, done_repo, "todo.undo", {"id": dd["todos"][-1]["id"]})
dc_project.apply_op(dd, done_repo, "flow.set", {"state": "need"})
dc_project.apply_op(dd, done_repo, "todo.done", {"id": dd["todos"][-1]["id"]})
check("a status set by hand survives ticking tasks", (dd["flow"] or {}).get("state") == "need", dd["flow"])
dc_project.apply_op(dd, done_repo, "todo.undo", {"id": dd["todos"][-1]["id"]})
dd["flow"] = None
check("an open task the person owns means waiting on them",
      dc_project.board_status(drows, dd, "active") == {"state": "need", "guessed": False})
try:
    dc_project.apply_op(dd, done_repo, "task.add", {"text": "x", "owner": "boss"})
    check("unknown task owner refused", False)
except dc_project.DcError:
    check("unknown task owner refused", True)
dc_project.apply_op(dd, done_repo, "outcome.set", {"text": "Leave report runs itself", "by": "claude"})
check("a Claude draft is saved but never confirmed",
      dd["profile"]["summary"] == "Leave report runs itself" and dd["outcome"]["confirmed"] is False)
dc_project.apply_op(dd, done_repo, "outcome.set", {"by": "user", "confirmed": True})
check("confirming keeps the text", dd["profile"]["summary"] == "Leave report runs itself"
      and dd["outcome"]["by"] == "user" and dd["outcome"]["confirmed"] is True, dd["outcome"])
check("new ops are listed for the board", {"flow.set", "task.add", "outcome.set"} <= set(dc_project.OPS))
side = dc_project.empty()
side.update({"flow": "bad"})
try:
    dc_project._validate(side)
    check("a non-object flow is refused", False)
except dc_project.DcError:
    check("a non-object flow is refused", True)

rate_home = tmp / "ratehome"
rate_home.mkdir()
(rate_home / dc_project.CONFIG_NAME).write_text(json.dumps({"project_roots": ["x"]}), encoding="utf-8")
dc_project.set_global_rate(str(rate_home), 55)
cfg = json.loads((rate_home / dc_project.CONFIG_NAME).read_text(encoding="utf-8"))
check("global rate saved beside other config", cfg == {"project_roots": ["x"], "hourly_rate": 55.0}, cfg)
check("global rate reads back", dc_project.global_rate(str(rate_home)) == 55.0)
for bad in (-1, "55", True):
    try:
        dc_project.set_global_rate(str(rate_home), bad)
        check(f"bad rate {bad!r} refused", False)
    except dc_project.DcError:
        check(f"bad rate {bad!r} refused", True)
dc_project.set_global_rate(str(rate_home), None)
check("rate can be cleared", dc_project.global_rate(str(rate_home)) is None)
dc_project.set_global_rate(str(rate_home), 60)
vd = dc_project.empty()
vd["savings"] = {"baseline_min": 90, "new_min": 10, "runs_per_year": 26}
vd["time"] = [{"id": "s1", "start": "2026-09-01T10:00:00+00:00", "end": "2026-09-01T16:00:00+00:00",
               "source": "auto", "note": ""}]
s1 = dc_project.savings(vd, str(rate_home))
s2 = dc_project.savings(vd, str(rate_home), you_minutes=90)
check("build cost defaults to the sidecar's own time",
      s1["build_minutes"] == 360 and s1["build_cost"] == 360.0, s1)
check("measured hands-on time replaces it when given", s2["build_minutes"] == 90 and
      s2["build_cost"] == 90.0 and round(s2["payback_years"], 4) == round(90 / (80 * 26), 4), s2)
freq_repo = make_repo("freq", "| m1 | fortnightly payroll export, runs fortnightly | a.py | active |")
check("run frequency guessed from the project's wording",
      dc_project.guess_frequency(freq_repo) == "fortnightly")
check("no wording, no guess", dc_project.guess_frequency(done_repo) is None)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
