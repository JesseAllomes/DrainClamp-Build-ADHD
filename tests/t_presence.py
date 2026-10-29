"""Session presence and direct hand-off checks (G11): dc_registry / dc_session / dc_state."""
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
import dc_state  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcpresence-"))
home = tmp / "home"
root = tmp / "proj"
(root / ".agent").mkdir(parents=True)
(root / ".agent" / "drainclamp-state.md").write_text(dc_state.template_text(), encoding="utf-8", newline="")
SIDECAR = root / ".agent" / "drainclamp-project.json"


def run(script, *args):
    proc = subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *args],
                          capture_output=True, text=True, encoding="utf-8")
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def sess(*args):
    return run("dc_session.py", "--home", str(home), "--root", str(root), *args)


def presence():
    return json.loads(SIDECAR.read_text(encoding="utf-8")).get("presence")


def backdate(minutes):
    data = json.loads(SIDECAR.read_text(encoding="utf-8"))
    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
    data["presence"]["heartbeat"] = old
    SIDECAR.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")
    return old


# 1. presence written
rc, out = sess("--presence", "--host", "claude", "--session", "sess-a")
check("presence recorded", rc == 0 and "recorded" in out, out)
p = presence()
check("presence holds host, session, since, heartbeat only",
      p and set(p) == {"host", "session", "since", "heartbeat"} and p["host"] == "claude"
      and p["session"] == "sess-a", p)

# 2. refreshed by a state write
old = backdate(50)
rc, out = run("dc_state.py", "--root", str(root), "--append-log", "presence check")
check("state write ran", rc == 0, out[-200:])
new = presence()["heartbeat"]
check("state write refreshes heartbeat", new > old, f"{old} -> {new}")

# 3. same session sees nothing; second session sees one IN USE line
rc, out = sess("--presence", "--host", "claude", "--session", "sess-a")
check("same session sees no IN USE", "IN USE" not in out, out)
backdate(7)
rc, out = sess("--presence", "--host", "claude", "--session", "sess-b")
lines = [ln for ln in out.splitlines() if ln.strip()]
check("second session sees one IN USE line",
      rc == 0 and lines == ["IN USE: sess-a (last active 7 min ago)"], out)
check("second session does not overwrite", presence()["session"] == "sess-a", presence())
rc, out = sess("--presence")
check("report-only shows the live session", "IN USE: sess-a" in out, out)

# 4. stale entry ignored
backdate(130)
rc, out = sess("--presence")
check("stale entry reported as none", "none" in out and "IN USE" not in out, out)
rc, out = sess("--presence", "--host", "claude", "--session", "sess-b")
check("stale entry ignored on claim", "IN USE" not in out and presence()["session"] == "sess-b", out)

# 5. hand-off line per host
rc, out = sess("--handoff", "--file", ".agent/notes.md", "--what", "added two tickets", "--session", "sess-c")
check("claude host gets SendMessage",
      out == "HANDOFF: SendMessage to sess-b: .agent/notes.md -- added two tickets", out)
rc, out = sess("--handoff", "--file", ".agent/notes.md", "--what", "added two tickets", "--session", "sess-b")
check("own session gets no hand-off", out == "HANDOFF: none (no live session)", out)
rc, out = sess("--presence", "--host", "codex", "--session", "sess-x", "--force")
check("force takes over", rc == 0 and presence()["host"] == "codex", out)
rc, out = sess("--handoff", "--file", ".agent/notes.md", "--what", "added two tickets", "--session", "sess-c")
check("other host gets the paste line",
      out == "HANDOFF: paste to sess-x: .agent/notes.md -- added two tickets", out)

# 6. no live session
backdate(200)
rc, out = sess("--handoff", "--file", "f.md", "--what", "x", "--session", "sess-c")
check("stale session means no live session", out == "HANDOFF: none (no live session)", out)
SIDECAR.unlink()
rc, out = sess("--handoff", "--file", "f.md", "--what", "x")
check("no sidecar means no live session", rc == 0 and out == "HANDOFF: none (no live session)", out)

# 7. no presence flags: heartbeat creates nothing
rc, out = run("dc_state.py", "--root", str(root), "--append-log", "plain")
check("state write without presence leaves no sidecar", not SIDECAR.exists(), out[-200:])

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
