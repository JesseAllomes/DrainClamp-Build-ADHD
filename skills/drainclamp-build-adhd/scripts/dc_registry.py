#!/usr/bin/env python3
"""Cross-repository project registry: `~/.drainclamp/projects.json`.

Per-repository state stays in `.agent/drainclamp-state.md`. This file is the
index over those repositories, so a session can ask "what am I working on?"
without scanning the disk.

The registry is a cache, never an authority. Every read prunes entries whose
root or state file has gone, and a corrupt file is replaced rather than
repaired -- losing the index costs a rescan, while trusting a damaged one
would point work at the wrong repository.

A repository in the system temp directory is a throwaway (a test, a stress
harness), so a registry outside it never indexes one: otherwise every scratch
repository a script makes lands in the Gate S menu and stays while its
directory does.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import _dcio
from _dcio import DcError

SCHEMA_VERSION = 1
HOME_ENV = "DRAINCLAMP_HOME"
DIR_NAME = ".drainclamp"
REGISTRY_NAME = "projects.json"
STATUSES = ("active", "complete")


def home_dir(home: Path | str | None = None) -> Path:
    """Registry directory. Explicit argument > DRAINCLAMP_HOME > ~/.drainclamp."""
    if home is not None:
        return Path(home).expanduser().resolve()
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / DIR_NAME


def registry_path(home: Path | str | None = None) -> Path:
    return home_dir(home) / REGISTRY_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty() -> dict:
    return {"schema": SCHEMA_VERSION, "projects": []}


def _key(root: Path | str) -> str:
    """Comparison key. Case-folded on Windows, where paths are insensitive."""
    text = str(root).replace("\\", "/").rstrip("/")
    return text.casefold() if os.name == "nt" else text


def group_of(root: Path | str) -> str | None:
    """Group name (the git top-level folder) when `root` is a project below it, else None."""
    resolved = Path(root).resolve()
    top = _dcio.repo_root(resolved)
    if top == resolved or not _dcio.is_within(resolved, top):
        return None
    return top.name


def display_name(root: Path | str) -> str:
    """`group › project` below a git top level, else the folder name."""
    resolved = Path(root).resolve()
    group = group_of(resolved)
    if group is None:
        return resolved.name
    return f"{group} › {resolved.relative_to(_dcio.repo_root(resolved)).as_posix()}"


def _valid(entry) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("root"), str)
        and entry.get("status") in STATUSES
    )


def load(home: Path | str | None = None) -> dict:
    """Read the registry. A missing, unreadable, or malformed file is empty."""
    text = _dcio.read_text(registry_path(home))
    if text is None:
        return _empty()
    try:
        data = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return _empty()
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return _empty()
    projects = data.get("projects")
    if not isinstance(projects, list):
        return _empty()
    return {"schema": SCHEMA_VERSION, "projects": [e for e in projects if _valid(e)]}


def state_file(root: Path | str) -> Path:
    return Path(root) / _dcio.AGENT_DIR_NAME / _dcio.STATE_NAME


def _within(path: Path, parent: Path) -> bool:
    return _key(path) == _key(parent) or _key(path).startswith(_key(parent) + "/")


def throwaway(root: Path | str, home: Path | str | None = None) -> bool:
    """True for a temp-directory repository that this registry should not index.

    A registry that is itself in the temp directory (a test's) indexes anything.
    """
    temp = Path(tempfile.gettempdir()).resolve()
    return _within(Path(root).resolve(), temp) and not _within(home_dir(home).resolve(), temp)


def prune(reg: dict, home: Path | str | None = None) -> tuple[dict, list[dict]]:
    """Drop entries whose repository or state file has gone, and throwaways.

    Returns (registry, dropped). Pruning is not a mutation of disk on its own;
    the caller saves if it wants the result persisted.
    """
    kept, dropped = [], []
    for entry in reg.get("projects", []):
        if state_file(entry["root"]).is_file() and not throwaway(entry["root"], home):
            kept.append(entry)
        else:
            dropped.append(entry)
    return {"schema": SCHEMA_VERSION, "projects": kept}, dropped


def save(reg: dict, home: Path | str | None = None) -> Path:
    path = registry_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA_VERSION, "projects": reg.get("projects", [])}
    _dcio.atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def find(reg: dict, root: Path | str) -> dict | None:
    target = _key(root)
    for entry in reg.get("projects", []):
        if _key(entry["root"]) == target:
            return entry
    return None


def touch(root: Path | str, home: Path | str | None = None,
          name: str | None = None) -> dict:
    """Record that `root` was worked on now. Never changes an existing status.

    Marking a project complete is a deliberate act; a later write to its state
    must not silently reopen it. A throwaway gets an entry that is never saved.
    """
    resolved = Path(root).resolve()
    if throwaway(resolved, home):
        return {"root": str(resolved).replace("\\", "/"), "name": name or display_name(resolved),
                "status": "active", "last": _now()}
    lock_dir = home_dir(home)
    lock_dir.mkdir(parents=True, exist_ok=True)
    with _dcio.FileLock(lock_dir):
        reg = load(home)
        entry = find(reg, resolved)
        group = group_of(resolved)
        if entry is None:
            entry = {
                "root": str(resolved).replace("\\", "/"),
                "name": name or display_name(resolved),
                "status": "active",
                "last": _now(),
            }
            reg["projects"].append(entry)
        else:
            entry["last"] = _now()
        if group and "group" not in entry:
            entry["group"] = group  # additive; entries keyed by a top-level root never get one
        save(reg, home)
    return entry


def set_status(root: Path | str, status: str,
               home: Path | str | None = None) -> dict:
    if status not in STATUSES:
        raise DcError(f"unknown status: {status}")
    resolved = Path(root).resolve()
    lock_dir = home_dir(home)
    lock_dir.mkdir(parents=True, exist_ok=True)
    with _dcio.FileLock(lock_dir):
        reg = load(home)
        entry = find(reg, resolved)
        if entry is None:
            raise DcError(f"not a registered project: {resolved}")
        entry["status"] = status
        entry["last"] = _now()
        save(reg, home)
    return entry


def forget(root: Path | str, home: Path | str | None = None) -> bool:
    """Drop a repository from the index. The repository itself is untouched.

    Removing the index entry is not the same as completing a project, so this
    never writes to the repository -- a re-registered root comes back with its
    state intact.
    """
    resolved = Path(root).resolve()
    lock_dir = home_dir(home)
    lock_dir.mkdir(parents=True, exist_ok=True)
    with _dcio.FileLock(lock_dir):
        reg = load(home)
        target = _key(resolved)
        kept = [e for e in reg["projects"] if _key(e["root"]) != target]
        removed = len(kept) != len(reg["projects"])
        if removed:
            save({"schema": SCHEMA_VERSION, "projects": kept}, home)
    return removed


def listing(status: str = "active", home: Path | str | None = None) -> list[dict]:
    """Pruned entries of one status, most recently worked first.

    The sort is the numbering the menu shows, so a follow-up `--complete 2`
    resolves against the same order the user read.
    """
    reg, dropped = prune(load(home), home)
    if dropped:
        try:
            save(reg, home)
        except (OSError, DcError):
            pass  # a stale index is survivable; a crash here is not
    rows = [e for e in reg["projects"] if e.get("status") == status]
    rows.sort(key=lambda e: (e.get("last") or "", e.get("name") or ""), reverse=True)
    return cluster_groups(rows)


def cluster_groups(rows: list[dict]) -> list[dict]:
    """Keep a group's projects together, at the place its most recent project sits.

    The menu numbers this order, so a follow-up `--complete 2` resolves against the
    same list the user read. With no grouped entries the order is untouched.
    """
    out: list[dict] = []
    placed: set[str] = set()
    for entry in rows:
        group = entry.get("group")
        if not group:
            out.append(entry)
        elif group not in placed:
            placed.add(group)
            out.extend(e for e in rows if e.get("group") == group)
    return out


# --------------------------------------------------------------------------
# Session presence (G11)
#
# Which session is working in a project, kept in the project's sidecar under the
# additive key "presence": {host, session, since, heartbeat}. Nothing else is
# stored: no tokens, no user identity beyond the session name the host shows.
# --------------------------------------------------------------------------

PRESENCE_KEY = "presence"
PRESENCE_STALE_MINUTES = 120
# Hosts that can message another session directly. Others get a paste line.
DIRECT_HOSTS = {"claude": "SendMessage"}


def _presence_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _presence_iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _presence_age(entry, now: datetime | None = None) -> float | None:
    """Minutes since the heartbeat, or None when the entry is unusable."""
    if not isinstance(entry, dict) or not entry.get("session"):
        return None
    try:
        beat = datetime.fromisoformat(str(entry.get("heartbeat")))
    except ValueError:
        return None
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    return max(0.0, ((now or _presence_now()) - beat).total_seconds() / 60)


def live_presence(data: dict, now: datetime | None = None) -> dict | None:
    """The recorded session if its heartbeat is under two hours old, else None."""
    entry = data.get(PRESENCE_KEY)
    age = _presence_age(entry, now)
    if age is None or age > PRESENCE_STALE_MINUTES:
        return None
    return entry


def in_use_line(entry: dict, now: datetime | None = None) -> str:
    return f"IN USE: {entry['session']} (last active {int(_presence_age(entry, now))} min ago)"


def _clean_name(text: str, label: str) -> str:
    text = " ".join(str(text or "").split())
    if not text or len(text) > 80:
        raise DcError(f"{label} must be 1-80 characters")
    return text


def claim_presence(root: Path | str, host: str, session: str, force: bool = False,
                   now: datetime | None = None) -> str | None:
    """Record this session unless another live one holds the project.

    Returns the IN USE line when another session is live (and nothing is
    written, unless `force`), else None. The same session just refreshes.
    """
    import dc_project  # deferred: dc_project imports this module
    host = _clean_name(host, "host").lower()
    session = _clean_name(session, "session")
    when = now or _presence_now()
    agent = Path(root) / _dcio.AGENT_DIR_NAME
    seen: dict = {}

    def apply(data: dict) -> dict:
        cur = live_presence(data, when)
        if cur is not None and cur.get("session") != session and not force:
            seen["line"] = in_use_line(cur, when)
            data["rev"] -= 1  # nothing changed; mutate adds one back
            return data
        same = cur is not None and cur.get("session") == session and cur.get("host") == host
        data[PRESENCE_KEY] = {"host": host, "session": session,
                              "since": cur.get("since") if same and cur.get("since") else _presence_iso(when),
                              "heartbeat": _presence_iso(when)}
        return data

    dc_project.mutate(agent, apply)
    return seen.get("line")


def heartbeat(agent: Path | str) -> None:
    """Refresh an existing presence entry. Best effort: never raises."""
    try:
        import dc_project
        agent = Path(agent)
        if not dc_project.sidecar_path(agent).is_file():
            return
        stamp = _presence_iso(_presence_now())

        def apply(data: dict) -> dict:
            entry = data.get(PRESENCE_KEY)
            if isinstance(entry, dict) and entry.get("session"):
                entry["heartbeat"] = stamp
            else:
                data["rev"] -= 1
            return data

        dc_project.mutate(agent, apply)
    except Exception:  # noqa: BLE001 - a heartbeat must never fail a state write
        pass


def handoff_line(root: Path | str, file: str, what: str, me: str | None = None,
                 now: datetime | None = None) -> str:
    """One line telling the agent how to pass a note to the live session."""
    import dc_project
    data = dc_project.load(Path(root) / _dcio.AGENT_DIR_NAME)
    entry = live_presence(data, now)
    if entry is None or (me and entry.get("session") == me):
        return "HANDOFF: none (no live session)"
    message = f"{file} -- {' '.join(str(what).split())}"
    session = entry["session"]
    if DIRECT_HOSTS.get(entry.get("host")):
        return f"HANDOFF: {DIRECT_HOSTS[entry['host']]} to {session}: {message}"
    return f"HANDOFF: paste to {session}: {message}"
