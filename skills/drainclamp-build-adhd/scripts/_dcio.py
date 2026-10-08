"""Shared I/O primitives for DrainClamp & Build.

Stdlib only. Everything that writes to `.agent/` goes through here so that
atomicity, locking and generation checking are implemented once.

Covers: atomic writes, PID-aware locking, generation validation, exit codes,
repo-root resolution, the subprocess sandbox, and output normalisation.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


def _utf8_console() -> None:
    """Make stdout/stderr survive non-ASCII on a legacy Windows code page.

    A repository is free to contain `modulo_naive.py` spelled with accents, and a
    symbol name reaching a cp1252 console would otherwise raise UnicodeEncodeError
    *while reporting* — turning a successful audit into a crash. `replace` is the
    right failure mode: a mangled glyph in a console line is a display loss, never
    a data loss, because everything durable is written to `.agent/` as UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


_utf8_console()

SCHEMA_VERSION = 1

# Exit codes. Shared across every dc_* script so a caller can branch on them.
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CHECK_FAILED = 2
EXIT_MISSING_REQUIRED = 3
EXIT_UNSAFE_COMMAND = 4
EXIT_TIMEOUT = 5
EXIT_NO_CHECKS = 6

AGENT_DIR_NAME = ".agent"
LOCK_NAME = ".dc.lock"
STATE_NAME = "drainclamp-state.md"

# A lock older than this is *eligible* for breaking, but is only broken once the
# owning process is confirmed gone. Age alone never justifies it.
LOCK_STALE_SECONDS = 300
# 10s was not enough: on Windows a cold interpreter start under three concurrent
# appenders can spend most of that budget before it ever reaches the lock.
LOCK_WAIT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.05
LOCK_STALE_CHECK_SECONDS = 1.0

# Windows only lets a file be renamed over when nothing else has it open.
# External holders (OneDrive, editors, indexers) are usually momentary.
REPLACE_RETRY_SECONDS = 5.0
REPLACE_POLL_SECONDS = 0.02


class DcError(Exception):
    """Operational failure that should be reported, not traced."""

    def __init__(self, message: str, code: int = EXIT_INTERNAL) -> None:
        super().__init__(message)
        self.code = code


class GenerationMismatch(DcError):
    """Another writer committed while we were preparing our own write."""

    def __init__(self, expected: int, found: int) -> None:
        super().__init__(
            f"generation mismatch: expected {expected}, found {found}; "
            "another writer committed first",
            EXIT_CHECK_FAILED,
        )
        self.expected = expected
        self.found = found


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def repo_root(start: Path | str | None = None) -> Path:
    """Canonical repository root, falling back to cwd outside a git repo."""
    base = Path(start).resolve() if start else Path.cwd().resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return base
    if out.returncode == 0 and out.stdout.strip():
        return Path(out.stdout.strip()).resolve()
    return base


def agent_dir(root: Path | None = None) -> Path:
    """`.agent/` under the repo root, created on demand."""
    target = (root or repo_root()) / AGENT_DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def is_within(path: Path, root: Path) -> bool:
    """True when `path` resolves at or beneath `root` (symlinks resolved)."""
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    return resolved == base or base in resolved.parents


