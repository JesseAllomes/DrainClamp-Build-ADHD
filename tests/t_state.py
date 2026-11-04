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

# --- --mark / --close ------------------------------------------------------
mk = newrepo()
run(mk, "--set", "ROADMAP", "--file", str(roadmap(mk, [("m1", "first", "a.py", "pending"),
                                                       ("m2", "second", "b.py", "pending"),
                                                       ("m3", "third", "c.py", "pending")])))
state_file = mk / ".agent" / "drainclamp-state.md"
before = state_file.read_text(encoding="utf-8")
r = run(mk, "--mark", "m1=active")
after = state_file.read_text(encoding="utf-8")
check("--mark flips one status cell", r.returncode == 0 and "| m1 | first | a.py | active |" in after, r.stdout + r.stderr)
check("--mark changes nothing but that cell",
      before.replace("| m1 | first | a.py | pending |", "| m1 | first | a.py | active |")
      == after.replace("generation=3", "generation=2"), after)
r = run(mk, "--mark", "m1=done,m2=active", "--log", "m1 done by mark")
after = state_file.read_text(encoding="utf-8")
check("--mark takes a comma list and appends --log",
      r.returncode == 0 and "| m1 | first | a.py | done |" in after and "| m2 | second | b.py | active |" in after
      and "m1 done by mark" in after and "LOG +=" in r.stdout, r.stdout + r.stderr)
r = run(mk, "--mark", "m3=pending", "--mark", "m2=done")
check("--mark is repeatable", r.returncode == 0 and "| m2 | second | b.py | done |" in
      state_file.read_text(encoding="utf-8"), r.stdout + r.stderr)
snap = state_file.read_text(encoding="utf-8")
r = run(mk, "--mark", "m3=active", "--mark", "m9=done")
check("an unknown id is an error and writes nothing",
      r.returncode != 0 and "m9" in r.stderr and state_file.read_text(encoding="utf-8") == snap, r.stdout + r.stderr)
r = run(mk, "--mark", "m3=bogus")
check("a bad status is refused and writes nothing",
      r.returncode != 0 and state_file.read_text(encoding="utf-8") == snap, r.stdout + r.stderr)
r = run(mk, "--mark", "m3=active", "--expect-generation", "1")
check("--mark honours --expect-generation", r.returncode != 0 and state_file.read_text(encoding="utf-8") == snap,
      r.stdout + r.stderr)
r = run(mk, "--log", "stray")
check("--log alone is refused", r.returncode != 0 and state_file.read_text(encoding="utf-8") == snap, r.stdout + r.stderr)
r = run(mk, "--close", "m3", "--log", "closed the last one")
check("--close marks done, logs, and prints the purge verdict",
      r.returncode == 0 and "| m3 | third | c.py | done |" in state_file.read_text(encoding="utf-8")
      and ("COMPLETE" in r.stdout or "PURGE" in r.stdout or "HOLD" in r.stdout)
      and "DRAINCLAMP:" in r.stdout, r.stdout + r.stderr)

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

# --- chunk read-size calculus ---------------------------------------------
small = newrepo()
(small / "a.py").write_text("x\n" * 119, encoding="utf-8")
(small / "b.py").write_text("x\n" * 120, encoding="utf-8")
(small / "next.py").write_text("x\n", encoding="utf-8")
v, human = dc_state.chunk_purge_check(["a.py::f", "a.py::g", "b.py"], ["next.py"], small, False)
check("small disjoint chunk holds and deduplicates paths",
      v.startswith("HOLD (chunk small") and "239" in v and "Context retained" in human, v)
v, _ = dc_state.chunk_purge_check(["a.py", "b.py"], ["next.py"], small, True)
check("context-high overrides small chunk hold", v.startswith("PURGE (chunk context-high"), v)
(small / "a.py").write_text("x\n" * 120, encoding="utf-8")
v, _ = dc_state.chunk_purge_check(["a.py", "b.py"], ["next.py"], small, False)
check("exactly 240 lines still purges", v.startswith("PURGE (chunk overlap 0%)"), v)
v, _ = dc_state.chunk_purge_check(["missing.py"], ["next.py"], small, False)
check("unreadable size never implies small chunk", v.startswith("PURGE"), v)
v, _ = dc_state.chunk_purge_check(["next.py"], ["next.py"], small, False)
check("small rule preserves existing overlap hold", v == "HOLD (chunk overlap 100%)", v)
v, _ = dc_state.chunk_purge_check(["next.py"], ["missing/*.py"], small, False)
check("small rule preserves unknown hold", "overlap unknown" in v, v)

# --- bounded authoritative-state view ------------------------------------
view_repo = newrepo()
view_state = dc_state.State.parse(dc_state.template_text())
view_state.sections["ARCH"] = "core -> responsibility -> entrypoint"
view_state.sections["DECISIONS"] = "- preserve this decision"
view_state.sections["VERIFY"] = ok
view_state.sections["ROADMAP"] = "| id | goal | files | status | deps |\n|---|---|---|---|---|\n" + "\n".join(
    [f"| m{i} | old goal {i} | old{i}.py | done | |" for i in range(1, 5)] +
    ["| m5 | current | current.py | active | m4 |", "| m6 | later | later.py | pending | m5 |"])
