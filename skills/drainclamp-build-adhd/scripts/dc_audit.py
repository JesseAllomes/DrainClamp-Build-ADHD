#!/usr/bin/env python3
"""Gate 0 — repository audit with a cache that knows when it is stale.

The audit is only worth caching if the freshness test cannot be fooled. All
five conditions must hold or the scan re-runs:

  1. schema and canonical root match
  2. HEAD is unchanged
  3. the fingerprint of eligible dirty and untracked files is unchanged
  4. the verification-config fingerprint is unchanged
  5. the TTL has not expired

HEAD plus a TTL is not enough on its own: uncommitted, renamed and untracked
files all move while HEAD sits still, and that is exactly the state a working
session is in.

Output is bounded (<=15 lines). The detail lands in `.agent/audit.md` and
`.agent/audit.json`; the JSON also carries the hashes Gate 3 reads, so a
verification-config change is detected with one bounded pass instead of broad
rediscovery.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import _dcio
import dc_map
import dc_state
from _dcio import DcError

AUDIT_SCHEMA = 1
AUDIT_JSON = "audit.json"
AUDIT_MD = "audit.md"
TTL_SECONDS = 24 * 60 * 60
MAX_OUTPUT_LINES = 15
TOP_DIRS = 6
MAX_DEAD_CANDIDATES = 5
MAX_READ_BYTES = 200_000

# Configuration files whose content changes what verification means. Presence is
# part of the input, so a creation, deletion or rename moves the fingerprint too.
CONFIG_NAMES = {
    "package.json", "pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini",
    "go.mod", "Cargo.toml", "ruff.toml", ".ruff.toml",
}
CONFIG_GLOBS = (
    "eslint.config.*", ".eslintrc*", "tsconfig*.json", "jest.config.*",
    "vitest.config.*",
)
# Never an input: a lockfile churns on every install without changing what the
# repository declares.
LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "go.sum", "requirements.lock",
}

DEAD_CODE_SKIP = {"main", "setup", "run"}


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------


def git_lines(root: Path, *args: str) -> list[str] | None:
    """Run a git query. None when git is absent or the command fails."""
    try:
        done = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.splitlines()


def head_commit(root: Path) -> str:
    lines = git_lines(root, "rev-parse", "HEAD")
    if not lines:
        return ""  # no git, or a repository with no commits yet
    return lines[0].strip()


def tracked_and_untracked(root: Path) -> set[str] | None:
    """Every non-ignored file git knows about, repo-relative."""
    lines = git_lines(root, "ls-files", "--cached", "--others", "--exclude-standard")
    if lines is None:
        return None
    return {line.strip() for line in lines if line.strip()}


# --------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
    except OSError as exc:
        return f"unreadable:{exc.errno}"
    return digest.hexdigest()


def is_config(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    if name in LOCKFILES:
        return False
    if name in CONFIG_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in CONFIG_GLOBS)


def config_files(root: Path, allowed: set[str] | None) -> list[str]:
    """Eligible, non-ignored configuration files, repo-relative and sorted."""
    found: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in dc_map.SKIP_DIRS or dc_map.is_link(entry):
                    continue
                stack.append(entry)
                continue
            rel = entry.relative_to(root).as_posix()
            if not is_config(rel):
                continue
            if allowed is not None and rel not in allowed:
                continue  # ignored by git
            found.append(rel)
    return sorted(found)


def config_fingerprint(root: Path, files: list[str]) -> str:
    """SHA-256 over a schema tag plus each path and content hash, in path order."""
    digest = hashlib.sha256()
    digest.update(f"dc-audit-config-v{AUDIT_SCHEMA}\n".encode("utf-8"))
    for rel in files:
        digest.update(f"{rel}\t{sha_file(root / rel)}\n".encode("utf-8"))
    return digest.hexdigest()


def dirty_fingerprint(root: Path) -> tuple[str, int]:
    """Fingerprint of eligible dirty, renamed and untracked files.

    Content-hashed rather than mtime-hashed: a rebuild that rewrites a file
    byte-for-byte should not invalidate the audit, and `touch` alone should not
    either. Paths are part of the input, so a rename moves the fingerprint.
    """
    lines = git_lines(root, "status", "--porcelain", "-uall")
    digest = hashlib.sha256()
    digest.update(f"dc-audit-dirty-v{AUDIT_SCHEMA}\n".encode("utf-8"))

    if lines is None:
        # No git: fall back to whole-tree metadata. Coarser, and said so.
        digest.update(b"no-git\n")
        count = 0
        for path, mtime_ns, size in dc_map.walk(root):
            rel = path.relative_to(root).as_posix()
            digest.update(f"{rel}\t{mtime_ns}\t{size}\n".encode("utf-8"))
            count += 1
        return digest.hexdigest(), count

    count = 0
    for line in sorted(lines):
        if len(line) < 4:
            continue
        code, rest = line[:2], line[3:]
        paths = [p.strip().strip('"') for p in rest.split(" -> ")]
        for rel in paths:
            if not eligible(rel):
                continue
            target = root / rel
            state = sha_file(target) if target.is_file() else "absent"
            digest.update(f"{code}\t{rel}\t{state}\n".encode("utf-8"))
            count += 1
    return digest.hexdigest(), count


def eligible(rel: str) -> bool:
    """Source or configuration — the files an audit's conclusions depend on."""
    suffix = ("." + rel.rsplit(".", 1)[-1].lower()) if "." in rel else ""
    return suffix in dc_map.SUPPORTED or suffix in dc_map.SOURCEY or is_config(rel)


