"""Windows lock + atomic write checks for _dcio.py (skeleton step 2)."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import _dcio  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcio-"))

# 1. plain replacement of an existing file
target = tmp / "state.md"
_dcio.atomic_write(target, "one\n")
_dcio.atomic_write(target, "two\n")
check("replaces an existing file", target.read_text(encoding="utf-8") == "two\n")

# 1a. a TRANSIENT external reader is waited out, not failed on
import threading  # noqa: E402

holder = open(target, "r", encoding="utf-8")


def _release_soon():
    time.sleep(0.6)
    holder.close()


threading.Thread(target=_release_soon, daemon=True).start()
t0 = time.monotonic()
try:
    _dcio.atomic_write(target, "three\n")
    transient_ok = target.read_text(encoding="utf-8") == "three\n"
    err = f"waited {time.monotonic() - t0:.2f}s"
except _dcio.DcError as exc:
    transient_ok, err = False, str(exc)
check("transient external reader is retried through", transient_ok, err)

# 1b. a PERSISTENT holder fails loudly and leaves the old content intact.
#
# This is Windows behaviour and cannot be reproduced elsewhere: os.replace over
# a file another process holds open raises there, while POSIX permits it and
# the write correctly succeeds. Asserting it on POSIX would report a platform
# difference as a defect, which is the same dishonesty as letting an absent
# runner read as a passing test -- so it is declared unverifiable here rather
# than failed.
if os.name == "nt":
    stuck = open(target, "r", encoding="utf-8")
    try:
        _dcio.REPLACE_RETRY_SECONDS = 0.4  # keep the test quick
        try:
            _dcio.atomic_write(target, "four\n")
            raised = False
        except _dcio.DcError as exc:
            raised = "holding the file open" in str(exc)
        intact = target.read_text(encoding="utf-8") == "three\n"
    finally:
        stuck.close()
        _dcio.REPLACE_RETRY_SECONDS = 5.0
    check("persistent holder raises DcError", raised)
else:
    intact = None
    print("SKIP  persistent holder raises DcError  "
          "(POSIX permits replace over an open handle)")
if intact is None:
    print("SKIP  failed write leaves previous content intact  (Windows-only)")
else:
    check("failed write leaves previous content intact", intact)

# no temp turds left behind, even after the failure
leftovers = [p.name for p in tmp.iterdir() if p.name.startswith(".state.md")]
check("no temp files left after write or failure", not leftovers, str(leftovers))

# 2. exclusive acquisition
lock_a = _dcio.FileLock(tmp, timeout=0.5)
lock_a.acquire()
lock_b = _dcio.FileLock(tmp, timeout=0.5)
try:
    lock_b.acquire()
    exclusive = False
except _dcio.DcError:
    exclusive = True
check("second acquire blocks while first is held", exclusive)
lock_a.release()
lock_b2 = _dcio.FileLock(tmp, timeout=0.5)
lock_b2.acquire()
check("lock is reacquirable after release", True)
lock_b2.release()

# 3. a lock owned by a LIVE pid is not broken, even when old
live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
try:
    (tmp / _dcio.LOCK_NAME).write_text(
        json.dumps({"pid": live.pid, "acquired": time.time() - 10_000}), encoding="utf-8"
    )
    lk = _dcio.FileLock(tmp, timeout=0.4)
    try:
        lk.acquire()
        broke_live = True
    except _dcio.DcError:
        broke_live = False
    check("stale-aged lock with LIVE owner is not broken", not broke_live)
finally:
    live.kill()
    live.wait()

# 4. a lock owned by a DEAD pid, aged out, is broken
dead = subprocess.Popen([sys.executable, "-c", "pass"])
dead.wait()
(tmp / _dcio.LOCK_NAME).write_text(
    json.dumps({"pid": dead.pid, "acquired": time.time() - 10_000}), encoding="utf-8"
)
lk = _dcio.FileLock(tmp, timeout=1.0)
try:
    lk.acquire()
    broke_dead = True
except _dcio.DcError:
    broke_dead = False
check("aged lock with DEAD owner is broken", broke_dead)
lk.release()

# 5. a DEAD owner that has NOT aged out is still not broken
dead2 = subprocess.Popen([sys.executable, "-c", "pass"])
dead2.wait()
(tmp / _dcio.LOCK_NAME).write_text(
    json.dumps({"pid": dead2.pid, "acquired": time.time()}), encoding="utf-8"
)
lk2 = _dcio.FileLock(tmp, timeout=0.4)
try:
    lk2.acquire()
    early = True
except _dcio.DcError:
    early = False
check("fresh lock is not broken even when owner is dead", not early)
try:
    (tmp / _dcio.LOCK_NAME).unlink()
except OSError:
    pass

# 6. _process_alive agrees with reality
check("_process_alive(self) is True", _dcio._process_alive(os.getpid()))
check("_process_alive(dead pid) is False", not _dcio._process_alive(dead.pid))

# 7. path containment
root = tmp / "repo"
(root / "pkg").mkdir(parents=True)
check("subdir is within root", _dcio.is_within(root / "pkg", root))
check("root is within root", _dcio.is_within(root, root))
check("sibling is not within root", not _dcio.is_within(tmp / "elsewhere", root))

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