view_state.sections["LOG"] = "\n".join(f"- log entry {i}" for i in range(8))
view_path = view_repo / ".agent/drainclamp-state.md"
view_path.parent.mkdir()
view_path.write_text(view_state.render(7), encoding="utf-8")
before = view_path.read_bytes()
rv = run(view_repo, "--resume-view")
check("resume-view CLI succeeds", rv.returncode == 0, rv.stderr.strip())
check("resume-view declares roadmap and log coverage", "SHOWING 3/6" in rv.stdout and "SHOWING 5/8" in rv.stdout, rv.stdout[:150])
check("resume-view preserves ARCH DECISIONS VERIFY", all(x in rv.stdout for x in ("core -> responsibility", "preserve this decision", ok)))
check("resume-view keeps last done and every open row with files and deps",
      all(x in rv.stdout for x in ("old4.py", "current.py", "later.py", "| m5 |")) and "old1.py" not in rv.stdout)
check("resume-view summarizes all done ids", "Done: m1, m2, m3, m4" in rv.stdout)
check("resume-view trims logs to last five", "- log entry 3" in rv.stdout and "- log entry 7" in rv.stdout and "- log entry 2" not in rv.stdout)
check("resume-view leaves state bytes and directory untouched", view_path.read_bytes() == before and set(view_path.parent.iterdir()) == {view_path})
absent = newrepo()
rv = run(absent, "--resume-view")
check("resume-view absent state is explicit and read-only", rv.returncode == 0 and "ABSENT" in rv.stdout and not (absent / ".agent").exists())
rv = run(view_repo, "--resume-view", "--append-log", "must not write")
check("resume-view cannot be combined with a mutation", rv.returncode != 0 and view_path.read_bytes() == before)

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

# --- r86: --mark with --append-log is refused ------------------------------
cmb = newrepo()
run(cmb, "--set", "ROADMAP", "--file", str(roadmap(cmb, [("m1", "first", "a.py", "pending")])))
cmb_file = cmb / ".agent" / "drainclamp-state.md"
cmb_before = cmb_file.read_text(encoding="utf-8")
r = run(cmb, "--mark", "m1=done", "--append-log", "x")
check("--mark with --append-log is refused and writes nothing",
      r.returncode != 0 and "cannot be combined" in (r.stdout + r.stderr)
      and "| m1 | first | a.py | pending |" in cmb_file.read_text(encoding="utf-8")
      and cmb_file.read_text(encoding="utf-8") == cmb_before, r.stdout + r.stderr)

# --- r87: a roadmap row without a closing pipe is marked --------------------
npp = newrepo()
npp_rm = npp / "rm.md"
npp_rm.write_text("| id | goal | files | status |\n|---|---|---|---|\n| m1 | goal | a.py | active",
                  encoding="utf-8")
run(npp, "--set", "ROADMAP", "--file", str(npp_rm))
npp_file = npp / ".agent" / "drainclamp-state.md"
r = run(npp, "--mark", "m1=done")
m1_rows = [ln for ln in npp_file.read_text(encoding="utf-8").splitlines() if ln.startswith("| m1 |")]
check("--mark changes a row that has no closing pipe",
      r.returncode == 0 and m1_rows == ["| m1 | goal | a.py | done"], repr((r.stdout + r.stderr, m1_rows)))

# --- i16a: --supersede marks a decision replaced, one guarded edit ------------
sup = newrepo()
sup_dec = sup / "dec.md"
sup_dec.write_text("- Host? -> board (user)\n\n- Rate? -> 90 (user)\n- Host? -> none, board dropped (user)\n",
                   encoding="utf-8")
run(sup, "--set", "DECISIONS", "--file", str(sup_dec))
sup_file = sup / ".agent" / "drainclamp-state.md"
gen = dc_state.State.parse(sup_file.read_text(encoding="utf-8")).generation
r = run(sup, "--supersede", "1", "--by", "3", "--expect-generation", str(gen))
body = dc_state.State.parse(sup_file.read_text(encoding="utf-8")).sections["DECISIONS"]
check("--supersede marks D1 by D3 (blank lines not counted)",
      r.returncode == 0 and "- Host? -> board (user) [superseded by D3]" in body
      and body.count("superseded") == 1 and "D1 superseded by D3" in r.stdout, r.stdout + r.stderr + body)
check("--supersede refuses a stale generation",
      run(sup, "--supersede", "2", "--by", "3", "--expect-generation", str(gen)).returncode != 0)