def verify_block_fingerprint(root: Path) -> tuple[str, int]:
    """Canonical hash of DC:VERIFY, plus its entry count.

    An unparseable or absent block hashes to a stable sentinel rather than
    raising: Gate 0 reports, it does not adjudicate someone else's state file.
    """
    state_path = _dcio.agent_dir(root) / _dcio.STATE_NAME
    text = _dcio.read_text(state_path)
    if text is None:
        return "absent", 0
    try:
        state = dc_state.State.parse(text)
        entries = dc_state.validate_verify_block(state.sections["VERIFY"])
    except DcError:
        return "unparseable", 0
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), len(entries)


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------


def count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def rollup_key(rel: str) -> str:
    """Display bucket: the first two path components.

    Rolling up to depth two keeps the bounded output from hiding code buried
    five directories down — it is counted, just counted under its ancestor.
    """
    parts = rel.split("/")
    if len(parts) == 1:
        return "(root)"
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def scan(root: Path) -> dict:
    skipped: list[str] = []
    buckets: dict[str, dict[str, int]] = {}
    total_files = total_lines = 0
    for path, _mtime, _size in dc_map.walk(root, skipped):
        rel = path.relative_to(root).as_posix()
        lines = count_lines(path)
        bucket = buckets.setdefault(rollup_key(rel), {"files": 0, "lines": 0})
        bucket["files"] += 1
        bucket["lines"] += lines
        total_files += 1
        total_lines += lines
    ordered = sorted(buckets.items(), key=lambda kv: (-kv[1]["lines"], kv[0]))
    return {
        "files": total_files,
        "lines": total_lines,
        "depth2": [{"path": name, **counts} for name, counts in ordered],
        "skipped_links": sorted(skipped),
    }


def parse_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        return {}
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError):
        return {}


EXT_LANG = {"py": "python", "pyi": "python",
            "js": "javascript", "mjs": "javascript", "cjs": "javascript"}


def languages_from_extensions(records: dict[str, list]) -> list[str]:
    """Languages present, by extension. Never a stack.

    An extension is evidence about language only — never about declared
    dependencies, which is what a manifest would have said. Reported under
    `languages` so a stdlib-only project still shows something useful without
    the guess ever being promoted into the `stack` field.
    """
    counts: dict[str, int] = {}
    for rel in records:
        _key, status, _symbols = records[rel]
        if status != dc_map.OK or "." not in rel:
            continue
        lang = EXT_LANG.get(rel.rsplit(".", 1)[-1].lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return []
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"{lang}(inferred: {n}/{total} indexed file(s))" for lang, n in ordered]


def detect_stack(root: Path, configs: list[str]) -> list[str]:
    """Stack by manifest presence, direct dependencies only.

    Lockfiles are deliberately excluded: they describe a resolved tree, not what
    this repository declares, and counting them makes every project look the
    same size. With no manifest the answer is `unknown` — an extension census is
    a language guess, and a guess never occupies this field.
    """
    found: list[str] = []
    names = {rel.rsplit("/", 1)[-1] for rel in configs}

    if "pyproject.toml" in names:
        data = parse_toml(root / "pyproject.toml")
        deps = data.get("project", {}).get("dependencies", []) or []
        found.append(f"python(pyproject: {len(deps)} direct dep(s))")
    elif (root / "requirements.txt").is_file():
        raw = (_dcio.read_text(root / "requirements.txt") or "").splitlines()
        deps = [ln for ln in raw if ln.strip() and not ln.strip().startswith("#")]
        found.append(f"python(requirements: {len(deps)} direct dep(s))")

    if "package.json" in names:
        try:
            data = json.loads(_dcio.read_text(root / "package.json") or "{}")
        except json.JSONDecodeError:
            data = {}
        deps = len(data.get("dependencies", {})) + len(data.get("devDependencies", {}))
        found.append(f"node(package.json: {deps} direct dep(s))")

    if "go.mod" in names:
        raw = (_dcio.read_text(root / "go.mod") or "").splitlines()
        deps = [ln for ln in raw if ln.strip().startswith("require") or "\t" in ln]
        found.append(f"go(go.mod: {len(deps)} require line(s))")

    if "Cargo.toml" in names:
        data = parse_toml(root / "Cargo.toml")
        found.append(f"rust(Cargo.toml: {len(data.get('dependencies', {}))} direct dep(s))")

    return found or ["unknown(no recognised manifest)"]


