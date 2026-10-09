"""dc_verify.py checks: allowlist, approval handoff, verdict rules, exit codes.

The commands here run a stub `pytest.py` through `python -m pytest`, which the
allowlist matches by shape. That keeps the suite deterministic on a machine
where pytest, go and cargo may or may not be installed, while still exercising
the real sandbox rather than a mocked one.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import _dcio  # noqa: E402
import dc_selftest  # noqa: E402
import dc_verify  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def verify(root, *args):
    """Run dc_verify against a fixture, outside the gate pipeline.

    Every fixture here exercises the verification engine -- allowlist matching,
    verdict rules, exit codes, timeouts -- on a repository that has no roadmap
    and no decisions, because none of that is what these checks are about. The
    Gate 2 precondition would otherwise answer first and every assertion below
    would pass or fail for a reason it never meant to test. Gate semantics have
    their own suite: t_gatestate.py.
    """
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_verify.py"), "--root", str(root),
         "--allow-no-state", *args],
        capture_output=True, text=True,
    )


def set_verify(root, entries):
    path = Path(root) / ".agent" / "entries.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_state.py"), "--root", str(root),
         "--set", "VERIFY", "--file", str(path)],
        capture_output=True, text=True,
    )


def stub_pytest(root, body):
    (Path(root) / "pytest.py").write_text(body, encoding="utf-8")


fixtures = Path(os.environ.get("DC_FIXTURES") or tempfile.mkdtemp(prefix="dcfix-"))
if not (fixtures / "MANIFEST.txt").exists():
    dc_selftest.materialise(fixtures)
repo = fixtures / "repo_calc"

# --- classification is code, not configuration -----------------------------
check("pytest recognised directly", dc_verify.classify(["pytest", "tests/"])[0] == "pytest")
check("python -m pytest recognised",
      dc_verify.classify([sys.executable, "-m", "pytest"])[0] == "pytest")
check("gofmt -l is output-sensitive",
      dc_verify.classify(["gofmt", "-l", "."]) == ("gofmt -l", dc_verify.NON_EMPTY_STDOUT))
check("ruff --fix is not a check", dc_verify.classify(["ruff", "check", "--fix"]) is None)
check("eslint --fix is not a check", dc_verify.classify(["eslint", "--fix", "src"]) is None)
check("black without --check is not a check", dc_verify.classify(["black", "src"]) is None)
check("an arbitrary script is not allowlisted",
      dc_verify.classify(["python", "-c", "print(1)"]) is None)

gofmt_result = _dcio.RunResult(["gofmt", "-l", "."], ".", 0, "main.go\n", "", 0.1)
check("gofmt exit 0 with a listing is a FAIL",
      dc_verify.verdict_for(gofmt_result, dc_verify.NON_EMPTY_STDOUT) == dc_verify.FAIL)
check("gofmt exit 0 with no listing is a PASS",
      dc_verify.verdict_for(_dcio.RunResult(["gofmt", "-l", "."], ".", 0, "", "", 0.1),
                            dc_verify.NON_EMPTY_STDOUT) == dc_verify.PASS)
check("exit-code tools are still read by exit code",
      dc_verify.verdict_for(_dcio.RunResult(["pytest"], ".", 1, "boom", "", 0.1),
                            dc_verify.EXIT_CODE) == dc_verify.FAIL)

# --- green and red through the real sandbox --------------------------------
stub_pytest(repo, 'print("2 passed")\nraise SystemExit(0)\n')
set_verify(repo, [{"id": "unit", "argv": [sys.executable, "-m", "pytest"],
                   "cwd": ".", "tier": "milestone"}])
out = verify(repo, "--tier", "milestone")
check("an allowlisted command runs and passes",
      out.returncode == 0 and "RESULT: PASS" in out.stdout, out.stdout.splitlines()[-1])
check("verify output is bounded", len(out.stdout.splitlines()) <= 10,
      f"{len(out.stdout.splitlines())} lines")
check("full log written", (repo / ".agent/verify.log").is_file())

stub_pytest(repo, 'print("E   assert 3 == 4")\nraise SystemExit(1)\n')
out = verify(repo, "--tier", "milestone")
check("a failing check exits 2", out.returncode == 2, f"rc={out.returncode}")
check("the first failing assertion is kept", "assert 3 == 4" in out.stdout,
      out.stdout.splitlines()[1] if len(out.stdout.splitlines()) > 1 else "")

# --- timeout ---------------------------------------------------------------
stub_pytest(repo, "import time\ntime.sleep(60)\n")
set_verify(repo, [{"id": "hang", "argv": [sys.executable, "-m", "pytest"],
                   "cwd": ".", "tier": "milestone", "timeout_s": 2}])
out = verify(repo, "--tier", "milestone")
check("a hanging check exits 5", out.returncode == 5, f"rc={out.returncode}")
check("the timeout says the tree was killed", "process tree killed" in out.stdout,
      out.stdout.splitlines()[1] if len(out.stdout.splitlines()) > 1 else "")
stub_pytest(repo, 'print("2 passed")\nraise SystemExit(0)\n')

# --- the repository is not an authority ------------------------------------
set_verify(repo, [{"id": "shellish", "argv": ["pytest", "tests/ | tee out.txt"],
                   "cwd": ".", "tier": "milestone"}])
out = verify(repo, "--tier", "milestone")
check("a piped entry is UNSAFE-COMMAND (exit 4)",
      out.returncode == 4 and dc_verify.UNSAFE in out.stdout, f"rc={out.returncode}")
check("nothing was executed for the unsafe entry",
      not (repo / "out.txt").exists())

# host-approved records are tied to a tree, so this part runs in a git copy
rec = fixtures / "repo_rec"
if not rec.exists():
    shutil.copytree(repo, rec)
    for cmd in (["init", "-q"], ["add", "-A"],
                ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
        subprocess.run(["git", *cmd], cwd=rec, capture_output=True)
set_verify(rec, [{"id": "custom", "argv": ["node", "scripts/custom-check.js"],
                  "cwd": ".", "tier": "milestone"}])
out = verify(rec, "--tier", "milestone")
digest_line = next((ln for ln in out.stdout.splitlines() if "digest:" in ln), "")
digest = digest_line.split("digest:")[-1].strip()
check("a non-allowlisted entry asks for approval (exit 4)",
      out.returncode == 4 and dc_verify.APPROVAL in out.stdout, f"rc={out.returncode}")
check("the approval record carries argv, cwd and a digest",
      "argv:" in out.stdout and "cwd:" in out.stdout and digest.startswith("sha256:"),
      digest)
check("an approval-only tier is never green",
      "NO-CHECKS-RUN" in out.stdout and out.returncode != 0,
      next((ln for ln in out.stdout.splitlines() if ln.startswith("RESULT")), ""))

out = verify(rec, "--record", "custom", "--digest", "sha256:deadbeefdead",
             "--status", "pass")
check("a mismatched digest is refused", out.returncode == 4 and "digest mismatch" in out.stderr,
      out.stderr.strip().splitlines()[0] if out.stderr else "")
check("nothing was recorded for the mismatch",
      not (rec / ".agent/verify-records.json").exists()
      or "custom" not in json.loads((rec / ".agent/verify-records.json")
                                    .read_text(encoding="utf-8")))

out = verify(rec, "--record", "custom", "--digest", digest, "--status", "pass")
check("the issued digest is accepted", out.returncode == 0 and "RECORDED" in out.stdout,
      out.stdout.strip())
out = verify(rec, "--tier", "milestone")
check("a recorded pass satisfies that entry",
      out.returncode == 0 and "host-approved record" in out.stdout,
      out.stdout.splitlines()[1] if len(out.stdout.splitlines()) > 1 else "")

(rec / "calc_extra.py").write_text("X = 1\n", encoding="utf-8")
out = verify(rec, "--tier", "milestone")
check("a code change after recording makes the record stale",
      out.returncode == 4 and "stale:" in out.stdout and "host-approved" not in out.stdout,
      f"rc={out.returncode}")
out = verify(rec, "--record", "custom", "--digest", digest, "--status", "pass")
out = verify(rec, "--tier", "milestone")
check("recording again for the new tree satisfies it",
      out.returncode == 0 and "host-approved record" in out.stdout, f"rc={out.returncode}")
(rec / ".agent" / "scratch.txt").write_text("state churn", encoding="utf-8")
out = verify(rec, "--tier", "milestone")
check("changes under .agent/ do not stale a record", out.returncode == 0, f"rc={out.returncode}")
records = json.loads((rec / ".agent/verify-records.json").read_text(encoding="utf-8"))
records["custom"].pop("tree")
(rec / ".agent/verify-records.json").write_text(json.dumps(records), encoding="utf-8")
out = verify(rec, "--tier", "milestone")
check("a record without a tree (pre-1.3) is stale", out.returncode == 4, f"rc={out.returncode}")
check("a non-git root yields no fingerprint", dc_verify.tree_fingerprint(Path(tempfile.mkdtemp())) is None)

set_verify(rec, [{"id": "custom", "argv": ["node", "scripts/custom-check.js", "--strict"],
                   "cwd": ".", "tier": "milestone"}])
out = verify(rec, "--tier", "milestone")
check("editing the entry invalidates its record",
      out.returncode == 4 and dc_verify.APPROVAL in out.stdout, f"rc={out.returncode}")

# --- cwd containment -------------------------------------------------------
(repo / "packages" / "api").mkdir(parents=True, exist_ok=True)
stub_pytest(repo / "packages" / "api", 'print("ok")\nraise SystemExit(0)\n')
set_verify(repo, [{"id": "sub", "argv": [sys.executable, "-m", "pytest"],
                   "cwd": "packages/api", "tier": "milestone"}])
out = verify(repo, "--tier", "milestone")
check("a monorepo subpackage cwd is accepted",
      out.returncode == 0 and "RESULT: PASS" in out.stdout,
      out.stdout.splitlines()[-1])

set_verify(repo, [{"id": "escape", "argv": [sys.executable, "-m", "pytest"],
                   "cwd": "../", "tier": "milestone"}])
out = verify(repo, "--tier", "milestone")
check("a cwd outside the repository is refused", out.returncode == 4, f"rc={out.returncode}")

escape_link = repo / "outside_link"
linked = False
if not escape_link.exists():
    try:
        os.symlink(str(repo.parent), str(escape_link), target_is_directory=True)
        linked = True
    except (OSError, NotImplementedError):
        linked = False
if linked:
    set_verify(repo, [{"id": "linked", "argv": [sys.executable, "-m", "pytest"],
                       "cwd": "outside_link", "tier": "milestone"}])
    out = verify(repo, "--tier", "milestone")
    check("a symlinked escape is resolved and refused", out.returncode == 4,
          f"rc={out.returncode}")
    escape_link.unlink()
else:
    print("SKIP  symlinked cwd escape (no symlink privilege on this host)")

# --- discovery: configured, not merely present -----------------------------
bare = Path(tempfile.mkdtemp(prefix="dcverify-bare-")) / "proj"
(bare / "src").mkdir(parents=True)
subprocess.run(["git", "init", "-q"], cwd=bare, capture_output=True)
(bare / "src" / "thing.py").write_text("def thing():\n    return 1\n", encoding="utf-8")
out = verify(bare, "--tier", "milestone")
check("a repo with no configured runner reports NO-CHECKS-RUN (exit 6)",
      out.returncode == 6 and "NO-CHECKS-RUN" in out.stdout
      and dc_verify.UNDISCOVERED in out.stdout, f"rc={out.returncode}")

(bare / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
out = verify(bare, "--tier", "milestone")
check("requirements.txt without pytest leaves it UNDISCOVERED",
      out.returncode == 6 and dc_verify.UNDISCOVERED in out.stdout, f"rc={out.returncode}")

(bare / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
discovered = json.loads(verify(bare, "--discover").stdout)
check("a declared pytest dep makes pytest a configured runner",
      any(entry["id"] == "pytest" for entry in discovered),
      ", ".join(entry["id"] for entry in discovered))

missing_tool = None
for name in ("gofmt", "cargo"):
    if shutil.which(name) is None:
        missing_tool = name
        break
if missing_tool:
    argv = ["gofmt", "-l", "."] if missing_tool == "gofmt" else ["cargo", "test"]
    set_verify(bare, [{"id": "absent", "argv": argv, "cwd": ".", "tier": "milestone"}])
    out = verify(bare, "--tier", "milestone")
    check("a configured but absent runner is MISSING-REQUIRED (exit 3)",
          out.returncode == 3 and dc_verify.MISSING_REQUIRED in out.stdout,
          f"rc={out.returncode}")
else:
    print("SKIP  MISSING-REQUIRED (both gofmt and cargo are installed here)")

# --- schema drift ----------------------------------------------------------
forged = repo / ".agent" / "forged.json"
forged.write_text(json.dumps([
    {"id": "trusted", "argv": ["node", "danger.js"], "cwd": ".", "tier": "milestone",
     "source": "user-approved"}
]), encoding="utf-8")
out = subprocess.run(
    [sys.executable, "-B", str(SCRIPTS / "dc_state.py"), "--root", str(repo),
     "--set", "VERIFY", "--file", str(forged)], capture_output=True, text=True)
check("a forged trust field cannot be stored at all",
      out.returncode != 0 and "unknown key" in out.stderr,
      out.stderr.strip().splitlines()[0] if out.stderr else "")

state_file = repo / ".agent" / _dcio.STATE_NAME
raw = state_file.read_text(encoding="utf-8")
hand_edited = raw.replace(
    '"tier": "milestone"', '"tier": "milestone", "source": "user-approved"', 1)
state_file.write_text(hand_edited, encoding="utf-8")
out = verify(repo, "--tier", "milestone")
check("a hand-edited trust field is rejected, not believed",
      out.returncode != 0 and "unknown key" in out.stderr,
      out.stderr.strip().splitlines()[0] if out.stderr else "")
state_file.write_text(raw, encoding="utf-8")

print()
# Git filenames are data, including Unicode, whitespace and literal arrows.
from unittest.mock import patch
unicode_repo = Path(tempfile.mkdtemp(prefix='dc-utf8-'))
def git_utf8(*args):
    return subprocess.run(['git', *args], cwd=unicode_repo, check=True, capture_output=True)
git_utf8('init', '-q')
git_utf8('config', 'user.email', 't@example.invalid')
git_utf8('config', 'user.name', 'test')
for name in ('módulo.py', 'staged_é.py', 'old_é.py', ' spaces .py'):
    (unicode_repo / name).write_text('x = 1\n', encoding='utf-8')
git_utf8('add', '.')
git_utf8('commit', '-qm', 'base')
(unicode_repo / 'módulo.py').write_text('x = 2\n', encoding='utf-8')
(unicode_repo / 'staged_é.py').write_text('x = 3\n', encoding='utf-8')
git_utf8('add', 'staged_é.py')
git_utf8('mv', 'old_é.py', 'new_é.py')
(unicode_repo / 'untracked_漢.py').write_text('x = 4\n', encoding='utf-8')
(unicode_repo / ' spaces .py').write_text('x = 6\n', encoding='utf-8')
changed_utf8, _ = dc_verify.changed_files(unicode_repo, None)
check('Git status preserves all filenames and both rename fields', set(changed_utf8) == {
    'módulo.py', 'staged_é.py', 'old_é.py', 'new_é.py', 'untracked_漢.py',
    ' spaces .py'}, repr(changed_utf8))
base_paths, _ = dc_verify.changed_files(unicode_repo, 'HEAD^{tree}')
check('Git base fallback preserves Unicode and spaces',
      'módulo.py' in base_paths and ' spaces .py' in base_paths)
# Windows forbids > in a real filename; exercise literal arrows through Git's
# exact NUL protocol on every host without creating an invalid local path.
arrow_status = subprocess.CompletedProcess([], 0,
    ' M literal -> arrow.py\0R  renamed -> destination.py\0old -> source.py\0', '')
with patch.object(dc_verify.subprocess, 'run', return_value=arrow_status):
    arrow_paths, _ = dc_verify.changed_files(unicode_repo, None)
check('NUL status preserves literal arrows and both rename fields', set(arrow_paths) == {
    'literal -> arrow.py', 'renamed -> destination.py', 'old -> source.py'}, repr(arrow_paths))
arrow_names = subprocess.CompletedProcess([], 0, 'literal -> arrow.py\0 spaces .py\0', '')
with patch.object(dc_verify.subprocess, 'run', return_value=arrow_names):
    arrow_base_paths, _ = dc_verify.changed_files(unicode_repo, 'HEAD')
check('NUL base names preserve literal arrows and spaces',
      arrow_base_paths == [' spaces .py', 'literal -> arrow.py'], repr(arrow_base_paths))
agent_utf8 = unicode_repo / '.agent'
agent_utf8.mkdir()
approved_entry = {'id': 'host', 'argv': ['custom-check'], 'cwd': '.', 'tier': 'fast'}
first_tree = dc_verify.tree_fingerprint(unicode_repo)
dc_verify.save_record(agent_utf8, 'host', dc_verify.digest_for(['custom-check'], '.'),
                      'pass', '', first_tree)
(unicode_repo / 'módulo.py').write_text('x = 7\n', encoding='utf-8')
second_tree = dc_verify.tree_fingerprint(unicode_repo)
check('Unicode edit changes fingerprint and invalidates host record', first_tree != second_tree
      and dc_verify.run_entry(approved_entry, unicode_repo, agent_utf8)['status'] == dc_verify.APPROVAL)
(unicode_repo / 'módulo.py').write_text('x = 8\n', encoding='utf-8')
check('second Unicode edit changes fingerprint again',
      second_tree != dc_verify.tree_fingerprint(unicode_repo))
(unicode_repo / 'ruff.toml').write_text('', encoding='utf-8')
(unicode_repo / 'eslint.config.js').write_text('', encoding='utf-8')
(unicode_repo / 'unchanged.py').write_text('bad style', encoding='utf-8')
(unicode_repo / 'unchanged.js').write_text('bad style', encoding='utf-8')
lint_discovery = dc_verify.discover(unicode_repo, ['módulo.py', 'changed.js'])
fast_lint, _ = dc_verify.select_entries([], lint_discovery, 'fast')
check('fast lint remains diff scoped',
      next(e for e in fast_lint if e['id'] == 'ruff')['argv'] == ['ruff', 'check', 'módulo.py']
      and next(e for e in fast_lint if e['id'] == 'eslint')['argv'] == ['eslint', 'changed.js'])
final_lint, _ = dc_verify.select_entries([
    {'id': 'ruff', 'argv': ['ruff', 'check', 'módulo.py'], 'tier': 'fast'},
    {'id': 'eslint', 'argv': ['eslint', 'changed.js'], 'tier': 'fast'}], lint_discovery, 'final')
check('final lint includes full targets beside stored scoped ids',
      any(e['id'] == 'ruff-final' and e['argv'] == ['ruff', 'check', '.'] for e in final_lint)
      and any(e['id'] == 'eslint-final' and e['argv'] == ['eslint', '.'] for e in final_lint))
# Mock execution: local payload bytes are never executable in these tests.
local_runner = unicode_repo / ('pytest.exe' if os.name == 'nt' else 'pytest')
local_runner.write_text('inert test data', encoding='utf-8')
local_runner.chmod(0o755)
for runner in (str(local_runner), './' + local_runner.name):
    with patch.object(dc_verify._dcio, 'run_sandboxed') as execution:
        local_row = dc_verify.run_entry({'id': 'local', 'argv': [runner],
                                        'cwd': '.', 'tier': 'fast'}, unicode_repo, agent_utf8)
    check('repository executable requires approval ' + runner,
          local_row['status'] == dc_verify.APPROVAL and not execution.called)
with patch.dict(os.environ, {'PATH': '.'}):
    with patch.object(dc_verify._dcio, 'run_sandboxed') as execution:
        local_row = dc_verify.run_entry({'id': 'path-local', 'argv': ['pytest'],
                                        'cwd': '.', 'tier': 'fast'}, unicode_repo, agent_utf8)
    check('relative PATH resolves in execution cwd and refuses local runner',
          local_row['status'] == dc_verify.APPROVAL and not execution.called)
external_link = Path(tempfile.mkdtemp(prefix='dc-exe-link-')) / local_runner.name
try:
    os.symlink(local_runner, external_link)
except (OSError, NotImplementedError):
    check('external executable symlink into repository refuses', True, 'SKIPPED: cannot create link')
else:
    with patch.object(dc_verify._dcio, 'run_sandboxed') as execution:
        link_row = dc_verify.run_entry({'id': 'linked', 'argv': [str(external_link)],
                                       'cwd': '.', 'tier': 'fast'}, unicode_repo, agent_utf8)
    check('external executable symlink into repository refuses',
          link_row['status'] == dc_verify.APPROVAL and not execution.called)
external_argv = [sys.executable, '-m', 'pytest']
with patch.object(dc_verify._dcio, 'run_sandboxed',
                  return_value=_dcio.RunResult(external_argv, '.', 0, '', '', 0.1)) as execution:
    external_row = dc_verify.run_entry({'id': 'external', 'argv': external_argv,
                                       'cwd': '.', 'tier': 'fast'}, unicode_repo, agent_utf8)
check('external interpreter executes pinned path but preserves original argv',
      external_row['status'] == dc_verify.PASS and external_row['argv'] == external_argv
      and execution.call_args.args[0][0] == str(Path(sys.executable).resolve()))
for flag in ('-w', '-w=true', '-unknown', '-l=true'):
    check('gofmt unsafe flag refuses ' + flag,
          dc_verify.classify(['gofmt', '-l', flag, '.']) is None)
formatter = Path(tempfile.mkdtemp(prefix='dc-format-')) / ('gofmt.exe' if os.name == 'nt' else 'gofmt')
formatter.write_text('inert', encoding='utf-8')
formatter.chmod(0o755)
outside_go = formatter.parent / 'outside.go'
outside_go.write_text('package main', encoding='utf-8')
for target in (str(outside_go), '../outside.go'):
    with patch.object(dc_verify._dcio, 'run_sandboxed') as execution:
        format_row = dc_verify.run_entry({'id': 'format', 'argv': [str(formatter), '-l', target],
                                         'cwd': '.', 'tier': 'fast'}, unicode_repo, agent_utf8)
    check('gofmt escaping path refuses ' + target,
          format_row['status'] in (dc_verify.APPROVAL, dc_verify.UNSAFE) and not execution.called)
escape_go = unicode_repo / 'escape.go'
try:
    os.symlink(outside_go, escape_go)
except (OSError, NotImplementedError):
    check('gofmt symlink escape refuses', True, 'SKIPPED: cannot create link')
else:
    with patch.object(dc_verify._dcio, 'run_sandboxed') as execution:
        format_row = dc_verify.run_entry({'id': 'format', 'argv': [str(formatter), '-l', 'escape.go'],
                                         'cwd': '.', 'tier': 'fast'}, unicode_repo, agent_utf8)
    check('gofmt symlink escape refuses', format_row['status'] in (dc_verify.APPROVAL, dc_verify.UNSAFE)
          and not execution.called)
with patch.object(dc_verify._dcio, 'run_sandboxed',
                  return_value=_dcio.RunResult([str(formatter), '-l', '.'], '.', 0, 'bad.go\n', '', 0.1)):
    valid_format = dc_verify.run_entry({'id': 'format', 'argv': [str(formatter), '-l', '.'],
                                       'cwd': '.', 'tier': 'fast'}, unicode_repo, agent_utf8)
check('valid gofmt remains output sensitive', valid_format['status'] == dc_verify.FAIL)

with patch.dict(os.environ, {'PATH': str(formatter.parent)}):
    with patch.object(dc_verify._dcio, 'run_sandboxed',
                      return_value=_dcio.RunResult(['gofmt', '-l', '.'], '.', 0, '', '', 0.1)) as execution:
        pinned_row = dc_verify.run_entry({'id': 'pinned', 'argv': ['gofmt', '-l', '.'],
                                         'cwd': '.', 'tier': 'fast'}, unicode_repo, agent_utf8)
    check('PATH runner is pinned externally in executed argv', pinned_row['status'] == dc_verify.PASS
          and execution.call_args.args[0][0] == str(formatter.resolve())
          and pinned_row['argv'] == ['gofmt', '-l', '.'])
with patch.dict(os.environ, {'PATH': ''}):
    with patch.object(dc_verify._dcio, 'run_sandboxed') as execution:
        missing_row = dc_verify.run_entry({'id': 'missing', 'argv': ['cargo', 'test'],
                                          'cwd': '.', 'tier': 'fast'}, unicode_repo, agent_utf8)
    check('missing configured runner retains missing verdict',
          missing_row['status'] == dc_verify.MISSING_REQUIRED and not execution.called)
for write_flag in ('-w', '-w=true'):
    with patch.object(dc_verify._dcio, 'run_sandboxed') as execution:
        write_row = dc_verify.run_entry({'id': 'write', 'argv': [str(formatter), '-l', write_flag, '.'],
                                        'cwd': '.', 'tier': 'fast'}, unicode_repo, agent_utf8)
    check('gofmt write never executes ' + write_flag,
          write_row['status'] == dc_verify.APPROVAL and not execution.called)

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
