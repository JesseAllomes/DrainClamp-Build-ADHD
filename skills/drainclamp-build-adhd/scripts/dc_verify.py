#!/usr/bin/env python3
"""Tiered verification for DrainClamp & Build.

Tiers: `fast` during the Red-Green loop (diff-scoped tests plus check-only
refiners), `milestone` at milestone close, `final` for the full suite and lint.

Three rules do most of the work here:

**The repository is not an authority.** `DC:VERIFY` is command text stored in a
repository file, so schema v1 stores no trust field and none is believed. Only
commands matching the runner/refiner allowlist execute. Anything else is handed
back to the agent as `APPROVAL-REQUIRED` with a digest, run through the host's
own approval path, and recorded against that digest.

**Configured, not merely present.** A `requirements.txt` mentioning nothing
about pytest does not make pytest a required runner; it makes it
`UNDISCOVERED`. Only a declared configuration turns absence into
`MISSING-REQUIRED`.

**Exit 0 is not universally a pass.** `gofmt -l` exits 0 while listing the files
it would rewrite, so each allowlist entry declares its own verdict rule.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import _dcio
import dc_registry
import dc_state
from _dcio import DcError

VERIFY_LOG = "verify.log"
RECORDS_NAME = "verify-records.json"
# Records keyed `tier:<tier>` hold the last tier outcome and the tree it ran
# against, so Gate 6 can refuse to review code Gate 3 has not passed.
TIER_RECORD_PREFIX = "tier:"
MAX_OUTPUT_LINES = 10
MAX_ROWS = 6

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
MISSING_REQUIRED = "MISSING-REQUIRED"
UNDISCOVERED = "UNDISCOVERED"
UNSAFE = "UNSAFE-COMMAND"
APPROVAL = "APPROVAL-REQUIRED"
TIMEOUT = "TIMEOUT"

EXIT_CODE = "exit-code"
NON_EMPTY_STDOUT = "non-empty-stdout"

TIER_ORDER = {"fast": 0, "milestone": 1, "final": 2}

PY_EXES = {"python", "python3", "py"}
NODE_PMS = {"npm", "yarn", "pnpm"}


def exe_name(argv: list[str]) -> str:
    """Basename of argv[0], lowercased, without a Windows launcher suffix."""
    raw = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
    return raw


# --------------------------------------------------------------------------
# Allowlist
#
# Each entry answers two questions: may this command run without host approval,
# and how is its verdict read? The second question is not decoration —
# `gofmt -l` exits 0 while printing the files it would rewrite, so reading its
# exit code alone reports a clean tree that is not clean.
# --------------------------------------------------------------------------


def _is_pytest(argv: list[str]) -> bool:
    exe = exe_name(argv)
    return exe == "pytest" or (exe in PY_EXES and argv[1:3] == ["-m", "pytest"])


def _is_go_test(argv: list[str]) -> bool:
    return exe_name(argv) == "go" and len(argv) > 1 and argv[1] == "test"


def _is_cargo_test(argv: list[str]) -> bool:
    return exe_name(argv) == "cargo" and len(argv) > 1 and argv[1] == "test"


def _is_pm_test(argv: list[str]) -> bool:
    if exe_name(argv) not in NODE_PMS:
        return False
    tail = [a for a in argv[1:] if not a.startswith("-")]
    return tail[:1] == ["test"] or tail[:2] == ["run", "test"]


def _is_ruff_check(argv: list[str]) -> bool:
    return (exe_name(argv) == "ruff" and len(argv) > 1 and argv[1] == "check"
            and not {"--fix", "--unsafe-fixes"} & set(argv))


def _is_black_check(argv: list[str]) -> bool:
    return exe_name(argv) == "black" and "--check" in argv


def _is_eslint(argv: list[str]) -> bool:
    exe = exe_name(argv)
    if exe == "npx":
        exe = exe_name(argv[1:]) if len(argv) > 1 else ""
    return exe == "eslint" and "--fix" not in argv


def _is_tsc(argv: list[str]) -> bool:
    exe = exe_name(argv)
    if exe == "npx":
        exe = exe_name(argv[1:]) if len(argv) > 1 else ""
    return exe == "tsc" and "--noEmit" in argv


def _is_gofmt(argv: list[str]) -> bool:
    return exe_name(argv) == "gofmt" and "-l" in argv


ALLOWLIST = (
    ("pytest", _is_pytest, EXIT_CODE),
    ("go test", _is_go_test, EXIT_CODE),
    ("cargo test", _is_cargo_test, EXIT_CODE),
    ("package-manager test", _is_pm_test, EXIT_CODE),
    ("ruff check", _is_ruff_check, EXIT_CODE),
    ("black --check", _is_black_check, EXIT_CODE),
    ("eslint", _is_eslint, EXIT_CODE),
    ("tsc --noEmit", _is_tsc, EXIT_CODE),
    # Output-sensitive: exit 0 with output means "these files are unformatted".
    ("gofmt -l", _is_gofmt, NON_EMPTY_STDOUT),
)


def classify(argv: list[str]) -> tuple[str, str] | None:
    """(allowlist name, verdict rule), or None when host approval is needed."""
    for name, matcher, rule in ALLOWLIST:
        try:
            if matcher(argv):
                return name, rule
        except (IndexError, TypeError):
            continue
    return None


def digest_for(argv: list[str], cwd: str) -> str:
    canonical = json.dumps({"argv": argv, "cwd": cwd}, sort_keys=True,
                           separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Changed-file set
# --------------------------------------------------------------------------


def git_lines(root: Path, *args: str) -> list[str] | None:
    try:
        done = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.splitlines()


def changed_files(root: Path, base: str | None) -> tuple[list[str], str]:
    """(repo-relative paths, how they were derived).

    Default is the working state: staged, unstaged, renamed and untracked,
    respecting ignore rules. `--base` switches to branch-relative. When neither
    is available the caller falls back to configured checks rather than guessing
    an empty change set — an empty set would silently verify nothing.
    """
    if base:
        lines = git_lines(root, "diff", "--name-only", f"{base}...HEAD")
        if lines is None:
            lines = git_lines(root, "diff", "--name-only", base)
        if lines is None:
            return [], "unavailable"
        return sorted({ln.strip() for ln in lines if ln.strip()}), f"base {base}"

    lines = git_lines(root, "status", "--porcelain", "-uall")
    if lines is None:
        return [], "unavailable"
    found: set[str] = set()
    for line in lines:
        if len(line) < 4:
            continue
        rest = line[3:]
        for part in rest.split(" -> "):  # renames contribute both paths
            cleaned = part.strip().strip('"')
            if cleaned:
                found.add(cleaned)
    return sorted(found), "working tree"


def tree_fingerprint(root: Path) -> str | None:
    """HEAD plus the content of every changed file, outside `.agent/`.

    A host-approved record vouches for the tree it ran against, not only the
    command. Without this a record kept passing a tier after the code it
    tested had changed. None when git cannot say, so no record can match.
    """
    head = git_lines(root, "rev-parse", "HEAD")
    changed, how = changed_files(root, None)
    if how == "unavailable":
        return None
    parts = {"head": head[0] if head else "", "files": {}}
    for rel in changed:
        if rel == ".agent" or rel.startswith(".agent/"):
            continue
        try:
            parts["files"][rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        except OSError:
            parts["files"][rel] = "gone"
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Discovery — configured, not merely present
# --------------------------------------------------------------------------


def read_json(path: Path) -> dict:
    try:
        return json.loads(_dcio.read_text(path) or "{}")
    except json.JSONDecodeError:
        return {}


def pytest_configured(root: Path) -> bool:
    """Configured, not merely importable somewhere on the machine.

    A `requirements.txt` that never mentions pytest leaves it `UNDISCOVERED`
    rather than `MISSING-REQUIRED` — absence is only a failure when the
    repository said it needed the tool.
    """
    if (root / "pytest.ini").is_file():
        return True
    if "[pytest]" in (_dcio.read_text(root / "tox.ini") or ""):
        return True
    if "[tool:pytest]" in (_dcio.read_text(root / "setup.cfg") or ""):
        return True
    pyproject = _dcio.read_text(root / "pyproject.toml") or ""
    if "[tool.pytest.ini_options]" in pyproject or "pytest" in pyproject:
        return True
    requirements = _dcio.read_text(root / "requirements.txt") or ""
    return any(line.strip().lower().startswith("pytest")
               for line in requirements.splitlines())


def node_test_configured(root: Path) -> bool:
    data = read_json(root / "package.json")
    if "test" in data.get("scripts", {}):
        return True
    declared = set(data.get("dependencies", {})) | set(data.get("devDependencies", {}))
    return bool(declared & {"jest", "vitest", "mocha"})


def discover(root: Path, changed: list[str]) -> list[dict]:
    """Configured runners and check-only refiners, as DC:VERIFY-shaped entries."""
    entries: list[dict] = []
    changed_py = [p for p in changed if p.endswith((".py", ".pyi"))]
    changed_js = [p for p in changed if p.endswith((".js", ".jsx", ".ts", ".tsx"))]

    if pytest_configured(root):
        entries.append({"id": "pytest", "argv": ["pytest"], "cwd": ".",
                        "tier": "milestone"})
        scoped = [p for p in changed_py if "test" in Path(p).name.lower()]
        if scoped:
            entries.append({"id": "pytest-changed", "argv": ["pytest", *scoped],
                            "cwd": ".", "tier": "fast"})
    if node_test_configured(root):
        entries.append({"id": "npm-test", "argv": ["npm", "test"], "cwd": ".",
                        "tier": "milestone"})
    if (root / "go.mod").is_file():
        entries.append({"id": "go-test", "argv": ["go", "test", "./..."], "cwd": ".",
                        "tier": "milestone"})
        entries.append({"id": "gofmt", "argv": ["gofmt", "-l", "."], "cwd": ".",
                        "tier": "fast"})
    if (root / "Cargo.toml").is_file():
        entries.append({"id": "cargo-test", "argv": ["cargo", "test"], "cwd": ".",
                        "tier": "milestone"})

    ruff_configured = any((root / name).is_file() for name in ("ruff.toml", ".ruff.toml")) \
        or "[tool.ruff" in (_dcio.read_text(root / "pyproject.toml") or "")
    if ruff_configured:
        entries.append({"id": "ruff", "argv": ["ruff", "check", *(changed_py or ["."])],
                        "cwd": ".", "tier": "fast"})
    if any(root.glob(".eslintrc*")) or any(root.glob("eslint.config.*")):
        entries.append({"id": "eslint", "argv": ["eslint", *(changed_js or ["."])],
                        "cwd": ".", "tier": "fast"})
    if any(root.glob("tsconfig*.json")):
        entries.append({"id": "tsc", "argv": ["tsc", "--noEmit"], "cwd": ".",
                        "tier": "milestone"})
    return entries


# --------------------------------------------------------------------------
# Records — the approval handoff
# --------------------------------------------------------------------------


def records_path(agent: Path) -> Path:
    return agent / RECORDS_NAME


def load_records(agent: Path) -> dict:
    try:
        return json.loads(_dcio.read_text(records_path(agent)) or "{}")
    except json.JSONDecodeError:
        return {}


def save_record(agent: Path, entry_id: str, digest: str, status: str,
                log: str | None, tree: str | None = None) -> None:
    with _dcio.FileLock(agent):
        records = load_records(agent)
        records[entry_id] = {
            "digest": digest,
            "tree": tree,
            "status": status,
            "when": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "log": log or "",
        }
        _dcio.atomic_write(records_path(agent), json.dumps(records, indent=2) + "\n")


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def verdict_for(result: _dcio.RunResult, rule: str) -> str:
    if result.timed_out:
        return TIMEOUT
    if result.missing:
        return MISSING_REQUIRED
    if rule == NON_EMPTY_STDOUT:
        # Output-sensitive: the listing *is* the failure, whatever the exit code.
        if result.stdout.strip():
            return FAIL
        return PASS if result.returncode == 0 else FAIL
    return PASS if result.returncode == 0 else FAIL


def run_entry(entry: dict, root: Path, agent: Path) -> dict:
    """Execute or refuse one entry. Never raises for an entry's own failure."""
    argv, cwd = entry["argv"], entry.get("cwd", ".")
    row = {"id": entry["id"], "argv": argv, "cwd": cwd, "tier": entry["tier"],
           "detail": "", "duration": 0.0}

    try:
        _dcio.check_repo_argv(argv)
    except DcError as exc:
        row.update(status=UNSAFE, detail=_dcio.collapse(str(exc), 160))
        return row

    known = classify(argv)
    if known is None:
        record = load_records(agent).get(entry["id"])
        digest = digest_for(argv, cwd)
        if record and record.get("digest") == digest:
            tree = tree_fingerprint(root)
            if tree is not None and record.get("tree") == tree:
                row.update(status=PASS if record.get("status") == "pass" else FAIL,
                           detail=f"host-approved record from {record.get('when', '?')}")
                return row
            row["stale"] = f"record from {record.get('when', '?')} is for a different tree"
        row.update(status=APPROVAL, detail=digest)
        return row

    name, rule = known
    try:
        result = _dcio.run_sandboxed(
            argv, root, cwd, timeout=int(entry.get("timeout_s") or
                                         _dcio.DEFAULT_TIMEOUT_SECONDS),
        )
    except DcError as exc:
        row.update(status=UNSAFE, detail=_dcio.collapse(str(exc), 160))
        return row

    status = verdict_for(result, rule)
    row["duration"] = result.duration
    row["status"] = status
    row["runner"] = name
    row["output"] = result.output
    if status == FAIL:
        row["detail"] = _dcio.first_failure_line(result.output) or f"{name} reported failure"
    elif status == MISSING_REQUIRED:
        row["detail"] = f"{argv[0]} is configured but not installed"
    elif status == TIMEOUT:
        row["detail"] = f"timed out after {result.duration:.0f}s; process tree killed"
    return row


