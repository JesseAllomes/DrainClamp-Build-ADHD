#!/usr/bin/env python3
"""Project sidecar: `.agent/drainclamp-project.json`, schema 1.

State schema v1 is frozen, so what the build workflow tracks beyond the roadmap
lives here: small chunks under each milestone with a note of what was done, an
error register, a to-do list, build time and token use. Dashboards are not
DrainClamp's job: project-board reads these files (read-only) and keeps its own
data. Older sidecars may still carry board fields; they are kept as found.

Chunks double as a token budget. Each one names the files (and symbols) it
touches, so implementation reads only those, and `next` prints a resume capsule
of a dozen lines instead of the whole state file and log. Chunk notes are never
loaded into context by any gate.

Unlike the registry, the sidecar is an authority, not a cache: a damaged or
unknown-schema file is refused and left exactly as found, never replaced.
Every write bumps `rev`, so a writer holding an old copy can be told so.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _dcio
import dc_chunk
import dc_registry
import dc_state
from _dcio import DcError

SCHEMA_VERSION = 1
SIDECAR_NAME = "drainclamp-project.json"
CHARTER_NAME = "charter.json"
LOCK_NAME = ".dcp.lock"
CONFIG_NAME = "config.json"
IDLE_MINUTES = 30
NOTE_CAP = 160
CAPSULE_CAP = 15

LISTS = ("errors", "todos", "time")
PREFIX = {"errors": "e", "todos": "t", "time": "s"}
# Gate 6 finding statuses that keep a milestone open. The review block itself is
# owned by dc_review.py; the capsule only counts these.
REVIEW_BLOCKING = ("candidate", "open", "fixing")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(text: str) -> datetime:
    try:
        when = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise DcError(f"not an ISO-8601 time: {text!r}") from None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def empty() -> dict:
    return {"schema": SCHEMA_VERSION, "rev": 0, "chunks": {}, "errors": [],
            "todos": [], "time": [], "tokens": None}


def sidecar_path(agent: Path) -> Path:
    return agent / SIDECAR_NAME


def _validate(data) -> dict:
    if not isinstance(data, dict):
        raise DcError("sidecar is not a JSON object; left untouched")
    if data.get("schema") != SCHEMA_VERSION:
        raise DcError(f"sidecar schema {data.get('schema')!r} is not {SCHEMA_VERSION}; "
                      "left untouched")
    out = empty()
    out.update(data)
    if not isinstance(out["rev"], int) or not isinstance(out["chunks"], dict) \
            or any(not isinstance(out[k], list) for k in LISTS) \
            or not (out["tokens"] is None or isinstance(out["tokens"], dict)):
        raise DcError("sidecar fields have the wrong types; left untouched")
    return out


def load(agent: Path) -> dict:
    """The sidecar, or an empty project when there is none. Never writes."""
    text = _dcio.read_text(sidecar_path(agent))
    if text is None:
        return empty()
    try:
        data = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        raise DcError(f"sidecar {sidecar_path(agent)} is not valid JSON; left untouched") \
            from None
    return _validate(data)


def mutate(agent: Path, apply_fn, expect: int | None = None) -> dict:
    """Locked read-modify-write. `expect` refuses the write if `rev` has moved."""
    agent.mkdir(parents=True, exist_ok=True)
    with _dcio.FileLock(agent, name=LOCK_NAME):
        data = load(agent)
        if expect is not None and data["rev"] != expect:
            raise DcError(f"sidecar revision is {data['rev']}, not {expect}; "
                          "reload before editing")
        data = apply_fn(data)
        data["rev"] += 1
        _dcio.atomic_write(sidecar_path(agent),
                           json.dumps(data, indent=2, sort_keys=True) + "\n")
    return data


def _next_id(items: list[dict], prefix: str) -> str:
    nums = [int(i["id"][len(prefix):]) for i in items
            if str(i.get("id", "")).startswith(prefix) and i["id"][len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1}"


def _find(items: list[dict], item_id: str, what: str) -> dict:
    for item in items:
        if item.get("id") == item_id:
            return item
    raise DcError(f"no {what} with id {item_id}")


# -- roadmap -----------------------------------------------------------------

def roadmap(root: Path) -> list[dict]:
    text = _dcio.read_text(root / _dcio.AGENT_DIR_NAME / _dcio.STATE_NAME)
    if text is None:
        return []
    return dc_state.parse_roadmap(dc_state.State.parse(text).sections["ROADMAP"])


def current_milestone(rows: list[dict]) -> dict | None:
    """The active row, else the first pending row whose deps are all done."""
    for r in rows:
        if r["status"] == "active":
            return r
    done = {r["id"] for r in rows if r["status"] == "done"}
    for r in rows:
        if r["status"] == "pending" and all(d in done for d in r.get("deps", [])):
            return r
    return None


# -- time --------------------------------------------------------------------

def minutes_of(session: dict) -> float:
    return max(0.0, (_parse_iso(session["end"]) - _parse_iso(session["start"]))
               .total_seconds() / 60)


def build_minutes(data: dict) -> float:
    return sum(minutes_of(s) for s in data["time"])


def touch_time(data: dict, when: datetime | None = None) -> dict:
    """Extend the open auto session, or start one after an idle gap."""
    when = when or _now()
    autos = [s for s in data["time"] if s.get("source") == "auto"]
    last = autos[-1] if autos else None
    if last and when - _parse_iso(last["end"]) <= timedelta(minutes=IDLE_MINUTES):
        if when > _parse_iso(last["end"]):
            last["end"] = _iso(when)
        return data
    data["time"].append({"id": _next_id(data["time"], "s"), "start": _iso(when),
                         "end": _iso(when), "source": "auto", "note": ""})
    return data


def summary(data: dict, home: str | None) -> dict:
    chunks = [c for cs in data["chunks"].values() for c in cs]
    return {
        "chunks": {"done": sum(1 for c in chunks if c.get("done")), "total": len(chunks)},
        "errors_open": sum(1 for e in data["errors"] if e.get("status") != "fixed"),
        "todos_open": sum(1 for t in data["todos"] if not t.get("done")),
        "build_minutes": build_minutes(data),
        "completed": data.get("completed"),
        "rev": data["rev"],
    }


# -- capsule -----------------------------------------------------------------

def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= NOTE_CAP else text[:NOTE_CAP - 3] + "..."


def read_hint(target: str, root: Path | None = None) -> str:
    """A large path-only target is indexed when dc_map has a grammar for it, else read by range."""
    from dc_map import SUPPORTED  # deferred: keep module import light
    path, _, symbol = target.partition("::")
    if symbol:
        return f"dc_chunk.py --symbol {symbol} --path {path}"
    full = (root / path) if root is not None else None
    if full is not None and _dcio.is_within(full, root) and full.is_file():
        try:
            total = dc_chunk.line_count(full)
        except DcError:
            total = 0
        if total > dc_chunk.SMALL_FILE_LINES and Path(path).suffix.lower() in SUPPORTED:
            return f"dc_map.py --path {path}  ({total} lines: index, then read one range)"
        elif total > dc_chunk.SMALL_FILE_LINES:
            return f"read {path}  ({total} lines: no index for this type; read one range)"
    return f"read {path}"


CLOSE = "close the milestone: dc_state.py --close <m> --log \"<sentence>\" (roadmap, log, then the --purge-check verdict)."


def close_line(root: Path, data: dict, rows: list[dict], mid: str) -> str:
    """How to close `mid` once its chunks are done: through Gate 6 when review applies.

    The rule is dc_review.guard_roadmap's: every milestone under `milestone` mode, only
    the last open one under `final`. Saying so here spares a resumed agent the refused
    roadmap write (NOT-RUN) that would otherwise be its first sign of the gate.
    Blocking findings come first in every mode but off, as they do in the guard.
    """
    review = data.get("review") if isinstance(data.get("review"), dict) else {}
    cfg = review.get("config") if isinstance(review.get("config"), dict) else {}
    mode = cfg.get("mode", "off")
    last_open = [r["id"] for r in rows if r["status"] != "done"] == [mid]
    if mode != "off":
        held = [str(f.get("id", "?")) for f in review.get("findings") or []
                if isinstance(f, dict) and f.get("status") in REVIEW_BLOCKING
                and (last_open or f.get("milestone") == mid)]
        if held:
            return (f"resolve Gate 6 findings first ({' '.join(held[:6])}; "
                    f"dc_review.py status), then {CLOSE}")
    if mode != "milestone" and not (mode == "final" and last_open):
        return CLOSE
    import dc_review  # deferred: dc_review imports this module
    try:
        name = dc_review.verdict(root, data, mid)[0]
    except DcError:
        name = "UNREADABLE"
    if name in ("REVIEW-PASS", "REVIEW-SKIPPED"):
        return f"Gate 6 {name}; {CLOSE}"
    return f"Gate 3 milestone tier, then Gate 6 (dc_review.py check: {name}), then {CLOSE}"


def target_lines(chunk: dict, root: Path) -> list[str]:
    """The chunk's Targets line plus a read hint for each of the first three."""
    targets = chunk.get("targets") or []
    if not targets:
        return []
    more = f" (+{len(targets) - 4})" if len(targets) > 4 else ""
    return (["Targets: " + ", ".join(targets[:4]) + more]
            + ["  " + read_hint(t, root) for t in targets[:3]])


