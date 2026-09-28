#!/usr/bin/env python3
"""Project sidecar: `.agent/drainclamp-project.json`, schema 1.

State schema v1 is frozen, so everything a person tracks about a project beyond
the roadmap lives here: small chunks under each milestone with a note of what
was done, an error register, a to-do list, build time, the time/cost the work
saves (estimated, and realised from a log of real runs), a health flag with a
dated update, and a profile -- summary, features and safeguards by area -- that
the board shows as the project's rundown. A new project's profile is seeded
from its charter.

Chunks double as a token budget. Each one names the files (and symbols) it
touches, so implementation reads only those, and `next` prints a resume capsule
of a dozen lines instead of the whole state file and log. Chunk notes are for
the dashboard and are never loaded into context by any gate.

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

LISTS = ("errors", "todos", "time", "runs", "updates")
PREFIX = {"errors": "e", "todos": "t", "time": "s"}
AREAS = ("Security", "Data retention", "Guardrails", "Confirmation checks", "Testing")
HEALTH = ("on", "risk", "off")
PROFILE_CAP = 100
UPDATES_KEPT = 50


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
            "todos": [], "time": [], "savings": {}, "runs": [], "updates": [],
            "health": None, "profile": {"summary": "", "features": [], "safeguards": []},
            "tokens": None}


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
            or not isinstance(out["savings"], dict) \
            or any(not isinstance(out[k], list) for k in LISTS) \
            or not isinstance(out["profile"], dict) \
            or not (out["health"] is None or isinstance(out["health"], dict)) \
            or not (out["tokens"] is None or isinstance(out["tokens"], dict)):
        raise DcError("sidecar fields have the wrong types; left untouched")
    prof = {"summary": "", "features": [], "safeguards": []}
    prof.update(out["profile"])
    if not isinstance(prof["summary"], str) or not isinstance(prof["features"], list) \
            or not isinstance(prof["safeguards"], list):
        raise DcError("sidecar profile has the wrong types; left untouched")
    out["profile"] = prof
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


# -- savings -----------------------------------------------------------------

def global_rate(home: str | None) -> float | None:
    text = _dcio.read_text(dc_registry.home_dir(home) / CONFIG_NAME)
    try:
        rate = json.loads(text).get("hourly_rate") if text else None
    except (ValueError, AttributeError):
        return None
    return float(rate) if isinstance(rate, (int, float)) else None


def savings(data: dict, home: str | None) -> dict:
    s = data["savings"]
    rate = s.get("hourly_rate")
    rate = float(rate) if isinstance(rate, (int, float)) else global_rate(home)
    out = {"baseline_min": s.get("baseline_min"), "new_min": s.get("new_min"),
           "runs_per_year": s.get("runs_per_year"), "hourly_rate": rate,
           "annual_hours": None, "annual_cost": None, "build_cost": None,
           "payback_years": None, "note": s.get("note", ""), "realised_cost": None}
    if all(isinstance(s.get(k), (int, float)) for k in ("baseline_min", "new_min",
                                                          "runs_per_year")):
        out["annual_hours"] = (s["baseline_min"] - s["new_min"]) * s["runs_per_year"] / 60
        if rate is not None:
            out["annual_cost"] = out["annual_hours"] * rate
    out.update(realised(data, s))
    if rate is not None and out["realised_hours"] is not None:
        out["realised_cost"] = out["realised_hours"] * rate
    if rate is not None:
        out["build_cost"] = build_minutes(data) / 60 * rate
        if out["annual_cost"]:
            out["payback_years"] = out["build_cost"] / out["annual_cost"]
    return out


def realised(data: dict, s: dict) -> dict:
    """Hours actually saved, from logged runs: each run saves baseline - its minutes."""
    runs = data.get("runs") or []
    base = s.get("baseline_min")
    if not runs or not isinstance(base, (int, float)):
        return {"realised_runs": sum(r.get("count", 0) for r in runs), "realised_hours": None}
    total = 0.0
    for r in runs:
        took = r.get("minutes")
        took = took if isinstance(took, (int, float)) else s.get("new_min")
        if isinstance(took, (int, float)):
            total += (base - took) * r.get("count", 0)
    return {"realised_runs": sum(r.get("count", 0) for r in runs),
            "realised_hours": total / 60}


def summary(data: dict, home: str | None) -> dict:
    chunks = [c for cs in data["chunks"].values() for c in cs]
    return {
        "chunks": {"done": sum(1 for c in chunks if c.get("done")), "total": len(chunks)},
        "errors_open": sum(1 for e in data["errors"] if e.get("status") != "fixed"),
        "todos_open": sum(1 for t in data["todos"] if not t.get("done")),
        "build_minutes": build_minutes(data),
        "savings": savings(data, home),
        "health": data.get("health"),
        "safeguards": {"in": sum(1 for g in data["profile"]["safeguards"]
                                 if g.get("status") == "in"),
                       "plan": sum(1 for g in data["profile"]["safeguards"]
                                   if g.get("status") != "in")},
        "completed": data.get("completed"),
        "rev": data["rev"],
    }


# -- capsule -----------------------------------------------------------------

def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= NOTE_CAP else text[:NOTE_CAP - 3] + "..."


def read_hint(target: str) -> str:
    path, _, symbol = target.partition("::")
    if symbol:
        return f"dc_chunk.py --symbol {symbol} --path {path}"
    return f"read {path}"


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
    nxt = next((c for c in chunks if not c.get("done")), None)
    if not chunks:
        lines.append("No chunks yet: split this milestone with `dc_project.py chunk add`.")
    elif nxt is None:
        lines.append("All chunks done: close the milestone (Gate 4 log, Gate 5).")
    else:
        est = f" (~{nxt['est_min']} min)" if nxt.get("est_min") else ""
        lines.append(f"Next chunk: {nxt['id']} - {_clip(nxt['goal'])}{est}")
        if nxt.get("targets"):
            lines.append("Targets: " + ", ".join(nxt["targets"][:4])
                         + (f" (+{len(nxt['targets']) - 4})" if len(nxt["targets"]) > 4 else ""))
            for t in nxt["targets"][:3]:
                lines.append("  " + read_hint(t))
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
        return [f"Milestone {mid}: all chunks done. Close it with Gate 5 "
                "(dc_state.py --purge-check)."]
    verdict, human = dc_state.chunk_purge_check(done.get("targets", []),
                                                nxt.get("targets", []), root, context_high)
    return [f"Next chunk: {nxt['id']} - {_clip(nxt['goal'])}", verdict, human]


# -- operations (shared by the CLI and dc_board.py) ---------------------------

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
       "todo.add", "todo.done", "todo.undo", "time.touch", "time.add", "time.delete",
       "savings.set", "profile.summary", "profile.seed", "feature.add", "feature.edit",
       "feature.delete", "safeguard.add", "safeguard.edit", "safeguard.delete",
       "health.set", "run.add", "run.delete")


def _capped(value: str, key: str) -> str:
    if len(value) > TEXT_CAP:
        raise DcError(f"{key} is over {TEXT_CAP} characters")
    return value


def _room(items: list, what: str) -> None:
    if len(items) >= PROFILE_CAP:
        raise DcError(f"at most {PROFILE_CAP} {what}")


def _safeguard_fields(g: dict, a: dict, partial: bool) -> None:
    for key in ("area", "control", "detail", "prevents", "status"):
        if partial and a.get(key) is None:
            continue
        value = _capped(_text(a, key, required=key in ("area", "control") and not partial), key)
        if key == "area" and value not in AREAS:
            raise DcError(f"area must be one of {', '.join(AREAS)}")
        if key == "status":
            value = value or "plan"
            if value not in ("in", "plan"):
                raise DcError("status must be in or plan")
        if key in ("area", "control") and not value:
            raise DcError(f"{key} is required")
        g[key] = value


def _lines(text: str) -> list[str]:
    return [l.strip().lstrip("-*\u2022 ").strip() for l in (text or "").splitlines()
            if l.strip().lstrip("-*\u2022 ").strip()]


def seed_profile(data: dict, charter: dict) -> int:
    """Fill a blank profile from the charter. Returns how many items were added.

    Only blanks are filled, so hand edits are never overwritten; every seeded
    safeguard starts as planned until the build puts it in place.
    """
    prof, added = data["profile"], 0
    if not prof["summary"] and charter.get("purpose"):
        prof["summary"] = charter["purpose"][:TEXT_CAP]
    if not prof["features"]:
        for row in charter.get("scope") or []:
            if row.get("priority") == "Won't" or not row.get("feature"):
                continue
            prof["features"].append({"id": _next_id(prof["features"], "f"),
                                     "name": row["feature"], "what": row.get("what", ""),
                                     "priority": row.get("priority", "")})
            added += 1
    if not prof["safeguards"]:
        seeds = [("Security", t, "") for t in _lines(charter.get("security", ""))]
        seeds += [("Data retention", t, "") for t in _lines(charter.get("retention", ""))]
        seeds += [("Guardrails", r["guardrail"], r["risk"])
                  for r in charter.get("risks") or [] if r.get("guardrail")]
        seeds += [("Guardrails", f"Never: {t}", "") for t in _lines(charter.get("never", ""))]
        seeds += [("Confirmation checks", t, "")
                  for t in _lines(charter.get("approvals", ""))]
        seeds += [("Testing", t, "") for t in _lines(charter.get("acceptance", ""))]
        for area, control, prevents in seeds[:PROFILE_CAP]:
            prof["safeguards"].append({"id": _next_id(prof["safeguards"], "g"), "area": area,
                                       "control": control[:TEXT_CAP], "detail": "",
                                       "prevents": prevents[:TEXT_CAP], "status": "plan"})
            added += 1
    return added


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
    if op == "savings.set":
        vals = {k: _num(a, k) for k in ("baseline_min", "new_min", "runs_per_year")}
        rate = _num(a, "hourly_rate", required=False)
        if any(v < 0 for v in vals.values()) or (rate is not None and rate < 0):
            raise DcError("savings figures must not be negative")
        data["savings"] = {**vals, "hourly_rate": rate, "note": _text(a, "note")}
        return "savings set"
    prof = data["profile"]
    if op == "profile.summary":
        prof["summary"] = _capped(_text(a, "text"), "text")
        return "summary saved"
    if op == "profile.seed":
        charter = load_charter(root / _dcio.AGENT_DIR_NAME)
        if charter is None:
            raise DcError("no .agent/charter.json to seed from")
        return f"profile seeded from charter ({seed_profile(data, charter)} items)"
    if op == "feature.add":
        _room(prof["features"], "features")
        fid = _next_id(prof["features"], "f")
        prof["features"].append({"id": fid, "name": _capped(_text(a, "name", True), "name"),
                                 "what": _capped(_text(a, "what"), "what"), "priority": ""})
        return f"feature {fid} added"
    if op == "feature.edit":
        f = _find(prof["features"], _text(a, "id", True), "feature")
        for key in ("name", "what"):
            if a.get(key) is not None:
                f[key] = _capped(_text(a, key, required=key == "name"), key)
        return f"feature {f['id']} saved"
    if op == "safeguard.add":
        _room(prof["safeguards"], "safeguards")
        g = {"id": _next_id(prof["safeguards"], "g")}
        _safeguard_fields(g, a, partial=False)
        prof["safeguards"].append(g)
        return f"safeguard {g['id']} added"
    if op == "safeguard.edit":
        g = _find(prof["safeguards"], _text(a, "id", True), "safeguard")
        _safeguard_fields(g, a, partial=True)
        return f"safeguard {g['id']} saved"
    if op in ("feature.delete", "safeguard.delete"):
        key = "features" if op == "feature.delete" else "safeguards"
        item = _find(prof[key], _text(a, "id", True), key[:-1])
        prof[key].remove(item)
        return f"{key[:-1]} {item['id']} deleted"
    if op == "health.set":
        state = _text(a, "state", True)
        if state not in HEALTH:
            raise DcError(f"state must be one of {', '.join(HEALTH)}")
        data["health"] = {"state": state, "update": _capped(_text(a, "update"), "update"),
                          "at": stamp}
        data["updates"] = (data["updates"] + [data["health"]])[-UPDATES_KEPT:]
        return f"health {state}"
    if op == "run.add":
        count = _num(a, "count", required=False)
        count = 1 if count is None else count
        if count <= 0 or count != int(count):
            raise DcError("count must be a whole number above 0")
        minutes = _num(a, "minutes", required=False)
        if minutes is not None and minutes < 0:
            raise DcError("minutes must not be negative")
        rid = _next_id(data["runs"], "r")
        data["runs"].append({"id": rid, "at": _iso(_parse_iso(a["at"])) if a.get("at")
                             else stamp, "count": int(count), "minutes": minutes,
                             "note": _capped(_text(a, "note"), "note")})
        return f"run {rid} logged ({int(count)} x)"
    if op == "run.delete":
        r = _find(data["runs"], _text(a, "id", True), "run")
        data["runs"].remove(r)
        return f"run {r['id']} deleted"
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
            v = c["value"]
            if all(v[k] is not None for k in ("baseline_min", "new_min", "runs_per_year")):
                data["savings"] = {k: v[k] for k in ("baseline_min", "new_min",
                                                     "runs_per_year")}
                data["savings"].update(hourly_rate=v["hourly_rate"], note="from charter")
            data["created"] = stamp
            seed_profile(data, c)
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

    sv = sub.add_parser("savings").add_subparsers(dest="op", required=True)
    a = sv.add_parser("set")
    a.add_argument("--baseline-min", type=float, required=True,
                   help="minutes per run before")
    a.add_argument("--new-min", type=float, required=True, help="minutes per run after")
    a.add_argument("--runs-per-year", type=float, required=True)
    a.add_argument("--rate", dest="hourly_rate", type=float,
                   help="hourly cost; overrides config.json")
    a.add_argument("--note", default="")

    pf = sub.add_parser("profile").add_subparsers(dest="op", required=True)
    a = pf.add_parser("summary")
    a.add_argument("--text", required=True)
    pf.add_parser("seed", help="fill blank profile fields from .agent/charter.json")

    ft = sub.add_parser("feature").add_subparsers(dest="op", required=True)
    a = ft.add_parser("add")
    a.add_argument("--name", required=True)
    a.add_argument("--what", default="")
    a = ft.add_parser("edit")
    a.add_argument("--id", required=True)
    a.add_argument("--name")
    a.add_argument("--what")
    ft.add_parser("delete").add_argument("--id", required=True)

    sg = sub.add_parser("safeguard").add_subparsers(dest="op", required=True)
    for name in ("add", "edit"):
        a = sg.add_parser(name)
        if name == "edit":
            a.add_argument("--id", required=True)
        a.add_argument("--area", choices=AREAS, required=name == "add")
        a.add_argument("--control", required=name == "add", help="what is in place")
        a.add_argument("--detail")
        a.add_argument("--prevents")
        a.add_argument("--status", choices=("in", "plan"))
    sg.add_parser("delete").add_argument("--id", required=True)

    hl = sub.add_parser("health").add_subparsers(dest="op", required=True)
    a = hl.add_parser("set")
    a.add_argument("--state", choices=HEALTH, required=True)
    a.add_argument("--update", default="", help="one line: where it stands")

    rn = sub.add_parser("run").add_subparsers(dest="op", required=True)
    a = rn.add_parser("add", help="log real use, for realised savings")
    a.add_argument("--count", type=int, default=1)
    a.add_argument("--minutes", type=float, help="minutes each run took (default: new_min)")
    a.add_argument("--at")
    a.add_argument("--note", default="")
    rn.add_parser("delete").add_argument("--id", required=True)

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
            sv = s["savings"]
            print(f"chunks {s['chunks']['done']}/{s['chunks']['total']} | "
                  f"errors open {s['errors_open']} | to-dos open {s['todos_open']} | "
                  f"build {s['build_minutes'] / 60:.1f} h")
            if sv["annual_hours"] is not None:
                cost = f", ${sv['annual_cost']:,.0f}/yr" if sv["annual_cost"] is not None else ""
                print(f"saves {sv['annual_hours']:.1f} h/yr{cost}")
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