def select_entries(stored: list[dict], discovered: list[dict],
                   tier: str) -> tuple[list[dict], str]:
    """Entries for this tier, and where they came from.

    Stored entries win: they are what the repository asked for. Discovery fills
    in only when the tier would otherwise run nothing — except at `final`, which
    is the full suite and full lint, so both sources run.
    """
    limit = TIER_ORDER[tier]
    chosen = [e for e in stored if TIER_ORDER.get(e.get("tier", "fast"), 0) <= limit]
    taken = {e["id"] for e in chosen}
    extra = [e for e in discovered
             if TIER_ORDER.get(e["tier"], 0) <= limit and e["id"] not in taken]

    if tier == "final" and chosen and extra:
        return chosen + extra, "DC:VERIFY + discovery"
    if chosen:
        return chosen, "DC:VERIFY"
    return extra, "discovery"


def write_log(agent: Path, tier: str, rows: list[dict], changed: list[str],
              origin: str, notes: list[str] | None = None) -> Path:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lines = [f"# dc_verify {tier} {stamp}",
             f"# changed: {len(changed)} file(s) ({origin})"]
    # A note explains why a run is not what it appears to be -- an unscoped
    # tier, or a bypassed gate. It belongs in the artefact that outlives the
    # terminal, or the only record of the exception is the scrollback.
    for note in notes or []:
        lines.append(f"# {note}")
    lines.append("")
    for row in rows:
        lines.append(f"== {row['id']} [{row['status']}] {row['argv']} (cwd {row['cwd']})")
        if row.get("detail"):
            lines.append(f"   {row['detail']}")
        if row.get("output"):
            lines.append(row["output"])
        lines.append("")
    path = agent / VERIFY_LOG
    _dcio.atomic_write(path, "\n".join(lines) + "\n")
    return path