def dead_code_candidates(root: Path, records: dict[str, list]) -> list[str]:
    """Module-level Python symbols referenced nowhere else.

    Candidates, never findings: a name reached through `getattr`, an entry point,
    a plugin registry or a template is invisible to this counting. The word
    `candidate` is the whole point.
    """
    defined: list[tuple[str, str]] = []
    for rel in sorted(records):
        _key, status, symbols = records[rel]
        if status != dc_map.OK or not rel.endswith((".py", ".pyi")):
            continue
        if "test" in rel.lower():
            continue
        for name, _start, _end in symbols:
            if "." in name or name.startswith("_") or name in DEAD_CODE_SKIP:
                continue
            defined.append((rel, name))
    if not defined:
        return []

    counts = {name: 0 for _rel, name in defined}
    for path, _mtime, size in dc_map.walk(root):
        if size > MAX_READ_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name in counts:
            if name in text:
                counts[name] += text.count(name)

    return [f"{rel}::{name}" for rel, name in defined if counts.get(name, 0) <= 1]


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def freshness(cached: dict, current: dict) -> list[str]:
    """Every reason the cache may not be reused. Empty means it may.

    All five conditions are evaluated rather than short-circuited: a new
    `pyproject.toml` is both an untracked file and a verification-config change,
    and reporting only the first would hide the one that drives Gate 3.
    """
    if not cached:
        return ["no cached audit"]
    if cached.get("schema") != AUDIT_SCHEMA:
        return ["audit schema changed"]
    if cached.get("root") != current["root"]:
        return ["canonical root changed"]

    reasons: list[str] = []
    if cached.get("head") != current["head"]:
        reasons.append("HEAD changed")
    if cached.get("dirty_fingerprint") != current["dirty_fingerprint"]:
        reasons.append("dirty or untracked files changed")
    if cached.get("verify_config_fingerprint") != current["verify_config_fingerprint"]:
        reasons.append("verification config changed")
    try:
        generated = datetime.fromisoformat(cached.get("generated", ""))
        age = (datetime.now(timezone.utc) - generated).total_seconds()
        if age > TTL_SECONDS:
            reasons.append(f"TTL expired ({age / 3600:.0f}h)")
    except ValueError:
        reasons.append("cached timestamp unreadable")
    return reasons


def gate3_reason(cached: dict, current: dict) -> str | None:
    """Why Gate 3 must run, or None when the fingerprint is unchanged."""
    if current["verify_entries"] == 0:
        return "DC:VERIFY is empty"
    if not cached:
        return "no cached fingerprint"
    if cached.get("verify_config_fingerprint") != current["verify_config_fingerprint"]:
        return "verification config changed"
    if cached.get("verify_block_fingerprint") != current["verify_block_fingerprint"]:
        return "DC:VERIFY changed"
    return None


def render_markdown(report: dict) -> str:
    lines = [
        f"<!-- drainclamp-build: audit; schema={AUDIT_SCHEMA} -->",
        "# Audit",
        "",
        f"Root: `{report['root']}`  ",
        f"Generated: {report['generated']}  ",
        f"HEAD: {report['head'] or '(none)'}",
        "",
        f"{report['scan']['files']} source file(s), {report['scan']['lines']} line(s). "
        "COVERAGE: partial — indexed extensions only.",
        "",
        "## Stack",
        "",
    ]
    lines += [f"- {item}" for item in report["stack"]]
    if report.get("languages"):
        lines += ["", "## Languages (inferred from extensions, not declared)", ""]
        lines += [f"- {item}" for item in report["languages"]]
    lines += ["", "## Totals (rolled up to depth 2)", "", "| path | files | lines |",
              "|---|---|---|"]
    lines += [f"| {row['path']} | {row['files']} | {row['lines']} |"
              for row in report["scan"]["depth2"]]
    lines += ["", "## Dead-code candidates (unverified)", ""]
    if report["dead_code_candidates"]:
        lines += [f"- {item}" for item in report["dead_code_candidates"]]
        lines += ["", "Candidates only. A name reached through `getattr`, an entry point or a "
                  "template is invisible to this count."]
    else:
        lines += ["- none"]
    if report["scan"]["skipped_links"]:
        lines += ["", "## Skipped links", ""]
        lines += [f"- {item} (linked directory, not followed)"
                  for item in report["scan"]["skipped_links"]]
    if report["config_files"]:
        lines += ["", "## Verification config inputs", ""]
        lines += [f"- {item}" for item in report["config_files"]]
    return "\n".join(lines) + "\n"