# --------------------------------------------------------------------------
# Atomic writes
# --------------------------------------------------------------------------


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` so readers never observe a partial file.

    Temp file in the same directory (same filesystem, so `os.replace` is a
    rename), flushed and fsynced before the swap.

    Windows caveat: `os.replace` raises PermissionError while *any* process
    holds the destination open, including read-only holders such as OneDrive,
    an editor, or the search indexer. Those holders are usually transient, so
    the swap is retried briefly. A holder that outlasts the window is reported
    as a failure — the file is never left partially written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        deadline = time.monotonic() + REPLACE_RETRY_SECONDS
        delay = REPLACE_POLL_SECONDS
        while True:
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                if time.monotonic() >= deadline:
                    raise DcError(
                        f"could not replace {path}: {exc.strerror or exc}. "
                        "Another process is holding the file open "
                        "(OneDrive, an editor, or an indexer). "
                        "Nothing was written.",
                        EXIT_INTERNAL,
                    ) from exc
                time.sleep(delay)
                delay = min(delay * 2, 0.25)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_text(path: Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------
# Locking
# --------------------------------------------------------------------------


def _process_alive(pid: int) -> bool:
    """Best-effort liveness check. Errs towards 'alive' when unsure.

    A false 'alive' costs a wait; a false 'dead' would let two writers into the
    critical section, so ambiguity always resolves to alive.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if out.returncode != 0:
            return True
        return f'"{pid}"' in out.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


class FileLock:
    """Exclusive lock for the short read-modify-replace window.

    Never held while scanning a tree or running checks. A stale-looking lock is
    broken only after the owning PID is confirmed gone.
    """

    def __init__(self, directory: Path, name: str = LOCK_NAME,
                 timeout: float = LOCK_WAIT_SECONDS) -> None:
        self.path = Path(directory) / name
        self.timeout = timeout
        self._held = False
        self._denied = False

    def _payload(self) -> str:
        return json.dumps({"pid": os.getpid(), "acquired": time.time()})

    def _try_acquire(self) -> bool:
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except PermissionError:
            # Windows: the previous holder's lock file is still being deleted
            # (delete-pending), so it can be neither opened nor created. Busy, not
            # forbidden. Elsewhere this error means the directory is read-only.
            if os.name == "nt":
                self._denied = True
                return False
            raise
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return False
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self._payload())
        self._held = True
        return True

    def _break_if_dead(self) -> bool:
        """Remove the lock only when its owner is provably gone."""
        try:
            raw = read_text(self.path)
        except OSError:
            return False  # held open or being deleted: owned for now
        if raw is None:
            return True  # vanished; caller retries
        try:
            info = json.loads(raw)
            pid = int(info.get("pid", -1))
            acquired = float(info.get("acquired", 0.0))
        except (ValueError, TypeError):
            # Unparseable (an owner between create and write, or a torn file): owned
            # until the file itself ages out, then dropped.
            pid = -1
            try:
                acquired = self.path.stat().st_mtime
            except OSError:
                return False
        if time.time() - acquired < LOCK_STALE_SECONDS:
            return False
        if pid > 0 and _process_alive(pid):
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True

    def acquire(self) -> None:
        self._denied = False
        deadline = time.monotonic() + self.timeout
        next_check = 0.0
        while True:
            if self._try_acquire():
                return
            # Reading the lock opens it, and on Windows an open file cannot be deleted:
            # waiters reading it every poll made the holder's release fail. A stale
            # lock is minutes old, so checking once a second loses nothing.
            if time.monotonic() >= next_check:
                self._break_if_dead()
                next_check = time.monotonic() + LOCK_STALE_CHECK_SECONDS
            if time.monotonic() >= deadline:
                if self._denied and not self.path.exists():
                    raise DcError(
                        f"could not create {self.path} within {self.timeout:.0f}s: permission "
                        f"denied; check write access to {self.path.parent}",
                        EXIT_INTERNAL,
                    )
                raise DcError(
                    f"could not acquire {self.path} within {self.timeout:.0f}s; "
                    "another DrainClamp process is active",
                    EXIT_INTERNAL,
                )
            time.sleep(LOCK_POLL_SECONDS)

    def release(self) -> None:
        if not self._held:
            return
        # Windows refuses the delete while another process has the file open (a
        # waiter, an indexer). Give up early and the lock outlives its holder until it
        # goes stale, so every waiter times out: retry like atomic_write does.
        deadline = time.monotonic() + REPLACE_RETRY_SECONDS
        try:
            while True:
                try:
                    self.path.unlink()
                    return
                except FileNotFoundError:
                    return
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(REPLACE_POLL_SECONDS)
        finally:
            self._held = False

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> bool:
        self.release()
        return False


# --------------------------------------------------------------------------
# Output normalisation
#
# A "<=10 lines" contract means nothing if a tool can emit one 4 MB line, a
# screenful of ANSI, or a NUL byte. Every captured stream passes through here.
# --------------------------------------------------------------------------

MAX_CAPTURE_BYTES = 256 * 1024
MAX_LINE_CHARS = 500
DEFAULT_TIMEOUT_SECONDS = 120

# CSI / OSC escape sequences, plus the standalone ESC that survives them.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b")
# C0/C1 controls except tab and newline. Carriage returns are handled first.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def normalise_output(text: str, max_line: int = MAX_LINE_CHARS) -> str:
    """Make captured output safe to print into a bounded report.

    Strips ANSI and control characters, resolves progress-bar carriage returns
    to their final state, and caps line length with an explicit marker so a
    truncation is never mistaken for the whole line.
    """
    text = strip_ansi(text)
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        # A progress bar rewrites one line many times; keep the last state.
        if "\r" in raw:
            raw = raw.split("\r")[-1]
        clean = _CONTROL_RE.sub("", raw).rstrip()
        if len(clean) > max_line:
            clean = clean[:max_line] + f" …[TRUNCATED {len(clean)} chars]"
        lines.append(clean)
    return "\n".join(lines)


def collapse(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """One-line summary: whitespace collapsed, length capped."""
    single = " ".join(normalise_output(text).split())
    if len(single) > limit:
        single = single[:limit] + " …[TRUNCATED]"
    return single


def first_failure_line(text: str) -> str:
    """The most useful single line from a failing run.

    Prefers an assertion or explicit error; falls back to the last non-empty
    line, which is where most runners put their summary.
    """
    lines = [ln for ln in normalise_output(text).split("\n") if ln.strip()]
    if not lines:
        return ""
    for line in lines:
        low = line.lower()
        if low.startswith(("e   ", "assert", "error", "fail")) or "assertionerror" in low:
            return collapse(line)
        if re.match(r"^[^\s:]+:\d+:", line):  # path:line: diagnostic
            return collapse(line)
    return collapse(lines[-1])


# --------------------------------------------------------------------------
# Subprocess sandbox
#
# Never a shell. Always an argv array. Always inside the repository.
# --------------------------------------------------------------------------

# Characters that only mean anything to a shell.
#
# Two different jobs, so two different checks:
#
# `check_argv` guards *our own* calls. Without a shell these characters are
# literal, so `python -c "import sys; sys.exit(3)"` is perfectly safe and must
# not be blocked. Only argv[0] is scanned — an executable name containing them
# is malformed.
#
# `check_repo_argv` guards entries read out of a repository file. There, a
# metacharacter means the author wrote the entry expecting shell parsing that it
# will never get, so the command would silently do something other than what it
# reads as. That is rejected.
#
# Neither check is the security boundary. Safety comes from never using a shell
# and from the runner allowlist — `python -c` can do anything at all without a
# single metacharacter in sight.
SHELL_METACHARS = set(";|&><`$\n\r")