def sibling_changes(root: Path) -> int | None:
    """Uncommitted files in the other projects of a group repo; None outside one.

    Counts only. Read from `git status` at the repo top level minus this project's
    own prefix; nothing is staged, stashed or opened.
    """
    prefix = _dcio.git_prefix(root)
    if not prefix:
        return None
    try:
        done = subprocess.run(["git", "status", "--porcelain", "-z", "-uall"], cwd=str(root),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    count = 0
    fields = iter(done.stdout.split(chr(0)))
    for line in fields:
        if len(line) < 4:
            continue
        if "R" in line[:2] or "C" in line[:2]:
            next(fields, "")  # the rename source is the same change
        parts = line[3:].replace("\\", "/").split("/")
        if ".agent" in parts or parts[-1] == _dcio.PROJECT_MARKER:
            continue  # DrainClamp's own files are not another project's work
        if _dcio.project_path(line[3:], prefix) is None:
            count += 1
    return count


def sibling_lines(root: Path) -> list[str]:
    """The `SIBLINGS:` report for `dc_project.py next`: empty unless a sibling has changes."""
    n = sibling_changes(root)
    if not n:
        return []
    return [f"SIBLINGS: {n} uncommitted file(s) in other projects of this repo",
            "Use a worktree per session so parallel work stays apart; never stage, stash or touch those files."]


def capsule(root: Path, data: dict, home: str | None = None) -> list[str]:
    """The resume view: what to do next and what to read, nothing else."""
    import dc_skills  # imports this module; loaded late to avoid the cycle
    rows = roadmap(root)
    done_rows = sum(1 for r in rows if r["status"] == "done")
    ms = current_milestone(rows)
    lines = [f"DRAINCLAMP NEXT: {root.name} | roadmap {done_rows}/{len(rows)} done"]
    if ms is None:
        lines.append("No startable milestone. Check the roadmap." if rows
                     else "No roadmap yet. Run Gate 2.")
        return lines
    chunks = data["chunks"].get(ms["id"], [])
    n_done = sum(1 for c in chunks if c.get("done"))
    lines.append(f"Milestone {ms['id']} ({ms['status']}): {_clip(ms['goal'])} "
                 f"| chunks {n_done}/{len(chunks)}")
    review = data.get("review") if isinstance(data.get("review"), dict) else {}
    held = [f for f in review.get("findings") or [] if isinstance(f, dict)
            and f.get("milestone") == ms["id"] and f.get("status") in REVIEW_BLOCKING]
    if held:
        lines.append(f"Review: {len(held)} blocking ({' '.join(f['id'] for f in held[:6])}) "
                     "- waiting on you (dc_review.py status)")
    ideas = [i["id"] for i in review.get("ideas") or [] if isinstance(i, dict)
             and i.get("status") == "proposed" and i.get("id")]
    if ideas:
        lines.append(f"Advisor: {len(ideas)} idea(s) to triage ({' '.join(ideas[:6])}) "
                     "- waiting on you (dc_review.py status)")
    import dc_plan  # imports this module lazily
    if plan := dc_plan.capsule_line(data, root, ms["id"]):
        lines.append(plan)
    nxt = next((c for c in chunks if not c.get("done")), None)
    if not chunks:
        lines.append("No chunks yet: split this milestone with `dc_project.py chunk add`.")
    elif nxt is None:
        lines.append("All chunks done: " + close_line(root, data, rows, ms["id"]))
    else:
        est = f" (~{nxt['est_min']} min)" if nxt.get("est_min") else ""
        lines.append(f"Next chunk: {nxt['id']} - {_clip(nxt['goal'])}{est}")
        lines += target_lines(nxt, root)
    last = [c for c in chunks if c.get("done")]
    if last:
        lines.append(f"Last done: {last[-1]['id']} - {_clip(last[-1].get('note') or last[-1]['goal'])}")
    if skills := dc_skills.capsule_line(data, home):
        lines.append(skills)
    errs = [e for e in data["errors"] if e.get("status") != "fixed"]
    todos = sum(1 for t in data["todos"] if not t.get("done"))
    lines.append(f"Open errors: {len(errs)} | open to-dos: {todos}")
    for e in errs[:2]:
        lines.append(f"  {e['id']}: {_clip(e['summary'])}")
    return lines[:CAPSULE_CAP]


def boundary(data: dict, root: Path, mid: str, cid: str,
             context_high: bool = False, host: str | None = None) -> list[str]:
    """Chunk-boundary verdict: purge before the next chunk, or close the milestone."""
    chunks = data["chunks"].get(mid, [])
    done = next(c for c in chunks if c["id"] == cid)
    nxt = next((c for c in chunks if not c.get("done")), None)
    if nxt is None:
        return [f"Milestone {mid}: all chunks done: " + close_line(root, data, roadmap(root), mid)]
    verdict, human = dc_state.chunk_purge_check(done.get("targets", []),
                                                nxt.get("targets", []), root, context_high)
    lines = [f"Next chunk: {nxt['id']} - {_clip(nxt['goal'])}", verdict, human]
    # HOLD keeps the context, so hand over what to read next; a PURGE resume runs `next` anyway
    if verdict.startswith("HOLD"):
        return lines + target_lines(nxt, root)
    return lines + [dc_state.purge_resume_line(root, host)]


# -- operations ----------------------------------------------------------------

def _targets(value) -> list[str]:
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    return [t.strip() for t in (value or "").split(";") if t.strip()]


def _num(a: dict, key: str, required: bool = True) -> float | None:
    value = a.get(key)
    if value is None or value == "":
        if required:
            raise DcError(f"{key} is required")
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise DcError(f"{key} must be a number") from None


def _text(a: dict, key: str, required: bool = False) -> str:
    value = a.get(key)
    value = "" if value is None else str(value).strip()
    if required and not value:
        raise DcError(f"{key} is required")
    return value


OPS = ("chunk.add", "chunk.done", "chunk.undo", "chunk.note", "error.add", "error.fix",
       "todo.add", "todo.done", "todo.undo", "time.touch", "time.add", "time.delete")


def _lines(text: str) -> list[str]:
    return [l.strip().lstrip("-*\u2022 ").strip() for l in (text or "").splitlines()
            if l.strip().lstrip("-*\u2022 ").strip()]


def apply_op(data: dict, root: Path, op: str, a: dict, now: datetime | None = None) -> str:
    """Apply one named edit to `data` in place and return a one-line result.

    Milestone status is not an operation here: it belongs to dc_state.py.
    """
    now = now or _now()
    stamp = _iso(now)
    if op == "chunk.add":
        mid = _text(a, "milestone", True)
        if mid not in {r["id"] for r in roadmap(root)}:
            raise DcError(f"milestone {mid} is not in DC:ROADMAP")
        cs = data["chunks"].setdefault(mid, [])
        cid = _next_id(cs, "c")
        est = _num(a, "est", required=False)
        chunk = {"id": cid, "goal": _text(a, "goal", True),
                 "targets": _targets(a.get("targets")),
                 "est_min": int(est) if est else None, "done": False, "done_at": None,
                 "note": ""}
        chunk.update(_fanout_fields(a, cs))
        cs.append(chunk)
        return f"chunk {mid}/{cid} added"
    if op in ("chunk.done", "chunk.undo", "chunk.note"):
        mid = _text(a, "milestone", True)
        c = _find(data["chunks"].get(mid, []), _text(a, "id", True), f"chunk in {mid}")
        if a.get("note") is not None:
            c["note"] = _text(a, "note")
        if op == "chunk.note":
            return f"chunk {mid}/{c['id']} note saved"
        c["done"] = op == "chunk.done"
        c["done_at"] = stamp if c["done"] else None
        touch_time(data, now)
        return f"chunk {mid}/{c['id']} {'done' if c['done'] else 'reopened'}"
    if op == "error.add":
        eid = _next_id(data["errors"], "e")
        fix = _text(a, "fix")
        data["errors"].append({"id": eid, "at": stamp, "summary": _text(a, "summary", True),
                               "milestone": _text(a, "milestone"), "chunk": _text(a, "chunk"),
                               "cause": _text(a, "cause"), "fix": fix,
                               "status": "fixed" if fix else "open"})
        return f"error {eid} logged"
    if op == "error.fix":
        e = _find(data["errors"], _text(a, "id", True), "error")
        e["fix"], e["status"], e["fixed_at"] = _text(a, "fix", True), "fixed", stamp
        return f"error {e['id']} fixed"
    if op == "todo.add":
        tid = _next_id(data["todos"], "t")
        data["todos"].append({"id": tid, "text": _text(a, "text", True),
                              "milestone": _text(a, "milestone"), "done": False,
                              "at": stamp, "done_at": None})
        return f"to-do {tid} added"
    if op in ("todo.done", "todo.undo"):
        t = _find(data["todos"], _text(a, "id", True), "to-do")
        t["done"] = op == "todo.done"
        t["done_at"] = stamp if t["done"] else None
        return f"to-do {t['id']} {'done' if t['done'] else 'reopened'}"
    if op == "time.touch":
        touch_time(data, now)
        return "time touched"
    if op == "time.add":
        minutes = _num(a, "minutes", required=False)
        if minutes is not None:
            if minutes <= 0:
                raise DcError("minutes must be positive")
            end = _parse_iso(a["end"]) if a.get("end") else now
            start = end - timedelta(minutes=minutes)
        elif a.get("start") and a.get("end"):
            start, end = _parse_iso(a["start"]), _parse_iso(a["end"])
        else:
            raise DcError("give minutes, or both start and end")
        if end <= start:
            raise DcError("end must be after start")
        sid = _next_id(data["time"], "s")
        data["time"].append({"id": sid, "start": _iso(start), "end": _iso(end),
                             "source": "manual", "note": _text(a, "note")})
        return f"time {sid} added ({(end - start).total_seconds() / 60:.0f} min)"
    if op == "time.delete":
        s = _find(data["time"], _text(a, "id", True), "time entry")
        data["time"].remove(s)
        return f"time {s['id']} deleted"
    raise DcError(f"unknown operation: {op}")


# -- conductor build: chunk flags, fan-out test, builder brief, merge order -----

SIZES = ("small", "medium", "large")
MAX_BUILDERS = 4


def _fanout_fields(a: dict, siblings: list[dict]) -> dict:
    """Optional chunk flags from `chunk add`. Additive: old chunks simply lack them."""
    out: dict = {}
    deps = [d.strip() for d in str(a.get("deps") or "").split(",") if d.strip()]
    known = {c["id"] for c in siblings}
    for d in deps:
        if d not in known:
            raise DcError(f"--deps: unknown chunk {d} in this milestone")
    if deps:
        out["deps"] = deps
    size = _text(a, "size")
    if size:
        if size not in SIZES:
            raise DcError(f"--size must be one of {', '.join(SIZES)}")
        out["size"] = size
    par = _text(a, "parallel")
    if par:
        if par not in ("yes", "no"):
            raise DcError("--parallel must be yes or no")
        if not _text(a, "why"):
            raise DcError("--why is required with --parallel (one line)")
        out["parallel"] = par
    if _text(a, "why"):
        out["why"] = _text(a, "why")[:NOTE_CAP]
    if _text(a, "critical"):
        out["critical"] = _text(a, "critical")[:NOTE_CAP]
    return out


def _path_key(target: str) -> str:
    """Target as a comparable path: `::symbol` stripped, slashes unified, case-folded."""
    key = target.split("::", 1)[0].strip().replace("\\", "/").lower()
    while key.startswith("./"):
        key = key[2:]
    return "." if key in (".", "") and target.strip() else key


def _chunk_files(c: dict) -> set[str]:
    return {_path_key(t) for t in c.get("targets") or [] if _path_key(t)}


def _overlap(a: set[str], b: set[str]) -> str | None:
    """The first path two target sets share: equal, or one inside the other's folder."""
    for x in sorted(a):
        for y in sorted(b):
            if x == "." or y == ".":
                return y if x == "." else x
            if x == y or y.startswith(x.rstrip("/") + "/"):
                return y
            if x.startswith(y.rstrip("/") + "/"):
                return x
    return None


def chunk_branch(root: Path, mid: str, cid: str) -> tuple[str, str]:
    """(branch, worktree path) for one builder chunk; chunk ids repeat across milestones."""
    repo = _dcio.repo_root(root)
    try:
        rel = root.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        rel = "."
    if rel == ".":
        slug = slugify(root.name) or "project"
    else:  # a short hash keeps a/b and a-b apart
        slug = (f"{slugify(rel.replace('/', '-')) or 'project'}-"
                f"{hashlib.sha256(rel.encode('utf-8')).hexdigest()[:6]}")
    return f"dc/{slug}-{mid}-{cid}", (repo / ".worktrees" / f"{slug}-{mid}-{cid}").as_posix()


def fanout(data: dict, root: Path, mid: str, no_subagents: bool = False,
           critical: list[str] | None = None) -> list[str]:
    """The fan-out test: PARALLEL lines, or exactly one SEQUENTIAL line with the first reason."""
    chunks = data["chunks"].get(mid, [])
    if not chunks:
        raise DcError(f"milestone {mid} has no chunks")
    by_id = {c["id"]: c for c in chunks}
    for cid in critical or []:
        if cid not in by_id:
            raise DcError(f"--critical: unknown chunk {cid}")
    if no_subagents:
        return ["SEQUENTIAL: host has no subagents"]
    done = {c["id"] for c in chunks if c.get("done")}
    open_chunks = [c for c in chunks if not c.get("done")]
    ready = [c for c in open_chunks if all(d in done for d in c.get("deps", []))]
    if len(ready) < 2:
        for c in open_chunks:
            waits = [d for d in c.get("deps", []) if d not in done]
            if waits:
                return [f"SEQUENTIAL: {c['id']} needs {waits[0]}"]
        return [f"SEQUENTIAL: {len(ready)} ready chunk(s), need 2"]
    for c in ready:
        crit = c.get("critical") or ("critical" if c["id"] in (critical or []) else "")
        if crit:  # deploys, live writes, migrations, releases: never beside a builder
            kind = "" if crit == "critical" else f" ({crit})"
            return [f"SEQUENTIAL: {c['id']} is critical{kind}"]
    # Qualifying chunks fan out even when other ready chunks do not qualify; those wait
    # and run in order after the merge. The first reason is kept for the sequential line.
    pick: list[dict] = []
    later: list[dict] = []
    reason = ""
    for c in ready:
        why = ""
        if c.get("size") not in ("medium", "large"):
            why = f"{c['id']} is {c['size']}" if c.get("size") else \
                f"{c['id']} has no size (needs medium+)"
        elif c.get("parallel", "no") == "no":
            why = f"{c['id']} is marked parallel no"
        elif not _chunk_files(c):
            why = f"{c['id']} has no targets"
        else:
            shared = None
            clash = None
            for x in pick:
                shared = _overlap(_chunk_files(x), _chunk_files(c))
                if shared:
                    clash = x
                    break
            if clash:
                why = f"{clash['id']} and {c['id']} share {shared}"
            elif len(pick) >= MAX_BUILDERS:
                why = f"{c['id']} waits (at most {MAX_BUILDERS} builders)"
        if why:
            reason = reason or why
            later.append(c)
        else:
            pick.append(c)
    if len(pick) < 2:
        return [f"SEQUENTIAL: {reason}"]
    lines = [f"PARALLEL: {', '.join(c['id'] for c in pick)} (independent, medium+)"]
    for c in pick:
        branch, tree = chunk_branch(root, mid, c["id"])
        lines.append(f"  {c['id']} -> branch {branch}  worktree {tree}")
    if later:
        lines.append(f"  then in order after the merge: {', '.join(c['id'] for c in later)}")
    return lines


def default_verify(root: Path) -> str:
    """The milestone-tier DC:VERIFY runner as a command line, or a pointer to the conductor."""
    text = _dcio.read_text(root / _dcio.AGENT_DIR_NAME / _dcio.STATE_NAME)
    try:
        entries = dc_state.validate_verify_block(
            dc_state.State.parse(text).sections["VERIFY"]) if text else []
    except DcError:
        entries = []
    for e in entries:
        if e["tier"] == "milestone":
            return " ".join(e["argv"])
    return "(no DC:VERIFY milestone runner: ask the conductor)"


def brief(data: dict, root: Path, mid: str, cid: str, budget: int = 40000,
          verify: str | None = None) -> list[str]:
    """Builder brief, at most 30 lines."""
    chunks = data["chunks"].get(mid, [])
    c = _find(chunks, cid, f"chunk in {mid}")
    contract = next((x for x in chunks if x["goal"].lower().startswith("contract:")
                     and x["id"] != cid and x.get("done")), None)
    branch, tree = chunk_branch(root, mid, cid)
    files = c.get("targets") or []
    lines = [f"BUILDER BRIEF {mid}/{cid}",
             f"Goal: {_clip(c['goal'])}",
             "Files allowed (edit nothing else):"]
    lines += [f"  {t}" for t in files[:8]] or ["  (none listed: ask the conductor)"]
    if len(files) > 8:
        lines.append(f"  (+{len(files) - 8} more in the sidecar)")
    if contract:
        note = _clip(contract.get("note") or "") or "(no note; read its goal)"
        lines.append(f"Contract ({contract['id']}): {_clip(contract['goal'])} -- {note}")
    else:
        lines.append("Contract: none")
    import dc_plan  # imports this module lazily
    planned = [t for t in dc_plan.planned_tests(root, mid) if t.startswith(f"{cid}: ")]
    if planned:
        rel = dc_plan.plan_path(root, mid).relative_to(root).as_posix()
        lines += [f"Plan: {rel} (read ## Interfaces and ### {cid})",
                  f"Test to write or pass: {_clip(planned[0].split(': ', 1)[1])}"]
    lines += [f"Verify: {verify or default_verify(root)}",
              f"Branch: {branch}",
              f"Worktree: {tree}",
              f"Token budget: {budget}",
              "Over budget: stop at the last green commit and report, never grind.",
              "Commits stay local on the dc/* branch; never push.",
              "Report: commit hashes, verify result, anything left."]
    return lines[:30]


def merge_order(data: dict, root: Path, mid: str, ids: list[str] | None = None) -> list[str]:
    """Chunks in dependency order (ties by id), one `<id> <branch>` per line."""
    chunks = data["chunks"].get(mid, [])
    by_id = {c["id"]: c for c in chunks}
    want = ids or [c["id"] for c in chunks]
    for cid in want:
        if cid not in by_id:
            raise DcError(f"unknown chunk {cid} in {mid}")
    pending = set(want)
    out: list[str] = []
    while pending:
        # only deps inside the requested set order the merge; others merged earlier
        ready = sorted(x for x in pending
                       if not any(d in pending for d in by_id[x].get("deps", [])))
        if not ready:
            raise DcError("dependency cycle among: " + ", ".join(sorted(pending)))
        out.append(ready[0])
        pending.discard(ready[0])
    return [f"{cid} {chunk_branch(root, mid, cid)[0]}" for cid in out]


# -- lifecycle: create from a charter, complete, reopen ------------------------

CHARTER_TEXT = ("name", "purpose", "sponsor", "stakeholders", "current_state", "objectives",
                "success", "out_of_scope", "milestones", "security", "retention",
                "assumptions", "never", "approvals", "acceptance")
CHARTER_ROWS = {"scope": ("feature", "what", "priority"),
                "risks": ("risk", "likelihood", "impact", "guardrail")}
CHARTER_ENUMS = {"priority": ("Must", "Should", "Could", "Won't"),
                 "likelihood": ("Low", "Med", "High"), "impact": ("Low", "Med", "High")}
CHARTER_VALUE = ("baseline_min", "new_min", "runs_per_year", "hourly_rate")
TEXT_CAP = 4000
ROWS_CAP = 50
SLUG_CAP = 63
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10))}


