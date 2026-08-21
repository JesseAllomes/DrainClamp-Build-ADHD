"""Schema v1 conformance + purge calculus for dc_state.py (skeleton step 3)."""
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import _dcio  # noqa: E402
import dc_state  # noqa: E402

fails = []
TEST_ENV = dict(os.environ)
TEST_ENV.setdefault(
    "DRAINCLAMP_HOME",
    str(Path(tempfile.mkdtemp(prefix="dcstate-registry-"))),
)


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def run(root, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_state.py"), "--root", str(root), *args],
        capture_output=True, text=True, env=TEST_ENV,
    )


def newrepo():
    d = Path(tempfile.mkdtemp(prefix="dcst-"))
    subprocess.run(["git", "init", "-q"], cwd=d, capture_output=True)
    return d


def roadmap(root, rows):
    body = ["| id | goal | files | status |", "|---|---|---|---|"]
    body += [f"| {i} | {g} | {f} | {s} |" for i, g, f, s in rows]
    p = root / "rm.md"
    p.write_text("\n".join(body), encoding="utf-8")
    return p


# --- section writes -------------------------------------------------------
repo = newrepo()
arch = repo / "arch.md"
arch.write_text("core -> does things -> main()\n", encoding="utf-8")
r1 = run(repo, "--set", "ARCH", "--file", str(arch))
r2 = run(repo, "--set", "ARCH", "--file", str(arch))
state = (repo / ".agent/drainclamp-state.md").read_text(encoding="utf-8")
check("--set ARCH twice succeeds", r1.returncode == 0 and r2.returncode == 0, r2.stderr.strip())
check("exactly one ARCH block", state.count("<!-- DC:ARCH -->") == 1)
check("generation advanced to 3", "generation=3" in state.splitlines()[0], state.splitlines()[0])
check("AGENTS.md not created", not (repo / "AGENTS.md").exists())
check("CLAUDE.md not created", not (repo / "CLAUDE.md").exists())
check(".gitignore not created", not (repo / ".gitignore").exists())
check("unignored .agent warning emitted once",
      "not ignored" in r1.stdout and "not ignored" not in r2.stdout)
capsule = repo / ".agent" / dc_state.RESUME_NAME
check("resume capsule emitted", capsule.exists())
check("resume capsule is bounded", capsule.stat().st_size < 512)
check("resume capsule carries generation", "Generation: 3" in capsule.read_text(encoding="utf-8"))

# --- schema conformance ---------------------------------------------------
def parse_fails(text, fragment):
    try:
        dc_state.State.parse(text)
        return False
    except _dcio.DcError as exc:
        return fragment in str(exc)


good = dc_state.template_text()
check("duplicated marker is a hard error",
      parse_fails(good.replace("<!-- DC:ARCH -->", "<!-- DC:ARCH -->\n<!-- DC:ARCH -->"),
                  "exactly once"))
check("unmatched marker is a hard error",
      parse_fails(good.replace("<!-- /DC:LOG -->", ""), "exactly once"))
check("out-of-order sections are a hard error",
      parse_fails(
          good.replace("<!-- DC:ARCH -->\n<!-- /DC:ARCH -->", "")
              .replace("<!-- DC:LOG -->", "<!-- DC:ARCH -->\n<!-- /DC:ARCH -->\n<!-- DC:LOG -->"),
          "out of order"))
check("missing header is a hard error", parse_fails("no header\n", "header missing"))
check("future schema refuses rewrite",
      parse_fails(good.replace("schema=1", "schema=2"), "not supported"))

# --- DC:VERIFY schema -----------------------------------------------------
def verify_fails(payload, fragment):
    try:
        dc_state.validate_verify_block("```json\n" + payload + "\n```")
        return False
    except _dcio.DcError as exc:
        return fragment in str(exc)


