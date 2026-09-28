#!/usr/bin/env python3
"""Durable DrainClamp state: `.agent/drainclamp-state.md`, schema v1.

Every mutation is one locked transaction: acquire -> re-read the whole file ->
modify one section -> validate the generation -> atomically replace -> release.
The lock is held only for that window, never while scanning.

DC:LOG is capped. Overflow moves to `.agent/drainclamp-log-archive.md` in a
crash-recoverable order: archive first and fsync, then replace the state file.
An interruption between the two leaves duplicates, never a gap, and the next
mutation reconciles them by id.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import _dcio
import dc_registry
from _dcio import DcError, GenerationMismatch

SECTIONS = ("ARCH", "DECISIONS", "ROADMAP", "VERIFY", "LOG")
HEADER_RE = re.compile(
    r"^<!--\s*drainclamp-build:\s*state;\s*schema=(\d+);\s*generation=(\d+)\s*-->\s*$"
)
VERIFY_KEYS_REQUIRED = {"id", "argv", "cwd", "tier"}
VERIFY_KEYS_OPTIONAL = {"timeout_s"}
VERIFY_TIERS = {"fast", "milestone", "final"}
LOG_ENTRY_RE = re.compile(r"^-\s+(\S+)\s+([0-9a-f]{8})\s+(.*)$")
ROADMAP_STATUSES = {"pending", "active", "done"}
PURGE_THRESHOLD = 0.20
LOG_CAP = 20
ARCHIVE_NAME = "drainclamp-log-archive.md"
RESUME_NAME = "drainclamp-resume.md"
ARCHIVE_HEADER = (
    "<!-- drainclamp-build: log archive; schema=1 -->\n"
    "# DrainClamp log archive\n"
    "\n"
    "Overflow from DC:LOG, keyed by the 8-character entry id. Logically\n"
    "append-only. Never auto-loaded into context, and never read during resume —\n"
    "resume uses `.agent/drainclamp-state.md` alone. This file is for forensics.\n"
)
PROMOTE_MARKER = "DRAINCLAMP:CONSTRAINTS"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class State:
    """Parsed state file. Malformed input raises; it is never auto-repaired."""

    def __init__(self, schema: int, generation: int, prologue: str,
                 sections: dict[str, str], layout: list[tuple[str, str]]):
        self.schema = schema
        self.generation = generation
        self.prologue = prologue
        self.sections = sections
        self.layout = layout

    @classmethod
    def parse(cls, text: str) -> "State":
        lines = text.splitlines()
        if not lines:
            raise DcError("state file is empty; expected a schema header")
        header = HEADER_RE.match(lines[0])
        if not header:
            raise DcError(
                "state file header missing or malformed; expected "
                "'<!-- drainclamp-build: state; schema=1; generation=N -->' on line 1"
            )
        schema, generation = int(header.group(1)), int(header.group(2))
        if schema != _dcio.SCHEMA_VERSION:
            raise DcError(
                f"state schema {schema} is not supported by this build "
                f"(expects {_dcio.SCHEMA_VERSION}); refusing to rewrite it"
            )

        body = "\n".join(lines[1:])
        sections: dict[str, str] = {}
        seen: list[str] = []
        for name in SECTIONS:
            opens = [m.start() for m in re.finditer(rf"<!--\s*DC:{name}\s*-->", body)]
            closes = [m.start() for m in re.finditer(rf"<!--\s*/DC:{name}\s*-->", body)]
            if len(opens) != 1 or len(closes) != 1:
                raise DcError(
                    f"DC:{name} must appear exactly once as a matched pair "
                    f"(found {len(opens)} open, {len(closes)} close)"
                )
            if closes[0] < opens[0]:
                raise DcError(f"DC:{name} close marker precedes its open marker")
            seen.append(name)

        order = sorted(
            SECTIONS,
            key=lambda n: re.search(rf"<!--\s*DC:{n}\s*-->", body).start(),
        )
        if tuple(order) != SECTIONS:
            raise DcError(
                "sections are out of order; expected "
                + " -> ".join(SECTIONS)
                + ", found "
                + " -> ".join(order)
            )

        layout: list[tuple[str, str]] = []
        cursor = 0
        for name in SECTIONS:
            open_m = re.search(rf"<!--\s*DC:{name}\s*-->", body)
            close_m = re.search(rf"<!--\s*/DC:{name}\s*-->", body)
            layout.append(("gap", body[cursor:open_m.start()]))
            sections[name] = body[open_m.end():close_m.start()].strip("\n")
            layout.append(("section", name))
            cursor = close_m.end()
        layout.append(("gap", body[cursor:]))
        return cls(schema, generation, "", sections, layout)

    def render(self, generation: int) -> str:
        out = [f"<!-- drainclamp-build: state; schema={self.schema}; "
               f"generation={generation} -->"]
        body = []
        for kind, value in self.layout:
            if kind == "gap":
                body.append(value)
            else:
                content = self.sections[value]
                inner = f"\n{content}\n" if content else "\n"
                body.append(f"<!-- DC:{value} -->{inner}<!-- /DC:{value} -->")
        out.append("".join(body))
        text = "\n".join(out)
        return text if text.endswith("\n") else text + "\n"


def template_text() -> str:
    tpl = Path(__file__).resolve().parent.parent / "assets" / "state.template.md"
    body = _dcio.read_text(tpl)
    if body is None:
        raise DcError(f"state template missing at {tpl}")
    return body


# --------------------------------------------------------------------------
# Section validation
# --------------------------------------------------------------------------


def validate_verify_block(raw: str) -> list[dict]:
    """Parse DC:VERIFY. Unknown keys are rejected, never ignored."""
    stripped = raw.strip()
    if not stripped:
        return []
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", stripped, re.DOTALL)
    payload = fence.group(1) if fence else stripped
    try:
        entries = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DcError(f"DC:VERIFY is not valid JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise DcError("DC:VERIFY must be a JSON array")

    ids: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"DC:VERIFY[{index}]"
        if not isinstance(entry, dict):
            raise DcError(f"{where} must be an object")
        keys = set(entry)
        missing = VERIFY_KEYS_REQUIRED - keys
        if missing:
            raise DcError(f"{where} missing required key(s): {', '.join(sorted(missing))}")
        unknown = keys - VERIFY_KEYS_REQUIRED - VERIFY_KEYS_OPTIONAL
        if unknown:
            raise DcError(
                f"{where} has unknown key(s): {', '.join(sorted(unknown))}. "
                "Schema v1 stores no trust or verdict field — those are derived in "
                "code. Adding a field requires schema 2."
            )
        if not isinstance(entry["argv"], list) or not entry["argv"]:
            raise DcError(f"{where} 'argv' must be a non-empty array, never a string")
        if not all(isinstance(part, str) for part in entry["argv"]):
            raise DcError(f"{where} 'argv' must contain only strings")
        if entry["tier"] not in VERIFY_TIERS:
            raise DcError(
                f"{where} 'tier' must be one of {', '.join(sorted(VERIFY_TIERS))}"
            )
        if not isinstance(entry["cwd"], str):
            raise DcError(f"{where} 'cwd' must be a string")
        if entry["id"] in ids:
            raise DcError(f"{where} duplicate id '{entry['id']}'")
        ids.add(entry["id"])
    return entries


def parse_roadmap(raw: str) -> list[dict]:
    """Rows of `| id | goal | files | status |`, optionally `| deps |`.

    The fifth column is optional and semicolon-separated. A four-column table
    parses exactly as it always did -- the loop already tolerated extra cells
    and ignored them -- so existing state files need no migration and the
    schema does not move.

    Dependencies are what let the purge calculus tell *which* pending milestone
    comes next. With several pending and nothing to order them, the calculus
    can only report `multiple eligible next milestones` and hold a context it
    might have been able to release.
    """
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0].lower() == "id" or set(cells[0]) <= {"-", ":"}:
            continue
        files = [f.strip() for f in cells[2].split(";") if f.strip()]
        status = cells[3].lower()
        if status not in ROADMAP_STATUSES:
            raise DcError(
                f"DC:ROADMAP row '{cells[0]}' has status '{cells[3]}'; "
                f"expected one of {', '.join(sorted(ROADMAP_STATUSES))}"
            )
        deps = [d.strip() for d in cells[4].split(";") if d.strip()] if len(cells) > 4 else []
        rows.append({"id": cells[0], "goal": cells[1], "files": files,
                     "status": status, "deps": deps})
    validate_deps(rows)
    return rows


def resume_text(state: "State", generation: int) -> str:
    """Render a bounded re-entry projection; the full state remains authoritative."""
    rows = parse_roadmap(state.sections.get("ROADMAP", ""))
    done = [r["id"] for r in rows if r["status"] == "done"]
    active = [r for r in rows if r["status"] == "active"]
    pending = [r for r in rows if r["status"] == "pending"]
    finished = ", ".join(done) if done else "none"
    live = f'{active[0]["id"]}: {active[0]["goal"]}' if active else "none"
    done_ids = set(done)
    eligible = [r for r in pending if set(r["deps"]).issubset(done_ids)] or pending
    if eligible:
        next_step = f'{eligible[0]["id"]}: {eligible[0]["goal"]}'
    elif rows and len(done) == len(rows):
        next_step = "project complete"
    else:
        next_step = "no roadmap action available"
    return (
        "# DrainClamp resume capsule\n"
        f"Done: {finished}\n"
        f"In progress: {live}\n"
        f"Next: {next_step}\n"
        f"Generation: {generation}\n"
    )


def write_resume_capsule(agent: Path, state: "State", generation: int) -> None:
    """Write the small re-entry projection after the authoritative state write."""
    _dcio.atomic_write(agent / RESUME_NAME, resume_text(state, generation))


def validate_deps(rows: list[dict]) -> None:
    """Reject a dependency graph that cannot be satisfied.

    A dependency naming a milestone that does not exist, or a cycle, makes the
    roadmap unorderable. Both are reported rather than repaired: a roadmap that
    silently drops an edge is worse than one that refuses to parse, because the
    calculus would then order milestones by a graph the author never wrote.
    """
    known = {r["id"] for r in rows}
    for row in rows:
        for dep in row["deps"]:
            if dep == row["id"]:
                raise DcError(f"DC:ROADMAP row '{row['id']}' depends on itself")
            if dep not in known:
                raise DcError(
                    f"DC:ROADMAP row '{row['id']}' depends on '{dep}', "
                    "which is not a milestone in this roadmap"
                )

    # Iterative peel: whatever cannot be removed is in, or behind, a cycle.
    remaining = {r["id"]: set(r["deps"]) for r in rows}
    while True:
        free = [rid for rid, deps in remaining.items() if not deps]
        if not free:
            break
        for rid in free:
            del remaining[rid]
        for deps in remaining.values():
            deps.difference_update(free)
    if remaining:
        raise DcError(
            "DC:ROADMAP has a dependency cycle involving: "
            + ", ".join(sorted(remaining))
        )


# --------------------------------------------------------------------------
# DC:LOG cap and archive
#
# Two files cannot be updated atomically together, so the order is chosen so
# that a crash duplicates rather than loses:
#
#   1. append the overflow entries to the archive, fsync
#   2. replace the state file without them
#
# A crash between 1 and 2 leaves those entries in *both* files. The next
# mutation notices the overlap by id and drops them from the active log. A crash
# before 1 changes nothing. There is no ordering that loses an entry.
# --------------------------------------------------------------------------


def log_lines(section: str) -> list[str]:
    return [line for line in section.splitlines() if line.strip()]


def entry_id(line: str) -> str | None:
    match = LOG_ENTRY_RE.match(line.strip())
    return match.group(2) if match else None


def archive_path(agent: Path) -> Path:
    return agent / ARCHIVE_NAME


def archived_ids(agent: Path) -> set[str]:
    text = _dcio.read_text(archive_path(agent))
    if text is None:
        return set()
    return {i for i in (entry_id(line) for line in text.splitlines()) if i}


def archive_append(agent: Path, entries: list[str]) -> int:
    """Append entries not already present. Idempotent by id.

    Returns how many were actually written. Durable before it returns, so the
    caller may then drop them from the active log.
    """
    path = archive_path(agent)
    existing_text = _dcio.read_text(path)
    known = archived_ids(agent)
    fresh = [line for line in entries if (eid := entry_id(line)) and eid not in known]
    if not fresh:
        return 0
    body = existing_text if existing_text is not None else ARCHIVE_HEADER
    if not body.endswith("\n"):
        body += "\n"
    _dcio.atomic_write(path, body + "\n".join(fresh) + "\n")
    return len(fresh)


def reconcile_log(agent: Path, lines: list[str]) -> list[str]:
    """Drop active entries already durable in the archive.

    This is what repairs a crash between the archive write and the state
    replacement: the entries survived in the archive, so the copies left in the
    active log are redundant.
    """
    known = archived_ids(agent)
    if not known:
        return lines
    return [line for line in lines if (entry_id(line) or "") not in known]


def apply_log_cap(agent: Path, lines: list[str]) -> list[str]:
    """Enforce LOG_CAP, archiving the oldest overflow first."""
    lines = reconcile_log(agent, lines)
    if len(lines) <= LOG_CAP:
        return lines
    overflow = lines[:len(lines) - LOG_CAP]
    archive_append(agent, overflow)  # durable before we drop them
    return lines[len(lines) - LOG_CAP:]


# --------------------------------------------------------------------------
# Transaction
# --------------------------------------------------------------------------


def load(state_path: Path) -> State:
    text = _dcio.read_text(state_path)
    if text is None:
        return State.parse(template_text())
    return State.parse(text)


def mutate(state_path: Path, lock_dir: Path, apply_fn, expect: int | None = None) -> int:
    """Locked read-modify-replace. Returns the committed generation.

    `expect` is the generation the caller read when it began the work it is
    about to commit, which is not the same as the generation on disk when it
    finally writes. The lock already serialises writers, so the recheck below
    can only catch an edit made outside the lock -- it cannot catch a second
    agent that read, planned and wrote entirely between this caller's read and
    its write. In a workflow where an operator alternates between agents on one
    repository, that window is minutes long, and the losing write is silent:
    both agents are told they succeeded, and one plan simply ceases to exist.

    Passing `expect` closes it. The comparison is against the generation the
    caller reasoned from, so a file that has moved on since is refused rather
    than overwritten.
    """
    with _dcio.FileLock(lock_dir):
        text = _dcio.read_text(state_path)
        fresh_file = text is None
        state = State.parse(template_text() if fresh_file else text)
        observed = state.generation

        if expect is not None and expect != observed:
            raise GenerationMismatch(expect, observed)

        apply_fn(state)

        if not fresh_file:
            recheck = _dcio.read_text(state_path)
            if recheck is None:
                raise DcError("state file disappeared mid-transaction")
            current = State.parse(recheck)
            if current.generation != observed:
                raise GenerationMismatch(observed, current.generation)

        committed = observed + 1
        _dcio.atomic_write(state_path, state.render(committed))
        write_resume_capsule(lock_dir, state, committed)
        return committed


# --------------------------------------------------------------------------
# Purge calculus
# --------------------------------------------------------------------------


GLOB_CHARS = set("*?[")


def normalise(path: str, root: Path) -> str | None:
    """Repo-relative, forward slashes, case-folded on Windows.

    Returns None when the path escapes the repository.
    """
    raw = path.strip().replace("\\", "/")
    if not raw:
        return None
    candidate = Path(raw)
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        rel = resolved.relative_to(root.resolve())
    except ValueError:
        return None
    text = posixpath.normpath(rel.as_posix())
    if text.startswith(".."):
        return None
    return text.lower() if sys.platform == "win32" else text


def expand(entries: list[str], root: Path) -> tuple[set[str], list[str], list[str]]:
    """Return (members, unresolved patterns, escaped paths).

    A named repo-relative file counts even when it does not exist yet —
    milestones routinely create files. Only directories and globs that resolve
    to nothing are unresolved.
    """
    members: set[str] = set()
    unresolved: list[str] = []
    escaped: list[str] = []
    for entry in entries:
        raw = entry.strip().replace("\\", "/")
        if not raw:
            continue
        if GLOB_CHARS & set(raw):
            hits = {
                norm
                for candidate in root.rglob("*")
                if candidate.is_file()
                and fnmatch.fnmatch(
                    candidate.relative_to(root).as_posix(), raw
                )
                and (norm := normalise(candidate.relative_to(root).as_posix(), root))
            }
            if hits:
                members |= hits
            else:
                unresolved.append(entry)
            continue
        norm = normalise(raw, root)
        if norm is None:
            escaped.append(entry)
            continue
        target = root / norm
        if target.is_dir():
            hits = {
                n
                for child in target.rglob("*")
                if child.is_file()
                and (n := normalise(child.relative_to(root).as_posix(), root))
            }
            if hits:
                members |= hits
            else:
                unresolved.append(entry)
            continue
        # Named file: counts whether or not it exists yet.
        members.add(norm)
    return members, unresolved, escaped


def purge_check(state: State, root: Path, context_high: bool) -> tuple[str, str]:
    """Return (verdict_line, human_line). Missing data never yields PURGE."""
    rows = parse_roadmap(state.sections["ROADMAP"])
    done = [r for r in rows if r["status"] == "done"]
    active = [r for r in rows if r["status"] == "active"]
    pending = [r for r in rows if r["status"] == "pending"]

    # Every milestone done is a terminator, not an overlap question. There is
    # no next milestone to compare against, so the overlap calculus below would
    # report `HOLD (no next milestone)` and leave a finished project sitting in
    # a context it no longer needs.
    if rows and len(done) == len(rows):
        return (
            f"COMPLETE ({len(rows)}/{len(rows)} milestones done)",
            f"DRAINCLAMP: All {len(rows)} milestones done. "
            "State saved to .agent/drainclamp-state.md.",
        )

    reason: str | None = None
    overlap: float | None = None
    target: dict | None = None

    # `deps` narrows the pending set to what is actually startable. Without it
    # every pending row looks equally eligible and the calculus can only hold.
    #
    # It does not disambiguate multiple *active* rows: several milestones
    # genuinely in flight at once is a real state, not a missing edge, and
    # holding the context is the correct answer there.
    done_ids = {r["id"] for r in done}
    unblocked = [r for r in pending if all(d in done_ids for d in r["deps"])]
    if not active and len(pending) > 1 and len(unblocked) == 1:
        pending = unblocked

    # Exactly one explicitly identified next milestone, or nothing.
    if len(active) == 1:
        target = active[0]
    elif len(active) > 1:
        reason = "multiple milestones active"
    # There is deliberately no "everything is blocked" branch. With an acyclic
    # graph -- which validate_deps guarantees -- and no active milestone, some
    # pending row always has every dependency satisfied, so the state cannot
    # arise. A branch that cannot fire would only claim a handling it does not
    # have.
    elif len(pending) > 1:
        reason = "multiple eligible next milestones"
    elif not active and len(pending) == 1:
        target = pending[0]
    else:
        reason = "no next milestone"

    if target is not None:
        if not target["files"]:
            reason = "next milestone empty"
        else:
            next_set, unresolved, _ = expand(target["files"], root)
            if unresolved:
                reason = f"unresolved path pattern: {unresolved[0]}"
            elif not next_set:
                reason = "next milestone empty"
            elif not done:
                # Nothing has been completed, so there is no set to compare
                # against. An empty completed set is undefined, not zero —
                # rendering it as 0% would manufacture a confident PURGE.
                reason = "no completed milestone"
            else:
                done_set, done_unresolved, _ = expand(done[-1]["files"], root)
                if done_unresolved:
                    reason = f"unresolved path pattern: {done_unresolved[0]}"
                elif not done_set:
                    reason = "completed milestone empty"
                else:
                    overlap = len(done_set & next_set) / len(next_set)

    if context_high:
        shown = f"overlap {overlap * 100:.0f}%" if overlap is not None else "overlap unknown"
        return (
            f"PURGE (context-high; {shown})",
            "DRAINCLAMP: State saved to .agent/drainclamp-state.md. "
            "Context purge recommended.",
        )
    if overlap is None:
        return (
            f"HOLD (overlap unknown: {reason})",
            "DRAINCLAMP: State saved to .agent/drainclamp-state.md. "
            f"Context retained (overlap unknown: {reason}).",
        )
    if overlap < PURGE_THRESHOLD:
        return (
            f"PURGE (overlap {overlap * 100:.0f}%)",
            "DRAINCLAMP: State saved to .agent/drainclamp-state.md. "
            "Context purge recommended.",
        )
    return (
        f"HOLD (overlap {overlap * 100:.0f}%)",
        "DRAINCLAMP: State saved to .agent/drainclamp-state.md. "
        f"Context retained (overlap {overlap * 100:.0f}%).",
    )


def chunk_purge_check(done_targets: list[str], next_targets: list[str], root: Path,
                      context_high: bool) -> tuple[str, str]:
    """The milestone calculus applied at a chunk boundary, over chunk targets.

    `path::symbol` compares by path: two symbols of one file share the reads
    that loaded it, so counting them apart would understate the overlap. As
    with milestones, an empty or unresolved side is unknown and never PURGE.
    """
    def paths(targets: list[str]) -> list[str]:
        return [t.partition("::")[0] for t in targets if t.partition("::")[0].strip()]

    saved = "DRAINCLAMP: Chunk saved to .agent/drainclamp-project.json. "
    reason: str | None = None
    overlap: float | None = None
    done_paths, next_paths = paths(done_targets), paths(next_targets)
    if not done_paths:
        reason = "finished chunk has no targets"
    elif not next_paths:
        reason = "next chunk has no targets"
    else:
        done_set, done_bad, _ = expand(done_paths, root)
        next_set, next_bad, _ = expand(next_paths, root)
        if done_bad or next_bad:
            reason = f"unresolved path pattern: {(done_bad or next_bad)[0]}"
        elif not done_set or not next_set:
            reason = "chunk targets resolve to nothing"
        else:
            overlap = len(done_set & next_set) / len(next_set)

    shown = f"overlap {overlap * 100:.0f}%" if overlap is not None else f"overlap unknown: {reason}"
    if context_high or (overlap is not None and overlap < PURGE_THRESHOLD):
        tag = f"context-high; {shown}" if context_high else shown
        return f"PURGE (chunk {tag})", saved + "Context purge recommended."
    return f"HOLD (chunk {shown})", saved + f"Context retained ({shown})."


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def log_id(sentence: str, when: str) -> str:
    """Stable-format unique id. Entropy included so two identical sentences
    logged in the same second do not collide — the archive dedups by id, and a
    collision there would silently drop an entry."""
    seed = f"{when}|{sentence}|{os.getpid()}|{os.urandom(8).hex()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def find_agents_files(root: Path) -> list[Path]:
    """Repository-scoped AGENTS.md candidates, nearest first."""
    found = [root / "AGENTS.md"] if (root / "AGENTS.md").exists() else []
    for path in sorted(root.rglob("AGENTS.md")):
        if path == root / "AGENTS.md":
            continue
        rel = path.relative_to(root).parts
        if any(part in {".git", ".venv", "node_modules", ".agent"} for part in rel):
            continue
        found.append(path)
    return found


def promote(state: State, root: Path, select: str | None, dest: str | None,
            confirm: bool) -> int:
    """Copy selected durable constraints into AGENTS.md.

    Deliberately narrow. Explicit permission makes the write allowable; it does
    not make wholesale copying appropriate. Task state — roadmap, verification
    list, log — is not instruction material and is never promoted.
    """
    decisions = [ln for ln in state.sections["DECISIONS"].splitlines() if ln.strip()]
    decisions = [ln for ln in decisions if ln.strip().lower() != "no open questions"]
    if not decisions:
        raise DcError("DC:DECISIONS holds no durable constraints to promote")

    if not select:
        print("Selectable constraints from DC:DECISIONS:")
        for index, line in enumerate(decisions, 1):
            print(f"  {index}. {line.lstrip('- ').strip()}")
        print()
        print("Nothing was written. Re-run with --select 1,3 to choose, then "
              "--confirm to write.")
        print("ROADMAP, VERIFY and LOG are task state and are never promoted.")
        return _dcio.EXIT_OK

    try:
        picked = [decisions[int(token) - 1] for token in select.split(",") if token.strip()]
    except (ValueError, IndexError) as exc:
        raise DcError(
            f"--select must be comma-separated indexes in 1..{len(decisions)}"
        ) from exc
    if not picked:
        raise DcError("--select chose nothing")

    candidates = find_agents_files(root)
    if dest:
        target = Path(dest)
        if not target.is_absolute():
            target = root / dest
        if not _dcio.is_within(target.parent, root):
            raise DcError(f"destination {dest} is outside the repository")
    elif len(candidates) == 1:
        target = candidates[0]
    elif len(candidates) > 1:
        listing = "\n".join(f"  {p.relative_to(root).as_posix()}" for p in candidates)
        raise DcError(
            "several instruction files apply; name one with --dest:\n" + listing
        )
    else:
        target = root / "AGENTS.md"

    block_lines = [f"<!-- {PROMOTE_MARKER} -->",
                   "## Constraints (promoted from DrainClamp state)", ""]
    block_lines += [f"- {line.lstrip('- ').strip()}" for line in picked]
    block_lines += ["", f"<!-- /{PROMOTE_MARKER} -->"]
    block = "\n".join(block_lines)

    rel = target.relative_to(root).as_posix() if _dcio.is_within(target, root) else str(target)
    existing = _dcio.read_text(target)
    action = "create" if existing is None else (
        "replace the DrainClamp section in" if PROMOTE_MARKER in existing else "append to")

    print(f"Proposed block for {rel} ({action}):")
    print()
    print(block)
    print()
    if not confirm:
        print(f"Nothing was written. Re-run with --confirm to {action} {rel}.")
        return _dcio.EXIT_OK

    if existing is None:
        merged = block + "\n"
    elif PROMOTE_MARKER in existing:
        merged = re.sub(
            rf"<!--\s*{PROMOTE_MARKER}\s*-->.*?<!--\s*/{PROMOTE_MARKER}\s*-->",
            block, existing, flags=re.DOTALL)
    else:
        joiner = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        merged = existing + joiner + block + "\n"

    _dcio.atomic_write(target, merged)
    print(f"{rel}: {len(picked)} constraint(s) written.")
    return _dcio.EXIT_OK


def warn_unignored(root: Path, agent: Path) -> None:
    """Warn once that `.agent/` is untracked-but-unignored. Never edits ignores."""
    stamp = agent / ".ignore-warned"
    if stamp.exists():
        return
    gitignore = root / ".gitignore"
    exclude = root / ".git" / "info" / "exclude"
    ignored = any(
        ".agent" in (_dcio.read_text(p) or "") for p in (gitignore, exclude)
    )
    if not ignored and (root / ".git").exists():
        print(
            "DRAINCLAMP: .agent/ is not ignored by this repository. "
            "Add it yourself if you want it excluded — nothing was modified."
        )
    stamp.write_text("1", encoding="utf-8")


def register(root: Path) -> None:
    """Index this repository so a session can list it. Best effort.

    The registry is a convenience over state that is already durable on disk,
    so a home directory that is read-only or on a dead network share must not
    turn a successful state write into a failed command.
    """
    try:
        dc_registry.touch(root)
    except (OSError, DcError, ValueError):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_state.py", description=__doc__)
    parser.add_argument("--root", default=None)
    parser.add_argument("--set", dest="section", choices=SECTIONS)
    parser.add_argument("--file", dest="source")
    parser.add_argument("--append-log", dest="log_entry")
    parser.add_argument("--expect-generation", dest="expect", type=int, default=None,
                        help="refuse the write if the file has moved past this "
                             "generation since you read it")
    parser.add_argument("--purge-check", action="store_true")
    parser.add_argument("--context-high", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--promote-to-agents", action="store_true",
                        help="copy selected DC:DECISIONS constraints into AGENTS.md")
    parser.add_argument("--select", help="comma-separated constraint indexes")
    parser.add_argument("--dest", help="which AGENTS.md to write")
    parser.add_argument("--confirm", action="store_true",
                        help="actually write; without it the block is only shown")
    args = parser.parse_args()

    root = _dcio.repo_root(args.root)
    agent = _dcio.agent_dir(root)
    state_path = agent / _dcio.STATE_NAME
    warn_unignored(root, agent)

    if args.section:
        if not args.source:
            raise DcError("--set requires --file")
        content = _dcio.read_text(Path(args.source))
        if content is None:
            raise DcError(f"source file not found: {args.source}")
        if args.section == "VERIFY":
            validate_verify_block(content)
        if args.section == "ROADMAP":
            parse_roadmap(content)

        def apply_set(state: State) -> None:
            state.sections[args.section] = content.strip("\n")

        gen = mutate(state_path, agent, apply_set, expect=args.expect)
        register(root)
        print(f"DC:{args.section} written (generation {gen})")
        return _dcio.EXIT_OK

    if args.log_entry:
        when = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        new_id = log_id(args.log_entry, when)

        moved = {"count": 0}

        def apply_log(state: State) -> None:
            existing = log_lines(state.sections["LOG"])
            existing.append(f"- {when} {new_id} {args.log_entry.strip()}")
            before = len(existing)
            existing = apply_log_cap(agent, existing)
            moved["count"] = max(0, before - len(existing))
            state.sections["LOG"] = "\n".join(existing)

        gen = mutate(state_path, agent, apply_log, expect=args.expect)
        register(root)
        note = f", {moved['count']} archived" if moved["count"] else ""
        print(f"DC:LOG += {new_id} (generation {gen}{note})")
        return _dcio.EXIT_OK

    if args.purge_check:
        state = load(state_path)
        verdict, human = purge_check(state, root, args.context_high)
        print(verdict)
        print(human)
        return _dcio.EXIT_OK

    if args.promote_to_agents:
        return promote(load(state_path), root, args.select, args.dest, args.confirm)

    if args.show:
        # Absence is the fact Gate 2 keys on, and load() falls back to the
        # template, so --show must say which of the two it is printing.
        # Reporting the template's own header rows as though they were saved
        # content is how a missing state file becomes an invisible skipped gate.
        present = _dcio.read_text(state_path) is not None
        state = load(state_path)
        if not present:
            print(f"state: ABSENT ({state_path.name} does not exist); "
                  f"the counts below are the empty template, not saved state")
        else:
            print(f"state: present ({state_path.name})")
        print(f"schema={state.schema} generation={state.generation}")
        for name in SECTIONS:
            body = state.sections[name].strip()
            print(f"DC:{name}: {len(body.splitlines()) if body else 0} line(s)")
        return _dcio.EXIT_OK

    parser.print_help()
    return _dcio.EXIT_OK


if __name__ == "__main__":
    _dcio.run_cli(main)