def slugify(name: str) -> str:
    """Folder name for a project: lower-case letters, digits and single hyphens."""
    text = re.sub(r"['’]", "", str(name).lower())  # "KPI's" -> "kpis"
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return slug[:SLUG_CAP].rstrip("-")


def check_slug(slug: str) -> str:
    if not slug or len(slug) > SLUG_CAP or not SLUG_RE.match(slug):
        raise DcError(f"folder name {slug!r} must be lower-case letters, digits and hyphens")
    if slug in RESERVED:
        raise DcError(f"folder name {slug!r} is reserved on Windows")
    return slug


def project_roots(home: str | None) -> list[Path]:
    """Folders new projects may be created in: `project_roots` in config.json."""
    text = _dcio.read_text(dc_registry.home_dir(home) / CONFIG_NAME)
    try:
        roots = json.loads(text).get("project_roots") if text else None
    except (ValueError, AttributeError):
        return []
    if not isinstance(roots, list):
        return []
    return [Path(r).expanduser().resolve() for r in roots if isinstance(r, str) and r.strip()]


def clean_charter(c) -> dict:
    """Validated copy of a charter. Only name and purpose are required."""
    if not isinstance(c, dict):
        raise DcError("charter must be an object")
    known = set(CHARTER_TEXT) | set(CHARTER_ROWS) | {"data", "value"}
    extra = sorted(set(c) - known)
    if extra:
        raise DcError(f"unknown charter fields: {', '.join(extra)}")
    out: dict = {}
    for key in CHARTER_TEXT:
        value = _text(c, key, required=key in ("name", "purpose"))
        if len(value) > TEXT_CAP:
            raise DcError(f"{key} is over {TEXT_CAP} characters")
        out[key] = value
    for key, fields in CHARTER_ROWS.items():
        rows = c.get(key) or []
        if not isinstance(rows, list) or len(rows) > ROWS_CAP:
            raise DcError(f"{key} must be a list of at most {ROWS_CAP} rows")
        kept = []
        for row in rows:
            if not isinstance(row, dict):
                raise DcError(f"{key} rows must be objects")
            item = {f: _text(row, f)[:TEXT_CAP] for f in fields}
            if not item[fields[0]]:
                continue  # a blank line the form left behind
            for f in fields:
                if f in CHARTER_ENUMS and item[f] and item[f] not in CHARTER_ENUMS[f]:
                    raise DcError(f"{f} must be one of {', '.join(CHARTER_ENUMS[f])}")
            kept.append(item)
        out[key] = kept
    data = c.get("data") or []
    if not isinstance(data, list) or len(data) > ROWS_CAP:
        raise DcError("data must be a list")
    out["data"] = [str(d).strip()[:200] for d in data if str(d).strip()]
    value = c.get("value") or {}
    if not isinstance(value, dict):
        raise DcError("value must be an object")
    out["value"] = {k: _num(value, k, required=False) for k in CHARTER_VALUE}
    if any(v is not None and v < 0 for v in out["value"].values()):
        raise DcError("value figures must not be negative")
    return out