@dataclass
class RunResult:
    argv: list[str]
    cwd: str
    returncode: int | None
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    truncated: bool = False
    missing: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def output(self) -> str:
        joined = self.stdout
        if self.stderr.strip():
            joined = f"{joined}\n{self.stderr}" if joined.strip() else self.stderr
        return joined


def check_argv(argv: list[str]) -> None:
    """Structural validation for any command we are about to run.

    Arguments may contain anything — without a shell they are literal. Only the
    executable itself is constrained.
    """
    if not isinstance(argv, list) or not argv:
        raise DcError("argv must be a non-empty array", EXIT_UNSAFE_COMMAND)
    for part in argv:
        if not isinstance(part, str):
            raise DcError("argv must contain only strings", EXIT_UNSAFE_COMMAND)
    hit = SHELL_METACHARS & set(argv[0])
    if hit:
        raise DcError(
            f"executable {argv[0]!r} contains {''.join(sorted(hit))!r}; "
            "that is not a program name",
            EXIT_UNSAFE_COMMAND,
        )


def check_repo_argv(argv: list[str]) -> None:
    """Strict validation for an argv array read out of a repository file.

    A metacharacter here means the entry was written expecting a shell. It will
    not get one, so the command would run as something other than it reads as.
    Reject it rather than misinterpret it silently.
    """
    check_argv(argv)
    for part in argv:
        hit = SHELL_METACHARS & set(part)
        if hit:
            raise DcError(
                f"argv element {part!r} contains shell metacharacter(s) "
                f"{''.join(sorted(hit))!r}. Checks run without a shell, so this "
                "would not do what it appears to. Split it into separate argv "
                "elements, or route it through the host's approval flow.",
                EXIT_UNSAFE_COMMAND,
            )