check("--supersede refuses twice", run(sup, "--supersede", "1", "--by", "2").returncode != 0)
check("--supersede refuses itself", run(sup, "--supersede", "2", "--by", "2").returncode != 0)
check("--supersede refuses a number out of range", run(sup, "--supersede", "4", "--by", "2").returncode != 0)
check("--supersede refuses a superseded replacement", run(sup, "--supersede", "2", "--by", "1").returncode != 0)
check("--by needs --supersede", run(sup, "--by", "2").returncode != 0)

# --- i16b: the resume view leaves superseded decisions out, and says so -------
rv = run(sup, "--resume-view")
check("resume-view drops the superseded decision", rv.returncode == 0
      and "- Host? -> board (user)" not in rv.stdout and "- Host? -> none, board dropped (user)" in rv.stdout
      and "- Rate? -> 90 (user)" in rv.stdout, rv.stdout)
check("resume-view declares decision coverage", "DECISIONS: SHOWING 2/3 (1 superseded)" in rv.stdout, rv.stdout[:300])
check("the full file keeps the superseded line",
      "[superseded by D3]" in sup_file.read_text(encoding="utf-8"))

# --- t21: the purge message is printed whole, by host -----------------------
pg = newrepo()
(pg / "a.py").write_text("a\n", encoding="utf-8")
(pg / "b.py").write_text("b\n", encoding="utf-8")
run(pg, "--set", "ROADMAP", "--file", str(roadmap(pg, [("m1", "one", "a.py", "done"), ("m2", "two", "b.py", "active")])))
pg_posix = str(pg.resolve()).replace("\\", "/")
r = run(pg, "--purge-check", "--host", "claude")
lines = r.stdout.splitlines()
check("t21: PURGE prints the /compact line for Claude Code", len(lines) == 3 and lines[0].startswith("PURGE")
      and lines[2] == f"Run /compact now, then resume with dc_session.py --resume {pg_posix}.", r.stdout + r.stderr)
r = run(pg, "--purge-check", "--host", "other")
check("t21: another host gets the fresh-session line",
      r.stdout.splitlines()[-1] == f"Start a fresh session and resume with dc_session.py --resume {pg_posix}.", r.stdout)
run(pg, "--set", "ROADMAP", "--file", str(roadmap(pg, [("m1", "one", "a.py", "done"), ("m2", "two", "a.py", "active")])))
r = run(pg, "--purge-check", "--host", "claude")
check("t21: HOLD prints no resume line", r.stdout.splitlines()[0].startswith("HOLD")
      and len(r.stdout.splitlines()) == 2, r.stdout)
sp_root = newrepo() / "with space"
sp_root.mkdir()
subprocess.run(["git", "init", "-q"], cwd=sp_root, capture_output=True)
(sp_root / "a.py").write_text("a\n", encoding="utf-8")
(sp_root / "b.py").write_text("b\n", encoding="utf-8")
run(sp_root, "--set", "ROADMAP", "--file",
    str(roadmap(sp_root, [("m1", "one", "a.py", "done"), ("m2", "two", "b.py", "active")])))
r = run(sp_root, "--purge-check", "--host", "claude")
sp_posix = str(sp_root.resolve()).replace("\\", "/")
check("r116: a path with spaces is quoted in the resume line",
      r.stdout.splitlines()[-1] == f'Run /compact now, then resume with dc_session.py --resume "{sp_posix}".',
      r.stdout + r.stderr)

# --- t23/i20: --close ticks the to-dos the row names, lists the rest ----------
import dc_project  # noqa: E402
td = newrepo()
run(td, "--set", "ROADMAP", "--file", str(roadmap(td, [
    ("m1", "build it (t1, t3) and t2/i9", "a.py", "active"), ("m2", "later t4 and t7", "b.py", "pending")])))
for text in ("one", "two", "three", "four"):
    dc_project.mutate(td / ".agent", lambda d, t=text: (dc_project.apply_op(d, td, "todo.add", {"text": t}), d)[1])
dc_project.mutate(td / ".agent", lambda d: (dc_project.apply_op(d, td, "todo.done", {"id": "t3"}), d)[1])
r = run(td, "--close", "m1")
todos = {t["id"]: t["done"] for t in dc_project.load(td / ".agent")["todos"]}
check("t23: --close ticks the open to-dos the row names, skips one already done",
      r.returncode == 0 and "TODOS ticked: t1, t2" in r.stdout and todos == {"t1": True, "t2": True, "t3": True, "t4": False},
      r.stdout + r.stderr)
check("t23: --close lists the to-dos still open for a done check",
      "TODOS still open: SHOWING 1/1" in r.stdout and "  t4: four" in r.stdout, r.stdout)
r = run(td, "--close", "m2")
check("t23: a named id with no sidecar to-do is reported, the close still lands",
      r.returncode == 0 and "TODOS ticked: t4" in r.stdout and "TODOS named but not in the sidecar: t7" in r.stdout and "DC:ROADMAP marked m2=done" in r.stdout, r.stdout + r.stderr)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
