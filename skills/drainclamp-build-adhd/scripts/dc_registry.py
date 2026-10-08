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
        return {"root": str(resolved).replace("\\", "/"), "name": name or resolved.name,
                "status": "active", "last": _now()}
    lock_dir = home_dir(home)
    lock_dir.mkdir(parents=True, exist_ok=True)
    with _dcio.FileLock(lock_dir):
        reg = load(home)
        entry = find(reg, resolved)
        if entry is None:
            entry = {
                "root": str(resolved).replace("\\", "/"),
                "name": name or resolved.name,
                "status": "active",
                "last": _now(),
            }
            reg["projects"].append(entry)
        else:
            entry["last"] = _now()
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
    return rows
