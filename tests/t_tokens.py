"""Aggregation and leak checks for dc_tokens.py, over synthetic transcripts."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import dc_tokens  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dctokens-"))
root = tmp / "projects"
(root / "proj-a").mkdir(parents=True)

SECRET = "correct-horse-battery-staple"


def turn(inp, created, read, out):
    return json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": inp, "cache_creation_input_tokens": created,
        "cache_read_input_tokens": read, "output_tokens": out}}})


def write(name, lines):
    (root / "proj-a" / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


# A skill-loaded session: the marker appears in a user turn carrying a secret
# that must never reach the report.
write("on.jsonl", [
    json.dumps({"type": "user", "message": {"content": f"run drainclamp on {SECRET}"}}),
    turn(10, 100, 1000, 50),
    turn(10, 100, 1000, 50),
])
# An unmarked session, twice the context per turn.
write("off.jsonl", [
    json.dumps({"type": "user", "message": {"content": "unrelated work"}}),
    turn(20, 200, 2000, 100),
])
# No usage records at all -- must be skipped, not counted as a zero-turn session.
write("empty.jsonl", [json.dumps({"type": "summary"})])
# A truncated trailing line, as a live session leaves behind.
write("partial.jsonl", [turn(1, 1, 1, 1), '{"type": "assis'])

rc = dc_tokens.report(root, "drainclamp", as_json=False)
check("report exits clean", rc == 0, rc)

proc = subprocess.run(
    [sys.executable, "-B", str(SCRIPTS / "dc_tokens.py"),
     "--transcripts", str(root), "--json"],
    capture_output=True, text=True)
check("json mode exits clean", proc.returncode == 0, proc.stderr.strip())
data = json.loads(proc.stdout)

on, off = data["with_skill"], data["without_skill"]
check("marked session counted once", on["sessions"] == 1, on["sessions"])
check("marked turns counted", on["turns"] == 2, on["turns"])
check("marked input summed", on["input"] == 20, on["input"])
check("marked cache reads summed", on["read"] == 2000, on["read"])

# off.jsonl + partial.jsonl are both unmarked; empty.jsonl has no usage at all.
check("unmarked sessions counted", off["sessions"] == 2, off["sessions"])
check("usage-free transcript skipped", data["sessions_scanned"] == 3,
      data["sessions_scanned"])
check("truncated line does not abort the scan", off["turns"] == 2, off["turns"])

# The whole point of an aggregate: no transcript text in the output.
text = proc.stdout
plain = subprocess.run(
    [sys.executable, "-B", str(SCRIPTS / "dc_tokens.py"), "--transcripts", str(root)],
    capture_output=True, text=True).stdout
check("json output leaks no transcript text", SECRET not in text)
check("table output leaks no transcript text", SECRET not in plain)
check("table reports both buckets",
      "with skill" in plain and "without skill" in plain)
check("table carries the observational caveat", "observational" in plain)

# A missing transcript directory is a stated error, not an empty green table.
proc = subprocess.run(
    [sys.executable, "-B", str(SCRIPTS / "dc_tokens.py"),
     "--transcripts", str(tmp / "nope")],
    capture_output=True, text=True)
check("missing transcript directory fails loudly",
      proc.returncode != 0 and "no transcript directory" in proc.stderr)

# A marker matching nothing puts every session on one side and says so.
proc = subprocess.run(
    [sys.executable, "-B", str(SCRIPTS / "dc_tokens.py"),
     "--transcripts", str(root), "--marker", "zzz-no-match"],
    capture_output=True, text=True)
check("empty bucket reports n/a rather than a fake delta",
      "delta: n/a" in proc.stdout, proc.stdout.strip().splitlines()[-2:])

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
