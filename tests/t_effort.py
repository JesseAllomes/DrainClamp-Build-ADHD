"""Your-time vs agent-time: the 10-minute rule, attribution order, the Unassigned pool."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
HOME = tempfile.mkdtemp(prefix="dcreg-")
os.environ["DRAINCLAMP_HOME"] = HOME  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import dc_effort  # noqa: E402
import dc_registry  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


tmp = Path(tempfile.mkdtemp(prefix="dceffort-"))
parent = tmp / "work"                 # a folder holding other projects
alpha, beta = parent / "alpha", parent / "beta"
loner = tmp / "loner"                 # a project nothing else sits inside
SECRET = "hunter2-do-not-print"

for root, stamps in ((parent, []), (alpha, ["2026-09-01T10:00:00+00:00", "2026-09-01T11:00:00+00:00"]),
                     (beta, ["2026-09-02T10:00:00+00:00"]), (loner, [])):
    write(root / ".agent" / "drainclamp-state.md",
          ["<!-- drainclamp-build: state; schema=1; generation=1 -->"] + [f"- {s} abcd1234 work" for s in stamps])
    dc_registry.touch(root, HOME)
ids = {r.name: dc_registry.project_id(r) for r in (parent, alpha, beta, loner)}

claude, codex, grok = tmp / "claude", tmp / "codex", tmp / "grok"


def cl(kind, ts, content=None, cwd=str(parent), **extra):
    r = {"type": kind, "timestamp": ts, "cwd": cwd, **extra}
    if content is not None:
        r["message"] = {"content": content}
    return json.dumps(r)


# Claude, cwd = parent folder, inside alpha's window (10:00-11:00 +-30 min):
#  prompt 1 at 10:05 (no earlier output -> 10 min), agent answers 10:07
#  tool result at 10:08 (not a prompt), agent 10:09
#  prompt 2 at 10:12 (gap 3 min since 10:09 -> 3 min), agent 10:30
#  prompt 3 at 12:00 (outside the window, parent cwd -> Unassigned, gap 90 -> 10 min)
write(claude / "proj" / "s1.jsonl", [
    cl("user", "2026-09-01T10:05:00Z", "fix the thing"),
    cl("assistant", "2026-09-01T10:07:00Z", [{"type": "text", "text": f"ok {SECRET}"}]),
    cl("user", "2026-09-01T10:08:00Z", [{"type": "tool_result", "content": "x"}]),
    cl("assistant", "2026-09-01T10:09:00Z", [{"type": "text", "text": "ok"}]),
    cl("user", "2026-09-01T10:12:00Z", f"next {SECRET}"),
    cl("assistant", "2026-09-01T10:30:00Z", [{"type": "text", "text": "done"}]),
    cl("user", "2026-09-01T12:00:00Z", "<command-name>/clear</command-name>"),
    cl("user", "2026-09-01T12:00:30Z", f"look at {str(beta)} and {str(beta)}/x and {str(beta)}/y"),
    cl("assistant", "2026-09-01T12:04:00Z", [{"type": "text", "text": "ok"}]),
    cl("user", "2026-09-01T12:05:00Z", "sidechain", isSidechain=True),
])
# Codex, cwd = loner (no windows) -> by working folder. Two prompts: 10 min, then 2 min.
write(codex / "2026" / "09" / "rollout-a.jsonl", [
    json.dumps({"timestamp": "2026-09-05T09:00:00Z", "type": "session_meta", "payload": {"cwd": str(loner)}}),
    json.dumps({"timestamp": "2026-09-05T09:00:05Z", "type": "event_msg", "payload": {"type": "user_message", "message": "go"}}),
    json.dumps({"timestamp": "2026-09-05T09:04:00Z", "type": "event_msg", "payload": {"type": "task_complete"}}),
    json.dumps({"timestamp": "2026-09-05T09:06:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "again"}}),
    json.dumps({"timestamp": "2026-09-05T09:07:00Z", "type": "event_msg", "payload": {"type": "agent_message"}}),
])
# Grok, cwd = home-like folder, inside beta's window -> beta. One turn of 5 minutes of agent time.
g = grok / "ws" / "sess1"
write(g / "summary.json", [json.dumps({"info": {"cwd": str(tmp)}, "generated_title": "Grok session title"})])
write(g / "chat_history.jsonl", [json.dumps({"type": "user", "content": "hi"})])
write(g / "events.jsonl", [
    json.dumps({"ts": "2026-09-02T10:10:00Z", "type": "turn_started"}),
    json.dumps({"ts": "2026-09-02T10:15:00Z", "type": "turn_ended"}),
])
write(grok / "ws" / "sess1" / "subagents" / "c" / "events.jsonl",
      [json.dumps({"ts": "2026-09-02T10:10:00Z", "type": "turn_started"})])

src = {"claude": claude, "codex": codex, "grok": grok}
raw = dc_effort.compute(HOME, src)
P = raw["projects"]

check("scanned all three hosts once each", raw["scanned"] == {"claude": 1, "codex": 1, "grok": 1}, raw["scanned"])
a = P.get(ids["alpha"], {})
check("window beats a parent cwd: two prompts on alpha", a.get("prompts") == 2, a)
check("first prompt with no earlier output counts the 10-min cap", a.get("you_min") == 13.0, a.get("you_min"))
check("agent time = prompt to last output in the turn", a.get("agent_min") == 4.0 + 18.0, a.get("agent_min"))
check("tool results, commands and sidechains are not prompts", a.get("hosts") == {"claude": 13.0}, a.get("hosts"))
lo = P.get(ids["loner"], {})
check("codex by working folder: 10 + 2 min", lo.get("you_min") == 12.0 and lo.get("prompts") == 2, lo)
b = P.get(ids["beta"], {})
check("grok turn attributed by beta's window", b.get("prompts") == 1 and b.get("agent_min") == 5.0, b)
check("grok subagents skipped", raw["scanned"]["grok"] == 1)
check("a parent folder never claims a session by cwd", ids["work"] not in P, list(P))
pool = raw["unassigned"]
check("the out-of-window prompt is Unassigned", len(pool) == 1 and pool[0]["prompts"] == 1, pool)
check("unassigned prompt counts min(gap, 10)", pool and pool[0]["you_min"] == 10.0, pool and pool[0]["you_min"])
check("suggestion = most-named non-parent root", pool and pool[0]["suggest"] == ids["beta"], pool and pool[0]["suggest"])
check("title is the first prompt, clipped", pool and pool[0]["title"].startswith("fix the thing"), pool and pool[0]["title"])

# cache + assignments
v = dc_effort.refresh(HOME, src)
check("refresh writes the cache", dc_effort.cache_path(HOME).is_file())
check("view totals the pool", v["unassigned_total"]["prompts"] == 1 and v["unassigned_total"]["you_min"] == 10.0,
      v["unassigned_total"])
dc_effort.assign(HOME, {pool[0]["key"]: ids["beta"]})
v2 = dc_effort.view(HOME)
check("assigning moves the minutes onto the project", v2["projects"][ids["beta"]]["you_min"] == 20.0,
      v2["projects"][ids["beta"]])
check("assigned sessions leave the pool", v2["unassigned"] == [] and v2["unassigned_total"]["prompts"] == 0)
check("cache itself is untouched by assignment", json.loads(dc_effort.cache_path(HOME).read_text())["unassigned"] != [])
dc_effort.assign(HOME, {pool[0]["key"]: None})
check("clearing an assignment returns it to the pool", len(dc_effort.view(HOME)["unassigned"]) == 1)

# CLI
env = {**os.environ, "DRAINCLAMP_HOME": HOME}
out = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_effort.py"), "show"],
                     capture_output=True, text=True, env=env)
check("show prints totals", out.returncode == 0 and "Unassigned" in out.stdout, out.stderr[-300:])
out = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_effort.py"), "show", "--json"],
                     capture_output=True, text=True, env=env)
check("only the clipped first prompt is kept; later text never reaches the output",
      "fix the thing" in out.stdout and SECRET not in out.stdout and SECRET not in dc_effort.cache_path(HOME).read_text())
out = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_effort.py"), "accept"],
                     capture_output=True, text=True, env=env)
check("accept assigns suggestions", out.returncode == 0 and "1 sessions" in out.stdout, out.stdout + out.stderr)
check("empty sources compute to nothing", dc_effort.compute(HOME, {"claude": tmp / "none"})["projects"] == {})
bad = Path(HOME) / dc_effort.CACHE_NAME
bad.write_text("{not json", encoding="utf-8")
try:
    dc_effort.view(HOME)
    check("a damaged cache is refused, not replaced", False)
except dc_effort.DcError:
    check("a damaged cache is refused, not replaced", bad.read_text() == "{not json")

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