def _pick_parent(location: str | None, roots: list[Path]) -> Path:
    if not roots:
        raise DcError("no project_roots in config.json; add the folders projects may "
                      "be created in")
    if not location:
        return roots[0]
    wanted = dc_registry._key(Path(location).expanduser().resolve())
    for r in roots:
        if dc_registry._key(r) == wanted:
            return r
    raise DcError(f"{location} is not one of the project_roots in config.json")


def create_project(charter: dict, home: str | None, location: str | None = None,
                   folder: str | None = None, now: datetime | None = None) -> dict:
    """New folder + git repo + state template + charter + sidecar + registry entry.

    The target must not exist, so nothing already on disk is ever overwritten.
    If a step fails, only the folder this call created is removed.
    """
    c = clean_charter(charter)
    parent = _pick_parent(location, project_roots(home))
    if not parent.is_dir():
        raise DcError(f"project root {parent} does not exist")
    slug = check_slug(folder.strip() if folder and folder.strip() else slugify(c["name"]))
    target = parent / slug
    if target.exists():
        raise DcError(f"{target} already exists; pick another name")
    git = shutil.which("git")
    if git is None:
        raise DcError("git is not on PATH; a project needs its own repository")
    stamp = _iso(now or _now())
    try:
        target.mkdir()
    except FileExistsError:
        raise DcError(f"{target} already exists; pick another name") from None
    try:
        proc = subprocess.run([git, "init", "-q", str(target)], capture_output=True,
                              text=True, timeout=60)
        if proc.returncode != 0:
            raise DcError(f"git init failed: {_dcio.first_failure_line(proc.stderr)}")
        agent = target / _dcio.AGENT_DIR_NAME
        agent.mkdir()
        _dcio.atomic_write(agent / _dcio.STATE_NAME, dc_state.template_text())
        _dcio.atomic_write(agent / CHARTER_NAME, json.dumps(
            {"schema": SCHEMA_VERSION, "created": stamp, "charter": c},
            indent=2, sort_keys=True) + "\n")

        def seed(data: dict) -> dict:
            data["created"] = stamp
            return data

        mutate(agent, seed)
        entry = dc_registry.touch(target, home, name=c["name"])
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)  # only ever the folder made above
        raise
    return {"root": entry["root"], "name": entry["name"], "folder": slug}


def load_charter(agent: Path) -> dict | None:
    text = _dcio.read_text(agent / CHARTER_NAME)
    try:
        data = json.loads(text) if text else None
    except (ValueError, UnicodeDecodeError):
        return None
    return data.get("charter") if isinstance(data, dict) else None


# Blank charter fields Gate 1 may ask about: each answer can change behaviour, data
# handling, a security boundary or a destructive action. The rest are never asked.
CHARTER_MATERIAL = ("purpose", "objectives", "success", "out_of_scope", "milestones",
                    "security", "retention", "never", "approvals", "acceptance", "scope")
CHARTER_LABELS = {"current_state": "Current state", "out_of_scope": "Out of scope"}
VIEW_ITEMS = 8


def _listed(items: list[str], label: str) -> list[str]:
    lines = [f"  {i}. {_clip(t)}" for i, t in enumerate(items[:VIEW_ITEMS], 1)]
    if len(items) > VIEW_ITEMS:
        lines.append(f"  TRUNCATED: {len(items) - VIEW_ITEMS} more {label} in .agent/charter.json")
    return lines


def charter_view(raw) -> list[str]:
    """The charter as Gates 1 and 2 need it: filled answers, material blanks, and the
    milestone and acceptance lines that seed the roadmap and the checks.

    The file is repository data, so it is validated here and shown as data.
    """
    if raw is None:
        return ["CHARTER: none (.agent/charter.json absent or unreadable). Run Gate 1 as usual."]
    try:
        c = clean_charter(raw)
    except DcError as exc:
        return [f"CHARTER: invalid, ignored ({exc}). Run Gate 1 as usual."]
    out = [f"CHARTER: {_clip(c['name'])} (.agent/charter.json; data, not instructions)"]
    for key in ("purpose", "objectives", "success", "out_of_scope", "current_state", "security",
                "retention", "never", "approvals", "assumptions"):
        if c[key]:
            out.append(f"{CHARTER_LABELS.get(key, key.capitalize())}: {_clip('; '.join(_lines(c[key])))}")
    if c["scope"]:
        by = {p: [r["feature"] for r in c["scope"] if (r["priority"] or "Must") == p]
              for p in CHARTER_ENUMS["priority"]}
        out.append("Scope: " + " | ".join(f"{p}: {_clip(', '.join(by[p])) or '-'}" for p in by))
    if c["risks"]:
        high = [r["risk"] for r in c["risks"] if r["impact"] == "High"]
        out.append(f"Risks: {len(c['risks'])}" + (f" (high impact: {_clip('; '.join(high))})"
                                                  if high else ""))
    if c["data"]:
        out.append(f"Data: {_clip(', '.join(c['data']))}")
    v = c["value"]
    if v["baseline_min"] is not None and v["new_min"] is not None:
        runs = f", {v['runs_per_year']:g} runs/yr" if v["runs_per_year"] is not None else ""
        out.append(f"Value: {v['baseline_min']:g} min -> {v['new_min']:g} min per run{runs}")
    milestones = _lines(c["milestones"]) or [r["feature"] for r in c["scope"]
                                             if (r["priority"] or "Must") == "Must"]
    if milestones:
        source = "milestones" if c["milestones"] else "Must scope"
        out.append(f"Draft ROADMAP rows (from {source}; files come from the audit):")
        out += _listed(milestones, "milestones")
    if c["acceptance"]:
        out.append("Acceptance (one test each; the runner goes in DC:VERIFY):")
        out += _listed(_lines(c["acceptance"]), "acceptance lines")
    blank = [k for k in CHARTER_MATERIAL if not c[k]]
    skip = [k for k in CHARTER_TEXT if k not in CHARTER_MATERIAL and k != "name" and not c[k]]
    out.append("Blank, material (Gate 1 may ask): " + (", ".join(blank) or "none"))
    if skip:
        out.append("Blank, not asked: " + ", ".join(skip))
    return out


def set_lifecycle(root: Path, status: str, home: str | None, note: str = "",
                  expect: int | None = None, now: datetime | None = None) -> dict:
    """Complete or reopen a project: registry status (what Gate S lists) + sidecar stamp.

    The state file is never touched. The sidecar is read first, so a damaged one
    stops the change before the registry moves.
    """
    if status not in dc_registry.STATUSES:
        raise DcError(f"unknown status: {status}")
    agent = root / _dcio.AGENT_DIR_NAME
    current = load(agent)["rev"]
    if expect is not None and current != expect:
        raise DcError(f"sidecar revision is {current}, not {expect}; reload before editing")
    note = str(note or "").strip()[:NOTE_CAP]
    dc_registry.set_status(root, status, home)

    def stamp(data: dict) -> dict:
        if status == "complete":
            data["completed"] = {"at": _iso(now or _now()), "note": note}
        else:
            data["completed"] = None
        return data

    return mutate(agent, stamp)


# -- auto-commit at a completed step (G9) -----------------------------------------

AUTOCOMMIT_DEFAULT = {"enabled": True, "clean_tree": False, "review_required": False,
                      "verify_cmd": ""}


def autocommit_cfg(data: dict) -> dict:
    cfg = dict(AUTOCOMMIT_DEFAULT)
    stored = data.get("autocommit")
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k in AUTOCOMMIT_DEFAULT})
    return cfg