EXECUTED = {PASS, FAIL, MISSING_REQUIRED, TIMEOUT}


def overall_verdict(rows: list[dict]) -> str:
    """The headline, derived from the same rules as the exit code.

    A tier where nothing actually ran says `NO-CHECKS-RUN`, whatever the reason.
    An entry that is neither allowlisted nor recorded has not been verified, and
    a report that stayed quiet about that would read as green.
    """
    statuses = {row["status"] for row in rows}
    if not rows or not statuses & EXECUTED:
        if UNSAFE in statuses:
            return "NO-CHECKS-RUN (unsafe command)"
        if APPROVAL in statuses:
            return "NO-CHECKS-RUN (approval required)"
        return "NO-CHECKS-RUN"
    if UNSAFE in statuses:
        return UNSAFE
    if APPROVAL in statuses:
        return APPROVAL
    if MISSING_REQUIRED in statuses:
        return MISSING_REQUIRED
    if TIMEOUT in statuses:
        return TIMEOUT
    if FAIL in statuses:
        return FAIL
    return PASS


def emit(tier: str, rows: list[dict], changed: list[str], change_origin: str,
         source: str, notes: list[str]) -> None:
    out = [f"TIER: {tier}  CHANGED: {len(changed)} file(s) ({change_origin})  "
           f"CHECKS: {source}"]
    shown = rows[:MAX_ROWS]
    for row in shown:
        detail = f"  {row['detail']}" if row.get("detail") else ""
        out.append(f"{row['status']:<17} {row['id']:<16} {row['duration']:.1f}s{detail}")
    if len(rows) > len(shown):
        out.append(f"SHOWING {len(shown)}/{len(rows)} checks (full log in .agent/{VERIFY_LOG})")
    out += notes[:2]

    tally: dict[str, int] = {}
    for row in rows:
        tally[row["status"]] = tally.get(row["status"], 0) + 1
    summary = ", ".join(f"{count} {status.lower()}" for status, count in sorted(tally.items()))
    out.append(f"RESULT: {overall_verdict(rows)} ({summary or 'nothing ran'})  "
               f"log: .agent/{VERIFY_LOG}")

    if len(out) > MAX_OUTPUT_LINES:
        kept = out[:MAX_OUTPUT_LINES - 1]
        kept.append(f"TRUNCATED {len(kept)}/{len(out)} lines; full log in .agent/{VERIFY_LOG}")
        out = kept
    print("\n".join(out))


