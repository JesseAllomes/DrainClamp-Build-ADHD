#!/usr/bin/env python3
"""Gate 2 planning gate and the Gate 4 fan-out rule. Stdlib only.

A milestone with several moving parts needs a written plan before its first chunk starts:
when the fan-out test says PARALLEL, when a chunk is large, or when the chunk estimates add
up to PLAN_EST_MIN or more. The plan lives in `.agent/plans/<m>.md` (never auto-loaded);
the sidecar key `plans` records approval by hash, so an edit after approval needs a fresh
approval. `chunk start` calls `start_gate`, which also enforces the fan-out verdict: a chunk
the test picks for a parallel build is not started sequentially without a recorded reason.

Driven through `dc_project.py plan ...` and `dc_project.py chunk start`.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import _dcio
from _dcio import DcError

PLAN_DIR = "plans"
PLAN_EST_MIN = 90
SECTIONS = ("Approach", "Interfaces", "Chunks", "Edge cases and risks", "Out of scope")
PLACEHOLDER = "TODO:"
MID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
TESTS_SHOWN = 20


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def plan_path(root: Path, mid: str) -> Path:
    if not MID_RE.match(mid or "") or ".." in mid:
        raise DcError(f"milestone id {mid!r} cannot name a plan file")
    return root / _dcio.AGENT_DIR_NAME / PLAN_DIR / f"{mid}.md"


def _digest(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _chunks(data: dict, mid: str) -> list[dict]:
    return [c for c in data.get("chunks", {}).get(mid, []) if isinstance(c, dict)]


def _fanout(data: dict, root: Path, mid: str) -> list[str]:
    import dc_project  # imports this module lazily; loaded late to avoid the cycle
    try:
        return dc_project.fanout(data, root, mid)
    except DcError as exc:
        return [f"SEQUENTIAL: {exc}"]


def picked(verdict: list[str]) -> list[str]:
    """Chunk ids a PARALLEL verdict sends to builders; [] for SEQUENTIAL."""
    if not verdict or not verdict[0].startswith("PARALLEL: "):
        return []
    ids = verdict[0][len("PARALLEL: "):].split(" (", 1)[0]
    return [x.strip() for x in ids.split(",") if x.strip()]


def need(data: dict, root: Path, mid: str) -> tuple[list[str], list[str]]:
    """(reasons a plan is required, fan-out verdict). No reasons: the plan is optional."""
    chunks = _chunks(data, mid)
    verdict = _fanout(data, root, mid) if chunks else ["SEQUENTIAL: no chunks"]
    reasons = []
    if picked(verdict):
        reasons.append("fan-out PARALLEL " + ", ".join(picked(verdict)))
    large = [c["id"] for c in chunks if c.get("size") == "large"]
    if large:
        reasons.append("large chunk " + ", ".join(large))
    # Releases and deploys are run, not designed: they do not count toward the estimate.
    total = sum(int(c.get("est_min") or 0) for c in chunks if not c.get("critical"))
    if total >= PLAN_EST_MIN:
        reasons.append(f"estimates total {total} min (>= {PLAN_EST_MIN})")
    return reasons, verdict


def status(data: dict, root: Path, mid: str) -> str:
    """approved | waived | stale (edited after approval) | draft (file, not approved) | missing"""
    rec = (data.get("plans") or {}).get(mid) or {}
    if rec.get("status") == "waived":
        return "waived"
    text = _dcio.read_text(plan_path(root, mid))
    if text is None:
        return "missing"
    if rec.get("status") == "approved":
        return "approved" if rec.get("hash") == _digest(text) else "stale"
    return "draft"


def sections(text: str) -> dict[str, str]:
    """`## Heading` -> body, in file order."""
    out: dict[str, str] = {}
    cur = None
    for line in text.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and not line.startswith("###"):
            cur = m.group(1)
            out[cur] = ""
        elif cur is not None:
            out[cur] += line + "\n"
    return out


def chunk_entries(body: str) -> dict[str, dict]:
    """`### c1 - goal` blocks under ## Chunks -> {id: {files: [...], test: str}}."""
    out: dict[str, dict] = {}
    cur = None
    for line in body.split("\n"):
        m = re.match(r"^###\s+(\S+)", line)
        if m:
            cur = m.group(1)
            out[cur] = {"files": [], "test": ""}
            continue
        if cur is None:
            continue
        m = re.match(r"^\s*Files:\s*(.*)$", line)
        if m:
            out[cur]["files"] += [f.strip().split("::", 1)[0] for f in m.group(1).split(";") if f.strip()]
        m = re.match(r"^\s*Test:\s*(.*)$", line)
        if m:
            out[cur]["test"] = (out[cur]["test"] + " " + m.group(1).strip()).strip()
    return out


