"""dc_selftest.py checks: every fixture family exists, and gaps are declared.

A fixture that quietly fails to materialise turns a real assertion into a
vacuous one, so the manifest has to say what was skipped and why.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import dc_map  # noqa: E402
import dc_selftest  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


dest = Path(tempfile.mkdtemp(prefix="dcselftest-")) / "fixtures"
manifest = dc_selftest.materialise(dest)

EXPECTED = {
    "py_pkg/core.py", "py_pkg/duplicate_a.py", "py_pkg/duplicate_b.py",
    "py_pkg/malformed.py", "py_pkg/crlf_module.py",
    "py_pkg/deep/deeper/deepest/buried.py", "js_pkg/app.js",
    "unsupported/legacy.rb", "spaced dir/module with spaces.py",
    "state/roadmap_empty.md", "repo_calc/src/calc.py",
    "repo_calc/notes_untracked.py",
}
missing = sorted(rel for rel in EXPECTED if not (dest / rel).is_file())
check("every fixture family is on disk", not missing, ", ".join(missing))
check("the manifest is written", (dest / dc_selftest.MANIFEST_NAME).is_file())

unicode_files = [p for p in (dest / "py_pkg").iterdir() if "naïve" in p.name]
check("the Unicode filename exists or is declared SKIPPED",
      bool(unicode_files) or manifest["families"]["unicode filename"] == "SKIPPED",
      manifest["families"]["unicode filename"])

crlf = (dest / "py_pkg" / "crlf_module.py").read_bytes()
check("the CRLF fixture really has CRLF endings", b"\r\n" in crlf)
check("the CRLF fixture does not duplicate core.py symbols",
      b"orphan_helper" not in crlf)

link = dest / "links" / "linked_pkg"
declared_skip = any("directory link" in note for note in manifest["notes"])
check("the directory link exists or is declared SKIPPED",
      dc_map.is_link(link) or declared_skip,
      manifest["families"]["directory link"])

status = subprocess.run(["git", "status", "--porcelain"], cwd=str(dest / "repo_calc"),
                        capture_output=True, text=True)
check("the git fixture leaves exactly one untracked file",
      [ln.split()[-1] for ln in status.stdout.splitlines()] == ["notes_untracked.py"],
      status.stdout.replace("\n", " | ").strip())

head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(dest / "repo_calc"),
                      capture_output=True, text=True)
check("the git fixture has a HEAD to compare against",
      head.returncode == 0 and len(head.stdout.strip()) >= 7,
      head.stdout.strip()[:12])

status2 = dc_selftest.materialise(dest)
check("materialising twice is deterministic",
      status2["families"] == manifest["families"],
      f"{len(status2['families'])} families both runs")

# The index must see the fixtures as the suites assume: two `handle` symbols,
# one malformed file, one unsupported extension.
records = dc_map.refresh(dest)
handles = [rel for rel, (_key, status, symbols) in records.items()
           if any(name == "handle" for name, _s, _e in symbols)]
check("the duplicate-symbol fixture really is ambiguous", len(handles) == 2,
      ", ".join(sorted(handles)))
check("the malformed fixture is PARSE-FAILED",
      records.get("py_pkg/malformed.py", [None, None])[1] == dc_map.PARSE_FAILED,
      str(records.get("py_pkg/malformed.py", ["", "?"])[1]))
check("the unsupported fixture is UNSUPPORTED",
      records.get("unsupported/legacy.rb", [None, None])[1] == dc_map.UNSUPPORTED,
      str(records.get("unsupported/legacy.rb", ["", "?"])[1]))

out = subprocess.run(
    [sys.executable, "-B", str(SCRIPTS / "dc_selftest.py"), "--materialise",
     "--dest", str(dest)], capture_output=True, text=True)
check("the CLI reports what it wrote", out.returncode == 0 and "FIXTURES:" in out.stdout,
      out.stdout.strip().splitlines()[0] if out.stdout else out.stderr.strip())
check("anything skipped is printed, not swallowed",
      (not manifest["notes"]) or "SKIPPED" in out.stdout,
      "; ".join(manifest["notes"]) or "nothing skipped on this host")

print()
# Destination ownership is checked before either cleanup or overwriting.
from _dcio import DcError
from unittest.mock import patch
unowned = Path(tempfile.mkdtemp(prefix='dc-unowned-'))
(unowned / '.git').mkdir()
(unowned / 'sentinel').write_bytes(b'preserve exactly')
for clean in (True, False):
    (unowned / '.git').mkdir(exist_ok=True)
    (unowned / 'sentinel').write_bytes(b'preserve exactly')
    try:
        dc_selftest.materialise(unowned, clean=clean)
        refused_owned = False
    except DcError:
        refused_owned = True
    check('unowned destination refuses clean=' + str(clean), refused_owned
          and (unowned / 'sentinel').exists()
          and (unowned / 'sentinel').read_bytes() == b'preserve exactly'
          and (unowned / '.git').is_dir() and not (unowned / 'py_pkg').exists())
(unowned / '.git').mkdir(exist_ok=True)
(unowned / 'sentinel').write_bytes(b'preserve exactly')
cli_refused = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'dc_selftest.py'),
                             '--materialise', '--dest', str(unowned)],
                            capture_output=True, text=True)
check('CLI refuses unowned destination', cli_refused.returncode != 0
      and (unowned / 'sentinel').exists()
      and (unowned / 'sentinel').read_bytes() == b'preserve exactly')
(unowned / 'sentinel').write_bytes(b'preserve exactly')
(unowned / 'MANIFEST.txt').write_text('# wrong owner\n', encoding='utf-8')
try:
    dc_selftest.materialise(unowned)
    wrong_owner = False
except DcError:
    wrong_owner = True
check('mismatched fixture marker refuses', wrong_owner and (unowned / 'sentinel').exists())
empty_dest = Path(tempfile.mkdtemp(prefix='dc-empty-'))
check('empty fixture destination works', bool(dc_selftest.materialise(empty_dest)))
owned_manifest_before = (empty_dest / 'MANIFEST.txt').read_bytes()
dc_selftest.materialise(empty_dest)
check('owned manifest rematerialises deterministically',
      (empty_dest / 'MANIFEST.txt').read_bytes() == owned_manifest_before)
with patch.object(dc_selftest.shutil, 'rmtree', side_effect=AssertionError('root cleanup attempted')):
    try:
        dc_selftest.materialise(Path(empty_dest.anchor))
        root_refused = False
    except DcError:
        root_refused = True
    except AssertionError:
        root_refused = False
check('root fixture destination refuses', root_refused)
link_parent = Path(tempfile.mkdtemp(prefix='dc-linkguard-'))
marker_source = link_parent / 'marker-source'
marker_source.write_text('# drainclamp-build fixtures; schema=1\n', encoding='utf-8')
for kind in ('destination', 'marker'):
    target = link_parent / ('target-' + kind)
    target.mkdir()
    (target / 'sentinel').write_bytes(b'keep')
    linked = link_parent / ('linked-' + kind)
    try:
        if kind == 'destination':
            os.symlink(target, linked, target_is_directory=True)
            candidate = linked
        else:
            os.symlink(marker_source, target / 'MANIFEST.txt')
            candidate = target
    except (OSError, NotImplementedError):
        check('linked fixture ' + kind + ' refuses', True, 'SKIPPED: host cannot create link')
        continue
    try:
        dc_selftest.materialise(candidate)
        link_refused = False
    except DcError:
        link_refused = True
    except OSError:
        link_refused = False
    check('linked fixture ' + kind + ' refuses', link_refused
          and (target / 'sentinel').exists()
          and (target / 'sentinel').read_bytes() == b'keep')

# Owned Windows Git objects may be read-only; cleanup retries only unlink.
import stat
readonly_dest = Path(tempfile.mkdtemp(prefix='dc-readonly-'))
(readonly_dest / 'MANIFEST.txt').write_text('# drainclamp-build fixtures; schema=1\n', encoding='utf-8')
readonly_file = readonly_dest / 'owned-object'
readonly_file.write_bytes(b'owned object')
readonly_file.chmod(stat.S_IREAD)
readonly_manifest = dc_selftest.materialise(readonly_dest)
check('owned read-only file can be rematerialised', bool(readonly_manifest)
      and not readonly_file.exists())

retry_dest = Path(tempfile.mkdtemp(prefix='dc-retry-'))
(retry_dest / 'MANIFEST.txt').write_text('# drainclamp-build fixtures; schema=1\n', encoding='utf-8')
retry_file = retry_dest / 'object'
retry_file.write_bytes(b'owned')
retry_file.chmod(stat.S_IREAD)
permission_failure = PermissionError('owned file is read-only')
def simulate_readonly_cleanup(path, **kwargs):
    callback = kwargs.get('onerror')
    if callback is None:
        raise permission_failure
    callback(os.unlink, str(retry_file), (PermissionError, permission_failure, None))
with patch.object(dc_selftest.shutil, 'rmtree', side_effect=simulate_readonly_cleanup):
    try:
        dc_selftest.materialise(retry_dest)
        retry_ok = not retry_file.exists()
    except PermissionError:
        retry_ok = False
check('owned regular file PermissionError is chmodded and retried', retry_ok)

cleanup_dest = Path(tempfile.mkdtemp(prefix='dc-cleanup-error-'))
(cleanup_dest / 'MANIFEST.txt').write_text('# drainclamp-build fixtures; schema=1\n', encoding='utf-8')
cleanup_file = cleanup_dest / 'keep'
cleanup_file.write_bytes(b'keep')
cleanup_failure = OSError('unrelated cleanup failure')
def simulate_cleanup_failure(path, **kwargs):
    callback = kwargs.get('onerror')
    if callback is None:
        raise cleanup_failure
    callback(os.unlink, str(cleanup_file), (OSError, cleanup_failure, None))
with patch.object(dc_selftest.shutil, 'rmtree', side_effect=simulate_cleanup_failure), \
        patch.object(dc_selftest.os, 'chmod') as chmod:
    try:
        dc_selftest.materialise(cleanup_dest)
        propagated = False
    except OSError as exc:
        propagated = exc is cleanup_failure
check('non-permission cleanup errors propagate without chmod',
      propagated and not chmod.called and cleanup_file.read_bytes() == b'keep')

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
