#!/usr/bin/env python3
"""Fixtures and verification matrix for DrainClamp & Build.

Two jobs:

1. `--materialise` writes the fixture families the suites need — Python,
   JavaScript, an unsupported extension, malformed supported source, paths with
   spaces, Unicode filenames, CRLF, a directory link, a git repository with an
   untracked file, an empty roadmap, and duplicate symbol names.
2. With no arguments it materialises into a temporary directory and runs every
   `tests/t_*.py` suite, aggregating their results.

Fixtures are generated, never hand-edited: a suite that needs a new shape adds
it here so every suite sees the same tree. Anything the platform refuses to
create (a directory link without the privilege for it) is reported in the
manifest as `SKIPPED`, never silently omitted — a missing fixture that looks
like a passing test is the failure this avoids.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import _dcio
import dc_map
from _dcio import DcError

FIXTURE_SCHEMA = 1
MANIFEST_NAME = "MANIFEST.txt"

# --------------------------------------------------------------------------
# Fixture bodies
# --------------------------------------------------------------------------

CORE_PY = '''"""Fixture module: one class with methods, one module function."""


class Widget:
    def build(self):
        return 1

    def teardown(self):
        return 2


def helper(value):
    return Widget().build() + value


def orphan_helper(value):
    """Never referenced anywhere: a dead-code *candidate*, not a fact."""
    return value * 2
'''

DUPLICATE_A_PY = '''def handle(event):
    return ("a", event)
'''

DUPLICATE_B_PY = '''def handle(event):
    return ("b", event)
'''

MALFORMED_PY = '''def oops(:
    return 1
'''

# Deliberately distinct symbol names: a copy of core.py would double every name
# and make a genuinely unreferenced symbol look referenced.
CRLF_PY = '''class CrlfWidget:
    def assemble(self):
        return 1


def crlf_entry(value):
    return CrlfWidget().assemble() + value
'''

UNICODE_PY = '''def naïve_sum(values):
    return sum(values)
'''

SPACED_PY = '''def spaced_entry():
    return "path with spaces"
'''

APP_JS = '''export function jsThing(x) {
  return x + 1;
}

async function loadAll(source) {
  return await source.read();
}

export class Panel {
  render() {
    return "panel";
  }
}

const arrowThing = (a, b) => a + b;

export default jsThing;
'''

LEGACY_RB = '''def legacy_entry
  1
end
'''

EMPTY_ROADMAP_MD = """| id | goal | files | status |
|---|---|---|---|
"""

CALC_PY = '''def add(a, b):
    return a + b


def subtract(a, b):
    return a - b
'''

TEST_CALC_PY = '''import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.calc import add, subtract  # noqa: E402


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2
'''

PYPROJECT_TOML = """[project]
name = "calc"
version = "0.1.0"
dependencies = ["requests"]