def exit_code_for(rows: list[dict]) -> int:
    statuses = {row["status"] for row in rows}
    if not rows:
        return _dcio.EXIT_NO_CHECKS
    if UNSAFE in statuses or APPROVAL in statuses:
        return _dcio.EXIT_UNSAFE_COMMAND
    if MISSING_REQUIRED in statuses:
        return _dcio.EXIT_MISSING_REQUIRED
    if TIMEOUT in statuses:
        return _dcio.EXIT_TIMEOUT
    if FAIL in statuses:
        return _dcio.EXIT_CHECK_FAILED
    if statuses <= {SKIP}:
        return _dcio.EXIT_NO_CHECKS
    return _dcio.EXIT_OK


def print_approval(rows: list[dict]) -> None:
    """One bounded record per non-allowlisted entry. Nothing was executed."""
    for row in rows:
        if row["status"] != APPROVAL:
            continue
        print(f"{APPROVAL}  id={row['id']}")
        print(f"  argv:   {json.dumps(row['argv'])}")
        print(f"  cwd:    {row['cwd']}")
        print(f"  digest: {row['detail']}")
        if row.get("stale"):
            print(f"  stale:  {row['stale']}; record again after this run")
        print("  Run it through the host's own approval path, then record the outcome:")
        print(f"  dc_verify.py --record {row['id']} --digest {row['detail']} "
              "--status pass|fail --log <path>")