ok = '[{"id":"t","argv":["pytest","tests/"],"cwd":".","tier":"milestone"}]'
check("valid entry accepted", len(dc_state.validate_verify_block("```json\n" + ok + "\n```")) == 1)
check("unknown key 'source' rejected",
      verify_fails('[{"id":"t","argv":["pytest"],"cwd":".","tier":"fast","source":"user-approved"}]',
                   "unknown key"))
check("argv as string rejected",
      verify_fails('[{"id":"t","argv":"pytest tests/","cwd":".","tier":"fast"}]', "non-empty array"))
check("missing required key rejected",
      verify_fails('[{"id":"t","argv":["pytest"],"cwd":"."}]', "missing required"))
check("bad tier rejected",
      verify_fails('[{"id":"t","argv":["pytest"],"cwd":".","tier":"someday"}]', "'tier' must be"))
check("duplicate id rejected",
      verify_fails('[{"id":"t","argv":["a"],"cwd":".","tier":"fast"},'
                   '{"id":"t","argv":["b"],"cwd":".","tier":"fast"}]', "duplicate id"))
bad = repo / "bad.json"
bad.write_text('[{"id":"t","argv":["pytest"],"cwd":".","tier":"fast","source":"repo"}]',
               encoding="utf-8")
rc = run(repo, "--set", "VERIFY", "--file", str(bad))
check("CLI rejects forged trust field", rc.returncode != 0 and "unknown key" in rc.stderr)

# --- generation guard -----------------------------------------------------
sp = repo / ".agent/drainclamp-state.md"
raced = {"n": 0}
orig = _dcio.read_text


def racing(path):
    text = orig(path)
    if Path(path) == sp and raced["n"] == 0 and text:
        raced["n"] = 1
        gen = int(text.splitlines()[0].split("generation=")[1].split()[0].rstrip("-> "))
        _dcio.atomic_write(sp, text.replace(f"generation={gen}", f"generation={gen + 9}"))
    return text


_dcio.read_text = racing
try:
    dc_state.mutate(sp, repo / ".agent", lambda s: s.sections.__setitem__("ARCH", "x"))
    guarded = False
    why = "no exception"
except _dcio.GenerationMismatch as exc:
    guarded, why = True, str(exc)
finally:
    _dcio.read_text = orig
check("stale generation aborts the transaction", guarded, why)

# --- append-log -----------------------------------------------------------
repo2 = newrepo()
for i in range(25):
    run(repo2, "--append-log", f"did thing {i}")
log = (repo2 / ".agent/drainclamp-state.md").read_text(encoding="utf-8")
entries = [ln for ln in log.splitlines() if ln.startswith("- 2")]
check("DC:LOG capped at 20", len(entries) == 20, f"{len(entries)}")
check("log entries carry an 8-char id",
      all(dc_state.LOG_ENTRY_RE.match(e) for e in entries))

# --- purge calculus -------------------------------------------------------
def verdict(rows, root=None, extra=()):
    r = root or newrepo()
    rm = roadmap(r, rows)
    run(r, "--set", "ROADMAP", "--file", str(rm))
    out = run(r, "--purge-check", *extra)
    return out.stdout.splitlines()[0] if out.stdout else out.stderr.strip()


check("identical sets -> HOLD 100%",
      verdict([("m1", "a", "src/a.py;src/b.py", "done"),
               ("m2", "b", "src/a.py;src/b.py", "active")]).startswith("HOLD (overlap 100%)"))
check("disjoint sets -> PURGE",
      verdict([("m1", "a", "src/a.py", "done"),
               ("m2", "b", "src/z.py", "active")]).startswith("PURGE"))
# 1 of 5 shared = 0.20 exactly -> HOLD (boundary)
check("overlap exactly 0.20 -> HOLD",
      verdict([("m1", "a", "s/1.py", "done"),
               ("m2", "b", "s/1.py;s/2.py;s/3.py;s/4.py;s/5.py", "active")]
              ).startswith("HOLD (overlap 20%)"))
