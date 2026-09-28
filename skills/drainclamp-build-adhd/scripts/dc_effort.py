#!/usr/bin/env python3
"""Your time vs agent time per project, from Claude, Codex and Grok sessions on disk.

A prompt costs you the gap since the agent last finished, capped at 10 minutes: a quick reply
counts what it took, and a reply after a long gap counts 10 minutes, because you were somewhere
else while the agent worked. Agent time is the prompt to the agent's last output in that turn.

A prompt belongs to the project whose log session window holds it (log entries 90 minutes
apart or less form a session, padded 30 minutes each side). Otherwise it belongs to the most
specific project root containing the session's working folder -- unless that folder holds
other projects, because a parent folder cannot say which child was worked on. What is left is
the Unassigned pool. Each unassigned session carries a suggestion (the project root it names
most) and can be assigned by hand; assignments live beside the cache in the registry home.

The scan reads every session file, so it is run on demand (`refresh`) and cached. Titles are
the first 80 characters of a session's first prompt (Grok: its own summary) and stay local.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _dcio
import dc_registry
from _dcio import DcError

SCHEMA_VERSION = 1
CAP_MIN = 10
GAP_MIN = 90
PAD_MIN = 30
CACHE_NAME = "effort.json"
ASSIGN_NAME = "effort-assign.json"
TITLE_CAP = 80
SUGGEST_MIN = 3
SESSIONS_KEPT = 300
HOSTS = ("claude", "codex", "grok")
NOT_PROMPTS = ("<command-", "<local-command", "<system-reminder", "<task-notification")
LOG_TS = re.compile(r"^- (\d{4}-\d{2}-\d{2}T\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?)", re.M)
ARCHIVE_NAME = "drainclamp-log-archive.md"


def _ts(value) -> datetime | None:
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            return datetime.fromtimestamp(int(value), timezone.utc)
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _clip(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= TITLE_CAP else text[:TITLE_CAP - 1] + "\u2026"


def default_sources() -> dict[str, Path]:
    home = Path.home()
    return {"claude": home / ".claude" / "projects", "codex": home / ".codex" / "sessions",
            "grok": home / ".grok" / "sessions"}


# -- projects and their log windows -------------------------------------------------

def log_windows(root: Path) -> list[tuple[datetime, datetime]]:
    agent = root / _dcio.AGENT_DIR_NAME
    stamps = set()
    for name in (_dcio.STATE_NAME, ARCHIVE_NAME):
        for m in LOG_TS.findall(_dcio.read_text(agent / name) or ""):
            when = _ts(m)
            if when:
                stamps.add(when)
    runs: list[list[datetime]] = []
    for when in sorted(stamps):
        if runs and when - runs[-1][1] <= timedelta(minutes=GAP_MIN):
            runs[-1][1] = when
        else:
            runs.append([when, when])
    pad = timedelta(minutes=PAD_MIN)
    return [(a - pad, z + pad) for a, z in runs]


def projects(home: str | None) -> list[dict]:
    out = []
    for e in dc_registry.listing("active", home) + dc_registry.listing("complete", home):
        out.append({"id": dc_registry.project_id(e["root"]), "name": e.get("name") or Path(e["root"]).name,
                    "key": dc_registry._key(e["root"]), "windows": log_windows(Path(e["root"]))})
    keys = [p["key"] for p in out]
    for p in out:
        p["parent"] = any(k.startswith(p["key"] + "/") for k in keys)
    return out


class Matcher:
    """Turn -> project id: log window first, then working folder (parents excluded)."""

    def __init__(self, projs: list[dict]) -> None:
        self.projs = projs
        roots = sorted((p for p in projs if not p["parent"]), key=lambda p: -len(p["key"]))
        self.by_key = {p["key"]: p["id"] for p in roots}
        # Longest root first, so a child's path wins over its parent's; any slash run matches.
        alts = [r"[\\/]+".join(re.escape(part) for part in p["key"].split("/")) for p in roots]
        self.mention = re.compile("|".join(alts), re.I) if alts else None

    def by_window(self, when: datetime) -> str | None:
        hits = [p["id"] for p in self.projs if any(a <= when <= z for a, z in p["windows"])]
        return hits[0] if len(hits) == 1 else None

    def by_cwd(self, cwd: str) -> str | None:
        if not cwd:
            return None
        key = dc_registry._key(cwd)
        best = None
        for p in self.projs:
            if key == p["key"] or key.startswith(p["key"] + "/"):
                if best is None or len(p["key"]) > len(best["key"]):
                    best = p
        return None if best is None or best["parent"] else best["id"]

    def mentions(self, line: str, into: Counter) -> None:
        if self.mention is None:
            return
        for m in self.mention.finditer(line):
            pid = self.by_key.get(dc_registry._key(re.sub(r"[\\/]+", "/", m.group(0))))
            if pid:
                into[pid] += 1


# -- session readers: each returns {key, host, cwd, title, turns[(start, prev_end, end)], hits} --

def _session(key: str, host: str) -> dict:
    return {"key": key, "host": host, "cwd": "", "title": "", "turns": [], "hits": Counter()}


def _lines(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    yield line, json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _prompt_text(content) -> str | None:
    """Text of a human prompt, or None for tool results and harness messages."""
    if isinstance(content, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content):
            return None
        content = " ".join(x.get("text", "") for x in content if isinstance(x, dict))
    if not isinstance(content, str) or content.lstrip().startswith(NOT_PROMPTS):
        return None
    return content


def read_claude(path: Path, match: Matcher) -> dict:
    s = _session(f"claude:{path.stem}", "claude")
    last_out, cur = None, None
    for line, r in _lines(path):
        match.mentions(line, s["hits"])
        if r.get("isSidechain"):
            continue
        when = _ts(r.get("timestamp"))
        if when is None:
            continue
        if r.get("type") == "assistant":
            last_out = when
            if cur:
                cur[2] = when
        elif r.get("type") == "user" and not r.get("isMeta"):
            text = _prompt_text((r.get("message") or {}).get("content"))
            if text is None:
                continue
            s["cwd"] = s["cwd"] or r.get("cwd", "")
            s["title"] = s["title"] or _clip(text)
            cur = [when, last_out, None]
            s["turns"].append(cur)
    return s


def read_codex(path: Path, match: Matcher) -> dict:
    s = _session(f"codex:{path.stem}", "codex")
    last_out, cur = None, None
    for line, r in _lines(path):
        match.mentions(line, s["hits"])
        p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        kind = p.get("type") or r.get("type")
        if r.get("type") in ("session_meta", "turn_context") and p.get("cwd"):
            s["cwd"] = s["cwd"] or p["cwd"]
        when = _ts(r.get("timestamp"))
        if when is None:
            continue
        if kind == "user_message":
            s["title"] = s["title"] or _clip(p.get("message", ""))
            cur = [when, last_out, None]
            s["turns"].append(cur)
        elif kind in ("agent_message", "task_complete") or \
                (r.get("type") == "response_item" and p.get("role") == "assistant"):
            last_out = when
            if cur:
                cur[2] = when
    return s


def read_grok(folder: Path, match: Matcher) -> dict:
    s = _session(f"grok:{folder.name}", "grok")
    try:
        summary = json.loads((folder / "summary.json").read_text(encoding="utf-8", errors="replace"))
        info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
        s["cwd"] = info.get("cwd", "") or ""
        s["title"] = _clip(summary.get("generated_title") or summary.get("session_summary") or "")
    except (OSError, ValueError, AttributeError):
        pass
    history = folder / "chat_history.jsonl"
    for line, _ in _lines(history):
        match.mentions(line, s["hits"])
    last_out, cur = None, None
    for _, r in _lines(folder / "events.jsonl"):
        when = _ts(r.get("ts"))
        if when is None:
            continue
        if r.get("type") == "turn_started":
            cur = [when, last_out, None]
            s["turns"].append(cur)
        elif r.get("type") == "turn_ended":
            last_out = when
            if cur:
                cur[2] = when
    return s


def sessions(sources: dict[str, Path], match: Matcher):
    root = sources.get("claude")
    if root and root.is_dir():
        for path in sorted(root.glob("*/*.jsonl")):
            yield read_claude(path, match)
    root = sources.get("codex")
    if root and root.is_dir():
        for path in sorted(root.rglob("*.jsonl")):
            yield read_codex(path, match)
    root = sources.get("grok")
    if root and root.is_dir():
        seen = set()
        for pattern in ("*/events.jsonl", "*/*/events.jsonl"):
            for path in sorted(root.glob(pattern)):
                if "subagents" in path.parts or path.parent in seen:
                    continue
                seen.add(path.parent)
                yield read_grok(path.parent, match)


# -- compute, cache, assign ----------------------------------------------------------

def _mine(start: datetime, prev_end: datetime | None) -> float:
    gap = (start - prev_end).total_seconds() / 60 if prev_end and start > prev_end else CAP_MIN
    return min(gap, CAP_MIN)


def _bucket() -> dict:
    return {"prompts": 0, "you_min": 0.0, "agent_min": 0.0, "hosts": {}}


def _add(b: dict, host: str, you: float, agent: float) -> None:
    b["prompts"] += 1
    b["you_min"] += you
    b["agent_min"] += agent
    b["hosts"][host] = b["hosts"].get(host, 0.0) + you


def compute(home: str | None = None, sources: dict[str, Path] | None = None,
            now: datetime | None = None) -> dict:
    projs = projects(home)
    match = Matcher(projs)
    names = {p["id"]: p["name"] for p in projs}
    per: dict[str, dict] = {}
    pool: list[dict] = []
    scanned = Counter()
    for s in sessions(sources or default_sources(), match):
        scanned[s["host"]] += 1
        left = _bucket()
        first = last = None
        for start, prev_end, end in s["turns"]:
            you = _mine(start, prev_end)
            agent = (end - start).total_seconds() / 60 if end and end > start else 0.0
            pid = match.by_window(start) or match.by_cwd(s["cwd"])
            if pid:
                _add(per.setdefault(pid, _bucket()), s["host"], you, agent)
                continue
            _add(left, s["host"], you, agent)
            first = first or start
            last = start
        if left["prompts"]:
            hit = s["hits"].most_common(1)
            suggest = hit[0][0] if hit and hit[0][1] >= SUGGEST_MIN else None
            pool.append({"key": s["key"], "host": s["host"], "title": s["title"],
                         "cwd": s["cwd"], "start": first.isoformat() if first else None,
                         "end": last.isoformat() if last else None, "prompts": left["prompts"],
                         "you_min": round(left["you_min"], 1), "agent_min": round(left["agent_min"], 1),
                         "suggest": suggest, "suggest_name": names.get(suggest)})
    for b in per.values():
        b["you_min"], b["agent_min"] = round(b["you_min"], 1), round(b["agent_min"], 1)
        b["hosts"] = {k: round(v, 1) for k, v in b["hosts"].items()}
    pool.sort(key=lambda x: -x["you_min"])
    return {"schema": SCHEMA_VERSION, "computed_at": (now or datetime.now(timezone.utc)).replace(
                microsecond=0).isoformat(),
            "rule": {"cap_min": CAP_MIN, "gap_min": GAP_MIN, "pad_min": PAD_MIN},
            "scanned": dict(scanned), "projects": per, "unassigned": pool}


def cache_path(home: str | None) -> Path:
    return dc_registry.home_dir(home) / CACHE_NAME


def assign_path(home: str | None) -> Path:
    return dc_registry.home_dir(home) / ASSIGN_NAME


def _read_json(path: Path, default):
    text = _dcio.read_text(path)
    if text is None:
        return default
    try:
        data = json.loads(text)
    except ValueError:
        raise DcError(f"{path} is not valid JSON; left untouched") from None
    return data


def refresh(home: str | None = None, sources: dict[str, Path] | None = None) -> dict:
    raw = compute(home, sources)
    folder = dc_registry.home_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    with _dcio.FileLock(folder, name=".effort.lock"):
        _dcio.atomic_write(cache_path(home), json.dumps(raw, indent=1) + "\n")
    return view(home, raw)


def assignments(home: str | None) -> dict[str, str]:
    data = _read_json(assign_path(home), {})
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def assign(home: str | None, items: dict[str, str | None]) -> dict[str, str]:
    """Set (or clear, with None) the project for unassigned session keys."""
    folder = dc_registry.home_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    with _dcio.FileLock(folder, name=".effort.lock"):
        current = assignments(home)
        for key, pid in items.items():
            if pid:
                current[str(key)] = str(pid)
            else:
                current.pop(str(key), None)
        _dcio.atomic_write(assign_path(home), json.dumps(current, indent=1, sort_keys=True) + "\n")
    return current


def view(home: str | None = None, raw: dict | None = None) -> dict | None:
    """The cache with hand assignments applied, or None before the first refresh."""
    raw = raw if raw is not None else _read_json(cache_path(home), None)
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
        return None
    given = assignments(home)
    per = {k: {**v, "hosts": dict(v.get("hosts", {}))} for k, v in raw.get("projects", {}).items()}
    pool = []
    for s in raw.get("unassigned", []):
        pid = given.get(s["key"])
        if not pid:
            pool.append(s)
            continue
        b = per.setdefault(pid, _bucket())
        b["prompts"] += s["prompts"]
        b["you_min"] = round(b["you_min"] + s["you_min"], 1)
        b["agent_min"] = round(b["agent_min"] + s["agent_min"], 1)
        b["hosts"][s["host"]] = round(b["hosts"].get(s["host"], 0.0) + s["you_min"], 1)
    total = {"prompts": sum(s["prompts"] for s in pool),
             "you_min": round(sum(s["you_min"] for s in pool), 1), "hosts": {}}
    for s in pool:
        total["hosts"][s["host"]] = round(total["hosts"].get(s["host"], 0.0) + s["you_min"], 1)
    return {**raw, "projects": per, "unassigned": pool[:SESSIONS_KEPT],
            "unassigned_total": total, "assigned": len(given)}


# -- CLI -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_effort.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", default=None, help="registry/config directory override")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ref = sub.add_parser("refresh", help="scan every session and rewrite the cache")
    for host in HOSTS:
        ref.add_argument(f"--{host}", type=Path, default=None, help=f"{host} session root")
    show = sub.add_parser("show", help="print the cached totals")
    show.add_argument("--json", action="store_true")
    asg = sub.add_parser("assign", help="assign an unassigned session to a project id")
    asg.add_argument("key")
    asg.add_argument("project", nargs="?", default=None, help="project id; omit to clear")
    sub.add_parser("accept", help="assign every unassigned session to its suggestion")
    args = parser.parse_args()

    if args.cmd == "refresh":
        src = default_sources()
        for host in HOSTS:
            if getattr(args, host) is not None:
                src[host] = getattr(args, host)
        v = refresh(args.home, src)
        print(f"DRAINCLAMP: effort cached ({', '.join(f'{k} {n}' for k, n in v['scanned'].items())}"
              f" sessions; {v['unassigned_total']['prompts']} prompts unassigned)")
        return _dcio.EXIT_OK
    if args.cmd == "assign":
        assign(args.home, {args.key: args.project})
        print(f"DRAINCLAMP: {args.key} {'assigned to ' + args.project if args.project else 'cleared'}")
        return _dcio.EXIT_OK
    v = view(args.home)
    if v is None:
        raise DcError("no effort cache yet; run dc_effort.py refresh")
    if args.cmd == "accept":
        picks = {s["key"]: s["suggest"] for s in v["unassigned"] if s.get("suggest")}
        assign(args.home, picks)
        print(f"DRAINCLAMP: {len(picks)} sessions assigned to their suggestion")
        return _dcio.EXIT_OK
    if args.json:
        print(json.dumps(v, indent=1))
        return _dcio.EXIT_OK
    names = {p["id"]: p["name"] for p in projects(args.home)}
    print(f"DRAINCLAMP EFFORT: computed {v['computed_at']} | rule min(gap, {CAP_MIN} min) per prompt")
    for pid, b in sorted(v["projects"].items(), key=lambda kv: -kv[1]["you_min"]):
        print(f"  {names.get(pid, pid)[:32]:32} you {b['you_min'] / 60:5.1f} h  agent "
              f"{b['agent_min'] / 60:5.1f} h  prompts {b['prompts']}")
    u = v["unassigned_total"]
    print(f"  Unassigned: {u['you_min'] / 60:.1f} h over {u['prompts']} prompts "
          f"({len(v['unassigned'])} sessions)")
    return _dcio.EXIT_OK


if __name__ == "__main__":
    _dcio.run_cli(main)