def emit(report: dict, refreshed: bool, reason: str | None, gate3: str | None) -> None:
    """<=15 lines. Anything trimmed says so."""
    out: list[str] = []
    if refreshed:
        out.append(f"AUDIT: refreshed ({reason})")
    else:
        out.append("CACHE: fresh (audit reused)")
    scan = report["scan"]
    out.append(f"root={report['root_name']} files={scan['files']} lines={scan['lines']} "
               "COVERAGE: partial")
    out.append("stack: " + "; ".join(report["stack"]))
    if report.get("languages"):
        out.append("languages (inferred): " + "; ".join(report["languages"]))

    rows = scan["depth2"]
    for row in rows[:TOP_DIRS]:
        out.append(f"  {row['path']}  {row['files']} file(s)  {row['lines']} line(s)")
    if len(rows) > TOP_DIRS:
        out.append(f"SHOWING {TOP_DIRS}/{len(rows)} directories (full table in .agent/audit.md)")

    candidates = report["dead_code_candidates"]
    if candidates:
        shown = candidates[:MAX_DEAD_CANDIDATES]
        out.append("dead-code candidates (unverified): " + ", ".join(shown))
        if len(candidates) > len(shown):
            out.append(f"SHOWING {len(shown)}/{len(candidates)} candidates")
    if scan["skipped_links"]:
        out.append(f"skipped {len(scan['skipped_links'])} linked director(y/ies): "
                   + ", ".join(scan["skipped_links"][:3]))
    out.append(f"GATE3: {'required (' + gate3 + ')' if gate3 else 'skip (fingerprint unchanged)'}")
    out.append("detail: .agent/audit.md")

    if len(out) > MAX_OUTPUT_LINES:
        kept = out[:MAX_OUTPUT_LINES - 1]
        kept.append(f"TRUNCATED {len(kept)}/{len(out)} lines; full report in .agent/audit.md")
        out = kept
    print("\n".join(out))


def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_audit.py", description=__doc__)
    parser.add_argument("root", nargs="?", default=None)
    parser.add_argument("--root", dest="root_flag", default=None,
                        help="same as the positional root; accepted so every "
                             "dc_*.py script takes --root")
    parser.add_argument("--force", action="store_true",
                        help="rescan even when the cache is fresh")
    args = parser.parse_args()

    if args.root_flag is not None and args.root is not None:
        raise DcError("give the root once: positionally or as --root, not both")

    root = _dcio.repo_root(args.root_flag or args.root)
    agent = _dcio.agent_dir(root)
    cache_file = agent / AUDIT_JSON

    allowed = tracked_and_untracked(root)
    configs = config_files(root, allowed)
    dirty_hash, dirty_count = dirty_fingerprint(root)
    verify_hash, verify_entries = verify_block_fingerprint(root)

    current = {
        "schema": AUDIT_SCHEMA,
        "root": root.as_posix(),
        "head": head_commit(root),
        "dirty_fingerprint": dirty_hash,
        "dirty_files": dirty_count,
        "verify_config_fingerprint": config_fingerprint(root, configs),
        "verify_block_fingerprint": verify_hash,
        "verify_entries": verify_entries,
        "config_files": configs,
    }

    cached: dict = {}
    raw = _dcio.read_text(cache_file)
    if raw:
        try:
            cached = json.loads(raw)
        except json.JSONDecodeError:
            cached = {}

    reasons = freshness(cached, current)
    reason = "; ".join(reasons)
    gate3 = gate3_reason(cached, current)

    if not reasons and not args.force:
        report = dict(cached)
        report["root_name"] = root.name
        # Gate 3 is judged on the live fingerprints, not the cached ones.
        emit(report, refreshed=False, reason=None, gate3=gate3)
        return _dcio.EXIT_OK

    records = dc_map.refresh(root)
    report = dict(current)
    report["generated"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report["root_name"] = root.name
    report["scan"] = scan(root)
    report["stack"] = detect_stack(root, configs)
    report["languages"] = languages_from_extensions(records)
    report["dead_code_candidates"] = dead_code_candidates(root, records)
    report["gate3_required"] = bool(gate3)

    persisted = {k: v for k, v in report.items() if k != "root_name"}
    _dcio.atomic_write(cache_file, json.dumps(persisted, indent=2) + "\n")
    _dcio.atomic_write(agent / AUDIT_MD, render_markdown(report))

    emit(report, refreshed=True, reason=reason or "forced", gate3=gate3)
    return _dcio.EXIT_OK


if __name__ == "__main__":
    _dcio.run_cli(main)
