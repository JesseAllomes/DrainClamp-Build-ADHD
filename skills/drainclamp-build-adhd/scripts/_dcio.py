"""Shared I/O primitives for DrainClamp & Build.

Stdlib only. Everything that writes to `.agent/` goes through here so that
atomicity, locking and generation checking are implemented once.

Covers: atomic writes, PID-aware locking, generation validation, exit codes,
repo-root and project-root resolution, the subprocess sandbox, and output normalisation.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
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


PROJECT_MARKER = ".drainclamp-project"


def project_root(start: Path | str | None = None) -> Path:
    """The DrainClamp project a path belongs to.

    In a group repo several projects share one git top level. The project root is
    the nearest folder, walking up from `start` (or cwd) to the top level, that
    holds saved state or a `.drainclamp-project` marker. With neither, an explicit
    `start` inside the repo is honoured as given (the first run of a new project
    in a sub-folder); with no `start` the answer is the git top level, which is
    what `repo_root()` has always returned. Outside git, `start`/cwd unchanged.
    """
    base = Path(start).resolve() if start else Path.cwd().resolve()
    top = repo_root(base)
    if top == base:
        return top
    probe = base
    while True:
        if (probe / AGENT_DIR_NAME / STATE_NAME).is_file() or (probe / PROJECT_MARKER).is_file():
            return probe
        if probe == top or probe.parent == probe:
            break
        probe = probe.parent
    if start and is_within(base, top):
        return base
    return top


def ensure_project_marker(root: Path) -> None:
    """Gate 2: leave a marker where the project root is below the git top level.

    Later runs without `--root` then find the project from any folder inside it.
    Single-project repos (root is the top level) get nothing.
    """
    if repo_root(root) == root.resolve():
        return
    marker = root / PROJECT_MARKER
    if not marker.exists():
        marker.write_bytes(b"")


def git_prefix(root: Path) -> str:
    """`a/` for a project in sub-folder `a` of its repo, `` at the top level or outside git.

    Status and diff output is repo-relative whatever the cwd; this is what to strip
    to make it project-relative.
    """
    try:
        out = subprocess.run(["git", "rev-parse", "--show-prefix"], cwd=str(root),
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = out.stdout.strip().replace("\\", "/") if out.returncode == 0 else ""
    return text if text.endswith("/") and "\0" not in text else ""  # a prefix always ends in `/`


def scope_args(prefix: str) -> list[str]:
    """Pathspec limiting a git status/diff/log to the project's own folder."""
    return ["--", "."] if prefix else []


def project_path(rel: str, prefix: str) -> str | None:
    """A repo-relative path made project-relative; None when it is in another project."""
    if not prefix:
        return rel
    return rel[len(prefix):] if rel.startswith(prefix) else None


def project_head(root: Path) -> list[str]:
    """Git args naming the project's own HEAD: the last commit that touched its folder.

    In a group repo a sibling's commit moves HEAD but not this project, so it must
    not read as a change here. At the top level this is plain `rev-parse HEAD`.
    """
    if git_prefix(root):
        return ["log", "-1", "--format=%H", "--", "."]
    return ["rev-parse", "HEAD"]


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
# Auto-commit at a completed step (G9)
# --------------------------------------------------------------------------
# Repos may auto-push on commit, so a commit here can be a publish. Every guard
# below exists for that: only the step's own paths, never a pre-dirty file, never
# another session's staged file, never on a failed check, a PII hit or an odd repo
# state. Returns plain lines; the first is COMMITTED or NOT COMMITTED.

AUTOCOMMIT_MAX_BYTES = 5 * 1024 * 1024
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbe93904"
GIT_STATE_MARKERS = (("MERGE_HEAD", "a merge"), ("rebase-merge", "a rebase"),
                     ("rebase-apply", "a rebase"), ("CHERRY_PICK_HEAD", "a cherry-pick"),
                     ("REVERT_HEAD", "a revert"), ("BISECT_LOG", "a bisect"))