def stored_entries(root: Path) -> list[dict]:
    state_path = _dcio.agent_dir(root) / _dcio.STATE_NAME
    text = _dcio.read_text(state_path)
    if text is None:
        return []
    state = dc_state.State.parse(text)
    return dc_state.validate_verify_block(state.sections["VERIFY"])


NO_STATE = "NO-STATE"


def state_gap(root: Path) -> str | None:
    """Why Gate 2 cannot be considered to have run, or None when it has.

    The scripts already refuse to let an absence read as a success: an entry
    that never executed is `NO-CHECKS-RUN`, a runner that is not installed is
    `MISSING-REQUIRED`. The gates had no such refusal. A skipped Gate 2 left no
    trace, so a session could report decisions recorded and milestones planned
    with nothing on disk to contradict it, and every check would still run and
    still go green.

    Presence of the file is not the question. A file written from the template
    parses cleanly and carries an empty roadmap, so a presence check would let
    exactly the claim this refusal exists to catch -- *the plan is recorded* --
    pass on a plan that records nothing. What is asserted is what is checked:
    a roadmap with at least one row, and a decisions section that says
    something, `no open questions` included.

    Verification is the right place to notice. It is the last gate before work
    is called done, and it is already the component whose job is reporting what
    did not happen.
    """
    target = dc_registry.state_file(root)
    if not target.is_file():
        return f"{target} is absent"
    try:
        state = dc_state.State.parse(target.read_text(encoding="utf-8"))
    except (DcError, OSError) as exc:
        return f"{target} could not be read as state: {exc}"
    if not dc_state.parse_roadmap(state.sections.get("ROADMAP", "")):
        return "DC:ROADMAP has no milestone rows"
    if not state.sections.get("DECISIONS", "").strip():
        return "DC:DECISIONS is empty (write `no open questions` when Gate 1 is skipped)"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_verify.py", description=__doc__)
    parser.add_argument("--root", default=None)
    parser.add_argument("--tier", choices=sorted(TIER_ORDER))
    parser.add_argument("--base", help="verify relative to this ref instead of the work tree")
    parser.add_argument("--discover", action="store_true",
                        help="print discovered checks as a DC:VERIFY array and exit")
    parser.add_argument("--record", dest="record_id",
                        help="record a host-approved outcome for this entry id")
    parser.add_argument("--digest", help="digest issued with the APPROVAL-REQUIRED record")
    parser.add_argument("--status", choices=("pass", "fail"))
    parser.add_argument("--log", help="path to the log of the approved run")
    parser.add_argument("--allow-no-state", action="store_true",
                        help="run a tier with no DrainClamp state on disk (standalone use)")
    args = parser.parse_args()

    root = _dcio.repo_root(args.root)
    agent = _dcio.agent_dir(root)

    if args.record_id:
        if not args.digest or not args.status:
            raise DcError("--record requires --digest and --status")
        entries = {e["id"]: e for e in stored_entries(root)}
        entry = entries.get(args.record_id)
        if entry is None:
            raise DcError(f"no DC:VERIFY entry with id '{args.record_id}'",
                          _dcio.EXIT_CHECK_FAILED)
        expected = digest_for(entry["argv"], entry.get("cwd", "."))
        if args.digest != expected:
            raise DcError(
                f"digest mismatch for '{args.record_id}': got {args.digest}, "
                f"entry hashes to {expected}. The recorded run is not the entry it "
                "claims to satisfy; nothing was recorded.",
                _dcio.EXIT_UNSAFE_COMMAND,
            )
        tree = tree_fingerprint(root)
        save_record(agent, args.record_id, expected, args.status, args.log, tree)
        print(f"RECORDED {args.record_id} {args.status} {expected} tree={tree or 'unknown'}")
        return _dcio.EXIT_OK

    changed, origin = changed_files(root, args.base)
    discovered = discover(root, changed)

    if args.discover:
        print(json.dumps(discovered, indent=2))
        return _dcio.EXIT_OK

    if not args.tier:
        parser.print_help()
        return _dcio.EXIT_OK

    gap = state_gap(root)
    if gap and not args.allow_no_state:
        print(f"TIER: {args.tier}  CHECKS: none")
        print(f"{NO_STATE}  {gap}")
        print(f"RESULT: NO-CHECKS-RUN (no state) (Gate 2 has not run)  "
              f"log: {agent / 'verify.log'}")
        print()
        print(f"{NO_STATE}  Gate 2 did not run for this repository.")
        print(f"  gap:      {gap}")
        print(f"  expected: {dc_registry.state_file(root)}")
        print("  Nothing was executed. A tier verified against no roadmap and no")
        print("  recorded decisions cannot report the work as done.")
        print("  Run Gate 2 (dc_state.py --set ...), or pass --allow-no-state for")
        print("  a standalone check outside the pipeline.")
        write_log(agent, args.tier, [], [], "none")
        return _dcio.EXIT_NO_CHECKS

    notes: list[str] = []
    if gap and args.allow_no_state:
        # A bypass that leaves no trace is a bypass nobody audits. It goes in
        # the report and in the log, so a routine --allow-no-state is visible
        # as the standing exception it has become.
        notes.append(f"NOTE: {NO_STATE} bypassed with --allow-no-state ({gap}); "
                     "results are not gated on a plan")
    if origin == "unavailable":
        notes.append("NOTE: no usable change set (non-git, or git unavailable); "
                     "running configured checks unscoped")
        changed = []

    entries, source = select_entries(stored_entries(root), discovered, args.tier)
    if not entries:
        notes.append(f"{UNDISCOVERED}: no configured runner or DC:VERIFY entry for this tier")
        write_log(agent, args.tier, [], changed, source, notes)
        emit(args.tier, [], changed, origin, source, notes)
        return _dcio.EXIT_NO_CHECKS

    # A known-empty diff at `fast` means the discovered, diff-scoped checks have
    # nothing to look at. Skipping them is honest; running them unscoped would
    # quietly turn the fast tier into the final one.
    idle = args.tier == "fast" and origin != "unavailable" and not changed
    rows: list[dict] = []
    for entry in entries:
        if idle and source == "discovery":
            rows.append({"id": entry["id"], "argv": entry["argv"],
                         "cwd": entry.get("cwd", "."), "tier": entry["tier"],
                         "status": SKIP, "duration": 0.0,
                         "detail": "no changed files to scope this check to"})
            continue
        rows.append(run_entry(entry, root, agent))

    write_log(agent, args.tier, rows, changed, source, notes)
    emit(args.tier, rows, changed, origin, source, notes)
    if any(row["status"] == APPROVAL for row in rows):
        print_approval(rows)
    rc = exit_code_for(rows)
    if args.base is None:
        save_record(agent, TIER_RECORD_PREFIX + args.tier, "tier",
                    "pass" if rc == _dcio.EXIT_OK else "fail", "verify.log", tree_fingerprint(root))
    return rc


if __name__ == "__main__":
    _dcio.run_cli(main)