[tool.pytest.ini_options]
testpaths = ["tests"]
"""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def write(path: Path, text: str, *, newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as handle:
        handle.write(text)


# A commit's hash covers its author and committer identity and timestamps, so
# a fixture committed with the ambient clock gets a different HEAD every run.
# Pinning all six makes the repository byte-identical across runs and hosts,
# which is what lets the suite assert that materialising twice is a no-op.
FIXTURE_IDENTITY = "fixture"
FIXTURE_EMAIL = "fixture@example.invalid"
FIXTURE_DATE = "2020-01-01T00:00:00+00:00"


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": FIXTURE_IDENTITY,
        "GIT_AUTHOR_EMAIL": FIXTURE_EMAIL,
        "GIT_AUTHOR_DATE": FIXTURE_DATE,
        "GIT_COMMITTER_NAME": FIXTURE_IDENTITY,
        "GIT_COMMITTER_EMAIL": FIXTURE_EMAIL,
        "GIT_COMMITTER_DATE": FIXTURE_DATE,
    })
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                          text=True, env=env)


def link_dir(link: Path, target: Path) -> str:
    """Directory link, by whichever mechanism this platform allows.

    Returns the mechanism used, or a `SKIPPED: …` note. Windows needs either
    developer mode or an elevated prompt for a symlink; a junction needs
    neither, so it is the fallback rather than a silent omission.
    """
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if done.returncode == 0 and link.exists():
            return "junction"
        return f"SKIPPED: no symlink privilege and mklink failed ({done.returncode})"
    return "SKIPPED: platform refused a directory symlink"


# --------------------------------------------------------------------------
# Materialisation
# --------------------------------------------------------------------------


def materialise(dest: Path, *, clean: bool = True) -> dict:
    """Write every fixture family under `dest`. Returns the manifest."""
    dest = Path(os.path.abspath(str(dest)))
    if dest == Path(dest.anchor) or dc_map.is_link(dest):
        raise DcError(f"refusing root or linked fixture destination: {dest}")
    if dest.resolve() != dest:
        raise DcError(f"fixture destination resolves away from its literal path: {dest}")
    if dest.exists():
        if not dest.is_dir():
            raise DcError(f"fixture destination is not a directory: {dest}")
        if any(dest.iterdir()):
            marker = dest / MANIFEST_NAME
            if dc_map.is_link(marker) or not marker.is_file():
                raise DcError(f"fixture destination is not owned: {dest}")
            try:
                header = marker.read_text(encoding="utf-8").splitlines()[0]
            except (OSError, UnicodeError, IndexError) as exc:
                raise DcError(f"cannot read fixture ownership marker: {dest}") from exc
            if header != "# drainclamp-build fixtures; schema=1":
                raise DcError(f"fixture destination is not owned: {dest}")
        if clean:
            def retry_owned_unlink(operation, path, exc_info):
                error = exc_info[1]
                candidate = Path(path)
                if (not isinstance(error, PermissionError)
                        or operation not in (os.unlink, os.remove)
                        or dc_map.is_link(candidate)
                        or not _dcio.is_within(candidate.resolve(), dest)):
                    raise error.with_traceback(exc_info[2])
                mode = candidate.lstat().st_mode
                if not stat.S_ISREG(mode):
                    raise error.with_traceback(exc_info[2])
                os.chmod(candidate, mode | stat.S_IWUSR)
                operation(path)
            shutil.rmtree(dest, onerror=retry_owned_unlink)
    dest.mkdir(parents=True, exist_ok=True)

    notes: list[str] = []

    # Python: symbols, duplicates, malformed, CRLF, Unicode.
    write(dest / "py_pkg" / "__init__.py", "")
    write(dest / "py_pkg" / "core.py", CORE_PY)
    write(dest / "py_pkg" / "duplicate_a.py", DUPLICATE_A_PY)
    write(dest / "py_pkg" / "duplicate_b.py", DUPLICATE_B_PY)
    write(dest / "py_pkg" / "malformed.py", MALFORMED_PY)
    write(dest / "py_pkg" / "crlf_module.py", CRLF_PY, newline="\r\n")
    unicode_name = "módulo_naïve.py"
    try:
        write(dest / "py_pkg" / unicode_name, UNICODE_PY)
    except (OSError, UnicodeError) as exc:  # exotic filesystem encoding
        notes.append(f"unicode filename SKIPPED: {exc}")
        unicode_name = ""

    # Deeply nested, so depth-2 rollups have something to roll up.
    write(dest / "py_pkg" / "deep" / "deeper" / "deepest" / "buried.py",
          "def buried_entry():\n    return 1\n")

    # JavaScript and an extension with no grammar at all.
    write(dest / "js_pkg" / "app.js", APP_JS)
    write(dest / "unsupported" / "legacy.rb", LEGACY_RB)

    # Paths with spaces.
    write(dest / "spaced dir" / "module with spaces.py", SPACED_PY)

    # Directory link (or an explicit note that this platform refused).
    links = dest / "links"
    links.mkdir(exist_ok=True)
    mechanism = link_dir(links / "linked_pkg", dest / "py_pkg")
    if mechanism.startswith("SKIPPED"):
        notes.append(f"directory link {mechanism}")

    # State fragments used by the state and purge suites.
    write(dest / "state" / "roadmap_empty.md", EMPTY_ROADMAP_MD)
    write(dest / "state" / "verify_valid.json",
          '[{"id": "pytest-unit", "argv": ["pytest", "tests/"], '
          '"cwd": ".", "tier": "milestone"}]\n')
    write(dest / "state" / "verify_forged.json",
          '[{"id": "x", "argv": ["pytest"], "cwd": ".", "tier": "fast", '
          '"source": "user-approved"}]\n')

    # A real git repository: committed tree plus one untracked file.
    repo = dest / "repo_calc"
    write(repo / "src" / "calc.py", CALC_PY)
    write(repo / "tests" / "test_calc.py", TEST_CALC_PY)
    write(repo / "pyproject.toml", PYPROJECT_TOML)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", FIXTURE_EMAIL)
    git(repo, "config", "user.name", FIXTURE_IDENTITY)
    # A host with core.autocrlf=true would rewrite these LF blobs on the way in
    # and change every object id with them.
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fixture: initial")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    if not head:
        notes.append("git fixture SKIPPED: git unavailable or commit refused")
    write(repo / "notes_untracked.py", "def untracked_entry():\n    return 1\n")

    manifest = {
        "schema": FIXTURE_SCHEMA,
        "dest": str(dest),
        "families": {
            "python": "py_pkg/core.py, duplicate_a.py, duplicate_b.py",
            "duplicate symbols": "py_pkg/duplicate_a.py::handle, duplicate_b.py::handle",
            "malformed python": "py_pkg/malformed.py",
            "crlf": "py_pkg/crlf_module.py",
            "unicode filename": f"py_pkg/{unicode_name}" if unicode_name else "SKIPPED",
            "deep nesting": "py_pkg/deep/deeper/deepest/buried.py",
            "javascript": "js_pkg/app.js",
            "unsupported extension": "unsupported/legacy.rb",
            "paths with spaces": "spaced dir/module with spaces.py",
            "directory link": f"links/linked_pkg ({mechanism})",
            "git repo + untracked": f"repo_calc (HEAD {head[:8] or 'none'})",
            "empty roadmap": "state/roadmap_empty.md",
        },
        "notes": notes,
    }

    lines = [f"# drainclamp-build fixtures; schema={FIXTURE_SCHEMA}", ""]
    lines += [f"{name}: {value}" for name, value in manifest["families"].items()]
    if notes:
        lines += ["", "NOTES:"] + [f"  {note}" for note in notes]
    write(dest / MANIFEST_NAME, "\n".join(lines) + "\n")
    return manifest


# --------------------------------------------------------------------------
# Matrix
# --------------------------------------------------------------------------


def suite_dir() -> Path | None:
    """`tests/` in the source checkout, or None when running from an install.

    An installed skill carries scripts, not suites, so the matrix is simply
    unavailable there — reported, not faked.
    """
    candidate = Path(__file__).resolve().parents[3] / "tests"
    return candidate if candidate.is_dir() else None


def run_suites(tests: Path, fixtures: Path, only: str | None) -> tuple[list[tuple[str, int, str]], int]:
    """Run each `t_*.py`. Returns ([(name, rc, tail)], failed count)."""
    env = dict(os.environ)
    env["DC_FIXTURES"] = str(fixtures)
    # The fixtures include a Unicode filename, and a suite that prints it must
    # not die on a console codepage. Bytes in, UTF-8 with replacement out — the
    # same discipline the sandbox uses.
    env["PYTHONIOENCODING"] = "utf-8"
    # Every state write registers its repository, and the suites write state in
    # dozens of throwaway fixtures. Without an override those land in the real
    # `~/.drainclamp/projects.json` and the next session menu opens 60 temp
    # directories deep. The registry follows the fixtures into the sandbox.
    registry_home = fixtures.parent / "registry-home"
    registry_home.mkdir(parents=True, exist_ok=True)
    env["DRAINCLAMP_HOME"] = str(registry_home)
    results: list[tuple[str, int, str]] = []
    failed = 0
    for script in sorted(tests.glob("t_*.py")):
        if only and only not in script.stem:
            continue
        done = subprocess.run(
            [sys.executable, "-B", str(script)],
            capture_output=True, env=env, cwd=str(tests.parent),
        )
        stdout = (done.stdout or b"").decode("utf-8", errors="replace")
        stderr = (done.stderr or b"").decode("utf-8", errors="replace")
        tail = ""
        for line in reversed(_dcio.normalise_output(stdout).splitlines()):
            if line.startswith("FAILURES:"):
                tail = line
                break
        if not tail:
            tail = _dcio.first_failure_line(f"{stdout}\n{stderr}")
        if done.returncode != 0:
            failed += 1
        results.append((script.stem, done.returncode, _dcio.collapse(tail, 120)))
    return results, failed


def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_selftest.py", description=__doc__)
    parser.add_argument("--materialise", action="store_true",
                        help="write fixtures and exit")
    parser.add_argument("--dest", default=None,
                        help="fixture directory (default: <repo>/tests/fixtures)")
    parser.add_argument("--only", help="run only suites whose name contains this")
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary fixture tree after a run")
    args = parser.parse_args()

    tests = suite_dir()
    if args.dest:
        dest = Path(args.dest)
    elif tests is not None:
        dest = tests / "fixtures"
    elif args.materialise:
        raise DcError("--dest is required when running from an installed skill")
    else:
        dest = Path(tempfile.mkdtemp(prefix="dcfix-"))

    if args.materialise:
        manifest = materialise(dest)
        print(f"FIXTURES: {len(manifest['families'])} families -> {dest}")
        for note in manifest["notes"]:
            print(f"SKIPPED: {note}")
        print(f"manifest: {(dest / MANIFEST_NAME)}")
        return _dcio.EXIT_OK

    if tests is None:
        raise DcError(
            "no tests/ directory beside this skill; the matrix runs from the "
            "source checkout. Use --materialise --dest <path> instead.",
            _dcio.EXIT_NO_CHECKS,
        )

    temp = Path(tempfile.mkdtemp(prefix="dcfix-"))
    materialise(temp)
    results, failed = run_suites(tests, temp, args.only)

    if not results:
        print("NO-CHECKS-RUN: no suite matched")
        return _dcio.EXIT_NO_CHECKS

    for name, code, tail in results:
        print(f"{'PASS' if code == 0 else 'FAIL'}  {name}  {tail}")
    print(f"RESULT: {'FAIL' if failed else 'PASS'} "
          f"({len(results) - failed}/{len(results)} suites)")
    if args.keep:
        print(f"fixtures kept: {temp}")
    else:
        shutil.rmtree(temp, ignore_errors=True)
    return _dcio.EXIT_CHECK_FAILED if failed else _dcio.EXIT_OK


if __name__ == "__main__":
    _dcio.run_cli(main)