def resolve_cwd(cwd: str | Path | None, root: Path) -> Path:
    """Resolve `cwd` and require it to sit at or beneath the repository root.

    Monorepo and per-package checks are legitimate, so anywhere inside the tree
    is allowed. Symlinks are resolved first, so a link out of the tree is caught.
    """
    root = root.resolve()
    candidate = Path(cwd) if cwd else root
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise DcError(f"cannot resolve cwd {cwd!r}: {exc}", EXIT_UNSAFE_COMMAND) from exc
    if not is_within(resolved, root):
        raise DcError(
            f"cwd {cwd!r} resolves to {resolved}, outside the repository at {root}. "
            "Checks run inside the repository only.",
            EXIT_UNSAFE_COMMAND,
        )
    if not resolved.is_dir():
        raise DcError(f"cwd {cwd!r} is not a directory", EXIT_UNSAFE_COMMAND)
    return resolved


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Kill the process *and its children*.

    Killing only the parent leaves orphaned test runners and compilers holding
    file handles — which on Windows then blocks the next atomic write.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
            time.sleep(0.2)
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), 9)
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_sandboxed(argv: list[str], root: Path, cwd: str | Path | None = None,
                  timeout: int = DEFAULT_TIMEOUT_SECONDS,
                  max_bytes: int = MAX_CAPTURE_BYTES) -> RunResult:
    """Run one command with every bound applied.

    Never a shell. argv validated, cwd contained, output decoded with
    replacement, capped in bytes, normalised, and the whole process tree killed
    on timeout.
    """
    check_argv(argv)
    workdir = resolve_cwd(cwd, root)

    if shutil.which(argv[0], path=os.environ.get("PATH")) is None and not Path(argv[0]).exists():
        return RunResult(argv, str(workdir), None, "", "", 0.0, missing=True)

    popen_kwargs: dict = {
        "cwd": str(workdir),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "shell": False,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except FileNotFoundError:
        return RunResult(argv, str(workdir), None, "", "", 0.0, missing=True)
    except OSError as exc:
        raise DcError(f"cannot run {argv[0]!r}: {exc.strerror or exc}") from exc

    timed_out = False
    try:
        raw_out, raw_err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_tree(proc)
        try:
            raw_out, raw_err = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError):
            raw_out, raw_err = b"", b""
    duration = time.monotonic() - started

    truncated = False
    if len(raw_out) > max_bytes:
        raw_out, truncated = raw_out[:max_bytes], True
    if len(raw_err) > max_bytes:
        raw_err, truncated = raw_err[:max_bytes], True

    stdout = normalise_output(raw_out.decode("utf-8", errors="replace"))
    stderr = normalise_output(raw_err.decode("utf-8", errors="replace"))
    if truncated:
        stdout += f"\n[TRUNCATED at {max_bytes} bytes]"

    return RunResult(
        argv=list(argv),
        cwd=str(workdir),
        returncode=None if timed_out else proc.returncode,
        stdout=stdout,
        stderr=stderr,
        duration=duration,
        timed_out=timed_out,
        truncated=truncated,
    )


# --------------------------------------------------------------------------
# CLI helpers
# --------------------------------------------------------------------------


def fail(message: str, code: int = EXIT_INTERNAL) -> None:
    print(f"DRAINCLAMP: {message}", file=sys.stderr)
    sys.exit(code)


def run_cli(main_fn) -> None:
    """Turn DcError into a clean message plus its exit code."""
    try:
        sys.exit(main_fn() or EXIT_OK)
    except DcError as exc:
        fail(str(exc), exc.code)
    except KeyboardInterrupt:
        fail("interrupted", EXIT_INTERNAL)
    except BrokenPipeError:
        # `dc_state.py --show | head` closes the pipe early. Python would
        # otherwise print a traceback at shutdown for a case that is not an
        # error: the reader simply stopped reading. Detach stdout so the
        # interpreter has nothing left to flush.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(EXIT_OK)