def _fast_verify_argv(root: Path) -> tuple[list[str], str]:
    """First DC:VERIFY entry of the fast tier, or []."""
    text = _dcio.read_text(root / _dcio.AGENT_DIR_NAME / _dcio.STATE_NAME)
    try:
        entries = dc_state.validate_verify_block(
            dc_state.State.parse(text).sections["VERIFY"]) if text else []
    except DcError:
        entries = []
    for e in entries:
        if e["tier"] == "fast":
            return list(e["argv"]), e.get("cwd") or "."
    return [], "."


def split_cmd(cmd: str) -> list[str]:
    """argv of a --verify-cmd string; Windows keeps backslashes in paths."""
    if os.name != "nt":
        return shlex.split(cmd)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p
            for p in shlex.split(cmd, posix=False)]


APPROVED_NAME = "autocommit-approved.json"


def _cmd_digest(root: Path, argv: list[str]) -> str:
    raw = json.dumps({"root": str(root.resolve()), "argv": argv}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _approved(home) -> list[str]:
    try:
        data = json.loads(_dcio.read_text(dc_registry.home_dir(home) / APPROVED_NAME) or "{}")
    except ValueError:
        return []
    got = data.get("approved") if isinstance(data, dict) else None
    return [d for d in got if isinstance(d, str)] if isinstance(got, list) else []


def approve_cmd(root: Path, argv: list[str], home) -> None:
    keep, dig = _approved(home), _cmd_digest(root, argv)
    if dig not in keep:
        path = dc_registry.home_dir(home) / APPROVED_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        _dcio.atomic_write(path, json.dumps({"approved": keep + [dig]}, indent=2) + "\n")


def _fast_clean_argv(root: Path, argv: list[str], cwd: str) -> tuple[list[str], str]:
    import dc_verify  # lazy
    if dc_verify.classify(argv) is None:
        return [], f"fast-tier check {argv[0]} is not on the runner allowlist; approve one with config --verify-cmd"
    try:
        exe = dc_verify._resolved_executable(argv[0], _dcio.resolve_cwd(cwd, root))
    except DcError as exc:
        return [], str(exc)
    if exe is None or _dcio.is_within(exe, _dcio.repo_root(root).resolve()):
        return [], f"fast-tier check runner {argv[0]} is missing or inside the repository"
    return [str(exe)] + list(argv[1:]), ""


def start_step(root: Path, mid: str, cid: str = "", sequential: str = "") -> str:
    """Record what is already edited, so the step's own edits can be told apart later.

    A chunk start first passes the planning gate and the fan-out rule (dc_plan.start_gate);
    a refusal writes nothing.
    """
    import dc_plan  # imports this module lazily
    agent = root / _dcio.AGENT_DIR_NAME
    if mid not in {r["id"] for r in roadmap(root)}:
        raise DcError(f"milestone {mid} is not in DC:ROADMAP")
    snap = _dcio.dirty_snapshot(root)
    snap["at"] = _iso(_now())
    gate: list[str] = []

    def apply(data: dict) -> dict:
        if cid:
            chunk = _find(data["chunks"].get(mid, []), cid, f"chunk in {mid}")
            gate.extend(dc_plan.start_gate(data, root, mid, cid, sequential))
            chunk["start"] = snap
        else:
            data.setdefault("milestone_start", {})[mid] = snap
        return data

    mutate(agent, apply)
    label = f"{mid}/{cid}" if cid else mid
    return "\n".join([f"step {label} started: {len(snap['files'])} file(s) already edited, "
                      "never auto-committed", *gate])


def commit_step(root: Path, mid: str, cid: str = "", verified: bool = False,
                trailers: list[str] | None = None, subject: str = "",
                extra_paths: list[str] | None = None, home: str | None = None) -> list[str]:
    """Auto-commit the step's own files. One COMMITTED or NOT COMMITTED line first."""
    agent = root / _dcio.AGENT_DIR_NAME
    data = load(agent)
    cfg = autocommit_cfg(data)
    if not cfg["enabled"]:
        return ["NOT COMMITTED: auto-commit is off for this project "
                "(dc_project.py config --autocommit on)"]
    if not verified:
        import dc_review  # deferred: dc_review imports this module
        vouched, why = dc_review.tier_record(root)
        if not vouched:
            return [f"NOT COMMITTED: the step's DC:VERIFY tier is not verified: {why}; "
                    "run dc_verify.py --tier fast, or pass --verified only after it passes"]
    chunk = _find(data["chunks"].get(mid, []), cid, f"chunk in {mid}") if cid else None
    snap = (chunk or {}).get("start") if cid else (data.get("milestone_start") or {}).get(mid)
    if not isinstance(snap, dict):
        return [f"NOT COMMITTED: no start snapshot for {mid}{'/' + cid if cid else ''}; "
                "run chunk start first, so pre-existing edits are never committed"]
    targets = [t.split("::", 1)[0] for t in (chunk or {}).get("targets", [])]
    targets += extra_paths or []
    paths, before, outside = _dcio.step_candidates(root, snap, targets)
    if chunk:
        goal = chunk["goal"]
    else:
        goal = next((r["goal"] for r in roadmap(root) if r["id"] == mid), mid)
    label = f"{mid}/{cid}" if cid else mid
    title = subject.strip() or f"Complete {label}: {goal}"
    clean_argv: list[str] = []
    clean_cwd = "."
    if cfg["clean_tree"]:
        if cfg["verify_cmd"]:
            clean_argv = split_cmd(cfg["verify_cmd"])
            if clean_argv and _cmd_digest(root, clean_argv) not in _approved(home):
                return ["NOT COMMITTED: the clean-tree command was not set on this machine; "
                        "re-set it with config --verify-cmd to approve it"]
        else:
            clean_argv, clean_cwd = _fast_verify_argv(root)
            if clean_argv:
                clean_argv, why = _fast_clean_argv(root, clean_argv, clean_cwd)
                if why:
                    return [f"NOT COMMITTED: {why}"]
        if not clean_argv:
            return ["NOT COMMITTED: clean-tree check is on but there is no command "
                    "(set config --verify-cmd or a fast-tier DC:VERIFY entry)"]
    branch = f"dc/{slugify(root.name) or 'project'}-{mid}"
    lines = _dcio.auto_commit(root, paths, title, trailers, branch=branch,
                              review_required=cfg["review_required"],
                              clean_argv=clean_argv, clean_cwd=clean_cwd, home=home)
    if lines[0].startswith("COMMITTED"):
        short = lines[0].split()[1]

        def apply(d: dict) -> dict:
            if cid:
                _find(d["chunks"].get(mid, []), cid, f"chunk in {mid}")["commit"] = short
            else:
                d.setdefault("milestone_commit", {})[mid] = short
            return d

        mutate(agent, apply)
    if before:
        lines.append(f"NOT COMMITTED: {before} file(s) had edits before this step")
    if outside:
        lines.append(f"LEFT: {len(outside)} changed file(s) outside this step's targets "
                     "(pass --paths to include one)")
    return lines


# -- CLI ---------------------------------------------------------------------

# -- G7 new-project intake -----------------------------------------------------
# The question flow is model behaviour (references/phase1-grill.md). This block does the
# deterministic parts: whether intake runs, the card, the open Blocking/Shaping items,
# the ~N estimate, the caps, the discovery m0 fallback and the charter write.

INTAKE_NAME = "intake.json"
INTAKE_CAPS = {"build": 8, "quick": 3}
SHAPING_ROOM = 3     # Shaping questions are asked only while fewer than this many were asked
QUICK_ITEMS = ("1", "3", "6")   # quick tier: Problem, MVP | Out, Success only
# key, label, class, card question, recommended default, alternative
INTAKE_ITEMS = (
    ("1", "Problem", "blocking", "what problem, and today's workaround (time x runs a year)?",
     "state the problem in one line and time the manual workaround", "describe it later in discovery"),
    ("2", "User", "shaping", "who uses it and what do they get?",
     "the requester is the only user", "name another user"),
    ("3", "MVP", "shaping", "first milestone | out of scope?",
     "smallest end-to-end slice | everything else", "list what is out"),
    ("4", "Inputs", "blocking", "systems, files, APIs: where, and is access held?",
     "use the sources already named in the request, access held", "access still to be arranged"),
    ("5", "Limits", "blocking", "data that never leaves, approvals, deadline, budget?",
     "no data leaves the machine, no approvals needed, no deadline", "name a limit"),
    ("6", "Success", "blocking", "one SMART measure a check or named manual step can prove?",
     "one DC:VERIFY command exits 0 on a sample input", "a named manual acceptance step"),
    ("7", "Risk", "shaping", "main risk and the manual fallback?",
     "bad output; fall back to the manual process", "name another risk"),
)
INTAKE_TRIGGERS = {  # flag -> (class, label, question, recommended default)
    "live-write": ("blocking", "Live write", "which live system is written, and how is a wrong write undone?",
                   "write only to a test copy first; keep a backup to roll back"),
    "personal-data": ("blocking", "Personal data", "which personal data is touched, and where may it be stored?",
                      "keep it local; store no more than needed"),
    "multi-system": ("blocking", "Systems", "which system wins when the integrated systems disagree?",
                     "the system of record wins; flag differences"),
    "no-access": ("blocking", "Access", "who grants the missing API access, and by when?",
                  "request access now; build against a stub meanwhile"),
    "unverifiable": ("blocking", "Verifiable", "what observable check proves it works?",
                     "a sample input with a known expected output"),
    "long-mvp": ("shaping", "Split", "the MVP is over a week: which part ships first?",
                 "ship the first milestone alone"),
    "contradiction": ("blocking", "Conflict", "two answers conflict: which one holds?",
                      "the later answer holds"),
}
PREMORTEM = "If this fails in 3 months, what most likely caused it?"
VAGUE = {"", "?", "idk", "dunno", "unsure", "maybe", "whatever", "tbd", "not sure", "dont know",
         "don't know", "no idea", "anything", "any"}


def _s(v) -> str:
    return str(v or "").strip()


def intake_needed(root: Path, agent: Path) -> bool:
    """New project: no state file, or a charter whose purpose or success is blank."""
    done = _dcio.read_text(agent / INTAKE_NAME)
    if done and '"written": true' in done:
        return False
    if not (agent / _dcio.STATE_NAME).is_file():
        return True
    c = load_charter(agent)
    return isinstance(c, dict) and not (_s(c.get("purpose")) and _s(c.get("success")))


def _charter_filled(c: dict) -> dict:
    sc = _s(c.get("milestones")) or any(isinstance(r, dict) and _s(r.get("feature"))
                                         for r in (c.get("scope") or []))
    return {"1": bool(_s(c.get("purpose"))), "2": bool(_s(c.get("stakeholders")) or _s(c.get("sponsor"))),
            "3": bool(sc and _s(c.get("out_of_scope"))), "4": bool(c.get("data")),
            "5": any(_s(c.get(k)) for k in ("security", "never", "approvals", "retention")),
            "6": bool(_s(c.get("success"))),
            "7": any(isinstance(r, dict) and _s(r.get("risk")) for r in (c.get("risks") or []))}


def _new_item(label, cls, q, rec, alt, src=None) -> dict:
    return {"label": label, "cls": cls, "q": q, "rec": rec, "alt": alt, "ans": None,
            "src": src, "reasked": 0, "disc": False}


def intake_session(agent: Path, tier: str = "build") -> dict:
    text = _dcio.read_text(agent / INTAKE_NAME)
    if text:
        try:
            s = json.loads(text)
            if isinstance(s, dict) and isinstance(s.get("items"), dict):
                return s
        except ValueError:
            pass
    c = load_charter(agent)
    filled = _charter_filled(c if isinstance(c, dict) else {})
    items = {}
    for key, label, cls, q, rec, alt in INTAKE_ITEMS:
        if tier == "quick":
            cls = "blocking" if key in QUICK_ITEMS else "skip"
        items[key] = _new_item(label, cls, q, rec, alt, "charter" if filled[key] else None)
    return {"tier": tier, "items": items, "asked": 0, "cur": None, "flags": [],
            "done": False, "n_extra": 0, "discovery": []}


def _save_intake(agent: Path, s: dict) -> None:
    _dcio.atomic_write(agent / INTAKE_NAME, json.dumps(s, indent=2, sort_keys=True) + "\n")


def _order(s: dict) -> list[str]:
    return [i[0] for i in INTAKE_ITEMS] + [k for k in s["items"] if k.startswith("x")] + ["pm"]


def _resolved(it: dict) -> bool:
    return it["src"] is not None or it["disc"]


def _open(s: dict, cls: str) -> list[str]:
    return [k for k in _order(s) if k in s["items"] and s["items"][k]["cls"] == cls
            and not _resolved(s["items"][k])]


def _pm_pending(s: dict) -> bool:
    return "pm" in s["items"] and not _resolved(s["items"]["pm"])


def intake_estimate(s: dict) -> int:
    """Questions asked so far (the open one included) plus the ones still worth asking."""
    cur = s["cur"] if s["cur"] in s["items"] and not _resolved(s["items"].get(s["cur"], {"src": 1})) else None
    blocking = len([k for k in _open(s, "blocking") if k != cur])
    pm = 1 if _pm_pending(s) and cur != "pm" else 0
    shaping = len([k for k in _open(s, "shaping") if k != cur]) if s["tier"] == "build" else 0
    room = max(0, SHAPING_ROOM - s["asked"] - blocking - pm)
    return min(s["asked"] + blocking + pm + min(shaping, room), INTAKE_CAPS[s["tier"]])


def _short(text, n: int = 56) -> str:
    text = " ".join(_s(text).split())
    return text if len(text) <= n else text[:n - 3] + "..."


def intake_card(s: dict, header: str) -> list[str]:
    out = [header]
    for key, label, *_ in INTAKE_ITEMS:
        it = s["items"][key]
        if it["cls"] == "skip" and it["src"] is None:
            state = "skipped (quick)"
        elif it["disc"]:
            state = f"? {_short(it['q'])} (m0 discovery)"
        elif it["src"] is None:
            state = f"? {_short(it['q'])}"
        elif it["src"] == "charter":
            state = "filled"
        elif it["src"] == "assumed":
            state = f"assumed: {_short(it['ans'])}"
        else:
            state = f"filled: {_short(it['ans'])}"
        out.append(f"{key} {label}: {state}")
    return out


def _qtext(it: dict) -> str:
    return it["q"] if it["label"] in ("Pre-mortem",) else f"{it['label']}: {it['q']}"


def _pick_next(s: dict) -> str | None:
    blocking = _open(s, "blocking")
    if blocking:
        return blocking[0]
    if _pm_pending(s):
        return "pm"
    if s["tier"] == "build" and s["asked"] < SHAPING_ROOM:
        shaping = _open(s, "shaping")
        if shaping:
            return shaping[0]
    return None


FINAL = "INTAKE: final card - ok, or what to change (then: intake ok)"


def _disc_list(s: dict) -> list[str]:
    """Every item deferred to m0 discovery, in card order."""
    return [_qtext(s["items"][k]) for k in _order(s)
            if k in s["items"] and s["items"][k]["disc"]]


def _finish(s: dict, capped: bool) -> list[str]:
    """Close the intake: unresolved Blocking -> m0 discovery, Shaping -> assumed defaults."""
    disc = []
    for k in _order(s):
        it = s["items"].get(k)
        if not it or _resolved(it) or it["cls"] == "skip":
            continue
        if it["cls"] == "blocking":
            if capped:
                it["disc"] = True
                disc.append(_qtext(it))
        elif it["cls"] == "shaping":
            it["ans"], it["src"] = it["rec"], "assumed"
    s["cur"], s["done"], s["discovery"] = None, True, _disc_list(s)
    head = ([f"INTAKE: cap {INTAKE_CAPS[s['tier']]} reached; m0 " + intake_m0(s["discovery"])]
            if disc else [])
    return head + intake_card(s, FINAL)


def intake_m0(disc: list[str]) -> str:
    return "discovery - answer by probing or a spike, not chat: " + " | ".join(disc)


def intake_turn(s: dict) -> list[str]:
    """The next question turn (<= 3 lines), or the final card when nothing is open."""
    cur = s["cur"]
    if cur and cur in s["items"] and not _resolved(s["items"][cur]):
        key = cur
    else:
        key = _pick_next(s)
        if key is None:
            return _finish(s, False)
        if s["asked"] >= INTAKE_CAPS[s["tier"]]:
            return _finish(s, True)
        s["asked"] += 1
        s["cur"] = key
    it = s["items"][key]
    n = max(intake_estimate(s), s["asked"])
    q = it["q"] if key == "pm" else _qtext(it)
    return [f"Q{s['asked']} of ~{n} · {q} 1) {it['rec']} (recommended) 2) {it['alt']} 3) other",
            "Reply 1, 2, text, default or rest."]


def intake_record(s: dict, key: str, text: str, source: str) -> list[str]:
    if key not in s["items"]:
        raise DcError(f"unknown intake item {key}")
    it = s["items"][key]
    t = _s(text)
    if t == "3" and source == "user" and it["cls"] == "blocking" and it["reasked"] >= 1:
        it["ans"], it["src"], it["disc"] = None, None, True  # re-ask option 3: m0 discovery
        if s["cur"] == key:
            s["cur"] = None
        return intake_turn(s)
    if t in ("1", "2") and source == "user":
        t = it["rec"] if t == "1" else it["alt"]
    elif t.lower().strip(" .!") in VAGUE and source == "user" and it["cls"] == "blocking":
        if it["reasked"] < 1:
            it["reasked"] = 1
            s["cur"] = key
            return [f"Pick one for {it['label']}: 1) {it['rec']} (recommended) 2) {it['alt']} "
                    "3) leave it for discovery"]
        t, source = it["rec"], "assumed"
    it["ans"], it["src"], it["disc"] = t, source, False
    if s["cur"] == key:
        s["cur"] = None
    return intake_turn(s)


def intake_flag(s: dict, name: str) -> list[str]:
    if name not in INTAKE_TRIGGERS:
        raise DcError(f"unknown trigger {name}; one of {', '.join(INTAKE_TRIGGERS)}")
    if name in s["flags"]:
        return [f"INTAKE: {name} already queued."]
    s["flags"].append(name)
    cls, label, q, rec = INTAKE_TRIGGERS[name]
    if name == "live-write" and s["tier"] == "build":
        s["items"].setdefault("pm", _new_item("Pre-mortem", "blocking", PREMORTEM,
                                              "stale input data", "a manual step skipped"))
    s["n_extra"] += 1
    s["items"][f"x{s['n_extra']}"] = _new_item(label, cls, q, rec, "answer in discovery")
    return [f"INTAKE: trigger {name} queued a {cls} question. Open blocking "
            f"{len(_open(s, 'blocking'))}, ~{intake_estimate(s)} questions."]


def intake_add(s: dict, text: str, cls: str) -> list[str]:
    if cls not in ("blocking", "shaping", "cosmetic"):
        raise DcError("class must be blocking, shaping or cosmetic")
    s["n_extra"] += 1
    key = f"x{s['n_extra']}"
    it = _new_item("Extra", cls, _s(text), "follow the repo convention", "answer in discovery")
    s["items"][key] = it
    if cls == "cosmetic":
        it["cls"], it["ans"], it["src"] = "shaping", "follow the repo convention", "repo"
        return [f"INTAKE: cosmetic, not asked: {_short(text)} -> follow the repo convention."]
    return [f"INTAKE: queued {cls} question {key}. Open blocking {len(_open(s, 'blocking'))}, "
            f"~{intake_estimate(s)} questions."]


def intake_write(root: Path, agent: Path, s: dict, name: str = "") -> list[str]:
    """Write the answers through the charter schema; return the DC:DECISIONS lines."""
    if _open(s, "blocking") or _pm_pending(s):
        raise DcError("Blocking items are still open; run `intake next`, or `intake rest`")
    if not s["done"]:
        _finish(s, False)
    path = agent / CHARTER_NAME
    raw_file = {}
    text = _dcio.read_text(path)
    if text:
        try:
            raw_file = json.loads(text)
        except ValueError:
            raw_file = {}
    c = dict(raw_file.get("charter") or {}) if isinstance(raw_file, dict) else {}
    it = s["items"]

    def ans(k):
        v = it.get(k)
        return v["ans"] if v and v["src"] not in (None, "charter") and v["ans"] else None

    if ans("1"):
        c["purpose"] = ans("1")
    if ans("2"):
        c["stakeholders"] = ans("2")
    if ans("3"):
        mvp, _, out = ans("3").partition("|")
        if mvp.strip() and mvp.strip() not in _s(c.get("milestones")).splitlines():
            c["milestones"] = (mvp.strip() + "\n" + _s(c.get("milestones"))).strip()
        if out.strip():
            c["out_of_scope"] = out.strip()
    if ans("4"):
        c["data"] = [d.strip() for d in re.split(r"[;\n]", ans("4")) if d.strip()]
    if ans("5"):
        c["security"] = ans("5")
    if ans("6"):
        c["success"] = ans("6")
        c["acceptance"] = _s(c.get("acceptance")) or ans("6")
    risks = list(c.get("risks") or [])
    for k in ("7", "pm"):
        if ans(k) and not any(isinstance(r, dict) and r.get("risk") == ans(k) for r in risks):
            risks.append({"risk": ans(k), "likelihood": "Med", "impact": "Med",
                          "guardrail": "manual fallback" if k == "7" else ""})
    if risks:
        c["risks"] = risks
    extra = [f"{_short(v['q'], 80)} -> {v['ans']}" for k, v in it.items()
             if k.startswith("x") and v["src"] and v["ans"]]
    have = _s(c.get("assumptions")).splitlines()
    extra = [e for e in extra if e not in have]
    if extra:
        c["assumptions"] = "\n".join(have + extra)
    if s.get("discovery"):
        kept = [ln for ln in _s(c.get("milestones")).splitlines()
                if not ln.startswith("m0 discovery")]
        c["milestones"] = "\n".join([f"m0 {intake_m0(s['discovery'])}"] + kept).strip()
    c["name"] = _s(c.get("name")) or _s(name) or root.name
    clean = clean_charter(c)
    _dcio.atomic_write(path, json.dumps(
        {"schema": SCHEMA_VERSION, "created": (raw_file or {}).get("created") or _iso(_now()),
         "charter": clean}, indent=2, sort_keys=True) + "\n")
    s["written"] = True
    lines = []
    for k in _order(s):
        v = it.get(k)
        if not v or v["ans"] is None or v["src"] in (None, "charter"):
            continue
        if v["src"] == "user" and v["cls"] != "blocking":
            continue
        lines.append(f"{v['q'] if k == 'pm' else _qtext(v)} -> {v['ans']} ({v['src']})")
    return lines


def intake_command(root: Path, agent: Path, args) -> list[str]:
    op = args.iop
    words = " ".join(args.words)
    if op == "start" and not intake_needed(root, agent):
        return ["INTAKE: skip (resumed project)"]
    s = intake_session(agent, args.tier)
    if op == "start":
        blocking = _open(s, "blocking")
        head = (f"INTAKE: card ({s['tier']}); {len(blocking)} blocking open, "
                f"~{intake_estimate(s)} questions; run `intake next`" if blocking else
                "INTAKE: card, nothing blocking open - ok, or what to change (then: intake ok)")
        out = intake_card(s, head)
    elif op == "next":
        out = intake_turn(s)
    elif op == "answer":
        key, _, text = words.partition(" ")
        out = intake_record(s, key, text, args.source)
    elif op == "default":
        key = s["cur"]
        if not key:
            raise DcError("no question is open; run `intake next`")
        out = intake_record(s, key, s["items"][key]["rec"], "assumed")
    elif op == "rest":
        for k in _order(s):
            v = s["items"].get(k)
            if v and not _resolved(v) and v["cls"] != "skip" and k != "pm":
                v["ans"], v["src"] = v["rec"], "assumed"
        s["cur"], s["done"], s["discovery"] = None, True, _disc_list(s)
        out = intake_card(s, FINAL)
    elif op == "rec":
        key, _, text = words.partition(" ")
        if key not in s["items"] or not text.strip():
            raise DcError("usage: intake rec ITEM TEXT")
        s["items"][key]["rec"] = text.strip()
        out = [f"INTAKE: recommended default for {key} set."]
    elif op == "flag":
        out = intake_flag(s, words)
    elif op == "add":
        out = intake_add(s, words, args.cls)
    elif op == "ok":
        decisions = intake_write(root, agent, s, args.name)
        _save_intake(agent, s)
        return (["INTAKE: charter written (.agent/charter.json). Gate 2 DC:DECISIONS:"]
                + (decisions or ["no open questions"]))
    else:
        raise DcError(f"unknown intake op {op}")
    _save_intake(agent, s)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dc_project.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=None)
    p.add_argument("--home", default=None, help="registry/config directory override")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("summary")
    s.add_argument("--json", action="store_true")
    sub.add_parser("next")
    sub.add_parser("charter", help="the charter as Gates 1 and 2 read it")

    ch = sub.add_parser("chunk").add_subparsers(dest="op", required=True)
    a = ch.add_parser("add")
    a.add_argument("--milestone", required=True)
    a.add_argument("--goal", required=True)
    a.add_argument("--targets", help="semicolon-separated path or path::symbol")
    a.add_argument("--est", type=int, help="estimated minutes (aim for <=25)")
    a.add_argument("--deps", help="comma-separated chunk ids (same milestone) done first")
    a.add_argument("--size", choices=SIZES)
    a.add_argument("--parallel", choices=("yes", "no"), help="needs --why")
    a.add_argument("--why", help="one line: why parallel yes or no")
    a.add_argument("--critical", metavar="KIND",
                   help="always sequential: deploy, payroll, migration, destructive, release")
    d = ch.add_parser("done")
    d.add_argument("--milestone", required=True)
    d.add_argument("--id", required=True)
    d.add_argument("--note", default=None, help="what was done")
    d.add_argument("--undo", action="store_true")
    d.add_argument("--context-high", action="store_true",
                   help="model-observed: context is over ~50%% of the window")
    d.add_argument("--host", default=None,
                   help="claude, codex, ...: picks the PURGE resume line (default: detected)")
    d.add_argument("--commit", action="store_true",
                   help="auto-commit the step first (as `commit`), then tick, boundary, compact line")
    d.add_argument("--verified", action="store_true", help="with --commit: as `commit --verified`")
    d.add_argument("--trailer", action="append", default=[], help="with --commit: as `commit`")
    d.add_argument("--subject", default="", help="with --commit: as `commit`")
    d.add_argument("--paths", default="", help="with --commit: as `commit`")
    st = ch.add_parser("start", help="snapshot already-edited files before a step (auto-commit)")
    st.add_argument("--milestone", required=True)
    st.add_argument("--id", default="", help="chunk id; omit for a milestone with no chunks")
    st.add_argument("--sequential", default="", metavar="REASON",
                    help="build a chunk the fan-out test picked for parallel in order (reason recorded)")
    ls = ch.add_parser("list")
    ls.add_argument("--milestone")

    fo = sub.add_parser("fanout", help="Gate 4 fan-out test: PARALLEL or SEQUENTIAL")
    fo.add_argument("--milestone", required=True)
    fo.add_argument("--no-subagents", action="store_true", help="host cannot run subagents")
    fo.add_argument("--critical", default="", help="comma-separated chunk ids that are critical")
    br = sub.add_parser("brief", help="builder brief for one chunk (<= 30 lines)")
    br.add_argument("--milestone", required=True)
    br.add_argument("--chunk", required=True)
    br.add_argument("--budget", type=int, default=40000)
    br.add_argument("--verify", default=None)
    pl = sub.add_parser("plan", help="Gate 2 planning gate: need, init, check, approve, waive")
    pl.add_argument("pop", choices=("need", "init", "check", "approve", "waive"))
    pl.add_argument("--milestone", required=True)
    pl.add_argument("--reason", default="", help="waive: the user's reason")
    mo = sub.add_parser("merge-order", help="chunks in dependency order with branch names")
    mo.add_argument("--milestone", required=True)
    mo.add_argument("--chunks", default="")

    er = sub.add_parser("error").add_subparsers(dest="op", required=True)
    a = er.add_parser("add")
    a.add_argument("--summary", required=True)
    for f in ("milestone", "chunk", "cause", "fix"):
        a.add_argument(f"--{f}", default="")
    f = er.add_parser("fix")
    f.add_argument("--id", required=True)
    f.add_argument("--fix", required=True)
    er.add_parser("list")

    td = sub.add_parser("todo").add_subparsers(dest="op", required=True)
    a = td.add_parser("add")
    a.add_argument("--text", required=True)
    a.add_argument("--milestone", default="")
    d = td.add_parser("done")
    d.add_argument("--id", required=True)
    d.add_argument("--undo", action="store_true")
    td.add_parser("list")

    tm = sub.add_parser("time").add_subparsers(dest="op", required=True)
    tm.add_parser("touch")
    a = tm.add_parser("add")
    a.add_argument("--minutes", type=float)
    a.add_argument("--start")
    a.add_argument("--end")
    a.add_argument("--note", default="")
    d = tm.add_parser("delete")
    d.add_argument("--id", required=True)

    cm = sub.add_parser("commit", help="auto-commit the step's own files (G9)")
    cm.add_argument("--milestone", required=True)
    cm.add_argument("--chunk", default="", help="chunk id; omit for a milestone with no chunks")
    cm.add_argument("--verified", action="store_true",
                    help="the step's DC:VERIFY tier passed; without it, a passing tier "
                         "record for the current tree is needed")
    cm.add_argument("--trailer", action="append", default=[],
                    help="attribution trailer line (repeatable); none by default")
    cm.add_argument("--subject", default="", help="commit subject (default: from the step)")
    cm.add_argument("--paths", default="", help="semicolon-separated extra paths the step edited")
    cf = sub.add_parser("config", help="per-project settings; no flags prints them")
    cf.add_argument("--autocommit", choices=("on", "off"))
    cf.add_argument("--clean-tree", choices=("on", "off"),
                    help="run the fast check in a temp worktree at the candidate commit")
    cf.add_argument("--review-required", choices=("on", "off"),
                    help="treat the repo as review-required even without CODEOWNERS")
    cf.add_argument("--verify-cmd", help="command for the clean-tree check")

    ik = sub.add_parser("intake", help="G7 new-project intake: card, questions, caps, charter write")
    ik.add_argument("iop", nargs="?", default="start",
                    choices=("start", "next", "answer", "default", "rest", "rec", "flag", "add", "ok"))
    ik.add_argument("words", nargs="*", help="answer: ITEM TEXT; flag: trigger; add: question; rec: text")
    ik.add_argument("--tier", choices=("build", "quick"), default="build")
    ik.add_argument("--source", choices=("user", "assumed", "repo"), default="user")
    ik.add_argument("--class", dest="cls", default="shaping")
    ik.add_argument("--name", default="")
    cr = sub.add_parser("create", help="new project folder from a charter JSON file")
    cr.add_argument("--charter", required=True, help="charter JSON (name and purpose required)")
    cr.add_argument("--location", help="one of project_roots in config.json (default: first)")
    cr.add_argument("--folder", help="folder name (default: from the project name)")
    for name in ("complete", "reopen"):
        lc = sub.add_parser(name, help=f"{name} this project (registry + sidecar)")
        lc.add_argument("--note", default="")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd == "create":
        text = _dcio.read_text(Path(args.charter))
        if text is None:
            raise DcError(f"cannot read {args.charter}")
        try:
            charter = json.loads(text)
        except ValueError:
            raise DcError(f"{args.charter} is not valid JSON") from None
        made = create_project(charter, args.home, args.location, args.folder)
        print(f"DRAINCLAMP: created {made['name']} at {made['root']} "
              "(git repo, state, charter, sidecar; registered).")
        print(f"Next: run dc_session.py --resume {made['root']}")
        return _dcio.EXIT_OK
    root = _dcio.project_root(args.root)
    if args.cmd in ("complete", "reopen"):
        status = "complete" if args.cmd == "complete" else "active"
        set_lifecycle(root, status, args.home, args.note)
        print(f"DRAINCLAMP: {root.name} {'marked complete' if status == 'complete' else 'reopened'}."
              " Its state file is untouched.")
        return _dcio.EXIT_OK
    agent = root / _dcio.AGENT_DIR_NAME
    if args.cmd == "intake":
        print("\n".join(intake_command(root, agent, args)))
        return _dcio.EXIT_OK

    if args.cmd == "summary":
        s = summary(load(agent), args.home)
        if args.json:
            print(json.dumps(s, indent=2, sort_keys=True))
        else:
            print(f"chunks {s['chunks']['done']}/{s['chunks']['total']} | "
                  f"errors open {s['errors_open']} | to-dos open {s['todos_open']} | "
                  f"build {s['build_minutes'] / 60:.1f} h")
        return _dcio.EXIT_OK

    if args.cmd == "next":
        print("\n".join(capsule(root, load(agent), args.home) + sibling_lines(root)))
        return _dcio.EXIT_OK
    if args.cmd == "charter":
        print("\n".join(charter_view(load_charter(agent))))
        return _dcio.EXIT_OK

    if args.cmd == "commit":
        print("\n".join(commit_step(root, args.milestone, args.chunk, args.verified,
                                    args.trailer, args.subject, _targets(args.paths),
                                    args.home)))
        return _dcio.EXIT_OK
    if args.cmd == "config":
        def edit(data: dict) -> dict:
            cfg = autocommit_cfg(data)
            if args.autocommit:
                cfg["enabled"] = args.autocommit == "on"
            if args.clean_tree:
                cfg["clean_tree"] = args.clean_tree == "on"
            if args.review_required:
                cfg["review_required"] = args.review_required == "on"
            if args.verify_cmd is not None:
                cfg["verify_cmd"] = args.verify_cmd.strip()
                if cfg["verify_cmd"]:
                    approve_cmd(root, split_cmd(cfg["verify_cmd"]), args.home)
            data["autocommit"] = cfg
            return data

        changed = any(v is not None for v in (args.autocommit, args.clean_tree,
                                              args.review_required, args.verify_cmd))
        cfg = autocommit_cfg(mutate(agent, edit) if changed else load(agent))

        def flag(v):
            return "on" if v else "off"

        print(f"DRAINCLAMP: autocommit {flag(cfg['enabled'])} | clean-tree "
              f"{flag(cfg['clean_tree'])} | review-required {flag(cfg['review_required'])} | "
              f"verify-cmd {cfg['verify_cmd'] or '(fast-tier DC:VERIFY)'}")
        return _dcio.EXIT_OK
    if args.cmd == "fanout":
        crit = [x.strip() for x in args.critical.split(",") if x.strip()]
        print("\n".join(fanout(load(agent), root, args.milestone, args.no_subagents, crit)))
        return _dcio.EXIT_OK
    if args.cmd == "brief":
        print("\n".join(brief(load(agent), root, args.milestone, args.chunk,
                              args.budget, args.verify)))
        return _dcio.EXIT_OK
    if args.cmd == "plan":
        import dc_plan  # imports this module lazily
        mid = args.milestone
        row = next((r for r in roadmap(root) if r["id"] == mid), None)
        if row is None:
            raise DcError(f"milestone {mid} is not in DC:ROADMAP")
        if args.pop == "need":
            print("\n".join(dc_plan.need_lines(load(agent), root, mid)))
        elif args.pop == "init":
            print("\n".join(dc_plan.init(load(agent), root, mid, row["goal"])))
        elif args.pop == "check":
            gaps = dc_plan.check(load(agent), root, mid)
            print(f"PLAN {mid}: complete - show it to the user, then plan approve" if not gaps else
                  f"PLAN {mid}: {len(gaps)} gap(s)\n" + "\n".join(f"  {g}" for g in gaps))
            return _dcio.EXIT_OK if not gaps else _dcio.EXIT_CHECK_FAILED
        else:
            out: list[str] = []

            def plan_op(data: dict) -> dict:
                out.append(dc_plan.record(data, root, mid, args.pop, args.reason))
                return data

            mutate(agent, plan_op)
            print(out[0])
        return _dcio.EXIT_OK
    if args.cmd == "merge-order":
        ids = [x.strip() for x in args.chunks.split(",") if x.strip()]
        print("\n".join(merge_order(load(agent), root, args.milestone, ids)))
        return _dcio.EXIT_OK

    op = f"{args.cmd}.{args.op}"
    if op == "chunk.start":
        print("DRAINCLAMP: " + start_step(root, args.milestone, args.id, args.sequential))
        return _dcio.EXIT_OK
    if op == "chunk.list":
        for mid, cs in load(agent)["chunks"].items():
            if args.milestone and mid != args.milestone:
                continue
            for c in cs:
                print(f"{mid}/{c['id']} [{'x' if c.get('done') else ' '}] {c['goal']}")
        return _dcio.EXIT_OK
    if op in ("error.list", "todo.list"):
        for item in load(agent)["errors" if args.cmd == "error" else "todos"]:
            flag = item.get("status", "open") if args.cmd == "error" else \
                ("x" if item.get("done") else " ")
            print(f"{item['id']} [{flag}] {item.get('summary') or item.get('text')}")
        return _dcio.EXIT_OK
    if op in ("chunk.done", "todo.done") and args.undo:
        op = op.replace(".done", ".undo")

    fields = {k: v for k, v in vars(args).items()
              if k not in ("cmd", "op", "root", "home", "undo", "context_high", "host",
                           "commit", "verified", "trailer", "subject", "paths")}
    committed = op == "chunk.done" and args.commit
    if committed:
        # One end-of-step call: commit, tick, boundary, compact line (phase4 order).
        lines = commit_step(root, args.milestone, args.id, args.verified, args.trailer,
                            args.subject, _targets(args.paths), args.home)
        print("\n".join(lines))
        if lines[0].startswith("COMMITTED"):
            short = lines[0].split()[1]
            fields["note"] = f"{short}: {args.note}" if args.note else short
        elif lines[0] in ("NOT COMMITTED: no files changed by this step",
                          "NOT COMMITTED: the step's files have no net change"):
            pass  # nothing to commit: the step is still done
        elif autocommit_cfg(load(agent))["enabled"]:
            print(f"DRAINCLAMP: chunk {args.milestone}/{args.id} not ticked: fix the reason above "
                  f"and rerun, or tick it without a commit: chunk done --milestone {args.milestone} "
                  f"--id {args.id}")
            return _dcio.EXIT_OK
    result = []

    def apply(data: dict) -> dict:
        result.append(apply_op(data, root, op, fields))
        return data

    data = mutate(agent, apply)
    print("DRAINCLAMP: " + result[0])
    if op == "chunk.add" and not getattr(args, "size", None):
        print("DRAINCLAMP: no --size: the fan-out test treats this chunk as sequential")
    if op == "chunk.done":
        verdict = boundary(data, root, args.milestone, args.id, args.context_high, args.host)
        print("\n".join(verdict))
        # A PURGE or close verdict already answers the compact question; never print both.
        if committed and not any(v.startswith(("PURGE", "Milestone ")) for v in verdict):
            held = next((v for v in verdict if v.startswith("HOLD")), "")
            advice = (dc_state.advice_of(held) if held
                      else dc_state.compact_advice(root, args.context_high))
            print(dc_state.compact_line(*dc_state.compact_check(root), advice))
    return _dcio.EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DcError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(_dcio.EXIT_INTERNAL)
