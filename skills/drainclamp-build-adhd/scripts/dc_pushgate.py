#!/usr/bin/env python3
"""Machine-wide PII push gate: a git pre-push hook for every repository.

`install` copies the scanner (dc_pii.py and what it imports) to a stable place
under ~/.drainclamp/pii-gate, writes hook dispatchers to ~/.drainclamp/git-hooks
and points `git config --global core.hooksPath` at them. From then on every
`git push`, from any tool, runs the scanner first and is refused when it finds
PII; a scanner that is missing or crashes refuses too.

A global hooks path replaces each repository's own `.git/hooks`, so every
dispatcher runs the repository's hook of the same name afterwards (pre-push
gets the same ref lines on stdin). Two hooks git fires on almost every command
(reference-transaction, post-index-change) get no dispatcher, to keep git fast;
a repository that relies on one of those keeps it only with a local hooksPath.

A repository with its own `core.hooksPath` (husky and similar) bypasses the
global one; `status` says so. `check` is Gate P: it scans what a push of the
current branch would publish, with the source scanner, and reports the hook
status and pii-redact drift alongside.

Commands: install [--force], uninstall, status, check.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import _dcio
import dc_registry
from _dcio import DcError

SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
GATE_FILES = {"scripts/dc_pii.py": SCRIPTS / "dc_pii.py",
              "scripts/_dcio.py": SCRIPTS / "_dcio.py",
              "scripts/dc_registry.py": SCRIPTS / "dc_registry.py",
              "assets/given-names.txt": SKILL / "assets" / "given-names.txt"}
HOOK_NAMES = ("applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit",
              "pre-merge-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-rebase",
              "post-checkout", "post-merge", "pre-push", "pre-receive", "update", "proc-receive",
              "post-receive", "post-update", "push-to-checkout", "pre-auto-gc", "post-rewrite",
              "sendemail-validate", "p4-changelist", "p4-prepare-changelist",
              "p4-post-changelist", "p4-pre-submit")
MARK = "# drainclamp push gate dispatcher"
GIT_TIMEOUT = 30


def gate_dir(home: str | None) -> Path:
    return dc_registry.home_dir(home) / "pii-gate"


def hooks_dir(home: str | None) -> Path:
    return dc_registry.home_dir(home) / "git-hooks"


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DcError(f"git {args[0]} failed: {exc}") from None
    if check and proc.returncode not in (0, 1):  # 1 = config key not set
        raise DcError(f"git {' '.join(args[:3])} failed: {_dcio.collapse(proc.stderr.strip())}")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _sh_path(p: Path | str) -> str:
    return str(p).replace("\\", "/")


def _same(a: str, b: Path) -> bool:
    if not a:
        return False
    try:
        return Path(os.path.expanduser(a)).resolve() == b.resolve()
    except OSError:
        return False


def dispatcher(name: str, python: str, gate: Path) -> str:
    """The shell script git runs for hook `name`."""
    chain = ('hook="$(git rev-parse --git-common-dir)/hooks/$(basename "$0")"\n')
    if name != "pre-push":
        return (f"#!/bin/sh\n{MARK}: runs the repository's own hook of this name.\n{chain}"
                'if [ -x "$hook" ]; then exec "$hook" "$@"; fi\nexit 0\n')
    return (f"#!/bin/sh\n{MARK}: PII scan, then the repository's own pre-push.\n"
            "input=$(cat)\n"
            f'PY="{python}"\n'
            '[ -x "$PY" ] || PY=$(command -v python3 || command -v python || command -v py)\n'
            'if [ -z "$PY" ]; then echo "DRAINCLAMP PII: no Python; push refused (fail closed)" >&2; exit 1; fi\n'
            f'printf \'%s\\n\' "$input" | "$PY" -B "{_sh_path(gate)}/scripts/dc_pii.py" prepush || exit 1\n'
            f"{chain}"
            'if [ -x "$hook" ]; then printf \'%s\\n\' "$input" | "$hook" "$@"; exit $?; fi\nexit 0\n')


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(home: str | None, force: bool = False) -> list[str]:
    hooks, gate = hooks_dir(home), gate_dir(home)
    current = _git("config", "--global", "--get", "core.hooksPath")
    if current and not _same(current, hooks) and not force:
        raise DcError(f"global core.hooksPath is already {current}; refusing to replace it. "
                      "Merge those hooks by hand, or rerun with --force to take it over.")
    for rel, src in GATE_FILES.items():
        if not src.is_file():
            raise DcError(f"source file missing: {src}")
        dest = gate / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    hooks.mkdir(parents=True, exist_ok=True)
    python = _sh_path(sys.executable)
    for name in HOOK_NAMES:
        path = hooks / name
        existing = _dcio.read_text(path)
        if existing is not None and MARK not in existing:
            raise DcError(f"{path} exists and is not ours; refusing to overwrite it")
        _dcio.atomic_write(path, dispatcher(name, python, gate))
        try:
            path.chmod(0o755)
        except OSError:
            pass
    _git("config", "--global", "core.hooksPath", _sh_path(hooks))
    lines = [f"DRAINCLAMP: push gate installed: core.hooksPath = {_sh_path(hooks)}; "
             f"scanner copied to {_sh_path(gate)}."]
    if current and not _same(current, hooks):
        lines.append(f"WARN: replaced the previous global core.hooksPath ({current}).")
    return lines


def uninstall(home: str | None) -> list[str]:
    hooks, gate = hooks_dir(home), gate_dir(home)
    current = _git("config", "--global", "--get", "core.hooksPath")
    lines = []
    if _same(current, hooks):
        _git("config", "--global", "--unset", "core.hooksPath")
        lines.append("DRAINCLAMP: global core.hooksPath unset.")
    elif current:
        lines.append(f"DRAINCLAMP: global core.hooksPath is {current}, not ours; left as is.")
    for name in HOOK_NAMES:
        path = hooks / name
        text = _dcio.read_text(path)
        if text is not None and MARK in text:
            path.unlink()
    for rel in GATE_FILES:
        (gate / rel).unlink(missing_ok=True)
    for d in (gate / "scripts", gate / "assets", gate, hooks):
        try:
            d.rmdir()  # only when empty: anything else in there is not ours
        except OSError:
            pass
    lines.append("DRAINCLAMP: push gate files removed.")
    return lines


def status(root: Path | None, home: str | None) -> tuple[bool, list[str]]:
    hooks, gate = hooks_dir(home), gate_dir(home)
    if not (hooks / "pre-push").is_file():
        return False, ["PUSH GATE: not installed (run dc_pushgate.py install)."]
    lines, ok = [], True
    effective = _git("config", "--get", "core.hooksPath", cwd=root) if root else \
        _git("config", "--global", "--get", "core.hooksPath")
    if not _same(effective, hooks):
        ok = False
        where = "this repository" if root else "global config"
        lines.append(f"PUSH GATE: INACTIVE for {where}: core.hooksPath = {effective or '(unset)'}.")
    stale = [rel for rel, src in GATE_FILES.items()
             if not (gate / rel).is_file() or _digest(gate / rel) != _digest(src)]
    if stale:
        ok = False
        lines.append(f"PUSH GATE: installed scanner differs from this plugin ({', '.join(stale)}); "
                     "rerun dc_pushgate.py install.")
    if ok:
        lines.append("PUSH GATE: active.")
    return ok, lines


def check(root: Path, home: str | None) -> tuple[int, list[str]]:
    """Gate P: scan what pushing the current branch would publish."""
    head = _git("rev-parse", "HEAD", cwd=root)
    if not head:
        raise DcError("no commits to push")
    upstream = _git("rev-parse", "--verify", "--quiet", "@{u}", cwd=root, check=False)
    line = f"HEAD {head} refs/heads/x {upstream or '0' * 40}\n"
    cmd = [sys.executable, "-B", str(SCRIPTS / "dc_pii.py"), "--root", str(root)]
    if home:
        cmd += ["--home", home]
    scan = subprocess.run(cmd + ["prepush"], input=line, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    drift = subprocess.run(cmd + ["drift"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    _, gate_lines = status(root, home)
    out = [ln for ln in (scan.stdout + scan.stderr).splitlines() if ln.strip()]
    out += gate_lines
    out += [ln for ln in drift.stdout.splitlines() if ln.startswith("WARN")]
    return (_dcio.EXIT_OK if scan.returncode == 0 else _dcio.EXIT_CHECK_FAILED), out


def main() -> int:
    p = argparse.ArgumentParser(prog="dc_pushgate.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=None)
    p.add_argument("--home", default=None, help="~/.drainclamp override")
    sub = p.add_subparsers(dest="cmd", required=True)
    ins = sub.add_parser("install")
    ins.add_argument("--force", action="store_true", help="take over another global hooksPath")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    sub.add_parser("check", help="Gate P: scan what pushing this branch would publish")
    args = p.parse_args()
    if args.cmd == "install":
        print("\n".join(install(args.home, args.force)))
        return _dcio.EXIT_OK
    if args.cmd == "uninstall":
        print("\n".join(uninstall(args.home)))
        return _dcio.EXIT_OK
    root = _dcio.repo_root(args.root) if args.cmd == "check" or args.root else None
    if args.cmd == "status":
        ok, lines = status(root, args.home)
        print("\n".join(lines))
        return _dcio.EXIT_OK if ok else _dcio.EXIT_CHECK_FAILED
    rc, lines = check(root, args.home)
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DcError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(_dcio.EXIT_INTERNAL)
