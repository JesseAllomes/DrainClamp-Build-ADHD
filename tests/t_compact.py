"""G8: dc_state --compact-check conditions, agent records, and the opt-in idle hook."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
fails = []
TEST_ENV = dict(os.environ)
TEST_ENV["DRAINCLAMP_HOME"] = tempfile.mkdtemp(prefix="dccompact-registry-")


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def py(script, *args, stdin=None, env=None):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *args],
                          capture_output=True, text=True, input=stdin, env=env or TEST_ENV)


def st(root, *args):
    return py("dc_state.py", "--root", str(root), *args)


def verdict(root):
    r = st(root, "--compact-check")
    return r.returncode, r.stdout.strip()


def newrepo():
    d = Path(tempfile.mkdtemp(prefix="dccp-"))
    subprocess.run(["git", "init", "-q"], cwd=d, capture_output=True)
    return d


def touch_future(path, secs=5):
    t = time.time() + secs
    os.utime(path, (t, t))


# --- state file ------------------------------------------------------------
repo = newrepo()
rc, out = verdict(repo)
check("no state file: not safe", rc != 0 and out == "Compact: not yet: state file not saved", out)

st(repo, "--append-log", "first step done")
rc, out = verdict(repo)
check("saved state, clean tree: safe", rc == 0 and out == "Compact: safe (state saved)", out)
check("output is exactly one line", len(st(repo, "--compact-check").stdout.splitlines()) == 1)

cap = repo / ".agent" / "drainclamp-resume.md"
good = cap.read_text(encoding="utf-8")
cap.write_text(good.replace("Generation: ", "Generation: 9"), encoding="utf-8")
rc, out = verdict(repo)
check("capsule behind state: not safe", rc != 0 and "generation" in out, out)
cap.write_text(good, encoding="utf-8")

lock = repo / ".agent" / ".dc.lock"
lock.write_text("{}", encoding="utf-8")
rc, out = verdict(repo)
check("lock held: not safe", rc != 0 and "lock held" in out, out)
lock.unlink()

(repo / ".agent" / "drainclamp-state.md").write_text("not a state file\n", encoding="utf-8")
rc, out = verdict(repo)
check("unparseable state: not safe", rc != 0 and "does not parse" in out, out)

# --- edits vs log ------------------------------------------------------------
repo = newrepo()
st(repo, "--append-log", "baseline")
for n in ("a.txt", "b.txt"):
    (repo / n).write_text("x\n", encoding="utf-8")
    touch_future(repo / n)
rc, out = verdict(repo)
check("2 untracked edits after log: not safe",
      rc != 0 and out == "Compact: not yet: 2 edited files not logged", out)
(repo / "b.txt").unlink()
rc, out = verdict(repo)
check("1 edit: singular reason", out == "Compact: not yet: 1 edited file not logged", out)
st(repo, "--append-log", "logged a")
os.utime(repo / "a.txt", (time.time() - 60, time.time() - 60))
rc, out = verdict(repo)
check("edit older than log: safe", rc == 0, out)
(repo / ".agent" / "scratch.txt").write_text("n\n", encoding="utf-8")
touch_future(repo / ".agent" / "scratch.txt")
rc, out = verdict(repo)
check(".agent files ignored", rc == 0, out)

subprocess.run(["git", "add", "a.txt"], cwd=repo, capture_output=True)
subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-qm", "c"],
               cwd=repo, capture_output=True)
touch_future(repo / "a.txt")
rc, out = verdict(repo)
check("committed-since-log edit: not safe", rc != 0 and "not logged" in out, out)

# --- t17: a chunk tick is the save mid-milestone -------------------------------
from datetime import datetime, timezone  # noqa: E402

repo = newrepo()
st(repo, "--append-log", "baseline")
(repo / "a.txt").write_text("x\n", encoding="utf-8")
touch_future(repo / "a.txt", 5)
rc, out = verdict(repo)
check("t17: edit after the log, no tick: not safe", rc != 0 and "not logged" in out, out)
tick = datetime.fromtimestamp(time.time() + 10, timezone.utc).isoformat(timespec="seconds")
(repo / ".agent" / "drainclamp-project.json").write_text(json.dumps(
    {"chunks": {"m1": [{"id": "c1", "done": True, "done_at": tick}]}}), encoding="utf-8")
rc, out = verdict(repo)
check("t17: chunk ticked after the edit: safe", rc == 0, out)
touch_future(repo / "a.txt", 20)
rc, out = verdict(repo)
check("t17: edit after the tick: not safe", rc != 0 and "not logged" in out, out)

# --- Gate 6 ------------------------------------------------------------------
repo = newrepo()
st(repo, "--append-log", "baseline")
side = repo / ".agent" / "drainclamp-project.json"


def sidecar(review):
    side.write_text(json.dumps({"schema": 1, "review": review}), encoding="utf-8")


sidecar({"rounds": [{"id": "R1", "milestone": "M1"}], "findings": []})
rc, out = verdict(repo)
check("open review round: not safe", rc != 0 and out == "Compact: not yet: Gate 6 round open", out)
sidecar({"rounds": [{"id": "R1", "milestone": "M1", "finished": "2026-01-01T00:00:00+00:00"}],
         "findings": [{"id": "F1", "status": "fixing"}]})
rc, out = verdict(repo)
check("fix in flight: not safe", rc != 0 and "Gate 6 fix" in out, out)
sidecar({"rounds": [{"id": "R1", "milestone": "M1", "finished": "2026-01-01T00:00:00+00:00"},
                    {"id": "R2", "aborted": True}],
         "findings": [{"id": "F1", "status": "fixed"}]})
rc, out = verdict(repo)
check("finished/aborted rounds, fixed finding: safe", rc == 0, out)

# --- agent records -----------------------------------------------------------
r = st(repo, "--running", "add", "build-x")
check("--running add", r.returncode == 0, r.stderr.strip())
rc, out = verdict(repo)
check("background work running: not safe",
      rc != 0 and out == "Compact: not yet: background work running: build-x", out)
st(repo, "--running", "add", "build-x")
st(repo, "--running", "done", "build-x")
rc, out = verdict(repo)
check("running done (add twice is idempotent): safe", rc == 0, out)
st(repo, "--pending-question", "set")
rc, out = verdict(repo)
check("pending question: not safe", rc != 0 and out == "Compact: not yet: question to the user pending", out)
st(repo, "--pending-question", "clear")
rc, out = verdict(repo)
check("question cleared: safe", rc == 0, out)
check("bad --running verb refused", st(repo, "--running", "stop", "x").returncode != 0)

# --- hook install ------------------------------------------------------------
home = Path(tempfile.mkdtemp(prefix="dchome-"))
cfg = home / ".claude" / "settings.json"
env = dict(TEST_ENV)
env.pop("CLAUDE_CONFIG_DIR", None)


def inst(*args):
    return py("dc_install.py", "--home", str(home), *args, env=env)


def ours(data):
    return [g for g in data.get("hooks", {}).get("Notification", [])
            if "--compact-hook" in json.dumps(g)]


r = inst("--compact-reminder", "off")
check("off with no settings file: no-op, no file created",
      r.returncode == 0 and not cfg.exists(), r.stdout + r.stderr)
cfg.parent.mkdir(parents=True)
other = {"model": "x-model", "permissions": {"allow": ["Bash(ls)"]},
         "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
                   "Notification": [{"matcher": "permission_prompt",
                                     "hooks": [{"type": "command", "command": "echo perm"}]}]}}
cfg.write_text(json.dumps(other), encoding="utf-8")
r = inst("--compact-reminder")
data = json.loads(cfg.read_text(encoding="utf-8"))
check("on installs one idle_prompt hook", r.returncode == 0 and len(ours(data)) == 1
      and ours(data)[0]["matcher"] == "idle_prompt", r.stdout + r.stderr)
check("on keeps every other key", data["model"] == "x-model" and data["permissions"] == other["permissions"]
      and data["hooks"]["Stop"] == other["hooks"]["Stop"]
      and other["hooks"]["Notification"][0] in data["hooks"]["Notification"])
check("install output states the idle delay", "60 seconds" in r.stdout, r.stdout)
check("hook never runs /compact", "/compact" not in cfg.read_text(encoding="utf-8"))
first = cfg.read_text(encoding="utf-8")
r = inst("--compact-reminder", "on")
check("on twice is idempotent", cfg.read_text(encoding="utf-8") == first and "already on" in r.stdout, r.stdout)
r = inst("--compact-reminder", "off")
data = json.loads(cfg.read_text(encoding="utf-8"))
check("off removes only ours", not ours(data)
      and data["hooks"]["Notification"] == other["hooks"]["Notification"]
      and data["hooks"]["Stop"] == other["hooks"]["Stop"], r.stdout)
r = inst("--compact-reminder", "off")
check("off twice is idempotent",
      "already off" in r.stdout and json.loads(cfg.read_text(encoding="utf-8")) == data)
cfg.write_text(json.dumps({"model": "x-model"}), encoding="utf-8")
inst("--compact-reminder", "on")
inst("--compact-reminder", "off")
check("off prunes the hooks key it created", json.loads(cfg.read_text(encoding="utf-8")) == {"model": "x-model"})
cfg.write_text("{broken", encoding="utf-8")
r = inst("--compact-reminder", "on")
check("invalid settings JSON refused, left as is",
      r.returncode != 0 and cfg.read_text(encoding="utf-8") == "{broken")

# --- hook body ---------------------------------------------------------------
repo = newrepo()
st(repo, "--append-log", "baseline")
r = py("dc_install.py", "--compact-hook", stdin=json.dumps({"cwd": str(repo)}))
seq = json.loads(r.stdout)["terminalSequence"] if r.stdout.strip() else ""
check("hook body notifies when safe", r.returncode == 0 and "safe to compact" in seq, r.stdout + r.stderr)
st(repo, "--pending-question", "set")
r = py("dc_install.py", "--compact-hook", stdin=json.dumps({"cwd": str(repo)}))
check("hook body silent when not safe", r.returncode == 0 and not r.stdout.strip(), r.stdout)
r = py("dc_install.py", "--compact-hook", stdin="")
check("hook body without a project is a quiet no-op", r.returncode == 0 and not r.stdout.strip())

print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)
