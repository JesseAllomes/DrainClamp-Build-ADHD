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
import json
import re
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


CLOSE = "close the milestone (Gate 4 log, Gate 5: dc_state.py --purge-check)."


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


def capsule(root: Path, data: dict) -> list[str]:
    """The resume view: what to do next and what to read, nothing else."""
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
    nxt = next((c for c in chunks if not c.get("done")), None)
    if not chunks:
        lines.append("No chunks yet: split this milestone with `dc_project.py chunk add`.")
    elif nxt is None:
        lines.append("All chunks done: " + close_line(root, data, rows, ms["id"]))
    else:
        est = f" (~{nxt['est_min']} min)" if nxt.get("est_min") else ""
        lines.append(f"Next chunk: {nxt['id']} - {_clip(nxt['goal'])}{est}")
        if nxt.get("targets"):
            lines.append("Targets: " + ", ".join(nxt["targets"][:4])
                         + (f" (+{len(nxt['targets']) - 4})" if len(nxt["targets"]) > 4 else ""))
            for t in nxt["targets"][:3]:
                lines.append("  " + read_hint(t, root))
    last = [c for c in chunks if c.get("done")]
    if last:
        lines.append(f"Last done: {last[-1]['id']} - {_clip(last[-1].get('note') or last[-1]['goal'])}")
    errs = [e for e in data["errors"] if e.get("status") != "fixed"]
    todos = sum(1 for t in data["todos"] if not t.get("done"))
    lines.append(f"Open errors: {len(errs)} | open to-dos: {todos}")
    for e in errs[:2]:
        lines.append(f"  {e['id']}: {_clip(e['summary'])}")
    return lines[:CAPSULE_CAP]


def boundary(data: dict, root: Path, mid: str, cid: str,
             context_high: bool = False) -> list[str]:
    """Chunk-boundary verdict: purge before the next chunk, or close the milestone."""
    chunks = data["chunks"].get(mid, [])
    done = next(c for c in chunks if c["id"] == cid)
    nxt = next((c for c in chunks if not c.get("done")), None)
    if nxt is None:
        return [f"Milestone {mid}: all chunks done: " + close_line(root, data, roadmap(root), mid)]
    verdict, human = dc_state.chunk_purge_check(done.get("targets", []),
                                                nxt.get("targets", []), root, context_high)
    return [f"Next chunk: {nxt['id']} - {_clip(nxt['goal'])}", verdict, human]


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
        cs.append({"id": cid, "goal": _text(a, "goal", True),
                   "targets": _targets(a.get("targets")),
                   "est_min": int(est) if est else None, "done": False, "done_at": None,
                   "note": ""})
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


# -- CLI ---------------------------------------------------------------------

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
    d = ch.add_parser("done")
    d.add_argument("--milestone", required=True)
    d.add_argument("--id", required=True)
    d.add_argument("--note", default=None, help="what was done")
    d.add_argument("--undo", action="store_true")
    d.add_argument("--context-high", action="store_true",
                   help="model-observed: context is over ~50%% of the window")
    ls = ch.add_parser("list")
    ls.add_argument("--milestone")

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
        print("Next: open that folder and run /drainclamp-build-adhd.")
        return _dcio.EXIT_OK
    root = _dcio.repo_root(args.root)
    if args.cmd in ("complete", "reopen"):
        status = "complete" if args.cmd == "complete" else "active"
        set_lifecycle(root, status, args.home, args.note)
        print(f"DRAINCLAMP: {root.name} {'marked complete' if status == 'complete' else 'reopened'}."
              " Its state file is untouched.")
        return _dcio.EXIT_OK
    agent = root / _dcio.AGENT_DIR_NAME

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
        print("\n".join(capsule(root, load(agent))))
        return _dcio.EXIT_OK
    if args.cmd == "charter":
        print("\n".join(charter_view(load_charter(agent))))
        return _dcio.EXIT_OK

    op = f"{args.cmd}.{args.op}"
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
              if k not in ("cmd", "op", "root", "home", "undo", "context_high")}
    result = []

    def apply(data: dict) -> dict:
        result.append(apply_op(data, root, op, fields))
        return data

    data = mutate(agent, apply)
    print("DRAINCLAMP: " + result[0])
    if op == "chunk.done":
        print("\n".join(boundary(data, root, args.milestone, args.id, args.context_high)))
    return _dcio.EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DcError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(_dcio.EXIT_INTERNAL)