CODEOWNERS_PLACES = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")
CLEAN_TREE_TIMEOUT = 600


def _git(top: Path, *args: str, env: dict | None = None,
         binary: bool = False) -> tuple[int, str, str]:
    """(rc, stdout, stderr) of git run at `top`, literal pathspecs, never prompting."""
    full = dict(os.environ)
    full.pop("GIT_INDEX_FILE", None)
    full["GIT_TERMINAL_PROMPT"] = "0"
    full.update(env or {})
    try:
        proc = subprocess.run(["git", "-C", str(top), "--literal-pathspecs",
                               "-c", "core.quotepath=false", *args],
                              capture_output=True, env=full, timeout=300,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    out = proc.stdout if binary else proc.stdout.decode("utf-8", "replace")
    return proc.returncode, out, proc.stderr.decode("utf-8", "replace").strip()


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "gone"


def _in_agent_dir(rel: str, prefix: str) -> bool:
    return rel.startswith(prefix + AGENT_DIR_NAME + "/")


def dirty_files(root: Path) -> dict[str, str]:
    """{repo-relative path: porcelain status} for the project's own folder, minus `.agent/`."""
    top, prefix = repo_root(root), git_prefix(root)
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if prefix:
        args += ["--", prefix.rstrip("/")]
    rc, out, err = _git(top, *args)
    if rc != 0:
        raise DcError(f"git status failed: {collapse(err)}")
    parts, found, i = out.split("\0"), {}, 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, rel = entry[:2], entry[3:]
        if code[0] in "RC":
            i += 1  # the old name follows
        if not _in_agent_dir(rel, prefix):
            found[rel] = code
    return found


def dirty_snapshot(root: Path) -> dict:
    """What is already edited before a step starts: {head, files: {path: sha256}}."""
    top = repo_root(root)
    rc, head, _ = _git(top, "rev-parse", "--verify", "-q", "HEAD")
    return {"head": head.strip() if rc == 0 else "",
            "files": {rel: _file_hash(top / rel) for rel in dirty_files(root)}}


def _under(rel: str, targets: list[str]) -> bool:
    return any(rel == t or rel.startswith(t.rstrip("/") + "/") for t in targets)


def step_candidates(root: Path, snapshot: dict, targets: list[str] | None = None
                    ) -> tuple[list[str], int, list[str]]:
    """(paths to commit, pre-dirty files still dirty, new-dirty paths outside the targets).

    A path dirty at the snapshot is never a candidate, changed since or not: it may hold
    the user's or another session's hunks. `targets` are project-relative paths or folders.
    """
    prefix = git_prefix(root)
    before = set(snapshot.get("files", {}))
    now = dirty_files(root)
    wanted = [prefix + t.replace("\\", "/").lstrip("./") for t in (targets or [])]
    keep, outside = [], []
    for rel in sorted(now):
        if rel in before:
            continue
        (keep if not wanted or _under(rel, wanted) else outside).append(rel)
    return keep, sum(1 for rel in now if rel in before), outside


def _review_required(top: Path, forced: bool | None) -> bool:
    return bool(forced) or any((top / p).is_file() for p in CODEOWNERS_PLACES)


def _default_branch(top: Path) -> set[str]:
    rc, out, _ = _git(top, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    names = {"main", "master"}
    if rc == 0 and out.strip():
        names.add(out.strip().split("/", 1)[-1])
    return names


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerror)


def _clean_tree(top: Path, prefix: str, commit: str, argv: list[str], scratch: Path) -> str | None:
    """Run `argv` in a temporary worktree at `commit`. None on pass, else the reason."""
    check_repo_argv(argv)
    hooks = scratch / "nohooks"
    hooks.mkdir()
    wt = scratch / "wt"
    rc, _, err = _git(top, "-c", f"core.hooksPath={hooks}", "worktree", "add", "--detach",
                      "-q", str(wt), commit)
    if rc != 0:
        return f"clean-tree worktree failed: {collapse(err)}"
    try:
        cwd = wt / prefix if prefix else wt
        try:
            proc = subprocess.run(argv, cwd=str(cwd), capture_output=True,
                                  timeout=CLEAN_TREE_TIMEOUT, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return f"clean-tree command not found: {argv[0]}"
        except subprocess.TimeoutExpired:
            return "clean-tree check timed out"
        if proc.returncode != 0:
            text = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", "replace")
            why = first_failure_line(text) or f"exit {proc.returncode}"
            return f"clean-tree check failed at the new commit: {why}"
        return None
    finally:
        _git(top, "worktree", "remove", "--force", str(wt))
        if wt.exists():
            _rmtree(wt)
        _git(top, "worktree", "prune")


def _pii_hits(top: Path, paths: list[str], env: dict, message: str,
              home: str | None) -> list[dict]:
    """Scan the exact staged diff (temp index vs HEAD), file names and message."""
    import dc_pii  # lazy: dc_pii imports this module
    opts = dc_pii.options(dc_pii.config(home))
    allow = dc_pii.parse_allow(read_text(top / dc_pii.ALLOW_FILE))
    hits: list[dict] = []
    rc, head, _ = _git(top, "rev-parse", "--verify", "-q", "HEAD")
    base = head.strip() if rc == 0 else EMPTY_TREE
    rc, diff, err = _git(top, "diff", "--cached", "-U0", "--no-color", "--no-ext-diff",
                         "--no-textconv", "-M", base, env=env)
    if rc != 0:
        raise DcError(f"git diff failed: {collapse(err)}")
    files, binaries = dc_pii.added_by_file(diff)
    for path, rows in files.items():
        if not dc_pii._path_allowed(path, allow):
            dc_pii.scan_text(hits, opts, allow, "staged", path, rows)
    for path in binaries:
        blob = (top / path).read_bytes() if (top / path).is_file() else b""
        dc_pii.scan_blob(top, hits, opts, allow, "staged", path, blob=blob)
    for path in paths:
        dc_pii.scan_text(hits, opts, allow, "staged", f"{path} (file name)",
                         [(0, re.sub(r"[/\\_.]+", " ", path))])
    dc_pii.scan_text(hits, opts, allow, "staged", "(commit message)",
                     list(enumerate(message.split("\n"), 1)))
    return hits


def auto_commit(root: Path, paths: list[str], subject: str, trailers: list[str] | None = None,
                branch: str = "", review_required: bool | None = None,
                clean_argv: list[str] | None = None, home: str | None = None) -> list[str]:
    """Commit exactly `paths` (repo-relative). See the block comment above for the guards."""
    def no(reason: str) -> list[str]:
        return [f"NOT COMMITTED: {reason}"]

    top = repo_root(root)
    if not paths:
        return no("no files changed by this step")
    rc, gitdir, err = _git(top, "rev-parse", "--absolute-git-dir")
    if rc != 0:
        return no(f"not a git repository ({collapse(err)})")
    gitdir_path = Path(gitdir.strip())
    for marker, what in GIT_STATE_MARKERS:
        if (gitdir_path / marker).exists():
            return no(f"{what} is in progress; finish it first")
    rc, ref, _ = _git(top, "symbolic-ref", "-q", "HEAD")
    if rc != 0:
        return no("HEAD is detached; switch to a branch first")
    current = ref.strip().removeprefix("refs/heads/")
    for _try in range(2):
        if not (gitdir_path / "index.lock").exists():
            break
        time.sleep(1.5)
    else:
        return no("the git index is locked (another git process or a stale index.lock)")
    rc, head, _ = _git(top, "rev-parse", "--verify", "-q", "HEAD")
    has_head = rc == 0

    message = subject.strip().splitlines()[0][:100] if subject.strip() else "Complete step"
    trailer_block = "\n".join(t.strip() for t in (trailers or []) if t.strip())
    full_message = message + ("\n\n" + trailer_block if trailer_block else "")

    new_paths = []
    for rel in paths:
        fp = top / rel
        tracked = has_head and _git(top, "cat-file", "-e", f"HEAD:{rel}")[0] == 0
        if fp.is_file():
            if fp.stat().st_size > AUTOCOMMIT_MAX_BYTES:
                return no(f"{rel} is over 5 MB; needs your go")
            if not tracked:
                with open(fp, "rb") as handle:
                    if b"\0" in handle.read(8000):
                        return no(f"{rel} is a new binary file; needs your go")
        if not tracked:
            new_paths.append(rel)

    scratch = Path(tempfile.mkdtemp(prefix="dc-autocommit-"))
    try:
        env = {"GIT_INDEX_FILE": str(scratch / "index")}
        rc, _, err = _git(top, "read-tree", "HEAD" if has_head else "--empty", env=env)
        if rc == 0:
            rc, _, err = _git(top, "add", "--", *paths, env=env)
        if rc != 0:
            return no(f"could not stage the step's files: {collapse(err)}")
        if has_head and _git(top, "diff", "--cached", "--quiet", env=env)[0] == 0:
            return no("the step's files have no net change")

        hits = _pii_hits(top, paths, env, full_message, home)
        if hits:
            kinds = sorted({h["kind"] for h in hits})
            where = sorted({h["where"] for h in hits})
            shown = ", ".join(where[:5]) + (f" (+{len(where) - 5} more)" if len(where) > 5 else "")
            return no(f"PII/secret scan hit ({', '.join(kinds)}) in {shown}; values not shown. "
                      "Remove the data, or allow it with dc_pii.py allow")

        lines: list[str] = []
        target = current
        if branch and _review_required(top, review_required) and current in _default_branch(top):
            target = branch
        if clean_argv:
            rc, tree, err = _git(top, "write-tree", env=env)
            cmt = ""
            if rc == 0:
                cargs = ["commit-tree", tree.strip(), "-m", full_message]
                if has_head:
                    cargs[2:2] = ["-p", head.strip()]
                rc, cmt, err = _git(top, *cargs)
            if rc != 0:
                return no(f"could not build the candidate commit: {collapse(err)}")
            why = _clean_tree(top, git_prefix(root), cmt.strip(), clean_argv, scratch)
            if why:
                return no(why)
        if target != current:
            exists = _git(top, "rev-parse", "--verify", "-q", f"refs/heads/{target}")[0] == 0
            rc, _, err = _git(top, "checkout", *([] if exists else ["-b"]), target)
            if rc != 0:
                return no(f"could not switch to work branch {target}: {collapse(err)}")
            lines.append(f"PR needed: review-required repo, committed to {target}, not {current}")

        for attempt in range(2):
            rc, err = 0, ""
            if new_paths:
                rc, _, err = _git(top, "add", "--", *new_paths)
            if rc == 0:
                margs = ["-m", message] + (["-m", trailer_block] if trailer_block else [])
                rc, _, err = _git(top, "commit", "--only", *margs, "--", *paths)
            if rc == 0:
                break
            if attempt == 0 and "index.lock" in err:
                time.sleep(1.5)
                continue
            if new_paths:  # leave nothing of ours staged
                _git(top, "restore", "--staged", "--", *new_paths)
            return no(f"git commit refused: {collapse(err) or 'no reason given'}")
        _, short, _ = _git(top, "rev-parse", "--short", "HEAD")
        _, names, _ = _git(top, "diff-tree", "--no-commit-id", "--name-only", "-r",
                           "--root", "HEAD")
        count = len([n for n in names.splitlines() if n.strip()])
        return [f"COMMITTED: {short.strip()} {message} ({count} file(s))"] + lines
    except DcError as exc:
        return no(str(exc))
    finally:
        _rmtree(scratch)


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