def init(data: dict, root: Path, mid: str, goal: str) -> list[str]:
    """Write the skeleton, chunks pre-filled; the agent writes only the design parts."""
    path = plan_path(root, mid)
    if path.exists():
        raise DcError(f"{path.relative_to(root).as_posix()} exists; edit it, then plan check")
    chunks = _chunks(data, mid)
    if not chunks:
        raise DcError(f"milestone {mid} has no chunks: add them first (chunk add)")
    reasons, verdict = need(data, root, mid)
    par = bool(picked(verdict))
    lines = [f"# Plan {mid}: {goal}", "",
             f"Required: {'; '.join(reasons) if reasons else 'no (optional plan)'}",
             f"Fan-out: {verdict[0]}", "",
             "## Approach", f"{PLACEHOLDER} how the milestone is built, in a few lines", "",
             "## Interfaces",
             (f"{PLACEHOLDER} the contract between parallel chunks: signatures, file formats, flags, "
              "who owns which shared name" if par else
              f"{PLACEHOLDER} signatures, formats or flags other code relies on; 'none' if nothing crosses a chunk"),
             "", "## Chunks"]
    for c in chunks:
        files = [t.split("::", 1)[0] for t in c.get("targets") or []]
        lines += [f"### {c['id']} - {c['goal']}",
                  f"Files: {'; '.join(dict.fromkeys(files)) or PLACEHOLDER + ' files this chunk owns'}",
                  f"Test: {PLACEHOLDER} test file and what it proves", ""]
    lines += ["## Edge cases and risks", f"{PLACEHOLDER} inputs and failure modes, and how each is handled", "",
              "## Out of scope", f"{PLACEHOLDER} what this milestone does not do", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    _dcio.atomic_write(path, "\n".join(lines))
    return [f"PLAN {mid}: {path.relative_to(root).as_posix()} written; replace every "
            f"'{PLACEHOLDER}' line, then plan check --milestone {mid}"]


def check(data: dict, root: Path, mid: str) -> list[str]:
    """Every gap in the plan, one line each. [] means the plan is complete."""
    text = _dcio.read_text(plan_path(root, mid))
    if text is None:
        return [f"no plan: dc_project.py plan init --milestone {mid}"]
    gaps = []
    secs = sections(text)
    for name in SECTIONS:
        body = secs.get(name)
        if body is None:
            gaps.append(f"section '## {name}' is missing")
        elif not body.strip():
            gaps.append(f"section '## {name}' is empty")
    n = sum(1 for ln in text.split("\n") if PLACEHOLDER in ln)
    if n:
        gaps.append(f"{n} '{PLACEHOLDER}' line(s) left")
    _, verdict = need(data, root, mid)
    par = picked(verdict)
    inter = (secs.get("Interfaces") or "").strip().lower()
    if par and (not inter or inter.startswith("none")):
        gaps.append("fan-out is PARALLEL: Interfaces must state the contract between the builders")
    entries = chunk_entries(secs.get("Chunks") or "")
    ids = {c["id"] for c in _chunks(data, mid)}
    for extra in sorted(set(entries) - ids):
        gaps.append(f"### {extra} is not a chunk of {mid}")
    for c in _chunks(data, mid):
        e = entries.get(c["id"])
        if e is None:
            gaps.append(f"{c['id']} has no '### {c['id']}' entry under ## Chunks")
            continue
        if not e["files"]:
            gaps.append(f"{c['id']} has no Files line")
        if not e["test"]:
            gaps.append(f"{c['id']} has no Test line (which test proves it)")
        owned = set(e["files"])
        for t in c.get("targets") or []:
            p = t.split("::", 1)[0]
            if p and p not in owned and not any(p.startswith(o.rstrip("/") + "/") for o in owned):
                gaps.append(f"{c['id']} target {p} is not in its Files")
    import dc_project
    for i, a in enumerate(par):
        for b in par[i + 1:]:
            if a in entries and b in entries:
                shared = dc_project._overlap(set(entries[a]["files"]), set(entries[b]["files"]))
                if shared:
                    gaps.append(f"parallel chunks {a} and {b} both own {shared}")
    return gaps


def planned_tests(root: Path, mid: str) -> list[str]:
    """`c1: <test>` per chunk from the plan, for the builder brief and the Gate 6 packet."""
    text = _dcio.read_text(plan_path(root, mid))
    if text is None:
        return []
    entries = chunk_entries(sections(text).get("Chunks") or "")
    return [f"{cid}: {e['test']}" for cid, e in entries.items() if e["test"]][:TESTS_SHOWN]


def record(data: dict, root: Path, mid: str, op: str, reason: str = "") -> str:
    """Mutates data: approve (complete plan only) or waive (reason required)."""
    plans = data.setdefault("plans", {})
    if op == "approve":
        gaps = check(data, root, mid)
        if gaps:
            raise DcError(f"plan {mid} has {len(gaps)} gap(s); run plan check and fix them first")
        text = _dcio.read_text(plan_path(root, mid)) or ""
        plans[mid] = {"status": "approved", "hash": _digest(text), "at": _now()}
        return f"PLAN {mid} approved ({plans[mid]['hash'][:12]})"
    if op == "waive":
        if len(reason.strip()) < 3:
            raise DcError("plan waive needs the user's reason (--reason)")
        plans[mid] = {"status": "waived", "reason": reason.strip()[:200], "at": _now()}
        return f"PLAN {mid} waived: {plans[mid]['reason']}"
    raise DcError(f"unknown plan op {op}")


def need_lines(data: dict, root: Path, mid: str) -> list[str]:
    reasons, verdict = need(data, root, mid)
    st = status(data, root, mid)
    head = (f"PLAN REQUIRED {mid}: {'; '.join(reasons)}" if reasons
            else f"PLAN OPTIONAL {mid}: one chunk set, no large chunk, under {PLAN_EST_MIN} min")
    return [head, f"plan status: {st}", f"fan-out: {verdict[0]}"]


def begun(data: dict, mid: str) -> bool:
    """A chunk of the milestone is done or started: building began before any plan."""
    return any(c.get("done") or c.get("start") for c in _chunks(data, mid))


def gate_refusal(data: dict, root: Path, mid: str) -> str | None:
    """Why a chunk may not start on the plan's account, or None.

    A required plan is asked for before the milestone's first chunk starts; a milestone
    already under way is not stalled. An approved plan edited since is always refused.
    """
    st = status(data, root, mid)
    if st in ("approved", "waived"):
        return None
    reasons, _ = need(data, root, mid)
    if st != "stale" and (not reasons or begun(data, mid)):
        return None
    if st == "stale":
        return (f"PLAN CHANGED {mid}: the plan was edited after approval; plan check, show the "
                f"user the change, then plan approve --milestone {mid}")
    return (f"PLAN REQUIRED {mid} ({'; '.join(reasons)}): plan init, fill it, plan check, "
            f"ask the user, plan approve --milestone {mid} (or plan waive --reason "
            f"\"<user's reason>\")")


def start_gate(data: dict, root: Path, mid: str, cid: str, sequential: str = "") -> list[str]:
    """Run before a chunk starts. Refuses (DcError) or returns the lines to print.

    Mutates data: records the fan-out verdict and any sequential reason for the milestone.
    """
    if not cid or not _chunks(data, mid):
        return []
    refusal = gate_refusal(data, root, mid)
    if refusal:
        raise DcError(refusal)
    gate = data.setdefault("gates", {}).setdefault(mid, {})
    verdict = _fanout(data, root, mid)
    pick = picked(verdict)
    out = []
    if cid in pick and not gate.get("sequential"):
        if len(sequential.strip()) < 3:
            raise DcError(f"FAN-OUT {mid}: {verdict[0]} - build these as the Conductor build "
                          f"(phase4: dc_project.py brief per chunk, builders on worktrees), or "
                          f"chunk start --sequential \"<reason>\" to build in order")
        gate["sequential"] = {"reason": sequential.strip()[:200], "at": _now(), "verdict": verdict[0]}
        out.append(f"FAN-OUT {mid}: sequential by choice ({gate['sequential']['reason']})")
    if gate.get("fanout") != verdict[0]:
        gate["fanout"] = verdict[0]
        out.append(f"FAN-OUT {mid}: {verdict[0]}")
    return out


def capsule_line(data: dict, root: Path, mid: str) -> str | None:
    """One `next` line while the plan blocks the milestone; None otherwise."""
    if not _chunks(data, mid) or gate_refusal(data, root, mid) is None:
        return None
    st = status(data, root, mid)
    if st == "stale":
        return f"Plan: {mid} edited after approval - re-approve before the next chunk (plan check)"
    reasons, _ = need(data, root, mid)
    return f"Plan: required ({'; '.join(reasons)}), {st} - plan init/check, then the user approves"