# 1 of 6 shared = 0.166 -> PURGE
check("overlap 0.17 -> PURGE",
      verdict([("m1", "a", "s/1.py", "done"),
               ("m2", "b", "s/1.py;s/2.py;s/3.py;s/4.py;s/5.py;s/6.py", "active")]
              ).startswith("PURGE (overlap 17%)"))
# An all-done roadmap is a terminator, not an overlap question: there is no
# next milestone to compare against, so it used to file as HOLD and leave a
# finished project sitting in a context it no longer needed.
check("every milestone done -> COMPLETE",
      verdict([("m1", "a", "s/1.py", "done")]) == "COMPLETE (1/1 milestones done)")
check("every milestone done counts them all",
      verdict([("m1", "a", "s/1.py", "done"),
               ("m2", "b", "s/2.py", "done")]) == "COMPLETE (2/2 milestones done)")
check("one row short of done is not COMPLETE",
      not verdict([("m1", "a", "s/1.py", "done"),
                   ("m2", "b", "s/2.py", "pending")]).startswith("COMPLETE"))
check("no next milestone -> HOLD unknown",
      verdict([]) == "HOLD (overlap unknown: no next milestone)")
check("empty next file set -> HOLD unknown",
      "next milestone empty" in verdict([("m1", "a", "s/1.py", "done"),
                                         ("m2", "b", "", "active")]))
check("two pending candidates -> HOLD unknown",
      "multiple eligible" in verdict([("m1", "a", "s/1.py", "done"),
                                      ("m2", "b", "s/2.py", "pending"),
                                      ("m3", "c", "s/3.py", "pending")]))

# named-but-nonexistent files still count
r = newrepo()
(r / "src").mkdir()
(r / "src/a.py").write_text("x", encoding="utf-8")
v = verdict([("m1", "a", "src/a.py;src/new1.py", "done"),
             ("m2", "b", "src/a.py;src/new1.py;src/new2.py;src/new3.py", "active")], root=r)
check("named future files count as members", v.startswith("HOLD (overlap 50%)"), v)

# unresolved glob forces unknown
r = newrepo()
v = verdict([("m1", "a", "src/a.py", "done"), ("m2", "b", "nothing/*.rs", "active")], root=r)
check("unresolved glob -> HOLD unknown", "unresolved path pattern" in v, v)

# context-high never manufactures a figure
r = newrepo()
(r / "s").mkdir()
for n in ("1", "2", "3", "4"):
    (r / f"s/{n}.py").write_text("x", encoding="utf-8")
v = verdict([("m1", "a", "s/1.py", "done"), ("m2", "b", "s/1.py;s/2.py;s/3.py;s/4.py", "active")],
            root=r, extra=("--context-high",))
check("context-high keeps the real overlap", v == "PURGE (context-high; overlap 25%)", v)
v = verdict([("m1", "a", "s/1.py", "done"), ("m2", "b", "nope/*.rs", "active")],
            root=newrepo(), extra=("--context-high",))
check("context-high with unknown overlap says unknown",
      v == "PURGE (context-high; overlap unknown)", v)

# --- concurrent appends ---------------------------------------------------
# Separate processes, not threads: the lock guards against other interpreters,
# so a purely threaded test would share one process and never exercise it.
conc = newrepo()
run(conc, "--append-log", "seed")
results: list = []


def _appender(n: int) -> None:
    results.append(run(conc, "--append-log", f"concurrent {n}"))


threads = [threading.Thread(target=_appender, args=(i,)) for i in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=90)

bad = [(r.returncode, (r.stderr or r.stdout)[:120]) for r in results if r.returncode != 0]
check("concurrent appends all exit 0", len(results) == 3 and not bad, str(bad)[:200])

log = _dcio.read_text(conc / ".agent" / _dcio.STATE_NAME) or ""
check("every concurrent append survives the race",
      all(f"concurrent {n}" in log for n in range(3)),
      f"{log.count('concurrent ')} of 3 present")

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
